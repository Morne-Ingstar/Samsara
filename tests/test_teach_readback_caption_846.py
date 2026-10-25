"""846: teaching readback and answer receipts remain visible in both displays."""
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava
from samsara import ava_pending
from samsara.tts.coordinator import AudioCoordinator
from samsara.tts.engine_base import SpeechHandle
from samsara.ui import ava_captions_qt as captions
from tests.test_dictionary_voice_818 import rig


_speak = ava.speak


@pytest.fixture
def caption_flow(rig, qapp, monkeypatch):
    from samsara.listening_display import build_display
    from samsara.ui.listening_indicator import ListeningIndicator

    def build(choice, muted=False, level='full', enabled=True):
        rig.app.config.update({
            'ui': {'listening_display': choice, 'live_surface': {'enabled': True}},
            'accessibility': {'ava_captions': enabled, 'ava_mute_audio': muted},
            'tts': {'feedback_level': level},
        })
        build_display(rig.app, ListeningIndicator)
        engine = Mock()
        engine.get_engine_state.return_value = 'idle'
        engine.speak.return_value = SpeechHandle(utterance_id='test-846')
        rig.app.audio_coordinator = AudioCoordinator(rig.app, engine, config={})
        monkeypatch.setattr(rig.app.audio_coordinator, 'transition_to', Mock())
        monkeypatch.setattr(ava, 'speak', _speak)
        monkeypatch.setattr(captions, '_post', lambda fn: fn())
        captions.reset_for_test()
        return engine

    rig.build = build
    yield rig
    if hasattr(rig.app, 'audio_coordinator'):
        rig.app.audio_coordinator.shutdown()
        controller = getattr(rig.app, 'live_surface', None)
        if controller:
            controller.close()
        else:
            rig.app.listening_indicator.destroy()
    captions.reset_for_test()
    qapp.processEvents()


def display_label(flow, qapp):
    controller = getattr(flow.app, 'live_surface', None)
    if controller:
        controller.drain()
    qapp.processEvents()
    return controller.view().notice.message if controller else flow.app.listening_indicator._chip_label


@pytest.mark.parametrize('choice', ['full', 'compact'])
@pytest.mark.parametrize('answer', ['yes', 'no'])
@pytest.mark.parametrize('seconds', [6, 10])
def test_readback_stays_for_entire_window_then_result(caption_flow, qapp, choice, answer, seconds):
    flow = caption_flow
    flow.app.config['ava'] = {'reply_window_seconds': seconds}
    engine = flow.build(choice)
    flow.app.history.append(('synthetic', 'wrong', False))
    assert ava._check_teaching_intent(flow.app, 'correct that to Atlas spelled A T L A S')
    q = flow.app._ava_question
    expected = q.text + ' Say yes or no'
    panel = captions.active_window()
    assert panel._text.text() == expected
    assert panel._who.text() == 'SAMSARA'
    # 888a: assert the exact configured hold; CI painting can consume a second.
    assert panel._timer.interval() == 60000 and panel._timer.isActive()
    assert not q.opened
    assert display_label(flow, qapp) == expected

    engine.speak.call_args.kwargs['on_done']()
    assert q.opened and q.deadline == 100 + seconds
    flow.now[0] = q.deadline - .01
    ava_pending._tick(flow.app, q)
    assert panel.isVisible() and panel._timer.remainingTime() >= seconds * 1000 - 100
    assert display_label(flow, qapp) == expected
    assert ava_pending.consume(flow.app, answer)
    assert flow.app._ava_question is None
    receipt = engine.speak.call_args.args[0]
    assert receipt != q.text
    assert ('Atlas' in receipt) if answer == 'yes' else ('not saved' in receipt.lower())
    assert panel._text.text() == receipt
    assert display_label(flow, qapp) == receipt
    assert flow.store.apply_corrections('wrong') == ('Atlas' if answer == 'yes' else 'wrong')
    engine.speak.call_args.kwargs['on_done']()
    assert panel._timer.isActive()


@pytest.mark.parametrize('choice', ['full', 'compact'])
def test_silent_readback_is_held_until_cancel(caption_flow, qapp, choice):
    flow = caption_flow
    engine = flow.build(choice, level='silent')
    ava._check_teaching_intent(flow.app, 'spell word A T L A S')
    q = flow.app._ava_question
    assert q.opened
    panel = captions.active_window()
    assert panel._text.text() == q.text + ' Say yes or no'
    assert panel._timer.remainingTime() >= 9900
    assert display_label(flow, qapp).endswith('Say yes or no')
    assert ava_pending.consume(flow.app, 'cancel')
    assert 'not saved' in panel._text.text().lower()
    assert panel._timer.isActive()
    engine.speak.assert_not_called()


def test_disabled_captions_still_show_readback_in_display(caption_flow, qapp):
    flow = caption_flow
    flow.build('compact', enabled=False)
    ava._check_teaching_intent(flow.app, 'spell word A T L A S')
    q = flow.app._ava_question
    assert captions.active_window() is None
    assert display_label(flow, qapp) == q.text + ' Say yes or no'
    ava_pending._expire(flow.app, q)
    assert flow.app._ava_question is None
    assert display_label(flow, qapp) == 'Not saved.'
