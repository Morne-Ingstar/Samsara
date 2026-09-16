"""Queue 140: half-duplex capture for the latched SessionMode.AVA lane."""

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from samsara.audio_engine import wake_consumer as wc_module
from samsara.audio_engine.frame import FRAME_SIZE
from samsara.audio_engine.ring import EMPTY
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.config_defaults import cfg_get
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.session_modes import SessionMode


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _frame():
    return SimpleNamespace(
        pcm=np.full(FRAME_SIZE, 20000, dtype=np.int16),
        device_epoch=0,
        seq=0,
    )


def _make_consumer(monkeypatch, *, tts_speaking=True, enabled=True, detector=None):
    engine = Mock()
    # On a real onset _process_frame rewinds the ring and consumes the current
    # frame through this reader.  Supply that one frame, then an empty ring.
    reader = Mock(read_next=Mock(side_effect=[_frame()] + [EMPTY] * 100))
    engine.register_consumer.return_value = reader
    coordinator = SimpleNamespace(is_speaking=tts_speaking, cancel_speech=Mock())
    app = SimpleNamespace(
        app_state="asleep",
        wake_word_active=True,
        wake_word_triggered=False,
        _wake_rearm_needs_onset=False,
        _hotkey_recording=False,
        command_mode_active=True,
        ava_command_session_active=False,
        _session_mode_manager=SimpleNamespace(mode=SessionMode.AVA),
        config={
            "command_mode": {"mode": "toggle", "ava_tts_half_duplex": enabled},
            "wake_word_config": {"audio": {"wake_detection_silence": 1.0}},
        },
        is_speaking=False,
        silence_start=None,
        _command_executed_at=None,
        _tts_last_speaking=0.0,
        _vad_available=True,
        _vad_is_speech=Mock(return_value=True),
        _vad_reset=Mock(),
        _wake_detector=detector,
        _wake_profile_detectors={},
        _oww_wake_detected=False,
        audio_coordinator=coordinator,
        echo_canceller=SimpleNamespace(is_active=False),
    )
    clock = _Clock()
    monkeypatch.setattr(wc_module.time, "monotonic", clock)
    consumer = WakeConsumer(engine, app)
    # This suite enters _process_frame directly rather than through the wake
    # transition that clears this latch.  It is orthogonal to AVA half-duplex
    # and would otherwise reject the first post-reply speech frame.
    consumer._await_wake_onset = False
    return consumer, app, coordinator, clock


def test_active_capture_is_discarded_during_latched_ava_tts(monkeypatch):
    consumer, app, _coordinator, _clock = _make_consumer(monkeypatch)
    app.is_speaking = True
    consumer._utterance_frames = [np.zeros(FRAME_SIZE, dtype=np.float32)]

    consumer._process_frame(_frame())

    app._vad_is_speech.assert_not_called()
    assert consumer._utterance_frames == []
    assert app.is_speaking is False


def test_fresh_onset_is_dropped_during_latched_ava_tts(monkeypatch):
    consumer, app, _coordinator, _clock = _make_consumer(monkeypatch)

    consumer._process_frame(_frame())

    app._vad_is_speech.assert_not_called()
    assert consumer._utterance_frames == []
    assert app.is_speaking is False


def test_capture_rearms_400ms_after_latched_ava_tts(monkeypatch):
    consumer, app, coordinator, clock = _make_consumer(monkeypatch)
    consumer._process_frame(_frame())
    coordinator.is_speaking = False
    consumer._process_frame(_frame())  # observes the SPEAKING -> tail transition
    clock.advance(0.4)

    consumer._process_frame(_frame())

    app._vad_is_speech.assert_called_once()
    assert len(consumer._utterance_frames) == 1


def test_config_off_restores_active_capture_behavior(monkeypatch):
    consumer, app, _coordinator, _clock = _make_consumer(monkeypatch, enabled=False)
    app.is_speaking = True

    consumer._process_frame(_frame())

    app._vad_is_speech.assert_called_once()
    assert len(consumer._utterance_frames) == 1


def test_wake_hit_interrupts_reply_and_opens_fresh_capture(monkeypatch):
    detector = SimpleNamespace(is_available=True, detected=Mock(return_value=True), reset=Mock())
    consumer, app, coordinator, _clock = _make_consumer(monkeypatch, detector=detector)
    app.is_speaking = True
    consumer._utterance_frames = [np.zeros(FRAME_SIZE, dtype=np.float32)]
    coordinator.cancel_speech.side_effect = lambda: setattr(coordinator, "is_speaking", False)

    consumer._process_frame(_frame())

    coordinator.cancel_speech.assert_called_once()
    detector.reset.assert_called_once()
    app._vad_is_speech.assert_called_once()
    assert len(consumer._utterance_frames) == 1
    assert app.is_speaking is True


def test_config_default_is_on():
    assert SETTINGS_SCHEMA["command_mode.ava_tts_half_duplex"]["default"] is True
    assert cfg_get({}, "command_mode.ava_tts_half_duplex") is True


def test_ava_reply_helper_requests_interruptible_speech(monkeypatch):
    from plugins.commands import ask_ollama

    coordinator = Mock()
    coordinator.speak.return_value = SimpleNamespace(utterance_id="reply")
    app = SimpleNamespace(config={}, audio_coordinator=coordinator)
    monkeypatch.setattr(ask_ollama, "get_max_response_length", lambda _app: 0)

    assert ask_ollama.speak(app, "Wake me with the word.") is True

    coordinator.speak.assert_called_once_with(
        "Wake me with the word.", category="ava_response", interruptible=True,
    )
