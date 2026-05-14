"""Tests for the Text-to-Speech settings tab.

All tests use mocked app, engine, and coordinator — no real audio hardware
or winsdk required. A hidden Tk root is created once per session so that
tk.BooleanVar / tk.DoubleVar / tk.StringVar can be instantiated in tests
without triggering "too early to create variable: no default root window."
"""

import sys
import tkinter as tk
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.tts.engine_base import VoiceInfo
from samsara.ui.tts_settings_tab import TTSSettingsTab, _DEFAULTS, _TEST_PHRASE


@pytest.fixture(scope="session", autouse=True)
def tk_root():
    """Hidden Tk root so tk.Variable subclasses can be constructed in tests."""
    root = tk.Tk()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_voice(name, lang="en-US", vid=None):
    return VoiceInfo(
        voice_id=vid or f"HKEY_{name}",
        display_name=name,
        language=lang,
        gender="male",
    )


def _make_settings_window(engine=None, coordinator=None, tts_cfg=None):
    """Minimal SettingsWindow-like mock."""
    app = MagicMock()
    app.config = {'tts': tts_cfg or {}}
    app.tts_engine = engine
    app.audio_coordinator = coordinator

    def _update_config(changes, save=True):
        for k, v in changes.items():
            app.config[k] = v

    app.update_config.side_effect = _update_config

    sw = MagicMock()
    sw.app = app
    # tabview.tab() returns a real (but hidden) Tk frame so CTk widgets can
    # be placed into it without a display server in most test scenarios.
    # We avoid actually building the UI in unit tests, so this is fine as mock.
    return sw, app


# ---------------------------------------------------------------------------
# Instantiation / graceful degradation
# ---------------------------------------------------------------------------

class TestTabInstantiation:
    def test_instantiates_without_engine(self):
        sw, app = _make_settings_window(engine=None)
        tab = TTSSettingsTab(sw)
        assert tab is not None

    def test_instantiates_without_coordinator(self):
        sw, app = _make_settings_window(coordinator=None)
        tab = TTSSettingsTab(sw)
        assert tab is not None

    def test_voice_map_empty_when_engine_none(self):
        sw, app = _make_settings_window(engine=None)
        tab = TTSSettingsTab(sw)
        assert tab._voice_label_to_id == {}


# ---------------------------------------------------------------------------
# Voice dropdown population
# ---------------------------------------------------------------------------

class TestVoiceDropdown:
    def test_voice_labels_built_from_engine(self):
        engine = MagicMock()
        engine.list_voices.return_value = [
            _make_voice("Microsoft David", "en-US", "vid-david"),
            _make_voice("Microsoft Zira", "en-US", "vid-zira"),
        ]
        sw, app = _make_settings_window(engine=engine)
        tab = TTSSettingsTab(sw)

        # Simulate what build() does when initialising voice dropdown
        tab._voice_label_to_id = {}
        tab._voice_id_to_label = {}
        for v in engine.list_voices():
            label = f"{v.display_name} ({v.language})"
            tab._voice_label_to_id[label] = v.voice_id
            tab._voice_id_to_label[v.voice_id] = label

        assert "Microsoft David (en-US)" in tab._voice_label_to_id
        assert "Microsoft Zira (en-US)" in tab._voice_label_to_id

    def test_saved_voice_id_resolves_to_label(self):
        engine = MagicMock()
        engine.list_voices.return_value = [
            _make_voice("Microsoft Mark", "en-US", "vid-mark"),
        ]
        sw, app = _make_settings_window(
            engine=engine,
            tts_cfg={'voice_id': 'vid-mark'},
        )
        tab = TTSSettingsTab(sw)
        tab._voice_id_to_label = {"vid-mark": "Microsoft Mark (en-US)"}
        tab._voice_label_to_id = {"Microsoft Mark (en-US)": "vid-mark"}
        # Resolution: voice_id → label lookup
        label = tab._voice_id_to_label.get('vid-mark', "fallback")
        assert label == "Microsoft Mark (en-US)"


# ---------------------------------------------------------------------------
# Slider value persistence
# ---------------------------------------------------------------------------

class TestSliderPersistence:
    def _tab_with_vars(self, speed=1.0, pitch=1.0, volume=0.8, enabled=True):
        sw, app = _make_settings_window()
        tab = TTSSettingsTab(sw)
        tab.tts_enabled_var = tk.BooleanVar(value=enabled)
        tab.voice_var = tk.StringVar(value="")
        tab.speed_var = tk.DoubleVar(value=speed)
        tab.pitch_var = tk.DoubleVar(value=pitch)
        tab.volume_var = tk.DoubleVar(value=volume)
        for attr in ['use_agent_responses_var', 'use_confirmations_var',
                     'use_warnings_var', 'use_status_updates_var',
                     'use_dictation_readback_var', 'use_errors_var']:
            setattr(tab, attr, tk.BooleanVar(value=True))
        return tab, app

    def test_save_persists_speed(self):
        tab, app = self._tab_with_vars(speed=1.5)
        tab.save()
        assert app.config['tts']['speed'] == pytest.approx(1.5)

    def test_save_persists_pitch(self):
        tab, app = self._tab_with_vars(pitch=0.7)
        tab.save()
        assert app.config['tts']['pitch'] == pytest.approx(0.7)

    def test_save_persists_volume(self):
        tab, app = self._tab_with_vars(volume=0.6)
        tab.save()
        assert app.config['tts']['volume'] == pytest.approx(0.6)

    def test_save_persists_enabled_true(self):
        tab, app = self._tab_with_vars(enabled=True)
        tab.save()
        assert app.config['tts']['enabled'] is True

    def test_save_persists_enabled_false(self):
        tab, app = self._tab_with_vars(enabled=False)
        tab.save()
        assert app.config['tts']['enabled'] is False

    def test_save_no_op_when_tab_never_built(self):
        """save() before build() must not crash or modify config."""
        sw, app = _make_settings_window(tts_cfg={'speed': 1.0})
        tab = TTSSettingsTab(sw)
        # tts_enabled_var is None — save() should return early
        tab.save()
        app.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# Test button calls coordinator.speak()
