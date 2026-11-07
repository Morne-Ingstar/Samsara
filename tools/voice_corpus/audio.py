"""Reuse the hands-free recorder's rolling PCM buffer without its console UI."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import numpy as np

from tools.probes.hf_corpus_record import RollingRecorder
from . import RATE
from .storage import external_path


def settings_snapshot(path=None):
    """Only a caller-selected offline snapshot; never discover a live profile."""
    if path is None:
        return {}
    data = json.loads(external_path(path).read_text(encoding="utf-8-sig"))
    return {key: data[key] for key in ("microphone", "microphone_name", "model_path", "device", "compute_type")
            if key in data}


def local_model(path=None, *, device="cpu", compute_type="int8"):
    if path is None:
        from huggingface_hub import try_to_load_from_cache
        cached = try_to_load_from_cache("Systran/faster-whisper-base", "model.bin")
        if not isinstance(cached, str):
            raise ValueError("A cached local base model is needed for voice controls; no download was attempted")
        path = Path(cached).parent
    path = Path(path).resolve()
    if not (path / "model.bin").is_file():
        raise ValueError("Choose a local faster-whisper folder containing model.bin")
    from faster_whisper import WhisperModel
    return WhisperModel(str(path), device=device, compute_type=compute_type,
                        cpu_threads=4, num_workers=1, local_files_only=True)


def control_phrase(model, pcm):
    if len(pcm) / RATE > 4:
        return None
    segments, _ = model.transcribe(np.asarray(pcm, dtype=np.float32) / 32768,
                                  language="en", beam_size=3, condition_on_previous_text=False)
    text = " ".join(s.text.strip() for s in segments).strip().casefold().rstrip(".!?")
    return text if text in {"again", "skip", "pause session"} else None


class Capture(RollingRecorder):
    def __init__(self, settings=None):
        super().__init__(lead_in_s=.4)
        self.settings = settings or {}
        self.statuses = []
        self.tail_done = threading.Event()

    def _callback(self, indata, frames, stamp, status):
        # Do not print PortAudio exceptions or private device details to logs.
        if status:
            self.statuses.append("input overflow or stream warning")
        super()._callback(indata, frames, stamp, None)

    def __enter__(self):
        import sounddevice as sd
        device = self.settings.get("microphone_name") or self.settings.get("microphone")
        self.device = sd.query_devices(device, "input")
        sd.check_input_settings(device=device, channels=1, samplerate=RATE, dtype="float32")
        self._stream = sd.InputStream(device=device, samplerate=RATE, channels=1,
                                      dtype="float32", callback=self._callback)
        self._stream.__enter__()
        return self

    def start_take(self):
        self.statuses.clear()
        self.tail_done.clear()
        super().start_take()
        self.pressed_at = time.monotonic()

    def release(self):
        held_s = time.monotonic() - self.pressed_at
        release_s = self._press_offset_s + held_s
        # Keep the full maximum tail. Replay applies the production quiet gate.
        self.tail_done.wait(1.2)
        samples, _, _ = self.finish_take()
        return np.asarray(samples, dtype="<i2"), release_s, list(self.statuses)

    def __exit__(self, *exc):
        self.tail_done.set()
        return super().__exit__(*exc)
