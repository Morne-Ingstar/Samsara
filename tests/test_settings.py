"""
Tests for SettingsWindow and configuration management.
"""
import pytest
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


class _StubApp:
    """Minimal app stand-in for headlessly constructing _SettingsWindow.

    All _build_*_tab methods read config via .get(key, default), so an
    empty config dict is enough -- this deliberately does NOT duplicate
    DictationApp's real default_config (that would drift out of sync);
    it only provides the handful of non-config attributes the tabs touch
    during __init__ (command_executor, hints, alarm_manager, etc).
    """

    def __init__(self):
        self.config = {}
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(commands={}, find_command=lambda p: None)
        self.hints = None
        self.alarm_manager = None

    def play_sound(self, *a, **k):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


class TestSettingsWindowConstruction:
    """Headless construction check -- also locks in that the duplicate
    command_mode.button / threshold_mode+cal_multiplier widgets stay gone."""

    def test_constructs_without_error(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        assert win is not None

    def test_cmd_button_widget_removed(self, qapp):
        """The old raw-key button picker was removed -- cmd_tab_button
        (now on the Modes tab) is the single home."""
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        assert 'cmd_button' not in win._widgets
        assert 'cmd_tab_button' in win._widgets

    def test_threshold_mode_widget_removed(self, qapp):
        """threshold_mode/cal_multiplier live only on the Advanced tab's
        adv_threshold_mode/adv_cal_multiplier -- single home."""
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        assert 'threshold_mode' not in win._widgets
        assert 'cal_multiplier' not in win._widgets
        assert 'adv_threshold_mode' in win._widgets
        assert 'adv_cal_multiplier' in win._widgets

    def test_output_selector_recovers_truncated_legacy_name(self, qapp):
        """An MME-truncated saved name resolves to its unique WASAPI output."""
        from samsara.ui.settings_qt import _SettingsWindow
        stub = _StubApp()
        stub.available_outputs = [{
            'id': 23,
            'name': 'Headphones (Arctis Nova Pro Wireless)',
            'hostapi': 'Windows WASAPI',
        }]
        stub.config = {
            'output_device': 7,
            'output_device_name': 'Headphones (Arctis Nova Pro Wir',
        }

        win = _SettingsWindow(stub)

        assert win._widgets['output_combo'].currentText() == (
            'Headphones (Arctis Nova Pro Wireless)'
        )
        general_updates = win._save_fns[0]({})
        assert general_updates['output_device'] == 23
        assert general_updates['output_device_name'] == (
            'Headphones (Arctis Nova Pro Wireless)'
        )

    def test_output_selector_exposes_but_preserves_missing_preference(self, qapp):
        """Fallback is visible and an untouched Apply keeps reconnect intent."""
        from samsara.ui.settings_qt import _SettingsWindow
        stub = _StubApp()
        stub.available_outputs = []
        stub.config = {
            'output_device': 7,
            'output_device_name': 'Disconnected headset',
        }

        win = _SettingsWindow(stub)

        assert win._widgets['output_combo'].currentText() == (
            'System default (saved output unavailable: Disconnected headset)'
        )
        general_updates = win._save_fns[0]({})
        assert 'output_device' not in general_updates
        assert 'output_device_name' not in general_updates

        win._widgets['output_combo'].setCurrentText('System default')
        general_updates = win._save_fns[0]({})
        assert general_updates['output_device'] is None
        assert general_updates['output_device_name'] is None

    def test_modes_tab_replaces_hotkeys_and_ai_commands(self, qapp):
        """Modes tab consolidates Hotkeys + AI Commands + the Commands tab's
        button picker -- those source tabs are gone."""
        from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
        win = _SettingsWindow(_StubApp())
        assert 'Modes' in _TAB_NAMES
        assert 'Hotkeys' not in _TAB_NAMES
        assert 'AI Commands' not in _TAB_NAMES
        assert len(_TAB_NAMES) == 10

        sidebar_labels = {win._sidebar.item(i).text() for i in range(win._sidebar.count())}
        assert 'Modes' in sidebar_labels
        assert 'Hotkeys' not in sidebar_labels
        assert 'AI Commands' not in sidebar_labels

    def test_modes_tab_save_fn_key_union(self, qapp):
        """Firing the Modes tab's save fn must produce the same key set the
        old Hotkeys + AI Commands saves produced together (config keys must
        not change -- this is a UI reorganization only)."""
        from samsara.ui.settings_qt import _SettingsWindow
        stub = _StubApp()
        win = _SettingsWindow(stub)

        # _save_fns[1] is the Modes tab's fn -- General is [0], Modes is next
        # (registration order == _stack.addWidget order in __init__).
        modes_save_fn = win._save_fns[1]
        produced = modes_save_fn({})

        expected_keys = {
            'hotkey', 'continuous_hotkey', 'wake_word_hotkey', 'command_hotkey',
            'streaming_hotkey', 'cancel_hotkey', 'undo_hotkey', 'dictate_commit_hotkey', 'ava_mode_key',
            'mode', 'wake_word_enabled', 'wake_word_config', 'command_mode',
            'ai_command_mode',
        }
        assert set(produced.keys()) == expected_keys

    def test_modes_tab_save_fn_command_mode_has_button(self, qapp):
        """command_mode.button/suppress_button are now written by the Modes
        tab's single save fn (previously split across Hotkeys + Commands)."""
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        modes_save_fn = win._save_fns[1]
        produced = modes_save_fn({})
        assert 'button' in produced['command_mode']
        assert 'suppress_button' in produced['command_mode']

    def test_modes_tab_save_fn_command_mode_enabled_reflects_checkbox(self, qapp):
        """The 'Enable command mode' checkbox is the single writer for
        command_mode.enabled -- toggling it must change what the save fn
        emits, both off->on and on->off."""
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        modes_save_fn = win._save_fns[1]

        # Default (empty config) construction: unchecked.
        assert win._widgets['cmd_tab_enabled'].isChecked() is False
        produced = modes_save_fn({})
        assert produced['command_mode']['enabled'] is False

        win._widgets['cmd_tab_enabled'].setChecked(True)
        produced = modes_save_fn({})
        assert produced['command_mode']['enabled'] is True

        win._widgets['cmd_tab_enabled'].setChecked(False)
        produced = modes_save_fn({})
        assert produced['command_mode']['enabled'] is False

    def test_modes_tab_command_mode_enabled_checkbox_reflects_existing_config(self, qapp):
        """Constructing the tab against a config with command_mode.enabled
        already True must pre-check the box (not just default it off)."""
        from samsara.ui.settings_qt import _SettingsWindow
        stub = _StubApp()
        stub.config = {"command_mode": {"enabled": True}}
        win = _SettingsWindow(stub)
        assert win._widgets['cmd_tab_enabled'].isChecked() is True

    def test_modes_tab_command_mode_button_default_is_right_ctrl(self, qapp):
        """Fresh/empty config -> the button combo defaults to Right Ctrl,
        not the old Mouse 4 default."""
        from samsara.ui.settings_qt import _SettingsWindow, _CMD_BUTTON_OPTIONS
        win = _SettingsWindow(_StubApp())
        assert win._widgets['cmd_tab_button'].currentText() == 'Right Ctrl (default)'
        modes_save_fn = win._save_fns[1]
        produced = modes_save_fn({})
        assert produced['command_mode']['button'] == 'rctrl'
        assert _CMD_BUTTON_OPTIONS['Mouse 4'] == 'mouse4'  # still selectable, just not default

    def test_modes_tab_has_one_voice_control_card_with_subordinate_hold_behavior(self, qapp):
        """Ordinary Command Mode and Hands-Free are one activation system,
        while AI Command Mode remains a separate card."""
        from PySide6.QtWidgets import QFrame
        from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES

        win = _SettingsWindow(_StubApp())
        page = win._stack.widget(_TAB_NAMES.index('Modes'))
        cards = [
            frame for frame in page.findChildren(QFrame)
            if frame.objectName() == 'settingsSectionCard'
        ]
        titles = [card.layout().itemAt(0).widget().text() for card in cards]

        assert titles == [
            'Hands-Free / Voice Control',
            'Dictation bindings',
            'AI Command Mode (Experimental)',
            'Ava Assistant',
            'Advanced tuning',
        ]
        assert 'Voice Commands' not in titles

        def containing_card(widget):
            parent = widget.parentWidget()
            while parent is not None and parent.objectName() != 'settingsSectionCard':
                parent = parent.parentWidget()
            return parent

        voice_card = cards[0]
        assert containing_card(win._widgets['cmd_mode']) is voice_card
        assert containing_card(win._widgets['command_hotkey']) is voice_card
        assert containing_card(win._widgets['wake_word_hotkey']) is voice_card
        assert containing_card(win._widgets['mode']) is cards[1]

    def test_modes_voice_control_rows_remain_search_registered(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES

        win = _SettingsWindow(_StubApp())
        modes_index = _TAB_NAMES.index('Modes')
        labels = {
            label for label, _desc, tab, *_widgets in win._search_rows
            if tab == modes_index
        }

        assert {
            'Enable voice control',
            'Button behavior',
            'Command-only key',
            'Wake activation key',
            'Primary dictation key behavior',
            'Enable AI command mode',
        } <= labels


class TestSidebarGrouping:
    """Sidebar has 3 non-selectable group headers (Settings / Tools / Support) + 10
    selectable tabs, and each Tools tab shows an instant-apply caption."""

    def test_header_and_selectable_row_counts(self, qapp):
        from PySide6.QtCore import Qt
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())

        headers = []
        selectable = []
        for i in range(win._sidebar.count()):
            item = win._sidebar.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsSelectable:
                selectable.append(item.text())
            else:
                headers.append(item.text())

        assert len(headers) == 3
        assert len(selectable) == 10
        assert set(headers) == {'SETTINGS', 'TOOLS', 'SUPPORT'}

    def test_tab_indices_unchanged(self, qapp):
        """Stack widget order/indices must be untouched by the regrouping --
        only the sidebar's visual row order changed. Modes occupies the old
        Hotkeys slot (index 1); AI Commands (old index 9) is gone."""
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        expected = {
            'General': 0, 'Modes': 1, 'Commands': 2, 'Sounds': 3, 'TTS': 4,
            'Ava / Cloud': 5, 'Alarms': 6, 'Health': 7, 'Advanced': 8,
            'Help & Support': 9,
        }
        for row, stack_index in win._sidebar_row_to_stack_index.items():
            name = win._sidebar.item(row).text()
            assert expected[name] == stack_index

    def test_clicking_a_header_does_nothing(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        before = win._stack.currentIndex()
        header_row = next(
            i for i in range(win._sidebar.count())
            if i not in win._sidebar_row_to_stack_index
        )
        win._on_sidebar_row_changed(header_row)
        assert win._stack.currentIndex() == before

    @pytest.mark.parametrize("tab_name,stack_index,expected_substring", [
        ("Commands", 2, "apply immediately"),
        ("Alarms",   6, "apply immediately"),
        ("Health",   7, "read-only"),
    ])
    def test_tool_tab_shows_instant_apply_caption(self, qapp, tab_name, stack_index, expected_substring):
        from PySide6.QtWidgets import QLabel
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        win._stack.setCurrentIndex(stack_index)
        tab_widget = win._stack.widget(stack_index)
        all_text = " ".join(lbl.text() for lbl in tab_widget.findChildren(QLabel)).lower()
        assert expected_substring in all_text, (
            f"{tab_name} tab has no caption containing {expected_substring!r}"
        )


class TestModesCollisionDetection:
    """Generalized Modes-tab-wide activation-binding collision checker."""

    def test_exact_duplicate_shows_banner_naming_both(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
        win = _SettingsWindow(_StubApp())
        win._stack.setCurrentIndex(_TAB_NAMES.index('Modes'))  # non-current stack pages report not-visible

        win._widgets['ava_mode_key']._combo = 'right_ctrl'
        win._widgets['ai_cmd_key'].setCurrentText('Right Ctrl')
        win._check_modes_collisions()

        warn = win._widgets['modes_collision_warn']
        assert warn.isVisibleTo(win)
        assert 'Ava mode' in warn.text()
        assert 'AI Command Mode key' in warn.text()

    def test_staged_thought_binding_participates_in_collision_warning(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
        win = _SettingsWindow(_StubApp())
        win._stack.setCurrentIndex(_TAB_NAMES.index('Modes'))

        win._widgets['dictate_commit_hotkey']._combo = (
            win._widgets['undo_hotkey'].combo
        )
        win._check_modes_collisions()

        warn = win._widgets['modes_collision_warn']
        assert warn.isVisibleTo(win)
        assert 'Paste staged thought' in warn.text()
        assert 'Undo' in warn.text()

    def test_no_collision_hides_banner(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        win._check_modes_collisions()
        warn = win._widgets['modes_collision_warn']
        assert not warn.isVisible()

    def test_superset_combo_shows_may_shadow_note(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
        win = _SettingsWindow(_StubApp())
        win._stack.setCurrentIndex(_TAB_NAMES.index('Modes'))

        win._widgets['hotkey']._combo = 'ctrl+shift'
        win._widgets['continuous_hotkey']._combo = 'ctrl+shift+a'
        win._check_modes_collisions()

        warn = win._widgets['modes_collision_warn']
        assert warn.isVisibleTo(win)
        assert 'may shadow' in warn.text()
        assert 'Record' in warn.text()
        assert 'Toggle continuous' in warn.text()

    def test_hotkey_capture_triggers_recheck(self, qapp):
        """_HotkeyButton's on_change hook fires the collision check without
        the caller needing to call it manually."""
        from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
        win = _SettingsWindow(_StubApp())
        win._stack.setCurrentIndex(_TAB_NAMES.index('Modes'))

        ava_btn = win._widgets['ava_mode_key']
        ai_key_combo = win._widgets['ai_cmd_key']
        ai_key_combo.setCurrentText('Right Ctrl')

        ava_btn._held = {'right_ctrl'}
        ava_btn._finish_capture()

        warn = win._widgets['modes_collision_warn']
        assert warn.isVisibleTo(win)


class TestAvaCloudTabNoLicenseGate:
    """Cloud AI is bring-your-own-key and free -- the settings tab must show
    every cloud control unconditionally, with no license/supporter-key gate
    hiding them. The old QStackedWidget gate (cloud_settings_widget) and its
    visibility toggle are gone for good."""

    def test_cloud_settings_visible_without_any_key(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
        win = _SettingsWindow(_StubApp())
        win._stack.setCurrentIndex(_TAB_NAMES.index('Ava / Cloud'))

        for widget_key in ('cloud_enabled', 'cloud_provider', 'cloud_api_key',
                            'cloud_model', 'cloud_timeout', 'ava_personality',
                            'ava_memory_mode', 'ava_memory_max_turns'):
            widget = win._widgets[widget_key]
            assert widget.isVisibleTo(win), f"{widget_key} should be visible with no license"
            assert widget.isEnabled(), f"{widget_key} should be enabled with no license"

    def test_gating_widget_removed(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        assert 'cloud_settings_widget' not in win._widgets

    def test_supporter_key_row_present_but_optional(self, qapp):
        """The relabeled supporter-key row still exists (for the future
        managed-key slot) but starts on the no-key page."""
        from samsara.ui.settings_qt import _SettingsWindow
        win = _SettingsWindow(_StubApp())
        assert 'cloud_license_entry' in win._widgets
        assert 'cloud_license_stack' in win._widgets
        assert win._widgets['cloud_license_stack'].currentIndex() == 0

    def test_save_fn_writes_cloud_settings_without_license(self, qapp):
        """Firing the Ava/Cloud save fn must persist cloud_llm settings with
        no premium_license present at all."""
        from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
        win = _SettingsWindow(_StubApp())
        win._widgets['cloud_enabled'].setChecked(True)
        win._widgets['cloud_api_key'].setText('sk-test-key')

        ava_cloud_save_fn = win._save_fns[_TAB_NAMES.index('Ava / Cloud')]
        produced = ava_cloud_save_fn({})
        assert produced['cloud_llm']['enabled'] is True
        assert produced['cloud_llm']['api_key'] == 'sk-test-key'


class TestApplyAndCloseSnapshot:
    """Regression test for the per-tab save-function refactor.

    tests/fixtures/settings_apply_and_close_snapshot.json was captured from
    the PRE-refactor _apply_and_close monolith, calling it on a freshly
    constructed window (default widget state, empty starting config) and
    dumping the resulting config dict. If the refactor changes what gets
    written for the same inputs, this test catches it.
    """

    def test_apply_and_close_matches_pre_refactor_snapshot(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        snapshot_path = Path(__file__).parent / "fixtures" / "settings_apply_and_close_snapshot.json"
        expected = json.loads(snapshot_path.read_text())

        stub = _StubApp()
        win = _SettingsWindow(stub)
        win._apply_and_close()

        assert stub.config == expected

    def test_microphone_switch_happens_before_bulk_config_update(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        class _MicStub(_StubApp):
            def __init__(self):
                super().__init__()
                self.config = {"microphone": 1, "microphone_name": "Old mic"}
                self.available_mics = [
                    {"id": 1, "name": "Old mic"},
                    {"id": 2, "name": "New mic"},
                ]
                self.switch_observations = []

            def switch_microphone(self, mic_id):
                self.switch_observations.append(
                    (mic_id, self.config.get("microphone"))
                )
                self.config["microphone"] = mic_id
                self.config["microphone_name"] = "New mic"

        stub = _MicStub()
        win = _SettingsWindow(stub)
        win._widgets["mic_combo"].setCurrentText("New mic")

        win._apply_and_close()

        assert stub.switch_observations == [(2, 1)]
        assert stub.config["microphone"] == 2
        assert stub.config["microphone_name"] == "New mic"

    def test_listening_indicator_setting_applies_live(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        stub = _StubApp()
        stub.apply_listening_indicator_settings = Mock()
        win = _SettingsWindow(stub)
        win._widgets["adv_indicator_enabled"].setChecked(True)

        win._apply_and_close()

        assert stub.config["listening_indicator_enabled"] is True
        stub.apply_listening_indicator_settings.assert_called_once_with()


class TestSettingsConfiguration:
    """Tests for settings configuration values"""

    def test_default_config_has_required_keys(self, sample_config):
        """Test that default config has all required keys"""
        required_keys = [
            'hotkey', 'mode', 'model_size', 'language',
            'auto_paste', 'add_trailing_space', 'microphone',
            'audio_feedback', 'command_mode_enabled'
        ]
        for key in required_keys:
            assert key in sample_config, f"Missing required key: {key}"

    def test_hotkey_format(self, sample_config):
        """Test hotkey is in correct format"""
        hotkey = sample_config['hotkey']
        assert isinstance(hotkey, str)
        # Should contain + for multi-key combos or be a single key
        assert len(hotkey) > 0

    def test_mode_valid_values(self, sample_config):
        """Test mode has valid value"""
        valid_modes = ['hold', 'toggle', 'continuous']
        assert sample_config['mode'] in valid_modes

    def test_model_size_valid_values(self, sample_config):
        """Test model_size has valid value"""
        valid_sizes = ['tiny', 'base', 'small', 'medium', 'large-v3']
        assert sample_config['model_size'] in valid_sizes

    def test_boolean_settings(self, sample_config):
        """Test boolean settings are actually booleans"""
        bool_keys = ['auto_paste', 'add_trailing_space', 'audio_feedback',
                     'command_mode_enabled', 'auto_capitalize', 'format_numbers']
        for key in bool_keys:
            if key in sample_config:
                assert isinstance(sample_config[key], bool), f"{key} should be bool"

    def test_numeric_settings(self, sample_config):
        """Test numeric settings are valid numbers"""
        assert isinstance(sample_config['silence_threshold'], (int, float))
        assert sample_config['silence_threshold'] > 0

        assert isinstance(sample_config['min_speech_duration'], (int, float))
        assert sample_config['min_speech_duration'] > 0

        assert isinstance(sample_config['sound_volume'], (int, float))
        assert 0 <= sample_config['sound_volume'] <= 1


class TestConfigPersistence:
    """Tests for saving and loading configuration"""

    def test_save_config_creates_file(self, tmp_path, sample_config):
        """Test that saving config creates the file"""
        config_file = tmp_path / "test_config.json"

        with open(config_file, 'w') as f:
            json.dump(sample_config, f, indent=2)

        assert config_file.exists()

    def test_load_config_reads_file(self, tmp_path, sample_config):
        """Test that loading config reads the file correctly"""
        config_file = tmp_path / "test_config.json"
        with open(config_file, 'w') as f:
            json.dump(sample_config, f)

        with open(config_file) as f:
            loaded = json.load(f)

        assert loaded['hotkey'] == sample_config['hotkey']
        assert loaded['mode'] == sample_config['mode']

    def test_config_missing_keys_get_defaults(self, tmp_path):
        """Test that missing config keys get default values"""
        partial_config = {'hotkey': 'ctrl+shift'}
        config_file = tmp_path / "partial_config.json"
        with open(config_file, 'w') as f:
            json.dump(partial_config, f)

        # Define defaults
        defaults = {
            'hotkey': 'ctrl+shift',
            'mode': 'hold',
            'model_size': 'base',
            'language': 'en'
        }

        # Load and merge with defaults
        with open(config_file) as f:
            loaded = json.load(f)

        for key, value in defaults.items():
            if key not in loaded:
                loaded[key] = value

        assert loaded['mode'] == 'hold'
        assert loaded['model_size'] == 'base'


class TestHotkeySettings:
    """Tests for hotkey configuration"""

    def test_parse_ctrl_shift(self):
        """Test parsing ctrl+shift hotkey"""
        hotkey = "ctrl+shift"
        keys = set(k.strip().lower() for k in hotkey.split('+'))
        assert keys == {'ctrl', 'shift'}

    def test_parse_three_key_combo(self):
        """Test parsing three-key combination"""
        hotkey = "ctrl+alt+d"
        keys = set(k.strip().lower() for k in hotkey.split('+'))
        assert keys == {'ctrl', 'alt', 'd'}

    def test_parse_single_key(self):
        """Test parsing single key hotkey"""
        hotkey = "escape"
        keys = set(k.strip().lower() for k in hotkey.split('+'))
        assert keys == {'escape'}

    def test_hotkey_normalization(self):
        """Test that hotkeys are normalized to lowercase"""
        hotkey = "CTRL+SHIFT"
        keys = set(k.strip().lower() for k in hotkey.split('+'))
        assert keys == {'ctrl', 'shift'}


class TestAutoStartSetting:
    """Tests for auto-start with Windows setting"""

    def test_startup_path_format(self, tmp_path):
        """Test startup path is correctly formatted"""
        import os
        appdata = os.environ.get('APPDATA', '')
        if appdata:
            startup_folder = Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
            startup_file = startup_folder / 'Samsara.vbs'

            # Check path format is valid
            assert 'Startup' in str(startup_file)
            assert startup_file.suffix == '.vbs'


class TestSoundSettings:
    """Tests for sound/audio feedback settings"""

    def test_volume_range(self, sample_config):
        """Test volume is in valid range"""
        volume = sample_config.get('sound_volume', 0.5)
        assert 0 <= volume <= 1

    def test_audio_feedback_toggle(self, sample_config):
        """Test audio feedback can be toggled"""
        sample_config['audio_feedback'] = False
        assert sample_config['audio_feedback'] is False

        sample_config['audio_feedback'] = True
        assert sample_config['audio_feedback'] is True


class TestMicrophoneSettings:
    """Tests for microphone configuration"""

    def test_microphone_none_uses_default(self, sample_config):
        """Test None microphone uses system default"""
        sample_config['microphone'] = None
        assert sample_config['microphone'] is None

    def test_microphone_by_id(self, sample_config):
        """Test microphone can be set by ID"""
        sample_config['microphone'] = 0
        assert sample_config['microphone'] == 0

    def test_show_all_devices_toggle(self, sample_config):
        """Test show_all_audio_devices toggle"""
        sample_config['show_all_audio_devices'] = True
        assert sample_config['show_all_audio_devices'] is True


class TestCommandSettings:
    """Tests for command-related settings"""

    def test_command_mode_toggle(self, sample_config):
        """Test command mode can be enabled/disabled"""
        sample_config['command_mode_enabled'] = True
        assert sample_config['command_mode_enabled'] is True

        sample_config['command_mode_enabled'] = False
        assert sample_config['command_mode_enabled'] is False

    def test_wake_word_setting(self, sample_config):
        """Test wake word configuration"""
        sample_config['wake_word'] = 'hello computer'
        assert sample_config['wake_word'] == 'hello computer'

    def test_wake_word_timeout_setting(self, sample_config):
        """Test wake word timeout configuration"""
        sample_config['wake_word_timeout'] = 10.0
        assert sample_config['wake_word_timeout'] == 10.0
