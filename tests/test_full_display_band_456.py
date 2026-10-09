"""456: Full's agreed S layout and structured producer contract; synthetic input."""
import ast
import collections
import logging
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QAccessible, QPalette

from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.feedback import (DEFAULT_TTL_MS, FILL_TOKENS, GLYPHS, Feedback,
    FeedbackKind, FeedbackPurpose, alias_handoff, feedback_for, producer_feedback, schedule_feedback)
from samsara.live_surface.model import CaptureState, EventKind, Lane, LiveSurfaceModel, NoticeKind, SurfaceEvent
from samsara.live_surface.placement import Edge, Placement, Rect, Screen, placement_record
from samsara.ui import theme
from samsara.ui.live_surface_qt import LiveSurfaceWidget


@pytest.fixture
def surface(qapp, monkeypatch):
    previous = theme.active_theme()
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    clock = [0.0]
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}})
    c = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: clock[0]),
        widget_factory=lambda src: LiveSurfaceWidget(src, reduced_motion=True))
    indicator = LiveSurfaceIndicatorAdapter(c)
    app.listening_indicator = indicator
    app._schedule_ui = lambda fn, *args: fn(*args)
    c._screens = lambda: [Screen('test', Rect(0, 0, 1600, 1200), True)]
    c.start()
    yield c, indicator, clock
    c.close(); qapp.processEvents(); theme.set_theme(previous)


def question(indicator, owner='q1'):
    indicator.show_feedback('Waiting for confirmation', 'pending', None,
        Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, owner))


@pytest.mark.parametrize('palette', ['dark', 'light'])
@pytest.mark.parametrize('kind', ['success', 'warning', 'error', 'accent', 'pending'])
def test_roles_effective_font_ink_and_accessible_names(surface, qapp, palette, kind):
    c, i, _ = surface; theme.set_theme(palette)
    i.set_session_mode('COMMAND'); i.show_outcome('Synthetic result', kind, 1800); c.drain()
    w = c.widget; w.ensurePolished(); qapp.processEvents()
    assert w._lane.text() == 'Command' and w._state.text() == 'listening'
    assert w._lane.font().pixelSize() == 18 and int(w._lane.font().weight()) == 700
    assert w._state.font().pixelSize() == 16 and int(w._state.font().weight()) == 400
    label = w._band.label
    fill = getattr(theme, FILL_TOKENS[w.view.notice.feedback])
    assert label.font().pixelSize() == 14 and int(label.font().weight()) == 700
    assert label.palette().color(QPalette.ColorRole.WindowText).name() == theme.qcolor(theme.ink_on(fill)).name()
    assert theme.contrast_ratio(theme.ink_on(fill), fill) >= 4.5
    assert theme.contrast_ratio(theme.TEXT_SECONDARY, theme.BG1) >= 4.5
    assert len({x.objectName() for x in [w._lane, w._state, label]}) == 3
    assert all(x.accessibleName() for x in [w._lane, w._state, w._band, label])
    assert w._lane.y() < w._state.y() < w._band.y()


@pytest.mark.parametrize('kind, expected', [('success', NoticeKind.SUCCESS), ('warning', NoticeKind.WARNING),
    ('error', NoticeKind.ERROR), ('accent', NoticeKind.INFORMATION)])
@pytest.mark.parametrize('ttl', [0, 500, 1800, 7000])
def test_producer_severity_and_exact_lifetime(surface, kind, expected, ttl):
    c, i, clock = surface
    i.show_outcome('Synthetic result', kind, ttl); c.drain()
    if ttl:
        assert c.view().notice.kind is expected
        assert c.view().notice.expires_at == ttl/1000
        clock[0] = ttl/1000 - .001; assert c.view().notice.message
    clock[0] = ttl/1000; assert not c.view().notice.message


def test_default_and_explicit_no_timer(surface):
    c, i, clock = surface
    i.show_outcome('Captured', 'success'); c.drain()
    assert c.view().notice.expires_at == DEFAULT_TTL_MS/1000
    i.show_outcome('Persistent result', 'warning', None); c.drain()
    clock[0] = 3600; assert c.view().notice.message == 'Persistent result'


