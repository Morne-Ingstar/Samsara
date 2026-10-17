"""608: real help/miss routes with synthetic controls, providers and effects.

DictationApp is supplied only by the approved no-bootstrap gate adapter.
No live app, profile, provider, input event or control enumeration is used.
"""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama, core_utils
from samsara import ai_preferences, ava_command_session as ava, ava_readiness
from samsara import commands, execution_policy as ep, handlers, uia
from samsara.command_registry import DispatchState as State
from samsara.command_scope import MatchContext
from samsara.session_modes import DispatchOutcome, outcome_chip
from samsara.ui import command_cheatsheet_qt as sheet, qt_runtime
from tools.dump_command_metadata import build_executor

LIVE_LOAD_PLUGINS = commands._plugin_commands.load_plugins


@pytest.fixture(autouse=True)
def real_registry(monkeypatch, _isolate_plugin_registry):
    # Opt into the production inert loader after conftest isolates its state.
    monkeypatch.setattr(commands._plugin_commands, "load_plugins", LIVE_LOAD_PLUGINS)


def node(name="Save", **kw):
    return replace(uia.Node(10, 1, 0, name, "ButtonControl", (0, 0, 80, 40),
                            True, False, False, 1), **kw)


@pytest.fixture
def help_env(monkeypatch):
    foreground = [10]
    controls = [node(), node("New tab"), node("Close tab")]
    service = SimpleNamespace(snapshot=Mock(side_effect=lambda **_kw:
                              uia.Snapshot(10, 1, tuple(controls), ())))
    monkeypatch.setattr(uia, "get_service", lambda: service)
    monkeypatch.setattr(handlers, "_get_foreground_hwnd", lambda: foreground[0])
    monkeypatch.setattr(handlers, "_get_foreground_exe_lower", lambda: "chrome.exe")
    posted = []
    monkeypatch.setattr(qt_runtime, "post", posted.append)
    app = SimpleNamespace(cheat_sheet=Mock(), audio_coordinator=Mock())
    return SimpleNamespace(app=app, controls=controls, service=service,
                           foreground=foreground, posted=posted)


@pytest.mark.parametrize("phrase,query", [("help tab", "tab"), ("help search tab", "tab"),
                                        ("what can I say", ""), ("help", "")])
def test_registered_help_consumes_topic_and_keeps_app_scope(help_env, monkeypatch, phrase, query):
    monkeypatch.setattr(commands._command_scope, "capture_context",
                        lambda: MatchContext.for_app("chrome.exe"))
    executor = build_executor()
    app = help_env.app
    app.config = {}
    app.command_matching_enabled = True
    app.command_mode_active = False
    result = executor.process_text(phrase, app, force_commands=True)
    assert result.state is State.COMPLETED
    assert help_env.service.snapshot.call_args.kwargs == {"refresh": True}
    app.cheat_sheet.show_scoped.assert_not_called()  # UI work is posted.
    help_env.posted[-1]()
    call = app.cheat_sheet.show_scoped.call_args
    assert "browsers" in call.args[0]
    assert "word" not in call.args[0]
    assert call.kwargs["query"] == query
    assert "click New tab" in call.kwargs["live_text"]
    assert ("click Save" in call.kwargs["live_text"]) is (not query)


def test_controls_are_visible_enabled_named_unique_and_bounded(help_env):
    help_env.controls[:] = [node("Hidden", offscreen=True), node("Disabled", enabled=False),
                           node("Static", control_type="TextControl"), node("Other", hwnd=20),
                           node("Zero", rect=(0, 0, 0, 0)), node(""), node("x" * 61),
                           node("Save"), node("SAVE"), *[node(f"Button {i}") for i in range(9)]]
    text = core_utils._clickable_help(10, "")
    assert text.count("click ") == 6
    assert "click Save" in text and "click SAVE" not in text
    assert not any(word in text for word in ("Hidden", "Disabled", "Static", "Other", "Zero"))
    assert "click Button 5" not in text


@pytest.mark.parametrize("failure", ["unavailable", "changed", "wrong_snapshot", "empty"])
def test_failed_or_changed_discovery_preserves_catalogue_help(help_env, failure):
    if failure == "unavailable":
        help_env.service.snapshot.side_effect = RuntimeError("fixture provider unavailable")
    elif failure == "changed":
        help_env.foreground[0] = 20
    elif failure == "wrong_snapshot":
        help_env.service.snapshot.side_effect = lambda **_kw: uia.Snapshot(20, 1, (), ())
    else:
        help_env.controls.clear()
    assert core_utils.what_can_i_say(help_env.app, "tab") is True
    help_env.posted[-1]()
    call = help_env.app.cheat_sheet.show_scoped.call_args
    assert call.kwargs["query"] == "tab"
    assert "click " not in call.kwargs["live_text"]
    assert "show numbers" in call.kwargs["live_text"]


def test_new_help_request_refreshes_controls(help_env):
    assert "click Save" in core_utils._clickable_help(10, "")
    help_env.controls[:] = [node("Print")]
    text = core_utils._clickable_help(10, "")
    assert "click Print" in text and "click Save" not in text
    assert help_env.service.snapshot.call_count == 2


