"""Tests for tools.stremio_control -- AHK script GENERATION only.

Never invokes AHK_EXE / subprocess.run against real AutoHotkey. Verifies
the generated script text (SetTitleMatchMode/WinActivate/expected Send
key), matching plugins/commands/stremio.py's original, empirically-verified
scripts exactly. tools/ is not a samsara package -- imported as a plain
namespace package, no samsara imports anywhere in this file either.
"""
from unittest.mock import patch

import pytest

from tools import stremio_control as sc


class TestKeyScriptGeneration:
    def test_contains_title_match_and_activate(self):
        script = sc._build_key_script("Space")
        assert "SetTitleMatchMode, 2" in script
        assert "WinActivate, Stremio" in script
        assert "WinWaitActive, Stremio,, 2" in script

    def test_exits_nonzero_when_window_not_found(self):
        script = sc._build_key_script("Space")
        assert "if ErrorLevel" in script
        assert "ExitApp, 1" in script

    def test_exits_zero_after_send(self):
        script = sc._build_key_script("Space")
        assert "ExitApp, 0" in script

    @pytest.mark.parametrize("key", ["Space", "f", "m"])
    def test_sends_exact_key(self, key):
        script = sc._build_key_script(key)
        assert f"Send, {{{key}}}" in script


class TestSendBodyScriptGeneration:
    def test_contains_title_match_and_activate(self):
        script = sc._build_send_body_script("Send, {Right 6}")
        assert "SetTitleMatchMode, 2" in script
        assert "WinActivate, Stremio" in script

    def test_embeds_send_body_verbatim(self):
        script = sc._build_send_body_script("Send, {Right 6}")
        assert "Send, {Right 6}" in script

    def test_embeds_win_shift_right_verbatim(self):
        """#+{Right} (Win+Shift+Right, AHK v1 modifier syntax: # = Win,
        + = Shift, prefixed directly onto the key name) must survive
        untouched -- {#+Right} would be invalid AHK."""
        script = sc._build_send_body_script("Send, #+{Right}")
        assert "Send, #+{Right}" in script
        assert "{#+Right}" not in script


class TestPublicControlFunctions:
    """Each public function delegates to _run_ahk with the expected script
    shape -- mock _run_ahk so no subprocess/AHK ever runs."""

    def test_pause_play_sends_space(self):
        with patch.object(sc, "_run_ahk", return_value=True) as mock_run:
            assert sc.pause_play() is True
        script = mock_run.call_args.args[0]
        assert "Send, {Space}" in script

    def test_fullscreen_sends_f(self):
        with patch.object(sc, "_run_ahk", return_value=True) as mock_run:
            assert sc.fullscreen() is True
        script = mock_run.call_args.args[0]
        assert "Send, {f}" in script

    def test_mute_sends_m(self):
        with patch.object(sc, "_run_ahk", return_value=True) as mock_run:
            assert sc.mute() is True
        script = mock_run.call_args.args[0]
        assert "Send, {m}" in script

    def test_skip_forward_sends_right_6(self):
        """Mirrors the original plugin's handle_skip_forward exactly:
        6x Right-arrow presses."""
        with patch.object(sc, "_run_ahk", return_value=True) as mock_run:
            assert sc.skip_forward() is True
        script = mock_run.call_args.args[0]
        assert "Send, {Right 6}" in script

    def test_skip_back_sends_left_2(self):
        """Mirrors the original plugin's handle_skip_back exactly:
        2x Left-arrow presses (intentionally asymmetric with
        skip_forward -- inherited as-is, not reconciled)."""
        with patch.object(sc, "_run_ahk", return_value=True) as mock_run:
            assert sc.skip_back() is True
        script = mock_run.call_args.args[0]
        assert "Send, {Left 2}" in script

    def test_control_function_propagates_run_ahk_failure(self):
        with patch.object(sc, "_run_ahk", return_value=False):
            assert sc.pause_play() is False

    def test_volume_up_sends_up(self):
        """Verified against the current Stremio player 2026-07-10:
        Up arrow = volume +10%."""
        with patch.object(sc, "_run_ahk", return_value=True) as mock_run:
            assert sc.volume_up() is True
        script = mock_run.call_args.args[0]
        assert "Send, {Up}" in script

    def test_volume_down_sends_down(self):
        """Verified against the current Stremio player 2026-07-10:
        Down arrow = volume -10%."""
        with patch.object(sc, "_run_ahk", return_value=True) as mock_run:
            assert sc.volume_down() is True
        script = mock_run.call_args.args[0]
        assert "Send, {Down}" in script

    def test_switch_monitor_sends_win_shift_right(self):
        """Win+Shift+Right moves the focused window to the next monitor
        (cycles). AHK v1 modifier syntax: #+{Right}, not {#+Right}."""
        with patch.object(sc, "_run_ahk", return_value=True) as mock_run:
            assert sc.switch_monitor() is True
        script = mock_run.call_args.args[0]
        assert "Send, #+{Right}" in script


class TestIsStremioRunning:
    def test_true_when_process_name_present(self):
        fake_output = (
            '"stremio-shell-ng.exe","1234","Console","1","50,000 K"\n'
        )
        with patch.object(sc.subprocess, "run") as mock_run:
            mock_run.return_value.stdout = fake_output
            assert sc.is_stremio_running() is True

    def test_true_when_only_runtime_process_present(self):
        fake_output = '"stremio-runtime.exe","5678","Console","1","10,000 K"\n'
        with patch.object(sc.subprocess, "run") as mock_run:
            mock_run.return_value.stdout = fake_output
            assert sc.is_stremio_running() is True

    def test_false_when_absent(self):
        fake_output = '"notepad.exe","1111","Console","1","5,000 K"\n'
        with patch.object(sc.subprocess, "run") as mock_run:
            mock_run.return_value.stdout = fake_output
            assert sc.is_stremio_running() is False

    def test_false_when_stale_process_name_only(self):
        # The OLD (now-fixed) process name must not false-positive.
        fake_output = '"stremio.exe","2222","Console","1","20,000 K"\n'
        with patch.object(sc.subprocess, "run") as mock_run:
            mock_run.return_value.stdout = fake_output
            assert sc.is_stremio_running() is False

    def test_fails_closed_on_exception(self):
        with patch.object(sc.subprocess, "run", side_effect=OSError("boom")):
            assert sc.is_stremio_running() is False


class TestKillStremio:
    def test_kills_both_known_process_names(self):
        with patch.object(sc.subprocess, "run") as mock_run:
            sc.kill_stremio()
        killed = [call.args[0][2] for call in mock_run.call_args_list]
        assert "stremio-shell-ng.exe" in killed
        assert "stremio-runtime.exe" in killed
        assert "stremio.exe" not in killed

    def test_never_raises_on_taskkill_failure(self):
        with patch.object(sc.subprocess, "run", side_effect=OSError("boom")):
            sc.kill_stremio()  # must not raise
