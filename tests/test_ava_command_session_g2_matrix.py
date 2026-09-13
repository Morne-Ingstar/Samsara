"""Ava Front Door P1, acceptance gate G2: the spec's explicit test matrix,
implemented as named tests. This file covers the rows not already covered
by the migrated 2026-07-19-guard suites in this pass:

  - confirmation binding + expiry + unrelated-turn immunity
  - scratch-that unified-stack semantics
  - surgical latch-drop w/ key pass-through
  - waterfall miss => single fallback => miss feedback

("mode enter/exit churn incl. ghost taps" and "stale-generation async
drops" are covered by tests/test_ava_command_session_ghost_tap.py and
tests/test_ava_command_session_authoritative_exit.py respectively;
"migration (keys, notice, tutorial refs)" by
tests/test_ava_command_session_migration.py.)

Two matrix rows are explicitly OUT OF SCOPE for this P1 pass -- both are
Agora orchestration-verb behavior, gated to P2 by the spec's "Phasing"
section ("P1 consolidation only... no orchestration semantics anywhere
user-visible"). There is no Agora token/listener surface anywhere in D3
today for these to exercise, so they are recorded as explicit skips
rather than silently omitted:
  - "no-token => zero orchestration surface"
  - "token-present-listener-dead => hidden verbs, no error UI"
"""
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
from pynput.keyboard import Key

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation
from samsara import ava_command_session
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals, CommandDispatchResult
from plugins.commands import ask_ollama

GOOD_SIGNALS = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))


@pytest.fixture(autouse=True)
def _reset_pending_action():
    """ask_ollama._pending_action is the single unified confirmation slot,
    shared by every door -- reset it around every test in this file so
    tests can't leak staged actions into each other."""
    ask_ollama.clear_pending_action()
    yield
    ask_ollama.clear_pending_action()


# =============================================================================
# Confirmation binding: single global _pending_action slot, 30s expiry,
# single-slot-replaced-by-new-staging, unrelated-turn immunity.
# =============================================================================

class TestConfirmationBinding:
    def test_stage_b_unsafe_verb_stages_confirmation_with_30s_expiry(self, monkeypatch):
        app = Mock()
        speak_mock = Mock()
        monkeypatch.setattr(ava_command_session, '_speak', speak_mock)
        before = time.time()
        ava_command_session._dispatch_action2(app, "close", "notepad", 0, {})
        after = time.time()

        action = ask_ollama.get_pending_action()
        assert action is not None
        assert action["type"] == "action2"
        assert action["verb"] == "close"
        assert action["argument"] == "notepad"
        assert before + 30 <= action["expires"] <= after + 30
        speak_mock.assert_called_once()
        assert "yes to confirm" in speak_mock.call_args.args[1]

    def test_safe_verb_executes_immediately_no_staging(self, monkeypatch):
        app = Mock()
        execute_mock = Mock()
        monkeypatch.setattr(ask_ollama, '_execute_action2', execute_mock)
        ava_command_session._dispatch_action2(app, "open", "notepad", 0, {})
        assert ask_ollama.get_pending_action() is None
        # Route + generation ride along now (execution policy); the verb and
        # argument are still exactly what was matched.
        execute_mock.assert_called_once()
        assert execute_mock.call_args.args == (app, "open", "notepad")
        assert execute_mock.call_args.kwargs["generation"] == 0

    def test_pending_action_expires_after_ttl(self):
        with ask_ollama._pending_action_lock:
            ask_ollama._pending_action = {
                "type": "action2", "verb": "close", "argument": "notepad",
                "expires": time.time() - 1,  # already expired
            }
        assert ask_ollama.get_pending_action() is None

    def test_single_slot_replaced_by_new_staging_not_stacked(self, monkeypatch):
        app = Mock()
        monkeypatch.setattr(ava_command_session, '_speak', Mock())
        ava_command_session._dispatch_action2(app, "close", "notepad", 0, {})
        first = ask_ollama.get_pending_action()
        assert first["argument"] == "notepad"

        ava_command_session._dispatch_action2(app, "close", "chrome", 0, {})
        second = ask_ollama.get_pending_action()
        assert second["argument"] == "chrome", \
            "staging a new action must replace the slot, not stack alongside it"

    def test_unrelated_turn_does_not_touch_a_staged_action(self, monkeypatch):
        """A staged confirmation must survive an unrelated utterance
        passing through the waterfall -- only 'yes'/'ava cancel'/scratch
        that may consume it."""
        with ask_ollama._pending_action_lock:
            ask_ollama._pending_action = {
                "type": "action2", "verb": "close", "argument": "notepad",
                "expires": time.time() + 30,
            }

        app = Mock()
        app._ava_cmd_generation = 0
        app._ava_cmd_miss_count = 0
        app.config = {'ava_command_session': {}}
        app.command_executor = Mock()
        # Unrelated utterance resolves as a stage (a) hit (some OTHER command).
        app.command_executor.process_text = Mock(return_value=("screenshot", True))

        ava_command_session._process_utterance(app, 0, "take a screenshot")

        still_pending = ask_ollama.get_pending_action()
        assert still_pending is not None
        assert still_pending["argument"] == "notepad"


