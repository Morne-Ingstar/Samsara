"""Conservative post-decode stutter cleanup; no app bootstrap or live data."""
import pytest

from samsara.cleanup import _remove_stutters, clean_text
from tools.voice_corpus.script import SCRIPT


@pytest.mark.parametrize('text,expected', [
    ('ac, ac, accessibility', 'Accessibility.'),
    ('b- b- because', 'Because.'),
    ('ke. key', 'Key.'),
    ('acc... accessibility', 'Accessibility.'),
    ('ac accessibility', 'Accessibility.'),
    ('ac ac accessibility', 'Accessibility.'),
    ('ac.ac.accessibility', 'Accessibility.'),
    ('ac…ac…accessibility', 'Accessibility.'),
    ('ac-ac-accessibility', 'Accessibility.'),
    ('ac, acc, accessibility', 'Accessibility.'),
    ('ac, ac, Accessibility', 'Accessibility.'),
    ('AC, AC, accessibility', 'Accessibility.'),
    ('Ac, Ac, accessibility', 'Accessibility.'),
    ('Jo, Jo, John arrived.', 'John arrived.'),
    ('We need ac, ac, accessibility today.', 'We need accessibility today.'),
    ('Try ac, ac, accessibility, please.', 'Try accessibility, please.'),
    ('First. ac, ac, accessibility matters.', 'First. Accessibility matters.'),
    ('Try ac, accessibility and b- because.', 'Try accessibility and because.'),
    ('Try ac, ac, accessibility?', 'Try accessibility?'),
    ('Try ac, ac, accessibility!', 'Try accessibility!'),
    ('the the the cat', 'The cat.'),
    ('The the the cat', 'The cat.'),
    ('THE THE THE cat', 'THE cat.'),
    ('the, the, the cat', 'The cat.'),
    ('the the cat', 'The cat.'),
    ('and and continue', 'And continue.'),
    ('to to town', 'To town.'),
    ('of of the town', 'Of the town.'),
    ('I I agree', 'I agree.'),
    ('I, I, I agree', 'I agree.'),
    ('i i agree', 'I agree.'),
    ('THE THE cat', 'THE cat.'),
    ('Try the the the the cat.', 'Try the cat.'),
    ('Please send send send the parcel.', 'Please send the parcel.'),
    ('Please uh the the cat.', 'Please the cat.'),
])
def test_clean_removes_evidenced_stutters(text, expected):
    assert clean_text(text, mode='clean') == expected


def test_reported_sentence():
    # Assemble the supplied regression example without copying it into evidence.
    lead = 'open source, free, key., '
    repair = 'accessibility application.'
    assert clean_text(lead + 'ac, ac, ' + repair) == 'Open' + lead[4:] + repair


@pytest.mark.parametrize('text', [
    'A apple.', 'I idea.', 'In instead.', 'Be because.', 'An answer.',
    'To tomorrow.', 'In, in instead.', 'Be, be because.',
    'An, an answer.', 'He helped.', 'Her herself.', 'Can candy.',
    'Art artist.', 'Car carpet.', 'App apple.', 'Key keyboard.',
    'Use useful.', 'Part partly.', 'Re-read the note.', 'Co-worker arrived.',
    'AC adapter.', 'Ac, the app.', 'Ac, accessibility matters.',
    'AC accessibility matters.', 'Jo John arrived.', 'Al Albert arrived.',
    'Dr. Drake arrived.', 'The AC AC adapter.', 'The SQL SQL SQL query.',
    'Alice Alice arrived.', 'Alice Alice Alice arrived.',
    'I know that that works.', 'She had had enough.', 'What it is is fine.',
    'No no.', 'No no no.', 'Bye bye.', 'Bye-bye.', 'Very very good.',
    'Really really good.', 'So so.', 'So so so.', 'One one one.',
    'Two two.', 'Twenty twenty twenty.', '1 1 1.', '42 42 42.',
    'Please please please stop.', 'Yes yes yes.', 'Stop stop stop.',
    'Send send the parcel.', 'Try aaaa apple.', 'Try ssssnake.',
    'Try ke, cat.', 'Try ac; accessibility.', 'Try ac: accessibility.',
    'Try ac! Accessibility.', 'Try ac? Accessibility.',
    'Try accessibility\nac.', 'The. The. The cat.', 'I. I agree.',
    'Try ac_accessibility.', 'Try ac2 accessibility.',
    "The cat's cat's collar.", 'The café café café.',
])
def test_clean_keeps_ambiguous_or_meaningful_speech(text):
    assert _remove_stutters(text) == text
    assert clean_text(text, mode='clean') == text


@pytest.mark.parametrize('text', [
    'ac, ac, accessibility', 'b- b- because', 'ke. key',
    'acc... accessibility', 'the the the cat', 'and and continue',
    'I I agree', 'no no no', 'AC, AC, accessibility',
    ' erm, uh, ac, ac, accessibility\n', '', None,
])
def test_verbatim_remains_byte_identical(text):
    assert clean_text(text, mode='verbatim') == text


def test_new_rules_are_clean_only():
    text = 'ac, ac, accessibility and and the the the cat'
    assert clean_text(text, mode='legacy') == text[0].upper() + text[1:] + '.'


@pytest.mark.parametrize('item', SCRIPT, ids=lambda item: item['id'])
def test_all_corpus_references_survive_stutter_pass_byte_identically(item):
    for field in ('intended_text', 'expected_text'):
        reference = item[field]
        assert _remove_stutters(reference).encode() == reference.encode()
        # The full formatter adds punctuation/caps; its result is also stable.
        formatted = clean_text(reference)
        assert _remove_stutters(formatted) == formatted


@pytest.mark.parametrize('item', [
    item for item in SCRIPT
    if {'legitimate_repeat', 'doubled_words'} & set(item['tags'])
], ids=lambda item: item['id'])
def test_corpus_repeat_preservation(item):
    reference = item['expected_text']
    assert clean_text(reference) == reference + '.'


def test_history_keeps_recoverable_raw_text(tmp_path):
    from samsara.history import HistoryManager

    raw = 'Try ac, ac, accessibility.'
    history = HistoryManager(tmp_path / 'history.db')
    try:
        history.add(raw_text=raw, display_text=clean_text(raw))
        entry = history.recent()[0]
        assert entry['raw_text'] == raw
        assert entry['display_text'] == 'Try accessibility.'
    finally:
        history.close()
