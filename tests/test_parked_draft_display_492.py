"""492 kept-draft copy and measured content size; no app bootstrap."""
import pytest

from samsara.live_surface.model import (CaptureState, DocumentState, Lane, Notice,
    NoticeKind, PresentationState, SurfaceView, VisibleForm)
from samsara.ui.live_surface_qt import LiveSurfaceWidget


def parked(text="Synthetic.", *, capture=CaptureState.OFF, notice=None):
    return SurfaceView(VisibleForm.LIVE, capture, Lane.HANDS_FREE_DICTATE,
        DocumentState.PARKED_DRAFT, PresentationState.COMPACT,
        notice or Notice(NoticeKind.NONE, "", None), "synthetic492", 1, text, "", True)


def test_ended_session_names_kept_draft_and_available_clicks(qapp):
    w = LiveSurfaceWidget(parked())
    w.show(); qapp.processEvents()
    assert w._lane.text() == "Kept draft"
    # 517 A1: old visible "Insert text ... Clear" hint -> buttons alone.
    assert w._state.text() == "" and not w._state.isVisible()
    assert w._insert.isVisible() and w._clear.isVisible()
    w.hide(); w.deleteLater()


def test_empty_feedback_does_not_reserve_a_blank_band(qapp):
    w = LiveSurfaceWidget(parked())
    w.show(); qapp.processEvents()
    assert not w._band.isVisible()
    assert w._band_height == 0
    assert w.height() < 180
    assert w._transcript.height() >= w._transcript.fontMetrics().height()
    w.hide(); w.deleteLater()


def test_explicit_line_breaks_determine_parked_height(qapp):
    one = LiveSurfaceWidget(parked())
    four = LiveSurfaceWidget(parked("One.\nTwo.\nThree.\nFour."))
    one.show(); four.show(); qapp.processEvents()
    assert four.height() >= one.height() + 3 * four._transcript.fontMetrics().height() - 4
    assert four._transcript.verticalScrollBar().maximum() == 0
    one.hide(); four.hide(); one.deleteLater(); four.deleteLater()


def test_active_hands_free_keeps_its_live_label(qapp):
    w = LiveSurfaceWidget(parked(capture=CaptureState.HANDS_FREE_LISTENING))
    assert w._lane.text() == "Hands-free"
    w.hide(); w.deleteLater()


def test_failed_paste_notice_stays_visible_with_kept_text(qapp):
    w = LiveSurfaceWidget(parked(notice=Notice(NoticeKind.ERROR, "Synthetic paste failure", None)))
    w.show(); qapp.processEvents()
    # 517: old full-width failure band -> single-line error chip in header.
    assert not w._band.isVisible()
    assert w._chip.isVisible() and "failure" in w._chip.notice.message
    assert w._chip.toolTip() == "Synthetic paste failure"
    assert "Synthetic." in w._transcript.toPlainText()
    assert w._insert.isVisible() and w._clear.isVisible()
    w.hide(); w.deleteLater()


