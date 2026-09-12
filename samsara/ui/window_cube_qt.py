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
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from samsara.ui import qt_runtime
from samsara.ui import theme

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


def apply_toolwindow_exstyle(win_id: int) -> bool:
    """Apply compute_exstyle() to a live native window handle."""
    try:
        user32 = ctypes.windll.user32
        current = user32.GetWindowLongW(int(win_id), GWL_EXSTYLE)
        user32.SetWindowLongW(int(win_id), GWL_EXSTYLE, compute_exstyle(current))
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

class _Row(QFrame):
    """A single clickable row."""

    def __init__(self, row: CubeRow, on_click):
        super().__init__()
        self._number = row.number
        self._on_click = on_click
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
            f"QFrame{{background:transparent;border-radius:6px;}}"
            f"QFrame:hover{{background:{theme.BG2};}}"
        )

    def mouseReleaseEvent(self, event):
        # Click-to-switch: the same action the spoken number performs.
        try:
            if self._on_click is not None:
                self._on_click(self._number)
        except Exception as exc:
            print(f"[CUBE] row click failed: {exc}")
        super().mouseReleaseEvent(event)


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
        self._on_click = on_click
        self._requested_position = position
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
        outer.addWidget(self._card)

        self._rows_layout = QVBoxLayout(self._card)
        self._rows_layout.setContentsMargins(6, 8, 6, 8)
        self._rows_layout.setSpacing(2)

        self._render_sig.connect(self._on_render)

    @Slot(list)
    def _on_render(self, rows: list):
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        if not rows:
            empty = QLabel("No windows")
            empty.setStyleSheet(
                f"color:{theme.TEXT_DISABLED};padding:10px;background:transparent;"
            )
            self._rows_layout.addWidget(empty)
        else:
            for row in rows:
                self._rows_layout.addWidget(_Row(row, self._on_click))
        self.adjustSize()
        self._place()

    def _place(self):
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QApplication

        if self._requested_position is not None:
            self.move(int(self._requested_position[0]), int(self._requested_position[1]))
            return
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x, y = default_position(
            (geo.left(), geo.top(), geo.right(), geo.bottom()),
            (self.width(), self.height()),
        )
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        apply_toolwindow_exstyle(int(self.winId()))

    def closeEvent(self, event):
        # Hide, never destroy -- keeps the reference valid so re-showing the
        # cube never has to rebuild it (same policy as task_overlay.py).
        event.ignore()
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
        if position is not None:
            self._position = position

    def show(self, rows: list):
        self._pending_rows = list(rows)
        if self._window is not None:
            self._window._render_sig.emit(list(rows))
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
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

    def current_position(self):
        """Where the panel actually sits, for persisting across sessions."""
        if self._window is None:
            return None
        try:
            return (self._window.x(), self._window.y())
        except Exception:
            return None

    def _init_window(self):
        """Runs on the Qt thread via qt_runtime.post()."""
        self._window = _CubeWindow(self._on_click, self._opacity, self._position)
        self._window._render_sig.emit(list(self._pending_rows))
        self._window.show()


_panel: "CubePanel | None" = None


def get_panel() -> CubePanel:
    """Process-wide singleton -- one cube, reused for the app's lifetime."""
    global _panel
    if _panel is None:
        _panel = CubePanel()
    return _panel
