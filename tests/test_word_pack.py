"""Focused checks for the Microsoft Word command pack (283)."""
from __future__ import annotations

import builtins
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugins.commands import word


@pytest.fixture
def app():
    return SimpleNamespace(
        _show_outcome_chip=MagicMock(),
        audio_coordinator=SimpleNamespace(speak=MagicMock()),
    )


@pytest.mark.parametrize(("handler", "keys"), [
    (word.font_bigger, ("ctrl", "]")),
    (word.font_smaller, ("ctrl", "[")),
    (word.bold_that, ("ctrl", "b")),
    (word.italic_that, ("ctrl", "i")),
    (word.underline_that, ("ctrl", "u")),
    (word.single_spacing, ("ctrl", "1")),
    (word.one_and_a_half_spacing, ("ctrl", "5")),
    (word.double_spacing, ("ctrl", "2")),
    (word.align_left, ("ctrl", "l")),
    (word.align_center, ("ctrl", "e")),
    (word.align_right, ("ctrl", "r")),
    (word.justify_text, ("ctrl", "j")),
    (word.bullet_list, ("ctrl", "shift", "l")),
    (word.select_paragraph, ("ctrl", "shift", "down")),
    (word.undo_in_word, ("ctrl", "z")),
    (word.redo_in_word, ("ctrl", "y")),
])
def test_shortcuts_require_focused_word_and_send_the_right_keys(monkeypatch, app, handler, keys):
    hotkey = MagicMock()
    monkeypatch.setattr(word, "_word_is_focused", lambda: True)
    monkeypatch.setattr(word.pyautogui, "hotkey", hotkey)

    assert handler(app) is True
    hotkey.assert_called_once_with(*keys)


def test_shortcut_declines_with_a_word_focused_chip(monkeypatch, app):
    monkeypatch.setattr(word, "_word_is_focused", lambda: False)
    send = MagicMock()
    monkeypatch.setattr(word.pyautogui, "hotkey", send)

    assert word.bold_that(app) is False
    send.assert_not_called()
    app._show_outcome_chip.assert_called_with("Word isn't focused", "warning")


