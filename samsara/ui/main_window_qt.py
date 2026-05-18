"""PySide6 main hub window for Samsara.

Drop-in replacement for MainWindow with the same public API:
    show() / hide() / close() / on_dictation_complete(text)

Layout:
    +--------------------------------------------------+
    | Samsara                          [status badge]  |
    +----------+---------------------------------------+
    | History  |                                       |
    | Dictionary  (QStackedWidget content area)        |
    | Settings |                                       |
    +----------+---------------------------------------+
    | mode: X  wake: Y  mic: Z         Last: preview  |
    +--------------------------------------------------+

Settings nav item opens the Qt settings window via app.open_settings().
History and Dictionary are embedded QWidget panels.
Close button hides to tray (closeEvent suppressed); app.close() force-closes.
"""

import threading
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QPlainTextEdit, QPushButton,
    QSizePolicy, QStackedWidget, QStatusBar, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Constants — match Tkinter version
# ---------------------------------------------------------------------------

DEFAULT_WIDTH  = 900
DEFAULT_HEIGHT = 650
MIN_WIDTH      = 700
MIN_HEIGHT     = 500
STATUS_POLL_MS = 2000
PREVIEW_CHARS  = 40
SIDEBAR_W      = 180
HISTORY_LIMIT  = 500

_BG       = "#0b0e14"
_SURFACE  = "#131820"
_ELEVATED = "#1a2030"
_BORDER   = "#2a3345"
_ACCENT   = "#5cc4d4"
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI = "#e4e8ef"
_TEXT_SEC = "#7a8599"
_TEXT_DIS = "#4a5568"
_SUCCESS  = "#6ee7a0"
_ERROR    = "#f87171"
_WARNING  = "#fbbf24"

