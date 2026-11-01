"""First-launch transfers overlap setup, without audio or application bootstrap."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect
from PySide6.QtWidgets import QApplication

from samsara import setup_model_download as download
from samsara.ui import first_run_pages_qt as pages, first_run_wizard_qt as wizard


class Scheduler:
    def __init__(self):
        self.pending = []

    def __call__(self, name, target, *, args=(), daemon=True):
        self.pending.append(lambda: target(*args))

    def run(self):
        self.pending.pop(0)()


def test_download_starts_without_blocking_and_same_model_is_not_repeated():
    scheduler, calls = Scheduler(), []
    def fetch(model, **options):
        calls.append((model, options))
        if options.get('local_files_only'):
            raise FileNotFoundError('Synthetic empty cache')
        assert manager.snapshot(model) == 'downloading'
        return '/synthetic/cache'
    manager = download.SetupModelDownload(downloader=fetch, spawn=scheduler)
    job = manager.request('base')
    assert manager.snapshot('base') == 'checking'
    assert not job.done.is_set()  # Request returned while the worker is pending.
    assert manager.request('base') is job
    assert len(scheduler.pending) == 1
    scheduler.run()
    manager.wait('base')
    assert manager.snapshot('base') == 'downloaded'
    assert calls == [('base', {'local_files_only': True}), ('base', {})]


def test_cached_model_uses_no_network():
    scheduler, fetch = Scheduler(), Mock(return_value='/synthetic/cache')
    manager = download.SetupModelDownload(downloader=fetch, spawn=scheduler)
    manager.request('base')
    scheduler.run()
    fetch.assert_called_once_with('base', local_files_only=True)
    assert manager.snapshot('base') == 'downloaded'


@pytest.mark.parametrize('fail_start', [False, True])
def test_failed_prefetch_unblocks_normal_startup_retry(fail_start):
    scheduler = Scheduler()
    def fail(*a, **k):
        raise OSError('Synthetic offline failure')
    manager = download.SetupModelDownload(downloader=fail, spawn=fail if fail_start else scheduler)
    job = manager.request('tiny')
    if not fail_start:
        scheduler.run()
    app = SimpleNamespace(_setup_model_download=manager)
    download.wait_for_setup_download(app, 'tiny')
    assert job.done.is_set()
    assert manager.snapshot('tiny') == 'failed'
    assert 'retry after setup' in download.progress_text('tiny', 'failed')


def test_startup_waits_for_selected_model_only():
    manager = download.SetupModelDownload(spawn=Scheduler())
    base = manager.request('base')
    tiny = manager.request('tiny')
    base.done = Mock()
    tiny.done = Mock()
    app = SimpleNamespace(_setup_model_download=manager)
    download.wait_for_setup_download(app, 'tiny')
    tiny.done.wait.assert_called_once_with()
    base.done.wait.assert_not_called()
    assert app._speech_model_phase == 'downloading'
    download.wait_for_setup_download(app, 'small')  # No transfer to await.
    download.wait_for_setup_download(SimpleNamespace(), 'base')  # Established profile.


def test_production_load_seam_waits_before_model_construction(monkeypatch):
    # Execute the actual startup seam, without importing dictation or its bootstrap.
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8'))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'load_model_async')
    worker = next(n for n in method.body if isinstance(n, ast.FunctionDef) and n.name == 'load')
    statements = worker.body[0].body
    start = next(i for i, n in enumerate(statements)
                 if isinstance(n, ast.ImportFrom) and n.module == 'samsara.gpu_fallback')
    end = next(i for i, n in enumerate(statements[start:], start)
               if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
               and isinstance(n.value.func, ast.Name) and n.value.func.id == 'load_resilient_model')
    fragment = ast.Module(body=statements[start:end + 1], type_ignores=[])
    calls = []
    manager = SimpleNamespace(snapshot=lambda _: 'downloading', wait=lambda name: calls.append(('wait', name)))
    app = SimpleNamespace(_setup_model_download=manager, config={'model_size': 'tiny'}, update_splash=Mock())
    from samsara import gpu_fallback
    def load(*a):
        calls.append(('construct', a[3]))
        return 'synthetic model', 0., 'cpu', 'int8'
    monkeypatch.setattr(gpu_fallback, 'load_resilient_model', load)
    exec(compile(fragment, '<startup-model-seam>', 'exec'),
         dict(self=app, _create_whisper_model=Mock(), device='cpu', compute_type='int8'))
    assert calls == [('wait', 'tiny'), ('construct', 'tiny')]
    assert app.model == 'synthetic model'


@pytest.fixture
def setup(qapp, tmp_path, monkeypatch):
    scheduler = Scheduler()
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(wizard, 'capture_endpoints', lambda: ())
    monkeypatch.setattr(wizard, 'register_notifications', lambda *a: None)
    monkeypatch.setattr(wizard._WizardWindow, '_start_meter', lambda self: None)
    monkeypatch.setattr(pages, 'cuda_chosen', lambda _: False)
    monkeypatch.setattr(pages.autostart, 'sync_run_entry', Mock())
    import sounddevice as sd
    monkeypatch.setattr(sd, 'query_devices', lambda *a, **k: {'name': 'Synthetic microphone'})
    monkeypatch.setattr(sd, 'default', SimpleNamespace(device=(7, -1)), raising=False)
    available = QRect(0, 0, 1366, 728)
    screen = SimpleNamespace(availableGeometry=lambda: available, devicePixelRatio=lambda: 1.)
    monkeypatch.setattr(QApplication, 'primaryScreen', lambda: screen)
    monkeypatch.setattr(wizard._WizardWindow, 'screen', lambda self: screen)
    windows = []
    def make(saved=None, loaded=False):
        path = tmp_path / f'config{len(windows)}.json'
        if saved is not None:
            path.write_text(json.dumps(saved), encoding='utf-8')
        manager = download.SetupModelDownload(spawn=scheduler, downloader=Mock())
        app = SimpleNamespace(_setup_model_download=manager, model_loaded=loaded)
        win = wizard._WizardWindow(path, app)
        windows.append(win)
        win._mics = [{'id': 7, 'name': 'Synthetic microphone'}]
        win._mic_result.emit('_load_done_', '')
        win.show()
        qapp.processEvents()
        return win, manager, path
    yield make, scheduler, available
    for win in windows:
        win.close()
        win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_first_window_starts_download_and_all_five_stages_keep_status_visible(setup, qapp):
    make, scheduler, available = setup
    win, manager, _ = make()
    assert len(wizard._STEPS) == 5
    assert 'base' in manager.jobs  # Already preparing on Welcome, before any Next.
    assert len(scheduler.pending) == 1
    for index in range(5):
        win._step = index
        win._show_step()
        # Page swaps post fit/layout work; measure after those events settle.
        for _ in range(8):
            qapp.processEvents()
        assert win._download_status.isVisible()
        assert 'Checking speech model' in win._download_status.text()
        assert available.contains(win.frameGeometry())
        for button in (win._back_btn, win._next_btn, win._tutorial_btn):
            if button.isVisible():
                assert win.rect().contains(QRect(button.mapTo(win, QPoint()), button.size()))


def test_model_changes_and_hardware_recommendation_prepare_selected_size(setup):
    make, scheduler, _ = setup
    win, manager, _ = make()
    win._decisions.receive_facts({'cpu_cores': 2, 'ram_gb': 4, 'gpu_usable': False})
    assert 'tiny' in manager.jobs
    next(b for b in win._model_group.buttons() if b.property('_value') == 'small').click()
    assert win._config['model_size'] == 'small'
    assert 'small' in manager.jobs
    assert '(small)' in win._download_status.text()
    assert len(scheduler.pending) == 3
    win._decisions.receive_facts({'cpu_cores': 2})
    assert win._config['model_size'] == 'small'  # Explicit choice wins.


@pytest.mark.parametrize('phase,substring', [('downloading', 'Downloading'), ('downloaded', 'loads when'), ('failed', 'retry')])
def test_truthful_transfer_status_never_claims_loaded(setup, phase, substring):
    make, _, _ = setup
    win, manager, _ = make()
    manager.jobs['base'].phase = phase
    win._decisions.refresh_readiness()
    assert substring in win._download_status.text()
    assert 'model is loaded' not in win._download_status.text()
    assert win._next_btn.isEnabled()  # Failure does not trap setup.


def test_cancellation_preserves_saved_preferences_and_completion_state(setup):
    make, _, _ = setup
    saved = {'first_run_complete': False, 'model_size': 'tiny',
             'microphone': 12, 'microphone_name': 'Preferred headset'}
    win, manager, path = make(saved)
    assert list(manager.jobs) == ['tiny']
    win.close()
    assert win.result is None
    assert json.loads(path.read_text(encoding='utf-8')) == saved


def test_existing_loaded_session_does_not_download_on_setup_rerun(setup):
    make, scheduler, _ = setup
    win, manager, _ = make(loaded=True)
    assert not manager.jobs and not scheduler.pending
    assert not win._download_status.isVisible()
