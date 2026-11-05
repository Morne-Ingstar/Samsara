"""475: fake-clock reading time, state changes, questions and History."""
from types import SimpleNamespace

import pytest

from samsara.message_durations import message_duration_ms
from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.feedback import (Feedback, FeedbackKind, FeedbackPurpose,
                                         schedule_feedback)
from samsara.live_surface.model import (CaptureState, EventKind, Lane, LiveSurfaceModel,
                                        NoticeKind, SurfaceEvent, VisibleForm)
from samsara.ui.listening_indicator import ListeningIndicator


@pytest.fixture(params=['Full', 'Compact'])
def display(request, qapp, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}})
    if request.param == 'Full':
        controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: clock[0]))
        indicator = LiveSurfaceIndicatorAdapter(controller)
        flush = controller.drain
        text = lambda: controller.view().notice.message
        deadline = lambda: controller.view().notice.expires_at
        expire = controller.view
    else:
        controller = None
        indicator = ListeningIndicator()
        monkeypatch.setattr(indicator, '_now_ms', lambda: round(clock[0] * 1000))
        indicator.set_idle_animation(False)
        flush = lambda: None
        text = lambda: indicator._chip_label or ''
        deadline = lambda: indicator._chip_deadline
        expire = indicator._clear_outcome
    app.listening_indicator = indicator
    app._schedule_ui = lambda fn, *args: fn(*args)
    yield SimpleNamespace(name=request.param, clock=clock, app=app, c=controller,
                          i=indicator, flush=flush, text=text, deadline=deadline, expire=expire)
    if controller:
        controller.close()
    else:
        indicator.destroy()
        indicator.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize('kind', ['success', 'accent', 'warning', 'error'])
@pytest.mark.parametrize('length', [1, 50, 160])
def test_each_kind_stays_its_computed_reading_time(display, kind, length):
    # 1038: problems persist; information gets 2.5 s + 60 ms beyond character 20.
    d, label = display, 'x' * length
    expected = message_duration_ms(label, kind, 500)
    d.i.show_outcome(label, kind, 500); d.flush()
    assert d.deadline() == (None if expected is None else expected / 1000)
    d.clock[0] = 600 if expected is None else expected / 1000 - .001
    d.expire()
    assert d.text() == label
    if expected is None:
        d.i.dismiss_error(); d.flush()
    else:
        d.clock[0] = expected / 1000
        d.expire()
    assert d.text() == ''


@pytest.mark.parametrize('kind', ['success', 'accent', 'warning', 'error'])
def test_routine_state_clear_hide_and_typed_cannot_wipe_a_result(display, kind):
    # 1038: a new capture retires problems; routine clears preserve unread info.
    d, label = display, 'Synthetic command result'
    d.i.show_outcome(label, kind); d.flush()
    original_deadline = d.deadline()
    d.clock[0] = .5
    d.i.set_listening(True); d.i.set_listening(False)
    d.i.set_session_mode(None, None); d.i.clear_outcome(); d.i.hide(); d.flush()
    problem = kind in ('error', 'warning')
    assert d.text() == ('' if problem else label)
    assert d.deadline() == (None if problem else original_deadline)
    d.i.show_outcome('typed', 'success', 900)
    d.i.show_outcome('Captured', 'success', 1000)
    schedule_feedback(d.app, '...', 'pending', None, source='hold_pending'); d.flush()
    assert d.text() == ('typed' if problem else label)
    d.clock[0] = 3 if problem else original_deadline
    d.expire()
    assert d.text() == 'Captured'  # 1038: queued information gets its own reading time.
    d.clock[0] += 2.5
    d.expire()
    assert d.text() == '...' and d.deadline() is None
    d.i.clear_feedback('hold'); d.flush(); d.expire()
    assert d.text() == ''
    if d.c:
        assert d.c.view().form is VisibleForm.MARK
        assert d.c.view().capture is CaptureState.OFF
    else:
        assert not d.i.isVisible()


def test_newer_result_replaces_and_gets_its_own_deadline(display):
    # 775 preserves the node ID; replacement is immediate with fresh reading time.
    # 1038: an unrelated result waits for explicit dismissal of the refusal.
    d = display
    d.i.show_outcome('First refusal', 'warning'); d.flush()
    assert d.deadline() == 4.0  # 1140: a one-off warning times out
    d.clock[0] = 1.0
    d.i.show_outcome('New result', 'success'); d.flush()
    assert d.text() == 'First refusal' and d.deadline() == 4.0
    d.i.dismiss_error(); d.flush()
    assert d.text() == 'New result' and d.deadline() == 3.5
    d.clock[0] = 3.5; d.expire()
    assert not d.text()


