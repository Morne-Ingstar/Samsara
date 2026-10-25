"""Four Full-display repairs; synthetic speech and isolated Qt, no bootstrap."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QAbstractSlider

from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.model import (CaptureState, EventKind, Lane, LiveSurfaceModel,
                                        NoticeKind, SurfaceEvent, VisibleForm)
from samsara.live_surface.partials import CapturePartials
from samsara.ui.live_surface_qt import LiveSurfaceWidget, _capture_mark, _mark_mode


@pytest.fixture
def surface(qapp, monkeypatch):
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    clock = [0.0]
    controller = LiveSurfaceController(
        SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}}),
        model=LiveSurfaceModel(clock=lambda: clock[0]),
        widget_factory=lambda source: LiveSurfaceWidget(source, reduced_motion=True))
    controller.start()
    yield controller, LiveSurfaceIndicatorAdapter(controller), clock
    controller.close()
    qapp.processEvents()


# G3: pending outcomes keep their host-owned words until an explicit resolution.
@pytest.mark.parametrize('message', ['Waiting for confirmation', 'Save this alias?'])
@pytest.mark.parametrize('elapsed', [90.0, 3600.0])
def test_pending_question_stays_visible_and_accessible(surface, message, elapsed):
    c, indicator, clock = surface
    indicator.show_outcome(message, 'pending', None); c.drain()
    clock[0] = elapsed; c.refresh_expired()
    assert c.view().notice.kind is NoticeKind.PENDING
    assert c.view().notice.expires_at is None
    assert c.view().form is VisibleForm.STATUS and not c.view().can_collapse
    assert message in c.widget._band.label.text()
    assert message in c.widget.accessibleDescription()
    assert message in c.widget._band.accessibleDescription()


@pytest.mark.parametrize('resolution', ['answer', 'cancel', 'empty_label', 'replacement', 'new_question'])
def test_pending_question_leaves_immediately_when_resolved_or_replaced(surface, resolution):
    c, indicator, _ = surface
    indicator.show_outcome('Old question', 'pending', None); c.drain()
    if resolution in ('answer', 'cancel'):
        indicator.clear_outcome()
    elif resolution == 'empty_label':
        indicator.show_outcome('', 'pending', None)
    elif resolution == 'replacement':
        indicator.show_outcome('Completed', 'success', 1000)
    else:
        indicator.show_outcome('New question', 'pending', None)
    c.drain()
    assert ('Old question' in c.widget._band.label.text()) == (resolution == 'replacement')
    assert c.view().notice.kind is (NoticeKind.PENDING if resolution == 'replacement' else
                                   NoticeKind.PENDING if resolution == 'new_question' else NoticeKind.NONE)


def test_pending_question_survives_capture_and_delivery_without_an_answer(surface):
    c, indicator, clock = surface
    indicator.show_outcome('Waiting for confirmation', 'pending', None); c.drain()
    token = c.begin_capture(Lane.HOLD)
    partials = CapturePartials(c, Lane.HOLD, token)
    partials.partial('Synthetic input')
    partials.final('Synthetic final', delivered=True)
    partials.stop(); c.drain()
    clock[0] = 90.0; c.refresh_expired()
    assert c.view().notice.kind is NoticeKind.PENDING
    assert 'Waiting for confirmation' in c.widget._band.label.text()


@pytest.mark.parametrize('kind', ['error', 'success'])
@pytest.mark.parametrize('elapsed', [0.0, 3.0])
def test_pending_replaces_preceding_timed_receipt_even_without_a_timer_refresh(surface, kind, elapsed):
    c, indicator, clock = surface
    indicator.show_outcome('Previous receipt', kind, 1000); c.drain()
    clock[0] = elapsed
    indicator.show_outcome('Waiting for confirmation', 'pending', None); c.drain()
    clock[0] = 90.0; c.refresh_expired()
    assert c.view().notice.kind is NoticeKind.PENDING
    assert 'Waiting for confirmation' in c.widget._band.label.text()


def test_pending_does_not_replace_a_persistent_fault(surface):
    c, indicator, _ = surface
    c.notice(NoticeKind.ERROR, 'Persistent fault'); c.drain()
    indicator.show_outcome('Waiting for confirmation', 'pending', None); c.drain()
    assert c.view().notice.kind is NoticeKind.PENDING
    # 888b / 775: result errors are timed even with no caller TTL; questions replace them.
    assert c.view().secondary_notice is None


@pytest.mark.parametrize('kind,ttl_ms,expires', [('success', 3000, 3.0), ('warning', 3000, 3.0),
                                              ('error', 3000, 3.0), ('accent', 500, 0.5)])
def test_ordinary_outcome_still_expires_at_its_existing_limit(surface, kind, ttl_ms, expires):
    c, indicator, clock = surface
    from samsara.message_durations import message_duration_ms
    expires = message_duration_ms('Ordinary result', kind, ttl_ms) / 1000
    indicator.show_outcome('Ordinary result', kind, ttl_ms); c.drain()
    clock[0] = expires - 0.01
    assert c.view().notice.message == 'Ordinary result'
    clock[0] = expires + 0.01; c.refresh_expired()
    assert c.view().notice.kind is NoticeKind.NONE


# G1: snooze is independent of capture, lane, draft and outcome ownership.
@pytest.mark.parametrize('session', [None, 'HANDS FREE', 'COMMAND', 'AVA'])
def test_snooze_persists_then_restores_the_previous_state(surface, session):
    c, indicator, clock = surface
    indicator.set_session_mode(session); c.drain()
    before = c.view(); before_label = c.widget._state.text()
    indicator.set_snoozed(True); c.drain()
    clock[0] = 3600.0; c.refresh_expired()
    assert c.view().snoozed and c.view().form is VisibleForm.STATUS
    assert c.widget._state.text() == 'Snoozed'
    assert 'Snoozed' in c.widget.accessibleDescription()
    assert _capture_mark(c.view()) == ('idle', 'asleep')
    assert _mark_mode(c.view()) == 'idle'
    indicator.set_snoozed(False); c.drain()
    assert c.view() == before
    assert c.widget._state.text() == before_label


def test_snooze_keeps_draft_question_and_intervening_lane_changes(surface):
    c, indicator, _ = surface
    c.post(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id='d', revision=1, text='Synthetic draft'))
    indicator.show_outcome('Waiting for confirmation', 'pending', None)
    indicator.set_snoozed(True); c.drain()
    assert c.view().text == 'Synthetic draft'
    assert c.widget._state.text() == 'Snoozed' and c.widget._band.label.text() == 'Waiting for confirmation'
    indicator.set_mode('Continuous'); c.drain()
    indicator.set_snoozed(False); c.drain()
    assert c.view().lane is Lane.CONTINUOUS and c.view().text == 'Synthetic draft'
    assert c.view().notice.kind is NoticeKind.PENDING
    assert c.widget._lane.text() == 'Continuous' and c.widget._band.label.text() == 'Waiting for confirmation'


# G10: restore scrolling after Qt layout; resume on end/Latest/new utterance.
def start_long_draft(c):
    c.post(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id='d', revision=1, text='Earlier draft.'))
    token = c.begin_capture(Lane.HOLD)
    partials = CapturePartials(c, Lane.HOLD, token)
    partials.partial(('Synthetic speech ' * 180) + 'LASTWORD'); c.drain()
    return partials


def test_long_partial_after_existing_draft_follows_end_on_first_event_turn(surface, qapp):
    c, _, _ = surface
    start_long_draft(c); qapp.processEvents()
    bar = c.widget._transcript.verticalScrollBar()
    assert 'LASTWORD' in c.widget._transcript.toPlainText()
    assert bar.maximum() > 0 and bar.value() == bar.maximum()


@pytest.mark.parametrize('gesture', ['scrollbar', 'wheel', 'voice'])
def test_deliberate_scrollback_is_not_yanked_by_partials_or_repeated_refresh(surface, qapp, gesture):
    c, _, _ = surface
    partials = start_long_draft(c); qapp.processEvents()
    w = c.widget; bar = w._transcript.verticalScrollBar()
    if gesture == 'scrollbar':
        bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepSub)
    elif gesture == 'voice':
        w.scroll_draft('up')
    else:
        point = QPointF(w._transcript.viewport().rect().center())
        wheel = QWheelEvent(point, point, QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        qapp.sendEvent(w._transcript.viewport(), wheel)
    qapp.processEvents()
    position = bar.value()
    assert not w._follow_latest and 0 < position < bar.maximum()
    for n in range(3):
        partials.partial(('Synthetic speech ' * (200 + 20*n)) + 'NEWLASTWORD')
        c.drain(); qapp.processEvents()
        assert not w._follow_latest and bar.value() == position
    assert w._unread_segments == 3 and w._latest.isVisible()
    c.drain(); qapp.processEvents()
    assert bar.value() == position


@pytest.mark.parametrize('resume', ['end', 'latest', 'new_capture', 'new_speech'])
def test_following_resumes_at_end_latest_or_a_new_utterance(surface, qapp, resume):
    c, _, _ = surface
    partials = start_long_draft(c); qapp.processEvents()
    w = c.widget; bar = w._transcript.verticalScrollBar()
    w.scroll_draft('up'); qapp.processEvents()
    assert not w._follow_latest
    if resume == 'end':
        bar.triggerAction(QAbstractSlider.SliderAction.SliderToMaximum)
    elif resume == 'latest':
        w._latest_clicked()
    elif resume == 'new_capture':
        partials.stop(); c.drain(); qapp.processEvents()
        assert not w._follow_latest
        token = c.begin_capture(Lane.HOLD)
        partials = CapturePartials(c, Lane.HOLD, token)
    else:
        c.post(SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.HANDS_FREE_LISTENING))
        c.drain(); qapp.processEvents()
        assert not w._follow_latest
        c.post(SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.RECORDING))
    qapp.processEvents()
    partials.partial(('Synthetic speech ' * 240) + 'NEWLASTWORD'); c.drain(); qapp.processEvents()
    assert w._follow_latest and bar.value() == bar.maximum()
    assert w._unread_segments == 0


# G5: reuse existing mode names; do not collapse Toggle/Continuous/Hold.
@pytest.mark.parametrize('mode,lane,activity', [('Hold', Lane.HOLD, 'recording'),
                                              ('Toggle', Lane.TOGGLE, 'recording'),
                                              ('Continuous', Lane.CONTINUOUS, 'listening')])
def test_mode_name_and_accessible_description_match_the_active_capture(surface, mode, lane, activity):
    c, indicator, _ = surface
    indicator.set_mode(mode); indicator.set_listening(True); c.drain()
    assert c.view().lane is lane
    assert c.widget._lane.text() == mode and c.widget._state.text() == activity
    c.post(SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.RECORDING)); c.drain()
    assert c.widget._lane.text() == mode and c.widget._state.text() == 'recording'
    assert mode in c.widget.accessibleDescription() and 'recording' in c.widget.accessibleDescription()


def test_session_lane_still_wins_then_restores_the_selected_mode(surface):
    c, indicator, _ = surface
    indicator.set_mode('Toggle'); indicator.set_session_mode('COMMAND'); c.drain()
    assert c.widget._lane.text() == 'Command' and c.widget._state.text() == 'listening'
    indicator.set_mode('Continuous'); c.drain()
    assert c.view().lane is Lane.COMMAND
    indicator.set_session_mode(None); indicator.set_listening(True); c.drain()
    assert c.widget._lane.text() == 'Continuous' and c.widget._state.text() == 'listening'


def test_new_states_are_announced_without_reading_partial_speech(surface, monkeypatch):
    c, indicator, _ = surface
    spoken = []
    monkeypatch.setattr(c.widget, '_announce', lambda key, message: spoken.append(message))
    indicator.show_outcome('Waiting for confirmation', 'pending', None); c.drain()
    assert spoken[-1] == 'question: Waiting for confirmation'
    indicator.set_snoozed(True); c.drain()
    assert spoken[-1] == 'Hold — Snoozed'
    indicator.set_snoozed(False); c.drain()
    token = c.begin_capture(Lane.HOLD); c.drain()
    spoken.clear()
    partials = CapturePartials(c, Lane.HOLD, token)
    for text in ('Synthetic', 'Synthetic incoming', 'Synthetic incoming speech'):
        partials.partial(text); c.drain()
    assert spoken == []
