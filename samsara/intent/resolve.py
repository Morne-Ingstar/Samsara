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

Execution rules (queue 93, after the Move A tribunal returned HALT). The
tiers above decide WHICH command an utterance looks like; these two rules
decide whether looking like it is allowed to RUN it. They are checked at the
one chokepoint every resolved decision passes through (_apply_rules), so
there is no tier-shaped hole:

    rule 1  TWO WORDS   a command whose canonical spoken phrase is a single
                        word never executes, and a single-word utterance
                        never executes -- whatever the confidence. This is
                        what makes "copy", "yes", "no" and "Claude"
                        dictatable for a user with no keyboard, and it kills
                        "To, um..." -> window_cube.two at the root.
                        Blocked -> DICTATION.
    rule 2  EXACT ONLY  only a literal match may execute: tier 1, or a tier-2
                        parse whose command words carry no penalty at all
                        (no stem, no confusion key, no synonym). A
                        fuzzy/similarity/homophone match is a suggestion,
                        never an execution -- `to` -> `two` is a homophone
                        promotion and is unreachable as an execution path.
                        Blocked -> SUGGEST.

Rule 2 is unconditional. Rule 1 is waived by the escape hatch: a configurable
prefix word (`intent.command_prefix`, empty = off) that forces the utterance
after it to be read as a command, so "<prefix> copy" runs copy. The prefix
never waives rule 2.

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
from dataclasses import dataclass, field, replace
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

