"""Voice feedback policy and migration; no live app, engine or dictation import."""
import ast
import copy
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from samsara import config_schema as schema
from samsara.tts.coordinator import (
    AudioCoordinator, AVA_SPEECH_CATEGORIES, MUTED_AVA_ID,
    SUPPRESSED_CMD_MODE_ID, SUPPRESSED_TTS_ID,
)
from samsara.tts.engine_base import SpeechHandle


def coordinator(level="full", *, enabled=True, captions=False, muted=False):
    app = MagicMock()
    app.command_mode_active = False
    app.config = {"tts": {"enabled": enabled, "feedback_level": level},
                  "accessibility": {"ava_captions": captions, "ava_mute_audio": muted}}
    engine = MagicMock()
    engine.get_engine_state.return_value = "idle"
    engine.is_speaking.return_value = False
    engine.speak.return_value = SpeechHandle(utterance_id="spoken")
    coord = AudioCoordinator(app, engine, config={})
    coord._emit_caption = MagicMock()
    return coord, app, engine


@pytest.mark.parametrize("category", ["agent_response", "confirmation", "unknown"])
def test_full_speaks(category):
    coord, app, engine = coordinator()
    assert coord.speak("Example", category=category).utterance_id == "spoken"
    engine.speak.assert_called_once()
    app.play_sound.assert_not_called()


@pytest.mark.parametrize("category,sound", [
    ("agent_response", "action_complete"), ("unknown", "action_complete"), ("error", "error")])
def test_brief_outcomes(category, sound, caplog):
    coord, app, engine = coordinator("brief")
    done = MagicMock()
    with caplog.at_level(logging.INFO):
        handle = coord.speak("Example", category=category, on_done=done)
    assert handle.utterance_id == "noop-brief-earcon"
    assert coord._last_spoken_handle is handle
    app.play_sound.assert_called_once_with(sound)
    engine.speak.assert_not_called()
    done.assert_not_called()  # Existing muted-Ava convention: caller handles noop.
    assert sum("level=brief" in record.message for record in caplog.records) == 1
    assert "Example" not in caplog.text


@pytest.mark.parametrize("category", ["confirmation", "readback", *sorted(AVA_SPEECH_CATEGORIES)])
def test_brief_questions_and_requested_speech(category):
    coord, app, engine = coordinator("brief")
    coord.speak("Example", category=category)
    engine.speak.assert_called_once()
    app.play_sound.assert_not_called()


@pytest.mark.parametrize("captions", [False, True])
@pytest.mark.parametrize("category", ["confirmation", "readback", *sorted(AVA_SPEECH_CATEGORIES), "agent_response", "error", "unknown"])
def test_silent_is_visible_without_duplicate_or_held_captions(captions, category):
    coord, app, engine = coordinator("silent", enabled=False, captions=captions)
    done = MagicMock()
    handle = coord.speak("Example", category=category, on_done=done)
    assert handle.utterance_id == "noop-silent"
    assert coord._last_spoken_handle is handle
    engine.speak.assert_not_called()
    app.play_sound.assert_not_called()
    done.assert_not_called()
    if category in AVA_SPEECH_CATEGORIES or category in ("confirmation", "readback"):
        coord._emit_caption.assert_called_once_with("Example", hold=False)
    else:
        coord._emit_caption.assert_not_called()


def test_command_limit_precedes_policy_and_ava_mute_still_applies():
    coord, app, engine = coordinator("brief", muted=True)
    app.command_mode_active = True
    app.config["command_mode"] = {"tts_char_limit": 5}
    assert coord.speak("Long outcome", category="agent_response").utterance_id == SUPPRESSED_CMD_MODE_ID
    assert coord.speak("Ava answer", category="ava_response").utterance_id == MUTED_AVA_ID
    app.play_sound.assert_not_called()
    engine.speak.assert_not_called()


