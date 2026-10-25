"""Window Cube -- a pinned, numbered, always-on-top list of open windows.

The user keeps their apps full-screen and says a single number to switch.
Unlike the letter overlay in plugins/commands/window_switcher.py (whose
labels sit ON each window, and so vanish when an app is full-screen, and
which auto-dismisses after 30 s), this is one small panel that stays put
until it is explicitly hidden.

Public API (all thread-safe -- every Qt call is marshalled onto the shared
runtime via qt_runtime.post()):

    panel = get_panel()
    panel.show(rows)      # rows == list of CubeRow
    panel.refresh(rows)
    panel.hide()
    panel.focus_review() # explicit keyboard review of an already shown cube

Window-manager flags (the reason this module exists rather than reusing the
letter overlay's widget):

  * WS_EX_TOOLWINDOW  -- keeps the panel out of the Alt-Tab list and, more
    importantly, out of its OWN enumeration: window_switcher._get_all_windows
    would otherwise happily label the cube as a switchable window.
  * WS_EX_NOACTIVATE  -- showing or clicking the panel never steals focus
    from whatever the user is actually working in. Without it, "switch to 3"
    would focus the cube instead of window 3.

Qt discipline: no QApplication is created here and no top-level exec() is
run -- the window is built and driven entirely through qt_runtime.post(),
and closeEvent hides rather than destroys so the Python reference stays
valid and re-showing is always reliable (same contract as task_overlay.py).
"""

from __future__ import annotations

import ctypes
import math
import weakref
import logging
from time import perf_counter
from dataclasses import dataclass

_latency_log = logging.getLogger(__name__)

from PySide6.QtCore import QEvent, QRect, Qt, Signal, Slot
from PySide6.QtGui import QCursor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication, QAbstractButton, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
    QScrollArea, QStyle, QStyleOption,
)

from samsara.ui import qt_runtime
from samsara.ui import theme
from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer

# ---------------------------------------------------------------------------
# Win32 extended-style bits
# ---------------------------------------------------------------------------

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000

#: Minimum row height (px). The rows are click targets, so they must stay
#: comfortably tappable -- the spec floor is 40.
ROW_HEIGHT = 44

PANEL_WIDTH = 260
EDGE_MARGIN = 24


def compute_exstyle(current: int) -> int:
    """Return the extended style the cube must run with.

    Pure so it can be asserted without creating a window: adds TOOLWINDOW
    (keeps the panel out of its own window list) and NOACTIVATE (showing it
    never steals focus), and clears APPWINDOW, which would otherwise force
    the panel back into the taskbar/Alt-Tab list and undo TOOLWINDOW.
    """
    return (current | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW


def apply_toolwindow_exstyle(win_id: int, *, review: bool = False) -> bool:
    """Apply compute_exstyle() to a live native window handle."""
    try:
        user32 = ctypes.windll.user32
        current = user32.GetWindowLongW(int(win_id), GWL_EXSTYLE)
        style = compute_exstyle(current)
        if review:
            style &= ~WS_EX_NOACTIVATE
        user32.SetWindowLongW(int(win_id), GWL_EXSTYLE, style)
        return True
    except Exception as exc:      # pragma: no cover - non-Windows/hostless
        print(f"[CUBE] ex-style apply failed: {exc}")
        return False


def default_position(monitor_rect, panel_size) -> tuple:
    """Bottom-right of the ACTIVE monitor, inset by EDGE_MARGIN.

    monitor_rect is (left, top, right, bottom); panel_size is (w, h). Pure,
    so the placement rule is testable without a screen.
    """
    left, top, right, bottom = monitor_rect
    width, height = panel_size
    x = right - width - EDGE_MARGIN
    y = bottom - height - EDGE_MARGIN
    return (max(left, x), max(top, y))


def _clamp_rect(rect: QRect, available: QRect) -> QRect:
    """All coordinates are Qt logical pixels; available excludes the taskbar."""
    width = min(rect.width(), available.width())
    height = min(rect.height(), available.height())
    return QRect(
        max(available.x(), min(rect.x(), available.x() + available.width() - width)),
        max(available.y(), min(rect.y(), available.y() + available.height() - height)),
        width, height,
    )


def _finite_pair(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) for v in value):
        return None
    return value


