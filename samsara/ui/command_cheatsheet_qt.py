"""
PySide6 command cheat sheet for Samsara.

Drop-in replacement for CommandCheatSheet with the same public API:
    show() / hide() / toggle() / destroy() / refresh()

Runs on its own daemon thread.  show() / hide() / toggle() are safe
to call from any thread (including the Tkinter main thread) because
they route through QTimer.singleShot which is documented as thread-safe.
"""

import json
import threading
from pathlib import Path
from typing import Callable, List

from PySide6.QtCore import Qt, QTimer, Signal, QPoint
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QLineEdit,
    QFrame, QSizeGrip, QSlider, QMenu, QAbstractItemView,
)

# ---------------------------------------------------------------------------
# Colour palette — matches the Tkinter version
# ---------------------------------------------------------------------------

_BG       = "#0b0e14"
_SURFACE  = "#131820"
_ELEVATED = "#1a2030"
_ACCENT   = "#5cc4d4"
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI = "#e4e8ef"
_TEXT_SEC = "#7a8599"
_BORDER   = "#2a3345"

_DEFAULT_W = 440
_DEFAULT_H = 520


def _pack_label(pack_id: str) -> str:
    try:
        from samsara.command_packs import PACKS
        return PACKS.get(pack_id, {}).get("label", pack_id.replace("-", " ").title())
    except Exception:
        return pack_id.replace("-", " ").title()


