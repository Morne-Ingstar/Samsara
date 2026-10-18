"""Prompt 271: wake commands use the shared command outcome path."""
from types import SimpleNamespace
from unittest.mock import Mock

from dictation import DictationApp
from samsara.session_modes import (
    CommandDispatchResult, CommandSuggestionCandidate, DispatchOutcome,
    SessionModeManager,
)


def _manager(dispatch, suggest=None):
    return SessionModeManager(
        abort_phrases=[], foreground_exe_resolver=lambda: None, inject_fn=Mock(),
        remove_chars_fn=Mock(), command_dispatch_fn=dispatch, agent_dispatch_fn=Mock(),
        command_suggestion_fn=suggest, clock=lambda: 1000.0,
    )


def _app(manager):
    handled = Mock()
    app = SimpleNamespace(
        app_state="asleep", wake_word_triggered=True,
        _ensure_session_mode_manager=Mock(return_value=manager),
        _handle_session_dispatch_outcome=handled,
        _indicator_success_and_reset=Mock(), _indicator_reset=Mock(),
        _start_wake_timeout=Mock(),
        _resolve_command_miss=lambda outcome, text: outcome,
    )
    return app, handled


def test_wake_miss_reaches_shared_chip_outcome_with_heard_text():
    app, handled = _app(_manager(Mock(return_value=CommandDispatchResult(False))))
    DictationApp._process_wake_command(app, "open chorm")
    outcome, text = handled.call_args.args
    assert outcome.kind == "command_miss"
    assert outcome.detail["heard"] == "open chorm"
    assert text == "open chorm"
    app._indicator_reset.assert_called_once()


def test_wake_suggestion_keeps_window_for_yes_then_runs_canonical():
    dispatch = Mock(side_effect=[CommandDispatchResult(False),
                                 CommandDispatchResult(True, "switch window")])
    app, handled = _app(_manager(dispatch, lambda _: CommandSuggestionCandidate("switch window")))
    DictationApp._process_wake_command(app, "switch windy")
    assert handled.call_args.args[0].kind == "command_suggestion_pending"
    assert app.wake_word_triggered is True
    app._start_wake_timeout.assert_called_once_with(8.0)
    DictationApp._process_wake_command(app, "yes")
    assert handled.call_args.args[0].kind == "command_executed"
    assert dispatch.call_args_list[1].args == ("switch window",)


def test_wake_no_cancels_and_other_utterance_dispatches_normally():
    dispatch = Mock(return_value=CommandDispatchResult(False))
    app, handled = _app(_manager(
        dispatch, Mock(side_effect=[CommandSuggestionCandidate("switch window"), None])))
    DictationApp._process_wake_command(app, "switch windy")
    DictationApp._process_wake_command(app, "no")
    assert handled.call_args.args[0].kind == "command_suggestion_declined"
    assert dispatch.call_args_list == [(("switch windy",), {})]
    # A second wake command after an offer is ordinary dispatch, not swallowed.
    app.wake_word_triggered = True
    DictationApp._process_wake_command(app, "open calendar")
    assert dispatch.call_args_list[-1].args == ("open calendar",)


def test_wake_never_offers_a_destructive_candidate():
    dispatch = Mock(return_value=CommandDispatchResult(False))
    app, handled = _app(_manager(
        dispatch, lambda _: CommandSuggestionCandidate("close window", "destructive")))
    DictationApp._process_wake_command(app, "clothes window")
    assert handled.call_args.args[0].kind == "command_miss"
    assert app.wake_word_triggered is False
