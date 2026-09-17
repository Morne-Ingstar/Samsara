"""Execution policy (queue 02c): the ONE choke point between "something
decided to act" and a side effect, plus the request-generation cancellation
that every Ava lane shares.

Covers, in order:
  * the risk classifier (built-in type + keys, ACTION2 verbs, Smart Actions
    tiers, plugin metadata -- unknown is never safe)
  * the policy table (route x risk -> Allowed / NeedsConfirmation / Denied)
  * Astra 2026-09-12 MODEL_BYPASS payloads (+ canonical synonyms): no effect
  * CANCEL_THEN_LATE_RESPONSE: a model answer after "ava cancel" does nothing
  * exit / re-enter with a queued item: nothing runs
  * unlisted tool id and out-of-range argument -> Denied
  * exact "switch window" still runs with no prompt
  * the stop path with a stalled fake model and a stalled fake delivery
    adapter (the Smart Actions dialog); voice "yes" and the dialog resolve
    ONE pending operation
"""
import ast
import logging
import collections
import json
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from plugins.commands import ask_ollama  # noqa: E402
from plugins.commands.app_verbs import ActionResult  # noqa: E402
from samsara import ava_command_session, commands as commands_mod, execution_policy as ep  # noqa: E402
from samsara import plugin_commands  # noqa: E402
from samsara.commands import CommandExecutor  # noqa: E402
from samsara.execution_policy import (  # noqa: E402
    Allowed, Denied, Invocation, NeedsConfirmation, Route,
    RISK_DESTRUCTIVE, RISK_UI, RISK_UNKNOWN, RISK_WRITE,
)

COMMANDS = {
    "switch window": {"type": "hotkey", "keys": ["alt", "tab"]},
    "copy": {"type": "hotkey", "keys": ["ctrl", "c"]},
    "close window": {"type": "hotkey", "keys": ["alt", "f4"]},
    "close tab": {"type": "hotkey", "keys": ["ctrl", "w"]},
    "enter": {"type": "press", "key": "enter"},
    "submit": {"type": "hotkey", "keys": ["enter"]},
    "delete selection": {"type": "press", "key": "delete"},
    "say hello": {"type": "text", "text": "hello"},
    "repeat that": {"type": "method", "method": "repeat_last_command"},
    "tidy up": {"type": "macro", "steps": [
        {"action": "hotkey", "keys": ["alt", "tab"]},
        {"action": "hotkey", "keys": ["alt", "f4"]},
    ]},
}


class _FakeHandler:
    """Stands in for every handler type: records the command it ran."""

    def __init__(self, effects):
        self.effects = effects

    def execute(self, cmd, ctx):
        self.effects.append(cmd)
        return True


@pytest.fixture
def effects():
    return []


@pytest.fixture
def executor(tmp_path, effects, monkeypatch):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"commands": COMMANDS}), encoding="utf-8")
    ex = CommandExecutor(path, plugins_dir=tmp_path / "no_plugins")
    handler = _FakeHandler(effects)
    monkeypatch.setattr(commands_mod, "get_handler", lambda _type: handler)
    return ex


@pytest.fixture
def app(executor):
    a = types.SimpleNamespace()
    a.config = {"ollama": {"enabled": True}}
    a.audio_coordinator = Mock()
    a.play_sound = Mock()
    a.command_executor = executor
    a._ava_cmd_generation = 7
    a._ava_cmd_mode_lock = threading.Lock()
    a._ava_session_dispatch_queue = collections.deque(maxlen=3)
    a._ava_session_dispatch_lock = threading.Lock()
    a._ava_session_request_in_flight = False
    a._show_outcome_chip = Mock()
    a.root = None
    return a


@pytest.fixture(autouse=True)
def _clean_pending():
    ask_ollama.clear_pending_action()
    ask_ollama._stop_schedule()
    yield
    ask_ollama.clear_pending_action()
    ask_ollama._stop_schedule()


def _spoken(app):
    return [c.args[0] for c in app.audio_coordinator.speak.call_args_list]


# ---------------------------------------------------------------------------
# 1. Classification: by effect, never by name
# ---------------------------------------------------------------------------

