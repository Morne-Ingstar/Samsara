"""Qt Dictionary panel — Vocabulary / Corrections / Wake Words.

Drop-in Qt replacement for samsara.ui.dictionary_frame.DictionaryFrame.

Three sub-tabs:
  Vocabulary   — words injected into Whisper's initial_prompt
  Corrections  — phonetic-wash overrides (samsara.phonetic_wash)
  Wake Words   — wake-phrase misrecognition map (samsara.wake_corrections)

Data is read/written through the same service layer as the CTk version;
no file or DB access happens directly here.

Saves are asynchronous (background thread) with a Signal-based status
label update so the Qt event loop is never blocked.
"""

import logging

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colours — matches the main Qt window palette
# ---------------------------------------------------------------------------

_BG       = "#0b0e14"
_SURFACE  = "#131820"
_ELEVATED = "#1a2030"
_BORDER   = "#2a3345"
_ACCENT   = "#5cc4d4"
_TEXT_PRI = "#e4e8ef"
_TEXT_SEC = "#7a8599"
_SUCCESS  = "#6ee7a0"
_ERROR    = "#f87171"
_MUTED    = "#4a5568"

_SS = f"""
QWidget {{ background:{_BG}; color:{_TEXT_PRI};
          font-family:'Segoe UI',sans-serif; font-size:13px; }}
QTabWidget::pane {{ border:1px solid {_BORDER}; background:{_BG}; }}
QTabBar::tab {{ background:{_SURFACE}; color:{_TEXT_SEC};
                padding:5px 14px; border:none; margin-right:2px; }}
QTabBar::tab:selected {{ background:{_ELEVATED}; color:{_ACCENT};
                         border-bottom:2px solid {_ACCENT}; }}
QTabBar::tab:hover:!selected {{ color:{_TEXT_PRI}; }}
QListWidget, QTableWidget {{
    background:{_SURFACE}; border:1px solid {_BORDER};
    color:{_TEXT_PRI}; outline:none; gridline-color:{_BORDER};
}}
QListWidget::item {{ padding:3px 6px; }}
QListWidget::item:selected {{ background:{_ACCENT}; color:{_BG}; }}
QTableWidget::item {{ padding:3px 6px; }}
QTableWidget::item:selected {{ background:{_ACCENT}; color:{_BG}; }}
QHeaderView::section {{
    background:{_ELEVATED}; color:{_TEXT_SEC}; border:none;
    border-right:1px solid {_BORDER}; padding:4px 8px;
    font-size:11px; font-weight:bold;
}}
QLineEdit {{
    background:{_SURFACE}; border:1px solid {_BORDER};
    color:{_TEXT_PRI}; padding:4px 8px; border-radius:4px;
}}
QLineEdit:focus {{ border-color:{_ACCENT}; }}
QPushButton {{
    background:{_ELEVATED}; color:{_TEXT_PRI};
    border:1px solid {_BORDER}; padding:4px 12px; border-radius:4px;
}}
QPushButton:hover {{ background:{_ACCENT}; color:{_BG}; border-color:{_ACCENT}; }}
QPushButton#danger {{ color:{_ERROR}; border-color:{_ERROR}; }}
QPushButton#danger:hover {{ background:{_ERROR}; color:{_BG}; }}
QScrollBar:vertical {{ background:{_BG}; width:6px; border:none; }}
QScrollBar::handle:vertical {{ background:{_BORDER}; border-radius:3px; min-height:20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
"""


# ---------------------------------------------------------------------------
# Internal worker signals
# ---------------------------------------------------------------------------

class _Signals(QObject):
    status = Signal(str, str)   # (message, color)


# ---------------------------------------------------------------------------
# Public panel
# ---------------------------------------------------------------------------

