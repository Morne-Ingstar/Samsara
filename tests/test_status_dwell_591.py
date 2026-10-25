"""Readable status lifetimes through the real Full and Compact entry points."""
from types import SimpleNamespace

import pytest

from samsara.message_durations import message_duration_ms, message_should_wait
from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.feedback import Feedback, FeedbackKind, FeedbackPurpose
from samsara.live_surface.model import (
    CaptureState, DocumentState, EventKind, Lane, LiveSurfaceModel, SurfaceEvent,
)
from samsara.ui.listening_indicator import ListeningIndicator


@pytest.fixture(params=['Full', 'Compact'])
def display(request, qapp, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    controller = None
    if request.param == 'Full':
        controller = LiveSurfaceController(
            SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}}),
            model=LiveSurfaceModel(clock=lambda: clock[0]))
        indicator = LiveSurfaceIndicatorAdapter(controller)
        flush = controller.drain
        text = lambda: controller.view().notice.message
        deadline = lambda: controller.view().notice.expires_at

        def expire():
            indicator.reconcile_interactions()
            controller.view()
    else:
        indicator = ListeningIndicator()
        indicator.set_idle_animation(False)
        monkeypatch.setattr(indicator, '_now_ms', lambda: round(clock[0] * 1000))
        flush = lambda: None
        text = lambda: indicator._chip_label or ''
        deadline = lambda: indicator._chip_deadline
        expire = indicator._clear_outcome

    def show(label, kind='success', ttl=10, feedback=None):
        if feedback is None:
            indicator.show_outcome(label, kind, ttl)
        else:
            indicator.show_feedback(label, kind, ttl, feedback)
        flush()

    yield SimpleNamespace(clock=clock, i=indicator, c=controller, show=show,
                          flush=flush, text=text, deadline=deadline, expire=expire)
    if controller:
        controller.close()
    else:
        indicator.destroy()
        indicator.deleteLater()
    qapp.processEvents()


# 888b / 775: retain historical node IDs while pinning the approved 2.5-8 s formula.
@pytest.mark.parametrize('length, expected', [(1, 2500), (19, 2500), (20, 2500),
                                            (50, 3000), (77, 4620), (78, 4680), (200, 8000)],
                         ids=['1-1500', '19-1500', '20-1550', '50-3350', '77-4970', '78-5000', '200-5000'])
def test_exact_shared_formula(length, expected):
    for kind in ('success', 'accent', 'warning', 'error'):
        assert message_duration_ms('x' * length, kind, 1) == expected


@pytest.mark.parametrize('label', ['typed', 'Captured', 'Synthetic receipt ' * 6])
def test_receipt_survives_clear_hide_and_expires_at_its_dwell(display, label):
    d = display
    d.show(label)
    end = message_duration_ms(label, 'success') / 1000
    assert d.deadline() == end
    d.clock[0] = .1
    d.i.clear_outcome()
    d.i.hide()
    d.flush()
    d.clock[0] = end - .001
    d.expire()
    assert d.text() == label
    d.clock[0] = end
    d.expire()
    assert d.text() == ''


def test_only_latest_waiter_shows_with_fresh_reading_time(display):
    # 888b / 775: each new result replaces the preceding one on arrival.
    d = display
    d.show('First receipt')
    d.clock[0] = .2
    d.show('Superseded waiter')
    assert d.text() == 'Superseded waiter'
    d.clock[0] = .4
    d.show('Latest receipt')
    assert d.text() == 'Latest receipt'
    end = .4 + message_duration_ms('Latest receipt', 'success') / 1000
    assert d.deadline() == end
    d.clock[0] = end - .001
    d.expire()
    assert d.text() == 'Latest receipt'
    d.clock[0] = end
    d.expire()
    assert d.text() == ''


def test_error_interrupts_and_lower_priority_waits(display):
    # 888b / 775: newer results replace errors as well as ordinary receipts.
    d = display
    d.show('typed')
    d.clock[0] = .1
    d.show('Synthetic error', 'error')
    assert d.text() == 'Synthetic error'
    d.clock[0] = .2
    d.show('Captured')
    assert d.text() == 'Captured'
    end = .2 + message_duration_ms('Captured', 'success') / 1000
    assert d.deadline() == end
    d.clock[0] = end
    d.expire()
    assert d.text() == ''


def test_arrival_after_missed_timer_keeps_waiters_in_order(display):
    # 888b / 775: delayed expiry must never resurrect a replaced receipt.
    d = display
    d.show('First receipt')
    end = d.deadline()
    d.clock[0] = .1
    d.show('Waiting receipt')
    assert d.text() == 'Waiting receipt'
    d.clock[0] = end + .2
    d.show('Latest arrival')
    assert d.text() == 'Latest arrival'
    d.clock[0] = d.deadline()
    d.expire()
    assert d.text() == ''


def test_full_queued_feedback_is_published_once_on_arrival():
    # 888b / 775: visible feedback and the published event advance together.
    clock = [0.0]
    model = LiveSurfaceModel(clock=lambda: clock[0])
    from samsara.live_surface.model import NoticeKind
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.SUCCESS, message='First receipt'))
    first_revision = model.view().feedback_revision
    clock[0] = .1
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.SUCCESS, message='Waiting receipt'))
    view = model.view()
    assert view.notice.message == view.feedback_event.message == 'Waiting receipt'
    assert view.feedback_revision == first_revision + 1
    clock[0] = view.notice.expires_at
    expired = model.view()
    assert expired.notice.kind is NoticeKind.NONE
    assert expired.feedback_revision == view.feedback_revision


