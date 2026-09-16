"""Queue 96 -- the boot log must say which DPI awareness the process got.

The question this exists to keep answered: Qt 6 asks for PER_MONITOR_AWARE_V2
when QApplication is constructed, and anything that claimed process awareness
earlier wins instead. Windows reports that collision as
"SetProcessDpiAwarenessContext() failed: Access is denied" -- the SAME error it
returns when awareness was already set to the value asked for. The error text
therefore cannot tell you which level you ended up at, and for a long time
nothing in the boot log could.

These tests pin the replacement: one INFO line per boot, naming one of the five
documented levels, in words that say what it means for WindowFromPoint.
"""
import ast
import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import boot

#: The five levels GetAwarenessFromDpiAwarenessContext /
#: AreDpiAwarenessContextsEqual can resolve to.
DOCUMENTED = {
    "UNAWARE", "SYSTEM_AWARE", "PER_MONITOR_AWARE",
    "PER_MONITOR_AWARE_V2", "UNAWARE_GDISCALED",
}


@pytest.fixture(autouse=True)
def _reset_once_flag():
    """log_dpi_awareness logs once per process. Each test gets a fresh one."""
    boot._dpi_awareness_logged = False
    yield
    boot._dpi_awareness_logged = False


# ---------------------------------------------------------------------------
# The named level
# ---------------------------------------------------------------------------

def test_the_boot_log_records_a_named_awareness_level(caplog):
    caplog.set_level(logging.INFO, logger="Samsara")
    name = boot.log_dpi_awareness()

    lines = [r.message for r in caplog.records if r.message.startswith("[DPI]")]
    assert len(lines) == 1, f"expected exactly one [DPI] line, got {lines}"
    assert name in DOCUMENTED, name
    assert name in lines[0], "the line must name the level it resolved"
    assert lines[0].startswith("[DPI] process DPI awareness: ")


def test_the_line_is_INFO_not_a_warning(caplog):
    caplog.set_level(logging.DEBUG, logger="Samsara")
    boot.log_dpi_awareness()
    records = [r for r in caplog.records if r.message.startswith("[DPI]")]
    assert [r.levelno for r in records] == [logging.INFO]


def test_it_logs_once_however_many_times_it_is_called(caplog):
    caplog.set_level(logging.INFO, logger="Samsara")
    first = boot.log_dpi_awareness()
    second = boot.log_dpi_awareness()
    third = boot.log_dpi_awareness()

    assert first in DOCUMENTED
    assert second is None and third is None
    assert len([r for r in caplog.records if r.message.startswith("[DPI]")]) == 1


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows API")
def test_resolve_reads_a_documented_context_and_a_documented_enum():
    name, enum = boot.resolve_dpi_awareness()
    assert name in DOCUMENTED, name
    assert enum in {"UNAWARE", "SYSTEM_AWARE", "PER_MONITOR_AWARE", "UNKNOWN"}, enum


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows API")
def test_the_enum_alone_cannot_tell_v2_from_v1():
    """Why the context name and the enum are BOTH logged: V2 and V1 share the
    enum value, so a line built from the enum would answer a different
    question than the one asked."""
    name, enum = boot.resolve_dpi_awareness()
    if name == "PER_MONITOR_AWARE_V2":
        assert enum == "PER_MONITOR_AWARE", (
            "V2 reports the V1 enum -- that is the whole reason for the "
            "AreDpiAwarenessContextsEqual sweep")


# ---------------------------------------------------------------------------
# What the line says
# ---------------------------------------------------------------------------

def test_a_non_v2_result_says_so_plainly(caplog, monkeypatch):
    monkeypatch.setattr(boot, "resolve_dpi_awareness",
                        lambda: ("SYSTEM_AWARE", "SYSTEM_AWARE"))
    caplog.set_level(logging.INFO, logger="Samsara")
    assert boot.log_dpi_awareness() == "SYSTEM_AWARE"

    line = [r.message for r in caplog.records if r.message.startswith("[DPI]")][0]
    assert "SYSTEM_AWARE" in line
    assert "NOT Per-Monitor V2" in line
    assert "WindowFromPoint" in line, "say what it costs, not just what it is"


