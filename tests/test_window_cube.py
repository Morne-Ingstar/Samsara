"""Window Cube: frozen numbering, scoped number commands, delegation.

No real windows and no Qt -- enumeration is mocked at the ONE seam the cube
reuses (window_switcher._get_all_windows), and the panel is replaced with a
recorder, so nothing here opens a window or needs a QApplication.

No module-level `import dictation` -- none of this needs the app module; the
session-mode stand-ins below are duck-typed exactly as the cube reads them.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins.commands import window_cube
from samsara.ui import window_cube_qt


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------

class _Rect:
    """Stand-in for the ctypes RECT window_switcher._assign_letters sorts on."""

    def __init__(self, left=0, top=0):
        self.left = left
        self.top = top
        self.right = left + 800
        self.bottom = top + 600


class _FakeApp:
    def __init__(self, *, mode=None, command_mode_active=False, cm_mode="hold"):
        self.config = {
            "window_cube": {"max_rows": 9, "opacity": 0.6},
            "command_mode": {"mode": cm_mode},
        }
        self.command_mode_active = command_mode_active
        self.sounds = []
        self.spoken = []
        self.saved = []
        if mode is not None:
            self._session_mode_manager = types.SimpleNamespace(mode=mode)

    def play_sound(self, name, **_kw):
        self.sounds.append(name)

    def update_config_and_save(self, updates):
        self.saved.append(updates)
        self.config.update(updates)


def _mode(name):
    """A SessionMode-shaped object: the cube reads only `.name`."""
    return types.SimpleNamespace(name=name)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset cube state and stub the Win32/Qt seams for every test."""
    window_cube._reset_for_tests()
    monkeypatch.setattr(window_cube, "_friendly_app_name",
                        lambda hwnd: _FAKE_NAMES.get(hwnd, "App"))
    monkeypatch.setattr(window_cube, "_speak",
                        lambda app, text: app.spoken.append(text))
    focused = []
    monkeypatch.setattr(window_cube, "_force_focus",
                        lambda hwnd: focused.append(hwnd) or True)
    rendered = []
    monkeypatch.setattr(window_cube, "_render",
                        lambda app, slots=None: rendered.append(slots))
    yield types.SimpleNamespace(focused=focused, rendered=rendered)
    window_cube._reset_for_tests()


_FAKE_NAMES = {}


def _set_windows(monkeypatch, windows):
    """windows == [(hwnd, title, app_name)] -- mocks the single enumeration
    seam the cube reuses from window_switcher."""
    _FAKE_NAMES.clear()
    rows = []
    for i, (hwnd, title, app_name) in enumerate(windows):
        _FAKE_NAMES[hwnd] = app_name
        rows.append((hwnd, title, _Rect(left=i * 100), False))
    monkeypatch.setattr(window_cube, "_get_all_windows", lambda exclude=None: rows)
    monkeypatch.setattr(window_cube, "_own_hwnd", lambda app: set())


# ---------------------------------------------------------------------------
# Frozen numbering
# ---------------------------------------------------------------------------

