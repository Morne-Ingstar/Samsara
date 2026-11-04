"""Model/composition bypass regressions; inert services and sandbox files only."""
import copy
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava
from samsara import composition_policy as composition, effect_policy as effects
from samsara import execution_policy as ep, commands as commands_mod
from samsara.command_registry import DispatchResult, DispatchState
from samsara.commands import CommandExecutor
from samsara.smart_actions_tools import ToolDispatcher


@pytest.fixture(autouse=True)
def isolated_pending(monkeypatch):
    ava.clear_pending_action()
    monkeypatch.setattr(ep, "listen_for_confirmation", lambda *a: None)
    yield
    ava.clear_pending_action()


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setattr(ToolDispatcher, "_init_emergency_db", lambda self: None)
    app = SimpleNamespace(config={}, _ava_cmd_generation=7, _show_outcome_chip=Mock())
    dispatcher = ToolDispatcher(app, {"allowed_directories": [str(tmp_path)],
        "personal_append_files": [str(tmp_path / "notes.md")],
        "allowed_domains": ["api.example.com"], "tier2_approvals": {}})
    monkeypatch.setattr(ep, "SMART_ACTION_SCHEMAS", {
        "append_to_file": {"path": {"type": "str", "required": True},
                           "content": {"type": "str", "required": True}},
        "paste_text": {"text": {"type": "str", "required": True}},
        # Only primitives are reviewed in this fixture. Production stays empty.
        "webhook_trigger": {"url": {"type": "str", "required": True},
                            "payload": {"type": "str"}},
        **{name: {"unused": {"type": "str"}} for name in composition.PLACEHOLDER_TOOLS},
    })
    return dispatcher, app, tmp_path


def webhook(payload="first"):
    return {"tool": "webhook_trigger", "args": {
        "url": "https://api.example.com/hook", "payload": payload}}


@pytest.mark.parametrize("remembered", ["legacy", "exact", "both"])
def test_remembered_webhook_never_approves_a_payload(tools, monkeypatch, remembered):
    dispatcher, app, _ = tools
    call = webhook()
    keys = ["webhook_trigger|" + call["args"]["url"],
            dispatcher._build_approval_key(call["tool"], call["args"])]
    selected = keys if remembered == "both" else keys[:1] if remembered == "legacy" else keys[1:]
    dispatcher._approvals.update({key: True for key in selected})
    confirm = Mock(return_value=(False, False))
    execute = Mock()
    monkeypatch.setattr(dispatcher, "_request_confirmation", confirm)
    monkeypatch.setattr(dispatcher, "_execute", execute)
    assert not dispatcher.dispatch(call)["success"]
    assert confirm.call_count == 1
    assert confirm.call_args.kwargs["allow_always"] is False
    execute.assert_not_called()


def test_webhook_asks_again_after_success_and_cannot_store_always(tools, monkeypatch):
    dispatcher, _, _ = tools
    confirm = Mock(return_value=(True, True))
    execute = Mock(return_value={"success": True})
    monkeypatch.setattr(dispatcher, "_request_confirmation", confirm)
    monkeypatch.setattr(dispatcher, "_execute", execute)
    for payload in ("first", "different", "first"):
        assert dispatcher.dispatch(webhook(payload))["success"]
    assert confirm.call_count == execute.call_count == 3
    assert dispatcher._approvals == {}


@pytest.mark.parametrize("change", ["generation", "scope"])
def test_tool_rechecks_after_the_answer(tools, monkeypatch, change):
    dispatcher, app, _ = tools

    def confirm(*a, **k):
        if change == "generation":
            app._ava_cmd_generation += 1
        else:
            dispatcher.allowed_domains.clear()
        return True, False

    monkeypatch.setattr(dispatcher, "_request_confirmation", confirm)
    execute = Mock()
    monkeypatch.setattr(dispatcher, "_execute", execute)
    assert not dispatcher.dispatch(webhook())["success"]
    execute.assert_not_called()


