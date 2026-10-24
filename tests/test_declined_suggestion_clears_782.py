"""782: pending questions follow their operation on Full and Compact."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from plugins.commands import ask_ollama
from samsara import execution_policy as policy
from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.feedback import producer_feedback
from samsara.live_surface.model import LiveSurfaceModel, NoticeKind
from samsara.session_feedback import outcome_chip
from samsara.session_modes import (CommandDispatchResult, CommandSuggestionCandidate,
                                  SessionMode, SessionModeManager, UtteranceSignals)
from samsara.ui.listening_indicator import ListeningIndicator

GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))
QUESTION = 'Did you mean open notepad? Say yes or no'


@pytest.mark.parametrize('source', ['command_awaiting_confirmation', 'command_suggestion_pending'])
@pytest.mark.parametrize('approved', [True, False])
def test_operation_without_liveness_method_preserves_approval_lifecycle(monkeypatch, source, approved):
    op = SimpleNamespace(op_id='782-legacy-operation', approved=None)
    current = [op]
    monkeypatch.setattr(policy, 'pending_operation', lambda: current[0])
    app = SimpleNamespace(_ava_cmd_generation=1)
    feedback = producer_feedback(app, 'pending', source)
    assert feedback.alive()
    op.approved = approved
    assert not feedback.alive()
    op.approved = None
    current[0] = SimpleNamespace(op_id='replacement', approved=None)
    assert not feedback.alive()


@pytest.fixture
def session(monkeypatch, qapp):
    ask_ollama.clear_pending_action()
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    clock = [100.0]
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}},
                          _ava_cmd_generation=1, _show_outcome_chip=Mock())
    dispatcher = Mock(return_value=CommandDispatchResult(False))
    manager = SessionModeManager(
        abort_phrases=['cancel', 'abort'],
        foreground_exe_resolver=lambda: 'fixture.exe', foreground_hwnd_resolver=lambda: 1,
        inject_fn=Mock(),
        remove_chars_fn=Mock(), command_dispatch_fn=dispatcher, agent_dispatch_fn=Mock(),
        command_suggestion_fn=lambda _: CommandSuggestionCandidate('open notepad'),
        pending_reply_fn=lambda text: policy.answer_pending(app, text), clock=lambda: clock[0])
    app._session_mode_manager = manager
    controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: clock[0]))
    full = LiveSurfaceIndicatorAdapter(controller)
    compact = ListeningIndicator()
    compact.set_idle_animation(False)
    compact.set_session_mode('COMMAND', None)
    full.set_session_mode('COMMAND')
    controller.drain()
    state = SimpleNamespace(app=app, manager=manager, dispatcher=dispatcher, clock=clock,
                            controller=controller, full=full, compact=compact, executed=Mock())
    yield state
    compact.destroy()
    controller.close()
    ask_ollama.clear_pending_action()
    qapp.processEvents()


def offer(s, *, local=False):
    if local:
        outcome = s.manager.dispatch_command_utterance('synthetic unmatched command')
        assert outcome.kind == 'command_suggestion_pending'
    op = None
    if not local or getattr(s, 'bind_local_policy', False):
        inv = policy.Invocation('782 synthetic', {}, policy.Route.EXACT,
                                policy.current_generation(s.app))
        op = policy.stage_pending(s.app, inv, QUESTION, on_approve=s.executed,
                                  targets={}, target_probe=lambda: {},
                                  refusal_guard=getattr(s, 'refusal_guard', None))
    feedback = producer_feedback(s.app, 'warning', 'command_suggestion_pending')
    for indicator in (s.full, s.compact):
        indicator.show_feedback(QUESTION, 'warning', 8000, feedback)
    s.controller.drain()
    assert s.controller.view().notice.message == QUESTION
    assert s.compact._question_alive()
    return op, feedback


def reconcile(s):
    s.controller.drain()
    s.controller.refresh_expired()
    s.compact._clear_outcome()


def assert_gone(s):
    reconcile(s)
    assert s.controller.view().notice.purpose.value != 'question'
    assert not s.compact._question_alive()
    assert s.compact._chip_label != QUESTION


@pytest.mark.parametrize('reply,expected', [('no', 'rejected'), ('yes', 'approved')])
@pytest.mark.parametrize('local', [False, True])
def test_answer_retires_question_and_reports_result(session, reply, expected, local):
    s = session
    s.bind_local_policy = True
    offer(s, local=local)
    outcome = s.manager.dispatch_command_utterance(reply)
    assert outcome.kind == 'pending_reply'
    assert outcome.detail['answer'] == expected
    label, kind = outcome_chip(outcome.kind, outcome.detail)
    feedback = producer_feedback(s.app, kind, outcome.kind, outcome.detail)
    for display in (s.full, s.compact):
        display.show_feedback(label, kind, 1800, feedback)
    assert_gone(s)
    assert s.manager._pending_command_suggestion is None
    assert s.executed.call_count == (1 if reply == 'yes' else 0)
    if reply == 'no':
        assert s.controller.view().notice.message.lower() == 'cancelled'
        s.clock[0] += 9
        s.compact._chip_deadline = 0
        reconcile(s)
        assert s.controller.view().notice.kind is NoticeKind.NONE
        assert s.compact._chip_label is None


@pytest.mark.parametrize('local', [False, True])
@pytest.mark.parametrize('ending', ['expired', 'superseded', 'stop_all', 'mode', 'exit'])
def test_lifecycle_retires_without_another_utterance(session, local, ending):
    s = session
    s.bind_local_policy = ending in ('superseded',)
    op, feedback = offer(s, local=local)
    if ending == 'expired':
        if op:
            op.deadline = 0
        s.clock[0] += 9
    elif ending == 'superseded':
        policy.stage_pending(s.app, policy.Invocation('782 replacement', {}, policy.Route.EXACT, 1),
                             'Replacement question', targets={}, target_probe=lambda: {})
    elif ending == 'stop_all':
        # This invokes the isolated policy function, never a running app/process.
        policy.stop_all(s.app, chip=False)
    elif ending == 'mode':
        s.manager.mode = SessionMode.DICTATE
    else:
        s.manager.reset()
    assert not feedback.alive()
    assert_gone(s)
    if op is not None and ending in ('mode', 'exit'):
        assert policy.answer_pending(s.app, 'yes') != 'approved'
    assert s.executed.call_count == 0


def test_new_command_replaces_local_suggestion(session):
    s = session
    offer(s, local=True)
    s.dispatcher.return_value = CommandDispatchResult(True, 'open calendar')
    outcome = s.manager.dispatch_command_utterance('open calendar')
    label, kind = outcome_chip(outcome.kind, outcome.detail)
    for display in (s.full, s.compact):
        display.show_feedback(label, kind, 1800, producer_feedback(s.app, kind, outcome.kind, outcome.detail))
    assert_gone(s)
    assert s.dispatcher.call_args.args == ('open calendar',)
    assert 'open calendar' in s.controller.view().notice.message


@pytest.mark.parametrize('local', [False, True])
def test_dismiss_control_cancels_operation_and_shows_receipt(session, qapp, local):
    s = session
    op, _ = offer(s, local=local)
    s.controller.start()
    qapp.processEvents()
    widget = s.controller.widget
    band = widget._chip or widget._band
    assert band.dismiss.isVisible()
    assert band.dismiss.fontMetrics().horizontalAdvance(band.dismiss.text()) <= band.dismiss.width()-4
    assert band.label.geometry().right() < band.dismiss.geometry().left()
    QTest.mouseClick(band.dismiss, Qt.MouseButton.LeftButton)
    assert_gone(s)
    assert s.controller.view().notice.message == 'Cancelled'
    assert s.manager._pending_command_suggestion is None
    assert not op or op.approved is False
    assert s.executed.call_count == 0


def test_wait_uses_extended_policy_deadline(session):
    s = session
    s.bind_local_policy = True
    op, feedback = offer(s, local=True)
    assert s.manager.dispatch_command_utterance('wait').detail['answer'] == 'extended'
    s.clock[0] += 9  # local offer's old deadline no longer owns the operation
    assert feedback.alive()
    assert s.manager._pending_command_suggestion is not None
    assert s.manager.dispatch_command_utterance('wait').detail['answer'] == 'refused:wait already used'
    assert feedback.alive()
    assert s.manager._pending_command_suggestion is not None
    op.deadline = 0
    assert_gone(s)


def test_stop_invalidates_legacy_yes_before_the_next_display_poll(session):
    s = session
    offer(s, local=True)
    policy.stop_all(s.app, chip=False)
    s.dispatcher.return_value = CommandDispatchResult(True, 'yes')
    outcome = s.manager.dispatch_command_utterance('yes')
    assert outcome.kind == 'command_executed'
    assert s.dispatcher.call_args.args == ('yes',)
    assert s.manager._pending_command_suggestion is None
    assert_gone(s)


def test_new_command_invalidates_policy_suggestion_context(session):
    s = session
    serial = [0]
    s.refusal_guard = lambda: 'new command' if serial[0] else None
    offer(s)
    def dispatch(text):
        serial[0] += 1
        return CommandDispatchResult(True, text)
    s.dispatcher.side_effect = dispatch
    outcome = s.manager.dispatch_command_utterance('open calendar')
    label, kind = outcome_chip(outcome.kind, outcome.detail)
    for display in (s.full, s.compact):
        display.show_feedback(label, kind, 1800, producer_feedback(s.app, kind, outcome.kind, outcome.detail))
    assert_gone(s)
    assert 'open calendar' in s.controller.view().notice.message
    assert policy.answer_pending(s.app, 'yes') != 'approved'
    assert s.executed.call_count == 0


def test_delayed_offer_cannot_restore_a_resolved_question(session):
    s = session
    _, feedback = offer(s)
    policy.answer_pending(s.app, 'no')
    assert_gone(s)
    for display in (s.full, s.compact):
        display.show_feedback(QUESTION, 'warning', 8000, feedback)
    late_feedback = producer_feedback(s.app, 'warning', 'command_suggestion_pending')
    assert not late_feedback.alive()
    for display in (s.full, s.compact):
        display.show_feedback(QUESTION, 'warning', 8000, late_feedback)
    assert_gone(s)


def test_wait_keeps_the_suggestion_dismiss_control(session, qapp):
    s = session
    op, _ = offer(s)
    outcome = s.manager.dispatch_command_utterance('wait')
    label, kind = outcome_chip(outcome.kind, outcome.detail)
    for display in (s.full, s.compact):
        display.show_feedback(label, kind, None, producer_feedback(s.app, kind, outcome.kind, outcome.detail))
    s.controller.drain()
    s.controller.start()
    qapp.processEvents()
    band = s.controller.widget._chip or s.controller.widget._band
    assert band.dismiss.isVisible()
    QTest.mouseClick(band.dismiss, Qt.MouseButton.LeftButton)
    assert_gone(s)
    assert op.approved is False
    assert s.executed.call_count == 0
