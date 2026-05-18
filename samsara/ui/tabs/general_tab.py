"""General settings tab.

Sections: Microphone, Basic Options, Listening Indicator, Profiles,
Voice Training, AI Model.

Extracted from SettingsWindow.build_general_tab() to keep SettingsWindow
under a manageable size.  All callbacks that open other windows or modify
system state are delegated via self.sw (the SettingsWindow instance).
"""

import tkinter as tk

import customtkinter as ctk

from samsara.languages import LANGUAGES, DEFAULT_TTS_VOICES


class GeneralTab:
    """General settings tab: Microphone, Basic Options, Listening Indicator,
    Profiles, Voice Training, AI Model."""

    # Language display <-> code maps (derived from shared languages module)
    LANG_DISPLAY_TO_CODE = {name: code for name, code in LANGUAGES}
    LANG_CODE_TO_DISPLAY = {code: name for name, code in LANGUAGES}

    # Model display <-> internal-value maps live here, not on SettingsWindow.
    MODEL_DISPLAY_TO_VALUE = {
        'tiny (~75 MB)': 'tiny',
        'tiny.en — English-only (~75 MB)': 'tiny.en',
        'base (~150 MB)': 'base',
        'base.en — English-only (~150 MB)': 'base.en',
        'small (~500 MB)': 'small',
        'small.en — English-only, recommended (~500 MB)': 'small.en',
        'medium (~1.5 GB)': 'medium',
        'medium.en — English-only (~1.5 GB)': 'medium.en',
        'large-v3 (~3 GB)': 'large-v3',
    }
    MODEL_VALUE_TO_DISPLAY = {v: k for k, v in MODEL_DISPLAY_TO_VALUE.items()}

    def __init__(self, parent_frame, app, settings_window):
        """
        parent_frame:    CTkFrame from tabview.tab("General")
        app:             DictationApp instance
        settings_window: SettingsWindow (for callbacks only — not for state)
        """
        self.app = app
        self.parent = parent_frame
        self.sw = settings_window
        self._built = False

        # tk.Vars — set during build()
        self.mic_var = None
        self.mic_combo = None
        self.show_all_devices_var = None
        self.auto_paste_var = None
        self.trailing_space_var = None
        self.auto_capitalize_var = None
        self.format_numbers_var = None
        self.command_mode_var = None
        self.auto_start_var = None
        self.indicator_enabled_var = None
        self.indicator_pos_var = None
        self.language_var = None
        self.model_var = None
        self._lang_warning_label = None

    # ------------------------------------------------------------------
    # Build (generator — yields between sections for staged loading)
    # ------------------------------------------------------------------

    def build(self):
        """Generator: build all sections, yielding between each."""
        general_scroll = ctk.CTkScrollableFrame(self.parent, fg_color="transparent")
        general_scroll.pack(fill='both', expand=True)

        # --- Microphone Section ---
        ctk.CTkLabel(general_scroll, text="Microphone",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(15, 10))
        mic_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        mic_frame.pack(fill='x', pady=(0, 20))
        ctk.CTkLabel(mic_frame, text="Selected device:").pack(anchor='w', padx=15, pady=(15, 5))

        available_mics = self.sw.available_mics
        mic_names = [mic['name'] for mic in available_mics]
        current_mic_id = self.app.config.get('microphone')
        current_selection = mic_names[0] if mic_names else "No microphones found"
        if current_mic_id is not None:
            for mic in available_mics:
                if mic['id'] == current_mic_id:
                    current_selection = mic['name']
                    break

        self.mic_var = tk.StringVar(value=current_selection)
        self.mic_combo = ctk.CTkComboBox(mic_frame, variable=self.mic_var,
                                         values=mic_names, width=400, state='readonly')
        self.mic_combo.pack(anchor='w', padx=15, pady=(0, 10))

        self.show_all_devices_var = tk.BooleanVar(
            value=self.app.config.get('show_all_audio_devices', False))
        ctk.CTkCheckBox(mic_frame,
                        text="Show all audio devices (includes virtual/system devices)",
                        variable=self.show_all_devices_var,
                        command=self.sw.refresh_microphone_list
                        ).pack(anchor='w', padx=15, pady=(0, 15))
        yield

        # --- Basic Options Section ---
        ctk.CTkLabel(general_scroll, text="Basic Options",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        options_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        options_frame.pack(fill='x', pady=(0, 20))

        self.auto_paste_var = tk.BooleanVar(
            value=self.app.config.get('auto_paste', True))
        ctk.CTkCheckBox(options_frame, text="Automatically paste transcribed text",
                        variable=self.auto_paste_var
                        ).pack(anchor='w', padx=15, pady=(15, 8))

        self.trailing_space_var = tk.BooleanVar(
            value=self.app.config.get('add_trailing_space', True))
        ctk.CTkCheckBox(options_frame, text="Add trailing space after text",
                        variable=self.trailing_space_var
                        ).pack(anchor='w', padx=15, pady=(0, 8))

        self.auto_capitalize_var = tk.BooleanVar(
            value=self.app.config.get('auto_capitalize', True))
        ctk.CTkCheckBox(options_frame, text="Auto-capitalize sentences",
                        variable=self.auto_capitalize_var
                        ).pack(anchor='w', padx=15, pady=(0, 8))

        self.format_numbers_var = tk.BooleanVar(
            value=self.app.config.get('format_numbers', True))
        ctk.CTkCheckBox(options_frame, text="Convert spoken numbers to digits",
                        variable=self.format_numbers_var
                        ).pack(anchor='w', padx=15, pady=(0, 8))

        self.command_mode_var = tk.BooleanVar(
            value=self.app.config.get('command_mode_enabled', True))
        ctk.CTkCheckBox(options_frame, text="Enable voice commands (recommended)",
                        variable=self.command_mode_var
                        ).pack(anchor='w', padx=15, pady=(0, 8))

        self.auto_start_var = tk.BooleanVar(value=self.sw.check_auto_start())
        ctk.CTkCheckBox(options_frame, text="Start Samsara with Windows",
                        variable=self.auto_start_var,
                        command=self.sw.toggle_auto_start
                        ).pack(anchor='w', padx=15, pady=(0, 15))
        yield

        # --- Listening Indicator Section ---
        ctk.CTkLabel(general_scroll, text="Listening Indicator",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        indicator_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        indicator_frame.pack(fill='x', pady=(0, 20))

        ctk.CTkLabel(indicator_frame,
                     text="An always-on-top pill that shows your current mode and pulses while recording",
                     text_color="gray"
                     ).pack(anchor='w', padx=15, pady=(15, 10))

        self.indicator_enabled_var = tk.BooleanVar(
            value=self.app.config.get('listening_indicator_enabled', False))
        ctk.CTkCheckBox(indicator_frame, text="Show listening indicator overlay",
                        variable=self.indicator_enabled_var
                        ).pack(anchor='w', padx=15, pady=(0, 10))

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
        yield

        # --- Profiles + Voice Training + AI Model ---
        ctk.CTkLabel(general_scroll, text="Profiles",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        profiles_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        profiles_frame.pack(fill='x', pady=(0, 20))
        ctk.CTkLabel(profiles_frame,
                     text="Save and load vocabulary and command configurations",
                     text_color="gray"
                     ).pack(anchor='w', padx=15, pady=(15, 10))
        ctk.CTkButton(profiles_frame, text="Manage Profiles...", width=160,
                      command=self.sw.open_profile_manager
                      ).pack(anchor='w', padx=15, pady=(0, 15))

        ctk.CTkLabel(general_scroll, text="Microphone Setup",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        setup_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        setup_frame.pack(fill='x', pady=(0, 20))
        ctk.CTkLabel(setup_frame,
                     text="Step-by-step guide: pick your device, set your level, "
                          "and confirm your wake word is working.",
                     text_color="gray", wraplength=540
                     ).pack(anchor='w', padx=15, pady=(15, 10))
        ctk.CTkButton(setup_frame, text="Run Mic Setup Guide...", width=190,
                      command=self.sw.open_mic_setup_guide
                      ).pack(anchor='w', padx=15, pady=(0, 15))

        ctk.CTkLabel(general_scroll, text="Voice Training",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        training_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        training_frame.pack(fill='x', pady=(0, 20))
        ctk.CTkLabel(training_frame,
                     text="Customize vocabulary, corrections, and microphone calibration",
                     text_color="gray"
                     ).pack(anchor='w', padx=15, pady=(15, 10))
        ctk.CTkButton(training_frame, text="Open Voice Training...", width=180,
                      command=self.sw.open_voice_training
                      ).pack(anchor='w', padx=15, pady=(0, 15))

        ctk.CTkLabel(general_scroll, text="AI Model",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        model_frame = ctk.CTkFrame(general_scroll, corner_radius=10)
        model_frame.pack(fill='x')

        # --- Language ---
        ctk.CTkLabel(model_frame, text="Transcription language:"
                     ).pack(anchor='w', padx=15, pady=(15, 5))

        lang_names = [name for name, _ in LANGUAGES]
        current_lang_code = self.app.config.get('language', 'en')
        current_lang_display = self.LANG_CODE_TO_DISPLAY.get(current_lang_code, 'English')

        self.language_var = tk.StringVar(value=current_lang_display)
        ctk.CTkComboBox(model_frame, variable=self.language_var,
                        values=lang_names, width=250, state='readonly'
                        ).pack(anchor='w', padx=15, pady=(0, 10))

        # --- Model ---
        ctk.CTkLabel(model_frame, text="Whisper model size:"
                     ).pack(anchor='w', padx=15, pady=(0, 5))

        model_options = list(self.MODEL_DISPLAY_TO_VALUE.keys())
        current_model = self.app.config.get('model_size', 'base')
        current_display = self.MODEL_VALUE_TO_DISPLAY.get(current_model, 'base (~150 MB)')

        self.model_var = tk.StringVar(value=current_display)
        ctk.CTkComboBox(model_frame, variable=self.model_var,
                        values=model_options, width=400, state='readonly'
                        ).pack(anchor='w', padx=15, pady=(0, 5))

        # Warning label: shown when a non-English language + .en model are selected
        self._lang_warning_label = ctk.CTkLabel(
            model_frame, text="",
            text_color="#E8A020",
            wraplength=460,
            justify='left',
            font=ctk.CTkFont(size=12),
        )
        self._lang_warning_label.pack(anchor='w', padx=15, pady=(0, 4))

        ctk.CTkLabel(model_frame,
                     text=".en variants are more accurate for English-only speakers. small.en on GPU is the sweet spot.",
                     text_color="gray"
                     ).pack(anchor='w', padx=15, pady=(0, 5))
        ctk.CTkLabel(model_frame, text="Model changes require restart",
                     text_color="#1f6aa5"
                     ).pack(anchor='w', padx=15, pady=(0, 15))

        # Wire traces so warning updates live as dropdowns change
        self.language_var.trace_add('write', self._on_language_or_model_changed)
        self.model_var.trace_add('write', self._on_language_or_model_changed)
        self._on_language_or_model_changed()  # set initial state

        self._built = True

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_language_or_model_changed(self, *_):
        if self._lang_warning_label is None or self.language_var is None or self.model_var is None:
            return
        lang_display = self.language_var.get()
        lang_code = self.LANG_DISPLAY_TO_CODE.get(lang_display, "en")
        model_value = self.MODEL_DISPLAY_TO_VALUE.get(self.model_var.get(), "")
        if lang_code != "en" and model_value.endswith(".en"):
            self._lang_warning_label.configure(
                text=f"You're using an English-only model. Switch to the multilingual "
                     f"version (e.g. 'small' instead of 'small.en') for best results "
                     f"in {lang_display}."
            )
        else:
            self._lang_warning_label.configure(text="")

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self) -> dict:
        """Read all vars and apply config updates via app.update_config(save=False).

        Returns {'new_model': str, 'mic_changed': bool} so that
        SettingsWindow.save_settings() can detect model changes and prompt
        for restart without needing direct access to GeneralTab internals.
        """
        if not self._built:
            return {
                'new_model': self.app.config.get('model_size', 'base'),
                'mic_changed': False,
            }

        # Model
        model_display = self.model_var.get()
        new_model = self.MODEL_DISPLAY_TO_VALUE.get(
            model_display, self.app.config.get('model_size', 'base'))

        # Basic options + model
        self.app.update_config({
            'auto_paste':          self.auto_paste_var.get(),
            'add_trailing_space':  self.trailing_space_var.get(),
            'auto_capitalize':     self.auto_capitalize_var.get(),
            'format_numbers':      self.format_numbers_var.get(),
            'model_size':          new_model,
            'command_mode_enabled': self.command_mode_var.get(),
            'show_all_audio_devices': self.show_all_devices_var.get(),
        }, save=False)

        # Listening indicator
        old_indicator_enabled = self.app.config.get('listening_indicator_enabled', False)
        old_indicator_pos     = self.app.config.get('listening_indicator_position', 'bottom-center')
        new_indicator_enabled = self.indicator_enabled_var.get()
        new_indicator_pos     = self.indicator_pos_var.get()
        self.app.update_config({
            'listening_indicator_enabled':  new_indicator_enabled,
            'listening_indicator_position': new_indicator_pos,
        }, save=False)
        if hasattr(self.app, 'listening_indicator'):
            if new_indicator_pos != old_indicator_pos:
                self.app._schedule_ui(
                    self.app.listening_indicator.set_position, new_indicator_pos)
            if new_indicator_enabled and not old_indicator_enabled:
                self.app._schedule_ui(self.app.listening_indicator.show)
            elif not new_indicator_enabled and old_indicator_enabled:
                self.app._schedule_ui(self.app.listening_indicator.hide)

        # Microphone
        mic_changed = False
        mic_name = self.mic_var.get()
        if mic_name:
            for mic in self.sw.available_mics:
                if mic['name'] == mic_name:
                    if self.app.config.get('microphone') != mic['id']:
                        mic_changed = True
                        self.app.update_config({'microphone': mic['id']}, save=False)
                    self.app.update_config({'microphone_name': mic['name']}, save=False)
                    break

        # Language + TTS voice auto-switch
        if self.language_var is not None:
            lang_display = self.language_var.get()
            new_lang = self.LANG_DISPLAY_TO_CODE.get(lang_display, 'en')
            old_lang = self.app.config.get('language', 'en')
            self.app.update_config({'language': new_lang}, save=False)

            if new_lang != old_lang:
                old_default_voice = DEFAULT_TTS_VOICES.get(old_lang)
                new_default_voice = DEFAULT_TTS_VOICES.get(new_lang)
                current_voice = self.app.config.get('tts', {}).get('voice_id')
                # Only auto-switch if the user never customized the voice
                if new_default_voice and (
                    current_voice == old_default_voice or current_voice is None
                ):
                    tts_cfg = dict(self.app.config.get('tts', {}) or {})
                    tts_cfg['voice_id'] = new_default_voice
                    self.app.update_config({'tts': tts_cfg}, save=False)

        return {'new_model': new_model, 'mic_changed': mic_changed}