def test_caller_mutation_cannot_change_the_approved_payload(tools, monkeypatch):
    dispatcher, _, _ = tools
    original = webhook()

    def confirm(*a, **k):
        original["args"]["payload"] = "changed by caller"
        return True, False

    monkeypatch.setattr(dispatcher, "_request_confirmation", confirm)
    execute = Mock(return_value={"success": True})
    monkeypatch.setattr(dispatcher, "_execute", execute)
    assert dispatcher.dispatch(original)["success"]
    assert execute.call_args.args[1]["payload"] == "first"


@pytest.mark.parametrize("changed", [
    {"url": "https://api.example.com/other"}, {"payload": "second"},
])
def test_approval_key_binds_full_arguments(tools, changed):
    dispatcher, _, _ = tools
    before = webhook()["args"]
    assert dispatcher._build_approval_key("webhook_trigger", before) != (
        dispatcher._build_approval_key("webhook_trigger", dict(before, **changed)))


def test_append_keys_do_not_share_a_parent_or_content_scope(tools):
    dispatcher, _, root = tools
    args = {"path": str(root / "one.md"), "content": "entry"}
    key = dispatcher._build_approval_key("append_to_file", args)
    assert key != dispatcher._build_approval_key("append_to_file", dict(args, path=str(root / "two.md")))
    assert key != dispatcher._build_approval_key("append_to_file", dict(args, content="other"))


@pytest.mark.parametrize("name", sorted(composition.PLACEHOLDER_TOOLS))
def test_placeholder_is_visibly_unavailable_without_a_question(tools, monkeypatch, name):
    dispatcher, _, _ = tools
    confirm = Mock()
    monkeypatch.setattr(dispatcher, "_request_confirmation", confirm)
    result = dispatcher.dispatch({"tool": name, "args": {}})
    assert not result["success"] and "not yet implemented" in result["result"]
    confirm.assert_not_called()


@pytest.mark.parametrize("args", [[], "text", 7, None])
def test_malformed_tool_arguments_never_execute(tools, monkeypatch, args):
    dispatcher, _, _ = tools
    execute = Mock()
    monkeypatch.setattr(dispatcher, "_execute", execute)
    assert not dispatcher.dispatch({"tool": "paste_text", "args": args})["success"]
    execute.assert_not_called()


@pytest.mark.parametrize("relative", [
    "launch.py", "launch.ps1", "launch.bat", "app.exe", "config.json", "settings.yaml",
    "AGENTS.md", "CLAUDE.md", "SKILL.md", "requirements.txt", ".ssh/notes.txt",
    "config/notes.md", "plugins/notes.md", "Shared/notes.txt", "OneDrive - Team/notes.md",
    "Dropbox/notes.txt", "note.txt:payload.ps1",
])
def test_parent_directory_cannot_authorize_protected_append(tools, monkeypatch, relative):
    dispatcher, _, root = tools
    execute = Mock()
    monkeypatch.setattr(dispatcher, "_execute", execute)
    result = dispatcher.dispatch({"tool": "append_to_file", "args": {
        "path": str(root / relative), "content": "inert test entry"}})
    assert not result["success"]
    execute.assert_not_called()


def test_personal_note_append_is_immediate_and_appends_exactly(tools, monkeypatch):
    dispatcher, _, root = tools
    note = root / "notes.md"
    note.write_text("existing\n", encoding="utf-8")
    confirm = Mock()
    monkeypatch.setattr(dispatcher, "_request_confirmation", confirm)
    assert dispatcher.dispatch({"tool": "append_to_file", "args": {
        "path": str(note), "content": "new entry"}})["success"]
    assert note.read_text(encoding="utf-8") == "existing\nnew entry\n"
    confirm.assert_not_called()


def test_directory_grant_alone_cannot_authorize_even_an_inert_file(tools, monkeypatch):
    from samsara.config_defaults import cfg_get
    dispatcher, _, root = tools
    assert cfg_get({}, 'smart_actions.personal_append_files') == []
    execute = Mock()
    monkeypatch.setattr(dispatcher, '_execute', execute)
    result = dispatcher.dispatch({'tool': 'append_to_file', 'args': {
        'path': str(root / 'other.md'), 'content': 'entry'}})
    assert not result['success'] and 'exact personal' in result['result']
    execute.assert_not_called()


