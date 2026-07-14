"""Focused regressions for Show Numbers discovery, matching, and Settings visibility."""

import threading
from types import SimpleNamespace

from samsara import plugin_commands
from samsara.command_registry import CommandMatcher
from samsara.commands import CommandExecutor
from samsara.ui.settings_qt import _collect_command_rows


def _plugin_entry(handler):
    entry = {
        "func": handler,
        "phrase": "show numbers",
        "aliases": ["show labels"],
        "source": "test_show_numbers",
        "pack": "accessibility",
    }
    return {"show numbers": entry, "show labels": entry}


def _stale_executor(app):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor._app = app
    executor._matcher_lock = threading.RLock()
    executor.commands = {}
    executor._matcher = CommandMatcher()
    executor._matcher.freeze()
    return executor


def test_loaded_show_numbers_plugin_repairs_stale_matcher_and_executes(monkeypatch):
    calls = []
    registry = _plugin_entry(
        lambda app, remainder: calls.append((app, remainder)) or True
    )
    monkeypatch.setattr(plugin_commands, "_REGISTRY", registry)
    monkeypatch.setattr(plugin_commands, "set_shared_matcher", lambda matcher: None)

    app = SimpleNamespace(
        config={"command_packs": {"accessibility": True}},
        command_matching_enabled=True,
        command_mode_active=True,
    )
    executor = _stale_executor(app)

    result, was_command = executor.process_text("show numbers", app)

    assert (result, was_command) == ("show numbers", True)
    assert calls == [(app, "")]
    entry, remainder = executor._matcher.match("show numbers")
    assert entry is not None
    assert entry.source == "plugin"
    assert entry.pack == "accessibility"
    assert remainder == ""


def test_disabled_accessibility_pack_does_not_self_enable(monkeypatch):
    calls = []
    monkeypatch.setattr(
        plugin_commands,
        "_REGISTRY",
        _plugin_entry(lambda app, remainder: calls.append((app, remainder)) or True),
    )
    monkeypatch.setattr(plugin_commands, "set_shared_matcher", lambda matcher: None)

    app = SimpleNamespace(
        config={"command_packs": {"accessibility": False}},
        command_matching_enabled=True,
        command_mode_active=True,
    )
    executor = _stale_executor(app)

    result, was_command = executor.process_text("show numbers", app)

    assert (result, was_command) == ("show numbers", False)
    assert calls == []


def test_settings_rows_include_plugin_commands_and_alias_search_metadata():
    matcher = CommandMatcher()
    matcher.load_builtins({
        "copy": {"type": "hotkey", "keys": ["ctrl", "c"], "pack": "core"},
    })
    matcher.load_plugins(_plugin_entry(lambda app, remainder: True))
    matcher.freeze()
    executor = SimpleNamespace(
        commands={
            "copy": {"type": "hotkey", "keys": ["ctrl", "c"], "pack": "core"},
        },
        _matcher=matcher,
    )

    rows = {row["phrase"]: row for row in _collect_command_rows(executor)}

    assert rows["copy"]["source"] == "builtin"
    assert rows["show numbers"]["source"] == "plugin"
    assert rows["show numbers"]["pack"] == "accessibility"
    assert rows["show numbers"]["aliases"] == ["show labels"]


def test_explicit_command_lane_bypasses_regular_dictation_command_gate(monkeypatch):
    """A latched COMMAND utterance must match even when ambient matching is off."""
    import dictation
    from samsara.session_modes import UtteranceSignals

    calls = []

    class _Executor:
        commands = {}

        def process_text(self, text, app, force_commands=False):
            calls.append((text, app, force_commands))
            return "show numbers", True

        def find_command(self, text):
            return text

    app = SimpleNamespace(
        _session_mode_manager=None,
        command_executor=_Executor(),
        command_mode_active=True,
        _command_mode_miss_count=0,
        _current_utterance_duration_s=3.5,
        config={
            "command_mode": {
                "command_matching_enabled": False,
                "miss_limit": 5,
            },
            "wake_word_config": {
                "wake_abort_phrase": ["cancel", "cancel dictation", "abort"],
            },
        },
        add_to_history=lambda *args, **kwargs: None,
        _log_history=lambda *args, **kwargs: None,
        _apply_formatting_tokens=lambda text: text,
        _paste_preserving_clipboard=lambda text: None,
        _ava_session_agent_dispatch_fn=lambda text, context: None,
        play_sound=lambda name: None,
        _update_mode_overlay=lambda mode: None,
        exit_command_mode=lambda: None,
    )
    monkeypatch.setattr(dictation, "increment_command_count", lambda name: None)

    manager = dictation.DictationApp._ensure_session_mode_manager(app)
    outcome = manager.dispatch_utterance(
        "show numbers",
        UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,)),
    )

    assert outcome.kind == "command_executed"
    assert calls == [("show numbers", app, True)]
