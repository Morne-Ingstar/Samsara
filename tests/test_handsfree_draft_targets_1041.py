"""1041: local commands never type into another application or wait to edit.

All desktop effects are recording ports. The maintained gate adapter supplies
the production whole DictationApp class for its read-only command probe.
"""
import json
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.draft_commands import INSERTIONS
from samsara.formatting_tokens import apply_formatting_tokens, apply_formatting_tokens_if_enabled
from samsara.session_modes import (
    CommandDispatchResult, HandsFreeCommandMatch, PendingTextPolicy,
    SessionMode, SessionModeManager, StackItem, UtteranceSignals,
)


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))
BAD = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(1.0,))


@pytest.fixture
def rig(monkeypatch):
    from dictation import _HANDS_FREE_PASTE_FIRST_COMMANDS
    from samsara import session_modes
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', 'end')
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'end'}))
    commands = json.loads((Path(__file__).parents[1] / 'commands.json').read_text(encoding='utf-8'))['commands']
    app = SimpleNamespace(config={}, command_executor=SimpleNamespace(commands=commands))
    app.agent = MethodType(lambda self, *args: None, app)
    effects, waits, pasted, removed = [], [], [], []
    vocabulary = set(commands) | {'select word', 'select line', 'select sentence',
        'select paragraph', 'select previous', 'insert', 'undo in word', 'redo in word', 'draft spelling', 'greet me'}

    def probe(text):
        phrase = 'question mark' if text == '?' else text.lower().rstrip('.!?,;:')
        if phrase.startswith('command select previous '):
            phrase = 'select previous'
        elif phrase.startswith(('command insert ', 'command paste snippet ')):
            phrase = 'insert'
        elif phrase == '?':
            phrase = 'question mark'
        if phrase not in vocabulary:
            return None
        policy = (PendingTextPolicy.COMMIT if phrase in _HANDS_FREE_PASTE_FIRST_COMMANDS
                  else PendingTextPolicy.PRESERVE)
        return HandsFreeCommandMatch(text, phrase, policy)

    manager = SessionModeManager(
        abort_phrases=['cancel dictation'], foreground_exe_resolver=lambda: 'editor.exe',
        foreground_hwnd_resolver=lambda: 123,
        inject_fn=lambda text, *args, **kwargs: pasted.append(text) or text,
        inject_prepared_fn=lambda text, *args, **kwargs: pasted.append(text) or text,
        prepare_dictate_fn=apply_formatting_tokens,
        format_dictate_fn=apply_formatting_tokens,
        remove_chars_fn=lambda count: removed.append(count) or True,
        command_dispatch_fn=lambda text: effects.append(text) or CommandDispatchResult(True, text),
        agent_dispatch_fn=app.agent, buffer_dictate_until_commit=True,
        hands_free_command_probe_fn=probe,
        cancel_window_fn=lambda match, run: waits.append(match.phrase) or 0,
        # Text commands must also work during a natural dictation fragment.
        fragment_clear_gap_s=2,
    )
    manager.reset(SessionMode.DICTATE)
    app._session_mode_manager = manager
    return SimpleNamespace(manager=manager, app=app, effects=effects, waits=waits,
                           pasted=pasted, removed=removed,
                           say=lambda text, signals=GOOD: manager.dispatch_utterance(text, signals))


@pytest.mark.parametrize('phrase,insertion', INSERTIONS.items())
def test_every_literal_text_command_edits_draft_immediately(rig, phrase, insertion):
    rig.say('synthetic sample')
    result = rig.say(phrase)
    assert result.detail['target'] == 'draft'
    assert rig.manager.dictate_pending_buffer == 'synthetic sample' + insertion
    assert rig.effects == rig.waits == rig.pasted == rig.removed == []
    assert rig.manager._do_scratch_that_n(1)['scratched'] == 1
    assert rig.manager.dictate_pending_buffer == 'synthetic sample'


@pytest.mark.parametrize('phrase', list(INSERTIONS) + ['delete that', 'undo', 'backspace',
    'select all', 'delete word', 'bold', 'italic', 'underline', 'strikethrough',
    'copy', 'copy that', 'cut', 'cut that', 'paste', 'redo'])
def test_without_draft_existing_external_route_and_window_are_preserved(rig, phrase):
    result = rig.say(phrase)
    assert result.kind == 'hands_free_command_executed'
    assert rig.effects == [phrase]
    assert rig.waits == [phrase]
    assert rig.manager.dictate_pending_buffer == ''


