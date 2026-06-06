"""Qt click-through overlay for Show Numbers — numbered pill labels.

Frameless, transparent, always-on-top, WindowTransparentForInput so physical
mouse clicks pass straight through to the app below. Does not steal focus.
"""

import ctypes
import ctypes.wintypes as _wt
import logging
import sys

from PySide6.QtCore import Qt, QRect, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QFont
from PySide6.QtWidgets import QApplication, QWidget

_logger = logging.getLogger(__name__)

_PILL_BG  = QColor(18, 18, 22, 230)
_PILL_BD  = QColor(70, 70, 80, 200)
_TEXT_CLR = QColor(255, 255, 255, 255)

# Set True to emit [DPI-COORD] and [OVERLAY-GEOM] debug lines.
_COORD_DEBUG = False

# ---------------------------------------------------------------------------
# Thread-level DPI awareness (Phase 3 fix)
# ---------------------------------------------------------------------------

def _ensure_dpi_thread_context() -> None:
    """Set per-monitor DPI V2 awareness on the calling thread.

    The samsara-qt thread creates Qt HWNDs.  SetProcessDpiAwareness is
    process-wide but Windows assigns per-thread DPI context based on the
    thread that calls CreateWindow.  Without this, HWNDs created on the
    background Qt thread may inherit system-DPI-aware context, causing
    devicePixelRatio() to be 1.0 on a 1.5x screen and the painter to use
    physical instead of logical coordinates -> top-left cluster bug.
    """
    if sys.platform != 'win32':
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)(LONG_PTR)-4
        ctypes.windll.user32.SetThreadDpiAwarenessContext(
            ctypes.c_ssize_t(-4)
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# DPI-aware physical-to-logical coordinate conversion
# ---------------------------------------------------------------------------

class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _wt.RECT),
        ("rcWork", _wt.RECT),
        ("dwFlags", ctypes.c_ulong),
    ]

_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    _wt.BOOL, _wt.HMONITOR, _wt.HDC, ctypes.POINTER(_wt.RECT), _wt.LPARAM
)


def _win32_monitor_rects() -> list:
    """Return Win32 monitor rects (physical or logical, same as Qt), sorted (left, top)."""
    rects = []

    def _cb(hmon, hdc, lprect, lparam):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcMonitor
        rects.append((r.left, r.top, r.right, r.bottom))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)
    return sorted(rects, key=lambda r: (r[0], r[1]))


def phys_to_logical(px: int, py: int) -> tuple:
    """Convert screen coordinates to Qt logical coordinates.

    Handles two coordinate modes automatically:

    * Physical mode (per-monitor DPI V2 active): Win32 rcMonitor width exceeds
      Qt logical width by the scale factor. Divide offset by ratio to convert.

    * Logical mode (DPI awareness not in effect for this API path): rcMonitor
      width matches Qt logical width. Coordinates are already logical; return
      unchanged.  Applying division here was the top-left-cluster bug.

    Falls back to identity if conversion data is unavailable.
    """
    try:
        phys_rects = _win32_monitor_rects()
        qt_screens = sorted(
            QApplication.screens(),
            key=lambda s: (s.geometry().x(), s.geometry().y()),
        )
        if len(phys_rects) != len(qt_screens):
            return px, py

        for (pl, pt, pr, pb), screen in zip(phys_rects, qt_screens):
            if pl <= px < pr and pt <= py < pb:
                ratio = screen.devicePixelRatio()
                geo = screen.geometry()
                win32_w = pr - pl
                qt_w    = geo.width()
                # Determine coordinate mode:
                # Logical: Win32 origins AND dimensions match Qt logical values.
                # Physical: at least the width or origin differs (DPI V2 active).
                # Width alone is insufficient for 100% DPI secondaries whose
                # physical width equals logical width but origin shifts left.
                is_logical = (
                    abs(win32_w - qt_w) <= 2
                    and pl == geo.x()
                    and pt == geo.y()
                )

                if is_logical:
                    if _COORD_DEBUG:
                        _logger.debug(
                            "[DPI-COORD] phys_to_logical(%d,%d): logical mode "
                            "(win32_w=%d == qt_w=%d, ratio=%.2f) -> (%d,%d)",
                            px, py, win32_w, qt_w, ratio, px, py,
                        )
                    return px, py

                # Origins or dimensions differ: Win32 returns physical pixels.
                lx = geo.x() + (px - pl) / ratio
                ly = geo.y() + (py - pt) / ratio
                if _COORD_DEBUG:
                    _logger.debug(
                        "[DPI-COORD] phys_to_logical(%d,%d): physical mode "
                        "(win32_w=%d, qt_w=%d, pl=%d, geo_x=%d, ratio=%.2f) -> (%d,%d)",
                        px, py, win32_w, qt_w, pl, geo.x(), ratio,
                        round(lx), round(ly),
                    )
                return round(lx), round(ly)
    except Exception:
        pass
    return px, py


