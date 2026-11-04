"""Opt-in real decoder-boundary incident evidence (1052a). No live profiles."""
import json
import logging
import threading
import wave
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import hold_recording_recovery as recovery
from samsara.hold_final_policy import FinalAudit, current_audit


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))
    owner = NS(config={'diagnostics': {'keep_incident_holds': True}}, model_rate=16000)
    yield owner
    store = getattr(owner, '_incident_store_1052a', None)
    if store:
        timer = getattr(store, 'expiry', None)
        if timer:
            timer.cancel()
        store.worker.shutdown(wait=True)


def session(app, first=False):
    if not first:
        app._incident_first_1052a = True
    hold = NS(app=app, _decode_audio=np.linspace(-.25, .25, 1607, dtype=np.float32),
              _hold_final_audit=FinalAudit())
    recovery.begin_incident(hold)
    hold._incident_release_1052a = recovery.time.monotonic()
    return hold


def flush(app):
    app._incident_store_1052a.worker.submit(lambda: None).result(timeout=5)


def pairs(app):
    flush(app)
    return sorted(recovery.incident_folder().glob('hold_*.json'))


def test_off_including_first_hold(tmp_path, app):
    app.config['diagnostics'] = {}
    hold = session(app, first=True)
    recovery.complete_incident(hold, 'fixture')
    assert not recovery.incident_folder().exists()
    assert not hasattr(app, '_incident_store_1052a')
    from samsara.config_schema import SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA['diagnostics.keep_incident_holds']['default'] is False
    assert SETTINGS_SCHEMA['diagnostics.incident_holds_max']['default'] == 5


@pytest.mark.parametrize('event,reason', [('[GUARD] streaming final discarded', 'guard_discard'),
    ('[QUALITY] preserving low confidence', 'quality_warning'),
    ('[HOLD_WINDOWS] unresolved start_s=1 end_s=2', 'lost_range'),
    ('[DECODE] hold recovery failed', 'decode_recovery')])
def test_trigger_file(app, caplog, event, reason):
    hold = session(app)
    def final():
        logging.getLogger('Samsara').warning(event)
        recovery.complete_incident(hold, 'fixture')
    with caplog.at_level(logging.INFO, logger='Samsara'):
        recovery.run_incident_final(NS(_session=hold, _final_pass=final))
    paths = pairs(app)
    assert len(paths) == 1
    assert reason in json.loads(paths[0].read_text())['reasons']


def test_first_hold_and_recovery(app):
    hold = session(app, first=True)
    recovery.complete_incident(hold, 'fixture')
    assert json.loads(pairs(app)[0].read_text())['reasons'] == ['first_hold']
    hold = session(app)
    hold._retry_retained_1042 = True
    recovery.complete_incident(hold, '')
    assert any('stopped_recovery' in json.loads(p.read_text())['reasons'] for p in pairs(app))


def test_following_hold_inside_window_and_expired(app, monkeypatch):
    hold = session(app)
    recovery.complete_incident(hold, 'fixture')
    assert pairs(app) == []
    later = session(app)
    assert 'followed_by_hold' in json.loads(pairs(app)[0].read_text())['reasons']
    recovery.complete_incident(later, 'fixture')
    store = app._incident_store_1052a
    store.previous['released'] -= 31
    session(app)
    assert len(pairs(app)) == 1


def test_following_hold_during_previous_final(app):
    hold = session(app)
    session(app)
    recovery.complete_incident(hold, 'fixture')
    assert 'followed_by_hold' in json.loads(pairs(app)[0].read_text())['reasons']


def test_ring_keeps_newest_five_and_clear(app):
    for index in range(9):
        hold = session(app, first=True)
        hold._hold_final_audit.drop('segment_quality')
        recovery.complete_incident(hold, str(index))
        flush(app)
    kept = pairs(app)
    assert len(kept) == 5
    assert all('delivered_text' not in json.loads(p.read_text()) and json.loads(p.read_text())['hold_final']['chars_delivered'] == 1 for p in kept)  # 1121: numbers-only sidecars; newest-slot identity covered by join tests.
    app._incident_store_1052a.clear().result(timeout=5)
    assert list(recovery.incident_folder().iterdir()) == []


