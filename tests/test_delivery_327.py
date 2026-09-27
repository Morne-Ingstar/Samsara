"""Exercise wake delivery without importing the running desktop app."""
import ast
import copy
import logging
import sys
import time
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import config_defaults, paste


def production():
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    app_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "DictationApp")
    methods = {node.name: node for node in app_class.body if isinstance(node, ast.FunctionDef)}
    decoder = methods["_decode_wake_word_buffer"]
    branch = next(node for node in ast.walk(decoder) if isinstance(node, ast.If)
                  and isinstance(node.test, ast.Compare)
                  and isinstance(node.test.left, ast.Attribute)
                  and node.test.left.attr == "app_state"
                  and any(isinstance(value, ast.Tuple) for value in node.test.comparators))
    route = ast.parse("def route(self, text):\n text_lower = text.lower()\n ww_config = self.config.get('wake_word_config', {})\n stage_timing = None\n").body[0]
    route.body.append(copy.deepcopy(branch))
    namespace = {
        "logger": logging.getLogger(__name__), "time": time,
        "config_defaults": config_defaults, "_WAKE_SESSION_SEND_WORDS": ["over"],
        "pyautogui": SimpleNamespace(press=Mock()),
        "current_stage_timing": lambda: None,
        "_get_foreground_hwnd": lambda: 42,
        "diagnostics": SimpleNamespace(record=Mock(), DiagRecord=SimpleNamespace),
        "_languages": SimpleNamespace(describe_diagnostics_language=lambda *args: "en"),
    }
    module = ast.Module(body=[copy.deepcopy(methods["_output_dictation"]), route], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace


@pytest.fixture
def app():
    namespace = production()
    instance = SimpleNamespace(
        config={"wake_word_config": {}, "auto_paste": True, "add_trailing_space": False},
        app_state="wake_session", wake_dictation_buffer=[], _wake_profile_active=True,
        _wake_session_first_chunk=True, _wake_session_mode="focus_dictate",
        _wake_session_send_word="over", _wake_session_target_process="editor.exe",
        _wake_session_target_hwnd=42,
        _apply_verbatim_if_active=lambda value: value,
        _deliver_text_to_focused_editor=Mock(return_value=False),
        _paste_preserving_clipboard=Mock(return_value=True),
        _foreground_process_name=Mock(return_value="editor.exe"),
        _emit_wake_trace=Mock(), _show_outcome_chip=Mock(),
        play_sound=Mock(), _end_wake_session=Mock(), _reset_wake_dictation=Mock(),
        add_to_history=Mock(), _log_history=Mock(), _notify_main_window=Mock(),
    )
    instance._output_dictation = MethodType(namespace["_output_dictation"], instance)
    instance.route = MethodType(namespace["route"], instance)
    return instance, namespace


def test_rejected_first_chunk_keeps_draft_and_never_sends(app):
    instance, namespace = app
    instance.route("synthetic draft over")
    namespace["pyautogui"].press.assert_not_called()
    instance._end_wake_session.assert_not_called()
    assert instance.wake_dictation_buffer == ["synthetic draft"]
    assert instance._wake_failed_draft == "synthetic draft"
    assert instance._wake_session_first_chunk is True
    instance.add_to_history.assert_not_called()
    instance.play_sound.assert_any_call("error")
    assert "draft kept" in instance._show_outcome_chip.call_args.args[0].lower()


def test_auto_paste_off_refuses_enter(app):
    instance, namespace = app
    instance.config["auto_paste"] = False
    instance.route("synthetic draft over")
    namespace["pyautogui"].press.assert_not_called()
    instance._end_wake_session.assert_not_called()
    assert instance.wake_dictation_buffer == ["synthetic draft"]
    assert "Auto-paste off" in instance._show_outcome_chip.call_args.args[0]


def test_previous_rejection_blocks_later_send(app):
    instance, namespace = app
    instance.route("synthetic draft")
    instance._deliver_text_to_focused_editor.return_value = True
    instance.route("over")
    namespace["pyautogui"].press.assert_not_called()
    assert instance.wake_dictation_buffer == ["synthetic draft"]


def test_success_records_only_after_delivery(app):
    instance, namespace = app
    events = []
    instance._deliver_text_to_focused_editor.side_effect = lambda value: events.append("paste") or True
    instance.add_to_history.side_effect = lambda *args, **kwargs: events.append("history")
    assert instance._output_dictation("synthetic draft") is True
    assert events == ["paste", "history"]
    assert instance._wake_session_first_chunk is False


def test_focus_change_refuses_enter(app):
    instance, namespace = app
    instance._deliver_text_to_focused_editor.return_value = True
    instance._foreground_process_name.return_value = "other.exe"
    instance.route("synthetic draft over")
    namespace["pyautogui"].press.assert_not_called()


def test_successful_paste_precedes_enter(app):
    instance, namespace = app
    events = []
    instance._deliver_text_to_focused_editor.side_effect = lambda value: events.append("paste") or True
    namespace["pyautogui"].press.side_effect = lambda value: events.append("enter")
    instance.route("synthetic draft over")
    assert events == ["paste", "enter"]
    instance._end_wake_session.assert_called_once()


def test_rejected_later_chunk_blocks_send(app):
    instance, namespace = app
    instance._deliver_text_to_focused_editor.return_value = True
    instance.route("first part")
    instance._paste_preserving_clipboard.return_value = False
    instance.route("second part")
    instance.route("over")
    namespace["pyautogui"].press.assert_not_called()
    assert instance.wake_dictation_buffer == ["second part"]
    assert instance._wake_failed_draft == "second part"


@pytest.mark.parametrize("composer_text,should_send", [
    ("", False), ("wrong draft", False), ("synthetic draft", True),
])
def test_claude_send_requires_observed_composer(app, monkeypatch, composer_text, should_send):
    from plugins.commands import message_claude
    from samsara.agent_channel import claude_reply
    import win32gui

    instance, namespace = app
    instance._wake_session_target_process = "claude.exe"
    instance._foreground_process_name.return_value = "claude.exe"
    instance._deliver_text_to_focused_editor.return_value = True
    monkeypatch.setattr(message_claude.ClaudeUiaAdapter, "find_composer", lambda self, hwnd: object())
    monkeypatch.setattr(message_claude.ClaudeUiaAdapter, "read_composer",
                        lambda self, composer: composer_text)
    monkeypatch.setattr(claude_reply, "before_send", lambda *args, **kwargs: None)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 42)
    instance.route("synthetic draft over")
    assert namespace["pyautogui"].press.called is should_send


def test_focused_editor_seam_returns_paste_result(monkeypatch):
    import pyautogui

    monkeypatch.setattr(pyautogui, "press", Mock())
    monkeypatch.setattr(paste.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(paste, "_get_foreground_hwnd", lambda: 42)
    instance = SimpleNamespace(_wake_session_target_hwnd=42,
                               _paste_preserving_clipboard=Mock(return_value=False))
    assert paste.deliver_text_to_focused_editor(instance, "draft") is False
    assert instance._paste_preserving_clipboard.call_args.kwargs["before_paste"]() is True
    instance._paste_preserving_clipboard.return_value = True
    assert paste.deliver_text_to_focused_editor(instance, "draft") is True


def test_dictation_module_is_not_imported():
    assert "dictation" not in sys.modules
