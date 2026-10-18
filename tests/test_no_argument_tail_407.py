"""407: complete-utterance dispatch with live registry and inert effects."""
import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from samsara import execution_policy as policy
from samsara.command_catalog import build_catalog, canonical_phrase
from samsara.command_registry import DispatchState as State
from tests.test_several_commands_399 import rig
from tools import plain_speech_bench as bench

ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads((ROOT/'tools/plain_speech_corpus.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('forced', [False, True])
@pytest.mark.parametrize('text', [
    'save the whales is their campaign slogan',
    'volume down the street has increased since construction began'])
def test_ordinary_sentences_are_misses_and_preserve_text(rig, text, forced):
    executor, app, calls = rig
    result = executor.process_text(text, force_commands=forced)
    assert result.state is State.MISS
    assert tuple(result) == (text, False)
    assert calls == []


@pytest.mark.parametrize('tail', [
    'please', 'now', 'thanks', 'thank you', 'for me', 'right now',
    'if you can', 'real quick', 'would you', 'can you', 'could you',
    'just', 'hey', 'okay', 'um', 'uh', 'so',
    'for me please', 'please for me', 'right now please',
    'if you can real quick thank you', 'would you please for me now'])
def test_allowed_tail(rig, tail):
    executor, app, calls = rig
    assert executor.process_text('volume down '+tail, force_commands=True).state is State.COMPLETED
    assert calls == [('volume down', tail)]


@pytest.mark.parametrize('tail', ['please tell me why', 'now the road is busy',
                                 'thanks for the memories', 'thank you for calling'])
def test_tail_must_consume_the_remainder(rig, tail):
    executor, app, calls = rig
    assert executor.process_text('volume down '+tail, force_commands=True).state is State.MISS
    assert calls == []


@pytest.mark.parametrize('forced', [False, True])
@pytest.mark.parametrize('text', [
    'save the whales please',
    'save the date for me',
    'save our seats right now',
    'save this story if you can',
    'save her a place thanks',
    'volume down the street right now',
    'volume down the hallway please',
    'volume down the road for me',
    'volume down the avenue now',
    'volume down the block thank you',
    'mute the debate please',
    'mute the narrator for me',
    'mute this argument right now',
    'mute your objections thanks',
    'mute the conversation real quick',
    'pause the discussion please',
    'pause the recording for me',
    'pause the lecture right now',
    'pause the story if you can',
    'pause the meeting thanks',
    'copy the manuscript please',
    'copy the invitation for me',
    'copy our notes right now',
    'copy the recipe thank you'])
def test_content_tail_ending_in_filler_is_still_a_miss(rig, text, forced):
    executor, app, calls = rig
    entry, remainder = executor._matcher.match(text)
    assert entry is not None and remainder and not executor._takes_argument(entry)
    result = executor.process_text(text, force_commands=forced)
    assert result.state is State.MISS
    assert tuple(result) == (text, False)
    assert calls == []


@pytest.mark.parametrize('text,phrase,remainder', [
    ('search for cats and dogs', 'search for', 'cats and dogs'),
    ('find tab github', 'find tab', 'github'),
    ('close the window before it rains', 'close', 'the window before it rains'),
    ('put this video on hold', 'send', 'this video on hold')])
def test_argument_commands_keep_their_remainder(rig, monkeypatch, text, phrase, remainder):
    executor, app, calls = rig
    records = [r.to_dict() for r in build_catalog(executor)]
    executor._matcher.set_enabled_packs({r['pack'] for r in records})
    # This test covers matching/argument preservation; every effect is inert.
    monkeypatch.setattr(policy, 'authorize', lambda *args, **kwargs: policy.Allowed())
    executor.process_text(text, force_commands=True)
    assert calls == [(phrase, remainder)]


def test_rejection_logs_counts_only_and_does_not_try_shorter_prefix(rig, monkeypatch, caplog):
    executor, app, calls = rig
    entry, _ = executor._matcher.match('volume down private sentence')
    shorter, _ = executor._matcher.match('search for cats')
    seen = []
    original = executor._matcher.match
    def match(text, context=None):
        seen.append(text)
        # The same complete query must stay deterministic across admission
        # probes. A shortened retry would resolve to a different command.
        return (entry, 'private sentence') if text == 'volume down private sentence' else (shorter, 'cats')
    monkeypatch.setattr(executor._matcher, 'match', match)
    app.config['commands']['grammar_tier'] = False
    with caplog.at_level('INFO'):
        result = executor.process_text('volume down private sentence', force_commands=True)
    assert result.state is State.MISS
    assert calls == []
    # Admission may probe again; every query must retain the complete text.
    assert seen and all(text == 'volume down private sentence' for text in seen)
    assert "[CMD] 'volume down' needs the whole sentence; treating '2 extra words' as not a command" in caplog.text
    assert 'private sentence' not in caplog.text
    monkeypatch.setattr(executor._matcher, 'match', original)


def test_rejected_prefix_reaches_single_grammar_tier(rig, monkeypatch):
    executor, app, calls = rig
    seen = []
    monkeypatch.setattr(executor, '_grammar_single', lambda *args: seen.append(args[0]))
    result = executor.process_text('volume down private sentence', force_commands=True)
    assert seen == ['volume down private sentence']
    assert result.state is State.MISS
    assert not calls


def replay_corpus(rig, monkeypatch, rule_enabled):
    executor, app, calls = rig
    # 415: measure the independent 407 tail rule under its legacy admission setting.
    app.config.setdefault("command_mode", {})["dictation_literal_commands_only"] = False
    records = [s.to_dict() for s in build_catalog(executor)]
    ids = {canonical_phrase(r): r['canonical_id'] for r in records}
    args = {r['canonical_id']: bool(r['args']) for r in records}
    with bench.synthetic_desktop(CORPUS) as context, monkeypatch.context() as patch:
        tiers = bench.LiveTiers(CORPUS, context)
        executor._matcher.set_enabled_packs(tiers.executor._matcher._enabled_packs)
        app.config['command_packs'] = tiers.pack_config
        if not rule_enabled:
            patch.setattr(executor, '_takes_argument', lambda entry: True)
        outcomes = []
        for row in CORPUS['utterances']:
            executor._matcher.set_context_provider(lambda text=row['text']: tiers.context_for(text))
            calls.clear()
            result = executor.process_text(row['text'], force_commands=row['tag'] != 'dictation')
            outcomes.append({**row, 'state': result.state.value, 'calls': calls[:],
                             'ids': [ids[p] for p, rest in calls],
                             'argument_bearing': [args[ids[p]] for p, rest in calls]})
        return outcomes


def test_all_200_corpus_rows_before_and_after(rig, monkeypatch):
    before = replay_corpus(rig, monkeypatch, False)
    after = replay_corpus(rig, monkeypatch, True)
    assert len(before) == len(after) == 200
    for old, new in zip(before, after, strict=True):
        if new['tag'] == 'exact':
            assert new['ids'] == old['ids'] == [new['expected']['canonical_id']]
        if new['tag'] == 'dictation':
            assert not any(not value for value in new['argument_bearing'])
        if old['argument_bearing'] and all(old['argument_bearing']):
            assert new['calls'] == old['calls']
    assert sum(bool(r['calls']) for r in before if r['tag'] == 'dictation') == 13
    assert sum(bool(r['calls']) for r in after if r['tag'] == 'dictation') == 3


def test_mechanical_remainder_reader_audit_and_legacy_behavior(rig, monkeypatch):
    executor, app, calls = rig
    records = [r.to_dict() for r in build_catalog(executor)]
    executor._matcher.set_enabled_packs({r['pack'] for r in records})
    monkeypatch.setattr(policy, 'authorize', lambda *args, **kwargs: policy.Allowed())
    seen = set()
    readers = []
    for entry in executor._matcher._sorted:
        if id(entry) in seen or entry.handler is None:
            continue
        seen.add(id(entry))
        handler = inspect.unwrap(entry.handler)
        path = Path(inspect.getsourcefile(handler)).resolve()
        assert path.is_relative_to(ROOT)
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        fn = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
        if len(fn.args.args) < 2:
            continue
        parameter = fn.args.args[1].arg
        body = fn.body
        if (body and isinstance(body[0], ast.If)
                and isinstance(body[0].test, ast.Name) and body[0].test.id == parameter
                and not body[0].orelse and len(body[0].body) == 1
                and isinstance(body[0].body[0], ast.Return)
                and isinstance(body[0].body[0].value, ast.Constant)
                and body[0].body[0].value.value is False):
            # Selection-edit handlers READ the remainder only to refuse it.
            # They must stay no-argument commands, rather than swallow prose.
            assert not executor._takes_argument(entry), entry.phrase
            body = body[1:]
        if any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == parameter
               for statement in body for n in ast.walk(statement)):
            assert executor._takes_argument(entry), entry.phrase
            if not entry.param_schema:
                readers.append(entry)
    assert readers
    for entry in readers:
        text = entry.phrase+' sample argument'
        calls.clear()
        with monkeypatch.context() as patch:
            patch.setattr(executor, '_takes_argument', lambda entry: True)
            old = executor.process_text(text, force_commands=True)
            expected = calls[:]
        calls.clear()
        new = executor.process_text(text, force_commands=True)
        assert new.state is old.state, entry.phrase
        assert calls == expected, entry.phrase
