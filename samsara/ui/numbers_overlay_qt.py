"""Qt click-through overlay for Show Numbers — numbered pill labels.

Frameless, transparent, always-on-top, WindowTransparentForInput so physical
mouse clicks pass straight through to the app below. Does not steal focus.
"""

import ctypes
import ctypes.wintypes as _wt

from PySide6.QtCore import Qt, QRect, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QFont
from PySide6.QtWidgets import QApplication, QWidget

_PILL_BG  = QColor(18, 18, 22, 230)
_PILL_BD  = QColor(70, 70, 80, 200)
_TEXT_CLR = QColor(255, 255, 255, 255)

# Set True to emit [DPI-COORD] debug lines. Turn off after confirming fix.
_COORD_DEBUG = False

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
    """Return physical monitor rects from Win32, sorted (left, top)."""
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
    import logging as _log
    _logger = _log.getLogger(__name__)

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
                    # Win32 and Qt agree on origin and size: both use the same
                    # coordinate space. No conversion needed.
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


class NumbersOverlayWindow(QWidget):
    """Fullscreen transparent click-through window spanning all monitors.

    Renders numbered pill labels via QPainter at absolute screen coordinates.
    Call update_labels() to refresh labels in place without recreating the window.
    """

    def __init__(self, labels: list) -> None:
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

    def update_labels(self, labels: list) -> None:
        self._labels = labels
        self.update()

    def paintEvent(self, event) -> None:
        if not self._labels:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        font = QFont("Segoe UI", 11, QFont.Bold)
        painter.setFont(font)

        ox = self._virt.x()
        oy = self._virt.y()

        for sx, sy, pw, ph, text in self._labels:
            rect = QRectF(sx - ox, sy - oy, pw, ph)

            path = QPainterPath()
            path.addRoundedRect(rect, 4.0, 4.0)
            painter.fillPath(path, _PILL_BG)

            painter.setPen(_PILL_BD)
            painter.drawPath(path)

            painter.setPen(_TEXT_CLR)
            painter.drawText(rect, Qt.AlignCenter, text)

        painter.end()
