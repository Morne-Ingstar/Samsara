"""Settings window chrome and destination registry (285)."""
from samsara.ui.settings.shared_qt import *
from samsara.ui.settings.services import SettingsServices
from samsara.ui.settings.assembly import PageAssembly
from samsara.ui.settings.presentation import style as presentation_style
from samsara.ui.settings import general_page, dictation_page, audio_page, ava_page, shortcuts_page, help_page, tools_page
from samsara.ui.settings.general_qt import GeneralPage
from samsara.ui.settings.modes_qt import ModesPage
from samsara.ui.settings.commands_qt import CommandsPage
from samsara.ui.settings.sounds_qt import SoundsPage
from samsara.ui.settings.tts_qt import TTSPage
from samsara.ui.settings.ava_cloud_qt import AvaCloudPage
from samsara.ui.settings.alarms_qt import AlarmsPage
from samsara.ui.settings.health_qt import HealthPage
from samsara.ui.settings.advanced_qt import AdvancedPage
from samsara.ui.settings.help_qt import HelpPage
from samsara.ui.settings.music_qt import MusicPage
from PySide6.QtWidgets import QMenu
from pathlib import Path

PAGE_MODULES = (general_page, dictation_page, audio_page, ava_page, shortcuts_page, help_page)
_TAB_NAMES = [module.TITLE for module in PAGE_MODULES]
_SIDEBAR_GROUPS = [("Settings", _TAB_NAMES)]
_ALIASES = {"Modes": "Dictation", "Sounds": "Audio", "TTS": "Audio", "Ava / Cloud": "Ava", "Advanced": "Dictation", "Help & Support": "Help", "Commands": "Command library"}


class SettingsQt:
    def __init__(self, app):
        self.app = app
        self._window = None
        self._tools = {}

    def show(self):
        qt_runtime.post(self._show_preferences)

    def _show_preferences(self, page="General"):
        if self._window is None:
            self._window = _SettingsWindow(self.app)
        self._window.show_tab(page)

    def show_tool(self, name):
        if name not in tools_page.TOOLS:
            raise ValueError(name)
        qt_runtime.post(lambda: self._show_tool(name))

    def _show_tool(self, name):
        # A tool owns a separate edit session, so applying it never applies
        # the user's unsaved Settings controls.
        if name not in self._tools:
            self._tools[name] = _SettingsWindow(self.app, tool=name)
        window = self._tools[name]
        window.show()
        window.raise_()
        window.activateWindow()


