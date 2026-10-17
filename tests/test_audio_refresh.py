"""Public refresh admission and the unchanged device-list helper.

Refresh admission accounts for active capture and cached device enumeration.
Fake PortAudio only; production app methods are AST-extracted, never imported.
"""
import types
import sys
from unittest.mock import Mock
import pytest
from samsara import microphone_recovery as recovery
from samsara.audio_devices import pick_index_by_name
from tests.test_audio_hotplug_recovery import Audio, dev, engine, recovery_app, app_method, MIC


@pytest.fixture
def case(monkeypatch):
    from samsara.audio_engine import engine as mod
    monkeypatch.setattr(mod.thread_registry, 'spawn', Mock())
    audio = Audio([dev(MIC)], default=0)
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    app = recovery_app(audio)
    app.recording = app.continuous_active = app.wake_word_active = False
    app._no_microphone = False
    app._ace_engine = engine(monkeypatch, audio, before_reopen=app._prepare_audio_rescan)
    app._ace_engine.start()
    app.refresh_audio_devices = lambda: app_method('refresh_audio_devices')(app)
    app._mic_refresh_blocked = lambda: app_method('_mic_refresh_blocked')(app)
    yield app, audio
    if app._ace_engine is not None:
        app._ace_engine.stop()


class TestIsAudioCaptureActiveUnchangedForCalibrationCaller:
    @pytest.mark.parametrize('attribute', ['recording', 'continuous_active', 'wake_word_active', 'engine'])
    def test_still_true_for_every_original_condition(self, case, attribute):
        app, audio = case
        if attribute != 'engine':
            app._ace_engine.stop()
            setattr(app, attribute, True)
        assert app_method('_is_audio_capture_active')(app) is True

    def test_false_when_nothing_active(self, case):
        app, audio = case
        app._ace_engine.stop()
        assert app_method('_is_audio_capture_active')(app) is False


class TestRefreshPicksUpNewDevice:
    def test_new_device_appears_after_reenumeration(self, case):
        app, audio = case
        audio.pending = [dev(MIC), dev('Bluetooth Headset Mic')]
        result = app.refresh_audio_devices()
        assert [d['name'] for d in result] == [MIC, 'Bluetooth Headset Mic']
        assert result is app.available_mics

    def test_reinit_dance_is_attempted(self, case):
        app, audio = case
        app.refresh_audio_devices()
        assert audio.events == ['terminate', 'initialize']

    def test_failed_reinit_reports_lost_and_retains_recovery_engine(self, case):
        app, audio = case
        def fail(): raise RuntimeError('PortAudio failed')
        audio._terminate = fail
        capture = app._ace_engine
        app.refresh_audio_devices()
        assert app._ace_engine is capture and capture.device_lost
        assert app._no_microphone


class TestCaptureSessionProtection:
    @pytest.mark.parametrize('attribute', ['recording', 'continuous_active', 'wake_word_active',
                                         'command_mode_active', 'ava_mode_active'])
    def test_live_capture_retains_stream_and_queues_explicit_refresh(self, case, attribute):
        app, audio = case
        stream = app._ace_engine.rescan_stream
        setattr(app, attribute, True)
        assert app._mic_refresh_blocked()
        assert app.refresh_audio_devices() is app.available_mics
        assert audio.events == [] and app._ace_engine.rescan_stream is stream
        assert recovery.changes_for(app)._input_pending

    def test_idle_engine_does_not_block_user_refresh(self, case):
        app, audio = case
        assert not app._mic_refresh_blocked()
        before = app._ace_engine.rescan_stream
        app.refresh_audio_devices()
        assert app._ace_engine.rescan_stream is not before
        assert app._ace_engine.running and not app._ace_engine.device_lost

    def test_deliberately_stopped_engine_is_left_alone(self, case):
        app, audio = case
        app._ace_engine.stop()
        app.refresh_audio_devices()
        assert not app._ace_engine.running and not app._ace_engine.device_lost
        assert audio.events == []

    def test_no_engine_still_refreshes_and_starts_capture(self, case):
        app, audio = case
        app._ace_engine.stop()
        app._ace_engine = None
        app.refresh_audio_devices()
        assert audio.events == ['terminate', 'initialize']
        app._start_ace_engine.assert_called_once()


class TestPickIndexByName:
    def test_finds_matching_device_by_name(self):
        devices = [{'name': 'Mic A'}, {'name': 'Mic B'}, {'name': 'Mic C'}]
        assert pick_index_by_name(devices, 'Mic B') == 1

    def test_first_match_position_zero(self):
        devices = [{'name': 'Mic A'}, {'name': 'Mic B'}]
        assert pick_index_by_name(devices, 'Mic A') == 0

    def test_missing_device_returns_none(self):
        devices = [{'name': 'Mic A'}, {'name': 'Mic B'}]
        assert pick_index_by_name(devices, 'Unplugged USB Mic') is None

    def test_empty_name_returns_none(self):
        devices = [{'name': 'Mic A'}]
        assert pick_index_by_name(devices, '') is None
        assert pick_index_by_name(devices, None) is None

    def test_empty_devices_list_returns_none(self):
        assert pick_index_by_name([], 'Mic A') is None


class TestGetAvailableMicrophonesWrapper:
    def test_get_available_microphones_delegates_to_shared_list_microphones(self, case):
        app, audio = case
        method = app_method('get_available_microphones',
                            list_microphones=lambda **kw: [{'id': 4, 'name': 'Delegated', 'channels': 2}])
        assert method(app) == [{'id': 4, 'name': 'Delegated', 'channels': 2}]

    def test_show_all_setting_passed_to_shared_helper(self, case):
        app, audio = case
        app.config['show_all_audio_devices'] = True
        helper = Mock(return_value=[])
        app_method('get_available_microphones', list_microphones=helper)(app)
        helper.assert_called_once_with(show_all=True)


class TestMissingDeviceFallbackToDefault:
    def test_fallback_selects_default_when_previous_device_gone(self, case):
        app, audio = case
        audio.pending = [dev('A Different Mic')]
        fresh = app.refresh_audio_devices()
        assert pick_index_by_name(fresh, MIC) is None
        assert app._capture_device == 0
        assert app.config['microphone_name'] == MIC
