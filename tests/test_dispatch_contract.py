"""Dispatch contract: what process_text says happened, and what callers do.

Astra's 2026-09-12 review, section 1 items 7 and 8, ported as tests in the
inverted direction:

  PLUGIN_NONE_RETURN     {"effects": ["worker scheduled"], "result": ["ask ava", false]}
      -> a None-returning async handler is QUEUED and claimed, never a miss,
         and the AVA waterfall never re-interprets it.
  PAYLOAD_NORMALIZATION  {"remainder": "tell claude dont rename foopy to foopy"}
      -> the plugin receives the user's original words.

PLUGIN_MODULE_IDENTITY (item 6) lives in tests/test_plugin_commands.py.
"""
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara import plugin_commands as _plugin_commands  # noqa: E402
from samsara.command_registry import (  # noqa: E402
    DispatchResult,
    DispatchState,
    adapt_handler_return,
)
from samsara.commands import CommandExecutor  # noqa: E402
from samsara import session_modes as sm  # noqa: E402

ASTRA_UTTERANCE = 'ask ava Tell Claude: "Don\'t rename Foo.py to foo.py".'
ASTRA_PAYLOAD = 'Tell Claude: "Don\'t rename Foo.py to foo.py"'


@pytest.fixture
def commands_file(tmp_path):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"commands": {
        "copy": {"type": "hotkey", "keys": ["ctrl", "c"]},
        "broken builtin": {"type": "no_such_handler_type"},
    }}), encoding="utf-8")
    return path