def test_question_owns_band_while_results_drop_and_failures_coexist(surface):
    c, i, clock = surface
    question(i); c.drain()
    i.show_outcome('Captured', 'success', 1800); c.drain()
    assert c.view().notice.owner == 'q1' and c.view().secondary_notice is None
    i.show_outcome('Recording failed', 'error', 3000); c.drain()
    assert c.view().notice.owner == 'q1'
    assert c.view().secondary_notice.feedback is FeedbackKind.ERROR
    assert c.widget.height() > 132
    clock[0] = 90; c.refresh_expired()
    assert c.view().notice.owner == 'q1' and c.view().secondary_notice is None
    assert c.widget.height() == 132
    i.clear_feedback('other'); c.drain(); assert c.view().notice.owner == 'q1'
    i.clear_feedback('q1'); c.drain(); assert c.view().notice.kind is NoticeKind.NONE
    assert 'Captured' not in c.widget.accessibleDescription()


def test_same_owner_can_answer_cancel_or_supersede(surface):
    c, i, _ = surface
    question(i); c.drain()
    i.show_feedback('New question', 'pending', None,
        Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, 'q1')); c.drain()
    assert c.view().notice.message == 'New question'
    i.show_feedback('confirmed', 'success', 1800, Feedback(FeedbackKind.SUCCESS, resolves='q1')); c.drain()
    assert c.view().notice.message == 'confirmed' and c.view().notice.kind is NoticeKind.SUCCESS


def test_running_hold_is_not_question_and_terminal_result_replaces_it(surface):
    c, i, _ = surface
    schedule_feedback(c.app, '...', 'pending', None, source='hold_pending'); c.drain()
    assert c.view().notice.purpose is FeedbackPurpose.RUNNING
    assert c.view().notice.feedback is FeedbackKind.RUNNING
    assert not c.view().can_collapse
    i.show_outcome('typed', 'success', 900); c.drain()
    assert c.view().notice.message == 'typed'
    question(i); c.drain()
    i.clear_feedback('hold'); c.drain(); assert c.view().notice.owner == 'q1'


def test_source_metadata_not_words_decides_question_running_and_reply():
    assert feedback_for('pending', source='command_cancel_window').purpose is FeedbackPurpose.RUNNING
    assert feedback_for('pending', source='command_awaiting_confirmation').purpose is FeedbackPurpose.QUESTION
    assert feedback_for('pending', source='personal_alias_offer').owner == 'alias'
    assert feedback_for('warning', source='command_suggestion_pending').purpose is FeedbackPurpose.QUESTION
    assert feedback_for('accent', source='command_executed', detail={'state': 'queued'}).purpose is FeedbackPurpose.RUNNING
    assert feedback_for('pending', source='ava_dispatched').purpose is FeedbackPurpose.RUNNING
    assert feedback_for('pending', source='draft_parked').purpose is FeedbackPurpose.RESULT
    assert feedback_for('pending', source='edit_proposed').purpose is FeedbackPurpose.QUESTION
    assert feedback_for('success', source='pending_reply', detail={'answer': 'approved'}).resolves == 'command'
    assert feedback_for('pending', source='pending_reply', detail={'answer': 'extended'}).purpose is FeedbackPurpose.QUESTION


def test_producer_operation_identity_and_lifecycle(surface, monkeypatch):
    from samsara import execution_policy as policy
    c, i, _ = surface
    current = [SimpleNamespace(op_id='operation1', approved=None)]
    monkeypatch.setattr(policy, 'pending_operation', lambda: current[0])
    op = current[0]
    data = producer_feedback(c.app, 'pending', 'command_awaiting_confirmation')
    assert data.owner == 'policy:operation1'
    i.show_feedback('Waiting for confirmation', 'pending', None, data); c.drain()
    i.show_outcome('Ordinary result', 'success', 500); c.drain()
    assert c.view().notice.owner == data.owner
    op.approved = True; c.refresh_expired()
    assert c.view().notice.kind is NoticeKind.NONE
    current[0] = SimpleNamespace(op_id='operation2', approved=None)
    data = producer_feedback(c.app, 'pending', 'command_awaiting_confirmation')
    i.show_feedback('Waiting for confirmation', 'pending', None, data); c.drain()
    current[0].approved = True
    i.show_outcome('confirmed', 'success', 1800); c.drain()
    assert c.view().notice.message == 'confirmed'


