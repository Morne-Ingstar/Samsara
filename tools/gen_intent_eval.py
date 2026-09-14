"""Generate tests/fixtures/intent_eval.jsonl -- the intent grammar's test set.

For EVERY canonical_id in the live catalog: 40 lines = 37 positive phrasings
(aliases, slot fills, fillers and politeness, verb synonyms, determiners,
reordered slots, Whisper-style mishearings, numerals/number words, casing and
punctuation) + 3 negative near-misses that must NOT resolve to it:

    sentence       a dictated sentence containing the phrase (must not
                   resolve to ANY command -- a sentence never executes)
    other_command  the nearest different command's phrasing
    nonsense       word salad around one of the phrase's words

Seeded and deterministic: the same catalog gives the same file byte for
byte. This file is the owner's "exhaustive list" as a TEST SET -- nothing in
samsara/intent reads it, and it must never become a lookup table.

The generator keeps its own synonym and mishearing tables on purpose: some
entries (GENERATOR_ONLY_*) are absent from samsara/intent, so the coverage
number includes phrasings the grammar was not told about.

Usage:
  F:\\envs\\sami\\python.exe tools\\gen_intent_eval.py            write the fixture
  F:\\envs\\sami\\python.exe tools\\gen_intent_eval.py --check    exit 1 if stale
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURE = REPO / "tests" / "fixtures" / "intent_eval.jsonl"
SEED = 25
PER_COMMAND = 40
NEGATIVES = 3
POSITIVES = PER_COMMAND - NEGATIVES

#: The app names the eval resolver is given (stands in for app index + windows).
APP_NAMES = ("chrome", "spotify", "discord", "notepad", "vs code", "firefox", "obsidian", "steam",
             "terminal", "word", "excel", "slack")

PREFIXES = ("can you", "could you", "could you just", "please", "hey", "um", "can you please",
            "would you please", "go ahead and", "okay", "uh", "i need you to", "so")
SUFFIXES = ("please", "for me", "now", "thanks", "right now", "for me please")

GEN_SYNONYMS = {
    "open": ("launch", "bring up", "pull up", "start"), "launch": ("open", "fire up"),
    "close": ("exit", "quit", "shut"), "move": ("put", "drag", "shift", "chuck"),
    "send": ("move", "throw", "toss", "push"), "put": ("move", "stick", "place"),
    "throw": ("fling", "send"), "show": ("display", "reveal", "pull up"), "hide": ("dismiss", "conceal"),
    "start": ("begin", "kick off"), "stop": ("end", "halt", "finish"), "cancel": ("abort", "call off"),
    "switch": ("jump", "flip", "change"), "focus": ("switch to", "jump to"), "search": ("look", "hunt"),
    "find": ("search for", "look for"), "snap": ("dock",), "select": ("highlight",),
    "delete": ("remove", "erase"), "remove": ("delete", "erase"), "clear": ("wipe", "delete"),
    "pause": ("freeze", "hold"), "resume": ("continue", "unpause"), "minimize": ("minimise", "shrink"),
    "maximize": ("maximise", "enlarge"), "refresh": ("reload", "rescan"), "reload": ("refresh",),
    "save": ("store",), "go": ("navigate", "head"), "read": ("read out", "say"),
    "record": ("capture", "film"), "capture": ("record",), "toggle": ("flip",), "mute": ("silence",),
    "list": ("enumerate",), "tell": ("message", "ping"), "ask": ("message",),
}
#: Synonyms samsara/intent does NOT know -- measured misses by design.
GENERATOR_ONLY_SYNONYMS = {
    "open": ("pop open",), "close": ("nuke",), "show": ("surface",), "move": ("scoot",),
    "stop": ("knock off",), "hide": ("stash",),
}

GEN_MISHEARINGS = {
    "tab": ("tap",), "close": ("clothes",), "right": ("write",), "one": ("won",), "two": ("to", "too"),
    "four": ("for",), "to": ("two",), "for": ("four",), "mute": ("moot",), "cube": ("queue",),
    "read": ("red",), "new": ("knew",), "here": ("hear",), "eight": ("ate",), "pause": ("paws",),
    "cursor": ("curser",), "maximize": ("maximise",), "minimize": ("minimise",), "center": ("centre",),
    "color": ("colour",), "sight": ("site",), "week": ("weak",),
}
GENERATOR_ONLY_MISHEARINGS = {
    "scroll": ("scrawl",), "snap": ("snack",), "window": ("widow",), "volume": ("volum",),
    "screen": ("scream",), "note": ("not",), "timer": ("time",), "lights": ("likes",),
}

NUMBER_WORDS = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven",
                "8": "eight", "9": "nine"}

SLOT_VALUES = {
    "text": ("buy milk", "the build is green", "call mom at five", "water the plants", "check the oven"),
    "playlist": ("lofi beats", "jazz", "my workout mix"),
    "int": ("3", "7", "twelve", "five", "number nine"),
    "nato_letter": ("bravo", "charlie", "delta", "b", "echo"),
    "nato_letters": ("alpha and bravo", "charlie delta", "b and c"),
    "monitor": ("the left screen", "monitor 2", "the right monitor", "the second display", "the main screen",
                "the tv", "the other one"),
    "side": ("left", "right", "the top left", "the right side"),
}

SENTENCE_TEMPLATES = (
    "yesterday i told my sister we should {p} before dinner",
    "honestly the whole point of the meeting was to {p} and nobody did",
    "our manager keeps asking whether anyone remembered to {p} last week",
    "last night he wrote in the notes that you can {p} whenever it feels right",
    "my grandmother never understood why people would {p} on a sunday",
    "during the lecture she explained how to {p} without breaking anything",
    "yesterday afternoon we argued for an hour about who would {p} first",
    "in the story the old captain decides to {p} as the storm arrives",
)

NONSENSE = ("purple", "banana", "sideways", "glimmer", "octopus", "velvet", "thunderous", "marmalade",
            "wobble", "quartz", "noodle", "lantern", "zeppelin", "pickle", "cobalt", "saxophone")


def _rng(cid: str, salt: str) -> random.Random:
    return random.Random(f"{SEED}:{cid}:{salt}")


def load_catalog() -> tuple:
    """(records, reserved whole-utterance words) from the live registry."""
    from samsara import command_catalog as cc
    from samsara.intent.resolve import load_records
    return load_records(), cc.reserved_whole_utterances()


# ---------------------------------------------------------------------------
# Positives
# ---------------------------------------------------------------------------


def _slot_text(arg: dict, rng: random.Random) -> str:
    kind = arg["type"]
    if kind == "nato_letter" and arg["name"].endswith("s"):
        kind = "nato_letters"
    if kind == "app_name":
        return rng.choice(APP_NAMES)
    return rng.choice(SLOT_VALUES.get(kind, SLOT_VALUES["text"]))


def base_forms(rec: dict, reserved: set, rng: random.Random) -> list:
    """[(text, kind, alias)] -- each alias with its slots filled."""
    args = rec.get("args", [])
    kinds = [a["type"] for a in args]
    out = []
    aliases = [a for a in rec["aliases"] if a not in reserved] or list(rec["aliases"])
    for alias in aliases:
        if alias in reserved:
            out.append((alias, "alias", alias))
            continue
        if kinds == ["app_name", "monitor"]:
            for _ in range(3):
                app, dest = rng.choice(APP_NAMES), rng.choice(SLOT_VALUES["monitor"])
                out.append((f"{alias} {app} to {dest}", "slot", alias))
                out.append((f"{alias} to {dest} {app}", "reorder", alias))
            continue
        required = any(a["required"] for a in args)
        if not required:
            out.append((alias, "alias", alias))
        for arg in args:
            for _ in range(2 if arg["required"] else 1):
                out.append((f"{alias} {_slot_text(arg, rng)}", "slot", alias))
    return out


def _swap_first(text: str, table: dict) -> list:
    toks = text.split()
    return [" ".join([syn] + toks[1:]) for syn in table.get(toks[0], ())]


def _swap_any(text: str, table: dict) -> list:
    toks, out = text.split(), []
    for i, tok in enumerate(toks):
        for alt in table.get(tok, ()):
            out.append(" ".join(toks[:i] + [alt] + toks[i + 1:]))
    return out


def variants(text: str, kind: str, reserved: set) -> list:
    """[(text, kind)] phrasings of one base form."""
    if text in reserved:
        return [(text.capitalize() + ".", "punct"), (text.upper(), "punct"), (text + "!", "punct"),
                (text.capitalize(), "punct"), (text.replace(" ", ", ", 1) if " " in text else text + "?", "punct")]
    toks = text.split()
    out = [(text, kind)]
    out += [(f"{p} {text}", "filler") for p in PREFIXES]
    out += [(f"{text} {s}", "filler") for s in SUFFIXES]
    out += [(t, "synonym") for t in _swap_first(text, GEN_SYNONYMS)]
    out += [(t, "synonym_unknown") for t in _swap_first(text, GENERATOR_ONLY_SYNONYMS)]
    out += [(t, "mishear") for t in _swap_any(text, GEN_MISHEARINGS)]
    out += [(t, "mishear_unknown") for t in _swap_any(text, GENERATOR_ONLY_MISHEARINGS)]
    if len(toks) >= 2 and toks[1] not in ("the", "a", "my", "this", "to"):
        out.append((" ".join([toks[0], "the"] + toks[1:]), "determiner"))
    if len(toks) == 2 and kind == "alias":
        out.append((f"{toks[1]} {toks[0]}", "reorder"))
    swapped = [NUMBER_WORDS.get(t, t) for t in toks]
    if swapped != toks:
        out.append((" ".join(swapped), "numeral"))
    back = {v: k for k, v in NUMBER_WORDS.items()}
    swapped = [back.get(t, t) for t in toks]
    if swapped != toks:
        out.append((" ".join(swapped), "numeral"))
    out.append((text.capitalize() + ".", "punct"))
    if len(toks) >= 2:
        out.append((toks[0].capitalize() + ", " + " ".join(toks[1:]) + "?", "punct"))
    return out


def positives(rec: dict, reserved: set) -> list:
    """[(text, kind, base form)]"""
    rng = _rng(rec["canonical_id"], "positive")
    pool = []
    for text, kind, _alias in base_forms(rec, reserved, rng):
        pool.extend((t, k, text) for t, k in variants(text, kind, reserved))
    by_kind, seen = {}, set()
    for text, kind, base in pool:
        if text not in seen:
            seen.add(text)
            by_kind.setdefault(kind, []).append((text, base))
    for texts in by_kind.values():
        rng.shuffle(texts)
    chosen, kinds = [], sorted(by_kind)
    while len(chosen) < POSITIVES and any(by_kind.values()):
        for kind in kinds:
            if by_kind[kind] and len(chosen) < POSITIVES:
                text, alias = by_kind[kind].pop()
                chosen.append((text, kind, alias))
    base = [(t, t) for t, _k, _a in base_forms(rec, reserved, _rng(rec["canonical_id"], "positive"))]
    padding = []                                   # short aliases: prefix x suffix x casing
    for text, alias in base:
        if text in reserved:                       # a control word is only ever heard alone
            for lead in ("", "... "):
                for case in (str.lower, str.capitalize, str.upper, str.title):
                    for end in ("", ".", "!", "?", "...", "!!"):
                        padding.append((lead + case(text) + end, "punct", alias))
        else:
            for p in PREFIXES:
                for s in SUFFIXES:
                    padding.append((f"{p} {text} {s}", "filler", alias))
                    padding.append((f"{p} {text} {s}".capitalize() + ".", "filler", alias))
    for text, kind, alias in padding:
        if len(chosen) >= POSITIVES:
            break
        if text not in seen:
            seen.add(text)
            chosen.append((text, kind, alias))
    return chosen


# ---------------------------------------------------------------------------
# Negatives
# ---------------------------------------------------------------------------


def _phrase(rec: dict, reserved: set, rng: random.Random) -> str:
    return base_forms(rec, reserved, rng)[0][0]


def negatives(rec: dict, records: list, reserved: set, alias_owners: dict) -> list:
    cid = rec["canonical_id"]
    rng = _rng(cid, "negative")
    phrase = _phrase(rec, reserved, rng)
    out = [(rng.choice(SENTENCE_TEMPLATES).format(p=phrase), "sentence")]

    mine = set(rec["aliases"])
    words = set(" ".join(rec["aliases"]).split())
    sharing = {o for a in mine for o in alias_owners[a]}
    ranked = []
    for other in records:
        if other["canonical_id"] in sharing:
            continue
        for alias in other["aliases"]:
            if alias not in reserved:
                ranked.append((-len(words & set(alias.split())), len(alias), other["canonical_id"], alias, other))
    ranked.sort(key=lambda r: r[:4])
    for *_key, alias, other in ranked:
        other_text = _phrase({**other, "aliases": [alias]}, reserved, _rng(other["canonical_id"], "as-other"))
        if other_text not in mine:                 # a filled slot can spell one of MY aliases
            break
    out.append((other_text, "other_command"))

    anchor = max(phrase.split(), key=len)
    salad = rng.sample(NONSENSE, 4)
    out.append((f"{salad[0]} {salad[1]} {anchor} {salad[2]} {salad[3]}", "nonsense"))
    return out


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


def build_lines(records: list, reserved: set) -> list:
    alias_owners = {}
    for rec in records:
        for alias in rec["aliases"]:
            alias_owners.setdefault(alias, set()).add(rec["canonical_id"])
    lines = []
    for rec in sorted(records, key=lambda r: r["canonical_id"]):
        cid = rec["canonical_id"]
        for text, kind, base in positives(rec, reserved):
            # The catalog itself can make a phrasing ambiguous: a filled slot
            # that spells another command's alias ("snap" + "right" is
            # builtin.snap_right) or one alias claimed by two commands.
            accept = sorted({cid} | alias_owners.get(base, set()))
            lines.append({"id": cid, "text": text, "polarity": "positive", "kind": kind, "accept": accept})
        for text, kind in negatives(rec, records, reserved, alias_owners):
            lines.append({"id": cid, "text": text, "polarity": "negative", "kind": kind, "reject": cid})
    return lines


def render(lines: list) -> str:
    return "".join(json.dumps(line, sort_keys=True, ensure_ascii=True) + "\n" for line in lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the fixture is stale")
    args = parser.parse_args(argv)
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        records, reserved = load_catalog()
    text = render(build_lines(records, reserved))
    if args.check:
        current = FIXTURE.read_text(encoding="utf-8") if FIXTURE.exists() else ""
        if current != text:
            print(f"stale: {FIXTURE.relative_to(REPO)}")
            return 1
        return 0
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE.relative_to(REPO)}: {text.count(chr(10))} lines, {len(records)} commands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
