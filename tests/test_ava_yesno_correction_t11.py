"""Real Ava question/plan/policy, synthetic speech and side effects only."""
import json
from types import SimpleNamespace
import pytest
from tests.test_ava_plan_and_ask_624 import rig, question
from plugins.commands import ask_ollama as ava
from samsara import ava_pending, execution_policy as policy

GUIDANCE = 'Say yes or no. To change the request, say cancel and ask again.'


def test_correction_gets_feedback_without_replacing_or_approving_plan(rig):
    question(rig)
    q = rig.app._ava_question
    before = list(rig.spoken)
    deadline = q.deadline
    assert ava_pending.consume(rig.app, 'actually open Notepad instead')
    assert rig.spoken == before + [GUIDANCE]
    assert rig.app.listening_indicator.show_outcome.call_args.args[0] == GUIDANCE
    assert rig.app._ava_question is q and q.deadline == deadline
    assert rig.effects == [] and policy.pending_operation() is None
    # The correction has not silently replaced the original requested plan.
    assert ava_pending.consume(rig.app, 'yes')
    assert [effect[0] for effect in rig.effects] == ['open chrome', 'scroll down']
    assert rig.app._ava_question is None


def test_repeated_corrections_do_not_loop_speech_or_extend_deadline(rig):
    question(rig)
    q = rig.app._ava_question
    deadline = q.deadline
    for text in ('actually Notepad instead', 'I meant Notepad', 'yes and delete everything'):
        assert ava_pending.consume(rig.app, text)
        assert rig.app._ava_question is q and q.deadline == deadline
        assert rig.effects == [] and policy.pending_operation() is None
    assert rig.spoken.count(GUIDANCE) == 1
    assert ava_pending.consume(rig.app, 'cancel')
    assert rig.app._ava_question is None and rig.effects == []
    # Cancel retires generation 0; the shared question() helper hardcodes 0.
    assert policy.current_generation(rig.app) == 1
    assert question(rig).state == 'stale'
    assert rig.app._ava_question is None
    result = ava.handle_response(rig.app, json.dumps({
        'type': 'ask', 'kind': 'yes_no', 'question': 'Run those steps?',
        'plan': ['open chrome', 'scroll down'],
    }), original_text='open chrome and scroll down',
        generation=policy.capture_generation(rig.app))
    assert result.state == 'queued'
    assert rig.app._ava_question is not q
    assert ava_pending.consume(rig.app, 'actually another request')
    assert rig.spoken.count(GUIDANCE) == 2  # A new question may explain again.


@pytest.mark.parametrize('speech', ['silent', 'unavailable'])
def test_guidance_without_speech_preserves_visible_feedback_and_deadline(rig, speech):
    question(rig)
    q = rig.app._ava_question
    deadline, timer = q.deadline, q.timer
    before = list(rig.spoken)
    attempts = []
    if speech == 'silent':
        def silent(text, **kwargs):
            attempts.append((text, kwargs))
            return SimpleNamespace(utterance_id='noop-silent')
        rig.app.audio_coordinator.speak = silent
    else:
        rig.app.audio_coordinator = None
    assert ava_pending.consume(rig.app, 'actually another request')
    assert rig.app.listening_indicator.show_outcome.call_args.args[0] == GUIDANCE
    assert rig.app._ava_question is q and q.deadline == deadline and q.timer is timer
    assert rig.spoken == before and rig.effects == []
    assert policy.pending_operation() is None
    if speech == 'silent':
        assert len(attempts) == 1 and attempts[0][0] == GUIDANCE
        assert 'on_done' not in attempts[0][1]
    # The failed/suppressed feedback does not reopen or extend answer capture.
    rig.now[0] = deadline
    assert ava_pending.consume(rig.app, 'yes')
    assert rig.app._ava_question is None and rig.effects == []


def test_empty_answer_does_not_spend_guidance_and_no_cancels_after_correction(rig):
    question(rig)
    q = rig.app._ava_question
    before = list(rig.spoken)
    assert ava_pending.consume(rig.app, '   ')
    assert rig.spoken == before and rig.app._ava_question is q
    assert ava_pending.consume(rig.app, 'actually another request')
    assert rig.spoken == before + [GUIDANCE]
    assert ava_pending.consume(rig.app, 'no')
    assert rig.app._ava_question is None
    assert rig.effects == [] and policy.pending_operation() is None
