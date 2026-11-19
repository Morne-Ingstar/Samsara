"""1241: hermetic harness safety/receipt tests; no app, GUI, COM or subprocesses."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tools.build_receipts import common, installed_probe, runner


@pytest.fixture
def identity():
    return dict(build_ts='2026-11-16T20:12:25-08:00', commit='a' * 40,
                timezone_source='build timestamp')


@pytest.fixture
def copied_app(tmp_path):
    root = tmp_path / 'scratch'
    app = root / 'app'
    app.mkdir(parents=True)
    (app / 'Samsara.exe').write_bytes(b'disposable executable fixture')
    return root, app, root / 'home'


def test_isolation_accepts_only_explicit_scratch_siblings(copied_app):
    root, app, home = copied_app
    assert common.validate_paths(app, home, root=root) == (app, home)


@pytest.mark.parametrize('replacement', ['profile', 'app/home', 'home/nested', '..'])
def test_isolation_rejects_alternate_profile(copied_app, replacement):
    root, app, _ = copied_app
    with pytest.raises(ValueError):
        common.validate_paths(app, root / replacement, root=root)


def test_isolation_preserves_nonempty_profile(copied_app):
    root, app, home = copied_app
    home.mkdir()
    existing = home / 'config.json'
    existing.write_text('{"keep": true}')
    with pytest.raises(ValueError, match='empty'):
        common.validate_paths(app, home, root=root)
    assert existing.read_text() == '{"keep": true}'


def test_missing_executable_is_a_precondition_failure(copied_app):
    root, app, home = copied_app
    (app / 'Samsara.exe').unlink()
    with pytest.raises(ValueError, match='Copy'):
        common.validate_paths(app, home, root=root)


def test_reparse_point_is_refused_without_following_it(tmp_path, monkeypatch):
    # 1276 reads link metadata without following the target.
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        return SimpleNamespace(st_file_attributes=0x400, st_mode=value.st_mode) if path == tmp_path else value

    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='reparse'):
        common.reject_links(tmp_path)


@pytest.mark.parametrize('metadata', [
    'commit=' + 'a' * 40 + '\nbuild_ts=2026-11-16T20:12:25-08:00\n',
    json.dumps(dict(commit='a' * 40, built_at='2026-11-16T20:12:25-08:00')),
])
def test_build_receipt_uses_metadata_not_file_times(copied_app, metadata):
    _, app, _ = copied_app
    (app / 'BUILD_INFO.txt').write_text(metadata, encoding='utf-8-sig')
    result = common.build_identity(app)
    assert result['build_ts'] == '2026-11-16T20:12:25-08:00'
    assert result['commit'] == 'a' * 40
    assert result['timezone_source'] == 'build timestamp'
    assert len(result['executable_sha256']) == len(result['metadata_sha256']) == 64


def test_real_daily_metadata_format_retains_timezone_inference(copied_app):
    _, app, _ = copied_app
    (app / 'BUILD_INFO.txt').write_text(
        'commit ' + 'a' * 40 + '\nsubject sample\ndate 2026-11-16 20:05:01 -0800\n'
        'built 2026-11-16 20:12:25 from a clean worktree by builder\n')
    result = common.build_identity(app)
    assert result['build_ts'] == '2026-11-16T20:12:25-08:00'
    assert result['timezone_source'].startswith('inferred')


@pytest.mark.parametrize('metadata', [
    'date 2026-11-16T20:05:01-08:00\n',  # commit date is not the build date
    'built 2026-11-16 20:12:25\n',
    'built invalid\n',
    'date 2026-11-15T20:05:01-08:00\nbuilt 2026-11-16 20:12:25\n',
])
def test_missing_or_ambiguous_build_time_cannot_create_a_receipt(copied_app, metadata):
    _, app, _ = copied_app
    (app / 'BUILD_INFO.txt').write_text(metadata)
    with pytest.raises(ValueError):
        common.build_identity(app)


@pytest.mark.parametrize('observation', [False, None, 1, 'PASS', [], {}])
def test_truthy_values_do_not_fabricate_pass(identity, tmp_path, observation):
    row = common.receipt('T68', identity, 'game mode on', tmp_path / 'proof.json',
                         {'actual_state': observation}, eligible=True)
    assert row['result'] == 'FAIL'
    assert row['acceptance_eligible'] is False


def test_component_pass_is_never_automatically_full_acceptance(identity, tmp_path):
    row = common.receipt('T87', identity, 'owned-session duck', tmp_path / 'proof.json',
                         {'restored': True}, limitation='Engine only')
    assert row['result'] == 'PASS'
    assert row['acceptance_eligible'] is False
    assert row['limitation'] == 'Engine only'


def test_old_build_and_unconfirmed_timezone_do_not_authorize_acceptance(identity, tmp_path):
    old = dict(identity, build_ts='2026-10-01T00:00:00+00:00')
    row = common.receipt('T29', old, 'Apply', tmp_path / 'proof.json', {'saved': True}, eligible=True)
    assert row['result'] == 'FAIL'
    inferred = dict(identity, timezone_source='inferred')
    row = common.receipt('T29', inferred, 'Apply', tmp_path / 'proof.json', {'saved': True}, eligible=True)
    assert row['result'] == 'PASS'
    assert row['acceptance_eligible'] is False


@pytest.mark.parametrize('changes', [dict(ticket='T99'), dict(trigger=''), dict(observations={})])
def test_incomplete_receipts_are_refused(identity, tmp_path, changes):
    args = dict(ticket='T29', identity=identity, trigger='Apply & Close',
                evidence=tmp_path / 'proof.json', observations={'saved': True})
    args.update(changes)
    with pytest.raises(ValueError):
        common.receipt(**args)


def test_atomic_json_is_readable_and_leaves_no_pending_file(tmp_path):
    path = tmp_path / 'evidence' / 'receipt.json'
    common.write_json(path, {'result': 'FAIL'})
    common.write_json(path, {'result': 'PASS'})
    assert json.loads(path.read_text()) == {'result': 'PASS'}
    assert not path.with_suffix('.json.tmp').exists()


def test_json_rejects_nonfinite_measurements(tmp_path):
    with pytest.raises(ValueError):
        common.write_json(tmp_path / 'receipt.json', {'volume': float('nan')})


def test_wait_for_is_bounded_without_real_sleep(monkeypatch):
    ticks = iter([0, .5, 1.1])
    monkeypatch.setattr(common.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(common.time, 'sleep', lambda _: None)
    with pytest.raises(TimeoutError):
        common.wait_for(lambda: False, timeout=1)


def test_channel_ignores_response_from_previous_request(tmp_path, monkeypatch):
    channel = runner.Channel(tmp_path, SimpleNamespace(poll=lambda: None))
    counter = iter([{'id': 'old', 'ok': True, 'value': 'stale'},
                    {'id': 'new', 'ok': True, 'value': {'ready': True}}])
    monkeypatch.setattr(channel, 'read', lambda _: next(counter))
    monkeypatch.setattr(runner.uuid, 'uuid4', lambda: SimpleNamespace(hex='new'))
    monkeypatch.setattr(common.time, 'sleep', lambda _: None)
    assert channel.call('state') == {'ready': True}
    assert json.loads((channel.directory / 'request.json').read_text())['operation'] == 'state'


def test_channel_rejects_exited_process(tmp_path):
    channel = runner.Channel(tmp_path, SimpleNamespace(poll=lambda: 0))
    with pytest.raises(RuntimeError, match='exited'):
        channel.read('response.json')


def test_channel_does_not_echo_private_error_messages(tmp_path, monkeypatch):
    channel = runner.Channel(tmp_path, SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(runner.uuid, 'uuid4', lambda: SimpleNamespace(hex='new'))
    monkeypatch.setattr(channel, 'read', lambda _: {'id': 'new', 'ok': False,
                                                 'error': 'arbitrary exception contents'})
    with pytest.raises(runner.ProbeError) as error:
        channel.call('state')
    assert error.value.code == 'Unknown'
    assert 'arbitrary' not in str(error.value)


def test_probe_cannot_boot_in_a_worker_or_normal_app(monkeypatch):
    monkeypatch.delenv('SAMSARA_RECEIPTS_1241', raising=False)
    # This returns before importing Qt, the product runtime or starting a thread.
    assert installed_probe.start_services(object()) is None


def test_interactive_requires_explicit_scratch_root(tmp_path):
    with pytest.raises(SystemExit) as error:
        runner.main(['--home', str(tmp_path / 'home'), '--report', str(tmp_path)])
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('daily', [False, True])
@pytest.mark.parametrize('mismatch', ['missing-root', 'missing-home', 'wrong-executable', 'live-home', 'other-home'])
def test_staged_probe_does_not_start_with_mismatched_binding(copied_app, monkeypatch, daily, mismatch):
    root, app, legacy_home = copied_app
    home = root / 'runs' / 'fake-run' / 'home' if daily else legacy_home
    plugin = home / 'personal_plugins' / 'receipt_probe.py'
    plugin.parent.mkdir(parents=True)
    plugin.touch()
    monkeypatch.setattr(installed_probe, '__file__', str(plugin))
    monkeypatch.setattr(sys, 'executable', str(app / 'Samsara.exe'))
    monkeypatch.setenv(installed_probe.ROOT_ENV, str(root))
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(home))
    monkeypatch.setenv('SAMSARA_RECEIPTS_1241', '1')
    monkeypatch.setenv('SAMSARA_RECEIPTS_1262', '1' if daily else '0')
    assert installed_probe.staged_home(daily=daily) == home
    if mismatch == 'missing-root':
        monkeypatch.delenv(installed_probe.ROOT_ENV)
    elif mismatch == 'missing-home':
        monkeypatch.delenv('SAMSARA_HOME_DIR')
    elif mismatch == 'wrong-executable':
        other_app = root / 'other-app'
        other_app.mkdir()
        other_exe = other_app / 'Samsara.exe'
        other_exe.write_bytes((app / 'Samsara.exe').read_bytes())
        monkeypatch.setattr(sys, 'executable', str(other_exe))
    elif mismatch == 'live-home':
        live_root = root / '.samsara'
        live_root.mkdir()
        monkeypatch.setenv(installed_probe.ROOT_ENV, str(live_root))
        monkeypatch.setenv('SAMSARA_HOME_DIR', str(live_root))
    else:
        other_plugin = root / 'other' / 'personal_plugins' / 'receipt_probe.py'
        if daily:
            other_plugin = root / 'runs' / 'other-run' / 'home' / 'personal_plugins' / 'receipt_probe.py'
        other_plugin.parent.mkdir(parents=True)
        other_plugin.touch()
        monkeypatch.setattr(installed_probe, '__file__', str(other_plugin))
    assert installed_probe.staged_home(daily=daily) is None
    assert installed_probe.start_services(object()) is None


def test_probe_rejects_arbitrary_operations(tmp_path):
    with pytest.raises(ValueError, match='UnknownOperation'):
        installed_probe.perform(object(), tmp_path, 'arbitrary-command')


def test_failed_check_redacts_exception_and_continues(identity, tmp_path):
    harness = runner.Harness(tmp_path, tmp_path, tmp_path / 'report', identity, root=tmp_path)
    def failed():
        raise ValueError('exception contents must not be recorded')
    harness.check('T67', 'selection > apply', failed, 'Live app untested')
    harness.check('T70', 'ordinary command', lambda: {'observed': True}, 'One command')
    saved = (harness.report / 'receipts.json').read_text()
    assert 'exception contents' not in saved
    assert [row['result'] for row in json.loads(saved)] == ['FAIL', 'PASS']
    assert all(Path(row['evidence']).is_file() for row in json.loads(saved))


def test_quit_verifies_normal_exit_and_session_marker(identity, tmp_path):
    harness = runner.Harness(tmp_path, tmp_path, tmp_path / 'report', identity, root=tmp_path)
    process = SimpleNamespace(pid=123, returncode=0, poll=lambda: None, wait=lambda **_: None)
    harness.process = process
    harness.channel = SimpleNamespace(call=lambda *_, **__: {'quit_requested': True})
    harness.quit()
    assert harness.lifecycle[-1] == {'event': 'normal_exit', 'pid': 123, 'verified': True}
    marker = tmp_path / 'logs' / 'session.running'
    marker.parent.mkdir()
    marker.touch()
    with pytest.raises(RuntimeError, match='shutdown'):
        harness.quit()
    assert harness.lifecycle[-1]['verified'] is False
