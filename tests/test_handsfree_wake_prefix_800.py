"""Wake-prefix routing with a real session manager and isolated side effects."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.handsfree_wake_prefix import command_text, install, preview_text, split_wake_prefix
from samsara.session_modes import (
    CommandDispatchResult, DispatchOutcome, SessionMode, SessionModeManager,
    UtteranceSignals,
)


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))
BAD = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(1.2,))


def rig(*, phrase="jarvis", matched=True, state=None, timeout=5, buffer=True):
    now = [100.0]
    app = SimpleNamespace(
        config={"wake_word_config": {"phrase": phrase, "audio": {"wake_command_timeout": timeout}}},
        play_sound=Mock(),
    )
    dispatch = Mock(side_effect=lambda text: CommandDispatchResult(
        matched=matched, phrase=text if matched else None,
        **({"state": state} if state else {}),
    ))
    manager = SessionModeManager(
        abort_phrases=["cancel"], foreground_exe_resolver=lambda: "editor.exe",
        foreground_hwnd_resolver=lambda: 123,
        inject_fn=Mock(return_value="typed"), remove_chars_fn=Mock(),
        command_dispatch_fn=dispatch, agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=buffer, hands_free_command_probe_fn=Mock(return_value=None),
        clock=lambda: now[0], stop_fn=Mock(),
    )
    install(app, manager)
    manager.reset(SessionMode.DICTATE)
    return manager, app, dispatch, now


def keep_draft(manager):
    manager.dispatch_utterance("A synthetic unfinished thought", GOOD)
    return (manager.dictate_pending_buffer, manager._draft.revision,
            list(manager._dictate_pending_audio), manager._dictate_target_hwnd)


def assert_draft(manager, snapshot):
    assert (manager.dictate_pending_buffer, manager._draft.revision,
            manager._dictate_pending_audio, manager._dictate_target_hwnd) == snapshot
    manager._inject_fn.assert_not_called()


@pytest.mark.parametrize("phrase", ["jarvis", "computer please"])
@pytest.mark.parametrize("command", ["open notepad", "click 5", "ask hermes what time it is"])
def test_prefix_routes_argument_commands_and_preserves_draft(phrase, command):
    manager, app, dispatch, _ = rig(phrase=phrase)
    snapshot = keep_draft(manager)
    outcome = manager.dispatch_utterance(f"{phrase}, {command}", GOOD)
    assert outcome.kind == "command_executed"
    dispatch.assert_called_once_with(command)
    manager._hands_free_command_probe_fn.assert_called_once()  # dictation only
    assert_draft(manager, snapshot)
    assert manager.mode is SessionMode.DICTATE
    app.play_sound.assert_not_called()


@pytest.mark.parametrize("overlay", [True, False])
def test_click_with_or_without_overlay_stays_a_command(overlay):
    manager, _, dispatch, _ = rig(matched=overlay)
    snapshot = keep_draft(manager)
    # The registry/effect owns overlay availability, never the dictation probe.
    outcome = manager.dispatch_utterance("Jarvis, click 5", GOOD)
    assert outcome.kind == ("command_executed" if overlay else "command_miss")
    dispatch.assert_called_once_with("click 5")
    assert_draft(manager, snapshot)


@pytest.mark.parametrize("state,matched,kind", [
    (None, False, "command_miss"), ("failed", True, "command_failed"),
    ("rejected", True, "command_failed"), ("cancelled", True, "command_failed"),
])
def test_miss_or_failure_never_dictates_or_changes_draft(state, matched, kind):
    manager, _, dispatch, _ = rig(matched=matched, state=state)
    snapshot = keep_draft(manager)
    stack = len(manager._stack)
    outcome = manager.dispatch_utterance("Jarvis, unknown command", GOOD)
    assert outcome.kind == kind
    assert outcome.detail["heard"] == "unknown command"
    assert len(manager._stack) == stack
    dispatch.assert_called_once_with("unknown command")
    assert_draft(manager, snapshot)


@pytest.mark.parametrize("text", ["I told Jarvis about it", "Jarvisian ideas", "Jarvis's notes"])
def test_mentions_and_longer_words_remain_dictation(text):
    manager, _, dispatch, _ = rig()
    outcome = manager.dispatch_utterance(text, GOOD)
    assert outcome.kind == "dictate_staged"
    assert manager.dictate_pending_buffer == text
    dispatch.assert_not_called()


@pytest.mark.parametrize("phrase", ["jarvis", "hey computer"])
def test_wake_alone_acknowledges_then_consumes_exactly_one_command(phrase):
    manager, app, dispatch, _ = rig(phrase=phrase)
    snapshot = keep_draft(manager)
    ack = manager.dispatch_utterance(phrase, GOOD)
    assert ack.detail["wake_command_pending"]
    app.play_sound.assert_called_once_with("start")
    assert_draft(manager, snapshot)
    outcome = manager.dispatch_utterance("click 5", GOOD)
    assert outcome.kind == "command_executed"
    dispatch.assert_called_once_with("click 5")
    assert_draft(manager, snapshot)
    assert manager.dispatch_utterance("Ordinary speech again", GOOD).kind == "dictate_staged"
    assert dispatch.call_count == 1


def test_followup_window_uses_configured_timeout_and_expires():
    manager, _, dispatch, now = rig(timeout=2)
    manager.dispatch_utterance("Jarvis", GOOD)
    now[0] += 2
    outcome = manager.dispatch_utterance("This is prose", GOOD)
    assert outcome.kind == "dictate_staged"
    dispatch.assert_not_called()


def test_empty_capture_does_not_consume_the_window():
    manager, _, dispatch, _ = rig()
    manager.dispatch_utterance("Jarvis", GOOD)
    assert manager.dispatch_utterance("", GOOD).kind == "empty"
    manager.dispatch_utterance("open notepad", GOOD)
    dispatch.assert_called_once_with("open notepad")


def test_reset_and_changed_config_disarm_followup():
    manager, app, dispatch, _ = rig()
    manager.dispatch_utterance("Jarvis", GOOD)
    manager.reset(SessionMode.DICTATE)
    assert manager.dispatch_utterance("ordinary prose", GOOD).kind == "dictate_staged"
    manager.dispatch_utterance("Jarvis", GOOD)
    app.config["wake_word_config"]["phrase"] = "computer"
    assert manager.dispatch_utterance("ordinary prose", GOOD).kind == "dictate_staged"
    manager.dispatch_utterance("Computer, open notepad", GOOD)
    dispatch.assert_called_once_with("open notepad")


@pytest.mark.parametrize("text", ["Jarvis", "Jarvis, open notepad"])
def test_bad_audio_refuses_without_typing_or_arming(text):
    manager, app, dispatch, _ = rig()
    snapshot = keep_draft(manager)
    assert manager.dispatch_utterance(text, BAD).kind == "hands_free_command_refused"
    assert_draft(manager, snapshot)
    app.play_sound.assert_not_called()
    dispatch.assert_not_called()
    assert manager.dispatch_utterance("ordinary prose", GOOD).kind == "dictate_staged"


def test_bad_followup_consumes_window_without_dictation():
    manager, _, dispatch, _ = rig()
    snapshot = keep_draft(manager)
    manager.dispatch_utterance("Jarvis", GOOD)
    assert manager.dispatch_utterance("click 5", BAD).kind == "hands_free_command_refused"
    assert_draft(manager, snapshot)
    assert manager.dispatch_utterance("ordinary prose", GOOD).kind == "dictate_staged"
    dispatch.assert_not_called()


def test_naked_stop_keeps_its_priority_and_disarms_window():
    manager, _, dispatch, _ = rig()
    snapshot = keep_draft(manager)
    manager.dispatch_utterance("Jarvis", GOOD)
    assert manager.dispatch_utterance("halt", GOOD).kind == "stopped"
    assert_draft(manager, snapshot)
    assert manager.dispatch_utterance("ordinary prose", GOOD).kind == "dictate_staged"
    dispatch.assert_not_called()


def test_explicit_prefix_bypasses_pending_word_replacement():
    manager, _, dispatch, _ = rig()
    snapshot = keep_draft(manager)
    manager._pending_correction = {"word": "thought"}
    manager.dispatch_utterance("Jarvis, open notepad", GOOD)
    dispatch.assert_called_once_with("open notepad")
    assert manager._pending_correction == {"word": "thought"}
    assert_draft(manager, snapshot)


@pytest.mark.parametrize("mode", [SessionMode.COMMAND, SessionMode.AVA])
def test_other_lanes_keep_the_full_utterance(mode):
    manager, _, dispatch, _ = rig()
    manager.reset(mode)
    text = "Jarvis, open notepad"
    manager.dispatch_utterance(text, GOOD)
    if mode is SessionMode.COMMAND:
        dispatch.assert_called_once_with(text)
    else:
        dispatch.assert_not_called()
        manager._agent_dispatch_fn.assert_called_once()
        assert manager._agent_dispatch_fn.call_args.args[0] == text


def test_unbuffered_dictation_does_not_strip_prefix():
    manager, _, dispatch, _ = rig(buffer=False)
    manager._dictate_target_process = "editor.exe"  # legacy lane requires an acquired focus lock
    manager.dispatch_utterance("Jarvis, open notepad", GOOD)
    dispatch.assert_not_called()
    assert manager._inject_fn.call_args.args[0] == "Jarvis, open notepad"


@pytest.mark.parametrize("text,phrase,expected", [
    ("Um, JARVIS! Open Notepad.", "jarvis", "Open Notepad."),
    ("Hey Computer, ask Hermes What's at https://Example.test/a?", "hey computer",
     "ask Hermes What's at https://Example.test/a?"),
    ("Computer please: Open C:\\MyFolder\\Notes.txt", "computer please",
     "Open C:\\MyFolder\\Notes.txt"),
    ("Jarvis.", "jarvis", ""),
    ("Jarvisian ideas", "jarvis", None),
    ("I told Jarvis about it", "jarvis", None),
    ("Jarvis, click 5", "computer", None),
    ("Jarvis, click 5", "", None),
])
def test_normalized_prefix_preserves_raw_arguments(text, phrase, expected):
    assert split_wake_prefix(text, phrase) == expected


def test_downstream_miss_replacement_keeps_command_and_preview_separate():
    manager, app, _, _ = rig(matched=False)
    miss = manager.dispatch_utterance("Jarvis, unknown command", GOOD)
    replacement = DispatchOutcome(kind="command_suggestion_pending")
    app._resolve_command_miss = Mock(return_value=replacement)
    preview = preview_text(miss, "Jarvis, unknown command")
    resolved = app._resolve_command_miss(miss, command_text(miss, "Jarvis, unknown command"))
    app._resolve_command_miss.assert_called_once_with(miss, "unknown command")
    assert resolved is replacement
    assert preview == ""  # A replacement outcome cannot put command words in the draft.


@pytest.mark.parametrize("detail", ["", None, {}])
def test_ordinary_outcomes_keep_existing_resolver_early_return(detail):
    outcome = DispatchOutcome(kind="empty", detail=detail)
    assert command_text(outcome, "ordinary speech") == "ordinary speech"
    assert preview_text(outcome, "ordinary speech") == "ordinary speech"


def test_arming_one_manager_never_changes_another_manager_or_commit_word():
    from samsara import session_modes
    before = (session_modes.DICTATE_COMMIT_PHRASE, session_modes._DICTATE_COMMIT_HOMOPHONES)
    first, _, first_dispatch, _ = rig()
    second, _, second_dispatch, _ = rig()
    first.dispatch_utterance("Jarvis", GOOD)
    assert second.dispatch_utterance("ordinary speech", GOOD).kind == "dictate_staged"
    first.dispatch_utterance("open notepad", GOOD)
    first_dispatch.assert_called_once_with("open notepad")
    second_dispatch.assert_not_called()
    assert (session_modes.DICTATE_COMMIT_PHRASE, session_modes._DICTATE_COMMIT_HOMOPHONES) == before
