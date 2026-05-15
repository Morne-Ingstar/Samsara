"""Mouse-wheel scroll plugin.

Sends WM_MOUSEWHEEL via SendInput at the current cursor position.
Works universally — browsers, Electron apps, Discord, editors — unlike
PageUp/PageDown which many apps intercept or ignore.

Config (optional, under config["scroll"]):
  slow_clicks         : 3   -- "a little" scroll
  default_clicks      : 8   -- plain "scroll up/down"
  medium_clicks       : 15
  medium_high_clicks  : 25
  fast_clicks         : 40

Each click is one WHEEL_DELTA unit (120).
"""

import ctypes
import ctypes.wintypes as wintypes

from samsara.plugin_commands import command

# ── Win32 constants ───────────────────────────────────────────────────────────

INPUT_MOUSE      = 0
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA      = 120

user32 = ctypes.windll.user32


# ── SendInput structures ──────────────────────────────────────────────────────

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   wintypes.DWORD),
        ("dwFlags",     wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type",   wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]


# ── Core scroll helper ────────────────────────────────────────────────────────

def _scroll(clicks: int, up: bool) -> bool:
    """Send a wheel event of `clicks` detents at the current cursor position.

    Args:
        clicks: number of WHEEL_DELTA units to scroll
        up:     True = scroll up (positive delta), False = scroll down

    Returns:
        True if SendInput reported at least one event sent.
    """
    delta = clicks * WHEEL_DELTA * (1 if up else -1)

    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp._input.mi.dwFlags = MOUSEEVENTF_WHEEL
    inp._input.mi.mouseData = wintypes.DWORD(delta & 0xFFFFFFFF)

    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent == 0:
        print("[SCROLL] SendInput failed — check permissions")
        return False
    return True


def _clicks(app, key: str, default: int) -> int:
    """Read a scroll amount from config, falling back to default."""
    cfg = getattr(app, "config", {}) if app is not None else {}
    return cfg.get("scroll", {}).get(key, default)


# ── Commands ──────────────────────────────────────────────────────────────────

@command("scroll up a little", pack="core")
def scroll_up_slow(app, remainder):
    return _scroll(_clicks(app, "slow_clicks", 3), up=True)


@command("scroll down a little", pack="core")
def scroll_down_slow(app, remainder):
    return _scroll(_clicks(app, "slow_clicks", 3), up=False)


@command("scroll up", pack="core")
def scroll_up(app, remainder):
    return _scroll(_clicks(app, "default_clicks", 8), up=True)


@command("scroll down", pack="core")
def scroll_down(app, remainder):
    return _scroll(_clicks(app, "default_clicks", 8), up=False)


@command("scroll up medium", pack="core")
def scroll_up_medium(app, remainder):
    return _scroll(_clicks(app, "medium_clicks", 15), up=True)


@command("scroll down medium", pack="core")
def scroll_down_medium(app, remainder):
    return _scroll(_clicks(app, "medium_clicks", 15), up=False)


@command("scroll up high", pack="core")
def scroll_up_high(app, remainder):
    return _scroll(_clicks(app, "medium_high_clicks", 25), up=True)


@command("scroll down high", pack="core")
def scroll_down_high(app, remainder):
    return _scroll(_clicks(app, "medium_high_clicks", 25), up=False)


@command("scroll up fast", pack="core")
def scroll_up_fast(app, remainder):
    return _scroll(_clicks(app, "fast_clicks", 40), up=True)


@command("scroll down fast", pack="core")
def scroll_down_fast(app, remainder):
    return _scroll(_clicks(app, "fast_clicks", 40), up=False)
