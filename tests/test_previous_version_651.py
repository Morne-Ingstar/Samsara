"""Retained metadata, helper contracts, early failure counting and both entry points.

No dictation import, subprocesses, Git, live profile, or network access.
"""
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import previous_version as pv, updater as up
from samsara import update_installer as ins


@pytest.fixture
def kept(tmp_path):
    install = tmp_path / 'Samsara'
    previous = tmp_path / '.Samsara-rollback-old'
    for folder, content in ((install, b'current'), (previous, b'previous')):
        folder.mkdir()
        (folder / 'Samsara.exe').write_bytes(content)
    status = tmp_path / 'profile' / 'updates' / 'last_update.json'
    record = dict(version='0.30.0-beta.8', tag='v0.30.0-beta.8',
                  kept_at='2026-10-17T12:00:00Z', path=str(previous),
                  sha256=hashlib.sha256(b'previous').hexdigest(), size_bytes=8)
    (previous / pv.CAPABILITY_FILE).write_text(record['sha256'], encoding='utf-8')
    pv.write_record(status, dict(state='reported', previous=record))
    return SimpleNamespace(install=install, previous=previous, status=status, record=record)


def test_record_verifies_and_prepare_never_moves_the_trees(kept):
    assert pv.previous_info(status_path=kept.status, install_dir=kept.install, verify=True) == kept.record
    prepared = pv.prepare_revert(status_path=kept.status, install_dir=kept.install, expected=kept.record)
    assert prepared.reverse and prepared.staged_dir == kept.previous
    assert prepared.workspace_dir.is_dir() and not list(prepared.workspace_dir.iterdir())
    assert (kept.install / 'Samsara.exe').read_bytes() == b'current'
    assert (kept.previous / 'Samsara.exe').read_bytes() == b'previous'


@pytest.mark.parametrize('version,marker', [
    ('0.30.0-beta.6', None), ('0.30.0-beta.7', None),
    ('0.30.0-beta.8', None), ('0.30.0-beta.8', 'stale executable hash'),
])
def test_unmarked_or_stale_capability_is_kept_but_never_reverted(kept, version, marker):
    record = dict(kept.record, version=version, tag='v' + version)
    pv.write_record(kept.status, dict(previous=record))
    capability = kept.previous / pv.CAPABILITY_FILE
    if marker is None:
        capability.unlink()
    else:
        capability.write_text(marker, encoding='utf-8')
    assert pv.previous_info(status_path=kept.status, install_dir=kept.install) is None
    with pytest.raises(up.UpdateError, match='No verified previous'):
        pv.prepare_revert(status_path=kept.status, install_dir=kept.install)
    assert pv.read_record(kept.status)['previous'] == record
    assert (kept.previous / 'Samsara.exe').read_bytes() == b'previous'
    assert not list(kept.install.parent.glob('.Samsara-update-*'))


