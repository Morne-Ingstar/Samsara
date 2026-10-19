"""706: deterministic file shards and the Windows CI matrix contract."""
from collections import Counter
from pathlib import Path
import os
import shlex
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
INDEX = 'SAMSARA_TEST_SHARD_INDEX'
TOTAL = 'SAMSARA_TEST_SHARD_TOTAL'


@pytest.fixture
def shard_hook(request):
    plugin = request.config.pluginmanager.get_plugin(str(ROOT / 'tests/conftest.py'))
    return plugin.pytest_collection_modifyitems


def partition_collection(hook, root, original):
    """Exercise the actual hook on collected Items, without another pytest process.

    The collection-only evidence plugin also calls this over the entire suite.
    A focused gate checks its real selection; a full collection checks balance.
    """
    config = SimpleNamespace(rootpath=root, hook=Mock())
    partitions = []
    already_sharded = INDEX in os.environ and TOTAL in os.environ
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(TOTAL, '4')
        for index in range(4):
            patch.setenv(INDEX, str(index))
            selected = list(original)
            config.hook.pytest_deselected.reset_mock()
            hook(config, selected)
            removed = [item for call in config.hook.pytest_deselected.call_args_list
                       for item in call.kwargs['items']]
            assert Counter(item.nodeid for item in selected + removed) == Counter(
                item.nodeid for item in original)
            partitions.append(selected)

    nodeids = [set(item.nodeid for item in part) for part in partitions]
    assert set.union(*nodeids) == {item.nodeid for item in original}
    assert sum(map(len, partitions)) == len(original)
    for index, part in enumerate(partitions):
        assert all(not nodeids[index] & other for other in nodeids[index + 1:])
    files = [{item.path for item in part} for part in partitions]
    for index, paths in enumerate(files):
        assert all(not paths & other for other in files[index + 1:])
    counts = list(map(len, partitions))
    if len(original) >= 1000 and not already_sharded:
        # Test counts approximate work; timings still depend on individual files.
        assert min(counts) > 0 and max(counts) / min(counts) <= 1.35, counts
    return {'tests': counts, 'files': list(map(len, files)), 'total': len(original)}


def test_real_collection_shards_cover_every_node_once_without_splitting_files(
        request, shard_hook):
    counts = partition_collection(shard_hook, request.config.rootpath, request.session.items)
    print('706 collected shard counts:', counts)


@pytest.mark.parametrize('values', [{}, {INDEX: '2'}, {TOTAL: '4'}])
def test_unconfigured_sharding_preserves_items_and_audio_policy(
        shard_hook, monkeypatch, tmp_path, values):
    for key in (INDEX, TOTAL, 'SAMSARA_TEST_REAL_AUDIO'):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    item = Mock(path=tmp_path / 'tests/test_audio.py')
    item.get_closest_marker.return_value = True
    items = [item]
    config = SimpleNamespace(rootpath=tmp_path, hook=Mock())
    shard_hook(config, items)
    assert items == [item]
    item.add_marker.assert_called_once()
    assert item.add_marker.call_args.args[0].name == 'skip'
    config.hook.pytest_deselected.assert_not_called()


@pytest.mark.parametrize('index,total', [('-1', '4'), ('4', '4'), ('0', '0'),
                                        ('0', '-2'), ('x', '4'), ('0', '')])
def test_invalid_shards_fail_before_silently_losing_tests(
        shard_hook, monkeypatch, index, total):
    monkeypatch.setenv(INDEX, index)
    monkeypatch.setenv(TOTAL, total)
    with pytest.raises(pytest.UsageError, match='shard'):
        shard_hook(SimpleNamespace(rootpath=ROOT), [])


def test_assignment_ignores_checkout_location_and_parametrization(
        shard_hook, monkeypatch, tmp_path):
    monkeypatch.setenv(INDEX, '0')
    monkeypatch.setenv(TOTAL, '4')
    results = []
    for checkout in (tmp_path / 'one', tmp_path / 'nested/two'):
        monkeypatch.setitem(shard_hook.__globals__, '__file__', str(checkout / 'tests/conftest.py'))
        original = [Mock(path=checkout / f'tests/test_file_{number}.py',
                         nodeid=f'tests/test_file_{number}.py::test_value[{param}]')
                    for number in range(40) for param in ('a', 'b')]
        config = SimpleNamespace(rootpath=checkout, hook=Mock())
        items = list(original)
        shard_hook(config, items)
        assert 0 < len(items) < len(original)
        assert items == [item for item in original if item in items]
        selected_files = {item.path for item in items}
        assert all((item in items) == (item.path in selected_files) for item in original)
        config.hook.pytest_deselected.assert_called_once_with(
            items=[item for item in original if item not in items])
        results.append([item.nodeid for item in items])
    assert results[0] == results[1]


@pytest.mark.parametrize('nested', [False, True])
def test_child_session_ignores_inherited_shards(shard_hook, monkeypatch, tmp_path, nested):
    child = tmp_path / 'child'
    if nested:
        child = ROOT / 'tests' / 'child-probe'
    monkeypatch.setenv(INDEX, 'invalid')
    monkeypatch.setenv(TOTAL, '4')
    item = Mock(path=child / 'test_probe.py')
    items = [item]
    config = SimpleNamespace(rootpath=child, hook=Mock())
    shard_hook(config, items)
    assert items == [item]
    config.hook.pytest_deselected.assert_not_called()