def test_native_input_after_trim_and_before_segment_drop(app, monkeypatch):
    from samsara.hallucination_filter import transcribe_without_outro
    import samsara.hallucination_filter as filters
    hold = session(app, first=True)
    audio = np.r_[np.full(16000, .1, np.float32), np.zeros(16000, np.float32)]
    received = []
    def transcribe(pcm, **params):
        received.append(pcm.copy())
        return iter([NS(text='fixture', start=0, end=1, avg_logprob=-.2,
                        compression_ratio=1.1, no_speech_prob=.01, temperature=.2)]), None
    app.model = NS(transcribe=transcribe)
    monkeypatch.setattr(filters, '_outro_is_unsupported', lambda *args: True)
    params = dict(temperature=(0., .2), beam_size=3, condition_on_previous_text=False,
                  without_timestamps=False)
    token = current_audit.set(hold._hold_final_audit)
    try:
        segments, _ = transcribe_without_outro(recovery.incident_model(app), audio, params)
        assert list(segments) == []
    finally:
        current_audit.reset(token)
    recovery.complete_incident(hold, '')
    path = pairs(app)[0]
    metadata = json.loads(path.read_text())
    with wave.open(str(path.with_suffix('.wav')), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() == len(received[0]) == 20800
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), '<i2')
    np.testing.assert_array_equal(pcm, np.rint(received[0] * 32768).astype('<i2'))
    assert metadata['sample_count'] == 20800
    assert metadata['decode_attempts'][0]['segments'][0]['avg_logprob'] == -.2
    assert metadata['decode_attempts'][0]['params']['beam_size'] == 3
    assert metadata['hold_final']['segments_dropped'] == 1
    assert 'decode_retry' in metadata['reasons']


def test_worker_logs_capture_only_safe_metrics(app, caplog):
    hold = session(app)
    def final():
        source = logging.getLogger('Samsara')
        source.info('[GATE] duration_s=1 verdict=pass')
        source.warning('[QUALITY] text must never be copied from logs')
        recovery.complete_incident(hold, 'fixture')
    worker = NS(_session=hold, _final_pass=final)
    with caplog.at_level(logging.INFO, logger='Samsara'):
        recovery.run_incident_final(worker)
    metadata = json.loads(pairs(app)[0].read_text())
    assert metadata['gate_metrics'] == ['[GATE] duration_s=1 verdict=pass']
    assert 'quality_warning' in metadata['reasons']
    assert 'text must never' not in json.dumps(metadata)


def test_disk_write_on_worker_and_clear_invalidates_pending(app, monkeypatch):
    store = recovery._store(app)
    calls = []
    original = wave.open
    def opened(*args, **kwargs):
        calls.append(threading.current_thread().name)
        return original(*args, **kwargs)
    monkeypatch.setattr(wave, 'open', opened)
    hold = session(app, first=True)
    recovery.complete_incident(hold, 'fixture')
    flush(app)
    assert calls and all(name.startswith('incident-holds') for name in calls)
    store.clear().result(timeout=5)
    assert pairs(app) == []


def test_incident_controls(qapp, app, monkeypatch):
    from PySide6.QtWidgets import QLabel, QPushButton
    from PySide6.QtGui import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toLocalFile()))
    widget = recovery.build_incident_controls(app)
    widget.show()
    qapp.processEvents()
    assert any(label.text() == 'Incident holds: 0 kept' for label in widget.findChildren(QLabel))
    buttons = {b.text(): b for b in widget.findChildren(QPushButton)}
    buttons['Open folder'].click()
    assert opened == [str(recovery.incident_folder()).replace('\\', '/')]
    buttons['Clear'].click()
    flush(app)
    widget.close()
    widget.deleteLater()


def test_clear_discards_inflight_final_and_switch_off_blocks_queued(app):
    hold = session(app, first=True)
    store = app._incident_store_1052a
    store.clear().result(timeout=5)
    recovery.complete_incident(hold, 'fixture')
    assert pairs(app) == []
    blocked = threading.Event()
    store.worker.submit(lambda: blocked.wait(5))
    hold = session(app, first=True)
    recovery.complete_incident(hold, 'fixture')
    app.config['diagnostics']['keep_incident_holds'] = False
    blocked.set()
    assert pairs(app) == []


def test_actual_retry_and_audit_drop_trigger(app):
    hold = session(app)
    app.model = NS(transcribe=lambda pcm, **params: (iter([]), None))
    token = current_audit.set(hold._hold_final_audit)
    try:
        model = recovery.incident_model(app)
        for temperature in (0., .2):
            segments, _ = model.transcribe(hold._decode_audio, temperature=temperature)
            list(segments)
    finally:
        current_audit.reset(token)
    hold._hold_final_audit.drop('segment_quality')
    recovery.complete_incident(hold, 'fixture')
    metadata = json.loads(pairs(app)[0].read_text())
    assert 'decode_retry' in metadata['reasons']
    assert 'dropped_segment' in metadata['reasons']
    assert [a['params']['temperature'] for a in metadata['decode_attempts']] == [0., .2]


