"""Full feedback geometry and lifetimes, synthetic input and no child process."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible

from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.feedback import FeedbackKind, FeedbackPurpose
from samsara.live_surface.model import (CaptureState, DocumentState, EventKind, Lane,
    LiveSurfaceModel, Notice, NoticeKind, PresentationState, SurfaceEvent, SurfaceView, VisibleForm)
from samsara.live_surface.placement import Edge, Placement, Rect, Screen, placement_record
from samsara.message_durations import message_duration_ms
from samsara.ui import theme
from samsara.ui.live_surface_qt import HEADER_HEIGHT, LiveSurfaceWidget


def view(*, notice=None, form=VisibleForm.LIVE, parked=False):
    return SurfaceView(form, CaptureState.OFF if parked else CaptureState.HANDS_FREE_LISTENING,
        Lane.HANDS_FREE_DICTATE, DocumentState.PARKED_DRAFT if parked else DocumentState.EDITABLE_DRAFT,
        PresentationState.COMPACT, notice or Notice(NoticeKind.NONE, '', None),
        'synthetic517', 1, 'Synthetic draft.\nA second line.', '', True)


@pytest.fixture
def widgets(qapp):
    created = []
    previous = theme.active_theme()
    def make(value):
        w = LiveSurfaceWidget(value, reduced_motion=True)
        w.show(); w.ensurePolished(); w.refresh(value, animate=False); qapp.processEvents()
        created.append(w)
        return w
    yield make
    for w in created:
        w.hide(); w.deleteLater()
    qapp.processEvents(); theme.set_theme(previous)


def test_staged_echo_creates_no_chip_or_visible_notice(widgets, qapp):
    echo = Notice(NoticeKind.INFORMATION, 'staged', None, feedback=FeedbackKind.INFORMATION, owner='draft')
    base = view()
    w = widgets(base); geometry = w.geometry(); header = w._lane.geometry()
    w.refresh(replace(base, notice=echo), animate=False); qapp.processEvents()
    assert w._state.text() == 'staged'
    assert w._chip is None and not w.findChildren(type(w._state), 'surfaceChip')
    assert w._band.notice is None and not w._band.isVisible()
    assert not w._secondary_band.isVisible() and w._band_height == 0
    assert w.geometry() == geometry and w._lane.geometry() == header


@pytest.mark.parametrize('edge', [Edge.TOP, Edge.BOTTOM, Edge.LEFT, Edge.RIGHT])
@pytest.mark.parametrize('t', [0, 1])
def test_receipt_and_expiry_leave_every_dock_and_header_fixed(widgets, qapp, edge, t):
    screen = Screen('synthetic', Rect(-1920, 0, 1920, 1040), True)
    placement = Placement(edge, t)
    c = LiveSurfaceController(SimpleNamespace(config={'ui': {'live_surface': {'enabled': True,
        'placement': {'preferred_monitor': screen.id, 'monitors': {screen.id: placement_record(placement)}}}}}))
    c._screens = lambda: [screen]
    base = view(); c.widget = w = widgets(base); c._place_widget(); qapp.processEvents()
    before = w.geometry(); mark = w.mapToGlobal(w._mark.pos())
    clear = w.mapToGlobal(w._clear.pos()); transcript = w._transcript.geometry()
    header = (w._lane.height(), w._state.height(), w._clear.height())
    w.set_reduced_motion(False)
    clock = [0.0]; model = LiveSurfaceModel(clock=lambda: clock[0])
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.SUCCESS,
                               feedback=FeedbackKind.SUCCESS, message='typed', ttl_s=1.8))
    w.refresh(replace(base, notice=model.view().notice)); c._place_widget(); qapp.processEvents()
    assert w.geometry() == before and w._animation is None
    assert w._chip.isVisible() and w._chip.height() == 26
    assert w.mapToGlobal(w._mark.pos()) == mark and w.mapToGlobal(w._clear.pos()) == clear
    assert w._transcript.geometry() == transcript
    assert (w._lane.height(), w._state.height(), w._clear.height()) == header
    clock[0] = 1.8
    w.refresh(replace(base, notice=model.view().notice)); c._place_widget(); qapp.processEvents()
    assert w._chip is None and w.geometry() == before
    assert w.mapToGlobal(w._mark.pos()) == mark and w.mapToGlobal(w._clear.pos()) == clear
    assert w._transcript.geometry() == transcript


@pytest.mark.parametrize('form,parked,latest', [(VisibleForm.STATUS, False, False),
    (VisibleForm.LIVE, False, True), (VisibleForm.LIVE, True, True), (VisibleForm.REVIEW, True, False)])
@pytest.mark.parametrize('palette', ['dark', 'light'])
def test_chip_elides_between_title_and_real_buttons_at_narrow_width(widgets, qapp, form, parked, latest, palette):
    # The external gate also runs this file with real QT_SCALE_FACTOR=1.5.
    theme.set_theme(palette)
    message = 'Synthetic result ' + ('with many words ' * 15) + 'ending'
    notice = Notice(NoticeKind.SUCCESS, message, 8, feedback=FeedbackKind.SUCCESS)
    w = widgets(view(notice=notice, form=form, parked=parked))
    if latest:
        w._follow_latest = False; w._unread_segments = 12; w._layout_children(); qapp.processEvents()
    chip = w._chip
    assert w.width() == (360 if form is VisibleForm.STATUS else 500)
    assert chip.isVisible() and chip.height() == 26
    assert w.card_content_rect.contains(chip.geometry())
    assert chip.x() > w._lane.geometry().right()
    assert chip.y() >= w.card_content_rect.y()
    assert chip.geometry().bottom() < w.card_content_rect.y()+HEADER_HEIGHT
    for button in (*w._controls, w._insert, w._latest):
        if button.isVisible():
            assert not chip.geometry().intersects(button.geometry())
    assert chip.label.text() == chip.label.fontMetrics().elidedText(message,
        Qt.TextElideMode.ElideRight, chip.label.width())
    assert '…' in chip.label.text()
    assert chip.toolTip() == message and chip.label.toolTip() == message
    assert chip.accessibleName() == message and chip.label.accessibleName() == message
    assert not chip.label.font().underline()
    assert theme.contrast_ratio(theme.TEXT_PRIMARY, theme.BG1) >= 4.5


@pytest.mark.parametrize('kind,feedback,purpose', [
    (NoticeKind.ERROR, FeedbackKind.ERROR, FeedbackPurpose.RESULT),
    (NoticeKind.WARNING, FeedbackKind.WARNING, FeedbackPurpose.RESULT),
    (NoticeKind.CONFIRMATION, FeedbackKind.QUESTION, FeedbackPurpose.QUESTION),
    (NoticeKind.ALIAS_OFFER, FeedbackKind.QUESTION, FeedbackPurpose.QUESTION),
    (NoticeKind.PENDING, FeedbackKind.QUESTION, FeedbackPurpose.QUESTION)])
def test_notices_needing_room_keep_all_text_and_existing_controls(widgets, qapp, kind, feedback, purpose):
    message = 'Synthetic first line.\n' + 'A longer second line with enough words to wrap. '*4
    base = view(parked=True)
    w = widgets(base); clear = w._clear.geometry(); insert = w._insert.geometry()
    notice = Notice(kind, message, None, feedback=feedback, purpose=purpose)
    w.refresh(replace(base, notice=notice), animate=False); qapp.processEvents()
    assert w._chip is None and w._band.isVisible()
    assert message in w._band.label.text() and w._band.label.wordWrap()
    assert w._band.label.height() >= w._band.label.heightForWidth(w._band.label.width())
    assert w._band.rect().contains(w._band.label.geometry())
    assert w._band.label.font().pixelSize() == theme.TYPE_BODY
    assert w._band_height == w._band.required_height(w.card_content_rect.width())
    assert w._clear.geometry() == clear and w._insert.geometry() == insert
    assert w._clear.isVisible() and w._insert.isVisible()
    assert w._state.text() == '' and not w._state.isVisible()
    assert message in w.accessibleDescription()
    if feedback in (FeedbackKind.ERROR, FeedbackKind.WARNING):
        assert w._band.label.text().startswith(feedback.value + ': ')


def test_single_line_question_still_has_a_row(widgets):
    notice = Notice(NoticeKind.CONFIRMATION, 'Synthetic question?', None,
        feedback=FeedbackKind.QUESTION, purpose=FeedbackPurpose.QUESTION)
    w = widgets(view(notice=notice))
    assert w._chip is None and w._band.isVisible() and w._clear.isVisible()


@pytest.mark.parametrize('form', [VisibleForm.LIVE, VisibleForm.REVIEW])
def test_kept_draft_has_only_title_actions_and_text(widgets, form):
    w = widgets(view(parked=True, form=form))
    assert w._lane.text() == 'Kept draft'
    assert w._state.text() == '' and not w._state.isVisible()
    assert w._insert.isVisible() and w._clear.isVisible()
    assert w._insert.accessibleName() == 'Insert waiting text'
    assert w._clear.accessibleName() == 'Clear draft'
    assert w._transcript.toPlainText() == w.view.text
    from samsara.ui.live_surface_qt import REGION_GAP
    assert w._transcript.y() == w.card_content_rect.y()+HEADER_HEIGHT+REGION_GAP


@pytest.mark.parametrize('message,feedback,ttl,purpose', [
    ('typed', FeedbackKind.SUCCESS, 1.8, FeedbackPurpose.RESULT),
    ('Captured', FeedbackKind.SUCCESS, .9, FeedbackPurpose.RESULT),
    ('staged', FeedbackKind.INFORMATION, 1.8, FeedbackPurpose.RESULT),
    ('Synthetic result', FeedbackKind.SUCCESS, 1.8, FeedbackPurpose.RESULT),
    ('Synthetic warning', FeedbackKind.WARNING, 1.8, FeedbackPurpose.RESULT),
    ('Synthetic error', FeedbackKind.ERROR, 1.8, FeedbackPurpose.RESULT),
    ('Synthetic question?', FeedbackKind.QUESTION, None, FeedbackPurpose.QUESTION),
    ('Synthetic running', FeedbackKind.RUNNING, None, FeedbackPurpose.RUNNING)])
def test_presentation_keeps_model_deadlines(widgets, qapp, message, feedback, ttl, purpose):
    clock = [0.0]; model = LiveSurfaceModel(clock=lambda: clock[0])
    kind = NoticeKind.CONFIRMATION if purpose is FeedbackPurpose.QUESTION else NoticeKind.INFORMATION
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=kind, message=message,
        feedback=feedback, purpose=purpose, ttl_s=ttl, persistent=ttl is None))
    notice = model.view().notice
    duration = message_duration_ms(message, feedback, None if ttl is None else ttl*1000, purpose=purpose)
    assert notice.expires_at == (None if duration is None else duration/1000)
    w = widgets(view(notice=notice))
    assert w.view.notice is notice
    if duration is not None:
        clock[0] = duration/1000 - .001
        assert model.view().notice is notice
        clock[0] = duration/1000
        w.refresh(replace(w.view, notice=model.view().notice), animate=False); qapp.processEvents()
        assert w._chip is None and w._band_height == 0


def test_echo_and_elided_receipt_keep_full_once_only_announcements(widgets, qapp, monkeypatch):
    messages = []
    monkeypatch.setattr(QAccessible, 'updateAccessibility', lambda event: messages.append(event.message()))
    w = widgets(view())
    model = LiveSurfaceModel(clock=lambda: 0.0)
    for text, feedback in [('staged', FeedbackKind.INFORMATION), ('Synthetic long result '*20, FeedbackKind.SUCCESS)]:
        model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.INFORMATION, message=text, feedback=feedback))
        feedback_view = model.view()
        current = replace(w.view, notice=feedback_view.notice, feedback_event=feedback_view.feedback_event,
                          feedback_revision=feedback_view.feedback_revision)
        w.refresh(current, animate=False); qapp.processEvents()
        assert messages[-1] == ('information' if feedback is FeedbackKind.INFORMATION else 'success') + ': ' + text
        count = len(messages)
        w.refresh(current, animate=False); qapp.processEvents()
        assert len(messages) == count
