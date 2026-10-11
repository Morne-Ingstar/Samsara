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
import shutil
from pathlib import Path
from typing import Callable, List

from PySide6.QtCore import Qt, QTimer, Signal, QPoint, QRect, QSignalBlocker, QEvent
from PySide6.QtGui import QColor, QCursor, QPalette, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QListView, QListWidget, QListWidgetItem, QLineEdit, QPushButton,
    QFrame, QSizeGrip, QSizePolicy, QSlider, QMenu, QAbstractItemView,
    QAbstractButton, QStyle, QStyleOption,
)

from samsara import command_catalog
from samsara.support_feedback import DOCUMENTATION_URL
from samsara.ui import qt_runtime

from samsara.log import get_logger
from samsara.ui import theme
from samsara.ui.command_reference_detail import CommandDetailCard, CommandRowDelegate, style_search
from samsara.ui.settings.calm_layout import ScrollEdges

logger = get_logger(__name__)


def _migrate_palette_state(legacy_path: Path, target_path: Path) -> None:
    """Move the former cwd state file into the per-user app directory once."""
    marker = target_path.parent / ".command-palette-migrated"
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            return
        if (legacy_path.resolve() != target_path.resolve()
                and legacy_path.is_file() and not target_path.exists()):
            try:
                legacy_path.replace(target_path)
            except OSError:
                # The old and new locations can be on different volumes.
                if not target_path.exists():
                    shutil.move(str(legacy_path), str(target_path))
        marker.write_text("command palette state migration checked\n", encoding="utf-8")
    except OSError as exc:
        # A read-only profile must not prevent the guide from opening.
        logger.warning(f"Command Reference state migration skipped: {exc}")


def _palette_state_path(explicit_path: Path | None = None) -> Path:
    if explicit_path is not None:
        return Path(explicit_path)
    from samsara.paths import samsara_home_dir

    target = samsara_home_dir() / "command_palette.json"
    _migrate_palette_state(Path.cwd() / "command_palette.json", target)
    return target

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


def rows_for_packs(rows: list[dict], pack_ids) -> list[dict]:
    """Rows visible in a pack-scoped cheat sheet; ``None`` keeps all rows."""
    if pack_ids is None:
        return list(rows)
    return [row for row in rows if row.get("pack", "core") in pack_ids]


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


def _row_is_personal_alias(row: dict) -> bool:
    """Whether the visible aliases mark this row as user-owned."""
    return any(str(alias).endswith(" (yours)")
               for alias in row.get("shown_aliases", ()))