class TestFrozenNumbering:
    def test_initial_pin_numbers_from_one(self, monkeypatch):
        _set_windows(monkeypatch, [
            (11, "Warp", "Warp"), (22, "Obsidian", "Obsidian"),
            (33, "Claude", "Claude"),
        ])
        app = _FakeApp()

        window_cube.handle_show_cube(app, "")

        slots = window_cube._state_for_tests()["slots"]
        assert [(s["number"], s["app"]) for s in slots] == [
            (1, "Warp"), (2, "Obsidian"), (3, "Claude"),
        ]

    def test_new_window_appends_and_never_shifts_existing_numbers(self, monkeypatch):
        _set_windows(monkeypatch, [
            (11, "Warp", "Warp"), (22, "Obsidian", "Obsidian"),
        ])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")

        # A new window opens LEFT of the others -- enumeration order puts it
        # first, which is exactly the case that would renumber naively.
        _set_windows(monkeypatch, [
            (99, "Chrome", "Chrome"), (11, "Warp", "Warp"),
            (22, "Obsidian", "Obsidian"),
        ])
        window_cube.handle_refresh_cube(app, "")

        slots = {s["hwnd"]: s["number"] for s in window_cube._state_for_tests()["slots"]}
        assert slots[11] == 1 and slots[22] == 2, "existing numbers shifted"
        assert slots[99] == 3, "new window did not append with the next number"

    def test_closed_window_keeps_its_slot_greyed(self, monkeypatch):
        _set_windows(monkeypatch, [
            (11, "Warp", "Warp"), (22, "Obsidian", "Obsidian"),
            (33, "Claude", "Claude"),
        ])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")

        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (33, "Claude", "Claude")])
        window_cube.handle_refresh_cube(app, "")

        slots = {s["number"]: s for s in window_cube._state_for_tests()["slots"]}
        assert slots[2]["closed"] is True, "closed window lost its greyed slot"
        assert slots[3]["closed"] is False
        assert slots[3]["hwnd"] == 33, "number 3 moved to a different window"

    def test_switching_to_a_closed_slot_is_refused(self, monkeypatch, ):
        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "Obsidian", "Obsidian")])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")
        _set_windows(monkeypatch, [(11, "Warp", "Warp")])
        window_cube.handle_refresh_cube(app, "")

        window_cube._switch_to_number(app, 2)

        assert app.spoken and "No window 2" in app.spoken[-1]

    def test_repinning_renumbers_from_scratch(self, monkeypatch):
        """The ONLY renumber: unpin + pin again, a deliberate user action."""
        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "Obsidian", "Obsidian")])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")
        window_cube.handle_hide_cube(app, "")

        _set_windows(monkeypatch, [(22, "Obsidian", "Obsidian")])
        window_cube.handle_show_cube(app, "")

        slots = window_cube._state_for_tests()["slots"]
        assert [(s["number"], s["hwnd"]) for s in slots] == [(1, 22)]


# ---------------------------------------------------------------------------
# Scoping of the bare number commands
# ---------------------------------------------------------------------------

class TestBareNumberScoping:
    @staticmethod
    def _handler():
        return window_cube._make_bare_number_handler("three", 3)

    def test_inactive_when_unpinned(self, monkeypatch):
        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "O", "O"), (33, "C", "C")])
        app = _FakeApp(command_mode_active=True)

        assert self._handler()(app, "") is False
        assert app.sounds == []

    def test_inactive_in_dictate_mode(self, monkeypatch, _isolate):
        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "O", "O"), (33, "C", "C")])
        app = _FakeApp(mode=_mode("DICTATE"), command_mode_active=True)
        window_cube.handle_show_cube(app, "")

        assert self._handler()(app, "") is False
        assert _isolate.focused == [], "a dictated number switched a window"

    def test_inactive_in_ava_mode(self, monkeypatch):
        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "O", "O"), (33, "C", "C")])
        app = _FakeApp(mode=_mode("AVA"), command_mode_active=True)
        window_cube.handle_show_cube(app, "")

        assert self._handler()(app, "") is False

    def test_active_in_command_mode(self, monkeypatch, _isolate):
        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "O", "O"), (33, "C", "C")])
        app = _FakeApp(mode=_mode("COMMAND"), command_mode_active=True)
        window_cube.handle_show_cube(app, "")

        assert self._handler()(app, "") is True
        assert _isolate.focused == [33]
        assert "success" in app.sounds

    def test_active_in_hold_to_command_without_a_session_manager(self, monkeypatch, _isolate):
        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "O", "O"), (33, "C", "C")])
        app = _FakeApp(cm_mode="hold")     # no _session_mode_manager at all
        window_cube.handle_show_cube(app, "")

        assert self._handler()(app, "") is True
        assert _isolate.focused == [33]