def resolve_position(position, screens, preferred, panel_size, occupied=()):
    """Resolve a saved anchor or valid legacy point, else use the cursor screen.

    screens contains (name, available QRect) pairs. Return (name, panel QRect),
    using exclusive right/bottom arithmetic to avoid QRect's inclusive edges.
    """
    screens = [(name, rect) for name, rect in screens if not rect.isEmpty()]
    if not screens:
        return None
    width, height = panel_size
    legacy = _finite_pair(position)
    if legacy is not None:
        rect = QRect(round(legacy[0]), round(legacy[1]), width, height)
        for name, available in screens:
            if available.contains(rect):
                return name, rect
    elif isinstance(position, dict):
        anchor = position.get("anchor")
        offsets = _finite_pair(position.get("offset"))
        if anchor in ("top-left", "top-right", "bottom-left", "bottom-right") and offsets:
            for name, available in screens:
                if name != position.get("screen"):
                    continue
                dx, dy = (round(offsets[0] * available.width()),
                          round(offsets[1] * available.height()))
                x = available.x() + dx if anchor.endswith("left") else (
                    available.x() + available.width() - width - dx)
                y = available.y() + dy if anchor.startswith("top") else (
                    available.y() + available.height() - height - dy)
                rect = QRect(x, y, width, height)
                if available.contains(rect):
                    return name, rect
    name, available = next(((n, r) for n, r in screens if n == preferred), screens[0])
    x, y = default_position(
        (available.x(), available.y(), available.x() + available.width(),
         available.y() + available.height()), (width, height),
    )
    rect = _clamp_rect(QRect(x, y, width, height), available)
    if any(rect.intersects(other) for other in occupied):
        rect.moveLeft(available.x() + EDGE_MARGIN)
    return name, _clamp_rect(rect, available)


def remember_position(screen_name, available: QRect, rect: QRect):
    """Represent the nearest corner's inward offsets as screen fractions."""
    rect = _clamp_rect(rect, available)
    right = rect.center().x() > available.center().x()
    bottom = rect.center().y() > available.center().y()
    dx = (available.x() + available.width() - rect.x() - rect.width()
          if right else rect.x() - available.x())
    dy = (available.y() + available.height() - rect.y() - rect.height()
          if bottom else rect.y() - available.y())
    return {"screen": screen_name,
            "anchor": ("bottom" if bottom else "top") + ("-right" if right else "-left"),
            "offset": [dx / available.width(), dy / available.height()]}


def _listening_rects():
    # Inspect only widgets already owned by this Qt application. No imports
    # that instantiate displays and no live config or native desktop reads.
    return [widget.frameGeometry() for widget in QApplication.topLevelWidgets()
            if widget.isVisible() and type(widget).__name__ in
            ("ListeningIndicator", "LiveSurfaceWidget")]


