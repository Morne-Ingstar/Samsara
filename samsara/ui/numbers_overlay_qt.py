"""Qt click-through overlay for Show Numbers — numbered pill labels.

Frameless, transparent, always-on-top, WindowTransparentForInput so physical
mouse clicks pass straight through to the app below. Does not steal focus.
"""

import ctypes
import ctypes.wintypes as _wt
import logging
import sys

from PySide6.QtCore import Qt, QPoint, QRect, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QFont
from PySide6.QtWidgets import QApplication, QWidget

from samsara.log import get_logger

logger = get_logger(__name__)

_logger = logging.getLogger(__name__)

_PILL_BG  = QColor(18, 18, 22, 230)
_PILL_BD  = QColor(70, 70, 80, 200)
_TEXT_CLR = QColor(255, 255, 255, 255)

# Set True to emit [DPI-COORD] and [OVERLAY-GEOM] debug lines.
# False by default to keep session logs clean; enable only when diagnosing
# coordinate or DPI issues on a specific machine.
_COORD_DEBUG = False

# Pill anchor offsets in logical pixels.
# Each pill's bottom-right corner is placed at (element_x + DX, element_y + DY)
# so labels float just outside the element corner rather than covering its content.
# DX = -(pill_width) + margin_right, DY = -(pill_height) + margin_bottom.
# These are added to the raw element logical coord before computing pill top-left:
#   pill_x = element_x - pill_width + PILL_ANCHOR_DX
#   pill_y = element_y - pill_height + PILL_ANCHOR_DY
# A value of -4 leaves a 4px gap between the pill's right/bottom edge and the corner.
PILL_ANCHOR_DX = -4
PILL_ANCHOR_DY = -4

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
    except Exception as e:
        logger.debug(f"_ensure_dpi_thread_context: {e}")


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


def _with_physical_dpi_context(fn):
    """Run a Win32 geometry query in per-monitor-v2 physical coordinates.

    UI Automation bounding rectangles are always physical pixels.  Win32
    monitor APIs, however, can be virtualized according to the calling
    thread's DPI context.  Temporarily forcing PMv2 makes both sides use the
    same native coordinate system; the prior context is restored immediately.
    """
    if sys.platform != 'win32':
        return fn()
    setter = getattr(ctypes.windll.user32, 'SetThreadDpiAwarenessContext', None)
    if setter is None:
        return fn()

    previous = None
    try:
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        previous = setter(ctypes.c_void_p(-4))
        return fn()
    finally:
        if previous:
            setter(previous)


def _win32_monitor_rects() -> list:
    """Return native physical monitor rectangles sorted by physical origin."""
    def _query():
        rects = []

        def _cb(hmon, hdc, lprect, lparam):
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
            r = info.rcMonitor
            rects.append((r.left, r.top, r.right, r.bottom))
            return True

        callback = _MONITORENUMPROC(_cb)
        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
        return sorted(rects, key=lambda r: (r[0], r[1]))

    return _with_physical_dpi_context(_query)


def _map_physical_to_qt(
    px: int,
    py: int,
    mappings: list,
) -> tuple:
    """Pure physical-pixel -> Qt-DIP transform for one monitor mapping.

    Mapping entries are (physical_rect, qt_rect, dpr), where rects are
    (left, top, right, bottom). Windows keeps monitor origins in native
    desktop coordinates while Qt scales each screen's size, so conversion is
    relative to the matched monitor origin rather than global division.
    """
    for (pl, pt, pr, pb), (ql, qt, qr, qb), ratio in mappings:
        if pl <= px < pr and pt <= py < pb:
            scale = float(ratio) if ratio else 1.0
            return (
                round(ql + (px - pl) / scale),
                round(qt + (py - pt) / scale),
            )
    return px, py


def phys_to_logical(px: int, py: int) -> tuple:
    """Convert UI Automation physical screen coordinates to Qt logical DIPs.

    Microsoft specifies that UIA bounding rectangles use physical pixels.
    Qt 6 widget/screen geometry uses device-independent pixels. Always map
    through the containing monitor; never infer UIA's coordinate system from
    a separately virtualized Win32 query.
    """
    try:
        physical = _win32_monitor_rects()
        qt_screens = sorted(
            QApplication.screens(),
            key=lambda s: (s.geometry().x(), s.geometry().y()),
        )
        if len(physical) != len(qt_screens):
            return px, py

        mappings = []
        for physical_rect, screen in zip(physical, qt_screens):
            geo = screen.geometry()
            qt_rect = (
                geo.x(), geo.y(),
                geo.x() + geo.width(), geo.y() + geo.height(),
            )
            mappings.append((physical_rect, qt_rect, screen.devicePixelRatio()))

        result = _map_physical_to_qt(px, py, mappings)
        if _COORD_DEBUG:
            _logger.debug(
                "[DPI-COORD] UIA physical (%d,%d) -> Qt logical (%d,%d)",
                px, py, result[0], result[1],
            )
        return result
    except Exception as e:
        logger.debug(f"phys_to_logical: {e}")
        return px, py


# ---------------------------------------------------------------------------
# Active screen detection
# ---------------------------------------------------------------------------

