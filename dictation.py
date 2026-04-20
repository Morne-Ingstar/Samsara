import os
import sys
import math
import wave

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

# Acquire single-instance lock (must keep reference to prevent garbage collection)
_instance_lock = _check_single_instance()

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
import sounddevice as sd
from pynput import keyboard as pynput_keyboard
from pynput.keyboard import Key, Controller as KeyboardController
import keyboard  # For reliable simultaneous key state detection
from pynput.mouse import Button, Controller as MouseController
import pyperclip
import pyautogui
from faster_whisper import WhisperModel
import pystray
from PIL import Image, ImageDraw
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import customtkinter as ctk
from voice_training import VoiceTrainingWindow
from samsara.profiles import ProfileManager
from samsara.ui.splash import SplashScreen
from samsara.ui.first_run_wizard import FirstRunWizard
from samsara.ui.profile_manager_ui import ProfileManagerWindow
from samsara.ui.settings_window import SettingsWindow
from samsara.ui.history_window import HistoryWindow
from samsara.ui.wake_word_debug import WakeWordDebugWindow
from samsara.ui.listening_indicator import ListeningIndicator
from samsara.wake_word_matcher import match_wake_phrase
from samsara.wake_corrections import apply_corrections as apply_wake_corrections, was_corrected
from samsara.command_parser import parse_wake_command, normalize_command_text, strip_wake_echoes
from samsara import plugin_commands as _plugin_commands
from samsara.constants import (
    MODEL_SAMPLE_RATE, DEFAULT_CAPTURE_RATE, PREBUFFER_SECONDS,
    DEFAULT_SPEECH_THRESHOLD, DEFAULT_MIN_SPEECH_DURATION, DEFAULT_SILENCE_TIMEOUT,
    WAKE_DETECTION_SILENCE, WAKE_COMMAND_TIMEOUT,
    ICON_TICK_FAST, ICON_TICK_MEDIUM, ICON_TICK_SLOW,
    ICON_SPIN_FAST, ICON_SPIN_MEDIUM, ICON_SPIN_SLOW,
    ICON_CHASE_FAST, ICON_CHASE_MEDIUM, ICON_CHASE_SLOW,
    CLIPBOARD_PASTE_DELAY, CLIPBOARD_RESTORE_DELAY,
)
from samsara.calibration import measure_ambient_rms, calibrate_threshold
from samsara.key_macros import KeyMacroManager, get_default_macro_config
from samsara.notifications import NotificationManager, get_default_notification_config
from samsara.alarms import AlarmManager, get_default_alarm_config
from samsara.echo_cancel import EchoCanceller
from samsara.clipboard import clipboard_lock as _clipboard_lock, save_clipboard as _save_clipboard_win32, restore_clipboard as _restore_clipboard_win32, paste_with_preservation


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



# Set up logging to file and console
LOG_DIR = Path(__file__).parent
LOG_FILE = LOG_DIR / 'samsara.log'

# Create logger
logger = logging.getLogger('Samsara')
logger.setLevel(logging.DEBUG)

# File handler - logs everything
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Console handler - also logs everything when console is visible
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Override print to also log
_original_print = print
def print(*args, **kwargs):
    message = ' '.join(str(arg) for arg in args)
    logger.info(message)
    _original_print(*args, **kwargs)

