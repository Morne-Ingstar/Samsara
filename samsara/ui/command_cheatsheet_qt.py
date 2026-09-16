"""
PySide6 command cheat sheet for Samsara.

Drop-in replacement for CommandCheatSheet with the same public API:
    show() / hide() / toggle() / destroy() / refresh()

All Qt operations are posted to the shared qt_runtime event loop.
show() / hide() / toggle() are safe to call from any thread.

Every row is a command-catalog record (samsara.command_catalog), built at
runtime from the LIVE registry rows the app passes as commands_cb -- so the
sheet can never list a command that is not registered. Rows are grouped by
plugin and show the canonical phrase, up to 3 other phrases, a
"destructive" tag for destructive commands and a "whole utterance" tag for
reserved control words (they only work said on their own). If no catalog
can be built, the sheet says so and links to Help; it never falls back to a
hand-written list.
"""

import json
from pathlib import Path
from typing import Callable, List

from PySide6.QtCore import Qt, QTimer, Signal, QPoint
from PySide6.QtGui import QColor, QCursor, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QLineEdit,
    QFrame, QSizeGrip, QSizePolicy, QSlider, QMenu, QAbstractItemView,
)

from samsara import command_catalog
from samsara.support_feedback import DOCUMENTATION_URL
from samsara.ui import qt_runtime

from samsara.log import get_logger
from samsara.ui import theme

logger = get_logger(__name__)

#: Shown instead of the list when no catalog can be built.
UNAVAILABLE_TEXT = (
    "The command list is unavailable right now. "
    f'See <a href="{DOCUMENTATION_URL}">Help</a> for the commands.'
)
_ALIAS_LIMIT = 3


def catalog_rows(commands: list | None) -> list | None:
    """Sheet rows for live registry rows (commands_cb()): one catalog record
    per command, keyed so the sheet can execute and pin it -- "phrase" is the
    canonical phrase. None when no catalog is available."""
    records = command_catalog.guidance_catalog(commands)
    if not records:
        return None
    rows = []
    for record in records:
        row = dict(record)
        row["phrase"] = command_catalog.canonical_phrase(record)
        row["shown_aliases"] = command_catalog.display_aliases(record, _ALIAS_LIMIT)
        rows.append(row)
    return rows


def plugin_label(plugin: str) -> str:
    return "Built-in" if plugin == "builtin" else plugin.replace("_", " ").title()


def row_tags(row: dict) -> list:
    tags = []
    if row.get("risk") == "destructive":
        tags.append("destructive")
    if row.get("whole_utterance"):
        tags.append("whole utterance")
    if row.get("scope_note"):
        tags.append(row["scope_note"])
        if row.get("live") is False:
            tags.append("not live here")
    return tags


# ---------------------------------------------------------------------------
# Scope (queue 68): which commands are live for the current app
# ---------------------------------------------------------------------------

def _scope_context():
    """The app the sheet describes: the foreground window, or the last
    external app when the sheet itself has focus. None if unavailable."""
    try:
        from samsara.command_scope import display_context
        return display_context()
    except Exception as exc:
        logger.debug(f"[CHEATSHEET] scope context unavailable: {exc}")
        return None


def annotate_scope(rows, context) -> list:
    """Copy rows, adding "live" (a candidate right now?) and "scope_note"
    (when a scoped command is live; "" for global commands)."""
    from samsara.command_scope import parse_scope, scope_live
    out = []
    for row in rows:
        row = dict(row)
        try:
            scope = parse_scope(row.get("scope"))
        except ValueError:
            scope = parse_scope({"tags": ["invalid_scope"]})
        row["scope_note"] = scope.describe() if scope is not None else ""
        row["live"] = scope_live(scope, context)[0]
        out.append(row)
    return out


def live_filter(rows, live_only: bool) -> tuple:
    """(rows to list, how many were hidden because they are not live here)."""
    if not live_only:
        return list(rows), 0
    shown = [r for r in rows if r.get("live", True)]
    return shown, len(rows) - len(shown)