# =============================================================================
# Unified scratch-that: staged action first, else the per-session dictation
# commit stack; never both.
# =============================================================================

class TestUnifiedScratchThat:
    def _make_app(self, session_mode_manager=None):
        app = types.SimpleNamespace()
        app.play_sound = Mock()
        app._session_mode_manager = session_mode_manager
        app._pop_pending_action_for_scratch = types.MethodType(
            dictation.DictationApp._pop_pending_action_for_scratch, app,
        )
        app._handle_unified_scratch_that = types.MethodType(
            dictation.DictationApp._handle_unified_scratch_that, app,
        )
        return app

    def test_pops_pending_action_when_present_without_touching_dictation_stack(self):
        with ask_ollama._pending_action_lock:
            ask_ollama._pending_action = {
                "type": "action2", "verb": "close", "argument": "notepad",
                "expires": time.time() + 30,
            }
        manager = types.SimpleNamespace(_do_scratch_that=Mock(return_value=True))
        app = self._make_app(session_mode_manager=manager)

        app._handle_unified_scratch_that()

        assert ask_ollama.get_pending_action() is None
        app.play_sound.assert_called_once_with('scratch_success')
        manager._do_scratch_that.assert_not_called()

    def test_falls_to_dictation_stack_when_no_pending_action(self):
        manager = types.SimpleNamespace(_do_scratch_that=Mock(return_value=True))
        app = self._make_app(session_mode_manager=manager)

        app._handle_unified_scratch_that()

        manager._do_scratch_that.assert_called_once()
        app.play_sound.assert_called_once_with('scratch_success')

    def test_refuses_when_dictation_stack_pop_fails(self):
        manager = types.SimpleNamespace(_do_scratch_that=Mock(return_value=False))
        app = self._make_app(session_mode_manager=manager)

        app._handle_unified_scratch_that()

        app.play_sound.assert_called_once_with('scratch_refuse')

    def test_refuses_when_neither_pending_action_nor_session_manager(self):
        app = self._make_app(session_mode_manager=None)

        app._handle_unified_scratch_that()

        app.play_sound.assert_called_once_with('scratch_refuse')


# =============================================================================
# SessionModeManager.dispatch_utterance's OWN scratch branch (the D2
# hands-free session's caller of pending_action_scratch_fn -- distinct
# from D3's own _handle_unified_scratch_that tested above) must honor the
# same pending-action-first semantics without session_modes.py importing
# anything about what a "pending action" is (pure orchestration boundary).
# =============================================================================

