"""CI-only smoke check for the frozen Samsara build.

Runs OUTSIDE the frozen app, on a GitHub Actions windows-latest runner.
NOT a replacement for tools\\frozen_smoke.py -- that harness is the full
11-check local pre-release gate (boot, wizard path, no-self-respawn, log
stability, clean shutdown, no orphans) and should keep running locally via
tools\\build_and_smoke.cmd before every tagged release.

This script exists because most of frozen_smoke.py's checks assume things a
stock CI runner does not reliably have:

  - A real audio input device. dictation.py's __init__ unconditionally calls
    _start_ace_engine(), which opens a PortAudio/WASAPI input stream. If no
    device exists, the exception is caught and logged via
    logger.exception(...) -- the app degrades gracefully and keeps booting,
    but logger.exception() writes a "Traceback (most recent call last):"
    line either way. frozen_smoke.py's wait_for_boot() treats ANY
    "Traceback" substring in the log as a hard failure, so on a mic-less
    runner it would report FAIL even though the app is fine. That is a
    false negative, not a real regression -- see RELEASING.md.

  - A cached Whisper model. WhisperModel(...) loads (and, on a fresh
    machine, downloads from Hugging Face Hub) BEFORE "[INIT] Startup
    complete." is logged. On a first CI run this is a real network call of
    unknown duration -- not something a hard pass/fail gate should depend
    on this early in CI adoption.

So this script only asks the weaker, still-useful question: does the frozen
EXE start, stay alive (or explicitly reach the boot marker) for a bounded
window, and exit cleanly when asked -- with no *unexplained* crash. Its full
log is uploaded as a build artifact for a human to read regardless of
outcome. 2026-07-10: the workflow step that calls this now gates the release
(continue-on-error removed) -- a non-zero exit here stops the build before
packaging/upload, per the 2026-07-10 CRASH_MARKERS tightening above that
made this check trustworthy enough to trust with that.

Usage:
    python tools\\ci_smoke.py dist\\Samsara [--timeout 180]
"""
from __future__ import annotations

import argparse
import json
import os
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
BOOT_MARKER = "[INIT] Startup complete."
ALREADY_RUNNING_MARKER = "Samsara is already running"
KNOWN_BENIGN_MARKERS = (
    # Caught-and-logged, not a crash -- see module docstring. Each entry
    # is a substring that must appear in a line at or before the
    # "Traceback" line itself (see LogScanner.feed's recent-lines
    # lookback) to excuse it -- do not broaden past the mic-less-audio-
    # device pattern, per the 2026-07-10 tightening: a wider benign list
    # is exactly how an import-crash could green-pass again.
    "[ACE] Engine failed to start",
    # 2026-07-10: a real CI run on a mic-less windows-latest runner showed
    # this single marker was already too NARROW -- the actual current
    # audio-init code path (dictation.py's _detect_capture_rate /
    # _run_calibration_if_auto / _start_sound_stream) logs THREE separate
    # sounddevice.PortAudioError tracebacks under different ERROR-level
    # prefixes ("[WARN] Could not query device...", "[CAL] Calibration
    # failed...", "[AUDIO] Failed to start sound stream..."), none of
    # which matched "[ACE] Engine failed to start" -- so ci_smoke failed a
    # build with no real bug (see tools/test_ci_smoke.py's real-log-based
    # regression test for the exact fixture that caught this).
    "PortAudioError",
    "Error querying device",
)
# 2026-07-10 tightening: "Traceback"/"CRITICAL" alone were only ever
# observable if the app got far enough to write its OWN log file.
# PyInstaller's bootloader failure (the actual symptom of a missing frozen
# dependency, e.g. ModuleNotFoundError for PySide6) fires BEFORE Python
# logging is configured and prints "Failed to execute script '<name>' due
# to unhandled exception: ..." to native STDERR, not the app log -- which
# this script used to discard outright (stderr=subprocess.DEVNULL). See
# launch()/_scan_stderr() below: stderr is now captured and scanned too.
CRASH_MARKERS = ("Traceback", "CRITICAL", "ModuleNotFoundError", "Failed to execute script")
DEFAULT_TIMEOUT_S = 180.0
POLL_INTERVAL_S = 0.5
SHUTDOWN_TIMEOUT_S = 15.0