def screen_for_hwnd(hwnd: int) -> "QScreen":
    """Return the QScreen that hwnd is on.

    Safe to call from any thread.  phys_to_logical adapts to the calling
    thread's DPI context so MonitorFromWindow + GetMonitorInfo coordinates
    are correctly mapped regardless of whether the thread is DPI V2-aware.
    Falls back to the primary screen.
    """
    if hwnd and sys.platform == 'win32':
        try:
            MONITOR_DEFAULTTONEAREST = 2
            hmon = ctypes.windll.user32.MonitorFromWindow(
                ctypes.c_ssize_t(hwnd), MONITOR_DEFAULTTONEAREST
            )
            if hmon:
                def _monitor_origin():
                    info = _MONITORINFO()
                    info.cbSize = ctypes.sizeof(_MONITORINFO)
                    ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
                    r = info.rcMonitor
                    return r.left + 1, r.top + 1

                # Query the monitor origin in the same physical coordinate
                # system UIA uses, then map it into Qt DIPs.
                px, py = _with_physical_dpi_context(_monitor_origin)
                lx, ly = phys_to_logical(px, py)
                screen = QApplication.screenAt(QPoint(lx, ly))
                if screen is not None:
                    return screen
        except Exception as e:
            logger.debug(f"screen_for_hwnd: {e}")
    primary = QApplication.primaryScreen()
    if primary is not None:
        return primary
    screens = QApplication.screens()
    return screens[0] if screens else None


# ---------------------------------------------------------------------------
# Overlay window
# ---------------------------------------------------------------------------

class NumbersOverlayWindow(QWidget):
    """Click-through overlay covering ONE monitor with numbered pill labels.

    Positioned to cover target_screen only — no multi-monitor spanning.
    Labels carry absolute screen logical coordinates; paintEvent subtracts
    the screen's logical origin (self._virt.x()/y()) to get widget-local
    coords.  Call update_labels() to refresh in place.
    """

    def __init__(self, labels: list, target_screen: "QScreen") -> None:
        # Set per-monitor DPI V2 on this thread before HWND creation.
        # Qt creates HWNDs lazily at show() time; the thread context at that
        # moment determines the HWND's effective DPI awareness.
        _ensure_dpi_thread_context()

        super().__init__(None)
        self._labels = labels   # list of [screen_x, screen_y, pill_w, pill_h, text]
        # Set only when this overlay is showing as a visible fallback from a
        # failed DOM (browser-extension) Show Numbers attempt -- see
        # plugins/commands/show_numbers.py's _try_show_dom_numbers. Empty
        # string means "no caption" (the normal, non-fallback case).
        self._caption = ""

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        # Materialize the native window and bind it to the intended QScreen
        # before assigning geometry. This makes the window's DPR come from
        # the target monitor rather than whichever screen Qt guessed first.
        int(self.winId())
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(target_screen)

        geo = target_screen.geometry()   # Qt device-independent pixels
        self._virt = QRect(geo)          # stable origin used by paintEvent
        self.setGeometry(geo)

        if _COORD_DEBUG:
            _logger.debug(
                "[OVERLAY-GEOM] target screen: name=%s geo=%s dpr=%.2f",
                target_screen.name(), geo, target_screen.devicePixelRatio(),
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

    def update_labels(self, labels: list, caption: str = "") -> None:
        self._labels = labels
        self._caption = caption
        self.update()

    def paintEvent(self, event) -> None:
        if not self._labels:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # QPainter on a QWidget already consumes Qt device-independent
        # coordinates and applies the window DPR to the backing store. A
        # second manual DPR scale is a double-transform on high-DPI screens.
        widget_dpr = self.devicePixelRatio()
        screen = self.screen()
        screen_dpr = screen.devicePixelRatio() if screen else widget_dpr
        coord_scale = 1.0

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
                _ox, _oy = self._virt.x(), self._virt.y()
                _ax = max(_ox, min(sx - pw + PILL_ANCHOR_DX,
                                   _ox + self._virt.width() - pw))
                _ay = max(_oy, min(sy - ph + PILL_ANCHOR_DY,
                                   _oy + self._virt.height() - ph))
                rx = (_ax - _ox) * coord_scale
                ry = (_ay - _oy) * coord_scale
                _logger.debug(
                    "[OVERLAY-PAINT] pill '%s': elem=(%d,%d) anchor=(%d,%d) "
                    "local=(%.1f,%.1f) sz=(%.0fx%.0f)",
                    text, sx, sy, _ax, _ay, rx, ry,
                    pw * coord_scale, ph * coord_scale,
                )

        font = QFont("Segoe UI", 11, QFont.Bold)
        painter.setFont(font)

        ox = self._virt.x()
        oy = self._virt.y()

        for sx, sy, pw, ph, text in self._labels:
            # Anchor: pill bottom-right at element top-left minus a small margin.
            # Clamp so pills near screen edges stay fully on-screen.
            ax = max(ox, min(sx - pw + PILL_ANCHOR_DX,
                             ox + self._virt.width() - pw))
            ay = max(oy, min(sy - ph + PILL_ANCHOR_DY,
                             oy + self._virt.height() - ph))
            lx = (ax - ox) * coord_scale
            ly = (ay - oy) * coord_scale
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

        if self._caption:
            cap_font = QFont("Segoe UI", 9)
            painter.setFont(cap_font)
            cap_rect = QRectF(8, 8, 260, 18)
            painter.setPen(_TEXT_CLR)
            painter.drawText(cap_rect, Qt.AlignLeft | Qt.AlignVCenter, self._caption)

        painter.end()
