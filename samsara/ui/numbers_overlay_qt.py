"""Qt click-through overlay for Show Numbers — numbered pill labels.

Frameless, transparent, always-on-top, WindowTransparentForInput so physical
mouse clicks pass straight through to the app below. Does not steal focus.
"""

import ctypes
import ctypes.wintypes as _wt
import logging
from pathlib import Path
import re
import sys

from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QFont, QPen
from PySide6.QtWidgets import QApplication, QWidget

from samsara.log import get_logger
from samsara.ui import theme
from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer

logger = get_logger(__name__)

_logger = logging.getLogger(__name__)

# Queue 811: one canonical CSS definition, embedded verbatim by content.js so
# an unpacked extension needs no new web-accessible resources or permissions.
_BADGE_CSS = (Path(__file__).resolve().parents[2] /
              'browser_extension/numbers_badge.css').read_text(encoding='utf-8').strip()
BADGE_STYLE = dict(re.findall(r'([\w-]+):([^;{}]+)', _BADGE_CSS))


def _pill_bg() -> QColor:
    return QColor(BADGE_STYLE['background'])


def _pill_border() -> QColor:
    rgba = [float(v) for v in re.findall(r'[\d.]+', BADGE_STYLE['border'].split('rgba(')[1])]
    return QColor(*map(int, rgba[:3]), round(255 * rgba[3]))


def _text_colour() -> QColor:
    return QColor(BADGE_STYLE['color'])


class TargetLabel(list):
    """Five-field compatibility sequence with explicit target and placement metadata.

    Unlike legacy grid/diagnostic labels, x/y are FINAL badge coordinates.
    Keeping this distinction explicit prevents a second anchor/DPI transform.
    """
    def __init__(self, x, y, w, h, text, target, compact=False):
        super().__init__([x, y, w, h, text])
        self.target, self.compact, self.collision = target, compact, False


def _overlap(a, b):
    return a[0] < b[0]+b[2] and b[0] < a[0]+a[2] and a[1] < b[1]+b[3] and b[1] < a[1]+a[3]


def layout_target_labels(elements):
    """Nudge only within each target; compact before accepting a collision.

    Very narrow targets centre their badge rather than moving it onto a
    neighbour. An impossible arrangement retains ALL numbers and marks both
    badges for target outlines; it never drops or renumbers a click target.
    """
    placed = []
    for number, element in enumerate(elements, 1):
        l, t, r, b = element['rect']
        text, chosen = str(number), None
        for compact in (False, True):
            minimum = int(BADGE_STYLE['min-width'][:-2])
            padding = int(BADGE_STYLE['padding'].split()[-1][:-2])
            line_height = int(re.search(r'/(\d+)px', BADGE_STYLE['font'])[1])
            w, h = (max(14, len(text)*6+2), 14) if compact else (minimum+2*padding+2, line_height+2)
            x0 = l if r-l >= w else (l+r-w)/2
            y0 = t if b-t >= h else (t+b-h)/2
            xs = {x0, max(x0, r-w)} if r-l >= w else {x0}
            ys = {y0, max(y0, b-h)} if b-t >= h else {y0}
            for previous in placed:
                if r-l >= w:
                    xs.update(x for x in (previous[0]+previous[2], previous[0]-w) if l <= x <= r-w)
                if b-t >= h:
                    ys.update(y for y in (previous[1]+previous[3], previous[1]-h) if t <= y <= b-h)
            positions = sorted(((x, y) for x in xs for y in ys), key=lambda p: (p[1]-y0, p[0]-x0))
            for x, y in positions:
                candidate = TargetLabel(x, y, w, h, text, (l, t, r, b), compact)
                if not any(_overlap(candidate, previous) for previous in placed):
                    chosen = candidate
                    break
            if chosen is not None:
                break
        if chosen is None:
            chosen = TargetLabel(x0, y0, w, h, text, (l, t, r, b), True)
        for previous in placed:
            if _overlap(chosen, previous):
                chosen.collision = previous.collision = True
        placed.append(chosen)
    return placed


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


