"""Executable classification and background rendering, with no live desktop/model."""
import ctypes
import logging
import threading
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from samsara import game_mode as gm


@pytest.fixture
def rig():
    now = [0.0]
    app = NS(config={'game_mode': {}}, model=object(), device_type='cuda',
             compute_type='float16', model_lock=threading.Lock(), recording=False)
    primary = app.model
    policy = gm.GameMode(app, Mock(return_value=object()), notify=Mock(), clock=lambda: now[0])
    return NS(policy=policy, app=app, primary=primary, now=now)


def game(name='QueensDomain-Win64-Shipping.exe', pid=42, hwnd=400):
    return gm.Sample(True, True, pid, hwnd, process_name=name, window_class='UnrealWindow')


def observe(rig, detector, seconds=0):
    rig.now[0] += seconds
    rig.policy.sample = detector()
    rig.policy.prepare()
    rig.policy.reconcile()


@pytest.mark.parametrize('name', [
    'stremio.exe', 'StremioService.exe', 'vlc.exe', 'mpv.exe', 'mpc-hc.exe',
    'mpc-hc64.exe', 'mpc-be64.exe', 'PotPlayerMini64.exe', 'PotPlayer.exe',
    'chrome.exe', 'BRAVE.EXE', 'msedge.exe', 'firefox.exe', 'opera.exe',
    'Spotify.exe', 'steam.exe', 'steamwebhelper.exe', r'C:\Apps\VLC.EXE',
])
def test_fullscreen_busy_media_browser_and_steam_client_cannot_enter(rig, name):
    sample = game(name)
    present, activity = Mock(), Mock()
    detector = gm.WindowsGameDetector(Mock(return_value=sample), present, activity)
    observe(rig, detector)
    assert not rig.policy.wanted and not rig.policy.active
    assert detector.tracked is None
    # The policy also rejects injected/custom probes carrying excluded metadata.
    rig.policy.sample = sample
    assert not rig.policy.wanted
    rig.policy.factory.assert_not_called()
    present.assert_not_called()
    activity.assert_not_called()


@pytest.mark.parametrize('name', ['QueensDomain-Win64-Shipping.exe', 'hl2.exe',
                                 'steamworld.exe', 'SomeStandaloneGame.exe'])
def test_real_fullscreen_rendering_games_still_enter(rig, name):
    detector = gm.WindowsGameDetector(Mock(return_value=game(name)))
    observe(rig, detector)
    assert rig.policy.active
    assert rig.app.device_type == 'cpu'
    assert detector.tracked.process_name == name


def test_background_rendering_survives_media_foreground_then_idle_expires(rig):
    foreground, present, activity = Mock(return_value=game()), Mock(return_value=True), Mock(return_value=True)
    detector = gm.WindowsGameDetector(foreground, present, activity)
    observe(rig, detector)
    # A browser's fullscreen video must neither replace nor hide the tracked game.
    foreground.return_value = game('brave.exe', 77, 700)
    for _ in range(3):
        observe(rig, detector, 60)
        assert rig.policy.active
        assert rig.policy.sample.tracked_pid == 42
    activity.assert_called_with(42)
    assert detector.tracked.pid == 42
    activity.return_value = False
    observe(rig, detector)
    observe(rig, detector, gm.EXIT_GRACE_SECONDS - .01)
    assert rig.policy.active
    observe(rig, detector, .01)
    assert not rig.policy.active
    assert rig.app.model is rig.primary
    assert present.return_value  # The game window remained open the whole time.


def test_rendering_or_uncertainty_resets_continuous_idle_grace(rig):
    foreground, activity = Mock(return_value=game()), Mock(return_value=False)
    detector = gm.WindowsGameDetector(foreground, Mock(return_value=True), activity)
    observe(rig, detector)
    foreground.return_value = gm.Sample(pid=77, hwnd=700)
    for signal in (True, None):
        observe(rig, detector)
        observe(rig, detector, 44)
        activity.return_value = signal
        observe(rig, detector)
        activity.return_value = False
        observe(rig, detector, 44)
        assert rig.policy.active
    observe(rig, detector, 45)
    assert not rig.policy.active


def test_foreground_game_idle_keeps_mode_until_focus_leaves(rig):
    foreground, activity = Mock(return_value=game()), Mock(return_value=False)
    detector = gm.WindowsGameDetector(foreground, Mock(return_value=True), activity)
    observe(rig, detector)
    foreground.return_value = replace(game(), fullscreen=False, gpu_busy=False)
    observe(rig, detector, 120)
    assert rig.policy.active
    activity.assert_not_called()
    foreground.return_value = gm.Sample(pid=77, hwnd=700)
    observe(rig, detector)
    observe(rig, detector, 45)
    assert not rig.policy.active


