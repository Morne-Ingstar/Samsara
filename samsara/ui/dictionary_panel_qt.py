"""Qt Dictionary panel — Vocabulary / Corrections / Wake Words.

Drop-in Qt replacement for samsara.ui.dictionary_frame.DictionaryFrame.

Three sub-tabs:
  Vocabulary   — words injected into Whisper's initial_prompt
  Corrections  — deterministic dictated-text corrections (DictionaryStore)
  Wake Words   — wake-phrase misrecognition map (samsara.wake_corrections)

Data is read/written through the same service layer as the CTk version;
no file or DB access happens directly here.

Saves are asynchronous (background thread) with a Signal-based status
label update so the Qt event loop is never blocked.
"""

from samsara.dictionary_store import get_dictionary_store
from samsara.config_defaults import cfg_get

import logging
import re
from contextlib import nullcontext

from PySide6.QtCore import Qt, Signal, Slot, QObject, QSettings, QItemSelectionModel
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from samsara.runtime import thread_registry
from samsara.ui import theme

logger = logging.getLogger(__name__)


def _qt_widget_is_valid(widget) -> bool:
    if widget is None:
        return False
    try:
        import shiboken6
        return shiboken6.isValid(widget)
    except Exception:
        return False

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
    builtin_saved = Signal(object, bool, bool)  # word (None = master), enabled, saved
    refresh = Signal()
    vocab_saved = Signal(str, bool, bool)


def refresh_dictionary(app):
    panel = getattr(app, '_dictionary_panel', None)
    if panel is not None and _qt_widget_is_valid(panel):
        panel._sigs.refresh.emit()


def prefill_dictionary(app, text):
    """Use Home's existing hub navigation; only fill, never persist."""
    from samsara.ui import qt_runtime
    app.show_main_window()

    def select():
        app.open_hub_page('Dictionary')

        def fill():
            panel = getattr(app, '_dictionary_panel', None)
            if panel is not None and _qt_widget_is_valid(panel):
                panel.prefill(text)

        qt_runtime.post(fill)

    qt_runtime.post(select)


class _VocabularyInput(QLineEdit):
    def __init__(self, app):
        super().__init__()
        self._app = app
        self.textChanged.connect(self._normalize_spelling)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._app._dictionary_add_focused = True

    def focusOutEvent(self, event):
        self._app._dictionary_add_focused = False
        super().focusOutEvent(event)

    def _normalize_spelling(self, text):
        if self.hasFocus() and re.fullmatch(r'[A-Z](?:[- .][A-Z]){2,}\.?', text):
            from samsara.letter_spelling import parse_letters
            word = parse_letters(text)
            if word:
                self.setText(word)
                self.setCursorPosition(len(word))


# ---------------------------------------------------------------------------
# Public panel
# ---------------------------------------------------------------------------

