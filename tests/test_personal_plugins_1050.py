"""1050: private overlays, release exclusions and migration with synthetic data."""
import json
import logging
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import plugin_commands as plugins, support_bundle
from tools import migrate_personal_commands as migration, release_manifest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'home'))
    monkeypatch.setattr(plugins, '_REGISTRY', {})
    monkeypatch.setattr(plugins, '_MODULE_ENTRIES', {})
    monkeypatch.setattr(plugins, '_LOADED_MODULES', {})
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith('samsara_personal_'):
            sys.modules.pop(name, None)


def plugin(folder, name='fixture.py', phrase='private fixture', tail=''):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text('from samsara.plugin_commands import command\n'
                    f'@command({phrase!r})\ndef fixture(app, remainder):\n    return True\n' + tail,
                    encoding='utf-8')
    return path


@pytest.mark.parametrize('setting', ['', 'relative', 'absolute', '~/overlay'])
def test_frozen_executor_loads_external_python_and_never_edits_bundle(tmp_path, monkeypatch, setting):
    from samsara.commands import CommandExecutor
    home = tmp_path / 'home'
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    bundle = tmp_path / 'daily' / '_internal'
    bundle.mkdir(parents=True)
    monkeypatch.setattr(sys, '_MEIPASS', str(bundle), raising=False)
    directory = (Path(setting).expanduser() if setting.startswith('~') else
                 tmp_path / 'absolute' if setting == 'absolute' else home / (setting or 'personal_plugins'))
    configured = str(directory) if setting == 'absolute' else setting
    plugin(directory)
    plugin(directory, 'a_broken.py', 'partial fixture', 'raise RuntimeError("private failure detail")\n')
    path = bundle / 'commands.json'
    path.write_text('{"commands": {"bundled fixture": {"type": "hotkey", "keys": "x"}}}')
    before = path.read_bytes()
    app = SimpleNamespace(config={'plugins': {'personal_dir': configured}})
    executor = CommandExecutor(path, app=app, plugins_dir=bundle / 'plugins' / 'commands')
    assert executor._matcher.match('private fixture')[0].handler(None, '') is True
    assert executor._matcher.match('partial fixture')[0] is None
    assert path.read_bytes() == before
    assert not (bundle / 'personal_plugins').exists()


def test_collision_message_never_contains_personal_phrase(tmp_path, caplog):
    plugin(tmp_path / 'home' / 'personal_plugins', phrase='private fixture')
    with caplog.at_level(logging.WARNING):
        plugins.load_extra_plugins(builtin_commands={'shipped fixture': {'aliases': ['private fixture']}})
    assert 'collision' in caplog.text
    assert 'private fixture' not in caplog.text
    assert not plugins._REGISTRY


def test_shipped_metadata_and_absent_home_guidance_exclude_loaded_personal(tmp_path, monkeypatch):
    from tools.dump_command_metadata import build_executor
    from samsara.command_catalog import build_catalog, guidance_catalog
    plugin(tmp_path / 'home' / 'personal_plugins')
    plugins.load_extra_plugins()
    # Exercise real tooling with an already-populated personal registry.
    executor = build_executor()
    records = build_catalog(executor)
    assert records
    assert 'private fixture' not in json.dumps([r.to_dict() for r in records])
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'absent'))
    guidance = guidance_catalog(executor._matcher.list_commands())
    assert guidance and 'private fixture' not in json.dumps(guidance)
    for relative in ('commands.json', 'commands_catalog.json', 'Docs/COMMAND_CATALOG.md'):
        assert 'private fixture' not in (ROOT / relative).read_text(encoding='utf-8')


@pytest.mark.parametrize('line', [
    '[PLUGINS] Skipped personal command private fixture: collision',
    '[PLUGIN] Executing: private fixture',
    '[EXEC] private fixture refused',
    '[CMD-COMPACT] words -> private fixture',
    '[SHOW_LATENCY] route_matched command=private fixture',
    '[SCOPE] private fixture unavailable',
    '[CHAIN] settle after private fixture',
    'Plugin command private fixture failed',
    '{"canonical": "private fixture"}',
])
def test_command_bearing_diagnostics_are_redacted(line):
    assert 'private fixture' not in support_bundle._scrub_log_line(line)


def test_actual_support_archive_excludes_plugin_files_backups_and_legacy_log_names(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    plugin(home / 'personal_plugins')
    (home / 'backups').mkdir()
    (home / 'backups' / 'private.zip').write_bytes(b'private fixture')
    (home / 'logs').mkdir()
    (home / 'logs' / 'samsara.log').write_text('[PLUGIN] Executing: private fixture\nsafe diagnostic\n')
    (home / 'config.json').write_text(json.dumps({'plugins': {'personal_dir': 'private fixture'}}))
    monkeypatch.setattr(support_bundle, '_system_text', lambda: 'synthetic system\n')
    monkeypatch.setattr(support_bundle, '_version_text', lambda config: 'synthetic version\n')
    result = support_bundle.build_bundle(home_dir=home, desktop_dir=tmp_path / 'desktop')
    with zipfile.ZipFile(result) as archive:
        assert not any('personal_plugins' in p or 'backup' in p for p in archive.namelist())
        assert all(b'private fixture' not in archive.read(p) for p in archive.namelist())
        assert b'safe diagnostic' in archive.read('samsara.log')


def test_build_manifest_smoke_excludes_even_tracked_private_payloads(tmp_path, monkeypatch):
    # Mock Git's inventory; tests work in a source export and never spawn Git.
    (tmp_path / '.git').mkdir()
    paths = ['plugins/commands/public.py', 'plugins/commands/local_fixture.py',
             'plugins/commands/demo_commands.py', 'plugins/personal_plugins/fixture.py',
             'plugins/backups/private.zip']
    for name in paths:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('synthetic')
    monkeypatch.setattr(release_manifest.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=('\0'.join(paths) + '\0').encode()))
    entries = release_manifest.tracked_tree_datas(tmp_path, 'plugins', 'plugins')
    assert [Path(src).name for src, _ in entries] == ['public.py']
    assert "tracked_tree_datas(app_dir, 'plugins', 'plugins')" in (ROOT / 'scripts/samsara.spec').read_text()


