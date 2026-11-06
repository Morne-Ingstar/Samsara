"""Wake startup status with fake loaders/audio; never import desktop bootstrap."""
import ast
from functools import lru_cache
import json
import logging
from pathlib import Path
import threading
import time
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import microphone_recovery
from samsara.ui import home_signals


ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=None)
def _method(name):
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "DictationApp")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == name)
    namespace = dict(
        logger=logging.getLogger(__name__), time=time, Path=Path,
        __file__=str(ROOT / "dictation.py"),
        config_defaults=SimpleNamespace(DEFAULTS={"wake_profiles": []}),
        wake_profiles=SimpleNamespace(refresh_primary_detector=lambda *a, **kw: None),
        WakeWordDetector=Mock(side_effect=AssertionError("No real wake models")),
    )
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 f"<wake-startup:{name}>", "exec"), namespace)
    return namespace[name]


def _app():
    app = SimpleNamespace(
        config={"wake_word_enabled": True, "microphone": 7,
                "wake_word_config": {"phrase": "jarvis", "audio": {
                    "measured_noise_floor": 0.01, "calibrated_microphone": 7}},
                "ava": {"provider_policy": "off"}},
        available_mics=[{"id": 7, "name": "Test microphone"}],
        snoozed=False, _no_microphone=False,
        _wake_models_lock=threading.Lock(), _wake_models_state="loading",
        _wake_start_pending=True, wake_ready=threading.Event(),
        _wake_consumer=SimpleNamespace(running=False), wake_word_active=False,
        _hands_free_fault=None, _wake_profile_detectors={},
        _load_oww_model=Mock(), _publish_wake_state=Mock(),
    )
    for name in ("wake_ready_state", "_wake_models_load_worker",
                 "_cancel_pending_wake_start", "_load_wake_profile_models"):
        setattr(app, name, MethodType(_method(name), app))
    app.start_wake_word_mode = Mock(side_effect=lambda: _activate(app))
    return app


def _activate(app):
    app._wake_consumer.running = True
    app.wake_word_active = True


def startup_observations(app):
    """Observe each publication and the points before capture/arming finish."""
    observations = []

    def observe(phase):
        state = home_signals.hands_free_state(app)
        observations.append({"phase": phase, "status": state.status,
                             "action": state.action, "pending": app._wake_start_pending})

    def activate():
        observe("activation entered")
        app._wake_consumer.running = True
        observe("capture running, detection not armed")
        app.wake_word_active = True

    app._publish_wake_state = lambda: observe("published")
    app.start_wake_word_mode = Mock(side_effect=activate)
    observe("models loading")
    app._wake_models_load_worker()
    observe("activation complete")
    return observations


def test_no_stopped_warning_between_models_ready_and_listener_armed():
    app = _app()
    samples = startup_observations(app)
    assert all(s["status"] in (home_signals.UNKNOWN, home_signals.READY)
               for s in samples), samples
    assert all(not s["action"] for s in samples), samples
    assert samples[-1]["status"] == home_signals.READY
    assert not app._wake_start_pending
    assert app.wake_ready.is_set()


def test_poll_overlapping_activation_completion_does_not_use_old_capture_state():
    app = _app()
    app._wake_models_state = "ready"

    class CompletingConsumer:
        @property
        def running(self):
            app.wake_word_active = True
            app._wake_start_pending = False
            return False  # Snapshot obtained immediately before completion.

    app._wake_consumer = CompletingConsumer()
    state = home_signals.hands_free_state(app)
    assert state.status == home_signals.UNKNOWN
    assert not state.action


@pytest.mark.parametrize("models,pending", [("off", False), ("loading", True)])
def test_initial_and_loading_states_have_no_restart_notice(models, pending):
    app = _app()
    app._wake_models_state, app._wake_start_pending = models, pending
    assert home_signals.hands_free_state(app).status == home_signals.UNKNOWN
    assert home_signals.problem_notice(app) is None


@pytest.mark.parametrize("model_file", ["", "__1159_missing_model__.onnx"])
def test_profile_without_model_uses_fallback_and_becomes_ready(model_file):
    app = _app()
    app.config["wake_profiles"] = [{"id": "hermes", "phrase": "hey hermes",
                                    "enabled": True, "oww_model": model_file}]
    app._wake_models_load_worker()
    assert app._wake_profile_detectors == {"hermes": None}
    assert home_signals.hands_free_state(app).status == home_signals.READY
    assert home_signals.problem_notice(app) is None