def test_level_is_read_live_and_earcon_failure_does_not_break_dispatch():
    coord, app, engine = coordinator("full")
    coord.speak("Example", category="agent_response")
    app.config["tts"]["feedback_level"] = "brief"
    app.play_sound.side_effect = RuntimeError("unavailable")
    assert coord.speak("Example", category="agent_response").utterance_id == "noop-brief-earcon"
    app.config["tts"]["feedback_level"] = "silent"
    assert coord.speak("Example", category="confirmation").utterance_id == "noop-silent"
    engine.speak.assert_called_once()


@pytest.mark.parametrize("level", [None, "junk", []])
@pytest.mark.parametrize("enabled", [False, True])
def test_missing_or_invalid_level_keeps_legacy_path(level, enabled):
    coord, app, engine = coordinator(level, enabled=enabled)
    if level is None:
        del app.config["tts"]["feedback_level"]
    handle = coord.speak("Example", category="agent_response")
    assert handle.utterance_id == ("spoken" if enabled else SUPPRESSED_TTS_ID)
    assert engine.speak.call_count == int(enabled)
    app.play_sound.assert_not_called()


@pytest.mark.parametrize("config,level", [
    ({"tts": {"enabled": False}}, "silent"),
    ({"tts": {"enabled": True}, "feedback": {"spoken_notices": "everything"}}, "full"),
    ({"tts": {"enabled": True}, "feedback": {"spoken_notices": "questions"}}, "brief"),
    ({"tts": {"enabled": True}}, "brief"),
    ({}, "silent"), ({"tts": {}}, "silent"),
    ({"tts": {"enabled": 1}}, "silent"),
    ({"tts": {"enabled": True, "feedback_level": "junk"}}, "brief"),
    ({"tts": {"enabled": True, "feedback_level": []}}, "brief"),
    ({"tts": {"enabled": True, "feedback_level": "silent"}}, "silent"),
    ({"tts": {"enabled": False, "feedback_level": "full"}}, "full"),
    ({"tts": {"enabled": False, "feedback_level": "brief"}}, "brief"),
])
def test_migration_is_idempotent_and_preserves_unrelated_values(config, level):
    config = copy.deepcopy(config)
    config.setdefault("tts", {})["engine"] = "edge"
    config.setdefault("feedback", {})["other"] = 42
    assert schema.migrate_removed_settings(config) is True
    assert config["tts"]["feedback_level"] == level
    assert config["tts"]["enabled"] is (level != "silent")
    assert config["tts"]["engine"] == "edge"
    assert config["feedback"] == {"other": 42}
    before = copy.deepcopy(config)
    assert schema.migrate_removed_settings(config) is False
    assert config == before


@pytest.mark.parametrize("level", ["silent", "brief", "full"])
def test_apply_feedback_level_is_the_shared_writer(level):
    config = {"tts": {"engine": "edge"}, "other": 3}
    assert schema.apply_feedback_level(config, level) is None
    assert config == {"tts": {"engine": "edge", "feedback_level": level,
                              "enabled": level != "silent"}, "other": 3}


@pytest.mark.parametrize("level", ["invalid", None, []])
def test_invalid_level_is_rejected_without_mutation(level):
    config = {"tts": {"enabled": False}, "other": [1]}
    before = copy.deepcopy(config)
    with pytest.raises(ValueError):
        schema.apply_feedback_level(config, level)
    assert config == before


@pytest.mark.parametrize("level,expected", [("full", "everything"), ("brief", "questions"),
                                          ("silent", "questions"), (None, "questions"), ("junk", "questions")])
def test_session_notices_derive_from_level(level, expected):
    assert schema.spoken_notices_for_level(level) == expected


