"""Append-only JSONL flight recorder for post-hoc incident forensics.

Two failure classes have escaped diagnosis repeatedly because they are
intermittent and the evidence evaporates before a log can be read after
the fact: hold-to-record occasionally producing a single letter, and
hands-free sessions crashing/freezing only after long uptime. This module
lets the seams that touch those failure classes document themselves as
they run, independent of whatever else is going on in the process.

CRITICAL: writes use os.open/os.write directly with O_APPEND, NEVER the
logging module -- the 2026-08-02 freeze poisoned the logging lock while
raw writes kept working (tribunal arc_20260803_163412). A stuck logging
handler must never be able to blind this recorder to the stuck-ness.

record() never raises. A broken flight recorder (missing directory,
permissions, full disk) must never be the thing that takes the app down;
losing an event is acceptable, crashing on one is not.

WITHIN-PROCESS LOCK: on Windows, os.open(..., os.O_APPEND) is a CRT
emulation (seek-to-end, then write) rather than a true kernel-level atomic
append -- unlike POSIX, two threads can interleave between the seek and
the write and clobber each other's line. A single process-wide lock
around the write closes that race for every thread in this process
(measured: unguarded, an 8-thread/25-event burst lost ~14% of lines).
Cross-process interleaving is not covered -- Samsara is single-instance
(see samsara/single_instance.py) so that gap is not expected to matter.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from samsara.paths import samsara_home_dir

# Single os.write() per record() call; keep comfortably under typical
# atomic-append guarantees for a single line.
_MAX_LINE_BYTES = 4096

# Guards the open+write+close sequence -- see the WITHIN-PROCESS LOCK note
# in the module docstring for why this is needed on Windows.
_write_lock = threading.Lock()


def _flight_dir() -> Path:
    return samsara_home_dir() / "flight"


def _events_path(now: float) -> Path:
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d")
    return _flight_dir() / f"events-{stamp}.jsonl"


def record(event: str, **fields) -> None:
    """Append one JSON event line: {event, t_wall, t_wall_iso, t_mono, thread, **fields}.

    Never raises -- every failure mode (deleted directory, disk full,
    permission error, unserializable field) is swallowed silently.
    """
    try:
        now = time.time()
        payload_obj = {
            "event": event,
            "t_wall": now,
            "t_wall_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "t_mono": time.monotonic(),
            "thread": threading.current_thread().name,
            **fields,
        }
        line = json.dumps(payload_obj, default=str, ensure_ascii=True) + "\n"
        data = line.encode("utf-8")
        if len(data) > _MAX_LINE_BYTES:
            # A field blew the budget (e.g. a caller passed a huge blob by
            # mistake) -- drop to a minimal record rather than lose the
            # event's existence entirely or risk a torn multi-line write.
            fallback_obj = {
                "event": event,
                "t_wall": now,
                "t_mono": payload_obj["t_mono"],
                "thread": payload_obj["thread"],
                "truncated": True,
            }
            data = (json.dumps(fallback_obj, default=str) + "\n").encode("utf-8")

        directory = _flight_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = _events_path(now)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        with _write_lock:
            fd = os.open(str(path), flags, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
    except Exception:
        pass
