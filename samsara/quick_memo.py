"""Serialized, durable storage for short voice memos."""
from __future__ import annotations

import os
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

from samsara.paths import samsara_home_dir
from samsara.log import get_logger

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
logger = get_logger("Samsara")


class MemoWriteError(OSError):
    """A memo could not be persisted and the caller must report it."""


def memo_dir(home=None) -> Path:
    root = Path(home) if home is not None else samsara_home_dir()
    if root.suffix.lower() == ".md":
        root = root.parent
    return root / "memos"


def memo_file(home=None) -> Path:
    supplied = Path(home) if home is not None else None
    if supplied is not None and supplied.suffix.lower() == ".md":
        return supplied
    return memo_dir(home) / "memos.md"


def _file_lock(path: Path):
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def append_memo(text, source, audio_path=None, home=None) -> Path:
    """Append one UTF-8 memo under its file lock, or raise MemoWriteError."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("memo text must not be empty")
    path = memo_file(home)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"## {timestamp}\n{text}\n"
    if audio_path is not None:
        relative = os.path.relpath(Path(audio_path), path.parent).replace(os.sep, "/")
        entry += f"[audio: {relative}]\n"
    entry += "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = _file_lock(path)
        with lock:
            is_new = not path.exists()
            with path.open("a", encoding="utf-8") as memo:
                if is_new:
                    memo.write("# Memos\n\n")
                memo.write(entry)
                memo.flush()
                os.fsync(memo.fileno())
    except OSError as exc:
        logger.exception("[MEMO] Could not append memo to %s", path)
        raise MemoWriteError(f"could not append memo to {path}") from exc
    return path


def _replace_with_retry(src, dst, attempts=6, initial_delay_s=0.02):
    """Replace a fresh file, tolerating short Windows scanner locks."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(attempts):
        try:
            return os.replace(src, dst)
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(initial_delay_s * (attempt + 1))


def retain_audio(audio, sample_rate, home=None) -> Path:
    """Save captured mono float audio for a memo and return its WAV path."""
    directory = memo_dir(home) / "audio"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now().strftime('%Y%m%dT%H%M%S_%f')}.wav"
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        values = [max(-1.0, min(1.0, float(value))) for value in audio]
        pcm = b"".join(int(value * 32767).to_bytes(2, "little", signed=True) for value in values)
        with wave.open(str(temporary), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(sample_rate))
            wav.writeframes(pcm)
        _replace_with_retry(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path