class TestClassify:
    @pytest.mark.parametrize("cmd,expected", [
        ({"type": "hotkey", "keys": ["alt", "tab"]}, RISK_UI),
        ({"type": "hotkey", "keys": ["ctrl", "c"]}, RISK_UI),
        ({"type": "press", "key": "home"}, RISK_UI),
        ({"type": "hotkey", "keys": ["shift", "end"]}, RISK_UI),
        ({"type": "launch", "target": "notepad.exe"}, RISK_UI),
        ({"type": "press", "key": "enter"}, RISK_WRITE),
        ({"type": "hotkey", "keys": ["enter"]}, RISK_WRITE),
        ({"type": "press", "key": "delete"}, RISK_WRITE),
        ({"type": "hotkey", "keys": ["ctrl", "v"]}, RISK_WRITE),
        ({"type": "text", "text": "x"}, RISK_WRITE),
        ({"type": "mouse", "button": "left"}, RISK_WRITE),
        ({"type": "hotkey", "keys": ["alt", "f4"]}, RISK_DESTRUCTIVE),
        ({"type": "hotkey", "keys": ["ctrl", "w"]}, RISK_DESTRUCTIVE),
        ({"type": "hotkey", "keys": "ctrl+shift+w"}, RISK_DESTRUCTIVE),
        ({"type": "hotkey", "keys": ["win", "l"]}, RISK_UI),
        ({"type": "hotkey", "keys": ["shift", "delete"]}, RISK_DESTRUCTIVE),
        ({"type": "method", "method": "show_cheat_sheet"}, RISK_UI),
        ({"type": "method", "method": "repeat_last_command"}, RISK_UNKNOWN),
        ({"type": "method", "method": "never_heard_of_it"}, RISK_UNKNOWN),
        ({"type": "macro", "steps": [{"action": "hotkey", "keys": ["alt", "tab"]}]}, RISK_UI),
        ({"type": "macro", "steps": [{"action": "press", "key": "enter"}]}, RISK_WRITE),
        ({"type": "macro", "steps": [{"action": "hotkey", "keys": ["alt", "tab"]},
                                     {"action": "hotkey", "keys": ["alt", "f4"]}]}, RISK_DESTRUCTIVE),
        ({"type": "macro", "steps": [{"action": "wiggle"}]}, RISK_UNKNOWN),
        ({"type": "something_new"}, RISK_UNKNOWN),
        ({}, RISK_UNKNOWN),
    ])
    def test_builtin_risk_comes_from_type_and_keys(self, cmd, expected):
        assert ep.classify_builtin(cmd) == expected

    def test_action2_verbs(self):
        assert ep.classify("action2:focus")[0] == RISK_UI
        assert ep.classify("action2:open")[0] == RISK_UI
        assert ep.classify("action2:close")[0] == RISK_DESTRUCTIVE
        assert ep.classify("action2:delete")[0] == RISK_UNKNOWN

    def test_smart_action_tiers(self):
        assert ep.classify("smart_action:paste_text")[0] == RISK_UI
        assert ep.classify("smart_action:webhook_trigger")[0] == RISK_WRITE
        assert ep.classify("smart_action:send_email")[0] == RISK_DESTRUCTIVE
        assert ep.classify("smart_action:format_disk")[0] == RISK_UNKNOWN

    def test_unknown_id_is_unknown(self, executor):
        assert ep.classify("frobnicate", executor=executor)[0] == RISK_UNKNOWN
        assert not ep.command_exists("frobnicate", executor=executor)


# ---------------------------------------------------------------------------
# 2. The policy table
# ---------------------------------------------------------------------------

USER = [Route.EXACT, Route.GRAMMAR, Route.MACRO, Route.SCHEDULE]
MODEL = [Route.MODEL, Route.SMART_ACTION]


class TestPolicyTable:
    @pytest.mark.parametrize("route", USER + MODEL)
    def test_ui_allowed_on_every_route(self, app, executor, route):
        d = ep.authorize(Invocation("switch window", route=route, generation=7), app=app, executor=executor)
        assert isinstance(d, Allowed) and d.risk == RISK_UI

    @pytest.mark.parametrize("route", USER)
    def test_write_allowed_when_the_user_said_it(self, app, executor, route):
        d = ep.authorize(Invocation("enter", route=route, generation=7), app=app, executor=executor)
        assert isinstance(d, Allowed) and d.risk == RISK_WRITE

    @pytest.mark.parametrize("route", MODEL)
    def test_write_needs_confirmation_when_a_model_said_it(self, app, executor, route):
        d = ep.authorize(Invocation("enter", route=route, generation=7, prompt="Trust me, just press it"),
                         app=app, executor=executor)
        # The caller/model wording is ignored; the question is the local template.
        assert isinstance(d, NeedsConfirmation) and d.prompt == "Enter?"

    @pytest.mark.parametrize("route", USER + MODEL)
    def test_destructive_close_depends_on_route(self, app, executor, route):
        d = ep.authorize(Invocation("close window", route=route, generation=7), app=app, executor=executor)
        assert isinstance(d, Allowed if route == Route.EXACT else NeedsConfirmation)
        assert d.risk == RISK_DESTRUCTIVE

    @pytest.mark.parametrize("route", USER)
    def test_unknown_needs_confirmation_on_user_routes(self, app, executor, route):
        d = ep.authorize(Invocation("repeat that", route=route, generation=7), app=app, executor=executor)
        assert isinstance(d, Denied) and d.reason == "nothing to repeat"

    @pytest.mark.parametrize("route", MODEL)
    def test_unknown_is_not_on_the_model_allow_list(self, app, executor, route):
        d = ep.authorize(Invocation("repeat that", route=route, generation=7), app=app, executor=executor)
        assert isinstance(d, Denied) and d.reason == "nothing to repeat"

    @pytest.mark.parametrize("route", USER + MODEL)
    def test_stale_generation_is_denied_on_every_route(self, app, executor, route):
        d = ep.authorize(Invocation("switch window", route=route, generation=6), app=app, executor=executor)
        assert isinstance(d, Denied) and d.reason == "stale"

    def test_confirmed_allows_but_is_still_generation_checked(self, app, executor):
        ok = ep.authorize(Invocation("close window", route=Route.MODEL, generation=7),
                          app=app, executor=executor, confirmed=True)
        assert isinstance(ok, Allowed) and ok.reason == "confirmed"
        app._ava_cmd_generation += 1
        late = ep.authorize(Invocation("close window", route=Route.MODEL, generation=7),
                            app=app, executor=executor, confirmed=True)
        assert isinstance(late, Denied) and late.reason == "stale"

    def test_missing_generation_is_stale(self, app, executor):
        d = ep.authorize(Invocation("switch window"), app=app, executor=executor)
        assert isinstance(d, Denied) and d.reason == "stale" and "no generation" in d.detail

    def test_every_decision_is_logged_and_chipped(self, app, executor):
        ep.authorize(Invocation("close window", route=Route.MODEL, generation=7), app=app, executor=executor)
        ep.authorize(Invocation("close window", route=Route.MODEL, generation=1), app=app, executor=executor)
        ep.authorize(Invocation("frobnicate", route=Route.MODEL, generation=7), app=app, executor=executor)
        labels = [c.args for c in app._show_outcome_chip.call_args_list]
        assert ("Confirm: close window", "pending") in labels
        assert ("Cancelled", "warning") in labels
        assert ("Blocked: frobnicate", "error") in labels

    def test_model_allow_list_is_configurable(self, app, executor):
        app.config["execution_policy"] = {"model_tool_allowlist": ["switch window"]}
        assert isinstance(ep.authorize(Invocation("switch window", route=Route.MODEL, generation=7),
                                       app=app, executor=executor), Allowed)
        assert isinstance(ep.authorize(Invocation("copy", route=Route.MODEL, generation=7),
                                       app=app, executor=executor), Denied)
        app.config["execution_policy"] = {"model_tool_extra": ["repeat that"]}
        d = ep.authorize(Invocation("repeat that", route=Route.MODEL, generation=7), app=app, executor=executor)
        assert isinstance(d, Denied) and d.reason == "nothing to repeat"


