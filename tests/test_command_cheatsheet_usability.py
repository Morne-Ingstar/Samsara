"""Command-reference usability regressions from the light-theme review."""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt

from samsara.ui import command_cheatsheet_qt as sheet


@pytest.fixture(scope="module")
def live_rows():
    from tools.dump_command_metadata import build_executor
    return build_executor()._matcher.list_commands()


@pytest.fixture
def window(qapp, tmp_path, monkeypatch, live_rows):
    rows = sheet.catalog_rows(live_rows)
    phrases = [row["phrase"] for row in rows[:6]]
    from samsara import command_stats
    monkeypatch.setattr(
        command_stats, "get_top_commands",
        lambda _limit: [(phrase, len(phrases) - index)
                        for index, phrase in enumerate(phrases)],
    )
    win = sheet._CheatSheetWindow(
        lambda _phrase: None, lambda: live_rows, tmp_path / "palette.json")
    win.show()
    qapp.processEvents()
    yield win, phrases, tmp_path / "palette.json"
    win.deleteLater()


def test_search_is_a_visible_full_size_control(window):
    win, _, _ = window
    css = win._filter.styleSheet()
    assert win._filter.minimumHeight() >= 44
    assert sheet.theme.BG2 in css and sheet.theme.BORDER in css
    assert win._filter.placeholderText() == "Search commands…"
    assert win._filter.accessibleName() == "Search commands"


def test_opacity_and_close_are_readable_controls(window):
    win, _, _ = window
    title = win._title_bar
    assert title._opacity_label.text() == "Opacity"
    assert f"font-size:{sheet.theme.TYPE_BODY}px" in title._opacity_label.styleSheet()
    assert sheet.theme.ACCENT in title._opacity_slider.styleSheet()
    assert title._opacity_slider.accessibleName() == "Window opacity"
    assert title._close_button.text() == "✕"
    assert title._close_button.width() >= 44 and title._close_button.height() >= 44
    assert title._close_button.accessibleName() == "Close command reference"


def test_most_used_is_collapsed_by_default_and_persists(window, qapp):
    win, phrases, palette = window
    assert win._most_used_button is not None
    assert "Most used (6)" in win._most_used_button.text()
    assert not win._most_used_expanded
    assert win.findChildren(sheet._StaticRow) == []

    win._most_used_button.click()
    qapp.processEvents()
    assert win._most_used_expanded
    assert len([row for row in win.findChildren(sheet._StaticRow)
                if row.isVisibleTo(win)]) == len(phrases)
    assert json.loads(palette.read_text(encoding="utf-8"))["most_used_expanded"] is True


def test_search_hides_history_and_still_finds_pinned_commands(window, qapp):
    win, phrases, _ = window
    phrase = phrases[0]
    win._toggle_pin(phrase)
    win._filter.setText(phrase)
    qapp.processEvents()
    assert not win._static_pane.isVisibleTo(win)
    shown = [win._list.item(i).data(Qt.ItemDataRole.UserRole)
             for i in range(win._list.count())]
    assert phrase in shown
    win._filter.clear()
    qapp.processEvents()
    assert win._static_pane.isVisibleTo(win)


def test_narrow_resize_is_discoverable_and_command_rows_wrap(window, qapp):
    win, _, _ = window
    assert win._resize_grip.isVisibleTo(win)
    assert win._resize_grip.accessibleName() == "Resize command reference"
    assert win._resize_grip._cue.text() == "↘"
    assert win.minimumWidth() < sheet._DEFAULT_W
    win.resize(win.minimumWidth(), 360)
    qapp.processEvents()
    assert win.width() == win.minimumWidth() and win.height() == 360
    assert win._list.wordWrap()
    assert win._list.textElideMode() is Qt.TextElideMode.ElideNone


def test_category_row_uses_space_for_commands_instead_of_a_redundant_label(window):
    win, _, _ = window
    assert win._category_bar._combo.accessibleName() == "Command category"
    assert win._category_bar._live_only.text() == "Live only"
    win._category_bar.set_hidden_count(12)
    assert win._category_bar._hidden_lbl.text() == "(12 not live here)"
