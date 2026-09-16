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

Completeness and the restore contract (queue 49, ARC audit6): a snapshot
records, by name and reason, any content it could not capture that Windows
will not re-create from what it did capture (ClipboardSnapshot.lost --
over the 100 MB cap, a non-memory format such as CF_ENHMETAFILE, an owner
that does not respond). That is logged at save and again at restore, and
restore_clipboard() returns True only for an exact restore. Restore does NOT
refuse an incomplete snapshot: by the time it runs, Samsara's own text has
already replaced the user's clipboard, so refusing would leave them with that
text and none of their content. The sequence-number guard is checked before
the restore is prepared AND again while the clipboard is held open,
immediately before EmptyClipboard, so a copy made at any point in the paste
window is never overwritten.
"""

import ctypes
import sys
import threading
import time
from typing import Callable, Dict, Optional

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
CF_METAFILEPICT = 3
CF_PALETTE = 9
CF_ENHMETAFILE = 14

_FORMAT_NAMES = {
    1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 4: "CF_SYLK", 5: "CF_DIF",
    6: "CF_TIFF", 7: "CF_OEMTEXT", 8: "CF_DIB", 9: "CF_PALETTE", 10: "CF_PENDATA",
    11: "CF_RIFF", 12: "CF_WAVE", 13: "CF_UNICODETEXT", 14: "CF_ENHMETAFILE",
    15: "CF_HDROP", 16: "CF_LOCALE", 17: "CF_DIBV5",
}

# Formats Windows re-creates by itself from another format when that other
# format is on the clipboard (the documented synthesized-format table). A
# format that was not snapshotted is NOT lost if one of its sources was.
_SYNTHESIZED_FROM = {
    CF_TEXT: {CF_UNICODETEXT, CF_OEMTEXT},
    CF_OEMTEXT: {CF_UNICODETEXT, CF_TEXT},
    CF_UNICODETEXT: {CF_TEXT, CF_OEMTEXT},
    CF_LOCALE: {CF_UNICODETEXT, CF_TEXT, CF_OEMTEXT},
    CF_BITMAP: {CF_DIB, CF_DIBV5},
    CF_DIB: {CF_DIBV5, CF_BITMAP},
    CF_DIBV5: {CF_DIB, CF_BITMAP},
    CF_PALETTE: {CF_DIB, CF_DIBV5},
    CF_METAFILEPICT: {CF_ENHMETAFILE},
    CF_ENHMETAFILE: {CF_METAFILEPICT},
}

#: A clipboard owner that does not answer a WM_NULL within this long is
#: treated as unable to render: the snapshot is skipped (and reported as
#: incomplete) rather than blocking dictation inside GetClipboardData.
_OWNER_PING_TIMEOUT_MS = 250
#: A snapshot slower than this is logged as a warning (delayed rendering).
_SLOW_SNAPSHOT_S = 0.5

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

    # SendMessageTimeoutW(WM_NULL) -- a responsiveness ping for the clipboard
    # owner, much stricter than IsHungAppWindow's ~5 s hung threshold.
    user32.SendMessageTimeoutW.argtypes = [HWND, UINT, ctypes.c_size_t, ctypes.c_ssize_t, UINT, UINT,
                                           ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

    # CountClipboardFormats -- works without opening the clipboard.
    user32.CountClipboardFormats.argtypes = []
    user32.CountClipboardFormats.restype = ctypes.c_int

    # GetClipboardFormatNameW -- names registered formats in log lines.
    user32.GetClipboardFormatNameW.argtypes = [UINT, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClipboardFormatNameW.restype = ctypes.c_int

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


def format_label(fmt: int) -> str:
    """Human-readable clipboard format name for log lines. Never raises."""
    if fmt in _FORMAT_NAMES:
        return _FORMAT_NAMES[fmt]
    if fmt >= 0xC000 and _user32 is not None:
        try:
            buf = ctypes.create_unicode_buffer(128)
            if _user32.GetClipboardFormatNameW(fmt, buf, 128):
                return f"{buf.value!r} ({fmt})"
        except Exception:
            pass
    return f"format {fmt}"


class ClipboardSnapshot(dict):
    """What save_clipboard() returns: a format-id -> raw-bytes dict, exactly
    like the plain dict this module has always returned, plus:

    `seq` -- the clipboard sequence number captured right after Samsara's
    own dictated-text copy (see paste_with_preservation). restore_clipboard()
    uses `seq`, when set, to detect a clipboard change during the paste
    window and abort rather than clobber whatever the user has now. A plain
    dict (or a ClipboardSnapshot with `seq` left at its default None) skips
    that check entirely -- restore behaves exactly as before this existed.

    `lost` -- {format id: reason} for content that was on the clipboard but
    could NOT be snapshotted and that Windows will not re-create from what
    was (see _SYNTHESIZED_FROM). Format id 0 stands for "the whole
    clipboard" (owner not responding, clipboard busy). `complete` is True
    when nothing was lost: only then can a restore put back exactly what the
    user had.
    """

    def __init__(self, *args, seq: "Optional[int]" = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.seq = seq
        self.lost: Dict[int, str] = {}

    @property
    def complete(self) -> bool:
        return not self.lost

    def describe_lost(self) -> str:
        return ", ".join(
            ("the whole clipboard" if fmt == 0 else format_label(fmt)) + f" ({reason})"
            for fmt, reason in sorted(self.lost.items())
        )


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

    saved = ClipboardSnapshot()
    skipped = 0
    not_saved: Dict[int, str] = {}   # candidate losses; synthesis-filtered at the end
    started = time.monotonic()

    if not _open_clipboard_with_retry():
        _log_error("Could not open clipboard for save after retries")
        _mark_whole_clipboard_lost(saved, "clipboard busy")
        return saved

    try:
        # A hung delayed-render provider (an app that registered a format
        # but defers actually producing the data until GetClipboardData is
        # called) would block GetClipboardData indefinitely below. Skip the
        # snapshot entirely rather than risk hanging dictation on it -- an
        # empty save just makes restore_clipboard() a no-op later; the
        # dictated text still lands via the paste that follows.
        # IsHungAppWindow only trips after ~5 s of unresponsiveness, so the
        # owner must also answer a WM_NULL ping within _OWNER_PING_TIMEOUT_MS.
        # Neither can interrupt a render that has already STARTED:
        # GetClipboardData has no timeout (see reports/49).
        owner = _user32.GetClipboardOwner()
        if owner and (_user32.IsHungAppWindow(owner) or not _owner_answers_ping(owner)):
            logger.info("[CLIP] clipboard owner hung, skipping snapshot")
            _mark_whole_clipboard_lost(saved, "clipboard owner not responding")
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
                not_saved[fmt] = "not a format that can be snapshotted"
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
                    not_saved[fmt] = "owner provided no data"
                    continue

                size = _kernel32.GlobalSize(handle)
                if size <= 0 or size > _MAX_FORMAT_BYTES:
                    skipped += 1
                    not_saved[fmt] = "over the 100 MB cap" if size > _MAX_FORMAT_BYTES else "empty"
                    continue

                ptr = _kernel32.GlobalLock(handle)
                if not ptr:
                    skipped += 1
                    not_saved[fmt] = "could not lock its memory"
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
                    not_saved[fmt] = "empty"
            except Exception as e:
                skipped += 1
                not_saved[fmt] = "read error"
                _log_error(f"Could not read clipboard format {fmt}", e)
    finally:
        _user32.CloseClipboard()

    elapsed = time.monotonic() - started
    if elapsed > _SLOW_SNAPSHOT_S:
        _log_error(f"clipboard snapshot took {elapsed:.1f} s -- the clipboard owner was slow to render its data")

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

    # Completeness (49): a skipped format is only a real loss if Windows will
    # not re-create it from a format that WAS saved, and an empty handle is
    # not content.
    for fmt, reason in not_saved.items():
        if reason == "empty" or _SYNTHESIZED_FROM.get(fmt, set()) & set(saved):
            continue
        saved.lost[fmt] = reason
    if saved.lost:
        _log_error("clipboard snapshot is incomplete -- these cannot be put back after a paste: "
                   + saved.describe_lost())

    if saved or skipped:
        print(f"[DEBUG] Clipboard saved: {len(saved)} format(s), skipped {skipped} format(s)")

    return saved


def _mark_whole_clipboard_lost(snapshot: "ClipboardSnapshot", reason: str) -> None:
    """Record that nothing could be snapshotted although the clipboard has
    content. Never raises."""
    try:
        if _user32.CountClipboardFormats() > 0:
            snapshot.lost[0] = reason
            _log_error(f"clipboard content could not be snapshotted ({reason}); "
                       "a paste will not be able to put it back")
    except Exception:
        pass


def _owner_answers_ping(owner) -> bool:
    """True if the clipboard owner window answers WM_NULL within
    _OWNER_PING_TIMEOUT_MS (SMTO_ABORTIFHUNG). Never raises; an API failure
    counts as answering, so a missing API never blocks a snapshot."""
    try:
        WM_NULL, SMTO_ABORTIFHUNG = 0x0000, 0x0002
        result = ctypes.c_size_t(0)
        return bool(_user32.SendMessageTimeoutW(owner, WM_NULL, 0, 0, SMTO_ABORTIFHUNG,
                                                _OWNER_PING_TIMEOUT_MS, ctypes.byref(result)))
    except Exception:
        return True


def restore_clipboard(saved: Dict[int, bytes]) -> bool:
    """
    Restore clipboard formats previously saved by save_clipboard().

    Args:
        saved: Dict from save_clipboard()

    Returns:
        True only when the user's clipboard ends up exactly as it was: every
        saved format was put back and the snapshot was complete -- or the
        restore was deliberately skipped because the user changed the
        clipboard during the paste window. False for any failed or partial
        restore (what could not be put back is logged by name). Never
        raises -- a clipboard-restore failure must never block a dictation
        paste.
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
    guard_seq = expected_seq      # what the sequence number must still read when we empty the clipboard
    if expected_seq is not None:
        current_seq = get_clipboard_sequence_number()
        if current_seq is not None and current_seq != expected_seq:
            # Sequence changed -- but rich web editors (Chromium contenteditable)
            # routinely bump the sequence while INGESTING our paste, which is
            # not a user action. If the clipboard still holds exactly the text
            # WE placed, restoring the user's original is safe. Only a payload
            # we did not write means a genuine third-party change.
            pasted_text = getattr(saved, 'pasted_text', None)
            current_text = None
            if pasted_text is not None and HAS_PYPERCLIP:
                try:
                    current_text = pyperclip.paste()
                except Exception:
                    current_text = None
            if pasted_text is not None and current_text == pasted_text:
                logger.info(
                    "[CLIP] sequence changed but clipboard still holds our own"
                    " text (target echo) -- restoring user's original"
                )
                guard_seq = current_seq
            else:
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
    failed = []
    try:
        # Sequence guard, second look (49): the check above ran before the
        # prepare phase and before OpenClipboard's retries (up to ~1.5 s), so
        # a copy the user made in that window would still be clobbered. With
        # the clipboard now held open by us nobody else can change it, so
        # checking again immediately before EmptyClipboard leaves no window.
        if guard_seq is not None:
            now_seq = get_clipboard_sequence_number()
            if now_seq is not None and now_seq != guard_seq:
                logger.info("[CLIP] clipboard changed while the restore was being prepared, "
                            "skipping restore to preserve user copy")
                for _fmt, handle in prepared:
                    _kernel32.GlobalFree(handle)
                return True

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
                failed.append(fmt)
    finally:
        _user32.CloseClipboard()

    print(f"[DEBUG] Clipboard restored: {restored_count}/{len(saved)} format(s)"
          f"{f', skipped {skipped}' if skipped else ''}")

    if failed:
        _log_error("clipboard restore was partial -- could not put back: "
                   + ", ".join(format_label(f) for f in failed))
    lost = getattr(saved, 'lost', None) or {}
    if lost:
        # NOT refused (49, audit issue 4): by the time restore runs, Samsara's
        # own text has already replaced the user's clipboard, so refusing an
        # incomplete snapshot would leave them with that text and none of
        # their content -- strictly more loss than putting back what was
        # saved. Put it back and say, by name, what could not be.
        _log_error("clipboard restored WITHOUT content that could not be snapshotted: "
                   + saved.describe_lost())
    return not failed and not lost


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


