"""Shipped decoder hints, separate from user words and corrections.

Only command-capable decode paths opt in. Never turn these hints into
speculative text replacements: ordinary dictation retains its original prompt.
"""
import json
import logging
import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from samsara.command_packs import PACKS
from samsara.constants import DEFAULT_WAKE_PHRASE
from samsara.config_defaults import cfg_get

logger = logging.getLogger(__name__)


def word_key(word):
    return unicodedata.normalize("NFC", word).strip().casefold()


@lru_cache(maxsize=1)
def _shipped_entries():
    path = Path(__file__).with_name("data") / "builtin_vocabulary.json"
    try:
        with path.open(encoding="utf-8") as stream:
            return tuple(json.load(stream))
    except (OSError, ValueError) as exc:
        logger.warning("Could not load shipped vocabulary: %s", exc)
        return ()


def builtin_entries(config):
    """All switchable entries, including the currently configured wake phrase."""
    wake = (config.get("wake_word_config") or {}).get("phrase", DEFAULT_WAKE_PHRASE)
    wake = wake.strip() if isinstance(wake, str) else DEFAULT_WAKE_PHRASE
    wake = wake or DEFAULT_WAKE_PHRASE
    entries = [{"word": wake, "why": "Configured wake word.", "packs": ["core"]}]
    seen = {word_key(wake)}
    for entry in _shipped_entries():
        if word_key(entry["word"]) not in seen:
            entries.append(dict(entry, packs=list(entry["packs"])))
            seen.add(word_key(entry["word"]))
    return entries


def relevant_words(config, user_words, disabled=()):
    """Wake first, then enabled packs; user spelling wins normalized conflicts."""
    if not cfg_get(config, "builtin_vocabulary_enabled"):
        return []
    switches = config.get("command_packs") or {}
    enabled = {name for name, meta in PACKS.items()
               if meta["always_on"] or switches.get(name, meta["default_enabled"])}
    # This reads pack preferences only; it never probes installations or starts
    # an assistant just to construct a decoder prompt.
    disabled = {word_key(word) for word in disabled}
    users = {word_key(word): word for word in user_words}
    words = []
    for entry in builtin_entries(config):
        key = word_key(entry["word"])
        if key in disabled or not enabled.intersection(entry["packs"]):
            continue
        words.append(users.get(key, entry["word"]))
    return words


class BuiltinPreferences:
    """Profile-local opt-outs survive dictionary imports and never count as words."""

    def __init__(self, config_path):
        self.path = Path(config_path).parent / "builtin_vocabulary_disabled.json"
        self.disabled = set()
        self.load_failed = False
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, list) or not all(isinstance(word, str) for word in data):
                    raise ValueError("expected a list of words")
                self.disabled = {word_key(word) for word in data}
        except (OSError, ValueError) as exc:
            self.load_failed = True
            logger.warning("Could not load built-in vocabulary preferences: %s", exc)

    def set_enabled(self, word, enabled):
        if self.load_failed:
            return False  # Preserve an unreadable preference file for recovery.
        updated = set(self.disabled)
        if enabled:
            updated.discard(word_key(word))
        else:
            updated.add(word_key(word))
        if updated == self.disabled:
            return True
        temporary = self.path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(sorted(updated), ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            logger.warning("Could not save built-in vocabulary preferences: %s", exc)
            return False
        self.disabled = updated
        return True


def prompt_words(config, user_words, disabled, budget, covered_words=None):
    """Fit complete hints without crowding the higher-priority user vocabulary."""
    kept = []
    covered = {word_key(word) for word in
               (user_words if covered_words is None else covered_words)}
    for word in relevant_words(config, user_words, disabled):
        # A user entry already appears in Common terms; don't duplicate it.
        if word_key(word) in covered:
            continue
        candidate = "Built-in words: " + ", ".join(kept + [word])
        if len(candidate) <= budget:
            kept.append(word)
        elif not kept:
            break  # Never evict the wake phrase to squeeze in a shorter name.
    return "Built-in words: " + ", ".join(kept) if kept else ""


def command_word_enabled(phrase, user_words, disabled):
    """An opt-out must also suppress the same term in auto-derived commands."""
    user_keys = {word_key(word) for word in user_words}
    return not any(re.search(r"(?<!\w)" + re.escape(word_key(word)) + r"(?!\w)",
                             word_key(phrase))
                   for word in disabled if word_key(word) not in user_keys)


def fit_decoder_prompt(parts, model, explicit_prompt):
    """Whisper retains only the final 223 prompt tokens; preserve priorities.

    The loaded recognizer supplies its own tokenizer (no download or import).
    Character budgeting remains the fallback before a model has loaded.
    """
    tokenizer = getattr(model, "hf_tokenizer", None)
    if tokenizer is None:
        return " ".join(parts) if parts else None
    parts = list(parts)
    try:
        def tokens():
            return len(tokenizer.encode(" " + " ".join(parts).strip(),
                                        add_special_tokens=False).ids)
        # Reserve the first (wake) hint while trying a whole personal list.
        # Only an oversized explicit override may finally displace the wake.
        for prefix, reserve_first in (("Voice commands: ", False),
                                      ("Built-in words: ", True),
                                      ("Common terms: ", False),
                                      ("Built-in words: ", False)):
            while parts and tokens() > 223:
                index = next((i for i, part in enumerate(parts)
                              if part != explicit_prompt and part.startswith(prefix)), None)
                if index is None:
                    break
                if reserve_first and ", " not in parts[index][len(prefix):]:
                    break
                if prefix == "Common terms: " or ", " not in parts[index][len(prefix):]:
                    parts.pop(index)
                else:
                    parts[index] = parts[index].rsplit(", ", 1)[0]
        return " ".join(parts) if parts else None
    except (AttributeError, TypeError, ValueError):
        return " ".join(parts) if parts else None
