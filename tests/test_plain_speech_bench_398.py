"""Queue 398 focused gate: real catalogue, fake providers, no app bootstrap."""
import json
from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tools import plain_speech_bench as bench


@pytest.fixture(scope='module')
def live():
    corpus, _ = bench.load_corpus()
    with bench.isolated_home(), bench.synthetic_desktop(corpus) as context:
        yield corpus, bench.LiveTiers(corpus, context)


def test_frozen_corpus_shape_and_independent_plain_phrasings(live):
    corpus, tiers = live
    _, digest = bench.load_corpus()
    assert digest == 'a6ac9cde82bb9d144fb1c2c9a63b7d933877841519bc4802200bdf4163445ce4'
    assert corpus['synthetic'] is True
    rows = corpus['utterances']
    assert Counter(r['tag'] for r in rows) == {'plain': 120, 'exact': 40, 'dictation': 40}
    assert len({r['id'] for r in rows}) == 200
    assert len({r['text'] for r in rows}) == 200
    assert sum(r.get('command_like_start', False) for r in rows) == 15
    assert corpus['synthetic_windows'] and corpus['synthetic_foreground']['app'] == 'chrome.exe'
    from samsara.command_catalog import normalize_phrase
    aliases = {normalize_phrase(a) for r in tiers.records for a in r['aliases']}
    commands = Counter()
    for row in rows:
        assert isinstance(row['text'], str) and row['text']
        if row['tag'] == 'dictation':
            assert row['expected'] == 'dictation'
            continue
        expected = row['expected']
        assert expected['canonical_id'] in tiers.by_id
        assert isinstance(expected['args'], dict)
        if row['tag'] == 'plain':
            commands[expected['canonical_id']] += 1
            assert normalize_phrase(row['text']) not in aliases
    assert len(commands) == 40
    assert set(commands.values()) == {3}


def test_every_exact_control_exists_in_live_catalogue_and_matches(live):
    corpus, tiers = live
    # LiveTiers uses dump_command_metadata.build_executor + build_catalog, not
    # checked-in metadata. Scoped browser controls use a fixed synthetic scope.
    from samsara.command_catalog import canonical_phrase
    for row in corpus['utterances']:
        if row['tag'] == 'exact':
            expected = row['expected']
            assert row['text'] == canonical_phrase(tiers.by_id[expected['canonical_id']])
            answer = tiers.exact(row['text'])
            assert (answer.canonical_id, answer.args) == (expected['canonical_id'], expected['args'])


@pytest.mark.parametrize('tier', ['exact', 'grammar', 'resolver', 'model'])
def test_fake_four_tiers_right_wrong_late_never_and_args(tier):
    rows = [{'expected': {'canonical_id': 'a', 'args': {'level': 30}}} for _ in range(4)]
    rows += [{'expected': 'dictation'}, {'expected': 'dictation'}]
    answers = [bench.Answer('a', {'level': 30}, 1),
               bench.Answer('b', {}, 10),
               bench.Answer('a', {'level': 30}, 3001, True),
               bench.Answer(ms=5000, late=True, error='TimeoutError'),
               bench.Answer(ms=2), bench.Answer('a', {}, 3)]
    s = bench.tally(rows, answers)
    assert (s['right'], s['wrong'], s['no_answer'], s['correct_rejections']) == (2, 2, 2, 1)
    assert s['late'] == 2 and s['errors'] == 1
    assert s['median_ms'] == 6.5
    assert s['p95_ms'] == pytest.approx(4500.25)
    assert bench.tally(rows[:1], [bench.Answer('a', {'level': 31})])['wrong'] == 1


def test_pipeline_first_answer_wins_even_when_wrong_and_late():
    first = bench.Answer('wrong', {}, 2600, True)
    later = bench.Answer('right', {}, 20)
    a = bench.pipeline([('exact', first), ('grammar', later), ('model', later)])
    assert a.canonical_id == 'wrong' and a.ms == 2600 and a.late
    assert a.detail['selected_tier'] == 'exact'
    a = bench.pipeline([('exact', bench.Answer(ms=2)), ('resolver', later), ('model', first)])
    assert a.canonical_id == 'right' and a.ms == 22
    assert bench.pipeline([('a', bench.Answer(ms=3)), ('b', bench.Answer(ms=4))]).ms == 7


def test_resolver_suggest_is_no_answer(live):
    fake = SimpleNamespace(kind='suggest', canonical_id='builtin.close_tab', args={}, chain=())
    assert bench.resolver_answer(fake).canonical_id is None
    assert bench.resolver_answer(SimpleNamespace(kind='dictation')).canonical_id is None
    fake.kind = 'resolved'
    assert bench.resolver_answer(fake).canonical_id == 'builtin.close_tab'
    _, tiers = live
    # A real one-word registered command is demoted by b2's execution rules.
    assert tiers.intent('copy').canonical_id is None


