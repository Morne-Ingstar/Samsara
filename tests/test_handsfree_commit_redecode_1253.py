"""1253: synthetic audio/prose exercise the actual staged send path."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import draft_reread, session_modes
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals
from samsara.draft_reread import staged_chunks_stable


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


class Model:
    pass


class Decoder:
    def __init__(self):
        self.model_rate = 16000
        self.model = Model()
        self.result = 'When you move the panel to the left, it stays there.'
        self.calls = []

    def decode(self, text, refs):
        self.calls.append((text, refs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def rig(monkeypatch):
    # Keep original PCM/model identity, without invoking VAD or real decoding.
    monkeypatch.setattr(draft_reread, 'speech_pcm', lambda *_: None)
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', 'send')
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'send'}))
    decoder = Decoder()
    prepare = Mock(side_effect=lambda text: text.replace('panel', 'diagram'))
    exact = Mock(side_effect=lambda text, *_: text)
    legacy = Mock(side_effect=AssertionError('prepared draft must use exact delivery'))
    remove = Mock(return_value=True)
    manager = SessionModeManager(
        abort_phrases=['cancel'], foreground_exe_resolver=lambda: 'synthetic.exe',
        foreground_hwnd_resolver=lambda: 1253, inject_fn=legacy,
        inject_prepared_fn=exact, prepare_dictate_fn=prepare,
        remove_chars_fn=remove, command_dispatch_fn=lambda *_: None,
        agent_dispatch_fn=lambda *_: None, buffer_dictate_until_commit=True,
        commit_redecode_fn=decoder.decode, live_surface_enabled=True,
    )
    manager.reset(SessionMode.DICTATE)

    def stage(*texts, samples=32000):
        for index, text in enumerate(texts):
            pcm = np.full(samples + index * 1600, .1, dtype=np.float32)
            assert manager._stage_dictate_chunk(text, pcm).kind == 'dictate_staged'
        return manager.dictate_pending_buffer

    return SimpleNamespace(manager=manager, decoder=decoder, prepare=prepare,
                           exact=exact, legacy=legacy, remove=remove, stage=stage)


@pytest.mark.parametrize('first,second,expected', [
    ("When it's ready...", 'We can ship it.', "When it's ready we can ship it."),
    ('Because the window is empty...', 'We can close it.',
     'Because the window is empty we can close it.'),
    ('The reason this is hard...', 'Is the missing context.',
     'The reason this is hard is the missing context.'),
    ('When the panel was ready...', 'We moved it.',
     'When the diagram was ready we moved it.'),
])
def test_dependent_copula_clauses_do_not_force_a_period(rig, first, second, expected):
    assert rig.stage(first, second) == expected


def test_clear_closed_clause_keeps_its_period(rig):
    assert rig.stage('The parcel arrived...', 'We can leave.') == 'The parcel arrived. We can leave.'


@pytest.mark.parametrize('first,second', [
    ('When you move a panel...', '...to the left.'),
    ('When you move a panel.', 'to the left.'),
    ('When you move a panel', 'to the left.'),
    ('When you move a panel', 'To the left.'),
    ('When you move a panel', '...To the left.'),
])
def test_send_redecodes_long_prepared_chunks_with_any_uncertain_seam(rig, first, second):
    staged = rig.stage(first, second)
    audio = list(rig.manager._dictate_pending_audio)
    assert rig.manager.draft_document.delivery_prepared
    assert not staged_chunks_stable(rig.manager)
    result = rig.manager.dispatch_utterance('send', GOOD)
    assert result.kind == 'dictate_committed'
    assert len(rig.decoder.calls) == 1
    text, refs = rig.decoder.calls[0]
    assert text == staged and all(a is b for a, b in zip(audio, refs))
    assert len(refs) == 2
    expected = 'When you move the diagram to the left, it stays there.'
    assert rig.prepare.call_count == 3
    assert rig.prepare.call_args.args == (rig.decoder.result,)
    assert rig.exact.call_args.args[0] == result.detail['text'] == expected
    assert rig.manager._stack.peek().payload == expected
    assert rig.manager.stage_buffer == expected
    rig.legacy.assert_not_called()
    # External scratch deletes the delivered replacement's exact length.
    assert rig.manager.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    rig.remove.assert_called_once_with(len(expected))


def test_clean_two_sentence_prepared_draft_skips_redecode(rig):
    staged = rig.stage('The parcel arrived.', 'We can leave.')
    assert staged_chunks_stable(rig.manager)
    assert rig.manager.dispatch_utterance('send', GOOD).kind == 'dictate_committed'
    assert rig.decoder.calls == []
    assert rig.prepare.call_count == 2
    assert rig.exact.call_args.args[0] == staged


@pytest.mark.parametrize('invalidate', ['short', 'new_model', 'lost_audio', 'missing_metadata'])
def test_clean_skip_still_requires_duration_model_and_complete_evidence(rig, invalidate):
    rig.stage('The parcel arrived.', 'We can leave.', samples=8000 if invalidate == 'short' else 32000)
    if invalidate == 'new_model':
        rig.decoder.model = Model()
    elif invalidate == 'lost_audio':
        rig.manager._dictate_pending_audio[0] = None
    elif invalidate == 'missing_metadata':
        rig.manager._stack.peek().extra.pop('uncertain_seam')
    rig.manager.dispatch_utterance('send', GOOD)
    assert len(rig.decoder.calls) == 1


def test_an_old_seam_cannot_disappear_with_bounded_undo_history(rig):
    rig.stage('When you move a panel...', '...to the left.',
              'The parcel arrived.', 'We can leave.', 'The task ended.',
              'The rain stopped.', 'The light failed.')
    assert len(rig.manager._stack) == 5
    assert not staged_chunks_stable(rig.manager)
    rig.manager.dispatch_utterance('send', GOOD)
    assert len(rig.decoder.calls) == 1


def test_poorer_prepared_redecode_loses(rig):
    staged = rig.stage('The first sentence ended.', 'The parcel arrived...', 'We can leave.')
    rig.decoder.result = ' '.join(['synthetic'] * 110)
    rig.manager.dispatch_utterance('send', GOOD)
    assert len(rig.decoder.calls) == 1 and rig.prepare.call_count == 4
    assert rig.exact.call_args.args[0] == staged


@pytest.mark.parametrize('result', [None, '', RuntimeError('synthetic decode failure')])
def test_empty_or_failed_redecode_keeps_prepared_draft(rig, result):
    staged = rig.stage('When you move a panel...', '...to the left.')
    rig.decoder.result = result
    rig.manager.dispatch_utterance('send', GOOD)
    assert rig.exact.call_args.args[0] == staged
    assert rig.prepare.call_count == 2


@pytest.mark.parametrize('failure', ['empty', 'exception'])
def test_preparation_failure_keeps_prepared_draft(rig, failure):
    staged = rig.stage('When you move a panel...', '...to the left.')
    rig.prepare.side_effect = RuntimeError('synthetic preparation failure') if failure == 'exception' else lambda _: ''
    rig.manager.dispatch_utterance('send', GOOD)
    assert rig.exact.call_args.args[0] == staged


@pytest.mark.parametrize('excluded', ['manual_edit', 'parked_hold'])
def test_edits_and_parked_holds_are_never_redecoded_or_reprepared(rig, excluded):
    rig.stage('When you move a panel...', '...to the left.')
    if excluded == 'manual_edit':
        rig.manager.draft_document.replace_text('Synthetic  manual edit.', edited=True)
    else:
        assert rig.manager.stage_parked_hold('Synthetic  held take.').kind == 'dictate_staged'
    expected = rig.manager.dictate_pending_buffer
    calls = rig.prepare.call_count
    rig.manager.dispatch_utterance('send', GOOD)
    assert rig.decoder.calls == [] and rig.prepare.call_count == calls
    assert rig.exact.call_args.args[0] == expected


def test_staged_scratch_and_redo_preserve_audio_and_seam_evidence(rig):
    rig.stage('When you move a panel...', '...to the left.')
    expected = rig.manager.dictate_pending_buffer
    audio = list(rig.manager._dictate_pending_audio)
    assert rig.manager.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert rig.manager.dictate_pending_buffer == 'When you move a diagram...'
    assert len(rig.manager._dictate_pending_audio) == 1
    assert rig.manager.dispatch_utterance('redo that', GOOD).kind == 'scratch_success'
    assert rig.manager.dictate_pending_buffer == expected
    assert all(a is b for a, b in zip(audio, rig.manager._dictate_pending_audio))
    assert not staged_chunks_stable(rig.manager)
    assert rig.manager._stack.peek().extra['uncertain_seam']


def test_consumed_legacy_commit_word_is_removed_before_preparation(rig):
    rig.stage('When you move a panel...', '...to the left.')
    # Preserve the existing decoder-suffix policy (end/and), without changing it.
    rig.decoder.result = 'When you move the panel to the left. End.'
    outcome = rig.manager._commit_dictate_buffer(target_mode=None, consumed_trailing_commit_word=True)
    assert outcome.kind == 'dictate_committed'
    assert rig.prepare.call_args.args == ('When you move the panel to the left.',)
    assert rig.exact.call_args.args[0] == 'When you move the diagram to the left.'


def test_previous_sent_seam_does_not_poison_a_new_clean_draft(rig):
    rig.stage('When you move a panel...', '...to the left.')
    rig.manager.dispatch_utterance('send', GOOD)
    assert len(rig.decoder.calls) == 1
    rig.stage('The parcel arrived.', 'We can leave.')
    assert staged_chunks_stable(rig.manager)
    rig.manager.dispatch_utterance('send', GOOD)
    assert len(rig.decoder.calls) == 1


def test_prepared_redecode_preserves_final_spacing_exactly(rig):
    rig.stage('When you move a panel...', '...to the left.')
    rig.decoder.result = 'Synthetic  replacement.\nSecond line.'
    rig.manager.dispatch_utterance('send', GOOD)
    assert rig.exact.call_args.args[0] == rig.decoder.result