def test_hold_progress_cannot_take_the_identity_of_a_live_policy_question(surface, monkeypatch):
    from samsara import execution_policy as policy
    c, i, _ = surface
    monkeypatch.setattr(policy, 'pending_operation', lambda: SimpleNamespace(op_id='live', approved=None))
    data = producer_feedback(c.app, 'pending', 'hold_pending')
    assert data.owner == 'hold' and data.purpose is FeedbackPurpose.RUNNING
    question(i); c.drain()
    i.show_feedback('...', 'pending', None, data); c.drain()
    assert c.view().notice.owner == 'q1'
    i.set_thinking(False); c.drain()
    assert c.view().notice.owner == 'q1'


def test_alias_is_cleared_only_when_producer_retires_it(surface):
    c, i, _ = surface
    c.app._personal_alias_offer = {'canonical': 'synthetic'}
    data = producer_feedback(c.app, 'pending', 'personal_alias_offer')
    with alias_handoff(c.app, c.app._personal_alias_offer):
        schedule_feedback(c.app, 'Save this alias?', 'pending', 8000)
    c.drain()
    i.show_outcome('Captured', 'success', 1800); c.drain()
    assert c.view().notice.owner == data.owner
    c.app._personal_alias_offer = None; c.refresh_expired()
    assert c.view().notice.kind is NoticeKind.NONE


def test_question_resolved_before_queued_handoff_does_not_reappear(surface):
    c, i, _ = surface
    i.show_feedback('Waiting for confirmation', 'pending', None,
        Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, 'retired', alive=lambda: False))
    c.drain()
    assert c.view().notice.kind is NoticeKind.NONE


@pytest.mark.parametrize('edge', list(Edge))
def test_ordinary_feedback_keeps_card_clear_words_and_mark_fixed(surface, qapp, edge):
    c, i, clock = surface
    c.app.config['ui']['live_surface']['placement'] = {'preferred_monitor': 'test',
        'monitors': {'test': placement_record(Placement(edge))}}
    c.begin_capture(Lane.HOLD)
    c.post(SurfaceEvent(EventKind.DOCUMENT_UPDATED, text='Synthetic settled draft.', document_id='d', revision=1))
    c.drain(); qapp.processEvents(); w = c.widget
    before = (QRect(w.geometry()), QRect(w._clear.geometry()), QRect(w._transcript.geometry()), w.mapToGlobal(w.mark_rect.topLeft()))
    for kind in ['success', 'warning', 'error', 'accent']:
        i.show_outcome('Synthetic receipt', kind, 1800); c.drain(); qapp.processEvents()
        assert (w.geometry(), w._clear.geometry(), w._transcript.geometry(), w.mapToGlobal(w.mark_rect.topLeft())) == before
        clock[0] += 2; c.refresh_expired(); qapp.processEvents()
        assert (w.geometry(), w._clear.geometry(), w._transcript.geometry(), w.mapToGlobal(w.mark_rect.topLeft())) == before


def test_empty_status_reserves_band_and_long_question_wraps_completely(surface, qapp):
    c, i, _ = surface
    i.set_session_mode('COMMAND'); c.drain(); qapp.processEvents()
    assert (c.widget.width(), c.widget.height()) == (360, 132)
    message = ('This is a synthetic long question with a complete answer instruction. ' * 3) + 'yes or no'
    i.show_feedback(message, 'pending', None, Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, 'long')); c.drain(); qapp.processEvents()
    band = c.widget._band
    assert band.label.text() == message and band.label.wordWrap()
    assert band.label.height() >= band.label.heightForWidth(band.label.width())
    assert c.widget.height() > 132 and band.rect().contains(band.label.geometry())
    assert message in c.widget.accessibleDescription()


