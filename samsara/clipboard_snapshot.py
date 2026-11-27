"""Win32 clipboard snapshots, lossless image preparation and prepared transactions."""
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
import struct
from typing import Dict


PREFLIGHT_TIMEOUT_S = 0.5
PREFLIGHT_BYTES = 4 * 1024 * 1024
PREFLIGHT_PING_MS = 150
_MAX_IMAGE_PIXELS = 16 * 1024 * 1024
_NO_PAYLOAD = object()
_IMAGE_NAMES = {"png", "jfif", "gif", "webp", "image/svg+xml"}
_VIRTUAL_FILES = {"filegroupdescriptor", "filegroupdescriptorw", "filecontents"}
_EXCEL_LINKS = {"link source", "link source descriptor", "objectlink", "ownerlink"}


class UnsaveableKind(str, Enum):
    COMPLEX = 'complex'
    IMAGE = 'image'
    TOO_LARGE = 'too_large'
    HUNG_OWNER = 'hung_owner'
    BUSY = 'busy'
    TIMEOUT = 'timeout'
    UNREADABLE = 'unreadable'
    CHANGED = 'changed'


@dataclass(frozen=True)
class Unsaveable:
    kind: UnsaveableKind

    def __bool__(self):
        return False


def is_image_format(cb, fmt, name):
    return fmt in (cb.CF_BITMAP, cb.CF_DIB, cb.CF_DIBV5) or (name or '').casefold() in _IMAGE_NAMES


def _owner_process(cb, owner):
    if owner:
        import psutil
        try:
            pid = cb.ctypes.c_ulong()
            cb._user32.GetWindowThreadProcessId(owner, cb.ctypes.byref(pid))
            return psutil.Process(pid.value).name().casefold() if pid.value else ''
        except (AttributeError, OSError, psutil.Error):
            pass
    return ''


