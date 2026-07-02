"""
Samsara Echo Cancellation Module

Uses WASAPI loopback capture to grab system audio output,
then applies an adaptive filter (NLMS) to subtract it from
the microphone signal before transcription.

Windows-only. Degrades gracefully on other platforms or when
loopback capture is unavailable.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
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


_AEC_CACHE_PATH = Path.home() / ".samsara" / "aec_latency_cache.json"


def _load_latency_cache() -> dict:
    """Return the AEC per-device latency cache, or {} on any read error."""
    try:
        with open(_AEC_CACHE_PATH, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_latency_cache(cache: dict) -> None:
    """Atomically write the latency cache (tmp + os.replace)."""
    try:
        _AEC_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _AEC_CACHE_PATH.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(cache, f, indent=2)
        os.replace(str(tmp), str(_AEC_CACHE_PATH))
    except OSError as e:
        logger.warning("[AEC] Failed to save latency cache: %s", e)


def _make_calibration_click(rate: int = 16000, duration_s: float = 0.05) -> np.ndarray:
    """Generate a Hann-windowed log chirp 200 Hz -> 4 kHz for lag calibration.

    A chirp is preferred over a click impulse: it deposits more acoustic
    energy into the room, survives speaker rolloff better, and still
    produces a sharp cross-correlation peak when matched against itself.
    """
    n = int(rate * duration_s)
    t = np.linspace(0, duration_s, n, endpoint=False)
    f0, f1 = 200.0, 4000.0
    K = duration_s / np.log(f1 / f0)
    phase = 2 * np.pi * f0 * K * (np.exp(t / K) - 1)
    chirp = np.sin(phase)
    window = np.hanning(n)
    return (chirp * window * 0.3).astype(np.float32)


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
        self._device_name: str = ""

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
            # PyAudioWPatch device dicts carry name/index/hostApi/channel counts
            # but no stable endpoint GUID. The name is stable for permanent
            # audio devices (changes only if the user renames the device in
            # Windows Sound settings), so use it as the latency cache key.
            self._device_name = loopback_device.get("name", "")
            if not self._device_name:
                logger.warning("[AEC] Loopback device has no name — latency cache disabled")

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
            except Exception as e:
                logger.debug(f"stop: {e}")
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

        except Exception as e:
            logger.debug(f"_stream_callback: {e}")

        return (None, pyaudio.paContinue)

    @staticmethod
    def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        """Simple linear-interpolation resample (fast, good enough for AEC reference)."""
        if src_rate == dst_rate:
            return audio
        ratio = dst_rate / src_rate
        n_out = int(round(len(audio) * ratio))
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
            except Exception as e:
                logger.debug(f"_cleanup_pa: {e}")
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

        if not hasattr(self, '_diag_count'):
            self._diag_count = 0
        self._diag_count += 1
        if self._diag_count % 50 == 1:
            w_norm = float(np.sum(np.abs(self._W)))
            yhat_rms = float(np.sqrt(np.mean(np.abs(Y_hat) ** 2)))
            e_minus_mic = float(np.max(np.abs(E - Mic)))
            print(f"[AEC-DIAG] W_norm={w_norm:.6f} Y_hat_rms={yhat_rms:.6f} E_minus_mic={e_minus_mic:.6f}")

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
        latency_ms: float = 50.0,
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
                Default 100ms is the WASAPI shared-mode midpoint (80-180ms
                range). start() overrides this from the per-device cache if
                a prior calibrate_and_cache() run produced a confident result.
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

        # Most recent reference (speaker) RMS, exposed so the wake-word
        # callback can tell when speakers are actively generating audio
        # (post-command echo suppression). None until process() has seen
        # at least one chunk with enough reference data.
        self.last_ref_rms = None

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
            self._apply_cached_latency()
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

        # Pull reference audio from loopback (already at 16kHz).
        # get_recent returns oldest-first; the current mic block's echo was
        # emitted from (n + L) samples ago to L samples ago, which is ref[:n].
        need = n + self._latency_samples
        ref_full = self._loopback.get_recent(need)

        # Left-pad with zeros when the ring hasn't yet filled the full window.
        # Without padding, a shorter slice shifts the applied delay each call,
        # which corrupts the NLMS weights. Zero-padding keeps the delay constant
        # at exactly _latency_samples from the first chunk onward.
        missing = need - len(ref_full)
        if missing > 0:
            ref_full = np.pad(ref_full, (missing, 0), mode='constant')
        ref = ref_full[:n]

        # Skip if system audio is silent
        ref_rms = float(np.sqrt(np.mean(ref ** 2)))
        self.last_ref_rms = ref_rms
        if ref_rms < 1e-6:
            return mic_audio

        # Apply adaptive filter at 16kHz
        cleaned_16k = self._aec.process(mic_16k, ref)

        # Divergence safety: if filter is amplifying, reset weights
        cleaned_energy = float(np.mean(cleaned_16k ** 2))
        mic_energy = float(np.mean(mic_16k ** 2))
        if cleaned_energy > mic_energy * 2.0 and mic_energy > 1e-10:
            print("[AEC] DIVERGENCE detected - resetting filter weights")
            self._aec.reset()
            cleaned_16k = mic_16k  # pass through unfiltered this block

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
            # Cross-correlation lag measurement
            lag_str = ""
            if ref_rms > 0.01 and len(mic_16k) > 256 and len(ref) > 256:
                try:
                    corr = np.correlate(mic_16k[:4096], ref[:4096], mode='full')
                    lag_samples = int(np.argmax(np.abs(corr)) - (len(ref[:4096]) - 1))
                    lag_ms = lag_samples / self._FILTER_RATE * 1000
                    lag_str = f" lag={lag_samples}smp({lag_ms:.0f}ms)"
                except Exception as e:
                    logger.debug(f"process: {e}")
            cancel_pct = int((1 - cleaned_rms / max(mic_rms, 1e-10)) * 100)
            print(f"[AEC] ref_rms={ref_rms:.6f} mic_rms={mic_rms:.6f} "
                  f"cleaned_rms={cleaned_rms:.6f} cancel={cancel_pct}%{lag_str}")

        return cleaned.reshape(original_shape)

    @staticmethod
    def _resample(audio, src_rate, dst_rate):
        """Linear-interpolation resample (matches LoopbackCapture._resample)."""
        if src_rate == dst_rate:
            return audio
        ratio = dst_rate / src_rate
        n_out = int(round(len(audio) * ratio))
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

    def _apply_cached_latency(self) -> None:
        """Load per-device cached latency on startup, or set 100ms static default.

        Called automatically by start() after loopback capture succeeds.
        The loopback device name (LoopbackCapture._device_name) is the cache key.
        """
        device_key = self._loopback._device_name
        if not device_key:
            return

        cache = _load_latency_cache()
        if device_key in cache:
            cached_ms = float(cache[device_key])
            self.set_latency(cached_ms)
            logger.info(
                "[AEC] Loaded cached latency %.0f ms for device '%s'",
                cached_ms, device_key,
            )
        else:
            self.set_latency(100.0)
            logger.info(
                "[AEC] No cached latency for '%s' — using 100ms default. "
                "Call calibrate_and_cache() to measure the actual delay.",
                device_key,
            )

    def calibrate_and_cache(self, mic_device_index=None) -> dict:
        """Measure speaker-to-mic latency, apply if confident, and persist per device.

        Wraps calibrate_lag() with a stricter confidence gate (>= 0.5 vs the
        internal 0.3). A cached wrong value is worse than the 100ms default, so
        the threshold is intentionally conservative.

        Returns the same dict as calibrate_lag(). On success also calls
        set_latency() so the new value takes effect immediately without restart.
        """
        result = self.calibrate_lag(mic_device_index=mic_device_index)
        if not result['success']:
            logger.warning("[AEC-CAL] Calibration failed: %s", result['message'])
            return result

        if result['confidence'] < 0.5:
            logger.warning(
                "[AEC-CAL] Confidence %.2f < 0.5 — not caching (lag %.0f ms). "
                "Increase speaker volume and try again.",
                result['confidence'], result['lag_ms'],
            )
            return result

        lag_ms = result['lag_ms']
        self.set_latency(lag_ms)
        logger.info(
            "[AEC-CAL] Latency set to %.0f ms (confidence %.2f)",
            lag_ms, result['confidence'],
        )

        device_key = self._loopback._device_name
        if device_key:
            cache = _load_latency_cache()
            cache[device_key] = lag_ms
            _save_latency_cache(cache)
            logger.info("[AEC-CAL] Cached latency for device '%s'", device_key)
        else:
            logger.warning("[AEC-CAL] Device name unavailable — result not cached")

        return result

    def calibrate_lag(self, mic_device_index=None, mic_rate=None) -> dict:
        """Measure speaker-to-mic latency using a controlled transient.

        Plays a Hann-windowed log chirp (200 Hz -> 4 kHz, 50 ms) through
        the default output device, records the mic for ~1.5 s, and locates
        the received chirp via cross-correlation.

        Args:
            mic_device_index: sounddevice device index for the mic.
                              None = system default input.
            mic_rate: Native mic capture rate (Hz). None = self.sample_rate.

        Returns dict with keys:
            success      (bool)
            lag_samples  (int,   at 16 kHz)
            lag_ms       (float)
            confidence   (float, 0-1; peak-to-median correlation ratio / 10)
            message      (str)

        Does NOT modify self.latency_ms or self._latency_samples.
        """
        import sounddevice as sd

        RATE = self._FILTER_RATE          # 16 kHz
        mic_rate = mic_rate or self.sample_rate

        click = _make_calibration_click(rate=RATE, duration_s=0.05)

        pre_s  = 0.20
        tail_s = 1.20
        total_s = pre_s + 0.05 + tail_s

        pre  = np.zeros(int(RATE * pre_s),  dtype=np.float32)
        tail = np.zeros(int(RATE * tail_s), dtype=np.float32)
        out_signal = np.concatenate([pre, click, tail])

        print(f"[AEC-CAL] Playing calibration chirp ({len(out_signal) / RATE:.2f}s total)...")

        mic_samples = int(mic_rate * total_s)
        recording = sd.rec(
            mic_samples,
            samplerate=mic_rate,
            channels=1,
            dtype='float32',
            device=mic_device_index,
            blocking=False,
        )

        try:
            sd.play(out_signal, samplerate=RATE, blocking=True)
        except Exception as e:
            sd.stop()
            return {
                'success': False,
                'lag_samples': 0, 'lag_ms': 0.0, 'confidence': 0.0,
                'message': f'Playback failed: {e}',
            }

        sd.wait()
        mic = recording.flatten()

        if mic_rate != RATE:
            mic = self._resample(mic, mic_rate, RATE)

        # Cross-correlation: mic[i..i+len(click)] vs click
        corr = np.correlate(mic, click, mode='valid')
        abs_corr = np.abs(corr)

        if len(abs_corr) == 0:
            return {
                'success': False,
                'lag_samples': 0, 'lag_ms': 0.0, 'confidence': 0.0,
                'message': 'Mic recording too short for cross-correlation.',
            }

        peak_idx = int(np.argmax(abs_corr))
        peak_val = float(abs_corr[peak_idx])

        # Confidence: peak / median of non-peak region
        mask = np.ones(len(abs_corr), dtype=bool)
        guard = min(int(RATE * 0.02), max(1, len(abs_corr) // 4))
        lo = max(0, peak_idx - guard)
        hi = min(len(abs_corr), peak_idx + guard)
        mask[lo:hi] = False
        baseline = float(np.median(abs_corr[mask])) + 1e-9 if mask.any() else peak_val + 1e-9
        confidence = float(min(1.0, (peak_val / baseline) / 10.0))

        # lag = where the click lands in mic - where we placed it in out_signal
        expected_start = int(RATE * pre_s)
        lag_samples = peak_idx - expected_start
        lag_ms = lag_samples / RATE * 1000.0

        # Plausibility check: accept -50ms..+500ms
        if lag_samples < -int(RATE * 0.05) or lag_samples > int(RATE * 0.5):
            return {
                'success': False,
                'lag_samples': lag_samples,
                'lag_ms': lag_ms,
                'confidence': confidence,
                'message': (
                    f'Measured lag {lag_ms:.0f}ms is outside the plausible 0-500ms range. '
                    f'Confidence={confidence:.2f}. Check speaker volume and mic levels.'
                ),
            }

        msg = (
            f'Measured lag: {lag_samples} samples = {lag_ms:.1f}ms '
            f'(confidence {confidence:.2f}).'
        )
        if confidence < 0.3:
            msg += ' WARNING: low confidence — try increasing speaker volume.'

        print(f'[AEC-CAL] {msg}')
        return {
            'success': confidence >= 0.3,
            'lag_samples': lag_samples,
            'lag_ms': lag_ms,
            'confidence': confidence,
            'message': msg,
        }
