"""Plugin module identity and idempotent registration.

Astra's 2026-09-12 review, section 1 item 6 (PLUGIN_MODULE_IDENTITY): discovery
used to execute each plugin file as a fresh `samsara_plugin_<stem>` module that
was never put in sys.modules, while runtime code imports
`plugins.commands.ask_ollama` directly. Pending/confirmation state therefore
lived in two module objects. These tests pin the inverted result: discovery
loads each plugin ONCE under its canonical package name, so a later canonical
import is the very same module, with the very same pending state.
"""
import importlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara import plugin_commands  # noqa: E402


@pytest.fixture
def clean_registry():
    saved = dict(plugin_commands._REGISTRY)
    plugin_commands._REGISTRY.clear()
    try:
        yield
    finally:
        plugin_commands._REGISTRY.clear()
        plugin_commands._REGISTRY.update(saved)


def _drop_modules(prefix):
    for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
        sys.modules.pop(name, None)


_PLUGIN_SOURCE = textwrap.dedent('''
    from samsara.plugin_commands import command

    EXEC_COUNT = globals().get("EXEC_COUNT", 0) + 1
    pending = []

    @command("stage identity action")
    def stage(app, remainder):
        pending.append("action")
        return True
''')


@pytest.fixture
def plugin_package(tmp_path, clean_registry):
    """An inert plugin living in a real importable package: idpkg.cmds.example."""
    pkg = tmp_path / "idpkg"
    cmds = pkg / "cmds"
    cmds.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (cmds / "__init__.py").write_text("", encoding="utf-8")
    (cmds / "example.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    _drop_modules("idpkg")
    try:
        yield cmds
    finally:
        _drop_modules("idpkg")
        sys.modules.pop("samsara_plugin_example", None)
        sys.path.remove(str(tmp_path))


class TestCanonicalIdentity:
    def test_discovery_then_canonical_import_is_one_module(self, plugin_package):
        assert plugin_commands.load_plugins(plugin_package) == 1
        handler = plugin_commands._REGISTRY["stage identity action"]["func"]

        # Stage state through the handler discovery registered...
        assert handler(None, "") is True

        # ...and read it back through an ordinary canonical import.
        canonical = importlib.import_module("idpkg.cmds.example")
        assert handler.__module__ == "idpkg.cmds.example"
        assert handler.__globals__ is canonical.__dict__
        assert canonical.pending == ["action"]
        # The legacy loader name is an alias of the same object, never a copy.
        assert sys.modules["samsara_plugin_example"] is canonical

    def test_canonical_import_then_discovery_does_not_reexecute(self, plugin_package):
        canonical = importlib.import_module("idpkg.cmds.example")
        canonical.pending.append("before discovery")

        plugin_commands.load_plugins(plugin_package)

        assert canonical.EXEC_COUNT == 1
        assert canonical.pending == ["before discovery"]
        handler = plugin_commands._REGISTRY["stage identity action"]["func"]
        assert handler is canonical.stage

    def test_second_discovery_is_idempotent(self, plugin_package):
        plugin_commands.load_plugins(plugin_package)
        handler = plugin_commands._REGISTRY["stage identity action"]["func"]
        module = sys.modules["idpkg.cmds.example"]

        plugin_commands.load_plugins(plugin_package)

        assert sys.modules["idpkg.cmds.example"] is module
        assert module.EXEC_COUNT == 1
        assert plugin_commands._REGISTRY["stage identity action"]["func"] is handler

    def test_non_package_directory_still_registers_one_module(self, tmp_path, clean_registry):
        """A loose directory (no __init__.py chain) cannot have a package name;
        the fallback name is registered in sys.modules so a second discovery
        reuses it instead of executing a second copy."""
        loose = tmp_path / "loose"
        loose.mkdir()
        (loose / "loose_example.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
        sys.modules.pop("samsara_plugin_loose_example", None)
        try:
            plugin_commands.load_plugins(loose)
            module = sys.modules["samsara_plugin_loose_example"]
            plugin_commands.load_plugins(loose)
            assert sys.modules["samsara_plugin_loose_example"] is module
            assert module.EXEC_COUNT == 1
        finally:
            sys.modules.pop("samsara_plugin_loose_example", None)


class TestRegistrationIdempotence:
    def test_reexecuting_a_decorator_replaces_instead_of_duplicating(self, clean_registry):
        def handler(app, remainder):
            return True

        plugin_commands.command("idempotent phrase", aliases=["idem alias"])(handler)
        first_unique = len({id(e) for e in plugin_commands._REGISTRY.values()})
        plugin_commands.command("idempotent phrase", aliases=["idem alias"])(handler)

        assert len({id(e) for e in plugin_commands._REGISTRY.values()}) == first_unique
        assert (plugin_commands._REGISTRY["idempotent phrase"]
                is plugin_commands._REGISTRY["idem alias"])


class TestServicesAreExplicit:
    def test_start_plugin_services_runs_each_hook_once(self, plugin_package):
        (plugin_package / "with_service.py").write_text(textwrap.dedent('''
            STARTS = []
            def start_services(app):
                STARTS.append(app)
        '''), encoding="utf-8")
        plugin_commands.load_plugins(plugin_package)
        module = importlib.import_module("idpkg.cmds.with_service")
        assert module.STARTS == []

        app = object()
        plugin_commands.start_plugin_services(app)
        plugin_commands.start_plugin_services(app)

        assert module.STARTS == [app]
        sys.modules.pop("samsara_plugin_with_service", None)


_REAL_DISCOVERY_PROBE = textwrap.dedent('''
    import json, sys, threading, time
    sys.path.insert(0, ROOT)
    from samsara import plugin_commands

    def health_threads():
        return sum(1 for t in threading.enumerate() if t.name.startswith("ollama-health"))

    plugin_commands.load_plugins(ROOT + "/plugins/commands")
    handler = plugin_commands._REGISTRY["ask ava"]["func"]
    confirm = plugin_commands._REGISTRY["yes"]["func"]
    discovered = sys.modules.get(handler.__module__)

    # Stage a pending action through the module discovery executed.
    with handler.__globals__["_pending_action_lock"]:
        handler.__globals__["_pending_action"] = {
            "type": "action", "command": "probe", "expires": time.time() + 60}

    import plugins.commands.ask_ollama as canonical
    from plugins.commands import ask_ollama as canonical_from

    after_import = health_threads()
    plugin_commands.load_plugins(ROOT + "/plugins/commands")
    plugin_commands.start_plugin_services(object())
    plugin_commands.start_plugin_services(object())

    print("PROBE " + json.dumps({
        "handler_module": handler.__module__,
        "same_module": discovered is canonical and canonical_from is canonical,
        "same_globals": handler.__globals__ is canonical.__dict__,
        "confirm_same_globals": confirm.__globals__ is canonical.__dict__,
        "canonical_pending": (canonical.get_pending_action() or {}).get("command"),
        "legacy_alias_same": sys.modules.get("samsara_plugin_ask_ollama") is canonical,
        "handler_stable": plugin_commands._REGISTRY["ask ava"]["func"] is handler,
        "health_threads_after_discovery_and_import": after_import,
        "health_threads_after_explicit_start": health_threads(),
    }))
''')


def test_real_discovery_then_canonical_import_shares_ask_ollama_state():
    """Astra's PLUGIN_MODULE_IDENTITY probe, inverted, on the REAL plugin set
    in an isolated interpreter (no shared sys.modules with this test run)."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", f"ROOT = {str(ROOT)!r}\n" + _REAL_DISCOVERY_PROBE],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    lines = [l for l in proc.stdout.splitlines() if l.startswith("PROBE ")]
    assert lines, f"probe produced no result\nstdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1][len("PROBE "):])

    assert result == {
        "handler_module": "plugins.commands.ask_ollama",
        "same_module": True,
        "same_globals": True,
        "confirm_same_globals": True,
        "canonical_pending": "probe",
        "legacy_alias_same": True,
        "handler_stable": True,
        "health_threads_after_discovery_and_import": 0,
        "health_threads_after_explicit_start": 1,
    }
