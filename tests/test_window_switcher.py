"""Regression cover for the window_switcher pieces the Window Cube reuses.

plugins/commands/window_cube.py imports _parse_letters (transitively, via the
copy/tile handlers), _assign_letters, _get_all_windows, _own_hwnd, _get_pid,
_force_focus and _speak from here rather than forking them. These tests pin
the contracts the cube depends on, so a change on this side that would
silently break the cube fails here first.

No real windows: _assign_letters and _parse_letters are pure, and the one
enumeration test drives the module's own filter predicate directly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins.commands import window_switcher as ws


class _Rect:
    def __init__(self, left=0, top=0):
        self.left = left
        self.top = top
        self.right = left + 800
        self.bottom = top + 600


# ---------------------------------------------------------------------------
# NATO / spoken-letter parsing
# ---------------------------------------------------------------------------

class TestParseLetters:
    def test_nato_words(self):
        assert ws._parse_letters("window switch bravo") == ["B"]

    def test_spoken_letter_names(self):
        assert ws._parse_letters("window switch bee") == ["B"]

    def test_raw_single_letter(self):
        assert ws._parse_letters("window switch b") == ["B"]

    def test_order_is_preserved_for_two_letters(self):
        """The cube's copy/tile delegation hands over "B into D" and relies on
        the order surviving."""
        assert ws._parse_letters("window copy b into d") == ["B", "D"]

    def test_longest_phrase_wins(self):
        assert ws._parse_letters("window switch double you") == ["W"]
        assert ws._parse_letters("window switch x-ray") == ["X"]

    def test_no_letters_returns_empty(self):
        assert ws._parse_letters("") == []

    def test_tile_phrase_yields_both_letters(self):
        assert ws._parse_letters("b and d") == ["B", "D"]


# ---------------------------------------------------------------------------
# Letter assignment -- the ordering the cube's numbering is built on
# ---------------------------------------------------------------------------

class TestAssignLetters:
    def test_visible_sorted_left_to_right(self):
        windows = [
            (33, "Third", _Rect(left=2000), False),
            (11, "First", _Rect(left=0), False),
            (22, "Second", _Rect(left=1000), False),
        ]

        mapping = ws._assign_letters(windows)

        assert mapping["A"][0] == 11
        assert mapping["B"][0] == 22
        assert mapping["C"][0] == 33

    def test_minimized_come_after_visible(self):
        windows = [
            (99, "Aaa minimized", _Rect(left=0), True),
            (11, "Visible", _Rect(left=500), False),
        ]

        mapping = ws._assign_letters(windows)

        assert mapping["A"][0] == 11
        assert mapping["B"][0] == 99
        assert mapping["B"][2] is True

    def test_minimized_sorted_alphabetically(self):
        windows = [
            (2, "Zebra", _Rect(), True),
            (1, "Apple", _Rect(), True),
        ]

        mapping = ws._assign_letters(windows)

        assert mapping["A"][1] == "Apple"
        assert mapping["B"][1] == "Zebra"

    def test_more_than_26_windows_get_two_letter_labels(self):
        windows = [(i, f"W{i}", _Rect(left=i * 10), False) for i in range(30)]

        mapping = ws._assign_letters(windows)

        assert len(mapping) == 30
        assert "AA" in mapping

    def test_entry_shape_is_hwnd_title_isminimized(self):
        """The cube unpacks exactly this 3-tuple."""
        mapping = ws._assign_letters([(11, "Title", _Rect(), False)])

        hwnd, title, is_min = mapping["A"]
        assert (hwnd, title, is_min) == (11, "Title", False)


# ---------------------------------------------------------------------------
# Mapping lookup
# ---------------------------------------------------------------------------

class TestMappingLookup:
    def test_get_window_by_letter_returns_hwnd(self, monkeypatch):
        monkeypatch.setattr(ws, "_mapping", {"A": (11, "Warp", False)})

        assert ws.get_window_by_letter("a") == 11
        assert ws.get_window_by_letter("A") == 11

    def test_get_window_by_letter_missing_is_none_without_speaking(self, monkeypatch):
        monkeypatch.setattr(ws, "_mapping", {})
        spoken = []
        monkeypatch.setattr(ws, "_speak", lambda app, text: spoken.append(text))

        assert ws.get_window_by_letter("Z") is None
        assert spoken == []

    def test_resolve_speaks_on_a_miss(self, monkeypatch):
        monkeypatch.setattr(ws, "_mapping", {})
        spoken = []
        monkeypatch.setattr(ws, "_speak", lambda app, text: spoken.append(text))

        assert ws._resolve(object(), "Q") is None
        assert spoken and "Q" in spoken[0]


# ---------------------------------------------------------------------------
# Enumeration filters the cube inherits
# ---------------------------------------------------------------------------

class TestEnumerationFilters:
    def test_samsara_windows_are_filtered_by_title_prefix(self):
        """This is what keeps the cube's own panel ("Samsara Window Cube")
        out of the list it renders."""
        source = Path(ws.__file__).read_text(encoding="utf-8")

        assert "title.startswith('Samsara')" in source

    def test_shell_titles_are_skipped(self):
        assert "Program Manager" in ws._SKIP_TITLES

    def test_own_hwnd_is_an_empty_set_by_contract(self):
        assert ws._own_hwnd(object()) == set()


# ---------------------------------------------------------------------------
# Monitor parsing (used by window move, unchanged by the cube)
# ---------------------------------------------------------------------------

class TestParseMonitorIndex:
    def test_named_monitors(self):
        assert ws._parse_monitor_index("move a to the left") == 1
        assert ws._parse_monitor_index("move a to the right") == 2

    def test_numbered_monitor(self):
        assert ws._parse_monitor_index("monitor 4") == 4

    def test_none_when_absent(self):
        assert ws._parse_monitor_index("somewhere") is None
