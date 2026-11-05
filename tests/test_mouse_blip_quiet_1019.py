"""1019: deterministic recovery notices; no app bootstrap, audio or live hooks."""
from types import SimpleNamespace

import pytest

from samsara import hotkeys, microphone_recovery as mic, recovery_notifications as notices


class Clock:
    def __init__(self):
        self.now = 0.0
        self.jobs = []

    def __call__(self):
        return self.now

    def schedule(self, delay, callback):
        job = SimpleNamespace(at=self.now + delay, callback=callback, cancelled=False)
        job.cancel = lambda: setattr(job, 'cancelled', True)
        self.jobs.append(job)
        return job

    def advance(self, seconds):
        target = self.now + seconds
        while self.jobs and min(job.at for job in self.jobs) <= target:
            job = min(self.jobs, key=lambda job: job.at)
            self.jobs.remove(job)
            self.now = job.at
            if not job.cancelled:
                job.callback()
        self.now = target


class Hook:
    retries_automatically = True

    def __init__(self, **callbacks):
        self.__dict__.update(callbacks)
        self.requested_suppress_buttons = self.suppress_buttons
        self.installed = True
        self.recovering = False
        self.install_error = ''

    def start(self):
        pass

    def stop(self):
        self.installed = False

    def interrupt(self, *, error=''):
        self.installed = False
        self.recovering = True
        self.install_error = error
        self.on_hook_failed(error or 'temporary hook interruption')

    def recover(self):
        self.installed = True
        self.recovering = False
        self.install_error = ''
        self.on_hook_recovered()


class App(hotkeys.HotkeyCluster):
    def __init__(self):
        self.config = {'hotkey': 'mouse4', 'command_mode': {'button': 'rctrl'}}
        self.warnings, self.chips, self.device_states = [], [], []
        self.tray_icon = SimpleNamespace(notify_warning=lambda *args: self.warnings.append(args))
        self.listening_indicator = SimpleNamespace(set_device_lost=self.device_states.append)
        self._mic_boot_ready = True
        self._no_microphone = False
        self._using_default_mic = False
        self.recording = False
        self.stops = []

    def _show_outcome_chip(self, *args, **kw):
        self.chips.append(args)

    def _schedule_ui(self, callback, *args):
        callback(*args)

    def _update_tray_tooltip(self):
        pass

    def stop_recording(self):
        self.stops.append(True)


@pytest.fixture
def notice_clock(monkeypatch):
    """Drive notification deadlines without sleeping or real timer threads."""
    clock = Clock()
    notice_class = notices.RecoveryNotice
    monkeypatch.setattr(notices, 'RecoveryNotice', lambda: notice_class(
        clock=clock, schedule=clock.schedule))
    monkeypatch.setattr(mic, 'time', SimpleNamespace(monotonic=clock))
    return clock


@pytest.fixture
def setup(monkeypatch, notice_clock):
    import samsara.mouse_hook as mouse_hook

    clock = notice_clock
    real_hook_class = mouse_hook.MouseHook
    monkeypatch.setattr(mouse_hook, 'MouseHook', Hook)
    spawned = []
    monkeypatch.setattr(hotkeys.thread_registry, 'spawn',
                        lambda name, callback, **kw: spawned.append((name, callback)))
    app = App()
    app._install_mouse_listener()
    return SimpleNamespace(app=app, clock=clock, spawned=spawned,
                           real_hook_class=real_hook_class)


def test_mouse_blip_inside_grace_has_no_balloon_or_chip(setup, caplog):
    app, clock = setup.app, setup.clock
    hook = app._mouse_hook
    hook.interrupt()
    assert app._main_hotkey_override == 'ctrl+shift'
    assert app.mouse_hotkey_status()['state'] == 'recovering'
    assert app.warnings == app.chips == []
    clock.advance(4.999)
    hook.recover()
    clock.advance(100)
    assert app.warnings == app.chips == []
    assert app._main_hotkey_override is None
    assert app._mouse_hotkey_disabled_reason is None
    assert app.config['hotkey'] == 'mouse4'
    assert 'automatic recovery' in caplog.text


def test_mouse_stuck_notifies_once_at_grace_without_another_event(setup):
    app, clock = setup.app, setup.clock
    app._mouse_hook.interrupt()
    clock.advance(4.999)
    assert app.warnings == []
    clock.advance(.001)
    assert len(app.warnings) == len(app.chips) == 1
    assert app.warnings[0][0] == 'Samsara mouse hotkey interrupted'
    assert 'ctrl+shift' in app.warnings[0][1]
    for _ in range(10):
        app._on_mouse_hook_failed('same outage')
    clock.advance(1200)
    assert len(app.warnings) == len(app.chips) == 1


