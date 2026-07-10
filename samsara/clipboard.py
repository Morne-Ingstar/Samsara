"""
Samsara Clipboard Module

Centralized clipboard operations with proper Windows API type handling for 64-bit systems.
Provides save/restore functionality to preserve user's clipboard during paste operations.

Format selection (see tools/clipboard_probe.py for the empirical investigation
behind this): only formats that are BOTH (a) genuine GlobalAlloc memory blocks
(safe to GlobalSize/GlobalLock) and (b) stable/non-redundant are snapshotted.

CF_BITMAP is a GDI HBITMAP handle, not memory -- GlobalLock on it is unsafe
(can corrupt the heap) and was already correctly excluded. CF_DIB, however,
IS a genuine GlobalAlloc memory block (a BITMAPINFOHEADER + pixel bytes) and
was being excluded too by an allowlist that only covered text formats -- this
was the actual bug: image data was silently dropped at the very first
enumeration pass, before any handle-type or synthesis concern even applied.
CF_DIBV5 and CF_METAFILEPICT are deliberately left OFF the snapshot list even
though they're technically memory-safe: Windows auto-synthesizes CF_BITMAP/
CF_DIBV5 from CF_DIB (and vice versa) on demand, so saving just CF_DIB and
letting Windows resynthesize the others on restore avoids juggling multiple
redundant image representations that could end up inconsistent.
"""

import ctypes
import sys
import threading
import time
from typing import Dict, Optional

from samsara.constants import CLIPBOARD_PASTE_DELAY, CLIPBOARD_RESTORE_DELAY
from samsara.log import get_logger

logger = get_logger(__name__)

# Global lock to prevent concurrent clipboard operations
clipboard_lock = threading.Lock()

# Try to import pyperclip for cross-platform fallback
try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    pyperclip = None
    HAS_PYPERCLIP = False

# Standard clipboard format IDs relevant to this module (Windows winuser.h).
CF_TEXT = 1
CF_BITMAP = 2
CF_OEMTEXT = 7
CF_DIB = 8
CF_UNICODETEXT = 13
CF_LOCALE = 16
CF_DIBV5 = 17

# Formats that are genuine GlobalAlloc memory blocks AND worth snapshotting.
# CF_DIB is the one image format here -- see module docstring for why
# CF_BITMAP/CF_DIBV5 are deliberately excluded despite being "image formats".
SAFE_FORMATS = {
    CF_TEXT,
    CF_OEMTEXT,
    CF_UNICODETEXT,
    CF_LOCALE,
    CF_DIB,
}

_MAX_FORMAT_BYTES = 100 * 1024 * 1024  # skip anything implausibly large
_OPEN_CLIPBOARD_MAX_RETRIES = 15
_OPEN_CLIPBOARD_INITIAL_DELAY = 0.02
_OPEN_CLIPBOARD_MAX_DELAY = 0.1


def is_snapshot_eligible_format(fmt: int) -> bool:
    """True if `fmt` is a format save_clipboard() will attempt to snapshot.

    Pure/no I/O -- directly unit-testable. A format is eligible when it's
    in SAFE_FORMATS (known-safe memory-block formats, including CF_DIB but
    NOT CF_BITMAP/CF_DIBV5 -- see module docstring) or is a registered
    format (id >= 0xC000, conventionally GlobalAlloc memory too).
    """
    return fmt in SAFE_FORMATS or fmt >= 0xC000


def is_nonempty_payload(raw: "bytes | None") -> bool:
    """True if `raw` is non-empty bytes -- the single predicate that
    decides whether a format counts as "saved"/"restored". Centralized so
    the debug line and the actual save/restore loops can never disagree
    about what counts as a real, non-empty capture."""
    return bool(raw)


def _log_error(msg, exc=None):
    """Clipboard failures are never fatal to dictation -- log at WARNING,
    never raise, always let the caller proceed with the paste."""
    if exc:
        logger.warning(f"[CLIPBOARD] {msg}: {exc}")
    else:
        logger.warning(f"[CLIPBOARD] {msg}")


def _setup_win32_api():
    """Set up Windows API function signatures for proper 64-bit handling."""
    if sys.platform != 'win32':
        return None, None

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Define proper argument and return types for 64-bit Windows
    # HANDLE is a pointer-sized type (64-bit on 64-bit Windows)
    HANDLE = ctypes.c_void_p
    HWND = ctypes.c_void_p
    UINT = ctypes.c_uint
    SIZE_T = ctypes.c_size_t
    LPVOID = ctypes.c_void_p
    BOOL = ctypes.c_int

    # OpenClipboard
    user32.OpenClipboard.argtypes = [HWND]
    user32.OpenClipboard.restype = BOOL

    # CloseClipboard
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = BOOL

    # EmptyClipboard
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = BOOL

    # EnumClipboardFormats
    user32.EnumClipboardFormats.argtypes = [UINT]
    user32.EnumClipboardFormats.restype = UINT

    # GetClipboardData
    user32.GetClipboardData.argtypes = [UINT]
    user32.GetClipboardData.restype = HANDLE

    # SetClipboardData
    user32.SetClipboardData.argtypes = [UINT, HANDLE]
    user32.SetClipboardData.restype = HANDLE

    # GlobalSize
    kernel32.GlobalSize.argtypes = [HANDLE]
    kernel32.GlobalSize.restype = SIZE_T

    # GlobalLock
    kernel32.GlobalLock.argtypes = [HANDLE]
    kernel32.GlobalLock.restype = LPVOID

    # GlobalUnlock
    kernel32.GlobalUnlock.argtypes = [HANDLE]
    kernel32.GlobalUnlock.restype = BOOL

    # GlobalAlloc
    kernel32.GlobalAlloc.argtypes = [UINT, SIZE_T]
    kernel32.GlobalAlloc.restype = HANDLE

    # GlobalFree
    kernel32.GlobalFree.argtypes = [HANDLE]
    kernel32.GlobalFree.restype = HANDLE

    return user32, kernel32


