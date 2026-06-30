import copy
import os
import shutil
import sys
import math
import wave

# In a windowed PyInstaller build (console=False), sys.stdout/sys.stderr are
# None. Any module-level print()/sys.stdout.write() then raises
# "'NoneType' object has no attribute 'write'" and the exe dies on launch.
# Install a no-op stream so all existing stdout/stderr writes are safe.
class _NullStream:
    def write(self, *a, **k):
        return 0
    def flush(self, *a, **k):
        pass
    def reconfigure(self, *a, **k):
        pass
    def isatty(self):
        return False

if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()

# Platform-specific imports
if sys.platform == 'win32':
    try:
        import ctypes
        import winsound
        HAS_WINSOUND = True
    except ImportError:
        HAS_WINSOUND = False
else:
    HAS_WINSOUND = False


def _get_default_render_id():
    """Return the Windows endpoint ID string of the current default audio output device.

    Queries IMMDeviceEnumerator (same COM path as plugins/commands/volume.py).
    Returns None on non-Windows or any failure — callers treat None as "unknown."
    """
    if sys.platform != 'win32':
        return None
    try:
        import struct, ctypes
        from ctypes import HRESULT, POINTER, byref, c_void_p, c_wchar_p, WINFUNCTYPE

        def _guid(s):
            p = s.strip('{}').split('-')
            return struct.pack('<IHH', int(p[0], 16), int(p[1], 16),
                               int(p[2], 16)) + bytes.fromhex(p[3] + p[4])

        def _vt(ptr, idx, ret, *args):
            fn = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0][idx]
            return WINFUNCTYPE(ret, *args)(fn)

        _CLSID = _guid('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
        _IID   = _guid('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
        ole32  = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0)

        enum = c_void_p()
        if ole32.CoCreateInstance(_CLSID, None, 23, _IID, byref(enum)) != 0:
            return None
        dev = c_void_p()
        # vtable[4] = GetDefaultAudioEndpoint(eRender=0, eConsole=0)
        hr = _vt(enum, 4, HRESULT, c_void_p, ctypes.c_uint, ctypes.c_uint,
                 POINTER(c_void_p))(enum, 0, 0, byref(dev))
        _vt(enum, 2, ctypes.c_ulong, c_void_p)(enum)   # Release enumerator
        if hr != 0 or not dev:
            return None
        id_ptr = c_wchar_p()
        # vtable[5] = GetId(ppstrId)
        hr = _vt(dev, 5, HRESULT, c_void_p, POINTER(c_wchar_p))(dev, byref(id_ptr))
        _vt(dev, 2, ctypes.c_ulong, c_void_p)(dev)     # Release device
        if hr != 0:
            return None
        result = id_ptr.value
        ole32.CoTaskMemFree(id_ptr)
        return result
    except Exception:
        return None


# Hide console window IMMEDIATELY before any output (Windows only)
def _hide_console_now():
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except:
        pass

# _hide_console_now()  # TEMPORARILY DISABLED for debug — uncomment when done testing

# ============================================================================
# Single Instance Check - Prevent multiple instances from running
# ============================================================================
def _check_single_instance():
    """
    Ensure only one instance of Samsara is running.
    Uses a lock file with platform-specific file locking.
    Returns the lock file handle (must be kept open) or exits if another instance exists.
    """
    from pathlib import Path
    import tempfile

    lock_file_path = Path(tempfile.gettempdir()) / "samsara.lock"

    try:
        # Open/create lock file
        if sys.platform == 'win32':
            import msvcrt
            # Open in write mode, create if doesn't exist
            lock_file = open(lock_file_path, 'w')
            try:
                # Try to get exclusive lock (non-blocking)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                # Write our PID
                lock_file.write(str(os.getpid()))
                lock_file.flush()
                return lock_file  # Keep file open to maintain lock
            except (IOError, OSError):
                # Another instance has the lock
                lock_file.close()
                # Try to read the other instance's PID
                try:
                    with open(lock_file_path, 'r') as f:
                        other_pid = f.read().strip()
                    print(f"[WARN] Samsara is already running (PID: {other_pid})")
                except:
                    print("[WARN] Samsara is already running")
                sys.exit(0)
        else:
            # Unix-like systems (macOS, Linux)
            import fcntl
            lock_file = open(lock_file_path, 'w')
            try:
                # Try to get exclusive lock (non-blocking)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Write our PID
                lock_file.write(str(os.getpid()))
                lock_file.flush()
                return lock_file  # Keep file open to maintain lock
            except (IOError, OSError):
                # Another instance has the lock
                lock_file.close()
                try:
                    with open(lock_file_path, 'r') as f:
                        other_pid = f.read().strip()
                    print(f"[WARN] Samsara is already running (PID: {other_pid})")
                except:
                    print("[WARN] Samsara is already running")
                sys.exit(0)
    except Exception as e:
        # If locking fails for any reason, log but continue
        # (better to have duplicate instances than no instances)
        print(f"[WARN] Could not check for existing instance: {e}")
        return None

# Single-instance lock is only meaningful when this file is run as the main
# program. Acquiring it at import time blocks pytest (any import of dictation
# triggers sys.exit(0) from _check_single_instance when a prior import already
# holds the lock). The lock is acquired from the __main__ block below via
# _acquire_instance_lock() -- the module-level name is kept so tests and
# helpers can reason about it without triggering the check.
_instance_lock = None


def _acquire_instance_lock():
    """Acquire the single-instance lock. Called from __main__ only."""
    global _instance_lock
    _instance_lock = _check_single_instance()
    return _instance_lock

# Fix OpenMP conflict between numpy and other libraries
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import re
import threading
import queue
import time
import collections
import subprocess
import logging
from datetime import datetime
import numpy as np
_PRE_SD_T = time.perf_counter()
import sounddevice as sd
_POST_SD_T = time.perf_counter()
_sd_import_ms = (_POST_SD_T - _PRE_SD_T) * 1000
sys.stdout.write(f"[BOOT-DIAG] sounddevice import (PortAudio init): {_sd_import_ms:.0f}ms\n")
sys.stdout.flush()
if _sd_import_ms > 5000:
    sys.stdout.write(f"[BOOT-DIAG] SLOW STEP: sounddevice import {_sd_import_ms:.0f}ms\n")
    sys.stdout.flush()
from pynput import keyboard as pynput_keyboard
from pynput.keyboard import Key, Controller as KeyboardController
import keyboard  # For reliable simultaneous key state detection
from pynput.mouse import Button, Controller as MouseController
import pyperclip
import pyautogui
if sys.stdout is not None:
    sys.stdout.write(f"[PRE-LOG] +{(time.perf_counter()-_POST_SD_T)*1000:.0f}ms (after input libs)\n")
    sys.stdout.flush()
# Check for Visual C++ Redistributable before any DLL-dependent imports.
# ctranslate2 (used by faster-whisper) requires msvcp140.dll which ships with
# the VC++ redist. On a clean machine this may not be installed.
if sys.platform == 'win32':
    try:
        import ctypes as _ctypes
        _ctypes.cdll.LoadLibrary("msvcp140.dll")
    except OSError:
        from PySide6.QtWidgets import QApplication as _QApp, QMessageBox as _QMB
        _app = _QApp.instance() or _QApp(sys.argv)
        _QMB.critical(
            None,
            "Missing Dependency",
            "Samsara requires the Visual C++ Redistributable.\n\n"
            "Download it from:\n"
            "https://aka.ms/vs/17/release/vc_redist.x64.exe\n\n"
            "Install it and restart Samsara.",
        )
        sys.exit(1)

from faster_whisper import WhisperModel
if sys.stdout is not None:
    sys.stdout.write(f"[PRE-LOG] +{(time.perf_counter()-_POST_SD_T)*1000:.0f}ms (after faster_whisper)\n")
    sys.stdout.flush()

# torch powers Silero VAD for real-time speech gating in the wake-word audio
# callback. It's already a transitive dependency of faster-whisper, but we
# guard the import so the app still starts if a user has a stripped install.
try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    torch = None
    _TORCH_AVAILABLE = False
from PIL import Image, ImageDraw
try:
    from samsara.ui.tray_qt import SamsaraTrayQt as _SamsaraTrayQt
except Exception as _tray_err:
    _SamsaraTrayQt = None
    print(f"[INIT] SamsaraTrayQt unavailable: {_tray_err}")
import json
from pathlib import Path
# Per-monitor DPI awareness must be declared before win32api does
# anything coordinate-related.  Without it, UIA BoundingRectangle (logical
# coords on a 150% display) and win32api.SetCursorPos (physical pixels)
# disagree — overlay labels appear in the right place but fallback clicks
# miss by a scaling-factor offset.
if sys.platform == 'win32':
    import ctypes as _dpi_ctypes
    try:
        _dpi_ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE_V2
    except (AttributeError, OSError):
        try:
            _dpi_ctypes.windll.user32.SetProcessDPIAware()   # Win7 fallback
        except Exception:
            pass
    del _dpi_ctypes

# Silence chatty third-party loggers that flood the console on import.
for _name in ("torio", "torio._extension", "torchaudio",
              "torchaudio._extension", "torch", "urllib3",
              "huggingface_hub",
              "PIL", "PIL.Image", "PIL.PngImagePlugin",
              "PIL.JpegImagePlugin", "PIL.TiffImagePlugin",
              "httpcore", "httpx",
              "comtypes", "comtypes.client"):
    logging.getLogger(_name).setLevel(logging.WARNING)
try:
    from samsara.ui.voice_training_qt import VoiceTrainingQt as _VoiceTrainingQt
except Exception as _vt_err:
    _VoiceTrainingQt = None
    print(f"[INIT] VoiceTrainingQt unavailable: {_vt_err}")
try:
    from samsara.ui.mic_setup_wizard_qt import MicSetupWizardQt as _MicSetupWizardQt
except Exception as _msw_err:
    _MicSetupWizardQt = None
    print(f"[INIT] MicSetupWizardQt unavailable: {_msw_err}")
try:
    from samsara.ui.ava_guide_qt import AvaGuideQt as _AvaGuideQt
except Exception as _ag_err:
    _AvaGuideQt = None
    print(f"[INIT] AvaGuideQt unavailable: {_ag_err}")
from samsara.profiles import ProfileManager
from samsara.ui.listening_indicator import ListeningIndicator
from samsara.cleanup import clean_text
from samsara.history import HistoryManager
from samsara.wake_word_matcher import match_wake_phrase
from samsara.wake_corrections import apply_corrections as apply_wake_corrections, was_corrected
from samsara.command_parser import parse_wake_command, normalize_command_text, strip_wake_echoes
from samsara.phonetic_wash import apply_phonetic_wash
from samsara.command_stats import increment_command_count, flush as flush_command_stats
from samsara import ava_corrections as _ava_corrections
from samsara import plugin_commands as _plugin_commands
from samsara.commands import CommandExecutor
from samsara.constants import (
    MODEL_SAMPLE_RATE, DEFAULT_CAPTURE_RATE,
    DEFAULT_SPEECH_THRESHOLD, DEFAULT_MIN_SPEECH_DURATION, DEFAULT_SILENCE_TIMEOUT,
    WAKE_DETECTION_SILENCE, WAKE_COMMAND_TIMEOUT,
    ICON_TICK_FAST, ICON_TICK_MEDIUM, ICON_TICK_SLOW,
    ICON_SPIN_FAST, ICON_SPIN_MEDIUM, ICON_SPIN_SLOW,
    ICON_CHASE_FAST, ICON_CHASE_MEDIUM, ICON_CHASE_SLOW,
    CLIPBOARD_PASTE_DELAY, CLIPBOARD_RESTORE_DELAY,
)
from samsara.calibration import measure_ambient_rms, calibrate_threshold
from samsara.key_macros import KeyMacroManager, get_default_macro_config
from samsara.learning import AdaptiveLearner
from samsara.notifications import NotificationManager, get_default_notification_config
from samsara.alarms import AlarmManager, get_default_alarm_config
from samsara.echo_cancel import EchoCanceller
from samsara.clipboard import clipboard_lock as _clipboard_lock, save_clipboard as _save_clipboard_win32, restore_clipboard as _restore_clipboard_win32, paste_with_preservation
from samsara.wake_detector import WakeWordDetector

# Minimum gap (ms) between AEC loopback open and ACE mic open.
# The Arctis Nova Pro Wireless WASAPI driver stalls 10-18 s when a second
# PortAudio client opens the same physical device within ~20 ms of the first.
# 600 ms is a conservative safe value measured empirically.
_AEC_TO_MIC_MIN_GAP_MS = 600

_WAKE_PRIMER_DELAY = 0.12
_WAKE_SESSION_TIMEOUT_S   = 10.0            # inactivity ends the open-ended wake session
_WAKE_SESSION_CHUNK_GAP_S = 1.0             # per-utterance VAD silence gap within a session
_WAKE_SESSION_SEND_WORDS  = ['over', 'send'] # default send terminators that finalize a wake session


def _get_pynput_command_key(button_name: str):
    """Resolve a command_mode.button string to a pynput Key or KeyCode.

    Returns None for mouse4/mouse5 (those are handled by the mouse listener)
    and for any unrecognised name.

    Supported keyboard values:
        rctrl / lctrl / ralt / lalt / rshift / lshift
        f1 ... f24  (function keys; f13-f24 are macro-pad / foot-pedal keys)
    """
    _SIMPLE = {
        'rctrl':      Key.ctrl_r,
        'right_ctrl': Key.ctrl_r,
        'lctrl':      Key.ctrl_l,
        'left_ctrl':  Key.ctrl_l,
        'ralt':       Key.alt_r,
        'right_alt':  Key.alt_r,
        'lalt':       Key.alt_l,
        'left_alt':   Key.alt_l,
        'rshift':     Key.shift_r,
        'right_shift': Key.shift_r,
        'lshift':     Key.shift_l,
        'left_shift': Key.shift_l,
    }
    if button_name in _SIMPLE:
        return _SIMPLE[button_name]
    if button_name.startswith('f'):
        tail = button_name[1:]
        if tail.isdigit():
            n = int(tail)
            if 1 <= n <= 24:
                # pynput defines f1-f20 in Key; f21-f24 may only exist as VK codes
                try:
                    return getattr(Key, button_name)
                except AttributeError:
                    pass
                try:
                    from pynput.keyboard import KeyCode
                    return KeyCode.from_vk(0x6F + n)   # F1=0x70 → Fn=0x6F+n
                except Exception:
                    return None
    return None


def _matches_pynput_key(key, target) -> bool:
    """True if *key* (from pynput callback) equals *target* (Key or KeyCode)."""
    if target is None:
        return False
    if key == target:
        return True
    # Cross-type comparison: Key enum member vs raw KeyCode — compare vk values.
    target_vk = getattr(getattr(target, 'value', target), 'vk', None)
    key_vk    = getattr(getattr(key,    'value', key),    'vk', None)
    if target_vk is not None and key_vk is not None:
        return target_vk == key_vk
    return False


def _split_audio_at_silences(
    audio,
    sample_rate,
    *,
    min_silence_s=0.3,
    max_chunk_s=25.0,
    silence_threshold=0.015,
):
    """Split long audio at silence boundaries for chunked Whisper transcription.

    Splits the waveform at pauses rather than at arbitrary 30-second
    boundaries so Whisper never straddles a word.  Does NOT discard any
    samples — every sample appears in exactly one returned chunk.

    Args:
        audio: float32 mono array at sample_rate Hz.
        sample_rate: samples per second (e.g. 16000).
        min_silence_s: minimum quiet-region duration to use as a split.
        max_chunk_s: target maximum chunk length; chunks are force-split
            here when no silence is found within the window.
        silence_threshold: per-window RMS below which a 100 ms window is
            counted as silence. 0.015 ≈ -36 dBFS; covers breath/room tone
            between sentences in typical microphone recordings.

    Returns:
        list of float32 arrays, always at least one element.
        Returns [audio] unchanged when the full recording fits in one chunk.
    """
    if len(audio) / sample_rate <= max_chunk_s:
        return [audio]

    win_samples = max(1, int(sample_rate * 0.1))   # 100 ms analysis windows
    n_windows   = len(audio) // win_samples
    if n_windows == 0:
        return [audio]

    # Vectorised RMS per window — much faster than a Python loop
    trimmed   = audio[:n_windows * win_samples]
    frames    = trimmed.reshape(n_windows, win_samples)
    rms       = np.sqrt(np.mean(frames ** 2, axis=1))
    is_silent = rms < silence_threshold

    # Collect candidate split points: centre of each silence run ≥ min_silence_s
    min_silent_wins = max(1, int(min_silence_s / 0.1))
    split_samples   = []
    i = 0
    while i < n_windows:
        if is_silent[i]:
            j = i
            while j < n_windows and is_silent[j]:
                j += 1
            if (j - i) >= min_silent_wins:
                split_samples.append(int(((i + j) // 2) * win_samples))
            i = j
        else:
            i += 1

    # Build chunks greedily: advance to the latest silence split within
    # max_chunk_s, or force-split there if no silence is found.
    max_chunk_samp = int(sample_rate * max_chunk_s)
    chunks, start  = [], 0
    while start < len(audio):
        target = start + max_chunk_samp
        if target >= len(audio):
            chunks.append(audio[start:])
            break
        candidates = [s for s in split_samples if start < s <= target]
        end        = candidates[-1] if candidates else target
        chunks.append(audio[start:end])
        start = end

    return chunks if chunks else [audio]


def resample_audio(audio, orig_sr, target_sr=MODEL_SAMPLE_RATE):
    """Resample audio from orig_sr to target_sr using linear interpolation.

    Good enough for speech -- Whisper is robust to minor artifacts.
    Returns the input unchanged if rates already match.
    """
    if orig_sr == target_sr:
        return audio
    duration = len(audio) / orig_sr
    new_length = int(duration * target_sr)
    old_indices = np.linspace(0, len(audio) - 1, num=len(audio))
    new_indices = np.linspace(0, len(audio) - 1, num=new_length)
    return np.interp(new_indices, old_indices, audio).astype(np.float32)


def _is_hallucinated_segments(seg_list, text):
    """True if the transcription shows Whisper's degenerate-repetition signature.
    Uses telemetry Whisper already computed; no re-inference. Conservative:
    only fires on clear signatures so real speech is never dropped."""
    t = (text or "").strip()
    if not t:
        return False
    # Signature A: high compression ratio on any segment (repetition compresses hard).
    # Whisper's own reject threshold is 2.4; we use a slightly higher 3.0 to stay
    # conservative and avoid touching borderline-but-real speech.
    for s in seg_list:
        cr = getattr(s, "compression_ratio", None)
        if cr is not None and cr > 3.0:
            return True
    # Signature B: low lexical diversity repetition (e.g. "click click click click").
    words = t.lower().split()
    if len(words) >= 4:
        uniq = len(set(words))
        if uniq <= max(2, len(words) // 4):
            return True
    # Signature C: very high no_speech_prob across all segments AND short output
    # (near-silent buffer that still emitted a token or two).
    if seg_list:
        nsp = [getattr(s, "no_speech_prob", 0.0) or 0.0 for s in seg_list]
        if nsp and min(nsp) > 0.8 and len(words) <= 3:
            return True
    return False


def hide_console():
    """Hide the console window (Windows only, no-op on other platforms)"""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except:
        pass


def open_file_or_folder(path):
    """Open a file or folder with the system's default handler (cross-platform)"""
    try:
        path_str = str(path)
        if sys.platform == 'win32':
            os.startfile(path_str)
        elif sys.platform == 'darwin':  # macOS
            subprocess.run(['open', path_str], check=True)
        else:  # Linux
            subprocess.run(['xdg-open', path_str], check=True)
        return True
    except Exception:
        return False



# Force stdout/stderr to UTF-8 so Unicode in transcriptions (arrows, em-dashes,
# smart quotes, emoji) can never raise UnicodeEncodeError on cp1252 Windows consoles.
# errors="replace" guarantees output can never crash a caller even on un-encodable bytes.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # packaged EXE / redirected / already-closed stream — non-fatal

if sys.stdout is not None:
    sys.stdout.write(f"[PRE-LOG] +{(time.perf_counter()-_POST_SD_T)*1000:.0f}ms (before logging setup)\n")
    sys.stdout.flush()
# Set up logging — persistent file in ~/.samsara/logs/ + console
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

LOG_DIR = Path(os.path.expanduser("~")) / ".samsara" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "samsara.log"

_log_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# File handler — DEBUG level, rotating 5 MB × 3 backups, UTF-8
file_handler = _RotatingFileHandler(
    LOG_FILE,
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(_log_fmt)

# Console handler — force UTF-8 so Unicode chars (arrows, etc.) don't
# raise UnicodeEncodeError on cp1252 Windows consoles.
# In the packaged EXE (console=False) sys.stdout is None and fileno()
# would crash; fall back to stderr (StreamHandler handles None silently).
if sys.stdout is not None and hasattr(sys.stdout, 'fileno'):
    try:
        console_handler = logging.StreamHandler(
            open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1, closefd=False)
        )
    except (OSError, AttributeError):
        console_handler = logging.StreamHandler(sys.stderr)
else:
    console_handler = logging.StreamHandler(sys.stderr)
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))

# Attach to root logger so all loggers (including exception hooks) feed here
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.DEBUG)
_root_logger.addHandler(file_handler)
_root_logger.addHandler(console_handler)

# Keep a named logger for Samsara's own print-override path
logger = logging.getLogger("Samsara")

# Suppress noisy third-party debug output
logging.getLogger("PIL").setLevel(logging.WARNING)
logging.getLogger("PIL.Image").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("comtypes").setLevel(logging.WARNING)

# Override print to also log
_original_print = print
def print(*args, **kwargs):
    message = ' '.join(str(arg) for arg in args)
    logger.info(message)
    try:
        _original_print(*args, **kwargs)
    except UnicodeEncodeError:
        # Last-resort: stdout still not UTF-8 (redirected pipe etc.).
        # The message is already in the UTF-8 log file via logger.info above,
        # so it is safe to emit an ASCII-safe fallback to the console.
        try:
            _original_print(message.encode("ascii", "replace").decode("ascii"), **kwargs)
        except Exception:
            pass  # never let console output break a caller


# ── Global exception hooks — log to file before crashing ─────────────────────

def _uncaught_exception_handler(exc_type, exc_value, exc_tb):
    import traceback as _tb
    logging.critical(
        "Uncaught exception:\n" +
        "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
    )
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _uncaught_exception_handler


_original_thread_init = threading.Thread.__init__

def _patched_thread_init(self, *args, **kwargs):
    _original_thread_init(self, *args, **kwargs)
    _original_run = self.run
    def _wrapped_run():
        try:
            _original_run()
        except Exception:
            import traceback as _tb
            logging.critical(
                f"Uncaught exception in thread {self.name}:\n" +
                _tb.format_exc()
            )
    self.run = _wrapped_run

threading.Thread.__init__ = _patched_thread_init

logger.info("=" * 50)
logger.info(f"Samsara starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
logger.info("=" * 50)






# ---------------------------------------------------------------------------
# Adaptive wake-word energy gate constants
# ---------------------------------------------------------------------------
# EMA weight applied to ambient-only frames when updating the noise floor.
# Slow (0.05) so transient louder sounds don't inflate the floor estimate.
_NOISE_FLOOR_ALPHA = 0.05

# A frame is considered ambient (eligible to update the floor) when its RMS
# is below (current_floor * _NOISE_FLOOR_SPEECH_RATIO).  Value of 2.0 means
# "less than twice the floor" = clearly not speech.
_NOISE_FLOOR_SPEECH_RATIO = 2.0

# Minimum noise floor so zero/near-silence input never drives the floor to
# zero and lets every subsequent buffer pass.
_NOISE_FLOOR_MIN = 0.0005

# Speech passes the gate when rms >= floor * _SPEECH_FLOOR_RATIO.
# 1.5x the ambient floor distinguishes speech from background noise.
# Low-gain mics (headsets, USB w/ AGC) have a narrow speech-to-ambient
# margin (~1.5-1.6x), so an aggressive ratio gates real speech out.
_SPEECH_FLOOR_RATIO = 1.5

# Hard absolute minimum so pure DC / zeroed buffers cannot pass even on a
# completely silent mic.
_ABS_FLOOR_MIN = 0.002


# ---------------------------------------------------------------------------
# Repeat / again command support
# ---------------------------------------------------------------------------

_REPEAT_BLACKLIST_TYPES = {
    "launch",
    "mouse",
}

_REPEAT_BLACKLIST_NAMES = {
    "close tab",
    "close window",
    "close virtual desktop",
    "delete file",
    "permanent delete",
    "delete word",
    "delete next word",
    "delete line",
    "new tab",
    "reopen tab",
    "duplicate tab",
    "submit",
    "cut",
    "record screen",
    "new note",
    "obsidian new note",
    "start narrator",
    "stop narrator",
    "backspace",
    "delete",
    "delete selection",
    "repeat",
    "again",
}


def _is_repeat_blacklisted(name: str, command: dict) -> bool:
    if name in _REPEAT_BLACKLIST_NAMES:
        return True
    if command.get("type") in _REPEAT_BLACKLIST_TYPES:
        return True
    return False


def _deep_merge(base, overlay):
    """Return a deep merge of two dicts. Values from `overlay` win on conflicts.
    New keys from `base` are preserved. Lists and primitives in overlay replace
    base entirely (we don't try to merge list elements)."""
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


_MISSING = object()


def _three_way_merge(base, memory, disk):
    """Three-way config merge.

    base   = last known on-disk state (snapshot taken when we last read/wrote)
    memory = current in-memory config
    disk   = current on-disk file

    Rules per key:
    - Only disk changed  -> use disk  (external edit; honour it)
    - Only memory changed -> use memory (runtime state; keep it)
    - Both changed        -> memory wins (app is authoritative for its own writes)
    - Neither changed     -> use memory (same as base)
    - New key in disk only -> include from disk
    - New key in memory only -> include from memory

    Nested dicts apply the same logic recursively.
    """
    if not (isinstance(base, dict) and isinstance(memory, dict) and isinstance(disk, dict)):
        return memory
    all_keys = set(base) | set(memory) | set(disk)
    result = {}
    for key in all_keys:
        b = base.get(key, _MISSING)
        m = memory.get(key, _MISSING)
        d = disk.get(key, _MISSING)

        if m is _MISSING and d is _MISSING:
            continue  # key existed only in base (deleted from both) — drop it
        if d is _MISSING:
            result[key] = m  # only in memory
        elif m is _MISSING:
            result[key] = d  # only on disk
        elif b is _MISSING:
            result[key] = m  # new in both: memory wins
        elif isinstance(b, dict) and isinstance(m, dict) and isinstance(d, dict):
            result[key] = _three_way_merge(b, m, d)
        else:
            mem_changed = m != b
            disk_changed = d != b
            if disk_changed and not mem_changed:
                result[key] = d   # only disk changed -> honour external edit
            else:
                result[key] = m   # memory changed (or neither) -> keep runtime value
    return result


def _resolve_target_window(process_name, exclude_pids=None):
    """Find the first visible (or minimized) top-level window whose owning
    process matches *process_name* (case-insensitive executable name).

    Returns (hwnd, title) or None.
    exclude_pids: set of int PIDs to skip (Samsara's own PID, terminal PIDs).

    Uses psutil for fast PID-by-name lookup, then EnumWindows to find a
    window owned by one of those PIDs — same Win32 pattern as window_switcher.
    """
    import ctypes
    import ctypes.wintypes as _wt
    try:
        import psutil as _ps
    except ImportError:
        print("[WAKE-TARGET] psutil not available — cannot resolve target window")
        return None

    exclude = exclude_pids or set()
    target_pids = set()
    try:
        for proc in _ps.process_iter(['pid', 'name']):
            name = proc.info.get('name') or ''
            if name.lower() == process_name.lower() and proc.info['pid'] not in exclude:
                target_pids.add(proc.info['pid'])
    except Exception as exc:
        print(f"[WAKE-TARGET] process enumeration error: {exc}")
        return None

    if not target_pids:
        return None

    _user32 = ctypes.windll.user32
    found = []

    def _enum_cb(hwnd, _):
        visible   = bool(_user32.IsWindowVisible(hwnd))
        minimized = bool(_user32.IsIconic(hwnd))
        if not visible and not minimized:
            return True
        pid = _wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in target_pids:
            return True
        title_len = _user32.GetWindowTextLengthW(hwnd)
        if title_len == 0:
            return True
        buf = ctypes.create_unicode_buffer(title_len + 1)
        _user32.GetWindowTextW(hwnd, buf, title_len + 1)
        title = buf.value
        if title:
            found.append((hwnd, title))
        return True

    _WNDPROC = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    _user32.EnumWindows(_WNDPROC(_enum_cb), 0)
    return found[0] if found else None


class DictationApp:
    def __init__(self, splash=None):
        self.splash = splash
        self.config_path = Path(__file__).parent / "config.json"

        # Boot-phase timing -- measure first, fix later.
        _bt0 = time.monotonic()
        _btp = [_bt0]  # mutable cell so the closure can write it
        def _boot(label: str) -> None:
            now = time.monotonic()
            print(f"[BOOT] {label}: {(now - _btp[0]) * 1000:.0f}ms  (total {(now - _bt0) * 1000:.0f}ms)")
            _btp[0] = now
        self._boot_log = _boot  # expose so load_model_async can use it

        # [BOOT-DIAG] perf_counter-based timing for slow-boot diagnosis.
        _bdiag_t0 = time.perf_counter()
        _bdiag_tp = [_bdiag_t0]
        def _bdiag(label: str) -> None:
            now = time.perf_counter()
            dt_step  = (now - _bdiag_tp[0]) * 1000
            dt_total = (now - _bdiag_t0)    * 1000
            _bdiag_tp[0] = now
            logger.info(f"[BOOT-DIAG] {label}: {dt_step:.0f}ms (total {dt_total:.0f}ms)")
            if dt_step > 5000:
                logger.info(f"[BOOT-DIAG] SLOW STEP: {label} {dt_step:.0f}ms")
        logger.info(f"[BOOT-DIAG] __init__ entry (perf_counter since sounddevice: {(_bdiag_t0 - _PRE_SD_T)*1000:.0f}ms)")
        # Protects all self.config mutations and save_config disk writes.
        # MUST be held before any mutation to self.config that precedes a save,
        # and before calling save_config() directly.
        # Never hold while doing audio work, VAD, AEC, model loading, or UI rendering.
        self._config_lock = threading.Lock()
        # Snapshot of config as last read from / written to disk.
        # Used by save_config for three-way merging and by reload_config_from_disk.
        self._config_last_disk_snapshot: dict = {}
        # File-system watcher; started after load_config completes.
        self._config_watcher = None

        # Check if first-run wizard is needed.
        # Triggers when: config missing, first_run_complete absent/false,
        # or no microphone was ever configured.
        need_wizard = False
        if not self.config_path.exists():
            need_wizard = True
        else:
            for _attempt in range(3):
                try:
                    with open(self.config_path, 'r') as f:
                        existing_config = json.load(f)
                    if not existing_config.get('first_run_complete', False):
                        need_wizard = True
                    elif existing_config.get('microphone') is None:
                        need_wizard = True
                    break
                except (OSError, PermissionError) as _e:
                    logger.warning("[CONFIG] pre-wizard check attempt %d failed: %s", _attempt + 1, _e)
                    if _attempt < 2:
                        time.sleep(0.1)
                    else:
                        logger.warning("[CONFIG] pre-wizard check failed 3x -- skipping wizard (assuming valid config)")
                        need_wizard = False
                except json.JSONDecodeError:
                    need_wizard = True
                    break
                except Exception:
                    need_wizard = True
                    break

        # Run first-run wizard if needed
        if need_wizard:
            # Close splash for wizard - wizard has its own UI
            if self.splash:
                try:
                    self.splash.close()
                except Exception as e:
                    print(f"[SPLASH] close() failed: {e}")
                self.splash = None
            print("First run detected - launching setup wizard...")
            from samsara.ui.first_run_wizard_qt import FirstRunWizardQt
            wizard = FirstRunWizardQt(self.config_path, self)
            wizard_result = wizard.run()
            if wizard_result:
                # Wizard completed successfully, save the config
                with open(self.config_path, 'w') as f:
                    json.dump(wizard_result, f, indent=2)
                print("Setup wizard completed successfully!")
            else:
                # Wizard was cancelled, use defaults but mark as complete
                print("Setup wizard cancelled - using default settings")
            # No splash after wizard - user already saw UI
            # Auto-launch tutorial after wizard (first run only)
            self._launch_tutorial_after_wizard = True

        print("[INIT] Loading config...")
        self.update_splash("Loading configuration...")
        with self._config_lock:
            self.load_config()
        _boot("config load")
        _bdiag("config load")

        self.update_splash("Setting up audio...")

        # Set the Samsara wheel as the default icon for all Qt windows.
        try:
            from PySide6.QtGui import QIcon, QImage, QPixmap
            from PySide6.QtWidgets import QApplication
            _icon_pil = self.create_icon_image(active=True).convert("RGBA")
            _icon_qi  = QImage(
                _icon_pil.tobytes(), _icon_pil.width, _icon_pil.height,
                QImage.Format.Format_RGBA8888,
            )
            QApplication.instance().setWindowIcon(QIcon(QPixmap.fromImage(_icon_qi)))
        except Exception as _e:
            print(f"[ICON] Could not set Qt window icon: {_e}")

        print("[INIT] Enumerating audio devices...")
        self.available_mics = self.get_available_microphones()
        _boot("audio device enumeration")
        _bdiag("get_available_microphones (sd.query_devices+hostapis)")

        # Try name-based reconciliation first — stable across index changes.
        # _reconcile_microphone_selection is defined later in the class but
        # resolved at call time, so this is safe.
        self._reconcile_microphone_selection()

        # Validate saved microphone ID against available devices.
        # Device indices change when switching host APIs (e.g. MME → WASAPI)
        # or when hardware is added/removed. Fall back to the first available.
        saved_mic = self.config.get('microphone')
        valid_ids = {mic['id'] for mic in self.available_mics}
        if saved_mic not in valid_ids and self.available_mics:
            old_id = saved_mic
            with self._config_lock:
                self.config['microphone'] = self.available_mics[0]['id']
                self.save_config()
            new_name = self.available_mics[0]['name']
            print(f"[CONFIG] Saved microphone {old_id} not found in current devices, "
                  f"switched to {new_name} (id={self.config['microphone']})")
            # Notify the user so they can confirm the right mic is selected
            def _mic_changed_dialog():
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    None,
                    "Microphone Changed",
                    f"Your previously selected microphone was not found on this machine.\n\n"
                    f"Samsara has switched to: {new_name}\n\n"
                    f"If this is wrong, open Settings to choose the correct microphone.",
                )
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication as _QApp
            QTimer.singleShot(2000, _QApp.instance(), _mic_changed_dialog)

        # Audio settings -- dual sample rates for WASAPI compatibility
        self.model_rate = MODEL_SAMPLE_RATE
        _t = time.perf_counter()
        self.capture_rate = self._detect_capture_rate(self.config.get('microphone'))
        _dt = (time.perf_counter() - _t) * 1000
        logger.info(f"[BOOT-DIAG] detect_capture_rate (sd.query_devices): {_dt:.0f}ms")
        if _dt > 5000:
            logger.info(f"[BOOT-DIAG] SLOW STEP: detect_capture_rate {_dt:.0f}ms")

        # Auto-calibrate speech threshold on startup
        _t = time.perf_counter()
        self._run_calibration_if_auto()
        _dt = (time.perf_counter() - _t) * 1000
        _boot("mic calibration")
        logger.info(f"[BOOT-DIAG] mic calibration (sd.InputStream open+1.5s record): {_dt:.0f}ms")
        if _dt > 5000:
            logger.info(f"[BOOT-DIAG] SLOW STEP: mic calibration {_dt:.0f}ms")

        self.recording = False
        self.command_mode_recording = False  # True when using command-only hotkey
        self._stop_in_flight = False         # True while stop_recording + trailing sleep is pending

        self._running = True

        # Set up audio feedback sounds (creates defaults if needed)
        self._setup_sounds()
        _boot("sound setup")
        _bdiag("sound setup")

        # Model settings
        self.model = None
        self.model_loaded = False
        self.loading_model = False
        self.model_lock = threading.Lock()  # Thread lock for model.transcribe() calls
        
        print("[INIT] Loading plugins...")
        commands_path = Path(__file__).parent / "commands.json"
        self.command_executor = CommandExecutor(commands_path, app=self)
        self.command_mode_enabled = self.config.get('command_mode_enabled', True)
        _boot("plugin discovery + command executor")
        _bdiag("plugin discovery + command executor")

        # Repeat / again state
        self._last_command = None       # command dict of last repeatable command
        self._last_command_name = None  # canonical phrase of last repeatable command

        # Mouse 4 command mode (walkie-talkie hold-to-talk)
        self.command_mode_active = False
        self._command_mode_lock = threading.Lock()
        self._command_mode_miss_count = 0
        self._command_mode_inactivity_timer = None
        self._command_mode_session_start = 0.0  # monotonic time of last enter
        self._command_mode_ghost_tap = False    # set when hold < enter_debounce_ms
        self._command_mode_key_held = False      # edge-trigger guard vs OS key auto-repeat

        # Ava mode — Right Alt held = talk to Ollama/Ava
        self.ava_mode_active = False
        self.ava_mode_recording = False
        self._ava_mode_ghost_tap = False
        self._ava_mode_session_start = 0.0
        self._ava_mode_lock = threading.Lock()
        self._ava_mode_key_held = False          # edge-trigger guard vs OS key auto-repeat

        # AI command mode -- LLM-backed voice-to-command translator (toggle)
        self.ai_command_mode_active = False
        self._ai_cmd_mode_lock = threading.Lock()
        self._ai_cmd_key_held = False            # edge-trigger guard vs OS key auto-repeat
        self._ai_cmd_ready = threading.Event()
        self._ai_cmd_ready.set()  # starts set; cleared during entry until cue finishes

        self._mouse_hook = None

        # Wake-word trace hook — the debug window registers a callback here
        # when open so the main pipeline's decisions show up in its trace view.
        # None means "no tracing" and _emit_wake_trace becomes a cheap no-op.
        self._wake_trace_callback = None

        # Tutorial interaction hooks — lightweight one-shot callables registered
        # by TutorialWindow and removed when the window closes.
        # Keys: 'dictation', 'command', 'ava'
        self._tutorial_hooks: dict = {}
        
        # Hotkey settings
        self.hotkey_pressed = False
        self.current_keys = set()
        self.key_press_times = {}  # Track when each key was pressed
        self.hotkey_window = 0.3  # 300ms window for hotkey detection
        
        # Mode tracking
        self.toggle_active = False  # For toggle mode
        self.continuous_active = False  # For continuous mode
        self.wake_word_active = False  # For wake word mode

        # Wake-word trace callback is initialized earlier (see above).

        # Tray icon chase animation state
        self._icon_chase_offset = 0
        self._icon_chase_timer = None
        self._icon_animating = False
        self._icon_rotation = 0.0        # current rotation angle in radians
        self._icon_chase_counter = 0     # counts ticks between color shifts
        self._icon_anim_reasons = set()  # tracks who wants animation (e.g. 'recording', 'wake_word')
        self.silence_start = None
        self.is_speaking = False
        self.speech_buffer = []
        self.buffer_lock = threading.Lock()
        # Silero VAD -- real-time speech gate for the wake-word audio callback.
        # When available, it replaces the old RMS debounce entirely. When it's
        # not (torch missing or download blocked), we fall back to RMS.
        self._vad_model = None
        self._vad_available = False
        self.wake_word_listening = False  # Currently listening for wake word
        self.wake_word_triggered = False  # Wake word detected, ready for command
        self._wake_trace_callback = None  # Optional: debug window registers here
        self._wake_transcription_in_progress = False  # Prevents concurrent Whisper calls on CPU

        # Rolling noise-floor estimate for adaptive wake energy gate.
        # Seeded from measured_noise_floor config key when available so the
        # floor survives restart; otherwise the first buffer initialises it.
        _saved_floor = (
            self.config
            .get('wake_word_config', {})
            .get('audio', {})
            .get('measured_noise_floor', None)
        )
        self._wake_noise_floor: float | None = (
            float(_saved_floor) if _saved_floor else None
        )

        # OpenWakeWord pre-filter: fast (~5ms) ONNX wake-word model that gates
        # Whisper calls. On CPU this drops idle load from ~100% to near zero.
        # Initialised lazily in _load_oww_model() after the Whisper model loads.
        self._wake_detector = None
        self._oww_wake_detected = False  # Set by OWW; consumed by silence flush

        # Phase 1 multi-wakeword: per-target OWW detectors (id -> WakeWordDetector|None).
        # None means that target uses Whisper-transcript fallback.
        # Loaded lazily in _load_wake_target_models() after the Whisper model loads.
        self._wake_target_detectors: dict = {}

        # Timestamp of the last successful command execution. While this is
        # within the 2-second post-command window, the audio callback
        # suppresses buffering to avoid picking up speaker output (Chrome
        # launch sound, notifications, etc.) as a new utterance.
        self._command_executed_at = None
        
        self._hotkey_recording = False  # Suppress wake word transcription during hotkey recording
        
        # Dictation mode tracking (for wake word dictation)
        self.dictation_mode = None  # None, 'dictate', 'short_dictate', 'long_dictate'
        self.dictation_buffer = []  # Audio buffer for dictation content
        self.dictation_start_time = None  # When dictation started
        
        # 4-state machine: asleep → command_window → quick_dictation / long_dictation
        self.app_state = 'asleep'
        self.wake_dictation_mode = None       # compat alias for app_state dictation type
        self.wake_dictation_buffer = []       # text chunks accumulated during dictation
        self.wake_dictation_start_time = None
        self._dictation_silence_timeout = None
        self._dictation_require_end = False
        self._dictation_finalize_timer = None
        self._dictation_finalize_lock = threading.Lock()
        self._dictation_paused = False

        # Single-level undo for the last pasted dictation. Shift+Left+Delete
        # only works if the caret hasn't moved since the paste, so the state
        # expires after _UNDO_EXPIRY_SECONDS or on the next paste.
        self._last_dictation_text = None
        self._last_dictation_length = 0
        self._undo_timer = None

        # Dictation history
        self.history_path = Path(__file__).parent / 'history.json'
        self.max_history = 100  # Keep last 100 items
        self.history = self.load_history()  # List of (timestamp, text, is_command) tuples

        # Persistent SQLite-backed history at ~/.samsara/history.db. Separate
        # from self.history (above) so the existing HistoryWindow keeps working
        # while the new store records every attempt -- including failures.
        try:
            self.history_db = HistoryManager()
            self.history_db.prune(max_entries=10000)
        except Exception as e:
            print(f"[HISTORY] Could not open persistent history: {e}")
            self.history_db = None
        _boot("history / SQLite init")
        _bdiag("history / SQLite init")

        print("[INIT] Building UI...")

        # Voice Training window — create on Qt thread
        self.voice_training_window = None
        if _VoiceTrainingQt is not None:
            def _init_vt():
                try:
                    self.voice_training_window = _VoiceTrainingQt(self)
                except Exception as _e:
                    print(f"[INIT] VoiceTrainingQt unavailable: {_e}")
            self._schedule_ui(_init_vt)
            print("[INIT] Using VoiceTrainingQt")

        # Mic setup wizard — create on Qt thread
        self.mic_setup_wizard = None
        if _MicSetupWizardQt is not None:
            def _init_mic_wiz():
                self.mic_setup_wizard = _MicSetupWizardQt(self)
            self._schedule_ui(_init_mic_wiz)

        # Ava setup guide — create on Qt thread
        self.ava_guide = None
        if _AvaGuideQt is not None:
            def _init_ava_guide():
                self.ava_guide = _AvaGuideQt(self)
            self._schedule_ui(_init_ava_guide)

        # Wake word debug window — create on Qt thread
        self.wake_word_debug_window = None
        try:
            from samsara.ui.wake_word_debug_qt import WakeWordDebugQt
            def _init_wwd():
                self.wake_word_debug_window = WakeWordDebugQt(self)
            self._schedule_ui(_init_wwd)
        except ImportError:
            print("[INIT] WakeWordDebugQt unavailable")

        # Listening state indicator overlay — must be created on the Qt thread.
        # ListeningIndicator is a QWidget; creating it on the main thread
        # causes "Timers cannot be started from another thread" and freezes
        # the entire Qt event loop.
        self.listening_indicator = None  # set by _init_indicator on Qt thread

        def _init_indicator():
            self.listening_indicator = ListeningIndicator()
            self.listening_indicator.set_mode(self._get_mode_display())
            self.listening_indicator.set_position(
                self.config.get('listening_indicator_position', 'bottom-center'))
            if self.config.get('listening_indicator_enabled', False):
                self.listening_indicator.show()

        from PySide6.QtCore import QTimer
        qt_app = __import__('PySide6.QtWidgets', fromlist=['QApplication']).QApplication.instance()
        if qt_app:
            QTimer.singleShot(0, qt_app, _init_indicator)

        # Vision bridge (optional; requires vision.enabled: true in config)
        self._vision_bridge = None
        vision_cfg = self.config.get("vision", {})
        if vision_cfg.get("enabled", False):
            try:
                from samsara.vision import VisionBridge
                self._vision_bridge = VisionBridge(self)
                if vision_cfg.get("warmup", True):
                    threading.Thread(
                        target=self._vision_bridge.warmup,
                        daemon=True,
                        name="vision-warmup",
                    ).start()
                    print("[VISION] Warmup started in background.")
            except Exception as e:
                print(f"[VISION] Init failed: {e}")
                self._vision_bridge = None

        # Command cheat sheet overlay
        palette_path = Path(__file__).parent / "command_palette.json"
        from samsara.ui.command_cheatsheet_qt import CommandCheatSheetQt
        # Command cheat sheet — create on Qt thread
        self.cheat_sheet = None
        def _init_cheatsheet():
            self.cheat_sheet = CommandCheatSheetQt(
                execute_cb=lambda phrase: self.command_executor.process_text(
                    phrase, self, force_commands=True
                ),
                commands_cb=lambda: self.command_executor._matcher.list_commands(),
                palette_path=palette_path,
            )
        self._schedule_ui(_init_cheatsheet)

        # Tutorial — auto-launch on first run (after wizard), on Qt thread.
        # On subsequent startups tutorial_complete is True so this is a no-op.
        if getattr(self, '_launch_tutorial_after_wizard', False) and \
                not self.config.get('tutorial_complete', False):
            def _init_tutorial():
                try:
                    from samsara.ui.tutorial_qt import show_tutorial
                    show_tutorial(self)
                except Exception as _e:
                    print(f"[TUTORIAL] Failed to launch tutorial: {_e}")
            self._schedule_ui(_init_tutorial)

        # Snooze state
        self.snoozed = False
        self._snooze_timer = None
        self._snooze_resume_time = None  # datetime or None for indefinite
        self._snooze_prior_mode_state = None  # what to restore on resume

        # Wake word trace callback — set by WakeWordDebugWindow while it is open
        # so the debug UI can visualize the MAIN app's wake word pipeline, not
        # just its own parallel implementation. No-op when None.
        self._wake_trace_callback = None

        # Key macro manager
        self.key_macro_manager = KeyMacroManager(self.config)
        self.key_macro_manager.start()

        # Adaptive learning for transcription corrections
        self.adaptive_learner = AdaptiveLearner(Path(__file__).parent)

        # Notification manager for reminders
        config_dir = Path(__file__).parent
        self.notification_manager = NotificationManager(config_dir)
        if self.config.get('notifications', {}).get('enabled', True):
            self.notification_manager.start()

        # Alarm manager for persistent sound reminders
        sounds_dir = Path(__file__).parent / 'sounds'
        self.alarm_manager = AlarmManager(
            config_dir=config_dir,
            sounds_dir=sounds_dir,
            get_config=lambda: self.config,
            save_config=self.persist_config
        )
        if self.config.get('alarms', {}).get('enabled', True):
            self.alarm_manager.start()

        # Contextual hint system
        from samsara.hints import HintManager
        self.hints = HintManager(self)

        print("[INIT] Initializing TTS...")
        # Gesture input lane (optional; off by default)
        self._camera_service = None
        self._gesture_loop = None

        # TTS engine + AudioCoordinator (optional; off by default)
        # engine selection: config tts.engine = "winrt" (default) or "edge"
        self.tts_engine = None
        self.audio_coordinator = None
        if self.config.get('tts', {}).get('enabled', False):
            try:
                from samsara.tts import WinRTEngine, EdgeTTSEngine, AudioCoordinator
                from samsara.tts.exceptions import EngineUnavailableError
                tts_engine_name = self.config.get('tts', {}).get('engine', 'winrt').lower()
                if tts_engine_name == 'edge':
                    self.tts_engine = EdgeTTSEngine()
                    print("[TTS] Initialized EdgeTTS engine (Azure Neural voices)")
                else:
                    self.tts_engine = WinRTEngine()
                    print("[TTS] Initialized WinRT engine")
                self.audio_coordinator = AudioCoordinator(
                    self,
                    engine=self.tts_engine,
                    config=self.config.get('audio_coordinator', {}),
                )
                print("[TTS] AudioCoordinator ready")
            except Exception as e:
                print(f"[TTS] Failed to initialize: {e}")
                self.tts_engine = None
                self.audio_coordinator = None
        _boot("TTS engine init")
        _bdiag("TTS engine init")

        # Smart Actions Phase 2: webhook bridge, session manager, tool dispatcher
        try:
            from samsara.smart_actions_bridge import SmartActionsBridge
            from samsara.smart_actions_session import SmartActionsSession
            from samsara.smart_actions_tools import ToolDispatcher
            sa_config = self.config.get('smart_actions', {})
            self._smart_actions_bridge = SmartActionsBridge(sa_config)
            self._smart_actions_session = SmartActionsSession(
                window_minutes=sa_config.get('session_window_minutes', 5))
            self._smart_actions_tools = ToolDispatcher(self, sa_config)
            print("[SMART ACTIONS] Phase 2 bridge/session/tools initialized")
        except Exception as e:
            print(f"[SMART ACTIONS] Phase 2 init failed: {e}")
            self._smart_actions_bridge = None
            self._smart_actions_session = None
            self._smart_actions_tools = None
        _boot("smart actions init")
        _bdiag("smart actions init")

        # Echo cancellation (removes system audio from mic input)
        aec_config = self.config.get('echo_cancellation', {})
        self.echo_canceller = EchoCanceller(
            sample_rate=self.capture_rate,
            enabled=aec_config.get('enabled', False),
            latency_ms=aec_config.get('latency_ms', 30.0),
        )
        if self.echo_canceller.enabled:
            if self.echo_canceller.start():
                self._aec_open_t = time.perf_counter()

        self.update_splash("Setting up keyboard...")

        # Start keyboard listener
        self.keyboard_listener = pynput_keyboard.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release
        )
        self.keyboard_listener.start()
        _boot("keyboard/mouse listener setup")
        _bdiag("keyboard/mouse listener setup")

        # Mouse listener for Mouse 4 command mode (hold-to-talk / toggle)
        self._install_mouse_listener()

        # Install the CapsLock hook used by streaming-mode dictation.
        # suppress=True means the OS never sees CapsLock while Samsara is
        # running -- it does not toggle the caps state, and the keyboard
        # library's hook gets every press/release before any other
        # listener. The callback is a no-op when streaming_mode is off.
        self._capslock_held = False
        self._capslock_hook = None
        self._install_capslock_hook()

        # Tell the user if the model needs to be downloaded vs just loaded
        _model_size = self.config.get('model_size', 'base')
        _model_folder = f"models--Systran--faster-whisper-{_model_size}"
        _model_cache = os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface", "hub", _model_folder
        )
        if os.path.exists(_model_cache):
            self.update_splash("Loading speech model...")
        else:
            self.update_splash("Downloading speech model (first run only, may take a few minutes)...")

        # Load model in background
        self.load_model_async()
        _boot("model load kicked off (async)")
        _bdiag("model load kicked off (async)")

        # ACE engine — always started for hold-mode dictation (ACE-03).
        # DictationSessionConsumer replaces the bespoke prebuffer + audio_callback path.
        # DebugRecorder is optional: set config["ace_debug_capture"] = true to enable.
        self._ace_engine           = None
        self._ace_debug_rec        = None
        self._dictation_consumer   = None
        self._continuous_consumer  = None
        self._wake_consumer        = None    # ACE-04C
        self._ace_dictation_active  = False   # True while hold-mode uses ACE consumer path
        self._ace_streaming_active  = False   # True while CapsLock streaming uses ACE consumer
        _t = time.perf_counter()
        self._start_ace_engine()
        _dt = (time.perf_counter() - _t) * 1000
        if self.config.get('ace_debug_capture', False) and self._ace_engine is not None:
            self._start_ace_debug_rec()
        _boot("ACE audio engine start")
        logger.info(f"[BOOT-DIAG] _start_ace_engine (total): {_dt:.0f}ms")
        if _dt > 5000:
            logger.info(f"[BOOT-DIAG] SLOW STEP: _start_ace_engine {_dt:.0f}ms")

        mode = self.config.get('mode', 'hold')
        print(f"Dictation app starting...")
        print(f"Mode: {mode}")
        print(f"Hotkey: [{self.config['hotkey']}]")
        print(f"Continuous hotkey: [{self.config.get('continuous_hotkey', 'ctrl+alt+d')}]")
        print(f"Wake word hotkey: [{self.config.get('wake_word_hotkey', 'ctrl+alt+w')}]")
        print(f"Using model: {self.config['model_size']}")
        print(f"Hotkey detection: state-based (simultaneous key support)")

        # Main hub window (sidebar nav into History/Dictionary/Settings).
        # Must be created on the Qt thread — same as all other QWidgets.
        from samsara.ui.main_window_qt import MainWindowQt
        self.main_window = None
        def _init_main_window():
            self.main_window = MainWindowQt(self)
        self._schedule_ui(_init_main_window)

        # NOTE: Splash is intentionally NOT closed here. load_model_async runs
        # the heavy Whisper/CUDA load on a background thread; closing the splash
        # before that finishes leaves the user with no indicator that the app
        # is still warming up. The model-load worker now closes the splash on
        # completion via _schedule_ui(self._on_model_loaded_close_splash).
        self.update_splash("Starting...")

        # Start config file watcher — detects external edits and reloads.
        try:
            from samsara.config_watch import ConfigWatcher
            self._config_watcher = ConfigWatcher(
                self.config_path,
                self._on_config_file_changed,
            )
            self._config_watcher.start()
            print("[CONFIG] File watcher started")
        except Exception as _cw_err:
            print(f"[CONFIG] File watcher unavailable: {_cw_err}")

        self.create_tray_icon()

    def update_splash(self, status):
        """Update splash screen status"""
        if self.splash:
            try:
                self.splash.set_status(status)
            except:
                pass

    def _close_splash_post_load(self):
        """Close the splash screen after the model has finished loading.
        Runs on the UI thread via _schedule_ui."""
        if self.splash:
            try:
                self.splash.close()
            except Exception as e:
                print(f"[SPLASH] close() failed: {e}")
            self.splash = None

    def _start_ace_engine(self) -> None:
        """Start the ACE AudioCaptureEngine and DictationSessionConsumer.

        Called unconditionally from __init__. The engine runs permanently
        at the native device rate, resampling to 16kHz int16 into the
        FrameBus ring. DictationSessionConsumer provides the hold-mode
        prebuffer rewind and frame accumulation for each utterance.
        """
        try:
            from samsara.audio_engine import FrameBus, AudioCaptureEngine
            from samsara.audio_engine.dictation_consumer import DictationSessionConsumer

            ring = FrameBus()
            # Pass the app's detected capture rate so the ACE engine opens at
            # the same sample rate as the wake word and prebuffer streams.
            # Both run on the same WASAPI device; mismatched rates cause one
            # stream to stop receiving callbacks (WASAPI dual-client starvation).
            engine_config = dict(self.config)
            engine_config['_capture_rate'] = self.capture_rate
            self._ace_engine = AudioCaptureEngine(ring, config=engine_config)
            logger.info("[BOOT-DIAG] ACE engine.start() called (sd.query_devices + sd.InputStream open)")
            _aec_open_t = getattr(self, '_aec_open_t', None)
            if _aec_open_t is not None:
                elapsed_s = time.perf_counter() - _aec_open_t
                remainder_s = _AEC_TO_MIC_MIN_GAP_MS / 1000.0 - elapsed_s
                if remainder_s > 0:
                    logger.info(
                        f"[BOOT-DIAG] AEC/ACE gap: {elapsed_s*1000:.0f}ms elapsed, "
                        f"sleeping {remainder_s*1000:.0f}ms to reach {_AEC_TO_MIC_MIN_GAP_MS}ms"
                    )
                    time.sleep(remainder_s)
            _t = time.perf_counter()
            self._ace_engine.start()
            _dt = (time.perf_counter() - _t) * 1000
            logger.info(f"[BOOT-DIAG] ACE engine.start() returned: {_dt:.0f}ms")
            if _dt > 5000:
                logger.info(f"[BOOT-DIAG] SLOW STEP: ACE engine.start() {_dt:.0f}ms")

            self._dictation_consumer = DictationSessionConsumer(
                engine=self._ace_engine,
                app=self,
            )

            from samsara.audio_engine.continuous_consumer import ContinuousConsumer
            self._continuous_consumer = ContinuousConsumer(
                engine=self._ace_engine,
                app=self,
            )

            from samsara.audio_engine.wake_consumer import WakeConsumer
            self._wake_consumer = WakeConsumer(
                engine=self._ace_engine,
                app=self,
            )

            print("[ACE] Engine started — hold / continuous / wake dictation ready")
        except Exception as exc:
            print(f"[ACE] Engine failed to start: {exc}")
            self._ace_engine         = None
            self._dictation_consumer = None

    def _start_ace_debug_rec(self) -> None:
        """Attach a DebugRecorder to the running ACE engine.

        Called from __init__ when config['ace_debug_capture'] is true.
        Writes timestamped WAVs to ~/.samsara/debug_audio/ for perceptual
        equivalence verification.
        """
        if self._ace_engine is None:
            return
        try:
            from samsara.audio_engine.debug_recorder import DebugRecorder
            output_dir = os.path.join(
                os.path.expanduser("~"), ".samsara", "debug_audio"
            )
            self._ace_debug_rec = DebugRecorder(
                engine=self._ace_engine,
                output_dir=output_dir,
                max_seconds=30.0,
            )
            self._ace_debug_rec.start_recording()
            print(f"[ACE] Debug capture active -> {output_dir}")
        except Exception as exc:
            print(f"[ACE] Debug recorder failed to start: {exc}")
            self._ace_debug_rec = None

    def _stop_ace_engine(self) -> None:
        """Deactivate all consumers, flush debug WAV, stop engine. Called from quit_app."""
        for _attr, _name in [
            ('_dictation_consumer',  'dictation'),
            ('_continuous_consumer', 'continuous'),
            ('_wake_consumer',       'wake'),
        ]:
            consumer = getattr(self, _attr, None)
            if consumer is not None:
                try:
                    consumer.deactivate()
                except Exception as exc:
                    print(f"[ACE] {_name} consumer deactivate error: {exc}")
                setattr(self, _attr, None)

        if self._ace_debug_rec is not None:
            try:
                path = self._ace_debug_rec.stop_recording()
                if path:
                    print(f"[ACE] Final debug WAV: {path}")
            except Exception as exc:
                print(f"[ACE] DebugRecorder stop error: {exc}")
            self._ace_debug_rec = None

        if self._ace_engine is not None:
            try:
                self._ace_engine.stop()
            except Exception as exc:
                print(f"[ACE] Engine stop error: {exc}")
            self._ace_engine = None

    def _show_startup_error(self, message: str):
        """Show a startup-failure dialog and exit. Runs on the UI thread."""
        self._close_splash_post_load()
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(
            None,
            "Samsara failed to start",
            f"An error occurred during startup:\n\n{message}\n\n"
            "Check the log file for the full traceback.",
        )
        self.quit_app()

    def load_config(self):
        """Load configuration from JSON file"""
        logger.debug("[CONFIG] load_config: entry")
        default_config = {
            "hotkey": "ctrl+shift",
            "continuous_hotkey": "ctrl+alt+d",
            "wake_word_hotkey": "ctrl+alt+w",
            "command_hotkey": "ctrl+alt+c",
            "undo_hotkey": "ctrl+alt+z",
            "correction_hotkey": "ctrl+alt+r",
            "cancel_hotkey": "escape",
            "mode": "hold",  # Options: "hold", "toggle", "continuous"
            "model_size": "base",
            "language": "en",
            "auto_paste": True,
            "add_trailing_space": True,
            "auto_capitalize": True,
            "format_numbers": True,
            "cleanup_mode": "clean",  # "clean" (filler removal + spacing) or "verbatim"
            "streaming_mode": False,  # live overlay partials in 'hold' mode
            "streaming_direct_paste": False,  # also paste partials into focused app
            "streaming_hotkey": "capslock",  # hotkey for streaming mode (suppressed; no caps toggle)
            "device": "auto",
            "microphone": None,
            "silence_threshold": DEFAULT_SILENCE_TIMEOUT,
            "min_speech_duration": DEFAULT_MIN_SPEECH_DURATION,
            "command_mode_enabled": False,
            "command_packs": {
                "core": True,
                "text-editing": True,
                "window-management": True,
                "browsers": True,
                "media": True,
                "smart-home": False,
                "3d-printing": False,
                "stremio": False,
                "screen-capture": False,
                "macros": False,
                "gaming": False,
                "mouse": False,
                "audio": False,
                "utilities": False,
                "smart-actions": True,
                "tasks": True,
            },
            "show_all_audio_devices": False,
            "audio_feedback": True,
            "sound_volume": 0.5,
            "sound_theme": "cute",
            "first_run_complete": True,
            "premium_license": "",
            # New nested wake word config
            "wake_word_config": {
                "enabled": True,
                "phrase": "jarvis",
                "phrase_options": ["jarvis", "hey jarvis", "computer", "hey computer", "samsa", "hey samsa"],
                "quick_silence_timeout": 1.0,
                "end_words": ["over", "done", "end dictation"],
                "cancel_words": ["cancel", "cancel dictation", "abort"],
                "pause_words": ["pause", "hold on", "wait"],
                "resume_words": ["resume", "continue", "go on"],
                "audio": {
                    "speech_threshold": DEFAULT_SPEECH_THRESHOLD,
                    "min_speech_duration": DEFAULT_MIN_SPEECH_DURATION,
                    "wake_detection_silence": WAKE_DETECTION_SILENCE,
                    "wake_command_timeout": WAKE_COMMAND_TIMEOUT,
                },
                "feedback": {
                    "play_sound_on_wake": True,
                    "play_sound_on_end": True
                }
            },
            # Phase 1 multi-wakeword: phrase -> target_process -> focus+dictate.
            # Each entry binds a spoken phrase to a target application by process name.
            # Missing oww_model -> Whisper-transcript fallback (match_wake_phrase).
            # Drop trained .onnx files into samsara/wake_models/ to enable OWW pre-filter.
            "wake_targets": [
                {
                    "id": "claude",
                    "phrase": "hey claude",
                    "oww_model": "hey_claude.onnx",
                    "target_process": "claude.exe",
                    "enabled": True,
                },
                {
                    "id": "hermes",
                    "phrase": "activate hermes",
                    "oww_model": "activate_hermes.onnx",
                    "target_process": "Hermes.exe",
                    "enabled": True,
                },
            ],
            # Echo cancellation (removes system audio from mic input)
            "echo_cancellation": {
                "enabled": False,
                "latency_ms": 30.0,
            },
            # Hub window geometry (size/position persist across sessions)
            "window_width": 900,
            "window_height": 650,
            "window_x": None,
            "window_y": None,
            # Performance mode for transcription speed/accuracy tradeoff
            "performance_mode": "balanced",  # "fast", "balanced", or "accurate"
            # Key macro system for accessibility (e.g., triple-tap W for auto-run)
            "key_macros": get_default_macro_config(),
            # Notification system for reminders (medication, breaks, hydration)
            "notifications": get_default_notification_config(),
            # Listening state indicator overlay
            "listening_indicator_enabled": False,
            "listening_indicator_position": "bottom-center",
            # Vision bridge (local Ollama vision model, opt-in)
            "vision": {
                "enabled": False,
                "model": "qwen2.5vl:3b",
                "warmup": True,
                "timeout": 90,
            },
            # Wake word listener (independent of capture mode)
            "wake_word_enabled": False,
            # Speech threshold calibration
            "threshold_mode": "auto",    # "auto" or "manual"
            "cal_multiplier": 3.0,       # multiplier above ambient for auto mode
            # Friendly aliases for Windows audio devices. Keys are spoken names
            # (match the voice command remainder); values are exact Windows
            # device names (Win+R -> mmsys.cpl to find them). Users customize
            # these by editing config.json -- no code change needed to add a device.
            "audio_devices": {
                "speakers": "Speakers",
                "headphones": "Headphones",
                "headset": "Headset Earphone",
                "earbuds": "Earbuds",
                "monitor": "DELL U2722D"
            },
            # Smart Actions: voice-to-markdown brain dump (Phase 1).
            # Per-user default lands in ~/Documents/Samsara Brain Dump.md.
            # Settings UI lets the user pick another path or disable earcons.
            "smart_actions": {
                "enabled": False,
                "brain_dump_path": str(Path.home() / "Documents" / "Samsara Brain Dump.md"),
                "earcons_enabled": True,
                "endpoint_url": "",
                "auth_header": "",
                "timeout_s": 30,
                "session_window_minutes": 5,
                "allowed_directories": [str(Path.home() / "Documents")],
                "allowed_domains": [],
                "tier2_approvals": {},
                "routing_verbs": ["ask", "plan", "summarize"],
            },
            # TTS subsystem (WinRTEngine + AudioCoordinator)
            "tts": {
                "enabled": False,   # opt-in; toggle in Settings → Text-to-Speech
                "voice_id": None,   # None = OS default voice
                "speed": 1.0,
                "pitch": 1.0,
                "volume": 0.8,
                # Per-context toggles — read by Phase 2 category-driven behavior.
                # Saved here from Settings UI but not yet acted on at runtime.
                "use_for_agent_responses": True,
                "use_for_confirmations": True,
                "use_for_warnings": True,
                "use_for_status_updates": True,
                "use_for_dictation_readback": False,
                "use_for_errors": True,
            },
            "audio_coordinator": {
                "enabled": True,
                "duck_factor": 0.7,
                "duck_default_duration_ms": 300,
                "duck_fade_ms": 5,
                "interrupt_grace_period_ms": 200,
                "speaking_wake_threshold_multiplier": 1.5,
                "speaking_vad_threshold_multiplier": 0.6,
                "thinking_pulse_interval_ms": 1000,
                "thinking_pulse_enabled": False,
            },
            # Mouse 4 walkie-talkie command mode
            "command_mode": {
                "enabled": False,           # opt-in; enable to use Mouse 4
                "mode": "hold",             # "hold" (hold to talk) or "toggle"
                "button": "mouse4",         # "mouse4" (XButton1) or "mouse5" (XButton2)
                "enter_debounce_ms": 200,   # delay before playing enter earcon
                "exit_earcon": True,        # play stop earcon on release/exit
                "miss_limit": 5,            # toggle: exit after N unmatched recordings
                "inactivity_timeout_s": 30, # toggle: exit after N seconds silence
                "tts_char_limit": 50,       # suppress TTS responses longer than this
                "suppress_button": True,    # consume mouse4/5 click so browsers don't navigate back
            },
            # Web shortcuts for "go to X" voice commands. Keys are spoken
            # aliases; values are target URLs. Users add their own by editing
            # config.json -- no code change needed.
            "web_shortcuts": {
                "mail": "https://mail.google.com",
                "email": "https://mail.google.com",
                "youtube": "https://youtube.com",
                "amazon": "https://amazon.com",
                "my orders": "https://www.amazon.com/gp/your-account/order-history",
                "github": "https://github.com",
                "reddit": "https://reddit.com"
            },
            # Gesture input lane (webcam hand-pose -> command). Opt-in; disabled
            # by default. Requires mediapipe and opencv-python in the environment.
            "gesture": {
                "enabled": False,
                "device_index": 0,
                "hold_ms": 350,
                "refractory_neutral_frames": 8,
                "min_detection_confidence": 0.6,
                "min_tracking_confidence": 0.5,
                "profile": {
                    "width": 640,
                    "height": 480,
                    "fps": 30,
                },
                "poses": {
                    "open_palm": "dictation_toggle",
                    "peace":     "ava_mode",
                    "fist":      "stop_cancel",
                    "shaka":     "window_chooser",
                },
            },
        }

        _loaded_from_disk = False
        logger.debug("[CONFIG] load_config: checking config_path existence")
        if self.config_path.exists():
            logger.debug("[CONFIG] load_config: opening config.json")
            try:
                with open(self.config_path, 'r') as f:
                    logger.debug("[CONFIG] load_config: reading JSON")
                    self.config = json.load(f)
                    logger.debug("[CONFIG] load_config: JSON loaded ok")
                _loaded_from_disk = True
            except json.JSONDecodeError as _je:
                bak_path = self.config_path.with_suffix('.json.bak')
                print(f"[CONFIG] config.json has invalid JSON: {_je}")
                if bak_path.exists():
                    try:
                        with open(bak_path, 'r') as f:
                            self.config = json.load(f)
                        _loaded_from_disk = True
                        print("[CONFIG] Loaded from config.json.bak (backup)")
                    except Exception:
                        print("[CONFIG] Backup also invalid — using defaults")
                else:
                    print("[CONFIG] No backup found — using defaults")
            except Exception:
                pass  # fall through to defaults below

        if _loaded_from_disk:
            # Migrate old flat wake word config to new nested structure
            logger.debug("[CONFIG] load_config: starting _migrate_wake_word_config")
            self._migrate_wake_word_config(default_config)
            logger.debug("[CONFIG] load_config: _migrate_wake_word_config done")

            # Fill in any missing top-level keys
            for key in default_config:
                if key not in self.config:
                    self.config[key] = default_config[key]
        else:
            self.config = default_config
            # save_config() requires _config_lock to be held; load_config is
            # always called under _config_lock so calling save_config() directly
            # (not re-acquiring the lock) is correct here.
            self.save_config()

        # Record the on-disk state so save_config can do three-way merging.
        logger.debug("[CONFIG] load_config: starting deepcopy snapshot")
        self._config_last_disk_snapshot = copy.deepcopy(self.config)
        logger.debug("[CONFIG] load_config: done")
    
    def _migrate_wake_word_config(self, default_config):
        """Migrate old flat wake word settings to new nested structure"""
        # Migrate old wake_word/combined modes to wake_word_enabled + hold
        old_mode = self.config.get('mode')
        if old_mode in ('wake_word', 'combined'):
            self.config['wake_word_enabled'] = True
            self.config['mode'] = 'hold'
            print(f"[MIGRATE] mode='{old_mode}' -> mode='hold' + wake_word_enabled=True")

        # Phase 1 multi-wakeword: inject wake_targets default when missing.
        if 'wake_targets' not in self.config:
            self.config['wake_targets'] = default_config.get('wake_targets', [])
            print("[MIGRATE] Injected default wake_targets (Phase 1 multi-wakeword)")

        # Check if we have old flat config but no new nested config
        if 'wake_word_config' not in self.config:
            # Create new nested config from defaults (deep copy so nested
            # dicts are not shared with default_config)
            import copy as _copy
            self.config['wake_word_config'] = _copy.deepcopy(default_config['wake_word_config'])

            # Migrate old values if they exist
            if 'wake_word' in self.config:
                self.config['wake_word_config']['phrase'] = self.config['wake_word']
            if 'wake_word_timeout' in self.config:
                # Old flat timeout. The 'modes' nesting no longer exists in the
                # schema; only apply if the current default actually has that
                # path, otherwise drop it silently (schema moved on).
                _wwc = self.config['wake_word_config']
                _modes = _wwc.get('modes')
                if isinstance(_modes, dict) and isinstance(_modes.get('dictate'), dict):
                    _modes['dictate']['silence_timeout'] = self.config['wake_word_timeout']
            if 'min_speech_duration' in self.config:
                self.config['wake_word_config']['audio']['min_speech_duration'] = self.config['min_speech_duration']
            
            # Save migrated config — _config_lock is already held by the
            # load_config() caller, so do not re-acquire (threading.Lock is
            # not reentrant and would deadlock).
            self.save_config()
            print("[CONFIG] Migrated wake word settings to new format")
        else:
            # Ensure all nested keys exist (for configs created between versions)
            self._deep_update(self.config['wake_word_config'], default_config['wake_word_config'])
    
    def _deep_update(self, target, source):
        """Recursively update target dict with missing keys from source"""
        for key, value in source.items():
            if key not in target:
                target[key] = value
            elif isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_update(target[key], value)
    
    def save_config(self):
        """Save configuration to JSON file atomically.

        Writes to a temp file first, then os.replace() — which is atomic on
        Windows + POSIX — swaps it into place. If serialization throws
        partway through (as happened with the MenuItem-in-config bug),
        config.json is left untouched instead of being truncated.

        Also keeps the previous good copy at config.json.bak so a future
        corruption (or a bad manual edit) can be recovered in one step.

        Caller MUST hold self._config_lock.  Use persist_config() for
        external or fire-and-forget saves where you don't already hold it.
        """
        assert self._config_lock.locked(), (
            "save_config() called without holding _config_lock! "
            "Acquire _config_lock before mutating config and calling save_config()."
        )
        tmp_path = self.config_path.with_suffix('.json.tmp')
        bak_path = self.config_path.with_suffix('.json.bak')

        try:
            # 0. Three-way merge: (last-known-disk, in-memory, current-disk).
            #    External edits (keys changed on disk since our last read/write)
            #    are preserved unless the app also changed the same key at
            #    runtime (in which case the runtime value wins).
            merged = self.config
            if self.config_path.exists():
                try:
                    with open(self.config_path, 'r') as f:
                        on_disk = json.load(f)
                    last_snap = getattr(self, '_config_last_disk_snapshot', None) or {}
                    merged = _three_way_merge(last_snap, self.config, on_disk)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"[WARN] Could not read on-disk config for merge: {e}")
                    merged = self.config

            # 1. Serialize to temp file. If json.dump raises, the real
            #    config.json is unaffected.
            with open(tmp_path, 'w') as f:
                json.dump(merged, f, indent=2)

            # 2. Back up current config.  Use shutil.copy2 (read → write to a
            #    different path) rather than os.replace/rename.  On Windows,
            #    MoveFileExW fails with access denied when any open handle on
            #    the source file lacks FILE_SHARE_DELETE — Python's default
            #    open() never sets that flag, so the config watcher's
            #    background read would block the rename.
            if self.config_path.exists():
                try:
                    shutil.copy2(self.config_path, bak_path)
                except OSError as e:
                    print(f"[WARN] Could not backup config to .bak: {e}")

            # 3. Write serialised config directly to config.json.  open() in
            #    'w' mode succeeds even while other handles have the file open
            #    for reading (Python opens with FILE_SHARE_READ|FILE_SHARE_WRITE
            #    by default), unlike os.replace() which requires FILE_SHARE_DELETE
            #    on every existing handle.
            tmp_text = tmp_path.read_text(encoding='utf-8')
            with open(self.config_path, 'w', encoding='utf-8') as f:
                f.write(tmp_text)
            try:
                tmp_path.unlink()
            except OSError:
                pass

            # 4. Sync in-memory config and snapshot to what was written.
            self.config = merged
            self._config_last_disk_snapshot = copy.deepcopy(merged)
        except Exception as e:
            # Clean up the temp file if we left one lying around
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            print(f"[ERROR] save_config failed: {e}")
            raise

    def persist_config(self) -> None:
        """Persist current in-memory config to disk. Thread-safe.

        For external callers (AlarmManager callback, settings_window) that
        don't hold _config_lock.  Acquires the lock then delegates to
        save_config().
        """
        with self._config_lock:
            self.save_config()

    def update_config_and_save(self, updates: dict) -> None:
        """Atomically apply updates to self.config and persist to disk.

        Use for simple key/value updates from any thread.
        For complex multi-step mutations acquire self._config_lock directly.
        """
        with self._config_lock:
            self.config.update(updates)
            self.save_config()

    def revoke_tier2_approvals(self) -> None:
        """Clear all memorised Tier-2 approvals and persist. Called from Settings UI."""
        with self._config_lock:
            self.config.setdefault('smart_actions', {})['tier2_approvals'] = {}
            self.save_config()

    def update_config(self, changes, save=True):
        """Apply config changes and optionally save to disk.

        Central entry point for runtime config mutations. Provides a single
        place to hook side-effects (stream restarts, UI updates) and future
        plugin notifications.

        The mutation and optional save are performed under _config_lock.
        Side-effects (apply_mode, set_wake_word_enabled, capture-rate
        detection) run after the lock is released — they may do audio work.

        Args:
            changes: dict of key-value pairs to update
            save: whether to persist to disk (default True)
        """
        with self._config_lock:
            self.config.update(changes)
            if save:
                self.save_config()

        # Side-effects outside the lock — may start/stop streams or do audio work
        if 'mode' in changes:
            self.apply_mode(changes['mode'])
        if 'wake_word_enabled' in changes:
            self.set_wake_word_enabled(changes['wake_word_enabled'])
        if 'microphone' in changes:
            self.capture_rate = self._detect_capture_rate(changes['microphone'])
        if 'gesture' in changes:
            self.set_gesture_enabled(changes['gesture'].get('enabled', False))
        if 'wake_word_config' in changes:
            new_phrase = changes['wake_word_config'].get('phrase', '')
            old_phrase = (
                (self._wake_detector._wake_phrase if self._wake_detector else '')
            )
            if new_phrase and new_phrase.lower() != old_phrase:
                self._oww_wake_detected = False
                oww_threshold = float(
                    changes['wake_word_config'].get('oww_threshold', 0.2)
                )
                self._wake_detector = WakeWordDetector(new_phrase, threshold=oww_threshold)

    def reload_config_from_disk(self) -> int:
        """Re-read config.json from disk and apply any changes to the running app.

        Returns the number of top-level keys that changed.
        Logs each changed key.  Fires the same side-effects as update_config.
        Safe to call from any thread.

        Used by the "reload config" voice command and by _on_config_file_changed.
        """
        try:
            with open(self.config_path, 'r') as f:
                new_disk = json.load(f)
        except json.JSONDecodeError as e:
            print(f"[CONFIG] reload_config_from_disk: invalid JSON — {e}")
            return 0
        except OSError as e:
            print(f"[CONFIG] reload_config_from_disk: could not read file — {e}")
            return 0
        return self._apply_disk_config(new_disk)

    def _on_config_file_changed(self, new_disk_config: dict) -> None:
        """Callback from ConfigWatcher when an external edit is detected."""
        self._apply_disk_config(new_disk_config)

    def _apply_disk_config(self, new_disk_config: dict) -> int:
        """Apply a freshly-read on-disk config to the running app.

        Performs a three-way merge (last-snapshot + memory + disk) so that
        external edits win for keys the app hasn't touched at runtime, while
        runtime state wins for keys the app actively manages.

        Returns the number of top-level keys that changed.
        """
        changed: dict = {}
        with self._config_lock:
            last_snap = self._config_last_disk_snapshot or {}
            merged = _three_way_merge(last_snap, self.config, new_disk_config)

            # Compute diff for logging and side-effects
            all_keys = set(self.config) | set(merged)
            for k in all_keys:
                old_v = self.config.get(k, _MISSING)
                new_v = merged.get(k, _MISSING)
                if old_v != new_v:
                    changed[k] = (old_v, new_v)

            self.config = merged
            self._config_last_disk_snapshot = copy.deepcopy(new_disk_config)

        for key, (old_v, new_v) in changed.items():
            print(f"[CONFIG] External edit detected: {key} changed "
                  f"{old_v!r} -> {new_v!r}")

        # Fire the same side-effects as update_config
        if 'mode' in changed:
            try:
                self.apply_mode(changed['mode'][1])
            except Exception as e:
                print(f"[CONFIG] apply_mode error: {e}")
        if 'wake_word_enabled' in changed:
            try:
                self.set_wake_word_enabled(changed['wake_word_enabled'][1])
            except Exception as e:
                print(f"[CONFIG] set_wake_word_enabled error: {e}")
        if 'microphone' in changed:
            try:
                self.capture_rate = self._detect_capture_rate(changed['microphone'][1])
            except Exception as e:
                print(f"[CONFIG] capture_rate update error: {e}")
        if 'wake_word_config' in changed:
            try:
                new_ww = changed['wake_word_config'][1]
                if isinstance(new_ww, dict):
                    new_phrase = new_ww.get('phrase', '')
                    old_phrase = (
                        self._wake_detector._wake_phrase if self._wake_detector else ''
                    )
                    if new_phrase and new_phrase.lower() != old_phrase:
                        self._oww_wake_detected = False
                        oww_threshold = float(new_ww.get('oww_threshold', 0.2))
                        self._wake_detector = WakeWordDetector(
                            new_phrase, threshold=oww_threshold
                        )
            except Exception as e:
                print(f"[CONFIG] wake_word_config update error: {e}")

        return len(changed)

    def set_app_state(self, **kwargs):
        """Update application state flags with transition logging.

        Centralizes critical state changes (recording, mode activation) so
        transitions are visible in the console log.
        """
        for key, value in kwargs.items():
            if not hasattr(self, key):
                print(f"[WARN] Unknown state key: {key}")
                continue
            old = getattr(self, key)
            if old != value:
                setattr(self, key, value)
                print(f"[STATE] {key}: {old} -> {value}")

    def _detect_capture_rate(self, device_id):
        """Query the native sample rate of a device. Falls back to DEFAULT_CAPTURE_RATE."""
        try:
            if device_id is not None:
                info = sd.query_devices(device_id)
                rate = int(info['default_samplerate'])
                print(f"[AUDIO] Device {device_id} native rate: {rate}Hz")
                return rate
        except Exception as e:
            print(f"[WARN] Could not query device {device_id} rate: {e}")
        return DEFAULT_CAPTURE_RATE

    def _run_calibration_if_auto(self):
        """Run mic calibration if threshold_mode is 'auto'. Updates config in place."""
        mode = self.config.get('threshold_mode', 'auto')
        if mode != 'auto':
            thresh = self.config.get('wake_word_config', {}).get('audio', {}).get(
                'speech_threshold', DEFAULT_SPEECH_THRESHOLD)
            print(f"[CAL] Threshold mode: manual ({thresh:.4f})")
            return

        mic_id = self.config.get('microphone')
        multiplier = self.config.get('cal_multiplier', 3.0)
        try:
            rms_samples = measure_ambient_rms(mic_id, self.capture_rate)
            threshold = calibrate_threshold(rms_samples, multiplier=multiplier)
            ambient = float(np.median(rms_samples)) if rms_samples else 0.0
            print(f"[CAL] Ambient RMS: {ambient:.4f} | "
                  f"Multiplier: {multiplier}x | Threshold: {threshold:.4f}")
        except Exception as e:
            threshold = DEFAULT_SPEECH_THRESHOLD
            print(f"[CAL] Calibration failed ({e}), using default {threshold:.4f}")

        # Apply to wake word audio config
        with self._config_lock:
            ww_config = self.config.get('wake_word_config', {})
            if 'audio' not in ww_config:
                ww_config['audio'] = {}
            ww_config['audio']['speech_threshold'] = threshold
            self.config['wake_word_config'] = ww_config

    def recalibrate_mic(self):
        """Re-run calibration in background and update config."""
        def _do():
            self._run_calibration_if_auto()
            self.persist_config()
        threading.Thread(target=_do, daemon=True).start()

    def get_available_microphones(self):
        """Get list of available microphone devices.

        Filters to WASAPI devices only (Windows) to avoid duplicates — the same
        physical mic appears once per host API (MME, DirectSound, WASAPI, WDM-KS)
        with different names and truncation rules. WASAPI is the preferred API
        and gives full-length, consistent device names.
        """
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        microphones = []
        seen_names = set()
        show_all = self.config.get('show_all_audio_devices', False)

        # Filter to WASAPI devices (preferred for full-length names and low latency).
        # Streams now open at the device's native rate and resample to 16kHz for Whisper.
        preferred_api_idx = None
        for idx, api in enumerate(hostapis):
            if 'WASAPI' in api['name']:
                preferred_api_idx = idx
                break

        for i, device in enumerate(devices):
            if device['max_input_channels'] <= 0:
                continue

            # Filter to preferred API only (unless show_all is enabled or API not found)
            if preferred_api_idx is not None and not show_all:
                if device['hostapi'] != preferred_api_idx:
                    continue

            name = device['name']

            # Deduplicate by normalized name (strip + lowercase)
            dedup_key = name.strip().lower()
            if dedup_key in seen_names:
                continue
            
            if not show_all:
                skip_keywords = [
                    'Stereo Mix', 'Wave Out Mix', 'What U Hear', 'Loopback', 
                    'CABLE', 'Virtual Audio', 'VB-Audio', 'Voicemeeter',
                    'Sound Mapper', 'Primary Sound', 'Wave Speaker', 'Wave Microphone',
                    'Stream Wave', 'Chat Capture', 'Hands-Free', 'HF Audio', 'Input ()',
                    'Line In (', 'VDVAD', 'SteelSeries Sonar', 'OCULUSVAD',
                    'VAD Wave', 'wc4400_8200'
                ]
                if any(kw.lower() in name.lower() for kw in skip_keywords):
                    continue
                if name.strip() == "Microphone ()":
                    continue
                if '@System32\\drivers\\' in name:
                    continue
                
            seen_names.add(dedup_key)
            microphones.append({
                'id': i,
                'name': name,
                'channels': device['max_input_channels']
            })
        
        return microphones
    
    def get_current_microphone_name(self):
        """Get the name of the currently selected microphone"""
        mic_id = self.config.get('microphone')
        if mic_id is None:
            return "Default"

        for mic in self.available_mics:
            if mic['id'] == mic_id:
                return mic['name']

        return "Unknown"

    def load_history(self):
        """Load history from file"""
        try:
            if self.history_path.exists():
                with open(self.history_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Convert lists back to tuples
                    return [tuple(item) for item in data]
        except Exception as e:
            print(f"Failed to load history: {e}")
        return []

    def save_history(self):
        """Save history to file"""
        try:
            with open(self.history_path, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Failed to save history: {e}")

    def get_transcription_params(self):
        """Get transcription parameters based on performance mode setting.
        
        Returns dict of parameters for model.transcribe()
        Performance modes:
        - fast: Lowest latency, may sacrifice some accuracy
        - balanced: Good speed/accuracy tradeoff (default)
        - accurate: Best accuracy, slower
        """
        mode = self.config.get('performance_mode', 'balanced')
        
        base_params = {
            'language': self.config['language'],
            'initial_prompt': self.voice_training_window.get_initial_prompt(),
        }
        
        if mode == 'fast':
            # Fastest settings - greedy decoding, minimal VAD
            return {
                **base_params,
                'beam_size': 1,  # Greedy decoding (fastest)
                'vad_filter': True,
                'vad_parameters': dict(
                    min_silence_duration_ms=300,
                    speech_pad_ms=100,
                ),
                'condition_on_previous_text': False,
                'without_timestamps': True,
                'word_timestamps': False,
                'temperature': 0.0,  # Deterministic (faster)
            }
        elif mode == 'accurate':
            # Most accurate settings
            return {
                **base_params,
                'beam_size': 5,
                'vad_filter': True,
                'vad_parameters': dict(
                    min_silence_duration_ms=500,
                    speech_pad_ms=300,
                ),
                'condition_on_previous_text': True,
                'without_timestamps': False,
                'word_timestamps': False,
            }
        else:  # balanced (default)
            return {
                **base_params,
                'beam_size': 3,
                'vad_filter': True,
                'vad_parameters': dict(
                    min_silence_duration_ms=500,
                    speech_pad_ms=200,
                ),
                'condition_on_previous_text': False,
                'without_timestamps': True,
                'word_timestamps': False,
            }

    def process_transcription(self, text):
        """Process transcribed text with auto-capitalize and number formatting"""
        self._skip_cleanup = False
        if not text:
            return text

        # Case formatter: first-token-only, opt-in via enable_case_formatters config
        if self.config.get('enable_case_formatters', False):
            from samsara.formatters import apply_case_formatter
            _formatted = apply_case_formatter(text)
            if _formatted is not None:
                self._skip_cleanup = True   # tell caller to bypass clean_text
                return _formatted

        # Number word to digit mapping
        number_words = {
            'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
            'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
            'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
            'fourteen': '14', 'fifteen': '15', 'sixteen': '16', 'seventeen': '17',
            'eighteen': '18', 'nineteen': '19', 'twenty': '20', 'thirty': '30',
            'forty': '40', 'fifty': '50', 'sixty': '60', 'seventy': '70',
            'eighty': '80', 'ninety': '90', 'hundred': '100', 'thousand': '1000',
            'million': '1000000', 'billion': '1000000000',
        }

        # Format numbers (e.g., "twenty one" -> "21")
        if self.config.get('format_numbers', True):
            # Handle compound numbers like "twenty one", "thirty five"
            tens = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
                    'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90}
            ones = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9}

            # Pattern for "twenty one" style numbers
            for ten_word, ten_val in tens.items():
                for one_word, one_val in ones.items():
                    pattern = rf'\b{ten_word}[\s-]{one_word}\b'
                    text = re.sub(pattern, str(ten_val + one_val), text, flags=re.IGNORECASE)

            # Replace standalone number words
            words = text.split()
            new_words = []
            for word in words:
                # Preserve punctuation attached to word
                prefix = ''
                suffix = ''
                core = word

                # Extract leading/trailing punctuation
                while core and not core[0].isalnum():
                    prefix += core[0]
                    core = core[1:]
                while core and not core[-1].isalnum():
                    suffix = core[-1] + suffix
                    core = core[:-1]

                # Check if core word is a number word
                if core.lower() in number_words:
                    new_words.append(prefix + number_words[core.lower()] + suffix)
                else:
                    new_words.append(word)

            text = ' '.join(new_words)

        # Auto-capitalize
        if self.config.get('auto_capitalize', True):
            if text:
                # Capitalize first letter
                text = text[0].upper() + text[1:] if len(text) > 1 else text.upper()

                # Capitalize after sentence-ending punctuation
                # Match . ! ? followed by space and lowercase letter
                def capitalize_after(match):
                    return match.group(1) + match.group(2).upper()

                text = re.sub(r'([.!?]\s+)([a-z])', capitalize_after, text)

        return text

    def add_to_history(self, text, is_command=False):
        """Add a transcription to history"""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.history.append((timestamp, text, is_command))
        # Keep only last N items
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        # Save to file
        self.save_history()

    def _get_foreground_app(self):
        """Return the title of the currently focused window, or 'Unknown'."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                return buf.value
        except Exception:
            pass
        return "Unknown"

    def _log_history(self, raw_text, display_text=None, duration_ms=0,
                     mode="hold", status="success", app_context=None,
                     entry_type="dictation", log_prob=None,
                     matched_command=None):
        """Write one entry to the persistent SQLite history (best-effort).

        Wrapped so callers don't need to null-check or try/except every site.
        Failures here must never break a transcription.
        """
        if self.history_db is None:
            return
        try:
            self.history_db.add(
                raw_text=raw_text,
                display_text=display_text if display_text is not None else raw_text,
                app_context=app_context if app_context is not None else self._get_foreground_app(),
                duration_ms=int(duration_ms),
                mode=mode,
                status=status,
                entry_type=entry_type,
                log_prob=log_prob,
                matched_command=matched_command,
            )
        except Exception as e:
            print(f"[HISTORY] log failed: {e}")

    def _notify_main_window(self, text):
        """Direct callback into the hub window (no event bus).

        Updates its 'last transcription' status preview and refreshes the
        history list without waiting for the next 5s poll. Best-effort:
        the hub is optional, so any failure here is swallowed.
        """
        win = getattr(self, 'main_window', None)
        if win is None:
            return
        try:
            win.on_dictation_complete(text)
        except Exception as e:
            print(f"[UI] main window notify failed: {e}")

    def _is_audio_capture_active(self) -> bool:
        """True if ANY audio input stream is currently open.

        Used to guard mic-list refreshes — sd.query_devices() can stutter
        active PortAudio streams on some drivers.
        """
        return (
            self.recording
            or self.continuous_active
            or self.wake_word_active
            or (self._ace_engine is not None and self._ace_engine._running)
        )

    def _reconcile_microphone_selection(self) -> None:
        """Reconcile self.config['microphone'] against the current device list using name.

        PortAudio indices are not stable across reconnects or reboots.
        If a stored microphone_name is found in the current list under a different
        index, the config is updated silently so the right device is used.
        Does NOT save — the caller decides whether to persist.
        """
        stored_name = self.config.get('microphone_name')
        if not stored_name:
            return  # older config with no stored name — no-op

        for mic in self.available_mics:
            if mic['name'] == stored_name:
                if self.config.get('microphone') != mic['id']:
                    old_idx = self.config.get('microphone')
                    self.config['microphone'] = mic['id']
                    print(f"[MIC] Reconciled '{stored_name}': index {old_idx} -> {mic['id']}")
                return  # found — whether index changed or not, we're done

        print(f"[MIC] Selected device '{stored_name}' not currently available "
              "— keeping last-known index")

    def switch_microphone(self, mic_id):
        """Switch to a different microphone at runtime.

        Stops every active audio stream, updates config, then restarts the
        streams that the current mode needs — bound to the new device this time.
        Without the restart, PortAudio streams continue capturing from the
        old device because the device ID is fixed at stream-construction time.
        """
        if self.config.get('microphone') == mic_id:
            return  # already on this mic, no-op

        # Remember what was running so we can restore it on the new device
        was_continuous = self.continuous_active
        was_wake_word = self.wake_word_active
        was_recording = self.recording

        # Stop everything first (order matters: active recording before its host stream)
        if was_recording:
            # Cancel rather than transcribe — the audio was captured on the wrong device
            self.cancel_recording()
        if was_continuous:
            self.stop_continuous_mode()
        if was_wake_word:
            self.stop_wake_word_mode()

        # Update config fields (mutations under lock, audio work outside)
        mic_entry = next((m for m in self.available_mics if m['id'] == mic_id), None)
        with self._config_lock:
            self.config['microphone'] = mic_id
            if mic_entry:
                self.config['microphone_name'] = mic_entry['name']
        self.capture_rate = self._detect_capture_rate(mic_id)
        self._run_calibration_if_auto()  # internally locks its own mutation
        self.persist_config()

        mic_name = self.get_current_microphone_name()
        print(f"[OK] Switched to microphone: {mic_name} ({self.capture_rate}Hz)")

        # Restart ACE engine on new device — bumps device_epoch so any
        # in-flight consumer sees the discontinuity via frame.device_epoch.
        if self._ace_engine is not None:
            try:
                self._ace_engine.bump_device_epoch()
                self._ace_engine.stop()
                self._ace_engine._config['microphone']    = mic_id
                self._ace_engine._config['_capture_rate'] = self.capture_rate
                self._ace_engine.start()
                print("[ACE] Engine restarted on new device")
            except Exception as exc:
                print(f"[ACE] Engine restart on mic switch failed: {exc}")

        # Restart whatever was running, now bound to the new device
        if was_wake_word:
            self.start_wake_word_mode()
        if was_continuous:
            self.start_continuous_mode()

        # Update tray icon tooltip
        self._update_tray_tooltip()

    def load_model_async(self):
        """Load Whisper model in background thread"""
        self._startup_failed = False

        def load():
          try:
            self.loading_model = True
            print("[INIT] Loading Whisper model...")
            
            # Determine compute device with detailed logging
            device = self.config['device']

            # Safety net: if config says CUDA but the runtime DLLs aren't
            # present (e.g. user installed CPU-only build, or moved CUDA pack
            # away), fall back to CPU silently rather than crashing at model
            # load time with "cublas64_12.dll not found".
            from samsara.cuda_detect import resolve_device, is_cuda_available
            if device == "cuda" and not is_cuda_available():
                print("[GPU] Config requested CUDA but CUDA pack not detected — "
                      "falling back to CPU. Install Samsara-CUDA-Pack to enable GPU.")
                device = "cpu"

            if device == "auto":
                try:
                    import ctranslate2
                    cuda_available = 'cuda' in ctranslate2.get_supported_compute_types('cuda')
                    if cuda_available:
                        device = "cuda"
                        print("[GPU] CUDA available via ctranslate2")
                    else:
                        device = "cpu"
                        print("[CPU] CUDA not available, using CPU")
                except Exception as e:
                    device = "cpu"
                    print(f"[CPU] Could not detect GPU: {e}")
            
            compute_type = "float16" if device == "cuda" else "int8"
            print(f"[CONFIG] Model: {self.config['model_size']}, Device: {device}, Compute: {compute_type}")
            
            load_start = time.time()
            self.model = WhisperModel(
                self.config['model_size'],
                device=device,
                compute_type=compute_type,
                cpu_threads=4,  # Use multiple CPU threads if on CPU
                num_workers=2,  # Parallel workers for preprocessing
            )
            load_time = time.time() - load_start
            
            # Store device info for logging
            self.device_type = device
            self.compute_type = compute_type
            
            self.model_loaded = True
            self.loading_model = False
            print(f"[OK] Model loaded in {load_time:.1f}s ({device}, {compute_type})")

            # Marshal to UI thread: close the splash now that the app is
            # truly ready to dictate. Until this point, the splash has been
            # showing "Loading speech model..." which is accurate.
            try:
                if self.splash:
                    self._schedule_ui(self._close_splash_post_load)
            except Exception as e:
                print(f"[SPLASH] Could not close splash: {e}")

            _boot_log = getattr(self, '_boot_log', lambda s: None)
            print("[INIT] Loading Silero VAD...")
            # Load Silero VAD for real-time speech gating (async-safe: if this
            # fails, the wake callback falls back to RMS).
            self._load_vad_model()
            _boot_log("async: Silero VAD load")

            print("[INIT] Loading OpenWakeWord pre-filter...")
            self._load_oww_model()
            self._load_wake_target_models()
            _boot_log("async: OpenWakeWord model load")

            print("Ready for dictation.")

            # Auto-start modes that require always-on listening
            mode = self.config.get('mode', 'hold')
            if mode == 'continuous':
                print("[AUTO] Starting continuous mode...")
                self.start_continuous_mode()

            print("[INIT] Starting audio streams...")
            # Hold/toggle: ACE engine ring provides rolling pre-buffer (ACE-03).
            # No separate prebuffer PortAudio stream needed at startup.

            # Auto-start wake word listener if enabled (works alongside any mode)
            if self.config.get('wake_word_enabled', False):
                print("[AUTO] Starting wake word listener...")
                self.start_wake_word_mode()
            _boot_log("async: wake word + audio stream start")

            # Auto-start gesture lane if enabled
            if self.config.get('gesture', {}).get('enabled', False):
                self._start_gesture_lane()

            print("[INIT] Startup complete.")

            # Ensure clean state — reset any recording flags that may have
            # been tripped by keyboard events during startup
            self.recording = False
            self.hotkey_pressed = False
            self.command_mode_recording = False
            self._hotkey_recording = False
            if hasattr(self, 'listening_indicator'):
                self._schedule_ui(self.listening_indicator.set_listening, False)
          except Exception as _exc:
            import traceback
            traceback.print_exc()
            self.loading_model = False
            self._startup_failed = True
            err_msg = str(_exc)
            self.update_splash(f"Startup error: {err_msg}")
            self._schedule_ui(self._show_startup_error, err_msg)

        thread = threading.Thread(target=load, daemon=True)
        thread.start()
    
    def parse_hotkey(self, hotkey_str):
        """Parse hotkey string into set of key names"""
        parts = hotkey_str.lower().split('+')
        keys = set()
        for part in parts:
            part = part.strip()
            if part in ('ctrl', 'control'):
                keys.add('ctrl')
            elif part in ('shift',):
                keys.add('shift')
            elif part in ('alt',):
                keys.add('alt')
            elif part in ('win', 'super', 'cmd'):
                keys.add('win')
            else:
                keys.add(part)
        return keys

    def get_key_name(self, key):
        """Get normalized key name"""
        try:
            if hasattr(key, 'char') and key.char:
                return key.char.lower()
            elif hasattr(key, 'name'):
                name = key.name.lower()
                if 'ctrl' in name:
                    return 'ctrl'
                elif 'shift' in name:
                    return 'shift'
                elif 'alt' in name:
                    return 'alt'
                elif 'win' in name or 'super' in name or 'cmd' in name:
                    return 'win'
                return name
        except:
            pass
        return None
    
    def get_active_keys(self):
        """Get keys pressed within the hotkey window (legacy, kept for compatibility)"""
        now = time.time()
        active_keys = set()
        for key, press_time in list(self.key_press_times.items()):
            if now - press_time < self.hotkey_window:
                active_keys.add(key)
            elif key not in self.current_keys:
                # Clean up old entries
                del self.key_press_times[key]
        # Also include currently held keys
        return active_keys | self.current_keys
    
    def check_hotkey_state(self, hotkey_str):
        """Check if all keys in a hotkey combo are currently pressed using state-based detection.
        
        This uses the keyboard library's is_pressed() for reliable simultaneous key detection,
        regardless of the order keys were pressed.
        """
        required_keys = self.parse_hotkey(hotkey_str)
        
        for key in required_keys:
            # Map our key names to keyboard library names
            if key == 'ctrl':
                if not (keyboard.is_pressed('ctrl') or keyboard.is_pressed('left ctrl') or keyboard.is_pressed('right ctrl')):
                    return False
            elif key == 'shift':
                if not (keyboard.is_pressed('shift') or keyboard.is_pressed('left shift') or keyboard.is_pressed('right shift')):
                    return False
            elif key == 'alt':
                if not (keyboard.is_pressed('alt') or keyboard.is_pressed('left alt') or keyboard.is_pressed('right alt')):
                    return False
            elif key == 'win':
                if not (keyboard.is_pressed('left windows') or keyboard.is_pressed('right windows')):
                    return False
            elif key == 'escape':
                if not keyboard.is_pressed('esc'):
                    return False
            else:
                # Regular key (letter, number, etc.)
                if not keyboard.is_pressed(key):
                    return False
        
        return True
    
    def get_pressed_keys_debug(self):
        """Return a string of currently pressed keys for debugging"""
        pressed = []
        for key in ['ctrl', 'shift', 'alt', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', 'esc']:
            try:
                if keyboard.is_pressed(key):
                    pressed.append(key)
            except:
                pass
        return '+'.join(pressed) if pressed else 'none'

    def on_key_press(self, key):
        """Handle key press - uses state-based checking for reliable simultaneous key detection"""
        key_name = self.get_key_name(key)
        if key_name:
            self.current_keys.add(key_name)
            self.key_press_times[key_name] = time.time()

        self._check_command_mode_key(key, pressed=True)

        # While snoozed, still track key state and allow alarm hotkeys,
        # but skip all dictation/recording hotkeys
        if self.snoozed:
            # Check for alarm hotkeys even while snoozed
            if hasattr(self, 'alarm_manager') and self.alarm_manager.is_nagging():
                complete_hotkey = self.alarm_manager.complete_hotkey
                dismiss_hotkey = self.alarm_manager.dismiss_hotkey
                if self.check_hotkey_state(complete_hotkey):
                    self.alarm_manager.complete()
                    self.play_sound('success')
                    return
                if self.check_hotkey_state(dismiss_hotkey):
                    self.alarm_manager.dismiss()
                    self.play_sound('stop')
                    return
            return

        mode = self.config.get('mode', 'hold')

        # Get hotkey configs
        main_hotkey = self.config['hotkey']
        cont_hotkey = self.config.get('continuous_hotkey', 'ctrl+alt+d')
        wake_hotkey = self.config.get('wake_word_hotkey', 'ctrl+alt+w')
        command_hotkey = self.config.get('command_hotkey', 'ctrl+alt+c')
        cancel_hotkey = self.config.get('cancel_hotkey', 'escape')

        # Use state-based detection - checks if keys are CURRENTLY held, regardless of press order
        # This is more reliable than event-based tracking for simultaneous key combos

        # Check for command-only hotkey (hold to record, match commands only, no text output)
        if self.check_hotkey_state(command_hotkey) and not self.hotkey_pressed and not self.recording:
            if self._stop_in_flight:
                print("[HOTKEY] Ignored re-trigger while stop in flight")
                return
            print(f"[HOTKEY] Command hotkey detected: {command_hotkey}")
            self.hotkey_pressed = True
            self.command_mode_recording = True
            self.start_recording(streaming=False)
            return
        
        # Undo hotkey (works in any mode, edge-triggered)
        undo_hotkey = self.config.get('undo_hotkey', 'ctrl+alt+z')
        if self.check_hotkey_state(undo_hotkey) and not self.hotkey_pressed:
            print(f"[HOTKEY] Undo hotkey detected: {undo_hotkey}")
            self.hotkey_pressed = True
            threading.Thread(target=self.undo_last_dictation, daemon=True).start()
            return

        # Correction report hotkey (works in any mode, edge-triggered)
        correction_hotkey = self.config.get('correction_hotkey', 'ctrl+alt+r')
        if self.check_hotkey_state(correction_hotkey) and not self.hotkey_pressed:
            print(f"[HOTKEY] Correction hotkey detected: {correction_hotkey}")
            self.hotkey_pressed = True
            self._schedule_ui(self._report_correction_dialog)
            return

        # Check for wake word enable/disable toggle (works in any mode)
        if self.check_hotkey_state(wake_hotkey) and not self.hotkey_pressed:
            print(f"[HOTKEY] Wake word hotkey detected: {wake_hotkey}")
            self.hotkey_pressed = True
            new_state = not self.config.get('wake_word_enabled', False)
            threading.Thread(target=self.set_wake_word_enabled,
                             args=(new_state,), daemon=True).start()
            return
        
        # Check for continuous mode toggle (works in any mode)
        if self.check_hotkey_state(cont_hotkey) and not self.hotkey_pressed:
            print(f"[HOTKEY] Continuous mode hotkey detected: {cont_hotkey}")
            self.hotkey_pressed = True
            self.toggle_continuous_mode()
            return

        # Check for cancel recording hotkey (only when recording)
        if self.check_hotkey_state(cancel_hotkey) and self.recording:
            print(f"[HOTKEY] Cancel hotkey detected: {cancel_hotkey}")
            self.cancel_recording()
            return

        # Check for alarm hotkeys (when an alarm is nagging)
        if hasattr(self, 'alarm_manager') and self.alarm_manager.is_nagging():
            complete_hotkey = self.alarm_manager.complete_hotkey
            dismiss_hotkey = self.alarm_manager.dismiss_hotkey
            
            # Check for complete hotkey (user did the task, gets streak credit)
            if self.check_hotkey_state(complete_hotkey):
                print(f"[HOTKEY] Alarm complete hotkey detected: {complete_hotkey}")
                self.alarm_manager.complete()
                self.play_sound('success')  # Success sound for completion
                return
            
            # Check for dismiss hotkey (just silence, no credit, breaks streak)
            if self.check_hotkey_state(dismiss_hotkey):
                print(f"[HOTKEY] Alarm dismiss hotkey detected: {dismiss_hotkey}")
                self.alarm_manager.dismiss()
                self.play_sound('stop')  # Neutral sound for dismissal
                return

        # Handle main hotkey based on mode.
        # Belt-and-braces: require BOTH the OS keyboard state (via
        # check_hotkey_state) AND pynput's event-tracked self.current_keys
        # to agree that every required key is held. This catches stale
        # OS state from synthesized events leaving e.g. shift "pressed"
        # when the user only physically holds ctrl.
        required_keys = self.parse_hotkey(main_hotkey)
        main_event_held = required_keys.issubset(self.current_keys)
        if (self.check_hotkey_state(main_hotkey)
                and main_event_held
                and not self.hotkey_pressed):
            if self._stop_in_flight:
                print("[HOTKEY] Ignored re-trigger while stop in flight")
                return
            print(f"[HOTKEY] Main hotkey detected: {main_hotkey} (mode: {mode})")
            if mode == 'hold':
                self.hotkey_pressed = True
                # Ctrl+Shift always drives batch mode -- streaming uses
                # CapsLock as its dedicated hotkey.
                self.start_recording(streaming=False)
            elif mode == 'toggle':
                self.hotkey_pressed = True
                if self.toggle_active:
                    self.toggle_active = False
                    self.stop_recording()
                else:
                    self.toggle_active = True
                    self.start_recording(streaming=False)
            elif mode == 'continuous':
                # In continuous mode, main hotkey toggles continuous listening
                self.hotkey_pressed = True
                self.toggle_continuous_mode()
    
    def on_key_release(self, key):
        """Handle key release - uses state-based checking for reliable detection"""
        key_name = self.get_key_name(key)
        if key_name and key_name in self.current_keys:
            self.current_keys.discard(key_name)

        self._check_command_mode_key(key, pressed=False)

        mode = self.config.get('mode', 'hold')
        
        # Get hotkey configs
        main_hotkey = self.config['hotkey']
        cont_hotkey = self.config.get('continuous_hotkey', 'ctrl+alt+d')
        wake_hotkey = self.config.get('wake_word_hotkey', 'ctrl+alt+w')
        command_hotkey = self.config.get('command_hotkey', 'ctrl+alt+c')
        
        # Reset hotkey flag when no hotkey combo is currently pressed
        # Use state-based checking for reliable detection
        main_pressed = self.check_hotkey_state(main_hotkey)
        cont_pressed = self.check_hotkey_state(cont_hotkey)
        wake_pressed = self.check_hotkey_state(wake_hotkey)
        command_pressed = self.check_hotkey_state(command_hotkey)
        
        if not main_pressed and not cont_pressed and not wake_pressed and not command_pressed:
            if self.hotkey_pressed:
                def _deferred_stop():
                    try:
                        self.stop_recording()
                    finally:
                        self._stop_in_flight = False

                if self.command_mode_recording and self.recording:
                    print(f"[HOTKEY] Command hotkey released, stopping recording")
                    self._stop_in_flight = True
                    threading.Thread(target=_deferred_stop, daemon=True,
                                     name='stop-rec').start()
                    self.hotkey_pressed = False
                elif mode == 'hold' and self.recording:
                    print(f"[HOTKEY] Main hotkey released, stopping recording")
                    self._stop_in_flight = True
                    threading.Thread(target=_deferred_stop, daemon=True,
                                     name='stop-rec').start()
                    self.hotkey_pressed = False
                else:
                    self.hotkey_pressed = False

    # ---- CapsLock streaming hotkey --------------------------------------

    def _install_capslock_hook(self):
        """Hook CapsLock with the keyboard library so it drives streaming
        dictation without ever toggling the system caps state.

        suppress=True means the OS never sees the CapsLock event -- no
        toggle, no LED change, no caps. Our callback decides whether to
        start/stop streaming based on the live streaming_mode config.

        IMPORTANT: This hook is only installed when streaming_mode is
        actually enabled. When streaming is off, we leave CapsLock alone
        so it works as a normal Windows toggle. set_streaming_mode()
        installs/uninstalls the hook dynamically when the user toggles it.

        We register an atexit cleanup so the hook is released even if
        Samsara crashes or is killed via Task Manager -- without this
        the user can be left with a CapsLock key the OS thinks is
        permanently consumed."""
        # Bail out if streaming mode is off -- no need to grab CapsLock
        if not self.config.get('streaming_mode', False):
            self._capslock_hook = None
            return

        try:
            self._capslock_hook = keyboard.hook_key(
                'caps lock', self._on_capslock_event, suppress=True)
        except Exception as e:
            print(f"[CAPSLOCK] Failed to install hook: {e}")
            self._capslock_hook = None
            return

        import atexit
        hook_ref = self._capslock_hook

        def _cleanup_capslock_hook():
            try:
                keyboard.unhook(hook_ref)
            except Exception:
                pass

        atexit.register(_cleanup_capslock_hook)

    def _uninstall_capslock_hook(self):
        """Release the CapsLock hook so the OS gets the key back. Called
        when streaming_mode is toggled off so CapsLock works normally."""
        if getattr(self, '_capslock_hook', None) is None:
            return
        try:
            keyboard.unhook(self._capslock_hook)
            print("[CAPSLOCK] Hook released — CapsLock returned to OS")
        except Exception as e:
            print(f"[CAPSLOCK] Failed to release hook: {e}")
        self._capslock_hook = None
        self._capslock_held = False

    def _on_capslock_event(self, event):
        """Hooked CapsLock handler. Runs on the keyboard library's hook
        thread -- spawn worker threads for blocking work."""
        try:
            if self.snoozed:
                return
            if not self.config.get('streaming_mode', False):
                return  # event still suppressed; we just don't trigger
            if not self.model_loaded:
                return

            if event.event_type == keyboard.KEY_DOWN:
                if self._capslock_held:
                    return  # ignore auto-repeat while held
                self._capslock_held = True
                threading.Thread(
                    target=self._capslock_start_streaming,
                    daemon=True, name="capslock-start").start()
            elif event.event_type == keyboard.KEY_UP:
                if not self._capslock_held:
                    return
                self._capslock_held = False
                threading.Thread(
                    target=self._capslock_stop_streaming,
                    daemon=True, name="capslock-stop").start()
        except Exception as e:
            print(f"[CAPSLOCK] event handler crashed: {e}")

    def _capslock_start_streaming(self):
        """Worker: start a streaming-mode recording. Wrapped so we can
        guard against re-entry if the user hammers CapsLock."""
        try:
            if self.recording:
                return
            print("[CAPSLOCK] press -> streaming start")
            self.start_recording(streaming=True)
        except Exception as e:
            print(f"[CAPSLOCK] start failed: {e}")

    def _capslock_stop_streaming(self):
        """Worker: stop the streaming recording on CapsLock release."""
        try:
            if not self.recording:
                return
            print("[CAPSLOCK] release -> streaming stop")
            self.stop_recording()
        except Exception as e:
            print(f"[CAPSLOCK] stop failed: {e}")

    # ---- Mouse 4 command mode (walkie-talkie hold-to-talk) ----------------

    def _install_mouse_listener(self):
        """Start the Win32 low-level mouse hook for Mouse 4/5 command mode.

        Only installed when command_mode.button is a mouse source.
        Keyboard sources (rctrl, f13, etc.) are handled by on_key_press/release.
        """
        cfg = self.config.get('command_mode', {})
        btn = cfg.get('button', 'mouse4')
        if btn not in ('mouse4', 'mouse5'):
            self._mouse_hook = None
            return

        should_suppress = cfg.get('suppress_button', True)
        suppress_btn = btn if should_suppress else None
        try:
            from samsara.mouse_hook import MouseHook
            self._mouse_hook = MouseHook(
                on_button_event=self._on_command_button,
                suppress_button=suppress_btn,
            )
            self._mouse_hook.start()
            print(f"[CMD MODE] Mouse hook started (suppress={suppress_btn})")
        except Exception as e:
            print(f"[CMD MODE] Mouse hook failed to start: {e}")
            self._mouse_hook = None

    def _on_command_button(self, button_name, pressed):
        """Mouse hook callback — routes the configured button to command mode."""
        cfg = self.config.get('command_mode', {})
        if not cfg.get('enabled', False):
            return
        if button_name != cfg.get('button', 'mouse4'):
            return
        mode = cfg.get('mode', 'hold')
        if mode == 'hold':
            if pressed:
                self.enter_command_mode()
            else:
                self.exit_command_mode()
        else:  # toggle
            if pressed:
                if self.command_mode_active:
                    self.exit_command_mode()
                else:
                    self.enter_command_mode()

    def _check_command_mode_key(self, key, pressed: bool) -> None:
        """Route keyboard events to the command mode state machine.

        Called from on_key_press / on_key_release for every key event.
        No-ops unless command_mode.button is a keyboard source.
        """
        cfg = self.config.get('command_mode', {})
        cmd_enabled = cfg.get('enabled', False)
        btn_name = cfg.get('button', 'mouse4')
        is_mouse = btn_name in ('mouse4', 'mouse5')

        # Command mode keyboard handling (skip if disabled or mouse source)
        if cmd_enabled and not is_mouse:
            target = _get_pynput_command_key(btn_name)
            if _matches_pynput_key(key, target):
                mode = cfg.get('mode', 'hold')
                # Edge-trigger: collapse OS key auto-repeat (and any phantom
                # press/release pairs from the LL hook) to a single rising edge
                # on press and a single falling edge on release.  Without this,
                # a held key fires enter/exit ~30x/sec → earcon chirp storm.
                if pressed:
                    if self._command_mode_key_held:
                        return  # auto-repeat — already handled the real press
                    self._command_mode_key_held = True
                else:
                    if not self._command_mode_key_held:
                        return  # phantom release with no matching real press
                    self._command_mode_key_held = False
                if mode == 'hold':
                    if pressed:
                        self.enter_command_mode()
                    else:
                        self.exit_command_mode()
                else:  # toggle
                    if pressed:
                        if self.command_mode_active:
                            self.exit_command_mode()
                        else:
                            self.enter_command_mode()
                return

        # Right Alt → Ava mode (mutual exclusion with command mode)
        if not self.config.get('ava_mode_enabled', True):
            return
        ava_key_name = self.config.get('ava_mode_key', 'right_alt')
        ava_target = _get_pynput_command_key(ava_key_name)
        if ava_target is not None and _matches_pynput_key(key, ava_target):
            if pressed:
                if self._ava_mode_key_held:
                    return  # auto-repeat
                self._ava_mode_key_held = True
            else:
                if not self._ava_mode_key_held:
                    return  # phantom release
                self._ava_mode_key_held = False
            if pressed and not self.ava_mode_active and not self.command_mode_active:
                self.enter_ava_mode()
            elif not pressed and self.ava_mode_active:
                self.exit_ava_mode()
            return

        # AI command mode (toggle; mutual exclusion with command mode and ava mode)
        ai_cfg = self.config.get('ai_command_mode', {})
        if ai_cfg.get('enabled', True):
            ai_key_name = ai_cfg.get('key', 'right_ctrl')
            ai_target = _get_pynput_command_key(ai_key_name)
            if ai_target is not None and _matches_pynput_key(key, ai_target):
                if pressed:
                    if self._ai_cmd_key_held:
                        return  # auto-repeat
                    self._ai_cmd_key_held = True
                else:
                    if not self._ai_cmd_key_held:
                        return  # phantom release
                    self._ai_cmd_key_held = False
                if pressed:
                    if self.ai_command_mode_active:
                        self.exit_ai_command_mode()
                    else:
                        self.enter_ai_command_mode()

    def enter_command_mode(self):
        """Enter command mode (idempotent). Safe to call from any thread."""
        with self._command_mode_lock:
            if self.command_mode_active or self.ava_mode_active:
                return
            self.command_mode_active = True
        self._command_mode_miss_count = 0
        self._command_mode_session_start = time.monotonic()
        self._command_mode_ghost_tap = False
        print("[CMD MODE] Entering command mode")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, True)
        cfg = self.config.get('command_mode', {})
        if cfg.get('mode', 'hold') == 'toggle':
            timeout_s = cfg.get('inactivity_timeout_s', 30)
            self._reset_command_mode_inactivity_timer(timeout_s)
        threading.Thread(target=self._do_enter_command_mode, daemon=True,
                         name='cmd-mode-enter').start()

    def _do_enter_command_mode(self):
        """Worker thread: arms command mode and fires debounced earcon."""
        cfg = self.config.get('command_mode', {})
        debounce_ms = cfg.get('enter_debounce_ms', 200)
        if cfg.get('mode', 'hold') == 'toggle':
            # Toggle (sustained) mode: per-utterance dispatch via WakeConsumer VAD.
            # Do NOT call start_recording() — that activates the dictation consumer
            # (accumulate-until-release) and sets _hotkey_recording=True, which
            # suppresses the WakeConsumer.  The WakeConsumer is already running;
            # _is_toggle_cmd() makes it service frames while command_mode_active.
            time.sleep(debounce_ms / 1000.0)
            if self.command_mode_active:
                self.play_sound('start', use_winsound=True)
            return
        # Hold mode: accumulate audio via dictation consumer until key release.
        if self.recording:
            return
        self.command_mode_recording = True
        self.start_recording(streaming=False, play_earcon=False)
        # 200ms debounce: skip earcon for accidental quick taps
        time.sleep(debounce_ms / 1000.0)
        if self.command_mode_active:
            self.play_sound('start', use_winsound=True)

    def exit_command_mode(self):
        """Exit command mode (idempotent). Safe to call from any thread."""
        with self._command_mode_lock:
            if not self.command_mode_active:
                return
            self.command_mode_active = False
            hold_ms = (time.monotonic() - self._command_mode_session_start) * 1000
        debounce_ms = self.config.get('command_mode', {}).get('enter_debounce_ms', 200)
        # Taps shorter than the debounce window are ghost taps — mark so
        # transcribe() can discard the audio without executing commands.
        self._command_mode_ghost_tap = (hold_ms < debounce_ms)
        if self._command_mode_ghost_tap:
            print(f"[CMD MODE] Ghost tap ({hold_ms:.0f}ms < {debounce_ms}ms) — audio will be discarded")
        print("[CMD MODE] Exiting command mode")
        self._cancel_command_mode_inactivity_timer()
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, False)
        was_recording = self.recording
        if was_recording:
            self.stop_recording()  # stop_recording() already plays "stop" as acknowledgment
        cfg = self.config.get('command_mode', {})
        if cfg.get('exit_earcon', True) and not was_recording:
            # Only play here when not going through stop_recording() to avoid doubling
            self.play_sound('stop')

    # ── Ava mode (Right Alt hold-to-talk → Ollama) ───────────────────────────

    def enter_ava_mode(self):
        """Enter Ava mode (idempotent). Safe to call from any thread."""
        with self._ava_mode_lock:
            if self.ava_mode_active or self.command_mode_active:
                return
            self.ava_mode_active = True
        self._ava_mode_session_start = time.monotonic()
        self._ava_mode_ghost_tap = False
        print("[AVA MODE] Entering Ava mode")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, True)
        threading.Thread(target=self._do_enter_ava_mode, daemon=True,
                         name='ava-mode-enter').start()

    def _do_enter_ava_mode(self):
        """Worker thread: starts recording and fires debounced earcon."""
        if self.recording:
            return
        self.ava_mode_recording = True
        self.start_recording(streaming=False, play_earcon=False)
        debounce_ms = self.config.get('command_mode', {}).get('enter_debounce_ms', 200)
        time.sleep(debounce_ms / 1000.0)
        if self.ava_mode_active:
            self.play_sound('start', use_winsound=True)

    def exit_ava_mode(self):
        """Exit Ava mode (idempotent). Safe to call from any thread."""
        with self._ava_mode_lock:
            if not self.ava_mode_active:
                return
            self.ava_mode_active = False
            hold_ms = (time.monotonic() - self._ava_mode_session_start) * 1000
        debounce_ms = self.config.get('command_mode', {}).get('enter_debounce_ms', 200)
        self._ava_mode_ghost_tap = (hold_ms < debounce_ms)
        if self._ava_mode_ghost_tap:
            print(f"[AVA MODE] Ghost tap ({hold_ms:.0f}ms < {debounce_ms}ms) — audio will be discarded")
        print("[AVA MODE] Exiting Ava mode")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, False)
        was_recording = self.recording
        if was_recording:
            self.stop_recording()
        elif not self._ava_mode_ghost_tap:
            self.play_sound('stop')

    # ------------------------------------------------------------------
    # AI command mode
    # ------------------------------------------------------------------

    def enter_ai_command_mode(self):
        """Enter AI command mode (idempotent, toggle). Safe from any thread."""
        with self._ai_cmd_mode_lock:
            if self.ai_command_mode_active:
                return
            if self.command_mode_active or self.ava_mode_active:
                return
            self.ai_command_mode_active = True
        self._ai_cmd_ready.clear()  # Mic gate: unblocks only after cue finishes
        print("[AI-CMD] Entering AI command mode")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, True)
        threading.Thread(target=self._do_enter_ai_command_mode, daemon=True,
                         name='ai-cmd-enter').start()

    def _do_enter_ai_command_mode(self):
        """Worker: play entry earcon, warm up model, play ready cue, then arm mic."""
        debounce_ms = self.config.get('command_mode', {}).get('enter_debounce_ms', 200)
        time.sleep(debounce_ms / 1000.0)
        if not self.ai_command_mode_active:
            self._ai_cmd_ready.set()
            return
        self.play_sound('start', use_winsound=True)
        ai_cfg = self.config.get('ai_command_mode', {})

        def _on_ready():
            from samsara.ai_command_mode import _play_ready_cue  # noqa: PLC0415
            _play_ready_cue(self)
            self._ai_cmd_ready.set()

        if ai_cfg.get('keep_warm', True):
            try:
                from samsara.ai_command_mode import warm_up  # noqa: PLC0415
                warm_up(self, on_done=_on_ready)
            except Exception:
                self._ai_cmd_ready.set()
        else:
            _on_ready()

    def exit_ai_command_mode(self):
        """Exit AI command mode (idempotent). Drains queue. Safe from any thread."""
        with self._ai_cmd_mode_lock:
            if not self.ai_command_mode_active:
                return
            self.ai_command_mode_active = False
        self._ai_cmd_ready.set()  # Unblock utterance gate if cue is still playing
        print("[AI-CMD] Exiting AI command mode")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, False)
        try:
            from samsara.ai_command_mode import cancel_queue, reset_cancel  # noqa: PLC0415
            cancel_queue()
            reset_cancel()
        except Exception:
            pass
        self.play_sound('stop')

    def _handle_ai_command_utterance(self, buffer: list, src_rate: int) -> None:
        """Transcribe one VAD-gated utterance and push to the AI command queue.

        Called from WakeConsumer._flush() while ai_command_mode_active.
        Shares _wake_transcription_in_progress with _handle_command_mode_utterance
        to prevent concurrent transcriptions.
        Stop-words are checked before enqueue so cancel is always responsive.
        """
        if not self._ai_cmd_ready.wait(timeout=60):
            print('[AI-CMD-UTT] Ready timeout -- dropping utterance')
            return
        if self._wake_transcription_in_progress:
            print('[AI-CMD-UTT] Transcription in progress -- skipping')
            return
        self._wake_transcription_in_progress = True
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, src_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate
            if audio_duration < 0.3:
                return
            print(f'[AI-CMD-UTT] Transcribing {audio_duration:.1f}s')
            transcribe_params = self.get_transcription_params()
            transcribe_params['vad_filter'] = False
            with self.model_lock:
                segments, _ = self.model.transcribe(audio, **transcribe_params)
            text = ''.join(s.text for s in segments).strip()
            text = self.voice_training_window.apply_corrections(text)
            if not text:
                return
            print(f'[AI-CMD-UTT] "{text}"')
            text_lower = text.lower().strip()
            # Stop-word gate: cancel before touching the queue
            from samsara.ai_command_mode import (  # noqa: PLC0415
                _STOP_WORDS, cancel_queue, reset_cancel,
            )
            if text_lower in _STOP_WORDS:
                cancel_queue()
                reset_cancel()
                return
            from samsara.ai_command_mode import enqueue_utterance  # noqa: PLC0415
            enqueue_utterance(self, text)
        except Exception as exc:
            print(f'[AI-CMD-UTT] Error: {exc}')
            import traceback  # noqa: PLC0415
            traceback.print_exc()
        finally:
            self._wake_transcription_in_progress = False
            self._vad_reset()

    def _route_to_ava(self, text: str):
        """Send transcribed speech to Ollama via the ask_ollama plugin."""
        def _worker():
            try:
                from plugins.commands.ask_ollama import handle_ask_ava
                handle_ask_ava(self, remainder=text)
            except ImportError:
                if hasattr(self, 'audio_coordinator') and self.audio_coordinator:
                    self.audio_coordinator.speak(
                        "Ollama plugin is not installed.",
                        category="error",
                    )
                else:
                    print(f"[AVA] Ollama plugin not found. User said: {text}")
            except Exception as e:
                print(f"[AVA] Error: {e}")
                if hasattr(self, 'audio_coordinator') and self.audio_coordinator:
                    self.audio_coordinator.speak(
                        "Sorry, I had an error processing that.",
                        category="error",
                    )
            finally:
                # Tutorial Ava hook — fires after Ava finishes (success or error)
                _tut_ava = self._tutorial_hooks.pop('ava', None)
                if _tut_ava:
                    self._schedule_ui(_tut_ava)
        threading.Thread(target=_worker, daemon=True, name="Ava-worker").start()

    def _reset_command_mode_inactivity_timer(self, timeout_s):
        self._cancel_command_mode_inactivity_timer()
        t = threading.Timer(timeout_s, self._on_command_mode_inactivity)
        t.daemon = True
        self._command_mode_inactivity_timer = t
        t.start()

    def _cancel_command_mode_inactivity_timer(self):
        t = self._command_mode_inactivity_timer
        if t is not None:
            t.cancel()
            self._command_mode_inactivity_timer = None

    def _on_command_mode_inactivity(self):
        print("[CMD MODE] Inactivity timeout — exiting command mode")
        self.exit_command_mode()

    def _rearm_command_recording(self):
        """Re-start recording for the next command in hold mode.

        Toggle mode: WakeConsumer re-arms automatically on each utterance-end
        silence boundary, so this is a no-op there.
        """
        if self.config.get('command_mode', {}).get('mode', 'hold') == 'toggle':
            return
        time.sleep(0.1)
        if self.command_mode_active and not self.recording:
            self.command_mode_recording = True
            self.start_recording(streaming=False, play_earcon=False)
    def _handle_command_mode_utterance(self, buffer: list, src_rate: int) -> None:
        """Transcribe and execute one VAD-gated utterance in toggle command mode.

        Called from WakeConsumer._flush() for each silence-bounded utterance
        while command_mode_active and mode=='toggle'.  The WakeConsumer resets
        its utterance buffer after calling _flush(), so it re-arms automatically
        for the next utterance — no explicit re-arm is needed here.

        Two timeouts are in play (do not conflate):
          utterance_silence_s  (~1 s, WakeConsumer)  -- ends THIS command
          inactivity_timeout_s (30 s, threading.Timer) -- ends the whole session
        """
        if self._wake_transcription_in_progress:
            print('[CMD-UTT] Transcription already in progress — skipping utterance')
            return
        self._wake_transcription_in_progress = True
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, src_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate

            if audio_duration < 0.3:
                return

            print(f'[CMD-UTT] Transcribing {audio_duration:.1f}s utterance')

            transcribe_params = self.get_transcription_params()
            transcribe_params['vad_filter'] = False

            with self.model_lock:
                segments, _ = self.model.transcribe(audio, **transcribe_params)
            text = ''.join(s.text for s in segments).strip()
            text = self.voice_training_window.apply_corrections(text)

            if not text:
                print('[CMD-UTT] Empty transcription')
                return

            print(f'[CMD-UTT] "{text}"')

            if self._command_mode_ghost_tap:
                self._command_mode_ghost_tap = False
                print('[CMD-UTT] Ghost tap — discarding')
                return

            result, was_command = self.command_executor.process_text(text, self)

            if was_command:
                _store_cmd = self.command_executor.commands.get(result) or {'type': 'plugin'}
                if (result
                        and not _is_repeat_blacklisted(result, _store_cmd)
                        and self.command_executor.find_command(result) == result):
                    self._last_command = _store_cmd
                    self._last_command_name = result
                if result:
                    increment_command_count(result)
                self.add_to_history(text, is_command=True)
                self._log_history(
                    raw_text=text,
                    duration_ms=int(audio_duration * 1000),
                    mode='command',
                    status='success',
                    entry_type='command',
                    matched_command=str(result) if result else None,
                )
                if self.command_mode_active:
                    self._command_mode_miss_count = 0
                    cm_cfg = self.config.get('command_mode', {})
                    timeout_s = cm_cfg.get('inactivity_timeout_s', 30)
                    self._reset_command_mode_inactivity_timer(timeout_s)
            else:
                print(f'[CMD] No command matched: "{text}"')
                if self.command_mode_active:
                    self._command_mode_miss_count += 1
                    cm_cfg = self.config.get('command_mode', {})
                    miss_limit = cm_cfg.get('miss_limit', 5)
                    if self._command_mode_miss_count >= miss_limit:
                        print(f'[CMD MODE] Miss limit ({miss_limit}) reached')
                        self.exit_command_mode()
        except Exception as exc:
            print(f'[CMD-UTT] Error: {exc}')
            import traceback
            traceback.print_exc()
        finally:
            self._wake_transcription_in_progress = False
            self._vad_reset()

    # -----------------------------------------------------------------------

    def toggle_continuous_mode(self):
        """Toggle continuous listening mode"""
        if self.continuous_active:
            self.stop_continuous_mode()
        else:
            self.start_continuous_mode()
    
    def start_continuous_mode(self):
        """Start continuous listening with auto-transcribe on silence."""
        if not self.model_loaded:
            if self.loading_model:
                print("Model still loading, please wait...")
            return

        self.play_sound("start", use_winsound=True)
        time.sleep(0.15)
        print("[MIC] Continuous mode ACTIVE — speak naturally, pauses will trigger transcription")

        # ACE path: ring consumer handles capture — no separate PortAudio stream.
        # Works alongside wake word mode without stream conflict.
        self._continuous_consumer.start()

        self.set_app_state(continuous_active=True)
        self._request_icon_chase('continuous')
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, True)

    def stop_continuous_mode(self):
        """Stop continuous listening mode."""
        self.set_app_state(continuous_active=False)

        if self._continuous_consumer is not None and self._continuous_consumer._running:
            # ACE path: stop consumer, transcribe remaining frames
            remaining = self._continuous_consumer.stop()
            if remaining:
                self.transcribe_continuous_buffer(remaining, src_rate=16000)

        print("[OFF] Continuous mode STOPPED")
        self.play_sound("stop")
        self._release_icon_chase('continuous')
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)

    def transcribe_continuous_buffer(self, buffer, src_rate=None):
        """Transcribe a buffer from continuous mode.

        src_rate: sample rate of the audio in buffer. Defaults to
        self.capture_rate for backward compatibility. Pass SAMPLE_RATE
        (16000) when buffer comes from the ACE ring (already at model rate).
        """
        if src_rate is None:
            src_rate = self.capture_rate
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, src_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate
            
            # Get transcription parameters based on performance mode
            transcribe_params = self.get_transcription_params()
            # DISABLE faster-whisper's VAD for hold-to-dictate. The user
            # explicitly pressed the hotkey — all captured audio is intentional
            # speech. VAD was stripping 80% of audio, causing garbled output.
            transcribe_params['vad_filter'] = False
            perf_mode = self.config.get('performance_mode', 'balanced')
            
            # Guard: Whisper hallucinates on very short audio (<0.5s).
            # It outputs phantom phrases like "Thank you" or "Subtitles by Amara".
            if audio_duration < 0.51:
                print(f"[SKIP] Audio too short ({audio_duration:.2f}s) — skipping transcription")
                return
            
            transcribe_start = time.time()
            with self.model_lock:
                segments, info = self.model.transcribe(audio, **transcribe_params)
            
            text = "".join([segment.text for segment in segments]).strip()
            transcribe_time = time.time() - transcribe_start
            
            # Performance logging
            rtf = transcribe_time / audio_duration if audio_duration > 0 else 0
            device_info = getattr(self, 'device_type', 'unknown')
            print(f"[PERF] Audio: {audio_duration:.1f}s | Transcribe: {transcribe_time*1000:.0f}ms | "
                  f"RTF: {rtf:.2f}x | Mode: {perf_mode} | Device: {device_info}")
            
            # Apply corrections dictionary
            text = self.voice_training_window.apply_corrections(text)
            
            if text:
                # Check for command mode toggle OR regular commands
                result, was_command = self.command_executor.process_text(text, self)

                if was_command:
                    _store_cmd = self.command_executor.commands.get(result) or {'type': 'plugin'}
                    if (result and not _is_repeat_blacklisted(result, _store_cmd)
                            and self.command_executor.find_command(result) == result):
                        self._last_command = _store_cmd
                        self._last_command_name = result
                    if result:
                        increment_command_count(result)
                        if hasattr(self, 'hints'):
                            n = self.hints.increment('command_count')
                            if n == 1:
                                self.hints.maybe_show(
                                    'first_command_success',
                                    "Voice command executed. Say 'what can I say?' to"
                                    " browse all available commands.",
                                    delay_s=1.5,
                                )
                            elif n == 5 and self.hints.get_counter('show_numbers_used') == 0:
                                self.hints.maybe_show(
                                    'show_numbers_intro',
                                    "Tip: say 'show numbers' to click anything on screen"
                                    " by voice -- no mouse needed.",
                                    delay_s=2.0,
                                )
                    # Tutorial command hook — one-shot, fires for ANY command
                    _tut_cmd = self._tutorial_hooks.pop('command', None)
                    if _tut_cmd:
                        try:
                            _tut_cmd(result or "")
                        except Exception:
                            pass
                    # Command was executed
                    return

                # Tutorial dictation hook — one-shot, removed after first fire
                _tut_dict = self._tutorial_hooks.pop('dictation', None)
                if _tut_dict:
                    try:
                        _tut_dict(text)
                    except Exception:
                        pass

                # Not a command, proceed with dictation
                # Apply text processing (auto-capitalize, number formatting)
                text = self.process_transcription(text)

                # Deterministic cleanup (filler removal, spacing). Snapshot
                # raw BEFORE cleanup so history can preserve the original.
                raw = text
                _cmode = 'verbatim' if getattr(self, '_skip_cleanup', False) else self.config.get('cleanup_mode', 'clean')
                text = clean_text(text, mode=_cmode)

                if self.config['add_trailing_space']:
                    text = text + " "

                print(f"[TEXT] {text}")

                if self.config['auto_paste']:
                    self._paste_preserving_clipboard(text)

                # Log to persistent history
                self._log_history(
                    raw_text=raw,
                    display_text=text.strip(),
                    duration_ms=int(audio_duration * 1000),
                    mode="continuous",
                    status="success",
                    entry_type="dictation",
                )
                self._notify_main_window(text.strip())

        except Exception as e:
            print(f"[ERROR] Transcription failed: {e}")
            self._log_history(
                raw_text="",
                display_text=f"[FAILED] {e}",
                mode="continuous",
                status="failed",
                entry_type="failed",
            )
            # Notify user so they know to retry
            try:
                import winsound
                winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
            except Exception:
                pass

    def toggle_wake_word_mode(self):
        """Toggle wake word listening mode"""
        if self.wake_word_active:
            self.stop_wake_word_mode()
        else:
            self.start_wake_word_mode()
    
    def start_wake_word_mode(self):
        """Start wake word listening — always listening for wake word."""
        if not self.model_loaded:
            if self.loading_model:
                print("Model still loading, please wait...")
            return

        self.play_sound("start", use_winsound=True)
        time.sleep(0.15)
        phrase = self.config.get('wake_word_config', {}).get('phrase', 'hey samsara')
        print(f"[LISTEN] Wake word mode ACTIVE - say '{phrase}' to give commands")

        self.silence_start       = None
        self.is_speaking         = False
        self.wake_word_triggered = False

        # ACE path: WakeConsumer polls the ring — no separate PortAudio stream.
        # Engine and wake consumer share the same device, no stream conflict.
        self._wake_consumer.start()

        self.set_app_state(wake_word_active=True)
        self._request_icon_chase('wake_word')
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, True)

        if hasattr(self, 'hints'):
            self.hints.maybe_show(
                'wake_mode_activated',
                "Wake word active. Say 'Jarvis' followed by a command."
                " Try 'Jarvis, show numbers' to click by voice.",
                delay_s=2.0,
            )

    def stop_wake_word_mode(self):
        """Stop wake word listening mode."""
        self.set_app_state(wake_word_active=False)
        self.wake_word_triggered = False
        self._reset_wake_dictation()

        if self._wake_consumer is not None and self._wake_consumer._running:
            # ACE path: stop consumer, transcribe remaining frames if triggered
            remaining = self._wake_consumer.stop()
            if remaining and self.wake_word_triggered:
                self.process_wake_word_buffer(remaining, src_rate=16000)

        print("[OFF] Wake word mode STOPPED")
        self.play_sound("stop")
        self._release_icon_chase('wake_word')
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)

    def _load_vad_model(self):
        """Load Silero VAD for real-time speech detection in the wake callback.

        Runs once after Whisper loads. Any failure (no torch, offline, hub
        cache miss) sets _vad_available=False and the callback falls back to
        RMS. Safe to call multiple times -- a successful prior load short-
        circuits.
        """
        if self._vad_available and self._vad_model is not None:
            return
        if not _TORCH_AVAILABLE:
            print("[VAD] torch unavailable, falling back to RMS speech detection")
            return
        try:
            logger.info("[BOOT-DIAG] torch.hub.load (Silero VAD) called — may contact GitHub if cache stale")
            _t = time.perf_counter()
            model, _ = torch.hub.load(
                'snakers4/silero-vad', 'silero_vad',
                force_reload=False, trust_repo=True,
            )
            _dt = (time.perf_counter() - _t) * 1000
            logger.info(f"[BOOT-DIAG] torch.hub.load (Silero VAD) returned: {_dt:.0f}ms")
            if _dt > 5000:
                logger.info(f"[BOOT-DIAG] SLOW STEP: torch.hub.load (Silero VAD) {_dt:.0f}ms")
            model.eval()
            self._vad_model = model
            self._vad_available = True
            self._vad_lock = threading.Lock()
            print("[VAD] Silero VAD loaded for real-time speech detection")
        except Exception as e:
            self._vad_model = None
            self._vad_available = False
            print(f"[VAD] Silero VAD not available, falling back to RMS: {e}")

    def _load_oww_model(self):
        """Load the OpenWakeWord model for the configured wake phrase.

        Called once after Whisper loads. Any failure (package not installed,
        no model for the phrase) leaves _wake_detector.is_available == False
        and the audio callback falls back to Whisper-based detection.
        Safe to call multiple times -- already-loaded detector short-circuits.
        """
        if self._wake_detector is not None and self._wake_detector.is_available:
            return
        ww_cfg = self.config.get('wake_word_config', {})
        wake_phrase = ww_cfg.get('phrase', 'jarvis')
        oww_threshold = float(ww_cfg.get('oww_threshold', 0.2))
        self._wake_detector = WakeWordDetector(wake_phrase, threshold=oww_threshold)
        if self._wake_detector.is_available:
            print(f"[OWW] Wake word pre-filter active for '{wake_phrase}'")
        else:
            print(f"[OWW] No pre-filter for '{wake_phrase}' — using Whisper detection")

    def _load_wake_target_models(self):
        """Load OWW models for all enabled wake_targets (Phase 1 multi-wakeword).

        Looks for custom .onnx files under samsara/wake_models/. When a model
        file is absent that target uses Whisper-transcript matching via
        match_wake_phrase — adequate for long phrases like "hey claude" /
        "activate hermes". Drop trained .onnx files there and restart to activate
        the OWW pre-filter for those targets.
        """
        targets = self.config.get('wake_targets', [])
        if not targets:
            return

        models_dir = Path(__file__).parent / 'samsara' / 'wake_models'
        oww_threshold = float(self.config.get('wake_word_config', {}).get('oww_threshold', 0.2))

        for target in targets:
            if not target.get('enabled', True):
                continue
            tid        = target.get('id', '')
            phrase     = target.get('phrase', '')
            model_file = target.get('oww_model', '')
            model_path = (models_dir / model_file) if model_file else None

            if model_path and model_path.exists():
                detector = WakeWordDetector(phrase, threshold=oww_threshold,
                                            model_path=str(model_path))
                self._wake_target_detectors[tid] = detector
                status = "OWW pre-filter active" if detector.is_available else "load failed — Whisper fallback"
                print(f"[OWW] Wake target '{tid}' ({phrase}): {status}")
            else:
                self._wake_target_detectors[tid] = None
                missing = f" ('{model_file}' not in wake_models/)" if model_file else ""
                print(f"[OWW] Wake target '{tid}' ({phrase}): no model{missing} — Whisper fallback")

    def _check_wake_targets(self, corrected_lower):
        """Match corrected transcript against all enabled wake_targets.

        Returns the first matching target dict, or None if no target matched.
        Called from process_wake_word_buffer before the legacy single-phrase check.
        """
        for target in self.config.get('wake_targets', []):
            if not target.get('enabled', True):
                continue
            phrase = target.get('phrase', '').lower().strip()
            if not phrase:
                continue
            matched, _, _ = match_wake_phrase(corrected_lower, phrase)
            if matched:
                return target
        return None

    def _dispatch_wake_target(self, target, corrected_lower=''):
        """Focus the target window and start a quick_dictation session.

        Called from process_wake_word_buffer when a wake_target phrase is
        detected. Focuses (and restores if minimized) the target window via
        window_switcher._force_focus, then enters quick_dictation mode so
        the user's next utterance is typed into that window. Session ends via
        the existing silence/timeout mechanism (Phase 2 will refine this).
        """
        process_name = target.get('target_process', '')
        phrase       = target.get('phrase', '').lower().strip()
        tid          = target.get('id', phrase)

        print(f"[WAKE-TARGET] '{phrase}' matched — targeting '{process_name}'")

        own_pid = os.getpid()
        result  = _resolve_target_window(process_name, exclude_pids={own_pid})

        if result is None:
            print(f"[WAKE-TARGET] No window found for '{process_name}' — process not running?")
            self.play_sound("error")
            return

        hwnd, title = result
        print(f"[WAKE-TARGET] Found window: '{title}' (hwnd={hwnd})")

        try:
            from plugins.commands import window_switcher as _ws
            focused = _ws._force_focus(hwnd)
            if focused:
                logger.info("[WAKE-TARGET] Focused %r", title)
            else:
                import ctypes as _ct
                _fg = _ct.windll.user32.GetForegroundWindow()
                _fgl = _ct.windll.user32.GetWindowTextLengthW(_fg)
                _fgb = _ct.create_unicode_buffer(_fgl + 1)
                _ct.windll.user32.GetWindowTextW(_fg, _fgb, _fgl + 1)
                logger.warning(
                    "[WAKE-TARGET] FOCUS FAILED for %r (foreground still %r) — proceeding to dictate anyway",
                    title, _fgb.value or "<unknown>",
                )
        except Exception as exc:
            print(f"[WAKE-TARGET] Focus failed: {exc}")
            self.play_sound("error")
            return

        # Extract any trailing speech spoken after the wake phrase in this same
        # utterance (e.g. "hey claude write a summary" -> "write a summary").
        initial_content = None
        if corrected_lower and phrase in corrected_lower:
            _, _, match_index = match_wake_phrase(corrected_lower, phrase)
            if match_index >= 0:
                remainder = corrected_lower[match_index + len(phrase):].strip()
                remainder = re.sub(r'^[^\w]+', '', remainder).strip()
                if remainder:
                    initial_content = remainder
                    print(f"[WAKE-TARGET] Pre-buffering trailing speech: '{initial_content}'")

        # Resolve per-target send policy.  Explicit key in config wins; otherwise
        # default by process/id name: hermes-targeted sessions stage only (never
        # auto-submit), all other targets press Enter on send-word detection.
        if 'send_policy' in target:
            _send_policy = target['send_policy']
        elif 'hermes' in process_name.lower() or 'hermes' in tid.lower():
            _send_policy = 'stage_only'
        else:
            _send_policy = 'enter'

        # Start open-ended wake session: per-utterance delivery, ends on inactivity.
        self._start_wake_session(initial_content=initial_content, send_policy=_send_policy)

    def _start_wake_session(self, initial_content=None, send_policy='enter'):
        """Enter open-ended wake session state.

        Each transcribed utterance is delivered immediately.  The session stays
        alive through silence and only ends after _WAKE_SESSION_TIMEOUT_S of
        inactivity or via the global cancel path.

        send_policy: 'enter' — press Enter after send-word detection (claude targets).
                     'stage_only' — text staged, Enter suppressed (hermes/agentic targets).
        """
        old_state = self.app_state
        self.app_state = 'wake_session'
        logger.info(f"[WS-DIAG] app_state set to {self.app_state!r}")
        self.wake_dictation_mode = 'wake_session'
        self.wake_dictation_buffer = []
        self.wake_dictation_start_time = time.time()
        self.wake_word_triggered = False
        self._dictation_paused = False
        self._dictation_require_end = False
        self._dictation_silence_timeout = _WAKE_SESSION_CHUNK_GAP_S
        self._wake_target_active = True
        self._wake_session_first_chunk = True
        self._wake_session_send_policy = send_policy

        if hasattr(self, 'wake_word_timer') and self.wake_word_timer:
            self.wake_word_timer.cancel()

        print(f"[STATE] {old_state} -> wake_session "
              f"(chunk gap: {_WAKE_SESSION_CHUNK_GAP_S}s, "
              f"inactivity timeout: {_WAKE_SESSION_TIMEOUT_S}s, "
              f"send_policy: {send_policy})")

        self._restart_wake_session_timer()
        logger.info(
            f"[WS-DIAG] start_wake_session: app_state={self.app_state!r} "
            f"timer_id={id(getattr(self,'_wake_session_inactivity_timer',None))}"
        )
        self.play_sound("start")

        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_mode, "Wake Session")
            self._schedule_ui(self.listening_indicator.set_listening, True)

        if initial_content:
            self._output_dictation(initial_content)

    def _restart_wake_session_timer(self):
        """Reset the inactivity countdown; called after each delivered utterance."""
        existing = getattr(self, '_wake_session_inactivity_timer', None)
        logger.info(
            f"[WS-DIAG] restart_wake_session_timer CALLED: "
            f"app_state={self.app_state!r} had_existing={existing is not None} "
            f"existing_id={id(existing)}"
        )
        if existing is not None:
            existing.cancel()
        t = threading.Timer(_WAKE_SESSION_TIMEOUT_S, self._end_wake_session)
        t.daemon = True
        t.start()
        self._wake_session_inactivity_timer = t
        logger.info(
            f"[WS-DIAG] restart_wake_session_timer ARMED: "
            f"new timer_id={id(t)} for {_WAKE_SESSION_TIMEOUT_S}s"
        )

    def _end_wake_session(self):
        """End the wake session and re-arm wake detection (called by inactivity timer)."""
        import traceback as _tb
        logger.info(
            f"[WS-DIAG] end_wake_session ENTERED: app_state={self.app_state!r} "
            f"caller={_tb.extract_stack()[-2].name}"
        )
        existing = getattr(self, '_wake_session_inactivity_timer', None)
        if existing is not None:
            existing.cancel()
            self._wake_session_inactivity_timer = None
        print("[WAKE-SESSION] ended (inactivity timeout)")
        self._reset_wake_dictation()

    def _vad_is_speech(self, chunk_float32, src_rate=None):
        """Return True if the chunk contains human speech.

        chunk_float32: flattened mono audio at src_rate.
        src_rate: sample rate of chunk_float32 (default: self.capture_rate).
                  Pass SAMPLE_RATE (16kHz) when chunk comes from the ACE ring.
        """
        if not self._vad_available or self._vad_model is None:
            return False
        if src_rate is None:
            src_rate = self.capture_rate
        chunk_16k = resample_audio(chunk_float32, src_rate, 16000)
        # Guarantee 1D — sounddevice returns (N, 1) for mono in some configs
        if chunk_16k.ndim > 1:
            chunk_16k = chunk_16k.flatten()
        window_size = 512
        for start in range(0, len(chunk_16k) - window_size + 1, window_size):
            window = chunk_16k[start:start + window_size]
            # Force float32 and correct shape before model call
            tensor = torch.from_numpy(window).float().unsqueeze(0)
            if tensor.shape != (1, 512):
                continue
            with self._vad_lock:
                with torch.no_grad():
                    try:
                        speech_prob = self._vad_model(tensor, 16000).item()
                    except RuntimeError as e:
                        print(f"[VAD] State corruption caught, auto-resetting: {e}")
                        self._vad_model.reset_states()
                        continue
            if speech_prob > 0.5:
                return True
        return False

    def _vad_reset(self):
        """Clear Silero VAD internal state between utterances."""
        if self._vad_available and self._vad_model is not None:
            with self._vad_lock:
                try:
                    self._vad_model.reset_states()
                except Exception as e:
                    print(f"[VAD] reset_states failed: {e}")

    def register_wake_trace_callback(self, callback):
        """Register a callable that receives wake-word pipeline trace events.

        Called by WakeWordDebugWindow while it is open so the debug UI can
        visualize the MAIN app's wake word pipeline (not just its own parallel
        test pipeline). Callback signature: callback(event_dict). Runs on a
        background thread — the callback is responsible for marshalling onto
        its own UI thread.
        """
        self._wake_trace_callback = callback

    def unregister_wake_trace_callback(self):
        """Clear the wake-word trace callback."""
        self._wake_trace_callback = None

    def _emit_wake_trace(self, event):
        """Emit a structured trace event to the registered callback (no-op if none)."""
        cb = self._wake_trace_callback
        if cb is None:
            return
        try:
            cb(event)
        except Exception as e:
            # Never let a debug UI bug break the main pipeline
            print(f"[WARN] wake trace callback failed: {e}")

    def calibrate_wake_mic(self, seconds: float = 3.0) -> float | None:
        """Sample ambient audio for *seconds* and seed the adaptive noise floor.

        Uses the ACE engine ring (via a temporary registered consumer) so no
        second InputStream is opened.  If the ACE engine is not running (legacy
        path), falls back to the most recent frames already buffered in the ring
        (rewind) and averages whatever is available.

        Returns the measured floor RMS, or None if no frames were available.
        Persists the result to wake_word_config.audio.measured_noise_floor so
        the floor survives a restart and seeds the EMA on next boot.
        """
        import logging as _log
        from samsara.audio_engine.frame import FRAME_MS as _FMS

        engine = getattr(self, '_ace_engine', None)
        if engine is None or not engine._running:
            _log.getLogger().warning("[CAL] ACE engine not running — calibrate_wake_mic has no audio source")
            return None

        frames_needed = int(seconds * 1000 / _FMS)
        reader = engine.register_consumer("wake-calibration")
        # Rewind into existing ring history for an instant sample if enough
        # history exists; otherwise we read forward in real time.
        from samsara.audio_engine.ring import EMPTY as _EMPTY
        from samsara.audio_engine.frame import PREBUFFER_FRAMES as _PF
        rewind_n = min(frames_needed, _PF)
        reader.rewind(rewind_n)

        rms_values = []
        deadline = time.monotonic() + seconds

        while time.monotonic() < deadline:
            frame = reader.read_next()
            if frame is _EMPTY:
                time.sleep(0.005)
                continue
            chunk = frame.pcm.astype(np.float32) / 32767.0
            rms_values.append(float(np.sqrt(np.mean(chunk ** 2))))

        engine.unregister_consumer(reader)

        if not rms_values:
            _log.getLogger().warning("[CAL] calibrate_wake_mic: no frames collected")
            return None

        measured = float(np.median(rms_values))
        measured = max(measured, _NOISE_FLOOR_MIN)
        self._wake_noise_floor = measured
        _log.getLogger().info(f"[CAL] Wake mic calibrated: floor={measured:.5f} ({len(rms_values)} frames)")

        # Persist so the floor survives restart.
        with self._config_lock:
            self.config.setdefault('wake_word_config', {}).setdefault('audio', {})
            self.config['wake_word_config']['audio']['measured_noise_floor'] = measured
            self.save_config()

        return measured

    def process_wake_word_buffer(self, buffer, src_rate=None):
        """Process audio — check for wake word, commands, or dictation content.

        src_rate: sample rate of audio in buffer (default: self.capture_rate).
                  Pass SAMPLE_RATE (16kHz) when buffer comes from the ACE ring.
        """
        if src_rate is None:
            src_rate = self.capture_rate
        _set_in_progress = False
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, src_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate

            # DEBUG: dump raw audio before Whisper for onset-clipping diagnosis.
            # Enable with config key debug_dump_wake_audio: true
            # Listen to the WAV — if the first word is already missing, it's a
            # pipeline/prebuffer issue, not Whisper.
            if self.config.get('debug_dump_wake_audio', False):
                try:
                    import wave as _wave
                    _dump_dir = Path(os.path.expanduser("~")) / ".samsara" / "debug_audio"
                    _dump_dir.mkdir(parents=True, exist_ok=True)
                    _ts = datetime.now().strftime("%H%M%S_%f")
                    _dump_path = _dump_dir / f"wake_{_ts}.wav"
                    _int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
                    with _wave.open(str(_dump_path), 'w') as _wf:
                        _wf.setnchannels(1)
                        _wf.setsampwidth(2)
                        _wf.setframerate(self.model_rate)
                        _wf.writeframes(_int16.tobytes())
                    print(f"[DEBUG] Dumped wake audio -> {_dump_path} ({audio_duration:.2f}s)")
                except Exception as _de:
                    print(f"[DEBUG] Audio dump failed: {_de}")

            # FIX 1: RMS energy gate — skip Whisper on silent audio.
            # On CPU machines Whisper takes ~1s per call; calling it on every
            # chunk saturates the CPU. This gate rejects the buffer early when
            # the audio energy is not meaningfully above the ambient noise floor.
            ww_config = self.config.get('wake_word_config', {})
            audio_config = ww_config.get('audio', {})
            audio_rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))

            use_adaptive = audio_config.get('adaptive_gate', True)
            if use_adaptive:
                # Update rolling noise-floor estimate from ambient-only frames.
                if self._wake_noise_floor is None:
                    self._wake_noise_floor = max(audio_rms, _NOISE_FLOOR_MIN)
                elif audio_rms < self._wake_noise_floor * _NOISE_FLOOR_SPEECH_RATIO:
                    self._wake_noise_floor = max(
                        (1.0 - _NOISE_FLOOR_ALPHA) * self._wake_noise_floor
                        + _NOISE_FLOOR_ALPHA * audio_rms,
                        _NOISE_FLOOR_MIN,
                    )

                # Absolute backstop only — the legacy speech_threshold is NOT
                # used as a hard floor here, because a desktop-tuned value
                # (e.g. 0.0200) would clamp the adaptive gate upward and defeat
                # the whole point for low-gain mics. Adaptive mode trusts the
                # measured floor; _ABS_FLOOR_MIN just blocks pure-DC buffers.
                gate_level = max(self._wake_noise_floor * _SPEECH_FLOOR_RATIO, _ABS_FLOOR_MIN)

                if audio_rms < gate_level:
                    logging.debug(
                        f"[WAKE] gated (rms {audio_rms:.4f} < adaptive {gate_level:.4f}"
                        f" [floor {self._wake_noise_floor:.4f} x{_SPEECH_FLOOR_RATIO}]) -- skipping"
                    )
                    return
            else:
                # adaptive_gate=False: exact original fixed-threshold behaviour.
                speech_threshold = audio_config.get('speech_threshold', DEFAULT_SPEECH_THRESHOLD)
                if audio_rms < speech_threshold:
                    logging.debug(
                        f"[WAKE] Below speech threshold (RMS {audio_rms:.4f} < {speech_threshold:.4f}), skipping"
                    )
                    return

            # FIX 2: In-progress flag — prevent concurrent Whisper calls.
            # On CPU a transcription can take longer than the next buffer window,
            # causing a queue of overlapping calls. Drop the new buffer if a
            # transcription is already running; on CPU the stale audio is useless
            # anyway. _set_in_progress tracks whether THIS call set the flag so
            # the outer finally only clears it when we own it.
            if self._wake_transcription_in_progress:
                logging.debug("[WAKE] Transcription already in progress, skipping")
                return
            self._wake_transcription_in_progress = True
            _set_in_progress = True

            # Get transcription parameters based on performance mode
            transcribe_params = self.get_transcription_params()
            # When Silero is unavailable we relied on RMS to gate speech into
            # the buffer. Whisper's own vad_filter then strips quiet audio a
            # second time — on low-gain mics this removes everything, producing
            # empty transcription. Disable it so RMS-gated audio passes through.
            if not self._vad_available:
                transcribe_params['vad_filter'] = False
            perf_mode = self.config.get('performance_mode', 'balanced')

            # Restart wake-session inactivity timer at flush time so transcription
            # latency never races the 10s window.  _output_dictation also restarts
            # it on delivery; the double-reset per utterance is harmless.
            if self.app_state == 'wake_session':
                self._restart_wake_session_timer()

            transcribe_start = time.time()
            with self.model_lock:
                segments, info = self.model.transcribe(audio, **transcribe_params)

            text = "".join([segment.text for segment in segments]).strip()
            transcribe_time = time.time() - transcribe_start

            # Performance logging for wake word mode
            rtf = transcribe_time / audio_duration if audio_duration > 0 else 0
            device_info = getattr(self, 'device_type', 'unknown')
            print(f"[PERF/WAKE] Audio: {audio_duration:.1f}s | Transcribe: {transcribe_time*1000:.0f}ms | "
                  f"RTF: {rtf:.2f}x | Mode: {perf_mode} | Device: {device_info}")
            
            # Apply corrections dictionary
            text = self.voice_training_window.apply_corrections(text)
            text_lower = text.lower()
            
            if not text:
                print(f"[HEAR] (nothing — Whisper returned empty for {audio_duration:.1f}s of audio)")
                # Only log when the user actually spoke (>0.5s of audio) but
                # Whisper returned nothing. Don't spam history with every
                # silent buffer the wake-word callback flushes.
                if audio_duration > 0.5:
                    self._log_history(
                        raw_text="",
                        display_text="(no speech detected)",
                        duration_ms=int(audio_duration * 1000),
                        mode="wake",
                        status="empty",
                        entry_type="failed",
                    )
                return
            
            print(f"[HEAR] \"{text}\"")
            
            # Get wake word config
            ww_config = self.config.get('wake_word_config', {})
            wake_phrase = ww_config.get('phrase', 'samsara').lower()

            self._emit_wake_trace({"stage": "utterance_start", "raw": text, "normalized": text_lower})

            # In dictation state (quick_dictation, long_dictation, or wake_session)?
            if self.app_state in ('quick_dictation', 'long_dictation', 'wake_session'):
                # Check cancel words
                cancel_words = ww_config.get('cancel_words', ['cancel'])
                for cw in cancel_words:
                    if cw.lower() in text_lower:
                        print(f"[CANCEL] Dictation cancelled ('{cw}')")
                        self._emit_wake_trace({"stage": "cancel_word_detected", "phrase": cw})
                        self.play_sound("error")
                        if self.app_state == 'wake_session':
                            self._end_wake_session()
                        else:
                            self._reset_wake_dictation()
                        self._emit_wake_trace({"stage": "utterance_end", "result": "cancelled"})
                        return

                # wake_session: check for send terminator before immediate delivery.
                # Control words are end-of-utterance only: the LAST token of the
                # stabilized transcript (stripped of trailing punctuation) must be an
                # exact match. Mid-utterance occurrences ("come over here") pass through
                # as normal dictated text.
                if self.app_state == 'wake_session':
                    _send_words = ww_config.get('send_words', _WAKE_SESSION_SEND_WORDS)
                    _tokens = text.strip().split()
                    _matched_sw = None
                    if _tokens:
                        _last_tok = _tokens[-1].rstrip('.,!?').lower()
                        _matched_sw = next(
                            (_sw for _sw in _send_words if _sw.lower() == _last_tok),
                            None,
                        )
                    if _matched_sw is not None:
                        _pre = ' '.join(_tokens[:-1])
                        if _pre:
                            self._output_dictation(_pre)
                        _policy = getattr(self, '_wake_session_send_policy', 'enter')
                        if _policy == 'enter':
                            time.sleep(0.05)
                            pyautogui.press('return')
                            self.play_sound("success")
                            print(f"[WAKE-SESSION] sent — '{_matched_sw}' detected, Enter pressed")
                        else:
                            self.play_sound("action_complete")
                            print(f"[WAKE-SESSION] staged — '{_matched_sw}' detected, Enter suppressed (stage_only)")
                        self._emit_wake_trace({"stage": "utterance_end", "result": "wake_session_sent",
                                               "send_word": _matched_sw, "policy": _policy})
                        self._end_wake_session()
                        return
                    self._output_dictation(text.strip())
                    self._emit_wake_trace({"stage": "utterance_end", "result": "wake_session_delivered"})
                    return

                # Check end words (primarily long_dictation, but works in both).
                # Checked before pause/resume so "over" finalizes even while paused.
                end_words = ww_config.get('end_words', ['over', 'done'])
                for ew in end_words:
                    if ew.lower() in text_lower:
                        print(f"[END] End word detected: '{ew}'")
                        end_index = text_lower.rfind(ew.lower())
                        final_text = text[:end_index].strip()
                        if self.wake_dictation_buffer:
                            final_text = ' '.join(self.wake_dictation_buffer) + ' ' + final_text
                        self._emit_wake_trace({"stage": "end_word_detected", "phrase": ew,
                                               "buffered_text": ' '.join(self.wake_dictation_buffer),
                                               "final_output": final_text.strip()})
                        if final_text.strip():
                            self._output_dictation(final_text.strip())
                        self._reset_wake_dictation()
                        self._emit_wake_trace({"stage": "utterance_end", "result": "end_word"})
                        return

                # Pause/resume state machine (long_dictation only)
                if self.app_state == 'long_dictation':
                    if self._dictation_paused:
                        # Only resume words get through; everything else is ignored.
                        resume_words = ww_config.get('resume_words', ['resume', 'continue', 'go on'])
                        for rw in resume_words:
                            if rw.lower() in text_lower:
                                self._dictation_paused = False
                                self.play_sound("start")
                                if hasattr(self, 'listening_indicator'):
                                    self._schedule_ui(self.listening_indicator.set_mode, "Long Dictation")
                                    self._schedule_ui(self.listening_indicator.set_listening, True)
                                print(f"[RESUME] Dictation resumed ('{rw}')")
                                self._emit_wake_trace({"stage": "resume",
                                                       "buffer_size": len(self.wake_dictation_buffer)})
                                self._emit_wake_trace({"stage": "utterance_end", "result": "resumed"})
                                return
                        print(f"[PAUSED] Ignoring: '{text}'")
                        self._emit_wake_trace({"stage": "utterance_end",
                                               "result": "paused_ignored", "text": text})
                        return

                    pause_words = ww_config.get('pause_words', ['pause'])
                    for pw in pause_words:
                        if pw.lower() in text_lower:
                            # Preserve any content spoken before the pause word.
                            pause_idx = text_lower.find(pw.lower())
                            cleaned = (text[:pause_idx] + text[pause_idx + len(pw):]).strip()
                            if cleaned:
                                self.wake_dictation_buffer.append(cleaned)
                                print(f"[DICTATE] Buffered (pre-pause): {cleaned}")
                            self._dictation_paused = True
                            self.silence_start = None
                            self.play_sound("stop")
                            if hasattr(self, 'listening_indicator'):
                                self._schedule_ui(self.listening_indicator.set_mode, "Paused")
                                self._schedule_ui(self.listening_indicator.set_listening, False)
                            print(f"[PAUSE] Dictation paused ('{pw}')")
                            self._emit_wake_trace({"stage": "pause",
                                                   "buffer_size": len(self.wake_dictation_buffer)})
                            self._emit_wake_trace({"stage": "utterance_end", "result": "paused"})
                            return

                # Accumulate text
                self.wake_dictation_buffer.append(text)
                print(f"[DICTATE] Buffered: {text}")
                self._emit_wake_trace({"stage": "dictation_buffered", "text": text,
                                       "buffer_size": len(self.wake_dictation_buffer)})

                if not self._dictation_require_end:
                    self._restart_dictation_timer()
                self._emit_wake_trace({"stage": "utterance_end", "result": "buffered"})
                return
            
            # Not in dictation mode - check for wake word (token-aware match)
            # Apply correction map before matching so known Whisper
            # misrecognitions ("charvis" -> "jarvis" etc.) still trigger.
            corrected_lower = apply_wake_corrections(text_lower)
            correction_applied = was_corrected(text_lower, corrected_lower)
            if correction_applied:
                print(f"[CORRECT] '{text_lower}' -> '{corrected_lower}'")

            logger.info(
                "[WAKE-CHECK] transcript=%r targets=%r",
                corrected_lower,
                [t.get('phrase') for t in self.config.get('wake_targets', []) if t.get('enabled', True)],
            )

            # Phase 1: check multi-wake targets BEFORE the legacy single-phrase check.
            # Each enabled wake_target has a distinct phrase ("hey claude",
            # "activate hermes") that doesn't overlap with legacy jarvis phrases.
            _wake_target = self._check_wake_targets(corrected_lower)
            if _wake_target is not None:
                self._dispatch_wake_target(_wake_target, corrected_lower=corrected_lower)
                return

            matched, match_type, match_index = match_wake_phrase(corrected_lower, wake_phrase)

            self._emit_wake_trace({
                "stage": "wake_word_check", "input": text, "normalized": text_lower,
                "corrected": corrected_lower, "correction_applied": correction_applied,
                "wake_phrase": wake_phrase, "matched": matched,
                "match_type": match_type, "match_index": match_index,
            })

            if matched:
                print(f"[MIC] Wake word detected: '{wake_phrase}' ({match_type} @ {match_index})")
                self.wake_word_triggered = True
                self.play_sound("start")

                # Light up the indicator — pulse stays on through the command
                if hasattr(self, 'listening_indicator'):
                    self._schedule_ui(self.listening_indicator.set_mode, "Listening...")
                    self._schedule_ui(self.listening_indicator.set_listening, True)

                # Slice from corrected (match_index is a position in corrected_lower)
                command_text = corrected_lower[match_index + len(wake_phrase):].strip()
                # Whisper often inserts punctuation between wake word and command
                # ("jarvis, dictate" → ", dictate"). Strip any leading non-word chars.
                command_text = normalize_command_text(command_text)

                command_text, echo_count = strip_wake_echoes(command_text, wake_phrase)
                if echo_count:
                    command_text = normalize_command_text(command_text)
                    print(f"[ECHO] Stripped {echo_count} echo(es) of '{wake_phrase}' from command")
                    self._emit_wake_trace({"stage": "echo_strip", "removed": echo_count,
                                           "cleaned": command_text})

                # Phonetic wash: undo Whisper's known mis-transcriptions of
                # command phrases (fine->find, get hub->github, mike->mic, etc.)
                # BEFORE parse_wake_command and the matcher see the text.
                command_text = apply_phonetic_wash(command_text)

                self._emit_wake_trace({"stage": "command_extract",
                                       "from_index": match_index, "command": command_text,
                                       "remainder": ""})

                cleaned_cmd = re.sub(r'[^\w\s]', '', command_text).strip()
                has_meaningful_command = len(cleaned_cmd) >= 2

                if has_meaningful_command:
                    print(f"[TEXT] Command: {command_text}")
                    self._process_wake_command(command_text)
                else:
                    if command_text:
                        print(f"[SKIP] Ignoring noise after wake word: '{command_text}'")
                    print("[LISTEN] Listening for command...")
                    self._start_wake_timeout()

                self._emit_wake_trace({"stage": "utterance_end",
                                       "result": "wake_word_detected" if not has_meaningful_command else "command_processed"})

            elif match_type == "substring":
                print(f"[SKIP] Substring-only wake match @ idx {match_index} -- not firing: '{text}'")
                self._emit_wake_trace({"stage": "utterance_end", "result": "substring_rejected"})

            elif self.wake_word_triggered:
                print(f"[TEXT] Command: {text}")
                self._emit_wake_trace({"stage": "command_extract",
                                       "from_index": -1, "command": text, "remainder": ""})
                self._process_wake_command(text)
                self._emit_wake_trace({"stage": "utterance_end", "result": "followup_command"})

            else:
                self._emit_wake_trace({"stage": "utterance_end", "result": "no_wake_word"})
                
        except Exception as e:
            print(f"[ERROR] Transcription failed: {e}")
            import traceback
            traceback.print_exc()
            self._log_history(
                raw_text="",
                display_text=f"[FAILED] {e}",
                mode="wake",
                status="failed",
                entry_type="failed",
            )
            # Notify user so they know to retry
            try:
                import winsound
                winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
            except Exception:
                pass
        finally:
            if _set_in_progress:
                self._wake_transcription_in_progress = False
            # Clear Silero VAD internal state so the next utterance starts
            # fresh. Without this, its rolling context bleeds between commands.
            self._vad_reset()

    def _process_wake_command(self, text):
        """Route a wake word command based on parsed intent (4-state machine)."""
        # Transition to command_window while we parse
        old_state = self.app_state
        self.app_state = 'command_window'
        if old_state != 'command_window':
            print(f"[STATE] {old_state} -> command_window")

        intent = parse_wake_command(text)
        print(f"[PARSE] raw='{text}' -> type={intent['type']}, "
              f"name={intent['name']}, content='{intent['content']}'")

        if intent["type"] == "dictation":
            # "type hello" → quick_dictation, "dictate" → long_dictation
            self._start_dictation_mode(
                intent["name"],
                initial_content=intent["content"],
            )
            return

        if intent["type"] == "command_text":
            # Show what we're doing on the indicator
            if hasattr(self, 'listening_indicator'):
                display = text.title() if len(text) < 25 else text[:22].title() + "..."
                self._schedule_ui(self.listening_indicator.set_mode, display)

            # Try regular command execution (pass original text for word-boundary matching)
            result, was_command = self.command_executor.process_text(
                text, self, force_commands=True)
            if was_command:
                _store_cmd = self.command_executor.commands.get(result) or {'type': 'plugin'}
                if (result and not _is_repeat_blacklisted(result, _store_cmd)
                        and self.command_executor.find_command(result) == result):
                    self._last_command = _store_cmd
                    self._last_command_name = result
                if result:
                    increment_command_count(result)
                self.wake_word_triggered = False
                self.app_state = 'asleep'
                # Arm Layer 3: the wake callback suppresses buffering for the
                # next 2s so a Chrome launch chime / notification doesn't get
                # mistaken for a new utterance.
                self._command_executed_at = time.time()
                print("[STATE] command_window -> asleep (command executed)")
                self._indicator_success_and_reset()
                return

            # Not a recognized command -- silently go back to sleep.
            # DO NOT paste unrecognized text after wake word. If the user
            # wanted dictation, they'd say "jarvis, type ..." or "jarvis,
            # dictate". This prevents false wake triggers (e.g. "service"
            # corrected to "jarvis") from typing garbage into the focused app.
            print(f"[SKIP] No command match for '{text}' — back to sleep")
            self.wake_word_triggered = False
            self.app_state = 'asleep'
            print("[STATE] command_window -> asleep (no match)")
            self._indicator_reset()
            return

        # type == "unknown" -- noise/garbage, back to asleep
        print(f"[SKIP] Ignoring noise: '{text}'")
        self.app_state = 'asleep'
        print("[STATE] command_window -> asleep (noise)")
        self._indicator_reset()
        self._start_wake_timeout()
    
    def _start_dictation_mode(self, mode_name, mode_config=None, initial_content=None):
        """Enter quick_dictation or long_dictation state.

        Args:
            mode_name: 'quick_dictation' or 'long_dictation'
            mode_config: ignored (kept for call-site compat), config read from self.config
            initial_content: optional first text chunk to buffer
        """
        old_state = self.app_state
        self.app_state = mode_name
        logger.info(f"[WS-DIAG] app_state set to {self.app_state!r} (was {old_state!r}) via _start_dictation_mode")
        self.wake_dictation_mode = mode_name  # compat alias
        self.wake_dictation_buffer = []
        self.wake_dictation_start_time = time.time()
        self.wake_word_triggered = False
        self._dictation_paused = False

        # Cancel any existing timers
        if hasattr(self, 'wake_word_timer') and self.wake_word_timer:
            self.wake_word_timer.cancel()
        if hasattr(self, '_dictation_finalize_timer') and self._dictation_finalize_timer:
            self._dictation_finalize_timer.cancel()
            self._dictation_finalize_timer = None

        ww_config = self.config.get('wake_word_config', {})

        if mode_name == 'quick_dictation':
            timeout = ww_config.get('quick_silence_timeout', 1.0)
            self._dictation_silence_timeout = timeout
            self._dictation_require_end = False
            print(f"[STATE] {old_state} -> quick_dictation (silence timeout: {timeout}s)")
        else:  # long_dictation
            self._dictation_silence_timeout = None  # silence handled by hard-cap, not VAD
            self._dictation_require_end = True

            # Initialize state-driven finalization tracking.
            # See _maybe_finalize_dictation for the protocol.
            self._dictation_finalize_requested = False
            self._pending_transcriptions = 0

            # Safety net: hard-cap timer in case end-word handling is
            # misconfigured (e.g. user emptied end_words in config) or VAD
            # never declares silence. After max_duration the dictation is
            # finalized with whatever has been buffered so far. Default 15s,
            # configurable via wake_word_config.long_max_duration.
            ww_config = self.config.get('wake_word_config', {})
            max_duration = ww_config.get('long_max_duration', 15.0)
            failsafe_duration = ww_config.get('long_failsafe_duration', 60.0)
            print(f"[STATE] {old_state} -> long_dictation "
                  f"(hard-cap: {max_duration}s, failsafe: {failsafe_duration}s)")

            if hasattr(self, '_dictation_hardcap_timer') and self._dictation_hardcap_timer:
                self._dictation_hardcap_timer.cancel()
            self._dictation_hardcap_timer = threading.Timer(
                max_duration, self._finalize_dictation_hardcap
            )
            self._dictation_hardcap_timer.daemon = True
            self._dictation_hardcap_timer.start()

            # Absolute failsafe — fires only if the soft hard-cap somehow
            # fails to drain the pipeline (e.g. stuck transcription worker).
            # Brutally resets regardless of pending state. Should normally
            # never fire in healthy operation.
            if hasattr(self, '_dictation_failsafe_timer') and self._dictation_failsafe_timer:
                self._dictation_failsafe_timer.cancel()
            self._dictation_failsafe_timer = threading.Timer(
                failsafe_duration, self._absolute_failsafe_reset
            )
            self._dictation_failsafe_timer.daemon = True
            self._dictation_failsafe_timer.start()

        self.play_sound("start")

        # Update listening indicator to show active dictation
        if hasattr(self, 'listening_indicator'):
            label = "Quick Dictation" if mode_name == 'quick_dictation' else "Long Dictation"
            self._schedule_ui(self.listening_indicator.set_mode, label)
            self._schedule_ui(self.listening_indicator.set_listening, True)

        if initial_content:
            self.wake_dictation_buffer.append(initial_content)
            print(f"[DICTATE] Initial content: {initial_content}")
            if not self._dictation_require_end:
                self._restart_dictation_timer()

    def _indicator_success_and_reset(self):
        """Flash success on indicator, hold briefly, then return to idle."""
        if not hasattr(self, 'listening_indicator'):
            return
        self._schedule_ui(self.listening_indicator.flash_success)
        # Hold the lit state for 800ms so the user sees what happened
        def _delayed_reset():
            import time
            time.sleep(0.8)
            self._indicator_reset()
        threading.Thread(target=_delayed_reset, daemon=True).start()

    def _indicator_reset(self):
        """Return indicator to idle state."""
        if not hasattr(self, 'listening_indicator'):
            return
        self._schedule_ui(self.listening_indicator.set_listening, False)
        mode_display = self._get_mode_display() if hasattr(self, '_get_mode_display') else "Hold"
        self._schedule_ui(self.listening_indicator.set_mode, mode_display)

    def _reset_wake_dictation(self):
        """Return to asleep state, clearing all dictation state."""
        old_state = self.app_state
        self.app_state = 'asleep'
        logger.info(f"[WS-DIAG] app_state set to {self.app_state!r} (was {old_state!r})")
        self.wake_dictation_mode = None
        self.wake_dictation_buffer = []
        self.wake_dictation_start_time = None
        self.wake_word_triggered = False
        self._dictation_silence_timeout = None
        self._dictation_require_end = False
        self._dictation_paused = False

        if hasattr(self, 'wake_word_timer') and self.wake_word_timer:
            self.wake_word_timer.cancel()
            self.wake_word_timer = None

        if hasattr(self, '_dictation_finalize_timer') and self._dictation_finalize_timer:
            self._dictation_finalize_timer.cancel()
            self._dictation_finalize_timer = None

        if hasattr(self, '_dictation_hardcap_timer') and self._dictation_hardcap_timer:
            self._dictation_hardcap_timer.cancel()
            self._dictation_hardcap_timer = None

        if hasattr(self, '_dictation_failsafe_timer') and self._dictation_failsafe_timer:
            self._dictation_failsafe_timer.cancel()
            self._dictation_failsafe_timer = None

        # Reset state-driven finalize tracking. Pending count should already
        # be 0 in healthy operation; clamp defensively in case of timer races.
        self._dictation_finalize_requested = False
        self._pending_transcriptions = 0
        self._wake_target_active = False
        self._wake_session_first_chunk = True
        self._wake_session_send_policy = 'enter'

        existing = getattr(self, '_wake_session_inactivity_timer', None)
        if existing is not None:
            existing.cancel()
            self._wake_session_inactivity_timer = None

        if old_state != 'asleep':
            print(f"[STATE] {old_state} -> asleep")

        # Reset listening indicator back to idle
        self._indicator_reset()

    def _restart_dictation_timer(self):
        """Restart the finalization timer for non-end-word dictation modes.

        After accumulating text, this timer gives the user a window to keep speaking.
        If no new speech arrives within the timeout, the accumulated text is output.
        """
        logger.info(f"[WS-DIAG] _restart_dictation_timer called: app_state={self.app_state!r}")
        if hasattr(self, '_dictation_finalize_timer') and self._dictation_finalize_timer:
            self._dictation_finalize_timer.cancel()

        timeout = self._dictation_silence_timeout or 0.6
        self._dictation_finalize_timer = threading.Timer(timeout, self._finalize_dictation_timeout)
        self._dictation_finalize_timer.start()

    def _finalize_dictation_timeout(self):
        """Called when the dictation finalization timer expires."""
        logger.info(f"[WS-DIAG] _finalize_dictation_timeout called: app_state={self.app_state!r}")
        try:
            with self._dictation_finalize_lock:
                if self.wake_dictation_mode and self.wake_dictation_buffer and not self._dictation_require_end:
                    final_text = ' '.join(self.wake_dictation_buffer)
                    print(f"[DONE] Dictation complete: {final_text}")
                    self._output_dictation(final_text)
                    self._reset_wake_dictation()
        except Exception as e:
            print(f"[ERROR] _finalize_dictation_timeout crashed: {e}")
            import traceback
            traceback.print_exc()

    def _finalize_dictation_hardcap(self):
        """Hard-cap soft-finalize for long_dictation. Sets a finalize-requested
        flag, forces any buffered audio to dispatch immediately, then asks the
        pipeline to finalize when it next becomes idle.

        This is NOT the absolute kill switch — that's _absolute_failsafe_reset
        below. The hard-cap is the user-facing "dictation should be done by
        now" signal. The failsafe is the hung-pipeline backstop.

        Architecture (per tribunal review):
          - User speaks → cap fires → flag set → buffer flushed
          - Already-pending transcription completes → finalize check passes
          - State reset happens once the pipeline is fully drained
        """
        try:
            with self._dictation_finalize_lock:
                if self.app_state != 'long_dictation':
                    return
                print("[HARDCAP] Time limit reached — flushing audio and requesting finalize")
                self._dictation_finalize_requested = True
        except Exception as e:
            print(f"[ERROR] _finalize_dictation_hardcap crashed: {e}")
            import traceback
            traceback.print_exc()
            return

        # Force any in-buffer audio to dispatch NOW so the pending counter
        # captures it. Without this, audio currently being captured but not
        # yet flushed by VAD silence would be lost.
        self._flush_speech_buffer_to_transcription()

        # Try to finalize immediately. If transcriptions are still in flight,
        # this is a no-op and finalize will happen via the completion-side
        # call to _maybe_finalize_dictation in process_wake_word_buffer.
        self._maybe_finalize_dictation()

    def _flush_speech_buffer_to_transcription(self):
        """Force whatever audio is currently in self.speech_buffer to dispatch
        to transcription, bypassing the VAD silence threshold. Used by the
        hard-cap to ensure no in-flight audio is lost.

        Returns True if a buffer was dispatched, False if nothing to flush.
        Increments _pending_transcriptions if dispatched.
        """
        with self.buffer_lock:
            if not self.speech_buffer:
                return False
            buffer_copy = self.speech_buffer.copy()
            self.speech_buffer = []

        self.is_speaking = False
        self.silence_start = None

        with self._dictation_finalize_lock:
            self._pending_transcriptions += 1

        threading.Thread(
            target=self._process_wake_word_buffer_tracked,
            args=(buffer_copy,),
            daemon=True,
        ).start()
        return True

    def _process_wake_word_buffer_tracked(self, buffer, src_rate=None):
        """Wrapper around process_wake_word_buffer that decrements the
        pending-transcriptions counter on completion (in finally), then
        triggers a finalize check.
        """
        try:
            self.process_wake_word_buffer(buffer, src_rate=src_rate)
        finally:
            with self._dictation_finalize_lock:
                self._pending_transcriptions = max(0, self._pending_transcriptions - 1)
            # Outside the lock: maybe_finalize takes its own
            self._maybe_finalize_dictation()

    def _maybe_finalize_dictation(self):
        """Centralized finalize check. Called from multiple completion points;
        only finalizes when ALL of these are true:
          - In long_dictation state
          - Finalize has been requested (cap fired or end-word seen)
          - No pending transcriptions
          - No active speech (defensive)

        Idempotent and lock-guarded. Safe to call from any thread.
        """
        try:
            with self._dictation_finalize_lock:
                if self.app_state != 'long_dictation':
                    return
                if not self._dictation_finalize_requested:
                    return
                if self._pending_transcriptions > 0:
                    return
                if getattr(self, 'is_speaking', False):
                    # Speech started again after cap fired. The next silence
                    # transition + completion will retrigger this check.
                    return

                # Pipeline is fully drained. Safe to finalize.
                if self.wake_dictation_mode and self.wake_dictation_buffer:
                    final_text = ' '.join(self.wake_dictation_buffer)
                    print(f"[DONE] Long dictation finalized: {final_text}")
                    # _output_dictation must be called outside the lock to
                    # avoid blocking the pipeline on clipboard/UI work.
                    pending_text = final_text
                else:
                    pending_text = None
                    print("[DONE] Long dictation finalized with empty buffer")

                self._reset_wake_dictation()
            # Released the lock — now do the user-visible output
            if pending_text:
                self._output_dictation(pending_text)
        except Exception as e:
            print(f"[ERROR] _maybe_finalize_dictation crashed: {e}")
            import traceback
            traceback.print_exc()

    def _absolute_failsafe_reset(self):
        """Brutal backstop. Called by an absolute timer (longer than the
        hard-cap). If the pipeline somehow leaks pending counts (worker
        crash, missed decrement, etc.), this guarantees we never hang.

        Resets state regardless of pending count. Logs loudly because if
        this fires it indicates a real bug somewhere.
        """
        try:
            with self._dictation_finalize_lock:
                if self.app_state != 'long_dictation':
                    return
                pending = self._pending_transcriptions
                buf_len = len(self.wake_dictation_buffer) if self.wake_dictation_buffer else 0
                print(f"[FAILSAFE] Absolute timeout — forcing reset "
                      f"(pending={pending}, buf_chunks={buf_len}). "
                      f"This indicates a stuck transcription worker.")
                if self.wake_dictation_buffer:
                    pending_text = ' '.join(self.wake_dictation_buffer)
                else:
                    pending_text = None
                self._reset_wake_dictation()
            if pending_text:
                self._output_dictation(pending_text)
        except Exception as e:
            print(f"[ERROR] _absolute_failsafe_reset crashed: {e}")
            import traceback
            traceback.print_exc()

    _UNDO_EXPIRY_SECONDS = 60.0

    def _paste_preserving_clipboard(self, text):
        """Paste text via clipboard while preserving the user's original clipboard content."""
        delay = self.config.get('clipboard_delay', CLIPBOARD_RESTORE_DELAY)
        paste_ok = False
        with _clipboard_lock:
            saved = _save_clipboard_win32()
            try:
                pyperclip.copy(text)
                time.sleep(CLIPBOARD_PASTE_DELAY)
                pyautogui.hotkey('ctrl', 'v')

                # Wait for paste to complete before restoring
                time.sleep(delay)
                paste_ok = True
            except Exception as e:
                print(f"[ERROR] Paste failed: {e}")
            finally:
                # Always restore clipboard, even if paste failed
                _restore_clipboard_win32(saved)

        if paste_ok:
            self._record_undoable_paste(text)
            self.adaptive_learner.record_transcription(text)

    def _deliver_text_to_focused_editor(self, text):
        # backspace removes focus-primer char; assumes empty input box at session start
        pyautogui.press('x')
        time.sleep(_WAKE_PRIMER_DELAY)
        pyautogui.press('backspace')
        time.sleep(_WAKE_PRIMER_DELAY)
        self._paste_preserving_clipboard(text)

    def _record_undoable_paste(self, text):
        """Remember the last pasted text so it can be undone via voice/hotkey."""
        self._last_dictation_text = text
        self._last_dictation_length = len(text)
        self._arm_undo_timer()

    def _arm_undo_timer(self):
        """Start a fresh expiry timer; cancel any existing one."""
        if self._undo_timer is not None:
            self._undo_timer.cancel()
        self._undo_timer = threading.Timer(self._UNDO_EXPIRY_SECONDS, self._clear_undo)
        self._undo_timer.daemon = True
        self._undo_timer.start()

    def _clear_undo(self):
        """Drop undo state (called on expiry or after a successful undo)."""
        self._last_dictation_text = None
        self._last_dictation_length = 0
        if self._undo_timer is not None:
            self._undo_timer.cancel()
            self._undo_timer = None

    def undo_last_dictation(self):
        """Undo the last dictated text by selecting and deleting it.

        Caveat: this drives Shift+Left + Delete via pyautogui, so it only works
        if the caret is still at the end of the last pasted run. If the user
        clicked away or typed since the paste, the selection will grab the
        wrong characters -- we intentionally do not try to detect that.
        """
        if not self._last_dictation_text:
            print("[UNDO] Nothing to undo")
            self.play_sound("error")
            return False

        text = self._last_dictation_text
        length = self._last_dictation_length
        for _ in range(length):
            pyautogui.hotkey('shift', 'left')
        pyautogui.press('delete')

        preview = text[:50] + ("..." if len(text) > 50 else "")
        print(f"[UNDO] Removed: {preview}")
        self.play_sound("success")
        self._clear_undo()
        return True

    def _report_correction_dialog(self):
        """Show the correction-reporting dialog (must be called on the Qt thread)."""
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        last = self.adaptive_learner.get_last_transcription()
        original, ok = QInputDialog.getText(
            None,
            "Report Correction",
            "What did Samsara transcribe? (edit if needed)",
            text=last,
        )
        if not ok or not original.strip():
            return

        corrected, ok = QInputDialog.getText(
            None,
            "Report Correction",
            f'What should "{original.strip()}" be?',
        )
        if not ok or not corrected.strip():
            return

        original = original.strip()
        corrected = corrected.strip()

        threshold_reached = self.adaptive_learner.record_correction(original, corrected)
        print(f"[LEARN] Correction recorded: '{original}' -> '{corrected}'")

        if threshold_reached:
            reply = QMessageBox.question(
                None,
                "Add to Dictionary?",
                f'Samsara has seen this correction {self.adaptive_learner.THRESHOLD} times.\n\n'
                f'Add "{original}" -> "{corrected}" to your corrections dictionary?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                vt = getattr(self, 'voice_training_window', None)
                if vt is not None:
                    vt.corrections_dict[original] = corrected
                    vt.save_training_data()
                self.adaptive_learner.mark_promoted(original, corrected)
                print(f"[LEARN] Promoted to dictionary: '{original}' -> '{corrected}'")
                self.play_sound("success")

    def _output_dictation(self, text):
        """Output dictated text"""
        # Apply text processing (auto-capitalize, number formatting)
        text = self.process_transcription(text)

        # Deterministic cleanup (filler removal, spacing).
        raw = text
        _cmode = 'verbatim' if getattr(self, '_skip_cleanup', False) else self.config.get('cleanup_mode', 'clean')
        text = clean_text(text, mode=_cmode)

        if self.config['add_trailing_space']:
            text = text + " "

        print(f"[OK] {text}")
        self.play_sound("success")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.flash_success)

        # Add to history
        self.add_to_history(text.strip(), is_command=False)
        self._log_history(
            raw_text=raw,
            display_text=text.strip(),
            mode="wake",
            status="success",
            entry_type="dictation",
        )
        self._notify_main_window(text.strip())

        if self.config['auto_paste']:
            logger.info(
                f"[WS-DIAG] _output_dictation: wake_target_active="
                f"{getattr(self,'_wake_target_active',None)} "
                f"first_chunk={getattr(self,'_wake_session_first_chunk',None)} "
                f"app_state={self.app_state!r}"
            )
            if getattr(self, '_wake_target_active', False):
                if getattr(self, '_wake_session_first_chunk', True):
                    self._deliver_text_to_focused_editor(text)
                    self._wake_session_first_chunk = False
                else:
                    self._paste_preserving_clipboard(' ' + text)
                logger.info(
                    f"[WS-DIAG] about to check restart: app_state={self.app_state!r} "
                    f"(will restart={self.app_state == 'wake_session'})"
                )
                if self.app_state == 'wake_session':
                    self._restart_wake_session_timer()
            else:
                self._paste_preserving_clipboard(text)

        if hasattr(self, 'hints'):
            self.hints.maybe_show(
                'first_wake_dictation',
                "Wake dictation pasted. Say 'undo' to remove it, or follow"
                " up with another wake word command.",
                delay_s=1.5,
            )
            n = self.hints.increment('wake_dictations')
            if n == 3:
                self.hints.maybe_show(
                    'wake_dictation_end_word',
                    "Tip: say an end word like 'over' after dictating to finish"
                    " immediately instead of waiting for silence.",
                    delay_s=2.0,
                )

    def _start_wake_timeout(self):
        """Start timeout for wake word command.
        
        This is the window for the user to speak a command after saying just the wake word.
        Uses a longer timeout (5s default) to give users time to formulate their command.
        This is different from silence_timeout which is for detecting end of speech.
        """
        if hasattr(self, 'wake_word_timer') and self.wake_word_timer:
            self.wake_word_timer.cancel()
        
        # Use a separate, longer timeout for waiting for command after wake word
        ww_config = self.config.get('wake_word_config', {})
        timeout = ww_config.get('audio', {}).get('wake_command_timeout', WAKE_COMMAND_TIMEOUT)
        self.wake_word_timer = threading.Timer(timeout, self.reset_wake_word)
        self.wake_word_timer.start()
    
    def reset_wake_word(self):
        """Reset wake word trigger after timeout"""
        try:
            with self._dictation_finalize_lock:
                if self.wake_word_triggered:
                    print("[TIMEOUT] Wake word timeout - say wake word again")
                    self.wake_word_triggered = False

                # If in dictation mode and timed out, output what we have
                if self.wake_dictation_mode and self.wake_dictation_buffer:
                    ww_config = self.config.get('wake_word_config', {})
                    require_end = ww_config.get('modes', {}).get(self.wake_dictation_mode, {}).get('require_end_word', False)

                    if not require_end:
                        # Output buffered content on timeout
                        final_text = ' '.join(self.wake_dictation_buffer)
                        print(f"[TIMEOUT] Dictation timeout - outputting: {final_text}")
                        self._output_dictation(final_text)
                    else:
                        print(f"[TIMEOUT] Long dictation timeout - say end word or wake word again")
                        self.play_sound("error")

                self._reset_wake_dictation()
        except Exception as e:
            print(f"[ERROR] reset_wake_word crashed: {e}")
            import traceback
            traceback.print_exc()

    def _setup_sounds(self):
        """Set up sound files - create defaults if needed"""
        import wave
        import struct

        self.sounds_dir = Path(__file__).parent / 'sounds'
        self.sounds_dir.mkdir(exist_ok=True)

        # Sound file names
        self.sound_files = {
            'start': self.sounds_dir / 'start.wav',
            'stop': self.sounds_dir / 'stop.wav',
            'success': self.sounds_dir / 'success.wav',
            'error': self.sounds_dir / 'error.wav'
        }

        # Generate default sounds if they don't exist
        sample_rate = 44100

        def generate_tone(frequency, duration, volume=0.5):
            """Generate a sine wave tone"""
            n_samples = int(sample_rate * duration)
            t = np.linspace(0, duration, n_samples, False)
            tone = np.sin(2 * np.pi * frequency * t) * volume

            # Fade in/out to prevent clicks
            fade_samples = min(int(sample_rate * 0.01), n_samples // 4)
            if fade_samples > 0:
                tone[:fade_samples] *= np.linspace(0, 1, fade_samples)
                tone[-fade_samples:] *= np.linspace(1, 0, fade_samples)

            return tone

        def save_wav(filepath, audio_data):
            """Save audio data as WAV file"""
            with wave.open(str(filepath), 'w') as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(sample_rate)
                # Convert float to 16-bit int
                audio_int = (audio_data * 32767).astype(np.int16)
                wav_file.writeframes(audio_int.tobytes())

        # Create default sounds if they don't exist
        if not self.sound_files['start'].exists():
            # Rising tone
            tone = generate_tone(660, 0.12, volume=0.6)
            save_wav(self.sound_files['start'], tone)

        if not self.sound_files['stop'].exists():
            # Falling tone
            tone = generate_tone(440, 0.1, volume=0.5)
            save_wav(self.sound_files['stop'], tone)

        if not self.sound_files['success'].exists():
            # Happy arpeggio
            t1 = generate_tone(523, 0.08, volume=0.5)
            gap = np.zeros(int(sample_rate * 0.02))
            t2 = generate_tone(659, 0.08, volume=0.5)
            t3 = generate_tone(784, 0.12, volume=0.5)
            audio = np.concatenate([t1, gap, t2, gap, t3])
            save_wav(self.sound_files['success'], audio)

        if not self.sound_files['error'].exists():
            # Low double beep
            t1 = generate_tone(220, 0.15, volume=0.5)
            gap = np.zeros(int(sample_rate * 0.08))
            t2 = generate_tone(196, 0.18, volume=0.5)
            audio = np.concatenate([t1, gap, t2])
            save_wav(self.sound_files['error'], audio)

        # Pre-load sounds into memory cache for low-latency playback
        self._sound_cache = {}
        self._sound_stream_sr = 44100  # Standard sample rate for output stream
        self._load_sound_cache()

        # Persistent output stream for low-latency sound playback.
        # Unlike sd.play() (which creates/destroys a stream per call and conflicts
        # with InputStream), a persistent OutputStream coexists safely.
        self._playback_buffer = np.zeros((0, 1), dtype=np.float32)
        self._buffer_lock = threading.Lock()
        self._sound_stream = None
        self._start_sound_stream()

        # Daemon thread that polls Windows for default output device changes
        # and restarts the output streams when the user switches devices.
        self._output_watcher_stop = threading.Event()
        threading.Thread(
            target=self._watch_output_device, daemon=True,
            name='samsara-output-watcher',
        ).start()

    def _load_sound_cache(self):
        """Pre-load all sound files into memory, normalized to common sample rate.

        Supports WAV natively, and MP3/OGG/FLAC if pydub is installed.

        Loads in two passes:
          1. Legacy hard-coded names (start/stop/success/error) from
             self.sound_files -- backed by sounds/<name>.* so user "Browse..."
             customisations still win.
          2. Auto-discover any other .wav files in the active theme directory
             (sounds/themes/<sound_theme>/) by file-stem. This is how the
             Phase-2 earcon vocabulary (capture_started, capture_saved,
             agent_routing, etc.) is loaded -- no hard-coded list needed.
        """
        self._sound_cache = {}
        target_sr = self._sound_stream_sr

        # Check for pydub support (enables MP3, OGG, FLAC, etc.)
        try:
            from pydub import AudioSegment
            HAS_PYDUB = True
        except ImportError:
            HAS_PYDUB = False

        for sound_type, sound_file in self.sound_files.items():
            # Look for sound file with various extensions
            sound_path = None
            base_path = sound_file.with_suffix('')
            for ext in ['.wav', '.mp3', '.ogg', '.flac', '.m4a']:
                test_path = base_path.with_suffix(ext)
                if test_path.exists():
                    sound_path = test_path
                    break

            # Also check the original path as-is
            if sound_path is None and sound_file.exists():
                sound_path = sound_file

            if sound_path is None:
                continue

            try:
                suffix = sound_path.suffix.lower()

                # Use pydub for non-WAV formats
                if suffix != '.wav' and HAS_PYDUB:
                    audio_seg = AudioSegment.from_file(str(sound_path))
                    # Convert to mono, target sample rate
                    audio_seg = audio_seg.set_channels(1).set_frame_rate(target_sr)
                    # Get raw samples as numpy array
                    samples = np.array(audio_seg.get_array_of_samples()).astype(np.float32)
                    # Normalize to -1 to 1
                    samples = samples / (2 ** (audio_seg.sample_width * 8 - 1))
                    audio_array = samples.reshape(-1, 1)
                    self._sound_cache[sound_type] = audio_array
                    continue
                elif suffix != '.wav':
                    # Non-WAV without pydub - skip
                    print(f"[AUDIO] Skipping {sound_path.name} - install pydub for MP3/OGG support")
                    continue
                
                # Native WAV loading
                with wave.open(str(sound_path), 'rb') as wf:
                    sample_rate = wf.getframerate()
                    n_channels = wf.getnchannels()
                    sample_width = wf.getsampwidth()
                    audio_data = wf.readframes(wf.getnframes())

                if sample_width == 1:
                    dtype = np.uint8
                elif sample_width == 2:
                    dtype = np.int16
                else:
                    dtype = np.int32

                audio_array = np.frombuffer(audio_data, dtype=dtype).astype(np.float32)

                if sample_width == 1:
                    audio_array = (audio_array - 128) / 128.0
                else:
                    audio_array = audio_array / (2 ** (sample_width * 8 - 1))

                # Mix stereo to mono
                if n_channels == 2:
                    audio_array = audio_array.reshape(-1, 2).mean(axis=1)

                # Resample to target rate if needed
                if sample_rate != target_sr:
                    duration = len(audio_array) / sample_rate
                    new_length = int(duration * target_sr)
                    indices = np.linspace(0, len(audio_array) - 1, new_length)
                    audio_array = np.interp(indices, np.arange(len(audio_array)), audio_array)

                # Ensure mono float32 column vector for stream write
                audio_array = audio_array.astype(np.float32).reshape(-1, 1)

                self._sound_cache[sound_type] = audio_array
            except Exception as e:
                print(f"[AUDIO] Failed to load {sound_path}: {e}")

        # Pass 2: auto-discover extended earcons in the active theme dir.
        # Anything not already in the cache (legacy 4 win) gets loaded by
        # file-stem so new earcons drop in without code changes.
        try:
            theme_name = self.config.get('sound_theme', 'cute') if hasattr(self, 'config') else 'cute'
            themes_root = self.sounds_dir / 'themes' / theme_name
            if themes_root.is_dir():
                for wav_path in sorted(themes_root.glob('*.wav')):
                    name = wav_path.stem
                    if name in self._sound_cache:
                        continue  # legacy 4 or already loaded
                    try:
                        with wave.open(str(wav_path), 'rb') as wf:
                            sample_rate = wf.getframerate()
                            n_channels = wf.getnchannels()
                            sample_width = wf.getsampwidth()
                            audio_data = wf.readframes(wf.getnframes())

                        if sample_width == 1:
                            dtype = np.uint8
                        elif sample_width == 2:
                            dtype = np.int16
                        else:
                            dtype = np.int32

                        audio_array = np.frombuffer(audio_data, dtype=dtype).astype(np.float32)
                        if sample_width == 1:
                            audio_array = (audio_array - 128) / 128.0
                        else:
                            audio_array = audio_array / (2 ** (sample_width * 8 - 1))
                        if n_channels == 2:
                            audio_array = audio_array.reshape(-1, 2).mean(axis=1)
                        if sample_rate != target_sr:
                            duration = len(audio_array) / sample_rate
                            new_length = int(duration * target_sr)
                            indices = np.linspace(0, len(audio_array) - 1, new_length)
                            audio_array = np.interp(indices, np.arange(len(audio_array)), audio_array)
                        audio_array = audio_array.astype(np.float32).reshape(-1, 1)
                        self._sound_cache[name] = audio_array
                    except Exception as e:
                        print(f"[AUDIO] Failed to load extended earcon {wav_path.name}: {e}")
        except Exception as e:
            print(f"[AUDIO] Extended-earcon discovery failed: {e}")

    def _start_sound_stream(self):
        """Start the persistent output stream for sound playback.
        
        This stream stays open for the lifetime of the app. Unlike sd.play()
        (which creates/destroys a temporary stream per call), a persistent
        OutputStream coexists safely with the InputStream used for recording.
        """
        try:
            self._sound_stream = sd.OutputStream(
                samplerate=self._sound_stream_sr,
                channels=1,
                dtype='float32',
                callback=self._sound_stream_callback,
                blocksize=1024,  # ~23ms at 44100Hz — good balance of latency vs efficiency
            )
            self._sound_stream.start()
            print("[AUDIO] Persistent sound stream started")
        except Exception as e:
            print(f"[AUDIO] Failed to start sound stream: {e}")
            self._sound_stream = None

    def _sound_stream_callback(self, outdata, frames, time_info, status):
        """Callback for the persistent output stream. Feeds audio from buffer."""
        try:
            with self._buffer_lock:
                n_buffered = len(self._playback_buffer)
                if n_buffered >= frames:
                    outdata[:] = self._playback_buffer[:frames]
                    self._playback_buffer = self._playback_buffer[frames:]
                elif n_buffered > 0:
                    outdata[:n_buffered] = self._playback_buffer
                    outdata[n_buffered:] = 0
                    self._playback_buffer = np.zeros((0, 1), dtype=np.float32)
                else:
                    outdata[:] = 0  # Silence when nothing to play
        except (sd.PortAudioError, OSError) as e:
            print(f"[AUDIO] Sound stream error: {e}")
            return

    def reload_sounds(self):
        """Reload sounds from disk (call after changing sound files)"""
        print("[AUDIO] Reloading sounds...")
        self._load_sound_cache()

    def play_sound(self, sound_type, use_winsound=False):
        """Play audio feedback sound via persistent output stream (non-blocking, low-latency).

        Writes pre-loaded audio data into the playback buffer. The persistent
        OutputStream callback drains it automatically. New sounds replace any
        currently playing sound (clean cutoff, no artifacts).

        Args:
            sound_type: legacy ('start'|'stop'|'success'|'error') or any
                earcon name auto-discovered from the active theme directory
                (e.g. 'capture_started', 'thinking_pulse').
            use_winsound: Deprecated/ignored.
        """
        if not self.config.get('audio_feedback', True):
            return

        # Notify AudioCoordinator so it can duck TTS volume if TTS is active.
        # getattr guard means play_sound works before the coordinator is set up.
        if getattr(self, 'audio_coordinator', None) is not None:
            self.audio_coordinator.on_earcon_starting(sound_type)

        cached = self._sound_cache.get(sound_type)
        if cached is None:
            # Surface unknown names once per name so missing earcons show up
            # in logs instead of silently dropping.
            if not hasattr(self, '_warned_sound_misses'):
                self._warned_sound_misses = set()
            if sound_type not in self._warned_sound_misses:
                self._warned_sound_misses.add(sound_type)
                print(f"[AUDIO] No cached sound for '{sound_type}' "
                      f"(check sounds/themes/<theme>/{sound_type}.wav)")
            return

        volume = self.config.get('sound_volume', 0.5)
        if volume <= 0:
            return

        # Scale volume and write to buffer — the stream callback handles the rest
        scaled = (cached * volume).astype(np.float32)
        with self._buffer_lock:
            self._playback_buffer = scaled  # Replace buffer (new sound wins)

    def stop_sound_stream(self):
        """Stop the persistent sound stream (call on app shutdown)"""
        print("[AUDIO] Stopping sound stream...")
        if self._sound_stream is not None:
            try:
                self._sound_stream.stop()
                self._sound_stream.close()
            except Exception:
                pass
            self._sound_stream = None

    def _watch_output_device(self):
        """Daemon thread: poll Windows every 2 s for default output device changes."""
        current_id = _get_default_render_id()
        stop = getattr(self, '_output_watcher_stop', None)
        if stop is None:
            return
        while not stop.wait(2.0):
            new_id = _get_default_render_id()
            if new_id and new_id != current_id:
                current_id = new_id
                try:
                    self._on_output_device_changed()
                except Exception as exc:
                    print(f'[AUDIO] Device change handler error: {exc}')

    def _on_output_device_changed(self):
        """Restart output streams after the Windows default audio device changes."""
        print('[AUDIO] Default output device changed — restarting streams')
        self.stop_sound_stream()
        self._start_sound_stream()
        eng = getattr(self, 'tts_engine', None)
        if eng is not None and hasattr(eng, 'restart_stream'):
            eng.restart_stream()


    def start_recording(self, streaming=None, play_earcon=True):
        """Start recording audio.

        streaming overrides:
          None  -- decide from config (legacy callers).
          False -- force batch mode (Ctrl+Shift hotkey path).
          True  -- force streaming (CapsLock hotkey path).
        play_earcon: play the "start" sound and brief wait (skip for command mode
          which manages its own debounced earcon).
        """
        if not self.model_loaded:
            if self.loading_model:
                print("Model still loading, please wait...")
            else:
                print("Model not loaded!")
            return

        if self._stop_in_flight:
            print("[HOTKEY] start_recording ignored — stop still in flight")
            return

        # Suppress wake word processing during hotkey recording
        self._hotkey_recording = True

        # Caller-forced streaming mode wins; otherwise fall back to the
        # config + 'hold' check. Streaming-mode in toggle/continuous is
        # not supported -- those paths use the existing batch behavior.
        if streaming is None:
            streaming = (self.config.get('streaming_mode', False)
                         and self.config.get('mode', 'hold') == 'hold')

        # Play start sound before opening capture.
        # Skipped for command mode which manages its own debounced 200ms earcon.
        if play_earcon:
            self.play_sound("start", use_winsound=True)
            time.sleep(0.15)  # Brief pause for sound to start

        if not streaming:
            # ACE path (ACE-03): DictationSessionConsumer provides audio from the
            # permanent engine ring. activate() rewinds to include prebuffer history
            # and applies the TTS contamination guard internally.
            if self._dictation_consumer is None:
                print("[ERROR] ACE dictation consumer not available — cannot record")
                self._hotkey_recording = False
                self.play_sound("error")
                if hasattr(self, 'listening_indicator'):
                    self._schedule_ui(self.listening_indicator.flash_error)
                return
            self._dictation_consumer.activate()
            self._ace_dictation_active = True
        else:
            # CapsLock streaming path (ACE-04B).
            self._ace_dictation_active = False
            # ACE path: streaming accumulator in consumer, no separate stream.
            self._dictation_consumer.activate_streaming()
            self._ace_streaming_active = True
            if hasattr(self, 'hints'):
                self.hints.maybe_show(
                    'streaming_mode',
                    "Streaming: text appears live as you speak."
                    " Final version replaces it on release.",
                    delay_s=1.0,
                )

        self.set_app_state(recording=True)

        # Update tray icon to show active recording (critical for toggle mode
        # where there's no physical key-hold to indicate state)
        self._request_icon_chase('recording')
        if hasattr(self, 'tray_icon'):
            self.tray_icon.title = f"Samsara - RECORDING"

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, True)

        if streaming:
            from samsara.streaming import StreamingSession
            self._streaming_session = StreamingSession(self)
            self._streaming_session.start()

    def stop_recording(self):
        """Stop recording and transcribe"""
        if not self.recording:
            return

        self.set_app_state(recording=False)
        # Only re-enable wake word if a new recording hasn't already started.
        # The deferred stop (trailing buffer) can fire AFTER the user has
        # pressed the hotkey again and started a new recording. In that case
        # hotkey_pressed is True and clearing _hotkey_recording here would
        # open the wake path mid-recording, letting it inject audio into the
        # active hold-mode session (observed: "loop loop loop" artifact).
        if not self.hotkey_pressed:
            self._hotkey_recording = False
        self.play_sound("stop")

        # Restore tray icon — release recording reason (wake_word may keep it spinning)
        self._release_icon_chase('recording')
        self._update_tray_tooltip()

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)
        
        # Trailing buffer: keep capturing briefly after the button is released
        # so the last word isn't clipped. 250 ms covers ~1 syllable at normal pace.
        tail_ms = self.config.get('recording_tail_ms', 250)
        if tail_ms > 0:
            time.sleep(tail_ms / 1000)

        if getattr(self, '_ace_dictation_active', False):
            # ACE path (ACE-03): drain consumer frames; no stream to close.
            self._ace_dictation_active = False
            audio = self._dictation_consumer.drain()
            if audio is None:
                print("[ACE] No audio captured or epoch abort")
                self.play_sound("error")
                if hasattr(self, 'listening_indicator'):
                    self._schedule_ui(self.listening_indicator.flash_error)
                return
        else:
            # Streaming path (CapsLock): ACE-04B consumer accumulator — no stream to close.
            # consumer stop_streaming() called inside StreamingSession.finalize().
            self._ace_streaming_active = False

            # Streaming session (CapsLock path): hand off to it and return.
            # The StreamingWorker calls consumer.stop_streaming() inside
            # _final_pass() to get the authoritative final audio; do NOT call
            # it here -- that would clear _streaming_frames before the worker
            # reads them, causing a silent loss of the recording.
            sess = getattr(self, '_streaming_session', None)
            if sess is not None:
                self._streaming_session = None
                try:
                    sess.finalize()
                except Exception as e:
                    print(f"[STREAM] finalize failed: {e}")
                return

        print("[...] Transcribing...")

        # Transcribe in background to not block hotkey listener
        def transcribe():
            try:
                audio_duration = len(audio) / self.model_rate
                
                # Get transcription parameters based on performance mode
                transcribe_params = self.get_transcription_params()
                # DISABLE faster-whisper's VAD for hotkey-triggered dictation.
                # User explicitly pressed the hotkey — don't strip their speech.
                transcribe_params['vad_filter'] = False
                perf_mode = self.config.get('performance_mode', 'balanced')

                # Guard: Whisper hallucinates on very short audio (<0.5s)
                if audio_duration < 0.51:
                    print(f"[SKIP] Audio too short ({audio_duration:.2f}s) — skipping")
                    return

                transcribe_start = time.time()

                if audio_duration > 30.0:
                    # Long audio: split at silence boundaries before transcription.
                    # Whisper's internal 30s chunking splits at arbitrary positions
                    # that can land mid-word.  Splitting at silence boundaries first
                    # keeps each chunk acoustically clean.
                    #
                    # Do NOT set condition_on_previous_text=True here — conditioning
                    # over long stitched sequences triggers Whisper's repetition-loop
                    # hallucination bug (the model echos earlier text indefinitely).
                    chunks = _split_audio_at_silences(audio, self.model_rate)
                    print(f"[LONG] {audio_duration:.1f}s recording split into "
                          f"{len(chunks)} chunk(s) at silence boundaries")
                    texts = []
                    for idx, chunk in enumerate(chunks):
                        chunk_dur = len(chunk) / self.model_rate
                        if chunk_dur < 0.2:
                            continue
                        with self.model_lock:
                            segs, _ = self.model.transcribe(chunk, **transcribe_params)
                        _segs_list = list(segs)
                        chunk_text = "".join(s.text for s in _segs_list).strip()
                        if _is_hallucinated_segments(_segs_list, chunk_text):
                            logging.getLogger("Samsara").info(
                                f"[GUARD] Suppressed hallucinated chunk {idx+1}: {chunk_text!r}")
                            chunk_text = ""
                        if chunk_text:
                            texts.append(chunk_text)
                        print(f"[LONG] Chunk {idx + 1}/{len(chunks)}: "
                              f"{chunk_dur:.1f}s → {len(chunk_text)} chars")
                    text = " ".join(texts).strip()
                else:
                    with self.model_lock:
                        segments, info = self.model.transcribe(audio, **transcribe_params)
                    _seg_list = list(segments)
                    text = "".join([s.text for s in _seg_list]).strip()
                    if _is_hallucinated_segments(_seg_list, text):
                        logging.getLogger("Samsara").info(
                            f"[GUARD] Suppressed hallucination: {text!r}")
                        text = ""

                transcribe_time = time.time() - transcribe_start

                # Performance logging
                rtf = transcribe_time / audio_duration if audio_duration > 0 else 0
                device_info = getattr(self, 'device_type', 'unknown')
                print(f"[PERF] Audio: {audio_duration:.1f}s | Transcribe: {transcribe_time*1000:.0f}ms | "
                      f"RTF: {rtf:.2f}x | Mode: {perf_mode} | Device: {device_info}")
                
                # Apply corrections dictionary
                text = self.voice_training_window.apply_corrections(text)
                
                # Check which mode produced this recording
                is_command_mode = self.command_mode_recording
                is_ava_mode = self.ava_mode_recording
                self.command_mode_recording = False
                self.ava_mode_recording = False

                # Ghost-tap prevention — discard accidental sub-debounce taps
                if is_command_mode and self._command_mode_ghost_tap:
                    self._command_mode_ghost_tap = False
                    print("[CMD] Ghost tap — discarding transcription")
                    return

                if is_ava_mode and self._ava_mode_ghost_tap:
                    self._ava_mode_ghost_tap = False
                    print("[AVA] Ghost tap — discarding transcription")
                    return

                if text:
                    text_lower = text.lower().strip()

                    # Voice exit from Mouse 4 command mode
                    if is_command_mode and any(
                        p in text_lower for p in ["exit command mode", "stop listening"]
                    ):
                        print(f"[CMD MODE] Voice exit: '{text_lower}'")
                        self.exit_command_mode()
                        return

                    # Command matching ONLY runs in command mode (Right Ctrl / Mouse 4).
                    # Hold-to-dictate (Ctrl+Shift) always outputs text — never matches
                    # commands, so words like "bring", "copy", "cut" are transcribed
                    # as-is rather than firing the corresponding voice command.
                    if is_command_mode:
                        result, was_command = self.command_executor.process_text(text, self)

                        if was_command:
                            _store_cmd = self.command_executor.commands.get(result) or {'type': 'plugin'}
                            if (result and not _is_repeat_blacklisted(result, _store_cmd)
                                    and self.command_executor.find_command(result) == result):
                                self._last_command = _store_cmd
                                self._last_command_name = result
                            if result:
                                increment_command_count(result)
                            # Command was executed - add to history as command
                            self.add_to_history(text, is_command=True)
                            self._log_history(
                                raw_text=text,
                                duration_ms=int(audio_duration * 1000),
                                mode="command",
                                status="success",
                                entry_type="command",
                                matched_command=str(result) if result else None,
                            )
                            # Toggle mode: reset miss count, refresh inactivity, re-arm
                            if self.command_mode_active:
                                self._command_mode_miss_count = 0
                                cm_cfg = self.config.get('command_mode', {})
                                if cm_cfg.get('mode', 'hold') == 'toggle':
                                    timeout_s = cm_cfg.get('inactivity_timeout_s', 30)
                                    self._reset_command_mode_inactivity_timer(timeout_s)
                                    threading.Thread(
                                        target=self._rearm_command_recording,
                                        daemon=True).start()
                            return

                        # No command matched in command mode — don't output text
                        print(f"[CMD] No command matched: '{text}'")
                        if self.command_mode_active:
                            self._command_mode_miss_count += 1
                            cm_cfg = self.config.get('command_mode', {})
                            miss_limit = cm_cfg.get('miss_limit', 5)
                            if (cm_cfg.get('mode', 'hold') == 'toggle'
                                    and self._command_mode_miss_count >= miss_limit):
                                print(f"[CMD MODE] Miss limit ({miss_limit}) reached")
                                self.exit_command_mode()
                            elif cm_cfg.get('mode', 'hold') == 'toggle':
                                threading.Thread(
                                    target=self._rearm_command_recording,
                                    daemon=True).start()
                        return

                    # --- Ava mode (Right Alt) ---
                    if is_ava_mode:
                        self._route_to_ava(text)
                        return

                    # Regular dictation mode - proceed with text output
                    # Apply text processing (auto-capitalize, number formatting)
                    text = self.process_transcription(text)

                    # Deterministic cleanup (filler removal, spacing).
                    raw = text
                    _cmode = 'verbatim' if getattr(self, '_skip_cleanup', False) else self.config.get('cleanup_mode', 'clean')
                    text = clean_text(text, mode=_cmode)

                    if self.config['add_trailing_space']:
                        text = text + " "

                    print(f"[OK] {text}")
                    self.play_sound("success")
                    if hasattr(self, 'listening_indicator'):
                        self._schedule_ui(self.listening_indicator.flash_success)

                    # Add to history
                    self.add_to_history(text.strip(), is_command=False)
                    self._log_history(
                        raw_text=raw,
                        display_text=text.strip(),
                        duration_ms=int(audio_duration * 1000),
                        mode="hold",
                        status="success",
                        entry_type="dictation",
                    )
                    self._notify_main_window(text.strip())

                    if self.config['auto_paste']:
                        self._paste_preserving_clipboard(text)

                    if hasattr(self, 'hints'):
                        self.hints.maybe_show(
                            'first_dictation_undo',
                            "Tip: say 'undo' to undo what was just typed.",
                        )
                        n = self.hints.increment('hold_dictations')
                        if n == 3:
                            self.hints.maybe_show(
                                'wake_word_suggestion',
                                "Tip: try wake word mode — say 'Jarvis, [command]'"
                                " without holding any keys. Enable it in Settings.",
                                delay_s=2.0,
                            )
                        elif n == 5:
                            self.hints.maybe_show(
                                'command_mode_intro',
                                "Tip: hold the hotkey and say a command like 'new line',"
                                " 'undo', or 'select all' to control your keyboard by voice.",
                                delay_s=2.0,
                            )
                        elif n == 10:
                            self.hints.maybe_show(
                                'dictation_cleanup_tip',
                                "Tip: if transcription adds unwanted filler words or"
                                " punctuation, try 'Verbatim' cleanup mode in Settings.",
                                delay_s=2.0,
                            )
                else:
                    print("No speech detected")
                    self.command_mode_recording = False  # Reset flag on no speech too
                    # Only log "empty" when there was actually audio to transcribe.
                    # Whisper hallucination guard above already filtered <0.5s.
                    if audio_duration > 0.5:
                        self._log_history(
                            raw_text="",
                            display_text="(no speech detected)",
                            duration_ms=int(audio_duration * 1000),
                            mode="hold",
                            status="empty",
                            entry_type="failed",
                        )

            except Exception as e:
                print(f"[ERROR] Transcription failed: {e}")
                self.play_sound("error")
                if hasattr(self, 'listening_indicator'):
                    self._schedule_ui(self.listening_indicator.flash_error)
                self._log_history(
                    raw_text="",
                    display_text=f"[FAILED] {e}",
                    mode="hold",
                    status="failed",
                    entry_type="failed",
                )
                # Notify user so they know to retry
                try:
                    import winsound
                    winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
                except Exception:
                    pass

        thread = threading.Thread(target=transcribe, daemon=True)
        thread.start()

    def cancel_recording(self):
        """Cancel recording without transcribing"""
        if not self.recording:
            return

        self.set_app_state(recording=False)
        if not self.hotkey_pressed:
            self._hotkey_recording = False  # Re-enable wake word processing
        print("[X] Recording cancelled")

        if getattr(self, '_ace_dictation_active', False):
            # ACE path: discard accumulated frames, no stream to close.
            self._ace_dictation_active = False
            if self._dictation_consumer is not None:
                self._dictation_consumer.cancel()

        self.play_sound("error")  # Play error sound to indicate cancellation

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)
            self._schedule_ui(self.listening_indicator.flash_error)

    def apply_mode(self, new_mode):
        """Apply a capture-mode change at runtime.

        Valid modes: 'hold', 'toggle', 'continuous'.
        Wake word is now a separate boolean (see set_wake_word_enabled).
        Returns True if the mode was applied, False if unchanged or invalid.
        """
        valid_modes = ('hold', 'toggle', 'continuous')
        if new_mode not in valid_modes:
            print(f"[MODE] Refused invalid mode: {new_mode}")
            return False

        current_mode = self.config.get('mode', 'hold')
        if new_mode == current_mode:
            return False

        # If currently recording (hold or toggle mode), stop the recording
        if self.recording:
            self.stop_recording()
            print(f"[MODE] Stopped active recording before mode switch")

        # Reset toggle state so it doesn't carry over
        self.toggle_active = False

        # Stop continuous mode if it was active but new mode is different
        if self.continuous_active and new_mode != 'continuous':
            self.stop_continuous_mode()
            print(f"[MODE] Deactivated continuous mode")

        # Activate continuous if that's the new mode
        if new_mode == 'continuous' and not self.continuous_active:
            self.start_continuous_mode()
            print(f"[MODE] Activated continuous mode")

        self.config['mode'] = new_mode
        print(f"[MODE] Mode changed to: {new_mode}")

        if hasattr(self, 'hints'):
            if new_mode == 'toggle':
                self.hints.maybe_show(
                    'toggle_mode_first',
                    "Toggle mode: tap the hotkey once to start, tap again to stop."
                    " Good for longer dictations without holding a key.",
                    delay_s=1.5,
                )
            elif new_mode == 'continuous':
                self.hints.maybe_show(
                    'continuous_mode_first',
                    "Continuous mode: recording stays on and handles pauses"
                    " automatically. Press the hotkey to stop.",
                    delay_s=1.5,
                )
            elif new_mode == 'hold':
                self.hints.maybe_show(
                    'hold_mode_return',
                    "Hold mode: hold the hotkey while speaking, release to transcribe.",
                    delay_s=1.5,
                )

        # Update listening indicator and tray tooltip
        display = self._get_mode_display()
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_mode, display)
        self._update_tray_tooltip()

        return True

    def _start_gesture_lane(self) -> None:
        """Start CameraService + GestureLoop. Safe to call from any thread."""
        if self._gesture_loop is not None:
            return
        try:
            from samsara.vision.camera_service import CameraService
            from samsara.vision.gesture_loop import GestureLoop
            gesture_cfg = self.config.get('gesture', {})
            device_index = gesture_cfg.get('device_index', 0)
            profile = gesture_cfg.get('profile', {})
            svc = CameraService.get_instance()
            svc.start(device_index=device_index, profile=profile or None)
            self._camera_service = svc
            loop = GestureLoop(self, svc, gesture_cfg)
            loop.start()
            self._gesture_loop = loop
            print("[GESTURE] Lane started")
        except Exception as _e:
            print(f"[GESTURE] Failed to start: {_e}")
            self._camera_service = None
            self._gesture_loop = None

    def _stop_gesture_lane(self) -> None:
        """Stop GestureLoop and release camera handle."""
        loop = self._gesture_loop
        if loop is not None:
            try:
                loop.stop()
            except Exception:
                pass
            self._gesture_loop = None
        svc = self._camera_service
        if svc is not None:
            try:
                svc.stop()
            except Exception:
                pass
            self._camera_service = None
        print("[GESTURE] Lane stopped")

    def set_gesture_enabled(self, enabled: bool) -> None:
        """Enable or disable the gesture lane and persist the setting."""
        with self._config_lock:
            self.config.setdefault('gesture', {})['enabled'] = enabled
            self.save_config()
        if enabled and self._gesture_loop is None:
            self._start_gesture_lane()
            print("[GESTURE] Lane ENABLED")
        elif not enabled and self._gesture_loop is not None:
            self._stop_gesture_lane()
            print("[GESTURE] Lane DISABLED")

    def set_wake_word_enabled(self, enabled):
        """Start or stop the wake word listener independently of capture mode."""
        with self._config_lock:
            self.config['wake_word_enabled'] = enabled
            self.save_config()
        if enabled and not self.wake_word_active:
            self.start_wake_word_mode()
            print("[WAKE] Wake word listener ENABLED")
        elif not enabled and self.wake_word_active:
            self.stop_wake_word_mode()
            print("[WAKE] Wake word listener DISABLED")
        # Update tray tooltip
        self._update_tray_tooltip()
        # Update listening indicator mode label
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_mode, self._get_mode_display())

    def switch_mode_from_tray(self, new_mode):
        """Tray-menu entry point: apply the mode, persist it, refresh the menu."""
        changed = self.apply_mode(new_mode)
        if changed:
            self.persist_config()
        self._update_tray_tooltip()

    def show_main_window(self):
        """Open (or refocus) the main hub window."""
        try:
            self.main_window.show()
            # Tkinter fallback: rebind close to minimize-to-tray.
            top = getattr(self.main_window, '_toplevel', None)
            if top is not None:
                top.protocol("WM_DELETE_WINDOW", self.hide_main_window)
        except Exception as e:
            print(f"[UI] Failed to show main window: {e}")

    def hide_main_window(self):
        """Close button on the hub: just minimize to tray."""
        try:
            self.main_window.hide()
        except Exception as e:
            print(f"[UI] Failed to hide main window: {e}")

    def set_streaming_mode(self, enabled):
        """Tray-menu entry point: flip the streaming-mode flag."""
        enabled = bool(enabled)
        if self.config.get('streaming_mode', False) == enabled:
            return
        with self._config_lock:
            self.config['streaming_mode'] = enabled
            self.save_config()
        print(f"[STREAM] streaming_mode -> {enabled}")

        # Install or release the CapsLock hook to match. When streaming is
        # off we don't grab CapsLock at all, so it works as normal Windows
        # caps toggle.
        if enabled:
            self._install_capslock_hook()
        else:
            self._uninstall_capslock_hook()


    def set_cleanup_mode(self, mode):
        """Tray-menu entry point: switch between 'clean' and 'verbatim' cleanup."""
        if mode not in ('clean', 'verbatim'):
            return
        if self.config.get('cleanup_mode') == mode:
            return
        with self._config_lock:
            self.config['cleanup_mode'] = mode
            self.save_config()
        print(f"[CLEANUP] Mode -> {mode}")

    def _get_mode_display(self):
        """Build a display string for the current mode + wake word state."""
        mode = self.config.get('mode', 'hold').title()
        if self.config.get('wake_word_enabled', False):
            return f"{mode} + Wake"
        return mode

    def _update_tray_tooltip(self):
        """Refresh the tray icon tooltip to reflect current mode/wake state."""
        if not hasattr(self, 'tray_icon'):
            return
        if self.snoozed:
            return  # snooze tooltip managed by _update_snooze_tooltip
        self.tray_icon.title = f"Samsara - {self._get_mode_display()}"

    # Wheel icon color scheme
    _WHEEL_COLORS = ['#185FA5', '#C0392B', '#1A1A1A']   # blue, red, black
    _WHEEL_IDLE   = ['#555555', '#666666', '#555555']
    _WHEEL_SNOOZE = ['#333333', '#333333', '#333333']
    _WHEEL_GOLD   = '#D4A017'

    def _schedule_ui(self, func, *args):
        """Schedule a function on the Qt main thread (replaces root.after).

        Safe to call from any thread.  Falls back to a direct call if Qt
        is not available so non-GUI code paths (tests, CI) still work.
        """
        if not self._running:
            return
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        qt_app = QApplication.instance()
        if qt_app is not None:
            QTimer.singleShot(0, qt_app, lambda: func(*args))
        else:
            try:
                func(*args)
            except Exception:
                pass

    @staticmethod
    def _arc_polygon(cx, cy, outer_r, inner_r, start_rad, end_rad, steps=24):
        """Return polygon points for a thick arc segment."""
        pts = []
        for i in range(steps + 1):
            t = start_rad + (end_rad - start_rad) * i / steps
            pts.append((cx + outer_r * math.cos(t), cy + outer_r * math.sin(t)))
        for i in range(steps, -1, -1):
            t = start_rad + (end_rad - start_rad) * i / steps
            pts.append((cx + inner_r * math.cos(t), cy + inner_r * math.sin(t)))
        return pts

    def create_icon_image(self, active=False, color_offset=0, rotation=0.0):
        """Create system tray icon — segmented wheel design.

        Three arc segments (blue, red, black) with gaps between them.
        Active state shows full colors + gold center dot.
        Idle state shows muted greys.
        color_offset shifts which color sits in which position (chase animation).
        rotation rotates the entire wheel (in radians).
        """
        size = 64
        image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        cx, cy = size / 2, size / 2
        ring_width = 12
        outer_r = size / 2 - 1
        inner_r = outer_r - ring_width

        if getattr(self, 'snoozed', False):
            colors = self._WHEEL_SNOOZE
        elif active:
            colors = self._WHEEL_COLORS
        else:
            colors = self._WHEEL_IDLE

        n = len(colors)
        gap_rad = math.radians(8)
        arc_len = (2 * math.pi - gap_rad * n) / n

        # Shift colors by offset for chase animation (clockwise)
        shifted = [colors[(i - color_offset) % n] for i in range(n)]

        for i, color in enumerate(shifted):
            start = i * (arc_len + gap_rad) - math.pi / 2 + rotation  # 12 o'clock + rotation
            end = start + arc_len
            poly = self._arc_polygon(cx, cy, outer_r, inner_r, start, end)
            draw.polygon(poly, fill=color)

        # Gold center dot (visible when active)
        dot_r = 3
        if active and not getattr(self, 'snoozed', False):
            draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
                         fill=self._WHEEL_GOLD)

        return image

    def _request_icon_chase(self, reason):
        """Register a reason for the icon to animate. Starts animation if not running."""
        self._icon_anim_reasons.add(reason)
        if not self._icon_animating:
            self._start_icon_chase()

    def _release_icon_chase(self, reason):
        """Remove a reason for animation. Stops only when ALL reasons are gone."""
        self._icon_anim_reasons.discard(reason)
        if not self._icon_anim_reasons and self._icon_animating:
            self._stop_icon_chase()

    def _start_icon_chase(self):
        """Start the spinning color-chase animation on the tray icon."""
        self._icon_animating = True
        self._icon_chase_offset = 0
        self._icon_chase_counter = 0
        self._icon_rotation = 0.0
        self._icon_chase_tick()

    def _stop_icon_chase(self):
        """Stop the chase animation and show idle icon."""
        self._icon_animating = False
        if self._icon_chase_timer is not None:
            self._icon_chase_timer.cancel()
            self._icon_chase_timer = None
        self._icon_chase_offset = 0
        self._icon_rotation = 0.0
        if hasattr(self, 'tray_icon'):
            try:
                self.tray_icon.icon = self.create_icon_image(active=False)
            except OSError:
                pass

    def _icon_chase_tick(self):
        """Advance the spin + chase and schedule the next tick.

        Speed varies by active state:
        - recording:  fast spin + fast chase  (80ms tick, chase every 6 ticks ~480ms)
        - continuous: medium spin + medium chase (80ms tick, chase every 10 ticks ~800ms)
        - wake_word:  slow spin + slow chase  (120ms tick, chase every 14 ticks ~1680ms)
        """
        if not self._icon_animating:
            return

        # Determine speed from highest-priority active reason
        if 'recording' in self._icon_anim_reasons:
            tick_interval = ICON_TICK_FAST
            spin_step = ICON_SPIN_FAST
            chase_every = ICON_CHASE_FAST
        elif 'continuous' in self._icon_anim_reasons:
            tick_interval = ICON_TICK_MEDIUM
            spin_step = ICON_SPIN_MEDIUM
            chase_every = ICON_CHASE_MEDIUM
        else:  # wake_word or anything else
            tick_interval = ICON_TICK_SLOW
            spin_step = ICON_SPIN_SLOW
            chase_every = ICON_CHASE_SLOW

        # Spin
        self._icon_rotation += spin_step

        # Chase: shift colors every N ticks
        self._icon_chase_counter += 1
        if self._icon_chase_counter >= chase_every:
            self._icon_chase_counter = 0
            self._icon_chase_offset = (self._icon_chase_offset + 1) % 3

        if hasattr(self, 'tray_icon'):
            try:
                self.tray_icon.icon = self.create_icon_image(
                    active=True,
                    color_offset=self._icon_chase_offset,
                    rotation=self._icon_rotation)
            except OSError:
                pass  # transient WinError during icon handle swap — skip this frame

        self._icon_chase_timer = threading.Timer(
            tick_interval, self._icon_chase_tick)
        self._icon_chase_timer.daemon = True
        self._icon_chase_timer.start()
    
    def open_settings(self):
        """Open settings window"""
        try:
            if not hasattr(self, '_settings_qt'):
                from samsara.ui.settings_qt import SettingsQt
                self._settings_qt = SettingsQt(self)
            self._settings_qt.show()
        except Exception as e:
            print(f"[SETTINGS] Error opening settings: {e}")
    
    def open_voice_training(self):
        """Open voice training window"""
        try:
            self.voice_training_window.show()
        except Exception as e:
            print(f"Error opening voice training: {e}")

    def open_mic_setup_guide(self):
        """Open the guided mic setup wizard."""
        if self.mic_setup_wizard is not None:
            self.mic_setup_wizard.show()

    def open_ava_guide(self):
        """Open the Ava setup guide."""
        if self.ava_guide is not None:
            self.ava_guide.show()

    def show_tutorial(self):
        """Show the interactive tutorial window. Safe to call from any thread."""
        def _open():
            try:
                from samsara.ui.tutorial_qt import show_tutorial
                show_tutorial(self)
            except Exception as _e:
                print(f"[TUTORIAL] Failed to open tutorial: {_e}")
        self._schedule_ui(_open)

    def open_history(self):
        """Open dictation history window"""
        try:
            if not hasattr(self, '_history_qt'):
                from samsara.ui.history_qt import HistoryQt
                self._history_qt = HistoryQt(self)
            self._history_qt.show()
        except Exception as e:
            print(f"[HISTORY] Error opening history: {e}")

    def open_wake_word_debug(self):
        """Open wake word debug/test window"""
        try:
            self.wake_word_debug_window.show()
        except Exception as e:
            print(f"Error opening wake word debug: {e}")

    def snooze_listening(self, minutes=None):
        """Temporarily pause all listening for the given duration.

        Args:
            minutes: Duration in minutes, or None for indefinite snooze.
        """
        if self.snoozed:
            return  # already snoozed

        # Remember what was actively running so we can restore it
        self._snooze_prior_mode_state = {
            'mode': self.config.get('mode', 'hold'),
            'continuous_active': self.continuous_active,
            'wake_word_active': self.wake_word_active,
            'wake_word_enabled': self.config.get('wake_word_enabled', False),
            'recording': self.recording,
            'toggle_active': getattr(self, 'toggle_active', False),
        }

        # Stop any active audio capture
        if self.recording:
            self.stop_recording()
        if self.continuous_active:
            self.stop_continuous_mode()
        if self.wake_word_active:
            self.stop_wake_word_mode()

        self.snoozed = True
        self.play_sound("stop")

        # Update listening indicator to idle + snoozed
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)
            self._schedule_ui(self.listening_indicator.set_snoozed, True)

        # Schedule auto-resume
        if minutes is not None:
            import datetime
            self._snooze_resume_time = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
            resume_str = self._snooze_resume_time.strftime("%H:%M")
            print(f"[SNOOZE] Listening snoozed for {minutes} min (resumes at {resume_str})")

            self._snooze_timer = threading.Timer(minutes * 60, self._on_snooze_expire)
            self._snooze_timer.daemon = True
            self._snooze_timer.start()
        else:
            self._snooze_resume_time = None
            print("[SNOOZE] Listening snoozed until manually resumed")

        # Update tray tooltip
        self._update_snooze_tooltip()

    def _update_snooze_tooltip(self):
        """Set tray icon tooltip to reflect snooze state."""
        if not hasattr(self, 'tray_icon'):
            return
        if self.snoozed:
            if self._snooze_resume_time is not None:
                resume_str = self._snooze_resume_time.strftime("%H:%M")
                self.tray_icon.title = f"Samsara - Snoozed (resumes at {resume_str})"
            else:
                self.tray_icon.title = "Samsara - Snoozed (until resumed)"
        else:
            self.tray_icon.title = f"Samsara - {self._get_mode_display()}"

    def _on_snooze_expire(self):
        """Called by the snooze timer when duration elapses."""
        self._snooze_timer = None
        self.resume_listening()

    def resume_listening(self):
        """Cancel snooze and restore the previously active listening mode."""
        if not self.snoozed:
            return

        # Cancel pending timer if resuming early
        if self._snooze_timer is not None:
            self._snooze_timer.cancel()
            self._snooze_timer = None

        self.snoozed = False
        self._snooze_resume_time = None
        print("[SNOOZE] Listening resumed")

        # Clear snoozed state on indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_snoozed, False)

        # Restore prior mode state
        prior = self._snooze_prior_mode_state or {}
        mode = prior.get('mode', self.config.get('mode', 'hold'))

        # Hold/toggle: ACE engine ring provides rolling pre-buffer (ACE-03).
        # No separate prebuffer stream to restart.

        # Restart continuous mode if it was active
        if prior.get('continuous_active') and mode == 'continuous':
            self.start_continuous_mode()

        # Restart wake word listener if it was enabled before snooze
        if prior.get('wake_word_enabled') and not self.wake_word_active:
            self.start_wake_word_mode()

        self._snooze_prior_mode_state = None
        self.play_sound("start")

        # Restore tray tooltip
        self._update_snooze_tooltip()

    def calibrate_echo_cancellation(self):
        """Run AEC lag calibration on a worker thread (blocking sd.play/rec)."""
        if self._is_audio_capture_active():
            print("[AEC-CAL] Cannot calibrate while audio capture is active. "
                  "Stop dictation and try again.")
            return

        def _run():
            print("[AEC-CAL] Starting calibration...")
            try:
                result = self.echo_canceller.calibrate_lag(
                    mic_device_index=self.config.get('microphone'),
                    mic_rate=self.capture_rate,
                )
            except Exception as e:
                print(f"[AEC-CAL] Calibration failed with exception: {e}")
                return

            print(f"[AEC-CAL] Result: {result}")
            if result['success']:
                lag = result['lag_ms']
                print(
                    f"[AEC-CAL] To apply this value, edit config.json: "
                    f'"echo_cancellation": {{"latency_ms": {lag:.1f}}}'
                )
            else:
                print(f"[AEC-CAL] Calibration not reliable: {result['message']}")

        threading.Thread(target=_run, daemon=True, name="aec-calibrate").start()

    def _dispatch_command(self, _cmd):
        """Re-execute self._last_command_name via the normal dispatch path."""
        self.command_executor.process_text(self._last_command_name, self)

    def repeat_last_command(self):
        """Re-execute the last repeatable command ("repeat" / "again")."""
        if self._last_command is None:
            print("[REPEAT] No repeatable command in history.")
            return
        print(f"[REPEAT] {self._last_command_name}")
        self._dispatch_command(self._last_command)

    def toggle_listening_indicator(self):
        """Toggle the listening indicator overlay on/off and persist to config."""
        enabled = not self.config.get('listening_indicator_enabled', False)
        with self._config_lock:
            self.config['listening_indicator_enabled'] = enabled
            self.save_config()
        if enabled:
            self._schedule_ui(self.listening_indicator.show)
        else:
            self._schedule_ui(self.listening_indicator.hide)

    def show_cheat_sheet(self):
        """Show the command reference overlay."""
        self._schedule_ui(self.cheat_sheet.show)

    def hide_cheat_sheet(self):
        """Hide the command reference overlay."""
        self._schedule_ui(self.cheat_sheet.hide)

    def toggle_cheat_sheet(self):
        """Toggle the command reference overlay."""
        self._schedule_ui(self.cheat_sheet.toggle)

    def create_tray_icon(self):
        """Create the Qt system tray icon and block the main thread.

        Creates SamsaraTrayQt on the samsara-qt thread via QTimer.singleShot,
        then keeps the main thread alive with a lightweight sleep loop.
        quit_app() sets self._running = False then calls os._exit(0).
        """
        from PySide6.QtCore import QTimer
        qt_app = __import__('PySide6.QtWidgets', fromlist=['QApplication']).QApplication.instance()

        def _create():
            self.tray_icon = _SamsaraTrayQt(self)

        QTimer.singleShot(0, qt_app, _create)
        QTimer.singleShot(0, qt_app, self.show_main_window)

        while self._running:
            import time as _t
            _t.sleep(0.2)
    
    def switch_microphone_and_refresh(self, mic_id):
        """Switch microphone and refresh the tray menu"""
        self.switch_microphone(mic_id)
    
    def open_config_folder(self):
        """Open the config folder"""
        open_file_or_folder(self.config_path.parent)

    def open_main_log(self):
        """Open the main log file in default text editor"""
        if LOG_FILE.exists():
            open_file_or_folder(LOG_FILE)
        else:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(None, "Log File", "No log file found yet.")

    def open_log_file(self):
        """Open the Samsara log file in Notepad (voice command target)."""
        if LOG_FILE.exists():
            subprocess.Popen(["notepad.exe", str(LOG_FILE)])
        else:
            print("[LOG] No log file found.")

    def open_voice_training_log(self):
        """Open the voice training log file"""
        log_file = LOG_DIR / 'voice_training.log'
        if log_file.exists():
            open_file_or_folder(log_file)
        else:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(None, "Log File", "No voice training log file found yet.")
    
    def quit_app(self):
        """Exit the application"""
        print("[EXIT] Shutting down Samsara...")

        # Signal background threads (e.g. stream-health monitor) to stop
        self._running = False

        # Stop config file watcher
        try:
            if self._config_watcher is not None:
                self._config_watcher.stop()
        except Exception:
            pass

        # Stop icon chase animation timer
        try:
            self._stop_icon_chase()
        except:
            pass
        
        try:
            if self.continuous_active:
                self.stop_continuous_mode()
        except:
            pass
        
        try:
            if self.wake_word_active:
                self.stop_wake_word_mode()
        except:
            pass

        try:
            self._stop_gesture_lane()
        except Exception:
            pass

        # Stop key macro manager (releases any held keys)
        try:
            if hasattr(self, 'key_macro_manager') and self.key_macro_manager:
                self.key_macro_manager.stop()
        except:
            pass

        # Stop notification manager
        try:
            if hasattr(self, 'notification_manager') and self.notification_manager:
                self.notification_manager.stop()
        except:
            pass

        # Stop alarm manager
        try:
            if hasattr(self, 'alarm_manager') and self.alarm_manager:
                self.alarm_manager.stop()
        except:
            pass

        # Stop ACE engine (deactivates consumer, flushes debug WAV if any)
        try:
            if hasattr(self, '_ace_engine') or hasattr(self, '_dictation_consumer'):
                self._stop_ace_engine()
        except:
            pass

        # Stop echo cancellation
        try:
            if hasattr(self, 'echo_canceller'):
                self.echo_canceller.stop()
        except:
            pass

        # Cancel snooze timer
        try:
            if self._snooze_timer is not None:
                self._snooze_timer.cancel()
                self._snooze_timer = None
        except:
            pass

        # Destroy listening indicator
        try:
            if hasattr(self, 'listening_indicator'):
                self.listening_indicator.destroy()
        except:
            pass

        # Destroy command cheat sheet
        try:
            if hasattr(self, 'cheat_sheet'):
                self.cheat_sheet.destroy()
        except:
            pass

        # Destroy show-numbers layered overlay
        try:
            from plugins.commands.show_numbers import _destroy_overlay_completely
            _destroy_overlay_completely()
        except Exception:
            pass

        # Shut down TTS coordinator + engine before the earcon stream closes
        try:
            if getattr(self, 'audio_coordinator', None) is not None:
                self.audio_coordinator.shutdown()
        except Exception:
            pass
        try:
            if getattr(self, 'tts_engine', None) is not None:
                self.tts_engine.shutdown()
        except Exception:
            pass

        # Stop output device watcher before closing the stream it manages
        try:
            stop_evt = getattr(self, '_output_watcher_stop', None)
            if stop_evt is not None:
                stop_evt.set()
        except Exception:
            pass

        # Stop persistent sound stream
        try:
            self.stop_sound_stream()
        except:
            pass

        # Close main hub window (saves geometry to config)
        try:
            if getattr(self, 'main_window', None) is not None:
                self.main_window.close()
        except:
            pass

        # Close persistent history database
        try:
            if getattr(self, 'history_db', None) is not None:
                self.history_db.close()
        except:
            pass

        # Stop keyboard listener
        try:
            self.keyboard_listener.stop()
        except:
            pass

        # Stop Win32 mouse hook (Mouse 4/5 command mode)
        try:
            if getattr(self, '_mouse_hook', None) is not None:
                self._mouse_hook.stop()
        except Exception:
            pass

        # Release the CapsLock hook so the OS resumes normal toggle behavior
        try:
            if getattr(self, '_capslock_hook', None) is not None:
                keyboard.unhook(self._capslock_hook)
                self._capslock_hook = None
        except Exception:
            pass

        # Stop tray icon (do this before GUI cleanup)
        try:
            self.tray_icon.stop()
        except:
            pass
        
        # Flush Ava alias use-count to disk before exit
        try:
            _ava_corrections.flush_pending()
        except Exception:
            pass

        # Flush debounced command stats and hint counters so counts inside
        # the 5-second coalesce window are not lost on clean shutdown.
        try:
            flush_command_stats()
        except Exception:
            pass
        try:
            if hasattr(self, 'hints') and self.hints is not None:
                self.hints.shutdown()
        except Exception:
            pass

        # Force exit — bypasses any remaining thread cleanup but guarantees
        # termination even if a background thread or Qt modal is blocking.
        print("[EXIT] Goodbye!")
        os._exit(0)