class TestWindowNumberCommands:
    def test_window_three_works_in_dictate(self, monkeypatch, _isolate):
        """The unambiguous form is safe everywhere, so it stays active in
        DICTATE where the bare number must not be."""
        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "O", "O"), (33, "C", "C")])
        app = _FakeApp(mode=_mode("DICTATE"))
        window_cube.handle_show_cube(app, "")

        handler = window_cube._make_window_number_handler(3)

        assert handler(app, "") is True
        assert _isolate.focused == [33]

    def test_window_three_inactive_when_unpinned(self, monkeypatch):
        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "O", "O"), (33, "C", "C")])
        app = _FakeApp(mode=_mode("DICTATE"))

        assert window_cube._make_window_number_handler(3)(app, "") is False


# ---------------------------------------------------------------------------
# Delegation to the existing copy/tile handlers
# ---------------------------------------------------------------------------

class TestDelegation:
    def test_cube_copy_translates_numbers_to_letters(self, monkeypatch):
        _set_windows(monkeypatch, [
            (11, "Warp", "Warp"), (22, "Obsidian", "Obsidian"),
            (33, "Claude", "Claude"), (44, "Chrome", "Chrome"),
        ])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")
        monkeypatch.setattr(window_cube, "_letters_for_numbers",
                            lambda a, numbers: ["B", "D"])
        seen = []
        monkeypatch.setattr(window_cube, "handle_window_copy",
                            lambda a, remainder: seen.append(remainder) or True)

        window_cube.handle_cube_copy(app, "2 into 4")

        assert seen == ["B into D"]

    def test_cube_tile_translates_numbers_to_letters(self, monkeypatch):
        _set_windows(monkeypatch, [
            (11, "Warp", "Warp"), (22, "Obsidian", "Obsidian"),
            (33, "Claude", "Claude"), (44, "Chrome", "Chrome"),
        ])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")
        monkeypatch.setattr(window_cube, "_letters_for_numbers",
                            lambda a, numbers: ["B", "D"])
        seen = []
        monkeypatch.setattr(window_cube, "handle_window_tile",
                            lambda a, remainder: seen.append(remainder) or True)

        window_cube.handle_cube_tile(app, "2 and 4")

        assert seen == ["B and D"]

    def test_letters_resolve_against_the_switchers_own_mapping(self, monkeypatch):
        """The translation must hand back the letters window_switcher itself
        will resolve -- looked up by hwnd in its live mapping, not guessed."""
        from plugins.commands import window_switcher as ws

        _set_windows(monkeypatch, [(11, "Warp", "Warp"), (22, "Obsidian", "Obsidian")])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")
        monkeypatch.setattr(ws, "_mapping", {
            "A": (11, "Warp", False), "B": (22, "Obsidian", False),
        })

        assert window_cube._letters_for_numbers(app, [2, 1]) == ["B", "A"]

    def test_copy_refuses_when_a_number_is_missing(self, monkeypatch):
        _set_windows(monkeypatch, [(11, "Warp", "Warp")])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")

        window_cube.handle_cube_copy(app, "2")

        assert app.spoken and "two numbers" in app.spoken[-1].lower()


# ---------------------------------------------------------------------------
# Rows / paging
# ---------------------------------------------------------------------------