def read_preflight(cb, deadline):
    """Read a sequenced snapshot, or return a typed refusal before publication."""
    api = cb._user32
    kind = UnsaveableKind
    if cb.time.monotonic() >= deadline:
        return Unsaveable(kind.TIMEOUT)
    seq_0 = cb.get_clipboard_sequence_number()
    if not seq_0:
        return Unsaveable(kind.UNREADABLE)
    owner = api.GetClipboardOwner()
    process = _owner_process(cb, owner)
    if process == 'rdpclip.exe':
        return Unsaveable(kind.COMPLEX)
    # Ping outside OpenClipboard. Win32 enumeration does not tell us which
    # handles are already materialized. GetClipboardData can enter a blocking
    # WM_RENDERFORMAT, so a nonresponding owner cannot safely be probed at all.
    if owner and (not owner_answers_ping(cb, owner, PREFLIGHT_PING_MS, fail_open=False)
                  or api.IsHungAppWindow(owner)):
        return Unsaveable(kind.HUNG_OWNER)
    if cb.time.monotonic() >= deadline:
        return Unsaveable(kind.TIMEOUT)
    if not cb._open_clipboard_with_retry(max_retries=5):
        return Unsaveable(kind.BUSY)

    saved = cb.ClipboardSnapshot()
    saved.preflight = True
    names = {}
    try:
        if cb.get_clipboard_sequence_number() != seq_0 or api.GetClipboardOwner() != owner:
            return Unsaveable(kind.CHANGED)
        # While open, only renders requested by this reader can legitimately
        # advance the source sequence. Record the post-render value below.
        fmt = api.EnumClipboardFormats(0)
        count = api.CountClipboardFormats()
        while fmt:
            if cb.time.monotonic() >= deadline:
                return Unsaveable(kind.TIMEOUT)
            if fmt in names or len(names) >= cb._MAX_SNAPSHOT_FORMATS:
                return Unsaveable(kind.COMPLEX)
            names[fmt] = (cb.registered_format_name(fmt) or '').casefold()
            fmt = api.EnumClipboardFormats(fmt)
        if len(names) < count:
            return Unsaveable(kind.UNREADABLE)

        registered = set(names.values())
        has_files = cb.CF_HDROP in names
        if ('biff12' in registered or (process == 'excel.exe' and registered & _EXCEL_LINKS)
                or (not has_files and registered & _VIRTUAL_FILES)
                or any(fmt == 0x0080 or 0x0200 <= fmt <= 0x03FF for fmt in names)):
            return Unsaveable(kind.COMPLEX)

        markers = {name.casefold(): 1 << index
                   for index, name in enumerate(cb._FENCED_PRIVATE_CLIPBOARD_FORMATS)}
        exclude_marker = cb.PRIVATE_CLIPBOARD_FORMATS[0].casefold()
        ignored = {'dataobject', 'ole private data'}
        if has_files:
            ignored |= _VIRTUAL_FILES
        saved.privacy_markers = sum(markers[name] for name in markers.keys() & registered)
        remaining = PREFLIGHT_BYTES

        def read(fmt):
            nonlocal remaining
            if cb.time.monotonic() >= deadline:
                return kind.TIMEOUT
            handle = api.GetClipboardData(fmt)
            if cb.time.monotonic() >= deadline:
                return kind.TIMEOUT
            if not handle:
                return _NO_PAYLOAD
            size = cb._kernel32.GlobalSize(handle)
            if not size:
                return _NO_PAYLOAD
            if size > remaining:
                return kind.TOO_LARGE
            ptr = cb._kernel32.GlobalLock(handle)
            if not ptr:
                return kind.UNREADABLE
            try:
                raw = cb.ctypes.string_at(ptr, size)
            finally:
                cb._kernel32.GlobalUnlock(handle)
            if not raw or len(raw) != size:
                return kind.UNREADABLE
            saved[fmt] = raw
            remaining -= size
            return None

        png_formats = [fmt for fmt, name in names.items() if name == 'png']
        lossless = [*png_formats, cb.CF_DIBV5, cb.CF_DIB]
        image_formats = {fmt for fmt, name in names.items() if is_image_format(cb, fmt, name)}
        ordinary = [fmt for fmt in names if fmt not in image_formats]
        alternate_images = [fmt for fmt in names if fmt in image_formats and fmt not in lossless]
        image_errors = set()
        have_lossless = False
        for fmt in [*ordinary, *lossless, *alternate_images]:
            name = names.get(fmt, '')
            synthesized = fmt in (cb.CF_DIBV5, cb.CF_DIB) and cb.CF_BITMAP in names
            if ((fmt not in names and not synthesized) or name in ignored
                    or fmt in (cb.CF_LOCALE, cb.CF_BITMAP)
                    # Windows enumerates CF_PALETTE as a conversion of DIB.
                    # A complete image and its regenerated DIB cover it.
                    or (fmt == cb.CF_PALETTE and image_formats)
                    or (fmt in (cb.CF_TEXT, cb.CF_OEMTEXT) and cb.CF_UNICODETEXT in names)):
                continue
            if not cb.is_snapshot_eligible_format(fmt):
                return Unsaveable(kind.COMPLEX)
            # One full image covers the equivalent raster formats. In
            # particular, avoid rendering a DIB once PNG is already saved.
            if fmt in lossless and have_lossless:
                continue
            error = read(fmt)
            if error is None:
                have_lossless = have_lossless or fmt in lossless
                continue
            if error is _NO_PAYLOAD:
                if name in markers:
                    # Exclusion is presence-based even when there are no bytes.
                    if name == exclude_marker:
                        raw = struct.pack('<I', 1)
                        if len(raw) > remaining:
                            return Unsaveable(kind.TOO_LARGE)
                        saved[fmt] = raw
                        remaining -= len(raw)
                    continue
                if is_image_format(cb, fmt, name):
                    continue
                return Unsaveable(kind.UNREADABLE)
            if is_image_format(cb, fmt, name) and error != kind.TIMEOUT:
                image_errors.add(error)
            else:
                return Unsaveable(error)

        if image_formats and not have_lossless:
            if kind.TOO_LARGE in image_errors:
                return Unsaveable(kind.TOO_LARGE)
            if kind.UNREADABLE in image_errors:
                return Unsaveable(kind.UNREADABLE)
            return Unsaveable(kind.COMPLEX if image_formats & saved.keys() else kind.IMAGE)
        saved.png_format = next(iter(png_formats), None)
        saved.image = bool(image_formats)
        saved.source_seq = cb.get_clipboard_sequence_number()
        saved.source_owner = owner
        if not saved.source_seq or api.GetClipboardOwner() != owner:
            return Unsaveable(kind.CHANGED)
        if cb.time.monotonic() >= deadline:
            return Unsaveable(kind.TIMEOUT)
        return saved
    finally:
        api.CloseClipboard()