if __name__ == "__main__":
    # Console is already hidden at top of file
    _DIAG_MAIN_T = time.perf_counter()
    print(f"[BOOT-DIAG] __main__: entry (since sounddevice import: {(_DIAG_MAIN_T - _POST_SD_T)*1000:.0f}ms)", flush=True)

    # Guard against double-launch. Must run before the splash / audio starts
    # so a second invocation exits cleanly without grabbing resources.
    _t = time.perf_counter()
    _acquire_instance_lock()
    _dt = (time.perf_counter() - _t) * 1000
    print(f"[BOOT-DIAG] instance lock (_check_single_instance): {_dt:.0f}ms", flush=True)
    if _dt > 5000:
        print(f"[BOOT-DIAG] SLOW STEP: instance lock {_dt:.0f}ms", flush=True)

    # Show splash screen during startup
    _t = time.perf_counter()
    from samsara.ui.splash_qt import SplashScreenQt
    splash = SplashScreenQt()
    _dt = (time.perf_counter() - _t) * 1000
    print(f"[BOOT-DIAG] splash init (SplashScreenQt): {_dt:.0f}ms", flush=True)
    if _dt > 5000:
        print(f"[BOOT-DIAG] SLOW STEP: splash init {_dt:.0f}ms", flush=True)
    splash.set_status("Initializing...")

    app = None
    try:
        app = DictationApp(splash)
    except Exception as e:
        splash.close()
        raise e
    finally:
        # os._exit(0) in quit_app bypasses this block, which is correct —
        # quit_app already releases the hook explicitly before exiting.
        # This finally only fires on an exception or KeyboardInterrupt that
        # propagates to __main__ without going through quit_app, ensuring
        # CapsLock is always returned to the OS on abnormal exits.
        if app is not None:
            try:
                app._uninstall_capslock_hook()
            except Exception:
                pass
