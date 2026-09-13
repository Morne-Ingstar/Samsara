"""plugins/commands/windows.py destination grammar + compound placement
(demo rehearsal step 3: "put warp on the left screen and claude on the
right"). Monitors and window enumeration are mocked -- no real desktop."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Same headless preamble as tests/test_window_manager.py.
for _mod in ('win32api', 'win32con', 'win32gui', 'win32process'):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
sys.modules['win32con'].SW_RESTORE = 9
sys.modules['win32con'].GWL_EXSTYLE = -20

import importlib  # noqa: E402
import plugins.commands.windows as wm  # noqa: E402
importlib.reload(wm)

from samsara.command_registry import DispatchState  # noqa: E402


def _mon(index, l, t, w, h, primary=False):
    return {'index': index, 'rect': (l, t, l + w, t + h), 'width': w, 'height': h,
            'primary': primary, 'device': f'\\\\.\\DISPLAY{index}', 'handle': index}


# Two side by side; three side by side; two stacked. Indexes follow x order
# (get_monitors sorts by x) but the tests never rely on that -- geometry does.
TWO = [_mon(1, 0, 0, 1920, 1080, primary=True), _mon(2, 1920, 0, 2560, 1440)]
THREE = [_mon(1, -1920, 0, 1920, 1080), _mon(2, 0, 0, 2560, 1440, primary=True), _mon(3, 2560, 0, 1920, 1080)]
STACKED = [_mon(1, 0, 0, 1920, 1080, primary=True), _mon(2, 0, 1080, 1920, 1080)]
ONE = [_mon(1, 0, 0, 1920, 1080, primary=True)]


class _App:
    def __init__(self, tv_device=None):
        self.config = {'window_manager': ({'tv_device': tv_device} if tv_device else {})}


# ---------------------------------------------------------------------------
# Destination grammar
# ---------------------------------------------------------------------------

class TestDestinationGrammar:
    @pytest.mark.parametrize("text,expected_index", [
        ("left screen", 1), ("the left monitor", 1), ("left display", 1), ("left", 1),
        ("right screen", 2), ("the right", 2), ("right monitor", 2),
        ("monitor 2", 2), ("screen 1", 1), ("2", 2),
        ("second screen", 2), ("the first screen", 1),
        ("main screen", 1), ("primary monitor", 1),
        ("tv", 2),
    ])
    def test_two_monitors(self, text, expected_index):
        target = wm._parse_destination(text, _App(), monitors=TWO)
        assert target and target['index'] == expected_index, (text, target)

    @pytest.mark.parametrize("text,expected_index", [
        ("left screen", 1), ("right screen", 3),
        ("middle screen", 2), ("the centre monitor", 2), ("center display", 2), ("middle", 2),
        ("third screen", 3), ("monitor 3", 3), ("main screen", 2),
    ])
    def test_three_monitors(self, text, expected_index):
        target = wm._parse_destination(text, _App(), monitors=THREE)
        assert target and target['index'] == expected_index, (text, target)

    def test_left_right_follow_geometry_not_index(self):
        # Indexes deliberately contradict physical position.
        weird = [_mon(1, 2560, 0, 1920, 1080), _mon(2, 0, 0, 2560, 1440, primary=True)]
        assert wm._parse_destination("left screen", _App(), monitors=weird)['index'] == 2
        assert wm._parse_destination("right screen", _App(), monitors=weird)['index'] == 1

    @pytest.mark.parametrize("text,expected_index", [("top screen", 1), ("bottom monitor", 2), ("upper", 1), ("lower screen", 2)])
    def test_stacked_top_bottom(self, text, expected_index):
        assert wm._parse_destination(text, _App(), monitors=STACKED)['index'] == expected_index

    def test_here_uses_cursor(self):
        with patch.object(wm, 'get_monitor_under_cursor', return_value=TWO[1]):
            assert wm._parse_destination("here", _App(), monitors=TWO)['index'] == 2

    def test_other_screen_is_the_one_the_cursor_is_not_on(self):
        with patch.object(wm, 'get_monitor_under_cursor', return_value=TWO[0]):
            assert wm._parse_destination("the other screen", _App(), monitors=TWO)['index'] == 2
        with patch.object(wm, 'get_monitor_under_cursor', return_value=TWO[1]):
            assert wm._parse_destination("other monitor", _App(), monitors=TWO)['index'] == 1

    def test_tv_prefers_configured_device(self):
        assert wm._parse_destination("tv", _App(tv_device='\\\\.\\DISPLAY1'), monitors=THREE)['index'] == 1

    @pytest.mark.parametrize("text,monitors,reason_part", [
        ("middle screen", TWO, "no middle"),
        ("top screen", TWO, "side by side"),
        ("left screen", STACKED, "stacked"),
        ("left screen", ONE, "only one"),
        ("monitor 4", TWO, "no monitor 4"),
        ("fourth screen", TWO, "no fourth"),
        ("the other screen", THREE, "ambiguous"),
        ("the purple screen", TWO, "unknown screen"),
        ("", TWO, "no destination"),
    ])
    def test_unresolvable_returns_explicit_marker(self, text, monitors, reason_part):
        target = wm._parse_destination(text, _App(), monitors=monitors)
        assert isinstance(target, wm.UnresolvedDestination)
        assert not target
        assert reason_part in target.reason, target


# ---------------------------------------------------------------------------
# Remainder / compound parsing (pure)
# ---------------------------------------------------------------------------

class TestPlacementParsing:
    @pytest.mark.parametrize("remainder,expected", [
        ("chrome to tv", ("chrome", "tv")),
        ("this to monitor 2", (None, "monitor 2")),
        ("warp on the left screen", ("warp", "left screen")),
        ("warp onto the right", ("warp", "right")),
        ("claude right", ("claude", "right")),
        ("warp left screen", ("warp", "left screen")),
        ("obsidian monitor 2", ("obsidian", "monitor 2")),
        ("left", (None, "left")),
    ])
    def test_single(self, remainder, expected):
        assert wm._parse_send_remainder(remainder) == expected

    def test_rehearsal_utterance_splits_into_two_placements(self):
        assert wm._parse_placements("warp on the left screen and claude on the right") == [
            ("warp", "left screen"), ("claude", "right"),
        ]

    @pytest.mark.parametrize("remainder", [
        "warp left, claude right",
        "warp left and claude right",
        "warp on the left, and claude on the right screen",
    ])
    def test_short_compound_forms(self, remainder):
        got = wm._parse_placements(remainder)
        assert [a for a, _d in got] == ["warp", "claude"]
        assert got[0][1].startswith("left") and got[1][1].startswith("right")

    def test_three_placements(self):
        assert wm._parse_placements("warp left, claude middle, obsidian right") == [
            ("warp", "left"), ("claude", "middle"), ("obsidian", "right"),
        ]

    def test_and_inside_an_app_name_does_not_split(self):
        assert wm._parse_placements("black and white to monitor 2") == [("black and white", "monitor 2")]

    def test_empty(self):
        assert wm._parse_placements("") == []


# ---------------------------------------------------------------------------
# handle_send end to end (monitors + windows mocked)
# ---------------------------------------------------------------------------

@pytest.fixture
def desktop(monkeypatch):
    """Two monitors; 'warp' has one window, 'claude' has two (topmost first),
    'obsidian' none; records every move."""
    moves = []
    monkeypatch.setattr(wm, 'get_monitors', lambda: TWO)
    monkeypatch.setattr(wm, 'move_window_to_monitor', lambda hwnd, mon: moves.append((hwnd, mon['index'])))
    windows = {'warp': [101], 'claude': [202, 201], 'obsidian': []}
    monkeypatch.setattr(wm, 'find_windows_by_app', lambda name, extra_ignore=None: list(windows.get(name, [])))
    monkeypatch.setattr(wm, 'get_monitor_under_cursor', lambda monitors=None: TWO[0])
    return moves


class TestHandleSend:
    def test_rehearsal_step_places_two_windows_in_order(self, desktop):
        result = wm.handle_send(_App(), "warp on the left screen and claude on the right")
        assert result.state is DispatchState.COMPLETED
        assert desktop == [(101, 1), (202, 2)]
        assert [p['app'] for p in result.detail['placements']] == ['warp', 'claude']

    def test_single_placement_still_works(self, desktop):
        result = wm.handle_send(_App(), "warp to monitor 2")
        assert result.state is DispatchState.COMPLETED and desktop == [(101, 2)]

    def test_two_same_app_windows_pick_most_recently_active_and_say_so(self, desktop, capsys):
        result = wm.handle_send(_App(), "claude on the right")
        assert desktop == [(202, 2)]
        assert "most recently active" in result.detail['placements'][0]['note']
        assert "2 claude windows" in capsys.readouterr().out

    def test_unknown_screen_is_refused_not_ignored(self, desktop, capsys):
        result = wm.handle_send(_App(), "warp to the purple screen")
        assert result.state is DispatchState.REJECTED
        assert result.detail['reason'].startswith("refused: unknown screen")
        assert desktop == []
        assert "refused: unknown screen" in capsys.readouterr().out

    def test_middle_screen_on_two_monitors_is_refused(self, desktop):
        result = wm.handle_send(_App(), "warp to the middle screen")
        assert result.state is DispatchState.REJECTED and desktop == []

    def test_partial_failure_is_reported_truthfully_and_not_undone(self, desktop, capsys):
        result = wm.handle_send(_App(), "warp on the left screen and obsidian on the right")
        assert result.state is DispatchState.FAILED
        assert result.detail['partial'] is True
        assert [p['app'] for p in result.detail['placements']] == ['warp']
        assert result.detail['failed']['app'] == 'obsidian'
        assert "no window found for 'obsidian'" in result.detail['reason']
        assert desktop == [(101, 1)], "the first placement stays; nothing is rolled back"
        assert "partial: placed warp" in capsys.readouterr().out

    def test_second_placement_unknown_screen_is_partial_too(self, desktop):
        result = wm.handle_send(_App(), "warp on the left screen and claude on the middle screen")
        assert result.state is DispatchState.FAILED and result.detail['partial']
        assert desktop == [(101, 1)]
        assert "unknown screen" in result.detail['failed']['reason']

    def test_first_placement_failing_stops_before_the_second(self, desktop):
        result = wm.handle_send(_App(), "obsidian on the left screen and claude on the right")
        assert result.state is DispatchState.FAILED and not result.detail.get('partial')
        assert desktop == []

    def test_no_window_is_failed_not_success(self, desktop):
        result = wm.handle_send(_App(), "obsidian to monitor 2")
        assert result.state is DispatchState.FAILED and desktop == []

    def test_no_destination_is_refused(self, desktop):
        result = wm.handle_send(_App(), "")
        assert result.state is DispatchState.REJECTED and desktop == []

    def test_foreground_window_when_no_app_named(self, desktop, monkeypatch):
        monkeypatch.setattr(wm.win32gui, 'GetForegroundWindow', lambda: 909)
        result = wm.handle_send(_App(), "this to the right screen")
        assert result.state is DispatchState.COMPLETED and desktop == [(909, 2)]

    def test_falls_back_to_app_verbs_resolver(self, desktop, monkeypatch):
        import plugins.commands.app_verbs as av
        monkeypatch.setattr(av, 'resolve_window', lambda name: (303, "Obsidian - vault", "Obsidian.exe"))
        result = wm.handle_send(_App(), "obsidian on the right")
        assert result.state is DispatchState.COMPLETED and desktop == [(303, 2)]
        assert result.detail['placements'][0]['resolved_by'] == 'resolved'


class TestHandleCursor:
    def test_cursor_to_left_screen(self, monkeypatch):
        moved = []
        monkeypatch.setattr(wm, 'get_monitors', lambda: TWO)
        monkeypatch.setattr(wm, 'teleport_cursor', lambda mon: moved.append(mon['index']))
        assert wm.handle_cursor(_App(), "the left screen") is True
        assert moved == [1]

    def test_cursor_unknown_is_refused(self, monkeypatch):
        monkeypatch.setattr(wm, 'get_monitors', lambda: TWO)
        monkeypatch.setattr(wm, 'find_windows_by_app', lambda name, extra_ignore=None: [])
        result = wm.handle_cursor(_App(), "the purple screen")
        assert result.state is DispatchState.REJECTED
