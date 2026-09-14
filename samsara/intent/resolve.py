"""resolve(utterance) -> resolved | suggest | dictation, in a fixed tier order.

    tier 1  EXACT       the utterance (as heard, or with fillers stripped) IS a
                        catalog alias                        confidence 1.0
    tier 2  GRAMMAR     grammar.Grammar.parse_chain: verb classes + slots +
                        chaining; resolved at >= GRAMMAR_RESOLVE
    tier 3  SIMILARITY  order-free token overlap (Dice) against every alias:
                        >= SIMILARITY_RESOLVE with nothing missing and at most
                        SIMILARITY_MAX_EXTRA extra words -> resolved;
                        >= SIMILARITY_SUGGEST -> suggest ("did you mean");
                        below -> dictation

Safety: whole-utterance control words (session_modes) only ever match tier 1
as heard. A destructive command never resolves through a look-alike, a
synonym or tier 3 -- that is a suggestion, never an execution. Tier 3 never
resolves a command with a required argument.

The catalog comes from the LIVE registry (the queue-15 path:
command_catalog.catalog_from_registry_rows over CommandMatcher.list_commands()),
with the remainder-reading argument inference re-applied from the plugin
handlers (command_catalog.infer_args). commands_catalog.json is a test
fixture only. NOT wired into dispatch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from samsara.intent import grammar as gr
from samsara.intent import normalize as nz

#: Tier order -- a constant with a test.
TIERS = ("exact", "grammar", "similarity")

GRAMMAR_RESOLVE = 0.75
SIMILARITY_RESOLVE = 0.86
SIMILARITY_SUGGEST = 0.60
SIMILARITY_MAX_EXTRA = 1
SUGGESTION_LIMIT = 3
#: Tiers 1+2 must decide within this on the full catalog (measured by tests).
LATENCY_BUDGET_MS = 30.0

RESOLVED, SUGGEST, DICTATION = "resolved", "suggest", "dictation"


@dataclass(frozen=True)
class Resolution:
    kind: str                                  # resolved | suggest | dictation
    canonical_id: Optional[str] = None
    args: dict = field(default_factory=dict)
    confidence: float = 0.0
    tier: Optional[str] = None                 # exact | grammar | similarity
    span: tuple = (0, 0)
    chain: tuple = ()                          # (Parse, ...) when "and"/"then" chained
    suggestions: tuple = ()                    # (canonical_id, ...) best first
    normalized: str = ""
    t12_ms: float = 0.0                        # time spent in tiers 1+2


# ---------------------------------------------------------------------------
# Catalog loading (live registry path)
# ---------------------------------------------------------------------------


def live_rows():
    """CommandMatcher.list_commands() of the production executor (no app)."""
    from tools.dump_command_metadata import build_executor  # noqa: PLC0415
    return build_executor()._matcher.list_commands()


def load_records(rows=None) -> list:
    """Catalog records from live registry rows (built when not given)."""
    from samsara import command_catalog as cc  # noqa: PLC0415
    from samsara import plugin_commands  # noqa: PLC0415
    records = cc.catalog_from_registry_rows(rows if rows is not None else live_rows())
    handlers = {}
    for module, entries in plugin_commands._MODULE_ENTRIES.items():
        stem = module.rsplit(".", 1)[-1]
        for phrase, entry in entries.items():
            handlers[f"{stem}.{cc.slug(phrase)}"] = (phrase, entry.get("func"))
    for rec in records:
        if rec["kind"] == "plugin" and not rec["args"] and rec["canonical_id"] in handlers:
            phrase, func = handlers[rec["canonical_id"]]
            from dataclasses import asdict  # noqa: PLC0415
            rec["args"] = [asdict(a) for a in cc.infer_args("plugin", phrase, None, func)]
    return records


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class IntentResolver:
    def __init__(self, records=None, *, rows=None, app_names=(), reserved=None):
        from samsara import command_catalog as cc  # noqa: PLC0415
        self.records = list(records) if records is not None else load_records(rows)
        self.reserved = frozenset(reserved if reserved is not None else cc.reserved_whole_utterances())
        self.by_id = {r["canonical_id"]: r for r in self.records}
        self.grammar = gr.Grammar(self.records, tuple(app_names), self.reserved)
        self._exact = {}
        for rec in self.records:
            canonical = cc.canonical_phrase(rec)
            for alias in rec["aliases"]:
                if alias not in self._exact or alias == canonical:
                    self._exact[alias] = rec["canonical_id"]
        self._similarity_index = [
            (t, [self._alias_key(w) for w in t.alias]) for t in self.grammar.templates
        ]

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _alias_key(word: str) -> str:
        return nz.confusion_key(gr._stem(word))

    @staticmethod
    def _variants(tok: str) -> set:
        out = {nz.confusion_key(gr._stem(tok))}
        for vc in gr.VERB_CLASSES:
            if tok in vc.synonyms:
                out.update(nz.confusion_key(v) for v in vc.verbs)
        for word, spoken in gr.OBJECT_SYNONYMS.items():
            if tok in spoken:
                out.add(nz.confusion_key(gr._stem(word)))
        return out

    def _missing_required(self, cid: str, args: dict) -> bool:
        return any(a["required"] and a["name"] not in args for a in self.by_id[cid].get("args", ()))

    def _destructive(self, cid: str) -> bool:
        return self.by_id[cid].get("risk") == "destructive"

    # -- tiers -------------------------------------------------------------

    def tier_exact(self, raw: list, stripped: list) -> Optional[Resolution]:
        heard = " ".join(raw)
        if heard in self._exact:
            return Resolution(RESOLVED, self._exact[heard], {}, 1.0, "exact", (0, len(raw)))
        said = " ".join(stripped)
        if said != heard and said in self._exact and said not in self.reserved:
            return Resolution(RESOLVED, self._exact[said], {}, 1.0, "exact", (0, len(stripped)))
        return None

    def tier_grammar(self, stripped: list):
        """-> (Resolution resolved, None) | (None, suggestion Parse or None)."""
        chain = self.grammar.parse_chain(stripped)
        if not chain:
            return None, None
        weakest = min(p.confidence for p in chain)
        first = chain[0]
        unsafe = any(self._destructive(p.canonical_id) and p.word_penalty > 0 for p in chain)
        missing = any(self._missing_required(p.canonical_id, p.args) for p in chain)
        if weakest >= GRAMMAR_RESOLVE and not unsafe and not missing:
            return Resolution(RESOLVED, first.canonical_id, dict(first.args), weakest, "grammar",
                              first.span, tuple(chain) if len(chain) > 1 else ()), None
        return None, first

    def tier_similarity(self, stripped: list) -> list:
        """[(score, missing, extra, Template)] best first."""
        content = [t for t in stripped if t not in gr.OPTIONAL_WORDS] or stripped
        variants = [self._variants(t) for t in content]
        scored = {}
        for t, keys in self._similarity_index:
            used = [False] * len(content)
            matched = 0
            for k in keys:
                for i, vs in enumerate(variants):
                    if not used[i] and k in vs:
                        used[i] = True
                        matched += 1
                        break
            if not matched:
                continue
            score = 2.0 * matched / (len(content) + len(keys))
            item = (round(score, 4), len(keys) - matched, len(content) - matched, t)
            best = scored.get(t.canonical_id)
            if best is None or item[:1] > best[:1]:
                scored[t.canonical_id] = item
        return sorted(scored.values(), key=lambda x: (-x[0], x[1], x[3].index))

    # -- entry point -------------------------------------------------------

    def resolve(self, utterance: str) -> Resolution:
        t0 = time.perf_counter()
        raw = nz.tokens(utterance)
        readings = nz.filler_variants(raw)         # least stripped first ("snap right now")
        stripped = readings[-1]
        normalized = " ".join(stripped)
        if not raw:
            return Resolution(DICTATION, normalized=normalized)
        for reading in readings:
            hit = self.tier_exact(raw, reading)
            if hit is not None:
                return _with(hit, " ".join(reading), (time.perf_counter() - t0) * 1000)
        weak, best = None, None
        for reading in readings:                   # every reading; the most confident parse wins
            resolved, weak_i = self.tier_grammar(reading)
            if resolved is not None and (best is None or resolved.confidence > best[0].confidence):
                best = (resolved, reading)
            weak = weak or weak_i
        t12 = (time.perf_counter() - t0) * 1000
        if best is not None:
            return _with(best[0], " ".join(best[1]), t12)

        ranked = self.tier_similarity(stripped)
        suggestions = []
        if weak is not None:
            suggestions.append(weak.canonical_id)
        if ranked:
            score, missing, extra, t = ranked[0]
            rec = self.by_id[t.canonical_id]
            safe = (rec.get("risk") != "destructive"
                    and not any(a["required"] for a in rec.get("args", ())))
            if (score >= SIMILARITY_RESOLVE and missing == 0 and extra <= SIMILARITY_MAX_EXTRA and safe
                    and weak is None):
                return Resolution(RESOLVED, t.canonical_id, {}, score, "similarity", (0, len(stripped)),
                                  normalized=normalized, t12_ms=t12)
            for score_i, _m, _e, ti in ranked:
                if score_i < SIMILARITY_SUGGEST or len(suggestions) >= SUGGESTION_LIMIT:
                    break
                if ti.canonical_id not in suggestions:
                    suggestions.append(ti.canonical_id)
        if suggestions:
            top_conf = weak.confidence if weak is not None else ranked[0][0]
            return Resolution(SUGGEST, suggestions[0], dict(weak.args) if weak is not None else {},
                              top_conf, "grammar" if weak is not None else "similarity",
                              (0, len(stripped)), suggestions=tuple(suggestions[:SUGGESTION_LIMIT]),
                              normalized=normalized, t12_ms=t12)
        return Resolution(DICTATION, normalized=normalized, t12_ms=t12)


def _with(res: Resolution, normalized: str, t12: float) -> Resolution:
    return Resolution(res.kind, res.canonical_id, res.args, res.confidence, res.tier, res.span, res.chain,
                      res.suggestions, normalized, t12)


_default: Optional[IntentResolver] = None


def resolve(utterance: str, resolver: Optional[IntentResolver] = None) -> Resolution:
    """Resolve against `resolver`, or a lazily built one over the live registry."""
    global _default
    if resolver is None:
        if _default is None:
            _default = IntentResolver()
        resolver = _default
    return resolver.resolve(utterance)
