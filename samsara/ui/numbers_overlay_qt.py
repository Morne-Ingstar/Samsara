"""Qt click-through overlay for Show Numbers — numbered pill labels.

Frameless, transparent, always-on-top, WindowTransparentForInput so physical
mouse clicks pass straight through to the app below. Does not steal focus.
"""

import ctypes
import ctypes.wintypes as _wt
import logging
import sys

from PySide6.QtCore import Qt, QPoint, QRect, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QFont, QPen
from PySide6.QtWidgets import QApplication, QWidget

from samsara.log import get_logger
from samsara.ui import theme

logger = get_logger(__name__)

_logger = logging.getLogger(__name__)

# The overlay floats over other applications, so its pills carry their own
# near-opaque surface -- but it is the app's surface, from the app's tokens,
# so a light theme does not leave a black pill on a white desktop.
# Built per paint, never cached: a module-level QColor would freeze the
# palette that was live at import (queue 129).
def _pill_bg() -> QColor:
    colour = theme.qcolor(theme.BG0)
    colour.setAlpha(230)
    return colour


def _pill_border() -> QColor:
    colour = theme.qcolor(theme.mix(theme.BG0, theme.TEXT_PRIMARY, 0.30))
    colour.setAlpha(200)
    return colour


def _text_colour() -> QColor:
    return theme.qcolor(theme.TEXT_PRIMARY)


# Queue 71: mouse-grid cell outlines. The accent is the app's one accent;
# the alpha keeps the app underneath readable while the grid is up.
def _grid_cell_pen() -> QPen:
    return QPen(theme.qcolor(theme.tint(theme.ACCENT, 0.47)), 1)


def _grid_edge_pen() -> QPen:
    return QPen(theme.qcolor(theme.tint(theme.ACCENT, 0.82)), 2)

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
        setter = ctypes.windll.user32.SetThreadDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        setter(ctypes.c_void_p(-4))
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
    matched = None
    for mapping in mappings:
        (pl, pt, pr, pb), _qt_rect, _ratio = mapping
        if pl <= px < pr and pt <= py < pb:
            matched = mapping
            break

    if matched is None:
        # UIA rectangles can include a browser/window border a few physical
        # pixels outside the monitor.  A strict point-containment check then
        # leaves that corner unscaled even though the rest of the rectangle
        # is on a high-DPI screen (for example y=3 against a screen at y=5).
        # Accept only a small edge overflow; genuinely off-desktop points
        # still retain the safe identity fallback.
        nearest = None
        nearest_distance = None
        for mapping in mappings:
            (pl, pt, pr, pb), _qt_rect, _ratio = mapping
            dx = max(pl - px, 0, px - pr)
            dy = max(pt - py, 0, py - pb)
            distance = max(dx, dy)
            if nearest_distance is None or distance < nearest_distance:
                nearest = mapping
                nearest_distance = distance
        if nearest_distance is not None and nearest_distance <= 16:
            matched = nearest

    if matched is None:
        return px, py

    (pl, pt, _pr, _pb), (ql, qt, _qr, _qb), ratio = matched
    scale = float(ratio) if ratio else 1.0
    return (
        round(ql + (px - pl) / scale),
        round(qt + (py - pt) / scale),
    )


_last_mapping_warning = None


def current_monitor_mappings() -> list:
    """[(physical_rect, qt_rect, dpr, QScreen)] for every monitor, paired by
    sorted origin. Qt thread only (reads QApplication.screens()).

    Returns [] when the Win32 and Qt monitor lists cannot be paired -- the
    caller then maps identity. That fallback used to be silent (queue 56:
    labels far from their elements with nothing in the log); it now logs a
    WARNING once per distinct layout."""
    global _last_mapping_warning
    physical = _win32_monitor_rects()
    qt_screens = sorted(
        QApplication.screens(),
        key=lambda s: (s.geometry().x(), s.geometry().y()),
    )
    if len(physical) != len(qt_screens):
        key = (len(physical), len(qt_screens))
        if key != _last_mapping_warning:
            _last_mapping_warning = key
            logger.warning(
                "[SHOW_NUMBERS] %d Win32 monitors but %d Qt screens -- cannot map "
                "UIA physical pixels to Qt coordinates; labels may be misplaced",
                len(physical), len(qt_screens))
        return []
    mappings = []
    for physical_rect, screen in zip(physical, qt_screens):
        geo = screen.geometry()
        qt_rect = (
            geo.x(), geo.y(),
            geo.x() + geo.width(), geo.y() + geo.height(),
        )
        mappings.append((physical_rect, qt_rect, screen.devicePixelRatio(), screen))
    return mappings


