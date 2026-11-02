"""Received bytes, shared transfers and UI retry, with no Hub or app bootstrap."""
import ast
import logging
from pathlib import Path
import socket
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import model_load_feedback, setup_model_download as download, speech_model_transfer as transfer
from samsara import speech_readiness
from tests.test_shorter_setup_935i import Scheduler, setup  # noqa: F401


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail('1007 tests must never touch the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture
def hub(monkeypatch, tmp_path):
    state = SimpleNamespace(calls=[], cached={'config.json', 'tokenizer.json'},
                            initial=0, fail=False, bars=[], offsets=[], complete=False)
    sizes = {'config.json': 100, 'tokenizer.json': 200, 'model.bin': 10_000_000}
    utils = ModuleType('faster_whisper.utils')
    utils._MODELS = {'base': 'Systran/faster-whisper-base'}
    utils.download_model = Mock(return_value=str(tmp_path))
    fw = ModuleType('faster_whisper')
    fw.utils = utils
    monkeypatch.setitem(sys.modules, 'faster_whisper', fw)
    monkeypatch.setitem(sys.modules, 'faster_whisper.utils', utils)
    client = ModuleType('huggingface_hub')
    client.HfApi = lambda: SimpleNamespace(model_info=lambda repo, **kw: SimpleNamespace(
        sha='fixed-commit', siblings=[SimpleNamespace(rfilename=n, size=s) for n, s in sizes.items()]
        + [SimpleNamespace(rfilename='README.md', size=99999)]))

    def fetch(repo, name, *, revision, local_files_only=False, tqdm_class=None):
        assert repo == 'Systran/faster-whisper-base' and revision == 'fixed-commit'
        state.calls.append((name, local_files_only))
        if local_files_only:
            if name not in state.cached:
                raise FileNotFoundError('synthetic cache miss')
        else:
            state.offsets.append(state.initial)
            with tqdm_class(total=sizes[name], initial=state.initial, unit='B', disable=True) as bar:
                state.bars.append(bar)
                # The mocked incomplete file remains empty during updates.
                partial = tmp_path / 'blob.incomplete'
                partial.touch()
                for count in (1_000_000, 2_000_000):
                    bar.update(count)
                    assert partial.stat().st_size == 0
                if state.fail:
                    state.initial = bar.n
                    raise OSError('synthetic interrupted download')
                bar.update(sizes[name] - bar.n)
            state.cached.add(name)
            state.complete = True
        return str(tmp_path / name)

    client.hf_hub_download = fetch
    monkeypatch.setitem(sys.modules, 'huggingface_hub', client)
    return state, sizes, tmp_path


def test_received_bytes_have_fixed_total_monotonic_percent_and_eta(hub, monkeypatch):
    state, sizes, path = hub
    now = iter(range(100))
    monkeypatch.setattr(transfer.time, 'monotonic', lambda: next(now))
    events = []
    assert transfer.fetch_model('base', progress=events.append) == str(path)
    assert events[0].downloaded == 300  # Cached small files credited up front.
    assert {v.total for v in events} == {sum(sizes.values())}
    assert [v.downloaded for v in events] == sorted(v.downloaded for v in events)
    assert events[-1].downloaded == events[-1].total
    assert any(v.eta is not None and v.eta > 0 for v in events)
    assert ('README.md', False) not in state.calls
    assert [n for n, local in state.calls if not local] == ['model.bin']
    active = next(v for v in events if v.eta is not None and v.eta > 0)
    assert 'MB (' in download.byte_text(active) and 'left' in download.byte_text(active)


def test_interruption_and_retry_retains_hub_resume_and_cached_files(hub):
    state, _, _ = hub
    state.fail = True
    with pytest.raises(OSError, match='interrupted'):
        transfer.fetch_model('base')
    assert state.initial == 3_000_000
    state.fail = False
    events = []
    transfer.fetch_model('base', progress=events.append)
    assert state.offsets == [0, 3_000_000]
    assert events[-1].downloaded == events[-1].total
    assert not any(n != 'model.bin' for n, local in state.calls if not local)


def test_cache_lookup_rejects_unfinished_snapshot_without_network(hub):
    _, _, path = hub
    with pytest.raises(FileNotFoundError, match='incomplete'):
        transfer.fetch_model('base', local_files_only=True)
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (path / name).touch()
    assert transfer.fetch_model('base', local_files_only=True) == str(path)


def test_cached_files_need_no_transfer(hub):
    state, sizes, _ = hub
    state.cached.update(sizes)
    events = []
    transfer.fetch_model('base', progress=events.append)
    assert not state.bars
    assert events[0].downloaded == events[0].total


def test_threaded_requests_join_one_job_and_listener_can_read_details():
    started, release = threading.Event(), threading.Event()
    calls, threads = [], []
    def fetch(model, **kwargs):
        if kwargs:
            raise FileNotFoundError('synthetic cache miss')
        calls.append(model)
        started.set()
        assert release.wait(2)
        return '/cache/base'
    def spawn(name, target, *, args, daemon):
        thread = threading.Thread(target=target, args=args, daemon=daemon)
        threads.append(thread)
        thread.start()
    manager = download.SetupModelDownload(downloader=fetch, spawn=spawn)
    job = manager.request('base')
    try:
        assert started.wait(2)
        assert manager.request('base') is manager.request('base', retry=True) is job
        values = []
        stop = manager.subscribe('base', lambda j: values.append(manager.details('base')))
        release.set()
        manager.wait('base')
        stop()
        assert calls == ['base'] and len(threads) == 1
        assert values[-1][0] == 'downloaded'
    finally:
        release.set()
        for thread in threads:
            thread.join(2)


def test_failure_requires_retry_and_only_one_retry_is_created():
    scheduler = Scheduler()
    manager = download.SetupModelDownload(downloader=Mock(side_effect=OSError('offline')), spawn=scheduler)
    old = manager.request('base')
    scheduler.run()
    assert old.done.is_set() and old.phase == 'failed'
    assert manager.request('base') is old
    new = manager.request('base', retry=True)
    assert new is not old and manager.request('base', retry=True) is new
    assert len(scheduler.pending) == 1


def test_real_startup_error_seam_keeps_shell_for_download_failure():
    manager = download.SetupModelDownload(spawn=Scheduler())
    job = manager.request('base')
    job.phase = 'failed'
    app = SimpleNamespace(_setup_model_download=manager, config={'model_size': 'base'},
                          _speech_model_phase='failed', _close_splash_post_load=Mock(),
                          show_main_window=Mock(), quit_app=Mock())
    source = Path(__file__).resolve().parents[1].joinpath('dictation.py').read_text(encoding='utf-8')
    fn = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == '_show_startup_error')
    scope = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<startup-error>', 'exec'), scope)
    scope['_show_startup_error'](app, 'synthetic offline')
    app._close_splash_post_load.assert_called_once_with()
    app.show_main_window.assert_called_once_with()
    app.quit_app.assert_not_called()
    job.phase = 'downloaded'
    assert not download.keep_download_retry_available(app)  # Other load errors retain the original policy.


