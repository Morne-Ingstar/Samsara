"""Queue 108/F9: the installer-to-EXE path for --fetch-components.

installer/samsara.iss runs `Samsara.exe --fetch-components <ids> --manifest
<url> --progress-window` at ssPostInstall and waits for it. Until 108 nothing
in dictation.py dispatched that argument, so the invocation either hung on a
normal long-running app or -- with an instance already running -- exited 0 at
the single-instance lock with no fetch. Both silent.

tests/test_first_run_components.py covers fetch_components_main itself
(download bytes, sidecars, exit codes). What is covered HERE is the part that
was missing: the entry point actually routing to it, ahead of the lock, in a
real child process launched the way the installer launches it.

The child is a real subprocess so the dispatch is exercised through
dictation.py's module body, but it exits above every heavy import, so no
audio device, no Qt and no instance lock is ever touched -- which is also why
this is safe to run while Samsara is live.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

ENTRY = REPO / "dictation.py"
#: Generous: the point is that it EXITS, not that it is fast. A regression
#: that starts the app instead shows up as this timeout, which is exactly the
#: installer's first failure shape (ewWaitUntilTerminated on a live app).
CHILD_TIMEOUT_S = 120.0


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def local_release(tmp_path):
    """A manifest and its archives on disk, served with --components-dir so
    the child never reaches the network."""
    comp_dir = tmp_path / "components"
    comp_dir.mkdir()
    wake = _zip_bytes({"a.onnx": b"a" * 3000, "b.onnx": b"b" * 2000})
    (comp_dir / "wake-word-models.zip").write_bytes(wake)
    manifest = {
        "schema_version": 1, "app_version": "9.9.9", "release_tag": "v9.9.9",
        "build_date": "2026-09-13T00:00:00Z", "git_sha": "0" * 40,
        "components": [{
            "id": "wake-word-models", "name": "Wake-word models",
            "description": "Offline wake-word detection", "kind": "model",
            "available": True, "filename": "wake-word-models.zip",
            "install_dir": "models", "requires": [], "default": False,
            "source": "built", "url": "https://example.invalid/wake-word-models.zip",
            "size_bytes": len(wake), "sha256": hashlib.sha256(wake).hexdigest(),
            "min_disk_bytes": 2 * len(wake),
        }],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    app_root = tmp_path / "app"
    app_root.mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    return {"manifest": manifest_path, "components": comp_dir,
            "app_root": app_root, "downloads": downloads, "home": tmp_path / "home"}


def _run(world, ids="wake-word-models", *, extra=()):
    """Launch the entry point the way installer/samsara.iss does."""
    argv = [sys.executable, str(ENTRY),
            "--fetch-components", ids,
            "--manifest", str(world["manifest"]),
            "--components-dir", str(world["components"]),
            "--app-root", str(world["app_root"]),
            "--downloads-dir", str(world["downloads"]),
            *extra]
    import os
    env = os.environ.copy()
    # Never the real profile, and never the running session's.
    env["SAMSARA_HOME_DIR"] = str(world["home"])
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=CHILD_TIMEOUT_S, cwd=str(REPO), env=env)


# ---------------------------------------------------------------------------
# The dispatch exists, and it is above the lock
# ---------------------------------------------------------------------------

def test_the_dispatch_runs_before_the_instance_lock():
    """Source order, asserted directly: the lock is what made the second
    failure shape silent, so the dispatch has to be above it -- and above
    the heavy imports, for the same reason the ducking host is."""
    source = ENTRY.read_text(encoding="utf-8", errors="replace")
    dispatch = source.index("_dispatch_startup_argument(sys.argv[1:])")
    lock = source.index("lock_single_instance()")
    splash = source.index("_samsara_boot.create_splash()")
    heavy = source.index("import sounddevice")
    assert dispatch < lock, "the dispatch must run before the single-instance lock"
    assert dispatch < splash, "the dispatch must run before the splash"
    assert dispatch < heavy, "the dispatch must run before the heavy imports"


def test_the_installer_argument_is_the_one_the_script_passes():
    """installer/samsara.iss and the dispatch must name the same flag; a
    rename on one side is the whole bug again."""
    import re

    iss = (REPO / "installer" / "samsara.iss").read_text(encoding="utf-8", errors="replace")
    source = ENTRY.read_text(encoding="utf-8", errors="replace")
    assert "--fetch-components" in iss
    modes = re.search(r"_ARGUMENT_MODES = \(([^)]*)\)", source).group(1)
    assert "--fetch-components" in modes


# ---------------------------------------------------------------------------
# The real child process
# ---------------------------------------------------------------------------

def test_a_fetch_invocation_fetches_and_exits(local_release):
    """The brief's first gate: performs the fetch and exits, with no splash
    and no app."""
    result = _run(local_release)
    assert result.returncode == 0, result.stdout + result.stderr
    installed = local_release["app_root"] / "models"
    assert (installed / "a.onnx").read_bytes() == b"a" * 3000
    assert (installed / "b.onnx").read_bytes() == b"b" * 2000
    assert "[PASS] wake-word-models" in result.stdout

    # No app was built: the process exits above the splash, the instance
    # lock and the log directory the app would create.
    assert "Startup complete" not in result.stdout
    assert not (local_release["home"] / "logs" / "session.running").exists()


_HOLD_LOCK = (
    "import sys, time; sys.path.insert(0, {repo!r});"
    "import samsara.boot as b; b.lock_single_instance();"
    "print('HELD', flush=True); time.sleep({seconds})"
)


def _hold_instance_lock(world, seconds=90):
    """A child holding the same instance-lock identity as the fetch child.

    The lock identity is derived from SAMSARA_HOME_DIR when it is set
    (samsara/boot.py _check_single_instance), so this holds a lock for the
    test's temp profile and never collides with a real running Samsara."""
    import os

    env = os.environ.copy()
    env["SAMSARA_HOME_DIR"] = str(world["home"])
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK.format(repo=str(REPO), seconds=seconds)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=str(REPO), env=env)
    assert proc.stdout.readline().strip() == "HELD", "the lock holder never started"
    return proc, env


