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
import shutil
import sys
import threading
import time
import unicodedata
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

from samsara.audio_devices import detect_capture_rate
from samsara.audio_engine import guide_capture
from samsara.ui import qt_runtime, theme
from samsara.runtime import thread_registry
from samsara.languages import LANGUAGES, is_boundaryless_script_char
from samsara.paths import quarantine_corrupt_file

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

# Colour comes from samsara.ui.theme, never from a literal here: this
# module used to keep its own copy of the dark palette, which is exactly
# how a second palette leaves one window unreadable (queue 129).

def _ss() -> str:
    """The window's stylesheet, built on demand. Never a module
    constant: an f-string evaluated at import time freezes the
    palette that happened to be live then (queue 129)."""
    return f"""
    QMainWindow, QDialog, QWidget {{
        background: {theme.BG0};
        color: {theme.TEXT_PRIMARY};
        font-family: 'Segoe UI', sans-serif;
        font-size: {theme.TYPE_BODY}px;
    }}
    QTabWidget::pane {{
        border: 1px solid {theme.BORDER};
        background: {theme.BG0};
    }}
    QTabBar::tab {{
        background: {theme.BG1};
        color: {theme.TEXT_SECONDARY};
        padding: 7px 18px;
        border: none;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background: {theme.BG2};
        color: {theme.ACCENT};
        border-bottom: 2px solid {theme.ACCENT};
    }}
    QTabBar::tab:hover:!selected {{ color: {theme.TEXT_PRIMARY}; }}
    QLineEdit, QTextEdit, QPlainTextEdit {{
        background: {theme.BG1};
        border: 1px solid {theme.BORDER};
        color: {theme.TEXT_PRIMARY};
        padding: 4px 8px;
        border-radius: 4px;
    }}
    QLineEdit:focus, QTextEdit:focus {{ border-color: {theme.ACCENT}; }}
    QPushButton {{
        background: {theme.BG2};
        color: {theme.TEXT_PRIMARY};
        border: 1px solid {theme.BORDER};
        padding: 5px 14px;
        border-radius: 4px;
    }}
    QPushButton:hover {{
        background: {theme.ACCENT};
        color: {theme.BG0};
        border-color: {theme.ACCENT};
    }}
    QPushButton:pressed {{ background: {theme.ACCENT_HOVER}; }}
    QPushButton#danger {{
        color: {theme.ERROR};
        border-color: {theme.ERROR};
    }}
    QPushButton#danger:hover {{
        background: {theme.ERROR};
        color: {theme.BG0};
    }}
    QListWidget {{
        background: {theme.BG1};
        border: 1px solid {theme.BORDER};
        color: {theme.TEXT_PRIMARY};
        outline: none;
    }}
    QListWidget::item {{ padding: 3px 8px; }}
    QListWidget::item:selected {{ background: {theme.ACCENT}; color: {theme.BG0}; }}
    QTableWidget {{
        background: {theme.BG1};
        border: 1px solid {theme.BORDER};
        gridline-color: {theme.BORDER};
        color: {theme.TEXT_PRIMARY};
        outline: none;
    }}
    QTableWidget::item:selected {{ background: {theme.ACCENT}; color: {theme.BG0}; }}
    QHeaderView::section {{
        background: {theme.BG2};
        color: {theme.TEXT_SECONDARY};
        border: none;
        border-right: 1px solid {theme.BORDER};
        padding: 4px 8px;
        font-size: {theme.TYPE_MIN}px;
        font-weight: bold;
    }}
    QComboBox {{
        background: {theme.BG1};
        border: 1px solid {theme.BORDER};
        color: {theme.TEXT_PRIMARY};
        padding: 4px 8px;
        border-radius: 4px;
    }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{
        background: {theme.BG1};
        color: {theme.TEXT_PRIMARY};
        selection-background-color: {theme.ACCENT};
        selection-color: {theme.BG0};
        border: 1px solid {theme.BORDER};
    }}
    QScrollBar:vertical {{
        background: {theme.BG0};
        width: 6px;
        border: none;
    }}
    QScrollBar::handle:vertical {{
        background: {theme.BORDER};
        border-radius: 3px;
        min-height: 20px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QProgressBar {{
        background: {theme.BG1};
        border: 1px solid {theme.BORDER};
        border-radius: 4px;
        text-align: center;
    }}
    QProgressBar::chunk {{ background: {theme.SUCCESS}; border-radius: 3px; }}
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
        # Set by load_training_data() on a parse failure (2026-07-16
        # correction-store hardening) -- save_training_data() logs a
        # one-time WARNING on the next successful write so a previous
        # quarantine doesn't pass silently.
        self._load_failed = False
        self._quarantine_path = None
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
                # Quarantine (rename aside) rather than fall through to
                # empty in-memory state that a later save could silently
                # write back over the original file -- the exact
                # 2026-07-09 correction-store loss pattern.
                self._quarantine_path = quarantine_corrupt_file(training_file, logger, exc)
                self._load_failed = True
        else:
            # The shipped Default dictionary is a seed for a new profile, not
            # a profile silently selected over the user's data.  Persisting it
            # once is important: after a user removes one of its corrections,
            # the now-present (even if empty) training_data.json is the record
            # of that choice and a later launch must not put it back.
            default_path = self._bundled_default_dictionary_path()
            try:
                with open(default_path, 'r', encoding='utf-8') as f:
                    default_data = json.load(f)
                # A profile's vocabulary is deliberate decoder context, so
                # applying it requires an explicit profile load.  Only the
                # conservative correction map is safe as a first-run seed.
                self.custom_vocab = []
                self.corrections_dict = dict(default_data.get('corrections', {}))
                if not self.save_training_data():
                    logger.warning("[STORE] Could not persist bundled default dictionary")
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("[STORE] Could not load bundled default dictionary: %s", exc)
        self._rebuild_corrections_pattern()

    @staticmethod
    def _bundled_default_dictionary_path() -> Path:
        """Return the packaged Default dictionary for first-profile seeding."""
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
        return root / 'profiles' / 'dictionaries' / 'Default.json'

    # ----------------------------------------------------------------
    # Single-pair add/remove (2026-07-11) -- canonical entry points for
    # samsara/teach_patterns.py's voice-teaching dispatch
    # (plugins/commands/ask_ollama.py). Every existing UI call site
    # (dictionary_panel_qt.py's _vocab_add, correction_capture_qt.py's
    # _on_always_fix) mutates custom_vocab/corrections_dict + calls
    # _rebuild_corrections_pattern()/save_training_data() inline rather
    # than through a shared method -- these are new, not a refactor of
    # those existing (working, low-risk-to-leave-alone) call sites.
    # Case-insensitive membership/lookup (voice-transcribed casing won't
    # reliably match whatever casing a word was originally added with),
    # but the STORED casing is left exactly as passed -- vocabulary
    # entries are often proper nouns where casing matters for the prompt.
    # ----------------------------------------------------------------

    def add_vocab_word(self, word: str) -> bool:
        """Returns False (no-op) if already present case-insensitively, or
        on save failure."""
        word = word.strip()
        if not word or any(w.lower() == word.lower() for w in self.custom_vocab):
            return False
        self.custom_vocab.append(word)
        return self.save_training_data()

    def remove_vocab_word(self, word: str) -> bool:
        word = word.strip()
        for existing in list(self.custom_vocab):
            if existing.lower() == word.lower():
                self.custom_vocab.remove(existing)
                return self.save_training_data()
        return False

    def add_correction(self, wrong: str, right: str) -> bool:
        """Exact same mutation sequence as correction_capture_qt.py's
        _on_always_fix: corrections_dict[wrong] = right, then
        _rebuild_corrections_pattern() (so apply_corrections() sees it on
        the very next call -- see that method's own docstring for why this
        step is required), then save_training_data(). An existing entry
        for `wrong` is overwritten, not confirm-gated -- this path is only
        reached after the caller's own atomic-substitution validation
        (samsara/teach_patterns.validate_correction_pair), and the
        "instant, permanent, name it + offer undo" confirmation happens at
        the dispatch layer, not here."""
        wrong, right = wrong.strip(), right.strip()
        if not wrong or not right:
            return False
        self.corrections_dict[wrong] = right
        self._rebuild_corrections_pattern()
        return self.save_training_data()

    def remove_correction(self, wrong: str) -> bool:
        wrong = wrong.strip()
        for existing in list(self.corrections_dict):
            if existing.lower() == wrong.lower():
                del self.corrections_dict[existing]
                self._rebuild_corrections_pattern()
                return self.save_training_data()
        return False

    def save_training_data(self) -> bool:
        training_file = Path(self.app.config_path).parent / 'training_data.json'
        if training_file.exists():
            try:
                shutil.copy2(training_file, training_file.with_name(training_file.name + '.bak'))
            except OSError as exc:
                logger.debug(f"[STORE] backup copy failed (non-fatal): {exc}")
        try:
            data = {'vocabulary': self.custom_vocab, 'corrections': self.corrections_dict}
            with open(training_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.error(f"Could not save training data: {exc}", exc_info=True)
            return False

        if self._load_failed:
            logger.warning(
                f"[STORE] training_data.json: writing after a previous load "
                f"failure -- previous file quarantined to {self._quarantine_path}"
            )
            self._load_failed = False
        else:
            logger.info(
                f"[STORE] training_data.json saved: {len(self.custom_vocab)} "
                f"vocab, {len(self.corrections_dict)} corrections"
            )
        return True

    # Whisper's initial_prompt budget is ~224 tokens; 800 chars is a safe
    # character-based proxy that keeps well clear of that limit.
    _PROMPT_CHAR_BUDGET = 800

    def get_initial_prompt(self, include_vocabulary: bool = True) -> "str | None":
        """Build the Whisper initial_prompt from up to three layered parts.

        include_vocabulary=False omits Priority 2 (genuine user vocabulary
        -- names, jargon, technical terms added via add_vocab_word, the
        "Common terms:" layer) AND Priority 3 (the auto-derived command
        vocabulary) entirely, returning Priority 1 (explicit custom prompt)
        alone or None. This is for decode paths that are pure free-form
        prose and never matched against the command registry -- the hotkey
        hold-to-record path (non-command press), the toggle-session DICTATE/
        AVA lanes, transcribe_continuous_buffer, and the wake-word dictation
        lane; see dictation.py's per-path wiring.

        2026-07-16 (commit 02e00b9): first found that Priority 3 alone (the
        ~280-phrase, comma-separated auto-derived command list) measurably
        destabilized long (>~15-20s) continuous-speech decodes -- an
        unusual, non-conversational decoder context -- and started gating
        it off the hotkey path via what was then called include_commands.
        2026-07-17/18 (SPARK decode matrix, N=10/cell against both incident
        WAVs): the SAME destabilization is caused by ANY non-conversational
        vocabulary content in initial_prompt, not command phrases
        specifically -- Priority 2's short 9-term "Common terms:" list
        reproduced the identical truncation signature on its own. This
        parameter was widened accordingly (and renamed to describe what it
        actually gates now) to omit Priority 2 as well for every free-form
        path, not just Priority 3. See dictation.py's module comment above
        _SANITY_MIN_DURATION_S for the full incident history.

        Command-lane decodes (matcher-side recognition, 1-3s utterances)
        are unaffected either way -- see dictation.py's per-path table.
        """
        try:
            parts: List[str] = []
            remaining = self._PROMPT_CHAR_BUDGET

            # Priority 1: custom prompt -- explicit user input, never
            # truncated or dropped for size, and never gated by
            # include_vocabulary: this is the user's own explicit override,
            # not auto-derived vocabulary, so it carries whatever residual
            # destabilization risk the user has knowingly opted into.
            custom_prompt = self.app.config.get('initial_prompt', '')
            if custom_prompt:
                parts.append(custom_prompt)
                remaining -= len(custom_prompt)

            # Priority 2: custom vocabulary -- included whole or not at all
            # (never truncate mid-phrase).
            if include_vocabulary and self.custom_vocab and remaining > 0:
                vocab_part = f"Common terms: {', '.join(self.custom_vocab)}"
                needed = len(vocab_part) + (1 if parts else 0)
                if needed <= remaining:
                    parts.append(vocab_part)
                    remaining -= needed

            # Priority 3: command vocabulary -- lowest priority, so it's the
            # one truncated (item-by-item, never mid-word) to fit what's left.
            if include_vocabulary and remaining > 0:
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
            # NFC-normalize before lowering so visually-identical keys
            # entered in different Unicode forms (e.g. a precomposed "é"
            # vs "e" + combining acute accent) land on the same dict entry
            # instead of silently missing each other.
            lookup[unicodedata.normalize('NFC', key).lower()] = self.corrections_dict[key]
            # Each end's \b anchor is decided independently by the
            # character AT that end. CJK/Thai/etc have no whitespace word
            # boundaries -- a \b anchor between two \w characters with no
            # separator (the normal case for these scripts) never matches,
            # so a key embedded mid-sentence would silently never fire on
            # that end. A pure-Latin key keeps both anchors (unchanged), a
            # pure-CJK key drops both (unchanged) -- but a MIXED-script key
            # like "foo字" keeps the anchor only on its Latin end, so it
            # still can't over-match inside "foobar" the way an all-or-
            # nothing decision would.
            prefix = (
                r'\b' if re.match(r'\w', key[0]) and not is_boundaryless_script_char(key[0])
                else ''
            )
            suffix = (
                r'\b' if re.match(r'\w', key[-1]) and not is_boundaryless_script_char(key[-1])
                else ''
            )
            parts.append(prefix + re.escape(key) + suffix)

        self._corrections_lookup = lookup
        self._corrections_pattern = re.compile('|'.join(parts), re.IGNORECASE)

    def apply_corrections(self, text: str) -> str:
        try:
            if self._corrections_pattern is None:
                return text

            def _replace(match: "re.Match") -> str:
                matched = match.group(0)
                # Same NFC-before-lower normalization as the lookup build
                # above, so both sides of the dict lookup agree regardless
                # of which Unicode form the matched text came in as.
                matched_key = unicodedata.normalize('NFC', matched).lower()
                replacement = self._corrections_lookup.get(matched_key, matched)
                # Case preservation only makes sense when the matched text
                # actually has cased characters -- CJK/Thai/etc have none,
                # so the replacement is used exactly as stored.
                if not any(ch.isupper() or ch.islower() for ch in matched):
                    return replacement
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
    _monitor_error_sig = Signal(str)             # level monitor could not read the mic
    _phrase_error_sig  = Signal(int, str)        # (idx, message) test recording failed

    def __init__(self, training: VoiceTrainingQt):
        super().__init__()
        self._tr = training

        self.setWindowTitle("Samsara Voice Training")
        self.resize(760, 680)
        self.setMinimumSize(620, 520)
        self.setStyleSheet(_ss())
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
        self._monitor_error_sig.connect(self._on_monitor_error)
        self._phrase_error_sig.connect(self._on_phrase_error)

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
        self._level_label.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        ml.addWidget(self._level_label)

        btn_row = QHBoxLayout()
        self._monitor_btn = QPushButton("Start Monitoring")
        # A real button: _section()'s body carries an unqualified
        # "background:transparent;border:none;" that cascades onto child
        # buttons and renders them as bare text. make_secondary's per-widget
        # stylesheet wins over that ancestor rule.
        theme.make_secondary(self._monitor_btn)
        self._monitor_btn.clicked.connect(self._toggle_monitoring)
        btn_row.addWidget(self._monitor_btn)
        btn_row.addStretch()
        ml.addLayout(btn_row)
        self._monitor_btn.setMinimumWidth(
            _button_min_width(self._monitor_btn, ["Start Monitoring", "Stop Monitoring"])
        )

        hint = QLabel(
            "Speak at your normal volume.  "
            "Green = good level,  Orange = too loud,  Red = too quiet."
        )
        hint.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        hint.setWordWrap(True)
        ml.addWidget(hint)

        # Test phrases
        phrase_frame, phrase_body = self._section("Recognition Test Phrases")
        lay.addWidget(phrase_frame, stretch=1)
        pl = QVBoxLayout(phrase_body)
        pl.setContentsMargins(12, 8, 12, 12)
        pl.setSpacing(4)

        sub = QLabel("Speak each phrase when prompted (5-second recording per phrase):")
        sub.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        pl.addWidget(sub)

        # Recording failures are shown here, never swallowed into a log line.
        self._phrase_status = QLabel("")
        self._phrase_status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        self._phrase_status.setWordWrap(True)
        pl.addWidget(self._phrase_status)

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
            status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-weight:bold;")
            self._phrase_results.append(status)
            row.addWidget(status)

            btn = QPushButton("Test")
            theme.make_secondary(btn)   # same cascade as the monitor button
            btn.clicked.connect(
                lambda _c=False, p=phrase, idx=i: self._test_phrase(p, idx)
            )
            row.addWidget(btn)
            btn.setMinimumWidth(_button_min_width(btn, ["Test"]))
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
        self._level_label.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        thread_registry.spawn("vt-monitor", self._monitor_worker, daemon=True)

    def _monitor_worker(self):
        """Emit the mic level until monitoring stops.

        While the AudioCaptureEngine runs it owns the device: read its ring
        (samsara.audio_engine.guide_capture). Only without ACE open a
        transient stream, at the device's own rate. Any failure is reported
        to the window (_monitor_error_sig), never left as a silent 0%.
        """
        app = self._tr.app
        engine = guide_capture.running_engine(app)
        if engine is not None:
            meter = None
            try:
                meter = guide_capture.RingLevelMeter(engine, "voice-training-monitor")
                while self._tr._monitoring:
                    self._level_sig.emit(_level_from_rms(meter.read_rms()))
                    time.sleep(0.05)
            except Exception as exc:
                logger.error(f"Monitoring (ACE ring) error: {exc}", exc_info=True)
                self._monitor_error_sig.emit(str(exc) or type(exc).__name__)
            finally:
                if meter is not None:
                    meter.close()
            return

        device = app.config.get('microphone')
        rate = detect_capture_rate(device)
        blocksize = max(256, int(rate * 0.05))
        stream = None
        try:
            stream = sd.InputStream(
                samplerate=rate, channels=1, dtype=np.float32,
                device=device, blocksize=blocksize,
            )
            stream.start()
            while self._tr._monitoring:
                data, _ = stream.read(blocksize)
                self._level_sig.emit(_level_from_rms(guide_capture.block_rms(data.reshape(-1))))
        except Exception as exc:
            logger.error(f"Monitoring stream error: {exc}", exc_info=True)
            self._monitor_error_sig.emit(str(exc) or type(exc).__name__)
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception as exc:
                    logger.debug(f"Monitoring stream close: {exc}")

    def _on_monitor_error(self, message: str):
        self._tr._monitoring = False
        self._monitor_btn.setText("Start Monitoring")
        self._level_bar.setValue(0)
        self._level_label.setText(f"Microphone error: {message}")
        self._level_label.setStyleSheet(f"color:{theme.ERROR};font-size:{theme.TYPE_MIN}px;font-weight:bold;")

    def _stop_monitoring(self):
        self._tr._monitoring = False
        self._monitor_btn.setText("Start Monitoring")
        self._level_bar.setValue(0)
        self._level_label.setText("Volume: 0%")
        self._level_label.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")

    def _on_level(self, level: float):
        self._level_bar.setValue(int(level))
        if level < 30:
            chunk_color = theme.ERROR
        elif level < 70:
            chunk_color = theme.SUCCESS
        else:
            chunk_color = theme.WARNING
        self._level_bar.setStyleSheet(
            f"QProgressBar{{background:{theme.BG1};border:1px solid {theme.BORDER};border-radius:4px;}}"
            f"QProgressBar::chunk{{background:{chunk_color};border-radius:3px;}}"
        )
        self._level_label.setText(f"Volume: {int(level)}%")

    def _test_phrase(self, phrase: str, idx: int):
        self._phrase_results[idx].setText("...")
        self._phrase_results[idx].setStyleSheet(f"color:{theme.ACCENT};font-weight:bold;")

        self._phrase_status.setText("")

        def _run():
            try:
                # Recording cue — the 5s window starts now. Signal only;
                # never mutate widgets directly from this worker thread.
                self._phrase_sig.emit(idx, "REC", theme.WARNING)
                try:
                    audio = self._record_test_audio(5.0)
                except Exception as exc:
                    logger.error(f"Test phrase recording error: {exc}", exc_info=True)
                    self._phrase_error_sig.emit(idx, str(exc) or type(exc).__name__)
                    return
                if audio.size == 0:
                    self._phrase_error_sig.emit(idx, "no audio was recorded")
                    return
                self._phrase_sig.emit(idx, "...", theme.ACCENT)

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
                    self._phrase_sig.emit(idx, "OK", theme.SUCCESS)
                else:
                    self._phrase_sig.emit(idx, "X", theme.ERROR)
                    similarity = _word_similarity(norm_expected, norm_result)
                    self._phrase_detail_sig.emit(
                        f"Expected:\n{phrase}\n\nGot:\n{raw_result}\n\nAccuracy: {similarity:.1f}%"
                    )
            except Exception as exc:
                logger.error(f"Test phrase error: {exc}", exc_info=True)
                self._phrase_sig.emit(idx, "!", theme.WARNING)

        thread_registry.spawn("vt-test", _run, daemon=True)

    def _record_test_audio(self, seconds: float) -> "np.ndarray":
        """``seconds`` of 16 kHz float32 audio for a test phrase: from the
        ACE ring while the engine owns the mic, else a transient recording at
        the device's own rate resampled to 16 kHz. Raises on failure."""
        app = self._tr.app
        engine = guide_capture.running_engine(app)
        if engine is not None:
            return guide_capture.record_from_ring(engine, seconds, "voice-training-test")
        device = app.config.get('microphone')
        rate = detect_capture_rate(device)
        audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1,
                       dtype=np.float32, device=device)
        sd.wait()
        return guide_capture.to_model_rate(audio, rate)

    def _on_phrase_error(self, idx: int, message: str):
        self._on_phrase_result(idx, "!", theme.ERROR)
        self._phrase_status.setText(f"Recording failed: {message}")
        self._phrase_status.setStyleSheet(f"color:{theme.ERROR};font-size:{theme.TYPE_MIN}px;font-weight:bold;")

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
        title.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_EMPHASIS}px;font-weight:bold;")
        lay.addWidget(title)

        desc = QLabel(
            "Add words or phrases that Whisper often misrecognises (technical terms, names, jargon).\n"
            "These are injected into Whisper's initial_prompt to bias transcription toward them."
        )
        desc.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
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
        title.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_EMPHASIS}px;font-weight:bold;")
        lay.addWidget(title)

        desc = QLabel(
            "Map Whisper transcription errors to your intended text.\n"
            "Applied automatically as a post-processing step after every transcription."
        )
        desc.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        inp_row = QHBoxLayout()
        self._wrong_input = QLineEdit()
        self._wrong_input.setPlaceholderText("Whisper says...")
        inp_row.addWidget(self._wrong_input, stretch=1)
        arrow = QLabel("->")
        arrow.setStyleSheet(f"color:{theme.TEXT_SECONDARY};padding:0 6px;")
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
        info.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
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
        # Same source of truth as Settings -> General (samsara/languages.py)
        # -- one config key, one language list, so a value picked in either
        # place always displays correctly in the other.
        self._lang_name_to_code = {name: code for name, code in LANGUAGES}
        lang_code_to_name = {code: name for name, code in LANGUAGES}
        lang_names = [name for name, _ in LANGUAGES]
        self._lang_combo.addItems(lang_names)
        current_lang = self._tr.app.config.get('language', 'en')
        current_lang_display = lang_code_to_name.get(current_lang, 'English (en)')
        if current_lang_display in lang_names:
            self._lang_combo.setCurrentText(current_lang_display)
        self._lang_combo.setFixedWidth(220)
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
        display = self._lang_combo.currentText()
        lang = self._lang_name_to_code.get(display, 'en')
        try:
            self._tr.app.update_config_and_save({'language': lang})
            QMessageBox.information(
                self, "Language Changed",
                f"Language set to: {display}\n\nChange takes effect immediately."
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
                lang_code_to_name = {code: name for name, code in LANGUAGES}
                display = lang_code_to_name.get(data['language'])
                if display is not None:
                    self._lang_combo.setCurrentText(display)

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
            f"QFrame{{background:{theme.BG1};border:1px solid {theme.BORDER};border-radius:6px;}}"
        )
        outer_lay = QVBoxLayout(frame)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        hdr = QLabel(f"  {title}")
        hdr.setFixedHeight(32)
        hdr.setStyleSheet(
            f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;font-weight:bold;"
            f"background:{theme.BG2};border-radius:6px 6px 0 0;"
            f"border-bottom:1px solid {theme.BORDER};"
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

def _level_from_rms(rms: float) -> float:
    """Map an RMS level to the 0-100 meter scale (-60 dBFS .. -10 dBFS)."""
    db = 20.0 * np.log10(rms + 1e-10)
    return max(0.0, min(100.0, (db + 60.0) * 2.0))


def _button_min_width(btn: QPushButton, texts: list, h_padding: int = 48) -> int:
    """Widest label plus make_secondary's horizontal padding (24 px each side)."""
    metrics = btn.fontMetrics()
    return max(metrics.horizontalAdvance(t) for t in texts) + h_padding


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
