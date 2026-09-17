"""Queue 49: ARC audit6's clipboard issues, end to end against a FAKE clipboard.

Isolation (the known landmine: earlier live-clipboard tests overwrote what the
owner had copied): every test here replaces samsara.clipboard's _user32 and
_kernel32 objects, ctypes.string_at / memmove, pyperclip and pyautogui with an
in-memory FakeClipboard. The real WinDLL objects are never reachable from the
code under test while a test runs -- an unimplemented call raises
AttributeError instead of touching Windows. test_no_real_clipboard_calls
verifies that by wrapping the real user32 clipboard entry points.

  * CF_HDROP (Explorer "Copy" of files) survives a dictation paste
  * a sequence change between save and restore aborts the restore and leaves
    the user's new copy intact -- including a copy made while the restore
    was being prepared (the race the first check alone left open)
  * an allocation failure mid-restore leaves the clipboard untouched
  * an over-cap format is reported lost by name and restore does not claim
    success (see reports/49 for why restore does not refuse outright)
"""
import logging
import struct
import types

import pytest

from samsara import clipboard as cb
from samsara.clipboard import (
    CF_BITMAP, CF_DIB, CF_HDROP, CF_LOCALE, CF_OEMTEXT, CF_TEXT, CF_UNICODETEXT,
    ClipboardSnapshot, paste_with_preservation, restore_clipboard, save_clipboard,
)

CF_ENHMETAFILE = 14
SHELL_IDLIST = 0xC0A1          # a registered format Explorer adds next to CF_HDROP


def _dropfiles(*paths):
    """A real DROPFILES block: 20-byte header (pFiles=20, fWide=1) + wide paths."""
    body = "".join(p + "\0" for p in paths) + "\0"
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + body.encode("utf-16-le")


def _utf16(text):
    return (text + "\0").encode("utf-16-le")


class FakeClipboard:
    """In-memory Win32 clipboard + global memory, shaped like the calls
    samsara.clipboard makes. Pointers are the handle numbers."""

    def __init__(self, formats=None):
        self.formats = dict(formats or {})
        self.seq = 100
        self.is_open = False
        self.mem = {}
        self.handle_fmt = {}
        self.next_handle = 5000
        self.owner = 0
        self.alloc_fail_on = None      # 1-based GlobalAlloc call number that returns NULL
        self.alloc_calls = 0
        self.set_fail_fmts = set()
        self.size_override = {}        # fmt -> reported GlobalSize
        self.before_open = None        # callable run just before a successful OpenClipboard
        self.empties = 0
        self.allocated = set()         # GlobalAlloc handles not yet freed or handed to SetClipboardData

    # -- user32 ---------------------------------------------------------
    def OpenClipboard(self, _hwnd):
        if self.is_open:
            return 0
        if self.before_open:
            hook, self.before_open = self.before_open, None
            hook()
        self.is_open = True
        return 1

    def CloseClipboard(self):
        self.is_open = False
        return 1

    def EmptyClipboard(self):
        assert self.is_open, "EmptyClipboard without OpenClipboard"
        self.formats = {}
        self.seq += 1
        self.empties += 1
        return 1

    def EnumClipboardFormats(self, current):
        assert self.is_open
        keys = list(self.formats)
        i = 0 if current == 0 else keys.index(current) + 1
        return keys[i] if i < len(keys) else 0

    def GetClipboardData(self, fmt):
        assert self.is_open
        if fmt not in self.formats:
            return 0
        h = self._new_handle(bytearray(self.formats[fmt]))
        self.handle_fmt[h] = fmt
        return h

    def SetClipboardData(self, fmt, h):
        assert self.is_open
        if fmt in self.set_fail_fmts:
            return 0
        self.formats[fmt] = bytes(self.mem.pop(h))
        self.allocated.discard(h)      # ownership passes to the clipboard
        self.seq += 1
        return h

    def GetClipboardSequenceNumber(self):
        return self.seq

    def GetClipboardOwner(self):
        return self.owner

    def IsHungAppWindow(self, _hwnd):
        return False

    def SendMessageTimeoutW(self, *_args):
        return 1

    def CountClipboardFormats(self):
        return len(self.formats)

    def GetClipboardFormatNameW(self, fmt, buf, _n):
        if fmt == SHELL_IDLIST:
            buf.value = "Shell IDList Array"
            return len(buf.value)
        return 0

    # -- kernel32 -------------------------------------------------------
    def GlobalAlloc(self, _flags, size):
        self.alloc_calls += 1
        if self.alloc_fail_on == self.alloc_calls:
            return 0
        h = self._new_handle(bytearray(size))
        self.allocated.add(h)
        return h

    def GlobalSize(self, h):
        fmt = self.handle_fmt.get(h)
        if fmt in self.size_override:
            return self.size_override[fmt]
        return len(self.mem[h])

    def GlobalLock(self, h):
        return h if h in self.mem else 0

    def GlobalUnlock(self, _h):
        return 1

    def GlobalFree(self, h):
        self.mem.pop(h, None)
        self.allocated.discard(h)
        return 0

    # -- helpers --------------------------------------------------------
    def _new_handle(self, data):
        self.next_handle += 1
        self.mem[self.next_handle] = data
        return self.next_handle

    def string_at(self, ptr, size):
        return bytes(self.mem[ptr][:size])

    def memmove(self, ptr, raw, n):
        self.mem[ptr][:n] = raw
        return ptr

    def user_copies_text(self, text):
        """What pyperclip.copy / a user's Ctrl+C does: one atomic replace."""
        self.formats = {CF_UNICODETEXT: _utf16(text)}
        self.seq += 1

    def text(self):
        raw = self.formats.get(CF_UNICODETEXT)
        return raw.decode("utf-16-le").rstrip("\0") if raw else ""