def row_text(row: dict) -> str:
    """One list line: canonical phrase, up to 3 other phrases, then tags."""
    text = row["phrase"].title()
    if row.get("shown_aliases"):
        text += "  -  also: " + ", ".join(row["shown_aliases"])
    tags = row_tags(row)
    if tags:
        text += "  [" + ", ".join(tags) + "]"
    return text

# ---------------------------------------------------------------------------
# Colour: every value comes from samsara/ui/theme.py (queue 78). This window
# used to carry its own eight-colour palette, copied from the Tkinter version
# and then left behind as the tokens moved on without it. Five of the eight
# still matched their token by luck; three had drifted:
#
#   secondary text  "#7a8599"  vs  rgba(255,255,255,0.75)   4.79:1 -> 10.33:1
#   border          "#2a3345"  vs  rgba(255,255,255,0.16)
#   selected fill   "#1a3a42"  vs  the accent tinted below
#
# Measured, not assumed: the old palette did clear WCAG AA, but its secondary
# text sat 0.29 above the 4.5:1 line while the token sits 5.8 above it, and a
# muted slate beside the app's own near-white is what reads as "the colouring
# is all off". The only computed value is the selected fill, tinted from the
# accent exactly as history_view.py does it, because a flat dim-cyan hex
# drifts from the accent the moment the accent moves.
# ---------------------------------------------------------------------------

#: Selected / pinned / flash fill: the accent at 14%, over whatever surface
#: it sits on. Same alpha as history_view._SELECTED_BG, so a selected row
#: looks the same in both windows.
_SELECTED_ALPHA = 0.14


def _selected_bg():
    return theme._rgba(theme.ACCENT, _SELECTED_ALPHA)
#: Queue 78: the default is the width the content actually needs at the
#: TYPE_BODY floor. 440 px was chosen when every string in here was 14 px.
_DEFAULT_W = 520
_DEFAULT_H = 560
#: A list with a filter above it needs this much height to be worth opening.
_MIN_H = 180


def _disabled_packs(config: dict | None = None) -> set:
    """Packs the registry currently refuses (samsara.command_packs.get_enabled_packs).
    The sheet has no app handle, so it reads the same config.json the app
    loads; pass a dict to bypass the file (tests)."""
    try:
        from samsara.command_packs import PACKS, get_enabled_packs
        if config is None:
            import json
            from samsara.paths import samsara_config_path
            config = json.loads(samsara_config_path().read_text(encoding="utf-8"))
        return set(PACKS) - set(get_enabled_packs(config))
    except Exception:
        return set()


def _annotate_disabled(rows, disabled: set) -> list:
    """Copy rows, flagging those whose pack is off so the UI can say so."""
    out = []
    for row in rows:
        row = dict(row)
        row["pack_disabled"] = row.get("pack", "core") in disabled
        out.append(row)
    return out


def _ss():
    return f"""
QMainWindow, QWidget {{ background: {theme.BG0}; color: {theme.TEXT_PRIMARY}; font-family: {theme.FONT_FAMILY}; font-size: {theme.TYPE_BODY}px; }}
QListWidget {{
    background: {theme.BG1};
    border: none;
    outline: none;
    color: {theme.TEXT_PRIMARY};
    font-size: {theme.TYPE_BODY}px;
}}
QListWidget::item {{ padding: 6px 10px; }}
QListWidget::item:hover {{ background: {theme.BG2}; }}
QListWidget::item:selected {{ background: {_selected_bg()}; color: {theme.ACCENT}; }}
QLineEdit {{
    background: {theme.BG1};
    border: none;
    color: {theme.TEXT_PRIMARY};
    font-size: {theme.TYPE_BODY}px;
    padding: 5px 8px;
}}
QScrollBar:vertical {{
    background: {theme.BG0};
    width: 6px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {theme.BORDER};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSlider::groove:horizontal {{
    height: 3px;
    background: {theme.BORDER};
    border-radius: 1px;
}}
QSlider::handle:horizontal {{
    background: {theme.TEXT_SECONDARY};
    width: 10px;
    height: 10px;
    margin: -3px 0;
    border-radius: 5px;
}}
QSlider::sub-page:horizontal {{ background: {theme.ACCENT}; border-radius: 1px; }}
QMenu {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; }}
QMenu::item:selected {{ background: {_selected_bg()}; color: {theme.ACCENT}; }}
QComboBox {{
    background: {theme.BG2};
    color: {theme.TEXT_PRIMARY};
    border: 1px solid {theme.BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: {theme.TYPE_BODY}px;
    min-width: 120px;
}}
QComboBox:hover {{ border-color: {theme.ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: url({theme.ARROW_PATH});
    width: 10px;
    height: 6px;
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {theme.BG1};
    color: {theme.TEXT_PRIMARY};
    border: 1px solid {theme.BORDER};
    selection-background-color: {_selected_bg()};
    selection-color: {theme.ACCENT};
    outline: none;
}}
"""


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------

