"""Pure tests for the typed Unicode injection event builder (2026-07-24)."""
from samsara.clipboard import build_unicode_key_events

KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D


def test_ascii_produces_down_up_pairs():
    ev = build_unicode_key_events("Hi")
    assert ev == [
        (0, ord("H"), KEYEVENTF_UNICODE),
        (0, ord("H"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        (0, ord("i"), KEYEVENTF_UNICODE),
        (0, ord("i"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]


def test_newline_becomes_vk_return_press():
    ev = build_unicode_key_events("a\nb")
    assert (VK_RETURN, 0, 0) in ev
    assert (VK_RETURN, 0, KEYEVENTF_KEYUP) in ev
    # CRLF collapses to one Enter
    assert len(build_unicode_key_events("a\r\nb")) == len(ev)


def test_astral_chars_become_surrogate_pairs():
    ev = build_unicode_key_events("\U0001F600")  # emoji
    scans = [scan for (_vk, scan, flags) in ev if flags & KEYEVENTF_UNICODE and not flags & KEYEVENTF_KEYUP]
    assert scans == [0xD83D, 0xDE00]
    assert len(ev) == 4  # two units x down+up


def test_empty_text_no_events():
    assert build_unicode_key_events("") == []
