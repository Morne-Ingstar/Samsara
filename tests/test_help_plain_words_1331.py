"""1331: the ten help replies match the visible offline help."""
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt

from samsara import samsara_navigation
from samsara.samsara_navigation import open_place
from samsara.ui import command_cheatsheet_qt as reference
from samsara.ui.settings.presentation import SettingRow
from tests.test_voice_help_search_1135 import CASES, offline, settle
from tests.test_help_intents_943 import executor, loaded_registry, rig


INTERNAL_WORDS = re.compile(
    r"\b(?:lane|legacy|retained for compatibility|this build|calibration|"
    r"local AI agent|natural language)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("question,destination", [(q, d) for q, d, _ in CASES])
def test_frozen_answer_matches_visible_first_sentence(
    rig, offline, qapp, qt_widget_scope, monkeypatch, tmp_path, question, destination,
):
    command_executor, _, _ = rig
    offline.audio_coordinator = SimpleNamespace(speak=Mock())
    offline.command_executor = command_executor
    offline.command_matching_enabled = True
    offline.command_mode_active = False
    offline._schedule_ui = lambda action: action()
    monkeypatch.setattr(command_executor, "_app", offline)
    monkeypatch.setattr(samsara_navigation, "open_place", open_place)

    unanswered = destination is None and "purple kite" in question
    if unanswered:
        monkeypatch.setattr(reference, "_scope_context", lambda: None)
        monkeypatch.setattr(reference._CheatSheetWindow, "_foreground_screen", lambda _: None)
        offline.cheat_sheet = reference.CommandCheatSheetQt(
            commands_cb=command_executor._matcher.list_commands,
            config_cb=lambda: offline.config,
            palette_path=tmp_path / "palette.json",
        )

    assert command_executor.process_text(question, force_commands=True).succeeded
    spoken = offline.audio_coordinator.speak.call_args.args[0]
    kind, key = destination or (None, None)
    if kind == "quick_reference":
        window = offline._quick_reference_qt._window
    elif unanswered:
        window = offline.cheat_sheet._window
    else:
        window = offline._settings_qt._window

    with qt_widget_scope(window):
        settle(qapp)
        if kind == "quick_reference":
            label = window._voice_help.active.body
        elif kind == "term":
            page = window._stack.currentWidget()
            assert page.title == "Help"
            label = page._offline_help.active.body
        elif kind == "page":
            assert window._stack.currentWidget().title == key
            label = window._local_page_help.controller.active.body
        elif unanswered:
            label = window._live_controls
            assert label.text() == "No answer found. Search commands here."
            assert window._filter.isVisible() and not window._filter.text()
            assert window._list.isVisible() and window._list.count() > 0
            notice = label.mapTo(window, label.rect().bottomLeft())
            commands = window._list.mapTo(window, window._list.rect().topLeft())
            assert notice.y() < commands.y()
            rows = [window._list.item(i) for i in range(window._list.count())]
            first_command = next(row for row in rows if row.data(Qt.ItemDataRole.UserRole))
            assert window._list.viewport().rect().intersects(window._list.visualItemRect(first_command))
            window._filter.setText(first_command.data(Qt.ItemDataRole.UserRole))
            assert 0 < window._list.count() < len(rows)
        else:
            page = window._stack.currentWidget()
            assert page.title == "General"
            label = next(row.detail for row in page.findChildren(SettingRow) if row.title == "Microphone")

        shown = label.text()
        assert label.isVisible() and shown.strip() and not INTERNAL_WORDS.search(shown)
        first, separator, _ = shown.partition(". ")
        if not unanswered:
            assert spoken == first + ("." if separator else "")
        else:
            assert spoken == shown
            reference_window = offline.cheat_sheet._window
            offline.cheat_sheet.show_filtered("")
            assert offline.cheat_sheet._window is reference_window and label.isHidden()