@pytest.mark.parametrize("chronic_pain", [False, True])
def test_fresh_wizard_stays_brief_after_load(chronic_pain):
    from samsara.ui.first_run_wizard_qt import _DEFAULTS, _USE_CASE_CONFIGS
    config = copy.deepcopy(_DEFAULTS)
    if chronic_pain:
        for key, value in _USE_CASE_CONFIGS["chronic_pain"].items():
            if isinstance(value, dict):
                config.setdefault(key, {}).update(value)
            else:
                config[key] = value
    schema.migrate_removed_settings(config)
    assert config["tts"]["feedback_level"] == "brief"
    assert config["tts"]["enabled"] is True


def test_schema_defaults_and_removed_key():
    assert schema.FEEDBACK_LEVELS == ("silent", "brief", "full")
    assert schema.FEEDBACK_LEVEL_FRESH_DEFAULT == "brief"
    assert tuple(schema.FEEDBACK_LEVEL_CHOICES) == schema.FEEDBACK_LEVELS
    assert schema.SETTINGS_SCHEMA["tts.feedback_level"]["default"] == "brief"
    assert schema.SETTINGS_SCHEMA["tts.enabled"]["default"] is True
    assert schema.SETTINGS_SCHEMA["tts.engine"]["default"] == "winrt"
    assert "feedback.spoken_notices" not in schema.SETTINGS_SCHEMA


def test_settings_combo_mirror_help_and_save(qapp):
    from tests.test_settings_pages import _StubApp
    from samsara.ui.settings_qt import _SettingsWindow
    app = _StubApp({"tts": {"feedback_level": "brief", "enabled": True}})
    window = _SettingsWindow(app)
    try:
        window.show_tab("Audio")
        qapp.processEvents()
        combo = window._widgets["tts_feedback_level"]
        mirror = window._widgets["tts_enabled"]
        assert [combo.itemData(i) for i in range(combo.count())] == ["silent", "brief", "full"]
        assert [combo.itemText(i) for i in range(combo.count())] == ["Silent", "Brief", "Full"]
        assert combo.currentData() == "brief"
        assert not mirror.isVisible()
        assert window._widgets["tts_engine"].currentText() == "winrt"
        assert "spoken_notices_combo" not in window._widgets
        assert window._rows_by_widget["tts_feedback_level"].detail.text() == schema.FEEDBACK_LEVEL_CHOICES["brief"][1]
        for level in schema.FEEDBACK_LEVELS:
            combo.setCurrentIndex(combo.findData(level))
            assert mirror.isChecked() is (level != "silent")
            assert window._rows_by_widget["tts_engine"].property("conditionVisible") is (level != "silent")
            assert window._rows_by_widget["tts_feedback_level"].detail.text() == schema.FEEDBACK_LEVEL_CHOICES[level][1]
            saved = window._source_saves["tts"][0]({})["tts"]
            assert saved["feedback_level"] == level
            assert saved["enabled"] is (level != "silent")
    finally:
        window.hide()
        window.deleteLater()


def test_saved_engine_survives_settings(qapp):
    from tests.test_settings_pages import _StubApp
    from samsara.ui.settings_qt import _SettingsWindow
    window = _SettingsWindow(_StubApp({"tts": {"engine": "edge", "feedback_level": "full", "enabled": True}}))
    try:
        assert window._widgets["tts_engine"].currentText() == "edge"
        assert window._source_saves["tts"][0]({})["tts"]["engine"] == "edge"
    finally:
        window.deleteLater()


