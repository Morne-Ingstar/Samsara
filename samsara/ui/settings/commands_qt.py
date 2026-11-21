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
from samsara.ui.widgets.checkbox_list import CheckBoxList

# Paths below stay relative to settings_qt.py, where this code was written.
from samsara.ui.settings_qt import __file__ as _SETTINGS_QT_FILE
from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH, _collect_command_rows, logger


from samsara.ui.settings.presentation import CleanComboBox as QComboBox, TOOLS_TABLE_MAX_WIDTH, DataTable, table_empty_state, data_heading, style_data_list


_LEADING_PACKS = ('accessibility', 'text-editing', 'window-management')
_INTEGRATION_PACKS = {'ai', 'agents', 'hermes'}


def matching_pack(query):
    from samsara.command_packs import PACKS

    for pack_id, meta in PACKS.items():
        if pack_id in _INTEGRATION_PACKS:
            continue
        if query in (pack_id, f'command_packs.{pack_id}', meta['label'].casefold()):
            return pack_id
    return None


class CommandsPage:
    """Methods of the Commands settings page (moved from _SettingsWindow)."""

    def _build_commands_tab(self):
        import json
        from pathlib import Path
        from samsara.command_packs import PACKS
        from .calm_controls import switch

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
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(0)

        cfg = self.app.config

        # ---- Section: Voice Commands ---------------------------------------
        cmd_header = QHBoxLayout()
        commands_title = data_heading(self._tool_section_title("Voice Commands"))
        cmd_header.addWidget(commands_title)
        cmd_header.setSpacing(8)
        quick_reference_button = QPushButton("Command list...")
        quick_reference_button.setObjectName("commandListButton")
        quick_reference_button.setMinimumHeight(44)
        quick_reference_button.clicked.connect(
            lambda: getattr(self.app, "open_quick_reference", lambda: None)()
        )
        self._widgets["commandListButton"] = quick_reference_button
        cmd_header.addWidget(quick_reference_button)
        search_box = QLineEdit()
        search_box.setAccessibleName("Search commands")
        search_box.setPlaceholderText("Search commands...")
        search_box.setMinimumWidth(140)
        search_box.setMaximumWidth(280)
        search_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._widgets['cmd_search'] = search_box
        cmd_header.addWidget(search_box)
        cmd_header.addStretch()
        layout.addSpacing(24)
        layout.addLayout(cmd_header)
        layout.addSpacing(8)

        # Table
        table = DataTable(0, 4)
        table.setAccessibleName(commands_title.text())
        table.setHorizontalHeaderLabels(['Voice Phrase', 'Type', 'Action', 'Description'])
        table.columns((176, 80, 128, 224), flexible=3)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setTabKeyNavigation(False)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._widgets['cmd_table'] = table
        self._populate_commands_table(table, "")
        search_box.textChanged.connect(lambda txt: self._filter_commands(table, txt))
        table_row = QHBoxLayout()
        table_row.setContentsMargins(0, 0, 0, 0)
        table_row.addWidget(table, 1)
        table_row.addStretch(0)
        layout.addLayout(table_row)

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
            f"border: 1px solid {theme.wash(0.14)}; border-radius: 6px; "
            "padding: 8px 16px; }"
            f"QPushButton:hover {{ background-color: {theme.wash(0.05)}; color: {theme.TEXT_PRIMARY}; }}"
        )
        edit_btn.clicked.connect(lambda: self._edit_selected_command(table))
        btn_row.addWidget(edit_btn)

        del_btn = QPushButton("Delete")
        del_btn.setObjectName("deleteCommandButton")
        del_btn.setStyleSheet(
            f"QPushButton {{ background-color: {theme.tint(theme.RECORDING, 0.15)}; color: {theme.ERROR}; "
            f"border: 1px solid {theme.tint(theme.RECORDING, 0.3)}; border-radius: 6px; padding: 8px 16px; }}"
            f"QPushButton:hover {{ background-color: {theme.tint(theme.RECORDING, 0.25)}; }}"
        )
        del_btn.clicked.connect(lambda: self._delete_selected_command(table))
        btn_row.addWidget(del_btn)

        test_btn = QPushButton("Test")
        test_btn.setObjectName("testCommandButton")
        test_btn.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {theme.ICON_IDLE}; "
            f"border: 1px solid {theme.wash(0.14)}; border-radius: 6px; "
            "padding: 8px 12px; }"
            f"QPushButton:hover {{ background-color: {theme.wash(0.05)}; color: {theme.TEXT_PRIMARY}; }}"
        )
        test_btn.clicked.connect(lambda: self._test_selected_command(table))
        btn_row.addWidget(test_btn)

        reload_btn = QPushButton("Reload")
        reload_btn.setObjectName("reloadCommandsButton")
        reload_btn.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {theme.ICON_IDLE}; "
            f"border: 1px solid {theme.wash(0.14)}; border-radius: 6px; "
            "padding: 8px 12px; }"
            f"QPushButton:hover {{ background-color: {theme.wash(0.05)}; color: {theme.TEXT_PRIMARY}; }}"
        )
        reload_btn.clicked.connect(lambda: self._reload_commands(table))
        btn_row.addWidget(reload_btn)
        btn_row.addStretch()

        layout.addSpacing(12)
        layout.addLayout(btn_row)
        layout.addSpacing(8)
        instant_note = QLabel("Changes to voice commands apply immediately.")  # PLACEHOLDER COPY (1077)
        instant_note.setWordWrap(True)
        instant_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(instant_note)
        footer = QLabel("Say these phrases while recording to trigger actions.")
        # Wrapping keeps the footer from widening the scroll content.
        footer.setWordWrap(True)
        footer.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(footer)

        layout.addSpacing(24)

        # ---- Section: Command Packs ----------------------------------------
        layout.addWidget(data_heading(self._tool_section_title("Command packs")))

        desc1 = QLabel(
            "Enable the packs you use. Disabling unused packs improves recognition accuracy."
        )
        desc1.setWordWrap(True)
        desc1.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        layout.addSpacing(8)
        layout.addWidget(desc1)
        layout.addSpacing(16)

        packs = CheckBoxList("Command packs", 384, TOOLS_TABLE_MAX_WIDTH)
        packs.setObjectName("commandPacksList")
        pack_container = packs.content
        pack_container.setObjectName("commandPacksContent")
        hidden_packs = QWidget(outer)
        hidden_packs.hide()

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
        restart_lbl.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.TYPE_BODY}px;")
        restart_lbl.setVisible(False)
        self._widgets['_pack_restart_lbl'] = restart_lbl

        order = (*_LEADING_PACKS,
                 *(key for key in PACKS if key not in (*_LEADING_PACKS, 'experimental')),
                 'experimental')
        for pack_id in order:
            if pack_id == 'hermes':
                continue  # Dedicated, consent-gated section below; never a second switch.
            meta = PACKS[pack_id]
            always_on = meta.get('always_on', False)
            default_on = meta.get('default_enabled', False)
            enabled = bool(current_packs.get(pack_id, default_on)) or always_on
            count = pack_counts.get(pack_id, 0)
            count_str = f"  ({count})" if count else ""

            cb = QCheckBox(hidden_packs) if pack_id in _INTEGRATION_PACKS else QCheckBox()
            cb.setAccessibleName(meta['label'])
            cb.setChecked(enabled)
            cb.setEnabled(not always_on)
            if pack_id != 'agents':
                cb.toggled.connect(lambda _, lbl=restart_lbl: lbl.setVisible(True))
            pack_checkboxes[pack_id] = cb
            if pack_id in _INTEGRATION_PACKS:
                continue

            row_w = QWidget()
            row_w.setProperty('dataListRow', True)
            row_w.setMinimumHeight(64)
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(0, 7, 12, 7)
            row_h.setSpacing(8)
            if not always_on:
                switch(cb)
            text_col = QVBoxLayout()
            text_col.setContentsMargins(0, 0, 0, 0)
            text_col.setSpacing(4)

            name_lbl = QLabel(
                meta.get('label', pack_id)
                + ("  •  always on" if always_on else "")
                + count_str
            )
            name_lbl.setStyleSheet(
                f"color: {theme.ICON_IDLE if always_on else theme.TEXT_PRIMARY}; "
                f"font-size: {theme.TYPE_BODY}px; font-weight: {'normal' if always_on else '600'};"
                "background: transparent;"
            )
            name_lbl.setMinimumHeight(22)
            text_col.addWidget(name_lbl)

            desc_lbl = QLabel(meta.get('description', ''))
            desc_lbl.setProperty('dataSecondary', True)
            desc_lbl.setObjectName(f"commandPackDescription_{pack_id}")
            desc_lbl.setStyleSheet(
                f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px; background: transparent;"
            )
            desc_lbl.setMinimumHeight(22)
            desc_lbl.setWordWrap(True)
            desc_lbl.setMinimumWidth(0)
            desc_lbl.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            text_col.addWidget(desc_lbl)
            row_h.addLayout(text_col, stretch=1)
            row_h.addWidget(cb, alignment=Qt.AlignmentFlag.AlignVCenter)

            if always_on:
                cb.hide()  # The adjacent manifest label is a capability badge, not a preference.
            packs.add_row(row_w, None if always_on else cb, name_lbl)

        style_data_list(pack_container)
        self._widgets['_pack_checkboxes'] = pack_checkboxes
        self._widgets['_pack_scroll'] = packs
        pack_row = QHBoxLayout()
        pack_row.setContentsMargins(0, 0, 0, 0)
        pack_row.addWidget(packs, 1)
        pack_row.addStretch(0)
        layout.addLayout(pack_row)
        layout.addWidget(restart_lbl)
        layout.addSpacing(8)
        from .hermes_section import add_section
        add_section(self, layout)

        # ---- Todoist ------------------------------------------------------
        command_layout = layout
        todoist_section = QWidget()
        todoist_section.setObjectName('todoistSection')
        layout = QVBoxLayout(todoist_section)
        command_layout.addWidget(todoist_section)
        layout.addWidget(self._tool_section_title("Todoist"))
        todoist_cfg = (cfg.get("tasks", {}) or {}).get("todoist", {}) or {}
        todoist_note = QLabel("When on, tasks you add by voice are sent to Todoist's servers.")
        todoist_note.setWordWrap(True)
        todoist_note.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(todoist_note)
        todoist_enabled = QCheckBox("Send new voice tasks to Todoist")
        todoist_enabled.setChecked(bool(todoist_cfg.get("enabled", False)))
        todoist_enabled.setMinimumHeight(theme.HIT_TARGET_MIN)
        layout.addWidget(todoist_enabled)
        todoist_layout = layout
        credentials = QWidget()
        credentials.setObjectName('todoistCredentials')
        layout = QVBoxLayout(credentials)
        todoist_layout.addWidget(credentials)
        token_row = QHBoxLayout()
        token_edit = QLineEdit(str(todoist_cfg.get("api_token", "")))
        token_edit.setObjectName("todoistTokenField")
        token_edit.setPlaceholderText("Todoist API token")
        token_edit.setAccessibleName(token_edit.placeholderText())
        token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        token_edit.setMinimumHeight(theme.HIT_TARGET_MIN)
        show_token = QPushButton("Show")
        show_token.setMinimumHeight(theme.HIT_TARGET_MIN)
        show_token.setMinimumWidth(theme.HIT_TARGET_MIN)
        def _toggle_token():
            shown = token_edit.echoMode() == QLineEdit.EchoMode.Normal
            token_edit.setEchoMode(QLineEdit.EchoMode.Password if shown else QLineEdit.EchoMode.Normal)
            show_token.setText("Show" if shown else "Hide")
        show_token.clicked.connect(_toggle_token)
        token_row.addWidget(token_edit, stretch=1)
        token_row.addWidget(show_token)
        layout.addLayout(token_row)
        todoist_result = QLabel("")
        todoist_result.setWordWrap(True)
        todoist_result.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        test_todoist = QPushButton("Test connection")
        test_todoist.setMinimumHeight(theme.HIT_TARGET_MIN)
        def _test_todoist():
            token = token_edit.text().strip()
            if not token:
                todoist_result.setText("Enter a Todoist token first.")
                todoist_result.setStyleSheet(f"color: {theme.ERROR}; font-size: {theme.TYPE_BODY}px;")
                return
            todoist_result.setText("Testing Todoist…")
            def show_result(msg, color):
                todoist_result.setText(msg)
                todoist_result.setStyleSheet(f"color: {color}; font-size: {theme.TYPE_BODY}px;")
            finish = self._visit_callback(show_result, todoist_result)
            def _run():
                from samsara import todoist_client
                try:
                    todoist_client.today(token)
                    msg, color = "Todoist connection works.", theme.ACCENT
                except Exception:
                    msg, color = "Todoist connection failed.", theme.ERROR
                qt_runtime.post(lambda: finish(msg, color))
            thread_registry.spawn("settings-todoist-test", _run, daemon=True)
        test_todoist.clicked.connect(_test_todoist)
        layout.addWidget(test_todoist)
        layout.addWidget(todoist_result)
        self._widgets.update(todoist_enabled=todoist_enabled, todoist_token=token_edit)
        layout.addSpacing(8)

        layout = command_layout

        # ---- Section: Command Profiles ------------------------------------
        profiles_title = self._tool_section_title("Command Profiles")
        profiles_title.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(profiles_title)
        layout.addSpacing(8)
        profile_entry = QWidget()
        profile_entry.setObjectName("commandProfilesEntry")
        profile_row = QHBoxLayout(profile_entry)
        profile_row.setContentsMargins(0, 0, 0, 0)
        profile_note = QLabel(
            "Profiles: export / import / manage. Default is built-in and read-only."
        )
        profile_note.setWordWrap(True)
        profile_note.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;"
        )
        profile_row.addWidget(profile_note)
        profile_button = QPushButton("Manage Profiles…")
        profile_button.setObjectName("manageCommandProfilesButton")
        profile_button.clicked.connect(lambda: self._open_command_profiles(table=table))
        profile_row.addWidget(profile_button)
        profile_row.addStretch()
        layout.addWidget(profile_entry)
        self._widgets['_command_profiles_button'] = profile_button
        self._widgets['_command_profiles_entry'] = profile_entry

        def _save(acc):
            updates = {}
            if 'hermes_section' in self._widgets:
                updates['hermes'] = self._widgets['hermes_section'].updates()
            if '_pack_checkboxes' in self._widgets:
                from samsara.command_packs import PACKS
                new_packs = dict(self.app.config.get('command_packs', {}) or {})
                new_packs.setdefault('hermes', PACKS['hermes']['default_enabled'])
                for pack_id, cb in self._widgets['_pack_checkboxes'].items():
                    meta = PACKS.get(pack_id, {})
                    if not meta.get('always_on'):
                        new_packs[pack_id] = cb.isChecked()
                updates['command_packs'] = new_packs
            updates['tasks'] = {**(self.app.config.get('tasks', {}) or {}), 'todoist': {
                'enabled': self._widgets['todoist_enabled'].isChecked(),
                'api_token': self._widgets['todoist_token'].text().strip(),
            }}
            return updates
        self._save_fns.append(_save)

        layout.addStretch()
        page.setWidget(outer)
        from .calm_layout import _style_row_button
        for button in outer.findChildren(QPushButton):
            button.setMinimumWidth(0)
            button.setFixedHeight(theme.HIT_TARGET_MIN)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            button.setProperty('settingsToolAction', True)
            _style_row_button(button)
        return page

    def _open_command_profiles(self, table=None) -> None:
        """Open the profile manager from the Commands page."""
        from pathlib import Path
        from samsara.profiles import ProfileManager
        from samsara.ui.profile_manager_qt import ProfileManagerQt

        if not hasattr(self, '_command_profile_manager_qt'):
            config_path = getattr(self.app, 'config_path', None)
            app_dir = Path(config_path).parent if config_path else Path(_SETTINGS_QT_FILE).parent
            executor = getattr(self.app, 'command_executor', None)
            commands_path = getattr(executor, 'commands_path', None)

            def _on_changed():
                try:
                    if executor is not None and hasattr(executor, 'reload_commands'):
                        executor.reload_commands()
                    elif executor is not None and hasattr(executor, 'load_commands'):
                        executor.load_commands()
                except Exception as exc:
                    logger.debug(f"_open_command_profiles reload: {exc}")
                if table is not None:
                    search = self._widgets.get('cmd_search')
                    self._populate_commands_table(table, search.text() if search else '')

            pm = ProfileManager(app_dir, commands_path=commands_path)
            self._command_profile_manager_qt = ProfileManagerQt(pm, _on_changed)
        self._command_profile_manager_qt.show()
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
                table.setRowHeight(row, theme.HIT_TARGET_MIN)
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
        # PLACEHOLDER COPY (840)
        table_empty_state(table, "No commands match your search." if filter_text else "No commands loaded.")

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
        report = self._visit_callback(self._on_test_result)
        restore = self._visit_callback(self.showNormal)
        import threading, time
        def _run():
            time.sleep(0.4)
            try:
                _result, ok = exe.process_text(
                    phrase, self.app, force_commands=True,
                )
                msg = f"'{phrase}' executed OK." if ok else f"'{phrase}' not found or failed."
                qt_runtime.post(lambda: report(msg, theme.ACCENT if ok else theme.ERROR))
            except Exception as exc:
                message = f"Error: {exc}"
                qt_runtime.post(lambda: report(message, theme.ERROR))
            # showNormal() is a QWidget method -- must run on the Qt thread,
            # not this worker thread. qt_runtime.post() is the established
            # marshal-a-callable-onto-the-Qt-thread idiom used throughout
            # the codebase (already imported in this module).
            qt_runtime.post(restore)
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
        hint0.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
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
            f"border: 1px solid {theme.wash(0.14)}; border-radius: 6px; padding: 8px 16px; }}"
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
