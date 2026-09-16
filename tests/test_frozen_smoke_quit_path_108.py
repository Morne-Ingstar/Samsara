"""Queue 108: the smoke harness's shutdown check is named for what it does.

tools/frozen_smoke.py reported a proc.terminate() result as "clean shutdown".
On Windows terminate() is TerminateProcess -- a Task-Manager "End task". It
bypasses quit_app() completely, so it covers none of duck restoration,
draft/history flushing or thread teardown. The harness was therefore printing
a green line for the one thing it does not test.

108 renames it to "forced termination" and adds the check that can tell the
difference: samsara.boot writes SESSION_MARKER ("session.running") at startup
and quit_app deletes it through end_session() as its last act, so the marker
surviving IS the record that the quit path never ran. These tests drive both
sides on real child processes.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_MODULE_PATH = REPO / "tools" / "frozen_smoke.py"
_spec = importlib.util.spec_from_file_location("frozen_smoke", _MODULE_PATH)
frozen_smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frozen_smoke)

#: A child that opens a session the way __main__ does, tells us it is up,
#: then either ends the session the way quit_app does, or blocks so the test
#: can terminate it.
_CHILD = """
import sys, time
sys.path.insert(0, {repo!r})
import samsara.boot as boot
log_dir = {log_dir!r}
boot.begin_session(log_dir)
print("UP", flush=True)
{tail}
"""
_QUIT_TAIL = 'boot.end_session(log_dir)\nprint("QUIT", flush=True)'
_BLOCK_TAIL = "time.sleep(120)"


def _spawn(log_dir: Path, tail: str) -> subprocess.Popen:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(repo=str(REPO), log_dir=str(log_dir), tail=tail)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(REPO))
    assert proc.stdout.readline().strip() == "UP", "the child never opened a session"
    return proc


# ---------------------------------------------------------------------------
# The rename
# ---------------------------------------------------------------------------

def test_the_shutdown_check_is_named_for_what_it_does():
    source = _MODULE_PATH.read_text(encoding="utf-8", errors="replace")
    assert "def forced_termination(" in source
    assert "def graceful_shutdown(" not in source
    assert 'Check("forced termination")' in source
    # The only surviving mention of the old label is the comment recording
    # that it was wrong.
    assert source.count('"clean shutdown"') == 1
    assert "which is the opposite of the truth" in source


def test_every_call_site_uses_the_new_name():
    source = _MODULE_PATH.read_text(encoding="utf-8", errors="replace")
    assert source.count("forced_termination(proc)") == 2          # both scenarios
    assert source.count("check_quit_path(profile_dir, expected=False)") == 2
    assert "graceful_shutdown(" not in source


def test_the_check_says_what_terminate_does_not_cover(tmp_path):
    """The reported line has to carry the caveat, not just the name -- the
    harness output is what a release reader actually sees."""
    proc = _spawn(tmp_path / "logs", _BLOCK_TAIL)
    check = frozen_smoke.forced_termination(proc, timeout_s=15.0)
    assert check.name == "forced termination"
    assert check.passed
    detail = f"{check.name} {getattr(check, 'detail', '')}".lower()
    assert "quit_app() was not run" in detail
    assert "duck restoration" in detail


# ---------------------------------------------------------------------------
# The quit path, on real processes
# ---------------------------------------------------------------------------

def test_a_real_quit_clears_the_completion_marker(tmp_path):
    """The new frozen-process test: a child that runs the real session
    lifecycle to its end leaves no marker, and the harness reports it."""
    profile = tmp_path / "profile"
    marker = profile / "logs" / frozen_smoke.SESSION_MARKER

    proc = _spawn(profile / "logs", _QUIT_TAIL)
    assert marker.exists(), "begin_session should have written the marker"
    assert proc.stdout.readline().strip() == "QUIT"
    assert proc.wait(timeout=30) == 0

    assert not marker.exists()
    assert frozen_smoke.quit_path_completed(profile) is True
    assert frozen_smoke.check_quit_path(profile, expected=True).passed


def test_forced_termination_leaves_the_marker_and_the_harness_says_so(tmp_path):
    """The contrast that makes the marker meaningful: terminate() never runs
    end_session, so the marker survives and the harness reports that the quit
    path correctly did NOT run."""
    profile = tmp_path / "profile"
    marker = profile / "logs" / frozen_smoke.SESSION_MARKER

    proc = _spawn(profile / "logs", _BLOCK_TAIL)
    assert marker.exists()
    assert frozen_smoke.forced_termination(proc, timeout_s=15.0).passed

    assert marker.exists(), "TerminateProcess must not have run end_session"
    assert frozen_smoke.quit_path_completed(profile) is False
    check = frozen_smoke.check_quit_path(profile, expected=False)
    assert check.passed and "did NOT run" in check.name


def test_the_marker_check_fails_when_the_expectation_is_wrong(tmp_path):
    """Both directions, so a silently-always-passing check cannot hide here."""
    profile = tmp_path / "profile"
    (profile / "logs").mkdir(parents=True)
    assert frozen_smoke.check_quit_path(profile, expected=True).passed        # no marker
    assert not frozen_smoke.check_quit_path(profile, expected=False).passed

    (profile / "logs" / frozen_smoke.SESSION_MARKER).write_text("{}", encoding="utf-8")
    assert frozen_smoke.check_quit_path(profile, expected=False).passed       # marker present
    assert not frozen_smoke.check_quit_path(profile, expected=True).passed


def test_the_harness_marker_name_matches_the_app(tmp_path):
    """frozen_smoke duplicates the constant so it can drive a frozen exe
    without the source package on sys.path. Duplicated means it can drift."""
    from samsara import boot

    assert frozen_smoke.SESSION_MARKER == boot.SESSION_MARKER

    # And the name really is the file begin_session writes.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    boot.begin_session(log_dir)
    try:
        written = [p.name for p in log_dir.iterdir()]
        assert frozen_smoke.SESSION_MARKER in written
        payload = json.loads((log_dir / frozen_smoke.SESSION_MARKER).read_text(encoding="utf-8"))
        assert payload["pid"] > 0
    finally:
        boot.end_session(log_dir)
    assert not (log_dir / frozen_smoke.SESSION_MARKER).exists()


def test_a_child_that_never_quits_is_still_reported_as_not_quit(tmp_path):
    """The shape the harness actually meets: a live app killed after a wait.
    Guards against the marker being cleared by anything other than
    end_session (an atexit hook, say), which would make it useless."""
    profile = tmp_path / "profile"
    proc = _spawn(profile / "logs", _BLOCK_TAIL)
    time.sleep(0.5)
    proc.kill()
    proc.wait(timeout=30)
    assert frozen_smoke.quit_path_completed(profile) is False