def test_model_load_exception_can_still_start_whisper_fallback():
    app = _app()
    app._load_oww_model.side_effect = RuntimeError("fake model unavailable")
    app._wake_models_load_worker()
    assert home_signals.hands_free_state(app).status == home_signals.READY
    assert app._hands_free_fault is None


def test_cancel_during_loading_does_not_activate_listener():
    app = _app()

    def cancel():
        app.config["wake_word_enabled"] = False
        app._cancel_pending_wake_start()

    app._load_oww_model.side_effect = cancel
    app._wake_models_load_worker()
    app.start_wake_word_mode.assert_not_called()
    assert home_signals.hands_free_state(app).status == home_signals.OFF
    assert not app._wake_start_pending


def test_cancel_after_models_ready_but_before_activation_is_respected():
    app = _app()
    app.wake_ready = SimpleNamespace(set=app._cancel_pending_wake_start)
    app._wake_models_load_worker()
    app.start_wake_word_mode.assert_not_called()
    assert not app._wake_start_pending


def test_activation_failure_ends_starting_state_and_preserves_restart_action():
    app = _app()
    app.start_wake_word_mode.side_effect = OSError("fake capture failure")
    with pytest.raises(OSError, match="fake capture failure"):
        app._wake_models_load_worker()
    assert not app._wake_start_pending
    assert home_signals.problem_notice(app).action == home_signals.ACTION_RESTART_HANDS_FREE


@pytest.mark.parametrize("models", ["loading", "ready"])
def test_real_fault_is_not_hidden_by_startup(models):
    app = _app()
    app._wake_models_state = models
    app._hands_free_fault = SimpleNamespace(reason="A real listener failure")
    assert home_signals.problem_notice(app).action == home_signals.ACTION_RESTART_HANDS_FREE


@pytest.mark.parametrize("stopped", ["consumer", "detection"])
def test_real_stop_after_activation_still_has_restart_action(stopped):
    app = _app()
    app._wake_models_load_worker()
    if stopped == "consumer":
        app._wake_consumer.running = False
    else:
        app.wake_word_active = False
    assert home_signals.problem_notice(app).action == home_signals.ACTION_RESTART_HANDS_FREE


def test_first_device_enumeration_and_queued_restart_do_not_spend_loading_budget():
    app = _app()
    app.loading_model = False
    app.restart_hands_free = Mock()
    changes = microphone_recovery.DeviceChanges(
        app, clock=lambda: 0.0, endpoints=lambda: frozenset({("test", "Test microphone")}),
        input_id=lambda: "test")
    changes.prime()
    changes._observe()
    assert not changes._input_pending
    changes.request_wake()
    for moment in (0.0, 5.0, 30.0):
        changes._restore_wake(moment)
    app.restart_hands_free.assert_not_called()
    assert changes._wake_attempts == 0
    assert app._hands_free_fault is None
    assert home_signals.problem_notice(app) is None

    def activate():
        # A device tick can overlap the models-ready / listener-start handoff.
        assert app._wake_models_state == "ready"
        changes._restore_wake(30.5)
        app.restart_hands_free.assert_not_called()
        assert changes._wake_attempts == 0
        _activate(app)

    app.start_wake_word_mode.side_effect = activate
    app._wake_models_load_worker()
    changes._restore_wake(31.0)
    assert not changes._wake_pending
    app.restart_hands_free.assert_not_called()


def test_unclean_exit_and_leftover_audio_recovery_do_not_create_wake_fault(
        tmp_path, monkeypatch):
    from samsara import audio_ducking
    from samsara.runtime.exit_reason import read_previous_exit

    marker = tmp_path / "last_exit.json"
    marker.write_text(json.dumps({"reason": "unknown"}), encoding="utf-8")
    assert read_previous_exit(marker) == "unknown"
    assert not marker.exists()
    journal = tmp_path / "duck-journal.json"
    journal.write_text(json.dumps({"ducked": [{"process_name": "test player",
        "original": 1.0, "applied": 0.3}]}), encoding="utf-8")
    monkeypatch.setattr(audio_ducking, "_journal_path", None)
    monkeypatch.setattr(audio_ducking, "_journal_pending", [])
    monkeypatch.setattr(audio_ducking, "_known_levels", {})
    spawn = Mock()
    monkeypatch.setattr(audio_ducking.thread_registry, "spawn", spawn)
    app = _app()
    assert audio_ducking.configure_duck_journal(journal, previous_unclean=True) == 1
    spawn.assert_called_once()
    assert app._hands_free_fault is None
    assert home_signals.problem_notice(app) is None
    samples = startup_observations(app)
    assert all(s["status"] != home_signals.UNAVAILABLE for s in samples), samples
