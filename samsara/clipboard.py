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

CF_DIBV5 (a GlobalAlloc BITMAPV5HEADER + pixel bytes -- same handle shape as
CF_DIB) IS also snapshotted: unlike CF_BITMAP, which Windows freely
resynthesizes from CF_DIB, DIBV5 carries per-pixel alpha that a plain CF_DIB
cannot represent, so resynthesizing CF_DIBV5 from a saved CF_DIB on restore
would silently drop that alpha channel. CF_METAFILEPICT is still deliberately
left OFF the snapshot list (not memory-safe to GlobalLock the way CF_DIB/
CF_DIBV5 are).

CF_HDROP (a GlobalAlloc DROPFILES struct + a list of file paths -- what's on
the clipboard after an Explorer "Copy" of one or more files) is snapshotted
for the same reason CF_DIB originally should have been: it's a genuine
memory block the old text-only allowlist never covered. Restoring it is what
lets a paste-then-restore dictation flow hand the user back a still-pastable
set of copied files afterward.
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
CF_HDROP = 15
CF_UNICODETEXT = 13
CF_LOCALE = 16
CF_DIBV5 = 17

# Formats that are genuine GlobalAlloc memory blocks AND worth snapshotting.
# CF_DIB/CF_DIBV5 are the image formats, CF_HDROP is the file-drop format --
# see module docstring for why CF_BITMAP is still excluded despite being an
# "image format" too (GDI handle, not memory).
SAFE_FORMATS = {
    CF_TEXT,
    CF_OEMTEXT,
    CF_UNICODETEXT,
    CF_LOCALE,
    CF_DIB,
    CF_HDROP,
    CF_DIBV5,
}

_MAX_FORMAT_BYTES = 100 * 1024 * 1024  # skip anything implausibly large
_OPEN_CLIPBOARD_MAX_RETRIES = 15
_OPEN_CLIPBOARD_INITIAL_DELAY = 0.02
_OPEN_CLIPBOARD_MAX_DELAY = 0.1


