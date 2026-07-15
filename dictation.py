import copy
import os
import shutil
import string
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
        _co_hr = ole32.CoInitializeEx(None, 0)
        try:
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
        finally:
            # S_OK (0) and S_FALSE (1) both mean THIS call initialized COM
            # on this thread and owns a reference that must be balanced.
            # RPC_E_CHANGED_MODE (0x80010106) means COM was already
            # initialized here in an incompatible mode -- we don't own a
            # reference and must not uninitialize it (comparing only
            # against 0/1 already excludes it; as a raw ctypes int return
            # value it won't equal either). Called every 2s from
            # _watch_output_device for the app's whole lifetime, so leaving
            # this unbalanced leaked one COM reference per call.
            if _co_hr in (0, 1):
                ole32.CoUninitialize()
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
    except Exception as e:
        logger.debug(f"Could not hide console window: {e}")

# _hide_console_now()  # TEMPORARILY DISABLED for debug — uncomment when done testing

# ============================================================================
# Single Instance Check - Prevent multiple instances from running
# ============================================================================

def _is_samsara_process(pid: int) -> bool:
    """True if `pid` is alive AND looks like a Samsara process.

    Liveness alone isn't enough: PIDs get reused by Windows, so a lock file
    naming a PID that's alive right now could belong to a completely
    unrelated process that started after the real Samsara process (which
    wrote that PID) died or was killed. Checks the process image name --
    "Samsara.exe" for a frozen build, or a python*.exe running dictation.py
    for a dev-mode instance.

    Prefers psutil (already a project dependency); falls back to raw
    ctypes OpenProcess + QueryFullProcessImageNameW if psutil isn't
    importable for some reason.
    """
    try:
        import psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            name = (proc.name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        except Exception:
            return False
        if name == "samsara.exe":
            return True
        if name.startswith("python"):
            try:
                cmdline = " ".join(proc.cmdline()).lower()
            except Exception:
                return False
            return "dictation.py" in cmdline
        return False

    if sys.platform != 'win32':
        # Can't verify identity without psutil off Windows -- assume it's
        # real rather than risk stealing a live process's lock.
        return True

    import ctypes
    import ctypes.wintypes as wt

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # No handle -- process is gone (or inaccessible; treat the same,
        # since we can't confirm it's Samsara either way).
        return False
    try:
        buf_len = wt.DWORD(260)
        buf = ctypes.create_unicode_buffer(260)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len))
        if not ok:
            return False
        image_name = Path(buf.value).name.lower()
        return image_name == "samsara.exe" or image_name.startswith("python")
    finally:
        kernel32.CloseHandle(handle)


def _steal_stale_lock_if_any(lock_file_path) -> None:
    """If lock_file_path exists and names a dead or non-Samsara PID, delete
    it. If it names a live Samsara process, log and exit(0) -- never hang;
    this whole check is non-blocking liveness/identity inspection, no wait.

    Runs before the OS-level lock acquisition below, which still does the
    actual atomic locking -- this only turns "some stale file is sitting
    there from a hard kill" into a clean steal instead of a false
    already-running refusal.
    """
    if not lock_file_path.exists():
        return
    try:
        recorded_pid = int(lock_file_path.read_text().strip())
    except (OSError, ValueError):
        # Unreadable/empty/corrupt -- can't belong to a live instance we'd
        # recognize; treat as stale.
        logger.info("[LOCK] lock file unreadable, stealing")
        try:
            lock_file_path.unlink()
        except OSError as e:
            logger.debug(f"[LOCK] could not remove unreadable lock file: {e}")
        return

    if _is_samsara_process(recorded_pid):
        logger.warning(f"[WARN] Samsara is already running (PID: {recorded_pid})")
        sys.exit(0)

    logger.info(f"[LOCK] stale lock from PID {recorded_pid}, stealing")
    try:
        lock_file_path.unlink()
    except OSError as e:
        logger.debug(f"[LOCK] could not remove stale lock file: {e}")


def _check_single_instance():
    """
    Ensure only one instance of Samsara is running.
    Windows uses a process-lifetime named mutex; Unix-like systems retain the
    existing file lock. Returns the retained handle or exits if another
    instance owns the same profile identity.

    The normal profile uses a fixed identity. When SAMSARA_HOME_DIR is
    explicitly set (temp-profile tooling, the tray's "Preview First-Run"
    dev action), the identity is derived from that path, so a preview
    instance never collides with the primary instance.
    """
    if sys.platform == 'win32':
        from samsara.single_instance import (
            AlreadyRunningError,
            acquire_single_instance_mutex,
        )
        try:
            return acquire_single_instance_mutex()
        except AlreadyRunningError:
            # Keep this exact marker: frozen smoke tooling recognizes it as
            # the expected fast refusal when a real instance is already up.
            logger.warning("[WARN] Samsara is already running")
            sys.exit(0)
        except Exception as e:
            # Preserve the existing fail-open startup policy. A broken
            # single-instance check must not make an accessibility app
            # impossible to launch.
            logger.warning(f"[WARN] Could not check for existing instance: {e}")
            return None

    from pathlib import Path
    import tempfile

    home_override = os.environ.get("SAMSARA_HOME_DIR")
    if home_override:
        import hashlib
        # normcase + realpath so equivalent paths (different case, trailing
        # slash, relative vs absolute, 8.3 vs long form) hash identically --
        # otherwise two preview launches pointed at "the same" dir by a
        # human typing it two different ways would get two different locks
        # and could run concurrently against one profile.
        normalized = os.path.normcase(os.path.realpath(home_override))
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        lock_name = f"samsara-{digest}.lock"
    else:
        lock_name = "samsara.lock"
    lock_file_path = Path(tempfile.gettempdir()) / lock_name
    _steal_stale_lock_if_any(lock_file_path)

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
                    logger.warning(f"[WARN] Samsara is already running (PID: {other_pid})")
                except Exception as e:
                    logger.debug(f"Could not read other instance PID: {e}")
                    logger.warning("[WARN] Samsara is already running")
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
                    logger.warning(f"[WARN] Samsara is already running (PID: {other_pid})")
                except Exception as e:
                    logger.debug(f"Could not read other instance PID: {e}")
                    logger.warning("[WARN] Samsara is already running")
                sys.exit(0)
    except Exception as e:
        # If locking fails for any reason, log but continue
        # (better to have duplicate instances than no instances)
        logger.warning(f"[WARN] Could not check for existing instance: {e}")
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

_RecordingOwnership = collections.namedtuple(
    '_RecordingOwnership',
    'is_command is_ava command_ghost ava_ghost',
)
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

def _create_whisper_model(*args, **kwargs):
    from faster_whisper import WhisperModel
    return WhisperModel(*args, **kwargs)


if sys.stdout is not None:
    sys.stdout.write(f"[PRE-LOG] +{(time.perf_counter()-_POST_SD_T)*1000:.0f}ms (after faster_whisper)\n")
    sys.stdout.flush()

from PIL import Image, ImageDraw
try:
    from samsara.ui.tray_qt import SamsaraTrayQt as _SamsaraTrayQt
except Exception as _tray_err:
    _SamsaraTrayQt = None
    print(f"[INIT] SamsaraTrayQt unavailable: {_tray_err}")
import json
from pathlib import Path
# Qt 6 declares PER_MONITOR_AWARE_V2 when QApplication is constructed. A
# second process-wide SetProcessDpiAwareness call here caused Qt's later call
# to fail with ERROR_ACCESS_DENIED on every healthy startup. Show Numbers uses
# explicit thread-level PMv2 contexts for its native geometry/click work, so
# process awareness has one owner and mixed-DPI coordinates stay deterministic.

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
from samsara.smart_corrections import smart_correct, warm_up as smart_corrections_warm_up
from samsara.formatting_tokens import apply_formatting_tokens_if_enabled
from samsara import diagnostics
from samsara import benchmark_store
from samsara import languages as _languages
from samsara.history import HistoryManager
from samsara.history_store import HistoryStore
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
    DEFAULT_CONTINUOUS_COMMIT_TRIGGER, DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
    DEFAULT_CONTINUOUS_MAX_BUFFER_S,
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
from samsara import audio_ducking
from samsara import wake_profiles
from samsara.clipboard import paste_with_preservation
from samsara.wake_detector import WakeWordDetector
from samsara.handlers import _get_foreground_exe_lower, _get_foreground_hwnd
from samsara.runtime import thread_registry
from samsara.session_modes import (
    SessionMode, SessionModeManager, UtteranceSignals, CommandDispatchResult,
    HandsFreeCommandMatch, PendingTextPolicy, normalize_utterance,
    GLOBAL_SESSION_EXIT_PHRASES,
)

# Commands with special pending-text behavior inside the combined hands-free
# lane. These are checked first; any other enabled command or user macro may
# still execute when it consumes the COMPLETE silence-bounded utterance (see
# CommandExecutor.find_exact_command). That exact-only fallback makes short
# hands-free commands useful without stealing command phrases embedded in
# ordinary prose. ``literal ...`` remains the explicit dictation escape.
_HANDS_FREE_PRESERVE_COMMANDS = frozenset({
    "scroll up", "scroll down", "scroll up a little", "scroll down a little",
    "scroll up medium", "scroll down medium", "scroll up high", "scroll down high",
    "scroll up fast", "scroll down fast", "scroll left", "scroll right",
    "scroll left a little", "scroll right a little", "page up", "page down",
    "scroll page up", "scroll page down", "up one page", "down one page",
    "scroll to top", "scroll to bottom", "go to top", "go to bottom",
    "top of page", "bottom of page", "jump to top", "jump to bottom",
    "show numbers", "show", "refresh numbers", "update numbers",
    "show windows", "label windows", "window labels",
    "read windows", "list windows", "what windows",
    "maximize", "minimize",
})
_HANDS_FREE_COMMIT_COMMANDS = frozenset({
    "submit", "enter", "escape", "press tab", "next field", "previous field",
    "back field", "back tab", "switch window", "switch app", "other window",
    "next tab", "previous tab", "go back", "go forward",
})
_HANDS_FREE_COMMIT_PREFIXES = (
    "focus ", "switch to ", "window switch ", "go to window ",
    "click ", "tap ",
)

_PENDING_CANCEL_UTTERANCES = frozenset({"nevermind", "never mind"})
_UNDO_TARGET_UNSET = object()


def _is_pending_cancel_utterance(text: str) -> bool:
    """True only when the complete utterance is a pending-state cancel."""
    normalized = " ".join((text or "").strip().lower().split())
    normalized = normalized.strip(string.punctuation + " ")
    return normalized in _PENDING_CANCEL_UTTERANCES


# Minimum gap (ms) between AEC loopback open and ACE mic open.
# The Arctis Nova Pro Wireless WASAPI driver stalls 10-18 s when a second
# PortAudio client opens the same physical device within ~20 ms of the first.
# 600 ms is a conservative safe value measured empirically.
_AEC_TO_MIC_MIN_GAP_MS = 600

_WAKE_PRIMER_DELAY = 0.12
_WAKE_SESSION_TIMEOUT_S   = 10.0            # inactivity ends the open-ended wake session
_WAKE_SESSION_CHUNK_GAP_S = 1.0             # per-utterance VAD silence gap within a session
_WAKE_SESSION_SEND_WORDS  = ['over', 'send'] # default send terminators that finalize a wake session

# --- Whisper hallucination prevention ("Gate and Reset" architecture) ---
# Causal fixes (input/model level) replacing the old output-text-only
# heuristic, which is demoted to a backstop (_is_hallucinated_segments).
# Per ARC tribunal verdict, arc_20260701_143252.md.
_NO_SPEECH_THRESHOLD = 0.6   # faster-whisper native: per-segment silence-probability cutoff
_LOGPROB_THRESHOLD   = -1.0  # faster-whisper native: log_prob_threshold (avg log-prob floor)
_COMPRESSION_RATIO_THRESHOLD = 2.4
                             # faster-whisper's own built-in compression_ratio_threshold default
                             # -- never explicitly passed as a transcribe() kwarg anywhere in this
                             # file (see get_transcription_params), so there's no config value to
                             # read back; this matches both faster-whisper's real internal default
                             # and samsara/diagnostics.py's classify() heuristic. Used by
                             # _is_quality_exhausted (2026-07-10): when faster-whisper's own
                             # temperature fallback ladder exhausts every rung and still can't
                             # meet log_prob_threshold/this compression ceiling, it returns the
                             # final failed attempt anyway rather than nothing -- an 11.7s blank
                             # hotkey hold on 2026-07-10 delivered "Thank you for watching!" this
                             # exact way (every temp 0.0-1.0 failed log_prob_threshold, then
                             # compression_ratio hit 7.125 at temp 0.8) because nothing downstream
                             # checked these signals before delivering the text.
_GATE_MAX_BUFFER_S   = 8.0   # only buffers this short or shorter are VAD-gated; longer
                             # real dictation bypasses the gate entirely (no added latency).
                             # Raised 3.0->8.0: 3-6s near-silent/whisper holds were bypassing
                             # the gate and producing phantom "Thank you for watching" text.
                             # NOTE (2026-07-10): that fix only pushed the exposure window out,
                             # it didn't close it -- an 11.7s hold reproduced the identical bug.
                             # _is_quality_exhausted (below) is the durable, length-independent
                             # fix; this constant is deliberately NOT raised again (see its own
                             # comment on why the gate must stay latency-free for real dictation).
_GATE_MIN_CONTIG_MS  = 150   # minimum CONTIGUOUS high-confidence speech run required to pass
_GATE_VAD_PROB       = 0.45  # Silero speech-probability threshold for the contiguous-run gate
_FADE_MS             = 50    # linear fade-in/out applied to hotkey buffers, kills the
                             # press/release click transient before it can reach VAD or Whisper
_GATE_HEAD_GRACE_CLICK_PAD_MS = 60
                             # FIX (2026-07-10 hotkey word-loss investigation, "head grace"):
                             # the start earcon (measured duration, see start_recording) plus
                             # this fixed pad for the mechanical key-press click transient that
                             # clusters right after it define a KNOWN, Samsara-generated noisy
                             # span at the head of the hotkey buffer. _buffer_has_contiguous_
                             # speech's head_grace_ms parameter tells the gate's scan not to let
                             # a low reading inside that known span break a contiguous run of
                             # real speech starting at or just past it. Does NOT touch/edit any
                             # audio samples -- the earcon-span buffer-muting approach was
                             # explicitly retracted; this only widens the gate's tolerance.


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
                    logger.debug(f"{button_name} not in pynput.Key -- falling back to VK code")
                try:
                    from pynput.keyboard import KeyCode
                    return KeyCode.from_vk(0x6F + n)   # F1=0x70 → Fn=0x6F+n
                except Exception as e:
                    logger.debug(f"KeyCode.from_vk fallback failed for {button_name}: {e}")
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