def test_mapped_network_note_cannot_inherit_local_directory_permission(tools, monkeypatch):
    dispatcher, _, root = tools
    monkeypatch.setattr(composition, 'network_drive', lambda path: True)
    assert not dispatcher._check_scope('append_to_file', {'path': str(root / 'notes.md')})[0]


def test_repository_and_hardlink_notes_are_not_autoappend_targets(tools):
    dispatcher, _, root = tools
    repository = root / "project"
    repository.mkdir()
    (repository / ".git").write_text("inert worktree marker")
    assert not dispatcher._check_scope("append_to_file", {"path": str(repository / "notes.md")})[0]
    note = root / "notes.txt"
    note.write_text("entry")
    os.link(note, root / "alias.txt")
    assert not dispatcher._check_scope("append_to_file", {"path": str(note)})[0]


@pytest.mark.parametrize("name,args", [
    ("paste_text", {"text": "hello"}),
    ("append_to_file", {"path": "notes.md", "content": "entry"}),
    ("webhook_trigger", {"url": "https://api.example.com/hook"}),
])
def test_private_effect_implementations_require_a_live_dispatch(tools, name, args):
    dispatcher, _, root = tools
    if name == "append_to_file":
        args = dict(args, path=str(root / "notes.md"))
    assert not getattr(dispatcher, "_tool_" + name)(args)["success"]
    assert not (root / "notes.md").exists()


@pytest.mark.parametrize("reason", ["refused_terminal", "refused_password", "refused_elevated"])
def test_paste_uses_mcp_safety_refusals(tools, monkeypatch, reason):
    from samsara import agent_server
    dispatcher, _, _ = tools
    monkeypatch.setattr(agent_server, "_foreground_target", lambda: ({}, reason))
    execute = Mock()
    monkeypatch.setattr(dispatcher, "_execute", execute)
    result = dispatcher.dispatch({"tool": "paste_text", "args": {"text": "hello"}})
    assert not result["success"] and reason in result["result"]
    execute.assert_not_called()


def test_paste_binds_editable_control_and_rechecks_before_injection(tools, monkeypatch):
    import sys
    dispatcher, _, _ = tools
    target = ["editor", 1]
    monkeypatch.setattr(composition, "paste_target", lambda app: tuple(target))
    pasted = Mock()

    def copied(text):
        target[1] += 1

    monkeypatch.setitem(sys.modules, "pyperclip", SimpleNamespace(copy=copied))
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(hotkey=pasted))
    assert not dispatcher.dispatch({"tool": "paste_text", "args": {"text": "hello"}})["success"]
    pasted.assert_not_called()


def test_webhook_redirect_cannot_inherit_payload_approval(tools, monkeypatch):
    import urllib.request as request
    dispatcher, _, _ = tools
    seen = []
    monkeypatch.setattr(dispatcher, "_request_confirmation", lambda *a, **k: (True, False))

    def opener(handler):
        def open_(req, timeout):
            seen.append(req.full_url)
            return handler().redirect_request(req, None, 307, "redirect", {}, "https://other.example/hook")
        return SimpleNamespace(open=open_)

    monkeypatch.setattr(request, "build_opener", opener)
    assert not dispatcher.dispatch(webhook())["success"]
    assert seen == ["https://api.example.com/hook"]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"commands": {
        "switch window": {"type": "hotkey", "keys": ["alt", "tab"]},
        "save": {"type": "hotkey", "keys": ["ctrl", "s"]},
        "enter": {"type": "press", "key": "enter"},
        "close window": {"type": "hotkey", "keys": ["alt", "f4"]},
        "repeat": {"type": "method", "method": "repeat_last_command"},
        "macro": {"type": "macro", "steps": []},
    }}))
    app = SimpleNamespace(config={}, _ava_cmd_generation=7, _show_outcome_chip=Mock(),
                          audio_coordinator=Mock())
    ex = CommandExecutor(path, plugins_dir=tmp_path / "none", app=app)
    app.command_executor = ex
    target = [effects.SubmitTarget("editor", 42, "notepad.exe", "edit", "v1", label="Notepad")]
    monkeypatch.setattr(effects, "resolve_submit_target", lambda app=None: target[0])
    monkeypatch.setattr(ep, "foreground_window_identity", lambda: {
        "hwnd": target[0].hwnd, "process": target[0].process, "pid": 9, "title_hash": "test"})
    calls = []
    handler = SimpleNamespace(execute=lambda command, ctx: calls.append(copy.deepcopy(command)) or True)
    monkeypatch.setattr(commands_mod, "get_handler", lambda kind: handler)
    return app, ex, target, calls, handler


