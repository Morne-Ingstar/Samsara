"""Level metering and short recordings for guide windows (voice training,
setup wizards) that must NOT open a second PortAudio stream on a device the
AudioCaptureEngine already owns.

A second stream on the same device is how the voice-training guide hit
``PortAudioError: Invalid sample rate [-9997]`` on a Focusrite while ACE held
it (2026-09-13 log). When ACE is running, guides read its ring instead; only
when it is not running may a guide open its own transient stream, at the
device's own rate (samsara.audio_devices.detect_capture_rate).

The ACE bus is 16 kHz int16 (frame.SAMPLE_RATE), so audio read here is
already at the Whisper model rate -- no resampling step.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import numpy as np

from .frame import SAMPLE_RATE
from .ring import EMPTY

#: Per-tick decay of the displayed level when no new frame arrived, so the
#: meter falls smoothly instead of freezing at the last value.
LEVEL_DECAY = 0.85


def running_engine(app):
    """The app's AudioCaptureEngine if it is running, else None."""
    ace = getattr(app, "_ace_engine", None) if app is not None else None
    if ace is not None and getattr(ace, "_running", False):
        return ace
    return None


def drain_float(reader) -> np.ndarray:
    """Every frame the reader has not seen yet, as float32 in [-1, 1]."""
    chunks = []
    while True:
        frame = reader.read_next()
        if frame is EMPTY:
            break
        chunks.append(frame.pcm.astype(np.float32) / 32767.0)
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


def to_model_rate(audio: np.ndarray, orig_sr: int) -> np.ndarray:
    """Linear resample of a transient-stream recording to the 16 kHz model
    rate (ring audio is already 16 kHz and never needs this)."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if orig_sr == SAMPLE_RATE or audio.size == 0:
        return audio
    new_len = int(audio.size * SAMPLE_RATE / orig_sr)
    return np.interp(
        np.linspace(0, audio.size - 1, num=new_len),
        np.arange(audio.size),
        audio,
    ).astype(np.float32)


def block_rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block * block)))


class RingLevelMeter:
    """A registered ring consumer that reports the latest RMS level."""

    def __init__(self, engine, name: str):
        self._engine = engine
        self._reader = engine.register_consumer(name)
        self.last_rms = 0.0

    def read_rms(self) -> float:
        """RMS of the frames since the previous call (decayed when none)."""
        block = drain_float(self._reader)
        if block.size:
            self.last_rms = block_rms(block)
        else:
            self.last_rms *= LEVEL_DECAY
        return self.last_rms

    def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            self._engine.unregister_consumer(reader)


def record_from_ring(
    engine,
    seconds: float,
    name: str,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
    stall_timeout: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> np.ndarray:
    """Record ``seconds`` of 16 kHz float32 audio from the engine's ring.

    Starts at the write head (no pre-trigger history). Raises TimeoutError if
    no audio arrives for ``stall_timeout`` seconds (engine stopped or device
    lost) so the caller can show the failure instead of a silent result.
    Returns early (shorter audio) when ``should_stop()`` becomes true.
    """
    needed = int(round(seconds * SAMPLE_RATE))
    reader = engine.register_consumer(name)
    try:
        parts = []
        got = 0
        last_audio = clock()
        while got < needed:
            block = drain_float(reader)
            if block.size:
                parts.append(block)
                got += block.size
                last_audio = clock()
                continue
            if should_stop is not None and should_stop():
                break
            if clock() - last_audio > stall_timeout:
                raise TimeoutError(
                    f"no audio from the microphone engine for {stall_timeout:.0f} s"
                )
            sleep(0.02)
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)[:needed]
    finally:
        engine.unregister_consumer(reader)
