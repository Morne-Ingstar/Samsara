"""Alarms page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
AlarmsPage is mixed into settings_qt._SettingsWindow, so every method still runs
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
    QSizePolicy,
    QSpinBox,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from samsara.runtime import thread_registry
from samsara.ui import theme

from samsara.ui.settings_qt import (
    _AlarmHotkeyButton,
    _CONTENT_MAX_WIDTH,
    _HotkeyButton,
    _format_alarm_next,
    logger,
)


class AlarmsPage:
    """Methods of the Alarms settings page (moved from _SettingsWindow)."""

    def _build_alarms_tab(self):
        from samsara.alarms import get_default_alarm_config

        outer = QWidget()
        outer.setMaximumWidth(_CONTENT_MAX_WIDTH)
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 12)
        layout.setSpacing(8)

        alarm_cfg = self.app.config.get('alarms', get_default_alarm_config()) or {}

        # ---- Section: Global Settings ---------------------------------------
        layout.addWidget(self._section_title("Alarm Settings"))
        layout.addSpacing(4)

        alarms_enabled = QCheckBox("Enable alarm reminders")
        alarms_enabled.setChecked(bool(alarm_cfg.get('enabled', True)))
        self._widgets['alarms_enabled'] = alarms_enabled
        layout.addWidget(alarms_enabled)
        layout.addSpacing(8)

        complete_key = _AlarmHotkeyButton(
            alarm_cfg.get('complete_hotkey', 'f7'), "Complete alarm"
        )
        self._widgets['alarms_complete_key'] = complete_key
        layout.addLayout(self._setting_row(
            "Complete alarm shortcut",
            "Stops the sound and records that you completed the task",
            complete_key,
        ))
        layout.addSpacing(6)

        dismiss_key = _AlarmHotkeyButton(
            alarm_cfg.get('dismiss_hotkey', 'f8'), "Dismiss alarm"
        )
        self._widgets['alarms_dismiss_key'] = dismiss_key
        layout.addLayout(self._setting_row(
            "Dismiss alarm shortcut",
            "Stops the sound without recording a completed task",
            dismiss_key,
        ))
        layout.addSpacing(6)

        nag_spin = QSpinBox()
        nag_spin.setRange(15, 300)
        nag_spin.setSingleStep(15)
        nag_spin.setSuffix(" s")
        nag_spin.setValue(int(alarm_cfg.get('nag_interval_seconds', 60)))
        self._widgets['alarms_nag'] = nag_spin
        layout.addLayout(self._setting_row(
            "Repeat interval",
            "How often to replay the alarm sound until completed or dismissed",
            nag_spin,
        ))
        layout.addSpacing(12)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background-color: {theme.wash(0.06)}; max-height: 1px;")
        layout.addWidget(sep)
        layout.addSpacing(4)

        # ---- Section: Alarm List --------------------------------------------
        layout.addWidget(self._section_title("Your Alarms"))
        instant_note = QLabel("Changes to alarms below apply immediately.")
        instant_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(instant_note)
        layout.addSpacing(4)

        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(['On', 'Name', 'Interval', 'Next', 'Streak'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setMinimumHeight(200)
        self._widgets['alarms_table'] = table
        self._populate_alarms_table(table)
        # This tab used to be the one non-scrollable Settings page. With
        # populated rows, its expanding table competed with the action row
        # for the same fixed viewport height and painted over it. Let the
        # content take its natural height inside the shared Settings scroll
        # pattern instead.
        layout.addWidget(table)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        _SEC = (
            f"QPushButton{{background-color:transparent;color:{theme.ICON_IDLE};"
            f"border:1px solid {theme.wash(0.14)};border-radius:6px;padding:7px 12px;}}"
            f"QPushButton:hover{{background-color:{theme.wash(0.05)};color:{theme.TEXT_PRIMARY};}}"
        )

        add_btn = QPushButton("Add Alarm")
        add_btn.setMinimumWidth(130)  # sizeHint is 116; a few px of margin
        add_btn.setMinimumHeight(theme.HIT_TARGET_MIN)
        add_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        add_btn.clicked.connect(lambda: self._open_alarm_dialog(None, table))
        btn_row.addWidget(add_btn)

        for label, width, handler in [
            ("Edit",      65, lambda: self._edit_selected_alarm(table)),
            ("Toggle",    80, lambda: self._toggle_selected_alarm(table)),  # sizeHint 69; a few px of margin
            ("Test",      55, lambda: self._test_selected_alarm(table)),
        ]:
            b = QPushButton(label)
            b.setMinimumWidth(width)
            b.setMinimumHeight(theme.HIT_TARGET_MIN)
            b.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            b.setStyleSheet(_SEC)
            b.clicked.connect(handler)
            btn_row.addWidget(b)

        del_btn = QPushButton("Delete")
        del_btn.setMinimumWidth(80)  # sizeHint is 67; a few px of margin
        del_btn.setMinimumHeight(theme.HIT_TARGET_MIN)
        del_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        del_btn.setStyleSheet(
            f"QPushButton{{background-color:{theme.tint(theme.RECORDING, 0.15)};color:{theme.ERROR};"
            f"border:1px solid {theme.tint(theme.RECORDING, 0.3)};border-radius:6px;padding:7px 12px;}}"
            f"QPushButton:hover{{background-color:{theme.tint(theme.RECORDING, 0.25)};}}"
        )
        del_btn.clicked.connect(lambda: self._delete_selected_alarm(table))
        btn_row.addWidget(del_btn)

        btn_row.addStretch()

        reset_btn = QPushButton("Reset Stats")
        reset_btn.setMinimumWidth(105)  # sizeHint is 95; a few px of margin
        reset_btn.setMinimumHeight(theme.HIT_TARGET_MIN)
        reset_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        reset_btn.setStyleSheet(_SEC)
        reset_btn.clicked.connect(lambda: self._reset_alarm_stats(table))
        btn_row.addWidget(reset_btn)

        layout.addLayout(btn_row)

        def _save(_acc):
            updates = {}
            if 'alarms_enabled' in self._widgets:
                from samsara.alarms import get_default_alarm_config
                alarms_cfg = dict(
                    self.app.config.get('alarms', get_default_alarm_config()) or {}
                )
                alarms_cfg['enabled'] = self._widgets['alarms_enabled'].isChecked()
                for wkey, cfg_key in [
                    ('alarms_complete_key', 'complete_hotkey'),
                    ('alarms_dismiss_key',  'dismiss_hotkey'),
                ]:
                    btn = self._widgets.get(wkey)
                    if isinstance(btn, _HotkeyButton):
                        alarms_cfg[cfg_key] = btn.combo
                alarms_cfg['nag_interval_seconds'] = self._widgets['alarms_nag'].value()
                updates['alarms'] = alarms_cfg
                am = getattr(self.app, 'alarm_manager', None)
                if am:
                    if alarms_cfg['enabled'] and not getattr(am, 'running', False):
                        am.start()
                    elif not alarms_cfg['enabled'] and getattr(am, 'running', False):
                        am.stop()
            return updates
        self._save_fns.append(_save)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )
        scroll.setWidget(outer)
        return scroll

    # Alarms tab helpers

    def _populate_alarms_table(
        self, table: QTableWidget, select_alarm_id=None, fallback_row=None
    ) -> None:
        from samsara.alarms import get_default_alarm_config
        if select_alarm_id is None:
            select_alarm_id = self._selected_alarm_id(table)
        if fallback_row is None:
            fallback_row = table.currentRow()
        selected_row = None
        table.setUpdatesEnabled(False)
        table.setRowCount(0)
        try:
            alarm_cfg = self.app.config.get('alarms', get_default_alarm_config()) or {}
            am = getattr(self.app, 'alarm_manager', None)
            for alarm in alarm_cfg.get('items', []):
                alarm_id = alarm.get('id', alarm.get('name', 'unknown'))
                row = table.rowCount()
                table.insertRow(row)
                table.setRowHeight(row, theme.HIT_TARGET_MIN)
                enabled = alarm.get('enabled', False)
                enabled_item = QTableWidgetItem(
                    "✓" if alarm.get('enabled', False) else "—"
                )
                enabled_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                enabled_item.setData(Qt.ItemDataRole.UserRole, alarm_id)
                table.setItem(row, 0, enabled_item)
                if select_alarm_id is not None and alarm_id == select_alarm_id:
                    selected_row = row
                table.setItem(row, 1, QTableWidgetItem(alarm.get('name', 'Unnamed')))
                table.setItem(row, 2, QTableWidgetItem(
                    f"{alarm.get('interval_minutes', 60)} min"
                ))
                next_at = None
                active = False
                if am:
                    try:
                        next_at = am.get_next_trigger_at(alarm_id)
                        active = am.nagging_alarm_id == alarm_id
                    except Exception as e:
                        logger.debug(f"_populate_alarms_table next trigger: {e}")
                table.setItem(row, 3, QTableWidgetItem(_format_alarm_next(
                    next_at,
                    enabled=enabled,
                    active=active,
                )))
                if am:
                    stats   = am.get_stats(alarm_id)
                    cur     = stats.get('current_streak', 0)
                    best    = stats.get('best_streak', 0)
                    streak  = f"{cur} / {best}" if (cur or best) else "—"
                else:
                    streak = "—"
                table.setItem(row, 4, QTableWidgetItem(streak))
        finally:
            table.setUpdatesEnabled(True)
        if selected_row is None and fallback_row is not None and fallback_row >= 0:
            if table.rowCount():
                selected_row = min(fallback_row, table.rowCount() - 1)
        if selected_row is not None:
            table.selectRow(selected_row)

    def _selected_alarm_id(self, table: QTableWidget):
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _edit_selected_alarm(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to edit.")
            return
        self._open_alarm_dialog(alarm_id, table)

    def _delete_selected_alarm(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to delete.")
            return
        am = getattr(self.app, 'alarm_manager', None)
        alarm = am.get_alarm(alarm_id) if am else None
        name  = alarm.get('name', alarm_id) if alarm else alarm_id
        reply = QMessageBox.question(
            self, "Confirm Delete", f"Delete the alarm '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and am:
            am.remove_alarm(alarm_id)
            self._populate_alarms_table(table)

    def _toggle_selected_alarm(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to toggle.")
            return
        am = getattr(self.app, 'alarm_manager', None)
        if am:
            selected_row = table.currentRow()
            am.toggle_alarm(alarm_id)
            self._populate_alarms_table(
                table, select_alarm_id=alarm_id, fallback_row=selected_row
            )

    def _test_selected_alarm(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to test.")
            return
        am = getattr(self.app, 'alarm_manager', None)
        if am:
            alarm = am.get_alarm(alarm_id)
            if alarm:
                thread_registry.spawn(
                    "settings_qt._test_selected_alarm",
                    lambda: am.play_sound(alarm), daemon=True
                )

    def _reset_alarm_stats(self, table: QTableWidget) -> None:
        alarm_id = self._selected_alarm_id(table)
        if not alarm_id:
            QMessageBox.warning(self, "No Selection", "Select an alarm to reset.")
            return
        am    = getattr(self.app, 'alarm_manager', None)
        alarm = am.get_alarm(alarm_id) if am else None
        name  = alarm.get('name', alarm_id) if alarm else alarm_id
        reply = QMessageBox.question(
            self, "Reset Stats", f"Reset all streak stats for '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and am:
            am.reset_stats(alarm_id)
            self._populate_alarms_table(table)

    def _open_alarm_dialog(self, edit_id, table: QTableWidget) -> None:
        from samsara.alarms import get_default_alarm_config
        am       = getattr(self.app, 'alarm_manager', None)
        existing = (am.get_alarm(edit_id) or {}) if (edit_id and am) else {}

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Alarm" if edit_id else "Add Alarm")
        dlg.setMinimumWidth(360)
        dlg.setStyleSheet(self.styleSheet())

        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setContentsMargins(20, 20, 20, 16)
        dlg_layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        name_edit = QLineEdit(existing.get('name', ''))
        name_edit.setPlaceholderText("e.g. Hydration")
        form.addRow("Name:", name_edit)

        interval_spin = QSpinBox()
        interval_spin.setRange(1, 480)
        interval_spin.setSuffix(" min")
        interval_spin.setValue(int(existing.get('interval_minutes', 60)))
        form.addRow("Interval:", interval_spin)

        sound_opts = ['alarm', 'chime', 'bell', 'gentle']
        if am:
            try:
                sound_opts = [s['value'] for s in am.get_available_sounds()]
            except Exception as e:
                logger.debug(f"_open_alarm_dialog: {e}")
        sound_combo = QComboBox()
        sound_combo.addItems(sound_opts)
        current_snd = existing.get('sound', 'alarm')
        if current_snd in sound_opts:
            sound_combo.setCurrentText(current_snd)
        form.addRow("Sound:", sound_combo)

        enabled_cb = QCheckBox("Enabled")
        enabled_cb.setChecked(bool(existing.get('enabled', True)))
        form.addRow("", enabled_cb)

        dlg_layout.addLayout(form)

        btn_row2 = QHBoxLayout()
        btn_row2.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(80)
        cancel_btn.setStyleSheet(
            f"QPushButton{{background-color:transparent;color:{theme.ICON_IDLE};"
            f"border:1px solid {theme.wash(0.14)};border-radius:6px;padding:8px 14px;}}"
        )
        cancel_btn.clicked.connect(dlg.reject)
        btn_row2.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setFixedWidth(80)
        save_btn.clicked.connect(lambda: self._alarm_dialog_save(
            dlg, edit_id, am, table,
            name_edit, interval_spin, sound_combo, enabled_cb,
        ))
        btn_row2.addWidget(save_btn)
        dlg_layout.addLayout(btn_row2)
        dlg.exec()

    def _alarm_dialog_save(
        self, dlg, edit_id, am, table,
        name_edit, interval_spin, sound_combo, enabled_cb,
    ) -> None:
        from samsara.alarms import get_default_alarm_config
        name = name_edit.text().strip()
        if not name:
            QMessageBox.warning(dlg, "Error", "Alarm name is required.")
            return
        interval = interval_spin.value()
        sound    = sound_combo.currentText()
        enabled  = enabled_cb.isChecked()

        selected_id = edit_id
        if am:
            if edit_id:
                am.update_alarm(edit_id, name=name, interval_minutes=interval,
                                sound=sound, enabled=enabled)
            else:
                added = am.add_alarm(name=name, interval_minutes=interval,
                                     sound=sound, enabled=enabled)
                selected_id = added.get('id', added.get('name'))
        else:
            # Fallback: write directly to config when alarm_manager not running
            alarms_cfg = self.app.config.setdefault(
                'alarms', get_default_alarm_config()
            )
            items = alarms_cfg.setdefault('items', [])
            if edit_id:
                for item in items:
                    if item.get('id') == edit_id or item.get('name') == edit_id:
                        item.update({'name': name, 'interval_minutes': interval,
                                     'sound': sound, 'enabled': enabled})
                        selected_id = item.get('id', item.get('name'))
                        break
            else:
                items.append({
                    'id':               name.lower().replace(' ', '_'),
                    'name':             name,
                    'interval_minutes': interval,
                    'sound':            sound,
                    'enabled':          enabled,
                })
                selected_id = name.lower().replace(' ', '_')

        self._populate_alarms_table(
            table, select_alarm_id=selected_id, fallback_row=table.currentRow()
        )
        dlg.accept()
        QMessageBox.information(self, "Saved", f"Alarm '{name}' saved.")