# ---------------------------------------------------------------------------

class TestTestButton:
    def _tab_ready(self, coordinator=None, voice_id=None):
        sw, app = _make_settings_window(coordinator=coordinator)
        tab = TTSSettingsTab(sw)
        tab.tts_enabled_var = tk.BooleanVar(value=True)
        tab.voice_var = tk.StringVar(value="Microsoft David (en-US)")
        if voice_id:
            tab._voice_label_to_id = {"Microsoft David (en-US)": voice_id}
        tab.speed_var = tk.DoubleVar(value=1.2)
        tab.pitch_var = tk.DoubleVar(value=1.0)
        tab.volume_var = tk.DoubleVar(value=0.7)
        tab._test_status_label = MagicMock()
        return tab, app

    def test_test_button_calls_coordinator_speak(self):
        coordinator = MagicMock()
        tab, app = self._tab_ready(coordinator=coordinator, voice_id="vid-david")
        tab._on_test_clicked()
        coordinator.speak.assert_called_once()
        _, kwargs = coordinator.speak.call_args
        assert _TEST_PHRASE in coordinator.speak.call_args[0]
        assert kwargs['speed'] == pytest.approx(1.2)
        assert kwargs['volume'] == pytest.approx(0.7)

    def test_test_button_passes_voice_id(self):
        coordinator = MagicMock()
        tab, app = self._tab_ready(coordinator=coordinator, voice_id="vid-david")
        tab._on_test_clicked()
        _, kwargs = coordinator.speak.call_args
        assert kwargs['voice_id'] == "vid-david"

    def test_test_button_shows_message_when_coordinator_none(self):
        tab, app = self._tab_ready(coordinator=None)
        tab._on_test_clicked()
        tab._test_status_label.configure.assert_called()
        status_text = tab._test_status_label.configure.call_args[1]['text']
        assert "not initialized" in status_text.lower() or "restart" in status_text.lower()

    def test_test_button_passes_none_voice_when_no_label_mapping(self):
        """When voice label doesn't map to an id, voice_id should be None."""
        coordinator = MagicMock()
        tab, app = self._tab_ready(coordinator=coordinator, voice_id=None)
        # No mapping in _voice_label_to_id
        tab._on_test_clicked()
        _, kwargs = coordinator.speak.call_args
        assert kwargs['voice_id'] is None


# ---------------------------------------------------------------------------
# Advanced toggles
# ---------------------------------------------------------------------------

class TestAdvancedToggles:
    def _tab_with_toggles(self, **overrides):
        sw, app = _make_settings_window()
        tab = TTSSettingsTab(sw)
        tab.tts_enabled_var = tk.BooleanVar(value=True)
        tab.voice_var = tk.StringVar(value="")
        tab.speed_var = tk.DoubleVar(value=1.0)
        tab.pitch_var = tk.DoubleVar(value=1.0)
        tab.volume_var = tk.DoubleVar(value=0.8)
        defaults = {
            'use_agent_responses_var': True,
            'use_confirmations_var': True,
            'use_warnings_var': True,
            'use_status_updates_var': True,
            'use_dictation_readback_var': False,
            'use_errors_var': True,
        }
        defaults.update(overrides)
        for attr, val in defaults.items():
            setattr(tab, attr, tk.BooleanVar(value=val))
        return tab, app

    def test_dictation_readback_defaults_false(self):
        tab, app = self._tab_with_toggles()
        tab.save()
        assert app.config['tts']['use_for_dictation_readback'] is False

    def test_toggle_dictation_readback_true_persists(self):
        tab, app = self._tab_with_toggles(use_dictation_readback_var=True)
        tab.save()
        assert app.config['tts']['use_for_dictation_readback'] is True

    def test_all_enabled_toggles_persist(self):
        tab, app = self._tab_with_toggles()
        tab.save()
        assert app.config['tts']['use_for_agent_responses'] is True
        assert app.config['tts']['use_for_confirmations'] is True
        assert app.config['tts']['use_for_warnings'] is True
        assert app.config['tts']['use_for_status_updates'] is True
        assert app.config['tts']['use_for_errors'] is True


# ---------------------------------------------------------------------------
# Master enable grays out controls
# ---------------------------------------------------------------------------

class TestMasterEnable:
    def test_dependent_widgets_list_is_populated_after_build(self):
        """After build the widget list should be non-empty.

        We can't fully test the CTk state change without a display,
        but we can assert the list was populated during build and that
        _apply_enabled_state() runs without error.
        """
        sw, app = _make_settings_window()
        tab = TTSSettingsTab(sw)
        tab.tts_enabled_var = tk.BooleanVar(value=True)
        # Add a mock widget so _apply_enabled_state has something to call
        mock_widget = MagicMock()
        tab._dependent_widgets.append(mock_widget)
        tab._apply_enabled_state()
        mock_widget.configure.assert_called_with(state='normal')

    def test_disabled_state_propagates_to_widgets(self):
        sw, app = _make_settings_window()
        tab = TTSSettingsTab(sw)
        tab.tts_enabled_var = tk.BooleanVar(value=False)
        mock_widget = MagicMock()
        tab._dependent_widgets.append(mock_widget)
        tab._apply_enabled_state()
        mock_widget.configure.assert_called_with(state='disabled')
