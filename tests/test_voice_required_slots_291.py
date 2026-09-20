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
    "playlist",
    "album",
    "artist",
    "insert",
    "save snippet",
    "find text",
    "tab number",
    "font size",
    "line spacing",
    "set font",
    "claude message submit",
}


@pytest.fixture(scope="module")
def voice_executor():
    """A production registry snapshot, built before per-test registry isolation."""
    executor = build_executor()
    entries = {
        entry.phrase: entry
        for entry in executor._matcher._sorted
        if any(spec.get("required") for spec in entry.param_schema.values())
    }
    assert set(entries) == REQUIRED_VOICE_PHRASES
    return executor, entries, dict(plugin_commands._REGISTRY)


@pytest.fixture
def live_voice_registry(voice_executor, monkeypatch):
    """Restore the production metadata removed by the test registry fixture."""
    executor, entries, registry = voice_executor
    monkeypatch.setattr(plugin_commands, "_REGISTRY", registry)
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
                      lambda: MatchContext.for_app("chrome.exe"))
        for phrase in sorted(entries):
            executor.process_text(f"{phrase} foo", force_commands=True)

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