@dataclass(frozen=True)
class CubeRow:
    """One numbered row. `detail` is the disambiguating second line -- set
    only when two rows share an app name (see window_cube._build_rows), so
    the common case stays a single clean "3  Claude" line."""

    number: int
    app: str
    detail: str = ""
    closed: bool = False


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class _Row(QAbstractButton):
    """A single clickable row."""

    def __init__(self, row: CubeRow, on_click):
        super().__init__()
        self._number = row.number
        self._on_click = on_click
        self.setAccessibleName(row.app)
        self.setAccessibleDescription(
            "; ".join(filter(None, (str(row.number), row.detail,
                                     "Closed window" if row.closed else "")))
        )
        self.setEnabled(not row.closed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.clicked.connect(self._activate)
        self.setMinimumHeight(ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        text_colour = theme.TEXT_DISABLED if row.closed else theme.TEXT_PRIMARY
        num_colour = theme.TEXT_DISABLED if row.closed else theme.ACCENT

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 6, 14, 6)
        lay.setSpacing(12)

        num = QLabel(str(row.number))
        num.setFixedWidth(22)
        num.setStyleSheet(
            f"color:{num_colour};font-size:{theme.FONT_SIZE_HEADING}px;"
            f"font-weight:600;background:transparent;"
        )
        lay.addWidget(num)

        stack = QVBoxLayout()
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)
        name = QLabel(row.app)
        name.setStyleSheet(
            f"color:{text_colour};font-size:{theme.FONT_SIZE_BODY}px;"
            f"background:transparent;"
        )
        stack.addWidget(name)
        if row.detail:
            detail = QLabel(row.detail)
            detail.setStyleSheet(
                f"color:{theme.TEXT_SECONDARY};"
                f"font-size:{theme.FONT_SIZE_CAPTION}px;background:transparent;"
            )
            stack.addWidget(detail)
        lay.addLayout(stack, stretch=1)

        self.setStyleSheet(
            f"QAbstractButton{{background:transparent;border:none;border-radius:6px;}}"
            f"QAbstractButton:hover{{background:{theme.BG2};}}"
        )

    def paintEvent(self, event):
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def _activate(self):
        # Click-to-switch: the same action the spoken number performs.
        try:
            if self._on_click is not None:
                self._on_click(self._number)
        except Exception as exc:
            print(f"[CUBE] row click failed: {exc}")


