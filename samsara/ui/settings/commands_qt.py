"""Commands page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
CommandsPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from samsara.runtime import thread_registry
from samsara.ui import qt_runtime, theme

# Paths below stay relative to settings_qt.py, where this code was written.
from samsara.ui.settings_qt import __file__ as _SETTINGS_QT_FILE
from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH, _collect_command_rows, logger


class CommandsPage:
    """Methods of the Commands settings page (moved from _SettingsWindow)."""

    def _build_commands_tab(self):
        import json
        from pathlib import Path
        from samsara.command_packs import PACKS

        page = QScrollArea()
        page.setObjectName("commandsPageScroll")
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )

        outer = QWidget()
        outer.setMaximumWidth(_CONTENT_MAX_WIDTH)
        outer.setObjectName("commandsPageContent")
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 12)
        layout.setSpacing(8)

        cfg = self.app.config

        # ---- Section: Command Packs ----------------------------------------
        layout.addWidget(self._section_title("Command Packs"))

        desc1 = QLabel(
            "Enable the packs you use. Disabling unused packs improves recognition accuracy."
        )
        desc1.setWordWrap(True)
        desc1.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: 12px;")
        layout.addWidget(desc1)
        layout.addSpacing(4)

        pack_scroll = QScrollArea()
        pack_scroll.setObjectName("commandPacksScroll")
        pack_scroll.setWidgetResizable(True)
        pack_scroll.setMinimumHeight(210)
        pack_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        pack_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        pack_scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {theme.BG1}; border-radius: 6px; "
            "border: 1px solid rgba(255,255,255,0.08); }"
        )

        pack_container = QWidget()
        pack_container.setStyleSheet("background: transparent;")
        pack_vlayout = QVBoxLayout(pack_container)
        pack_vlayout.setContentsMargins(10, 8, 10, 8)
        pack_vlayout.setSpacing(3)

        current_packs = cfg.get('command_packs', {}) or {}
        pack_checkboxes: dict[str, QCheckBox] = {}

        # Count commands per pack from both commands.json and plugin registry
        pack_counts: dict[str, int] = {}
        try:
            exe = getattr(self.app, 'command_executor', None)
            if exe and hasattr(exe, 'commands_path'):
                raw = json.loads(exe.commands_path.read_text(encoding='utf-8'))
                for cmd in raw.get('commands', raw).values():
                    p = cmd.get('pack', 'core')
                    pack_counts[p] = pack_counts.get(p, 0) + 1
        except Exception as e:
            logger.debug(f"_build_commands_tab: {e}")
        try:
            from samsara import plugin_commands as _pc
            seen: set = set()
            for entry in _pc._REGISTRY.values():
                eid = id(entry)
                if eid in seen:
                    continue
                seen.add(eid)
                p = entry.get('pack', 'core')
                pack_counts[p] = pack_counts.get(p, 0) + 1
        except Exception as e:
            logger.debug(f"_build_commands_tab: {e}")

        restart_lbl = QLabel("Restart Samsara to apply pack changes.")
        restart_lbl.setStyleSheet(f"color: {theme.WARNING}; font-size: 12px;")
        restart_lbl.setVisible(False)
        self._widgets['_pack_restart_lbl'] = restart_lbl

        for pack_id, meta in PACKS.items():
            always_on     = meta.get('always_on', False)
            default_on    = meta.get('default_enabled', False)
            enabled       = bool(current_packs.get(pack_id, default_on)) or always_on
            count         = pack_counts.get(pack_id, 0)
            count_str     = f"  ({count})" if count else ""

            row_w = QWidget()
            row_w.setStyleSheet("background: transparent;")
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.setSpacing(8)

            cb = QCheckBox()
            cb.setChecked(enabled)
            cb.setEnabled(not always_on)
            cb.toggled.connect(lambda _, lbl=restart_lbl: lbl.setVisible(True))
            pack_checkboxes[pack_id] = cb
            row_h.addWidget(cb, alignment=Qt.AlignmentFlag.AlignTop)

            text_col = QVBoxLayout()
            text_col.setContentsMargins(0, 0, 0, 0)
            text_col.setSpacing(1)

            name_lbl = QLabel(
                meta.get('label', pack_id)
                + ("  •  always on" if always_on else "")
                + count_str
            )
            name_lbl.setStyleSheet(
                f"color: {theme.ICON_IDLE if always_on else theme.TEXT_PRIMARY}; "
                f"font-size: 13px; font-weight: {'normal' if always_on else '600'};"
                "background: transparent;"
            )
            text_col.addWidget(name_lbl)

            desc_lbl = QLabel(meta.get('description', ''))
            desc_lbl.setObjectName(f"commandPackDescription_{pack_id}")
            desc_lbl.setStyleSheet(
                "color: #AEB4C0; font-size: 13px; background: transparent;"
            )
            desc_lbl.setWordWrap(True)
            desc_lbl.setMinimumWidth(0)
            desc_lbl.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            text_col.addWidget(desc_lbl)
            row_h.addLayout(text_col, stretch=1)

            pack_vlayout.addWidget(row_w)

        pack_vlayout.addStretch()
        pack_scroll.setWidget(pack_container)
        self._widgets['_pack_checkboxes'] = pack_checkboxes
        layout.addWidget(pack_scroll)
        layout.addWidget(restart_lbl)
        layout.addSpacing(4)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("background-color: rgba(255,255,255,0.06); max-height: 1px;")
        layout.addWidget(sep2)
        layout.addSpacing(4)

        # ---- Section: Voice Commands ---------------------------------------
        cmd_header = QHBoxLayout()
        cmd_header.addWidget(self._section_title("Voice Commands"))
        cmd_header.addStretch()
        search_box = QLineEdit()
        search_box.setPlaceholderText("Search commands...")
        search_box.setMinimumWidth(140)
        search_box.setMaximumWidth(280)
        search_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._widgets['cmd_search'] = search_box
        cmd_header.addWidget(search_box)
        layout.addLayout(cmd_header)

        instant_note = QLabel("Changes to commands below apply immediately.")
        # Word-wrap so this sentence cannot force a horizontal scrollbar on the
        # Commands page. An unwrapped QLabel reports its FULL single-line width
        # as minimumSizeHint, which under a wider font stack than Segoe UI
        # (528px here vs a 530px viewport) drags the whole page wider. Wrapping
        # does not change how it renders where it already fits on one line.
        instant_note.setWordWrap(True)
        instant_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: 12px;")
        layout.addWidget(instant_note)
        layout.addSpacing(4)

        # Table
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(['Voice Phrase', 'Type', 'Action', 'Description'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setStyleSheet(
            table.styleSheet()
            + "QTableWidget { alternate-background-color: rgba(255,255,255,0.02); }"
        )
        table.setMinimumHeight(260)
        self._widgets['cmd_table'] = table
        self._populate_commands_table(table, "")
        search_box.textChanged.connect(lambda txt: self._filter_commands(table, txt))
        layout.addWidget(table, stretch=1)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        add_btn = QPushButton("Add Command")
        add_btn.setObjectName("addCommandButton")
        add_btn.clicked.connect(lambda: self._open_command_dialog(None, table))
        btn_row.addWidget(add_btn)

        edit_btn = QPushButton("Edit")
        edit_btn.setObjectName("editCommandButton")
        edit_btn.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {theme.ICON_IDLE}; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; "
            "padding: 8px 16px; }"
            f"QPushButton:hover {{ background-color: rgba(255,255,255,0.05); color: {theme.TEXT_PRIMARY}; }}"
        )
        edit_btn.clicked.connect(lambda: self._edit_selected_command(table))
        btn_row.addWidget(edit_btn)

        del_btn = QPushButton("Delete")
        del_btn.setObjectName("deleteCommandButton")
        del_btn.setStyleSheet(
            f"QPushButton {{ background-color: rgba(200,60,60,0.15); color: {theme.ERROR}; "
            "border: 1px solid rgba(200,60,60,0.3); border-radius: 6px; padding: 8px 16px; }"
            "QPushButton:hover { background-color: rgba(200,60,60,0.25); }"
        )
        del_btn.clicked.connect(lambda: self._delete_selected_command(table))
        btn_row.addWidget(del_btn)

        test_btn = QPushButton("Test")
        test_btn.setObjectName("testCommandButton")
        test_btn.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {theme.ICON_IDLE}; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; "
            "padding: 8px 12px; }"
            f"QPushButton:hover {{ background-color: rgba(255,255,255,0.05); color: {theme.TEXT_PRIMARY}; }}"
        )
        test_btn.clicked.connect(lambda: self._test_selected_command(table))
        btn_row.addWidget(test_btn)

        btn_row.addStretch()

        reload_btn = QPushButton("Reload")
        reload_btn.setObjectName("reloadCommandsButton")
        reload_btn.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {theme.ICON_IDLE}; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; "
            "padding: 8px 12px; }"
            f"QPushButton:hover {{ background-color: rgba(255,255,255,0.05); color: {theme.TEXT_PRIMARY}; }}"
        )
        reload_btn.clicked.connect(lambda: self._reload_commands(table))
        btn_row.addWidget(reload_btn)

        layout.addLayout(btn_row)

        footer = QLabel("Say these phrases while recording to trigger actions.")
        # Same reason as instant_note above: unwrapped, this label's 636px
        # single-line minimum is the widest thing on the page and is what the
        # horizontal scrollbar was tracking.
        footer.setWordWrap(True)
        footer.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: 12px;")
        layout.addWidget(footer)

        def _save(acc):
            updates = {}
            if '_pack_checkboxes' in self._widgets:
                from samsara.command_packs import PACKS
                new_packs = dict(self.app.config.get('command_packs', {}) or {})
                for pack_id, cb in self._widgets['_pack_checkboxes'].items():
                    meta = PACKS.get(pack_id, {})
                    if not meta.get('always_on'):
                        new_packs[pack_id] = cb.isChecked()
                updates['command_packs'] = new_packs
            return updates
        self._save_fns.append(_save)

        page.setWidget(outer)
        return page
    @staticmethod
    def _cmd_action_text(cmd_data: dict) -> str:
        t = cmd_data.get('type', '')
        if t == 'hotkey':
            return '+'.join(k.capitalize() for k in cmd_data.get('keys', []))
        if t == 'launch':
            tgt = cmd_data.get('target', '')
            return ('...' + tgt[-24:]) if len(tgt) > 27 else tgt
        if t in ('press', 'key_down', 'key_up'):
            verb = {'press': 'Press', 'key_down': 'Hold', 'key_up': 'Release'}.get(t, t)
            return f"{verb} {cmd_data.get('key', '').upper()}"
        if t == 'mouse':
            return (f"{cmd_data.get('action','click').replace('_',' ').title()} "
                    f"({cmd_data.get('button','left')})")
        if t == 'release_all':
            return "Release all keys"
        if t == 'text':
            txt = cmd_data.get('text', '')
            return repr(txt[:30]) + ('...' if len(txt) > 30 else '')
        if t == 'macro':
            steps = cmd_data.get('steps', [])
            return f"Macro ({len(steps)} steps)"
        return str(cmd_data.get('type', ''))

    def _populate_commands_table(self, table: QTableWidget, filter_text: str = '') -> None:
        table.setUpdatesEnabled(False)
        table.setRowCount(0)
        try:
            executor = getattr(self.app, 'command_executor', None)
            fl = filter_text.lower()
            for command in _collect_command_rows(executor):
                searchable = (
                    command['phrase'], command['source'], command['type'],
                    command['description'], command['pack'],
                    ' '.join(command['aliases']),
                )
                if fl and not any(fl in str(value).lower() for value in searchable):
                    continue
                row = table.rowCount()
                table.insertRow(row)
                phrase_item = QTableWidgetItem(command['phrase'])
                phrase_item.setData(Qt.ItemDataRole.UserRole, command['source'])
                table.setItem(row, 0, phrase_item)
                table.setItem(row, 1, QTableWidgetItem(command['type']))
                action = (
                    self._cmd_action_text(command['data'])
                    if command['source'] == 'builtin'
                    else f"{command['pack']} plugin"
                )
                table.setItem(row, 2, QTableWidgetItem(action))
                table.setItem(row, 3, QTableWidgetItem(command['description']))
        finally:
            table.setUpdatesEnabled(True)

    def _filter_commands(self, table: QTableWidget, text: str) -> None:
        self._populate_commands_table(table, text)

    def _selected_phrase(self, table: QTableWidget):
        rows = table.selectedItems()
        if not rows:
            return None
        return table.item(table.currentRow(), 0).text()

    def _save_commands_to_disk(self) -> None:
        import json
        from pathlib import Path
        exe = getattr(self.app, 'command_executor', None)
        if exe is None:
            return
        path = getattr(exe, 'commands_path', None)
        if path is None:
            path = Path(_SETTINGS_QT_FILE).parent.parent.parent / 'commands.json'
        try:
            data = {'commands': exe.commands}
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            if hasattr(exe, 'rebuild_matcher'):
                exe.rebuild_matcher()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save commands:\n{e}")

    def _reload_commands(self, table: QTableWidget) -> None:
        exe = getattr(self.app, 'command_executor', None)
        if exe is None:
            return
        try:
            if hasattr(exe, 'reload_commands'):
                exe.reload_commands()
            else:
                exe.load_commands()
            search = self._widgets.get('cmd_search')
            self._populate_commands_table(
                table, search.text() if search else ''
            )
            QMessageBox.information(
                self, "Reloaded",
                f"Loaded {len(_collect_command_rows(exe))} commands."
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to reload:\n{e}")

    def _delete_selected_command(self, table: QTableWidget) -> None:
        phrase = self._selected_phrase(table)
        if not phrase:
            QMessageBox.warning(self, "No Selection", "Select a command to delete.")
            return
        exe = getattr(self.app, 'command_executor', None)
        if exe is None or phrase not in getattr(exe, 'commands', {}):
            QMessageBox.information(
                self, "Plugin Command",
                "Plugin commands are defined in source and cannot be deleted here.",
            )
            return
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete the command '{phrase}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if phrase in exe.commands:
            del exe.commands[phrase]
            self._save_commands_to_disk()
            search = self._widgets.get('cmd_search')
            self._populate_commands_table(
                table, search.text() if search else ''
            )

    def _test_selected_command(self, table: QTableWidget) -> None:
        phrase = self._selected_phrase(table)
        if not phrase:
            QMessageBox.warning(self, "No Selection", "Select a command to test.")
            return
        exe = getattr(self.app, 'command_executor', None)
        if exe is None:
            return
        self.showMinimized()
        import threading, time
        def _run():
            time.sleep(0.4)
            try:
                _result, ok = exe.process_text(
                    phrase, self.app, force_commands=True,
                )
                msg = f"'{phrase}' executed OK." if ok else f"'{phrase}' not found or failed."
                self._test_result.emit(msg, theme.ACCENT if ok else theme.ERROR)
            except Exception as exc:
                self._test_result.emit(f"Error: {exc}", theme.ERROR)
            # showNormal() is a QWidget method -- must run on the Qt thread,
            # not this worker thread. qt_runtime.post() is the established
            # marshal-a-callable-onto-the-Qt-thread idiom used throughout
            # the codebase (already imported in this module).
            qt_runtime.post(self.showNormal)
        thread_registry.spawn("settings_qt._run", _run, daemon=True)

    def _edit_selected_command(self, table: QTableWidget) -> None:
        phrase = self._selected_phrase(table)
        if not phrase:
            QMessageBox.warning(self, "No Selection", "Select a command to edit.")
            return
        exe = getattr(self.app, 'command_executor', None)
        if exe is None or phrase not in getattr(exe, 'commands', {}):
            QMessageBox.information(
                self, "Plugin Command",
                "Plugin commands are defined in source and cannot be edited here.",
            )
            return
        self._open_command_dialog(phrase, table)

    def _open_command_dialog(self, edit_phrase, table: QTableWidget) -> None:
        exe = getattr(self.app, 'command_executor', None)
        if exe is None:
            return

        existing = exe.commands.get(edit_phrase, {}) if edit_phrase else {}
        _TYPES = ['hotkey', 'text', 'launch', 'press', 'key_down', 'key_up',
                  'mouse', 'release_all']

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Command" if edit_phrase else "Add Command")
        dlg.setMinimumWidth(480)
        dlg.setStyleSheet(self.styleSheet())

        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setContentsMargins(20, 20, 20, 16)
        dlg_layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        phrase_edit = QLineEdit(edit_phrase or '')
        phrase_edit.setPlaceholderText("e.g. open browser")
        form.addRow("Voice phrase:", phrase_edit)

        type_combo = QComboBox()
        type_combo.addItems(_TYPES)
        type_combo.setCurrentText(existing.get('type', 'hotkey'))
        form.addRow("Command type:", type_combo)

        dlg_layout.addLayout(form)

        # Dynamic fields area — QStackedWidget
        stack = QStackedWidget()
        stack.setMinimumHeight(70)

        # Page 0: hotkey
        p_hotkey = QWidget()
        pl0 = QFormLayout(p_hotkey)
        pl0.setContentsMargins(0, 0, 0, 0)
        keys_edit = QLineEdit('+'.join(existing.get('keys', [])))
        keys_edit.setPlaceholderText("e.g. ctrl+shift+a")
        pl0.addRow("Keys:", keys_edit)
        hint0 = QLabel("Use + to combine keys: ctrl, shift, alt, a-z, 0-9, f1-f12, etc.")
        hint0.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: 11px;")
        pl0.addRow("", hint0)
        stack.addWidget(p_hotkey)   # 0

        # Page 1: text
        p_text = QWidget()
        pl1 = QFormLayout(p_text)
        pl1.setContentsMargins(0, 0, 0, 0)
        text_edit = QLineEdit(existing.get('text', ''))
        text_edit.setPlaceholderText("Text to insert")
        pl1.addRow("Text:", text_edit)
        stack.addWidget(p_text)     # 1

        # Page 2: launch
        p_launch = QWidget()
        pl2 = QFormLayout(p_launch)
        pl2.setContentsMargins(0, 0, 0, 0)
        target_edit = QLineEdit(existing.get('target', ''))
        target_edit.setPlaceholderText("e.g. chrome.exe or full path")
        pl2.addRow("Program:", target_edit)
        stack.addWidget(p_launch)   # 2

        # Page 3: press / key_down / key_up
        p_key = QWidget()
        pl3 = QFormLayout(p_key)
        pl3.setContentsMargins(0, 0, 0, 0)
        key_edit = QLineEdit(existing.get('key', ''))
        key_edit.setPlaceholderText("e.g. space, enter, a, shift")
        pl3.addRow("Key:", key_edit)
        stack.addWidget(p_key)      # 3

        # Page 4: mouse
        p_mouse = QWidget()
        pl4 = QFormLayout(p_mouse)
        pl4.setContentsMargins(0, 0, 0, 0)
        action_combo = QComboBox()
        action_combo.addItems(['click', 'double_click'])
        action_combo.setCurrentText(existing.get('action', 'click'))
        pl4.addRow("Action:", action_combo)
        button_combo = QComboBox()
        button_combo.addItems(['left', 'right', 'middle'])
        button_combo.setCurrentText(existing.get('button', 'left'))
        pl4.addRow("Button:", button_combo)
        stack.addWidget(p_mouse)    # 4

        # Page 5: release_all
        p_release = QWidget()
        pl5 = QVBoxLayout(p_release)
        pl5.setContentsMargins(0, 0, 0, 0)
        pl5.addWidget(QLabel("No additional settings — this releases all held keys."))
        stack.addWidget(p_release)  # 5

        _TYPE_PAGE = {
            'hotkey': 0, 'text': 1, 'launch': 2,
            'press': 3, 'key_down': 3, 'key_up': 3,
            'mouse': 4, 'release_all': 5,
        }
        stack.setCurrentIndex(_TYPE_PAGE.get(type_combo.currentText(), 0))
        type_combo.currentTextChanged.connect(
            lambda t: stack.setCurrentIndex(_TYPE_PAGE.get(t, 0))
        )
        dlg_layout.addWidget(stack)

        desc_form = QFormLayout()
        desc_form.setContentsMargins(0, 0, 0, 0)
        desc_edit = QLineEdit(existing.get('description', ''))
        desc_edit.setPlaceholderText("Optional description")
        desc_form.addRow("Description:", desc_edit)
        dlg_layout.addLayout(desc_form)

        # Buttons
        dlg_layout.addSpacing(4)
        btn_row2 = QHBoxLayout()
        btn_row2.addStretch()
        cancel_btn2 = QPushButton("Cancel")
        cancel_btn2.setFixedWidth(90)
        cancel_btn2.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {theme.ICON_IDLE}; "
            "border: 1px solid rgba(255,255,255,0.14); border-radius: 6px; padding: 8px 16px; }"
            f"QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; }}"
        )
        cancel_btn2.clicked.connect(dlg.reject)
        btn_row2.addWidget(cancel_btn2)

        save_btn2 = QPushButton("Save")
        save_btn2.setFixedWidth(90)
        save_btn2.clicked.connect(lambda: self._dialog_save(
            dlg, edit_phrase, exe, table,
            phrase_edit, type_combo, keys_edit, text_edit,
            target_edit, key_edit, action_combo, button_combo, desc_edit,
        ))
        btn_row2.addWidget(save_btn2)
        dlg_layout.addLayout(btn_row2)

        dlg.exec()

    def _dialog_save(
        self, dlg, edit_phrase, exe, table,
        phrase_edit, type_combo, keys_edit, text_edit,
        target_edit, key_edit, action_combo, button_combo, desc_edit,
    ) -> None:
        phrase = phrase_edit.text().strip().lower()
        if not phrase:
            QMessageBox.warning(dlg, "Error", "Voice phrase is required.")
            return

        if not edit_phrase or phrase != edit_phrase.lower():
            if phrase in exe.commands:
                QMessageBox.warning(
                    dlg, "Error",
                    f"A command '{phrase}' already exists."
                )
                return

        t = type_combo.currentText()
        data: dict = {'type': t, 'description': desc_edit.text().strip()}

        if t == 'hotkey':
            keys = [k.strip().lower() for k in keys_edit.text().split('+') if k.strip()]
            if not keys:
                QMessageBox.warning(dlg, "Error", "Specify at least one key.")
                return
            data['keys'] = keys
        elif t == 'text':
            txt = text_edit.text().strip()
            if not txt:
                QMessageBox.warning(dlg, "Error", "Specify text to insert.")
                return
            data['text'] = txt
        elif t == 'launch':
            tgt = target_edit.text().strip()
            if not tgt:
                QMessageBox.warning(dlg, "Error", "Specify a program to launch.")
                return
            data['target'] = tgt
        elif t in ('press', 'key_down', 'key_up'):
            k = key_edit.text().strip().lower()
            if not k:
                QMessageBox.warning(dlg, "Error", "Specify a key.")
                return
            data['key'] = k
        elif t == 'mouse':
            data['action'] = action_combo.currentText()
            data['button'] = button_combo.currentText()

        if edit_phrase and phrase != edit_phrase.lower():
            exe.commands.pop(edit_phrase, None)

        exe.commands[phrase] = data
        self._save_commands_to_disk()

        search = self._widgets.get('cmd_search')
        self._populate_commands_table(table, search.text() if search else '')

        dlg.accept()
        QMessageBox.information(self, "Saved", f"Command '{phrase}' saved.")
