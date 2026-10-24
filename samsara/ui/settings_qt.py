"""Settings window chrome and destination registry (285)."""
from samsara.ui.settings.shared_qt import *
from samsara.ui.settings.services import SettingsServices
from samsara.ui.settings.assembly import PageAssembly
from samsara.ui.settings.presentation import style as presentation_style
from samsara.ui import window_activation
from samsara.ui.settings import sidebar as settings_sidebar
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
from PySide6.QtWidgets import QListWidgetItem
from pathlib import Path
from shiboken6 import isValid
from samsara.ui.settings.lazy_access import LookupMap, LookupRows, assembling

PAGE_MODULES = (general_page, dictation_page, audio_page, ava_page, shortcuts_page, help_page)
_TAB_NAMES = [module.TITLE for module in PAGE_MODULES]
_SIDEBAR_GROUPS = [("Settings", _TAB_NAMES)]
_ALIASES = {"Modes": "Dictation", "Sounds": "Audio", "TTS": "Audio", "Ava / Cloud": "Ava", "Advanced": "Dictation", "Help & Support": "Help", "Commands": "Command library"}


class SettingsQt:
    def __init__(self, app):
        self.app = app
        self._window = None

    def show(self):
        qt_runtime.post(self._show_preferences)

    def show_page(self, page):
        """Open one registered preference page from any thread (819)."""
        page = _ALIASES.get(page, page)
        if page not in _TAB_NAMES:
            raise ValueError(page)
        qt_runtime.post(lambda: self._show_preferences(page))

    def show_setting(self, key):
        """Open and reveal a named setup section using lazy navigation (819)."""
        def show():
            self._show_tool('Integrations' if key in ('agents', 'agent_token') else 'Command library')
            self._window.focus_setting(key)
        qt_runtime.post(show)

    def _show_preferences(self, page="General"):
        if self._window is None:
            self._window = _SettingsWindow(self.app)
        self._window.show_tab(page)

    def show_tool(self, name):
        name = _ALIASES.get(name, name)
        if name not in tools_page.TOOLS:
            raise ValueError(name)
        qt_runtime.post(lambda: self._show_tool(name))

    def _show_tool(self, name):
        if self._window is None:
            self._window = _SettingsWindow(self.app)
        self._window.open_tool(name)


