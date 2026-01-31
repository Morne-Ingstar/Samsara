import os
import sys
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

_hide_console_now()

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

import threading
import queue
import time
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
from samsara.ui.profile_manager_ui import ProfileManagerWindow
from samsara.ui.wake_word_debug import WakeWordDebugWindow
from samsara.key_macros import KeyMacroManager, get_default_macro_config
from samsara.notifications import NotificationManager, get_default_notification_config


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


class SplashScreen:
    """Loading splash screen shown during startup"""

    def __init__(self):
        self.start_time = time.time()
        self.min_display_time = 3.0  # Minimum seconds to show splash

        # Create hidden root that will be reused by the app
        self.root = tk.Tk()
        self.root.withdraw()  # Hide root

        # Create splash as Toplevel
        self.splash = tk.Toplevel(self.root)
        self.splash.title("Samsara")
        self.splash.overrideredirect(True)  # No window decorations
        self.splash.attributes('-topmost', True)

        # Window size and centering
        width, height = 350, 150
        x = (self.splash.winfo_screenwidth() // 2) - (width // 2)
        y = (self.splash.winfo_screenheight() // 2) - (height // 2)
        self.splash.geometry(f"{width}x{height}+{x}+{y}")

        # Dark theme
        self.splash.configure(bg='#2d2d2d')

        # App name
        tk.Label(self.splash, text="Samsara", font=('Segoe UI', 20, 'bold'),
                bg='#2d2d2d', fg='#00CED1').pack(pady=(25, 5))

        # Status text
        self.status_var = tk.StringVar(value="Starting...")
        self.status_label = tk.Label(self.splash, textvariable=self.status_var,
                                      font=('Segoe UI', 10), bg='#2d2d2d', fg='#aaaaaa')
        self.status_label.pack(pady=(5, 15))

        # Progress bar
        self.progress = ttk.Progressbar(self.splash, length=280, mode='indeterminate')
        self.progress.pack(pady=(0, 20))
        self.progress.start(15)

        # Force splash to be visible
        self.splash.lift()
        self.splash.focus_force()
        self.splash.update_idletasks()
        self.splash.update()
        self.root.update_idletasks()
        self.root.update()

    def set_status(self, text):
        """Update status text"""
        self.status_var.set(text)
        self.splash.update_idletasks()
        self.splash.update()
        self.root.update_idletasks()
        self.root.update()

    def close(self):
        """Close the splash screen but keep root for app to reuse"""
        try:
            # Ensure minimum display time
            elapsed = time.time() - self.start_time
            if elapsed < self.min_display_time:
                remaining = self.min_display_time - elapsed
                # Update splash during wait
                wait_until = time.time() + remaining
                while time.time() < wait_until:
                    self.splash.update()
                    time.sleep(0.05)

            self.progress.stop()
            self.splash.destroy()
        except:
            pass

    def get_root(self):
        """Return the root window for app to reuse"""
        return self.root

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


class FirstRunWizard:
    """First-run setup wizard for new users"""

    def __init__(self, config_path):
        self.config_path = config_path
        self.config = {}
        self.result = None  # Will be set to config dict if completed
        self.current_step = 0
        self.steps = ["welcome", "microphone", "model", "hotkeys", "complete"]
        self.available_mics = []
        self.test_stream = None

    def get_available_microphones(self):
        """Get list of available microphone devices (filtered)"""
        devices = sd.query_devices()
        microphones = []
        seen_names = set()

        # Skip common virtual/loopback devices and unwanted system devices
        skip_keywords = [
            'Stereo Mix', 'Wave Out Mix', 'What U Hear', 'Loopback',
            'CABLE', 'Virtual Audio', 'VB-Audio', 'Voicemeeter',
            'Sound Mapper', 'Primary Sound', 'Wave Speaker', 'Wave Microphone',
            'Stream Wave', 'Chat Capture', 'Hands-Free', 'HF Audio', 'Input ()',
            'Line In (', 'VDVAD', 'SteelSeries Sonar', 'OCULUSVAD',
            'VAD Wave', 'wc4400_8200', 'Microsoft Sound Mapper'
        ]

        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                name = device['name']

                # Filter out duplicates
                if name in seen_names:
                    continue

                # Skip virtual/system devices
                if any(keyword.lower() in name.lower() for keyword in skip_keywords):
                    continue

                # Skip empty or placeholder names
                if name.strip() == "Microphone ()" or not name.strip():
                    continue

                # Skip driver paths
                if '@System32\\drivers\\' in name:
                    continue

                seen_names.add(name)
                microphones.append({'id': i, 'name': name})

        return microphones

    def run(self):
        """Run the wizard and return config dict or None if cancelled"""
        self.available_mics = self.get_available_microphones()

        self.root = tk.Tk()
        self.root.title("Samsara Setup")
        self.root.geometry("600x580")
        self.root.resizable(False, False)

        # Center on screen
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() // 2) - 300
        y = (self.root.winfo_screenheight() // 2) - 290
        self.root.geometry(f"+{x}+{y}")

        # Dark theme
        self.root.configure(bg='#2d2d2d')

        # Main container
        self.container = tk.Frame(self.root, bg='#2d2d2d', padx=30, pady=20)
        self.container.pack(fill='both', expand=True)

        # Navigation buttons - pack at bottom FIRST so they're always visible
        self.nav_frame = tk.Frame(self.container, bg='#2d2d2d')
        self.nav_frame.pack(side='bottom', fill='x', pady=(20, 0))

        self.back_btn = tk.Button(self.nav_frame, text="Back", command=self.prev_step,
                                   bg='#444', fg='white', padx=20, pady=5)
        self.back_btn.pack(side='left')

        self.next_btn = tk.Button(self.nav_frame, text="Next", command=self.next_step,
                                   bg='#0078d4', fg='white', padx=20, pady=5)
        self.next_btn.pack(side='right')

        # Content frame (changes per step) - fills remaining space
        self.content_frame = tk.Frame(self.container, bg='#2d2d2d')
        self.content_frame.pack(fill='both', expand=True)

        # Initialize config with defaults
        self.config = {
            "hotkey": "ctrl+shift",
            "continuous_hotkey": "ctrl+alt+d",
            "wake_word_hotkey": "ctrl+alt+w",
            "mode": "hold",
            "model_size": "base",
            "language": "en",
            "auto_paste": True,
            "add_trailing_space": True,
            "auto_capitalize": True,
            "format_numbers": True,
            "device": "auto",
            "microphone": None,
            "silence_threshold": 2.0,
            "min_speech_duration": 0.3,
            "command_mode_enabled": False,
            "wake_word": "hey samsara",
            "wake_word_timeout": 5.0,
            "show_all_audio_devices": False,
            "audio_feedback": True,
            "first_run_complete": True
        }

        # Variables for selections
        self.mic_var = tk.StringVar()
        self.model_var = tk.StringVar(value="base")

        # Hotkey variables
        self.hotkey_var = tk.StringVar(value="ctrl+shift")
        self.continuous_hotkey_var = tk.StringVar(value="ctrl+alt+d")
        self.wake_word_hotkey_var = tk.StringVar(value="ctrl+alt+w")
        self.capturing_hotkey = None  # Which hotkey is being captured
        self.captured_keys = set()

        # Wake word variable
        self.wake_word_var = tk.StringVar(value="hey samsara")

        self.show_step()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()

        return self.result

    def clear_content(self):
        """Clear the content frame"""
        for widget in self.content_frame.winfo_children():
            widget.destroy()

    def show_step(self):
        """Display the current step"""
        self.clear_content()
        step = self.steps[self.current_step]

        # Update navigation buttons
        self.back_btn.config(state='normal' if self.current_step > 0 else 'disabled')

        if step == "welcome":
            self.show_welcome()
        elif step == "microphone":
            self.show_microphone()
        elif step == "model":
            self.show_model()
        elif step == "hotkeys":
            self.show_hotkeys()
        elif step == "complete":
            self.show_complete()

    def show_welcome(self):
        """Welcome page"""
        tk.Label(self.content_frame, text="Welcome to Samsara",
                font=('Segoe UI', 18, 'bold'), bg='#2d2d2d', fg='#0078d4').pack(pady=(20, 10))

        tk.Label(self.content_frame, text="Voice Dictation for Everyone",
                font=('Segoe UI', 12), bg='#2d2d2d', fg='#aaaaaa').pack(pady=(0, 30))

        info_text = """This wizard will help you set up Samsara in just a few steps:

1. Select your microphone
2. Choose speech recognition quality
3. Learn the keyboard shortcuts

Setup takes about 1 minute."""

        tk.Label(self.content_frame, text=info_text, font=('Segoe UI', 10),
                bg='#2d2d2d', fg='white', justify='left').pack(pady=10)

    def show_microphone(self):
        """Microphone selection page"""
        tk.Label(self.content_frame, text="Select Your Microphone",
                font=('Segoe UI', 14, 'bold'), bg='#2d2d2d', fg='white').pack(pady=(10, 20))

        if not self.available_mics:
            tk.Label(self.content_frame, text="No microphones detected!",
                    font=('Segoe UI', 11), bg='#2d2d2d', fg='#ff6b6b').pack(pady=10)
            return

        # Set default selection
        if not self.mic_var.get() and self.available_mics:
            self.mic_var.set(self.available_mics[0]['name'])

        # Microphone dropdown
        mic_frame = tk.Frame(self.content_frame, bg='#2d2d2d')
        mic_frame.pack(pady=10, fill='x')

        tk.Label(mic_frame, text="Microphone:", font=('Segoe UI', 10),
                bg='#2d2d2d', fg='white').pack(anchor='w')

        mic_names = [m['name'] for m in self.available_mics]
        self.mic_combo = ttk.Combobox(mic_frame, textvariable=self.mic_var,
                                       values=mic_names, state='readonly', width=50)
        self.mic_combo.pack(fill='x', pady=5)

        # Test button
        test_frame = tk.Frame(self.content_frame, bg='#2d2d2d')
        test_frame.pack(pady=20)

        self.test_btn = tk.Button(test_frame, text="Test Microphone",
                                   command=self.test_microphone,
                                   bg='#444', fg='white', padx=15, pady=5)
        self.test_btn.pack()

        self.test_label = tk.Label(test_frame, text="", font=('Segoe UI', 9),
                                    bg='#2d2d2d', fg='#aaaaaa')
        self.test_label.pack(pady=10)

    def test_microphone(self):
        """Test the selected microphone"""
        mic_name = self.mic_var.get()
        mic_id = None
        for m in self.available_mics:
            if m['name'] == mic_name:
                mic_id = m['id']
                break

        if mic_id is None:
            self.test_label.config(text="Please select a microphone", fg='#ff6b6b')
            return

        self.test_btn.config(state='disabled', text="Listening...")
        self.test_label.config(text="Speak now...", fg='#00CED1')
        self.root.update()

        try:
            # Record for 2 seconds
            duration = 2
            audio = sd.rec(int(16000 * duration), samplerate=16000,
                          channels=1, dtype=np.float32, device=mic_id)
            sd.wait()

            # Check if we got audio
            rms = np.sqrt(np.mean(audio**2))
            if rms > 0.01:
                self.test_label.config(text="Microphone working! Audio detected.", fg='#00ff00')
            else:
                self.test_label.config(text="Very quiet - try speaking louder", fg='#ffaa00')

        except Exception as e:
            self.test_label.config(text=f"Error: {str(e)[:40]}", fg='#ff6b6b')

        self.test_btn.config(state='normal', text="Test Microphone")

    def show_model(self):
        """Model selection page"""
        tk.Label(self.content_frame, text="Choose Recognition Quality",
                font=('Segoe UI', 14, 'bold'), bg='#2d2d2d', fg='white').pack(pady=(10, 20))

        models = [
            ("tiny", "Fastest", "~75MB download, lowest accuracy"),
            ("base", "Balanced (Recommended)", "~150MB download, good accuracy"),
            ("small", "Best Quality", "~500MB download, highest accuracy"),
        ]

        for value, title, desc in models:
            frame = tk.Frame(self.content_frame, bg='#2d2d2d')
            frame.pack(fill='x', pady=5)

            rb = tk.Radiobutton(frame, text=title, variable=self.model_var,
                               value=value, font=('Segoe UI', 11),
                               bg='#2d2d2d', fg='white', selectcolor='#444',
                               activebackground='#2d2d2d', activeforeground='white')
            rb.pack(anchor='w')

            tk.Label(frame, text=desc, font=('Segoe UI', 9),
                    bg='#2d2d2d', fg='#888888').pack(anchor='w', padx=25)

        tk.Label(self.content_frame,
                text="The model downloads on first use.\nYou can change this later in Settings.",
                font=('Segoe UI', 9), bg='#2d2d2d', fg='#666666').pack(pady=(30, 0))

    def show_hotkeys(self):
        """Hotkeys configuration page"""
        tk.Label(self.content_frame, text="Shortcuts & Wake Word",
                font=('Segoe UI', 14, 'bold'), bg='#2d2d2d', fg='white').pack(pady=(5, 10))

        tk.Label(self.content_frame, text="Click a button and press your desired key combination",
                font=('Segoe UI', 9), bg='#2d2d2d', fg='#888888').pack(pady=(0, 10))

        hotkeys = [
            ("hotkey", self.hotkey_var, "Hold to Record", "Hold keys to record, release to transcribe"),
            ("continuous_hotkey", self.continuous_hotkey_var, "Continuous Mode", "Toggle always-on listening"),
            ("wake_word_hotkey", self.wake_word_hotkey_var, "Wake Word Mode", "Toggle wake word activation"),
        ]

        self.hotkey_buttons = {}

        for key_name, var, label, desc in hotkeys:
            frame = tk.Frame(self.content_frame, bg='#2d2d2d')
            frame.pack(fill='x', pady=5)

            left_frame = tk.Frame(frame, bg='#2d2d2d')
            left_frame.pack(side='left', fill='x', expand=True)

            tk.Label(left_frame, text=label, font=('Segoe UI', 10, 'bold'),
                    bg='#2d2d2d', fg='white').pack(anchor='w')
            tk.Label(left_frame, text=desc, font=('Segoe UI', 8),
                    bg='#2d2d2d', fg='#888888').pack(anchor='w')

            btn = tk.Button(frame, textvariable=var, width=18,
                           font=('Consolas', 9), bg='#444', fg='#00CED1',
                           command=lambda k=key_name: self.start_hotkey_capture(k))
            btn.pack(side='right', padx=5)
            self.hotkey_buttons[key_name] = btn

        # Wake word section
        tk.Frame(self.content_frame, bg='#444', height=1).pack(fill='x', pady=(15, 10))

        wake_frame = tk.Frame(self.content_frame, bg='#2d2d2d')
        wake_frame.pack(fill='x', pady=5)

        left_frame = tk.Frame(wake_frame, bg='#2d2d2d')
        left_frame.pack(side='left', fill='x', expand=True)

        tk.Label(left_frame, text="Wake Word Phrase", font=('Segoe UI', 10, 'bold'),
                bg='#2d2d2d', fg='white').pack(anchor='w')
        tk.Label(left_frame, text="Say this to activate voice commands",
                font=('Segoe UI', 8), bg='#2d2d2d', fg='#888888').pack(anchor='w')

        # Dropdown with preset options
        wake_words = ["hey samsara", "ok samsara", "hey computer", "listen up", "voice command"]
        self.wake_word_combo = ttk.Combobox(wake_frame, textvariable=self.wake_word_var,
                                            values=wake_words, width=16, font=('Segoe UI', 9))
        self.wake_word_combo.pack(side='right', padx=5)

        tk.Label(self.content_frame,
                text="You can also type a custom wake word above.",
                font=('Segoe UI', 9), bg='#2d2d2d', fg='#666666').pack(pady=(10, 0))

    def start_hotkey_capture(self, key_name):
        """Start capturing a hotkey"""
        self.capturing_hotkey = key_name
        self.captured_keys = set()
        self.current_pressed = set()  # Track currently held keys
        self.finalize_timer = None

        # Update button to show capturing state
        btn = self.hotkey_buttons[key_name]
        btn.config(text="Press keys...", bg='#0078d4')

        # Bind key events to the root window
        self.root.bind('<KeyPress>', self.on_hotkey_press)
        self.root.bind('<KeyRelease>', self.on_hotkey_release)
        self.root.focus_set()

    def on_hotkey_press(self, event):
        """Handle key press during hotkey capture"""
        if not self.capturing_hotkey:
            return

        # Cancel any pending finalization
        if self.finalize_timer:
            self.root.after_cancel(self.finalize_timer)
            self.finalize_timer = None

        key = self.normalize_key(event)
        if key:
            self.captured_keys.add(key)
            self.current_pressed.add(key)
            # Update button to show current combination
            self.update_hotkey_display()

    def on_hotkey_release(self, event):
        """Handle key release during hotkey capture"""
        if not self.capturing_hotkey:
            return

        key = self.normalize_key(event)
        if key:
            self.current_pressed.discard(key)

        # When all keys are released, start a timer to finalize
        if not self.current_pressed and self.captured_keys:
            self.finalize_timer = self.root.after(300, self.finalize_hotkey)

    def update_hotkey_display(self):
        """Update the button to show current key combination"""
        hotkey = self.build_hotkey_string()
        if hotkey and self.capturing_hotkey:
            btn = self.hotkey_buttons[self.capturing_hotkey]
            btn.config(text=hotkey)

    def finalize_hotkey(self):
        """Finalize the captured hotkey"""
        if not self.capturing_hotkey:
            return

        hotkey = self.build_hotkey_string()
        if hotkey:
            # Update the appropriate variable
            if self.capturing_hotkey == "hotkey":
                self.hotkey_var.set(hotkey)
            elif self.capturing_hotkey == "continuous_hotkey":
                self.continuous_hotkey_var.set(hotkey)
            elif self.capturing_hotkey == "wake_word_hotkey":
                self.wake_word_hotkey_var.set(hotkey)

        # Reset button appearance
        btn = self.hotkey_buttons[self.capturing_hotkey]
        btn.config(bg='#444')

        # Unbind and reset
        self.root.unbind('<KeyPress>')
        self.root.unbind('<KeyRelease>')
        self.capturing_hotkey = None
        self.captured_keys = set()
        self.current_pressed = set()
        self.finalize_timer = None

    def normalize_key(self, event):
        """Normalize a key event to a standard string"""
        key = event.keysym.lower()

        # Map common key names
        key_map = {
            'control_l': 'ctrl', 'control_r': 'ctrl',
            'alt_l': 'alt', 'alt_r': 'alt',
            'shift_l': 'shift', 'shift_r': 'shift',
            'super_l': 'win', 'super_r': 'win',
            'escape': 'esc', 'return': 'enter',
            'space': 'space', 'tab': 'tab',
            'backspace': 'backspace', 'delete': 'delete',
        }

        return key_map.get(key, key)

    def build_hotkey_string(self):
        """Build a hotkey string from captured keys"""
        if not self.captured_keys:
            return None

        # Define modifier order
        modifiers = ['ctrl', 'alt', 'shift', 'win']
        parts = []

        # Add modifiers in order
        for mod in modifiers:
            if mod in self.captured_keys:
                parts.append(mod)

        # Add non-modifier keys
        for key in sorted(self.captured_keys):
            if key not in modifiers:
                parts.append(key)

        return '+'.join(parts) if parts else None

    def show_complete(self):
        """Setup complete page"""
        self.next_btn.config(text="Start Samsara")

        tk.Label(self.content_frame, text="Setup Complete!",
                font=('Segoe UI', 16, 'bold'), bg='#2d2d2d', fg='#00ff00').pack(pady=(10, 10))

        # Map model names to friendly descriptions
        model_names = {
            'tiny': 'Fastest (tiny)',
            'base': 'Balanced (base)',
            'small': 'Best Quality (small)'
        }
        model_display = model_names.get(self.model_var.get(), self.model_var.get())

        # Settings summary in a compact format
        settings_frame = tk.Frame(self.content_frame, bg='#3d3d3d', padx=15, pady=10)
        settings_frame.pack(fill='x', pady=10)

        tk.Label(settings_frame, text=f"Microphone: {self.mic_var.get() or 'Default'}",
                font=('Segoe UI', 9), bg='#3d3d3d', fg='white', anchor='w').pack(fill='x')
        tk.Label(settings_frame, text=f"Model: {model_display}",
                font=('Segoe UI', 9), bg='#3d3d3d', fg='white', anchor='w').pack(fill='x')
        tk.Label(settings_frame, text=f"Record: {self.hotkey_var.get()} (hold)",
                font=('Segoe UI', 9), bg='#3d3d3d', fg='#00CED1', anchor='w').pack(fill='x')
        tk.Label(settings_frame, text=f"Continuous: {self.continuous_hotkey_var.get()}",
                font=('Segoe UI', 9), bg='#3d3d3d', fg='#00CED1', anchor='w').pack(fill='x')
        tk.Label(settings_frame, text=f"Wake Word Key: {self.wake_word_hotkey_var.get()}",
                font=('Segoe UI', 9), bg='#3d3d3d', fg='#00CED1', anchor='w').pack(fill='x')
        tk.Label(settings_frame, text=f"Wake Phrase: \"{self.wake_word_var.get()}\"",
                font=('Segoe UI', 9), bg='#3d3d3d', fg='#00CED1', anchor='w').pack(fill='x')

        tk.Label(self.content_frame, text="Model downloads on first use. Look for the tray icon.",
                font=('Segoe UI', 9), bg='#2d2d2d', fg='#666666').pack(pady=(15, 0))

    def next_step(self):
        """Go to next step"""
        if self.current_step == len(self.steps) - 1:
            # Final step - save and exit
            self.save_and_close()
            return

        # Save current step selections
        if self.steps[self.current_step] == "microphone":
            mic_name = self.mic_var.get()
            for m in self.available_mics:
                if m['name'] == mic_name:
                    self.config['microphone'] = m['id']
                    break

        elif self.steps[self.current_step] == "model":
            self.config['model_size'] = self.model_var.get()

        elif self.steps[self.current_step] == "hotkeys":
            self.config['hotkey'] = self.hotkey_var.get()
            self.config['continuous_hotkey'] = self.continuous_hotkey_var.get()
            self.config['wake_word_hotkey'] = self.wake_word_hotkey_var.get()
            self.config['wake_word'] = self.wake_word_var.get()

        self.current_step += 1
        self.show_step()

    def prev_step(self):
        """Go to previous step"""
        if self.current_step > 0:
            self.current_step -= 1
            # Reset next button text if going back from complete
            if self.steps[self.current_step] != "complete":
                self.next_btn.config(text="Next")
            self.show_step()

    def save_and_close(self):
        """Save config and close wizard"""
        # Save config to file
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self.config, f, indent=2)
            self.result = self.config
        except Exception as e:
            print(f"Error saving config: {e}")

        self.root.destroy()

    def on_close(self):
        """Handle window close - use defaults"""
        self.config['first_run_complete'] = True
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self.config, f, indent=2)
        except:
            pass
        self.result = self.config
        self.root.destroy()


# ============================================================================
# Clipboard Save/Restore - preserves all clipboard formats via Windows API
# ============================================================================

# Lock to prevent concurrent clipboard operations (race condition in continuous mode)
_clipboard_lock = threading.Lock()

def _save_clipboard_win32():
    """Save all clipboard formats using Windows API. Returns dict of format->bytes."""
    if sys.platform != 'win32':
        try:
            return {'text': pyperclip.paste()}
        except Exception:
            return {}

    saved = {}
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Retry logic - clipboard may be locked by another application
    max_retries = 10
    retry_delay = 0.03  # Start with 30ms
    clipboard_opened = False
    for attempt in range(max_retries):
        if user32.OpenClipboard(0):
            clipboard_opened = True
            break
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 1.5, 0.1)  # Cap at 100ms

    if not clipboard_opened:
        print("[WARN] Could not open clipboard for save after retries - clipboard content will be lost")
        return saved

    try:
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            try:
                handle = user32.GetClipboardData(fmt)
                if not handle:
                    continue
                size = kernel32.GlobalSize(handle)
                if size <= 0:
                    continue
                ptr = kernel32.GlobalLock(handle)
                if ptr:
                    raw = ctypes.string_at(ptr, size)
                    saved[fmt] = raw
                    kernel32.GlobalUnlock(handle)
            except Exception:
                pass
    finally:
        user32.CloseClipboard()

    if saved:
        print(f"[DEBUG] Clipboard saved: {len(saved)} format(s)")
    else:
        print("[DEBUG] Clipboard was empty, nothing to preserve")

    return saved


