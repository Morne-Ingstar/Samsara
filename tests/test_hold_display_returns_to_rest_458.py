"""Successful streaming hold settles its progress chip; isolated gate only."""
from types import SimpleNamespace
from unittest.mock import Mock
import os
from pathlib import Path
import threading

import pytest

from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.model import (CaptureState, DocumentState, EventKind, Lane,
                                        LiveSurfaceModel, NoticeKind, SurfaceEvent, VisibleForm)
from samsara.live_surface.partials import CapturePartials
from samsara.ui.live_surface_qt import LiveSurfaceWidget
from samsara.message_durations import message_duration_ms


@pytest.fixture
def hold(qapp, monkeypatch, qt_widget_scope):
    # The gate supplies exact DictationApp definitions without its bootstrap.
    from dictation import DictationApp
    from samsara import streaming

    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    monkeypatch.setattr(streaming._user32, 'GetForegroundWindow', lambda: 17)
    clock, callbacks = [0.0], []
    app = SimpleNamespace(config={'auto_paste': True, 'ui': {'live_surface': {'enabled': True}}},
        _schedule_ui=lambda fn, *args: callbacks.append((fn, args)),
        _paste_preserving_clipboard=Mock(return_value=True), _log_history=Mock(),
        add_to_history=Mock(), _notify_main_window=Mock(), play_sound=Mock())
    for name in ('_show_hold_pending_chip', '_show_outcome_chip', '_show_dispatch_outcome_chip'):
        setattr(app, name, getattr(DictationApp, name).__get__(app))
    controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: clock[0]),
        widget_factory=lambda source: LiveSurfaceWidget(source, reduced_motion=True))
    app.live_surface = controller
    app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
    controller.start()
    token = controller.begin_capture(Lane.HOLD)
    partials = CapturePartials(controller, Lane.HOLD, token)
    session = streaming.StreamingSession.__new__(streaming.StreamingSession)
    session.app, session._live_partials = app, partials
    session._overlay = streaming._LiveSurfaceStreamingOverlay(partials)
    session._state_lock, session.cancel_event = threading.Lock(), threading.Event()
    session._parking_enabled, session._direct_paste, session._target_hwnd = True, False, 17
    session._mark_done = Mock()

    def flush():
        while callbacks:
            fn, args = callbacks.pop(0)
            fn(*args)
        controller.drain()
        qapp.processEvents()

    flush()
    yield SimpleNamespace(app=app, c=controller, clock=clock, flush=flush,
                          session=session, partials=partials, token=token)
    widget = controller.widget
    controller.close()
    with qt_widget_scope(widget):
        pass


def release_and_deliver(h):
    h.partials.partial('Synthetic preview.')
    h.app._schedule_ui(h.app.listening_indicator.set_listening, False)
    h.app._show_hold_pending_chip()
    # The streaming producer, not the legacy adapter, still owns this token.
    h.partials.partial('Synthetic last preview.')
    h.flush()
    h.session.on_final('Synthetic sentence.', duration_s=1.0)
    h.flush()


def save_full(h, name):
    destination = os.environ.get('SAMSARA_458_PROOF_DIR')
    if destination:
        h.c.widget.grab().save(str(Path(destination) / f'Full_{name}_two_seconds.png'))


def test_successful_streaming_hold_returns_to_rest_within_receipt_time(hold):
    h = hold
    release_and_deliver(h)
    h.app._paste_preserving_clipboard.assert_called_once_with('Synthetic sentence.')
    assert h.c.view().document is DocumentState.DELIVERED
    # 888b / 775: the delivered receipt now gets 2.5 seconds of reading time.
    h.clock[0] = message_duration_ms('typed', 'success') / 1000
    h.c.refresh_expired()
    save_full(h, 'delivered_hold')
    assert h.c.widget._state.text() == 'Idle'
    assert h.c.view().form is VisibleForm.MARK
    assert h.c.view().notice.kind is NoticeKind.NONE


@pytest.mark.parametrize('kind', [EventKind.PARTIAL, EventKind.CAPTURE_CHANGED])
def test_delivered_hold_rejects_late_partial_or_transcribing_update(hold, kind):
    h = hold
    release_and_deliver(h)
    h.c.post(SurfaceEvent(kind, capture_id=h.token, sequence=2 ** 31,
        lane=Lane.HOLD, capture=CaptureState.TRANSCRIBING, text='Synthetic late preview.'))
    h.flush()
    # 888b / 775: late producer work cannot outlive the current receipt.
    h.clock[0] = message_duration_ms('typed', 'success') / 1000
    h.c.refresh_expired()
    assert h.c.view().capture is CaptureState.OFF
    assert h.c.view().provisional_text == ''
    assert h.c.widget._state.text() == 'Idle'


def test_clear_and_next_hold_work_after_delivery(hold):
    h = hold
    release_and_deliver(h)
    h.c._clear()
    h.flush()
    token = h.c.begin_capture(Lane.HOLD)
    CapturePartials(h.c, Lane.HOLD, token).partial('Synthetic new preview.')
    h.flush()
    assert h.c.view().capture is CaptureState.RECORDING
    assert h.c.view().provisional_text == 'Synthetic new preview.'
    assert h.c.view().notice.kind is NoticeKind.NONE


@pytest.mark.parametrize('lane', [Lane.TOGGLE, Lane.HANDS_FREE_DICTATE])
def test_non_streaming_toggle_and_hands_free_finish_settle_their_result(hold, lane):
    from samsara.session_modes import DispatchOutcome

    h = hold
    h.partials.stop()
    indicator = h.app.listening_indicator
    if lane is Lane.TOGGLE:
        indicator.set_mode('Toggle')
        indicator.set_listening(True)
        indicator.set_listening(False)
        h.app._show_hold_pending_chip()
        h.app._schedule_ui(indicator.flash_success)
        h.app._show_outcome_chip('typed', 'success', 900)
    else:
        indicator.set_session_mode('HANDS FREE')
        h.app._show_dispatch_outcome_chip(DispatchOutcome(kind='dictate_committed'))
    h.flush()
    # Captured and typed are separate protected receipts. Each gets its
    # reading time, so settle at most these two finite deadlines.
    for _ in range(2):
        notice = h.c.view().notice
        if notice.kind is NoticeKind.NONE:
            break
        assert notice.expires_at is not None
        h.clock[0] = notice.expires_at + .001
        h.c.refresh_expired()
    assert h.c.view().notice.kind is NoticeKind.NONE
    assert h.c.view().capture is (CaptureState.OFF if lane is Lane.TOGGLE
                                  else CaptureState.HANDS_FREE_LISTENING)
    assert h.c.widget._state.text() == ('Idle' if lane is Lane.TOGGLE else 'listening')
    if lane is Lane.HANDS_FREE_DICTATE:
        assert h.c.widget._lane.text() == 'Hands-free'


def test_failed_paste_keeps_the_draft_and_does_not_claim_typed(hold):
    h = hold
    h.app._paste_preserving_clipboard.return_value = False
    manager = SimpleNamespace(draft_document=SimpleNamespace(
        document_id='synthetic-draft', revision=1, text='Synthetic sentence.', manual_commit=True),
        stage_parked_hold=Mock())
    h.app._ensure_session_mode_manager = lambda: manager
    release_and_deliver(h)
    h.clock[0] = 2.0
    h.c.refresh_expired()
    manager.stage_parked_hold.assert_called_once()
    assert h.c.view().document is DocumentState.PARKED_DRAFT
    assert h.c.view().text == 'Synthetic sentence.'
    assert h.c.view().notice.message != 'typed'
