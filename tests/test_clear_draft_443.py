"""Voice clear and its memory-only take-back; synthetic drafts, no bootstrap."""
import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.config_schema import spoken_notices_for_level
from samsara.session_modes import (
    CLEAR_DRAFT_PHRASES, CLEAR_DRAFT_CONFIRM_TTL_S, CommandDispatchResult,
    SessionMode, SessionModeManager, UtteranceSignals, outcome_chip,
)

GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,),
                        transcript_confident=True)
NOISE = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(50.0,))


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make_manager():
    managers = []

    def make(level="brief"):
        clock, spoken, injected = Clock(), [], Mock()
        manager = SessionModeManager(
            abort_phrases=["cancel", "abort"],
            foreground_exe_resolver=lambda: "notepad.exe",
            foreground_hwnd_resolver=lambda: 4242,
            inject_fn=injected, remove_chars_fn=Mock(),
            command_dispatch_fn=lambda _: CommandDispatchResult(matched=False, phrase=None),
            agent_dispatch_fn=Mock(), buffer_dictate_until_commit=True,
            live_surface_enabled=True, clock=clock,
            speak_fn=lambda text, category: spoken.append((text, category)),
        )
        manager.reset(SessionMode.DICTATE)
        manager.set_spoken_notices(spoken_notices_for_level(level))
        managers.append(manager)
        return manager, clock, spoken, injected

    yield make
    for manager in managers:
        manager._clear_draft_undo.drop()


@pytest.mark.parametrize("phrase", CLEAR_DRAFT_PHRASES)
@pytest.mark.parametrize("level", ["silent", "brief", "full"])
def test_each_phrase_clears_immediately_at_every_feedback_level(make_manager, phrase, level):
    manager, _, spoken, injected = make_manager(level)
    manager._dictate_pending_buffer = "Synthetic first thought.\nSecond thought.  "
    outcome = manager.dispatch_utterance(phrase, GOOD)
    assert outcome.kind == "dictate_draft_cleared"
    assert manager.dictate_pending_buffer == ""
    assert not hasattr(manager, "_pending_clear")
    assert spoken == ([("Draft cleared", "confirmation")] if level == "full" else [])
    assert outcome_chip(outcome.kind, outcome.detail) == (
        'Draft cleared - say "bring it back"', "success")
    injected.assert_not_called()


@pytest.mark.parametrize("sentence", [
    "I told him to scratch all that", "I told him to scratch all of that",
    "We should scratch everything we planned", "He asked me to bring it back tomorrow",
])
def test_phrases_inside_sentences_remain_dictation(make_manager, sentence):
    manager, _, spoken, _ = make_manager()
    assert manager.dispatch_utterance(sentence, GOOD).kind == "dictate_staged"
    assert sentence in manager.dictate_pending_buffer
    assert spoken == []


def test_restore_is_exact_and_preserves_audio_chunks_and_scratch(make_manager):
    manager, clock, spoken, injected = make_manager("full")
    for index, text in enumerate(("First thought.", "Second thought!", "Third thought?")):
        manager.dispatch_utterance(text, UtteranceSignals(
            has_contiguous_speech=True, compression_ratios=(1.0,), audio_ref=f"synthetic-{index}"))
    # Include a correction and exact whitespace that must not be normalised.
    manager._dictate_pending_buffer = manager.dictate_pending_buffer.replace("First", "Corrected")
    before = manager.dictate_pending_buffer
    audio, items = list(manager._dictate_pending_audio), list(manager._stack._items)
    manager.dispatch_utterance("Scratch all that.", GOOD)
    assert manager._dictate_pending_audio == [] and not manager._stack._items
    clock.now += 29.9
    spoken.clear()
    outcome = manager.dispatch_utterance("Bring it back!", GOOD)
    assert outcome.kind == "dictate_draft_recovered"
    assert manager.dictate_pending_buffer == before
    assert manager._dictate_pending_audio == audio and list(manager._stack._items) == items
    assert spoken == [] and manager._clear_draft_undo._snapshot is None
    assert manager.dispatch_utterance("scratch that", GOOD).kind == "scratch_success"
    assert "Third thought?" not in manager.dictate_pending_buffer
    injected.assert_not_called()


