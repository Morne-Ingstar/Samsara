"""Voice memo capture: arm a one-shot divert of the next hold-to-dictate
recording into an Obsidian vault instead of injecting it as text.

Flow: the "voice memo" command arms this module (arm()). The NEXT
hold-to-dictate recording, on the hotkey path only (dictation.py's
_stop_recording_impl -> transcribe() closure, plain-dictate branch), is
checked with is_armed() and diverted into capture() instead of injection.
Accessibility rationale: zero-typing capture of spoken thoughts into a
permanent, searchable archive -- the WAV becomes an inline-playable
Obsidian audio embed, the transcript makes it searchable.

State is intentionally simple module-level globals (not a class) so tests
can reset it trivially between cases without needing an app fixture.
"""
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from samsara.log import get_logger

logger = get_logger("Samsara")

_DEFAULT_ARM_TIMEOUT_S = 120

_armed = False
_armed_at = 0.0


def arm(app) -> None:
    """Arm a one-shot capture: the next hold-to-dictate recording is
    diverted into the vault instead of injected."""
    global _armed, _armed_at
    _armed = True
    _armed_at = time.monotonic()
    logger.info("[MEMO] Armed — next dictation will be captured")


def disarm(app) -> bool:
    """Clear the arm state. Returns whether it was armed beforehand."""
    global _armed, _armed_at
    was_armed = _armed
    _armed = False
    _armed_at = 0.0
    return was_armed


def is_armed(config) -> bool:
    """True only while armed and within arm_timeout_s of arm(). Clears and
    logs expiry as a side effect the first time it's found stale."""
    global _armed, _armed_at
    if not _armed:
        return False
    timeout_s = (config.get('voice_memo', {}) or {}).get('arm_timeout_s', _DEFAULT_ARM_TIMEOUT_S)
    if time.monotonic() - _armed_at > timeout_s:
        _armed = False
        _armed_at = 0.0
        logger.info("[MEMO] Arm expired")
        return False
    return True


def _write_wav(path: Path, audio, sample_rate) -> None:
    pcm_int16 = np.clip(
        np.asarray(audio, dtype=np.float32) * 32767.0, -32768, 32767
    ).astype(np.int16)
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm_int16.tobytes())


def _append_note_entry(note_path: Path, embed_relpath: str, text: str) -> None:
    is_new = not note_path.exists()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"## {timestamp}\n![[{embed_relpath}]]\n\n{text}\n"
    with open(note_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write("# Voice Memos\n\n")
        else:
            f.write("\n")
        f.write(entry)


def capture(app, audio, sample_rate, text) -> bool:
    """Write the recording as a WAV into the vault and append a note entry
    with an audio embed + transcript. Never raises -- any failure is
    logged and returns False so the caller falls through to normal
    injection (a memo failure must never lose the user's dictation)."""
    try:
        cfg = app.config.get('voice_memo', {}) or {}
        vault_dir = Path(cfg.get('vault_dir', ''))
        note_relpath = cfg.get('note_relpath', 'Voice Memos.md')
        attachments_relpath = cfg.get('attachments_relpath', 'Attachments/Memos')

        attachments_dir = vault_dir / attachments_relpath
        attachments_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        wav_name = f"memo_{ts}.wav"
        wav_path = attachments_dir / wav_name

        _write_wav(wav_path, audio, sample_rate)

        embed_relpath = (Path(attachments_relpath) / wav_name).as_posix()
        note_path = vault_dir / note_relpath
        note_path.parent.mkdir(parents=True, exist_ok=True)
        _append_note_entry(note_path, embed_relpath, text)

        disarm(app)
        logger.info(f"[MEMO] Captured -> {wav_path}")
        if hasattr(app, 'play_sound'):
            app.play_sound("success")
        if hasattr(app, 'listening_indicator'):
            schedule_ui = getattr(app, '_schedule_ui', None)
            flash_success = getattr(app.listening_indicator, 'flash_success', None)
            if schedule_ui is not None and flash_success is not None:
                schedule_ui(flash_success)
        return True
    except Exception as exc:
        logger.exception(f"[MEMO] Capture failed: {exc}")
        if hasattr(app, 'play_sound'):
            app.play_sound("error")
        return False