def test_real_exit_keeps_a_sendable_full_draft_without_resurrecting_after_clear(monkeypatch):
    import ast
    import logging
    import threading
    import time
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock
    from samsara import execution_policy, ava_command_session
    from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals

    source = Path(__file__).resolve().parents[1] / "dictation.py"
    cls = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body
               if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "exit_command_mode")
    ns = dict(time=time, logger=logging.getLogger("492.synthetic"),
              listening_display=SimpleNamespace(display_visible=lambda app: True))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), "exec"), ns)
    monkeypatch.setattr(execution_policy, "stop_all", Mock())
    monkeypatch.setattr(ava_command_session, "cancel_all", Mock())
    injected = []
    manager = SessionModeManager(abort_phrases=[], foreground_exe_resolver=lambda: "synthetic.exe",
        foreground_hwnd_resolver=lambda: 99, inject_fn=lambda text, *a: injected.append(text) or True,
        remove_chars_fn=Mock(), command_dispatch_fn=Mock(), agent_dispatch_fn=Mock(),
        live_surface_enabled=True, buffer_dictate_until_commit=True)
    manager.reset(SessionMode.DICTATE)
    manager.dispatch_utterance("Synthetic waiting draft.", UtteranceSignals(True, (1.2,), True))
    app = SimpleNamespace(_command_mode_lock=threading.Lock(), command_mode_active=True,
        _command_mode_session_start=time.monotonic()-5, config={"command_mode": {"mode": "toggle"}},
        _cancel_command_mode_inactivity_timer=Mock(), _session_mode_manager=manager,
        _release_wake_consumer=Mock(), _release_streaming_preview=Mock(),
        listening_indicator=Mock(), _schedule_ui=Mock(), live_surface=Mock(),
        recording=False, play_sound=Mock(), _show_outcome_chip=Mock())
    ns["exit_command_mode"](app)
    assert manager.has_parked_hold
    assert manager.retained_draft == ""  # no duplicate buffer to revive later
    assert manager.commit_pending_dictation().kind == "dictate_committed"
    assert injected == ["Synthetic waiting draft."]
    manager.reset(SessionMode.DICTATE)
    assert manager.dictate_pending_buffer == ""
from dataclasses import replace
from PySide6.QtCore import QAbstractAnimation
from samsara.live_surface.placement import Edge, Placement, Rect, Screen
from types import SimpleNamespace
from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.placement import placement_record


@pytest.mark.parametrize('failure', ['pause_draft', 'discard_retained_draft', 'reset', 'sync_draft'])
def test_session_exit_releases_preview_when_each_parking_step_raises(monkeypatch, failure):
    """Each new 492 call shares reset's unconditional preview-release protection."""
    import ast
    import logging
    import threading
    import time
    from pathlib import Path
    from unittest.mock import Mock
    from samsara import execution_policy, ava_command_session

    source = Path(__file__).resolve().parents[1] / 'dictation.py'
    cls = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
               if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'exit_command_mode')
    ns = dict(time=time, logger=logging.getLogger('492.synthetic'))
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), ns)
    monkeypatch.setattr(execution_policy, 'stop_all', Mock())
    monkeypatch.setattr(ava_command_session, 'cancel_all', Mock())
    manager = SimpleNamespace(_live_surface_enabled=True, dictate_pending_buffer='Synthetic.',
        has_parked_hold=True, pause_draft=Mock(), discard_retained_draft=Mock(), reset=Mock())
    surface = SimpleNamespace(sync_draft=Mock())
    getattr(surface if failure == 'sync_draft' else manager, failure).side_effect = RuntimeError(failure)
    app = SimpleNamespace(_command_mode_lock=threading.Lock(), command_mode_active=True,
        _command_mode_session_start=time.monotonic()-5, config={'command_mode': {'mode': 'toggle'}},
        _cancel_command_mode_inactivity_timer=Mock(), _session_mode_manager=manager,
        _release_wake_consumer=Mock(), _release_streaming_preview=Mock(), live_surface=surface)
    with pytest.raises(RuntimeError, match=failure):
        ns['exit_command_mode'](app)
    app._release_streaming_preview.assert_called_once_with()


def controller(qapp, screen, placement):
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True, 'placement': {
        'preferred_monitor': screen.id, 'monitors': {screen.id: placement_record(placement)}}}}})
    c = LiveSurfaceController(app)
    c._screens = lambda: [screen]
    c.widget = LiveSurfaceWidget(c, reduced_motion=True)
    return c