class TestSessionModeManagerHonorsPendingActionScratchFn:
    def _make_manager(self, pending_action_scratch_fn):
        mocks = {
            "foreground": Mock(return_value="notepad.exe"),
            "inject": Mock(),
            "remove_chars": Mock(),
            "command_dispatch": Mock(return_value=CommandDispatchResult(matched=False)),
            "agent_dispatch": Mock(),
            "on_scratch_result": Mock(),
        }
        mgr = SessionModeManager(
            abort_phrases=["cancel", "abort"],
            foreground_exe_resolver=mocks["foreground"],
            inject_fn=mocks["inject"],
            remove_chars_fn=mocks["remove_chars"],
            command_dispatch_fn=mocks["command_dispatch"],
            agent_dispatch_fn=mocks["agent_dispatch"],
            on_scratch_result=mocks["on_scratch_result"],
            pending_action_scratch_fn=pending_action_scratch_fn,
            clock=lambda: 1000.0,
        )
        return mgr, mocks

    def test_pending_action_present_short_circuits_the_dictation_stack(self):
        do_scratch_that_calls = []
        mgr, mocks = self._make_manager(pending_action_scratch_fn=lambda: True)
        mgr._do_scratch_that = Mock(side_effect=lambda: do_scratch_that_calls.append(1) or True)

        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)

        assert outcome.kind == "scratch_success"
        assert do_scratch_that_calls == [], \
            "a pending action being present must short-circuit BEFORE the dictation stack pop"
        mocks["on_scratch_result"].assert_called_once_with(True)

    def test_pending_action_absent_falls_through_to_dictation_stack(self):
        mgr, mocks = self._make_manager(pending_action_scratch_fn=lambda: None)
        mgr._do_scratch_that = Mock(return_value=True)

        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)

        assert outcome.kind == "scratch_success"
        mgr._do_scratch_that.assert_called_once()

    def test_no_callable_wired_preserves_old_behavior(self):
        """Backward compatibility: omitting pending_action_scratch_fn
        entirely (its default is None) must behave exactly as it did
        before this parameter existed."""
        mgr, mocks = self._make_manager(pending_action_scratch_fn=None)
        mgr._do_scratch_that = Mock(return_value=False)

        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)

        assert outcome.kind == "scratch_refuse"
        mgr._do_scratch_that.assert_called_once()


# =============================================================================
# Surgical Alt guard: any non-session key while latched drops the latch
# immediately AND the key event still falls through to every other handler
# (no suppression -- this listener was never constructed with suppress=True).
# =============================================================================

class FakeWakeConsumer:
    def __init__(self):
        self._running = False

    def start(self):
        self._running = True

    def stop(self):
        self._running = False
        return []


class TestSurgicalAltGuardPassThrough:
    def _make_app(self, monkeypatch, ava_active=True):
        app = types.SimpleNamespace()
        app.config = {
            'command_mode': {'enabled': False, 'enter_debounce_ms': 0},
            'ava_command_session': {'enabled': True, 'key': 'left_alt'},
        }
        app.command_mode_active = False
        app.ava_mode_active = False
        app.ava_command_session_active = ava_active
        app._ava_mode_lock = threading.Lock()
        app._ava_mode_key_held = False
        app._ava_cmd_key_held = False
        app._ava_cmd_key_press_time = 0.0
        app._ava_cmd_miss_count = 0
        app._ava_cmd_generation = 0
        app._ava_cmd_ready = threading.Event()
        app._ava_cmd_ready.set()
        app._ava_cmd_mode_lock = threading.Lock()
        app._reset_ava_cmd_inactivity_timer = lambda timeout_s: None
        app._cancel_ava_cmd_inactivity_timer = lambda: None
        app._wake_consumer = FakeWakeConsumer()
        app._wake_consumer_reasons = set()
        app._wake_consumer_lock = threading.Lock()
        app._ensure_wake_consumer = types.MethodType(dictation.DictationApp._ensure_wake_consumer, app)
        app._release_wake_consumer = types.MethodType(dictation.DictationApp._release_wake_consumer, app)
        app.play_sound = Mock()
        monkeypatch.setattr(dictation.thread_registry, 'spawn', lambda *a, **k: None)
        app._do_enter_ava_command_session = lambda: None
        app.enter_ava_command_session = types.MethodType(dictation.DictationApp.enter_ava_command_session, app)
        app.exit_ava_command_session = types.MethodType(dictation.DictationApp.exit_ava_command_session, app)
        app.enter_ava_mode = types.MethodType(dictation.DictationApp.enter_ava_mode, app)
        app._do_enter_ava_mode = types.MethodType(dictation.DictationApp._do_enter_ava_mode, app)
        app._check_command_mode_key = types.MethodType(dictation.DictationApp._check_command_mode_key, app)
        return app

    def test_non_session_key_drops_the_latch_immediately(self, monkeypatch):
        app = self._make_app(monkeypatch, ava_active=True)
        app._check_command_mode_key(Key.esc, pressed=True)
        assert app.ava_command_session_active is False

    def test_non_session_key_still_falls_through_to_its_own_handler(self, monkeypatch):
        """The guard does not `return` -- the SAME key press that dropped
        the latch must still reach Ava mode's own press handling below it,
        proving no suppression/short-circuit happens."""
        app = self._make_app(monkeypatch, ava_active=True)
        app._check_command_mode_key(Key.alt_r, pressed=True)  # ava_mode_key default
        assert app.ava_command_session_active is False, "latch dropped by the guard"
        assert app.ava_mode_active is True, \
            "the SAME key press must still activate Ava mode -- guard is non-suppressing"

    def test_own_session_key_does_not_trigger_the_guard(self, monkeypatch):
        app = self._make_app(monkeypatch, ava_active=True)
        app._check_command_mode_key(Key.alt_l, pressed=True)  # left_alt == the session's own key
        assert app.ava_command_session_active is True, \
            "the session's own toggle key must never trip its own latch-drop guard"

    def test_guard_is_a_noop_when_session_not_active(self, monkeypatch):
        app = self._make_app(monkeypatch, ava_active=False)
        app.exit_ava_command_session = Mock()
        app._check_command_mode_key(Key.esc, pressed=True)
        app.exit_ava_command_session.assert_not_called()


