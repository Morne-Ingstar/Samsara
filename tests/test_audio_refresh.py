"""Tests for the audio device refresh feature: DictationApp.refresh_audio_devices()
and the pure samsara.audio_devices.pick_index_by_name() selection helper.

sounddevice is fully monkeypatched -- no real PortAudio calls, no real audio
devices touched. Exercises the real production methods (DictationApp.method(app, ...)
pattern), not a re-implementation, matching test_transcription_params.py's convention.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation
from samsara.audio_devices import pick_index_by_name


# ============================================================================
# Fake sounddevice plumbing
# ============================================================================

class _FakeHostApi(dict):
    pass


def _device(name, max_input_channels=1, hostapi=0):
    return {'name': name, 'max_input_channels': max_input_channels, 'hostapi': hostapi}


class _FakeSd:
    """Stand-in for the `sd` module dictation.py imports as `sounddevice as sd`."""

    def __init__(self, devices):
        self.devices = devices
        self.query_devices_calls = 0
        self.terminate_calls = 0
        self.initialize_calls = 0
        self.terminate_should_raise = False

    def query_devices(self):
        self.query_devices_calls += 1
        return self.devices

    def query_hostapis(self):
        return [_FakeHostApi(name='Windows WASAPI')]

    def _terminate(self):
        self.terminate_calls += 1
        if self.terminate_should_raise:
            raise RuntimeError("simulated PortAudio terminate failure")

    def _initialize(self):
        self.initialize_calls += 1


def _make_app(fake_sd, recording=False, continuous_active=False,
              wake_word_active=False, ace_running=False, microphone=None,
              microphone_name=None):
    app = types.SimpleNamespace()
    app.config = {
        'show_all_audio_devices': False,
        'microphone': microphone,
        'microphone_name': microphone_name,
    }
    app.available_mics = []
    app.recording = recording
    app.continuous_active = continuous_active
    app.wake_word_active = wake_word_active
    app._ace_engine = types.SimpleNamespace(_running=ace_running) if ace_running else None

    app.get_available_microphones = types.MethodType(
        dictation.DictationApp.get_available_microphones, app)
    app.refresh_audio_devices = types.MethodType(
        dictation.DictationApp.refresh_audio_devices, app)
    app._is_audio_capture_active = types.MethodType(
        dictation.DictationApp._is_audio_capture_active, app)
    app._reconcile_microphone_selection = types.MethodType(
        dictation.DictationApp._reconcile_microphone_selection, app)
    return app


@pytest.fixture
def fake_sd(monkeypatch):
    fake = _FakeSd([_device("Built-in Mic")])
    monkeypatch.setattr(dictation, "sd", fake)
    return fake


# ============================================================================
# refresh_audio_devices() -- new device appears
# ============================================================================

class TestRefreshPicksUpNewDevice:
    def test_new_device_appears_after_reenumeration(self, fake_sd):
        app = _make_app(fake_sd)
        app.available_mics = app.get_available_microphones()
        assert [m['name'] for m in app.available_mics] == ["Built-in Mic"]

        # Simulate a Bluetooth mic connecting after launch.
        fake_sd.devices = [_device("Built-in Mic"), _device("Bluetooth Headset Mic")]

        result = app.refresh_audio_devices()

        names = [m['name'] for m in result]
        assert "Bluetooth Headset Mic" in names
        assert result is app.available_mics

    def test_reinit_dance_is_attempted(self, fake_sd):
        app = _make_app(fake_sd)
        app.refresh_audio_devices()
        assert fake_sd.terminate_calls == 1
        assert fake_sd.initialize_calls == 1

    def test_reinit_failure_falls_back_to_plain_requery(self, fake_sd):
        fake_sd.terminate_should_raise = True
        app = _make_app(fake_sd)

        result = app.refresh_audio_devices()

        # Re-init raised, but the method must not propagate -- and must
        # still return a freshly re-queried device list.
        assert [m['name'] for m in result] == ["Built-in Mic"]
        assert fake_sd.query_devices_calls >= 1


# ============================================================================
# Skip path -- audio active
# ============================================================================

class TestSkipWhenAudioActive:
    @pytest.mark.parametrize("kwargs", [
        {'recording': True},
        {'continuous_active': True},
        {'wake_word_active': True},
        {'ace_running': True},
    ])
    def test_enumeration_not_rerun_when_active(self, fake_sd, kwargs):
        app = _make_app(fake_sd, **kwargs)
        app.available_mics = [{'id': 0, 'name': 'Stale Cached Mic', 'channels': 1}]
        calls_before = fake_sd.query_devices_calls

        result = app.refresh_audio_devices()

        assert fake_sd.query_devices_calls == calls_before   # not re-run
        assert fake_sd.terminate_calls == 0
        assert fake_sd.initialize_calls == 0
        assert result == [{'id': 0, 'name': 'Stale Cached Mic', 'channels': 1}]
        assert result is app.available_mics

    def test_skip_logged_at_info(self, fake_sd, caplog):
        import logging
        app = _make_app(fake_sd, recording=True)
        with caplog.at_level(logging.INFO):
            app.refresh_audio_devices()
        assert any("refresh skipped" in r.message and "audio active" in r.message
                   for r in caplog.records)


# ============================================================================
# pick_index_by_name -- pure helper
# ============================================================================

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


# ============================================================================
# Missing-device fallback to default -- integration of the pure helper with
# the UI callback pattern (settings_qt.py / wizards): idx is None -> caller
# falls back to index 0 ("default"). Verified here at the call-pattern level.
# ============================================================================

class TestMissingDeviceFallbackToDefault:
    def test_fallback_selects_default_when_previous_device_gone(self, fake_sd):
        app = _make_app(fake_sd)
        app.available_mics = app.get_available_microphones()

        # The previously-selected mic was unplugged; the fresh list no
        # longer contains it.
        fake_sd.devices = [_device("A Different Mic")]
        fresh = app.refresh_audio_devices()

        idx = pick_index_by_name(fresh, "Built-in Mic")
        assert idx is None

        # Mirrors the settings_qt.py/wizard fallback: idx is None -> select
        # the first/default entry instead of leaving the combo unset.
        fallback_idx = idx if idx is not None else (0 if fresh else None)
        assert fallback_idx == 0
        assert fresh[fallback_idx]['name'] == "A Different Mic"
