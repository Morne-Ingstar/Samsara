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

# Ducking-host divert (tribunal arc_20260803_163412, tier-2). A frozen build
# has no separate interpreter to hand `-m samsara.ducking_host` to, so
# audio_ducking's transport re-executes THIS executable with the sentinel
# set. This check MUST stay above every heavy import below: the child owns
# COM and two pipes and nothing else -- it must never build a second app
# (audio engine, Qt, hotkey hooks, instance lock). Harmless from source,
# where the sentinel is never set on this process (the transport puts it
# only in the child's own environment copy).
if os.environ.get("SAMSARA_DUCKING_HOST") == "1":
    from samsara.ducking_host import main as _ducking_host_main
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(_ducking_host_main())


#: Startup argument dispatch (108/F9). CLI modes that are NOT "run the app"
#: are routed here and exit, for the same reason the ducking host above does:
#: they must never build a second app, and -- the part that was actually
#: broken -- they must never reach the single-instance lock. Keep this ABOVE
#: every heavy import below.
#:
#: installer/samsara.iss runs `Samsara.exe --fetch-components <ids> ...` at
#: ssPostInstall with ewWaitUntilTerminated. Before 108 nothing dispatched it,
#: so the installer either waited on a normal long-running app (no instance
#: running) or got exit 0 from the instance lock in samsara/boot.py with no
#: fetch having happened (one already running) -- both silent. The lock is
#: ~14,700 lines below; this runs first, so a fetch during an active session
#: still fetches, which is exactly what post-install needs.
_ARGUMENT_MODES = ("--fetch-components",)


def _dispatch_startup_argument(argv):
    """Return an exit code for a non-app CLI mode, or None to start the app.

    argv is sys.argv[1:]. Kept a plain function with no app state so the
    tests can drive it without importing anything heavy."""
    if not argv or not any(arg in _ARGUMENT_MODES for arg in argv):
        return None
    # Imported here, not at module scope: the app path must not pay for it,
    # and this module is imported by tooling that never passes arguments.
    from samsara.ui.first_run_qt import fetch_components_main
    return int(fetch_components_main(argv))


_STARTUP_ARGUMENT_CODE = _dispatch_startup_argument(sys.argv[1:])
if _STARTUP_ARGUMENT_CODE is not None:
    sys.exit(_STARTUP_ARGUMENT_CODE)


# Before every heavy/native import below (torch guard, sounddevice, scipy,
# faster-whisper, Qt) so a crash during boot also leaves a dump.
from samsara.paths import samsara_home_dir as _fh_home_dir
import samsara.boot as _samsara_boot
_FAULTHANDLER_FILE = _samsara_boot._enable_faulthandler(_fh_home_dir() / "logs")

# Platform-specific imports
if sys.platform == 'win32':
    try:
        import ctypes
        import winsound
        HAS_WINSOUND = True
    except ImportError:
        HAS_WINSOUND = False

    # Taskbar identity (AUMID): must be set before any window/taskbar icon
    # is created, so this runs at module import time -- the earliest
    # possible point, well before DictationApp exists. Without this,
    # Windows groups/identifies the taskbar entry by the interpreter
    # (python.exe/pythonw.exe) rather than by Samsara, showing the
    # generic Python icon and pinning under the wrong name. A stable,
    # unique ID lets Windows show our own icon and group our windows
    # under one taskbar entry once the app is pinned or run frozen.
    try:
        import ctypes as _ctypes_aumid
        _ctypes_aumid.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "MorneIngstar.Samsara"
        )
    except Exception:
        pass
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
import samsara.torch_guard; samsara.torch_guard.install()
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

# ACE audio stack import -- moved here from _start_ace_engine (was a lazy
# in-function import). Measured 2026-07-22: this cold import (first touch
# of samsara.audio_engine -> engine.py -> scipy.signal) accounted for
# ~11.4s of a 52s boot, ALL of it spent before AudioCaptureEngine.start()
# (which itself only takes ~22ms). Importing eagerly here pays that cost
# during module load instead of blocking _start_ace_engine() later in
# __init__. Wrapped in try/except (matching the optional-UI-import pattern
# used below for tray/voice-training/etc.) so a broken install still boots
# in a degraded state instead of crashing before the splash screen shows --
# _start_ace_engine() checks for None and falls back exactly as it did
# when the import lived inside its own try/except.
_PRE_ACE_T = time.perf_counter()
try:
    from samsara.audio_engine import FrameBus, AudioCaptureEngine
    _ace_t1 = time.perf_counter()
    sys.stdout.write(f"[BOOT-DIAG] audio_engine package import (incl. scipy.signal): {(_ace_t1 - _PRE_ACE_T)*1000:.0f}ms\n")
    from samsara.audio_engine.dictation_consumer import DictationSessionConsumer
    _ace_t2 = time.perf_counter()
    sys.stdout.write(f"[BOOT-DIAG] audio_engine.dictation_consumer import: {(_ace_t2 - _ace_t1)*1000:.0f}ms\n")
    from samsara.audio_engine.continuous_consumer import ContinuousConsumer
    _ace_t3 = time.perf_counter()
    sys.stdout.write(f"[BOOT-DIAG] audio_engine.continuous_consumer import: {(_ace_t3 - _ace_t2)*1000:.0f}ms\n")
    from samsara.audio_engine.wake_consumer import WakeConsumer
    _ace_t4 = time.perf_counter()
    sys.stdout.write(f"[BOOT-DIAG] audio_engine.wake_consumer import: {(_ace_t4 - _ace_t3)*1000:.0f}ms\n")
    sys.stdout.flush()
except Exception as _ace_import_err:
    FrameBus = AudioCaptureEngine = None
    DictationSessionConsumer = ContinuousConsumer = WakeConsumer = None
    sys.stdout.write(f"[BOOT-DIAG] samsara.audio_engine import FAILED: {_ace_import_err}\n")
    sys.stdout.flush()
_ace_import_ms = (time.perf_counter() - _PRE_ACE_T) * 1000
sys.stdout.write(f"[BOOT-DIAG] samsara.audio_engine import (total): {_ace_import_ms:.0f}ms\n")
sys.stdout.flush()
if _ace_import_ms > 5000:
    sys.stdout.write(f"[BOOT-DIAG] SLOW STEP: samsara.audio_engine import {_ace_import_ms:.0f}ms\n")
    sys.stdout.flush()

from pynput import keyboard as pynput_keyboard
from pynput.keyboard import Key, Controller as KeyboardController
import keyboard  # CapsLock streaming hook ONLY (config-gated; see _install_capslock_hook)


# ---------------------------------------------------------------------------
# Raw key-state polling (2026-08-03, tribunal arc_20260803_163412)
#
# keyboard.is_pressed() lazily installs the `keyboard` library's OWN
# WH_KEYBOARD_LL hook on first call and keeps it forever -- giving this
# process TWO serialized low-level hooks (pynput + keyboard), which the
# freeze tribunal flagged as a first-tier hazard (Windows serializes the
# chain; a stall in either wedges input). GetAsyncKeyState reads key state
# with NO hook and no library machinery.
# ---------------------------------------------------------------------------
_VK_BY_NAME = {
    'ctrl': (0x11, 0xA2, 0xA3), 'shift': (0x10, 0xA0, 0xA1),
    'alt': (0x12, 0xA4, 0xA5), 'win': (0x5B, 0x5C),
    'escape': (0x1B,), 'esc': (0x1B,), 'space': (0x20,), 'tab': (0x09,),
    'enter': (0x0D,), 'backspace': (0x08,), 'caps lock': (0x14,),
    'capslock': (0x14,), 'delete': (0x2E,), 'insert': (0x2D,),
    'home': (0x24,), 'end': (0x23,), 'page up': (0x21,), 'page down': (0x22,),
    'up': (0x26,), 'down': (0x28,), 'left': (0x25,), 'right': (0x27,),
}
for _i in range(1, 25):
    _VK_BY_NAME[f'f{_i}'] = (0x6F + _i,)
for _c in 'abcdefghijklmnopqrstuvwxyz':
    _VK_BY_NAME[_c] = (ord(_c.upper()),)
for _d in '0123456789':
    _VK_BY_NAME[_d] = (ord(_d),)


def _raw_key_pressed(name: str) -> bool:
    """Hook-free key-state check via GetAsyncKeyState (high bit = down).

    Unknown names return False with a one-time warning rather than falling
    back to keyboard.is_pressed -- the fallback would silently reinstall
    the second LL hook this exists to eliminate.
    """
    vks = _VK_BY_NAME.get(name.lower().strip())
    if not vks:
        if name not in _raw_key_pressed._warned:  # type: ignore[attr-defined]
            logger.warning('[KEYS] no VK mapping for %r; treating as not pressed', name)
            _raw_key_pressed._warned.add(name)  # type: ignore[attr-defined]
        return False
    import ctypes
    ga = ctypes.windll.user32.GetAsyncKeyState
    pressed = any(ga(vk) & 0x8000 for vk in vks)
    if pressed:
        _raw_key_pressed._pressed_vks[vks] = True  # type: ignore[attr-defined]
    elif _raw_key_pressed._pressed_vks.pop(vks, False):  # type: ignore[attr-defined]
        # Record only an observed pressed -> released edge. VK tuples
        # share state across aliases (e.g. esc/escape); an initially idle
        # key is silent. Pop consumes the edge before recorder work.
        # Keep the diagnostic re-read from the cdc39ea release-verification
        # seam, but avoid both it and the recorder write lock on steady polls.
        flight_recorder.record(
            'key_state.not_pressed', key=name,
            raw_bits={hex(vk): ga(vk) for vk in vks},
        )
    return pressed


_raw_key_pressed._warned = set()  # type: ignore[attr-defined]
_raw_key_pressed._pressed_vks = {}  # type: ignore[attr-defined]

from pynput.mouse import Button, Controller as MouseController
import pyperclip
import pyautogui
if sys.stdout is not None:
    sys.stdout.write(f"[PRE-LOG] +{(time.perf_counter()-_POST_SD_T)*1000:.0f}ms (after input libs)\n")
    sys.stdout.flush()
