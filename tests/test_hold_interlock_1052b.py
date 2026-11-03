"""1052b: real startup and wake suppression; no hardware or app bootstrap."""
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from samsara.audio_engine import wake_consumer as wake
from tests.test_wake_consumer_hotkey_deafness import _make_wc, _loud_frame


def app(**changes):
    values = dict(recording=False, _hotkey_recording=False, wake_word_active=True,
                  _hold_capture_lifecycle_lock=threading.RLock(),
                  _dictation_consumer=NS(_active=False, _streaming_capturing=False),
                  _start_hands_free_idle_duck=Mock(), play_sound=Mock())
    values.update(changes)
    return NS(**values)


@pytest.mark.parametrize('flag', ['recording', '_hotkey_recording', '_active', '_streaming_capturing'])
def test_any_open_hold_defers_both_startup_outputs(flag, monkeypatch, caplog):
    host = app()
    target = host._dictation_consumer if flag.startswith(('_active', '_streaming')) else host
    setattr(target, flag, True)
    monkeypatch.setattr(wake.time, 'sleep', Mock())
    with caplog.at_level('INFO'):
        wake._on_wake_ready(host)
    host.play_sound.assert_not_called()
    host._start_hands_free_idle_duck.assert_not_called()
    assert 'wake ready: cue skipped, hold active' in caplog.text
    setattr(target, flag, False)
    wake.resume_wake_ready_output(host)
    wake.resume_wake_ready_output(host)
    host.play_sound.assert_called_once_with('start', use_winsound=True)
    host._start_hands_free_idle_duck.assert_called_once_with()


def test_release_flags_do_not_end_consumer_capture(monkeypatch):
    host = app(recording=True)
    monkeypatch.setattr(wake.time, 'sleep', Mock())
    wake._on_wake_ready(host)
    host.recording = False
    host._dictation_consumer._streaming_capturing = True
    wake.resume_wake_ready_output(host)
    host.play_sound.assert_not_called()
    host._dictation_consumer._streaming_capturing = False
    wake.resume_wake_ready_output(host)
    host.play_sound.assert_called_once()


def test_cancelled_wake_mode_does_not_replay_stale_output(monkeypatch):
    import dictation

    host = app(recording=True)
    wake._on_wake_ready(host)
    host.recording = False
    host.wake_word_active = False
    host._hands_free_duck_lock = threading.RLock()
    host._hands_free_duck_restore_timer = None
    host._hands_free_capture_ducker = None
    host._hands_free_capture_duck_start_generation = 0
    host._hands_free_capture_duck_owners = set()
    host._hands_free_capture_duck_restore_generation = 0
    host._hands_free_idle_ducker = None
    host._bump_wake_gate_freeze = Mock()
    dictation.DictationApp._stop_hands_free_idle_duck(host)
    wake.resume_wake_ready_output(host)
    host.wake_word_active = True
    wake.resume_wake_ready_output(host)
    host.play_sound.assert_not_called()
    host._start_hands_free_idle_duck.assert_not_called()


def test_existing_toggle_poll_cannot_cancel_startup_before_wake_state_is_published(monkeypatch):
    host = app(recording=True, wake_word_active=False)
    monkeypatch.setattr(wake.time, 'sleep', Mock())
    wake._on_wake_ready(host)
    wake.resume_wake_ready_output(host)
    assert host._wake_ready_output_pending is True
    host.wake_word_active = True
    host.recording = False
    wake.resume_wake_ready_output(host)
    host.play_sound.assert_called_once()


def test_readiness_during_hold_still_arms_and_engages_suppression(monkeypatch):
    import dictation  # Executed only through the reviewed no-bootstrap adapter.

    consumer, _, host = _make_wc(hotkey_recording=True)
    host.recording = True
    host.model_loaded = True
    host.config['wake_word_config'] = {'phrase': 'jarvis'}
    host._hold_capture_lifecycle_lock = threading.RLock()
    host._dictation_consumer = NS(_active=True, _streaming_capturing=False)
    host._request_wake_models = Mock(return_value=True)
    host.listening_indicator = None
    monkeypatch.setattr(wake.time, 'sleep', Mock())
    dictation.DictationApp.start_wake_word_mode(host)
    host._ensure_wake_consumer.assert_called_once_with('wake_word')
    host.set_app_state.assert_called_once_with(wake_word_active=True)
    host.play_sound.assert_not_called()
    host._start_hands_free_idle_duck.assert_not_called()
    consumer._process_frame(_loud_frame())
    assert consumer._hotkey_suppressed_last is True
    host._vad_is_speech.assert_not_called()


def test_idle_start_keeps_existing_outputs(monkeypatch):
    host = app()
    monkeypatch.setattr(wake.time, 'sleep', Mock())
    wake._on_wake_ready(host)
    host._start_hands_free_idle_duck.assert_called_once_with()
    host.play_sound.assert_called_once_with('start', use_winsound=True)
    assert host._wake_ready_output_pending is False


def test_startup_output_uses_the_hold_admission_lock(monkeypatch):
    host = app()
    monkeypatch.setattr(wake.time, 'sleep', Mock())
    ownership = []
    host.play_sound.side_effect = lambda *a, **kw: ownership.append(
        host._hold_capture_lifecycle_lock._is_owned())
    wake._on_wake_ready(host)
    assert ownership == [True]


def test_async_continuous_autostart_uses_the_same_hold_interlock(monkeypatch):
    import dictation
    import numpy as np
    from samsara.audio_engine.continuous_consumer import ContinuousConsumer

    host = app(recording=True, model_loaded=True, continuous_active=False,
               config={},
               set_app_state=Mock(), _request_icon_chase=Mock(),
               listening_indicator=NS(set_listening=Mock()), _schedule_ui=Mock())
    consumer = ContinuousConsumer(Mock(), host)
    consumer.start = Mock()
    host._continuous_consumer = consumer
    monkeypatch.setattr(wake.time, 'sleep', Mock())
    dictation.DictationApp.start_continuous_mode(host)
    host._continuous_consumer.start.assert_called_once()
    host.play_sound.assert_not_called()
    host._start_hands_free_idle_duck.assert_not_called()
    host.continuous_active = True
    host.recording = False
    quiet = NS(pcm=np.zeros(512, dtype=np.int16))
    consumer._process_frame(quiet)
    consumer._process_frame(quiet)
    host.play_sound.assert_called_once_with('start', use_winsound=True)
    host._start_hands_free_idle_duck.assert_not_called()