def migration_tree(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    path = plugin(repo / 'plugins' / 'commands', 'local_fixture.py')
    monkeypatch.setattr(migration, 'RETIRED', frozenset({migration.fingerprint('retired fixture')}))
    (repo / 'commands.json').write_text(json.dumps({'commands': {
        'public fixture': {'type': 'hotkey', 'keys': 'x'},
        'retired fixture': {'type': 'hotkey', 'keys': 'y'}}}))
    (repo / 'commands_catalog.json').write_text(json.dumps({'commands': [
        {'canonical_id': 'local.fixture', 'aliases': ['private fixture'], 'source': 'plugins/commands/local_fixture.py:2'},
        {'canonical_id': 'builtin.retired_fixture', 'aliases': ['retired fixture']},
        {'canonical_id': 'builtin.public_fixture', 'aliases': ['public fixture']},
    ]}))
    (repo / 'Docs').mkdir()
    (repo / 'Docs' / 'COMMAND_CATALOG.md').write_text('local.fixture\nbuiltin.retired_fixture\nbuiltin.public_fixture\n')
    return repo, path, tmp_path / 'personal_plugins'


def test_migration_dry_run_apply_backup_catalog_cleanup_and_idempotency(tmp_path, monkeypatch, capsys):
    repo, path, dest = migration_tree(tmp_path, monkeypatch)
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    assert migration.migrate(repo, dest) == 2
    assert 'retired fixture' in capsys.readouterr().out
    assert path.exists() and not dest.exists()
    assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    assert migration.migrate(repo, dest, apply=True) == 2
    assert not path.exists()
    assert (dest / path.name).read_bytes() == before[path.relative_to(repo)]
    assert (dest / 'migration_backup' / 'commands.json').read_bytes() == before[Path('commands.json')]
    assert 'retired fixture' not in (repo / 'commands.json').read_text()
    assert 'private fixture' not in (repo / 'commands_catalog.json').read_text()
    assert 'builtin.retired_fixture' not in (repo / 'Docs' / 'COMMAND_CATALOG.md').read_text()
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(dest.parent))
    assert plugins.load_extra_plugins() == 2
    handler = plugins._REGISTRY['retired fixture']['func']
    seen = []
    monkeypatch.setattr(sys.modules[handler.__module__], 'get_handler',
                        lambda kind: SimpleNamespace(execute=lambda data, ctx: seen.append((data, ctx)) or True))
    app = SimpleNamespace(command_executor=SimpleNamespace(_build_context=lambda app: 'fixture context'))
    assert handler(app, '') is True and seen[0][0]['keys'] == 'y'
    assert migration.migrate(repo, dest, apply=True) == 0


def test_migration_refuses_inside_tree_and_existing_destination(tmp_path, monkeypatch):
    repo, path, dest = migration_tree(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='outside'):
        migration.migrate(repo, repo / 'personal_plugins', apply=True)
    plugin(dest, path.name)
    with pytest.raises(ValueError, match='overwrite'):
        migration.migrate(repo, dest, apply=True)
    assert path.exists()


def test_incident_bundle_scrubs_logs_flight_and_config(tmp_path, monkeypatch):
    from tools import bundle_incident
    home = tmp_path / 'incident-home'
    (home / 'logs').mkdir(parents=True)
    (home / 'flight').mkdir()
    (home / 'logs' / 'samsara.log').write_text('[PLUGIN] Executing: private fixture\n')
    (home / 'flight' / 'events-2000.jsonl').write_text('{"command_name": "private fixture"}\n')
    (home / 'probe.log').write_text('[EXEC] private fixture refused\n')
    (home / 'guard.log').write_text('[SCOPE] private fixture unavailable\n')
    (home / 'freeze_stacks.txt').write_text('File personal_plugins/private fixture.py\n')
    (home / 'config.json').write_text(json.dumps({'plugins': {'personal_dir': 'private fixture'}}))
    monkeypatch.setattr(bundle_incident, '_git_head', lambda repo: 'synthetic-head')
    result = bundle_incident.bundle_incident(home, tmp_path)
    assert all('private fixture' not in p.read_text() for p in result.iterdir())
    event = json.loads((result / 'flight_events.jsonl').read_text())
    assert event['event'] == 'redacted'


def test_default_home_resolution_without_environment_override(tmp_path, monkeypatch):
    from samsara.paths import samsara_home_dir
    monkeypatch.delenv('SAMSARA_HOME_DIR', raising=False)
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    home = tmp_path / '.samsara'
    assert samsara_home_dir() == home
    plugin(home / 'personal_plugins')
    assert plugins.load_extra_plugins() == 1
    assert plugins._REGISTRY['private fixture']['source'] == 'personal'