def _open_dib(raw):
    """Wrap a packed clipboard DIB with the file header Pillow expects."""
    from PIL import Image

    header_size = struct.unpack_from('<I', raw)[0]
    if header_size == 12:
        bits = struct.unpack_from('<H', raw, 10)[0]
        palette_bytes = (1 << bits) * 3 if bits <= 8 else 0
        offset = header_size + palette_bytes
    else:
        bits, compression = struct.unpack_from('<HI', raw, 14)
        colors = struct.unpack_from('<I', raw, 32)[0]
        palette_entries = colors or (1 << bits if bits <= 8 else 0)
        masks = {3: 12, 6: 16}.get(compression, 0) if header_size == 40 else 0
        offset = header_size + masks + palette_entries * 4
        if header_size == 124:
            profile, size = struct.unpack_from('<II', raw, 112)
            if size and profile == offset:
                offset += size
    if offset >= len(raw):
        raise ValueError('truncated packed DIB')
    header = struct.pack('<2sIHHI', b'BM', len(raw) + 14, 0, 0, offset + 14)
    return Image.open(BytesIO(header + raw), formats=['BMP'])


def image_restore_payloads(cb, saved):
    """Generate missing lossless representations before either clipboard write."""
    from PIL import Image

    png_id = saved.png_format or cb._user32.RegisterClipboardFormatW('PNG')
    if not png_id:
        raise OSError('could not register PNG')
    png = saved.get(png_id)
    dib = saved.get(cb.CF_DIBV5) or saved.get(cb.CF_DIB)
    source = Image.open(BytesIO(png), formats=['PNG']) if png else _open_dib(dib)
    with source:
        if source.width * source.height > _MAX_IMAGE_PIXELS:
            return Unsaveable(UnsaveableKind.TOO_LARGE)
        color_profile = source.info.get('icc_profile')
        remaining = PREFLIGHT_BYTES - sum(map(len, saved.values()))
        needs_dibv5 = cb.CF_DIBV5 not in saved
        dibv5_size = 124 + 4 * source.width * source.height + len(color_profile or b'')
        if needs_dibv5:
            remaining -= dibv5_size
            if remaining < 0:
                return Unsaveable(UnsaveableKind.TOO_LARGE)
        source.load()
        if not png:
            with BytesIO() as output:
                source.save(output, format='PNG', icc_profile=color_profile)
                if output.tell() > remaining:
                    return Unsaveable(UnsaveableKind.TOO_LARGE)
                saved[png_id] = output.getvalue()
        if needs_dibv5:
            # PNG pixels are straight-alpha BGRA for the DIBV5 color masks.
            width, height = source.size
            pixels = source.convert('RGBA').tobytes('raw', 'BGRA')
            header = bytearray(124)
            struct.pack_into('<IiiHHII', header, 0, 124, width, -height, 1, 32, 3, len(pixels))
            struct.pack_into('<IIIII', header, 40, 0xFF0000, 0xFF00, 0xFF, 0xFF000000,
                             0x4D424544 if color_profile else 0x73524742)
            struct.pack_into('<III', header, 108, 4,
                             124 + len(pixels) if color_profile else 0, len(color_profile or b''))
            saved[cb.CF_DIBV5] = bytes(header) + pixels + (color_profile or b'')
    return saved


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


def owner_answers_ping(cb, owner, timeout_ms=None, *, fail_open=True) -> bool:
    """True if the clipboard owner window answers WM_NULL within
    _OWNER_PING_TIMEOUT_MS (SMTO_ABORTIFHUNG). Never raises; an API failure
    counts as answering, so a missing API never blocks a snapshot."""
    try:
        WM_NULL, SMTO_ABORTIFHUNG = 0x0000, 0x0002
        result = cb.ctypes.c_size_t(0)
        return bool(cb._user32.SendMessageTimeoutW(owner, WM_NULL, 0, 0, SMTO_ABORTIFHUNG,
                                                   timeout_ms or cb._OWNER_PING_TIMEOUT_MS,
                                                   cb.ctypes.byref(result)))
    except Exception:
        return fail_open


def _clipboard_window(cb):
    # OpenClipboard(NULL) cannot give EmptyClipboard a valid write owner.
    return cb._user32.CreateWindowExW(0, 'STATIC', None, 0, 0, 0, 0, 0,
                                   None, None, None, None)