class DictionaryPanelQt(QWidget):
    """Three-tab dictionary editor.  Embed directly in any Qt layout."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app    = app
        self._alive  = True
        self._sigs   = _Signals(self)
        self.setStyleSheet(_ss())
        self._build_ui()
        self._app._dictionary_panel = self
        self._sigs.refresh.connect(self.reload, Qt.ConnectionType.QueuedConnection)
        self._sigs.vocab_saved.connect(self._vocab_saved)
        subscribe = getattr(self._vt, 'add_change_listener', None)
        if callable(subscribe):
            disconnect = subscribe(self._dictionary_changed)
            self.destroyed.connect(lambda _obj=None: disconnect())

    def _dictionary_changed(self):
        if _qt_widget_is_valid(self):
            self._sigs.refresh.emit()

    def showEvent(self, event):
        self._alive = True
        self.reload()
        super().showEvent(event)
        # Measure after parenting/polish: the hub supplies the final font.
        row_height = self._vocab_list.sizeHintForRow(0)
        if row_height < 0:
            row_height = self._vocab_list.fontMetrics().height() + 6
        self._vocab_list.setMinimumHeight(
            4 * row_height + 2 * self._vocab_list.frameWidth())

    @Slot()
    def reload(self):
        if not self._alive:
            return
        selected = {item.text() for item in self._vocab_list.selectedItems()}
        scroll = self._vocab_list.verticalScrollBar().value()
        self._vocab_list.clear()
        self._vocab_list.addItems(list(self._custom_vocab))
        for row in range(self._vocab_list.count()):
            item = self._vocab_list.item(row)
            item.setSelected(item.text() in selected)
        self._vocab_list.verticalScrollBar().setValue(scroll)
        table = self._corrections_table
        selected = {table.item(index.row(), 0).text()
                    for index in table.selectionModel().selectedRows()}
        scroll = table.verticalScrollBar().value()
        self._kv_load(table, 'corrections')
        for row in range(table.rowCount()):
            if table.item(row, 0).text() in selected:
                table.selectionModel().select(
                    table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        table.verticalScrollBar().setValue(scroll)

    def prefill(self, text):
        self._tabs.setCurrentIndex(0)
        self._vocab_input.setText(text)
        self._vocab_input.setFocus()
        self._vocab_input.selectAll()
        self._vocab_status.setText('Not saved yet. Edit the word, then say add or press Add.')

    def closeEvent(self, e):
        self._alive = False
        e.accept()

    # ------------------------------------------------------------------
    # Service accessors (no direct file I/O)
    # ------------------------------------------------------------------

    @property
    def _vt(self):
        return get_dictionary_store(self._app)

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

        vocab_title = "Vocabulary"
        self._tabs.addTab(self._build_vocab_tab(vocab_title), vocab_title)
        for mode, title in (("corrections", "Corrections"), ("wake", "Wake Words")):
            self._tabs.addTab(self._build_kv_tab(mode, title), title)

    # ------------------------------------------------------------------
    # Vocabulary tab
    # ------------------------------------------------------------------

    def _build_vocab_tab(self, title: str) -> QWidget:
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(6)

        desc = QLabel(
            "Add names, technical terms, or words Samsara often mishears."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(desc)

        self._voice_settings = QSettings("Samsara", "Samsara")
        self._voice_expander = QToolButton()
        self._voice_expander.setText("How to add words by voice")
        self._voice_expander.setAccessibleName(self._voice_expander.text())
        self._voice_expander.setCheckable(True)
        self._voice_expander.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._voice_expander.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        lay.addWidget(self._voice_expander)
        help_text = QLabel(
            'Say: “add the word Siobhan to my vocabulary spelled S I O B H A N”. '
            'After the readback, say yes or no without a key. '
            'You can also say “learn the word Siobhan spelled S-I-O-B-H-A-N”, '
            '“correct that to Siobhan spelled S I O B H A N”, or '
            '“add the selected word to my vocabulary”. '
            'Works with Ava off. Say “add that to the dictionary” to edit your last dictation here.'
        )
        help_text.setObjectName('dictionaryVoiceHelp')
        help_text.setWordWrap(True)
        help_text.setStyleSheet(f'color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;')
        lay.addWidget(help_text)

        def expand_voice(expanded):
            self._voice_expander.setArrowType(
                Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
            help_text.setVisible(expanded)
            self._voice_settings.setValue("dictionary/voice_help_expanded", expanded)

        expanded = self._voice_settings.value("dictionary/voice_help_expanded", False, type=bool)
        self._voice_expander.setChecked(expanded)
        help_text.setVisible(expanded)
        self._voice_expander.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._voice_expander.toggled.connect(expand_voice)

        self._build_builtin_words(lay)

        # Input row
        add_row = QHBoxLayout()
        self._vocab_input = _VocabularyInput(self._app)
        self._vocab_input.setAccessibleName("Add vocabulary word")
        self._vocab_input.setPlaceholderText("Word or phrase to add...")
        self._vocab_input.returnPressed.connect(self._vocab_add)
        add_row.addWidget(self._vocab_input, stretch=1)
        add_btn = QPushButton("Add")
        add_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        add_btn.clicked.connect(self._vocab_add)
        add_row.addWidget(add_btn)
        lay.addLayout(add_row)

        # List
        self._vocab_list = QListWidget()
        self._vocab_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._vocab_list.setAccessibleName(title)
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
        imp_btn = QPushButton("Import")
        imp_btn.clicked.connect(self._vocab_import)
        exp_btn = QPushButton("Export")
        exp_btn.clicked.connect(self._vocab_export)
        btn_row.addWidget(rem_btn)
        btn_row.addWidget(imp_btn)
        btn_row.addWidget(exp_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        # Vocabulary JSON is distinct from a full dictionary bundle.
        more_btn = QToolButton()
        more_btn.setText("More")
        more_btn.setAccessibleName("More dictionary actions")
        more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more_btn)
        menu.addAction("Export dictionary…", self._dictionary_export)
        menu.addAction("Import dictionary…", self._dictionary_import)
        more_btn.setMenu(menu)
        btn_row.insertWidget(3, more_btn)
        for button in (rem_btn, imp_btn, exp_btn, more_btn):
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        # Status label
        self._vocab_status = QLabel("")
        self._vocab_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(self._vocab_status)
        self._sigs.status.connect(
            lambda msg, col, w=self._vocab_status:
                w.setText(msg) or w.setStyleSheet(f"color:{col};font-size:{theme.TYPE_MIN}px;")
        )

        return page

    def _build_builtin_words(self, layout):
        from samsara.ui.settings.calm_controls import switch

        master_row = QHBoxLayout()
        self._builtin_enabled = switch(QCheckBox())
        self._builtin_enabled.setObjectName("builtinVocabularyEnabled")
        label = QLabel("Use them")
        label.setBuddy(self._builtin_enabled)
        self._builtin_enabled.setAccessibleName("Use Samsara's built-in words")
        self._builtin_enabled.setChecked(cfg_get(self._app.config, "builtin_vocabulary_enabled"))
        self._builtin_enabled.setEnabled(callable(getattr(self._app, "update_config_and_save", None)))

        self._builtin_expander = QToolButton()
        self._builtin_expander.setText("Built-in words")
        self._builtin_expander.setAccessibleName("Show built-in words")
        self._builtin_expander.setCheckable(True)
        self._builtin_expander.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._builtin_expander.setArrowType(Qt.ArrowType.RightArrow)
        self._builtin_expander.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        master_row.addWidget(self._builtin_expander)
        master_row.addWidget(self._builtin_enabled)
        master_row.addWidget(label)
        master_row.addStretch()
        layout.addLayout(master_row)

        self._builtin_scroll = QScrollArea()
        self._builtin_scroll.setWidgetResizable(True)
        self._builtin_scroll.setMaximumHeight(220)
        self._builtin_scroll.setMinimumHeight(132)
        self._builtin_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        rows = QVBoxLayout(content)
        rows.setContentsMargins(8, 4, 8, 4)
        rows.setSpacing(2)
        self._builtin_switches = {}
        service = self._vt
        entries = service.get_builtin_words() if callable(getattr(service, "get_builtin_words", None)) else []
        for entry in entries:
            word = entry["word"]
            row = QHBoxLayout()
            control = switch(QCheckBox())
            control.setAccessibleName(f"Use built-in word {word}")
            control.setChecked(entry["enabled"])
            control.toggled.connect(lambda enabled, word=word: self._builtin_save(word, enabled))
            text = QLabel(word)
            text.setBuddy(control)
            text.setToolTip(entry["why"])
            row.addWidget(control)
            row.addWidget(text)
            row.addStretch()
            rows.addLayout(row)
            self._builtin_switches[word] = control
        rows.addStretch()
        self._builtin_scroll.setWidget(content)
        self._builtin_scroll.setVisible(False)
        self._builtin_scroll.setEnabled(self._builtin_enabled.isChecked())
        layout.addWidget(self._builtin_scroll)
        self._builtin_expander.toggled.connect(self._builtin_expand)
        self._builtin_enabled.toggled.connect(lambda enabled: self._builtin_save(None, enabled))
        self._sigs.builtin_saved.connect(self._builtin_save_finished)

    def _builtin_expand(self, expanded):
        self._builtin_expander.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._builtin_expander.setAccessibleName("Hide built-in words" if expanded else "Show built-in words")
        self._builtin_scroll.setVisible(expanded)

    def _builtin_save(self, word, enabled):
        # Serialize preference writes and keep all disk work off the Qt thread.
        self._builtin_enabled.setEnabled(False)
        self._builtin_scroll.setEnabled(False)
        self._vocab_status.setText("Saving...")

        def save():
            saved = False
            had_key = "builtin_vocabulary_enabled" in self._app.config
            previous = self._app.config.get("builtin_vocabulary_enabled")
            try:
                if word is None:
                    if not getattr(self._app, "_config_load_failed", False):
                        saved = self._app.update_config_and_save({"builtin_vocabulary_enabled": enabled}) is not False
                else:
                    saved = self._vt.set_builtin_word_enabled(word, enabled)
            except Exception:
                logger.exception("Could not save built-in vocabulary preference")
            if word is None and not saved:
                with getattr(self._app, "_config_lock", None) or nullcontext():
                    if had_key:
                        self._app.config["builtin_vocabulary_enabled"] = previous
                    else:
                        self._app.config.pop("builtin_vocabulary_enabled", None)
            if self._alive:
                self._sigs.builtin_saved.emit(word, enabled, saved)

        thread_registry.spawn("dictionary_panel_qt.builtin_save", save, daemon=True)

    def _builtin_save_finished(self, word, enabled, saved):
        if not self._alive or not _qt_widget_is_valid(self):
            return
        control = self._builtin_enabled if word is None else self._builtin_switches[word]
        if not saved:
            control.blockSignals(True)
            control.setChecked(not enabled)
            control.blockSignals(False)
        self._builtin_enabled.setEnabled(callable(getattr(self._app, "update_config_and_save", None)))
        self._builtin_scroll.setEnabled(self._builtin_enabled.isChecked())
        self._vocab_status.setText("" if saved else "Could not save built-in words. Try again.")

    def _vocab_add(self):
        voice = getattr(self, '_voice_add_pending', False)
        self._voice_add_pending = False
        word = self._vocab_input.text().strip()
        if not word:
            if voice:
                from plugins.commands.ask_ollama import _teach_notice
                _teach_notice(self._app, 'Which word?')
            return
        vocab = self._custom_vocab
        if any(item.casefold() == word.casefold() for item in vocab):
            self._vocab_status.setText(f'"{word}" is already in the list.')
            if voice:
                from plugins.commands.ask_ollama import _teach_notice, _teach_history
                _teach_notice(self._app, f'{word} is already in your dictionary.')
                _teach_history(self._app, 'add', 'already saved')
            return
        self._vocab_input.setEnabled(False)
        self._vocab_status.setText('Saving...')

        def save():
            before = list(vocab)
            saved = self._vt.add_vocab_word(word)
            if not saved:
                vocab[:] = before
            if self._alive:
                self._sigs.vocab_saved.emit(word, saved, voice)

        thread_registry.spawn('dictionary_panel_qt.add_vocab', save, daemon=True)

    def add_by_voice(self):
        self._voice_add_pending = True
        self._vocab_add()

    def _vocab_saved(self, word, saved, voice):
        self._vocab_input.setEnabled(True)
        if saved:
            self._vocab_input.clear()
            self.reload()
        self._vocab_status.setText(f'Added {word} to your dictionary.' if saved else
                                   'Could not save that word. Try again.')
        if voice:
            from plugins.commands.ask_ollama import _teach_notice, _teach_history
            _teach_notice(self._app, self._vocab_status.text())
            _teach_history(self._app, 'add', 'saved' if saved else 'failed to save')

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
        self.reload()
        self._vocab_status.setText(message)
        self._vocab_status.setStyleSheet(
            f"color:{theme.SUCCESS};font-size:{theme.TYPE_MIN}px;"
        )

    # ------------------------------------------------------------------
    # Corrections / Wake Words tabs (shared factory)
    # ------------------------------------------------------------------

    def _build_kv_tab(self, mode: str, title: str) -> QWidget:
        """Build a two-column key-value editor tab.

        mode='corrections' -> DictionaryStore training_data.json
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
        field1.setAccessibleName(col1_hdr)
        field1.setPlaceholderText(ph1)
        arrow = QLabel("->")
        arrow.setStyleSheet(f"color:{theme.TEXT_SECONDARY};padding:0 4px;")
        field2 = QLineEdit()
        field2.setAccessibleName(col2_hdr)
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
        if mode == "corrections":
            self._corrections_table = table
        table.setAccessibleName(title)
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
                for key, value in sorted(dict(vt.corrections_dict).items()):
                    self._kv_insert_row(table, key, value, "user")
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
            # Let the table stylesheet own foreground colour. The former
            # explicit TEXT_SECONDARY brush was built from a CSS rgba() token,
            # which QColor rejected and painted black; an explicit brush also
            # stayed stale after a live theme switch. The Source column already
            # distinguishes built-in rows without making their text harder to read.
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
                        def update_if_alive(callback):
                            def apply():
                                if (self._alive and _qt_widget_is_valid(self)
                                        and _qt_widget_is_valid(table)
                                        and _qt_widget_is_valid(status_lbl)):
                                    callback()
                            return apply
                        if saved:
                            QTimer.singleShot(0, qt, update_if_alive(lambda: self._kv_reload(table, mode, status_lbl)))
                        else:
                            QTimer.singleShot(0, qt, update_if_alive(lambda: status_lbl.setText("Save refused -- see log")))
            except Exception as exc:
                logger.error(f"[DICT] {mode} add failed: {exc}", exc_info=True)
                if self._alive:
                    from PySide6.QtCore import QTimer
                    from PySide6.QtWidgets import QApplication
                    qt = QApplication.instance()
                    if qt:
                        msg = str(exc)
                        def show_error(message=msg):
                            if (self._alive and _qt_widget_is_valid(self)
                                    and _qt_widget_is_valid(status_lbl)):
                                status_lbl.setText(f"Save failed: {message}")
                        QTimer.singleShot(0, qt, show_error)

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
