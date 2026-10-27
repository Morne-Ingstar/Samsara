"""Settings opens without waiting for hardware or unvisited destinations."""
from types import SimpleNamespace
from time import perf_counter
from unittest.mock import Mock
import threading

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QWidget

from samsara import cuda_detect
from samsara.runtime import thread_registry
from samsara.ui import settings_qt
from tests.test_settings_pages import _StubApp, _schema_defaults


@pytest.fixture(autouse=True)
def no_registry_changes(monkeypatch, qapp):
    from samsara import autostart
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **kw: None)
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.fixture
def window(qapp, monkeypatch):
    from samsara.ui.first_run_qt import ComponentsPage
    callbacks, jobs = [], []
    monkeypatch.setattr(cuda_detect, 'request_cuda_status', callbacks.append)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda *a: None)
    monkeypatch.setattr(thread_registry, 'spawn', lambda name, fn, **kw: jobs.append((name, fn)))
    config = _schema_defaults()
    config['tts']['network_voice_accepted'] = True
    win = settings_qt._SettingsWindow(_StubApp(config))
    win._test_cuda_callbacks, win._test_jobs = callbacks, jobs
    yield win
    win.hide()
    win.deleteLater()
    qapp.processEvents()


def test_construction_budget_and_no_unvisited_page_build(qapp, monkeypatch):
    # Keep the gate's shell budget independent of font/backend instrumentation.
    # The real page builders are exercised by the visit tests and profiler.
    def source(owner):
        return QWidget()
    def first_page(owner):
        page = QWidget()
        owner._pages['General'] = page
        return page
    for name in ('general', 'modes', 'advanced'):
        monkeypatch.setattr(settings_qt._SettingsWindow,
                            settings_qt._SettingsWindow._SOURCE_BUILDERS[name], source)
    monkeypatch.setattr(settings_qt.general_page, 'build', first_page)
    monkeypatch.setattr(settings_qt._SettingsWindow, '_prepare_actions', lambda *a: None)
    monkeypatch.setattr(settings_qt.theme, 'apply_design_scale', lambda *a: None)
    monkeypatch.setattr(cuda_detect, 'request_cuda_status', lambda callback: None)
    called = []
    for module in settings_qt.PAGE_MODULES[1:]:
        monkeypatch.setattr(module, 'build', lambda owner: pytest.fail('unvisited page built'))
    monkeypatch.setattr(settings_qt.tools_page, 'build', lambda *a: pytest.fail('unvisited tool built'))
    monkeypatch.setattr(cuda_detect, 'is_cuda_available', lambda: pytest.fail('UI probed CUDA'))
    started = perf_counter()
    win = settings_qt._SettingsWindow(_StubApp(_schema_defaults()))
    win.show()
    QTimer.singleShot(0, lambda: called.append('responsive'))
    qapp.processEvents()
    elapsed = perf_counter() - started
    try:
        assert elapsed < 2.0, elapsed
        assert called == ['responsive']
        assert set(win._pages) == {'General'}
        assert set(win._source_pages) == {'general', 'modes', 'advanced'}
        assert win._tool_pages == {}
    finally:
        win.hide()
        win.deleteLater()


def test_visit_materializes_only_requested_destination_once(window, monkeypatch):
    module = settings_qt.dictation_page
    original, calls = module.build, []
    def build(owner):
        calls.append(True)
        return original(owner)
    monkeypatch.setattr(module, 'build', build)
    window.show_tab('Dictation')
    window.show_tab('General')
    window.show_tab('Dictation')
    assert calls == [True]
    assert set(window._pages) == {'General', 'Dictation'}
    assert window._stack.currentWidget() is window._pages['Dictation']


def test_each_tool_is_lazy_and_keeps_independent_controls(window):
    window._select_tool('Health')
    assert set(window._tool_pages) == {'Health'}
    assert 'music' not in window._source_pages
    assert 'commands' not in window._source_pages
    window._select_tool('Integrations')
    integration = window._tool_pages['Integrations']
    window._select_tool('Command library')
    assert window._tool_pages['Integrations'] is integration
    assert integration.isAncestorOf(window._widgets['todoist_enabled'])


def test_unvisited_integration_does_not_appear_in_command_library(window):
    window._select_tool('Command library')
    assert set(window._tool_pages) == {'Command library'}
    library = window._tool_pages['Command library']
    assert not library.isAncestorOf(window._widgets['todoist_enabled'])


def test_apply_collection_includes_unopened_preferences(window):
    window._widgets['auto_capitalize'].setChecked(False)
    updates = window._collect_destination_updates({})
    assert updates['auto_capitalize'] is False
    assert 'tts' in updates
    assert 'cloud_llm' in updates
    assert set(window._pages) == set(settings_qt._TAB_NAMES)
    assert 'tts' in window._source_pages


