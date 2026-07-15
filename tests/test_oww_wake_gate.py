"""Focused regressions for OpenWakeWord and the secondary RMS gate."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from dictation import DictationApp
from samsara.audio_engine import wake_consumer as wake_consumer_module
from samsara.audio_engine.wake_consumer import WakeConsumer


def _sleeping_consumer(*, detected, wake_profiles=None):
    detector = SimpleNamespace(is_available=True, reset=Mock())
    app = SimpleNamespace(
        config={"wake_profiles": wake_profiles or []},
        _wake_detector=detector,
        app_state="asleep",
        wake_word_triggered=False,
        _oww_wake_detected=detected,
        process_wake_word_buffer=Mock(),
    )
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._is_ai_cmd_mode = lambda candidate: False
    consumer._is_toggle_cmd = lambda candidate: False
    return consumer, app, detector


def test_oww_hit_is_forwarded_across_async_dispatch(monkeypatch):
    consumer, app, _detector = _sleeping_consumer(detected=True)
    dispatched = {}

    def fake_spawn(name, target, args=(), kwargs=None, daemon=True):
        dispatched.update(name=name, target=target, args=args, kwargs=kwargs, daemon=daemon)

    monkeypatch.setattr(wake_consumer_module.thread_registry, "spawn", fake_spawn)

    consumer._flush([np.zeros(160, dtype=np.float32)])

    assert dispatched["target"] is app.process_wake_word_buffer
    assert dispatched["kwargs"] == {"oww_confirmed": True}
    assert app._oww_wake_detected is False


def test_no_oww_hit_still_drops_buffer_before_whisper(monkeypatch):
    consumer, _app, detector = _sleeping_consumer(detected=False)
    spawn = Mock()
    monkeypatch.setattr(wake_consumer_module.thread_registry, "spawn", spawn)

    consumer._flush([np.zeros(160, dtype=np.float32)])

    spawn.assert_not_called()
    detector.reset.assert_called_once_with()


def test_oww_hit_stays_confirmed_when_whisper_profiles_are_enabled(monkeypatch):
    consumer, app, _detector = _sleeping_consumer(
        detected=True,
        wake_profiles=[{"id": "hermes", "phrase": "activate hermes", "enabled": True}],
    )
    dispatched = {}

    def fake_spawn(name, target, args=(), kwargs=None, daemon=True):
        dispatched.update(name=name, target=target, args=args, kwargs=kwargs, daemon=daemon)

    monkeypatch.setattr(wake_consumer_module.thread_registry, "spawn", fake_spawn)

    consumer._flush([np.zeros(160, dtype=np.float32)])

    assert dispatched["target"] is app.process_wake_word_buffer
    assert dispatched["kwargs"] == {"oww_confirmed": True}
    assert app._oww_wake_detected is False


def test_profile_fallback_still_reaches_whisper_without_primary_oww_hit(monkeypatch):
    consumer, app, detector = _sleeping_consumer(
        detected=False,
        wake_profiles=[{"id": "hermes", "phrase": "activate hermes", "enabled": True}],
    )
    dispatched = {}

    def fake_spawn(name, target, args=(), kwargs=None, daemon=True):
        dispatched.update(name=name, target=target, args=args, kwargs=kwargs, daemon=daemon)

    monkeypatch.setattr(wake_consumer_module.thread_registry, "spawn", fake_spawn)

    consumer._flush([np.zeros(160, dtype=np.float32)])

    assert dispatched["target"] is app.process_wake_word_buffer
    assert dispatched["kwargs"] == {"oww_confirmed": False}
    detector.reset.assert_not_called()


def _gate_app(*, adaptive=True, floor=None, threshold=0.02):
    app = DictationApp.__new__(DictationApp)
    app.config = {
        "wake_word_config": {
            "audio": {
                "adaptive_gate": adaptive,
                "speech_threshold": threshold,
            }
        }
    }
    app._wake_noise_floor = floor
    return app


def test_oww_confirmed_buffer_bypasses_gate_without_polluting_noise_floor():
    app = _gate_app(floor=None)

    rejected = app._wake_audio_is_below_gate(0.0284, oww_confirmed=True)

    assert rejected is False
    assert app._wake_noise_floor is None


def test_unconfirmed_buffer_retains_existing_adaptive_gate_behavior():
    app = _gate_app(floor=None)

    rejected = app._wake_audio_is_below_gate(0.0284, oww_confirmed=False)

    assert rejected is True
    assert app._wake_noise_floor == 0.0284


def test_unconfirmed_buffer_retains_existing_fixed_gate_behavior():
    app = _gate_app(adaptive=False, threshold=0.02)

    assert app._wake_audio_is_below_gate(0.01, oww_confirmed=False) is True
    assert app._wake_audio_is_below_gate(0.03, oww_confirmed=False) is False


def test_process_method_passes_oww_confirmation_to_gate():
    app = DictationApp.__new__(DictationApp)
    app.capture_rate = 16000
    app.model_rate = 16000
    app.config = {}
    app._vad_reset = Mock()
    seen = []

    def reject_at_gate(audio_rms, *, oww_confirmed=False):
        seen.append((audio_rms, oww_confirmed))
        return True

    app._wake_audio_is_below_gate = reject_at_gate

    app.process_wake_word_buffer(
        [np.full(160, 0.01, dtype=np.float32)],
        src_rate=16000,
        oww_confirmed=True,
    )

    assert seen[0][1] is True
    app._vad_reset.assert_called_once_with()
