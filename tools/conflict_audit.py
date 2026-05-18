"""Full command conflict audit — runs standalone, no app instance needed."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from samsara import plugin_commands
from samsara.command_registry import CommandMatcher
from samsara.command_packs import get_enabled_packs

commands_path = ROOT / "commands.json"
with open(commands_path, encoding="utf-8") as f:
    builtins = json.load(f)

plugin_commands.load_plugins(str(ROOT / "plugins" / "commands"))

matcher = CommandMatcher()
matcher.load_builtins(builtins)
matcher.load_plugins(plugin_commands._REGISTRY)
matcher.freeze()

all_entries = matcher._sorted
all_rows = matcher._match_table

default_enabled = get_enabled_packs({})

# ── 1. Exact phrase conflicts ────────────────────────────────────────────────
phrase_map = {}
for tokens, entry in all_rows:
    p = " ".join(tokens)
    phrase_map.setdefault(p, [])
    if entry not in phrase_map[p]:
        phrase_map[p].append(entry)

exact = {p: es for p, es in phrase_map.items() if len(es) > 1}
print(f"\n{'='*60}")
print(f"1. EXACT CONFLICTS: {len(exact)}")
print("="*60)
for p, entries in sorted(exact.items()):
    for e in entries:
        role = "PRIMARY" if p == e.phrase else "ALIAS"
        print(f"  [{role}] \"{p}\" -> {e.source}:{e.phrase}  pack={e.pack}")

# ── 2. Prefix overlaps (default-enabled packs) ───────────────────────────────
enabled_phrases = []
for e in all_entries:
    if e.pack in default_enabled:
        enabled_phrases.append((e.phrase, e))
    for alias in e.aliases:
        if e.pack in default_enabled:
            enabled_phrases.append((alias, e))

prefix_pairs = []
seen_pairs = set()
for i, (pa, ea) in enumerate(enabled_phrases):
    ta = pa.split()
    for pb, eb in enabled_phrases[i+1:]:
        tb = pb.split()
        if ea is eb:
            continue
        short, long_, se, le = (pa, pb, ea, eb) if len(ta) <= len(tb) else (pb, pa, eb, ea)
        ts = short.split()
        tl = long_.split()
        if tl[:len(ts)] == ts:
            key = (short, long_)
            if key not in seen_pairs:
                seen_pairs.add(key)
                prefix_pairs.append((short, long_, se.pack, le.pack))

prefix_pairs.sort(key=lambda x: x[0])
print(f"\n{'='*60}")
print(f"2. PREFIX OVERLAPS (default-enabled packs): {len(prefix_pairs)}")
print("="*60)
for short, long_, sp, lp in prefix_pairs:
    print(f"  \"{short}\" shadows \"{long_}\"  [{sp} -> {lp}]")

# ── 3. Cross-plugin alias conflicts ─────────────────────────────────────────
print(f"\n{'='*60}")
print("3. ALIAS -> PRIMARY CONFLICTS (alias of A == primary phrase of B)")
print("="*60)
primaries = {e.phrase: e for e in all_entries}
alias_conflicts = []
for e in all_entries:
    for alias in e.aliases:
        if alias in primaries and primaries[alias] is not e:
            other = primaries[alias]
            alias_conflicts.append((alias, e.phrase, other.phrase))
for alias, owner_phrase, other_phrase in sorted(alias_conflicts):
    print(f"  alias \"{alias}\" on \"{owner_phrase}\" clashes with primary \"{other_phrase}\"")
if not alias_conflicts:
    print("  (none)")

# ── 4. Very short primary phrases (≤2 tokens, enabled by default) ────────────
print(f"\n{'='*60}")
print("4. SHORT PRIMARY PHRASES (<=2 tokens, risk of accidental trigger)")
print("="*60)
for e in sorted(all_entries, key=lambda e: (e.token_count, e.phrase)):
    if e.token_count <= 2 and e.pack in default_enabled:
        print(f"  [{e.token_count}t] \"{e.phrase}\"  pack={e.pack}  src={e.source}")

# ── 5. Phonetically close pairs (manual inspection helpers) ──────────────────
# Simple heuristic: phrases that differ in <=1 token (after normalising common
# Whisper confusions) or share all but their last token.
print(f"\n{'='*60}")
print("5. NEAR-HOMOPHONE RISKS (phrases sharing all-but-last token)")
print("="*60)
CONFUSABLE = {
    "to": "two", "two": "to", "too": "to",
    "up": "app", "app": "up",
    "in": "inn", "on": "one",
    "for": "four", "four": "for",
    "write": "right", "right": "write",
    "by": "buy",
    "I": "aye",
}
all_p = [(e.phrase, e) for e in all_entries if e.pack in default_enabled]
near_pairs = set()
for i, (pa, ea) in enumerate(all_p):
    ta = pa.split()
    for pb, eb in all_p[i+1:]:
        if ea is eb:
            continue
        tb = pb.split()
        if len(ta) != len(tb):
            continue
        diffs = [(a, b) for a, b in zip(ta, tb) if a != b]
        if len(diffs) == 1:
            a_tok, b_tok = diffs[0]
            # Check if they're known homophones or Whisper confusables
            if (CONFUSABLE.get(a_tok) == b_tok or
                    CONFUSABLE.get(b_tok) == a_tok or
                    a_tok[:3] == b_tok[:3]):  # share 3-char prefix
                key = tuple(sorted([pa, pb]))
                if key not in near_pairs:
                    near_pairs.add(key)
                    print(f"  \"{pa}\" vs \"{pb}\"  (differ on '{a_tok}' vs '{b_tok}')")

# ── 6. Remainder-required commands with vague/short triggers ─────────────────
print(f"\n{'='*60}")
print("6. VAGUE TRIGGERS — short phrase that REQUIRES a meaningful remainder")
print("   (if user pauses mid-phrase, the bare trigger fires incorrectly)")
print("="*60)
# Heuristics: 1-2 token command, remainder is required for it to be useful
VAGUE_SUSPECTS = {"ask", "go to", "send", "get", "find", "open", "use",
                  "search for", "browse to", "bring", "switch to", "note"}
for e in sorted(all_entries, key=lambda e: e.phrase):
    if e.phrase in VAGUE_SUSPECTS and e.pack in default_enabled:
        print(f"  \"{e.phrase}\"  pack={e.pack}  src={e.source}")

print("\nDone.")
