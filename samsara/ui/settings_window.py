"""
Samsara Settings Window

Full settings UI with tabbed interface for configuring all app options.
Extracted from dictation.py to reduce monolith size.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

import customtkinter as ctk
import numpy as np

from samsara.profiles import ProfileManager
from samsara.ui.profile_manager_ui import ProfileManagerWindow
from samsara.alarms import get_default_alarm_config


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

        # Use cached mic list for instant open; refresh in background
        self.available_mics = self.app.available_mics or []

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
        self.tabview = ctk.CTkTabview(self.window, corner_radius=10, command=self.on_tab_changed)
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
        self.tabview.add("Alarms")
        self.tabview.add("Advanced")

        # Initialize lazy loading tracking
        self.built_tabs = {"General"}

        # Build only the General tab initially
        self.build_general_tab()

        self.window.protocol("WM_DELETE_WINDOW", self.close)

        # Show the fully-built window and bring to front
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self.window.after(100, lambda: self.window.lift())

        # Refresh mic list in background so the window opens instantly
        def _refresh_mics():
            fresh = self.app.get_available_microphones()
            if self.window:
                self.window.after(0, self._apply_mic_refresh, fresh)
        threading.Thread(target=_refresh_mics, daemon=True).start()

    def _apply_mic_refresh(self, fresh_mics):
        """Update the mic combobox with a freshly-enumerated mic list."""
        self.available_mics = fresh_mics
        self.app.available_mics = fresh_mics
        if hasattr(self, 'mic_combo') and self.mic_combo:
            try:
                names = [m['name'] for m in fresh_mics]
                self.mic_combo.configure(values=names)
            except Exception:
                pass

    def on_tab_changed(self):
        """Handle tab changes to implement lazy loading."""
        current_tab = self.tabview.get()
        if current_tab not in self.built_tabs:
            build_methods = {
                "Hotkeys & Modes": self.build_hotkeys_modes_tab,
                "Commands": self.build_commands_tab,
                "Sounds": self.build_sounds_tab,
                "Alarms": self.build_alarms_tab,
                "Advanced": self.build_advanced_tab,
            }
            builder = build_methods.get(current_tab)
            if builder:
                builder()
                self.built_tabs.add(current_tab)

    def build_general_tab(self):
        """Build the General settings tab."""
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

        # Listening Indicator Section
        indicator_label = ctk.CTkLabel(general_scroll, text="Listening Indicator",
                                       font=ctk.CTkFont(size=16, weight="bold"))
        indicator_label.pack(anchor='w', pady=(0, 10))

        indicator_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        indicator_frame.pack(fill='x', pady=(0, 20))

        ctk.CTkLabel(indicator_frame,
                     text="An always-on-top pill that shows your current mode and pulses while recording",
                     text_color="gray").pack(anchor='w', padx=15, pady=(15, 10))

        self.indicator_enabled_var = tk.BooleanVar(
            value=self.app.config.get('listening_indicator_enabled', False))
        ctk.CTkCheckBox(indicator_frame, text="Show listening indicator overlay",
                        variable=self.indicator_enabled_var).pack(anchor='w', padx=15, pady=(0, 10))

        pos_row = ctk.CTkFrame(indicator_frame, fg_color="transparent")
        pos_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(pos_row, text="Position:", width=70, anchor='w').pack(side='left')
        self.indicator_pos_var = tk.StringVar(
            value=self.app.config.get('listening_indicator_position', 'bottom-center'))
        pos_combo = ctk.CTkComboBox(pos_row, variable=self.indicator_pos_var,
                                     values=["top-left", "top-center", "top-right",
                                             "bottom-left", "bottom-center", "bottom-right"],
                                     width=160, state='readonly')
        pos_combo.pack(side='left', padx=(0, 10))

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

    def build_hotkeys_modes_tab(self):
        """Build the Hotkeys & Modes settings tab."""
        hotkey_tab = self.tabview.tab("Hotkeys & Modes")
        
        # Create scrollable frame for Hotkeys tab content
        hotkey_scroll = ctk.CTkScrollableFrame(hotkey_tab, fg_color="transparent")
        hotkey_scroll.pack(fill='both', expand=True)

        # Recording Mode Section
        mode_label = ctk.CTkLabel(hotkey_scroll, text="Recording Mode", font=ctk.CTkFont(size=16, weight="bold"))
        mode_label.pack(anchor='w', pady=(15, 10))

        mode_frame = ctk.CTkFrame(hotkey_scroll, corner_radius=10)
        mode_frame.pack(fill='x', pady=(0, 20))

        current_mode = self.app.config.get('mode', 'hold')
        self.mode_var = tk.StringVar(value=current_mode)

        ctk.CTkRadioButton(mode_frame, text="Hold to record (hold key, release to transcribe)",
                          variable=self.mode_var, value='hold').pack(anchor='w', padx=15, pady=(15, 8))
        ctk.CTkRadioButton(mode_frame, text="Toggle mode (press to start/stop recording)",
                          variable=self.mode_var, value='toggle').pack(anchor='w', padx=15, pady=(0, 8))
        ctk.CTkRadioButton(mode_frame, text="Continuous (auto-transcribe on speech pause)",
                          variable=self.mode_var, value='continuous').pack(anchor='w', padx=15, pady=(0, 15))

        # Wake word is a separate checkbox (works alongside any capture mode)
        self.wake_word_enabled_var = tk.BooleanVar(
            value=self.app.config.get('wake_word_enabled', False))
        ctk.CTkCheckBox(mode_frame, text="Enable wake word listener (works with any mode above)",
                        variable=self.wake_word_enabled_var).pack(anchor='w', padx=15, pady=(0, 15))

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

        # Command-only hotkey
        row3b = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row3b.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(row3b, text="Command only:", width=150, anchor='w').pack(side='left')
        self.cmd_hotkey_var = tk.StringVar(value=self.app.config.get('command_hotkey', 'ctrl+alt+c'))
        self.cmd_hotkey_entry = ctk.CTkEntry(row3b, textvariable=self.cmd_hotkey_var, width=180, state='disabled')
        self.cmd_hotkey_entry.pack(side='left', padx=(0, 10))
        self.cmd_hotkey_btn = ctk.CTkButton(row3b, text="Change", width=80,
                                             command=lambda: self.start_capture('command_hotkey'))
        self.cmd_hotkey_btn.pack(side='left')
        self.hotkey_buttons['command_hotkey'] = self.cmd_hotkey_btn

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

    def build_commands_tab(self):
        """Build the Commands settings tab."""
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

    def build_sounds_tab(self):
        """Build the Sounds settings tab."""
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

    def build_alarms_tab(self):
        """Build the Alarms settings tab."""
        alarms_tab = self.tabview.tab("Alarms")
        
        # Create scrollable frame for Alarms tab content
        alarms_scroll = ctk.CTkScrollableFrame(alarms_tab, fg_color="transparent")
        alarms_scroll.pack(fill='both', expand=True)

        # Global Settings Section
        alarm_settings_label = ctk.CTkLabel(alarms_scroll, text="Alarm Settings", font=ctk.CTkFont(size=16, weight="bold"))
        alarm_settings_label.pack(anchor='w', pady=(15, 10))

        alarm_settings_frame = ctk.CTkFrame(alarms_scroll, corner_radius=10)
        alarm_settings_frame.pack(fill='x', pady=(0, 20))

        # Get current alarm config
        alarm_config = self.app.config.get('alarms', get_default_alarm_config())

        # Enable alarms toggle
        self.alarms_enabled_var = tk.BooleanVar(value=alarm_config.get('enabled', True))
        ctk.CTkCheckBox(alarm_settings_frame, text="Enable alarm reminders",
                       variable=self.alarms_enabled_var).pack(anchor='w', padx=15, pady=(15, 10))

        # Complete hotkey row (user completed the task, gets streak credit)
        complete_row = ctk.CTkFrame(alarm_settings_frame, fg_color="transparent")
        complete_row.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(complete_row, text="Complete hotkey:", width=120, anchor='w').pack(side='left')
        self.alarm_complete_var = tk.StringVar(value=alarm_config.get('complete_hotkey', 'f7'))
        self.alarm_complete_entry = ctk.CTkEntry(complete_row, textvariable=self.alarm_complete_var, width=80, state='disabled')
        self.alarm_complete_entry.pack(side='left', padx=(0, 10))
        self.alarm_complete_btn = ctk.CTkButton(complete_row, text="Change", width=80,
                                               command=lambda: self.start_capture('alarm_complete_hotkey'))
        self.alarm_complete_btn.pack(side='left')
        ctk.CTkLabel(complete_row, text="✓ Gets streak credit", text_color="#00CED1").pack(side='left', padx=(10, 0))
        self.hotkey_buttons['alarm_complete_hotkey'] = self.alarm_complete_btn

        # Dismiss hotkey row (just silence, no credit, breaks streak)
        dismiss_row = ctk.CTkFrame(alarm_settings_frame, fg_color="transparent")
        dismiss_row.pack(fill='x', padx=15, pady=(0, 10))
        ctk.CTkLabel(dismiss_row, text="Dismiss hotkey:", width=120, anchor='w').pack(side='left')
        self.alarm_dismiss_var = tk.StringVar(value=alarm_config.get('dismiss_hotkey', 'f8'))
        self.alarm_dismiss_entry = ctk.CTkEntry(dismiss_row, textvariable=self.alarm_dismiss_var, width=80, state='disabled')
        self.alarm_dismiss_entry.pack(side='left', padx=(0, 10))
        self.alarm_dismiss_btn = ctk.CTkButton(dismiss_row, text="Change", width=80,
                                               command=lambda: self.start_capture('alarm_dismiss_hotkey'))
        self.alarm_dismiss_btn.pack(side='left')
        ctk.CTkLabel(dismiss_row, text="✗ Breaks streak", text_color="#ff6b6b").pack(side='left', padx=(10, 0))
        self.hotkey_buttons['alarm_dismiss_hotkey'] = self.alarm_dismiss_btn

        # Nag interval row
        nag_row = ctk.CTkFrame(alarm_settings_frame, fg_color="transparent")
        nag_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(nag_row, text="Repeat interval:", width=120, anchor='w').pack(side='left')
        self.alarm_nag_var = tk.IntVar(value=alarm_config.get('nag_interval_seconds', 60))
        nag_entry = ctk.CTkEntry(nag_row, textvariable=self.alarm_nag_var, width=80)
        nag_entry.pack(side='left', padx=(0, 10))
        ctk.CTkLabel(nag_row, text="seconds (how often to replay until dismissed)").pack(side='left')

        # Alarm List Section
        alarm_list_label = ctk.CTkLabel(alarms_scroll, text="Your Alarms", font=ctk.CTkFont(size=16, weight="bold"))
        alarm_list_label.pack(anchor='w', pady=(0, 10))

        alarm_list_frame = ctk.CTkFrame(alarms_scroll, corner_radius=10)
        alarm_list_frame.pack(fill='both', expand=True, pady=(0, 10))

        # Treeview for alarms
        alarm_tree_container = ctk.CTkFrame(alarm_list_frame, fg_color="transparent")
        alarm_tree_container.pack(fill='both', expand=True, padx=10, pady=10)

        alarm_tree_scroll = ttk.Scrollbar(alarm_tree_container)
        alarm_tree_scroll.pack(side='right', fill='y')

        # Style for alarm treeview (increased row height for stats)
        style = ttk.Style()
        style.configure("Alarms.Treeview",
                       background="#2b2b2b",
                       foreground="white",
                       fieldbackground="#2b2b2b",
                       rowheight=36)
        style.configure("Alarms.Treeview.Heading",
                       background="#1f6aa5",
                       foreground="white",
                       font=('Segoe UI', 10, 'bold'),
                       relief='flat')
        style.map("Alarms.Treeview.Heading", background=[('active', '#2980b9')])
        style.map("Alarms.Treeview", background=[('selected', '#1f6aa5')])

        self.alarm_tree = ttk.Treeview(alarm_tree_container, 
                                       columns=('enabled', 'name', 'interval', 'streak'),
                                       show='headings', 
                                       yscrollcommand=alarm_tree_scroll.set,
                                       style="Alarms.Treeview", 
                                       height=8)
        self.alarm_tree.pack(side='left', fill='both', expand=True)
        alarm_tree_scroll.config(command=self.alarm_tree.yview)

        # Column headings
        self.alarm_tree.heading('enabled', text='On')
        self.alarm_tree.heading('name', text='Name')
        self.alarm_tree.heading('interval', text='Interval')
        self.alarm_tree.heading('streak', text='Streak / Best')

        # Column widths
        self.alarm_tree.column('enabled', width=40, minwidth=40, anchor='center')
        self.alarm_tree.column('name', width=150, minwidth=100)
        self.alarm_tree.column('interval', width=80, minwidth=60)
        self.alarm_tree.column('streak', width=100, minwidth=80, anchor='center')

        # Populate alarms list
        self.populate_alarms_list()

        # Alarm buttons frame
        alarm_btn_frame = ctk.CTkFrame(alarms_scroll, fg_color="transparent")
        alarm_btn_frame.pack(fill='x', pady=(0, 10))

        ctk.CTkButton(alarm_btn_frame, text="Add Alarm", width=100,
                     command=self.add_alarm_dialog).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Edit", width=70,
                     command=self.edit_alarm_dialog).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Delete", width=70, fg_color="#cc4444", hover_color="#aa3333",
                     command=self.delete_alarm).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Toggle", width=70, fg_color="gray40",
                     command=self.toggle_alarm_enabled).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Test", width=60, fg_color="gray40",
                     command=self.test_alarm_sound).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Reset Stats", width=90, fg_color="gray40",
                     command=self.reset_alarm_stats).pack(side='left')

        # Info text
        ctk.CTkLabel(alarms_scroll,
                    text=f"Complete ({self.alarm_complete_var.get().upper()}) to get streak credit. Dismiss ({self.alarm_dismiss_var.get().upper()}) to silence without credit.",
                    text_color="gray", wraplength=600).pack(anchor='w', pady=(5, 0))

    def build_advanced_tab(self):
        """Build the Advanced settings tab."""
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

        # Hardware Acceleration Section
        hw_label = ctk.CTkLabel(advanced_scroll, text="Hardware Acceleration", font=ctk.CTkFont(size=16, weight="bold"))
        hw_label.pack(anchor='w', pady=(0, 10))

        hw_frame = ctk.CTkFrame(advanced_scroll, corner_radius=10)
        hw_frame.pack(fill='x', pady=(0, 20))

        device_row = ctk.CTkFrame(hw_frame, fg_color="transparent")
        device_row.pack(fill='x', padx=15, pady=(15, 5))
        ctk.CTkLabel(device_row, text="Compute device:", width=150, anchor='w').pack(side='left')
        
        # Device options with display names
        self.device_display_to_value = {
            'CPU': 'cpu',
            'CUDA (NVIDIA GPU)': 'cuda'
        }
        self.device_value_to_display = {v: k for k, v in self.device_display_to_value.items()}
        
        current_device = self.app.config.get('device', 'cpu')
        current_device_display = self.device_value_to_display.get(current_device, 'CPU')
        
        self.device_var = tk.StringVar(value=current_device_display)
        device_combo = ctk.CTkComboBox(device_row, variable=self.device_var,
                                        values=list(self.device_display_to_value.keys()),
                                        width=180, state='readonly')
        device_combo.pack(side='left')

        ctk.CTkLabel(hw_frame, text="CUDA requires an NVIDIA GPU with compatible drivers installed",
                    text_color="gray").pack(anchor='w', padx=15, pady=(0, 5))
        ctk.CTkLabel(hw_frame, text="Device changes require restart",
                    text_color="#1f6aa5").pack(anchor='w', padx=15, pady=(0, 10))

        # CUDA Setup Instructions (collapsible)
        cuda_instructions_frame = ctk.CTkFrame(hw_frame, fg_color="transparent")
        cuda_instructions_frame.pack(fill='x', padx=15, pady=(0, 15))
        
        def toggle_cuda_instructions():
            if cuda_text_frame.winfo_viewable():
                cuda_text_frame.pack_forget()
                cuda_toggle_btn.configure(text="▶ CUDA Setup Instructions")
            else:
                cuda_text_frame.pack(fill='x', pady=(5, 0))
                cuda_toggle_btn.configure(text="▼ CUDA Setup Instructions")
        
        cuda_toggle_btn = ctk.CTkButton(cuda_instructions_frame, text="▶ CUDA Setup Instructions",
                                         command=toggle_cuda_instructions,
                                         fg_color="transparent", text_color="#1f6aa5",
                                         hover_color=("gray90", "gray20"),
                                         anchor='w', width=200)
        cuda_toggle_btn.pack(anchor='w')
        
        cuda_text_frame = ctk.CTkFrame(cuda_instructions_frame, fg_color=("gray95", "gray17"))
        # Initially hidden - don't pack
        
        cuda_instructions = """To enable CUDA support:
