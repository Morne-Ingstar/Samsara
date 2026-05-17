"""Sounds settings tab.

Sections: Audio Feedback, Sound Theme, Smart Actions Earcons,
Custom Sound Files.
"""

import shutil
import tkinter as tk
import wave
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import numpy as np

# sounds_tab.py lives at samsara/ui/tabs/ — sounds are one level up at samsara/ui/sounds/
_UI_DIR = Path(__file__).parent.parent


class SoundsTab:
    """Sounds tab: audio feedback toggle, volume, theme picker, custom WAV files."""

    def __init__(self, parent_frame, app, settings_window):
        self.parent = parent_frame
        self.app    = app
        self.sw     = settings_window
        self._built = False

        # tk.Vars — set during build()
        self.audio_feedback_var = None
        self.sound_volume_var   = None
        self.sound_theme_var    = None

        # Widget refs
        self.volume_label  = None
        self.volume_slider = None
        self.theme_combo   = None
        self.sound_labels  = {}   # sound_type -> CTkLabel showing current filename

    # ------------------------------------------------------------------
    # Build (generator)
    # ------------------------------------------------------------------

    def build(self):
        sounds_scroll = ctk.CTkScrollableFrame(self.parent, fg_color="transparent")
        sounds_scroll.pack(fill='both', expand=True)

        # --- Audio Feedback ---
        ctk.CTkLabel(sounds_scroll, text="Audio Feedback",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(15, 10))

        feedback_frame = ctk.CTkFrame(sounds_scroll, corner_radius=10)
        feedback_frame.pack(fill='x', pady=(0, 20))

        self.audio_feedback_var = tk.BooleanVar(
            value=self.app.config.get('audio_feedback', True))
        ctk.CTkCheckBox(feedback_frame, text="Enable audio feedback sounds",
                        variable=self.audio_feedback_var
                        ).pack(anchor='w', padx=15, pady=(15, 10))

        volume_row = ctk.CTkFrame(feedback_frame, fg_color="transparent")
        volume_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(volume_row, text="Volume:", width=80, anchor='w').pack(side='left')

        self.sound_volume_var = tk.DoubleVar(
            value=self.app.config.get('sound_volume', 0.5))
        self.volume_slider = ctk.CTkSlider(
            volume_row, from_=0.0, to=1.0,
            variable=self.sound_volume_var, width=200,
            command=self.on_volume_change)
        self.volume_slider.pack(side='left', padx=(0, 10))

        self.volume_label = ctk.CTkLabel(
            volume_row,
            text=f"{int(self.sound_volume_var.get() * 100)}%",
            width=50)
        self.volume_label.pack(side='left')

        ctk.CTkButton(volume_row, text="Test", width=60,
                      command=lambda: self.app.play_sound('success')
                      ).pack(side='left', padx=(10, 0))
        yield

        # --- Sound Theme ---
        ctk.CTkLabel(sounds_scroll, text="Sound Theme",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        theme_frame = ctk.CTkFrame(sounds_scroll, corner_radius=10)
        theme_frame.pack(fill='x', pady=(0, 20))

        theme_row = ctk.CTkFrame(theme_frame, fg_color="transparent")
        theme_row.pack(fill='x', padx=15, pady=15)
        ctk.CTkLabel(theme_row, text="Theme:", width=80, anchor='w').pack(side='left')

        themes_dir = _UI_DIR / 'sounds' / 'themes'
        available_themes = ['cute', 'warm', 'zen', 'classic']
        if themes_dir.exists():
            available_themes = [
                d.name for d in themes_dir.iterdir()
                if d.is_dir() and (d / 'start.wav').exists()
            ]

        self.sound_theme_var = tk.StringVar(
            value=self.app.config.get('sound_theme', 'cute'))
        self.theme_combo = ctk.CTkComboBox(
            theme_row, variable=self.sound_theme_var,
            values=available_themes, width=150, state='readonly')
        self.theme_combo.pack(side='left', padx=(0, 10))

        ctk.CTkButton(theme_row, text="Apply Theme", width=100,
                      command=self.apply_sound_theme
                      ).pack(side='left', padx=(0, 10))

        ctk.CTkLabel(theme_frame,
                     text="cute = playful bloops  •  warm = OS boot vibes  •  zen = singing bowls  •  classic = original",
                     text_color="gray", font=ctk.CTkFont(size=11)
                     ).pack(anchor='w', padx=15, pady=(0, 15))

        # --- Smart Actions Earcons ---
        ctk.CTkLabel(sounds_scroll, text="Smart Actions Earcons",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 8))

        ctk.CTkLabel(sounds_scroll,
                     text="Preview the Smart Actions audio cues for the active theme.",
                     text_color="gray", font=ctk.CTkFont(size=11)
                     ).pack(anchor='w', pady=(0, 8))

        earcons_frame = ctk.CTkFrame(sounds_scroll, corner_radius=10)
        earcons_frame.pack(fill='x', pady=(0, 20))

        EARCON_PREVIEWS = [
            ('capture_started',  'Capture started'),
            ('capture_saved',    'Capture saved'),
            ('agent_routing',    'Agent routing'),
            ('agent_response',   'Agent response'),
            ('confirm_required', 'Confirm required'),
            ('action_complete',  'Action complete'),
            ('thinking_pulse',   'Thinking pulse'),
        ]

        grid = ctk.CTkFrame(earcons_frame, fg_color="transparent")
        grid.pack(fill='x', padx=15, pady=(15, 15))

        cols = 3
        for idx, (earcon_name, label_text) in enumerate(EARCON_PREVIEWS):
            row = idx // cols
            col = idx % cols
            cell = ctk.CTkFrame(grid, fg_color="transparent")
            cell.grid(row=row, column=col, sticky='ew', padx=(0, 10), pady=4)
            ctk.CTkLabel(cell, text=label_text, width=130, anchor='w').pack(side='left')
            ctk.CTkButton(cell, text="Test", width=60,
                          command=lambda n=earcon_name: self.app.play_sound(n)
                          ).pack(side='left')
        for c in range(cols):
            grid.grid_columnconfigure(c, weight=1)

        # --- Custom Sound Files ---
        ctk.CTkLabel(sounds_scroll, text="Custom Sound Files",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))
        ctk.CTkLabel(sounds_scroll, text="Replace default sounds with your own WAV files:",
                     text_color="gray"
                     ).pack(anchor='w', pady=(0, 10))

        sounds_frame = ctk.CTkFrame(sounds_scroll, corner_radius=10)
        sounds_frame.pack(fill='x', pady=(0, 20))
        yield

        # Sound file rows
        sound_types = [
            ('start',   'Recording start:'),
            ('stop',    'Recording stop:'),
            ('success', 'Transcription success:'),
            ('error',   'Error sound:'),
        ]

        for sound_type, label_text in sound_types:
            row = ctk.CTkFrame(sounds_frame, fg_color="transparent")
            row.pack(fill='x', padx=15,
                     pady=(10, 5) if sound_type == 'start' else (5, 5))

            ctk.CTkLabel(row, text=label_text, width=140, anchor='w').pack(side='left')

            sound_file = self.app.sound_files.get(sound_type)
            filename   = sound_file.name if sound_file and sound_file.exists() else "Not set"
            file_label = ctk.CTkLabel(row, text=filename, width=150,
                                      anchor='w', text_color="gray")
            file_label.pack(side='left', padx=(0, 10))
            self.sound_labels[sound_type] = file_label

            ctk.CTkButton(row, text="Play", width=60,
                          command=lambda st=sound_type: self.preview_sound(st)
                          ).pack(side='left', padx=(0, 5))
            ctk.CTkButton(row, text="Browse...", width=80,
                          command=lambda st=sound_type: self.browse_sound(st)
                          ).pack(side='left', padx=(0, 5))
            ctk.CTkButton(row, text="Reset", width=60, fg_color="gray40",
                          command=lambda st=sound_type: self.reset_sound(st)
                          ).pack(side='left')

        ctk.CTkLabel(sounds_frame, text="").pack(pady=5)

        ctk.CTkLabel(sounds_scroll,
                     text="Supported format: WAV files (44100 Hz recommended)",
                     text_color="gray"
                     ).pack(anchor='w', pady=(0, 5))

        sounds_folder = _UI_DIR / 'sounds'
        ctk.CTkLabel(sounds_scroll,
                     text=f"Sound files location: {sounds_folder}",
                     text_color="gray"
                     ).pack(anchor='w')

        self._built = True

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------

    def on_volume_change(self, value):
        volume = float(value)
        if self.volume_label:
            self.volume_label.configure(text=f"{int(volume * 100)}%")
        self.app.update_config({'sound_volume': volume}, save=False)

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def apply_sound_theme(self):
        theme      = self.sound_theme_var.get()
        themes_dir = _UI_DIR / 'sounds' / 'themes' / theme
        sounds_dir = _UI_DIR / 'sounds'

        if not themes_dir.exists():
            print(f"[WARN] Theme folder not found: {themes_dir}")
            return

        for wav in themes_dir.glob('*.wav'):
            shutil.copy2(wav, sounds_dir / wav.name)

        self.app.update_config({'sound_theme': theme})
        self.app._load_sound_cache()
        self.app.play_sound('success')
        print(f"[OK] Sound theme applied: {theme}")

    # ------------------------------------------------------------------
    # Custom sound file actions
    # ------------------------------------------------------------------

    def preview_sound(self, sound_type):
        self.app.play_sound(sound_type)

    def browse_sound(self, sound_type):
        filename = filedialog.askopenfilename(
            title=f"Select {sound_type} sound",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
            parent=self.sw.window,
        )
        if filename:
            dest = self.app.sounds_dir / f"{sound_type}.wav"
            try:
                shutil.copy(filename, dest)
                self.app._load_sound_cache()
                self.sound_labels[sound_type].configure(text=f"{sound_type}.wav")
                messagebox.showinfo(
                    "Sound Updated",
                    f"Sound file updated successfully!\n\nFile: {Path(filename).name}",
                    parent=self.sw.window)
            except Exception as e:
                messagebox.showerror(
                    "Error", f"Failed to copy sound file:\n{e}",
                    parent=self.sw.window)

    def reset_sound(self, sound_type):
        sound_file = self.app.sound_files.get(sound_type)
        if sound_file and sound_file.exists():
            sound_file.unlink()

        sample_rate = 44100

        def generate_tone(frequency, duration, volume=0.5):
            n_samples = int(sample_rate * duration)
            t = np.linspace(0, duration, n_samples, False)
            tone = np.sin(2 * np.pi * frequency * t) * volume
            fade_samples = min(int(sample_rate * 0.01), n_samples // 4)
            if fade_samples > 0:
                tone[:fade_samples]  *= np.linspace(0, 1, fade_samples)
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
            save_wav(sound_file, generate_tone(660, 0.12, volume=0.6))
        elif sound_type == 'stop':
            save_wav(sound_file, generate_tone(440, 0.10, volume=0.5))
        elif sound_type == 'success':
            gap   = np.zeros(int(sample_rate * 0.02))
            audio = np.concatenate([
                generate_tone(523, 0.08, 0.5), gap,
                generate_tone(659, 0.08, 0.5), gap,
                generate_tone(784, 0.12, 0.5),
            ])
            save_wav(sound_file, audio)
        elif sound_type == 'error':
            gap   = np.zeros(int(sample_rate * 0.08))
            audio = np.concatenate([
                generate_tone(220, 0.15, 0.5), gap,
                generate_tone(196, 0.18, 0.5),
            ])
            save_wav(sound_file, audio)

        self.app._load_sound_cache()
        self.sound_labels[sound_type].configure(text=f"{sound_type}.wav")
        messagebox.showinfo(
            "Sound Reset",
            f"'{sound_type}' sound reset to default.",
            parent=self.sw.window)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self):
        if not self._built:
            return
        self.app.update_config({
            'audio_feedback': self.audio_feedback_var.get(),
            'sound_volume':   self.sound_volume_var.get(),
        }, save=False)