def test_synthetic_windows_are_fixed_and_do_not_enumerate(live):
    corpus, _ = live
    from plugins.commands import app_verbs, windows
    assert app_verbs.resolve_window('firefox') == (102, 'Synthetic Firefox', 'firefox.exe')
    assert windows.get_all_movable_windows() == [(r['hwnd'], r['title'], r['pid'])
                                                for r in corpus['synthetic_windows']]
    assert app_verbs.resolve_window('nonexistent') is None


def test_production_model_menu_prompt_grounding_and_late_deadline(live, monkeypatch):
    _, tiers = live
    from plugins.commands import ask_ollama
    from samsara import ava_command_session, ai_preferences
    # No network transport: keep actual resolver/menu/prompt/parser.
    seen = []
    def fake_ask(text, app, model, system):
        seen.append((text, model, system))
        menu = app.command_executor.ava_menu(text, limit=12, app=app,
                                              context=tiers.context_for(text))
        return 'CONFIRM Test action.\nACTION ' + menu[0]
    monkeypatch.setattr(ask_ollama, 'ask_ollama', fake_ask)
    monkeypatch.setattr(ai_preferences, 'runtime_state', lambda *a, **k:
                        SimpleNamespace(allowed=True, provider=SimpleNamespace(identity='ollama')))
    run = tiers.model('fake:1b')
    answer = run('can you turn that down a bit')
    assert len(answer.detail['menu']) <= 12
    assert answer.detail['menu']
    assert answer.detail['deadline_ms'] == 2500 and not answer.detail['deadline_enforced']
    assert answer.detail['model_called']
    assert seen[0][1] == 'fake:1b' and '{COMMAND_LIST}' not in seen[0][2]
    selected = tiers.canonical(answer.detail['menu'][0])
    assert answer.canonical_id == selected
    monkeypatch.setattr(ask_ollama, 'ask_ollama', lambda *a, **k: 'CONFIRM Test action.\nACTION fabricated command')
    assert run('can you turn that down a bit').canonical_id is None
    monkeypatch.setattr(ask_ollama, 'ask_ollama', lambda *a, **k: 'CONFIRM Test action.\nAPP open | made up app')
    assert run('can you turn that down a bit').canonical_id is None
    # Exercise the current production length boundary, without a provider call.
    assert not run('please ' * (ava_command_session.MAX_REQUEST_WORDS + 1)).detail['model_called']
    # Prove that only expiry is disabled, not closed-world acceptance. Artificial
    # clock advances after provider reply; no real sleep or model load.
    monkeypatch.setattr(ask_ollama, 'ask_ollama', fake_ask)
    clock = iter([100., 100., 110.])
    monkeypatch.setattr(ava_command_session.time, 'monotonic', lambda: next(clock, 110.))
    assert run('can you turn that down a bit').canonical_id == selected


def test_from_shadow_fixture_and_cli_do_not_persist_text(tmp_path, monkeypatch):
    folder = tmp_path / 'generated-shadow'
    folder.mkdir()
    (folder / 'intent-2026-10-08.jsonl').write_text(
        json.dumps({'v': 1, 'text': 'fixture one'}) + '\n' +
        json.dumps({'v': 1, 'text': 'fixture two'}) + '\n', encoding='utf-8')
    rows = bench.read_shadow(folder)
    result = bench.compare_shadow(rows, {
        'exact': lambda text: bench.Answer('a'),
        'grammar': lambda text: bench.Answer('b' if text == 'fixture one' else 'a'),
        'resolver': lambda text: bench.Answer(),
    })
    assert result['n'] == 2 and result['disagree'] == 2
    assert result['pairs']['exact vs grammar'] == 1
    many = bench.compare_shadow(rows * 15, {'a': lambda t: bench.Answer('a'),
                                           'b': lambda t: bench.Answer('b')})
    assert len(many['examples']) == 20 and many['disagree'] == 30
    @contextmanager
    def desktop(corpus):
        yield None
    monkeypatch.setattr(bench, 'synthetic_desktop', desktop)
    monkeypatch.setattr(bench, 'LiveTiers', lambda *a: SimpleNamespace(
        exact=lambda t: bench.Answer(), grammar=lambda t: bench.Answer('b'),
        intent=lambda t: bench.Answer()))
    monkeypatch.setattr(bench, 'discover_models', lambda *a: {
        'running': False, 'models': [], 'skipped': [], 'reason': 'fixture'})
    output = tmp_path / 'reports'
    assert bench.main(['--from-shadow', str(folder), '--output-dir', str(output)]) == 0
    assert not output.exists()


