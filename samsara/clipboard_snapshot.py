"""Win32 clipboard snapshot reader (split out of samsara.clipboard, 1167s).

Moved, not rewritten, to keep samsara/clipboard.py under the size ratchet.
Every Win32 object, constant and helper is looked up on samsara.clipboard at
call time, so tests that patch cb._user32 / cb._kernel32 / cb.ctypes /
cb.time, the snapshot caps or cb._save_clipboard_impl keep their meaning.
Only samsara.clipboard calls these functions.
"""
from typing import Dict


def read_snapshot(cb) -> "cb.ClipboardSnapshot":
    """Body of samsara.clipboard._save_clipboard_impl on Windows."""
    saved = cb.ClipboardSnapshot()
    skipped = 0
    not_saved: Dict[int, str] = {}   # candidate losses; synthesis-filtered at the end
    started = cb.time.monotonic()
    total_bytes = 0
    format_count = 0

    if not cb._open_clipboard_with_retry():
        cb._log_error("Could not open clipboard for save after retries")
        cb._mark_whole_clipboard_lost(saved, "clipboard busy")
        return saved

    attempted = set()      # formats GetClipboardData was asked for
    synthesized = set()    # 1167s: DIB bytes explicitly requested for CF_BITMAP
    try:
        # A hung delayed-render provider (an app that registered a format
        # but defers actually producing the data until GetClipboardData is
        # called) would block GetClipboardData indefinitely below. An owner
        # Windows reports as hung (IsHungAppWindow, ~5 s unresponsive) skips
        # the snapshot. 1167s: a WM_NULL ping that merely misses
        # _OWNER_PING_TIMEOUT_MS is advisory -- formats that are already
        # rendered are still read. Nothing can interrupt a render that has
        # STARTED (GetClipboardData has no timeout), so every read runs in
        # save_clipboard()'s single-flight worker under the caller's bound.
        owner = cb._user32.GetClipboardOwner()
        if owner and cb._user32.IsHungAppWindow(owner):
            cb.logger.info("[CLIP] clipboard owner hung, skipping snapshot")
            cb._mark_whole_clipboard_lost(saved, "clipboard owner not responding")
            return saved
        if owner and not cb._owner_answers_ping(owner):
            cb.logger.info("[CLIP] clipboard owner slow to answer; reading within the snapshot deadline")
            saved.owner_slow = True

        def read(fmt):
            """Copy one memory format now; record why not. False = stop."""
            nonlocal skipped, total_bytes
            if not cb.is_snapshot_eligible_format(fmt):
                # Never GetClipboardData/GlobalLock a GDI handle (CF_BITMAP).
                raise ValueError(f"not a memory format: {fmt}")
            attempted.add(fmt)
            try:
                # GetClipboardData forces a delayed-render provider (e.g.
                # a screenshot tool that defers rendering until requested)
                # to render now, so this always returns real data or NULL,
                # never a placeholder -- no separate "force render" step
                # needed.
                handle = cb._user32.GetClipboardData(fmt)
                # A provider can answer WM_NULL, then stall while rendering.
                # Never copy a late result or publish it as a usable backup.
                if cb.time.monotonic() - started >= cb._SNAPSHOT_TIMEOUT_S:
                    saved.lost[0] = "clipboard backup timed out"
                    return False
                if not handle:
                    skipped += 1
                    not_saved[fmt] = "owner provided no data"
                    return True

                size = cb._kernel32.GlobalSize(handle)
                if size <= 0 or size > cb._MAX_FORMAT_BYTES:
                    skipped += 1
                    not_saved[fmt] = "over the 100 MB cap" if size > cb._MAX_FORMAT_BYTES else "empty"
                    return True
                if total_bytes + size > cb._MAX_SNAPSHOT_BYTES:
                    saved.lost[0] = "over the 100 MB total snapshot cap"
                    return False

                ptr = cb._kernel32.GlobalLock(handle)
                if not ptr:
                    skipped += 1
                    not_saved[fmt] = "could not lock its memory"
                    return True
                try:
                    raw = cb.ctypes.string_at(ptr, size)
                finally:
                    cb._kernel32.GlobalUnlock(handle)

                # Never count a format as saved unless its bytes are
                # actually non-empty -- an empty "success" is a lie.
                if cb.is_nonempty_payload(raw):
                    saved[fmt] = raw
                    total_bytes += size
                else:
                    skipped += 1
                    not_saved[fmt] = "empty"
            except Exception as e:
                skipped += 1
                not_saved[fmt] = "read error"
                cb._log_error(f"Could not read clipboard format {fmt}", e)
            return True

        fmt = 0
        complete_enumeration = False
        while True:
            if cb.time.monotonic() - started >= cb._SNAPSHOT_TIMEOUT_S:
                saved.lost[0] = "clipboard backup timed out"
                break
            fmt = cb._user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                complete_enumeration = True
                break
            format_count += 1
            if format_count > cb._MAX_SNAPSHOT_FORMATS:
                saved.lost[0] = "too many clipboard formats"
                break

            # Skip formats that aren't memory handles, or that we
            # deliberately don't snapshot (CF_BITMAP/CF_METAFILEPICT/etc --
            # see module docstring).
            if not cb.is_snapshot_eligible_format(fmt):
                skipped += 1
                not_saved[fmt] = "not a format that can be snapshotted"
                continue
            if not read(fmt):
                break

        # 1167s: a bitmap is not lost merely because no DIB was enumerated
        # (or the enumerated one was empty/failed). Ask Windows for the
        # documented CF_BITMAP -> CF_DIB / CF_DIBV5 conversion while the
        # clipboard is still open, copy it immediately under the same caps
        # and deadline, and never touch the HBITMAP itself.
        if (complete_enumeration and cb.CF_BITMAP in not_saved
                and not (cb.is_nonempty_payload(saved.get(cb.CF_DIB))
                         or cb.is_nonempty_payload(saved.get(cb.CF_DIBV5)))):
            for wanted in (cb.CF_DIB, cb.CF_DIBV5):
                if wanted in attempted:
                    continue
                if cb.time.monotonic() - started >= cb._SNAPSHOT_TIMEOUT_S:
                    saved.lost[0] = "clipboard backup timed out"
                    break
                keep_going = read(wanted)
                if cb.is_nonempty_payload(saved.get(wanted)):
                    synthesized.add(wanted)
                    not_saved.pop(wanted, None)
                    cb.logger.info("[CLIP] bitmap backed up as %s bytes requested from Windows",
                                   cb.format_label(wanted))
                    break
                # A conversion that produced nothing is not separate loss:
                # the bitmap itself is what stays unpreserved, with the reason.
                why = not_saved.pop(wanted, None)
                if why:
                    not_saved[cb.CF_BITMAP] = f"no image bytes from Windows ({why})"
                if not keep_going:
                    break
    finally:
        # Read while the clipboard is still held: an owner's delayed render
        # (or a conversion) is included, nobody else can change it here.
        saved.source_seq = cb.get_clipboard_sequence_number()
        cb._user32.CloseClipboard()
    saved.synthesized = frozenset(synthesized)

    elapsed = cb.time.monotonic() - started
    if elapsed > cb._SLOW_SNAPSHOT_S:
        cb._log_error(f"clipboard snapshot took {elapsed:.1f} s -- the clipboard owner was slow to render its data")

    # Legacy text dedup: if CF_UNICODETEXT was captured non-empty, Windows
    # synthesizes CF_TEXT/CF_OEMTEXT/CF_LOCALE from it automatically on
    # restore (the mirror image of the CF_DIB -> CF_BITMAP synthesis this
    # module already relies on -- see module docstring). Dropping the
    # redundant legacy copies here shrinks the restore window (fewer
    # formats for restore's prepare phase to allocate) without
    # losing anything actually restorable.
    if cb.is_nonempty_payload(saved.get(cb.CF_UNICODETEXT)):
        for legacy_fmt in (cb.CF_TEXT, cb.CF_OEMTEXT, cb.CF_LOCALE):
            if legacy_fmt in saved:
                del saved[legacy_fmt]
                skipped += 1

    # Completeness (292): an owner that provides no handle for a private
    # format has supplied no user content to lose.  Keep delivery fail-closed
    # for actual text/HTML/RTF/file/image content, but preserve every readable
    # format and warn once about non-content formats we omit.
    # Only genuine saved bytes cover a format. DIB bytes Samsara requested
    # for a bitmap cover that bitmap, never an advertised CF_DIBV5 whose
    # alpha/colour space a plain DIB cannot carry (1167s).
    native = set(saved) - set(synthesized)
    for fmt, reason in not_saved.items():
        covering = native | (set(saved) if fmt == cb.CF_BITMAP else set())
        if reason == "empty" or cb._SYNTHESIZED_FROM.get(fmt, set()) & covering:
            continue
        if reason == "owner provided no data" and cb.is_known_private_format(fmt):
            continue
        if (cb.is_user_content_format(fmt)
                or reason in {"over the 100 MB cap", "could not lock its memory", "read error"}):
            saved.lost[fmt] = reason
        else:
            saved.omitted[fmt] = reason
    if saved.lost:
        cb._log_error("clipboard snapshot is incomplete -- these cannot be put back after a paste: "
                      + saved.describe_lost())
    if saved.omitted:
        cb.logger.warning("[CLIP] clipboard snapshot omitted formats that could not be restored: "
                          + saved.describe_omitted())

    if saved or skipped:
        print(f"[DEBUG] Clipboard saved: {len(saved)} format(s), skipped {skipped} format(s)")

    return saved


def mark_whole_clipboard_lost(cb, snapshot, reason) -> None:
    """Record that nothing could be snapshotted although the clipboard has
    content. Never raises."""
    try:
        if cb._user32.CountClipboardFormats() > 0:
            snapshot.lost[0] = reason
            cb._log_error(f"clipboard content could not be snapshotted ({reason}); "
                          "a paste will not be able to put it back")
    except Exception:
        snapshot.lost[0] = reason


def owner_answers_ping(cb, owner) -> bool:
    """True if the clipboard owner window answers WM_NULL within
    _OWNER_PING_TIMEOUT_MS (SMTO_ABORTIFHUNG). Never raises; an API failure
    counts as answering, so a missing API never blocks a snapshot."""
    try:
        WM_NULL, SMTO_ABORTIFHUNG = 0x0000, 0x0002
        result = cb.ctypes.c_size_t(0)
        return bool(cb._user32.SendMessageTimeoutW(owner, WM_NULL, 0, 0, SMTO_ABORTIFHUNG,
                                                   cb._OWNER_PING_TIMEOUT_MS, cb.ctypes.byref(result)))
    except Exception:
        return True