def test_restore_preserves_exact_unicode_and_whitespace(make_manager):
    manager, _, _, _ = make_manager()
    before = "  Synthetic café.\n\nSecond thought!\t  "
    manager._dictate_pending_buffer = before
    manager.dispatch_utterance("scratch all that", GOOD)
    assert manager.dispatch_utterance("bring it back", GOOD).kind == "dictate_draft_recovered"
    assert manager.dictate_pending_buffer == before


def test_empty_second_clear_keeps_the_existing_take_back(make_manager):
    manager, _, spoken, _ = make_manager("full")
    manager._dictate_pending_buffer = "Synthetic first clear."
    manager.dispatch_utterance("scratch all that", GOOD)
    spoken.clear()
    assert manager.dispatch_utterance("scratch everything", GOOD).kind == "dictate_clear_nothing"
    assert spoken == []
    assert manager.dispatch_utterance("bring it back", GOOD).kind == "dictate_draft_recovered"
    assert manager.dictate_pending_buffer == "Synthetic first clear."


@pytest.mark.parametrize("seconds", [30.0, 30.1, 100.0])
def test_expired_restore_is_screen_only(make_manager, seconds):
    manager, clock, spoken, _ = make_manager("full")
    manager._dictate_pending_buffer = "Synthetic expired draft."
    manager.dispatch_utterance("scratch all that", GOOD)
    clock.now += seconds
    spoken.clear()
    outcome = manager.dispatch_utterance("bring it back", GOOD)
    assert outcome.kind == "dictate_recover_nothing"
    assert outcome_chip(outcome.kind, outcome.detail) == ("nothing to bring back", "warning")
    assert manager.dictate_pending_buffer == "" and spoken == []
    assert manager._clear_draft_undo._snapshot is None


@pytest.mark.parametrize("new_thought", ["Synthetic next thought.", "yes", "no"])
def test_new_thought_drops_copy_and_restore_cannot_overwrite_it(make_manager, new_thought):
    manager, _, spoken, _ = make_manager("full")
    manager._dictate_pending_buffer = "Synthetic old thought."
    manager.dispatch_utterance("scratch everything", GOOD)
    manager.dispatch_utterance(new_thought, GOOD)
    before = manager.dictate_pending_buffer
    assert manager._clear_draft_undo._snapshot is None
    spoken.clear()
    outcome = manager.dispatch_utterance("bring it back", GOOD)
    assert outcome.kind == "dictate_recover_nothing"
    assert manager.dictate_pending_buffer == before and spoken == []


@pytest.mark.parametrize("ending", ["reset", "cancel", "go to sleep"])
def test_session_end_drops_copy(make_manager, ending):
    manager, _, spoken, _ = make_manager("full")
    manager._dictate_pending_buffer = "Synthetic session draft."
    manager.dispatch_utterance("scratch everything", GOOD)
    if ending == "reset":
        manager.reset(SessionMode.DICTATE)
    else:
        manager.dispatch_utterance(ending, GOOD)
        assert manager._clear_draft_undo._snapshot is None
        manager.reset(SessionMode.DICTATE)
    spoken.clear()
    assert manager.dispatch_utterance("bring it back", GOOD).kind == "dictate_recover_nothing"
    assert manager.dictate_pending_buffer == "" and spoken == []


@pytest.mark.parametrize("text", ["", "   \n"])
@pytest.mark.parametrize("level", ["silent", "brief", "full"])
def test_empty_clear_is_silent_and_keeps_existing_empty_chip(make_manager, text, level):
    manager, _, spoken, _ = make_manager(level)
    manager._dictate_pending_buffer = text
    outcome = manager.dispatch_utterance("scratch all that", GOOD)
    assert outcome.kind == "dictate_clear_nothing"
    assert manager.dictate_pending_buffer == text and spoken == []
    assert manager._clear_draft_undo._snapshot is None
    assert outcome_chip(outcome.kind, outcome.detail) == ("refused: nothing staged", "warning")


