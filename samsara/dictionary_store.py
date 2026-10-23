"""Shared vocabulary, correction persistence and decoder prompt budgeting.

No window or Qt dependency. Existing profile files retain their path and format.
"""

import json
import logging
import os
import re
import shutil
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import List

from samsara.languages import is_boundaryless_script_char
from samsara.paths import quarantine_corrupt_file
from samsara.builtin_vocabulary import (
    BuiltinPreferences, builtin_entries, command_word_enabled, prompt_words, word_key,
    fit_decoder_prompt, relevant_words,
)

logger = logging.getLogger(__name__)

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


class DictionaryStore:
    """Dictionary service shared by dictation, teaching and dictionary editors."""

    def __init__(self, app):
        self.app = app
        self.custom_vocab: List[str] = []
        self.corrections_dict: dict = {}
        self._corrections_pattern = None
        self._corrections_lookup: dict = {}
        self._load_failed = False
        self._quarantine_path = None
        self._loaded_bytes = None
        self._builtin_preferences = BuiltinPreferences(app.config_path)
        self.load_training_data()

    def get_builtin_words(self):
        """UI rows stay separate from custom_vocab and dictionary exports."""
        disabled = self._builtin_preferences.disabled
        return [dict(entry, enabled=word_key(entry['word']) not in disabled)
                for entry in builtin_entries(self.app.config)]

    def set_builtin_word_enabled(self, word, enabled):
        if word_key(word) not in {word_key(entry['word']) for entry in self.get_builtin_words()}:
            return False
        return self._builtin_preferences.set_enabled(word, enabled)

    def load_training_data(self):
        self._loaded_bytes = None
        training_file = Path(self.app.config_path).parent / 'training_data.json'
        if training_file.exists():
            try:
                raw = training_file.read_bytes()
                data = json.loads(raw.decode('utf-8'))
                self.custom_vocab     = data.get('vocabulary', [])
                self.corrections_dict = data.get('corrections', {})
                self._loaded_bytes = raw
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
        self._migrate_legacy_user_corrections(training_file)
        self._rebuild_corrections_pattern()

    def _migrate_legacy_user_corrections(self, training_file: Path) -> None:
        """Move pre-783d728 Corrections-tab entries into the live store.

        The old panel stored deterministic corrections beside the config in
        user_corrections.json. Archive that legacy file after a one-time merge
        rather than deleting it. Existing authoritative corrections win,
        including case-insensitive duplicates.
        """
        legacy_file = training_file.with_name('user_corrections.json')
        if not legacy_file.exists():
            return

        try:
            with open(legacy_file, 'r', encoding='utf-8') as f:
                legacy_data = json.load(f)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("[STORE] Could not migrate legacy user corrections: %s", exc)
            return

        if not isinstance(legacy_data, dict):
            logger.warning("[STORE] Could not migrate legacy user corrections: expected an object")
            return

        existing_keys = {
            unicodedata.normalize('NFC', str(key)).lower()
            for key in self.corrections_dict
        }
        migrated = 0
        for wrong, right in legacy_data.items():
            wrong, right = str(wrong).strip(), str(right).strip()
            normalized_wrong = unicodedata.normalize('NFC', wrong).lower()
            if wrong and right and normalized_wrong not in existing_keys:
                self.corrections_dict[wrong] = right
                existing_keys.add(normalized_wrong)
                migrated += 1

        if not self.save_training_data():
            logger.warning("[STORE] Could not save migrated legacy user corrections")
            return

        archive = legacy_file.with_name(
            f"user_corrections.json.migrated-{date.today().isoformat()}"
        )
        suffix = 2
        while archive.exists():
            archive = legacy_file.with_name(
                f"user_corrections.json.migrated-{date.today().isoformat()}-{suffix}"
            )
            suffix += 1
        try:
            legacy_file.replace(archive)
        except OSError as exc:
            logger.warning("[STORE] Migrated %d legacy corrections but could not archive the source: %s", migrated, exc)
            return
        logger.info("[STORE] Migrated %d legacy user corrections to training_data.json", migrated)

    @staticmethod
    def _bundled_default_dictionary_path() -> Path:
        """Return the packaged Default dictionary for first-profile seeding."""
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
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

    def learn_draft_correction(self, wrong: str, right: str, *, vocabulary=False) -> bool:
        """Explicit post-edit approval; a failed save must not teach in memory."""
        wrong, right = wrong.strip(), right.strip()
        if not right or (not vocabulary and (not wrong or wrong.casefold() == right.casefold())):
            return False
        corrections, words = dict(self.corrections_dict), list(self.custom_vocab)
        if vocabulary:
            if any(word.casefold() == right.casefold() for word in words):
                return True
            ok = self.add_vocab_word(right)
        else:
            key = next((key for key in corrections if key.casefold() == wrong.casefold()), wrong)
            ok = self.add_correction(key, right)
        if not ok:
            self.corrections_dict.clear()
            self.corrections_dict.update(corrections)
            self.custom_vocab[:] = words
            self._rebuild_corrections_pattern()
        return ok

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
            # Preserve an unchanged existing file exactly, including its layout.
            previous = json.loads(self._loaded_bytes) if self._loaded_bytes is not None else None
            if previous is not None and data == {
                'vocabulary': previous.get('vocabulary', []),
                'corrections': previous.get('corrections', {}),
            }:
                raw = self._loaded_bytes
            else:
                raw = json.dumps(data, indent=2).replace('\n', os.linesep).encode('utf-8')
            training_file.write_bytes(raw)
            self._loaded_bytes = raw
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

    # Legacy character cap; 801 also enforces 223 tokens with the loaded
    # recognizer's tokenizer because 800 characters can exceed that limit.
    _PROMPT_CHAR_BUDGET = 800

    def get_initial_prompt(self, include_vocabulary: bool = True) -> "str | None":
        """Build the Whisper initial_prompt from prioritized layered parts.

        include_vocabulary=False omits Priority 2 (genuine user vocabulary
        -- names, jargon, technical terms added via add_vocab_word, the
        "Common terms:" layer) AND Priority 3 (the auto-derived command
        vocabulary) and 801's built-in hints, returning Priority 1 (explicit custom prompt)
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

            # Reserve the enabled wake hint before fitting a whole user list.
            # Explicit initial_prompt remains the user's unbounded override.
            wake_reserve = 0
            covered_words = []
            if include_vocabulary:
                hints = relevant_words(self.app.config, self.custom_vocab,
                                       self._builtin_preferences.disabled)
                wake = builtin_entries(self.app.config)[0]['word']
                if hints and word_key(hints[0]) == word_key(wake):
                    wake_reserve = len('Built-in words: ' + hints[0]) + 1

            # Priority 2: custom vocabulary -- included whole or not at all
            # (never truncate mid-phrase).
            if include_vocabulary and self.custom_vocab and remaining > 0:
                vocab_part = f"Common terms: {', '.join(self.custom_vocab)}"
                needed = len(vocab_part) + (1 if parts else 0)
                if needed + wake_reserve <= remaining:
                    parts.append(vocab_part)
                    remaining -= needed
                    covered_words = self.custom_vocab

            # 801: built-ins are hints only on vocabulary-enabled paths. Keep
            # the existing free-form guard and the user's higher priority.
            if include_vocabulary and remaining > 0:
                builtins = prompt_words(self.app.config, self.custom_vocab,
                                        self._builtin_preferences.disabled,
                                        min(360, remaining - (1 if parts else 0)),
                                        covered_words=covered_words)
                if builtins:
                    parts.append(builtins)
                    remaining -= len(builtins) + (1 if len(parts) > 1 else 0)

            # Priority 3: command vocabulary -- lowest priority, so it's the
            # one truncated (item-by-item, never mid-word) to fit what's left.
            if include_vocabulary and remaining > 0:
                cmd_words = [word for word in self._get_command_vocabulary_words()
                             if command_word_enabled(word, self.custom_vocab,
                                                     self._builtin_preferences.disabled)]
                kept: List[str] = []
                for word in cmd_words:
                    candidate = f"Voice commands: {', '.join(kept + [word])}"
                    needed = len(candidate) + (1 if parts else 0)
                    if needed > remaining:
                        break
                    kept.append(word)
                if kept:
                    parts.append(f"Voice commands: {', '.join(kept)}")

            return fit_decoder_prompt(parts, getattr(self.app, 'model', None), custom_prompt)
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

def get_dictionary_store(app):
    """Resolve the service, accepting the legacy plugin/test attribute."""
    store = getattr(app, '__dict__', {}).get('dictionary_store')
    if store is not None:
        return store
    return getattr(app, 'voice_training_window', None)


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