def test_a_v2_result_says_the_coordinates_are_real_pixels(caplog, monkeypatch):
    monkeypatch.setattr(boot, "resolve_dpi_awareness",
                        lambda: ("PER_MONITOR_AWARE_V2", "PER_MONITOR_AWARE"))
    caplog.set_level(logging.INFO, logger="Samsara")
    assert boot.log_dpi_awareness() == "PER_MONITOR_AWARE_V2"

    line = [r.message for r in caplog.records if r.message.startswith("[DPI]")][0]
    assert "PER_MONITOR_AWARE_V2" in line
    assert "NOT" not in line
    assert "WindowFromPoint" in line


def test_the_line_never_claims_a_failure(caplog, monkeypatch):
    """The complaint this queue started from: the only DPI line in the boot
    log said "failed: Access is denied", which is what Windows returns when
    awareness was ALREADY SET -- success and collision look identical. The
    new line reports the state, never an outcome."""
    for resolved in (("SYSTEM_AWARE", "SYSTEM_AWARE"),
                     ("PER_MONITOR_AWARE_V2", "PER_MONITOR_AWARE"),
                     ("UNAWARE", "UNAWARE")):
        boot._dpi_awareness_logged = False
        caplog.clear()
        monkeypatch.setattr(boot, "resolve_dpi_awareness", lambda r=resolved: r)
        caplog.set_level(logging.INFO, logger="Samsara")
        boot.log_dpi_awareness()
        line = [r.message for r in caplog.records
                if r.message.startswith("[DPI]")][0]
        assert "failed" not in line.lower()
        assert "access is denied" not in line.lower()


# ---------------------------------------------------------------------------
# It must never take the boot down
# ---------------------------------------------------------------------------

def test_an_unreadable_api_is_reported_not_raised(caplog, monkeypatch):
    def _boom():
        raise OSError("no user32 here")

    monkeypatch.setattr(boot, "resolve_dpi_awareness",
                        lambda: (None, "OSError: no user32 here"))
    caplog.set_level(logging.INFO, logger="Samsara")
    assert boot.log_dpi_awareness() is None
    assert "could not be read" in caplog.text


def test_resolve_swallows_a_broken_ctypes(monkeypatch):
    broken = types.ModuleType("ctypes")

    def _explode(*_a, **_k):
        raise AttributeError("windll is gone")

    broken.__getattr__ = _explode
    monkeypatch.setitem(sys.modules, "ctypes", broken)
    name, detail = boot.resolve_dpi_awareness()
    assert name is None
    assert detail


def test_it_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    name, detail = boot.resolve_dpi_awareness()
    assert name is None and detail == "not Windows"


# ---------------------------------------------------------------------------
# Where it is called from
# ---------------------------------------------------------------------------

def test_create_splash_logs_it_once_qt_is_up(monkeypatch, caplog):
    """SplashScreenQt.__init__ calls qt_runtime.ensure_started(), which
    constructs QApplication and waits -- so create_splash() returning means Qt
    has had its chance to claim awareness."""
    events = []

    class _Splash:
        def __init__(self):
            events.append("init")

        def set_status(self, text):
            events.append(("status", text))

    monkeypatch.setitem(sys.modules, "samsara.ui.splash_qt",
                        types.SimpleNamespace(SplashScreenQt=_Splash))
    caplog.set_level(logging.INFO, logger="Samsara")
    boot.create_splash()

    assert events == ["init", ("status", "Initializing...")], (
        "the splash hand-off itself must be unchanged")
    assert len([r for r in caplog.records if r.message.startswith("[DPI]")]) == 1


def test_a_dpi_read_that_explodes_does_not_stop_the_splash(monkeypatch, caplog):
    class _Splash:
        def set_status(self, text):
            pass

    monkeypatch.setitem(sys.modules, "samsara.ui.splash_qt",
                        types.SimpleNamespace(SplashScreenQt=_Splash))
    monkeypatch.setattr(boot, "resolve_dpi_awareness",
                        lambda: (None, "RuntimeError: nope"))
    caplog.set_level(logging.INFO, logger="Samsara")
    assert isinstance(boot.create_splash(), _Splash)


# ---------------------------------------------------------------------------
# boot.py's import contract, which this change had to stay inside
# ---------------------------------------------------------------------------

def test_ctypes_is_imported_lazily_not_at_module_level():
    """tests/test_boot.py asserts a CLOSED whitelist of module-level imports.
    The DPI read needs ctypes, so it imports it inside the function -- the
    same convention boot.py already uses for Qt and samsara.*."""
    source = Path(boot.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level.add((node.module or "").split(".")[0])
    assert "ctypes" not in top_level
    assert "ctypes" in source, "it is still imported, just not at module level"