def test_new_queued_action_notice_replaces_old_feedback(display):
    # A newly queued action replaces the previous message immediately.
    # 1038: a new user capture releases old feedback before its action notice.
    d = display
    d.i.show_outcome('Previous refusal', 'warning'); d.flush()
    d.clock[0] = .5
    d.i.set_listening(True); d.flush()
    d.i.show_feedback('Synthetic action queued', 'pending', None,
                      Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, 'command'))
    d.flush()
    assert d.text() == 'Synthetic action queued' and d.deadline() is None
    d.clock[0] = 600; d.expire()
    assert d.text() == 'Synthetic action queued' and d.deadline() is None


def test_question_stays_until_its_producer_answers_or_times_out(display):
    d, alive = display, [True]
    question = Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION,
                        'synthetic-question', alive=lambda: alive[0])
    d.i.show_outcome('Previous result', 'success'); d.flush()
    d.i.show_feedback('Confirm synthetic action?', 'warning', 8000, question); d.flush()
    assert d.text() == 'Confirm synthetic action?' and d.deadline() is None
    d.clock[0] = 90
    d.i.set_listening(True)
    d.i.set_listening(False)
    d.i.show_outcome('typed', 'success', 900) if d.c else schedule_feedback(d.app, 'typed', 'success', 900)
    d.flush(); d.expire()
    assert d.text() == 'Confirm synthetic action?'
    alive[0] = False
    if d.c:
        d.i.reconcile_interactions()
    d.expire()
    assert d.text() == ''


def test_full_delivery_and_lane_events_preserve_result_until_expiry():
    # 1038: info remains through lifecycle events until its full reading time.
    clock = [0.0]
    model = LiveSurfaceModel(clock=lambda: clock[0])
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.INFORMATION,
                               message='Synthetic result', ttl_s=.5))
    deadline = model.view().notice.expires_at
    clock[0] = .25
    for event in (SurfaceEvent(EventKind.CAPTURE_STARTED, capture_id='synthetic', lane=Lane.HOLD),
                  SurfaceEvent(EventKind.LANE_CHANGED, lane=Lane.COMMAND),
                  SurfaceEvent(EventKind.DELIVERY_FINISHED, text='Synthetic delivery'),
                  SurfaceEvent(EventKind.CAPTURE_STOPPED), SurfaceEvent(EventKind.NOTICE_CLEARED)):
        model.consume(event)
        assert model.view().notice.message == 'Synthetic result'
        assert model.view().notice.expires_at == deadline
    clock[0] = deadline
    assert model.view().notice.kind is NoticeKind.NONE
    assert model.view().form is VisibleForm.STATUS
    clock[0] = .25 + 2.5
    assert model.view().form is VisibleForm.MARK


def test_explicit_reply_resolves_a_question_without_a_liveness_callback(display):
    d = display
    d.i.show_feedback('Confirm synthetic action?', 'pending', None,
                      Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, 'command'))
    d.flush()
    d.i.show_feedback('Synthetic confirmation', 'success', 1800,
                      Feedback(FeedbackKind.SUCCESS, resolves='command'))
    d.flush()
    assert d.text() == 'Synthetic confirmation' and d.deadline() is not None


def test_running_operation_with_liveness_does_not_expire_like_a_result(display):
    d, alive = display, [True]
    d.i.show_feedback('Synthetic work running', 'accent', None,
        Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, 'synthetic-work', alive=lambda: alive[0]))
    d.flush()
    d.clock[0] = 30
    d.expire()
    assert d.text() == 'Synthetic work running'
    alive[0] = False
    if d.c:
        d.i.reconcile_interactions()
    d.expire()
    assert not d.text()


@pytest.mark.parametrize('owner', ['', 'command'])
def test_unrelated_result_does_not_answer_a_question(display, owner):
    d = display
    d.i.show_feedback('Confirm synthetic action?', 'pending', None,
                      Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, owner))
    d.flush()
    d.i.show_feedback('Unrelated result', 'success', 1800, Feedback(FeedbackKind.SUCCESS))
    d.flush()
    assert d.text() == 'Confirm synthetic action?' and d.deadline() is None