class CommandCheatSheetQt:
    """Drop-in Qt replacement for CommandCheatSheet."""

    def __init__(
        self,
        root=None,               # ignored — Tkinter root not needed
        execute_cb: Callable = None,
        commands_cb: Callable = None,
        palette_path: Path = None,
    ):
        self._execute_cb  = execute_cb  or (lambda p: None)
        self._commands_cb = commands_cb or (lambda: [])
        self._palette_path = Path(palette_path) if palette_path else Path("command_palette.json")
        self._window: "_CheatSheetWindow | None" = None
        self._init_posted = False
        self._visible = False

    # ----------------------------------------------------------------
    # Public API (callable from any thread)
    # ----------------------------------------------------------------

    def show(self):
        self._visible = True
        if self._window is not None:
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def hide(self):
        self._visible = False
        if self._window is not None:
            qt_runtime.post(self._window.hide)

    def toggle(self):
        if self._window is not None and self._window.isVisible():
            self.hide()
        else:
            self.show()

    def destroy(self):
        self._visible = False
        if self._window is not None:
            qt_runtime.post(self._window.deleteLater)
            self._window = None
            self._init_posted = False

    def refresh(self):
        if self._window is not None and self._visible:
            qt_runtime.post(self._window.refresh_commands)

    # ----------------------------------------------------------------
    # Qt-thread
    # ----------------------------------------------------------------

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _CheatSheetWindow(
            self._execute_cb, self._commands_cb, self._palette_path
        )
        self._window.show()


# ---------------------------------------------------------------------------
# Title bar (drag handle + opacity + close)
# ---------------------------------------------------------------------------

class _TitleBar(QWidget):
    def __init__(self, win: "_CheatSheetWindow"):
        super().__init__(win)
        # Queue 78: 34 px was sized for 14 px chrome text; the window's own
        # name is now TYPE_EMPHASIS and needs the room.
        self.setFixedHeight(40)
        self.setStyleSheet(f"background:{theme.BG1};")
        self._win = win
        self._drag_pos: QPoint | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 6, 0)
        lay.setSpacing(6)

        drag_lbl = QLabel("Command Reference")
        drag_lbl.setStyleSheet(
            f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_EMPHASIS}px;font-weight:600;")
        drag_lbl.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        lay.addWidget(drag_lbl, stretch=1)

        op_lbl = QLabel("opacity")
        op_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(op_lbl)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(35, 100)
        self._opacity_slider.setValue(int(win.windowOpacity() * 100))
        self._opacity_slider.setFixedWidth(70)
        self._opacity_slider.setStyleSheet(_ss())
        self._opacity_slider.valueChanged.connect(
            lambda v: win.setWindowOpacity(v / 100.0)
        )
        self._opacity_slider.sliderReleased.connect(win._save_palette)
        lay.addWidget(self._opacity_slider)

        close_lbl = QLabel("  x  ")
        close_lbl.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;padding:2px 4px;"
        )
        close_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_lbl.mousePressEvent = lambda _e: win.hide()
        close_lbl.enterEvent  = lambda _e: close_lbl.setStyleSheet(
            f"color:{theme.ERROR};font-size:{theme.TYPE_MIN}px;padding:2px 4px;")
        close_lbl.leaveEvent  = lambda _e: close_lbl.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;padding:2px 4px;")
        lay.addWidget(close_lbl)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, e):
        if (e.buttons() & Qt.MouseButton.LeftButton) and self._drag_pos is not None:
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        if self._drag_pos is not None:
            self._drag_pos = None
            self._win._save_palette()


