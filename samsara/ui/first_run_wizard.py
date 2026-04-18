"""
Samsara First Run Wizard

Step-by-step setup wizard for new users: microphone selection, model choice,
hotkey configuration, and wake word setup.
"""

import json
import tkinter as tk
from pathlib import Path

import numpy as np
import sounddevice as sd


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
            "command_hotkey": "ctrl+alt+c",
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
        self.command_hotkey_var = tk.StringVar(value="ctrl+alt+c")
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
            # Record at device native rate for 2 seconds
            duration = 2
            device_info = sd.query_devices(mic_id)
            native_rate = int(device_info['default_samplerate'])
            audio = sd.rec(int(native_rate * duration), samplerate=native_rate,
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
            ("command_hotkey", self.command_hotkey_var, "Command Only", "Hold to speak a command (no text output)"),
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
            elif self.capturing_hotkey == "command_hotkey":
                self.command_hotkey_var.set(hotkey)

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
            self.config['command_hotkey'] = self.command_hotkey_var.get()
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

