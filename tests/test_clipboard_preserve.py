"""Tests for samsara.clipboard: the clipboard save/restore round-trip that
must preserve images (screenshots), not just text.

Background (see tools/clipboard_probe.py for the full empirical
investigation): the bug was that save_clipboard()'s format allowlist only
ever covered text formats -- CF_DIB (the standard, GlobalAlloc-memory-safe
image interchange format) was excluded alongside CF_BITMAP (a GDI handle,
genuinely unsafe to GlobalLock). This meant image data was silently dropped
at the very first enumeration pass, before any handle-type/synthesis
concern even applied -- the debug line's "N formats saved" was truthful
about what it captured, but what it captured never included the image.

There is no separate "convert CF_BITMAP to CF_DIB" function to test: the
fix relies on Windows' own format synthesis (confirmed empirically -- when
only CF_DIB is placed on the clipboard, Windows' own EnumClipboardFormats
already reports CF_BITMAP/CF_DIBV5 as available too, and the reverse holds).
save_clipboard() simply requests CF_DIB directly; Windows renders it from
whatever image format the source app actually provided. So
is_snapshot_eligible_format(CF_BITMAP) being False is the whole "conversion
policy" -- there's nothing else to convert.

Pure-logic tests need no clipboard. The one real round-trip test is
skipped when clipboard access isn't available (CI-safety).

Clipboard-safety net: TestRealClipboardRoundTrip's tests deliberately
clobber the live Windows clipboard as part of exercising save/restore --
without the `preserve_real_clipboard` fixture below, whatever the user
actually had copied gets overwritten and left as one of this file's fixture
strings (this really happened -- "original clipboard text" ended up pasted
into real documents). The fixture snapshots the clipboard via RAW
win32clipboard calls -- independent of samsara.clipboard.save_clipboard(),
the code under test -- before each such test and restores it after,
regardless of pass/fail. These tests are also tagged @pytest.mark.clipboard
so a clipboard-safe run is available via `-m "not clipboard"` even though
the fixture already makes the default (unfiltered) run harmless.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import clipboard as clipboard_module
from samsara.clipboard import (
    CF_TEXT, CF_BITMAP, CF_OEMTEXT, CF_DIB, CF_UNICODETEXT, CF_LOCALE, CF_DIBV5,
    CF_HDROP,
    SAFE_FORMATS,
    ClipboardSnapshot,
    is_snapshot_eligible_format,
    is_nonempty_payload,
    save_clipboard,
    restore_clipboard,
)


# ============================================================================
# Format selection -- which formats get snapshotted
# ============================================================================

class TestSnapshotEligibleFormats:
    def test_cf_dib_is_eligible(self):
        """The actual bug: CF_DIB is a genuine GlobalAlloc memory block
        (BITMAPINFOHEADER + pixel bytes) and must be snapshotted."""
        assert is_snapshot_eligible_format(CF_DIB) is True

    def test_cf_bitmap_is_not_eligible(self):
        """CF_BITMAP is a GDI HBITMAP handle, not memory -- GlobalLock on
        it is unsafe. Must never be a snapshot target."""
        assert is_snapshot_eligible_format(CF_BITMAP) is False

    def test_cf_dibv5_is_eligible(self):
        """FIX 5: technically-memory-safe CF_DIBV5 is now snapshotted too --
        unlike CF_BITMAP, CF_DIB->CF_DIBV5 synthesis on restore can't
        reconstruct the per-pixel alpha channel DIBV5 carries."""
        assert is_snapshot_eligible_format(CF_DIBV5) is True

    def test_cf_hdrop_is_eligible(self):
        """FIX 2: CF_HDROP is a genuine GlobalAlloc memory block (a
        DROPFILES struct + paths) -- restoring it re-enables pasting
        Explorer-copied files after a dictation paste."""
        assert is_snapshot_eligible_format(CF_HDROP) is True

    def test_text_formats_are_eligible(self):
        assert is_snapshot_eligible_format(CF_TEXT) is True
        assert is_snapshot_eligible_format(CF_OEMTEXT) is True
        assert is_snapshot_eligible_format(CF_UNICODETEXT) is True
        assert is_snapshot_eligible_format(CF_LOCALE) is True

    def test_registered_formats_are_eligible(self):
        """Format IDs >= 0xC000 are app-registered formats (e.g. "PNG",
        "HTML Format") -- conventionally GlobalAlloc memory too."""
        assert is_snapshot_eligible_format(0xC000) is True
        assert is_snapshot_eligible_format(0xC0FF) is True

    def test_unknown_low_formats_are_not_eligible(self):
        """CF_METAFILEPICT(3), CF_TIFF(6), CF_PALETTE(9), etc. -- anything
        not explicitly in SAFE_FORMATS and below the registered-format
        range must be excluded."""
        for fmt in (3, 4, 5, 6, 9, 10, 11, 12, 14):
            assert is_snapshot_eligible_format(fmt) is False, f"format {fmt} should not be eligible"

    def test_safe_formats_set_matches_documented_contents(self):
        assert SAFE_FORMATS == {
            CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE, CF_DIB, CF_HDROP, CF_DIBV5,
        }


# ============================================================================
# Non-empty payload predicate -- what counts as "saved"/"restored"
# ============================================================================

class TestNonEmptyPayload:
    def test_empty_bytes_excluded(self):
        assert is_nonempty_payload(b"") is False

    def test_none_excluded(self):
        assert is_nonempty_payload(None) is False

    def test_nonempty_bytes_included(self):
        assert is_nonempty_payload(b"\x00") is True
        assert is_nonempty_payload(b"some image bytes") is True


# ============================================================================
# Mocked win32-API tests -- exercise save/restore control flow without a
# real clipboard, by monkeypatching samsara.clipboard's module-level
# _user32/_kernel32 (or the higher-level helper functions built on them).
# Windows-only: _user32/_kernel32 are None on other platforms.
# ============================================================================

_WIN32_ONLY = pytest.mark.skipif(
    sys.platform != 'win32', reason="mocks target the Windows clipboard API"
)


def _install_fake_clipboard(monkeypatch, formats: dict) -> None:
    """Patch samsara.clipboard's win32 layer so _save_clipboard_impl()
    enumerates exactly `formats` ({fmt_id: bytes}) and reads back exactly
    those bytes -- without ever touching the real clipboard or real process
    memory. The format id itself doubles as the fake handle/pointer, since
    GetClipboardData/GlobalLock/string_at are all faked to key off it."""
    monkeypatch.setattr(clipboard_module, "_open_clipboard_with_retry", lambda *a, **kw: True)
    monkeypatch.setattr(clipboard_module._user32, "CloseClipboard", lambda: True)
    monkeypatch.setattr(clipboard_module._user32, "GetClipboardOwner", lambda: 0)
    monkeypatch.setattr(clipboard_module._user32, "IsHungAppWindow", lambda h: False)

    fmt_ids = list(formats.keys())
    state = {"i": 0}

    def fake_enum(_current):
        if state["i"] >= len(fmt_ids):
            return 0
        fmt = fmt_ids[state["i"]]
        state["i"] += 1
        return fmt

    monkeypatch.setattr(clipboard_module._user32, "EnumClipboardFormats", fake_enum)
    monkeypatch.setattr(clipboard_module._user32, "GetClipboardData", lambda fmt: fmt)
    monkeypatch.setattr(clipboard_module._kernel32, "GlobalSize", lambda h: len(formats[h]))
    monkeypatch.setattr(clipboard_module._kernel32, "GlobalLock", lambda h: h)
    monkeypatch.setattr(clipboard_module._kernel32, "GlobalUnlock", lambda h: True)
    monkeypatch.setattr(clipboard_module.ctypes, "string_at", lambda ptr, size: formats[ptr])


@_WIN32_ONLY
class TestSequenceNumberGuard:
    """FIX 1: restore_clipboard() aborts if the clipboard sequence number
    changed since Samsara's own dictated-text copy, rather than clobbering
    whatever changed it."""

    def test_seq_changed_aborts_restore_without_touching_clipboard(self, monkeypatch):
        saved = ClipboardSnapshot({CF_UNICODETEXT: b"original"}, seq=1)
        monkeypatch.setattr(clipboard_module, "get_clipboard_sequence_number", lambda: 2)

        open_calls = []
        monkeypatch.setattr(
            clipboard_module, "_open_clipboard_with_retry",
            lambda *a, **kw: open_calls.append(1) or True,
        )

        result = restore_clipboard(saved)

        assert result is True, "aborting to preserve the user's copy is reported as success, not failure"
        assert open_calls == [], "clipboard must never be opened once a sequence-number mismatch is detected"

    def test_seq_unchanged_lets_restore_proceed(self, monkeypatch):
        """A matching seq must not short-circuit restore -- execution
        should reach the (real) atomic-prepare phase."""
        saved = ClipboardSnapshot({CF_UNICODETEXT: b"original"}, seq=1)
        monkeypatch.setattr(clipboard_module, "get_clipboard_sequence_number", lambda: 1)

        alloc_calls = []
        monkeypatch.setattr(
            clipboard_module._kernel32, "GlobalAlloc",
            lambda flags, size: (alloc_calls.append(size), 0)[1],  # fail fast, no real memory touched
        )

        result = restore_clipboard(saved)

        assert alloc_calls == [len(b"original")], "matching seq must let restore proceed into the prepare phase"
        assert result is False  # the forced GlobalAlloc failure above

    def test_plain_dict_without_seq_skips_check(self, monkeypatch):
        """Backward compatibility: a plain dict (no .seq attribute, e.g.
        from a caller other than paste_with_preservation) must not be
        affected by the guard at all."""
        saved = {CF_UNICODETEXT: b"original"}
        monkeypatch.setattr(
            clipboard_module, "get_clipboard_sequence_number",
            lambda: (_ for _ in ()).throw(AssertionError("must not be called when saved has no seq")),
        )
        monkeypatch.setattr(clipboard_module._kernel32, "GlobalAlloc", lambda flags, size: 0)

        result = restore_clipboard(saved)

        assert result is False  # reaches the (forced-failing) prepare phase, proving the guard was skipped


@_WIN32_ONLY
class TestAtomicRestore:
    """FIX 3: any allocation failure during restore's prepare phase frees
    everything prepared so far and returns without ever opening/emptying
    the live clipboard -- the prior clipboard content is left untouched."""

    def test_allocation_failure_never_opens_clipboard(self, monkeypatch):
        saved = ClipboardSnapshot({CF_UNICODETEXT: b"hello"})
        monkeypatch.setattr(clipboard_module._kernel32, "GlobalAlloc", lambda flags, size: 0)

        open_calls = []
        monkeypatch.setattr(
            clipboard_module, "_open_clipboard_with_retry",
            lambda *a, **kw: open_calls.append(1) or True,
        )

        result = restore_clipboard(saved)

        assert result is False
        assert open_calls == [], "clipboard must never be opened if any format fails to allocate"

    def test_allocation_failure_frees_earlier_successful_allocations(self, monkeypatch):
        """A failure on the second format must not leak the handle already
        allocated for the first."""
        saved = ClipboardSnapshot({CF_UNICODETEXT: b"first", CF_TEXT: b"second"})
        freed = []
        alloc_seq = iter([111, 0])  # first alloc succeeds, second fails
        monkeypatch.setattr(clipboard_module._kernel32, "GlobalAlloc", lambda flags, size: next(alloc_seq))
        monkeypatch.setattr(clipboard_module._kernel32, "GlobalLock", lambda h: h)
        monkeypatch.setattr(clipboard_module._kernel32, "GlobalUnlock", lambda h: True)
        monkeypatch.setattr(clipboard_module._kernel32, "GlobalFree", lambda h: freed.append(h))
        monkeypatch.setattr(clipboard_module.ctypes, "memmove", lambda *a, **kw: None)

        result = restore_clipboard(saved)

        assert result is False
        assert 111 in freed, "the handle from the first, successful allocation must be freed on abort"


@_WIN32_ONLY
class TestLegacyTextDedup:
    """FIX 6: when CF_UNICODETEXT is captured non-empty, save_clipboard()
    drops CF_TEXT/CF_OEMTEXT/CF_LOCALE -- Windows synthesizes them from
    CF_UNICODETEXT on restore."""

    def test_unicode_present_skips_legacy_text_formats(self, monkeypatch):
        formats = {
            CF_UNICODETEXT: "hello".encode("utf-16-le"),
            CF_TEXT: b"hello",
            CF_OEMTEXT: b"hello",
            CF_LOCALE: b"\x09\x04\x00\x00",
        }
        _install_fake_clipboard(monkeypatch, formats)

        saved = save_clipboard()

        assert CF_UNICODETEXT in saved
        assert CF_TEXT not in saved
        assert CF_OEMTEXT not in saved
        assert CF_LOCALE not in saved

    def test_unicode_absent_keeps_legacy_text_formats(self, monkeypatch):
        """No CF_UNICODETEXT captured -> nothing to synthesize legacy text
        from, so the legacy formats must be kept."""
        formats = {CF_TEXT: b"hello", CF_OEMTEXT: b"hello"}
        _install_fake_clipboard(monkeypatch, formats)

        saved = save_clipboard()

        assert CF_TEXT in saved
        assert CF_OEMTEXT in saved


@_WIN32_ONLY
class TestHungOwnerGuard:
    """FIX 4: a hung clipboard owner (would block GetClipboardData
    indefinitely) makes save_clipboard() skip the snapshot entirely --
    empty save, no enumeration attempted -- rather than risk hanging
    dictation on it."""

    def test_hung_owner_yields_empty_save_without_enumerating(self, monkeypatch):
        monkeypatch.setattr(clipboard_module, "_open_clipboard_with_retry", lambda *a, **kw: True)
        monkeypatch.setattr(clipboard_module._user32, "CloseClipboard", lambda: True)
        monkeypatch.setattr(clipboard_module._user32, "GetClipboardOwner", lambda: 12345)
        monkeypatch.setattr(clipboard_module._user32, "IsHungAppWindow", lambda h: True)
        enum_calls = []
        monkeypatch.setattr(
            clipboard_module._user32, "EnumClipboardFormats",
            lambda cur: (enum_calls.append(1), 0)[1],
        )

        saved = save_clipboard()

        assert saved == {}
        assert enum_calls == [], "must not attempt enumeration when the owner is hung"

    def test_empty_save_lets_paste_proceed_without_restore(self, monkeypatch):
        """The dictation paste itself must still go through on an empty
        (hung-owner) save -- and with nothing saved, restore_clipboard()
        must never be invoked."""
        monkeypatch.setattr(clipboard_module, "save_clipboard", lambda: ClipboardSnapshot())
        restore_calls = []
        monkeypatch.setattr(
            clipboard_module, "restore_clipboard",
            lambda saved: restore_calls.append(saved) or True,
        )
        monkeypatch.setattr(clipboard_module.pyperclip, "copy", lambda t: None)
        monkeypatch.setattr("pyautogui.hotkey", lambda *a, **kw: None, raising=False)

        result = clipboard_module.paste_with_preservation("dictated text", paste_delay=0, restore_delay=0)

        assert result is True
        assert restore_calls == [], "an empty save must not trigger a restore call"


# ============================================================================
# Real clipboard round-trip -- skipped if clipboard access is unavailable
# ============================================================================

def _clipboard_available() -> bool:
    if sys.platform != 'win32':
        return False
    try:
        import win32clipboard  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    try:
        import ctypes
        opened = ctypes.windll.user32.OpenClipboard(None)
        if opened:
            ctypes.windll.user32.CloseClipboard()
        return bool(opened)
    except Exception:
        return False


# ============================================================================
# Real-clipboard safety net -- snapshot/restore around every live test
# ============================================================================

# Minimum formats required by the task; both are what the round-trip tests
# themselves clobber, so restoring just these two puts the user's clipboard
# back exactly as they'd observe it (text, or an image via CF_DIB). These
# are just the standard Windows format-ID constants (already imported above
# for the pure-logic tests) -- reusing them isn't reusing any save/restore
# LOGIC from the code under test, so the snapshot/restore below stays
# independent of samsara.clipboard as required.
_SNAPSHOT_FORMATS = (CF_UNICODETEXT, CF_DIB)


def _snapshot_real_clipboard() -> dict:
    """Raw win32clipboard snapshot of whatever's really on the clipboard
    right now. Deliberately does NOT go through
    samsara.clipboard.save_clipboard() (the code under test) -- this safety
    net must work independently of it, or a bug in save_clipboard() could
    silently disable its own regression protection."""
    import win32clipboard

    snapshot = {}
    win32clipboard.OpenClipboard()
    try:
        for fmt in _SNAPSHOT_FORMATS:
            try:
                if not win32clipboard.IsClipboardFormatAvailable(fmt):
                    continue
                data = win32clipboard.GetClipboardData(fmt)
                if fmt == CF_DIB:
                    data = bytes(data)
                snapshot[fmt] = data
            except Exception:
                continue
    finally:
        win32clipboard.CloseClipboard()
    return snapshot


def _restore_real_clipboard(snapshot: dict) -> None:
    """Restore a snapshot taken by _snapshot_real_clipboard(), or just
    empty the clipboard if the snapshot was empty (the user's clipboard
    genuinely had nothing in the snapshotted formats)."""
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        for fmt, data in snapshot.items():
            win32clipboard.SetClipboardData(fmt, data)
    finally:
        win32clipboard.CloseClipboard()


@pytest.fixture
def preserve_real_clipboard():
    """Snapshot the user's REAL clipboard before a live-clipboard test,
    restore it after -- no matter what the test does or whether it fails.

    Function-scoped, not module/session-scoped: each test in
    TestRealClipboardRoundTrip clobbers the clipboard multiple times as
    part of its own body (that's the thing under test), so only a
    snapshot/restore around EACH test guarantees no test leaks clobbered
    state into the next one, and that the user's real clipboard is
    correctly restored even if one test in the class fails while another
    passes. The snapshot/restore calls themselves are a handful of Win32
    API calls -- negligible cost per test.
    """
    snapshot = _snapshot_real_clipboard()
    yield
    try:
        _restore_real_clipboard(snapshot)
    except Exception:
        logging.getLogger("Samsara").error(
            "[CLIPBOARD-TEST] Failed to restore the user's real clipboard "
            "after a clipboard test -- their clipboard may now contain "
            "test fixture data instead of what they actually had copied.",
            exc_info=True,
        )


@pytest.mark.clipboard
@pytest.mark.skipif(not _clipboard_available(), reason="clipboard not available in this environment")
class TestRealClipboardRoundTrip:
    def test_text_and_dib_survive_save_clobber_restore(self, preserve_real_clipboard):
        import io
        import win32clipboard
        from PIL import Image

        # Build a real DIB payload (BITMAPINFO header + pixel bytes --
        # what CF_DIB actually is, same technique tools/clipboard_probe.py
        # uses to put an image on the clipboard programmatically).
        img = Image.new("RGB", (32, 24), color=(10, 200, 60))
        buf = io.BytesIO()
        img.save(buf, "BMP")
        dib_bytes = buf.getvalue()[14:]  # strip the 14-byte BITMAPFILEHEADER

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib_bytes)
        finally:
            win32clipboard.CloseClipboard()

        saved = save_clipboard()
        assert CF_DIB in saved, "CF_DIB must be captured by save_clipboard()"
        assert saved[CF_DIB] == dib_bytes, "captured DIB bytes must match exactly"

        # Clobber -- simulates the dictation paste overwriting the clipboard.
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText("dictated sentence", win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

        ok = restore_clipboard(saved)
        assert ok is True

        # Verify the image survived byte-identically.
        win32clipboard.OpenClipboard()
        try:
            restored_dib = win32clipboard.GetClipboardData(win32clipboard.CF_DIB)
        finally:
            win32clipboard.CloseClipboard()
        assert bytes(restored_dib) == dib_bytes, "restored DIB bytes must match the original exactly"

    def test_text_only_clipboard_still_round_trips(self, preserve_real_clipboard):
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText("original clipboard text", win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

        saved = save_clipboard()
        assert CF_UNICODETEXT in saved

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText("clobbered", win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

        assert restore_clipboard(saved) is True

        win32clipboard.OpenClipboard()
        try:
            restored_text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        assert restored_text == "original clipboard text"