def _fade_edges(audio, sample_rate, fade_ms=_FADE_MS):
    """Apply a linear fade-in/out to the first/last fade_ms of audio.

    Kills the high-energy transient from the physical hotkey press/release,
    which Whisper otherwise hears as "click click click". Returns a new
    array; does not modify the input in place.

    Clamps the fade length to at most half the buffer so in/out ramps never
    overlap on a very short buffer (the hotkey path already skips anything
    under 0.51s, but this stays safe regardless of caller).
    """
    n = len(audio)
    fade_samples = int(sample_rate * fade_ms / 1000.0)
    fade_samples = min(fade_samples, n // 2)
    if fade_samples <= 0:
        return audio
    out = np.asarray(audio, dtype=np.float32).copy()
    ramp_in = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
    ramp_out = np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
    out[:fade_samples] *= ramp_in
    out[-fade_samples:] *= ramp_out
    return out


def _dump_hotkey_buffer(audio, sample_rate) -> None:
    """Opt-in diagnostic (config: debug.dump_hotkey_buffers, off by
    default): write the exact assembled hotkey buffer -- post-prepend,
    PRE-fade -- to ~/.samsara/debug/hotkey_<timestamp>.wav, so the raw
    seam (unmasked by the 50ms edge fade) can be listened to directly.
    2026-07-10 hotkey word-loss investigation. Never raises -- a dump
    failure must not affect the transcription it's diagnosing."""
    try:
        out_dir = Path.home() / ".samsara" / "debug"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        path = out_dir / f"hotkey_{ts}.wav"
        pcm_int16 = np.clip(
            np.asarray(audio, dtype=np.float32) * 32767.0, -32768, 32767
        ).astype(np.int16)
        with wave.open(str(path), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm_int16.tobytes())
        logger.debug(f"[SEAM] Dumped hotkey buffer -> {path}")
    except Exception as e:
        logger.debug(f"[SEAM] hotkey buffer dump failed (non-fatal): {e}")


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


# Well-known Whisper hallucination strings that appear on near-silent audio,
# across languages -- Signature E below. Language-independent by
# construction (checked as a plain case-insensitive substring match, no
# transcription-language dependency), so it extends _is_hallucinated_segments
# rather than needing a separate per-language check. "amara.org" alone
# catches variants not explicitly listed (Amara.org crowd-subtitles the
# phrase on many different source videos/languages).
#
# 2026-07-10: added the ENGLISH "thank you for watching" family -- every
# non-English variant of this exact staple (Japanese/Chinese/Ukrainian/
# Spanish below) was already covered, but the English original was missing
# entirely. An 11.7s blank hotkey hold delivered "Thank you for watching!"
# verbatim because of that gap (see _is_quality_exhausted for the other half
# of that fix -- the decode had ALSO exhausted every quality threshold).
# Matching is via the existing lowercase-substring + dominance-ratio check
# below (case/punctuation-insensitive by construction: text is lowercased
# and only whitespace-normalized, so trailing "!"/"." on the transcript
# doesn't prevent the bare phrase from still dominating the ratio). No other
# hallucination staples added here -- stay scoped to this exact family.
_HALLUCINATION_STRING_BLACKLIST = (
    "thank you for watching",
    "thanks for watching",
    "untertitel der amara.org-community",
    "sous-titrage st' 501",
    "ご視聴ありがとうございました",
    "字幕由amara.org社区提供",
    "дякую за перегляд",
    "gracias por ver el video",
    "amara.org",
)


def _is_hallucinated_segments(seg_list, text):
    """True if the transcription shows Whisper's degenerate-repetition signature.

    BACKSTOP ONLY. The primary hallucination defenses are causal and run
    before/during transcription: faster-whisper's native no_speech_threshold/
    log_prob_threshold, the per-press condition_on_previous_text=False
    conversation-context reset, the contiguous-confidence VAD gate on short
    buffers, and the click-fade (see module-level _NO_SPEECH_THRESHOLD /
    _GATE_* / _FADE_MS constants and _buffer_has_contiguous_speech). This
    output-text heuristic is a cheap last-resort net for whatever slips
    through those, not the primary defense -- avoid extending the
    repetition-based signatures (A/B/D); the fixed-string blacklist
    (Signature E) is a deliberate, bounded exception since those exact
    strings are never legitimate dictation regardless of language.

    Uses telemetry Whisper already computed; no re-inference. Conservative:
    only fires on clear signatures so real speech is never dropped."""
    t = (text or "").strip()
    if not t:
        return False
    # Signature E: well-known multilingual Whisper hallucination strings
    # (subtitle-crowdsourcing credits that leak out on near-silent audio).
    # Language-independent -- checked regardless of the configured
    # dictation language.
    #
    # DOMINANCE, NOT PRESENCE: a legitimate utterance can simply MENTION one
    # of these strings ("I was reading about the amara.org community") --
    # discarding the whole transcription on bare substring presence would
    # eat real speech (the bare "amara.org" entry is the worst case for
    # this). Gate on the longest matching phrase covering >=80% of the
    # (whitespace-normalized) transcript instead -- that's the phrase
    # constituting substantially the whole output, not an incidental
    # mention. Below that ratio it's incidental and falls through to the
    # repetition signatures (A/B/D) below. Never scrub/mutate the text: a
    # mid-string removal would corrupt legitimate surrounding speech --
    # gate-or-pass is the only safe move, so this stays a pure boolean check.
    t_lower = t.lower()
    t_norm = re.sub(r'\s+', ' ', t_lower).strip()
    if t_norm:
        best_len = 0
        for phrase in _HALLUCINATION_STRING_BLACKLIST:
            phrase_norm = re.sub(r'\s+', ' ', phrase).strip()
            if phrase_norm and phrase_norm in t_norm:
                best_len = max(best_len, len(phrase_norm))
        if best_len and best_len / len(t_norm) >= 0.80:
            return True
    # Signature A: high compression ratio on any segment (repetition compresses hard).
    # Whisper's own reject threshold is 2.4; we use a slightly higher 3.0 to stay
    # conservative and avoid touching borderline-but-real speech.
    for s in seg_list:
        cr = getattr(s, "compression_ratio", None)
        if cr is not None and cr > 3.0:
            return True
    # Signature B: low lexical diversity repetition (e.g. "click click click click").
    # Strip surrounding punctuation before comparing words so "click," "click."
    # "click!" count as the same repeated word instead of inflating diversity.
    words = [w.strip(string.punctuation) for w in t.lower().split()]
    words = [w for w in words if w]
    if len(words) >= 4:
        uniq = len(set(words))
        if uniq <= max(2, len(words) // 4):
            return True
    # Signature D: the ENTIRE transcription is 2-3 identical tokens (e.g.
    # "click click", "beep beep beep"). Too short to trip Signature B's
    # >=4-word check. An embedded mention inside real speech ("I heard a
    # click click sound") is untouched -- this only fires when the repeat
    # IS the whole transcription, not part of a longer one.
    #
    # CORROBORATION REQUIRED: a bare 2-3 token whole-utterance repeat is
    # NOT on its own a reliable hallucination signal -- real emphatic
    # speech ("no no", "stop stop", "yes yes yes") looks identical at the
    # text level. This must only fire when acoustically corroborated: every
    # segment's no_speech_prob > 0.5 (near-silence). A user actually saying
    # "no no" into a live mic produces LOW no_speech_prob and now passes
    # through untouched; a phantom "click click" from a near-silent buffer
    # keeps HIGH no_speech_prob and is still caught. Empty seg_list or any
    # segment missing no_speech_prob telemetry means there's nothing to
    # corroborate with -- never fire in that case. Eating real speech is
    # worse than letting a rare hallucination through.
    if 2 <= len(words) <= 3 and len(set(words)) == 1 and seg_list:
        nsp_values = [getattr(s, "no_speech_prob", None) for s in seg_list]
        if all(v is not None and v > 0.5 for v in nsp_values):
            return True
    # Signature C: very high no_speech_prob across all segments AND short output
    # (near-silent buffer that still emitted a token or two).
    if seg_list:
        nsp = [getattr(s, "no_speech_prob", 0.0) or 0.0 for s in seg_list]
        if nsp and min(nsp) > 0.8 and len(words) <= 3:
            return True
    return False


def _is_quality_exhausted(seg_list, transcribe_params):
    """True if faster-whisper's OWN quality gate never actually passed for
    this decode -- its temperature fallback ladder exhausted every rung
    (0.0, 0.2, 0.4, ... up to 1.0 by default) still failing log_prob_
    threshold or the compression-ratio ceiling, and it returned the final
    failed attempt anyway rather than nothing. See module-level comment on
    _COMPRESSION_RATIO_THRESHOLD for the production incident this fixes.

    Distinct from _is_hallucinated_segments: that's a backstop against
    KNOWN hallucination TEXT signatures (fixed phrases, repetition) --
    this is a pure QUALITY-SIGNAL check, independent of what the text
    actually says. A transcription can look perfectly plausible and still
    be untrustworthy if Whisper itself never found a decode confident
    enough to stop early on.

    Reads log_prob_threshold from transcribe_params -- the SAME dict
    actually passed to model.transcribe() for this call -- so this can
    never drift from what was really configured. compression_ratio_
    threshold is never explicitly passed as a kwarg anywhere in this file
    (see get_transcription_params/_build_hotkey_transcribe_params), so
    there is no config value to read for it; _COMPRESSION_RATIO_THRESHOLD
    is faster-whisper's own real internal default for that check.

    Real speech does not produce these signals (that's the entire premise
    behind faster-whisper accepting a decode instead of escalating temp
    in the first place) -- a normal dictation's segments pass both checks,
    so this never fires on genuine speech, long or short. Empty seg_list
    means nothing to judge -- never fires."""
    if not seg_list:
        return False
    sig = diagnostics.segment_signals(seg_list)
    if sig['n_segments'] == 0:
        return False
    logprob_threshold = transcribe_params.get('log_prob_threshold')
    failing_logprob = (
        logprob_threshold is not None
        and sig['avg_logprob'] is not None
        and sig['avg_logprob'] < logprob_threshold
    )
    failing_compression = (
        sig['compression_ratio'] is not None
        and sig['compression_ratio'] > _COMPRESSION_RATIO_THRESHOLD
    )
    return failing_logprob or failing_compression


def hide_console():
    """Hide the console window (Windows only, no-op on other platforms)"""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception as e:
        logger.debug(f"Could not hide console window: {e}")


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
        except (ValueError, OSError) as _reconf_err:
            # Pre-logging-setup: logger isn't configured yet at this point in
            # module import -- print() is the only channel available.
            print(f"[INIT] {_stream_name} UTF-8 reconfigure skipped (packaged EXE / "
                  f"redirected / already-closed stream): {_reconf_err}")

if sys.stdout is not None:
    sys.stdout.write(f"[PRE-LOG] +{(time.perf_counter()-_POST_SD_T)*1000:.0f}ms (before logging setup)\n")
    sys.stdout.flush()
# Set up logging — persistent file in ~/.samsara/logs/ + console
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from samsara.paths import (
    migrate_legacy_source_config,
    samsara_config_path,
    samsara_home_dir,
)
from samsara.log import SAMSARA_LOG_HANDLER_TAG as _SAMSARA_LOG_HANDLER_TAG

LOG_DIR = samsara_home_dir() / "logs"
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

# Attach to root logger so all loggers (including exception hooks) feed here.
# Some samsara.* modules imported above already called get_logger() at
# import time (before this point), which makes samsara.log's fallback path
# attach its own file+console handler pair to the root logger -- meant only
# for standalone script/test usage that never reaches this bootstrap. Remove
# exactly that pair (and any pair from a previous run of this same block,
# e.g. a test re-importing this module) by tag rather than blanket-clearing
# root.handlers, so anything unrelated already on root (pytest's own
# log-capture handler, for instance) is left alone. This also makes the
# block idempotent: re-running it always yields exactly one tagged pair.
_root_logger = logging.getLogger()
for _h in list(_root_logger.handlers):
    if getattr(_h, _SAMSARA_LOG_HANDLER_TAG, False):
        _root_logger.removeHandler(_h)
setattr(file_handler, _SAMSARA_LOG_HANDLER_TAG, True)
setattr(console_handler, _SAMSARA_LOG_HANDLER_TAG, True)
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
        except Exception as e:
            # never let console output break a caller -- already in the log
            # file via logger.info(message) above, so this is display-only.
            logger.debug(f"ASCII-fallback console print also failed: {e}")


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
        logger.debug("[WAKE-TARGET] psutil not available — cannot resolve target window")
        return None

    exclude = exclude_pids or set()
    target_pids = set()
    try:
        for proc in _ps.process_iter(['pid', 'name']):
            name = proc.info.get('name') or ''
            if name.lower() == process_name.lower() and proc.info['pid'] not in exclude:
                target_pids.add(proc.info['pid'])
    except Exception as exc:
        logger.exception(f"[WAKE-TARGET] process enumeration error: {exc}")
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


_PREVIEW_PROFILE_MAX_AGE_S = 3600  # 1 hour
_PREVIEW_STARTUP_MONITOR_S = 60.0
_PREVIEW_DIAGNOSTIC_NAME = "preview-startup.log"


def _read_preview_diagnostics(path: Path, max_chars: int = 16000) -> str:
    """Read a bounded tail of detached-child output without raising."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<could not read {path}: {exc}>"
    return text[-max_chars:] if text else "<no child output was captured>"


def _show_preview_failure(message: str, diagnostic_path: "Path | None" = None) -> None:
    """Log full preview failure details and post a concise visible toast."""
    if diagnostic_path is not None:
        diagnostics = _read_preview_diagnostics(diagnostic_path)
        logger.error(
            "[PREVIEW] %s\nChild diagnostics (%s):\n%s",
            message, diagnostic_path, diagnostics,
        )
        last_line = next(
            (line.strip() for line in reversed(diagnostics.splitlines()) if line.strip()),
            "No child output was captured.",
        )
        visible = f"{message}\n\n{last_line}\n\nDetails: {diagnostic_path}"
    else:
        logger.error("[PREVIEW] %s", message)
        visible = message

    try:
        from samsara.ui.reminder_toast import get_toast
        get_toast().show("Preview First-Run Failed", visible)
    except Exception as toast_exc:
        logger.debug(f"[PREVIEW] Could not show failure toast: {toast_exc}")


def _monitor_preview_startup(
    process,
    diagnostic_path: Path,
    timeout: float = _PREVIEW_STARTUP_MONITOR_S,
) -> None:
    """Surface a detached preview that exits unsuccessfully during startup."""
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.info(
            "[PREVIEW] Child remained alive through %.1fs startup monitor; "
            "diagnostics=%s",
            timeout, diagnostic_path,
        )
        return
    except Exception as exc:
        logger.warning(f"[PREVIEW] Could not monitor child startup: {exc}")
        return

    if return_code:
        _show_preview_failure(
            f"Preview exited during startup with code {return_code}.",
            diagnostic_path,
        )
    else:
        logger.info("[PREVIEW] Child exited normally during startup monitor")


def _reap_old_preview_profiles() -> None:
    """Delete samsara_firstrun_* temp profile dirs older than the threshold.

    Each "Preview First-Run" tray action spawns a fully detached child that
    ends its own life with os._exit(0) (see DictationApp.quit_app), which
    bypasses atexit -- so nothing else ever cleans these up, and every click
    would otherwise leak one temp directory forever. Swept here, right
    before creating a new one (DictationApp.preview_first_run), rather than
    on a timer or at the detached child's own (unreliable) exit. Anything
    younger than the threshold is left alone in case that instance is still
    running.
    """
    import glob
    import tempfile as _tempfile

    pattern = str(Path(_tempfile.gettempdir()) / "samsara_firstrun_*")
    now = time.time()
    for path_str in glob.glob(pattern):
        try:
            p = Path(path_str)
            if not p.is_dir():
                continue
            if now - p.stat().st_mtime < _PREVIEW_PROFILE_MAX_AGE_S:
                continue
            shutil.rmtree(p, ignore_errors=True)
            logger.info(f"[PREVIEW] Reaped stale preview profile: {p}")
        except Exception as e:
            logger.debug(f"[PREVIEW] Could not reap {path_str}: {e}")


class DictationApp:
    def __init__(self, splash=None):
        self.splash = splash
        # Source and frozen launches share one per-user profile. Tests,
        # first-run previews, and other isolated launches use the explicit
        # SAMSARA_HOME_DIR override instead of a second implicit config.
        self.config_path = samsara_config_path()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # Boot-phase timing -- measure first, fix later.
        _bt0 = time.monotonic()
        _btp = [_bt0]  # mutable cell so the closure can write it
        def _boot(label: str) -> None:
            now = time.monotonic()
            logger.info(f"[BOOT] {label}: {(now - _btp[0]) * 1000:.0f}ms  (total {(now - _bt0) * 1000:.0f}ms)")
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
                    logger.exception(f"[SPLASH] close() failed: {e}")
                self.splash = None
            logger.info("First run detected - launching setup wizard...")
            from samsara.ui.first_run_wizard_qt import FirstRunWizardQt
            logger.debug(
                "[WIZ-DIAG] calling wizard.run() from thread name=%r ident=%s",
                threading.current_thread().name, threading.get_ident(),
            )
            wizard = FirstRunWizardQt(self.config_path, self)
            wizard_result = wizard.run()
            if wizard_result:
                # Wizard completed successfully, save the config
                with open(self.config_path, 'w') as f:
                    json.dump(wizard_result, f, indent=2)
                logger.info("Setup wizard completed successfully!")
            else:
                # Wizard was cancelled, use defaults but mark as complete
                logger.info("Setup wizard cancelled - using default settings")
            # No splash after wizard - user already saw UI
            # Auto-launch tutorial after wizard (first run only)
            self._launch_tutorial_after_wizard = True

        logger.info("[INIT] Loading config...")
        self.update_splash("Loading configuration...")
        with self._config_lock:
            self.load_config()
        _boot("config load")
        _bdiag("config load")

        self.update_splash("Setting up audio...")

        # Set the Samsara wheel as the default icon for all Qt windows.
        #
        # QImage/QPixmap/QIcon are GUI objects that must be constructed on
        # the Qt thread; this __init__ runs on a different thread. Building
        # them here used to intermittently deadlock boot (observed ~67% of
        # cold boots hanging at exactly this point, before audio device
        # enumeration). The PIL rendering (create_icon_image) isn't Qt and
        # stays here; only the QImage/QPixmap/QIcon/setWindowIcon calls move
        # onto the Qt thread via qt_runtime.post(), fire-and-forget so boot
        # never blocks on it.
        try:
            _icon_pil = self.create_icon_image(active=True).convert("RGBA")
            _icon_bytes = _icon_pil.tobytes()
            _icon_w, _icon_h = _icon_pil.width, _icon_pil.height

            def _apply_window_icon():
                try:
                    from PySide6.QtGui import QIcon, QImage, QPixmap
                    from PySide6.QtWidgets import QApplication
                    icon_qi = QImage(
                        _icon_bytes, _icon_w, _icon_h,
                        QImage.Format.Format_RGBA8888,
                    )
                    QApplication.instance().setWindowIcon(QIcon(QPixmap.fromImage(icon_qi)))
                except Exception as _e:
                    logger.exception(f"[ICON] Could not set Qt window icon: {_e}")

            from samsara.ui import qt_runtime
            qt_runtime.ensure_started()
            qt_runtime.post(_apply_window_icon)
        except Exception as _e:
            logger.exception(f"[ICON] Could not prepare window icon: {_e}")

        logger.info("[INIT] Enumerating audio devices...")
        from samsara.output_devices import (
            enumerate_output_devices,
            reconcile_output_device,
        )
        self.available_mics = self.get_available_microphones()
        self.available_outputs = enumerate_output_devices(
            sd,
            show_all=self.config.get('show_all_audio_devices', False),
        )
        self.output_device, self.output_device_name, output_missing = (
            reconcile_output_device(
                self.available_outputs,
                self.config.get('output_device'),
                self.config.get('output_device_name'),
            )
        )
        if output_missing:
            logger.warning(
                "[AUDIO] Selected output '%s' is unavailable; using system default",
                self.config.get('output_device_name') or self.config.get('output_device'),
            )
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
            logger.info(f"[CONFIG] Saved microphone {old_id} not found in current devices, "
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
        
        logger.info("[INIT] Loading plugins...")
        commands_path = Path(__file__).parent / "commands.json"
        self.command_executor = CommandExecutor(commands_path, app=self)
        # Gates command MATCHING during regular dictation -- distinct from
        # command_mode.enabled, which gates the walkie-talkie button/session
        # listener. Renamed from the legacy top-level 'command_mode_enabled'
        # (see _migrate_command_matching_enabled_flag in load_config).
        self.command_matching_enabled = self.config.get('command_mode', {}).get(
            'command_matching_enabled', True)
        _boot("plugin discovery + command executor")
        _bdiag("plugin discovery + command executor")

        # App index for parameterized "focus/open/close <x>" voice verbs
        # (plugins/commands/app_verbs.py) -- loads the on-disk cache
        # instantly (if any) and kicks a background thread to enumerate
        # fresh; never blocks boot.
        try:
            from samsara.app_index import get_app_index
            get_app_index().ensure_built_async()
        except Exception as exc:
            logger.exception(f"[APP-INDEX] Could not start background build: {exc}")

        # Repeat / again state
        self._last_command = None       # command dict of last repeatable command
        self._last_command_name = None  # canonical phrase of last repeatable command

        # Mouse 4 command mode (walkie-talkie hold-to-talk)
        self.command_mode_active = False
        self._command_mode_lock = threading.Lock()
        self._command_mode_miss_count = 0
        self._command_mode_inactivity_timer = None
        self._command_mode_timer_lock = threading.Lock()
        self._command_mode_session_start = 0.0  # monotonic time of last enter
        self._command_mode_ghost_tap = False    # set when hold < enter_debounce_ms
        self._command_mode_key_held = False      # edge-trigger guard vs OS key auto-repeat

        # Set while the ACE input stream is recovering from an unexpected
        # device loss -- an announced outage must not let the session's
        # inactivity timer expire out from under a user who is simply
        # waiting for their mic to come back. See _touch_session_activity,
        # _pause_session_inactivity_for_device_recovery.
        self._session_recovery_pause = False

        # Unified session state machine (COMMAND <-> DICTATE <-> AVA) for
        # toggle command mode. Constructed lazily on first toggle-mode entry;
        # reset() (not reconstruction) on every later entry/exit so the
        # wired-up callables are built once. See samsara/session_modes.py.
        self._session_mode_manager: "SessionModeManager | None" = None

        # SessionMode.AVA request-in-flight tracking (Phase 2). Distinct from
        # ava_mode_active/ava_mode_recording below, which belong to the
        # separate hold-to-talk Ava path (Right Alt) -- this queue only
        # serializes utterances dispatched through the LATCHED AVA session
        # mode, so a second "ava ..." utterance said while the agent is still
        # answering the first one doesn't spawn a second concurrent request.
        self._ava_session_dispatch_lock = threading.Lock()
        self._ava_session_request_in_flight = False
        self._ava_session_dispatch_queue: "collections.deque" = collections.deque(maxlen=3)

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
        # not (local ONNX load or inference failure), we fall back to RMS.
        self._vad_model = None
        self._vad_available = False
        # Constructed before the asynchronously loaded model is published.
        # ONNX inference is serialized through this lock; the model instance
        # is deliberately separate from faster-whisper's cached VAD instance.
        self._vad_lock = threading.Lock()
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

        # Phase 1 multi-wakeword: per-profile OWW detectors (id -> WakeWordDetector|None).
        # None means that profile uses Whisper-transcript fallback.
        # Loaded lazily in _load_wake_profile_models() after the Whisper model loads.
        self._wake_profile_detectors: dict = {}

        # Profile isolation: the send_word of whichever wake_profile is
        # CURRENTLY driving an open wake_session, captured at dispatch time
        # (_dispatch_wake_profile -> _start_wake_session) and consumed by the
        # wake_session termination check. Each profile carries its own
        # distinct send_word (agentic-safety requirement -- see
        # wake_profiles.normalize_profile_mode_and_send_word), so this must
        # be scoped to exactly the active profile's session, not read from
        # the shared/global wake_word_config.send_words list -- otherwise
        # profile A's terminator word can prematurely end profile B's
        # session. Reset to None by _reset_wake_dictation() on every
        # session-end path so it never survives into the next session.
        self._wake_session_send_word: "str | None" = None

        # Timestamp of the last successful command execution. While this is
        # within the 2-second post-command window, the audio callback
        # suppresses buffering to avoid picking up speaker output (Chrome
        # launch sound, notifications, etc.) as a new utterance.
        self._command_executed_at = None
        
        self._hotkey_recording = False  # Suppress wake word transcription during hotkey recording
        self._last_recording_earcon_ms = 0.0  # head-grace bookkeeping, see start_recording
        
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

        # Single-level undo for the last pasted dictation. Native Ctrl+Z is
        # only sent while the exact top-level window that received the paste
        # is still foreground; otherwise undo fails closed and remains
        # available until expiry so the user can refocus and retry.
        self._last_dictation_text = None
        self._last_dictation_length = 0
        self._last_dictation_hwnd = None
        self._undo_timer = None

        # Dictation history. self.config_path.parent (not Path(__file__).parent)
        # so this per-user file follows config.json's own SAMSARA_HOME_DIR /
        # frozen-build routing above -- otherwise a first-run preview instance
        # (SAMSARA_HOME_DIR set to a temp dir) would still read/overwrite the
        # REAL profile's history.json since it lives next to the code, not
        # in the profile directory.
        self.history_path = self.config_path.parent / 'history.json'
        self.max_history = 100  # Keep last 100 items
        self.history = self.load_history()  # List of (timestamp, text, is_command) tuples

        # Persistent SQLite-backed history at ~/.samsara/history.db. Separate
        # from self.history (above) so the existing HistoryWindow keeps working
        # while the new store records every attempt -- including failures.
        try:
            self.history_db = HistoryManager()
            self.history_db.prune(max_entries=10000)
        except Exception as e:
            logger.exception(f"[HISTORY] Could not open persistent history: {e}")
            self.history_db = None
        # Thin task-shaped façade (append/query/delete/clear) over the same
        # HistoryManager instance above -- see samsara/history_store.py.
        # Not a second database; the redesigned history list view reads
        # through this instead of history_db's richer session-tracking API.
        self.history_store = HistoryStore(self.history_db)
        _boot("history / SQLite init")
        _bdiag("history / SQLite init")

        logger.info("[INIT] Building UI...")

        # Voice Training window — create on Qt thread
        self.voice_training_window = None
        if _VoiceTrainingQt is not None:
            def _init_vt():
                try:
                    self.voice_training_window = _VoiceTrainingQt(self)
                except Exception as _e:
                    logger.debug(f"[INIT] VoiceTrainingQt unavailable: {_e}")
            self._schedule_ui(_init_vt)
            logger.info("[INIT] Using VoiceTrainingQt")

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
            logger.debug("[INIT] WakeWordDebugQt unavailable")

        # Listening state indicator overlay — must be created on the Qt thread.
        # ListeningIndicator is a QWidget; creating it on the main thread
        # causes "Timers cannot be started from another thread" and freezes
        # the entire Qt event loop.
        self.listening_indicator = None  # set by _init_indicator on Qt thread

        def _init_indicator():
            self.listening_indicator = ListeningIndicator()
            self.listening_indicator.set_mode(self._get_mode_display())
            position = self.config.get('listening_indicator_position', 'bottom-center')
            custom = self.config.get('listening_indicator_custom_position')
            if position == 'custom' and isinstance(custom, dict) and custom:
                self.listening_indicator.set_custom_position(
                    custom.get('screen'),
                    custom.get('cx') if custom.get('cx') is not None else 0.5,
                    custom.get('cy') if custom.get('cy') is not None else 0.5)
            else:
                self.listening_indicator.set_position(position)
            self.listening_indicator.placement_committed.connect(
                self._on_indicator_placement_committed)
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
                    thread_registry.spawn(
                        "vision-warmup",
                        self._vision_bridge.warmup,
                        daemon=True,
                    )
                    logger.info("[VISION] Warmup started in background.")
            except Exception as e:
                logger.exception(f"[VISION] Init failed: {e}")
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
                    logger.exception(f"[TUTORIAL] Failed to launch tutorial: {_e}")
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

        # Adaptive learning for transcription corrections. self.config_path.parent
        # (not Path(__file__).parent) -- see history_path above for why:
        # correction_candidates.json is per-user accumulated data and must
        # follow the same profile-isolation routing as config.json.
        self.adaptive_learner = AdaptiveLearner(self.config_path.parent)

        # Notification manager for reminders. config_dir here (reminders.json
        # and, below, alarm_stats.json) is per-user data -- same reasoning
        # and same self.config_path.parent routing as history_path above.
        config_dir = self.config_path.parent
        self.notification_manager = NotificationManager(config_dir)
        if self.config.get('notifications', {}).get('enabled', True):
            self.notification_manager.start()

        # Alarm manager for persistent sound reminders
        sounds_dir = Path(__file__).parent / 'sounds'
        self.alarm_manager = AlarmManager(
            config_dir=config_dir,
            sounds_dir=sounds_dir,
            get_config=lambda: self.config,
            save_config=self.persist_config,
            output_device=self.output_device,
        )
        self.alarm_manager.on_alarm_triggered = self._show_alarm_notification
        if self.config.get('alarms', {}).get('enabled', True):
            self.alarm_manager.start()

        # Contextual hint system
        from samsara.hints import HintManager
        self.hints = HintManager(self)

        logger.info("[INIT] Initializing TTS...")
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
                    self.tts_engine = EdgeTTSEngine(output_device=self.output_device)
                    logger.info("[TTS] Initialized EdgeTTS engine (Azure Neural voices)")
                else:
                    self.tts_engine = WinRTEngine(output_device=self.output_device)
                    logger.info("[TTS] Initialized WinRT engine")
                self.audio_coordinator = AudioCoordinator(
                    self,
                    engine=self.tts_engine,
                    config=self.config.get('audio_coordinator', {}),
                )
                logger.info("[TTS] AudioCoordinator ready")
            except Exception as e:
                logger.exception(f"[TTS] Failed to initialize: {e}")
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
            logger.info("[SMART ACTIONS] Phase 2 bridge/session/tools initialized")
        except Exception as e:
            logger.exception(f"[SMART ACTIONS] Phase 2 init failed: {e}")
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
        self._capslock_lifecycle_lock = threading.Lock()
        self._capslock_streaming_session = None
        self._capslock_hook = None
        self._install_capslock_hook()

        # Tell the user if the model needs to be downloaded vs just loaded
        _model_size = self.config.get('model_size', 'base')

        # .en models cannot transcribe non-English audio at all -- never
        # silently swap the model, just make the mismatch visible. Auto
        # counts as "non-English" here too: auto-detect is pointless on an
        # English-only model.
        _configured_lang = self.config.get('language', 'en')
        if _configured_lang != 'en' and _languages.is_english_only_model(_model_size):
            logger.warning(
                f"[LANG] Configured language={_configured_lang!r} but model_size="
                f"{_model_size!r} is English-only -- switch to a multilingual "
                f"model (no .en suffix) or transcription will stay in English."
            )

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
        self._streaming_session     = None    # Sole owner until final/cancel cleanup completes
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
        logger.info(f"Dictation app starting...")
        logger.info(f"Mode: {mode}")
        logger.info(f"Hotkey: [{self.config['hotkey']}]")
        logger.info(f"Continuous hotkey: [{self.config.get('continuous_hotkey', 'ctrl+alt+d')}]")
        logger.info(f"Wake word hotkey: [{self.config.get('wake_word_hotkey', 'ctrl+alt+w')}]")
        logger.info(f"Using model: {self.config['model_size']}")
        logger.info(f"Hotkey detection: state-based (simultaneous key support)")

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
            logger.info("[CONFIG] File watcher started")
        except Exception as _cw_err:
            logger.warning(f"[CONFIG] File watcher unavailable: {_cw_err}")

        self.create_tray_icon()

    def update_splash(self, status):
        """Update splash screen status"""
        if self.splash:
            try:
                self.splash.set_status(status)
            except Exception as e:
                logger.debug(f"Splash status update failed: {e}")

    def _close_splash_post_load(self):
        """Close the splash screen after the model has finished loading.
        Runs on the UI thread via _schedule_ui."""
        if self.splash:
            try:
                self.splash.close()
            except Exception as e:
                logger.exception(f"[SPLASH] close() failed: {e}")
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
            self._ace_engine = AudioCaptureEngine(
                ring, config=engine_config,
                on_stream_death=self._on_ace_stream_death,
                on_recovery_success=self._on_ace_recovery_success,
                on_give_up=self._on_ace_recovery_give_up,
            )
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

            logger.debug("[ACE] Engine started — hold / continuous / wake dictation ready")
        except Exception as exc:
            logger.exception(f"[ACE] Engine failed to start: {exc}")
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
            output_dir = str(samsara_home_dir() / "debug_audio")
            self._ace_debug_rec = DebugRecorder(
                engine=self._ace_engine,
                output_dir=output_dir,
                max_seconds=30.0,
            )
            self._ace_debug_rec.start_recording()
            logger.debug(f"[ACE] Debug capture active -> {output_dir}")
        except Exception as exc:
            logger.exception(f"[ACE] Debug recorder failed to start: {exc}")
            self._ace_debug_rec = None

    def _on_ace_stream_death(self) -> None:
        """AudioCaptureEngine.on_stream_death -- the input device died
        unexpectedly (unplugged, BT dropped). Runs on whatever thread
        sounddevice's finished_callback fires on.

        Ends whatever utterance was in flight NOW rather than leaving it
        frozen until recovery succeeds or gives up (no new frames arrive
        during the outage, so nothing would otherwise flush or discard it),
        and pauses the session's inactivity timer so a user waiting for
        their mic to reconnect doesn't get silently kicked out of an
        otherwise-fine session."""
        logger.error("[ACE] Audio device lost -- entering recovery")
        try:
            self.play_sound('error')
        except Exception as e:
            logger.debug(f"[ACE] Death earcon failed: {e}")

        wc = getattr(self, '_wake_consumer', None)
        if wc is not None:
            try:
                wc.abort_utterance()
            except Exception as e:
                logger.debug(f"[ACE] WakeConsumer abort during device loss failed: {e}")

        if self.continuous_active:
            cc = getattr(self, '_continuous_consumer', None)
            if cc is not None:
                try:
                    cc.abort()
                except Exception as e:
                    logger.debug(f"[ACE] ContinuousConsumer abort during device loss failed: {e}")

        self._pause_session_inactivity_for_device_recovery()

    def _on_ace_recovery_success(self) -> None:
        """AudioCaptureEngine.on_recovery_success -- the device reappeared
        and the stream was rebuilt on the same FrameBus; all consumers
        resume automatically (they never re-register)."""
        logger.info("[ACE] Audio device recovered -- stream rebuilt")
        try:
            self.play_sound('start')
        except Exception as e:
            logger.debug(f"[ACE] Recovery earcon failed: {e}")
        self._resume_session_inactivity_after_device_recovery()

    def _on_ace_recovery_give_up(self) -> None:
        """AudioCaptureEngine.on_give_up -- 60s of polling never found the
        device again. Loud failure, but the app stays alive: the user can
        still reconnect the device and pick it (or another) from the tray
        mic menu, which will restart the engine via switch_microphone()."""
        logger.error("[ACE] Audio device recovery gave up after 60s -- device never reappeared")
        try:
            self.play_sound('error')
        except Exception as e:
            logger.debug(f"[ACE] Give-up earcon failed: {e}")
        nm = getattr(self, 'notification_manager', None)
        if nm is not None:
            try:
                nm.show_notification(
                    "Microphone Lost",
                    "Samsara can't hear you. Reconnect your mic and select it "
                    "from the tray menu.",
                    duration=10,
                )
            except Exception as e:
                logger.debug(f"[ACE] Give-up notification failed: {e}")
        self._resume_session_inactivity_after_device_recovery()

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
                    logger.exception(f"[ACE] {_name} consumer deactivate error: {exc}")
                setattr(self, _attr, None)

        if self._ace_debug_rec is not None:
            try:
                path = self._ace_debug_rec.stop_recording()
                if path:
                    logger.debug(f"[ACE] Final debug WAV: {path}")
            except Exception as exc:
                logger.exception(f"[ACE] DebugRecorder stop error: {exc}")
            self._ace_debug_rec = None

        if self._ace_engine is not None:
            try:
                self._ace_engine.stop()
            except Exception as exc:
                logger.exception(f"[ACE] Engine stop error: {exc}")
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
            "dictate_commit_hotkey": DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
            "correction_hotkey": "ctrl+alt+r",
            "cancel_hotkey": "escape",
            # Nested hotkey namespace (new features land here rather than as
            # more top-level *_hotkey keys). capture_correction opens the
            # correction-capture window (samsara/ui/correction_capture_qt.py)
            # pre-filled with the last dictation. Verified against every
            # other hotkey default above (ctrl+shift, ctrl+alt+d/w/c/z/r,
            # escape, capslock, ctrl+space) -- ctrl+alt+x is free.
            "hotkeys": {
                "capture_correction": "ctrl+alt+x",
            },
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
            "output_device": None,
            "output_device_name": None,
            "silence_threshold": DEFAULT_SILENCE_TIMEOUT,
            "min_speech_duration": DEFAULT_MIN_SPEECH_DURATION,
            # Continuous mode commit trigger: "silence" is today's fixed
            # 2s-auto-commit behavior (unchanged). "key" lets the user talk
            # with unlimited thinking pauses and commit each utterance by
            # tapping continuous_commit_hotkey instead. Set to "key" in
            # config/Settings to enable -- not turned on by default.
            "continuous_commit_trigger": DEFAULT_CONTINUOUS_COMMIT_TRIGGER,
            # Hotkey that commits the accumulated speech when trigger == "key".
            # Only ever active while continuous mode is running with that
            # trigger -- never live in hold/toggle modes.
            "continuous_commit_hotkey": DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
            # Safety cap (seconds of accumulated speech): bounds an
            # un-committed "key"-mode session so it can't grow unbounded if
            # the user forgets to tap the commit hotkey. No effect in
            # "silence" mode.
            "continuous_max_buffer_s": DEFAULT_CONTINUOUS_MAX_BUFFER_S,
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
            "ui_scale": 1.0,
            "first_run_complete": True,
            "premium_license": "",
            # New nested wake word config
            "wake_word_config": {
                "enabled": True,
                "phrase": "jarvis",
                "phrase_options": ["jarvis", "hey jarvis", "computer", "hey computer", "samsa", "hey samsa"],
                "quick_silence_timeout": 1.0,
                "end_words": ["over", "done", "end dictation"],
                "wake_abort_phrase": ["cancel", "cancel dictation", "abort"],
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
            # Multi-wakeword: phrase -> target_process -> focus+dictate.
            # Each entry binds a spoken phrase to a target application by process name.
            # Missing oww_model -> Whisper-transcript fallback (match_wake_phrase).
            # Drop trained .onnx files into samsara/wake_models/ to enable OWW pre-filter.
            # mode: 'focus_dictate' types live and presses Enter on send_word;
            #       'stage_send' buffers utterances (never types until send_word) and
            #       never presses Enter -- for agentic targets (see samsara/wake_profiles.py).
            # send_word: THIS profile's own terminator -- must be distinct across
            #       profiles (agentic-safety requirement, see docs design review
            #       arc_20260629_170545.md). Never shared with another profile.
            # Phrases are validated at config load (samsara.wake_profiles.validate_wake_profiles):
            # under 3 syllables or a duplicate of another enabled profile's phrase disables it.
            "wake_profiles": [
                {
                    "id": "claude",
                    "phrase": "activate claude",
                    "oww_model": "activate_claude.onnx",
                    "target_process": "claude.exe",
                    "enabled": True,
                    "mode": "focus_dictate",
                    "send_word": "over",
                },
                {
                    "id": "hermes",
                    "phrase": "activate hermes",
                    "oww_model": "activate_hermes.onnx",
                    "target_process": "Hermes.exe",
                    "enabled": True,
                    "mode": "stage_send",
                    "send_word": "send",
                },
            ],
            # Echo cancellation (removes system audio from mic input).
            # Default OFF (2026-07-10): the homegrown NLMS adaptive filter
            # converges to only 3-8% echo reduction and adversarial review
            # concluded it likely adds artifacts/distortion -- net-negative.
            # Retired pending WebRTC AEC3 / Windows communications-mode
            # evaluation (separate, post-release item). Code kept intact,
            # just not on by default -- see samsara/echo_cancel.py and
            # samsara/config_schema.py's echo_cancellation.enabled entry.
            "echo_cancellation": {
                "enabled": False,
                "latency_ms": 30.0,
            },
            # Audio ducking (2026-07-10) -- attenuates OTHER apps' audio
            # sessions while dictating instead of subtracting echo after
            # capture. Off by default, opt-in -- see samsara/audio_ducking.py
            # and samsara/config_schema.py's ducking.enabled entry.
            "ducking": {
                "enabled": False,
                "level": 0.2,
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
            # Smart Corrections: optional LLM post-processing pass over
            # dictation output (homophones/misrecognitions/punctuation).
            # Off by default. See samsara/smart_corrections.py.
            "smart_corrections": {
                "enabled": False,
                "backend": "auto",            # "auto" | "ollama" | "cloud"
                "ollama_model": "qwen2.5:3b",  # already pulled on this machine
                "timeout_s": 6.0,
                "min_words": 3,
                "allow_cloud_fallback": False,  # opt-in: auto may route to cloud when local AI is down
                "keep_alive": "30m",            # Ollama model residency after each call
                "modes": {"hotkey": True, "wake": True, "streaming": False},
                # Opt-in: strip filler words, immediate self-corrections, and
                # abandoned fragments. Widens the sanitizer's shrink allowance
                # and suspends its punctuation floor while on -- see
                # samsara/smart_corrections.py.
                "repair_disfluencies": False,
            },
            # Inline formatting tokens ("new line" -> \n, "new paragraph" ->
            # \n\n, "insert tab" -> \t, "bullet"/"bullet point" -> \n• ) applied to
            # DICTATE output only, after smart_correct, before delivery. See
            # samsara/formatting_tokens.py.
            "formatting_tokens": {
                "enabled": True,
            },
            # Dictation Diagnostics: per-utterance pipeline instrumentation
            # (samsara/diagnostics.py). Ring buffer always active; this only
            # gates the optional on-disk JSONL append.
            "diagnostics": {
                "write_jsonl": False,
            },
            # Opt-in debug tooling. dump_hotkey_buffers: write the exact
            # assembled hotkey buffer (post-prepend, pre-fade -- what
            # Whisper actually receives) to ~/.samsara/debug/hotkey_*.wav
            # on every hotkey transcription. Off by default -- 2026-07-10
            # hotkey word-loss investigation; see
            # DictationSessionConsumer._log_seam_diagnostics.
            "debug": {
                "dump_hotkey_buffers": False,
            },
            # Personal WER benchmark: opt-in local (user's real audio, gold
            # transcript) sample collection for the offline accuracy harness.
            # Off by default -- audio never leaves the machine either way.
            # See samsara/benchmark_store.py and tools/benchmark_eval.py.
            "benchmark": {
                "collect_samples": False,
                "max_samples": 200,
            },
            # Correction capture (samsara/correction_capture.py +
            # samsara/ui/correction_capture_qt.py): the hotkey-triggered
            # "fix my last dictation" flow. max_edit_ratio gates the
            # whole-text rewrite check -- if more than this fraction of the
            # words changed, extraction offers zero learnable pairs.
            "correction_capture": {
                "max_edit_ratio": 0.5,
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
            # Walkie-talkie command mode (also hosts the unified session --
            # dictate/Ava voice switching -- when mode is "toggle")
            "command_mode": {
                "enabled": False,           # opt-in; see Modes tab -> Command Mode
                "command_matching_enabled": False,  # gates command MATCHING during
                                            # regular dictation (spoken "command mode
                                            # on/off") -- distinct from "enabled" above
                "mode": "hold",             # "hold" (hold to talk) or "toggle"
                "button": "rctrl",          # "rctrl" (default), "mouse4"/"mouse5" (XButton1/2), or other keys -- see _CMD_BUTTON_OPTIONS
                "enter_debounce_ms": 200,   # delay before playing enter earcon
                "exit_earcon": True,        # play stop earcon on release/exit
                "miss_limit": 5,            # toggle: exit after N unmatched recordings
                "inactivity_timeout_s": 300, # toggle: exit after N seconds silence
                "tts_char_limit": 50,       # suppress TTS responses longer than this
                "suppress_button": True,    # consume mouse4/5 click so browsers don't navigate back
                "utterance_silence_s": 1.0,         # toggle, COMMAND sub-mode: per-utterance VAD silence gap
                "dictate_utterance_silence_s": 2.0, # toggle, DICTATE sub-mode: longer gap for mid-sentence pauses
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
                logger.exception(f"[CONFIG] config.json has invalid JSON: {_je}")
                if bak_path.exists():
                    try:
                        with open(bak_path, 'r') as f:
                            self.config = json.load(f)
                        _loaded_from_disk = True
                        logger.info("[CONFIG] Loaded from config.json.bak (backup)")
                    except Exception as _bak_err:
                        logger.error(f"[CONFIG] Backup also invalid — using defaults: {_bak_err}")
                else:
                    logger.warning("[CONFIG] No backup found — using defaults")
            except Exception as _cfg_err:
                # config IO failure the user should be able to find in the log
                # even though load_config() itself must still fall through to
                # defaults here -- a broken/unreadable config.json must never
                # prevent the app from starting.
                logger.exception(f"[CONFIG] config.json read failed — using defaults: {_cfg_err}")

        if _loaded_from_disk:
            # Migrate old flat wake word config to new nested structure
            logger.debug("[CONFIG] load_config: starting _migrate_wake_word_config")
            self._migrate_wake_word_config(default_config)
            logger.debug("[CONFIG] load_config: _migrate_wake_word_config done")

            self._migrate_command_matching_enabled_flag()

            # Fill in any missing top-level keys
            for key in default_config:
                if key not in self.config:
                    self.config[key] = default_config[key]
        else:
            self.config = default_config
            wake_profiles.validate_wake_profiles(self.config['wake_profiles'])
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
            logger.info(f"[MIGRATE] mode='{old_mode}' -> mode='hold' + wake_word_enabled=True")

        # Multi-wakeword: wake_targets -> wake_profiles rename (tribunal spec,
        # design review arc_20260629_170545.md). Renames an existing on-disk
        # list in place rather than discarding the user's own edits.
        if 'wake_targets' in self.config and 'wake_profiles' not in self.config:
            self.config['wake_profiles'] = self.config.pop('wake_targets')
            logger.info("[MIGRATE] Renamed wake_targets -> wake_profiles")

        if 'wake_profiles' not in self.config:
            self.config['wake_profiles'] = copy.deepcopy(default_config.get('wake_profiles', []))
            logger.info("[MIGRATE] Injected default wake_profiles")

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
            logger.info("[CONFIG] Migrated wake word settings to new format")
        else:
            # Ensure all nested keys exist (for configs created between versions)
            self._deep_update(self.config['wake_word_config'], default_config['wake_word_config'])

        # cancel_words -> wake_abort_phrase rename, inside the now-guaranteed
        # wake_word_config dict.
        _wwc = self.config['wake_word_config']
        if 'cancel_words' in _wwc and 'wake_abort_phrase' not in _wwc:
            _wwc['wake_abort_phrase'] = _wwc.pop('cancel_words')
            logger.info("[MIGRATE] Renamed wake_word_config.cancel_words -> wake_abort_phrase")

        # Per-profile send_word (agentic-safety requirement): migrate the old
        # GLOBAL send_words list (wake_word_config.send_words, first entry)
        # into each profile that doesn't already carry its own send_word, and
        # fold legacy send_policy ('enter'|'stage_only') into mode
        # ('focus_dictate'|'stage_send'). A stage_send (agentic) target must
        # end up with a send_word distinct from any focus_dictate target's --
        # this only fills a default, it never clobbers an explicit value, so
        # profiles can diverge from here on.
        _legacy_send_words = _wwc.get('send_words') or ['over']
        _legacy_default_send_word = _legacy_send_words[0]
        for _profile in self.config['wake_profiles']:
            if isinstance(_profile, dict):
                wake_profiles.normalize_profile_mode_and_send_word(
                    _profile, default_send_word=_legacy_default_send_word)

        wake_profiles.validate_wake_profiles(self.config['wake_profiles'])
    
    def _migrate_command_matching_enabled_flag(self):
        """Migrate the legacy top-level 'command_mode_enabled' flag into
        'command_mode.command_matching_enabled'.

        Two near-identically-named flags coexisted: top-level
        'command_mode_enabled' (legacy -- gates command MATCHING during
        regular dictation) and nested 'command_mode.enabled' (gates the
        walkie-talkie button/session listener, unrelated). Renaming the
        legacy one to live inside the same 'command_mode' sub-dict, next to
        its actual sibling settings, makes the two impossible to confuse by
        name alone. Runs once per legacy config; a config that never had the
        top-level key (fresh installs, already-migrated configs) is a no-op.
        """
        if 'command_mode_enabled' not in self.config:
            return
        legacy_value = self.config.pop('command_mode_enabled')
        self.config.setdefault('command_mode', {})['command_matching_enabled'] = legacy_value
        self.save_config()
        logger.info(
            f"[MIGRATE] command_mode_enabled={legacy_value!r} -> "
            f"command_mode.command_matching_enabled={legacy_value!r}"
        )

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
                    logger.warning(f"[WARN] Could not read on-disk config for merge: {e}")
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
                    logger.warning(f"[WARN] Could not backup config to .bak: {e}")

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
            except OSError as e:
                logger.debug(f"Temp config file cleanup failed: {e}")

            # 4. Sync in-memory config and snapshot to what was written.
            self.config = merged
            self._config_last_disk_snapshot = copy.deepcopy(merged)
        except Exception as e:
            # Clean up the temp file if we left one lying around
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError as _cleanup_err:
                logger.debug(f"Temp config file cleanup failed after save error: {_cleanup_err}")
            logger.exception(f"[ERROR] save_config failed: {e}")
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
            logger.exception(f"[CONFIG] reload_config_from_disk: invalid JSON — {e}")
            return 0
        except OSError as e:
            logger.exception(f"[CONFIG] reload_config_from_disk: could not read file — {e}")
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
            logger.info(f"[CONFIG] External edit detected: {key} changed "
                  f"{old_v!r} -> {new_v!r}")

        # Fire the same side-effects as update_config
        if 'mode' in changed:
            try:
                self.apply_mode(changed['mode'][1])
            except Exception as e:
                logger.exception(f"[CONFIG] apply_mode error: {e}")
        if 'wake_word_enabled' in changed:
            try:
                self.set_wake_word_enabled(changed['wake_word_enabled'][1])
            except Exception as e:
                logger.exception(f"[CONFIG] set_wake_word_enabled error: {e}")
        if 'microphone' in changed:
            try:
                self.capture_rate = self._detect_capture_rate(changed['microphone'][1])
            except Exception as e:
                logger.exception(f"[CONFIG] capture_rate update error: {e}")
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
                logger.exception(f"[CONFIG] wake_word_config update error: {e}")

        return len(changed)

    def set_app_state(self, **kwargs):
        """Update application state flags with transition logging.

        Centralizes critical state changes (recording, mode activation) so
        transitions are visible in the console log.
        """
        for key, value in kwargs.items():
            if not hasattr(self, key):
                logger.warning(f"[WARN] Unknown state key: {key}")
                continue
            old = getattr(self, key)
            if old != value:
                setattr(self, key, value)
                logger.debug(f"[STATE] {key}: {old} -> {value}")

    def _detect_capture_rate(self, device_id):
        """Query the native sample rate of a device. Falls back to DEFAULT_CAPTURE_RATE."""
        try:
            if device_id is not None:
                info = sd.query_devices(device_id)
                rate = int(info['default_samplerate'])
                logger.info(f"[AUDIO] Device {device_id} native rate: {rate}Hz")
                return rate
        except Exception as e:
            logger.exception(f"[WARN] Could not query device {device_id} rate: {e}")
        return DEFAULT_CAPTURE_RATE

    def _run_calibration_if_auto(self):
        """Run mic calibration if threshold_mode is 'auto'. Updates config in place."""
        mode = self.config.get('threshold_mode', 'auto')
        if mode != 'auto':
            thresh = self.config.get('wake_word_config', {}).get('audio', {}).get(
                'speech_threshold', DEFAULT_SPEECH_THRESHOLD)
            logger.debug(f"[CAL] Threshold mode: manual ({thresh:.4f})")
            return

        mic_id = self.config.get('microphone')
        multiplier = self.config.get('cal_multiplier', 3.0)
        try:
            rms_samples = measure_ambient_rms(mic_id, self.capture_rate)
            threshold = calibrate_threshold(rms_samples, multiplier=multiplier)
            ambient = float(np.median(rms_samples)) if rms_samples else 0.0
            logger.debug(f"[CAL] Ambient RMS: {ambient:.4f} | "
                  f"Multiplier: {multiplier}x | Threshold: {threshold:.4f}")
        except Exception as e:
            threshold = DEFAULT_SPEECH_THRESHOLD
            logger.exception(f"[CAL] Calibration failed ({e}), using default {threshold:.4f}")

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
        thread_registry.spawn("dictation._do", _do, daemon=True)

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

    def refresh_audio_devices(self):
        """Re-enumerate input microphones, picking up devices connected after boot.

        sounddevice/PortAudio caches its host API device list at
        initialization time -- a Bluetooth mic (or any device) plugged in
        after launch never appears from a plain sd.query_devices() call.
        Forcing PortAudio to re-scan requires sd._terminate() + sd._initialize()
        (the documented re-enumeration pattern). That re-init is best-effort:
        if it raises, we log a warning and fall back to a plain re-query
        (get_available_microphones() always re-queries regardless, so the
        fallback is implicit -- no separate code path needed).

        MUST NOT run while any audio stream is open: PortAudio re-init while
        a stream is live can stutter or kill it. Gated on
        _is_audio_capture_active() -- the same flag tray_qt.py's menu rebuild
        already uses to guard mic-list refreshes. If active, this is a no-op:
        logs at INFO and returns the current (unchanged) list.

        Thread-safety: call only from the Qt/UI thread. There is no
        additional lock here -- every existing reader/writer of
        self.available_mics (tray_qt.py's menu rebuild, settings_qt.py, the
        setup wizards) already only touches it from the Qt thread, and
        _is_audio_capture_active() is the same gate already relied on to
        keep re-enumeration from racing an active recording/stream.

        Returns the fresh list, in the same shape as
        get_available_microphones() (this IS that same method -- there is
        only one enumeration code path).
        """
        if self._is_audio_capture_active():
            logger.info("[MIC] refresh skipped — audio active")
            return self.available_mics

        try:
            sd._terminate()
            sd._initialize()
        except Exception as exc:
            logger.warning(f"[MIC] PortAudio re-init failed, falling back to plain re-query: {exc}")

        self.available_mics = self.get_available_microphones()
        self._reconcile_microphone_selection()
        return self.available_mics

    def get_available_output_devices(self):
        """Return deduplicated output endpoints for Settings."""
        from samsara.output_devices import enumerate_output_devices
        self.available_outputs = enumerate_output_devices(
            sd,
            show_all=self.config.get('show_all_audio_devices', False),
        )
        return self.available_outputs

    def switch_output_device(self, device_id, device_name=None):
        """Route Samsara feedback only; never changes the Windows default."""
        from samsara.output_devices import reconcile_output_device

        if device_id is None:
            resolved_id, resolved_name, missing = None, None, False
        else:
            self.available_outputs = enumerate = self.get_available_output_devices()
            resolved_id, resolved_name, missing = reconcile_output_device(
                enumerate, device_id, device_name
            )
        if missing:
            logger.warning(
                "[AUDIO] Selected output '%s' disconnected; using system default",
                device_name or device_id,
            )

        self.output_device = resolved_id
        self.output_device_name = resolved_name
        self.stop_sound_stream()
        self._start_sound_stream()
        # _start_sound_stream may itself fall back after an open failure.
        effective = self.output_device
        engine = getattr(self, 'tts_engine', None)
        if engine is not None and hasattr(engine, 'set_output_device'):
            engine.set_output_device(effective)
        alarms = getattr(self, 'alarm_manager', None)
        if alarms is not None and hasattr(alarms, 'set_output_device'):
            alarms.set_output_device(effective)
        logger.info(
            "[AUDIO] Samsara output set to %s",
            resolved_name if effective is not None else "System default",
        )

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
            logger.exception(f"Failed to load history: {e}")
        return []

    def save_history(self):
        """Save history to file"""
        try:
            with open(self.history_path, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception(f"Failed to save history: {e}")

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
            # Single source of truth for the Whisper `language` kwarg --
            # "auto" resolves to None (faster-whisper auto-detect); every
            # other value passes through as-is. See samsara/languages.py.
            'language': _languages.resolve_transcribe_language(self),
            'initial_prompt': self.voice_training_window.get_initial_prompt(),
            # Native faster-whisper silence suppression (primary hallucination
            # defense -- see "Gate and Reset" architecture, module-level
            # constants above). More causal than any post-hoc text check:
            # Whisper itself returns empty on low-speech-probability audio.
            'no_speech_threshold': _NO_SPEECH_THRESHOLD,
            'log_prob_threshold': _LOGPROB_THRESHOLD,
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

    def _build_hotkey_transcribe_params(self):
        """Build model.transcribe() kwargs for the hotkey dictation path.

        Starts from get_transcription_params() (mode-based defaults) and
        forces the hotkey-specific overrides: VAD disabled (the user
        explicitly pressed the hotkey -- don't strip their speech), and a
        clean per-press conversation-context reset (no residual conditioning
        carried over from a previous press -- see the "Gate and Reset"
        hallucination-prevention architecture, module-level constants near
        the top of this file). The vocabulary/initial_prompt from voice
        training is still applied -- the clean-slate guarantee is about
        conversation context (condition_on_previous_text), not vocabulary
        biasing. Used by both the normal (<30s) and [LONG] branches of the
        hotkey transcribe() closure -- they share this same dict, so this is
        the single place that guarantee is enforced.

        vad_filter=False HISTORY (2026-07-10, flipped twice in one night --
        read this before touching it again): originally force-disabled
        (the setting you see now) from the click/bloop hallucination era,
        on the theory that faster-whisper's own VAD could strip a user's
        genuine speech right after the hotkey press. Commit 576f412
        flipped it to True (mode default) based on an A/B decode-parameter
        experiment (tools/transcribe_ab.py) against dumps of "you know
        what I mean" transcribing as "i know what i mean"/garbage -- BUT
        that experiment ran against Whisper "base" (transcribe_ab.py's
        hardcoded model), not the production model, and the observed
        defect turned out to be unrelated to decode parameters entirely:
        samsara/cleanup.py's FILLERS list stripped r'\\byou know\\b'
        UNANCHORED, deleting the phrase from every position in every
        dictation regardless of vad_filter, downstream of Whisper. Fixed
        there (comma-anchored, matching every other context-sensitive
        filler in that list) instead. Re-running the same dumps confirms
        the PRODUCTION model transcribes them correctly with vad_filter
        True OR False -- the A/B result that justified the flip doesn't
        replicate once the real (cleanup.py) cause is fixed, so this
        reverts to the original force-False: smaller change surface, and
        the theoretical clipping risk it guards against was never actually
        disproven, only a different, unrelated bug was found and fixed.
        tools/transcribe_ab.py now accepts --model/--device and defaults
        to the live-config model rather than a hardcoded 'base', so this
        specific model-mismatch confound can't recur silently.
        tests/test_transcription_params.py's vad_filter lock was reverted
        to match -- see that file for the test-level documentation.
        """
        transcribe_params = self.get_transcription_params()
        # DISABLE faster-whisper's VAD for hotkey-triggered dictation.
        # User explicitly pressed the hotkey — don't strip their speech.
        transcribe_params['vad_filter'] = False
        # Force a clean slate on EVERY hotkey press. Conditioning on
        # tokens carried over from a previous press is what let
        # hallucinations escalate over a session -- each press must
        # start with zero residual conversational state, independent of the
        # [LONG] path (which has its own reasons not to condition).
        transcribe_params['condition_on_previous_text'] = False
        # Vocabulary biasing is still wanted per-press -- only conversation
        # context gets the clean-slate reset above, not the trained prompt.
        transcribe_params['initial_prompt'] = self.voice_training_window.get_initial_prompt() or ""
        # Command-mode hotkey (Right Ctrl / Mouse 4, self.command_mode_recording)
        # is matched against the English command registry -- force English
        # regardless of the configured dictation language so command
        # recognition stays reliable. Commands are English-only by design;
        # Ava-mode (Right Alt) recordings are NOT forced here since that
        # content goes to the LLM as a natural-language query, not matched
        # against a fixed phrase registry.
        if getattr(self, 'command_mode_recording', False):
            transcribe_params['language'] = 'en'
        return transcribe_params

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
        except Exception as e:
            logger.debug(f"Could not read foreground window title: {e}")
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
            logger.exception(f"[HISTORY] log failed: {e}")

    def _notify_main_window(self, text):
        """Direct callback into the hub window (no event bus).

        Updates its 'last transcription' status preview and refreshes the
        history list without waiting for the next 5s poll. Best-effort:
        the hub is optional, so any failure here is swallowed.
        """
        win = getattr(self, 'main_window', None)
        if win is not None:
            try:
                win.on_dictation_complete(text)
            except Exception as e:
                logger.exception(f"[UI] main window notify failed: {e}")
        history_window = getattr(self, '_history_qt', None)
        if history_window is not None:
            try:
                history_window.refresh()
            except Exception as e:
                logger.exception(f"[UI] standalone history refresh failed: {e}")

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
                    logger.debug(f"[MIC] Reconciled '{stored_name}': index {old_idx} -> {mic['id']}")
                return  # found — whether index changed or not, we're done

        logger.debug(f"[MIC] Selected device '{stored_name}' not currently available "
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
        logger.info(f"[OK] Switched to microphone: {mic_name} ({self.capture_rate}Hz)")

        # Restart ACE engine on new device — bumps device_epoch so any
        # in-flight consumer sees the discontinuity via frame.device_epoch.
        if self._ace_engine is not None:
            try:
                self._ace_engine.bump_device_epoch()
                self._ace_engine.stop()
                self._ace_engine._config['microphone']      = mic_id
                self._ace_engine._config['microphone_name'] = self.config.get('microphone_name')
                self._ace_engine._config['_capture_rate']   = self.capture_rate
                self._ace_engine.start()
                logger.debug("[ACE] Engine restarted on new device")
            except Exception as exc:
                logger.exception(f"[ACE] Engine restart on mic switch failed: {exc}")

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
            logger.info("[INIT] Loading Whisper model...")
            
            # Determine compute device with detailed logging
            device = self.config['device']

            # Safety net: if config says CUDA but the runtime DLLs aren't
            # present (e.g. user installed CPU-only build, or moved CUDA pack
            # away), fall back to CPU silently rather than crashing at model
            # load time with "cublas64_12.dll not found".
            from samsara.cuda_detect import (
                cuda_status_message,
                is_cuda_available,
                resolve_device,
            )
            if device == "cuda" and not is_cuda_available():
                logger.warning(
                    "[GPU] Config requested CUDA but it is unavailable — %s "
                    "Falling back to CPU.",
                    cuda_status_message(),
                )
                device = "cpu"

            if device == "auto":
                try:
                    import ctranslate2
                    cuda_available = 'cuda' in ctranslate2.get_supported_compute_types('cuda')
                    if cuda_available:
                        device = "cuda"
                        logger.debug("[GPU] CUDA available via ctranslate2")
                    else:
                        device = "cpu"
                        logger.debug("[CPU] CUDA not available, using CPU")
                except Exception as e:
                    device = "cpu"
                    logger.exception(f"[CPU] Could not detect GPU: {e}")
            
            compute_type = "float16" if device == "cuda" else "int8"
            logger.info(f"[CONFIG] Model: {self.config['model_size']}, Device: {device}, Compute: {compute_type}")
            
            load_start = time.time()
            self.model = _create_whisper_model(
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
            logger.info(f"[OK] Model loaded in {load_time:.1f}s ({device}, {compute_type})")

            # Marshal to UI thread: close the splash now that the app is
            # truly ready to dictate. Until this point, the splash has been
            # showing "Loading speech model..." which is accurate.
            try:
                if self.splash:
                    self._schedule_ui(self._close_splash_post_load)
            except Exception as e:
                logger.exception(f"[SPLASH] Could not close splash: {e}")

            _boot_log = getattr(self, '_boot_log', lambda s: None)
            logger.info("[INIT] Loading Silero VAD...")
            # Load Silero VAD for real-time speech gating (async-safe: if this
            # fails, the wake callback falls back to RMS).
            self._load_vad_model()
            _boot_log("async: Silero VAD load")

            logger.info("[INIT] Loading OpenWakeWord pre-filter...")
            self._load_oww_model()
            self._load_wake_profile_models()
            _boot_log("async: OpenWakeWord model load")

            logger.info("Ready for dictation.")

            # Auto-start modes that require always-on listening
            mode = self.config.get('mode', 'hold')
            if mode == 'continuous':
                logger.info("[AUTO] Starting continuous mode...")
                self.start_continuous_mode()

            logger.info("[INIT] Starting audio streams...")
            # Hold/toggle: ACE engine ring provides rolling pre-buffer (ACE-03).
            # No separate prebuffer PortAudio stream needed at startup.

            # Auto-start wake word listener if enabled (works alongside any mode)
            if self.config.get('wake_word_enabled', False):
                logger.info("[AUTO] Starting wake word listener...")
                self.start_wake_word_mode()
            _boot_log("async: wake word + audio stream start")

            # Auto-start gesture lane if enabled
            if self.config.get('gesture', {}).get('enabled', False):
                self._start_gesture_lane()

            # Warm the local Ollama model (if Smart Corrections resolves to
            # it) so the first real correction call doesn't also pay a
            # cold-start model-load penalty. Fire-and-forget, own thread.
            try:
                smart_corrections_warm_up(self)
            except Exception as e:
                logger.debug(f"[SMART] warm_up call failed: {e}")

            logger.info("[INIT] Startup complete.")

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

        thread = thread_registry.spawn("dictation.load", load, daemon=True)
    
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
        except Exception as e:
            logger.debug(f"Key name normalization failed: {e}")
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
            except Exception as e:
                logger.debug(f"is_pressed check failed for {key!r}: {e}")
        return '+'.join(pressed) if pressed else 'none'

    def _hands_free_dictation_commit_available(self) -> bool:
        """Whether the local commit key may paste a buffered thought now.

        This deliberately does not construct a session manager: outside an
        already-active latched hands-free session the key is inert.
        """
        manager = self._session_mode_manager
        return bool(
            self.command_mode_active
            and self.config.get('command_mode', {}).get('mode', 'hold') == 'toggle'
            and manager is not None
            and manager.mode is SessionMode.DICTATE
            and manager.buffer_dictate_until_commit
            and manager.dictate_pending_buffer
        )

    def _commit_pending_hands_free_dictation(self):
        """Commit through SessionModeManager's transactional paste path."""
        if not self._hands_free_dictation_commit_available():
            logger.debug('[HOTKEY] Paste staged thought ignored: no active buffered thought')
            return None

        manager = self._session_mode_manager
        try:
            outcome = manager.commit_pending_dictation()
            logger.info(
                '[SESSION] local dictate commit outcome=%s detail=%s',
                outcome.kind,
                outcome.detail,
            )
            self._handle_session_dispatch_outcome(outcome, "")
            return outcome
        except Exception as exc:
            logger.exception('[SESSION] Local dictate commit failed unexpectedly: %s', exc)
            try:
                self.play_sound('error')
            except Exception as sound_exc:
                logger.debug('[SESSION] Local commit error earcon failed: %s', sound_exc)
            return None

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
                logger.debug("[HOTKEY] Ignored re-trigger while stop in flight")
                return
            logger.debug(f"[HOTKEY] Command hotkey detected: {command_hotkey}")
            self.hotkey_pressed = True
            self.command_mode_recording = True
            self.start_recording(streaming=False)
            return
        
        # Undo hotkey (works in any mode, edge-triggered)
        undo_hotkey = self.config.get('undo_hotkey', 'ctrl+alt+z')
        if self.check_hotkey_state(undo_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Undo hotkey detected: {undo_hotkey}")
            self.hotkey_pressed = True
            thread_registry.spawn("dictation.undo_last_dictation", self.undo_last_dictation, daemon=True)
            return

        # Correction report hotkey (works in any mode, edge-triggered)
        correction_hotkey = self.config.get('correction_hotkey', 'ctrl+alt+r')
        if self.check_hotkey_state(correction_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Correction hotkey detected: {correction_hotkey}")
            self.hotkey_pressed = True
            self._schedule_ui(self._report_correction_dialog)
            return

        # Correction CAPTURE hotkey (works in any mode, edge-triggered) --
        # opens samsara/ui/correction_capture_qt.py pre-filled with the last
        # dictation. Distinct from correction_hotkey above (the older
        # threshold-based "Report Correction" dialog).
        capture_correction_hotkey = self.config.get('hotkeys', {}).get('capture_correction', 'ctrl+alt+x')
        if self.check_hotkey_state(capture_correction_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Capture correction hotkey detected: {capture_correction_hotkey}")
            self.hotkey_pressed = True
            thread_registry.spawn(
                "dictation.open_correction_capture", self.open_correction_capture, daemon=True)
            return

        # Check for wake word enable/disable toggle (works in any mode)
        if self.check_hotkey_state(wake_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Wake word hotkey detected: {wake_hotkey}")
            self.hotkey_pressed = True
            new_state = not self.config.get('wake_word_enabled', False)
            thread_registry.spawn("dictation.set_wake_word_enabled", self.set_wake_word_enabled,
                                   args=(new_state,), daemon=True)
            return

        # Trusted local equivalent of saying the sole commit word in the
        # buffered hands-free DICTATE lane. It never starts/stops audio and is
        # inert in hold/toggle recording, COMMAND/AVA, and continuous mode.
        dictate_commit_hotkey = self.config.get(
            'dictate_commit_hotkey', DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
        )
        if (self._hands_free_dictation_commit_available()
                and self.check_hotkey_state(dictate_commit_hotkey)
                and not self.hotkey_pressed):
            logger.debug('[HOTKEY] Paste staged thought detected: %s', dictate_commit_hotkey)
            self.hotkey_pressed = True
            thread_registry.spawn(
                'dictation.commit_pending_hands_free',
                self._commit_pending_hands_free_dictation,
                daemon=True,
            )
            return
        
        # Check for continuous mode toggle (works in any mode)
        if self.check_hotkey_state(cont_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Continuous mode hotkey detected: {cont_hotkey}")
            self.hotkey_pressed = True
            self.toggle_continuous_mode()
            return

        # Continuous-mode manual commit hotkey. ONLY live while continuous
        # mode is actually running with trigger == "key" -- never in
        # hold/toggle modes, and never in continuous mode's default
        # "silence" trigger.
        if (mode == 'continuous' and self.continuous_active
                and self.config.get('continuous_commit_trigger', DEFAULT_CONTINUOUS_COMMIT_TRIGGER) == 'key'):
            commit_hotkey = self.config.get('continuous_commit_hotkey', DEFAULT_CONTINUOUS_COMMIT_HOTKEY)
            if self.check_hotkey_state(commit_hotkey) and not self.hotkey_pressed:
                logger.debug(f"[HOTKEY] Continuous commit hotkey detected: {commit_hotkey}")
                self.hotkey_pressed = True
                if self._continuous_consumer is not None:
                    self._continuous_consumer.commit_now()
                return

        # Check for cancel recording hotkey (only when recording)
        if self.check_hotkey_state(cancel_hotkey) and self.recording:
            logger.debug(f"[HOTKEY] Cancel hotkey detected: {cancel_hotkey}")
            self.cancel_recording()
            return

        # Check for alarm hotkeys (when an alarm is nagging)
        if hasattr(self, 'alarm_manager') and self.alarm_manager.is_nagging():
            complete_hotkey = self.alarm_manager.complete_hotkey
            dismiss_hotkey = self.alarm_manager.dismiss_hotkey
            
            # Check for complete hotkey (user did the task, gets streak credit)
            if self.check_hotkey_state(complete_hotkey):
                logger.debug(f"[HOTKEY] Alarm complete hotkey detected: {complete_hotkey}")
                self.alarm_manager.complete()
                self.play_sound('success')  # Success sound for completion
                return
            
            # Check for dismiss hotkey (just silence, no credit, breaks streak)
            if self.check_hotkey_state(dismiss_hotkey):
                logger.debug(f"[HOTKEY] Alarm dismiss hotkey detected: {dismiss_hotkey}")
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
            if self.recording or getattr(self, '_streaming_session', None) is not None:
                logger.info("[HOTKEY] Main hotkey ignored -- another recording owns capture")
                return
            if self._stop_in_flight:
                logger.debug("[HOTKEY] Ignored re-trigger while stop in flight")
                return
            logger.debug(f"[HOTKEY] Main hotkey detected: {main_hotkey} (mode: {mode})")
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
                    logger.debug(f"[HOTKEY] Command hotkey released, stopping recording")
                    self._stop_in_flight = True
                    thread_registry.spawn('stop-rec', _deferred_stop, daemon=True)
                    self.hotkey_pressed = False
                elif mode == 'hold' and self.recording:
                    logger.debug(f"[HOTKEY] Main hotkey released, stopping recording")
                    self._stop_in_flight = True
                    thread_registry.spawn('stop-rec', _deferred_stop, daemon=True)
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
            logger.error(f"[CAPSLOCK] Failed to install hook: {e}")
            self._capslock_hook = None
            return

        import atexit
        hook_ref = self._capslock_hook

        def _cleanup_capslock_hook():
            try:
                keyboard.unhook(hook_ref)
            except Exception as e:
                logger.debug(f"[CAPSLOCK] atexit unhook failed: {e}")

        atexit.register(_cleanup_capslock_hook)

    def _uninstall_capslock_hook(self):
        """Release the CapsLock hook so the OS gets the key back. Called
        when streaming_mode is toggled off so CapsLock works normally."""
        if getattr(self, '_capslock_hook', None) is None:
            return
        try:
            keyboard.unhook(self._capslock_hook)
            logger.info("[CAPSLOCK] Hook released — CapsLock returned to OS")
        except Exception as e:
            logger.exception(f"[CAPSLOCK] Failed to release hook: {e}")
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
                thread_registry.spawn(
                    "capslock-start", self._capslock_start_streaming, daemon=True)
            elif event.event_type == keyboard.KEY_UP:
                if not self._capslock_held:
                    return
                self._capslock_held = False
                thread_registry.spawn(
                    "capslock-stop", self._capslock_stop_streaming, daemon=True)
        except Exception as e:
            logger.exception(f"[CAPSLOCK] event handler crashed: {e}")

    def _capslock_start_streaming(self):
        """Worker: start a streaming-mode recording. Wrapped so we can
        guard against re-entry if the user hammers CapsLock."""
        try:
            with self._capslock_lifecycle_lock:
                if not self.config.get('streaming_mode', False):
                    return
                if self.recording or getattr(self, '_streaming_session', None) is not None:
                    logger.info("[CAPSLOCK] streaming start ignored -- another recording owns capture")
                    return
                logger.info("[CAPSLOCK] press -> streaming start")
                self.start_recording(streaming=True)
                self._capslock_streaming_session = getattr(
                    self, '_streaming_session', None,
                )
        except Exception as e:
            logger.exception(f"[CAPSLOCK] start failed: {e}")

    def _capslock_stop_streaming(self):
        """Worker: stop the streaming recording on CapsLock release."""
        try:
            with self._capslock_lifecycle_lock:
                owned = getattr(self, '_capslock_streaming_session', None)
                if (owned is None
                        or getattr(self, '_streaming_session', None) is not owned
                        or not self.recording):
                    return
                self._capslock_streaming_session = None
                logger.info("[CAPSLOCK] release -> streaming stop")
                self.stop_recording()
        except Exception as e:
            logger.exception(f"[CAPSLOCK] stop failed: {e}")

    # ---- Mouse 4 command mode (walkie-talkie hold-to-talk) ----------------

    def _install_mouse_listener(self):
        """Start the Win32 low-level mouse hook for Mouse 4/5 command mode.

        Only installed when command_mode.button is a mouse source.
        Keyboard sources (rctrl, f13, etc.) are handled by on_key_press/release.
        """
        cfg = self.config.get('command_mode', {})
        btn = cfg.get('button', 'rctrl')
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
            logger.info(f"[CMD MODE] Mouse hook started (suppress={suppress_btn})")
        except Exception as e:
            logger.exception(f"[CMD MODE] Mouse hook failed to start: {e}")
            self._mouse_hook = None

    def _on_command_button(self, button_name, pressed):
        """Mouse hook callback — routes the configured button to command mode."""
        cfg = self.config.get('command_mode', {})
        if not cfg.get('enabled', False):
            return
        if button_name != cfg.get('button', 'rctrl'):
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
        btn_name = cfg.get('button', 'rctrl')
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

    # ── Unified session mode state machine (COMMAND <-> DICTATE) ────────────

    def _probe_hands_free_command(self, text: str):
        """Classify one utterance without executing it.

        The SessionModeManager needs this side-effect-free probe so it can
        commit staged text before focus-changing commands, then execute the
        command only if the paste succeeded. None means ordinary dictation.
        """
        normalized = normalize_utterance(apply_phonetic_wash(text))
        if not normalized:
            return None

        dispatch_text = normalized
        policy = None
        canonical = None
        if normalized in _HANDS_FREE_PRESERVE_COMMANDS:
            policy = PendingTextPolicy.PRESERVE
        elif normalized in _HANDS_FREE_COMMIT_COMMANDS:
            policy = PendingTextPolicy.COMMIT
        elif any(normalized.startswith(prefix) for prefix in _HANDS_FREE_COMMIT_PREFIXES):
            policy = PendingTextPolicy.COMMIT
        else:
            # While Show Numbers owns the screen, a sole spoken number is an
            # implicit "click <number>" and then control returns here.
            try:
                from plugins.commands import show_numbers as _show_numbers
                tokens = normalized.split()
                number_only = bool(tokens) and all(
                    token.isdigit() or token in _show_numbers._WORD_TO_NUM
                    for token in tokens
                )
                if (_show_numbers.is_overlay_active() and number_only
                        and _show_numbers._parse_spoken_number(normalized) is not None):
                    dispatch_text = f"click {normalized}"
                    policy = PendingTextPolicy.COMMIT
            except Exception as exc:
                logger.debug(f"[SESSION] Show Numbers command probe unavailable: {exc}")

        # Every enabled command and user macro is available when (and only
        # when) it consumes the entire utterance. Unknown command types commit
        # pending text first because they may move focus, submit, launch a
        # process, or invalidate the current target. Curated navigation above
        # retains its more precise PRESERVE/COMMIT policy.
        if policy is None:
            command_executor = getattr(self, 'command_executor', None)
            find_exact = getattr(command_executor, 'find_exact_command', None)
            if find_exact is not None:
                canonical = find_exact(dispatch_text)
                if canonical is not None:
                    policy = PendingTextPolicy.COMMIT

        if policy is None:
            return None
        if canonical is None:
            canonical = self.command_executor.find_command(dispatch_text)
        if canonical is None:
            # Disabled command pack or stale/unavailable command: preserve the
            # user's words as dictation instead of pretending an action exists.
            return None
        return HandsFreeCommandMatch(
            dispatch_text=dispatch_text,
            phrase=canonical,
            pending_policy=policy,
        )

    def _ensure_session_mode_manager(self) -> "SessionModeManager":
        """Lazily build the SessionModeManager with its wired callables.

        Built once; every session entry/exit calls reset() on this same
        instance rather than reconstructing it (session end always discards
        MODE STATE -- current mode, the unit-of-work stack -- but the
        wiring itself is stable for the app's lifetime).
        """
        if self._session_mode_manager is not None:
            return self._session_mode_manager

        def _command_dispatch_fn(text: str) -> CommandDispatchResult:
            audio_duration = getattr(self, '_current_utterance_duration_s', 0.0)
            # SessionModeManager calls this only after committing an utterance
            # to either the legacy COMMAND lane or the curated exact-command
            # branch of the combined hands-free lane. The preference governing
            # opportunistic matching during ordinary dictation does not apply.
            result, was_command = self.command_executor.process_text(
                text, self, force_commands=True,
            )
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
                if (self.command_mode_active
                        and (self._session_mode_manager is None
                             or self._session_mode_manager.mode is SessionMode.COMMAND)):
                    self._command_mode_miss_count = 0
                    # Inactivity timer reset happens once, uniformly, in
                    # _handle_session_dispatch_outcome (the single chokepoint)
                    # after dispatch_utterance returns -- not duplicated here.
                return CommandDispatchResult(matched=True, phrase=result)

            logger.info(f'[CMD] No command matched: "{text}"')
            if (self.command_mode_active
                    and (self._session_mode_manager is None
                         or self._session_mode_manager.mode is SessionMode.COMMAND)):
                self._command_mode_miss_count += 1
                cm_cfg = self.config.get('command_mode', {})
                miss_limit = cm_cfg.get('miss_limit', 5)
                if self._command_mode_miss_count >= miss_limit:
                    logger.info(f'[CMD MODE] Miss limit ({miss_limit}) reached')
                    self.exit_command_mode()
            return CommandDispatchResult(matched=False, phrase=None)

        def _inject_fn(text: str, commit_focus_guard=None):
            """Commit one complete unified-session DICTATE thought.

            `text` is the complete, RAW, multi-chunk accumulated buffer --
            natural-pause chunks were staged with no formatting applied
            (see session_modes._stage_dictate_chunk). This runs the SAME
            formatting pipeline normal wake-lane dictation uses
            (_output_dictation), in the same order, exactly once, over the
            complete text -- never duplicated, never per-chunk:

                process_transcription (auto_capitalize, format_numbers)
                -> clean_text (cleanup_mode, incl. verbatim -- unchanged)
                -> smart_correct (gated on smart_corrections.modes.wake,
                   the DICTATE/wake lane this session path belongs to)
                -> add_trailing_space
                -> _apply_formatting_tokens (must run last, immediately
                   before delivery -- see formatting_tokens.py)

            Returns the delivered (pasted) text on success, so
            SessionModeManager's undo stack and stage_buffer record what was
            ACTUALLY typed rather than the pre-formatting input. Returns
            False on paste failure OR any unexpected exception during
            formatting -- either way the caller retains the pending buffer,
            stays in DICTATE, and plays the existing error earcon.
            """
            try:
                raw = text  # pre-pipeline accumulated thought, for history's raw_text
                formatted = self.process_transcription(text)
                cleanup_mode = (
                    'verbatim' if getattr(self, '_skip_cleanup', False)
                    else self.config.get('cleanup_mode', 'clean')
                )
                formatted = clean_text(formatted, mode=cleanup_mode)
                if self.config.get('smart_corrections', {}).get('modes', {}).get('wake', True):
                    formatted = smart_correct(formatted, self)
                if self.config['add_trailing_space']:
                    formatted = formatted + " "
                formatted = self._apply_formatting_tokens(formatted)
            except Exception as e:
                logger.exception(f'[SESSION] DICTATE commit formatting failed: {e}')
                return False

            paste_ok = self._paste_preserving_clipboard(
                formatted,
                before_paste=commit_focus_guard,
            )
            if not paste_ok:
                return False

            display = formatted.strip()
            if display:
                self.add_to_history(display, is_command=False)
                self._log_history(
                    raw_text=raw,
                    display_text=display,
                    mode="dictate",
                    status="success",
                    entry_type="dictation",
                )
                self._notify_main_window(display)
            return formatted

        def _remove_chars_fn(n: int) -> None:
            # Same select-back + delete idiom as undo_last_dictation() --
            # reused, not new injection code.
            for _ in range(n):
                pyautogui.hotkey('shift', 'left')
            pyautogui.press('delete')

        _MODE_EARCONS = {
            SessionMode.COMMAND: 'mode_command',
            SessionMode.DICTATE: 'mode_dictate',
            SessionMode.AVA: 'mode_ava',
        }

        def _on_mode_change(mode: "SessionMode") -> None:
            self.play_sound(_MODE_EARCONS.get(mode, 'mode_command'))
            self._update_mode_overlay(mode)

        def _on_focus_lock_revert() -> None:
            logger.info('[SESSION] Focus-lock mismatch -- foreground window changed; '
                        'suppressing injection and retaining DICTATE mode')
            self.play_sound('focus_lock_revert')
            self._update_mode_overlay(SessionMode.DICTATE)

        def _on_scratch_result(success: bool) -> None:
            self.play_sound('scratch_success' if success else 'scratch_refuse')

        def _on_abort() -> None:
            logger.info('[SESSION] Global abort phrase -- exiting command mode')
            self.exit_command_mode()

        def _on_switch_dispatch_error(exc: Exception) -> None:
            logger.error(f'[SESSION] Prefix-switch payload dispatch failed, mode reverted: {exc}')
            self.play_sound('error')

        ww_cfg = self.config.get('wake_word_config', {})
        configured_abort = ww_cfg.get(
            'wake_abort_phrase', ['cancel', 'cancel dictation', 'abort'],
        )
        if isinstance(configured_abort, str):
            configured_abort = [configured_abort]
        abort_phrases = list(dict.fromkeys([
            *configured_abort,
            *GLOBAL_SESSION_EXIT_PHRASES,
        ]))

        self._session_mode_manager = SessionModeManager(
            abort_phrases=abort_phrases,
            foreground_exe_resolver=_get_foreground_exe_lower,
            foreground_hwnd_resolver=_get_foreground_hwnd,
            inject_fn=_inject_fn,
            format_dictate_fn=self._apply_formatting_tokens,
            remove_chars_fn=_remove_chars_fn,
            command_dispatch_fn=_command_dispatch_fn,
            agent_dispatch_fn=self._ava_session_agent_dispatch_fn,
            on_mode_change=_on_mode_change,
            on_focus_lock_revert=_on_focus_lock_revert,
            on_scratch_result=_on_scratch_result,
            on_abort=_on_abort,
            on_switch_dispatch_error=_on_switch_dispatch_error,
            buffer_dictate_until_commit=True,
            hands_free_command_probe_fn=getattr(
                self, '_probe_hands_free_command', None,
            ),
        )
        return self._session_mode_manager

    def _ava_session_agent_dispatch_fn(self, text: str, context: "str | None") -> None:
        """Wired into SessionModeManager as agent_dispatch_fn (SessionMode.AVA).

        Builds the prompt (with a delimited STAGED TEXT block when the
        stage-buffer was explicitly referenced) and hands off to the SAME
        agent pipeline hold-to-talk Ava uses -- plugins.commands.ask_ollama.
        handle_ask_ava -- no second agent client. Concurrent AVA-mode
        utterances are serialized through a depth-3 queue (oldest dropped)
        so at most one request is ever in flight; this function itself never
        blocks the caller (the utterance-processing thread).
        """
        if self._try_cancel_pending_ava_utterance(text):
            return
        payload_text = f"STAGED TEXT:\n{context}\n\n{text}" if context else text

        with self._ava_session_dispatch_lock:
            if self._ava_session_request_in_flight:
                if len(self._ava_session_dispatch_queue) >= self._ava_session_dispatch_queue.maxlen:
                    logger.info('[AVA-SESSION] Queue full (3) -- dropping oldest queued utterance')
                    # No existing queue-warning earcon in this codebase --
                    # reuse scratch_refuse (Phase 1's "this didn't go
                    # through" sound) rather than adding a new asset.
                    self.play_sound('scratch_refuse')
                self._ava_session_dispatch_queue.append(payload_text)
                return
            self._ava_session_request_in_flight = True

        self._start_ava_session_worker(payload_text)

    def _start_ava_session_worker(self, payload_text: str) -> None:
        from plugins.commands.ask_ollama import handle_ask_ava
        handle_ask_ava(self, remainder=payload_text, on_done=self._on_ava_session_request_done)

    def _on_ava_session_request_done(self) -> None:
        """handle_ask_ava's on_done hook -- fires exactly once per request
        (early-exit or full worker cycle). Drains the next queued utterance
        if any, else clears the in-flight flag.

        Also touches the session-activity chokepoint: a slow agent response
        must not let the inactivity timer expire out from under a user who
        is still mid-conversation with Ava, waiting on an answer that
        hasn't landed yet."""
        self._touch_session_activity()
        with self._ava_session_dispatch_lock:
            if self._ava_session_dispatch_queue:
                next_text = self._ava_session_dispatch_queue.popleft()
            else:
                self._ava_session_request_in_flight = False
                return
        self._start_ava_session_worker(next_text)

    # Session mode badge (COMMAND/DICTATE/AVA) accent colors. Lives on the
    # listening indicator pill -- NEVER on samsara.ui.status_overlay (the
    # Reminders & Alarms window). That window must never be shown, hidden,
    # or otherwise touched by session code; an earlier version wired the
    # badge there and every mode transition popped/hid the user's Reminders
    # window as a side effect.
    _MODE_OVERLAY = {
        SessionMode.COMMAND: ("COMMAND", "#5EEAD4"),
        SessionMode.DICTATE: ("HANDS FREE", "#f59e0b"),
        SessionMode.AVA: ("AVA", "#A78BFA"),
    }

    def _update_mode_overlay(self, mode: "SessionMode") -> None:
        name, color = self._MODE_OVERLAY.get(mode, ("COMMAND", "#5EEAD4"))
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_session_mode, name, color)

    def enter_command_mode(self):
        """Enter command mode (idempotent). Safe to call from any thread."""
        with self._command_mode_lock:
            if self.command_mode_active or self.ava_mode_active:
                return
            self.command_mode_active = True
        self._command_mode_miss_count = 0
        self._command_mode_session_start = time.monotonic()
        self._command_mode_ghost_tap = False
        logger.info("[CMD MODE] Entering command mode")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, True)
        cfg = self.config.get('command_mode', {})
        if cfg.get('mode', 'hold') == 'toggle':
            timeout_s = cfg.get('inactivity_timeout_s', 300)
            self._reset_command_mode_inactivity_timer(timeout_s)
            # The latched toggle is the combined hands-free session: exact
            # navigation commands and buffered dictation coexist from entry.
            # Hold-to-record command mode never enters this branch.
            self._ensure_session_mode_manager().reset(initial_mode=SessionMode.DICTATE)
            if hasattr(self, 'listening_indicator'):
                # Force-visible for the session's duration regardless of
                # listening_indicator_enabled -- restored to the
                # config-controlled state in exit_command_mode().
                self._schedule_ui(self.listening_indicator.show)
            self._update_mode_overlay(SessionMode.DICTATE)
        thread_registry.spawn('cmd-mode-enter', self._do_enter_command_mode, daemon=True)

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
        with self._command_mode_lock:
            if not self.command_mode_active or self.recording:
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
            logger.info(f"[CMD MODE] Ghost tap ({hold_ms:.0f}ms < {debounce_ms}ms) — audio will be discarded")
        logger.info("[CMD MODE] Exiting command mode")
        self._cancel_command_mode_inactivity_timer()
        # Session end discards all state. A future toggle entry chooses the
        # combined hands-free lane; reset's COMMAND default remains for legacy
        # direct callers and the optional command-only lane.
        if self._session_mode_manager is not None:
            self._session_mode_manager.reset()
        is_toggle_session = self.config.get('command_mode', {}).get('mode', 'hold') == 'toggle'
        if hasattr(self, 'listening_indicator'):
            if is_toggle_session:
                # Clear the session badge and restore whatever visibility
                # listening_indicator_enabled calls for -- it was
                # force-visible only for the session's duration (see
                # enter_command_mode). The Reminders & Alarms window
                # (status_overlay.py) is never touched here.
                self._schedule_ui(self.listening_indicator.set_session_mode, None, None)
                if not self.config.get('listening_indicator_enabled', False):
                    self._schedule_ui(self.listening_indicator.hide)
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
        logger.info("[AVA MODE] Entering Ava mode")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, True)
        thread_registry.spawn('ava-mode-enter', self._do_enter_ava_mode, daemon=True)

    def _do_enter_ava_mode(self):
        """Worker thread: starts recording and fires debounced earcon."""
        with self._ava_mode_lock:
            if not self.ava_mode_active or self.recording:
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
            logger.info(f"[AVA MODE] Ghost tap ({hold_ms:.0f}ms < {debounce_ms}ms) — audio will be discarded")
        logger.info("[AVA MODE] Exiting Ava mode")
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
        logger.info("[AI-CMD] Entering AI command mode")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, True)
        thread_registry.spawn('ai-cmd-enter', self._do_enter_ai_command_mode, daemon=True)

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
        logger.info("[AI-CMD] Exiting AI command mode")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, False)
        try:
            from samsara.ai_command_mode import cancel_queue, reset_cancel  # noqa: PLC0415
            cancel_queue()
            reset_cancel()
        except Exception as e:
            logger.debug(f"[AI-CMD] Queue cancel/reset on exit failed: {e}")
        self.play_sound('stop')

    def _handle_ai_command_utterance(self, buffer: list, src_rate: int) -> None:
        """Transcribe one VAD-gated utterance and push to the AI command queue.

        Called from WakeConsumer._flush() while ai_command_mode_active.
        Shares _wake_transcription_in_progress with _handle_command_mode_utterance
        to prevent concurrent transcriptions.
        Stop-words are checked before enqueue so cancel is always responsive.
        """
        if not self._ai_cmd_ready.wait(timeout=60):
            logger.info('[AI-CMD-UTT] Ready timeout -- dropping utterance')
            return
        if self._wake_transcription_in_progress:
            logger.info('[AI-CMD-UTT] Transcription in progress -- skipping')
            return
        self._wake_transcription_in_progress = True
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, src_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate
            if audio_duration < 0.3:
                return
            logger.info(f'[AI-CMD-UTT] Transcribing {audio_duration:.1f}s')
            # NOT forced to English: this content is a natural-language
            # query routed to the AI/Ava queue, not matched against the
            # command registry -- use the configured dictation language.
            # The English _STOP_WORDS gate below is best-effort in
            # non-English (commands/control-words remain English-only).
            transcribe_params = self.get_transcription_params()
            transcribe_params['vad_filter'] = False
            with self.model_lock:
                segments, _ = self.model.transcribe(audio, **transcribe_params)
            text = ''.join(s.text for s in segments).strip()
            text = self.voice_training_window.apply_corrections(text)
            if not text:
                return
            logger.info(f'[AI-CMD-UTT] "{text}"')
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
            logger.exception(f'[AI-CMD-UTT] Error: {exc}')
            import traceback  # noqa: PLC0415
            traceback.print_exc()
        finally:
            self._wake_transcription_in_progress = False
            self._vad_reset()

    def _try_cancel_pending_ava_utterance(self, text: str) -> bool:
        """Cancel Ava state only for an exact cancel utterance while pending."""
        if not _is_pending_cancel_utterance(text):
            return False
        try:
            from plugins.commands import ask_ollama
            if ask_ollama.get_pending_action() is None:
                return False
            ask_ollama.handle_ava_cancel(self)
            return True
        except Exception as exc:
            logger.exception(f"[AVA] Pending-action cancellation failed: {exc}")
            self.play_sound('error')
            return True

    def _try_cancel_pending_wake_command(self, text: str) -> bool:
        """Cancel the Jarvis command-wait state on an exact cancel utterance."""
        if not _is_pending_cancel_utterance(text):
            return False
        self._try_cancel_pending_ava_utterance(text)
        timer = getattr(self, 'wake_word_timer', None)
        if timer is not None:
            timer.cancel()
            self.wake_word_timer = None
        self.wake_word_triggered = False
        self.app_state = 'asleep'
        logger.info('[WAKE] Pending command cancelled by exact nevermind utterance')
        self._indicator_reset()
        self._emit_wake_trace({
            "stage": "utterance_end",
            "result": "pending_command_cancelled",
        })
        return True

    def _route_to_ava(self, text: str):
        """Send transcribed speech to Ava -- but try it as a literal voice
        command first, through the SAME registry/dispatch wake word mode
        uses (_process_wake_command), so an exact phrase like "show numbers"
        executes its plugin handler instead of being swallowed whole into
        an LLM prompt Ava has no way to resolve. force_commands=True mirrors
        wake word mode: Ava mode is not the command_matching_enabled toggle
        feature, so a recognized command always dispatches here regardless
        of that setting. Only text that doesn't match anything registered
        falls through to Ollama below, unchanged."""
        if self._try_cancel_pending_ava_utterance(text):
            return
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
            self.add_to_history(text, is_command=True)
            self._log_history(
                raw_text=text,
                mode="command",
                status="success",
                entry_type="command",
                matched_command=str(result) if result else None,
            )
            return

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
                    logger.debug(f"[AVA] Ollama plugin not found. User said: {text}")
            except Exception as e:
                logger.exception(f"[AVA] Error: {e}")
                if hasattr(self, 'audio_coordinator') and self.audio_coordinator:
                    self.audio_coordinator.speak(
                        "Sorry, I had an error processing that.",
                        category="error",
                    )
        thread_registry.spawn("Ava-worker", _worker, daemon=True)

    def _reset_command_mode_inactivity_timer(self, timeout_s):
        with self._command_mode_timer_lock:
            self._cancel_command_mode_inactivity_timer_locked()
            t = thread_registry.timer(
                "dictation.command_mode_inactivity", timeout_s,
                self._on_command_mode_inactivity, daemon=True)
            self._command_mode_inactivity_timer = t

    def _cancel_command_mode_inactivity_timer(self):
        with self._command_mode_timer_lock:
            self._cancel_command_mode_inactivity_timer_locked()

    def _cancel_command_mode_inactivity_timer_locked(self):
        """Caller must hold _command_mode_timer_lock. Cancel-then-clear is
        the only mutation of _command_mode_inactivity_timer -- every reset
        and every cancel goes through this, under the lock, so concurrent
        activity signals (e.g. an AVA on_done firing on the Ava-worker
        thread at the same moment a fresh command-mode utterance is
        dispatched on its own per-utterance thread) can never race and leak
        a second live timer."""
        t = self._command_mode_inactivity_timer
        if t is not None:
            t.cancel()
            self._command_mode_inactivity_timer = None

    def _touch_session_activity(self) -> None:
        """Single chokepoint for the unified toggle-command-mode session's
        inactivity timer. Every activity signal -- a matched or missed
        command, a DICTATE chunk, an AVA utterance dispatch (substantive or
        not), any mode transition, scratch-that, abort, and AVA
        agent-response completion (_on_ava_session_request_done) -- resets
        the SAME timer here instead of each lane rolling its own reset.
        A discarded near-silence buffer never reaches this method at all:
        WakeConsumer drops those before _handle_command_mode_utterance is
        ever invoked, so genuinely idle silence still times out normally.

        No-ops while _session_recovery_pause is set -- an announced device
        outage must not have some other in-flight signal quietly re-arm the
        timer out from under the deliberate pause below."""
        if not self.command_mode_active:
            return
        if self._session_recovery_pause:
            return
        cm_cfg = self.config.get('command_mode', {})
        if cm_cfg.get('mode', 'hold') != 'toggle':
            return
        timeout_s = cm_cfg.get('inactivity_timeout_s', 300)
        self._reset_command_mode_inactivity_timer(timeout_s)

    def _pause_session_inactivity_for_device_recovery(self) -> None:
        """Cancel the inactivity timer for the duration of an announced ACE
        device outage -- a user waiting for their mic to reconnect must
        never have the session silently exit out from under them just
        because no utterances could possibly arrive while the stream is
        down. Does not touch command_mode_active; the session stays
        latched exactly as the recovery contract requires."""
        self._session_recovery_pause = True
        self._cancel_command_mode_inactivity_timer()

    def _resume_session_inactivity_after_device_recovery(self) -> None:
        """Clear the recovery pause and re-arm the timer with a fresh full
        window -- called on both recovery success and give-up, since either
        way the outage is now "announced and over" from the session's
        perspective."""
        self._session_recovery_pause = False
        self._touch_session_activity()

    def _on_command_mode_inactivity(self):
        """threading.Timer callback -- runs on its own thread. Must never
        raise uncaught: a raise here would leave the session latched
        (command_mode_active still True) but with its only path back to
        COMMAND-mode listening broken -- deaf but latched, a zombie
        session. On any failure inside exit_command_mode(), force the same
        end-state directly (flip the flag, cancel the timer, reset mode
        state) so the session provably ends rather than hanging."""
        try:
            logger.info("[CMD MODE] Inactivity timeout — exiting command mode")
            self.exit_command_mode()
        except Exception as exc:
            logger.exception(f"[CMD MODE] Inactivity handler failed: {exc} -- forcing session end")
            try:
                self.play_sound('error')
            except Exception as e:
                logger.debug(f"[CMD MODE] Error earcon failed: {e}")
            with self._command_mode_lock:
                self.command_mode_active = False
            self._cancel_command_mode_inactivity_timer()
            if self._session_mode_manager is not None:
                try:
                    self._session_mode_manager.reset()
                except Exception as e:
                    logger.debug(f"[CMD MODE] Session mode manager reset failed during forced end: {e}")

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
        """Transcribe and dispatch one queued VAD-gated utterance in the
        unified toggle-command-mode session (COMMAND <-> DICTATE).

        WakeConsumer owns a single FIFO drain worker for these utterances, so
        this method is never invoked concurrently and every silence-bounded
        chunk is handled in capture order. The model lock remains the final
        cross-pipeline serialization guard.

        Called from WakeConsumer._flush() for each silence-bounded utterance
        while command_mode_active and mode=='toggle'.  The WakeConsumer resets
        its utterance buffer after calling _flush(), so it re-arms automatically
        for the next utterance — no explicit re-arm is needed here.

        Two timeouts are in play (do not conflate):
          utterance_silence_s / dictate_utterance_silence_s (WakeConsumer,
              picked by current SessionMode) -- ends THIS utterance
          inactivity_timeout_s (300 s by default, threading.Timer) -- ends the whole session

        Dispatch itself (abort phrase, "scratch that", switch words, and
        per-mode handling) lives in SessionModeManager -- this method's job
        is just: transcribe, compute the hallucination-gate signals the
        switch matcher needs, and hand the text off.
        """
        if self._wake_transcription_in_progress:
            logger.info('[CMD-UTT] Another transcription is active — waiting on model lock')
        self._wake_transcription_in_progress = True
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, src_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate

            if audio_duration < 0.3:
                return

            logger.debug(f'[CMD-UTT] Transcribing {audio_duration:.1f}s utterance')

            transcribe_params = self.get_transcription_params()
            transcribe_params['vad_filter'] = False
            # Command-mode utterances (mode==COMMAND) are matched against the
            # English command registry AND control words (switch/scratch/
            # abort, checked by SessionModeManager on every utterance
            # regardless of mode) -- force English there. DICTATE/AVA use
            # the configured dictation language; control-word recognition
            # during those sub-modes is best-effort in non-English (commands
            # remain English-only by design).
            manager = self._ensure_session_mode_manager()
            if manager.mode is SessionMode.COMMAND:
                transcribe_params['language'] = 'en'

            with self.model_lock:
                segments, _ = self.model.transcribe(audio, **transcribe_params)
            seg_list = list(segments)
            text = ''.join(s.text for s in seg_list).strip()
            text = self.voice_training_window.apply_corrections(text)

            if not text:
                logger.debug('[CMD-UTT] Empty transcription')
                return

            logger.debug(f'[CMD-UTT] "{text}"')

            if self._command_mode_ghost_tap:
                self._command_mode_ghost_tap = False
                logger.debug('[CMD-UTT] Ghost tap — discarding')
                return

            signals = self._compute_switch_gate_signals(audio, seg_list)
            self._current_utterance_duration_s = audio_duration

            outcome = manager.dispatch_utterance(text, signals)
            logger.info(f'[SESSION] mode={manager.mode.value} outcome={outcome.kind} detail={outcome.detail}')
            self._handle_session_dispatch_outcome(outcome, text)
        except Exception as exc:
            # Any exception here (transcription error, injection failure,
            # a lane's dispatch blowing up) must earcon and leave the
            # session ALIVE in its current mode -- never propagate and kill
            # this utterance's thread silently. Mode/command_mode_active
            # are untouched, so the next utterance dispatches normally.
            logger.exception(f'[CMD-UTT] Error: {exc}')
            try:
                self.play_sound('error')
            except Exception as e:
                logger.debug(f'[CMD-UTT] Error earcon failed: {e}')
        finally:
            self._wake_transcription_in_progress = False
            self._vad_reset()

    def _handle_session_dispatch_outcome(self, outcome: "DispatchOutcome", text: str) -> None:
        """Side effects keyed on the unified session's dispatch outcome that
        don't belong inside SessionModeManager itself (earcons, the
        command-mode inactivity timer) -- split out from
        _handle_command_mode_utterance so this logic is testable without a
        full transcription pipeline.

        _touch_session_activity() is the SINGLE chokepoint for the unified
        session's inactivity timer: every outcome except "empty" (a
        discarded near-silence/blank transcription -- never activity) resets
        it here, once, regardless of which lane produced it. This replaces
        the old scattered per-lane resets (COMMAND-only, and AVA-only)."""
        if outcome.kind == "ava_rejected_not_substantive":
            # Coughs/"uh"/stray syllables that survive the hallucination
            # gates but aren't worth an agent API call + spoken reply. No
            # existing "miss" earcon in this codebase -- reuse
            # scratch_refuse (the established "this didn't go through"
            # sound; same choice already made for the AVA dispatch
            # queue-full-drop case in _ava_session_agent_dispatch_fn).
            logger.info(f'[AVA] Rejected non-substantive utterance: "{text}"')
            self.play_sound('scratch_refuse')
        elif outcome.kind == "dictate_commit_refused":
            logger.info('[SESSION] DICTATE commit word rejected by anti-hallucination gate; '
                        'pending text retained')
            self.play_sound('scratch_refuse')
        elif outcome.kind == "hands_free_command_refused":
            logger.info('[SESSION] Hands-free command rejected by anti-hallucination gate; '
                        'pending text retained')
            self.play_sound('scratch_refuse')
        elif outcome.kind == "hands_free_command_failed":
            logger.error('[SESSION] Reserved hands-free command failed to execute: %r',
                         outcome.detail)
            self.play_sound('error')
        if outcome.kind != "empty":
            self._touch_session_activity()

    def _compute_switch_gate_signals(self, audio, seg_list) -> "UtteranceSignals":
        """Compute the switch/scratch-that anti-hallucination gate signals
        from the SAME existing detectors dictation.py already uses
        (_buffer_has_contiguous_speech, per-segment compression_ratio) --
        no new DSP. Fails CLOSED (None / empty) on any error, matching the
        gate's own fail-closed contract."""
        has_contiguous_speech = None
        try:
            if self._vad_available and self._vad_model is not None:
                has_contiguous_speech = self._buffer_has_contiguous_speech(audio, self.model_rate)
        except Exception as exc:
            logger.exception(f'[SESSION] contiguous-speech gate errored (failing closed): {exc}')
            has_contiguous_speech = None

        compression_ratios = tuple(getattr(s, 'compression_ratio', None) for s in seg_list)
        # These are faster-whisper's own accepted-segment signals. Preserve
        # unavailable metadata as None so the short-commit exception fails
        # closed on older backends or incomplete test doubles.
        transcript_confident = None
        if seg_list:
            confidence_pairs = [
                (getattr(s, 'avg_logprob', None), getattr(s, 'no_speech_prob', None))
                for s in seg_list
            ]
            if all(avg is not None and no_speech is not None
                   for avg, no_speech in confidence_pairs):
                transcript_confident = all(
                    avg >= _LOGPROB_THRESHOLD and no_speech <= _NO_SPEECH_THRESHOLD
                    for avg, no_speech in confidence_pairs
                )
        return UtteranceSignals(
            has_contiguous_speech=has_contiguous_speech,
            compression_ratios=compression_ratios,
            transcript_confident=transcript_confident,
        )

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
                logger.info("Model still loading, please wait...")
            return

        self.play_sound("start", use_winsound=True)
        time.sleep(0.15)
        logger.debug("[MIC] Continuous mode ACTIVE — speak naturally, pauses will trigger transcription")

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

        logger.info("[OFF] Continuous mode STOPPED")
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
            
            # Get transcription parameters based on performance mode. NOT
            # forced to English: continuous mode always transcribes with the
            # configured dictation language -- ambient command phrases below
            # (command_executor.process_text) are matched best-effort against
            # that same transcription and simply fall through to dictation
            # output when they don't match (commands remain English-only).
            transcribe_params = self.get_transcription_params()
            # DISABLE faster-whisper's VAD for hold-to-dictate. The user
            # explicitly pressed the hotkey — all captured audio is intentional
            # speech. VAD was stripping 80% of audio, causing garbled output.
            transcribe_params['vad_filter'] = False
            perf_mode = self.config.get('performance_mode', 'balanced')
            
            # Guard: Whisper hallucinates on very short audio (<0.5s).
            # It outputs phantom phrases like "Thank you" or "Subtitles by Amara".
            if audio_duration < 0.51:
                logger.info(f"[SKIP] Audio too short ({audio_duration:.2f}s) — skipping transcription")
                return
            
            transcribe_start = time.time()
            with self.model_lock:
                segments, info = self.model.transcribe(audio, **transcribe_params)
            
            text = "".join([segment.text for segment in segments]).strip()
            transcribe_time = time.time() - transcribe_start
            
            # Performance logging
            rtf = transcribe_time / audio_duration if audio_duration > 0 else 0
            device_info = getattr(self, 'device_type', 'unknown')
            logger.debug(f"[PERF] Audio: {audio_duration:.1f}s | Transcribe: {transcribe_time*1000:.0f}ms | "
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
                        except Exception as e:
                            logger.debug(f"Tutorial command hook failed: {e}")
                    # Command was executed
                    return

                # Tutorial dictation hook — one-shot, removed after first fire
                _tut_dict = self._tutorial_hooks.pop('dictation', None)
                if _tut_dict:
                    try:
                        _tut_dict(text)
                    except Exception as e:
                        logger.debug(f"Tutorial dictation hook failed: {e}")

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

                logger.info(f"[TEXT] {text}")

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
            logger.exception(f"[ERROR] Transcription failed: {e}")
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
            except Exception as _snd_err:
                logger.debug(f"Failure earcon (winsound) unavailable: {_snd_err}")

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
                logger.info("Model still loading, please wait...")
            return

        self.play_sound("start", use_winsound=True)
        time.sleep(0.15)
        phrase = self.config.get('wake_word_config', {}).get('phrase', 'hey samsara')
        logger.info(f"[LISTEN] Wake word mode ACTIVE - say '{phrase}' to give commands")

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

        logger.info("[OFF] Wake word mode STOPPED")
        self.play_sound("stop")
        self._release_icon_chase('wake_word')
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)

    def _load_vad_model(self):
        """Load Silero VAD for real-time speech detection in the wake callback.

        Runs once after Whisper loads. The ONNX model is shipped inside
        faster-whisper, so this never contacts the network or depends on a
        torch hub cache. Any failure sets _vad_available=False and the
        callback falls back to RMS. Safe to call multiple times -- a
        successful prior load short-circuits.
        """
        if self._vad_available and self._vad_model is not None:
            return
        try:
            # Keep this lazy: importing dictation.py alone must not import the
            # heavy faster_whisper/onnxruntime stack (hermetic collection).
            from faster_whisper.utils import get_assets_path
            from faster_whisper.vad import SileroVADModel

            asset_path = Path(get_assets_path()) / "silero_vad_v6.onnx"
            if not asset_path.is_file():
                raise FileNotFoundError(f"bundled VAD asset missing: {asset_path}")

            logger.info("[BOOT-DIAG] Loading bundled Silero VAD ONNX model (local only)")
            _t = time.perf_counter()
            # Do not use faster_whisper.vad.get_vad_model(): that returns a
            # process-global singleton which Whisper may invoke concurrently
            # via vad_filter outside Samsara's lock.
            model = SileroVADModel(str(asset_path))
            _dt = (time.perf_counter() - _t) * 1000
            logger.info(f"[BOOT-DIAG] Bundled Silero VAD ONNX load returned: {_dt:.0f}ms")
            if _dt > 5000:
                logger.info(f"[BOOT-DIAG] SLOW STEP: bundled Silero VAD ONNX load {_dt:.0f}ms")
            with self._vad_lock:
                self._vad_model = model
                # Publish availability last, after both lock and model exist.
                self._vad_available = True
            logger.debug("[VAD] Bundled Silero VAD ONNX loaded for real-time speech detection")
        except Exception as e:
            with self._vad_lock:
                self._vad_model = None
                self._vad_available = False
            logger.warning(f"[VAD] Silero VAD ONNX unavailable, falling back to RMS: {e}")

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
            logger.debug(f"[OWW] Wake word pre-filter active for '{wake_phrase}'")
        else:
            logger.debug(f"[OWW] No pre-filter for '{wake_phrase}' — using Whisper detection")

    def _load_wake_profile_models(self):
        """Load OWW models for all enabled wake_profiles (Phase 1 multi-wakeword).

        Looks for custom .onnx files under samsara/wake_models/. When a model
        file is absent that profile uses Whisper-transcript matching via
        match_wake_phrase — adequate for long phrases like "hey claude" /
        "activate hermes". Drop trained .onnx files there and restart to activate
        the OWW pre-filter for those profiles.
        """
        profiles = self.config.get('wake_profiles', [])
        if not profiles:
            return

        models_dir = Path(__file__).parent / 'samsara' / 'wake_models'
        oww_threshold = float(self.config.get('wake_word_config', {}).get('oww_threshold', 0.2))

        for profile in profiles:
            if not profile.get('enabled', True):
                continue
            tid        = profile.get('id', '')
            phrase     = profile.get('phrase', '')
            model_file = profile.get('oww_model', '')
            model_path = (models_dir / model_file) if model_file else None

            if model_path and model_path.exists():
                detector = WakeWordDetector(phrase, threshold=oww_threshold,
                                            model_path=str(model_path))
                self._wake_profile_detectors[tid] = detector
                status = "OWW pre-filter active" if detector.is_available else "load failed — Whisper fallback"
                logger.debug(f"[OWW] Wake profile '{tid}' ({phrase}): {status}")
            else:
                self._wake_profile_detectors[tid] = None
                missing = f" ('{model_file}' not in wake_models/)" if model_file else ""
                logger.debug(f"[OWW] Wake profile '{tid}' ({phrase}): no model{missing} — Whisper fallback")

    def _check_wake_profiles(self, corrected_lower):
        """Match corrected transcript against all enabled wake_profiles.

        Returns the first matching profile dict, or None if no profile matched.
        Called from process_wake_word_buffer before the legacy single-phrase check.
        """
        for profile in self.config.get('wake_profiles', []):
            if not profile.get('enabled', True):
                continue
            phrase = profile.get('phrase', '').lower().strip()
            if not phrase:
                continue
            matched, _, _ = match_wake_phrase(corrected_lower, phrase)
            if matched:
                return profile
        return None

    def _dispatch_wake_profile(self, profile, corrected_lower=''):
        """Focus the target window and start a quick_dictation session.

        Called from process_wake_word_buffer when a wake_profile phrase is
        detected. Focuses (and restores if minimized) the target window via
        window_switcher._force_focus, then enters quick_dictation mode so
        the user's next utterance is typed into that window. Session ends via
        the existing silence/timeout mechanism (Phase 2 will refine this).
        """
        process_name = profile.get('target_process', '')
        phrase       = profile.get('phrase', '').lower().strip()
        tid          = profile.get('id', phrase)

        logger.info(f"[WAKE-PROFILE] '{phrase}' matched — targeting '{process_name}'")

        own_pid = os.getpid()
        result  = _resolve_target_window(process_name, exclude_pids={own_pid})

        if result is None:
            logger.info(f"[WAKE-PROFILE] No window found for '{process_name}' — process not running?")
            self.play_sound("error")
            return

        hwnd, title = result
        logger.info(f"[WAKE-PROFILE] Found window: '{title}' (hwnd={hwnd})")

        try:
            from plugins.commands import window_switcher as _ws
            focused = _ws._force_focus(hwnd)
            if focused:
                logger.info("[WAKE-PROFILE] Focused %r", title)
                self.play_sound("target_focused")
            else:
                import ctypes as _ct
                _fg = _ct.windll.user32.GetForegroundWindow()
                _fgl = _ct.windll.user32.GetWindowTextLengthW(_fg)
                _fgb = _ct.create_unicode_buffer(_fgl + 1)
                _ct.windll.user32.GetWindowTextW(_fg, _fgb, _fgl + 1)
                logger.warning(
                    "[WAKE-PROFILE] FOCUS FAILED for %r (foreground still %r) — proceeding to dictate anyway",
                    title, _fgb.value or "<unknown>",
                )
        except Exception as exc:
            logger.exception(f"[WAKE-PROFILE] Focus failed: {exc}")
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
                    logger.info(f"[WAKE-PROFILE] Pre-buffering trailing speech: '{initial_content}'")

        # Resolve per-profile mode.  Explicit key in config wins; otherwise
        # default by process/id name: hermes-targeted sessions stage only (never
        # auto-submit), all other profiles press Enter on send-word detection.
        if 'mode' in profile:
            _mode = profile['mode']
        elif 'hermes' in process_name.lower() or 'hermes' in tid.lower():
            _mode = 'stage_send'
        else:
            _mode = 'focus_dictate'

        # Start open-ended wake session: per-utterance delivery, ends on inactivity.
        # send_word is THIS profile's own terminator (never the shared/global
        # default) -- passed through so the termination check later only
        # recognizes this profile's word, not every profile's.
        self._start_wake_session(
            initial_content=initial_content, mode=_mode,
            send_word=profile.get('send_word'),
        )

    def _start_wake_session(self, initial_content=None, mode='focus_dictate', send_word=None):
        """Enter open-ended wake session state.

        Each transcribed utterance is delivered immediately.  The session stays
        alive through silence and only ends after _WAKE_SESSION_TIMEOUT_S of
        inactivity or via the global cancel path.

        mode: 'focus_dictate' — press Enter after send-word detection (claude profiles).
              'stage_send' — text staged, Enter suppressed (hermes/agentic profiles).
        send_word: the dispatching profile's own terminator word. Stored on
              self for the duration of this session so the termination check
              (see process_wake_word_buffer's wake_session branch) is scoped
              to exactly this profile, not the shared/global send_words list
              -- profile isolation for the agentic-safety send_word contract.
        """
        # Duck other apps' audio for this open-ended dictation window --
        # only reached once a wake word has actually fired and an active
        # session is opening (not during passive always-on wake listening).
        # No-op unless ducking.enabled.
        self._duck_audio()

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
        self._wake_profile_active = True
        self._wake_session_first_chunk = True
        self._wake_session_mode = mode
        self._wake_session_send_word = send_word

        if hasattr(self, 'wake_word_timer') and self.wake_word_timer:
            self.wake_word_timer.cancel()

        logger.debug(f"[STATE] {old_state} -> wake_session "
              f"(chunk gap: {_WAKE_SESSION_CHUNK_GAP_S}s, "
              f"inactivity timeout: {_WAKE_SESSION_TIMEOUT_S}s, "
              f"mode: {mode})")

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
        t = thread_registry.timer(
            "dictation.wake_session_timeout", _WAKE_SESSION_TIMEOUT_S,
            self._end_wake_session, daemon=True)
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
        logger.info("[WAKE-SESSION] ended (inactivity timeout)")
        self._reset_wake_dictation()

    def _vad_probabilities(self, audio_16k):
        """Return one ONNX speech probability per complete 512-sample frame.

        Caller must hold ``_vad_lock``. The bundled faster-whisper wrapper is
        stateless between calls and accepts a flat float32 buffer whose length
        is a multiple of 512. Discarding a sub-frame tail preserves the prior
        torch path's behavior.
        """
        audio_16k = np.ascontiguousarray(audio_16k, dtype=np.float32).reshape(-1)
        usable = (audio_16k.size // 512) * 512
        if usable == 0:
            return np.empty(0, dtype=np.float32)
        probabilities = self._vad_model(audio_16k[:usable])
        return np.asarray(probabilities, dtype=np.float32).reshape(-1)

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
        with self._vad_lock:
            probabilities = self._vad_probabilities(chunk_16k)
        return bool(np.any(probabilities > 0.5))

    def _vad_reset(self):
        """Compatibility hook for utterance-boundary VAD cleanup.

        faster-whisper's bundled ONNX wrapper initializes its h/c/context
        arrays inside every call, so there is no recurrent state to clear.
        Existing callers retain this hook to keep their cleanup paths stable.
        """
        return None

    def _buffer_has_contiguous_speech(self, audio, src_rate,
                                       min_ms=_GATE_MIN_CONTIG_MS,
                                       prob_threshold=_GATE_VAD_PROB,
                                       head_grace_ms: float = 0.0):
        """True if `audio` contains a CONTIGUOUS run of >= min_ms high-confidence speech.

        Unlike _vad_is_speech (which early-exits on the first speech-probable
        frame -- fine for live "is anyone talking" gating), this tracks the
        LONGEST contiguous run of frames above prob_threshold and requires it
        to span at least min_ms. That's what rejects rhythmic line-noise
        (speech-probable in total, but never contiguous) while still passing
        a short whispered word.

        Reuses the same dedicated Silero ONNX model/lock as _vad_is_speech.
        Falls back to _zcr_energy_contiguous_speech (Fix 5) if Silero
        is unavailable. Fails OPEN (returns True) if neither can run --
        eating real speech is worse than letting a rare beep through.

        The lock covers the whole ONNX inference call. Although this wrapper
        carries no recurrent state between calls, serializing its dedicated
        InferenceSession keeps scans deterministic and avoids relying on
        provider-specific concurrent-run behavior.

        head_grace_ms ("head grace"): treats a low reading inside the
        first head_grace_ms of the buffer as NEUTRAL rather than
        contiguity-breaking -- it neither resets an accumulating run nor
        fabricates one. This is for a KNOWN, Samsara-generated noisy span
        (start earcon + key-click transient) at the head of hotkey
        buffers; it never touches/edits any audio sample (the earcon-span
        buffer-muting approach was explicitly retracted). A window that
        DOES score above threshold inside the grace span still counts
        normally toward the run.
        """
        if not self._vad_available or self._vad_model is None:
            return self._zcr_energy_contiguous_speech(audio, src_rate, min_ms=min_ms)

        chunk_16k = resample_audio(audio, src_rate, 16000)
        if chunk_16k.ndim > 1:
            chunk_16k = chunk_16k.flatten()

        window_size = 512
        frame_ms = window_size / 16000 * 1000.0  # 32ms per Silero frame
        min_contig_frames = max(1, int(min_ms / frame_ms))
        grace_frames = max(0, int(round(head_grace_ms / frame_ms)))

        try:
            with self._vad_lock:
                probabilities = self._vad_probabilities(chunk_16k)
        except Exception as e:
            logger.exception(f"[VAD] ONNX gate inference failed, using ZCR fallback: {e}")
            return self._zcr_energy_contiguous_speech(audio, src_rate, min_ms=min_ms)

        contig = 0
        best_contig = 0
        for idx, speech_prob in enumerate(probabilities):
            if speech_prob > prob_threshold:
                contig += 1
                best_contig = max(best_contig, contig)
            elif idx < grace_frames:
                pass  # head grace: low reading in the known noisy span -- neutral, not a break
            else:
                contig = 0

        passed = best_contig >= min_contig_frames
        if passed:
            # Evidence trail for the next leak: a gate PASS is otherwise
            # invisible (only SKIP is logged today), so there's no ground
            # truth for why a given buffer reached Whisper.
            logging.getLogger("Samsara").debug(
                "[GATE] pass: max contiguous speech %dms (buffer %.1fs)%s",
                round(best_contig * frame_ms), len(audio) / src_rate,
                f", head_grace={head_grace_ms:.0f}ms" if head_grace_ms > 0 else "",
            )
        return passed

    def _zcr_energy_contiguous_speech(self, audio, src_rate,
                                       min_ms=_GATE_MIN_CONTIG_MS,
                                       zcr_low=0.02, zcr_high=0.30):
        """Fallback presence gate when Silero VAD is unavailable (Fix 5).

        Windowed zero-crossing-rate + energy check: a window counts as
        speech-like if its short-time energy clears an adaptive floor AND its
        ZCR falls in the speech-plausible band (electrical hum/line noise
        typically sits outside this band even when energy is high). Requires
        the same contiguous-run length as the VAD path.

        Fails OPEN (returns True) on any failure or a buffer too short to
        analyze -- see _buffer_has_contiguous_speech's docstring for why.
        """
        try:
            audio = np.asarray(audio, dtype=np.float32)
            if audio.ndim > 1:
                audio = audio.flatten()

            window_samples = max(1, int(src_rate * 0.032))  # ~32ms, matches Silero frame size
            n_windows = len(audio) // window_samples
            if n_windows == 0:
                return True  # fail open -- buffer too short to analyze

            trimmed = audio[:n_windows * window_samples]
            frames = trimmed.reshape(n_windows, window_samples)

            energy = np.sqrt(np.mean(frames ** 2, axis=1))
            # Adaptive floor: a fixed multiple of the buffer's own noise
            # floor, so this works across mic gain/environment rather than
            # a fixed absolute threshold. Uses the 10th percentile, NOT the
            # median -- when the buffer is mostly speech (e.g. 3s of a 4s
            # hold), the median IS speech-level energy, making the
            # threshold ~2x the speech itself and rejecting the very frames
            # it should pass. The 10th percentile tracks the quiet frames
            # (the true noise floor) regardless of how much of the buffer
            # is speech.
            noise_floor = np.percentile(energy, 10)
            energy_thresh = max(noise_floor * 2.0, 1e-4)

            signs = np.sign(frames)
            signs[signs == 0] = 1
            zero_crossings = np.abs(np.diff(signs, axis=1)) > 0
            zcr = np.mean(zero_crossings, axis=1)

            speech_like = (energy > energy_thresh) & (zcr > zcr_low) & (zcr < zcr_high)

            frame_ms = window_samples / src_rate * 1000.0
            min_contig_frames = max(1, int(min_ms / frame_ms))

            contig = 0
            best_contig = 0
            for is_speech in speech_like:
                if is_speech:
                    contig += 1
                    best_contig = max(best_contig, contig)
                else:
                    contig = 0

            passed = best_contig >= min_contig_frames
            if passed:
                logging.getLogger("Samsara").debug(
                    "[GATE] pass: max contiguous speech %dms (buffer %.1fs) [ZCR fallback]",
                    round(best_contig * frame_ms), len(audio) / src_rate,
                )
            return passed
        except Exception as e:
            logger.debug(f"[GATE] ZCR fallback failed, failing open: {e}")
            return True

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
            logger.exception(f"[WARN] wake trace callback failed: {e}")

    def calibrate_wake_mic(self, seconds: float = 3.0,
                           cancel_event=None) -> float | None:
        """Sample ambient audio for *seconds* and seed the adaptive noise floor.

        Uses the ACE engine ring (via a temporary registered consumer) so no
        second InputStream is opened.  The temporary reader starts at the live
        write head so calibration measures a fresh, full quiet interval.

        Returns the measured floor RMS, or None if no frames were available.
        Persists the result to wake_word_config.audio.measured_noise_floor so
        the floor survives a restart and seeds the EMA on next boot.
        When cancel_event is supplied, cancellation returns None without
        changing or persisting the current floor.
        """
        import logging as _log

        engine = getattr(self, '_ace_engine', None)
        if engine is None or not engine._running:
            _log.getLogger().warning("[CAL] ACE engine not running — calibrate_wake_mic has no audio source")
            return None

        reader = engine.register_consumer("wake-calibration")
        from samsara.audio_engine.ring import EMPTY as _EMPTY

        rms_values = []
        deadline = time.monotonic() + seconds
        cancelled = False
        try:
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                frame = reader.read_next()
                if frame is _EMPTY:
                    time.sleep(0.005)
                    continue
                chunk = frame.pcm.astype(np.float32) / 32767.0
                rms_values.append(float(np.sqrt(np.mean(chunk ** 2))))
        finally:
            engine.unregister_consumer(reader)

        if cancelled:
            _log.getLogger().info("[CAL] Wake mic calibration cancelled")
            return None

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

    def _wake_audio_is_below_gate(self, audio_rms, *, oww_confirmed=False):
        """Return True when a wake buffer should be rejected before Whisper.

        OpenWakeWord-positive buffers have already passed a purpose-built wake
        detector. They still go through Whisper phrase confirmation, but must
        not be rejected (or learned as ambient noise) by this secondary energy
        gate. Other callers retain the existing adaptive/fixed RMS behavior.
        """
        if oww_confirmed:
            logging.debug(
                f"[WAKE] OWW-confirmed buffer bypassing RMS gate (rms {audio_rms:.4f}); "
                "Whisper confirmation still required"
            )
            return False

        ww_config = self.config.get('wake_word_config', {})
        audio_config = ww_config.get('audio', {})
        use_adaptive = audio_config.get('adaptive_gate', True)
        if use_adaptive:
            # Update rolling noise-floor estimate only from buffers that have
            # not already been identified as wake speech by OpenWakeWord.
            if self._wake_noise_floor is None:
                self._wake_noise_floor = max(audio_rms, _NOISE_FLOOR_MIN)
            elif audio_rms < self._wake_noise_floor * _NOISE_FLOOR_SPEECH_RATIO:
                self._wake_noise_floor = max(
                    (1.0 - _NOISE_FLOOR_ALPHA) * self._wake_noise_floor
                    + _NOISE_FLOOR_ALPHA * audio_rms,
                    _NOISE_FLOOR_MIN,
                )

            gate_level = max(self._wake_noise_floor * _SPEECH_FLOOR_RATIO, _ABS_FLOOR_MIN)
            if audio_rms < gate_level:
                logging.debug(
                    f"[WAKE] gated (rms {audio_rms:.4f} < adaptive {gate_level:.4f}"
                    f" [floor {self._wake_noise_floor:.4f} x{_SPEECH_FLOOR_RATIO}]) -- skipping"
                )
                return True
        else:
            speech_threshold = audio_config.get('speech_threshold', DEFAULT_SPEECH_THRESHOLD)
            if audio_rms < speech_threshold:
                logging.debug(
                    f"[WAKE] Below speech threshold (RMS {audio_rms:.4f} < {speech_threshold:.4f}), skipping"
                )
                return True
        return False

    def process_wake_word_buffer(self, buffer, src_rate=None, *, oww_confirmed=False):
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
                    _dump_dir = samsara_home_dir() / "debug_audio"
                    _dump_dir.mkdir(parents=True, exist_ok=True)
                    _ts = datetime.now().strftime("%H%M%S_%f")
                    _dump_path = _dump_dir / f"wake_{_ts}.wav"
                    _int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
                    with _wave.open(str(_dump_path), 'w') as _wf:
                        _wf.setnchannels(1)
                        _wf.setsampwidth(2)
                        _wf.setframerate(self.model_rate)
                        _wf.writeframes(_int16.tobytes())
                    logger.debug(f"[DEBUG] Dumped wake audio -> {_dump_path} ({audio_duration:.2f}s)")
                except Exception as _de:
                    logger.exception(f"[DEBUG] Audio dump failed: {_de}")

            # FIX 1: RMS energy gate — skip Whisper on silent audio.
            # On CPU machines Whisper takes ~1s per call; calling it on every
            # chunk saturates the CPU. This gate rejects the buffer early when
            # the audio energy is not meaningfully above the ambient noise floor.
            audio_rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
            if self._wake_audio_is_below_gate(audio_rms, oww_confirmed=oww_confirmed):
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

            # Get transcription parameters based on performance mode. NOT
            # forced to English: this is the wake/_output_dictation lane --
            # transcribes with the configured dictation language. The
            # cancel/send/end/pause/resume control words checked below are
            # small fixed English word lists, best-effort in non-English
            # (commands/control-words remain English-only by design).
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

            _seg_list = list(segments)
            text = "".join([segment.text for segment in _seg_list]).strip()
            transcribe_time = time.time() - transcribe_start

            # Diagnostics: accumulate Whisper quality signals across every
            # chunk feeding the same buffered wake utterance -- quick/long
            # dictation may flush this buffer several times before
            # _output_dictation delivers one final joined text. Reset by
            # _output_dictation after it reads the accumulator. Never
            # touches control flow; diagnostics-only, defensive.
            try:
                _t_ms = int(transcribe_time * 1000)
                _sig = diagnostics.segment_signals(_seg_list)
                _acc = getattr(self, '_wake_diag_acc', None) or {
                    'audio_s': 0.0, 'avg_logprob': None, 'compression_ratio': None,
                    'no_speech_prob': None, 'temperature': None, 'n_segments': 0,
                    't_transcribe_ms': 0, 'detected_language': None,
                }
                _acc['audio_s'] += audio_duration
                _acc['t_transcribe_ms'] += _t_ms
                _acc['n_segments'] += _sig['n_segments']
                _acc['detected_language'] = getattr(info, 'language', None) or _acc['detected_language']
                if _sig['avg_logprob'] is not None:
                    _acc['avg_logprob'] = (
                        _sig['avg_logprob'] if _acc['avg_logprob'] is None
                        else min(_acc['avg_logprob'], _sig['avg_logprob'])
                    )
                if _sig['compression_ratio'] is not None:
                    _acc['compression_ratio'] = (
                        _sig['compression_ratio'] if _acc['compression_ratio'] is None
                        else max(_acc['compression_ratio'], _sig['compression_ratio'])
                    )
                if _sig['no_speech_prob'] is not None:
                    _acc['no_speech_prob'] = (
                        _sig['no_speech_prob'] if _acc['no_speech_prob'] is None
                        else max(_acc['no_speech_prob'], _sig['no_speech_prob'])
                    )
                if _sig['temperature'] is not None:
                    _acc['temperature'] = (
                        _sig['temperature'] if _acc['temperature'] is None
                        else max(_acc['temperature'], _sig['temperature'])
                    )
                self._wake_diag_acc = _acc
            except Exception as _diag_exc:
                logger.debug(f"[DIAG] wake signal accumulation failed: {_diag_exc}")

            # Performance logging for wake word mode
            rtf = transcribe_time / audio_duration if audio_duration > 0 else 0
            device_info = getattr(self, 'device_type', 'unknown')
            logger.info(f"[PERF/WAKE] Audio: {audio_duration:.1f}s | Transcribe: {transcribe_time*1000:.0f}ms | "
                  f"RTF: {rtf:.2f}x | Mode: {perf_mode} | Device: {device_info}")
            
            # Apply corrections dictionary
            text = self.voice_training_window.apply_corrections(text)
            text_lower = text.lower()
            
            if not text:
                logger.info(f"[HEAR] (nothing — Whisper returned empty for {audio_duration:.1f}s of audio)")
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
            
            logger.info(f"[HEAR] \"{text}\"")
            
            # Get wake word config
            ww_config = self.config.get('wake_word_config', {})
            wake_phrase = ww_config.get('phrase', 'samsara').lower()

            self._emit_wake_trace({"stage": "utterance_start", "raw": text, "normalized": text_lower})

            # In dictation state (quick_dictation, long_dictation, or wake_session)?
            if self.app_state in ('quick_dictation', 'long_dictation', 'wake_session'):
                # Check abort words
                abort_words = ww_config.get('wake_abort_phrase', ['cancel'])
                for cw in abort_words:
                    if cw.lower() in text_lower:
                        logger.info(f"[CANCEL] Dictation cancelled ('{cw}')")
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
                    # Profile isolation: use ONLY the dispatching profile's own
                    # send_word when this session was started via a wake_profile
                    # (the normal path -- see _dispatch_wake_profile). Falling
                    # back to the shared/global send_words list here would let
                    # profile A's terminator word ("over") prematurely end
                    # profile B's session (send_word "send") just because both
                    # happen to appear in the same global default/config list --
                    # exactly the agentic-safety hazard the per-profile
                    # send_word field exists to prevent. The global list is
                    # kept only as a defensive fallback for a session with no
                    # recorded send_word (shouldn't happen via the normal
                    # dispatch path, but fails safe rather than never matching).
                    _profile_send_word = getattr(self, '_wake_session_send_word', None)
                    _send_words = (
                        [_profile_send_word] if _profile_send_word
                        else ww_config.get('send_words', _WAKE_SESSION_SEND_WORDS)
                    )
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
                        _mode = getattr(self, '_wake_session_mode', 'focus_dictate')
                        if _mode == 'focus_dictate':
                            time.sleep(0.05)
                            pyautogui.press('return')
                            self.play_sound("success")
                            self.play_sound("text_sent")
                            logger.info(f"[WAKE-SESSION] sent — '{_matched_sw}' detected, Enter pressed")
                        else:
                            self.play_sound("action_complete")
                            self.play_sound("text_sent")
                            logger.info(f"[WAKE-SESSION] staged — '{_matched_sw}' detected, Enter suppressed (stage_send)")
                        self._emit_wake_trace({"stage": "utterance_end", "result": "wake_session_sent",
                                               "send_word": _matched_sw, "mode": _mode})
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
                        logger.info(f"[END] End word detected: '{ew}'")
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
                                logger.info(f"[RESUME] Dictation resumed ('{rw}')")
                                self._emit_wake_trace({"stage": "resume",
                                                       "buffer_size": len(self.wake_dictation_buffer)})
                                self._emit_wake_trace({"stage": "utterance_end", "result": "resumed"})
                                return
                        logger.info(f"[PAUSED] Ignoring: '{text}'")
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
                                logger.info(f"[DICTATE] Buffered (pre-pause): {cleaned}")
                            self._dictation_paused = True
                            self.silence_start = None
                            self.play_sound("stop")
                            if hasattr(self, 'listening_indicator'):
                                self._schedule_ui(self.listening_indicator.set_mode, "Paused")
                                self._schedule_ui(self.listening_indicator.set_listening, False)
                            logger.info(f"[PAUSE] Dictation paused ('{pw}')")
                            self._emit_wake_trace({"stage": "pause",
                                                   "buffer_size": len(self.wake_dictation_buffer)})
                            self._emit_wake_trace({"stage": "utterance_end", "result": "paused"})
                            return

                # Accumulate text
                self.wake_dictation_buffer.append(text)
                logger.info(f"[DICTATE] Buffered: {text}")
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
                logger.info(f"[CORRECT] '{text_lower}' -> '{corrected_lower}'")

            logger.info(
                "[WAKE-CHECK] transcript=%r profiles=%r",
                corrected_lower,
                [t.get('phrase') for t in self.config.get('wake_profiles', []) if t.get('enabled', True)],
            )

            # Phase 1: check multi-wake profiles BEFORE the legacy single-phrase check.
            # Each enabled wake_profile has a distinct phrase ("hey claude",
            # "activate hermes") that doesn't overlap with legacy jarvis phrases.
            _wake_profile = self._check_wake_profiles(corrected_lower)
            if _wake_profile is not None:
                self._dispatch_wake_profile(_wake_profile, corrected_lower=corrected_lower)
                return

            matched, match_type, match_index = match_wake_phrase(corrected_lower, wake_phrase)

            self._emit_wake_trace({
                "stage": "wake_word_check", "input": text, "normalized": text_lower,
                "corrected": corrected_lower, "correction_applied": correction_applied,
                "wake_phrase": wake_phrase, "matched": matched,
                "match_type": match_type, "match_index": match_index,
            })

            if matched:
                logger.debug(f"[MIC] Wake word detected: '{wake_phrase}' ({match_type} @ {match_index})")
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
                    logger.info(f"[ECHO] Stripped {echo_count} echo(es) of '{wake_phrase}' from command")
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
                    logger.info(f"[TEXT] Command: {command_text}")
                    self._process_wake_command(command_text)
                else:
                    if command_text:
                        logger.info(f"[SKIP] Ignoring noise after wake word: '{command_text}'")
                    logger.info("[LISTEN] Listening for command...")
                    self._start_wake_timeout()

                self._emit_wake_trace({"stage": "utterance_end",
                                       "result": "wake_word_detected" if not has_meaningful_command else "command_processed"})

            elif match_type == "substring":
                logger.debug(f"[SKIP] Substring-only wake match @ idx {match_index} -- not firing: '{text}'")
                self._emit_wake_trace({"stage": "utterance_end", "result": "substring_rejected"})

            elif self.wake_word_triggered:
                if self._try_cancel_pending_wake_command(text):
                    return
                logger.info(f"[TEXT] Command: {text}")
                self._emit_wake_trace({"stage": "command_extract",
                                       "from_index": -1, "command": text, "remainder": ""})
                self._process_wake_command(text)
                self._emit_wake_trace({"stage": "utterance_end", "result": "followup_command"})

            else:
                self._emit_wake_trace({"stage": "utterance_end", "result": "no_wake_word"})
                
        except Exception as e:
            logger.exception(f"[ERROR] Transcription failed: {e}")
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
            except Exception as _snd_err:
                logger.debug(f"Failure earcon (winsound) unavailable: {_snd_err}")
        finally:
            if _set_in_progress:
                self._wake_transcription_in_progress = False
            # Retain the utterance-boundary cleanup hook. The bundled ONNX VAD
            # is stateless between calls, so this is currently a no-op.
            self._vad_reset()

    def _process_wake_command(self, text):
        """Route a wake word command based on parsed intent (4-state machine)."""
        # Transition to command_window while we parse
        old_state = self.app_state
        self.app_state = 'command_window'
        if old_state != 'command_window':
            logger.debug(f"[STATE] {old_state} -> command_window")

        intent = parse_wake_command(text)
        logger.info(f"[PARSE] raw='{text}' -> type={intent['type']}, "
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
                logger.debug("[STATE] command_window -> asleep (command executed)")
                self._indicator_success_and_reset()
                return

            # Not a recognized command -- silently go back to sleep.
            # DO NOT paste unrecognized text after wake word. If the user
            # wanted dictation, they'd say "jarvis, type ..." or "jarvis,
            # dictate". This prevents false wake triggers (e.g. "service"
            # corrected to "jarvis") from typing garbage into the focused app.
            logger.info(f"[SKIP] No command match for '{text}' — back to sleep")
            self.wake_word_triggered = False
            self.app_state = 'asleep'
            logger.debug("[STATE] command_window -> asleep (no match)")
            self._indicator_reset()
            return

        # type == "unknown" -- noise/garbage, back to asleep
        logger.info(f"[SKIP] Ignoring noise: '{text}'")
        self.app_state = 'asleep'
        logger.debug("[STATE] command_window -> asleep (noise)")
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
            logger.debug(f"[STATE] {old_state} -> quick_dictation (silence timeout: {timeout}s)")
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
            logger.debug(f"[STATE] {old_state} -> long_dictation "
                  f"(hard-cap: {max_duration}s, failsafe: {failsafe_duration}s)")

            if hasattr(self, '_dictation_hardcap_timer') and self._dictation_hardcap_timer:
                self._dictation_hardcap_timer.cancel()
            self._dictation_hardcap_timer = thread_registry.timer(
                "dictation.hardcap", max_duration,
                self._finalize_dictation_hardcap, daemon=True
            )

            # Absolute failsafe — fires only if the soft hard-cap somehow
            # fails to drain the pipeline (e.g. stuck transcription worker).
            # Brutally resets regardless of pending state. Should normally
            # never fire in healthy operation.
            if hasattr(self, '_dictation_failsafe_timer') and self._dictation_failsafe_timer:
                self._dictation_failsafe_timer.cancel()
            self._dictation_failsafe_timer = thread_registry.timer(
                "dictation.failsafe", failsafe_duration,
                self._absolute_failsafe_reset, daemon=True
            )

        self.play_sound("start")

        # Update listening indicator to show active dictation
        if hasattr(self, 'listening_indicator'):
            label = "Quick Dictation" if mode_name == 'quick_dictation' else "Long Dictation"
            self._schedule_ui(self.listening_indicator.set_mode, label)
            self._schedule_ui(self.listening_indicator.set_listening, True)

        if initial_content:
            self.wake_dictation_buffer.append(initial_content)
            logger.info(f"[DICTATE] Initial content: {initial_content}")
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
        thread_registry.spawn("dictation._delayed_reset", _delayed_reset, daemon=True)

    def _indicator_reset(self):
        """Return indicator to idle state."""
        if not hasattr(self, 'listening_indicator'):
            return
        self._schedule_ui(self.listening_indicator.set_listening, False)
        mode_display = self._get_mode_display() if hasattr(self, '_get_mode_display') else "Hold"
        self._schedule_ui(self.listening_indicator.set_mode, mode_display)

    def _reset_wake_dictation(self):
        """Return to asleep state, clearing all dictation state."""
        # Restore audio ducked by _duck_audio() at _start_wake_session().
        # This is the single common exit chokepoint for every wake-session
        # end path (inactivity timeout, send-word, explicit cancel -- see
        # this function's many call sites), unlike _end_wake_session()
        # which only covers the timeout path. Always safe: a no-op if
        # nothing was ducked.
        self._restore_audio()

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
        self._wake_profile_active = False
        self._wake_session_first_chunk = True
        self._wake_session_mode = 'focus_dictate'
        # Profile isolation: clear the just-ended session's send_word so a
        # future session that somehow starts without one (defensive-only --
        # the normal dispatch path always supplies it) can't inherit a stale
        # word from whichever profile ran previously.
        self._wake_session_send_word = None

        existing = getattr(self, '_wake_session_inactivity_timer', None)
        if existing is not None:
            existing.cancel()
            self._wake_session_inactivity_timer = None

        if old_state != 'asleep':
            logger.debug(f"[STATE] {old_state} -> asleep")

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
        self._dictation_finalize_timer = thread_registry.timer(
            "dictation.finalize_timeout", timeout, self._finalize_dictation_timeout)

    def _finalize_dictation_timeout(self):
        """Called when the dictation finalization timer expires."""
        logger.info(f"[WS-DIAG] _finalize_dictation_timeout called: app_state={self.app_state!r}")
        try:
            with self._dictation_finalize_lock:
                if self.wake_dictation_mode and self.wake_dictation_buffer and not self._dictation_require_end:
                    final_text = ' '.join(self.wake_dictation_buffer)
                    logger.info(f"[DONE] Dictation complete: {final_text}")
                    self._output_dictation(final_text)
                    self._reset_wake_dictation()
        except Exception as e:
            logger.exception(f"[ERROR] _finalize_dictation_timeout crashed: {e}")
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
                logger.info("[HARDCAP] Time limit reached — flushing audio and requesting finalize")
                self._dictation_finalize_requested = True
        except Exception as e:
            logger.exception(f"[ERROR] _finalize_dictation_hardcap crashed: {e}")
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

        thread_registry.spawn(
            "dictation._process_wake_word_buffer_tracked",
            self._process_wake_word_buffer_tracked,
            args=(buffer_copy,),
            daemon=True,
        )
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
                    logger.info(f"[DONE] Long dictation finalized: {final_text}")
                    # _output_dictation must be called outside the lock to
                    # avoid blocking the pipeline on clipboard/UI work.
                    pending_text = final_text
                else:
                    pending_text = None
                    logger.info("[DONE] Long dictation finalized with empty buffer")

                self._reset_wake_dictation()
            # Released the lock — now do the user-visible output
            if pending_text:
                self._output_dictation(pending_text)
        except Exception as e:
            logger.exception(f"[ERROR] _maybe_finalize_dictation crashed: {e}")
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
                logger.info(f"[FAILSAFE] Absolute timeout — forcing reset "
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
            logger.exception(f"[ERROR] _absolute_failsafe_reset crashed: {e}")
            import traceback
            traceback.print_exc()

    _UNDO_EXPIRY_SECONDS = 60.0

    def _paste_preserving_clipboard(self, text, before_paste=None):
        """Paste text via clipboard while preserving the user's original clipboard content."""
        delay = self.config.get('clipboard_delay', CLIPBOARD_RESTORE_DELAY)
        paste_target = {'hwnd': None}

        def _capture_target_before_paste():
            """Compose the caller's focus guard with undo-target capture.

            paste_with_preservation invokes this immediately before Ctrl+V,
            after its clipboard preparation delay. Capturing here avoids
            remembering whichever window happened to be foreground earlier
            when the transcription worker began.
            """
            if before_paste is not None and not before_paste():
                return False
            paste_target['hwnd'] = _get_foreground_hwnd()
            return True

        # Keep one clipboard implementation. The centralized path captures
        # the clipboard sequence number immediately after Samsara's copy, so
        # an unrelated copy made during the paste window is never overwritten
        # by restoring our stale snapshot.
        paste_ok = paste_with_preservation(
            text,
            paste_delay=CLIPBOARD_PASTE_DELAY,
            restore_delay=delay,
            before_paste=_capture_target_before_paste,
        )
        if paste_ok:
            self._record_undoable_paste(
                text, target_hwnd=paste_target['hwnd'],
            )
            self.adaptive_learner.record_transcription(text)
            logger.info(
                "[PASTE] Ctrl+V sent chars=%d hwnd=%r",
                len(text), _get_foreground_hwnd(),
            )
        else:
            logger.error("[PASTE] Ctrl+V delivery failed; text retained by caller when possible")
        return paste_ok

    def _deliver_text_to_focused_editor(self, text):
        # backspace removes focus-primer char; assumes empty input box at session start
        pyautogui.press('x')
        time.sleep(_WAKE_PRIMER_DELAY)
        pyautogui.press('backspace')
        time.sleep(_WAKE_PRIMER_DELAY)
        self._paste_preserving_clipboard(text)

    def _record_undoable_paste(self, text, target_hwnd=_UNDO_TARGET_UNSET):
        """Remember a paste and the exact window eligible for native undo."""
        self._last_dictation_text = text
        self._last_dictation_length = len(text)
        self._last_dictation_hwnd = (
            _get_foreground_hwnd()
            if target_hwnd is _UNDO_TARGET_UNSET else target_hwnd
        )
        self._arm_undo_timer()

    def _arm_undo_timer(self):
        """Start a fresh expiry timer; cancel any existing one."""
        if self._undo_timer is not None:
            self._undo_timer.cancel()
        self._undo_timer = thread_registry.timer(
            "dictation.undo_expiry", self._UNDO_EXPIRY_SECONDS,
            self._clear_undo, daemon=True)

    def _clear_undo(self):
        """Drop undo state (called on expiry or after a successful undo)."""
        self._last_dictation_text = None
        self._last_dictation_length = 0
        self._last_dictation_hwnd = None
        if self._undo_timer is not None:
            self._undo_timer.cancel()
            self._undo_timer = None

    def undo_last_dictation(self):
        """Undo the last paste through the target application's undo stack.

        The exact foreground HWND must still match the window recorded
        immediately before Ctrl+V. A mismatch fails closed without consuming
        the saved undo, allowing the user to refocus that window and retry.
        """
        if not self._last_dictation_text:
            logger.info("[UNDO] Nothing to undo")
            self.play_sound("error")
            return False

        target_hwnd = getattr(self, '_last_dictation_hwnd', None)
        current_hwnd = _get_foreground_hwnd()
        if target_hwnd is None or current_hwnd != target_hwnd:
            logger.warning(
                "[UNDO] Refused: paste window is not foreground "
                "(target_hwnd=%r current_hwnd=%r)",
                target_hwnd, current_hwnd,
            )
            self.play_sound("error")
            return False

        text = self._last_dictation_text
        try:
            pyautogui.hotkey('ctrl', 'z')
        except Exception as exc:
            logger.exception("[UNDO] Ctrl+Z injection failed: %s", exc)
            self.play_sound("error")
            return False

        preview = text[:50] + ("..." if len(text) > 50 else "")
        logger.info(f"[UNDO] Native Ctrl+Z sent for: {preview}")
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
        logger.info(f"[LEARN] Correction recorded: '{original}' -> '{corrected}'")

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
                logger.info(f"[LEARN] Promoted to dictionary: '{original}' -> '{corrected}'")
                self.play_sound("success")

    def _apply_formatting_tokens(self, text: str) -> str:
        """Single chokepoint for samsara.formatting_tokens on DICTATE-lane
        output -- reused by the hotkey path, wake-session dictation, and
        (via the format_dictate_fn callable) the session DICTATE lane.
        Never called for COMMAND-mode or AVA text. Must run AFTER
        smart_correct and before the text is delivered/pasted or logged to
        history, so history stores what was actually typed."""
        return apply_formatting_tokens_if_enabled(
            text, self.config.get('formatting_tokens', {}).get('enabled', True))

    def _output_dictation(self, text):
        """Output dictated text"""
        _diag_entry = time.perf_counter()
        # Whisper signals accumulated across every process_wake_word_buffer
        # chunk feeding this utterance (quick/long dictation can flush the
        # buffer several times before one final join-and-output). Consume
        # and reset here so the next utterance starts from a clean slate.
        _diag_acc = getattr(self, '_wake_diag_acc', None) or {}
        self._wake_diag_acc = None

        # Apply text processing (auto-capitalize, number formatting)
        _diag_corr_start = time.perf_counter()
        text = self.process_transcription(text)

        # Deterministic cleanup (filler removal, spacing).
        raw = text
        _cmode = 'verbatim' if getattr(self, '_skip_cleanup', False) else self.config.get('cleanup_mode', 'clean')
        text = clean_text(text, mode=_cmode)
        t_corrections_ms = int((time.perf_counter() - _diag_corr_start) * 1000)

        # Smart Corrections (optional LLM cleanup pass) -- wake-word
        # dictation gate. Runs on this same worker thread; never blocks
        # output on failure (see smart_correct docs).
        t_smart_ms = -1
        smart_changed = False
        if self.config.get('smart_corrections', {}).get('modes', {}).get('wake', True):
            _diag_smart_start = time.perf_counter()
            _text_before_smart = text
            text = smart_correct(text, self)
            t_smart_ms = int((time.perf_counter() - _diag_smart_start) * 1000)
            smart_changed = (text != _text_before_smart)

        if self.config['add_trailing_space']:
            text = text + " "

        # Inline formatting tokens ("new line" -> \n, etc.) -- after
        # smart_correct, before delivery/history, so history stores what
        # was actually typed (see _apply_formatting_tokens).
        text = self._apply_formatting_tokens(text)

        logger.info(f"[OK] {text}")
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

        # Diagnostics record -- total combines the accumulated transcribe
        # time (across every chunk feeding this utterance) with this
        # method's own corrections/smart/overhead time.
        try:
            diagnostics.record(diagnostics.DiagRecord(
                mode="wake",
                audio_s=_diag_acc.get('audio_s', 0.0),
                model_name=self.config.get('model_size', ''),
                device=getattr(self, 'device_type', 'unknown'),
                compute_type=self.config.get('compute_type', ''),
                t_transcribe_ms=_diag_acc.get('t_transcribe_ms', -1),
                t_corrections_ms=t_corrections_ms,
                t_smart_ms=t_smart_ms,
                t_total_ms=(
                    max(_diag_acc.get('t_transcribe_ms', 0), 0)
                    + int((time.perf_counter() - _diag_entry) * 1000)
                ),
                avg_logprob=_diag_acc.get('avg_logprob'),
                compression_ratio=_diag_acc.get('compression_ratio'),
                no_speech_prob=_diag_acc.get('no_speech_prob'),
                temperature=_diag_acc.get('temperature'),
                n_segments=_diag_acc.get('n_segments', 0),
                text=text,
                smart_changed=smart_changed,
                language=_languages.describe_diagnostics_language(
                    self.config.get('language', 'en'), _diag_acc.get('detected_language'),
                ),
            ), app=self)
        except Exception as _diag_exc:
            logger.debug(f"[DIAG] wake record failed: {_diag_exc}")

        if self.config['auto_paste']:
            logger.info(
                f"[WS-DIAG] _output_dictation: wake_profile_active="
                f"{getattr(self,'_wake_profile_active',None)} "
                f"first_chunk={getattr(self,'_wake_session_first_chunk',None)} "
                f"app_state={self.app_state!r}"
            )
            if getattr(self, '_wake_profile_active', False):
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
        self.wake_word_timer = thread_registry.timer(
            "dictation.wake_word_reset", timeout, self.reset_wake_word)
    
    def reset_wake_word(self):
        """Reset wake word trigger after timeout"""
        try:
            with self._dictation_finalize_lock:
                if self.wake_word_triggered:
                    logger.debug("[TIMEOUT] Wake word timeout - say wake word again")
                    self.wake_word_triggered = False

                # If in dictation mode and timed out, output what we have
                if self.wake_dictation_mode and self.wake_dictation_buffer:
                    ww_config = self.config.get('wake_word_config', {})
                    require_end = ww_config.get('modes', {}).get(self.wake_dictation_mode, {}).get('require_end_word', False)

                    if not require_end:
                        # Output buffered content on timeout
                        final_text = ' '.join(self.wake_dictation_buffer)
                        logger.debug(f"[TIMEOUT] Dictation timeout - outputting: {final_text}")
                        self._output_dictation(final_text)
                    else:
                        logger.debug(f"[TIMEOUT] Long dictation timeout - say end word or wake word again")
                        self.play_sound("error")

                self._reset_wake_dictation()
        except Exception as e:
            logger.exception(f"[ERROR] reset_wake_word crashed: {e}")
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
        from samsara.output_devices import output_sample_rate
        self._sound_stream_sr = output_sample_rate(
            sd, getattr(self, 'output_device', None), fallback=44100,
        )
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
        thread_registry.spawn(
            'samsara-output-watcher', self._watch_output_device, daemon=True,
        )

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
                    logger.info(f"[AUDIO] Skipping {sound_path.name} - install pydub for MP3/OGG support")
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
                logger.exception(f"[AUDIO] Failed to load {sound_path}: {e}")

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
                        logger.exception(f"[AUDIO] Failed to load extended earcon {wav_path.name}: {e}")
        except Exception as e:
            logger.exception(f"[AUDIO] Extended-earcon discovery failed: {e}")

    def _start_sound_stream(self):
        """Start the persistent output stream for sound playback.
        
        This stream stays open for the lifetime of the app. Unlike sd.play()
        (which creates/destroys a temporary stream per call), a persistent
        OutputStream coexists safely with the InputStream used for recording.
        """
        try:
            # WASAPI endpoints often accept only their mix-format rate (the
            # Arctis Nova endpoint, for example, rejects 44.1 kHz and requires
            # 48 kHz). Rebuild the cache whenever routing changes so callback
            # frames always match the stream's native/default rate.
            from samsara.output_devices import output_sample_rate
            stream_rate = output_sample_rate(
                sd, getattr(self, 'output_device', None),
                fallback=getattr(self, '_sound_stream_sr', 44100),
            )
            if stream_rate != self._sound_stream_sr:
                self._sound_stream_sr = stream_rate
                self._load_sound_cache()
                with self._buffer_lock:
                    self._playback_buffer = np.zeros((0, 1), dtype=np.float32)
            self._sound_stream = sd.OutputStream(
                samplerate=self._sound_stream_sr,
                channels=1,
                dtype='float32',
                callback=self._sound_stream_callback,
                blocksize=1024,  # ~21-23 ms at common 44.1/48 kHz rates
                device=getattr(self, 'output_device', None),
            )
            self._sound_stream.start()
            logger.info(
                "[AUDIO] Persistent sound stream started (device=%s, rate=%d Hz)",
                getattr(self, 'output_device', None), self._sound_stream_sr,
            )
        except Exception as e:
            requested = getattr(self, 'output_device', None)
            if requested is not None:
                logger.warning(
                    "[AUDIO] Output device %s failed (%s); falling back to system default",
                    requested, e,
                )
                self.output_device = None
                self.output_device_name = None
                self._start_sound_stream()
                return
            logger.exception(f"[AUDIO] Failed to start sound stream: {e}")
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
            logger.exception(f"[AUDIO] Sound stream error: {e}")
            return

    def reload_sounds(self):
        """Reload sounds from disk (call after changing sound files)"""
        logger.info("[AUDIO] Reloading sounds...")
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
                logger.info(f"[AUDIO] No cached sound for '{sound_type}' "
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
        logger.info("[AUDIO] Stopping sound stream...")
        if self._sound_stream is not None:
            try:
                self._sound_stream.stop()
                self._sound_stream.close()
            except Exception as e:
                logger.debug(f"[AUDIO] Sound stream stop/close failed: {e}")
            self._sound_stream = None

    def _watch_output_device(self):
        """Daemon thread: poll Windows every 2 s for default output device changes."""
        current_id = _get_default_render_id()
        stop = getattr(self, '_output_watcher_stop', None)
        if stop is None:
            return
        while not stop.wait(2.0):
            # An explicit Samsara device is independent of the Windows default.
            if getattr(self, 'output_device', None) is not None:
                continue
            new_id = _get_default_render_id()
            if new_id and new_id != current_id:
                current_id = new_id
                try:
                    self._on_output_device_changed()
                except Exception as exc:
                    logger.exception(f'[AUDIO] Device change handler error: {exc}')

    def _on_output_device_changed(self):
        """Restart output streams after the Windows default audio device changes."""
        logger.info('[AUDIO] Default output device changed — restarting streams')
        self.stop_sound_stream()
        self._start_sound_stream()
        eng = getattr(self, 'tts_engine', None)
        if eng is not None and hasattr(eng, 'restart_stream'):
            eng.restart_stream()


    def _duck_audio(self):
        """Lower other apps' audio for the dictation window about to open,
        if ducking.enabled -- no-op otherwise. See samsara/audio_ducking.py;
        this is the single place config is consulted so every call site
        (hotkey start_recording, wake-session start) stays in sync."""
        ducking_cfg = self.config.get('ducking', {}) or {}
        if ducking_cfg.get('enabled', False):
            audio_ducking.duck(ducking_cfg.get('level', 0.2))

    def _restore_audio(self):
        """Counterpart to _duck_audio -- always safe to call even if
        ducking was never engaged (audio_ducking.restore() is a no-op when
        not currently ducked), so call sites don't need their own
        enabled-check on the way out."""
        audio_ducking.restore()

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
                logger.info("Model still loading, please wait...")
            else:
                logger.info("Model not loaded!")
            return

        # Every recording source shares one DictationSessionConsumer. Starting
        # a second source used to overwrite its flags/session reference and
        # orphan a streaming worker plus overlay until the 120-second cap.
        if self.recording or getattr(self, '_streaming_session', None) is not None:
            logger.info("[RECORDING] start ignored -- capture already has an owner")
            return

        if self._stop_in_flight:
            logger.debug("[HOTKEY] start_recording ignored — stop still in flight")
            return

        # Duck other apps' audio as early as possible in the start sequence
        # -- before the earcon/capture activation below -- so the reduced
        # bleed is in effect for as much of the actual recording as
        # possible. No-op unless ducking.enabled (see _duck_audio).
        self._duck_audio()

        # Suppress wake word processing during hotkey recording -- FIX 1
        # (2026-07-10 hotkey word-loss investigation): WakeConsumer now goes
        # fully deaf on the very next poll frame (see wake_consumer.py's
        # _process_frame), but any utterance it was ALREADY mid-accumulating
        # right up to this instant would otherwise sit frozen and stale
        # until this flag clears -- discard it now rather than risk it
        # being flushed later. No-ops harmlessly if toggle-command-mode/
        # AI-command-mode owns the in-progress utterance instead.
        self._hotkey_recording = True
        if self._wake_consumer is not None:
            try:
                self._wake_consumer.discard_stale_wake_utterance()
            except Exception as e:
                logger.debug(f"[HOTKEY] discard_stale_wake_utterance failed: {e}")

        # Caller-forced streaming mode wins; otherwise fall back to the
        # config + 'hold' check. Streaming-mode in toggle/continuous is
        # not supported -- those paths use the existing batch behavior.
        if streaming is None:
            streaming = (self.config.get('streaming_mode', False)
                         and self.config.get('mode', 'hold') == 'hold')

        # Play start sound before opening capture.
        # Skipped for command mode which manages its own debounced 200ms earcon.
        # Head-grace bookkeeping (2026-07-10 hotkey word-loss investigation):
        # record the earcon's measured duration when it actually plays, so
        # the hotkey gate call in stop_recording() can grant a grace span
        # covering it (see _GATE_HEAD_GRACE_CLICK_PAD_MS). Reset to 0 first
        # so a recording with play_earcon=False (e.g. command mode) never
        # inherits a stale value from a previous hotkey press.
        self._last_recording_earcon_ms = 0.0
        if play_earcon:
            self.play_sound("start", use_winsound=True)
            _start_earcon = self._sound_cache.get('start')
            if (_start_earcon is not None and self.config.get('audio_feedback', True)
                    and self.config.get('sound_volume', 0.5) > 0):
                self._last_recording_earcon_ms = len(_start_earcon) / self._sound_stream_sr * 1000.0
            time.sleep(0.15)  # Brief pause for sound to start

        if not streaming:
            # ACE path (ACE-03): DictationSessionConsumer provides audio from the
            # permanent engine ring. activate() rewinds to include prebuffer history
            # and applies the TTS contamination guard internally.
            if self._dictation_consumer is None:
                logger.error("[ERROR] ACE dictation consumer not available — cannot record")
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
        """Stop recording and transcribe.

        Thin wrapper around _stop_recording_impl() whose only job is the
        audio-ducking restore guarantee: _stop_recording_impl() is a large
        function with many internal early returns and exception paths (see
        its own docstring/body) -- wrapping it here, rather than editing
        every one of those paths, guarantees _restore_audio() ALWAYS runs
        when a dictation window that _duck_audio() may have ducked for
        closes, regardless of how this call ends. audio_ducking.restore()
        is itself a no-op when nothing was ducked, so this is always safe
        to call unconditionally.
        """
        try:
            self._stop_recording_impl()
        finally:
            self._restore_audio()

    def _take_recording_ownership(self):
        """Return and synchronously clear ownership for the recording being stopped.

        The returned tuple is immutable so background transcription cannot
        observe mode/ghost flags belonging to a later recording.
        """
        ownership = _RecordingOwnership(
            is_command=bool(getattr(self, 'command_mode_recording', False)),
            is_ava=bool(getattr(self, 'ava_mode_recording', False)),
            command_ghost=bool(getattr(self, '_command_mode_ghost_tap', False)),
            ava_ghost=bool(getattr(self, '_ava_mode_ghost_tap', False)),
        )
        self.command_mode_recording = False
        self.ava_mode_recording = False
        self._command_mode_ghost_tap = False
        self._ava_mode_ghost_tap = False
        return ownership

    def _stop_recording_impl(self):
        """Stop recording and transcribe"""
        if not self.recording:
            return

        ownership = self._take_recording_ownership()
        adaptive_release_tail = bool(
            getattr(self, '_ace_dictation_active', False)
            and self.config.get('mode', 'hold') == 'hold'
            and not ownership.is_command
        )
        self.set_app_state(recording=False)
        if not adaptive_release_tail:
            # Preserve existing command/streaming timing. Normal ACE hold
            # dictation keeps WakeConsumer suppressed through its adaptive
            # tail so final words cannot seed a second wake utterance.
            if not self.hotkey_pressed:
                self._hotkey_recording = False
            self.play_sound("stop")

        # Restore tray icon — release recording reason (wake_word may keep it spinning)
        self._release_icon_chase('recording')
        self._update_tray_tooltip()

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)
        
        if not adaptive_release_tail:
            # Legacy fixed tail remains for command and streaming paths.
            tail_ms = self.config.get('recording_tail_ms', 250)
            if tail_ms > 0:
                time.sleep(tail_ms / 1000)

        if getattr(self, '_ace_dictation_active', False):
            # ACE path (ACE-03): drain consumer frames; no stream to close.
            self._ace_dictation_active = False
            if adaptive_release_tail:
                try:
                    audio = self._dictation_consumer.drain_after_release(
                        silence_ms=int(self.config.get('recording_tail_silence_ms', 300)),
                        max_tail_ms=int(self.config.get('recording_tail_max_ms', 1200)),
                        speech_threshold=float(self.config.get(
                            'recording_tail_speech_threshold', 0.008,
                        )),
                    )
                finally:
                    if not self.hotkey_pressed:
                        self._hotkey_recording = False
                    self.play_sound("stop")
            else:
                audio = self._dictation_consumer.drain()
            if audio is None:
                logger.debug("[ACE] No audio captured or epoch abort")
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
                try:
                    sess.finalize()
                except Exception as e:
                    logger.exception(f"[STREAM] finalize failed: {e}")
                return

        logger.info("[...] Transcribing...")

        # Discard sub-debounce mode taps before gate/model work. Ownership
        # was already cleared synchronously, so even this early return cannot
        # contaminate the next ordinary recording.
        if ownership.is_command and ownership.command_ghost:
            logger.info("[CMD] Ghost tap — discarding recording")
            return
        if ownership.is_ava and ownership.ava_ghost:
            logger.info("[AVA] Ghost tap — discarding recording")
            return

        # Transcribe in background to not block hotkey listener
        def transcribe():
            try:
                audio_duration = len(audio) / self.model_rate

                # Get transcription parameters based on performance mode,
                # with the hotkey-path overrides (VAD off, clean-slate reset)
                # applied -- see _build_hotkey_transcribe_params.
                transcribe_params = self._build_hotkey_transcribe_params()
                perf_mode = self.config.get('performance_mode', 'balanced')

                # Guard: Whisper hallucinates on very short audio (<0.5s)
                if audio_duration < 0.51:
                    logger.info(f"[SKIP] Audio too short ({audio_duration:.2f}s) — skipping")
                    return

                if self.config.get('debug', {}).get('dump_hotkey_buffers', False):
                    _dump_hotkey_buffer(audio, self.model_rate)

                # Kill the mechanical hotkey press/release click transient
                # before it can trigger the gate below or Whisper itself.
                audio_faded = _fade_edges(audio, self.model_rate, _FADE_MS)

                # Short-buffer presence gate: hallucinations concentrate in
                # short, near-silent buffers. Buffers longer than
                # _GATE_MAX_BUFFER_S bypass the gate entirely -- real
                # dictation must never pay VAD latency or risk being gated.
                # Head grace (2026-07-10): covers the start earcon (measured
                # duration, 0 if none played this recording) plus a fixed
                # pad for the mechanical key-click transient -- see
                # _GATE_HEAD_GRACE_CLICK_PAD_MS.
                _head_grace_ms = self._last_recording_earcon_ms + _GATE_HEAD_GRACE_CLICK_PAD_MS
                if audio_duration <= _GATE_MAX_BUFFER_S and not self._buffer_has_contiguous_speech(
                    audio_faded, self.model_rate,
                    min_ms=_GATE_MIN_CONTIG_MS, prob_threshold=_GATE_VAD_PROB,
                    head_grace_ms=_head_grace_ms,
                ):
                    logger.debug(f"[GATE] No contiguous speech in short buffer "
                          f"({audio_duration:.2f}s) — skipping")
                    # FM3 diagnostics: this buffer never reached the model at
                    # all -- distinct from outcome="empty" (model ran, text
                    # came back blank). No transcription happened here, so no
                    # segment signals exist (n_segments stays at its 0
                    # default). Never raises/blocks the (already-decided)
                    # early return.
                    try:
                        diagnostics.record(diagnostics.DiagRecord(
                            mode="command" if ownership.is_command else "hotkey",
                            audio_s=audio_duration,
                            model_name=self.config.get('model_size', ''),
                            device=getattr(self, 'device_type', 'unknown'),
                            compute_type=self.config.get('compute_type', ''),
                            outcome="gated",
                            language=_languages.describe_diagnostics_language(
                                self.config.get('language', 'en'),
                            ),
                        ), app=self)
                    except Exception as _diag_exc:
                        logger.debug(f"[DIAG] gated record failed: {_diag_exc}")
                    return

                transcribe_start = time.time()
                _diag_all_segs = []
                _detected_lang = None
                _diag_path = "long" if audio_duration > 30.0 else "short"

                if audio_duration > 30.0:
                    # Long audio: split at silence boundaries before transcription.
                    # Whisper's internal 30s chunking splits at arbitrary positions
                    # that can land mid-word.  Splitting at silence boundaries first
                    # keeps each chunk acoustically clean.
                    #
                    # Do NOT set condition_on_previous_text=True here — conditioning
                    # over long stitched sequences triggers Whisper's repetition-loop
                    # hallucination bug (the model echos earlier text indefinitely).
                    # The clean-slate reset in _build_hotkey_transcribe_params is
                    # condition_on_previous_text only -- initial_prompt is NOT cleared;
                    # the voice-training vocabulary still biases every chunk here too.
                    chunks = _split_audio_at_silences(audio_faded, self.model_rate)
                    logger.info(f"[LONG] {audio_duration:.1f}s recording split into "
                          f"{len(chunks)} chunk(s) at silence boundaries")
                    texts = []
                    for idx, chunk in enumerate(chunks):
                        chunk_dur = len(chunk) / self.model_rate
                        if chunk_dur < 0.2:
                            continue
                        with self.model_lock:
                            segs, _chunk_info = self.model.transcribe(chunk, **transcribe_params)
                        _detected_lang = getattr(_chunk_info, 'language', None) or _detected_lang
                        _segs_list = list(segs)
                        _diag_all_segs.extend(_segs_list)
                        chunk_text = "".join(s.text for s in _segs_list).strip()
                        if _is_hallucinated_segments(_segs_list, chunk_text):
                            logging.getLogger("Samsara").info(
                                f"[GUARD] Suppressed hallucinated chunk {idx+1}: {chunk_text!r}")
                            chunk_text = ""
                        elif _is_quality_exhausted(_segs_list, transcribe_params):
                            _sig = diagnostics.segment_signals(_segs_list)
                            logging.getLogger("Samsara").info(
                                f"[QUALITY] decode ladder exhausted (logprob "
                                f"{_sig['avg_logprob']}, compression {_sig['compression_ratio']}) "
                                f"-- rejecting chunk {idx+1}: {chunk_text!r}")
                            chunk_text = ""
                        if chunk_text:
                            texts.append(chunk_text)
                        logger.info(f"[LONG] Chunk {idx + 1}/{len(chunks)}: "
                              f"{chunk_dur:.1f}s → {len(chunk_text)} chars")
                    text = " ".join(texts).strip()
                else:
                    with self.model_lock:
                        segments, info = self.model.transcribe(audio_faded, **transcribe_params)
                    _detected_lang = getattr(info, 'language', None) or _detected_lang
                    _seg_list = list(segments)
                    _diag_all_segs.extend(_seg_list)
                    text = "".join([s.text for s in _seg_list]).strip()
                    if _is_hallucinated_segments(_seg_list, text):
                        logging.getLogger("Samsara").info(
                            f"[GUARD] Suppressed hallucination: {text!r}")
                        text = ""
                    elif _is_quality_exhausted(_seg_list, transcribe_params):
                        _sig = diagnostics.segment_signals(_seg_list)
                        logging.getLogger("Samsara").info(
                            f"[QUALITY] decode ladder exhausted (logprob "
                            f"{_sig['avg_logprob']}, compression {_sig['compression_ratio']}) "
                            f"-- rejecting: {text!r}")
                        text = ""

                transcribe_time = time.time() - transcribe_start
                t_transcribe_ms = int(transcribe_time * 1000)
                try:
                    _diag_sig = diagnostics.segment_signals(_diag_all_segs)
                except Exception as _diag_exc:
                    logger.debug(f"[DIAG] segment signal extraction failed: {_diag_exc}")
                    _diag_sig = {}

                # Performance logging
                rtf = transcribe_time / audio_duration if audio_duration > 0 else 0
                device_info = getattr(self, 'device_type', 'unknown')
                logger.debug(f"[PERF] Audio: {audio_duration:.1f}s | Transcribe: {transcribe_time*1000:.0f}ms | "
                      f"RTF: {rtf:.2f}x | Mode: {perf_mode} | Device: {device_info}")
                
                # Apply corrections dictionary
                _bench_raw_transcript = text
                text = self.voice_training_window.apply_corrections(text)

                is_command_mode = ownership.is_command
                is_ava_mode = ownership.is_ava

                if text:
                    text_lower = text.lower().strip()

                    # Voice exit from Mouse 4 command mode
                    if is_command_mode and any(
                        p in text_lower for p in ["exit command mode", "stop listening"]
                    ):
                        logger.info(f"[CMD MODE] Voice exit: '{text_lower}'")
                        self.exit_command_mode()
                        return

                    # Command matching ONLY runs in command mode (Right Ctrl / Mouse 4).
                    # Hold-to-dictate (Ctrl+Shift) always outputs text — never matches
                    # commands, so words like "bring", "copy", "cut" are transcribed
                    # as-is rather than firing the corresponding voice command.
                    if is_command_mode:
                        try:
                            diagnostics.record(diagnostics.DiagRecord(
                                mode="command",
                                audio_s=audio_duration,
                                model_name=self.config.get('model_size', ''),
                                device=getattr(self, 'device_type', 'unknown'),
                                compute_type=self.config.get('compute_type', ''),
                                t_transcribe_ms=t_transcribe_ms,
                                t_total_ms=t_transcribe_ms,
                                text=text,
                                # Command-mode transcription is always forced
                                # to English (see _build_hotkey_transcribe_params)
                                # regardless of the general dictation language.
                                language="en",
                                **_diag_sig,
                            ), app=self)
                        except Exception as _diag_exc:
                            logger.debug(f"[DIAG] command-mode record failed: {_diag_exc}")

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
                                    timeout_s = cm_cfg.get('inactivity_timeout_s', 300)
                                    self._reset_command_mode_inactivity_timer(timeout_s)
                                    thread_registry.spawn(
                                        "dictation._rearm_command_recording",
                                        self._rearm_command_recording, daemon=True)
                            return

                        # No command matched in command mode — don't output text
                        logger.info(f"[CMD] No command matched: '{text}'")
                        if self.command_mode_active:
                            self._command_mode_miss_count += 1
                            cm_cfg = self.config.get('command_mode', {})
                            miss_limit = cm_cfg.get('miss_limit', 5)
                            if (cm_cfg.get('mode', 'hold') == 'toggle'
                                    and self._command_mode_miss_count >= miss_limit):
                                logger.info(f"[CMD MODE] Miss limit ({miss_limit}) reached")
                                self.exit_command_mode()
                            elif cm_cfg.get('mode', 'hold') == 'toggle':
                                thread_registry.spawn(
                                    "dictation._rearm_command_recording",
                                    self._rearm_command_recording, daemon=True)
                        return

                    # --- Ava mode (Right Alt) ---
                    if is_ava_mode:
                        self._route_to_ava(text)
                        return

                    # Regular dictation mode - proceed with text output
                    # Apply text processing (auto-capitalize, number formatting)
                    _diag_corr_start = time.perf_counter()
                    text = self.process_transcription(text)

                    # Deterministic cleanup (filler removal, spacing).
                    raw = text
                    _cmode = 'verbatim' if getattr(self, '_skip_cleanup', False) else self.config.get('cleanup_mode', 'clean')
                    text = clean_text(text, mode=_cmode)
                    t_corrections_ms = int((time.perf_counter() - _diag_corr_start) * 1000)

                    # Smart Corrections (optional LLM cleanup pass) -- hotkey
                    # hold-to-dictate gate. Runs on this same worker thread;
                    # never blocks output on failure (see smart_correct docs).
                    t_smart_ms = -1
                    smart_changed = False
                    if self.config.get('smart_corrections', {}).get('modes', {}).get('hotkey', True):
                        _diag_smart_start = time.perf_counter()
                        _text_before_smart = text
                        text = smart_correct(text, self)
                        t_smart_ms = int((time.perf_counter() - _diag_smart_start) * 1000)
                        smart_changed = (text != _text_before_smart)

                    if self.config['add_trailing_space']:
                        text = text + " "

                    # Inline formatting tokens ("new line" -> \n, etc.) --
                    # after smart_correct, before delivery/history, so
                    # history stores what was actually typed (see
                    # _apply_formatting_tokens).
                    text = self._apply_formatting_tokens(text)

                    logger.info(f"[OK] {text}")
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

                    # Diagnostics record -- total measured from transcribe start
                    # to just before paste, matching the smart_correct call site.
                    try:
                        diagnostics.record(diagnostics.DiagRecord(
                            mode="hotkey",
                            audio_s=audio_duration,
                            model_name=self.config.get('model_size', ''),
                            device=getattr(self, 'device_type', 'unknown'),
                            compute_type=self.config.get('compute_type', ''),
                            t_transcribe_ms=t_transcribe_ms,
                            t_corrections_ms=t_corrections_ms,
                            t_smart_ms=t_smart_ms,
                            t_total_ms=int((time.time() - transcribe_start) * 1000),
                            text=text,
                            smart_changed=smart_changed,
                            language=_languages.describe_diagnostics_language(
                                self.config.get('language', 'en'), _detected_lang,
                            ),
                            **_diag_sig,
                        ), app=self)
                    except Exception as _diag_exc:
                        logger.debug(f"[DIAG] hotkey record failed: {_diag_exc}")

                    # Personal WER benchmark sample (opt-in, off by default --
                    # see samsara/benchmark_store.py). Raw audio buffer at
                    # model rate, pre-corrections transcript, and this fully
                    # processed text. Never affects dictation output on failure.
                    try:
                        benchmark_store.append_sample(
                            self, audio, self.model_rate,
                            _bench_raw_transcript, text.strip(),
                            self.config.get('model_size', ''),
                        )
                    except Exception as _bench_exc:
                        logger.debug(f"[BENCH] append_sample failed: {_bench_exc}")

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
                    logger.info("No speech detected")
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

                    # FM3 diagnostics: the model DID run (unlike outcome=
                    # "gated" above) but produced no usable text -- record it
                    # as a first-class event so this failure mode leaves a
                    # trail. n_segments (from _diag_sig, already computed
                    # above -- no duplicate signal extraction) is the
                    # CRITICAL disambiguator: 0 means the model returned
                    # nothing at all; >0 means segments came back but were
                    # suppressed/blank (hallucination guard or the native
                    # no_speech_threshold/log_prob_threshold gates). Never
                    # raises/blocks the (already-decided) empty return.
                    try:
                        diagnostics.record(diagnostics.DiagRecord(
                            mode="command" if is_command_mode else "hotkey",
                            audio_s=audio_duration,
                            model_name=self.config.get('model_size', ''),
                            device=getattr(self, 'device_type', 'unknown'),
                            compute_type=self.config.get('compute_type', ''),
                            t_transcribe_ms=t_transcribe_ms,
                            t_total_ms=int((time.time() - transcribe_start) * 1000),
                            text="",
                            outcome="empty",
                            path=_diag_path,
                            language=_languages.describe_diagnostics_language(
                                self.config.get('language', 'en'), _detected_lang,
                            ),
                            **_diag_sig,
                        ), app=self)
                    except Exception as _diag_exc:
                        logger.debug(f"[DIAG] empty-result record failed: {_diag_exc}")

            except Exception as e:
                logger.exception(f"[ERROR] Transcription failed: {e}")
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
                except Exception as _snd_err:
                    logger.debug(f"Failure earcon (winsound) unavailable: {_snd_err}")

        thread = thread_registry.spawn("dictation.transcribe", transcribe, daemon=True)

    def cancel_recording(self):
        """Cancel recording without transcribing"""
        streaming_session = getattr(self, '_streaming_session', None)
        if not self.recording and streaming_session is None:
            return

        self.set_app_state(recording=False)
        if not self.hotkey_pressed:
            self._hotkey_recording = False  # Re-enable wake word processing
        logger.info("[X] Recording cancelled")

        if streaming_session is not None:
            self._ace_streaming_active = False
            try:
                streaming_session.cancel()
            except Exception as e:
                logger.exception(f"[STREAM] cancel failed: {e}")
        elif getattr(self, '_ace_dictation_active', False):
            # ACE path: discard accumulated frames, no stream to close.
            self._ace_dictation_active = False
            if self._dictation_consumer is not None:
                self._dictation_consumer.cancel()

        self.play_sound("error")  # Play error sound to indicate cancellation

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)
            self._schedule_ui(self.listening_indicator.flash_error)

    def _on_streaming_session_finished(self, session):
        """Release app ownership only if ``session`` is still the owner."""
        if getattr(self, '_streaming_session', None) is not session:
            return
        self._streaming_session = None
        if getattr(self, '_capslock_streaming_session', None) is session:
            self._capslock_streaming_session = None
        self._ace_streaming_active = False
        if not self.recording:
            self._hotkey_recording = False

    def apply_mode(self, new_mode):
        """Apply a capture-mode change at runtime.

        Valid modes: 'hold', 'toggle', 'continuous'.
        Wake word is now a separate boolean (see set_wake_word_enabled).
        Returns True if the mode was applied, False if unchanged or invalid.
        """
        valid_modes = ('hold', 'toggle', 'continuous')
        if new_mode not in valid_modes:
            logger.info(f"[MODE] Refused invalid mode: {new_mode}")
            return False

        current_mode = self.config.get('mode', 'hold')
        if new_mode == current_mode:
            return False

        # If currently recording (hold or toggle mode), stop the recording
        if self.recording:
            self.stop_recording()
            logger.info(f"[MODE] Stopped active recording before mode switch")

        # Reset toggle state so it doesn't carry over
        self.toggle_active = False

        # Stop continuous mode if it was active but new mode is different
        if self.continuous_active and new_mode != 'continuous':
            self.stop_continuous_mode()
            logger.info(f"[MODE] Deactivated continuous mode")

        # Activate continuous if that's the new mode
        if new_mode == 'continuous' and not self.continuous_active:
            self.start_continuous_mode()
            logger.info(f"[MODE] Activated continuous mode")

        self.config['mode'] = new_mode
        logger.info(f"[MODE] Mode changed to: {new_mode}")

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
            logger.info("[GESTURE] Lane started")
        except Exception as _e:
            logger.error(f"[GESTURE] Failed to start: {_e}")
            self._camera_service = None
            self._gesture_loop = None

    def _stop_gesture_lane(self) -> None:
        """Stop GestureLoop and release camera handle."""
        loop = self._gesture_loop
        if loop is not None:
            try:
                loop.stop()
            except Exception as e:
                logger.debug(f"[GESTURE] Loop stop failed: {e}")
            self._gesture_loop = None
        svc = self._camera_service
        if svc is not None:
            try:
                svc.stop()
            except Exception as e:
                logger.debug(f"[GESTURE] Camera service stop failed: {e}")
            self._camera_service = None
        logger.info("[GESTURE] Lane stopped")

    def set_gesture_enabled(self, enabled: bool) -> None:
        """Enable or disable the gesture lane and persist the setting."""
        with self._config_lock:
            self.config.setdefault('gesture', {})['enabled'] = enabled
            self.save_config()
        if enabled and self._gesture_loop is None:
            self._start_gesture_lane()
            logger.info("[GESTURE] Lane ENABLED")
        elif not enabled and self._gesture_loop is not None:
            self._stop_gesture_lane()
            logger.info("[GESTURE] Lane DISABLED")

    def set_wake_word_enabled(self, enabled):
        """Start or stop the wake word listener independently of capture mode."""
        with self._config_lock:
            self.config['wake_word_enabled'] = enabled
            self.save_config()
        if enabled and not self.wake_word_active:
            self.start_wake_word_mode()
            logger.info("[WAKE] Wake word listener ENABLED")
        elif not enabled and self.wake_word_active:
            self.stop_wake_word_mode()
            logger.info("[WAKE] Wake word listener DISABLED")
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
            logger.exception(f"[UI] Failed to show main window: {e}")

    def hide_main_window(self):
        """Close button on the hub: just minimize to tray."""
        try:
            self.main_window.hide()
        except Exception as e:
            logger.exception(f"[UI] Failed to hide main window: {e}")

    def set_streaming_mode(self, enabled):
        """Tray-menu entry point: flip the streaming-mode flag."""
        enabled = bool(enabled)
        with self._capslock_lifecycle_lock:
            if not enabled and getattr(self, '_streaming_session', None) is not None:
                logger.info("[STREAM] Mode disabled during active session -- cancelling")
                self.cancel_recording()
            if self.config.get('streaming_mode', False) == enabled:
                return
            with self._config_lock:
                self.config['streaming_mode'] = enabled
                self.save_config()
            logger.info(f"[STREAM] streaming_mode -> {enabled}")

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
        logger.info(f"[CLEANUP] Mode -> {mode}")

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
                logger.exception("_schedule_ui direct-call fallback failed")

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
            except OSError as e:
                logger.debug(f"Tray icon idle-image swap failed: {e}")

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
            except OSError as e:
                # transient WinError during icon handle swap — skip this frame
                logger.debug(f"Tray icon animation frame swap failed: {e}")

        self._icon_chase_timer = thread_registry.timer(
            "dictation.icon_chase", tick_interval,
            self._icon_chase_tick, daemon=True)
    
    def open_settings(self):
        """Open settings window"""
        try:
            if not hasattr(self, '_settings_qt'):
                from samsara.ui.settings_qt import SettingsQt
                self._settings_qt = SettingsQt(self)
            self._settings_qt.show()
        except Exception as e:
            logger.exception(f"[SETTINGS] Error opening settings: {e}")
    
    def open_voice_training(self):
        """Open voice training window"""
        try:
            self.voice_training_window.show()
        except Exception as e:
            logger.exception(f"Error opening voice training: {e}")

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
                logger.exception(f"[TUTORIAL] Failed to open tutorial: {_e}")
        self._schedule_ui(_open)

    def open_history(self):
        """Open dictation history window"""
        try:
            if not hasattr(self, '_history_qt'):
                from samsara.ui.history_qt import HistoryQt
                self._history_qt = HistoryQt(self)
            self._history_qt.show()
        except Exception as e:
            logger.exception(f"[HISTORY] Error opening history: {e}")

    def open_dictation_diagnostics(self):
        """Open dictation diagnostics window"""
        try:
            if not hasattr(self, '_diagnostics_qt'):
                from samsara.ui.diagnostics_qt import DiagnosticsQt
                self._diagnostics_qt = DiagnosticsQt(self)
            self._diagnostics_qt.show()
        except Exception as e:
            logger.exception(f"[DIAG] Error opening dictation diagnostics: {e}")

    def open_quick_reference(self):
        """Open the Quick Reference window (live hotkeys/phrases/modes)"""
        try:
            if not hasattr(self, '_quick_reference_qt'):
                from samsara.ui.quick_reference_qt import QuickReferenceQt
                self._quick_reference_qt = QuickReferenceQt(self)
            self._quick_reference_qt.show()
        except Exception as e:
            logger.exception(f"[QUICKREF] Error opening quick reference: {e}")

    def open_correction_capture(self):
        """Open the correction-capture window, pre-filled with the most
        recent dictation. Safe to call from any thread -- history lookup
        happens here (on whatever thread called this), window construction
        is posted to the Qt thread by CorrectionCaptureQt itself."""
        try:
            rows = self.history_store.query(type_filter='dictation', limit=1)
            last_text = rows[0]['display_text'] if rows else ''
            from samsara.ui.correction_capture_qt import CorrectionCaptureQt
            CorrectionCaptureQt(self).open(last_text)
        except Exception as e:
            logger.exception(f"[CORRECT-CAP] Error opening correction capture: {e}")

    def open_benchmark_review(self):
        """Open the personal WER benchmark gold-standard review window"""
        try:
            if not hasattr(self, '_benchmark_review_qt'):
                from samsara.ui.benchmark_review_qt import BenchmarkReviewQt
                self._benchmark_review_qt = BenchmarkReviewQt(self)
            self._benchmark_review_qt.show()
        except Exception as e:
            logger.exception(f"[BENCH] Error opening benchmark review: {e}")

    def open_log_viewer(self):
        """Open the live log viewer window"""
        try:
            if not hasattr(self, '_log_viewer_qt'):
                from samsara.ui.log_viewer_qt import LogViewerQt
                self._log_viewer_qt = LogViewerQt(self)
            self._log_viewer_qt.show()
        except Exception as e:
            logger.exception(f"[LOGVIEW] Error opening log viewer: {e}")

    def open_stress_test_wizard(self):
        """Open the guided stress-test wizard"""
        try:
            if not hasattr(self, '_stress_wizard_qt'):
                from samsara.ui.stress_wizard_qt import StressWizardQt
                self._stress_wizard_qt = StressWizardQt(self)
            self._stress_wizard_qt.show()
        except Exception as e:
            logger.exception(f"[STRESS] Error opening stress test wizard: {e}")

    def open_wake_word_debug(self):
        """Open wake word debug/test window"""
        try:
            self.wake_word_debug_window.show()
        except Exception as e:
            logger.exception(f"Error opening wake word debug: {e}")

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
            logger.info(f"[SNOOZE] Listening snoozed for {minutes} min (resumes at {resume_str})")

            self._snooze_timer = thread_registry.timer(
                "dictation.snooze_expire", minutes * 60,
                self._on_snooze_expire, daemon=True)
        else:
            self._snooze_resume_time = None
            logger.info("[SNOOZE] Listening snoozed until manually resumed")

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
        logger.info("[SNOOZE] Listening resumed")

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
            logger.debug("[AEC-CAL] Cannot calibrate while audio capture is active. "
                  "Stop dictation and try again.")
            return

        def _run():
            logger.debug("[AEC-CAL] Starting calibration...")
            try:
                result = self.echo_canceller.calibrate_lag(
                    mic_device_index=self.config.get('microphone'),
                    mic_rate=self.capture_rate,
                )
            except Exception as e:
                logger.exception(f"[AEC-CAL] Calibration failed with exception: {e}")
                return

            logger.debug(f"[AEC-CAL] Result: {result}")
            if result['success']:
                lag = result['lag_ms']
                logger.debug(
                    f"[AEC-CAL] To apply this value, edit config.json: "
                    f'"echo_cancellation": {{"latency_ms": {lag:.1f}}}'
                )
            else:
                logger.debug(f"[AEC-CAL] Calibration not reliable: {result['message']}")

        thread_registry.spawn("aec-calibrate", _run, daemon=True)

    def _dispatch_command(self, _cmd):
        """Re-execute self._last_command_name via the normal dispatch path."""
        self.command_executor.process_text(self._last_command_name, self)

    def repeat_last_command(self):
        """Re-execute the last repeatable command ("repeat" / "again")."""
        if self._last_command is None:
            logger.info("[REPEAT] No repeatable command in history.")
            return
        logger.info(f"[REPEAT] {self._last_command_name}")
        self._dispatch_command(self._last_command)

    def toggle_listening_indicator(self):
        """Toggle the listening indicator overlay on/off and persist to config."""
        enabled = not self.config.get('listening_indicator_enabled', False)
        with self._config_lock:
            self.config['listening_indicator_enabled'] = enabled
            self.save_config()
        self.apply_listening_indicator_settings()

    def apply_listening_indicator_settings(self):
        """Apply the persisted indicator visibility and placement live.

        Settings already run on Qt's UI thread, but this method is also used
        by tray actions and may therefore be called from another thread.  Keep
        the widget work behind the application's normal UI scheduler.
        """
        indicator = getattr(self, 'listening_indicator', None)
        if indicator is None:
            return

        enabled = bool(self.config.get('listening_indicator_enabled', False))
        position = self.config.get('listening_indicator_position', 'bottom-center')
        custom = self.config.get('listening_indicator_custom_position')

        def _apply():
            if position == 'custom' and isinstance(custom, dict):
                indicator.set_custom_position(
                    custom.get('screen'),
                    custom.get('cx') if custom.get('cx') is not None else 0.5,
                    custom.get('cy') if custom.get('cy') is not None else 0.5,
                )
            else:
                indicator.set_position(position)

            if enabled:
                indicator.show()
            else:
                indicator.hide()

        self._schedule_ui(_apply)

    def enter_indicator_move_mode(self):
        """Tray action: temporarily unlock the listening indicator so it can
        be left-dragged to a custom on-screen position."""
        if not hasattr(self, 'listening_indicator') or self.listening_indicator is None:
            return
        self._schedule_ui(self.listening_indicator.enter_move_mode)

    def _on_indicator_placement_committed(self, payload):
        """ListeningIndicator.placement_committed handler -- persists a
        drag-to-position commit or a preset chosen from its move-mode
        right-click menu. The widget owns drag/geometry math only; this is
        the single place that writes it to config, via the existing
        update_config_and_save() persistence path."""
        if payload.get('type') == 'custom':
            updates = {
                'listening_indicator_position': 'custom',
                'listening_indicator_custom_position': {
                    'screen': payload.get('screen'),
                    'cx': payload.get('cx'),
                    'cy': payload.get('cy'),
                },
            }
        else:
            updates = {
                'listening_indicator_position': payload.get('position', 'bottom-center'),
            }
        self.update_config_and_save(updates)

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

    def preview_first_run(self):
        """Relaunch Samsara as a second, independent process pointed at a
        fresh temp SAMSARA_HOME_DIR, so the first-run wizard fires without
        touching the real profile in samsara_home_dir(). This instance is
        left running -- it is NOT a restart (see restart_app in
        plugins.commands.core_utils for that). The temp dir is left on disk
        for inspection; _reap_old_preview_profiles() reclaims stale ones on
        the NEXT preview launch (the detached child can't reliably clean up
        after itself -- see that function's docstring).

        Temp-dir creation, arg construction, and the actual spawn are ALL
        inside the try below -- previously only the Popen call itself was
        guarded, so a failure in mkdtemp()/_build_restart_args() would raise
        uncaught, and a Popen failure was logged but otherwise invisible.
        This is a manually-triggered dev action with no other feedback
        path, so any failure here now also surfaces a visible toast instead
        of silently no-op'ing.
        """
        import tempfile
        # Reuses the same frozen-vs-source argv logic as the "restart" voice
        # command instead of duplicating it here.
        from plugins.commands.core_utils import _build_restart_args

        home_dir = None
        diagnostic_handle = None
        try:
            _reap_old_preview_profiles()

            home_dir = tempfile.mkdtemp(prefix="samsara_firstrun_")
            logger.info(f"[PREVIEW] Launching first-run preview, SAMSARA_HOME_DIR={home_dir}")

            args, cwd = _build_restart_args()
            env = os.environ.copy()
            env["SAMSARA_HOME_DIR"] = home_dir

            # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP break the child out
            # of the parent's Windows Job Object so it survives after the
            # parent exits.
            flags = 0
            if sys.platform == 'win32':
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

            diagnostic_path = Path(home_dir) / _PREVIEW_DIAGNOSTIC_NAME
            diagnostic_handle = open(
                diagnostic_path, "w", encoding="utf-8", buffering=1,
            )
            process = subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=diagnostic_handle,
                stderr=diagnostic_handle,
            )
            # Popen duplicated/inherited the explicit stdio handle. The parent
            # must close its copy immediately; the detached child keeps its
            # own handle for diagnostics without tying lifetime to this app.
            diagnostic_handle.close()
            diagnostic_handle = None
            try:
                thread_registry.spawn(
                    "preview-startup-monitor",
                    _monitor_preview_startup,
                    args=(process, diagnostic_path),
                    daemon=True,
                )
            except Exception as monitor_exc:
                # The child did launch; don't delete its isolated profile or
                # claim otherwise. Make the lost failure-monitoring path loud.
                _show_preview_failure(
                    f"Preview launched, but startup monitoring failed: {monitor_exc}",
                    diagnostic_path,
                )
        except Exception as e:
            if diagnostic_handle is not None:
                try:
                    diagnostic_handle.close()
                except Exception:
                    pass
            # No child owns this profile if spawn itself failed, so clean it
            # now rather than waiting an hour for the next preview sweep.
            if home_dir:
                shutil.rmtree(home_dir, ignore_errors=True)
            _show_preview_failure(f"Could not launch the preview instance: {e}")

    def _show_alarm_notification(self, alarm: dict) -> None:
        """Show the visual companion to an alarm's persistent sound."""
        from samsara.ui.reminder_toast import get_toast

        name = str(alarm.get('name') or 'Unnamed')
        posted = get_toast().show(
            f"Alarm: {name}",
            (
                'Choose Complete or Dismiss below, say "complete alarm" or '
                '"dismiss alarm", or use your configured alarm shortcuts.'
            ),
            on_dismiss=self.alarm_manager.dismiss,
            on_complete=self.alarm_manager.complete,
        )
        if not posted:
            logger.warning(f"[ALARM] Visual notification rejected for {name!r}")

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
            logger.info("[LOG] No log file found.")

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
        logger.info("[EXIT] Shutting down Samsara...")

        # Signal background threads (e.g. stream-health monitor) to stop
        self._running = False

        # Every step below is independent, best-effort teardown: one
        # subsystem failing to stop cleanly must never block the rest of
        # shutdown or the final os._exit(0) -- so each keeps swallowing
        # after logging (Tier 1 "genuinely-optional" rule for exit-path
        # cleanup), never re-raising.

        # Stop config file watcher
        try:
            if self._config_watcher is not None:
                self._config_watcher.stop()
        except Exception as e:
            logger.debug(f"[EXIT] Config watcher stop failed: {e}")

        # Stop icon chase animation timer
        try:
            self._stop_icon_chase()
        except Exception as e:
            logger.debug(f"[EXIT] Icon chase stop failed: {e}")

        try:
            if self.continuous_active:
                self.stop_continuous_mode()
        except Exception as e:
            logger.debug(f"[EXIT] Continuous mode stop failed: {e}")

        try:
            if self.wake_word_active:
                self.stop_wake_word_mode()
        except Exception as e:
            logger.debug(f"[EXIT] Wake word mode stop failed: {e}")

        try:
            self._stop_gesture_lane()
        except Exception as e:
            logger.debug(f"[EXIT] Gesture lane stop failed: {e}")

        # Stop key macro manager (releases any held keys)
        try:
            if hasattr(self, 'key_macro_manager') and self.key_macro_manager:
                self.key_macro_manager.stop()
        except Exception as e:
            logger.debug(f"[EXIT] Key macro manager stop failed: {e}")

        # Stop notification manager
        try:
            if hasattr(self, 'notification_manager') and self.notification_manager:
                self.notification_manager.stop()
        except Exception as e:
            logger.debug(f"[EXIT] Notification manager stop failed: {e}")

        # Alarm callbacks can post a reminder toast, so stop their producer
        # before terminally stopping the toast itself.
        try:
            if hasattr(self, 'alarm_manager') and self.alarm_manager:
                self.alarm_manager.stop()
        except Exception as e:
            logger.debug(f"[EXIT] Alarm manager stop failed: {e}")

        # Terminally stop the toast before Show Numbers is torn down below.
        try:
            from samsara.ui.reminder_toast import get_toast
            get_toast().stop()
        except Exception as e:
            logger.debug(f"[EXIT] Reminder toast stop failed: {e}")

        # Stop ACE engine (deactivates consumer, flushes debug WAV if any)
        try:
            if hasattr(self, '_ace_engine') or hasattr(self, '_dictation_consumer'):
                self._stop_ace_engine()
        except Exception as e:
            logger.debug(f"[EXIT] ACE engine stop failed: {e}")

        # Stop echo cancellation
        try:
            if hasattr(self, 'echo_canceller'):
                self.echo_canceller.stop()
        except Exception as e:
            logger.debug(f"[EXIT] Echo canceller stop failed: {e}")

        # Cancel snooze timer
        try:
            if self._snooze_timer is not None:
                self._snooze_timer.cancel()
                self._snooze_timer = None
        except Exception as e:
            logger.debug(f"[EXIT] Snooze timer cancel failed: {e}")

        # Destroy listening indicator
        try:
            if hasattr(self, 'listening_indicator'):
                self.listening_indicator.destroy()
        except Exception as e:
            logger.debug(f"[EXIT] Listening indicator destroy failed: {e}")

        # Destroy command cheat sheet
        try:
            if hasattr(self, 'cheat_sheet'):
                self.cheat_sheet.destroy()
        except Exception as e:
            logger.debug(f"[EXIT] Cheat sheet destroy failed: {e}")

        # Destroy show-numbers layered overlay
        try:
            from plugins.commands.show_numbers import _destroy_overlay_completely
            _destroy_overlay_completely()
        except Exception as e:
            logger.debug(f"[EXIT] Show-numbers overlay destroy failed: {e}")

        # Shut down TTS coordinator + engine before the earcon stream closes
        try:
            if getattr(self, 'audio_coordinator', None) is not None:
                self.audio_coordinator.shutdown()
        except Exception as e:
            logger.debug(f"[EXIT] Audio coordinator shutdown failed: {e}")
        try:
            if getattr(self, 'tts_engine', None) is not None:
                self.tts_engine.shutdown()
        except Exception as e:
            logger.debug(f"[EXIT] TTS engine shutdown failed: {e}")

        # Stop output device watcher before closing the stream it manages
        try:
            stop_evt = getattr(self, '_output_watcher_stop', None)
            if stop_evt is not None:
                stop_evt.set()
        except Exception as e:
            logger.debug(f"[EXIT] Output device watcher stop failed: {e}")

        # Stop persistent sound stream
        try:
            self.stop_sound_stream()
        except Exception as e:
            logger.debug(f"[EXIT] Sound stream stop failed: {e}")

        # Close main hub window (saves geometry to config)
        try:
            if getattr(self, 'main_window', None) is not None:
                self.main_window.close()
        except Exception as e:
            logger.debug(f"[EXIT] Main window close failed: {e}")

        # Close persistent history database
        try:
            if getattr(self, 'history_db', None) is not None:
                self.history_db.close()
        except Exception as e:
            logger.debug(f"[EXIT] History DB close failed: {e}")

        # Stop keyboard listener
        try:
            self.keyboard_listener.stop()
        except Exception as e:
            logger.debug(f"[EXIT] Keyboard listener stop failed: {e}")

        # Stop Win32 mouse hook (Mouse 4/5 command mode)
        try:
            if getattr(self, '_mouse_hook', None) is not None:
                self._mouse_hook.stop()
        except Exception as e:
            logger.debug(f"[EXIT] Mouse hook stop failed: {e}")

        # Release the CapsLock hook so the OS resumes normal toggle behavior
        try:
            if getattr(self, '_capslock_hook', None) is not None:
                keyboard.unhook(self._capslock_hook)
                self._capslock_hook = None
        except Exception as e:
            logger.debug(f"[EXIT] CapsLock unhook failed: {e}")

        # Stop tray icon (do this before GUI cleanup)
        try:
            self.tray_icon.stop()
        except Exception as e:
            logger.debug(f"[EXIT] Tray icon stop failed: {e}")

        # Flush Ava alias use-count to disk before exit
        try:
            _ava_corrections.flush_pending()
        except Exception as e:
            logger.debug(f"[EXIT] Ava alias flush failed: {e}")

        # Flush debounced command stats and hint counters so counts inside
        # the 5-second coalesce window are not lost on clean shutdown.
        try:
            flush_command_stats()
        except Exception as e:
            logger.debug(f"[EXIT] Command stats flush failed: {e}")
        try:
            if hasattr(self, 'hints') and self.hints is not None:
                self.hints.shutdown()
        except Exception as e:
            logger.debug(f"[EXIT] Hints shutdown failed: {e}")

        # Join registered non-daemon threads (best-effort; logs stragglers,
        # never blocks past its timeout, never force-kills).
        try:
            thread_registry.shutdown()
        except Exception as e:
            logger.debug(f"[EXIT] Thread registry shutdown failed: {e}")

        # Force exit — bypasses any remaining thread cleanup but guarantees
        # termination even if a background thread or Qt modal is blocking.
        logger.info("[EXIT] Goodbye!")
        os._exit(0)

if __name__ == "__main__":
    # Console is already hidden at top of file
    _DIAG_MAIN_T = time.perf_counter()
    logger.debug(f"[BOOT-DIAG] __main__: entry (since sounddevice import: {(_DIAG_MAIN_T - _POST_SD_T)*1000:.0f}ms)")

    # Guard against double-launch. Must run before the splash / audio starts
    # so a second invocation exits cleanly without grabbing resources.
    _t = time.perf_counter()
    _acquire_instance_lock()
    _dt = (time.perf_counter() - _t) * 1000
    logger.debug(f"[BOOT-DIAG] instance lock (_check_single_instance): {_dt:.0f}ms")
    if _dt > 5000:
        logger.debug(f"[BOOT-DIAG] SLOW STEP: instance lock {_dt:.0f}ms")

    # Source builds historically kept a second config beside dictation.py.
    # Carry the newer legacy profile across exactly once, after acquiring the
    # instance lock and before any settings (including Qt scale) are read.
    if not getattr(sys, "frozen", False):
        try:
            if migrate_legacy_source_config(Path(__file__).parent / "config.json"):
                logger.info("[CONFIG] Migrated legacy source profile to the per-user profile")
        except Exception as _config_migration_exc:
            logger.warning(
                "[CONFIG] Could not migrate legacy source profile: %s",
                _config_migration_exc,
            )

    # QApplication reads QT_SCALE_FACTOR only during construction. Apply the
    # user's restart-required accessibility scale before the splash starts Qt.
    try:
        from samsara.ui_scale import apply_early_ui_scale
        _early_config_path = samsara_config_path()
        _early_scale = apply_early_ui_scale(_early_config_path)
        logger.info(f"[UI] Early interface scale: {_early_scale:g}x")
    except Exception as _scale_exc:
        logger.warning(f"[UI] Could not apply interface scale: {_scale_exc}")

    # Show splash screen during startup
    _t = time.perf_counter()
    from samsara.ui.splash_qt import SplashScreenQt
    splash = SplashScreenQt()
    _dt = (time.perf_counter() - _t) * 1000
    logger.debug(f"[BOOT-DIAG] splash init (SplashScreenQt): {_dt:.0f}ms")
    if _dt > 5000:
        logger.debug(f"[BOOT-DIAG] SLOW STEP: splash init {_dt:.0f}ms")
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
            except Exception as e:
                logger.warning(f"CapsLock hook release on abnormal exit failed: {e}")
