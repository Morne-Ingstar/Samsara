"""PySide6 Voice Training window for Samsara.

Drop-in replacement for VoiceTrainingWindow with the same public API:
    show() / close()
    get_initial_prompt() -> str | None
    apply_corrections(text) -> str
    load_training_data() / save_training_data()

Runs on the existing samsara-qt thread via QTimer.singleShot.
Mic monitoring runs on a daemon thread; UI updates are marshalled back
via Signal so Qt never touches audio buffers from a foreign thread.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import List, Tuple

import numpy as np
import sounddevice as sd
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from samsara.ui import qt_runtime
from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Words Whisper already handles reliably.  Do NOT inject these into the
# initial prompt — prompt bleeding risk outweighs any benefit.
# ---------------------------------------------------------------------------

_COMMON_ENGLISH = {
    'open', 'close', 'copy', 'cut', 'paste', 'undo', 'redo', 'save',
    'find', 'print', 'bold', 'italic', 'underline', 'escape', 'submit',
    'space', 'tab', 'backspace', 'delete', 'select', 'all', 'mute',
    'zoom', 'scroll', 'up', 'down', 'left', 'right', 'new', 'next',
    'previous', 'show', 'hide', 'go', 'back', 'forward', 'hold',
    'stop', 'release', 'press', 'double', 'click', 'line', 'word',
    'page', 'period', 'comma', 'colon', 'quote', 'dash', 'ask',
    'use', 'switch', 'volume', 'play', 'pause', 'search', 'for',
    'to', 'the', 'my', 'me', 'a', 'an', 'is', 'in', 'on', 'at',
}

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

_BG       = "#0b0e14"
_SURFACE  = "#131820"
_ELEVATED = "#1a2030"
_BORDER   = "#2a3345"
_ACCENT   = "#5cc4d4"
_TEXT_PRI = "#e4e8ef"
_TEXT_SEC = "#7a8599"
_SUCCESS  = "#6ee7a0"
_ERROR    = "#f87171"
_WARNING  = "#fbbf24"

_SS = f"""
QMainWindow, QDialog, QWidget {{
    background: {_BG};
    color: {_TEXT_PRI};
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {_BORDER};
    background: {_BG};
}}
QTabBar::tab {{
    background: {_SURFACE};
    color: {_TEXT_SEC};
    padding: 7px 18px;
    border: none;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {_ELEVATED};
    color: {_ACCENT};
    border-bottom: 2px solid {_ACCENT};
}}
QTabBar::tab:hover:!selected {{ color: {_TEXT_PRI}; }}
QLineEdit, QTextEdit, QPlainTextEdit {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    color: {_TEXT_PRI};
    padding: 4px 8px;
    border-radius: 4px;
}}
QLineEdit:focus, QTextEdit:focus {{ border-color: {_ACCENT}; }}
QPushButton {{
    background: {_ELEVATED};
    color: {_TEXT_PRI};
    border: 1px solid {_BORDER};
    padding: 5px 14px;
    border-radius: 4px;
}}
QPushButton:hover {{
    background: {_ACCENT};
    color: {_BG};
    border-color: {_ACCENT};
}}
QPushButton:pressed {{ background: #4aa8b8; }}
QPushButton#danger {{
    color: {_ERROR};
    border-color: {_ERROR};
}}
QPushButton#danger:hover {{
    background: {_ERROR};
    color: {_BG};
}}
QListWidget {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    color: {_TEXT_PRI};
    outline: none;
}}
QListWidget::item {{ padding: 3px 8px; }}
QListWidget::item:selected {{ background: {_ACCENT}; color: {_BG}; }}
QTableWidget {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    gridline-color: {_BORDER};
    color: {_TEXT_PRI};
    outline: none;
}}
QTableWidget::item:selected {{ background: {_ACCENT}; color: {_BG}; }}
QHeaderView::section {{
    background: {_ELEVATED};
    color: {_TEXT_SEC};
    border: none;
    border-right: 1px solid {_BORDER};
    padding: 4px 8px;
    font-size: 11px;
    font-weight: bold;
}}
QComboBox {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    color: {_TEXT_PRI};
    padding: 4px 8px;
    border-radius: 4px;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {_SURFACE};
    color: {_TEXT_PRI};
    selection-background-color: {_ACCENT};
    selection-color: {_BG};
    border: 1px solid {_BORDER};
}}
QScrollBar:vertical {{
    background: {_BG};
    width: 6px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QProgressBar {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {_SUCCESS}; border-radius: 3px; }}
"""


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------

class VoiceTrainingQt:
    """Drop-in Qt replacement for VoiceTrainingWindow."""

    def __init__(self, app):
        self.app = app
        self._window: "_TrainingWindow | None" = None
        self._monitoring = False
        self._init_posted = False
        self.custom_vocab: List[str] = []
        self.corrections_dict: dict = {}
        self._corrections_pattern = None
        self._corrections_lookup: dict = {}
        self.load_training_data()

    # ----------------------------------------------------------------
    # Backwards-compat: dictation.py checks .window before calling show()
    # ----------------------------------------------------------------

    @property
    def window(self):
        return self._window

    # ----------------------------------------------------------------
    # Pure-logic API — no UI dependency, safe from any thread
    # ----------------------------------------------------------------

    def load_training_data(self):
        training_file = Path(self.app.config_path).parent / 'training_data.json'
        if training_file.exists():
            try:
                with open(training_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.custom_vocab     = data.get('vocabulary', [])
                self.corrections_dict = data.get('corrections', {})
            except Exception as exc:
                logger.error(f"Could not load training data: {exc}", exc_info=True)
        self._rebuild_corrections_pattern()

    def save_training_data(self) -> bool:
        training_file = Path(self.app.config_path).parent / 'training_data.json'
        try:
            data = {'vocabulary': self.custom_vocab, 'corrections': self.corrections_dict}
            with open(training_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as exc:
            logger.error(f"Could not save training data: {exc}", exc_info=True)
            return False

    # Whisper's initial_prompt budget is ~224 tokens; 800 chars is a safe
    # character-based proxy that keeps well clear of that limit.
    _PROMPT_CHAR_BUDGET = 800

    def get_initial_prompt(self) -> "str | None":
        try:
            parts: List[str] = []
            remaining = self._PROMPT_CHAR_BUDGET

            # Priority 1: custom prompt -- explicit user input, never
            # truncated or dropped for size.
            custom_prompt = self.app.config.get('initial_prompt', '')
            if custom_prompt:
                parts.append(custom_prompt)
                remaining -= len(custom_prompt)

            # Priority 2: custom vocabulary -- included whole or not at all
            # (never truncate mid-phrase).
            if self.custom_vocab and remaining > 0:
                vocab_part = f"Common terms: {', '.join(self.custom_vocab)}"
                needed = len(vocab_part) + (1 if parts else 0)
                if needed <= remaining:
                    parts.append(vocab_part)
                    remaining -= needed

            # Priority 3: command vocabulary -- lowest priority, so it's the
            # one truncated (item-by-item, never mid-word) to fit what's left.
            if remaining > 0:
                cmd_words = self._get_command_vocabulary_words()
                kept: List[str] = []
                for word in cmd_words:
                    candidate = f"Voice commands: {', '.join(kept + [word])}"
                    needed = len(candidate) + (1 if parts else 0)
                    if needed > remaining:
                        break
                    kept.append(word)
                if kept:
                    parts.append(f"Voice commands: {', '.join(kept)}")

            return " ".join(parts) if parts else None
        except Exception as exc:
            logger.error(f"Error building initial prompt: {exc}", exc_info=True)
            return None

    def _rebuild_corrections_pattern(self):
        """Recompile the single-pass corrections regex from corrections_dict.

        Called whenever corrections_dict is mutated (add/remove/clear/import/
        load) so apply_corrections() never sees a stale pattern.
        """
        keys = [k for k in self.corrections_dict if k]
        if not keys:
            self._corrections_pattern = None
            self._corrections_lookup = {}
            return

        # Longest-first so "going to" wins over "going" on overlapping matches.
        keys_sorted = sorted(keys, key=len, reverse=True)
        lookup: dict = {}
        parts = []
        for key in keys_sorted:
            lookup[key.lower()] = self.corrections_dict[key]
            prefix = r'\b' if re.match(r'\w', key[0]) else ''
            suffix = r'\b' if re.match(r'\w', key[-1]) else ''
            parts.append(prefix + re.escape(key) + suffix)

        self._corrections_lookup = lookup
        self._corrections_pattern = re.compile('|'.join(parts), re.IGNORECASE)

    def apply_corrections(self, text: str) -> str:
        try:
            if self._corrections_pattern is None:
                return text

            def _replace(match: "re.Match") -> str:
                matched = match.group(0)
                replacement = self._corrections_lookup.get(matched.lower(), matched)
                if matched.isupper():
                    return replacement.upper()
                if matched[:1].isupper():
                    return replacement[:1].upper() + replacement[1:]
                return replacement

            return self._corrections_pattern.sub(_replace, text)
        except Exception as exc:
            logger.error(f"Error applying corrections: {exc}", exc_info=True)
            return text

    def calculate_similarity(self, s1: str, s2: str) -> float:
        """Word-set Jaccard similarity as a percentage (0–100)."""
        return _word_similarity(s1, s2)

    def _get_command_vocabulary_words(self) -> List[str]:
        try:
            matcher = None
            cmd_exec = getattr(self.app, 'command_executor', None)
            if cmd_exec is not None:
                matcher = getattr(cmd_exec, '_matcher', None)

            vocab_words: set = set()
            if matcher is not None:
                for entry in matcher.list_commands():
                    for phrase in [entry.get('phrase', '')] + list(entry.get('aliases', [])):
                        if not phrase:
                            continue
                        tokens = phrase.split()
                        if len(tokens) >= 2:
                            if any(t not in _COMMON_ENGLISH for t in tokens):
                                vocab_words.add(phrase)
                        elif len(tokens) == 1 and tokens[0] not in _COMMON_ENGLISH:
                            vocab_words.add(tokens[0])

            for cfg_key in ('web_shortcuts', 'audio_devices'):
                cfg_map = self.app.config.get(cfg_key, {}) or {}
                for key in cfg_map:
                    key_lower = key.lower().strip()
                    if not key_lower:
                        continue
                    tokens = key_lower.split()
                    if len(tokens) >= 2:
                        if any(t not in _COMMON_ENGLISH for t in tokens):
                            vocab_words.add(key_lower)
                    elif tokens and tokens[0] not in _COMMON_ENGLISH:
                        vocab_words.add(tokens[0])

            return sorted(vocab_words)[:36]
        except Exception as exc:
            logger.error(f"Error extracting command vocabulary: {exc}")
            return []

    # ----------------------------------------------------------------
    # Window lifecycle — safe to call from any thread
    # ----------------------------------------------------------------

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
            qt_runtime.post(self._window.activateWindow)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def close(self):
        self._monitoring = False
        if self._window is not None:
            qt_runtime.post(self._window.close)

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _TrainingWindow(self)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()

    def _on_destroyed(self):
        self._monitoring = False
        self._window = None
        self._init_posted = False


# ---------------------------------------------------------------------------
# Internal window
# ---------------------------------------------------------------------------

class _TrainingWindow(QMainWindow):

    _level_sig         = Signal(float)
    _phrase_sig        = Signal(int, str, str)   # (idx, text, colour)
    _phrase_detail_sig = Signal(str)             # mismatch detail for popup

    def __init__(self, training: VoiceTrainingQt):
        super().__init__()
        self._tr = training

        self.setWindowTitle("Samsara Voice Training")
        self.resize(760, 680)
        self.setMinimumSize(620, 520)
        self.setStyleSheet(_SS)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint
        )

        central = QWidget()
        self.setCentralWidget(central)
        root_lay = QVBoxLayout(central)
        root_lay.setContentsMargins(12, 12, 12, 12)
        root_lay.setSpacing(10)

        self._tabs = QTabWidget()
        root_lay.addWidget(self._tabs, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        root_lay.addLayout(close_row)

        self._build_calibration_tab()
        self._build_vocabulary_tab()
        self._build_corrections_tab()
        self._build_advanced_tab()

        self._level_sig.connect(self._on_level)
        self._phrase_sig.connect(self._on_phrase_result)
        self._phrase_detail_sig.connect(self._on_phrase_detail)

    # ----------------------------------------------------------------
    # Calibration
    # ----------------------------------------------------------------

    def _build_calibration_tab(self):
        tab = QWidget()
        self._tabs.addTab(tab, "Calibration")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        # Mic level monitor
        mon_frame, mon_body = self._section("Microphone Level Monitor")
        lay.addWidget(mon_frame)
        ml = QVBoxLayout(mon_body)
        ml.setContentsMargins(12, 8, 12, 12)
        ml.setSpacing(6)

        self._level_bar = QProgressBar()
        self._level_bar.setRange(0, 100)
        self._level_bar.setTextVisible(False)
        self._level_bar.setFixedHeight(28)
        ml.addWidget(self._level_bar)

        self._level_label = QLabel("Volume: 0%")
        self._level_label.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        ml.addWidget(self._level_label)

        btn_row = QHBoxLayout()
        self._monitor_btn = QPushButton("Start Monitoring")
        self._monitor_btn.setFixedWidth(150)
        self._monitor_btn.clicked.connect(self._toggle_monitoring)
        btn_row.addWidget(self._monitor_btn)
        btn_row.addStretch()
        ml.addLayout(btn_row)

        hint = QLabel(
            "Speak at your normal volume.  "
            "Green = good level,  Orange = too loud,  Red = too quiet."
        )
        hint.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        hint.setWordWrap(True)
        ml.addWidget(hint)

        # Test phrases
        phrase_frame, phrase_body = self._section("Recognition Test Phrases")
        lay.addWidget(phrase_frame, stretch=1)
        pl = QVBoxLayout(phrase_body)
        pl.setContentsMargins(12, 8, 12, 12)
        pl.setSpacing(4)

        sub = QLabel("Speak each phrase when prompted (5-second recording per phrase):")
        sub.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        pl.addWidget(sub)

        self._phrase_results: List[QLabel] = []
        _test_phrases = [
            "the quick brown fox jumps over the lazy dog",
            "pack my box with five dozen liquor jugs",
            "sphinx of black quartz judge my vow",
            "how vexingly quick daft zebras jump",
            "the five boxing wizards jump quickly",
        ]
        for i, phrase in enumerate(_test_phrases):
            row = QHBoxLayout()
            lbl = QLabel(f"#{i+1}: {phrase}")
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            lbl.setWordWrap(True)
            row.addWidget(lbl, stretch=1)

            status = QLabel("--")
            status.setFixedWidth(28)
            status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status.setStyleSheet(f"color:{_TEXT_SEC};font-weight:bold;")
            self._phrase_results.append(status)
            row.addWidget(status)

            btn = QPushButton("Test")
            btn.setFixedWidth(64)
            btn.clicked.connect(
                lambda _c=False, p=phrase, idx=i: self._test_phrase(p, idx)
            )
            row.addWidget(btn)
            pl.addLayout(row)

        pl.addStretch()

    def _toggle_monitoring(self):
        if self._tr._monitoring:
            self._stop_monitoring()
        else:
            self._start_monitoring()

    def _start_monitoring(self):
        self._tr._monitoring = True
        self._monitor_btn.setText("Stop Monitoring")

        def _run():
            stream = None
            try:
                stream = sd.InputStream(
                    samplerate=16000, channels=1, dtype=np.float32,
                    device=self._tr.app.config.get('microphone'),
                    blocksize=1024,
                )
                stream.start()
                while self._tr._monitoring:
                    try:
                        data, _ = stream.read(1024)
                        rms   = float(np.sqrt(np.mean(data ** 2)))
                        db    = 20.0 * np.log10(rms + 1e-10)
                        level = max(0.0, min(100.0, (db + 60.0) * 2.0))
                        self._level_sig.emit(level)
                        time.sleep(0.05)
                    except Exception as exc:
                        logger.debug(f"Monitoring loop: {exc}")
                        break
            except Exception as exc:
                logger.error(f"Monitoring stream error: {exc}", exc_info=True)
            finally:
                if stream:
                    stream.stop()
                    stream.close()

        thread_registry.spawn("vt-monitor", _run, daemon=True)

    def _stop_monitoring(self):
        self._tr._monitoring = False
        self._monitor_btn.setText("Start Monitoring")
        self._level_bar.setValue(0)
        self._level_label.setText("Volume: 0%")

    def _on_level(self, level: float):
        self._level_bar.setValue(int(level))
        if level < 30:
            chunk_color = _ERROR
        elif level < 70:
            chunk_color = _SUCCESS
        else:
            chunk_color = _WARNING
        self._level_bar.setStyleSheet(
            f"QProgressBar{{background:{_SURFACE};border:1px solid {_BORDER};border-radius:4px;}}"
            f"QProgressBar::chunk{{background:{chunk_color};border-radius:3px;}}"
        )
        self._level_label.setText(f"Volume: {int(level)}%")

    def _test_phrase(self, phrase: str, idx: int):
        self._phrase_results[idx].setText("...")
        self._phrase_results[idx].setStyleSheet(f"color:{_ACCENT};font-weight:bold;")

        def _run():
            try:
                # Recording cue — the 5s window starts now. Signal only;
                # never mutate widgets directly from this worker thread.
                self._phrase_sig.emit(idx, "REC", _WARNING)
                audio = sd.rec(
                    int(5 * 16000), samplerate=16000, channels=1, dtype=np.float32,
                    device=self._tr.app.config.get('microphone'),
                )
                sd.wait()
                self._phrase_sig.emit(idx, "...", _ACCENT)
                audio = audio.flatten()

                # Measure the SAME pipeline dictation uses, not a hardcoded
                # stand-in — only vad_filter is forced off, matching the
                # hotkey path's rationale (a deliberate, bounded recording,
                # not a stream that needs silence-trimming).
                params = self._tr.app.get_transcription_params()
                params['vad_filter'] = False
                lock = getattr(self._tr.app, 'model_lock', None) or threading.Lock()
                with lock:
                    segments, _ = self._tr.app.model.transcribe(audio, **params)

                raw_result    = "".join(s.text for s in segments).strip()
                norm_result   = _normalize_phrase(raw_result)
                norm_expected = _normalize_phrase(phrase)
                if norm_result == norm_expected:
                    self._phrase_sig.emit(idx, "OK", _SUCCESS)
                else:
                    self._phrase_sig.emit(idx, "X", _ERROR)
                    similarity = _word_similarity(norm_expected, norm_result)
                    self._phrase_detail_sig.emit(
                        f"Expected:\n{phrase}\n\nGot:\n{raw_result}\n\nAccuracy: {similarity:.1f}%"
                    )
            except Exception as exc:
                logger.error(f"Test phrase error: {exc}", exc_info=True)
                self._phrase_sig.emit(idx, "!", _WARNING)

        thread_registry.spawn("vt-test", _run, daemon=True)

    def _on_phrase_result(self, idx: int, text: str, color: str):
        lbl = self._phrase_results[idx]
        lbl.setText(text)
        lbl.setStyleSheet(f"color:{color};font-weight:bold;")

    def _on_phrase_detail(self, message: str):
        QMessageBox.information(self, "Test Result", message)

    # ----------------------------------------------------------------
    # Vocabulary
    # ----------------------------------------------------------------

    def _build_vocabulary_tab(self):
        tab = QWidget()
        self._tabs.addTab(tab, "Vocabulary")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        title = QLabel("Custom Vocabulary")
        title.setStyleSheet(f"color:{_TEXT_PRI};font-size:15px;font-weight:bold;")
        lay.addWidget(title)

        desc = QLabel(
            "Add words or phrases that Whisper often misrecognises (technical terms, names, jargon).\n"
            "These are injected into Whisper's initial_prompt to bias transcription toward them."
        )
        desc.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        add_row = QHBoxLayout()
        self._vocab_input = QLineEdit()
        self._vocab_input.setPlaceholderText("Word or phrase to add...")
        self._vocab_input.returnPressed.connect(self._add_vocab)
        add_row.addWidget(self._vocab_input, stretch=1)
        add_btn = QPushButton("Add")
        add_btn.setFixedWidth(70)
        add_btn.clicked.connect(self._add_vocab)
        add_row.addWidget(add_btn)
        lay.addLayout(add_row)

        self._vocab_list = QListWidget()
        for word in self._tr.custom_vocab:
            self._vocab_list.addItem(word)
        lay.addWidget(self._vocab_list, stretch=1)

        btn_row = QHBoxLayout()
        rem_btn = QPushButton("Remove Selected")
        rem_btn.clicked.connect(self._remove_vocab)
        btn_row.addWidget(rem_btn)
        clr_btn = QPushButton("Clear All")
        clr_btn.setObjectName("danger")
        clr_btn.clicked.connect(self._clear_vocab)
        btn_row.addWidget(clr_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

    def _add_vocab(self):
        word = self._vocab_input.text().strip()
        if word and word not in self._tr.custom_vocab:
            self._tr.custom_vocab.append(word)
            self._vocab_list.addItem(word)
            self._vocab_input.clear()
            self._tr.save_training_data()

    def _remove_vocab(self):
        row = self._vocab_list.currentRow()
        if row >= 0:
            word = self._vocab_list.item(row).text()
            self._vocab_list.takeItem(row)
            if word in self._tr.custom_vocab:
                self._tr.custom_vocab.remove(word)
            self._tr.save_training_data()

    def _clear_vocab(self):
        reply = QMessageBox.question(
            self, "Confirm", "Remove all custom vocabulary?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._tr.custom_vocab = []
            self._vocab_list.clear()
            self._tr.save_training_data()

    # ----------------------------------------------------------------
    # Corrections
    # ----------------------------------------------------------------

    def _build_corrections_tab(self):
        tab = QWidget()
        self._tabs.addTab(tab, "Corrections")
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        title = QLabel("Corrections Dictionary")
        title.setStyleSheet(f"color:{_TEXT_PRI};font-size:15px;font-weight:bold;")
        lay.addWidget(title)

        desc = QLabel(
            "Map Whisper transcription errors to your intended text.\n"
            "Applied automatically as a post-processing step after every transcription."
        )
        desc.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        inp_row = QHBoxLayout()
        self._wrong_input = QLineEdit()
        self._wrong_input.setPlaceholderText("Whisper says...")
        inp_row.addWidget(self._wrong_input, stretch=1)
        arrow = QLabel("->")
        arrow.setStyleSheet(f"color:{_TEXT_SEC};padding:0 6px;")
        inp_row.addWidget(arrow)
        self._correct_input = QLineEdit()
        self._correct_input.setPlaceholderText("You meant...")
        self._correct_input.returnPressed.connect(self._add_correction)
        inp_row.addWidget(self._correct_input, stretch=1)
        add_btn = QPushButton("Add")
        add_btn.setFixedWidth(70)
        add_btn.clicked.connect(self._add_correction)
        inp_row.addWidget(add_btn)
        lay.addLayout(inp_row)

        self._corr_table = QTableWidget(0, 2)
        self._corr_table.setHorizontalHeaderLabels(["Whisper Says", "Correct Text"])
        self._corr_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._corr_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._corr_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._corr_table.verticalHeader().setVisible(False)
        lay.addWidget(self._corr_table, stretch=1)

        for wrong, correct in self._tr.corrections_dict.items():
            self._insert_correction_row(wrong, correct)

        btn_row = QHBoxLayout()
        rem_btn = QPushButton("Remove Selected")
        rem_btn.clicked.connect(self._remove_correction)
        btn_row.addWidget(rem_btn)
        clr_btn = QPushButton("Clear All")
        clr_btn.setObjectName("danger")
        clr_btn.clicked.connect(self._clear_corrections)
        btn_row.addWidget(clr_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

    def _insert_correction_row(self, wrong: str, correct: str):
        row = self._corr_table.rowCount()
        self._corr_table.insertRow(row)
        self._corr_table.setItem(row, 0, QTableWidgetItem(wrong))
        self._corr_table.setItem(row, 1, QTableWidgetItem(correct))

    def _add_correction(self):
        wrong   = self._wrong_input.text().strip()
        correct = self._correct_input.text().strip()
        if wrong and correct:
            self._tr.corrections_dict[wrong] = correct
            self._tr._rebuild_corrections_pattern()
            self._insert_correction_row(wrong, correct)
            self._wrong_input.clear()
            self._correct_input.clear()
            self._tr.save_training_data()

    def _remove_correction(self):
        rows = self._corr_table.selectionModel().selectedRows()
        for idx in sorted(rows, key=lambda i: i.row(), reverse=True):
            wrong = self._corr_table.item(idx.row(), 0).text()
            self._corr_table.removeRow(idx.row())
            self._tr.corrections_dict.pop(wrong, None)
        if rows:
            self._tr._rebuild_corrections_pattern()
            self._tr.save_training_data()

    def _clear_corrections(self):
        reply = QMessageBox.question(
            self, "Confirm", "Remove all correction rules?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._tr.corrections_dict = {}
            self._tr._rebuild_corrections_pattern()
            self._corr_table.setRowCount(0)
            self._tr.save_training_data()

    # ----------------------------------------------------------------
    # Advanced
    # ----------------------------------------------------------------

    def _build_advanced_tab(self):
        tab = QWidget()
        self._tabs.addTab(tab, "Advanced")
        tab_lay = QVBoxLayout(tab)
        tab_lay.setContentsMargins(0, 0, 0, 0)
        tab_lay.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;}")
        tab_lay.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        lay = QVBoxLayout(content)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        # Model info
        model_frame, model_body = self._section("Model Selection")
        lay.addWidget(model_frame)
        ml = QVBoxLayout(model_body)
        ml.setContentsMargins(12, 8, 12, 12)
        ml.setSpacing(4)
        current_model = self._tr.app.config.get('model_size', 'base')
        ml.addWidget(QLabel(f"Current model: <b>{current_model}</b>"))
        info = QLabel(
            "tiny: Fastest  |  base: Recommended  |  small: Better accuracy\n"
            "medium: Very good  |  large-v3: Best quality (requires GPU)\n"
            "Change in Settings -> General.  Takes effect on restart."
        )
        info.setStyleSheet(f"color:{_TEXT_SEC};font-size:11px;")
        info.setWordWrap(True)
        ml.addWidget(info)

        # Language
        lang_frame, lang_body = self._section("Language")
        lay.addWidget(lang_frame)
        ll = QHBoxLayout(lang_body)
        ll.setContentsMargins(12, 12, 12, 12)
        ll.setSpacing(8)
        ll.addWidget(QLabel("Transcription language:"))
        self._lang_combo = QComboBox()
        lang_codes = ['en', 'es', 'fr', 'de', 'it', 'pt', 'nl', 'pl', 'ru', 'zh', 'ja', 'ko']
        self._lang_combo.addItems(lang_codes)
        current_lang = self._tr.app.config.get('language', 'en')
        if current_lang in lang_codes:
            self._lang_combo.setCurrentText(current_lang)
        self._lang_combo.setFixedWidth(100)
        ll.addWidget(self._lang_combo)
        apply_btn = QPushButton("Apply")
        apply_btn.setFixedWidth(80)
        apply_btn.clicked.connect(self._apply_language)
        ll.addWidget(apply_btn)
        ll.addStretch()

        # Initial prompt
        prompt_frame, prompt_body = self._section("Initial Prompt")
        lay.addWidget(prompt_frame)
        pl = QVBoxLayout(prompt_body)
        pl.setContentsMargins(12, 8, 12, 12)
        pl.setSpacing(6)
        pl.addWidget(QLabel(
            "Custom context passed to Whisper (combined with vocabulary and command phrases):"
        ))
        self._prompt_edit = QTextEdit()
        self._prompt_edit.setFixedHeight(80)
        self._prompt_edit.setPlaceholderText(
            "E.g. 'Technical discussion about Python, React, and machine learning.'"
        )
        self._prompt_edit.setPlainText(self._tr.app.config.get('initial_prompt', ''))
        pl.addWidget(self._prompt_edit)
        save_btn = QPushButton("Save Prompt")
        save_btn.setFixedWidth(120)
        save_btn.clicked.connect(self._save_prompt)
        pl.addWidget(save_btn)

        # Backup and restore
        backup_frame, backup_body = self._section("Backup and Restore")
        lay.addWidget(backup_frame)
        bl = QHBoxLayout(backup_body)
        bl.setContentsMargins(12, 12, 12, 12)
        bl.setSpacing(8)
        exp_btn = QPushButton("Export Training Data")
        exp_btn.clicked.connect(self._export_data)
        bl.addWidget(exp_btn)
        imp_btn = QPushButton("Import Training Data")
        imp_btn.clicked.connect(self._import_data)
        bl.addWidget(imp_btn)
        bl.addStretch()

        lay.addStretch()

    def _apply_language(self):
        lang = self._lang_combo.currentText()
        try:
            self._tr.app.update_config_and_save({'language': lang})
            QMessageBox.information(
                self, "Language Changed",
                f"Language set to: {lang}\n\nChange takes effect immediately."
            )
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to change language:\n{exc}")

    def _save_prompt(self):
        prompt = self._prompt_edit.toPlainText().strip()
        try:
            self._tr.app.update_config_and_save({'initial_prompt': prompt})
            QMessageBox.information(self, "Saved", "Initial prompt saved successfully.")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to save prompt:\n{exc}")

    def _export_data(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Training Data", "voice_training_backup.json",
            "JSON files (*.json);;All files (*.*)"
        )
        if not filename:
            return
        try:
            data = {
                'vocabulary':     self._tr.custom_vocab,
                'corrections':    self._tr.corrections_dict,
                'initial_prompt': self._tr.app.config.get('initial_prompt', ''),
                'language':       self._tr.app.config.get('language', 'en'),
            }
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            QMessageBox.information(self, "Export Complete",
                                    f"Training data exported to:\n{filename}")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to export:\n{exc}")

    def _import_data(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Import Training Data", "",
            "JSON files (*.json);;All files (*.*)"
        )
        if not filename:
            return
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if 'vocabulary' in data:
                self._tr.custom_vocab = data['vocabulary']
                self._vocab_list.clear()
                for w in self._tr.custom_vocab:
                    self._vocab_list.addItem(w)
            if 'corrections' in data:
                self._tr.corrections_dict = data['corrections']
                self._tr._rebuild_corrections_pattern()
                self._corr_table.setRowCount(0)
                for wrong, correct in self._tr.corrections_dict.items():
                    self._insert_correction_row(wrong, correct)
            config_updates = {}
            if 'initial_prompt' in data:
                config_updates['initial_prompt'] = data['initial_prompt']
                self._prompt_edit.setPlainText(data['initial_prompt'])
            if 'language' in data:
                config_updates['language'] = data['language']
                lang_items = [self._lang_combo.itemText(i)
                              for i in range(self._lang_combo.count())]
                if data['language'] in lang_items:
                    self._lang_combo.setCurrentText(data['language'])

            self._tr.save_training_data()

            persisted = True
            if config_updates:
                # Same real persistence method _apply_language/_save_prompt use --
                # not persist_config(), which only flushes already-applied
                # in-memory changes and would silently no-op these updates.
                try:
                    self._tr.app.update_config_and_save(config_updates)
                except Exception as e:
                    persisted = False
                    logger.warning(f"_import_data: failed to persist config updates: {e}")

            if persisted:
                QMessageBox.information(self, "Import Complete",
                                        "Training data imported successfully.")
            else:
                QMessageBox.warning(
                    self, "Import Partially Complete",
                    "Training data was imported, but the language/prompt "
                    "settings could not be saved to disk. They may be lost "
                    "on restart."
                )
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to import:\n{exc}")

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    @staticmethod
    def _section(title: str) -> "Tuple[QFrame, QWidget]":
        """Return (outer_frame, body_widget).  Caller sets a layout on body."""
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{_SURFACE};border:1px solid {_BORDER};border-radius:6px;}}"
        )
        outer_lay = QVBoxLayout(frame)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        hdr = QLabel(f"  {title}")
        hdr.setFixedHeight(32)
        hdr.setStyleSheet(
            f"color:{_TEXT_PRI};font-size:13px;font-weight:bold;"
            f"background:{_ELEVATED};border-radius:6px 6px 0 0;"
            f"border-bottom:1px solid {_BORDER};"
        )
        outer_lay.addWidget(hdr)

        body = QWidget()
        body.setStyleSheet("background:transparent;border:none;")
        outer_lay.addWidget(body)

        return frame, body

    def closeEvent(self, e):
        self._stop_monitoring()
        e.accept()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _normalize_phrase(s: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace for comparison.

    Whisper output like "The quick brown fox jumps over the lazy dog." must
    count as an exact match against the plain test phrase.
    """
    s = re.sub(r"[^\w\s']", '', s.lower())
    return " ".join(s.split())


def _word_similarity(s1: str, s2: str) -> float:
    w1, w2 = set(s1.split()), set(s2.split())
    if not w1 and not w2:
        return 100.0
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2) * 100.0
