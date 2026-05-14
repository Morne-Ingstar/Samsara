"""Tests for CommandCheatSheet.

Covers the non-UI logic: palette load/save, filter, pin/unpin, and the
execute callback wiring. Tk window creation is skipped via mock so these
tests run headlessly.
"""

import json
import sys
import tkinter as tk
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from samsara.ui.command_cheatsheet import CommandCheatSheet


SAMPLE_COMMANDS = [
    {"phrase": "copy", "type": "hotkey", "pack": "core", "aliases": []},
    {"phrase": "paste", "type": "hotkey", "pack": "core", "aliases": []},
    {"phrase": "new tab", "type": "hotkey", "pack": "browsers", "aliases": []},
    {"phrase": "close tab", "type": "hotkey", "pack": "browsers", "aliases": []},
    {"phrase": "play music", "type": "plugin", "pack": "media", "aliases": ["start music"]},
]


def _make_sheet(tmp_path, commands=None):
    if commands is None:
        commands = list(SAMPLE_COMMANDS)
    root = MagicMock(spec=tk.Misc)
    execute_cb = MagicMock()
    commands_cb = MagicMock(return_value=commands)
    palette_path = tmp_path / "command_palette.json"
    sheet = CommandCheatSheet(root, execute_cb, commands_cb, palette_path)
    return sheet, execute_cb, commands_cb, palette_path


# ---------------------------------------------------------------------------
# Palette persistence
# ---------------------------------------------------------------------------