def _restore_clipboard_win32(saved):
    """Restore clipboard formats saved by _save_clipboard_win32."""
    if not saved:
        print("[DEBUG] No clipboard content to restore (was empty)")
        return

    if sys.platform != 'win32':
        text = saved.get('text')
        if text:
            try:
                pyperclip.copy(text)
                print("[DEBUG] Clipboard restored (non-Windows)")
            except Exception:
                pass
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    GMEM_MOVEABLE = 0x0002

    # Retry logic - clipboard may be locked by paste target
    max_retries = 10
    retry_delay = 0.03  # Start with 30ms
    for attempt in range(max_retries):
        if user32.OpenClipboard(0):
            break
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 1.5, 0.1)  # Cap at 100ms
    else:
        print("[WARN] Could not open clipboard for restore after retries - original content lost")
        return

    restored_count = 0
    try:
        user32.EmptyClipboard()
        for fmt, raw in saved.items():
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
            if not h:
                continue
            ptr = kernel32.GlobalLock(h)
            if ptr:
                ctypes.memmove(ptr, raw, len(raw))
                kernel32.GlobalUnlock(h)
                user32.SetClipboardData(fmt, h)
                restored_count += 1
            else:
                kernel32.GlobalFree(h)
    finally:
        user32.CloseClipboard()

    print(f"[DEBUG] Clipboard restored: {restored_count}/{len(saved)} format(s)")


class CommandExecutor:
    """Executes voice commands - hotkeys, launches, key holds, etc."""
    
    def __init__(self, commands_path):
        self.commands_path = commands_path
        self.commands = {}
        self.held_keys = {}  # Track currently held keys
        self.keyboard_controller = KeyboardController()
        self.mouse_controller = MouseController()
        self.load_commands()
        
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
            success = self.execute_command(command)
            return command, success
        
        # Not a command, return text for dictation
        return text, False

