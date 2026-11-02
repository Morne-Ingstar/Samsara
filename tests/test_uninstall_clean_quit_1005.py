"""Uninstall reaches the production shutdown body without booting the app.

Only teardown boundaries are replaced; marker lifecycle and exit diagnostics
are real. Native Inno smoke remains a separate, report-local build step.
"""
import ast
import ctypes
from ctypes import wintypes
import logging
import sys
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import boot, single_instance


ROOT = Path(__file__).resolve().parents[1]
ISS = (ROOT / 'installer/samsara.iss').read_text(encoding='utf-8')


def section(start, end):
    return ISS.split(start, 1)[1].split(end, 1)[0]


def production_quit(monkeypatch, logs):
    # Exact production definition, following the existing quit-method tests;
    # never import dictation or execute its bootstrap/constructor.
    source = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8-sig'))
    cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'quit_app')
    namespace = dict(logger=logging.getLogger('Samsara'), LOG_DIR=logs,
                     _samsara_boot=boot, os=__import__('os'),
                     thread_registry=SimpleNamespace(shutdown=Mock()),
                     audio_ducking=SimpleNamespace(shutdown_host=Mock()),
                     _ava_corrections=SimpleNamespace(flush_pending=Mock()),
                     flush_command_stats=Mock(), describe_text=str)
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<shutdown-1005>', 'exec'), namespace)
    for name, attributes in {
        'plugins.commands.ask_ollama': {'_finish_ava_session': Mock()},
        'plugins.commands.show_numbers': {'_destroy_overlay_completely': Mock()},
        'samsara.ui.reminder_toast': {'get_toast': Mock(return_value=SimpleNamespace(stop=Mock()))},
    }.items():
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(**attributes))
    app = SimpleNamespace(_running=True, _hold_capture_lifecycle_lock=threading.RLock(),
                          _resolve_parked_draft_before_quit=Mock(return_value=True),
                          cancel_recording=Mock(), _close_hold_capture_duck=Mock(),
                          stop_sound_stream=Mock(), history_db=SimpleNamespace(close=Mock()),
                          tray_icon=SimpleNamespace(stop=Mock()))
    app.quit_app = MethodType(namespace['quit_app'], app)
    return app


@pytest.mark.parametrize('silent', [False, True], ids=['interactive', 'silent'])
def test_uninstall_receiver_runs_real_shutdown_clears_marker_and_next_boot_is_clean(monkeypatch, tmp_path, caplog, silent, qapp):
    from samsara import process_exit
    from samsara.runtime import exit_reason

    logs = tmp_path / 'logs'
    assert boot.begin_session(logs) is None
    assert (logs / boot.SESSION_MARKER).exists()
    app = production_quit(monkeypatch, logs)
    monkeypatch.setattr(sys.modules['__main__'], 'app', None, raising=False)
    monkeypatch.setattr(single_instance, '_quit_target', None)
    hard_exit = Mock()
    monkeypatch.setattr(process_exit, 'hard_exit', hard_exit)
    monkeypatch.setattr(process_exit, 'trace_exit', Mock())
    single_instance.bind_quit_target(app)
    monkeypatch.setattr(single_instance, '_activation_receiver', None)
    ids = {single_instance._ACTIVATION_MESSAGE: 0xC100,
           single_instance.UNINSTALL_QUIT_MESSAGE: 0xC101}
    monkeypatch.setattr(single_instance, '_activation_user32',
                        lambda: SimpleNamespace(RegisterWindowMessageW=ids.__getitem__))
    single_instance._create_activation_receiver('1005.fake.profile')
    receiver = single_instance._activation_receiver
    try:
        msg = wintypes.MSG()
        msg.message = ids[single_instance.UNINSTALL_QUIT_MESSAGE]
        msg.wParam = int(silent)
        with caplog.at_level(logging.INFO, logger='Samsara'):
            assert receiver.nativeEvent(None, ctypes.addressof(msg)) == (True, 1)
            assert app._running  # queued on Qt, exactly as the tray's action
            qapp.processEvents()
    finally:
        receiver.deleteLater()
    assert not app._running
    app.cancel_recording.assert_called_once_with()
    app.history_db.close.assert_called_once_with()
    app.tray_icon.stop.assert_called_once_with()
    hard_exit.assert_called_once_with(0)
    assert not (logs / boot.SESSION_MARKER).exists()
    assert 'Uninstall requested clean shutdown' in caplog.text
    assert 'Shutting down Samsara... reason=user-quit' in caplog.text
    assert '[EXIT] Goodbye!' in caplog.text
    assert exit_reason.read_previous_exit(logs / 'last_exit.json') == 'user-quit'
    assert boot.begin_session(logs) is None
    boot.end_session(logs)