class _CubeWindow(QWidget):
    """The panel itself. Lives on the qt_runtime thread."""

    _render_sig = Signal(list)

    def __init__(self, on_click, opacity: float, position):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        # Title prefix is a second, independent guarantee that the cube never
        # lands in its own list: window_switcher._get_all_windows drops every
        # window whose title starts with "Samsara".
        self.setWindowTitle("Samsara Window Cube")
        self.setAccessibleName(self.windowTitle())
        self.setAccessibleDescription(
            "Press Control+Alt+F6 from a Samsara window for keyboard review; "
            "press Escape to return."
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._reviewing = False
        self._return_focus = None
        self._passive_return_focus = None
        self._announcer = LiveSurfaceAnnouncer(self)
        self._announced_rows = ()
        self._announcement = "No windows"
        self._review_shortcut = QShortcut(QKeySequence("Ctrl+Alt+F6"), self)
        self._review_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._review_shortcut.activated.connect(self.focus_review)
        self._on_click = on_click
        self._requested_position = position
        self._position_snapshot = None
        self._placement_screen = None
        self._placing = False
        self.setWindowOpacity(opacity)
        self.setFixedWidth(PANEL_WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._card = QWidget()
        self._card.setObjectName("cubeCard")
        # Scoped by object name: an unscoped "background/border" rule here
        # cascades to every child widget, drawing a border box around each
        # row and label.
        self._card.setStyleSheet(
            f"QWidget#cubeCard{{background:{theme.BG1};"
            f"border:1px solid {theme.BORDER};border-radius:10px;}}"
        )
        # Short logical desktops (including 200% scaling) must retain access
        # to all rows without growing underneath the taskbar.
        self._scroll = QScrollArea()
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self._scroll.setWidget(self._card)
        outer.addWidget(self._scroll)

        self._rows_layout = QVBoxLayout(self._card)
        self._rows_layout.setContentsMargins(6, 8, 6, 8)
        self._rows_layout.setSpacing(2)

        self._render_sig.connect(self._on_render)

    @Slot(list)
    def _on_render(self, rows: list):
        self._announced_rows = tuple(rows)
        self._announcement = "; ".join(
            " ".join(filter(None, (str(row.number), row.app, row.detail,
                                    "Closed window" if row.closed else "")))
            for row in rows
        ) or "No windows"
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if not rows:
            empty = QLabel("No windows")
            empty.setStyleSheet(
                f"color:{theme.TEXT_DISABLED};padding:10px;background:transparent;"
            )
            self._rows_layout.addWidget(empty)
        else:
            for row in rows:
                button = _Row(row, self._activate_number)
                button.setFocusPolicy(Qt.FocusPolicy.StrongFocus if self._reviewing
                                      else Qt.FocusPolicy.NoFocus)
                self._rows_layout.addWidget(button)
        self._rows_layout.activate()
        self._card.adjustSize()
        self._place()
        if self.isVisible():
            self._announce_rows()

    def _announce_rows(self):
        self._announcer.announce_transition(self._announced_rows, self._announcement)

    def focus_review(self):
        """Deliberate app-local shortcut/API entry; passive showing stays quiet."""
        if not self.isVisible():
            return
        if not self._reviewing:
            focused = QApplication.focusWidget()
            self._return_focus = weakref.ref(focused) if focused is not None else None
            self._reviewing = True
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            for row in self.findChildren(_Row):
                row.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
            self.show()
        apply_toolwindow_exstyle(int(self.winId()), review=True)
        self.raise_()
        self.activateWindow()
        QApplication.setActiveWindow(self)
        for row in self.findChildren(_Row):
            if row.isEnabled():
                row.setFocus(Qt.FocusReason.ShortcutFocusReason)
                break

    def _restore_passive(self, *, restore_focus=True):
        if not self._reviewing:
            return
        self._reviewing = False
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        for row in self.findChildren(_Row):
            row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        if visible:
            self.show()
        apply_toolwindow_exstyle(int(self.winId()))
        previous = self._return_focus() if self._return_focus else None
        self._return_focus = None
        if restore_focus and previous is not None:
            try:
                if previous.isVisible():
                    previous.window().activateWindow()
                    previous.setFocus(Qt.FocusReason.ShortcutFocusReason)
            except RuntimeError:
                pass  # the invoking field may have been destroyed during review

    def _activate_number(self, number):
        # Let the existing switch callback focus its target after review ends.
        self._restore_passive(restore_focus=False)
        if self._on_click is not None:
            self._on_click(number)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._reviewing:
            self._restore_passive()
            event.accept()
            return
        super().keyPressEvent(event)

    def hide(self):
        self._restore_passive()
        super().hide()

    def show(self):
        if not self._reviewing:
            focused = QApplication.focusWidget()
            self._passive_return_focus = (
                weakref.ref(focused) if focused is not None and focused.window() is not self
                else None
            )
        super().show()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.Paint and getattr(self, '_latency_request_at', None) is not None:
            _latency_log.debug('[SHOW_LATENCY] cube request_to_paint_ms=%.3f',
                               (perf_counter() - self._latency_request_at) * 1000)
            self._latency_request_at = None
        # Some Qt platforms activate Tool windows despite the passive flags.
        # Keep the invoking Qt field active if such an activation slips through.
        if event.type() == QEvent.Type.WindowActivate and not self._reviewing:
            previous = self._passive_return_focus() if self._passive_return_focus else None
            if previous is not None:
                try:
                    if previous.isVisible():
                        QApplication.setActiveWindow(previous.window())
                        previous.setFocus(Qt.FocusReason.OtherFocusReason)
                except RuntimeError:
                    pass
        return result

    def _place(self):
        if self._placing:
            return
        screens = QApplication.screens()
        if not screens:
            return
        cursor_screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        layout = [(screen.name(), screen.availableGeometry()) for screen in screens]
        preferred = (self._placement_screen if any(name == self._placement_screen for name, _ in layout)
                     else cursor_screen.name() if cursor_screen else None)
        result = resolve_position(self._requested_position, layout, preferred,
                                  (PANEL_WIDTH, self._card.sizeHint().height()),
                                  _listening_rects())
        if result is None:
            return
        name, rect = result
        self._placement_screen = name
        self._placing = True
        try:
            self.setFixedSize(rect.size())
            self.move(rect.topLeft())
        finally:
            self._placing = False
        available = next(geo for screen_name, geo in layout if screen_name == name)
        self._position_snapshot = remember_position(name, available, rect)

    def _screen_geometry_changed(self, *_):
        if self.isVisible():
            self._place()

    def showEvent(self, event):
        super().showEvent(event)
        self._place()
        if not getattr(self, "_screens_connected", False):
            app = QApplication.instance()
            app.screenAdded.connect(self._connect_screen)
            app.screenRemoved.connect(self._screen_geometry_changed)
            for screen in QApplication.screens():
                self._connect_screen(screen)
            self._screens_connected = True
        apply_toolwindow_exstyle(int(self.winId()), review=self._reviewing)
        self._announce_rows()

    def _connect_screen(self, screen):
        screen.availableGeometryChanged.connect(self._screen_geometry_changed)
        screen.geometryChanged.connect(self._screen_geometry_changed)
        self._screen_geometry_changed()

    def closeEvent(self, event):
        # Hide, never destroy -- keeps the reference valid so re-showing the
        # cube never has to rebuild it (same policy as task_overlay.py).
        event.ignore()
        self._restore_passive()
        self.hide()


class CubePanel:
    """Thread-safe wrapper -- the only thing callers touch."""

    def __init__(self):
        self._window: "_CubeWindow | None" = None
        self._init_posted = False
        self._pending_rows: list = []
        self._on_click = None
        self._opacity = 0.6
        self._position = None

    def configure(self, *, on_click=None, opacity=None, position=None):
        if on_click is not None:
            self._on_click = on_click
        if opacity is not None:
            self._opacity = float(opacity)
        self._position = position

    def show(self, rows: list):
        self._latency_request_at = perf_counter()
        self._pending_rows = list(rows)
        if self._window is not None:
            qt_runtime.post(self._show_rows)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.ensure_started()
            qt_runtime.post(self._init_window)

    def refresh(self, rows: list):
        self._pending_rows = list(rows)
        if self._window is not None:
            self._window._render_sig.emit(list(rows))

    def hide(self):
        if self._window is not None:
            qt_runtime.post(self._window.hide)

    def focus_review(self):
        """Enter review explicitly; never invoked by show() or refresh()."""
        if self._window is not None:
            qt_runtime.post(self._window.focus_review)

    def current_position(self):
        """Where the panel actually sits, for persisting across sessions."""
        if self._window is None:
            return None
        # Written only on the Qt thread. Do not read QWidget geometry from
        # the voice-command thread when hide cube remembers the position.
        snapshot = self._window._position_snapshot
        return dict(snapshot, offset=list(snapshot["offset"])) if snapshot else None

    def _show_rows(self):
        self._window._latency_request_at = self._latency_request_at
        self._window._requested_position = self._position
        self._window._placement_screen = None  # choose cursor monitor on each show
        self._window._on_click = self._on_click
        self._window.setWindowOpacity(self._opacity)
        self._window._on_render(list(self._pending_rows))
        self._window.show()
        self._window.raise_()

    def _init_window(self):
        """Runs on the Qt thread via qt_runtime.post()."""
        started = perf_counter()
        self._window = _CubeWindow(self._on_click, self._opacity, self._position)
        _latency_log.debug('[SHOW_LATENCY] cube construct_ms=%.3f', (perf_counter() - started) * 1000)
        self._show_rows()


_panel: "CubePanel | None" = None


def get_panel() -> CubePanel:
    """Process-wide singleton -- one cube, reused for the app's lifetime."""
    global _panel
    if _panel is None:
        _panel = CubePanel()
    return _panel