def test_routine_receipts_and_persistent_status_contracts():
    assert message_duration_ms('typed', 'success', 900) == 2500
    assert message_duration_ms('Captured', 'success', 1000) == 2500
    assert message_duration_ms('Persistent fault', 'error', None) is None  # 1038: persistent result errors.
    assert message_duration_ms('Question', 'warning', 8000, purpose='question') is None
    assert message_duration_ms('Short result', 'success', 0) == 2500


def test_result_and_refusal_remain_searchable_in_history_after_expiry(display, tmp_path):
    from samsara.history import HistoryManager
    from samsara.ui.history_view import FILTER_ALL, _matches_type_filter
    d = display
    db = HistoryManager(tmp_path / 'synthetic_history.db')
    d.app.history_db = db
    try:
        schedule_feedback(d.app, 'Synthetic refusal: nothing ran', 'warning', 1800)
        d.flush()
        d.clock[0] = 9
        d.expire()
        assert d.text() == ''  # 1140: a one-off warning times out.
        d.i.dismiss_error(); d.flush()
        rows = db.recent_windowed(search='Synthetic refusal')
        assert len(rows) == 1
        assert rows[0]['display_text'] == 'Synthetic refusal: nothing ran'
        assert rows[0]['status'] == 'warning'
        assert _matches_type_filter(rows[0], FILTER_ALL)
        schedule_feedback(d.app, 'Synthetic result', 'success', 1800); d.flush()
        schedule_feedback(d.app, 'typed', 'success', 900); d.flush()
        assert len(db.recent()) == 2  # Exactly one row per message, no state receipt.
    finally:
        db.close()


def test_full_direct_notice_is_retained_without_a_chip_handoff(monkeypatch, tmp_path):
    from samsara.history import HistoryManager
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    db = HistoryManager(tmp_path / 'direct_notice.db')
    try:
        c = LiveSurfaceController(SimpleNamespace(config={}, history_db=db))
        c.notice(NoticeKind.WARNING, 'Synthetic direct refusal'); c.drain()
        assert db.recent()[0]['display_text'] == 'Synthetic direct refusal'
    finally:
        db.close()


def test_both_widgets_keep_feedback_visible_when_returning_to_rest(qapp, monkeypatch):
    import os
    from pathlib import Path
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    clock, label = [0.0], 'Refused: synthetic action'
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}})
    c = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: clock[0]),
        widget_factory=lambda source: LiveSurfaceWidget(source, reduced_motion=True))
    full, compact = LiveSurfaceIndicatorAdapter(c), ListeningIndicator()
    monkeypatch.setattr(compact, '_now_ms', lambda: round(clock[0] * 1000))
    compact.set_idle_animation(False)
    try:
        c.start()
        for indicator in (full, compact):
            indicator.show_outcome(label, 'warning', 500)
        c.drain()
        clock[0] = 1
        full.set_listening(False); full.clear_outcome(); full.hide(); c.drain()
        compact.set_listening(False); compact.clear_outcome(); compact.hide()
        qapp.processEvents()
        # 517: old band label equality -> visible header chip with full tooltip.
        assert c.widget.isVisible() and c.widget._chip.isVisible()
        assert c.widget._chip.notice.message == label and c.widget._chip.toolTip() == label
        assert c.widget._state.text() == 'Idle'
        assert compact.isVisible() and compact._active_chip() == (label, 'warning')
        destination = os.environ.get('SAMSARA_475_PROOF_DIR')
        if destination:
            directory = Path(destination)
            directory.mkdir(parents=True, exist_ok=True)
            c.widget.grab().save(str(directory / 'Full_refusal_at_1_second.png'))
            compact.grab().save(str(directory / 'Compact_refusal_at_1_second.png'))
        clock[0] = 4
        c.refresh_expired(); compact._clear_outcome(); qapp.processEvents()
        assert c.view().notice.message != label and compact._chip_label != label  # 1140: warning timed out
        full.dismiss_error(); compact.dismiss_error(); c.drain()
        assert c.view().form is VisibleForm.MARK and not c.widget._band.isVisible()
        assert not compact.isVisible()
    finally:
        c.close(); compact.destroy(); compact.deleteLater(); qapp.processEvents()