@pytest.mark.parametrize("route", [ep.Route.MACRO, ep.Route.SCHEDULE])
def test_composed_route_cannot_launder_model_allowlist(harness, route):
    app, ex, _, calls, _ = harness
    app.config["execution_policy"] = {"model_tool_allowlist": ["switch window"]}
    assert ex.execute_canonical("save", app, route=route, generation=7).state is DispatchState.REJECTED
    assert not calls
    assert ex.execute_canonical("switch window", app, route=route, generation=7).state is DispatchState.COMPLETED


def test_nested_dispatch_cannot_drop_model_source_or_generation(harness):
    app, ex, _, calls, _ = harness
    app.config["execution_policy"] = {"model_tool_allowlist": ["switch window"]}
    parent = ep.Invocation("wrapper", route=ep.Route.MODEL, generation=7)
    outcome = composition.run_bound(parent, lambda: ex.execute_canonical("save", app))
    assert outcome.state is DispatchState.REJECTED and not calls
    app._ava_cmd_generation = 8
    outcome = composition.run_bound(parent, lambda: ex.execute_canonical("switch window", app))
    assert outcome.state is DispatchState.REJECTED and not calls


def test_each_macro_commit_has_its_own_question(harness):
    app, ex, target, calls, _ = harness
    ex.commands["macro"]["steps"] = [{"action": "hotkey", "keys": ["alt", "f4"]}] * 2
    outcome = ex.execute_canonical("macro", app, confirmed=True, generation=7)
    assert outcome.state is DispatchState.QUEUED and not calls
    first = ep.pending_operation()
    assert first.approve(app=app)
    assert len(calls) == 1
    second = ep.pending_operation()
    assert second is not None and second is not first
    assert first.approve(app=app) is False
    assert len(calls) == 1
    assert second.approve(app=app)
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["plan", "target", "generation"])
def test_macro_answer_cannot_approve_changed_plan_or_target(harness, change):
    app, ex, target, calls, _ = harness
    ex.commands["macro"]["steps"] = [{"action": "hotkey", "keys": ["alt", "f4"]}]
    assert ex.execute_canonical("macro", app).state is DispatchState.QUEUED
    op = ep.pending_operation()
    if change == "plan":
        ex.commands["macro"]["steps"][0]["keys"] = ["ctrl", "w"]
    elif change == "target":
        target[0] = replace(target[0], hwnd=43)
    else:
        app._ava_cmd_generation += 1
    assert not op.approve(app=app)
    assert not calls


def test_ordinary_macro_steps_are_immediate_and_preserve_order(harness):
    app, ex, _, calls, _ = harness
    ex.commands["macro"]["steps"] = [{"action": "hotkey", "keys": keys}
        for keys in (["ctrl", "a"], ["ctrl", "s"], ["alt", "tab"])]
    assert ex.execute_canonical("macro", app).state is DispatchState.COMPLETED
    assert [item["steps"][0]["keys"] for item in calls] == [["ctrl", "a"], ["ctrl", "s"], ["alt", "tab"]]
    assert ep.pending_operation() is None


def test_macro_re_resolves_a_commit_after_navigation(harness):
    app, ex, target, calls, handler = harness
    ex.commands["macro"]["steps"] = [{"action": "hotkey", "keys": ["alt", "tab"]},
                                      {"action": "press", "key": "enter"}]

    def execute(command, ctx):
        calls.append(command)
        target[0] = replace(target[0], kind="public", process="browser.exe", revision="public-v2")
        return True

    handler.execute = execute
    assert ex.execute_canonical("macro", app).state is DispatchState.QUEUED
    assert len(calls) == 1
    assert ep.pending_operation().invocation.effect_target.kind == "public"