def is_snapshot_eligible_format(fmt: int) -> bool:
    """True if `fmt` is a format save_clipboard() will attempt to snapshot.

    Pure/no I/O -- directly unit-testable. A format is eligible when it's
    in SAFE_FORMATS (known-safe memory-block formats, including CF_DIB,
    CF_DIBV5, and CF_HDROP but NOT CF_BITMAP/CF_METAFILEPICT -- see module
    docstring) or is a registered format (id >= 0xC000, conventionally
    GlobalAlloc memory too).
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

    # GetClipboardSequenceNumber -- increments on every clipboard content
    # change; used to detect a clipboard change during the paste window.
    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = ctypes.c_uint32

    # GetClipboardOwner / IsHungAppWindow -- used to skip snapshotting when
    # a delayed-render provider is hung (would block GetClipboardData
    # indefinitely).
    user32.GetClipboardOwner.argtypes = []
    user32.GetClipboardOwner.restype = HWND

    user32.IsHungAppWindow.argtypes = [HWND]
    user32.IsHungAppWindow.restype = BOOL

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


def get_clipboard_sequence_number() -> "Optional[int]":
    """Current Windows clipboard sequence number (increments on every
    clipboard content change), or None on non-Windows / if unavailable.
    Never raises -- callers treat None as "can't tell, skip the check"."""
    if sys.platform != 'win32' or _user32 is None:
        return None
    try:
        return _user32.GetClipboardSequenceNumber()
    except Exception:
        return None


class ClipboardSnapshot(dict):
    """What save_clipboard() returns: a format-id -> raw-bytes dict, exactly
    like the plain dict this module has always returned, plus an optional
    `seq` -- the clipboard sequence number captured right after Samsara's
    own dictated-text copy (see paste_with_preservation). restore_clipboard()
    uses `seq`, when set, to detect a clipboard change during the paste
    window and abort rather than clobber whatever the user has now. A plain
    dict (or a ClipboardSnapshot with `seq` left at its default None) skips
    that check entirely -- restore behaves exactly as before this existed.
    """

    def __init__(self, *args, seq: "Optional[int]" = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.seq = seq


def save_clipboard() -> "ClipboardSnapshot":
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
        return ClipboardSnapshot()


def _save_clipboard_impl() -> "ClipboardSnapshot":
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
        # A hung delayed-render provider (an app that registered a format
        # but defers actually producing the data until GetClipboardData is
        # called) would block GetClipboardData indefinitely below. Skip the
        # snapshot entirely rather than risk hanging dictation on it -- an
        # empty save just makes restore_clipboard() a no-op later; the
        # dictated text still lands via the paste that follows.
        owner = _user32.GetClipboardOwner()
        if owner and _user32.IsHungAppWindow(owner):
            logger.info("[CLIP] clipboard owner hung, skipping snapshot")
            return saved

        fmt = 0
        while True:
            fmt = _user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                break

            # Skip formats that aren't memory handles, or that we
            # deliberately don't snapshot (CF_BITMAP/CF_METAFILEPICT/etc --
            # see module docstring).
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

    # Legacy text dedup: if CF_UNICODETEXT was captured non-empty, Windows
    # synthesizes CF_TEXT/CF_OEMTEXT/CF_LOCALE from it automatically on
    # restore (the mirror image of the CF_DIB -> CF_BITMAP synthesis this
    # module already relies on -- see module docstring). Dropping the
    # redundant legacy copies here shrinks the restore window (fewer
    # formats for restore's atomic prepare phase to allocate) without
    # losing anything actually restorable.
    if is_nonempty_payload(saved.get(CF_UNICODETEXT)):
        for legacy_fmt in (CF_TEXT, CF_OEMTEXT, CF_LOCALE):
            if legacy_fmt in saved:
                del saved[legacy_fmt]
                skipped += 1

    if saved or skipped:
        print(f"[DEBUG] Clipboard saved: {len(saved)} format(s), skipped {skipped} format(s)")

    return ClipboardSnapshot(saved)


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

    # Sequence-number guard: `saved.seq` (when set -- see ClipboardSnapshot)
    # is the clipboard sequence number captured right after Samsara's own
    # dictated-text copy. If it's different now, someone/something changed
    # the clipboard during the paste window -- restoring our stale snapshot
    # over that would clobber a real user action, so abort before ever
    # touching the clipboard. A plain dict (no `seq` attribute, e.g. from a
    # caller other than paste_with_preservation) skips this check entirely.
    expected_seq = getattr(saved, 'seq', None)
    if expected_seq is not None:
        current_seq = get_clipboard_sequence_number()
        if current_seq is not None and current_seq != expected_seq:
            logger.info("[CLIP] clipboard changed during paste window, skipping restore to preserve user copy")
            return True

    GMEM_MOVEABLE = 0x0002

    # Atomic restore, phase 1: allocate+lock+memcpy EVERY saved format
    # before touching the live clipboard at all. If any one of them fails,
    # free everything prepared so far and bail without ever calling
    # OpenClipboard/EmptyClipboard -- leaving the clipboard's current
    # (prior) content untouched beats an EmptyClipboard() followed by only
    # a partial restore.
    prepared = []
    skipped = 0
    for fmt, raw in saved.items():
        # Never attempt to restore an empty payload -- a 0-byte "restored"
        # format is a lie, and GlobalAlloc(0) is asking for trouble for no
        # benefit.
        if not is_nonempty_payload(raw):
            skipped += 1
            continue
        try:
            h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
            if not h:
                raise OSError(f"GlobalAlloc({len(raw)} bytes) returned NULL")

            ptr = _kernel32.GlobalLock(h)
            if not ptr:
                _kernel32.GlobalFree(h)
                raise OSError("GlobalLock returned NULL")
            try:
                ctypes.memmove(ptr, raw, len(raw))
            finally:
                _kernel32.GlobalUnlock(h)

            prepared.append((fmt, h))
        except Exception as e:
            _log_error(f"Failed to prepare clipboard format {fmt} for restore -- aborting restore atomically", e)
            for _fmt, handle in prepared:
                _kernel32.GlobalFree(handle)
            return False

    if not prepared:
        return True  # everything saved was empty/unrestorable -- nothing to do, not a failure

    # Atomic restore, phase 2: every handle exists now -- only past this
    # point do we touch the live clipboard.
    if not _open_clipboard_with_retry():
        _log_error("Could not open clipboard for restore after retries")
        for _fmt, handle in prepared:
            _kernel32.GlobalFree(handle)
        return False

    restored_count = 0
    try:
        _user32.EmptyClipboard()

        for fmt, h in prepared:
            if _user32.SetClipboardData(fmt, h):
                restored_count += 1
            else:
                # SetClipboardData failed -- free that one handle and keep
                # going with the rest (unlike phase 1, a single Set failure
                # here doesn't invalidate the handles already handed off).
                _kernel32.GlobalFree(h)
                skipped += 1
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

            # Capture the clipboard sequence number right after our own
            # copy -- if it's different by the time restore_clipboard()
            # runs, something else changed the clipboard during the paste
            # window and blindly restoring would clobber that, not just
            # put back the original content restore is meant to protect.
            if isinstance(saved, ClipboardSnapshot):
                saved.seq = get_clipboard_sequence_number()

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
