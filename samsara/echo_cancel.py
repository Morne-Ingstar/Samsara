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
    """Frequency-domain block NLMS adaptive filter for echo cancellation.

    Uses overlap-save method with FFT for efficient convolution.
    Processes entire blocks at once -- no Python for-loops in the signal path.
    """

    def __init__(self, block_size=1024, filter_blocks=4, step_size=0.02,
                 regularization=1e-6):
        """
        Args:
            block_size: Samples per processing block (power of 2). At 16kHz,
                1024 = 64ms per block.
            filter_blocks: Number of filter blocks. Total filter length =
                block_size * filter_blocks. 4 blocks x 1024 = 4096 taps = 256ms
                of echo path modeling at 16kHz.
            step_size: NLMS step size (mu). Start conservative at 0.02.
            regularization: Small constant to avoid division by zero.
        """
        self.block_size = block_size
        self.filter_blocks = filter_blocks
        self.step_size = step_size
        self.reg = regularization

        self.fft_size = 2 * block_size  # overlap-save FFT size
        n_bins = self.fft_size // 2 + 1

        # Frequency-domain filter weights (complex)
        self._W = np.zeros((filter_blocks, n_bins), dtype=np.complex64)

        # Reference signal history (frequency domain)
        self._ref_history = np.zeros((filter_blocks, n_bins), dtype=np.complex64)

        # Input buffers for overlap-save
        self._mic_buffer = np.zeros(self.fft_size, dtype=np.float32)
        self._ref_buffer = np.zeros(self.fft_size, dtype=np.float32)

        # Output accumulator for chunks smaller than block_size
        self._out_buffer = np.zeros(0, dtype=np.float32)

    def process(self, mic, ref):
        """Process audio block, removing echo.

        Handles arbitrary input sizes by buffering internally.
        All heavy computation uses numpy FFT -- no Python for-loops in signal path.
        """
        if len(mic) == 0:
            return mic

        n = min(len(mic), len(ref))
        mic = mic[:n].astype(np.float32)
        ref = ref[:n].astype(np.float32)

        result = np.empty(n, dtype=np.float32)
        pos = 0

        while pos < n:
            chunk = min(self.block_size, n - pos)

            # Shift buffers and add new data
            self._mic_buffer[:-chunk] = self._mic_buffer[chunk:]
            self._mic_buffer[-chunk:] = mic[pos:pos + chunk]

            self._ref_buffer[:-chunk] = self._ref_buffer[chunk:]
            self._ref_buffer[-chunk:] = ref[pos:pos + chunk]

            if chunk == self.block_size:
                # Full block -- process with FFT
                result[pos:pos + chunk] = self._process_block()
            else:
                # Partial block -- pass through (will be processed next call)
                result[pos:pos + chunk] = mic[pos:pos + chunk]

            pos += chunk

        return result

    def _process_block(self):
        """Process one full block using overlap-save frequency-domain NLMS."""
        B = self.block_size

        # FFT of current mic and reference blocks
        Mic = np.fft.rfft(self._mic_buffer)
        Ref = np.fft.rfft(self._ref_buffer)

        # Shift reference history and store new block
        self._ref_history[1:] = self._ref_history[:-1]
        self._ref_history[0] = Ref

        # Estimate echo: sum of W[i] * ref_history[i] in frequency domain
        Y_hat = np.sum(self._W * self._ref_history, axis=0)

        # Error in frequency domain
        E = Mic - Y_hat

        # Convert error to time domain (overlap-save: keep last block_size samples)
        error_td = np.fft.irfft(E, n=self.fft_size)
        cleaned = error_td[-B:]

        # NLMS weight update (frequency domain)
        ref_power = np.sum(np.abs(self._ref_history) ** 2, axis=0) + self.reg

        # Update weights -- one vectorized operation per filter block
        for i in range(self.filter_blocks):
            self._W[i] += self.step_size * (E * np.conj(self._ref_history[i])) / ref_power

        return cleaned.astype(np.float32)

    def reset(self):
        """Reset the adaptive filter."""
        self._W[:] = 0
        self._ref_history[:] = 0
        self._mic_buffer[:] = 0
        self._ref_buffer[:] = 0


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

    _FILTER_RATE = 16000  # adaptive filter always runs at 16kHz

    def __init__(
        self,
        sample_rate: int = 16000,
        enabled: bool = True,
        block_size: int = 1024,
        filter_blocks: int = 4,
        step_size: float = 0.02,
        latency_ms: float = 30.0,
    ):
        """
        Args:
            sample_rate: Mic capture rate (Hz). Audio is downsampled to 16kHz
                for filtering, then upsampled back.
            enabled: Whether AEC is active.
            block_size: FFT block size (1024 @ 16kHz = 64ms per block).
            filter_blocks: Number of blocks (4 x 1024 = 256ms echo path).
            step_size: NLMS step size (0.02 is conservative).
            latency_ms: Estimated speaker-to-mic delay in milliseconds.
        """
        self.sample_rate = sample_rate
        self.enabled = enabled
        self.latency_ms = latency_ms

        # Loopback captures at 16kHz (matches filter rate)
        self._loopback = LoopbackCapture(
            target_rate=self._FILTER_RATE, buffer_seconds=2.0
        )
        self._aec = AdaptiveEchoCanceller(
            block_size=block_size, filter_blocks=filter_blocks,
            step_size=step_size,
        )
        self._started = False

        # Latency offset in 16kHz samples (reference is at filter rate)
        self._latency_samples = int(self._FILTER_RATE * latency_ms / 1000.0)

        # Diagnostic logging counter
        self._process_count = 0

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

        Downsamples to 16kHz for filtering, then upsamples back to capture rate.

        Args:
            mic_audio: float32 mono mic audio at self.sample_rate.

        Returns:
            Cleaned audio (same shape/dtype), or the original if AEC
            is not active.
        """
        if not self.is_active:
            return mic_audio

        original_shape = mic_audio.shape
        mic_flat = mic_audio.flatten()

        # Downsample mic to 16kHz for filtering
        if self.sample_rate != self._FILTER_RATE:
            mic_16k = self._resample(mic_flat, self.sample_rate, self._FILTER_RATE)
        else:
            mic_16k = mic_flat

        n = len(mic_16k)

        # Pull reference audio from loopback (already at 16kHz)
        ref = self._loopback.get_recent(n + self._latency_samples)

        # Align reference to compensate for speaker-to-mic delay
        if len(ref) >= n + self._latency_samples:
            ref = ref[:n]
        elif len(ref) >= n:
            ref = ref[:n]
        else:
            return mic_audio  # not enough reference yet

        # Skip if system audio is silent
        ref_rms = float(np.sqrt(np.mean(ref ** 2)))
        if ref_rms < 1e-6:
            return mic_audio

        # Apply adaptive filter at 16kHz
        cleaned_16k = self._aec.process(mic_16k, ref)

        # Upsample back to capture rate
        if self.sample_rate != self._FILTER_RATE:
            cleaned = self._resample(cleaned_16k, self._FILTER_RATE, self.sample_rate)
            # Match original length exactly
            orig_len = len(mic_flat)
            if len(cleaned) > orig_len:
                cleaned = cleaned[:orig_len]
            elif len(cleaned) < orig_len:
                cleaned = np.pad(cleaned, (0, orig_len - len(cleaned)))
        else:
            cleaned = cleaned_16k

        # Periodic diagnostic logging (every 100 chunks ~ every 10s)
        self._process_count += 1
        if self._process_count % 100 == 1:
            mic_rms = float(np.sqrt(np.mean(mic_16k ** 2)))
            cleaned_rms = float(np.sqrt(np.mean(cleaned_16k ** 2)))
            print(f"[AEC] ref_rms={ref_rms:.6f} mic_rms={mic_rms:.6f} "
                  f"cleaned_rms={cleaned_rms:.6f}")

        return cleaned.reshape(original_shape)

    @staticmethod
    def _resample(audio, src_rate, dst_rate):
        """Linear-interpolation resample (matches LoopbackCapture._resample)."""
        if src_rate == dst_rate:
            return audio
        ratio = dst_rate / src_rate
        n_out = int(len(audio) * ratio)
        indices = np.arange(n_out) / ratio
        indices = np.clip(indices, 0, len(audio) - 1)
        idx_floor = indices.astype(np.intp)
        idx_ceil = np.minimum(idx_floor + 1, len(audio) - 1)
        frac = indices - idx_floor
        return (audio[idx_floor] * (1 - frac) + audio[idx_ceil] * frac).astype(np.float32)

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
        self._latency_samples = int(self._FILTER_RATE * latency_ms / 1000.0)