def make_min_config() -> dict:
    return {"first_run_complete": True, "microphone": 0}


def read_new_lines(log_path: Path, offset: int) -> tuple[list[str], int]:
    if not log_path.exists():
        return [], offset
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        data = f.read()
        new_offset = f.tell()
    if not data:
        return [], new_offset
    return data.splitlines(), new_offset


def launch(exe_path: Path, home_dir: Path, stderr_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["SAMSARA_HOME_DIR"] = str(home_dir)
    # stderr goes to a real file, not DEVNULL: this is the ONLY channel a
    # PyInstaller bootloader failure ("Failed to execute script ... due to
    # unhandled exception: No module named 'X'") is written to -- it fires
    # before the app's own log file exists. A file (not subprocess.PIPE)
    # avoids any risk of pipe-buffer deadlock from an app that writes more
    # to stderr than a pipe's buffer holds; we just read the file after the
    # process exits/is terminated.
    stderr_f = open(stderr_path, "wb")
    return subprocess.Popen(
        [str(exe_path)],
        cwd=str(exe_path.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=stderr_f,
    )


def scan_stderr(stderr_path: Path) -> "str | None":
    """Return the first unexplained-crash line found in captured stderr, or
    None. Same benign-marker carve-out as the log-file scanner (KNOWN_
    BENIGN_MARKERS), applied to the whole captured blob since stderr for a
    short-lived process is small and bounded (no need for the log file's
    incremental/windowed scan)."""
    if not stderr_path.exists():
        return None
    try:
        text = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    if any(b in text for b in KNOWN_BENIGN_MARKERS):
        return None
    for line in text.splitlines():
        if any(m in line for m in CRASH_MARKERS):
            return line
    return None


class LogScanner:
    """Incremental Traceback/boot-marker scanner, shared between main()'s
    live log-tailing loop (feed() called once per poll with just the newly
    appeared lines) and tests (feed() called once with an entire pre-
    existing log file's lines) -- same algorithm either way, since feed()
    only cares about the sliding recent-lines window, not how many calls
    it took to see everything.
    """

    def __init__(self, recent_window: int = 15):
        self.recent_window = recent_window
        self.recent_lines: "list[str]" = []
        self.benign_seen: "list[str]" = []
        self.outcome: "str | None" = None
        self.unexplained_crash_line: "str | None" = None

    def feed(self, lines: "list[str]") -> None:
        """Process a batch of new lines. Stops early (any lines after the
        triggering one in THIS batch are left unprocessed) as soon as a
        terminal outcome (boot / already_running) or an unexplained crash
        is found -- matching a live tail's "we know enough now" early
        exit. Call again with more lines to keep scanning if outcome is
        still None and unexplained_crash_line is still None."""
        for line in lines:
            self.recent_lines.append(line)
            if len(self.recent_lines) > self.recent_window:
                self.recent_lines.pop(0)
            if ALREADY_RUNNING_MARKER in line:
                self.outcome = "already_running"
                return
            if BOOT_MARKER in line:
                self.outcome = "boot"
                return
            if any(m in line for m in CRASH_MARKERS):
                # logger.exception() writes the "<marker>: <exc>" line
                # first, then the "Traceback (most recent call last):"
                # block right after it -- both land in recent_lines by the
                # time we see the crash-marker line itself.
                if any(b in prev for prev in self.recent_lines for b in KNOWN_BENIGN_MARKERS):
                    self.benign_seen.append(line)
                else:
                    self.unexplained_crash_line = line
                    return


def terminate(proc: subprocess.Popen) -> str:
    if proc.poll() is not None:
        return "already exited"
    proc.terminate()
    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        return f"terminated within {SHUTDOWN_TIMEOUT_S:.0f}s"
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        return "force-killed after terminate() timeout"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_path", help=r"Path to the PyInstaller onedir output, e.g. dist\Samsara")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                         help="Seconds to wait for boot marker / crash before giving up (default: %(default)s)")
    parser.add_argument("--log-copy-to", default=None,
                         help="After the run, copy samsara.log here (fixed, predictable path for CI artifact upload -- "
                              "the isolated profile dir itself lives under a fresh tempfile.mkdtemp(), which is not a "
                              "stable path to point an upload-artifact glob at).")
    args = parser.parse_args(argv)

    dist_path = Path(args.dist_path).resolve()
    exe_path = dist_path / EXE_NAME
    if not exe_path.exists():
        print(f"[FAIL] locate {EXE_NAME} -- not found at {exe_path}")
        return 1

    work_root = Path(tempfile.mkdtemp(prefix="samsara_ci_smoke_"))
    profile_dir = work_root / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.json").write_text(
        json.dumps(make_min_config(), indent=2), encoding="utf-8"
    )
    log_path = profile_dir / "logs" / "samsara.log"
    stderr_path = work_root / "stderr.log"
    print(f"[INFO] isolated profile: {profile_dir}")
    print(f"[INFO] launching {exe_path}")

    proc = launch(exe_path, profile_dir, stderr_path)
    print(f"[INFO] pid={proc.pid}")

    deadline = time.monotonic() + args.timeout
    offset = 0
    scanner = LogScanner()

    while time.monotonic() < deadline:
        # Read the log BEFORE checking whether the process has exited -- a
        # fast exit (e.g. the single-instance lock rejecting a second
        # launch) can otherwise beat the log read, leaving outcome="exited"
        # with the actual reason ("Samsara is already running") sitting
        # unread in the log.
        lines, offset = read_new_lines(log_path, offset)
        scanner.feed(lines)
        if scanner.outcome is not None or scanner.unexplained_crash_line:
            break
        if proc.poll() is not None:
            # One last read in case the final lines landed between the read
            # above and the process actually exiting.
            lines, offset = read_new_lines(log_path, offset)
            scanner.feed(lines)
            break
        time.sleep(POLL_INTERVAL_S)

    outcome = scanner.outcome
    if outcome is None and not scanner.unexplained_crash_line and proc.poll() is not None:
        outcome = "exited"
    if outcome is None:
        outcome = "timeout"
    benign_seen = scanner.benign_seen
    unexplained_crash_line = scanner.unexplained_crash_line

    still_alive = proc.poll() is None
    shutdown_detail = terminate(proc)

    # Read stderr only AFTER the process has exited/been killed, so the
    # file handle is fully flushed and closed -- catches PyInstaller
    # bootloader failures (missing frozen dependency) that never reach the
    # app's own log file at all. Overrides outcome/unexplained_crash_line
    # below regardless of what the log-based scan concluded: a still-alive
    # process that reached the boot marker but ALSO wrote a bootloader
    # crash to stderr must still fail.
    stderr_crash_line = scan_stderr(stderr_path)

    if args.log_copy_to:
        import shutil
        dest = Path(args.log_copy_to)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists():
            shutil.copyfile(log_path, dest)
            print(f"[INFO] copied log to {dest}")
        else:
            print(f"[INFO] no log file to copy ({log_path} was never created)")

    print(f"[INFO] outcome={outcome} still_alive_before_shutdown={still_alive}")
    print(f"[INFO] shutdown: {shutdown_detail}")
    if benign_seen:
        print(f"[WARN] {len(benign_seen)} known-benign traceback line(s) logged (see module docstring):")
        for line in benign_seen[:5]:
            print(f"        {line}")

    # Checked FIRST, ahead of every other branch: a bootloader/import crash
    # on stderr fails the build no matter what the log-based outcome was
    # (boot marker reached, timed out alive, or otherwise) -- see the
    # comment above scan_stderr() call.
    if stderr_crash_line:
        print(f"[FAIL] unexplained crash marker in stderr: {stderr_crash_line!r}")
        return 1
    if outcome == "already_running":
        print("[FAIL] another Samsara instance is already running system-wide on this runner")
        return 1
    if unexplained_crash_line:
        print(f"[FAIL] unexplained crash marker in log: {unexplained_crash_line!r}")
        return 1
    if outcome == "exited":
        print(f"[FAIL] process exited on its own with code {proc.returncode} before boot marker/timeout")
        return 1
    if outcome == "boot":
        print(f"[PASS] reached {BOOT_MARKER!r}")
        return 0
    if outcome == "timeout" and still_alive:
        print(f"[PASS] process stayed alive for {args.timeout:.0f}s without crashing "
              f"(did not reach boot marker -- likely still downloading/loading the Whisper model)")
        return 0

    print("[FAIL] unhandled outcome")
    return 1


if __name__ == "__main__":
    sys.exit(main())