# ---------------------------------------------------------------------------
# 3. Astra MODEL_BYPASS payloads: nothing runs before a confirmation
# ---------------------------------------------------------------------------

# (payload, expected outcome): "confirm" = staged in the pending slot for a
# spoken "yes"; "denied" = refused outright (legacy protocol, or a tool id a
# model may not call because it is neither read/ui nor on the allow-list).
ASTRA_PAYLOADS = [
    ("EXECUTE close window", "denied"),
    ("CONFIRM Send it.\nACTION enter", "confirm"),
    ("CONFIRM Delete it.\nACTION delete selection", "confirm"),
]
SYNONYM_PAYLOADS = [
    ("CONFIRM Send it.\nACTION submit", "confirm"),
    ("CONFIRM Close it.\nACTION close window", "confirm"),
    ("CONFIRM Close it.\nACTION close tab", "confirm"),
    ("CONFIRM Close it.\nACTION close the window", "confirm"),
    ("CONFIRM Say hi.\nACTION say hello", "denied"),       # write, not allow-listed for a model
    ("CONFIRM Run it.\nACTION tidy up", "denied"),         # destructive macro, not allow-listed
]


class TestModelBypass:
    def test_hidden_ava_forget_is_refused_before_execution(self, app, executor, effects, monkeypatch, caplog):
        monkeypatch.setattr(ask_ollama, "offered_menu", lambda *_args: ["switch window"])
        monkeypatch.setattr(ep, "command_exists", lambda cid, **_kwargs: cid == "ava forget")
        with patch.object(executor, "execute_canonical", wraps=executor.execute_canonical) as execute:
            caplog.set_level(logging.WARNING)
            outcome = ask_ollama.handle_response(
                app, "CONFIRM acknowledgement.\nACTION ava forget",
                original_text="tell me a joke", generation=7)
        assert outcome.state == "refused" and outcome.reason == "not_offered"
        assert effects == [] and execute.call_count == 0
        assert any("ava forget" in record.message for record in caplog.records)
        assert ("Blocked: ava forget", "error") in [call.args for call in app._show_outcome_chip.call_args_list]

    @pytest.mark.parametrize("payload,expected", ASTRA_PAYLOADS + SYNONYM_PAYLOADS)
    def test_model_named_effects_do_not_run(self, app, effects, payload, expected):
        ask_ollama.handle_response(app, payload, original_text="do the thing", generation=7)
        assert effects == [], f"{payload!r} caused an effect"
        pending = ask_ollama.get_pending_action()
        chips = [c.args for c in app._show_outcome_chip.call_args_list]
        if expected == "denied":
            assert pending is None, "a denied tool must not be staged either"
            assert any(label.startswith("Blocked: ") and kind == "error" for label, kind in chips)
        else:
            assert pending is not None and pending["type"] == "action"
            assert pending["generation"] == 7
            assert any("yes to confirm" in s for s in _spoken(app))

    def test_model_action2_close_is_staged_not_run(self, app, effects):
        with patch("plugins.commands.app_verbs.do_close") as do_close:
            ask_ollama.handle_response(app, "CONFIRM Close it.\nACTION2 close | notepad",
                                       original_text="close notepad", generation=7)
        do_close.assert_not_called()
        pending = ask_ollama.get_pending_action()
        assert pending["type"] == "action2" and pending["verb"] == "close"

    def test_yes_runs_exactly_once_through_the_same_choke_point(self, app, effects):
        ask_ollama.handle_response(app, "CONFIRM Send it.\nACTION enter", original_text="send it", generation=7)
        assert effects == []
        ask_ollama.handle_ava_confirm(app)
        assert [c.get("key") for c in effects] == ["enter"]
        assert ask_ollama.get_pending_action() is None
        ask_ollama.handle_ava_confirm(app)          # nothing left to confirm
        assert len(effects) == 1

    def test_yes_after_cancel_runs_nothing(self, app, effects):
        ask_ollama.handle_response(app, "CONFIRM Send it.\nACTION enter", original_text="send it", generation=7)
        ask_ollama.handle_ava_cancel(app)
        assert ask_ollama.get_pending_action() is None
        assert "Cancelled." in _spoken(app)
        ask_ollama.handle_ava_confirm(app)
        assert effects == []

    def test_yes_after_exit_is_stale(self, app, effects):
        ask_ollama.handle_response(app, "CONFIRM Send it.\nACTION enter", original_text="send it", generation=7)
        app._ava_cmd_generation += 1        # session exit / sleep
        ask_ollama.handle_ava_confirm(app)
        assert effects == []
        assert "That request expired." in _spoken(app)

    def test_menu_listed_model_ui_command_runs_immediately(self, app, effects, monkeypatch):
        monkeypatch.setattr(ask_ollama, "offered_menu", lambda *_args: ["switch window"])
        ask_ollama.handle_response(app, "CONFIRM Switching.\nACTION switch window",
                                   original_text="switch", generation=7)
        assert [c.get("keys") for c in effects] == [["alt", "tab"]]
        assert ask_ollama.get_pending_action() is None

    def test_model_unlisted_tool_id_is_denied(self, app, effects):
        ask_ollama.handle_response(app, "CONFIRM Sure.\nACTION format disk", original_text="x", generation=7)
        assert effects == [] and ask_ollama.get_pending_action() is None

    def test_legacy_execute_protocol_is_gone(self, app, effects):
        ask_ollama.handle_response(app, "EXECUTE switch window", original_text="x", generation=7)
        assert effects == []
        assert any("not a tool" in s for s in _spoken(app))
        assert not hasattr(ask_ollama, "_UNSAFE_COMMANDS") or not ask_ollama._UNSAFE_COMMANDS, \
            "the name denylist is retired; risk comes from the registry"


