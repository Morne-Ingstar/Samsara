"""Reuse the hands-free recorder's rolling PCM buffer without its console UI."""
from __future__ import annotations

import json
import math
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
    settings = {key: data[key] for key in ("microphone", "microphone_name", "microphone_hostapi",
                "_capture_rate", "model_path", "device", "compute_type") if key in data}
    if "microphone" not in settings and "microphone_index" in data:
        settings["microphone"] = data["microphone_index"]
    return settings


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


class DailyCapture(Capture):
    """Exact snapshot endpoint; production callback, without engine lifecycle/recovery.

    The standalone runner isolates SAMSARA_HOME_DIR before production imports.
    No running application, live configuration or reference output is consulted.
    """

    device_epoch = 0

    def __enter__(self):
        import sounddevice as sd
        name = self.settings.get("microphone_name")
        api = self.settings.get("microphone_hostapi") or "Windows WASAPI"
        if not name or name in ("Default", "System default"):
            raise ValueError("Daily capture needs an offline snapshot with an exact microphone_name")
        apis = sd.query_hostapis()
        matches = [(index, info) for index, info in enumerate(sd.query_devices())
                   if info["max_input_channels"] > 0
                   and info["name"].strip().casefold() == name.strip().casefold()
                   and apis[info["hostapi"]]["name"].casefold() == api.casefold()]
        if len(matches) != 1:
            raise ValueError("Daily capture needs exactly one matching microphone and host API; no fallback opened")
        self.device_id, self.device = matches[0]
        self.hostapi = apis[self.device["hostapi"]]["name"]
        native_rate = int(self.device["default_samplerate"])
        # Match the engine's optional caller rate only for the recorded index.
        rate = self.settings.get("_capture_rate") if self.device_id == self.settings.get("microphone") else None
        self.native_rate = int(rate or native_rate)
        if self.native_rate <= 0:
            raise ValueError("Daily capture needs a positive native sample rate")
        from samsara.audio_engine.engine import AudioCaptureEngine, _design_polyphase_filter
        from samsara.audio_engine.frame import FRAME_MS
        self.blocksize = self.native_rate * FRAME_MS // 1000
        self._processor = AudioCaptureEngine(self)
        common = math.gcd(RATE, self.native_rate)
        up, down = RATE // common, self.native_rate // common
        self._processor._up, self._processor._down = up, down
        if (up, down) != (1, 1):
            # Pure design avoids the engine's profile/disk cache; same taps.
            state = _design_polyphase_filter(up, down)
            self._processor._resample_state = (*state, math.ceil(self.blocksize * up / down))
        sd.check_input_settings(device=self.device_id, channels=1, samplerate=self.native_rate, dtype="float32")
        self._stream = sd.InputStream(device=self.device_id, samplerate=self.native_rate, channels=1,
                                      dtype="float32", blocksize=self.blocksize, callback=self._callback)
        self._stream.__enter__()
        if self._stream.device != self.device_id or self._stream.samplerate != self.native_rate:
            self._stream.__exit__(None, None, None)
            raise ValueError("Daily capture opened an unexpected device or rate; stream closed")
        self.receipt = (f"capture-path=daily; device={self.device_id}; host API={self.hostapi}; "
                        f"input={self.native_rate} Hz; mono float32; block={self.blocksize}; output={RATE} Hz PCM16")
        print(self.receipt, flush=True)
        return self

    def _callback(self, indata, frames, stamp, status):
        if status:
            self.statuses.append("input overflow or stream warning")
        self._processor._on_audio_block(indata, frames, stamp, status)

    def write(self, pcm, stamp, epoch):
        # FrameBus sink: preserve production PCM16 exactly, including -32768.
        with self._lock:
            block = pcm.tolist()
            if self._samples is not None:
                self._samples.extend(block)
            self._ring.extend(block)

    def finish_take(self):
        with self._lock:
            pcm, self._samples = self._samples, None
            offset = self._press_offset_s
        return pcm, None, offset
