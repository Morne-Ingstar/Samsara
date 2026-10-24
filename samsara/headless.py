"""Shared, resource-injectable composition and a finite, read-only CLI check.

This checks imports and composition, never microphone/desktop dictation.
Nothing in this module acquires a device, reads a profile or starts a thread.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class LazyImport:
    """Keep legacy patchable input names without importing their backend."""

    def __init__(self, module, attribute=None):
        self.module, self.attribute = module, attribute

    def resolve(self):
        value = importlib.import_module(self.module)
        return getattr(value, self.attribute) if self.attribute else value

    def __getattr__(self, name):
        return getattr(self.resolve(), name)

    def __call__(self, *args, **kwargs):
        return self.resolve()(*args, **kwargs)


class DeferredController:
    def __init__(self, factory):
        self.factory = factory
        self.controller = None

    def __getattr__(self, name):
        if self.controller is None:
            self.controller = self.factory()
        return getattr(self.controller, name)


class KeyMap:
    """Class/instance descriptor preserving the native desktop key values."""

    def __init__(self, factory):
        self.factory = factory

    def __get__(self, instance, owner):
        return self.factory()


class ResourceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ResourcePort:
    value: Any = None
    reason: str = 'Disabled during the headless composition check.'  # PLACEHOLDER COPY (851)

    @property
    def available(self):
        return self.value is not None

    def require(self):
        if not self.available:
            raise ResourceUnavailable(self.reason)
        return self.value


RESOURCE_NAMES = ('keyboard', 'mouse', 'audio', 'hotkeys', 'speech', 'models',
                  'network', 'desktop', 'profile_writes', 'plugin_registry', 'plugin_services', 'hermes')


@dataclass
class ResourcePorts:
    """Explicit resource handles; absence is unavailable, never fake success."""
    ports: dict[str, ResourcePort] = field(default_factory=dict)
    key_map: dict = field(default_factory=dict)

    def __post_init__(self):
        unknown = self.ports.keys() - set(RESOURCE_NAMES)
        if unknown:
            raise ValueError(f'Unknown resource ports: {sorted(unknown)}')
        self.ports = {name: self.ports.get(name, ResourcePort()) for name in RESOURCE_NAMES}
        if not self.ports['plugin_registry'].available:
            self.ports['plugin_registry'] = ResourcePort(
                reason='Plugin discovery requires desktop imports and generated theme assets; no registry supplied.'  # PLACEHOLDER COPY (851)
            )

    def controller(self, name):
        return DeferredController(self.ports[name].require)


@dataclass
class Services:
    command_executor: Any
    settings_schema: dict
    settings_defaults: dict
    handlers: dict


def compose_services(app, commands_path=None, *, resources=None, executor_factory=None,
                     plugins_dir=None):
    """The app and headless entry share the real executor, matcher and schema.

    With resources=None the desktop executor retains its service-start policy.
    Injected ports disable automatic service starts; no UI or app is constructed.
    """
    from .config_schema import SETTINGS_SCHEMA
    from .config_defaults import DEFAULTS
    from .handlers import build_handler_registry
    if executor_factory is None:
        from .commands import CommandExecutor
        executor_factory = CommandExecutor
    kwargs = {'app': app}
    if resources is not None:
        kwargs['resources'] = resources
    if plugins_dir is not None:
        kwargs['plugins_dir'] = plugins_dir
    executor = executor_factory(commands_path, **kwargs)
    return Services(executor, SETTINGS_SCHEMA, DEFAULTS, build_handler_registry())


class HeadlessEffectError(RuntimeError):
    pass


def deny_effects():
    """Deny resource acquisition even if a future plugin adds an eager effect.

    Used only by the isolated check, never installed during package import.
    """
    import builtins
    import contextlib
    import importlib.abc
    import io
    import os
    import socket
    import subprocess
    import sys
    import threading
    from unittest.mock import patch

    @contextlib.contextmanager
    def guarded():
        attempts = []

        def deny(name):
            def refused(*args, **kwargs):
                attempts.append(name)
                raise HeadlessEffectError(f'Headless check denied {name}')
            return refused

        class Imports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] in {
                    'pynput', 'keyboard', 'pyautogui', 'sounddevice', 'pyaudio',
                    'faster_whisper', 'torch', 'ctranslate2', 'onnxruntime',
                    'openwakeword', 'dictation',
                }:
                    deny('import ' + fullname)()

        def readonly(original):
            def opened(file, mode='r', *args, **kwargs):
                if any(flag in mode for flag in 'wax+'):
                    deny('file write')()
                return original(file, mode, *args, **kwargs)
            return opened

        original_os_open = os.open

        def open_fd(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                deny('file descriptor write')()
            return original_os_open(path, flags, *args, **kwargs)

        finder = Imports()
        sys.meta_path.insert(0, finder)
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(sys, 'dont_write_bytecode', True))
                for module, name, replacement in (
                    (builtins, 'open', readonly(builtins.open)),
                    (io, 'open', readonly(io.open)), (os, 'open', open_fd),
                    (threading.Thread, 'start', deny('thread start')),
                    (socket.socket, 'connect', deny('network connect')),
                    (socket.socket, 'connect_ex', deny('network connect')),
                    (socket, 'create_connection', deny('network connect')),
                    (socket.socket, 'bind', deny('network bind')),
                    (subprocess, 'Popen', deny('process start')),
                    (os, 'system', deny('process start')),
                ):
                    stack.enter_context(patch.object(module, name, replacement))
                for name in ('mkdir', 'makedirs', 'remove', 'unlink', 'rmdir', 'rename', 'replace'):
                    stack.enter_context(patch.object(os, name, deny('filesystem mutation')))
                yield attempts
        finally:
            sys.meta_path.remove(finder)

    return guarded()


def check(*, resources=None, commands_path=None, plugins_dir=None):
    """Compose actual shared services; return metadata only, never user text."""
    from types import SimpleNamespace
    resources = resources if resources is not None else ResourcePorts()
    statuses = {name: {'state': 'ready' if port.available else 'unavailable',
                       'reason': 'Injected resource handle.' if port.available else port.reason}
                for name, port in resources.ports.items()}
    try:
        from copy import deepcopy
        from .config_defaults import DEFAULTS
        config = {}
        for key, value in DEFAULTS.items():
            node = config
            *parents, leaf = key.split('.')
            for parent in parents:
                node = node.setdefault(parent, {})
            node[leaf] = deepcopy(value)
        graph = compose_services(SimpleNamespace(config=config), commands_path,
                                 resources=resources, plugins_dir=plugins_dir)
        executor = graph.command_executor
        if not executor.commands or not graph.settings_schema or not graph.handlers:
            raise ValueError('Required composition is empty')
        statuses.update({
            'command_registry': {'state': 'ready', 'reason': 'Production executor and matcher composed with the reported registry port.',
                                 'entries': len(executor._matcher._entries)},
            'settings_model': {'state': 'ready', 'reason': 'Canonical Settings schema and defaults loaded.',
                               'entries': len(graph.settings_schema)},
            'command_handlers': {'state': 'ready', 'reason': 'Production handler registry composed.',
                                 'entries': len(graph.handlers)},
        })
        return {'ok': True, 'scope': 'import/composition only', 'services': statuses}
    except Exception as exc:
        for name in ('command_registry', 'settings_model', 'command_handlers'):
            statuses[name] = {'state': 'unavailable', 'reason': f'Composition failed: {type(exc).__name__}'}
        return {'ok': False, 'scope': 'import/composition only', 'services': statuses}


def main(argv=None):
    """Run with a disposable empty profile and exit without application boot."""
    import contextlib
    import io
    import json
    import logging
    import os
    import tempfile
    from unittest.mock import patch
    argv = [] if argv is None else list(argv)
    if argv not in ([], ['--headless-check']):
        return 2
    with tempfile.TemporaryDirectory(prefix='samsara-headless-') as temporary:
        profile = str(Path(temporary) / 'profile')
        env = {key: profile for key in ('HOME', 'USERPROFILE', 'SAMSARA_HOME_DIR',
                                       'APPDATA', 'LOCALAPPDATA', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME')}
        env.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1')
        with patch.dict(os.environ, env), patch.object(logging.getLogger(), 'handlers', [logging.NullHandler()]), deny_effects() as attempts:
            # Existing production discovery prints counts/timings. Keep JSON stdout finite.
            with contextlib.redirect_stdout(io.StringIO()):
                result = check()
            if attempts:
                result['ok'] = False
                result['denied_effects'] = sorted(set(attempts))
        if Path(profile).exists():
            result['ok'] = False
            result['profile_changed'] = True
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