class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.window = None
        self.capturing_hotkey = None
        self.captured_keys = set()
        self.available_mics = []

    def show(self):
        if self.window is not None:
            try:
                self.window.lift()
                self.window.focus_force()
                return
            except:
                self.window = None

        # Get available microphones
        self.available_mics = self.app.get_available_microphones()

        # Set CustomTkinter appearance based on system
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        # Create modern CTk window
        self.window = ctk.CTkToplevel(self.app.root)
        self.window.title("Samsara Settings")
        self.window.geometry("700x700")
        self.window.resizable(True, True)
        self.window.minsize(650, 600)

        # Hide window while building UI to prevent incremental rendering
        self.window.withdraw()

        # Use grid layout for reliable button placement
        self.window.grid_rowconfigure(0, weight=1)
        self.window.grid_columnconfigure(0, weight=1)

        # Create tabview (modern tabs)
        self.tabview = ctk.CTkTabview(self.window, corner_radius=10)
        self.tabview.grid(row=0, column=0, sticky='nsew', padx=20, pady=(20, 10))

        # Bottom buttons frame
        btn_frame = ctk.CTkFrame(self.window, fg_color="transparent", height=60)
        btn_frame.grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 20))
        btn_frame.grid_propagate(False)

        # Buttons inside the frame
        self.apply_btn = ctk.CTkButton(btn_frame, text="Apply & Close", width=140, height=40,
                                       command=self.save_and_close)
        self.apply_btn.pack(side='right', padx=(10, 0), pady=10)

        self.cancel_btn = ctk.CTkButton(btn_frame, text="Cancel", width=100, height=40,
                                        fg_color="gray40", hover_color="gray30",
                                        command=self.close)
        self.cancel_btn.pack(side='right', pady=10)

        # Add tabs
        self.tabview.add("General")
        self.tabview.add("Hotkeys & Modes")
        self.tabview.add("Commands")
        self.tabview.add("Sounds")
        self.tabview.add("Advanced")

        # === GENERAL TAB ===
        general_tab = self.tabview.tab("General")
        
        # Create scrollable frame for General tab content
        general_scroll = ctk.CTkScrollableFrame(general_tab, fg_color="transparent")
        general_scroll.pack(fill='both', expand=True)

        # Microphone Section
        mic_label = ctk.CTkLabel(general_scroll, text="Microphone", font=ctk.CTkFont(size=16, weight="bold"))
        mic_label.pack(anchor='w', pady=(15, 10))

        mic_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        mic_frame.pack(fill='x', pady=(0, 20))

        ctk.CTkLabel(mic_frame, text="Selected device:").pack(anchor='w', padx=15, pady=(15, 5))

        mic_names = [mic['name'] for mic in self.available_mics]
        current_mic_id = self.app.config.get('microphone')
        current_selection = mic_names[0] if mic_names else "No microphones found"

        if current_mic_id is not None:
            for mic in self.available_mics:
                if mic['id'] == current_mic_id:
                    current_selection = mic['name']
                    break

        self.mic_var = tk.StringVar(value=current_selection)
        self.mic_combo = ctk.CTkComboBox(mic_frame, variable=self.mic_var, values=mic_names,
                                         width=400, state='readonly')
        self.mic_combo.pack(anchor='w', padx=15, pady=(0, 10))

        self.show_all_devices_var = tk.BooleanVar(value=self.app.config.get('show_all_audio_devices', False))
        ctk.CTkCheckBox(mic_frame, text="Show all audio devices (includes virtual/system devices)",
                       variable=self.show_all_devices_var, command=self.refresh_microphone_list).pack(anchor='w', padx=15, pady=(0, 15))

        # Basic Options Section
        options_label = ctk.CTkLabel(general_scroll, text="Basic Options", font=ctk.CTkFont(size=16, weight="bold"))
        options_label.pack(anchor='w', pady=(0, 10))

        options_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        options_frame.pack(fill='x', pady=(0, 20))

        self.auto_paste_var = tk.BooleanVar(value=self.app.config.get('auto_paste', True))
        ctk.CTkCheckBox(options_frame, text="Automatically paste transcribed text",
                       variable=self.auto_paste_var).pack(anchor='w', padx=15, pady=(15, 8))

        self.trailing_space_var = tk.BooleanVar(value=self.app.config.get('add_trailing_space', True))
        ctk.CTkCheckBox(options_frame, text="Add trailing space after text",
                       variable=self.trailing_space_var).pack(anchor='w', padx=15, pady=(0, 8))

        self.auto_capitalize_var = tk.BooleanVar(value=self.app.config.get('auto_capitalize', True))
        ctk.CTkCheckBox(options_frame, text="Auto-capitalize sentences",
                       variable=self.auto_capitalize_var).pack(anchor='w', padx=15, pady=(0, 8))

        self.format_numbers_var = tk.BooleanVar(value=self.app.config.get('format_numbers', True))
        ctk.CTkCheckBox(options_frame, text="Convert spoken numbers to digits",
                       variable=self.format_numbers_var).pack(anchor='w', padx=15, pady=(0, 8))

        self.command_mode_var = tk.BooleanVar(value=self.app.config.get('command_mode_enabled', True))
        ctk.CTkCheckBox(options_frame, text="Enable voice commands (recommended)",
                       variable=self.command_mode_var).pack(anchor='w', padx=15, pady=(0, 8))

        # Auto-start option
        self.auto_start_var = tk.BooleanVar(value=self.check_auto_start())
        ctk.CTkCheckBox(options_frame, text="Start Samsara with Windows",
                       variable=self.auto_start_var,
                       command=self.toggle_auto_start).pack(anchor='w', padx=15, pady=(0, 15))

        # Profiles Section
        profiles_label = ctk.CTkLabel(general_scroll, text="Profiles", font=ctk.CTkFont(size=16, weight="bold"))
        profiles_label.pack(anchor='w', pady=(0, 10))

        profiles_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        profiles_frame.pack(fill='x', pady=(0, 20))

        profiles_desc = ctk.CTkLabel(profiles_frame, 
                                     text="Save and load vocabulary and command configurations",
                                     text_color="gray")
        profiles_desc.pack(anchor='w', padx=15, pady=(15, 10))

        ctk.CTkButton(profiles_frame, text="Manage Profiles...", width=160,
                     command=self.open_profile_manager).pack(anchor='w', padx=15, pady=(0, 15))

        # Voice Training Section
        training_label = ctk.CTkLabel(general_scroll, text="Voice Training", font=ctk.CTkFont(size=16, weight="bold"))
        training_label.pack(anchor='w', pady=(0, 10))

        training_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        training_frame.pack(fill='x', pady=(0, 20))

        training_desc = ctk.CTkLabel(training_frame, 
                                     text="Customize vocabulary, corrections, and microphone calibration",
                                     text_color="gray")
        training_desc.pack(anchor='w', padx=15, pady=(15, 10))

        ctk.CTkButton(training_frame, text="Open Voice Training...", width=180,
                     command=self.open_voice_training).pack(anchor='w', padx=15, pady=(0, 15))

        # Model Section
        model_label = ctk.CTkLabel(general_scroll, text="AI Model", font=ctk.CTkFont(size=16, weight="bold"))
        model_label.pack(anchor='w', pady=(0, 10))

        model_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        model_frame.pack(fill='x')

        ctk.CTkLabel(model_frame, text="Whisper model size:").pack(anchor='w', padx=15, pady=(15, 5))

        # Model options with disk space info
        model_options = [
            'tiny (~75 MB)',
            'base (~150 MB)',
            'small (~500 MB)',
            'medium (~1.5 GB)',
            'large-v3 (~3 GB)'
        ]
        # Map display names to actual values
        self.model_display_to_value = {
            'tiny (~75 MB)': 'tiny',
            'base (~150 MB)': 'base',
            'small (~500 MB)': 'small',
            'medium (~1.5 GB)': 'medium',
            'large-v3 (~3 GB)': 'large-v3'
        }
        self.model_value_to_display = {v: k for k, v in self.model_display_to_value.items()}
        
        current_model = self.app.config.get('model_size', 'base')
        current_display = self.model_value_to_display.get(current_model, 'base (~150 MB)')
        
        self.model_var = tk.StringVar(value=current_display)
        model_combo = ctk.CTkComboBox(model_frame, variable=self.model_var,
                                      values=model_options,
                                      width=200, state='readonly')
        model_combo.pack(anchor='w', padx=15, pady=(0, 5))

        ctk.CTkLabel(model_frame, text="tiny: Fastest  |  base: Recommended  |  large-v3: Most accurate",
                    text_color="gray").pack(anchor='w', padx=15, pady=(0, 5))
        ctk.CTkLabel(model_frame, text="Model changes require restart",
                    text_color="#1f6aa5").pack(anchor='w', padx=15, pady=(0, 15))

        # === HOTKEYS & MODES TAB ===
        hotkey_tab = self.tabview.tab("Hotkeys & Modes")
        
        # Create scrollable frame for Hotkeys tab content
        hotkey_scroll = ctk.CTkScrollableFrame(hotkey_tab, fg_color="transparent")
        hotkey_scroll.pack(fill='both', expand=True)

        # Recording Mode Section
        mode_label = ctk.CTkLabel(hotkey_scroll, text="Recording Mode", font=ctk.CTkFont(size=16, weight="bold"))
        mode_label.pack(anchor='w', pady=(15, 10))

        mode_frame = ctk.CTkFrame(hotkey_scroll, corner_radius=10)
        mode_frame.pack(fill='x', pady=(0, 20))

        # Determine current runtime mode (not just config default)
        # Runtime state overrides config if a mode is actively toggled
        if self.app.wake_word_active:
            current_mode = 'wake_word'
        elif self.app.continuous_active:
            current_mode = 'continuous'
        else:
            current_mode = self.app.config.get('mode', 'hold')
        
        self.mode_var = tk.StringVar(value=current_mode)

        ctk.CTkRadioButton(mode_frame, text="Hold to record (hold key, release to transcribe)",
                          variable=self.mode_var, value='hold').pack(anchor='w', padx=15, pady=(15, 8))
        ctk.CTkRadioButton(mode_frame, text="Toggle mode (press to start/stop recording)",
                          variable=self.mode_var, value='toggle').pack(anchor='w', padx=15, pady=(0, 8))
        ctk.CTkRadioButton(mode_frame, text="Continuous (auto-transcribe on speech pause)",
                          variable=self.mode_var, value='continuous').pack(anchor='w', padx=15, pady=(0, 8))
        ctk.CTkRadioButton(mode_frame, text="Wake word (hands-free activation)",
                          variable=self.mode_var, value='wake_word').pack(anchor='w', padx=15, pady=(0, 15))

        # Keyboard Shortcuts Section
        hotkey_label = ctk.CTkLabel(hotkey_scroll, text="Keyboard Shortcuts", font=ctk.CTkFont(size=16, weight="bold"))
        hotkey_label.pack(anchor='w', pady=(0, 10))

        hotkey_frame = ctk.CTkFrame(hotkey_scroll, corner_radius=10)
        hotkey_frame.pack(fill='x')

        # Hotkey rows
        self.hotkey_buttons = {}

        # Record hotkey
        row1 = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row1.pack(fill='x', padx=15, pady=(15, 8))
        ctk.CTkLabel(row1, text="Record hotkey:", width=150, anchor='w').pack(side='left')
        self.hotkey_var = tk.StringVar(value=self.app.config.get('hotkey', 'ctrl+shift'))
        self.hotkey_entry = ctk.CTkEntry(row1, textvariable=self.hotkey_var, width=180, state='disabled')
        self.hotkey_entry.pack(side='left', padx=(0, 10))
        self.hotkey_btn = ctk.CTkButton(row1, text="Change", width=80,
                                        command=lambda: self.start_capture('hotkey'))
        self.hotkey_btn.pack(side='left')
        self.hotkey_buttons['hotkey'] = self.hotkey_btn

        # Continuous hotkey
        row2 = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row2.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(row2, text="Toggle continuous:", width=150, anchor='w').pack(side='left')
        self.cont_hotkey_var = tk.StringVar(value=self.app.config.get('continuous_hotkey', 'ctrl+alt+d'))
        self.cont_hotkey_entry = ctk.CTkEntry(row2, textvariable=self.cont_hotkey_var, width=180, state='disabled')
        self.cont_hotkey_entry.pack(side='left', padx=(0, 10))
        self.cont_hotkey_btn = ctk.CTkButton(row2, text="Change", width=80,
                                             command=lambda: self.start_capture('continuous_hotkey'))
        self.cont_hotkey_btn.pack(side='left')
        self.hotkey_buttons['continuous_hotkey'] = self.cont_hotkey_btn

        # Wake word hotkey
        row3 = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row3.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(row3, text="Toggle wake word:", width=150, anchor='w').pack(side='left')
        self.wake_hotkey_var = tk.StringVar(value=self.app.config.get('wake_word_hotkey', 'ctrl+alt+w'))
        self.wake_hotkey_entry = ctk.CTkEntry(row3, textvariable=self.wake_hotkey_var, width=180, state='disabled')
        self.wake_hotkey_entry.pack(side='left', padx=(0, 10))
        self.wake_hotkey_btn = ctk.CTkButton(row3, text="Change", width=80,
                                             command=lambda: self.start_capture('wake_word_hotkey'))
        self.wake_hotkey_btn.pack(side='left')
        self.hotkey_buttons['wake_word_hotkey'] = self.wake_hotkey_btn

        # Cancel recording hotkey
        row4 = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row4.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(row4, text="Cancel recording:", width=150, anchor='w').pack(side='left')
        self.cancel_hotkey_var = tk.StringVar(value=self.app.config.get('cancel_hotkey', 'escape'))
        self.cancel_hotkey_entry = ctk.CTkEntry(row4, textvariable=self.cancel_hotkey_var, width=180, state='disabled')
        self.cancel_hotkey_entry.pack(side='left', padx=(0, 10))
        self.cancel_hotkey_btn = ctk.CTkButton(row4, text="Change", width=80,
                                             command=lambda: self.start_capture('cancel_hotkey'))
        self.cancel_hotkey_btn.pack(side='left')
        self.hotkey_buttons['cancel_hotkey'] = self.cancel_hotkey_btn

        # === COMMANDS TAB ===
        commands_tab = self.tabview.tab("Commands")

        # Header
        cmd_header = ctk.CTkFrame(commands_tab, fg_color="transparent")
        cmd_header.pack(fill='x', pady=(15, 10))

        ctk.CTkLabel(cmd_header, text="Voice Commands",
                    font=ctk.CTkFont(size=16, weight="bold")).pack(side='left')

        # Search box
        self.cmd_search_var = tk.StringVar()
        self.cmd_search_var.trace('w', lambda *args: self.filter_commands())
        search_entry = ctk.CTkEntry(cmd_header, textvariable=self.cmd_search_var,
                                   placeholder_text="Search commands...", width=200)
        search_entry.pack(side='right')

        # Command list frame
        list_frame = ctk.CTkFrame(commands_tab, corner_radius=10)
        list_frame.pack(fill='both', expand=True, pady=(0, 10))

        # Treeview for commands (using ttk as CTk doesn't have treeview)
        tree_container = ctk.CTkFrame(list_frame, fg_color="transparent")
        tree_container.pack(fill='both', expand=True, padx=10, pady=10)

        # Scrollbar
        tree_scroll = ttk.Scrollbar(tree_container)
        tree_scroll.pack(side='right', fill='y')

        # Style the treeview for dark mode
        style = ttk.Style()
        # Use 'clam' theme which allows heading customization (Windows default ignores it)
        style.theme_use('clam')
        style.configure("Commands.Treeview",
                       background="#2b2b2b",
                       foreground="white",
                       fieldbackground="#2b2b2b",
                       rowheight=28)
        style.configure("Commands.Treeview.Heading",
                       background="#1f6aa5",
                       foreground="white",
                       font=('Segoe UI', 10, 'bold'),
                       relief='flat')
        style.map("Commands.Treeview.Heading",
                 background=[('active', '#2980b9')])
        style.map("Commands.Treeview", background=[('selected', '#1f6aa5')])

        self.cmd_tree = ttk.Treeview(tree_container, columns=('phrase', 'type', 'action', 'description'),
                                     show='headings', yscrollcommand=tree_scroll.set,
                                     style="Commands.Treeview", height=12)
        self.cmd_tree.pack(side='left', fill='both', expand=True)
        tree_scroll.config(command=self.cmd_tree.yview)

        # Column headings
        self.cmd_tree.heading('phrase', text='Voice Phrase')
        self.cmd_tree.heading('type', text='Type')
        self.cmd_tree.heading('action', text='Action')
        self.cmd_tree.heading('description', text='Description')

        # Column widths
        self.cmd_tree.column('phrase', width=140, minwidth=100)
        self.cmd_tree.column('type', width=70, minwidth=60)
        self.cmd_tree.column('action', width=150, minwidth=100)
        self.cmd_tree.column('description', width=180, minwidth=100)

        # Populate commands
        self.populate_commands_list()

        # Button frame
        cmd_btn_frame = ctk.CTkFrame(commands_tab, fg_color="transparent")
        cmd_btn_frame.pack(fill='x', pady=(0, 5))

        ctk.CTkButton(cmd_btn_frame, text="Add Command", width=120,
                     command=self.add_command_dialog).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_frame, text="Edit", width=80,
                     command=self.edit_command_dialog).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_frame, text="Delete", width=80, fg_color="#cc4444", hover_color="#aa3333",
                     command=self.delete_command).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_frame, text="Test", width=80, fg_color="gray40",
                     command=self.test_command).pack(side='left', padx=(0, 5))

        # Reload button on right
        ctk.CTkButton(cmd_btn_frame, text="Reload", width=80, fg_color="gray40",
                     command=self.reload_commands).pack(side='right')

        # Info text
        ctk.CTkLabel(commands_tab,
                    text="Say these phrases while dictating to trigger actions. Commands work in all modes.",
                    text_color="gray").pack(anchor='w')

        # === SOUNDS TAB ===
        sounds_tab = self.tabview.tab("Sounds")
        
        # Create scrollable frame for Sounds tab content
        sounds_scroll = ctk.CTkScrollableFrame(sounds_tab, fg_color="transparent")
        sounds_scroll.pack(fill='both', expand=True)

        # Audio Feedback Toggle
        feedback_label = ctk.CTkLabel(sounds_scroll, text="Audio Feedback", font=ctk.CTkFont(size=16, weight="bold"))
        feedback_label.pack(anchor='w', pady=(15, 10))

        feedback_frame = ctk.CTkFrame(sounds_scroll, corner_radius=10)
        feedback_frame.pack(fill='x', pady=(0, 20))

        self.audio_feedback_var = tk.BooleanVar(value=self.app.config.get('audio_feedback', True))
        ctk.CTkCheckBox(feedback_frame, text="Enable audio feedback sounds",
                       variable=self.audio_feedback_var).pack(anchor='w', padx=15, pady=(15, 10))

        # Volume slider row
        volume_row = ctk.CTkFrame(feedback_frame, fg_color="transparent")
        volume_row.pack(fill='x', padx=15, pady=(0, 15))

        ctk.CTkLabel(volume_row, text="Volume:", width=80, anchor='w').pack(side='left')

        self.sound_volume_var = tk.DoubleVar(value=self.app.config.get('sound_volume', 0.5))

        self.volume_slider = ctk.CTkSlider(volume_row, from_=0.0, to=1.0,
                                           variable=self.sound_volume_var, width=200,
                                           command=self.on_volume_change)
        self.volume_slider.pack(side='left', padx=(0, 10))

        self.volume_label = ctk.CTkLabel(volume_row, text=f"{int(self.sound_volume_var.get() * 100)}%", width=50)
        self.volume_label.pack(side='left')

        # Test volume button
        ctk.CTkButton(volume_row, text="Test", width=60,
                     command=lambda: self.app.play_sound('success')).pack(side='left', padx=(10, 0))

        # Sound Theme Section
        theme_label = ctk.CTkLabel(sounds_scroll, text="Sound Theme", font=ctk.CTkFont(size=16, weight="bold"))
        theme_label.pack(anchor='w', pady=(0, 10))

        theme_frame = ctk.CTkFrame(sounds_scroll, corner_radius=10)
        theme_frame.pack(fill='x', pady=(0, 20))

        theme_row = ctk.CTkFrame(theme_frame, fg_color="transparent")
        theme_row.pack(fill='x', padx=15, pady=15)

        ctk.CTkLabel(theme_row, text="Theme:", width=80, anchor='w').pack(side='left')

        # Get available themes
        themes_dir = Path(__file__).parent / 'sounds' / 'themes'
        available_themes = ['cute', 'warm', 'zen', 'classic']
        if themes_dir.exists():
            available_themes = [d.name for d in themes_dir.iterdir() if d.is_dir() and (d / 'start.wav').exists()]

        self.sound_theme_var = tk.StringVar(value=self.app.config.get('sound_theme', 'cute'))
        self.theme_combo = ctk.CTkComboBox(theme_row, variable=self.sound_theme_var,
                                           values=available_themes, width=150, state='readonly')
        self.theme_combo.pack(side='left', padx=(0, 10))

        ctk.CTkButton(theme_row, text="Apply Theme", width=100,
                     command=self.apply_sound_theme).pack(side='left', padx=(0, 10))

        # Theme descriptions
        theme_desc = ctk.CTkLabel(theme_frame, text="cute = playful bloops  •  warm = OS boot vibes  •  zen = singing bowls  •  classic = original",
                                  text_color="gray", font=ctk.CTkFont(size=11))
        theme_desc.pack(anchor='w', padx=15, pady=(0, 15))

        # Custom Sounds Section
        sounds_label = ctk.CTkLabel(sounds_scroll, text="Custom Sound Files", font=ctk.CTkFont(size=16, weight="bold"))
        sounds_label.pack(anchor='w', pady=(0, 10))

        ctk.CTkLabel(sounds_scroll, text="Replace default sounds with your own WAV files:",
                    text_color="gray").pack(anchor='w', pady=(0, 10))

        sounds_frame = ctk.CTkFrame(sounds_scroll, corner_radius=10)
        sounds_frame.pack(fill='x', pady=(0, 20))

        # Sound file rows
        self.sound_labels = {}
        sound_types = [
            ('start', 'Recording start:'),
            ('stop', 'Recording stop:'),
            ('success', 'Transcription success:'),
            ('error', 'Error sound:')
        ]

        for sound_type, label_text in sound_types:
            row = ctk.CTkFrame(sounds_frame, fg_color="transparent")
            row.pack(fill='x', padx=15, pady=(10, 5) if sound_type == 'start' else (5, 5))

            ctk.CTkLabel(row, text=label_text, width=140, anchor='w').pack(side='left')

            # Current file label
            sound_file = self.app.sound_files.get(sound_type)
            filename = sound_file.name if sound_file and sound_file.exists() else "Not set"
            file_label = ctk.CTkLabel(row, text=filename, width=150, anchor='w', text_color="gray")
            file_label.pack(side='left', padx=(0, 10))
            self.sound_labels[sound_type] = file_label

            # Preview button
            preview_btn = ctk.CTkButton(row, text="Play", width=60,
                                        command=lambda st=sound_type: self.preview_sound(st))
            preview_btn.pack(side='left', padx=(0, 5))

            # Browse button
            browse_btn = ctk.CTkButton(row, text="Browse...", width=80,
                                       command=lambda st=sound_type: self.browse_sound(st))
            browse_btn.pack(side='left', padx=(0, 5))

            # Reset button
            reset_btn = ctk.CTkButton(row, text="Reset", width=60, fg_color="gray40",
                                      command=lambda st=sound_type: self.reset_sound(st))
            reset_btn.pack(side='left')

        # Add padding at bottom
        ctk.CTkLabel(sounds_frame, text="").pack(pady=5)

        # Info text
        ctk.CTkLabel(sounds_scroll, text="Supported format: WAV files (44100 Hz recommended)",
                    text_color="gray").pack(anchor='w', pady=(0, 5))

        sounds_folder = Path(__file__).parent / 'sounds'
        ctk.CTkLabel(sounds_scroll, text=f"Sound files location: {sounds_folder}",
                    text_color="gray").pack(anchor='w')

        # === ADVANCED TAB ===
        advanced_tab = self.tabview.tab("Advanced")
        
        # Create scrollable frame for Advanced tab content
        advanced_scroll = ctk.CTkScrollableFrame(advanced_tab, fg_color="transparent")
        advanced_scroll.pack(fill='both', expand=True)

        # Continuous Mode Settings
        cont_label = ctk.CTkLabel(advanced_scroll, text="Continuous Mode Settings", font=ctk.CTkFont(size=16, weight="bold"))
        cont_label.pack(anchor='w', pady=(15, 10))

        cont_frame = ctk.CTkFrame(advanced_scroll, corner_radius=10)
        cont_frame.pack(fill='x', pady=(0, 20))

        # Silence threshold
        silence_row = ctk.CTkFrame(cont_frame, fg_color="transparent")
        silence_row.pack(fill='x', padx=15, pady=(15, 8))
        ctk.CTkLabel(silence_row, text="Silence threshold:", width=150, anchor='w').pack(side='left')
        self.silence_var = tk.DoubleVar(value=self.app.config.get('silence_threshold', 2.0))
        silence_entry = ctk.CTkEntry(silence_row, textvariable=self.silence_var, width=80)
        silence_entry.pack(side='left', padx=(0, 10))
        ctk.CTkLabel(silence_row, text="seconds").pack(side='left')

        # Min speech duration
        speech_row = ctk.CTkFrame(cont_frame, fg_color="transparent")
        speech_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(speech_row, text="Min speech duration:", width=150, anchor='w').pack(side='left')
        self.min_speech_var = tk.DoubleVar(value=self.app.config.get('min_speech_duration', 0.3))
        min_speech_entry = ctk.CTkEntry(speech_row, textvariable=self.min_speech_var, width=80)
        min_speech_entry.pack(side='left', padx=(0, 10))
        ctk.CTkLabel(speech_row, text="seconds").pack(side='left')

        # Performance Settings
        perf_label = ctk.CTkLabel(advanced_scroll, text="Performance", font=ctk.CTkFont(size=16, weight="bold"))
        perf_label.pack(anchor='w', pady=(0, 10))

        perf_frame = ctk.CTkFrame(advanced_scroll, corner_radius=10)
        perf_frame.pack(fill='x', pady=(0, 20))

        perf_row = ctk.CTkFrame(perf_frame, fg_color="transparent")
        perf_row.pack(fill='x', padx=15, pady=(15, 5))
        ctk.CTkLabel(perf_row, text="Performance mode:", width=150, anchor='w').pack(side='left')
        self.perf_mode_var = tk.StringVar(value=self.app.config.get('performance_mode', 'balanced'))
        perf_combo = ctk.CTkComboBox(perf_row, variable=self.perf_mode_var,
                                      values=['fast', 'balanced', 'accurate'],
                                      width=150, state='readonly')
        perf_combo.pack(side='left')

        ctk.CTkLabel(perf_frame, text="fast: Lowest latency | balanced: Good tradeoff | accurate: Best quality",
                    text_color="gray").pack(anchor='w', padx=15, pady=(0, 15))

        # Wake Word Settings
        wake_label = ctk.CTkLabel(advanced_scroll, text="Wake Word Settings", font=ctk.CTkFont(size=16, weight="bold"))
        wake_label.pack(anchor='w', pady=(0, 10))

        wake_frame = ctk.CTkFrame(advanced_scroll, corner_radius=10)
        wake_frame.pack(fill='x')

        # Get wake word config
        ww_config = self.app.config.get('wake_word_config', {})
        
        # Wake word phrase
        wake_word_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        wake_word_row.pack(fill='x', padx=15, pady=(15, 8))
        ctk.CTkLabel(wake_word_row, text="Wake phrase:", width=120, anchor='w').pack(side='left')
        
        phrase_options = ww_config.get('phrase_options', ['samsara', 'hey samsara', 'computer', 'jarvis'])
        current_phrase = ww_config.get('phrase', 'samsara')
        self.wake_phrase_var = tk.StringVar(value=current_phrase)
        wake_phrase_dropdown = ctk.CTkComboBox(wake_word_row, variable=self.wake_phrase_var,
                                               values=phrase_options, width=150)
        wake_phrase_dropdown.pack(side='left', padx=(0, 10))
        ctk.CTkLabel(wake_word_row, text="(or type custom)", text_color="gray").pack(side='left')

        # End word
        end_config = ww_config.get('end_word', {})
        end_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        end_row.pack(fill='x', padx=15, pady=(0, 8))
        
        self.end_word_enabled_var = tk.BooleanVar(value=end_config.get('enabled', True))
        ctk.CTkCheckBox(end_row, text="End word:", variable=self.end_word_enabled_var,
                       width=120).pack(side='left')
        
        end_options = end_config.get('phrase_options', ['over', 'done', 'go', 'send', 'execute'])
        self.end_phrase_var = tk.StringVar(value=end_config.get('phrase', 'over'))
        end_dropdown = ctk.CTkComboBox(end_row, variable=self.end_phrase_var,
                                       values=end_options, width=150)
        end_dropdown.pack(side='left', padx=(0, 10))

        # Cancel word
        cancel_config = ww_config.get('cancel_word', {})
        cancel_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        cancel_row.pack(fill='x', padx=15, pady=(0, 8))
        
        self.cancel_word_enabled_var = tk.BooleanVar(value=cancel_config.get('enabled', False))
        ctk.CTkCheckBox(cancel_row, text="Cancel word:", variable=self.cancel_word_enabled_var,
                       width=120).pack(side='left')
        
        cancel_options = cancel_config.get('phrase_options', ['cancel', 'abort', 'never mind'])
        self.cancel_phrase_var = tk.StringVar(value=cancel_config.get('phrase', 'cancel'))
        cancel_dropdown = ctk.CTkComboBox(cancel_row, variable=self.cancel_phrase_var,
                                          values=cancel_options, width=150)
        cancel_dropdown.pack(side='left', padx=(0, 10))

        # Pause word
        pause_config = ww_config.get('pause_word', {})
        pause_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        pause_row.pack(fill='x', padx=15, pady=(0, 12))
        
        self.pause_word_enabled_var = tk.BooleanVar(value=pause_config.get('enabled', False))
        ctk.CTkCheckBox(pause_row, text="Pause word:", variable=self.pause_word_enabled_var,
                       width=120).pack(side='left')
        
        pause_options = pause_config.get('phrase_options', ['pause', 'hold on', 'wait'])
        self.pause_phrase_var = tk.StringVar(value=pause_config.get('phrase', 'pause'))
        pause_dropdown = ctk.CTkComboBox(pause_row, variable=self.pause_phrase_var,
                                         values=pause_options, width=150)
        pause_dropdown.pack(side='left', padx=(0, 10))

        # Dictation Mode Timeouts section
        modes_label = ctk.CTkLabel(wake_frame, text="Dictation Mode Timeouts", 
                                   font=ctk.CTkFont(size=13, weight="bold"))
        modes_label.pack(anchor='w', padx=15, pady=(5, 8))
        
        modes_config = ww_config.get('modes', {})
        
        # Dictate timeout
        dictate_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        dictate_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(dictate_row, text="\"dictate\":", width=100, anchor='w').pack(side='left')
        self.dictate_timeout_var = tk.DoubleVar(value=modes_config.get('dictate', {}).get('silence_timeout', 0.6))
        ctk.CTkEntry(dictate_row, textvariable=self.dictate_timeout_var, width=60).pack(side='left', padx=(0, 5))
        ctk.CTkLabel(dictate_row, text="sec", width=30).pack(side='left')

        # Short dictate timeout
        short_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        short_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(short_row, text="\"short dictate\":", width=100, anchor='w').pack(side='left')
        self.short_timeout_var = tk.DoubleVar(value=modes_config.get('short_dictate', {}).get('silence_timeout', 0.4))
        ctk.CTkEntry(short_row, textvariable=self.short_timeout_var, width=60).pack(side='left', padx=(0, 5))
        ctk.CTkLabel(short_row, text="sec", width=30).pack(side='left')

        # Long dictate timeout
        long_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        long_row.pack(fill='x', padx=15, pady=(0, 12))
        ctk.CTkLabel(long_row, text="\"long dictate\":", width=100, anchor='w').pack(side='left')
        self.long_timeout_var = tk.DoubleVar(value=modes_config.get('long_dictate', {}).get('silence_timeout', 60.0))
        ctk.CTkEntry(long_row, textvariable=self.long_timeout_var, width=60).pack(side='left', padx=(0, 5))
        ctk.CTkLabel(long_row, text="sec (requires end word)", text_color="gray").pack(side='left')

        # Test/Debug button
        debug_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        debug_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkButton(debug_row, text="Test Wake Word...", width=150,
                     command=self.open_wake_word_debug).pack(side='left')
        ctk.CTkLabel(debug_row, text="Live testing and parameter tuning",
                    text_color="gray").pack(side='left', padx=(10, 0))

        self.window.protocol("WM_DELETE_WINDOW", self.close)

        # Show the fully-built window and bring to front
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self.window.after(100, lambda: self.window.lift())

    def start_capture(self, hotkey_name):
        self.capturing_hotkey = hotkey_name
        self.captured_keys = set()

        if hotkey_name == 'hotkey':
            self.hotkey_var.set("Press keys...")
            self.hotkey_btn.configure(text="...")
        elif hotkey_name == 'continuous_hotkey':
            self.cont_hotkey_var.set("Press keys...")
            self.cont_hotkey_btn.configure(text="...")
        elif hotkey_name == 'wake_word_hotkey':
            self.wake_hotkey_var.set("Press keys...")
            self.wake_hotkey_btn.configure(text="...")
        elif hotkey_name == 'cancel_hotkey':
            self.cancel_hotkey_var.set("Press keys...")
            self.cancel_hotkey_btn.configure(text="...")

        self.window.bind('<KeyPress>', self.on_capture_key)
        self.window.bind('<KeyRelease>', self.on_capture_release)

    def on_capture_key(self, event):
        if self.capturing_hotkey is None:
            return

        key = event.keysym.lower()
        if key in ('control_l', 'control_r'):
            key = 'ctrl'
        elif key in ('shift_l', 'shift_r'):
            key = 'shift'
        elif key in ('alt_l', 'alt_r'):
            key = 'alt'
        elif key in ('super_l', 'super_r', 'win_l', 'win_r'):
            key = 'win'

        self.captured_keys.add(key)
        hotkey_str = '+'.join(sorted(self.captured_keys))

        if self.capturing_hotkey == 'hotkey':
            self.hotkey_var.set(hotkey_str)
        elif self.capturing_hotkey == 'continuous_hotkey':
            self.cont_hotkey_var.set(hotkey_str)
        elif self.capturing_hotkey == 'wake_word_hotkey':
            self.wake_hotkey_var.set(hotkey_str)
        elif self.capturing_hotkey == 'cancel_hotkey':
            self.cancel_hotkey_var.set(hotkey_str)

    def on_capture_release(self, event):
        if self.capturing_hotkey is None:
            return

        hotkey_str = '+'.join(sorted(self.captured_keys))
        if hotkey_str:
            if self.capturing_hotkey == 'hotkey':
                self.hotkey_var.set(hotkey_str)
                self.hotkey_btn.configure(text="Set")
            elif self.capturing_hotkey == 'continuous_hotkey':
                self.cont_hotkey_var.set(hotkey_str)
                self.cont_hotkey_btn.configure(text="Set")
            elif self.capturing_hotkey == 'wake_word_hotkey':
                self.wake_hotkey_var.set(hotkey_str)
                self.wake_hotkey_btn.configure(text="Set")
            elif self.capturing_hotkey == 'cancel_hotkey':
                self.cancel_hotkey_var.set(hotkey_str)
                self.cancel_hotkey_btn.configure(text="Set")

        self.window.unbind('<KeyPress>')
        self.window.unbind('<KeyRelease>')
        self.capturing_hotkey = None
        self.captured_keys = set()

    def save_settings(self):
        old_model = self.app.config.get('model_size', 'base')
        # Convert display name back to actual model value
        model_display = self.model_var.get()
        new_model = self.model_display_to_value.get(model_display, 'base')
        model_changed = old_model != new_model

        self.app.config['mode'] = self.mode_var.get()
        self.app.config['hotkey'] = self.hotkey_var.get()
        self.app.config['continuous_hotkey'] = self.cont_hotkey_var.get()
        self.app.config['wake_word_hotkey'] = self.wake_hotkey_var.get()
        self.app.config['cancel_hotkey'] = self.cancel_hotkey_var.get()
        self.app.config['silence_threshold'] = self.silence_var.get()
        self.app.config['min_speech_duration'] = self.min_speech_var.get()
        self.app.config['auto_paste'] = self.auto_paste_var.get()
        self.app.config['add_trailing_space'] = self.trailing_space_var.get()
        self.app.config['auto_capitalize'] = self.auto_capitalize_var.get()
        self.app.config['format_numbers'] = self.format_numbers_var.get()
        self.app.config['model_size'] = new_model
        self.app.config['command_mode_enabled'] = self.command_mode_var.get()
        self.app.config['show_all_audio_devices'] = self.show_all_devices_var.get()
        self.app.config['audio_feedback'] = self.audio_feedback_var.get()
        self.app.config['sound_volume'] = self.sound_volume_var.get()
        self.app.config['performance_mode'] = self.perf_mode_var.get()

        # Save wake word config
        ww_config = self.app.config.get('wake_word_config', {})
        ww_config['phrase'] = self.wake_phrase_var.get()
        ww_config['end_word'] = {
            'enabled': self.end_word_enabled_var.get(),
            'phrase': self.end_phrase_var.get(),
            'phrase_options': ww_config.get('end_word', {}).get('phrase_options', [])
        }
        ww_config['cancel_word'] = {
            'enabled': self.cancel_word_enabled_var.get(),
            'phrase': self.cancel_phrase_var.get(),
            'phrase_options': ww_config.get('cancel_word', {}).get('phrase_options', [])
        }
        ww_config['pause_word'] = {
            'enabled': self.pause_word_enabled_var.get(),
            'phrase': self.pause_phrase_var.get(),
            'phrase_options': ww_config.get('pause_word', {}).get('phrase_options', [])
        }
        ww_config['modes'] = {
            'dictate': {
                'silence_timeout': self.dictate_timeout_var.get(),
                'require_end_word': False
            },
            'short_dictate': {
                'silence_timeout': self.short_timeout_var.get(),
                'require_end_word': False
            },
            'long_dictate': {
                'silence_timeout': self.long_timeout_var.get(),
                'require_end_word': True
            }
        }
        self.app.config['wake_word_config'] = ww_config

        self.app.command_mode_enabled = self.command_mode_var.get()

        mic_name = self.mic_var.get()
        mic_changed = False

        for mic in self.available_mics:
            if mic['name'] == mic_name:
                if self.app.config.get('microphone') != mic['id']:
                    mic_changed = True
                    self.app.config['microphone'] = mic['id']
                break

        self.app.save_config()

        # Apply mode change at runtime - deactivate modes that don't match new selection
        new_mode = self.mode_var.get()
        
        # Stop wake word mode if it was active but new mode is different
        if self.app.wake_word_active and new_mode != 'wake_word':
            self.app.stop_wake_word_mode()
            print(f"[MODE] Deactivated wake word mode")
        
        # Stop continuous mode if it was active but new mode is different
        if self.app.continuous_active and new_mode != 'continuous':
            self.app.stop_continuous_mode()
            print(f"[MODE] Deactivated continuous mode")
        
        # Activate the new mode if it's wake_word or continuous
        if new_mode == 'wake_word' and not self.app.wake_word_active:
            self.app.start_wake_word_mode()
            print(f"[MODE] Activated wake word mode")
        elif new_mode == 'continuous' and not self.app.continuous_active:
            self.app.start_continuous_mode()
            print(f"[MODE] Activated continuous mode")
        
        print(f"[MODE] Mode changed to: {new_mode}")

        if mic_changed and hasattr(self.app, 'tray_icon') and hasattr(self.app, 'get_menu'):
            self.app.tray_icon.menu = self.app.get_menu()
            self.app.tray_icon.title = f"Samsara - {self.app.get_current_microphone_name()}"
            print(f"Microphone changed to: {self.app.get_current_microphone_name()}")

        print("Settings saved successfully!")

        if model_changed:
            self.prompt_restart_for_model(old_model, new_model)

    def prompt_restart_for_model(self, old_model, new_model):
        """Ask user if they want to restart to apply new model"""
        result = messagebox.askyesno(
            "Restart Required",
            f"Model changed from '{old_model}' to '{new_model}'.\n\n"
            f"The app needs to restart to load the new model.\n"
            f"(The new model will be downloaded if needed)\n\n"
            f"Restart now?",
            parent=self.window
        )
        if result:
            self.restart_app()

    def restart_app(self):
        """Restart the application"""
        print("Restarting application...")
        self.close()

        python = sys.executable
        script = os.path.abspath(sys.argv[0])

        if hasattr(self.app, 'tray_icon'):
            self.app.tray_icon.stop()
        if hasattr(self.app, 'keyboard_listener'):
            self.app.keyboard_listener.stop()

        os.execv(python, [python, script])

    def save_and_close(self):
        """Save settings and close window"""
        self.save_settings()
        self.close()

    def get_startup_path(self):
        """Get the platform-specific startup/autostart file path"""
        if sys.platform == 'win32':
            startup_folder = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
            return startup_folder / 'Samsara.vbs'
        elif sys.platform == 'darwin':  # macOS
            return Path.home() / 'Library' / 'LaunchAgents' / 'com.samsara.plist'
        else:  # Linux
            config_home = os.environ.get('XDG_CONFIG_HOME', '')
            if config_home:
                return Path(config_home) / 'autostart' / 'samsara.desktop'
            return Path.home() / '.config' / 'autostart' / 'samsara.desktop'

    def check_auto_start(self):
        """Check if auto-start is enabled"""
        startup_file = self.get_startup_path()
        return startup_file.exists()

    def toggle_auto_start(self):
        """Enable or disable auto-start (cross-platform)"""
        startup_file = self.get_startup_path()
        script_path = Path(__file__)
        python_exe = sys.executable

        if self.auto_start_var.get():
            # Enable auto-start: create platform-specific startup entry
            try:
                startup_file.parent.mkdir(parents=True, exist_ok=True)

                if sys.platform == 'win32':
                    # Windows VBS script
                    vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "{script_path.parent}"
