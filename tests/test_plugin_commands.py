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
import inspect
import sys
import textwrap
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara import plugin_commands  # noqa: E402

# Capture the production loader before conftest's per-test fixture suppresses
# discovery of the real command directory. The real-plugin test opts in here.
_load_real_plugins = plugin_commands.load_plugins


@pytest.fixture
def clean_registry():
    # Discovery and service startup share more than the phrase registry.
    # Keep these inert package tests from starting previously loaded hooks.
    names = ("_REGISTRY", "_MODULE_ENTRIES", "_LOADED_MODULES", "_STARTED_SERVICES")
    saved = {name: getattr(plugin_commands, name).copy() for name in names}
    for name in names:
        getattr(plugin_commands, name).clear()
    try:
        yield
    finally:
        for name in names:
            state = getattr(plugin_commands, name)
            state.clear()
            state.update(saved[name])


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
    # An outer sys.path entry (including a gate's plugin directory) must not
    # turn pytest's temporary ancestors into extra namespace packages.
    plugin_root = tmp_path / "plugin-root"
    pkg = plugin_root / "idpkg"
    cmds = pkg / "cmds"
    cmds.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (cmds / "__init__.py").write_text("", encoding="utf-8")
    (cmds / "example.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    sys.path.insert(0, str(plugin_root))
    _drop_modules("idpkg")
    try:
        yield cmds
    finally:
        _drop_modules("idpkg")
        sys.modules.pop("samsara_plugin_example", None)
        sys.path.remove(str(plugin_root))


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
        assert {m.__name__ for m in plugin_commands._LOADED_MODULES.values()} == {
            "idpkg.cmds.example", "idpkg.cmds.with_service"}

        app = object()
        plugin_commands.start_plugin_services(app)
        plugin_commands.start_plugin_services(app)

        assert module.STARTS == [app]
        sys.modules.pop("samsara_plugin_with_service", None)


@pytest.fixture
def cold_real_plugins(clean_registry, monkeypatch):
    """Cold plugin imports without a subprocess or changes to cached plugins."""
    def is_plugin(name):
        return (name == "plugins" or name.startswith("plugins.")
                or name.startswith("samsara_plugin_"))

    with monkeypatch.context() as patch:
        for name in list(sys.modules):
            if is_plugin(name):
                patch.delitem(sys.modules, name)
        try:
            yield
        finally:
            # Drop this test's new package objects before restoring the originals,
            # including their parent-package attributes and legacy aliases.
            for name in list(sys.modules):
                if is_plugin(name):
                    sys.modules.pop(name)


def test_real_discovery_then_canonical_import_shares_ask_ollama_state(
        cold_real_plugins, monkeypatch):
    """Real discovery and canonical imports share pending state and lifecycle."""
    from samsara import ai_preferences, ava_readiness
    from samsara.runtime import thread_registry

    spawned = []

    def record_spawn(name, target, **kwargs):
        token = object()
        spawned.append((name, target, kwargs, token))
        return token

    # Exercise real service registration without starting perpetual pollers or
    # contacting Ollama. Reset readiness independently of earlier test imports.
    monkeypatch.setattr(thread_registry, "spawn", record_spawn)
    monkeypatch.setattr(ava_readiness, "_monitor", None)
    commands = ROOT / "plugins" / "commands"
    loaded = _load_real_plugins(commands)
    assert loaded == sum(not path.name.startswith("_") for path in commands.glob("*.py"))
    handler = plugin_commands._REGISTRY["ask ava"]["func"]
    confirm = plugin_commands._REGISTRY["yes"]["func"]
    discovered = sys.modules[handler.__module__]
    implementation = inspect.unwrap(handler)

    with implementation.__globals__["_pending_action_lock"]:
        implementation.__globals__["_pending_action"] = {
            "type": "action", "command": "probe", "expires": time.time() + 60}

    import plugins.commands.ask_ollama as canonical
    from plugins.commands import ask_ollama as canonical_from

    assert handler.__module__ == "plugins.commands.ask_ollama"
    assert discovered is canonical_from is canonical
    assert handler is canonical.handle_ask_ava
    # own_turn uses functools.wraps: its globals belong to ava_turn_queue.
    # The underlying implementation must still own the canonical state.
    assert implementation.__globals__ is canonical.__dict__
    assert confirm is canonical.handle_ava_confirm
    assert confirm.__globals__ is canonical.__dict__
    assert canonical.get_pending_action()["command"] == "probe"
    assert sys.modules["samsara_plugin_ask_ollama"] is canonical
    assert _load_real_plugins(commands) == loaded
    assert plugin_commands._REGISTRY["ask ava"]["func"] is handler
    assert canonical.get_pending_action()["command"] == "probe"
    assert spawned == []
    assert canonical._health_monitor_thread is None

    config = {"ollama": {"host": "http://127.0.0.1:11434", "model": "test-model"}}
    consent = ai_preferences.accept_consent(
        config, provider=ai_preferences.provider_for(config, "local"),
        accepted_at="test-acceptance")
    app = SimpleNamespace(config=ai_preferences.apply_preferences(
        config, policy="local", ava=True, consent=consent, local_model="test-model"))
    assert canonical._ai_state(app).allowed

    # Only this plugin's lifecycle is under test. Other real hooks mutate alias
    # metadata or start servers and must not run against this synthetic app.
    plugin_commands._STARTED_SERVICES.update(
        id(module) for module in plugin_commands._LOADED_MODULES.values()
        if module is not canonical)
    plugin_commands.start_plugin_services(app)
    plugin_commands.start_plugin_services(app)
    canonical.start_services(app)

    assert [name for name, *_ in spawned] == ["ollama-health", "ava-readiness"]
    assert spawned[0][1] is canonical._health_monitor_loop
    assert spawned[0][2] == {"daemon": True}
    assert canonical._health_monitor_thread is spawned[0][3]
    assert spawned[1][1].__self__ is ava_readiness._monitor
    assert plugin_commands._REGISTRY["ask ava"]["func"] is handler
    assert canonical.get_pending_action()["command"] == "probe"