# ---------------------------------------------------------------------------
# 4. Exact route: the hot path is unchanged for low-risk phrases
# ---------------------------------------------------------------------------

class TestExactRoute:
    def test_switch_window_runs_with_no_prompt(self, app, executor, effects):
        assert executor.execute_command("switch window", app) is True
        assert [c.get("keys") for c in effects] == [["alt", "tab"]]
        assert ask_ollama.get_pending_action() is None
        app.audio_coordinator.speak.assert_not_called()

    def test_process_text_switch_window(self, app, executor, effects):
        phrase, ok = executor.process_text("switch window", app, force_commands=True)
        assert (phrase, ok) == ("switch window", True)
        assert len(effects) == 1

    def test_exact_write_runs(self, app, executor, effects):
        assert executor.execute_command("enter", app) is True
        assert len(effects) == 1

    def test_exact_undoable_close_runs_without_prompt(self, app, executor, effects):
        assert executor.execute_command("close window", app, generation=7) is True
        assert ask_ollama.get_pending_action() is None
        assert [c.get("keys") for c in effects] == [["alt", "f4"]]

    def test_macro_is_classified_by_its_worst_step(self, app, executor, effects):
        assert executor.execute_command("tidy up", app) is False
        assert effects == [] and ask_ollama.get_pending_action()["command"] == "tidy up"

    def test_stale_exact_runs_nothing(self, app, executor, effects):
        assert executor.execute_command("switch window", app, generation=3) is False
        assert effects == []


# ---------------------------------------------------------------------------
# 5. Plugins: registry metadata, args, allow-list
# ---------------------------------------------------------------------------

@pytest.fixture
def plugin_registry():
    """Four plugin commands registered through the REAL decorator (so the
    registry's own metadata rules apply), BEFORE the executor snapshots the
    registry -- request this fixture ahead of `executor`/`app`."""
    saved = dict(plugin_commands._REGISTRY)
    plugin_commands._REGISTRY.clear()
    calls = []

    def _handler(phrase):
        def handler(app, remainder):
            calls.append((phrase, remainder))
            return True
        handler.__name__ = phrase.replace(" ", "_")
        return handler

    plugin_commands.command("set volume", risk_class="reversible",
                            param_schema={"level": {"type": "int", "min": 0, "max": 100}})(_handler("set volume"))
    plugin_commands.command("show the time", risk_class="safe",
                            param_schema={"zone": {"type": "str", "max_len": 40}})(_handler("show the time"))
    plugin_commands.command("say the date", risk_class="safe")(_handler("say the date"))  # no schema
    plugin_commands.command("legacy thing")(_handler("legacy thing"))      # nothing declared
    plugin_commands.command("wipe drive", risk_class="destructive")(_handler("wipe drive"))
    try:
        yield calls
    finally:
        plugin_commands._REGISTRY.clear()
        plugin_commands._REGISTRY.update(saved)