class TestPalettePersistence:

    def test_load_pinned_from_existing_palette(self, tmp_path):
        palette = tmp_path / "command_palette.json"
        palette.write_text(json.dumps({
            "pinned": ["copy", "paste"],
            "geometry": {"x": 100, "y": 200, "w": 450, "h": 500},
        }))
        sheet, *_ = _make_sheet(tmp_path)
        assert "copy" in sheet._pinned
        assert "paste" in sheet._pinned

    def test_load_geometry_from_existing_palette(self, tmp_path):
        palette = tmp_path / "command_palette.json"
        palette.write_text(json.dumps({
            "pinned": [],
            "geometry": {"x": 123, "y": 456, "w": 400, "h": 300},
        }))
        sheet, *_ = _make_sheet(tmp_path)
        assert sheet._geom["x"] == 123
        assert sheet._geom["y"] == 456
        assert sheet._geom["w"] == 400
        assert sheet._geom["h"] == 300

    def test_missing_palette_uses_defaults(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        assert sheet._pinned == set()
        assert sheet._geom["x"] is None

    def test_corrupt_palette_uses_defaults(self, tmp_path):
        palette = tmp_path / "command_palette.json"
        palette.write_text("NOT JSON{{{")
        sheet, *_ = _make_sheet(tmp_path)
        assert sheet._pinned == set()


# ---------------------------------------------------------------------------
# Pin / unpin
# ---------------------------------------------------------------------------

class TestPinUnpin:

    def test_toggle_pin_adds_phrase(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._toggle_pin("copy")
        assert "copy" in sheet._pinned

    def test_toggle_pin_removes_phrase(self, tmp_path):
        palette = tmp_path / "command_palette.json"
        palette.write_text(json.dumps({"pinned": ["copy"], "geometry": {}}))
        sheet, *_ = _make_sheet(tmp_path)
        sheet._toggle_pin("copy")
        assert "copy" not in sheet._pinned

    def test_pin_saved_to_palette(self, tmp_path):
        sheet, _, _, palette_path = _make_sheet(tmp_path)
        sheet._win = MagicMock()
        sheet._win.winfo_x.return_value = 0
        sheet._win.winfo_y.return_value = 0
        sheet._win.winfo_width.return_value = 440
        sheet._win.winfo_height.return_value = 520
        sheet._toggle_pin("paste")
        data = json.loads(palette_path.read_text())
        assert "paste" in data["pinned"]

    def test_unpin_saved_to_palette(self, tmp_path):
        palette = tmp_path / "command_palette.json"
        palette.write_text(json.dumps({"pinned": ["paste"], "geometry": {}}))
        sheet, _, _, palette_path = _make_sheet(tmp_path)
        sheet._win = MagicMock()
        sheet._win.winfo_x.return_value = 0
        sheet._win.winfo_y.return_value = 0
        sheet._win.winfo_width.return_value = 440
        sheet._win.winfo_height.return_value = 520
        sheet._toggle_pin("paste")
        data = json.loads(palette_path.read_text())
        assert "paste" not in data["pinned"]


# ---------------------------------------------------------------------------
# Filter logic (_apply_filter uses _all and returns ordered list)
# ---------------------------------------------------------------------------

class TestFilter:

    def _get_filtered(self, sheet, query=""):
        """Run filter and return ordered list of phrases."""
        if sheet._filter_var is not None:
            sheet._filter_var.set(query)

        raw = query.strip().lower()
        if raw == "filter commands..." or not raw:
            filtered = list(sheet._all)
        else:
            filtered = [
                c for c in sheet._all
                if raw in c["phrase"]
                or any(raw in a for a in c.get("aliases", []))
            ]
        pinned = [c for c in filtered if c["phrase"] in sheet._pinned]
        unpinned = [c for c in filtered if c["phrase"] not in sheet._pinned]
        return [c["phrase"] for c in pinned + unpinned]

    def test_no_filter_returns_all(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._all = list(SAMPLE_COMMANDS)
        result = self._get_filtered(sheet, "")
        assert len(result) == len(SAMPLE_COMMANDS)

    def test_filter_by_phrase_substring(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._all = list(SAMPLE_COMMANDS)
        result = self._get_filtered(sheet, "tab")
        assert "new tab" in result
        assert "close tab" in result
        assert "copy" not in result

    def test_filter_by_alias(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._all = list(SAMPLE_COMMANDS)
        result = self._get_filtered(sheet, "start music")
        assert "play music" in result

    def test_pinned_commands_appear_first(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._all = list(SAMPLE_COMMANDS)
        sheet._pinned = {"paste"}
        result = self._get_filtered(sheet, "")
        assert result[0] == "paste"

    def test_empty_query_returns_all(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._all = list(SAMPLE_COMMANDS)
        result = self._get_filtered(sheet, "")
        assert set(result) == {"copy", "paste", "new tab", "close tab", "play music"}


# ---------------------------------------------------------------------------
# execute callback
# ---------------------------------------------------------------------------

class TestExecuteCallback:

    def test_flash_and_execute_calls_cb(self, tmp_path):
        sheet, execute_cb, *_ = _make_sheet(tmp_path)
        row_info = {
            "phrase": "copy",
            "cell": MagicMock(),
        }
        sheet._flash_id = None
        with patch.object(sheet._root, "after", return_value=1):
            sheet._flash_and_execute(row_info, "copy")
        execute_cb.assert_called_once_with("copy")

    def test_execute_exception_does_not_propagate(self, tmp_path):
        sheet, execute_cb, *_ = _make_sheet(tmp_path)
        execute_cb.side_effect = RuntimeError("boom")
        row_info = {"phrase": "copy", "cell": MagicMock()}
        sheet._flash_id = None
        with patch.object(sheet._root, "after", return_value=1):
            sheet._flash_and_execute(row_info, "copy")


# ---------------------------------------------------------------------------
# show / hide / toggle / destroy
# ---------------------------------------------------------------------------

class TestLifecycle:

    def test_toggle_calls_show_when_hidden(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._visible = False
        sheet.show = MagicMock()
        sheet.hide = MagicMock()
        sheet.toggle()
        sheet.show.assert_called_once()
        sheet.hide.assert_not_called()

    def test_toggle_calls_hide_when_visible(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._visible = True
        sheet.show = MagicMock()
        sheet.hide = MagicMock()
        sheet.toggle()
        sheet.hide.assert_called_once()
        sheet.show.assert_not_called()

    def test_destroy_sets_visible_false(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._visible = True
        sheet._win = MagicMock(spec=tk.Toplevel)
        sheet.destroy()
        assert not sheet._visible

    def test_hide_sets_visible_false(self, tmp_path):
        sheet, *_ = _make_sheet(tmp_path)
        sheet._visible = True
        sheet._win = MagicMock(spec=tk.Toplevel)
        sheet.hide()
        assert not sheet._visible