@pytest.mark.parametrize('phrase,expected', [
    ('question mark', 'synthetic sample?'), ('comma', 'synthetic sample,'),
    ('period', 'synthetic sample.'), ('new line', 'synthetic sample.\n'),
    ('new paragraph', 'synthetic sample.\n\n'),
])
def test_explicit_punctuation_corrects_decoder_period(rig, phrase, expected):
    rig.say('synthetic sample.')
    rig.say(phrase)
    assert rig.manager.dictate_pending_buffer == expected


@pytest.mark.parametrize('phrase', ['undo', 'delete that', 'scratch that', 'undo that'])
def test_undo_family_restores_local_punctuation(rig, phrase):
    rig.say('synthetic sample')
    rig.say('question mark')
    rig.say(phrase)
    assert rig.manager.dictate_pending_buffer == 'synthetic sample'
    assert rig.effects == rig.waits == rig.removed == []


@pytest.mark.parametrize('phrase', ['scratch that', 'undo that'])
def test_scratch_without_draft_uses_verified_external_undo_port(rig, phrase):
    rig.manager._stack.push(StackItem('dictation_chunk', 'abc', SessionMode.DICTATE, 0,
                                    {'target_process': 'editor.exe', 'hwnd': 123}))
    assert rig.say(phrase).kind == 'scratch_success'
    assert rig.removed == [3]
    assert rig.effects == rig.waits == []


@pytest.mark.parametrize('phrase,expected', [('cap that', 'synthetic Sample'),
    ('uppercase that', 'synthetic SAMPLE'), ('lowercase that', 'synthetic sample')])
def test_case_controls_are_local(rig, phrase, expected):
    rig.say('synthetic sample')
    rig.say(phrase)
    assert rig.manager.dictate_pending_buffer == expected
    assert rig.effects == rig.waits == []


def test_cap_that_without_draft_remains_dictation(rig):
    # No global catalog entry exists for this new draft-only control.
    assert rig.say('cap that').kind == 'dictate_staged'
    assert rig.manager.dictate_pending_buffer == 'cap that'
    assert rig.effects == []


@pytest.mark.parametrize('select,expected', [('select last word', 'synthetic '),
    ('select previous word', 'synthetic '), ('select word left', 'synthetic '),
    ('select word', 'synthetic '), ('select sentence', ''), ('select line', ''),
    ('select paragraph', ''), ('select all', ''), ('select that', '')])
def test_selection_and_deletion_stay_local(rig, select, expected):
    rig.say('synthetic sample')
    rig.say(select)
    rig.say('delete selection')
    assert rig.manager.dictate_pending_buffer == expected
    assert rig.effects == rig.waits == rig.removed == []
    rig.say('undo')
    assert rig.manager.dictate_pending_buffer == 'synthetic sample'


@pytest.mark.parametrize('phrase,expected', [('backspace', 'synthetic sampl'),
    ('delete word', 'synthetic '), ('delete next word', 'synthetic sample'),
    ('delete line', ''), ('delete', 'synthetic sample')])
def test_deletion_at_draft_caret(rig, phrase, expected):
    rig.say('synthetic sample')
    rig.say(phrase)
    assert rig.manager.dictate_pending_buffer == expected
    assert rig.effects == rig.waits == rig.removed == []


def test_movement_selection_replacement_and_stale_selection(rig):
    rig.say('synthetic sample')
    rig.say('go to document start')
    rig.say('select word right')
    rig.say('replacement')
    assert rig.manager.dictate_pending_buffer == 'replacement sample'
    rig.manager._draft.replace_text('independent revision')
    rig.say('backspace')
    assert rig.manager.dictate_pending_buffer == 'independent revisio'


@pytest.mark.parametrize('separator', ['space', 'new line', 'new paragraph', 'open parenthesis'])
def test_next_dictation_respects_spoken_separator(rig, separator):
    rig.say('synthetic sample')
    rig.say(separator)
    rig.say('continuation')
    assert rig.manager.dictate_pending_buffer == 'synthetic sample' + INSERTIONS[separator] + 'continuation'
    assert rig.effects == rig.waits == []