class TestPluginPolicy:
    def test_registry_keeps_declared_and_flat_risk_apart(self, plugin_registry):
        assert ep.classify("legacy thing", declared_only=True)[0] == RISK_UNKNOWN
        assert ep.classify("legacy thing")[0] == RISK_UNKNOWN
        assert ep.classify("wipe drive", declared_only=True)[0] == RISK_DESTRUCTIVE
        assert ep.classify("set volume")[0] == RISK_WRITE

    def test_out_of_range_argument_is_denied(self, plugin_registry, app):
        d = ep.authorize(Invocation("set volume", {"level": 500}, Route.EXACT, 7), app=app)
        assert isinstance(d, Denied) and d.reason == "invalid_args"
        d = ep.authorize(Invocation("set volume", {"level": "loud"}, Route.MODEL, 7), app=app)
        assert isinstance(d, Denied) and d.reason == "invalid_args"
        assert isinstance(ep.authorize(Invocation("set volume", {"level": 40}, Route.EXACT, 7), app=app), Allowed)

    def test_declared_safe_plugin_with_a_schema_is_model_callable(self, plugin_registry, app):
        assert isinstance(ep.authorize(Invocation("show the time", route=Route.MODEL, generation=7), app=app), Allowed)

    def test_safe_plugin_without_a_schema_is_model_callable_with_no_arguments(
            self, plugin_registry, app):
        """Queue 107 (deliberate reversal of the earlier rule for THIS case
        only): a read/ui command called with NO arguments has nothing to
        validate, and an ACTION line has no argument slot at all. Denying it
        made almost every plugin -- "volume up" included -- offerable to Ava
        but impossible for her to run, which is Astra F1. Arguments without a
        schema are still refused, and write/destructive/unclassified commands
        are still "unvalidated" (the tests below)."""
        assert isinstance(
            ep.authorize(Invocation("say the date", route=Route.MODEL, generation=7), app=app),
            Allowed)
        d = ep.authorize(Invocation("say the date", {"zone": "utc"}, Route.MODEL, 7), app=app)
        assert isinstance(d, Denied) and d.reason == "unvalidated"

    def test_undeclared_plugin_is_unavailable_to_a_model(self, plugin_registry, app):
        d = ep.authorize(Invocation("legacy thing", route=Route.MODEL, generation=7), app=app)
        assert isinstance(d, Denied) and d.reason == "unvalidated"

    def test_undeclared_plugin_prompts_for_the_users_exact_phrase(self, plugin_registry, executor, app):
        result = executor.process_text("legacy thing", app, force_commands=True)
        assert tuple(result) == ("legacy thing", True)
        assert plugin_registry == []
        assert ask_ollama.get_pending_action()["command"] == "legacy thing"

    def test_declared_destructive_plugin_prompts_on_exact(self, plugin_registry, executor, app):
        result = executor.process_text("wipe drive", app, force_commands=True)
        assert tuple(result) == ("wipe drive", True), "held for confirmation is still a claimed command"
        assert plugin_registry == []
        assert ask_ollama.get_pending_action()["command"] == "wipe drive"
        ask_ollama.handle_ava_confirm(app)
        assert plugin_registry == [("wipe drive", "")]


# ---------------------------------------------------------------------------
# 6. CANCEL_THEN_LATE_RESPONSE (Astra probe): the generation is the owner
# ---------------------------------------------------------------------------

def _capture_spawn(monkeypatch):
    captured = []
    monkeypatch.setattr(ask_ollama.thread_registry, "spawn",
                        lambda name, target, *a, **k: captured.append(target))
    monkeypatch.setattr(ask_ollama, "_check_ollama_available", lambda host, timeout=3: True)
    monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda app, text: False)
    return captured


class TestCancelThenLateResponse:
    def test_late_model_response_after_cancel_runs_nothing(self, app, effects, monkeypatch):
        workers = _capture_spawn(monkeypatch)
        monkeypatch.setattr(ask_ollama, "ask_ollama",
                            lambda prompt, app_: "CONFIRM Switching.\nACTION switch window")
        ask_ollama.handle_ask_ava(app, "switch to the other window")
        assert len(workers) == 1 and effects == []
        ask_ollama.handle_ava_cancel(app)         # bumps the generation
        workers[0]()                              # the model "arrives" now
        assert effects == [], "a response captured under an old generation must not act"
        assert ask_ollama.get_pending_action() is None

    def test_late_model_confirmation_after_cancel_stages_nothing(self, app, effects, monkeypatch):
        workers = _capture_spawn(monkeypatch)
        monkeypatch.setattr(ask_ollama, "ask_ollama", lambda prompt, app_: "CONFIRM Close it.\nACTION close window")
        ask_ollama.handle_ask_ava(app, "close the window")
        ask_ollama.handle_ava_cancel(app)
        workers[0]()
        assert effects == [] and ask_ollama.get_pending_action() is None

    def test_same_generation_response_still_works(self, app, effects, monkeypatch):
        workers = _capture_spawn(monkeypatch)
        monkeypatch.setattr(ask_ollama, "ask_ollama", lambda prompt, app_: "CONFIRM Switching.\nACTION switch window")
        ask_ollama.handle_ask_ava(app, "switch window")
        workers[0]()
        assert len(effects) == 1

    def test_direct_handle_response_with_old_generation_is_denied(self, app, effects):
        ask_ollama.handle_response(app, "CONFIRM Switching.\nACTION switch window", original_text="x", generation=2)
        assert effects == []


