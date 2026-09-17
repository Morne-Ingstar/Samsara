"""Small, deterministic tier-2 grammar for the canonical command catalog.

This module is deliberately shadow-only.  It reads the catalog supplied by
the caller and returns canonical command ids; it does not load the catalog,
dispatch a command, create Qt objects, or import ``dictation``.

The result is a list of ``(canonical_id, args, span, confidence)`` tuples.
Spans are token-index pairs, matching the existing intent grammar's span
convention.  A parse must cover the complete utterance (apart from a single
politeness word), which is important: a command-shaped substring in prose is
not a command.
"""
from __future__ import annotations

import re
from typing import Any


Parse = tuple[str, dict, tuple[int, int], float]
ParseResult = list[Parse]

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_CHAIN_WORDS = frozenset({"and", "then"})
_POLITE_PREFIXES = frozenset({"please"})
_REPEAT_WORDS = {"twice": 2, "again": 2}
_SIDE_WORDS = {
    "left": "left", "right": "right", "top": "top", "bottom": "bottom",
    "up": "top", "down": "bottom", "upper": "top", "lower": "bottom",
    "middle": "middle", "center": "middle", "centre": "middle", "full": "full",
}
_MONITOR_WORDS = frozenset({
    "left", "right", "top", "bottom", "upper", "lower", "middle", "center", "centre",
    "central", "screen", "monitor", "display", "tv", "here", "other", "main", "primary",
    "first", "second", "third", "fourth", "fifth", "sixth", "one", "two", "three",
    "four", "five", "six", "seven", "eight", "nine", "zero",
})
_SEPARATORS = frozenset({"and", "into", "to", "on", "onto", "with", "plus"})


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def _records(catalog: Any) -> list[dict]:
    if isinstance(catalog, dict):
        catalog = catalog.get("commands", ())
    if isinstance(catalog, list) and all(isinstance(record, dict) for record in catalog):
        return catalog
    return [record if isinstance(record, dict) else record.to_dict()
            for record in (catalog or ()) if hasattr(record, "to_dict")]


def _catalog_index(catalog: Any, records: list[dict]) -> dict[str, list[tuple[dict, list[str], int]]]:
    """Return a small first-token index, reused while the same catalog lives.

    The cache is an implementation detail of this read-only operation: the
    catalog object is retained as the identity guard, so equal-looking but
    independently supplied catalogs never share state.
    """
    cache = getattr(_catalog_index, "_cache", None)
    if cache is not None and cache[0] is catalog:
        return cache[1]
    index: dict[str, list[tuple[dict, list[str], int]]] = {}
    for record_index, record in enumerate(records):
        for alias in record.get("aliases", ()):
            alias_words = _tokens(alias)
            if alias_words:
                index.setdefault(alias_words[0], []).append((record, alias_words, record_index))
    _catalog_index._cache = (catalog, index)
    return index


def _number_words() -> dict[str, int]:
    # The application already owns the spoken-number table.  Import lazily so
    # importing this pure module does not import a plugin or attach handlers.
    from plugins.commands.show_numbers import _WORD_TO_NUM
    return _WORD_TO_NUM


def _ordinals() -> dict[str, int]:
    from plugins.commands.windows import _ORDINALS
    return _ORDINALS


def _number(words: list[str]) -> int | None:
    """Parse one 0--99 number, including digits, compounds, and ordinals."""
    if not words:
        return None
    if len(words) == 1:
        word = words[0]
        if word.isdigit() and int(word) <= 99:
            return int(word)
        ordinal = _ordinals().get(word)
        if ordinal is not None:
            return ordinal
    numeric = _number_words()
    if len(words) <= 2 and all(word in numeric for word in words):
        value = sum(numeric[word] for word in words)
        if 0 <= value <= 99:
            return value
    return None


def _letter_map() -> dict[str, str]:
    # This is the one canonical NATO/letter table used by the window switcher.
    cached = getattr(_letter_map, "_cache", None)
    if cached is not None:
        return cached
    from plugins.commands.window_switcher import PHONETIC
    out = {}
    for phrase, letter in PHONETIC.items():
        out[" ".join(_tokens(phrase))] = letter.lower()
    for letter in "abcdefghijklmnopqrstuvwxyz":
        out[letter] = letter
    _letter_map._cache = out
    return out