class TestRows:
    def test_second_line_only_when_app_names_collide(self, monkeypatch):
        _set_windows(monkeypatch, [
            (11, "Inbox - Chrome", "Chrome"),
            (22, "Docs - Chrome", "Chrome"),
            (33, "Notes", "Obsidian"),
        ])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")
        slots = window_cube._state_for_tests()["slots"]

        rows = window_cube._to_cube_rows(app, slots)

        by_number = {r.number: r for r in rows}
        assert by_number[1].detail and by_number[2].detail, "collision not disambiguated"
        assert by_number[3].detail == "", "unique app name should stay one line"

    def test_page_two_shows_the_next_block(self, monkeypatch):
        _set_windows(monkeypatch, [(i, f"W{i}", f"App{i}") for i in range(1, 10)])
        app = _FakeApp()
        app.config["window_cube"]["max_rows"] = 8
        window_cube.handle_show_cube(app, "")

        page_one = [s["number"] for s in window_cube._page_slots(app)]
        window_cube.handle_cube_page(app, "two")
        page_two = [s["number"] for s in window_cube._page_slots(app)]

        assert page_one == list(range(1, 9))
        assert page_two == [9]

    def test_page_three_refuses_and_keeps_the_current_page(self, monkeypatch):
        _set_windows(monkeypatch, [(i, f"W{i}", f"App{i}") for i in range(1, 10)])
        app = _FakeApp()
        app.config["window_cube"]["max_rows"] = 8
        window_cube.handle_show_cube(app, "")
        window_cube.handle_cube_page(app, "two")

        assert window_cube.handle_cube_page(app, "three") is True
        assert window_cube._state_for_tests()["page"] == 2
        assert "only 2 cube pages" in app.spoken[-1]

    def test_page_question_does_not_offer_a_nonexistent_page(self, monkeypatch):
        _set_windows(monkeypatch, [(1, "W1", "App1")])
        app = _FakeApp()
        window_cube.handle_show_cube(app, "")

        window_cube.handle_cube_page(app, "")

        assert app.spoken[-1] == "There is only one cube page."


# ---------------------------------------------------------------------------
# The panel must never appear in its own list
# ---------------------------------------------------------------------------

class TestPanelExcludesItself:
    def test_toolwindow_and_noactivate_bits_are_set(self):
        result = window_cube_qt.compute_exstyle(0)

        assert result & window_cube_qt.WS_EX_TOOLWINDOW, "TOOLWINDOW not set"
        assert result & window_cube_qt.WS_EX_NOACTIVATE, "NOACTIVATE not set"

    def test_appwindow_bit_is_cleared(self):
        """APPWINDOW would force the panel back into the window list and undo
        TOOLWINDOW, so an existing one must be stripped."""
        result = window_cube_qt.compute_exstyle(window_cube_qt.WS_EX_APPWINDOW)

        assert not (result & window_cube_qt.WS_EX_APPWINDOW)
        assert result & window_cube_qt.WS_EX_TOOLWINDOW

    def test_existing_style_bits_are_preserved(self):
        assert window_cube_qt.compute_exstyle(0x00000008) & 0x00000008

    def test_title_prefix_is_the_second_guarantee(self):
        """window_switcher._get_all_windows drops titles starting with
        "Samsara"; the panel's title must keep that belt-and-braces filter
        working even if the ex-style call ever fails."""
        source = Path(window_cube_qt.__file__).read_text(encoding="utf-8")

        assert 'setWindowTitle("Samsara Window Cube")' in source

    def test_rows_are_tappable_height(self):
        assert window_cube_qt.ROW_HEIGHT >= 40

    def test_default_position_is_bottom_right_of_the_active_monitor(self):
        x, y = window_cube_qt.default_position((0, 0, 1920, 1080), (260, 300))

        assert x == 1920 - 260 - window_cube_qt.EDGE_MARGIN
        assert y == 1080 - 300 - window_cube_qt.EDGE_MARGIN

    def test_default_position_respects_a_secondary_monitor_origin(self):
        x, y = window_cube_qt.default_position((1920, 0, 3840, 1080), (260, 300))

        assert x == 3840 - 260 - window_cube_qt.EDGE_MARGIN
        assert x >= 1920


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_max_rows_default_and_override(self):
        app = _FakeApp()
        assert window_cube._max_rows(app) == 9
        app.config["window_cube"]["max_rows"] = 4
        assert window_cube._max_rows(app) == 4

    def test_bad_max_rows_falls_back(self):
        app = _FakeApp()
        app.config["window_cube"]["max_rows"] = "not a number"
        assert window_cube._max_rows(app) == window_cube.MAX_ROWS_DEFAULT

    def test_missing_config_section_is_safe(self):
        app = _FakeApp()
        app.config.pop("window_cube")
        assert window_cube._cfg(app) == {}
        assert window_cube._max_rows(app) == window_cube.MAX_ROWS_DEFAULT