_SS = f"""
QMainWindow, QWidget {{
    background: {_BG};
    color: {_TEXT_PRI};
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QTableWidget {{
    background: {_SURFACE};
    border: none;
    gridline-color: {_BORDER};
    outline: none;
    color: {_TEXT_PRI};
}}
QTableWidget::item {{ padding: 3px 6px; }}
QTableWidget::item:selected {{
    background: {_ACCENT_DIM};
    color: {_ACCENT};
}}
QHeaderView::section {{
    background: {_SURFACE};
    color: {_TEXT_SEC};
    border: none;
    border-bottom: 1px solid {_BORDER};
    padding: 4px 6px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
}}
QPlainTextEdit {{
    background: {_SURFACE};
    border: none;
    border-top: 1px solid {_BORDER};
    color: {_TEXT_PRI};
    font-family: 'Consolas', monospace;
    font-size: 12px;
    padding: 6px 8px;
}}
QLineEdit {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 5px 8px;
    font-size: 12px;
}}
QLineEdit:focus {{ border-color: {_ACCENT}; }}
QComboBox {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 5px 8px;
    font-size: 12px;
    min-width: 110px;
}}
QComboBox::drop-down {{ border: none; }}
QComboBox QAbstractItemView {{
    background: {_ELEVATED};
    border: 1px solid {_BORDER};
    color: {_TEXT_PRI};
    selection-background-color: {_ACCENT_DIM};
}}
QPushButton {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 5px 14px;
    font-size: 12px;
}}
QPushButton:hover {{ background: {_ELEVATED}; border-color: {_ACCENT}; }}
QPushButton:pressed {{ background: {_ACCENT_DIM}; }}
QListWidget {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    outline: none;
    color: {_TEXT_PRI};
}}
QListWidget::item {{ padding: 5px 8px; }}
QListWidget::item:selected {{ background: {_ACCENT_DIM}; color: {_ACCENT}; }}
QTabWidget::pane {{
    border: none;
    background: {_BG};
}}
QTabBar::tab {{
    background: {_SURFACE};
    color: {_TEXT_SEC};
    padding: 6px 16px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 2px;
    font-size: 12px;
}}
QTabBar::tab:selected {{
    color: {_ACCENT};
    border-bottom-color: {_ACCENT};
    background: {_BG};
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
QStatusBar {{
    background: {_SURFACE};
    border-top: 1px solid {_BORDER};
    color: {_TEXT_SEC};
    font-size: 11px;
}}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _btn(text, *, accent=False):
    b = QPushButton(text)
    if accent:
        b.setStyleSheet(
            f"background: {_ACCENT_DIM}; color: {_ACCENT};"
            f" border-color: {_ACCENT}; border-radius: 4px;"
            f" padding: 5px 14px;"
        )
    return b


def _label(text, color=_TEXT_SEC, size=11, bold=False):
    lbl = QLabel(text)
    weight = "600" if bold else "400"
    lbl.setStyleSheet(
        f"color: {color}; font-size: {size}px; font-weight: {weight};"
        " background: transparent;"
    )
    return lbl


# ---------------------------------------------------------------------------
# History panel
# ---------------------------------------------------------------------------

class _HistoryPanel(QWidget):
    """Embedded history viewer. Mirrors history_qt.py but as a panel widget."""

    _COLORS = {
        "command":      QColor("#5EEAD4"),
        "wake_command": QColor("#5EEAD4"),
        "failed":       QColor("#FF6666"),
    }

    # Signal to marshal DB results back onto the Qt thread before
    # touching any widgets — background threads must not call _populate
    # directly or Qt silently drops the updates.
    _rows_ready = Signal(list)

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app = app
        self._rows = []
        self._setup_ui()
        self._rows_ready.connect(self._on_rows_ready)
        QTimer.singleShot(0, self._load)

    # ---- UI -----------------------------------------------------------------

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Toolbar
        bar = QWidget()
        bar.setStyleSheet(f"background: {_BG};")
        blay = QHBoxLayout(bar)
        blay.setContentsMargins(0, 0, 0, 8)
        blay.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search...")
        self._search.textChanged.connect(self._apply_filter)

        self._type_combo = QComboBox()
        self._type_combo.addItems(["All types", "command", "dictation", "failed"])
        self._type_combo.currentTextChanged.connect(self._apply_filter)

        self._refresh_btn = _btn("Refresh")
        self._copy_btn    = _btn("Copy")
        self._delete_btn  = _btn("Delete")

        self._refresh_btn.clicked.connect(self._load)
        self._copy_btn.clicked.connect(self._copy)
        self._delete_btn.clicked.connect(self._delete)

        blay.addWidget(self._search, stretch=1)
        blay.addWidget(self._type_combo)
        blay.addWidget(self._refresh_btn)
        blay.addWidget(self._copy_btn)
        blay.addWidget(self._delete_btn)
        root.addWidget(bar)

        # Table
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Time", "Type", "Text"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(False)
        self._table.itemSelectionChanged.connect(self._on_select)
        root.addWidget(self._table, stretch=1)

        # Detail pane
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setFixedHeight(64)
        self._detail.setFont(QFont("Consolas", 11))
        root.addWidget(self._detail)

    # ---- Data ---------------------------------------------------------------

    def _load(self):
        def _fetch():
            db = getattr(self._app, 'history_db', None)
            if db is None:
                return []
            try:
                return db.get_recent(HISTORY_LIMIT)
            except Exception:
                return []

        threading.Thread(
            target=lambda: self._rows_ready.emit(_fetch()),
            daemon=True, name="history-panel-load",
        ).start()

    @Slot(list)
    def _on_rows_ready(self, rows: list):
        self._rows = rows
        self._apply_filter()

    def _apply_filter(self):
        q    = self._search.text().lower()
        filt = self._type_combo.currentText()
        rows = self._rows
        if filt != "All types":
            rows = [r for r in rows if r.get("type", "") == filt]
        if q:
            rows = [r for r in rows
                    if q in (r.get("text") or "").lower()
                    or q in (r.get("type") or "").lower()]
        self._populate(rows)

    def _populate(self, rows):
        self._table.setRowCount(0)
        for row in rows:
            r = self._table.rowCount()
            self._table.insertRow(r)

            ts = row.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts).strftime("%H:%M:%S")
            except Exception:
                pass

            rtype = row.get("type", "")
            text  = row.get("text", "")

            t_item = QTableWidgetItem(ts)
            y_item = QTableWidgetItem(rtype)
            x_item = QTableWidgetItem(text)

            color = self._COLORS.get(rtype)
            if color:
                for item in (t_item, y_item, x_item):
                    item.setForeground(color)

            t_item.setData(Qt.UserRole, row.get("id"))
            x_item.setData(Qt.UserRole, text)

            self._table.setItem(r, 0, t_item)
            self._table.setItem(r, 1, y_item)
            self._table.setItem(r, 2, x_item)

        self._table.setRowHeight(0, 26) if self._table.rowCount() else None

    def _on_select(self):
        rows = self._table.selectedItems()
        if not rows:
            self._detail.setPlainText("")
            return
        row = self._table.currentRow()
        item = self._table.item(row, 2)
        if item:
            self._detail.setPlainText(item.data(Qt.UserRole) or "")

    def _copy(self):
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, 2)
        if item:
            QApplication.clipboard().setText(item.data(Qt.UserRole) or "")

    def _delete(self):
        row = self._table.currentRow()
        if row < 0:
            return
        id_item = self._table.item(row, 0)
        if id_item is None:
            return
        row_id = id_item.data(Qt.UserRole)
        db = getattr(self._app, 'history_db', None)
        if db and row_id is not None:
            try:
                db.delete(row_id)
            except Exception:
                pass
        self._table.removeRow(row)

    # ---- Public -------------------------------------------------------------

    def on_new_entry(self):
        self._load()

    def refresh(self):
        self._load()


# ---------------------------------------------------------------------------
# Dictionary panel
# ---------------------------------------------------------------------------

class _DictionaryPanel(QWidget):
    """Vocabulary / Corrections / Wake Words editor."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app = app
        self._setup_ui()

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        tabs = QTabWidget()
        lay.addWidget(tabs)

        tabs.addTab(self._make_vocab_tab(),   "Vocabulary")
        tabs.addTab(self._make_kv_tab("corrections"), "Corrections")
        tabs.addTab(self._make_kv_tab("wake"),        "Wake Words")

    # ---- Vocabulary tab -----------------------------------------------------

    def _make_vocab_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        lay.addWidget(_label(
            "Words and phrases Whisper will recognise. One per line.",
            color=_TEXT_SEC, size=12,
        ))

        self._vocab_list = QListWidget()
        lay.addWidget(self._vocab_list, stretch=1)
        self._load_vocab()

        row = QHBoxLayout()
        self._vocab_input = QLineEdit()
        self._vocab_input.setPlaceholderText("Add word or phrase...")
        self._vocab_input.returnPressed.connect(self._vocab_add)
        add_btn = _btn("Add")
        add_btn.clicked.connect(self._vocab_add)
        rem_btn = _btn("Remove")
        rem_btn.clicked.connect(self._vocab_remove)
        save_btn = _btn("Save", accent=True)
        save_btn.clicked.connect(self._vocab_save)
        row.addWidget(self._vocab_input, stretch=1)
        row.addWidget(add_btn)
        row.addWidget(rem_btn)
        row.addWidget(save_btn)
        lay.addLayout(row)
        return w

    def _load_vocab(self):
        self._vocab_list.clear()
        vt = getattr(self._app, 'voice_training_window', None)
        vocab = vt.custom_vocab if vt is not None else []
        for word in vocab:
            self._vocab_list.addItem(str(word))

    def _vocab_add(self):
        text = self._vocab_input.text().strip()
        if not text:
            return
        self._vocab_list.addItem(text)
        self._vocab_input.clear()

    def _vocab_remove(self):
        for item in self._vocab_list.selectedItems():
            self._vocab_list.takeItem(self._vocab_list.row(item))

    def _vocab_save(self):
        words = [self._vocab_list.item(i).text()
                 for i in range(self._vocab_list.count())]
        vt = getattr(self._app, 'voice_training_window', None)
        if vt is not None:
            vt.custom_vocab = words
            try:
                vt.save_training_data()
            except Exception as e:
                print(f"[DICT] Vocab save error: {e}")

    # ---- Key-value tabs (Corrections + Wake Words) --------------------------

    def _make_kv_tab(self, mode: str):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        desc = ("Phonetic overrides: heard phrase -> intended text."
                if mode == "corrections"
                else "Wake word misrecognitions: heard -> correct phrase.")
        lay.addWidget(_label(desc, color=_TEXT_SEC, size=12))

        lst = QListWidget()
        lay.addWidget(lst, stretch=1)

        input_row = QHBoxLayout()
        heard = QLineEdit()
        heard.setPlaceholderText("Heard...")
        intended = QLineEdit()
        intended.setPlaceholderText("Intended...")
        add_btn  = _btn("Add")
        rem_btn  = _btn("Remove")
        save_btn = _btn("Save", accent=True)
        input_row.addWidget(heard, stretch=1)
        input_row.addWidget(QLabel("->"))
        input_row.addWidget(intended, stretch=1)
        input_row.addWidget(add_btn)
        input_row.addWidget(rem_btn)
        input_row.addWidget(save_btn)
        lay.addLayout(input_row)

        if mode == "corrections":
            import samsara.phonetic_wash as _mod
        else:
            import samsara.wake_corrections as _mod

        def _load():
            lst.clear()
            for k, v in (_mod.get_user_corrections() or {}).items():
                lst.addItem(f"{k}  ->  {v}")

        def _add():
            h = heard.text().strip()
            i = intended.text().strip()
            if h and i:
                lst.addItem(f"{h}  ->  {i}")
                heard.clear()
                intended.clear()

        def _remove():
            for item in lst.selectedItems():
                lst.takeItem(lst.row(item))

        def _save():
            corrections = {}
            for idx in range(lst.count()):
                parts = lst.item(idx).text().split("  ->  ", 1)
                if len(parts) == 2:
                    corrections[parts[0].strip()] = parts[1].strip()
            try:
                _mod.set_user_corrections(corrections)
                _mod.reload_corrections()
            except Exception as e:
                print(f"[DICT] Save error ({mode}): {e}")

        _load()
        add_btn.clicked.connect(_add)
        rem_btn.clicked.connect(_remove)
        save_btn.clicked.connect(_save)
        heard.returnPressed.connect(_add)
        return w


