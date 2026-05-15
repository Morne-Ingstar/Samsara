"""Tests for the show_numbers plugin — _parse_spoken_number logic."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub uiautomation so the module loads in CI without the package installed.
if 'uiautomation' not in sys.modules:
    import types
    sys.modules['uiautomation'] = types.ModuleType('uiautomation')

# Stub win32 modules
for _mod in ('win32api', 'win32con', 'win32gui'):
    if _mod not in sys.modules:
        import types
        sys.modules[_mod] = types.ModuleType(_mod)

from plugins.commands.show_numbers import _parse_spoken_number


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