# ---------------------------------------------------------------------------
# Static pane row (Most Used + Pinned sections)
# ---------------------------------------------------------------------------

class _StaticRow(QFrame):
    def __init__(self, cmd: dict, count: int | None, pinned: bool,
                 execute_cb, toggle_pin_cb, parent=None):
        super().__init__(parent)
        self._phrase = cmd["phrase"]
        self._execute_cb  = execute_cb
        self._toggle_pin  = toggle_pin_cb
        self._flashing    = False

        self.setFixedHeight(32)        # 78: 28 px clipped 16 px text
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(f"QFrame{{background:{theme.BG1};border:none;}}")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 8, 0)
        lay.setSpacing(0)

        self._pin_lbl = QLabel("*" if pinned else " ")
        self._pin_lbl.setFixedWidth(18)
        self._pin_lbl.setStyleSheet(
            f"color:{theme.ACCENT if pinned else theme.TEXT_SECONDARY};"
            f"font-size:{theme.TYPE_BODY}px;font-weight:600;"
        )
        self._pin_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._pin_lbl.mousePressEvent = lambda _e: self._on_pin()
        lay.addWidget(self._pin_lbl)

        phrase_lbl = QLabel(cmd["phrase"].title())
        phrase_lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(phrase_lbl, stretch=1)

        right_text = str(count) if count is not None else (
            cmd.get("pack", "") if cmd.get("pack", "") not in ("", "core") else ""
        )
        if cmd.get("pack_disabled"):
            right_text = (right_text + " (pack off)").strip()
        if right_text:
            right_lbl = QLabel(right_text)
            right_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
            lay.addWidget(right_lbl)

    def _on_pin(self):
        self._toggle_pin(self._phrase)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._flash()
            try:
                self._execute_cb(self._phrase)
            except Exception as exc:
                print(f"[CHEATSHEET] Execute '{self._phrase}': {exc}")

    def enterEvent(self, e):
        if not self._flashing:
            self.setStyleSheet(f"QFrame{{background:{theme.BG2};border:none;}}")
        self._pin_lbl.parentWidget()  # keep reference

    def leaveEvent(self, e):
        if not self._flashing:
            self.setStyleSheet(f"QFrame{{background:{theme.BG1};border:none;}}")

    def _flash(self):
        self._flashing = True
        self.setStyleSheet(f"QFrame{{background:{_selected_bg()};border:none;}}")
        QTimer.singleShot(300, self._unflash)

    def _unflash(self):
        self._flashing = False
        self.setStyleSheet(f"QFrame{{background:{theme.BG1};border:none;}}")


# ---------------------------------------------------------------------------
# Category selector — compact dropdown replacing the old tab strip
# ---------------------------------------------------------------------------