def test_selected_model_starts_and_setup_retry_never_blocks_finish(setup):
    make, scheduler, _ = setup
    win, manager, _ = make()
    next(b for b in win._model_group.buttons() if b.property('_value') == 'small').click()
    assert 'small' in manager.jobs
    job = manager.jobs['small']
    job.phase, job.progress = 'downloading', transfer.Progress(72_000_000, 145_000_000, 52)
    win._decisions.refresh_readiness()
    assert '72.0 / 145.0 MB (49%)' in win._download_status.text()
    assert 'about 52s left' in win._download_status.text()
    assert not win._download_retry.isVisible()
    job.phase = 'failed'
    job.done.set()
    win._decisions.refresh_readiness()
    assert win._download_retry.isVisible() and win._next_btn.isEnabled()
    before = len(scheduler.pending)
    win._download_retry.click()
    assert len(scheduler.pending) == before + 1
    assert manager.jobs['small'] is not job
    assert not win._download_retry.isVisible()


@pytest.mark.parametrize('prefix', ['', 'Warning: '])
def test_warnings_demoted_without_hiding_other_warnings(prefix):
    transfer.quiet_hub_advice()
    for name, message in (
        ('huggingface_hub.utils._http', 'You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN'),
        ('huggingface_hub.file_download', "Xet Storage is enabled for this repo, but the 'hf_xet' package is not installed. Falling back to regular HTTP download."),
    ):
        log = logging.getLogger(name)
        record = logging.LogRecord(name, logging.WARNING, __file__, 1, prefix + message, (), None)
        assert log.filter(record)
        assert record.levelno == logging.DEBUG and record.levelname == 'DEBUG'
        other = logging.LogRecord(name, logging.WARNING, __file__, 1, 'Server unreachable', (), None)
        assert log.filter(other) and other.levelno == logging.WARNING


