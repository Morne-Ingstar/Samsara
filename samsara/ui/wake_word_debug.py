"""
Wake Word Debug/Test Window for Samsara
Real-time debugging and tuning interface for wake word functionality.
"""

import tkinter as tk
import customtkinter as ctk
import numpy as np
import threading
import time
from datetime import datetime
import sounddevice as sd


class WakeWordDebugWindow:
    """Debug window for testing and tuning wake word detection."""
    
    def __init__(self, app):
        self.app = app
        self.window = None
        self.audio_stream = None
        self.running = False
        self.current_rms = 0.0
        self.state = "idle"
        self.wake_word_detected_time = None
        self.dictation_mode = None
        self.dictation_buffer = []
        self._dictation_start_time = None
        
    def show(self):
        """Show the debug window."""
        if self.window is not None:
            try:
                self.window.lift()
                self.window.focus_force()
                return
            except:
                self.window = None
        
        self.window = ctk.CTkToplevel()
        self.window.title("Wake Word Debug")
        self.window.geometry("700x900")
        self.window.resizable(True, True)
        self.window.minsize(650, 800)
        
        # Main scrollable container
        main_frame = ctk.CTkScrollableFrame(self.window, fg_color="transparent")
        main_frame.pack(fill='both', expand=True, padx=15, pady=15)
        
        # Get wake word config
        ww_config = self.app.config.get('wake_word_config', {})
        
        # =====================================================================
        # Status Section
        # =====================================================================
        status_label = ctk.CTkLabel(main_frame, text="Status", 
                                    font=ctk.CTkFont(size=16, weight="bold"))
        status_label.pack(anchor='w', pady=(0, 10))
        
        status_frame = ctk.CTkFrame(main_frame, corner_radius=10)
        status_frame.pack(fill='x', pady=(0, 15))
        
        # State indicator
        state_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        state_row.pack(fill='x', padx=15, pady=(15, 8))
        ctk.CTkLabel(state_row, text="State:", width=120, anchor='w').pack(side='left')
        self.state_label = ctk.CTkLabel(state_row, text="Idle", 
                                        text_color="#888888",
                                        font=ctk.CTkFont(weight="bold"))
        self.state_label.pack(side='left')
        
        # Wake word display
        wake_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        wake_row.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(wake_row, text="Wake phrase:", width=120, anchor='w').pack(side='left')
        wake_phrase = ww_config.get('phrase', 'samsara')
        self.wake_word_label = ctk.CTkLabel(wake_row, text=f'"{wake_phrase}"',
                                            text_color="#00CED1")
        self.wake_word_label.pack(side='left')
        
        # End word display
        end_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        end_row.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(end_row, text="End word:", width=120, anchor='w').pack(side='left')
        end_config = ww_config.get('end_word', {})
        if end_config.get('enabled', False):
            end_text = f'"{end_config.get("phrase", "over")}"'
            end_color = "#00CED1"
        else:
            end_text = "(disabled)"
            end_color = "#888888"
        self.end_word_label = ctk.CTkLabel(end_row, text=end_text, text_color=end_color)
        self.end_word_label.pack(side='left')
        
        # Dictation mode display
        mode_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        mode_row.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(mode_row, text="Dictation mode:", width=120, anchor='w').pack(side='left')
        self.mode_label = ctk.CTkLabel(mode_row, text="None", text_color="#888888")
        self.mode_label.pack(side='left')
        
        # Timer display
        timer_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        timer_row.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(timer_row, text="Timer:", width=120, anchor='w').pack(side='left')
        self.timer_label = ctk.CTkLabel(timer_row, text="--", text_color="#888888")
        self.timer_label.pack(side='left')

        # Flow indicator
        flow_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        flow_row.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(flow_row, text="Flow:", width=120, anchor='w').pack(side='left')
        self.flow_label = ctk.CTkLabel(flow_row, text="Idle", text_color="#888888",
                                        wraplength=400, justify='left')
        self.flow_label.pack(side='left', fill='x', expand=True)

        # Last heard
        heard_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        heard_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(heard_row, text="Last heard:", width=120, anchor='w').pack(side='left')
        self.last_heard_label = ctk.CTkLabel(heard_row, text="(nothing yet)",
                                             text_color="#888888",
                                             wraplength=400, justify='left')
        self.last_heard_label.pack(side='left', fill='x', expand=True)
        
        # =====================================================================
        # Audio Level Section
        # =====================================================================
        level_label = ctk.CTkLabel(main_frame, text="Audio Level", 
                                   font=ctk.CTkFont(size=16, weight="bold"))
        level_label.pack(anchor='w', pady=(0, 10))
        
        level_frame = ctk.CTkFrame(main_frame, corner_radius=10)
        level_frame.pack(fill='x', pady=(0, 15))
        
        # Level bar
        bar_frame = ctk.CTkFrame(level_frame, fg_color="transparent")
        bar_frame.pack(fill='x', padx=15, pady=(15, 5))
        
        self.level_bar = ctk.CTkProgressBar(bar_frame, width=400, height=25)
        self.level_bar.pack(side='left', fill='x', expand=True)
        self.level_bar.set(0)
        
        self.level_value = ctk.CTkLabel(bar_frame, text="0.000", width=60)
        self.level_value.pack(side='left', padx=(10, 0))
        
        # Threshold indicator
        thresh_row = ctk.CTkFrame(level_frame, fg_color="transparent")
        thresh_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(thresh_row, text="Speech threshold:", width=120, anchor='w').pack(side='left')
        audio_config = ww_config.get('audio', {})
        thresh_val = audio_config.get('speech_threshold', 0.01)
        self.thresh_indicator = ctk.CTkLabel(thresh_row, text=f"{thresh_val:.3f}",
                                             text_color="#888888")
        self.thresh_indicator.pack(side='left')
        
        # =====================================================================
        # Parameters Section
        # =====================================================================
        params_label = ctk.CTkLabel(main_frame, text="Tunable Parameters", 
                                    font=ctk.CTkFont(size=16, weight="bold"))
        params_label.pack(anchor='w', pady=(0, 10))
        
        params_frame = ctk.CTkFrame(main_frame, corner_radius=10)
        params_frame.pack(fill='x', pady=(0, 15))
        
        # Speech threshold slider
        speech_row = ctk.CTkFrame(params_frame, fg_color="transparent")
        speech_row.pack(fill='x', padx=15, pady=(15, 10))
        ctk.CTkLabel(speech_row, text="Speech threshold:", width=150, anchor='w').pack(side='left')
        self.speech_thresh_var = ctk.DoubleVar(value=audio_config.get('speech_threshold', 0.01))
        self.speech_thresh_slider = ctk.CTkSlider(speech_row, from_=0.001, to=0.05,
                                                   variable=self.speech_thresh_var,
                                                   width=200,
                                                   command=self.on_speech_thresh_change)
        self.speech_thresh_slider.pack(side='left', padx=(0, 10))
        self.speech_thresh_label = ctk.CTkLabel(speech_row, text=f"{self.speech_thresh_var.get():.3f}", width=50)
        self.speech_thresh_label.pack(side='left')
        
        # Min speech duration slider
        min_speech_row = ctk.CTkFrame(params_frame, fg_color="transparent")
        min_speech_row.pack(fill='x', padx=15, pady=(0, 10))
        ctk.CTkLabel(min_speech_row, text="Min speech (s):", width=150, anchor='w').pack(side='left')
        self.min_speech_var = ctk.DoubleVar(value=audio_config.get('min_speech_duration', 0.3))
        self.min_speech_slider = ctk.CTkSlider(min_speech_row, from_=0.1, to=2.0,
                                                variable=self.min_speech_var,
                                                width=200,
                                                command=self.on_min_speech_change)
        self.min_speech_slider.pack(side='left', padx=(0, 10))
        self.min_speech_label = ctk.CTkLabel(min_speech_row, text=f"{self.min_speech_var.get():.1f}", width=50)
        self.min_speech_label.pack(side='left')
        
        # Mode timeouts header
        ctk.CTkLabel(params_frame, text="Mode Timeouts:", 
                    font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=15, pady=(10, 5))
        
        modes_config = ww_config.get('modes', {})
        
        # Dictate timeout
        dictate_row = ctk.CTkFrame(params_frame, fg_color="transparent")
        dictate_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(dictate_row, text="dictate:", width=100, anchor='w').pack(side='left')
        self.dictate_timeout_var = ctk.DoubleVar(value=modes_config.get('dictate', {}).get('silence_timeout', 2.0))
        ctk.CTkSlider(dictate_row, from_=0.5, to=10.0, variable=self.dictate_timeout_var,
                     width=180, command=lambda v: self.dictate_timeout_label.configure(text=f"{v:.1f}s")).pack(side='left', padx=(0, 10))
        self.dictate_timeout_label = ctk.CTkLabel(dictate_row, text=f"{self.dictate_timeout_var.get():.1f}s", width=40)
        self.dictate_timeout_label.pack(side='left')
        
        # Short dictate timeout
        short_row = ctk.CTkFrame(params_frame, fg_color="transparent")
        short_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(short_row, text="short dictate:", width=100, anchor='w').pack(side='left')
        self.short_timeout_var = ctk.DoubleVar(value=modes_config.get('short_dictate', {}).get('silence_timeout', 1.0))
        ctk.CTkSlider(short_row, from_=0.3, to=5.0, variable=self.short_timeout_var,
                     width=180, command=lambda v: self.short_timeout_label.configure(text=f"{v:.1f}s")).pack(side='left', padx=(0, 10))
        self.short_timeout_label = ctk.CTkLabel(short_row, text=f"{self.short_timeout_var.get():.1f}s", width=40)
        self.short_timeout_label.pack(side='left')
        
        # Long dictate timeout
        long_row = ctk.CTkFrame(params_frame, fg_color="transparent")
        long_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(long_row, text="long dictate:", width=100, anchor='w').pack(side='left')
        self.long_timeout_var = ctk.DoubleVar(value=modes_config.get('long_dictate', {}).get('silence_timeout', 60.0))
        ctk.CTkSlider(long_row, from_=10.0, to=120.0, variable=self.long_timeout_var,
                     width=180, command=lambda v: self.long_timeout_label.configure(text=f"{v:.0f}s")).pack(side='left', padx=(0, 10))
        self.long_timeout_label = ctk.CTkLabel(long_row, text=f"{self.long_timeout_var.get():.0f}s", width=40)
        self.long_timeout_label.pack(side='left')
        
        # =====================================================================
        # Test Controls Section
        # =====================================================================
        test_label = ctk.CTkLabel(main_frame, text="Test Controls",
                                  font=ctk.CTkFont(size=16, weight="bold"))
        test_label.pack(anchor='w', pady=(0, 10))

        test_frame = ctk.CTkFrame(main_frame, corner_radius=10)
        test_frame.pack(fill='x', pady=(0, 15))

        # Mode selector
        mode_row = ctk.CTkFrame(test_frame, fg_color="transparent")
        mode_row.pack(fill='x', padx=15, pady=(15, 8))
        ctk.CTkLabel(mode_row, text="Force mode:", width=100, anchor='w').pack(side='left')
        self.test_mode_var = tk.StringVar(value="(auto)")
        mode_dropdown = ctk.CTkComboBox(mode_row, variable=self.test_mode_var,
                                         values=["(auto)", "dictate", "short_dictate", "long_dictate"],
                                         width=150)
        mode_dropdown.pack(side='left', padx=(0, 10))
        ctk.CTkButton(mode_row, text="Enter Mode", width=90,
                     command=self._force_enter_mode).pack(side='left', padx=(0, 10))
        ctk.CTkButton(mode_row, text="Reset", width=70,
                     fg_color="gray40", hover_color="gray30",
                     command=self._reset_test_mode).pack(side='left')

        # Test word buttons
        word_row = ctk.CTkFrame(test_frame, fg_color="transparent")
        word_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(word_row, text="Simulate:", width=100, anchor='w').pack(side='left')
        ctk.CTkButton(word_row, text="End Word", width=80,
                     command=lambda: self._simulate_word('end')).pack(side='left', padx=(0, 5))
        ctk.CTkButton(word_row, text="Cancel Word", width=90,
                     command=lambda: self._simulate_word('cancel')).pack(side='left', padx=(0, 5))
        ctk.CTkButton(word_row, text="Pause Word", width=85,
                     command=lambda: self._simulate_word('pause')).pack(side='left')

        # =====================================================================
        # Log Section
        # =====================================================================
        log_label = ctk.CTkLabel(main_frame, text="Event Log",
                                 font=ctk.CTkFont(size=16, weight="bold"))
        log_label.pack(anchor='w', pady=(0, 10))
        
        log_frame = ctk.CTkFrame(main_frame, corner_radius=10)
        log_frame.pack(fill='both', expand=True, pady=(0, 15))
        
        self.log_text = ctk.CTkTextbox(log_frame, height=180, state='disabled')
        self.log_text.pack(fill='both', expand=True, padx=10, pady=10)
        
        # =====================================================================
        # Control Buttons
        # =====================================================================
        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(fill='x')
        
        self.start_btn = ctk.CTkButton(btn_frame, text="Start Test", width=120,
                                       fg_color="#228B22", hover_color="#2E8B2E",
                                       command=self.start_test)
        self.start_btn.pack(side='left', padx=(0, 10))
        
        self.stop_btn = ctk.CTkButton(btn_frame, text="Stop", width=120,
                                      fg_color="#8B0000", hover_color="#A52A2A",
                                      command=self.stop_test, state='disabled')
        self.stop_btn.pack(side='left', padx=(0, 10))
        
        ctk.CTkButton(btn_frame, text="Clear Log", width=100,
                     fg_color="gray40", hover_color="gray30",
                     command=self.clear_log).pack(side='left', padx=(0, 10))
        
        ctk.CTkButton(btn_frame, text="Apply to Config", width=120,
                     command=self.apply_to_config).pack(side='right')
        
        # Handle window close
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Log initial state
        self.log(f"Wake phrase: \"{wake_phrase}\"")
        if end_config.get('enabled', False):
            self.log(f"End word: \"{end_config.get('phrase', 'over')}\"")
        self.log(f"Microphone: {self.app.config.get('microphone', 'default')}")
        self.log("Ready to test. Click 'Start Test' to begin.")
        self.log("")
        self.log("Commands to try:")
        self.log(f"  \"{wake_phrase}\" - wake word only")
        self.log(f"  \"{wake_phrase} dictate\" - start dictation")
        self.log(f"  \"{wake_phrase} short dictate\" - quick entry")
        self.log(f"  \"{wake_phrase} long dictate\" - extended (needs end word)")
    
    def log(self, message):
        """Add a message to the log."""
        if self.log_text is None:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state='normal')
        self.log_text.insert('end', f"[{timestamp}] {message}\n")
        self.log_text.see('end')
        self.log_text.configure(state='disabled')
    
    def clear_log(self):
        """Clear the log."""
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')
    
    def update_state(self, state, color="#888888"):
        """Update the state display."""
        self.state = state
        if self.state_label:
            self.state_label.configure(text=state, text_color=color)
    
    def update_mode(self, mode):
        """Update the dictation mode display."""
        self.dictation_mode = mode
        if self.mode_label:
            if mode:
                self.mode_label.configure(text=mode.replace('_', ' ').title(), text_color="#FFD700")
            else:
                self.mode_label.configure(text="None", text_color="#888888")
    
    def update_last_heard(self, text):
        """Update the last heard display."""
        if self.last_heard_label:
            display = text if len(text) < 60 else text[:57] + "..."
            self.last_heard_label.configure(text=f'"{display}"', text_color="white")
    
    def on_speech_thresh_change(self, value):
        """Handle speech threshold slider change."""
        self.speech_thresh_label.configure(text=f"{value:.3f}")
        self.thresh_indicator.configure(text=f"{value:.3f}")
    
    def on_min_speech_change(self, value):
        """Handle min speech slider change."""
        self.min_speech_label.configure(text=f"{value:.1f}")
    
    def apply_to_config(self):
        """Apply current slider values to app config."""
        ww_config = self.app.config.get('wake_word_config', {})
        
        # Update audio settings
        if 'audio' not in ww_config:
            ww_config['audio'] = {}
        ww_config['audio']['speech_threshold'] = self.speech_thresh_var.get()
        ww_config['audio']['min_speech_duration'] = self.min_speech_var.get()
        
        # Update mode timeouts
        if 'modes' not in ww_config:
            ww_config['modes'] = {}
        ww_config['modes']['dictate'] = {
            'silence_timeout': self.dictate_timeout_var.get(),
            'require_end_word': False
        }
        ww_config['modes']['short_dictate'] = {
            'silence_timeout': self.short_timeout_var.get(),
            'require_end_word': False
        }
        ww_config['modes']['long_dictate'] = {
            'silence_timeout': self.long_timeout_var.get(),
            'require_end_word': True
        }
        
        self.app.config['wake_word_config'] = ww_config
        self.app.save_config()
        self.log("Settings applied and saved to config!")
    
    def start_test(self):
        """Start wake word testing."""
        if not self.app.model_loaded:
            self.log("ERROR: Model not loaded yet. Please wait...")
            return
        
        self.running = True
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')

        self.speech_buffer = []
        self.silence_start = None
        self.is_speaking = False
        self.wake_word_triggered = False
        self.dictation_mode = None
        self.dictation_buffer = []
        self._dictation_start_time = None

        self.update_state("Listening for wake word...", "#00CED1")
        self.update_mode(None)
        self.update_flow("Listening...")
        self.timer_label.configure(text="--", text_color="#888888")
        self.log("Started listening...")
        
        # Start audio stream
        try:
            self.audio_stream = sd.InputStream(
                samplerate=16000,
                channels=1,
                dtype=np.float32,
                callback=self.audio_callback,
                device=self.app.config.get('microphone'),
                blocksize=int(16000 * 0.1)  # 100ms blocks
            )
            self.audio_stream.start()
        except Exception as e:
            self.log(f"ERROR starting audio: {e}")
            self.stop_test()
    
    def stop_test(self):
        """Stop wake word testing."""
        self.running = False
        self._dictation_start_time = None

        if self.audio_stream:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except:
                pass
            self.audio_stream = None

        self.start_btn.configure(state='normal')
        self.stop_btn.configure(state='disabled')
        self.update_state("Stopped", "#888888")
        self.update_mode(None)
        self.update_flow("Idle")
        self.level_bar.set(0)
        self.level_value.configure(text="0.000")
        self.timer_label.configure(text="--", text_color="#888888")
        self.log("Stopped listening.")
    
    def audio_callback(self, indata, frames, time_info, status):
        """Process incoming audio."""
        if not self.running:
            return
        
        audio_chunk = indata.copy().flatten()
        rms = np.sqrt(np.mean(audio_chunk**2))
        self.current_rms = rms
        
        # Update UI (schedule on main thread)
        if self.window:
            self.window.after(0, self.update_level_display, rms)
        
        speech_threshold = self.speech_thresh_var.get()
        min_speech = self.min_speech_var.get()
        
        # Get appropriate silence timeout
        if self.dictation_mode == 'long_dictate':
            silence_threshold = self.long_timeout_var.get()
        elif self.dictation_mode == 'short_dictate':
            silence_threshold = self.short_timeout_var.get()
        elif self.dictation_mode == 'dictate':
            silence_threshold = self.dictate_timeout_var.get()
        else:
            silence_threshold = self.dictate_timeout_var.get()
        
        if rms > speech_threshold:
            # Speech detected
            if not self.is_speaking:
                self.is_speaking = True
                if self.window:
                    state = f"Speaking ({self.dictation_mode or 'listening'})"
                    color = "#00FF00"
                    self.window.after(0, self.update_state, state, color)
            
            self.silence_start = None
            self.speech_buffer.append(audio_chunk)
        else:
            # Silence
            if self.is_speaking:
                self.speech_buffer.append(audio_chunk)
                
                if self.silence_start is None:
                    self.silence_start = time.time()
                elif time.time() - self.silence_start >= silence_threshold:
                    # End of speech
                    speech_duration = len(self.speech_buffer) * 0.1
                    
                    if speech_duration >= min_speech:
                        buffer_copy = self.speech_buffer.copy()
                        self.speech_buffer = []
                        self.is_speaking = False
                        self.silence_start = None
                        
                        # Process in background
                        thread = threading.Thread(
                            target=self.process_audio,
                            args=(buffer_copy,),
                            daemon=True
                        )
                        thread.start()
                    else:
                        if self.window:
                            self.window.after(0, self.log, f"Discarded: too short ({speech_duration:.1f}s)")
                        self.speech_buffer = []
                        self.is_speaking = False
                        self.silence_start = None
                        if self.window:
                            state = f"Listening ({self.dictation_mode or 'wake word'})..."
                            color = "#FFD700" if self.dictation_mode else "#00CED1"
                            self.window.after(0, self.update_state, state, color)
    
    def update_level_display(self, rms):
        """Update the level meter display."""
        if self.level_bar:
            display_value = min(1.0, rms * 10)
            self.level_bar.set(display_value)
            self.level_value.configure(text=f"{rms:.3f}")
            
            thresh = self.speech_thresh_var.get()
            if rms > thresh:
                self.level_bar.configure(progress_color="#00FF00")
            else:
                self.level_bar.configure(progress_color="#1f538d")
    
    def process_audio(self, buffer):
        """Process audio buffer - transcribe and check for wake word/commands."""
        if self.window:
            self.window.after(0, self.update_state, "Processing...", "#FF6B6B")
        
        try:
            audio = np.concatenate(buffer)
            
            segments, info = self.app.model.transcribe(
                audio,
                language=self.app.config['language'],
                beam_size=5,
                vad_filter=True,
                initial_prompt=self.app.voice_training_window.get_initial_prompt()
            )
            
            text = "".join([segment.text for segment in segments]).strip()
            text = self.app.voice_training_window.apply_corrections(text)
            
            if not text:
                if self.window:
                    self.window.after(0, self.log, "No speech detected")
                    self.window.after(0, self._reset_listening_state)
                return
            
            if self.window:
                self.window.after(0, self.update_last_heard, text)
                self.window.after(0, self.log, f"Heard: \"{text}\"")
            
            text_lower = text.lower()
            ww_config = self.app.config.get('wake_word_config', {})
            wake_phrase = ww_config.get('phrase', 'samsara').lower()
            
            # Check for cancel word if in dictation mode
            if self.dictation_mode:
                cancel_config = ww_config.get('cancel_word', {})
                if cancel_config.get('enabled', False):
                    cancel_phrase = cancel_config.get('phrase', 'cancel').lower()
                    if cancel_phrase in text_lower:
                        if self.window:
                            self.window.after(0, self.log, f"[X] CANCELLED with '{cancel_phrase}'")
                            self.window.after(0, self.update_flow, "Cancelled")
                        self.dictation_mode = None
                        self.dictation_buffer = []
                        self.wake_word_triggered = False
                        self._dictation_start_time = None
                        if self.window:
                            self.window.after(0, self.update_mode, None)
                            self.window.after(0, self._reset_listening_state)
                        return
                
                # Check for pause word
                pause_config = ww_config.get('pause_word', {})
                if pause_config.get('enabled', False):
                    pause_phrase = pause_config.get('phrase', 'pause').lower()
                    if pause_phrase in text_lower:
                        # Pause word - reset silence timer, strip pause word
                        self.silence_start = None
                        self._dictation_start_time = time.time()
                        remaining = text_lower.replace(pause_phrase, '').strip()
                        if remaining:
                            pause_idx = text_lower.find(pause_phrase)
                            cleaned = (text[:pause_idx] + text[pause_idx + len(pause_phrase):]).strip()
                            if cleaned:
                                self.dictation_buffer.append(cleaned)
                        if self.window:
                            self.window.after(0, self.log, f"|| PAUSE - timer reset ('{pause_phrase}')")
                            self.window.after(0, self._start_timer_update)
                            self.window.after(0, self._reset_listening_state)
                        return

                # Check for end word
                end_config = ww_config.get('end_word', {})
                if end_config.get('enabled', False):
                    end_phrase = end_config.get('phrase', 'over').lower()
                    if end_phrase in text_lower:
                        end_index = text_lower.rfind(end_phrase)
                        final_content = text[:end_index].strip()

                        if self.dictation_buffer:
                            final_content = ' '.join(self.dictation_buffer) + ' ' + final_content

                        if self.window:
                            self.window.after(0, self.log, f"[*] END WORD detected: '{end_phrase}'")
                            self.window.after(0, self.log, f"-> Final output: \"{final_content.strip()}\"")
                            self.window.after(0, self.update_flow,
                                             "Wake Word -> Command -> Recording -> End Word -> Done")

                        self.dictation_mode = None
                        self.dictation_buffer = []
                        self.wake_word_triggered = False
                        self._dictation_start_time = None
                        if self.window:
                            self.window.after(0, self.update_mode, None)
                            self.window.after(0, self._reset_listening_state)
                        return
                
                # Accumulate dictation content
                self.dictation_buffer.append(text)
                self._dictation_start_time = time.time()
                if self.window:
                    self.window.after(0, self.log, f"  [buffered for {self.dictation_mode}]")
                    self.window.after(0, self._start_timer_update)
                    self.window.after(0, self._reset_listening_state)
                return
            
            # Check for wake word
            if wake_phrase in text_lower:
                if self.window:
                    self.window.after(0, self.log, f"[*] WAKE WORD detected: '{wake_phrase}'")
                    self.window.after(0, self.update_flow, "Wake Word [*]")

                self.wake_word_triggered = True

                # Extract command after wake word
                wake_index = text_lower.find(wake_phrase)
                command = text[wake_index + len(wake_phrase):].strip()

                if command:
                    self._process_command(command)
                else:
                    if self.window:
                        self.window.after(0, self.log, "  Waiting for command...")
                        self.window.after(0, self.update_state, "Waiting for command...", "#FFD700")
                        self.window.after(0, self.update_flow, "Wake Word [*] -> Waiting...")
                        
            elif self.wake_word_triggered:
                # This is the command
                self._process_command(text)
            else:
                if self.window:
                    self.window.after(0, self.log, "  (no wake word)")
                    self.window.after(0, self._reset_listening_state)
        
        except Exception as e:
            if self.window:
                self.window.after(0, self.log, f"ERROR: {e}")
                self.window.after(0, self.update_state, "Error - see log", "#FF0000")
    
    def _process_command(self, command):
        """Process a command after wake word."""
        command_lower = command.lower().strip()

        # Check for dictation mode commands
        if command_lower in ['long dictate', 'long dictation']:
            self._enter_dictation_mode('long_dictate', "LONG DICTATE",
                                       "Long dictate (say end word)...")
            return

        if command_lower in ['short dictate', 'short dictation', 'quick dictate']:
            self._enter_dictation_mode('short_dictate', "SHORT DICTATE",
                                       "Short dictate...")
            return

        if command_lower in ['dictate', 'dictation']:
            self._enter_dictation_mode('dictate', "DICTATE", "Dictating...")
            return

        # Check for dictation command with content
        for cmd, mode in [('long dictate', 'long_dictate'), ('short dictate', 'short_dictate'), ('dictate', 'dictate')]:
            if command_lower.startswith(cmd + ' '):
                content = command[len(cmd):].strip()
                self._enter_dictation_mode(mode, mode.upper().replace('_', ' '),
                                           f"Dictating ({mode.replace('_', ' ')})...",
                                           initial_content=content)
                return

        # Not a dictation command - treat as immediate output
        if self.window:
            self.window.after(0, self.log, f"-> Command/text: \"{command}\"")
            self.window.after(0, self.update_flow, "Wake Word -> Command -> Done")
        self.wake_word_triggered = False
        if self.window:
            self.window.after(0, self._reset_listening_state)

    def _enter_dictation_mode(self, mode, display_name, state_text, initial_content=None):
        """Enter a dictation mode with proper state setup."""
        self.dictation_mode = mode
        self.dictation_buffer = [initial_content] if initial_content else []
        self.wake_word_triggered = False
        self._dictation_start_time = time.time()

        if self.window:
            if initial_content:
                self.window.after(0, self.log, f"-> Starting {display_name} with: \"{initial_content}\"")
            else:
                self.window.after(0, self.log, f"-> Starting {display_name} mode")
            self.window.after(0, self.update_mode, mode)
            self.window.after(0, self.update_state, state_text, "#FFD700")
            self.window.after(0, self.update_flow,
                             f"Wake Word -> {display_name} -> [Recording...]")
            self.window.after(0, self._start_timer_update)
    
    def _reset_listening_state(self):
        """Reset to listening state."""
        if self.running:
            if self.dictation_mode:
                state = f"Dictating ({self.dictation_mode.replace('_', ' ')})..."
                color = "#FFD700"
            elif self.wake_word_triggered:
                state = "Waiting for command..."
                color = "#FFD700"
            else:
                state = "Listening for wake word..."
                color = "#00CED1"
                if self.flow_label:
                    self.update_flow("Listening...")
                if self.timer_label:
                    self.timer_label.configure(text="--", text_color="#888888")
            self.update_state(state, color)
    
    def _force_enter_mode(self):
        """Force enter a dictation mode for testing."""
        mode = self.test_mode_var.get()
        if mode == "(auto)":
            self.log("Select a mode to force (not 'auto')")
            return
        if not self.running:
            self.log("Start the test first before forcing a mode")
            return

        self.dictation_mode = mode
        self.dictation_buffer = []
        self.wake_word_triggered = False
        self._dictation_start_time = time.time()
        self.update_mode(mode)
        self.log(f"Forced into {mode} mode - speak now")
        self.update_state(f"Dictating ({mode.replace('_', ' ')})...", "#FFD700")
        self._start_timer_update()

    def _reset_test_mode(self):
        """Reset to normal wake word listening."""
        self.dictation_mode = None
        self.dictation_buffer = []
        self.wake_word_triggered = False
        self._dictation_start_time = None
        self.update_mode(None)
        self.update_flow("Idle")
        self.timer_label.configure(text="--", text_color="#888888")
        if self.running:
            self.update_state("Listening for wake word...", "#00CED1")
            self.log("Reset to wake word listening")

    def _simulate_word(self, word_type):
        """Simulate an end/cancel/pause word for testing."""
        if not self.dictation_mode:
            self.log(f"Not in dictation mode - enter a mode first")
            return

        ww_config = self.app.config.get('wake_word_config', {})

        if word_type == 'end':
            end_config = ww_config.get('end_word', {})
            if not end_config.get('enabled', False):
                self.log("End word is disabled in config")
                return
            phrase = end_config.get('phrase', 'over')
            final_text = ' '.join(self.dictation_buffer) if self.dictation_buffer else "(empty)"
            self.log(f"[SIM] End word '{phrase}' - output: \"{final_text}\"")
            self._reset_test_mode()

        elif word_type == 'cancel':
            cancel_config = ww_config.get('cancel_word', {})
            if not cancel_config.get('enabled', False):
                self.log("Cancel word is disabled in config")
                return
            phrase = cancel_config.get('phrase', 'cancel')
            self.log(f"[SIM] Cancel word '{phrase}' - dictation aborted")
            self._reset_test_mode()

        elif word_type == 'pause':
            pause_config = ww_config.get('pause_word', {})
            if not pause_config.get('enabled', False):
                self.log("Pause word is disabled in config")
                return
            phrase = pause_config.get('phrase', 'pause')
            self.silence_start = None
            self._dictation_start_time = time.time()
            self.log(f"[SIM] Pause word '{phrase}' - timer reset")
            self._start_timer_update()

    def _start_timer_update(self):
        """Start updating the timer display."""
        if not self.window or not self.running:
            return
        self._update_timer()

    def _update_timer(self):
        """Update the countdown timer display."""
        if not self.window or not self.running or not self.dictation_mode:
            self.timer_label.configure(text="--", text_color="#888888")
            return

        if not hasattr(self, '_dictation_start_time') or not self._dictation_start_time:
            self.timer_label.configure(text="--", text_color="#888888")
            return

        # Get timeout for current mode
        if self.dictation_mode == 'long_dictate':
            timeout = self.long_timeout_var.get()
        elif self.dictation_mode == 'short_dictate':
            timeout = self.short_timeout_var.get()
        else:
            timeout = self.dictate_timeout_var.get()

        elapsed = time.time() - self._dictation_start_time
        remaining = max(0, timeout - elapsed)

        if remaining > 0:
            color = "#00FF00" if remaining > timeout * 0.3 else "#FF6B6B"
            self.timer_label.configure(text=f"{remaining:.1f}s", text_color=color)
            self.window.after(100, self._update_timer)
        else:
            self.timer_label.configure(text="0.0s", text_color="#FF0000")
            if not self._get_require_end():
                self.log(f"Timer expired - would output: \"{' '.join(self.dictation_buffer)}\"")
                self._reset_test_mode()

    def _get_require_end(self):
        """Check if current mode requires end word."""
        ww_config = self.app.config.get('wake_word_config', {})
        modes = ww_config.get('modes', {})
        if self.dictation_mode:
            return modes.get(self.dictation_mode, {}).get('require_end_word', False)
        return False

    def update_flow(self, step):
        """Update the flow indicator."""
        if self.flow_label:
            self.flow_label.configure(text=step)

    def on_close(self):
        """Handle window close."""
        self.stop_test()
        if self.window:
            self.window.destroy()
            self.window = None

    def close(self):
        """Close the window."""
        self.on_close()
