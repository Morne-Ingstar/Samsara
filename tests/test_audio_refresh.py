"""Tests for the audio device refresh feature: DictationApp.refresh_audio_devices()
and the pure samsara.audio_devices.pick_index_by_name() selection helper.

sounddevice is fully monkeypatched -- no real PortAudio calls, no real audio
devices touched. Exercises the real production methods (DictationApp.method(app, ...)
pattern), not a re-implementation, matching test_transcription_params.py's convention.

2026-07-17: refresh_audio_devices() used to gate on _is_audio_capture_active(),
which is true almost the entire life of the process (the ACE engine starts at
boot and runs permanently) -- so the guard was a constant True and the
function never actually re-enumerated in production. Fixed by splitting the
guard: refresh_audio_devices() now uses the new, narrower
_mic_refresh_blocked() (true only for an actual in-progress recording hold)
and cycles the ACE engine's stop()/start() around the PortAudio re-init
instead of treating "the engine exists" as a block.
_is_audio_capture_active() itself is UNCHANGED and still correct for its one
remaining caller, calibrate_echo_cancellation() -- see
TestIsAudioCaptureActiveUnchangedForCalibrationCaller.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation
from samsara.audio_devices import pick_index_by_name
from samsara import audio_devices as shared_audio_devices


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


class _FakeAceEngine:
    """Stand-in for AudioCaptureEngine -- tracks stop()/start() calls and
    toggles _running the same way the real engine does (see
    samsara/audio_engine/engine.py's stop()/start()), so
    refresh_audio_devices()'s cycle-around-reinit logic can be verified
    without a real PortAudio stream."""

    def __init__(self, running=False):
        self._running = running
        self.stop_calls = 0
        self.start_calls = 0
        self.stop_should_raise = False
        self.start_should_raise = False

    def stop(self):
        self.stop_calls += 1
        if self.stop_should_raise:
            raise RuntimeError("simulated ACE stop failure")
        self._running = False

    def start(self):
        self.start_calls += 1
        if self.start_should_raise:
            raise RuntimeError("simulated ACE start failure")
        self._running = True


def _make_app(fake_sd, recording=False, continuous_active=False,
              wake_word_active=False, ace_running=False, microphone=None,
              microphone_name=None, ace_engine=None):
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
    if ace_engine is not None:
        app._ace_engine = ace_engine
    elif ace_running:
        app._ace_engine = _FakeAceEngine(running=True)
    else:
        app._ace_engine = None

    app.get_available_microphones = types.MethodType(
        dictation.DictationApp.get_available_microphones, app)
    app.refresh_audio_devices = types.MethodType(
        dictation.DictationApp.refresh_audio_devices, app)
    app._is_audio_capture_active = types.MethodType(
        dictation.DictationApp._is_audio_capture_active, app)
    app._mic_refresh_blocked = types.MethodType(
        dictation.DictationApp._mic_refresh_blocked, app)
    app._reconcile_microphone_selection = types.MethodType(
        dictation.DictationApp._reconcile_microphone_selection, app)
    return app


# ============================================================================
# _is_audio_capture_active() itself -- must NOT be touched by this fix.
# Still the correct (unchanged) gate for calibrate_echo_cancellation(),
# which does its own blocking sd.play()/sd.rec() and genuinely conflicts
# with a concurrently-open ACE stream on the same device.
# ============================================================================

class TestIsAudioCaptureActiveUnchangedForCalibrationCaller:
    @pytest.mark.parametrize("kwargs", [
        {'recording': True},
        {'continuous_active': True},
        {'wake_word_active': True},
        {'ace_running': True},
    ])
    def test_still_true_for_every_original_condition(self, fake_sd, kwargs):
        app = _make_app(fake_sd, **kwargs)
        assert app._is_audio_capture_active() is True

    def test_false_when_nothing_active(self, fake_sd):
        app = _make_app(fake_sd)
        assert app._is_audio_capture_active() is False


@pytest.fixture
def fake_sd(monkeypatch):
    fake = _FakeSd([_device("Built-in Mic")])
    monkeypatch.setattr(dictation, "sd", fake)
    monkeypatch.setattr(shared_audio_devices, "sd", fake)
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
# Hard block -- self.recording ONLY. 2026-07-17 fix: refresh_audio_devices()
# used to gate on _is_audio_capture_active(), which OR's in the ACE engine's
# always-on _running flag -- true almost the entire life of the process, so
# this guard was a constant True and refresh_audio_devices() never actually
# ran in production. continuous_active/wake_word_active/ace_running must
# now all let the refresh PROCEED (see TestRefreshReachableWithOnlyAceActive
# and TestAceEngineCycledAroundReinit below) -- only an actual dictation
# hold (self.recording) still refuses outright.
# ============================================================================

class TestHardBlockOnRecordingOnly:
    def test_recording_still_hard_blocks(self, fake_sd):
        app = _make_app(fake_sd, recording=True)
        app.available_mics = [{'id': 0, 'name': 'Stale Cached Mic', 'channels': 1}]
        calls_before = fake_sd.query_devices_calls

        result = app.refresh_audio_devices()

        assert fake_sd.query_devices_calls == calls_before   # not re-run
        assert fake_sd.terminate_calls == 0
        assert fake_sd.initialize_calls == 0
        assert result == [{'id': 0, 'name': 'Stale Cached Mic', 'channels': 1}]
        assert result is app.available_mics

    def test_recording_skip_logged_at_info_distinctly(self, fake_sd, caplog):
        import logging
        app = _make_app(fake_sd, recording=True)
        with caplog.at_level(logging.INFO):
            app.refresh_audio_devices()
        assert any(
            "refresh skipped" in r.message and "dictation hold" in r.message
            for r in caplog.records
        )

    @pytest.mark.parametrize("kwargs", [
        {'continuous_active': True},
        {'wake_word_active': True},
        {'ace_running': True},
    ])
    def test_none_of_these_alone_hard_block(self, fake_sd, kwargs):
        """The exact production bug: with ONLY these states active (no
        actual recording), refresh must reach real re-enumeration -- this
        would have failed against the old _is_audio_capture_active() gate."""
        app = _make_app(fake_sd, **kwargs)
        assert app._mic_refresh_blocked() is False


# ============================================================================
# THE production bug, reproduced directly: ACE running, nothing else --
# the exact state a normal idle Samsara session is in almost all the time.
# ============================================================================

class TestRefreshReachableWithOnlyAceActive:
    def test_reenumeration_actually_runs(self, fake_sd):
        ace = _FakeAceEngine(running=True)
        app = _make_app(fake_sd, ace_engine=ace)
        app.available_mics = app.get_available_microphones()
        fake_sd.devices = [_device("Built-in Mic"), _device("Newly Plugged Mic")]

        result = app.refresh_audio_devices()

        assert fake_sd.terminate_calls == 1
        assert fake_sd.initialize_calls == 1
        names = [m['name'] for m in result]
        assert "Newly Plugged Mic" in names

    def test_ace_stopped_before_and_restarted_after(self, fake_sd):
        ace = _FakeAceEngine(running=True)
        app = _make_app(fake_sd, ace_engine=ace)

        app.refresh_audio_devices()

        assert ace.stop_calls == 1
        assert ace.start_calls == 1
        assert ace._running is True   # restored to its prior state


# ============================================================================
# finally-block guarantee: capture must be restarted even if the re-init
# (or anything between stop() and start()) raises.
# ============================================================================

class TestAceRestartedAfterRaisingReinit:
    def test_restarted_after_portaudio_reinit_raises(self, fake_sd):
        fake_sd.terminate_should_raise = True
        ace = _FakeAceEngine(running=True)
        app = _make_app(fake_sd, ace_engine=ace)

        # sd._terminate() raising is already caught+logged inside
        # refresh_audio_devices() (falls back to plain re-query) -- must
        # not prevent restart either.
        app.refresh_audio_devices()

        assert ace.stop_calls == 1
        assert ace.start_calls == 1
        assert ace._running is True

    def test_restarted_even_if_reconcile_raises(self, fake_sd, monkeypatch):
        ace = _FakeAceEngine(running=True)
        app = _make_app(fake_sd, ace_engine=ace)

        def _boom():
            raise RuntimeError("simulated reconcile failure")
        app._reconcile_microphone_selection = _boom

        with pytest.raises(RuntimeError):
            app.refresh_audio_devices()

        assert ace.start_calls == 1
        assert ace._running is True

    def test_not_left_running_false_if_start_itself_fails(self, fake_sd, caplog):
        """start() failing is logged, not swallowed silently or re-raised
        past refresh_audio_devices() -- the caller still gets back
        whatever device list it managed to get."""
        import logging
        ace = _FakeAceEngine(running=True)
        ace.start_should_raise = True
        app = _make_app(fake_sd, ace_engine=ace)

        with caplog.at_level(logging.ERROR):
            result = app.refresh_audio_devices()

        assert ace.start_calls == 1
        assert result is app.available_mics


class TestAceNotTouchedWhenNotRunning:
    def test_stop_and_start_not_called_when_ace_was_never_running(self, fake_sd):
        ace = _FakeAceEngine(running=False)
        app = _make_app(fake_sd, ace_engine=ace)

        app.refresh_audio_devices()

        assert ace.stop_calls == 0
        assert ace.start_calls == 0

    def test_no_ace_engine_at_all_still_refreshes(self, fake_sd):
        app = _make_app(fake_sd)  # ace_engine=None, ace_running=False
        assert app._ace_engine is None

        result = app.refresh_audio_devices()

        assert fake_sd.terminate_calls == 1
        assert [m['name'] for m in result] == ["Built-in Mic"]


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


class TestGetAvailableMicrophonesWrapper:
    def test_get_available_microphones_delegates_to_shared_list_microphones(self, fake_sd, monkeypatch):
        app = _make_app(fake_sd)
        monkeypatch.setattr(
            "dictation.list_microphones",
            lambda show_all=False: [{'id': 4, 'name': 'Delegated', 'channels': 2}],
        )

        result = app.get_available_microphones()

        assert result == [{'id': 4, 'name': 'Delegated', 'channels': 2}]

    def test_show_all_setting_passed_to_shared_helper(self, fake_sd, monkeypatch):
        app = _make_app(fake_sd)
        app.config['show_all_audio_devices'] = True
        calls = []

        def _fake_list_microphones(show_all=False):
            calls.append(show_all)
            return [{'id': 5, 'name': 'WithShowAll', 'channels': 1}]

        monkeypatch.setattr("dictation.list_microphones", _fake_list_microphones)

        app.get_available_microphones()

        assert calls == [True]


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
