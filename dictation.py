import os
import ctypes
import winsound

# Hide console window IMMEDIATELY before any output
def _hide_console_now():
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except:
        pass

_hide_console_now()

# Fix OpenMP conflict between numpy and other libraries
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import threading
import queue
import sys
import time
import subprocess
import logging
from datetime import datetime
import numpy as np
import sounddevice as sd
from pynput import keyboard
from pynput.keyboard import Key, Controller as KeyboardController
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


def hide_console():
    """Hide the console window on Windows"""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except:
        pass


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
                # Launch application
                target = cmd['target']
                # Use 'start' command on Windows for better compatibility
                subprocess.Popen(f'start "" "{target}"', shell=True)
                print(f"[OK] Launching: {target}")
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
    
    def process_text(self, text, app_instance=None):
        """Process transcribed text - check for command or return text for dictation"""
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
        
        # Check if command mode is enabled
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

        # Make it appear on top and grab focus
        self.window.lift()
        self.window.focus_force()
        self.window.after(100, lambda: self.window.lift())

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

        # Microphone Section
        mic_label = ctk.CTkLabel(general_tab, text="Microphone", font=ctk.CTkFont(size=16, weight="bold"))
        mic_label.pack(anchor='w', pady=(15, 10))

        mic_frame = ctk.CTkFrame(general_tab, corner_radius=10)
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
        options_label = ctk.CTkLabel(general_tab, text="Basic Options", font=ctk.CTkFont(size=16, weight="bold"))
        options_label.pack(anchor='w', pady=(0, 10))

        options_frame = ctk.CTkFrame(general_tab, corner_radius=10)
        options_frame.pack(fill='x', pady=(0, 20))

        self.auto_paste_var = tk.BooleanVar(value=self.app.config.get('auto_paste', True))
        ctk.CTkCheckBox(options_frame, text="Automatically paste transcribed text",
                       variable=self.auto_paste_var).pack(anchor='w', padx=15, pady=(15, 8))

        self.trailing_space_var = tk.BooleanVar(value=self.app.config.get('add_trailing_space', True))
        ctk.CTkCheckBox(options_frame, text="Add trailing space after text",
                       variable=self.trailing_space_var).pack(anchor='w', padx=15, pady=(0, 8))

        self.command_mode_var = tk.BooleanVar(value=self.app.config.get('command_mode_enabled', True))
        ctk.CTkCheckBox(options_frame, text="Enable voice commands (recommended)",
                       variable=self.command_mode_var).pack(anchor='w', padx=15, pady=(0, 15))

        # Model Section
        model_label = ctk.CTkLabel(general_tab, text="AI Model", font=ctk.CTkFont(size=16, weight="bold"))
        model_label.pack(anchor='w', pady=(0, 10))

        model_frame = ctk.CTkFrame(general_tab, corner_radius=10)
        model_frame.pack(fill='x')

        ctk.CTkLabel(model_frame, text="Whisper model size:").pack(anchor='w', padx=15, pady=(15, 5))

        self.model_var = tk.StringVar(value=self.app.config.get('model_size', 'base'))
        model_combo = ctk.CTkComboBox(model_frame, variable=self.model_var,
                                      values=['tiny', 'base', 'small', 'medium', 'large-v3'],
                                      width=200, state='readonly')
        model_combo.pack(anchor='w', padx=15, pady=(0, 5))

        ctk.CTkLabel(model_frame, text="tiny: Fastest  |  base: Recommended  |  large-v3: Most accurate",
                    text_color="gray").pack(anchor='w', padx=15, pady=(0, 5))
        ctk.CTkLabel(model_frame, text="Model changes require restart",
                    text_color="#1f6aa5").pack(anchor='w', padx=15, pady=(0, 15))

        # === HOTKEYS & MODES TAB ===
        hotkey_tab = self.tabview.tab("Hotkeys & Modes")

        # Recording Mode Section
        mode_label = ctk.CTkLabel(hotkey_tab, text="Recording Mode", font=ctk.CTkFont(size=16, weight="bold"))
        mode_label.pack(anchor='w', pady=(15, 10))

        mode_frame = ctk.CTkFrame(hotkey_tab, corner_radius=10)
        mode_frame.pack(fill='x', pady=(0, 20))

        self.mode_var = tk.StringVar(value=self.app.config.get('mode', 'hold'))

        ctk.CTkRadioButton(mode_frame, text="Hold to record (hold key, release to transcribe)",
                          variable=self.mode_var, value='hold').pack(anchor='w', padx=15, pady=(15, 8))
        ctk.CTkRadioButton(mode_frame, text="Toggle mode (press to start/stop recording)",
                          variable=self.mode_var, value='toggle').pack(anchor='w', padx=15, pady=(0, 8))
        ctk.CTkRadioButton(mode_frame, text="Continuous (auto-transcribe on speech pause)",
                          variable=self.mode_var, value='continuous').pack(anchor='w', padx=15, pady=(0, 8))
        ctk.CTkRadioButton(mode_frame, text="Wake word (hands-free activation)",
                          variable=self.mode_var, value='wake_word').pack(anchor='w', padx=15, pady=(0, 15))

        # Keyboard Shortcuts Section
        hotkey_label = ctk.CTkLabel(hotkey_tab, text="Keyboard Shortcuts", font=ctk.CTkFont(size=16, weight="bold"))
        hotkey_label.pack(anchor='w', pady=(0, 10))

        hotkey_frame = ctk.CTkFrame(hotkey_tab, corner_radius=10)
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
        row3.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(row3, text="Toggle wake word:", width=150, anchor='w').pack(side='left')
        self.wake_hotkey_var = tk.StringVar(value=self.app.config.get('wake_word_hotkey', 'ctrl+alt+w'))
        self.wake_hotkey_entry = ctk.CTkEntry(row3, textvariable=self.wake_hotkey_var, width=180, state='disabled')
        self.wake_hotkey_entry.pack(side='left', padx=(0, 10))
        self.wake_hotkey_btn = ctk.CTkButton(row3, text="Change", width=80,
                                             command=lambda: self.start_capture('wake_word_hotkey'))
        self.wake_hotkey_btn.pack(side='left')
        self.hotkey_buttons['wake_word_hotkey'] = self.wake_hotkey_btn

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
        style.configure("Commands.Treeview",
                       background="#2b2b2b",
                       foreground="white",
                       fieldbackground="#2b2b2b",
                       rowheight=28)
        style.configure("Commands.Treeview.Heading",
                       background="#1f6aa5",
                       foreground="white",
                       font=('Segoe UI', 10, 'bold'))
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

        # Audio Feedback Toggle
        feedback_label = ctk.CTkLabel(sounds_tab, text="Audio Feedback", font=ctk.CTkFont(size=16, weight="bold"))
        feedback_label.pack(anchor='w', pady=(15, 10))

        feedback_frame = ctk.CTkFrame(sounds_tab, corner_radius=10)
        feedback_frame.pack(fill='x', pady=(0, 20))

        self.audio_feedback_var = tk.BooleanVar(value=self.app.config.get('audio_feedback', True))
        ctk.CTkCheckBox(feedback_frame, text="Enable audio feedback sounds",
                       variable=self.audio_feedback_var).pack(anchor='w', padx=15, pady=15)

        # Custom Sounds Section
        sounds_label = ctk.CTkLabel(sounds_tab, text="Custom Sound Files", font=ctk.CTkFont(size=16, weight="bold"))
        sounds_label.pack(anchor='w', pady=(0, 10))

        ctk.CTkLabel(sounds_tab, text="Replace default sounds with your own WAV files:",
                    text_color="gray").pack(anchor='w', pady=(0, 10))

        sounds_frame = ctk.CTkFrame(sounds_tab, corner_radius=10)
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
        ctk.CTkLabel(sounds_tab, text="Supported format: WAV files (44100 Hz recommended)",
                    text_color="gray").pack(anchor='w', pady=(0, 5))

        sounds_folder = Path(__file__).parent / 'sounds'
        ctk.CTkLabel(sounds_tab, text=f"Sound files location: {sounds_folder}",
                    text_color="gray").pack(anchor='w')

        # === ADVANCED TAB ===
        advanced_tab = self.tabview.tab("Advanced")

        # Continuous Mode Settings
        cont_label = ctk.CTkLabel(advanced_tab, text="Continuous Mode Settings", font=ctk.CTkFont(size=16, weight="bold"))
        cont_label.pack(anchor='w', pady=(15, 10))

        cont_frame = ctk.CTkFrame(advanced_tab, corner_radius=10)
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

        # Wake Word Settings
        wake_label = ctk.CTkLabel(advanced_tab, text="Wake Word Settings", font=ctk.CTkFont(size=16, weight="bold"))
        wake_label.pack(anchor='w', pady=(0, 10))

        wake_frame = ctk.CTkFrame(advanced_tab, corner_radius=10)
        wake_frame.pack(fill='x')

        # Wake word phrase
        wake_word_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        wake_word_row.pack(fill='x', padx=15, pady=(15, 8))
        ctk.CTkLabel(wake_word_row, text="Wake word phrase:", width=150, anchor='w').pack(side='left')
        self.wake_word_var = tk.StringVar(value=self.app.config.get('wake_word', 'samsara'))
        wake_word_entry = ctk.CTkEntry(wake_word_row, textvariable=self.wake_word_var, width=150)
        wake_word_entry.pack(side='left')

        # Command timeout
        timeout_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        timeout_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(timeout_row, text="Command timeout:", width=150, anchor='w').pack(side='left')
        self.wake_timeout_var = tk.DoubleVar(value=self.app.config.get('wake_word_timeout', 5.0))
        wake_timeout_entry = ctk.CTkEntry(timeout_row, textvariable=self.wake_timeout_var, width=80)
        wake_timeout_entry.pack(side='left', padx=(0, 10))
        ctk.CTkLabel(timeout_row, text="seconds").pack(side='left')

        self.window.protocol("WM_DELETE_WINDOW", self.close)

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

        self.window.unbind('<KeyPress>')
        self.window.unbind('<KeyRelease>')
        self.capturing_hotkey = None
        self.captured_keys = set()

    def save_settings(self):
        old_model = self.app.config.get('model_size', 'base')
        new_model = self.model_var.get()
        model_changed = old_model != new_model

        self.app.config['mode'] = self.mode_var.get()
        self.app.config['hotkey'] = self.hotkey_var.get()
        self.app.config['continuous_hotkey'] = self.cont_hotkey_var.get()
        self.app.config['wake_word_hotkey'] = self.wake_hotkey_var.get()
        self.app.config['silence_threshold'] = self.silence_var.get()
        self.app.config['min_speech_duration'] = self.min_speech_var.get()
        self.app.config['wake_word'] = self.wake_word_var.get()
        self.app.config['wake_word_timeout'] = self.wake_timeout_var.get()
        self.app.config['auto_paste'] = self.auto_paste_var.get()
        self.app.config['add_trailing_space'] = self.trailing_space_var.get()
        self.app.config['model_size'] = new_model
        self.app.config['command_mode_enabled'] = self.command_mode_var.get()
        self.app.config['show_all_audio_devices'] = self.show_all_devices_var.get()
        self.app.config['audio_feedback'] = self.audio_feedback_var.get()

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

    def close(self):
        if self.window:
            try:
                self.window.destroy()
            except:
                pass
            finally:
                self.window = None

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
                                     values=['hotkey', 'launch', 'press', 'key_down', 'key_up', 'mouse', 'release_all'])
        type_combo.pack(anchor='w', padx=20)

        # Dynamic fields frame
        fields_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        fields_frame.pack(fill='x', padx=20, pady=(15, 0))

        # Variables for different field types
        keys_var = tk.StringVar(value='+'.join(existing_data.get('keys', [])))
        target_var = tk.StringVar(value=existing_data.get('target', ''))
        key_var = tk.StringVar(value=existing_data.get('key', ''))
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
        
        # Settings window
        self.settings_window = SettingsWindow(self)

        # Voice Training window
        self.voice_training_window = VoiceTrainingWindow(self)

        self.update_splash("Setting up keyboard...")

        # Start keyboard listener
        self.keyboard_listener = keyboard.Listener(
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
        print(f"Using model: {self.config['model_size']}")

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
            "mode": "hold",
            "model_size": "base",
            "language": "en",
            "auto_paste": True,
            "add_trailing_space": True,
            "device": "auto",
            "microphone": None,
            "silence_threshold": 2.0,
            "min_speech_duration": 0.3,
            "command_mode_enabled": False,  # Start with command mode OFF by default
            "wake_word": "hey claude",
            "wake_word_timeout": 5.0,
            "show_all_audio_devices": False,
            "audio_feedback": True,  # Play sounds for recording start/stop
            "first_run_complete": True  # Track if first-run wizard has been completed (default True for existing installs)
        }
        
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r') as f:
                    self.config = json.load(f)
                for key in default_config:
                    if key not in self.config:
                        self.config[key] = default_config[key]
            except:
                self.config = default_config
        else:
            self.config = default_config
            self.save_config()
    
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
            
            # Determine compute device
            device = self.config['device']
            if device == "auto":
                try:
                    import torch
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                except:
                    device = "cpu"
            
            compute_type = "float16" if device == "cuda" else "int8"
            
            self.model = WhisperModel(
                self.config['model_size'],
                device=device,
                compute_type=compute_type
            )
            self.model_loaded = True
            self.loading_model = False
            print(f"Model loaded! ({device})")
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
        """Get keys pressed within the hotkey window"""
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

    def on_key_press(self, key):
        """Handle key press"""
        key_name = self.get_key_name(key)
        if key_name:
            self.current_keys.add(key_name)
            self.key_press_times[key_name] = time.time()

        mode = self.config.get('mode', 'hold')
        required_keys = self.parse_hotkey(self.config['hotkey'])
        cont_keys = self.parse_hotkey(self.config.get('continuous_hotkey', 'ctrl+alt+d'))
        wake_keys = self.parse_hotkey(self.config.get('wake_word_hotkey', 'ctrl+alt+w'))

        # Use time-window based key detection for more reliable hotkey matching
        active_keys = self.get_active_keys()

        # Check for wake word mode toggle (works in any mode)
        if wake_keys.issubset(active_keys) and not self.hotkey_pressed:
            self.hotkey_pressed = True
            # Run in separate thread to avoid blocking
            threading.Thread(target=self.toggle_wake_word_mode, daemon=True).start()
            return
        
        # Check for continuous mode toggle (works in any mode)
        if cont_keys.issubset(active_keys) and not self.hotkey_pressed:
            self.hotkey_pressed = True
            self.toggle_continuous_mode()
            return

        # Handle main hotkey based on mode
        if required_keys.issubset(active_keys) and not self.hotkey_pressed:
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
        """Handle key release"""
        key_name = self.get_key_name(key)
        if key_name and key_name in self.current_keys:
            self.current_keys.discard(key_name)
        
        mode = self.config.get('mode', 'hold')
        required_keys = self.parse_hotkey(self.config['hotkey'])
        cont_keys = self.parse_hotkey(self.config.get('continuous_hotkey', 'ctrl+alt+d'))
        wake_keys = self.parse_hotkey(self.config.get('wake_word_hotkey', 'ctrl+alt+w'))
        
        # Reset hotkey flag when keys released
        if not required_keys.issubset(self.current_keys) and not cont_keys.issubset(self.current_keys) and not wake_keys.issubset(self.current_keys):
            if self.hotkey_pressed:
                if mode == 'hold' and self.recording:
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

        # Play sound immediately while stream starts
        self.play_sound("start")
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
            
            segments, info = self.model.transcribe(
                audio,
                language=self.config['language'],
                beam_size=5,
                vad_filter=True,
                initial_prompt=self.voice_training_window.get_initial_prompt()
            )
            
            text = "".join([segment.text for segment in segments]).strip()
            
            # Apply corrections dictionary
            text = self.voice_training_window.apply_corrections(text)
            
            if text:
                # Check for command mode toggle OR regular commands
                result, was_command = self.command_executor.process_text(text, self)
                
                if was_command:
                    # Command was executed
                    return
                
                # Not a command, proceed with dictation
                if self.config['add_trailing_space']:
                    text = text + " "
                
                print(f"[TEXT] {text}")
                
                if self.config['auto_paste']:
                    pyperclip.copy(text)
                    time.sleep(0.05)
                    pyautogui.hotkey('ctrl', 'v')
                    
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

        # Play sound immediately while stream starts
        self.play_sound("start")
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
        
        speech_threshold = 0.01
        silence_threshold = self.config.get('silence_threshold', 2.0)
        min_speech = self.config.get('min_speech_duration', 0.5)
        
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
        """Process audio - check for wake word or execute command"""
        try:
            audio = np.concatenate(buffer)
            
            segments, info = self.model.transcribe(
                audio,
                language=self.config['language'],
                beam_size=5,
                vad_filter=True,
                initial_prompt=self.voice_training_window.get_initial_prompt()
            )
            
            text = "".join([segment.text for segment in segments]).strip()
            
            # Apply corrections dictionary
            text = self.voice_training_window.apply_corrections(text).lower()
            
            if not text:
                return
            
            wake_word = self.config.get('wake_word', 'hey claude').lower()
            
            # Check if wake word is in the text
            if wake_word in text:
                # Wake word detected!
                print(f"[MIC] Wake word detected: '{wake_word}'")
                self.wake_word_triggered = True
                
                # Extract command after wake word
                wake_word_index = text.find(wake_word)
                command_text = text[wake_word_index + len(wake_word):].strip()
                
                if command_text:
                    # There's a command after the wake word
                    print(f"[TEXT] Command: {command_text}")
                    self.execute_wake_word_command(command_text)
                else:
                    # Just wake word, waiting for next speech
                    print("[LISTEN] Listening for command...")
                    # Set a timer to reset if no command comes
                    self.wake_word_timer = threading.Timer(
                        self.config.get('wake_word_timeout', 5.0),
                        self.reset_wake_word
                    )
                    self.wake_word_timer.start()
            elif self.wake_word_triggered:
                # Wake word was already said, this is the command
                print(f"[TEXT] Command: {text}")
                self.execute_wake_word_command(text)
                self.wake_word_triggered = False
                
        except Exception as e:
            print(f"Wake word processing error: {e}")
    
    def execute_wake_word_command(self, text):
        """Execute command from wake word input"""
        # Check for command mode toggle OR regular commands
        result, was_command = self.command_executor.process_text(text, self)
        
        if was_command:
            # Command was executed
            self.wake_word_triggered = False
            return
        
        # Not a command, proceed with dictation
        if self.config['add_trailing_space']:
            text = text + " "
        
        print(f"[OK] {text}")
        
        if self.config['auto_paste']:
            pyperclip.copy(text)
            time.sleep(0.05)
            pyautogui.hotkey('ctrl', 'v')
        
        self.wake_word_triggered = False
    
    def reset_wake_word(self):
        """Reset wake word trigger after timeout"""
        if self.wake_word_triggered:
            print("[TIMEOUT] Wake word timeout - say wake word again")
            self.wake_word_triggered = False

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

    def play_sound(self, sound_type):
        """Play audio feedback sounds using WAV files (non-blocking)"""
        if not self.config.get('audio_feedback', True):
            return

        sound_file = self.sound_files.get(sound_type)
        if sound_file is None or not sound_file.exists():
            return

        def _play():
            try:
                winsound.PlaySound(str(sound_file), winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass  # Silently ignore audio errors

        # Play in background thread
        threading.Thread(target=_play, daemon=True).start()

    def start_recording(self):
        """Start recording audio"""
        if not self.model_loaded:
            if self.loading_model:
                print("Model still loading, please wait...")
            else:
                print("Model not loaded!")
            return

        # Play sound immediately (in background) - user hears feedback while stream starts
        self.play_sound("start")

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
                segments, info = self.model.transcribe(
                    audio,
                    language=self.config['language'],
                    beam_size=5,
                    vad_filter=True,
                    initial_prompt=self.voice_training_window.get_initial_prompt()
                )
                
                text = "".join([segment.text for segment in segments]).strip()
                
                # Apply corrections dictionary
                text = self.voice_training_window.apply_corrections(text)
                
                if text:
                    # Check for command mode toggle OR regular commands
                    result, was_command = self.command_executor.process_text(text, self)
                    
                    if was_command:
                        # Command was executed
                        return
                    
                    # Not a command, proceed with dictation
                    if self.config['add_trailing_space']:
                        text = text + " "
                    
                    print(f"[OK] {text}")
                    self.play_sound("success")

                    if self.config['auto_paste']:
                        pyperclip.copy(text)
                        time.sleep(0.05)
                        pyautogui.hotkey('ctrl', 'v')
                else:
                    print("No speech detected")

            except Exception as e:
                print(f"Transcription error: {e}")
                self.play_sound("error")
        
        thread = threading.Thread(target=transcribe, daemon=True)
        thread.start()

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
                pystray.MenuItem("Voice Training", self.open_voice_training),
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
        os.startfile(self.config_path.parent)

    def open_main_log(self):
        """Open the main log file in default text editor"""
        log_file = LOG_FILE
        if log_file.exists():
            os.startfile(log_file)
        else:
            messagebox.showinfo("Log File", "No log file found yet.")

    def open_voice_training_log(self):
        """Open the voice training log file"""
        log_file = LOG_DIR / 'voice_training.log'
        if log_file.exists():
            os.startfile(log_file)
        else:
            messagebox.showinfo("Log File", "No voice training log file found yet.")
    
    def quit_app(self):
        """Exit the application"""
        if self.continuous_active:
            self.stop_continuous_mode()
        self.keyboard_listener.stop()
        self.tray_icon.stop()
        sys.exit(0)

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