# ---------------------------------------------------------------------------
# Main Qt window
# ---------------------------------------------------------------------------

class _MainWindow(QMainWindow):
    _dictation_sig = Signal(str)

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._force_close = False
        self._panel_cache = {}
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(STATUS_POLL_MS)
        self._poll_timer.timeout.connect(self._refresh_status)
        self._geom_timer = QTimer(self)
        self._geom_timer.setSingleShot(True)
        self._geom_timer.setInterval(800)
        self._geom_timer.timeout.connect(self._save_geometry)

        self.setWindowTitle("Samsara")
        self.setStyleSheet(_SS)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self._restore_geometry()
        self._build_ui()
        self._activate("History")
        self._poll_timer.start()
        self._dictation_sig.connect(self._on_dictation)

    # ---- Layout -------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(52)
        header.setStyleSheet(f"background: {_BG}; border-bottom: 1px solid {_BORDER};")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(20, 0, 20, 0)
        title = QLabel("Samsara")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        title.setStyleSheet(f"color: {_TEXT_PRI};")
        self._badge = QLabel("ready")
        self._badge.setStyleSheet(f"color: {_TEXT_SEC}; font-size: 11px;")
        hlay.addWidget(title)
        hlay.addStretch()
        hlay.addWidget(self._badge)
        outer.addWidget(header)

        # Body: sidebar + content
        body = QWidget()
        blay = QHBoxLayout(body)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(0)

        # Sidebar
        sidebar = QWidget()
        sidebar.setFixedWidth(SIDEBAR_W)
        sidebar.setStyleSheet(
            f"background: {_SURFACE}; border-right: 1px solid {_BORDER};")
        slay = QVBoxLayout(sidebar)
        slay.setContentsMargins(0, 12, 0, 12)
        slay.setSpacing(2)

        self._nav_btns = {}
        for name in ("History", "Dictionary", "Settings"):
            btn = QPushButton(name)
            btn.setFixedHeight(44)
            btn.setCheckable(True)
            btn.setStyleSheet(self._nav_style(False))
            btn.clicked.connect(lambda _, n=name: self._activate(n))
            slay.addWidget(btn)
            self._nav_btns[name] = btn
        slay.addStretch()

        blay.addWidget(sidebar)

        # Content stack
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background: {_BG};")
        blay.addWidget(self._stack, stretch=1)
        outer.addWidget(body, stretch=1)

        # Status bar
        sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        self.setStatusBar(sb)
        self._lbl_mode  = QLabel("mode: ...")
        self._lbl_wake  = QLabel("wake: ...")
        self._lbl_mic   = QLabel("mic: ...")
        self._lbl_prev  = QLabel("")
        self._lbl_prev.setAlignment(Qt.AlignRight)
        for lbl in (self._lbl_mode, self._lbl_wake, self._lbl_mic, self._lbl_prev):
            lbl.setStyleSheet(f"color: {_TEXT_SEC}; font-size: 11px; padding: 0 6px;")
        sb.addWidget(self._lbl_mode)
        sb.addWidget(self._lbl_wake)
        sb.addWidget(self._lbl_mic)
        sb.addPermanentWidget(self._lbl_prev)

    @staticmethod
    def _nav_style(active: bool) -> str:
        if active:
            return (f"background: {_ACCENT_DIM}; color: {_ACCENT};"
                    f" border: none; border-left: 3px solid {_ACCENT};"
                    f" text-align: left; padding-left: 14px;"
                    f" font-size: 13px; font-weight: 600;"
                    f" border-radius: 0;")
        return (f"background: transparent; color: {_TEXT_SEC};"
                f" border: none; border-left: 3px solid transparent;"
                f" text-align: left; padding-left: 14px;"
                f" font-size: 13px; font-weight: 600;"
                f" border-radius: 0;")

    # ---- Navigation ---------------------------------------------------------

    def _activate(self, name: str):
        if name == "Settings":
            try:
                self._app.open_settings()
            except Exception as e:
                print(f"[MAIN] open_settings error: {e}")
            self._highlight(name)
            return

        if name not in self._panel_cache:
            panel = self._make_panel(name)
            if panel is None:
                return
            self._panel_cache[name] = panel
            self._stack.addWidget(panel)

        self._stack.setCurrentWidget(self._panel_cache[name])
        self._highlight(name)

    def _make_panel(self, name: str):
        if name == "History":
            return _HistoryPanel(self._app)
        if name == "Dictionary":
            return _DictionaryPanel(self._app)
        return None

    def _highlight(self, active: str):
        for name, btn in self._nav_btns.items():
            btn.setChecked(name == active)
            btn.setStyleSheet(self._nav_style(name == active))

    # ---- Status -------------------------------------------------------------

    def _refresh_status(self):
        cfg = getattr(self._app, 'config', {}) or {}

        mode = cfg.get('mode', 'hold').title()
        self._lbl_mode.setText(f"mode: {mode}")

        wake_on = cfg.get('wake_word_enabled', False)
        phrase  = cfg.get('wake_word_config', {}).get('phrase', 'samsara')
        self._lbl_wake.setText(
            f"wake: {phrase} (on)" if wake_on else "wake: off")

        mic_id   = cfg.get('microphone')
        mic_name = "default"
        for m in getattr(self._app, 'available_mics', []) or []:
            if m.get('id') == mic_id:
                mic_name = m.get('name', 'default')
                break
        if len(mic_name) > 36:
            mic_name = mic_name[:35] + '...'
        self._lbl_mic.setText(f"mic: {mic_name}")

        if getattr(self._app, 'snoozed', False):
            self._badge.setText("snoozed")
            self._badge.setStyleSheet(f"color: {_WARNING}; font-size: 11px;")
        elif getattr(self._app, 'recording', False):
            self._badge.setText("recording")
            self._badge.setStyleSheet(f"color: {_ERROR}; font-size: 11px;")
        elif (getattr(self._app, 'continuous_active', False) or
              getattr(self._app, 'wake_word_active', False)):
            self._badge.setText("listening")
            self._badge.setStyleSheet(f"color: {_SUCCESS}; font-size: 11px;")
        else:
            self._badge.setText("ready")
            self._badge.setStyleSheet(f"color: {_TEXT_SEC}; font-size: 11px;")

    @Slot(str)
    def _on_dictation(self, text: str):
        preview = text.replace('\n', ' ').strip()
        if len(preview) > PREVIEW_CHARS:
            preview = preview[:PREVIEW_CHARS - 1] + '...'
        self._lbl_prev.setText(f"Last: {preview}" if preview else "")

        panel = self._panel_cache.get("History")
        if panel is not None and self._stack.currentWidget() is panel:
            try:
                panel.on_new_entry()
            except Exception:
                pass

    # ---- Geometry -----------------------------------------------------------

    def _restore_geometry(self):
        cfg = getattr(self._app, 'config', {}) or {}
        w = max(MIN_WIDTH,  int(cfg.get('window_width',  DEFAULT_WIDTH)  or DEFAULT_WIDTH))
        h = max(MIN_HEIGHT, int(cfg.get('window_height', DEFAULT_HEIGHT) or DEFAULT_HEIGHT))
        x = cfg.get('window_x')
        y = cfg.get('window_y')
        if x is not None and y is not None:
            try:
                screen = QApplication.primaryScreen().geometry()
                x = max(0, min(int(x), screen.width()  - 100))
                y = max(0, min(int(y), screen.height() - 100))
                self.setGeometry(x, y, w, h)
                return
            except Exception:
                pass
        self.resize(w, h)

    def _save_geometry(self):
        try:
            g = self.geometry()
            changes = {
                'window_width':  g.width(),
                'window_height': g.height(),
                'window_x':      g.x(),
                'window_y':      g.y(),
            }
            if hasattr(self._app, 'update_config'):
                self._app.update_config(changes)
        except Exception as e:
            print(f"[MAIN] geometry save error: {e}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._geom_timer.start()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._geom_timer.start()

    # ---- Close / hide -------------------------------------------------------

    def closeEvent(self, e):
        if self._force_close:
            self._poll_timer.stop()
            self._save_geometry()
            e.accept()
        else:
            # Minimize to tray — the tray icon is the lifecycle owner.
            self.hide()
            e.ignore()

    def force_close(self):
        self._force_close = True
        self.close()


# ---------------------------------------------------------------------------
# Public wrapper — same API as the Tkinter MainWindow
# ---------------------------------------------------------------------------

class MainWindowQt:
    """Drop-in Qt replacement for MainWindow."""

    def __init__(self, app):
        self._app    = app
        self._window: "_MainWindow | None" = None
        self._thread: "threading.Thread | None" = None

    # ---- Public API (callable from any thread) ------------------------------

    def show(self):
        if self._window is not None:
            QTimer.singleShot(0, self._window.show)
            QTimer.singleShot(0, self._window.raise_)
            QTimer.singleShot(0, self._window.activateWindow)
        else:
            self._thread = threading.Thread(
                target=self._create, daemon=True, name="main-window-qt",
            )
            self._thread.start()

    def hide(self):
        if self._window is not None:
            QTimer.singleShot(0, self._window.hide)

    def close(self):
        if self._window is not None:
            QTimer.singleShot(0, self._window.force_close)
            self._window = None

    def on_dictation_complete(self, text: str):
        if self._window is not None:
            self._window._dictation_sig.emit(text)

    # ---- Thread -------------------------------------------------------------

    def _create(self):
        qt_app = QApplication.instance()
        owns_app = qt_app is None
        if qt_app is None:
            qt_app = QApplication([])
        if owns_app:
            self._window = _MainWindow(self._app)
            self._window.destroyed.connect(self._on_destroyed)
            self._window.show()
            qt_app.exec()
            self._window = None
        else:
            QTimer.singleShot(0, qt_app, self._init_window)

    def _init_window(self):
        self._window = _MainWindow(self._app)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()

    def _on_destroyed(self):
        self._window = None
