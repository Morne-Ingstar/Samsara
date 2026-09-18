"""Qt Dictionary panel — Vocabulary / Corrections / Wake Words.

Drop-in Qt replacement for samsara.ui.dictionary_frame.DictionaryFrame.

Three sub-tabs:
  Vocabulary   — words injected into Whisper's initial_prompt
  Corrections  — deterministic dictated-text corrections (VoiceTrainingQt)
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
from samsara.ui import theme

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colours — matches the main Qt window palette
# ---------------------------------------------------------------------------

# Colour comes from samsara.ui.theme, never from a literal here: this
# module used to keep its own copy of the dark palette, which is exactly
# how a second palette leaves one window unreadable (queue 129).

def _ss() -> str:
    """The window's stylesheet, built on demand. Never a module
    constant: an f-string evaluated at import time freezes the
    palette that happened to be live then (queue 129)."""
    return f"""
    QWidget {{ background:{theme.BG0}; color:{theme.TEXT_PRIMARY};
              font-family:'Segoe UI',sans-serif; font-size:{theme.TYPE_BODY}px; }}
    QTabWidget::pane {{ border:1px solid {theme.BORDER}; background:{theme.BG0}; }}
    QTabBar::tab {{ background:{theme.BG1}; color:{theme.TEXT_SECONDARY};
                    padding:5px 14px; border:none; margin-right:2px; }}
    QTabBar::tab:selected {{ background:{theme.BG2}; color:{theme.ACCENT};
                             border-bottom:2px solid {theme.ACCENT}; }}
    QTabBar::tab:hover:!selected {{ color:{theme.TEXT_PRIMARY}; }}
    QListWidget, QTableWidget {{
        background:{theme.BG1}; border:1px solid {theme.BORDER};
        color:{theme.TEXT_PRIMARY}; outline:none; gridline-color:{theme.BORDER};
    }}
    QListWidget::item {{ padding:3px 6px; }}
    QListWidget::item:selected {{ background:{theme.ACCENT}; color:{theme.BG0}; }}
    QTableWidget::item {{ padding:3px 6px; }}
    QTableWidget::item:selected {{ background:{theme.ACCENT}; color:{theme.BG0}; }}
    QHeaderView::section {{
        background:{theme.BG2}; color:{theme.TEXT_SECONDARY}; border:none;
        border-right:1px solid {theme.BORDER}; padding:4px 8px;
        font-size:{theme.TYPE_MIN}px; font-weight:bold;
    }}
    QLineEdit {{
        background:{theme.BG1}; border:1px solid {theme.BORDER};
        color:{theme.TEXT_PRIMARY}; padding:4px 8px; border-radius:4px;
    }}
    QLineEdit:focus {{ border-color:{theme.ACCENT}; }}
    QPushButton {{
        background:{theme.BG2}; color:{theme.TEXT_PRIMARY};
        border:1px solid {theme.BORDER}; padding:4px 12px; border-radius:4px;
    }}
    QPushButton:hover {{ background:{theme.ACCENT}; color:{theme.BG0}; border-color:{theme.ACCENT}; }}
    QPushButton#danger {{ color:{theme.ERROR}; border-color:{theme.ERROR}; }}
    QPushButton#danger:hover {{ background:{theme.ERROR}; color:{theme.BG0}; }}
