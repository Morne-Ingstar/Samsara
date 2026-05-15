"""
Advanced settings tab: Continuous Mode, Speech Threshold, Hardware,
Performance, Echo Cancellation, Wake Word Settings.
"""

import threading
import tkinter as tk

import customtkinter as ctk


class AdvancedTab:
    """Advanced settings tab: Continuous Mode, Speech Threshold, Hardware,
    Performance, Echo Cancellation, Wake Word Settings."""

    def __init__(self, parent_frame, app):
        """
        parent_frame: the CTkScrollableFrame or frame that this tab should
                      pack its widgets into.
        app:          the DictationApp instance (for config read/write).
        """
        self.app = app
        self.parent = parent_frame
        self._built = False
        self._window = None

    def build(self):
        """Generator: build the tab UI in sections, yielding between each
        to allow the caller to interleave with other work (lazy loading).
        Mirrors the existing yield pattern from build_advanced_tab()."""
        self._window = self.parent.winfo_toplevel()

        advanced_scroll = ctk.CTkScrollableFrame(self.parent, fg_color="transparent")
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
        yield

        # --- Speech Threshold Calibration ---
        cal_label = ctk.CTkLabel(advanced_scroll, text="Speech Threshold",
                                  font=ctk.CTkFont(size=16, weight="bold"))
        cal_label.pack(anchor='w', pady=(0, 10))

        cal_frame = ctk.CTkFrame(advanced_scroll, corner_radius=10)
        cal_frame.pack(fill='x', pady=(0, 20))

        # Mode radio buttons
        self.threshold_mode_var = tk.StringVar(
            value=self.app.config.get('threshold_mode', 'auto'))

        ctk.CTkRadioButton(cal_frame, text="Auto-calibrate on startup (Recommended)",
                           variable=self.threshold_mode_var, value='auto',
                           command=self._on_threshold_mode_change
                           ).pack(anchor='w', padx=15, pady=(15, 5))
        ctk.CTkRadioButton(cal_frame, text="Manual (use custom value)",
                           variable=self.threshold_mode_var, value='manual',
                           command=self._on_threshold_mode_change
                           ).pack(anchor='w', padx=15, pady=(0, 10))

        # Auto section: current value + recalibrate button
        self._cal_auto_frame = ctk.CTkFrame(cal_frame, fg_color="transparent")
        self._cal_auto_frame.pack(fill='x', padx=15, pady=(0, 10))
        current_thresh = self.app.config.get('wake_word_config', {}).get(
            'audio', {}).get('speech_threshold', 0.03)
        self._cal_value_label = ctk.CTkLabel(self._cal_auto_frame,
                                              text=f"Current: {current_thresh:.4f}",
                                              text_color="#00CED1")
        self._cal_value_label.pack(side='left', padx=(0, 15))
        ctk.CTkButton(self._cal_auto_frame, text="Recalibrate Now", width=130,
                      command=self._recalibrate_from_settings).pack(side='left')

        # Manual section: threshold entry
        self._cal_manual_frame = ctk.CTkFrame(cal_frame, fg_color="transparent")
        self._cal_manual_frame.pack(fill='x', padx=15, pady=(0, 10))
        ctk.CTkLabel(self._cal_manual_frame, text="Threshold:", width=80,
                     anchor='w').pack(side='left')
        self.manual_threshold_var = tk.DoubleVar(value=current_thresh)
        ctk.CTkEntry(self._cal_manual_frame, textvariable=self.manual_threshold_var,
                     width=100).pack(side='left', padx=(0, 10))
        ctk.CTkLabel(self._cal_manual_frame, text="(0.005 - 0.20)",
                     text_color="gray").pack(side='left')

        # Sensitivity multiplier (power-user)
        mult_row = ctk.CTkFrame(cal_frame, fg_color="transparent")
        mult_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(mult_row, text="Sensitivity multiplier:", width=150,
                     anchor='w').pack(side='left')
        self.cal_multiplier_var = tk.DoubleVar(
            value=self.app.config.get('cal_multiplier', 3.0))
        ctk.CTkSlider(mult_row, from_=1.5, to=6.0,
                      variable=self.cal_multiplier_var, width=150).pack(side='left', padx=(0, 10))
        self._cal_mult_label = ctk.CTkLabel(mult_row, text=f"{self.cal_multiplier_var.get():.1f}x",
                                             width=40)
        self._cal_mult_label.pack(side='left')
        self.cal_multiplier_var.trace_add('write', lambda *_: self._cal_mult_label.configure(
            text=f"{self.cal_multiplier_var.get():.1f}x"))

        # Show/hide based on mode
        self._on_threshold_mode_change()
        yield

        # --- Hardware Acceleration Section ---
        hw_label = ctk.CTkLabel(advanced_scroll, text="Hardware Acceleration", font=ctk.CTkFont(size=16, weight="bold"))
        hw_label.pack(anchor='w', pady=(0, 10))

        hw_frame = ctk.CTkFrame(advanced_scroll, corner_radius=10)
        hw_frame.pack(fill='x', pady=(0, 20))

        device_row = ctk.CTkFrame(hw_frame, fg_color="transparent")
        device_row.pack(fill='x', padx=15, pady=(15, 5))
        ctk.CTkLabel(device_row, text="Compute device:", width=150, anchor='w').pack(side='left')

        # Device options -- CUDA is only offered if the runtime DLLs are
        # actually present. This prevents users from selecting CUDA, restarting,
        # and hitting "cublas64_12.dll not found" at model-load time.
        from samsara.cuda_detect import is_cuda_available, cuda_status_message
        self.device_display_to_value = {'CPU': 'cpu'}
        if is_cuda_available():
            self.device_display_to_value['CUDA (NVIDIA GPU)'] = 'cuda'
        self.device_value_to_display = {v: k for k, v in self.device_display_to_value.items()}

        # If config says cuda but DLLs are missing, surface as CPU in the UI.
        # The model loader will silently fall back at runtime (see resolve_device).
        current_device = self.app.config.get('device', 'cpu')
        if current_device == 'cuda' and not is_cuda_available():
            current_device_display = 'CPU'
        else:
            current_device_display = self.device_value_to_display.get(current_device, 'CPU')

        self.device_var = tk.StringVar(value=current_device_display)
        device_combo = ctk.CTkComboBox(device_row, variable=self.device_var,
                                        values=list(self.device_display_to_value.keys()),
                                        width=180, state='readonly')
        device_combo.pack(side='left')

        # Status hint reflecting actual CUDA availability, not just docs
        ctk.CTkLabel(hw_frame, text=cuda_status_message(),
                    text_color="gray", wraplength=600, justify='left').pack(
                        anchor='w', padx=15, pady=(0, 5))
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
        cuda_text.configure(state='disabled')

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
        yield

        # --- Echo Cancellation Settings ---
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
        yield

        # --- Wake Word Settings ---
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

        phrase_options = list(ww_config.get('phrase_options', ['jarvis', 'hey jarvis', 'computer', 'hey computer', 'samsa', 'hey samsa']))
        current_phrase = ww_config.get('phrase', 'jarvis')
        # Track the list as instance state so Add/Remove can mutate it.
        self._phrase_options = list(phrase_options)
        self.wake_phrase_var = tk.StringVar(value=current_phrase)
        self._wake_phrase_dropdown = ctk.CTkComboBox(
            wake_word_row, variable=self.wake_phrase_var,
            values=self._phrase_options, width=150)
        self._wake_phrase_dropdown.pack(side='left', padx=(0, 10))
        ctk.CTkLabel(wake_word_row, text="(active wake phrase)", text_color="gray").pack(side='left')

        # Manage list of available wake phrases
        manage_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        manage_row.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(manage_row, text="Add phrase:", width=120, anchor='w').pack(side='left')
        self._new_phrase_var = tk.StringVar()
        new_phrase_entry = ctk.CTkEntry(
            manage_row, textvariable=self._new_phrase_var, width=150,
            placeholder_text="e.g. computer")
        new_phrase_entry.pack(side='left', padx=(0, 6))

        def _add_phrase():
            phrase = self._new_phrase_var.get().strip().lower()
            if not phrase:
                return
            if phrase in self._phrase_options:
                self._new_phrase_var.set("")
                self.wake_phrase_var.set(phrase)
                return
            self._phrase_options.append(phrase)
            self._wake_phrase_dropdown.configure(values=self._phrase_options)
            self.wake_phrase_var.set(phrase)
            self._new_phrase_var.set("")

        def _remove_phrase():
            phrase = self.wake_phrase_var.get().strip().lower()
            if not phrase or phrase not in self._phrase_options:
                return
            if len(self._phrase_options) <= 1:
                # Don't let the list become empty -- the matcher needs at
                # least one wake phrase or the wake-word system breaks.
                return
            self._phrase_options.remove(phrase)
            self._wake_phrase_dropdown.configure(values=self._phrase_options)
            # Pick the first remaining option as the new active phrase.
            self.wake_phrase_var.set(self._phrase_options[0])

        ctk.CTkButton(manage_row, text="Add", width=60, command=_add_phrase).pack(side='left', padx=(0, 4))
        ctk.CTkButton(manage_row, text="Remove selected", width=120, command=_remove_phrase).pack(side='left')

        # --- 4-State Dictation Settings ---
        dict_label = ctk.CTkLabel(wake_frame, text="Dictation Settings",
                                  font=ctk.CTkFont(size=13, weight="bold"))
        dict_label.pack(anchor='w', padx=15, pady=(5, 8))

        # Quick Dictation silence timeout
        quick_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        quick_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(quick_row, text="Quick Dictation timeout:", width=180, anchor='w').pack(side='left')
        self.quick_timeout_var = tk.DoubleVar(
            value=ww_config.get('quick_silence_timeout', 1.0))
        ctk.CTkEntry(quick_row, textvariable=self.quick_timeout_var, width=60).pack(side='left', padx=(0, 5))
        ctk.CTkLabel(quick_row, text="sec", width=30).pack(side='left')
        ctk.CTkLabel(quick_row, text="(silence before auto-finish)",
                     text_color="gray").pack(side='left', padx=(5, 0))

        # End words
        end_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        end_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(end_row, text="End words:", width=180, anchor='w').pack(side='left')
        end_words = ww_config.get('end_words', ['over', 'done', 'end dictation'])
        self.end_words_var = tk.StringVar(value=', '.join(end_words))
        ctk.CTkEntry(end_row, textvariable=self.end_words_var, width=300).pack(side='left')
        ctk.CTkLabel(end_row, text="(finish Long Dictation)",
                     text_color="gray").pack(side='left', padx=(5, 0))

        # Cancel words
        cancel_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        cancel_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(cancel_row, text="Cancel words:", width=180, anchor='w').pack(side='left')
        cancel_words = ww_config.get('cancel_words', ['cancel', 'cancel dictation', 'abort'])
        self.cancel_words_var = tk.StringVar(value=', '.join(cancel_words))
        ctk.CTkEntry(cancel_row, textvariable=self.cancel_words_var, width=300).pack(side='left')
        ctk.CTkLabel(cancel_row, text="(discard current dictation)",
                     text_color="gray").pack(side='left', padx=(5, 0))

        # Pause words
        pause_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        pause_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(pause_row, text="Pause words:", width=180, anchor='w').pack(side='left')
        pause_words = ww_config.get('pause_words', ['pause', 'hold on', 'wait'])
        self.pause_words_var = tk.StringVar(value=', '.join(pause_words))
        ctk.CTkEntry(pause_row, textvariable=self.pause_words_var, width=300).pack(side='left')
        ctk.CTkLabel(pause_row, text="(reset silence timer)",
                     text_color="gray").pack(side='left', padx=(5, 0))

        # Resume words
        resume_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        resume_row.pack(fill='x', padx=15, pady=(0, 12))
        ctk.CTkLabel(resume_row, text="Resume words:", width=180, anchor='w').pack(side='left')
        resume_words = ww_config.get('resume_words', ['resume', 'continue', 'go on'])
        self.resume_words_var = tk.StringVar(value=', '.join(resume_words))
        ctk.CTkEntry(resume_row, textvariable=self.resume_words_var, width=300).pack(side='left')
        ctk.CTkLabel(resume_row, text="(not yet active)",
                     text_color="gray").pack(side='left', padx=(5, 0))

        # Test/Debug button
        debug_row = ctk.CTkFrame(wake_frame, fg_color="transparent")
        debug_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkButton(debug_row, text="Test Wake Word...", width=150,
                     command=self.app.open_wake_word_debug).pack(side='left')
        ctk.CTkLabel(debug_row, text="Live testing and parameter tuning",
                    text_color="gray").pack(side='left', padx=(10, 0))

        self._built = True

    def save(self):
        """Read all tk.Vars and write the corresponding config keys via
        self.app.update_config(). Called by SettingsWindow.save_settings()."""
        if not self._built:
            return {
                'device_changed': False,
                'old_device': self.app.config.get('device', 'cpu'),
                'new_device': self.app.config.get('device', 'cpu'),
            }

        old_device = self.app.config.get('device', 'cpu')
        device_display = self.device_var.get()
        new_device = self.device_display_to_value.get(device_display, 'cpu')
        device_changed = old_device != new_device

        self.app.update_config({
            'silence_threshold': self.silence_var.get(),
            'min_speech_duration': self.min_speech_var.get(),
            'device': new_device,
            'performance_mode': self.perf_mode_var.get(),
            'threshold_mode': self.threshold_mode_var.get(),
            'cal_multiplier': self.cal_multiplier_var.get(),
        }, save=False)

        self.app.update_config({
            'echo_cancellation': {
                'enabled': self.aec_enabled_var.get(),
                'latency_ms': self.aec_latency_var.get(),
            },
        }, save=False)

        ww_config = self.app.config.get('wake_word_config', {})
        ww_config['phrase'] = self.wake_phrase_var.get()
        # Save the user-managed list of wake phrases. If the active
        # phrase was typed but never added via the Add button, include
        # it so the user's choice survives.
        phrases = list(self._phrase_options)
        active = self.wake_phrase_var.get().strip().lower()
        if active and active not in phrases:
            phrases.append(active)
        ww_config['phrase_options'] = phrases
        ww_config['quick_silence_timeout'] = float(self.quick_timeout_var.get())
        ww_config['end_words'] = [w.strip() for w in self.end_words_var.get().split(',') if w.strip()]
        ww_config['cancel_words'] = [w.strip() for w in self.cancel_words_var.get().split(',') if w.strip()]
        ww_config['pause_words'] = [w.strip() for w in self.pause_words_var.get().split(',') if w.strip()]
        ww_config['resume_words'] = [w.strip() for w in self.resume_words_var.get().split(',') if w.strip()]
        # Remove old-format keys if they exist
        for old_key in ('end_word', 'cancel_word', 'pause_word', 'modes'):
            ww_config.pop(old_key, None)

        # Apply manual threshold if in manual mode
        if self.threshold_mode_var.get() == 'manual':
            manual_val = self.manual_threshold_var.get()
            manual_val = max(0.005, min(0.20, manual_val))
            if 'audio' not in ww_config:
                ww_config['audio'] = {}
            ww_config['audio']['speech_threshold'] = manual_val

        self.app.update_config({'wake_word_config': ww_config}, save=False)

        return {
            'device_changed': device_changed,
            'old_device': old_device,
            'new_device': new_device,
        }

    def _on_threshold_mode_change(self):
        """Show/hide auto vs manual calibration controls."""
        mode = self.threshold_mode_var.get()
        if mode == 'auto':
            self._cal_auto_frame.pack(fill='x', padx=15, pady=(0, 10))
            self._cal_manual_frame.pack_forget()
        else:
            self._cal_auto_frame.pack_forget()
            self._cal_manual_frame.pack(fill='x', padx=15, pady=(0, 10))

    def _recalibrate_from_settings(self):
        """Run calibration and update the display label."""
        self._cal_value_label.configure(text="Calibrating...")
        def _do():
            try:
                had_wake = getattr(self.app, 'wake_word_active', False)
                had_continuous = getattr(self.app, 'continuous_active', False)
                if had_wake:
                    self.app.stop_wake_word_mode()
                if had_continuous:
                    self.app.stop_continuous_mode()

                import time
                time.sleep(0.3)  # let streams release the device

                self.app._run_calibration_if_auto()
                self.app.persist_config()

                # Restart streams that were active
                if had_wake:
                    self.app.start_wake_word_mode()
                if had_continuous:
                    self.app.start_continuous_mode()

                thresh = self.app.config.get('wake_word_config', {}).get(
                    'audio', {}).get('speech_threshold', 0.03)
                try:
                    if self._window and self._window.winfo_exists():
                        self._window.after(0, self._cal_value_label.configure,
                                         {"text": f"Current: {thresh:.4f}"})
                except Exception:
                    pass
            except Exception as e:
                print(f"[CAL] Recalibration failed: {e}")
                try:
                    if self._window and self._window.winfo_exists():
                        self._window.after(0, self._cal_value_label.configure,
                                         {"text": f"Failed: {e}"})
                except Exception:
                    pass
        threading.Thread(target=_do, daemon=True).start()