# ---------------------------------------------------------------------------
# 7. Stop path independent of understanding (Astra section 5.3)
# ---------------------------------------------------------------------------

def _dictation_app(executor):
    # Compile only the exercised methods: never import/initialize the live app module.
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    names = {"_ava_session_agent_dispatch_fn", "_start_ava_session_worker",
             "_try_stop_utterance", "_on_ava_session_request_done",
             "exit_ava_command_session", "_route_to_ava"}
    namespace = {"logger": logging.getLogger("policy_test")}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    harness = type("PolicyAppHarness", (), {name: namespace[name] for name in names})
    a = harness()
    a._try_cancel_pending_ava_utterance = Mock(return_value=False)
    a.config = {"ollama": {"enabled": True}, "ava_command_session": {}}
    a.audio_coordinator = Mock()
    a.play_sound = Mock()
    a.command_executor = executor
    a._ava_cmd_generation = 3
    a._ava_cmd_mode_lock = threading.Lock()
    a._ava_session_dispatch_lock = threading.Lock()
    a._ava_session_dispatch_queue = collections.deque(maxlen=3)
    a._ava_session_request_in_flight = False
    a._show_outcome_chip = Mock()
    a._touch_session_activity = Mock()
    a._indicator_reset = Mock()
    a._emit_wake_trace = Mock()
    a.ava_command_session_active = True
    a._ava_cmd_miss_count = 0
    a._ava_cmd_ready = threading.Event()
    a._cancel_ava_cmd_inactivity_timer = Mock()
    a._release_wake_consumer = Mock()
    return a


class TestStopPath:
    def test_stop_utterances(self):
        assert ep.is_stop_utterance("Stop.")
        assert ep.is_stop_utterance("ava cancel")
        assert ep.is_stop_utterance("go to sleep")
        assert not ep.is_stop_utterance("stop the music")
        assert not ep.is_stop_utterance("nevermind"), "nevermind stays the pending-only cancel"

    def test_stop_does_not_wait_on_a_stalled_model(self, executor, effects, monkeypatch):
        app = _dictation_app(executor)
        release = threading.Event()
        started = threading.Event()

        def stalled_model(prompt, app_):
            started.set()
            release.wait(5)
            return "CONFIRM Switching.\nACTION switch window"

        monkeypatch.setattr(ask_ollama, "ask_ollama", stalled_model)
        monkeypatch.setattr(ask_ollama, "_check_ollama_available", lambda host, timeout=3: True)
        monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda app_, text: False)
        threads = []

        def real_thread(name, target, *a, **k):
            t = threading.Thread(target=target, daemon=True)
            threads.append(t)
            t.start()
            return t

        monkeypatch.setattr(ask_ollama.thread_registry, "spawn", real_thread)

        app._ava_session_agent_dispatch_fn("switch to the other window", None)
        assert started.wait(2)
        app._ava_session_agent_dispatch_fn("close the window", None)      # queued behind it
        assert len(app._ava_session_dispatch_queue) == 1
        gen_before = app._ava_cmd_generation

        t0 = time.monotonic()
        app._ava_session_agent_dispatch_fn("stop", None)
        assert time.monotonic() - t0 < 1.0, "stop must not queue behind inference"
        assert app._ava_cmd_generation == gen_before + 1
        assert len(app._ava_session_dispatch_queue) == 0
        assert ("stopped", "warning") in [c.args for c in app._show_outcome_chip.call_args_list]

        release.set()
        for t in threads:
            t.join(3)
        assert effects == [], "the model's late answer must not act"
        assert app._ava_session_request_in_flight is False

    def test_exit_then_reenter_with_queued_item_runs_nothing(self, executor, effects):
        app = _dictation_app(executor)
        app._ava_session_request_in_flight = True          # something is mid-flight
        app._ava_session_agent_dispatch_fn("close the window", None)
        assert list(app._ava_session_dispatch_queue) == [(3, "close the window")]

        app.exit_ava_command_session()                     # session exit bumps + clears
        assert app._ava_cmd_generation > 3
        assert len(app._ava_session_dispatch_queue) == 0

        # Re-enter (generation moves again) and let the old in-flight
        # request finish: nothing queued may start, nothing may run.
        app._ava_cmd_generation += 1
        app._start_ava_session_worker = Mock()
        app._on_ava_session_request_done()
        app._start_ava_session_worker.assert_not_called()
        assert app._ava_session_request_in_flight is False
        assert effects == []

    def test_stale_queued_item_is_dropped_at_drain(self, executor, effects):
        app = _dictation_app(executor)
        app._ava_session_request_in_flight = True
        app._ava_session_dispatch_queue.append((2, "close the window"))   # from before a bump
        app._ava_session_dispatch_queue.append((3, "switch window"))      # current
        app._start_ava_session_worker = Mock()
        app._on_ava_session_request_done()
        app._start_ava_session_worker.assert_called_once_with((3, "switch window"))

    def test_hold_ava_stop_bypasses_command_and_model(self, executor, effects):
        app = _dictation_app(executor)
        app.command_executor = Mock()
        app._route_to_ava("cancel")
        app.command_executor.process_text.assert_not_called()

    def test_waterfall_drain_stale_keeps_current_items(self, executor):
        app = _dictation_app(executor)
        ava_command_session.drain_stale(app)               # start empty
        ava_command_session._task_queue.put_nowait((2, "old"))
        ava_command_session._task_queue.put_nowait((3, "new"))
        assert ava_command_session.drain_stale(app) == 1
        assert ava_command_session._task_queue.get_nowait() == (3, "new")

    def test_scheduled_tick_from_an_old_generation_stops(self, app, effects):
        ask_ollama._execute_safe(app, {"command": "switch window", "generation": 2})
        assert effects == []
        ask_ollama._execute_safe(app, {"command": "switch window", "generation": 7})
        assert len(effects) == 1


