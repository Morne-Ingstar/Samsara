"""One-shot clipboard reader. A stalled Win32 render dies with this process."""
import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

if __name__ == '__main__' and not getattr(sys, 'frozen', False):
    # -I omits the script directory. Import only from our own source tree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from samsara import clipboard_snapshot as snapshot
from samsara.clipboard_snapshot import Unsaveable, UnsaveableKind


_MAGIC = b'SCB2'
_HEADER = struct.Struct('<4sIQIHB')  # magic, sequence, HWND, PNG ID, count, flags
_FORMAT = struct.Struct('<II')       # format ID, byte count
_EXIT_GRACE_S = 0.075


def _window_station():
    # Clipboard data is scoped to a window station. Verify the inherited
    # station before opening it, even if two stations have the same sequence.
    api = ctypes.windll.user32
    station = api.GetProcessWindowStation
    station.argtypes = []
    station.restype = wintypes.HANDLE
    name = api.GetUserObjectInformationW
    name.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    name.restype = wintypes.BOOL
    result = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    handle = station()
    if handle and name(handle, 2, result, ctypes.sizeof(result), ctypes.byref(needed)):
        return result.value
    return ''


def _worker_command():
    interpreter = Path(sys.executable)
    if getattr(sys, 'frozen', False):
        return [str(interpreter.with_name('samsara-clipboard-preflight.exe'))]
    if interpreter.name.lower() == 'pythonw.exe':
        interpreter = interpreter.with_name('python.exe')
    return [str(interpreter), '-I', '-B', str(Path(__file__).resolve())]


def _write_result(output, saved):
    if isinstance(saved, Unsaveable):
        output.write(saved.kind.value.encode('ascii'))
        return
    flags = saved.image | (saved.privacy_markers << 1)
    output.write(_HEADER.pack(_MAGIC, saved.source_seq, saved.source_owner or 0,
                              saved.png_format or 0, len(saved), flags))
    for fmt, raw in saved.items():
        output.write(_FORMAT.pack(fmt, len(raw)))
        output.write(raw)


def _read_result(cb, data):
    if not data.startswith(_MAGIC):
        return Unsaveable(UnsaveableKind(data.decode('ascii')))

    _, sequence, owner, png_format, count, flags = _HEADER.unpack_from(data)
    # A read can add PNG and DIBV5 during image preparation.
    if count > cb._MAX_SNAPSHOT_FORMATS + 2 or flags >> (1 + len(cb._FENCED_PRIVATE_CLIPBOARD_FORMATS)):
        raise ValueError('invalid clipboard preflight header')
    saved = cb.ClipboardSnapshot()
    offset = _HEADER.size
    for _ in range(count):
        fmt, size = _FORMAT.unpack_from(data, offset)
        offset += _FORMAT.size
        end = offset + size
        if not size or end > len(data) or fmt in saved or not cb.is_snapshot_eligible_format(fmt):
            raise ValueError('invalid clipboard preflight format')
        saved[fmt] = data[offset:end]
        offset = end
    if offset != len(data):
        raise ValueError('unexpected clipboard preflight bytes')
    saved.preflight = True
    saved.source_seq = sequence
    saved.source_owner = owner or None
    saved.png_format = png_format or None
    saved.image = bool(flags & 1)
    saved.privacy_markers = flags >> 1
    return saved


def capture(cb, deadline):
    """Wait for a private pipe while the parent leaves OpenClipboard alone."""
    try:
        station = _window_station()
        if not station:
            return Unsaveable(UnsaveableKind.UNREADABLE)
        process = subprocess.Popen(
            [*_worker_command(), str(deadline), station],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW,
            env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'),
        )
    except OSError:
        return Unsaveable(UnsaveableKind.UNREADABLE)

    try:
        output, _ = process.communicate(timeout=max(0, deadline - cb.time.monotonic()))
        if cb.time.monotonic() >= deadline:
            return Unsaveable(UnsaveableKind.HUNG_OWNER)
        max_bytes = snapshot.PREFLIGHT_BYTES + _HEADER.size + _FORMAT.size * (cb._MAX_SNAPSHOT_FORMATS + 2)
        if process.returncode or len(output) > max_bytes:
            return Unsaveable(UnsaveableKind.UNREADABLE)
        return _read_result(cb, output)
    except subprocess.TimeoutExpired:
        return Unsaveable(UnsaveableKind.HUNG_OWNER)
    except (OSError, ValueError, struct.error):
        return Unsaveable(UnsaveableKind.UNREADABLE)
    finally:
        if process.poll() is None:
            try:
                # On Windows Popen.kill calls TerminateProcess. Wait briefly
                # for the OS to release the native lock and close the pipe.
                process.kill()
                process.communicate(timeout=_EXIT_GRACE_S)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    cb.logger.error('[CLIP] Clipboard preflight helper exit could not be confirmed')


def read_snapshot(cb, deadline):
    saved = snapshot.read_preflight(cb, deadline)
    if isinstance(saved, cb.ClipboardSnapshot) and saved.image:
        return snapshot.image_restore_payloads(cb, saved)
    return saved


def main():
    deadline, expected_station = sys.argv[1:]
    deadline = float(deadline)
    if _window_station() != expected_station or not expected_station:
        saved = Unsaveable(UnsaveableKind.UNREADABLE)
    elif time.monotonic() >= deadline:
        saved = Unsaveable(UnsaveableKind.TIMEOUT)
    else:
        # Suppress the standalone logger's profile-file bootstrap. Clipboard
        # contents travel only over the anonymous binary stdout pipe.
        logging.getLogger().addHandler(logging.NullHandler())
        from samsara import clipboard as cb

        saved = read_snapshot(cb, deadline)
    _write_result(sys.stdout.buffer, saved)
    return 0


if __name__ == '__main__':
    sys.exit(main())
