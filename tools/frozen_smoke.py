"""Frozen-build smoke harness for Samsara.

Runs OUTSIDE the frozen app. Launches a built Samsara.exe against an
isolated temp profile (via the SAMSARA_HOME_DIR env override, see
samsara/paths.py) and drives it through boot + basic liveness checks --
the class of thing that only breaks in the frozen build, never in dev:
config_path resolving into a wiped _internal\\, the first-run wizard firing
on every rebuild, import-order/hidden-import failures, missing data files,
and duplicate-logging-handler bugs.

This harness NEVER touches the real ~/.samsara -- every launch gets its own
temp profile directory.

Usage:
    F:\\envs\\sami\\python.exe tools\\frozen_smoke.py dist\\Samsara

Exit code 0 if every check passes, 1 otherwise. Prints one PASS/FAIL line
per check.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

EXE_NAME = "Samsara.exe"

# Exact strings emitted by dictation.py -- see samsara.log format
# "%(asctime)s - %(levelname)s - %(message)s".
BOOT_MARKER = "[INIT] Startup complete."
ALREADY_RUNNING_MARKER = "Samsara is already running"
WIZARD_START_MARKER = "First run detected - launching setup wizard..."
# NOTE: "[WIZ-DIAG]" is NOT a reliable "the wizard fired" signal on its own --
# qt_runtime.py's post() helper logs a "[WIZ-DIAG] post(): ..." line on every
# call regardless of caller (splash screen included), so it appears on every
# normal boot too. The only unambiguous "the wizard fired" signal is
# WIZARD_START_MARKER, which dictation.py logs exactly once, right before
# invoking FirstRunWizardQt, and nowhere else. Generic crash markers only here.
FAIL_MARKERS = ("Traceback", "CRITICAL")

BOOT_TIMEOUT_S = 90.0
WIZARD_SURVIVE_S = 15.0
LOG_STABILITY_WINDOW_S = 5.0
LOG_STABILITY_MAX_NEW_LINES = 200
LOG_POLL_INTERVAL_S = 0.25
SHUTDOWN_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Pure helpers -- unit tested directly in tests/test_frozen_smoke_unit.py
# ---------------------------------------------------------------------------

def make_min_config() -> dict:
    """Minimal config.json that boots without triggering the first-run
    wizard. Derived from dictation.py's own wizard-skip condition
    (first_run_complete truthy + microphone non-null) and default_config
    fill-in for everything else -- not copied from any real profile."""
    return {"first_run_complete": True, "microphone": 0}


def read_new_lines(log_path: Path, offset: int) -> tuple[list[str], int]:
    """Return (lines appended since byte offset, new offset). Tolerant of
    the file not existing yet (rotating file handler hasn't created it)."""
    if not log_path.exists():
        return [], offset
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        data = f.read()
        new_offset = f.tell()
    if not data:
        return [], new_offset
    return data.splitlines(), new_offset


def find_marker(lines: list[str], marker: str) -> str | None:
    """First line containing marker, or None."""
    for line in lines:
        if marker in line:
            return line
    return None


def find_any_marker(lines: list[str], markers: tuple[str, ...]) -> str | None:
    """First line containing any of markers, or None."""
    for line in lines:
        for marker in markers:
            if marker in line:
                return line
    return None


def count_occurrences(text: str, marker: str) -> int:
    return text.count(marker)


def find_self_respawn(child_process_names: list[str]) -> list[str]:
    """Given the names of a launched process's (recursive) child processes,
    return the ones that look like a self-spawned copy of the app."""
    return [n for n in child_process_names if n.lower() == EXE_NAME.lower()]


def log_growth_exceeds_bound(new_line_count: int, max_lines: int = LOG_STABILITY_MAX_NEW_LINES) -> bool:
    return new_line_count >= max_lines


# ---------------------------------------------------------------------------
# Check bookkeeping
# ---------------------------------------------------------------------------

class Check:
    def __init__(self, name: str):
        self.name = name
        self.passed: bool | None = None
        self.detail = ""

    def ok(self, detail: str = "") -> "Check":
        self.passed = True
        self.detail = detail
        return self

    def fail(self, detail: str = "") -> "Check":
        self.passed = False
        self.detail = detail
        return self

    def line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        suffix = f" -- {self.detail}" if self.detail else ""
        return f"[{status}] {self.name}{suffix}"


# ---------------------------------------------------------------------------
# Log watching
# ---------------------------------------------------------------------------

def wait_for_boot(log_path: Path, timeout_s: float, wizard_expected: bool = False):
    """Poll log_path until a terminal marker or timeout.

    Returns (outcome, detail, offset) where outcome is one of:
      "boot"            -- BOOT_MARKER seen (only when wizard_expected is False)
      "wizard"          -- WIZARD_START_MARKER seen (only when wizard_expected is True)
      "already_running" -- another instance holds the global single-instance lock
      "fail"            -- a failure marker appeared
      "timeout"         -- none of the above within timeout_s
    """
    deadline = time.monotonic() + timeout_s
    offset = 0
    while time.monotonic() < deadline:
        lines, offset = read_new_lines(log_path, offset)
        if lines:
            already = find_marker(lines, ALREADY_RUNNING_MARKER)
            if already:
                return "already_running", already, offset
            bad = find_any_marker(lines, FAIL_MARKERS)
            if bad:
                return "fail", bad, offset
            if wizard_expected:
                wiz = find_marker(lines, WIZARD_START_MARKER)
                if wiz:
                    return "wizard", wiz, offset
            else:
                # The wizard firing at all during a normal boot (valid,
                # already-complete config pre-seeded) is itself a regression.
                unexpected_wizard = find_marker(lines, WIZARD_START_MARKER)
                if unexpected_wizard:
                    return "fail", unexpected_wizard, offset
                boot = find_marker(lines, BOOT_MARKER)
                if boot:
                    return "boot", boot, offset
        time.sleep(LOG_POLL_INTERVAL_S)
    return "timeout", None, offset


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def launch(exe_path: Path, home_dir: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["SAMSARA_HOME_DIR"] = str(home_dir)
    return subprocess.Popen(
        [str(exe_path)],
        cwd=str(exe_path.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def check_no_self_respawn(pid: int) -> Check:
    check = Check("exactly one Samsara.exe process (no self-respawn)")
    if psutil is None:
        return check.fail("psutil not installed -- skipped")
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return check.fail("launched process not found")
    try:
        children = proc.children(recursive=True)
    except psutil.Error as e:
        return check.fail(f"could not enumerate child processes: {e}")
    child_names = []
    for c in children:
        try:
            child_names.append(c.name())
        except psutil.Error:
            continue
    copies = find_self_respawn(child_names)
    if copies:
        return check.fail(f"{len(copies)} self-spawned child process(es) detected")
    return check.ok("no child Samsara.exe processes")


def cleanup_stale_lock(pid: int) -> None:
    """Delete %TEMP%\\samsara.lock if it names the PID we just terminated.

    terminate()/kill() (TerminateProcess) release the OS-level msvcrt lock
    on the file immediately, but nothing unlinks the file itself, so its
    stale PID sits there until something removes it. dictation.py's own
    _steal_stale_lock_if_any() would catch this on the *next* launch too,
    but back-to-back harness runs (this scenario, then the wizard scenario,
    in the same smoke-test invocation) start their next launch fast enough
    that cleaning it up here immediately is worth doing rather than relying
    on that later.
    """
    lock_path = Path(tempfile.gettempdir()) / "samsara.lock"
    if not lock_path.exists():
        return
    try:
        recorded_pid = int(lock_path.read_text().strip())
    except (OSError, ValueError):
        return
    if recorded_pid == pid:
        try:
            lock_path.unlink()
        except OSError:
            pass


def graceful_shutdown(proc: subprocess.Popen, timeout_s: float = SHUTDOWN_TIMEOUT_S) -> Check:
    """No scriptable graceful-exit mechanism exists: quit_app() (dictation.py)
    is only reachable via the tray "Exit" menu item (a GUI click) or a fatal
    startup-error dialog; there is no signal handler, named event/mutex, or
    CLI flag registered anywhere in the app. Confirmed by reading
    quit_app()/tray_qt.py and grepping for signal.signal/win32event usage.
    Falling back to terminate() (TerminateProcess) -- identical to a
    Task-Manager "End task": no atexit/signal/app-level cleanup runs."""
    check = Check("clean shutdown")
    proc.terminate()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            return check.fail(f"process survived terminate() + kill() past {timeout_s + 5:.0f}s")
        cleanup_stale_lock(proc.pid)
        return check.fail(
            f"did not exit within {timeout_s:.0f}s of terminate() "
            f"(no scriptable graceful-exit mechanism exists); force-killed"
        )
    cleanup_stale_lock(proc.pid)
    return check.ok(
        "terminate() -- no scriptable graceful-exit mechanism exists "
        f"(tray-menu Exit is GUI-only); exited within {timeout_s:.0f}s"
    )


def check_no_orphans(pid: int, exe_path: Path) -> Check:
    check = Check("no orphaned Samsara.exe processes after exit")
    if psutil is None:
        return check.fail("psutil not installed -- skipped")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            break
        time.sleep(0.25)
    else:
        return check.fail(f"pid {pid} still exists 10s after shutdown attempt")
    orphans = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if p.info["name"] and p.info["name"].lower() == EXE_NAME.lower():
                if p.info["exe"] and Path(p.info["exe"]).resolve() == exe_path.resolve():
                    orphans.append(p.info["pid"])
        except (psutil.Error, OSError):
            continue
    if orphans:
        return check.fail(f"{len(orphans)} orphaned process(es) still running: pids={orphans}")
    return check.ok("no orphaned processes")


# ---------------------------------------------------------------------------
# Scenario 1: normal boot + liveness
# ---------------------------------------------------------------------------

def run_boot_and_liveness(exe_path: Path, work_root: Path) -> list[Check]:
    checks: list[Check] = []
    profile_dir = work_root / "profile_boot"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.json").write_text(
        json.dumps(make_min_config(), indent=2), encoding="utf-8"
    )
    log_path = profile_dir / "logs" / "samsara.log"

    proc = launch(exe_path, profile_dir)
    try:
        outcome, detail, offset = wait_for_boot(log_path, BOOT_TIMEOUT_S)

        if outcome == "already_running":
            checks.append(Check("boot").fail(
                f"another Samsara instance is already running system-wide "
                f"(the single-instance lock at %TEMP%\\samsara.lock is not "
                f"profile-scoped) -- close it and re-run: {detail!r}"
            ))
            return checks
        if outcome == "fail":
            checks.append(Check("boot").fail(f"failure marker in log: {detail!r}"))
            return checks
        if outcome == "timeout":
            checks.append(Check("boot").fail(f'no "{BOOT_MARKER}" within {BOOT_TIMEOUT_S:.0f}s'))
            return checks
        checks.append(Check("boot").ok(detail))

        # c) process still alive
        alive = proc.poll() is None
        proc_check = Check("process alive after boot")
        checks.append(proc_check.ok() if alive else proc_check.fail(
            f"process exited with code {proc.returncode}"
        ))
        if not alive:
            return checks

        # d) exactly one Samsara.exe for this launch
        checks.append(check_no_self_respawn(proc.pid))

        # e) log line count stable-ish over a window; also re-scan that
        # window for failure markers appearing shortly after boot.
        new_lines, offset = read_new_lines(log_path, offset)
        time.sleep(LOG_STABILITY_WINDOW_S)
        more_lines, offset = read_new_lines(log_path, offset)
        window_lines = new_lines + more_lines
        stability_check = Check("log line count stable after boot")
        bad = find_any_marker(window_lines, FAIL_MARKERS)
        if bad:
            stability_check.fail(f"failure marker appeared shortly after boot: {bad!r}")
        elif log_growth_exceeds_bound(len(window_lines)):
            stability_check.fail(
                f"{len(window_lines)} new log lines in {LOG_STABILITY_WINDOW_S:.0f}s "
                f"(>= {LOG_STABILITY_MAX_NEW_LINES}) -- possible runaway logging loop"
            )
        else:
            stability_check.ok(f"{len(window_lines)} new log lines in {LOG_STABILITY_WINDOW_S:.0f}s")
        checks.append(stability_check)

        # Duplicate-handler regression gate: the one-shot boot line must
        # appear exactly once in the whole log.
        full_text = log_path.read_text(encoding="utf-8", errors="replace")
        dup_check = Check('"[INIT] Startup complete." appears exactly once')
        count = count_occurrences(full_text, BOOT_MARKER)
        if count == 1:
            dup_check.ok()
        else:
            dup_check.fail(f"appeared {count} times -- possible duplicate log handler")
        checks.append(dup_check)

    finally:
        if proc.poll() is None:
            checks.append(graceful_shutdown(proc))
            checks.append(check_no_orphans(proc.pid, exe_path))
        else:
            checks.append(Check("clean shutdown").ok("process had already exited"))

    return checks


# ---------------------------------------------------------------------------
# Scenario 2: first-run wizard path (second launch, no config)
# ---------------------------------------------------------------------------

def run_wizard_path(exe_path: Path, work_root: Path) -> list[Check]:
    checks: list[Check] = []
    profile_dir = work_root / "profile_wizard"
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True)
    # Deliberately no config.json -- the wizard must fire.
    log_path = profile_dir / "logs" / "samsara.log"

    proc = launch(exe_path, profile_dir)
    try:
        outcome, detail, offset = wait_for_boot(log_path, WIZARD_SURVIVE_S, wizard_expected=True)

        if outcome == "already_running":
            checks.append(Check("wizard path").fail(
                f"another Samsara instance is already running system-wide -- "
                f"close it and re-run: {detail!r}"
            ))
            return checks
        if outcome == "fail":
            checks.append(Check("wizard path").fail(f"crash/traceback during wizard boot: {detail!r}"))
            return checks

        wizard_check = Check("first-run wizard started")
        if outcome == "wizard":
            wizard_check.ok(detail)
        else:
            wizard_check.fail(f'no "{WIZARD_START_MARKER}" seen within {WIZARD_SURVIVE_S:.0f}s')
        checks.append(wizard_check)

        # Regardless of whether we saw the start marker, keep watching for
        # the remainder of the window for a crash, and confirm the process
        # is still alive at the end -- "surviving + no exception is the bar."
        deadline = time.monotonic() + WIZARD_SURVIVE_S
        crashed_line = None
        while time.monotonic() < deadline:
            lines, offset = read_new_lines(log_path, offset)
            bad = find_any_marker(lines, ("Traceback", "CRITICAL"))
            if bad:
                crashed_line = bad
                break
            if proc.poll() is not None:
                break
            time.sleep(LOG_POLL_INTERVAL_S)

        survive_check = Check(f"wizard process survives {WIZARD_SURVIVE_S:.0f}s without crashing")
        if crashed_line:
            survive_check.fail(f"failure marker: {crashed_line!r}")
        elif proc.poll() is not None:
            survive_check.fail(f"process exited early with code {proc.returncode}")
        else:
            survive_check.ok()
        checks.append(survive_check)

    finally:
        if proc.poll() is None:
            checks.append(graceful_shutdown(proc))
            checks.append(check_no_orphans(proc.pid, exe_path))
        else:
            checks.append(Check("clean shutdown").ok("process had already exited"))

    return checks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_path", help=r"Path to the PyInstaller onedir output, e.g. dist\Samsara")
    args = parser.parse_args(argv)

    dist_path = Path(args.dist_path).resolve()
    exe_path = dist_path / EXE_NAME
    if not exe_path.exists():
        print(f"[FAIL] locate {EXE_NAME} -- not found at {exe_path}")
        return 1

    work_root = Path(tempfile.mkdtemp(prefix="samsara_smoke_"))
    print(f"(isolated temp profile root: {work_root})")

    all_checks: list[Check] = []
    all_checks.extend(run_boot_and_liveness(exe_path, work_root))
    all_checks.extend(run_wizard_path(exe_path, work_root))

    for check in all_checks:
        print(check.line())

    passed = all(c.passed for c in all_checks)
    if passed:
        shutil.rmtree(work_root, ignore_errors=True)
    else:
        print(f"(one or more checks failed -- temp profile left at {work_root} for inspection)")

    print(f"RESULT: {'PASS' if passed else 'FAIL'} ({sum(c.passed for c in all_checks)}/{len(all_checks)} checks passed)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