@pytest.mark.parametrize("source", ["quit discard", "clear button", "live surface"])
@pytest.mark.parametrize("level", ["silent", "brief", "full"])
def test_programmatic_click_and_quit_paths_remain_unchanged(make_manager, source, level):
    manager, clock, spoken, injected = make_manager(level)
    before = "Synthetic legacy draft. "
    manager._dictate_pending_buffer = before
    outcome = manager.confirm_clear_draft(source)
    assert outcome.detail == {"cleared_chars": len(before), "recoverable_chars": len(before), "source": source}
    assert outcome_chip(outcome.kind, outcome.detail)[0] == "draft cleared, say bring back my draft"
    assert spoken == ([("Draft cleared. Say bring back my draft if you want it back.", "confirmation")]
                      if level == "full" else [])
    clock.now += 31  # Legacy recovery outlives the new voice-only window.
    manager.reset(SessionMode.DICTATE)
    assert manager.dispatch_utterance("bring back my draft", GOOD).kind == "dictate_draft_recovered"
    assert manager.dictate_pending_buffer == before
    injected.assert_not_called()


def test_cleared_text_never_reaches_logging_or_outcome_detail(make_manager, caplog):
    manager, _, _, _ = make_manager("full")
    private_text = "Synthetic privacy sentinel 443.\nExact spacing.  "
    manager._dictate_pending_buffer = private_text
    with caplog.at_level(logging.DEBUG):
        clear = manager.dispatch_utterance("scratch all that", GOOD)
        restore = manager.dispatch_utterance("bring it back", GOOD)
    assert manager.dictate_pending_buffer == private_text
    assert all(private_text not in record.getMessage() and "privacy sentinel" not in record.getMessage()
               for record in caplog.records)
    assert "privacy sentinel" not in repr(clear.detail) + repr(restore.detail)


def test_timer_drops_copy_without_another_utterance_and_old_timer_cannot_drop_new_copy(make_manager, monkeypatch):
    timers = []

    class Timer:
        def __init__(self, seconds, callback, args):
            self.seconds, self.callback, self.args = seconds, callback, args
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    def timer(name, seconds, callback, args, daemon):
        assert name == "draft.clear_undo"
        instance = Timer(seconds, callback, args)
        instance.daemon = daemon
        instance.start()
        return instance

    monkeypatch.setattr("samsara.draft_clear_undo.thread_registry.timer", timer)
    manager, _, _, _ = make_manager()
    manager._dictate_pending_buffer = "Synthetic timer draft."
    manager.dispatch_utterance("scratch everything", GOOD)
    assert timers[0].seconds == CLEAR_DRAFT_CONFIRM_TTL_S and timers[0].daemon
    manager.dispatch_utterance("bring it back", GOOD)
    assert timers[0].cancelled
    manager.dispatch_utterance("scratch everything", GOOD)
    timers[0].callback(*timers[0].args)
    assert manager._clear_draft_undo._snapshot is not None
    timers[1].callback(*timers[1].args)
    assert manager._clear_draft_undo._snapshot is None and manager._clear_draft_undo._timer is None


def test_clear_undo_real_timer_is_registered_and_expires_without_utterance(make_manager):
    from samsara.runtime import thread_registry

    manager, _, _, _ = make_manager()
    undo = manager._clear_draft_undo
    undo._ttl_s = 0.05
    manager._dictate_pending_buffer = "Synthetic timer draft."
    # Hold the lock until the timer reference and registration are inspected.
    with undo._lock:
        manager.dispatch_utterance("scratch everything", GOOD)
        timer = undo._timer
        assert timer.daemon and timer.interval == 0.05
        assert any(entry["ident"] == timer.ident and entry["alive"]
                   for entry in thread_registry.snapshot())
    timer.join(timeout=2)
    assert not timer.is_alive()
    assert not undo.pending and undo._timer is None