class _SettingsWindow(
    PageAssembly, SettingsServices, GeneralPage, ModesPage, CommandsPage,
    SoundsPage, TTSPage, AvaCloudPage, AlarmsPage, HealthPage, AdvancedPage,
    HelpPage, MusicPage, QMainWindow,
):
    _test_result = Signal(str, str)
    _visit_test_result = Signal(object, str, str)
    _background_result = Signal(object)

    def __init__(self, app, tool=None):
        super().__init__()
        self.app = app
        self._visit_generation = 0
        self._visit_shown = False
        self._config_at_open = copy.deepcopy(self.app.config)
        self._test_result.connect(self._on_test_result)
        self._visit_test_result.connect(self._deliver_visit_test_result)
        self._background_result.connect(lambda callback: callback(), Qt.ConnectionType.QueuedConnection)
        self._build_contents(tool)

    def _build_contents(self, tool=None):
        # Ava assembly wraps this method per tree; never nest old wrappers.
        self._building_contents = True
        self._assembly_depth = 0
        self._controls_at_open = copy.deepcopy(self.app.config)
        self.__dict__.pop('_refresh_conditions', None)
        self.__dict__.pop('_component_rows', None)
        self._tool_name = tool
        self._ava_consent_staged = None
        self._widgets = LookupMap(self, self._lookup_control)
        self._save_fns = []
        self._source_rows = []
        self._source_pages = {}
        self._destination_rows = LookupRows(self, self._ensure_preferences)
        self._rows_by_widget = LookupMap(self, self._lookup_control)
        self._placed_controls = set()
        self._pages = LookupMap(self, self._lookup_page)
        self._cloud_reachable = None
        self._advanced_stack_index = None
        self._source_store = QWidget(self)
        self._source_store.hide()
        self.setWindowTitle("Samsara — " + (tool or "Settings"))
        self.setMinimumSize(780, 480)
        available = self.screen().availableGeometry()
        self.resize(min(1160, max(720, available.width() - 40)), min(920, max(480, available.height() - 40)))
        self.setStyleSheet(presentation_style())
        self._source_saves = LookupMap(self, self._lookup_source_saves, self._ensure_all_source_saves)
        self._condition_connections = set()
        self._tool_pages = LookupMap(self, self._lookup_tool)
        self._stack = QStackedWidget()
        # Stable navigation indices without constructing unvisited destinations.
        for module in PAGE_MODULES:
            self._stack.addWidget(QWidget())
        self._stack.currentChanged.connect(self._on_stack_changed)
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(central)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search settings…")
        self._search_edit.setAccessibleName("Search settings")
        self._search_edit.setMinimumHeight(44)
        body = QHBoxLayout()
        self._sidebar = QListWidget()
        self._sidebar.setAccessibleName("Settings pages")
        self._sidebar_row_to_stack_index = {}
        for index, name in enumerate(_TAB_NAMES):
            self._sidebar.addItem(name)
            self._sidebar_row_to_stack_index[index] = index
        heading = QListWidgetItem('Tools')
        heading.setData(Qt.ItemDataRole.UserRole, 'heading')
        heading.setFlags(Qt.ItemFlag.NoItemFlags)
        self._sidebar.addItem(heading)
        self._tool_sidebar_rows = {}
        for name in tools_page.TOOLS:
            self._tool_sidebar_rows[name] = self._sidebar.count()
            self._sidebar.addItem(tools_page.LABELS[name])
        # Heading, search and page list share one surface (360); a tool window has no search.
        sidebar_panel = settings_sidebar.build_panel(_SIDEBAR_GROUPS[0][0], None if tool else self._search_edit, self._sidebar)
        # Tools and preferences share the shell and retain their controls.
        # Apply still collects only the currently selected destination.
        page_column = QVBoxLayout()
        page_column.setSpacing(0)
        page_column.addWidget(self._stack, 1)
        body.addWidget(sidebar_panel)
        body.addLayout(page_column, 1)
        root.addLayout(body, 1)
        self._sidebar.currentRowChanged.connect(self._on_sidebar_row_changed)
        self._sidebar.setCurrentRow(0)
        footer_strip = QFrame()
        footer_strip.setObjectName('footer')
        footer = QHBoxLayout(footer_strip)
        footer.addStretch()
        # PLACEHOLDER COPY (838a): one left footer label across every page.
        cancel = QPushButton("Cancel")
        self._close_button = cancel
        cancel.clicked.connect(self.close)
        footer.addWidget(cancel)
        apply = QPushButton("Apply && Close")
        apply.setObjectName("apply")
        self._apply_button = apply
        apply.clicked.connect(self._apply_and_close)
        footer.addWidget(apply)
        page_column.addWidget(footer_strip)
        # Air around Tools / Cancel / Apply so they no longer touch the window edges (360).
        footer.setContentsMargins(theme.SPACE_2, theme.SPACE_1, theme.SPACE_2, theme.SPACE_2)
        footer.setSpacing(theme.SPACE_1)
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._apply_search_filter)
        self._search_edit.textChanged.connect(lambda _: self._search_debounce.start())
        self._build_search_registry()
        self._apply_metric_minimum_widths()
        self._building_contents = False
        self._apply_visible_design_scale()
        self._save_fns = [self._collect_destination_updates]
        if tool:
            self._select_tool(tool)

    def _refresh_visit(self):
        """Each hidden-to-shown visit starts from live config, not abandoned edits."""
        self._visit_generation += 1
        self._stop_visit_timers()
        tool = self._tool_name
        page = _TAB_NAMES[self._stack.currentIndex()] if not tool else None
        self._search_debounce.stop()
        if dict.__contains__(self._widgets, 'tts_engine_warn'):
            self.removeEventFilter(self._widgets['tts_engine_warn'])
        input_layout = self.__dict__.pop('_settings_input_layout', None)
        if input_layout is not None:
            self.removeEventFilter(input_layout)
            input_layout.deleteLater()
        old_central = self.takeCentralWidget()
        old_store = self._source_store
        old_central.hide()
        old_store.hide()
        old_central.setParent(None)
        old_store.setParent(None)
        old_central.deleteLater()
        old_store.deleteLater()
        self._search_debounce.deleteLater()
        self._config_at_open = copy.deepcopy(self.app.config)
        self._build_contents(tool)
        if page:
            self._sidebar.setCurrentRow(_TAB_NAMES.index(page))

    def show(self):
        # Build before Qt begins delivering show events to the old child tree.
        # Already-visible navigation retains both edits and the snapshot.
        if not self.isVisible() and self._visit_shown:
            self._refresh_visit()
        self._visit_shown = True
        super().show()

    def showNormal(self):
        # surface_requested restores minimized windows through this entry.
        if not self.isVisible() and self._visit_shown:
            self._refresh_visit()
        self._visit_shown = True
        super().showNormal()

    def _visit_callback(self, callback, *widgets):
        generation = self._visit_generation

        def guarded(*args, **kwargs):
            if (isValid(self) and generation == self._visit_generation
                    and all(isValid(widget) for widget in widgets)):
                return callback(*args, **kwargs)

        return guarded

    def _deliver_visit_test_result(self, callback, message, color):
        callback(message, color)

    def _stop_visit_timers(self):
        # Physical capture posts retain the old button until delivery. Revoke
        # capture before retiring its widgets so those posts become no-ops.
        for button in self.findChildren(_HotkeyButton):
            button._capturing = False
            button._held.clear()
        timer = getattr(self, '_tts_sample_timer', None)
        if timer is not None and isValid(timer):
            timer.stop()
        self._tts_sample_timer = None

    @assembling
    def _build_search_registry(self):
        rows = [
            (row.title, row.full_help, _TAB_NAMES.index(row.page_name), row.heading, row.detail, row.value)
            for row in self._destination_rows if row.page_name in _TAB_NAMES
        ]
        # Keep the same list object when an inventory walk builds more pages.
        if not hasattr(self, '_search_rows'):
            self._search_rows = LookupRows(self, self._ensure_preferences)
        self._search_rows[:] = rows

    def _on_stack_changed(self, index):
        if 0 <= index < len(PAGE_MODULES) and not self._assembly_depth:
            page = self._ensure_destination(index)
            self._stack.setCurrentWidget(page)

    def _apply_search_filter(self):
        query = self._search_edit.text().strip().casefold()
        if query:
            # Search is an explicit request across destinations, including those
            # that have not yet been opened. Empty search never builds pages.
            for index in range(len(PAGE_MODULES)):
                self._ensure_destination(index)
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
        if query and any(term in query for term in ('connect an ai app', 'agent')):
            self._select_tool('Integrations')
            return
        if query and matched:
            # A setting's name outranks an incidental mention in help, e.g.
            # "Stage timing" before "staged thought" when searching "stage".
            best_rank = max(name_matches.values())
            best_pages = {name for name, rank in name_matches.items() if rank == best_rank}
            if self._tool_name or _TAB_NAMES[self._stack.currentIndex()] not in best_pages:
                self._sidebar.setCurrentRow(next(i for i, name in enumerate(_TAB_NAMES) if name in best_pages))

    def _on_sidebar_row_changed(self, row):
        for name, tool_row in self._tool_sidebar_rows.items():
            if row == tool_row:
                self._select_tool(name)
                return
        if row in self._sidebar_row_to_stack_index:
            self._tool_name = None
            self.setWindowTitle("Samsara — Settings")
            if hasattr(self, '_close_button'):
                self._close_button.setText('Cancel')
            self._ensure_destination(row)
            self._stack.setCurrentIndex(row)
            self._refresh_apply_button()
            if _TAB_NAMES[row] == 'Ava':
                self._refresh_sc_status()

    def show_tab(self, name):
        name = _ALIASES.get(name, name)
        if name in tools_page.TOOLS:
            self.open_tool(name)
            return
        window_activation.surface_requested(self)
        self._sidebar.setCurrentRow(_TAB_NAMES.index(name))

    def _select_tool(self, name):
        name = _ALIASES.get(name, name)
        if name not in tools_page.TOOLS:
            raise ValueError(name)
        self._ensure_tool(name)
        self._sidebar.blockSignals(True)
        self._sidebar.setCurrentRow(self._tool_sidebar_rows[name])
        self._sidebar.blockSignals(False)
        self._tool_name = name
        page = self._tool_pages[name]
        self._stack.setCurrentWidget(page)
        self._refresh_apply_button()
        page.verticalScrollBar().setValue(0)
        page.horizontalScrollBar().setValue(0)
        # Qt can scroll a newly shown page to its first focused editor.
        # Land at the heading after those layout/focus events as well.
        QTimer.singleShot(0, self._visit_callback(
            lambda: page.verticalScrollBar().setValue(0), page))
        # PLACEHOLDER COPY (838a): one left footer label across every page.
        self._close_button.setText('Cancel')
        self.setWindowTitle('Samsara — ' + tools_page.LABELS[name])

    def _refresh_apply_button(self):
        apply_button = getattr(self, "_apply_button", None)
        if apply_button is None:
            return
        page = self._stack.currentWidget()
        apply_button.setVisible(not bool(page and page.property("settingsNoApply")))

    def open_tool(self, name):
        window_activation.surface_requested(self)
        self._select_tool(name)

    def open_preferences(self, name):
        self.show_tab(name)

    def closeEvent(self, event):
        event.ignore()
        self._visit_generation += 1
        self._stop_visit_timers()
        self.hide()

    def apply_theme(self):
        self.setStyleSheet(presentation_style())
        for widget in self.findChildren(QWidget):
            if widget.styleSheet():
                widget.setStyleSheet(theme.retheme_stylesheet(widget.styleSheet()))
        self._apply_visible_design_scale()

    def _collect_destination_updates(self, _acc):
        updates = {}
        if self._tool_name:
            source, allowed = {
                'Command library': ('commands', {'command_packs'}),
                'Integrations': ('commands', {'tasks', 'hermes', 'command_packs'}),
                'Alarms': ('alarms', {'alarms'}),
                'Music': ('music', {'music_library'}),
                'Components': ('advanced', {'gesture'}),
                'Health': ('health', set()),
            }[self._tool_name]
            for save in self._source_saves.get(source, []):
                updates.update({k: v for k, v in save(updates).items() if k in allowed})
            if 'alarms' in updates:
                current = self.app.config.get('alarms', {}) or {}
                for key in ('complete_hotkey', 'dismiss_hotkey'):
                    if key in current:
                        updates['alarms'][key] = current[key]
            if 'command_packs' in updates:
                current = self.app.config.get('command_packs', {}) or {}
                if self._tool_name == 'Integrations':
                    hermes = self._widgets.get('hermes_section')
                    if hermes is not None:
                        updates['command_packs'] = dict(current)
                        updates['command_packs']['hermes'] = hermes.enabled.isChecked()
                    else:
                        updates.pop('command_packs')
                else:
                    for key in ('ai', 'hermes'):
                        if key in current:
                            updates['command_packs'][key] = current[key]
                        else:
                            updates['command_packs'].pop(key, None)
            return updates
        self._ensure_preferences()
        for source in ('general', 'modes', 'sounds', 'tts', 'ava', 'advanced', 'help'):
            for save in self._source_saves.get(source, []):
                part = save(updates)
                part.pop('gesture', None)
                updates.update(part)
        if 'wake_profile_controls' in self._widgets:
            updates['wake_profiles'] = self._edited_wake_profiles()
        if 'alarms_complete_key' not in self._widgets:
            return updates
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
        if self._tool_name in ('Command library', 'Integrations'):
            rebuild = getattr(getattr(self.app, 'command_executor', None), 'rebuild_matcher', None)
            if callable(rebuild):
                rebuild()
        if not self._tool_name and self.app.config.get('sound_theme', 'cute') != previous_sound:
            root = Path(getattr(self.app, 'sounds_dir', None) or Path(__file__).resolve().parents[2] / 'sounds')
            self._apply_sound_theme(self.app.config['sound_theme'], root, root / 'themes')