def test_recording_changes_immediately_while_receipt_remains(display):
    d = display
    d.show('typed')
    d.clock[0] = .1
    d.i.set_listening(True)
    d.flush()
    assert (d.c.view().capture is CaptureState.RECORDING if d.c else d.i._listening)
    assert d.text() == 'typed'
    d.i.set_listening(False)
    d.flush()
    assert (d.c.view().capture is CaptureState.OFF if d.c else not d.i._listening)
    assert d.text() == 'typed'


def test_capture_message_can_interrupt_error():
    assert not message_should_wait('error', 'live', protected=True)
    assert message_should_wait('error', 'success', protected=True)
    assert message_should_wait('success', 'success', protected=True)
    assert not message_should_wait('success', 'error', protected=True)


def test_cancelled_queued_progress_never_reappears(display):
    d = display
    d.show('typed')
    end = d.deadline()
    d.show('Synthetic processing', 'pending', None,
           Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, 'hold'))
    # 888b / 775: progress replaces the result immediately; cancellation removes it.
    assert d.text() == 'Synthetic processing'
    d.i.clear_feedback('hold')
    d.flush()
    d.clock[0] = end
    d.expire()
    assert d.text() == ''


def test_dead_queued_producer_does_not_show(display):
    d, alive = display, [True]
    d.show('Synthetic warning', 'warning')
    end = d.deadline()
    d.show('Synthetic work', 'accent', None,
           Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, 'work', alive=lambda: alive[0]))
    alive[0] = False
    d.clock[0] = end
    d.expire()
    assert d.text() == ''


def test_question_keeps_its_producer_owned_lifetime(display):
    d = display
    d.show('Synthetic question?', 'pending', None,
           Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, 'question'))
    d.clock[0] = 100
    d.expire()
    assert d.text() == 'Synthetic question?'
    d.i.clear_feedback('question')
    d.flush()
    d.expire()
    assert d.text() == ''


def test_full_document_receipt_dwell_does_not_delay_document_or_capture():
    clock = [0.0]
    model = LiveSurfaceModel(clock=lambda: clock[0])
    model.consume(SurfaceEvent(EventKind.DELIVERY_FINISHED, text='Synthetic delivered content'))
    assert model.view().document is DocumentState.DELIVERED
    # 888b / 775: delivery document and receipt share the 2.5-second floor.
    clock[0] = 2.499
    model.consume(SurfaceEvent(EventKind.NOTICE_CLEARED))
    assert model.view().document is DocumentState.DELIVERED
    clock[0] = 2.5
    assert model.view().document is DocumentState.EMPTY
    model.consume(SurfaceEvent(EventKind.DELIVERY_FINISHED, text='Synthetic delivered content'))
    clock[0] = 2.6
    model.consume(SurfaceEvent(EventKind.CAPTURE_STARTED, capture_id='new', lane=Lane.HOLD))
    assert model.view().capture is CaptureState.RECORDING
    assert model.view().document is DocumentState.EMPTY


def test_history_footer_coalesces_and_defers_refresh(qapp, monkeypatch):
    from samsara.ui import history_view as hv
    clock = [0.0]
    monkeypatch.setattr('samsara.message_durations.monotonic', lambda: clock[0])
    window = hv.HistoryView(None)
    try:
        window._show_toast('Synthetic confirmation')
        end = window._toast.deadline
        clock[0] = .1
        window._show_toast('Superseded receipt')
        window._show_toast('Latest receipt')
        window._restore_status()
        assert window._status_lbl.text() == 'Synthetic confirmation'
        clock[0] = end
        window._restore_status()
        assert window._status_lbl.text() == 'Latest receipt'
        window._show_toast('Synthetic failure', success=False)
        assert window._status_lbl.text() == 'Synthetic failure'
        clock[0] = window._toast.deadline
        window._restore_status()
        assert window._status_lbl.text() != 'Synthetic failure'
    finally:
        window._toast_timer.stop()
        window.close()
        window.deleteLater()
    qapp.processEvents()


def test_history_arrival_after_missed_timer_keeps_waiters_in_order(qapp, monkeypatch):
    from samsara.ui import history_view as hv
    clock = [0.0]
    monkeypatch.setattr('samsara.message_durations.monotonic', lambda: clock[0])
    window = hv.HistoryView(None)
    try:
        window._show_toast('First receipt')
        end = window._toast.deadline
        clock[0] = .1
        window._show_toast('Waiting receipt')
        clock[0] = end + .2
        window._show_toast('Latest arrival')
        assert window._status_lbl.text() == 'Waiting receipt'
        assert window._toast.deadline == clock[0] + message_duration_ms(window._status_lbl.text(), 'success') / 1000
        clock[0] = window._toast.deadline
        window._restore_status()
        assert window._status_lbl.text() == 'Latest arrival'
        assert window._toast.deadline == clock[0] + message_duration_ms(window._status_lbl.text(), 'success') / 1000
        clock[0] = window._toast.deadline
        window._restore_status()
        assert window._status_lbl.text() != 'Latest arrival'
    finally:
        window._toast_timer.stop()
        window.close()
        window.deleteLater()
    qapp.processEvents()