def test_edit_redo_and_counted_scratch(rig):
    rig.say('synthetic sample')
    rig.say('question mark')
    rig.say('undo')
    rig.say('redo')
    assert rig.manager.dictate_pending_buffer == 'synthetic sample?'
    rig.say('scratch that twice')
    assert rig.manager.dictate_pending_buffer == ''
    rig.say('redo that')
    assert rig.manager.dictate_pending_buffer == 'synthetic sample?'
    assert rig.effects == rig.waits == rig.removed == []


@pytest.mark.parametrize('phrase,paste_first', [
    ('bold', True), ('italic', True), ('underline', True),
    ('strikethrough', False), ('copy', False), ('copy that', False),
    ('cut', False), ('cut that', False), ('paste', False),
])
def test_native_formatting_and_clipboard_keep_existing_policy(rig, phrase, paste_first):
    # FINISH IT 1: native formatting/clipboard commands retain 447's app route.
    rig.manager._fragment_clear_gap_s = 0
    rig.say('synthetic sample')
    draft = rig.manager.draft_document
    before = (draft.document_id, draft.revision, draft.segments, draft.manual_commit)
    assert rig.say(phrase).kind == 'hands_free_command_executed'
    assert rig.effects == rig.waits == [phrase]
    assert rig.pasted == (['synthetic sample'] if paste_first else [])
    assert rig.manager.dictate_pending_buffer == ('' if paste_first else 'synthetic sample')
    if not paste_first:
        assert (draft.document_id, draft.revision, draft.segments, draft.manual_commit) == before


@pytest.mark.parametrize('phrase', ['bold', 'italic', 'underline'])
def test_failed_native_formatting_paste_preserves_draft_and_blocks_effect(rig, phrase):
    rig.manager._fragment_clear_gap_s = 0
    rig.say('synthetic sample')
    rig.manager._inject_fn = rig.manager._inject_prepared_fn = lambda *args, **kwargs: False
    result = rig.say(phrase)
    assert result.kind == 'hands_free_command_blocked'
    assert result.detail['commit_outcome'] == 'dictate_commit_failed'
    assert rig.manager.dictate_pending_buffer == 'synthetic sample'
    assert rig.effects == []


def test_counted_selection_and_snippet_payloads_remain_data(rig, monkeypatch):
    from samsara import snippets
    monkeypatch.setattr(snippets, 'find_snippet', lambda name: {'text': 'undo; delete that'} if name == 'sample' else None)
    rig.say('synthetic first second')
    rig.say('command select previous two words')
    rig.say('command insert sample')
    assert rig.manager.dictate_pending_buffer == 'synthetic undo; delete that'
    assert rig.effects == rig.waits == []


@pytest.mark.parametrize('phrase,paste_first', [
    ('undo in word', False), ('redo in word', False), ('heading one', False),
    ('highlight selection in obsidian', False), ('submit', True), ('other window', False),
])
def test_explicit_app_commands_keep_original_route(rig, phrase, paste_first):
    rig.manager._fragment_clear_gap_s = 0
    rig.say('synthetic sample')
    rig.say(phrase)
    assert rig.effects == [phrase]
    assert rig.waits == [phrase]
    assert rig.manager.dictate_pending_buffer == ('' if paste_first else 'synthetic sample')
    assert rig.pasted == (['synthetic sample'] if paste_first else [])


def test_bad_signal_and_literal_prefix_never_apply_local_edit(rig):
    rig.say('synthetic sample')
    assert rig.say('question mark', BAD).kind == 'hands_free_command_refused'
    assert rig.manager.dictate_pending_buffer == 'synthetic sample'
    rig.say('literal cap that')
    assert rig.manager.dictate_pending_buffer.endswith('cap that')
    assert rig.effects == rig.waits == []


def test_question_mark_symbol_and_final_send_are_exact(rig):
    rig.say('synthetic sample.')
    rig.say('?')
    rig.say('end')
    assert rig.pasted == ['synthetic sample?']
    assert rig.effects == rig.waits == []


def test_real_window_is_skipped_only_while_draft_is_owned(rig):
    rig.manager._cancel_window_fn = Mock(return_value=1.5)
    assert rig.say('question mark').kind == 'command_cancel_window'
    rig.say('synthetic sample')
    assert rig.say('question mark').detail['target'] == 'draft'
    assert rig.manager._cancel_window_fn.call_count == 1
    assert rig.effects == []


def test_empty_local_edit_stops_owning_commands_after_finish(rig):
    rig.say('synthetic sample')
    rig.say('select all')
    rig.say('delete selection')
    assert rig.say('end').kind == 'dictate_committed'
    rig.say('question mark')
    assert rig.effects == ['question mark']