@pytest.fixture
def fake(monkeypatch):
    clip = FakeClipboard()
    user32 = types.SimpleNamespace(**{name: getattr(clip, name) for name in (
        "OpenClipboard", "CloseClipboard", "EmptyClipboard", "EnumClipboardFormats", "GetClipboardData",
        "SetClipboardData", "GetClipboardSequenceNumber", "GetClipboardOwner", "IsHungAppWindow",
        "SendMessageTimeoutW", "CountClipboardFormats", "GetClipboardFormatNameW")})
    kernel32 = types.SimpleNamespace(**{name: getattr(clip, name) for name in (
        "GlobalAlloc", "GlobalSize", "GlobalLock", "GlobalUnlock", "GlobalFree")})
    monkeypatch.setattr(cb, "_user32", user32)
    monkeypatch.setattr(cb, "_kernel32", kernel32)
    monkeypatch.setattr(cb.sys, "platform", "win32")
    monkeypatch.setattr(cb, "ctypes", types.SimpleNamespace(
        string_at=clip.string_at, memmove=clip.memmove,
        create_unicode_buffer=lambda n: types.SimpleNamespace(value=""),
        c_size_t=lambda v=0: v, byref=lambda v: v))
    monkeypatch.setattr(cb, "_OPEN_CLIPBOARD_MAX_RETRIES", 1)
    monkeypatch.setattr(cb, "pyperclip", types.SimpleNamespace(copy=clip.user_copies_text, paste=clip.text))
    monkeypatch.setattr(cb, "HAS_PYPERCLIP", True)
    import time as real_time
    monkeypatch.setattr(cb, "time", types.SimpleNamespace(sleep=lambda _s: None, monotonic=real_time.monotonic))
    return clip


@pytest.fixture
def target(monkeypatch, fake):
    """A fake pyautogui whose Ctrl+V 'reads' the clipboard; tests can hang an
    extra action off it (e.g. the user copying something mid-window)."""
    pasted, after_paste = [], []

    def hotkey(*keys):
        assert keys == ("ctrl", "v")
        pasted.append(fake.text())
        for action in after_paste:
            action()

    module = types.SimpleNamespace(hotkey=hotkey)
    monkeypatch.setitem(__import__("sys").modules, "pyautogui", module)
    return types.SimpleNamespace(pasted=pasted, after_paste=after_paste)


# ---------------------------------------------------------------------------
# Issue 1: CF_HDROP
# ---------------------------------------------------------------------------