def pill_rect(sx: int, sy: int, pw: int, ph: int, bounds: tuple) -> tuple:
    """Final (x, y, w, h) of one pill, in logical coordinates.

    The pill's bottom-right sits just outside the element's top-left corner
    (PILL_ANCHOR_DX/DY), then is clamped to lie fully inside `bounds`
    (left, top, right, bottom) -- the target window intersected with its
    screen -- so a label never lands on a different window or off-screen.
    Pure; shared by paintEvent and by the plan/tests."""
    left, top, right, bottom = bounds
    x = sx - pw + PILL_ANCHOR_DX
    y = sy - ph + PILL_ANCHOR_DY
    x = max(left, min(x, right - pw))
    y = max(top, min(y, bottom - ph))
    return x, y, pw, ph


def phys_to_logical(px: int, py: int) -> tuple:
    """Convert UI Automation physical screen coordinates to Qt logical DIPs.

    Microsoft specifies that UIA bounding rectangles use physical pixels.
    Qt 6 widget/screen geometry uses device-independent pixels. Always map
    through the containing monitor; never infer UIA's coordinate system from
    a separately virtualized Win32 query. Qt thread only.
    """
    try:
        mappings = [(p, q, r) for p, q, r, _s in current_monitor_mappings()]
        if not mappings:
            return px, py

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
        # Queue 71: mouse-grid cell outlines, logical (l, t, r, b). Empty for
        # the numbers overlay, which draws pills only.
        self._cells: list = []
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
        # Pills are clamped inside these logical bounds (queue 56): the target
        # window intersected with this screen. Default: the whole screen.
        self._bounds = (geo.x(), geo.y(), geo.x() + geo.width(), geo.y() + geo.height())
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

    def update_labels(self, labels: list, caption: str = "", bounds: "tuple | None" = None,
                      cells: "list | None" = None) -> None:
        self._labels = labels
        self._caption = caption
        if bounds is not None:
            self.set_bounds(bounds)
        self.set_cells(cells)
        self.update()

    def set_cells(self, cells: "list | None") -> None:
        """Queue 71: cell rectangles to outline, in LOGICAL coordinates
        (left, top, right, bottom). The mouse grid draws these so the user can
        see which region each number owns; None (the numbers overlay) draws
        pills only, exactly as before."""
        self._cells = list(cells) if cells else []

    def set_bounds(self, bounds: tuple) -> None:
        """Clamp pills to (left, top, right, bottom), intersected with this
        window's screen."""
        v = self._virt
        left = max(v.x(), bounds[0])
        top = max(v.y(), bounds[1])
        right = min(v.x() + v.width(), bounds[2])
        bottom = min(v.y() + v.height(), bounds[3])
        if right - left < 40 or bottom - top < 30:
            left, top, right, bottom = v.x(), v.y(), v.x() + v.width(), v.y() + v.height()
        self._bounds = (left, top, right, bottom)

    def _paint_cells(self, painter) -> None:
        """Outline the mouse grid's cells (queue 71).

        A hairline per cell plus a slightly stronger outer border: enough to
        see which region a number owns, faint enough to read the app through.
        Coordinates are logical, like the labels, and are offset by the same
        screen origin.
        """
        if not self._cells:
            return
        ox, oy = self._virt.x(), self._virt.y()
        painter.save()
        painter.setBrush(Qt.NoBrush)
        for left, top, right, bottom in self._cells:
            rect = QRectF(left - ox, top - oy, right - left, bottom - top)
            painter.setPen(_grid_cell_pen())
            painter.drawRect(rect)
        outer = QRectF(
            min(c[0] for c in self._cells) - ox, min(c[1] for c in self._cells) - oy,
            max(c[2] for c in self._cells) - min(c[0] for c in self._cells),
            max(c[3] for c in self._cells) - min(c[1] for c in self._cells),
        )
        painter.setPen(_grid_edge_pen())
        painter.drawRect(outer)
        painter.restore()

    def paintEvent(self, event) -> None:
        if not self._labels:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._paint_cells(painter)

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

        font = theme.qfont(theme.TYPE_BODY, weight=QFont.Bold)
        painter.setFont(font)

        ox = self._virt.x()
        oy = self._virt.y()

        for sx, sy, pw, ph, text in self._labels:
            # Anchor: pill bottom-right at element top-left minus a small margin,
            # clamped inside the target window on this screen (pill_rect).
            ax, ay, _pw, _ph = pill_rect(sx, sy, pw, ph, self._bounds)
            lx = (ax - ox) * coord_scale
            ly = (ay - oy) * coord_scale
            lw = pw * coord_scale
            lh = ph * coord_scale
            rect = QRectF(lx, ly, lw, lh)

            path = QPainterPath()
            path.addRoundedRect(rect, 4.0, 4.0)
            painter.fillPath(path, _pill_bg())

            painter.setPen(_pill_border())
            painter.drawPath(path)

            painter.setPen(_text_colour())
            painter.drawText(rect, Qt.AlignCenter, text)

        if self._caption:
            cap_font = theme.qfont(theme.TYPE_MIN)
            painter.setFont(cap_font)
            cap_rect = QRectF(8, 8, 260, 18)
            painter.setPen(_text_colour())
            painter.drawText(cap_rect, Qt.AlignLeft | Qt.AlignVCenter, self._caption)

        painter.end()
