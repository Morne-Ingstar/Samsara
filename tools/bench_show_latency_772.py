"""Isolated offscreen handler benchmark; never boots dictation or reads a live profile.

Run with --real-enumerator to additionally time native enumeration (counts only).
Times are local wall times, not microphone/STT or native compositor measurements.
"""
import argparse
import contextlib
import importlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real-enumerator', action='store_true')
    args = parser.parse_args()
    measurements = []

    def timed(stage, fn):
        start = perf_counter()
        result = fn()
        measurements.append((stage, (perf_counter() - start) * 1000))
        return result

    with tempfile.TemporaryDirectory(prefix='show-latency-772-') as profile:
        for key in ('HOME', 'USERPROFILE', 'SAMSARA_HOME_DIR', 'APPDATA', 'LOCALAPPDATA'):
            os.environ[key] = profile
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        os.environ['QT_QPA_FONTDIR'] = r'C:\Windows\Fonts'
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        # Suppress production chatter: native titles must never enter evidence.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cube = timed('import command handler', lambda: importlib.import_module('plugins.commands.window_cube'))
            ui = timed('import cube Qt', lambda: importlib.import_module('samsara.ui.window_cube_qt'))
            cs = timed('import reference Qt', lambda: importlib.import_module('samsara.ui.command_cheatsheet_qt'))
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication
            from samsara import execution_policy as policy
            from tools.dump_command_metadata import build_executor
            qt = QApplication.instance() or QApplication([])
            qt.setQuitOnLastWindowClosed(False)
            executor = timed('build registry (boot only)', build_executor)
            app = SimpleNamespace(config={'window_cube': {'max_rows': 15}}, command_executor=executor)
            windows = [(i + 1, 'Synthetic document', SimpleNamespace(left=i * 40, top=0), False)
                       for i in range(15)]
            if args.real_enumerator:
                real = timed('native enumeration', lambda: cube._get_all_windows(set()))
                real_count = len(real)
                timed('native rows incl names', lambda: cube._build_rows(app))
            else:
                real_count = None
            post = lambda cb: QTimer.singleShot(0, qt, cb)

            def pump():
                # Include initial paint; drain deferred deletes between repeated shows.
                for _ in range(4):
                    qt.processEvents()

            with patch.object(ui.qt_runtime, 'post', post), patch.object(ui.qt_runtime, 'ensure_started', lambda: None), \
                    patch.object(cube, '_get_all_windows', lambda exclude: windows), \
                    patch.object(cube, '_get_pid', lambda hwnd: os.getpid()), patch.object(cube, '_own_hwnd', lambda app: set()):
                for repeat in ('cold', 'warm'):
                    timed(f'cube {repeat} exact route', lambda: executor._matcher.match('show cube'))
                    delay = policy.cancel_window_for('show cube', app)
                    measurements.append((f'cube {repeat} policy delay', delay * 1000))
                    timed(f'cube {repeat} build 15 rows', lambda: cube._build_rows(app))
                    start = perf_counter()
                    cube.handle_show_cube(app, '')
                    returned = perf_counter()
                    pump()
                    assert ui.get_panel()._window.isVisible()
                    measurements.extend([(f'cube {repeat} handler return', (returned - start) * 1000),
                                         (f'cube {repeat} route-to-painted', (perf_counter() - start) * 1000)])
                    ui.get_panel()._window.hide()
                sheet = cs.CommandCheatSheetQt(commands_cb=executor._matcher.list_commands,
                                              palette_path=Path(profile) / 'palette.json', config_cb=lambda: {})
                for repeat in ('cold', 'warm'):
                    timed(f'reference {repeat} exact route', lambda: executor._matcher.match('show commands'))
                    measurements.append((f'reference {repeat} policy delay', policy.cancel_window_for('show commands', app) * 1000))
                    start = perf_counter()
                    sheet.show()
                    pump()
                    assert sheet._window.isVisible()
                    measurements.append((f'reference {repeat} route-to-painted', (perf_counter() - start) * 1000))
                    sheet._window.hide()
                sheet._window.deleteLater()
                ui.get_panel()._window.deleteLater()
                pump()
        print('stage                                           ms')
        for stage, ms in measurements:
            print(f'{stage:44} {ms:9.3f}')
        if real_count is not None:
            print(f'Native switchable windows: {real_count} (titles suppressed)')
        print('Speech-end -> transcript -> route: unmeasured (requires owner voice session).')
        print('Policy delays are computed, not slept; Qt is offscreen, not a native display proof.')
        print(json.dumps(dict(measurements), sort_keys=True))
        logging.shutdown()


if __name__ == '__main__':
    main()