# =============================================================================
# Waterfall miss: exactly ONE LLM fallback call, a conversational (non-
# command) reply is spoken as a MISS -- never as free-form chat.
# =============================================================================

class TestWaterfallMissSingleFallbackThenMissFeedback:
    def _make_app(self):
        app = Mock()
        app._ava_cmd_generation = 0
        app._ava_cmd_miss_count = 0
        app.ava_command_session_active = True
        app.command_executor = Mock()
        app.command_executor.process_text = Mock(return_value=(None, False))  # stage (a) miss
        app.audio_coordinator = Mock()
        app.exit_ava_command_session = Mock()
        app.play_sound = Mock()
        app.config = {'ava_command_session': {}}
        return app

    def _spoken_texts(self, app):
        return [c.args[0] for c in app.audio_coordinator.speak.call_args_list]

    def test_stage_c_called_exactly_once_on_a_double_miss(self, monkeypatch):
        app = self._make_app()
        monkeypatch.setattr(ava_command_session, '_match_action2_grammar', lambda u: None)
        monkeypatch.setattr(ava_command_session, '_build_shortlist', lambda app, u, cfg: [])
        ask_mock = Mock(return_value="just chatting, no command here")
        monkeypatch.setattr(ask_ollama, 'ask_ollama', ask_mock)
        monkeypatch.setattr(
            ask_ollama, '_parse_structured_response', lambda resp: {'type': 'conversation'},
        )
        handle_response_mock = Mock()
        monkeypatch.setattr(ask_ollama, 'handle_response', handle_response_mock)

        ava_command_session._process_utterance(app, 0, "tell me something")

        ask_mock.assert_called_once()
        handle_response_mock.assert_not_called()

    def test_conversational_reply_is_spoken_as_miss_not_as_chat(self, monkeypatch):
        app = self._make_app()
        monkeypatch.setattr(ava_command_session, '_match_action2_grammar', lambda u: None)
        monkeypatch.setattr(ava_command_session, '_build_shortlist', lambda app, u, cfg: [])
        conversational_reply = "Sure, I'd love to chat about that!"
        monkeypatch.setattr(ask_ollama, 'ask_ollama', lambda *a, **k: conversational_reply)
        monkeypatch.setattr(
            ask_ollama, '_parse_structured_response', lambda resp: {'type': 'conversation'},
        )
        monkeypatch.setattr(ask_ollama, 'handle_response', Mock())

        ava_command_session._process_utterance(app, 0, "tell me something")

        spoken = self._spoken_texts(app)
        assert conversational_reply not in spoken, \
            "a conversational LLM reply must never be spoken verbatim -- D3 stays command-first"
        assert spoken == ["I didn't catch a command in that."]
        assert app._ava_cmd_miss_count == 1

# P2 scope (Agora orchestration verbs/callback) placeholder tests removed
# here -- see docs/reviews/test_suite_audit.md NEVER RUNS for the reason:
# D3 has no Agora token/orchestration surface at all in P1, so there was no
# production code for them to ever exercise, and building that surface to
# give them something to test is out of scope for a test-only fix.