def _row_text_colour(row: dict) -> str:
    """Text role for a command row, including the personal-alias cue."""
    if not row.get("live", True) or _row_is_personal_alias(row):
        return theme.TEXT_SECONDARY
    return theme.TEXT_PRIMARY

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
_DEFAULT_W = 460
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
QMainWindow {{ background: {theme.BG0}; }}
QWidget {{ color: {theme.TEXT_PRIMARY}; font-family: {theme.FONT_FAMILY}; font-size: {theme.TYPE_BODY}px; }}
QListWidget {{
    background: {theme.BG1};
    border: none;
    outline: none;
    color: {theme.TEXT_PRIMARY};
    font-size: {theme.TYPE_BODY}px;
}}
QListWidget::item {{ padding: 0; }}
QListWidget::item:hover {{ background: {theme.BG2}; }}
QListWidget::item:selected {{ background: {_selected_bg()}; color: {theme.ACCENT}; }}
QLineEdit {{
    background: {theme.BG2};
    border: 1px solid {theme.BORDER};
    border-radius: 6px;
    color: {theme.TEXT_PRIMARY};
    font-size: {theme.TYPE_BODY}px;
    padding: 6px 10px;
}}
QLineEdit:focus {{ border-color: {theme.ACCENT}; }}
{theme.SCROLLBAR_QSS}
QSlider::groove:horizontal {{
    height: 4px;
    background: {theme.BORDER};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {theme.ACCENT};
    border: 2px solid {theme.BG1};
    width: 14px;
    height: 14px;
    margin: -7px 0;
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{ background: {theme.ACCENT}; border-radius: 1px; }}
QMenu {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; }}
QMenu::item:selected {{ background: {_selected_bg()}; color: {theme.ACCENT}; }}
QComboBox {{
    background: {theme.BG2};
    color: {theme.TEXT_PRIMARY};
    border: 1px solid {theme.BORDER};
    border-radius: 4px;
    padding: 0 8px;
    min-height: 0;
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
        self._palette_path = _palette_state_path(palette_path)
        self._window: "_CheatSheetWindow | None" = None
        self._init_posted = False
        self._visible = False
        self._pending_pack_scope = None
        self._pending_view = None

    # ----------------------------------------------------------------
    # Public API (callable from any thread)
    # ----------------------------------------------------------------

    def show(self):
        self._request_show(None, None)

    def show_filtered(self, text: str):
        """Apply Home's category and query before the first list is built."""
        self._request_show(None, ("All", text))

    def _request_show(self, scope, view):
        self._visible = True
        self._pending_pack_scope = scope
        self._pending_view = view
        if self._window is not None:
            qt_runtime.post(lambda: self._show_with_pack_scope(scope, view))
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def show_scoped(self, pack_ids):
        """Show only ``pack_ids``; safe to call from the session worker."""
        self._request_show(frozenset(pack_ids), None)

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
            self._execute_cb, self._commands_cb, self._palette_path,
            initial_scope=self._pending_pack_scope, initial_view=self._pending_view,
        )
        self._window.show()

    def _show_with_pack_scope(self, pack_ids, view=None):
        """Apply scope and raise the sheet on the Qt thread."""
        self._window.set_view(pack_ids, view)
        self._window.show()
        self._window.raise_()


# ---------------------------------------------------------------------------
# Title bar (drag handle + opacity + close)
# ---------------------------------------------------------------------------

class _TitleBar(QWidget):
    def __init__(self, win: "_CheatSheetWindow"):
        super().__init__(win)
        self.setFixedHeight(theme.HIT_TARGET_MIN + 2 * theme.SPACE_1)
        self.setStyleSheet(f"background:{theme.BG1};")
        self._win = win
        self._drag_pos: QPoint | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.SPACE_2, theme.SPACE_1, theme.SPACE_2, theme.SPACE_1)
        lay.setSpacing(theme.SPACE_1)

        self._title_label = QLabel("Command Reference")
        self._title_label.setStyleSheet(
            f"background:transparent;color:{theme.TEXT_PRIMARY};"
            f"font-size:{theme.TYPE_EMPHASIS}px;font-weight:600;")
        self._title_label.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        lay.addWidget(self._title_label, stretch=1)

        self._opacity_label = QLabel("Opacity")
        self._opacity_label.setStyleSheet(
            f"background:transparent;color:{theme.TEXT_SECONDARY};"
            f"font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(self._opacity_label)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(35, 100)
        self._opacity_slider.setValue(int(win.windowOpacity() * 100))
        self._opacity_slider.setFixedWidth(72)
        self._opacity_slider.setAccessibleName("Window opacity")
        self._opacity_slider.setToolTip("Adjust command-reference transparency")
        self._opacity_slider.setStyleSheet(_ss())
        self._opacity_slider.valueChanged.connect(
            lambda v: win.setWindowOpacity(v / 100.0)
        )
        self._opacity_slider.sliderReleased.connect(win._save_palette)
        lay.addWidget(self._opacity_slider)

        self._close_button = QPushButton("✕")
        self._close_button.setAccessibleName("Close command reference")
        self._close_button.setToolTip("Close command reference")
        self._close_button.setFixedSize(theme.HIT_TARGET_MIN, theme.HIT_TARGET_MIN)
        self._close_button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._close_button.setStyleSheet(
            f"QPushButton{{background:{theme.BG2};color:{theme.TEXT_SECONDARY};"
            f"border:1px solid {theme.BORDER};border-radius:6px;font-size:{theme.TYPE_TITLE}px;}}"
            f"QPushButton:hover{{border-color:{theme.ERROR};color:{theme.ERROR};}}"
            f"QPushButton:pressed{{background:{_selected_bg()};color:{theme.ERROR};}}"
        )
        self._close_button.clicked.connect(win.hide)
        lay.addWidget(self._close_button)

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

class _ActionButton(QAbstractButton):
    """Native action semantics with the reference's existing label rendering."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

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


class _StaticRow(_ActionButton):
    def __init__(self, cmd: dict, count: int | None, pinned: bool,
                 execute_cb, toggle_pin_cb, parent=None):
        super().__init__(parent)
        self._phrase = cmd["phrase"]
        self._execute_cb  = execute_cb
        self._toggle_pin  = toggle_pin_cb

        self.setFixedHeight(theme.HIT_TARGET_MIN)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._set_background(theme.BG1)
        self.clicked.connect(self._activate)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACE_1)

        self._pin_lbl = _ActionButton(self)
        self._pin_lbl.setCheckable(True)
        self._pin_lbl.setChecked(pinned)
        self._pin_lbl.setFixedWidth(18)
        self._pin_lbl.setStyleSheet("QAbstractButton{background:transparent;border:none;}")
        self._pin_mark = QLabel("*" if pinned else " ", self._pin_lbl)
        self._pin_mark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._pin_mark.setStyleSheet(
            f"color:{theme.ACCENT if pinned else theme.TEXT_SECONDARY};"
            f"font-size:{theme.TYPE_BODY}px;font-weight:600;"
        )
        pin_layout = QHBoxLayout(self._pin_lbl)
        pin_layout.setContentsMargins(0, 0, 0, 0)
        pin_layout.addWidget(self._pin_mark)
        self._pin_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._pin_lbl.clicked.connect(self._on_pin)

        phrase_lbl = QLabel(cmd["phrase"].title())
        self.setAccessibleName(phrase_lbl.text())
        self._pin_lbl.setAccessibleName(f"Pin {phrase_lbl.text()}")
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
        lay.addWidget(self._pin_lbl)

    def _on_pin(self):
        pinned = self._pin_lbl.isChecked()
        self._pin_mark.setText("*" if pinned else " ")
        self._pin_mark.setStyleSheet(
            f"color:{theme.ACCENT if pinned else theme.TEXT_SECONDARY};"
            f"font-size:{theme.TYPE_BODY}px;font-weight:600;"
        )
        self._toggle_pin(self._phrase)

    def _set_background(self, colour):
        # QLabel inherits QFrame: the former frame rule also filled its labels.
        self.setStyleSheet(
            f"QAbstractButton{{background:{colour};border:none;border-bottom:1px solid {theme.BORDER_FAINT};}}"
            f"QLabel{{background:transparent;border:none;}}")

    def _activate(self):
        try:
            self._execute_cb(self._phrase, self)
        except Exception as exc:
            print(f"[CHEATSHEET] Execute '{self._phrase}': {exc}")

    def enterEvent(self, e):
        self._set_background(theme.BG2)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._set_background(theme.BG1)
        super().leaveEvent(e)


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

        self.setFixedHeight(theme.HIT_TARGET_MIN + theme.SPACE_2)
        self.setStyleSheet(
            f"background:{theme.BG0};"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.SPACE_2, theme.SPACE_1, theme.SPACE_2, theme.SPACE_1)
        lay.setSpacing(theme.SPACE_1)

        self._combo = QComboBox()
        self._combo.setAccessibleName("Command category")
        self._combo.setToolTip("Choose a command category")
        self._combo.setStyleSheet(_ss())
        self._combo.setFixedHeight(theme.HIT_TARGET_MIN)
        # Queue 78: AdjustToContents made the combo demand the width of its
        # longest pack name, and everything to its right -- "Live here only"
        # among them -- was clipped instead. The combo is the one thing on
        # this row that can elide honestly, so it is the one that gives way.
        self._combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._combo.setMinimumContentsLength(10)
        self._combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
        self._combo.currentIndexChanged.connect(self._on_changed)
        lay.addWidget(self._combo, stretch=1)

        # Queue 68: commands scoped to another app / state are hidden by
        # default; the box says how many and lets the user see them all.
        self._live_only = QCheckBox("Live only")
        self._live_only.setFixedHeight(theme.HIT_TARGET_MIN)
        self._live_only.setAccessibleName("Show commands live here only")
        self._live_only.setChecked(True)
        self._live_only.setToolTip(
            "Some commands only work in a particular app or while something is on screen. "
            "Untick to list them all, with when each one works.")
        self._live_only.setStyleSheet(
            f"background:transparent;color:{theme.TEXT_SECONDARY};"
            f"font-size:{theme.TYPE_BODY}px;")
        self._live_only.toggled.connect(self._on_live_toggled)
        lay.addWidget(self._live_only)
        self._hidden_lbl = QLabel("")
        self._hidden_lbl.setStyleSheet(
            f"background:transparent;color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        self._hidden_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._hidden_lbl.setMinimumWidth(0)
        lay.addWidget(self._hidden_lbl)

    def _on_live_toggled(self, checked: bool):
        if self._on_live_only is not None:
            self._on_live_only(bool(checked))

    def set_hidden_count(self, count: int):
        self._hidden_lbl.setText(f"({count} not live here)" if count else "")
        self._hidden_lbl.setToolTip(
            f"{count} commands are not available in the current app or state" if count else "")

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

class _VisibleSizeGrip(QSizeGrip):
    """A normal resize grip with a visible, theme-aware direction cue."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Resize command reference")
        self.setToolTip("Drag to resize")
        self.setFixedSize(32, 32)
        self.setCursor(QCursor(Qt.CursorShape.SizeFDiagCursor))
        self.setStyleSheet(
            f"QSizeGrip{{background:{theme.BG1};border-left:1px solid {theme.BORDER};"
            f"border-top:1px solid {theme.BORDER};}}")
        self._cue = QLabel("↘", self)
        self._cue.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cue.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._cue.setStyleSheet(
            f"background:transparent;color:{theme.ACCENT};font-size:{theme.TYPE_HEADING}px;")
        parent.installEventFilter(self)

    def eventFilter(self, parent, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            QTimer.singleShot(0, self, self._place)
        return super().eventFilter(parent, event)

    def _place(self):
        parent = self.parentWidget()
        self.move(parent.width() - self.width(), parent.height() - self.height())
        self.raise_()

    def resizeEvent(self, event):
        self._cue.setGeometry(self.rect())
        super().resizeEvent(event)


class _CheatSheetWindow(QMainWindow):

    def __init__(
        self,
        execute_cb: Callable,
        commands_cb: Callable,
        palette_path: Path,
        *, initial_scope=None, initial_view=None,
    ):
        super().__init__()
        self._execute_cb  = execute_cb
        self._commands_cb = commands_cb
        self._palette_path = palette_path
        self._all: List[dict] = []
        self._pack_scope = initial_scope
        self._refill_running = False
        self._refill_scheduled = False
        self._pending_filter = None
        self._pinned: set = set()
        self._live_only = True
        self._active_category = "All"
        self._opacity = 0.85
        self._most_used_expanded = False
        self._geom = {"x": None, "y": None, "w": _DEFAULT_W, "h": _DEFAULT_H}
        self._has_opened = False

        self._load_palette()
        if initial_view is not None:
            self._active_category = initial_view[0]

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

        # Search. The inset card remains visible on both palettes; the old
        # borderless white field disappeared into the light window.
        search_wrap = QWidget()
        search_wrap.setStyleSheet(f"background:{theme.BG0};")
        search_layout = QHBoxLayout(search_wrap)
        search_layout.setContentsMargins(theme.SPACE_2, theme.SPACE_1, theme.SPACE_2, theme.SPACE_1)
        self._filter = QLineEdit()
        self._filter.setAccessibleName("Search commands")
        self._filter.setPlaceholderText("Search commands…")
        style_search(self._filter)
        # Queue 78: Qt paints a placeholder at ~50% of the text colour, which
        # lands under 3:1. The role is set explicitly so it is TEXT_SECONDARY.
        _palette = self._filter.palette()
        _palette.setColor(QPalette.ColorRole.PlaceholderText,
                          theme.qcolor(theme.TEXT_SECONDARY))
        self._filter.setPalette(_palette)
        if initial_view is not None:
            self._filter.setText(initial_view[1])
        self._filter.textChanged.connect(self._apply_filter)
        search_layout.addWidget(self._filter)
        lay.addWidget(search_wrap)

        _sep = lambda: [s := QFrame(), s.setFixedHeight(1),
                        s.setStyleSheet(f"background:{theme.BORDER};")][0]

        lay.addWidget(_sep())

        # Static pane (Most Used + Pinned) — rebuilt on refresh/pin change
        self._static_pane = QWidget()
        self._static_pane.setStyleSheet(f"background:{theme.BG0};")
        self._static_layout = QVBoxLayout(self._static_pane)
        self._static_layout.setContentsMargins(theme.SPACE_2, 0, theme.SPACE_2, 0)
        self._static_layout.setSpacing(0)
        lay.addWidget(self._static_pane)

        # Category tab bar
        self._category_bar = _CategoryTabBar(self._set_category, on_live_only=self._set_live_only)
        lay.addWidget(self._category_bar)

        # Command list
        self._list = QListWidget()
        self._list.setAccessibleName("Command list")
        self._list.setStyleSheet(_ss())
        self._list.setViewportMargins(theme.SPACE_2, 0, theme.SPACE_2, 0)
        self._list.setItemDelegate(CommandRowDelegate(self._list))
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setMouseTracking(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setWordWrap(True)
        self._list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._list.setResizeMode(QListView.ResizeMode.Adjust)
        self._list.setUniformItemSizes(False)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setSpacing(0)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemActivated.connect(self._on_item_clicked)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        lay.addWidget(self._list, stretch=1)
        self._scroll_edges = ScrollEdges(self._list)

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

        # Overlay the grip in the corner instead of reserving an empty strip.
        self._resize_grip = _VisibleSizeGrip(inner)

        # Queue 78: the narrowest this window may be is the widest row it
        # has to draw -- the title bar and the category row, measured, not
        # guessed. Below this, Qt would clip a label mid-word.
        self._apply_width_floor()

        theme.apply_design_scale(self)
        # The additive guide treatment sets minimum heights plus padding;
        # these fixed controls already include both in their 44-DIP target.
        self._category_bar._combo.setStyleSheet(_ss())
        self._title_bar._close_button.setStyleSheet(
            theme.secondary_button_qss() + 'QPushButton{padding:0;min-height:0;}')
        self._most_used_qss = (
            f'QPushButton{{background:{theme.BG0};color:{theme.TEXT_SECONDARY};border:none;'
            f'text-align:left;padding:0;min-height:{theme.HIT_TARGET_MIN}px;max-height:{theme.HIT_TARGET_MIN}px;'
            f'font-size:{theme.TYPE_BODY}px;font-weight:600;}}'
            f'QPushButton:hover{{background:{theme.BG2};color:{theme.TEXT_PRIMARY};}}')
        self._detail_card = CommandDetailCard(self, inner)
        self.refresh_commands()

    # ----------------------------------------------------------------
    # Commands
    # ----------------------------------------------------------------

    def set_pack_scope(self, pack_ids):
        """Restrict this open sheet to catalog rows in ``pack_ids``.

        ``None`` is the normal, unscoped command-reference view.
        """
        self.set_view(pack_ids)

    def set_view(self, pack_ids, view=None):
        """Change scope/category/search as one refill, on the Qt thread."""
        scope = None if pack_ids is None else frozenset(pack_ids)
        scope_changed = scope != self._pack_scope
        self._pack_scope = scope
        if view is not None:
            self._active_category, text = view
            self._category_bar.set_active(self._active_category)
            with QSignalBlocker(self._filter):
                self._filter.setText(text)
        if scope_changed:
            self.refresh_commands()
        elif view is not None:
            self._apply_filter(self._filter.text())
        if view is not None:
            self._save_palette()

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
        annotated = annotate_scope(_annotate_disabled(rows or [], _disabled_packs()), _scope_context())
        self._all = rows_for_packs(annotated, self._pack_scope)
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
        if self._refill_running:
            self._pending_filter = text
            return
        self._pending_filter = None
        self._refill_running = True
        try:
            self._refill_list(text)
        finally:
            self._refill_running = False
            if self._pending_filter is not None and not self._refill_scheduled:
                self._refill_scheduled = True
                QTimer.singleShot(0, self, self._drain_filter)

    def _drain_filter(self):
        self._refill_scheduled = False
        text, self._pending_filter = self._pending_filter, None
        if text is not None:
            self._apply_filter(text)

    def _refill_list(self, text: str):
        self._detail_card.dismiss()
        raw = text.strip().lower()
        self._static_pane.setVisible(not raw and getattr(self, "_static_has_content", False))
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

        # Searching is allowed to find pinned commands too because the static
        # pane gets out of the way while a query is active.
        unpinned = (filtered if raw else
                    [c for c in filtered if c["phrase"] not in self._pinned])

        with QSignalBlocker(self._list):
            self._item_generation = getattr(self, "_item_generation", 0) + 1
            item_generation = self._item_generation
            self._list.clear()
            for cmd in unpinned:
                item = QListWidgetItem()
                # Execution uses the canonical phrase; the id is kept for tests
                # and tooling (every row resolves to a real catalog command).
                item.setData(Qt.ItemDataRole.UserRole, cmd["phrase"])
                item.setData(Qt.ItemDataRole.UserRole + 1, cmd["canonical_id"])
                item.setData(Qt.ItemDataRole.UserRole + 2, item_generation)
                item.setText(row_text(cmd))
                tooltip = cmd.get("description", "")
                if cmd.get("pack") not in (None, "", "core"):
                    tooltip += f"\nPack: {cmd['pack']}" + (" (off)" if cmd.get("pack_disabled") else "")
                if cmd.get("scope_note"):
                    tooltip += f"\nWorks {cmd['scope_note']}" + ("" if cmd.get("live", True) else " -- not live here")
                item.setToolTip(tooltip.strip())
                item.setForeground(theme.qcolor(_row_text_colour(cmd)))
                self._list.addItem(item)

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
        self._detail_card.dismiss()
        # Clear existing rows
        for i in reversed(range(self._static_layout.count())):
            child = self._static_layout.takeAt(i)
            if child.widget():
                child.widget().hide()
                child.widget().deleteLater()

        has_content = False
        self._most_used_button = None
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
            arrow = "▼" if self._most_used_expanded else "▶"
            self._most_used_button = QPushButton(f"{arrow}  Most used ({len(top)})")
            self._most_used_button.setAccessibleName(
                ("Collapse" if self._most_used_expanded else "Expand") + " most used commands")
            self._most_used_button.setToolTip(
                "Hide most used commands" if self._most_used_expanded else "Show most used commands")
            self._most_used_button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._most_used_button.setStyleSheet(self._most_used_qss)
            self._most_used_button.setFixedHeight(theme.HIT_TARGET_MIN)
            self._most_used_button.clicked.connect(self._toggle_most_used)
            self._static_layout.addWidget(self._most_used_button)
            if self._most_used_expanded:
                for phrase, cnt in top:
                    row = _StaticRow(
                        phrase_to_cmd[phrase], count=cnt,
                        pinned=(phrase_to_cmd[phrase]["phrase"] in self._pinned),
                        execute_cb=self._show_details, toggle_pin_cb=self._toggle_pin,
                        parent=self._static_pane,
                    )
                    self._static_layout.addWidget(row)
            has_content = True

        # ---- Pinned ----
        pinned_cmds = [c for c in self._all if c["phrase"] in self._pinned]
        if pinned_cmds:
            if has_content and self._most_used_expanded:
                sep = QFrame()
                sep.setFixedHeight(1)
                sep.setStyleSheet(f"background:{theme.BORDER};")
                self._static_layout.addWidget(sep)
            self._static_layout.addWidget(self._section_label("PINNED"))
            for cmd in pinned_cmds:
                row = _StaticRow(
                    cmd, count=None,
                    pinned=True,
                    execute_cb=self._show_details, toggle_pin_cb=self._toggle_pin,
                    parent=self._static_pane,
                )
                self._static_layout.addWidget(row)
            has_content = True

        if has_content:
            sep = QFrame()
            sep.setFixedHeight(1)
            sep.setStyleSheet(f"background:{theme.BORDER};margin:2px 0;")
            self._static_layout.addWidget(sep)

        self._static_has_content = has_content
        self._static_pane.setVisible(
            has_content and not bool(self._filter.text().strip()))
        # Rebuilt children otherwise enter the focus chain after the list.
        previous = self._filter
        for index in range(self._static_layout.count()):
            widget = self._static_layout.itemAt(index).widget()
            actions = ((widget, widget._pin_lbl) if isinstance(widget, _StaticRow)
                       else (widget,) if widget is self._most_used_button else ())
            for action in actions:
                QWidget.setTabOrder(previous, action)
                previous = action
        QWidget.setTabOrder(previous, self._category_bar._combo)

    def _toggle_most_used(self):
        self._most_used_expanded = not self._most_used_expanded
        self._save_palette()
        self._rebuild_static_pane()

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;font-weight:bold;"
            f"padding:3px 0 2px 0;background:{theme.BG0};"
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
        rect = self._list.visualItemRect(item)
        rect.moveTopLeft(self._list.viewport().mapTo(self._detail_card.parentWidget(), rect.topLeft()))
        self._list.setCurrentItem(item)
        del item
        self._show_details(phrase, self._list, rect)

    def _show_details(self, phrase, origin, anchor=None):
        command = next((row for row in self._all if row['phrase'] == phrase), None)
        if command is None:
            return
        if anchor is None:
            anchor = QRect(origin.mapTo(self._detail_card.parentWidget(), QPoint()), origin.size())
        self._detail_card.open(command, origin, anchor)

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
        pin_focused = any(row._phrase == phrase and row._pin_lbl.hasFocus()
                          for row in self.findChildren(_StaticRow))
        if phrase in self._pinned:
            self._pinned.discard(phrase)
        else:
            self._pinned.add(phrase)
        self._save_palette()
        self._rebuild_static_pane()
        self._apply_filter(self._filter.text())
        if pin_focused:
            for row in self.findChildren(_StaticRow):
                if row._phrase == phrase and row.isVisibleTo(self):
                    row._pin_lbl.setFocus(Qt.FocusReason.OtherFocusReason)
                    break
            else:
                self._list.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if item is None:
            return
        phrase = item.data(Qt.ItemDataRole.UserRole)
        del item
        if not phrase:
            return
        menu = QMenu(self)
        is_pinned = phrase in self._pinned
        action = menu.addAction("Unpin" if is_pinned else "Pin")
        menu.addSeparator()
        chosen = menu.exec(self._list.mapToGlobal(pos))
        if chosen == action:
            self._toggle_pin(phrase)

    # ----------------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------------

    def _load_palette(self):
        try:
            if self._palette_path.exists():
                data = json.loads(
                    self._palette_path.read_text(encoding="utf-8")
                )
                if not isinstance(data, dict):
                    return
                self._pinned          = set(data.get("pinned", []))
                self._opacity         = max(0.35, float(data.get("opacity", 0.85)))
                self._active_category = data.get("last_category", "All")
                self._most_used_expanded = bool(data.get("most_used_expanded", False))
                g = data.get("geometry", {})
                if not isinstance(g, dict):
                    g = {}
                try:
                    x, y = int(g["x"]), int(g["y"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    x = y = None
                try:
                    width = max(1, int(g.get("w", _DEFAULT_W)))
                    height = max(1, int(g.get("h", _DEFAULT_H)))
                except (TypeError, ValueError, OverflowError):
                    width, height = _DEFAULT_W, _DEFAULT_H
                self._geom = {
                    "x": x,
                    "y": y,
                    "w": width,
                    "h": height,
                }
        except Exception as e:
            logger.debug(f"_load_palette: {e}")

    def _save_palette(self):
        try:
            data = {
                "pinned":        sorted(self._pinned),
                "opacity":       round(self.windowOpacity(), 2),
                "last_category": self._active_category,
                "most_used_expanded": self._most_used_expanded,
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
        initial = not self._has_opened
        has_saved_position = self._geom["x"] is not None and self._geom["y"] is not None
        if initial and has_saved_position:
            self.move(self._geom["x"], self._geom["y"])
            candidate = QRect(self._geom["x"], self._geom["y"],
                              self.width(), self.height())
        elif initial:
            candidate = QRect()
        else:
            candidate = QRect(self.frameGeometry())

        screen = self._screen_for_visible_rect(candidate)
        saved_rejected = bool(
            (initial and has_saved_position and screen is None)
            or (not initial and screen is None)
        )
        if screen is None:
            screen = self._foreground_screen() or QApplication.primaryScreen()
            if screen is None:
                screens = QApplication.screens()
                screen = screens[0] if screens else None
            if screen is not None:
                area = screen.availableGeometry()
                x = area.right() - self.width() - 40
                y = area.top() + (area.height() - self.height()) // 2
                self.move(x, y)
        if screen is not None:
            screen = self._clamp_to_screen(screen) or screen

        self._has_opened = True
        resolved = QApplication.screenAt(self.frameGeometry().center()) or screen
        screen_name = resolved.name() if resolved is not None else "unknown"
        logger.info(
            "[CHEATSHEET] Opened geometry=(%d,%d,%d,%d) screen=%s "
            "saved_rejected=%s",
            self.x(), self.y(), self.width(), self.height(), screen_name,
            saved_rejected,
        )

    @staticmethod
    def _screen_for_visible_rect(rect):
        """Find a current screen containing a substantial part of ``rect``.

        At least one quarter of the frame, with a minimum useful overlap of
        up to 120x140 DIPs, must remain visible. This rejects stale monitor
        coordinates while accepting a window that only needs edge-clamping.
        """
        if rect.isEmpty():
            return None
        minimum_width = max(1, min(120, int(rect.width() * 0.30)))
        minimum_height = max(1, min(140, int(rect.height() * 0.30)))
        minimum_area = rect.width() * rect.height() * 0.25
        best_screen, best_area = None, 0
        for candidate in QApplication.screens():
            visible = rect.intersected(candidate.availableGeometry())
            area = visible.width() * visible.height()
            if (visible.width() >= minimum_width
                    and visible.height() >= minimum_height
                    and area >= minimum_area and area > best_area):
                best_screen, best_area = candidate, area
        return best_screen

    def _foreground_screen(self):
        """Resolve the screen containing the Windows foreground window."""
        try:
            import sys
            if sys.platform != "win32":
                return None
            import ctypes
            hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
            if not hwnd:
                return None
            # This existing mapper handles Windows DPI coordinate conversion.
            from samsara.ui.numbers_overlay_qt import screen_for_hwnd
            return screen_for_hwnd(hwnd)
        except Exception as exc:
            logger.debug(f"Command Reference foreground-screen lookup failed: {exc}")
            return None

    def _clamp_to_screen(self, screen=None):
        """Move the window back inside its screen if any part is off-screen.

        Frameless windows bypass OS edge-clamping, so this must be called
        explicitly on every show() to guard against saved off-screen coords.
        """
        screen = screen or (
            QGuiApplication.screenAt(self.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return None
        w, h = self.width(), self.height()
        scr = screen.availableGeometry()
        x = max(scr.left(), min(self.x(), scr.right()  - w))
        y = max(scr.top(),  min(self.y(), scr.bottom() - h))
        if x != self.x() or y != self.y():
            self.move(x, y)
        return screen

    def closeEvent(self, e):
        self._save_palette()
        e.ignore()
        self.hide()

    def hideEvent(self, e):
        self._detail_card.dismiss()
        self._save_palette()
        e.accept()
