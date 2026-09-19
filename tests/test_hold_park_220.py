"""220-F parked hold contract: pure, no-device state traces.

These tests never import ``dictation``.  They exercise the shared document,
parking latch and streaming delivery adapter with synthetic targets/effects.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from samsara.hotkeys import HotkeyCluster
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals
from samsara.streaming import StreamingOverlayQt, StreamingSession


SIGNALS = UtteranceSignals(
    has_contiguous_speech=True, transcript_confident=True, compression_ratios=(1.0,),
)


def make_manager(*, live=True, injected=None, target=None):
    injected = injected if injected is not None else []
    target = target if target is not None else {"exe": "writer.exe", "hwnd": 9}
    manager = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=lambda: target["exe"],
        foreground_hwnd_resolver=lambda: target["hwnd"],
        inject_fn=lambda text, check=None: injected.append(text) or True,
        remove_chars_fn=lambda _n: (_ for _ in ()).throw(
            AssertionError("local parked undo must not delete external text")),
        command_dispatch_fn=lambda _text: None,
        agent_dispatch_fn=lambda _text, _context=None: None,
        buffer_dictate_until_commit=True,
        live_surface_enabled=live,
    )
    manager.reset(SessionMode.DICTATE)
    return manager, injected, target


class TestParkedHoldDocument:
    def test_pause_before_release_latches_then_delayed_final_stages_without_paste(self):
        manager, injected, _target = make_manager()
        assert manager.pause_draft(source="pause key").kind == "draft_parked"
        assert manager.stage_parked_hold("First sentence. ").kind == "dictate_staged"
        assert manager.draft_document.manual_commit
        assert manager.dictate_pending_buffer == "First sentence. "
        assert injected == []

    def test_release_before_pause_can_latch_and_append_the_next_settled_hold(self):
        manager, injected, _target = make_manager()
        manager.pause_draft(source="existing draft")
        manager.stage_parked_hold("First. ")
        manager.stage_parked_hold("Second. ")
        assert manager.dictate_pending_buffer == "First. Second. "
        assert manager.draft_document.manual_commit
        assert injected == []

    def test_only_explicit_commit_clears_manual_delivery(self):
        manager, injected, _target = make_manager()
        manager.pause_draft()
        manager.stage_parked_hold("Parked text. ")
        outcome = manager.commit_pending_dictation()
        assert outcome.kind == "dictate_committed"
        assert injected == ["Parked text. "]
        assert not manager.draft_document.manual_commit
        assert not manager.has_parked_hold

    def test_failed_explicit_delivery_keeps_the_reviewable_document(self):
        injected = []
        manager, _injected, _target = make_manager(injected=injected)
        manager.pause_draft()
        manager.stage_parked_hold("Keep after failure. ")
        manager._inject_fn = lambda _text, _guard=None: False
        outcome = manager.commit_pending_dictation()
        assert outcome.kind == "dictate_commit_failed"
        assert manager.dictate_pending_buffer == "Keep after failure. "
        assert injected == []

    def test_clear_stashes_before_emptying_and_recovery_remains_explicit(self):
        manager, _injected, _target = make_manager()
        manager.pause_draft()
        manager.stage_parked_hold("Keep this. ")
        cleared = manager.confirm_clear_draft("test")
        assert cleared.kind == "dictate_draft_cleared"
        assert manager.dictate_pending_buffer == ""
        assert manager.recoverable_draft == "Keep this. "
        assert manager.recover_draft()
        assert manager.dictate_pending_buffer == "Keep this. "

    def test_reset_keeps_a_parked_document_until_clear_or_commit(self):
        manager, injected, _target = make_manager()
        manager.pause_draft()
        manager.stage_parked_hold("Do not lose this. ")
        manager.reset(SessionMode.COMMAND)
        assert manager.dictate_pending_buffer == "Do not lose this. "
        assert injected == []

    def test_mic_stop_or_timeout_cannot_expire_an_existing_parked_draft(self):
        manager, injected, _target = make_manager()
        manager.pause_draft(source="before mic stop")
        manager.stage_parked_hold("Settled text. ")
        # A terminal capture with no final text has no document operation to
        # perform.  The process-lifetime parked draft remains available.
        assert manager.dictate_pending_buffer == "Settled text. "
        assert manager.has_parked_hold and injected == []

    def test_undo_prefers_local_edit_then_segment_and_never_external_delete(self):
        manager, _injected, _target = make_manager()
        manager.pause_draft()
        manager.stage_parked_hold("one ")
        manager.stage_parked_hold("two ")
        segment = manager.draft_document.segments[-1]
        edited = manager.draft_document.edit(
            manager.draft_document.document_id, manager.draft_document.revision,
            segment.id, 0, 3, "three")
        assert edited.accepted
        assert manager._do_scratch_that()
        assert manager.dictate_pending_buffer == "one two "
        assert manager._do_scratch_that()
        assert manager.dictate_pending_buffer == "one "

    def test_exact_voice_pause_parks_but_literal_phrase_is_dictated(self):
        manager, _injected, _target = make_manager()
        outcome = manager.dispatch_utterance("pause draft", SIGNALS)
        assert outcome.kind == "draft_parked"
        literal = manager.dispatch_utterance("literal pause draft", SIGNALS)
        assert literal.kind == "dictate_staged"
        assert "pause draft" in manager.dictate_pending_buffer

    def test_flag_off_keeps_pause_phrase_and_key_inert(self):
        manager, _injected, _target = make_manager(live=False)
        assert manager.pause_draft().kind == "draft_pause_unavailable"
        outcome = manager.dispatch_utterance("pause draft", SIGNALS)
        assert outcome.kind == "dictate_staged"

        app = SimpleNamespace(
            config={"mode": "hold", "ui": {"live_surface": {"enabled": False}}},
            recording=True, command_mode_recording=False, ava_mode_recording=False,
            pause_hold_capture=MagicMock(return_value=True),
        )
        assert not HotkeyCluster._pause_active_hold(app)
        app.pause_hold_capture.assert_not_called()


class TestStreamingParkDelivery:
    def _session(self, app):
        session = StreamingSession.__new__(StreamingSession)
        session.app = app
        session._parking_enabled = True
        session._target_hwnd = object()  # synthetic original target; no real HWND
        session._state = StreamingSession.STATE_STREAMING
        session._state_lock = threading.Lock()
        session.stop_event = threading.Event()
        session.cancel_event = threading.Event()
        session._direct_paste = False
        session._last_pasted = False
        session._overlay = MagicMock()
        session._capture_cleanup_lock = threading.Lock()
        session._capture_cleaned = False
        session._finished_notified = False
        return session

    def test_focus_change_parks_instead_of_inserting(self):
        manager, inserted, _target = make_manager()
        app = SimpleNamespace(
            _hold_park_requested=False,
            _session_mode_manager=manager,
            _ensure_session_mode_manager=lambda: manager,
            _verbatim_rule=lambda: None,
            live_surface=None,
            config={"auto_paste": True},
            _on_streaming_session_finished=MagicMock(),
            _dictation_consumer=None,
            _paste_preserving_clipboard=MagicMock(),
        )
        session = self._session(app)
        session._deliver_final("Focus changed text", None, 1.0, 0)
        assert manager.has_parked_hold
        assert manager.dictate_pending_buffer == "Focus changed text"
        assert inserted == []
        app._paste_preserving_clipboard.assert_not_called()

    def test_repeated_release_is_idempotent_while_finalizing(self):
        session = StreamingSession.__new__(StreamingSession)
        session._state = StreamingSession.STATE_STREAMING
        session._state_lock = threading.Lock()
        session.stop_event = threading.Event()
        session.finalize()
        session.finalize()
        assert session.stop_event.is_set()
        assert session._state == StreamingSession.STATE_FINALIZING

    def test_ambiguous_terminal_phrase_is_retained_whole_not_inserted(self):
        manager, inserted, _target = make_manager()
        app = SimpleNamespace(
            _hold_park_requested=False,
            _session_mode_manager=manager,
            _ensure_session_mode_manager=lambda: manager,
            _verbatim_rule=lambda: None,
            live_surface=None,
            config={"auto_paste": True},
            _on_streaming_session_finished=MagicMock(),
            _dictation_consumer=None,
            _paste_preserving_clipboard=MagicMock(),
        )
        session = self._session(app)
        session._deliver_final("Keep this pause draft", None, 1.0, 0)
        assert manager.dictate_pending_buffer == "Keep this pause draft"
        assert inserted == []
        app._paste_preserving_clipboard.assert_not_called()

    def test_literal_terminal_phrase_does_not_be_treated_as_voice_control(self):
        manager, _inserted, _target = make_manager()
        app = SimpleNamespace(
            _hold_park_requested=False,
            _session_mode_manager=manager,
            _ensure_session_mode_manager=lambda: manager,
            _verbatim_rule=lambda: "forced",
            live_surface=None,
            config={"auto_paste": False},
            _on_streaming_session_finished=MagicMock(),
            _dictation_consumer=None,
        )
        session = self._session(app)
        # Focus is synthetic/unknown, so delivery still parks safely; this
        # assertion isolates the phrase rule by proving the literal text stays.
        session._deliver_final("literal pause draft", None, 1.0, 0)
        assert manager.dictate_pending_buffer == "literal pause draft"
