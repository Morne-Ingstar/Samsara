#!/usr/bin/env python3
"""
tools/phonetic_audit.py — Phonetic collision audit for Samsara commands.

Converts every command phrase to a CMU phoneme sequence and computes
Levenshtein distance between all pairs.  Flags homophones (identical
phonemes) and near-collisions (normalised distance <= 0.25).  Also checks
each command against a short list of common English phrases.

Usage:
    python tools/phonetic_audit.py

Exit code is always 0 (report, not a test).
"""

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pronouncing

# ---------------------------------------------------------------------------
# Tunable parameters
# ---------------------------------------------------------------------------

COLLISION_THRESHOLD = 0.25   # normalised Levenshtein; pairs below this are flagged

COMMON_PHRASES = [
    # Verb homophones / near-homophones with command names
    "save", "safe", "safer", "safely", "savings", "saved",
    "find", "fine", "fined", "finding", "mind",
    "close", "clothes", "closed", "closing", "cloves",
    "open", "opening", "opened",
    "send", "sent", "sending", "bend", "tend", "lend",
    "play", "played", "playing", "clay", "slay",
    "stop", "stopped", "stopping", "top", "drop",
    "back", "pack", "lack", "black", "knack",
    "next", "text", "flexed",
    "cut", "gut", "but", "hut",
    "copy", "coffee",
    "paste", "paced", "based", "taste",
    "undo", "into", "unto",
    "bold", "cold", "gold", "old", "told", "hold",
    "mute", "cute", "lute",
    "mark", "dark", "park", "bark",
    "click", "thick", "brick", "slick",
    "scroll", "stroll", "role", "poll",
    "tab", "cab", "grab", "jab", "dab",
    "zoom", "room", "doom", "bloom",
    "snap", "nap", "map", "lap", "cap",
    "pin", "bin", "tin", "win", "thin",
    "run", "done", "gun", "fun", "sun",
    "read", "reed", "red", "head",
]

# ---------------------------------------------------------------------------
# Phoneme utilities
# ---------------------------------------------------------------------------

_WARN_ISSUED = set()


def _letter_approx(word: str) -> list[str]:
    """Very rough letter-pair grapheme approximation for OOV words."""
    return list(word.upper())


def phrase_to_phones(phrase: str) -> list[str]:
    """Convert a phrase to a flat list of stress-stripped CMU phoneme tokens."""
    tokens: list[str] = []
    for word in phrase.lower().split():
        word_clean = re.sub(r"[^a-z']", "", word)
        if not word_clean:
            continue
        matches = pronouncing.phones_for_word(word_clean)
        if matches:
            raw = matches[0].split()
        else:
            if word_clean not in _WARN_ISSUED:
                print(f"[WARN] Unknown phonemes for '{word_clean}', using letter approximation")
                _WARN_ISSUED.add(word_clean)
            raw = _letter_approx(word_clean)
        # Strip stress digits
        tokens.extend(p.rstrip("012") for p in raw)
    return tokens


# ---------------------------------------------------------------------------
# Levenshtein over token lists
# ---------------------------------------------------------------------------

def levenshtein(a: list, b: list) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            )
        prev = curr
    return prev[-1]


# ---------------------------------------------------------------------------
# Load commands
# ---------------------------------------------------------------------------