WshShell.Run """" & "{python_exe}" & """ """ & "{script_path}" & """", 0, False
Set WshShell = Nothing
'''
                    startup_file.write_text(vbs_content)

                elif sys.platform == 'darwin':
                    # macOS launchd plist
                    plist_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.samsara</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe}</string>
        <string>{script_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{script_path.parent}</string>
</dict>
</plist>
'''
                    startup_file.write_text(plist_content)

                else:
                    # Linux .desktop file
                    desktop_content = f'''[Desktop Entry]
Type=Application
Name=Samsara
Exec={python_exe} {script_path}
Path={script_path.parent}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
'''
                    startup_file.write_text(desktop_content)

                platform_name = "Windows" if sys.platform == 'win32' else ("macOS" if sys.platform == 'darwin' else "Linux")
                messagebox.showinfo("Auto-Start Enabled",
                    f"Samsara will now start automatically when {platform_name} starts.",
                    parent=self.window)
            except Exception as e:
                messagebox.showerror("Error",
                    f"Failed to enable auto-start:\n{e}",
                    parent=self.window)
                self.auto_start_var.set(False)
        else:
            # Disable auto-start: remove startup entry
            try:
                if startup_file.exists():
                    startup_file.unlink()
                messagebox.showinfo("Auto-Start Disabled",
                    "Samsara will no longer start automatically.",
                    parent=self.window)
            except Exception as e:
                messagebox.showerror("Error",
                    f"Failed to disable auto-start:\n{e}",
                    parent=self.window)
                self.auto_start_var.set(True)

    def open_profile_manager(self):
        """Open the profile manager window."""
        # Get the app directory for ProfileManager
        app_dir = Path(__file__).parent
        
        # Initialize profile manager
        pm = ProfileManager(str(app_dir))
        
        # Define callback for when profiles change
        def on_profiles_changed():
            # Reload the commands in the app
            if hasattr(self.app, 'load_commands'):
                self.app.load_commands()
            # Reload training data (vocabulary/corrections)
            if hasattr(self.app, 'load_training_data'):
                self.app.load_training_data()
        
        # Open the profile manager window
        profile_window = ProfileManagerWindow(
            self.window,
            pm,
            on_profiles_changed=on_profiles_changed
        )
        profile_window.show()

    def open_voice_training(self):
        """Open the voice training window from settings."""
        self.app.open_voice_training()

    def open_wake_word_debug(self):
        """Open the wake word debug window from settings."""
        self.app.open_wake_word_debug()

    def close(self):
        if self.window:
            try:
                self.window.destroy()
            except:
                pass
            finally:
                self.window = None

    def on_volume_change(self, value):
        """Update volume label and apply volume change immediately"""
        volume = float(value)
        self.volume_label.configure(text=f"{int(volume * 100)}%")
        # Apply volume change immediately
        self.app.config['sound_volume'] = volume

    def apply_sound_theme(self):
        """Apply the selected sound theme"""
        import shutil
        theme = self.sound_theme_var.get()
        themes_dir = Path(__file__).parent / 'sounds' / 'themes' / theme
        sounds_dir = Path(__file__).parent / 'sounds'
        
        if not themes_dir.exists():
            print(f"[WARN] Theme folder not found: {themes_dir}")
            return
        
        # Copy theme sounds to main sounds folder
        for wav in themes_dir.glob('*.wav'):
            shutil.copy2(wav, sounds_dir / wav.name)
        
        # Save theme preference
        self.app.config['sound_theme'] = theme
        self.app.save_config()
        
        # Reload sound cache
        self.app._load_sound_cache()
        
        # Play success sound to preview
        self.app.play_sound('success')
        print(f"[OK] Sound theme applied: {theme}")

    def preview_sound(self, sound_type):
        """Play preview of the selected sound"""
        self.app.play_sound(sound_type)

    def browse_sound(self, sound_type):
        """Browse for a custom WAV file"""
        from tkinter import filedialog
        import shutil

        filename = filedialog.askopenfilename(
            title=f"Select {sound_type} sound",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
            parent=self.window
        )

        if filename:
            # Copy file to sounds folder with correct name
            dest = self.app.sounds_dir / f"{sound_type}.wav"
            try:
                shutil.copy(filename, dest)
                self.app._load_sound_cache()
                self.sound_labels[sound_type].configure(text=f"{sound_type}.wav")
                messagebox.showinfo("Sound Updated",
                    f"Sound file updated successfully!\n\nFile: {Path(filename).name}",
                    parent=self.window)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to copy sound file:\n{e}", parent=self.window)

    def reset_sound(self, sound_type):
        """Reset sound to default generated tone"""
        import wave

        sound_file = self.app.sound_files.get(sound_type)
        if sound_file and sound_file.exists():
            sound_file.unlink()  # Delete existing file

        # Regenerate default sound
        sample_rate = 44100

        def generate_tone(frequency, duration, volume=0.5):
            n_samples = int(sample_rate * duration)
            t = np.linspace(0, duration, n_samples, False)
            tone = np.sin(2 * np.pi * frequency * t) * volume
            fade_samples = min(int(sample_rate * 0.01), n_samples // 4)
            if fade_samples > 0:
                tone[:fade_samples] *= np.linspace(0, 1, fade_samples)
                tone[-fade_samples:] *= np.linspace(1, 0, fade_samples)
            return tone

        def save_wav(filepath, audio_data):
            with wave.open(str(filepath), 'w') as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                audio_int = (audio_data * 32767).astype(np.int16)
                wav_file.writeframes(audio_int.tobytes())

        if sound_type == 'start':
            tone = generate_tone(660, 0.12, volume=0.6)
            save_wav(sound_file, tone)
        elif sound_type == 'stop':
            tone = generate_tone(440, 0.1, volume=0.5)
            save_wav(sound_file, tone)
        elif sound_type == 'success':
            t1 = generate_tone(523, 0.08, volume=0.5)
            gap = np.zeros(int(sample_rate * 0.02))
            t2 = generate_tone(659, 0.08, volume=0.5)
            t3 = generate_tone(784, 0.12, volume=0.5)
            audio = np.concatenate([t1, gap, t2, gap, t3])
            save_wav(sound_file, audio)
        elif sound_type == 'error':
            t1 = generate_tone(220, 0.15, volume=0.5)
            gap = np.zeros(int(sample_rate * 0.08))
            t2 = generate_tone(196, 0.18, volume=0.5)
            audio = np.concatenate([t1, gap, t2])
            save_wav(sound_file, audio)

        self.app._load_sound_cache()
        self.sound_labels[sound_type].configure(text=f"{sound_type}.wav")
        messagebox.showinfo("Sound Reset", f"'{sound_type}' sound reset to default.", parent=self.window)

    # === COMMAND MANAGEMENT METHODS ===

    def get_command_action_text(self, cmd_data):
        """Get human-readable action text for a command"""
        cmd_type = cmd_data.get('type', '')
        if cmd_type == 'hotkey':
            keys = cmd_data.get('keys', [])
            return '+'.join(k.capitalize() for k in keys)
        elif cmd_type == 'launch':
            target = cmd_data.get('target', '')
            # Shorten long paths
            if len(target) > 30:
                return '...' + target[-27:]
            return target
        elif cmd_type == 'press':
            return f"Press {cmd_data.get('key', '').upper()}"
        elif cmd_type == 'key_down':
            return f"Hold {cmd_data.get('key', '').upper()}"
        elif cmd_type == 'key_up':
            return f"Release {cmd_data.get('key', '').upper()}"
        elif cmd_type == 'mouse':
            action = cmd_data.get('action', 'click')
            button = cmd_data.get('button', 'left')
            return f"{action.replace('_', ' ').title()} ({button})"
        elif cmd_type == 'release_all':
            return "Release all keys"
        return str(cmd_data)

    def populate_commands_list(self, filter_text=''):
        """Populate the commands treeview"""
        # Clear existing items
        for item in self.cmd_tree.get_children():
            self.cmd_tree.delete(item)

        # Get commands from the app's command executor
        commands = self.app.command_executor.commands

        for phrase, cmd_data in sorted(commands.items()):
            # Filter if search text provided
            if filter_text:
                search_lower = filter_text.lower()
                if (search_lower not in phrase.lower() and
                    search_lower not in cmd_data.get('type', '').lower() and
                    search_lower not in cmd_data.get('description', '').lower()):
                    continue

            cmd_type = cmd_data.get('type', 'unknown')
            action = self.get_command_action_text(cmd_data)
            description = cmd_data.get('description', '')

            self.cmd_tree.insert('', 'end', values=(phrase, cmd_type, action, description))

    def filter_commands(self):
        """Filter commands based on search box"""
        filter_text = self.cmd_search_var.get()
        self.populate_commands_list(filter_text)

    def get_selected_command(self):
        """Get the currently selected command phrase"""
        selection = self.cmd_tree.selection()
        if not selection:
            return None
        item = self.cmd_tree.item(selection[0])
        return item['values'][0] if item['values'] else None

    def add_command_dialog(self):
        """Open dialog to add a new command"""
        self.open_command_editor(None)

    def edit_command_dialog(self):
        """Open dialog to edit selected command"""
        phrase = self.get_selected_command()
        if not phrase:
            messagebox.showwarning("No Selection", "Please select a command to edit.", parent=self.window)
            return
        self.open_command_editor(phrase)

    def open_command_editor(self, edit_phrase=None):
        """Open the command editor dialog"""
        dialog = ctk.CTkToplevel(self.window)
        dialog.title("Edit Command" if edit_phrase else "Add Command")
        dialog.geometry("500x400")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        # Center on parent
        dialog.update_idletasks()
        x = self.window.winfo_x() + (self.window.winfo_width() - 500) // 2
        y = self.window.winfo_y() + (self.window.winfo_height() - 400) // 2
        dialog.geometry(f"+{x}+{y}")

        # Get existing command data if editing
        existing_data = {}
        if edit_phrase:
            existing_data = self.app.command_executor.commands.get(edit_phrase, {})

        # Voice phrase
        ctk.CTkLabel(dialog, text="Voice Phrase:", font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(20, 5))
        phrase_var = tk.StringVar(value=edit_phrase or '')
        phrase_entry = ctk.CTkEntry(dialog, textvariable=phrase_var, width=300)
        phrase_entry.pack(anchor='w', padx=20)
        ctk.CTkLabel(dialog, text="What you say to trigger this command", text_color="gray").pack(anchor='w', padx=20)

        # Command type
        ctk.CTkLabel(dialog, text="Command Type:", font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(15, 5))
        type_var = tk.StringVar(value=existing_data.get('type', 'hotkey'))
        type_combo = ctk.CTkComboBox(dialog, variable=type_var, width=200, state='readonly',
                                     values=['hotkey', 'text', 'launch', 'press', 'key_down', 'key_up', 'mouse', 'release_all'])
        type_combo.pack(anchor='w', padx=20)

        # Dynamic fields frame
        fields_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        fields_frame.pack(fill='x', padx=20, pady=(15, 0))

        # Variables for different field types
        keys_var = tk.StringVar(value='+'.join(existing_data.get('keys', [])))
        target_var = tk.StringVar(value=existing_data.get('target', ''))
        key_var = tk.StringVar(value=existing_data.get('key', ''))
        text_var = tk.StringVar(value=existing_data.get('text', ''))
        mouse_action_var = tk.StringVar(value=existing_data.get('action', 'click'))
        mouse_button_var = tk.StringVar(value=existing_data.get('button', 'left'))

        field_widgets = []

        def update_fields(*args):
            # Clear existing field widgets
            for widget in field_widgets:
                widget.destroy()
            field_widgets.clear()

            cmd_type = type_var.get()

            if cmd_type == 'hotkey':
                lbl = ctk.CTkLabel(fields_frame, text="Keys (e.g., ctrl+shift+a):")
                lbl.pack(anchor='w')
                field_widgets.append(lbl)
                entry = ctk.CTkEntry(fields_frame, textvariable=keys_var, width=300)
                entry.pack(anchor='w')
                field_widgets.append(entry)
                hint = ctk.CTkLabel(fields_frame, text="Use + to combine keys: ctrl, shift, alt, win, a-z, 0-9, f1-f12, etc.", text_color="gray")
                hint.pack(anchor='w')
                field_widgets.append(hint)

            elif cmd_type == 'text':
                lbl = ctk.CTkLabel(fields_frame, text="Text to insert:")
                lbl.pack(anchor='w')
                field_widgets.append(lbl)
                entry = ctk.CTkEntry(fields_frame, textvariable=text_var, width=300)
                entry.pack(anchor='w')
                field_widgets.append(entry)
                hint = ctk.CTkLabel(fields_frame, text="Punctuation, symbols, or any text to paste", text_color="gray")
                hint.pack(anchor='w')
                field_widgets.append(hint)

            elif cmd_type == 'launch':
                lbl = ctk.CTkLabel(fields_frame, text="Program/Command to run:")
                lbl.pack(anchor='w')
                field_widgets.append(lbl)
                entry = ctk.CTkEntry(fields_frame, textvariable=target_var, width=400)
                entry.pack(anchor='w')
                field_widgets.append(entry)
                hint = ctk.CTkLabel(fields_frame, text="e.g., chrome.exe, notepad.exe, or full path", text_color="gray")
                hint.pack(anchor='w')
                field_widgets.append(hint)

            elif cmd_type in ('press', 'key_down', 'key_up'):
                lbl = ctk.CTkLabel(fields_frame, text="Key:")
                lbl.pack(anchor='w')
                field_widgets.append(lbl)
                entry = ctk.CTkEntry(fields_frame, textvariable=key_var, width=150)
                entry.pack(anchor='w')
                field_widgets.append(entry)
                hint = ctk.CTkLabel(fields_frame, text="Single key: a, space, enter, shift, w, etc.", text_color="gray")
                hint.pack(anchor='w')
                field_widgets.append(hint)

            elif cmd_type == 'mouse':
                lbl1 = ctk.CTkLabel(fields_frame, text="Mouse Action:")
                lbl1.pack(anchor='w')
                field_widgets.append(lbl1)
                action_combo = ctk.CTkComboBox(fields_frame, variable=mouse_action_var, width=150, state='readonly',
                                               values=['click', 'double_click'])
                action_combo.pack(anchor='w')
                field_widgets.append(action_combo)

                lbl2 = ctk.CTkLabel(fields_frame, text="Button:")
                lbl2.pack(anchor='w', pady=(10, 0))
                field_widgets.append(lbl2)
                btn_combo = ctk.CTkComboBox(fields_frame, variable=mouse_button_var, width=150, state='readonly',
                                            values=['left', 'right', 'middle'])
                btn_combo.pack(anchor='w')
                field_widgets.append(btn_combo)

            elif cmd_type == 'release_all':
                lbl = ctk.CTkLabel(fields_frame, text="No additional settings needed.\nThis releases all held keys.", text_color="gray")
                lbl.pack(anchor='w')
                field_widgets.append(lbl)

        type_var.trace('w', update_fields)
        update_fields()  # Initial population

        # Description
        ctk.CTkLabel(dialog, text="Description:", font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(15, 5))
        desc_var = tk.StringVar(value=existing_data.get('description', ''))
        desc_entry = ctk.CTkEntry(dialog, textvariable=desc_var, width=400)
        desc_entry.pack(anchor='w', padx=20)

        # Buttons
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill='x', padx=20, pady=20)

        def save_command():
            phrase = phrase_var.get().strip().lower()
            if not phrase:
                messagebox.showerror("Error", "Voice phrase is required.", parent=dialog)
                return

            # Check for duplicate if adding new or renaming
            if not edit_phrase or phrase != edit_phrase.lower():
                if phrase in self.app.command_executor.commands:
                    messagebox.showerror("Error", f"A command with phrase '{phrase}' already exists.", parent=dialog)
                    return

            cmd_type = type_var.get()
            cmd_data = {
                'type': cmd_type,
                'description': desc_var.get().strip()
            }

            if cmd_type == 'hotkey':
                keys = [k.strip().lower() for k in keys_var.get().split('+') if k.strip()]
                if not keys:
                    messagebox.showerror("Error", "Please specify at least one key.", parent=dialog)
                    return
                cmd_data['keys'] = keys

            elif cmd_type == 'launch':
                target = target_var.get().strip()
                if not target:
                    messagebox.showerror("Error", "Please specify a program to launch.", parent=dialog)
                    return
                cmd_data['target'] = target

            elif cmd_type in ('press', 'key_down', 'key_up'):
                key = key_var.get().strip().lower()
                if not key:
                    messagebox.showerror("Error", "Please specify a key.", parent=dialog)
                    return
                cmd_data['key'] = key

            elif cmd_type == 'mouse':
                cmd_data['action'] = mouse_action_var.get()
                cmd_data['button'] = mouse_button_var.get()

            elif cmd_type == 'text':
                text_to_insert = text_var.get().strip()
                if not text_to_insert:
                    messagebox.showerror("Error", "Please specify text to insert.", parent=dialog)
                    return
                cmd_data['text'] = text_to_insert

            # Remove old command if renaming
            if edit_phrase and phrase != edit_phrase.lower():
                del self.app.command_executor.commands[edit_phrase]

            # Add/update command
            self.app.command_executor.commands[phrase] = cmd_data

            # Save to file
            self.save_commands()

            # Refresh list
            self.populate_commands_list(self.cmd_search_var.get())

            dialog.destroy()
            messagebox.showinfo("Success", f"Command '{phrase}' saved successfully!", parent=self.window)

        ctk.CTkButton(btn_frame, text="Save", width=100, command=save_command).pack(side='right', padx=(10, 0))
        ctk.CTkButton(btn_frame, text="Cancel", width=100, fg_color="gray40",
                     command=dialog.destroy).pack(side='right')

    def delete_command(self):
        """Delete the selected command"""
        phrase = self.get_selected_command()
        if not phrase:
            messagebox.showwarning("No Selection", "Please select a command to delete.", parent=self.window)
            return

        if messagebox.askyesno("Confirm Delete",
                              f"Are you sure you want to delete the command '{phrase}'?",
                              parent=self.window):
            if phrase in self.app.command_executor.commands:
                del self.app.command_executor.commands[phrase]
                self.save_commands()
                self.populate_commands_list(self.cmd_search_var.get())
                messagebox.showinfo("Deleted", f"Command '{phrase}' deleted.", parent=self.window)

    def test_command(self):
        """Test/execute the selected command"""
        phrase = self.get_selected_command()
        if not phrase:
            messagebox.showwarning("No Selection", "Please select a command to test.", parent=self.window)
            return

        # Minimize settings window briefly
        self.window.iconify()
        self.window.after(500, lambda: self._execute_test_command(phrase))

    def _execute_test_command(self, phrase):
        """Execute test command after delay"""
        try:
            result = self.app.command_executor.execute_command(phrase)
            self.window.after(500, self.window.deiconify)
            if result:
                messagebox.showinfo("Test Result", f"Command '{phrase}' executed successfully!", parent=self.window)
            else:
                messagebox.showwarning("Test Result", f"Command '{phrase}' not found or failed.", parent=self.window)
        except Exception as e:
            self.window.deiconify()
            messagebox.showerror("Test Error", f"Error executing command:\n{e}", parent=self.window)

    def reload_commands(self):
        """Reload commands from file"""
        try:
            self.app.command_executor.load_commands()
            self.populate_commands_list(self.cmd_search_var.get())
            messagebox.showinfo("Reloaded", f"Loaded {len(self.app.command_executor.commands)} commands.", parent=self.window)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to reload commands:\n{e}", parent=self.window)

    def save_commands(self):
        """Save commands to commands.json"""
        commands_path = Path(__file__).parent / 'commands.json'
        try:
            data = {'commands': self.app.command_executor.commands}
            with open(commands_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save commands:\n{e}", parent=self.window)

    def refresh_microphone_list(self):
        """Refresh the microphone list when 'show all devices' is toggled"""
        self.app.config['show_all_audio_devices'] = self.show_all_devices_var.get()
        self.available_mics = self.app.get_available_microphones()

        mic_names = [mic['name'] for mic in self.available_mics]
        self.mic_combo.configure(values=mic_names)

        if self.mic_var.get() not in mic_names and mic_names:
            self.mic_var.set(mic_names[0])


class HistoryWindow:
    """Window to view dictation history"""

    def __init__(self, app):
        self.app = app
        self.window = None

    def show(self):
        if self.window is not None:
            try:
                self.window.lift()
                self.window.focus_force()
                return
            except:
                self.window = None

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.window = ctk.CTkToplevel(self.app.root)
        self.window.title("Dictation History")
        self.window.geometry("600x500")
        self.window.resizable(True, True)
        self.window.minsize(400, 300)

        self.window.lift()
        self.window.focus_force()
        self.window.after(100, lambda: self.window.lift())

        # Use grid layout
        self.window.grid_rowconfigure(0, weight=1)
        self.window.grid_columnconfigure(0, weight=1)

        # Main frame
        main_frame = ctk.CTkFrame(self.window)
        main_frame.grid(row=0, column=0, sticky='nsew', padx=20, pady=(20, 10))
        main_frame.grid_rowconfigure(0, weight=1)
        main_frame.grid_columnconfigure(0, weight=1)

        # Treeview for history
        tree_container = ctk.CTkFrame(main_frame, fg_color="transparent")
        tree_container.grid(row=0, column=0, sticky='nsew', padx=10, pady=10)
        tree_container.grid_rowconfigure(0, weight=1)
        tree_container.grid_columnconfigure(0, weight=1)

        # Scrollbar
        tree_scroll = ttk.Scrollbar(tree_container)
        tree_scroll.grid(row=0, column=1, sticky='ns')

        # Style for dark mode
        style = ttk.Style()
        # Use 'clam' theme which allows heading customization (Windows default ignores it)
        style.theme_use('clam')
        style.configure("History.Treeview",
                       background="#2b2b2b",
                       foreground="white",
                       fieldbackground="#2b2b2b",
                       rowheight=28)
        style.configure("History.Treeview.Heading",
                       background="#1f6aa5",
                       foreground="white",
                       font=('Segoe UI', 10, 'bold'),
                       relief='flat')
        style.map("History.Treeview.Heading",
                 background=[('active', '#2980b9')])
        style.map("History.Treeview", background=[('selected', '#1f6aa5')])

        self.tree = ttk.Treeview(tree_container, columns=('time', 'type', 'text'),
                                 show='headings', yscrollcommand=tree_scroll.set,
                                 style="History.Treeview")
        self.tree.grid(row=0, column=0, sticky='nsew')
        tree_scroll.config(command=self.tree.yview)

        # Column headings
        self.tree.heading('time', text='Time')
        self.tree.heading('type', text='Type')
        self.tree.heading('text', text='Text')

        # Column widths
        self.tree.column('time', width=140, minwidth=120)
        self.tree.column('type', width=80, minwidth=60)
        self.tree.column('text', width=400, minwidth=200)

        # Populate history
        self.refresh_history()

        # Button frame
        btn_frame = ctk.CTkFrame(self.window, fg_color="transparent", height=60)
        btn_frame.grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 20))
        btn_frame.grid_propagate(False)

        ctk.CTkButton(btn_frame, text="Copy Selected", width=120,
                     command=self.copy_selected).pack(side='left', padx=(0, 5), pady=10)
        ctk.CTkButton(btn_frame, text="Copy All", width=100,
                     command=self.copy_all).pack(side='left', padx=(0, 5), pady=10)
        ctk.CTkButton(btn_frame, text="Clear History", width=100,
                     fg_color="#cc4444", hover_color="#aa3333",
                     command=self.clear_history).pack(side='left', pady=10)

        ctk.CTkButton(btn_frame, text="Refresh", width=80,
                     fg_color="gray40", command=self.refresh_history).pack(side='right', padx=(5, 0), pady=10)
        ctk.CTkButton(btn_frame, text="Close", width=80,
                     fg_color="gray40", command=self.close).pack(side='right', pady=10)

        self.window.protocol("WM_DELETE_WINDOW", self.close)

    def refresh_history(self):
        """Refresh the history list"""
        # Clear existing
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Add history items (newest first)
        for timestamp, text, is_command in reversed(self.app.history):
            item_type = "Command" if is_command else "Dictation"
            # Truncate long text for display
            display_text = text if len(text) <= 80 else text[:77] + "..."
            self.tree.insert('', 'end', values=(timestamp, item_type, display_text),
                           tags=('command' if is_command else 'dictation',))

        # Style tags
        self.tree.tag_configure('command', foreground='#00CED1')
        self.tree.tag_configure('dictation', foreground='white')

    def copy_selected(self):
        """Copy selected item to clipboard"""
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("No Selection", "Please select an item to copy.", parent=self.window)
            return

        # Get full text from history (not truncated display text)
        item = self.tree.item(selection[0])
        index = self.tree.index(selection[0])
        # History is reversed in display, so we need to reverse index
        history_index = len(self.app.history) - 1 - index
        if 0 <= history_index < len(self.app.history):
            _, text, _ = self.app.history[history_index]
            pyperclip.copy(text)
            messagebox.showinfo("Copied", "Text copied to clipboard.", parent=self.window)

    def copy_all(self):
        """Copy all dictation text to clipboard"""
        texts = [text for _, text, is_cmd in self.app.history if not is_cmd]
        if texts:
            pyperclip.copy('\n'.join(texts))
            messagebox.showinfo("Copied", f"Copied {len(texts)} dictations to clipboard.", parent=self.window)
        else:
            messagebox.showinfo("Empty", "No dictation history to copy.", parent=self.window)

    def clear_history(self):
        """Clear all history"""
        if messagebox.askyesno("Clear History",
                              "Are you sure you want to clear all history?",
                              parent=self.window):
            self.app.history.clear()
            self.app.save_history()  # Save empty history to file
            self.refresh_history()

    def close(self):
        if self.window:
            try:
                self.window.destroy()
            except:
                pass
            finally:
                self.window = None


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
        
        # Audio settings
        self.sample_rate = 16000
        self.audio_queue = queue.Queue()
        self.recording = False
        self.audio_data = []

        # Set up audio feedback sounds (creates defaults if needed)
        self._setup_sounds()

        # Model settings
        self.model = None
        self.model_loaded = False
        self.loading_model = False
        
        # Command system
        commands_path = Path(__file__).parent / "commands.json"
        self.command_executor = CommandExecutor(commands_path)
        self.command_mode_enabled = self.config.get('command_mode_enabled', True)
        
        # Hotkey settings
        self.hotkey_pressed = False
        self.current_keys = set()
        self.key_press_times = {}  # Track when each key was pressed
        self.hotkey_window = 0.3  # 300ms window for hotkey detection
        
        # Mode tracking
        self.toggle_active = False  # For toggle mode
        self.continuous_active = False  # For continuous mode
        self.wake_word_active = False  # For wake word mode
        self.continuous_stream = None
        self.silence_start = None
        self.speech_buffer = []
        self.is_speaking = False
        self.wake_word_listening = False  # Currently listening for wake word
        self.wake_word_triggered = False  # Wake word detected, ready for command
        
        # Dictation mode tracking (for wake word dictation)
        self.dictation_mode = None  # None, 'dictate', 'short_dictate', 'long_dictate'
        self.dictation_buffer = []  # Audio buffer for dictation content
        self.dictation_start_time = None  # When dictation started
        
        # Wake word dictation mode tracking
        self.wake_dictation_mode = None  # None, 'dictate', 'short_dictate', 'long_dictate'
        self.wake_dictation_buffer = []  # Audio buffer for dictation content
        self.wake_dictation_start_time = None  # When dictation started
        self._dictation_silence_timeout = None  # Dynamic timeout for current mode
        self._dictation_require_end = False  # Whether current mode requires end word
        self._dictation_finalize_timer = None  # Timer for auto-finalizing dictation

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

        # Key macro manager
        self.key_macro_manager = KeyMacroManager(self.config)
        self.key_macro_manager.start()

        # Notification manager for reminders
        config_dir = Path(__file__).parent
        self.notification_manager = NotificationManager(config_dir)
        if self.config.get('notifications', {}).get('enabled', True):
            self.notification_manager.start()

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
            "cancel_hotkey": "escape",
            "mode": "hold",
            "model_size": "base",
            "language": "en",
            "auto_paste": True,
            "add_trailing_space": True,
            "auto_capitalize": True,
            "format_numbers": True,
            "device": "auto",
            "microphone": None,
            "silence_threshold": 2.0,
            "min_speech_duration": 0.3,
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
                "end_word": {
                    "enabled": True,
                    "phrase": "over",
                    "phrase_options": ["over", "done", "go", "send", "execute", "that's all", "end dictation"]
                },
                "cancel_word": {
                    "enabled": False,
                    "phrase": "cancel",
                    "phrase_options": ["cancel", "abort", "never mind", "scratch that"]
                },
                "pause_word": {
                    "enabled": False,
                    "phrase": "pause",
                    "phrase_options": ["pause", "hold on", "wait"]
                },
                "modes": {
                    "dictate": {
                        "silence_timeout": 0.6,
                        "require_end_word": False
                    },
                    "short_dictate": {
                        "silence_timeout": 0.4,
                        "require_end_word": False
                    },
                    "long_dictate": {
                        "silence_timeout": 60.0,
                        "require_end_word": True
                    }
                },
                "audio": {
                    "speech_threshold": 0.01,
                    "min_speech_duration": 0.3,
                    "wake_detection_silence": 1.2,  # Longer to capture "wake word [pause] command"
                    "wake_command_timeout": 5.0
                },
                "feedback": {
                    "play_sound_on_wake": True,
                    "play_sound_on_end": True
                }
            },
            # Performance mode for transcription speed/accuracy tradeoff
            "performance_mode": "balanced",  # "fast", "balanced", or "accurate"
            # Key macro system for accessibility (e.g., triple-tap W for auto-run)
            "key_macros": get_default_macro_config(),
            # Notification system for reminders (medication, breaks, hydration)
            "notifications": get_default_notification_config()
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
        """Save configuration to JSON file"""
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def get_available_microphones(self):
        """Get list of available microphone devices"""
        devices = sd.query_devices()
        microphones = []
        seen_names = set()
        show_all = self.config.get('show_all_audio_devices', False)
        
        for i, device in enumerate(devices):
            # Only include input devices with at least 1 channel
            if device['max_input_channels'] > 0:
                name = device['name']
                
                # Filter out duplicates
                if name in seen_names:
                    continue
                
                # Apply filters only if "show all devices" is disabled
                if not show_all:
                    # Skip common virtual/loopback devices and unwanted system devices
                    skip_keywords = [
                        'Stereo Mix', 'Wave Out Mix', 'What U Hear', 'Loopback', 
                        'CABLE', 'Virtual Audio', 'VB-Audio', 'Voicemeeter',
                        'Sound Mapper', 'Primary Sound', 'Wave Speaker', 'Wave Microphone',
                        'Stream Wave', 'Chat Capture', 'Hands-Free', 'HF Audio', 'Input ()',
                        'Line In (', 'VDVAD', 'SteelSeries Sonar', 'OCULUSVAD',
                        'VAD Wave', 'wc4400_8200'
                    ]
                    if any(keyword.lower() in name.lower() for keyword in skip_keywords):
                        continue
                    
                    # Skip "Microphone ()" with nothing in parentheses
                    if name.strip() == "Microphone ()":
                        continue
                    
                    # Also skip devices that are clearly drivers/system components
                    if '@System32\\drivers\\' in name:
                        continue
                    
                seen_names.add(name)
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
            import re
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
                import re
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
        """Switch to a different microphone"""
        # Stop any active recording/continuous mode
        if self.continuous_active:
            self.stop_continuous_mode()
        if self.recording:
            self.stop_recording()
        
        # Update config
        self.config['microphone'] = mic_id
        self.save_config()
        
        mic_name = self.get_current_microphone_name()
        print(f"[OK] Switched to microphone: {mic_name}")
        
        # Update tray icon tooltip
        if hasattr(self, 'tray_icon'):
            self.tray_icon.title = f"Samsara - {mic_name}"

    def load_model_async(self):
        """Load Whisper model in background thread"""
        def load():
            self.loading_model = True
            print("Loading Whisper model (this may take a moment on first run)...")
            
            # Determine compute device with detailed logging
            device = self.config['device']
            if device == "auto":
                try:
                    import torch
                    cuda_available = torch.cuda.is_available()
                    if cuda_available:
                        device = "cuda"
                        gpu_name = torch.cuda.get_device_name(0)
                        gpu_mem = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                        print(f"[GPU] CUDA available: {gpu_name} ({gpu_mem:.1f} GB)")
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

        mode = self.config.get('mode', 'hold')
        
        # Get hotkey configs
        main_hotkey = self.config['hotkey']
        cont_hotkey = self.config.get('continuous_hotkey', 'ctrl+alt+d')
        wake_hotkey = self.config.get('wake_word_hotkey', 'ctrl+alt+w')
        cancel_hotkey = self.config.get('cancel_hotkey', 'escape')

        # Use state-based detection - checks if keys are CURRENTLY held, regardless of press order
        # This is more reliable than event-based tracking for simultaneous key combos
        
        # Check for wake word mode toggle (works in any mode)
        if self.check_hotkey_state(wake_hotkey) and not self.hotkey_pressed:
            print(f"[HOTKEY] Wake word hotkey detected: {wake_hotkey}")
            self.hotkey_pressed = True
            # Run in separate thread to avoid blocking
            threading.Thread(target=self.toggle_wake_word_mode, daemon=True).start()
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
            elif mode == 'wake_word':
                # In wake word mode, main hotkey toggles wake word listening
                self.hotkey_pressed = True
                self.toggle_wake_word_mode()
    
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
        
        # Reset hotkey flag when no hotkey combo is currently pressed
        # Use state-based checking for reliable detection
        main_pressed = self.check_hotkey_state(main_hotkey)
        cont_pressed = self.check_hotkey_state(cont_hotkey)
        wake_pressed = self.check_hotkey_state(wake_hotkey)
        
        if not main_pressed and not cont_pressed and not wake_pressed:
            if self.hotkey_pressed:
                if mode == 'hold' and self.recording:
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

        self.speech_buffer = []
        self.silence_start = None
        self.is_speaking = False

        self.continuous_stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype=np.float32,
            callback=self.continuous_audio_callback,
            device=self.config['microphone'],
            blocksize=int(self.sample_rate * 0.1)  # 100ms blocks
        )
        self.continuous_stream.start()
        self.continuous_active = True
        
        # Update tray icon
        if hasattr(self, 'tray_icon'):
            self.tray_icon.icon = self.create_icon_image(active=True)

    def stop_continuous_mode(self):
        """Stop continuous listening mode"""
        self.continuous_active = False
        
        if self.continuous_stream:
            self.continuous_stream.stop()
            self.continuous_stream.close()
            self.continuous_stream = None
        
        # Transcribe any remaining audio
        if self.speech_buffer:
            self.transcribe_buffer()
        
        print("[OFF] Continuous mode STOPPED")
        self.play_sound("stop")

        # Update tray icon
        if hasattr(self, 'tray_icon'):
            self.tray_icon.icon = self.create_icon_image(active=False)

    def continuous_audio_callback(self, indata, frames, time_info, status):
        """Callback for continuous listening - detects speech and silence"""
        if not self.continuous_active:
            return
        
        # Calculate RMS energy to detect speech
        audio_chunk = indata.copy().flatten()
        rms = np.sqrt(np.mean(audio_chunk**2))
        
        # Threshold for speech detection (adjust as needed)
        speech_threshold = 0.01
        silence_threshold = self.config.get('silence_threshold', 2.0)
        min_speech = self.config.get('min_speech_duration', 0.3)

        if rms > speech_threshold:
            # Speech detected
            self.is_speaking = True
            self.silence_start = None
            self.speech_buffer.append(audio_chunk)
        else:
            # Silence detected
            if self.is_speaking:
                # Still capture some silence at the end for context
                self.speech_buffer.append(audio_chunk)
                
                if self.silence_start is None:
                    self.silence_start = time.time()
                elif time.time() - self.silence_start >= silence_threshold:
                    # Enough silence - check if we have enough speech
                    speech_duration = len(self.speech_buffer) * 0.1  # Each block is 100ms
                    
                    if speech_duration >= min_speech:
                        # Transcribe in background
                        buffer_copy = self.speech_buffer.copy()
                        self.speech_buffer = []
                        self.is_speaking = False
                        self.silence_start = None
                        
                        thread = threading.Thread(
                            target=self.transcribe_continuous_buffer,
                            args=(buffer_copy,),
                            daemon=True
                        )
                        thread.start()
                    else:
                        # Not enough speech, discard
                        self.speech_buffer = []
                        self.is_speaking = False
                        self.silence_start = None

    def transcribe_continuous_buffer(self, buffer):
        """Transcribe a buffer from continuous mode"""
        try:
            audio = np.concatenate(buffer)
            audio_duration = len(audio) / self.sample_rate
            
            # Get transcription parameters based on performance mode
            transcribe_params = self.get_transcription_params()
            perf_mode = self.config.get('performance_mode', 'balanced')
            
            transcribe_start = time.time()
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
        if self.speech_buffer:
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

        self.speech_buffer = []
        self.silence_start = None
        self.is_speaking = False
        self.wake_word_triggered = False

        self.continuous_stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype=np.float32,
            callback=self.wake_word_audio_callback,
            device=self.config['microphone'],
            blocksize=int(self.sample_rate * 0.1)  # 100ms blocks
        )
        self.continuous_stream.start()
        self.wake_word_active = True

        # Update tray icon
        if hasattr(self, 'tray_icon'):
            self.tray_icon.icon = self.create_icon_image(active=True)
    
    def stop_wake_word_mode(self):
        """Stop wake word listening mode"""
        self.wake_word_active = False
        self.wake_word_triggered = False
        
        # Reset dictation mode
        self._reset_wake_dictation()
        
        if self.continuous_stream:
            self.continuous_stream.stop()
            self.continuous_stream.close()
            self.continuous_stream = None
        
        # Transcribe any remaining audio if wake word was triggered
        if self.speech_buffer and self.wake_word_triggered:
            self.transcribe_wake_word_buffer()
        
        self.speech_buffer = []
        
        print("[OFF] Wake word mode STOPPED")
        self.play_sound("stop")

        # Update tray icon
        if hasattr(self, 'tray_icon'):
            self.tray_icon.icon = self.create_icon_image(active=False)

    def wake_word_audio_callback(self, indata, frames, time_info, status):
        """Callback for wake word listening"""
        if not self.wake_word_active:
            return
        
        audio_chunk = indata.copy().flatten()
        rms = np.sqrt(np.mean(audio_chunk**2))
        
        # Get thresholds from config
        ww_config = self.config.get('wake_word_config', {})
        audio_config = ww_config.get('audio', {})
        speech_threshold = audio_config.get('speech_threshold', 0.01)
        min_speech = audio_config.get('min_speech_duration', 0.3)
        
        # Use dynamic silence timeout if in dictation mode, otherwise use fast wake detection
        if hasattr(self, '_dictation_silence_timeout') and self._dictation_silence_timeout:
            # In dictation mode - use mode-specific timeout
            silence_threshold = self._dictation_silence_timeout
        else:
            # Not in dictation mode - use longer threshold to capture natural speech
            # (people pause between wake word and command, e.g., "Saturn [pause] hello world")
            silence_threshold = audio_config.get('wake_detection_silence', 1.2)
        
        if rms > speech_threshold:
            # Speech detected
            self.is_speaking = True
            self.silence_start = None
            self.speech_buffer.append(audio_chunk)
        else:
            # Silence detected
            if self.is_speaking:
                self.speech_buffer.append(audio_chunk)
                
                if self.silence_start is None:
                    self.silence_start = time.time()
                elif time.time() - self.silence_start >= silence_threshold:
                    # Enough silence - check if we have enough speech
                    speech_duration = len(self.speech_buffer) * 0.1
                    
                    if speech_duration >= min_speech:
                        # Transcribe to check for wake word or command
                        buffer_copy = self.speech_buffer.copy()
                        self.speech_buffer = []
                        self.is_speaking = False
                        self.silence_start = None
                        
                        thread = threading.Thread(
                            target=self.process_wake_word_buffer,
                            args=(buffer_copy,),
                            daemon=True
                        )
                        thread.start()
                    else:
                        # Not enough speech, discard
                        self.speech_buffer = []
                        self.is_speaking = False
                        self.silence_start = None
    
    def process_wake_word_buffer(self, buffer):
        """Process audio - check for wake word, commands, or dictation content"""
        try:
            audio = np.concatenate(buffer)
            audio_duration = len(audio) / self.sample_rate
            
            # Get transcription parameters based on performance mode
            transcribe_params = self.get_transcription_params()
            perf_mode = self.config.get('performance_mode', 'balanced')
            
            transcribe_start = time.time()
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
            
            # Check for cancel word first (if in dictation mode)
            if self.wake_dictation_mode:
                cancel_config = ww_config.get('cancel_word', {})
                if cancel_config.get('enabled', False):
                    cancel_phrase = cancel_config.get('phrase', 'cancel').lower()
                    if cancel_phrase in text_lower:
                        print(f"[CANCEL] Dictation cancelled")
                        self.play_sound("error")
                        self._reset_wake_dictation()
                        return
                
                # Check for pause word
                pause_config = ww_config.get('pause_word', {})
                if pause_config.get('enabled', False):
                    pause_phrase = pause_config.get('phrase', 'pause').lower()
                    if pause_phrase in text_lower:
                        # Pause word detected - reset silence timer, don't accumulate
                        print(f"[PAUSE] Timer reset (pause word: '{pause_phrase}')")
                        self.silence_start = None
                        # Strip pause word and accumulate any remaining text
                        remaining = text_lower.replace(pause_phrase, '').strip()
                        if remaining:
                            # Get the non-lowered version with pause word removed
                            pause_idx = text_lower.find(pause_phrase)
                            cleaned = (text[:pause_idx] + text[pause_idx + len(pause_phrase):]).strip()
                            if cleaned:
                                self.wake_dictation_buffer.append(cleaned)
                                print(f"[DICTATE] Buffered (after pause strip): {cleaned}")
                        return

                # Check for end word
                end_config = ww_config.get('end_word', {})
                if end_config.get('enabled', False):
                    end_phrase = end_config.get('phrase', 'over').lower()
                    if end_phrase in text_lower:
                        # End word detected - finalize dictation
                        print(f"[END] End word detected: '{end_phrase}'")
                        # Remove end word from text
                        end_index = text_lower.rfind(end_phrase)
                        final_text = text[:end_index].strip()
                        
                        # Combine with any buffered audio text
                        if self.wake_dictation_buffer:
                            final_text = ' '.join(self.wake_dictation_buffer) + ' ' + final_text
                        
                        if final_text.strip():
                            self._output_dictation(final_text.strip())
                        
                        self._reset_wake_dictation()
                        return
                
                # In dictation mode, accumulate text
                self.wake_dictation_buffer.append(text)
                print(f"[DICTATE] Buffered: {text}")

                # For modes that don't require end word, start a finalization timer
                # If no new speech arrives within the timeout, output accumulated text
                if not self._dictation_require_end:
                    self._restart_dictation_timer()
                return
            
            # Not in dictation mode - check for wake word
            if wake_phrase in text_lower:
                # Wake word detected!
                print(f"[MIC] Wake word detected: '{wake_phrase}'")
                self.wake_word_triggered = True
                self.play_sound("start")
                
                # Extract command after wake word
                wake_index = text_lower.find(wake_phrase)
                command_text = text[wake_index + len(wake_phrase):].strip()
                
                # Filter out garbage (just punctuation, single chars)
                import re
                cleaned_cmd = re.sub(r'[^\w\s]', '', command_text).strip()
                has_meaningful_command = len(cleaned_cmd) >= 2
                
                if has_meaningful_command:
                    # There's a real command after the wake word
                    print(f"[TEXT] Command: {command_text}")
                    self._process_wake_command(command_text)
                else:
                    # Just wake word (or garbage like punctuation), waiting for next speech
                    if command_text:
                        print(f"[SKIP] Ignoring noise after wake word: '{command_text}'")
                    print("[LISTEN] Listening for command...")
                    self._start_wake_timeout()
                    
            elif self.wake_word_triggered:
                # Wake word was already said, this is the command
                print(f"[TEXT] Command: {text}")
                self._process_wake_command(text)
                
        except Exception as e:
            print(f"Wake word processing error: {e}")
            import traceback
            traceback.print_exc()
    
    def _process_wake_command(self, text):
        """Process command after wake word - check for dictation modes or execute"""
        text_lower = text.lower().strip()
        ww_config = self.config.get('wake_word_config', {})
        modes_config = ww_config.get('modes', {})
        
        # Check for dictation mode commands
        if text_lower in ['long dictate', 'long dictation']:
            self._start_dictation_mode('long_dictate', modes_config.get('long_dictate', {}))
            return
        elif text_lower in ['short dictate', 'short dictation', 'quick dictate']:
            self._start_dictation_mode('short_dictate', modes_config.get('short_dictate', {}))
            return
        elif text_lower in ['dictate', 'dictation']:
            self._start_dictation_mode('dictate', modes_config.get('dictate', {}))
            return
        
        # Check if text starts with dictation command and has content after
        for cmd, mode_name in [('long dictate', 'long_dictate'), ('short dictate', 'short_dictate'), ('dictate', 'dictate')]:
            if text_lower.startswith(cmd + ' '):
                # Start dictation mode with initial content
                mode_config = modes_config.get(mode_name, {})
                content = text[len(cmd):].strip()
                self._start_dictation_mode(mode_name, mode_config, initial_content=content)
                return
        
        # Not a dictation command - try regular command execution
        # Use force_commands=True to bypass command_mode_enabled check in wake word mode
        result, was_command = self.command_executor.process_text(text, self, force_commands=True)
        
        if was_command:
            self.wake_word_triggered = False
            return
        
        # Filter out garbage/noise (just punctuation, single chars, etc.)
        import re
        cleaned = re.sub(r'[^\w\s]', '', text).strip()  # Remove punctuation
        if len(cleaned) < 2:
            # Too short to be meaningful - probably just noise/hallucination
            print(f"[SKIP] Ignoring noise: '{text}'")
            # Keep listening for actual command
            self._start_wake_timeout()
            return
        
        # Not a recognized command - treat as simple dictation (immediate output)
        self._output_dictation(text)
        self.wake_word_triggered = False
    
    def _start_dictation_mode(self, mode_name, mode_config, initial_content=None):
        """Start a dictation mode (dictate, short_dictate, long_dictate)"""
        self.wake_dictation_mode = mode_name
        self.wake_dictation_buffer = []
        self.wake_dictation_start_time = time.time()
        self.wake_word_triggered = False

        # Cancel any existing timers
        if hasattr(self, 'wake_word_timer') and self.wake_word_timer:
            self.wake_word_timer.cancel()
        if hasattr(self, '_dictation_finalize_timer') and self._dictation_finalize_timer:
            self._dictation_finalize_timer.cancel()
            self._dictation_finalize_timer = None

        timeout = mode_config.get('silence_timeout', 0.6)
        require_end = mode_config.get('require_end_word', False)

        print(f"[DICTATE] Started {mode_name} mode (timeout: {timeout}s, require_end: {require_end})")
        self.play_sound("start")

        # Update silence threshold for this mode
        self._dictation_silence_timeout = timeout
        self._dictation_require_end = require_end

        if initial_content:
            self.wake_dictation_buffer.append(initial_content)
            print(f"[DICTATE] Initial content: {initial_content}")
            # Start finalization timer if this mode doesn't require end word
            if not require_end:
                self._restart_dictation_timer()
    
    def _reset_wake_dictation(self):
        """Reset dictation mode state"""
        self.wake_dictation_mode = None
        self.wake_dictation_buffer = []
        self.wake_dictation_start_time = None
        self.wake_word_triggered = False
        self._dictation_silence_timeout = None
        self._dictation_require_end = False

        if hasattr(self, 'wake_word_timer') and self.wake_word_timer:
            self.wake_word_timer.cancel()
            self.wake_word_timer = None

        if hasattr(self, '_dictation_finalize_timer') and self._dictation_finalize_timer:
            self._dictation_finalize_timer.cancel()
            self._dictation_finalize_timer = None

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
            if self.wake_dictation_mode and self.wake_dictation_buffer and not self._dictation_require_end:
                final_text = ' '.join(self.wake_dictation_buffer)
                print(f"[DONE] Dictation complete: {final_text}")
                self._output_dictation(final_text)
                self._reset_wake_dictation()
        except Exception as e:
            print(f"[ERROR] _finalize_dictation_timeout crashed: {e}")
            import traceback
            traceback.print_exc()

    def _paste_preserving_clipboard(self, text):
        """Paste text via clipboard while preserving the user's original clipboard content."""
        with _clipboard_lock:
            saved = _save_clipboard_win32()
            try:
                pyperclip.copy(text)
                time.sleep(0.05)
                pyautogui.hotkey('ctrl', 'v')

                # Wait for paste to complete before restoring
                # Use longer delay to ensure slow apps have time to read clipboard
                time.sleep(0.4)
            except Exception as e:
                print(f"[ERROR] Paste failed: {e}")
            finally:
                # Always restore clipboard, even if paste failed
                _restore_clipboard_win32(saved)

    def _output_dictation(self, text):
        """Output dictated text"""
        # Apply text processing (auto-capitalize, number formatting)
        text = self.process_transcription(text)

        if self.config['add_trailing_space']:
            text = text + " "

        print(f"[OK] {text}")
        self.play_sound("success")

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
        timeout = ww_config.get('audio', {}).get('wake_command_timeout', 5.0)
        self.wake_word_timer = threading.Timer(timeout, self.reset_wake_word)
        self.wake_word_timer.start()
    
    def reset_wake_word(self):
        """Reset wake word trigger after timeout"""
        try:
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
        if self.recording:
            self.audio_data.append(indata.copy())

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

        # Sound playback queue and worker thread
        self._sound_queue = queue.Queue()
        self._sound_worker = threading.Thread(target=self._sound_playback_worker, daemon=True)
        self._sound_worker.start()

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

    def _sound_playback_worker(self):
        """Background thread for sound playback using sd.play() for clean audio."""
        self._sound_worker_running = True
        self._sound_error_count = 0
        max_errors = 5
        
        print("[AUDIO] Sound worker thread started")

        while self._sound_worker_running:
            try:
                # Wait for sound with timeout for clean shutdown
                try:
                    sound_request = self._sound_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                if sound_request is None:  # Shutdown signal
                    break
                
                audio_array, volume = sound_request

                # Apply volume scaling
                scaled = (audio_array * volume).flatten()
                
                # Use sd.play() for clean, artifact-free playback
                # This handles buffering internally and avoids choppy audio
                sd.play(scaled, self._sound_stream_sr)
                sd.wait()  # Wait for playback to complete
                
                self._sound_queue.task_done()
                self._sound_error_count = 0  # Reset on success
                
            except Exception as e:
                print(f"[AUDIO] Playback error: {e}")
                self._sound_error_count += 1
                
                # If too many errors, try to recover
                if self._sound_error_count >= max_errors:
                    self._recover_audio_system()
                    self._sound_error_count = 0
        
        print("[AUDIO] Sound worker thread stopped")
    
    def _recover_audio_system(self):
        """Attempt to recover the audio system after multiple failures"""
        print("[AUDIO] Attempting audio system recovery...")
        
        try:
            # Stop any current playback
            try:
                sd.stop()
            except Exception:
                pass
            
            # Small delay to let audio system settle
            time.sleep(0.1)
            
            # Re-initialize sounddevice (query devices forces re-init)
            sd.query_devices()
            
            # Reload sound cache
            self._load_sound_cache()
            
            print("[AUDIO] Audio system recovery completed")
            
        except Exception as e:
            print(f"[AUDIO] Recovery failed: {e}")
    
    def _fallback_play(self, sound_type):
        """Fallback to winsound (Windows only) or system command"""
        sound_file = self.sound_files.get(sound_type)
        if sound_file is None or not sound_file.exists():
            return
        
        try:
            if HAS_WINSOUND:
                winsound.PlaySound(str(sound_file), winsound.SND_FILENAME | winsound.SND_ASYNC)
            elif sys.platform == 'darwin':  # macOS
                subprocess.Popen(['afplay', str(sound_file)], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif sys.platform.startswith('linux'):
                # Try aplay (ALSA) or paplay (PulseAudio)
                for player in ['paplay', 'aplay']:
                    try:
                        subprocess.Popen([player, str(sound_file)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        break
                    except FileNotFoundError:
                        continue
        except Exception as e:
            print(f"[AUDIO] Fallback playback also failed for {sound_type}: {e}")
    
    def reload_sounds(self):
        """Reload sounds from disk (call after changing sound files)"""
        print("[AUDIO] Reloading sounds...")
        self._load_sound_cache()

    def play_sound(self, sound_type, use_winsound=False):
        """Play audio feedback sound via persistent stream (non-blocking, low-latency).
        
        Args:
            sound_type: 'start', 'stop', 'success', or 'error'
            use_winsound: If True on Windows, use winsound directly (avoids sd.play/InputStream conflicts)
        """
        if not self.config.get('audio_feedback', True):
            return

        # On Windows, use winsound for start sounds to avoid conflict with InputStream
        # Note: winsound only supports WAV files
        if use_winsound and HAS_WINSOUND:
            sounds_dir = Path(__file__).parent / 'sounds'
            # Look for WAV file specifically (winsound doesn't support MP3)
            sound_path = sounds_dir / f'{sound_type}.wav'
            if sound_path.exists():
                try:
                    winsound.PlaySound(str(sound_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                    return
                except Exception as e:
                    print(f"[AUDIO] winsound failed: {e}")
                    # Fall through to regular playback
            # If no WAV but we have it cached (from MP3), use sounddevice queue instead
            # (may conflict with InputStream, but better than no sound)

        cached = self._sound_cache.get(sound_type)
        if cached is None:
            return

        volume = self.config.get('sound_volume', 0.5)
        
        # Clear queue to prevent sound backlog (only play latest)
        while not self._sound_queue.empty():
            try:
                self._sound_queue.get_nowait()
            except queue.Empty:
                break
        
        self._sound_queue.put((cached, volume))
    
    def stop_sound_worker(self):
        """Stop the sound worker thread (call on app shutdown)"""
        print("[AUDIO] Stopping sound worker...")
        self._sound_worker_running = False
        self._sound_queue.put(None)  # Shutdown signal
        if hasattr(self, '_sound_worker') and self._sound_worker.is_alive():
            self._sound_worker.join(timeout=2.0)
        # Stop any ongoing playback
        try:
            sd.stop()
        except Exception:
            pass

    def start_recording(self):
        """Start recording audio"""
        if not self.model_loaded:
            if self.loading_model:
                print("Model still loading, please wait...")
            else:
                print("Model not loaded!")
            return

        # Play start sound using winsound on Windows to avoid InputStream conflict
        self.play_sound("start", use_winsound=True)
        time.sleep(0.15)  # Brief pause for sound to start

        self.audio_data = []
        self.stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype=np.float32,
            callback=self.audio_callback,
            device=self.config['microphone']
        )
        self.stream.start()
        self.recording = True
        print("[REC] Recording...")

    def stop_recording(self):
        """Stop recording and transcribe"""
        if not self.recording:
            return

        self.recording = False
        self.play_sound("stop")
        
        if hasattr(self, 'stream'):
            self.stream.stop()
            self.stream.close()

        if not self.audio_data:
            print("No audio recorded")
            return
        
        print("[...] Transcribing...")
        
        # Combine audio chunks
        audio = np.concatenate(self.audio_data, axis=0).flatten()
        
        # Transcribe in background to not block hotkey listener
        def transcribe():
            try:
                audio_duration = len(audio) / self.sample_rate
                
                # Get transcription parameters based on performance mode
                transcribe_params = self.get_transcription_params()
                perf_mode = self.config.get('performance_mode', 'balanced')
                
                transcribe_start = time.time()
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
                        # Command was executed - add to history as command
                        self.add_to_history(text, is_command=True)
                        return

                    # Not a command, proceed with dictation
                    # Apply text processing (auto-capitalize, number formatting)
                    text = self.process_transcription(text)

                    if self.config['add_trailing_space']:
                        text = text + " "

                    print(f"[OK] {text}")
                    self.play_sound("success")

                    # Add to history
                    self.add_to_history(text.strip(), is_command=False)

                    if self.config['auto_paste']:
                        self._paste_preserving_clipboard(text)
                else:
                    print("No speech detected")

            except Exception as e:
                print(f"Transcription error: {e}")
                self.play_sound("error")
        
        thread = threading.Thread(target=transcribe, daemon=True)
        thread.start()

    def cancel_recording(self):
        """Cancel recording without transcribing"""
        if not self.recording:
            return

        self.recording = False
        print("[X] Recording cancelled")

        if hasattr(self, 'stream'):
            self.stream.stop()
            self.stream.close()

        # Clear audio data without transcribing
        self.audio_data = []
        self.play_sound("error")  # Play error sound to indicate cancellation

    def create_icon_image(self, active=False):
        """Create system tray icon - clean waveform design"""
        size = 64
        image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        
        # Color: teal when active, dark gray when idle
        color = '#00CED1' if active else '#555555'
        
        # Draw audio waveform bars
        bar_width = 4
        gap = 3
        heights = [15, 28, 42, 28, 15, 35, 20]  # Varied heights
        x = 8
        
        for h in heights:
            y_top = 32 - (h // 2)
            y_bottom = 32 + (h // 2)
            draw.rectangle([x, y_top, x + bar_width, y_bottom], fill=color)
            x += bar_width + gap
        
        return image
    
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

    def create_tray_icon(self):
        """Create and run system tray icon"""
        def get_menu():
            """Generate menu dynamically to reflect current state"""
            mode = self.config.get('mode', 'hold')
            
            # Create microphone submenu
            mic_menu_items = []
            current_mic_id = self.config.get('microphone')
            
            for mic in self.available_mics:
                mic_id = mic['id']
                mic_name = mic['name']
                is_current = (mic_id == current_mic_id)
                
                # Create menu item with checkmark for current mic
                mic_menu_items.append(
                    pystray.MenuItem(
                        f"{'*' if is_current else '   '}{mic_name}",
                        lambda _, mid=mic_id: self.switch_microphone_and_refresh(mid)
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
                    lambda: None,
                    enabled=False
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
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Settings", self.open_settings),
                pystray.MenuItem("History", self.open_history),
                pystray.MenuItem("Voice Training", self.open_voice_training),
                pystray.MenuItem("Wake Word Debug", self.open_wake_word_debug),
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
            f"Samsara - {self.get_current_microphone_name()}",
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

        # Stop sound worker thread
        try:
            self.stop_sound_worker()
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
