"""Minimal hold-to-record dictation while the full Samsara app is offline.

This intentionally avoids dictation.py, Qt, commands, wake words, and all
main-application state.  It reads the normal config once (never writes it),
then forces every imported Samsara helper into an isolated fallback profile.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any, Callable

import numpy as np


def _resolve_repo_root() -> Path:
    """Return the import root for source and frozen execution."""
    if getattr(sys, "frozen", False):
        frozen_root = getattr(sys, "_MEIPASS", "")
        if frozen_root:
            frozen_path = Path(frozen_root).resolve()
            if frozen_path.exists():
                return frozen_path
    return Path(__file__).resolve().parents[1]


REPO_ROOT = _resolve_repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MAIN_HOME = Path.home() / ".samsara"
MAIN_CONFIG = MAIN_HOME / "config.json"
FALLBACK_HOME = Path.home() / ".samsara-fallback"
DEFAULT_LOCK_PATH = Path(tempfile.gettempdir()) / "samsara-fallback.lock"

# Set this before importing any samsara.* helper.  The fallback may read the
# main config explicitly through MAIN_CONFIG, but helpers can never resolve a
# writable path inside the real profile.
os.environ["SAMSARA_HOME_DIR"] = str(FALLBACK_HOME)

from samsara import single_instance
from samsara import flight_recorder

MODEL_RATE = 16_000
FRAME_MS = 30
PREBUFFER_MS = 300
TAIL_SILENCE_MS = 300
TAIL_MAX_MS = 1_200
TAIL_SPEECH_RMS = 0.008
EXIT_HOTKEY = "ctrl+alt+q"


DEFAULTS: dict[str, Any] = {
    "hotkey": "ctrl+shift",
    "microphone": None,
    "microphone_name": None,
    "model_size": "base",
    "device": "auto",
    "language": "en",
    "cleanup_mode": "clean",
    "add_trailing_space": True,
    "initial_prompt": "",
}


def load_main_config(path: Path = MAIN_CONFIG) -> dict[str, Any]:
    """Read selected preferences once.  This function never opens for write."""
    config = dict(DEFAULTS)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for key in DEFAULTS:
                if key in loaded:
                    config[key] = loaded[key]
    except (OSError, ValueError, TypeError):
        pass
    return config


def resample_audio(audio: np.ndarray, source_rate: int, target_rate: int = MODEL_RATE) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not len(audio) or source_rate == target_rate:
        return audio
    new_length = max(1, int(round(len(audio) * target_rate / source_rate)))
    old_x = np.arange(len(audio), dtype=np.float64)
    new_x = np.linspace(0, len(audio) - 1, new_length, dtype=np.float64)
    return np.interp(new_x, old_x, audio).astype(np.float32)


def resolve_microphone(config: dict[str, Any], sd_module: Any) -> tuple[Any, int, str]:
    """Resolve the saved device by stable name first, then numeric index."""
    try:
        devices = list(sd_module.query_devices())
    except Exception as exc:
        raise RuntimeError("Could not query available input devices.") from exc
    if not devices:
        raise RuntimeError("No input devices were found on this system.")

    saved_name = str(config.get("microphone_name") or "").strip().casefold()
    chosen: Any = None

    if saved_name:
        for index, device in enumerate(devices):
            if int(device.get("max_input_channels", 0)) > 0 and str(device.get("name", "")).strip().casefold() == saved_name:
                chosen = index
                break

    if chosen is None:
        saved_index = config.get("microphone")
        if isinstance(saved_index, int) and 0 <= saved_index < len(devices):
            if int(devices[saved_index].get("max_input_channels", 0)) > 0:
                chosen = saved_index

    try:
        info = sd_module.query_devices(chosen, "input")
    except Exception as exc:
        raise RuntimeError(f"Failed to initialize input device {chosen!r}.") from exc
    rate = int(round(float(info["default_samplerate"])))
    return chosen, rate, str(info.get("name", "Default input"))


class CaptureBuffer:
    """Thread-safe prebuffer plus bounded speech-aware release tail."""

    def __init__(
        self,
        sample_rate: int,
        *,
        prebuffer_ms: int = PREBUFFER_MS,
        silence_ms: int = TAIL_SILENCE_MS,
        max_tail_ms: int = TAIL_MAX_MS,
        speech_rms: float = TAIL_SPEECH_RMS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sample_rate = sample_rate
        self.silence_ms = silence_ms
        self.max_tail_ms = max_tail_ms
        self.speech_rms = speech_rms
        self.clock = clock
        max_blocks = max(1, math.ceil(prebuffer_ms / FRAME_MS))
        self._prebuffer: collections.deque[np.ndarray] = collections.deque(maxlen=max_blocks)
        self._frames: list[np.ndarray] = []
        self._state = "idle"
        self._tail_started = 0.0
        self._quiet_ms = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def append(self, block: np.ndarray) -> None:
        mono = np.asarray(block, dtype=np.float32).reshape(-1).copy()
        if not len(mono):
            return
        with self._lock:
            if self._state == "idle":
                self._prebuffer.append(mono)
                return
            self._frames.append(mono)
            if self._state == "tail":
                rms = float(np.sqrt(np.mean(mono * mono)))
                duration_ms = len(mono) * 1000.0 / self.sample_rate
                self._quiet_ms = self._quiet_ms + duration_ms if rms < self.speech_rms else 0.0

    def start(self) -> bool:
        with self._lock:
            if self._state != "idle":
                return False
            self._frames = [frame.copy() for frame in self._prebuffer]
            self._prebuffer.clear()
            self._quiet_ms = 0.0
            self._state = "recording"
            return True

    def release(self) -> bool:
        with self._lock:
            if self._state != "recording":
                return False
            self._tail_started = self.clock()
            self._quiet_ms = 0.0
            self._state = "tail"
            return True

    def tail_ready(self) -> bool:
        with self._lock:
            if self._state != "tail":
                return False
            elapsed_ms = (self.clock() - self._tail_started) * 1000.0
            return self._quiet_ms >= self.silence_ms or elapsed_ms >= self.max_tail_ms

    def finish(self) -> np.ndarray:
        with self._lock:
            audio = np.concatenate(self._frames) if self._frames else np.empty(0, dtype=np.float32)
            self._frames.clear()
            self._quiet_ms = 0.0
            self._state = "idle"
            return audio.astype(np.float32, copy=False)

    def cancel(self) -> None:
        with self._lock:
            self._frames.clear()
            self._state = "idle"


def main_samsara_running() -> bool:
    """Return whether the normal-profile Samsara instance owns its mutex."""
    try:
        mutex = single_instance.acquire_single_instance_mutex(profile_dir=None)
    except single_instance.AlreadyRunningError:
        return True
    if mutex is not None:
        mutex.close()
    return False


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            return True
        import msvcrt

        self.handle = open(self.path, "a+b")
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()).encode("ascii"))
        self.handle.flush()
        return True

    def close(self) -> None:
        if self.handle is None:
            return
        if sys.platform == "win32":
            import msvcrt

            try:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self.handle.close()
        self.handle = None


def _beep(kind: str) -> None:
    try:
        import winsound

        tones = {"start": (760, 70), "stop": (520, 70), "ok": (900, 90), "error": (240, 240)}
        frequency, duration = tones[kind]
        winsound.Beep(frequency, duration)
    except Exception:
        pass


def _configure_logging() -> logging.Logger:
    FALLBACK_HOME.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("samsara-fallback")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(FALLBACK_HOME / "fallback.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    return logger


def resolve_device_and_compute(config: dict[str, Any]) -> tuple[str, str]:
    """Resolve a fallback-safe (device, compute_type) pair."""
    requested = str(config.get("device") or "auto").strip().lower()
    if requested in {"", "auto"}:
        requested = "auto"

    if requested == "auto":
        try:
            import ctranslate2

            supported = ctranslate2.get_supported_compute_types("cuda")
            if "cuda" in supported:
                return "cuda", "float16"
        except Exception:
            # Keep deterministic fallback behavior when CUDA probing itself fails.
            return "cpu", "int8"
        return "cpu", "int8"

    if requested not in {"cpu", "cuda"}:
        raise ValueError(
            "Invalid device setting: expected one of 'auto', 'cpu', or 'cuda'."
        )

    if requested == "cuda":
        try:
            import ctranslate2

            if "cuda" not in ctranslate2.get_supported_compute_types("cuda"):
                raise RuntimeError("CUDA is not available for this Python runtime.")
        except RuntimeError as exc:
            raise RuntimeError(f"Fallback requested CUDA but it is unavailable: {exc}") from exc
        except Exception:
            raise RuntimeError("Could not verify CUDA availability for fallback decode.")

    compute = "float16" if requested == "cuda" else "int8"
    return requested, compute


def _load_model(config: dict[str, Any], logger: logging.Logger) -> Any:
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError("Missing dependency: faster_whisper. Install dependencies in the fallback environment.") from exc

    device, compute_type = resolve_device_and_compute(config)
    model_kwargs = {
        "device": device,
        "compute_type": compute_type,
    }
    if device == "cpu":
        model_kwargs.update(cpu_threads=4, num_workers=2)

    model_name = str(config.get("model_size") or "base")
    try:
        return WhisperModel(model_name, **model_kwargs)
    except Exception:
        if device == "cpu":
            raise
        logger.exception("GPU model load failed; retrying on CPU")
        return WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=4,
            num_workers=2,
        )


def prepare_text(raw_text: str, config: dict[str, Any]) -> str:
    from samsara.cleanup import clean_text

    text = clean_text(raw_text.strip(), mode=str(config.get("cleanup_mode") or "clean"))
    if text and config.get("add_trailing_space", True):
        text += " "
    return text


def _transcribe_and_paste(model: Any, audio: np.ndarray, config: dict[str, Any], logger: logging.Logger) -> bool:
    from samsara.clipboard import paste_with_preservation

    flight_recorder.record('wake.fallback_decode_start', audio_samples=len(audio))
    segments, _info = model.transcribe(
        audio,
        language=str(config.get("language") or "en"),
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=str(config.get("initial_prompt") or ""),
    )
    raw_text = "".join(segment.text for segment in segments).strip()
    text = prepare_text(raw_text, config)
    if not text.strip():
        logger.warning("Transcription was empty; nothing pasted")
        flight_recorder.record('wake.fallback_decode_end', result='empty')
        return False
    if not paste_with_preservation(text):
        logger.error(
            "Clipboard paste failed. Verify pyperclip and pyautogui are available "
            "and the active window is ready for paste."
        )
        print("Paste failed. Check clipboard permissions and target focus.")
        flight_recorder.record(
            'wake.fallback_decode_end', result='paste_failed', text_len=len(text),
        )
        return False
    logger.info("Pasted %d characters", len(text.rstrip()))
    flight_recorder.record('wake.fallback_decode_end', result='ok', text_len=len(text))
    return True


def _import_runtime_modules() -> tuple[Any, Any]:
    try:
        import keyboard
    except Exception as exc:
        raise RuntimeError(
            "Missing dependency: keyboard. Install with 'pip install keyboard' in the fallback env."
        ) from exc

    try:
        import sounddevice as sd
    except Exception as exc:
        raise RuntimeError(
            "Missing dependency: sounddevice. Install with 'pip install sounddevice' in the fallback env."
        ) from exc

    return keyboard, sd


def run() -> int:
    logger = _configure_logging()
    lock = InstanceLock(DEFAULT_LOCK_PATH)
    if not lock.acquire():
        print("Samsara Fallback is already running.")
        return 2
    try:
        if main_samsara_running():
            print("Close the full Samsara app before starting Samsara Fallback.")
            return 2

        config = load_main_config()
        device_for_model = resolve_device_and_compute(config)[0]
        print("Loading the local speech model...")
        logger.info(
            "Device configured as %s, selected=%s",
            config.get("device", "auto"),
            device_for_model,
        )
        model = _load_model(config, logger)

        keyboard, sd = _import_runtime_modules()

        device, sample_rate, device_name = resolve_microphone(config, sd)
        capture = CaptureBuffer(sample_rate)

        def audio_callback(indata, _frames, _time_info, status) -> None:
            if status:
                logger.warning("Audio stream status: %s", status)
            capture.append(indata[:, 0])

        blocksize = max(1, int(round(sample_rate * FRAME_MS / 1000.0)))
        hotkey = str(config.get("hotkey") or "ctrl+shift")
        print(f"Ready on {device_name}.")
        print(f"Hold {hotkey}, speak, and release to paste. Press {EXIT_HOTKEY} to exit.")
        logger.info("Ready: device=%s rate=%d hotkey=%s", device_name, sample_rate, hotkey)

        was_down = False
        with sd.InputStream(
            device=device,
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            callback=audio_callback,
        ):
            while True:
                if main_samsara_running():
                    print("Full Samsara started; closing fallback to avoid duplicate capture.")
                    logger.info("Main Samsara lock detected; exiting")
                    return 0
                if keyboard.is_pressed(EXIT_HOTKEY):
                    return 0

                down = keyboard.is_pressed(hotkey)
                if down and not was_down and capture.start():
                    _beep("start")
                    logger.info("Recording started")
                    flight_recorder.record('hold_recording.start', trigger='fallback_hotkey', key_combo=hotkey)
                elif not down and was_down and capture.release():
                    logger.info("Hotkey released; collecting speech-aware tail")
                    flight_recorder.record('hold_recording.stop_triggered', reason='fallback_hotkey_release', key_combo=hotkey)
                was_down = down

                if capture.tail_ready():
                    native_audio = capture.finish()
                    _beep("stop")
                    if len(native_audio) < int(sample_rate * 0.25):
                        logger.warning("Recording too short; ignored")
                        _beep("error")
                    else:
                        try:
                            audio = resample_audio(native_audio, sample_rate)
                            ok = _transcribe_and_paste(model, audio, config, logger)
                            _beep("ok" if ok else "error")
                        except Exception as exc:
                            logger.exception("Transcription/paste failed")
                            print(f"Dictation failed: {exc}. See {FALLBACK_HOME / 'fallback.log'}")
                            _beep("error")
                time.sleep(0.01)
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        logger.exception("Fallback startup failed")
        print(f"Samsara Fallback failed: {exc}")
        print(f"Details: {FALLBACK_HOME / 'fallback.log'}")
        _beep("error")
        return 1
    except Exception as exc:
        logger.exception("Fallback startup failed")
        print(f"Samsara Fallback failed: {exc}")
        print(f"Details: {FALLBACK_HOME / 'fallback.log'}")
        _beep("error")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(run())