# ---------------------------------------------------------------------------
# Overlay window
# ---------------------------------------------------------------------------

class NumbersOverlayWindow(QWidget):
    """Fullscreen transparent click-through window spanning all monitors.

    Renders numbered pill labels via QPainter at absolute screen logical
    coordinates.  Call update_labels() to refresh in place.
    """

    def __init__(self, labels: list) -> None:
        # Ensure the thread creating HWNDs has per-monitor DPI V2 context.
        # HWND creation is deferred to show() time; setting context here
        # ensures it is active on this thread when show() fires.
        _ensure_dpi_thread_context()

        super().__init__(None)
        self._labels = labels   # list of [screen_x, screen_y, pill_w, pill_h, text]

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        virt = QRect()
        for screen in QApplication.screens():
            virt = virt.united(screen.geometry())
        self._virt = virt
        self.setGeometry(virt)

        if _COORD_DEBUG:
            _logger.debug(
                "[OVERLAY-GEOM] _virt: x=%d y=%d w=%d h=%d",
                virt.x(), virt.y(), virt.width(), virt.height(),
            )
            _logger.debug(
                "[OVERLAY-GEOM] setGeometry: x=%d y=%d w=%d h=%d",
                self.geometry().x(), self.geometry().y(),
                self.geometry().width(), self.geometry().height(),
            )
            _logger.debug(
                "[OVERLAY-GEOM] devicePixelRatio=%.2f screen=%s",
                self.devicePixelRatio(),
                self.screen().name() if self.screen() else 'None',
            )
            for i, s in enumerate(QApplication.screens()):
                _logger.debug(
                    "[OVERLAY-GEOM] screen[%d]: geo=%s dpr=%.2f name=%s",
                    i, s.geometry(), s.devicePixelRatio(), s.name(),
                )
            app = QApplication.instance()
            if app:
                try:
                    _logger.debug(
                        "[OVERLAY-GEOM] Qt attrs: AA_EnableHighDpiScaling=%s "
                        "AA_UseHighDpiPixmaps=%s",
                        app.testAttribute(Qt.AA_EnableHighDpiScaling),
                        app.testAttribute(Qt.AA_UseHighDpiPixmaps),
                    )
                except AttributeError:
                    _logger.debug(
                        "[OVERLAY-GEOM] Qt6: high-DPI attrs removed (always on)"
                    )

    def update_labels(self, labels: list) -> None:
        self._labels = labels
        self.update()

    def paintEvent(self, event) -> None:
        if not self._labels:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # Detect and compensate for DPR mismatch.
        # If the overlay HWND was created with incorrect thread DPI context,
        # self.devicePixelRatio() < screen.devicePixelRatio().  In that case
        # the painter operates in physical pixels while our pill coords are in
        # screen logical pixels.  Scale painter so logical coords map correctly.
        widget_dpr = self.devicePixelRatio()
        screen     = self.screen()
        screen_dpr = screen.devicePixelRatio() if screen else widget_dpr
        # coord_scale > 1.0 when the widget DPR is lower than the screen DPR
        # (e.g. widget=1.0, screen=1.5 -> scale=1.5 so logical 1000 -> physical 1500)
        coord_scale = screen_dpr / widget_dpr if widget_dpr > 0 else 1.0

        if _COORD_DEBUG:
            _logger.debug(
                "[OVERLAY-PAINT] widget_dpr=%.2f screen_dpr=%.2f coord_scale=%.3f "
                "ox=%d oy=%d widget_w=%d widget_h=%d",
                widget_dpr, screen_dpr, coord_scale,
                self._virt.x(), self._virt.y(),
                self.width(), self.height(),
            )
            for lbl in self._labels[:3]:
                sx, sy, pw, ph, text = lbl
                ox, oy = self._virt.x(), self._virt.y()
                rx = (sx - ox) * coord_scale
                ry = (sy - oy) * coord_scale
                _logger.debug(
                    "[OVERLAY-PAINT] pill '%s': screen=(%d,%d) "
                    "local=(%.1f,%.1f) sz=(%.0fx%.0f)",
                    text, sx, sy, rx, ry, pw * coord_scale, ph * coord_scale,
                )

        font = QFont("Segoe UI", 11, QFont.Bold)
        painter.setFont(font)

        ox = self._virt.x()
        oy = self._virt.y()

        for sx, sy, pw, ph, text in self._labels:
            lx = (sx - ox) * coord_scale
            ly = (sy - oy) * coord_scale
            lw = pw * coord_scale
            lh = ph * coord_scale
            rect = QRectF(lx, ly, lw, lh)

            path = QPainterPath()
            path.addRoundedRect(rect, 4.0, 4.0)
            painter.fillPath(path, _PILL_BG)

            painter.setPen(_PILL_BD)
            painter.drawPath(path)

            painter.setPen(_TEXT_CLR)
            painter.drawText(rect, Qt.AlignCenter, text)

        painter.end()