def load_all_commands() -> list[tuple[str, str]]:
    """Return list of (phrase, source) for builtins + plugin canonicals + aliases."""
    # Builtins
    commands_path = PROJECT_ROOT / "commands.json"
    with open(commands_path, encoding="utf-8") as f:
        builtin_cmds = json.load(f)["commands"]

    entries: list[tuple[str, str]] = [
        (name.lower().strip(), "builtin")
        for name in builtin_cmds
    ]

    # Plugins — suppress noisy startup prints
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        from samsara import plugin_commands
        plugin_commands.load_plugins(str(PROJECT_ROOT / "plugins" / "commands"))

    seen_ids: set[int] = set()
    for phrase, entry_data in plugin_commands._REGISTRY.items():
        eid = id(entry_data)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)

        canonical = entry_data.get("phrase", phrase).lower().strip()
        src_module = getattr(entry_data.get("func"), "__module__", "plugin")
        src = f"plugin:{src_module}"
        entries.append((canonical, src))

        for alias in entry_data.get("aliases", []):
            entries.append((alias.lower().strip(), f"{src}:alias"))

    # Deduplicate by phrase (keep first occurrence)
    seen_phrases: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for phrase, src in entries:
        if phrase not in seen_phrases:
            seen_phrases.add(phrase)
            deduped.append((phrase, src))

    return deduped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    SEP  = "=" * 70
    DASH = "-" * 70

    # ---- Load ----
    print(SEP)
    print("PHONETIC COLLISION AUDIT")
    print(SEP)

    commands = load_all_commands()
    builtin_count  = sum(1 for _, s in commands if s == "builtin")
    plugin_count   = sum(1 for _, s in commands if s.startswith("plugin:"))
    total          = len(commands)
    print(f"Loaded {builtin_count} builtin commands and {plugin_count} plugin "
          f"commands ({total} total)\n")

    # ---- Phonemise ----
    phone_map: dict[str, list[str]] = {}
    for phrase, _ in commands:
        phone_map[phrase] = phrase_to_phones(phrase)

    fallback_count = len(_WARN_ISSUED)
    resolved_count = total - fallback_count  # approximation (may be < total if multi-word)
    print(f"Phonemes resolved for {resolved_count}, fallback used for "
          f"{fallback_count} word(s)\n")

    # ---- Section 1: homophones ----
    homophones: list[tuple[str, str]] = []
    phrases = [p for p, _ in commands]

    # Group by phoneme tuple for fast homophone detection
    from collections import defaultdict
    phone_groups: dict[tuple, list[str]] = defaultdict(list)
    for phrase in phrases:
        key = tuple(phone_map[phrase])
        phone_groups[key].append(phrase)

    for key, group in phone_groups.items():
        if len(group) > 1:
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    homophones.append((group[i], group[j]))

    # ---- Section 2: near collisions ----
    NearCollision = tuple  # (phrase_a, phrase_b, dist, norm_dist, common_flag)
    near: list[tuple] = []

    common_phone_map: dict[str, list[str]] = {}
    for cp in COMMON_PHRASES:
        common_phone_map[cp] = phrase_to_phones(cp)

    # Build common-phrase set of phoneme tuples for fast membership check
    common_phone_set: set[tuple] = {
        tuple(v) for v in common_phone_map.values() if v
    }

    # Pre-build set of homophone pairs for dedup
    homo_set = {(a, b) for a, b in homophones} | {(b, a) for a, b in homophones}

    for i in range(len(phrases)):
        for j in range(i + 1, len(phrases)):
            pa, pb = phrases[i], phrases[j]
            if (pa, pb) in homo_set:
                continue  # already in section 1
            phones_a = phone_map[pa]
            phones_b = phone_map[pb]
            if not phones_a or not phones_b:
                continue
            dist = levenshtein(phones_a, phones_b)
            max_len = max(len(phones_a), len(phones_b))
            norm = dist / max_len if max_len else 0.0
            if norm <= COLLISION_THRESHOLD:
                common_flag = (
                    tuple(phones_a) in common_phone_set
                    or tuple(phones_b) in common_phone_set
                )
                near.append((pa, pb, dist, norm, common_flag))

    near.sort(key=lambda x: (x[3], x[0]))

    # ---- Section 3: common-speech overlaps ----
    CommonOverlap = tuple  # (command_phrase, common_phrase, dist)
    overlaps: list[tuple] = []
    seen_overlap: set[tuple[str, str]] = set()

    for phrase in phrases:
        phones_p = phone_map[phrase]
        if not phones_p:
            continue
        for cp, phones_cp in common_phone_map.items():
            if not phones_cp:
                continue
            dist = levenshtein(phones_p, phones_cp)
            if dist <= 1:
                key = (phrase, cp)
                if key not in seen_overlap:
                    seen_overlap.add(key)
                    overlaps.append((phrase, cp, dist))

    overlaps.sort(key=lambda x: (x[0], x[2]))

    # ---- Print ----
    print(DASH)
    print("SECTION 1: HOMOPHONES (identical phonemes — HIGHEST RISK)")
    print(DASH)
    if homophones:
        for a, b in sorted(homophones):
            print(f"  {a!r:40} === {b!r}")
    else:
        print("  (none found)")

    print()
    print(DASH)
    print(f"SECTION 2: NEAR COLLISIONS (normalised distance <= {COLLISION_THRESHOLD})")
    print(DASH)
    if near:
        for pa, pb, dist, norm, flag in near:
            flag_str = "  [common word]" if flag else ""
            print(f"  {pa!r:38} <->  {pb!r:38}  dist={dist}  norm={norm:.2f}{flag_str}")
    else:
        print("  (none found)")

    print()
    print(DASH)
    print("SECTION 3: COMMON-SPEECH OVERLAPS (distance <= 1 from common phrase)")
    print(DASH)
    if overlaps:
        for phrase, cp, dist in overlaps:
            marker = "HOMOPHONE" if dist == 0 else f"distance={dist}"
            print(f"  {phrase!r:38}  collides with  {cp!r}  ({marker})")
    else:
        print("  (none found)")

    print()
    print(SEP)
    print(f"SUMMARY: {len(homophones)} homophones, {len(near)} near-collisions, "
          f"{len(overlaps)} common-speech overlaps")
    print(SEP)


if __name__ == "__main__":
    main()
