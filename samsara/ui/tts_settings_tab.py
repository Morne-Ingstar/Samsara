"""TTS settings tab for Samsara Settings window.

Builds the "Text-to-Speech" tab and wires UI controls to config.
Keeps all tab logic here so settings_window.py only needs a one-line
registration and a one-block save delegate.

Usage (from SettingsWindow):
    from samsara.ui.tts_settings_tab import TTSSettingsTab
    self._tts_tab = TTSSettingsTab(self)     # in show()
    yield from self._tts_tab.build()         # in build_tts_tab()
    self._tts_tab.save()                     # in save_settings()
"""

import tkinter as tk
from pathlib import Path

import customtkinter as ctk


# Default values used when config keys are absent.
_DEFAULTS = {
    'enabled': False,
    'voice_id': None,
    'speed': 1.0,
    'pitch': 1.0,
    'volume': 0.8,
    'use_for_agent_responses': True,
    'use_for_confirmations': True,
    'use_for_warnings': True,
    'use_for_status_updates': True,
    'use_for_dictation_readback': False,
    'use_for_errors': True,
}

_TEST_PHRASE = "Note saved. Your reminder will be in the brain dump."


class TTSSettingsTab:
    """Manages the Text-to-Speech settings tab lifecycle."""

    def __init__(self, settings_window):
        self._sw = settings_window         # parent SettingsWindow
        self._app = settings_window.app

        # UI variables — set during build(), read during save()
        self.tts_enabled_var: tk.BooleanVar | None = None
        self.voice_var: tk.StringVar | None = None
        self.speed_var: tk.DoubleVar | None = None
        self.pitch_var: tk.DoubleVar | None = None
        self.volume_var: tk.DoubleVar | None = None

        self.use_agent_responses_var: tk.BooleanVar | None = None
        self.use_confirmations_var: tk.BooleanVar | None = None
        self.use_warnings_var: tk.BooleanVar | None = None
        self.use_status_updates_var: tk.BooleanVar | None = None
        self.use_dictation_readback_var: tk.BooleanVar | None = None
        self.use_errors_var: tk.BooleanVar | None = None

        # Internal: list of widgets to disable when TTS is off
        self._dependent_widgets: list = []

        # voice_id → voice label map (built during build())
        self._voice_label_to_id: dict[str, str] = {}
        self._voice_id_to_label: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Build (generator -- yields between sections for staged loading)
    # ------------------------------------------------------------------

    def build(self):
        """Generator that yields between sections for staged loading."""
        cfg = self._app.config.get('tts', {})
        tab = self._sw.tabview.tab("Text-to-Speech")

        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill='both', expand=True)

        yield from self._build_master_toggle(scroll, cfg)
        yield from self._build_voice_section(scroll, cfg)
        yield from self._build_sliders_section(scroll, cfg)
        yield from self._build_test_button(scroll)
        yield from self._build_advanced_section(scroll, cfg)

        # Apply initial enable state so widgets start with the right state
        self._apply_enabled_state()

    def _build_master_toggle(self, parent, cfg):
        ctk.CTkLabel(parent, text="Text-to-Speech",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(15, 10))

        toggle_frame = ctk.CTkFrame(parent, corner_radius=10)
        toggle_frame.pack(fill='x', pady=(0, 5))

        self.tts_enabled_var = tk.BooleanVar(value=bool(cfg.get('enabled', _DEFAULTS['enabled'])))
        ctk.CTkCheckBox(
            toggle_frame,
            text="Enable text-to-speech",
            variable=self.tts_enabled_var,
            command=self._apply_enabled_state,
        ).pack(anchor='w', padx=15, pady=(15, 8))

        ctk.CTkLabel(
            toggle_frame,
            text="Restart Samsara to apply changes to this setting.",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).pack(anchor='w', padx=15, pady=(0, 15))
        yield

    def _build_voice_section(self, parent, cfg):
        ctk.CTkLabel(parent, text="Voice",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        voice_frame = ctk.CTkFrame(parent, corner_radius=10)
        voice_frame.pack(fill='x', pady=(0, 20))

        engine = getattr(self._app, 'tts_engine', None)
        voice_labels = []
        current_voice_id = cfg.get('voice_id')
        initial_label = "No voices available — enable TTS and restart to populate"

        if engine is not None:
            try:
                for v in engine.list_voices():
                    label = f"{v.display_name} ({v.language})"
                    voice_labels.append(label)
                    self._voice_label_to_id[label] = v.voice_id
                    self._voice_id_to_label[v.voice_id] = label
            except Exception:
                pass

        if voice_labels:
            # Resolve saved voice_id to a label; fall back to first available
            initial_label = (
                self._voice_id_to_label.get(current_voice_id, voice_labels[0])
                if current_voice_id
                else voice_labels[0]
            )

        self.voice_var = tk.StringVar(value=initial_label)

        voice_row = ctk.CTkFrame(voice_frame, fg_color="transparent")
        voice_row.pack(fill='x', padx=15, pady=(15, 15))
        ctk.CTkLabel(voice_row, text="Voice:", width=80, anchor='w').pack(side='left')

        combo_values = voice_labels if voice_labels else [initial_label]
        voice_combo = ctk.CTkComboBox(
            voice_row,
            variable=self.voice_var,
            values=combo_values,
            width=350,
            state='readonly' if voice_labels else 'disabled',
        )
        voice_combo.pack(side='left')
        self._dependent_widgets.append(voice_combo)
        yield

    def _build_sliders_section(self, parent, cfg):
        ctk.CTkLabel(parent, text="Voice tuning",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        sliders_frame = ctk.CTkFrame(parent, corner_radius=10)
        sliders_frame.pack(fill='x', pady=(0, 20))

        sliders = [
            ('speed', 'Speed',  self._make_speed_var(cfg), 0.5, 2.0, 0.1),
            ('pitch', 'Pitch',  self._make_pitch_var(cfg), 0.5, 2.0, 0.1),
            ('volume', 'Volume', self._make_volume_var(cfg), 0.0, 1.0, 0.05),
        ]

        for attr, label_text, var, from_, to, step in sliders:
            setattr(self, f'{attr}_var', var)

            row = ctk.CTkFrame(sliders_frame, fg_color="transparent")
            row.pack(fill='x', padx=15, pady=(12, 4))

            val_label = ctk.CTkLabel(row, text=f"{label_text}: {var.get():.2f}", width=120, anchor='w')
            val_label.pack(side='left')

            def _make_trace(v=var, lbl=val_label, lname=label_text):
                def _update(*_):
                    lbl.configure(text=f"{lname}: {v.get():.2f}")
                return _update

            var.trace_add('write', _make_trace())

            slider = ctk.CTkSlider(row, from_=from_, to=to, variable=var, width=280)
            slider.pack(side='left', padx=(8, 0))
            self._dependent_widgets.append(slider)

        # Bottom padding
        ctk.CTkLabel(sliders_frame, text="").pack(pady=4)
        yield

    def _make_speed_var(self, cfg):
        return tk.DoubleVar(value=float(cfg.get('speed', _DEFAULTS['speed'])))

    def _make_pitch_var(self, cfg):
        return tk.DoubleVar(value=float(cfg.get('pitch', _DEFAULTS['pitch'])))

    def _make_volume_var(self, cfg):
        return tk.DoubleVar(value=float(cfg.get('volume', _DEFAULTS['volume'])))

    def _build_test_button(self, parent):
        test_frame = ctk.CTkFrame(parent, corner_radius=10)
        test_frame.pack(fill='x', pady=(0, 20))

        test_btn = ctk.CTkButton(
            test_frame,
            text="Test voice",
            width=160,
            command=self._on_test_clicked,
        )
        test_btn.pack(anchor='w', padx=15, pady=15)
        self._dependent_widgets.append(test_btn)
        self._test_status_label = ctk.CTkLabel(
            test_frame, text="", text_color="gray",
            font=ctk.CTkFont(size=11))
        self._test_status_label.pack(anchor='w', padx=15, pady=(0, 15))
        yield

    def _build_advanced_section(self, parent, cfg):
        """Per-context toggles — plumbing for Phase 2 category-driven behavior.

        These checkboxes write to config but AudioCoordinator does not yet
        read them. Phase 2 (queue semantics + category-driven behavior) will
        wire them. They're included now so the Settings UI is stable when
        Phase 2 lands.
        """
        # Collapsible container
        advanced_toggle_btn = ctk.CTkButton(
            parent,
            text="Advanced — when should Samsara speak?  ▶",
            fg_color="transparent",
            text_color=("gray30", "gray70"),
            hover_color=("gray90", "gray20"),
            anchor='w',
            command=lambda: _toggle(),
        )
        advanced_toggle_btn.pack(anchor='w', pady=(0, 4))

        advanced_frame = ctk.CTkFrame(parent, corner_radius=10)
        # Start collapsed

        def _toggle():
            if advanced_frame.winfo_ismapped():
                advanced_frame.pack_forget()
                advanced_toggle_btn.configure(
                    text="Advanced — when should Samsara speak?  ▶")
            else:
                advanced_frame.pack(fill='x', pady=(0, 20))
                advanced_toggle_btn.configure(
                    text="Advanced — when should Samsara speak?  ▼")

        note_text = ("These settings are read by Phase 2 category-driven behavior. "
                     "They are saved to config but not yet acted on.")
        ctk.CTkLabel(
            advanced_frame, text=note_text,
            text_color="gray", font=ctk.CTkFont(size=11),
            wraplength=540, justify='left',
        ).pack(anchor='w', padx=15, pady=(12, 8))

        toggles = [
            ('use_agent_responses_var',   "Speak agent responses",          'use_for_agent_responses',     True),
            ('use_confirmations_var',     "Speak confirmations",            'use_for_confirmations',       True),
            ('use_warnings_var',          "Speak warnings",                 'use_for_warnings',            True),
            ('use_status_updates_var',    "Speak status updates (Thinking…)", 'use_for_status_updates',   True),
            ('use_dictation_readback_var',"Speak dictation readback",       'use_for_dictation_readback',  False),
            ('use_errors_var',            "Speak errors",                   'use_for_errors',              True),
        ]

        for attr, label_text, cfg_key, default in toggles:
            var = tk.BooleanVar(value=bool(cfg.get(cfg_key, default)))
            setattr(self, attr, var)
            cb = ctk.CTkCheckBox(advanced_frame, text=label_text, variable=var)
            cb.pack(anchor='w', padx=15, pady=(0, 6))
            self._dependent_widgets.append(cb)

        ctk.CTkLabel(advanced_frame, text="").pack(pady=4)
        yield

    # ------------------------------------------------------------------
    # Enable/disable cascade
    # ------------------------------------------------------------------

    def _apply_enabled_state(self):
        """Gray out dependent controls when TTS is disabled."""
        if self.tts_enabled_var is None:
            return
        enabled = self.tts_enabled_var.get()
        new_state = 'normal' if enabled else 'disabled'
        for widget in self._dependent_widgets:
            try:
                widget.configure(state=new_state)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Test button
    # ------------------------------------------------------------------

    def _on_test_clicked(self):
        coordinator = getattr(self._app, 'audio_coordinator', None)
        if coordinator is None:
            self._set_test_status("TTS not initialized — restart with TTS enabled to test.")
            return

        voice_label = self.voice_var.get() if self.voice_var else None
        voice_id = self._voice_label_to_id.get(voice_label) if voice_label else None
        speed = float(self.speed_var.get()) if self.speed_var else 1.0
        volume = float(self.volume_var.get()) if self.volume_var else 0.8

        try:
            coordinator.speak(
                _TEST_PHRASE,
                voice_id=voice_id,
                speed=speed,
                volume=volume,
                category="general",
            )
            self._set_test_status(f"Speaking: \"{_TEST_PHRASE}\"")
        except Exception as e:
            self._set_test_status(f"Error: {e}")

    def _set_test_status(self, msg: str):
        if hasattr(self, '_test_status_label'):
            try:
                self._test_status_label.configure(text=msg)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self):
        """Collect UI state into the app config. Called by save_settings()."""
        if self.tts_enabled_var is None:
            return  # Tab was never visited

        cfg = dict(self._app.config.get('tts', {}) or {})

        cfg['enabled'] = bool(self.tts_enabled_var.get())

        # Voice: map label back to voice_id (None if unchanged or not available)
        if self.voice_var is not None:
            label = self.voice_var.get()
            cfg['voice_id'] = self._voice_label_to_id.get(label) or None

        if self.speed_var is not None:
            cfg['speed'] = round(float(self.speed_var.get()), 2)
        if self.pitch_var is not None:
            cfg['pitch'] = round(float(self.pitch_var.get()), 2)
        if self.volume_var is not None:
            cfg['volume'] = round(float(self.volume_var.get()), 2)

        # Per-context toggles (Phase 2 plumbing)
        for attr, cfg_key in [
            ('use_agent_responses_var',   'use_for_agent_responses'),
            ('use_confirmations_var',     'use_for_confirmations'),
            ('use_warnings_var',          'use_for_warnings'),
            ('use_status_updates_var',    'use_for_status_updates'),
            ('use_dictation_readback_var','use_for_dictation_readback'),
            ('use_errors_var',            'use_for_errors'),
        ]:
            var = getattr(self, attr, None)
            if var is not None:
                cfg[cfg_key] = bool(var.get())

        self._app.update_config({'tts': cfg}, save=False)