def _prepare_handle(cb, raw):
    handle = cb._kernel32.GlobalAlloc(0x0002, len(raw))  # GMEM_MOVEABLE
    if not handle:
        raise OSError("GlobalAlloc returned NULL")
    try:
        ptr = cb._kernel32.GlobalLock(handle)
        if not ptr:
            raise OSError("GlobalLock returned NULL")
        try:
            cb.ctypes.memmove(ptr, raw, len(raw))
        finally:
            cb._kernel32.GlobalUnlock(handle)
    except Exception:
        cb._kernel32.GlobalFree(handle)
        raise
    return handle


def _privacy_payloads(cb, exclude, *, already_present=0):
    for index, (name, value) in enumerate(zip(cb._FENCED_PRIVATE_CLIPBOARD_FORMATS, (1, 0, 0, 1))):
        if (index == 0 and not exclude) or already_present & (1 << index):
            continue
        fmt = cb._user32.RegisterClipboardFormatW(name)
        if not fmt:
            raise OSError("could not register clipboard privacy format")
        yield fmt, value.to_bytes(4, 'little')


def _restore_payloads(cb, saved):
    payloads = {fmt: raw for fmt, raw in saved.items() if cb.is_nonempty_payload(raw)}
    # Keep a genuinely empty clipboard empty after the transport is cleared.
    if getattr(saved, 'preflight', False) and (payloads or saved.privacy_markers):
        return {**dict(_privacy_payloads(cb, exclude=False, already_present=saved.privacy_markers)),
                **payloads}
    return payloads


def _free_handles(cb, handles):
    for handle in handles.values():
        cb._kernel32.GlobalFree(handle)
    handles.clear()


def _prepare_handles(cb, payloads):
    handles = {}
    try:
        for fmt, raw in payloads.items():
            handles[fmt] = _prepare_handle(cb, raw)
    except Exception:
        _free_handles(cb, handles)
        raise
    return handles


def _restore_fenced_clipboard(cb, saved, *, prepared):
    """Restore prepared handles while the transport window still owns the data."""
    cb._restore_status(saved, 'failed')
    owner = saved.transport_owner
    complete = saved.complete

    def kept_newer():
        cb.logger.info("[CLIP] Clipboard changed during paste; keeping the newer copy")
        cb._restore_status(saved, 'kept_newer')
        return complete

    payloads, handles = prepared
    failed = []
    opened = False
    try:
        if cb._user32.GetClipboardOwner() != owner:
            return kept_newer()
        opened = cb._open_clipboard_with_retry(hwnd=owner)
        if not opened:
            return False
        if cb._user32.GetClipboardOwner() != owner:
            return kept_newer()
        if not cb._user32.EmptyClipboard():
            return False
        for fmt, raw in payloads.items():
            if not cb._user32.SetClipboardData(fmt, handles[fmt]):
                # Retry once while retaining every handle already transferred.
                cb._kernel32.GlobalFree(handles.pop(fmt))
                try:
                    handles[fmt] = _prepare_handle(cb, raw)
                except OSError:
                    failed.append(fmt)
                    continue
                if not cb._user32.SetClipboardData(fmt, handles[fmt]):
                    failed.append(fmt)
                    continue
            del handles[fmt]
    except Exception:
        cb._log_error("restore_clipboard failed unexpectedly")
        return False
    finally:
        if opened:
            cb._user32.CloseClipboard()

    if failed:
        cb._log_error("clipboard restore was partial -- could not put back: "
                   + ", ".join(cb.format_label(fmt) for fmt in failed))
    restored = not failed and complete
    cb._restore_status(saved, 'put_back' if restored else 'partial')
    return restored


def _stage_fenced_text(cb, text, *, expected_seq=None, expected_owner=None, state=None, before_stage=None):
    """Publish protected text under the same lock as the sequence check."""
    handles = {}
    opened = False
    window = None
    try:
        payloads = dict(_privacy_payloads(cb, exclude=True))
        payloads[cb.CF_UNICODETEXT] = (text + '\0').encode('utf-16-le')
        handles = _prepare_handles(cb, payloads)
        window = _clipboard_window(cb)
        if not window:
            return False
        opened = cb._open_clipboard_with_retry(max_retries=5, hwnd=window)
        if not opened:
            return cb.recovery.unsaveable(UnsaveableKind.BUSY)
        if before_stage is not None and not before_stage():
            return False
        if expected_seq is not None:
            if (cb.get_clipboard_sequence_number() != expected_seq
                    or (cb._user32.GetClipboardOwner() or None) != (expected_owner or None)):
                return cb.recovery.unsaveable(UnsaveableKind.CHANGED)
        if not cb._user32.EmptyClipboard():
            return False
        if state is not None:
            state['owner'] = window
        for fmt in payloads:
            if not cb._user32.SetClipboardData(fmt, handles[fmt]):
                return False
            del handles[fmt]  # ownership transferred to Windows
        return True
    except Exception:
        cb._log_error("Protected clipboard write failed")
        return False
    finally:
        if opened:
            if state is not None and state.get('owner'):
                state['seq'] = cb.get_clipboard_sequence_number()
            cb._user32.CloseClipboard()
        if window and (state is None or not state.get('owner')):
            cb._user32.DestroyWindow(window)
        _free_handles(cb, handles)