def test_repeat_preserves_source_and_never_replays_unknown_submit(harness):
    app, ex, _, calls, _ = harness
    app._last_command_name = "save"
    app._last_command = copy.deepcopy(ex.commands["save"])
    assert ex.execute_canonical("repeat", app).state is DispatchState.COMPLETED
    calls.clear()
    app.config["execution_policy"] = {"model_tool_allowlist": ["repeat"]}
    assert ex.execute_canonical("repeat", app, route=ep.Route.MODEL, generation=7).state is DispatchState.REJECTED
    assert not calls
    app._last_command_name = "enter"
    app._last_command = copy.deepcopy(ex.commands["enter"])
    assert ex.execute_canonical("repeat", app).state is DispatchState.REJECTED
    assert not calls


@pytest.mark.parametrize("state", [DispatchState.QUEUED, DispatchState.FAILED, DispatchState.REJECTED])
def test_schedule_key_uses_executor_and_stops_on_uncertain_outcome(harness, monkeypatch, state):
    app, ex, _, calls, _ = harness
    execute = Mock(return_value=DispatchResult(state, "key:enter", "key:enter", {}))
    monkeypatch.setattr(ex, "execute_canonical", execute)
    stop = Mock()
    monkeypatch.setattr(ava, "_stop_schedule", stop)
    ava._execute_safe(app, {"key": "enter", "generation": 7})
    execute.assert_called_once_with("key:enter", app, route=ep.Route.SCHEDULE, generation=7)
    stop.assert_called_once()
    assert not calls


def test_schedule_unknown_result_and_already_queued_cancel_do_not_retry(harness, monkeypatch):
    import threading
    app, ex, _, _, _ = harness
    execute = Mock(return_value=None)
    stop_schedule = Mock()
    monkeypatch.setattr(ex, 'execute_canonical', execute)
    monkeypatch.setattr(ava, '_stop_schedule', stop_schedule)
    ava._execute_safe(app, {'key': 'tab', 'generation': 7})
    stop_schedule.assert_called_once()
    queued = []
    app._schedule_ui = queued.append
    stop = threading.Event()
    ava._execute_safe(app, {'key': 'tab', 'generation': 7}, stop=stop)
    stop.set()
    queued.pop()()
    assert execute.call_count == 1


def test_macro_type_cannot_smuggle_navigation_or_submit_keys(harness, monkeypatch):
    app, ex, _, calls, _ = harness
    monkeypatch.setattr(composition, 'paste_target', lambda app: ('safe', 'editor'))
    ex.commands['macro']['steps'] = [{'action': 'type', 'text': 'text\t\n'}]
    assert ex.execute_canonical('macro', app).state is DispatchState.REJECTED
    assert not calls


def test_queued_schedule_callback_freezes_task_and_rechecks_generation(harness, monkeypatch):
    app, ex, _, calls, _ = harness
    queue = []
    app._schedule_ui = queue.append
    task = {"command": "switch window", "generation": 7}
    ava._execute_safe(app, task)
    task["command"] = "close window"
    queue.pop()()
    assert len(calls) == 1 and calls[0] == ex.commands["switch window"]
    ava._execute_safe(app, task)
    app._ava_cmd_generation += 1
    monkeypatch.setattr(ava, "_stop_schedule", Mock())
    queue.pop()()
    assert len(calls) == 1


@pytest.mark.parametrize("command,expected", [("switch window", "completed"), ("close window", "refused")])
def test_schedule_setup_is_immediate_only_for_admitted_ordinary_effects(harness, monkeypatch, command, expected):
    app, ex, _, calls, _ = harness
    start = Mock()
    monkeypatch.setattr(ava, "_start_schedule", start)
    monkeypatch.setattr(ava, "speak", lambda *a, **k: None)
    outcome = ava.handle_response(app, f"CONFIRM Model wording\nSCHEDULE 5 {command}",
                                  original_text="repeat that", generation=7)
    assert outcome.state == expected
    assert start.call_count == (1 if expected == "completed" else 0)
    assert ep.pending_operation() is None and not calls