def _letters(words: list[str]) -> list[str] | None:
    if not words:
        return None
    mapping = _letter_map()
    result = []
    i = 0
    while i < len(words):
        pair = " ".join(words[i:i + 2])
        if pair in mapping and " " in pair:
            result.append(mapping[pair])
            i += 2
            continue
        word = words[i]
        if word in _SEPARATORS:
            i += 1
            continue
        letter = mapping.get(word)
        if letter is None:
            return None
        result.append(letter)
        i += 1
    return result or None


def _repeat(words: list[str]) -> tuple[list[str], int | None]:
    if not words:
        return words, None
    if words[-1] in _REPEAT_WORDS:
        return words[:-1], _REPEAT_WORDS[words[-1]]
    if len(words) >= 2 and words[-1] == "times":
        n = _number([words[-2]])
        if n is not None and n > 0:
            return words[:-2], n
    return words, None


def _strip_prefix(words: list[str]) -> list[str]:
    while len(words) > 1 and words[0] in _POLITE_PREFIXES:
        words = words[1:]
    return words


def _match_alias(words: list[str], alias_words: list[str]) -> int:
    if alias_words and words[:len(alias_words)] == alias_words:
        return len(alias_words)
    return 0


def _text_arg(arg: dict, words: list[str]) -> tuple[str, Any] | None:
    if any(word in _CHAIN_WORDS for word in words):
        return None
    if not words:
        return None if arg.get("required") else (arg["name"], None)
    return arg["name"], " ".join(words)


def _monitor(words: list[str]) -> str | None:
    words = list(words)
    while words and words[0] in {"to", "on", "onto"}:
        words.pop(0)
    words = [word for word in words if word not in {"the", "a"}]
    if not words or any(word not in _MONITOR_WORDS for word in words):
        return None
    return " ".join(words)


def _slot(arg: dict, words: list[str]) -> tuple[str, Any] | None:
    kind = arg.get("type", "text")
    name = arg.get("name", "arg")
    if kind == "int":
        if name.endswith("s"):
            values = []
            current = []
            for word in words + ["and"]:
                if word in _SEPARATORS:
                    if current:
                        value = _number(current)
                        if value is None:
                            return None
                        values.append(value)
                        current = []
                else:
                    current.append(word)
            return (name, values) if values else None
        value = _number(words)
        return (name, value) if value is not None else None
    if kind == "nato_letter":
        letters = _letters(words)
        if letters is None:
            return None
        if name.endswith("s") or len(letters) > 1:
            return name, letters
        return name, letters[0]
    if kind == "app_name":
        if not words or len(words) > 4 or any(word in _CHAIN_WORDS for word in words):
            return None
        name_text = " ".join(words)
        # resolve_window is intentionally consulted here rather than copied;
        # the spoken name remains the argument so this parser stays harmless.
        from plugins.commands.app_verbs import resolve_window
        try:
            resolve_window(name_text)
        except Exception:
            # Resolution is live-window context.  An unresolved app can still
            # be a valid target for an open/launch command, so retain the text.
            pass
        return name, name_text
    if kind == "monitor":
        value = _monitor(words)
        return (name, value) if value is not None else None
    if kind == "side":
        normalized = [_SIDE_WORDS.get(word, word) for word in words if word not in {"the", "side", "screen"}]
        value = " ".join(normalized)
        return (name, value) if value in {"left", "right", "top", "bottom", "middle", "full"} else None
    return _text_arg(arg, words)