class _SettingsWindow(
    PageAssembly, SettingsServices, GeneralPage, ModesPage, CommandsPage,
    SoundsPage, TTSPage, AvaCloudPage, AlarmsPage, HealthPage, AdvancedPage,
    HelpPage, MusicPage, QMainWindow,
):
    _test_result = Signal(str, str)

    def __init__(self, app, tool=None):
        super().__init__()
        self.app = app
        self._tool_name = tool
        self._widgets = {}
        self._save_fns = []
        self._source_rows = []
        self._source_pages = {}
        self._destination_rows = []
        self._rows_by_widget = {}
        self._placed_controls = set()
        self._pages = {}
        self._cloud_reachable = None
        self._advanced_stack_index = None
        self._test_result.connect(self._on_test_result)
        self._source_store = QWidget(self)
        self._source_store.hide()
        self.setWindowTitle("Samsara — " + (tool or "Settings"))
        self.setMinimumSize(720, 480)
        available = self.screen().availableGeometry()
        self.resize(min(1160, max(720, available.width() - 40)), min(920, max(480, available.height() - 40)))
        self.setStyleSheet(presentation_style())
        self._source_saves = {}
        # Preserve the established callback order for shared nested config.
        for name, builder in (
            ('general', self._build_general_tab), ('modes', self._build_modes_tab),
            ('commands', self._build_commands_tab), ('sounds', self._build_sounds_tab),
            ('tts', self._build_tts_tab), ('ava', self._build_ava_cloud_tab),
            ('alarms', self._build_alarms_tab), ('health', self._build_health_tab),
            ('advanced', self._build_advanced_tab), ('help', self._build_support_tab),
            ('music', self._build_music_tab),
        ):
            before = len(self._save_fns)
            page = builder()
            page.setParent(self._source_store)
            self._source_pages[name] = page
            self._source_saves[name] = self._save_fns[before:]
        self._prepare_actions()
        self._stack = QStackedWidget()
        for module in PAGE_MODULES:
            self._stack.addWidget(module.build(self))
        self._tool_pages = tools_page.build(self)
        for page in self._tool_pages.values():
            page.setParent(self._source_store)
        self._connect_conditions()
        # Status labels retain their original callbacks and move beside the
        # feature that owns them rather than remaining in hidden scaffolding.
        for name, key in [('Shortcuts', 'modes_collision_warn'), ('Audio', 'tts_test_status')]:
            widget = self._widgets.get(key)
            if widget is not None:
                self._pages[name].column.insertWidget(2, widget)
        for key, row_key in [('cloud_test_status', 'cloud_api_key'), ('cloud_info_label', 'cloud_provider')]:
            row = self._rows_by_widget[row_key]
            row.grid.addWidget(self._widgets[key], row.grid.rowCount(), 0, 1, 3)
        self._pages['Ava'].advanced.rows.insertWidget(0, self._widgets['sc_status_label'])
        support_status = self._source_store.findChild(QLabel, "feedbackStatusLabel")
        self._pages['Help'].column.insertWidget(2, support_status)
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(central)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search settings…")
        self._search_edit.setAccessibleName("Search settings")
        self._search_edit.setMinimumHeight(44)
        if not tool:
            root.addWidget(self._search_edit)
        body = QHBoxLayout()
        self._sidebar = QListWidget()
        self._sidebar.setFixedWidth(180)
        self._sidebar.setAccessibleName("Settings pages")
        self._sidebar_row_to_stack_index = {}
        for index, name in enumerate(_TAB_NAMES):
            self._sidebar.addItem(name)
            self._sidebar_row_to_stack_index[index] = index
        self._sidebar.setStyleSheet(f"QListWidget {{ border: none; background: {theme.BG1}; }} QListWidget::item {{ min-height: 44px; padding: 0 16px; border-radius: 6px; }} QListWidget::item:selected {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; }}")
        if tool:
            self._stack.setParent(self._source_store)
            root.addWidget(self._tool_pages[tool], 1)
        else:
            body.addWidget(self._sidebar)
            body.addWidget(self._stack, 1)
            root.addLayout(body, 1)
        self._sidebar.currentRowChanged.connect(self._on_sidebar_row_changed)
        self._sidebar.setCurrentRow(0)
        footer = QHBoxLayout()
        tools = QPushButton("Tools")
        menu = QMenu(tools)
        for name in tools_page.TOOLS:
            menu.addAction(name).triggered.connect(lambda checked=False, n=name: self.open_tool(n))
        tools.setMenu(menu)
        footer.addWidget(tools)
        footer.addStretch()
        cancel = QPushButton("Close" if tool else "Cancel")
        cancel.clicked.connect(self.close)
        footer.addWidget(cancel)
        apply = QPushButton("Apply && Close")
        apply.setObjectName("apply")
        apply.clicked.connect(self._apply_and_close)
        footer.addWidget(apply)
        root.addLayout(footer)
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._apply_search_filter)
        self._search_edit.textChanged.connect(lambda _: self._search_debounce.start())
        self._build_search_registry()
        self._apply_metric_minimum_widths()
        theme.apply_design_scale(self)
        self._save_fns = [self._collect_destination_updates]

    def _build_search_registry(self):
        self._search_rows = [
            (row.title, row.full_help, _TAB_NAMES.index(row.page_name), row.heading, row.detail, row.value)
            for row in self._destination_rows if row.page_name in _TAB_NAMES
        ]

    def _apply_search_filter(self):
        query = self._search_edit.text().strip().casefold()
        self._refresh_conditions()
        matched = set()
        name_matches = {}
        for row in self._destination_rows:
            if row.page_name not in _TAB_NAMES:
                continue
            match = not query or query in (row.title + " " + row.full_help + " " + " ".join(row.config_keys)).casefold()
            row.setVisible(match and row.property('conditionVisible') is not False)
            if query and match:
                matched.add(row.page_name)
                title = row.title.casefold()
                rank = 3 if title == query else 2 if title.startswith(query) else 1 if query in title else 0
                name_matches[row.page_name] = max(name_matches.get(row.page_name, 0), rank)
                if row.disclosure:
                    row.disclosure.reveal()
        for page in self._pages.values():
            page.advanced.refresh_filter_state(query)
        if query and matched:
            # A setting's name outranks an incidental mention in help, e.g.
            # "Stage timing" before "staged thought" when searching "stage".
            best_rank = max(name_matches.values())
            best_pages = {name for name, rank in name_matches.items() if rank == best_rank}
            if _TAB_NAMES[self._stack.currentIndex()] not in best_pages:
                self._sidebar.setCurrentRow(next(i for i, name in enumerate(_TAB_NAMES) if name in best_pages))

    def _on_sidebar_row_changed(self, row):
        if row in self._sidebar_row_to_stack_index:
            self._stack.setCurrentIndex(row)
            if _TAB_NAMES[row] == 'Ava':
                self._refresh_sc_status()

    def show_tab(self, name):
        name = _ALIASES.get(name, name)
        if name in tools_page.TOOLS:
            self.open_tool(name)
            return
        self._sidebar.setCurrentRow(_TAB_NAMES.index(name))
        self.show()
        self.raise_()
        self.activateWindow()

    def open_tool(self, name):
        if not hasattr(self, '_tool_controller'):
            self._tool_controller = SettingsQt(self.app)
        self._tool_controller.show_tool(name)

    def open_preferences(self, name):
        if not hasattr(self, '_preference_controller'):
            self._preference_controller = SettingsQt(self.app)
        qt_runtime.post(lambda: self._preference_controller._show_preferences(name))

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def apply_theme(self):
        self.setStyleSheet(presentation_style())
        for widget in self.findChildren(QWidget):
            if widget.styleSheet():
                widget.setStyleSheet(theme.retheme_stylesheet(widget.styleSheet()))
        theme.apply_design_scale(self)

    def _collect_destination_updates(self, _acc):
        updates = {}
        if self._tool_name:
            source, allowed = {
                'Command library': ('commands', {'command_packs'}),
                'Integrations': ('commands', {'tasks'}),
                'Alarms': ('alarms', {'alarms'}),
                'Music': ('music', {'music_library'}),
                'Components': ('advanced', {'gesture'}),
                'Health': ('health', set()),
            }[self._tool_name]
            for save in self._source_saves[source]:
                updates.update({k: v for k, v in save(updates).items() if k in allowed})
            if 'alarms' in updates:
                current = self.app.config.get('alarms', {}) or {}
                for key in ('complete_hotkey', 'dismiss_hotkey'):
                    if key in current:
                        updates['alarms'][key] = current[key]
            if 'command_packs' in updates:
                current = self.app.config.get('command_packs', {}) or {}
                if 'ai' in current:
                    updates['command_packs']['ai'] = current['ai']
            return updates
        for source in ('general', 'modes', 'sounds', 'tts', 'ava', 'advanced', 'help'):
            for save in self._source_saves[source]:
                part = save(updates)
                part.pop('gesture', None)
                updates.update(part)
        if 'wake_profile_controls' in self._widgets:
            updates['wake_profiles'] = self._edited_wake_profiles()
        alarms = dict(self.app.config.get('alarms', {}) or {})
        alarms['complete_hotkey'] = self._widgets['alarms_complete_key'].combo
        alarms['dismiss_hotkey'] = self._widgets['alarms_dismiss_key'].combo
        updates['alarms'] = alarms
        return updates

    def _edited_wake_profiles(self):
        profiles = copy.deepcopy(self.app.config.get('wake_profiles', []))
        for profile, controls in zip(profiles, self._widgets['wake_profile_controls']):
            profile['phrase'] = controls['phrase'].text().strip()
            profile['target_process'] = controls['target_process'].text().strip()
            profile['send_word'] = controls['send_word'].text().strip()
            profile['enabled'] = controls['enabled'].isChecked()
        return profiles

    def _apply_and_close(self):
        previous_sound = self.app.config.get('sound_theme', 'cute')
        super()._apply_and_close()
        if not self._tool_name and self.app.config.get('sound_theme', 'cute') != previous_sound:
            root = Path(getattr(self.app, 'sounds_dir', None) or Path(__file__).resolve().parents[2] / 'sounds')
            self._apply_sound_theme(self.app.config['sound_theme'], root, root / 'themes')
