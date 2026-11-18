"""1250: synthetic pause seams, deliberate punctuation and exact undo."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.session_modes import (
    CommandDispatchResult, HandsFreeCommandMatch, PendingTextPolicy,
    SessionMode, SessionModeManager, UtteranceSignals,
)


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))


def manager(*, prepare=None, words=(), corrections=None, buffered=True):
    class App:
        def dispatch(self, *args):
            return None

    app = App()
    app.dictionary_store = SimpleNamespace(custom_vocab=list(words), corrections_dict=corrections or {})
    inject = Mock(side_effect=lambda text, *args: text)
    subject = SessionModeManager(
        abort_phrases=['cancel'], foreground_exe_resolver=lambda: 'synthetic.exe',
        foreground_hwnd_resolver=lambda: 1250, inject_fn=inject,
        remove_chars_fn=Mock(return_value=True), command_dispatch_fn=lambda text: CommandDispatchResult(False),
        agent_dispatch_fn=app.dispatch, buffer_dictate_until_commit=buffered,
        prepare_dictate_fn=prepare, live_surface_enabled=True,
        hands_free_command_probe_fn=lambda text: HandsFreeCommandMatch(
            dispatch_text='ellipsis', phrase='ellipsis', pending_policy=PendingTextPolicy.PRESERVE,
        ) if text.strip().lower() == 'ellipsis' else None,
    )
    subject.force_mode(SessionMode.DICTATE)
    return subject, inject


def stage(subject, *chunks):
    for chunk in chunks:
        assert subject.dispatch_utterance(chunk, GOOD).kind == 'dictate_staged'
    return subject.dictate_pending_buffer


@pytest.mark.parametrize('chunks,expected', [
    (['We need to...', 'Review the diagram.'], 'We need to review the diagram.'),
    (['We need to...', '...Review the diagram.'], 'We need to review the diagram.'),
    (['We need to…', '…Review the diagram.'], 'We need to review the diagram.'),
    (['We need to..', '..review the diagram.'], 'We need to review the diagram.'),
    (['We need to', '...Review the diagram.'], 'We need to review the diagram.'),
    # 1253: a copula is uncertain; retain the original node id for gate parity.
    pytest.param(['The lamp is blue...', 'We can leave.'], 'The lamp is blue we can leave.',
                 id='chunks5-The lamp is blue. We can leave.'),
    (['The parcel arrived...', '...We can leave.'], 'The parcel arrived. We can leave.'),
    (['The lamp is blue.', '...We can leave.'], 'The lamp is blue. We can leave.'),
    (['The task is...', 'Almost finished.'], 'The task is almost finished.'),
    (['We were thinking...', 'About the diagram.'], 'We were thinking about the diagram.'),
    (['We need the...', 'Diagram by Friday.'], 'We need the diagram by Friday.'),
    (['We need to...', '"Review the diagram."'], 'We need to "review the diagram."'),
    (['No no no...', 'No changes yet.'], 'No no no no changes yet.'),
    (['We need...', '42 Values.'], 'We need 42 Values.'),
    (['We need... another sketch before...', 'Review starts.'],
     'We need... another sketch before review starts.'),
    (['We need to...', 'Review... then revise.'], 'We need to review... then revise.'),
])
def test_pause_shapes(chunks, expected):
    subject, _ = manager()
    assert stage(subject, *chunks) == expected


@pytest.mark.parametrize('next_chunk', [
    'I can help.', "I'm available.", 'I’m available.', 'NASA can help.',
    'API support.', 'GitHub support.',
])
def test_intrinsic_capitals_survive(next_chunk):
    subject, _ = manager()
    assert stage(subject, 'We can ask whether...', next_chunk) == 'We can ask whether ' + next_chunk


@pytest.mark.parametrize('words,corrections,next_chunk', [
    (['Ada'], {}, 'Ada can help.'),
    (['Ada Lovelace'], {}, 'Lovelace can help.'),
    ([], {'ayda': 'Ada'}, 'Ada can help.'),
    (['diagram'], {}, 'Diagram review.'),
])
def test_dictionary_capitals(words, corrections, next_chunk):
    subject, _ = manager(words=words, corrections=corrections)
    expected = next_chunk if next_chunk.startswith(('Ada', 'Lovelace')) else 'diagram review.'
    assert stage(subject, 'We can ask whether...', next_chunk) == 'We can ask whether ' + expected


def test_spoken_ellipsis_is_literal_across_later_chunks_and_undo():
    subject, _ = manager()
    stage(subject, 'A deliberate pause.')
    assert subject.dispatch_utterance('ellipsis', GOOD).kind == 'hands_free_command_executed'
    assert subject.dictate_pending_buffer == 'A deliberate pause...'
    assert stage(subject, 'Then continue.', 'And finish.') == 'A deliberate pause... Then continue. And finish.'
    subject.dispatch_utterance('scratch that', GOOD)
    subject.dispatch_utterance('scratch that', GOOD)
    assert subject.dictate_pending_buffer == 'A deliberate pause...'


@pytest.mark.parametrize('first,second,expected', [
    ('We need to..', 'Review the diagram.', 'We need to review the diagram.'),
    ('The parcel arrived…', 'We can leave.', 'The parcel arrived. We can leave.'),
])
def test_scratch_restores_dots_and_repeated_join_is_identical(first, second, expected):
    subject, _ = manager()
    assert stage(subject, first, second) == expected
    subject.dispatch_utterance('scratch that', GOOD)
    assert subject.dictate_pending_buffer == first
    assert stage(subject, second) == expected


def test_prepared_chunk_removes_its_own_leading_marker_not_a_raw_length():
    subject, _ = manager(prepare=lambda text: text.strip().replace('... ', '…'))
    assert stage(subject, 'We need to...', '  ... Review  the diagram.') == 'We need to review  the diagram.'


def test_final_ellipsis_survives_staging_and_commit():
    subject, inject = manager()
    expected = 'We need to review and then...'
    assert stage(subject, 'We need to...', 'Review and then...') == expected
    assert subject.commit_pending_dictation().kind == 'dictate_committed'
    assert inject.call_args.args[0] == expected


def test_marker_only_chunk_does_not_mutate_the_draft():
    subject, _ = manager()
    stage(subject, 'We need to...')
    assert subject._stage_dictate_chunk('...').kind == 'empty'
    assert subject.dictate_pending_buffer == 'We need to...'


def test_held_take_and_immediate_delivery_keep_their_punctuation():
    subject, _ = manager()
    subject.stage_parked_hold('Synthetic... held take...')
    assert subject.dictate_pending_buffer == 'Synthetic... held take...'
    subject, inject = manager(buffered=False)
    for chunk in ['Synthetic...', 'Another take.']:
        assert subject.dispatch_utterance(chunk, GOOD).kind == 'dictate_injected'
    assert [call.args[0] for call in inject.call_args_list] == ['Synthetic...', ' Another take.']