def _app(**overrides):
    base = dict(
        config={"command_packs": {"ai": True}},
        command_matching_enabled=True,
        command_mode_active=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def allow_policy(monkeypatch):
    """Execution policy is enforced elsewhere (queue 02c); these tests pin the
    handler-result contract underneath it, so authorisation always allows."""
    from samsara import commands as commands_module

    policy = getattr(commands_module, "execution_policy", None)
    if policy is not None and hasattr(policy, "authorize"):
        monkeypatch.setattr(policy, "authorize", lambda *a, **k: policy.Allowed())


# ---------------------------------------------------------------------------
# The result type and the handler boundary adapter
# ---------------------------------------------------------------------------

class TestDispatchResult:
    def test_unpacks_as_the_legacy_pair_with_claimed_semantics(self):
        for state in DispatchState:
            result, was_command = DispatchResult(state, "phrase", "phrase")
            assert result == "phrase"
            assert was_command is (state is not DispatchState.MISS)

    def test_compares_equal_to_the_legacy_tuple(self):
        assert DispatchResult(DispatchState.COMPLETED, "copy", "copy") == ("copy", True)
        assert DispatchResult.miss("hello") == ("hello", False)

    def test_failed_is_still_claimed(self):
        failed = DispatchResult(DispatchState.FAILED, "copy", "copy")
        assert failed.claimed is True
        assert failed.succeeded is False

    @pytest.mark.parametrize("value,state", [
        (True, DispatchState.COMPLETED),
        (None, DispatchState.QUEUED),
        (False, DispatchState.MISS),
        ("anything truthy", DispatchState.COMPLETED),
        (DispatchState.CANCELLED, DispatchState.CANCELLED),
        (DispatchResult(DispatchState.FAILED), DispatchState.FAILED),
    ])
    def test_handler_return_adapter(self, value, state):
        assert adapt_handler_return(value) is state


# ---------------------------------------------------------------------------
# process_text states with synthetic registrations
# ---------------------------------------------------------------------------

class TestProcessTextStates:
    def _executor(self, commands_file, tmp_path):
        return CommandExecutor(commands_file, plugins_dir=tmp_path / "none")

    def test_none_returning_handler_is_queued_not_a_miss(self, commands_file, tmp_path, allow_policy):
        effects = []

        @_plugin_commands.command("schedule work")
        def _async(app, remainder):
            effects.append("worker scheduled")  # returns None, like handle_ask_ava

        executor = self._executor(commands_file, tmp_path)
        dispatch = executor.process_text("schedule work hello", _app(), force_commands=True)

        assert effects == ["worker scheduled"]
        assert dispatch.state is DispatchState.QUEUED
        assert tuple(dispatch) == ("schedule work", True)

    def test_raising_handler_is_failed_and_claimed(self, commands_file, tmp_path, allow_policy):
        @_plugin_commands.command("explode")
        def _boom(app, remainder):
            raise RuntimeError("device gone")

        executor = self._executor(commands_file, tmp_path)
        dispatch = executor.process_text("explode", _app(), force_commands=True)

        assert dispatch.state is DispatchState.FAILED
        assert dispatch.claimed is True
        assert "device gone" in dispatch.detail["error"]

    def test_declining_handler_gives_the_utterance_back(self, commands_file, tmp_path, allow_policy):
        @_plugin_commands.command("start strata test")
        def _decline(app, remainder):
            return False if remainder else True

        executor = self._executor(commands_file, tmp_path)
        dispatch = executor.process_text("start strata test please", _app(), force_commands=True)

        assert dispatch.state is DispatchState.MISS
        assert tuple(dispatch) == ("start strata test please", False)
        assert dispatch.detail == {"declined_by": "start strata test"}

    def test_failing_builtin_is_failed_not_dictation(self, commands_file, tmp_path, allow_policy):
        executor = self._executor(commands_file, tmp_path)
        dispatch = executor.process_text("broken builtin", _app(), force_commands=True)

        assert dispatch.state is DispatchState.FAILED
        assert tuple(dispatch) == ("broken builtin", True)

    def test_debounced_command_is_rejected_not_dictation(self, commands_file, tmp_path, allow_policy):
        @_plugin_commands.command("next track", debounce=60.0)
        def _next(app, remainder):
            return True

        executor = self._executor(commands_file, tmp_path)
        app = _app(command_mode_active=True)
        assert executor.process_text("next track", app, force_commands=True).state is DispatchState.COMPLETED

        dispatch = executor.process_text("next track", app, force_commands=True)
        assert dispatch.state is DispatchState.REJECTED
        assert tuple(dispatch) == ("next track", True)

    def test_no_match_is_a_miss_carrying_the_original_text(self, commands_file, tmp_path):
        executor = self._executor(commands_file, tmp_path)
        dispatch = executor.process_text("Hello there, Bob.", _app(), force_commands=True)
        assert dispatch.state is DispatchState.MISS
        assert tuple(dispatch) == ("Hello there, Bob.", False)

    def test_explicit_cancelled_state_is_honoured(self, commands_file, tmp_path, allow_policy):
        @_plugin_commands.command("stop that")
        def _stop(app, remainder):
            return DispatchState.CANCELLED

        executor = self._executor(commands_file, tmp_path)
        dispatch = executor.process_text("stop that", _app(), force_commands=True)
        assert dispatch.state is DispatchState.CANCELLED
        assert dispatch.claimed is True


# ---------------------------------------------------------------------------
# The REAL ask_ollama registrations (Astra's PLUGIN_NONE_RETURN, inverted)
# ---------------------------------------------------------------------------

@pytest.fixture
def real_ask_ollama(commands_file, tmp_path, monkeypatch, allow_policy):
    """Executor whose registry holds the real plugins/commands/ask_ollama.py
    registrations, installed through the production loader, with every
    external effect (speech, threads, network) replaced by a recorder."""
    module = _plugin_commands._import_plugin(ROOT / "plugins" / "commands" / "ask_ollama.py")
    assert module is sys.modules["plugins.commands.ask_ollama"]

    effects = []
    monkeypatch.setattr(module, "speak", lambda app, text: effects.append(("speak", text)))
    monkeypatch.setattr(module, "_check_ollama_available", lambda host, timeout=3: True)
    monkeypatch.setattr(module.cloud_llm, "is_enabled", lambda app: False)

    def _spawn(name, fn, **kwargs):
        effects.append(("spawn", name))
        fn()  # run the worker inline so its effects are observable
        return None

    monkeypatch.setattr(module.thread_registry, "spawn", _spawn)
    monkeypatch.setattr(module, "_check_teaching_intent",
                        lambda app, remainder: effects.append(("payload", remainder)) or True)
    monkeypatch.setattr(module, "_pending_action", None)
    executor = CommandExecutor(commands_file, plugins_dir=tmp_path / "none")
    # Rebuild against an app config that enables the "ai" pack, without
    # passing app to the constructor (which would start plugin services).
    executor._app = _app()
    executor.rebuild_matcher()
    return SimpleNamespace(module=module, executor=executor, effects=effects)


class TestRealRegistrations:
    def test_handler_is_the_canonical_module_function(self, real_ask_ollama):
        entry, _ = real_ask_ollama.executor._matcher.match("ask ava hello")
        assert entry.handler is real_ask_ollama.module.handle_ask_ava
        assert entry.handler.__module__ == "plugins.commands.ask_ollama"

    def test_ask_ava_schedules_once_and_is_queued(self, real_ask_ollama):
        dispatch = real_ask_ollama.executor.process_text(
            "ask ava hello", _app(), force_commands=True)

        assert dispatch.state is DispatchState.QUEUED
        assert tuple(dispatch) == ("hey ava", True)
        assert [e for e in real_ask_ollama.effects if e[0] == "spawn"] == [
            ("spawn", "ask_ollama._worker")]

    def test_ask_ava_receives_the_original_payload(self, real_ask_ollama):
        real_ask_ollama.executor.process_text(ASTRA_UTTERANCE, _app(), force_commands=True)
        assert ("payload", ASTRA_PAYLOAD) in real_ask_ollama.effects

    @pytest.mark.parametrize("utterance", ["yes", "Yeah.", "ava cancel"])
    def test_none_returning_confirm_and_cancel_are_queued(self, real_ask_ollama, utterance):
        dispatch = real_ask_ollama.executor.process_text(utterance, _app(), force_commands=True)
        assert dispatch.state is DispatchState.QUEUED
        assert dispatch.claimed is True

    def test_pending_state_staged_by_the_module_is_seen_by_canonical_import(self, real_ask_ollama):
        from plugins.commands import ask_ollama as canonical

        real_ask_ollama.module._pending_action = {
            "type": "schedule_probe", "expires": time.time() + 60}
        assert canonical.get_pending_action()["type"] == "schedule_probe"


# ---------------------------------------------------------------------------
# Consumers: the AVA waterfall, the session lanes and the chip
# ---------------------------------------------------------------------------

class TestWaterfallNeverReinterpretsAClaimedCommand:
    @pytest.mark.parametrize("state", [
        DispatchState.QUEUED, DispatchState.FAILED,
        DispatchState.REJECTED, DispatchState.CANCELLED,
    ])
    def test_stage_b_and_c_do_not_run(self, monkeypatch, state):
        from samsara import ava_command_session as acs

        calls = []
        monkeypatch.setattr(acs, "_match_action2_grammar", lambda u: calls.append("b"))
        monkeypatch.setattr(acs, "_stage_c_llm_fallback",
                            lambda *a, **k: calls.append("c") or False)
        monkeypatch.setattr(acs, "_build_shortlist", lambda *a, **k: [])
        monkeypatch.setattr(acs, "_set_thinking_indicator", lambda *a, **k: None)

        class _Executor:
            def process_text(self, text, app, force_commands=False, **kwargs):
                return DispatchResult(state, "hey ava", "hey ava")

        app = SimpleNamespace(config={}, command_executor=_Executor(),
                              _ava_cmd_generation=1, _ava_cmd_miss_count=3)
        acs._process_utterance(app, 1, "ask ava close the window")

        assert calls == []
        assert app._ava_cmd_miss_count == 0

    def test_a_real_miss_still_falls_through(self, monkeypatch):
        from samsara import ava_command_session as acs

        calls = []
        monkeypatch.setattr(acs, "_match_action2_grammar",
                            lambda u: calls.append("b") or ("focus", "notepad"))
        monkeypatch.setattr(acs, "_dispatch_action2", lambda *a, **k: calls.append("dispatch"))

        class _Executor:
            def process_text(self, text, app, force_commands=False, **kwargs):
                return DispatchResult.miss(text)

        app = SimpleNamespace(config={}, command_executor=_Executor(),
                              _ava_cmd_generation=1, _ava_cmd_miss_count=0)
        acs._process_utterance(app, 1, "focus notepad")
        assert calls == ["b", "dispatch"]


def _manager(dispatch_result):
    return sm.SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=lambda: "editor.exe",
        inject_fn=lambda *a, **k: True,
        remove_chars_fn=lambda n: None,
        command_dispatch_fn=lambda text: dispatch_result,
        agent_dispatch_fn=lambda *a: None,
    )


