"""wake_word_config.opens_session: a wake-word hit opens the latched
hands-free session (the toggle tap's entry) instead of the one-command wake
window. With the flag absent or false, the wake path is unchanged -- proven
by recording every app interaction of the REAL _decode_wake_word_buffer."""
from types import MethodType, SimpleNamespace

import numpy as np
import pytest

import dictation
from samsara.audio_engine.wake_dispatch import TranscriptionOwners


class _Recorder:
    """Attribute-style stub that logs each call as (name, args)."""

    def __init__(self, log, name, result=None):
        self._log, self._name, self._result = log, name, result

    def __call__(self, *args, **kwargs):
        self._log.append((self._name, args, tuple(sorted(kwargs.items()))))
        return self._result(*args) if callable(self._result) else self._result


def _app(config, transcript, command_mode_active=False):
    log = []
    app = SimpleNamespace(
        config=config, app_state="asleep", capture_rate=16000, model_rate=16000,
        _transcription_owners=TranscriptionOwners(), _vad_available=True,
        wake_word_triggered=False, command_mode_active=command_mode_active,
        model_lock=__import__("threading").Lock(),
        model=SimpleNamespace(transcribe=lambda audio, **kw: (
            [SimpleNamespace(text=transcript)], SimpleNamespace(language="en"))),
        voice_training_window=SimpleNamespace(apply_corrections=lambda t: t),
    )
    for name, result in {
        "_wake_audio_is_below_gate": False, "get_transcription_params": {},
        "_filter_dictation_language": lambda text, info: text,
        "_emit_wake_trace": None, "_log_history": None, "_check_wake_profiles": None,
        "_confirm_wake_capture": None, "play_sound": None, "_process_wake_command": None,
        "_start_wake_timeout": None, "_vad_reset": None, "_schedule_ui": None,
        "_dispatch_session_transition": None, "_try_cancel_pending_wake_command": False,
    }.items():
        setattr(app, name, _Recorder(log, name, result))
    app.enter_command_mode = "enter_command_mode"       # identity marker for the transition
    for name in ("_decode_wake_word_buffer", "_wake_opens_session", "_open_session_from_wake"):
        setattr(app, name, MethodType(getattr(dictation.DictationApp, name), app))
    return app, log


def _run(config, transcript="jarvis open chrome", **kw):
    app, log = _app(config, transcript, **kw)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(dictation, "resample_audio", lambda audio, *a: audio)
    try:
        app._decode_wake_word_buffer([np.full(1600, .2, dtype=np.float32)], src_rate=16000)
    finally:
        monkey.undo()
    return app, [entry for entry in log if entry[0] != "_emit_wake_trace"], log


BASE = {"wake_word_config": {"phrase": "jarvis"}, "command_mode": {"mode": "toggle"}}


def _with(**ww):
    return {"wake_word_config": {"phrase": "jarvis", **ww}, "command_mode": {"mode": "toggle"}}


class TestFlagOffIsUnchanged:
    @pytest.mark.parametrize("transcript", ["jarvis open chrome", "jarvis", "nothing to see here"])
    def test_false_and_absent_produce_identical_interactions(self, transcript):
        _a1, _c1, absent = _run(BASE, transcript)
        _a2, _c2, false = _run(_with(opens_session=False), transcript)
        assert absent == false

    def test_legacy_one_command_window(self):
        app, calls, _ = _run(_with(opens_session=False))
        names = [c[0] for c in calls]
        assert ("_process_wake_command", ("open chrome",), ()) in calls
        assert ("play_sound", ("start",), ()) in calls
        assert "_dispatch_session_transition" not in names
        assert app.wake_word_triggered is True

    def test_flag_is_ignored_without_a_toggle_session(self, caplog):
        hold = {"wake_word_config": {"phrase": "jarvis", "opens_session": True},
                "command_mode": {"mode": "hold"}}
        legacy = {"wake_word_config": {"phrase": "jarvis"}, "command_mode": {"mode": "hold"}}
        _a, _c, flagged = _run(hold)
        _b, _d, plain = _run(legacy)
        assert flagged == plain
        assert any("opens_session" in r.message for r in caplog.records)


class TestFlagOnOpensTheLatchedSession:
    def test_wake_hit_enters_through_the_toggle_entry(self):
        app, calls, _ = _run(_with(opens_session=True))
        assert ("_dispatch_session_transition", ("enter_command_mode",), ()) in calls
        names = [c[0] for c in calls]
        assert "_process_wake_command" not in names        # no one-command window
        assert "play_sound" not in names                    # the session entry plays its own earcon
        assert "_start_wake_timeout" not in names
        assert app.wake_word_triggered is False
        assert app.app_state == "asleep"

    def test_bare_wake_phrase_also_opens(self):
        _app_, calls, _ = _run(_with(opens_session=True), transcript="Jarvis.")
        assert ("_dispatch_session_transition", ("enter_command_mode",), ()) in calls

    def test_already_open_session_is_left_alone(self):
        _app_, calls, _ = _run(_with(opens_session=True), command_mode_active=True)
        assert "_dispatch_session_transition" not in [c[0] for c in calls]

    def test_non_wake_speech_changes_nothing(self):
        _a, _c, flagged = _run(_with(opens_session=True), "nothing to see here")
        _b, _d, plain = _run(BASE, "nothing to see here")
        assert flagged == plain


def test_config_schema_registers_the_new_keys():
    from samsara.config_defaults import DEFAULTS
    assert DEFAULTS["wake_word_config.opens_session"] is False
    assert DEFAULTS["command_mode.abort_phrases"] == []


def test_session_manager_receives_configured_abort_phrases(monkeypatch):
    captured = {}

    class _Manager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(dictation, "SessionModeManager", _Manager)
    app = SimpleNamespace(
        _session_mode_manager=None, config={"command_mode": {"abort_phrases": "that's all"},
                                            "wake_word_config": {}},
        command_executor=None, _apply_formatting_tokens=lambda t: t,
        _ava_session_agent_dispatch_fn=None, _dictate_commit_redecode=None,
        _pop_pending_action_for_scratch=None,
    )
    dictation.DictationApp._ensure_session_mode_manager(app)
    assert captured["extra_sleep_phrases"] == ["that's all"]
    assert "go to sleep" in captured["abort_phrases"]