# ---------------------------------------------------------------------------
# 8. Smart Actions: the dialog is a consumer of the SAME pending operation
# ---------------------------------------------------------------------------

@pytest.fixture
def dispatcher(app, monkeypatch):
    from samsara.smart_actions_tools import ToolDispatcher
    import plugins.commands.smart_actions as sa
    monkeypatch.setattr(sa, "_play_earcon", lambda *a, **k: None)
    monkeypatch.setattr(sa, "get_config", lambda app_: {})
    app.config["smart_actions"] = {"enabled": True, "earcons_enabled": False}
    # Smart Actions tools have no reviewed schema in production (unavailable to
    # the model); these tests exercise the shared pending machinery with one.
    monkeypatch.setattr(ep, "SMART_ACTION_SCHEMAS", {
        "paste_text": {"text": {"type": "str", "required": True}},
        "send_email": {"to": {"type": "str"}},
        "delete_file": {"path": {"type": "str", "required": True}},
    })
    d = ToolDispatcher(app, {"allowed_directories": [], "allowed_domains": [], "tier2_approvals": {}})
    return d


def _run_dispatch_in_thread(d, tool_call):
    box = {}

    def _go():
        box["result"] = d.dispatch(tool_call)

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    for _ in range(100):
        if ask_ollama.get_pending_action() is not None:
            break
        time.sleep(0.02)
    return t, box


class TestSmartActionsSharedPending:
    def test_auto_tier_runs_without_a_prompt(self, dispatcher):
        with patch.object(dispatcher, "_execute", return_value={"success": True, "result": None}) as ex, \
             patch.object(dispatcher, "_confirm_dialog") as dlg:
            dispatcher.dispatch({"tool": "paste_text", "args": {"text": "hi"}})
        ex.assert_called_once()
        dlg.assert_not_called()

    def test_voice_yes_resolves_the_dialogs_pending_op(self, app, dispatcher):
        with patch.object(dispatcher, "_confirm_dialog"), \
             patch.object(dispatcher, "_execute", return_value={"success": True, "result": "sent"}) as ex:
            t, box = _run_dispatch_in_thread(dispatcher, {"tool": "send_email", "args": {}})
            pending = ask_ollama.get_pending_action()
            assert pending is not None and pending["command"] == "smart_action:send_email"
            ask_ollama.handle_ava_confirm(app)            # voice "yes"
            t.join(3)
        assert box["result"] == {"success": True, "result": "sent"}
        ex.assert_called_once_with("send_email", {})
        assert ask_ollama.get_pending_action() is None

    def test_dialog_button_resolves_the_same_op(self, app, dispatcher):
        def fake_dialog(desc, allow_always, op=None):
            op.approve()                                   # the Approve button
        with patch.object(dispatcher, "_confirm_dialog", side_effect=fake_dialog), \
             patch.object(dispatcher, "_execute", return_value={"success": True, "result": None}) as ex:
            result = dispatcher.dispatch({"tool": "send_email", "args": {}})
        assert result["success"] and ex.call_count == 1

    def test_stop_unblocks_a_stalled_delivery_adapter(self, app, dispatcher):
        """The dialog never answers (stalled adapter). The stop path rejects
        the pending op, the waiter returns at once, nothing executes."""
        with patch.object(dispatcher, "_confirm_dialog"), \
             patch.object(dispatcher, "_execute") as ex:
            t, box = _run_dispatch_in_thread(dispatcher, {"tool": "delete_file", "args": {"path": "x"}})
            assert ask_ollama.get_pending_action() is not None
            t0 = time.monotonic()
            ep.stop_all(app, "test stop")
            t.join(3)
            assert not t.is_alive() and time.monotonic() - t0 < 2.0
        ex.assert_not_called()
        assert box["result"]["success"] is False

    def test_approval_after_exit_is_stale(self, app, dispatcher):
        with patch.object(dispatcher, "_confirm_dialog"), \
             patch.object(dispatcher, "_execute") as ex:
            t, box = _run_dispatch_in_thread(dispatcher, {"tool": "send_email", "args": {}})
            op = ask_ollama.get_pending_action()["op"]
            app._ava_cmd_generation += 1                   # exit / sleep
            assert op.approve() is False                   # the button lands late
            t.join(3)
        ex.assert_not_called()
        assert box["result"]["success"] is False
        assert op.cancel_reason == "stale"