1. Open PowerShell in the Samsara folder
2. Run: pip uninstall ctranslate2
3. Run: pip install ctranslate2 --extra-index-url https://download.pytorch.org/whl/cu118
4. Restart Samsara and select CUDA above"""
        
        cuda_text = ctk.CTkTextbox(cuda_text_frame, height=100, wrap='word',
                                    fg_color="transparent", activate_scrollbars=False)
        cuda_text.pack(fill='x', padx=10, pady=10)
        cuda_text.insert('1.0', cuda_instructions)
        cuda_text.configure(state='disabled')  # Make read-only but selectable

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

        # Echo Cancellation Settings
        aec_label = ctk.CTkLabel(advanced_scroll, text="Echo Cancellation", font=ctk.CTkFont(size=16, weight="bold"))
        aec_label.pack(anchor='w', pady=(0, 10))

        aec_frame = ctk.CTkFrame(advanced_scroll, corner_radius=10)
        aec_frame.pack(fill='x', pady=(0, 20))

        aec_config = self.app.config.get('echo_cancellation', {})
        self.aec_enabled_var = tk.BooleanVar(value=aec_config.get('enabled', False))
        ctk.CTkCheckBox(aec_frame, text="Enable echo cancellation (removes system audio from mic)",
                       variable=self.aec_enabled_var).pack(anchor='w', padx=15, pady=(15, 8))

        latency_row = ctk.CTkFrame(aec_frame, fg_color="transparent")
        latency_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(latency_row, text="Latency compensation:", width=160, anchor='w').pack(side='left')
        self.aec_latency_var = tk.DoubleVar(value=aec_config.get('latency_ms', 30.0))
        aec_latency_entry = ctk.CTkEntry(latency_row, textvariable=self.aec_latency_var, width=80)
        aec_latency_entry.pack(side='left', padx=(0, 10))
        ctk.CTkLabel(latency_row, text="ms").pack(side='left')

        ctk.CTkLabel(aec_frame,
                    text="Filters out music/video audio so only your voice is transcribed.\n"
                         "Requires restart. Windows only (uses WASAPI loopback).",
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
        elif hotkey_name == 'command_hotkey':
            self.cmd_hotkey_var.set("Press keys...")
            self.cmd_hotkey_btn.configure(text="...")
        elif hotkey_name == 'alarm_complete_hotkey':
            self.alarm_complete_var.set("Press keys...")
            self.alarm_complete_btn.configure(text="...")
        elif hotkey_name == 'alarm_dismiss_hotkey':
            self.alarm_dismiss_var.set("Press keys...")
            self.alarm_dismiss_btn.configure(text="...")

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
        elif self.capturing_hotkey == 'command_hotkey':
            self.cmd_hotkey_var.set(hotkey_str)
        elif self.capturing_hotkey == 'alarm_complete_hotkey':
            self.alarm_complete_var.set(hotkey_str)
        elif self.capturing_hotkey == 'alarm_dismiss_hotkey':
            self.alarm_dismiss_var.set(hotkey_str)

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
            elif self.capturing_hotkey == 'command_hotkey':
                self.cmd_hotkey_var.set(hotkey_str)
                self.cmd_hotkey_btn.configure(text="Set")
            elif self.capturing_hotkey == 'alarm_complete_hotkey':
                self.alarm_complete_var.set(hotkey_str)
                self.alarm_complete_btn.configure(text="Set")
            elif self.capturing_hotkey == 'alarm_dismiss_hotkey':
                self.alarm_dismiss_var.set(hotkey_str)
                self.alarm_dismiss_btn.configure(text="Set")

        self.window.unbind('<KeyPress>')
        self.window.unbind('<KeyRelease>')
        self.capturing_hotkey = None
        self.captured_keys = set()

    def _get_var(self, attr, config_key, default=None, nested_keys=None):
        """Read a setting from the UI variable if the tab was built,
        otherwise fall back to the existing config value.  This avoids
        force-building unvisited tabs just to read their defaults."""
        var = getattr(self, attr, None)
        if var is not None:
            return var.get()
        # Tab was never visited -- keep current config value
        if nested_keys:
            val = self.app.config
            for k in nested_keys:
                val = val.get(k, {}) if isinstance(val, dict) else {}
            return val if val != {} else default
        return self.app.config.get(config_key, default)

    def save_settings(self):
        # No force-building of unvisited tabs -- _get_var falls back to config

        old_model = self.app.config.get('model_size', 'base')
        # Convert display name back to actual model value
        if hasattr(self, 'model_var') and self.model_var is not None:
            model_display = self.model_var.get()
            new_model = self.model_display_to_value.get(model_display, 'base')
        else:
            new_model = old_model
        model_changed = old_model != new_model

        # Track device changes
        old_device = self.app.config.get('device', 'cpu')
        if hasattr(self, 'device_var') and self.device_var is not None:
            device_display = self.device_var.get()
            new_device = self.device_display_to_value.get(device_display, 'cpu')
        else:
            new_device = old_device
        device_changed = old_device != new_device

        # Batch all simple config changes (save=False; we save once at the end)
        self.app.update_config({
            'mode': self._get_var('mode_var', 'mode', 'hold'),
            'wake_word_enabled': self._get_var('wake_word_enabled_var', 'wake_word_enabled', False),
            'hotkey': self._get_var('hotkey_var', 'hotkey', 'ctrl+shift'),
            'continuous_hotkey': self._get_var('cont_hotkey_var', 'continuous_hotkey', 'ctrl+alt+d'),
            'wake_word_hotkey': self._get_var('wake_hotkey_var', 'wake_word_hotkey', 'ctrl+alt+w'),
            'command_hotkey': self._get_var('cmd_hotkey_var', 'command_hotkey', 'ctrl+alt+c'),
            'cancel_hotkey': self._get_var('cancel_hotkey_var', 'cancel_hotkey', 'escape'),
            'silence_threshold': self._get_var('silence_var', 'silence_threshold', 2.0),
            'min_speech_duration': self._get_var('min_speech_var', 'min_speech_duration', 0.3),
            'auto_paste': self._get_var('auto_paste_var', 'auto_paste', True),
            'add_trailing_space': self._get_var('trailing_space_var', 'add_trailing_space', True),
            'auto_capitalize': self._get_var('auto_capitalize_var', 'auto_capitalize', True),
            'format_numbers': self._get_var('format_numbers_var', 'format_numbers', True),
            'model_size': new_model,
            'device': new_device,
            'command_mode_enabled': self._get_var('command_mode_var', 'command_mode_enabled', False),
            'show_all_audio_devices': self._get_var('show_all_devices_var', 'show_all_audio_devices', False),
            'audio_feedback': self._get_var('audio_feedback_var', 'audio_feedback', True),
            'sound_volume': self._get_var('sound_volume_var', 'sound_volume', 0.5),
            'performance_mode': self._get_var('perf_mode_var', 'performance_mode', 'balanced'),
        }, save=False)

        # Save listening indicator settings
        old_indicator_enabled = self.app.config.get('listening_indicator_enabled', False)
        old_indicator_pos = self.app.config.get('listening_indicator_position', 'bottom-center')
        new_indicator_enabled = self._get_var('indicator_enabled_var', 'listening_indicator_enabled', False)
        new_indicator_pos = self._get_var('indicator_pos_var', 'listening_indicator_position', 'bottom-center')
        self.app.update_config({
            'listening_indicator_enabled': new_indicator_enabled,
            'listening_indicator_position': new_indicator_pos,
        }, save=False)

        # Apply indicator changes at runtime
        if hasattr(self.app, 'listening_indicator'):
            if new_indicator_pos != old_indicator_pos:
                self.app._schedule_ui(self.app.listening_indicator.set_position, new_indicator_pos)
            if new_indicator_enabled and not old_indicator_enabled:
                self.app._schedule_ui(self.app.listening_indicator.show)
            elif not new_indicator_enabled and old_indicator_enabled:
                self.app._schedule_ui(self.app.listening_indicator.hide)

        # Save echo cancellation settings
        self.app.update_config({
            'echo_cancellation': {
                'enabled': self._get_var('aec_enabled_var', 'enabled', False,
                                         nested_keys=['echo_cancellation', 'enabled']),
                'latency_ms': self._get_var('aec_latency_var', 'latency_ms', 30.0,
                                            nested_keys=['echo_cancellation', 'latency_ms']),
            },
        }, save=False)

        # Save wake word config -- only update fields if Advanced tab was visited
        ww_config = self.app.config.get('wake_word_config', {})
        if "Advanced" in self.built_tabs:
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
        self.app.update_config({'wake_word_config': ww_config}, save=False)

        # Save alarm settings -- only update fields if Alarms tab was visited
        if 'alarms' not in self.app.config:
            self.app.update_config({'alarms': get_default_alarm_config()}, save=False)
        if "Alarms" in self.built_tabs:
            alarms = self.app.config['alarms']
            alarms['enabled'] = self.alarms_enabled_var.get()
            alarms['complete_hotkey'] = self.alarm_complete_var.get()
            alarms['dismiss_hotkey'] = self.alarm_dismiss_var.get()
            alarms['nag_interval_seconds'] = self.alarm_nag_var.get()
        
        # Apply alarm settings at runtime
        if "Alarms" in self.built_tabs and hasattr(self.app, 'alarm_manager'):
            if self.alarms_enabled_var.get() and not self.app.alarm_manager.running:
                self.app.alarm_manager.start()
            elif not self.alarms_enabled_var.get() and self.app.alarm_manager.running:
                self.app.alarm_manager.stop()

        self.app.command_mode_enabled = self.app.config['command_mode_enabled']

        mic_name = self.mic_var.get() if hasattr(self, 'mic_var') and self.mic_var else None
        mic_changed = False

        if mic_name:
            for mic in self.available_mics:
                if mic['name'] == mic_name:
                    if self.app.config.get('microphone') != mic['id']:
                        mic_changed = True
                        self.app.update_config({'microphone': mic['id']}, save=False)
                    break

        self.app.save_config()

        # Apply mode change at runtime (delegates to DictationApp.apply_mode)
        new_mode = self.app.config['mode']
        if self.app.apply_mode(new_mode):
            self.app.save_config()
            if hasattr(self.app, 'tray_icon') and hasattr(self.app, 'get_menu'):
                try:
                    self.app.tray_icon.menu = self.app.get_menu()
                except Exception as e:
                    print(f"[MODE] Failed to refresh tray menu: {e}")

        # Apply wake word enable/disable at runtime
        new_ww = self.app.config.get('wake_word_enabled', False)
        self.app.set_wake_word_enabled(new_ww)

        if mic_changed and hasattr(self.app, 'tray_icon') and hasattr(self.app, 'get_menu'):
            self.app.tray_icon.menu = self.app.get_menu()
            self.app._update_tray_tooltip()
            print(f"Microphone changed to: {self.app.get_current_microphone_name()}")

        print("Settings saved successfully!")

        if model_changed:
            self.prompt_restart_for_model(old_model, new_model)
        elif device_changed:
            self.prompt_restart_for_device(old_device, new_device)

    def prompt_restart_for_device(self, old_device, new_device):
        """Ask user if they want to restart to apply new device"""
        device_names = {'cpu': 'CPU', 'cuda': 'CUDA (GPU)'}
        old_name = device_names.get(old_device, old_device)
        new_name = device_names.get(new_device, new_device)
        result = messagebox.askyesno(
            "Restart Required",
            f"Device changed from '{old_name}' to '{new_name}'.\n\n"
            f"The app needs to restart to use the new device.\n\n"
            f"Restart now?",
            parent=self.window
        )
        if result:
            self.restart_app()

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
        import subprocess
        print("Restarting application...")
        
        # Stop background services first
        if hasattr(self.app, 'tray_icon'):
            try:
                self.app.tray_icon.stop()
            except:
                pass
        if hasattr(self.app, 'keyboard_listener'):
            try:
                self.app.keyboard_listener.stop()
            except:
                pass
        
        # Spawn new process BEFORE exiting
        if sys.platform == 'win32':
            # Use the VBS launcher on Windows for proper console-less restart
            launcher = Path(__file__).parent / "_launcher.vbs"
            if launcher.exists():
                subprocess.Popen(['wscript', str(launcher)], 
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                # Fallback: direct pythonw launch
                script = Path(__file__).resolve()
                subprocess.Popen([sys.executable, str(script)],
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 
                                           subprocess.DETACHED_PROCESS)
        else:
            # Unix: spawn detached process
            script = Path(__file__).resolve()
            subprocess.Popen([sys.executable, str(script)],
                           start_new_session=True)
        
        # Now exit current instance
        self.close()
        sys.exit(0)

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
        self.app.update_config({'sound_volume': volume}, save=False)

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
        self.app.update_config({'sound_theme': theme})
        
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

    # ==================== ALARM METHODS ====================

    def populate_alarms_list(self):
        """Populate the alarms treeview with streak stats"""
        # Clear existing items
        for item in self.alarm_tree.get_children():
            self.alarm_tree.delete(item)

        # Get alarms from config
        alarm_config = self.app.config.get('alarms', get_default_alarm_config())
        alarms = alarm_config.get('items', [])

        for alarm in alarms:
            alarm_id = alarm.get('id', alarm.get('name', 'unknown'))
            enabled = "✓" if alarm.get('enabled', False) else ""
            name = alarm.get('name', 'Unnamed')
            interval = f"{alarm.get('interval_minutes', 60)} min"
            
            # Get streak info from alarm manager
            if hasattr(self.app, 'alarm_manager'):
                stats = self.app.alarm_manager.get_stats(alarm_id)
                current = stats.get('current_streak', 0)
                best = stats.get('best_streak', 0)
                if current > 0 or best > 0:
                    streak_text = f"🔥 {current} / {best}"
                else:
                    streak_text = "—"
            else:
                streak_text = "—"

            self.alarm_tree.insert('', 'end', iid=alarm_id,
                                   values=(enabled, name, interval, streak_text))

    def get_selected_alarm(self):
        """Get the selected alarm ID"""
        selection = self.alarm_tree.selection()
        if selection:
            return selection[0]
        return None

    def add_alarm_dialog(self):
        """Show dialog to add a new alarm"""
        self._show_alarm_dialog()

    def edit_alarm_dialog(self):
        """Show dialog to edit selected alarm"""
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to edit.", parent=self.window)
            return
        self._show_alarm_dialog(edit_id=alarm_id)

    def _show_alarm_dialog(self, edit_id=None):
        """Show add/edit alarm dialog"""
        dialog = ctk.CTkToplevel(self.window)
        dialog.title("Edit Alarm" if edit_id else "Add Alarm")
        dialog.geometry("400x350")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()

        # Get existing data if editing
        existing_data = {}
        if edit_id and hasattr(self.app, 'alarm_manager'):
            existing_data = self.app.alarm_manager.get_alarm(edit_id) or {}

        # Name field
        ctk.CTkLabel(dialog, text="Alarm Name:", font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(20, 5))
        name_var = tk.StringVar(value=existing_data.get('name', ''))
        name_entry = ctk.CTkEntry(dialog, textvariable=name_var, width=300)
        name_entry.pack(anchor='w', padx=20)

        # Interval field
        ctk.CTkLabel(dialog, text="Interval (minutes):", font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(15, 5))
        interval_var = tk.IntVar(value=existing_data.get('interval_minutes', 60))
        interval_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        interval_frame.pack(anchor='w', padx=20)
        interval_entry = ctk.CTkEntry(interval_frame, textvariable=interval_var, width=100)
        interval_entry.pack(side='left')
        ctk.CTkLabel(interval_frame, text="minutes", text_color="gray").pack(side='left', padx=(10, 0))

        # Sound selection
        ctk.CTkLabel(dialog, text="Sound:", font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(15, 5))
        
        # Get available sounds
        sound_options = ['Alarm', 'Chime', 'Bell', 'Gentle']  # Built-in sounds
        current_sound = existing_data.get('sound', 'alarm')
        if current_sound in ['alarm', 'chime', 'bell', 'gentle']:
            current_display = current_sound.title()
        else:
            current_display = Path(current_sound).stem.replace('_', ' ').title() if current_sound else 'Alarm'
            if current_display not in sound_options:
                sound_options.append(current_display)

        sound_var = tk.StringVar(value=current_display)
        sound_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        sound_frame.pack(anchor='w', padx=20, fill='x')
        
        sound_combo = ctk.CTkComboBox(sound_frame, variable=sound_var, values=sound_options, width=150)
        sound_combo.pack(side='left')
        
        def browse_sound():
            from tkinter import filedialog
            filepath = filedialog.askopenfilename(
                parent=dialog,
                title="Select Sound File",
                filetypes=[("Audio Files", "*.wav *.mp3"), ("WAV Files", "*.wav"), ("MP3 Files", "*.mp3")]
            )
            if filepath:
                filename = Path(filepath).stem.replace('_', ' ').title()
                current_values = list(sound_combo.cget('values'))
                if filename not in current_values:
                    current_values.append(filename)
                    sound_combo.configure(values=current_values)
                sound_var.set(filename)
                # Store full path for later
                dialog.custom_sound_path = filepath

        ctk.CTkButton(sound_frame, text="Browse...", width=80, command=browse_sound).pack(side='left', padx=(10, 0))
        
        def preview_sound():
            sound_name = sound_var.get().lower().replace(' ', '_')
            if hasattr(dialog, 'custom_sound_path'):
                sound_path = dialog.custom_sound_path
            elif sound_name in ['alarm', 'chime', 'bell', 'gentle']:
                sound_path = sound_name
            else:
                sound_path = sound_name
            if hasattr(self.app, 'alarm_manager'):
                threading.Thread(target=lambda: self.app.alarm_manager.play_sound_file(sound_path), daemon=True).start()

        ctk.CTkButton(sound_frame, text="Test", width=60, fg_color="gray40", command=preview_sound).pack(side='left', padx=(10, 0))

        # Enabled checkbox
        enabled_var = tk.BooleanVar(value=existing_data.get('enabled', True))
        ctk.CTkCheckBox(dialog, text="Enabled", variable=enabled_var).pack(anchor='w', padx=20, pady=(15, 0))

        # Buttons
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill='x', padx=20, pady=20)

        def save_alarm():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Error", "Please enter an alarm name.", parent=dialog)
                return

            interval = interval_var.get()
            if interval < 1:
                messagebox.showerror("Error", "Interval must be at least 1 minute.", parent=dialog)
                return

            # Get sound value
            sound_display = sound_var.get()
            if hasattr(dialog, 'custom_sound_path'):
                sound = dialog.custom_sound_path
            else:
                sound = sound_display.lower().replace(' ', '_')

            if edit_id:
                # Update existing alarm
                self.app.alarm_manager.update_alarm(
                    edit_id,
                    name=name,
                    interval_minutes=interval,
                    sound=sound,
                    enabled=enabled_var.get()
                )
            else:
                # Add new alarm
                self.app.alarm_manager.add_alarm(
                    name=name,
                    interval_minutes=interval,
                    sound=sound,
                    enabled=enabled_var.get()
                )

            self.populate_alarms_list()
            dialog.destroy()
            messagebox.showinfo("Success", f"Alarm '{name}' saved!", parent=self.window)

        ctk.CTkButton(btn_frame, text="Save", width=100, command=save_alarm).pack(side='right', padx=(10, 0))
        ctk.CTkButton(btn_frame, text="Cancel", width=100, fg_color="gray40",
                     command=dialog.destroy).pack(side='right')

    def delete_alarm(self):
        """Delete the selected alarm"""
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to delete.", parent=self.window)
            return

        # Get alarm name for confirmation
        alarm = self.app.alarm_manager.get_alarm(alarm_id) if hasattr(self.app, 'alarm_manager') else None
        alarm_name = alarm.get('name', alarm_id) if alarm else alarm_id

        if messagebox.askyesno("Confirm Delete",
                              f"Are you sure you want to delete the alarm '{alarm_name}'?",
                              parent=self.window):
            if hasattr(self.app, 'alarm_manager'):
                self.app.alarm_manager.remove_alarm(alarm_id)
                self.populate_alarms_list()
                messagebox.showinfo("Deleted", f"Alarm '{alarm_name}' deleted.", parent=self.window)

    def toggle_alarm_enabled(self):
        """Toggle the selected alarm's enabled state"""
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to toggle.", parent=self.window)
            return

        if hasattr(self.app, 'alarm_manager'):
            new_state = self.app.alarm_manager.toggle_alarm(alarm_id)
            if new_state is not None:
                self.populate_alarms_list()
                state_text = "enabled" if new_state else "disabled"
                print(f"[ALARM] Toggled alarm {alarm_id}: {state_text}")

    def test_alarm_sound(self):
        """Test the selected alarm's sound"""
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to test.", parent=self.window)
            return

        if hasattr(self.app, 'alarm_manager'):
            alarm = self.app.alarm_manager.get_alarm(alarm_id)
            if alarm:
                # Play in background thread to not block UI
                threading.Thread(target=lambda: self.app.alarm_manager.play_sound(alarm), daemon=True).start()

    def reset_alarm_stats(self):
        """Reset streak stats for the selected alarm"""
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to reset stats.", parent=self.window)
            return

        # Get alarm name for confirmation
        alarm = self.app.alarm_manager.get_alarm(alarm_id) if hasattr(self.app, 'alarm_manager') else None
        alarm_name = alarm.get('name', alarm_id) if alarm else alarm_id

        if messagebox.askyesno("Reset Stats",
                              f"Reset all stats for '{alarm_name}'?\n\nThis will clear:\n• Current streak\n• Best streak\n• Total completions",
                              parent=self.window):
            if hasattr(self.app, 'alarm_manager'):
                self.app.alarm_manager.reset_stats(alarm_id)
                self.populate_alarms_list()
                messagebox.showinfo("Stats Reset", f"Stats for '{alarm_name}' have been reset.", parent=self.window)

