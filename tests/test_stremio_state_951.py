"""951 intent memory and future state probe; all desktop effects are inert."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import stremio
from samsara import plugin_commands
from samsara.command_registry import CommandMatcher, DispatchState
from samsara.command_scope import MatchContext

sc = stremio.stremio_control


@pytest.fixture
def app():
    return SimpleNamespace(_show_outcome_chip=Mock(),
                           audio_coordinator=SimpleNamespace(speak=Mock()))


@pytest.fixture
def player(monkeypatch):
    state = SimpleNamespace(paused=False, hwnd=101, pid=202, created=303,
                            available=True, observed=False, now=100.0)
    monkeypatch.setattr(sc, "_playback_memory", None, raising=False)
    monkeypatch.setattr(sc.time, "monotonic", lambda: state.now)

    def target():
        if not state.available:
            return None
        cls = getattr(sc, "PlaybackTarget", SimpleNamespace)
        return cls(hwnd=state.hwnd, pid=state.pid, created=state.created)

    def probe():
        if not state.available or not state.observed:
            return None
        cls = getattr(sc, "PlaybackSnapshot", SimpleNamespace)
        return cls(hwnd=state.hwnd, pid=state.pid, paused=state.paused)

    def send(_script):
        # Model the AHK target/foreground guard without executing desktop code.
        if not state.available:
            return False
        state.paused = not state.paused
        return True

    monkeypatch.setattr(sc, "_find_playback_target", Mock(side_effect=target), raising=False)
    monkeypatch.setattr(sc, "probe_playback_state", Mock(side_effect=probe), raising=False)
    state.send = Mock(side_effect=send)
    monkeypatch.setattr(sc, "_run_ahk", state.send)
    return state


def test_pause_twice_sends_one_action(app, player):
    assert stremio.handle_pause(app, "") is True
    assert stremio.handle_pause(app, "") is True
    assert player.paused is True
    assert player.send.call_count == 1
    app.audio_coordinator.speak.assert_called_with(
        "Already paused — say 'toggle stremio' if it isn't", category="command")


def test_resume_twice_sends_one_action(app, player):
    player.paused = True
    assert stremio.handle_resume(app, "") is True
    assert stremio.handle_resume(app, "") is True
    assert player.paused is False
    assert player.send.call_count == 1
    app.audio_coordinator.speak.assert_called_with(
        "Already playing — say 'toggle stremio' if it isn't", category="command")


def test_pause_then_resume(app, player):
    assert stremio.handle_pause(app, "") is True
    assert player.paused is True
    assert stremio.handle_resume(app, "") is True
    assert player.paused is False
    assert player.send.call_count == 2


@pytest.mark.parametrize("handler", [stremio.handle_pause, stremio.handle_resume])
def test_unknown_state_sends_first_request_then_suppresses_repeat(app, player, handler):
    assert handler(app, "") is True
    assert handler(app, "") is True
    assert player.send.call_count == 1


def test_production_probe_is_unknown_but_intent_memory_works(app, monkeypatch):
    assert sc.probe_playback_state() is None
    # Reinstall inert desktop boundaries; never invoke AHK or a live provider.
    monkeypatch.setattr(sc, "_playback_memory", None)
    monkeypatch.setattr(sc, "_find_playback_target", lambda: sc.PlaybackTarget(101, 202, 303))
    send = Mock(return_value=True)
    monkeypatch.setattr(sc, "_run_ahk", send)
    assert stremio.handle_pause(app, "") is True
    assert stremio.handle_pause(app, "") is True
    assert send.call_count == 1


def test_probe_error_uses_intent_memory(app, player, monkeypatch):
    monkeypatch.setattr(sc, "probe_playback_state", Mock(side_effect=OSError("unavailable")))
    assert stremio.handle_pause(app, "") is True
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 1


@pytest.mark.parametrize("snapshot", [None, False, "paused", SimpleNamespace(paused=True)])
def test_invalid_probe_result_uses_validated_target(app, player, monkeypatch, snapshot):
    monkeypatch.setattr(sc, "probe_playback_state", lambda: snapshot)
    assert stremio.handle_pause(app, "") is True
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 1


@pytest.mark.parametrize("hwnd,pid,paused", [(0, 202, False), (101, 0, False),
                                           ("101", 202, False), (101, 202, None),
                                           (909, 202, True), (101, 909, True)])
def test_malformed_or_other_target_probe_cannot_override_memory(
        app, player, monkeypatch, hwnd, pid, paused):
    monkeypatch.setattr(sc, "probe_playback_state", lambda: sc.PlaybackSnapshot(hwnd, pid, paused))
    assert stremio.handle_pause(app, "") is True
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 1
    assert "target := 101\nexpectedPid := 202" in player.send.call_args.args[0]


def test_closed_target_clears_memory_and_does_not_send(app, player):
    assert stremio.handle_pause(app, "") is True
    player.send.reset_mock()
    player.available = False
    result = stremio.handle_resume(app, "")
    assert result.state is DispatchState.FAILED
    assert result.claimed and result.detail["unavailable"]
    player.send.assert_not_called()
    assert sc._playback_memory is None
    player.available = True
    assert stremio.handle_pause(app, "") is True
    player.send.assert_called_once()


@pytest.mark.parametrize("field", ["hwnd", "pid", "created"])
def test_window_process_or_restart_resets_memory(app, player, field):
    assert stremio.handle_pause(app, "") is True
    setattr(player, field, getattr(player, field) + 1000)
    player.paused = False
    assert stremio.handle_pause(app, "") is True
    assert player.paused is True
    assert player.send.call_count == 2
    script = player.send.call_args.args[0]
    assert f"target := {player.hwnd}\nexpectedPid := {player.pid}" in script
    assert "WinExist(" not in script


def test_switching_back_to_old_window_does_not_restore_old_memory(app, player):
    assert stremio.handle_pause(app, "") is True
    player.hwnd = 909
    assert stremio.handle_pause(app, "") is True
    player.hwnd = 101
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 3


def test_expiry_from_last_send_resets_even_after_suppressed_repeat(app, player):
    assert sc.PLAYBACK_MEMORY_SECONDS == 30 * 60
    assert stremio.handle_pause(app, "") is True
    player.now += sc.PLAYBACK_MEMORY_SECONDS - 1
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 1
    player.now += 1
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 2


def test_expiry_is_configurable_by_one_constant(app, player, monkeypatch):
    monkeypatch.setattr(sc, "PLAYBACK_MEMORY_SECONDS", 5)
    assert stremio.handle_pause(app, "") is True
    player.now += 5
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 2


def test_real_probe_overrides_conflicting_intent(app, player):
    assert stremio.handle_pause(app, "") is True
    player.observed = True
    player.paused = False  # Mouse/remote resumed it.
    assert stremio.handle_pause(app, "") is True
    assert player.paused is True
    assert player.send.call_count == 2
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 2


def test_probe_noop_discards_old_intent_without_caching_observation(app, player):
    assert stremio.handle_pause(app, "") is True
    player.observed = True
    player.paused = False
    assert stremio.handle_resume(app, "") is True
    assert player.send.call_count == 1
    assert sc._playback_memory is None
    player.observed = False
    assert stremio.handle_resume(app, "") is True
    assert player.send.call_count == 2


def test_foreground_or_identity_drift_aborts_request_and_clears_memory(app, player):
    assert stremio.handle_resume(app, "") is True
    player.send.side_effect = None
    player.send.return_value = False  # AHK detects wrong exe, vanished HWND or lost focus.
    result = stremio.handle_pause(app, "")
    assert result.state is DispatchState.FAILED
    assert sc._playback_memory is None
    script = player.send.call_args.args[0]
    assert "WinActivate, ahk_id %target%" in script
    assert "WinWaitActive, ahk_id %target%,, 2" in script
    final_guard = script[script.index("Sleep, 150"):script.index("Send, {Space}")]
    assert 'WinActive("ahk_id " target)' in final_guard
    assert "targetPid != expectedPid" in final_guard
    assert 'targetExe != "stremio-shell-ng.exe"' in final_guard
    assert 'targetExe != "stremio.exe"' in final_guard
    assert "ExitApp, 1" in final_guard
    assert "WinActivate, Stremio" not in script
    player.send.return_value = True
    assert stremio.handle_resume(app, "") is True  # Failed send must not retain old resume.
    assert player.send.call_count == 3


@pytest.mark.parametrize("phrase", ["toggle stremio", "play pause stremio"])
def test_explicit_toggle_routes_and_always_sends_space(app, player, phrase):
    plugin_commands._reinstall_module_commands(stremio)
    registry = {key: entry for key, entry in plugin_commands._REGISTRY.items()
                if entry["source"] == stremio.__name__}
    matcher = CommandMatcher()
    matcher.load_plugins(registry)
    matcher.freeze()
    entry, remainder = matcher.match(phrase, MatchContext.for_app("notepad.exe"))
    assert entry is not None and entry.phrase == "toggle stremio"
    assert entry.handler(app, remainder) is True
    assert entry.handler(app, remainder) is True
    assert player.send.call_count == 2
    script = player.send.call_args.args[0]
    assert script.count("Send, {Space}") == 1
    assert "target := 101\nexpectedPid := 202" in script
    matcher.set_enabled_packs({"core"})
    assert matcher.match(phrase, MatchContext.for_app("notepad.exe"))[0] is None


def test_toggle_flips_memory_in_both_directions(app, player):
    assert stremio.handle_pause(app, "") is True
    assert stremio.handle_toggle_stremio(app, "") is True
    assert sc._playback_memory.paused is False
    assert stremio.handle_resume(app, "") is True
    assert player.send.call_count == 2
    assert stremio.handle_toggle_stremio(app, "") is True
    assert sc._playback_memory.paused is True
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 3


def test_toggle_uses_real_probe_before_flipping_memory(app, player):
    assert stremio.handle_pause(app, "") is True
    player.observed, player.paused = True, False
    assert stremio.handle_toggle_stremio(app, "") is True
    assert player.paused is True
    assert sc._playback_memory.paused is True


def test_toggle_with_unknown_or_expired_memory_does_not_invent_state(app, player):
    assert stremio.handle_toggle_stremio(app, "") is True
    assert sc._playback_memory is None
    assert stremio.handle_pause(app, "") is True
    player.now += sc.PLAYBACK_MEMORY_SECONDS
    assert stremio.handle_toggle_stremio(app, "") is True
    assert sc._playback_memory is None
    assert stremio.handle_pause(app, "") is True
    assert player.send.call_count == 4


def test_toggle_missing_window_reports_failure_and_resets_memory(app, player):
    assert stremio.handle_pause(app, "") is True
    player.available = False
    result = stremio.handle_toggle_stremio(app, "")
    assert result.state is DispatchState.FAILED
    assert sc._playback_memory is None
    app.audio_coordinator.speak.assert_called_with(
        "Stremio playback control unavailable", category="error")
    script = player.send.call_args.args[0]
    assert 'WinExist("ahk_exe stremio-shell-ng.exe")' in script
    assert 'WinExist("ahk_exe stremio.exe")' in script
    assert "if (!target || !expectedPid)\n    ExitApp, 1" in script


@pytest.mark.parametrize("exe", ["stremio-shell-ng.exe", "stremio.exe"])
def test_native_discovery_checks_executable_visibility_creation_and_closes_handles(monkeypatch, exe):
    import ctypes
    if not hasattr(ctypes, "WinDLL"):
        pytest.skip("Win32 ABI test")
    kernel, user = Mock(), Mock()
    # Include a misleading title (irrelevant), a hidden player and denied process.
    names = {101: "notepad.exe", 102: exe, 103: exe, 104: exe}
    user.EnumWindows.side_effect = lambda callback, _: all(callback(hwnd, 0) for hwnd in names)
    user.IsWindowVisible.side_effect = lambda hwnd: hwnd != 102
    def get_pid(hwnd, pid):
        pid._obj.value = hwnd + 100
        return 1
    user.GetWindowThreadProcessId.side_effect = get_pid
    kernel.OpenProcess.side_effect = lambda access, inherit, pid: 0 if pid == 203 else pid
    def get_path(handle, flags, path, size):
        path.value = "C:\\Apps\\" + names[handle - 100]
        size._obj.value = len(path.value)
        return True
    kernel.QueryFullProcessImageNameW.side_effect = get_path
    def get_times(handle, created, *rest):
        created._obj.dwHighDateTime = 1
        created._obj.dwLowDateTime = 7
        return True
    kernel.GetProcessTimes.side_effect = get_times
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_: user if name == "user32" else kernel)
    assert sc._find_playback_target() == sc.PlaybackTarget(104, 204, (1 << 32) | 7)
    assert [call.args[0] for call in kernel.CloseHandle.call_args_list] == [201, 204]
    assert kernel.OpenProcess.call_args.args == (0x1000, False, 204)


def test_native_discovery_failure_returns_no_target(monkeypatch):
    import ctypes
    if not hasattr(ctypes, "WinDLL"):
        pytest.skip("Win32 ABI test")
    monkeypatch.setattr(ctypes, "WinDLL", Mock(side_effect=OSError("unavailable")))
    assert sc._find_playback_target() is None