def test_device_loss_remains_factual_with_question_draft_and_capture_callbacks(surface):
    c, i, _ = surface
    c.begin_capture(Lane.HOLD); c.drain()
    i.set_device_lost(True); c.drain()
    question(i); c.post(SurfaceEvent(EventKind.DOCUMENT_UPDATED, text='Synthetic draft.', document_id='d', revision=1)); c.drain()
    c.post(SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=CaptureState.TRANSCRIBING)); c.drain()
    assert c.view().capture is CaptureState.UNAVAILABLE
    assert c.widget._state.text() == 'microphone unavailable'
    assert c.view().text == 'Synthetic draft.' and c.view().notice.owner == 'q1'
    i.set_device_lost(False); c.drain()
    assert c.view().capture is CaptureState.TRANSCRIBING


def test_feedback_announced_once_partial_and_expiry_are_silent(surface, monkeypatch):
    c, i, clock = surface; messages = []
    monkeypatch.setattr(QAccessible, 'updateAccessibility', lambda event: messages.append(event.message()))
    question(i); c.drain(); count = len(messages)
    c.widget.refresh(c.view(), animate=False); assert len(messages) == count
    c.post(SurfaceEvent(EventKind.PARTIAL, text='Synthetic partial')); c.drain()
    assert len(messages) == count
    i.show_outcome('Captured', 'success', 1800); c.drain()
    assert len(messages) == count+1 and messages[-1] == 'success: Captured'
    clock[0] = 90; c.refresh_expired(); assert len(messages) == count+1
    assert c.view().notice.owner == 'q1'


def test_monochrome_shapes_and_no_underlines(surface):
    assert len(set(GLYPHS[k] for k in [FeedbackKind.SUCCESS, FeedbackKind.WARNING,
        FeedbackKind.ERROR, FeedbackKind.INFORMATION, FeedbackKind.QUESTION])) == 5
    c, _, _ = surface
    c.post(SurfaceEvent(EventKind.DOCUMENT_UPDATED, text='Synthetic words', document_id='d', revision=1)); c.drain()
    cursor = c.widget._transcript.textCursor(); cursor.setPosition(1)
    assert not cursor.charFormat().fontUnderline()


def test_compact_handoff_is_exactly_the_original_three_arguments():
    class Compact:
        def show_outcome(self, *args): pass
    calls = []; compact = Compact()
    app = SimpleNamespace(listening_indicator=compact, _schedule_ui=lambda fn, *args: calls.append((fn, args)))
    schedule_feedback(app, 'unchanged words', 'pending', None, source='hold_pending')
    assert calls == [(compact.show_outcome, ('unchanged words', 'pending', None))]


def test_exact_dictation_handoff_forwards_source_detail_duration_and_hold_purpose(surface):
    # Compile only exact original method nodes; no application/module bootstrap.
    from samsara import outcome_ring
    source = Path(__file__).resolve().parents[1] / 'dictation.py'
    cls = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    wanted = {'_show_outcome_chip', '_show_dispatch_outcome_chip', '_show_hold_pending_chip', '_resolve_hold_chip'}
    nodes = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    namespace = dict(collections=collections, outcome_ring=outcome_ring, time=time, logger=logging.getLogger('456'))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    c, i, _ = surface
    for name in wanted: setattr(c.app, name, namespace[name].__get__(c.app))
    c.app._show_hold_pending_chip(); c.drain()
    assert c.view().notice.purpose is FeedbackPurpose.RUNNING
    c.app._show_outcome_chip('Synthetic result', 'warning', 7000); c.drain()
    assert c.view().notice.kind is NoticeKind.WARNING and c.view().notice.expires_at == 7
    c.app._show_dispatch_outcome_chip(SimpleNamespace(kind='command_awaiting_confirmation', detail={'phrase': 'synthetic action'})); c.drain()
    assert c.view().notice.purpose is FeedbackPurpose.QUESTION
    c.app._show_outcome_chip('Ordinary result', 'success', 1800); c.drain()
    assert c.view().notice.purpose is FeedbackPurpose.QUESTION
    c.app._show_dispatch_outcome_chip(SimpleNamespace(kind='pending_reply', detail={'answer': 'approved'})); c.drain()
    assert c.view().notice.kind is NoticeKind.SUCCESS
