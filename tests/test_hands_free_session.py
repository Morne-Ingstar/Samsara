"""Focused contract tests for the combined toggle hands-free lane."""
from unittest.mock import Mock, patch

from dictation import DictationApp
from samsara.session_modes import (
    CommandDispatchResult,
    HandsFreeCommandMatch,
    PendingTextPolicy,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
)


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))
BAD = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(1.2,))


def _build(*, probe=None, command_matches=True):
    state = {"exe": "editor.exe", "hwnd": 101}
    events = []

    def inject(text):
        events.append(("paste", text))
        return text

    def dispatch(text):
        events.append(("command", text))
        return CommandDispatchResult(
            matched=command_matches,
            phrase=text if command_matches else None,
        )

    manager = SessionModeManager(
        abort_phrases=["cancel", "abort", "stop listening"],
        foreground_exe_resolver=lambda: state["exe"],
        foreground_hwnd_resolver=lambda: state["hwnd"],
        inject_fn=inject,
        remove_chars_fn=Mock(),
        command_dispatch_fn=dispatch,
        agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True,
        hands_free_command_probe_fn=probe,
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    return manager, state, events


def _match(text, *, policy):
    return HandsFreeCommandMatch(
        dispatch_text=text,
        phrase=text,
        pending_policy=policy,
    )


def test_toggle_session_can_start_directly_in_combined_lane():
    manager, _state, _events = _build()
    assert manager.mode is SessionMode.DICTATE


def test_unmatched_utterance_remains_buffered_dictation():
    manager, _state, events = _build(probe=lambda _text: None)

    outcome = manager.dispatch_utterance(
        "I need to scroll down through the document", GOOD,
    )

    assert outcome.kind == "dictate_staged"
    assert manager.dictate_pending_buffer == "I need to scroll down through the document"
    assert events == []


def test_preserve_command_executes_without_pasting_or_leaving_lane():
    def probe(text):
        if text.lower() == "scroll down":
            return _match("scroll down", policy=PendingTextPolicy.PRESERVE)
        return None

    manager, _state, events = _build(probe=probe)
    manager.dispatch_utterance("unfinished thought", GOOD)

    outcome = manager.dispatch_utterance("scroll down", GOOD)

    assert outcome.kind == "hands_free_command_executed"
    assert manager.mode is SessionMode.DICTATE
    assert manager.dictate_pending_buffer == "unfinished thought"
    assert events == [("command", "scroll down")]


def test_focus_changing_command_commits_before_it_executes():
    def probe(text):
        if text.lower() == "next field":
            return _match("next field", policy=PendingTextPolicy.COMMIT)
        return None

    manager, _state, events = _build(probe=probe)
    manager.dispatch_utterance("subject text", GOOD)

    outcome = manager.dispatch_utterance("next field", GOOD)

    assert outcome.kind == "hands_free_command_executed"
    assert manager.mode is SessionMode.DICTATE
    assert manager.dictate_pending_buffer == ""
    assert events == [
        ("paste", "subject text"),
        ("command", "next field"),
    ]


def test_focus_mismatch_blocks_navigation_and_retains_pending_text():
    def probe(text):
        if text.lower() == "other window":
            return _match("other window", policy=PendingTextPolicy.COMMIT)
        return None

    manager, state, events = _build(probe=probe)
    manager.dispatch_utterance("do not lose this", GOOD)
    state["hwnd"] = 202

    outcome = manager.dispatch_utterance("other window", GOOD)

    assert outcome.kind == "hands_free_command_blocked"
    assert outcome.detail["commit_outcome"] == "dictate_commit_blocked_focus_lock"
    assert manager.dictate_pending_buffer == "do not lose this"
    assert events == []


def test_distrusted_reserved_command_is_neither_executed_nor_dictated():
    def probe(text):
        if text.lower() == "submit":
            return _match("submit", policy=PendingTextPolicy.COMMIT)
        return None

    manager, _state, events = _build(probe=probe)
    manager.dispatch_utterance("message body", GOOD)

    outcome = manager.dispatch_utterance("submit", BAD)

    assert outcome.kind == "hands_free_command_refused"
    assert manager.dictate_pending_buffer == "message body"
    assert events == []


def test_literal_escape_dictates_a_reserved_whole_utterance():
    def probe(text):
        if text.lower() == "scroll down":
            return _match("scroll down", policy=PendingTextPolicy.PRESERVE)
        return None

    manager, _state, events = _build(probe=probe)

    outcome = manager.dispatch_utterance("literal scroll down", GOOD)

    assert outcome.kind == "dictate_staged"
    assert manager.dictate_pending_buffer == "scroll down"
    assert events == []


def test_reserved_command_execution_failure_does_not_become_dictation():
    def probe(_text):
        return _match("scroll down", policy=PendingTextPolicy.PRESERVE)

    manager, _state, events = _build(probe=probe, command_matches=False)

    outcome = manager.dispatch_utterance("scroll down", GOOD)

    assert outcome.kind == "hands_free_command_failed"
    assert manager.dictate_pending_buffer == ""
    assert events == [("command", "scroll down")]


def _probe_app(canonical_by_text):
    app = DictationApp.__new__(DictationApp)
    app.command_executor = Mock()
    app.command_executor.find_command.side_effect = canonical_by_text.get
    return app


def test_runtime_probe_reserves_only_exact_scroll_utterance():
    app = _probe_app({"scroll down": "scroll down"})

    exact = app._probe_hands_free_command("scroll down")
    sentence = app._probe_hands_free_command(
        "I need to scroll down through the document",
    )

    assert exact.pending_policy is PendingTextPolicy.PRESERVE
    assert exact.dispatch_text == "scroll down"
    assert sentence is None


def test_runtime_probe_marks_focus_navigation_commit_first():
    app = _probe_app({"focus claude": "focus"})

    match = app._probe_hands_free_command("focus Claude")

    assert match.pending_policy is PendingTextPolicy.COMMIT
    assert match.dispatch_text == "focus claude"


def test_runtime_probe_accepts_bare_number_only_while_overlay_active():
    app = _probe_app({"click twenty six": "click"})

    with patch("plugins.commands.show_numbers.is_overlay_active", return_value=True):
        match = app._probe_hands_free_command("twenty six")

    assert match.dispatch_text == "click twenty six"
    assert match.pending_policy is PendingTextPolicy.COMMIT


def test_global_abort_still_exits_before_any_hands_free_probe():
    probe = Mock()
    manager, _state, _events = _build(probe=probe)

    outcome = manager.dispatch_utterance("stop listening", BAD)

    assert outcome.kind == "abort"
    probe.assert_not_called()