def test_production_load_resolves_joined_transfer_then_constructs_locally(monkeypatch):
    path = '/synthetic/snapshot'
    app = SimpleNamespace(model_loaded=False, config={'model_size': 'base'}, update_splash=Mock())
    manager = app._setup_model_download = download.SetupModelDownload(spawn=Scheduler())
    job = manager.request('base')
    job.phase, job.path = 'downloaded', path
    job.done.set()
    # Exercise the actual dictation factory without importing its bootstrap.
    source = Path(__file__).resolve().parents[1].joinpath('dictation.py').read_text(encoding='utf-8')
    fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == '_create_whisper_model')
    scope = {'os': SimpleNamespace(path=SimpleNamespace(isdir=lambda _: False))}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<model-factory>', 'exec'), scope)
    fw, utils = ModuleType('faster_whisper'), ModuleType('faster_whisper.utils')
    utils.download_model = Mock(side_effect=FileNotFoundError('empty cache'))
    fw.WhisperModel = Mock(return_value=object())
    monkeypatch.setitem(sys.modules, 'faster_whisper', fw)
    monkeypatch.setitem(sys.modules, 'faster_whisper.utils', utils)
    class App:
        model_loaded = False
        _setup_model_download = manager
        _schedule_ui = None
        def update_splash(self, *a):
            pass
    loaded, _ = model_load_feedback.load_speech_model(App().update_splash, scope['_create_whisper_model'], 'base', 'cpu', 'int8')
    assert loaded is fw.WhisperModel.return_value
    assert fw.WhisperModel.call_args.args[0] == path
    assert fw.WhisperModel.call_args.kwargs['local_files_only'] is True


@pytest.mark.parametrize('display', ['compact', 'full'])
def test_progress_reaches_both_listening_displays(qapp, monkeypatch, qt_widget_scope, display):
    from tests.test_home_live_state_fu02 import BootApp
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui import qt_runtime
    monkeypatch.setattr(qt_runtime, 'post', lambda fn, *args: fn(*args))
    app = BootApp()
    app.config['ui'] = {'listening_display': display, 'live_surface': {'enabled': True}}
    if display == 'compact':
        indicator = ListeningIndicator()
    else:
        controller = LiveSurfaceController(app)
        indicator = LiveSurfaceIndicatorAdapter(controller)
    app.listening_indicator = indicator
    job = download.Download(phase='downloading', progress=transfer.Progress(72_000_000, 145_000_000, 52))
    download.publish_to_app(app, 'base', job)
    message = speech_readiness.status(app)
    assert '72.0 / 145.0 MB (49%)' in message
    if display == 'compact':
        with qt_widget_scope(indicator):
            assert indicator._chip_label == message
    else:
        assert controller.view().notice.message == message


def test_home_progress_and_retry_button_restarts_once(qapp, monkeypatch, qt_widget_scope):
    from samsara.ui import home_qt, home_signals, main_window_qt
    from tests.test_home_qt import _app, _hub, _FakeSettings
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(main_window_qt._MainWindow, '_save_geometry', lambda self: None)
    monkeypatch.setattr(main_window_qt._MainWindow, '_restore_geometry', lambda self: None)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda app: 0)
    monkeypatch.setattr(home_signals, '_diag_records', lambda *a: [])
    monkeypatch.setattr(home_signals, 'ava_state', lambda app: home_signals.CapabilityState(home_signals.AVA, home_signals.OFF))
    monkeypatch.setattr(home_qt, '_user_word_count', lambda app: 0)
    app = _app(model_loaded=False, loading_model=False, _speech_model_phase='downloading',
               _speech_model_progress=transfer.Progress(72_000_000, 145_000_000, 52), load_model_async=Mock())
    with qt_widget_scope(_hub(qapp, monkeypatch, app=app)) as win:
        # 1014: detailed progress belongs in Home's banner; keep the bottom bar short.
        assert '72.0 / 145.0 MB (49%)' in win._panel_cache['Home']._notice_text.text()
        assert win._lbl_mode.text() == 'Downloading speech model'
        app._speech_model_phase = 'failed'
        win._refresh_status()
        assert win._badge.text() == 'Retry' and win._badge.isEnabled()
        win._badge.click()
        win._badge.click()
        app.load_model_async.assert_called_once_with()
        assert app.loading_model and app._speech_model_phase == 'loading'
        app.model_loaded = True
        win._refresh_status()
        assert win._badge.text() != 'Retry' and speech_readiness.status(app) == ''