def test_thin_runtime_setter_persists_and_updates_manager_without_importing_dictation():
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "set_feedback_level")
    namespace = {"logger": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    app = SimpleNamespace(config={}, _config_lock=threading.Lock(),
                          save_config=MagicMock(), _session_mode_manager=MagicMock())
    assert namespace["set_feedback_level"](app, "full") == "full"
    assert app.config["tts"] == {"enabled": True, "feedback_level": "full"}
    app._session_mode_manager.set_spoken_notices.assert_called_once_with("everything")
    app.save_config.assert_called_once()


# FINISH IT: exercise app seams with fake engines and AST-extracted methods.
class _FinishEngine:
    def __init__(self):
        self.speak = MagicMock(return_value=SpeechHandle(utterance_id="finish-spoken"))
        self.cancel_all = MagicMock()
        self.shutdown = MagicMock()
        self.set_volume = MagicMock()

    def get_engine_state(self):
        return "idle"

    def is_speaking(self):
        return False


@pytest.fixture(scope="session", autouse=True)
def _gate_uses_fake_engine_factories():
    """Keep legacy Settings Apply tests from constructing audio/network engines.

    The required gate starts with this file. Its session fixture protects all
    later Apply tests too; individual revocation tests still override these
    exported factories with their own recording engines. Real runtime/config
    logic is exercised unchanged, with only external engine constructors faked.
    """
    import samsara.tts as tts
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tts, "WinRTEngine", lambda **kwargs: _FinishEngine())
        patch.setattr(tts, "EdgeTTSEngine", lambda **kwargs: _FinishEngine())
        yield


@pytest.fixture(autouse=True)
def fake_engine_constructors(monkeypatch):
    """Even the existing AST setter test must never construct a real engine."""
    import samsara.tts as tts
    factories = SimpleNamespace(local=MagicMock(side_effect=lambda **kw: _FinishEngine()),
                                network=MagicMock(side_effect=lambda **kw: _FinishEngine()))
    monkeypatch.setattr(tts, "WinRTEngine", factories.local)
    monkeypatch.setattr(tts, "EdgeTTSEngine", factories.network)
    return factories


def _app_methods(*names, timing=None):
    import time
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    selected = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in selected} == set(names)
    namespace = {"logger": logging.getLogger(__name__), "time": time, "threading": threading,
                 "current_stage_timing": lambda: timing}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def _finish_app(level="silent", *, tts=None):
    config = {"tts": tts if tts is not None else {
        "feedback_level": level, "enabled": level != "silent", "engine": "winrt"}}
    ready = threading.Event()
    ready.set()
    return SimpleNamespace(config=config, _config_lock=threading.Lock(),
                           tts_engine=None, audio_coordinator=None, tts_ready=threading.Event(),
                           _startup_shell_ready=ready, _resolve_playback_output=lambda: None,
                           command_mode_active=False, play_sound=MagicMock(), save_config=MagicMock(),
                           _session_mode_manager=MagicMock(),
                           _maybe_announce_ava_command_session_migration=MagicMock())


def _start(app):
    _app_methods("_tts_init_worker")["_tts_init_worker"](app)
    return app.audio_coordinator


@pytest.mark.parametrize("tts", [{"enabled": False}, {"enabled": False, "feedback_level": "silent"}])
def test_finish_startup_off_installs_router_without_building_engine(tts, fake_engine_constructors):
    app = _finish_app(tts=tts)
    coord = _start(app)
    assert isinstance(coord, AudioCoordinator)
    assert coord.engine is None and app.tts_engine is None
    assert app.tts_ready.is_set()
    app._maybe_announce_ava_command_session_migration.assert_called_once()
    fake_engine_constructors.local.assert_not_called()
    fake_engine_constructors.network.assert_not_called()


def test_finish_boot_schedules_worker_even_when_silent():
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    start = next(i for i, n in enumerate(init.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Attribute) and t.attr == "tts_engine" for t in n.targets))
    end = next(i for i in range(start, len(init.body)) if isinstance(init.body[i], ast.Expr)
               and isinstance(init.body[i].value, ast.Call)
               and isinstance(init.body[i].value.func, ast.Name) and init.body[i].value.func.id == "_boot")
    app = _finish_app()
    app._tts_init_worker = MagicMock()
    spawn = MagicMock()
    namespace = {"self": app, "threading": threading, "thread_registry": SimpleNamespace(spawn=spawn)}
    exec(compile(ast.Module(body=init.body[start:end], type_ignores=[]), str(source), "exec"), namespace)
    spawn.assert_called_once_with("dictation.tts_init", app._tts_init_worker, daemon=True)
    app._maybe_announce_ava_command_session_migration.assert_not_called()


