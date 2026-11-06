"""Real question lifecycle and visible Dismiss controls; synthetic effects only."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QPushButton

from samsara import ava_pending, button_voice, execution_policy as policy, message_lifetime
from samsara.ui.button_voice_qt import VisibleActions
from tests.test_ava_plan_and_ask_624 import rig, question
from tests.test_message_durations_475 import display
from tests.test_button_voice_parity_1150 import GOOD, NOISE


@pytest.fixture
def shown_question(rig, display, qapp, monkeypatch):
    d = display
    rig.app.listening_indicator = d.i
    d.clock[0] = rig.now[0]
    if d.c:
        d.c.app = rig.app
        d.c.start()
        d.c.widget._apply_motion_preference(True)
    monkeypatch.setattr(message_lifetime, 'consume_acknowledgement', lambda *a, **k: False)
    assert question(rig).state == 'queued'
    d.flush()
    qapp.processEvents()
    qapp.processEvents()
    bridge = VisibleActions(qapp, lambda fn: fn())
    monkeypatch.setattr(button_voice, '_bridge', bridge)
    matches = [a.target() for a in bridge.snapshot if a.label == 'Dismiss']
    assert len(matches) == 1, (d.text(), [(a.label, a.identity) for a in bridge.snapshot],
        [(b.text(), b.isVisible(), b.geometry().getRect()) for b in d.c.widget.findChildren(QPushButton)] if d.c else '')
    button = matches[0]
    assert button.isVisible() and button.isEnabled()
    assert button.width() <= 76 and button.height() == 28
    try:
        yield rig, d, bridge, button
    finally:
        bridge.timer.stop()
        bridge.deleteLater()


@pytest.mark.parametrize('action', ['mouse', 'dismiss', 'click dismiss'])
def test_visible_dismiss_closes_without_effect_and_next_yes_works(shown_question, action, qapp):
    r, d, bridge, button = shown_question
    old = r.app._ava_question
    if action == 'mouse':
        button.click()
    else:
        assert button_voice.consume_before_pending(r.app, action, GOOD,
            self_hear=False, capture_path='toggle_command')
    d.flush()
    qapp.processEvents()
    assert r.app._ava_question is None
    assert old.timer.cancelled and r.effects == []
    assert r.app.app_state == 'asleep' and not r.app.wake_word_triggered
    r.app._release_wake_consumer.assert_called_once_with('ava_question')
    assert 'Run those steps?' not in d.text()
    assert question(r).state == 'queued'
    assert ava_pending.consume(r.app, 'yes')
    assert [effect[0] for effect in r.effects] == ['open chrome', 'scroll down']
    assert r.app._ava_question is None


def test_queued_click_cannot_dismiss_replacement_question(shown_question, qapp):
    r, d, bridge, button = shown_question
    pending = []
    bridge.post = pending.append
    assert bridge.dispatch('click dismiss')
    old = r.app._ava_question
    assert ava_pending.ask(r.app, old.runner, 'yes_no', 'Another synthetic question?', ()).state == 'queued'
    newer = r.app._ava_question
    d.flush()
    qapp.processEvents()
    pending.pop()()
    assert newer is not old and r.app._ava_question is newer
    assert not ava_pending.dismiss_question(r.app, old)
    assert r.effects == []


@pytest.mark.parametrize('phrase', ['dismiss', 'click dismiss'])
def test_noise_and_self_hearing_do_not_dismiss(shown_question, phrase):
    r, d, bridge, button = shown_question
    old = r.app._ava_question
    for signals, self_hear in ((NOISE, False), (GOOD, True)):
        assert button_voice.consume_before_pending(r.app, phrase, signals,
            self_hear=self_hear, capture_path='toggle_command')
        assert r.app._ava_question is old and r.effects == []


@pytest.mark.parametrize('phrase', ['dismiss', 'click dismiss'])
def test_answer_lane_dismiss_and_successor(rig, phrase):
    assert question(rig).state == 'queued'
    assert ava_pending.consume(rig.app, phrase, capture_path='wake_command')
    assert rig.app._ava_question is None and rig.effects == []
    assert question(rig).state == 'queued'
    assert ava_pending.consume(rig.app, 'yes')
    assert len(rig.effects) == 2


@pytest.mark.parametrize('phrase', ['literal dismiss', 'we should click dismiss later', 'dismiss and send'])
def test_only_whole_dismiss_utterance_is_accepted(rig, phrase):
    question(rig)
    old = rig.app._ava_question
    assert ava_pending.consume(rig.app, phrase)
    assert rig.app._ava_question is old and rig.effects == []


@pytest.mark.parametrize('path', ['model', 'echo'])
def test_untrusted_answer_source_cannot_dismiss(rig, path):
    question(rig)
    old = rig.app._ava_question
    assert ava_pending.consume(rig.app, 'dismiss', capture_path=path)
    assert rig.app._ava_question is old and rig.effects == []


@pytest.mark.parametrize('effect', ['send', 'delete'])
@pytest.mark.parametrize('phrase', ['dismiss', 'click dismiss'])
def test_dismiss_never_approves_pending_send_or_delete(rig, effect, phrase):
    approved = Mock()
    op = policy.PendingOperation(policy.Invocation(effect, generation=0),
        'Confirm synthetic action?', app=rig.app, clock=lambda: rig.now[0], on_approve=approved)
    policy._install_record(rig.app, {'type': 'action', 'op': op})
    runner = SimpleNamespace(eligible=lambda: True, expired=Mock(), run=approved)
    assert ava_pending.ask(rig.app, runner, 'policy', op.prompt, (), operation=op).state == 'queued'
    assert ava_pending.consume(rig.app, phrase)
    assert rig.app._ava_question is None and policy.pending_operation() is None
    assert op.approved is False
    approved.assert_not_called()
    assert rig.effects == []


def test_expired_button_retires_without_running(shown_question):
    r, d, bridge, button = shown_question
    old = r.app._ava_question
    r.now[0] = old.deadline
    button.click()
    assert r.app._ava_question is None and r.effects == []
