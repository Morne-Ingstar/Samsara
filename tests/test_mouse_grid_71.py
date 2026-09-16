"""Queue 71 -- the mouse grid: geometry, chaining, scoping, actions.

Never imports dictation and never shows a window: the Qt post is captured, the
pointer and the mouse buttons are faked, and the geometry is pure.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import plugins.commands.show_numbers as sn  # noqa: E402
from samsara import command_scope  # noqa: E402
from samsara.ui import mouse_grid as mg  # noqa: E402
from samsara.ui import numbers_overlay_qt as nov  # noqa: E402

# Mixed-DPI, multi-monitor desktop, PHYSICAL pixels (what the pointer uses):
#   1: 2560x1440 at 100%      2: 3840x2160 at 150% to its right      3: 1920x1080 at 100% above
MON1 = (0, 0, 2560, 1440)
MON2 = (2560, 0, 6400, 2160)
MON3 = (0, -1080, 1920, 0)
PHYSICAL = [MON2, MON3, MON1]          # deliberately unsorted
MAPPINGS = [(MON1, (0, 0, 2560, 1440), 1.0),
            (MON2, (2560, 0, 5120, 1440), 1.5),
            (MON3, (0, -1080, 1920, 0), 1.0)]


@pytest.fixture(autouse=True)
def _clean_grid():
    sn._grid_state = None
    yield
    sn._grid_state = None


@pytest.fixture
def app():
    return SimpleNamespace(config={})


@pytest.fixture
def driven(monkeypatch):
    """Grid wired to fakes: records pointer moves, clicks and Qt posts."""
    log = SimpleNamespace(moves=[], clicks=[], posts=0)
    monkeypatch.setattr(sn, "_move_pointer", lambda x, y: log.moves.append((x, y)))
    monkeypatch.setattr(sn, "_grid_click", lambda action: log.clicks.append(action))
    monkeypatch.setattr(sn.qt_runtime, "ensure_started", lambda *a, **k: None)
    monkeypatch.setattr(sn.qt_runtime, "post", lambda fn: log.__setattr__("posts", log.posts + 1))
    monkeypatch.setattr(sn, "_report_chip", lambda *a, **k: None)
    monkeypatch.setattr(sn, "_monitor_rects_physical", lambda: mg.order_monitors(PHYSICAL))
    monkeypatch.setattr(sn, "_window_rect_physical", lambda hwnd: (300, 200, 1300, 900))
    return log


# ---------------------------------------------------------------------------
# geometry: two and three levels, on the mixed-DPI layout
# ---------------------------------------------------------------------------

class TestGeometry:
    def test_nine_cells_tile_the_area_exactly(self):
        cells = mg.cells(MON1)
        assert len(cells) == 9
        assert cells[0][1] == (0, 0, 853, 480)          # 1: top-left
        assert cells[4][1] == (853, 480, 1707, 960)     # 5: centre
        assert cells[8][1] == (1707, 960, 2560, 1440)   # 9: bottom-right
        # no seams and no overlap: each column/row edge is shared
        assert cells[0][1][2] == cells[1][1][0]
        assert cells[2][1][2] == MON1[2]
        assert cells[6][1][3] == MON1[3]

    def test_two_levels_land_on_the_expected_point(self):
        level2 = mg.refine(MON1, [5, 1])
        assert level2 == (853, 480, 1138, 640)
        assert mg.center(level2) == (995, 560)

    def test_three_levels_on_the_150_percent_monitor(self):
        # Cells are computed in physical pixels, so the 150% monitor's own
        # origin (2560) and size (3840x2160) drive the arithmetic.
        # 5 -> (3840,720,5120,1440); 3 -> (4693,720,5120,960); 8 -> the
        # bottom-centre ninth of that.
        level3 = mg.refine(MON2, [5, 3, 8])
        assert mg.cell_rect(MON2, 5) == (3840, 720, 5120, 1440)
        assert mg.cell_rect(mg.cell_rect(MON2, 5), 3) == (4693, 720, 5120, 960)
        assert level3 == (4835, 880, 4978, 960)
        assert mg.center(level3) == (4906, 920)
        assert level3[0] >= MON2[0] and level3[2] <= MON2[2]

    def test_three_levels_are_under_a_toolbar_icon(self):
        left, top, right, bottom = mg.refine(MON1, [1, 1, 1])
        assert (right - left, bottom - top) == (95, 53)

    def test_refinement_stops_at_the_minimum_cell(self):
        tiny = mg.refine(MON1, [1] * 12)
        assert not mg.too_small(mg.cell_rect(MON1, 1))
        assert mg.too_small(tiny) or (tiny[2] - tiny[0]) > mg.MIN_CELL_PX

    def test_monitors_are_numbered_left_to_right(self):
        assert mg.order_monitors(PHYSICAL) == [(1, MON3), (2, MON1), (3, MON2)]

    def test_pills_map_through_the_dpi_mapping(self):
        # A cell centre on the 150% monitor maps to Qt logical DIPs via the
        # same conversion queue 56 built; the pill must land on that screen.
        cx, cy = mg.center(mg.cell_rect(MON2, 5))
        lx, ly = nov._map_physical_to_qt(cx, cy, MAPPINGS)
        assert (lx, ly) == (2560 + int((cx - 2560) / 1.5), int(cy / 1.5))
        assert 2560 <= lx <= 5120


# ---------------------------------------------------------------------------
# chaining
# ---------------------------------------------------------------------------

class TestChaining:
    def test_parse_reads_a_whole_chain(self):
        assert mg.parse("grid five three eight click") == {
            "scope": None, "monitor": None, "digits": [5, 3, 8], "action": "click", "back": False}
        assert mg.parse("grid 5 3 8 move")["action"] == "move"
        assert mg.parse("grid window 7")["scope"] == mg.SCOPE_WINDOW
        assert mg.parse("grid here")["scope"] == mg.SCOPE_WINDOW
        assert mg.parse("grid monitor two") == {
            "scope": mg.SCOPE_MONITOR, "monitor": 2, "digits": [], "action": None, "back": False}
        assert mg.parse("grid monitor 2 five")["digits"] == [5]
        assert mg.parse("grid, uh, five")["digits"] == [5]
        assert mg.parse("right click")["action"] == "right"

    def test_chained_equals_step_by_step_geometry(self):
        assert mg.refine(MON1, [5, 3, 8]) == mg.cell_rect(mg.cell_rect(mg.cell_rect(MON1, 5), 3), 8)

    def test_one_utterance_matches_three_separate_ones(self, app, driven):
        sn.handle_mouse_grid(app, "five three eight")
        chained = sn.grid_state()

        sn._grid_state = None
        sn.handle_mouse_grid(app, "")
        for digit in ("five", "three", "eight"):
            sn.handle_click(app, digit)       # a bare number arrives as "click N"
        stepwise = sn.grid_state()

        assert chained["rect"] == stepwise["rect"]
        assert chained["path"] == stepwise["path"] == [5, 3, 8]

    def test_chained_utterance_with_an_action_clicks_once(self, app, driven):
        sn.handle_mouse_grid(app, "five three eight click")
        assert driven.clicks == ["click"]
        assert sn.grid_active() is False          # the grid closes after acting
        assert driven.moves[-1] == mg.center(mg.refine(MON1, [5, 3, 8]))


# ---------------------------------------------------------------------------
# scope: the numbers are live only while the grid is on screen
# ---------------------------------------------------------------------------

class TestScoping:
    def test_tag_is_published_and_follows_visibility(self):
        assert "mouse_grid.visible" not in command_scope.active_tags()
        sn._grid_state = {"rect": MON1, "area": MON1, "path": [], "label": "m", "hwnd": 0}
        assert "mouse_grid.visible" in command_scope.active_tags()
        sn._grid_state = None
        assert "mouse_grid.visible" not in command_scope.active_tags()

    @pytest.mark.parametrize("phrase", ["hide grid", "grid back", "move here"])
    def test_grid_phrases_are_not_candidates_while_hidden(self, phrase):
        # conftest isolates _REGISTRY per test, so read what this module
        # actually declared at import instead.
        from samsara.plugin_commands import _MODULE_ENTRIES

        entry = _MODULE_ENTRIES["plugins.commands.show_numbers"][phrase]
        scope = entry["scope"]
        assert scope is not None and "mouse_grid.visible" in scope.tags
        ctx_hidden = command_scope.MatchContext.for_app("notepad.exe", tags=frozenset())
        ctx_shown = command_scope.MatchContext.for_app("notepad.exe", tags={"mouse_grid.visible"})
        assert command_scope.scope_live(scope, ctx_hidden)[0] is False
        assert command_scope.scope_live(scope, ctx_shown)[0] is True

    def test_a_number_does_nothing_to_the_grid_while_it_is_hidden(self, app, driven):
        assert sn.grid_active() is False
        assert sn._grid_from_click(app, "5") is None     # falls through to the label path
        assert driven.moves == [] and driven.clicks == []

    def test_the_window_cube_keeps_its_own_bare_numbers(self):
        from samsara.plugin_commands import _MODULE_ENTRIES

        # 71 must not re-register one..nine: the registry is one entry per
        # phrase, so that would displace the cube's tag-scoped numbers.
        declared = set(_MODULE_ENTRIES.get("plugins.commands.show_numbers", {}))
        for entry in _MODULE_ENTRIES.get("plugins.commands.show_numbers", {}).values():
            declared.update(entry["aliases"])
        assert not declared & {"one", "two", "three", "four", "five",
                               "six", "seven", "eight", "nine"}

    def test_click_by_text_still_works_while_the_grid_is_up(self, app, driven):
        sn.handle_mouse_grid(app, "")
        assert sn._grid_from_click(app, "save") is None   # not digits, not an action


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

class TestActions:
    @pytest.mark.parametrize("utterance, expected", [
        ("five click", "click"),
        ("five right click", "right"),
        ("five double click", "double"),
    ])
    def test_each_action_fires_at_the_cell_centre(self, app, driven, utterance, expected):
        sn.handle_mouse_grid(app, utterance)
        assert driven.clicks == [expected]
        assert driven.moves[-1] == mg.center(mg.cell_rect(MON1, 5))

    def test_move_moves_and_does_not_click(self, app, driven):
        sn.handle_mouse_grid(app, "five three move")
        assert driven.clicks == []
        assert driven.moves[-1] == mg.center(mg.refine(MON1, [5, 3]))
        assert sn.grid_active() is False

    def test_move_here_parks_without_clicking(self, app, driven):
        sn.handle_mouse_grid(app, "five")
        sn.handle_move_here(app, "")
        assert driven.clicks == []
        assert driven.moves[-1] == mg.center(mg.cell_rect(MON1, 5))
        assert sn.grid_active() is False

    def test_every_refinement_moves_the_pointer(self, app, driven):
        sn.handle_mouse_grid(app, "")
        assert driven.moves == [mg.center(MON1)]
        sn.handle_click(app, "5")
        assert driven.moves[-1] == mg.center(mg.cell_rect(MON1, 5))

    def test_back_undoes_one_level(self, app, driven):
        sn.handle_mouse_grid(app, "five three")
        assert sn.grid_state()["path"] == [5, 3]
        sn.handle_grid_back(app, "")
        state = sn.grid_state()
        assert state["path"] == [5]
        assert state["rect"] == mg.cell_rect(MON1, 5)
        assert driven.moves[-1] == mg.center(mg.cell_rect(MON1, 5))

    def test_hide_grid_closes_it(self, app, driven):
        sn.handle_mouse_grid(app, "")
        assert sn.grid_active() is True
        sn.handle_hide_grid(app, "")
        assert sn.grid_active() is False

    def test_bare_click_acts_at_the_current_cell(self, app, driven):
        sn.handle_mouse_grid(app, "five")
        sn.handle_click(app, "")
        assert driven.clicks == ["click"]
        assert driven.moves[-1] == mg.center(mg.cell_rect(MON1, 5))


# ---------------------------------------------------------------------------
# scopes: screen, window, named monitor
# ---------------------------------------------------------------------------

class TestScopes:
    def test_default_is_the_monitor_holding_the_focused_window(self, app, driven):
        sn.handle_mouse_grid(app, "")
        assert sn.grid_state()["area"] == MON1

    def test_window_scope_uses_the_window_rect(self, app, driven):
        sn.handle_mouse_grid(app, "window")
        assert sn.grid_state()["area"] == (300, 200, 1300, 900)

    def test_named_monitor(self, app, driven):
        sn.handle_mouse_grid(app, "monitor three")
        assert sn.grid_state()["area"] == MON2
        assert sn.grid_state()["label"] == "monitor 3"

    def test_unknown_monitor_falls_back_to_the_active_one(self, app, driven):
        sn.handle_mouse_grid(app, "monitor nine")
        assert sn.grid_state()["area"] == MON1