class TestSessionLanes:
    @pytest.mark.parametrize("state", ["failed", "rejected", "cancelled"])
    def test_command_lane_reports_unsuccessful_commands_as_command_failed(self, state):
        manager = _manager(sm.CommandDispatchResult(matched=True, phrase="copy", state=state))
        outcome = manager._dispatch_command("copy")
        assert outcome.kind == "command_failed"
        assert outcome.detail == {"phrase": "copy", "state": state}
        assert len(manager._stack) == 0

    @pytest.mark.parametrize("state", ["completed", "queued"])
    def test_command_lane_executed_carries_the_state(self, state):
        manager = _manager(sm.CommandDispatchResult(matched=True, phrase="copy", state=state))
        outcome = manager._dispatch_command("copy")
        assert outcome.kind == "command_executed"
        assert outcome.detail["state"] == state

    def test_legacy_dispatch_fn_without_state_is_completed(self):
        manager = _manager(sm.CommandDispatchResult(matched=True, phrase="copy"))
        assert manager._dispatch_command("copy").detail["state"] == "completed"

    def test_hands_free_failure_carries_the_state(self):
        manager = _manager(sm.CommandDispatchResult(matched=True, phrase="copy", state="failed"))
        match = sm.HandsFreeCommandMatch(dispatch_text="copy", phrase="copy")
        outcome = manager._dispatch_hands_free_command(match)
        assert outcome.kind == "hands_free_command_failed"
        assert outcome.detail["state"] == "failed"


