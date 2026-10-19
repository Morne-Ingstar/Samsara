"""Queue 291: required plugin slots must match the exact voice-route args.

This imports no dictation module and intercepts the executor before handlers,
so each real matcher route can be checked without an external side effect.
"""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara.command_registry import DispatchResult, DispatchState
from samsara.command_scope import MatchContext
from samsara.execution_policy import Route, validate_args
from samsara import plugin_commands
from tools.dump_command_metadata import build_executor


REQUIRED_VOICE_PHRASES = {
    "play playlist",
    "play album",
    "play artist",
    "insert",
    "save snippet",
    "find text",
    "tab number",
    "font size",
    "line spacing",
    "set font",
    "claude message submit",
    "choose control",
    "collapse",
    "expand",
    "focus control",
    "toggle",
}

# 345b's controller identities have required tokens, but are never voice slots.
# 262b's send.monitor is a typed slot, not a required free-text remainder.
REQUIRED_TYPED_PHRASES = {
    'send', 'interact focus', 'interact minimize', 'interact maximize',
    'interact move', 'interact snap', 'interact close',
}


@pytest.fixture(scope="module")
def voice_executor():
    """A production registry snapshot, built before per-test registry isolation."""
    with pytest.MonkeyPatch.context() as clean:
        clean.setattr(plugin_commands, '_REGISTRY', {})
        clean.setattr(plugin_commands, '_shared_matcher', None)
        executor = build_executor()
        registry = dict(plugin_commands._REGISTRY)
    required = {
        entry.phrase: entry
        for entry in executor._matcher._sorted
        if any(spec.get("required") for spec in entry.param_schema.values())
    }
    assert set(required) == REQUIRED_VOICE_PHRASES | REQUIRED_TYPED_PHRASES
    entries = {phrase: entry for phrase, entry in required.items()
               if entry.param_schema.get('remainder', {}).get('required')}
    assert set(entries) == REQUIRED_VOICE_PHRASES
    assert all(entry.voice_triggerable for entry in entries.values())
    assert all(not required[phrase].voice_triggerable
               for phrase in REQUIRED_TYPED_PHRASES if phrase.startswith('interact '))
    assert required['send'].param_schema['monitor'] == {'type': 'monitor', 'required': True}
    return executor, entries, registry


@pytest.fixture
def live_voice_registry(voice_executor, monkeypatch):
    """Restore the production metadata removed by the test registry fixture."""
    executor, entries, registry = voice_executor
    monkeypatch.setattr(plugin_commands, "_REGISTRY", dict(registry))
    executor._matcher.set_enabled_packs({entry.pack for entry in entries.values()})
    return executor, entries


def test_required_slots_accept_the_actual_exact_voice_remainder(live_voice_registry, monkeypatch):
    executor, entries = live_voice_registry
    captured = {}

    def capture(command_id, app_instance=None, **kwargs):
        captured[command_id] = kwargs
        return DispatchResult(DispatchState.REJECTED, command_id, command_id)

    with monkeypatch.context() as patch:
        patch.setattr(executor, "execute_canonical", capture)
        patch.setattr(executor._matcher, "current_context",
                      lambda: MatchContext.for_app("chrome.exe", tags={'click_by_name.pending'}))
        for phrase in sorted(entries):
            executor.process_text(f"{phrase} foo", force_commands=True)
        for alias in ('playlist', 'album', 'artist'):
            result = executor.process_text(f"{alias} foo", force_commands=True)
            assert result.phrase == f'play {alias}'

    assert set(captured) == REQUIRED_VOICE_PHRASES
    for phrase, entry in entries.items():
        actual = captured[phrase]
        assert actual["route"] is Route.EXACT
        assert actual["args"] == {"remainder": "foo"}
        assert validate_args(entry.param_schema, actual["args"], route=actual["route"]) is None


def test_bare_playlist_is_denied_as_a_missing_required_remainder(live_voice_registry):
    executor, _entries = live_voice_registry

    result = executor.process_text("playlist", force_commands=True)

    assert result.state is DispatchState.REJECTED
    assert result.detail == {
        "reason": "invalid_args",
        "detail": "missing required argument 'remainder'",
    }


def test_send_with_a_spoken_monitor_satisfies_its_required_slot(live_voice_registry, monkeypatch):
    """A named monitor reaches policy using synthetic screens, without effects."""
    from plugins.commands import windows
    from samsara import arg_types

    executor, _entries = live_voice_registry
    captured = {}
    # Collection may install Win32 mocks; neither those nor the real desktop
    # should decide whether this voice-slot regression passes.
    monitors = [
        {'handle': 1, 'index': 1, 'rect': (0, 0, 1920, 1080),
         'primary': True, 'device': 'left'},
        {'handle': 2, 'index': 2, 'rect': (1920, 0, 3840, 1080),
         'primary': False, 'device': 'right'},
    ]
    monkeypatch.setattr(windows, 'get_monitors', lambda: monitors)
    monkeypatch.setattr(windows.win32api, 'EnumDisplayMonitors',
                        lambda: [(m['handle'], None, m['rect']) for m in monitors])
    # A prior module reload may leave the static validator bound to its old
    # module globals. Validate through the same module whose screens we fake.
    monkeypatch.setitem(arg_types._ARG_TYPE_VALIDATORS, 'monitor', windows._validate_monitor)

    def capture(command_id, app_instance=None, **kwargs):
        captured.update(command_id=command_id, **kwargs)
        return DispatchResult(DispatchState.REJECTED, command_id, command_id)

    monkeypatch.setattr(executor, 'execute_canonical', capture)
    executor._matcher.set_enabled_packs({'window-management'})
    executor.process_text('send chrome to left screen', force_commands=True)
    assert captured['command_id'] == 'send'
    assert captured['route'] is Route.EXACT
    assert captured['args'] == {
        'app_name': 'chrome', 'monitor': 'left screen',
        'remainder': 'chrome to left screen',
    }
    entry = executor._matcher._entries['send']
    assert validate_args(entry.param_schema, captured['args'], route=captured['route']) is None