def test_topic_and_plain_control_text_reach_real_help_view(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(sheet, "_scope_context", lambda: MatchContext.for_app("chrome.exe"))
    executor = build_executor()
    window = sheet._CheatSheetWindow(lambda _p: None, executor._matcher.list_commands,
                                     tmp_path / "palette.json",
                                     initial_scope={"browsers", "core"},
                                     initial_view=("All", "tab", "Controls found here: click <Save>."))
    try:
        window.show()
        qapp.processEvents()
        assert window._filter.text() == "tab"
        assert window._live_controls.textFormat().name == "PlainText"
        assert "<Save>" in window._live_controls.text()
        assert window._list.count() > 0
        assert all("tab" in window._list.item(i).text().casefold()
                   for i in range(window._list.count()))
        window.set_view({"core"}, ("All", "", ""))
        assert window._filter.text() == ""
        assert not window._live_controls.isVisible()
    finally:
        window.close()
        window.deleteLater()


def local_config():
    config = {"ollama": {"model": "fixture", "host": "http://127.0.0.1:11434"}}
    consent = ai_preferences.accept_consent(config, features=("ava",),
                                             provider=ai_preferences.provider_for(config, "local"))
    return ai_preferences.apply_preferences(config, policy="local", ava=True,
                                             consent=consent, local_model="fixture")


@pytest.fixture
def miss_env(monkeypatch):
    ask_ollama.clear_pending_action()
    ava.reset_cancellations()
    monkeypatch.setattr(commands._command_scope, "capture_context",
                        lambda: MatchContext.for_app("chrome.exe"))
    executor = build_executor()
    app = SimpleNamespace(config=local_config(), command_executor=executor,
                           _ava_cmd_generation=41, _ava_interpret_request=1,
                           _command_mode_miss_count=2, _show_outcome_chip=Mock(),
                           _speak_session_notice=Mock(), audio_coordinator=Mock())
    executor._app = app
    effects = []
    class Handler:
        def execute(self, data, context):
            effects.append(data)
            return True
    monkeypatch.setattr(commands, "get_handler", lambda _kind: Handler())
    monkeypatch.setattr(executor, "_build_context", lambda _app: None)
    target = {"hwnd": 10, "pid": 11, "process": "fixture.exe", "title_hash": "synthetic"}
    monkeypatch.setattr(ep, "foreground_window_identity", lambda: dict(target))
    monkeypatch.setattr(handlers, "_get_foreground_hwnd", lambda: target.get("hwnd", 0))
    monkeypatch.setattr(ava_readiness, "readiness_for", lambda _app: SimpleNamespace(ready=True))
    provider = Mock(return_value="CONFIRM Untrusted model wording\nACTION snap left")
    monkeypatch.setattr(ask_ollama, "ask_ollama", provider)
    monkeypatch.setattr(executor, "ava_menu", lambda *_a, **_kw: ["snap left", "snap right", "focus"])
    yield SimpleNamespace(app=app, executor=executor, effects=effects, target=target, provider=provider)
    ask_ollama.clear_pending_action()
    ava.reset_cancellations()


def dispatch_miss(env, text="put this window on the left side"):
    from dictation import DictationApp  # approved whole-class adapter, no bootstrap
    return DictationApp._resolve_command_miss(
        env.app, DispatchOutcome(kind="command_miss", detail={"heard": "synthetic miss"}), text)


@pytest.mark.parametrize("route", ["local", "cloud"])
def test_real_miss_proposes_once_and_yes_executes_once_via_model_policy(miss_env, monkeypatch, route):
    if route == "cloud":
        config = {"cloud_llm": {"provider": "deepseek", "api_key": True}}
        consent = ai_preferences.accept_consent(
            config, provider=ai_preferences.provider_for(config, "cloud"), cloud=True)
        miss_env.app.config = ai_preferences.apply_preferences(
            config, policy="cloud", ava=True, consent=consent)
    spy = Mock(wraps=ep.authorize)
    monkeypatch.setattr(ep, "authorize", spy)
    outcome = dispatch_miss(miss_env)
    assert outcome.kind == "command_suggestion_pending"
    assert outcome.detail["phrase"] == "snap left"
    assert miss_env.effects == []
    assert miss_env.provider.call_count == 1
    pending = ep.pending_operation()
    assert pending.prompt == "Did you mean 'snap left'? Say yes."
    assert "Untrusted" not in pending.prompt
    assert ep.answer_pending(miss_env.app, "yes") == "approved"
    assert len(miss_env.effects) == 1
    assert ep.answer_pending(miss_env.app, "yes") is None
    assert len(miss_env.effects) == 1
    assert all(c.args[0].route is ep.Route.MODEL for c in spy.call_args_list)
    assert any(c.kwargs.get("confirmed") for c in spy.call_args_list)


@pytest.mark.parametrize("change", ["off", "interpret_off", "window", "closed", "pid", "title",
                                    "generation", "new_request", "new_command", "cancelled", "expiry",
                                    "pack", "scope", "args"])
def test_yes_cannot_approve_changed_authority_or_context(miss_env, change, monkeypatch):
    assert dispatch_miss(miss_env).kind == "command_suggestion_pending"
    pending = ep.pending_operation()
    if change == "off":
        miss_env.app.config["ava"]["provider_policy"] = "off"
    elif change == "interpret_off":
        miss_env.app.config["ava_command_session"] = {"interpret_misses": False}
    elif change in {"window", "pid", "title"}:
        miss_env.target[{"window": "hwnd", "pid": "pid", "title": "title_hash"}[change]] = "changed"
    elif change == "closed":
        miss_env.target.clear()
    elif change == "generation":
        miss_env.app._ava_cmd_generation += 1
    elif change == "new_request":
        miss_env.app._ava_interpret_request += 1
    elif change == "new_command":
        miss_env.executor._grammar_request_serial = getattr(miss_env.executor, "_grammar_request_serial", 0) + 1
    elif change == "cancelled":
        ava.cancel(str(miss_env.app._ava_interpret_request))
    elif change == "expiry":
        pending.deadline = 0
    elif change == "pack":
        monkeypatch.setattr(miss_env.executor, "command_availability", lambda *_a, **_kw: ("pack_disabled", "fixture"))
    elif change == "scope":
        monkeypatch.setattr(miss_env.executor, "command_availability", lambda *_a, **_kw: ("out_of_scope", "fixture"))
    else:
        pending.invocation.args["remainder"] = "changed"
    ep.answer_pending(miss_env.app, "yes")
    assert miss_env.effects == []


@pytest.mark.parametrize("reply", ["no", "ava cancel"])
def test_no_and_stop_cancel_suggestions(miss_env, reply):
    assert dispatch_miss(miss_env).kind == "command_suggestion_pending"
    if reply == "no":
        assert ep.answer_pending(miss_env.app, reply) == "rejected"
    else:
        ep.stop_all(miss_env.app, reply)
    ep.answer_pending(miss_env.app, "yes")
    assert miss_env.effects == []


def test_model_cannot_approve_and_prose_yes_does_not_approve(miss_env):
    dispatch_miss(miss_env)
    pending = ep.pending_operation()
    assert not pending.approve(source=ep.ANSWER_MODEL)
    assert ep.answer_pending(miss_env.app, "yes I was thinking about that") is None
    assert miss_env.effects == []


@pytest.mark.parametrize("mode", ["off", "interpret_off", "pending"])
def test_ai_off_disabled_interpretation_and_existing_pending_stay_plain_misses(miss_env, mode):
    if mode == "off":
        miss_env.app.config["ava"]["provider_policy"] = "off"
    elif mode == "interpret_off":
        miss_env.app.config["ava_command_session"] = {"interpret_misses": False}
    else:
        ep.stage_pending(miss_env.app, ep.Invocation("snap right", generation=41), "Existing question")
    existing = ep.pending_operation()
    assert dispatch_miss(miss_env).kind == "command_miss"
    miss_env.provider.assert_not_called()
    assert ep.pending_operation() is existing
    assert miss_env.effects == []


@pytest.mark.parametrize("response", ["CONFIRM Anything\nACTION fabricated command",
                                      "CONFIRM Anything\nACTION snap left\nACTION snap right",
                                      "CONFIRM Anything\nACTION2 focus invented target"])
def test_unknown_chained_or_ungrounded_model_output_is_a_plain_miss(miss_env, response):
    miss_env.provider.return_value = response
    assert dispatch_miss(miss_env).kind == "command_miss"
    assert ep.pending_operation() is None and miss_env.effects == []


def test_unknown_id_and_invalid_args_cannot_be_staged(miss_env):
    for name, args in [("fabricated command", {}), ("snap left", {"remainder": "ignored suffix"}),
                       ("snap left", {"extra": "value"})]:
        result = ask_ollama.suggest_command_miss(miss_env.app, name, generation=41, args=args)
        assert result.state is State.MISS
        assert ep.pending_operation() is None


def test_semantic_app_target_cannot_bypass_existing_model_authority(miss_env, monkeypatch):
    named = {"hwnd": 20, "process": "notes.exe"}
    monkeypatch.setattr(ep, "resolve_target", lambda _name: dict(named))
    result = ask_ollama.suggest_command_miss(miss_env.app, "focus", generation=41,
                                            args={"remainder": "notes"})
    # The live focus schema declares app_name, which only user routes may
    # validate. A model's free-text remainder must stay a miss, never a prompt.
    assert result.state is State.MISS
    assert ep.pending_operation() is None
    assert miss_env.effects == []


def test_help_wrapper_passes_scope_query_and_controls_without_constructing_widgets(help_env):
    wrapper = sheet.CommandCheatSheetQt(palette_path=None)
    wrapper.show_scoped({"core"}, query="tab", live_text="Controls found here: click New tab.")
    assert wrapper._window is None
    assert wrapper._pending_pack_scope == frozenset({"core"})
    assert wrapper._pending_view == ("All", "tab", "Controls found here: click New tab.")
