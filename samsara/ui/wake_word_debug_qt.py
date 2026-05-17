"""PySide6 wake word debug window — Phase 1.

Passive observation mode: receives trace events and log messages from
the live wake word pipeline via signals and displays them in real time.
Active test mode (standalone audio capture + transcription) is Phase 2.

Public API matches WakeWordDebugWindow exactly:
    show() / close() / on_app_trace(event)
"""

import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFrame,
    QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSlider,
    QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

_BG       = "#0b0e14"
_SURFACE  = "#131820"
_ELEVATED = "#1a2030"
_BORDER   = "#2a3345"
_ACCENT   = "#5cc4d4"
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI = "#e4e8ef"
_TEXT_SEC = "#7a8599"
_TEXT_DIS = "#4a5568"
_SUCCESS  = "#6ee7a0"
_ERROR    = "#f87171"
_WARNING  = "#fbbf24"
_GOLD     = "#FFD700"
_GREEN    = "#00FF00"
_CYAN     = "#00CED1"

_MAX_TIMELINE_UTTERANCES = 50

_SS = f"""
QMainWindow, QWidget {{
    background: {_BG};
    color: {_TEXT_PRI};
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
}}
QFrame[class="card"] {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 6px;
}}
QPlainTextEdit {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    font-family: 'Consolas', monospace;
    font-size: 11px;
    padding: 6px;
}}
QPushButton {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 5px 14px;
    font-size: 12px;
    min-width: 80px;
}}
QPushButton:hover  {{ background: {_ELEVATED}; border-color: {_ACCENT}; }}
QPushButton:pressed {{ background: {_ACCENT_DIM}; }}
QPushButton:disabled {{ color: {_TEXT_DIS}; border-color: {_TEXT_DIS}; }}
QSlider::groove:horizontal {{
    height: 4px; background: {_BORDER}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {_ACCENT}; width: 12px; height: 12px;
    margin: -4px 0; border-radius: 6px;
}}
QSlider::sub-page:horizontal {{ background: {_ACCENT}; border-radius: 2px; }}
QDoubleSpinBox, QComboBox {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 4px 8px;
    min-width: 70px;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {_ELEVATED}; border: 1px solid {_BORDER};
    color: {_TEXT_PRI};
    selection-background-color: {_ACCENT_DIM};
}}
QScrollBar:vertical {{
    background: {_BG}; width: 6px; border: none;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 3px; min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _card():
    f = QFrame()
    f.setProperty("class", "card")
    f.setStyleSheet(f"background: {_SURFACE}; border: 1px solid {_BORDER}; border-radius: 6px;")
    return f


def _section_label(text):
    lbl = QLabel(text)
    lbl.setFont(QFont("Segoe UI", 13, QFont.Bold))
    lbl.setStyleSheet(f"color: {_TEXT_PRI}; background: transparent;")
    return lbl


def _kv_label(text, color=_TEXT_SEC):
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {color}; background: transparent; font-size: 12px;")
    return lbl


def _btn(text, color=None):
    b = QPushButton(text)
    if color:
        b.setStyleSheet(
            f"background: {color}; border: 1px solid {color};"
            f" border-radius: 4px; color: white; padding: 5px 14px;"
        )
    return b


# ---------------------------------------------------------------------------
# The Qt window
# ---------------------------------------------------------------------------

class _DebugWindow(QMainWindow):
    # Signals for thread-safe updates from the wake word pipeline
    _trace_sig   = Signal(dict)
    _log_sig     = Signal(str)
    _state_sig   = Signal(str, str)   # text, colour
    _mode_sig    = Signal(object)     # str or None
    _heard_sig   = Signal(str)
    _flow_sig    = Signal(str)
    _eval_sig    = Signal(dict)
    _level_sig   = Signal(float)      # Phase 2: RMS from audio stream

    def __init__(self, app):
        super().__init__()
        self._app         = app
        self._trace_buf   = []          # list of utterance blocks
        self._current_blk = []          # in-flight utterance events
        self._log_pending = []          # lines not yet flushed to widget
        self._log_timer   = QTimer(self)
        self._log_timer.setSingleShot(True)
        self._log_timer.setInterval(200)
        self._log_timer.timeout.connect(self._flush_log)

        # Active-test state (populated by Phase 2)
        self.running        = False
        self.audio_stream   = None
        self._speech_thresh = 0.03
        self._min_speech    = 0.3
        self._dictate_to    = 2.0
        self._short_to      = 1.0
        self._long_to       = 60.0

        self.setWindowTitle("Wake Word Debug")
        self.setStyleSheet(_SS)
        self.resize(750, 900)
        self.setMinimumSize(680, 700)

        self._build_ui()
        self._connect_signals()

    # ----------------------------------------------------------------
    # UI construction
    # ----------------------------------------------------------------

    def _build_ui(self):
        # Outer scroll area so the whole window scrolls on small screens
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(f"background: {_BG};")
        self.setCentralWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        lay = QVBoxLayout(container)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(16)

        lay.addWidget(_section_label("Status"))
        lay.addWidget(self._build_status_card())

        lay.addWidget(_section_label("Wake Word Evaluation"))
        lay.addWidget(self._build_eval_card())

        lay.addWidget(_section_label("Audio Level"))
        lay.addWidget(self._build_level_card())

        lay.addWidget(_section_label("Tunable Parameters"))
        lay.addWidget(self._build_params_card())

        lay.addWidget(self._build_buttons_row())

        lay.addWidget(_section_label("Test Controls"))
        lay.addWidget(self._build_test_controls_card())

        lay.addWidget(_section_label("Decision Timeline"))
        lay.addWidget(self._build_timeline_card())

        lay.addWidget(_section_label("Event Log"))
        lay.addWidget(self._build_log_card())

        lay.addStretch()

    def _build_status_card(self):
        card = _card()
        lay  = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        ww   = self._app.config.get('wake_word_config', {})
        phrase  = ww.get('phrase', 'samsara')
        end_cfg = ww.get('end_word', {})
        end_txt = f'"{end_cfg.get("phrase", "over")}"' if end_cfg.get('enabled') else "(disabled)"
        end_col = _CYAN if end_cfg.get('enabled') else _TEXT_SEC

        self._state_lbl = self._kv_row(lay, "State:",        "Idle",          _TEXT_SEC)
        self._wake_lbl  = self._kv_row(lay, "Wake phrase:",  f'"{phrase}"',   _CYAN)
        self._end_lbl   = self._kv_row(lay, "End word:",     end_txt,         end_col)
        self._mode_lbl  = self._kv_row(lay, "Dict. mode:",   "None",          _TEXT_SEC)
        self._timer_lbl = self._kv_row(lay, "Timer:",        "--",            _TEXT_SEC)
        self._flow_lbl  = self._kv_row(lay, "Flow:",         "Idle",          _TEXT_SEC, wrap=True)
        self._heard_lbl = self._kv_row(lay, "Last heard:",   "(nothing yet)", _TEXT_SEC, wrap=True)
        return card

    def _build_eval_card(self):
        card = _card()
        lay  = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(4)

        self._ev_raw      = self._eval_row(lay, "Raw text:")
        self._ev_norm     = self._eval_row(lay, "Normalized:")
        self._ev_corrected= self._eval_row(lay, "Corrected:")
        self._ev_phrase   = self._eval_row(lay, "Wake phrase:")
        self._ev_match    = self._eval_row(lay, "Match:")
        self._ev_method   = self._eval_row(lay, "Match method:")
        self._ev_index    = self._eval_row(lay, "Match index:")
        return card

    def _build_level_card(self):
        card = _card()
        lay  = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        bar_row = QHBoxLayout()
        self._level_bar = QSlider(Qt.Horizontal)
        self._level_bar.setRange(0, 1000)
        self._level_bar.setValue(0)
        self._level_bar.setEnabled(False)
        self._level_bar.setFixedHeight(20)
        self._level_val = _kv_label("0.000")
        bar_row.addWidget(self._level_bar, stretch=1)
        bar_row.addWidget(self._level_val)
        lay.addLayout(bar_row)

        thresh = self._app.config.get('wake_word_config', {}).get('audio', {}).get('speech_threshold', 0.03)
        self._thresh_lbl = self._kv_row(lay, "Speech threshold:", f"{thresh:.3f}", _TEXT_SEC)
        return card

    def _build_params_card(self):
        card = _card()
        lay  = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        audio_cfg = self._app.config.get('wake_word_config', {}).get('audio', {})
        modes_cfg = self._app.config.get('wake_word_config', {}).get('modes', {})

        self._thresh_spin, _ = self._slider_row(
            lay, "Speech threshold:",
            audio_cfg.get('speech_threshold', 0.03), 0.005, 0.15, 3,
            lambda v: self._on_thresh_change(v))
        self._min_spin, _ = self._slider_row(
            lay, "Min speech (s):",
            audio_cfg.get('min_speech_duration', 0.3), 0.1, 2.0, 1)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        lay.addWidget(sep)

        to_lbl = QLabel("Mode Timeouts:")
        to_lbl.setStyleSheet(f"color: {_TEXT_PRI}; font-weight: 600;")
        lay.addWidget(to_lbl)

        self._dictate_spin, _ = self._slider_row(
            lay, "dictate:",
            modes_cfg.get('dictate', {}).get('silence_timeout', 2.0), 0.5, 10.0, 1)
        self._short_spin, _ = self._slider_row(
            lay, "short dictate:",
            modes_cfg.get('short_dictate', {}).get('silence_timeout', 1.0), 0.3, 5.0, 1)
        self._long_spin, _ = self._slider_row(
            lay, "long dictate:",
            modes_cfg.get('long_dictate', {}).get('silence_timeout', 60.0), 10.0, 120.0, 0)
        return card

    def _build_buttons_row(self):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._start_btn = _btn("Start Test", "#1a5c1a")
        self._stop_btn  = _btn("Stop",       "#5c1a1a")
        self._stop_btn.setEnabled(False)
        clear_btn   = _btn("Clear Log")
        apply_btn   = _btn("Apply to Config")
        apply_btn.setStyleSheet(
            f"background: {_ACCENT_DIM}; border: 1px solid {_ACCENT};"
            f" border-radius: 4px; color: {_ACCENT}; padding: 5px 14px;")

        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn.clicked.connect(self._on_stop)
        clear_btn.clicked.connect(self._clear_log)
        apply_btn.clicked.connect(self._apply_to_config)

        lay.addWidget(self._start_btn)
        lay.addWidget(self._stop_btn)
        lay.addWidget(clear_btn)
        lay.addStretch()
        lay.addWidget(apply_btn)
        return row

    def _build_test_controls_card(self):
        card = _card()
        lay  = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        mode_row = QHBoxLayout()
        mode_row.addWidget(_kv_label("Force mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["(auto)", "dictate", "short_dictate", "long_dictate"])
        enter_btn = _btn("Enter Mode")
        reset_btn = _btn("Reset")
        enter_btn.clicked.connect(self._force_enter_mode)
        reset_btn.clicked.connect(self._reset_test_mode)
        mode_row.addWidget(self._mode_combo)
        mode_row.addWidget(enter_btn)
        mode_row.addWidget(reset_btn)
        mode_row.addStretch()
        lay.addLayout(mode_row)

        sim_row = QHBoxLayout()
        sim_row.addWidget(_kv_label("Simulate:"))
        for label, wtype in [("End Word", "end"), ("Cancel Word", "cancel"), ("Pause Word", "pause")]:
            b = _btn(label)
            b.clicked.connect(lambda _, w=wtype: self._simulate_word(w))
            sim_row.addWidget(b)
        sim_row.addStretch()
        lay.addLayout(sim_row)
        return card

    def _build_timeline_card(self):
        card = _card()
        lay  = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self._timeline = QPlainTextEdit()
        self._timeline.setReadOnly(True)
        self._timeline.setFixedHeight(200)
        lay.addWidget(self._timeline)

        btn_row = QHBoxLayout()
        export_btn = _btn("Export Timeline")
        clear_btn  = _btn("Clear Timeline")
        export_btn.clicked.connect(self._export_timeline)
        clear_btn.clicked.connect(self._clear_timeline)
        btn_row.addWidget(export_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        return card

    def _build_log_card(self):
        card = _card()
        lay  = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(160)
        lay.addWidget(self._log)
        return card

    # ---- Layout helpers ---------------------------------------------------

    @staticmethod
    def _kv_row(parent_lay, key, value, color=_TEXT_SEC, wrap=False):
        row = QHBoxLayout()
        row.setSpacing(8)
        key_lbl = QLabel(key)
        key_lbl.setFixedWidth(120)
        key_lbl.setStyleSheet(f"color: {_TEXT_SEC}; background: transparent;")
        val_lbl = QLabel(value)
        val_lbl.setStyleSheet(f"color: {color}; background: transparent;")
        if wrap:
            val_lbl.setWordWrap(True)
        row.addWidget(key_lbl)
        row.addWidget(val_lbl, stretch=1)
        parent_lay.addLayout(row)
        return val_lbl

    @staticmethod
    def _eval_row(parent_lay, key):
        row = QHBoxLayout()
        row.setSpacing(8)
        key_lbl = QLabel(key)
        key_lbl.setFixedWidth(120)
        key_lbl.setStyleSheet(f"color: {_TEXT_DIS}; background: transparent;")
        val_lbl = QLabel("--")
        val_lbl.setStyleSheet(f"color: {_TEXT_SEC}; background: transparent;")
        row.addWidget(key_lbl)
        row.addWidget(val_lbl, stretch=1)
        parent_lay.addLayout(row)
        return val_lbl

    @staticmethod
    def _slider_row(parent_lay, label, init, lo, hi, decimals, on_change=None):
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label)
        lbl.setFixedWidth(140)
        lbl.setStyleSheet(f"color: {_TEXT_SEC}; background: transparent;")
        slider = QSlider(Qt.Horizontal)
        steps  = 1000
        slider.setRange(0, steps)
        slider.setValue(int((init - lo) / (hi - lo) * steps))
        val_lbl = QLabel(f"{init:.{decimals}f}")
        val_lbl.setFixedWidth(50)
        val_lbl.setStyleSheet(f"color: {_TEXT_PRI}; background: transparent;")
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setDecimals(decimals)
        spin.setSingleStep(10 ** -decimals)
        spin.setValue(init)
        spin.setFixedWidth(80)

        def _sync_slider(v):
            slider.blockSignals(True)
            slider.setValue(int((v - lo) / (hi - lo) * steps))
            slider.blockSignals(False)
            val_lbl.setText(f"{v:.{decimals}f}")
            if on_change:
                on_change(v)

        def _sync_spin(pos):
            v = lo + (pos / steps) * (hi - lo)
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)
            val_lbl.setText(f"{v:.{decimals}f}")
            if on_change:
                on_change(v)

        slider.valueChanged.connect(_sync_spin)
        spin.valueChanged.connect(_sync_slider)

        row.addWidget(lbl)
        row.addWidget(slider, stretch=1)
        row.addWidget(val_lbl)
        row.addWidget(spin)
        parent_lay.addLayout(row)
        return spin, val_lbl

    # ----------------------------------------------------------------
    # Signal wiring
    # ----------------------------------------------------------------

    def _connect_signals(self):
        self._trace_sig.connect(self._on_trace)
        self._log_sig.connect(self._append_log)
        self._state_sig.connect(self._set_state)
        self._mode_sig.connect(self._set_mode)
        self._heard_sig.connect(self._set_heard)
        self._flow_sig.connect(self._set_flow)
        self._eval_sig.connect(self._update_eval)
        self._level_sig.connect(self._update_level)

    # ----------------------------------------------------------------
    # Trace pipeline (mirrors the Tkinter version)
    # ----------------------------------------------------------------

    def trace(self, event: dict):
        """Feed a structured wake-word pipeline event. Thread-safe."""
        event.setdefault('_time', datetime.now().strftime("%H:%M:%S"))
        stage = event.get('stage', '')

        if stage == 'utterance_start':
            self._current_blk = [event]
        elif stage == 'utterance_end':
            self._current_blk.append(event)
            block = list(self._current_blk)
            self._current_blk = []
            self._trace_buf.append(block)
            if len(self._trace_buf) > _MAX_TIMELINE_UTTERANCES:
                self._trace_buf.pop(0)
            self._trace_sig.emit({'_block': block})
        else:
            self._current_blk.append(event)

        if stage == 'wake_word_check':
            self._eval_sig.emit(event)

        line = self._format_trace_line(event)
        if line:
            self.log(line)

    @staticmethod
    def _format_trace_line(event: dict):
        stage = event.get('stage', '')
        if stage == 'utterance_start':
            return f'--- Heard: "{event.get("raw", "")}"'
        if stage == 'wake_word_check':
            m = event.get('matched', False)
            t = event.get('match_type', 'none')
            i = event.get('match_index', -1)
            return f"[WAKE_MATCH] {'YES' if m else 'NO'} ({t} @ idx {i})"
        if stage == 'command_extract':
            cmd = event.get('command', '')
            return f'[CMD_EXTRACT] "{cmd}"' if cmd else None
        if stage == 'command_classify':
            cls = event.get('classification', '')
            kw  = event.get('matched_keyword', '')
            return f'[CLASSIFY] {cls}' + (f' (keyword: "{kw}")' if kw else '')
        if stage == 'mode_switch':
            return f"[MODE] -> {event.get('to_mode', '?')}"
        if stage == 'dictation_buffered':
            return f'[BUFFERED] "{event.get("text", "")}" (buf={event.get("buffer_size", 0)})'
        if stage == 'end_word_detected':
            return f'[END_WORD] "{event.get("phrase", "")}" -> output: "{event.get("final_output", "")}"'
        if stage in ('cancel_word_detected', 'pause_word_detected'):
            return f'[{stage.upper().split("_")[0]}] "{event.get("phrase", "")}"'
        if stage == 'utterance_end':
            return f"--- Result: {event.get('result', '?')}"
        return None

    @staticmethod
    def _timeline_detail(ev: dict) -> str:
        stage = ev.get('stage', '')
        if stage == 'wake_word_check':
            m = ev.get('matched', False)
            t = ev.get('match_type', 'none')
            i = ev.get('match_index', -1)
            return f"-> {'TRUE' if m else 'FALSE'} ({t} @ idx {i})"
        if stage == 'command_extract':
            return f'-> "{ev.get("command", "")}"'
        if stage == 'command_classify':
            cls = ev.get('classification', '')
            kw  = ev.get('matched_keyword', '')
            return f'-> {cls}' + (f' (keyword: "{kw}")' if kw else '')
        if stage == 'mode_switch':
            return f"-> {ev.get('to_mode', '?')}"
        if stage == 'dictation_buffered':
            return f'-> "{ev.get("text", "")}" (buf_size={ev.get("buffer_size", 0)})'
        if stage == 'end_word_detected':
            return f'-> output: "{ev.get("final_output", "")}"'
        if stage in ('cancel_word_detected', 'pause_word_detected'):
            return f'-> "{ev.get("phrase", "")}"'
        return ""

    @Slot(dict)
    def _on_trace(self, event: dict):
        block = event.get('_block')
        if block is not None:
            self._render_timeline_block(block)

    def _render_timeline_block(self, block: list):
        if not block:
            return
        ts  = block[0].get('_time', '')
        raw = block[0].get('raw', '')
        norm = block[0].get('normalized', '')
        corrected = block[0].get('corrected', '')
        correction_applied = block[0].get('correction_applied', False)

        lines = [f"{'=' * 40} UTTERANCE [{ts}]"]
        lines.append(f'RAW:  "{raw}"')
        lines.append(f'NORM: "{norm}"')
        if correction_applied:
            lines.append(f'CORR: "{corrected}"   [correction applied]')
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
        lines.append("=" * 52)
        lines.append("")

        self._timeline.appendPlainText("\n".join(lines))
        sb = self._timeline.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ----------------------------------------------------------------
    # Update slots
    # ----------------------------------------------------------------

    @Slot(str, str)
    def _set_state(self, text: str, color: str):
        self._state_lbl.setText(text)
        self._state_lbl.setStyleSheet(f"color: {color}; background: transparent;")

    @Slot(object)
    def _set_mode(self, mode):
        if mode:
            self._mode_lbl.setText(mode.replace('_', ' ').title())
            self._mode_lbl.setStyleSheet(f"color: {_GOLD}; background: transparent;")
        else:
            self._mode_lbl.setText("None")
            self._mode_lbl.setStyleSheet(f"color: {_TEXT_SEC}; background: transparent;")

    @Slot(str)
    def _set_heard(self, text: str):
        display = text if len(text) < 70 else text[:67] + "..."
        self._heard_lbl.setText(f'"{display}"')
        self._heard_lbl.setStyleSheet(f"color: {_TEXT_PRI}; background: transparent;")

    @Slot(str)
    def _set_flow(self, text: str):
        self._flow_lbl.setText(text)

    @Slot(dict)
    def _update_eval(self, event: dict):
        def _clamp(s):
            return s if len(s) < 70 else s[:67] + "..."

        raw  = event.get('input', '')
        norm = event.get('normalized', '')
        corr = event.get('corrected', '')
        correction_applied = event.get('correction_applied', False)
        phrase  = event.get('wake_phrase', '')
        matched = event.get('matched', False)
        mtype   = event.get('match_type', 'none')
        idx     = event.get('match_index', -1)

        self._ev_raw.setText(_clamp(raw))
        self._ev_norm.setText(_clamp(norm))
        if correction_applied:
            self._ev_corrected.setText(_clamp(corr))
            self._ev_corrected.setStyleSheet(f"color: {_GOLD}; background: transparent;")
        else:
            self._ev_corrected.setText("(no correction applied)")
            self._ev_corrected.setStyleSheet(f"color: {_TEXT_DIS}; background: transparent;")
        self._ev_phrase.setText(f'"{phrase}"')
        self._ev_match.setText("YES" if matched else "NO")
        self._ev_match.setStyleSheet(
            f"color: {_GREEN if matched else _TEXT_DIS}; background: transparent;")
        self._ev_method.setText(mtype)
        self._ev_index.setText(str(idx) if idx >= 0 else "--")

    @Slot(float)
    def _update_level(self, rms: float):
        self._level_bar.setValue(min(1000, int(rms * 10000)))
        self._level_val.setText(f"{rms:.3f}")
        thresh = self._thresh_spin.value()
        color  = _GREEN if rms > thresh else _ACCENT
        self._level_bar.setStyleSheet(
            f"QSlider::sub-page:horizontal {{ background: {color}; border-radius: 2px; }}")

    def _on_thresh_change(self, value: float):
        self._thresh_lbl.setText(f"{value:.3f}")

    # ----------------------------------------------------------------
    # Logging
    # ----------------------------------------------------------------

    def log(self, message: str):
        """Thread-safe log append."""
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_sig.emit(f"[{ts}] {message}")

    @Slot(str)
    def _append_log(self, line: str):
        self._log_pending.append(line)
        if not self._log_timer.isActive():
            self._log_timer.start()

    def _flush_log(self):
        if not self._log_pending:
            return
        text = "\n".join(self._log_pending)
        self._log_pending.clear()
        self._log.appendPlainText(text)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _clear_log(self):
        self._log.clear()

    # ----------------------------------------------------------------
    # Timeline export / clear
    # ----------------------------------------------------------------

    def _export_timeline(self):
        if not self._trace_buf:
            self.log("Nothing to export.")
            return
        docs = Path(__file__).parent.parent.parent / "docs"
        docs.mkdir(exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = docs / f"wake_word_trace_{ts}.txt"
        lines = []
        for block in self._trace_buf:
            if not block:
                continue
            bts = block[0].get('_time', '')
            raw = block[0].get('raw', '')
            lines.append(f"{'=' * 40} UTTERANCE [{bts}]")
            lines.append(f'RAW:  "{raw}"')
            lines.append("")
            step = 1
            for ev in block[1:]:
                stage = ev.get('stage', '')
                if stage == 'utterance_end':
                    lines.append(f"  -> RESULT: {ev.get('result', '?')}")
                    continue
                lines.append(f"[{step}] {stage:22s} {self._timeline_detail(ev)}")
                step += 1
            lines.append("=" * 52)
            lines.append("")
        path.write_text("\n".join(lines), encoding='utf-8')
        self.log(f"Exported to {path.name}")

    def _clear_timeline(self):
        self._trace_buf.clear()
        self._timeline.clear()

    # ----------------------------------------------------------------
    # Config
    # ----------------------------------------------------------------

    def _apply_to_config(self):
        ww = self._app.config.setdefault('wake_word_config', {})
        ww.setdefault('audio', {})['speech_threshold']    = self._thresh_spin.value()
        ww['audio']['min_speech_duration']                = self._min_spin.value()
        modes = ww.setdefault('modes', {})
        modes['dictate']       = {'silence_timeout': self._dictate_spin.value(), 'require_end_word': False}
        modes['short_dictate'] = {'silence_timeout': self._short_spin.value(),   'require_end_word': False}
        modes['long_dictate']  = {'silence_timeout': self._long_spin.value(),    'require_end_word': True}
        self._app.persist_config()
        self.log("Settings applied and saved.")

    # ----------------------------------------------------------------
    # Test controls (Phase 2 fills _on_start / _on_stop)
    # ----------------------------------------------------------------

    def _on_start(self):
        self.log("[Phase 2] Standalone audio test not yet implemented.")

    def _on_stop(self):
        pass

    def _force_enter_mode(self):
        mode = self._mode_combo.currentText()
        if mode == "(auto)":
            self.log("Select a mode (not 'auto')")
            return
        if not self.running:
            self.log("Start the test first before forcing a mode.")
            return
        self._mode_sig.emit(mode)
        self.log(f"Forced into {mode} mode.")

    def _reset_test_mode(self):
        self._mode_sig.emit(None)
        self._flow_sig.emit("Idle")
        self._timer_lbl.setText("--")
        self._timer_lbl.setStyleSheet(f"color: {_TEXT_SEC}; background: transparent;")
        if self.running:
            self._state_sig.emit("Listening for wake word...", _CYAN)
            self.log("Reset to wake word listening.")

    def _simulate_word(self, word_type: str):
        if not self.running:
            self.log("Not running — start test first.")
            return
        ww = self._app.config.get('wake_word_config', {})
        cfg_key = {'end': 'end_word', 'cancel': 'cancel_word', 'pause': 'pause_word'}[word_type]
        cfg = ww.get(cfg_key, {})
        if not cfg.get('enabled', False):
            self.log(f"{word_type} word is disabled in config.")
            return
        phrase = cfg.get('phrase', word_type)
        self.log(f"[SIM] {word_type} word '{phrase}'")

    # ----------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------

    def update_state(self, text: str, color: str = _TEXT_SEC):
        self._state_sig.emit(text, color)

    def update_mode(self, mode):
        self._mode_sig.emit(mode)

    def update_last_heard(self, text: str):
        self._heard_sig.emit(text)

    def update_flow(self, text: str):
        self._flow_sig.emit(text)

    def closeEvent(self, e):
        self._log_timer.stop()
        if hasattr(self._app, '_wake_trace_callback'):
            if getattr(self._app, '_wake_trace_callback', None) is not None:
                cb = self._app._wake_trace_callback
                if hasattr(cb, '__self__') and cb.__self__ is self:
                    self._app._wake_trace_callback = None
        e.accept()


# ---------------------------------------------------------------------------
# Public wrapper — same API as WakeWordDebugWindow
# ---------------------------------------------------------------------------

class WakeWordDebugQt:
    """Drop-in Qt replacement for WakeWordDebugWindow."""

    def __init__(self, app):
        self._app    = app
        self._window: "_DebugWindow | None" = None
        self._thread = None

    # ---- Public API (callable from any thread) ------------------------------

    def show(self):
        if self._window is not None:
            QTimer.singleShot(0, self._window.show)
            QTimer.singleShot(0, self._window.raise_)
            QTimer.singleShot(0, self._window.activateWindow)
        else:
            self._thread = threading.Thread(
                target=self._create, daemon=True, name="wake-debug-qt",
            )
            self._thread.start()

    def close(self):
        if self._window is not None:
            QTimer.singleShot(0, self._window.close)
            self._window = None

    def on_app_trace(self, event: dict):
        """Called from the wake word pipeline. Thread-safe."""
        if self._window is not None:
            self._window.trace(event)

    # ---- Thread -------------------------------------------------------------

    def _create(self):
        qt_app   = QApplication.instance()
        owns_app = qt_app is None
        if qt_app is None:
            qt_app = QApplication([])
        if owns_app:
            self._init_window()
            qt_app.exec()
            self._window = None
        else:
            QTimer.singleShot(0, qt_app, self._init_window)

    def _init_window(self):
        self._window = _DebugWindow(self._app)
        self._window.destroyed.connect(self._on_destroyed)
        # Register as the app's trace callback
        if hasattr(self._app, '_wake_trace_callback'):
            self._app._wake_trace_callback = self.on_app_trace
        self._window.show()

    def _on_destroyed(self):
        self._window = None