def paste_with_preservation(
    text: str,
    paste_delay: float = CLIPBOARD_PASTE_DELAY,
    restore_delay: float = CLIPBOARD_RESTORE_DELAY,
    before_paste: Optional[Callable[[], bool]] = None,
) -> bool:
    """
    Paste text via clipboard while preserving original clipboard content.

    This is the main entry point for dictation paste operations.

    Args:
        text: Text to paste
        paste_delay: Delay after copying before pasting (seconds)
        restore_delay: Delay after pasting before restoring clipboard (seconds)
        before_paste: Optional fail-closed focus guard, evaluated immediately
            before Ctrl+V after clipboard preparation and paste delay.

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
                saved.pasted_text = text

            # Small delay to ensure clipboard is ready
            time.sleep(paste_delay)

            if before_paste is not None and not before_paste():
                logger.warning(
                    "[CLIP] Paste cancelled because the foreground target changed"
                )
                return False

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



# ---------------------------------------------------------------------------
# Typed Unicode injection (2026-07-24)
#
# Synthetic Ctrl+V into rich web editors is a documented double-execution
# hazard (the editor's keydown handler AND the native paste event can both
# fire: JupyterLab #11639, apache/hop #6438, WKWebView) and it forces the
# clipboard-preservation dance above. For ordinary dictation lengths, typing
# the text as KEYEVENTF_UNICODE key events sidesteps both problems entirely:
# no clipboard touch, no paste shortcut, nothing for the target to double.
# ---------------------------------------------------------------------------

def build_unicode_key_events(text: str) -> list:
    """Return the (wVk=0, wScan=codeunit, flags) tuples for typing `text`.

    Pure helper (testable without Windows): one down+up pair per UTF-16 code
    unit, so astral characters (emoji etc.) become surrogate pairs -- exactly
    what KEYEVENTF_UNICODE expects. Newlines become VK_RETURN presses so
    multi-line dictation keeps its line breaks in targets that treat \n and
    Enter differently.
    """
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002
    VK_RETURN = 0x0D
    events = []
    for ch in text.replace('\r\n', '\n'):
        if ch == '\n':
            events.append((VK_RETURN, 0, 0))
            events.append((VK_RETURN, 0, KEYEVENTF_KEYUP))
            continue
        units = ch.encode('utf-16-le')
        for i in range(0, len(units), 2):
            code = units[i] | (units[i + 1] << 8)
            events.append((0, code, KEYEVENTF_UNICODE))
            events.append((0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def type_text_unicode(text: str, chunk: int = 64) -> bool:
    """Type `text` into the focused window via SendInput KEYEVENTF_UNICODE.

    Returns True when every event batch was accepted. Never raises.
    """
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        from ctypes import wintypes

        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class MOUSEINPUT(ctypes.Structure):
            # Required in the union even though we never send mouse events:
            # SendInput validates cbSize against the FULL union (largest
            # member). With only KEYBDINPUT the struct is 32 bytes on x64
            # instead of 40 and Windows rejects every batch ("accepted 0/N"
            # -- the 2026-08-02 live failure).
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if ctypes.sizeof(INPUT) != expected:
            logger.warning(
                "[TYPE] INPUT struct sizeof=%d (expected %d); aborting typed injection",
                ctypes.sizeof(INPUT), expected,
            )
            return False

        INPUT_KEYBOARD = 1
        events = build_unicode_key_events(text)
        arr_all = []
        for vk, scan, flags in events:
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.union.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
            arr_all.append(inp)

        # 30ms settle before the first batch: Chromium occasionally delivers
        # the opening keystroke twice when injection begins mid focus
        # transition (live artifact 2026-08-02: single-N decode typed as NN).
        time.sleep(0.03)
        user32 = ctypes.windll.user32
        i = 0
        while i < len(arr_all):
            batch = arr_all[i:i + chunk]
            ArrType = INPUT * len(batch)
            sent = user32.SendInput(len(batch), ArrType(*batch), ctypes.sizeof(INPUT))
            if sent != len(batch):
                logger.warning(
                    "[TYPE] SendInput accepted %d/%d events; aborting typed injection",
                    sent, len(batch),
                )
                return False
            i += chunk
            time.sleep(0.001)
        return True
    except Exception as e:
        _log_error("type_text_unicode failed", e)
        return False

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