def map_physical_rect_to_qt(rect, mappings):
    if not mappings:
        return tuple(rect)
    cx, cy = (rect[0]+rect[2])/2, (rect[1]+rect[3])/2
    mapping = next((m for m in mappings if m[0][0] <= cx < m[0][2] and m[0][1] <= cy < m[0][3]), None)
    if mapping is None:
        mapping = max(mappings, key=lambda m: max(0, min(rect[2], m[0][2])-max(rect[0], m[0][0])) *
                      max(0, min(rect[3], m[0][3])-max(rect[1], m[0][1])))
    (pl, pt, _pr, _pb), (ql, qt, _qr, _qb), ratio = mapping
    return (round(ql+(rect[0]-pl)/ratio), round(qt+(rect[1]-pt)/ratio),
            round(ql+(rect[2]-pl)/ratio), round(qt+(rect[3]-pt)/ratio))


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

class MatchNumbersOverlay:
    """Match-only labels using the ordinary renderer, one window per screen.

    Construct/show/hide on qt_runtime's thread. The owner supplies freshness
    and cancellation callbacks so a hidden or expired selection never acts.
    """

    def __init__(self, nodes, is_live, cancel):
        self._windows = []
        self._hidden = False
        self._is_live, self._cancel = is_live, cancel
        groups = {}
        for number, node in enumerate(nodes, 1):
            x, y = phys_to_logical(node.rect[0], node.rect[1])
            screen = QApplication.screenAt(QPoint(x, y))
            if screen is None:
                continue
            labels = groups.setdefault(screen.name(), (screen, []))[1]
            text = str(number)
            labels.append([x, y, max(28, 12 + len(text) * 10), 22, text])
        for screen, labels in groups.values():
            self._windows.append(NumbersOverlayWindow(labels, screen))
        self._timer = QTimer()
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)

    def _poll(self):
        if not self._is_live():
            self.hide()
            self._cancel()

    def show(self):
        if not self._is_live():
            self._cancel()
            return
        for window in self._windows:
            window.show()
            window.raise_()
        self._timer.start()

    def hide(self):
        if self._hidden:
            return
        self._hidden = True
        self._timer.stop()
        for window in self._windows:
            window.hide()
            window.deleteLater()
        self._windows.clear()
        self._timer.deleteLater()


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
        self._highlighted_number = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(75)
        self._hover_timer.timeout.connect(self._poll_hover)
        # Set only when this overlay is showing as a visible fallback from a
        # failed DOM (browser-extension) Show Numbers attempt -- see
        # plugins/commands/show_numbers.py's _try_show_dom_numbers. Empty
        # string means "no caption" (the normal, non-fallback case).
        self._caption = ""

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.ToolTip
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
            | Qt.NoDropShadowWindowHint
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
        self.setAccessibleName("Numbered targets")
        self._announcer = LiveSurfaceAnnouncer(self)
        self._update_accessibility()

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
        self._highlighted_number = None
        self._hover_timer.stop()
        if self.isVisible() and any(getattr(label, "collision", False) for label in labels):
            self._hover_timer.start()
        self._caption = caption
        if bounds is not None:
            self.set_bounds(bounds)
        self.set_cells(cells)
        self.update()

    def _update_accessibility(self) -> None:
        # The input carries target positions, not UIA element names. Expose
        # that same mapping as readable text without adding hidden controls.
        mappings = [f"{text}: target at {x}, {y}" for x, y, _w, _h, text in self._labels]
        mappings.extend(
            f"{self._labels[i][4] if i < len(self._labels) else i + 1}: cell {left}, {top}, {right}, {bottom}"
            for i, (left, top, right, bottom) in enumerate(self._cells)
        )
        self.setAccessibleDescription("; ".join(part for part in (self._caption, *mappings) if part))
        if self.isVisible():
            self._announce_mapping()

    def _announce_mapping(self) -> None:
        # Keep large grids readable on demand without speaking every coordinate.
        summary = f"{self.accessibleName()}: {len(self._labels)}"
        if self._caption:
            summary = f"{summary}; {self._caption}"
        self._announcer.announce_transition(self.accessibleDescription(), summary)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._announce_mapping()
        if any(getattr(label, "collision", False) for label in self._labels):
            self._hover_timer.start()

    def hideEvent(self, event) -> None:
        self._announcer.reset()
        self._hover_timer.stop()
        super().hideEvent(event)

    def set_cells(self, cells: "list | None") -> None:
        """Queue 71: cell rectangles to outline, in LOGICAL coordinates
        (left, top, right, bottom). The mouse grid draws these so the user can
        see which region each number owns; None (the numbers overlay) draws
        pills only, exactly as before."""
        self._cells = list(cells) if cells else []
        self._update_accessibility()

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

    def highlight_number(self, number):
        self._highlighted_number = str(number) if number is not None else None
        self.update()

    def _poll_hover(self):
        # Transparent-for-input windows receive no Qt mouse events. Poll only
        # while an unresolved collision is visible; never steal input/focus.
        point = _wt.POINT()
        if sys.platform != 'win32' or not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return
        x, y = phys_to_logical(point.x, point.y)
        number = next((label[4] for label in reversed(self._labels)
                       if getattr(label, 'collision', False) and
                       label[0] <= x < label[0]+label[2] and label[1] <= y < label[1]+label[3]), None)
        if number != self._highlighted_number:
            self.highlight_number(number)

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

        font = QFont(QApplication.font().family())
        font.setPixelSize(int(re.search(r'(\d+)px/', BADGE_STYLE['font'])[1]))
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)

        ox = self._virt.x()
        oy = self._virt.y()

        for label in self._labels:
            sx, sy, pw, ph, text = label
            # Anchor: pill bottom-right at element top-left minus a small margin,
            # clamped inside the target window on this screen (pill_rect).
            if isinstance(label, TargetLabel):
                ax, ay = sx, sy
                font.setPixelSize(9 if label.compact else int(re.search(r'(\d+)px/', BADGE_STYLE['font'])[1]))
                painter.setFont(font)
            else:
                ax, ay, _pw, _ph = pill_rect(sx, sy, pw, ph, self._bounds)
            lx = (ax - ox) * coord_scale
            ly = (ay - oy) * coord_scale
            lw = pw * coord_scale
            lh = ph * coord_scale
            rect = QRectF(lx, ly, lw, lh)

            path = QPainterPath()
            radius = int(BADGE_STYLE['border-radius'][:-2])
            path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            painter.fillPath(path, _pill_bg())

            painter.setPen(_pill_border())
            painter.drawPath(path)

            painter.setPen(_text_colour())
            painter.drawText(rect, Qt.AlignCenter, text)

        # Draw last: on tiny controls the badge covers the entire target, so
        # painting its outline under the badge would hide the only cue.
        for label in self._labels:
            if isinstance(label, TargetLabel) and label.collision and label[4] == self._highlighted_number:
                l, t, r, b = label.target
                painter.setBrush(Qt.NoBrush)
                pen = QPen(_text_colour())
                pen.setWidthF(1 / widget_dpr)  # one physical pixel at any DPI
                painter.setPen(pen)
                painter.drawRect(QRectF(l-ox, t-oy, r-l, b-t))

        if self._caption:
            cap_font = theme.qfont(theme.TYPE_MIN)
            painter.setFont(cap_font)
            cap_rect = QRectF(8, 8, 260, 18)
            painter.setPen(_text_colour())
            painter.drawText(cap_rect, Qt.AlignLeft | Qt.AlignVCenter, self._caption)

        painter.end()