@pytest.mark.parametrize('lazy_failure', [False, True])
def test_cuda_fallback_archives_effective_native_parameters(app, lazy_failure):
    from samsara.gpu_fallback import ResilientSpeechModel
    hold = session(app)
    received = []
    segment = NS(start=0, end=.1, avg_logprob=-.3, compression_ratio=1.2,
                 no_speech_prob=.02, temperature=0.)
    def gpu(pcm, **params):
        received.append(('cuda', pcm.copy(), params.copy()))
        if not lazy_failure:
            raise RuntimeError('CUDA error')
        def failed_segments():
            yield segment
            raise RuntimeError('CUDA error')
        return failed_segments(), None
    def cpu(pcm, **params):
        received.append(('cpu', pcm.copy(), params.copy()))
        return iter([segment]), None
    app.model = ResilientSpeechModel(app, NS(transcribe=gpu),
        lambda: NS(transcribe=cpu), device='cuda', notify=lambda: True)
    params = dict(temperature=(0., .2, .4), beam_size=7, best_of=5,
                  max_new_tokens=400, condition_on_previous_text=False,
                  without_timestamps=True)
    token = current_audit.set(hold._hold_final_audit)
    try:
        model = recovery.incident_model(app)
        segments, _ = model.transcribe(hold._decode_audio, **params)
        assert list(segments) == [segment]
        # Subsequent CPU recovery retains its explicit temperature (926).
        segments, _ = model.transcribe(hold._decode_audio, **dict(params, temperature=.2))
        assert list(segments) == [segment]
    finally:
        current_audit.reset(token)
    assert recovery._incident_audit.get() is None
    recovery.complete_incident(hold, 'fixture')
    path = pairs(app)[0]
    metadata = json.loads(path.read_text())
    attempts = metadata['decode_attempts']
    assert len(attempts) == len(received) == 3  # No duplicate outer observations.
    assert [call[0] for call in received] == ['cuda', 'cpu', 'cpu']
    assert attempts[0]['params']['temperature'] == [0., .2, .4]
    # 1052a rebase: 1052d on master gives the first CPU fallback its (0, .2) ladder.
    assert [a['params']['temperature'] for a in attempts[1:]] == [[0., .2], .2]
    for attempt, (_, pcm, effective) in zip(attempts, received):
        assert attempt['sample_count'] == len(pcm)
        for key, value in attempt['params'].items():
            assert value == (list(effective[key]) if isinstance(effective.get(key), tuple)
                             else effective.get(key))
    assert attempts[1]['params']['beam_size'] == 3
    assert attempts[1]['params']['best_of'] == 1
    assert attempts[1]['params']['max_new_tokens'] == 192
    assert len(attempts[0]['segments']) == int(lazy_failure)
    assert attempts[1]['segments'][0]['avg_logprob'] == -.3
    assert 'decode_retry' in metadata['reasons']
    with wave.open(str(path.with_suffix('.wav')), 'rb') as wav:
        assert wav.getnframes() == len(received[1][1])
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), '<i2')
    np.testing.assert_array_equal(pcm, np.rint(received[1][1] * 32768).astype('<i2'))


def test_native_hook_without_opt_in_does_not_observe(app):
    from samsara.gpu_fallback import ResilientSpeechModel
    app.config['diagnostics']['keep_incident_holds'] = False
    hold = session(app, first=True)
    native = NS(transcribe=lambda *args, **kwargs: (iter([]), None))
    app.model = ResilientSpeechModel(app, native, lambda: native, device='cpu')
    token = current_audit.set(hold._hold_final_audit)
    try:
        assert recovery.incident_model(app) is app.model
        segments, _ = app.model.transcribe(hold._decode_audio, temperature=0.)
        assert list(segments) == []
    finally:
        current_audit.reset(token)
    assert not hasattr(hold._hold_final_audit, 'incident_attempts')
    assert not recovery.incident_folder().exists()