class DictionaryPanelQt(QWidget):
    """Three-tab dictionary editor.  Embed directly in any Qt layout."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app    = app
        self._alive  = True
        self._sigs   = _Signals()
        self.setStyleSheet(_SS)
        self._build_ui()

    def closeEvent(self, e):
        self._alive = False
        e.accept()

    # ------------------------------------------------------------------
    # Service accessors (no direct file I/O)
    # ------------------------------------------------------------------

    @property
    def _vt(self):
        return getattr(self._app, 'voice_training_window', None)

    @property
    def _custom_vocab(self):
        vt = self._vt
        return vt.custom_vocab if vt is not None else []

    def _save_vocab(self):
        vt = self._vt
        if vt is not None:
            try:
                vt.save_training_data()
            except Exception as exc:
                logger.error(f"[DICT] Vocab save failed: {exc}", exc_info=True)

    # ------------------------------------------------------------------
    # Top-level layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._tabs = QTabWidget()
        lay.addWidget(self._tabs)

        self._tabs.addTab(self._build_vocab_tab(),       "Vocabulary")
        self._tabs.addTab(self._build_kv_tab("corrections"), "Corrections")
        self._tabs.addTab(self._build_kv_tab("wake"),        "Wake Words")

    # ------------------------------------------------------------------
    # Vocabulary tab
    # ------------------------------------------------------------------

    def _build_vocab_tab(self) -> QWidget:
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(8)

        desc = QLabel(
            "Words injected into Whisper's initial_prompt.  Add proper nouns, "
            "technical terms, or anything Whisper consistently mishears.  "
            "Changes take effect on the next dictation — no restart needed."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
        lay.addWidget(desc)

        # Input row
        add_row = QHBoxLayout()
        self._vocab_input = QLineEdit()
        self._vocab_input.setPlaceholderText("Word or phrase to add...")
        self._vocab_input.returnPressed.connect(self._vocab_add)
        add_row.addWidget(self._vocab_input, stretch=1)
        add_btn = QPushButton("Add")
        add_btn.setFixedWidth(60)
        add_btn.clicked.connect(self._vocab_add)
        add_row.addWidget(add_btn)
        lay.addLayout(add_row)

        # List
        self._vocab_list = QListWidget()
        self._vocab_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        for word in self._custom_vocab:
            self._vocab_list.addItem(word)
        lay.addWidget(self._vocab_list, stretch=1)

        # Button row
        btn_row = QHBoxLayout()
        rem_btn = QPushButton("Remove Selected")
        rem_btn.clicked.connect(self._vocab_remove)
        imp_btn = QPushButton("Import JSON")
        imp_btn.clicked.connect(self._vocab_import)
        exp_btn = QPushButton("Export JSON")
        exp_btn.clicked.connect(self._vocab_export)
        btn_row.addWidget(rem_btn)
        btn_row.addWidget(imp_btn)
        btn_row.addWidget(exp_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        # Status label
        self._vocab_status = QLabel("")
        self._vocab_status.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        lay.addWidget(self._vocab_status)
        self._sigs.status.connect(
            lambda msg, col, w=self._vocab_status:
                w.setText(msg) or w.setStyleSheet(f"color:{col};font-size:11px;")
        )

        return page

    def _vocab_add(self):
        word = self._vocab_input.text().strip()
        if not word:
            return
        vocab = self._custom_vocab
        if word in vocab:
            self._vocab_status.setText(f'"{word}" is already in the list.')
            return
        vocab.append(word)
        self._vocab_list.addItem(word)
        self._vocab_input.clear()
        self._vocab_status.setText("")
        thread_registry.spawn("dictionary_panel_qt._save_vocab", self._save_vocab, daemon=True)

    def _vocab_remove(self):
        rows = sorted(
            {self._vocab_list.row(i) for i in self._vocab_list.selectedItems()},
            reverse=True
        )
        if not rows:
            return
        vocab = self._custom_vocab
        for row in rows:
            word = self._vocab_list.item(row).text()
            self._vocab_list.takeItem(row)
            if word in vocab:
                vocab.remove(word)
        thread_registry.spawn("dictionary_panel_qt._save_vocab", self._save_vocab, daemon=True)

    def _vocab_export(self):
        import json
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Vocabulary", "samsara-vocabulary.json",
            "JSON files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({"vocabulary": list(self._custom_vocab)}, f, indent=2)
            self._vocab_status.setText(
                f"Exported {len(self._custom_vocab)} words to {path}"
            )
            self._vocab_status.setStyleSheet(
                f"color:{_SUCCESS};font-size:11px;"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def _vocab_import(self):
        import json
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Vocabulary", "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            words = data.get('vocabulary') if isinstance(data, dict) else data
            if not isinstance(words, list):
                QMessageBox.warning(
                    self, "Import failed",
                    "Expected JSON with a 'vocabulary' list of strings."
                )
                return
            vocab = self._custom_vocab
            added = 0
            for w in words:
                w = str(w).strip()
                if w and w not in vocab:
                    vocab.append(w)
                    self._vocab_list.addItem(w)
                    added += 1
            thread_registry.spawn("dictionary_panel_qt._save_vocab", self._save_vocab, daemon=True)
            self._vocab_status.setText(
                f"Imported {added} new word(s) — skipped duplicates."
            )
            self._vocab_status.setStyleSheet(f"color:{_SUCCESS};font-size:11px;")
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))

    # ------------------------------------------------------------------
    # Corrections / Wake Words tabs (shared factory)
    # ------------------------------------------------------------------

    def _build_kv_tab(self, mode: str) -> QWidget:
        """Build a two-column key-value editor tab.

        mode='corrections' -> phonetic_wash
        mode='wake'        -> wake_corrections
        """
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(8)

        if mode == "corrections":
            desc_text = (
                'Fixes Whisper misrecognitions in command phrases '
                '(e.g. "open crow" -> "open chrome").  '
                'User entries override built-in defaults.'
            )
            col1_hdr = "Heard"
            col2_hdr = "Should be"
            ph1, ph2 = "Whisper says...", "You mean..."
        else:
            desc_text = (
                "Maps Whisper misrecognitions of your wake phrase back to its "
                "canonical form.  Token-level: 'charvis' anywhere in the "
                "transcription becomes 'jarvis'."
            )
            col1_hdr = "Heard"
            col2_hdr = "Wake word"
            ph1, ph2 = "Heard as...", "Correct phrase..."

        desc = QLabel(desc_text)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;")
        lay.addWidget(desc)

        # Input row
        inp_row = QHBoxLayout()
        inp_row.setSpacing(6)
        field1 = QLineEdit()
        field1.setPlaceholderText(ph1)
        arrow = QLabel("->")
        arrow.setStyleSheet(f"color:{_TEXT_SEC};padding:0 4px;")
        field2 = QLineEdit()
        field2.setPlaceholderText(ph2)
        add_btn = QPushButton("Add")
        add_btn.setFixedWidth(56)
        inp_row.addWidget(field1, stretch=1)
        inp_row.addWidget(arrow)
        inp_row.addWidget(field2, stretch=1)
        inp_row.addWidget(add_btn)
        lay.addLayout(inp_row)

        # Table: Heard | Value | Source
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels([col1_hdr, col2_hdr, "Source"])
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(2, 72)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        lay.addWidget(table, stretch=1)

        # Button row
        btn_row = QHBoxLayout()
        rem_btn = QPushButton("Remove Selected  (user only)")
        rem_btn.setObjectName("danger")
        btn_row.addWidget(rem_btn)
        btn_row.addStretch()
        note = QLabel("Default entries are read-only.")
        note.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        btn_row.addWidget(note)
        lay.addLayout(btn_row)

        # Status
        status_lbl = QLabel("")
        status_lbl.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        lay.addWidget(status_lbl)

        # Load data and wire up
        self._kv_load(table, mode)
        field2.returnPressed.connect(
            lambda: self._kv_add(table, field1, field2, mode, status_lbl)
        )
        add_btn.clicked.connect(
            lambda: self._kv_add(table, field1, field2, mode, status_lbl)
        )
        rem_btn.clicked.connect(
            lambda: self._kv_remove(table, mode, status_lbl)
        )

        return page

    def _kv_module(self, mode: str):
        if mode == "corrections":
            from samsara import phonetic_wash as _m
        else:
            from samsara import wake_corrections as _m
        return _m

    def _kv_load(self, table: QTableWidget, mode: str):
        """Populate table with user entries (editable) then defaults (dimmed)."""
        table.setRowCount(0)
        try:
            mod = self._kv_module(mode)
            user = mod.get_user_corrections() or {}

            if mode == "corrections":
                phrase_def = mod.get_default_phrase_corrections() or {}
                word_def   = mod.get_default_word_corrections()   or {}
                defaults   = {**phrase_def, **word_def}
            else:
                defaults = mod.get_default_corrections() or {}

            for k in sorted(user):
                self._kv_insert_row(table, k, user[k], "user")
            for k in sorted(defaults):
                if k not in user:
                    self._kv_insert_row(table, k, defaults[k], "default")
        except Exception as exc:
            logger.error(f"[DICT] Load {mode} failed: {exc}", exc_info=True)

    def _kv_insert_row(self, table: QTableWidget, key: str, val: str, src: str):
        row = table.rowCount()
        table.insertRow(row)
        is_default = (src == "default")

        for col, text in enumerate([key, val, src]):
            item = QTableWidgetItem(text)
            if is_default:
                item.setForeground(
                    __import__('PySide6.QtGui', fromlist=['QColor']).QColor(_MUTED)
                )
            table.setItem(row, col, item)

    def _kv_add(self, table, field1, field2, mode, status_lbl):
        heard = field1.text().strip().lower()
        right = field2.text().strip()
        if not heard or not right:
            return
        field1.clear()
        field2.clear()
        status_lbl.setText("Saving...")

        def _do():
            try:
                mod = self._kv_module(mode)
                cur = mod.get_user_corrections() or {}
                cur[heard] = right
                mod.set_user_corrections(cur)
                if hasattr(mod, 'reload_corrections'):
                    mod.reload_corrections()
                if self._alive:
                    from PySide6.QtCore import QTimer
                    from PySide6.QtWidgets import QApplication
                    qt = QApplication.instance()
                    if qt:
                        QTimer.singleShot(0, qt, lambda: self._kv_reload(table, mode, status_lbl))
            except Exception as exc:
                logger.error(f"[DICT] {mode} add failed: {exc}", exc_info=True)
                if self._alive:
                    from PySide6.QtCore import QTimer
                    from PySide6.QtWidgets import QApplication
                    qt = QApplication.instance()
                    if qt:
                        QTimer.singleShot(0, qt, lambda: status_lbl.setText(f"Save failed: {exc}"))

        thread_registry.spawn("dictionary_panel_qt._kv_add", _do, daemon=True)

    def _kv_remove(self, table, mode, status_lbl):
        rows = sorted(
            {i.row() for i in table.selectionModel().selectedRows()},
            reverse=True
        )
        if not rows:
            return

        # Check all selected rows are user entries
        for row in rows:
            src_item = table.item(row, 2)
            if src_item and src_item.text() != "user":
                QMessageBox.information(
                    self,
                    "Read-only",
                    "Built-in defaults cannot be removed.  "
                    "Add a user entry with the corrected mapping to override one."
                )
                return

        keys = [table.item(row, 0).text() for row in rows]
        status_lbl.setText("Saving...")

        def _do():
            try:
                mod = self._kv_module(mode)
                cur = mod.get_user_corrections() or {}
                for k in keys:
                    cur.pop(k, None)
                mod.set_user_corrections(cur)
                if hasattr(mod, 'reload_corrections'):
                    mod.reload_corrections()
                if self._alive:
                    from PySide6.QtCore import QTimer
                    from PySide6.QtWidgets import QApplication
                    qt = QApplication.instance()
                    if qt:
                        QTimer.singleShot(0, qt, lambda: self._kv_reload(table, mode, status_lbl))
            except Exception as exc:
                logger.error(f"[DICT] {mode} remove failed: {exc}", exc_info=True)

        thread_registry.spawn("dictionary_panel_qt._kv_remove", _do, daemon=True)

    def _kv_reload(self, table, mode, status_lbl):
        """Reload table from source after a background save completes."""
        self._kv_load(table, mode)
        status_lbl.setText("")
