"""Tests for the show_numbers plugin — _parse_spoken_number logic."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub uiautomation / win32 only when the real package is missing (CI).
# An unconditional empty stub leaked into every other test collected in the
# same process (plugin loading then failed on win32api.error) -- queue 56.
for _mod in ('uiautomation', 'win32api', 'win32con', 'win32gui'):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            import types
            sys.modules[_mod] = types.ModuleType(_mod)

from plugins.commands.show_numbers import _get_foreground_control, _parse_spoken_number


def test_digit_simple():
    assert _parse_spoken_number("7") == 7


def test_digit_two_digit():
    assert _parse_spoken_number("37") == 37


def test_word_simple():
    assert _parse_spoken_number("seven") == 7


def test_word_compound():
    assert _parse_spoken_number("thirty seven") == 37


def test_word_ninety_nine():
    assert _parse_spoken_number("ninety nine") == 99


def test_out_of_range():
    assert _parse_spoken_number("237") is None


def test_no_number():
    assert _parse_spoken_number("hello") is None


def test_empty():
    assert _parse_spoken_number("") is None


def test_foreground_control_uses_direct_api_when_available():
    import types
    expected = object()
    auto = types.SimpleNamespace(GetForegroundControl=lambda: expected)
    assert _get_foreground_control(auto) is expected


def test_foreground_control_falls_back_to_handle_api():
    import types
    expected = object()
    auto = types.SimpleNamespace(
        GetForegroundWindow=lambda: 42,
        ControlFromHandle=lambda hwnd: expected if hwnd == 42 else None,
    )
    assert _get_foreground_control(auto) is expected
