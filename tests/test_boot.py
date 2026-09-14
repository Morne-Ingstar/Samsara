"""samsara/boot.py: the boot sequence extracted from dictation.py (27).

Contracts:
  * boot never imports dictation, and loads only the standard library at
    import time (samsara.* / Qt stay inside the functions, as they were inline);
  * dictation.py calls the boot steps at the same points, in the same order,
    and no longer defines the moved code itself;
  * the moved steps keep their exact log lines and failure behaviour.
"""

import ast
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOT_SRC = ROOT / "samsara" / "boot.py"
DICTATION_SRC = ROOT / "dictation.py"

MOVED = {
    "_enable_faulthandler", "_is_samsara_process", "_steal_stale_lock_if_any",
    "_check_single_instance", "_acquire_instance_lock", "_BootStageTimer",
}


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------------------
# Import isolation
# ---------------------------------------------------------------------------

def test_boot_never_imports_dictation():
    for node in ast.walk(_tree(BOOT_SRC)):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "dictation" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "dictation"


def test_boot_module_level_imports_are_stdlib_only():
    top = [n for n in _tree(BOOT_SRC).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = set()
    for node in top:
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        else:
            names.add((node.module or "").split(".")[0])
    assert names <= {"logging", "os", "sys", "threading", "time", "pathlib"}, names


def test_importing_boot_does_not_pull_in_dictation():
    code = ("import sys\nimport samsara.boot as b\n"
            "assert 'dictation' not in sys.modules\n"
            "assert b.logger.name == 'Samsara'\nprint('ok')\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]


# ---------------------------------------------------------------------------
# dictation.py calls the steps where the inline code used to run
# ---------------------------------------------------------------------------

def _boot_call_lines(nodes):
    calls = []
    for node in nodes:
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "_samsara_boot"):
                calls.append((sub.lineno, sub.func.attr))
    return [name for _line, name in sorted(calls)]


def test_dictation_no_longer_defines_the_moved_code():
    defined = {n.name for n in _tree(DICTATION_SRC).body
               if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert defined.isdisjoint(MOVED), defined & MOVED


def test_module_level_steps_run_in_the_original_order():
    tree = _tree(DICTATION_SRC)
    body = tree.body
    main = next(n for n in body if isinstance(n, ast.If) and "__main__" in ast.dump(n.test))
    module_level = [n for n in body if n is not main
                    and not isinstance(n, (ast.ClassDef, ast.FunctionDef))]
    assert _boot_call_lines(module_level) == ["_enable_faulthandler", "check_vc_redistributable"]

    def first_line(pred):
        return next(n.lineno for n in body if pred(n))

    boot_import = first_line(lambda n: isinstance(n, ast.Import)
                             and any(a.name == "samsara.boot" for a in n.names))
    ducking_divert = first_line(lambda n: isinstance(n, ast.If) and "SAMSARA_DUCKING_HOST" in ast.dump(n))
    sounddevice = first_line(lambda n: isinstance(n, ast.Import)
                             and any(a.name == "sounddevice" for a in n.names))
    whisper_factory = first_line(lambda n: isinstance(n, ast.FunctionDef)
                                 and n.name == "_create_whisper_model")
    vc_check = next(n.lineno for n in body for s in ast.walk(n)
                    if isinstance(s, ast.Call) and getattr(s.func, "attr", "") == "check_vc_redistributable")
    # the divert still runs before any samsara import; faulthandler/AUMID before the
    # native imports; the VC++ check before the faster-whisper factory, as before
    assert ducking_divert < boot_import < sounddevice
    assert vc_check < whisper_factory


def test_main_block_steps_run_in_the_original_order():
    tree = _tree(DICTATION_SRC)
    main = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.dump(n.test))
    assert _boot_call_lines([main]) == [
        "lock_single_instance", "migrate_legacy_source_profile",
        "apply_early_interface_scale", "create_splash", "show_startup_failure",
    ]
    src = ast.unparse(main)
    assert src.index("create_splash") < src.index("DictationApp(splash)")
    assert "migrate_legacy_source_profile(Path(__file__).parent)" in src


# ---------------------------------------------------------------------------
# Behaviour of the moved steps
# ---------------------------------------------------------------------------

@pytest.fixture
def boot():
    import samsara.boot as module
    return module


def test_lock_single_instance_keeps_its_diag_line(boot, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(boot, "_check_single_instance", lambda: calls.append(1) or "handle")
    caplog.set_level(logging.DEBUG, logger="Samsara")
    boot.lock_single_instance()
    assert calls == [1]
    assert boot._instance_lock == "handle"
    assert any(r.name == "Samsara" and r.getMessage().startswith(
        "[BOOT-DIAG] instance lock (_check_single_instance): ") for r in caplog.records)


def test_migrate_legacy_profile_uses_the_given_source_dir(boot, monkeypatch, tmp_path, caplog):
    import samsara.paths as paths
    seen = []
    monkeypatch.setattr(paths, "migrate_legacy_source_config", lambda p: seen.append(p) or True)
    caplog.set_level(logging.INFO, logger="Samsara")
    boot.migrate_legacy_source_profile(tmp_path)
    assert seen == [tmp_path / "config.json"]
    assert "[CONFIG] Migrated legacy source profile to the per-user profile" in caplog.text


def test_migrate_legacy_profile_is_skipped_when_frozen_and_never_raises(boot, monkeypatch, tmp_path, caplog):
    import samsara.paths as paths
    monkeypatch.setattr(paths, "migrate_legacy_source_config", lambda p: pytest.fail("ran frozen"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    boot.migrate_legacy_source_profile(tmp_path)
    monkeypatch.delattr(sys, "frozen")

    def boom(_p):
        raise OSError("disk")
    monkeypatch.setattr(paths, "migrate_legacy_source_config", boom)
    caplog.set_level(logging.WARNING, logger="Samsara")
    boot.migrate_legacy_source_profile(tmp_path)
    assert "[CONFIG] Could not migrate legacy source profile: disk" in caplog.text


def test_create_splash_builds_sets_status_and_logs(boot, monkeypatch, caplog):
    events = []

    class _Splash:
        def __init__(self):
            events.append("init")

        def set_status(self, text):
            events.append(("status", text))

    monkeypatch.setitem(sys.modules, "samsara.ui.splash_qt", types.SimpleNamespace(SplashScreenQt=_Splash))
    caplog.set_level(logging.DEBUG, logger="Samsara")
    splash = boot.create_splash()
    assert isinstance(splash, _Splash)
    assert events == ["init", ("status", "Initializing...")]
    assert "[BOOT-DIAG] splash init (SplashScreenQt): " in caplog.text


def test_show_startup_failure_rich_legacy_and_broken_splash(boot, caplog):
    class _Rich:
        def __init__(self):
            self.events = []

        def set_status(self, t):
            self.events.append(("status", t))

        def set_detail(self, t):
            self.events.append(("detail", t))

        def set_error(self, t, d):
            self.events.append(("error", t, d))

    rich = _Rich()
    boot.show_startup_failure(rich, RuntimeError("model gone"))
    assert rich.events == [("status", "Startup could not finish"), ("detail", "model gone"),
                           ("error", "Startup could not finish", "model gone")]

    legacy = types.SimpleNamespace(statuses=[])
    legacy.set_status = legacy.statuses.append
    boot.show_startup_failure(legacy, RuntimeError("x"))
    assert legacy.statuses == ["Startup could not finish"]

    class _Broken:
        def set_status(self, t):
            raise RuntimeError("splash dead")

    caplog.set_level(logging.DEBUG, logger="Samsara")
    boot.show_startup_failure(_Broken(), RuntimeError("x"))     # never raises
    assert "Could not show synchronous startup error: splash dead" in caplog.text


def test_early_interface_scale_logs_success_and_failure(boot, monkeypatch, caplog, tmp_path):
    import samsara.paths as paths
    import samsara.ui_scale as ui_scale
    monkeypatch.setattr(paths, "samsara_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(ui_scale, "apply_early_ui_scale", lambda p: 1.3)
    caplog.set_level(logging.INFO, logger="Samsara")
    boot.apply_early_interface_scale()
    assert "[UI] Early interface scale: 1.3x" in caplog.text

    def boom(_p):
        raise ValueError("bad scale")
    monkeypatch.setattr(ui_scale, "apply_early_ui_scale", boom)
    boot.apply_early_interface_scale()
    assert "[UI] Could not apply interface scale: bad scale" in caplog.text


def test_vc_check_never_raises_when_available(boot):
    boot.check_vc_redistributable()     # msvcp140 is present wherever the suite runs


def test_aumid_call_stayed_in_dictation_module_level():
    """tests/test_theme_identity.py executes dictation.py's own module-level
    AUMID block, so that block deliberately did not move."""
    assert "SetCurrentProcessExplicitAppUserModelID" not in BOOT_SRC.read_text(encoding="utf-8")
    body = _tree(DICTATION_SRC).body
    assert any(isinstance(n, ast.If) and "SetCurrentProcessExplicitAppUserModelID" in ast.unparse(n)
               for n in body)


def test_boot_stage_timer_logs_to_the_samsara_logger_by_default(boot, caplog):
    caplog.set_level(logging.INFO, logger="Samsara")
    timer = boot._BootStageTimer()
    timer("unit stage")
    assert any(r.name == "Samsara" and r.getMessage().startswith("[BOOT] unit stage: ")
               for r in caplog.records)