def _assert_session_question(app):
    timing = MagicMock()
    app.audio_coordinator._emit_caption = MagicMock()
    _app_methods("_speak_session_notice", timing=timing)["_speak_session_notice"](
        app, "Clear this draft?", "confirmation")
    app.audio_coordinator._emit_caption.assert_called_once_with("Clear this draft?", hold=False)
    timing.feedback_started.assert_called_once()
    timing.feedback_finished.assert_called_once()
    app.play_sound.assert_not_called()


def test_finish_silent_session_question_reaches_caption_and_finishes_timing():
    app = _finish_app()
    _start(app)
    _assert_session_question(app)


def test_finish_apply_both_directions_and_revokes_under_both_locks(fake_engine_constructors):
    from tests.test_tts_revoke_328 import _Settings
    app = _finish_app("brief")
    old = _start(app)
    engine = app.tts_engine
    checks = []

    def revoked():
        checks.append((app._config_lock.locked(), old._dispatch_lock._is_owned()))

    engine.cancel_all.side_effect = revoked
    engine.shutdown.side_effect = revoked
    _Settings(app, {"enabled": False, "feedback_level": "silent", "engine": "winrt"})._apply_and_close()
    assert checks and all(a and b for a, b in checks)
    engine.cancel_all.assert_called()
    engine.shutdown.assert_called_once()
    assert app.tts_engine is None and app.audio_coordinator.engine is None
    _assert_session_question(app)
    _Settings(app, {"enabled": True, "feedback_level": "brief", "engine": "winrt"})._apply_and_close()
    assert app.tts_engine is not None and app.tts_engine is not engine
    assert fake_engine_constructors.local.call_count == 2
    app.audio_coordinator.speak("Continue?", category="confirmation")
    app.tts_engine.speak.assert_called_once()
    # A retained router cannot revive its retired engine after re-enabling.
    assert old.speak("Old path", category="confirmation").utterance_id == SUPPRESSED_TTS_ID
    engine.speak.assert_not_called()
    app.audio_coordinator.shutdown()


def test_finish_setter_rebuilds_only_across_silent_boundary(fake_engine_constructors):
    app = _finish_app()
    _start(app)
    setter = _app_methods("set_feedback_level")["set_feedback_level"]
    assert setter(app, "brief") == "brief"
    engine, coord = app.tts_engine, app.audio_coordinator
    assert engine is not None
    assert setter(app, "full") == "full"
    assert setter(app, "brief") == "brief"
    assert app.tts_engine is engine and app.audio_coordinator is coord
    assert setter(app, "full") == "full"
    assert setter(app, "silent") == "silent"
    assert app.tts_engine is None and app.audio_coordinator.engine is None
    fake_engine_constructors.local.assert_called_once()
    engine.cancel_all.assert_called()
    engine.shutdown.assert_called_once()
    assert [call.args[0] for call in app._session_mode_manager.set_spoken_notices.call_args_list] == [
        "questions", "everything", "questions", "everything", "questions"]
    assert app.save_config.call_count == 5
    _assert_session_question(app)


@pytest.mark.parametrize("method,args", [("on_earcon_starting", ("action_complete",)),
                                       ("shutdown", ()), ("_schedule_interrupt_grace", ()),
                                       ("_on_user_speech_interrupt", ()), ("_on_duck_restore", ()),
                                       ("cancel_speech", ())])
def test_finish_engine_less_methods_are_safe(method, args, monkeypatch):
    from samsara.runtime import thread_registry
    app = _finish_app()
    coord = AudioCoordinator(app, None)
    spawn = MagicMock()
    monkeypatch.setattr(thread_registry, "spawn", spawn)
    # A stale timer/interrupt path must also tolerate a retained handle.
    coord._active_handle = SpeechHandle(utterance_id="old")
    coord._state = "SPEAKING"
    coord._interrupt_poll_active = True
    assert getattr(coord, method)(*args) is None
    spawn.assert_not_called()


