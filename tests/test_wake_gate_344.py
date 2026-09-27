"""A profile's transcript can use only the OWW hit from its own capture."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine.wake_consumer import AdmittedWakeBuffer, WakeConsumer
from samsara.wake_profiles import profile_admitted
from samsara import wake_profiles
from samsara.wake_word_matcher import match_wake_phrase


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))


def _consumer(*, primary=False, profile_hits=(), detectors=None, profiles=None,
              session_open=False):
    app = SimpleNamespace(
        app_state="wake_session" if session_open else "asleep",
        wake_word_triggered=False,
        _wake_detector=SimpleNamespace(is_available=True, reset=Mock()),
        _wake_profile_detectors=detectors or {},
        _oww_primary_hit=primary,
        _oww_profile_hits=set(profile_hits),
        _oww_wake_detected=bool(primary or profile_hits),
        _wake_rearm_needs_onset=False,
        config={"wake_profiles": profiles or []},
        process_wake_word_buffer=Mock(),
        _close_hands_free_capture_duck=Mock(),
        _open_hands_free_capture_duck=Mock(return_value=7),
    )
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._hands_free_capture_duck_token = None
    consumer._is_toggle_cmd = lambda _app: False
    return consumer, app


def _flush(consumer):
    consumer._flush([np.zeros(160, dtype=np.float32)], owner_token=5)


def test_modelless_profile_cannot_bypass_generic_gate():
    profile = {"id": "claude", "phrase": "okay claude", "enabled": True}
    consumer, app = _consumer(profiles=[profile], detectors={"claude": None})
    _flush(consumer)
    app.process_wake_word_buffer.assert_not_called()
    app._close_hands_free_capture_duck.assert_called_once_with(5)


def test_modelless_profile_uses_generic_hit_only():
    profile = {"id": "claude", "phrase": "okay claude", "enabled": True}
    consumer, app = _consumer(primary=True, profiles=[profile], detectors={"claude": None})
    _flush(consumer)
    buffer = app.process_wake_word_buffer.call_args.args[0]
    assert isinstance(buffer, AdmittedWakeBuffer)
    assert buffer.primary_hit is True and buffer.profile_hits == frozenset()
    assert profile_admitted(profile, app._wake_profile_detectors,
                            primary_hit=buffer.primary_hit, profile_hits=buffer.profile_hits)
    assert app._oww_profile_hits == set() and app._oww_primary_hit is False


def test_model_backed_profile_needs_its_own_hit():
    profile = {"id": "claude", "phrase": "hey claude", "enabled": True}
    detector = SimpleNamespace(is_available=True)
    consumer, app = _consumer(profile_hits={"claude"}, profiles=[profile],
                              detectors={"claude": detector})
    _flush(consumer)
    buffer = app.process_wake_word_buffer.call_args.args[0]
    assert buffer.primary_hit is False and buffer.profile_hits == frozenset({"claude"})
    assert profile_admitted(profile, app._wake_profile_detectors,
                            primary_hit=buffer.primary_hit, profile_hits=buffer.profile_hits)
    assert not profile_admitted(profile, app._wake_profile_detectors, primary_hit=True)


def test_another_profile_hit_cannot_authorize_claude():
    claude = {"id": "claude", "phrase": "hey claude", "enabled": True}
    hermes = {"id": "hermes", "phrase": "activate hermes", "enabled": True}
    detectors = {"claude": SimpleNamespace(is_available=True),
                 "hermes": SimpleNamespace(is_available=True)}
    consumer, app = _consumer(profile_hits={"hermes"}, profiles=[claude, hermes],
                              detectors=detectors)
    _flush(consumer)
    buffer = app.process_wake_word_buffer.call_args.args[0]
    assert not profile_admitted(claude, detectors, profile_hits=buffer.profile_hits)
    assert profile_admitted(hermes, detectors, profile_hits=buffer.profile_hits)


def test_failed_short_model_never_uses_generic_hit_or_session():
    short = {"id": "claude", "phrase": "hey claude", "enabled": True}
    assert not profile_admitted(short, {"claude": None}, primary_hit=True)
    assert not profile_admitted(short, {"claude": None}, session_open=True)


def test_hit_from_previous_capture_window_is_rejected():
    profile = {"id": "claude", "phrase": "hey claude", "enabled": True}
    detector = SimpleNamespace(is_available=True)
    consumer, app = _consumer(profile_hits={"claude"}, profiles=[profile],
                              detectors={"claude": detector})
    app._oww_profile_hit_ns = {"claude": 1_000_000_000}
    consumer._flush([np.zeros(160, dtype=np.float32)], owner_token=5,
                    speech_start_ns=10_000_000_000, speech_end_ns=11_000_000_000)
    app.process_wake_word_buffer.assert_not_called()
    app._close_hands_free_capture_duck.assert_called_once_with(5)


def test_actual_dictation_profile_match_seam_without_importing_app():
    """Execute only this method's AST: importing dictation attaches a live log handler."""
    tree = ast.parse((Path(__file__).parents[1] / "dictation.py").read_text(encoding="utf-8"))
    app_class = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "DictationApp")
    method = next(node for node in app_class.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_check_wake_profiles")
    namespace = {"wake_profiles": wake_profiles, "match_wake_phrase": match_wake_phrase}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "dictation.py", "exec"), namespace)
    check = namespace["_check_wake_profiles"]
    claude = {"id": "claude", "phrase": "hey claude", "enabled": True}
    hermes = {"id": "hermes", "phrase": "activate hermes", "enabled": True}
    app = SimpleNamespace(config={"wake_profiles": [claude, hermes]},
                          _wake_profile_detectors={"claude": SimpleNamespace(is_available=True),
                                                   "hermes": None})
    assert check(app, "hey claude", profile_hits={"claude"}) is claude
    assert check(app, "hey claude", profile_hits={"hermes"}) is None
    assert check(app, "hey claude", primary_hit=True) is None
    app._wake_profile_detectors["claude"] = None
    assert check(app, "hey claude", primary_hit=True) is None  # failed short model
    assert check(app, "activate hermes", primary_hit=True) is hermes
