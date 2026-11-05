"""Rapid game switching with fake time/detection; no app boot or real models."""
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from samsara.game_mode import (
    EXIT_GRACE_SECONDS, MAX_SWITCHES_PER_WINDOW, GameMode, Sample,
    WindowsGameDetector, before_capture, decode_model, publish, runtime_text,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SpeechModel:
    def transcribe(self):
        # A real faster-whisper decode is lazy too. Yield metadata only.
        yield self


@pytest.fixture
def rig():
    clock = Clock()
    primary, cpu = SpeechModel(), SpeechModel()
    app = NS(config={'model_size': 'small', 'device': 'cuda', 'game_mode': {}},
             model=primary, device_type='cuda', compute_type='float16',
             model_lock=threading.Lock(), _hold_capture_lifecycle_lock=threading.RLock(),
             recording=False, _running=True)
    factory = Mock(return_value=cpu)
    policy = app._game_mode = GameMode(app, factory, clock=clock, notify=Mock())
    return NS(app=app, policy=policy, factory=factory, primary=primary, cpu=cpu, clock=clock)


def enter(r):
    r.policy.sample = Sample(True, True, 42, 400)
    r.policy.prepare()
    assert r.policy.reconcile()


def hold(r):
    """Exercise the production admission seam and decode selection under locks."""
    with r.app._hold_capture_lifecycle_lock:
        before_capture(r.app)
        selected = r.app.model
        r.app.recording = True
    try:
        assert not r.policy.reconcile()
        with r.app.model_lock:
            assert list(decode_model(r.app, 'hold_final').transcribe()) == [selected]
    finally:
        r.app.recording = False
    return selected


def test_ten_alt_tabs_per_minute_keep_cpu_and_every_hold_decodes(rig):
    r = rig
    enter(r)
    for _ in range(10):
        r.clock.advance(3)
        r.policy.sample = Sample()  # Even minimizing on every alt-tab is safe.
        assert not r.policy.reconcile()
        assert hold(r) is r.cpu
        r.clock.advance(3)
        r.policy.sample = Sample(True, True, 42, 400)
        assert not r.policy.reconcile()
        assert hold(r) is r.cpu
    r.factory.assert_called_once()
    assert r.policy.notify.call_count == 1


def test_background_unminimized_game_is_retained_without_gpu_activity(rig):
    r = rig
    foreground, present = Mock(return_value=Sample(True, True, 42, 400)), Mock(return_value=True)
    detector = WindowsGameDetector(foreground, present)
    r.policy.sample = detector()
    r.policy.prepare()
    r.policy.reconcile()
    foreground.return_value = Sample(False, False, 77, 700)
    for _ in range(10):
        r.clock.advance(60)
        r.policy.sample = detector()
        assert not r.policy.reconcile()
        assert r.app.model is r.cpu
    assert present.call_args.args[0].pid == 42
    assert present.call_args.args[0].hwnd == 400


def test_closed_or_minimized_game_needs_full_continuous_exit_grace(rig):
    r = rig
    enter(r)
    r.policy.sample = Sample()
    r.clock.advance(EXIT_GRACE_SECONDS - .01)
    r.policy.sample = Sample()
    assert not r.policy.reconcile()
    r.clock.advance(.01)
    r.policy.sample = Sample()
    assert r.policy.reconcile()
    assert r.app.model is r.primary
    assert r.policy.resident is r.cpu
    r.policy.sample = Sample(True, True, 42, 400)
    r.policy.prepare()
    assert r.policy.reconcile()
    r.factory.assert_called_once()


@pytest.mark.parametrize('presence', [True, None])
def test_returned_or_uncertain_window_resets_exit_timer(rig, presence):
    r = rig
    enter(r)
    r.policy.sample = Sample()
    r.clock.advance(44)
    r.policy.sample = Sample(game_present=presence)
    r.clock.advance(44)
    r.policy.sample = Sample()
    assert not r.policy.reconcile()
    r.clock.advance(44)
    r.policy.sample = Sample()
    assert not r.policy.reconcile()
    r.clock.advance(1)
    r.policy.sample = Sample()
    assert r.policy.reconcile()


def test_wall_time_without_new_samples_cannot_expire_grace(rig):
    enter(rig)
    rig.policy.sample = Sample()
    rig.clock.advance(1000)
    assert rig.policy.wanted
    assert not rig.policy.reconcile()


@pytest.mark.parametrize('entering', [True, False])
@pytest.mark.parametrize('busy', ['recording', '_pending_transcriptions', 'loading_model'])
def test_detection_flips_during_capture_decode_queue_or_primary_load(rig, entering, busy):
    r = rig
    if not entering:
        enter(r)
    else:
        r.policy.manual = True
        r.policy.prepare()
        r.policy.manual = None
    previous = r.app.model
    setattr(r.app, busy, True)
    r.policy.sample = Sample(True, True) if entering else Sample()
    r.clock.advance(45)
    r.policy.sample = Sample(True, True) if entering else Sample()
    assert not r.policy.reconcile()
    assert r.app.model is previous
    assert ('(game mode)' in runtime_text(r.app)) == (not entering)
    setattr(r.app, busy, False)
    assert r.policy.reconcile()
    assert r.app.model is (r.cpu if entering else r.primary)


def test_lazy_decode_owns_model_until_iteration_finishes(rig):
    r = rig
    enter(r)
    with r.app.model_lock:
        segments = decode_model(r.app, 'hold_final').transcribe()
        r.policy.sample = Sample()
        r.clock.advance(45)
        r.policy.sample = Sample()
        assert not r.policy.reconcile()
        assert next(segments) is r.cpu
        assert not r.policy.reconcile()
        assert list(segments) == []
    assert r.policy.reconcile()
    assert r.app.model is r.primary


def test_switch_cap_coalesces_to_latest_request_without_delaying_capture(rig):
    r = rig
    r.policy.manual = True
    r.policy.prepare()
    for i in range(20):
        r.policy.set_manual(i % 2 == 0)
        assert hold(r) in (r.primary, r.cpu)
        r.clock.advance(1)
    assert r.policy.notify.call_count == MAX_SWITCHES_PER_WINDOW
    r.policy.set_manual(True)
    assert r.app.model is r.primary
    assert '(game mode)' not in runtime_text(r.app)
    r.clock.advance(40)
    assert r.policy.reconcile()
    assert r.app.model is r.cpu
    r.factory.assert_called_once()


@pytest.mark.parametrize('flip', ['off', 'on', 'different_model'])
def test_capture_and_decode_continue_during_slow_single_flight_load(rig, flip):
    r = rig
    started, release, captured = threading.Event(), threading.Event(), threading.Event()
    failures = []

    def load(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return r.cpu

    r.factory.side_effect = load
    r.policy.manual = True
    loader = threading.Thread(target=r.policy.prepare)
    loader.start()
    try:
        assert started.wait(1)
        assert not r.policy.prepare()  # no duplicate constructor
        r.policy.set_manual(False)
        if flip == 'on':
            r.policy.set_manual(True)
        elif flip == 'different_model':
            r.app.config['game_mode']['cpu_model'] = 'small.en'
            r.policy.set_manual(True)

        def capture():
            try:
                assert hold(r) is r.primary
            except BaseException as error:
                failures.append(error)
            finally:
                captured.set()

        worker = threading.Thread(target=capture)
        worker.start()
        assert captured.wait(1), 'capture waited behind model construction'
        worker.join(1)
        assert not failures
        assert 'small on CUDA' in runtime_text(r.app)
    finally:
        release.set()
        loader.join(3)
    assert not loader.is_alive()
    assert r.app.model is r.primary  # prepare never publishes a model swap
    r.policy.reconcile()
    assert r.app.model is (r.cpu if flip == 'on' else r.primary)
    assert (r.policy.resident is None) == (flip == 'different_model')
    r.factory.assert_called_once()


def test_load_started_at_idle_cannot_swap_into_a_new_recording(rig):
    r = rig
    def load(*args, **kwargs):
        before_capture(r.app)
        r.app.recording = True
        return r.cpu
    r.factory.side_effect = load
    r.policy.manual = True
    r.policy.prepare()
    assert not r.policy.reconcile()
    assert r.app.model is r.primary
    r.app.recording = False
    assert r.policy.reconcile()


def test_monitor_refreshes_detection_after_constructor(rig):
    r = rig
    samples = iter([Sample(True, True, 42, 400), Sample(game_present=True)])
    r.policy.probe = Mock(side_effect=lambda: next(samples))
    def load(*args, **kwargs):
        r.clock.advance(90)
        r.policy.stop.set()
        return r.cpu
    r.factory.side_effect = load
    r.policy.run()
    assert r.policy.probe.call_count == 2
    assert r.policy.sample.game_present is True
    assert r.policy.active


def test_capture_does_not_wait_for_policy_lock(rig):
    r = rig
    locked, release, captured = threading.Event(), threading.Event(), threading.Event()
    def own_lock():
        with r.policy._lock:
            locked.set()
            release.wait(3)
    owner = threading.Thread(target=own_lock)
    owner.start()
    worker = threading.Thread(target=lambda: (before_capture(r.app), captured.set()))
    try:
        assert locked.wait(1)
        worker.start()
        assert captured.wait(1), 'capture blocked on the policy lock'
    finally:
        release.set()
        owner.join(3)
        worker.join(3)
    assert r.app.model is r.primary


def test_notification_runs_outside_capture_and_model_locks(rig):
    r = rig
    acquired = []
    def notify(*args):
        def inspect():
            for lock in (r.app._hold_capture_lifecycle_lock, r.app.model_lock, r.policy._lock):
                ok = lock.acquire(blocking=False)
                acquired.append(ok)
                if ok:
                    lock.release()
        worker = threading.Thread(target=inspect)
        worker.start()
        worker.join(1)
    r.policy.notify = notify
    enter(r)
    assert acquired == [True, True, True]


def test_stale_qt_posts_cannot_revert_display_label(rig, monkeypatch):
    from samsara.ui import qt_runtime
    r = rig
    posted = []
    monkeypatch.setattr(qt_runtime, 'post', posted.append)
    r.app.listening_indicator = NS(set_game_mode=Mock(), show_outcome=Mock())
    publish(r.app, False)
    enter(r)
    posted[0]()
    r.app.listening_indicator.set_game_mode.assert_called_once_with(True)


def test_runtime_reports_loaded_names_despite_saved_changes(rig):
    r = rig
    r.app.config['model_size'] = 'large-v3'
    r.app.config['device'] = 'cpu'
    assert runtime_text(r.app) == 'Right now: small on CUDA'
    enter(r)
    r.app.config['game_mode']['cpu_model'] = 'small.en'
    r.app.recording = True
    r.policy.prepare()
    assert runtime_text(r.app) == 'Right now: base.en on CPU (game mode)'
    r.app.recording = False
    r.factory.return_value = SpeechModel()
    r.policy.prepare()
    assert runtime_text(r.app) == 'Right now: base.en on CPU (game mode)'
    r.policy.reconcile()
    assert runtime_text(r.app) == 'Right now: small.en on CPU (game mode)'


def test_failed_replacement_keeps_game_model_and_matching_label(rig):
    r = rig
    enter(r)
    r.factory.side_effect = OSError('test load failure')
    r.app.config['game_mode']['cpu_model'] = 'small.en'
    assert not r.policy.prepare()
    assert not r.policy.reconcile()
    assert r.app.model is r.cpu
    assert r.policy.notify.call_args.args[1] is True
    assert runtime_text(r.app) == 'Right now: base.en on CPU (game mode)'


def test_replaced_cpu_model_retires_only_on_monitor_after_grace(rig):
    import weakref
    r = rig
    enter(r)
    old = weakref.ref(r.cpu)
    r.cpu = None
    r.factory.return_value = SpeechModel()
    r.app.config['game_mode']['cpu_model'] = 'small.en'
    r.policy.prepare()
    before_capture(r.app)
    assert old() is not None
    r.clock.advance(59)
    r.policy._release_retired()
    assert old() is not None
    r.clock.advance(1)
    r.app.recording = True
    r.policy._release_retired()
    assert old() is not None
    r.app.recording = False
    r.policy._release_retired()
    assert old() is None


def test_new_cpu_load_is_not_started_behind_active_decoder(rig):
    r = rig
    r.policy.manual = True
    with r.app.model_lock:
        assert not r.policy.prepare()
    r.factory.assert_not_called()
    assert r.policy.prepare()


@pytest.mark.parametrize('exists,minimized,pid,query_ok,expected', [
    (True, False, 42, True, True),
    (True, True, 42, True, False),
    (False, False, 42, True, False),
    (True, False, 77, True, False),
    (True, False, 42, False, None),
])
def test_window_presence_distinguishes_focus_minimization_closure_and_reuse(
        monkeypatch, exists, minimized, pid, query_ok, expected):
    import ctypes
    from samsara.game_mode import game_window_present
    def query(hwnd, pointer):
        pointer._obj.value = pid
        return int(query_ok)
    user = NS(IsWindow=Mock(return_value=exists), IsIconic=Mock(return_value=minimized),
              GetWindowThreadProcessId=Mock(side_effect=query))
    monkeypatch.setattr(ctypes, 'WinDLL', Mock(return_value=user), raising=False)
    assert game_window_present(Sample(True, True, 42, 400)) is expected


def test_settings_status_is_in_real_compute_row_and_updates(qapp, monkeypatch, rig):
    from PySide6.QtCore import QTimer
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp
    monkeypatch.setattr('samsara.autostart.sync_run_entry', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.cuda_detect.request_cuda_status', lambda cb: cb(True, 'Ready'))
    app = _StubApp({'model_size': 'small', 'device': 'cuda', 'game_mode': {}})
    app.model, app.device_type, app.compute_type = rig.primary, 'cuda', 'float16'
    app.model_lock, app._hold_capture_lifecycle_lock = threading.Lock(), threading.RLock()
    app._game_mode = GameMode(app, rig.factory, notify=Mock(), clock=rig.clock)
    window = _SettingsWindow(app)
    try:
        window.show_tab('Dictation')
        window._pages['Dictation'].advanced.toggle.setChecked(True)
        window.show()
        qapp.processEvents()
        label = window._widgets['game_runtime_status']
        assert label.isVisible()
        assert window._rows_by_widget['adv_device'].isAncestorOf(label)
        assert label.text() == 'Right now: small on CUDA'
        app._game_mode.set_manual(True)
        app._game_mode.prepare()
        app._game_mode.reconcile()
        label.findChild(QTimer).timeout.emit()
        assert label.text() == 'Right now: base.en on CPU (game mode)'
        assert window._widgets['adv_device'].currentText() == 'CUDA (NVIDIA GPU)'
    finally:
        window.close()
        window.deleteLater()