def test_gate_and_existing_scratch_meaning_are_preserved(make_manager):
    manager, _, _, _ = make_manager()
    manager.dispatch_utterance("First synthetic thought.", GOOD)
    manager.dispatch_utterance("Second synthetic thought.", GOOD)
    assert manager.dispatch_utterance("scratch that", GOOD).kind == "scratch_success"
    assert manager.dictate_pending_buffer == "First synthetic thought."
    assert manager.dispatch_utterance("scratch everything", NOISE).kind != "dictate_draft_cleared"
    assert "First synthetic thought" in manager.dictate_pending_buffer


def test_voice_clear_does_not_populate_legacy_five_minute_recovery(make_manager):
    manager, clock, _, _ = make_manager()
    manager._dictate_pending_buffer = "Synthetic voice-only draft."
    manager.dispatch_utterance("scratch everything", GOOD)
    clock.now += 31
    assert manager.recoverable_draft == ""
    assert manager.dispatch_utterance("bring back my draft", GOOD).kind == "dictate_recover_nothing"


def test_parked_hold_new_thought_drops_voice_copy(make_manager):
    manager, _, _, _ = make_manager()
    manager._dictate_pending_buffer = "Synthetic previous draft."
    manager.dispatch_utterance("scratch everything", GOOD)
    manager.stage_parked_hold("Synthetic new hold.")
    assert manager._clear_draft_undo._snapshot is None


def test_parked_edited_draft_restores_document_undo_and_stale_coordinates(make_manager):
    manager, _, _, _ = make_manager()
    manager.pause_draft()
    manager.stage_parked_hold("Synthetic original hold.  ")
    document = manager.draft_document
    segment = document.segments[0]
    document.edit(document.document_id, document.revision, segment.id, 10, 18, "corrected")
    before, revision = document.text, document.revision
    manager.dispatch_utterance("scratch everything", GOOD)
    manager.dispatch_utterance("bring it back", GOOD)
    assert document.text == before and document.manual_commit and document.edited
    assert not document.edit(document.document_id, revision, segment.id, 0, 1, "X").accepted
    assert manager.dispatch_utterance("scratch that", GOOD).kind == "scratch_success"
    assert document.text == "Synthetic original hold.  "


@pytest.mark.skipif(os.environ.get("CI", "").lower() == "true"
                 and bool(os.environ.get("PYTEST_XDIST_WORKER")),
                 reason="908: hosted xdist Qt eventFilter SystemError; passes locally")
def test_new_live_display_shows_clear_and_restore_from_real_outcomes(make_manager, qapp, monkeypatch):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import CaptureState, Lane, LiveSurfaceModel

    monkeypatch.setattr("samsara.live_surface.controller.qt_runtime.post", lambda _: None)
    manager, _, _, _ = make_manager()
    app = SimpleNamespace(config={"ui": {"live_surface": {"enabled": True}}},
                          _session_mode_manager=manager)
    now = [0.0]
    controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: now[0]))
    controller.start()
    adapter = LiveSurfaceIndicatorAdapter(controller)
    controller.begin_capture(Lane.HANDS_FREE_DICTATE, capture=CaptureState.HANDS_FREE_LISTENING)
    manager._dictate_pending_buffer = "Synthetic display draft."
    controller.sync_draft(manager)
    controller.drain()
    try:
        for phrase, expected in [("scratch all that", 'Draft cleared - say "bring it back"'),
                                 ("bring it back", "draft back")]:
            outcome = manager.dispatch_utterance(phrase, GOOD)
            controller.sync_draft(manager)
            adapter.show_outcome(*outcome_chip(outcome.kind, outcome.detail))
            controller.drain()
            if phrase == "bring it back":
                # 775 / 888a: the latest receipt replaces the previous one immediately.
                assert controller.view().notice.message == expected
                assert controller.view().text == manager.dictate_pending_buffer
            assert controller.view().notice.message == expected
            assert controller.view().text == manager.dictate_pending_buffer
    finally:
        controller.close()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()