def test_com_worker_initializes_its_thread_and_never_launches_word(monkeypatch):
    calls = []
    active = SimpleNamespace(marker="active")
    pythoncom = SimpleNamespace(
        CoInitialize=lambda: calls.append("init"),
        CoUninitialize=lambda: calls.append("uninit"),
    )
    client = SimpleNamespace(GetActiveObject=lambda name: calls.append(name) or active)
    real_import = builtins.__import__

    def imported(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pythoncom":
            return pythoncom
        if name == "win32com.client":
            return client
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", imported)
    status, value = word._com_call(lambda application: application.marker)

    assert (status, value) == ("ok", "active")
    assert calls == ["init", "Word.Application", "uninit"]


@pytest.mark.parametrize(("handler", "remainder", "attribute", "expected"), [
    (word.font_size, "18", "font_size", 18),
    (word.line_spacing, "1.5", "spacing_rule", 1),
])
def test_parameterised_com_calls_use_the_current_selection(monkeypatch, app, handler, remainder, attribute, expected):
    selection = SimpleNamespace(
        Font=SimpleNamespace(Size=None),
        ParagraphFormat=SimpleNamespace(LineSpacingRule=None),
    )

    def call(_app, action):
        return True, action(selection)

    monkeypatch.setattr(word, "_selection_call", call)
    assert handler(app, remainder) is True
    value = selection.Font.Size if attribute == "font_size" else selection.ParagraphFormat.LineSpacingRule
    assert value == expected


@pytest.mark.parametrize("remainder", ["5", "73", "big", ""])
def test_font_size_bounds_are_refused_before_com(monkeypatch, app, remainder):
    call = MagicMock()
    monkeypatch.setattr(word, "_selection_call", call)

    assert word.font_size(app, remainder) is False
    call.assert_not_called()
    app._show_outcome_chip.assert_called_with("Font size must be from 6 to 72", "warning")


def test_set_font_accepts_only_word_font_names(monkeypatch, app):
    selection = SimpleNamespace(
        Application=SimpleNamespace(FontNames=["Aptos", "Courier New"]),
        Font=SimpleNamespace(Name=None),
    )
    monkeypatch.setattr(word, "_selection_call", lambda _app, action: (True, action(selection)))

    assert word.set_font(app, "courier new") is True
    assert selection.Font.Name == "Courier New"
    assert word.set_font(app, "made up font") is False
    app._show_outcome_chip.assert_called_with("Word doesn't have made up font", "warning")


@pytest.mark.parametrize(("handler", "unit"), [
    (word.select_sentence, 3), (word.select_line, 5), (word.select_word, 2),
])
def test_selection_commands_expand_only_the_current_selection(monkeypatch, app, handler, unit):
    monkeypatch.setattr(word, "_word_is_focused", lambda: True)  # 823: pin the Word tier.
    selection = SimpleNamespace(Expand=MagicMock())
    monkeypatch.setattr(word, "_selection_call", lambda _app, action: (True, action(selection)))

    assert handler(app) is True
    selection.Expand.assert_called_once_with(unit)


def test_numbered_list_uses_only_the_selected_range(monkeypatch, app):
    listing = SimpleNamespace(ListType=0, ApplyNumberDefault=MagicMock(), RemoveNumbers=MagicMock())
    selection = SimpleNamespace(Range=SimpleNamespace(ListFormat=listing))
    monkeypatch.setattr(word, "_selection_call", lambda _app, action: (True, action(selection)))

    assert word.numbered_list(app) is True
    listing.ApplyNumberDefault.assert_called_once_with()
    listing.RemoveNumbers.assert_not_called()


@pytest.mark.parametrize(("status", "copy"), [
    ("not_open", "Word isn't open"),
    ("error", "Word couldn't complete that"),
    ("unavailable", "Word automation unavailable"),
])
def test_com_failures_are_spoken_and_chipped(monkeypatch, app, status, copy):
    monkeypatch.setattr(word, "_word_is_focused", lambda: True)
    monkeypatch.setattr(word, "_com_call", lambda _action: (status, None))

    assert word.font_size(app, "12") is False
    app._show_outcome_chip.assert_called_with(copy, "warning")
    app.audio_coordinator.speak.assert_called_with(copy, category="agent_response", interruptible=False)


def test_word_count_speaks_the_current_selection_count(monkeypatch, app):
    selection = SimpleNamespace(Words=SimpleNamespace(Count=23))
    monkeypatch.setattr(word, "_selection_call", lambda _app, action: (True, action(selection)))

    assert word.word_count(app) is True
    app._show_outcome_chip.assert_called_with("23 words", "success")


def test_word_phrase_is_whole_utterance_safe_in_dictation():
    """Use a fresh process: direct unit imports intentionally do not become
    production plugin-loader imports in this process."""
    code = '''
import json
from samsara import command_catalog as cc
from samsara.intent.resolve import IntentResolver
from tools.dump_command_metadata import build_executor
records = cc.to_document(cc.build_catalog(build_executor()))["commands"]
resolver = IntentResolver(records)
arguments = {"font size": "font size 18", "line spacing": "line spacing 1.5", "set font": "set font Aptos"}
phrases = [arguments.get(row["aliases"][0], row["aliases"][0]) for row in records
           if row["plugin"] == "word"]
print(json.dumps([(p, len(p.split()), resolver.resolve(p).kind,
                  resolver.resolve(f"I will {p} after this sentence").kind) for p in phrases]))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    rows = json.loads(out.stdout.strip().splitlines()[-1])
    assert len(rows) == 24
    assert all(words >= 2 and exact == "resolved" and sentence != "resolved"
               for _phrase, words, exact, sentence in rows), rows