def known_report():
    plain = {'n': 120, 'right': 108, 'wrong': 1, 'no_answer': 11,
             'correct_rejections': 0, 'median_ms': 10., 'p95_ms': 20., 'max_ms': 30.,
             'late': 0, 'errors': 0}
    exact = {**plain, 'n': 40, 'right': 40, 'wrong': 0, 'no_answer': 0}
    negative = {**plain, 'n': 40, 'right': 0, 'wrong': 0, 'no_answer': 40,
                'correct_rejections': 40}
    summary = {'plain': plain, 'exact': exact, 'dictation': negative, 'all': plain}
    memory = {'min_free_bytes': 2 * bench.GIB}
    return {'machine': {'name': 'FIXTURE', 'cpu': 'Fixture CPU', 'physical_cores': 4,
                        'total_memory_bytes': 8 * bench.GIB},
            'summary': {name: summary for name in ['exact', 'grammar', 'resolver',
                                                   'pipeline:local', 'alternative:local']},
            'checks': {name: bench.pass_lines(summary, memory) for name in
                       ['pipeline:local', 'alternative:local']},
            'models': {}, 'memory': {'local': memory}, 'corpus_sha256': 'abc',
            'discovery': {'running': False, 'models': [], 'skipped': [],
                          'reason': 'Ollama was not running; the model tier was skipped.'}}


def test_report_first_twelve_lines_for_known_input():
    data = known_report()
    lines = bench.render_report(data).splitlines()[:12]
    assert len(lines) == 12 and all(lines)
    assert lines[0] == 'This machine is FIXTURE, Fixture CPU, 4 cores and 8.0 GiB of memory.'
    assert lines[1] == 'The grammar tier got 108 of 120 plain commands right, with a median of 10.000 ms using synthetic windows.'
    assert lines[2] == 'Grammar versus resolver: 108 versus 108 plain right; 0 versus 0 dictation actions.'
    assert lines[3] == 'Ollama was not running; the model tier was skipped.'
    assert 'Wrong plain actions' in lines[4] and 'Dictation wrongly executed' in lines[5]
    for line in lines[6:11]:
        assert 'PASS' in line and 'model pipelines not measured - Ollama was not running' in line
    assert lines[11] == bench.LAPTOP


def test_fixed_pass_thresholds_fail_without_softening():
    data = known_report()
    summary = data['summary']['pipeline:local']
    summary['plain'] = {**summary['plain'], 'right': 101, 'wrong': 3,
                         'median_ms': 1501, 'p95_ms': 3001}
    summary['dictation'] = {**summary['dictation'], 'wrong': 1}
    checks = bench.pass_lines(summary, {'min_free_bytes': bench.GIB})
    assert all(c['status'] == 'FAIL' for c in checks.values())


def test_discovery_skips_uninstalled_and_never_downloads(monkeypatch):
    monkeypatch.setattr(bench, 'ollama_get', lambda endpoint: {
        'models': [{'name': 'tiny:1b', 'size': bench.GIB}, {'name': 'large:8b', 'size': 4 * bench.GIB}]
    } if endpoint == 'tags' else {'models': []})
    found = bench.discover_models()
    assert found['models'] == ['tiny:1b']
    assert found['skipped'] == ['llama3.2:3b', 'qwen2.5:3b']
    assert bench.discover_models(['large:8b'])['models'] == ['large:8b']
    def down(endpoint):
        raise ConnectionRefusedError()
    monkeypatch.setattr(bench, 'ollama_get', down)
    assert not bench.discover_models()['running']


def test_all_four_tiers_and_both_pipeline_tables(monkeypatch):
    corpus, _ = bench.load_corpus()
    @contextmanager
    def sampler():
        yield {'min_free_bytes': 3 * bench.GIB, 'peak_rss_bytes': 0}
    monkeypatch.setattr(bench, 'MemorySampler', sampler)
    monkeypatch.setattr(bench, 'ollama_get', lambda endpoint: {'models': []})
    tiers = SimpleNamespace(exact=lambda t: bench.Answer(), grammar=lambda t: bench.Answer('wrong'),
                            intent=lambda t: bench.Answer(),
                            model=lambda name: lambda t: bench.Answer('model-wrong'))
    data = bench.run_bench(corpus, tiers, {'models': ['fake:1b']})
    assert set(data['summary']) == {'exact', 'grammar', 'resolver', 'model:fake:1b',
                                  'pipeline:local', 'alternative:local',
                                  'pipeline:fake:1b', 'alternative:fake:1b'}
    assert data['summary']['pipeline:fake:1b']['plain']['wrong'] == 120
    assert data['rows'][0]['answers']['pipeline:fake:1b']['canonical_id'] == 'wrong'
    assert data['rows'][0]['answers']['alternative:fake:1b']['canonical_id'] == 'model-wrong'