#: Bumped whenever a rule that can change an outcome changes, and stamped on
#: every Resolution so a shadow log read a week later can separate the
#: populations. 1 = pre-93 (tiers only). 2 = rules 1 and 2 below.
EXECUTION_RULES_VERSION = 2
#: Rule 1: fewer words than this never executes -- neither the command's
#: canonical spoken phrase nor the utterance itself.
MIN_COMMAND_WORDS = 2
#: Why a decision that a tier resolved was not allowed to execute.
BLOCK_ONE_WORD_COMMAND = "one_word_command"
BLOCK_ONE_WORD_UTTERANCE = "one_word_utterance"
BLOCK_INEXACT = "inexact_match"


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
    word_penalty: float = 0.0                  # 0.0 = the command words matched literally
    alias_words: int = 0                       # words in the alias that matched (0 = not alias-driven)
    blocked: Optional[str] = None              # the execution rule that demoted this, if any
    forced: bool = False                       # the command prefix was spoken
    #: Queue 127. True only when the command words the user spoke ARE a
    #: registered spoken form -- a canonical phrase or an alias, in order,
    #: with nothing inserted. word_penalty == 0 does NOT imply this: a
    #: hand-written grammar rule returns a non-literal form at zero cost
    #: (Astra's probe: "go page downwards" -> scroll.page_down, no alias,
    #: penalty 0.0), and a template accepts inserted determiners for free.
    #: Strict: a FILLED SLOT is not literal either, because the words in the
    #: slot are the user's and not the catalog's. That is what makes this a
    #: measurement rather than a candidate gate -- see block_reason, which
    #: does NOT read it. The shadow row records it so the gap between what
    #: rule 2 says and what rule 2 does stays countable from the log.
    literal: bool = False
    rules_version: int = EXECUTION_RULES_VERSION


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
    def __init__(self, records=None, *, rows=None, app_names=(), reserved=None, prefix=""):
        from samsara import command_catalog as cc  # noqa: PLC0415
        from samsara.command_scope import parse_scope  # noqa: PLC0415
        self.records = list(records) if records is not None else load_records(rows)
        self.reserved = frozenset(reserved if reserved is not None else cc.reserved_whole_utterances())
        self.by_id = {r["canonical_id"]: r for r in self.records}
        #: Rule 1 reads the CANONICAL SPOKEN PHRASE, not the alias that
        #: happened to match: "get rid of the demo" matching windows.bring
        #: through its one-word alias "get" is still a one-word command.
        #: Argument slots are not words -- app_verbs.open is "open", one word.
        self._canonical_words = {r["canonical_id"]: len(cc.canonical_phrase(r).split())
                                 for r in self.records}
        #: Default escape-hatch prefix; resolve(prefix=...) overrides per call.
        self.command_prefix = str(prefix or "")
        self._app_names = tuple(app_names)
        # Queue 68: scoped commands are only candidates when their scope is
        # live. The grammar, exact and similarity indexes are built per set of
        # excluded (out-of-scope) ids and cached, so a lower-ranked in-scope
        # parse still wins instead of being hidden behind an out-of-scope one.
        self._scopes = {}
        for rec in self.records:
            try:
                scope = parse_scope(rec.get("scope"))
            except ValueError:
                scope = parse_scope({"tags": ["invalid_scope"]})
            if scope is not None:
                self._scopes[rec["canonical_id"]] = scope
        self._views = {}
        self.grammar, self._exact, self._similarity_index = self._view(frozenset())

    # -- scope views (queue 68) ---------------------------------------------

    def _view(self, excluded: frozenset):
        """(grammar, exact, similarity_index) over records minus `excluded`."""
        view = self._views.get(excluded)
        if view is None:
            from samsara import command_catalog as cc  # noqa: PLC0415
            records = [r for r in self.records if r["canonical_id"] not in excluded]
            grammar = gr.Grammar(records, self._app_names, self.reserved)
            exact = {}
            for rec in records:
                canonical = cc.canonical_phrase(rec)
                for alias in rec["aliases"]:
                    if alias not in exact or alias == canonical:
                        exact[alias] = rec["canonical_id"]
            similarity = [(t, [self._alias_key(w) for w in t.alias]) for t in grammar.templates]
            view = self._views[excluded] = (grammar, exact, similarity)
        return view

    def excluded_ids(self, context=None) -> frozenset:
        """Canonical ids whose scope is not live in `context` (None = no
        foreground known, current tags)."""
        if not self._scopes:
            return frozenset()
        from samsara import command_scope  # noqa: PLC0415
        if context is None:
            context = command_scope.MatchContext.unresolved(command_scope.UNRESOLVED_NO_PROVIDER,
                                                            command_scope.active_tags())
        return frozenset(cid for cid, scope in self._scopes.items()
                         if not command_scope.scope_live(scope, context)[0])

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

    def _trigger_words(self, parse) -> int:
        """How many words triggered THIS chain member.

        The alias it matched, or -- when no alias did, which is every
        hand-written rule -- the command's own canonical phrase, because
        that is the form the grammar recognised. Queue 127: the old
        expression skipped alias-less parses, so a rule-source member
        contributed nothing to the minimum.
        """
        if parse.alias:
            return len(parse.alias.split())
        return self._canonical_words.get(parse.canonical_id, MIN_COMMAND_WORDS)

    def _parse_is_literal(self, parse, stripped: list) -> bool:
        """True when the tokens this member spanned ARE its alias, in order.

        Queue 127 / Astra: zero word-penalty was being read as "the command
        words matched literally", and it does not mean that. A template
        accepts inserted determiners for free ("complete THE alarm"), a
        reordering for free ("turn the off the lights"), and a hand-written
        rule returns a form that is in no alias at all.

        Deliberately STRICT about slots: "search for cats" spans the alias
        "search for" plus a word the catalog never registered, so it is not
        literal here. That is the honest reading of "the words the user spoke
        are a registered form", and it is why this is a measurement and not a
        gate -- gating on it would stop every parameterised command. Read the
        number with that in mind: non-literal is one bucket holding four very
        different things (rule source, inserted word, reordered, filled
        slot), and the queue 127 report breaks it out.
        """
        if not parse.alias:
            return False                        # rule source: never a registered form
        start, end = parse.span
        return nz.tokens(parse.alias) == list(stripped[start:end])

    def _missing_required(self, cid: str, args: dict) -> bool:
        return any(a["required"] and a["name"] not in args for a in self.by_id[cid].get("args", ()))

    def _destructive(self, cid: str) -> bool:
        return self.by_id[cid].get("risk") == "destructive"

    # -- tiers -------------------------------------------------------------

    def tier_exact(self, raw: list, stripped: list, exact=None) -> Optional[Resolution]:
        exact = self._exact if exact is None else exact
        heard = " ".join(raw)
        if heard in exact:
            # Queue 127: alias_words was left at 0 here, so rule 1's
            # matched-form leg never fired on the exact tier and the
            # canonical-length leg was doing that work by accident.
            return Resolution(RESOLVED, exact[heard], {}, 1.0, "exact", (0, len(raw)),
                              alias_words=len(raw), literal=True)
        said = " ".join(stripped)
        if said != heard and said in exact and said not in self.reserved:
            return Resolution(RESOLVED, exact[said], {}, 1.0, "exact", (0, len(stripped)),
                              alias_words=len(stripped), literal=True)
        return None

    def tier_grammar(self, stripped: list, grammar=None):
        """-> (Resolution resolved, None) | (None, suggestion Parse or None)."""
        chain = (self.grammar if grammar is None else grammar).parse_chain(stripped)
        if not chain:
            return None, None
        weakest = min(p.confidence for p in chain)
        first = chain[0]
        unsafe = any(self._destructive(p.canonical_id) and p.word_penalty > 0 for p in chain)
        missing = any(self._missing_required(p.canonical_id, p.args) for p in chain)
        if weakest >= GRAMMAR_RESOLVE and not unsafe and not missing:
            # The WORST penalty in the chain: one look-alike anywhere in
            # "open notepad and to" makes the whole chain inexact (rule 2).
            penalty = max(p.word_penalty for p in chain)
            # The SHORTEST trigger in the chain, counted over EVERY member
            # (queue 127 defect 3: only the first member's canonical was
            # checked, so a one-word command in a later member walked past
            # rule 1). A member with no alias -- every hand-written rule --
            # falls back to its canonical phrase, which is the form the
            # grammar recognised; the old code's `if p.alias` skipped those
            # entirely and defaulted the whole chain to 0, which rule 1 then
            # read as "not alias-driven, nothing to check".
            alias_words = min(self._trigger_words(p) for p in chain)
            literal = all(self._parse_is_literal(p, stripped) for p in chain)
            return Resolution(RESOLVED, first.canonical_id, dict(first.args), weakest, "grammar",
                              first.span, tuple(chain) if len(chain) > 1 else (),
                              word_penalty=penalty, alias_words=alias_words,
                              literal=literal), None
        return None, first

    def tier_similarity(self, stripped: list, index=None) -> list:
        """[(score, missing, extra, Template)] best first."""
        content = [t for t in stripped if t not in gr.OPTIONAL_WORDS] or stripped
        variants = [self._variants(t) for t in content]
        scored = {}
        for t, keys in (self._similarity_index if index is None else index):
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

    # -- execution rules (queue 93) ----------------------------------------

    def block_reason(self, res: Resolution, spoken_words: int, *, forced: bool = False):
        """Which execution rule, if any, forbids `res` from executing.

        `spoken_words`: how many words are in the READING THAT MATCHED, not in
        the most-stripped reading. "To, um..." matched on ["to"] and is one
        word; "snap right now" matched on the alias itself and is three. Using
        the most-stripped reading instead would call "snap right now" a
        one-word utterance, because "right now" is a filler.
        Rule 1 is checked on the words the USER SPOKE: the trigger form the
        utterance actually matched, and the utterance itself. A two-word
        command reached through a one-word alias ("ava" for "hey ava") is
        still a single word triggering a command, which is the thing the rule
        exists to stop.

        Rule 2 is checked first, so the escape hatch cannot buy an inexact
        match: a spoken prefix says "this is a command", not "trust a
        homophone"."""
        if res.kind != RESOLVED or res.canonical_id is None:
            return None
        # Rule 2. Queue 93 described this as "only a literal match executes"
        # and implemented `word_penalty == 0`, which is a DIFFERENT question:
        # 12% of the corpus's zero-cost resolutions are not a registered
        # form at all. Queue 127 measures that gap (`literal`) and corrects
        # the wording; it deliberately does NOT gate on it, because gating on
        # it is not a repair, it is a product change. Under any honest
        # definition of "literal" a FILLED SLOT is not literal -- the words
        # in the slot are the user's, not the catalog's -- so a literal gate
        # takes out every parameterised command: "search for cats" and "move
        # chrome to the left screen" stop executing, measured at slot 0/242
        # and numeral 9/82 over the eval corpus. What rule 2 gates is
        # therefore unchanged: a look-alike or synonym that was PAID for,
        # or a tier-3 match at all.
        if res.tier == "similarity" or res.word_penalty > 0:
            return BLOCK_INEXACT
        if forced:
            return None
        # Rule 1, two legs. The third -- the command's CANONICAL word count --
        # was removed in queue 127. It is catalog metadata, not evidence about
        # what the user said: `ask_ollama.yes` has the canonical "yes", so the
        # two-word alias "go ahead", spoken as two words, came back as
        # `dictation / one_word_command` and the confirmation word was
        # unusable. Nothing the user did was one word. The two legs below say
        # what the rule actually claims -- a command may not be triggered by a
        # single spoken word -- and they cover every case the canonical leg
        # covered that was real: a one-word trigger still fails leg 1 ("ava"
        # for "hey ava"), and a one-word utterance still fails leg 2.
        #
        # Removing it has a published consequence, and it is a finding, not
        # an oversight. "I feel it, baby." reached health_tracker.symptom and
        # the canonical leg blocked it -- for a reason that is false. The
        # user said four words, and the form that matched is "i feel", a
        # REGISTERED TWO-WORD ALIAS of that command. Nothing about it is one
        # word. What actually makes it unsafe is that the command pairs a
        # weak two-word alias with a free-text slot that swallows the rest of
        # the sentence, and no rule here can tell it apart from "search for
        # cats", which is the same shape and is a command the user wants.
        # That is a decision about the catalog (or about an address prefix),
        # and it is the owner's to make on these numbers -- see the queue 127
        # report. Leaving a leg that blocks the right utterance for the wrong
        # reason would have hidden the question.
        if 0 < res.alias_words < MIN_COMMAND_WORDS:
            return BLOCK_ONE_WORD_COMMAND
        if spoken_words < MIN_COMMAND_WORDS:
            return BLOCK_ONE_WORD_UTTERANCE
        return None

    def _apply_rules(self, res: Resolution, spoken_words: int, *, forced: bool) -> Resolution:
        """The one chokepoint EVERY non-dictation decision passes through --
        suggestions included, not just executions.

        An inexact match becomes a SUGGEST: the gate still says what it
        thought, it just may not act on it. A one-word command or utterance
        becomes DICTATION, because the whole point of rule 1 is that the user
        gets their word typed -- and a suggestion that claims the utterance
        would lose it just as surely as an execution would. That is the
        ~8%-of-utterances data loss the Move A auditor warned about; rule 1
        makes it unreachable rather than tolerable. The id it would have run
        is kept in `suggestions` so the shadow log can still count what was
        demoted, and `blocked` records which rule did it."""
        one_word = not forced and spoken_words < MIN_COMMAND_WORDS
        reason = self.block_reason(res, spoken_words, forced=forced)
        if reason is None and not one_word:
            return replace(res, forced=forced)
        if one_word:
            reason = BLOCK_ONE_WORD_UTTERANCE
        elif reason == BLOCK_INEXACT:
            return replace(res, kind=SUGGEST, blocked=reason, forced=forced,
                           suggestions=res.suggestions or (res.canonical_id,))
        return replace(res, kind=DICTATION, canonical_id=None, args={}, chain=(),
                       blocked=reason, forced=forced,
                       suggestions=res.suggestions or ((res.canonical_id,) if res.canonical_id else ()))

    def _strip_prefix(self, raw: list, prefix) -> tuple:
        """(tokens after the escape-hatch prefix, True) when the utterance
        opens with it, else (raw, False). Empty prefix = the hatch is off,
        which is the default: the owner picks the word from data later."""
        word = self.command_prefix if prefix is None else prefix
        ptoks = nz.tokens(str(word or ""))
        if ptoks and raw[:len(ptoks)] == ptoks:
            return raw[len(ptoks):], True
        return raw, False

    # -- entry point -------------------------------------------------------

    def resolve(self, utterance: str, context=None, *, prefix=None) -> Resolution:
        """context: command_scope.MatchContext of the utterance (captured at
        dispatch time); None = no foreground known, current tags.
        prefix: the escape-hatch word for this call (None = the resolver's
        own, which is "" -- off -- unless one was configured)."""
        t0 = time.perf_counter()
        grammar, exact, index = self._view(self.excluded_ids(context))
        raw, forced = self._strip_prefix(nz.tokens(utterance), prefix)
        readings = nz.filler_variants(raw)         # least stripped first ("snap right now")
        stripped = readings[-1]
        normalized = " ".join(stripped)
        if not raw:
            return Resolution(DICTATION, normalized=normalized, forced=forced)
        for reading in readings:
            hit = self.tier_exact(raw, reading, exact)
            if hit is not None:
                # tier_exact's span is the matched form's own length: (0,
                # len(raw)) when the utterance as heard IS the alias, else
                # (0, len(reading)).
                hit = self._apply_rules(hit, hit.span[1], forced=forced)
                return _with(hit, " ".join(reading), (time.perf_counter() - t0) * 1000)
        weak, best = None, None
        for reading in readings:                   # every reading; the most confident parse wins
            resolved, weak_i = self.tier_grammar(reading, grammar)
            if resolved is not None and (best is None or resolved.confidence > best[0].confidence):
                best = (resolved, reading)
            weak = weak or weak_i
        t12 = (time.perf_counter() - t0) * 1000
        if best is not None:
            # Demotion ends here rather than falling through to tier 3: rule 1
            # says a one-word command or utterance IS dictation, and letting a
            # fuzzier tier have another go at it would be the opposite.
            return _with(self._apply_rules(best[0], len(best[1]), forced=forced),
                         " ".join(best[1]), t12)

        ranked = self.tier_similarity(stripped, index)
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
                # Rule 2 always demotes this one: tier 3 is order-free token
                # overlap over confusion keys -- fuzzy by construction, so it
                # can only ever suggest. Kept as a resolved candidate and run
                # through the same chokepoint so there is one place to read.
                hit = Resolution(RESOLVED, t.canonical_id, {}, score, "similarity", (0, len(stripped)),
                                 normalized=normalized, t12_ms=t12)
                return self._apply_rules(hit, len(stripped), forced=forced)
            for score_i, _m, _e, ti in ranked:
                if score_i < SIMILARITY_SUGGEST or len(suggestions) >= SUGGESTION_LIMIT:
                    break
                if ti.canonical_id not in suggestions:
                    suggestions.append(ti.canonical_id)
        if suggestions:
            top_conf = weak.confidence if weak is not None else ranked[0][0]
            hit = Resolution(SUGGEST, suggestions[0], dict(weak.args) if weak is not None else {},
                             top_conf, "grammar" if weak is not None else "similarity",
                             (0, len(stripped)), suggestions=tuple(suggestions[:SUGGESTION_LIMIT]),
                             normalized=normalized, t12_ms=t12)
            # A suggestion goes through the rules too: "to", "two" and
            # "Claude." are one word, so they are the user's text, not a
            # "did you mean" that eats it.
            return self._apply_rules(hit, len(stripped), forced=forced)
        return Resolution(DICTATION, normalized=normalized, t12_ms=t12, forced=forced)


def _with(res: Resolution, normalized: str, t12: float) -> Resolution:
    return replace(res, normalized=normalized, t12_ms=t12)


_default: Optional[IntentResolver] = None


def resolve(utterance: str, resolver: Optional[IntentResolver] = None, context=None,
            *, prefix=None) -> Resolution:
    """Resolve against `resolver`, or a lazily built one over the live registry."""
    global _default
    if resolver is None:
        if _default is None:
            _default = IntentResolver()
        resolver = _default
    return resolver.resolve(utterance, context, prefix=prefix)
