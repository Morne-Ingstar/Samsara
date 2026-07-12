"""Tests for dictation.py's single-instance lock stealing logic.

Covers _is_samsara_process() (liveness + identity check) and
_steal_stale_lock_if_any() (the pre-check that runs before the OS-level
msvcrt/fcntl lock acquisition in _check_single_instance()). Doesn't touch
the real %TEMP%\\samsara.lock -- every test uses a tmp_path lock file, and
process liveness/identity is mocked via psutil rather than spawning real
processes.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import psutil
import pytest

import dictation


# ---------------------------------------------------------------------------
# _is_samsara_process
# ---------------------------------------------------------------------------

def test_dead_pid_is_not_samsara():
    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(99999)):
        assert dictation._is_samsara_process(99999) is False


def test_access_denied_is_not_samsara():
    with patch("psutil.Process", side_effect=psutil.AccessDenied(99999)):
        assert dictation._is_samsara_process(99999) is False


def test_alive_samsara_exe_is_samsara():
    proc = MagicMock()
    proc.name.return_value = "Samsara.exe"
    with patch("psutil.Process", return_value=proc):
        assert dictation._is_samsara_process(1234) is True


def test_alive_unrelated_process_is_not_samsara():
    """PID reuse: some other process (e.g. explorer.exe) now owns a PID
    that used to belong to Samsara. Liveness alone must not be enough."""
    proc = MagicMock()
    proc.name.return_value = "explorer.exe"
    with patch("psutil.Process", return_value=proc):
        assert dictation._is_samsara_process(1234) is False


def test_alive_dev_mode_python_running_dictation_is_samsara():
    proc = MagicMock()
    proc.name.return_value = "python.exe"
    proc.cmdline.return_value = ["F:\\envs\\sami\\python.exe", "dictation.py"]
    with patch("psutil.Process", return_value=proc):
        assert dictation._is_samsara_process(5678) is True


def test_alive_unrelated_python_process_is_not_samsara():
    """Some other python.exe (a venv tool, a script, hermes-agent, etc.)
    reused the PID -- must not be mistaken for a dev-mode Samsara."""
    proc = MagicMock()
    proc.name.return_value = "python.exe"
    proc.cmdline.return_value = ["python.exe", "server.py"]
    with patch("psutil.Process", return_value=proc):
        assert dictation._is_samsara_process(5678) is False


def test_cmdline_lookup_failure_is_not_samsara():
    proc = MagicMock()
    proc.name.return_value = "python.exe"
    proc.cmdline.side_effect = psutil.AccessDenied(5678)
    with patch("psutil.Process", return_value=proc):
        assert dictation._is_samsara_process(5678) is False


# ---------------------------------------------------------------------------
# _steal_stale_lock_if_any
# ---------------------------------------------------------------------------

def test_no_lock_file_is_a_noop(tmp_path):
    lock_path = tmp_path / "samsara.lock"
    assert not lock_path.exists()
    dictation._steal_stale_lock_if_any(lock_path)  # must not raise
    assert not lock_path.exists()


def test_dead_pid_lock_gets_stolen(tmp_path, caplog):
    lock_path = tmp_path / "samsara.lock"
    lock_path.write_text("424242")

    with patch("dictation._is_samsara_process", return_value=False):
        with caplog.at_level("INFO"):
            dictation._steal_stale_lock_if_any(lock_path)

    assert not lock_path.exists(), "stale lock file should have been deleted"
    assert any(
        "stale lock from PID 424242" in r.message and "stealing" in r.message
        for r in caplog.records
    )


def test_alive_other_process_pid_lock_refuses_and_exits(tmp_path, caplog):
    """A live PID that is NOT Samsara (unrelated process reused the PID)
    must still be treated as stale and stolen -- liveness alone isn't
    enough, only _is_samsara_process's identity check matters here."""
    lock_path = tmp_path / "samsara.lock"
    lock_path.write_text("13579")

    with patch("dictation._is_samsara_process", return_value=False):
        with caplog.at_level("INFO"):
            dictation._steal_stale_lock_if_any(lock_path)

    assert not lock_path.exists()
    assert any("stale lock from PID 13579" in r.message for r in caplog.records)


def test_alive_samsara_pid_lock_refuses_without_hanging(tmp_path, caplog):
    lock_path = tmp_path / "samsara.lock"
    lock_path.write_text("777")

    with patch("dictation._is_samsara_process", return_value=True):
        with caplog.at_level("WARNING"):
            with pytest.raises(SystemExit) as exc_info:
                dictation._steal_stale_lock_if_any(lock_path)

    assert exc_info.value.code == 0
    assert lock_path.exists(), "a genuinely live instance's lock must not be deleted"
    assert any(
        "Samsara is already running" in r.message and "777" in r.message
        for r in caplog.records
    )


def test_unreadable_lock_file_is_treated_as_stale(tmp_path):
    lock_path = tmp_path / "samsara.lock"
    lock_path.write_text("not-a-pid")

    # _is_samsara_process must never even be consulted -- an unparseable
    # PID can't be resolved to any process to check.
    with patch("dictation._is_samsara_process") as mock_check:
        dictation._steal_stale_lock_if_any(lock_path)
        mock_check.assert_not_called()

    assert not lock_path.exists()


def test_empty_lock_file_is_treated_as_stale(tmp_path):
    lock_path = tmp_path / "samsara.lock"
    lock_path.write_text("")
    dictation._steal_stale_lock_if_any(lock_path)
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# _check_single_instance end-to-end (steal path only exercises non-blocking
# code -- confirms the hang theory is refuted: nothing here waits/sleeps)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt locking is Windows-only")
def test_check_single_instance_steals_dead_pid_lock_and_acquires(tmp_path, monkeypatch):
    # conftest.py forces SAMSARA_HOME_DIR globally for test isolation, but
    # this test exercises the DEFAULT "samsara.lock" filename -- with
    # SAMSARA_HOME_DIR set, _check_single_instance() derives a hashed
    # lock_name instead, so it must be removed here to test the true default.
    monkeypatch.delenv("SAMSARA_HOME_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    lock_path = tmp_path / "samsara.lock"
    lock_path.write_text("999999")  # a PID essentially guaranteed dead

    with patch("dictation._is_samsara_process", return_value=False):
        handle = dictation._check_single_instance()

    assert handle is not None, "should have acquired the lock after stealing it"
    # msvcrt.locking() holds the byte range exclusively even against a
    # second handle from this same process/test -- close it (releasing the
    # lock) before reading the file back to check what got written.
    handle.close()
    assert lock_path.read_text().strip() == str(os.getpid())


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt locking is Windows-only")
def test_check_single_instance_exits_immediately_for_live_samsara_lock(tmp_path, monkeypatch):
    """Confirms the refusal path is an immediate exit, not a wait -- this is
    the "never hang" requirement: a live-Samsara lock must fail fast."""
    # See test_check_single_instance_steals_dead_pid_lock_and_acquires above:
    # must test the DEFAULT "samsara.lock" filename, not the SAMSARA_HOME_DIR
    # -derived hashed name conftest.py's global override would otherwise cause.
    monkeypatch.delenv("SAMSARA_HOME_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    lock_path = tmp_path / "samsara.lock"
    lock_path.write_text("888")

    import time
    with patch("dictation._is_samsara_process", return_value=True):
        start = time.monotonic()
        with pytest.raises(SystemExit) as exc_info:
            dictation._check_single_instance()
        elapsed = time.monotonic() - start

    assert exc_info.value.code == 0
    assert elapsed < 1.0, f"refusal took {elapsed:.2f}s -- should be instant, not a wait"
