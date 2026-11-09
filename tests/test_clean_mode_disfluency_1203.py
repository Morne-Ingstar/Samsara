"""Post-decode cleanup regressions; no application bootstrap or live files."""
import pytest

from samsara.cleanup import clean_text


def test_reported_comma_wrapped_sentence():
    text = "Okay, uh, look at, just, even just like the last two dictations I did."
    assert clean_text(text, mode='clean') == (
        "Okay, look at, just, even just like the last two dictations I did."
    )


@pytest.mark.parametrize('filler', ['uh', 'um', 'er', 'erm', 'ah', 'uhm', 'hmm'])
@pytest.mark.parametrize('template,expected', [
    ('Okay, {}, continue.', 'Okay, continue.'),
    ('{}, continue.', 'Continue.'),
    ('First. {}, continue.', 'First. Continue.'),
    ('Please {} continue.', 'Please continue.'),
    ('Continue, {}.', 'Continue.'),
])
def test_standalone_fillers_and_leftover_punctuation(filler, template, expected):
    assert clean_text(template.format(filler.upper()), mode='clean') == expected


@pytest.mark.parametrize('text', ['Ah', 'ah.', 'AH!', 'Hmm', 'hmm?', 'HMM...'])
def test_deliberate_whole_utterance_reply(text):
    expected = text[0].upper() + text[1:]
    if expected[-1] not in '.!?':
        expected += '.'
    assert clean_text(text, mode='clean') == expected


@pytest.mark.parametrize('text', [
    'An umbrella ahead of the terminal.',
    'Human humming and thermal warmth.',
    'Actually, so, well, like, basically, you know, I mean, continue.',
])
def test_real_words_and_discourse_survive(text):
    assert clean_text(text, mode='clean') == text


def test_adjacent_fillers_do_not_leave_comma_runs():
    assert clean_text('Okay, uh, erm, hmm, continue.') == 'Okay, continue.'


@pytest.mark.parametrize('text', [
    'okay, UH, erm, ah, uhm, hmm, continue', 'Ah!', 'hmm?',
])
def test_verbatim_stays_exact(text):
    assert clean_text(text, mode='verbatim') == text


def test_unrecognized_mode_retains_legacy_cleanup():
    assert clean_text('erm ah uhm hmm continue', mode='legacy') == 'Erm ah uhm hmm continue.'