def test_three_mouse_blips_escalate_then_cooldown_survives_recovery(setup):
    app, clock = setup.app, setup.clock
    for index in range(8):
        app._mouse_hook.interrupt()
        assert len(app.warnings) == (0 if index < 2 else 1)
        clock.advance(.2)
        app._mouse_hook.recover()
        clock.advance(2)
    assert len(app.chips) == 1
    clock.advance(notices.RECOVERY_NOTICE_COOLDOWN_SECONDS)
    app._mouse_hook.interrupt()
    clock.advance(notices.RECOVERY_NOTICE_GRACE_SECONDS)
    assert len(app.warnings) == 2


def test_mouse_blips_outside_repeat_window_stay_silent(setup):
    app, clock = setup.app, setup.clock
    for _ in range(4):
        app._mouse_hook.interrupt()
        clock.advance(.1)
        app._mouse_hook.recover()
        clock.advance(31)
    assert app.warnings == app.chips == []


@pytest.mark.parametrize('install_error', ['', 'SetWindowsHookEx failed'])
def test_disabled_mouse_notifies_immediately_even_during_cooldown(setup, install_error):
    app, clock = setup.app, setup.clock
    app._mouse_hook.interrupt()
    clock.advance(5)
    assert len(app.warnings) == 1
    hook = app._mouse_hook
    hook.retries_automatically = bool(install_error)
    hook.interrupt(error=install_error)
    assert len(app.warnings) == 2
    assert app.warnings[-1][0] == 'Samsara mouse hotkey disabled'
    assert app.mouse_hotkey_status()['state'] == 'disabled'
    clock.advance(100)
    assert len(app.warnings) == 2


def test_queued_mouse_timer_cannot_warn_after_recovery_or_replacement(setup):
    app, clock = setup.app, setup.clock
    app._mouse_hook.interrupt()
    stale = clock.jobs[-1].callback
    app._mouse_hook.recover()
    app._mouse_hook.interrupt()
    stale()  # Even cancellation racing an already-dispatched callback is safe.
    assert app.warnings == []
    old_hook = app._mouse_hook
    app.refresh_mouse_hook(rearm=True)
    assert app._mouse_hook is not old_hook
    clock.advance(10)
    assert app.warnings == app.chips == []


def test_mouse_fallback_releases_a_held_recording_immediately(setup):
    app = setup.app
    app.recording = app._hold_down_mouse = True
    app._main_hotkey_source = 'mouse'
    app._mouse_hook.interrupt()
    assert not app._hold_down_mouse
    assert app._main_hotkey_source == 'key'
    assert len(setup.spawned) == 1 and setup.spawned[0][0] == 'stop-rec'
    setup.spawned[0][1]()
    assert app.stops == [True]
    assert app.warnings == app.chips == []


@pytest.mark.parametrize('fallback', [False, True])
def test_microphone_flap_has_no_balloon(setup, fallback):
    app, clock = setup.app, setup.clock
    app._using_default_mic = fallback
    mic.set_state(app, not fallback)
    assert app.device_states == [not fallback]
    clock.advance(4.9)
    app._using_default_mic = False
    mic.set_state(app, False)
    clock.advance(20)
    assert app.warnings == []


@pytest.mark.parametrize('fallback', [False, True])
def test_microphone_stays_missing_or_in_fallback_notifies_once(setup, fallback):
    app, clock = setup.app, setup.clock
    app._using_default_mic = fallback
    mic.set_state(app, not fallback)
    for _ in range(5):
        clock.advance(.9)
        mic.set_state(app, not fallback)
    assert app.warnings == []  # Watcher polls are not extra episodes.
    clock.advance(.5)
    assert app.warnings == [('Microphone' if fallback else 'No microphone',
                             mic.FALLBACK_MESSAGE if fallback else mic.MISSING_MESSAGE)]
    clock.advance(1000)
    mic.set_state(app, not fallback)
    assert len(app.warnings) == 1


@pytest.mark.parametrize('fallback', [False, True])
def test_three_mic_flaps_escalate_then_respect_cooldown(setup, fallback):
    app, clock = setup.app, setup.clock
    for index in range(5):
        app._using_default_mic = fallback
        mic.set_state(app, not fallback)
        assert len(app.warnings) == (0 if index < 2 else 1)
        clock.advance(.2)
        app._using_default_mic = False
        mic.set_state(app, False)
        clock.advance(2)
    app._using_default_mic = fallback
    mic.set_state(app, not fallback)
    clock.advance(5)
    assert len(app.warnings) == 1
    app._using_default_mic = False
    mic.set_state(app, False)
    clock.advance(600)
    app._using_default_mic = fallback
    mic.set_state(app, not fallback)
    clock.advance(5)
    assert len(app.warnings) == 2