_SS = f"""
QMainWindow, QWidget {{ background: {_BG}; color: {_TEXT_PRI}; font-family: 'Segoe UI', sans-serif; font-size: 12px; }}
QListWidget {{
    background: {_SURFACE};
    border: none;
    outline: none;
    color: {_TEXT_PRI};
    font-size: 12px;
}}
QListWidget::item {{ padding: 3px 8px; }}
QListWidget::item:hover {{ background: {_ELEVATED}; }}
QListWidget::item:selected {{ background: {_ACCENT_DIM}; color: {_ACCENT}; }}
QLineEdit {{
    background: {_SURFACE};
    border: none;
    color: {_TEXT_PRI};
    font-size: 12px;
    padding: 5px 8px;
}}
QScrollBar:vertical {{
    background: {_BG};
    width: 6px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSlider::groove:horizontal {{
    height: 3px;
    background: {_BORDER};
    border-radius: 1px;
}}
QSlider::handle:horizontal {{
    background: {_TEXT_SEC};
    width: 10px;
    height: 10px;
    margin: -3px 0;
    border-radius: 5px;
}}
QSlider::sub-page:horizontal {{ background: {_ACCENT}; border-radius: 1px; }}
QMenu {{ background: {_SURFACE}; color: {_TEXT_PRI}; border: 1px solid {_BORDER}; }}
QMenu::item:selected {{ background: {_ACCENT_DIM}; color: {_ACCENT}; }}
QComboBox {{
    background: {_SURFACE};
    color: {_TEXT_PRI};
    border: none;
    border-radius: 3px;
    padding: 3px 8px;
    font-size: 11px;
    min-width: 120px;
}}
QComboBox:hover {{ background: {_ELEVATED}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox::down-arrow {{
    width: 8px; height: 8px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {_TEXT_SEC};
}}
QComboBox QAbstractItemView {{
    background: {_SURFACE};
    color: {_TEXT_PRI};
    border: 1px solid {_BORDER};
    selection-background-color: {_ACCENT_DIM};
    selection-color: {_ACCENT};
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
        self._thread: "threading.Thread | None" = None
        self._visible = False

    # ----------------------------------------------------------------
    # Public API (callable from any thread)
    # ----------------------------------------------------------------

    def show(self):
        if self._window is not None:
            self._visible = True
            QTimer.singleShot(0, self._window.show)
            QTimer.singleShot(0, self._window.raise_)
        else:
            self._visible = True
            self._thread = threading.Thread(
                target=self._create, daemon=True, name="cheatsheet-qt"
            )
            self._thread.start()

    def hide(self):
        self._visible = False
        if self._window is not None:
            QTimer.singleShot(0, self._window.hide)

    def toggle(self):
        if self._visible:
            self.hide()
        else:
            self.show()

    def destroy(self):
        self._visible = False
        if self._window is not None:
            QTimer.singleShot(0, self._window.deleteLater)
            self._window = None

    def refresh(self):
        if self._window is not None and self._visible:
            QTimer.singleShot(0, self._window.refresh_commands)

    # ----------------------------------------------------------------
    # Thread
    # ----------------------------------------------------------------

    def _create(self):
        qt_app = QApplication.instance()
        if qt_app is None:
            qt_app = QApplication([])
            self._init_window()
            qt_app.exec()
            self._visible = False
            self._window = None
        else:
            QTimer.singleShot(0, qt_app, self._init_window)

    def _init_window(self):
        self._window = _CheatSheetWindow(
            self._execute_cb, self._commands_cb, self._palette_path
        )
        self._window.destroyed.connect(self._on_window_destroyed)
        self._window.show()

    def _on_window_destroyed(self):
        self._visible = False
        self._window = None


# ---------------------------------------------------------------------------
# Title bar (drag handle + opacity + close)
# ---------------------------------------------------------------------------

class _TitleBar(QWidget):
    def __init__(self, win: "_CheatSheetWindow"):
        super().__init__(win)
        self.setFixedHeight(34)
        self.setStyleSheet(f"background:{_SURFACE};")
        self._win = win
        self._drag_pos: QPoint | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 6, 0)
        lay.setSpacing(6)

        drag_lbl = QLabel("Command Reference")
        drag_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        drag_lbl.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        lay.addWidget(drag_lbl, stretch=1)

        op_lbl = QLabel("opacity")
        op_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:9px;")
        lay.addWidget(op_lbl)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(35, 100)
        self._opacity_slider.setValue(int(win.windowOpacity() * 100))
        self._opacity_slider.setFixedWidth(70)
        self._opacity_slider.setStyleSheet(_SS)
        self._opacity_slider.valueChanged.connect(
            lambda v: win.setWindowOpacity(v / 100.0)
        )
        self._opacity_slider.sliderReleased.connect(win._save_palette)
        lay.addWidget(self._opacity_slider)

        close_lbl = QLabel("  x  ")
        close_lbl.setStyleSheet(
            f"color:{_TEXT_SEC};font-size:12px;padding:2px 4px;"
        )
        close_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_lbl.mousePressEvent = lambda _e: win.hide()
        close_lbl.enterEvent  = lambda _e: close_lbl.setStyleSheet(
            "color:#e06060;font-size:12px;padding:2px 4px;")
        close_lbl.leaveEvent  = lambda _e: close_lbl.setStyleSheet(
            f"color:{_TEXT_SEC};font-size:12px;padding:2px 4px;")
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

        self.setFixedHeight(28)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(f"QFrame{{background:{_SURFACE};border:none;}}")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 8, 0)
        lay.setSpacing(0)

        self._pin_lbl = QLabel("*" if pinned else " ")
        self._pin_lbl.setFixedWidth(18)
        self._pin_lbl.setStyleSheet(
            f"color:{_ACCENT if pinned else _TEXT_SEC};font-size:11px;font-weight:bold;"
        )
        self._pin_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._pin_lbl.mousePressEvent = lambda _e: self._on_pin()
        lay.addWidget(self._pin_lbl)

        phrase_lbl = QLabel(cmd["phrase"].title())
        phrase_lbl.setStyleSheet(f"color:{_TEXT_PRI};font-size:12px;")
        lay.addWidget(phrase_lbl, stretch=1)

        right_text = str(count) if count is not None else (
            cmd.get("pack", "") if cmd.get("pack", "") not in ("", "core") else ""
        )
        if right_text:
            right_lbl = QLabel(right_text)
            right_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:10px;")
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
            self.setStyleSheet(f"QFrame{{background:{_ELEVATED};border:none;}}")
        self._pin_lbl.parentWidget()  # keep reference

    def leaveEvent(self, e):
        if not self._flashing:
            self.setStyleSheet(f"QFrame{{background:{_SURFACE};border:none;}}")

    def _flash(self):
        self._flashing = True
        self.setStyleSheet(f"QFrame{{background:{_ACCENT_DIM};border:none;}}")
        QTimer.singleShot(300, self._unflash)

    def _unflash(self):
        self._flashing = False
        self.setStyleSheet(f"QFrame{{background:{_SURFACE};border:none;}}")


# ---------------------------------------------------------------------------
# Category selector — compact dropdown replacing the old tab strip
# ---------------------------------------------------------------------------

class _CategoryTabBar(QWidget):
    """A single-row category picker using a QComboBox.

    Replaces the original horizontal tab strip, which became unreadable
    at the window's default width once enough command packs were registered.
    A dropdown takes exactly one line regardless of how many categories exist.
    """

    def __init__(self, on_select, parent=None):
        super().__init__(parent)
        self._on_select = on_select
        self._pack_ids: List[str] = []

        self.setFixedHeight(32)
        self.setStyleSheet(
            f"background:{_SURFACE};border-bottom:1px solid {_BORDER};"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(6)

        cat_lbl = QLabel("Category")
        cat_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:10px;")
        lay.addWidget(cat_lbl)

        self._combo = QComboBox()
        self._combo.setStyleSheet(_SS)
        self._combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self._combo.currentIndexChanged.connect(self._on_changed)
        lay.addWidget(self._combo)
        lay.addStretch()

    def set_categories(self, pack_ids: List[str], active_id: str):
        self._pack_ids = ["All"] + pack_ids
        self._combo.blockSignals(True)
        self._combo.clear()
        for pid in self._pack_ids:
            label = "All commands" if pid == "All" else _pack_label(pid)
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
        self.setMinimumSize(280, 180)
        self.setStyleSheet(_SS)

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
        outer.setStyleSheet(f"background:{_BORDER};")
        self.setCentralWidget(outer)
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(1, 1, 1, 1)
        outer_lay.setSpacing(0)

        inner = QWidget()
        inner.setStyleSheet(f"background:{_BG};")
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
            f"QLineEdit{{background:{_SURFACE};border:none;color:{_TEXT_PRI};"
            f"font-size:12px;padding:5px 10px;}}"
        )
        self._filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self._filter)

        _sep = lambda: [s := QFrame(), s.setFixedHeight(1),
                        s.setStyleSheet(f"background:{_BORDER};")][0]

        lay.addWidget(_sep())

        # Static pane (Most Used + Pinned) — rebuilt on refresh/pin change
        self._static_pane = QWidget()
        self._static_pane.setStyleSheet(f"background:{_BG};")
        self._static_layout = QVBoxLayout(self._static_pane)
        self._static_layout.setContentsMargins(0, 0, 0, 0)
        self._static_layout.setSpacing(0)
        lay.addWidget(self._static_pane)

        # Category tab bar
        self._category_bar = _CategoryTabBar(self._set_category)
        lay.addWidget(self._category_bar)

        # Command list
        self._list = QListWidget()
        self._list.setStyleSheet(_SS)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._list.setSpacing(0)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        lay.addWidget(self._list, stretch=1)

        # Resize grip row
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch()
        grip = QSizeGrip(inner)
        grip.setStyleSheet("background:transparent;")
        grip_row.addWidget(grip)
        lay.addLayout(grip_row)

        self.refresh_commands()

    # ----------------------------------------------------------------
    # Commands
    # ----------------------------------------------------------------

    def refresh_commands(self):
        try:
            self._all = list(self._commands_cb())
        except Exception as exc:
            print(f"[CHEATSHEET] commands_cb error: {exc}")
            self._all = []
        self._rebuild_static_pane()

        # Build ordered pack list from PACKS definition, only include packs
        # that have at least one command currently loaded.
        try:
            from samsara.command_packs import PACKS
            pack_order = list(PACKS.keys())
        except Exception:
            pack_order = []
        seen: set = set()
        pack_ids: List[str] = []
        for pid in pack_order:
            if any(c.get("pack", "") == pid for c in self._all):
                pack_ids.append(pid)
                seen.add(pid)
        for c in self._all:
            pid = c.get("pack", "")
            if pid and pid not in seen:
                pack_ids.append(pid)
                seen.add(pid)

        if self._active_category != "All" and self._active_category not in pack_ids:
            self._active_category = "All"

        self._category_bar.set_categories(pack_ids, self._active_category)
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

        # Category filter
        if self._active_category != "All":
            filtered = [
                c for c in filtered
                if c.get("pack", "core") == self._active_category
            ]

        # Pinned items live in static pane — exclude from scroll list
        unpinned = [c for c in filtered if c["phrase"] not in self._pinned]

        self._list.blockSignals(True)
        self._list.clear()
        for cmd in unpinned:
            phrase = cmd["phrase"]
            pack   = cmd.get("pack", "")
            item = QListWidgetItem()
            # Store phrase for execution
            item.setData(Qt.ItemDataRole.UserRole, phrase)
            # Format: "phrase" with pack hint right-aligned via spaces
            display = phrase.title()
            if pack and pack != "core":
                # Pad with spaces — approximate right-alignment
                item.setToolTip(f"Pack: {pack}")
            item.setText(display)
            item.setForeground(QColor(_TEXT_PRI))
            self._list.addItem(item)
        self._list.blockSignals(False)

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
        phrase_to_cmd = {c["phrase"]: c for c in self._all}

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
                    pinned=(phrase in self._pinned),
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
                sep.setStyleSheet(f"background:{_BORDER};")
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
            sep.setStyleSheet(f"background:{_BORDER};margin:2px 0;")
            self._static_layout.addWidget(sep)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{_TEXT_SEC};font-size:9px;font-weight:bold;"
            f"padding:3px 10px 2px 10px;background:{_BG};"
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
        item.setForeground(QColor(_ACCENT))
        item.setBackground(QColor(_ACCENT_DIM))
        def _restore():
            item.setForeground(orig)
            item.setBackground(QColor(0, 0, 0, 0))
        QTimer.singleShot(300, _restore)
        self._execute(phrase)

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
        except Exception:
            pass

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
        e.accept()

    def hideEvent(self, e):
        self._save_palette()
        e.accept()