def test_late_page_uses_visit_snapshot_and_apply_preserves_outside_changes(window):
    opened = window._config_at_open['tts']['feedback_level']
    window.app.config['tts'].update(feedback_level='silent', enabled=False)
    window._widgets['auto_capitalize'].setChecked(False)
    window._collect_destination_updates({})
    assert window._widgets['tts_feedback_level'].currentData() == opened
    window._apply_and_close()
    assert window.app.config['tts']['feedback_level'] == 'silent'
    assert window.app.config['tts']['enabled'] is False


def test_source_snapshot_never_replaces_live_config_for_workers():
    from samsara.ui.settings.lazy_access import source_config
    live_config = {'value': 'outside'}
    app = SimpleNamespace(config=live_config)
    owner = SimpleNamespace(app=app, _controls_at_open={'value': 'opened'})
    observed = []
    with source_config(owner):
        captured = owner.app
        assert captured.config == {'value': 'opened'}
        assert app.config is live_config
        worker = threading.Thread(target=lambda: observed.append(captured.config))
        worker.start()
        worker.join(timeout=2)
        assert observed == [live_config]
        assert observed[0] is live_config
    assert owner.app is app
    assert captured.config is live_config


def test_cuda_result_is_queued_and_old_visit_cannot_mutate_new_controls(window, qapp):
    old_label = window._widgets['cuda_status']
    deliver = window._test_cuda_callbacks[0]
    worker = threading.Thread(target=lambda: deliver(True, 'GPU ready'))
    worker.start()
    worker.join(timeout=2)
    assert old_label.text() == 'checking...'
    qapp.processEvents()
    assert old_label.text() == 'GPU ready'
    assert 'CUDA (NVIDIA GPU)' in window._widgets['adv_device_map']
    window.show()
    window.close()
    window.show()
    new_label = window._widgets['cuda_status']
    deliver(False, 'stale result')
    qapp.processEvents()
    assert new_label is not old_label
    assert new_label.text() == 'checking...'


def test_pending_cuda_does_not_change_saved_device(window):
    # The configured CUDA selection remains available while detection is pending.
    window.app.config['device'] = 'cuda'
    window.show()
    window.close()
    window.show()
    assert not window._widgets['adv_device'].isEnabled()
    assert window._collect_destination_updates({})['device'] == 'cuda'


@pytest.mark.parametrize('saved', ['auto', 'cuda'])
@pytest.mark.parametrize('available', [False, True])
def test_saved_device_survives_probe_and_unrelated_apply(window, qapp, monkeypatch, saved, available):
    # 940: incidental CPU coercion opened a modal and blocked the next visit.
    from samsara.ui import speech_hardware_qt
    notice = Mock()
    monkeypatch.setattr(speech_hardware_qt, '_notice', notice)
    window.app.config['device'] = saved
    window.show()
    window.close()
    window.show()
    combo = window._widgets['adv_device']
    window._test_cuda_callbacks[-1](available, 'Synthetic GPU probe')
    qapp.processEvents()
    assert window._collect_destination_updates({})['device'] == saved
    window._apply_and_close()
    assert window.app.config['device'] == saved
    notice.assert_not_called()
    window.show()
    combo = window._widgets['adv_device']
    combo.setCurrentText('CPU')
    assert window._collect_destination_updates({})['device'] == 'cpu'


def test_voice_enumeration_waits_for_worker_and_delivers_on_ui(window, qapp, monkeypatch):
    calls = []
    voice = SimpleNamespace(voice_id='local-test', display_name='Local', language='en-US')
    monkeypatch.setattr(window, '_tts_voices_for', lambda *a: calls.append(threading.get_ident()) or [voice])
    window.show_tab('Audio')
    combo = window._widgets['tts_voice_combo']
    assert calls == []
    assert combo.currentText() == 'checking...'
    jobs = [fn for name, fn in window._test_jobs if name == 'settings-voice-catalog']
    worker = threading.Thread(target=jobs[0])
    worker.start()
    worker.join(timeout=2)
    assert calls and calls[0] != threading.get_ident()
    assert combo.currentText() == 'checking...'
    qapp.processEvents()
    assert combo.findData('local-test') >= 0
    assert combo.isEnabled()


def test_voice_results_from_previous_provider_are_discarded(window, qapp, monkeypatch):
    monkeypatch.setattr(window, '_tts_voices_for', lambda provider, accepted: [
        SimpleNamespace(voice_id=provider, display_name=provider, language='en-US')])
    window.show_tab('Audio')
    window._widgets['tts_engine'].setCurrentText('edge')
    jobs = [fn for name, fn in window._test_jobs if name == 'settings-voice-catalog']
    jobs[-1]()
    jobs[0]()
    qapp.processEvents()
    combo = window._widgets['tts_voice_combo']
    assert combo.findData('edge') >= 0
    assert combo.findData('winrt') < 0