def test_files_copied_in_explorer_survive_a_dictation_paste(fake, target):
    original = {
        CF_HDROP: _dropfiles(r"C:\Users\me\report.docx", r"C:\Users\me\photo.png"),
        SHELL_IDLIST: b"\x02\x00\x00\x00idlist-bytes",
    }
    fake.formats = dict(original)

    ok = paste_with_preservation("the dictated sentence", paste_delay=0, restore_delay=0)

    assert ok is True
    assert target.pasted == ["the dictated sentence"]
    assert fake.formats == original, "the file list must come back byte-identical"


def test_incomplete_file_snapshot_refuses_to_replace_explorer_copy(fake, target):
    original = {CF_HDROP: _dropfiles(r"C:\Users\me\large-file-list.txt")}
    fake.formats = dict(original)
    fake.size_override[CF_HDROP] = 101 * 1024 * 1024

    assert paste_with_preservation("the dictated sentence", paste_delay=0, restore_delay=0) is False
    assert target.pasted == []
    assert fake.formats == original


# ---------------------------------------------------------------------------
# Issue 2: sequence-number guard
# ---------------------------------------------------------------------------

def test_user_copy_during_the_paste_window_aborts_restore_and_is_kept(fake, target):
    fake.formats = {CF_UNICODETEXT: _utf16("what I had before")}
    target.after_paste.append(lambda: fake.user_copies_text("https://the-url-i-just-copied"))

    paste_with_preservation("dictated", paste_delay=0, restore_delay=0)

    assert fake.text() == "https://the-url-i-just-copied"
    assert fake.empties == 0, "the clipboard must never be emptied once the user has copied"


def test_user_copy_while_the_restore_is_being_prepared_is_kept(fake, target):
    """The first sequence check passes; the user copies before our
    OpenClipboard succeeds. The second check, made while we hold the
    clipboard, must catch it."""
    fake.formats = {CF_UNICODETEXT: _utf16("what I had before")}
    target.after_paste.append(
        lambda: setattr(fake, "before_open", lambda: fake.user_copies_text("copied during prepare")))

    paste_with_preservation("dictated", paste_delay=0, restore_delay=0)

    assert fake.text() == "copied during prepare"
    assert fake.empties == 0
    assert fake.allocated == set(), "prepared handles must be freed on abort"


def test_unchanged_sequence_restores_the_original(fake, target):
    fake.formats = {CF_UNICODETEXT: _utf16("what I had before")}
    paste_with_preservation("dictated", paste_delay=0, restore_delay=0)
    assert fake.text() == "what I had before"


# ---------------------------------------------------------------------------
# Issue 3: atomic restore
# ---------------------------------------------------------------------------

def test_allocation_failure_mid_restore_leaves_the_clipboard_untouched(fake):
    snapshot = ClipboardSnapshot({CF_HDROP: _dropfiles(r"C:\a.txt"), CF_DIB: b"B" * 64, SHELL_IDLIST: b"idl"})
    fake.formats = {CF_UNICODETEXT: _utf16("what is on the clipboard now")}
    before, seq_before = dict(fake.formats), fake.seq
    fake.alloc_fail_on = 2

    assert restore_clipboard(snapshot) is False

    assert fake.formats == before and fake.seq == seq_before and fake.empties == 0
    assert fake.allocated == set(), "the first, successful allocation must be freed"


