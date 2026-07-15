"""Focused regressions for sustained toggle-session DICTATE capture.

The formatting-pipeline tests below (buffered-DICTATE commit onward) guard
the actual bug this file is named for: unified toggle-session DICTATE used
to stage raw Whisper chunks and paste them untouched on "end", bypassing
process_transcription/clean_text/smart_correct/formatting-tokens entirely.
The fix reuses that SAME pipeline (see dictation.py's _inject_fn, wired as
SessionModeManager's inject_fn) exactly once over the complete accumulated
text at commit -- never per natural-pause chunk.
"""

from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.session_modes import SessionMode, UtteranceSignals


def _app(*, mode=SessionMode.DICTATE, command_mode_active=True, app_state="asleep"):
    return SimpleNamespace(
        command_mode_active=command_mode_active,
        config={"command_mode": {"mode": "toggle"}},
        _session_mode_manager=SimpleNamespace(mode=mode),
        app_state=app_state,
        _touch_session_activity=Mock(),
    )


def test_hard_cap_does_not_discard_toggle_dictate():
    assert WakeConsumer._hard_cap_applies(_app()) is False


def test_hard_cap_still_protects_toggle_command():
    assert WakeConsumer._hard_cap_applies(_app(mode=SessionMode.COMMAND)) is True


def test_sustained_speech_refreshes_inactivity_timer_at_throttled_intervals():
    app = _app()
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._last_toggle_activity_touch = 0.0

    consumer._touch_toggle_speech_activity(10.0)
    consumer._touch_toggle_speech_activity(10.5)
    consumer._touch_toggle_speech_activity(11.1)

    assert app._touch_session_activity.call_count == 2


def test_toggle_utterance_fifo_drains_every_chunk_in_capture_order():
    app = _app()
    app._handle_command_mode_utterance = Mock()
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._toggle_queue = __import__("collections").deque()
    consumer._toggle_queue_lock = __import__("threading").Lock()
    consumer._toggle_worker_active = False

    spawned = []
    with patch(
        "samsara.audio_engine.wake_consumer.thread_registry.spawn",
        side_effect=lambda _name, target, **_kwargs: spawned.append(target),
    ):
        consumer._enqueue_toggle_utterance(["first"])
        consumer._enqueue_toggle_utterance(["second"])

    assert len(spawned) == 1
    spawned[0]()
    assert app._handle_command_mode_utterance.call_args_list == [
        call(["first"], 16000),
        call(["second"], 16000),
    ]
    assert app._touch_session_activity.call_count == 2


def _buffered_dictation_app(config_overrides=None):
    """A DictationApp stand-in wired for buffered toggle-session DICTATE,
    with every side-effecting call mocked so tests can assert on it.

    Config includes every key the reused formatting pipeline
    (process_transcription / clean_text / smart_correct /
    _apply_formatting_tokens, all called from dictation.py's _inject_fn)
    actually reads. add_trailing_space in particular is accessed via bare
    subscript there -- matching every other dictation finalize path
    (_output_dictation, the continuous-mode path) -- so it must always be
    present or the pipeline raises, exactly as it would against a real
    app.config that never omits this key.
    """
    from dictation import DictationApp

    app = DictationApp.__new__(DictationApp)
    app._session_mode_manager = None
    app.config = {
        "wake_word_config": {"wake_abort_phrase": ["cancel", "abort"]},
        "formatting_tokens": {"enabled": True},
        "add_trailing_space": False,
        "auto_capitalize": True,
        "format_numbers": True,
        "cleanup_mode": "clean",
        "smart_corrections": {"enabled": False, "modes": {"wake": True}},
        "enable_case_formatters": False,
    }
    if config_overrides:
        app.config.update(config_overrides)
    app._paste_preserving_clipboard = Mock(return_value=True)
    app.add_to_history = Mock()
    app._log_history = Mock()
    app._notify_main_window = Mock()
    app.play_sound = Mock()
    app._update_mode_overlay = Mock()
    app.exit_command_mode = Mock()
    return app