def test_quit_target_is_bound_before_the_constructor_blocks():
    source = (ROOT / 'dictation.py').read_text(encoding='utf-8-sig')
    assert 'bind_quit_target(self); self.create_tray_icon()' in source
    receiver = (ROOT / 'samsara/single_instance.py').read_text(encoding='utf-8')
    assert 'QTimer.singleShot(0, lambda: _request_uninstall_quit(silent=silent))' in receiver
    assert 'silent = bool(msg.wParam)' in receiver


def test_missing_target_is_logged_instead_of_silently_ignored(monkeypatch, caplog):
    monkeypatch.setattr(single_instance, '_quit_target', None)
    monkeypatch.setattr(sys.modules['__main__'], 'app', None, raising=False)
    single_instance._request_uninstall_quit()
    assert 'Uninstall quit target is not ready' in caplog.text


@pytest.mark.parametrize('field', ['retained_draft', 'dictate_pending_buffer'])
def test_silent_uninstall_with_unsent_words_does_not_open_draft_dialog(monkeypatch, caplog, field):
    app = SimpleNamespace(_session_mode_manager=SimpleNamespace(**{field: 'synthetic'}))
    app.quit_app = MethodType(lambda self, **kwargs: pytest.fail('must not open quit dialog'), app)
    monkeypatch.setattr(single_instance, '_quit_target', None)
    single_instance.bind_quit_target(app)
    single_instance._request_uninstall_quit(silent=True)
    assert 'Silent uninstall deferred: unsent draft' in caplog.text


def test_cuda_hardware_filter_precedes_types_sizes_and_unattended_selection():
    row = next(line for line in ISS.splitlines() if line.startswith('Name: "cuda_pack"'))
    assert 'Check: CanUseCuda' in row and 'ExtraDiskSpaceRequired:' in row
    check = section('function CanUseCuda()', 'procedure InitializeWizard()')
    assert 'if not GpuChecked then begin' in check
    assert 'GpuDetected := DetectNvidia();' in check
    assert 'GpuChecked := True;' in check
    assert 'Result := GpuDetected <> 0;' in check


def test_silent_uninstall_uses_same_quit_wait_and_identity_checks_without_forms():
    stop = section('procedure EnsureInstallStopped();', '{ Check every payload file')
    assert stop.index('if GetArrayLength(Processes) = 0 then exit;') < stop.index('if not UninstallSilent() then')
    deferred = stop.index('if UninstallSilent() then begin UninstallCloseCancelled')
    assert stop.index('if WaitForInstallExit(Processes, WaitForm, Status) then exit;') < deferred < stop.index('TerminateProcess(Handle, 0)')
    assert stop.count('if not UninstallSilent() then begin') == 2
    assert stop.index('WaitForm := nil;') < stop.index('CreateCustomForm')
    assert stop.index('RequestInstallQuit(Processes);') < stop.index('if WaitForInstallExit(Processes, WaitForm, Status) then exit;')
    assert 'if WaitForm <> nil then WaitForm.Free();' in stop
    wait = section('function WaitForInstallExit(', 'procedure EnsureInstallStopped();')
    assert 'if WaitForm <> nil then begin' in wait
    assert 'Sleep(250)' in wait and 'Processes[I].Handle' not in wait  # uses InstallIsRunning's retained handles


def test_confirmation_foreground_and_finish_text_layout():
    foreground = section('function InitializeUninstall()', '{ 994: never target')
    assert 'if not UninstallSilent() then BringToFrontAndRestore();' in foreground
    finish = section('if CurPageID = wpFinished then begin', 'function InitializeUninstall()')
    assert finish.index('FinishedLabel.Caption := Msg;') < finish.index('FinishedLabel.AdjustHeight();')
    assert 'WizardForm.RunList.Top := WizardForm.FinishedLabel.Top +' in finish