# Set up API on module load
_user32, _kernel32 = _setup_win32_api()


def _open_clipboard_with_retry(
    max_retries: int = _OPEN_CLIPBOARD_MAX_RETRIES,
    initial_delay: float = _OPEN_CLIPBOARD_INITIAL_DELAY,
    max_delay: float = _OPEN_CLIPBOARD_MAX_DELAY,
) -> bool:
    """OpenClipboard with retry -- the clipboard is contended (another app,
    or Windows itself briefly after a copy/paste) and OpenClipboard fails
    transiently. Shared by save_clipboard() and restore_clipboard() so
    there's exactly one retry policy, not two copies of it."""
    delay = initial_delay
    for _ in range(max_retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(delay)
        delay = min(delay * 1.5, max_delay)
    return False


def save_clipboard() -> Dict[int, bytes]:
    """
    Save all clipboard formats using Windows API.

    Returns:
        Dict mapping format ID to raw bytes data.
        Empty dict if clipboard is empty or on error. Never raises --
        a clipboard-save failure must never block a dictation paste.
    """
    try:
        return _save_clipboard_impl()
    except Exception as e:
        _log_error("save_clipboard failed unexpectedly", e)
        return {}


def _save_clipboard_impl() -> Dict[int, bytes]:
    if sys.platform != 'win32':
        # Fallback for non-Windows: just save text
        if HAS_PYPERCLIP:
            try:
                text = pyperclip.paste()
                if text:
                    return {'text': text.encode('utf-8')}
            except Exception as e:
                _log_error("Failed to save clipboard text fallback", e)
        return {}

    saved: Dict[int, bytes] = {}
    skipped = 0

    if not _open_clipboard_with_retry():
        _log_error("Could not open clipboard for save after retries")
        return saved

    try:
        fmt = 0
        while True:
            fmt = _user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                break

            # Skip formats that aren't memory handles, or that we
            # deliberately don't snapshot (CF_BITMAP/CF_DIBV5/etc -- see
            # module docstring).
            if not is_snapshot_eligible_format(fmt):
                skipped += 1
                continue

            try:
                # GetClipboardData forces a delayed-render provider (e.g.
                # a screenshot tool that defers rendering until requested)
                # to render now, so this always returns real data or NULL,
                # never a placeholder -- no separate "force render" step
                # needed.
                handle = _user32.GetClipboardData(fmt)
                if not handle:
                    skipped += 1
                    continue

                size = _kernel32.GlobalSize(handle)
                if size <= 0 or size > _MAX_FORMAT_BYTES:
                    skipped += 1
                    continue

                ptr = _kernel32.GlobalLock(handle)
                if not ptr:
                    skipped += 1
                    continue
                try:
                    raw = ctypes.string_at(ptr, size)
                finally:
                    _kernel32.GlobalUnlock(handle)

                # Never count a format as saved unless its bytes are
                # actually non-empty -- an empty "success" is a lie.
                if is_nonempty_payload(raw):
                    saved[fmt] = raw
                else:
                    skipped += 1
            except Exception as e:
                skipped += 1
                _log_error(f"Could not read clipboard format {fmt}", e)
    finally:
        _user32.CloseClipboard()

    if saved or skipped:
        print(f"[DEBUG] Clipboard saved: {len(saved)} format(s), skipped {skipped} format(s)")

    return saved


def restore_clipboard(saved: Dict[int, bytes]) -> bool:
    """
    Restore clipboard formats previously saved by save_clipboard().

    Args:
        saved: Dict from save_clipboard()

    Returns:
        True if restoration was successful. Never raises -- a
        clipboard-restore failure must never block a dictation paste.
    """
    try:
        return _restore_clipboard_impl(saved)
    except Exception as e:
        _log_error("restore_clipboard failed unexpectedly", e)
        return False


def _restore_clipboard_impl(saved: Dict[int, bytes]) -> bool:
    if not saved:
        return True  # Nothing to restore is success

    if sys.platform != 'win32':
        # Fallback for non-Windows
        text = saved.get('text')
        if text and HAS_PYPERCLIP:
            try:
                pyperclip.copy(text.decode('utf-8'))
                return True
            except Exception as e:
                _log_error("Failed to restore clipboard text fallback", e)
        return False

    GMEM_MOVEABLE = 0x0002

    if not _open_clipboard_with_retry():
        _log_error("Could not open clipboard for restore after retries")
        return False

    restored_count = 0
    skipped = 0
    try:
        _user32.EmptyClipboard()

        for fmt, raw in saved.items():
            # Never attempt to restore an empty payload -- a 0-byte
            # "restored" format is a lie, and GlobalAlloc(0) is asking for
            # trouble for no benefit.
            if not is_nonempty_payload(raw):
                skipped += 1
                continue
            try:
                h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
                if not h:
                    skipped += 1
                    continue

                ptr = _kernel32.GlobalLock(h)
                if ptr:
                    try:
                        ctypes.memmove(ptr, raw, len(raw))
                    finally:
                        _kernel32.GlobalUnlock(h)

                    if _user32.SetClipboardData(fmt, h):
                        restored_count += 1
                    else:
                        # SetClipboardData failed, we need to free the memory
                        _kernel32.GlobalFree(h)
                        skipped += 1
                else:
                    _kernel32.GlobalFree(h)
                    skipped += 1
            except Exception as e:
                skipped += 1
                _log_error(f"Failed to restore clipboard format {fmt}", e)
    finally:
        _user32.CloseClipboard()

    print(f"[DEBUG] Clipboard restored: {restored_count}/{len(saved)} format(s)"
          f"{f', skipped {skipped}' if skipped else ''}")

    return restored_count > 0 or len(saved) == 0


def copy_text(text: str) -> bool:
    """Copy `text` to the clipboard, replacing its current contents (no
    save/restore -- this is for user-facing "copy result" actions, not a
    paste-then-restore dictation flow). Never raises; returns False on
    failure so callers can fall back to their own status messaging."""
    if not HAS_PYPERCLIP:
        _log_error("pyperclip not available")
        return False
    try:
        pyperclip.copy(text)
        return True
    except Exception as e:
        _log_error("copy_text failed", e)
        return False


def paste_with_preservation(text: str, paste_delay: float = CLIPBOARD_PASTE_DELAY, restore_delay: float = CLIPBOARD_RESTORE_DELAY) -> bool:
    """
    Paste text via clipboard while preserving original clipboard content.

    This is the main entry point for dictation paste operations.

    Args:
        text: Text to paste
        paste_delay: Delay after copying before pasting (seconds)
        restore_delay: Delay after pasting before restoring clipboard (seconds)

    Returns:
        True if paste was successful
    """
    if not HAS_PYPERCLIP:
        _log_error("pyperclip not available")
        return False

    try:
        import pyautogui
    except ImportError:
        _log_error("pyautogui not available")
        return False

    with clipboard_lock:
        # save_clipboard() never raises (see above), but the paste itself
        # must proceed even if something upstream of that guarantee still
        # goes wrong -- clipboard preservation must never block dictation.
        try:
            saved = save_clipboard()
        except Exception as e:
            _log_error("Unexpected error saving clipboard before paste", e)
            saved = {}

        try:
            # Copy the text to paste
            pyperclip.copy(text)

            # Small delay to ensure clipboard is ready
            time.sleep(paste_delay)

            # Simulate Ctrl+V
            pyautogui.hotkey('ctrl', 'v')

            # Wait for the target application to read the clipboard
            # This is necessary because some apps read clipboard asynchronously
            time.sleep(restore_delay)

            return True

        except Exception as e:
            _log_error("Paste failed", e)
            return False

        finally:
            # Always restore clipboard, even if paste failed
            if saved:
                restore_clipboard(saved)


# Convenience function for testing
def test_clipboard_preservation() -> bool:
    """
    Test that clipboard preservation works correctly.

    Returns:
        True if test passed
    """
    if not HAS_PYPERCLIP:
        print("pyperclip not available for testing")
        return False

    original = "ORIGINAL_TEST_CONTENT_" + str(time.time())
    paste_text = "PASTE_TEXT_" + str(time.time())

    # Set original content
    pyperclip.copy(original)
    time.sleep(0.1)

    # Save
    saved = save_clipboard()
    if not saved:
        print("FAILED: Could not save clipboard")
        return False

    # Overwrite
    pyperclip.copy(paste_text)
    time.sleep(0.1)

    # Verify overwrite
    if pyperclip.paste() != paste_text:
        print("FAILED: Clipboard overwrite didn't work")
        return False

    # Restore
    if not restore_clipboard(saved):
        print("FAILED: Restore returned False")
        return False

    time.sleep(0.1)

    # Verify restoration
    restored = pyperclip.paste()
    if restored == original:
        print(f"SUCCESS: Clipboard preserved correctly")
        return True
    else:
        print(f"FAILED: Expected '{original}', got '{restored}'")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("Clipboard Preservation Test")
    print("=" * 60)
    success = test_clipboard_preservation()
    sys.exit(0 if success else 1)