class _CategoryTabBar(QWidget):
    """A single-row category picker using a QComboBox.

    Replaces the original horizontal tab strip, which became unreadable
    at the window's default width once enough command packs were registered.
    A dropdown takes exactly one line regardless of how many categories exist.
    """

    def __init__(self, on_select, parent=None, on_live_only=None):
        super().__init__(parent)
        self._on_select = on_select
        self._on_live_only = on_live_only
        self._pack_ids: List[str] = []

        self.setFixedHeight(38)        # 78: room for TYPE_BODY controls
        self.setStyleSheet(
            f"background:{theme.BG1};border-bottom:1px solid {theme.BORDER};"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(6)

        cat_lbl = QLabel("Category")
        cat_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(cat_lbl)

        self._combo = QComboBox()
        self._combo.setStyleSheet(_ss())
        # Queue 78: AdjustToContents made the combo demand the width of its
        # longest pack name, and everything to its right -- "Live here only"
        # among them -- was clipped instead. The combo is the one thing on
        # this row that can elide honestly, so it is the one that gives way.
        self._combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._combo.setMinimumContentsLength(12)
        self._combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
        self._combo.currentIndexChanged.connect(self._on_changed)
        lay.addWidget(self._combo)
        lay.addStretch()

        # Queue 68: commands scoped to another app / state are hidden by
        # default; the box says how many and lets the user see them all.
        self._live_only = QCheckBox("Live here only")
        self._live_only.setChecked(True)
        self._live_only.setToolTip(
            "Some commands only work in a particular app or while something is on screen. "
            "Untick to list them all, with when each one works.")
        self._live_only.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        self._live_only.toggled.connect(self._on_live_toggled)
        lay.addWidget(self._live_only)
        self._hidden_lbl = QLabel("")
        self._hidden_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(self._hidden_lbl)

    def _on_live_toggled(self, checked: bool):
        if self._on_live_only is not None:
            self._on_live_only(bool(checked))

    def set_hidden_count(self, count: int):
        self._hidden_lbl.setText(f"({count} not live here)" if count else "")

    def set_categories(self, pack_ids: List[str], active_id: str, disabled: set = frozenset()):
        """pack_ids are the group ids (catalog plugin stems since queue 15)."""
        self._pack_ids = ["All"] + pack_ids
        self._disabled = set(disabled)
        self._combo.blockSignals(True)
        self._combo.clear()
        for pid in self._pack_ids:
            label = "All commands" if pid == "All" else plugin_label(pid)
            if pid in getattr(self, "_disabled", ()):
                label += " (off)"
            self._combo.addItem(label, userData=pid)
        # Restore selection
        idx = self._pack_ids.index(active_id) if active_id in self._pack_ids else 0
        self._combo.setCurrentIndex(idx)
        self._combo.blockSignals(False)

    def set_active(self, pack_id: str):
        if pack_id in self._pack_ids:
            self._combo.blockSignals(True)
            self._combo.setCurrentIndex(self._pack_ids.index(pack_id))
            self._combo.blockSignals(False)

    def _on_changed(self, index: int):
        if 0 <= index < len(self._pack_ids):
            self._on_select(self._pack_ids[index])


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class _CheatSheetWindow(QMainWindow):

    def __init__(
        self,
        execute_cb: Callable,
        commands_cb: Callable,
        palette_path: Path,
    ):
        super().__init__()
        self._execute_cb  = execute_cb
        self._commands_cb = commands_cb
        self._palette_path = palette_path
        self._all: List[dict] = []
        self._pinned: set = set()
        self._live_only = True
        self._active_category = "All"
        self._opacity = 0.85
        self._geom = {"x": None, "y": None, "w": _DEFAULT_W, "h": _DEFAULT_H}

        self._load_palette()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setWindowOpacity(self._opacity)
        self.resize(self._geom["w"], self._geom["h"])
        # The floor is set from the bars themselves once they exist (see the
        # end of __init__): 280 px let the user drag the window narrower than
        # its own title, which is how "Command Referen" and "Live here onl"
        # ended up cut off (queue 78). A layout that cannot fit is a layout
        # bug, not a reason to shorten the words.
        self.setMinimumHeight(_MIN_H)
        self.setStyleSheet(_ss())

        # Initial position: restore saved coords if present, otherwise
        # default to the right-centre of the primary screen.
        # showEvent will clamp to screen on every show() call.
        if self._geom["x"] is not None:
            self.move(int(self._geom["x"]), int(self._geom["y"]))
        else:
            scr = QApplication.primaryScreen().availableGeometry()
            self.move(scr.right() - _DEFAULT_W - 40, (scr.height() - _DEFAULT_H) // 2)

        # ---- Layout ---------------------------------------------------------
        # 1-px border via outer widget background
        outer = QWidget()
        outer.setStyleSheet(f"background:{theme.BORDER};")
        self.setCentralWidget(outer)
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(1, 1, 1, 1)
        outer_lay.setSpacing(0)

        inner = QWidget()
        inner.setStyleSheet(f"background:{theme.BG0};")
        outer_lay.addWidget(inner)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Title bar
        self._title_bar = _TitleBar(self)
        lay.addWidget(self._title_bar)

        # Filter
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter commands...")
        self._filter.setStyleSheet(
            f"QLineEdit{{background:{theme.BG1};border:none;color:{theme.TEXT_PRIMARY};"
            f"font-size:{theme.TYPE_BODY}px;padding:7px 10px;}}"
        )
        # Queue 78: Qt paints a placeholder at ~50% of the text colour, which
        # lands under 3:1. The role is set explicitly so it is TEXT_SECONDARY.
        _palette = self._filter.palette()
        _palette.setColor(QPalette.ColorRole.PlaceholderText,
                          theme.qcolor(theme.TEXT_SECONDARY))
        self._filter.setPalette(_palette)
        self._filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self._filter)

        _sep = lambda: [s := QFrame(), s.setFixedHeight(1),
                        s.setStyleSheet(f"background:{theme.BORDER};")][0]

        lay.addWidget(_sep())

        # Static pane (Most Used + Pinned) — rebuilt on refresh/pin change
        self._static_pane = QWidget()
        self._static_pane.setStyleSheet(f"background:{theme.BG0};")
        self._static_layout = QVBoxLayout(self._static_pane)
        self._static_layout.setContentsMargins(0, 0, 0, 0)
        self._static_layout.setSpacing(0)
        lay.addWidget(self._static_pane)

        # Category tab bar
        self._category_bar = _CategoryTabBar(self._set_category, on_live_only=self._set_live_only)
        lay.addWidget(self._category_bar)

        # Command list
        self._list = QListWidget()
        self._list.setStyleSheet(_ss())
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._list.setSpacing(0)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        lay.addWidget(self._list, stretch=1)

        # Degraded path: no catalog -> one honest line and a Help link.
        self._unavailable = QLabel(UNAVAILABLE_TEXT)
        self._unavailable.setObjectName("cheatsheetUnavailable")
        self._unavailable.setWordWrap(True)
        self._unavailable.setOpenExternalLinks(True)
        self._unavailable.setTextFormat(Qt.TextFormat.RichText)
        self._unavailable.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;padding:12px;")
        self._unavailable.setVisible(False)
        lay.addWidget(self._unavailable)

        # Resize grip row
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch()
        grip = QSizeGrip(inner)
        grip.setStyleSheet("background:transparent;")
        grip_row.addWidget(grip)
        lay.addLayout(grip_row)

        # Queue 78: the narrowest this window may be is the widest row it
        # has to draw -- the title bar and the category row, measured, not
        # guessed. Below this, Qt would clip a label mid-word.
        self._apply_width_floor()

        self.refresh_commands()

    # ----------------------------------------------------------------
    # Commands
    # ----------------------------------------------------------------

    def refresh_commands(self):
        try:
            live = self._commands_cb()
        except Exception as exc:
            print(f"[CHEATSHEET] commands_cb error: {exc}")
            live = None
        try:
            rows = catalog_rows(live)
        except Exception as exc:
            logger.warning(f"[CHEATSHEET] command catalog unavailable: {exc}")
            rows = None
        self._catalog_available = rows is not None
        self._all = annotate_scope(_annotate_disabled(rows or [], _disabled_packs()), _scope_context())
        self._unavailable.setVisible(not self._catalog_available)
        self._list.setVisible(self._catalog_available)
        self._category_bar.setVisible(self._catalog_available)
        self._filter.setVisible(self._catalog_available)
        self._rebuild_static_pane()

        # Groups are catalog plugins, in label order; a group reads "(off)"
        # when every command in it belongs to a disabled pack.
        plugin_ids = sorted({c["plugin"] for c in self._all}, key=plugin_label)
        if self._active_category != "All" and self._active_category not in plugin_ids:
            self._active_category = "All"
        off = {p for p in plugin_ids
               if all(c.get("pack_disabled") for c in self._all if c["plugin"] == p)}
        self._category_bar.set_categories(plugin_ids, self._active_category, off)
        self._apply_filter(self._filter.text())

    def _apply_filter(self, text: str = ""):
        raw = text.strip().lower()
        if raw:
            filtered = [
                c for c in self._all
                if raw in c["phrase"] or
                   any(raw in a for a in c.get("aliases", []))
            ]
        else:
            filtered = list(self._all)

        # Category (plugin group) filter
        if self._active_category != "All":
            filtered = [c for c in filtered if c.get("plugin") == self._active_category]

        # Scope (queue 68): hide commands that are not live here, and say how many.
        filtered, hidden = live_filter(filtered, self._live_only)
        self._category_bar.set_hidden_count(hidden)

        # Pinned items live in static pane — exclude from scroll list
        unpinned = [c for c in filtered if c["phrase"] not in self._pinned]

        self._list.blockSignals(True)
        self._list.clear()
        for cmd in unpinned:
            item = QListWidgetItem()
            # Execution uses the canonical phrase; the id is kept for tests
            # and tooling (every row resolves to a real catalog command).
            item.setData(Qt.ItemDataRole.UserRole, cmd["phrase"])
            item.setData(Qt.ItemDataRole.UserRole + 1, cmd["canonical_id"])
            item.setText(row_text(cmd))
            tooltip = cmd.get("description", "")
            if cmd.get("pack") not in (None, "", "core"):
                tooltip += f"\nPack: {cmd['pack']}" + (" (off)" if cmd.get("pack_disabled") else "")
            if cmd.get("scope_note"):
                tooltip += f"\nWorks {cmd['scope_note']}" + ("" if cmd.get("live", True) else " -- not live here")
            item.setToolTip(tooltip.strip())
            item.setForeground(theme.qcolor(
                theme.TEXT_PRIMARY if cmd.get("live", True) else theme.TEXT_SECONDARY))
            self._list.addItem(item)
        self._list.blockSignals(False)

    def _set_live_only(self, live_only: bool):
        self._live_only = live_only
        self._apply_filter(self._filter.text())

    def _set_category(self, cat: str):
        self._active_category = cat
        self._category_bar.set_active(cat)
        self._apply_filter(self._filter.text())
        self._save_palette()

    # ----------------------------------------------------------------
    # Static pane (Most Used + Pinned)
    # ----------------------------------------------------------------

    def _rebuild_static_pane(self):
        # Clear existing rows
        for i in reversed(range(self._static_layout.count())):
            child = self._static_layout.takeAt(i)
            if child.widget():
                child.widget().deleteLater()

        has_content = False
        # Usage stats are keyed by whatever phrase was said: map every alias.
        phrase_to_cmd = {}
        for c in self._all:
            for alias in c.get("aliases", []):
                phrase_to_cmd.setdefault(alias, c)
            phrase_to_cmd[c["phrase"]] = c

        # ---- Most Used ----
        try:
            from samsara.command_stats import get_top_commands
            top_raw = get_top_commands(8)
            top = [(name, cnt) for name, cnt in top_raw
                   if name in phrase_to_cmd and cnt > 0]
        except Exception:
            top = []

        if top:
            self._static_layout.addWidget(self._section_label("MOST USED"))
            for phrase, cnt in top:
                row = _StaticRow(
                    phrase_to_cmd[phrase], count=cnt,
                    pinned=(phrase_to_cmd[phrase]["phrase"] in self._pinned),
                    execute_cb=self._execute, toggle_pin_cb=self._toggle_pin,
                    parent=self._static_pane,
                )
                self._static_layout.addWidget(row)
            has_content = True

        # ---- Pinned ----
        pinned_cmds = [c for c in self._all if c["phrase"] in self._pinned]
        if pinned_cmds:
            if has_content:
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet(f"background:{theme.BORDER};")
                self._static_layout.addWidget(sep)
            self._static_layout.addWidget(self._section_label("PINNED"))
            for cmd in pinned_cmds:
                row = _StaticRow(
                    cmd, count=None,
                    pinned=True,
                    execute_cb=self._execute, toggle_pin_cb=self._toggle_pin,
                    parent=self._static_pane,
                )
                self._static_layout.addWidget(row)
            has_content = True

        if has_content:
            sep = QFrame()
            sep.setFixedHeight(1)
            sep.setStyleSheet(f"background:{theme.BORDER};margin:2px 0;")
            self._static_layout.addWidget(sep)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;font-weight:bold;"
            f"padding:3px 10px 2px 10px;background:{theme.BG0};"
        )
        return lbl

    # ----------------------------------------------------------------
    # Execute + flash
    # ----------------------------------------------------------------

    def _execute(self, phrase: str):
        try:
            self._execute_cb(phrase)
        except Exception as exc:
            print(f"[CHEATSHEET] Execute '{phrase}': {exc}")

    def _on_item_clicked(self, item: QListWidgetItem):
        phrase = item.data(Qt.ItemDataRole.UserRole)
        if not phrase:
            return
        # Flash
        orig = item.foreground()
        item.setForeground(theme.qcolor(theme.ACCENT))
        item.setBackground(theme.qcolor(_selected_bg()))
        def _restore():
            item.setForeground(orig)
            item.setBackground(QColor(Qt.GlobalColor.transparent))
        QTimer.singleShot(300, _restore)
        self._execute(phrase)

    def _apply_width_floor(self):
        """Minimum width = the widest fixed row's own size hint (queue 78).

        The title bar (name + opacity slider + close) and the category row
        (label + combo + "Live here only" + hidden count) are the two rows
        that cannot reflow. Asking them how wide they need to be, rather
        than hardcoding a number, means the floor follows the type scale:
        raise TYPE_BODY again and the window simply refuses to be squeezed
        further instead of clipping a word."""
        try:
            need = max(self._title_bar.sizeHint().width(),
                       self._category_bar.sizeHint().width())
        except Exception:
            return
        need += 2                                   # the 1 px border each side
        self.setMinimumWidth(need)
        if self.width() < need:
            self.resize(need, self.height())

    # ----------------------------------------------------------------
    # Pin
    # ----------------------------------------------------------------

    def _toggle_pin(self, phrase: str):
        if phrase in self._pinned:
            self._pinned.discard(phrase)
        else:
            self._pinned.add(phrase)
        self._save_palette()
        self._rebuild_static_pane()
        self._apply_filter(self._filter.text())

    def _on_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if item is None:
            return
        phrase = item.data(Qt.ItemDataRole.UserRole)
        if not phrase:
            return
        menu = QMenu(self)
        is_pinned = phrase in self._pinned
        action = menu.addAction("Unpin" if is_pinned else "Pin")
        menu.addSeparator()
        exec_action = menu.addAction("Execute")
        chosen = menu.exec(self._list.mapToGlobal(pos))
        if chosen == action:
            self._toggle_pin(phrase)
        elif chosen == exec_action:
            self._execute(phrase)

    # ----------------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------------

    def _load_palette(self):
        try:
            if self._palette_path.exists():
                data = json.loads(
                    self._palette_path.read_text(encoding="utf-8")
                )
                self._pinned          = set(data.get("pinned", []))
                self._opacity         = max(0.35, float(data.get("opacity", 0.85)))
                self._active_category = data.get("last_category", "All")
                g = data.get("geometry", {})
                self._geom = {
                    "x": g.get("x"),
                    "y": g.get("y"),
                    "w": g.get("w", _DEFAULT_W),
                    "h": g.get("h", _DEFAULT_H),
                }
        except Exception as e:
            logger.debug(f"_load_palette: {e}")

    def _save_palette(self):
        try:
            data = {
                "pinned":        sorted(self._pinned),
                "opacity":       round(self.windowOpacity(), 2),
                "last_category": self._active_category,
                "geometry": {
                    "x": self.x(), "y": self.y(),
                    "w": self.width(), "h": self.height(),
                },
            }
            self._palette_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            print(f"[CHEATSHEET] Save palette: {exc}")

    def showEvent(self, e):
        super().showEvent(e)
        self._clamp_to_screen()

    def _clamp_to_screen(self):
        """Move the window back inside its screen if any part is off-screen.

        Frameless windows bypass OS edge-clamping, so this must be called
        explicitly on every show() to guard against saved off-screen coords.
        """
        from PySide6.QtGui import QGuiApplication
        w, h = self.width(), self.height()
        scr = (QGuiApplication.screenAt(self.frameGeometry().topLeft()) or
               QApplication.primaryScreen()).availableGeometry()
        x = max(scr.left(), min(self.x(), scr.right()  - w))
        y = max(scr.top(),  min(self.y(), scr.bottom() - h))
        if x != self.x() or y != self.y():
            self.move(x, y)

    def closeEvent(self, e):
        self._save_palette()
        e.ignore()
        self.hide()

    def hideEvent(self, e):
        self._save_palette()
        e.accept()
