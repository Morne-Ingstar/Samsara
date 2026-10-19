"""History's deferred layout work must not outlive its native window."""
import os
from pathlib import Path
import subprocess
import sys


def _exercise_history_lifetime():
    import gc
    import time
    import types
    from datetime import datetime

    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtWidgets import QApplication
    from samsara.ui import history_view as hv, main_window_qt

    class Settings:
        def __init__(self, *_args):
            pass

        def value(self, _key, default=None, **_kwargs):
            return default

        def setValue(self, *_args):
            pass

    hv.QSettings = Settings
    errors = []
    def capture_error(kind, value, tb):
        errors.append(f'{kind.__name__}: {value}')
        sys.__excepthook__(kind, value, tb)
    sys.excepthook = capture_error
    qapp = QApplication([])
    for visit in range(12):
        app = types.SimpleNamespace(config={}, history=[
            (datetime.now().isoformat(), 'Synthetic history entry ' * 30, False),
        ])
        win = main_window_qt._MainWindow(app)
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        win.resize(1024, 700)
        win.show()
        win._activate('History')
        view = win._panel_cache['History']
        deadline = time.monotonic() + 5
        while not view._rows_by_item_id and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert view._rows_by_item_id, 'History did not load'
        win.resize(700, 500)
        qapp.processEvents()
        # Queue both viewport relayout and detail-card sizing, then destroy
        # their owner before the next event-loop turn can deliver either.
        view._schedule_relayout()
        for i in range(view._list.count()):
            if view._list.item(i).data(Qt.ItemDataRole.UserRole):
                view._list.setCurrentRow(i)
                break
        win.hide()
        win.deleteLater()
        QCoreApplication.sendPostedEvents(win, QEvent.Type.DeferredDelete)
        del view, win, app
        gc.collect()
        qapp.processEvents()
        assert not errors, '\n'.join(errors)
    print('12 History visits: default/narrow, queued layout, deletion, collection: clean')


def test_history_narrow_layout_exits_cleanly_in_fresh_process(tmp_path):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / 'profile'
    home.mkdir()
    env = dict(os.environ)
    for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
                'SAMSARA_HOME_DIR', 'TEMP', 'TMP', 'TMPDIR'):
        env[key] = str(home)
    env.update(QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=str(root))
    for key in tuple(env):
        if key.startswith('SAMSARA_TEST_SHARD_'):
            del env[key]
    result = subprocess.run(
        [sys.executable, '-X', 'faulthandler', str(Path(__file__).resolve()), '--history-child'],
        cwd=root, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '12 History visits:' in result.stdout


if __name__ == '__main__' and '--history-child' in sys.argv:
    _exercise_history_lifetime()