def test_finish_engine_failure_keeps_router_and_logs_once(fake_engine_constructors, caplog):
    app = _finish_app("brief")
    fake_engine_constructors.local.side_effect = RuntimeError("test engine unavailable")
    with caplog.at_level(logging.ERROR):
        _start(app)
    assert app.tts_ready.is_set()
    assert isinstance(app.audio_coordinator, AudioCoordinator)
    assert app.audio_coordinator.engine is None and app.tts_engine is None
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
    app._maybe_announce_ava_command_session_migration.assert_called_once()
    schema.apply_feedback_level(app.config, "silent")
    _assert_session_question(app)


@pytest.mark.parametrize("handle_id", ["noop-silent", "noop-brief-earcon", "noop-ava-muted", None])
def test_finish_claude_reply_completes_skipped_speech_once(handle_id):
    from samsara.agent_channel import claude_reply
    app = SimpleNamespace(audio_coordinator=None)
    if handle_id:
        app.audio_coordinator = SimpleNamespace(speak=MagicMock(return_value=SpeechHandle(utterance_id=handle_id)))
    token = object()
    state = claude_reply._state(app)
    state["token"] = token
    done = MagicMock()
    claude_reply._speak(app, "Example", token, on_done=done)
    assert state["speaking"] is False
    done.assert_called_once()


def test_finish_test_voice_reports_disabled_engine(qapp):
    from tests.test_settings_pages import _StubApp
    from samsara.ui.settings_qt import _SettingsWindow
    app = _StubApp({"tts": {"feedback_level": "silent", "enabled": False}})
    app.audio_coordinator = AudioCoordinator(app, None)
    app.audio_coordinator.speak = MagicMock()
    window = _SettingsWindow(app)
    try:
        window._test_tts()
        assert "TTS is off" in window._widgets["tts_test_status"].text()
        app.audio_coordinator.speak.assert_not_called()
    finally:
        window.deleteLater()


@pytest.mark.parametrize("handle_id", ["noop-silent", "noop-brief-earcon", "noop-tts-disabled"])
def test_finish_ava_helper_does_not_report_skipped_speech_as_spoken(handle_id):
    """Extract the existing helper, keeping the regression free of model imports."""
    source = Path(__file__).resolve().parents[1] / "plugins/commands/ask_ollama.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "speak")
    record = MagicMock()
    namespace = {"logger": logging.getLogger(__name__), "_strip_tags": lambda text: text,
                 "get_max_response_length": lambda app: 0, "_record_speech": record,
                 "execution_policy": SimpleNamespace(current_generation=lambda app: 1),
                 "_turn_local": SimpleNamespace(spoken_text=[])}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    app = SimpleNamespace(audio_coordinator=SimpleNamespace(
        speak=MagicMock(return_value=SpeechHandle(utterance_id=handle_id))), tts_engine=_FinishEngine())
    assert namespace["speak"](app, "Example") is False
    assert record.call_args.args[0] is False
    assert namespace["_turn_local"].spoken_text == []
    app.tts_engine.speak.assert_not_called()


def test_finish_requested_voice_sample_speaks_in_brief(qapp):
    from tests.test_settings_pages import _StubApp
    from samsara.ui.settings_qt import _SettingsWindow
    app = _StubApp({"tts": {"feedback_level": "brief", "enabled": True}})
    engine = _FinishEngine()
    app.audio_coordinator = AudioCoordinator(app, engine)
    window = _SettingsWindow(app)
    try:
        window._test_tts()
        engine.speak.assert_called_once()
        assert engine.speak.call_args.kwargs["category"] == "readback"
        assert "Speaking:" in window._widgets["tts_test_status"].text()
    finally:
        app.audio_coordinator.shutdown()
        window.deleteLater()