@pytest.mark.parametrize('path', ['hold_partial', 'handsfree_preview'])
def test_partial_decode_bypasses_incident_observation_even_during_final(app, monkeypatch, path):
    from samsara import vocabulary_decode
    audio = np.ones(1600, dtype=np.float32)
    segment = NS(text='fixture')
    calls = []
    def transcribe(pcm, **params):
        calls.append(pcm)
        return [segment], None
    app.model = NS(transcribe=transcribe)
    app.dictionary = NS(custom_vocab=[])
    def unexpected(*args, **kwargs):
        pytest.fail('partial decoding must not enter incident observation')
    monkeypatch.setattr(recovery, 'incident_model', unexpected)
    audit = FinalAudit()
    token = current_audit.set(audit)
    try:
        segments, _ = vocabulary_decode.transcribe_with_vocabulary(app, audio, path=path)
        assert list(segments) == [segment]
    finally:
        current_audit.reset(token)
    assert len(calls) == 1 and calls[0] is audio
    assert not hasattr(audit, 'incident_attempts')
    assert not recovery.incident_folder().exists()

def test_controls_live_in_help_diagnostics_and_not_health(qapp, app, monkeypatch):
    from PySide6.QtWidgets import QLabel, QPushButton
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp
    from samsara import autostart, health_store
    from samsara.runtime import thread_registry
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(health_store, 'get_today', lambda: [])
    monkeypatch.setattr(health_store, 'get_pain_average', lambda **k: None)
    monkeypatch.setattr(health_store, 'get_by_type', lambda *a, **k: [])
    window = _SettingsWindow(_StubApp())
    window._sidebar.setCurrentRow(5)
    page = window._pages['Help']
    page.advanced.reveal()
    controls = page.advanced.rows.itemAt(1).widget()
    assert page.advanced.rows.itemAt(0).widget().text() == 'Diagnostics'
    assert any(label.text() == 'Incident holds: 0 kept' for label in controls.findChildren(QLabel))
    window.show()
    qapp.processEvents()
    for button in controls.findChildren(QPushButton):
        assert button.geometry().bottom() < controls.height()
    window._select_tool('Health')
    health = window._tool_pages['Health']
    assert not any(label.text().startswith('Incident holds:') for label in health.findChildren(QLabel))
    window.close()
    window.deleteLater()

@pytest.mark.parametrize('has_reason', [False, True])
def test_clear_during_completion_never_republishes_old_hold(app, monkeypatch, has_reason):
    """1052a FINISH IT 4: Clear wins after the early generation check."""
    from concurrent.futures import ThreadPoolExecutor
    hold = session(app, first=has_reason)
    store = app._incident_store_1052a
    paused, resume = threading.Event(), threading.Event()
    original_array = recovery.np.array
    def copying(*args, **kwargs):
        if threading.current_thread().name.startswith('completion-barrier'):
            paused.set()
            assert resume.wait(10), 'completion barrier was not released'
        return original_array(*args, **kwargs)
    monkeypatch.setattr(recovery.np, 'array', copying)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='completion-barrier') as worker:
        completion = worker.submit(recovery.complete_incident, hold, 'fixture')
        try:
            assert paused.wait(5), 'completion did not reach PCM copy after generation check'
            store.clear().result(timeout=5)
            assert store.generation != hold._incident_1052a['generation']
            assert pairs(app) == []
            assert store.previous is None
        finally:
            resume.set()
        completion.result(timeout=5)
    assert pairs(app) == []
    assert store.previous is None
    # A following hold must not resurrect a reason-free stale previous record.
    later = session(app)
    assert pairs(app) == []
    recovery.complete_incident(later, 'fixture')
    assert pairs(app) == []
    assert store.previous['generation'] == store.generation
    # Clear invalidates old work, while new-generation incident capture still works.
    new_hold = session(app)
    new_hold._incident_1052a['signals'].add('quality_warning')
    recovery.complete_incident(new_hold, 'fixture')
    assert any('quality_warning' in json.loads(path.read_text())['reasons'] for path in pairs(app))


def test_submit_rejects_record_from_before_clear(app):
    hold = session(app)
    recovery.complete_incident(hold, 'fixture')
    store = app._incident_store_1052a
    record = store.previous
    store.clear().result(timeout=5)
    assert store.submit(record) is None
    assert not record.get('submitted')
    assert pairs(app) == []
    assert store.previous is None


@pytest.mark.parametrize('policy', ['audit', 'lost_range'])
def test_prepare_final_keeps_master_recovery_policy_and_archives(app, monkeypatch, policy):
    hold = session(app, first=True)
    if policy == 'lost_range':
        del hold._hold_final_audit
        hold._hold_failed_ranges = [(0, .1)]
    def unexpected(*args):
        pytest.fail('audited/lost-range finals must retain master recovery policy')
    monkeypatch.setattr(recovery, '_prepare_final', unexpected)
    assert recovery.prepare_final(hold, '', '', .1) == ('', '', .1)
    assert len(pairs(app)) == 1