def paste_with_fence(cb, text, paste_delay, restore_delay, before_paste,
                     delivery_observer, before_stage):
    """Send a guarded paste after a complete, read-only clipboard preflight.

    before_stage is a repeatable target/cancellation check. When supplied,
    before_paste runs immediately before dispatch. A legacy before_paste
    callback runs once after preflight and before publication, so it can
    prepare the target only after the clipboard has been saved.
    """
    try:
        import pyautogui
    except ImportError:
        cb._log_error("pyautogui not available")
        return False

    with cb.clipboard_lock:
        receipt = cb.recovery.current_attempt()
        target = cb._paste_target()
        if target is not None:
            from samsara.injection_safety import window_integrity
            if not target[0] or window_integrity(target[0]).blocked:
                return False

        def staging_allowed():
            return ((before_stage is None or before_stage())
                    and cb._paste_target_matches(target))

        if not staging_allowed():
            return False
        saved = cb.preflight_clipboard()
        if isinstance(saved, Unsaveable):
            return cb.recovery.unsaveable(saved.kind)
        if not saved.source_seq:
            return cb.recovery.unsaveable(UnsaveableKind.UNREADABLE)

        published = {}
        restore_handles = {}
        try:
            if before_stage is None and before_paste is not None and not before_paste():
                return False
            # An allocation failure must happen before the original owner
            # is released. Keep the restorable handles through dispatch.
            restore_payloads = _restore_payloads(cb, saved)
            restore_handles = _prepare_handles(cb, restore_payloads)
            staged = _stage_fenced_text(
                cb, text, expected_seq=saved.source_seq, expected_owner=saved.source_owner,
                state=published, before_stage=staging_allowed,
            )
            if not staged:
                return staged
            readiness = cb.wait_for_clipboard_ready(published['seq'], paste_delay)
            if readiness != 'ready':
                kind = UnsaveableKind.CHANGED if readiness == 'changed' else UnsaveableKind.BUSY
                return cb.recovery.unsaveable(kind)
            if before_stage is not None and before_paste is not None and not before_paste():
                return False
            if not cb._paste_target_matches(target):
                return False
            if cb.get_clipboard_sequence_number() != published['seq']:
                return cb.recovery.unsaveable(UnsaveableKind.CHANGED)
            outcome = cb.message_paste_to_focused_control()
            if outcome == cb.DeliveryOutcome.NOT_SENT:
                return False
            if outcome != cb.DeliveryOutcome.CONFIRMED and not getattr(outcome, 'suppress_keystroke', False):
                if not cb._paste_target_matches(target):
                    return False
                if cb.get_clipboard_sequence_number() != published['seq']:
                    return cb.recovery.unsaveable(UnsaveableKind.CHANGED)
                pyautogui.hotkey('ctrl', 'v')
            if delivery_observer is not None:
                delivery_observer(outcome)

            cb.time.sleep(max(restore_delay, cb.CLIPBOARD_RESTORE_DELAY))
            return True
        except Exception:
            cb._log_error("Clipboard paste failed")
            cb.recovery.fail("Clipboard paste failed. Draft kept.")
            return False
        finally:
            try:
                if owner := published.get('owner'):
                    saved.seq = published.get('seq')
                    saved.transport_owner = owner
                    try:
                        if not _restore_fenced_clipboard(
                            cb, saved, prepared=(restore_payloads, restore_handles),
                        ):
                            cb.logger.error('[CLIP] Original clipboard restore failed; exact preservation not confirmed')
                            if receipt is not None:
                                receipt.restore_failed = True
                    finally:
                        cb._user32.DestroyWindow(owner)
            finally:
                _free_handles(cb, restore_handles)