@pytest.mark.parametrize('form', [VisibleForm.LIVE, VisibleForm.REVIEW])
def test_wrapped_question_and_error_keep_review_content_and_clear(qapp, form):
    """456's wrapped bands and simultaneous question/error survive 492 sizing."""
    from samsara.live_surface.feedback import FeedbackKind, FeedbackPurpose
    question = Notice(NoticeKind.CONFIRMATION,
        'Synthetic question first line.\nSynthetic question second line.', None,
        feedback=FeedbackKind.QUESTION, purpose=FeedbackPurpose.QUESTION, owner='synthetic')
    error = Notice(NoticeKind.ERROR, 'Synthetic error first line.\nSynthetic error second line.',
                   None, feedback=FeedbackKind.ERROR)
    base = replace(parked('One.\nTwo.\nThree.\nFour.'), form=form)
    w = LiveSurfaceWidget(base)
    try:
        w.show(); w.ensurePolished(); w.refresh(base, animate=False); qapp.processEvents()
        height = w.height(); clear = w._clear.geometry(); transcript_height = w._transcript.height()
        w.refresh(replace(base, notice=question, secondary_notice=error), animate=False)
        qapp.processEvents()
        bands = [w._band, w._secondary_band]
        assert w.height() == height + sum(b.required_height(w.card_content_rect.width()) for b in bands)
        for band in bands:
            assert band.isVisible() and band.label.wordWrap()
            assert band.label.height() >= band.label.heightForWidth(band.label.width())
            assert band.rect().contains(band.label.geometry())
        assert w._clear.geometry() == clear and w._clear.isVisible()
        assert w._transcript.height() == transcript_height
        assert w._transcript.verticalScrollBar().maximum() == 0
        assert w._transcript.toPlainText() == base.text
    finally:
        w.hide(); w.deleteLater()


@pytest.mark.parametrize('form', [VisibleForm.STATUS, VisibleForm.LIVE, VisibleForm.REVIEW])
@pytest.mark.parametrize('edge', [Edge.TOP, Edge.BOTTOM, Edge.FREE])
def test_notice_resizes_only_away_from_dock_and_preserves_text_distance(qapp, form, edge):
    c = controller(qapp, Screen('synthetic', Rect(0, 0, 1920, 1040), True),
                   Placement(edge, .5, free_cx=.4, free_cy=.4))
    w = c.widget
    try:
        base = replace(parked('One.\nTwo.\nThree.'), form=form,
                       document=DocumentState.EMPTY if form is VisibleForm.STATUS else DocumentState.EDITABLE_DRAFT)
        w.refresh(base, animate=False); c._place_widget(); w.show(); qapp.processEvents()
        height = w.height(); top = w.y(); bottom = w.geometry().bottom()
        distance = bottom - (w.y()+w._transcript.y())
        assert w._band_height == 0 and not w._band.isVisible()
        w.set_reduced_motion(False)
        # 517: old one-line notice grew by >=56 DIP; only content needing
        # a row now grows away from its dock. Short receipts are header chips.
        w.refresh(replace(base, notice=Notice(NoticeKind.ERROR, 'Synthetic message\nSecond line', None)))
        c._place_widget()
        assert w._animation.state() == QAbstractAnimation.State.Running
        w._animation.setCurrentTime(w._animation.duration()//2); qapp.processEvents()
        if edge is Edge.BOTTOM and form is not VisibleForm.STATUS:
            assert w.geometry().bottom() == bottom
            assert bottom - (w.y()+w._transcript.y()) == distance
        w._animation.setCurrentTime(w._animation.duration()); qapp.processEvents()
        band = w._band.required_height(w.card_content_rect.width())
        assert w.height() == height + band
        if edge is Edge.BOTTOM:
            assert w.geometry().bottom() == bottom
            if form is not VisibleForm.STATUS:
                assert bottom - (w.y()+w._transcript.y()) == distance
        else:
            assert w.y() == top
        w.refresh(base); c._place_widget()
        w._animation.setCurrentTime(w._animation.duration()); qapp.processEvents()
        assert w.height() == height and not w._band.isVisible()
        assert w.y() == top
    finally:
        w.hide(); w.deleteLater()
