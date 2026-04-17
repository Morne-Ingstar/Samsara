"""
Samsara Echo Cancellation Module

Uses WASAPI loopback capture to grab system audio output,
then applies an adaptive filter (NLMS) to subtract it from
the microphone signal before transcription.

Windows-only. Degrades gracefully on other platforms or when
loopback capture is unavailable.
"""

import logging
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

logger = logging.getLogger("Samsara")

# Optional dependency — only available on Windows
try:
    import pyaudiowpatch as pyaudio
    HAS_PYAUDIO = True
except ImportError:
    pyaudio = None
    HAS_PYAUDIO = False


class LoopbackCapture:
    """Captures system audio output via WASAPI loopback.

    Runs in a background thread, continuously filling a ring buffer
    with the latest system audio (resampled to a target sample rate,
    mono, float32).
    """

    def __init__(self, target_rate: int = 16000, buffer_seconds: float = 2.0):
        self.target_rate = target_rate
        self.buffer_seconds = buffer_seconds

        self._pa: Optional[object] = None
        self._stream: Optional[object] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

        # Ring buffer of float32 mono samples at target_rate
        buf_size = int(target_rate * buffer_seconds)
        self._buffer = np.zeros(buf_size, dtype=np.float32)
        self._write_pos = 0  # next write index (wraps around)

        # Source device info (populated on start)
        self._device_rate: int = 0
        self._device_channels: int = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        """Start loopback capture.  Returns True on success."""
        if self._running:
            return True
        if not HAS_PYAUDIO:
            logger.warning("[AEC] PyAudioWPatch not available — echo cancellation disabled")
            return False

        try:
            self._pa = pyaudio.PyAudio()
            wasapi_info = None

            # Find WASAPI host API
            for i in range(self._pa.get_host_api_count()):
                api = self._pa.get_host_api_info_by_index(i)
                if api["name"].lower().startswith("windows wasapi"):
                    wasapi_info = api
                    break

            if wasapi_info is None:
                logger.warning("[AEC] WASAPI host API not found")
                self._cleanup_pa()
                return False

            # Find the default output device's loopback
            default_output = self._pa.get_device_info_by_index(
                wasapi_info["defaultOutputDevice"]
            )

            # PyAudioWPatch exposes loopback devices — find the one
            # matching the default output.
            loopback_device = None
            for i in range(self._pa.get_device_count()):
                dev = self._pa.get_device_info_by_index(i)
                if dev.get("isLoopbackDevice") and dev["name"].startswith(
                    default_output["name"]
                ):
                    loopback_device = dev
                    break

            if loopback_device is None:
                logger.warning(
                    "[AEC] No loopback device found for '%s'", default_output["name"]
                )
                self._cleanup_pa()
                return False

            self._device_rate = int(loopback_device["defaultSampleRate"])
            self._device_channels = int(loopback_device["maxInputChannels"])

            frames_per_buffer = int(self._device_rate * 0.05)  # 50 ms chunks

            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=self._device_channels,
                rate=self._device_rate,
                input=True,
                input_device_index=loopback_device["index"],
                frames_per_buffer=frames_per_buffer,
                stream_callback=self._stream_callback,
            )
            self._stream.start_stream()
            self._running = True
            logger.info(
                "[AEC] Loopback capture started: %s @ %d Hz, %d ch",
                loopback_device["name"],
                self._device_rate,
                self._device_channels,
            )
            return True

        except Exception as e:
            logger.warning("[AEC] Failed to start loopback capture: %s", e)
            self._cleanup_pa()
            return False

    def stop(self):
        """Stop loopback capture and release resources."""
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._cleanup_pa()
        logger.info("[AEC] Loopback capture stopped")

    def get_recent(self, num_samples: int) -> np.ndarray:
        """Return the most recent *num_samples* of loopback audio (float32 mono, target_rate)."""
        with self._lock:
            buf_len = len(self._buffer)
            n = min(num_samples, buf_len)
            start = (self._write_pos - n) % buf_len
            if start + n <= buf_len:
                return self._buffer[start : start + n].copy()
            else:
                part1 = self._buffer[start:]
                part2 = self._buffer[: (start + n) - buf_len]
                return np.concatenate([part1, part2])

    # ---- internal ----

    def _stream_callback(self, in_data, frame_count, time_info, status):
        """PyAudio callback — runs on audio thread."""
        if not self._running:
            return (None, pyaudio.paComplete)

        try:
            audio = np.frombuffer(in_data, dtype=np.float32)

            # Convert to mono if needed
            if self._device_channels > 1:
                audio = audio.reshape(-1, self._device_channels).mean(axis=1)

            # Resample to target rate if needed
            if self._device_rate != self.target_rate:
                audio = self._resample(audio, self._device_rate, self.target_rate)

            # Write into ring buffer
            with self._lock:
                n = len(audio)
                buf_len = len(self._buffer)
                end = self._write_pos + n
                if end <= buf_len:
                    self._buffer[self._write_pos : end] = audio
                else:
                    first = buf_len - self._write_pos
                    self._buffer[self._write_pos :] = audio[:first]
                    self._buffer[: n - first] = audio[first:]
                self._write_pos = end % buf_len

        except Exception:
            pass  # never crash the audio callback

        return (None, pyaudio.paContinue)

    @staticmethod
    def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        """Simple linear-interpolation resample (fast, good enough for AEC reference)."""
        if src_rate == dst_rate:
            return audio
        ratio = dst_rate / src_rate
        n_out = int(len(audio) * ratio)
        indices = np.arange(n_out) / ratio
        # Clamp to valid range
        indices = np.clip(indices, 0, len(audio) - 1)
        idx_floor = indices.astype(np.intp)
        idx_ceil = np.minimum(idx_floor + 1, len(audio) - 1)
        frac = indices - idx_floor
        return audio[idx_floor] * (1 - frac) + audio[idx_ceil] * frac

    def _cleanup_pa(self):
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


