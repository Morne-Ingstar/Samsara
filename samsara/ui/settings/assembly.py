"""Compose destinations from existing controls without duplicating their state.

The source factories retain validation, consent and save callbacks. Controls
are reparented once into declared destinations; source scaffolding stays hidden.
"""
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QComboBox, QCheckBox, QAbstractSpinBox,
    QLineEdit, QMenu, QVBoxLayout, QHBoxLayout, QSizePolicy, QDialog, QScrollArea,
)
from samsara.ui import theme, qt_runtime
from samsara.config_schema import SETTINGS_SCHEMA
from .presentation import Page, SettingRow, CleanComboBox
from .calm_controls import PercentSpinBox, short_choice, switch
from .lazy_access import assembling, source_config


class PageAssembly:
    def _apply_metric_minimum_widths(self):
        super()._apply_metric_minimum_widths()
        from samsara.ui.widgets.beta_notice import BetaNotice
        for notice in self.findChildren(BetaNotice):
            notice.fit_to_text()

    @assembling
    def _apply_visible_design_scale(self):
        # Keep the real Settings owner: its theme hook installs row dividers,
        # input geometry and component-row observers. Hidden source scaffolding
        # must not receive that expensive widget-by-widget style pass.
        store = self._source_store
        store.setParent(None)
        try:
            inputs = getattr(self, '_settings_input_layout', None)
            if inputs is not None:
                inputs.window = self.centralWidget()
                inputs.refresh()
            theme.apply_design_scale(self)
            inputs = getattr(self, '_settings_input_layout', None)
            if inputs is not None:
                inputs.window = self.centralWidget()
        finally:
            store.setParent(self)
            store.hide()

    @assembling
    def _style_materialized_page(self, name, page):
        # Give the shared theme hook the same Settings context, scoped to this
        # page. Earlier destinations retain their styled controls and observers.
        page._destination_rows = [row for row in self._destination_rows if page.isAncestorOf(row)]
        page._pages = {name: page} if hasattr(page, 'column') else {}
        page._tool_pages = {name: page}
        page._components_page = self._components_page
        page._component_rows = self._component_rows
        theme.apply_design_scale(page)

    _SOURCE_BUILDERS = {
        'general': '_build_general_tab', 'modes': '_build_modes_tab',
        'commands': '_build_commands_tab', 'sounds': '_build_sounds_tab',
        'tts': '_build_tts_tab', 'ava': '_build_ava_cloud_tab',
        'alarms': '_build_alarms_tab', 'health': '_build_health_tab',
        'advanced': '_build_advanced_tab', 'help': '_build_support_tab',
        'music': '_build_music_tab',
    }
    _DESTINATION_SOURCES = {
        'General': ('general', 'modes', 'advanced'),
        'Dictation': ('general', 'modes', 'advanced'),
        'Audio': ('general', 'modes', 'advanced', 'sounds', 'tts'),
        'Ava': ('general', 'modes', 'advanced', 'ava'),
        'Shortcuts': ('modes', 'alarms'),
        'Help': ('general', 'advanced', 'help'),
    }

    @assembling
    def _ensure_source(self, name):
        if name in self._source_pages:
            return
        previous = self._save_fns
        self._save_fns = []
        try:
            with source_config(self):
                page = getattr(self, self._SOURCE_BUILDERS[name])()
            self._source_saves[name] = self._save_fns
        finally:
            self._save_fns = previous
        page.setParent(self._source_store)
        self._source_pages[name] = page
        if name == 'commands':
            # Integration controls must stay out of Command library even before
            # their own destination has been visited.
            section = page.findChild(QWidget, 'todoistSection')
            self._widgets['_todoist_section'] = section
            section.setParent(self._source_store)
            section.hide()

    @assembling
    def _ensure_destination(self, index):
        from samsara.ui.settings_qt import PAGE_MODULES
        module = PAGE_MODULES[index]
        name = module.TITLE
        if name in self._pages:
            return self._pages[name]
        for source in self._DESTINATION_SOURCES[name]:
            self._ensure_source(source)
        with source_config(self):
            self._prepare_actions(name)
            page = module.build(self)
        placeholder = self._stack.widget(index)
        self._stack.removeWidget(placeholder)
        self._stack.insertWidget(index, page)
        placeholder.deleteLater()
        if name in ('Shortcuts', 'Audio'):
            key = 'modes_collision_warn' if name == 'Shortcuts' else 'tts_test_status'
            page.column.insertWidget(2, self._widgets[key])
        if name == 'Ava':
            for key, row_key in [('cloud_test_status', 'cloud_api_key'), ('cloud_info_label', 'cloud_provider')]:
                row = self._rows_by_widget[row_key]
                row.grid.addWidget(self._widgets[key], row.grid.rowCount(), 0, 1, 3)
            page.smart_corrections.layout().insertWidget(1, self._widgets['sc_status_label'])
            self._sync_openrouter_picker()
        if name == 'Dictation':
            self._rows_by_widget['adv_device'].grid.addWidget(self._widgets['cuda_status'], 2, 0, 1, 3)
        if name == 'Help':
            page.column.insertWidget(2, self._source_store.findChild(QLabel, 'feedbackStatusLabel'))
        self._add_beta_notice(page)
        self._connect_conditions()
        self._build_search_registry()
        self._apply_metric_minimum_widths()
        if not self._building_contents:
            self._style_materialized_page(name, page)
        return page

    @assembling
    def _ensure_tool(self, name):
        from . import tools_page
        if name in self._tool_pages:
            return
        source = {'Command library': 'commands', 'Integrations': 'commands',
                  'Alarms': 'alarms', 'Health': 'health', 'Music': 'music',
                  'Components': 'advanced'}[name]
        self._ensure_source(source)
        with source_config(self):
            pages = tools_page.build(self, name)
        self._tool_pages.update(pages)
        for title, page in pages.items():
            if title == 'Command library':
                from .agents_section import AgentsControls
                controls = page.findChild(AgentsControls)
                pack_row = controls.parentWidget()
                page.widget().layout().insertWidget(0, controls)
                controls.enable_row.addWidget(controls.enabled)
                controls.enabled.setAccessibleName('Let AI apps use Samsara')
                # Setup uses the destination's scroll area, not the small pack
                # inventory viewport. The original row no longer has an editor.
                pack_row.hide()
            self._add_beta_notice(page)
            self._stack.addWidget(page)
            self._style_materialized_page(title, page)
        self._connect_conditions()

    def _add_beta_notice(self, page):
        """One insertion point for preferences and tools, only when built."""
        from samsara.ui.widgets.beta_notice import BetaNotice, _Session
        host = self.centralWidget()  # Replaced on each Settings open, not Apply.
        if not hasattr(host, '_beta_notice_session'):
            host._beta_notice_session = _Session(host)
        container = page.widget()
        if container.findChild(BetaNotice) is None:
            container.layout().insertWidget(0, BetaNotice(container, session=host._beta_notice_session))

    def focus_setting(self, key):
        """Use the existing lazy row lookup and destination navigation."""
        from .presentation import Disclosure
        from .agents_section import AgentsControls
        if key in ('agents', 'agent_token'):
            self._select_tool('Command library')
            controls = self._tool_pages['Command library'].findChild(AgentsControls)
            target = controls.enabled if key == 'agents' else controls.rotate
        else:
            row = self._rows_by_widget[key]  # 619 materializes this destination.
            if self._search_edit.text():
                self._search_edit.clear()
                self._apply_search_filter()
            self.show_tab(row.page_name)
            if row.disclosure:
                row.disclosure.reveal()
            target = self._widgets[key]
        ancestor = target.parentWidget()
        scrolls = []
        while ancestor is not None and ancestor is not self:
            if isinstance(ancestor, Disclosure):
                ancestor.reveal()
            if isinstance(ancestor, QScrollArea):
                scrolls.append(ancestor)
            ancestor = ancestor.parentWidget()
        def land():
            for scroll in scrolls:
                scroll.widget().layout().activate()
                scroll.ensureWidgetVisible(target, 12, 12)
            target.setFocus(Qt.FocusReason.OtherFocusReason)
        QTimer.singleShot(0, self._visit_callback(land, target))

    def _ensure_preferences(self):
        from samsara.ui.settings_qt import PAGE_MODULES
        for index in range(len(PAGE_MODULES)):
            self._ensure_destination(index)

    def _lookup_page(self, name):
        from samsara.ui.settings_qt import _TAB_NAMES
        if name in _TAB_NAMES:
            self._ensure_destination(_TAB_NAMES.index(name))

    def _lookup_tool(self, name):
        from .tools_page import TOOLS
        if name in TOOLS:
            self._ensure_tool(name)

    def _lookup_control(self, key):
        from samsara.ui.settings_qt import PAGE_MODULES
        for index, module in enumerate(PAGE_MODULES):
            if any(key in spec[2].split(',') for spec in module.BASE + module.ADVANCED):
                self._ensure_destination(index)
                return
        if key == 'wake_profiles':
            self._lookup_page('Dictation')
        elif key.startswith(('cloud_', 'ava_', 'sc_')):
            self._lookup_page('Ava')
        elif key.startswith(('tts_', 'sound_')):
            self._lookup_page('Audio')
        elif key.startswith('todoist_'):
            self._lookup_tool('Integrations')
        elif key == 'cmd_table':
            self._lookup_tool('Command library')

    def _lookup_source_saves(self, name):
        if name in self._SOURCE_BUILDERS:
            self._ensure_source(name)

    def _ensure_all_source_saves(self):
        for name in self._SOURCE_BUILDERS:
            self._ensure_source(name)

    def _setting_row(self, label, description, widget, control_width=260):
        row = super()._setting_row(label, description, widget, control_width)
        self._source_rows.append((label, description, widget, row))
        return row

    def _source_button(self, text, source=None):
        parent = self._source_pages[source] if source else self._source_store
        found = [b for b in parent.findChildren(QPushButton) if b.text() == text]
        if len(found) != 1:
            raise ValueError(f"Expected one source action {source}:{text}, got {len(found)}")
        return found[0]

    def _take_control(self, key):
        if key in self._widgets:
            widget = self._widgets[key]
        elif ':' in key:
            source, text = key.split(':', 1)
            widget = self._source_button(text, None if source == 'action' else source)
        else:
            widget = self._source_store.findChild(QWidget, key)
        if widget is None:
            raise KeyError(f"Settings destination has no control: {key}")
        if id(widget) in self._placed_controls:
            raise ValueError(f"Settings control placed twice: {key}")
        self._placed_controls.add(id(widget))
        for _label, _desc, control, layout in self._source_rows:
            if control == widget or control.isAncestorOf(widget):
                if not widget.accessibleName():
                    widget.setAccessibleName(_label)
                left = layout.itemAt(0)
                if left and left.widget():
                    left.widget().hide()
        if key in ('adv_ducking_level', 'adv_hands_free_ducking_level', 'adv_hands_free_idle_level'):
            widget = PercentSpinBox.adapt(widget, self)
            self._widgets[key] = widget
            self._placed_controls.add(id(widget))
        widget.setParent(None)
        widget.setMinimumWidth(44)
        widget.setMaximumWidth(16777215)
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        widget.setMinimumHeight(44)
        if isinstance(widget, QComboBox):
            widget.setFont(theme.qfont(theme.TYPE_BODY))
            text_width = max((widget.fontMetrics().horizontalAdvance(widget.itemText(i)) for i in range(widget.count())), default=80)
            widget.setFixedWidth(min(320, max(116, text_width + 54)))
        elif isinstance(widget, QAbstractSpinBox):
            widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            widget.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        elif isinstance(widget, QCheckBox):
            widget.setText('')
            switch(widget)
        elif isinstance(widget, QPushButton):
            widget.setMinimumWidth(max(44, widget.fontMetrics().horizontalAdvance(widget.text()) + 30))
        elif isinstance(widget, QLineEdit):
            widget.setMinimumWidth(160)
            widget.setMaximumWidth(320)
        # CUDA options arrive after construction; keep its model-backed picker.
        return widget if key == 'adv_device' else short_choice(widget, key)

    def make_page(self, title, subtitle, base, advanced):
        page = Page(title, subtitle)
        self._pages[title] = page
        for is_advanced, specs in ((False, base), (True, advanced)):
            for spec in specs:
                label, description, names, config, *_old_help_copy = spec
                keys = names.split(',') if names else []
                controls = [self._take_control(key) for key in keys]
                help_data = next(
                    (SETTINGS_SCHEMA[key]["help"] for key in filter(None, config.split(','))
                     if key in SETTINGS_SCHEMA and "help" in SETTINGS_SCHEMA[key]),
                    None,
                )
                row = SettingRow(label, description, controls, help_data)
                # Keep source explanations searchable without turning them
                # into disclosure copy or adding a help button.
                explanations = [
                    text for _label, text, widget, _source_row in self._source_rows
                    if any(widget == getattr(control, '_canonical_control', control)
                           or widget.isAncestorOf(getattr(control, '_canonical_control', control)) for control in controls)
                    and text and text != description
                ]
                row.full_help = " ".join((description, *explanations, help_data["why"] if help_data else ""))
                row.config_keys = tuple(filter(None, config.split(',')))
                row.widget_keys = tuple(keys)
                row.page_name = title
                row.disclosure = page.advanced if is_advanced else None
                self._destination_rows.append(row)
                page.add_row(row, is_advanced)
                for key in keys:
                    self._rows_by_widget[key] = row
                for key in keys:
                    warning = self._widgets.get(key + '_warn')
                    if warning is not None:
                        row.grid.addWidget(warning, row.grid.rowCount(), 0, 1, 3)
        return page

    def _choice_button(self, title, names):
        button = QPushButton(title)
        button.setMinimumHeight(44)
        menu = QMenu(button)
        for label, key in names:
            target = self._widgets.get(key) or self._source_store.findChild(QPushButton, key)
            if target is None:
                raise KeyError(key)
            action = menu.addAction(label)
            action.triggered.connect(target.click)
            # These are action aliases, not second preference controls.
        button.setMenu(menu)
        return button

    def _prepare_actions(self, destination):
        if destination == 'Dictation':
            precision = QLabel(self._widgets['adv_compute_type'].currentText())
            precision.setAccessibleName('Stored diagnostic precision')
            precision.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard | Qt.TextInteractionFlag.TextSelectableByMouse)
            self._widgets['computePrecisionStatus'] = precision
        if destination == 'General':
            self._widgets['floatingWindowActions'] = self._choice_button('Recover window', [
                ('Reset both floating windows', 'reset_floating_window_positions'),
                ('Reset preview position', 'reset_preview_position'),
                ('Reset indicator position', 'reset_indicator_position'),
            ])
        if destination == 'Help':
            self._widgets['profilesAction'] = self._source_button('Manage Profiles…', 'general')
            self._widgets['setupGuides'] = self._choice_button('Choose guide', [
                ('Interactive tutorial', 'replayTutorialButton'),
                ('Microphone setup', 'openMicSetupGuideButton'),
                ('Ava setup', 'openAvaSetupGuideButton'),
                ('Manage profiles', 'profilesAction'),
            ])
            self._widgets['supportChoices'] = self._choice_button('Get help', [
                ('Email the developer', 'betaFeedbackButton'),
                ('Create support bundle', 'createSupportBundleButton'),
                ('Open bundle folder', 'openSupportBundleFolderButton'),
                ('Copy safe diagnostics', 'copyDiagnosticButton'),
                ('Public GitHub report', 'reportBugButton'),
                ('Open live log', 'openLiveLogButton'),
            ])
            self._widgets['backupChoices'] = self._choice_button('Manage backup', [
                ('Export private configuration', 'exportConfigurationButton'),
                ('Import private configuration', 'importConfigurationButton'),
                ('Restore automatic backup', 'restoreConfigBackupButton'),
            ])
        if destination == 'Ava':
            route = CleanComboBox()
            self._widgets['providerRoute'] = route
            self._configure_ava_route_master(route)
        if destination == 'Shortcuts':
            corrections = QPushButton('Open')
            corrections.clicked.connect(self._open_corrections)
            self._widgets['openCorrections'] = corrections
        if destination == 'Audio':
            preview = QPushButton('Preview')
            sounds = QMenu(preview)
            for key, text in [('start', 'Recording start'), ('stop', 'Recording stop'), ('success', 'Success'), ('error', 'Error'), ('capture_started', 'Capture started'), ('capture_saved', 'Capture saved'), ('agent_routing', 'Agent routing'), ('agent_response', 'Agent response'), ('confirm_required', 'Confirmation'), ('action_complete', 'Action complete'), ('thinking_pulse', 'Thinking')]:
                sounds.addAction(text).triggered.connect(lambda checked=False, k=key: self._preview_sound(k))
            preview.setMenu(sounds)
            self._widgets['soundPreview'] = preview


    def _preview_sound(self, key):
        root = Path(getattr(self.app, 'sounds_dir', None) or Path(__file__).resolve().parents[3] / 'sounds')
        selected = self._widgets['sound_theme_combo'].currentText()
        wav = root / 'themes' / selected / (key + '.wav')
        if wav.exists():
            self._play_wav_at_volume(wav, self._test_volume())
        else:
            self._play(root, key)

    def _open_corrections(self):
        from samsara.ui.correction_capture_qt import CorrectionCaptureQt
        from samsara.runtime import thread_registry
        def load():
            rows = self.app.history_store.query(type_filter='dictation', limit=1)
            last_text = rows[0]['display_text'] if rows else ''
            def show():
                if not hasattr(self, '_corrections'):
                    self._corrections = CorrectionCaptureQt(self.app)
                window = self._corrections._open_window(last_text)
                report = QPushButton('Report a correction')
                report.setMinimumHeight(44)
                report.clicked.connect(lambda: self.app._report_correction_dialog())
                window.centralWidget().layout().addWidget(report)
            qt_runtime.post(show)
        thread_registry.spawn('settings-corrections', load, daemon=True)

    @assembling
    def _refresh_conditions(self):
        if not hasattr(self, '_rows_by_widget'):
            return
        w = self._widgets
        cloud = 'cloud_enabled' in w and w['cloud_enabled'].isChecked()
        editing = 'ava_edit_enabled' in w and w['ava_edit_enabled'].isChecked()
        speech = 'tts_enabled' in w and w['tts_enabled'].isChecked()
        for key, row in self._rows_by_widget.items():
            if row.widget_keys[0] != key:
                continue
            visible = True
            if key in ('cloud_provider', 'cloud_api_key', 'cloud_model', 'cloud_timeout', 'cloud_web_search'):
                visible = cloud
            if key == 'cloud_web_search':
                visible = cloud and w['cloud_web_search'].isEnabled()
            if key in ('ava_cmd_model', 'ava_cmd_keep_warm', 'sc_model'):
                visible = not cloud
            if key.startswith('ava_edit_') and key != 'ava_edit_enabled':
                visible = editing and (key != 'ava_edit_model' or not cloud)
            if key.startswith('sc_mode_') or key == 'sc_repair_disfluencies':
                visible = editing and w['sc_enabled'].isChecked()
            if key in ('tts_engine', 'tts_voice_combo', 'tts_speed', 'tts_volume_slider'):
                visible = speech
            if key == 'wake_word_phrase':
                visible = w['wake_word_enabled'].isChecked()
            if key == 'adv_manual_threshold':
                visible = w['adv_threshold_mode'].currentText() == 'manual'
            if key == 'adv_cal_multiplier':
                visible = w['adv_threshold_mode'].currentText() != 'manual'
            if key == 'adv_ducking_level':
                visible = w['adv_ducking_recording_mode'].currentText() == 'Duck'
            if key == 'adv_aec_latency':
                visible = w['adv_aec_enabled'].isChecked()
            if key == 'cmd_tab_suppress':
                visible = w['cmd_tab_button'].combo in ('mouse4', 'mouse5')
            row.setProperty('conditionVisible', visible)
            row.setVisible(visible)

    @assembling
    def _connect_conditions(self):
        if not hasattr(self, '_condition_connections'):
            self._condition_connections = set()
        for key in ('cloud_enabled', 'ava_edit_enabled', 'sc_enabled', 'tts_enabled', 'wake_word_enabled', 'adv_aec_enabled'):
            if key in self._widgets and key not in self._condition_connections:
                self._widgets[key].toggled.connect(lambda _value: self._refresh_conditions())
                self._condition_connections.add(key)
        for key in ('cloud_provider', 'adv_threshold_mode', 'adv_ducking_recording_mode'):
            if key in self._widgets and key not in self._condition_connections:
                self._widgets[key].currentIndexChanged.connect(lambda _value: self._refresh_conditions())
                self._condition_connections.add(key)
        if 'cmd_tab_button' in self._widgets and 'cmd_tab_button' not in self._condition_connections:
            self._widgets['cmd_tab_button'].bindingChanged.connect(lambda _value: self._refresh_conditions())
            self._condition_connections.add('cmd_tab_button')
        self._refresh_conditions()

    def config_row_inventory(self):
        """Effective controls, including tool records and generated pack rows.

        Record fields belong to their manager/editor, not global preferences.
        Repeated sound previews and provider status labels are not editors.
        """
        # An explicit inventory request covers every editor, including tools.
        from samsara.ui.settings_qt import PAGE_MODULES
        from .tools_page import TOOLS
        for index in range(len(PAGE_MODULES)):
            self._ensure_destination(index)
        for tool in TOOLS:
            self._ensure_tool(tool)
        bindings = []
        for row in self._destination_rows:
            for key in row.config_keys:
                bindings.append((key, row.page_name, row))
        for pack_id, control in self._widgets['_pack_checkboxes'].items():
            if pack_id != 'ai':
                bindings.append(('command_packs.' + pack_id, 'Command library', control))
        for key in ('profile', 'model', 'effort'):
            if 'hermes_' + key in self._widgets:
                bindings.append(('hermes.' + key, 'Command library', self._widgets['hermes_' + key]))
        for key, tool, widget in (
            ('tasks.todoist.enabled', 'Integrations', 'todoist_enabled'),
            ('tasks.todoist.api_token', 'Integrations', 'todoist_token'),
            ('music_library', 'Music', 'music_library_table'),
            ('alarms.items', 'Alarms', 'alarms_table'),
        ):
            bindings.append((key, tool, self._widgets[widget]))
        return bindings