def test_dispatch_voice_keeps_task_and_plan_binding_without_a_wait(monkeypatch):
    from samsara.agent_channel.dispatch_voice import DispatchVoice
    from samsara import agora_bridge
    request = Mock(return_value={"accepted": True})
    monkeypatch.setattr(agora_bridge, "dispatch_request", request)
    assert DispatchVoice().approve("task-test", "plan-test") == {"accepted": True}
    assert request.call_args.args[2] == {"task_id": "task-test", "plan_hash": "plan-test", "source": "voice"}


@pytest.mark.parametrize("answer_source,reason", [
    (ep.ANSWER_VOICE, "delivered"), (ep.ANSWER_MODEL, "timeout"),
])
def test_mcp_type_text_keeps_its_per_operation_spoken_approval(tmp_path, monkeypatch, answer_source, reason):
    from samsara import agent_server as agents
    monkeypatch.setattr(agents, "TYPE_TIMEOUT", 0.06)
    monkeypatch.setattr(agents, "CONFIRM_ECHO_TAIL", 0)
    app = SimpleNamespace(config={"command_packs": {"agents": True},
        "agent_server": {"allow_typing": True}}, _ava_cmd_generation=7, _running=True)
    prompts, pastes = [], []

    def speak(prompt, **kwargs):
        prompts.append(prompt)
        kwargs["on_done"]()
        return SimpleNamespace(utterance_id="inert")

    app.audio_coordinator = SimpleNamespace(speak=speak, is_speaking=False)
    app._show_outcome_chip = lambda *args: ep.answer_pending(app, "yes", source=answer_source)
    service = agents.AgentServer(app, profile=tmp_path)
    monkeypatch.setattr(service, "_on_ui", lambda fn: fn())
    monkeypatch.setattr(service, "_target_probe", lambda: ({"hwnd": 10, "pid": 20,
        "birth": 1, "exe": "notepad.exe", "app": "Notepad"}, None))
    monkeypatch.setattr(agents.paste_delivery, "paste_preserving_clipboard",
        lambda app, text, **kwargs: pastes.append(text) is None if kwargs["before_paste"]() else False)
    result = service._type_text({"text": "inert sample"}, "Test agent", 0)
    assert result["reason"] == reason
    assert len(prompts) == 1
    assert pastes == (["inert sample"] if reason == "delivered" else [])
    assert service._http is None  # No service, token, port or network was started.


@pytest.mark.parametrize("cancel_during_reply", [False, True])
def test_smart_actions_route_carries_capture_generation(tools, monkeypatch, cancel_during_reply):
    from plugins.commands import smart_actions as smart
    dispatcher, app, _ = tools
    app._smart_actions_tools = Mock()
    app._smart_actions_session = Mock()
    app._smart_actions_session.consume_observations.return_value = []
    app._smart_actions_session.snapshot_context.return_value = []
    bridge = app._smart_actions_bridge = Mock()

    def reply(*a):
        if cancel_during_reply:
            app._ava_cmd_generation += 1
        return {"tool_calls": [webhook()]}

    bridge.send.side_effect = reply
    monkeypatch.setattr(smart, "_play_earcon", lambda *a: None)
    monkeypatch.setattr(smart, "get_config", lambda app: {})
    assert smart._do_agent_route(app, "inert request", "ask")
    if cancel_during_reply:
        app._smart_actions_tools.dispatch.assert_not_called()
    else:
        assert app._smart_actions_tools.dispatch.call_args.kwargs == {"generation": 7}


def test_composition_cannot_validate_undeclared_spoken_slots():
    # A model cannot get an undeclared argument admitted by calling it a macro.
    for route in (ep.Route.MODEL, ep.Route.SMART_ACTION, ep.Route.MACRO, ep.Route.SCHEDULE):
        assert ep.validate_args(None, {"remainder": "unreviewed target"}, route=route)[0] == "unvalidated"
    assert ep.validate_args(None, {"remainder": "spoken target"}, route=ep.Route.EXACT) is None