@pytest.mark.parametrize('marked', [False, True])
def test_capability_controls_real_row_tray_and_twice_failed_offer(qapp, monkeypatch, kept, marked):
    from samsara import paths, update_installer_customizations as custom
    from samsara.ui import update_qt as ui
    from samsara.ui.tray_qt import SamsaraTrayQt
    from tests.test_tray_qt import _make_app, _stub_tray
    from PySide6.QtWidgets import QMenu
    if not marked:
        (kept.previous / pv.CAPABILITY_FILE).unlink()
    lookup = pv.previous_info
    monkeypatch.setattr(pv, 'previous_info', lambda: lookup(status_path=kept.status, install_dir=kept.install))
    monkeypatch.setattr(ui, 'update_unavailable_reason', lambda: None)
    row = ui._PreviousVersionRow(SimpleNamespace())
    menu = QMenu()
    action = ui.add_previous_tray_item(menu, SimpleNamespace())
    assert row.button.isEnabled() is marked
    assert (action is not None) is marked
    app = _make_app()
    app.config['updates'] = {'tray_menu_entry': True}
    tray, _ = _stub_tray(monkeypatch, app)
    tools = next(a.menu() for a in tray._menu.actions() if a.text() == 'Tools')
    assert any(a.text() == 'Go back to the previous version' for a in tools.actions()) is marked
    if not marked:
        assert row.detail.text() == 'No previous version is available to go back to.'
        app.config['updates'] = {'tray_menu_entry': False}
        assert not SamsaraTrayQt._update_entry_enabled(tray)
    monkeypatch.setattr(pv.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(pv.sys, 'argv', ['Samsara.exe'])
    monkeypatch.delenv('SAMSARA_HOME_DIR', raising=False)
    monkeypatch.setattr(paths, 'samsara_home_dir', lambda: kept.status.parents[1])
    monkeypatch.setattr(pv, 'another_instance_running', lambda: False)
    monkeypatch.setattr(custom, '_helper_owns_update', lambda _: False)
    counter = kept.status.parent / 'startup_failures.json'
    from samsara import __version__
    for pid in (1, 2):
        pv.begin_start(counter, __version__, pid=pid, is_running=lambda _: False)
    begin = pv.begin_start
    monkeypatch.setattr(pv, 'begin_start', lambda path, version: begin(path, version, is_running=lambda _: False))
    show = Mock(return_value=False)
    monkeypatch.setattr(ui, 'startup_previous_offer', show)
    pv.early_startup()
    assert show.called is marked
    assert pv.read_record(counter)['failures'] == 2
    assert kept.previous.is_dir()
    row.deleteLater(); menu.deleteLater()


@pytest.mark.parametrize('change', ['missing', 'hash', 'outside', 'tag', 'relative'])
def test_invalid_kept_copy_is_not_used(kept, change):
    record = dict(kept.record)
    if change == 'missing':
        (kept.previous / 'Samsara.exe').unlink()
    elif change == 'hash':
        (kept.previous / 'Samsara.exe').write_bytes(b'changed')
    elif change == 'outside':
        record['path'] = str(kept.install)
    elif change == 'tag':
        record['tag'] = 'v1.0.0'
    else:
        record['path'] = '.Samsara-rollback-old'
    pv.write_record(kept.status, dict(previous=record))
    assert pv.previous_info(status_path=kept.status, install_dir=kept.install, verify=True) is None
    with pytest.raises(up.UpdateError):
        pv.prepare_revert(status_path=kept.status, install_dir=kept.install)


def test_changed_confirmation_does_not_revert_another_version(kept):
    expected = dict(kept.record, version='0.1.0')
    with pytest.raises(up.UpdateError, match='changed'):
        pv.prepare_revert(status_path=kept.status, install_dir=kept.install, expected=expected)


def test_previous_metadata_survives_new_update_and_notice_consumption(kept, monkeypatch):
    for state in ('ready', 'waiting', 'awaiting_confirmation', 'installed', 'reported'):
        up._write_status(kept.status, state, 'test status', 'v1.0.0')
        assert pv.read_record(kept.status)['previous'] == kept.record
    monkeypatch.setattr(up, 'update_unavailable_reason', lambda: None)
    monkeypatch.setattr(up, 'samsara_home_dir', lambda: kept.status.parents[1])
    monkeypatch.setattr(ins, 'repair_notice', lambda: None)
    assert up.reconcile_update_on_startup() is None
    assert up.reconcile_update_on_startup() is None
    assert pv.read_record(kept.status)['previous'] == kept.record


def test_successful_retained_copy_is_excluded_from_cleanup(kept, monkeypatch):
    workspace = kept.install.parent / '.Samsara-update-completed'
    pv.write_record(kept.status, dict(state='installed', tag='v1.0.0',
        install_dir=str(kept.install), rollback_dir=str(kept.previous),
        workspace_dir=str(workspace), previous=kept.record))
    monkeypatch.setattr(up, 'update_unavailable_reason', lambda: None)
    monkeypatch.setattr(up, 'samsara_home_dir', lambda: kept.status.parents[1])
    monkeypatch.setattr(up, '_frozen_install_dir', lambda: kept.install)
    monkeypatch.setattr(ins, 'repair_notice', lambda: None)
    runner = Mock(side_effect=AssertionError('no cleanup process needed'))
    assert up.reconcile_update_on_startup(process_runner=runner) is None
    runner.assert_not_called()
    assert kept.previous.is_dir()
    assert pv.read_record(kept.status)['previous'] == kept.record


def forward(kept):
    workspace = kept.install.parent / '.Samsara-update-new'
    staged = workspace / 'payload'
    staged.mkdir(parents=True)
    (staged / 'Samsara.exe').write_bytes(b'updated')
    return up.PreparedUpdate('0.31.0', 'v0.31.0', kept.install, staged,
        kept.install.parent / '.Samsara-rollback-new', workspace, kept.status,
        kept.status.parent / 'install-v0.31.0-new.ps1')


def test_zip_keeps_copy_only_after_health_and_removes_older_copy(kept):
    script = up._archive_helper_script(forward(kept), 2147483000)
    assert script.index("Write-UpdateStatus 'staging_health'") < script.index('\n    Mark-CurrentCapability\n')
    assert script.index('\n    Mark-CurrentCapability\n') < script.index('Move-Item -LiteralPath $install -Destination $rollback')
    marking = script[script.index('function Mark-CurrentCapability'):script.index('function Get-PreviousHash')]
    assert 'Join-Path $install' in marking and '$staged' not in marking and '$rollback' not in marking
    assert "Get-PreviousHash (Join-Path $install 'Samsara.exe')" in marking
    retention = script[script.index('function Keep-PreviousVersion'):script.index('function Write-UpdateStatus')]
    assert 'Mark-CurrentCapability' not in retention
    success = script.index('$cleanupErrors = @()')
    assert script.index('Keep-PreviousVersion', success) > script.index("$currentStatus.state -eq 'installed'")
    assert 'Remove-Item -LiteralPath $rollback -Recurse -Force -ErrorAction Stop' not in script[success:script.index('exit 0', success)]
    for field in ('version=$sourceVersion', 'tag=$sourceTag', 'kept_at=', 'path=$rollback', 'sha256=$hash', 'size_bytes=$size'):
        assert field in script
    assert "$oldExe = Join-Path $rollback 'Samsara.exe'" in retention
    assert script.index('Write-UpdateStatus $confirmed.state $confirmed.message') < script.index('\n    Remove-ObsoletePrevious\n}', script.index('function Keep-PreviousVersion'))
    assert 'Remove-Item -LiteralPath $script:obsoletePrevious -Recurse -Force' in script
    assert 'Assert-PreviousPath $script:obsoletePrevious' in script


def test_installer_success_uses_same_retention(kept, monkeypatch):
    from dataclasses import asdict
    p = forward(kept)
    installer = p.workspace_dir / 'SamsaraSetup-0.31.0.exe'
    installer.write_bytes(b'installer')
    p = ins.PreparedInstaller(**asdict(p), installer_path=installer,
                              installer_sha256=hashlib.sha256(b'installer').hexdigest())
    monkeypatch.setattr(ins, 'installation', lambda *_: ins.Installation(kept.install, kind='installed'))
    script = ins.helper(p, 2147483000)
    assert script.index('\n    Mark-CurrentCapability\n') < script.index('Copy-Item -LiteralPath $install -Destination $staged -Recurse -Force')
    assert 'Copy-Item -LiteralPath $install -Destination $staged -Recurse -Force' in script
    healthy = script.index("Write-HelperLog 'health confirmed'")
    assert 'Keep-PreviousVersion' in script[healthy:script.index('exit 0', healthy)]
    assert 'previous=$script:previous' in script


def test_reverse_swap_health_and_restore_contract(kept):
    prepared = pv.prepare_revert(status_path=kept.status, install_dir=kept.install)
    script = up._helper_script(prepared, 2147483000)
    transaction = script[script.index('$oldMoved = $false'):]
    outgoing = transaction.index('Move-Item -LiteralPath $install -Destination $rollback')
    incoming = transaction.index('Move-Item -LiteralPath $staged -Destination $install')
    health = transaction.index("Write-HelperLog 'reverse health confirmed'")
    assert outgoing < incoming < health < transaction.index('Keep-PreviousVersion')
    failed = transaction[transaction.index('} catch {\n    $problem'):]
    assert failed.index('Move-Item -LiteralPath $install -Destination $staged') < failed.index('Move-Item -LiteralPath $rollback -Destination $install')
    assert "Write-UpdateStatus 'rolled_back'" in failed
    assert '$previous = $originalPrevious' in failed
    assert 'Remove-Item -LiteralPath $install' not in transaction
    assert "Write-UpdateStatus 'failed'" in failed and 'both copies were retained' in failed
    assert script.count("Get-PreviousHash (Join-Path $staged 'Samsara.exe')") == 2
    assert transaction.index('Mark-CurrentCapability') < outgoing
    assert script.index('Assert-PreviousCapability $staged $previous.sha256') < script.index("Write-UpdateStatus 'waiting'")
    assert transaction.index('Assert-PreviousCapability $staged $originalPrevious.sha256') < outgoing
    assert 'Write-HelperLog' in script and 'helper_pid=$PID' in script


def test_retry_cleanup_keeps_rollback_and_persists_metadata(kept):
    script = up._cleanup_retry_script(kept.install.parent / '.Samsara-update-new',
                                     kept.previous, kept.install, kept.status, 'v1.0.0')
    assert 'if ($candidate -ieq $script:previous.path) { continue }' in script
    assert 'previous=$script:previous' in script
    assert 'if ($sourceVersion -and $sourceTag) { Keep-PreviousVersion }' in script
    assert 'Assert-PreviousPath $script:obsoletePrevious' in script


def test_installed_reverse_updates_version_and_restores_registration_on_failure(kept, monkeypatch):
    prepared = pv.prepare_revert(status_path=kept.status, install_dir=kept.install)
    monkeypatch.setattr(ins, 'installation', lambda *_: ins.Installation(kept.install, kind='installed', view=32))
    script = up._helper_script(prepared, 2147483000)
    assert '[Microsoft.Win32.RegistryView]::Registry32' in script
    assert "$key.SetValue('DisplayVersion', $tag.TrimStart('v'))" in script
    assert 'if ($registrySnapshot) { Restore-InstallRegistry $registrySnapshot }' in script
    assert script.index('Move-Item -LiteralPath $staged -Destination $install') < script.index("$key.SetValue('DisplayVersion', $tag.TrimStart('v'))")


def test_pending_cleanup_launch_owns_stdio_and_avoids_detached_powershell(kept, monkeypatch):
    monkeypatch.setattr(up, '_system_powershell_path', lambda: Path('powershell.exe'))
    calls = []
    def runner(command, **kwargs):
        assert not kwargs['stdout'].closed
        calls.append((command, kwargs))
    up._launch_cleanup_retry(kept.install.parent / '.Samsara-update-completed',
        kept.previous, kept.install, kept.status, 'v1.0.0', process_runner=runner)
    command, options = calls[0]
    assert not options['creationflags'] & up.subprocess.DETACHED_PROCESS
    assert options['stdin'] == up.subprocess.DEVNULL
    assert options['stdout'] is options['stderr']
    assert '-WindowStyle' in command and 'Hidden' in command


def test_first_two_failed_starts_offer_only_on_third(tmp_path):
    path = tmp_path / 'failures.json'
    kwargs = dict(is_running=lambda _: False)
    assert not pv.begin_start(path, 'v1', now=0, pid=1, **kwargs)
    assert pv.read_record(path)['failures'] == 0
    assert not pv.begin_start(path, 'v1', now=1, pid=2, **kwargs)
    assert pv.read_record(path)['failures'] == 1
    assert pv.begin_start(path, 'v1', now=2, pid=3, **kwargs)
    assert pv.read_record(path)['first_failed_at'] == 0


@pytest.mark.parametrize('version,now', [('different', 2), ('v1', 601), ('v1', -1)])
def test_different_version_expiry_and_clock_change_reset_counter(tmp_path, version, now):
    path = tmp_path / 'failures.json'
    for pid, stamp in ((1, 0), (2, 1)):
        pv.begin_start(path, 'v1', now=stamp, pid=pid, is_running=lambda _: False)
    assert not pv.begin_start(path, version, now=now, pid=3, is_running=lambda _: False)
    assert pv.read_record(path)['failures'] == 0


def test_concurrent_shortcut_is_not_a_start_failure(tmp_path):
    path = tmp_path / 'failures.json'
    pv.begin_start(path, 'v1', now=0, pid=1, is_running=lambda _: False)
    old = path.read_bytes()
    assert not pv.begin_start(path, 'v1', now=1, pid=2, is_running=lambda pid: pid == 1)
    assert path.read_bytes() == old


def test_healthy_start_clears_only_its_own_counter(tmp_path):
    path = tmp_path / 'failures.json'
    pv.begin_start(path, 'v1', now=0, pid=1, is_running=lambda _: False)
    pv.mark_healthy(path, pid=2)
    assert path.exists()
    pv.mark_healthy(path, pid=1)
    assert not path.exists()
    assert not pv.begin_start(path, 'v1', now=5, pid=3, is_running=lambda _: False)


@pytest.mark.parametrize('raw', ['not json', '[]', '{"version":"v1","pending_pid":123,"started_at":"bad"}'])
def test_corrupt_counter_is_replaced_without_blocking_startup(tmp_path, raw):
    path = tmp_path / 'failures.json'
    path.write_text(raw)
    assert not pv.begin_start(path, 'v1', now=1, pid=1, is_running=lambda _: False)
    assert pv.read_record(path)['failures'] == 0


def test_early_hook_is_after_maintenance_before_native_imports():
    source = (Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8')
    ast.parse(source)
    assert source.index('_samsara_boot._enable_faulthandler(') < source.index('_previous_early_startup()') < source.index('import sounddevice')
    boot = (Path(__file__).resolve().parents[1] / 'samsara' / 'boot.py').read_text(encoding='utf-8')
    assert 'migrate_before_boot()  # Existing pre-native call site' in boot


@pytest.mark.parametrize('argv', [['Samsara.exe', '--restart-helper'], ['Samsara.exe', '--fetch-components'], ['Samsara.exe', '--migrate-update-customizations']])
def test_maintenance_launches_do_not_count(argv, monkeypatch):
    monkeypatch.setattr(pv.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(pv.sys, 'argv', argv)
    begin = Mock(side_effect=AssertionError('must not count maintenance'))
    monkeypatch.setattr(pv, 'begin_start', begin)
    pv.early_startup()
    begin.assert_not_called()


def test_update_helper_owned_start_does_not_count(monkeypatch, tmp_path):
    from samsara import paths, update_installer_customizations as custom
    monkeypatch.setattr(pv.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(pv.sys, 'argv', ['Samsara.exe'])
    monkeypatch.delenv('SAMSARA_HOME_DIR', raising=False)
    monkeypatch.setattr(paths, 'samsara_home_dir', lambda: tmp_path)
    monkeypatch.setattr(pv, 'another_instance_running', lambda: False)
    monkeypatch.setattr(custom, '_helper_owns_update', lambda payload: True)
    begin = Mock(side_effect=AssertionError('helper owns recovery'))
    monkeypatch.setattr(pv, 'begin_start', begin)
    pv.early_startup()
    begin.assert_not_called()


@pytest.mark.parametrize('offer,has_copy,expected', [(False, True, False), (True, False, False), (True, True, True)])
def test_early_offer_requires_counter_threshold_and_a_kept_copy(monkeypatch, tmp_path, kept, offer, has_copy, expected):
    from samsara import paths, update_installer_customizations as custom
    from samsara.ui import update_qt as ui
    monkeypatch.setattr(pv.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(pv.sys, 'argv', ['Samsara.exe', '--quiet'])
    monkeypatch.delenv('SAMSARA_HOME_DIR', raising=False)
    monkeypatch.setattr(paths, 'samsara_home_dir', lambda: tmp_path)
    monkeypatch.setattr(pv, 'another_instance_running', lambda: False)
    monkeypatch.setattr(custom, '_helper_owns_update', lambda payload: False)
    monkeypatch.setattr(pv, 'begin_start', lambda path, version: offer)
    monkeypatch.setattr(pv, 'previous_info', lambda: kept.record if has_copy else None)
    show = Mock(return_value=False)  # Keep trying continues normal boot.
    monkeypatch.setattr(ui, 'startup_previous_offer', show)
    pv.early_startup()
    assert show.called is expected


def test_running_instance_shortcut_never_records_failure_or_offers_revert(monkeypatch):
    monkeypatch.setattr(pv.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(pv.sys, 'argv', ['Samsara.exe'])
    monkeypatch.delenv('SAMSARA_HOME_DIR', raising=False)
    monkeypatch.setattr(pv, 'another_instance_running', lambda: True)
    begin = Mock(side_effect=AssertionError('a second shortcut is not a crash'))
    monkeypatch.setattr(pv, 'begin_start', begin)
    pv.early_startup()
    begin.assert_not_called()


@pytest.mark.parametrize('handle,error,expected', [(1, 0, True), (0, 2, False), (0, 5, True)])
def test_running_instance_mutex_probe_only_queries_existence(monkeypatch, handle, error, expected):
    import ctypes
    from samsara.single_instance import DEFAULT_MUTEX_NAME
    kernel = SimpleNamespace(OpenMutexW=Mock(return_value=handle), CloseHandle=Mock())
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **kw: kernel)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: error)
    assert pv.another_instance_running() is expected
    kernel.OpenMutexW.assert_called_once_with(0x00100000, False, DEFAULT_MUTEX_NAME)
    if handle:
        kernel.CloseHandle.assert_called_once_with(handle)
    else:
        kernel.CloseHandle.assert_not_called()


def test_kept_copy_makes_updates_entry_visible_above_recovery(monkeypatch, kept):
    from samsara.ui.tray_qt import SamsaraTrayQt
    app = SimpleNamespace(config={'updates': {'tray_menu_entry': False}})
    tray = SimpleNamespace(_app=app)
    monkeypatch.setattr(pv, 'previous_info', lambda: kept.record)
    assert SamsaraTrayQt._update_entry_enabled(tray)
    monkeypatch.setattr(pv, 'previous_info', lambda: None)
    assert not SamsaraTrayQt._update_entry_enabled(tray)


@pytest.mark.parametrize('found_update', [False, True])
def test_kept_copy_stays_directly_below_updates_even_with_flag_off(qapp, monkeypatch, kept, found_update):
    from tests.test_tray_qt import _make_app, _stub_tray
    from samsara.ui import tray_qt
    app = _make_app()
    app.config['updates'] = {'tray_menu_entry': False}
    monkeypatch.setattr(pv, 'previous_info', lambda: kept.record)
    tray, _ = _stub_tray(monkeypatch, app)
    if found_update:
        tray._available_update = SimpleNamespace(version='9.9.9')
        tray._rebuild_menu()
    tools = next(action.menu() for action in tray._menu.actions() if action.text() == 'Tools')
    texts = [action.text() for action in tools.actions()]
    assert texts.index('Go back to the previous version') == texts.index(tray_qt.update_check_menu_label()) + 1


def test_disabled_updates_row_and_no_tray_item_without_compatible_copy(qapp, monkeypatch):
    from samsara.ui import update_qt as ui
    from PySide6.QtWidgets import QMenu
    reason = 'No previous version is available to go back to.'
    monkeypatch.setattr(ui, 'previous_entry', lambda: (None, reason))
    row = ui._PreviousVersionRow(SimpleNamespace())
    menu = QMenu()
    action = ui.add_previous_tray_item(menu, SimpleNamespace())
    assert not row.button.isEnabled() and action is None and not menu.actions()
    assert row.detail.text() == row.button.toolTip() == reason
    row.deleteLater(); menu.deleteLater()


def test_enabled_row_shows_kept_version_date_and_cost(qapp, monkeypatch, kept):
    from samsara.ui import update_qt as ui
    monkeypatch.setattr(ui, 'previous_entry', lambda: (dict(kept.record, size_bytes=850 * 1024 * 1024), ''))
    row = ui._PreviousVersionRow(SimpleNamespace())
    assert row.button.isEnabled()
    for value in ('v0.30.0-beta.8', '2026-10-17', '850.0 MB'):
        assert value in row.detail.text()
    row.deleteLater()


@pytest.mark.parametrize('failed', [None, '0.30.0-beta.9'])
def test_confirmation_and_twice_failed_offer_use_owner_wording(qapp, kept, failed):
    from samsara.ui import update_qt as ui
    dialog, button = ui.previous_dialog(kept.record, failed_version=failed)
    assert button.text() == 'Go back'
    assert 'Keep trying' in [b.text() for b in dialog.buttons()] if failed else 'Cancel' in [b.text() for b in dialog.buttons()]
    assert dialog.text() == (
        "Samsara v0.30.0-beta.9 didn't start twice. Go back to v0.30.0-beta.8?" if failed else
        'Go back to v0.30.0-beta.8? Samsara will restart.')
    assert dialog.defaultButton() is not button
    dialog.deleteLater()


def test_cancelled_parked_draft_never_launches_or_quits(qapp, monkeypatch, kept):
    from samsara.ui import update_qt as ui
    app = SimpleNamespace(_resolve_parked_draft_before_quit=lambda: False, quit_app=Mock())
    prepared = pv.prepare_revert(status_path=kept.status, install_dir=kept.install)
    launch = Mock(side_effect=AssertionError('cancelled'))
    monkeypatch.setattr(pv, 'launch_revert', launch)
    request = ui._PreviousRequest(app, None, kept.record)
    request.complete(prepared, '')
    launch.assert_not_called(); app.quit_app.assert_not_called()
    assert not prepared.workspace_dir.exists()


def test_helper_launch_failure_keeps_running_app(qapp, monkeypatch, kept):
    from samsara.ui import update_qt as ui
    app = SimpleNamespace(_resolve_parked_draft_before_quit=lambda: True, quit_app=Mock())
    prepared = pv.prepare_revert(status_path=kept.status, install_dir=kept.install)
    monkeypatch.setattr(pv, 'launch_revert', Mock(side_effect=RuntimeError('synthetic launch failure')))
    warning = Mock()
    monkeypatch.setattr(ui.QMessageBox, 'warning', warning)
    request = ui._PreviousRequest(app, None, kept.record)
    request.complete(prepared, '')
    app.quit_app.assert_not_called(); warning.assert_called_once()
    assert (kept.install / 'Samsara.exe').read_bytes() == b'current'