def _dictate_and_end(app, chunks):
    """Dispatch each chunk into DICTATE (natural pauses -- staged, nothing
    pasted), then commit with the sole-word "end". Returns
    (session_mode_manager, final_dispatch_outcome)."""
    signals = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))
    with patch("dictation._get_foreground_exe_lower", return_value="codex.exe"), \
         patch("dictation._get_foreground_hwnd", return_value=4242):
        manager = app._ensure_session_mode_manager()
        manager.force_mode(SessionMode.DICTATE)
        for chunk in chunks:
            outcome = manager.dispatch_utterance(chunk, signals)
            assert outcome.kind == "dictate_staged", (
                f"chunk {chunk!r} did not stage cleanly: {outcome.kind}"
            )
        outcome = manager.dispatch_utterance("end", signals)
    return manager, outcome


def test_buffered_session_commit_records_legacy_and_sqlite_history():
    """Natural pauses stage without pasting (req 1); "end" commits once,
    and history/notify/paste all see the same finalized text (req 7).

    Expected text is "1 complete thought." (not "One...") -- format_numbers
    is on in the default test config and correctly converts the number
    word, same as it would for any other dictation path (req 3)."""
    app = _buffered_dictation_app()
    manager, outcome = _dictate_and_end(app, ["One complete thought."])

    assert outcome.kind == "dictate_committed"
    assert manager.mode is SessionMode.DICTATE
    app._paste_preserving_clipboard.assert_called_once_with("1 complete thought.", before_paste=ANY)
    app.add_to_history.assert_called_once_with("1 complete thought.", is_command=False)
    assert app._log_history.call_args.kwargs["mode"] == "dictate"
    app._notify_main_window.assert_called_once_with("1 complete thought.")


def test_staged_chunks_create_no_history_until_one_successful_paste():
    app = _buffered_dictation_app()
    signals = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))
    with patch("dictation._get_foreground_exe_lower", return_value="codex.exe"), \
         patch("dictation._get_foreground_hwnd", return_value=4242):
        manager = app._ensure_session_mode_manager()
        manager.force_mode(SessionMode.DICTATE)
        manager.dispatch_utterance("first part", signals)
        manager.dispatch_utterance("second part", signals)
        app.add_to_history.assert_not_called()
        app._log_history.assert_not_called()
        outcome = manager.dispatch_utterance("end", signals)

    assert outcome.kind == "dictate_committed"
    app.add_to_history.assert_called_once()
    app._log_history.assert_called_once()


# ---------------------------------------------------------------------------
# Formatting pipeline -- the actual bug fix. Every case below reuses
# process_transcription/clean_text/smart_correct/_apply_formatting_tokens
# via dictation.py's _inject_fn; none of this duplicates that logic.
# ---------------------------------------------------------------------------

def test_lowercase_chunk_gets_capitalized_and_punctuated_on_end():
    """'this is a test' + 'end' pastes 'This is a test.'"""
    app = _buffered_dictation_app()
    manager, outcome = _dictate_and_end(app, ["this is a test"])
    assert outcome.kind == "dictate_committed"
    app._paste_preserving_clipboard.assert_called_once_with("This is a test.", before_paste=ANY)


def test_two_chunks_join_into_one_thought_not_two_forced_sentences():
    """Two natural-pause chunks become ONE formatted thought -- one leading
    capital, one terminal period -- not two sentences forced at the chunk
    boundary (req 4: no formatting per silence-bounded chunk)."""
    app = _buffered_dictation_app()
    manager, outcome = _dictate_and_end(app, ["Hello there", "How are you today"])
    assert outcome.kind == "dictate_committed"
    app._paste_preserving_clipboard.assert_called_once_with(
        "Hello there how are you today.",
        before_paste=ANY,
    )


def test_existing_commas_and_apostrophes_survive():
    """req 5: punctuation Whisper already produced is preserved."""
    app = _buffered_dictation_app()
    manager, outcome = _dictate_and_end(app, ["it's raining, and cold"])
    assert outcome.kind == "dictate_committed"
    app._paste_preserving_clipboard.assert_called_once_with(
        "It's raining, and cold.",
        before_paste=ANY,
    )


def test_verbatim_mode_adds_no_capitalization_or_terminal_punctuation():
    """req 3/4: cleanup_mode=verbatim must not gain a capital or a period."""
    app = _buffered_dictation_app({"cleanup_mode": "verbatim", "auto_capitalize": False})
    manager, outcome = _dictate_and_end(app, ["print the value"])
    assert outcome.kind == "dictate_committed"
    app._paste_preserving_clipboard.assert_called_once_with("print the value", before_paste=ANY)