def _slots(record: dict, remainder: list[str]) -> dict | None:
    args = record.get("args") or []
    if not args:
        return {} if not remainder else None
    if len(args) == 1:
        parsed = _slot(args[0], remainder)
        if parsed is None:
            return None
        name, value = parsed
        return {} if value is None else {name: value}

    kinds = [a.get("type") for a in args]
    # Both two-slot command families use an explicit destination boundary.
    if kinds == ["app_name", "monitor"]:
        for i, word in enumerate(remainder):
            if word in {"to", "on", "onto"} and i and i + 1 < len(remainder):
                app = _slot(args[0], remainder[:i])
                monitor = _slot(args[1], remainder[i:])
                if app and monitor:
                    return {app[0]: app[1], monitor[0]: monitor[1]}
        return None
    if kinds == ["nato_letter", "monitor"]:
        for i, word in enumerate(remainder):
            if word in {"to", "on", "onto"} and i and i + 1 < len(remainder):
                letter = _slot(args[0], remainder[:i])
                monitor = _slot(args[1], remainder[i:])
                if letter and monitor:
                    return {letter[0]: letter[1], monitor[0]: monitor[1]}
        return None
    # Catalog records currently have no other structured multi-slot shape.
    return None


def _parse_one(words: list[str], index: dict[str, list[tuple[dict, list[str], int]]], offset: int = 0) -> ParseResult:
    original_length = len(words)
    words = _strip_prefix(words)
    words, repeat = _repeat(words)
    if not words:
        return []
    candidates: list[tuple[int, int, int, Parse]] = []
    for record, alias_words, record_index in index.get(words[0], ()):
        consumed = _match_alias(words, alias_words)
        if not consumed:
            continue
        args = _slots(record, words[consumed:])
        if args is None:
            continue
        if repeat is not None:
            args = dict(args)
            args["repeat"] = repeat
        confidence = 0.99 if consumed == len(words) else 0.97
        span = (offset, offset + original_length)
        parsed = (record.get("canonical_id", ""), args, span, confidence)
        # Argument-bearing templates are the grammar's useful form.  This
        # lets ``open obsidian`` select app_verbs.open over a generated exact
        # alias such as builtin.open_obsidian.
        typed = bool(record.get("args"))
        candidates.append((consumed, int(typed), -record_index, parsed))

    # The longest canonical phrase wins; catalog order breaks ties.
    if not candidates:
        # The spoken form is common and unambiguous, but the legacy catalog
        # has the non-argument ``switch window`` form.  Prefer the lettered
        # window-switch command when that canonical record is present.
        if len(words) == 3 and words[:2] == ["switch", "window"]:
            letter_record = next(
                (record for entries in index.values() for record, _alias, _n in entries
                 if record.get("canonical_id") == "window_switcher.window_switch"),
                None,
            )
            letter = _letters(words[2:])
            if letter_record and letter:
                return [(letter_record["canonical_id"], {"label": letter[0]},
                         (offset, offset + original_length), 0.96)]
        return []
    candidates.sort(key=lambda item: (item[1], item[0], item[2]), reverse=True)
    return [candidates[0][3]]


def _split_chain(words: list[str]) -> list[list[str]] | None:
    parts = []
    start = 0
    for i, word in enumerate(words):
        if word in _CHAIN_WORDS:
            if i == start or i == len(words) - 1:
                return None
            parts.append(words[start:i])
            start = i + 1
    if start == 0:
        return [words]
    parts.append(words[start:])
    return parts


def parse(utterance: str, catalog: Any) -> ParseResult:
    """Parse a complete utterance against ``catalog`` without side effects.

    ``catalog`` may be the decoded ``commands_catalog.json`` object, its
    ``commands`` list, or a list of ``CommandSpec`` instances.  Empty or
    malformed input returns ``[]``.  Chaining is accepted only when every
    segment parses, so ordinary prose remains empty.
    """
    words = _tokens(utterance)
    if not words:
        return []
    records = _records(catalog)
    if not records:
        return []
    index = _catalog_index(catalog, records)

    whole = _parse_one(words, index)
    if whole:
        return whole

    parts = _split_chain(words)
    if not parts or len(parts) == 1:
        return []
    result: ParseResult = []
    offset = 0
    for part in parts:
        parsed = _parse_one(part, index, offset)
        if not parsed:
            return []
        result.extend(parsed)
        offset += len(part) + 1
    return result


__all__ = ["Parse", "ParseResult", "parse"]