# Check for Visual C++ Redistributable before any DLL-dependent imports.
# ctranslate2 (used by faster-whisper) requires msvcp140.dll which ships with
# the VC++ redist. On a clean machine this may not be installed.
_samsara_boot.check_vc_redistributable()

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
from samsara.smart_corrections import (
    smart_correct, warm_up as smart_corrections_warm_up, is_enabled as smart_corrections_is_enabled,
)
from samsara.formatting_tokens import apply_formatting_tokens_if_enabled
from samsara import config_defaults
from samsara import injection_safety
from samsara import diagnostics
from samsara import flight_recorder
from samsara import outcome_ring
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
    LIVE_VAD_PROB_THRESHOLD, CONTIGUOUS_VAD_PROB_THRESHOLD,
    ADAPTIVE_SPEECH_FLOOR_RATIO, HOLD_RELEASE_TAIL_SPEECH_THRESHOLD,
)
from samsara.calibration import measure_ambient_rms, calibrate_threshold
from samsara.key_macros import KeyMacroManager, get_default_macro_config
from samsara.learning import AdaptiveLearner
from samsara.notifications import NotificationManager, get_default_notification_config
from samsara.alarms import AlarmManager, get_default_alarm_config
from samsara.echo_cancel import EchoCanceller
from samsara import audio_ducking
from samsara.audio_devices import force_rescan, list_microphones
from samsara import wake_profiles
from samsara import voice_memo
from samsara import quick_memo
from samsara import paste as _paste
from samsara.clipboard import paste_with_preservation, type_text_unicode
from samsara.paste import _UNDO_TARGET_UNSET
# Queue 128: the output-text quality gates moved to samsara/transcript_gates.py
# unchanged. Every name is re-exported here -- several are unused inside this
# file and exist only so dictation.<name> keeps resolving for the tests and
# tools that reach for them by that path.
from samsara.transcript_gates import (
    _COMPRESSION_RATIO_THRESHOLD,
    _HALLUCINATION_STRING_BLACKLIST, _is_hallucinated_segments,
    _TRAILING_GARBAGE_RUN_RE, _trim_trailing_garbage_run,
    _drop_trailing_garbage_segments,
    _CONTEXT_WORD_STRIP, _context_words,
    _ECHO_MIN_WORDS, _CONTEXT_ECHO_CHIP, _is_context_echo,
    _TAIL_REPEAT_RUN, _CONTEXT_TAIL_CHARS, _SENTENCE_START_RE,
    _sanitise_context_tail,
    _is_quality_exhausted, _keep_low_confidence_long_chunk,
    _apply_segment_quality_gates,
)
# Queue 151: these audio helpers are pure module-level code. Re-export them
# so existing dictation.<name> users retain their compatibility path.
from samsara.audio_tools import (
    _speech_rms_coverage, _suspected_silent_data_loss,
    _apply_retry_on_suspected_loss, _split_audio_at_silences, _fade_edges,
    _dump_hotkey_buffer, resample_audio, _HotkeyDecodeResult,
)
from samsara.wake_detector import WakeWordDetector
from samsara.handlers import _get_foreground_exe_lower, _get_foreground_hwnd
from samsara.runtime import thread_registry
from samsara.audio_engine.wake_dispatch import TranscriptionOwners, WakeDispatchQueue
from samsara.session_modes import (
    SessionMode, SessionModeManager, UtteranceSignals, CommandDispatchResult, InjectionDelivery,
    HandsFreeCommandMatch, PendingTextPolicy, normalize_utterance,
    GLOBAL_SESSION_EXIT_PHRASES, resolve_ava_invocations, is_scratch_that,
    # Queue 106: the control-phrase exemption in _is_dictate_context_echo
    # checks the SAME matchers dispatch_utterance does, so a refused echo can
    # never be a phrase the session would have acted on.
    is_dictate_commit, is_recover_draft, match_switch_word,
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

# Ava command session (D3) tap-toggle debounce floor. Left-Alt is a
# TAP-to-toggle control (one clean press-release latches the session on;
# another latches it off) -- NOT a hold control like Right-Alt Ava, whose
# 200ms command_mode.enter_debounce_ms guard exists to reject an
# ACCIDENTAL brief tap on a control that's supposed to be HELD. Applying
# that same 200ms floor here (2026-07-23 G3 live-test finding) silently
# ate legitimate fast taps -- an 80ms or 150ms press-release never
# toggled the session at all. This floor exists only to reject genuine
# keyboard-hardware contact bounce / phantom double-fire, not human tap
# speed, so it stays far below normal reaction time.
_AVA_CMD_TAP_DEBOUNCE_MS = 40

# Hands-free capture-window ducking (2026-07-24): delay between a capture
# window closing (transcription complete or buffer discarded) and the
# deep duck actually restoring back to idle level. Serves two purposes
# with one timer -- a minimum settle "tail" after capture ends, AND the
# debounce: a new capture window opening before this fires cancels the
# pending restore outright, so rapid consecutive utterances reuse the
# still-active duck instead of a stop-then-immediately-restart flicker.
_HANDS_FREE_CAPTURE_DUCK_RESTORE_DELAY_S = 0.5

# Adaptive wake-gate freeze settle window (2026-07-24): how long AFTER a
# duck transition ends (or TTS stops speaking) the noise-floor EMA stays
# frozen before resuming normal adaptation. Matches the capture-duck
# restore delay so the floor doesn't start chasing again until the audio
# has actually settled at its new (post-restore) level.
_WAKE_GATE_FREEZE_SETTLE_S = 0.5

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
_GATE_MAX_BUFFER_S   = 8.0   # short-capture fast path AND the VAD chunk length of the gate.
                             # Captures up to this long are scanned in ONE VAD call (unchanged).
                             # Longer captures skip VAD when overall RMS is at or above
                             # _SANITY_RMS_FLOOR_DB (audible dictation stays off the VAD lock);
                             # below it the WHOLE capture is scanned in chunks of this length,
                             # taking the VAD lock once per chunk (39, 2026-09-13: this used to
                             # scan only the first 8 s, so quiet toggle takes whose speech
                             # started later were discarded -- 26.5 s and 69.8 s takes).
                             # Raised 3.0->8.0: 3-6s near-silent/whisper holds were bypassing
                             # the gate and producing phantom "Thank you for watching" text.
                             # NOTE (2026-07-10): that fix only pushed the exposure window out,
                             # it didn't close it -- an 11.7s hold reproduced the identical bug.
                             # Both the quiet-prefix check and the language-confidence
                             # gate now cover the long silent-hold exposure window.
_GATE_MIN_CONTIG_MS  = 150   # minimum CONTIGUOUS high-confidence speech run required to pass
_GATE_VAD_PROB       = CONTIGUOUS_VAD_PROB_THRESHOLD  # Silero speech-probability threshold for the contiguous-run gate
_FADE_MS             = 50    # linear fade-in/out applied to hotkey buffers, kills the
                             # press/release click transient before it can reach VAD or Whisper
_GATE_HEAD_GRACE_CLICK_PAD_MS = 60

#: One contiguous-speech scan: the longest run, where it starts, how much was scanned.
_SpeechRun = collections.namedtuple(
    '_SpeechRun', ['passed', 'best_ms', 'offset_s', 'scanned_s', 'chunks', 'method', 'failed_open', 'buffer_s'],
)


class _GateDecision:
    """The presence gate's verdict for one capture (truthy = SKIP decoding).

    describe() is the log line, with the numbers that make the decision
    checkable: capture length, the path taken, the longest speech run and the
    offset where it starts, how much audio was scanned (and in how many
    chunks), the RMS for long captures, and the threshold applied."""

    __slots__ = ('skip', 'path', 'buffer_s', 'rms_db', 'head_grace_ms', 'best_ms', 'offset_s',
                 'scanned_s', 'chunks', 'method', 'failed_open')

    def __init__(self, *, skip, path, buffer_s, rms_db=None, head_grace_ms=0.0, best_ms=0,
                 offset_s=0.0, scanned_s=0.0, chunks=0, method='vad', failed_open=False):
        self.skip = bool(skip)
        self.path = path
        self.buffer_s = float(buffer_s)
        self.rms_db = rms_db
        self.head_grace_ms = float(head_grace_ms or 0.0)
        self.best_ms = int(best_ms)
        self.offset_s = float(offset_s)
        self.scanned_s = float(scanned_s)
        self.chunks = int(chunks)
        self.method = method
        self.failed_open = bool(failed_open)

    def __bool__(self):
        return self.skip

    def describe(self) -> str:
        floor = f"{_SANITY_RMS_FLOOR_DB:.0f} dBFS"
        if self.path == 'loud':
            return (f"[GATE] pass: buffer {self.buffer_s:.2f}s rms {self.rms_db:.1f} dBFS >= {floor} floor "
                    f"-- audible long capture, decoded without VAD")
        verdict = "skip" if self.skip else "pass"
        relation = "<" if self.skip else ">="
        if self.best_ms > 0:
            where = (f"longest speech run {self.best_ms}ms at {self.offset_s:.2f}s {relation} "
                     f"{_GATE_MIN_CONTIG_MS}ms threshold")
        else:
            where = f"no speech frames found {relation} {_GATE_MIN_CONTIG_MS}ms threshold"
        scope = (f"buffer {self.buffer_s:.2f}s, scanned {self.scanned_s:.2f}s "
                 f"in {self.chunks} chunk{'s' if self.chunks != 1 else ''}, path {self.path}")
        if self.rms_db is not None:
            scope += f", rms {self.rms_db:.1f} dBFS < {floor} floor"
        extras = []
        if self.method != 'vad':
            extras.append(f"{self.method} fallback")
        if self.failed_open:
            extras.append("failed open")
        if self.head_grace_ms > 0:
            extras.append(f"head_grace={self.head_grace_ms:.0f}ms")
        tail = f", {', '.join(extras)}" if extras else ""
        suffix = " -- no contiguous speech anywhere in the capture, skipping" if self.skip else ""
        return f"[GATE] {verdict}: {where} ({scope}{tail}){suffix}"
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

_LONG_DECODE_CEILING_S = 180.0
                             # Resource guard, NOT a quality boundary. Whisper
                             # decodes real dictation cleanly well past 30s in
                             # a single model.transcribe() call -- the
                             # silence-splitter that used to run above 30s
                             # (_split_audio_at_silences, commit 21ee3f0) was
                             # stripping acoustic/semantic context from each
                             # chunk, degrading avg_logprob/compression_ratio
                             # versus the same audio decoded whole, and the
                             # quality gates below then rejected the degraded
                             # fragments -- verified empirically on 2026-07-15
                             # saved hotkey buffers: a 55s capture split into
                             # 24.9s+24.9s+5.2s chunks produced only 198 chars,
                             # while the SAME saved audio decoded whole in one
                             # call returned the complete transcript (a 96s
                             # capture likewise decoded completely in one
                             # call). The splitter's original justification
                             # is also gone: it existed to avoid setting
                             # condition_on_previous_text=True over long
                             # stitched sequences (Whisper's repetition-loop
                             # bug), but _build_hotkey_transcribe_params now
                             # forces condition_on_previous_text=False
                             # unconditionally on every hotkey decode, long or
                             # short -- that flag, not splitting, is what
                             # prevents the loop today. Only a recording past
                             # THIS ceiling still uses
                             # _split_audio_at_silences, purely to bound
                             # memory/latency on an outlier-length buffer; see
                             # _apply_segment_quality_gates for how segments
                             # from either decode path are gated identically.

# --- Silent data-loss sanity check (2026-07-16 incident) ---
# A 35.3s hold-to-record hotkey dictation delivered only 80 chars -- the
# START of the utterance stitched directly onto its END, ~25s of genuine
# continuous mid-recording speech gone. Root-caused to faster-whisper
# itself: on the FIRST 30s decode window (the only window that receives
# initial_prompt as decoder context under condition_on_previous_text=
# False -- see _build_hotkey_transcribe_params), the model can terminate
# generation far short of the window's actual content while still
# reporting a segment nominally spanning the whole window, with signals
# (avg_logprob, compression_ratio) that look individually unremarkable --
# invisible to _is_quality_exhausted, which only sees the few tokens that
# WERE generated. Reproduced deterministically against the live 540-char
# command-vocabulary initial_prompt on the incident WAV, and found in ~37%
# of recent >30s hotkey captures (~/.samsara/debug) when re-decoded with
# the same prompt -- but decode-parameter sweeps (chunk_length, beam_size,
# no_speech_threshold, vad_filter -- vad_filter=True does avoid it here,
# but that param is locked False, see tests/test_transcription_params.py)
# showed the failure itself is NOT reliably deterministic run-to-run
# (almost certainly CUDA/float16 numeric nondeterminism tipping a
# borderline decode), so no single decode-param change can be proven to
# eliminate it. This is therefore a fail-loud backstop, not a cure: catch
# "long recording, implausibly little text" after the fact and surface it
# loudly instead of silently delivering truncated text as if it were the
# whole utterance.
_SANITY_MIN_DURATION_S = 15.0
                             # Below this, natural pauses/short utterances
                             # make chars/sec too noisy a signal on its own
                             # (a genuinely short, unhurried sentence can
                             # legitimately read low) -- only worth checking
                             # once there's enough audio for the ratio to
                             # mean something.
_SANITY_MIN_CPS = 3.0
                             # Chars/sec floor below which a long decode looks
                             # suspicious. Calibrated against real captures
                             # (~/.samsara/debug, 2026-07-15/16): genuine slow/
                             # deliberate dictation with COMPLETE sentences
                             # measured 3.4-9.8 cps; confirmed truncated
                             # decodes (this incident and others found via the
                             # same audit) measured 0.16-2.96 cps. Set below
                             # the observed complete-speech floor so normal
                             # unhurried dictation never trips this.
_SANITY_MIN_SPEECH_COVERAGE = 0.5
                             # Corroboration required before flagging: at least
                             # half the buffer must read as vocal-energy-
                             # present. A genuinely quiet/mostly-silent long
                             # hold producing little text is correctly quiet,
                             # not a decode failure -- this is what tells the
                             # two cases apart.
_SANITY_RMS_WINDOW_S = 0.5
_SANITY_RMS_FLOOR_DB = -40.0
                             # dBFS noise-floor cutoff for "this window has
                             # vocal energy". Coarse and VAD-free by design --
                             # this only corroborates a WARNING-level heuristic,
                             # not a hard gate, so a cheap RMS scan is
                             # preferable to taking the VAD model's lock on
                             # every long hotkey decode.


# X buttons the Win32 mouse hook (samsara/mouse_hook.py) reports. Either may be
# bound to command_mode.button or to the main record hotkey (config['hotkey']).
_MOUSE_HOTKEY_BUTTONS = ('mouse4', 'mouse5')
# The keyboard hotkeys other than the main one, with on_key_press's defaults:
# while any of them is physically held it legitimately owns hotkey_pressed.
_OTHER_HOTKEY_KEYS = (
    ('continuous_hotkey', 'ctrl+alt+d'), ('wake_word_hotkey', 'ctrl+alt+w'),
    ('command_hotkey', 'ctrl+alt+c'), ('memo_hotkey', 'ctrl+alt+m'),
    ('undo_hotkey', 'ctrl+alt+z'), ('correction_hotkey', 'ctrl+alt+r'),
    ('cancel_hotkey', 'escape'),
)


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


class _SafeRotatingFileHandler(_RotatingFileHandler):
    """RotatingFileHandler hardened against a Windows rollover failure that
    silently freezes logging forever (2026-07-20 incident: samsara.log
    stuck at exactly 5,242,880 bytes -- maxBytes -- for days with zero
    output; samsara.log.3 existed but .1/.2 did not, meaning a rollover
    got partway through its rename chain and then died).

    Root cause: stock doRollover() closes self.stream FIRST, then renames
    .2->.3, .1->.2, and finally current->.1 in that order, only reopening
    self.stream (`self.stream = self._open()`) after ALL renames succeed.
    If any rename in that chain raises -- e.g. PermissionError because a
    second process (another Samsara instance, a diagnostic tool, AV
    scanning a backup file) holds an open handle on one of the backup
    files without FILE_SHARE_DELETE, which plain Python open() does not
    request on Windows -- the exception propagates out of doRollover().
    RotatingFileHandler.emit() catches it via handleError(), which is a
    silent no-op whenever sys.stderr is None (true for this app's
    windowless packaged EXE, see the console_handler fallback just
    below). self.stream is left None; the CURRENT oversized log file was
    NEVER renamed away (that step -- self.rotate(baseFilename, dfn) --
    never runs, since the exception hits earlier in the chain) or
    reopened. Every subsequent emit() reopens that SAME still-oversized
    file (shouldRollover's own `if self.stream is None: self.stream =
    self._open()`), sees it is still >= maxBytes, retries the identical
    doomed rollover, and fails again -- forever, in total silence.

    Fix: never let a rollover failure leave the handler unable to write.
    If the normal rename chain raises, give up on preserving this
    rotation's history and truncate the current file in place instead --
    logging keeps working, which is what actually matters; losing one
    rotation cycle's backups is an acceptable trade for never silently
    losing ALL future logging.
    """

    def doRollover(self):
        try:
            super().doRollover()
        except Exception as exc:
            self._truncate_and_continue(exc)

    def _truncate_and_continue(self, exc: Exception) -> None:
        try:
            print(f"[LOG] Rollover failed ({exc!r}) -- truncating "
                  f"{self.baseFilename} and continuing rather than freezing.",
                  file=sys.stderr)
        except Exception:
            pass
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
        except Exception:
            pass
        try:
            # 'w' (truncate), not 'a': the file is at/over maxBytes and the
            # normal rename chain couldn't move it out of the way, so
            # truncating in place is the only way left to guarantee writes
            # keep landing on disk instead of piling onto an oversized file
            # or, worse, going nowhere.
            self.stream = open(self.baseFilename, mode='w', encoding=self.encoding)
        except Exception as reopen_exc:
            # Truly cannot open the log file (e.g. directory gone, disk
            # full) -- nothing more this handler can do. self.stream stays
            # None; shouldRollover's own reopen-on-None will keep retrying
            # on the next emit(), same as stock behavior for a hard
            # filesystem failure this class was never meant to paper over.
            try:
                print(f"[LOG] Could not reopen {self.baseFilename} after "
                      f"rollover failure: {reopen_exc!r}", file=sys.stderr)
            except Exception:
                pass


# File handler — DEBUG level, rotating 5 MB × 3 backups, UTF-8
file_handler = _SafeRotatingFileHandler(
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


def _verify_logging_self_check() -> bool:
    """Boot-time self-check (2026-07-20 incident): write a marker line
    right after logging init and confirm samsara.log's size/mtime
    actually moved. The failure mode that froze logging for days (see
    _SafeRotatingFileHandler's docstring) left every write silently
    dropped -- this catches that class of failure at boot, in seconds,
    instead of discovering it days later when the log is needed and
    empty. Best-effort: any exception here is itself reported (never
    raised) and treated as a failed check rather than crashing startup
    over a diagnostic.
    """
    try:
        before = LOG_FILE.stat() if LOG_FILE.exists() else None
        before_size = before.st_size if before else -1
        before_mtime = before.st_mtime if before else -1
        logger.info(f"[BOOT-DIAG] logging self-check marker {time.time()}")
        file_handler.flush()
        after = LOG_FILE.stat()
        moved = after.st_size != before_size or after.st_mtime != before_mtime
        if not moved:
            print(f"[LOG] WARNING: logging self-check found no size/mtime "
                  f"change on {LOG_FILE} after a marker write -- logging "
                  f"may be silently stuck.", file=sys.stderr)
        return moved
    except Exception as exc:
        try:
            print(f"[LOG] Logging self-check failed: {exc!r}", file=sys.stderr)
        except Exception:
            pass
        return False


# Read by DictationApp.__init__ (self._logging_self_check_failed) and
# surfaced as a visible tray warning once the app is fully up -- see
# samsara/ui/tray_qt.py's _poll_startup_health.
LOGGING_SELF_CHECK_OK = _verify_logging_self_check()

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
_SPEECH_FLOOR_RATIO = ADAPTIVE_SPEECH_FLOOR_RATIO

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


def _cancel_window_module():
    """samsara.execution_policy when the queue-69 cancel window hooks can run
    (never raises; None when unavailable)."""
    try:
        from samsara import execution_policy
        return execution_policy
    except Exception:
        return None


def _config_phrase_list(value) -> list:
    """A config phrase list that may be written as one string or a list."""
    if isinstance(value, str):
        return [value]
    return [str(p) for p in (value or []) if isinstance(p, str)]


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
    # Config-backup safeguard (2026-07-2x "config wiped to defaults"
    # incident). See save_config()'s rolling-backup step,
    # _write_last_known_good(), and _quarantine_corrupt_config().
    _CONFIG_BACKUP_DIRNAME = "config_backups"
    _CONFIG_BACKUP_KEEP = 15
    _LAST_KNOWN_GOOD_FILENAME = "last_known_good.json"

    def __init__(self, splash=None):
        self.splash = splash
        # See _verify_logging_self_check() / _SafeRotatingFileHandler above:
        # surfaced as a tray warning once startup finishes (tray_qt.py's
        # _poll_startup_health), not blocked on here -- a logging problem
        # must never prevent the app from starting.
        self._logging_self_check_failed = not LOGGING_SELF_CHECK_OK
        # Config-backup safeguard: set True only when load_config() could
        # not read an existing config file this session (see
        # _quarantine_corrupt_config()) -- latches save_config() off for
        # the rest of the session so in-memory defaults can never
        # overwrite the preserved original. Also surfaced as a tray
        # warning (tray_qt.py's _poll_startup_health), same pattern as
        # _logging_self_check_failed above.
        self._config_load_failed = False
        self._config_corrupt_backup_name = None
        # Startup work is split between this thread and the asynchronous model
        # loader.  Keep splash progress monotonic when those two lanes report
        # at nearly the same time.
        self._splash_progress = 0
        self._splash_progress_lock = threading.Lock()
        self._startup_shell_ready = threading.Event()
        # Source and frozen launches share one per-user profile. Tests,
        # first-run previews, and other isolated launches use the explicit
        # SAMSARA_HOME_DIR override instead of a second implicit config.
        self.config_path = samsara_config_path()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # Boot-phase timing, one "last mark" per thread (see _BootStageTimer).
        _boot = _samsara_boot._BootStageTimer()
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
        self.update_splash(
            "Loading configuration...", 8,
            "Reading preferences and accessibility settings",
        )
        with self._config_lock:
            self.load_config()
        _boot("config load")
        _bdiag("config load")

        self.update_splash(
            "Setting up audio...", 18,
            "Opening the configured microphone and audio pipeline",
        )

        # Set the Samsara lotus-wheel artwork as the default icon for all
        # Qt windows (assets/icon/samsara.ico -- a multi-resolution .ico,
        # Qt/Windows picks whichever embedded size fits).
        #
        # QIcon is a GUI object that must be constructed on the Qt thread;
        # this __init__ runs on a different thread. Building Qt image
        # objects here used to intermittently deadlock boot (observed ~67%
        # of cold boots hanging at exactly this point, before audio device
        # enumeration) -- the fix, preserved here, is that only the (non-Qt)
        # file path is resolved on this thread, and QIcon construction plus
        # setWindowIcon move onto the Qt thread via qt_runtime.post(),
        # fire-and-forget so boot never blocks on it.
        try:
            _icon_base = (
                sys._MEIPASS
                if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')
                else os.path.dirname(os.path.abspath(__file__))
            )
            _icon_ico_path = os.path.join(_icon_base, 'assets', 'icon', 'samsara.ico')

            def _apply_window_icon():
                try:
                    from PySide6.QtGui import QIcon
                    from PySide6.QtWidgets import QApplication
                    if os.path.exists(_icon_ico_path):
                        QApplication.instance().setWindowIcon(QIcon(_icon_ico_path))
                    else:
                        logger.warning(f"[ICON] Icon file not found: {_icon_ico_path}")
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

        # Auto-calibrate speech threshold on startup. A calibration of the
        # same device from the last 24 h is reused instead of recording 1.5 s
        # on the boot thread; it is then refreshed in the background once
        # startup completes (see load_model_async).
        _t = time.perf_counter()
        _cal_result = self._run_calibration_if_auto(use_cache=True)
        self._calibration_refresh_due = (_cal_result == 'cached')
        _dt = (time.perf_counter() - _t) * 1000
        _boot("mic calibration")
        logger.info(f"[BOOT-DIAG] mic calibration ({_cal_result}): {_dt:.0f}ms")
        if _dt > 5000:
            logger.info(f"[BOOT-DIAG] SLOW STEP: mic calibration {_dt:.0f}ms")

        self.recording = False
        self.command_mode_recording = False  # True when using command-only hotkey
        self._stop_in_flight = False         # True while stop_recording + trailing sleep is pending
        self._hold_capture_lifecycle_lock = threading.RLock()
        self._hold_capture_duck_seq = 0
        self._hold_capture_duck_token = None
        self._hold_capture_duck_confirmed_at = None

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
        self._language_confidence_gate = _languages.LanguageConfidenceGate()
        
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
        self._session_transition_lock = threading.Lock()
        self._command_mode_miss_count = 0
        self._command_mode_inactivity_timer = None
        self._command_mode_timer_lock = threading.Lock()
        # Queue 50 (ARC audit5b): bumped on EVERY reset and cancel. A timer
        # callback carries the generation it was armed with and does nothing
        # unless that is still current -- Timer.cancel() cannot stop a
        # callback that has already started running.
        self._timer_generation = 0
        # Monotonic deadline the current inactivity timer will fire at --
        # threading.Timer has no query-remaining-time API, so this is
        # tracked alongside it (set/cleared in lockstep, see
        # _reset_command_mode_inactivity_timer / _cancel_command_mode_
        # inactivity_timer_locked) purely so a hold-suspend pause can
        # compute how much time was actually left. None whenever no timer
        # is running.
        self._command_mode_inactivity_deadline = None
        # Remaining seconds captured by _pause_command_mode_inactivity_for_
        # hold, consumed by _resume_command_mode_inactivity_after_hold.
        # None when not currently paused for a hold.
        self._command_mode_inactivity_remaining_on_hold = None
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

        # Ava command session (D3, Ava Front Door spec v2) -- Left-Alt
        # latched, command-first waterfall session. Replaces the old
        # ai_command_mode.py/"AI command mode" (deleted).
        self.ava_command_session_active = False
        self._ava_cmd_mode_lock = threading.Lock()
        self._ava_cmd_key_held = False            # edge-trigger guard vs OS key auto-repeat
        self._ava_cmd_key_press_time = 0.0        # set on press; toggle-on-release ghost-tap check reads this
        self._ava_cmd_ready = threading.Event()
        self._ava_cmd_ready.set()  # starts set; cleared during entry until cue finishes
        self._ava_cmd_miss_count = 0              # consecutive unresolved utterances (see ava_command_session._process_utterance)
        self._ava_cmd_generation = 0              # bumped on every enter AND exit; staleness guard for async work in flight across an exit (see enter/exit_ava_command_session)
        self._ava_cmd_inactivity_timer = None     # spec D3: 60s inactivity exit (config ava_command_session.inactivity_timeout_s)
        self._ava_cmd_timer_lock = threading.Lock()

        self._mouse_hook = None
        # Which input drove the current main-hotkey press: 'key' (combo) or
        # 'mouse' (config['hotkey'] is mouse4/mouse5). on_key_release must
        # never stop a mouse-held recording.
        self._main_hotkey_source = 'key'
        # Physical-press state for the main hotkey, one flag per source (28,
        # 2026-09-13). hotkey_pressed used to carry both "a key is down" and
        # "the mouse button is down"; a lost release on one path then muted
        # the other for good. _hold_down_key: the keyboard combo is down and
        # owns an in-progress main-hotkey action. _hold_down_mouse: the bound
        # mouse button is down (edge trigger). Toggle state is toggle_active
        # alone and never sits behind either flag.
        self._hold_down_key = False
        self._hold_down_mouse = False

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
        self._memo_recording = False
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
        self._transcription_owners = TranscriptionOwners()
        self._wake_dispatch_queue = WakeDispatchQueue(self.config)
        self._wake_session_lock = threading.RLock()
        self._wake_session_inactivity_timer = None
        self._wake_session_expires_at = None
        self._wake_session_timer_token = None
        self._wake_word_settings_lock = threading.Lock()
        self._wake_word_settings_guard = threading.Lock()
        self._wake_word_settings_generation = 0

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
        self._wake_profile_fallback_warned: set = set()

        # Lazy wake-model load (boot fix 1): OpenWakeWord (+ the sklearn import
        # it drags in) is loaded only when wake listening is first requested,
        # on its own thread. _wake_models_state is 'off' -> 'loading' ->
        # 'ready'; wake_ready is set once the detectors exist. A start request
        # made while loading is remembered in _wake_start_pending and carried
        # out by the loader. See _request_wake_models / wake_ready_state.
        self._wake_models_lock = threading.Lock()
        self._wake_models_state = 'off'
        self._wake_start_pending = False
        self.wake_ready = threading.Event()

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
            self.listening_indicator.set_idle_animation(self._idle_animation_enabled())
            self.listening_indicator.set_wake_armed(bool(self.wake_word_active))
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
        # Constructed on a worker once the tray/main window are scheduled
        # (boot fix 3: `import edge_tts` + engine ctor cost 1.6 s cold on this
        # thread). Until then both stay None -- every speaker already guards
        # on that -- and tts_ready is clear.
        self.tts_engine = None
        self.audio_coordinator = None
        self.tts_ready = threading.Event()
        if self.config.get('tts', {}).get('enabled', False):
            thread_registry.spawn("dictation.tts_init", self._tts_init_worker, daemon=True)
        else:
            self.tts_ready.set()
            # Ava Front Door spec v2 "Migration" notice (toast only: no TTS).
            # With TTS enabled the worker announces once the engine exists.
            self._maybe_announce_ava_command_session_migration()
        _boot("TTS engine init (deferred)")
        _bdiag("TTS engine init (deferred)")

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

        # Hands-free audio ducking (2026-07-24) -- see the "ducking"
        # default_config block's own comment for the two-stage design.
        # Two INDEPENDENT SessionDucker instances, layered LIFO (idle
        # starts first / stops last): _hands_free_idle_ducker lives for
        # the wake-word toggle's whole duration; _hands_free_capture_
        # ducker lives per capture-window (speech onset through
        # transcription-complete/discard + a short debounced tail).
        # Neither is ever driven by, or drives, any session-end/exit path.
        self._hands_free_idle_ducker = None
        self._hands_free_capture_ducker = None
        self._hands_free_duck_lock = threading.Lock()
        self._hands_free_duck_restore_timer = None
        self._hands_free_capture_duck_restore_generation = 0
        self._hands_free_capture_duck_restore_token = object()
        self._hands_free_capture_duck_owners: set[int] = set()
        self._hands_free_capture_duck_owner_seq = 0
        self._hands_free_capture_duck_start_generation = 0
        self._hands_free_capture_duck_starting = False
        # Adaptive wake-gate freeze (2026-07-24): monotonic deadline until
        # which _wake_audio_is_below_gate must NOT let a sample update
        # self._wake_noise_floor -- bumped by every duck transition (idle
        # or capture, start or stop) so the EMA floor doesn't chase a
        # duck-induced volume step and then misread the restore step back
        # up as speech onset. See _bump_wake_gate_freeze/_wake_gate_frozen.
        self._wake_gate_freeze_until = 0.0

        self.update_splash(
            "Setting up keyboard...", 35,
            "Installing hotkeys and listening controls",
        )

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

        # ACE engine — always started for hold-mode dictation (ACE-03).
        # Started BEFORE load_model_async() (boot fix 2): the model thread's
        # cold CUDA/ctranslate2 DLL loads starved this thread's resample-filter
        # design for 12-13 s on cold boots, holding back the tray and main
        # window (perf_artifacts/boot_profile.md section 3). The filter itself
        # is now cached on disk too (audio_engine.engine._get_polyphase_filter).
        # DictationSessionConsumer replaces the bespoke prebuffer + audio_callback path.
        # DebugRecorder is optional: set config["ace_debug_capture"] = true to enable.
        self._ace_engine           = None
        self._ace_debug_rec        = None
        self._dictation_consumer   = None
        self._continuous_consumer  = None
        self._wake_consumer        = None    # ACE-04C
        # WakeConsumer serves two independent masters -- wake-phrase
        # DETECTION (start_wake_word_mode/stop_wake_word_mode) and
        # toggle-session utterance dispatch (enter_command_mode/
        # exit_command_mode's toggle branch) -- see _ensure_wake_consumer/
        # _release_wake_consumer just above start_wake_word_mode. Reason-
        # counted like _icon_anim_reasons/_request_icon_chase: a boolean
        # would let one master's stop() kill the pipeline out from under
        # the other still using it.
        self._wake_consumer_reasons = set()
        self._wake_consumer_lock    = threading.Lock()
        # Queue 109: the last fatal that stopped hands-free listening
        # (audio_engine.wake_consumer.HandsFreeFault), or None when listening
        # is healthy. PERSISTENT -- cleared only by a successful restart, so
        # Home can still explain a stop the user walked away from. The
        # counter is the automatic-restart budget, reset by a user restart.
        self._hands_free_fault          = None
        self._hands_free_fault_attempts = 0
        self._ace_dictation_active  = False   # True while hold-mode uses ACE consumer path
        self._ace_streaming_active  = False   # True while CapsLock streaming uses ACE consumer
        self._streaming_session     = None    # Sole owner until final/cancel cleanup completes
        self._dictate_preview       = None    # DictatePreviewSession, DICTATE-lane only -- see _ensure_streaming_preview
        _t = time.perf_counter()
        self._start_ace_engine()
        _dt = (time.perf_counter() - _t) * 1000
        if self.config.get('ace_debug_capture', False) and self._ace_engine is not None:
            self._start_ace_debug_rec()
        _boot("ACE audio engine start")
        _bdiag("ACE audio engine start")
        logger.info(f"[BOOT-DIAG] _start_ace_engine (total): {_dt:.0f}ms")
        if _dt > 5000:
            logger.info(f"[BOOT-DIAG] SLOW STEP: _start_ace_engine {_dt:.0f}ms")

        _model_folder = f"models--Systran--faster-whisper-{_model_size}"
        _model_cache = os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface", "hub", _model_folder
        )
        if os.path.exists(_model_cache):
            self.update_splash(
                "Loading speech model...", 50,
                "Preparing local speech recognition",
            )
        else:
            self.update_splash(
                "Downloading speech model...", 50,
                "First download only; this may take a few minutes",
            )

        # Load model in background
        self.load_model_async()
        _boot("model load kicked off (async)")
        _bdiag("model load kicked off (async)")

        self.update_splash(
            "Audio capture ready...", 60,
            "Preparing the interface while speech recognition loads",
        )

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

        # NOTE: Splash is intentionally NOT closed here.  The model worker also
        # waits for create_tray_icon() to schedule the tray and main window, so
        # neither startup lane can report completion before the other is ready.
        self.update_splash(
            "Preparing the interface...", 65,
            "Building the Samsara controls and tray menu",
        )

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

    def _tts_init_worker(self) -> None:
        """Build the TTS engine + AudioCoordinator off the boot thread, after
        create_tray_icon() has scheduled the tray and main window. Publishes
        the coordinator last so a speaker never sees a coordinator without
        its engine, then runs the startup announcements that need a voice."""
        shell_ready = getattr(self, "_startup_shell_ready", None)
        if shell_ready is not None:
            shell_ready.wait()
        _t = time.perf_counter()
        try:
            from samsara.tts import WinRTEngine, EdgeTTSEngine, AudioCoordinator
            tts_engine_name = self.config.get('tts', {}).get('engine', 'winrt').lower()
            if tts_engine_name == 'edge':
                engine = EdgeTTSEngine(output_device=self.output_device)
                logger.info("[TTS] Initialized EdgeTTS engine (Azure Neural voices)")
            else:
                engine = WinRTEngine(output_device=self.output_device)
                logger.info("[TTS] Initialized WinRT engine")
            coordinator = AudioCoordinator(
                self,
                engine=engine,
                config=self.config.get('audio_coordinator', {}),
            )
            self.tts_engine = engine
            self.audio_coordinator = coordinator
            logger.info(f"[TTS] AudioCoordinator ready (tts_ready in "
                        f"{(time.perf_counter() - _t) * 1000:.0f}ms, off the boot thread)")
        except Exception as e:
            logger.exception(f"[TTS] Failed to initialize: {e}")
            self.tts_engine = None
            self.audio_coordinator = None
        finally:
            self.tts_ready.set()
        # Ava Front Door spec v2 "Migration": one-time first-run-after-update
        # notice for the ai_command_mode -> ava_command_session consolidation
        # (toast + one spoken line). Deliberately no legacy-module compatibility
        # switch (spec ruling: "keeping the deleted brain alive defeats the
        # consolidation and doubles the test surface") -- this notice is the
        # entire migration UX.
        try:
            self._maybe_announce_ava_command_session_migration()
        except Exception as e:
            logger.debug(f"[TTS] Startup announcement failed: {e}")

    def update_splash(self, status, progress=None, detail=None, *, error=False):
        """Update startup state without allowing concurrent phases to regress.

        ``set_status`` remains the only required splash API.  Progress, detail,
        and error setters are used when the richer splash implementation
        provides them, keeping lightweight test/legacy splash objects working.
        """
        splash = getattr(self, "splash", None)
        if splash is None:
            return

        lock = getattr(self, "_splash_progress_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._splash_progress_lock = lock

        with lock:
            current = getattr(self, "_splash_progress", 0)
            if progress is not None:
                progress = max(0, min(100, int(progress)))
                # A slower startup lane may report after a faster one.  Ignore
                # its entire stale phase so the visible status stays truthful.
                if progress < current and not error:
                    return
                self._splash_progress = max(current, progress)

            try:
                splash.set_status(status)
                if progress is not None and hasattr(splash, "set_progress"):
                    splash.set_progress(self._splash_progress)
                if detail is not None and hasattr(splash, "set_detail"):
                    splash.set_detail(detail)
                if error and hasattr(splash, "set_error"):
                    splash.set_error(status, detail)
            except Exception as e:
                logger.debug(f"Splash status update failed: {e}")

    def _close_splash_post_load(self):
        """Close the splash screen after every startup lane has finished.
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
            # FrameBus/AudioCaptureEngine/*Consumer classes are imported at
            # module top-level now (see [BOOT-DIAG] audio_engine import block
            # near the sounddevice import) instead of lazily here -- None
            # means that import failed, so degrade the same way the old
            # in-function try/except did.
            if AudioCaptureEngine is None or FrameBus is None:
                raise RuntimeError(
                    "samsara.audio_engine failed to import at module load "
                    "(see earlier [BOOT-DIAG] samsara.audio_engine import FAILED line)"
                )

            ring = FrameBus()
            # Pass the app's detected capture rate so the ACE engine opens at
            # the same sample rate as the wake word and prebuffer streams.
            # Both run on the same WASAPI device; mismatched rates cause one
            # stream to stop receiving callbacks (WASAPI dual-client starvation).
            engine_config = dict(self.config)
            engine_config['_capture_rate'] = self.capture_rate
            _t_ctor = time.perf_counter()
            self._ace_engine = AudioCaptureEngine(
                ring, config=engine_config,
                on_stream_death=self._on_ace_stream_death,
                on_recovery_success=self._on_ace_recovery_success,
                on_give_up=self._on_ace_recovery_give_up,
            )
            _dt_ctor = (time.perf_counter() - _t_ctor) * 1000
            logger.info(f"[BOOT-DIAG] AudioCaptureEngine() construction: {_dt_ctor:.0f}ms")
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

            self._continuous_consumer = ContinuousConsumer(
                engine=self._ace_engine,
                app=self,
            )

            # on_fatal is not optional in production (queue 109, Astra F6):
            # without it a fatal in the poll loop ends hands-free listening
            # and tells the user with one error beep. For someone who cannot
            # type, that is their input method gone with no explanation and
            # no way back. The handler records a persistent fault Home reads
            # and, for a transient failure only, restarts by itself.
            self._wake_consumer = WakeConsumer(
                engine=self._ace_engine,
                app=self,
                on_fatal=self._on_wake_consumer_fatal,
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

        def _structural_error(config_obj):
            if not isinstance(config_obj, dict):
                return (
                    "top-level config must be a JSON object (dict), got "
                    f"{type(config_obj).__name__}"
                )
            required_containers = {
                "wake_word_config": dict,
                "wake_profiles": list,
                "command_mode": dict,
                "ducking": dict,
                "smart_corrections": dict,
                "wake_targets": list,
                "ai_command_mode": dict,
            }
            for key, expected_type in required_containers.items():
                if key in config_obj and not isinstance(config_obj[key], expected_type):
                    return (
                        f"top-level '{key}' must be {expected_type.__name__} "
                        f"when present, got {type(config_obj[key]).__name__}"
                    )
            return None

        default_config = {
            "hotkey": "ctrl+shift",
            "continuous_hotkey": "ctrl+alt+d",
            "wake_word_hotkey": "ctrl+alt+w",
            "command_hotkey": "ctrl+alt+c",
            "undo_hotkey": "ctrl+alt+z",
            "dictate_commit_hotkey": DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
            "correction_hotkey": "ctrl+alt+r",
            "cancel_hotkey": "escape",
            "memo_hotkey": "ctrl+alt+m",
            # Queue 92: memo audio is now kept BY DEFAULT. The flag was
            # written for the dictation path, where retention is clearly
            # wrong; for a memo the recording IS the feature -- "record
            # whatever I said" -- and a mis-transcribed name in a memo is
            # not reconstructable from the words. Nothing leaves the
            # machine, and the retention policy below bounds the cost.
            # Anyone who had set this False keeps False.
            "memo_retain_audio": True,
            # The retention policy (samsara.quick_memo.prune_audio): audio
            # older than this many days goes, then the oldest goes until the
            # total fits the size cap. 0 disables either limit. A memo the
            # user pinned in the memo list is never pruned, and a pruned
            # memo keeps its transcript -- only the WAV is removed.
            "memo_audio_retention_days": 90,
            "memo_audio_max_mb": 500,
            "memo_file": None,
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
            #
            # hands_free_* (2026-07-24) -- SEPARATE from the hotkey-path
            # enabled/level above: wires the SessionDucker engine
            # (samsara/audio_ducking.py, previously unwired) to hands-free
            # listening instead. Two-stage, both keyed to ACTIVE CAPTURE
            # windows and the wake-word toggle, never to any session-end
            # event (see _open_hands_free_capture_duck/_close_hands_free_
            # capture_duck and start_wake_word_mode/stop_wake_word_mode):
            #   hands_free_level: applied only DURING a capture window
            #     (speech onset through transcription complete/discard +
            #     a short debounced tail) -- the deep duck.
            #   hands_free_idle_level: applied for the WHOLE DURATION the
            #     wake-word toggle is on -- a PERSISTENT MILD DUCK, not an
            #     opt-in extra (2026-07-24 amendment; default 0.8, set to
            #     1.0 to disable). WHY this exists even though it's not
            #     tied to active capture: the echo canceller does not
            #     converge reliably (see samsara/echo_cancel.py), so the
            #     1500ms rolling pre-buffer (samsara.constants.
            #     PREBUFFER_SECONDS, captured BEFORE a capture-duck can
            #     possibly engage -- ducking starts only once speech onset
            #     is already detected) and the wake-word detector itself
            #     both hear media at whatever level was playing BEFORE
            #     onset. This idle duck is what actually gives the wake
            #     word and pre-buffer their SNR; lowering it trades idle
            #     media volume for wake-word/onset recognition accuracy
            #     until AEC is fixed. Composes via the engine's documented
            #     RELATIVE/MIN-style layering: a capture duck started
            #     while an idle duck is active restores back to
            #     idle_level (not full) when the capture window closes;
            #     only the idle duck's own stop (wake-word toggle OFF)
            #     restores to true full volume.
            "ducking": {
                "enabled": False,
                "level": 0.2,
                "hands_free_enabled": True,
                "hands_free_level": 0.15,
                "hands_free_idle_level": 0.8,
            },
            # Voice memo capture (2026-07-24): "voice memo" arms a one-shot
            # divert of the NEXT hold-to-dictate recording -- instead of
            # injecting text, the audio + transcript are saved into an
            # Obsidian vault (see samsara/voice_memo.py). vault_dir/
            # note_relpath/attachments_relpath are all relative-to-vault
            # except vault_dir itself, which is an absolute path.
            "voice_memo": {
                "vault_dir": "C:\\Users\\Morne\\Documents\\Obsidian Vault",
                "note_relpath": "Voice Memos.md",
                "attachments_relpath": "Attachments/Memos",
                "arm_timeout_s": 120,
                # Queue 92: the vault is a MIRROR, not the store of record.
                # Off by default because vault_dir does not exist for most
                # users and Samsara must not append to someone's notes
                # uninvited. Switch it on and every memo is also written
                # into the vault as an ![[embed]] plus transcript, which
                # Obsidian plays inline and syncs to a phone for free.
                "mirror_memos": False,
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
        # Config-backup safeguard: True only when a config file EXISTED but
        # neither it nor config.json.bak could be parsed -- distinct from
        # "no file at all" (true first run), which must still write
        # defaults. See _quarantine_corrupt_config() / save_config()'s
        # latch check just below where this drives the failure branch.
        _existing_file_unreadable = False
        logger.debug("[CONFIG] load_config: checking config_path existence")
        if self.config_path.exists():
            logger.debug("[CONFIG] load_config: opening config.json")
            try:
                with open(self.config_path, 'r') as f:
                    logger.debug("[CONFIG] load_config: reading JSON")
                    self.config = json.load(f)
                    logger.debug("[CONFIG] load_config: JSON loaded ok")
                _loaded_from_disk = True
                _error = _structural_error(self.config)
                if _error:
                    logger.error(f"[CONFIG] config.json has invalid structure: {_error}")
                    _loaded_from_disk = False
                    _existing_file_unreadable = True
            except json.JSONDecodeError as _je:
                bak_path = self.config_path.with_suffix('.json.bak')
                logger.exception(f"[CONFIG] config.json has invalid JSON: {_je}")
                if bak_path.exists():
                    try:
                        with open(bak_path, 'r') as f:
                            self.config = json.load(f)
                        _loaded_from_disk = True
                        logger.info("[CONFIG] Loaded from config.json.bak (backup)")
                        _error = _structural_error(self.config)
                        if _error:
                            logger.error(
                                "[CONFIG] config.json.bak has invalid structure: "
                                f"{_error}"
                            )
                            _loaded_from_disk = False
                            _existing_file_unreadable = True
                    except Exception as _bak_err:
                        logger.error(f"[CONFIG] Backup also invalid — using defaults: {_bak_err}")
                        _existing_file_unreadable = True
                else:
                    logger.warning("[CONFIG] No backup found — using defaults")
                    _existing_file_unreadable = True
            except Exception as _cfg_err:
                # config IO failure the user should be able to find in the log
                # even though load_config() itself must still fall through to
                # defaults here -- a broken/unreadable config.json must never
                # prevent the app from starting.
                logger.exception(f"[CONFIG] config.json read failed — using defaults: {_cfg_err}")
                _existing_file_unreadable = True

        if _loaded_from_disk:
            # Migration saves write self.config as-is, bypassing save_config's
            # three-way merge. The merge has no notion of "deleted in memory":
            # a key absent from memory but present on disk is taken from disk,
            # so every migration that pops a legacy key (wake_targets,
            # command_mode_enabled, ai_command_mode) had it restored by its own
            # save and re-ran on every boot (perf_artifacts/boot_profile.md
            # section 4). Safe here: self.config was read from that file a
            # moment ago under _config_lock, so there is no external edit to
            # preserve.
            self._config_migration_save = True
            try:
                # Migrate old flat wake word config to new nested structure
                logger.debug("[CONFIG] load_config: starting _migrate_wake_word_config")
                self._migrate_wake_word_config(default_config)
                logger.debug("[CONFIG] load_config: _migrate_wake_word_config done")

                self._migrate_command_matching_enabled_flag()
                self._migrate_ai_command_mode_config()
            finally:
                self._config_migration_save = False

            # Fill in any missing top-level keys
            for key in default_config:
                if key not in self.config:
                    self.config[key] = default_config[key]
        elif _existing_file_unreadable:
            # 2026-07-2x incident: a config file EXISTED but couldn't be
            # parsed, and this branch used to fall straight through to
            # `self.config = default_config; self.save_config()` --
            # silently overwriting the user's real (if corrupted) file
            # with fresh defaults. Never again: quarantine the original
            # (preserved, never deleted), run this session in memory on
            # defaults, and latch save_config() off so those defaults can
            # never reach disk over it. See _quarantine_corrupt_config()
            # and save_config()'s latch check.
            quarantine_path = self._quarantine_corrupt_config()
            self.config = default_config
            wake_profiles.validate_wake_profiles(self.config['wake_profiles'])
            self._config_load_failed = True
            self._config_corrupt_backup_name = (
                quarantine_path.name if quarantine_path is not None else None
            )
        else:
            # True first run: no config file existed at all -- the ONLY
            # case where writing defaults to disk is correct.
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

    def _quarantine_corrupt_config(self) -> "Path | None":
        """Rename an unreadable config.json aside as evidence, timestamped,
        NEVER deleted. Called only from load_config()'s failure branch, when
        a config file existed on disk but neither it nor config.json.bak
        could be parsed.

        Every OTHER per-user store in this app already has this exact
        safety net (samsara/paths.py's quarantine_corrupt_file(), used by
        phonetic_wash/wake_corrections/ava_corrections/voice_training since
        a 2026-07-09 correction-store loss: a parse failure fell back to
        empty in-memory state, and the next save wrote that empty state
        straight over the original file). config.json itself never had
        it -- the direct root-cause candidate for the 2026-07-2x "config
        wiped to defaults" incident this method exists to close. Uses its
        own naming (config.corrupt-YYYYMMDD-HHMMSS.json, not that helper's
        name.corrupt-timestamp scheme) to keep the .json extension intact
        for casual double-click inspection.

        Returns the quarantine path (used to build the UI warning message
        -- see save_config()'s latch and tray_qt.py's _poll_startup_health),
        or None if there was nothing to quarantine (config_path didn't
        actually exist) or the rename itself failed. Never raises.
        """
        if not self.config_path.exists():
            return None
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        quarantine_path = self.config_path.with_name(f"config.corrupt-{ts}.json")
        try:
            self.config_path.rename(quarantine_path)
            logger.error(
                f"[CONFIG] config.json could not be read -- quarantined to "
                f"{quarantine_path.name} (original bytes preserved, never "
                f"overwritten). Running this session on in-memory defaults; "
                f"saving is disabled until restart."
            )
            return quarantine_path
        except OSError as exc:
            logger.exception(f"[CONFIG] Could not quarantine unreadable config: {exc}")
            return None

    def _config_backups_dir(self) -> Path:
        """~/.samsara/config_backups/, created on first use."""
        d = samsara_home_dir() / self._CONFIG_BACKUP_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _rotate_config_backup(self) -> None:
        """Copy the CURRENT on-disk config.json into a timestamped, rolling
        backup under _config_backups_dir() before save_config()'s write
        replaces it, then prune to the newest _CONFIG_BACKUP_KEEP. Never
        raises -- a backup failure must not block the actual save (the
        3-way-merged write is still the more important thing to land).
        """
        try:
            backups_dir = self._config_backups_dir()
            ts = datetime.now().strftime('%Y%m%d-%H%M%S')
            dest = backups_dir / f"config-{ts}.json"
            # Rapid successive saves within the same second: don't clobber
            # the earlier snapshot, disambiguate instead.
            n = 1
            while dest.exists():
                dest = backups_dir / f"config-{ts}-{n}.json"
                n += 1
            shutil.copy2(self.config_path, dest)
            self._prune_config_backups()
        except OSError as exc:
            logger.warning(f"[CONFIG] Rolling backup failed (continuing save): {exc}")

    def _prune_config_backups(self) -> None:
        """Keep only the newest _CONFIG_BACKUP_KEEP rolling backups, oldest
        deleted first. Sorted by mtime, NOT filename -- the same-second
        collision disambiguator (config-<ts>-1.json, see
        _rotate_config_backup) sorts lexically BEFORE its own
        undisambiguated sibling (config-<ts>.json: "-" < "." in ASCII),
        which would silently invert chronological order for same-second
        rapid saves if sorted by name. last_known_good.json doesn't match
        the config-*.json glob and is never touched here."""
        backups_dir = self._config_backups_dir()
        files = sorted(backups_dir.glob("config-*.json"), key=lambda p: p.stat().st_mtime)
        excess = len(files) - self._CONFIG_BACKUP_KEEP
        for f in files[:max(0, excess)]:
            try:
                f.unlink()
            except OSError as exc:
                logger.debug(f"[CONFIG] Could not prune old backup {f.name}: {exc}")

    def _write_last_known_good(self) -> None:
        """Copy the current on-disk config.json to config_backups/
        last_known_good.json. Called ONLY once boot reaches "startup
        complete" (see load_model_async's load() closure, after every
        step that could raise has already run) -- a config that crashed
        the boot never becomes LKG, by construction: this is never on an
        exception path. Never raises -- LKG is a safety net, not
        something that should itself take down a successful boot.
        """
        try:
            if not self.config_path.exists():
                return
            backups_dir = self._config_backups_dir()
            shutil.copy2(self.config_path, backups_dir / self._LAST_KNOWN_GOOD_FILENAME)
            logger.debug("[CONFIG] last_known_good.json updated")
        except OSError as exc:
            logger.warning(f"[CONFIG] Could not write last_known_good.json: {exc}")

    def list_config_backups(self) -> list[tuple[str, Path]]:
        """(label, path) pairs for every restorable backup -- the rolling
        config-*.json snapshots plus last_known_good.json if present --
        newest first by mtime. Pure/no Qt dependency so this is unit-
        testable without a UI; samsara/ui/settings_qt.py's "Restore from
        backup" control is the only caller in the app itself.
        """
        backups_dir = self._config_backups_dir()
        entries: list[tuple[str, Path]] = []
        lkg_path = backups_dir / self._LAST_KNOWN_GOOD_FILENAME
        if lkg_path.exists():
            ts = datetime.fromtimestamp(lkg_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            entries.append((f"Last known good ({ts})", lkg_path))
        for f in backups_dir.glob("config-*.json"):
            ts = datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            entries.append((ts, f))
        entries.sort(key=lambda pair: pair[1].stat().st_mtime, reverse=True)
        return entries

    def _migrate_wake_word_config(self, default_config):
        """Migrate old flat wake word settings to new nested structure"""
        # The top-level key is the sole listener switch.  The stale nested
        # key is never read, so remove it.  Only use its legacy value when
        # the authoritative key is absent.
        wwc = self.config.get('wake_word_config')
        if isinstance(wwc, dict) and 'enabled' in wwc:
            legacy_enabled = wwc.pop('enabled')
            if 'wake_word_enabled' not in self.config:
                self.config['wake_word_enabled'] = bool(legacy_enabled)
            self.save_config()
        if self._wake_word_config_already_migrated():
            wake_profiles.validate_wake_profiles(self.config['wake_profiles'])
            logger.debug("wake config migration: no-op")
            return

        # wake_word_config already existing on entry means the branch below
        # that creates it (and saves) won't run -- if the only change this
        # pass makes is dropping a stale wake_targets block, nothing else
        # persists it, so save explicitly at the end. See the wake_targets
        # branch just below.
        _wwc_present_at_entry = isinstance(self.config.get('wake_word_config'), dict)
        _removed_stale_wake_targets = False

        # Migrate old wake_word/combined modes to wake_word_enabled + hold
        old_mode = self.config.get('mode')
        if old_mode in ('wake_word', 'combined'):
            self.config['wake_word_enabled'] = True
            self.config['mode'] = 'hold'
            logger.info(f"[MIGRATE] mode='{old_mode}' -> mode='hold' + wake_word_enabled=True")

        # Multi-wakeword: wake_targets -> wake_profiles rename (tribunal spec,
        # design review arc_20260629_170545.md). Renames an existing on-disk
        # list in place rather than discarding the user's own edits. If both
        # keys are present, wake_profiles is authoritative -- wake_targets is
        # an orphaned leftover from a rename that ran before wake_profiles
        # existed; drop it rather than merge its (stale) contents.
        if 'wake_targets' in self.config:
            if 'wake_profiles' not in self.config:
                self.config['wake_profiles'] = self.config.pop('wake_targets')
                logger.info("[MIGRATE] Renamed wake_targets -> wake_profiles")
            else:
                del self.config['wake_targets']
                logger.info("[MIGRATE] Removed stale wake_targets (wake_profiles is authoritative)")
                _removed_stale_wake_targets = True

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

        # Persist the stale wake_targets removal so it doesn't reappear next
        # boot. When wake_word_config didn't exist at entry, the branch above
        # already called save_config() and this same removal rode along with
        # it -- only save again here for the steady-state case where nothing
        # else in this pass triggered a save.
        if _removed_stale_wake_targets and _wwc_present_at_entry:
            self.save_config()

    def _wake_word_config_already_migrated(self) -> bool:
        """True when every migration this function performs would be a
        no-op against the current in-memory config: all target keys are
        already in place and no legacy source key survives. Lets
        _migrate_wake_word_config() skip the (redundant, every-boot) rename
        checks and wake_word_config deep-merge once a config has settled.
        """
        wwc = self.config.get('wake_word_config')
        if not isinstance(wwc, dict):
            return False
        if self.config.get('mode') in ('wake_word', 'combined'):
            return False
        if 'wake_targets' in self.config or 'wake_profiles' not in self.config:
            return False
        if 'wake_word' in self.config or 'wake_word_timeout' in self.config \
                or 'min_speech_duration' in self.config:
            return False
        if 'cancel_words' in wwc:
            return False
        profiles = self.config.get('wake_profiles')
        if not isinstance(profiles, list):
            return False
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            if 'send_policy' in profile or 'mode' not in profile or 'send_word' not in profile:
                return False
        return True

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

    def _migrate_ai_command_mode_config(self):
        """Migrate the deleted 'ai_command_mode' config block to
        'ava_command_session' (Ava Front Door spec v2 "Migration" section
        -- D3 replaces samsara/ai_command_mode.py, which no longer
        exists). Runs once per legacy config; a config that never had the
        top-level key (fresh installs, already-migrated configs) is a
        no-op.

        Key mapping -- meaningful keys carried over as-is:
          enabled, key, backend, model, queue_depth_cap, keep_warm,
          miss_limit, ready_cue_enabled, ready_cue_dir.
        Dead keys dropped (not straight-renamed -- each was tied to
        machinery this pass deletes, per spec's DROP list):
          wake_phrase       -- D2's exact-phrase Ava entry covers voice
                               entry now; D3 is key-only.
          step_settle_seconds, show_plan_hud
                            -- the old multi-step "plan" HUD/executor is
                               gone; the new waterfall resolves to at
                               most one dispatched action per utterance
                               via the app's ordinary command dispatch.
          menu_limit        -- replaced by shortlist_size (a per-utterance
                               fuzzy top-N, not a flat truncation of the
                               whole menu) -- not a value-preserving
                               rename, so not migrated.
        """
        if 'ai_command_mode' not in self.config:
            return
        old = self.config.pop('ai_command_mode') or {}
        carried = {
            k: old[k] for k in (
                'enabled', 'key', 'backend', 'model', 'queue_depth_cap',
                'keep_warm', 'miss_limit', 'ready_cue_enabled', 'ready_cue_dir',
            ) if k in old
        }
        dropped = sorted(set(old) - set(carried))
        existing = self.config.setdefault('ava_command_session', {})
        merged = {**carried, **existing}  # a fresh ava_command_session block (unlikely but possible) wins
        self.config['ava_command_session'] = merged
        self.save_config()
        logger.info(
            f"[MIGRATE] ai_command_mode -> ava_command_session: carried {sorted(carried)}, "
            f"dropped {dropped}"
        )

    def _maybe_announce_ava_command_session_migration(self) -> None:
        """One-time first-run-after-update notice (Ava Front Door spec v2
        "Migration"): toast + one spoken line, exactly once ever (per
        install), using HintManager's existing one-shot-per-hint_id
        primitive (samsara/hints.py) -- there is no version-gated "what's
        new" mechanism in this codebase to hook into instead, and building
        one is out of scope for this pass (a single fixed message doesn't
        need it). Silently a no-op if hints are disabled or this has
        already fired."""
        hints_mgr = getattr(self, 'hints', None)
        if hints_mgr is None:
            return
        hint_id = "ava_command_session_migration_v1"
        message = "Left-Alt is now an Ava command session."
        already_shown = hint_id in getattr(hints_mgr, '_shown', set())
        hints_mgr.maybe_show(hint_id, message)
        if already_shown:
            return
        ac = getattr(self, 'audio_coordinator', None)
        if ac is not None:
            try:
                ac.speak(message, category="system_notice")
            except Exception as e:
                logger.debug(f"[MIGRATE] Migration notice TTS failed: {e}")

    def _deep_update(self, target, source):
        """Recursively update target dict with missing keys from source"""
        for key, value in source.items():
            if key not in target:
                target[key] = value
            elif isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_update(target[key], value)
    
    def save_config(self):
        """Save configuration to JSON file, then rotate a timestamped backup.

        Prefers a true atomic os.replace() for the final swap into place,
        with a short retry + fallback (see step 3) -- a concurrent
        config-watcher read can transiently hold config.json open without
        FILE_SHARE_DELETE on Windows. If serialization throws partway
        through (as happened with the MenuItem-in-config bug), config.json
        is left untouched instead of being truncated.

        Also keeps the previous good copy at config.json.bak, AND
        (2026-07-2x config-backup safeguard) a rolling, timestamped copy of
        the PRE-write on-disk state under _config_backups_dir() (newest
        _CONFIG_BACKUP_KEEP kept -- see _rotate_config_backup()) so a bad
        write is never the only surviving copy of a user's settings.

        SAVE LATCH: a no-op if self._config_load_failed is set -- meaning
        load_config() could not read an existing config file THIS session
        (see its failure branch and _quarantine_corrupt_config()). This is
        the fix for the actual incident this whole safeguard exists for:
        load_config() used to fall through to `self.config = default_config
        ...; self.save_config()` on any read failure, silently overwriting
        a real (if corrupted) file with fresh defaults. Every write path in
        this app funnels through save_config() (persist_config,
        update_config_and_save, update_config), so gating it here is a
        single choke point -- defaults from a failed load can never reach
        disk over the quarantined original.

        Caller MUST hold self._config_lock.  Use persist_config() for
        external or fire-and-forget saves where you don't already hold it.
        """
        assert self._config_lock.locked(), (
            "save_config() called without holding _config_lock! "
            "Acquire _config_lock before mutating config and calling save_config()."
        )
        if getattr(self, '_config_load_failed', False):
            logger.warning(
                "[CONFIG] save_config() blocked -- config failed to load "
                "this session (see _quarantine_corrupt_config); refusing "
                "to write defaults over the preserved original. Restore a "
                "backup and restart to re-enable saving."
            )
            return

        tmp_path = self.config_path.with_suffix('.json.tmp')
        bak_path = self.config_path.with_suffix('.json.bak')

        try:
            # 0. Three-way merge: (last-known-disk, in-memory, current-disk).
            #    External edits (keys changed on disk since our last read/write)
            #    are preserved unless the app also changed the same key at
            #    runtime (in which case the runtime value wins).
            #    Skipped for load_config's migration saves -- see
            #    _config_migration_save there.
            merged = self.config
            if self.config_path.exists() and not getattr(self, '_config_migration_save', False):
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

            # 2a. Rolling, timestamped backup of the CURRENT on-disk state,
            #     taken before this write replaces it (2026-07-2x
            #     config-backup safeguard).
            if self.config_path.exists():
                self._rotate_config_backup()

            # 2b. Back up current config to the single .bak slot too (kept
            #     for existing recovery callers -- load_config()'s
            #     JSONDecodeError fallback, Import's own note to the user).
            #     Use shutil.copy2 (read -> write to a different path)
            #     rather than os.replace/rename for THIS copy specifically:
            #     on Windows, MoveFileExW fails with access denied when any
            #     open handle on the source file lacks FILE_SHARE_DELETE --
            #     Python's default open() never sets that flag, so the
            #     config watcher's background read would block the rename.
            if self.config_path.exists():
                try:
                    shutil.copy2(self.config_path, bak_path)
                except OSError as e:
                    logger.warning(f"[WARN] Could not backup config to .bak: {e}")

            # 3. Swap the new content into place. Prefer a true atomic
            #    os.replace() (same guarantee the module docstring always
            #    claimed but the code didn't actually deliver); retry
            #    briefly since a concurrent config-watcher read can
            #    transiently hold config.json open without FILE_SHARE_DELETE
            #    on Windows, then fall back to the historical direct
            #    overwrite (open('w') succeeds even under a shared read
            #    handle) so a save can never simply fail outright.
            swapped = False
            for _attempt in range(3):
                try:
                    os.replace(tmp_path, self.config_path)
                    swapped = True
                    break
                except OSError as exc:
                    logger.debug(f"[CONFIG] os.replace attempt {_attempt + 1} failed: {exc}")
                    time.sleep(0.02)
            if not swapped:
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
        old_ava_cmd_cfg = self.config.get('ava_command_session') if 'ava_command_session' in changes else None

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
        if 'hotkey' in changes or 'command_mode' in changes:
            self.refresh_mouse_hook()
        if 'ui' in changes and getattr(self, 'listening_indicator', None) is not None:
            self._schedule_ui(self.listening_indicator.set_idle_animation,
                              self._idle_animation_enabled())
        # Only rebuild a detector that has been loaded: before the lazy wake
        # load (or while it runs) the loader picks the new phrase up itself.
        if 'wake_word_config' in changes and self.wake_ready_state() == 'ready':
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
        if 'ava_command_session' in changes:
            self._maybe_exit_ava_command_session_on_config_change(
                old_ava_cmd_cfg or {}, changes['ava_command_session'],
            )

    def _maybe_exit_ava_command_session_on_config_change(self, old_cfg: dict, new_cfg) -> None:
        """Force-exit the Ava command session if a config change made
        while it's active would disable the feature or change its
        activation key out from under the user.

        2026-07-19 incident report item 3 (carried over per Ava Front
        Door spec v2's "2026-07-19 exclusive-ownership + generation
        guards carry over"): previously no runtime side effect existed
        for these config changes at all -- disabling the feature or
        changing its key while the session was active removed the user's
        only exit binding while ava_command_session_active stayed True.
        Shared by update_config() and _apply_disk_config() (external file
        edits) -- both funnel here so the check and its logging exist in
        exactly one place.
        """
        if not self.ava_command_session_active:
            return
        if not isinstance(new_cfg, dict):
            return
        old_cfg = old_cfg or {}
        was_enabled = old_cfg.get('enabled', True)
        now_enabled = new_cfg.get('enabled', True)
        old_key = old_cfg.get('key', 'left_alt')
        new_key = new_cfg.get('key', 'left_alt')
        if (was_enabled and not now_enabled) or (old_key != new_key):
            logger.info(
                f"[AVA-CMD] Config change while active (enabled {was_enabled}->{now_enabled}, "
                f"key {old_key!r}->{new_key!r}) -- force-exiting"
            )
            self.exit_ava_command_session()

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
        if 'hotkey' in changed or 'command_mode' in changed:
            try:
                self.refresh_mouse_hook(rearm=False)
            except Exception as e:
                logger.exception(f"[CONFIG] mouse hook refresh error: {e}")
        if 'ui' in changed and getattr(self, 'listening_indicator', None) is not None:
            self._schedule_ui(self.listening_indicator.set_idle_animation,
                              self._idle_animation_enabled())
        if 'wake_word_config' in changed and self.wake_ready_state() == 'ready':
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
        if 'ava_command_session' in changed:
            try:
                old_v, new_v = changed['ava_command_session']
                self._maybe_exit_ava_command_session_on_config_change(
                    old_v if isinstance(old_v, dict) else {}, new_v,
                )
            except Exception as e:
                logger.exception(f"[CONFIG] ava_command_session update error: {e}")

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

    # Persisted auto-calibration (boot fix 4, perf_artifacts/boot_profile.md):
    # a separate file, not config.json, so a boot that reuses or refreshes a
    # calibration never rotates a config backup.
    _CALIBRATION_CACHE_FILENAME = "mic_calibration.json"
    _CALIBRATION_MAX_AGE_S = 24 * 3600

    def _calibration_identity(self) -> dict:
        """What a stored calibration must match to be reused: same device
        (by name when known -- PortAudio indices shift -- else by index),
        same capture rate, same multiplier."""
        return {
            'device_id': self.config.get('microphone'),
            'device_name': self.config.get('microphone_name'),
            'capture_rate': int(getattr(self, 'capture_rate', 0) or 0),
            'multiplier': float(self.config.get('cal_multiplier', 3.0)),
        }

    def _calibration_cache_path(self) -> Path:
        return self.config_path.parent / self._CALIBRATION_CACHE_FILENAME

    def _load_fresh_calibration(self, now: "float | None" = None) -> "float | None":
        """Threshold from the calibration cache if it belongs to the current
        device and is younger than _CALIBRATION_MAX_AGE_S, else None."""
        path = self._calibration_cache_path()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                stored = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning(f"[CAL] Ignoring unreadable calibration cache: {e}")
            return None
        if not isinstance(stored, dict):
            return None
        ident = self._calibration_identity()
        if ident['device_name'] and stored.get('device_name'):
            same_device = stored.get('device_name') == ident['device_name']
        else:
            same_device = stored.get('device_id') == ident['device_id']
        if not same_device:
            return None
        if stored.get('capture_rate') != ident['capture_rate']:
            return None
        if stored.get('multiplier') != ident['multiplier']:
            return None
        try:
            age = (time.time() if now is None else now) - float(stored['calibrated_at'])
            threshold = float(stored['threshold'])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= age < self._CALIBRATION_MAX_AGE_S) or not math.isfinite(threshold):
            return None
        if self._calibration_saturated(threshold):
            return None
        return threshold

    @staticmethod
    def _calibration_saturated(threshold: float) -> bool:
        """True when calibrate_threshold clamped to CALIBRATION_CEILING: the
        "ambient" recording was speech or noise, not room tone (live log
        2026-09-13: ambient RMS 0.1589 -> 0.1500, then reused on the next
        boot). Such a value may serve the current session but must never be
        stored or reused -- re-measuring beats reviving a bad calibration."""
        from samsara.constants import CALIBRATION_CEILING
        return threshold >= CALIBRATION_CEILING

    def _save_calibration(self, threshold: float) -> None:
        """Persist a measured threshold with its device identity. Never raises.
        A ceiling-clamped (contaminated) threshold is not persisted."""
        if self._calibration_saturated(threshold):
            logger.info(f"[CAL] Not persisting calibration at the ceiling "
                        f"({threshold:.4f}); the room was not quiet")
            return
        path = self._calibration_cache_path()
        record = dict(self._calibration_identity(),
                      threshold=float(threshold), calibrated_at=time.time())
        tmp = path.with_suffix('.json.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(record, f, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"[CAL] Could not persist calibration: {e}")

    def _measure_speech_threshold(self) -> "tuple[float, bool]":
        """Record ambient noise and derive a threshold. Returns (threshold,
        measured_ok); a failed or empty recording yields the default and
        False so it is never persisted as a real calibration."""
        mic_id = self.config.get('microphone')
        multiplier = self.config.get('cal_multiplier', 3.0)
        try:
            rms_samples = measure_ambient_rms(mic_id, self.capture_rate)
            threshold = calibrate_threshold(rms_samples, multiplier=multiplier)
            ambient = float(np.median(rms_samples)) if rms_samples else 0.0
            logger.debug(f"[CAL] Ambient RMS: {ambient:.4f} | "
                  f"Multiplier: {multiplier}x | Threshold: {threshold:.4f}")
            return threshold, len(rms_samples) >= 3
        except Exception as e:
            threshold = DEFAULT_SPEECH_THRESHOLD
            logger.exception(f"[CAL] Calibration failed ({e}), using default {threshold:.4f}")
            return threshold, False

    def _apply_speech_threshold(self, threshold: float) -> None:
        """Apply to wake word audio config (in memory; persisted by the next save)."""
        with self._config_lock:
            ww_config = self.config.get('wake_word_config', {})
            if 'audio' not in ww_config:
                ww_config['audio'] = {}
            ww_config['audio']['speech_threshold'] = threshold
            self.config['wake_word_config'] = ww_config

    def _run_calibration_if_auto(self, use_cache=False):
        """Run mic calibration if threshold_mode is 'auto'. Updates config in place.

        use_cache=True (boot only) reuses a fresh calibration of the same
        device instead of recording. Every other caller (manual recalibrate,
        microphone switch) always records. Returns 'manual', 'cached',
        'measured' or 'failed'.
        """
        mode = self.config.get('threshold_mode', 'auto')
        if mode != 'auto':
            thresh = self.config.get('wake_word_config', {}).get('audio', {}).get(
                'speech_threshold', DEFAULT_SPEECH_THRESHOLD)
            logger.debug(f"[CAL] Threshold mode: manual ({thresh:.4f})")
            return 'manual'

        if use_cache:
            cached = self._load_fresh_calibration()
            if cached is not None:
                logger.info(f"[CAL] Reusing calibration from the last 24 h "
                            f"(threshold {cached:.4f}); refreshing after startup")
                self._apply_speech_threshold(cached)
                return 'cached'

        threshold, ok = self._measure_speech_threshold()
        if ok:
            self._save_calibration(threshold)
        self._apply_speech_threshold(threshold)
        return 'measured' if ok else 'failed'

    def _refresh_calibration_in_background(self) -> None:
        """Re-measure after startup when boot reused a stored calibration, so
        a changed room still gets picked up. Skipped (and the stored value
        kept) if a hold-to-dictate recording is live at either end of the
        1.5 s window -- the user's own speech must not become "ambient"."""
        def _do():
            if self.config.get('threshold_mode', 'auto') != 'auto':
                return
            if getattr(self, 'recording', False):
                logger.info("[CAL] Background recalibration skipped: recording in progress")
                return
            threshold, ok = self._measure_speech_threshold()
            if not ok or getattr(self, 'recording', False) or self._calibration_saturated(threshold):
                logger.info("[CAL] Background recalibration discarded")
                return
            self._save_calibration(threshold)
            self._apply_speech_threshold(threshold)
            logger.info(f"[CAL] Background recalibration: threshold {threshold:.4f}")
        thread_registry.spawn("dictation.calibration_refresh", _do, daemon=True)

    def recalibrate_mic(self):
        """Re-run calibration in background and update config."""
        def _do():
            self._run_calibration_if_auto()
            self.persist_config()
        thread_registry.spawn("dictation._do", _do, daemon=True)

    def get_available_microphones(self):
        """Get list of available microphone devices."""
        return list_microphones(
            show_all=self.config.get('show_all_audio_devices', False),
        )

    def _mic_refresh_blocked(self) -> bool:
        """True only for the one case refresh_audio_devices() genuinely
        cannot proceed: an actual hold-to-dictate press is open right now
        (self.recording). Deliberately narrower than
        _is_audio_capture_active() -- see that method's docstring for why
        the two questions ("is a stream open that re-enumeration would
        disturb, and can it be cycled?" vs "does the always-on ACE engine
        exist?") are not the same question, and 2026-07-17's fix comment on
        refresh_audio_devices() for the bug this predicate exists to avoid
        reintroducing.

        continuous_active/wake_word_active are NOT included: as of the ACE
        ring-consumer migration (_start_ace_engine), neither owns a
        separate PortAudio stream -- both read the same FrameBus the ACE
        engine writes, so refresh_audio_devices() cycling that ONE engine
        around the PortAudio re-init is sufficient; there is nothing left
        for those two flags to protect here. self.recording is excluded
        from that cycling on UX/data-integrity grounds, not stream
        ownership: interrupting a live user-held utterance mid-word to
        cycle capture would risk losing or corrupting what they're saying,
        unlike an idle continuous/wake listener which only misses a brief
        (sub-second) window of ambient audio.
        """
        return self.recording

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

        2026-07-17 FIX -- this function was UNREACHABLE in production before
        this change. It used to gate on _is_audio_capture_active(), which
        OR's in `self._ace_engine is not None and self._ace_engine._running`
        -- and the ACE engine is started unconditionally in __init__ and
        runs for the whole process lifetime (_start_ace_engine's own
        docstring: "runs permanently"), so that term is true from shortly
        after boot until shutdown. The guard was therefore a constant True
        for the entire life of a running app: every refresh call hit
        "[MIC] refresh skipped -- audio active" and returned the stale
        list, and _reconcile_microphone_selection() below it never ran
        either. Confirmed empirically 2026-07-16 -- not a hypothesis.

        Fixed by splitting what the guard was conflating: whether a stream
        is open that re-enumeration would disturb, and separately, whether
        that stream can just be cycled around the re-init instead of
        treated as an unconditional block. The ACE engine -- the sole
        remaining stream owner post ring-consumer-migration (see
        _mic_refresh_blocked's docstring) -- gets stopped before
        sd._terminate()/_initialize() and restarted after, in a finally
        block so a raising re-init can never leave the user with no
        capture at all. The one genuine hard block
        (_mic_refresh_blocked(): self.recording) still refuses outright,
        same as the old behavior for that one case -- "Stop dictation to
        refresh devices" is honest there, just not for every other state
        the old guard also blocked on.

        Thread-safety: call only from the Qt/UI thread, same contract as
        before -- switch_microphone() (existing precedent) already cycles
        this same ACE engine's stop()/start() synchronously from the Qt
        thread with no additional marshalling, and every existing reader/
        writer of self.available_mics (tray_qt.py's menu rebuild,
        settings_qt.py, the setup wizards) already only touches it from
        the Qt thread too. Callers that need to invoke this off the Qt
        thread must marshal onto it themselves (e.g. via
        samsara.ui.qt_runtime.post()) rather than this method inventing
        its own thread-hop -- see first_run_wizard_qt.py's refresh handler
        for the pattern.

        Returns the fresh list, in the same shape as
        get_available_microphones() (this IS that same method -- there is
        only one enumeration code path).
        """
        if self._mic_refresh_blocked():
            logger.info("[MIC] refresh skipped — dictation hold in progress")
            return self.available_mics

        ace = self._ace_engine
        ace_was_running = ace is not None and ace._running
        if ace_was_running:
            try:
                ace.stop()
            except Exception as exc:
                logger.exception(f"[MIC] ACE engine stop before refresh failed: {exc}")

        try:
            try:
                force_rescan()
            except Exception as exc:
                logger.warning(
                    "[MIC] PortAudio re-scan failed, continuing with re-query: %s", exc,
                )

            self.available_mics = self.get_available_microphones()
            self._reconcile_microphone_selection()
        finally:
            # Restart unconditionally if it was running -- this must happen
            # even if the re-init or re-enumeration above raised, or the
            # user is left with no audio capture at all until next restart.
            if ace_was_running:
                try:
                    ace.start()
                except Exception as exc:
                    logger.exception(f"[MIC] ACE engine restart after refresh failed: {exc}")

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

    def get_transcription_params(self, include_vocabulary: bool = True):
        """Get transcription parameters based on performance mode setting.

        include_vocabulary=False builds initial_prompt from ONLY the
        explicit user-set config['initial_prompt'] override (Priority 1),
        omitting genuine trained vocabulary and auto-derived command
        phrases (Priorities 2/3) -- see voice_training_qt.get_initial_prompt
        for the full history. Free-form dictation paths (hold-to-dictate,
        toggle-session DICTATE/AVA, transcribe_continuous_buffer,
        process_wake_word_buffer) pass False; command-matched paths keep
        the default True. Defaults to True so every OTHER existing caller
        (streaming.py's final-pass params, the voice-training phrase
        self-test) is unaffected by this parameter's addition.

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
            'initial_prompt': self.voice_training_window.get_initial_prompt(
                include_vocabulary=include_vocabulary,
            ),
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
                # Queue 124: was True, and that silently lost long-form
                # speech -- see the block on `balanced` below for the
                # mechanism and the measurements. `fast` had it worse than
                # `balanced` (87.9% of the words returned against 94.4%), and
                # fixing it costs `fast` 0.21 WER points and 16 ms on short
                # clips. "May sacrifice some accuracy" is a fair description
                # of beam 1; silently dropping an eighth of a paragraph is
                # not, and it is not what anyone chooses this mode for.
                'without_timestamps': False,
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
        else:  # balanced
            return {
                **base_params,
                'beam_size': 3,
                'vad_filter': True,
                'vad_parameters': dict(
                    min_silence_duration_ms=500,
                    speech_pad_ms=200,
                ),
                'condition_on_previous_text': False,
                # Queue 124. THIS LINE WAS `True`, AND IT LOST DICTATED TEXT.
                # Do not put it back without reading the report.
                #
                # Measured on 5 minutes of continuous read speech, one
                # parameter changed per arm (perf_artifacts + queue 124):
                #
                #   base   balanced              7.95% WER, 94.4% of words
                #   base   + without_timestamps  2.89% WER, 99.5% of words
                #   small  balanced             34.88% WER, 66.1% of words
                #   small  + without_timestamps  1.63% WER, 99.5% of words
                #
                # condition_on_previous_text is NOT the cause -- flipping it
                # alone changed nothing (base 7.95% -> 8.31%). The 118 report
                # attributed the loss to lost context; that was wrong.
                #
                # MECHANISM, read from faster_whisper/transcribe.py: with
                # without_timestamps=True the prompt carries the
                # `no_timestamps` token (:1554), so the model emits no
                # timestamp tokens, so _split_segments_by_timestamps finds
                # none and falls through to `seek += segment_size` -- the
                # decoder can only advance in whole 30 s blocks. A window
                # culled by no_speech_threshold/log_prob_threshold does
                # `seek += segment_size; continue` (:1234) and takes 30
                # SECONDS OF SPEECH with it, and a window that decodes only
                # part of its audio cannot resume at the last word. With
                # timestamps on, seek advances to the last decoded timestamp
                # (:1079) and nothing between windows is lost.
                #
                # Short utterances hide all of it: they fit in one window, so
                # there is no seam to lose. That is why every prior bench
                # missed this.
                'without_timestamps': False,
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
        the top of this file). Used by both the normal (<30s) and [LONG]
        branches of the hotkey transcribe() closure -- they share this same
        dict, so this is the single place that guarantee is enforced.

        initial_prompt's vocabulary component: this method is shared by two
        different hotkey presses distinguished only by
        command_mode_recording at call time -- ordinary hold-to-dictate
        (free prose, never matched against the command registry) and the
        command-only hotkey / Mouse 4 (self.command_mode_recording=True,
        matched against the command registry a few lines below). Only the
        latter actually needs vocabulary biasing at all;
        get_transcription_params's include_vocabulary therefore mirrors
        command_mode_recording exactly. Explicit custom prompt (Priority 1,
        config['initial_prompt']) still applies to both regardless -- it's
        a user-set override, not auto-derived vocabulary.

        HISTORY: 2026-07-16 (commit 02e00b9) first gated only the
        auto-derived command-phrase list (Priority 3) off this path, having
        proved it destabilized long continuous-speech decodes. 2026-07-17/18
        (SPARK decode matrix, N=10/cell against both incident WAVs) found
        the SAME failure mode from Priority 2 alone (the short "Common
        terms:" trained-vocabulary list) -- ANY non-conversational
        vocabulary content in initial_prompt is the actual destabilizer,
        not command phrases specifically. Widened accordingly: hold-to-
        dictate now drops Priorities 2 AND 3 both, keeping only the
        explicit Priority-1 override if the user set one.

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
        # include_vocabulary mirrors command_mode_recording (True only for
        # the command-only hotkey / Mouse 4, matched below): that press
        # benefits from vocabulary biasing (trained "Common terms" + the
        # auto-derived command-phrase list), but ordinary hold-to-dictate
        # never matches the command registry, so it drops both -- see the
        # docstring above.
        _is_command_hotkey = getattr(self, 'command_mode_recording', False)
        transcribe_params = self.get_transcription_params(include_vocabulary=_is_command_hotkey)
        transcribe_params['initial_prompt'] = transcribe_params['initial_prompt'] or ""
        # DISABLE faster-whisper's VAD for hotkey-triggered dictation.
        # User explicitly pressed the hotkey — don't strip their speech.
        transcribe_params['vad_filter'] = False
        # Force a clean slate on EVERY hotkey press. Conditioning on
        # tokens carried over from a previous press is what let
        # hallucinations escalate over a session -- each press must
        # start with zero residual conversational state, independent of the
        # [LONG] path (which has its own reasons not to condition).
        transcribe_params['condition_on_previous_text'] = False
        # Command-mode hotkey (Right Ctrl / Mouse 4, self.command_mode_recording)
        # is matched against the English command registry -- force English
        # regardless of the configured dictation language so command
        # recognition stays reliable. Commands are English-only by design;
        # Ava-mode (Right Alt) recordings are NOT forced here since that
        # content goes to the LLM as a natural-language query, not matched
        # against a fixed phrase registry.
        if _is_command_hotkey:
            transcribe_params['language'] = 'en'
        return transcribe_params

    def _filter_dictation_language(self, text, info, *, remember=True, feedback=True):
        """Return empty on unexpected low-confidence language or script mismatch."""
        if not text.strip():
            return text
        # VERBATIM toggle, consumed BEFORE the gate and before smart
        # corrections (the prompt's ordering requirement). This is the single
        # function every finalize lane -- hold, toggle-DICTATE, wake and
        # streaming commit -- calls first, so hooking it here makes the
        # toggle work in every mode without four separate call sites. The
        # phrase is consumed (returns "") so it is never typed.
        #
        # getattr, not a direct call: this method is bound onto bare
        # SimpleNamespace stubs by the language-gate tests, which exercise
        # the gate in isolation and have no reason to know about the verbatim
        # profile. A stub without the collaborator simply skips the toggle.
        _consume = getattr(self, '_consume_verbatim_toggle', None)
        if _consume is not None and _consume(text, feedback=feedback):
            return ''
        gate = self._language_confidence_gate
        language = getattr(info, 'language', None)
        probability = getattr(info, 'language_probability', None)
        reason, expected = gate.evaluate(
            text, language, probability, _languages.resolve_transcribe_language(self),
            self.config.get('language_confidence_floor', _languages.LANGUAGE_CONFIDENCE_FLOOR),
            remember=remember,
        )
        if reason is None:
            return text
        logger.info('[LANGUAGE] Rejected language=%s probability=%s text=%r',
                    language, probability, text[:40])
        flight_recorder.record('decode.language_rejected', language=language,
                               language_probability=probability, reason=reason,
                               expected_languages=sorted(expected))
        if feedback:
            self.play_sound('scratch_refuse')
        return ''

    def _decode_hotkey_audio(self, audio_faded, transcribe_params, audio_duration, *, free_form=None):
        """One full hotkey decode pass: single-shot or [LONG]-split
        (mirrors the >_LONG_DECODE_CEILING_S resource-guard fallback),
        followed by the same segment-level quality gating either way.

        Extracted from the hotkey transcribe() closure so both the primary
        decode and the SPARK P0 auto-retry (_apply_retry_on_suspected_loss)
        call the REAL production branching logic instead of two independent
        copies that could silently drift apart -- same rationale as
        _build_hotkey_transcribe_params's own extraction (see
        tests/test_transcription_params.py's module docstring).

        Returns a _HotkeyDecodeResult(text, low_confidence, seg_list,
        detected_lang, diag_path, language_rejected).
        """
        if free_form is None:
            free_form = not getattr(self, 'command_mode_recording', False)
        decode_infos = []
        if audio_duration > _LONG_DECODE_CEILING_S:
            diag_path = "long"
            chunks = _split_audio_at_silences(audio_faded, self.model_rate)
            logger.info(f"[LONG] {audio_duration:.1f}s recording exceeds the "
                  f"{_LONG_DECODE_CEILING_S:.0f}s single-decode ceiling -- split "
                  f"into {len(chunks)} chunk(s) at silence boundaries")
            seg_list = []
            detected_lang = None
            for idx, chunk in enumerate(chunks):
                chunk_dur = len(chunk) / self.model_rate
                if chunk_dur < 0.2:
                    continue
                with self.model_lock:
                    segs, chunk_info = self.model.transcribe(chunk, **transcribe_params)
                detected_lang = getattr(chunk_info, 'language', None) or detected_lang
                decode_infos.append(chunk_info)
                chunk_segs = list(segs)
                seg_list.extend(chunk_segs)
                logger.info(f"[LONG] Chunk {idx + 1}/{len(chunks)}: "
                      f"{chunk_dur:.1f}s → {len(chunk_segs)} segment(s)")
        else:
            diag_path = "short"
            with self.model_lock:
                segments, info = self.model.transcribe(audio_faded, **transcribe_params)
            detected_lang = getattr(info, 'language', None)
            decode_infos.append(info)
            seg_list = list(segments)

        text, low_confidence = _apply_segment_quality_gates(
            seg_list, transcribe_params, audio_duration,
        )
        if free_form and text:
            for info in decode_infos:
                if not self._filter_dictation_language(text, info):
                    return _HotkeyDecodeResult('', False, seg_list, detected_lang, diag_path, True)
        return _HotkeyDecodeResult(text, low_confidence, seg_list, detected_lang, diag_path)

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

        # Format numbers: digits only where the words are used as numbers
        # ("page one" -> "page 1", "twenty one" -> "21"); a prose "one" stays
        # a word. Rule and history: samsara/number_format.py (queue 55).
        if self.config.get('format_numbers', True):
            from samsara.number_format import format_spoken_numbers
            text = format_spoken_numbers(text)

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
        """True if ANY audio input stream is conceptually "in use" --
        recording, continuous mode, wake word mode, or the ACE engine.

        2026-07-17: NOT used by refresh_audio_devices() anymore -- see
        _mic_refresh_blocked() for that. Since the ACE engine starts
        unconditionally at boot and runs for the process lifetime, the
        last term here is true almost from launch to shutdown, which made
        this predicate a de facto constant True whenever used as a "safe
        to re-enumerate" gate (confirmed empirically 2026-07-16). It
        remains correct and UNCHANGED for its one remaining caller,
        calibrate_echo_cancellation(): that path does its own blocking
        sd.play()/sd.rec() directly against the configured device, which
        DOES genuinely conflict with a concurrently-open ACE stream on the
        same device (unlike refresh_audio_devices(), calibration has no
        stop/restart-around-it option -- it needs the device fully quiet
        for the duration of the lag measurement), so "the always-on ACE
        engine exists" is the right question there, not the wrong one.
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
            _boot_log = getattr(self, '_boot_log', lambda s: None)
            if hasattr(_boot_log, 'begin_thread'):
                _boot_log.begin_thread()
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
            _boot_log("async: Whisper model load (hotkey dictation ready)")
            self.update_splash(
                "Speech model ready...", 70,
                f"Loaded on {device.upper()} in {load_time:.1f} seconds",
            )

            logger.info("[INIT] Loading Silero VAD...")
            self.update_splash(
                "Calibrating speech detection...", 76,
                "Loading the local voice activity detector",
            )
            # Load Silero VAD for real-time speech gating (async-safe: if this
            # fails, the wake callback falls back to RMS).
            self._load_vad_model()
            _boot_log("async: Silero VAD load")

            # OpenWakeWord is NOT loaded here any more (boot fix 1): with wake
            # listening off it is never needed, and with it on the
            # start_wake_word_mode() call below hands the load to its own
            # thread, so "Ready" never waits on it.
            if self.config.get('wake_word_enabled', False):
                logger.info("[INIT] OpenWakeWord deferred to its own thread (wake word on)")
            else:
                logger.info("[INIT] OpenWakeWord not loaded (wake word off; loads on first enable)")

            logger.info("Ready for dictation.")

            # Auto-start modes that require always-on listening
            mode = self.config.get('mode', 'hold')
            self.update_splash(
                "Starting listening services...", 90,
                "Activating the configured audio modes",
            )
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
                self.update_splash(
                    "Starting gesture control...", 94,
                    "Activating hands-free gesture recognition",
                )
                self._start_gesture_lane()

            # The main thread opens ACE and builds the rest of the interface
            # while this worker loads the models.  Do not announce completion
            # until create_tray_icon() has scheduled the tray and main window.
            self.update_splash(
                "Finalizing Samsara...", 96,
                "Waiting for all startup systems to report ready",
            )
            shell_ready = getattr(self, "_startup_shell_ready", None)
            if shell_ready is not None:
                shell_ready.wait()

            # Warm the local Ollama model (if Smart Corrections resolves to
            # it) so the first real correction call doesn't also pay a
            # cold-start model-load penalty. warm_up() resolves the backend
            # first, and that is a synchronous HTTP probe of Ollama (2 s
            # timeout, 4.0 s measured on every boot while Ollama is down) --
            # so it runs on its own thread, never on this lane before
            # "Startup complete", and only when Smart Corrections is on
            # (settings_qt warms it when the user turns it on later).
            if smart_corrections_is_enabled(self):
                def _smart_warm_up():
                    try:
                        smart_corrections_warm_up(self)
                    except Exception as e:
                        logger.debug(f"[SMART] warm_up call failed: {e}")
                thread_registry.spawn("dictation.smart_corrections_warm_up",
                                      _smart_warm_up, daemon=True)

            # Ensure clean state — reset any recording flags that may have
            # been tripped by keyboard events during startup
            self.recording = False
            self.hotkey_pressed = False
            self.command_mode_recording = False
            self._hotkey_recording = False
            if hasattr(self, 'listening_indicator'):
                self._schedule_ui(self.listening_indicator.set_listening, False)

            _ready_detail = "All configured systems are ready"
            if self.config.get('wake_word_enabled', False) and \
                    self.wake_ready_state() != 'ready':
                _ready_detail = "Dictation ready; wake word still loading"
            self.update_splash("Samsara ready", 100, _ready_detail)
            logger.info(f"[INIT] Startup complete. (wake: {self.wake_ready_state()})")
            _boot_log("async: startup complete")
            try:
                from samsara import ava_readiness
                ava_readiness.schedule_warm_on_boot(
                    self, lambda name, fn: thread_registry.spawn(name, fn, daemon=True))
            except Exception as exc:
                logger.debug(f"[AVA-WARM] could not schedule warm-up: {exc}")

            # Config-backup safeguard: only NOW, having reached "startup
            # complete" without raising anywhere above in this closure, is
            # the on-disk config worth trusting as "last known good" -- a
            # config that crashed the boot never reaches this line, so it
            # can never become LKG. See _write_last_known_good().
            self._write_last_known_good()

            # Boot reused a stored calibration: refresh it now, off the boot path.
            if getattr(self, '_calibration_refresh_due', False):
                self._calibration_refresh_due = False
                self._refresh_calibration_in_background()

            # Completion animation and minimum display timing belong to the
            # splash.  Only request dismissal after Startup complete is true.
            try:
                splash = getattr(self, "splash", None)
                if splash is not None:
                    if hasattr(splash, "complete"):
                        splash.complete("Samsara ready", _ready_detail)
                    self._schedule_ui(self._close_splash_post_load)
            except Exception as e:
                logger.exception(f"[SPLASH] Could not complete splash: {e}")
          except Exception as _exc:
            import traceback
            traceback.print_exc()
            self.loading_model = False
            self._startup_failed = True
            err_msg = str(_exc)
            self.update_splash(
                "Startup could not finish", None, err_msg, error=True,
            )
            self._schedule_ui(self._show_startup_error, err_msg)

        thread = thread_registry.spawn("dictation.load", load, daemon=True)
    
    # Hotkey/key-listener delegates. Runtime fallback remains observable to
    # source-level regression tests without retaining decision logic here.
    def parse_hotkey(self, hotkey_str):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.parse_hotkey(self, hotkey_str)

    def get_key_name(self, key):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.get_key_name(self, key)

    def get_active_keys(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.get_active_keys(self)

    def check_hotkey_state(self, hotkey_str):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.check_hotkey_state(self, hotkey_str)

    def get_pressed_keys_debug(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.get_pressed_keys_debug(self)

    def _hands_free_dictation_commit_available(self) -> bool:
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._hands_free_dictation_commit_available(self)

    def _commit_pending_hands_free_dictation(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._commit_pending_hands_free_dictation(self)

    def on_key_press(self, key):
        # main_hotkey = getattr(self, '_main_hotkey_override', None) or self.config['hotkey']
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.on_key_press(self, key)

    def _start_recording_declined(self) -> bool:
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._start_recording_declined(self)

    def _other_hotkey_held(self) -> bool:
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._other_hotkey_held(self)

    def _hotkey_state_text(self) -> str:
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._hotkey_state_text(self)

    def on_key_release(self, key):
        # main_hotkey = getattr(self, '_main_hotkey_override', None) or self.config['hotkey']
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.on_key_release(self, key)

    def _install_capslock_hook(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._install_capslock_hook(self)

    def _uninstall_capslock_hook(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._uninstall_capslock_hook(self)

    def _on_capslock_event(self, event):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._on_capslock_event(self, event)

    def _capslock_start_streaming(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._capslock_start_streaming(self)

    def _capslock_stop_streaming(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._capslock_stop_streaming(self)

    def _mouse_hook_bindings(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._mouse_hook_bindings(self)

    def _install_mouse_listener(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._install_mouse_listener(self)

    def refresh_mouse_hook(self, rearm=True):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.refresh_mouse_hook(self, rearm=rearm)

    def _on_mouse_hook_failed(self, reason):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._on_mouse_hook_failed(self, reason)

    def release_mouse_buttons(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.release_mouse_buttons(self)

    def _mouse_fallback_to_keyboard(self, why, reason=''):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._mouse_fallback_to_keyboard(self, why, reason=reason)

    def mouse_hotkey_status(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.mouse_hotkey_status(self)

    def reenable_mouse_hotkey(self):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster.reenable_mouse_hotkey(self)

    def _on_mouse_button(self, button_name, pressed):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._on_mouse_button(self, button_name, pressed)

    def _main_hotkey_toggle_off(self, source: str) -> None:
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._main_hotkey_toggle_off(self, source)

    def _mouse_guard(self, why: str) -> None:
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._mouse_guard(self, why)

    def _on_main_hotkey_mouse(self, pressed):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._on_main_hotkey_mouse(self, pressed)

    def _on_command_button(self, button_name, pressed):
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._on_command_button(self, button_name, pressed)

    def _check_command_mode_key(self, key, pressed: bool) -> None:
        from samsara import hotkeys
        return hotkeys.HotkeyCluster._check_command_mode_key(self, key, pressed)
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
        # Queue 69: curated everyday words (and a Show Numbers click) are
        # "reserved"; only the generic fallback below is not. The cancel
        # window applies to reserved words only when the user extends it.
        reserved = policy is not None
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
            reserved=reserved,
        )

    def _stage_hands_free_cancel_window(self, match, run) -> float:
        """Queue 69: SessionModeManager's cancel_window_fn. Stages `run` behind
        a cancel window when the execution policy gives this command one;
        returns the window in seconds, or 0.0 to dispatch now (off, a read
        command, one that keeps its yes/no, or anything going wrong)."""
        try:
            from samsara import execution_policy as _policy
            delay = _policy.cancel_window_for(match.phrase, self, reserved=bool(getattr(match, 'reserved', False)))
            if delay <= 0:
                # An earlier window still open runs first: commands keep the
                # order they were said in.
                _policy.flush_cancel_window()
                return 0.0
            inv = _policy.Invocation(
                command_id=match.phrase,
                route=_policy.Route.EXACT,
                generation=_policy.current_generation(self),
                source_text=match.dispatch_text,
            )
            _policy.stage_cancel_window(self, inv, match.phrase, delay, run)
            return delay
        except Exception as exc:
            logger.warning(f'[SESSION] cancel window unavailable, dispatching now: {exc}')
            return 0.0

    def _handle_deferred_session_outcome(self, outcome) -> None:
        """A command held by a cancel window finally ran (or was refused)."""
        logger.info(f'[SESSION] deferred outcome={outcome.kind} detail={outcome.detail}')
        self._handle_session_dispatch_outcome(outcome, str(outcome.detail.get('dispatch_text') or ''))

    def _ensure_session_mode_manager(self) -> "SessionModeManager":
        """Lazily build the SessionModeManager with its wired callables.

        Built once; every session entry/exit calls reset() on this same
        instance rather than reconstructing it (session end always discards
        MODE STATE -- current mode, the unit-of-work stack -- but the
        wiring itself is stable for the app's lifetime).
        """
        if self._session_mode_manager is not None:
            return self._session_mode_manager

        # Queue 142: install the configured commit word before anything reads
        # it. session_modes keeps it as a module constant, so this one call
        # also reaches command_catalog, quick_reference_qt, tutorial_qt,
        # first_run_wizard_qt and settings/modes_qt, all of which read
        # DICTATE_COMMIT_PHRASE at call time.
        try:
            from samsara import session_modes  # noqa: PLC0415
            _configured_commit = (self.config.get('command_mode', {})
                                  .get('dictate_commit_word')
                                  or session_modes.DEFAULT_DICTATE_COMMIT_PHRASE)
            _installed = session_modes.set_commit_phrase(_configured_commit)
            if _installed != str(_configured_commit).strip().lower():
                logger.warning(
                    "[SESSION] commit word %r refused (one word only); keeping %r",
                    _configured_commit, _installed)
            else:
                logger.info("[SESSION] dictation commit word: %r", _installed)
            if _installed in session_modes.COMMIT_WORD_HOMOPHONE_RISKS:
                logger.warning(
                    "[SESSION] commit word %r is a common filler; a pause after it "
                    "in ordinary speech will paste the draft", _installed)
        except Exception as exc:  # never block the session on a config read
            logger.warning("[SESSION] commit word not applied: %s", exc)

        def _command_dispatch_fn(text: str) -> CommandDispatchResult:
            audio_duration = getattr(self, '_current_utterance_duration_s', 0.0)
            # SessionModeManager calls this only after committing an utterance
            # to either the legacy COMMAND lane or the curated exact-command
            # branch of the combined hands-free lane. The preference governing
            # opportunistic matching during ordinary dictation does not apply.
            dispatch = self.command_executor.process_text(
                text, self, force_commands=True,
            )
            result, was_command = dispatch
            # DispatchResult.state; a plain (result, bool) tuple from an
            # older/stub executor only knows claimed-or-not.
            state = getattr(getattr(dispatch, 'state', None), 'value', None) or (
                'completed' if was_command else 'miss')
            detail = getattr(dispatch, 'detail', None) or {}
            awaiting = bool(was_command and detail.get('awaiting_confirmation'))
            self._log_command_dispatch(text, result if was_command else None, state)
            if was_command:
                # Claimed, whatever the state: a failed or refused command is
                # still a command -- not a miss, never dictation. Held for a
                # yes/no is not carried out: nothing has run yet.
                carried_out = state in ('completed', 'queued', 'matched') and not awaiting
                _store_cmd = self.command_executor.commands.get(result) or {'type': 'plugin'}
                if (carried_out and result
                        and not _is_repeat_blacklisted(result, _store_cmd)
                        and self.command_executor.find_command(result) == result):
                    self._last_command = _store_cmd
                    self._last_command_name = result
                if carried_out and result:
                    increment_command_count(result)
                self.add_to_history(text, is_command=True)
                self._log_history(
                    raw_text=text,
                    duration_ms=int(audio_duration * 1000),
                    mode='command',
                    status='success' if carried_out else ('awaiting_confirmation' if awaiting else state),
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
                return CommandDispatchResult(matched=True, phrase=result, state=state,
                                             awaiting_confirmation=awaiting)

            disabled_pack = detail.get('pack') if detail.get('reason') == 'pack_disabled' else None
            if disabled_pack:
                logger.info(f'[CMD] "{text}" is a command in the disabled pack {disabled_pack!r}')
            else:
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
            return CommandDispatchResult(matched=False, phrase=None, disabled_pack=disabled_pack)

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
                # VERBATIM profile: replaces the whole pipeline below, since
                # capitalisation/cleanup/smart-corrections/auto-punctuation
                # are precisely what it exists to suppress.
                formatted = self._apply_verbatim_if_active(text)
                if formatted is None:
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

            paste_ok, delivery_confirmed = self._paste_preserving_clipboard(
                formatted,
                before_paste=commit_focus_guard,
                return_delivery_confirmation=True,
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
            return InjectionDelivery(formatted, confirmed=delivery_confirmed)

        def _remove_chars_fn(n: int) -> bool:
            # Queue 50 (ARC audit5b): was n x pyautogui.hotkey('shift','left')
            # + press('delete'). Measured (reports/50): pyautogui sends arrows
            # with scan code 0 and no extended-key flag and sleeps PAUSE=0.1 s
            # after every call, so 40 characters took 4.1 s and -- on a plain
            # text control and on a target that samples Shift late -- selected
            # nothing or deleted the wrong character. Paced Backspace carries
            # no modifier state and re-checks the target window between
            # batches: 40 characters in 0.14 s, exact on all three targets.
            verdict = injection_safety.window_integrity()
            if verdict.blocked:
                logger.warning('[SESSION] scratch-that refused: foreground window is elevated (%s)',
                               verdict.describe())
                return False
            start_hwnd = _get_foreground_hwnd()
            deleted, why = injection_safety.delete_backwards(
                n, still_target=lambda: start_hwnd is not None and _get_foreground_hwnd() == start_hwnd)
            if why != 'done':
                logger.warning('[SESSION] scratch-that stopped after %d of %d characters: %s',
                               deleted, n, why)
                return False
            return True

        _MODE_EARCONS = {
            SessionMode.COMMAND: 'mode_command',
            SessionMode.DICTATE: 'mode_dictate',
            SessionMode.AVA: 'mode_ava',
        }

        def _on_mode_change(mode: "SessionMode") -> None:
            # Queue 110. A mode switch is unambiguous user intent to move on,
            # so it cancels whatever Ava is still doing BEFORE the new mode's
            # earcon -- speech stopped, pending reply dropped, generation
            # bumped so a slow response cannot land afterwards, staged action
            # rejected, chipped. Gated on turn_is_live() so an ordinary switch
            # with nothing in flight costs nothing and leaves a staged
            # confirmation from some other subsystem alone.
            _cancel = getattr(self, '_cancel_ava_turn', None)
            if _cancel is not None:
                _cancel(f'mode switch to {getattr(mode, "value", mode)}')
            self.play_sound(_MODE_EARCONS.get(mode, 'mode_command'))
            self._update_mode_overlay(mode)
            self._update_streaming_preview(mode)
            if mode is SessionMode.AVA:
                # Shown after this utterance's own outcome chip ("-> AVA"),
                # which would otherwise replace it at once -- see
                # _handle_session_dispatch_outcome.
                self._ava_readiness_chip_due = True

        try:
            from samsara import ava_readiness
            ava_readiness.tracker.add_listener(self._on_ava_readiness_change)
        except Exception as exc:
            logger.debug(f'[AVA-READY] listener registration failed: {exc}')

        def _on_focus_lock_revert() -> None:
            logger.info('[SESSION] Focus-lock mismatch -- foreground window changed; '
                        'suppressing injection and retaining DICTATE mode')
            self.play_sound('focus_lock_revert')
            self._update_mode_overlay(SessionMode.DICTATE)

        def _on_scratch_result(success: bool) -> None:
            self.play_sound('scratch_success' if success else 'scratch_refuse')

        def _on_abort() -> None:
            logger.info('[SESSION] Global abort phrase -- exiting command mode')
            # Queue 110: an abort phrase ("cancel") and a sleep phrase both
            # land here, and both must reach an Ava turn that is still
            # talking. Only "stop" did before, and only in the AVA lane
            # (_try_stop_utterance).
            _cancel = getattr(self, '_cancel_ava_turn', None)
            if _cancel is not None:
                _cancel('abort phrase')
            self.exit_command_mode()

        def _on_switch_dispatch_error(exc: Exception) -> None:
            # Shared by a failed prefix-switch payload (mode reverted), a failed
            # DICTATE commit (draft retained) and failing mode-change side
            # effects (mode kept) -- session_modes logs which one it was.
            logger.error(f'[SESSION] Session action failed: {exc}')
            self.play_sound('error')

        def _session_stop(reason: str) -> dict:
            """Queue 116: the spoken emergency stop, and the stop half of a
            sleep phrase. This is the callable session_modes has accepted as
            `stop_fn` since it was written and NEVER been passed -- which made
            session_modes' stop branch dead in the shipped app, and made
            sleep's "the stop runs FIRST" guarantee false.

            execution_policy.stop_all bumps the execution generation first, so
            anything mid-model-call is stale before anything else is touched,
            then cancels the answer being spoken (queue 110), the staged
            confirmation, both Ava queues and the schedule. Drafts, mode and
            microphone are deliberately untouched: a stop is not an abort and
            not a sleep.

            chip=False: the DispatchOutcome(kind="stopped") chip says WHAT was
            halted, which the generic "stopped" chip would replace with less.
            The earcon fires only when something really was in flight, so a
            stop that caught nothing does not sound like a success.
            """
            from samsara import execution_policy  # noqa: PLC0415
            cleared = execution_policy.stop_all(
                self, f'voice stop ({reason})', chip=False) or {}
            try:
                if any(cleared.get(k) for k in
                       ('speech', 'in_flight', 'pending', 'queued', 'schedule')):
                    self.play_sound('stop')
            except Exception as exc:
                logger.debug(f'[SESSION] stop earcon failed: {exc}')
            return cleared

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

        # Exact-phrase Ava entry list -- config-file-editable only this pass
        # (no settings UI; see config_schema.py's "ava_invocations" entry).
        configured_ava_invocations = resolve_ava_invocations(self.config)

        def _ava_ready_probe():
            """Cheap "can Ava answer right now?" check, consulted before any
            switch into AVA (2026-09-11). Returns None when ready, or a
            human-readable reason string that the session logs at WARNING and
            earcons -- so "Ava is switched off" never again looks identical to
            "Ava didn't hear me". Deliberately does NOT probe the network:
            this runs on the switch utterance, and a dead Ollama host would
            otherwise stall the mode change behind a socket timeout. The
            per-utterance dispatch path already reports an unreachable host."""
            try:
                from plugins.commands.ask_ollama import ava_entry_block_reason
            except Exception as exc:
                return f"the Ava plugin could not be imported ({exc})"
            try:
                # Queue 57: also refuses when the cached readiness of the
                # configured provider (DeepSeek, Ollama, ...) is offline.
                # Still no network here -- samsara/ava_readiness.py probes in
                # the background and this reads its snapshot.
                return ava_entry_block_reason(self)
            except Exception as exc:
                return f"the Ava plugin readiness check failed ({exc})"

        self._session_mode_manager = SessionModeManager(
            abort_phrases=abort_phrases,
            ava_invocations=configured_ava_invocations,
            ava_ready_probe_fn=_ava_ready_probe,
            foreground_exe_resolver=_get_foreground_exe_lower,
            foreground_hwnd_resolver=_get_foreground_hwnd,
            inject_fn=_inject_fn,
            format_dictate_fn=self._apply_formatting_tokens,
            remove_chars_fn=_remove_chars_fn,
            window_integrity_fn=injection_safety.window_integrity,
            command_dispatch_fn=_command_dispatch_fn,
            agent_dispatch_fn=self._ava_session_agent_dispatch_fn,
            on_mode_change=_on_mode_change,
            on_focus_lock_revert=_on_focus_lock_revert,
            on_scratch_result=_on_scratch_result,
            on_abort=_on_abort,
            on_switch_dispatch_error=_on_switch_dispatch_error,
            buffer_dictate_until_commit=True,
            commit_redecode_fn=self._dictate_commit_redecode,
            hands_free_command_probe_fn=getattr(
                self, '_probe_hands_free_command', None,
            ),
            pending_action_scratch_fn=self._pop_pending_action_for_scratch,
            # Queue 69: a complete-utterance "no" / "cancel" / "stop" (or a
            # number for a choice) answers an open cancel window before any
            # lane reads the words. Returns None when no window is open, so
            # with the window off every utterance goes where it went before.
            pending_reply_fn=self._answer_cancel_window,
            cancel_window_fn=self._stage_hands_free_cancel_window,
            on_deferred_outcome=self._handle_deferred_session_outcome,
            # command_mode.abort_phrases: user-added whole-utterance exits
            # that behave like the built-in sleep phrases (draft retained).
            extra_sleep_phrases=_config_phrase_list(
                self.config.get('command_mode', {}).get('abort_phrases', [])),
            # Queue 116: the emergency stop. stop_phrases REPLACES the
            # built-in list (session_modes falls back to it when the config
            # value is empty or unusable), so a word can be changed and not
            # only added.
            stop_fn=_session_stop,
            stop_phrases=_config_phrase_list(
                self.config.get('command_mode', {}).get('stop_phrases', [])),
            # Queue 80: "scratch everything" asks out loud before discarding.
            speak_fn=self._speak_session_notice,
        )
        return self._session_mode_manager

    def _speak_session_notice(self, text: str, category: str = "confirmation") -> None:
        """Queue 80: session questions and their results, spoken. Category
        "confirmation" is exempt from command_mode.tts_char_limit (queue 58).
        Never raises: speech must not break dispatch."""
        try:
            coordinator = getattr(self, 'audio_coordinator', None)
            if coordinator is not None:
                coordinator.speak(text, category=category)
            else:
                logger.warning(f'[SESSION] no audio coordinator; not spoken: {text!r}')
        except Exception as exc:
            logger.warning(f'[SESSION] session notice not spoken ({exc}): {text!r}')

    def clear_dictation_draft(self):
        """builtin.scratch_everything outside the hands-free dictation lane
        (e.g. command mode). The lane itself intercepts the phrase and asks
        first; here execution_policy has already asked, because the method is
        rated destructive (_METHOD_RISK). Discards the staged draft only --
        nothing already pasted is touched."""
        manager = getattr(self, '_session_mode_manager', None)
        if manager is None or not manager.dictate_pending_buffer.strip():
            self._speak_session_notice("There is nothing staged to clear.")
            return False
        manager.clear_pending_draft()
        self._speak_session_notice("Draft cleared.")
        return True

    def _answer_cancel_window(self, text: str):
        try:
            from samsara import execution_policy as _policy
            return _policy.answer_cancel_window(self, text)
        except Exception as exc:
            logger.debug(f'[SESSION] cancel window reply unavailable: {exc}')
            return None

    def _log_command_dispatch(self, utterance: str, phrase, state: str) -> None:
        """[CMD-DISPATCH]: one INFO line per hands-free command dispatch, with
        the session mode, the registry phrase, its catalog canonical id and
        the real state. 2026-09-14 22:35: "show windows" logged only
        outcome=command_executed state=queued -- it was actually held by the
        execution policy for a confirmation whose spoken prompt was
        suppressed by command_mode.tts_char_limit, and nothing ran. Logging
        must never break dispatch. (Queue 58: confirmation questions are now
        exempt from that limit, and a held command reports
        command_awaiting_confirmation.)"""
        try:
            manager = self._session_mode_manager
            mode = manager.mode.value if manager is not None else 'no_session'
            canonical = self._command_canonical_id(phrase) if phrase else None
            awaiting = False
            if state == 'queued' and phrase:
                from samsara.execution_policy import pending_operation
                op = pending_operation()
                inv = getattr(op, 'invocation', None)
                if op is not None and getattr(inv, 'command_id', None) == phrase:
                    awaiting = True
            logger.info(
                f'[CMD-DISPATCH] mode={mode} utterance={utterance!r} resolved={phrase!r} '
                f'canonical={canonical} state={state} awaiting_confirmation={awaiting}'
            )
        except Exception as exc:
            logger.debug(f'[CMD-DISPATCH] logging failed: {exc}')

    def _command_canonical_id(self, phrase: str):
        """Registry phrase -> commands_catalog.json canonical_id (e.g. "show
        windows" -> "window_switcher.show_windows"), or None. Built once from
        the catalog file; aliases map to their record's id."""
        table = getattr(self, '_command_canonical_ids', None)
        if table is None:
            table = {}
            try:
                from samsara.command_catalog import load_catalog_json, normalize_phrase
                for record in load_catalog_json() or []:
                    cid = record.get('canonical_id')
                    for name in [record.get('phrase'), *(record.get('aliases') or [])]:
                        if name and cid:
                            table.setdefault(normalize_phrase(name), cid)
            except Exception as exc:
                logger.debug(f'[CMD-DISPATCH] catalog unavailable for canonical ids: {exc}')
            self._command_canonical_ids = table
        from samsara.command_catalog import normalize_phrase
        return table.get(normalize_phrase(str(phrase)))

    def _dictate_commit_redecode(self, joined_text: str, audio_refs: list):
        """Re-decode a complete DICTATE thought from collected per-utterance audio."""
        cm_cfg = self.config.get("command_mode", {})
        if not isinstance(cm_cfg, dict):
            cm_cfg = {}

        if not cm_cfg.get("dictate_commit_redecode", True):
            logger.info(
                "[SESSION] Skipping commit re-decode because command_mode.dictate_commit_redecode is False",
            )
            return None

        refs = [np.asarray(ref, dtype=np.float32) for ref in audio_refs if ref is not None]
        if not refs:
            return None

        # Each float32 mono stream is model-rate (16 kHz), so one second is
        # about 64 KB — this 120 s default cap keeps a worst-case staged
        # thought under ~7.7 MB for a single re-decode.
        total_s = (sum(len(r) for r in refs) / float(self.model_rate)) if self.model_rate else 0.0
        try:
            max_s = float(cm_cfg.get("dictate_commit_redecode_max_s", 120.0))
        except (TypeError, ValueError):
            max_s = 120.0

        if max_s and max_s > 0 and total_s > max_s:
            logger.info(
                "[SESSION] Skipping commit re-decode because stitched duration %.2fs exceeds "
                "configured cap %.2fs",
                total_s,
                max_s,
            )
            return None

        concatenated_audio = np.concatenate(refs) if len(refs) > 1 else refs[0]
        params = self.get_transcription_params(include_vocabulary=False)
        params["language"] = "en"
        params["vad_filter"] = False
        # 02e00b9: long-lived DICTATE commits must not carry a prompt.
        params["initial_prompt"] = None

        with self.model_lock:
            segments, info = self.model.transcribe(concatenated_audio, **params)
        segments = list(segments)
        text = ''.join(getattr(seg, 'text', '') for seg in segments).strip()
        text = self.voice_training_window.apply_corrections(text)
        if not text:
            return None

        kept_segments = _drop_trailing_garbage_segments(segments)
        text = _trim_trailing_garbage_run(text)
        if not text:
            logger.info(f'[GUARD] Suppressed commit re-decode: {text!r}')
            return None

        if _is_hallucinated_segments(kept_segments, text):
            logger.info(f'[GUARD] Suppressed commit re-decode hallucination: {text!r}')
            return None

        if not self._filter_dictation_language(text, info):
            return None
        return text

    def _pop_pending_action_for_scratch(self) -> "bool | None":
        """Unified 'scratch that' stage 1 (Ava Front Door spec v2,
        "Confirmation binding"): if a staged action is pending
        (ask_ollama._pending_action, shared by D1's model-derived actions
        and D3's waterfall-staged ones), clear it and report success.
        Returns None when nothing is pending, signaling the caller
        (SessionModeManager.dispatch_utterance, or D3's own
        _handle_unified_scratch_that) to fall through to the per-session
        dictation-commit stack instead. Pure state mutation, no
        sound/speech here -- both callers already play the SAME
        scratch_success/scratch_refuse earcon from their own outcome
        handling, so this stays a single source of truth without
        duplicating feedback."""
        from plugins.commands import ask_ollama  # noqa: PLC0415
        if ask_ollama.get_pending_action() is None:
            return None
        ask_ollama.clear_pending_action()
        return True

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
        # Stop path independent of understanding (execution_policy.stop_all):
        # an exact stop/cancel utterance never queues behind the model.
        if self._try_stop_utterance(text, 'ava session'):
            return
        # Queue 102 -- Ava's edit proposals. THE ONE dispatch call site this
        # feature touches. It sits here, and not in session_modes' dispatch,
        # deliberately: this is already the Ava address path, so the edit
        # feature inherits the trigger rather than inventing one. By the time
        # an utterance reaches this function it has been through the substance
        # gate and detect_stage_reference has already decided whether it is
        # talking about the staged dictation -- which is exactly the question
        # "make that shorter" asks.
        #
        # Placed AFTER cancel and stop so neither can be swallowed by a staged
        # proposal, and BEFORE the agent queue so an edit never costs an Ava
        # request. Returns False for anything it does not claim, and its own
        # errors are caught inside, so a bug in the edit path can only ever
        # cost the user an edit -- never their Ava turn.
        # getattr, not a bare attribute call: this function is exercised with
        # app harnesses that implement only the surface under test (see
        # tests/test_execution_policy.py::TestStopPath), and an AttributeError
        # on the LOOKUP happens before _try_ava_edit's own guard can catch it.
        # Same defensive shape queue 110 uses for _cancel_ava_turn just below.
        _try_edit = getattr(self, '_try_ava_edit', None)
        if _try_edit is not None and _try_edit(text):
            return
        payload_text = f"STAGED TEXT:\n{context}\n\n{text}" if context else text
        # Request identity: captured at the moment the user spoke. Queue
        # items are (generation, text), never bare strings -- see
        # _on_ava_session_request_done, which skips stale ones.
        from samsara import execution_policy  # noqa: PLC0415
        request = (execution_policy.current_generation(self), payload_text)

        with self._ava_session_dispatch_lock:
            if self._ava_session_request_in_flight:
                if len(self._ava_session_dispatch_queue) >= self._ava_session_dispatch_queue.maxlen:
                    logger.info('[AVA-SESSION] Queue full (3) -- dropping oldest queued utterance')
                    # No existing queue-warning earcon in this codebase --
                    # reuse scratch_refuse (Phase 1's "this didn't go
                    # through" sound) rather than adding a new asset.
                    self.play_sound('scratch_refuse')
                self._ava_session_dispatch_queue.append(request)
                return
            self._ava_session_request_in_flight = True

        self._start_ava_session_worker(request)

    def _start_ava_session_worker(self, request) -> None:
        from plugins.commands.ask_ollama import handle_ask_ava
        generation, payload_text = request if isinstance(request, tuple) else (None, request)
        handle_ask_ava(self, remainder=payload_text, on_done=self._on_ava_session_request_done,
                       generation=generation)

    def _try_ava_edit(self, text: str) -> bool:
        """Queue 102. Hand the utterance to samsara.ava_edit; True means the
        edit path consumed it and it must NOT go to the agent.

        Everything the feature does lives behind this one call -- proposing,
        the staged-proposal slot, the chip, the spoken diff, and the apply.
        Import is local so a broken/absent edit package cannot stop the app
        from booting or take the Ava lane down with it."""
        try:
            from samsara import ava_edit  # noqa: PLC0415
            return ava_edit.handle_utterance(self, text)
        except Exception as exc:
            logger.debug(f'[AVA-EDIT] edit path unavailable: {exc}')
            return False

    def _cancel_ava_turn(self, reason: str):
        """Queue 110. Cancel an Ava turn that is still live, from a trigger
        that is not a spoken stop: a mode switch, an abort/sleep phrase.

        Delegates to plugins.commands.ask_ollama.cancel_turn, which is the
        one seam -- it decides whether anything is live and does the whole
        cancellation through execution_policy.stop_all. Returns None when
        there was nothing to cancel. Never raises: a mode change must happen
        even if the Ava plugin is missing or broken.
        """
        try:
            from plugins.commands.ask_ollama import cancel_turn  # noqa: PLC0415
            return cancel_turn(self, reason)
        except Exception as exc:
            logger.debug(f'[AVA-SESSION] cancel_turn unavailable ({reason}): {exc}')
            return None

    def _try_stop_utterance(self, text: str, lane: str) -> bool:
        """Exact "stop"/"cancel"/"never mind"/"go to sleep" spoken into an
        Ava lane: invalidate every earlier request right now (generation
        bump + queues + pending + schedule), keep drafts, say so. Runs
        BEFORE anything is queued behind the inference worker."""
        from samsara import execution_policy  # noqa: PLC0415
        if not execution_policy.is_stop_utterance(text):
            return False
        cleared = execution_policy.stop_all(self, f'voice stop ({lane})')
        try:
            if cleared["pending"] or cleared["schedule"] or cleared["queued"] or cleared["in_flight"]:
                self.play_sound('stop')
        except Exception as exc:
            logger.debug(f"[POLICY] stop earcon failed: {exc}")
        return True

    def _on_ava_session_request_done(self) -> None:
        """handle_ask_ava's on_done hook -- fires exactly once per request
        (early-exit or full worker cycle). Drains the next queued utterance
        if any, else clears the in-flight flag.

        Also signals the session-activity chokepoint. Note that this call
        does NOT extend the session: since the 2026-09-10 session policy,
        _touch_session_activity() only re-arms the inactivity timer for a
        fresh Silero speech onset (speech_onset=True), and agent completion
        is explicitly not one -- see its docstring and
        docs/HANDS_FREE_GATES_FINDINGS.md "Session policy". The call is kept
        so every lane still reports through the one chokepoint; a slow agent
        response is covered by the user speaking again, not by this hook."""
        self._touch_session_activity()
        # Resolves the pending "Ava..." chip with what actually happened
        # (queue 57): handle_ask_ava leaves (label, kind) on
        # self._ava_turn_outcome -- a failure, an unspoken answer and a
        # cancelled request are no longer shown as "Ava <check>".
        _chip = getattr(self, '_show_outcome_chip', None)
        turn_chip = getattr(self, '_ava_turn_outcome', None)
        self._ava_turn_outcome = None
        if turn_chip is None:
            # No recorded outcome = the request was dropped (cancel/stop made
            # it stale). The pending "Ava..." chip has no TTL, so resolve it.
            turn_chip = ("Ava: cancelled", "accent")
        if _chip is not None:
            _chip(*turn_chip)
        from samsara import execution_policy  # noqa: PLC0415
        with self._ava_session_dispatch_lock:
            next_request = None
            while self._ava_session_dispatch_queue:
                candidate = self._ava_session_dispatch_queue.popleft()
                gen = candidate[0] if isinstance(candidate, tuple) else None
                if execution_policy.is_current(self, gen):
                    next_request = candidate
                    break
                logger.info('[AVA-SESSION] Dropping stale queued request (gen %s)', gen)
            if next_request is None:
                self._ava_session_request_in_flight = False
                return
        self._start_ava_session_worker(next_request)

    # Session mode badge (COMMAND/DICTATE/AVA) accent colors. Lives on the
    # listening indicator pill -- NEVER on samsara.ui.status_overlay (the
    # Reminders & Alarms window). That window must never be shown, hidden,
    # or otherwise touched by session code; an earlier version wired the
    # badge there and every mode transition popped/hid the user's Reminders
    # window as a side effect.
    @staticmethod
    def _mode_overlay():
        """Badge label and colour per session mode.

        A method, not a class attribute: a dict built when the class body runs
        reads each token once, at import, and would keep the startup palette
        for the life of the process (queue 129)."""
        from samsara.ui import theme  # noqa: PLC0415
        return {
            SessionMode.COMMAND: ("COMMAND", theme.ACCENT),
            SessionMode.DICTATE: ("HANDS FREE", theme.WARNING),
            SessionMode.AVA: ("AVA", theme.AVA),
        }

    def _update_mode_overlay(self, mode: "SessionMode") -> None:
        from samsara.ui import theme  # noqa: PLC0415
        name, color = self._mode_overlay().get(mode, ("COMMAND", theme.ACCENT))
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_session_mode, name, color)

    def enter_command_mode(self):
        """Enter command mode (idempotent). Safe to call from any thread.

        Exclusive voice-mode ownership (2026-07-19 incident, report §
        "Asymmetric overlap"; carried over into D3 per Ava Front Door spec
        v2's "2026-07-19 exclusive-ownership + generation guards carry
        over"): if the Ava command session (D3) is active, exit it first
        -- with its own normal exit feedback -- before proceeding. A
        different mode's activation being pressed is unambiguous user
        intent; rejecting it would have left the incident's AI-command
        latch in place instead of resolving it. Called unlocked, before
        acquiring _command_mode_lock, so exit_ava_command_session's own
        locking/async work never nests under this lock."""
        if self.ava_command_session_active:
            self.exit_ava_command_session()
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
            # BUG FIX (2026-07-18): the WakeConsumer pipeline was previously
            # assumed already running (see the now-corrected comment in
            # _do_enter_command_mode) -- true only when wake_word_enabled,
            # since it's the only thing that otherwise starts the consumer
            # (dictation.py boot, ~4407). With wake word off, entering a
            # toggle session armed command_mode_active but nothing was
            # polling the ring, so the session was silently deaf. Ensuring
            # here (synchronously, before the worker thread's debounce
            # sleep -- not deferred to _do_enter_command_mode) guarantees
            # the pipeline is running the instant this call returns, and
            # does NOT enable wake-phrase detection itself (wake_word_active
            # stays whatever it already was) -- see _ensure_wake_consumer.
            self._ensure_wake_consumer('toggle_session')
            if hasattr(self, 'listening_indicator'):
                # Force-visible for the session's duration regardless of
                # listening_indicator_enabled -- restored to the
                # config-controlled state in exit_command_mode().
                self._schedule_ui(self.listening_indicator.show)
            self._update_mode_overlay(SessionMode.DICTATE)
            # reset(initial_mode=DICTATE) above sets .mode directly, bypassing
            # _switch_mode -- on_mode_change (which _update_streaming_preview
            # is wired into) never fires for this initial lane. Same reason
            # _update_mode_overlay needs its own explicit call just above.
            self._update_streaming_preview(SessionMode.DICTATE)
        thread_registry.spawn('cmd-mode-enter', self._do_enter_command_mode, daemon=True)

    def _do_enter_command_mode(self):
        """Worker thread: arms command mode and fires debounced earcon."""
        cfg = self.config.get('command_mode', {})
        debounce_ms = cfg.get('enter_debounce_ms', 200)
        if cfg.get('mode', 'hold') == 'toggle':
            # Toggle (sustained) mode: per-utterance dispatch via WakeConsumer VAD.
            # Do NOT call start_recording() — that activates the dictation consumer
            # (accumulate-until-release) and sets _hotkey_recording=True, which
            # suppresses the WakeConsumer. The WakeConsumer pipeline is ENSURED
            # running by enter_command_mode() (_ensure_wake_consumer), synchronously,
            # before this worker thread was even spawned -- not merely assumed
            # to already be running from wake_word_enabled (that assumption was
            # the 2026-07-18 toggle-deaf-with-wake-off bug). _is_toggle_cmd()
            # makes the (now-guaranteed-running) consumer service frames while
            # command_mode_active.
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

    def _dispatch_session_transition(self, action) -> None:
        """Run enter/exit_command_mode on a worker, never inline in the
        low-level keyboard hook callback.

        The 2026-07-28 and 2026-08-02 freezes (watchdog stack dumps,
        proof_home/freeze_stacks.txt) both show the pynput WH_KEYBOARD_LL
        hook thread wedged for minutes inside exit_command_mode -- the
        hook must return fast, and the teardown does Qt signal emits,
        locking, and audio work. Worse, typed injection (25bc7ee) sends
        synthetic input whose delivery synchronizes with the very hook
        chain this callback is blocking: a commit's SendInput in flight
        plus an exit hotkey is an AB-BA deadlock. One transition may be
        in flight at a time; extra presses during a transition are
        dropped (idempotent -- the user is mashing the same intent).
        """
        lock = self._session_transition_lock
        # acquire(blocking=False) test-and-set is atomic; the previous
        # getattr-then-assign pair let two near-simultaneous hotkey presses
        # both observe "not in flight" and both spawn a worker.
        if not lock.acquire(blocking=False):
            logger.debug('[SESSION] transition already in flight; drop')
            return

        def _run():
            try:
                action()
            except Exception:
                logger.exception('[SESSION] transition failed')
            finally:
                lock.release()

        from samsara.runtime import thread_registry
        thread_registry.spawn('session.transition', _run, daemon=True)

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
        # Session exit is a cancellation: every Ava request captured during
        # this session (queued, in flight, or staged for "yes") is now stale
        # and Denied at the execution-policy choke point. Drafts untouched.
        try:
            from samsara import execution_policy  # noqa: PLC0415
            execution_policy.stop_all(self, 'session exit', chip=False)
        except Exception as exc:
            logger.debug(f"[POLICY] stop_all on session exit failed: {exc}")
        is_toggle_session = self.config.get('command_mode', {}).get('mode', 'hold') == 'toggle'
        try:
            # Session end discards all state. A future toggle entry chooses
            # the combined hands-free lane; reset's COMMAND default remains
            # for legacy direct callers and the optional command-only lane.
            if self._session_mode_manager is not None:
                self._session_mode_manager.reset()
            if is_toggle_session:
                # Release this session's hold on the WakeConsumer pipeline
                # (see enter_command_mode's _ensure_wake_consumer). If
                # wake_word_enabled is separately holding it open, the
                # pipeline correctly keeps running for wake detection --
                # reason-counted, not a boolean, so this can't stop it out
                # from under wake mode.
                self._release_wake_consumer('toggle_session')
        finally:
            # reset() above bypasses on_mode_change (see enter_command_mode's
            # own explicit call), so a session ending while still in DICTATE
            # would otherwise leak a running preview overlay/thread.
            # Unconditional AND in `finally` (2026-07-19 dogfooding fix): every
            # session-exit path -- toggle-off key, inactivity timeout, global
            # abort phrase, WakeConsumer poll-loop crash -- funnels through
            # this one method, and each of THOSE callers either swallows an
            # exception from here (wake_consumer.py's crash handler: bare
            # `except: pass`) or, on inactivity timeout, force-clears
            # command_mode_active in its own except-fallback WITHOUT its own
            # release call (_on_command_mode_inactivity). Previously, if
            # reset() or _release_wake_consumer() above raised, the overlay
            # release below was skipped entirely and none of those callers
            # made it up for it -- the DICTATE-lane overlay would keep running
            # after the session had already ended. A no-op when nothing was
            # ever started (config off, or session ended from COMMAND/AVA).
            self._release_streaming_preview()
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
        """Enter Ava mode (idempotent). Safe to call from any thread.

        Exclusive voice-mode ownership (2026-07-19 incident root cause;
        carried over into D3 per Ava Front Door spec v2): this guard
        previously omitted the AI-command boolean, so Ava could enter ON
        TOP of a latched AI-command session (the incident's exact path --
        Right-Alt's clean 157ms ghost-tap exit then cleaned up only Ava,
        leaving the other session latched and nagging). If the Ava
        command session (D3) is active, exit it first -- with its own
        normal exit feedback -- before proceeding with Ava entry. Called
        unlocked, before acquiring _ava_mode_lock, so
        exit_ava_command_session's own locking/async work never nests
        under this lock."""
        if self.ava_command_session_active:
            self.exit_ava_command_session()
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
    # Ava command session (D3, Ava Front Door spec v2) -- replaces the
    # old "AI command mode" (samsara/ai_command_mode.py, deleted).
    # ------------------------------------------------------------------

    def enter_ava_command_session(self):
        """Enter the Ava command session (idempotent, toggle). Safe from any thread.

        Authoritative-exit plumbing (2026-07-19 incident, report items
        3/5/7/8, Fix 2 / P0b -- carried over verbatim per spec):
          - Bumps _ava_cmd_generation. Async work in flight from a PRIOR
            session (ready-wait, transcription, resolution, waterfall
            stages, TTS) checks this and drops silently once it's stale --
            see _handle_ava_command_utterance and
            ava_command_session.py's _process_utterance/_speak.
          - reset_cancel() clears the cancel flag exit_ava_command_session()
            now leaves SET (see that method) -- a fresh entry is the only
            thing that re-arms the worker.
          - _ensure_wake_consumer('ava_command_session') takes a
            reason-counted lease on the WakeConsumer pipeline (same
            mechanism as the toggle hands-free session), so entering
            while wake-word is off doesn't leave the session latched but
            deaf.
          - Arms the spec-new 60s inactivity timer (config
            ava_command_session.inactivity_timeout_s).
        """
        with self._ava_cmd_mode_lock:
            if self.ava_command_session_active:
                return
            if self.command_mode_active or self.ava_mode_active:
                return
            self.ava_command_session_active = True
            self._ava_cmd_generation += 1
        self._ava_cmd_miss_count = 0
        self._ava_cmd_ready.clear()  # Mic gate: unblocks only after cue finishes
        try:
            from samsara.ava_command_session import reset_cancel  # noqa: PLC0415
            reset_cancel()
        except Exception as e:
            logger.debug(f"[AVA-CMD] reset_cancel on enter failed: {e}")
        self._ensure_wake_consumer('ava_command_session')
        cfg = self.config.get('ava_command_session', {})
        timeout_s = cfg.get('inactivity_timeout_s', 60)
        self._reset_ava_cmd_inactivity_timer(timeout_s)
        logger.info("[AVA-CMD] Entering Ava command session")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, True)
            # Distinct visible badge (spec D3 "LATCH FEEDBACK"): the same
            # generic set_command_mode(True) pill also lights up for
            # command_mode/ava_mode -- set_session_mode gives this
            # session its OWN label/color so it is not confusable with
            # either, reusing the existing COMMAND/DICTATE/AVA badge
            # mechanism (session_modes.py's hands-free lane already uses
            # it the same way).
            from samsara.ui import theme  # noqa: PLC0415
            self._schedule_ui(self.listening_indicator.set_session_mode, "AVA CMD", theme.ACCENT)
        thread_registry.spawn('ava-cmd-enter', self._do_enter_ava_command_session, daemon=True)

    def _do_enter_ava_command_session(self):
        """Worker: play entry earcon, warm up model, play ready cue, then arm mic."""
        debounce_ms = self.config.get('command_mode', {}).get('enter_debounce_ms', 200)
        time.sleep(debounce_ms / 1000.0)
        if not self.ava_command_session_active:
            self._ava_cmd_ready.set()
            return
        # Distinct earcon from ava_mode/command_mode's shared 'start' sound
        # (spec D3 "LATCH FEEDBACK"; falls back to 'start' harmlessly via
        # play_sound's own missing-asset handling if the theme lacks it).
        self.play_sound('mode_command', use_winsound=True)
        ava_cfg = self.config.get('ava_command_session', {})

        def _on_ready():
            from samsara.ava_command_session import _play_ready_cue  # noqa: PLC0415
            _play_ready_cue(self)
            self._ava_cmd_ready.set()

        if ava_cfg.get('keep_warm', True):
            try:
                from samsara.ava_command_session import warm_up  # noqa: PLC0415
                warm_up(self, on_done=_on_ready)
            except Exception:
                self._ava_cmd_ready.set()
        else:
            _on_ready()

    def exit_ava_command_session(self):
        """Exit the Ava command session (idempotent). Drains queue. Safe from any thread.

        cancel_queue() leaves the module's _cancel event SET -- deliberately
        NOT immediately reset here (2026-07-19 incident report item 8: the
        old immediate reset_cancel() left only a brief window where the
        worker's dequeue-time check could actually catch a stale item, so
        an in-flight resolver call or waterfall stage could keep running
        after a legitimate exit). The cancel flag now stays set for the
        entire time the session is inactive; only
        enter_ava_command_session()'s reset_cancel() re-arms the worker
        for a fresh session. _ava_cmd_generation is bumped here too, so
        async work outside the queue (already past dequeue, already
        resolving/executing) also observes the exit -- see
        enter_ava_command_session's docstring for the full mechanism.
        """
        with self._ava_cmd_mode_lock:
            if not self.ava_command_session_active:
                return
            self.ava_command_session_active = False
            self._ava_cmd_generation += 1
        self._ava_cmd_miss_count = 0
        self._ava_cmd_ready.set()  # Unblock utterance gate if cue is still playing
        self._cancel_ava_cmd_inactivity_timer()
        logger.info("[AVA-CMD] Exiting Ava command session")
        # Exit is a cancellation for everything staged/queued/in flight
        # under the old generation (pending confirmation, scheduler, AVA
        # queue). The generation was bumped above under the lock, so only
        # the clearing half runs here. Drafts are untouched.
        try:
            from samsara import execution_policy  # noqa: PLC0415
            cleared = execution_policy.stop_all(self, 'ava command session exit',
                                                chip=False, bump=False)
            # Queue 110: chip=False stays (the generic "stopped" chip is not
            # what leaving a session means), but a turn that was actually cut
            # off mid-answer has to SAY so -- the exit earcon alone reads as a
            # normal exit. Only when something was really in flight.
            if cleared.get("speech") or cleared.get("in_flight") or cleared.get("queued"):
                show = getattr(self, '_show_outcome_chip', None)
                if show is not None:
                    self._schedule_ui(show, "Ava: cancelled", "accent")
        except Exception as e:
            logger.debug(f"[AVA-CMD] stop_all on exit failed: {e}")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_command_mode, False)
            self._schedule_ui(self.listening_indicator.set_session_mode, None, None)
        try:
            from samsara.ava_command_session import cancel_queue  # noqa: PLC0415
            cancel_queue()
        except Exception as e:
            logger.debug(f"[AVA-CMD] Queue cancel on exit failed: {e}")
        self._release_wake_consumer('ava_command_session')
        self.play_sound('stop')

    def _reset_ava_cmd_inactivity_timer(self, timeout_s):
        """Spec D3 "Timeouts": inactivity exit after timeout_s of silence
        (config ava_command_session.inactivity_timeout_s, default 60).
        Re-armed on entry and on every flushed utterance (see
        _handle_ava_command_utterance) -- mirrors the existing hands-free
        session's _reset_command_mode_inactivity_timer pattern."""
        with self._ava_cmd_timer_lock:
            self._cancel_ava_cmd_inactivity_timer_locked()
            t = thread_registry.timer(
                "dictation.ava_cmd_inactivity", timeout_s,
                self._on_ava_cmd_inactivity, daemon=True)
            self._ava_cmd_inactivity_timer = t

    def _cancel_ava_cmd_inactivity_timer(self):
        with self._ava_cmd_timer_lock:
            self._cancel_ava_cmd_inactivity_timer_locked()

    def _cancel_ava_cmd_inactivity_timer_locked(self):
        t = self._ava_cmd_inactivity_timer
        if t is not None:
            t.cancel()
            self._ava_cmd_inactivity_timer = None

    def _on_ava_cmd_inactivity(self):
        """threading.Timer callback -- runs on its own thread. Must never
        raise uncaught: a raise here would leave the session latched but
        with its only path back to normal listening broken -- deaf but
        latched, a zombie session (same failure mode the hands-free
        session's own inactivity callback guards against)."""
        try:
            logger.info("[AVA-CMD] Inactivity timeout -- exiting")
            self.exit_ava_command_session()
        except Exception as e:
            logger.exception(f"[AVA-CMD] Inactivity exit failed, forcing end-state: {e}")
            self.ava_command_session_active = False
            self._cancel_ava_cmd_inactivity_timer()

    def _handle_unified_scratch_that(self) -> None:
        """Single 'scratch that' definition across every door (Ava Front
        Door spec v2's "Confirmation binding" section): pops the UNIFIED
        stack top -- the most recently staged action if one is pending
        (ask_ollama._pending_action, shared by D1's model-derived actions
        and D3's waterfall-staged ones via ava_command_session._dispatch_
        action2/handle_response), else the last dictation commit
        (session_modes.py's existing per-hands-free-session
        UnitOfWorkStack). Exactly one of the two fires per call; never
        both. Reuses the SAME scratch_success/scratch_refuse earcon
        convention _ensure_session_mode_manager's own on_scratch_result
        callback already uses, so both paths give identical feedback."""
        if self._pop_pending_action_for_scratch() is not None:
            self.play_sound('scratch_success')
            return
        manager = getattr(self, '_session_mode_manager', None)
        if manager is not None:
            ok = manager._do_scratch_that()
            self.play_sound('scratch_success' if ok else 'scratch_refuse')
            return
        self.play_sound('scratch_refuse')

    def _handle_ava_command_utterance(self, buffer: list, src_rate: int) -> None:
        """Transcribe one VAD-gated utterance and push to the Ava command
        session's waterfall queue.

        Called from WakeConsumer._flush() while ava_command_session_active.
        Owns an Ava-lane token; model_lock serializes cross-lane decodes.
        Abort phrases, and "scratch
        that", are both checked before enqueue so they're always
        responsive even while the waterfall queue is backed up.

        Session-generation guard (2026-07-19 incident report items 7-8,
        carried over verbatim per spec): captures the generation active
        when this utterance was flushed, and rechecks it after the ready
        wait and again after transcription -- both are places a
        legitimate exit_ava_command_session() (or exit + re-entry) could
        happen while this coroutine was blocked or working, and nothing
        previously rechecked ava_command_session_active before enqueueing
        or speaking. A stale generation drops the utterance silently
        (debug log only). The captured generation is threaded through
        enqueue_utterance so the same check can continue at resolution
        and execution time -- see ava_command_session.py.
        """
        entry_generation = self._ava_cmd_generation
        if not self._ava_cmd_ready.wait(timeout=60):
            logger.info('[AVA-CMD-UTT] Ready timeout -- dropping utterance')
            return
        if self._ava_cmd_generation != entry_generation:
            logger.debug(
                f'[AVA-CMD-UTT] Stale generation after ready wait '
                f'({entry_generation} != {self._ava_cmd_generation}) -- dropping'
            )
            return
        token = self._transcription_owners.claim('ava', only_if_idle=True)
        if token is None:
            logger.info('[AVA-CMD-UTT] Transcription in progress -- skipping')
            return
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, src_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate
            if audio_duration < 0.3:
                return
            logger.info(f'[AVA-CMD-UTT] Transcribing {audio_duration:.1f}s')
            # NOT forced to English: this content is a natural-language
            # query routed to the waterfall's stage (c), not matched
            # against the command registry directly -- use the configured
            # dictation language. Stage (a)/(b) and the control-word gates
            # below are best-effort in non-English (commands/control-words
            # remain English-only).
            transcribe_params = self.get_transcription_params()
            transcribe_params['vad_filter'] = False
            with self.model_lock:
                segments, _ = self.model.transcribe(audio, **transcribe_params)
                text = ''.join(s.text for s in segments).strip()
            text = self.voice_training_window.apply_corrections(text)
            if not text:
                return
            logger.info(f'[AVA-CMD-UTT] "{text}"')
            if self._ava_cmd_generation != entry_generation:
                logger.debug(
                    f'[AVA-CMD-UTT] Stale generation after transcription '
                    f'({entry_generation} != {self._ava_cmd_generation}) -- dropping'
                )
                return

            # Any activity (including a miss, checked later downstream)
            # re-arms the 60s inactivity timer -- spec D3 "Timeouts".
            cfg = self.config.get('ava_command_session', {})
            self._reset_ava_cmd_inactivity_timer(cfg.get('inactivity_timeout_s', 60))

            # Global abort phrases (spec: "Abort phrases honored") -- exact
            # whole-utterance match, same normalization discipline as
            # scratch-that/dictate-commit elsewhere in this app.
            if normalize_utterance(text) in {normalize_utterance(p) for p in GLOBAL_SESSION_EXIT_PHRASES}:
                self.exit_ava_command_session()
                return

            # "scratch that": unified stack, checked before enqueue so it's
            # always responsive even while the waterfall queue is backed up.
            if is_scratch_that(text):
                self._handle_unified_scratch_that()
                return

            # Stop path (execution_policy.stop_all): "stop"/"cancel"/"ava
            # cancel" bump the generation and empty the backlog HERE, on the
            # utterance thread, never behind the waterfall/inference queue.
            if self._try_stop_utterance(text, 'ava command session'):
                return

            from samsara.ava_command_session import enqueue_utterance  # noqa: PLC0415
            enqueue_utterance(self, entry_generation, text)
        except Exception as exc:
            logger.exception(f'[AVA-CMD-UTT] Error: {exc}')
            import traceback  # noqa: PLC0415
            traceback.print_exc()
        finally:
            self._transcription_owners.release('ava', token)
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
        try:
            from samsara import execution_policy  # noqa: PLC0415
            execution_policy.bump_generation(self, 'wake sleep')
        except Exception as exc:
            logger.debug(f"[POLICY] generation bump on wake sleep failed: {exc}")
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
        if self._try_stop_utterance(text, 'hold ava'):
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
            self._cancel_command_mode_inactivity_timer_locked()   # bumps the generation
            generation = self._timer_generation
            t = thread_registry.timer(
                "dictation.command_mode_inactivity", timeout_s,
                self._on_command_mode_inactivity, args=(generation,), daemon=True)
            self._command_mode_inactivity_timer = t
            self._command_mode_inactivity_deadline = time.monotonic() + timeout_s

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
        # Invalidate whatever is armed -- including a callback already running.
        self._timer_generation = getattr(self, '_timer_generation', 0) + 1
        t = self._command_mode_inactivity_timer
        if t is not None:
            t.cancel()
            self._command_mode_inactivity_timer = None
        self._command_mode_inactivity_deadline = None

    def _pause_command_mode_inactivity_for_hold(self) -> None:
        """hands_free.suspend_on_hold (default true): a hotkey hold must
        not let the toggle-session inactivity timer expire out from under
        the user just because the hold itself is long. Unlike
        _pause_session_inactivity_for_device_recovery (which re-arms with
        a FRESH full window on resume -- appropriate for an outage of
        unknown length), this remembers the exact REMAINING time so a
        30-second hold against a 300-second timeout resumes with ~270
        seconds left, not a fresh 300. Safe to call when no timer is
        running (e.g. hands-free not active, or already idle) -- it just
        records nothing to resume."""
        with self._command_mode_timer_lock:
            deadline = self._command_mode_inactivity_deadline
            self._cancel_command_mode_inactivity_timer_locked()
            self._command_mode_inactivity_remaining_on_hold = (
                max(0.0, deadline - time.monotonic()) if deadline is not None else None
            )

    def _resume_command_mode_inactivity_after_hold(self) -> None:
        """Counterpart to _pause_command_mode_inactivity_for_hold -- re-arms
        the timer with whatever time was actually left when the hold
        started, or does nothing if no timer was running then (or the
        session has since ended)."""
        remaining, self._command_mode_inactivity_remaining_on_hold = (
            self._command_mode_inactivity_remaining_on_hold, None,
        )
        if remaining is not None and self.command_mode_active:
            self._reset_command_mode_inactivity_timer(remaining)

    def _touch_session_activity(self, *, speech_onset=False) -> None:
        """Only a fresh Silero onset extends toggle-session inactivity.

        Queueing, decode/delivery, and agent completion may still signal this
        chokepoint, but cannot extend a session without a new speech edge.

        No-ops while _session_recovery_pause is set -- an announced device
        outage must not have some other in-flight signal quietly re-arm the
        timer out from under the deliberate pause below."""
        if not speech_onset or not self.command_mode_active:
            return
        _cancel_windows = _cancel_window_module()
        if _cancel_windows is not None:
            _cancel_windows.note_speech_onset(self)
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
        # Re-establish the idle deadline after a device outage; subsequent
        # speech-driven extensions still require a new Silero onset.
        cm_cfg = self.config.get('command_mode', {})
        if self.command_mode_active and cm_cfg.get('mode', 'hold') == 'toggle':
            self._reset_command_mode_inactivity_timer(cm_cfg.get('inactivity_timeout_s', 300))

    def _on_command_mode_inactivity(self, generation=None):
        """threading.Timer callback -- runs on its own thread. Must never
        raise uncaught: a raise here would leave the session latched
        (command_mode_active still True) but with its only path back to
        COMMAND-mode listening broken -- deaf but latched, a zombie
        session. On any failure inside exit_command_mode(), force the same
        end-state directly (flip the flag, cancel the timer, reset mode
        state) so the session provably ends rather than hanging.

        `generation` (queue 50): the timer generation this callback was armed
        with. If a reset or cancel has happened since, this is a stale timer
        whose Timer.cancel() came too late -- it must not end the live
        session. The check and the claim happen under the timer lock; None
        (direct callers) skips the check."""
        if generation is not None:
            with self._command_mode_timer_lock:
                if generation != self._timer_generation:
                    logger.info("[CMD MODE] Stale inactivity timer (generation %s, current %s) ignored",
                                generation, self._timer_generation)
                    return
                # Claim it: this callback is now the one ending the session, so
                # a second run of the same timer is stale too. exit_command_mode
                # still cancels and clears the timer object itself.
                self._timer_generation += 1
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
        token = self._transcription_owners.claim('toggle')
        # 2026-09-14 (queue 54): every capture logs its session mode, and
        # every return before dispatch_utterance names why -- a user must
        # never be left with silence as the only record of what happened.
        _capture_mode = 'unknown'
        audio_duration = 0.0
        _dispatched = False
        try:
            _mgr = self._session_mode_manager
            _capture_mode = _mgr.mode.value if _mgr is not None else 'no_session'
        except Exception:
            pass
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, src_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate

            if audio_duration < 0.3:
                self._log_cmd_utt_dropped('too_short', _capture_mode, audio_duration)
                return

            logger.info(f'[CMD-UTT] capture mode={_capture_mode} duration={audio_duration:.1f}s')
            logger.debug(f'[CMD-UTT] Transcribing {audio_duration:.1f}s utterance')

            # Command-mode utterances (mode==COMMAND) are matched against the
            # English command registry AND control words (switch/scratch/
            # abort, checked by SessionModeManager on every utterance
            # regardless of mode) -- keep vocabulary biasing there (it's
            # short, matcher-side recognition, not free-form prose).
            # DICTATE/AVA drop vocabulary biasing entirely -- free-form
            # prose, decode-matrix-established to be destabilized by it
            # (SPARK 2026-07-17/18, N=10/cell).
            #
            # Language is forced to English for the WHOLE lane, every mode --
            # NOT threaded through get_transcription_params()'s shared
            # base_params (that stays mode-language-driven for every other
            # caller: hold-to-dictate, transcribe_continuous_buffer,
            # process_wake_word_buffer), overridden here per-call-site
            # exactly like the command hotkey already does in
            # _build_hotkey_transcribe_params (02e00b9's per-path contract).
            # HISTORY (2026-07-18 17:03-17:04 incident): this lane used to
            # force English ONLY while mode==COMMAND. Recovering from an
            # errant Ava-mode entry (see match_ava_invocation in
            # session_modes.py / CHANGE 1), Morne's first "dictate mode"
            # attempt was spoken while mode==AVA, decoded as Vietnamese
            # ("Duoc thay mot", lang 'vi' @0.51 confidence) with auto-
            # language on, and got dispatched to Ava as a query instead of
            # switching mode -- two tries needed to recover. Mode-switch
            # phrases and short commands can be spoken from ANY mode
            # (that's the whole point of an any-to-any switch grammar), and
            # this is exactly where language auto-detect is weakest (short
            # utterances, little context). DELIBERATE TRADE: genuine
            # non-English free-form DICTATE/AVA prose spoken in this lane is
            # now also English-biased -- accepted because reliably escaping
            # a latched session via a recognizable control word matters more
            # here than non-English free-form accuracy in this specific
            # lane. The free-form hold-to-dictate HOTKEY path is untouched --
            # it keeps the user's configured language, forced or auto,
            # unconditionally.
            manager = self._ensure_session_mode_manager()
            _is_command_lane = manager.mode is SessionMode.COMMAND
            # Captured now (this utterance's actual capture-time lane) for
            # the streaming-preview clear/flash below -- dispatch_utterance
            # can itself switch manager.mode (e.g. a mode-switch phrase), so
            # checking manager.mode again AFTER dispatch would tell us the
            # lane this utterance switched TO, not the one its audio (and
            # any preview partials) were captured in.
            _was_dictate_lane = manager.mode is SessionMode.DICTATE
            transcribe_params = self.get_transcription_params(include_vocabulary=_is_command_lane)
            transcribe_params['vad_filter'] = False
            transcribe_params['language'] = 'en'
            # Queue 106: kept for the echo check after the decode -- the
            # refusal has to compare the text against the prompt this
            # utterance was ACTUALLY conditioned on, not a tail recomputed
            # afterwards from a buffer that may have moved on.
            context_tail = ''
            if (
                manager.mode is SessionMode.DICTATE
                and audio_duration <= 25.0
            ):
                # Queue 106: dictate_context_tail returns a raw
                # source[-200:] character slice, which in the five real
                # tails recovered for queue 44 always began mid-word and
                # once ended "and nd nd nd". Sanitised before it is handed
                # to the decoder -- see _sanitise_context_tail.
                # Called with no argument, as before: the manager owns the
                # window size (its own max_chars default), _CONTEXT_TAIL_CHARS
                # only tells the sanitiser how wide that window is so it can
                # tell a truncated slice from a whole short buffer. The two
                # are pinned equal by tests/test_context_prompt_contamination_106.
                context_tail = _sanitise_context_tail(
                    manager.dictate_context_tail(), _CONTEXT_TAIL_CHARS)
                # 02e00b9: 25 s guard keeps short-turn DICTATE chunks
                # continuity-biased, but skips long-tail decodes where
                # Whisper can become unstable when initial_prompt carries
                # conversational context.
                if context_tail:
                    transcribe_params['initial_prompt'] = context_tail

            transcribe_start = time.time()
            with self.model_lock:
                segments, info = self.model.transcribe(audio, **transcribe_params)
                seg_list = list(segments)
            text = ''.join(s.text for s in seg_list).strip()
            transcribe_time = time.time() - transcribe_start
            # Hands-free uses this VAD-bounded per-utterance path instead of
            # the hold-to-dictate finalizer. Keep the same diagnostic record
            # contract at the recogniser boundary so Voice Help can see both
            # capture families. This is deliberately before dispatch: a
            # recognised utterance that a later policy gate refuses is still
            # evidence that audio reached Whisper.
            try:
                _diag_sig = diagnostics.segment_signals(seg_list)
            except Exception as _diag_exc:
                logger.debug(f"[DIAG] hands-free signal extraction failed: {_diag_exc}")
                _diag_sig = {}
            try:
                diagnostics.record(diagnostics.DiagRecord(
                    mode="hands_free",
                    lane=_capture_mode,
                    audio_s=audio_duration,
                    model_name=self.config.get('model_size', config_defaults.DEFAULTS['model_size']),
                    device=getattr(self, 'device_type', 'unknown'),
                    compute_type=self.config.get('compute_type', config_defaults.DEFAULTS['compute_type']),
                    t_transcribe_ms=int(transcribe_time * 1000),
                    t_total_ms=int(transcribe_time * 1000),
                    avg_logprob=_diag_sig.get('avg_logprob'),
                    compression_ratio=_diag_sig.get('compression_ratio'),
                    no_speech_prob=_diag_sig.get('no_speech_prob'),
                    temperature=_diag_sig.get('temperature'),
                    n_segments=_diag_sig.get('n_segments', 0),
                    text=text,
                    language=_languages.describe_diagnostics_language(
                        self.config.get('language', 'en'), getattr(info, 'language', None),
                    ),
                    outcome="empty" if not text else "ok",
                ), app=self)
            except Exception as _diag_exc:
                logger.debug(f"[DIAG] hands-free record failed: {_diag_exc}")
            text = self.voice_training_window.apply_corrections(text)

            if not text:
                logger.debug('[CMD-UTT] Empty transcription')
                self._log_cmd_utt_dropped('empty_transcription', _capture_mode, audio_duration)
                return

            # Hallucination screening -- this toggle-session path runs its
            # own transcribe() call above, entirely separate from the
            # hotkey path's decode (process_wake_word_buffer), so it never
            # inherited that path's gating. Mirrors
            # _apply_segment_quality_gates' whole-decode check (step 1,
            # dictation.py ~1047) but deliberately WITHOUT the
            # quality-exhaustion gate or never-silently-empty floor: those
            # exist because losing a whole long hotkey recording is
            # catastrophic for an accessibility user with nothing to fall
            # back on. This is a continuous multi-utterance session --
            # rejecting one bad utterance costs the user a few words to
            # repeat, not a lost thought (rejection happens BEFORE
            # dispatch_utterance/DICTATE staging below, so any text already
            # staged from earlier utterances in this session is untouched).
            #
            # Trailing-garbage trim runs FIRST, not after the whole-decode
            # check: Whisper hallucinates a run of underscores/dashes/
            # periods on the near-silent tail after real speech at a
            # toggle utterance's silence boundary -- confirmed in
            # production logs (e.g. '"the __________"',
            # '"ready for <hundreds of underscores>"'). That run compresses
            # hard enough (6-17x in testing) to trip Signature A on its
            # own -- if the untrimmed seg_list were checked first, one
            # garbage segment would reject the WHOLE utterance, including
            # real speech before it, and this trim would never run. So the
            # garbage segment's telemetry is excluded from the check (see
            # _drop_trailing_garbage_segments) before _is_hallucinated_segments
            # ever runs, letting real preceding words survive on their own
            # merits while the garbage segment's inflated compression ratio
            # is never consulted. The signature-B repetition check
            # independently can't see this pattern at all: punctuation
            # stripping removes an underscore run from its word list
            # entirely rather than registering it as repetition.
            _kept_segs = _drop_trailing_garbage_segments(seg_list)
            _trimmed = _trim_trailing_garbage_run(text)
            if _trimmed != text:
                if not _trimmed:
                    logger.info(f'[GUARD] Suppressed hallucination: {text!r}')
                    self._log_cmd_utt_dropped('hallucination_trailing_garbage', _capture_mode, audio_duration)
                    return
                logger.info(f'[GUARD] Trimmed trailing garbage: {text!r} -> {_trimmed!r}')
                text = _trimmed

            if _is_hallucinated_segments(_kept_segs, text):
                logger.info(f'[GUARD] Suppressed hallucination: {text!r}')
                self._log_cmd_utt_dropped('hallucination_segments', _capture_mode, audio_duration)
                return

            # An exhausted Whisper fallback can emit plausible text while its
            # quality signals say no temperature met the configured threshold.
            # In DICTATE this must stop here: dispatch_utterance is the pending
            # buffer's sole writer, so allowing it through would feed the bad
            # decode back as the next initial_prompt.
            if _was_dictate_lane and _is_quality_exhausted(_kept_segs, transcribe_params):
                logger.info(f'[GUARD] Refused low-confidence dictate decode: {text!r}')
                self._log_cmd_utt_dropped('quality_exhausted', _capture_mode, audio_duration)
                try:
                    self._show_outcome_chip('low-confidence decode - not staged', 'warning')
                except Exception as exc:
                    logger.debug(f'[CMD-UTT] low-confidence chip failed: {exc}')
                return

            logger.debug(f'[CMD-UTT] "{text}"')

            if self._command_mode_ghost_tap:
                self._command_mode_ghost_tap = False
                logger.debug('[CMD-UTT] Ghost tap — discarding')
                self._log_cmd_utt_dropped('ghost_tap', _capture_mode, audio_duration)
                return

            if not _is_command_lane:
                text = self._filter_dictation_language(text, info)
                if not text:
                    logger.debug('[CMD-UTT] Empty transcription')
                    self._log_cmd_utt_dropped('language_filter', _capture_mode, audio_duration)
                    return

            # Queue 106: the decode reproduced the end of the prompt it was
            # given. Refused HERE, before dispatch_utterance -- which is the
            # only writer of the pending/stage buffer, and therefore the only
            # way anything reaches the next utterance's context tail. That is
            # what keeps the loop from closing: an echo is neither pasted nor
            # allowed to become the prompt that produces the next one.
            if self._is_dictate_context_echo(text, context_tail):
                logger.info(f'[GUARD] Refused context echo: {text!r} '
                            f'(tail ends {context_tail[-60:]!r})')
                self._log_cmd_utt_dropped('context_echo', _capture_mode, audio_duration)
                # Never a silent drop. The user has to know their words did
                # not land, or they carry on talking into a draft that
                # stopped listening several sentences ago.
                try:
                    self._show_outcome_chip(_CONTEXT_ECHO_CHIP, 'warning')
                except Exception as exc:
                    logger.debug(f'[CMD-UTT] context-echo chip failed: {exc}')
                return

            signals = self._compute_switch_gate_signals(
                audio,
                seg_list,
                audio_ref=audio if _was_dictate_lane else None,
            )
            self._current_utterance_duration_s = audio_duration

            _cancel_windows = _cancel_window_module()
            if _cancel_windows is not None:
                _cancel_windows.note_utterance_start()
            outcome = manager.dispatch_utterance(text, signals)
            _dispatched = True
            logger.info(f'[SESSION] mode={manager.mode.value} outcome={outcome.kind} detail={outcome.detail}')
            self._handle_session_dispatch_outcome(outcome, text)
            if _cancel_windows is not None:
                # Queue 69: a window HELD by this utterance's speech onset is
                # decided by what the utterance became (after its own chip,
                # so a "cancelled" chip is the one left showing).
                _cancel_windows.after_utterance(self, outcome.kind)
            if _was_dictate_lane and self._dictate_preview is not None:
                # This utterance's authoritative final just landed. `text`
                # is the same string dispatch_utterance above just used --
                # not a re-decode. on_utterance_final itself suppresses
                # control phrases (scratch that / end / and / switch words /
                # Ava invocations / exit phrases) from the visible
                # transcript; outcome.kind == "scratch_success" is passed
                # through as the REAL (not text-guessed) signal that the
                # scratch-that undo actually happened, so the preview can
                # correctly mirror it by popping the just-finalized line --
                # see DictatePreviewSession.on_utterance_final's docstring.
                try:
                    self._dictate_preview.on_utterance_final(
                        text,
                        scratch_success=(outcome.kind == 'scratch_success'),
                        dictate_committed=(outcome.kind == 'dictate_committed'),
                        # Queue 88: "bring back my draft" rewrote the staged
                        # buffer from the recovery slot. The preview has no
                        # other way to learn that -- the utterance's own text
                        # is the control phrase, not the draft.
                        draft_recovered=(outcome.kind == 'dictate_draft_recovered'),
                    )
                except Exception as e:
                    logger.debug(f'[DICTATE-PREVIEW] on_utterance_final failed: {e}')
            if _was_dictate_lane:
                # Shadow intent gate (36): observer only, strictly AFTER
                # dispatch_utterance has staged/injected the text and the
                # outcome was handled -- it gets a copy of the text and the
                # outcome kind, queues them, and returns; the decision runs on
                # a background worker. It never raises.
                self._intent_shadow_observe(text, outcome)
        except Exception as exc:
            # Any exception here (transcription error, injection failure,
            # a lane's dispatch blowing up) must earcon and leave the
            # session ALIVE in its current mode -- never propagate and kill
            # this utterance's thread silently. Mode/command_mode_active
            # are untouched, so the next utterance dispatches normally.
            logger.exception(f'[CMD-UTT] Error: {exc}')
            if not _dispatched:
                self._log_cmd_utt_dropped(f'error:{type(exc).__name__}', _capture_mode, audio_duration)
            try:
                self.play_sound('error')
            except Exception as e:
                logger.debug(f'[CMD-UTT] Error earcon failed: {e}')
        finally:
            self._transcription_owners.release('toggle', token)
            self._vad_reset()

    def _is_dictate_context_echo(self, text, context_tail) -> bool:
        """_is_context_echo, with the one exemption that predicate cannot
        make on its own: a recognised session CONTROL phrase is never an
        echo, however well it matches the tail.

        "bring back my draft" is four words, exactly the echo floor, and it
        is the phrase the owner reaches for when the draft is already in a
        mess -- precisely the state in which the buffer might end with odd
        text. Refusing a control phrase would be this fix eating the way out
        of the problem it exists to fix. Checked against the same matchers
        dispatch_utterance itself uses, so the exemption can never drift
        from what actually counts as control.

        Only the PURE module-level matchers are consulted -- deliberately not
        the manager-bound ones (_matches_abort_phrase, match_ava_invocation).
        Those need session state this predicate would have to reach for, and
        the exemption they would add is empty in practice: every control
        phrase that is both four words or more AND could plausibly be the
        verbatim tail of dictated prose is covered here, "bring back my
        draft" being the one that actually matters. A predicate that can
        disable the whole check by reading a stray attribute is worth less
        than the case it would cover.

        Best effort: a matcher raising falls back to the plain text check
        rather than to accepting the echo.
        """
        if not _is_context_echo(text, context_tail):
            return False
        try:
            if (is_scratch_that(text)
                    or is_dictate_commit(text)
                    or is_recover_draft(text)
                    or match_switch_word(text) is not None):
                logger.debug(f'[CMD-UTT] context echo exempt (control phrase): {text!r}')
                return False
        except Exception as exc:
            logger.debug(f'[CMD-UTT] control-phrase exemption unavailable: {exc}')
        return True

    @staticmethod
    def _log_cmd_utt_dropped(reason: str, mode: str, duration_s: float) -> None:
        """One INFO line for an utterance that never reached (or never finished)
        SessionModeManager.dispatch_utterance. The reason is a fixed token so the
        log answers "why did nothing happen?" without the transcript text."""
        logger.info(f'[CMD-UTT] dropped reason={reason} mode={mode} duration={duration_s:.1f}s')
        _cancel_windows = _cancel_window_module()
        if _cancel_windows is not None:
            # Queue 69: the sound that held a cancel window was not speech
            # that reached dispatch -- release the hold (the command runs).
            _cancel_windows.after_utterance(None, f'dropped_{reason}')

    def _intent_shadow_observe(self, text, outcome) -> None:
        """Hand one finalised DICTATE utterance to the shadow intent gate
        (samsara/intent/shadow.py). Observer only: reads the outcome KIND,
        never the manager; any failure is counted, logged once, swallowed.
        Disabled (intent.shadow_enabled false) costs one dict lookup -- no
        import of the gate, no thread, no file."""
        try:
            section = self.config.get('intent')
            if isinstance(section, dict) and not section.get('shadow_enabled', True):
                return
            shadow = getattr(self, '_intent_shadow', None)
            if shadow is None:
                from samsara.intent.shadow import IntentShadow

                def _resolver_factory():
                    # The app's own live registry rows -- never a second
                    # CommandExecutor (that would reload every plugin).
                    from samsara.intent.resolve import IntentResolver
                    return IntentResolver(rows=self.command_executor._matcher.list_commands())

                shadow = self._intent_shadow = IntentShadow(_resolver_factory, lambda: self.config)
            shadow.observe(str(text), str(getattr(outcome, 'kind', '')))
        except Exception as exc:
            self._intent_shadow_errors = getattr(self, '_intent_shadow_errors', 0) + 1
            if self._intent_shadow_errors == 1:
                logger.warning(f"[INTENT-SHADOW] observe failed ({type(exc).__name__}: {exc}); "
                               f"further failures this session are counted, not logged")

    def _handle_session_dispatch_outcome(self, outcome: "DispatchOutcome", text: str) -> None:
        """Side effects keyed on the unified session's dispatch outcome that
        don't belong inside SessionModeManager itself (earcons, the
        command-mode inactivity timer) -- split out from
        _handle_command_mode_utterance so this logic is testable without a
        full transcription pipeline.

        _touch_session_activity() is the SINGLE chokepoint for the unified
        session's inactivity timer: every outcome except "empty" (a
        discarded near-silence/blank transcription -- never activity)
        signals it here, once, regardless of which lane produced it. This
        replaces the old scattered per-lane resets (COMMAND-only, and
        AVA-only).

        Signalling is not the same as extending. Since the 2026-09-10
        session policy, _touch_session_activity() re-arms the timer ONLY for
        a fresh Silero speech onset (speech_onset=True); this call site
        passes no such flag, so dispatch outcomes -- decode, delivery,
        command execution -- funnel through the chokepoint without
        extending the session. Only the user speaking again does that. See
        _touch_session_activity's own docstring and
        docs/HANDS_FREE_GATES_FINDINGS.md "Session policy"."""
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
        elif outcome.kind == "dictate_recover_nothing":
            # Queue 88: "bring back my draft" with an empty slot is a REFUSAL,
            # and must sound like one. SessionModeManager already speaks
            # "There is no draft to bring back." and the chip reads "nothing to
            # bring back"; this is the same "didn't go through" earcon every
            # other refusal on this path uses, so the answer is unmistakable
            # before the sentence has even started.
            logger.info('[SESSION] recover-draft asked for with an empty slot; nothing restored')
            self.play_sound('scratch_refuse')
        elif outcome.kind in ("hands_free_command_failed", "command_failed"):
            # Recognised command that did not carry out (failed, refused by
            # debounce/policy, or cancelled). Refusals get the "didn't go
            # through" sound; genuine failures the error earcon.
            state = outcome.detail.get('state')
            if state in ('rejected', 'cancelled'):
                logger.info('[SESSION] Command %s: %r', state, outcome.detail)
                self.play_sound('scratch_refuse')
            else:
                logger.error('[SESSION] Command failed to execute: %r', outcome.detail)
                self.play_sound('error')
        elif outcome.kind == "dictate_blocked_elevated":
            # Queue 50: the "audible lie" -- Windows would silently drop
            # anything typed into an elevated window. Its own earcon, never a
            # success sound; the text is retained.
            logger.warning('[SESSION] Not typing: the foreground window runs as administrator '
                           '(%s) -- text kept', outcome.detail.get('integrity'))
            self._play_window_locked()
        elif outcome.kind == "ava_entry_failed":
            # 2026-09-11: AVA entry must never fail silently. The session
            # already logged the reason at WARNING and stayed in the previous
            # mode (session_modes._do_switch); this is the audible half.
            retained = outcome.detail.get('mode_retained')
            logger.warning(
                '[SESSION] Could not switch to Ava: %s -- still in %s mode',
                outcome.detail.get('reason', 'unknown reason'),
                getattr(retained, 'value', retained),
            )
            self.play_sound('error')
            _refusal = getattr(self, '_speak_ava_entry_refusal', None)   # stubs may lack it
            if _refusal is not None:
                _refusal(outcome.detail.get('reason'))
            if str(outcome.detail.get('reason') or '').startswith('Ava is offline'):
                # The generic chip truncates the sentence to ~24 chars; the
                # readiness chip says "Ava: offline: bad API key" instead.
                self._ava_readiness_chip_due = True
        # Best-effort UI side-channel: resolved via getattr so a missing or
        # failing chip can never skip the earcons above or the inactivity
        # chokepoint below (this method is also bound onto minimal stubs).
        _chip = getattr(self, '_show_dispatch_outcome_chip', None)
        if _chip is not None:
            _chip(outcome)
        if getattr(self, '_ava_readiness_chip_due', False):
            # Queue 57: readiness of the configured provider, before the user
            # speaks to Ava. Replaces the "-> AVA" switch chip on purpose.
            self._ava_readiness_chip_due = False
            _ready_chip = getattr(self, '_show_ava_readiness_chip', None)
            if _ready_chip is not None:
                _ready_chip()
        # Queue 198: this is the one cross-utterance seam. It sees the MISS
        # outcome and the next final outcome, while DICTATE outcomes remain
        # ineligible inside command_catalog.personal_alias_offer_after_outcome.
        try:
            from samsara.command_catalog import personal_alias_offer_after_outcome
            offer = personal_alias_offer_after_outcome(self, outcome, text)
            if offer:
                from samsara.session_modes import outcome_chip
                chip = outcome_chip("personal_alias_offer", offer)
                if chip:
                    self._show_outcome_chip(*chip, 8000)
        except Exception as exc:
            logger.warning("[ALIASES] Offer state failed: %s", exc)
        if outcome.kind != "empty":
            self._touch_session_activity()

    # ── Outcome chip (queue 41) ─────────────────────────────────────────────
    # The listening indicator is the one place the app says what just
    # happened. The vocabulary lives in session_modes.outcome_chip (Qt-free,
    # tested); these helpers only schedule it onto the Qt thread.

    #: Chip for the queue-50 elevated-window refusal (same text the session
    #: outcome chip uses -- see session_modes.outcome_chip).
    WINDOW_LOCKED_CHIP = chr(0x2717) + " can't type: admin window"   # session_modes.CHIP_CROSS

    def _play_window_locked(self) -> None:
        """The queue-50 'this window will not accept typing' earcon. Falls back
        to the error earcon for a sound theme without window_locked.wav, so it
        is never silent and never a success sound."""
        cache = getattr(self, '_sound_cache', None) or {}
        self.play_sound('window_locked' if 'window_locked' in cache else 'error')

    def _announce_window_locked(self, verdict, lane: str) -> None:
        """Log + earcon + chip for a delivery refused because the foreground
        window runs at a higher integrity level."""
        logger.warning("[INJECT] %s: not typing -- the foreground window runs as administrator and "
                       "Windows would silently drop the text (%s)", lane, verdict.describe())
        self._play_window_locked()
        self._show_outcome_chip(self.WINDOW_LOCKED_CHIP, "error")

    # ── Ava readiness surface (queue 57) ────────────────────────────────────
    AVA_READINESS_CHIP_TTL_MS = 4000

    def _speak_ava_entry_refusal(self, reason) -> None:
        """Say why AVA entry was refused. ask_ollama's reasons are already
        sentences ("Ava is offline. DeepSeek rejected the API key. ...");
        internal ones get a plain prefix. Category ava_status is exempt from
        command_mode.tts_char_limit, which would otherwise swallow it."""
        try:
            text = str(reason or "").strip()
            if not text.startswith("Ava "):
                text = f"Ava is not available: {text or 'unknown reason'}."
            coordinator = getattr(self, 'audio_coordinator', None)
            if coordinator is not None:
                coordinator.speak(text, category="ava_status")
        except Exception as exc:
            logger.debug(f"[SESSION] Ava refusal speech failed: {exc}")

    def _show_ava_readiness_chip(self) -> None:
        """"Ava: ready (DeepSeek)" / "Ava: offline: bad API key" /
        "Ava: checking (DeepSeek)" -- shown on entering AVA, before the user
        has said anything, and again whenever readiness changes while in AVA."""
        try:
            from plugins.commands.ask_ollama import readiness_chip
            label, kind = readiness_chip(self)
            self._show_outcome_chip(label, kind, self.AVA_READINESS_CHIP_TTL_MS)
        except Exception as exc:
            logger.debug(f"[AVA-READY] chip failed: {exc}")

    def _on_ava_readiness_change(self, old, new) -> None:
        """ava_readiness listener (monitor thread or Ava worker). Only
        surfaces while the session is in AVA; a failed turn already spoke its
        own reason, so only probe-detected losses are spoken here."""
        try:
            manager = getattr(self, '_session_mode_manager', None)
            if manager is None or manager.mode is not SessionMode.AVA:
                return
            if not getattr(self, 'command_mode_active', False):
                return
            self._show_ava_readiness_chip()
            if new.offline and not old.offline and new.source == 'probe':
                coordinator = getattr(self, 'audio_coordinator', None)
                if coordinator is not None:
                    coordinator.speak(new.spoken_reason(), category="ava_status")
        except Exception as exc:
            logger.debug(f"[AVA-READY] change handler failed: {exc}")

    def _show_outcome_chip(self, label, kind, ttl_ms="default", *, source=""):
        """Schedule a chip on the indicator. Any chip shown here also counts
        as the resolution of a pending hold "..." -- see _resolve_hold_chip.

        `source` is the DispatchOutcome kind this chip was made from, passed
        by _show_dispatch_outcome_chip and empty for a directly raised chip.
        It is what lets a reader tell a COMMAND outcome from a dictation one
        (queue 109): outcome_chip() maps unrelated kinds onto identical words,
        so the label alone cannot answer that, and Home's miss diagnostic
        counts commands.
        """
        self._hold_chip_resolved_seq = getattr(self, '_hold_chip_seq', 0)
        # Home's "last action" card and its miss diagnostic read this ring
        # (newest last, capped at 8); pending/live chips are progress, not
        # outcomes, so they are skipped. One schema for writer and readers --
        # samsara.outcome_ring.OutcomeRecord, a NamedTuple, so the positional
        # readers (home_qt.render_outcome) are unaffected.
        if kind not in outcome_ring.PROGRESS_KINDS:
            if getattr(self, '_outcome_ring', None) is None:
                self._outcome_ring = collections.deque(maxlen=8)
            self._outcome_ring.append(
                outcome_ring.record(label, kind, time.time(), source))
        indicator = getattr(self, 'listening_indicator', None)
        if indicator is None or not hasattr(indicator, 'show_outcome'):
            return
        if ttl_ms == "default":
            from samsara.session_modes import CHIP_TTL_MS
            ttl_ms = None if kind in ('pending', 'live') else CHIP_TTL_MS
        try:
            self._schedule_ui(indicator.show_outcome, label, kind, ttl_ms)
        except Exception as exc:
            logger.debug(f"[CHIP] schedule failed: {exc}")

    def _show_dispatch_outcome_chip(self, outcome) -> None:
        """Map one DispatchOutcome to its chip. An unmapped kind still shows
        ("? <kind>", visibly wrong on purpose) and logs a WARNING naming the
        kind so it gets added to session_modes.outcome_chip."""
        try:
            from samsara.session_modes import chip_ttl_ms, is_mapped_outcome, outcome_chip
            chip = outcome_chip(outcome.kind, outcome.detail)
            if chip is None:
                return
            if not is_mapped_outcome(outcome.kind):
                # Kind name only -- never the rendered label, which carries
                # non-ASCII glyphs a cp1252 console handler cannot encode.
                logger.warning("[CHIP] Unmapped DispatchOutcome kind %r -- add it to "
                               "session_modes.outcome_chip", outcome.kind)
            label, chip_kind = chip
            # The DispatchOutcome kind travels with the chip into the ring
            # (queue 109): it is the only field that says which lane the
            # outcome belongs to once the label has been rendered.
            self._show_outcome_chip(label, chip_kind, chip_ttl_ms(outcome.kind, chip_kind),
                                    source=outcome.kind)
        except Exception as exc:
            logger.debug(f"[CHIP] outcome chip failed for {outcome.kind!r}: {exc}")

    def _show_hold_pending_chip(self) -> int:
        """Show the hold-release "..." and return its sequence number.

        Deliberately does NOT mark itself resolved: it is the pending chip the
        transcription outcome is expected to replace."""
        from samsara.session_modes import CHIP_ELLIPSIS
        self._hold_chip_seq = getattr(self, '_hold_chip_seq', 0) + 1
        seq = self._hold_chip_seq
        indicator = getattr(self, 'listening_indicator', None)
        if indicator is not None and hasattr(indicator, 'show_outcome'):
            try:
                self._schedule_ui(indicator.show_outcome, CHIP_ELLIPSIS, "pending", None)
            except Exception as exc:
                logger.debug(f"[CHIP] pending chip failed: {exc}")
        return seq

    def _resolve_hold_chip(self, seq: int) -> None:
        """Watchdog for the hold "..." chip.

        The hold transcription thread has many exits (gated, language
        rejected, memo, hold-to-command, early returns). Rather than put a
        chip at every one, the thread's wrapper calls this when it ends: if no
        terminal chip was shown for THIS hold, the pending "..." is cleared so
        it can never be left on screen indefinitely."""
        if getattr(self, '_hold_chip_resolved_seq', 0) >= seq:
            return
        indicator = getattr(self, 'listening_indicator', None)
        if indicator is not None and hasattr(indicator, 'clear_outcome'):
            try:
                self._schedule_ui(indicator.clear_outcome)
            except Exception as exc:
                logger.debug(f"[CHIP] pending chip clear failed: {exc}")

    def _compute_switch_gate_signals(self, audio, seg_list, audio_ref=None) -> "UtteranceSignals":
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
            audio_ref=audio_ref,
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
            # include_vocabulary=False: free-form dictation, decode-matrix-
            # established (SPARK 2026-07-17/18, N=10/cell) to be destabilized
            # by vocabulary content in initial_prompt -- see
            # voice_training_qt.get_initial_prompt.
            transcribe_params = self.get_transcription_params(include_vocabulary=False)
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
            text = self._filter_dictation_language(text, info)
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
        if self.wake_word_active or getattr(self, '_wake_start_pending', False):
            self.stop_wake_word_mode()
        else:
            self.start_wake_word_mode()

    # ── Hands-free fatal recovery (queue 109, Astra F6) ───────────────────

    def _on_wake_consumer_fatal(self, exc) -> None:
        """WakeConsumer.on_fatal -- the poll loop has died and cleaned up.

        Runs ON THE DYING POLL THREAD, after _handle_fatal has released the
        ducks, drained the toggle queue, cancelled the session timers and
        cleared the session flags. Everything here must therefore be quick
        and must not raise: this thread is about to end either way, and a
        failure here is the difference between an explained stop and a
        silent one.

        Two things happen. The fault is RECORDED, persistently -- it is read
        by samsara.ui.home_signals.hands_free_state and lives until a restart
        succeeds, because a chip that expires and an error beep are exactly
        what left the user with nothing. And, only for a failure classified
        transient (wake_consumer.classify_fatal), a restart is scheduled,
        capped at wake_consumer.FATAL_RETRY_CAP attempts on a backoff. A
        programming error is never retried: it would raise again on the next
        frame and spin.
        """
        try:
            from samsara.audio_engine import wake_consumer as _wc   # noqa: PLC0415
            attempts = int(getattr(self, '_hands_free_fault_attempts', 0) or 0)
            classification = _wc.classify_fatal(exc)
            retrying = (classification == _wc.FATAL_TRANSIENT
                        and attempts < _wc.FATAL_RETRY_CAP)
            self._hands_free_fault = _wc.fault_from(
                exc, at=time.time(), attempts=attempts, retrying=retrying)
            logger.error("[WAKE] Hands-free stopped (%s, attempt %d/%d, retry=%s): %s",
                         classification, attempts, _wc.FATAL_RETRY_CAP, retrying, exc)
            flight_recorder.record('wake.fatal', classification=classification,
                                   attempts=attempts, retrying=retrying,
                                   exc=type(exc).__name__)
        except Exception as record_exc:
            logger.exception(f"[WAKE] Could not record the hands-free fault: {record_exc}")
            retrying = False

        try:
            if callable(getattr(self, 'play_sound', None)):
                self.play_sound('error')
        except Exception as sound_exc:
            logger.debug(f"[WAKE] fatal earcon failed: {sound_exc}")
        self._publish_hands_free_fault()

        if not retrying:
            return
        try:
            from samsara.audio_engine import wake_consumer as _wc   # noqa: PLC0415
            delay = _wc.FATAL_RETRY_DELAYS_S[attempts]
            self._hands_free_fault_attempts = attempts + 1
            thread_registry.timer('wake.fatal_restart', delay,
                                  self._retry_hands_free_after_fatal,
                                  args=(attempts + 1,), daemon=True)
            logger.info("[WAKE] Automatic hands-free restart %d/%d in %.0fs",
                        attempts + 1, _wc.FATAL_RETRY_CAP, delay)
        except Exception as retry_exc:
            logger.exception(f"[WAKE] Could not schedule the hands-free restart: {retry_exc}")

    def _retry_hands_free_after_fatal(self, attempt: int) -> None:
        """One scheduled automatic restart. A restart that does not bring
        capture back leaves the fault standing, with `retrying` now false
        when the budget is spent, so Home stops promising another go."""
        if getattr(self, '_hands_free_fault', None) is None:
            return                     # something already recovered it
        ok = self.restart_hands_free(source=f"auto:{attempt}")
        logger.info("[WAKE] Automatic hands-free restart %d: %s",
                    attempt, "listening again" if ok else "still stopped")

    def restart_hands_free(self, *, source: str = "user") -> bool:
        """Bring hands-free listening back after a fatal stop.

        Returns True only when the wake listener is actually running again --
        never on "the restart was attempted". Home's recovery button reads
        this, and a button that reports success onto a still-dead listener is
        the failure this whole queue is about.

        A USER restart also resets the automatic budget: someone asking again
        by hand is a new attempt, not a continuation of the app's own.
        """
        if source == "user":
            self._hands_free_fault_attempts = 0
        if not self.config.get('wake_word_enabled', False):
            # Nothing to restore: hands-free is switched off, which Home
            # reports as OFF rather than as a fault.
            self._hands_free_fault = None
            return False
        try:
            if not self.wake_word_active:
                self.start_wake_word_mode()
            else:
                self._ensure_wake_consumer('wake_word')
        except Exception as exc:
            logger.exception(f"[WAKE] Hands-free restart failed: {exc}")
            return False

        consumer = getattr(self, '_wake_consumer', None)
        running = bool(getattr(consumer, 'running', False))
        if running and self.wake_word_active:
            self._hands_free_fault = None
            self._hands_free_fault_attempts = 0
            logger.info("[WAKE] Hands-free restarted (%s)", source)
        else:
            fault = getattr(self, '_hands_free_fault', None)
            if fault is not None:
                # The restart did not take. Keep the explanation and say
                # truthfully whether the app still intends to try again.
                try:
                    import dataclasses                               # noqa: PLC0415
                    from samsara.audio_engine import wake_consumer as _wc  # noqa: PLC0415
                    spent = int(getattr(self, '_hands_free_fault_attempts', 0) or 0)
                    self._hands_free_fault = dataclasses.replace(
                        fault, attempts=spent,
                        retrying=(fault.classification == _wc.FATAL_TRANSIENT
                                  and spent < _wc.FATAL_RETRY_CAP))
                except Exception as exc:
                    logger.debug(f"[WAKE] Could not update the hands-free fault: {exc}")
        self._publish_hands_free_fault()
        return running and bool(self.wake_word_active)

    def _publish_hands_free_fault(self) -> None:
        """Push the fault (or its clearing) to the surfaces that already
        re-read app state -- the tray tooltip and the indicator's mode label.
        Home re-reads on its own poll; this is so the tray does not keep
        claiming a listener that has stopped."""
        try:
            self._publish_wake_state()
        except Exception as exc:
            logger.debug(f"[WAKE] Could not publish the hands-free fault: {exc}")

    def _ensure_wake_consumer(self, reason: str) -> None:
        """Ensure the WakeConsumer PIPELINE (poll thread) is running for
        `reason`, WITHOUT touching wake-phrase DETECTION state or any of
        start_wake_word_mode's user-facing side effects (earcon,
        wake_word_active flag, listening-indicator, hints). Reason-counted
        (see _wake_consumer_reasons, initialized alongside _wake_consumer)
        so wake-detection and a toggle session can each hold the pipeline
        open independently -- one releasing its reason must never stop the
        consumer while the other still needs it.

        Idempotent: adding an already-held reason, or calling this while
        the consumer is already running, is a no-op (WakeConsumer.start()
        itself no-ops when already running).
        """
        with self._wake_consumer_lock:
            self._wake_consumer_reasons.add(reason)
            if self._wake_consumer is not None and not self._wake_consumer._running:
                self._wake_consumer.start()

    def _release_wake_consumer(self, reason: str) -> None:
        """Release `reason`'s hold on the WakeConsumer pipeline. Stops the
        consumer ONLY when no reason still needs it running -- see
        _ensure_wake_consumer. Discarding a reason that was never held (or
        calling this when the consumer is already stopped) is a safe
        no-op, so double-exit / rapid toggle sequences can't corrupt state.

        When this call is the one that actually stops the consumer (the
        last reason releasing), mirrors stop_wake_word_mode's original
        handling of a still-triggered wake-word utterance in flight: any
        remaining buffered audio is flushed through process_wake_word_buffer.
        """
        with self._wake_consumer_lock:
            self._wake_consumer_reasons.discard(reason)
            if (self._wake_consumer_reasons
                    or self._wake_consumer is None
                    or not self._wake_consumer._running):
                return
            remaining = self._wake_consumer.stop()
        if remaining and self.wake_word_triggered:
            self.process_wake_word_buffer(remaining, src_rate=16000)
        elif remaining:
            logger.info('[WAKE] Listener stopped -- discarding buffered utterance')
            flight_recorder.record('wake.dispatch_dropped', reason='listener_stopped')

    def _update_streaming_preview(self, mode: "SessionMode") -> None:
        """Show/hide the toggle-session DICTATE-lane streaming preview on
        every mode transition (wired into SessionModeManager's
        on_mode_change alongside _update_mode_overlay). COMMAND/AVA lanes
        never show it -- those utterances are ~1s and partials would be
        noise. Config-gated: when session_streaming_preview is off, this
        never constructs a DictatePreviewSession, so zero new code runs in
        the hot path.

        reset()-driven entry (the toggle session's initial DICTATE lane,
        and session end) does NOT go through on_mode_change -- see
        enter_command_mode/exit_command_mode's own explicit calls to this
        method alongside their existing _update_mode_overlay calls.
        """
        if not self.config.get('command_mode', {}).get('session_streaming_preview', True):
            return
        if mode is SessionMode.DICTATE:
            self._ensure_streaming_preview()
        else:
            self._release_streaming_preview()

    def _ensure_streaming_preview(self) -> None:
        """Start the DICTATE-lane preview overlay if not already running.
        Idempotent. Failure here is best-effort -- never allowed to affect
        the authoritative per-utterance transcription/dispatch path."""
        if self._dictate_preview is not None:
            return
        try:
            from samsara.streaming import DictatePreviewSession
            self._dictate_preview = DictatePreviewSession(self)
            self._dictate_preview.start()
        except Exception as e:
            logger.exception(f'[DICTATE-PREVIEW] Failed to start: {e}')
            self._dictate_preview = None

    def _release_streaming_preview(self) -> None:
        """Stop and clear the DICTATE-lane preview overlay. Idempotent
        no-op when nothing is running."""
        preview, self._dictate_preview = self._dictate_preview, None
        if preview is None:
            return
        try:
            preview.stop()
        except Exception as e:
            logger.debug(f'[DICTATE-PREVIEW] Stop failed: {e}')

    # ── Hands-free audio ducking (2026-07-24) ──────────────────────────────
    #
    # Two-stage, layered LIFO via two INDEPENDENT SessionDucker instances
    # (samsara/audio_ducking.py): idle duck engages/releases with the
    # wake-word toggle (start_wake_word_mode/stop_wake_word_mode -- the
    # ONLY thing that ends it); capture duck engages/releases per capture
    # window (WakeConsumer speech onset through transcription-complete/
    # discard + a debounced tail -- see wake_consumer.py). Neither path
    # calls, or is called by, anything that ends a session/mode -- this
    # is pure volume side effect, introducing zero new exit semantics.
    # See the "ducking" default_config block's own comment for the full
    # design rationale (AEC not converging, pre-buffer SNR, etc).

    def _hands_free_duck_excludes(self) -> set:
        """PIDs to exclude from hands-free ducking, beyond SessionDucker's
        own automatic own-pid exclusion. No separate TTS/ffmpeg child-
        process PID tracking exists anywhere in this codebase (verified:
        nothing under samsara/tts/ spawns a subprocess), so there is
        nothing else to exclude today. A dedicated method -- rather than
        an inline literal at each call site -- so a future PID-tracking
        addition has exactly one place to plug in."""
        # The Core Audio host is a child Python process in source runs.  It
        # has a session of its own and must never become a duck target.
        return audio_ducking.ducking_host_pids()

    def _bump_wake_gate_freeze(self) -> None:
        """Called on every duck transition (idle or capture, start or
        stop) -- freezes the adaptive wake-gate's noise-floor EMA
        (_wake_audio_is_below_gate) for _WAKE_GATE_FREEZE_SETTLE_S so it
        doesn't chase a duck-induced volume step and then misread the
        eventual restore as speech onset. See _wake_gate_frozen()."""
        self._wake_gate_freeze_until = time.monotonic() + _WAKE_GATE_FREEZE_SETTLE_S

    def _wake_gate_frozen(self) -> bool:
        """True while the adaptive wake-gate must not update its noise-
        floor EMA: TTS is actively speaking (a continuous state, checked
        directly -- not just a transition) OR a duck transition happened
        within the settle window. Read from _wake_audio_is_below_gate.
        Never raises -- a broken read here must never crash wake
        processing, just fail toward "not frozen" (normal adaptation)."""
        try:
            coordinator = getattr(self, 'audio_coordinator', None)
            if coordinator is not None and getattr(coordinator, 'is_speaking', False):
                return True
            return time.monotonic() < self._wake_gate_freeze_until
        except Exception:
            return False

    @staticmethod
    def _log_duck_result(ducker, label: str) -> None:
        """One log line per duck engage/restore, with the ducked-session
        count. SessionDucker has no public count accessor; reads the
        tracking dict directly (same package, acceptable internal use --
        see samsara/audio_ducking.py)."""
        try:
            count = len(ducker._tracked_by_id)
        except Exception:
            count = 0
        logger.info(f"[DUCK] {label}: {count} session(s)")

    def _start_hands_free_idle_duck(self) -> None:
        """Engage the persistent mild duck for the wake-word toggle's
        WHOLE duration (2026-07-24 amendment: the mechanism, not an
        option) -- NOT tied to active capture. Called ONLY from
        start_wake_word_mode(); the only thing that ever ends this is
        stop_wake_word_mode()'s matching _stop_hands_free_idle_duck()."""
        cfg = self.config.get('ducking', {}) or {}
        if not cfg.get('hands_free_enabled', True):
            return
        level = float(cfg.get('hands_free_idle_level', 0.8))
        if level >= 1.0:
            return  # 1.0 == disabled by design, not a failure
        try:
            with self._hands_free_duck_lock:
                if self._hands_free_idle_ducker is not None:
                    return  # already engaged
                excludes = self._hands_free_duck_excludes()
            ducker = audio_ducking.SessionDucker(
                duck_level=level, exclude_pids=excludes,
            )
            ducker.start()
            published = False
            with self._hands_free_duck_lock:
                if self._hands_free_idle_ducker is None:
                    self._hands_free_idle_ducker = ducker
                    published = True
            if published:
                self._bump_wake_gate_freeze()
                self._log_duck_result(ducker, "idle duck engaged")
            else:
                ducker.stop()
        except Exception as exc:
            logger.warning(f"[DUCK] Failed to engage idle duck: {exc}")

    def _stop_hands_free_idle_duck(self) -> None:
        """Release the idle duck -- called ONLY from stop_wake_word_mode().
        Also tears down any still-active capture duck and its pending
        debounce timer: belt-and-suspenders cleanup (not a new exit path
        -- it only ever runs as part of the ALREADY-existing toggle-off
        call) so the wake-word toggle turning off can never leave
        anything ducked, even mid capture-window."""
        try:
            with self._hands_free_duck_lock:
                timer = self._hands_free_duck_restore_timer
                self._hands_free_duck_restore_timer = None
                capture_ducker = self._hands_free_capture_ducker
                self._hands_free_capture_ducker = None
                self._hands_free_capture_duck_start_generation += 1
                self._hands_free_capture_duck_owners.clear()
                self._hands_free_capture_duck_starting = False
                self._hands_free_capture_duck_restore_generation += 1
                idle_ducker = self._hands_free_idle_ducker
                self._hands_free_idle_ducker = None
            if timer is not None:
                timer.cancel()
            if capture_ducker is not None:
                self._log_duck_result(capture_ducker, "capture duck restored (toggle off)")
                capture_ducker.stop()
            if idle_ducker is not None:
                self._log_duck_result(idle_ducker, "idle duck restored")
                idle_ducker.stop()
            self._bump_wake_gate_freeze()
        except Exception as exc:
            logger.warning(f"[DUCK] Failed to release idle duck: {exc}")

    def _open_hands_free_capture_duck(self, owner_token: int | None = None) -> int | None:
        """Engage the deep duck for an active capture window (speech
        onset accepted). Reuses the already-active instance if one exists
        (rapid consecutive utterances) and cancels any pending debounced
        restore. Called from WakeConsumer at speech onset; never calls,
        and is never called by, any session-end/exit path."""
        cfg = self.config.get('ducking', {}) or {}
        if not cfg.get('hands_free_enabled', True):
            return owner_token
        level = float(cfg.get('hands_free_level', 0.15))
        if level >= 1.0:
            return owner_token

        if owner_token is None:
            with self._hands_free_duck_lock:
                self._hands_free_capture_duck_owner_seq += 1
                owner_token = self._hands_free_capture_duck_owner_seq
        owner_token = int(owner_token)

        ducker: audio_ducking.SessionDucker | None = None
        started_here = False
        start_generation = 0
        ducker_to_stop: audio_ducking.SessionDucker | None = None
        try:
            with self._hands_free_duck_lock:
                timer = self._hands_free_duck_restore_timer
                self._hands_free_duck_restore_timer = None
                if timer is not None:
                    timer.cancel()

                self._hands_free_capture_duck_owners.add(owner_token)

                if self._hands_free_capture_ducker is not None:
                    return owner_token
                elif self._hands_free_capture_duck_starting:
                    return owner_token
                else:
                    self._hands_free_capture_duck_starting = True
                    started_here = True
                    self._hands_free_capture_duck_start_generation += 1
                    start_generation = self._hands_free_capture_duck_start_generation

            if started_here:
                ducker = audio_ducking.SessionDucker(
                    duck_level=level, exclude_pids=self._hands_free_duck_excludes(),
                )
                ducker.start()
                published = False
                should_publish = False
                with self._hands_free_duck_lock:
                    current_generation = self._hands_free_capture_duck_start_generation
                    if start_generation == current_generation:
                        self._hands_free_capture_duck_starting = False
                    if start_generation == current_generation and self._hands_free_capture_duck_owners:
                        should_publish = True
                        if self._hands_free_capture_ducker is None:
                            self._hands_free_capture_ducker = ducker

                if should_publish:
                    if self._hands_free_capture_ducker is ducker:
                        self._bump_wake_gate_freeze()
                        self._log_duck_result(ducker, "capture duck engaged")
                        published = True
                    else:
                        ducker.stop()
                else:
                    ducker.stop()
                if not published:
                    with self._hands_free_duck_lock:
                        self._hands_free_capture_duck_owners.discard(owner_token)
                    logger.debug('[DUCK] Capture duck start lost ownership: %s', owner_token)
                    return None
            self._bump_wake_gate_freeze()
            return owner_token
        except Exception as exc:
            with self._hands_free_duck_lock:
                ducker_to_stop = ducker if started_here else None
                if start_generation == self._hands_free_capture_duck_start_generation:
                    self._hands_free_capture_duck_starting = False
                self._hands_free_capture_duck_owners.discard(owner_token)
            logger.warning(f"[DUCK] Failed to engage capture duck: {exc}")
            if ducker_to_stop is not None:
                try:
                    ducker_to_stop.stop()
                except Exception:
                    pass
            return None

    def _close_hands_free_capture_duck(self, owner_token: int | None = None) -> None:
        """Schedule the deep duck's release after
        _HANDS_FREE_CAPTURE_DUCK_RESTORE_DELAY_S -- NOT immediate, so rapid
        consecutive utterances reuse the still-active duck (the pending timer
        is cancelled by the next _open_hands_free_capture_duck) instead of a
        stop-then-immediately-restart flicker. Call in a finally-shape at
        every close-capable path (see wake_consumer.py) so this always fires,
        even if processing raised."""
        try:
            with self._hands_free_duck_lock:
                if owner_token is None or owner_token not in self._hands_free_capture_duck_owners:
                    logger.debug('[DUCK] Ignoring stale capture duck close: %s', owner_token)
                    return
                self._hands_free_capture_duck_owners.remove(owner_token)

                if (
                    self._hands_free_capture_ducker is None
                    and not self._hands_free_capture_duck_starting
                ):
                    timer = self._hands_free_duck_restore_timer
                    self._hands_free_duck_restore_timer = None
                    if timer is not None:
                        timer.cancel()
                    return

                if self._hands_free_capture_duck_owners:
                    timer = self._hands_free_duck_restore_timer
                    self._hands_free_duck_restore_timer = None
                    if timer is not None:
                        timer.cancel()
                    return

                existing = self._hands_free_duck_restore_timer
                if existing is not None:
                    existing.cancel()

                self._hands_free_capture_duck_restore_generation += 1
                restore_generation = self._hands_free_capture_duck_restore_generation
                self._hands_free_capture_duck_restore_token = object()
                restore_token = self._hands_free_capture_duck_restore_token
                self._hands_free_duck_restore_timer = thread_registry.timer(
                    "hands_free_capture_duck.restore",
                    _HANDS_FREE_CAPTURE_DUCK_RESTORE_DELAY_S,
                    self._restore_hands_free_capture_duck_now,
                    args=(restore_generation, restore_token),
                )
        except Exception as exc:
            logger.warning(f"[DUCK] Failed to schedule capture duck restore: {exc}")

    def _restore_hands_free_capture_duck_now(
        self,
        restore_generation: int,
        restore_token: object,
    ) -> None:
        """Timer callback: actually stop() the capture ducker, restoring its
        tracked sessions back to whatever was playing when it started (idle
        level, if the wake-word toggle is still on; true full volume
        otherwise)."""
        try:
            with self._hands_free_duck_lock:
                if (
                    restore_generation != self._hands_free_capture_duck_restore_generation
                    or restore_token is not self._hands_free_capture_duck_restore_token
                ):
                    return
                self._hands_free_duck_restore_timer = None
                if self._hands_free_capture_duck_owners:
                    return
                ducker = self._hands_free_capture_ducker
                self._hands_free_capture_ducker = None

            if ducker is None:
                return
            self._log_duck_result(ducker, "capture duck restored")
            ducker.stop()
            self._bump_wake_gate_freeze()
        except Exception as exc:
            logger.warning(f"[DUCK] Failed to restore capture duck: {exc}")

    def wake_ready_state(self) -> str:
        """'off' (wake models never requested), 'loading' or 'ready'. Shown by
        the tray tooltip/menu and the splash's final detail line. An app
        built without the lazy-load state (test doubles) reports 'ready'."""
        if not hasattr(self, '_wake_models_lock'):
            return 'ready'
        return self._wake_models_state

    def _request_wake_models(self, start_listening: bool) -> bool:
        """Ensure the OpenWakeWord detectors are (being) loaded. Returns True
        when they are ready now. Otherwise starts the loader thread if it is
        not already running and, if start_listening, records the request
        under the same lock the loader reads it with, so a load finishing
        concurrently can never drop it."""
        if not hasattr(self, '_wake_models_lock'):
            return True
        with self._wake_models_lock:
            if self._wake_models_state == 'ready':
                return True
            if start_listening:
                self._wake_start_pending = True
            if self._wake_models_state == 'loading':
                return False
            self._wake_models_state = 'loading'
        logger.info("[WAKE] Loading wake word models on their own thread "
                    "(listening starts when ready)")
        thread_registry.spawn("dictation.wake_models_load",
                              self._wake_models_load_worker, daemon=True)
        self._publish_wake_state()
        return False

    def _cancel_pending_wake_start(self) -> None:
        if hasattr(self, '_wake_models_lock'):
            with self._wake_models_lock:
                self._wake_start_pending = False

    def _wake_models_load_worker(self) -> None:
        _t = time.perf_counter()
        try:
            self._load_oww_model()
            self._load_wake_profile_models()
            # The phrase may have changed while loading (update_config skips
            # rebuilding a detector that doesn't exist yet) -- honour it.
            phrase = self.config.get('wake_word_config', {}).get('phrase', 'jarvis')
            detector = self._wake_detector
            if detector is not None and phrase.lower().strip() != detector._wake_phrase:
                oww_threshold = float(self.config.get('wake_word_config', {}).get('oww_threshold', 0.2))
                self._wake_detector = WakeWordDetector(phrase, threshold=oww_threshold)
        except Exception as exc:
            # Detector construction never raises by contract; if something
            # else did, wake listening still works via the Whisper fallback.
            logger.exception(f"[WAKE] Wake model load failed: {exc}")
        with self._wake_models_lock:
            self._wake_models_state = 'ready'
            start = self._wake_start_pending
            self._wake_start_pending = False
        self.wake_ready.set()
        logger.info(f"[WAKE] wake_ready: models loaded in "
                    f"{(time.perf_counter() - _t) * 1000:.0f}ms")
        self._publish_wake_state()
        if start and not self.wake_word_active:
            self.start_wake_word_mode()

    def _publish_wake_state(self) -> None:
        """Push wake loading/ready to the tray tooltip and indicator label."""
        try:
            self._update_tray_tooltip()
            if getattr(self, 'listening_indicator', None) is not None:
                self._schedule_ui(self.listening_indicator.set_mode, self._get_mode_display())
        except Exception as exc:
            logger.debug(f"[WAKE] Could not publish wake state: {exc}")

    def start_wake_word_mode(self):
        """Start wake word listening — always listening for wake word."""
        if not self.model_loaded:
            if self.loading_model:
                logger.info("Model still loading, please wait...")
            return
        # OpenWakeWord loads lazily on first use; the loader calls back into
        # here once the detectors exist (earcon + ACTIVE state only then).
        if not self._request_wake_models(start_listening=True):
            return

        self._start_hands_free_idle_duck()
        self.play_sound("start", use_winsound=True)
        time.sleep(0.15)
        phrase = self.config.get('wake_word_config', {}).get(
            'phrase', config_defaults.DEFAULTS['wake_word_config.phrase'])
        logger.info(f"[LISTEN] Wake word mode ACTIVE - say '{phrase}' to give commands")
        self._warn_wake_fallback_once()

        self.silence_start       = None
        self.is_speaking         = False
        self.wake_word_triggered = False

        # ACE path: WakeConsumer polls the ring — no separate PortAudio stream.
        # Engine and wake consumer share the same device, no stream conflict.
        # Reason-counted: a toggle session may already hold the pipeline
        # open (see _ensure_wake_consumer) -- this must not double-start it,
        # and must not let a later toggle-session exit stop it out from
        # under wake detection.
        self._ensure_wake_consumer('wake_word')

        self.set_app_state(wake_word_active=True)
        self._request_icon_chase('wake_word')
        # Arming is NOT capturing (42): the indicator goes ARMED -- open eye,
        # slow turn, no capture pill, no listening pulse. It used to call
        # set_listening(True) here too, which lit the pill as if the record
        # hotkey were held and stayed lit until a real capture's release
        # cleared it. Only an actual capture (hotkey, wake phrase heard,
        # wake session, continuous) sets listening.
        if getattr(self, 'listening_indicator', None) is not None:
            self._schedule_ui(self.listening_indicator.set_wake_armed, True)

        if hasattr(self, 'hints'):
            self.hints.maybe_show(
                'wake_mode_activated',
                "Wake word active. Say 'Jarvis' followed by a command."
                " Try 'Jarvis, show numbers' to click by voice.",
                delay_s=2.0,
            )

    def stop_wake_word_mode(self):
        """Stop wake word listening mode."""
        self._cancel_pending_wake_start()
        self.set_app_state(wake_word_active=False)
        self.wake_word_triggered = False
        self._stop_hands_free_idle_duck()
        self._reset_wake_dictation()

        # Reason-counted release: only actually stops the consumer if no
        # toggle session is also holding it open (see _release_wake_consumer).
        self._release_wake_consumer('wake_word')

        logger.info("[OFF] Wake word mode STOPPED")
        self.play_sound("stop")
        self._release_icon_chase('wake_word')
        if getattr(self, 'listening_indicator', None) is not None:
            self._schedule_ui(self.listening_indicator.set_wake_armed, False)
            # A wake-owned capture (phrase heard, wake session, wake
            # dictation) ends with wake mode -- _reset_wake_dictation above
            # does not touch the indicator -- but a hotkey recording in
            # progress is not wake's to clear; its release does that.
            if not getattr(self, 'recording', False):
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
            if getattr(self, 'wake_word_active', False):
                self._warn_wake_fallback_once()

    def _warn_wake_fallback_once(self):
        """One loud notice per app instance when hands-free lacks its OWW filter."""
        detector = getattr(self, '_wake_detector', None)
        if detector is not None and detector.is_available:
            return
        if getattr(self, '_wake_fallback_warned', False):
            return
        self._wake_fallback_warned = True
        phrase = self.config.get('wake_word_config', {}).get('phrase', 'jarvis')
        logger.warning("[OWW] No pre-filter for '%s' -- Whisper wake fallback decodes all room audio",
                       phrase)
        flight_recorder.record('wake.fallback_active', phrase=phrase, detector='whisper')

    def _warn_wake_profile_fallback_once(self, tid, phrase):
        """One loud notice per profile lacking a usable OWW pre-filter model."""
        if tid in self._wake_profile_fallback_warned:
            return
        self._wake_profile_fallback_warned.add(tid)
        logger.warning(
            "[OWW] Wake profile '%s' (%s) has no pre-filter model -- Whisper "
            "wake fallback decodes all room audio for it", tid, phrase,
        )
        flight_recorder.record('wake.fallback_active', phrase=phrase, detector='whisper', profile=tid)

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
                if not detector.is_available and getattr(self, 'wake_word_active', False):
                    self._warn_wake_profile_fallback_once(tid, phrase)
            else:
                self._wake_profile_detectors[tid] = None
                missing = f" ('{model_file}' not in wake_models/)" if model_file else ""
                logger.debug(f"[OWW] Wake profile '{tid}' ({phrase}): no model{missing} — Whisper fallback")
                if getattr(self, 'wake_word_active', False):
                    self._warn_wake_profile_fallback_once(tid, phrase)

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

    def _confirm_wake_capture(self):
        """Publish the confirmed-wake boundary for the ring consumer."""
        from samsara.audio_engine.wake_consumer import wake_session_policy
        policy = wake_session_policy(self.config)
        self._wake_capture_admission = (
            time.perf_counter(), policy['prebuffer_policy'], policy['post_wake_guard_ms'],
        )

    def _start_wake_session(self, initial_content=None, mode='focus_dictate', send_word=None):
        """Enter a wake session bounded by inactivity and an absolute duration.

        Each transcribed utterance is delivered immediately.  The session stays
        alive until the shorter of the inactivity timeout and max_session_s,
        or until the user sends/cancels. Only new Silero onsets extend inactivity.

        mode: 'focus_dictate' — press Enter after send-word detection (claude profiles).
              'stage_send' — text staged, Enter suppressed (hermes/agentic profiles).
        send_word: the dispatching profile's own terminator word. Stored on
              self for the duration of this session so the termination check
              (see process_wake_word_buffer's wake_session branch) is scoped
              to exactly this profile, not the shared/global send_words list
              -- profile isolation for the agentic-safety send_word contract.
        """
        with self._wake_session_lock:
            self._confirm_wake_capture()
            from samsara.audio_engine.wake_consumer import wake_session_policy
            policy = wake_session_policy(self.config)
            self._wake_session_started_at = time.monotonic()
            self._wake_session_deadline = self._wake_session_started_at + policy['max_session_s']

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
                  f"inactivity timeout: {policy['inactivity_timeout_s']}s, "
                  f"maximum session: {policy['max_session_s']}s, "
                  f"mode: {mode})")

            self._restart_wake_session_timer(initial=True)
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

    def _restart_wake_session_timer(self, *, initial=False, speech_onset=False):
        """Arm on confirmation/onset, never beyond the fixed session deadline."""
        with self._wake_session_lock:
            if self.app_state != 'wake_session' or not (initial or speech_onset):
                return
            if not initial and self._expire_wake_session():
                return
            from samsara.audio_engine.wake_consumer import wake_session_policy
            now = time.monotonic()
            policy = wake_session_policy(self.config)
            expires_at = min(self._wake_session_deadline, now + policy['inactivity_timeout_s'])
            existing = getattr(self, '_wake_session_inactivity_timer', None)
            if existing is not None:
                existing.cancel()
            self._wake_session_expires_at = expires_at
            timer_token = object()
            self._wake_session_timer_token = timer_token
            t = thread_registry.timer(
                "dictation.wake_session_timeout", max(0.0, expires_at - now),
                lambda started_at=self._wake_session_started_at: self._expire_wake_session(
                    expected_started_at=started_at, expected_timer_token=timer_token), daemon=True)
            self._wake_session_inactivity_timer = t

    def _expire_wake_session(self, expected_started_at=None, expected_timer_token=None):
        """Check from both the timer and live frames, even during continuous speech."""
        with self._wake_session_lock:
            if self.app_state != 'wake_session':
                return False
            if (expected_timer_token is not None
                    and expected_timer_token is not self._wake_session_timer_token):
                return False
            if (expected_started_at is not None
                    and expected_started_at != self._wake_session_started_at):
                return False
            expires_at = getattr(self, '_wake_session_expires_at', None)
            deadline = getattr(self, '_wake_session_deadline', None)
            now = time.monotonic()
            if expires_at is None or deadline is None or now < expires_at:
                return False
            reason = ('max_session_s' if now >= deadline else 'inactivity_timeout_s')
            self._end_wake_session(reason=reason)
            return True

    def _end_wake_session(self, reason='ended'):
        """Close admission; wake detection requires a new Silero onset."""
        with self._wake_session_lock:
            import traceback as _tb
            logger.info(
                f"[WS-DIAG] end_wake_session ENTERED: app_state={self.app_state!r} "
                f"caller={_tb.extract_stack()[-2].name}"
            )
            existing = getattr(self, '_wake_session_inactivity_timer', None)
            if existing is not None:
                existing.cancel()
                self._wake_session_inactivity_timer = None
            logger.info("[WAKE-SESSION] ended (%s)", reason)
            flight_recorder.record('wake.session_close', reason=reason)
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
        return bool(np.any(probabilities > LIVE_VAD_PROB_THRESHOLD))

    def _vad_reset(self):
        """Compatibility hook for utterance-boundary VAD cleanup.

        faster-whisper's bundled ONNX wrapper initializes its h/c/context
        arrays inside every call, so there is no recurrent state to clear.
        Existing callers retain this hook to keep their cleanup paths stable.
        """
        return None

    def _buffer_should_skip_decode(self, audio, src_rate, *, head_grace_ms=0.0):
        """Presence gate for a finished hotkey capture -> _GateDecision (truthy = skip).

        Three paths, all deciding on the WHOLE capture (39):
          short   <= _GATE_MAX_BUFFER_S: one VAD call over the buffer -- the
                  original fast path, unchanged in behaviour;
          loud    longer, overall RMS >= _SANITY_RMS_FLOOR_DB: audible
                  dictation, decoded without touching the VAD lock;
          chunked longer and quiet: every _GATE_MAX_BUFFER_S chunk is scanned,
                  the longest contiguous speech run is tracked across chunk
                  boundaries, and the capture is skipped only if no run anywhere
                  reaches _GATE_MIN_CONTIG_MS.
        """
        buffer_s = len(audio) / src_rate
        if buffer_s <= _GATE_MAX_BUFFER_S:
            return self._gate_scan(audio, src_rate, head_grace_ms=head_grace_ms, path="short")
        rms = float(np.sqrt(np.mean(np.asarray(audio, dtype=np.float32) ** 2)))
        rms_db = 20.0 * math.log10(rms + 1e-12)
        if not rms < 10 ** (_SANITY_RMS_FLOOR_DB / 20.0):
            decision = _GateDecision(skip=False, path="loud", buffer_s=buffer_s, rms_db=rms_db,
                                     head_grace_ms=head_grace_ms)
            logging.getLogger("Samsara").debug(decision.describe())
            return decision
        return self._gate_scan(audio, src_rate, head_grace_ms=head_grace_ms, path="chunked",
                               chunk_s=_GATE_MAX_BUFFER_S, rms_db=rms_db)

    def _gate_scan(self, audio, src_rate, *, head_grace_ms, path, chunk_s=None, rms_db=None):
        """Run the contiguous-speech scan for the presence gate and log the decision."""
        run = self._speech_run_scan(audio, src_rate, min_ms=_GATE_MIN_CONTIG_MS,
                                    prob_threshold=_GATE_VAD_PROB, head_grace_ms=head_grace_ms,
                                    chunk_s=chunk_s)
        decision = _GateDecision(
            skip=not run.passed, path=path, buffer_s=len(audio) / src_rate, rms_db=rms_db,
            head_grace_ms=head_grace_ms, best_ms=run.best_ms, offset_s=run.offset_s,
            scanned_s=run.scanned_s, chunks=run.chunks, method=run.method, failed_open=run.failed_open,
        )
        if not decision.skip:
            logging.getLogger("Samsara").debug(decision.describe())
        return decision

    def _speech_run_scan(self, audio, src_rate, *, min_ms=_GATE_MIN_CONTIG_MS,
                         prob_threshold=_GATE_VAD_PROB, head_grace_ms=0.0, chunk_s=None):
        """Longest contiguous speech run anywhere in `audio` -> _SpeechRun.

        chunk_s=None scans in one VAD call (the original behaviour). With
        chunk_s, the 16 kHz buffer is split into whole-frame chunks of that
        length and the lock is taken per chunk, so a long quiet capture never
        holds the VAD lock for its whole duration; the run counter and its
        start carry across chunk boundaries, and head grace applies to the
        buffer's first head_grace_ms only. VAD unavailable or failing -> the
        ZCR/energy fallback over the whole buffer; that failing -> fail OPEN.
        """
        buffer_s = len(audio) / src_rate
        if not self._vad_available or self._vad_model is None:
            return self._zcr_speech_run(audio, src_rate, min_ms=min_ms)

        chunk_16k = resample_audio(audio, src_rate, 16000)
        if chunk_16k.ndim > 1:
            chunk_16k = chunk_16k.flatten()

        window_size = 512
        frame_ms = window_size / 16000 * 1000.0  # 32ms per Silero frame
        min_contig_frames = max(1, int(min_ms / frame_ms))
        grace_frames = max(0, int(round(head_grace_ms / frame_ms)))
        step = len(chunk_16k) if chunk_s is None else max(window_size, int(chunk_s * 16000) // window_size * window_size)

        contig = 0
        run_start = 0
        best_contig = 0
        best_start = 0
        idx = 0
        chunks = 0
        try:
            for begin in range(0, max(1, len(chunk_16k)), max(1, step)):
                with self._vad_lock:
                    probabilities = self._vad_probabilities(chunk_16k[begin:begin + step])
                chunks += 1
                for speech_prob in probabilities:
                    if speech_prob > prob_threshold:
                        if contig == 0:
                            run_start = idx
                        contig += 1
                        if contig > best_contig:
                            best_contig, best_start = contig, run_start
                    elif idx < grace_frames:
                        pass  # head grace: low reading in the known noisy span -- neutral, not a break
                    else:
                        contig = 0
                    idx += 1
        except Exception as e:
            logger.exception(f"[VAD] ONNX gate inference failed, using ZCR fallback: {e}")
            return self._zcr_speech_run(audio, src_rate, min_ms=min_ms)

        return _SpeechRun(
            passed=best_contig >= min_contig_frames, best_ms=round(best_contig * frame_ms),
            offset_s=best_start * frame_ms / 1000.0, scanned_s=idx * frame_ms / 1000.0,
            chunks=chunks, method="vad", failed_open=False, buffer_s=buffer_s,
        )

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
        run_start = 0
        best_contig = 0
        best_start = 0
        for idx, speech_prob in enumerate(probabilities):
            if speech_prob > prob_threshold:
                if contig == 0:
                    run_start = idx
                contig += 1
                if contig > best_contig:
                    best_contig, best_start = contig, run_start
            elif idx < grace_frames:
                pass  # head grace: low reading in the known noisy span -- neutral, not a break
            else:
                contig = 0

        passed = best_contig >= min_contig_frames
        if passed:
            # Evidence trail for the next leak: a gate PASS is otherwise
            # invisible, so there's no ground truth for why a given buffer
            # reached Whisper. Where the run was found makes it checkable (39).
            logging.getLogger("Samsara").debug(
                "[GATE] pass: max contiguous speech %dms at %.2fs (buffer %.1fs)%s",
                round(best_contig * frame_ms), best_start * frame_ms / 1000.0, len(audio) / src_rate,
                f", head_grace={head_grace_ms:.0f}ms" if head_grace_ms > 0 else "",
            )
        return passed

    def _zcr_speech_run(self, audio, src_rate, min_ms=_GATE_MIN_CONTIG_MS):
        """_SpeechRun from the ZCR/energy fallback (whole buffer, one pass)."""
        buffer_s = len(audio) / src_rate
        run = self._zcr_energy_contiguous_speech(audio, src_rate, min_ms=min_ms, _detail=True)
        if not isinstance(run, tuple):          # failed open / too short to analyse
            return _SpeechRun(passed=bool(run), best_ms=0, offset_s=0.0, scanned_s=0.0, chunks=0,
                              method="zcr", failed_open=True, buffer_s=buffer_s)
        passed, best_ms, offset_s, scanned_s = run
        return _SpeechRun(passed=passed, best_ms=best_ms, offset_s=offset_s, scanned_s=scanned_s,
                          chunks=1, method="zcr", failed_open=False, buffer_s=buffer_s)

    def _zcr_energy_contiguous_speech(self, audio, src_rate,
                                       min_ms=_GATE_MIN_CONTIG_MS,
                                       zcr_low=0.02, zcr_high=0.30, _detail=False):
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
            run_start = 0
            best_contig = 0
            best_start = 0
            for idx, is_speech in enumerate(speech_like):
                if is_speech:
                    if contig == 0:
                        run_start = idx
                    contig += 1
                    if contig > best_contig:
                        best_contig, best_start = contig, run_start
                else:
                    contig = 0

            passed = best_contig >= min_contig_frames
            if passed and not _detail:
                logging.getLogger("Samsara").debug(
                    "[GATE] pass: max contiguous speech %dms at %.2fs (buffer %.1fs) [ZCR fallback]",
                    round(best_contig * frame_ms), best_start * frame_ms / 1000.0, len(audio) / src_rate,
                )
            if _detail:
                return (passed, round(best_contig * frame_ms), best_start * frame_ms / 1000.0,
                        n_windows * frame_ms / 1000.0)
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

    #: Minimum sample for a wake floor (48). A frame is one ACE ring frame:
    #: FRAME_MS (100 ms) of 16 kHz audio, so 3 s should yield 30. The floor is
    #: a median of per-frame RMS; below 80 % of the expected frames the window
    #: had a stall (the 2026-09-14 21:02 run wrote a floor from 5 frames while
    #: the process was already dying), and never fewer than 20 frames (2 s),
    #: or one noise burst moves the median. Below either: nothing is written.
    _WAKE_CAL_MIN_FRACTION = 0.8
    _WAKE_CAL_MIN_FRAMES = 20

    def calibrate_wake_mic(self, seconds: float = 3.0,
                           cancel_event=None) -> float | None:
        """Sample ambient audio for *seconds* and seed the adaptive noise floor.

        Uses the ACE engine ring (via a temporary registered consumer) so no
        second InputStream is opened.  The temporary reader starts at the live
        write head so calibration measures a fresh, full quiet interval.

        Returns the measured floor RMS, or None when nothing was written:
        engine not running, cancelled, or too small a sample (see
        _WAKE_CAL_MIN_FRACTION / _WAKE_CAL_MIN_FRAMES). Every outcome leaves
        self.last_wake_calibration = {status, frames, expected_frames,
        min_frames, seconds, floor, message} for the UI to show.
        Persists a successful result to
        wake_word_config.audio.measured_noise_floor so the floor survives a
        restart and seeds the EMA on next boot. When cancel_event is
        supplied, cancellation returns None without changing or persisting
        the current floor.
        """
        import logging as _log
        from samsara.audio_engine.frame import FRAME_MS as _FRAME_MS, SAMPLE_RATE as _FRAME_RATE

        expected_frames = max(1, int(round(seconds * 1000 / _FRAME_MS)))
        min_frames = max(self._WAKE_CAL_MIN_FRAMES, math.ceil(expected_frames * self._WAKE_CAL_MIN_FRACTION))

        def _report(status, frames=0, samples=0, floor=None, message=""):
            self.last_wake_calibration = {
                "status": status, "frames": frames, "expected_frames": expected_frames,
                "min_frames": min_frames, "seconds": samples / _FRAME_RATE, "floor": floor,
                "message": message,
            }

        engine = getattr(self, '_ace_engine', None)
        if engine is None or not engine._running:
            _log.getLogger().warning("[CAL] ACE engine not running — calibrate_wake_mic has no audio source")
            _report("unavailable", message="Background calibration was unavailable: the audio engine is not "
                                           "running. Nothing was saved.")
            return None

        reader = engine.register_consumer("wake-calibration")
        from samsara.audio_engine.ring import EMPTY as _EMPTY

        rms_values = []
        samples = 0
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
                samples += len(chunk)
                rms_values.append(float(np.sqrt(np.mean(chunk ** 2))))
        finally:
            engine.unregister_consumer(reader)

        if cancelled:
            _log.getLogger().info("[CAL] Wake mic calibration cancelled")
            _report("cancelled", len(rms_values), samples, message="Calibration cancelled. Nothing was saved.")
            return None

        if len(rms_values) < min_frames:
            got_s = samples / _FRAME_RATE
            message = (f"Calibration refused: only {len(rms_values)} of the {expected_frames} expected "
                       f"{_FRAME_MS} ms frames arrived ({got_s:.1f} s of audio; at least {min_frames} "
                       f"are needed). Nothing was saved -- try again.")
            _log.getLogger().warning(f"[CAL] {message}")
            _report("insufficient", len(rms_values), samples, message=message)
            return None

        measured = float(np.median(rms_values))
        measured = max(measured, _NOISE_FLOOR_MIN)
        self._wake_noise_floor = measured
        _report("ok", len(rms_values), samples, floor=measured)
        _log.getLogger().info(f"[CAL] Wake mic calibrated: floor={measured:.5f} ({len(rms_values)} frames, "
                              f"{samples / _FRAME_RATE:.1f} s)")

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

        # 2026-09-14 live-log incident: inside an open session the adaptive
        # gate (floor 0.0088 x1.5 = 0.0132) sat above the owner's speech
        # (0.004-0.010) and discarded every capture, end/cancel words
        # included. Once the user has deliberately woken the app, skip only
        # near-silence. Returns before the EMA update: in-session buffers are
        # the user's voice, not ambient, same reasoning as the OWW bypass.
        from samsara.audio_engine.wake_consumer import (
            BYPASS_ADAPTIVE_GATE_IN_SESSION_KEY, in_session_rms_gate,
            wake_capture_session_open,
        )
        session_open = wake_capture_session_open(self)
        if session_open and config_defaults.cfg_get(self.config, BYPASS_ADAPTIVE_GATE_IN_SESSION_KEY):
            skip, threshold, rule = in_session_rms_gate(audio_rms)
            logger.info(
                f"[WAKE-GATE] state={self.app_state} rms={audio_rms:.4f} threshold={threshold:.4f} "
                f"rule={rule} decision={'skip' if skip else 'pass'}"
            )
            return skip

        ww_config = self.config.get('wake_word_config', {})
        audio_config = ww_config.get('audio', {})
        use_adaptive = audio_config.get('adaptive_gate', True)
        if use_adaptive:
            # Update rolling noise-floor estimate only from buffers that have
            # not already been identified as wake speech by OpenWakeWord --
            # and (2026-07-24) only while the gate is NOT frozen: a duck
            # transition (idle or capture engaging/releasing) or active TTS
            # steps the ambient media volume abruptly, and letting the EMA
            # chase that step means the eventual restore back up reads as a
            # fresh speech onset (see _wake_gate_frozen/_bump_wake_gate_
            # freeze). The very first-ever seed still happens regardless of
            # freeze state -- there is no "chase" risk with no prior floor
            # to protect, and the gate needs SOME floor to make any
            # decision at all.
            if self._wake_noise_floor is None:
                self._wake_noise_floor = max(audio_rms, _NOISE_FLOOR_MIN)
            elif (
                not self._wake_gate_frozen()
                and audio_rms < self._wake_noise_floor * _NOISE_FLOOR_SPEECH_RATIO
            ):
                self._wake_noise_floor = max(
                    (1.0 - _NOISE_FLOOR_ALPHA) * self._wake_noise_floor
                    + _NOISE_FLOOR_ALPHA * audio_rms,
                    _NOISE_FLOOR_MIN,
                )

            gate_level = max(self._wake_noise_floor * _SPEECH_FLOOR_RATIO, _ABS_FLOOR_MIN)
            if session_open:
                # Bypass disabled by config: the pre-fix rule still decides,
                # but an in-session skip is never silent.
                logger.info(
                    f"[WAKE-GATE] state={self.app_state} rms={audio_rms:.4f} threshold={gate_level:.4f} "
                    f"rule=adaptive decision={'skip' if audio_rms < gate_level else 'pass'}"
                )
            if audio_rms < gate_level:
                logging.debug(
                    f"[WAKE] gated (rms {audio_rms:.4f} < adaptive {gate_level:.4f}"
                    f" [floor {self._wake_noise_floor:.4f} x{_SPEECH_FLOOR_RATIO}]) -- skipping"
                )
                return True
        else:
            speech_threshold = audio_config.get('speech_threshold', DEFAULT_SPEECH_THRESHOLD)
            if session_open:
                logger.info(
                    f"[WAKE-GATE] state={self.app_state} rms={audio_rms:.4f} threshold={speech_threshold:.4f} "
                    f"rule=fixed_speech_threshold decision={'skip' if audio_rms < speech_threshold else 'pass'}"
                )
            if audio_rms < speech_threshold:
                logging.debug(
                    f"[WAKE] Below speech threshold (RMS {audio_rms:.4f} < {speech_threshold:.4f}), skipping"
                )
                return True
        return False

    def process_wake_word_buffer(self, buffer, src_rate=None, *, oww_confirmed=False,
                                 owner_token=None, tracked=False):
        """Transfer one utterance to the bounded wake FIFO.

        The capture duck protects recording, not Whisper decoding. Release
        its owner before enqueueing so a queued decode cannot keep media low.
        """
        src_rate = self.capture_rate if src_rate is None else src_rate
        with self._wake_session_lock:
            session_started_at = (
                getattr(self, '_wake_session_started_at', None)
                if self.app_state == 'wake_session' else None
            )
        if tracked:
            with self._dictation_finalize_lock:
                self._pending_transcriptions += 1

        if owner_token is not None:
            self._close_hands_free_capture_duck(owner_token)

        def finish():
            if tracked:
                with self._dictation_finalize_lock:
                    self._pending_transcriptions = max(0, self._pending_transcriptions - 1)
                self._maybe_finalize_dictation()

        def decode():
            if not self.wake_word_active:
                logger.info('[WAKE] Listener stopped -- discarding queued utterance')
                flight_recorder.record('wake.dispatch_dropped', reason='listener_stopped')
                return
            if session_started_at is not None:
                with self._wake_session_lock:
                    self._expire_wake_session()
                    if (self.app_state != 'wake_session'
                            or self._wake_session_started_at != session_started_at):
                        logger.info('[WAKE] Session ended -- discarding queued utterance')
                        flight_recorder.record('wake.dispatch_dropped', reason='session_ended')
                        return
            flight_recorder.record('session.dispatch', op='started', kind='wake_buffer')
            try:
                self._decode_wake_word_buffer(buffer, src_rate, oww_confirmed=oww_confirmed)
            finally:
                flight_recorder.record('session.dispatch', op='completed', kind='wake_buffer')

        flight_recorder.record('session.dispatch', op='enqueued', kind='wake_buffer',
                               thread='wake-utt-queue', oww_confirmed=oww_confirmed)
        self._wake_dispatch_queue.enqueue(decode, finish)

    def _decode_wake_word_buffer(self, buffer, src_rate=None, *, oww_confirmed=False):
        """Process audio — check for wake word, commands, or dictation content.

        src_rate: sample rate of audio in buffer (default: self.capture_rate).
                  Pass SAMPLE_RATE (16kHz) when buffer comes from the ACE ring.
        """
        if src_rate is None:
            src_rate = self.capture_rate
        session_started_at = (
            getattr(self, '_wake_session_started_at', None)
            if self.app_state == 'wake_session' else None
        )
        token = self._transcription_owners.claim('wake')
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

            # The wake FIFO preserves capture order; model_lock serializes
            # Whisper against the independent toggle, Ava and hold lanes.

            # Get transcription parameters based on performance mode. NOT
            # forced to English: this is the wake/_output_dictation lane --
            # transcribes with the configured dictation language. The
            # cancel/send/end/pause/resume control words checked below are
            # small fixed English word lists, best-effort in non-English
            # (commands/control-words remain English-only by design).
            # include_vocabulary=False: free-form dictation, decode-matrix-
            # established (SPARK 2026-07-17/18, N=10/cell) to be destabilized
            # by vocabulary content in initial_prompt -- see
            # voice_training_qt.get_initial_prompt.
            transcribe_params = self.get_transcription_params(include_vocabulary=False)
            # When Silero is unavailable we relied on RMS to gate speech into
            # the buffer. Whisper's own vad_filter then strips quiet audio a
            # second time — on low-gain mics this removes everything, producing
            # empty transcription. Disable it so RMS-gated audio passes through.
            if not self._vad_available:
                transcribe_params['vad_filter'] = False
            perf_mode = self.config.get('performance_mode', 'balanced')

            transcribe_start = time.time()
            with self.model_lock:
                segments, info = self.model.transcribe(audio, **transcribe_params)
                # Whisper returns a lazy iterator: decoding runs while it is
                # consumed, so the cross-lane lock must cover iteration too.
                _seg_list = list(segments)
            text = "".join([segment.text for segment in _seg_list]).strip()
            transcribe_time = time.time() - transcribe_start

            if session_started_at is not None:
                self._expire_wake_session()
                if (self.app_state != 'wake_session'
                        or getattr(self, '_wake_session_started_at', None) != session_started_at):
                    flight_recorder.record('session.dispatch', op='dropped',
                                           kind='wake_buffer', reason='session_ended')
                    return

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
            text = self._filter_dictation_language(text, info)
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
            wake_phrase = ww_config.get(
                'phrase', config_defaults.DEFAULTS['wake_word_config.phrase']).lower()

            self._emit_wake_trace({"stage": "utterance_start", "raw": text, "normalized": text_lower})

            # In dictation state (quick_dictation, long_dictation, or wake_session)?
            if self.app_state in ('quick_dictation', 'long_dictation', 'wake_session'):
                # Check abort words
                abort_words = ww_config.get(
                    'wake_abort_phrase', config_defaults.DEFAULTS['wake_word_config.wake_abort_phrase'])
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
                            # self.silence_start is otherwise owned by
                            # WakeConsumer's poll thread (see wake_consumer.py
                            # module docstring); this decode-completion thread
                            # must not stomp on it while the poll thread is
                            # mid-utterance -- that races a concurrent
                            # `time.time() - self.silence_start` read (a
                            # TypeError on a None it just wrote) and restarts
                            # the new utterance's silence window. Only the
                            # already-idle case (no active speech) is ours to
                            # clear, and it's already None there.
                            if not getattr(self, 'is_speaking', False):
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
                self._confirm_wake_capture()
                self._flash_tray_heard()
                if self._wake_opens_session():
                    # wake_word_config.opens_session: the wake phrase arms the
                    # latched hands-free session (SAMSARA_VISION.md section 1)
                    # instead of the one-command window below.
                    self._open_session_from_wake(corrected_lower[match_index + len(wake_phrase):])
                    self._emit_wake_trace({"stage": "utterance_end", "result": "wake_opened_session"})
                    return
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
            self._transcription_owners.release('wake', token)
            # Retain the utterance-boundary cleanup hook. The bundled ONNX VAD
            # is stateless between calls, so this is currently a no-op.
            self._vad_reset()

    def _wake_opens_session(self) -> bool:
        """wake_word_config.opens_session (default False): a wake-word hit
        opens the latched hands-free session instead of the one-command
        window. The latched session only exists in command_mode.mode
        'toggle'; with any other mode the flag is ignored (logged) and the
        wake word keeps its one-command window."""
        if not self.config.get('wake_word_config', {}).get('opens_session', False):
            return False
        if self.config.get('command_mode', {}).get('mode', 'hold') != 'toggle':
            logger.warning("[WAKE] wake_word_config.opens_session is set but command_mode.mode "
                           "is not 'toggle' -- no latched session to open; using the "
                           "one-command wake window")
            return False
        return True

    def _open_session_from_wake(self, trailing_text: str = "") -> None:
        """Open the latched hands-free session from a wake-word hit, through
        the SAME entry the command-mode toggle tap uses (enter_command_mode
        via _dispatch_session_transition), so it enters in the same lane with
        the same earcon, badge and inactivity timer. Idempotent: a wake word
        spoken inside an open session changes nothing. Words spoken after the
        wake phrase in the same utterance are not dispatched -- the session
        starts listening with the next utterance."""
        self.wake_word_triggered = False
        trailing = normalize_command_text(trailing_text or "")
        if trailing:
            logger.info(f"[WAKE] Opening hands-free session; not dispatching trailing text {trailing!r}")
        else:
            logger.info("[WAKE] Opening hands-free session")
        if self.command_mode_active:
            return
        self._dispatch_session_transition(self.enter_command_mode)

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
        with self._wake_session_lock:
            # Restore audio ducked by _duck_audio() at _start_wake_session().
            # This is the single common exit chokepoint for every wake-session
            # end path (inactivity timeout, send-word, explicit cancel -- see
            # this function's many call sites), unlike _end_wake_session()
            # which only covers the timeout path. Always safe: a no-op if
            # nothing was ducked.
            self._restore_audio()

            old_state = self.app_state
            if old_state == 'wake_session':
                self._wake_rearm_needs_onset = True
            self._wake_capture_admission = None
            self._wake_session_started_at = None
            self._wake_session_deadline = None
            self._wake_session_expires_at = None
            self._wake_session_timer_token = None
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
        # yet flushed by VAD silence would be lost. Routed through the
        # consumer that actually owns the buffer (WakeConsumer._utterance_
        # frames since the ACE migration) -- self.speech_buffer/buffer_lock
        # were the pre-ACE accumulator and nothing has appended to them
        # since; flushing them was a guaranteed no-op.
        consumer = getattr(self, '_wake_consumer', None)
        if consumer is not None:
            consumer.flush_utterance()

        # Try to finalize immediately. If transcriptions are still in flight,
        # this is a no-op and finalize will happen via the completion-side
        # call to _maybe_finalize_dictation in process_wake_word_buffer.
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

    # Typed Unicode injection is OFF by default (empty allowlist) as of
    # 2026-09-05. It was introduced (5b0ae81) for Chromium, where synthetic
    # Ctrl+V was believed to double-execute in rich web editors, and routed
    # per-process in a569cf8. Live evidence retired it: with SendInput
    # accepting every KEYEVENTF_UNICODE event, Brave's omnibox and <textarea>
    # received 2-3 of 47 chars (owner: "4 letters" for a 48-char dictation,
    # samsara.log 2026-09-05 15:09:57); survivors are periodic, ~1 char per
    # ~100ms of injection, and slowing to 15ms/char still lost 80%.
    # contenteditable landed intact in one probe run and 0/47 in the next.
    # Clipboard Ctrl+V landed 47/47 in every target in every run
    # (Documents\Claude\probe_inject_matrix.py + *.json). Warp's "CCCT"
    # (2026-08-02) is the same class, not "terminals discard VK=0".
    # Mechanism unconfirmed (Samsara's own LL hook sits in the injection
    # path; a Samsara-closed control run was not performed). Typed injection
    # stays available as an explicit opt-in via config
    # 'typed_injection_processes' (list of lowercase exe names).
    _TYPED_INJECTION_PROCESSES: frozenset = frozenset()

    # ── VERBATIM profile (samsara/verbatim.py) ──────────────────────────────
    # This class decides WHEN the profile applies; verbatim.py decides WHAT
    # the text becomes. Precedence, highest first:
    #   1. spoken toggle ("literal on" / "verbatim on")
    #   2. target-process list  (verbatim.processes)
    #   3. browser address bar  (UIA)
    #   4. normal -- the usual capitalise/clean/smart-correct pipeline

    def _consume_verbatim_toggle(self, text: str, *, feedback: bool = True) -> bool:
        """Flip the verbatim toggle if `text` is one of its phrases.

        True means the utterance WAS the toggle and must not be typed. Uses
        verbatim.match_toggle so the phrase table lives with the rest of the
        profile's tables."""
        try:
            from samsara import verbatim
            wanted = verbatim.match_toggle(text)
        except Exception as exc:
            logger.debug(f"[VERBATIM] toggle match failed: {exc}")
            return False
        if wanted is None:
            return False
        self.set_verbatim_forced(wanted)
        if feedback:
            try:
                self.play_sound('success')
            except Exception:
                pass
        return True

    def _verbatim_forced(self) -> bool:
        """The spoken toggle. Cleared when a hands-free session ends."""
        return bool(getattr(self, '_verbatim_force', False))

    def set_verbatim_forced(self, on: bool) -> None:
        self._verbatim_force = bool(on)
        logger.info("[VERBATIM] toggle %s", "ON" if on else "OFF")
        preview = getattr(self, '_dictate_preview', None)
        if preview is not None:
            try:
                preview.set_literal_badge(bool(on))
            except Exception as exc:
                logger.debug(f"[VERBATIM] preview badge failed: {exc}")

    def _verbatim_target_process(self) -> "str | None":
        """Focused process name when it is on the verbatim list, else None."""
        try:
            from samsara import verbatim
            configured = self.config.get('verbatim', {}).get('processes')
            name = self._foreground_process_name()
            if name and verbatim.matches_process(name, configured):
                return name
        except Exception as exc:
            logger.debug(f"[VERBATIM] process check failed: {exc}")
        return None

    def _verbatim_address_bar(self) -> bool:
        """True when the focused UIA element is a browser URL bar.

        Reads three properties off the focused control -- ControlTypeName,
        AutomationId, Name -- and hands them to verbatim.is_address_bar,
        which owns the matching table. Fails closed (False) if uiautomation
        is unavailable or the COM call raises."""
        if not self.config.get('verbatim', {}).get('address_bar', True):
            return False
        try:
            from samsara import verbatim
            import uiautomation as auto
            element = auto.GetFocusedControl()
            if element is None:
                return False
            return verbatim.is_address_bar(
                control_type=getattr(element, 'ControlTypeName', '') or '',
                automation_id=getattr(element, 'AutomationId', '') or '',
                name=getattr(element, 'Name', '') or '',
            )
        except Exception as exc:
            logger.debug(f"[VERBATIM] address-bar check unavailable: {exc}")
            return False

    def _verbatim_rule(self) -> "str | None":
        """Which rule puts this utterance in verbatim mode, or None.

        Returns the rule name for the one INFO line per utterance required
        when the profile is active. Order is the documented precedence."""
        if not self.config.get('verbatim', {}).get('enabled', True):
            return None
        if self._verbatim_forced():
            return "toggle"
        process = self._verbatim_target_process()
        if process:
            return f"process:{process}"
        if self._verbatim_address_bar():
            return "address_bar"
        return None

    def _apply_verbatim_if_active(self, text: str) -> "str | None":
        """Run the profile when a rule matches; None means 'not verbatim'.

        The single place the three finalize sites call -- each one skips its
        whole capitalise/clean/smart-correct/formatting-token pipeline when
        this returns a string, because every one of those steps is exactly
        what the profile exists to suppress."""
        if not text:
            return None
        rule = self._verbatim_rule()
        if rule is None:
            return None
        try:
            from samsara import verbatim
            result = verbatim.apply(text)
        except Exception as exc:
            logger.exception(f"[VERBATIM] transform failed, falling back to normal: {exc}")
            return None
        logger.info("[VERBATIM] rule=%s applied: %r -> %r", rule, text, result)
        return result

    def _foreground_process_name(self) -> str:
        return _paste.foreground_process_name()

    def _foreground_wants_typed_injection(self) -> bool:
        return _paste.foreground_wants_typed_injection(self)

    @staticmethod
    def _flight_foreground_process_name() -> str | None:
        return _paste.flight_foreground_process_name()

    def _paste_preserving_clipboard(self, text, before_paste=None, return_delivery_confirmation=False):
        # The implementation remains the injection_safety.window_integrity()
        # guarded paste_with_preservation(...) path and records successful
        # delivery through self._record_undoable_paste(...); the implementation
        # now lives in samsara.paste.
        return _paste.paste_preserving_clipboard(
            self,
            text,
            before_paste=before_paste,
            return_delivery_confirmation=return_delivery_confirmation,
            paste_with_preservation_fn=paste_with_preservation,
            type_text_unicode_fn=type_text_unicode,
            get_foreground_hwnd_fn=_get_foreground_hwnd,
        )

    def _deliver_text_to_focused_editor(self, text):
        return _paste.deliver_text_to_focused_editor(self, text)

    def _record_undoable_paste(self, text, target_hwnd=_UNDO_TARGET_UNSET):
        return _paste.record_undoable_paste(
            self,
            text,
            target_hwnd=target_hwnd,
            get_foreground_hwnd_fn=_get_foreground_hwnd,
        )

    def _arm_undo_timer(self):
        return _paste.arm_undo_timer(self)

    def _clear_undo(self):
        return _paste.clear_undo(self)

    def undo_last_dictation(self):
        return _paste.undo_last_dictation(
            self,
            get_foreground_hwnd_fn=_get_foreground_hwnd,
        )

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
        raw = text
        # VERBATIM profile: bypasses this whole pipeline (see
        # _apply_verbatim_if_active). _verbatim_active gates the steps below.
        _verbatim_text = self._apply_verbatim_if_active(text)
        _verbatim_active = _verbatim_text is not None
        if _verbatim_active:
            text = _verbatim_text
            t_corrections_ms = int((time.perf_counter() - _diag_corr_start) * 1000)
        else:
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
        if (not _verbatim_active
                and self.config.get('smart_corrections', {}).get('modes', {}).get('wake', True)):
            _diag_smart_start = time.perf_counter()
            _text_before_smart = text
            text = smart_correct(text, self)
            t_smart_ms = int((time.perf_counter() - _diag_smart_start) * 1000)
            smart_changed = (text != _text_before_smart)

        if not _verbatim_active:
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
                model_name=self.config.get('model_size', config_defaults.DEFAULTS['model_size']),
                device=getattr(self, 'device_type', 'unknown'),
                compute_type=self.config.get('compute_type', config_defaults.DEFAULTS['compute_type']),
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

    def play_sound(self, sound_type, use_winsound=False, volume=None):
        """Play audio feedback sound via persistent output stream (non-blocking, low-latency).

        Writes pre-loaded audio data into the playback buffer. The persistent
        OutputStream callback drains it automatically. New sounds replace any
        currently playing sound (clean cutoff, no artifacts).

        Args:
            sound_type: legacy ('start'|'stop'|'success'|'error') or any
                earcon name auto-discovered from the active theme directory
                (e.g. 'capture_started', 'thinking_pulse').
            use_winsound: Deprecated/ignored.
            volume: optional 0..1 override for THIS playback only. Used by
                the Settings Sounds tab's Test button so it plays at the
                slider's current, unsaved value. The saved sound_volume in
                config is never read or written when this is given.
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

        if volume is None:
            volume = self.config.get('sound_volume', 0.5)
        try:
            volume = min(max(float(volume), 0.0), 1.0)
        except (TypeError, ValueError):
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
        else:
            flight_recorder.record('ducker.op', op='start', noop=True, reason='disabled_in_config')

    def _restore_audio(self):
        """Counterpart to _duck_audio -- always safe to call even if
        ducking was never engaged (audio_ducking.restore() is a no-op when
        not currently ducked), so call sites don't need their own
        enabled-check on the way out."""
        audio_ducking.restore()

    def start_recording(self, streaming=None, play_earcon=True):
        """Serialize capture startup with release, cancellation and shutdown."""
        with self._hold_capture_lifecycle_lock:
            if (not self._running or self.recording
                    or getattr(self, '_streaming_session', None) is not None
                    or self._stop_in_flight or not self.model_loaded):
                return
            started = False
            try:
                started = self._start_recording_impl(streaming, play_earcon)
            finally:
                if not started:
                    try:
                        self.cancel_recording()
                    finally:
                        try:
                            if self._dictation_consumer is not None:
                                try:
                                    self._dictation_consumer.cancel()
                                finally:
                                    self._dictation_consumer.stop_streaming()
                        finally:
                            self._ace_dictation_active = False
                            self._ace_streaming_active = False
                            self._hotkey_recording = False
                            self._close_hold_capture_duck()

    def _open_hold_capture_duck(self):
        """Reuse capture ownership; wait for synchronous volume acknowledgements."""
        self._hold_capture_duck_confirmed_at = None
        cfg = self.config.get('ducking', {}) or {}
        if (not self.config.get('capture_duck_hold_enabled', True)
                or not cfg.get('hands_free_enabled', True)
                or float(cfg.get('hands_free_level', 0.15)) >= 1.0):
            return
        # Hands-free owners are positive. A fresh negative token prevents a
        # delayed close from an earlier hold from releasing a later one.
        self._hold_capture_duck_seq += 1
        token = -self._hold_capture_duck_seq
        self._hold_capture_duck_token = token
        started = time.perf_counter()
        confirmed = False
        try:
            if self._open_hands_free_capture_duck(token) != token:
                raise RuntimeError('Hold capture duck could not engage')
            # The shared helper returns early if another owner is still
            # starting. Do not mistake that token reservation for completion.
            deadline = time.perf_counter() + 2.0
            while True:
                with self._hands_free_duck_lock:
                    ducker = self._hands_free_capture_ducker
                    starting = self._hands_free_capture_duck_starting
                    owned = token in self._hands_free_capture_duck_owners
                if not owned or not self._running:
                    raise RuntimeError('Hold capture duck lost ownership')
                if ducker is not None:
                    if ducker.sessions_failed or ducker.last_error:
                        raise RuntimeError('Hold capture duck volume update failed')
                    break
                if not starting or time.perf_counter() >= deadline:
                    raise RuntimeError('Hold capture duck did not confirm')
                time.sleep(0.005)
            self._hold_capture_duck_confirmed_at = time.perf_counter()
            confirmed = True
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            flight_recorder.record('hold_capture_duck.engage', owner_token=token,
                                   confirmed=confirmed, elapsed_ms=elapsed_ms)
            logger.info('[DUCK] Hold capture confirmed=%s in %.2f ms', confirmed, elapsed_ms)
            if not confirmed:
                self._close_hold_capture_duck()

    def _close_hold_capture_duck(self, *, immediate=False):
        """Release only this hold; shutdown also flushes an unowned debounce."""
        token, self._hold_capture_duck_token = self._hold_capture_duck_token, None
        self._hold_capture_duck_confirmed_at = None
        try:
            if token is not None:
                try:
                    if self._dictation_consumer is not None:
                        self._dictation_consumer.finish_capture()
                finally:
                    self._close_hands_free_capture_duck(token)
        finally:
            if immediate:
                with self._hands_free_duck_lock:
                    generation = self._hands_free_capture_duck_restore_generation
                    restore_token = self._hands_free_capture_duck_restore_token
                self._restore_hands_free_capture_duck_now(generation, restore_token)

    def _start_recording_impl(self, streaming=None, play_earcon=True):
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

        # Ordinary holds share the confirmed capture duck and exclude older
        # ring frames. Other recording lanes retain their legacy duck policy.
        if (self.config.get('mode', 'hold') == 'hold'
                and not getattr(self, 'command_mode_recording', False)
                and not getattr(self, 'ava_mode_recording', False)
                and not getattr(self, '_memo_recording', False)):
            self._open_hold_capture_duck()
        else:
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

        flight_recorder.record(
            'hold_recording.start',
            trigger='capslock_stream' if streaming else 'hotkey_batch',
            key_combo=self.config.get('hotkey'),
            mode=self.config.get('mode', 'hold'),
        )

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
                    from samsara.session_modes import CHIP_CROSS
                    self._show_outcome_chip(f"{CHIP_CROSS} no mic", "error")
                return
            if self._dictation_consumer.activate() is False:
                return False
            self._ace_dictation_active = True
        else:
            # CapsLock streaming path (ACE-04B).
            self._ace_dictation_active = False
            # ACE path: streaming accumulator in consumer, no separate stream.
            if self._dictation_consumer.activate_streaming() is False:
                return False
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
            # "REC" in the live (error) colour for the whole hold -- the one
            # unambiguous "it is capturing you right now" signal.
            self._show_outcome_chip("REC", "live", None)

        if streaming:
            from samsara.streaming import StreamingSession
            self._streaming_session = StreamingSession(self)
            self._streaming_session.start()
        return True

    def start_memo_capture(self):
        """Start the batch capture path for the quick-memo voice command."""
        if self.recording or getattr(self, '_streaming_session', None) is not None:
            return False
        self._memo_recording = True
        self.play_sound('capture_started', use_winsound=True)
        self.start_recording(streaming=False, play_earcon=False)
        return bool(self.recording)

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
        with self._hold_capture_lifecycle_lock:
            try:
                self._stop_recording_impl()
            finally:
                try:
                    self._restore_audio()
                finally:
                    self._close_hold_capture_duck()

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

        memo_recording = bool(getattr(self, '_memo_recording', False))
        self._memo_recording = False

        flight_recorder.record(
            'hold_recording.stop',
            command_mode_recording=bool(getattr(self, 'command_mode_recording', False)),
            ava_mode_recording=bool(getattr(self, 'ava_mode_recording', False)),
            streaming=bool(getattr(self, '_streaming_session', None) is not None),
        )

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
        _hold_chip_seq = 0
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)
            # "..." until the transcription outcome replaces it; the watchdog
            # wrapped around the transcribe thread below clears it otherwise.
            _hold_chip_seq = self._show_hold_pending_chip()

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
                            'recording_tail_speech_threshold', HOLD_RELEASE_TAIL_SPEECH_THRESHOLD,
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
                flight_recorder.record('hold_recording.stop_reason', reason='empty_buffer_or_epoch_abort')
                self.play_sound("error")
                if hasattr(self, 'listening_indicator'):
                    self._schedule_ui(self.listening_indicator.flash_error)
                    from samsara.session_modes import CHIP_CROSS
                    # Was a bare beep; the reason is now on screen.
                    self._show_outcome_chip(f"{CHIP_CROSS} no audio", "error")
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
                    flight_recorder.record(
                        'hold_recording.stop_reason', reason='audio_too_short',
                        audio_duration_ms=int(audio_duration * 1000),
                    )
                    return

                if self.config.get('debug', {}).get('dump_hotkey_buffers', False):
                    _dump_hotkey_buffer(audio, self.model_rate)

                # Kill the mechanical hotkey press/release click transient
                # before it can trigger the gate below or Whisper itself.
                audio_faded = _fade_edges(audio, self.model_rate, _FADE_MS)

                # Presence gate over the WHOLE capture (39): one VAD call for
                # short buffers, no VAD for audible long ones, every chunk of
                # a quiet long one -- see _buffer_should_skip_decode.
                # Head grace (2026-07-10): covers the start earcon (measured
                # duration, 0 if none played this recording) plus a fixed
                # pad for the mechanical key-click transient -- see
                # _GATE_HEAD_GRACE_CLICK_PAD_MS.
                _head_grace_ms = self._last_recording_earcon_ms + _GATE_HEAD_GRACE_CLICK_PAD_MS
                _gate = self._buffer_should_skip_decode(
                    audio_faded, self.model_rate,
                    head_grace_ms=_head_grace_ms,
                )
                if _gate:
                    logger.info(_gate.describe() if isinstance(_gate, _GateDecision) else
                                f"[GATE] skip: no contiguous speech ({audio_duration:.2f}s)")
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
                            model_name=self.config.get('model_size', config_defaults.DEFAULTS['model_size']),
                            device=getattr(self, 'device_type', 'unknown'),
                            compute_type=self.config.get('compute_type', config_defaults.DEFAULTS['compute_type']),
                            outcome="gated",
                            language=_languages.describe_diagnostics_language(
                                self.config.get('language', 'en'),
                            ),
                        ), app=self)
                    except Exception as _diag_exc:
                        logger.debug(f"[DIAG] gated record failed: {_diag_exc}")
                    return

                transcribe_start = time.time()

                # Resource-guard fallback ONLY -- see _LONG_DECODE_CEILING_S
                # for why the [LONG]-split branch inside _decode_hotkey_audio
                # is not a quality boundary. Reachable solely for a runaway
                # recording far beyond any normal dictation length; splitting
                # trades some accuracy for bounded memory/latency on that
                # outlier-length buffer.
                #
                # Do NOT set condition_on_previous_text=True on that branch --
                # conditioning over long stitched sequences triggers Whisper's
                # repetition-loop hallucination bug (the model echos earlier
                # text indefinitely). The clean-slate reset in
                # _build_hotkey_transcribe_params is condition_on_previous_text
                # only -- initial_prompt is NOT cleared there; command-hotkey
                # vocabulary still biases every chunk (free-form hold-to-
                # dictate already has none to bias with, per #1 above).
                _decode_result = self._decode_hotkey_audio(
                    audio_faded, transcribe_params, audio_duration, free_form=not ownership.is_command)

                # Fail-loud backstop for silent mid-decode data loss (see
                # module comment above _SANITY_MIN_DURATION_S), with the
                # SPARK P0 auto-retry: a free-form decode (never command-
                # lane -- matcher-side, short utterances, see
                # _apply_retry_on_suspected_loss) that trips the sanity
                # check gets ONE re-decode of the SAME audio with
                # initial_prompt="", delivered if it passes, otherwise the
                # longer of the two. Checked against the gated decode text
                # (not the final post-cleanup text) so this reflects what
                # Whisper actually returned, not downstream filler-
                # stripping/smart-correct trimming.
                def _retry_decode():
                    _retry_params = dict(transcribe_params)
                    _retry_params['initial_prompt'] = ""
                    return self._decode_hotkey_audio(
                        audio_faded, _retry_params, audio_duration, free_form=not ownership.is_command)

                _decode_result, _suspected_data_loss, _retried = _apply_retry_on_suspected_loss(
                    _decode_result, _retry_decode, audio_faded, self.model_rate, audio_duration,
                    ownership.is_command,
                )
                text = _decode_result.text
                _low_confidence = _decode_result.low_confidence
                _diag_all_segs = list(_decode_result.seg_list)
                _detected_lang = _decode_result.detected_lang
                _diag_path = _decode_result.diag_path

                flight_recorder.record(
                    'hold_recording.decoded',
                    audio_duration_ms=int(audio_duration * 1000),
                    text_len=len(text) if text else 0,
                    text_preview=(text or '')[:12],
                    low_confidence=bool(_low_confidence),
                    suspected_data_loss=bool(_suspected_data_loss),
                    retried=bool(_retried),
                )

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

                    # Voice exit from Mouse 4 command mode. Queue 84: WHOLE
                    # utterance only. This was a substring test, the same rule
                    # that let "Have it stop listening to you, or something
                    # like that." end a hands-free session and destroy a
                    # 477-character draft. No draft lives in this lane, but a
                    # sentence containing the phrase must not end it either.
                    if is_command_mode and normalize_utterance(text) in {
                        normalize_utterance(p) for p in ("exit command mode", "stop listening")
                    }:
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
                                model_name=self.config.get('model_size', config_defaults.DEFAULTS['model_size']),
                                device=getattr(self, 'device_type', 'unknown'),
                                compute_type=self.config.get('compute_type', config_defaults.DEFAULTS['compute_type']),
                                t_transcribe_ms=t_transcribe_ms,
                                t_total_ms=t_transcribe_ms,
                                text=text,
                                outcome=("suspected_loss" if _suspected_data_loss
                                          else "low_confidence" if _low_confidence
                                          else "ok"),
                                path=_diag_path,
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
                    raw = text
                    # VERBATIM profile -- see _apply_verbatim_if_active.
                    _verbatim_text = self._apply_verbatim_if_active(text)
                    _verbatim_active = _verbatim_text is not None
                    if _verbatim_active:
                        text = _verbatim_text
                        t_corrections_ms = int((time.perf_counter() - _diag_corr_start) * 1000)
                    else:
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
                    if (not _verbatim_active
                            and self.config.get('smart_corrections', {}).get('modes', {}).get('hotkey', True)):
                        _diag_smart_start = time.perf_counter()
                        _text_before_smart = text
                        text = smart_correct(text, self)
                        t_smart_ms = int((time.perf_counter() - _diag_smart_start) * 1000)
                        smart_changed = (text != _text_before_smart)

                    if not _verbatim_active:
                        if self.config['add_trailing_space']:
                            text = text + " "

                        # Inline formatting tokens ("new line" -> \n, etc.) --
                        # after smart_correct, before delivery/history, so
                        # history stores what was actually typed (see
                        # _apply_formatting_tokens).
                        text = self._apply_formatting_tokens(text)

                    if memo_recording:
                        try:
                            memo_home = self.config.get('memo_file') or None
                            audio_path = None
                            if self.config.get('memo_retain_audio', True):
                                audio_path = quick_memo.retain_audio(
                                    audio, self.model_rate, home=memo_home)
                            # Queue 92: a memo that opens with a category the
                            # user has already defined ("shopping: milk") is
                            # filed under it. An unknown first word is just
                            # part of the memo -- nothing is ever invented.
                            known = quick_memo.categories(memo_home)
                            category, body = quick_memo.split_category(
                                text.strip(), known)
                            record = quick_memo.add_memo(
                                body, source=quick_memo.SOURCE_VOICE,
                                audio_path=audio_path, home=memo_home,
                                category=category,
                                category_source=(quick_memo.CATEGORY_SPOKEN
                                                 if category else None))
                        except Exception as exc:
                            logger.exception(f"[MEMO] Save failed: {exc}")
                            self.play_sound('error')
                            return
                        # Best-effort extras, AFTER the memo is safely stored:
                        # neither may turn a saved memo into a failure.
                        try:
                            quick_memo.mirror_to_vault(record, self.config, home=memo_home)
                        except Exception as exc:
                            logger.debug(f"[MEMO] Mirror skipped: {exc}")
                        try:
                            quick_memo.prune_audio(self.config, home=memo_home)
                        except Exception as exc:
                            logger.debug(f"[MEMO] Prune skipped: {exc}")
                        self.play_sound('capture_saved')
                        self.add_to_history("[memo] " + body, is_command=False)
                        return

                    # Voice memo divert (2026-07-24): "voice memo" arms a
                    # one-shot capture of the NEXT hold-to-dictate
                    # recording -- hotkey path only, checked here (after
                    # the text is fully finalized, before any injection or
                    # undo-stack bookkeeping) so a captured memo never
                    # reaches _paste_preserving_clipboard/_record_undoable_
                    # paste. capture() itself disarms and plays its own
                    # confirmation; a False return (any failure) falls
                    # through to normal injection below so a memo failure
                    # never loses the user's dictation. See
                    # samsara/voice_memo.py.
                    if voice_memo.is_armed(self.config) and voice_memo.capture(
                        self, audio, self.model_rate, text
                    ):
                        self.add_to_history("[memo] " + text.strip(), is_command=False)
                        self._log_history(
                            raw_text=raw,
                            display_text="[memo] " + text.strip(),
                            duration_ms=int(audio_duration * 1000),
                            mode="hold",
                            status="success",
                            entry_type="dictation",
                        )
                        return

                    logger.info(f"[OK] {text}")
                    # Queue 50: this success sound used to play before the
                    # paste below, even into an elevated window that drops
                    # every keystroke. Check first; never claim success there.
                    _verdict = injection_safety.window_integrity() if self.config['auto_paste'] else None
                    if _verdict is not None and _verdict.blocked:
                        self._announce_window_locked(_verdict, "hold dictation")
                        self.add_to_history(text.strip(), is_command=False)
                        self._log_history(
                            raw_text=raw,
                            display_text=text.strip(),
                            duration_ms=int(audio_duration * 1000),
                            mode="hold",
                            status="failed",
                            entry_type="dictation",
                        )
                        return
                    if _verdict is None or _verdict.confirmed_ok:
                        self.play_sound("success")
                        if hasattr(self, 'listening_indicator'):
                            self._schedule_ui(self.listening_indicator.flash_success)
                            self._show_outcome_chip("typed", "success", 900)
                    else:
                        logger.warning("[INJECT] hold dictation: cannot confirm the foreground window "
                                       "accepts typing (%s) -- sending without a success sound",
                                       _verdict.describe())
                        self._show_outcome_chip("sent, unconfirmed", "warning")

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
                            model_name=self.config.get('model_size', config_defaults.DEFAULTS['model_size']),
                            device=getattr(self, 'device_type', 'unknown'),
                            compute_type=self.config.get('compute_type', config_defaults.DEFAULTS['compute_type']),
                            t_transcribe_ms=t_transcribe_ms,
                            t_corrections_ms=t_corrections_ms,
                            t_smart_ms=t_smart_ms,
                            t_total_ms=int((time.time() - transcribe_start) * 1000),
                            text=text,
                            smart_changed=smart_changed,
                            outcome=("suspected_loss" if _suspected_data_loss
                                      else "low_confidence" if _low_confidence
                                      else "ok"),
                            path=_diag_path,
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
                            self.config.get('model_size', config_defaults.DEFAULTS['model_size']),
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
                    if hasattr(self, 'listening_indicator'):
                        from samsara.session_modes import CHIP_CROSS
                        self._show_outcome_chip(f"{CHIP_CROSS} no speech", "error")
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
                            model_name=self.config.get('model_size', config_defaults.DEFAULTS['model_size']),
                            device=getattr(self, 'device_type', 'unknown'),
                            compute_type=self.config.get('compute_type', config_defaults.DEFAULTS['compute_type']),
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
                    from samsara.session_modes import CHIP_CROSS
                    self._show_outcome_chip(f"{CHIP_CROSS} transcribe failed", "error")
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

        def _transcribe_with_chip_watchdog():
            try:
                transcribe()
            finally:
                if _hold_chip_seq:
                    self._resolve_hold_chip(_hold_chip_seq)

        thread = thread_registry.spawn(
            "dictation.transcribe", _transcribe_with_chip_watchdog, daemon=True,
        )

    def cancel_recording(self):
        """Escape and failed startup must release the hold even if cancel raises."""
        with self._hold_capture_lifecycle_lock:
            try:
                self._cancel_recording_impl()
            finally:
                self._close_hold_capture_duck()

    def _cancel_recording_impl(self):
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
        """Keep tray callbacks responsive, including config I/O and the stop join."""
        with self._wake_word_settings_guard:
            self._wake_word_settings_generation += 1
            generation = self._wake_word_settings_generation

        def apply():
            with self._wake_word_settings_lock:
                with self._wake_word_settings_guard:
                    if generation != self._wake_word_settings_generation:
                        return
                self._apply_wake_word_enabled(bool(enabled))

        thread_registry.spawn('dictation.wake_word_settings', apply, daemon=True)

    def _apply_wake_word_enabled(self, enabled):
        """Apply the latest listener setting on the serialized settings worker."""
        with self._config_lock:
            self.config['wake_word_enabled'] = enabled
            self.save_config()
        if enabled and not self.wake_word_active:
            self.start_wake_word_mode()
            logger.info("[WAKE] Wake word listener ENABLED")
        elif not enabled and (self.wake_word_active
                              or getattr(self, '_wake_start_pending', False)):
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

    def open_hub_page(self, name):
        """Open the hub window on one named page (queue 92).

        The tray's "Open memos" goes through here. Returns False when the
        hub cannot take it, so the caller falls back to its own affordance
        (for memos, the raw markdown file) instead of appearing to do
        nothing.
        """
        window = getattr(self, 'main_window', None)
        opener = getattr(window, 'open_page', None) if window is not None else None
        if not callable(opener):
            return False
        try:
            return bool(opener(name))
        except Exception as exc:
            logger.exception(f"[UI] Failed to open hub page {name}: {exc}")
            return False

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
            wake_state = getattr(self, '_wake_models_state', 'ready')
            if wake_state != 'ready':
                return f"{mode} + Wake (loading)"
            return f"{mode} + Wake"
        return mode

    def _update_tray_tooltip(self):
        """Refresh the tray icon tooltip to reflect current mode/wake state."""
        if not hasattr(self, 'tray_icon'):
            return
        if self.snoozed:
            return  # snooze tooltip managed by _update_snooze_tooltip
        self.tray_icon.title = f"Samsara - {self._get_mode_display()}"

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

    def _idle_animation_enabled(self) -> bool:
        """config ui.idle_animation (default True): the indicator's idle blink
        and glance only -- never state animation, never the tray."""
        ui_cfg = self.config.get('ui', {})
        if not isinstance(ui_cfg, dict):
            return True
        return bool(ui_cfg.get('idle_animation', True))

    def _tray_mark(self):
        """(capture, eye) the tray shows for the live app state.

        Capture (wheel): recording > Ava > listening (command mode,
        continuous, or the wake listener armed) > idle. Eye (hands-free):
        the heard animation's keyframe while it plays > asleep (snoozed) >
        armed (wake listener running) > off. See tray_qt.MARK_STATES.
        """
        wake_armed = bool(getattr(self, 'wake_word_active', False))
        snoozed = bool(getattr(self, 'snoozed', False))
        if getattr(self, 'recording', False):
            capture = 'recording'
        elif (getattr(self, 'ava_mode_active', False)
                or getattr(self, 'ava_command_session_active', False)):
            capture = 'ava'
        elif (getattr(self, 'command_mode_active', False)
                or getattr(self, 'continuous_active', False) or wake_armed):
            capture = 'listening'
        else:
            capture = 'idle'

        heard_eye = getattr(self, '_tray_heard_eye', None)
        if heard_eye is not None:
            eye = heard_eye
        elif snoozed:
            eye = 'asleep'
        elif wake_armed:
            eye = 'armed'
        else:
            eye = 'off'
        return capture, eye

    def create_icon_image(self, rotation=0.0, opacity=1.0):
        """The tray frame for the live state: a tray_qt.MarkFrame.

        Only describes the frame -- tray_qt renders it with render_mark on
        the Qt thread, so this is safe from the icon timer's worker thread.
        rotation is in radians (the chase timer's unit).
        """
        from samsara.ui.tray_qt import MarkFrame
        capture, eye = self._tray_mark()
        return MarkFrame(capture, eye, math.degrees(rotation), opacity)

    def _push_tray_icon(self):
        """Show the live tray state now (outside an animation tick)."""
        if not hasattr(self, 'tray_icon'):
            return
        try:
            self.tray_icon.icon = self.create_icon_image(
                rotation=getattr(self, '_icon_rotation', 0.0))
        except OSError as e:
            logger.debug(f"Tray icon state swap failed: {e}")

    def _flash_tray_heard(self):
        """Wake phrase heard: play tray_qt.HEARD_KEYFRAMES on the tray eye."""
        from samsara.ui.tray_qt import HEARD_KEYFRAMES
        self._tray_heard_generation = getattr(self, '_tray_heard_generation', 0) + 1
        generation = self._tray_heard_generation

        def _step(index):
            if generation != self._tray_heard_generation:
                return   # a newer flash superseded this one
            at_ms, eye = HEARD_KEYFRAMES[index]
            self._tray_heard_eye = eye
            self._push_tray_icon()
            if index + 1 < len(HEARD_KEYFRAMES):
                delay_s = (HEARD_KEYFRAMES[index + 1][0] - at_ms) / 1000.0
                thread_registry.timer(
                    "dictation.tray_heard", delay_s,
                    lambda: _step(index + 1), daemon=True)

        _step(0)

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
        # _icon_rotation is NOT reset: the head keeps chasing the tail from
        # wherever it stopped, never snapping to an aligned rest pose.
        self._icon_chase_tick()

    def _stop_icon_chase(self):
        """Stop the chase animation and show idle icon."""
        self._icon_animating = False
        if self._icon_chase_timer is not None:
            self._icon_chase_timer.cancel()
            self._icon_chase_timer = None
        self._icon_chase_offset = 0
        self._push_tray_icon()   # keeps the current rotation (no aligned rest)

    def _icon_chase_tick(self):
        """Advance the tray animation and schedule the next tick.

        Motion means capture (queue 19): the ring TURNS in every active
        capture state -- the ouroboros head chasing its tail -- and speed is
        the state channel (tray_qt.SPIN_SECONDS_PER_TURN):
        - recording:     filled red wheel AND turning, 'recording' pace
                         (fill/colour = state, rotation = liveness)
        - transcribing:  the 'recording' reason outlives capture -> fastest
        - continuous:    'listening' pace (ambient)
        - wake_word:     'armed' pace (ambient)
        No opacity pulse any more. At rest the chase is released and the mark
        stands still (_stop_icon_chase). Thinking (2.4 s/turn) is signalled
        only to the listening indicator (set_thinking from ask_ollama / the Ava
        command session), so the tray has no thinking reason. Tick intervals
        stay the existing ICON_TICK_* constants.
        """
        if not self._icon_animating:
            return

        from samsara.ui.tray_qt import SPIN_SECONDS_PER_TURN

        # Highest-priority active reason picks the tick and the pace.
        if getattr(self, 'recording', False):
            tick_interval, pace = ICON_TICK_FAST, 'recording'
        elif 'recording' in self._icon_anim_reasons:
            tick_interval, pace = ICON_TICK_FAST, 'transcribing'
        elif 'continuous' in self._icon_anim_reasons:
            tick_interval, pace = ICON_TICK_MEDIUM, 'listening'
        else:  # wake_word or anything else
            tick_interval, pace = ICON_TICK_SLOW, 'armed'

        self._icon_rotation += 2 * math.pi * tick_interval / SPIN_SECONDS_PER_TURN[pace]

        if hasattr(self, 'tray_icon'):
            try:
                self.tray_icon.icon = self.create_icon_image(rotation=self._icon_rotation)
            except OSError as e:
                # transient WinError during icon handle swap -- skip this frame
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

    def open_transcribe_file(self):
        """Open the "Transcribe a file" dialog (queue 123).

        Sits beside the other open_* window openers and is the entry point
        commands.json's "transcribe file" points at. The decode runs on the
        thread registry from inside the dialog; nothing here touches the
        microphone or the capture lanes."""
        try:
            from samsara.ui.transcribe_file_qt import open_transcribe_file
            self._transcribe_file_qt = open_transcribe_file(self)
        except Exception as e:
            logger.exception(f"[FILE-TX] Error opening the transcribe-file dialog: {e}")

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
        """Set tray icon tooltip (and the asleep eye) to reflect snooze state."""
        if not hasattr(self, 'tray_icon'):
            return
        self._push_tray_icon()
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

        _boot_log = getattr(self, '_boot_log', None)

        def _create():
            self.tray_icon = _SamsaraTrayQt(self)
            if _boot_log is not None:
                _boot_log("tray icon created")

        QTimer.singleShot(0, qt_app, _create)
        QTimer.singleShot(0, qt_app, self.show_main_window)
        # This method intentionally blocks below for the application's
        # lifetime.  Signal the model-loading lane only after the UI work has
        # been queued; otherwise a fast model load can close the splash while
        # synchronous audio/interface initialization is still underway.
        shell_ready = getattr(self, "_startup_shell_ready", None)
        if shell_ready is not None:
            shell_ready.set()
        if _boot_log is not None:
            _boot_log("shell ready (tray + main window scheduled)")

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

        # Release the Win32 mouse hook FIRST (32): nothing below may hold the
        # user's mouse hostage, and this method ends in os._exit, which skips
        # samsara.mouse_hook's atexit release.
        try:
            if getattr(self, '_mouse_hook', None) is not None:
                self._mouse_hook.stop()
                self._mouse_hook = None
        except Exception as e:
            logger.error(f"[EXIT] Mouse hook stop failed: {e}")

        # Signal background threads (e.g. stream-health monitor) to stop
        self._running = False

        try:
            with self._hold_capture_lifecycle_lock:
                try:
                    self.cancel_recording()
                finally:
                    self._close_hold_capture_duck(immediate=True)
        except Exception as e:
            logger.debug(f"[EXIT] Hold capture cleanup failed: {e}")

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

        # Win32 mouse hook: released at the top of quit_app (32); this
        # repeat covers a hook installed while shutdown was running.
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
        _samsara_boot.end_session(LOG_DIR)      # 48: a deliberate exit, not a crash
        os._exit(0)

if __name__ == "__main__":
    # Console is already hidden at top of file
    _DIAG_MAIN_T = time.perf_counter()
    logger.debug(f"[BOOT-DIAG] __main__: entry (since sounddevice import: {(_DIAG_MAIN_T - _POST_SD_T)*1000:.0f}ms)")

    # Guard against double-launch. Must run before the splash / audio starts
    # so a second invocation exits cleanly without grabbing resources.
    _samsara_boot.lock_single_instance()

    # 48: after the lock, so a refused second launch never touches the running
    # session's marker. Notes a previous run that never reached quit_app (with
    # its faulthandler summary), and logs worker-thread / unraisable exceptions
    # and Qt's own warnings. See samsara/boot.py "Crash evidence".
    _previous_session = _samsara_boot.begin_session(LOG_DIR, logger)
    # 52: put back any app volume a killed run left ducked, before anything
    # can start a new duck and mistake the ducked level for the user's own.
    audio_ducking.configure_duck_journal(
        LOG_DIR / audio_ducking.DUCK_JOURNAL_NAME, previous_unclean=_previous_session is not None,
    )
    _samsara_boot.install_exception_hooks(logger)
    _samsara_boot.install_qt_message_handler(logger)

    # Source builds historically kept a second config beside dictation.py.
    # Carry the newer legacy profile across exactly once, after acquiring the
    # instance lock and before any settings (including Qt scale) are read.
    _samsara_boot.migrate_legacy_source_profile(Path(__file__).parent)

    # QApplication reads QT_SCALE_FACTOR only during construction. Apply the
    # user's restart-required accessibility scale before the splash starts Qt.
    _samsara_boot.apply_early_interface_scale()

    # The palette has to be bound before the splash -- the first window built
    # -- or that window keeps the default one for the life of the process.
    _samsara_boot.apply_early_theme()

    # Show splash screen during startup
    splash = _samsara_boot.create_splash()

    app = None
    try:
        app = DictationApp(splash)
    except Exception as e:
        # Keep the shared Qt runtime and splash alive so a synchronous startup
        # failure is visible instead of flashing away before the traceback can
        # be read.  Rich splashes render a dedicated error state; the original
        # status-only API still gets a useful message.
        _samsara_boot.show_startup_failure(splash, e)
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
