"""505: synthetic kept-draft state traces, without app bootstrap or devices.

The exit helper mirrors exit_command_mode's verified parking/retention seam;
the existing 492 tests cover that seam in the host. All lane and recovery
operations below use the real session manager and authoritative document.
"""
import pytest

from samsara.draft_document import DraftDocument, RECOVERABLE_DRAFT_TTL_S
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals


SIGNALS = UtteranceSignals(True, (1.2,), True)
TEXT = "Synthetic waiting draft."


def end_session(manager):
    if manager._live_surface_enabled and manager.dictate_pending_buffer:
        manager.pause_draft(source="session exit")
        manager.discard_retained_draft()
    if not manager.has_parked_hold:
        manager.retain_draft()
    manager.reset()


def make_manager(*, full=True):
    injected = []
    manager = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=lambda: "synthetic.exe",
        foreground_hwnd_resolver=lambda: 99,
        inject_fn=lambda text, *args: injected.append(text) or True,
        remove_chars_fn=lambda count: True,
        command_dispatch_fn=lambda text: None,
        agent_dispatch_fn=lambda text, context=None: None,
        buffer_dictate_until_commit=True,
        live_surface_enabled=full,
        on_abort=lambda: end_session(manager),
    )
    manager.reset(SessionMode.DICTATE)
    assert manager.dispatch_utterance(TEXT, SIGNALS).kind == "dictate_staged"
    return manager, injected


@pytest.mark.parametrize("lane", ["command mode", "ava mode"])
def test_k1_exit_restart_switch_then_dictate_keeps_words_and_inserts_once(lane):
    manager, injected = make_manager()
    end_session(manager)
    manager.reset(SessionMode.DICTATE)
    before = manager.draft_document.segments
    assert manager.dispatch_utterance(lane, SIGNALS).kind == "mode_switch"
    assert manager.dispatch_utterance("dictate", SIGNALS).kind == "mode_switch"
    assert manager.dictate_pending_buffer == TEXT
    assert manager.draft_document.segments == before
    assert manager.has_parked_hold and manager.draft_document.manual_commit
    assert injected == []
    assert manager.commit_pending_dictation().kind == "dictate_committed"
    assert injected == [TEXT]
    assert not manager.has_parked_hold


def test_repeated_command_dictate_switches_never_clear_or_send_kept_words():
    manager, injected = make_manager()
    end_session(manager)
    manager.reset(SessionMode.DICTATE)
    for phrase in ("command mode", "dictate", "command mode", "dictate"):
        assert manager.dispatch_utterance(phrase, SIGNALS).kind == "mode_switch"
        assert manager.dictate_pending_buffer == TEXT
        assert manager.has_parked_hold
        assert injected == []


@pytest.mark.parametrize("full", [True, False], ids=["Full", "Compact"])
def test_k2_cancel_restart_recover_then_insert_does_not_double(full):
    manager, injected = make_manager(full=full)
    assert manager.dispatch_utterance("cancel", SIGNALS).kind == "abort"
    assert manager.recoverable_draft == TEXT
    manager.reset(SessionMode.DICTATE)
    assert manager.dictate_pending_buffer == TEXT
    outcome = manager.dispatch_utterance("bring back my draft", SIGNALS)
    assert outcome.kind == "dictate_draft_recovered"
    assert not outcome.detail["prepended"]
    assert manager.dictate_pending_buffer == TEXT
    assert manager.recoverable_draft == ""
    assert manager.dispatch_utterance("bring back my draft", SIGNALS).kind == "dictate_recover_nothing"
    assert manager.commit_pending_dictation().kind == "dictate_committed"
    assert injected == [TEXT]


def test_recover_empty_parked_document_still_restores_the_stashed_words():
    manager, injected = make_manager()
    assert manager.confirm_clear_draft("clear button").kind == "dictate_draft_cleared"
    manager.pause_draft(source="empty parked draft")
    assert manager.dictate_pending_buffer == ""
    assert manager.recover_draft() == {"chars": len(TEXT), "source": "clear button", "prepended": False}
    assert manager.dictate_pending_buffer == TEXT and manager.has_parked_hold
    assert manager.commit_pending_dictation().kind == "dictate_committed"
    assert injected == [TEXT]


def test_lane_switch_keeps_parked_audio_and_terminal_metadata():
    manager, _ = make_manager()
    manager.pause_draft()
    audio = object()
    manager._dictate_pending_audio = [audio]
    manager._last_dictate_ended_terminal = True
    manager.dispatch_utterance("command mode", SIGNALS)
    manager.dispatch_utterance("dictate", SIGNALS)
    assert manager.dictate_pending_buffer == TEXT
    assert manager._dictate_pending_audio == [audio]
    assert manager._last_dictate_ended_terminal is True


def test_duplicate_recovery_keeps_audio_segments_and_local_undo():
    manager, _ = make_manager()
    audio = object()
    manager._dictate_pending_audio = [audio]
    manager._stash_recoverable_draft("cancel")
    manager.pause_draft()
    manager.stage_parked_hold("Synthetic newer words.")
    before = manager.draft_document.segments
    assert manager.recover_draft()["prepended"] is False
    assert manager._dictate_pending_audio == [audio]
    assert manager.draft_document.segments == before
    assert manager._do_scratch_that()
    assert manager.dictate_pending_buffer == TEXT


@pytest.mark.parametrize("existing", [TEXT, TEXT + " Extra.", "Earlier. " + TEXT, "Earlier. " + TEXT + " Extra."])
def test_document_recovery_consumes_an_already_contained_copy_without_editing(existing):
    doc = DraftDocument()
    doc.append(TEXT)
    doc.stash_recoverable("cancel")
    doc.replace_text(existing)
    doc.mark_manual_commit()
    before = (doc.segments, doc.revision, doc.manual_commit)
    assert doc.recover(prepend=True) == {"chars": len(TEXT), "source": "cancel", "prepended": False}
    assert doc.text == existing
    assert (doc.segments, doc.revision, doc.manual_commit) == before
    assert doc.recover() is None


def test_different_recovery_still_prepends_text_and_audio_in_order():
    manager, _ = make_manager()
    older_audio, newer_audio = object(), object()
    manager._dictate_pending_audio = [older_audio]
    manager.confirm_clear_draft("clear button")
    manager.stage_parked_hold("Synthetic newer words.")
    manager._dictate_pending_audio = [newer_audio]
    assert manager.recover_draft()["prepended"]
    assert manager.dictate_pending_buffer == TEXT + " Synthetic newer words."
    assert manager._dictate_pending_audio == [older_audio, newer_audio]


def test_expired_recovery_does_not_change_kept_draft():
    now = [0.0]
    doc = DraftDocument(clock=lambda: now[0])
    doc.append(TEXT)
    doc.stash_recoverable("cancel")
    now[0] = RECOVERABLE_DRAFT_TTL_S + 1
    assert doc.recover(prepend=True) is None
    assert doc.text == TEXT


def test_kept_draft_survives_failed_lane_badge_update():
    manager, injected = make_manager()
    manager.pause_draft()
    manager.dispatch_utterance("command mode", SIGNALS)

    def fail_dictate_badge(mode):
        if mode is SessionMode.DICTATE:
            raise RuntimeError("Synthetic badge failure")

    manager._on_mode_change = fail_dictate_badge
    assert manager.dispatch_utterance("dictate", SIGNALS).kind == "mode_switch_failed"
    assert manager.mode is SessionMode.COMMAND
    assert manager.dictate_pending_buffer == TEXT and manager.has_parked_hold
    assert injected == []