class TestChipForStates:
    def test_completed_is_a_tick(self):
        assert sm.outcome_chip("command_executed", {"phrase": "copy", "state": "completed"}) == (
            f"{sm.CHIP_CHECK} copy", "success")

    @pytest.mark.parametrize("state", ["queued", "matched"])
    def test_queued_is_never_a_success_tick(self, state):
        assert sm.outcome_chip("command_executed", {"phrase": "hey ava", "state": state}) == (
            f"hey ava{sm.CHIP_ELLIPSIS}", "accent")

    def test_failed_is_an_error(self):
        assert sm.outcome_chip("command_failed", {"phrase": "copy", "state": "failed"}) == (
            f"{sm.CHIP_CROSS} copy", "error")

    def test_rejected_is_a_refusal(self):
        assert sm.outcome_chip("command_failed", {"phrase": "next track", "state": "rejected"}) == (
            "refused: next track", "warning")

    def test_cancelled(self):
        assert sm.outcome_chip("hands_free_command_failed",
                               {"phrase": "copy", "state": "cancelled"}) == (
            "cancelled: copy", "warning")


class TestDictationSessionDispatchFn:
    """dictation.py's _command_dispatch_fn: a claimed-but-failed command is a
    command (no miss count, no dictation), but not a success."""

    def _app(self, dispatch):
        import dictation

        history = []

        class _Executor:
            commands = {}

            def process_text(self, text, app, force_commands=False, **kwargs):
                return dispatch

            def find_command(self, text):
                return text

        app = SimpleNamespace(
            _session_mode_manager=None,
            command_executor=_Executor(),
            command_mode_active=True,
            _command_mode_miss_count=2,
            _current_utterance_duration_s=1.0,
            config={"command_mode": {"miss_limit": 5},
                    "wake_word_config": {"wake_abort_phrase": ["cancel"]}},
            add_to_history=lambda *a, **k: None,
            _log_history=lambda **k: history.append(k),
            _apply_formatting_tokens=lambda text: text,
            _paste_preserving_clipboard=lambda text: None,
            _ava_session_agent_dispatch_fn=lambda text, context: None,
            play_sound=lambda name: None,
            _update_mode_overlay=lambda mode: None,
            exit_command_mode=lambda: None,
            _dictate_commit_redecode=lambda *a, **k: None,
            _pop_pending_action_for_scratch=lambda *a, **k: None,
            _log_command_dispatch=lambda *a, **k: None,
        )
        return dictation, app, history

    def test_failed_command_is_claimed_but_not_success(self, monkeypatch):
        dispatch = DispatchResult(DispatchState.FAILED, "copy", "copy")
        dictation, app, history = self._app(dispatch)
        counted = []
        monkeypatch.setattr(dictation, "increment_command_count", counted.append)

        manager = dictation.DictationApp._ensure_session_mode_manager(app)
        result = manager._command_dispatch_fn("copy")

        assert result == sm.CommandDispatchResult(matched=True, phrase="copy", state="failed")
        assert app._command_mode_miss_count == 0
        assert counted == []
        assert history[-1]["status"] == "failed"
        assert not hasattr(app, "_last_command_name")

    def test_queued_command_counts_as_carried_out(self, monkeypatch):
        dispatch = DispatchResult(DispatchState.QUEUED, "hey ava", "hey ava")
        dictation, app, history = self._app(dispatch)
        counted = []
        monkeypatch.setattr(dictation, "increment_command_count", counted.append)

        manager = dictation.DictationApp._ensure_session_mode_manager(app)
        result = manager._command_dispatch_fn("ask ava hello")

        assert result.state == "queued"
        assert counted == ["hey ava"]
        assert history[-1]["status"] == "success"

    def test_held_for_confirmation_is_not_carried_out(self, monkeypatch):
        """Queue 58: queued-for-a-yes/no is not a run command."""
        dispatch = DispatchResult(DispatchState.QUEUED, "show windows", "show windows",
                                  {"awaiting_confirmation": True})
        dictation, app, history = self._app(dispatch)
        counted = []
        monkeypatch.setattr(dictation, "increment_command_count", counted.append)

        manager = dictation.DictationApp._ensure_session_mode_manager(app)
        result = manager._command_dispatch_fn("show windows")

        assert result == sm.CommandDispatchResult(matched=True, phrase="show windows", state="queued",
                                                  awaiting_confirmation=True)
        assert counted == []
        assert history[-1]["status"] == "awaiting_confirmation"
        assert not hasattr(app, "_last_command_name")

    def test_disabled_pack_miss_carries_the_pack(self, monkeypatch):
        dispatch = DispatchResult.miss("show windows", {"reason": "pack_disabled", "pack": "window-management"})
        dictation, app, history = self._app(dispatch)
        manager = dictation.DictationApp._ensure_session_mode_manager(app)
        result = manager._command_dispatch_fn("show windows")
        assert result == sm.CommandDispatchResult(matched=False, disabled_pack="window-management")