def test_mic_fallback_warning_does_not_hide_later_total_loss(setup):
    app, clock = setup.app, setup.clock
    app._using_default_mic = True
    mic.set_state(app, False)
    clock.advance(5)
    mic.set_state(app, True)
    clock.advance(5)
    assert [warning[0] for warning in app.warnings] == ['Microphone', 'No microphone']


def test_microphone_deliberate_attempt_keeps_immediate_error_chip(setup):
    app, clock = setup.app, setup.clock
    mic.set_state(app, True, notify=True)
    assert app.chips == [(mic.MISSING_MESSAGE, 'error')]
    assert app.warnings == []
    clock.advance(5)
    assert app.warnings == [('No microphone', mic.MISSING_MESSAGE)]


@pytest.mark.parametrize('recovers', [False, True])
def test_microphone_boot_grace_does_not_double_delay(setup, recovers):
    app, clock = setup.app, setup.clock
    del app._no_microphone
    app._mic_boot_ready = False
    mic.set_state(app, True)
    clock.advance(4.9)
    assert app.warnings == []
    if recovers:
        mic.set_state(app, False)
    clock.advance(.1)
    assert len(app.warnings) == (0 if recovers else 1)


def test_mic_warning_waits_for_tray_without_consuming_cooldown(setup):
    app, clock = setup.app, setup.clock
    tray = app.tray_icon
    app.tray_icon = None
    mic.set_state(app, True)
    clock.advance(5)
    assert app.warnings == []
    app.tray_icon = tray
    mic.set_state(app, True)
    assert len(app.warnings) == 1


def test_mouse_and_mic_cooldowns_are_independent(setup):
    app, clock = setup.app, setup.clock
    app._mouse_hook.interrupt()
    mic.set_state(app, True)
    clock.advance(5)
    assert [warning[0] for warning in app.warnings] == [
        'Samsara mouse hotkey interrupted', 'No microphone']


@pytest.fixture
def real_hook(setup, monkeypatch):
    import samsara.mouse_hook as mouse_hook

    app, clock = setup.app, setup.clock
    monkeypatch.setattr(mouse_hook, '_perf', clock)
    hook = setup.real_hook_class(lambda *_: None, on_hook_failed=app._on_mouse_hook_failed)
    monkeypatch.setattr(hook, 'start', lambda: None)
    # Retain the app's exact recovery closure without starting native hooks.
    def factory(**kwargs):
        hook.on_hook_recovered = kwargs['on_hook_recovered']
        hook.on_hook_interrupted = kwargs['on_hook_interrupted']
        return hook
    monkeypatch.setattr(mouse_hook, 'MouseHook', factory)
    app._install_mouse_listener()
    return hook


def test_real_hook_reports_distinct_recovered_blips_inside_reset_window(setup, real_hook):
    app, clock, hook = setup.app, setup.clock, real_hook
    for index in range(3):
        hook._begin_recovery('lost')
        hook._begin_recovery('same outage')
        assert app._main_hotkey_override == 'ctrl+shift'
        assert app.mouse_hotkey_status()['state'] == 'recovering'
        assert len(app.warnings) == (1 if index == 2 else 0)
        clock.advance(.2)
        hook._recovered()
        clock.advance(2)
    assert len(app.warnings) == len(app.chips) == 1
    assert app._main_hotkey_override is None


def test_real_hook_install_failure_during_grace_is_loud_once(setup, real_hook, monkeypatch):
    app, clock, hook = setup.app, setup.clock, real_hook
    hook._begin_recovery('temporary loss')
    assert app.warnings == []
    monkeypatch.setattr(hook, '_start_hook_thread', lambda: setattr(
        hook, 'install_error', 'SetWindowsHookEx failed'))
    hook._attempt_install()
    assert app.warnings[0][0] == 'Samsara mouse hotkey disabled'
    hook._attempt_install()
    clock.advance(10)
    assert len(app.warnings) == 1


def test_production_scheduler_uses_registered_daemon_timer(monkeypatch):
    calls = []
    timer = SimpleNamespace(cancel=lambda: None)
    monkeypatch.setattr(notices.thread_registry, 'timer',
                        lambda *args, **kwargs: calls.append((args, kwargs)) or timer)
    notice = notices.RecoveryNotice(clock=lambda: 0.0)
    notice.interrupted(lambda: True, lambda: True)
    assert calls[0][0][0:2] == ('input-recovery-notice', notices.RECOVERY_NOTICE_GRACE_SECONDS)
    assert calls[0][1] == {'daemon': True}
