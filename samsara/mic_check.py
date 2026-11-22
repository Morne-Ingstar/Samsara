"""A short, explicit quiet/phrase microphone check. No Qt or device on import.

PCM and heard words stay in memory. The last diagnostics result contains
only measurements.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
import uuid

import numpy as np

SAMPLE_RATE = 16000  # Both mic_score sources supply model-rate PCM.
FRAME_S = .1
QUIET_S = 5
PHRASE_S = 8
REPEAT_PAUSE_S = 2
REPEAT_S = 7
CHECK_TIMEOUT_MS = 28_000
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
REPEAT_SENTENCE = "Please pack my box with five dozen red mugs."
ROWS = ("Level", "Background", "Clarity", "First word")
VERDICTS = {
    "too_loud": "Lower the microphone input volume in Windows.",
    "too_quiet": "Move the microphone closer to your mouth.",
    "noisy": "Move away from the background noise and check again.",
    "good": "Your voice came through at a usable level.",
    "no_speech": "Wait for the cue to read the sentence, then check again.",
}


@dataclass(frozen=True)
class Finding:
    verdict: str
    sentence: str
    tone: str = "success"


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
    """Measure the signal before sending the take to the recognizer.

    Use the median quiet level for finding speech and an upper percentile for
    reporting noticeable noise. A brief lull should not hide a noisy room.
    """
    from samsara import mic_lab_dsp as dsp

    audio, quiet = pcm_float(audio), pcm_float(quiet)
    phrase, background = measure(audio), measure(quiet)
    frames = LiveLevels().feed(audio)
    quiet_levels = [f["rms"] for f in LiveLevels().feed(quiet)] or [background["rms"]]
    floor, upper = np.percentile(quiet_levels, [50, 90])
    active = [i for i, frame in enumerate(frames) if frame["rms"] > max(floor * 1.8, 1e-5)]
    speech_rms = float(np.percentile([frames[i]["rms"] for i in active], 75)) if active else phrase["rms"]
    noise_db = dbfs(upper)
    snr = dbfs(speech_rms) - noise_db
    full_frames = sum(f["peak"] >= FULL_SCALE for f in frames)
    clipping = dsp.clipping(audio, SAMPLE_RATE)
    # A very faint smooth waveform can fall within the plateau tolerance at
    # each peak. In that range the level check should report faintness.
    flattened_peaks = (phrase["peak"] >= QUIET_RMS
                       and clipping.value["flat_top_runs"] > dsp.THRESHOLDS["clip"][6])
    level = "good"
    if phrase["clipped_fraction"] > CLIP_FRACTION or full_frames >= REPEATED_FULL_SCALE_FRAMES or flattened_peaks:
        level = "too_loud"
    elif len(active) < MIN_SPEECH_FRAMES:
        level = "no_speech"
    elif speech_rms < QUIET_RMS:
        level = "too_quiet"

    quiet_floor = dsp.noise_floor(quiet, SAMPLE_RATE)
    quiet_muted = any(quiet_floor.details.get("gated", ())) and dbfs(floor) < dsp.THRESHOLDS["noise_dbfs"][0]
    # With a muted reference, look for the same near-silence between active
    # frames. Otherwise a pause alone does not establish automatic muting.
    muted_pause = False
    if quiet_muted and len(active) >= MIN_SPEECH_FRAMES:
        muted_pause = any(f["rms_dbfs"] < dsp.THRESHOLDS["noise_dbfs"][0]
                          for f in frames[active[0] + 1:active[-1]])
    gaps = dsp.dropouts(audio, SAMPLE_RATE).value
    # Ordinary pauses can go to zero on a gated input. Only attribute
    # speech-flanked zeros to missing audio when the quiet reference rules that out.
    dropout = bool(gaps and not quiet_muted and (
        gaps["zero_runs"] >= 2 or gaps["longest_s"] >= dsp.THRESHOLDS["dropout"][1]))
    possible_dropout = bool(gaps and gaps["zero_runs"] and not quiet_muted and not dropout)
    # A digital zero run at the cue is evidence of a gap only when the quiet
    # reference had signal. A person's reaction time does not zero the stream.
    nonzero = np.flatnonzero(audio)
    start_gap_ms = float(nonzero[0]) * 1000 / SAMPLE_RATE if nonzero.size and not quiet_muted else 0.
    bandwidth = dsp.upper_band_ratio(audio, SAMPLE_RATE) if len(active) >= MIN_SPEECH_FRAMES else None
    verdict = "noisy" if level == "good" and not quiet_muted and snr < NOISY_SNR_DB else level
    return dict(verdict=verdict, message=VERDICTS[verdict], speech_rms=speech_rms,
                speech_dbfs=dbfs(speech_rms), noise_dbfs=noise_db, snr_db=snr,
                noise_level=noise_label(noise_db), full_scale_frames=full_frames,
                level=level, muted_quiet=quiet_muted, gated=muted_pause, dropout=dropout,
                possible_dropout=possible_dropout, start_gap_ms=start_gap_ms,
                upper_band_db=bandwidth.value if bandwidth else None,
                narrowband=bool(bandwidth and bandwidth.verdict == "Bad"),
                duration_s=len(audio) / SAMPLE_RATE, **phrase)


def first_word_missing(target, heard):
    from samsara.mic_score import normalize

    expected, actual = normalize(target), normalize(heard or "")
    return actual[:1] == expected[1:2] and expected[0] not in actual[:2]


def checklist(result, heard, *, hands_free=False, repeat_heard=None, repeat_result=None):
    """Combine independent signal checks with the words the decoder returned."""
    from samsara.mic_lab_battery import word_score
    from samsara.mic_score import normalize

    level_key = result["level"]
    level = Finding(
        {"good": "Good", "too_loud": "Too loud", "too_quiet": "Too quiet", "no_speech": "No speech"}[level_key],
        VERDICTS[level_key], "success" if level_key == "good" else "error" if level_key == "too_loud" else "warning",
    )

    if result["gated"]:
        background = Finding("Cuts to silence", "Turn off automatic muting in your microphone software, if available.", "warning")
    elif result["muted_quiet"]:
        background = Finding("Unclear", "The quiet sample was silent, so background sound could not be measured.", "neutral")
    elif result["noise_level"] == "low":
        background = Finding("Quiet", "Little background sound came through.")
    elif level_key == "no_speech":
        background = Finding("Noise heard", "Background sound came through during the quiet step.", "neutral")
    elif result["snr_db"] < NOISY_SNR_DB:
        background = Finding("Too noisy", VERDICTS["noisy"], "warning")
    else:
        background = Finding("Voice stood out", "Your voice was well above the background sound.")

    unavailable = Finding("Unavailable", "Samsara could not check the words in this recording.", "neutral")
    clarity, first_word = unavailable, unavailable
    show_transcript = False
    if heard is not None:
        actual = normalize(heard)
        expected = normalize(SENTENCE)
        score = word_score(SENTENCE, heard)
        show_transcript = bool(actual and score["raw_errors"])
        if not actual:
            clarity = Finding("No words heard", "Read the sentence after the cue and check again.", "warning")
            first_word = Finding("Not checked", "Samsara heard no words to compare.", "neutral")
        elif score["first_word"]:
            first_word = Finding("Heard", "The first word came through.")
        elif first_word_missing(SENTENCE, heard):
            if first_word_missing(REPEAT_SENTENCE, repeat_heard):
                first_word = Finding("Missing twice", "Try a different microphone.", "warning")
            elif result["start_gap_ms"] >= 150:
                first_word = Finding("Missing", "The recording started with a gap, and Samsara missed the first word.", "warning")
            else:
                first_word = Finding("Missing", "Samsara missed the first word.", "warning")
        else:
            first_word = Finding("Different word", "Samsara heard a different first word.", "warning")
        if actual:
            only_first_missing = actual == expected[1:]
            if only_first_missing:
                clarity = Finding("Rest matched", "Samsara heard the rest of the sentence as written.")
            elif score["raw_errors"] == 0:
                clarity = Finding("Sentence matched", "Samsara heard the sentence as written.")
            else:
                verdict = "One word misheard" if score["raw_errors"] == 1 else "Words differed"
                clarity = Finding(verdict, "Play back the recording to check it.", "warning")

    repeated_gap = repeat_result and (repeat_result["dropout"] or repeat_result["possible_dropout"])
    if result["dropout"] or (result["possible_dropout"] and repeated_gap):
        clarity = Finding("Gaps in sound", "Reconnect the microphone and check again.", "error")
    elif result["narrowband"] and level_key == "good":
        sentence = ("Bluetooth hands-free limited sound quality." if hands_free
                    else "Try a different microphone if this recording sounds muffled.")
        clarity = Finding("Muffled", sentence, "warning")
    elif result["possible_dropout"] and repeat_result is None and clarity.tone == "success":
        clarity = Finding("Unclear", "A short gap in the recording could not be checked again.", "neutral")
    return (level, background, clarity, first_word), show_transcript


def inspect_microphone(name):
    """Read the matching Windows endpoint without changing its settings."""
    from samsara.mic_lab_endpoint import bluetooth_hands_free, inspect_endpoint, Observation

    observed = inspect_endpoint(name=name) if name != "System default microphone" else Observation()
    if not observed.known:
        return None, bluetooth_hands_free(name).value is True
    volume = observed.value.get("volume_scalar")
    return (float(volume) if isinstance(volume, (float, int)) and 0 <= volume <= 1 else None,
            observed.value["bluetooth_hands_free"].value is True)


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


def capture(source, cancel, emit, *, clock=time.monotonic, sleep=time.sleep, repeat=False):
    """Bound capture to sample counts; stall/cancel never become a good verdict."""
    quiet, speech = [], []
    quiet_count = speech_count = 0
    quiet_s, phrase_s = (REPEAT_PAUSE_S, REPEAT_S) if repeat else (QUIET_S, PHRASE_S)
    quiet_needed, phrase_needed = round(quiet_s * SAMPLE_RATE), round(phrase_s * SAMPLE_RATE)
    meter = LiveLevels()
    source.pull()  # Discard any buffered/pre-trigger speech.
    last_audio = clock()
    emit("repeat_pause" if repeat else "quiet", None)
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
                if not repeat:
                    emit("noise", measure(np.concatenate(quiet))["rms_dbfs"])
                emit("repeat_speak" if repeat else "speak", None)
        if block.size:
            take = min(len(block), phrase_needed - speech_count)
            speech.append(block[:take])
            speech_count += take
    if cancel.is_set():
        raise InterruptedError
    return np.concatenate(speech), np.concatenate(quiet)
