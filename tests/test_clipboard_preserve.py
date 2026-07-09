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
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.clipboard import (
    CF_TEXT, CF_BITMAP, CF_OEMTEXT, CF_DIB, CF_UNICODETEXT, CF_LOCALE, CF_DIBV5,
    SAFE_FORMATS,
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

    def test_cf_dibv5_is_not_eligible(self):
        """Technically memory-safe, but deliberately excluded: Windows
        resynthesizes CF_DIBV5 from a saved CF_DIB on restore, so saving
        it too would just be redundant/possibly-inconsistent."""
        assert is_snapshot_eligible_format(CF_DIBV5) is False

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
        for fmt in (3, 4, 5, 6, 9, 10, 11, 12, 14, 15):
            assert is_snapshot_eligible_format(fmt) is False, f"format {fmt} should not be eligible"

    def test_safe_formats_set_matches_documented_contents(self):
        assert SAFE_FORMATS == {CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE, CF_DIB}


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


@pytest.mark.skipif(not _clipboard_available(), reason="clipboard not available in this environment")
class TestRealClipboardRoundTrip:
    def test_text_and_dib_survive_save_clobber_restore(self):
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

    def test_text_only_clipboard_still_round_trips(self):
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
