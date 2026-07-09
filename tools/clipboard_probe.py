"""Throwaway probe for the clipboard-preservation image-loss bug.

Puts a real image on the clipboard programmatically (via PIL + win32clipboard,
as CF_DIB -- the standard technique, and what Snipping Tool/Win+Shift+S
places on the clipboard among other formats), then:

1. Enumerates ALL clipboard formats present (bypassing any allowlist) to see
   what's really there.
2. Runs the EXISTING samsara.clipboard.save_clipboard() and reports exactly
   which format IDs it captured and their byte sizes.
3. Directly probes CF_DIB (8) via GetClipboardData/GlobalSize/GlobalLock,
   independent of save_clipboard(), to check whether it's a safe,
   lockable memory handle (validating whether widening the allowlist to
   include it would be safe).
4. Overwrites the clipboard with plain text (simulating a dictation paste).
5. Runs the EXISTING restore_clipboard().
6. Checks whether CF_DIB is retrievable post-restore, and whether its bytes
   match the original.

Usage:
    F:\\envs\\sami\\python.exe tools\\clipboard_probe.py
"""
from __future__ import annotations

import ctypes
import io
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image
import win32clipboard

from samsara.clipboard import save_clipboard, restore_clipboard, _user32, _kernel32

_user32.GetClipboardFormatNameW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetClipboardFormatNameW.restype = ctypes.c_int

CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_DIBV5 = 17

_KNOWN_FORMAT_NAMES = {
    1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 4: "CF_SYLK",
    5: "CF_DIF", 6: "CF_TIFF", 7: "CF_OEMTEXT", 8: "CF_DIB", 9: "CF_PALETTE",
    10: "CF_PENDATA", 11: "CF_RIFF", 12: "CF_WAVE", 13: "CF_UNICODETEXT",
    14: "CF_ENHMETAFILE", 15: "CF_HDROP", 16: "CF_LOCALE", 17: "CF_DIBV5",
}


def _format_name(fmt: int) -> str:
    if fmt in _KNOWN_FORMAT_NAMES:
        return _KNOWN_FORMAT_NAMES[fmt]
    if fmt >= 0xC000:
        try:
            buf = ctypes.create_unicode_buffer(256)
            n = _user32.GetClipboardFormatNameW(fmt, buf, 256)
            if n:
                return f"registered:{buf.value}"
        except Exception:
            pass
        return f"registered:0x{fmt:X}"
    return f"unknown:{fmt}"


def _make_test_dib() -> bytes:
    """A real (small) generated image, encoded as a raw DIB (BITMAPINFO
    header + pixel bytes, no BITMAPFILEHEADER) -- the standard technique for
    CF_DIB clipboard payloads."""
    img = Image.new("RGB", (64, 48), color=(220, 40, 40))
    for x in range(64):
        for y in range(48):
            if (x // 8 + y // 8) % 2 == 0:
                img.putpixel((x, y), (40, 40, 220))
    buf = io.BytesIO()
    img.save(buf, "BMP")
    bmp_bytes = buf.getvalue()
    # Strip the 14-byte BITMAPFILEHEADER -- CF_DIB is everything after it.
    return bmp_bytes[14:]


def _set_clipboard_dib(dib_bytes: bytes) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib_bytes)
    finally:
        win32clipboard.CloseClipboard()


def _enumerate_all_formats() -> list:
    formats = []
    if not _user32.OpenClipboard(None):
        print("  !! could not open clipboard to enumerate")
        return formats
    try:
        fmt = 0
        while True:
            fmt = _user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            formats.append(fmt)
    finally:
        _user32.CloseClipboard()
    return formats


def _probe_format_directly(fmt: int):
    """GetClipboardData/GlobalSize/GlobalLock on `fmt`, independent of
    save_clipboard()'s allowlist. Returns (size, first_bytes) or None."""
    if not _user32.OpenClipboard(None):
        print(f"  !! could not open clipboard to probe format {fmt}")
        return None
    try:
        handle = _user32.GetClipboardData(fmt)
        if not handle:
            print(f"  GetClipboardData({_format_name(fmt)}) -> NULL handle")
            return None
        size = _kernel32.GlobalSize(handle)
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            print(f"  GlobalLock failed for {_format_name(fmt)} (handle non-null, size={size})")
            return None
        try:
            raw = ctypes.string_at(ptr, min(size, 64))
            return size, raw
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def main() -> int:
    print("=" * 70)
    print("Clipboard preservation probe")
    print("=" * 70)

    test_dib = _make_test_dib()
    print(f"\n[1] Generated test image DIB: {len(test_dib)} bytes")

    _set_clipboard_dib(test_dib)
    print("[1] Placed on clipboard as CF_DIB.")

    print("\n[2] ALL formats currently on clipboard (raw enumeration):")
    all_formats = _enumerate_all_formats()
    for fmt in all_formats:
        print(f"    {fmt:6d}  {_format_name(fmt)}")
    if not all_formats:
        print("    (none found -- probe cannot continue meaningfully)")

    print(f"\n[3] Direct probe of CF_DIB ({CF_DIB}) BEFORE save_clipboard():")
    direct = _probe_format_directly(CF_DIB)
    if direct:
        size, head = direct
        print(f"    size={size} bytes, first bytes={head[:16].hex()}")
        print(f"    matches original DIB header: {head[:16] == test_dib[:16]}")
    else:
        print("    CF_DIB not directly retrievable -- unexpected, investigate.")

    print("\n[4] Running samsara.clipboard.save_clipboard() (the EXISTING code)...")
    saved = save_clipboard()
    print(f"    save_clipboard() reports {len(saved)} format(s) saved:")
    for fmt, raw in saved.items():
        print(f"      {fmt:6d}  {_format_name(fmt):20s}  {len(raw)} bytes")
    if CF_DIB in saved:
        print(f"    CF_DIB WAS captured by save_clipboard(); size={len(saved[CF_DIB])}")
    else:
        print("    *** CF_DIB was NOT captured by save_clipboard() ***")
    if CF_BITMAP in saved:
        print(f"    CF_BITMAP WAS captured by save_clipboard(); size={len(saved[CF_BITMAP])}"
              " (should never happen -- CF_BITMAP is an HBITMAP, not memory)")

    print("\n[5] Overwriting clipboard with plain text (simulating dictation paste)...")
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText("dictated sentence goes here", win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()

    print("[6] Running samsara.clipboard.restore_clipboard() (the EXISTING code)...")
    ok = restore_clipboard(saved)
    print(f"    restore_clipboard() returned {ok}")

    print(f"\n[7] Direct probe of CF_DIB ({CF_DIB}) AFTER restore:")
    direct_after = _probe_format_directly(CF_DIB)
    if direct_after:
        size, head = direct_after
        print(f"    size={size} bytes, first bytes={head[:16].hex()}")
        print(f"    matches original DIB header: {head[:16] == test_dib[:16]}")
        print("    RESULT: image SURVIVED the save/clobber/restore round-trip.")
    else:
        print("    *** RESULT: image LOST -- CF_DIB not retrievable after restore. ***")
        print("    This reproduces the reported symptom despite save_clipboard()")
        print("    claiming success for the formats it DID capture.")

    print("\n[8] ALL formats on clipboard AFTER restore (raw enumeration):")
    after_formats = _enumerate_all_formats()
    for fmt in after_formats:
        print(f"    {fmt:6d}  {_format_name(fmt)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
