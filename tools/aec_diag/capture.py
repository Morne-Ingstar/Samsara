"""Audio capture/playback helpers for AEC diagnostics."""

from __future__ import annotations

import threading
from collections import deque

import numpy as np


class AudioRingBuffer:
    """Bounded ring of float32 mono blocks (1D samples)."""

    def __init__(self, sample_rate: int, max_seconds: float = 10.0):
        self.sample_rate = sample_rate
        self.max_samples = max(1, int(sample_rate * float(max_seconds)))
        self._max_seconds = max_seconds
        self._chunks = deque()
        self._total_samples = 0
        self._lock = threading.Lock()
        self.status_events = 0

    def append(self, samples: np.ndarray) -> None:
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return

        with self._lock:
            self._chunks.append(arr.copy())
            self._total_samples += arr.size
            while self._total_samples > self.max_samples and self._chunks:
                dropped = self._chunks.popleft()
                self._total_samples -= dropped.size

    def get_recent_seconds(self, seconds: float) -> np.ndarray:
        sample_count = int(float(seconds) * self.sample_rate)
        return self.get_recent_samples(sample_count)

    def get_recent_samples(self, sample_count: int) -> np.ndarray:
        with self._lock:
            if sample_count <= 0 or self._total_samples == 0:
                return np.zeros(0, dtype=np.float32)

            sample_count = min(sample_count, self._total_samples)
            remaining = sample_count
            parts = []

            for chunk in reversed(self._chunks):
                if remaining <= 0:
                    break
                take = min(remaining, chunk.size)
                if take:
                    parts.append(chunk[-take:])
                    remaining -= take

            parts.reverse()
            if not parts:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(parts)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._total_samples = 0

    @property
    def sample_count(self) -> int:
        return self._total_samples

    @property
    def window_seconds(self) -> float:
        return self._max_seconds

    def record_status(self) -> None:
        self.status_events += 1


class RingInputRecorder:
    """sounddevice input stream + ring buffer."""

    def __init__(self, sample_rate: int, device, max_seconds: float, name: str):
        self.sample_rate = sample_rate
        self.device = device
        self.name = name
        self.buffer = AudioRingBuffer(sample_rate=sample_rate, max_seconds=max_seconds)
        self._stream = None

    def start(self, sd_module) -> bool:
        def cb(indata, frames, time_info, status):
            if status:
                self.buffer.record_status()
            self.buffer.append(np.asarray(indata)[:, 0])

        try:
            self._stream = sd_module.InputStream(
                device=self.device,
                samplerate=self.sample_rate,
                channels=1,
                dtype=np.float32,
                blocksize=0,
                callback=cb,
            )
            self._stream.start()
            return True
        except Exception as exc:
            print(f"[AEC-DIAG] Failed to start {self.name} input stream: {exc}")
            self._stream = None
            return False

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def recent(self, seconds: float) -> np.ndarray:
        return self.buffer.get_recent_seconds(seconds)


def find_default_devices() -> tuple[int | None, int | None]:
    import sounddevice as sd

    default_input = None
    default_output = None

    try:
        default_device = sd.default.device
        if isinstance(default_device, tuple):
            default_input = default_device[0]
            default_output = default_device[1]
    except Exception:
        pass
    return default_input, default_output


def pick_rate_for_device(sd_module, device, rates: tuple[int, ...], is_input: bool = True) -> int | None:
    for rate in rates:
        try:
            if is_input:
                sd_module.check_input_settings(device=device, samplerate=rate, channels=1, dtype="float32")
            else:
                sd_module.check_output_settings(device=device, samplerate=rate, channels=2, dtype="float32")
            return int(rate)
        except Exception:
            continue
    return None


def find_wasapi_loopback_device(sd_module):
    try:
        host_apis = sd_module.query_hostapis()
        devices = sd_module.query_devices()
    except Exception:
        return None, None

    wasapi_idx = None
    for idx, api in enumerate(host_apis):
        if "wasapi" in str(api.get("name", "")).lower():
            wasapi_idx = idx
            break

    if wasapi_idx is None:
        return None, None

    default_output = sd_module.default.device[1] if sd_module.default.device else None
    output_name = ""
    if default_output is not None:
        try:
            output_name = str(sd_module.query_devices(default_output).get("name", ""))
        except Exception:
            output_name = ""

    candidates = []
    for idx, dev in enumerate(devices):
        if int(dev.get("hostapi", -1)) != wasapi_idx:
            continue
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        name = str(dev.get("name", "")).lower()
        score = 1 if "loopback" in name else 0
        if output_name and output_name.lower() in name:
            score += 2
        candidates.append((score, idx))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1], wasapi_idx


class LoopbackRecorder(RingInputRecorder):
    """Open WASAPI loopback stream as an input stream when available."""

    def __init__(
        self,
        sample_rate: int,
        max_seconds: float = 10.0,
        device: int | None = None,
    ):
        super().__init__(sample_rate=sample_rate, device=device, max_seconds=max_seconds, name="loopback")

    @staticmethod
    def select_device(sd_module) -> int | None:
        idx, _ = find_wasapi_loopback_device(sd_module)
        if idx is not None:
            return int(idx)
        return None

    def get_recent(self, seconds: float) -> np.ndarray:
        return self.recent(seconds)


def run_repeating_playback(
    sd_module,
    signal: np.ndarray,
    sample_rate: int,
    device: int | None,
    volume: float = 0.2,
) -> None:
    signal = np.clip(np.asarray(signal, dtype=np.float32), -1.0, 1.0)
    play = signal * float(volume)
    sd_module.play(play, samplerate=sample_rate, device=device, blocking=True)


def build_band_limited_chirp(
    sample_rate: int,
    duration_seconds: float = 0.30,
    f_start: float = 180.0,
    f_end: float = 7000.0,
    amplitude: float = 0.25,
) -> np.ndarray:
    n = max(1, int(sample_rate * duration_seconds))
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    k = duration_seconds / np.log(f_end / f_start)
    phase = 2.0 * np.pi * f_start * k * (np.exp(t / k) - 1.0)
    chirp = np.sin(phase).astype(np.float32)
    window = np.hanning(n).astype(np.float32)
    return chirp * window * float(amplitude)


def build_probe_waveform(sample_rate: int, duration_seconds: float, symbol_seconds: float = 0.25) -> np.ndarray:
    symbol = build_band_limited_chirp(sample_rate, symbol_seconds, amplitude=0.35)
    reps = max(1, int(np.ceil((sample_rate * duration_seconds) / symbol.size)))
    return np.tile(symbol, reps)[: int(sample_rate * duration_seconds)].astype(np.float32)

