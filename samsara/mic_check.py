"""A short, explicit quiet/phrase microphone check. No Qt or device on import.

Levels describe the captured signal, not recognition accuracy. PCM and heard
words stay in memory; the last diagnostics result contains only measurements.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import uuid

import numpy as np

SAMPLE_RATE = 16000  # Both mic_score sources supply model-rate PCM.
FRAME_S = .1
QUIET_S = 1.2
PHRASE_S = 8
STALL_S = 2
CLIP_SAMPLE = .98
CLIP_FRACTION = .001
FULL_SCALE = .999
REPEATED_FULL_SCALE_FRAMES = 3
# mic_score.signal_summary uses .005 RMS = -46.02 dBFS. This is a
# diagnostic guide, not proof of why a particular utterance failed. The
# speech_presence gate uses VAD probability/duration, not a loudness cutoff.
QUIET_RMS = .005
QUIET_DBFS = 20 * math.log10(QUIET_RMS)
NOISY_SNR_DB = 15
NOISE_LOW_DBFS = -50
NOISE_HIGH_DBFS = -35
MIN_SPEECH_FRAMES = 3
SENTENCE = "The quick brown fox jumps over the lazy dog."
# PLACEHOLDER COPY (1120)
VERDICTS = {
    "too_loud": "Too loud — your input is clipping. Lower the microphone input level in Windows sound settings.",
    "too_quiet": "Too quiet — your voice is faint. Move the microphone closer to your mouth.",
    "noisy": "Noisy — background sound is close to your voice. Move away from the noise and check again.",
    "good": "Good — your level is clear of clipping and background noise. Keep this microphone position.",
    "no_speech": "No clear speech measured. Say the sentence after the quiet step and check again.",
}


def dbfs(rms):
    return 20 * math.log10(max(float(rms), 1e-6))


def pcm_float(audio):
    pcm = np.asarray(audio)
    if pcm.ndim != 1 or pcm.dtype.kind not in "fi" or not np.isfinite(pcm).all():
        raise ValueError("Microphone audio was invalid. Check again.")
    if pcm.dtype.kind == "i":
        pcm = pcm.astype(np.float64) / max(abs(np.iinfo(pcm.dtype).min), np.iinfo(pcm.dtype).max)
    return pcm.astype(np.float32)


def measure(audio):
    pcm = pcm_float(audio)
    if not pcm.size:
        raise ValueError("No microphone audio arrived. Check the connection and try again.")
    absolute = np.abs(pcm.astype(np.float64))
    rms = float(np.sqrt(np.mean(absolute ** 2)))
    return dict(rms=rms, rms_dbfs=dbfs(rms), peak=float(absolute.max()),
                clipped_fraction=float(np.mean(absolute >= CLIP_SAMPLE)))


class LiveLevels:
    """Emit exact 100 ms windows even when source blocks straddle boundaries."""
    def __init__(self, sample_rate=SAMPLE_RATE):
        self.frame_size = round(sample_rate * FRAME_S)
        self.pending = np.empty(0, np.float32)

    def feed(self, audio):
        self.pending = np.concatenate((self.pending, pcm_float(audio)))
        frames = []
        while self.pending.size >= self.frame_size:
            frames.append(measure(self.pending[:self.frame_size]))
            self.pending = self.pending[self.frame_size:]
        return frames


def noise_label(noise_dbfs):
    if noise_dbfs is None:
        return "not measured"
    return "low" if noise_dbfs < NOISE_LOW_DBFS else "high" if noise_dbfs >= NOISE_HIGH_DBFS else "moderate"


def evaluate(audio, quiet):
    """Explicit quiet reference plus upper-quartile phrase frames exclude pauses.

    A single spike is never repeated clipping. Three speech-bearing frames
    are required for a reassuring result; constant room noise is inconclusive.
    """
    audio, quiet = pcm_float(audio), pcm_float(quiet)
    phrase, background = measure(audio), measure(quiet)
    frames = LiveLevels().feed(audio)
    levels = [f["rms"] for f in frames]
    active = [rms for rms in levels if rms > max(background["rms"] * 1.8, 1e-5)]
    speech_rms = float(np.percentile(active, 75)) if active else phrase["rms"]
    noise_db = background["rms_dbfs"]
    snr = dbfs(speech_rms) - noise_db
    full_frames = sum(f["peak"] >= FULL_SCALE for f in frames)
    speech_frames = len(active)
    if phrase["clipped_fraction"] > CLIP_FRACTION or full_frames >= REPEATED_FULL_SCALE_FRAMES:
        verdict = "too_loud"
    elif speech_frames < MIN_SPEECH_FRAMES:
        verdict = "no_speech"
    elif speech_rms < QUIET_RMS:
        verdict = "too_quiet"
    elif snr < NOISY_SNR_DB:
        verdict = "noisy"
    else:
        verdict = "good"
    return dict(verdict=verdict, message=VERDICTS[verdict], speech_rms=speech_rms,
                speech_dbfs=dbfs(speech_rms), noise_dbfs=noise_db, snr_db=snr,
                noise_level=noise_label(noise_db), full_scale_frames=full_frames,
                duration_s=len(audio) / SAMPLE_RATE, **phrase)


def save_result(result, microphone, path=None):
    """Allowlist metrics: no audio, transcript, prompts or exception text."""
    if path is None:
        from samsara.paths import samsara_home_dir
        path = samsara_home_dir() / "diagnostics" / "mic_check.json"
    path = Path(path)
    keys = ("verdict", "speech_rms", "speech_dbfs", "noise_dbfs", "snr_db", "noise_level",
            "full_scale_frames", "duration_s", "rms", "rms_dbfs", "peak", "clipped_fraction")
    record = {key: result[key] for key in keys}
    record.update(schema=1, microphone_name=microphone, time=datetime.now(timezone.utc).isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        pending.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def selected_device(app):
    from samsara.mic_score import microphone_name
    name = microphone_name(app)
    config = app.config
    device_id = config.get("microphone")
    return {"id": device_id, "name": name or (config.get("microphone_name") if device_id is not None else None) or "System default microphone"}


def capture(source, cancel, emit, *, clock=time.monotonic, sleep=time.sleep):
    """Bound capture to sample counts; stall/cancel never become a good verdict."""
    quiet, speech = [], []
    quiet_count = speech_count = 0
    quiet_needed, phrase_needed = round(QUIET_S * SAMPLE_RATE), round(PHRASE_S * SAMPLE_RATE)
    meter = LiveLevels()
    source.pull()  # Discard any buffered/pre-trigger speech.
    last_audio = clock()
    emit("quiet", None)
    while speech_count < phrase_needed:
        if cancel.is_set():
            raise InterruptedError
        block = pcm_float(source.pull())
        if not block.size:
            if clock() - last_audio > STALL_S:
                raise RuntimeError("No microphone audio arrived. Check the connection and try again.")
            sleep(.02)
            continue
        last_audio = clock()
        for frame in meter.feed(block):
            emit("level", frame)
        if quiet_count < quiet_needed:
            take = min(len(block), quiet_needed - quiet_count)
            quiet.append(block[:take])
            quiet_count += take
            block = block[take:]
            if quiet_count == quiet_needed:
                emit("noise", measure(np.concatenate(quiet))["rms_dbfs"])
                emit("speak", None)
        if block.size:
            take = min(len(block), phrase_needed - speech_count)
            speech.append(block[:take])
            speech_count += take
    if cancel.is_set():
        raise InterruptedError
    return np.concatenate(speech), np.concatenate(quiet)