class AdaptiveEchoCanceller:
    """NLMS adaptive filter for echo cancellation.

    Subtracts an estimated echo from the microphone signal using the
    loopback reference.  The filter adapts continuously so it tracks
    changes in the acoustic path (speaker → mic coupling, latency, EQ).

    Typical usage::

        aec = AdaptiveEchoCanceller(filter_length=800, step_size=0.5)
        cleaned = aec.process(mic_chunk, ref_chunk)
    """

    def __init__(
        self,
        filter_length: int = 800,
        step_size: float = 0.3,
        regularization: float = 1e-6,
    ):
        """
        Args:
            filter_length: Number of taps in the adaptive filter.
                At 16 kHz, 800 taps ≈ 50 ms of echo path modeling.
            step_size: NLMS step size (μ).  0.0–1.0; higher = faster
                adaptation but more noise.  0.3 is a good default.
            regularization: Small constant to avoid division by zero.
        """
        self.filter_length = filter_length
        self.step_size = step_size
        self.reg = regularization

        # Adaptive filter weights
        self._w = np.zeros(filter_length, dtype=np.float32)
        # Reference signal delay line
        self._ref_buf = np.zeros(filter_length, dtype=np.float32)

    def process(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        """Process a chunk of audio.

        Args:
            mic: Microphone signal (float32, mono, any length).
            ref: Loopback reference signal (float32, mono, same length as mic).

        Returns:
            Cleaned microphone signal with echo subtracted.
        """
        if len(mic) == 0:
            return mic

        # Ensure matching lengths (truncate the longer one)
        n = min(len(mic), len(ref))
        mic = mic[:n].astype(np.float32)
        ref = ref[:n].astype(np.float32)

        out = np.empty(n, dtype=np.float32)
        w = self._w
        ref_buf = self._ref_buf
        fl = self.filter_length
        mu = self.step_size
        reg = self.reg

        for i in range(n):
            # Shift reference into delay line
            ref_buf = np.roll(ref_buf, 1)
            ref_buf[0] = ref[i]

            # Estimate echo: y_hat = w · ref_buf
            y_hat = np.dot(w, ref_buf)

            # Error = mic - echo_estimate  (this is the cleaned signal)
            error = mic[i] - y_hat
            out[i] = error

            # NLMS weight update
            power = np.dot(ref_buf, ref_buf) + reg
            w = w + (mu * error / power) * ref_buf

        # Save state for next chunk
        self._w = w
        self._ref_buf = ref_buf

        return out

    def reset(self):
        """Reset the adaptive filter (e.g. when switching audio devices)."""
        self._w[:] = 0
        self._ref_buf[:] = 0


class EchoCanceller:
    """High-level echo cancellation manager for Samsara.

    Combines loopback capture with an adaptive filter.  Designed to be
    called from the microphone audio callbacks.

    Usage::

        ec = EchoCanceller(sample_rate=16000, enabled=True)
        ec.start()   # begins loopback capture in background

        # In audio callback:
        cleaned = ec.process(mic_chunk_float32)

        ec.stop()    # when done
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        enabled: bool = True,
        filter_length: int = 800,
        step_size: float = 0.3,
        latency_ms: float = 30.0,
    ):
        """
        Args:
            sample_rate: Mic sample rate (Hz).
            enabled: Whether AEC is active.
            filter_length: Adaptive filter taps (800 @ 16kHz = 50ms).
            step_size: NLMS step size.
            latency_ms: Estimated system latency (speaker→mic delay)
                in milliseconds.  Used to align the reference signal.
        """
        self.sample_rate = sample_rate
        self.enabled = enabled
        self.latency_ms = latency_ms

        self._loopback = LoopbackCapture(
            target_rate=sample_rate, buffer_seconds=2.0
        )
        self._aec = AdaptiveEchoCanceller(
            filter_length=filter_length, step_size=step_size
        )
        self._started = False

        # Track how many mic samples we've processed so we can pull
        # the right amount of reference audio
        self._latency_samples = int(sample_rate * latency_ms / 1000.0)

    @property
    def is_active(self) -> bool:
        """True if AEC is enabled AND loopback is running."""
        return self.enabled and self._started and self._loopback.is_running

    def start(self) -> bool:
        """Start the echo canceller (loopback capture).

        Returns True if loopback capture started successfully.
        If it fails, AEC will be silently disabled.
        """
        if not self.enabled:
            return False
        if self._started:
            return True

        ok = self._loopback.start()
        if ok:
            self._started = True
            self._aec.reset()
            logger.info("[AEC] Echo cancellation active")
        else:
            logger.warning("[AEC] Could not start — continuing without echo cancellation")
        return ok

    def stop(self):
        """Stop the echo canceller."""
        if self._started:
            self._loopback.stop()
            self._started = False

    def process(self, mic_audio: np.ndarray) -> np.ndarray:
        """Process a microphone audio chunk, removing system audio echo.

        Args:
            mic_audio: float32 mono mic audio (any length).

        Returns:
            Cleaned audio (same shape/dtype), or the original if AEC
            is not active.
        """
        if not self.is_active:
            return mic_audio

        n = len(mic_audio.flatten())

        # Pull the matching amount of reference audio from the ring buffer,
        # accounting for latency offset
        ref = self._loopback.get_recent(n + self._latency_samples)

        # Take the older portion (aligned to when the mic actually picked
        # up the echo)
        if len(ref) >= n + self._latency_samples:
            ref = ref[: n]
        elif len(ref) >= n:
            ref = ref[: n]
        else:
            # Not enough reference data yet — pass through
            return mic_audio

        # Check if reference has any energy (if system is silent, skip AEC
        # to avoid unnecessary filter noise)
        ref_rms = np.sqrt(np.mean(ref ** 2))
        if ref_rms < 1e-6:
            return mic_audio

        original_shape = mic_audio.shape
        cleaned = self._aec.process(mic_audio.flatten(), ref)
        return cleaned.reshape(original_shape)

    def set_enabled(self, enabled: bool):
        """Enable or disable AEC at runtime."""
        if enabled and not self.enabled:
            self.enabled = True
            self.start()
        elif not enabled and self.enabled:
            self.enabled = False
            self.stop()

    def set_latency(self, latency_ms: float):
        """Update the latency compensation."""
        self.latency_ms = latency_ms
        self._latency_samples = int(self.sample_rate * latency_ms / 1000.0)
