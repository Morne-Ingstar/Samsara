"""830: a state-owned readback expects a short word, not whole-buffer energy."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from plugins.commands import ask_ollama as ava
from samsara import ava_pending, speech_presence
from samsara.audio_engine import wake_consumer
from tests.test_dictionary_voice_818 import rig
from tests.test_wake_gate_in_session import _Harness


def readback(rig):
    ava._check_teaching_intent(rig.app, 'spell word S I O B H A N')
    return rig.app._ava_question


def capture(peak=.006):
    audio = np.zeros(40000, np.float32)
    audio[24000:32000] = peak * np.sin(2 * np.pi * 200 * np.arange(8000) / 16000)
    return audio


def scores(monkeypatch, probability=.4, frames=16, known=True):
    probabilities = (0.0,) * 47 + (probability,) * frames + (0.0,) * (79 - 47 - frames)
    result = speech_presence.Presence(known, max_probability=max(probabilities),
                                     probabilities=probabilities, samples=40000)
    measure = Mock(return_value=result)
    monkeypatch.setattr(speech_presence._presence, 'measure', measure)
    return measure


def test_incident_repro_old_gate_skips_quiet_short_word_and_new_gate_decodes(rig, monkeypatch):
    audio = capture()
    rms = float(np.sqrt(np.mean(audio ** 2)))
    assert rms == pytest.approx(.006 / np.sqrt(10))
    assert wake_consumer.in_session_rms_gate(rms)[0]
    # The brief's literal peak=.03 sine is already admitted by the old gate.
    assert not wake_consumer.in_session_rms_gate(float(np.sqrt(np.mean(capture(.03) ** 2))))[0]
    readback(rig)
    app = _Harness(app_state='command_window', transcript='yes')
    app.__dict__.update(rig.app.__dict__)
    measure = scores(monkeypatch)
    app._decode_wake_word_buffer([audio], 16000)
    app.model.transcribe.assert_called_once()
    assert app.model.transcribe.call_args.kwargs['vad_filter'] is False
    measure.assert_called_once()  # No second generic gate after short-answer admission.
    assert rig.store.custom_vocab == ['Siobhan']


@pytest.mark.parametrize('probability,frames,known,skip', [
    (.4, 10, True, False), (.51, 1, True, False),
    (.4, 9, True, True), (.0, 16, True, True), (.0, 16, False, False),
])
def test_short_answer_presence_run_peak_and_unknown(rig, monkeypatch, probability, frames, known, skip):
    readback(rig)
    scores(monkeypatch, probability, frames, known)
    assert wake_consumer.pending_short_answer_gate(rig.app, capture(), 16000) is skip


@pytest.mark.parametrize('ownership', ['ordinary_session', 'not_state_owned', 'retired', 'not_open', 'spelling'])
def test_ordinary_handsfree_gate_and_other_owners_unchanged(rig, monkeypatch, ownership):
    if ownership != 'ordinary_session':
        q = readback(rig)
        if ownership == 'not_state_owned': q.state_owned = False
        if ownership == 'retired': rig.app._ava_cmd_generation += 1
        if ownership == 'not_open': q.opened = False
        if ownership == 'spelling': q.runner.pending['type'] = 'vocab_spelling_wait'
    measure = scores(monkeypatch)
    assert wake_consumer.pending_short_answer_gate(rig.app, capture(), 16000) is None
    measure.assert_not_called()
    assert wake_consumer.in_session_rms_gate(.0019) == (True, .002, 'in_session_near_silence')


@pytest.mark.parametrize('decoded', [False, True])
def test_real_decode_skip_or_empty_reprompts_once_and_records_question(rig, monkeypatch, decoded):
    q = readback(rig)
    app = _Harness(app_state='command_window', transcript='')
    app.__dict__.update(rig.app.__dict__)
    scores(monkeypatch, .4 if decoded else 0)
    app._decode_wake_word_buffer([capture()], 16000)
    assert app._ava_question is q and q.reprompted
    assert rig.spoken[-1] == 'Say yes or no'
    assert q.deadline == 110
    row = rig.db.recent()[0]
    assert row['raw_text'] == q.text
    assert ('empty' if decoded else 'skipped') in row['display_text']
    app._decode_wake_word_buffer([capture()], 16000)
    assert rig.spoken.count('Say yes or no') == 1
    assert app._ava_question is q
    rig.now[0] = 111
    ava_pending._tick(app, q)
    assert app._ava_question is None
    assert 'timeout' in rig.db.recent()[0]['display_text']
    assert rig.db.recent()[0]['raw_text'] == q.text


def test_initial_and_retry_deadlines_start_at_speech_end_and_silence_has_one_retry(rig):
    rig.app.defer_speech = True
    q = readback(rig)
    assert not q.opened
    rig.now[0] = 120
    rig.callbacks[-1]()
    assert q.deadline == 130
    rig.now[0] = 131
    ava_pending._tick(rig.app, q)
    assert rig.app._ava_question is q and not q.opened
    assert rig.spoken[-1] == 'Say yes or no'
    rig.now[0] = 135
    rig.callbacks[-1]()
    assert q.opened and q.deadline == 145
    rig.now[0] = 146
    ava_pending._tick(rig.app, q)
    assert rig.app._ava_question is None and ava._pending_action is None
    assert 'timeout' in rig.db.recent()[0]['display_text']
    assert rig.db.recent()[0]['raw_text'] == q.text
    assert rig.spoken[-1] == 'Not saved.'
    rig.callbacks[-1]()
    assert rig.app._ava_question is None
    rig.app._release_wake_consumer.assert_called_once()


@pytest.mark.parametrize('answer,saved', [('yes', True), ('yeah', True), ('yep', True),
    ('no', False), ('nope', False), ('cancel', False), ('YES.', True),
    ('open chrome', False), ('yes and save another word', False)])
def test_accepted_answers_and_nonanswers_are_consumed_once(rig, answer, saved):
    readback(rig)
    assert ava_pending.consume(rig.app, answer)
    assert rig.store.custom_vocab == (['Siobhan'] if saved else [])
    assert rig.app._ava_question is None and ava._pending_action is None
    if answer in {'open chrome', 'yes and save another word'}:
        assert rig.spoken[-1] == "I'll take that as no. Not saved."


@pytest.mark.parametrize('value,expected', [(15, 15), (1, 2), (60, 30),
    ('bad', 10), (float('nan'), 10), (float('inf'), 10)])
def test_existing_key_controls_readback_window(rig, value, expected):
    rig.app.config['ava'] = {'reply_window_seconds': value}
    assert readback(rig).deadline == 100 + expected


def test_retry_speech_cannot_resurrect_replaced_question(rig):
    q = readback(rig)
    rig.app.defer_speech = True
    assert ava_pending.missed_capture(rig.app, 'skipped')
    retry_done = rig.callbacks[-1]
    ava._check_teaching_intent(rig.app, 'spell word A T L A S')
    successor = rig.app._ava_question
    assert successor is not q
    retry_done()
    assert rig.app._ava_question is successor and not successor.opened


def test_readback_key_is_config_only():
    from samsara.config_schema import SETTINGS_SCHEMA
    row = SETTINGS_SCHEMA['ava.reply_window_seconds']
    assert row['default'] == 10
    assert row['settings_ui'] is False


def test_detector_failure_keeps_decoding_and_disabled_gate_is_respected(rig, monkeypatch):
    readback(rig)
    measure = scores(monkeypatch)
    measure.side_effect = RuntimeError('detector unavailable')
    assert wake_consumer.pending_short_answer_gate(rig.app, capture(), 16000) is False
    rig.app.config['dictation'] = {'speech_presence_gate': False}
    measure.reset_mock()
    assert wake_consumer.pending_short_answer_gate(rig.app, capture(), 16000) is False
    measure.assert_not_called()


def test_teaching_wall_clock_expiry_refreshes_at_both_tts_completions(rig, monkeypatch):
    wall = [1000.0]
    monkeypatch.setattr(ava, 'time', SimpleNamespace(time=lambda: wall[0]))
    rig.app.defer_speech = True
    q = readback(rig)
    wall[0], rig.now[0] = 1040, 140
    rig.callbacks[-1]()
    assert q.runner.pending['expires'] == 1050
    assert q.runner.pending['speak_until'] == 0
    assert ava_pending.missed_capture(rig.app, 'empty decode')
    wall[0], rig.now[0] = 1045, 145
    rig.callbacks[-1]()
    assert q.deadline == 155 and q.runner.pending['expires'] == 1055
    rig.app._ensure_wake_consumer.assert_called_once()


def test_normal_handsfree_decode_still_uses_rms_and_generic_speech_gate(monkeypatch):
    app = _Harness(app_state='quick_dictation')
    measure = scores(monkeypatch, 0)
    app._decode_wake_word_buffer([capture()], 16000)
    app.model.transcribe.assert_not_called()
    measure.assert_not_called()
    app._decode_wake_word_buffer([capture(.03)], 16000)
    app.model.transcribe.assert_not_called()
    measure.assert_called_once()


@pytest.mark.parametrize('probability', [0, .9])
def test_replaced_question_during_scoring_cannot_retry_or_decode_for_successor(rig, monkeypatch, probability):
    old = readback(rig)
    measure = scores(monkeypatch, probability)
    result = measure.return_value

    def replaced(*_args):
        ava._check_teaching_intent(rig.app, 'spell word A T L A S')
        return result

    measure.side_effect = replaced
    assert wake_consumer.pending_short_answer_gate(rig.app, capture(), 16000) is True
    successor = rig.app._ava_question
    assert successor is not old and not successor.reprompted
    assert 'Say yes or no' not in rig.spoken
    assert not ava_pending.missed_capture(rig.app, 'skipped', question=old)


def test_question_cannot_apply_short_gate_after_capture_state_taken_over(rig, monkeypatch):
    readback(rig)
    rig.app.app_state = 'quick_dictation'
    measure = scores(monkeypatch)
    assert wake_consumer.pending_short_answer_gate(rig.app, capture(), 16000) is None
    measure.assert_not_called()