def test_only_repository_tests_are_sharded(shard_hook, monkeypatch, tmp_path):
    monkeypatch.setenv(INDEX, '1')
    monkeypatch.setenv(TOTAL, '4')
    repo_test = Mock(path=ROOT / 'tests/test_example.py')
    probes = [Mock(path=ROOT / 'test_probe.py'),
              Mock(path=ROOT / 'tools/test_probe.py'),
              Mock(path=tmp_path / 'test_probe.py')]
    items = [probes[0], repo_test, *probes[1:]]
    config = SimpleNamespace(rootpath=ROOT, hook=Mock())
    shard_hook(config, items)
    assert items == probes
    config.hook.pytest_deselected.assert_called_once_with(items=[repo_test])


def test_greedy_packing_balances_large_files_and_ignores_collection_order(
        shard_hook, monkeypatch):
    original = [Mock(path=ROOT / f'tests/test_{name}.py', nodeid=f'{name}::{number}')
                for name, count in [('a', 10), ('b', 8), ('c', 6), ('d', 4), ('e', 2)]
                for number in range(count)]
    monkeypatch.setenv(TOTAL, '4')
    expected = [{'a'}, {'b'}, {'c'}, {'d', 'e'}]
    for index, names in enumerate(expected):
        monkeypatch.setenv(INDEX, str(index))
        for order in (original, list(reversed(original))):
            selected = list(order)
            shard_hook(SimpleNamespace(rootpath=ROOT, hook=Mock()), selected)
            assert {item.nodeid.split('::')[0] for item in selected} == names
            assert selected == [item for item in order if item.nodeid.split('::')[0] in names]
    # A shard containing one very large file cannot itself form four balanced
    # subshards. CI checks its partition integrity without demanding that.
    large_file = [Mock(path=ROOT / 'tests/test_large.py', nodeid=f'large::{n}')
                  for n in range(1000)]
    assert partition_collection(shard_hook, ROOT, large_file)['total'] == 1000


def test_single_shard_keeps_every_item_without_deselection(shard_hook, monkeypatch):
    monkeypatch.setenv(INDEX, '0')
    monkeypatch.setenv(TOTAL, '1')
    items = [Mock(path=ROOT / 'tests/test_example.py')]
    before = list(items)
    config = SimpleNamespace(rootpath=ROOT, hook=Mock())
    shard_hook(config, items)
    assert items == before
    config.hook.pytest_deselected.assert_not_called()


@pytest.fixture
def test_job():
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    return workflow['jobs']['test']


def test_ci_matrix_shards_only_main_suite_and_preserves_recovery(test_job):
    assert test_job['strategy'] == {'fail-fast': False, 'matrix': {'shard': [0, 1, 2, 3]}}
    assert test_job['runs-on'] == 'windows-latest'
    assert test_job['timeout-minutes'] == 120
    assert INDEX not in test_job.get('env', {}) and TOTAL not in test_job.get('env', {})
    main = next(step for step in test_job['steps'] if step.get('name') == 'Run tests with coverage')
    assert main['timeout-minutes'] == 100
    assert main['env'][INDEX] == '${{ matrix.shard }}' and str(main['env'][TOTAL]) == '4'
    args = shlex.split(main['run'])
    assert args[args.index('-n') + 1] == '2'
    assert args[args.index('--dist') + 1] == 'loadfile'
    assert {'--max-worker-restart=100', '--timeout=120', '--timeout-method=thread'} <= set(args)
    assert args[args.index('-m', 3) + 1] == 'not requires_desktop'
    assert 'SAMSARA_TEST_HARD_TIMEOUT' not in main['env']


def test_ci_non_test_checks_and_unsharded_desktop_run_once(test_job):
    once = {'Verify embedded version identity', 'Set up Node.js for browser-extension tests',
            'Test Show Numbers browser extension', 'Check Qt discipline',
            'Check thread discipline', 'Report plugin imports'}
    for name in once:
        steps = [step for step in test_job['steps'] if step.get('name') == name]
        assert len(steps) == 1 and steps[0]['if'] == 'matrix.shard == 0'
    desktop = next(step for step in test_job['steps'] if step.get('name') == 'Run native desktop tests')
    assert desktop['if'] == 'always() && matrix.shard == 0'
    assert desktop['continue-on-error'] is True and desktop['timeout-minutes'] == 15
    assert INDEX not in desktop.get('env', {}) and TOTAL not in desktop.get('env', {})
    assert 'Remove-Item Env:\\QT_QPA_PLATFORM' in desktop['run']


def test_ci_reports_have_separate_shard_names(test_job):
    upload = next(step for step in test_job['steps'] if step.get('name') == 'Upload test reports')
    assert upload['if'] == 'always()'
    assert upload['with']['name'] == 'test-reports-${{ matrix.shard }}'
    assert set(upload['with']['path'].splitlines()) == {'junit.xml', 'coverage.xml'}
    coverage = next(step for step in test_job['steps'] if step.get('name') == 'Upload coverage report')
    assert coverage['if'] == 'always()'
    assert coverage['with']['name'] == 'test-coverage-${{ matrix.shard }}'
    assert coverage['with']['flags'] == 'shard-${{ matrix.shard }}'
