"""Tests for window manager plugin: saved layouts + lost window recovery."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Minimal win32 stubs so the module imports cleanly in a headless environment
# ---------------------------------------------------------------------------

_win32_mocks = {}
for _mod in ('win32api', 'win32con', 'win32gui', 'win32process', 'psutil'):
    if _mod not in sys.modules:
        _win32_mocks[_mod] = MagicMock()
        sys.modules[_mod] = _win32_mocks[_mod]

# win32con constants used by the plugin
sys.modules['win32con'].SW_SHOWMAXIMIZED = 3
sys.modules['win32con'].SW_MAXIMIZE = 3
sys.modules['win32con'].SW_RESTORE = 9
sys.modules['win32con'].GWL_EXSTYLE = -20

import importlib
import plugins.commands.windows as wm
importlib.reload(wm)


# ---------------------------------------------------------------------------
# Shared monitor fixtures
# ---------------------------------------------------------------------------

MONITOR_1 = {'index': 1, 'rect': (0, 0, 1920, 1080), 'width': 1920, 'height': 1080, 'primary': True}
MONITOR_2 = {'index': 2, 'rect': (1920, 0, 3840, 1080), 'width': 1920, 'height': 1080, 'primary': False}


# ---------------------------------------------------------------------------
# _extract_layout_name
# ---------------------------------------------------------------------------

class TestExtractLayoutName:

    def test_plain_name(self):
        assert wm._extract_layout_name('work') == 'work'

    def test_as_prefix(self):
        assert wm._extract_layout_name('as work') == 'work'

    def test_called_prefix(self):
        assert wm._extract_layout_name('called morning routine') == 'morning routine'

    def test_named_prefix(self):
        assert wm._extract_layout_name('named office') == 'office'

    def test_the_prefix(self):
        assert wm._extract_layout_name('the chill') == 'chill'

    def test_lowercased(self):
        assert wm._extract_layout_name('Work') == 'work'

    def test_empty_returns_none(self):
        assert wm._extract_layout_name('') is None

    def test_whitespace_only_returns_none(self):
        assert wm._extract_layout_name('   ') is None

    def test_reserved_default_rejected(self):
        assert wm._extract_layout_name('default') is None

    def test_reserved_current_rejected(self):
        assert wm._extract_layout_name('current') is None

    def test_reserved_none_rejected(self):
        assert wm._extract_layout_name('none') is None

    def test_too_long_rejected(self):
        assert wm._extract_layout_name('a' * 31) is None

    def test_exactly_30_chars_ok(self):
        name = 'a' * 30
        assert wm._extract_layout_name(name) == name

    def test_multiword_name(self):
        assert wm._extract_layout_name('morning routine') == 'morning routine'

    def test_as_then_reserved_rejected(self):
        assert wm._extract_layout_name('as default') is None


# ---------------------------------------------------------------------------
# _is_rect_on_any_monitor
# ---------------------------------------------------------------------------

class TestIsRectOnAnyMonitor:

    MONITORS = [MONITOR_1, MONITOR_2]

    def test_fully_on_monitor_1(self):
        assert wm._is_rect_on_any_monitor((100, 100, 900, 700), self.MONITORS) is True

    def test_fully_on_monitor_2(self):
        assert wm._is_rect_on_any_monitor((2000, 100, 3000, 700), self.MONITORS) is True

    def test_entirely_off_screen_below(self):
        assert wm._is_rect_on_any_monitor((0, 1200, 400, 1600), self.MONITORS) is False

    def test_entirely_off_screen_left(self):
        assert wm._is_rect_on_any_monitor((-500, 0, -100, 400), self.MONITORS) is False

    def test_partial_overlap_left_edge(self):
        # Window straddles the left edge of monitor 1
        assert wm._is_rect_on_any_monitor((-100, 100, 100, 700), self.MONITORS) is True

    def test_partial_overlap_between_monitors(self):
        # Window spans the seam between monitor 1 and monitor 2
        assert wm._is_rect_on_any_monitor((1900, 0, 1940, 600), self.MONITORS) is True

    def test_zero_size_rect_on_monitor(self):
        # Degenerate (zero-area) rect at a point inside the monitor still
        # passes the overlap check (right > ml, left < mr, ...).
        assert wm._is_rect_on_any_monitor((500, 500, 500, 500), self.MONITORS) is True

    def test_no_monitors_returns_false(self):
        assert wm._is_rect_on_any_monitor((0, 0, 800, 600), []) is False


# ---------------------------------------------------------------------------
# Saved layouts: save / load / overwrite / delete
# ---------------------------------------------------------------------------

class TestSavedLayouts:

    @pytest.fixture(autouse=True)
    def patch_layouts_path(self, tmp_path):
        layouts_file = tmp_path / 'window_layouts.json'
        with patch.object(wm, '_get_layouts_path', return_value=layouts_file):
            yield layouts_file

    @pytest.fixture
    def one_window(self):
        """Minimal movable-window list: one Chrome window."""
        import win32con as _wc
        win32gui = sys.modules['win32gui']
        win32gui.GetWindowRect.return_value = (100, 100, 900, 700)
        win32gui.GetWindowPlacement.return_value = (0, 1, (0, 0), (0, 0), (100, 100, 900, 700))
        psutil = sys.modules['psutil']
        psutil.Process.return_value.name.return_value = 'chrome.exe'
        with patch.object(wm, 'get_all_movable_windows',
                          return_value=[(1001, 'Google Chrome', 9999)]), \
             patch.object(wm, 'get_monitors', return_value=[MONITOR_1]):
            yield

    def test_save_layout_creates_file(self, patch_layouts_path, one_window):
        wm._save_current_layout('work')
        assert patch_layouts_path.exists()
        data = json.loads(patch_layouts_path.read_text())
        assert 'work' in data

    def test_save_layout_structure(self, patch_layouts_path, one_window):
        wm._save_current_layout('work')
        data = json.loads(patch_layouts_path.read_text())
        entry = data['work']
        assert 'created' in entry
        assert 'windows' in entry
        assert len(entry['windows']) == 1
        win = entry['windows'][0]
        assert win['app'] == 'chrome.exe'
        assert win['monitor_index'] == 1
        assert win['rect'] == [100, 100, 900, 700]
        assert win['maximized'] is False

    def test_save_layout_overwrites_existing(self, patch_layouts_path, one_window):
        wm._save_current_layout('work')
        wm._save_current_layout('work')
        data = json.loads(patch_layouts_path.read_text())
        assert len([k for k in data if k == 'work']) == 1

    def test_save_multiple_layouts(self, patch_layouts_path, one_window):
        wm._save_current_layout('work')
        wm._save_current_layout('chill')
        data = json.loads(patch_layouts_path.read_text())
        assert 'work' in data and 'chill' in data

    def test_delete_layout_removes_entry(self, patch_layouts_path, one_window):
        wm._save_current_layout('work')
        wm._delete_layout('work')
        data = json.loads(patch_layouts_path.read_text())
        assert 'work' not in data

    def test_delete_nonexistent_is_noop(self, patch_layouts_path):
        patch_layouts_path.write_text('{}')
        wm._delete_layout('ghost')  # should not raise

    def test_load_all_layouts_empty_when_no_file(self, patch_layouts_path):
        assert wm._load_all_layouts() == {}

    def test_atomic_write_uses_tmp(self, patch_layouts_path, one_window, tmp_path):
        # Verify no .tmp file is left over after a successful save
        wm._save_current_layout('work')
        tmp_file = patch_layouts_path.with_suffix('.json.tmp')
        assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# _restore_layout
# ---------------------------------------------------------------------------

class TestRestoreLayout:

    @pytest.fixture(autouse=True)
    def patch_layouts_path(self, tmp_path):
        layouts_file = tmp_path / 'window_layouts.json'
        with patch.object(wm, '_get_layouts_path', return_value=layouts_file):
            yield layouts_file

    def _write_layout(self, path, name, windows):
        data = {name: {'created': '2026-01-01T00:00:00', 'windows': windows}}
        path.write_text(json.dumps(data))

    def test_restore_calls_setwindowpos(self, patch_layouts_path):
        self._write_layout(patch_layouts_path, 'work', [{
            'app': 'chrome.exe', 'title_pattern': '',
            'monitor_index': 1, 'rect': [100, 100, 900, 700], 'maximized': False,
        }])
        mock_hwnd = 1001
        with patch.object(wm, 'find_windows_by_app', return_value=[mock_hwnd]), \
             patch.object(wm, 'get_monitors', return_value=[MONITOR_1]):
            win32gui = sys.modules['win32gui']
            win32gui.IsIconic.return_value = False
            wm._restore_layout('work')
            win32gui.SetWindowPos.assert_called()

    def test_restore_missing_app_skipped(self, patch_layouts_path):
        self._write_layout(patch_layouts_path, 'work', [{
            'app': 'notepad.exe', 'title_pattern': '',
            'monitor_index': 1, 'rect': [0, 0, 800, 600], 'maximized': False,
        }])
        with patch.object(wm, 'find_windows_by_app', return_value=[]), \
             patch.object(wm, 'get_monitors', return_value=[MONITOR_1]):
            wm._restore_layout('work')  # must not raise

    def test_restore_nonexistent_layout_noop(self, patch_layouts_path):
        patch_layouts_path.write_text('{}')
        with patch.object(wm, 'get_monitors', return_value=[MONITOR_1]):
            wm._restore_layout('ghost')  # must not raise

    def test_restore_maximized_calls_show_maximize(self, patch_layouts_path):
        self._write_layout(patch_layouts_path, 'work', [{
            'app': 'code.exe', 'title_pattern': '',
            'monitor_index': 1, 'rect': [0, 0, 1920, 1080], 'maximized': True,
        }])
        mock_hwnd = 2001
        with patch.object(wm, 'find_windows_by_app', return_value=[mock_hwnd]), \
             patch.object(wm, 'get_monitors', return_value=[MONITOR_1]):
            win32gui = sys.modules['win32gui']
            win32gui.IsIconic.return_value = False
            wm._restore_layout('work')
            win32gui.ShowWindow.assert_called_with(mock_hwnd, sys.modules['win32con'].SW_MAXIMIZE)

    def test_restore_multiple_windows_each_matched(self, patch_layouts_path):
        self._write_layout(patch_layouts_path, 'work', [
            {'app': 'chrome.exe', 'title_pattern': '', 'monitor_index': 1,
             'rect': [0, 0, 800, 600], 'maximized': False},
            {'app': 'code.exe', 'title_pattern': '', 'monitor_index': 2,
             'rect': [1920, 0, 3840, 1080], 'maximized': True},
        ])
        hwnd_chrome, hwnd_code = 1001, 2002
        def _fwa(name, *_a):
            if 'chrome' in name.lower():
                return [hwnd_chrome]
            if 'code' in name.lower():
                return [hwnd_code]
            return []
        with patch.object(wm, 'find_windows_by_app', side_effect=_fwa), \
             patch.object(wm, 'get_monitors', return_value=[MONITOR_1, MONITOR_2]):
            win32gui = sys.modules['win32gui']
            win32gui.IsIconic.return_value = False
            wm._restore_layout('work')
            assert win32gui.SetWindowPos.call_count >= 2


# ---------------------------------------------------------------------------
# _detect_lost_windows
# ---------------------------------------------------------------------------

class TestDetectLostWindows:

    def test_no_lost_windows_when_all_on_screen(self):
        wins = [(101, 'Chrome', 1), (102, 'Code', 2)]
        rects = {101: (100, 100, 900, 700), 102: (2000, 100, 3000, 700)}
        win32gui = sys.modules['win32gui']
        win32gui.GetWindowRect.side_effect = lambda h: rects[h]
        with patch.object(wm, 'get_all_movable_windows', return_value=wins), \
             patch.object(wm, 'get_monitors', return_value=[MONITOR_1, MONITOR_2]):
            lost = wm._detect_lost_windows()
        assert lost == []

    def test_detects_off_screen_window(self):
        wins = [(101, 'Ghost App', 1)]
        win32gui = sys.modules['win32gui']
        # Clear any side_effect set by a prior test before applying return_value
        win32gui.GetWindowRect.side_effect = None
        win32gui.GetWindowRect.return_value = (-2000, -2000, -1000, -1000)
        psutil = sys.modules['psutil']
        psutil.Process.return_value.name.return_value = 'ghost.exe'
        with patch.object(wm, 'get_all_movable_windows', return_value=wins), \
             patch.object(wm, 'get_monitors', return_value=[MONITOR_1]):
            lost = wm._detect_lost_windows()
        assert len(lost) == 1
        assert lost[0]['hwnd'] == 101
        assert lost[0]['title'] == 'Ghost App'

    def test_mixed_on_and_off_screen(self):
        wins = [(101, 'On Screen', 1), (102, 'Off Screen', 2)]
        win32gui = sys.modules['win32gui']
        win32gui.GetWindowRect.side_effect = lambda h: (
            (100, 100, 900, 700) if h == 101 else (-3000, -3000, -2000, -2000)
        )
        psutil = sys.modules['psutil']
        psutil.Process.return_value.name.return_value = 'app.exe'
        with patch.object(wm, 'get_all_movable_windows', return_value=wins), \
             patch.object(wm, 'get_monitors', return_value=[MONITOR_1]):
            lost = wm._detect_lost_windows()
        assert len(lost) == 1
        assert lost[0]['hwnd'] == 102