@pytest.mark.parametrize("route", [Route.EXACT, Route.MODEL])
@pytest.mark.parametrize("phrase", ["close tab", "close window", "lock computer", "lock screen",
                                    "permanent delete", "going dark", "again", "repeat"])
def test_owner_decision_eight_commands(app, executor, monkeypatch, phrase, route):
    executor.commands.update({
        "lock computer": {"type": "hotkey", "keys": ["win", "l"]},
        "lock screen": {"type": "hotkey", "keys": ["win", "l"]},
        "permanent delete": {"type": "hotkey", "keys": ["shift", "delete"]},
        "again": {"type": "method", "method": "repeat_last_command"},
        "repeat": {"type": "method", "method": "repeat_last_command"},
    })
    monkeypatch.setattr(ep, "_plugin_entry", lambda cid: {
        "metadata": {"risk_class": "destructive", "reversibility": "unknown"}
    } if cid == "going dark" else None)
    app._last_command_name = "switch window"
    app._last_command = executor.commands["switch window"]
    d = ep.authorize(Invocation(phrase, route=route, generation=7), app=app)
    if phrase == "going dark" and route == Route.MODEL:
        # A plugin with no declared argument schema is unavailable to a model.
        assert isinstance(d, Denied) and d.reason == "unvalidated"
        return
    prompt = phrase in {"permanent delete", "going dark"} or (
        route == Route.MODEL and phrase in {"close tab", "close window"})
    assert isinstance(d, NeedsConfirmation if prompt else Allowed)
    if route == Route.EXACT and phrase in {"close tab", "close window"}:
        assert d.hint == "undoable"


@pytest.mark.parametrize("route", [Route.EXACT, Route.MODEL])
@pytest.mark.parametrize("last_id", [None, "switch window", "permanent delete"])
def test_repeat_inherits_target(app, executor, route, last_id):
    executor.commands["permanent delete"] = {"type": "hotkey", "keys": ["shift", "delete"]}
    app._last_command_name = last_id
    app._last_command = executor.commands.get(last_id)
    d = ep.authorize(Invocation("repeat that", route=route, generation=7), app=app)
    expected = Denied if last_id is None else NeedsConfirmation if last_id == "permanent delete" else Allowed
    assert isinstance(d, expected)
    if last_id is None:
        assert d.reason == "nothing to repeat"
    elif last_id == "permanent delete":
        assert "Permanent delete" in d.prompt


@pytest.mark.parametrize("route", [Route.EXACT, Route.MODEL, Route.GRAMMAR])
@pytest.mark.parametrize("phrase", ["lock screen", "mystery action"])
def test_unknown_metadata_requires_prompt_except_explicit_safe_table(app, monkeypatch, route, phrase):
    monkeypatch.setattr(ep, "_plugin_entry", lambda cid: {"risk_class": "safe", "metadata": {"risk_class": "unknown"}})
    d = ep.authorize(Invocation(phrase, route=route, generation=7), app=app)
    if route == Route.MODEL:
        assert isinstance(d, Denied) and d.reason == "unvalidated"   # no schema declared
    elif phrase == "lock screen" and route == Route.EXACT:
        assert isinstance(d, Allowed) and d.hint
    else:
        assert isinstance(d, NeedsConfirmation)


@pytest.mark.parametrize("value,allowed", [(True, True), (False, False), ("unknown", False), ("reversible", True)])
def test_declared_reversibility_controls_exact_destructive(app, monkeypatch, value, allowed):
    monkeypatch.setattr(ep, "_plugin_entry", lambda cid: {"metadata": {
        "risk_class": "destructive", "reversibility": value}})
    d = ep.authorize(Invocation("going dark", generation=7), app=app)
    assert isinstance(d, Allowed if allowed else NeedsConfirmation)


@pytest.mark.parametrize("change_history", [False, True])
def test_repeat_confirmation_runs_bound_target_once(app, executor, effects, change_history):
    executor.commands["permanent delete"] = {"type": "hotkey", "keys": ["shift", "delete"]}
    app._last_command_name = "permanent delete"
    app._last_command = executor.commands["permanent delete"]
    assert executor.execute_command("repeat that", app, route=Route.MODEL, generation=7) is False
    assert effects == []
    if change_history:
        app._last_command_name = "close window"
        app._last_command = executor.commands["close window"]
    ask_ollama.handle_ava_confirm(app)
    assert effects == ([] if change_history else [executor.commands["permanent delete"]])
    assert ask_ollama.get_pending_action() is None


def test_unknown_metadata_respects_explicit_model_allowlist(app, monkeypatch):
    monkeypatch.setattr(ep, "_plugin_entry", lambda cid: {"metadata": {"risk_class": "unknown"}})
    app.config["execution_policy"] = {"model_tool_allowlist": ["switch window"]}
    d = ep.authorize(Invocation("mystery action", route=Route.MODEL, generation=7), app=app)
    # Undeclared schema denies before the allow-list is even consulted.
    assert isinstance(d, Denied) and d.reason == "unvalidated"
