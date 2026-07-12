"""Tests for hermetic pytest collection.

Two collection-time side effects used to leak into whatever machine ran the
suite:

1. samsara/paths.py's samsara_home_dir() (and samsara/log.py, which binds
   SAMSARA_LOG_FILE from it at import time) resolved to the real ~/.samsara
   the instant any Samsara module was imported -- including just from
   `pytest --collect-only`, before a single test body ever ran. Fixed by
   conftest.py force-assigning SAMSARA_HOME_DIR at module level (before any
   Samsara import happens during collection) to a session-owned temp dir.

2. dictation.py imported `faster_whisper` (a heavy, slow-loading ML
   dependency) at module level, so merely importing dictation.py -- again,
   just from collection -- paid that cost and pulled it into sys.modules.
   Fixed by deferring it behind a `_create_whisper_model()` factory that
   imports faster_whisper only when actually called (real model construction
   time), not at import time.

test_dictation_import_is_hermetic below runs in a subprocess rather than
in-process: this pytest process has already imported dictation (and
faster_whisper, via other test modules/fixtures) by the time any test body
runs, so sys.modules here can never show a clean "not yet imported" state.
A fresh subprocess is the only way to observe a true first import.
"""
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import dictation

REPO_ROOT = Path(__file__).parents[1]


def test_dictation_import_is_hermetic(tmp_path):
    """A fresh `import dictation`, in a subprocess with its own isolated
    SAMSARA_HOME_DIR, must not pull faster_whisper into sys.modules (it's
    behind the lazy factory now, not a module-level import), and must
    resolve its log path under that temp home, not the real ~/.samsara."""
    home = tmp_path / "home"
    home.mkdir()

    script = (
        "import json, sys\n"
        "import dictation\n"
        "print('RESULT_JSON:' + json.dumps({\n"
        "    'faster_whisper_imported': 'faster_whisper' in sys.modules,\n"
        "    'log_file': str(dictation.LOG_FILE),\n"
        "}))\n"
    )

    env = dict(os.environ)
    env["SAMSARA_HOME_DIR"] = str(home)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=90, env=env,
    )
    assert result.returncode == 0, (
        f"subprocess import of dictation failed "
        f"(exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    result_lines = [
        line for line in result.stdout.splitlines() if line.startswith("RESULT_JSON:")
    ]
    assert result_lines, f"no RESULT_JSON line in stdout:\n{result.stdout}"
    payload = json.loads(result_lines[-1][len("RESULT_JSON:"):])

    assert payload["faster_whisper_imported"] is False, (
        "faster_whisper must not be imported by `import dictation` alone"
    )
    log_file = Path(payload["log_file"]).resolve()
    assert str(log_file).startswith(str(home.resolve())), (
        f"dictation's log path {log_file} did not resolve under the "
        f"isolated SAMSARA_HOME_DIR {home}"
    )


def test_create_whisper_model_factory_delegates(monkeypatch):
    """The factory must do nothing but forward to faster_whisper.WhisperModel
    -- verified via a fake module injected into sys.modules ONLY for this
    test (monkeypatch.setitem auto-restores it on teardown), never globally
    from conftest, so a genuinely missing faster_whisper stays detectable
    everywhere else."""
    calls = {}

    class _FakeWhisperModel:
        def __init__(self, *args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    result = dictation._create_whisper_model("base", device="cpu", compute_type="int8")

    assert isinstance(result, _FakeWhisperModel)
    assert calls["args"] == ("base",)
    assert calls["kwargs"] == {"device": "cpu", "compute_type": "int8"}


def test_create_whisper_model_factory_propagates_missing_dependency(monkeypatch):
    """No try/except inside the factory: a genuinely missing faster_whisper
    must raise ModuleNotFoundError up to dictation.py's existing
    startup-error handling (load_model_async's outer except Exception),
    not be swallowed inside the factory itself."""
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(ModuleNotFoundError):
        dictation._create_whisper_model("base")