def test_the_lock_really_would_claim_a_second_launch(local_release):
    """Guards the next test from being vacuous: with the holder up, an
    ordinary second acquisition of the SAME identity exits 0 having done
    nothing. That silent success is what the installer used to get."""
    holder, env = _hold_instance_lock(local_release, seconds=30)
    try:
        second = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r);"
             "import samsara.boot as b; b.lock_single_instance();"
             "print('ACQUIRED')" % str(REPO)],
            capture_output=True, text=True, timeout=60, cwd=str(REPO), env=env)
        assert second.returncode == 0
        assert "ACQUIRED" not in second.stdout, (
            "the lock did not refuse the second launch, so the next test proves nothing"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_a_fetch_while_an_instance_is_running_does_not_exit_zero_without_fetching(local_release):
    """The brief's second gate, and the failure that reads as success.

    The dispatch sits above lock_single_instance(), so the lock cannot claim
    this invocation. The strong form of the assertion is not "exit 0" -- the
    broken build also exits 0 -- it is that the component is on disk."""
    installed = local_release["app_root"] / "models" / "a.onnx"
    assert not installed.exists()

    holder, _env = _hold_instance_lock(local_release)
    try:
        result = _run(local_release)
        assert result.returncode == 0, result.stdout + result.stderr
        assert installed.exists(), (
            "the fetch was claimed by the instance lock and exited 0 without "
            "fetching -- the exact silent failure 108 fixes"
        )
        assert "[PASS] wake-word-models" in result.stdout
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_a_bad_component_id_reports_a_nonzero_code_and_writes_nothing(local_release):
    """The installer reads the exit code (FetchOutcome). A bad id has to be
    distinguishable from success, and must not half-install."""
    result = _run(local_release, ids="not-a-component")
    assert result.returncode == 2                       # first_run_qt.EXIT_BAD_ARGS
    assert not (local_release["app_root"] / "models").exists()


def test_a_normal_launch_is_not_diverted():
    """The dispatch must be inert without the flag, or every ordinary start
    is now a fetch. Driven directly rather than by launching the app."""
    import re

    source = ENTRY.read_text(encoding="utf-8", errors="replace")
    body = re.search(r"\ndef _dispatch_startup_argument\(argv\):.*?\n\n\n", source, re.S).group(0)
    namespace: dict = {}
    exec(body.replace("_ARGUMENT_MODES", "('--fetch-components',)"), namespace)  # noqa: S102
    dispatch = namespace["_dispatch_startup_argument"]
    assert dispatch([]) is None
    assert dispatch(["--debug"]) is None
    assert dispatch(None) is None