logger.info("=" * 50)
logger.info(f"Samsara starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
logger.info("=" * 50)



# ============================================================================


class CommandExecutor:
    """Executes voice commands - hotkeys, launches, key holds, etc."""
    
    def __init__(self, commands_path):
        self.commands_path = commands_path
        self.commands = {}
        self.held_keys = {}  # Track currently held keys
        self.keyboard_controller = KeyboardController()
        self.mouse_controller = MouseController()
        self.load_commands()

        # Plugin commands: auto-load *.py files from plugins/commands/.
        # Missing directory is not fatal -- app runs fine without plugins.
        plugins_dir = Path(__file__).parent / "plugins" / "commands"
        try:
            _plugin_commands.load_plugins(plugins_dir)
        except Exception as e:
            print(f"[PLUGINS] Failed to load plugins: {e}")
        unique = len({id(entry) for entry in _plugin_commands._REGISTRY.values()})
        print(f"[PLUGINS] Loaded {unique} plugin commands")
        
        # Key mapping for pynput
        self.key_map = {
            'ctrl': Key.ctrl,
            'shift': Key.shift,
            'alt': Key.alt,
            'win': Key.cmd,
            'enter': Key.enter,
            'esc': Key.esc,
            'space': Key.space,
            'tab': Key.tab,
            'backspace': Key.backspace,
            'delete': Key.delete,
            'home': Key.home,
            'end': Key.end,
            'pageup': Key.page_up,
            'pagedown': Key.page_down,
            'up': Key.up,
            'down': Key.down,
            'left': Key.left,
            'right': Key.right,
            'f1': Key.f1, 'f2': Key.f2, 'f3': Key.f3, 'f4': Key.f4,
            'f5': Key.f5, 'f6': Key.f6, 'f7': Key.f7, 'f8': Key.f8,
            'f9': Key.f9, 'f10': Key.f10, 'f11': Key.f11, 'f12': Key.f12,
        }
    
    def load_commands(self):
        """Load commands from JSON file"""
        try:
            with open(self.commands_path, 'r') as f:
                data = json.load(f)
                self.commands = data.get('commands', {})
            print(f"[OK] Loaded {len(self.commands)} voice commands")
        except Exception as e:
            print(f"[WARN] Could not load commands: {e}")
            self.commands = {}
    
    def get_key(self, key_str):
        """Convert key string to pynput Key object"""
        key_lower = key_str.lower()
        if key_lower in self.key_map:
            return self.key_map[key_lower]
        # For single character keys
        return key_str.lower() if len(key_str) == 1 else key_str
    
    def execute_command(self, command_name):
        """Execute a voice command by name"""
        if command_name not in self.commands:
            return False
        
        cmd = self.commands[command_name]
        cmd_type = cmd.get('type')
        
        try:
            if cmd_type == 'hotkey':
                # Execute hotkey combination
                keys = [self.get_key(k) for k in cmd['keys']]
                for key in keys[:-1]:
                    self.keyboard_controller.press(key)
                self.keyboard_controller.press(keys[-1])
                self.keyboard_controller.release(keys[-1])
                for key in reversed(keys[:-1]):
                    self.keyboard_controller.release(key)
                print(f"[OK] Executed: {command_name}")
                return True
            
            elif cmd_type == 'press':
                # Single key press
                key = self.get_key(cmd['key'])
                self.keyboard_controller.press(key)
                self.keyboard_controller.release(key)
                print(f"[OK] Pressed: {cmd['key']}")
                return True
            
            elif cmd_type == 'key_down':
                # Hold key down
                key = self.get_key(cmd['key'])
                self.keyboard_controller.press(key)
                self.held_keys[cmd['key']] = key
                print(f"[OK] Holding: {cmd['key']}")
                return True
            
            elif cmd_type == 'key_up':
                # Release held key
                key_str = cmd['key']
                if key_str in self.held_keys:
                    self.keyboard_controller.release(self.held_keys[key_str])
                    del self.held_keys[key_str]
                    print(f"[OK] Released: {key_str}")
                return True
            
            elif cmd_type == 'release_all':
                # Release all held keys
                for key in self.held_keys.values():
                    self.keyboard_controller.release(key)
                count = len(self.held_keys)
                self.held_keys.clear()
                print(f"[OK] Released {count} held keys")
                return True
            
            elif cmd_type == 'mouse':
                # Mouse actions
                action = cmd.get('action')
                if action == 'click':
                    button = Button.left if cmd.get('button') == 'left' else Button.right
                    self.mouse_controller.click(button)
                    print(f"[OK] Mouse {cmd.get('button')} click")
                elif action == 'double_click':
                    self.mouse_controller.click(Button.left, 2)
                    print(f"[OK] Mouse double click")
                return True
            
            elif cmd_type == 'launch':
                # Launch application (cross-platform)
                target = cmd['target']
                try:
                    if sys.platform == 'win32':
                        subprocess.Popen(f'start "" "{target}"', shell=True)
                    elif sys.platform == 'darwin':  # macOS
                        subprocess.Popen(['open', target])
                    else:  # Linux
                        subprocess.Popen(['xdg-open', target])
                    print(f"[OK] Launching: {target}")
                except Exception as e:
                    print(f"[ERROR] Failed to launch {target}: {e}")
                return True

            elif cmd_type == 'text':
                # Insert text (for punctuation, snippets, etc.)
                text_to_insert = cmd.get('text', '')
                if text_to_insert:
                    with _clipboard_lock:
                        saved = _save_clipboard_win32()
                        pyperclip.copy(text_to_insert)
                        time.sleep(0.02)
                        pyautogui.hotkey('ctrl', 'v')
                        time.sleep(0.4)  # Increased delay for slow apps
                        _restore_clipboard_win32(saved)
                    print(f"[OK] Inserted: {text_to_insert}")
                return True

            else:
                print(f"[WARN] Unknown command type: {cmd_type}")
                return False
                
        except Exception as e:
            print(f"[ERROR] Command execution error: {e}")
            return False
    
    def find_command(self, text):
        """Check if transcribed text matches a command"""
        text_lower = text.lower().strip()
        
        # Exact match first
        if text_lower in self.commands:
            return text_lower
        
        # Check for partial matches - command must be at start or end of text
        # This prevents false positives
        for cmd_name in self.commands:
            # Command at the start
            if text_lower.startswith(cmd_name + " ") or text_lower.startswith(cmd_name):
                return cmd_name
            # Command at the end
            if text_lower.endswith(" " + cmd_name) or text_lower.endswith(cmd_name):
                return cmd_name
            # Exact match in the middle with spaces around it
            if f" {cmd_name} " in f" {text_lower} ":
                return cmd_name

        # Plugin commands -- lower priority than built-ins on name conflict.
        plugin_entry, _remainder = _plugin_commands.find_command(text)
        if plugin_entry is not None:
            return plugin_entry['phrase']

        return None
    
    def process_text(self, text, app_instance=None, force_commands=False):
        """Process transcribed text - check for command or return text for dictation
        
        Args:
            text: Transcribed text to process
            app_instance: Reference to the main app for state access
            force_commands: If True, always check commands (bypasses command_mode_enabled check).
                           Used by wake word mode where commands should always work.
        """
        if not text:
            return None, False
        
        text_lower = text.lower().strip()
        
        # ALWAYS check for command mode toggle commands first
        if "command mode on" in text_lower or "command mode enable" in text_lower or "enable command mode" in text_lower:
            if app_instance:
                app_instance.command_mode_enabled = True
                app_instance.config['command_mode_enabled'] = True
                app_instance.save_config()
                print("[OK] Command mode ENABLED")
            return "command_mode_on", True
        
        if "command mode off" in text_lower or "command mode disable" in text_lower or "disable command mode" in text_lower:
            if app_instance:
                app_instance.command_mode_enabled = False
                app_instance.config['command_mode_enabled'] = False
                app_instance.save_config()
                print("[OFF] Command mode DISABLED")
            return "command_mode_off", True

        # Check for reminder commands (ALWAYS works, regardless of command mode)
        if app_instance and hasattr(app_instance, 'notification_manager'):
            reminder_result = app_instance.notification_manager.parse_remind_command(text)
            if reminder_result:
                minutes, task = reminder_result
                message = task if task else "Time's up!"
                app_instance.notification_manager.add_quick_reminder(minutes, message)
                print(f"[OK] Reminder set for {minutes} minutes: {message}")
                app_instance.play_sound("success")
                return f"reminder_{minutes}min", True

        # Check if command mode is enabled (skip if force_commands is True, e.g., wake word mode)
        if not force_commands:
            if not hasattr(self, 'command_mode_enabled'):
                return text, False

            # If command mode is disabled, return text for dictation
            if app_instance and not app_instance.command_mode_enabled:
                return text, False

        # Try to find and execute a command
        command = self.find_command(text)
        if command:
            if command in self.commands:
                cmd = self.commands[command]
                if cmd.get('type') == 'method':
                    method_name = cmd.get('method')
                    if app_instance and method_name and hasattr(app_instance, method_name):
                        try:
                            getattr(app_instance, method_name)()
                            return command, True
                        except Exception as e:
                            print(f"[ERROR] method '{method_name}' failed: {e}")
                            return command, False
                    print(f"[WARN] method '{method_name}' not found on app")
                    return command, False
                success = self.execute_command(command)
            else:
                print(f"[PLUGIN] Executing: {command}")
                _phrase, success = _plugin_commands.execute_command(text, app=app_instance)
            return command, success

        # Not a command, return text for dictation
        return text, False



class DictationApp:
    def __init__(self, splash=None):
        self.splash = splash
        self.config_path = Path(__file__).parent / "config.json"

        # Check if first-run wizard is needed
        # Only show wizard if config doesn't exist at all (truly new installation)
        # Existing configs without first_run_complete are assumed to be from before the wizard was added
        need_wizard = False
        if not self.config_path.exists():
            need_wizard = True
        else:
            try:
                with open(self.config_path, 'r') as f:
                    existing_config = json.load(f)
                    # Only show wizard if first_run_complete is explicitly False
                    # (meaning wizard was started but not completed)
                    if 'first_run_complete' in existing_config and not existing_config['first_run_complete']:
                        need_wizard = True
            except:
                # Config file is corrupted, show wizard
                need_wizard = True

        # Run first-run wizard if needed
        if need_wizard:
            # Close splash for wizard - wizard has its own UI
            if self.splash:
                self.splash.root.destroy()  # Fully destroy splash's root
                self.splash = None
            print("First run detected - launching setup wizard...")
            wizard = FirstRunWizard(self.config_path)
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

        self.update_splash("Loading configuration...")
        self.load_config()

        self.update_splash("Setting up audio...")

        # Reuse root from splash, or create new hidden one
        if self.splash:
            self.root = self.splash.get_root()
        else:
            self.root = tk.Tk()
            self.root.withdraw()  # Hide it

        # Get available microphones
        self.available_mics = self.get_available_microphones()

        # Validate saved microphone ID against available devices.
        # Device indices change when switching host APIs (e.g. MME → WASAPI)
        # or when hardware is added/removed. Fall back to the first available.
        saved_mic = self.config.get('microphone')
        valid_ids = {mic['id'] for mic in self.available_mics}
        if saved_mic not in valid_ids and self.available_mics:
            old_id = saved_mic
            self.config['microphone'] = self.available_mics[0]['id']
            self.save_config()
            print(f"[CONFIG] Saved microphone {old_id} not found in current devices, "
                  f"switched to {self.available_mics[0]['name']} (id={self.config['microphone']})")
        
        # Audio settings -- dual sample rates for WASAPI compatibility
        self.model_rate = MODEL_SAMPLE_RATE
        self.capture_rate = self._detect_capture_rate(self.config.get('microphone'))

        # Auto-calibrate speech threshold on startup
        self._run_calibration_if_auto()

        self.audio_queue = queue.Queue()
        self.recording = False
        self.command_mode_recording = False  # True when using command-only hotkey
        self.audio_data = []

        # Set up audio feedback sounds (creates defaults if needed)
        self._setup_sounds()

        # Model settings
        self.model = None
        self.model_loaded = False
        self.loading_model = False
        self.model_lock = threading.Lock()  # Thread lock for model.transcribe() calls
        
        # Command system
        commands_path = Path(__file__).parent / "commands.json"
        self.command_executor = CommandExecutor(commands_path)
        self.command_mode_enabled = self.config.get('command_mode_enabled', True)

        # Wake-word trace hook — the debug window registers a callback here
        # when open so the main pipeline's decisions show up in its trace view.
        # None means "no tracing" and _emit_wake_trace becomes a cheap no-op.
        self._wake_trace_callback = None
        
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
        self.continuous_stream = None
        self.silence_start = None
        self.speech_buffer = []
        self.buffer_lock = threading.Lock()
        self.is_speaking = False
        self._speech_onset_count = 0  # consecutive chunks above threshold (debounce)
        self._speech_onset_pending = []  # chunks during debounce (kept so we don't lose speech start)
        self.wake_word_listening = False  # Currently listening for wake word
        self.wake_word_triggered = False  # Wake word detected, ready for command
        self._wake_trace_callback = None  # Optional: debug window registers here
        
        # Pre-buffer: rolling circular buffer captures audio BEFORE hotkey press
        # so the first ~1.5 seconds of speech are never lost to startup delay.
        # Each chunk is 100ms at capture_rate. Resampled to model_rate before transcription.
        self._prebuffer_seconds = PREBUFFER_SECONDS
        self._prebuffer_chunks = int(self._prebuffer_seconds / 0.1)  # 15 chunks at 100ms each
        self._prebuffer = collections.deque(maxlen=self._prebuffer_chunks)
        self._prebuffer_active = False  # Whether background stream is feeding the pre-buffer
        self._prebuffer_stream = None  # Standalone pre-buffer stream (when no wake word)
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

        # Settings window
        self.settings_window = SettingsWindow(self)

        # Voice Training window
        self.voice_training_window = VoiceTrainingWindow(self)

        # History window
        self.history_window = HistoryWindow(self)

        # Wake word debug window
        self.wake_word_debug_window = WakeWordDebugWindow(self)

        # Listening state indicator overlay
        self.listening_indicator = ListeningIndicator(self.root)
        self.listening_indicator.set_mode(self._get_mode_display())
        self.listening_indicator.set_position(
            self.config.get('listening_indicator_position', 'bottom-center'))
        if self.config.get('listening_indicator_enabled', False):
            self.listening_indicator.show()

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
            save_config=self.save_config
        )
        if self.config.get('alarms', {}).get('enabled', True):
            self.alarm_manager.start()

        # Echo cancellation (removes system audio from mic input)
        aec_config = self.config.get('echo_cancellation', {})
        self.echo_canceller = EchoCanceller(
            sample_rate=self.capture_rate,
            enabled=aec_config.get('enabled', False),
            latency_ms=aec_config.get('latency_ms', 30.0),
        )
        if self.echo_canceller.enabled:
            self.echo_canceller.start()

        self.update_splash("Setting up keyboard...")

        # Start keyboard listener
        self.keyboard_listener = pynput_keyboard.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release
        )
        self.keyboard_listener.start()

        self.update_splash("Loading speech model...")

        # Load model in background
        self.load_model_async()
        
        mode = self.config.get('mode', 'hold')
        print(f"Dictation app starting...")
        print(f"Mode: {mode}")
        print(f"Hotkey: [{self.config['hotkey']}]")
        print(f"Continuous hotkey: [{self.config.get('continuous_hotkey', 'ctrl+alt+d')}]")
        print(f"Wake word hotkey: [{self.config.get('wake_word_hotkey', 'ctrl+alt+w')}]")
        print(f"Using model: {self.config['model_size']}")
        print(f"Hotkey detection: state-based (simultaneous key support)")

        # Close splash and start system tray
        self.update_splash("Starting...")
        if self.splash:
            self.splash.close()
            self.splash = None
        self.create_tray_icon()

    def update_splash(self, status):
        """Update splash screen status"""
        if self.splash:
            try:
                self.splash.set_status(status)
            except:
                pass

    def load_config(self):
        """Load configuration from JSON file"""
        default_config = {
            "hotkey": "ctrl+shift",
            "continuous_hotkey": "ctrl+alt+d",
            "wake_word_hotkey": "ctrl+alt+w",
            "command_hotkey": "ctrl+alt+c",
            "undo_hotkey": "ctrl+alt+z",
            "cancel_hotkey": "escape",
            "mode": "hold",  # Options: "hold", "toggle", "continuous"
            "model_size": "base",
            "language": "en",
            "auto_paste": True,
            "add_trailing_space": True,
            "auto_capitalize": True,
            "format_numbers": True,
            "device": "auto",
            "microphone": None,
            "silence_threshold": DEFAULT_SILENCE_TIMEOUT,
            "min_speech_duration": DEFAULT_MIN_SPEECH_DURATION,
            "command_mode_enabled": False,
            "show_all_audio_devices": False,
            "audio_feedback": True,
            "sound_volume": 0.5,
            "sound_theme": "cute",
            "first_run_complete": True,
            # New nested wake word config
            "wake_word_config": {
                "enabled": True,
                "phrase": "samsara",
                "phrase_options": ["samsara", "hey samsara", "computer", "hey computer", "jarvis", "hey jarvis"],
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
            # Echo cancellation (removes system audio from mic input)
            "echo_cancellation": {
                "enabled": False,
                "latency_ms": 30.0,
            },
            # Performance mode for transcription speed/accuracy tradeoff
            "performance_mode": "balanced",  # "fast", "balanced", or "accurate"
            # Key macro system for accessibility (e.g., triple-tap W for auto-run)
            "key_macros": get_default_macro_config(),
            # Notification system for reminders (medication, breaks, hydration)
            "notifications": get_default_notification_config(),
            # Listening state indicator overlay
            "listening_indicator_enabled": False,
            "listening_indicator_position": "bottom-center",
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
            }
        }

        if self.config_path.exists():
            try:
                with open(self.config_path, 'r') as f:
                    self.config = json.load(f)
                
                # Migrate old flat wake word config to new nested structure
                self._migrate_wake_word_config(default_config)
                
                # Fill in any missing top-level keys
                for key in default_config:
                    if key not in self.config:
                        self.config[key] = default_config[key]
                
            except:
                self.config = default_config
        else:
            self.config = default_config
            self.save_config()
    
    def _migrate_wake_word_config(self, default_config):
        """Migrate old flat wake word settings to new nested structure"""
        # Migrate old wake_word/combined modes to wake_word_enabled + hold
        old_mode = self.config.get('mode')
        if old_mode in ('wake_word', 'combined'):
            self.config['wake_word_enabled'] = True
            self.config['mode'] = 'hold'
            print(f"[MIGRATE] mode='{old_mode}' -> mode='hold' + wake_word_enabled=True")

        # Check if we have old flat config but no new nested config
        if 'wake_word_config' not in self.config:
            # Create new nested config from defaults
            self.config['wake_word_config'] = default_config['wake_word_config'].copy()
            
            # Migrate old values if they exist
            if 'wake_word' in self.config:
                self.config['wake_word_config']['phrase'] = self.config['wake_word']
            if 'wake_word_timeout' in self.config:
                # Apply old timeout to dictate mode
                self.config['wake_word_config']['modes']['dictate']['silence_timeout'] = self.config['wake_word_timeout']
            if 'min_speech_duration' in self.config:
                self.config['wake_word_config']['audio']['min_speech_duration'] = self.config['min_speech_duration']
            
            # Save migrated config
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
        """
        tmp_path = self.config_path.with_suffix('.json.tmp')
        bak_path = self.config_path.with_suffix('.json.bak')

        try:
            # 1. Serialize to temp file. If json.dump raises, the real
            #    config.json is unaffected.
            with open(tmp_path, 'w') as f:
                json.dump(self.config, f, indent=2)

            # 2. Roll the existing good config to .bak (best-effort; ignore
            #    errors on the very first save when there's nothing to roll).
            if self.config_path.exists():
                try:
                    os.replace(self.config_path, bak_path)
                except OSError as e:
                    print(f"[WARN] Could not rotate config to .bak: {e}")

            # 3. Atomically promote tmp -> config.json
            os.replace(tmp_path, self.config_path)
        except Exception as e:
            # Clean up the temp file if we left one lying around
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            print(f"[ERROR] save_config failed: {e}")
            raise
    
    def update_config(self, changes, save=True):
        """Apply config changes and optionally save to disk.

        Central entry point for runtime config mutations. Provides a single
        place to hook side-effects (stream restarts, UI updates) and future
        plugin notifications.

        Args:
            changes: dict of key-value pairs to update
            save: whether to persist to disk (default True)
        """
        self.config.update(changes)

        # Apply runtime side-effects for keys that need them
        if 'mode' in changes:
            self.apply_mode(changes['mode'])
        if 'wake_word_enabled' in changes:
            self.set_wake_word_enabled(changes['wake_word_enabled'])
        if 'microphone' in changes:
            self.capture_rate = self._detect_capture_rate(changes['microphone'])

        if save:
            self.save_config()

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
        ww_config = self.config.get('wake_word_config', {})
        if 'audio' not in ww_config:
            ww_config['audio'] = {}
        ww_config['audio']['speech_threshold'] = threshold
        self.config['wake_word_config'] = ww_config

    def recalibrate_mic(self):
        """Re-run calibration in background and update config."""
        def _do():
            self._run_calibration_if_auto()
            self.save_config()
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
        if not text:
            return text

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
        was_prebuffer = self._prebuffer_stream is not None
        was_recording = self.recording

        # Stop everything first (order matters: active recording before its host stream)
        if was_recording:
            # Cancel rather than transcribe — the audio was captured on the wrong device
            self.cancel_recording()
        if was_continuous:
            self.stop_continuous_mode()
        if was_wake_word:
            self.stop_wake_word_mode()
        if was_prebuffer:
            self._stop_prebuffer_stream()

        # Update config + save
        self.config['microphone'] = mic_id
        self.capture_rate = self._detect_capture_rate(mic_id)
        self._run_calibration_if_auto()
        self.save_config()

        mic_name = self.get_current_microphone_name()
        print(f"[OK] Switched to microphone: {mic_name} ({self.capture_rate}Hz)")

        # Restart whatever was running, now bound to the new device
        if was_wake_word:
            self.start_wake_word_mode()
        if was_continuous:
            self.start_continuous_mode()
        if was_prebuffer and self.model_loaded:
            # Only restart the standalone prebuffer for hold/toggle modes —
            # wake_word / continuous have their own pre-buffering inside their streams
            mode = self.config.get('mode', 'hold')
            if mode in ('hold', 'toggle'):
                self._start_prebuffer_stream()

        # Update tray icon tooltip
        self._update_tray_tooltip()

    def load_model_async(self):
        """Load Whisper model in background thread"""
        def load():
            self.loading_model = True
            print("Loading Whisper model (this may take a moment on first run)...")
            
            # Determine compute device with detailed logging
            device = self.config['device']
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
            print("Ready for dictation.")
            
            # Auto-start modes that require always-on listening
            mode = self.config.get('mode', 'hold')
            if mode == 'continuous':
                print("[AUTO] Starting continuous mode...")
                self.start_continuous_mode()

            # Start pre-buffer stream for hold/toggle
            if mode in ('hold', 'toggle'):
                self._start_prebuffer_stream()

            # Auto-start wake word listener if enabled (works alongside any mode)
            if self.config.get('wake_word_enabled', False):
                print("[AUTO] Starting wake word listener...")
                self.start_wake_word_mode()
        
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
            print(f"[HOTKEY] Command hotkey detected: {command_hotkey}")
            self.hotkey_pressed = True
            self.command_mode_recording = True
            self.start_recording()
            return
        
        # Undo hotkey (works in any mode, edge-triggered)
        undo_hotkey = self.config.get('undo_hotkey', 'ctrl+alt+z')
        if self.check_hotkey_state(undo_hotkey) and not self.hotkey_pressed:
            print(f"[HOTKEY] Undo hotkey detected: {undo_hotkey}")
            self.hotkey_pressed = True
            threading.Thread(target=self.undo_last_dictation, daemon=True).start()
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

        # Handle main hotkey based on mode
        if self.check_hotkey_state(main_hotkey) and not self.hotkey_pressed:
            print(f"[HOTKEY] Main hotkey detected: {main_hotkey} (mode: {mode})")
            if mode == 'hold':
                self.hotkey_pressed = True
                self.start_recording()
            elif mode == 'toggle':
                self.hotkey_pressed = True
                if self.toggle_active:
                    self.toggle_active = False
                    self.stop_recording()
                else:
                    self.toggle_active = True
                    self.start_recording()
            elif mode == 'continuous':
                # In continuous mode, main hotkey toggles continuous listening
                self.hotkey_pressed = True
                self.toggle_continuous_mode()
    
    def on_key_release(self, key):
        """Handle key release - uses state-based checking for reliable detection"""
        key_name = self.get_key_name(key)
        if key_name and key_name in self.current_keys:
            self.current_keys.discard(key_name)
        
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
                if self.command_mode_recording and self.recording:
                    # Command hotkey released - stop recording (will process as command-only)
                    print(f"[HOTKEY] Command hotkey released, stopping recording")
                    self.stop_recording()
                elif mode == 'hold' and self.recording:
                    print(f"[HOTKEY] Main hotkey released, stopping recording")
                    self.stop_recording()
                self.hotkey_pressed = False

    def toggle_continuous_mode(self):
        """Toggle continuous listening mode"""
        if self.continuous_active:
            self.stop_continuous_mode()
        else:
            self.start_continuous_mode()
    
    def start_continuous_mode(self):
        """Start continuous listening with auto-transcribe on silence"""
        if not self.model_loaded:
            if self.loading_model:
                print("Model still loading, please wait...")
            return

        # Play start sound using winsound on Windows to avoid InputStream conflict
        self.play_sound("start", use_winsound=True)
        time.sleep(0.15)  # Brief pause for sound to start
        print("[MIC] Continuous mode ACTIVE - speak naturally, pauses will trigger transcription")

        with self.buffer_lock:
            self.speech_buffer = []
        self.silence_start = None
        self.is_speaking = False

        stream = self._open_stream_with_timeout(
            samplerate=self.capture_rate,
            channels=1,
            dtype=np.float32,
            callback=self.continuous_audio_callback,
            device=self.config['microphone'],
            blocksize=int(self.capture_rate * 0.1)  # 100ms blocks
        )
        if stream is None:
            print("[ERROR] Could not open continuous stream — audio subsystem busy. Try again.")
            self.play_sound("error")
            return
        
        self.continuous_stream = stream
        self.continuous_stream.start()
        self.set_app_state(continuous_active=True)

        # Update tray icon -- start chase animation
        self._request_icon_chase('continuous')

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, True)

    def stop_continuous_mode(self):
        """Stop continuous listening mode"""
        self.set_app_state(continuous_active=False)
        
        if self.continuous_stream:
            self.continuous_stream.stop()
            self.continuous_stream.close()
            self.continuous_stream = None
        
        # Transcribe any remaining audio
        with self.buffer_lock:
            remaining = self.speech_buffer.copy()
            self.speech_buffer = []
        if remaining:
            self.transcribe_continuous_buffer(remaining)

        print("[OFF] Continuous mode STOPPED")
        self.play_sound("stop")

        # Update tray icon — stop chase animation
        self._release_icon_chase('continuous')

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)

    def continuous_audio_callback(self, indata, frames, time_info, status):
        """Callback for continuous listening - detects speech and silence"""
        try:
            if status:
                print(f"[WARN] Audio status: {status}")
            
            if not self.continuous_active:
                return
            
            # Apply echo cancellation if active
            audio_chunk = indata.copy()
            if self.echo_canceller.is_active:
                audio_chunk = self.echo_canceller.process(audio_chunk)
            audio_chunk = audio_chunk.flatten()
            
            # Calculate RMS energy to detect speech
            rms = np.sqrt(np.mean(audio_chunk**2))
            
            # Threshold for speech detection (from wake word config, shared across modes)
            ww_audio = self.config.get('wake_word_config', {}).get('audio', {})
            speech_threshold = ww_audio.get('speech_threshold', DEFAULT_SPEECH_THRESHOLD)
            silence_threshold = self.config.get('silence_threshold', DEFAULT_SILENCE_TIMEOUT)
            min_speech = self.config.get('min_speech_duration', DEFAULT_MIN_SPEECH_DURATION)

            if rms > speech_threshold:
                # Speech detected
                self.is_speaking = True
                self.silence_start = None
                with self.buffer_lock:
                    self.speech_buffer.append(audio_chunk)
            else:
                # Silence detected
                if self.is_speaking:
                    # Still capture some silence at the end for context
                    with self.buffer_lock:
                        self.speech_buffer.append(audio_chunk)

                    if self.silence_start is None:
                        self.silence_start = time.time()
                    elif time.time() - self.silence_start >= silence_threshold:
                        # Enough silence - check if we have enough speech
                        with self.buffer_lock:
                            speech_duration = len(self.speech_buffer) * 0.1  # Each block is 100ms

                            if speech_duration >= min_speech:
                                buffer_copy = self.speech_buffer.copy()
                                self.speech_buffer = []
                            else:
                                buffer_copy = None
                                self.speech_buffer = []

                        if buffer_copy is not None:
                            self.is_speaking = False
                            self.silence_start = None
                            thread = threading.Thread(
                                target=self.transcribe_continuous_buffer,
                                args=(buffer_copy,),
                                daemon=True
                            )
                            thread.start()
                        else:
                            self.is_speaking = False
                            self.silence_start = None
        except Exception as e:
            print(f"[ERROR] Audio callback exception: {e}")

    def transcribe_continuous_buffer(self, buffer):
        """Transcribe a buffer from continuous mode"""
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, self.capture_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate
            
            # Get transcription parameters based on performance mode
            transcribe_params = self.get_transcription_params()
            perf_mode = self.config.get('performance_mode', 'balanced')
            
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
                    # Command was executed
                    return

                # Not a command, proceed with dictation
                # Apply text processing (auto-capitalize, number formatting)
                text = self.process_transcription(text)

                if self.config['add_trailing_space']:
                    text = text + " "

                print(f"[TEXT] {text}")

                if self.config['auto_paste']:
                    self._paste_preserving_clipboard(text)

        except Exception as e:
            print(f"Transcription error: {e}")

    def transcribe_buffer(self):
        """Transcribe remaining buffer when stopping"""
        with self.buffer_lock:
            if not self.speech_buffer:
                return
            buffer_copy = self.speech_buffer.copy()
            self.speech_buffer = []
        self.transcribe_continuous_buffer(buffer_copy)
    
    def toggle_wake_word_mode(self):
        """Toggle wake word listening mode"""
        if self.wake_word_active:
            self.stop_wake_word_mode()
        else:
            self.start_wake_word_mode()
    
    def start_wake_word_mode(self):
        """Start wake word listening - always listening for wake word"""
        if not self.model_loaded:
            if self.loading_model:
                print("Model still loading, please wait...")
            return

        # Play start sound using winsound on Windows to avoid InputStream conflict
        self.play_sound("start", use_winsound=True)
        time.sleep(0.15)  # Brief pause for sound to start
        wake_word = self.config.get('wake_word', 'hey claude')
        print(f"[LISTEN] Wake word mode ACTIVE - say '{wake_word}' to give commands")

        with self.buffer_lock:
            self.speech_buffer = []
        self.silence_start = None
        self.is_speaking = False
        self.wake_word_triggered = False

        stream = self._open_stream_with_timeout(
            samplerate=self.capture_rate,
            channels=1,
            dtype=np.float32,
            callback=self.wake_word_audio_callback,
            device=self.config['microphone'],
            blocksize=int(self.capture_rate * 0.1)  # 100ms blocks
        )
        if stream is None:
            print("[ERROR] Could not open wake word stream — audio subsystem busy. Try again.")
            self.play_sound("error")
            return
        
        self.continuous_stream = stream
        self.continuous_stream.start()
        self.set_app_state(wake_word_active=True)

        # Update tray icon -- start chase animation
        self._request_icon_chase('wake_word')

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, True)

    def stop_wake_word_mode(self):
        """Stop wake word listening mode"""
        self.set_app_state(wake_word_active=False)
        self.wake_word_triggered = False
        
        # Reset dictation mode
        self._reset_wake_dictation()
        
        if self.continuous_stream:
            self.continuous_stream.stop()
            self.continuous_stream.close()
            self.continuous_stream = None
        
        # Transcribe any remaining audio if wake word was triggered
        with self.buffer_lock:
            if self.speech_buffer and self.wake_word_triggered:
                buffer_copy = self.speech_buffer.copy()
            else:
                buffer_copy = None
            self.speech_buffer = []

        if buffer_copy:
            self.process_wake_word_buffer(buffer_copy)
        
        print("[OFF] Wake word mode STOPPED")
        self.play_sound("stop")

        # Update tray icon — stop chase animation
        self._release_icon_chase('wake_word')

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)

    def wake_word_audio_callback(self, indata, frames, time_info, status):
        """Callback for wake word listening"""
        try:
            if status:
                print(f"[WARN] Audio status: {status}")
            
            if not self.wake_word_active:
                return
            
            # Apply echo cancellation if active
            audio_chunk = indata.copy()
            if self.echo_canceller.is_active:
                audio_chunk = self.echo_canceller.process(audio_chunk)
            audio_chunk = audio_chunk.flatten()
            
            # If hotkey recording is active, skip everything -
            # the recording stream handles audio capture directly,
            # and we must NOT feed the pre-buffer to avoid overlap on next session
            if self._hotkey_recording:
                return
            
            # Feed the pre-buffer (only when not hotkey recording)
            self._prebuffer.append(audio_chunk.copy())
            
            rms = np.sqrt(np.mean(audio_chunk**2))
            
            # Get thresholds from config
            ww_config = self.config.get('wake_word_config', {})
            audio_config = ww_config.get('audio', {})
            speech_threshold = audio_config.get('speech_threshold', DEFAULT_SPEECH_THRESHOLD)
            min_speech = audio_config.get('min_speech_duration', DEFAULT_MIN_SPEECH_DURATION)
            
            # Use dynamic silence timeout if in dictation mode, otherwise use fast wake detection
            if self.app_state == 'long_dictation':
                # Long dictation: no silence timeout -- mic stays hot indefinitely.
                # Use a very large value so the silence branch never fires.
                silence_threshold = 999999.0
            elif self.app_state == 'quick_dictation' and self._dictation_silence_timeout:
                silence_threshold = self._dictation_silence_timeout
            else:
                # Asleep / command_window -- use wake detection silence
                silence_threshold = audio_config.get('wake_detection_silence', WAKE_DETECTION_SILENCE)
            
            if rms > speech_threshold:
                if self.is_speaking:
                    # Already confirmed speech — keep buffering
                    self.silence_start = None
                    with self.buffer_lock:
                        self.speech_buffer.append(audio_chunk)
                        # Cap buffer at 5 seconds to prevent unbounded accumulation
                        if len(self.speech_buffer) >= 50:  # 50 chunks × 100ms = 5s
                            buffer_copy = self.speech_buffer.copy()
                            self.speech_buffer = []
                            self.is_speaking = False
                            self._speech_onset_count = 0
                            threading.Thread(
                                target=self.process_wake_word_buffer,
                                args=(buffer_copy,), daemon=True
                            ).start()
                else:
                    # Not yet speaking — require 3 consecutive loud chunks
                    # to filter fan noise / ambient hum from real speech
                    self._speech_onset_count += 1
                    self._speech_onset_pending.append(audio_chunk)
                    if self._speech_onset_count >= 3:  # 300ms of sustained energy
                        self.is_speaking = True
                        self.silence_start = None
                        with self.buffer_lock:
                            # Prepend the debounce chunks so we don't lose speech start
                            self.speech_buffer.extend(self._speech_onset_pending)
                        self._speech_onset_pending = []
            else:
                # Below threshold — reset onset counter
                self._speech_onset_count = 0
                self._speech_onset_pending = []
                # Silence detected
                if self.is_speaking:
                    with self.buffer_lock:
                        self.speech_buffer.append(audio_chunk)

                    if self.silence_start is None:
                        self.silence_start = time.time()
                    elif time.time() - self.silence_start >= silence_threshold:
                        # Enough silence - check if we have enough speech
                        with self.buffer_lock:
                            speech_duration = len(self.speech_buffer) * 0.1

                            if speech_duration >= min_speech:
                                buffer_copy = self.speech_buffer.copy()
                                self.speech_buffer = []
                            else:
                                buffer_copy = None
                                self.speech_buffer = []

                        if buffer_copy is not None:
                            self.is_speaking = False
                            self.silence_start = None
                            thread = threading.Thread(
                                target=self.process_wake_word_buffer,
                                args=(buffer_copy,),
                                daemon=True
                            )
                            thread.start()
                        else:
                            self.is_speaking = False
                            self.silence_start = None
        except Exception as e:
            print(f"[ERROR] Audio callback exception: {e}")
    
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

    def process_wake_word_buffer(self, buffer):
        """Process audio - check for wake word, commands, or dictation content"""
        try:
            audio = np.concatenate(buffer)
            audio = resample_audio(audio, self.capture_rate, self.model_rate)
            audio_duration = len(audio) / self.model_rate
            
            # Get transcription parameters based on performance mode
            transcribe_params = self.get_transcription_params()
            perf_mode = self.config.get('performance_mode', 'balanced')
            
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
                return
            
            # Get wake word config
            ww_config = self.config.get('wake_word_config', {})
            wake_phrase = ww_config.get('phrase', 'samsara').lower()

            self._emit_wake_trace({"stage": "utterance_start", "raw": text, "normalized": text_lower})

            # In dictation state (quick_dictation or long_dictation)?
            if self.app_state in ('quick_dictation', 'long_dictation'):
                # Check cancel words
                cancel_words = ww_config.get('cancel_words', ['cancel'])
                for cw in cancel_words:
                    if cw.lower() in text_lower:
                        print(f"[CANCEL] Dictation cancelled ('{cw}')")
                        self._emit_wake_trace({"stage": "cancel_word_detected", "phrase": cw})
                        self.play_sound("error")
                        self._reset_wake_dictation()
                        self._emit_wake_trace({"stage": "utterance_end", "result": "cancelled"})
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
            print(f"Wake word processing error: {e}")
            import traceback
            traceback.print_exc()
    
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
                self.wake_word_triggered = False
                self.app_state = 'asleep'
                print("[STATE] command_window -> asleep (command executed)")
                self._indicator_success_and_reset()
                return

            # Not a recognized command -- output as quick dictation
            self._output_dictation(text)
            self.wake_word_triggered = False
            self.app_state = 'asleep'
            print("[STATE] command_window -> asleep (text output)")
            self._indicator_success_and_reset()
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
            self._dictation_silence_timeout = None  # no silence timeout
            self._dictation_require_end = True
            print(f"[STATE] {old_state} -> long_dictation (end word required)")

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

        if old_state != 'asleep':
            print(f"[STATE] {old_state} -> asleep")

        # Reset listening indicator back to idle
        self._indicator_reset()

    def _restart_dictation_timer(self):
        """Restart the finalization timer for non-end-word dictation modes.

        After accumulating text, this timer gives the user a window to keep speaking.
        If no new speech arrives within the timeout, the accumulated text is output.
        """
        if hasattr(self, '_dictation_finalize_timer') and self._dictation_finalize_timer:
            self._dictation_finalize_timer.cancel()

        timeout = self._dictation_silence_timeout or 0.6
        self._dictation_finalize_timer = threading.Timer(timeout, self._finalize_dictation_timeout)
        self._dictation_finalize_timer.start()

    def _finalize_dictation_timeout(self):
        """Called when the dictation finalization timer expires."""
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

    def _output_dictation(self, text):
        """Output dictated text"""
        # Apply text processing (auto-capitalize, number formatting)
        text = self.process_transcription(text)

        if self.config['add_trailing_space']:
            text = text + " "

        print(f"[OK] {text}")
        self.play_sound("success")
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.flash_success)

        # Add to history
        self.add_to_history(text.strip(), is_command=False)

        if self.config['auto_paste']:
            self._paste_preserving_clipboard(text)
    
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

    def audio_callback(self, indata, frames, time_info, status):
        """Callback for audio stream (hold/toggle mode)"""
        try:
            if status:
                print(f"[WARN] Audio status: {status}")
            
            if self.recording:
                chunk = indata.copy()
                if self.echo_canceller.is_active:
                    chunk = self.echo_canceller.process(chunk)
                self.audio_data.append(chunk)
        except Exception as e:
            print(f"[ERROR] Audio callback exception: {e}")

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

    def _load_sound_cache(self):
        """Pre-load all sound files into memory, normalized to common sample rate.
        
        Supports WAV natively, and MP3/OGG/FLAC if pydub is installed.
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
            sound_type: 'start', 'stop', 'success', or 'error'
            use_winsound: Deprecated/ignored.
        """
        if not self.config.get('audio_feedback', True):
            return

        cached = self._sound_cache.get(sound_type)
        if cached is None:
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

    def _prebuffer_callback(self, indata, frames, time_info, status):
        """Audio callback for standalone pre-buffer stream (hold/toggle modes).
        Simply feeds a rolling buffer so audio before hotkey press is captured."""
        try:
            if status:
                pass  # Ignore status warnings for background stream
            chunk = indata.copy()
            if self.echo_canceller.is_active:
                chunk = self.echo_canceller.process(chunk)
            self._prebuffer.append(chunk.flatten())
        except Exception:
            pass

    def _open_stream_with_timeout(self, timeout=3.0, retries=2, backoff=0.5, **kwargs):
        """Open an sd.InputStream with a timeout to prevent hangs.
        
        When Windows audio subsystem is disrupted (e.g. headphone connect/disconnect),
        sd.InputStream() can block indefinitely. This wraps it in a thread with a timeout
        and retries with backoff.
        
        Args:
            timeout: Seconds to wait for stream creation before giving up
            retries: Number of retry attempts after first failure
            backoff: Seconds to wait between retries (doubles each attempt)
            **kwargs: Passed directly to sd.InputStream()
            
        Returns:
            sd.InputStream or None if all attempts failed
        """
        for attempt in range(1 + retries):
            result = [None]
            error = [None]
            
            def _create():
                try:
                    result[0] = sd.InputStream(**kwargs)
                except Exception as e:
                    error[0] = e
            
            t = threading.Thread(target=_create, daemon=True)
            t.start()
            t.join(timeout=timeout)
            
            if t.is_alive():
                # Stream creation hung — PortAudio is probably disrupted
                wait = backoff * (2 ** attempt)
                if attempt < retries:
                    print(f"[WARN] Audio stream open timed out ({timeout}s), retrying in {wait:.1f}s... (attempt {attempt + 1}/{1 + retries})")
                    time.sleep(wait)
                    continue
                else:
                    print(f"[ERROR] Audio stream open timed out after {1 + retries} attempts. Audio subsystem may be disrupted.")
                    return None
            
            if error[0]:
                wait = backoff * (2 ** attempt)
                if attempt < retries:
                    print(f"[WARN] Audio stream error: {error[0]}, retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                else:
                    print(f"[ERROR] Audio stream failed after {1 + retries} attempts: {error[0]}")
                    return None
            
            return result[0]
        
        return None

    def _start_prebuffer_stream(self):
        """Start a lightweight background audio stream for pre-buffering.
        Only used in hold/toggle modes where no other stream is running."""
        if self._prebuffer_stream is not None:
            return  # Already running
        try:
            stream = self._open_stream_with_timeout(
                samplerate=self.capture_rate,
                channels=1,
                dtype=np.float32,
                callback=self._prebuffer_callback,
                device=self.config['microphone'],
                blocksize=int(self.capture_rate * 0.1)  # 100ms blocks
            )
            if stream is None:
                print("[WARN] Could not start pre-buffer stream (audio subsystem busy)")
                return
            self._prebuffer_stream = stream
            self._prebuffer_stream.start()
            self._prebuffer_active = True
            print(f"[PRE] Pre-buffer stream started ({self._prebuffer_seconds}s rolling buffer)")
        except Exception as e:
            print(f"[WARN] Could not start pre-buffer stream: {e}")

    def _stop_prebuffer_stream(self):
        """Stop the standalone pre-buffer stream."""
        if self._prebuffer_stream is not None:
            try:
                self._prebuffer_stream.stop()
                self._prebuffer_stream.close()
            except Exception:
                pass
            self._prebuffer_stream = None
            self._prebuffer_active = False

    def start_recording(self):
        """Start recording audio"""
        if not self.model_loaded:
            if self.loading_model:
                print("Model still loading, please wait...")
            else:
                print("Model not loaded!")
            return

        # Suppress wake word processing during hotkey recording
        self._hotkey_recording = True

        # Grab pre-buffer contents BEFORE playing the start sound
        # This captures audio from ~1.5s before the hotkey was pressed
        prebuffer_audio = list(self._prebuffer)
        self._prebuffer.clear()

        # Stop the pre-buffer stream to avoid two streams on the same device.
        # Running dual streams causes PortAudio to deliver overlapping audio data,
        # leading to duplicated words (especially on quick stops).
        self._stop_prebuffer_stream()

        # Play start sound using winsound on Windows to avoid InputStream conflict
        self.play_sound("start", use_winsound=True)
        time.sleep(0.15)  # Brief pause for sound to start

        # Seed audio_data with pre-buffered audio (speech before hotkey press)
        self.audio_data = [chunk.reshape(-1, 1) for chunk in prebuffer_audio] if prebuffer_audio else []
        if prebuffer_audio:
            prebuf_duration = len(prebuffer_audio) * 0.1
            print(f"[PRE] Pre-buffer: {prebuf_duration:.1f}s of audio captured before hotkey")
        
        # Record timestamp so we can detect overlap between pre-buffer and recording
        self._recording_start_time = time.time()
        
        stream = self._open_stream_with_timeout(
            samplerate=self.capture_rate,
            channels=1,
            dtype=np.float32,
            callback=self.audio_callback,
            device=self.config['microphone']
        )
        if stream is None:
            print("[ERROR] Could not open recording stream — audio subsystem may be disrupted by device change. Try again in a moment.")
            self._hotkey_recording = False
            self.play_sound("error")
            if hasattr(self, 'listening_indicator'):
                self._schedule_ui(self.listening_indicator.flash_error)
            # Try to restart pre-buffer so next attempt works
            self._start_prebuffer_stream()
            return
        
        self.stream = stream
        self.stream.start()
        self.set_app_state(recording=True)

        # Update tray icon to show active recording (critical for toggle mode
        # where there's no physical key-hold to indicate state)
        self._request_icon_chase('recording')
        if hasattr(self, 'tray_icon'):
            self.tray_icon.title = f"Samsara - RECORDING"

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, True)

    def stop_recording(self):
        """Stop recording and transcribe"""
        if not self.recording:
            return

        self.set_app_state(recording=False)
        self._hotkey_recording = False  # Re-enable wake word processing
        self.play_sound("stop")

        # Restore tray icon — release recording reason (wake_word may keep it spinning)
        self._release_icon_chase('recording')
        self._update_tray_tooltip()

        # Update listening indicator
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_listening, False)
        
        if hasattr(self, 'stream'):
            self.stream.stop()
            self.stream.close()

        # Restart pre-buffer stream for next recording session
        self._start_prebuffer_stream()

        if not self.audio_data:
            print("No audio recorded")
            return
        
        print("[...] Transcribing...")
        
        # Combine audio chunks and resample to model rate
        audio = np.concatenate(self.audio_data, axis=0).flatten()
        audio = resample_audio(audio, self.capture_rate, self.model_rate)

        # Transcribe in background to not block hotkey listener
        def transcribe():
            try:
                audio_duration = len(audio) / self.model_rate
                
                # Get transcription parameters based on performance mode
                transcribe_params = self.get_transcription_params()
                perf_mode = self.config.get('performance_mode', 'balanced')
                
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
                
                # Check if we're in command-only mode (from command hotkey)
                is_command_mode = self.command_mode_recording
                self.command_mode_recording = False  # Reset flag
                
                if text:
                    # Check for command mode toggle OR regular commands
                    result, was_command = self.command_executor.process_text(text, self)

                    if was_command:
                        # Command was executed - add to history as command
                        self.add_to_history(text, is_command=True)
                        return

                    # Not a command
                    if is_command_mode:
                        # In command-only mode, don't output text if no command matched
                        print(f"[CMD] No command matched: '{text}'")
                        return
                    
                    # Regular dictation mode - proceed with text output
                    # Apply text processing (auto-capitalize, number formatting)
                    text = self.process_transcription(text)

                    if self.config['add_trailing_space']:
                        text = text + " "

                    print(f"[OK] {text}")
                    self.play_sound("success")
                    if hasattr(self, 'listening_indicator'):
                        self._schedule_ui(self.listening_indicator.flash_success)

                    # Add to history
                    self.add_to_history(text.strip(), is_command=False)

                    if self.config['auto_paste']:
                        self._paste_preserving_clipboard(text)
                else:
                    print("No speech detected")
                    self.command_mode_recording = False  # Reset flag on no speech too

            except Exception as e:
                print(f"Transcription error: {e}")
                self.play_sound("error")
                if hasattr(self, 'listening_indicator'):
                    self._schedule_ui(self.listening_indicator.flash_error)
        
        thread = threading.Thread(target=transcribe, daemon=True)
        thread.start()

    def cancel_recording(self):
        """Cancel recording without transcribing"""
        if not self.recording:
            return

        self.set_app_state(recording=False)
        self._hotkey_recording = False  # Re-enable wake word processing
        print("[X] Recording cancelled")

        if hasattr(self, 'stream'):
            self.stream.stop()
            self.stream.close()

        # Clear audio data without transcribing
        self.audio_data = []
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

        # Manage pre-buffer stream: hold/toggle get a standalone stream,
        # continuous mode handles its own audio
        if new_mode in ('hold', 'toggle'):
            if self.model_loaded:
                self._start_prebuffer_stream()
        else:
            self._stop_prebuffer_stream()

        # Activate continuous if that's the new mode
        if new_mode == 'continuous' and not self.continuous_active:
            self.start_continuous_mode()
            print(f"[MODE] Activated continuous mode")

        self.config['mode'] = new_mode
        print(f"[MODE] Mode changed to: {new_mode}")

        # Update listening indicator and tray tooltip
        display = self._get_mode_display()
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_mode, display)
        self._update_tray_tooltip()

        return True

    def set_wake_word_enabled(self, enabled):
        """Start or stop the wake word listener independently of capture mode."""
        self.config['wake_word_enabled'] = enabled
        self.save_config()
        if enabled and not self.wake_word_active:
            self.start_wake_word_mode()
            print("[WAKE] Wake word listener ENABLED")
        elif not enabled and self.wake_word_active:
            self.stop_wake_word_mode()
            print("[WAKE] Wake word listener DISABLED")
        # Update tray tooltip and menu
        self._update_tray_tooltip()
        if hasattr(self, 'tray_icon') and hasattr(self, 'get_menu'):
            try:
                self.tray_icon.menu = self.get_menu()
            except Exception:
                pass
        # Update listening indicator mode label
        if hasattr(self, 'listening_indicator'):
            self._schedule_ui(self.listening_indicator.set_mode, self._get_mode_display())

    def switch_mode_from_tray(self, new_mode):
        """Tray-menu entry point: apply the mode, persist it, refresh the menu."""
        changed = self.apply_mode(new_mode)
        if changed:
            self.save_config()
        # Always refresh the menu so the checkmark reflects current state
        if hasattr(self, 'tray_icon') and hasattr(self, 'get_menu'):
            try:
                self.tray_icon.menu = self.get_menu()
            except Exception as e:
                print(f"[MODE] Failed to refresh tray menu: {e}")
        self._update_tray_tooltip()

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
        """Schedule a function on the tkinter main thread.
        Silently ignores RuntimeError if the mainloop hasn't started yet
        (e.g. during the background load thread) or is shutting down."""
        try:
            self.root.after(0, func, *args)
        except RuntimeError:
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
        # If window already exists and is open, just bring it to front
        if self.settings_window.window is not None:
            try:
                self.settings_window.window.lift()
                self.settings_window.window.focus_force()
                return
            except:
                # Window was closed improperly, reset it
                self.settings_window.window = None

        # Call directly on main thread to prevent issues
        try:
            self.settings_window.show()
        except Exception as e:
            print(f"Error opening settings: {e}")
            # Reset the window and try again
            self.settings_window.window = None
            try:
                self.settings_window.show()
            except Exception as e2:
                print(f"Failed to open settings after reset: {e2}")
                messagebox.showerror("Error", f"Failed to open Settings:\n{e2}")
    
    def open_voice_training(self):
        """Open voice training window"""
        if self.voice_training_window.window is not None:
            try:
                self.voice_training_window.window.lift()
                self.voice_training_window.window.focus_force()
                return
            except:
                self.voice_training_window.window = None

        # Don't use threading for window creation - call directly on main thread
        try:
            self.voice_training_window.show()
        except Exception as e:
            print(f"Error opening voice training: {e}")
            messagebox.showerror("Error", f"Failed to open Voice Training:\n{e}")

    def open_history(self):
        """Open dictation history window"""
        if self.history_window.window is not None:
            try:
                self.history_window.window.lift()
                self.history_window.window.focus_force()
                return
            except:
                self.history_window.window = None

        try:
            self.history_window.show()
        except Exception as e:
            print(f"Error opening history: {e}")
            messagebox.showerror("Error", f"Failed to open History:\n{e}")

    def open_wake_word_debug(self):
        """Open wake word debug/test window"""
        if self.wake_word_debug_window.window is not None:
            try:
                self.wake_word_debug_window.window.lift()
                self.wake_word_debug_window.window.focus_force()
                return
            except:
                self.wake_word_debug_window.window = None

        try:
            self.wake_word_debug_window.show()
        except Exception as e:
            print(f"Error opening wake word debug: {e}")
            messagebox.showerror("Error", f"Failed to open Wake Word Debug:\n{e}")

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

        # Stop pre-buffer stream so no audio processing happens
        self._stop_prebuffer_stream()

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

        # Restart pre-buffer for hold/toggle modes
        if mode in ('hold', 'toggle') and self.model_loaded:
            self._start_prebuffer_stream()

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

    def toggle_listening_indicator(self):
        """Toggle the listening indicator overlay on/off and persist to config."""
        enabled = not self.config.get('listening_indicator_enabled', False)
        self.config['listening_indicator_enabled'] = enabled
        self.save_config()
        if enabled:
            self._schedule_ui(self.listening_indicator.show)
        else:
            self._schedule_ui(self.listening_indicator.hide)

    def create_tray_icon(self):
        """Create and run system tray icon"""
        def get_menu():
            """Generate menu dynamically to reflect current state"""
            mode = self.config.get('mode', 'hold')
            
            # Create microphone submenu
            mic_menu_items = []
            current_mic_id = self.config.get('microphone')

            # Factory: returns a clean 2-arg callback that captures mic_id in
            # its closure scope. Avoids two pitfalls:
            #   (a) `lambda _, mid=mic_id: ...` binds menu_item to mid (wrong value)
            #   (b) `lambda _i, _it, mid=mic_id: ...` exceeds pystray's 2-arg limit
            def _make_mic_callback(mid):
                def _cb(_icon, _item):
                    self.switch_microphone_and_refresh(mid)
                return _cb

            for mic in self.available_mics:
                mic_id = mic['id']
                mic_name = mic['name']
                is_current = (mic_id == current_mic_id)
                
                # Create menu item with checkmark for current mic
                mic_menu_items.append(
                    pystray.MenuItem(
                        f"{'*' if is_current else '   '}{mic_name}",
                        _make_mic_callback(mic_id)
                    )
                )
            
            return pystray.Menu(
                pystray.MenuItem(
                    f"[MIC] {self.get_current_microphone_name()}", 
                    pystray.Menu(*mic_menu_items) if mic_menu_items else None
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    f"Mode: {mode.title()}",
                    pystray.Menu(
                        pystray.MenuItem(
                            'Hold to Talk',
                            lambda _i, _it: self.switch_mode_from_tray('hold'),
                            checked=lambda _it, m='hold': self.config.get('mode', 'hold') == m,
                            radio=True
                        ),
                        pystray.MenuItem(
                            'Toggle (click to start/stop)',
                            lambda _i, _it: self.switch_mode_from_tray('toggle'),
                            checked=lambda _it, m='toggle': self.config.get('mode', 'hold') == m,
                            radio=True
                        ),
                        pystray.MenuItem(
                            'Continuous',
                            lambda _i, _it: self.switch_mode_from_tray('continuous'),
                            checked=lambda _it, m='continuous': self.config.get('mode', 'hold') == m,
                            radio=True
                        ),
                    )
                ),
                pystray.MenuItem(
                    f"Wake Word ({self.config.get('wake_word_config', {}).get('phrase', 'samsara')})",
                    lambda _i, _it: self.set_wake_word_enabled(
                        not self.config.get('wake_word_enabled', False)),
                    checked=lambda _it: self.config.get('wake_word_enabled', False)
                ),
                pystray.MenuItem(
                    f"Hotkey: {self.config['hotkey']}", 
                    lambda: None,
                    enabled=False
                ),
                pystray.MenuItem(
                    f"Model: {self.config['model_size']}",
                    lambda: None,
                    enabled=False
                ),
                pystray.MenuItem(
                    "Snoozed" if self.snoozed else "Snooze",
                    pystray.Menu(
                        pystray.MenuItem(
                            "Snooze 5 minutes",
                            lambda _i, _it: self.snooze_listening(5),
                            enabled=not self.snoozed
                        ),
                        pystray.MenuItem(
                            "Snooze 15 minutes",
                            lambda _i, _it: self.snooze_listening(15),
                            enabled=not self.snoozed
                        ),
                        pystray.MenuItem(
                            "Snooze 30 minutes",
                            lambda _i, _it: self.snooze_listening(30),
                            enabled=not self.snoozed
                        ),
                        pystray.MenuItem(
                            "Snooze 1 hour",
                            lambda _i, _it: self.snooze_listening(60),
                            enabled=not self.snoozed
                        ),
                        pystray.MenuItem(
                            "Snooze until resumed",
                            lambda _i, _it: self.snooze_listening(None),
                            enabled=not self.snoozed
                        ),
                        pystray.Menu.SEPARATOR,
                        pystray.MenuItem(
                            "Resume now",
                            lambda _i, _it: self.resume_listening(),
                            enabled=self.snoozed
                        ),
                    )
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Settings", self.open_settings),
                pystray.MenuItem("History", self.open_history),
                pystray.MenuItem("Voice Training", self.open_voice_training),
                pystray.MenuItem("Wake Word Debug", self.open_wake_word_debug),
                pystray.MenuItem(
                    "Show Listening Indicator",
                    self.toggle_listening_indicator,
                    checked=lambda _it: self.config.get('listening_indicator_enabled', False)
                ),
                pystray.MenuItem("Recalibrate Mic", lambda _i, _it: self.recalibrate_mic()),
                pystray.MenuItem("Open Config Folder", self.open_config_folder),
                pystray.MenuItem("View Logs", pystray.Menu(
                    pystray.MenuItem("Main Log (samsara.log)", self.open_main_log),
                    pystray.MenuItem("Voice Training Log", self.open_voice_training_log)
                )),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self.quit_app)
            )
        
        self.tray_icon = pystray.Icon(
            "Samsara",
            self.create_icon_image(),
            f"Samsara - {self._get_mode_display()}",
            get_menu()
        )
        
        # Store the menu generator for updates
        self.get_menu = get_menu

        # Run pystray in detached mode (separate thread) so tkinter can run on main thread
        self.tray_icon.run_detached()

        # Run tkinter mainloop on main thread (required for window controls to work)
        self.root.mainloop()
    
    def switch_microphone_and_refresh(self, mic_id):
        """Switch microphone and refresh the tray menu"""
        self.switch_microphone(mic_id)
        # Recreate menu to show updated checkmark
        if hasattr(self, 'tray_icon') and hasattr(self, 'get_menu'):
            self.tray_icon.menu = self.get_menu()
    
    def open_config_folder(self):
        """Open the config folder"""
        open_file_or_folder(self.config_path.parent)

    def open_main_log(self):
        """Open the main log file in default text editor"""
        log_file = LOG_FILE
        if log_file.exists():
            open_file_or_folder(log_file)
        else:
            messagebox.showinfo("Log File", "No log file found yet.")

    def open_voice_training_log(self):
        """Open the voice training log file"""
        log_file = LOG_DIR / 'voice_training.log'
        if log_file.exists():
            open_file_or_folder(log_file)
        else:
            messagebox.showinfo("Log File", "No voice training log file found yet.")
    
    def quit_app(self):
        """Exit the application"""
        print("[EXIT] Shutting down Samsara...")

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

        # Stop persistent sound stream
        try:
            self.stop_sound_stream()
        except:
            pass

        # Stop keyboard listener
        try:
            self.keyboard_listener.stop()
        except:
            pass

        # Stop tray icon (do this before GUI cleanup)
        try:
            self.tray_icon.stop()
        except:
            pass
        
        # Force exit - os._exit bypasses cleanup but guarantees termination
        # This is necessary because pystray calls us from a background thread
        # and tkinter GUI cleanup from non-main thread can hang
        print("[EXIT] Goodbye!")
        os._exit(0)

if __name__ == "__main__":
    # Console is already hidden at top of file

    # Show splash screen during startup
    splash = SplashScreen()
    splash.set_status("Initializing...")

    try:
        app = DictationApp(splash)
    except Exception as e:
        splash.close()
        raise e
