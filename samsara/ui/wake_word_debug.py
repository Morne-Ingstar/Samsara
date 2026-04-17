"""
Wake Word Debug/Test Window for Samsara
Real-time debugging, observability, and tuning interface for wake word functionality.
"""

import tkinter as tk
import customtkinter as ctk
import numpy as np
import threading
import time
from datetime import datetime
from pathlib import Path
import sounddevice as sd

from samsara.wake_word_matcher import match_wake_phrase
from samsara.wake_corrections import apply_corrections as apply_wake_corrections, was_corrected

def _resample_audio(audio, orig_sr, target_sr=16000):
    """Lightweight linear-interpolation resample for speech audio."""
    if orig_sr == target_sr:
        return audio
    duration = len(audio) / orig_sr
    new_length = int(duration * target_sr)
    old_indices = np.linspace(0, len(audio) - 1, num=len(audio))
    new_indices = np.linspace(0, len(audio) - 1, num=new_length)
    return np.interp(new_indices, old_indices, audio).astype(np.float32)


class WakeWordDebugWindow:
    """Debug window for testing and tuning wake word detection."""

    _MAX_TIMELINE_UTTERANCES = 50

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

        # Performance: cached widget values to skip redundant reconfigs
        self._prev_level_value = None
        self._prev_level_color = None
        self._prev_state_text = None
        self._prev_state_color = None
        self._prev_timer_text = None
        self._prev_timer_color = None

        # Performance: unified UI poll loop instead of per-callback after(0)
        self._pending_rms = None
        self._ui_poll_id = None
        self._log_buffer = []
        self._log_flush_id = None

        # Structured trace buffer (list of utterance blocks)
        self.trace_buffer = []
        self._current_trace = []

        # Widget refs -- null-initialized so safety guards work before stages complete
        self.state_label = None
        self.wake_word_label = None
        self.end_word_label = None
        self.mode_label = None
        self.timer_label = None
        self.flow_label = None
        self.last_heard_label = None
        self._eval_raw_label = None
        self._eval_norm_label = None
        self._eval_corrected_label = None
        self._eval_phrase_label = None
        self._eval_match_label = None
        self._eval_method_label = None
        self._eval_index_label = None
        self.level_bar = None
        self.level_value = None
        self.thresh_indicator = None
        self.speech_thresh_var = None
        self.speech_thresh_label = None
        self.min_speech_var = None
        self.min_speech_label = None
        self.dictate_timeout_var = None
        self.dictate_timeout_label = None
        self.short_timeout_var = None
        self.short_timeout_label = None
        self.long_timeout_var = None
        self.long_timeout_label = None
        self.test_mode_var = None
        self.start_btn = None
        self.stop_btn = None
        self.timeline_text = None
        self.log_text = None
        self._main_frame = None

        # Cached fonts (created once per show(), shared across all stages)
        self._font_header = None
        self._font_bold = None
        self._font_mono = None

    # ==================================================================
    # Structured trace pipeline
    # ==================================================================

    def trace(self, event):
        """Record a structured debug event.

        Called from background threads -- schedules UI updates via after(0).
        """
        event.setdefault('_time', datetime.now().strftime("%H:%M:%S"))
        stage = event.get('stage', '')

        if stage == 'utterance_start':
            self._current_trace = [event]
        elif stage == 'utterance_end':
            self._current_trace.append(event)
            self.trace_buffer.append(list(self._current_trace))
            if len(self.trace_buffer) > self._MAX_TIMELINE_UTTERANCES:
                self.trace_buffer.pop(0)
            block = self._current_trace
            self._current_trace = []
            if self.window:
                self.window.after(0, self._render_timeline_block, block)
        else:
            self._current_trace.append(event)

        if stage == 'wake_word_check' and self.window:
            self.window.after(0, self._update_eval_panel, event)

        line = self._format_trace_line(event)
        if line:
            self.log(line)

    def _format_trace_line(self, event):
        stage = event.get('stage', '')
        if stage == 'utterance_start':
            return f"--- Heard: \"{event.get('raw', '')}\""
        if stage == 'wake_word_check':
            m = event.get('matched', False)
            t = event.get('match_type', 'none')
            i = event.get('match_index', -1)
            return f"[WAKE_MATCH] {'YES' if m else 'NO'} ({t} @ idx {i})"
        if stage == 'command_extract':
            cmd = event.get('command', '')
            return f"[CMD_EXTRACT] \"{cmd}\"" if cmd else None
        if stage == 'command_classify':
            cls = event.get('classification', '')
            kw = event.get('matched_keyword', '')
            extra = f" (keyword: \"{kw}\")" if kw else ""
            return f"[CLASSIFY] {cls}{extra}"
        if stage == 'mode_switch':
            return f"[MODE] -> {event.get('to_mode', '?')}"
        if stage == 'dictation_buffered':
            return f"[BUFFERED] \"{event.get('text', '')}\" (buf={event.get('buffer_size', 0)})"
        if stage == 'end_word_detected':
            return f"[END_WORD] \"{event.get('phrase', '')}\" -> output: \"{event.get('final_output', '')}\""
        if stage in ('cancel_word_detected', 'pause_word_detected'):
            return f"[{stage.upper().split('_')[0]}] \"{event.get('phrase', '')}\""
        if stage == 'utterance_end':
            return f"--- Result: {event.get('result', '?')}"
        return None

    def _render_timeline_block(self, block):
        if not hasattr(self, 'timeline_text') or self.timeline_text is None:
            return
        ts = block[0].get('_time', '') if block else ''
        raw = block[0].get('raw', '') if block else ''
        norm = block[0].get('normalized', '') if block else ''
        corrected = block[0].get('corrected', '') if block else ''
        correction_applied = block[0].get('correction_applied', False) if block else False
        lines = [f"{'=' * 44} UTTERANCE [{ts}] {'=' * 3}"]
        lines.append(f"RAW:  \"{raw}\"")
        lines.append(f"NORM: \"{norm}\"")
        if correction_applied:
            lines.append(f"CORR: \"{corrected}\"   [correction applied]")
        lines.append("")
        step = 1
        for ev in block[1:]:
            stage = ev.get('stage', '')
            if stage == 'utterance_end':
                lines.append(f"  -> RESULT: {ev.get('result', '?')}")
                continue
            detail = self._timeline_detail(ev)
            lines.append(f"[{step}] {stage:22s} {detail}")
            step += 1
        lines.append("=" * 55)
        lines.append("")
        text = "\n".join(lines)
        self.timeline_text.configure(state='normal')
        self.timeline_text.insert('end', text)
        self.timeline_text.see('end')
        self.timeline_text.configure(state='disabled')

    @staticmethod
    def _timeline_detail(ev):
        stage = ev.get('stage', '')
        if stage == 'wake_word_check':
            m = ev.get('matched', False)
            t = ev.get('match_type', 'none')
            i = ev.get('match_index', -1)
            return f"-> {'TRUE' if m else 'FALSE'} ({t} @ idx {i})"
        if stage == 'command_extract':
            return f"-> \"{ev.get('command', '')}\""
        if stage == 'command_classify':
            cls = ev.get('classification', '')
            kw = ev.get('matched_keyword', '')
            extra = f" (keyword: \"{kw}\")" if kw else ""
            return f"-> {cls}{extra}"
        if stage == 'mode_switch':
            return f"-> {ev.get('to_mode', '?')}"
        if stage == 'dictation_buffered':
            return f"-> \"{ev.get('text', '')}\" (buf_size={ev.get('buffer_size', 0)})"
        if stage == 'end_word_detected':
            return f"-> output: \"{ev.get('final_output', '')}\""
        if stage in ('cancel_word_detected', 'pause_word_detected'):
            return f"-> \"{ev.get('phrase', '')}\""
        return ""

    def _update_eval_panel(self, event):
        if not hasattr(self, '_eval_raw_label'):
            return
        raw = event.get('input', '')
        norm = event.get('normalized', '')
        corrected = event.get('corrected', '')
        correction_applied = event.get('correction_applied', False)
        phrase = event.get('wake_phrase', '')
        matched = event.get('matched', False)
        mtype = event.get('match_type', 'none')
        idx = event.get('match_index', -1)
        self._eval_raw_label.configure(text=raw if len(raw) < 70 else raw[:67] + "...")
        self._eval_norm_label.configure(text=norm if len(norm) < 70 else norm[:67] + "...")
        if self._eval_corrected_label is not None:
            if correction_applied:
                shown = corrected if len(corrected) < 70 else corrected[:67] + "..."
                self._eval_corrected_label.configure(text=shown, text_color="#FFD700")
            else:
                self._eval_corrected_label.configure(text="(no correction applied)",
                                                     text_color="#666666")
        self._eval_phrase_label.configure(text=f'"{phrase}"')
        self._eval_match_label.configure(text="YES" if matched else "NO",
                                         text_color="#00FF00" if matched else "#888888")
        self._eval_method_label.configure(text=mtype)
        self._eval_index_label.configure(text=str(idx) if idx >= 0 else "--")

    def on_app_trace(self, event):
        """Called from DictationApp's wake word pipeline to feed events here."""
        self.trace(event)

    # ==================================================================
    # Window UI
    # ==================================================================

    # ==================================================================
    # Staged UI construction
    # ==================================================================

    def show(self):
        """Show the debug window. UI builds progressively in 4 stages."""
        if self.window is not None:
            try:
                self.window.lift()
                self.window.focus_force()
                return
            except:
                self.window = None

        # Cache fonts once (avoids re-creating CTkFont objects per widget)
        self._font_header = ctk.CTkFont(size=16, weight="bold")
        self._font_bold = ctk.CTkFont(weight="bold")
        self._font_mono = ctk.CTkFont(family="Consolas", size=11)

        # Snapshot config so stages don't re-read
        self._ww_config = self.app.config.get('wake_word_config', {})

        # Create window shell -- visible immediately
        self.window = ctk.CTkToplevel()
        self.window.title("Wake Word Debug")
        self.window.geometry("750x1100")
        self.window.resizable(True, True)
        self.window.minsize(700, 900)
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)

        # Register as trace callback on main app
        if hasattr(self.app, '_wake_trace_callback'):
            self.app._wake_trace_callback = self.on_app_trace

        # Kick off staged build
        self.window.after(0, self._build_ui_stage_1)

    def _build_ui_stage_1(self):
        """Stage 1: scrollable container + Status section + Eval panel (lightweight labels)."""
        if not self.window:
            return

        self._main_frame = ctk.CTkScrollableFrame(self.window, fg_color="transparent")
        self._main_frame.pack(fill='both', expand=True, padx=15, pady=15)
        mf = self._main_frame

        # --- Status Section ---
        ctk.CTkLabel(mf, text="Status", font=self._font_header).pack(anchor='w', pady=(0, 10))
        status_frame = ctk.CTkFrame(mf, corner_radius=10)
        status_frame.pack(fill='x', pady=(0, 15))

        state_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        state_row.pack(fill='x', padx=15, pady=(15, 8))
        ctk.CTkLabel(state_row, text="State:", width=120, anchor='w').pack(side='left')
        self.state_label = ctk.CTkLabel(state_row, text="Idle", text_color="#888888",
                                        font=self._font_bold)
        self.state_label.pack(side='left')

        ww_config = self._ww_config
        wake_phrase = ww_config.get('phrase', 'samsara')
        self.wake_word_label = self._make_status_row(status_frame, "Wake phrase:",
                                                     f'"{wake_phrase}"', "#00CED1")
        end_config = ww_config.get('end_word', {})
        end_txt = f'"{end_config.get("phrase", "over")}"' if end_config.get('enabled') else "(disabled)"
        end_clr = "#00CED1" if end_config.get('enabled') else "#888888"
        self.end_word_label = self._make_status_row(status_frame, "End word:", end_txt, end_clr)
        self.mode_label = self._make_status_row(status_frame, "Dictation mode:", "None")
        self.timer_label = self._make_status_row(status_frame, "Timer:", "--")

        flow_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        flow_row.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(flow_row, text="Flow:", width=120, anchor='w').pack(side='left')
        self.flow_label = ctk.CTkLabel(flow_row, text="Idle", text_color="#888888",
                                       wraplength=400, justify='left')
        self.flow_label.pack(side='left', fill='x', expand=True)

        heard_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        heard_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(heard_row, text="Last heard:", width=120, anchor='w').pack(side='left')
        self.last_heard_label = ctk.CTkLabel(heard_row, text="(nothing yet)",
                                             text_color="#888888", wraplength=400, justify='left')
        self.last_heard_label.pack(side='left', fill='x', expand=True)

        # --- Wake Word Evaluation Panel ---
        ctk.CTkLabel(mf, text="Wake Word Evaluation",
                     font=self._font_header).pack(anchor='w', pady=(0, 10))
        eval_frame = ctk.CTkFrame(mf, corner_radius=10)
        eval_frame.pack(fill='x', pady=(0, 15))

        self._eval_raw_label = self._make_eval_row(eval_frame, "Raw text:", pad=(12, 6))
        self._eval_norm_label = self._make_eval_row(eval_frame, "Normalized:")
        self._eval_corrected_label = self._make_eval_row(eval_frame, "Corrected:")
        self._eval_phrase_label = self._make_eval_row(eval_frame, "Wake phrase:")
        self._eval_match_label = self._make_eval_row(eval_frame, "Match:")
        self._eval_method_label = self._make_eval_row(eval_frame, "Match method:")
        self._eval_index_label = self._make_eval_row(eval_frame, "Match index:", pad=(0, 12))

        self.window.after(10, self._build_ui_stage_2)

    def _build_ui_stage_2(self):
        """Stage 2: Audio level + Start/Stop buttons + Parameters sliders."""
        if not self.window:
            return
        mf = self._main_frame
        ww_config = self._ww_config
        audio_config = ww_config.get('audio', {})

        # --- Audio Level ---
        ctk.CTkLabel(mf, text="Audio Level", font=self._font_header).pack(anchor='w', pady=(0, 10))
        level_frame = ctk.CTkFrame(mf, corner_radius=10)
        level_frame.pack(fill='x', pady=(0, 15))
        bar_frame = ctk.CTkFrame(level_frame, fg_color="transparent")
        bar_frame.pack(fill='x', padx=15, pady=(15, 5))
        self.level_bar = ctk.CTkProgressBar(bar_frame, width=400, height=25)
        self.level_bar.pack(side='left', fill='x', expand=True)
        self.level_bar.set(0)
        self.level_value = ctk.CTkLabel(bar_frame, text="0.000", width=60)
        self.level_value.pack(side='left', padx=(10, 0))
        thresh_row = ctk.CTkFrame(level_frame, fg_color="transparent")
        thresh_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(thresh_row, text="Speech threshold:", width=120, anchor='w').pack(side='left')
        self.thresh_indicator = ctk.CTkLabel(thresh_row,
                                             text=f"{audio_config.get('speech_threshold', 0.03):.3f}",
                                             text_color="#888888")
        self.thresh_indicator.pack(side='left')

        # --- Parameters ---
        ctk.CTkLabel(mf, text="Tunable Parameters", font=self._font_header).pack(anchor='w', pady=(0, 10))
        params_frame = ctk.CTkFrame(mf, corner_radius=10)
        params_frame.pack(fill='x', pady=(0, 15))

        speech_row = ctk.CTkFrame(params_frame, fg_color="transparent")
        speech_row.pack(fill='x', padx=15, pady=(15, 10))
        ctk.CTkLabel(speech_row, text="Speech threshold:", width=150, anchor='w').pack(side='left')
        self.speech_thresh_var = ctk.DoubleVar(value=audio_config.get('speech_threshold', 0.03))
        ctk.CTkSlider(speech_row, from_=0.005, to=0.15, variable=self.speech_thresh_var,
                      width=200, command=self.on_speech_thresh_change).pack(side='left', padx=(0, 10))
        self.speech_thresh_label = ctk.CTkLabel(speech_row,
                                                text=f"{self.speech_thresh_var.get():.3f}", width=50)
        self.speech_thresh_label.pack(side='left')

        min_row = ctk.CTkFrame(params_frame, fg_color="transparent")
        min_row.pack(fill='x', padx=15, pady=(0, 10))
        ctk.CTkLabel(min_row, text="Min speech (s):", width=150, anchor='w').pack(side='left')
        self.min_speech_var = ctk.DoubleVar(value=audio_config.get('min_speech_duration', 0.3))
        ctk.CTkSlider(min_row, from_=0.1, to=2.0, variable=self.min_speech_var,
                      width=200, command=self.on_min_speech_change).pack(side='left', padx=(0, 10))
        self.min_speech_label = ctk.CTkLabel(min_row, text=f"{self.min_speech_var.get():.1f}", width=50)
        self.min_speech_label.pack(side='left')

        ctk.CTkLabel(params_frame, text="Mode Timeouts:",
                     font=self._font_bold).pack(anchor='w', padx=15, pady=(10, 5))
        modes_config = ww_config.get('modes', {})

        self.dictate_timeout_var, self.dictate_timeout_label = self._make_timeout_row(
            params_frame, "dictate:",
            modes_config.get('dictate', {}).get('silence_timeout', 2.0), 0.5, 10.0, "{:.1f}s")
        self.short_timeout_var, self.short_timeout_label = self._make_timeout_row(
            params_frame, "short dictate:",
            modes_config.get('short_dictate', {}).get('silence_timeout', 1.0), 0.3, 5.0, "{:.1f}s")
        self.long_timeout_var, self.long_timeout_label = self._make_timeout_row(
            params_frame, "long dictate:",
            modes_config.get('long_dictate', {}).get('silence_timeout', 60.0),
            10.0, 120.0, "{:.0f}s", pady=(0, 15))

        # --- Control Buttons (early so user can interact) ---
        btn_frame = ctk.CTkFrame(mf, fg_color="transparent")
        btn_frame.pack(fill='x', pady=(0, 15))
        self.start_btn = ctk.CTkButton(btn_frame, text="Start Test", width=120,
                                       fg_color="#228B22", hover_color="#2E8B2E",
                                       command=self.start_test)
        self.start_btn.pack(side='left', padx=(0, 10))
        self.stop_btn = ctk.CTkButton(btn_frame, text="Stop", width=120,
                                      fg_color="#8B0000", hover_color="#A52A2A",
                                      command=self.stop_test, state='disabled')
        self.stop_btn.pack(side='left', padx=(0, 10))
        ctk.CTkButton(btn_frame, text="Clear Log", width=100, fg_color="gray40",
                      hover_color="gray30", command=self.clear_log).pack(side='left', padx=(0, 10))
        ctk.CTkButton(btn_frame, text="Apply to Config", width=120,
                      command=self.apply_to_config).pack(side='right')

        self.window.after(10, self._build_ui_stage_3)

    def _build_ui_stage_3(self):
        """Stage 3: Test controls + Decision timeline (heavy textbox)."""
        if not self.window:
            return
        mf = self._main_frame

        # --- Test Controls ---
        ctk.CTkLabel(mf, text="Test Controls", font=self._font_header).pack(anchor='w', pady=(0, 10))
        test_frame = ctk.CTkFrame(mf, corner_radius=10)
        test_frame.pack(fill='x', pady=(0, 15))
        mode_row = ctk.CTkFrame(test_frame, fg_color="transparent")
        mode_row.pack(fill='x', padx=15, pady=(15, 8))
        ctk.CTkLabel(mode_row, text="Force mode:", width=100, anchor='w').pack(side='left')
        self.test_mode_var = tk.StringVar(value="(auto)")
        ctk.CTkComboBox(mode_row, variable=self.test_mode_var,
                        values=["(auto)", "dictate", "short_dictate", "long_dictate"],
                        width=150).pack(side='left', padx=(0, 10))
        ctk.CTkButton(mode_row, text="Enter Mode", width=90,
                      command=self._force_enter_mode).pack(side='left', padx=(0, 10))
        ctk.CTkButton(mode_row, text="Reset", width=70, fg_color="gray40",
                      hover_color="gray30", command=self._reset_test_mode).pack(side='left')
        word_row = ctk.CTkFrame(test_frame, fg_color="transparent")
        word_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(word_row, text="Simulate:", width=100, anchor='w').pack(side='left')
        for txt, wt in [("End Word", 'end'), ("Cancel Word", 'cancel'), ("Pause Word", 'pause')]:
            ctk.CTkButton(word_row, text=txt, width=80 + (10 if 'Cancel' in txt else 0),
                          command=lambda w=wt: self._simulate_word(w)).pack(side='left', padx=(0, 5))

        # --- Decision Timeline (heavy: CTkTextbox) ---
        ctk.CTkLabel(mf, text="Decision Timeline", font=self._font_header).pack(anchor='w', pady=(0, 10))
        tl_frame = ctk.CTkFrame(mf, corner_radius=10)
        tl_frame.pack(fill='both', expand=True, pady=(0, 15))
        self.timeline_text = ctk.CTkTextbox(tl_frame, height=200, state='disabled',
                                            font=self._font_mono)
        self.timeline_text.pack(fill='both', expand=True, padx=10, pady=10)
        tl_btn = ctk.CTkFrame(mf, fg_color="transparent")
        tl_btn.pack(fill='x', pady=(0, 15))
        ctk.CTkButton(tl_btn, text="Export Timeline", width=130,
                      command=self._export_timeline).pack(side='left', padx=(0, 10))
        ctk.CTkButton(tl_btn, text="Clear Timeline", width=120, fg_color="gray40",
                      hover_color="gray30", command=self._clear_timeline).pack(side='left')

        self.window.after(10, self._build_ui_stage_4)

    def _build_ui_stage_4(self):
        """Stage 4: Event log (heavy textbox) + initial log messages."""
        if not self.window:
            return
        mf = self._main_frame

        # --- Event Log (heavy: CTkTextbox) ---
        ctk.CTkLabel(mf, text="Event Log", font=self._font_header).pack(anchor='w', pady=(0, 10))
        log_frame = ctk.CTkFrame(mf, corner_radius=10)
        log_frame.pack(fill='both', expand=True, pady=(0, 15))
        self.log_text = ctk.CTkTextbox(log_frame, height=150, state='disabled')
        self.log_text.pack(fill='both', expand=True, padx=10, pady=10)

        # Write initial log (now that log_text exists, any buffered messages flush too)
        ww_config = self._ww_config
        wake_phrase = ww_config.get('phrase', 'samsara')
        end_config = ww_config.get('end_word', {})
        self.log(f"Wake phrase: \"{wake_phrase}\"")
        if end_config.get('enabled', False):
            self.log(f"End word: \"{end_config.get('phrase', 'over')}\"")
        self.log(f"Microphone: {self.app.config.get('microphone', 'default')}")
        self.log("Ready to test. Click 'Start Test' to begin.")

        # Clean up config snapshot
        self._ww_config = None

    # --- UI builder helpers (shared by stages) ---

    @staticmethod
    def _make_status_row(parent, label_text, text="", color="#888888", pad=(0, 8)):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill='x', padx=15, pady=pad)
        ctk.CTkLabel(row, text=label_text, width=120, anchor='w').pack(side='left')
        lbl = ctk.CTkLabel(row, text=text, text_color=color)
        lbl.pack(side='left')
        return lbl

    @staticmethod
    def _make_eval_row(parent, label_text, pad=(0, 6)):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill='x', padx=15, pady=pad)
        ctk.CTkLabel(row, text=label_text, width=120, anchor='w', text_color="#888888").pack(side='left')
        lbl = ctk.CTkLabel(row, text="--", text_color="#AAAAAA")
        lbl.pack(side='left', fill='x', expand=True)
        return lbl

    @staticmethod
    def _make_timeout_row(parent, label, init, lo, hi, fmt, pady=(0, 5)):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill='x', padx=15, pady=pady)
        ctk.CTkLabel(row, text=label, width=100, anchor='w').pack(side='left')
        var = ctk.DoubleVar(value=init)
        lbl = ctk.CTkLabel(row, text=fmt.format(init), width=40)
        ctk.CTkSlider(row, from_=lo, to=hi, variable=var, width=180,
                      command=lambda v, l=lbl, f=fmt: l.configure(text=f.format(v))
                      ).pack(side='left', padx=(0, 10))
        lbl.pack(side='left')
        return var, lbl

    # ==================================================================
    # Export / clear timeline
    # ==================================================================

    def _export_timeline(self):
        if not self.trace_buffer:
            self.log("Nothing to export.")
            return
        docs_dir = Path(__file__).parent.parent.parent / "docs"
        docs_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = docs_dir / f"wake_word_trace_{ts}.txt"
        lines = []
        for block in self.trace_buffer:
            if not block:
                continue
            bts = block[0].get('_time', '')
            raw = block[0].get('raw', '')
            norm = block[0].get('normalized', '')
            lines.append(f"{'=' * 44} UTTERANCE [{bts}] {'=' * 3}")
            lines.append(f"RAW:  \"{raw}\"")
            lines.append(f"NORM: \"{norm}\"")
            lines.append("")
            step = 1
            for ev in block[1:]:
                stage = ev.get('stage', '')
                if stage == 'utterance_end':
                    lines.append(f"  -> RESULT: {ev.get('result', '?')}")
                    continue
                lines.append(f"[{step}] {stage:22s} {self._timeline_detail(ev)}")
                step += 1
            lines.append("=" * 55)
            lines.append("")
        path.write_text("\n".join(lines), encoding='utf-8')
        self.log(f"Timeline exported to {path.name}")

    def _clear_timeline(self):
        self.trace_buffer.clear()
        if self.timeline_text:
            self.timeline_text.configure(state='normal')
            self.timeline_text.delete('1.0', 'end')
            self.timeline_text.configure(state='disabled')

    # ==================================================================
    # Logging (batched)
    # ==================================================================

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log_buffer.append(f"[{timestamp}] {message}\n")
        # Only schedule flush if the textbox exists and no flush is pending
        if self.log_text is not None and self._log_flush_id is None and self.window:
            self._log_flush_id = self.window.after(200, self._flush_log)

    def _flush_log(self):
        self._log_flush_id = None
        if not self._log_buffer or self.log_text is None:
            return
        text = "".join(self._log_buffer)
        self._log_buffer.clear()
        self.log_text.configure(state='normal')
        self.log_text.insert('end', text)
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def clear_log(self):
        if self.log_text is None:
            return
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    # ==================================================================
    # UI helpers
    # ==================================================================

    def update_state(self, state, color="#888888"):
        self.state = state
        if self.state_label and (state != self._prev_state_text or color != self._prev_state_color):
            self._prev_state_text = state
            self._prev_state_color = color
            self.state_label.configure(text=state, text_color=color)

    def update_mode(self, mode):
        self.dictation_mode = mode
        if self.mode_label:
            if mode:
                self.mode_label.configure(text=mode.replace('_', ' ').title(), text_color="#FFD700")
            else:
                self.mode_label.configure(text="None", text_color="#888888")

    def update_last_heard(self, text):
        if self.last_heard_label:
            display = text if len(text) < 60 else text[:57] + "..."
            self.last_heard_label.configure(text=f'"{display}"', text_color="white")

    def on_speech_thresh_change(self, value):
        if self.speech_thresh_label:
            self.speech_thresh_label.configure(text=f"{value:.3f}")
        if self.thresh_indicator:
            self.thresh_indicator.configure(text=f"{value:.3f}")

    def on_min_speech_change(self, value):
        if self.min_speech_label:
            self.min_speech_label.configure(text=f"{value:.1f}")

    def apply_to_config(self):
        ww_config = self.app.config.get('wake_word_config', {})
        if 'audio' not in ww_config:
            ww_config['audio'] = {}
        ww_config['audio']['speech_threshold'] = self.speech_thresh_var.get()
        ww_config['audio']['min_speech_duration'] = self.min_speech_var.get()
        if 'modes' not in ww_config:
            ww_config['modes'] = {}
        ww_config['modes']['dictate'] = {'silence_timeout': self.dictate_timeout_var.get(), 'require_end_word': False}
        ww_config['modes']['short_dictate'] = {'silence_timeout': self.short_timeout_var.get(), 'require_end_word': False}
        ww_config['modes']['long_dictate'] = {'silence_timeout': self.long_timeout_var.get(), 'require_end_word': True}
        self.app.config['wake_word_config'] = ww_config
        self.app.save_config()
        self.log("Settings applied and saved to config!")

    def update_flow(self, step):
        if self.flow_label:
            self.flow_label.configure(text=step)

    # ==================================================================
    # Audio polling / level meter
    # ==================================================================

    def _start_ui_poll(self):
        if self._ui_poll_id is not None:
            return
        self._ui_poll_tick()

    def _stop_ui_poll(self):
        if self._ui_poll_id is not None:
            try:
                self.window.after_cancel(self._ui_poll_id)
            except Exception:
                pass
            self._ui_poll_id = None

    def _ui_poll_tick(self):
        if not self.running or not self.window:
            self._ui_poll_id = None
            return
        rms = self._pending_rms
        if rms is not None:
            self._pending_rms = None
            self._update_level_display(rms)
        if self.dictation_mode and self._dictation_start_time:
            self._update_timer_display()
        self._ui_poll_id = self.window.after(250, self._ui_poll_tick)

    def _update_level_display(self, rms):
        if not self.level_bar:
            return
        self.level_bar.set(min(1.0, rms * 10))
        text = f"{rms:.3f}"
        if text != self._prev_level_value:
            self._prev_level_value = text
            self.level_value.configure(text=text)
        thresh = self.speech_thresh_var.get()
        new_color = "#00FF00" if rms > thresh else "#1f538d"
        if new_color != self._prev_level_color:
            self._prev_level_color = new_color
            self.level_bar.configure(progress_color=new_color)

    # ==================================================================
    # Test control
    # ==================================================================

    def start_test(self):
        if not self.app.model_loaded:
            self.log("ERROR: Model not loaded yet. Please wait...")
            return
        if self.start_btn is None:
            self.log("ERROR: UI still loading. Please wait...")
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
        if self.timer_label:
            self.timer_label.configure(text="--", text_color="#888888")
        self.log("Started listening...")
        try:
            capture_rate = getattr(self.app, 'capture_rate', 48000)
            self.audio_stream = sd.InputStream(
                samplerate=capture_rate, channels=1, dtype=np.float32,
                callback=self.audio_callback,
                device=self.app.config.get('microphone'),
                blocksize=int(capture_rate * 0.1))
            self.audio_stream.start()
            self._start_ui_poll()
        except Exception as e:
            self.log(f"ERROR starting audio: {e}")
            self.stop_test()

    def stop_test(self):
        self.running = False
        self._dictation_start_time = None
        self._stop_ui_poll()
        if self.audio_stream:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except:
                pass
            self.audio_stream = None
        self._prev_level_value = self._prev_level_color = None
        self._prev_timer_text = self._prev_timer_color = None
        self._pending_rms = None
        if self.start_btn:
            self.start_btn.configure(state='normal')
        if self.stop_btn:
            self.stop_btn.configure(state='disabled')
        self.update_state("Stopped", "#888888")
        self.update_mode(None)
        self.update_flow("Idle")
        if self.level_bar:
            self.level_bar.set(0)
        if self.level_value:
            self.level_value.configure(text="0.000")
        if self.timer_label:
            self.timer_label.configure(text="--", text_color="#888888")
        self.log("Stopped listening.")

    def audio_callback(self, indata, frames, time_info, status):
        if not self.running:
            return
        audio_chunk = indata.copy().flatten()
        rms = np.sqrt(np.mean(audio_chunk**2))
        self.current_rms = rms
        self._pending_rms = rms
        speech_threshold = self.speech_thresh_var.get()
        min_speech = self.min_speech_var.get()
        if self.dictation_mode == 'long_dictate':
            sil = self.long_timeout_var.get()
        elif self.dictation_mode == 'short_dictate':
            sil = self.short_timeout_var.get()
        else:
            sil = self.dictate_timeout_var.get()
        if rms > speech_threshold:
            if not self.is_speaking:
                self.is_speaking = True
                if self.window:
                    self.window.after(0, self.update_state,
                                     f"Speaking ({self.dictation_mode or 'listening'})", "#00FF00")
            self.silence_start = None
            self.speech_buffer.append(audio_chunk)
        elif self.is_speaking:
            self.speech_buffer.append(audio_chunk)
            if self.silence_start is None:
                self.silence_start = time.time()
            elif time.time() - self.silence_start >= sil:
                dur = len(self.speech_buffer) * 0.1
                if dur >= min_speech:
                    buf = self.speech_buffer.copy()
                    self.speech_buffer = []
                    self.is_speaking = False
                    self.silence_start = None
                    threading.Thread(target=self.process_audio, args=(buf,), daemon=True).start()
                else:
                    if self.window:
                        self.window.after(0, self.log, f"Discarded: too short ({dur:.1f}s)")
                    self.speech_buffer = []
                    self.is_speaking = False
                    self.silence_start = None
                    if self.window:
                        st = f"Listening ({self.dictation_mode or 'wake word'})..."
                        cl = "#FFD700" if self.dictation_mode else "#00CED1"
                        self.window.after(0, self.update_state, st, cl)

    # ==================================================================
    # Audio processing (shared matcher + trace events)
    # ==================================================================

    def process_audio(self, buffer):
        if self.window:
            self.window.after(0, self.update_state, "Processing...", "#FF6B6B")
        try:
            audio = np.concatenate(buffer)
            capture_rate = getattr(self.app, 'capture_rate', 48000)
            model_rate = getattr(self.app, 'model_rate', 16000)
            audio = _resample_audio(audio, capture_rate, model_rate)
            segments, info = self.app.model.transcribe(
                audio, language=self.app.config['language'], beam_size=5,
                vad_filter=True,
                initial_prompt=self.app.voice_training_window.get_initial_prompt())
            text = "".join([s.text for s in segments]).strip()
            text = self.app.voice_training_window.apply_corrections(text)
            if not text:
                if self.window:
                    self.window.after(0, self.log, "No speech detected")
                    self.window.after(0, self._reset_listening_state)
                return

            text_lower = text.lower()
            ww_config = self.app.config.get('wake_word_config', {})
            wake_phrase = ww_config.get('phrase', 'samsara').lower()
            if self.window:
                self.window.after(0, self.update_last_heard, text)

            # Apply wake-word correction map BEFORE matching. Keeps the raw
            # transcription for display/debugging, but matches against the
            # corrected form so known Whisper misrecognitions still trigger.
            corrected_lower = apply_wake_corrections(text_lower)
            correction_applied = was_corrected(text_lower, corrected_lower)

            self.trace({"stage": "utterance_start", "raw": text,
                        "normalized": text_lower, "corrected": corrected_lower,
                        "correction_applied": correction_applied})

            if self.dictation_mode:
                result = self._handle_dictation_input(text, text_lower, ww_config)
                self.trace({"stage": "utterance_end", "result": result})
                return

            matched, match_type, match_index = match_wake_phrase(corrected_lower, wake_phrase)
            self.trace({"stage": "wake_word_check", "input": text,
                        "normalized": text_lower, "corrected": corrected_lower,
                        "correction_applied": correction_applied,
                        "wake_phrase": wake_phrase, "matched": matched,
                        "match_type": match_type, "match_index": match_index})

            if matched:
                self.wake_word_triggered = True
                # Slice from the corrected string — match_index is a position
                # in corrected_lower, not text. Corrections may alter length.
                command = corrected_lower[match_index + len(wake_phrase):].strip()
                self.trace({"stage": "command_extract", "from_index": match_index,
                            "command": command, "remainder": ""})
                if command:
                    self._process_command_traced(command)
                else:
                    if self.window:
                        self.window.after(0, self.update_state, "Waiting for command...", "#FFD700")
                        self.window.after(0, self.update_flow, "Wake Word [*] -> Waiting...")
                    self.trace({"stage": "command_classify", "command": "",
                                "classification": "waiting_for_command", "matched_keyword": ""})
                self.trace({"stage": "utterance_end",
                            "result": "wake_word_detected" if not command else "command_processed"})
            elif self.wake_word_triggered:
                self.trace({"stage": "command_extract", "from_index": -1,
                            "command": text, "remainder": ""})
                self._process_command_traced(text)
                self.trace({"stage": "utterance_end", "result": "followup_command"})
            else:
                self.trace({"stage": "utterance_end", "result": "no_wake_word"})
                if self.window:
                    self.window.after(0, self._reset_listening_state)
        except Exception as e:
            if self.window:
                self.window.after(0, self.log, f"ERROR: {e}")
                self.window.after(0, self.update_state, "Error - see log", "#FF0000")

    def _handle_dictation_input(self, text, text_lower, ww_config):
        cancel_config = ww_config.get('cancel_word', {})
        if cancel_config.get('enabled', False):
            cancel_phrase = cancel_config.get('phrase', 'cancel').lower()
            if cancel_phrase in text_lower:
                self.trace({"stage": "cancel_word_detected", "phrase": cancel_phrase})
                self.dictation_mode = None
                self.dictation_buffer = []
                self.wake_word_triggered = False
                self._dictation_start_time = None
                if self.window:
                    self.window.after(0, self.update_mode, None)
                    self.window.after(0, self._reset_listening_state)
                return "cancelled"
        pause_config = ww_config.get('pause_word', {})
        if pause_config.get('enabled', False):
            pause_phrase = pause_config.get('phrase', 'pause').lower()
            if pause_phrase in text_lower:
                self.trace({"stage": "pause_word_detected", "phrase": pause_phrase})
                self.silence_start = None
                self._dictation_start_time = time.time()
                remaining = text_lower.replace(pause_phrase, '').strip()
                if remaining:
                    pause_idx = text_lower.find(pause_phrase)
                    cleaned = (text[:pause_idx] + text[pause_idx + len(pause_phrase):]).strip()
                    if cleaned:
                        self.dictation_buffer.append(cleaned)
                if self.window:
                    self.window.after(0, self._reset_listening_state)
                return "paused"
        end_config = ww_config.get('end_word', {})
        if end_config.get('enabled', False):
            end_phrase = end_config.get('phrase', 'over').lower()
            if end_phrase in text_lower:
                end_index = text_lower.rfind(end_phrase)
                final = text[:end_index].strip()
                if self.dictation_buffer:
                    final = ' '.join(self.dictation_buffer) + ' ' + final
                self.trace({"stage": "end_word_detected", "phrase": end_phrase,
                            "buffered_text": ' '.join(self.dictation_buffer),
                            "final_output": final.strip()})
                self.dictation_mode = None
                self.dictation_buffer = []
                self.wake_word_triggered = False
                self._dictation_start_time = None
                if self.window:
                    self.window.after(0, self.update_mode, None)
                    self.window.after(0, self._reset_listening_state)
                return "end_word"
        self.dictation_buffer.append(text)
        self._dictation_start_time = time.time()
        self.trace({"stage": "dictation_buffered", "text": text,
                    "buffer_size": len(self.dictation_buffer)})
        if self.window:
            self.window.after(0, self._reset_listening_state)
        return "buffered"

    def _process_command_traced(self, command):
        cl = command.lower().strip()
        for keywords, mode in [
            (['long dictate', 'long dictation'], 'long_dictate'),
            (['short dictate', 'short dictation', 'quick dictate'], 'short_dictate'),
            (['dictate', 'dictation'], 'dictate'),
        ]:
            if cl in keywords:
                self.trace({"stage": "command_classify", "command": command,
                            "classification": "dictation_mode", "matched_keyword": cl})
                self.trace({"stage": "mode_switch", "from_mode": self.dictation_mode, "to_mode": mode})
                self._enter_dictation_mode(mode, mode.upper().replace('_', ' '),
                                           f"Dictating ({mode.replace('_', ' ')})...")
                return
        for cmd, mode in [('long dictate', 'long_dictate'), ('short dictate', 'short_dictate'),
                          ('dictate', 'dictate')]:
            if cl.startswith(cmd + ' '):
                content = command[len(cmd):].strip()
                self.trace({"stage": "command_classify", "command": command,
                            "classification": "dictation_mode", "matched_keyword": cmd})
                self.trace({"stage": "mode_switch", "from_mode": self.dictation_mode, "to_mode": mode})
                self._enter_dictation_mode(mode, mode.upper().replace('_', ' '),
                                           f"Dictating ({mode.replace('_', ' ')})...",
                                           initial_content=content)
                return
        self.trace({"stage": "command_classify", "command": command,
                    "classification": "freeform_text", "matched_keyword": ""})
        if self.window:
            self.window.after(0, self.update_flow, "Wake Word -> Command -> Done")
        self.wake_word_triggered = False
        if self.window:
            self.window.after(0, self._reset_listening_state)

    # ==================================================================
    # Dictation mode helpers
    # ==================================================================

    def _enter_dictation_mode(self, mode, display_name, state_text, initial_content=None):
        self.dictation_mode = mode
        self.dictation_buffer = [initial_content] if initial_content else []
        self.wake_word_triggered = False
        self._dictation_start_time = time.time()
        if self.window:
            msg = f"-> Starting {display_name} with: \"{initial_content}\"" if initial_content else f"-> Starting {display_name} mode"
            self.window.after(0, self.log, msg)
            self.window.after(0, self.update_mode, mode)
            self.window.after(0, self.update_state, state_text, "#FFD700")
            self.window.after(0, self.update_flow, f"Wake Word -> {display_name} -> [Recording...]")

    def _reset_listening_state(self):
        if self.running:
            if self.dictation_mode:
                st, cl = f"Dictating ({self.dictation_mode.replace('_', ' ')})...", "#FFD700"
            elif self.wake_word_triggered:
                st, cl = "Waiting for command...", "#FFD700"
            else:
                st, cl = "Listening for wake word...", "#00CED1"
                if self.flow_label:
                    self.update_flow("Listening...")
                if self.timer_label:
                    self.timer_label.configure(text="--", text_color="#888888")
            self.update_state(st, cl)

    def _force_enter_mode(self):
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

    def _reset_test_mode(self):
        self.dictation_mode = None
        self.dictation_buffer = []
        self.wake_word_triggered = False
        self._dictation_start_time = None
        self.update_mode(None)
        self.update_flow("Idle")
        if self.timer_label:
            self.timer_label.configure(text="--", text_color="#888888")
        if self.running:
            self.update_state("Listening for wake word...", "#00CED1")
            self.log("Reset to wake word listening")

    def _simulate_word(self, word_type):
        if not self.dictation_mode:
            self.log("Not in dictation mode - enter a mode first")
            return
        ww_config = self.app.config.get('wake_word_config', {})
        if word_type == 'end':
            ec = ww_config.get('end_word', {})
            if not ec.get('enabled', False):
                self.log("End word is disabled in config"); return
            phrase = ec.get('phrase', 'over')
            self.log(f"[SIM] End word '{phrase}' - output: \"{' '.join(self.dictation_buffer) if self.dictation_buffer else '(empty)'}\"")
            self._reset_test_mode()
        elif word_type == 'cancel':
            cc = ww_config.get('cancel_word', {})
            if not cc.get('enabled', False):
                self.log("Cancel word is disabled in config"); return
            self.log(f"[SIM] Cancel word '{cc.get('phrase', 'cancel')}' - dictation aborted")
            self._reset_test_mode()
        elif word_type == 'pause':
            pc = ww_config.get('pause_word', {})
            if not pc.get('enabled', False):
                self.log("Pause word is disabled in config"); return
            self.silence_start = None
            self._dictation_start_time = time.time()
            self.log(f"[SIM] Pause word '{pc.get('phrase', 'pause')}' - timer reset")

    def _start_timer_update(self):
        pass

    def _update_timer_display(self):
        if not self.dictation_mode or not self._dictation_start_time:
            nt, nc = "--", "#888888"
            if nt != self._prev_timer_text:
                self._prev_timer_text = nt; self._prev_timer_color = nc
                self.timer_label.configure(text=nt, text_color=nc)
            return
        timeout = {'long_dictate': self.long_timeout_var, 'short_dictate': self.short_timeout_var
                   }.get(self.dictation_mode, self.dictate_timeout_var).get()
        remaining = max(0, timeout - (time.time() - self._dictation_start_time))
        if remaining > 0:
            nc = "#00FF00" if remaining > timeout * 0.3 else "#FF6B6B"
            nt = f"{remaining:.1f}s"
        else:
            nt, nc = "0.0s", "#FF0000"
        if nt != self._prev_timer_text or nc != self._prev_timer_color:
            self._prev_timer_text = nt; self._prev_timer_color = nc
            self.timer_label.configure(text=nt, text_color=nc)
        if remaining <= 0 and not self._get_require_end():
            self.log(f"Timer expired - would output: \"{' '.join(self.dictation_buffer)}\"")
            self._reset_test_mode()

    def _get_require_end(self):
        ww_config = self.app.config.get('wake_word_config', {})
        if self.dictation_mode:
            return ww_config.get('modes', {}).get(self.dictation_mode, {}).get('require_end_word', False)
        return False

    # ==================================================================
    # Window lifecycle
    # ==================================================================

    def on_close(self):
        self._stop_ui_poll()
        if self._log_flush_id is not None and self.window:
            try:
                self.window.after_cancel(self._log_flush_id)
            except Exception:
                pass
            self._log_flush_id = None
        self._log_buffer.clear()
        if hasattr(self.app, '_wake_trace_callback') and self.app._wake_trace_callback == self.on_app_trace:
            self.app._wake_trace_callback = None
        self.stop_test()
        if self.window:
            self.window.destroy()
            self.window = None

    def close(self):
        self.on_close()