""" + theme.SCROLLBAR_QSS


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
        self.setStyleSheet(_ss())
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
        desc.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
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

        transfer_row = QHBoxLayout()
        export_dictionary_btn = QPushButton("Export dictionary…")
        export_dictionary_btn.setObjectName("exportDictionaryButton")
        export_dictionary_btn.clicked.connect(self._dictionary_export)
        import_dictionary_btn = QPushButton("Import dictionary…")
        import_dictionary_btn.setObjectName("importDictionaryButton")
        import_dictionary_btn.clicked.connect(self._dictionary_import)
        transfer_row.addWidget(export_dictionary_btn)
        transfer_row.addWidget(import_dictionary_btn)
        transfer_row.addStretch()
        lay.addLayout(transfer_row)

        # Status label
        self._vocab_status = QLabel("")
        self._vocab_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(self._vocab_status)
        self._sigs.status.connect(
            lambda msg, col, w=self._vocab_status:
                w.setText(msg) or w.setStyleSheet(f"color:{col};font-size:{theme.TYPE_MIN}px;")
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
                f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;"
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
            self._vocab_status.setStyleSheet(f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;")
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))

    def _dictionary_profile_manager(self):
        from pathlib import Path
        from samsara.profiles import ProfileManager

        config_path = getattr(self._app, "config_path", None)
        app_dir = Path(config_path).parent if config_path else Path.cwd()
        return ProfileManager(app_dir)

    def _dictionary_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export dictionary", "samsara-dictionary.json",
            "Samsara dictionary (*.json);;JSON files (*.json)"
        )
        if not path:
            return
        ok, message = self._dictionary_profile_manager().export_dictionary_bundle(path)
        if ok:
            self._vocab_status.setText(message)
            self._vocab_status.setStyleSheet(
                f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;"
            )
        else:
            QMessageBox.critical(self, "Export failed", message)

    def _dictionary_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import dictionary", "",
            "Samsara dictionary (*.json);;JSON files (*.json)"
        )
        if not path:
            return

        choice = QMessageBox(self)
        choice.setWindowTitle("Import dictionary")
        choice.setText("How should the imported dictionary be applied?")
        choice.setInformativeText(
            "Merge keeps conflicting current entries. Replace backs up the current dictionary first."
        )
        merge_btn = choice.addButton("Merge", QMessageBox.ButtonRole.AcceptRole)
        replace_btn = choice.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
        choice.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        choice.exec()
        clicked = choice.clickedButton()
        if clicked is merge_btn:
            mode = "merge"
        elif clicked is replace_btn:
            mode = "replace"
        else:
            return

        ok, message = self._dictionary_profile_manager().import_dictionary_bundle(path, mode=mode)
        if not ok:
            QMessageBox.critical(self, "Import failed", message)
            return
        try:
            vt = self._vt
            if vt is not None:
                vt.load_training_data()
            from samsara import wake_corrections
            wake_corrections.reload_corrections()
            from samsara.command_catalog import install_user_aliases
            matcher = getattr(getattr(self._app, "command_executor", None), "_matcher", None)
            install_user_aliases(matcher, self._dictionary_profile_manager().app_dir)
        except Exception as exc:
            logger.debug("Dictionary import reload failed: %s", exc)
        self._vocab_list.clear()
        for word in self._custom_vocab:
            self._vocab_list.addItem(word)
        self._vocab_status.setText(message)
        self._vocab_status.setStyleSheet(
            f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;"
        )

    # ------------------------------------------------------------------
    # Corrections / Wake Words tabs (shared factory)
    # ------------------------------------------------------------------

    def _build_kv_tab(self, mode: str) -> QWidget:
        """Build a two-column key-value editor tab.

        mode='corrections' -> VoiceTrainingQt training_data.json
        mode='wake'        -> wake_corrections
        """
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(8)

        if mode == "corrections":
            desc_text = (
                'Fixes Whisper misrecognitions in dictated text '
                '(e.g. "open crow" -> "open Chrome").  '
                'Changes take effect on the next dictation.'
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
        desc.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(desc)

        # Input row
        inp_row = QHBoxLayout()
        inp_row.setSpacing(6)
        field1 = QLineEdit()
        field1.setPlaceholderText(ph1)
        arrow = QLabel("->")
        arrow.setStyleSheet(f"color:{theme.TEXT_SECONDARY};padding:0 4px;")
        field2 = QLineEdit()
        field2.setPlaceholderText(ph2)
        add_btn = QPushButton("Add")
        add_btn.setFixedWidth(56)
        inp_row.addWidget(field1, stretch=1)
        inp_row.addWidget(arrow)
        inp_row.addWidget(field2, stretch=1)
        inp_row.addWidget(add_btn)
        lay.addLayout(inp_row)

        # OWNER COPY — REVIEW: replace this placeholder with owner-approved copy.
        if mode == "corrections":
            scope_note = QLabel(
                "A correction replaces the word everywhere it is dictated as a standalone word."
            )
            scope_note.setWordWrap(True)
            scope_note.setStyleSheet(
                f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;"
            )
            lay.addWidget(scope_note)

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
        note = QLabel("Removing a seeded entry keeps it removed.")
        note.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        btn_row.addWidget(note)
        lay.addLayout(btn_row)

        # Status
        status_lbl = QLabel("")
        status_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
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
        if mode == "wake":
            from samsara import wake_corrections as _m
            return _m
        raise ValueError(f"No key-value module for mode {mode!r}")

    def _kv_load(self, table: QTableWidget, mode: str):
        """Populate table with user entries (editable) then defaults (dimmed)."""
        table.setRowCount(0)
        try:
            if mode == "corrections":
                vt = self._vt
                if vt is None:
                    return
                for key in sorted(vt.corrections_dict):
                    self._kv_insert_row(table, key, vt.corrections_dict[key], "user")
                return
            mod = self._kv_module(mode)
            user = mod.get_user_corrections() or {}
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
            color = theme.TEXT_SECONDARY if is_default else theme.TEXT_PRIMARY
            item.setForeground(__import__('PySide6.QtGui', fromlist=['QColor']).QColor(color))
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
                if mode == "corrections":
                    vt = self._vt
                    saved = vt is not None and vt.add_correction(heard, right)
                else:
                    mod = self._kv_module(mode)
                    cur = mod.get_user_corrections() or {}
                    cur[heard] = right
                    # Adding always grows the dict, so this can never trip the
                    # empty-overwrite guard -- default allow_empty=False is
                    # correct here; checked anyway so a write failure for any
                    # OTHER reason still surfaces instead of reloading stale state.
                    saved = mod.set_user_corrections(cur)
                    if saved and hasattr(mod, 'reload_corrections'):
                        mod.reload_corrections()
                if self._alive:
                    from PySide6.QtCore import QTimer
                    from PySide6.QtWidgets import QApplication
                    qt = QApplication.instance()
                    if qt:
                        if saved:
                            QTimer.singleShot(0, qt, lambda: self._kv_reload(table, mode, status_lbl))
                        else:
                            QTimer.singleShot(0, qt, lambda: status_lbl.setText("Save refused -- see log"))
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
                if mode == "corrections":
                    vt = self._vt
                    saved = vt is not None and all(vt.remove_correction(key) for key in keys)
                else:
                    mod = self._kv_module(mode)
                    cur = mod.get_user_corrections() or {}
                    for k in keys:
                        cur.pop(k, None)
                    # This IS the panel's clear-all-user-entries path: the user
                    # explicitly selected and removed every row, which can
                    # legitimately drive cur to {} -- allow_empty=True so the
                    # empty-overwrite guard doesn't refuse a deliberate delete.
                    saved = mod.set_user_corrections(cur, allow_empty=True)
                    if saved and hasattr(mod, 'reload_corrections'):
                        mod.reload_corrections()
                if self._alive:
                    from PySide6.QtCore import QTimer
                    from PySide6.QtWidgets import QApplication
                    qt = QApplication.instance()
                    if qt:
                        if saved:
                            QTimer.singleShot(0, qt, lambda: self._kv_reload(table, mode, status_lbl))
                        else:
                            QTimer.singleShot(0, qt, lambda: status_lbl.setText("Save refused -- see log"))
            except Exception as exc:
                logger.error(f"[DICT] {mode} remove failed: {exc}", exc_info=True)

        thread_registry.spawn("dictionary_panel_qt._kv_remove", _do, daemon=True)

    def _kv_reload(self, table, mode, status_lbl):
        """Reload table from source after a background save completes."""
        self._kv_load(table, mode)
        status_lbl.setText("")
