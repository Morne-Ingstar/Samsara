"""Offline exact-lane versus unwired deciders; never dispatch or bootstrap.

Default: repository synthetic fixtures. --from-shadow PATH reads only that
explicit file/folder, prints to the terminal, and never saves utterances.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import json
import logging
import sys
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def bind_probe(executor, config=None):
    """Bind exact production definitions, without importing dictation."""
    from samsara.commands import apply_phonetic_wash
    from samsara.session_modes import HandsFreeCommandMatch, PendingTextPolicy, normalize_utterance
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    names = {'_probe_media_transport', '_probe_hands_free_command'}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    constants = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in {
            '_HANDS_FREE_PRESERVE_COMMANDS', '_HANDS_FREE_COMMIT_COMMANDS',
            '_HANDS_FREE_COMMIT_PREFIXES'} for t in n.targets)]
    namespace = dict(HandsFreeCommandMatch=HandsFreeCommandMatch, PendingTextPolicy=PendingTextPolicy,
                     normalize_utterance=normalize_utterance, apply_phonetic_wash=apply_phonetic_wash,
                     logger=logging.getLogger(__name__))
    exec(compile(ast.Module(body=constants + methods, type_ignores=[]), 'dictation.py', 'exec'), namespace)
    app = SimpleNamespace(command_executor=executor, config=config or {})
    for name in names:
        setattr(app, name, MethodType(namespace[name], app))
    return app._probe_hands_free_command


def load_rows(from_shadow=None):
    if from_shadow is not None:
        path = Path(from_shadow)
        files = sorted(path.glob('intent-*.jsonl')) if path.is_dir() else [path]
        rows = []
        for file in files:
            for line in file.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                rows.append(dict(text=row['text'], dictation=row.get('would') == 'dictate',
                                 app=row.get('app'), source=file.name))
        return {'shadow': rows}
    rows = [json.loads(line) for line in
            (ROOT / 'tests/fixtures/intent_eval.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    # other_command negatives reject ONE named command, not all commands;
    # their texts deliberately request a different command (gen_intent_eval).
    groups = {'intent_eval': [dict(text=r['text'], dictation=(r['polarity'] == 'negative'
                                                            and r['kind'] != 'other_command')) for r in rows]}
    corpus = ROOT / 'tools/plain_speech_corpus.json'
    if corpus.exists():
        groups['398_dictation'] = [dict(text=r['text'], dictation=True) for r in
                                  json.loads(corpus.read_text(encoding='utf-8'))['utterances']
                                  if r['expected'] == 'dictation']
    return groups


def measure(rows, executor, records, resolver, *, context):
    from samsara import grammar
    from samsara.command_scope import MatchContext
    from samsara.commands import apply_phonetic_wash
    from samsara.session_modes import normalize_utterance
    probe = bind_probe(executor)
    totals = dict(rows=len(rows), labelled_dictation=0, exact=0, exact_dictation=0,
                  lane_execute=0, lane_stage=0, lane_false_positives=[],
                  resolver_additional=0, resolver_additional_dictation=0,
                  grammar_additional=0, grammar_additional_dictation=0,
                  resolver_only=[], grammar_only=[], exact_p95_ms=0, probe_p95_ms=0)
    exact_times, probe_times = [], []
    for row in rows:
        text, dictated = row['text'], row['dictation']
        row_context = MatchContext.for_app(row['app']) if row.get('app') else context
        executor._matcher.set_context_provider(lambda c=row_context: c)
        start = time.perf_counter()
        exact = executor.find_exact_command(normalize_utterance(apply_phonetic_wash(text)))
        exact_times.append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        match = probe(text)
        probe_times.append((time.perf_counter() - start) * 1000)
        decision = resolver.resolve(text, context=row_context)
        parsed = grammar.parse(text, records)
        resolved = decision.kind == 'resolved'
        # This is a parser verdict, not an execution proposal. Report all full
        # parses, as shadow.grammar2_verdict does; wire none into the lane.
        grammar_hit = bool(parsed)
        totals['labelled_dictation'] += int(dictated)
        totals['exact'] += int(exact is not None)
        totals['exact_dictation'] += int(exact is not None and dictated)
        totals['lane_execute'] += int(match is not None)
        totals['lane_stage'] += int(match is None)
        if match is not None and dictated:
            totals['lane_false_positives'].append(dict(text=text, phrase=match.phrase, reserved=match.reserved))
        totals['resolver_additional'] += int(resolved and exact is None)
        totals['resolver_additional_dictation'] += int(resolved and exact is None and dictated)
        totals['grammar_additional'] += int(grammar_hit and exact is None)
        totals['grammar_additional_dictation'] += int(grammar_hit and exact is None and dictated)
        if resolved and not grammar_hit:
            totals['resolver_only'].append(dict(text=text, canonical=decision.canonical_id))
        if grammar_hit and not resolved:
            totals['grammar_only'].append(dict(text=text, canonical=parsed[0][0], resolver=decision.kind))
    for key in ('resolver_only', 'grammar_only'):
        totals[key + '_count'] = len(totals[key])
        totals[key] = totals[key][:10]
    for key, values in (('exact_p95_ms', exact_times), ('probe_p95_ms', probe_times)):
        totals[key] = round(sorted(values)[min(len(values) - 1, int(len(values) * .95))], 3) if values else 0
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-shadow', type=Path)
    args = parser.parse_args(argv)
    # Shared 398 context managers use only an empty temp profile and synthetic
    # desktop stubs. No provider discovery, HTTP, model or benchmark run occurs.
    from tools.plain_speech_bench import isolated_home, load_corpus, synthetic_desktop, LiveTiers
    groups = load_rows(args.from_shadow)
    corpus, _ = load_corpus()
    with contextlib.redirect_stdout(sys.stderr), isolated_home(), synthetic_desktop(corpus) as context:
        tiers = LiveTiers(corpus, context)
        result = {name: measure(rows, tiers.executor, tiers.records, tiers.resolver, context=context)
                  for name, rows in groups.items()}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(any(r['lane_false_positives'] for r in result.values()))


if __name__ == '__main__':
    raise SystemExit(main())
