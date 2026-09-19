"""Command-reference usability regressions from the light-theme review."""
from __future__ import annotations

import json
import logging

import pytest
from PySide6.QtCore import Qt

from samsara.ui import command_cheatsheet_qt as sheet


def test_default_palette_state_lives_under_samsara_home(tmp_path, monkeypatch):
    cwd = tmp_path / "working-directory"
    home = tmp_path / "profile"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(home))

    assert sheet._palette_state_path() == home / "command_palette.json"
    assert not (cwd / "command_palette.json").exists()


def test_legacy_cwd_palette_is_migrated_once(tmp_path, monkeypatch):
    cwd = tmp_path / "working-directory"
    home = tmp_path / "profile"
    cwd.mkdir()
    legacy = cwd / "command_palette.json"
    legacy.write_text('{"opacity": 0.7}', encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(home))

    target = sheet._palette_state_path()
    assert target.read_text(encoding="utf-8") == '{"opacity": 0.7}'
    assert not legacy.exists()

    legacy.write_text('{"opacity": 0.4}', encoding="utf-8")
    assert sheet._palette_state_path() == target
    assert legacy.exists()  # the migration marker prevents stale state returning
    assert json.loads(target.read_text(encoding="utf-8"))["opacity"] == 0.7


def test_palette_path_resolution_does_not_fail_when_profile_is_unwritable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path / "read-only-profile"))

    def deny_mkdir(*_args, **_kwargs):
        raise PermissionError("read-only profile")

    monkeypatch.setattr(sheet.Path, "mkdir", deny_mkdir)
    assert sheet._palette_state_path() == tmp_path / "read-only-profile" / "command_palette.json"


def _make_position_window(qapp, path):
    return sheet._CheatSheetWindow(lambda _phrase: None, lambda: [], path)


def test_saved_geometry_on_removed_screen_uses_foreground_screen_default(
    qapp, tmp_path, monkeypatch, caplog
):
    primary = qapp.primaryScreen()
    area = primary.availableGeometry()
    path = tmp_path / "palette.json"
    path.write_text(json.dumps({"geometry": {"x": 100000, "y": -100000}}),
                    encoding="utf-8")
    monkeypatch.setattr(
        sheet._CheatSheetWindow, "_foreground_screen", lambda _self: primary
    )

    with caplog.at_level(logging.INFO, logger=sheet.logger.name):
        win = _make_position_window(qapp, path)
        win.show()
        qapp.processEvents()

    assert area.contains(win.frameGeometry().center())
    assert win.x() == area.right() - win.width() - 40
    messages = [record.getMessage() for record in caplog.records]
    opened = next(message for message in messages if "[CHEATSHEET] Opened" in message)
    assert "geometry=(" in opened and f"screen={primary.name()}" in opened
    assert "saved_rejected=True" in opened
    win.deleteLater()


def test_saved_geometry_on_present_screen_is_honoured(qapp, tmp_path):
    area = qapp.primaryScreen().availableGeometry()
    x, y = area.left() + 40, area.top() + 40
    path = tmp_path / "palette.json"
    path.write_text(json.dumps({"geometry": {
        "x": x, "y": y, "w": sheet._DEFAULT_W, "h": sheet._DEFAULT_H,
    }}), encoding="utf-8")

    win = _make_position_window(qapp, path)
    win.show()
    qapp.processEvents()
    assert win.x() == x and win.y() == y
    win.deleteLater()


@pytest.mark.parametrize("contents", [None, "not-json"], ids=["missing", "corrupt"])
def test_missing_or_corrupt_palette_uses_defaults_without_exception(
    contents, qapp, tmp_path
):
    path = tmp_path / "palette.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    win = _make_position_window(qapp, path)
    assert win._geom == {
        "x": None, "y": None, "w": sheet._DEFAULT_W, "h": sheet._DEFAULT_H,
    }
    assert win._opacity == 0.85
    win.show()
    qapp.processEvents()
    assert win.isVisible()
    win.deleteLater()


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
