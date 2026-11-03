"""1052b: local loops, natural repetition and the decoder-facing retry contract."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from samsara.transcript_gates import (
    LOOP_WARNING, _apply_segment_quality_gates, emit_loop_warning,
    gate_repetition_segments, gate_repetition_text,
)


def segment(text, **changes):
    return NS(**(dict(text=text, compression_ratio=1.4, no_speech_prob=.1,
                      avg_logprob=-.2, temperature=0., start=0., end=1.) | changes))


LEGITIMATE = (
    'No no no, keep the original version.',
    'That is very very useful for this project.',
    'Yes, yes, yes, I agree with the proposal.',
    'I had had enough by the end of the meeting.',
    'The copy that that editor sent was correct.',
    'Please go go ahead with the next step.',
    'We need to talk about this again and again.',
    'To be or not to be is the question.',
    'One by one, one by one, they arrived.',
    'He said no, she said no, and I said no.',
)


@pytest.mark.parametrize('text', LEGITIMATE + ('no no', 'no no no', 'very very', 'yes yes yes'))
def test_real_repeated_words_pass_byte_for_byte(text):
    result = gate_repetition_text(text)
    assert result.text == text
    assert not result.changed
    assert not result.needs_retry
    assert _apply_segment_quality_gates([segment(text)], {}, 2.) == (text, False)


@pytest.mark.parametrize('text', (
    'Yeah yeah yeah yeah yeah', 'the the the the',
    'Beep, BEEP! beep. beep? beep.',
    'go back go back go back', 'turn it off turn it off turn it off',
    'one real word beep beep beep beep',
))
def test_unsalvageable_loop_delivers_nothing_and_requests_one_retry(text):
    result = gate_repetition_segments([segment(text)])
    assert result.text == ''
    assert result.needs_retry is True
    assert result.dropped == 1
    assert result.segments == ()
    quality = _apply_segment_quality_gates([segment(text)], {}, 2.)
    assert quality == ('', False)
    assert quality.needs_retry is True


@pytest.mark.parametrize('text,expected', (
    ('beep beep beep beep Keep these four useful words.', 'Keep these four useful words.'),
    ('Keep these beep beep beep beep four useful words.', 'Keep these four useful words.'),
    ('Keep these four useful words. beep beep beep beep', 'Keep these four useful words.'),
    ('Keep these four useful words. go back go back go back', 'Keep these four useful words.'),
    ('turn it off turn it off turn it off Keep these four useful words.', 'Keep these four useful words.'),
    ('beep beep beep beep Keep these four useful words. tick tick tick tick', 'Keep these four useful words.'),
    # Synthetic structural analogue of the supplied 00:41 incident; no owner dictation.
    ('Beep beep beep beep beep Keep the complete project notes for tomorrow morning.',
     'Keep the complete project notes for tomorrow morning.'),
))
def test_salvage_prefix_middle_suffix_and_multiple_runs(text, expected):
    result = gate_repetition_text(text)
    assert result.text == expected
    assert result.stripped >= 1
    assert not result.needs_retry


def test_cleaned_segment_keeps_timing_and_does_not_mutate_native_result():
    original = segment('beep beep beep beep Keep these four useful words.', compression_ratio=29.3)
    result = gate_repetition_segments([original])
    assert result.segments[0].start == original.start
    assert result.segments[0].avg_logprob == original.avg_logprob
    assert original.compression_ratio == 29.3
    assert original.text.startswith('beep')
    quality = _apply_segment_quality_gates([original], {}, 2.)
    assert quality == ('Keep these four useful words.', False)
    assert quality.repetition.stripped == 1


def test_bad_middle_segment_cannot_erase_good_neighbours():
    segments = [segment('Keep the first complete sentence.'),
                segment(' beep beep beep beep', compression_ratio=29.3),
                segment(' Keep the last complete sentence.')]
    result = _apply_segment_quality_gates(segments, {}, 4.)
    assert result == ('Keep the first complete sentence. Keep the last complete sentence.', False)
    assert result.needs_retry


def test_natural_repeats_cannot_fund_the_four_ordinary_word_floor():
    result = gate_repetition_text('no no very very beep beep beep beep')
    assert result.text == ''
    assert result.needs_retry


def test_loop_logs_counts_without_transcript(caplog):
    with caplog.at_level('INFO', logger='Samsara'):
        gate_repetition_segments([segment('beep beep beep beep Keep these four useful words.')])
    assert '[LOOP_GATE] runs_stripped=1 segments_dropped=0 needs_retry=False' in caplog.text
    assert 'useful' not in caplog.text
    assert 'beep' not in caplog.text


def test_warning_emits_existing_kind_without_implementing_lifetime():
    app = NS(_show_outcome_chip=Mock())
    emit_loop_warning(app, gate_repetition_segments([segment('A clean ordinary sentence.')]))
    app._show_outcome_chip.assert_not_called()
    emit_loop_warning(app, gate_repetition_segments([segment('beep beep beep beep')]))
    app._show_outcome_chip.assert_called_once_with(LOOP_WARNING, 'warning')