def test_set_failure_after_empty_is_reported_as_partial(fake, caplog):
    snapshot = ClipboardSnapshot({CF_HDROP: _dropfiles(r"C:\a.txt"), SHELL_IDLIST: b"idl"})
    fake.set_fail_fmts = {SHELL_IDLIST}
    with caplog.at_level(logging.WARNING):
        assert restore_clipboard(snapshot) is False
    assert CF_HDROP in fake.formats
    assert any("restore was partial" in r.getMessage() and "Shell IDList Array" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# Issue 4: incomplete snapshots
# ---------------------------------------------------------------------------

def test_over_cap_format_is_reported_lost_and_restore_does_not_claim_success(fake, target, caplog):
    """49 decision (see reports/49): restore does not REFUSE an incomplete
    snapshot -- when it runs, our own text already replaced the user's
    clipboard, so refusing would leave only that. It puts back what was
    saved, names what was not, and returns False."""
    fake.formats = {CF_DIB: b"D" * 32, CF_HDROP: _dropfiles(r"C:\big.psd")}
    fake.size_override[CF_DIB] = 101 * 1024 * 1024

    with caplog.at_level(logging.WARNING):
        snapshot = save_clipboard()
    assert snapshot.complete is False
    assert snapshot.lost == {CF_DIB: "over the 100 MB cap"}
    assert any("snapshot is incomplete" in r.getMessage() and "CF_DIB" in r.getMessage()
               and "100 MB" in r.getMessage() for r in caplog.records)

    fake.user_copies_text("dictated")
    snapshot.seq = fake.seq
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert restore_clipboard(snapshot) is False
    assert fake.formats == {CF_HDROP: _dropfiles(r"C:\big.psd")}, "what was saved still comes back"
    assert any("WITHOUT content that could not be snapshotted" in r.getMessage() and "CF_DIB" in r.getMessage()
               for r in caplog.records)


def test_formats_windows_resynthesizes_do_not_make_a_snapshot_incomplete(fake):
    fake.formats = {CF_UNICODETEXT: _utf16("hi"), CF_TEXT: b"hi\0", CF_OEMTEXT: b"hi\0", CF_LOCALE: b"\x09\x04\0\0",
                    CF_BITMAP: b"hbitmap-handle", CF_DIB: b"dib-bytes"}
    snapshot = save_clipboard()
    assert snapshot.complete is True, snapshot.lost
    assert set(snapshot) == {CF_UNICODETEXT, CF_DIB}          # legacy text deduped, CF_BITMAP never snapshotted


def test_unsnapshottable_office_format_is_named_as_lost(fake):
    fake.formats = {CF_UNICODETEXT: _utf16("cells"), CF_ENHMETAFILE: b"emf-handle"}
    snapshot = save_clipboard()
    assert snapshot.lost == {CF_ENHMETAFILE: "not a format that can be snapshotted"}
    assert "CF_ENHMETAFILE" in snapshot.describe_lost()


# ---------------------------------------------------------------------------
# Issue 7: delayed rendering
# ---------------------------------------------------------------------------

def test_unresponsive_owner_skips_the_snapshot_and_says_the_content_is_lost(fake, caplog):
    fake.formats = {CF_UNICODETEXT: _utf16("from a frozen app")}
    fake.owner = 4242
    fake.SendMessageTimeoutW = lambda *a: 0                   # no answer within the timeout
    cb._user32.SendMessageTimeoutW = fake.SendMessageTimeoutW
    get_calls = []
    cb._user32.GetClipboardData = lambda fmt: get_calls.append(fmt) or 0

    with caplog.at_level(logging.WARNING):
        snapshot = save_clipboard()

    assert dict(snapshot) == {} and get_calls == [], "must not risk blocking in GetClipboardData"
    assert snapshot.lost == {0: "clipboard owner not responding"}
    assert any("could not be snapshotted" in r.getMessage() for r in caplog.records)


def test_slow_snapshot_is_logged(fake, monkeypatch, caplog):
    fake.formats = {CF_UNICODETEXT: _utf16("big range")}
    ticks = iter([10.0, 11.4])
    monkeypatch.setattr(cb.time, "monotonic", lambda: next(ticks, 11.4))   # cb.time is the fixture's namespace
    with caplog.at_level(logging.WARNING):
        save_clipboard()
    assert any("snapshot took 1.4 s" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Isolation proof
# ---------------------------------------------------------------------------

def test_no_real_clipboard_calls(monkeypatch, fake, target):
    """While the fake is installed, the real user32 clipboard functions must
    never be called by the code under test."""
    import ctypes
    real = ctypes.windll.user32
    calls = []
    for name in ("OpenClipboard", "EmptyClipboard", "SetClipboardData", "GetClipboardData"):
        # Record and REFUSE -- never forward to Windows, even if reached.
        monkeypatch.setattr(real, name, lambda *a, _n=name: calls.append(_n) or 0)
    fake.formats = {CF_HDROP: _dropfiles(r"C:\x.txt")}
    paste_with_preservation("dictated", paste_delay=0, restore_delay=0)
    assert calls == []
