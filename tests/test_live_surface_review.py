"""220-G review surface: identity-bound corrections and large controls."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QAbstractAnimation, QTimer
from shiboken6 import isValid

from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.model import (CaptureState, DocumentState, EventKind, Lane,
                                        Notice, NoticeKind, PresentationState,
                                        SurfaceEvent, SurfaceView, VisibleForm)
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals
from samsara.ui.live_surface_qt import LiveSurfaceWidget, MARK_SIZE


@pytest.fixture
def surface_owner(qapp, qt_deferred_delete):
    """Keep review objects alive until their Qt activity is stopped and deleted."""
    owners = []

    def own(obj):
        owners.append(obj)
        return obj

    try:
        yield own
    finally:
        for owner in reversed(owners):
            controller = isinstance(owner, LiveSurfaceController)
            widget = owner.widget if controller else owner
            if widget is not None and isValid(widget):
                for animation in widget.findChildren(QAbstractAnimation):
                    animation.stop()
                for timer in widget.findChildren(QTimer):
                    timer.stop()
                if controller:
                    owner.close()
                else:
                    widget.hide()
                    widget.deleteLater()
                # Do not run queued controller callbacks or another test's deletes.
                qt_deferred_delete(widget)


def manager(*, live=True):
    result = SessionModeManager(abort_phrases=["cancel"], inject_fn=lambda *_: True,
        foreground_exe_resolver=lambda: "writer.exe",
        remove_chars_fn=lambda *_: None, command_dispatch_fn=lambda *_: None,
        agent_dispatch_fn=lambda *_: None, buffer_dictate_until_commit=True,
        live_surface_enabled=live)
    result.reset(SessionMode.DICTATE)
    return result


def review(text="one two two three"):
    return SurfaceView(VisibleForm.REVIEW, CaptureState.OFF, Lane.HOLD,
        DocumentState.PARKED_DRAFT, PresentationState.PINNED_REVIEW,
        Notice(NoticeKind.NONE, "", None), "d", 1, text, "", False)


class Source:
    def __init__(self, view, segments=()): self._view, self.segments = view, segments
    def view(self): return self._view
    def document_snapshot(self): return {"document_id": self._view.document_id,
        "revision": self._view.revision, "segments": self.segments}


def test_repeated_word_selection_carries_segment_span_and_occurrence(qapp, surface_owner):
    source = Source(review(), [("s", "one two two three")])
    widget = surface_owner(LiveSurfaceWidget(source)); widget.show(); qapp.processEvents()
    chosen = []
    widget.correct_word_requested.connect(chosen.append)
    widget._anchor_clicked(__import__('PySide6').QtCore.QUrl("word:2"))
    assert chosen[0]["segment_id"] == "s"
    assert chosen[0]["start"] == 8 and chosen[0]["end"] == 11
    assert chosen[0]["occurrence"] == 1


def test_stale_span_is_refused_after_a_revision_change():
    subject = manager(); subject.pause_draft(); subject.stage_parked_hold("one two two ")
    segment = subject.draft_document.segments[0]
    selection = {"document_id": subject.draft_document.document_id,
                 "revision": subject.draft_document.revision, "segment_id": segment.id,
                 "start": 8, "end": 11}
    selected = subject.select_live_surface_word(selection)
    assert selected["ok"]
    subject.stage_parked_hold("later ")
    assert subject.apply_live_surface_correction(selected["selection"], "three") == {"ok": False, "reason": "stale_revision"}


def test_local_correction_is_undoable_and_never_trains_a_global_rule():
    subject = manager(); subject.pause_draft(); subject.stage_parked_hold("wrong word ")
    segment = subject.draft_document.segments[0]
    chosen = subject.select_live_surface_word({"document_id": subject.draft_document.document_id,
        "revision": subject.draft_document.revision, "segment_id": segment.id, "start": 0, "end": 5})
    assert chosen["ok"]
    assert subject.apply_live_surface_correction(chosen["selection"], "right")["ok"]
    assert subject.dictate_pending_buffer == "right word "
    assert subject._do_scratch_that()
    assert subject.dictate_pending_buffer == "wrong word "


def test_no_match_keeps_spell_and_edit_and_picker_targets_are_large(qapp, surface_owner):
    source = Source(review(), [("s", "unusualtoken")])
    widget = surface_owner(LiveSurfaceWidget(source)); widget.show_word_picker(); qapp.processEvents()
    assert widget._picker_buttons[0].minimumHeight() >= MARK_SIZE
    selection = {"document_id": "d", "revision": 1, "segment_id": "s", "start": 0, "end": 12, "word": "unusualtoken"}
    widget.show(); widget.show_candidates(selection, []); qapp.processEvents()
    assert widget._keep.isVisible() and widget._spell.isVisible() and widget._edit.isVisible()
    widget._open_editor("spell")
    widget._editor.setText("A1-b'")
    assert widget._editor.text() == "A-b'"
    applied = []; widget.correction_apply_requested.connect(lambda item, text: applied.append(text))
    widget._apply_editor(); assert applied == ["A-b'"]


def test_scroll_back_stays_pinned_while_new_text_arrives_and_latest_resumes(qapp, surface_owner):
    long = "word " * 500
    source = Source(review(long), [("s", long)])
    widget = surface_owner(LiveSurfaceWidget(source)); widget.show(); qapp.processEvents()
    bar = widget._transcript.verticalScrollBar(); bar.setValue(0); widget._follow_latest = False; qapp.processEvents()
    assert not widget._follow_latest
    source._view = review(long + "new "); source.segments = [("s", long + "new ")]
    widget.refresh(animate=False); qapp.processEvents()
    assert not widget._follow_latest and widget._unread_segments == 1
    widget._latest_clicked(); assert widget._follow_latest and widget._unread_segments == 0


def test_controller_uses_candidate_service_and_identity_not_rendered_offsets(qapp, surface_owner):
    subject = manager(); subject.pause_draft(); subject.stage_parked_hold("hear hear ")
    app = SimpleNamespace(_session_mode_manager=subject, voice_training_window=None)
    controller = surface_owner(LiveSurfaceController(app))
    controller.sync_draft(subject); controller.drain()
    controller.start(); qapp.processEvents()
    segment = subject.draft_document.segments[0]
    controller._correct({"document_id": subject.draft_document.document_id,
        "revision": subject.draft_document.revision, "segment_id": segment.id, "start": 5, "end": 9})
    assert controller.widget._selection["word"] == "hear"
    assert controller.widget._keep.isVisible()


def test_flag_off_surface_correction_is_inert():
    subject = manager(live=False)
    assert subject.select_live_surface_word({}) == {"ok": False, "reason": "unavailable"}


def test_sent_receipt_copy_requires_an_empty_draft():
    subject = manager()
    assert subject.copy_sent_receipt_as_draft("sent receipt")["ok"]
    assert subject.dictate_pending_buffer == "sent receipt"
    assert subject.copy_sent_receipt_as_draft("another") == {"ok": False, "reason": "draft_pending"}


def test_scoped_voice_picker_phrases_are_inert_without_the_surface():
    signals = UtteranceSignals(has_contiguous_speech=True, transcript_confident=True,
                               compression_ratios=(1.0,))
    subject = manager(); calls = []
    subject.set_surface_review_action_fn(lambda action, value: calls.append((action, value)) or True)
    assert subject.dispatch_utterance("correct two", signals).kind == "surface_review_action"
    assert subject.dispatch_utterance("word 2", signals).kind == "surface_review_action"
    assert calls == [("correct", "two"), ("word", 2)]
    disabled = manager(live=False)
    assert disabled.dispatch_utterance("word 2", signals).kind == "dictate_staged"


def test_correction_survives_queued_draft_sync_and_cancel_closes_it(qapp, surface_owner):
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt
    from samsara.live_surface.model import PresentationState
    subject = manager(); subject.pause_draft(); subject.stage_parked_hold('stop here ')
    app = SimpleNamespace(_session_mode_manager=subject, voice_training_window=None)
    c = surface_owner(LiveSurfaceController(app))
    c.sync_draft(subject); c.drain(); c.start()
    segment = subject.draft_document.segments[0]
    c._correct({'document_id': subject.draft_document.document_id,
        'revision': subject.draft_document.revision, 'segment_id': segment.id, 'start': 0, 'end': 4})
    c.sync_draft(subject); c.drain(); QTest.qWait(150)
    assert c.view().presentation is PresentationState.CORRECTION
    assert c.widget._keep.isVisible() and c.widget._spell.isVisible() and c.widget._edit.isVisible()
    QTest.keyClick(c.widget, Qt.Key.Key_Escape); c.drain()
    assert c.widget._selection is None
    assert c.view().presentation is not PresentationState.CORRECTION


def test_candidate_and_empty_panes_remain_visible_in_every_source_form(qapp, surface_owner):
    from dataclasses import replace
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    selection = {'document_id': 'd', 'revision': 1, 'word': 'stop'}
    for form in VisibleForm:
        for candidates in ([], [SimpleNamespace(replacement=f'choice {n}') for n in range(6)]):
            w = surface_owner(LiveSurfaceWidget(replace(review(), form=form)))
            w.show(); w.show_candidates(selection, candidates); qapp.processEvents()
            assert w._more.isVisible() is (len(candidates) > 4)
            controls = (w._keep, w._spell, w._edit) + ((w._more,) if len(candidates) > 4 else ())
            for control in controls:
                assert control.isVisible() and w.rect().contains(control.geometry())
                assert control.width() >= 44 and control.height() >= 44
            if candidates:
                assert all(b.isVisible() for b in w._candidate_buttons)
                w._more.click(); assert w._candidate_buttons[0].text().startswith('5.')
                w._more.click(); assert w._candidate_buttons[0].text().startswith('1.')
            else:
                assert 'no suggestions' in w._correction_hint.text().lower()
                assert not any(b.isVisible() for b in w._picker_buttons)
                assert not any(b.isVisible() for b in w._candidate_buttons)
            w._keep.click(); assert w._selection is None
            w.show_word_picker(); qapp.processEvents()
            assert w._picker_buttons[0].isVisible() and w._cancel.isVisible()
            QTest.keyClick(w, Qt.Key.Key_Escape)
            assert not w._picker_active
            w.hide()


def test_finishing_keyboard_correction_restores_passive_window(qapp, surface_owner):
    from PySide6.QtCore import Qt
    w = surface_owner(LiveSurfaceWidget(review()))
    w.show(); w.show_candidates({'document_id': 'd', 'revision': 1, 'word': 'two'}, [])
    w._open_editor('edit')
    assert not (w.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
    w._cancel_editor(); w._keep.click()
    assert w.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_changed_document_closes_editor_and_restores_passive_capture(qapp, surface_owner):
    from dataclasses import replace
    from PySide6.QtCore import Qt
    w = surface_owner(LiveSurfaceWidget(review()))
    w.show(); w.show_candidates({'document_id': 'd', 'revision': 1, 'word': 'two'}, [])
    w._open_editor('edit')
    cancelled = []; w.correction_cancel_requested.connect(lambda: cancelled.append(True))
    w.refresh(replace(review(), revision=2), animate=False)
    assert w._selection is None and not w._editor.isVisible() and cancelled == [True]
    assert w.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