def test_formatting_tokens_applied_exactly_once():
    """req 3/10: 'new line' becomes one literal newline, inserted as the
    LAST pipeline step (after cleanup added the terminal period), not
    substituted per chunk and not substituted twice."""
    app = _buffered_dictation_app()
    manager, outcome = _dictate_and_end(app, ["hello new line world"])
    assert outcome.kind == "dictate_committed"
    app._paste_preserving_clipboard.assert_called_once_with("Hello\nworld.", before_paste=ANY)


def test_enabled_smart_corrections_receives_complete_thought_once():
    """req 3/6: Smart Corrections runs once, on the full joined text, only
    when the wake/DICTATE lane's setting enables it."""
    app = _buffered_dictation_app({
        "smart_corrections": {"enabled": True, "modes": {"wake": True}},
    })
    with patch("dictation.smart_correct", return_value="mocked result.") as mock_sc:
        manager, outcome = _dictate_and_end(app, ["first part", "second part"])

    assert outcome.kind == "dictate_committed"
    mock_sc.assert_called_once()
    called_text = mock_sc.call_args[0][0].lower()
    assert "first part" in called_text and "second part" in called_text
    app._paste_preserving_clipboard.assert_called_once_with("mocked result.", before_paste=ANY)


def test_disabled_smart_corrections_never_called():
    """req 6: never invoked at all when the wake/DICTATE lane's setting is off."""
    app = _buffered_dictation_app({
        "smart_corrections": {"enabled": True, "modes": {"wake": False}},
    })
    with patch("dictation.smart_correct") as mock_sc:
        manager, outcome = _dictate_and_end(app, ["hello there"])

    assert outcome.kind == "dictate_committed"
    mock_sc.assert_not_called()


def test_history_display_and_paste_share_finalized_text_raw_text_is_pre_cleanup():
    """req 7/8: history/display/paste/notify all get the same finalized
    text; raw_text keeps the pre-cleanup accumulated text separately."""
    app = _buffered_dictation_app()
    manager, outcome = _dictate_and_end(app, ["um this is a test"])
    assert outcome.kind == "dictate_committed"

    pasted = app._paste_preserving_clipboard.call_args[0][0]
    assert pasted == "This is a test."  # "um" filler stripped by clean_text
    app.add_to_history.assert_called_once_with(pasted, is_command=False)
    app._notify_main_window.assert_called_once_with(pasted)

    log_kwargs = app._log_history.call_args.kwargs
    assert log_kwargs["display_text"] == pasted
    assert "um" in log_kwargs["raw_text"].lower()
    assert log_kwargs["raw_text"] != pasted


def test_paste_failure_retains_pending_buffer_and_plays_error_earcon():
    """req 9: a failed commit keeps the buffer, stays in DICTATE, and plays
    the existing error earcon rather than losing the dictated thought."""
    app = _buffered_dictation_app()
    app._paste_preserving_clipboard = Mock(return_value=False)
    manager, outcome = _dictate_and_end(app, ["this will fail to paste"])

    assert outcome.kind == "dictate_commit_failed"
    assert manager.mode == SessionMode.DICTATE
    assert manager.dictate_pending_buffer  # retained, not cleared
    app.play_sound.assert_any_call("error")
    app.add_to_history.assert_not_called()


def test_the_end_and_weekend_remain_ordinary_dictated_text():
    """req: sole-word 'end' matching must not misfire on 'the end' or
    'weekend' -- both stay staged as ordinary text, never committed."""
    app = _buffered_dictation_app()
    signals = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))
    with patch("dictation._get_foreground_exe_lower", return_value="codex.exe"), \
         patch("dictation._get_foreground_hwnd", return_value=4242):
        manager = app._ensure_session_mode_manager()
        manager.force_mode(SessionMode.DICTATE)
        outcome1 = manager.dispatch_utterance("let's go to the end", signals)
        outcome2 = manager.dispatch_utterance("it's almost weekend", signals)

    assert outcome1.kind == "dictate_staged"
    assert outcome2.kind == "dictate_staged"
    app._paste_preserving_clipboard.assert_not_called()
    assert "the end" in manager.dictate_pending_buffer.lower()
    assert "weekend" in manager.dictate_pending_buffer.lower()