def test_outside_engine_change_cannot_reuse_old_provider_voice(window, monkeypatch):
    window.show_tab('Audio')
    window._config_at_open['tts']['voice_id'] = 'local-1'
    window.app.config['tts'].update(engine='edge', voice_id='local-1')
    monkeypatch.setattr(window, '_tts_voices_for', lambda *a: pytest.fail('Apply enumerated voices on UI'))
    assert window._validate_tts_voice({'engine': 'edge', 'voice_id': 'local-1'})['voice_id'] is None
    assert window._validate_tts_voice({'engine': 'edge', 'voice_id': 'local-2'})['voice_id'] is None
    window.app.config['tts']['voice_id'] = 'edge-1'
    assert window._validate_tts_voice({'engine': 'edge', 'voice_id': 'edge-1'})['voice_id'] == 'edge-1'


def test_search_finds_unvisited_page_and_inventory_keeps_all_editors(window):
    window._search_edit.setText('stage timing')
    window._apply_search_filter()
    assert window._stack.currentWidget() is window._pages['Help']
    inventory = window.config_row_inventory()
    assert any(key == 'tts.voice_id' for key, page, row in inventory)
    assert any(key == 'music_library' for key, page, row in inventory)


def test_every_destination_is_complete_when_visited(window):
    for index, module in enumerate(settings_qt.PAGE_MODULES):
        page = window._ensure_destination(index)
        assert window._stack.widget(index) is page
        for spec in module.BASE + module.ADVANCED:
            if spec[3].startswith('echo_cancellation.'):
                continue
            for key in filter(None, spec[2].split(',')):
                assert key in window._rows_by_widget, (module.TITLE, key)


def test_slow_cuda_worker_does_not_hold_ui_thread(qapp, monkeypatch):
    started, release, threads, results, heartbeat = threading.Event(), threading.Event(), [], [], []
    monkeypatch.setattr(cuda_detect, '_session_status', None)
    monkeypatch.setattr(cuda_detect, '_pending_callbacks', [])
    monkeypatch.setattr(cuda_detect, '_probe_running', False)
    def slow_probe():
        started.set()
        assert release.wait(timeout=3)
        return False
    def spawn(name, fn, **kw):
        worker = threading.Thread(target=fn)
        threads.append(worker)
        worker.start()
    monkeypatch.setattr(cuda_detect, 'is_cuda_available', slow_probe)
    monkeypatch.setattr(cuda_detect, 'cuda_status_message', lambda: 'ready')
    monkeypatch.setattr(thread_registry, 'spawn', spawn)
    try:
        before = perf_counter()
        cuda_detect.request_cuda_status(lambda *result: results.append(result))
        assert perf_counter() - before < 1
        assert started.wait(timeout=1)
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qapp.processEvents()
        assert heartbeat == [True]
        assert results == []
    finally:
        release.set()
        for worker in threads:
            worker.join(timeout=3)
    assert results == [(False, 'ready')]


def test_cuda_requests_share_one_worker_and_cache_result(monkeypatch):
    jobs, results, probes = [], [], []
    monkeypatch.setattr(cuda_detect, '_session_status', None)
    monkeypatch.setattr(cuda_detect, '_pending_callbacks', [])
    monkeypatch.setattr(cuda_detect, '_probe_running', False)
    monkeypatch.setattr(cuda_detect, 'is_cuda_available', lambda: probes.append(True) or True)
    monkeypatch.setattr(cuda_detect, 'cuda_status_message', lambda: 'ready')
    monkeypatch.setattr(thread_registry, 'spawn', lambda name, fn, **kw: jobs.append(fn))
    cuda_detect.request_cuda_status(lambda *result: results.append(result))
    cuda_detect.request_cuda_status(lambda *result: results.append(result))
    assert probes == []
    assert len(jobs) == 1
    jobs[0]()
    cuda_detect.request_cuda_status(lambda *result: results.append(result))
    assert results == [(True, 'ready')] * 3
    assert len(probes) == len(jobs) == 1


def test_cuda_probe_failure_finishes_pending_callbacks(monkeypatch):
    jobs, results = [], []
    monkeypatch.setattr(cuda_detect, '_session_status', None)
    monkeypatch.setattr(cuda_detect, '_pending_callbacks', [])
    monkeypatch.setattr(cuda_detect, '_probe_running', False)
    monkeypatch.setattr(thread_registry, 'spawn', lambda name, fn, **kw: jobs.append(fn))
    def fail():
        raise OSError('unavailable backend')
    monkeypatch.setattr(cuda_detect, 'is_cuda_available', fail)
    cuda_detect.request_cuda_status(lambda *result: results.append(result))
    jobs[0]()
    assert results == [(False, 'CUDA detection unavailable.')]
    assert cuda_detect._probe_running is False


def test_cuda_path_discovery_does_not_import_native_backend(monkeypatch, tmp_path):
    import builtins
    import sys
    original = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        assert name != 'ctranslate2', 'discovery must not load the native backend'
        return original(name, *args, **kwargs)
    monkeypatch.delitem(sys.modules, 'ctranslate2', raising=False)
    monkeypatch.setattr(cuda_detect, 'find_spec', lambda name: SimpleNamespace(origin=str(tmp_path / '__init__.py')))
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    assert tmp_path in cuda_detect._probable_search_paths()