def test_closed_or_reused_window_does_not_consult_stale_pid_gpu(rig):
    foreground, present, activity = Mock(return_value=game()), Mock(return_value=False), Mock()
    detector = gm.WindowsGameDetector(foreground, present, activity)
    observe(rig, detector)
    foreground.return_value = gm.Sample(pid=77, hwnd=700)
    observe(rig, detector)
    assert detector.tracked is None
    activity.assert_not_called()
    observe(rig, detector, 45)
    assert not rig.policy.active


def test_manual_on_off_auto_still_override_excluded_video(rig):
    rig.policy.sample = game('stremio.exe')
    rig.policy.set_manual(True)
    rig.policy.prepare()
    rig.policy.reconcile()
    assert rig.policy.active
    rig.policy.set_manual(False)
    assert not rig.policy.active
    rig.policy.set_manual(None)
    assert rig.policy.automatic and not rig.policy.wanted
    rig.policy.sample = game()
    rig.policy.reconcile()
    assert rig.policy.active


def test_transition_logs_are_once_per_change_and_keep_game_provenance(rig, caplog):
    caplog.set_level(logging.INFO, logger=gm.__name__)
    foreground = Mock(return_value=game())
    detector = gm.WindowsGameDetector(foreground, Mock(return_value=True), Mock(return_value=False))
    for _ in range(3):
        observe(rig, detector)
    foreground.return_value = gm.Sample(pid=77, hwnd=700, process_name='chrome.exe')
    observe(rig, detector)
    observe(rig, detector, 45)
    observe(rig, detector, 45)
    messages = [r.getMessage() for r in caplog.records if r.name == gm.__name__]
    for event in ('Tracking', 'Auto on', 'Auto off', 'On', 'Off'):
        hits = [m for m in messages if m.startswith('[GAME-MODE] '+event+' ')]
        assert len(hits) == 1
        assert "process='QueensDomain-Win64-Shipping.exe'" in hits[0]
        assert "window_class='UnrealWindow'" in hits[0]
        assert 'tracked_pid=42' in hits[0]
        assert 'fullscreen=' in hits[0] and 'gpu_busy=' in hits[0]
    assert 'tracked_gpu_busy=False' in next(m for m in messages if '[GAME-MODE] Auto off ' in m)


@pytest.mark.parametrize('name,expected_busy', [('stremio.exe', False), ('', False),
                                             ('QueensDomain-Win64-Shipping.exe', True)])
def test_windows_sample_reads_class_and_executable_never_title(monkeypatch, name, expected_busy):
    def rect(hwnd, pointer):
        pointer._obj.left = pointer._obj.top = 0
        pointer._obj.right, pointer._obj.bottom = 1920, 1080
        return True

    def monitor(handle, pointer):
        return rect(handle, NS(_obj=pointer._obj.monitor))

    def pid(hwnd, pointer):
        pointer._obj.value = 42
        return True

    def window_class(hwnd, buffer, size):
        buffer.value = 'UnrealWindow'
        return len(buffer.value)

    user = NS(GetForegroundWindow=Mock(return_value=400), MonitorFromWindow=Mock(return_value=1),
              GetWindowRect=Mock(side_effect=rect), GetMonitorInfoW=Mock(side_effect=monitor),
              IsIconic=Mock(return_value=False), GetWindowThreadProcessId=Mock(side_effect=pid),
              GetClassNameW=Mock(side_effect=window_class), GetWindowTextW=Mock(side_effect=AssertionError('title read')))
    monkeypatch.setattr(ctypes, 'WinDLL', Mock(return_value=user), raising=False)
    monkeypatch.setattr('sys.platform', 'win32')
    monkeypatch.setattr(gm, 'process_name', Mock(return_value=name))
    activity = Mock(return_value=True)
    monkeypatch.setattr(gm, 'gpu_busy', activity)
    sample = gm.windows_sample()
    assert sample.fullscreen and sample.gpu_busy is expected_busy
    assert (sample.pid, sample.hwnd, sample.process_name, sample.window_class) == (42, 400, name, 'UnrealWindow')
    assert activity.call_count == int(expected_busy)
    user.GetWindowTextW.assert_not_called()


def test_process_lookup_failure_is_safe(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, 'Process', Mock(side_effect=psutil.AccessDenied(42)))
    assert gm.process_name(42) == ''


def test_minimized_game_can_still_be_checked_for_rendering(monkeypatch):
    def pid(hwnd, pointer):
        pointer._obj.value = 42
        return True

    user = NS(IsWindow=Mock(return_value=True), IsIconic=Mock(return_value=True),
              GetWindowThreadProcessId=Mock(side_effect=pid))
    monkeypatch.setattr(ctypes, 'WinDLL', Mock(return_value=user), raising=False)
    detector = gm.WindowsGameDetector(Mock(return_value=game()), activity=Mock(return_value=True))
    detector()
    detector.foreground.return_value = gm.Sample(pid=77, hwnd=700)
    assert detector().game_present is True
    detector.activity.assert_called_once_with(42)
