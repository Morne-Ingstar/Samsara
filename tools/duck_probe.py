"""Manual live COM ducking probe (standalone)."""

from __future__ import annotations

import os
import sys
import time
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from samsara.audio_ducking import SessionDucker, _iter_audio_sessions


def _process_name(pid: int) -> str | None:
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:
        return None


def _read_tracked(ducker: SessionDucker) -> list[tuple[str, int, str | None, float, float, str | None]]:
    rows: list[tuple[str, int, str | None, float, float, str | None]] = []
    for session in ducker._tracked_by_id.values():
        current: float | None = None
        error: str | None = None
        try:
            current = float(session.handle.get_master_volume())
        except Exception as exc:
            current = None
            error = str(exc)
        rows.append(
            (
                session.session_id,
                session.pid,
                _process_name(session.pid),
                float(session.original_volume),
                float(session.applied_volume),
                error,
            )
        )
    return rows


def _read_restored(session_ids: Iterable[str]) -> dict[str, float | None]:
    wanted = set(session_ids)
    restored: dict[str, float | None] = {session_id: None for session_id in wanted}
    try:
        for session in _iter_audio_sessions():
            if session.session_id in wanted:
                try:
                    restored[session.session_id] = float(session.get_master_volume())
                except Exception:
                    restored[session.session_id] = None
    except Exception:
        return restored

    return restored


def run() -> int:
    ducker = SessionDucker(duck_level=0.3)
    ducker.start()

    print(
        "SessionDucker counters: "
        f"seen={ducker.sessions_seen} "
        f"ducked={ducker.sessions_ducked} "
        f"failed={ducker.sessions_failed} "
        f"last_error={ducker.last_error!r}"
    )

    tracked = _read_tracked(ducker)
    for session_id, pid, name, original_volume, applied_volume, session_error in tracked:
        print(
            f"session session_id={session_id!r} pid={pid} "
            f"name={name or '<unknown>'} original={original_volume:.6f} "
            f"applied={applied_volume:.6f} error={session_error}"
        )

    if not tracked:
        ducker.stop()
        print("no sessions ducked")
        return 2

    if ducker.last_error:
        ducker.stop()
        return 1

    time.sleep(3)

    ducker.stop()

    print(
        "SessionDucker counters after restore: "
        f"seen={ducker.sessions_seen} "
        f"ducked={ducker.sessions_ducked} "
        f"failed={ducker.sessions_failed} "
        f"last_error={ducker.last_error!r}"
    )

    if ducker.last_error:
        return 1

    if ducker.sessions_ducked < 1:
        return 2

    restored = _read_restored(session_id for session_id, *_ in tracked)
    for session_id, pid, name, original_volume, _applied, _ in tracked:
        restored_volume = restored.get(session_id)
        print(
            f"restored session_id={session_id!r} pid={pid} "
            f"name={name or '<unknown>'} original={original_volume:.6f} "
            f"restored={('<missing>' if restored_volume is None else f'{restored_volume:.6f}')}"
        )
        if restored_volume is None:
            print(f"did not read restored volume for session_id={session_id!r}")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
