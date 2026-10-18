"""Queue 398: frozen synthetic plain speech, resolution only, never execution.

Run with the development interpreter, or double-click plain_speech_bench.cmd.
No model downloads, app bootstrap, live profile or desktop enumeration.
--from-shadow reads ONLY a caller-selected folder and does not persist its text.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(__file__).with_name('plain_speech_corpus.json')
DEADLINE_MS = 2500.0
GIB = 1024 ** 3
GB = 10 ** 9
LAPTOP = ('The packaged laptop build cannot run this script; a later build needs '
          'a benchmark entry point and these assets, outside queue 398 ownership.')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Answer:
    canonical_id: str | None = None
    args: dict = field(default_factory=dict)
    ms: float = 0.0
    late: bool = False
    error: str | None = None
    detail: dict = field(default_factory=dict)


def load_corpus(path=CORPUS):
    raw = Path(path).read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


@contextlib.contextmanager
def isolated_home():
    """Set before importing ANY application modules; never copy owner data."""
    # The standalone shared logger otherwise leaves a rotating file handle in
    # the disposable home. Install a task-local null handler BEFORE imports.
    root_logger = logging.getLogger()
    handler = logging.NullHandler()
    root_logger.addHandler(handler)
    with tempfile.TemporaryDirectory(prefix='samsara-plain-bench-') as directory:
        try:
            with patch.dict(os.environ, {'SAMSARA_HOME_DIR': directory,
                                         'PYTHONDONTWRITEBYTECODE': '1'}):
                yield Path(directory)
        finally:
            root_logger.removeHandler(handler)
            handler.close()


@contextlib.contextmanager
def synthetic_desktop(corpus):
    from plugins.commands import app_verbs, windows
    from samsara import command_scope
    rows = corpus['synthetic_windows']
    context = command_scope.MatchContext.for_app(corpus['synthetic_foreground']['app'])

    def resolve(name):
        query = name.lower().strip()
        for row in rows:
            if query and (query in row['title'].lower() or
                          query == row['process'].rsplit('.', 1)[0].lower()):
                return row['hwnd'], row['title'], row['process']
        return None

    with patch.object(app_verbs, 'resolve_window', resolve), \
            patch.object(windows, 'get_all_movable_windows',
                         lambda: [(r['hwnd'], r['title'], r['pid']) for r in rows]), \
            patch.object(command_scope, 'capture_context', lambda: context):
        yield context


class LiveTiers:
    """Use one live executor/catalogue and fixed synthetic scope for all tiers."""
    def __init__(self, corpus, context):
        from tools.dump_command_metadata import build_executor
        from samsara import command_catalog as cc
        from samsara.intent.resolve import IntentResolver
        self.executor = build_executor()  # no app => no plugin services
        self.context = context
        self.executor._matcher.set_context_provider(lambda: context)
        self.records = [s.to_dict() for s in cc.build_catalog(self.executor)]
        self.by_id = {r['canonical_id']: r for r in self.records}
        self.phrase_ids = {cc.canonical_phrase(r): r['canonical_id'] for r in self.records}
        # Enable precisely the packs needed by the frozen forty intents. Leaving
        # a requested command disabled would test settings, not recognition.
        from samsara.command_packs import get_enabled_packs
        self.pack_config = {self.by_id[row['expected']['canonical_id']]['pack']: True
                            for row in corpus['utterances'] if row['expected'] != 'dictation'}
        self.executor._matcher.set_enabled_packs(get_enabled_packs({'command_packs': self.pack_config}))
        # The live catalogue binds select-all to Word. Give those four rows a
        # fixed synthetic Word scope, not the owner's foreground. This is scene
        # setup from frozen labels, independent of any tier's returned answer.
        from samsara.command_scope import MatchContext
        self.row_contexts = {
            row['text']: MatchContext.for_app('winword.exe')
            for row in corpus['utterances']
            if row['expected'] != 'dictation' and
            row['expected']['canonical_id'] == 'builtin.select_all'}
        self.row_contexts.update({
            row['text']: MatchContext.for_app(context.exe, tags=['show_numbers.visible'])
            for row in corpus['utterances'] if row['expected'] != 'dictation'
            and row['expected']['canonical_id'] in
            {'show_numbers.click', 'show_numbers.right_click', 'show_numbers.hide_numbers'}})
        self.resolver = IntentResolver(self.records, app_names=[
            r['process'].rsplit('.', 1)[0] for r in corpus['synthetic_windows']])

    def canonical(self, phrase):
        return self.phrase_ids.get(phrase, 'unmapped:' + phrase)

    def context_for(self, text):
        return self.row_contexts.get(text, self.context)

    def argument_view(self, cid, args):
        """Map a handler remainder into the declared slot, without resolving again.

        Structured args remain untouched. No-slot handlers ignore remainders in
        production; their answer is the action alone. Failed slot decoding is
        preserved as raw remainder, so it cannot be scored as a correct value.
        """
        if 'remainder' not in args:
            return args
        record = self.by_id.get(cid)
        if record is None:
            return args
        if not record.get('args'):
            return {}
        remainder = args['remainder']
        if not remainder:
            return {}
        if cid in {'show_numbers.click', 'show_numbers.right_click'}:
            # The actual handler finds spoken numbers inside a remainder such
            # as "on number seven"; grammar slot parsing is stricter. Reuse
            # this pure parser so the registry tier's arguments reflect what
            # the shipped handler would receive, without calling that handler.
            from plugins.commands.show_numbers import _parse_spoken_number
            label = _parse_spoken_number(remainder)
            return {'label': label} if label is not None else args
        from samsara.grammar import _slots, _tokens
        parsed = _slots(record, _tokens(remainder))
        return parsed if parsed is not None else args

    def exact(self, text):
        hit = self.executor._matcher.match_detail(text, context=self.context_for(text))
        if hit is None:
            return Answer()
        cid = self.canonical(hit.entry.phrase)
        return Answer(cid, self.argument_view(cid, {'remainder': hit.remainder}))

    def grammar(self, text):
        from samsara.grammar import parse
        hits = parse(text, self.records)
        if not hits:
            return Answer()
        if len(hits) != 1:
            return Answer('__multiple_actions__', {'chain': [h[0] for h in hits]})
        return Answer(hits[0][0], hits[0][1])

    def intent(self, text):
        return resolver_answer(self.resolver.resolve(text, self.context_for(text)))

    def model(self, model):
        """Production resolver/menu/prompt/parser; record but don't discard late.

        Readiness is supplied from read-only Ollama probes. Deadline rejection
        alone is disabled, as the brief requests. Eight-word limit, ACTION2
        recognition, closed-menu validation and grounding remain production code.
        """
        from samsara import ai_preferences, ava_command_session, ava_readiness
        from plugins.commands import ask_ollama
        config = {'ollama': {'enabled': True, 'model': model,
                             'host': 'http://127.0.0.1:11434'},
                  'ava': {'provider_policy': 'local'},
                  'ava_command_session': {'model': model, 'interpret_misses': True,
                                          'shortlist_size': 12, 'keep_warm': False},
                  'ava_personality': 'relaxed', 'command_packs': {**self.pack_config, 'ai': True}}
        provider = ai_preferences.provider_for(config, 'local')
        config['ava']['consent'] = {
            'version': ai_preferences.CONSENT_VERSION,
            'accepted_at': 'synthetic-benchmark', 'features': ['ava'],
            'provider': provider.record(), 'cloud_version': None}
        app = SimpleNamespace(config=config, command_executor=self.executor,
                              audio_coordinator=None, play_sound=lambda *a, **k: None)

        class MeasuredResolver(ava_command_session.CommandMissResolver):
            def _cancelled(self, request_id, deadline):
                return ava_command_session.is_cancelled(request_id)

        resolver = MeasuredResolver(app)
        counter = 0

        def run(text):
            nonlocal counter
            counter += 1
            # The shared lane also checks deadlines now. This offline bench
            # measures late answers rather than discarding them at either gate.
            deadline = math.inf
            menus, prompts = [], []
            menu_source = app.command_executor.ava_menu
            ask_source = ask_ollama.ask_ollama

            def capture_menu(*args, **kwargs):
                kwargs['context'] = self.context_for(text)
                menu = menu_source(*args, **kwargs)
                menus.append(menu)
                return menu

            def capture_ask(*args, **kwargs):
                # Hash only: never persist model prompt/profile text.
                prompts.append(hashlib.sha256(kwargs.get('system', '').encode()).hexdigest())
                return ask_source(*args, **kwargs)

            with patch.object(ava_readiness, 'readiness_for',
                              lambda unused: SimpleNamespace(ready=True)), \
                    patch.object(app.command_executor, 'ava_menu', capture_menu), \
                    patch.object(ask_ollama, 'ask_ollama', capture_ask):
                proposal = resolver.resolve(text, f'bench-{model}-{counter}', deadline)
            detail = {'menu': menus[0] if menus else [], 'menu_limit': 12,
                      'model_called': bool(prompts), 'prompt_sha256': prompts,
                      'deadline_ms': DEADLINE_MS, 'deadline_enforced': False}
            if proposal is None:
                return Answer(detail=detail)
            cid = self.canonical(proposal.command_id)
            return Answer(cid, self.argument_view(cid, proposal.args), detail=detail)
        return run


def resolver_answer(resolution):
    if resolution.kind != 'resolved':
        return Answer(detail={'kind': resolution.kind})
    if len(resolution.chain) > 1:
        return Answer('__multiple_actions__', {'chain': [p.canonical_id for p in resolution.chain]})
    return Answer(resolution.canonical_id, dict(resolution.args))


def measure(text, tier):
    started = time.perf_counter()
    try:
        result = tier(text)
    except Exception as exc:
        # Resolution failure is a no-answer, visible and counted; not a crash
        # that silently deletes a difficult row from the denominator.
        result = Answer(error=type(exc).__name__)
    result.ms = (time.perf_counter() - started) * 1000
    result.late = result.ms > DEADLINE_MS
    return result


def pipeline(answers):
    elapsed = 0.0
    for tier, answer in answers:
        elapsed += answer.ms
        if answer.canonical_id is not None:
            return Answer(answer.canonical_id, dict(answer.args), elapsed,
                          elapsed > DEADLINE_MS, answer.error, {'selected_tier': tier})
    return Answer(ms=elapsed, late=elapsed > DEADLINE_MS)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def tally(rows, answers):
    right = wrong = missing = rejected = 0
    for row, answer in zip(rows, answers, strict=True):
        expected = row['expected']
        if answer.canonical_id is None:
            missing += 1
            rejected += expected == 'dictation'
        elif expected != 'dictation' and answer.canonical_id == expected['canonical_id'] \
                and answer.args == expected['args']:
            right += 1
        else:
            wrong += 1
    times = [a.ms for a in answers]
    return {'n': len(rows), 'right': right, 'wrong': wrong, 'no_answer': missing,
            'correct_rejections': rejected,
            'median_ms': statistics.median(times) if times else None,
            'p95_ms': percentile(times, .95), 'max_ms': max(times) if times else None,
            'late': sum(a.late for a in answers),
            'errors': sum(a.error is not None for a in answers)}


def summaries(rows, answers):
    result = {'all': tally(rows, answers)}
    for tag in ('plain', 'exact', 'dictation'):
        indices = [i for i, row in enumerate(rows) if row['tag'] == tag]
        result[tag] = tally([rows[i] for i in indices], [answers[i] for i in indices])
    return result


def pass_lines(summary, memory):
    plain, negative = summary['plain'], summary['dictation']
    checks = {
        'plain_accuracy': (100 * plain['right'] / plain['n'], 85, 'minimum'),
        'wrong_rate': (100 * plain['wrong'] / plain['n'], 2, 'maximum'),
        'dictation_actions': (negative['wrong'], 0, 'maximum'),
        'median_ms': (plain['median_ms'], 1500, 'maximum'),
        'p95_ms': (plain['p95_ms'], 3000, 'maximum'),
        'free_memory_gb': (memory.get('min_free_bytes', 0) / GB, 1.5, 'minimum'),
    }
    return {key: {'value': value, 'limit': limit, 'status': 'PASS' if
                  ((value >= limit) if bound == 'minimum' else (value <= limit)) else 'FAIL'}
            for key, (value, limit, bound) in checks.items()}


class MemorySampler:
    """Sample aggregate Ollama RSS and free system memory; no other process data."""
    def __init__(self, interval=.05):
        self.interval = interval
        self.stop_event = threading.Event()
        self.data = {'peak_rss_bytes': 0, 'samples': 0, 'sample_errors': 0,
                     'interval_ms': interval * 1000}

    def sample(self):
        import psutil  # declared in requirements.txt
        free = psutil.virtual_memory().available
        rss = 0
        for process in psutil.process_iter(['name']):
            name = (process.info['name'] or '').lower()
            if name.startswith('ollama'):
                try:
                    rss += process.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self.data['sample_errors'] += 1
        self.data['samples'] += 1
        self.data.setdefault('free_before_bytes', free)
        self.data['min_free_bytes'] = min(self.data.get('min_free_bytes', free), free)
        if rss >= self.data['peak_rss_bytes']:
            self.data['peak_rss_bytes'] = rss
            self.data['free_at_peak_bytes'] = free

    def _run(self):
        while not self.stop_event.wait(self.interval):
            self.sample()

    def __enter__(self):
        self.sample()
        self.thread = threading.Thread(target=self._run, name='bench-memory', daemon=True)
        self.thread.start()
        return self.data

    def __exit__(self, *unused):
        self.stop_event.set()
        self.thread.join()
        self.sample()


def ollama_get(endpoint):
    with urllib.request.urlopen('http://127.0.0.1:11434/api/' + endpoint, timeout=2) as response:
        return json.load(response)


def discover_models(requested=None):
    try:
        installed = ollama_get('tags').get('models', [])
        loaded = ollama_get('ps').get('models', [])
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return {'running': False, 'models': [],
                'skipped': list(requested or ['llama3.2:3b', 'qwen2.5:3b']),
                'reason': 'Ollama was not running; the model tier was skipped.',
                'probe_error': type(exc).__name__}
    names = {item['name'] for item in installed}
    chosen = list(requested) if requested is not None else ['llama3.2:3b', 'qwen2.5:3b'] + [
        item['name'] for item in installed if item.get('size', math.inf) <= GIB]
    chosen = list(dict.fromkeys(chosen))
    return {'running': True, 'models': [n for n in chosen if n in names],
            'skipped': [n for n in chosen if n not in names],
            'loaded_before': [item['name'] for item in loaded],
            'gpu_before': {item['name']: item.get('size_vram', 0) for item in loaded}}


def machine_info():
    import psutil
    cpu = platform.processor() or 'unknown'
    if sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                cpu = winreg.QueryValueEx(key, 'ProcessorNameString')[0].strip()
        except OSError:
            pass
    return {'name': platform.node(), 'cpu': cpu, 'physical_cores': psutil.cpu_count(logical=False),
            'logical_cores': psutil.cpu_count(), 'total_memory_bytes': psutil.virtual_memory().total,
            'os': platform.platform()}


def run_bench(corpus, tiers, discovery):
    rows = corpus['utterances']
    base = {'exact': tiers.exact, 'grammar': tiers.grammar, 'resolver': tiers.intent}
    results, stats, memories, models = {}, {}, {}, {}
    with MemorySampler() as local_memory:
        for name, tier in base.items():
            results[name] = [measure(row['text'], tier) for row in rows]
            stats[name] = summaries(rows, results[name])
    memories['local'] = local_memory
    for model in discovery['models']:
        name = 'model:' + model
        tier = tiers.model(model)
        # No unloading or warming someone else's running model to force cold.
        loaded = ollama_get('ps').get('models', [])
        cold = model not in {item['name'] for item in loaded}
        print(f'Measuring {model}: {len(rows)} rows.', flush=True)
        with MemorySampler() as memory:
            answers = []
            for i, row in enumerate(rows):
                answers.append(measure(row['text'], tier))
                if (i + 1) % 20 == 0:
                    print(f'{model}: {i + 1}/{len(rows)} rows.', flush=True)
        results[name] = answers
        stats[name] = summaries(rows, answers)
        memories[name] = memory
        first_call = next((a for a in answers if a.detail.get('model_called')), None)
        try:
            gpu_samples = ollama_get('ps').get('models', [])
            gpu = next((item.get('size_vram', 0) > 0 for item in gpu_samples
                        if item['name'] == model), None)
        except (OSError, ValueError):
            gpu = None
        models[model] = {'cold_before': cold, 'first_call_ms': first_call.ms if first_call else None,
                         'cold_first_call_ms': first_call.ms if cold and first_call else None,
                         'gpu_used': gpu, 'model_calls': sum(a.detail.get('model_called', False) for a in answers),
                         'late_answers': sum(a.late and a.canonical_id is not None for a in answers),
                         'late_calls': sum(a.late for a in answers),
                         'cold_note': 'not forced; existing loaded models are left alone'}
    checks = {}
    for model in [None, *discovery['models']]:
        suffix = ':local' if model is None else ':' + model
        for middle, label in [('grammar', 'pipeline'), ('resolver', 'alternative')]:
            name = label + suffix
            chain = ['exact', middle] + ([] if model is None else ['model:' + model])
            results[name] = [pipeline([(tier, results[tier][i]) for tier in chain])
                             for i in range(len(rows))]
            stats[name] = summaries(rows, results[name])
            memory = local_memory if model is None else memories['model:' + model]
            # Include the local phase minimum, even if the model phase was freer.
            memory = {**memory, 'min_free_bytes': min(memory['min_free_bytes'], local_memory['min_free_bytes'])}
            checks[name] = pass_lines(stats[name], memory)
    return {'summary': stats, 'checks': checks, 'memory': memories, 'models': models,
            'rows': [{'id': row['id'], 'tag': row['tag'], 'expected': row['expected'],
                      'answers': {name: asdict(answers[i]) for name, answers in results.items()}}
                     for i, row in enumerate(rows)]}


def read_shadow(folder):
    """Explicit input only; refuse implicit owner-home paths and malformed rows."""
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError('The selected shadow folder does not exist.')
    rows = []
    for path in sorted(folder.glob('intent-*.jsonl')):
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            item = json.loads(line)
            if not isinstance(item.get('text'), str):
                raise ValueError(f'Missing text in {path.name} line {number}')
            rows.append({'id': f'{path.name}:{number}', 'text': item['text']})
    return rows


def compare_shadow(rows, tiers):
    counts = {}
    disagreements = []
    for row in rows:
        answers = {name: measure(row['text'], tier) for name, tier in tiers.items()}
        views = {name: json.dumps([a.canonical_id, a.args], sort_keys=True)
                 for name, a in answers.items()}
        names = list(views)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                key = a + ' vs ' + b
                counts[key] = counts.get(key, 0) + (views[a] != views[b])
        if len(set(views.values())) > 1:
            disagreements.append({'id': row['id'], 'text': row['text'], 'answers': views})
    return {'n': len(rows), 'disagree': len(disagreements), 'pairs': counts,
            'examples': disagreements[:20]}


def owner_lines(data):
    machine, summary = data['machine'], data['summary']
    g, r = summary['grammar'], summary['resolver']
    p = summary['pipeline:local']
    model_notes = []
    for model, detail in data['models'].items():
        s = summary['pipeline:' + model]['plain']
        memory = data['memory']['model:' + model]
        model_notes.append(f'With {model}, {s["right"]} of 120 plain commands were right, '
                           f'median {s["median_ms"]:.1f} ms, worst {s["max_ms"]:.1f} ms, '
                           f'Ollama peak memory {memory["peak_rss_bytes"] / GIB:.2f} GiB')
    model_line = '; '.join(model_notes) + '.' if model_notes else data['discovery'].get(
        'reason', 'No requested model was installed; model measurements were skipped.')
    wrong = ', '.join(f'{name} {s["plain"]["wrong"]}' for name, s in summary.items()
                      if name.startswith(('pipeline:', 'alternative:')))
    negatives = ', '.join(f'{name} {s["dictation"]["wrong"]} of 40' for name, s in summary.items()
                          if name.startswith(('pipeline:', 'alternative:')))

    def check(key, words):
        outcomes = ', '.join(f'{name} {checks[key]["status"]} ({checks[key]["value"]:.2f})'
                             for name, checks in data['checks'].items())
        if not data['models']:
            outcomes += '; model pipelines not measured - ' + (
                'Ollama was not running' if not data['discovery']['running'] else 'models not installed')
        return words + ': ' + outcomes + '.'

    return [
        f'This machine is {machine["name"]}, {machine["cpu"]}, {machine["physical_cores"]} cores '
        f'and {machine["total_memory_bytes"] / GIB:.1f} GiB of memory.',
        f'The grammar tier got {g["plain"]["right"]} of 120 plain commands right, '
        f'with a median of {g["plain"]["median_ms"]:.3f} ms using synthetic windows.',
        f'Grammar versus resolver: {g["plain"]["right"]} versus {r["plain"]["right"]} plain right; '
        f'{g["dictation"]["wrong"]} versus {r["dictation"]["wrong"]} dictation actions.',
        model_line,
        'Wrong plain actions: ' + wrong + '.',
        'Dictation wrongly executed: ' + negatives + '.',
        check('plain_accuracy', 'At least 85 percent plain right'),
        check('wrong_rate', 'At most 2 percent wrong plain actions'),
        check('dictation_actions', 'Zero dictation actions'),
        check('median_ms', 'Median at most 1500 ms') + ' ' + check('p95_ms', '95th percentile at most 3000 ms'),
        check('free_memory_gb', 'Free memory always at least 1.5 GB'),
        LAPTOP,
    ]


def render_report(data):
    lines = owner_lines(data) + ['', '## Measurement', '',
        f'Corpus SHA-256: `{data["corpus_sha256"]}`. Synthetic, frozen before measurement.',
        'Fixed corpus window stubs; synthetic Chrome scope, with Word scope for select-all rows.',
        'Number-click and hide-numbers rows have a synthetic visible-number-overlay scope tag.',
        'Synthetic profile enables packs required by the forty frozen intents; other packs retain defaults.',
        'Resolution proposals only. No handlers, policy execution, audio or input effects.',
        'Wrong means a different command or different arguments; no answer is separate.',
        'Latency is wall time for every row, including misses and skipped over-eight-word model rows.',
        'Pass latency uses the 120 plain rows. Percentiles use linear interpolation.',
        'Pipelines use the first answer and the sum of preceding tier timings from independent replays.',
        'Model replay uses the shipped 12-item menu/prompt/parser, including its ACTION2 shortcut.',
        'The 2500 ms deadline is recorded but not enforced; late proposals count and are listed.',
        'Memory is aggregate Ollama RSS and system available memory, sampled every 50 ms; transient peaks may be missed.',
        'No GPU used for local tiers. Model GPU use is reported from Ollama residency, or unknown.', '',
        '| Tier | Tag | Right command | Wrong command | No answer | Correct rejection | Median ms | P95 ms | Late | Errors |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for name, tags in data['summary'].items():
        for tag in ('plain', 'exact', 'dictation', 'all'):
            s = tags[tag]
            lines.append(f'| {name} | {tag} | {s["right"]} | {s["wrong"]} | {s["no_answer"]} | '
                         f'{s["correct_rejections"]} | {s["median_ms"]:.3f} | {s["p95_ms"]:.3f} | '
                         f'{s["late"]} | {s["errors"]} |')
    lines += ['', '## Model and memory evidence', '', '```json',
              json.dumps({key: data[key] for key in ('discovery', 'models', 'memory')}, indent=2),
              '```', '', '## Every pass line', '',
              '| Pipeline | Accuracy >=85% | Wrong <=2% | Dictation =0 | Median <=1500 ms | P95 <=3000 ms | Free >=1.5 GB |',
              '|---|---|---|---|---|---|---|']
    for name, checks in data['checks'].items():
        lines.append('| ' + name + ' | ' + ' | '.join(
            f'{c["status"]} ({c["value"]:.3f})' for c in checks.values()) + ' |')
    lines += ['', 'Missing installed models: ' + ', '.join(data['discovery']['skipped']) + '.',
              'Model pipelines are unmeasured when Ollama or requested models are unavailable.',
              'The laptop and owner shadow replay have not been run by this worker.',
              'An exact-registry control below 40/40 invalidates the harness; see JSON row evidence.']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--models', nargs='+', help='Installed Ollama model names; never downloaded')
    parser.add_argument('--output-dir', type=Path,
                        default=Path.home() / 'Documents' / 'Samsara-bench')
    parser.add_argument('--from-shadow', type=Path)
    args = parser.parse_args(argv)
    corpus, digest = load_corpus()
    discovery = discover_models(args.models)
    print(discovery.get('reason', 'Using installed models: ' + ', '.join(discovery['models'])), flush=True)
    with isolated_home(), synthetic_desktop(corpus) as context:
        tiers = LiveTiers(corpus, context)
        if args.from_shadow:
            active = {'exact': tiers.exact, 'grammar': tiers.grammar, 'resolver': tiers.intent}
            active.update({'model:' + m: tiers.model(m) for m in discovery['models']})
            comparison = compare_shadow(read_shadow(args.from_shadow), active)
            # Explicit shadow output goes to terminal only, never the report files.
            print(json.dumps(comparison, indent=2, ensure_ascii=True))
            return 0
        data = run_bench(corpus, tiers, discovery)
    data.update(machine=machine_info(), corpus_sha256=digest, discovery=discovery,
                synthetic=True, measured_at=datetime.now().astimezone().isoformat())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = 'plain_speech_' + platform.node() + '_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    (args.output_dir / (stem + '.json')).write_text(json.dumps(data, indent=2), encoding='utf-8')
    (args.output_dir / (stem + '.md')).write_text(render_report(data), encoding='utf-8')
    print('\n'.join(owner_lines(data)))
    print('Reports: ' + str(args.output_dir))
    if data['summary']['exact']['exact']['right'] != 40:
        print('HARNESS INVALID: exact-registry controls were below 40/40.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