def test_case_and_ordinary_commands_do_not_claim_spelling_capture(rig):
    rig.say('synthetic sample')
    rig.app._draft_spelling_input = SimpleNamespace(active=True)
    rig.manager._hands_free_command_probe_fn = lambda text: HandsFreeCommandMatch(text, 'draft spelling')
    rig.say('backspace')
    rig.say('cap that')
    assert rig.effects == ['backspace', 'cap that']
    assert rig.manager.dictate_pending_buffer == 'synthetic sample'


@pytest.mark.parametrize('undo', ['scratch that', 'undo', 'delete that'])
def test_unrelated_app_command_cannot_hide_local_undo(rig, undo):
    rig.manager._fragment_clear_gap_s = 0
    rig.say('synthetic sample')
    rig.say('question mark')
    rig.say('other window')
    rig.say(undo)
    assert rig.manager.dictate_pending_buffer == 'synthetic sample'
    assert rig.effects == ['other window']
    assert rig.removed == []


def test_example_plugin_text_uses_its_shared_payload_and_local_undo(rig):
    from plugins.commands.example_greet import GREETING_TEXT
    rig.say('synthetic sample')
    rig.say('greet me')
    assert rig.manager.dictate_pending_buffer == 'synthetic sample' + GREETING_TEXT
    rig.say('undo')
    assert rig.manager.dictate_pending_buffer == 'synthetic sample'
    assert rig.effects == rig.waits == []


@pytest.mark.parametrize('source,expected', [
    ('is that right question mark', 'is that right?'),
    ('is that right question mark.', 'is that right?'),
    ('synthetic comma sample period.', 'synthetic, sample.'),
    ('synthetic question mark, next', 'synthetic? next'),
    ('synthetic new paragraph sample', 'synthetic\n\nsample'),
])
def test_inline_punctuation_is_displayed_in_draft_and_idempotent(rig, source, expected):
    rig.say(source)
    assert rig.manager.dictate_pending_buffer == expected
    assert apply_formatting_tokens(expected) == expected
    assert apply_formatting_tokens_if_enabled(source, False) == source
    assert rig.effects == rig.waits == []


@pytest.mark.parametrize('punctuation', ['question mark', '?'])
def test_production_exact_probe_and_disabled_pack(rig, punctuation):
    from dictation import DictationApp
    from samsara.commands import CommandExecutor
    from samsara.command_registry import CommandMatcher
    from samsara.command_scope import MatchContext
    matcher = CommandMatcher()
    matcher.set_enabled_packs({'core', 'text-editing'})
    matcher.load_builtins(rig.app.command_executor.commands)
    matcher.freeze()
    matcher.set_context_provider(lambda: MatchContext.for_app('editor.exe'))
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.commands = rig.app.command_executor.commands
    executor._matcher = matcher
    executor._matcher_lock = threading.RLock()
    executor._app = rig.app
    executor._resources = None
    rig.app.command_executor = executor
    rig.app._probe_media_transport = MethodType(DictationApp._probe_media_transport, rig.app)
    rig.manager._hands_free_command_probe_fn = MethodType(DictationApp._probe_hands_free_command, rig.app)
    rig.say('synthetic sample')
    assert rig.say(punctuation).detail['target'] == 'draft'
    assert rig.manager.dictate_pending_buffer == 'synthetic sample?'
    matcher.set_enabled_packs({'core'})
    assert rig.say('comma').kind == 'dictate_staged'
    assert rig.effects == rig.waits == []


@pytest.mark.parametrize('source,expected', [('camel synthetic sample', 'syntheticSample'),
    ('page twenty one', 'page 21')])
def test_production_number_and_case_preparation_already_targets_draft(rig, source, expected):
    from dictation import DictationApp
    rig.app.config.update(enable_case_formatters=True, format_numbers=True, auto_capitalize=False)
    rig.manager._prepare_dictate_fn = MethodType(DictationApp.process_transcription, rig.app)
    rig.say(source)
    assert rig.manager.dictate_pending_buffer == expected
    assert rig.effects == rig.waits == rig.pasted == []


def test_verbatim_spelling_already_targets_draft(rig):
    from samsara.verbatim import apply
    rig.manager._prepare_dictate_fn = apply
    rig.say('spell cap g i t')
    assert rig.manager.dictate_pending_buffer == 'Git'
    assert rig.effects == rig.waits == rig.pasted == []
