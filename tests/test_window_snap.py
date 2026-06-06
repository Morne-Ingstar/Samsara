"""Tests for window snap geometry (_snap_rect, _get_monitor_for_window)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / 'plugins' / 'commands'))

from windows import _snap_rect, _get_monitor_for_window, _SNAP_DIRECTIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _monitor(left, top, right, bottom, index=1, primary=False):
    return {
        'handle': 0,
        'rect': (left, top, right, bottom),
        'width': right - left,
        'height': bottom - top,
        'primary': primary,
        'device': r'\\.\DISPLAY1',
        'index': index,
    }


# 1920x1040 work area (1080p with 40px taskbar at bottom)
MON_1080 = _monitor(0, 0, 1920, 1040, index=1, primary=True)

# 2560x1400 work area (1440p with 40px taskbar)
MON_1440 = _monitor(1920, 0, 4480, 1400, index=2)

# Monitor with odd dimensions (1366x768)
MON_ODD = _monitor(0, 0, 1366, 768, index=1, primary=True)


# ---------------------------------------------------------------------------
# _snap_rect: halves
# ---------------------------------------------------------------------------

def test_snap_left_position():
    x, y, w, h = _snap_rect(MON_1080, 'left')
    assert x == 0 and y == 0

def test_snap_left_size():
    x, y, w, h = _snap_rect(MON_1080, 'left')
    assert w == 960 and h == 1040

def test_snap_right_position():
    x, y, w, h = _snap_rect(MON_1080, 'right')
    assert x == 960 and y == 0

def test_snap_right_size():
    x, y, w, h = _snap_rect(MON_1080, 'right')
    assert w == 960 and h == 1040

def test_snap_left_right_cover_full_width():
    lx, ly, lw, lh = _snap_rect(MON_1080, 'left')
    rx, ry, rw, rh = _snap_rect(MON_1080, 'right')
    assert lx + lw == rx        # zones are contiguous
    assert lw + rw == 1920      # together they fill the full width

def test_snap_top_position():
    x, y, w, h = _snap_rect(MON_1080, 'top')
    assert x == 0 and y == 0

def test_snap_top_size():
    x, y, w, h = _snap_rect(MON_1080, 'top')
    assert w == 1920 and h == 520

def test_snap_bottom_position():
    x, y, w, h = _snap_rect(MON_1080, 'bottom')
    assert x == 0 and y == 520

def test_snap_bottom_size():
    x, y, w, h = _snap_rect(MON_1080, 'bottom')
    assert w == 1920 and h == 520

def test_snap_top_bottom_cover_full_height():
    tx, ty, tw, th = _snap_rect(MON_1080, 'top')
    bx, by, bw, bh = _snap_rect(MON_1080, 'bottom')
    assert ty + th == by        # zones are contiguous
    assert th + bh == 1040      # together they fill the full height


# ---------------------------------------------------------------------------
# _snap_rect: quadrants
# ---------------------------------------------------------------------------

def test_snap_top_left():
    x, y, w, h = _snap_rect(MON_1080, 'top left')
    assert x == 0 and y == 0 and w == 960 and h == 520

def test_snap_top_right():
    x, y, w, h = _snap_rect(MON_1080, 'top right')
    assert x == 960 and y == 0 and w == 960 and h == 520

def test_snap_bottom_left():
    x, y, w, h = _snap_rect(MON_1080, 'bottom left')
    assert x == 0 and y == 520 and w == 960 and h == 520

def test_snap_bottom_right():
    x, y, w, h = _snap_rect(MON_1080, 'bottom right')
    assert x == 960 and y == 520 and w == 960 and h == 520

def test_quadrants_cover_full_area():
    quads = ['top left', 'top right', 'bottom left', 'bottom right']
    total_area = sum(w * h for _, _, w, h in [_snap_rect(MON_1080, d) for d in quads])
    assert total_area == 1920 * 1040


# ---------------------------------------------------------------------------
# _snap_rect: non-zero monitor origin (e.g. secondary monitor at x=1920)
# ---------------------------------------------------------------------------

def test_snap_left_offset_monitor():
    x, y, w, h = _snap_rect(MON_1440, 'left')
    assert x == 1920 and y == 0
    assert w == 1280 and h == 1400

def test_snap_right_offset_monitor():
    x, y, w, h = _snap_rect(MON_1440, 'right')
    assert x == 3200 and y == 0
    assert w == 1280 and h == 1400

def test_snap_bottom_right_offset_monitor():
    x, y, w, h = _snap_rect(MON_1440, 'bottom right')
    assert x == 3200 and y == 700
    assert w == 1280 and h == 700


# ---------------------------------------------------------------------------
# _snap_rect: odd dimensions (floor division gives extra pixel to right/bottom)
# ---------------------------------------------------------------------------

def test_snap_left_odd_width():
    x, y, w, h = _snap_rect(MON_ODD, 'left')
    assert w == 683   # 1366 // 2

def test_snap_right_odd_width():
    x, y, w, h = _snap_rect(MON_ODD, 'right')
    assert w == 683   # 1366 - 683

def test_snap_left_right_contiguous_odd():
    lx, ly, lw, lh = _snap_rect(MON_ODD, 'left')
    rx, ry, rw, rh = _snap_rect(MON_ODD, 'right')
    assert lx + lw == rx
    assert lw + rw == 1366


# ---------------------------------------------------------------------------
# _snap_rect: unknown direction returns None
# ---------------------------------------------------------------------------

def test_snap_unknown_direction():
    assert _snap_rect(MON_1080, 'centre') is None
    assert _snap_rect(MON_1080, '') is None
    assert _snap_rect(MON_1080, 'north') is None


# ---------------------------------------------------------------------------
# _SNAP_DIRECTIONS: set completeness
# ---------------------------------------------------------------------------

def test_all_eight_directions_registered():
    expected = {
        'left', 'right', 'top', 'bottom',
        'top left', 'top right', 'bottom left', 'bottom right',
    }
    assert expected == _SNAP_DIRECTIONS


# ---------------------------------------------------------------------------
# _get_monitor_for_window: center-point routing
# ---------------------------------------------------------------------------

def _make_hwnd_mock(rect):
    """Patch win32gui.GetWindowRect for a fake hwnd."""
    return rect


def test_get_monitor_for_window_on_primary(monkeypatch):
    import win32gui
    monitors = [MON_1080, MON_1440]
    monkeypatch.setattr(win32gui, 'GetWindowRect', lambda hwnd: (100, 100, 800, 600))
    result = _get_monitor_for_window(0xDEAD, monitors)
    assert result is MON_1080

def test_get_monitor_for_window_on_secondary(monkeypatch):
    import win32gui
    monitors = [MON_1080, MON_1440]
    # Window centered at (2500, 300) -- on secondary monitor (1920-4480)
    monkeypatch.setattr(win32gui, 'GetWindowRect', lambda hwnd: (2000, 100, 3000, 500))
    result = _get_monitor_for_window(0xDEAD, monitors)
    assert result is MON_1440

def test_get_monitor_for_window_fallback_to_primary_on_exception(monkeypatch):
    import win32gui
    monitors = [MON_1080, MON_1440]
    monkeypatch.setattr(win32gui, 'GetWindowRect', lambda hwnd: (_ for _ in ()).throw(Exception("fail")))
    result = _get_monitor_for_window(0xDEAD, monitors)
    assert result is MON_1080
