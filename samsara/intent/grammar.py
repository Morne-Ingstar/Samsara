"""Tier 2: the deterministic grammar -- verb + object + slots, not a sentence list.

Every catalog alias becomes a TEMPLATE: its tokens, then the command's
declared argument slots. An utterance matches a template when, token by
token, each alias word is said as itself, as a Whisper look-alike
(normalize.confusion_key), as a singular/plural, as a synonym from its VERB
CLASS (first word) or from OBJECT_SYNONYMS (later words), with determiners
and other OPTIONAL_WORDS allowed in between -- and the rest of the utterance
parses as the command's slots. A few RULES cover families whose phrasing is
not alias-shaped (volume, scroll, numbered tabs, dictating to Claude).

A parse is Parse(canonical_id, args, confidence, span) or None. "and"/"then"
chaining returns several parses, each of which must parse on its own.

Slot parsers are shared by every command and reuse the plugins' own grammar
(see normalize.py and docs/INTENT_GRAMMAR.md "Reuse"):
  app_name   samsara.app_index.score_name_match / MATCH_FLOOR against the
             names the caller supplies (app index + running windows)
  monitor    plugins/commands/windows._is_destination_text / _parse_send_remainder
  int        normalize.parse_number (show_numbers) / parse_ordinal (windows)
  nato       normalize.parse_letters (window_switcher)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from samsara.intent import normalize as nz

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerbClass:
    name: str
    synonyms: tuple          # spoken forms (may be multi-word)
    verbs: frozenset         # catalog verbs (first alias token) the class covers


#: Verb classes. An alias whose first word is in `verbs` may be said with any
#: of `synonyms`. Add a class here (docs/INTENT_GRAMMAR.md "Adding a verb class").
VERB_CLASSES = (
    VerbClass("PLACE", ("move", "put", "send", "throw", "drag", "stick", "shift", "push", "toss",
                        "fling", "place", "chuck", "take", "shove"),
              frozenset({"move", "put", "send", "throw"})),
    VerbClass("OPEN", ("open", "launch", "start", "run", "bring up", "pull up", "fire up", "start up",
                       "boot up", "load", "open up"),
              frozenset({"open", "launch"})),
    VerbClass("BEGIN", ("start", "begin", "kick off", "commence", "initiate"),
              frozenset({"start", "begin"})),
    VerbClass("CLOSE", ("close", "exit", "quit", "shut", "kill", "terminate", "close out", "shut down"),
              frozenset({"close", "exit", "quit"})),
    VerbClass("STOP", ("stop", "end", "finish", "halt", "quit"),
              frozenset({"stop", "end", "finish"})),
    VerbClass("CANCEL", ("cancel", "abort", "call off", "scrap"),
              frozenset({"cancel", "abort"})),
    VerbClass("SWITCH", ("switch", "focus", "jump", "change", "flip", "go", "move"),
              frozenset({"switch", "focus"})),
    VerbClass("SHOW", ("show", "display", "reveal", "bring up", "pull up", "open"),
              frozenset({"show"})),
    VerbClass("HIDE", ("hide", "dismiss", "conceal", "get rid of", "put away"),
              frozenset({"hide", "dismiss"})),
    VerbClass("SEARCH", ("search", "look", "find", "google", "hunt", "look up"),
              frozenset({"search", "find"})),
    VerbClass("SNAP", ("snap", "dock", "stick", "pin"),
              frozenset({"snap", "dock"})),
    VerbClass("SELECT", ("select", "highlight", "grab", "mark"),
              frozenset({"select"})),
    VerbClass("DELETE", ("delete", "remove", "erase", "wipe", "clear", "get rid of"),
              frozenset({"delete", "remove", "clear"})),
    VerbClass("PAUSE", ("pause", "freeze", "hold"),
              frozenset({"pause"})),
    VerbClass("RESUME", ("resume", "continue", "unpause", "restart"),
              frozenset({"resume", "continue"})),
    VerbClass("MINIMIZE", ("minimize", "minimise", "shrink", "collapse"),
              frozenset({"minimize"})),
    VerbClass("MAXIMIZE", ("maximize", "maximise", "enlarge", "expand"),
              frozenset({"maximize"})),
    VerbClass("REFRESH", ("refresh", "reload", "update", "rescan"),
              frozenset({"refresh", "reload"})),
    VerbClass("SAVE", ("save", "store", "keep"),
              frozenset({"save"})),
    VerbClass("GO", ("go", "navigate", "head", "jump"),
              frozenset({"go", "navigate"})),
    VerbClass("READ", ("read", "read out", "say", "speak"),
              frozenset({"read"})),
    VerbClass("RECORD", ("record", "capture", "film"),
              frozenset({"record", "capture"})),
    VerbClass("TOGGLE", ("toggle", "flip", "switch"),
              frozenset({"toggle"})),
    VerbClass("MUTE", ("mute", "silence", "quiet"),
              frozenset({"mute"})),
    VerbClass("LIST", ("list", "enumerate", "tell me"),
              frozenset({"list"})),
    VerbClass("DICTATE_TO", ("tell", "message", "send", "text", "ping", "write"),
              frozenset({"tell", "message", "send"})),
)

#: Later alias words that may be said differently (one-way: alias word -> spoken).
OBJECT_SYNONYMS = {
    "monitor": ("screen", "display"), "screen": ("monitor", "display"), "display": ("screen", "monitor"),
    "app": ("application", "program"), "window": ("app",), "little": ("bit", "tad", "touch"),
    "fast": ("quickly", "quick", "lot"), "settings": ("preferences", "options", "config"),
    "folder": ("directory",), "tab": ("page",), "top": ("beginning",), "bottom": ("end",),
    "note": ("notes",), "tasks": ("todos", "chores"), "task": ("todo", "chore"),
    "reminder": ("alert",), "reminders": ("alerts",), "song": ("track", "tune"), "track": ("song", "tune"),
    "music": ("song", "songs", "tunes"), "memo": ("note",), "config": ("configuration", "settings"),
    "computer": ("pc", "machine"), "everything": ("all",), "all": ("everything",),
    "picture": ("image", "photo"), "screenshot": ("snapshot", "screen grab", "screengrab"),
    "microphone": ("mic",), "mic": ("microphone",), "lights": ("light", "lamps"), "labels": ("tags",),
    "numbers": ("labels", "tags"), "desktop": ("desk",), "far": ("very", "all the way"),
    "main": ("primary",), "middle": ("center", "centre"), "calculator": ("calc",),
    "explorer": ("browser",), "zoom": ("magnification",), "layout": ("arrangement",),
}

#: Words that may sit between template words without changing the meaning.
OPTIONAL_WORDS = frozenset({"the", "a", "an", "my", "this", "that", "some", "over", "on", "into", "onto",
                            "current", "active", "up", "please", "for"})

#: Leading words dropped before a free-text slot ("remind me to ... that ...").
TEXT_LEADERS = ("saying", "that", "about", "to say")

P_SYNONYM = 0.03
P_STEM = 0.02
P_CONFUSION = 0.05
P_TEXT_SLOT = 0.05
P_RAW_APP = 0.10
P_UNKNOWN_APP = 0.30
P_MISSING_REQUIRED = 0.30
GRAMMAR_BASE = 0.95
RULE_CONFIDENCE = 0.92
MAX_APP_TOKENS = 4
CHAIN_WORDS = (("and", "then"), ("then",), ("and",), ("after", "that"))


@dataclass(frozen=True)
class Parse:
    canonical_id: str
    args: dict
    confidence: float
    span: tuple                               # (start, end) token indices
    source: str = "grammar"                   # grammar | rule
    alias: str = ""
    word_penalty: float = 0.0                 # look-alike / synonym cost of the command words alone


@dataclass(frozen=True)
class Unresolved:
    """A slot heard but not resolvable without runtime context ("the other one")."""
    text: str


def _stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") else tok


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@dataclass
class Template:
    index: int
    canonical_id: str
    alias: tuple
    args: tuple
    risk: str


@dataclass
class Grammar:
    records: list
    app_names: tuple = ()
    reserved: frozenset = frozenset()
    templates: list = field(default_factory=list)
    _head: dict = field(default_factory=dict)
    _synonym_heads: dict = field(default_factory=dict)
    _ids: frozenset = frozenset()

    def __post_init__(self):
        verb_classes = {}
        for vc in VERB_CLASSES:
            for verb in vc.verbs:
                verb_classes.setdefault(verb, []).append(vc)
        self._verb_classes = verb_classes
        ids = set()
        for n, rec in enumerate(self.records):
            ids.add(rec["canonical_id"])
            args = tuple((a["name"], a["type"], bool(a["required"])) for a in rec.get("args", ()))
            for alias in rec.get("aliases", ()):
                if alias in self.reserved:
                    continue            # whole-utterance control words: exact tier only
                toks = tuple(alias.split())
                if not toks:
                    continue
                t = Template(len(self.templates), rec["canonical_id"], toks, args, rec.get("risk", "ui"))
                self.templates.append(t)
                head = toks[0]
                for form in {head, nz.confusion_key(head), _stem(head)}:
                    self._head.setdefault(form, []).append(t)
                for vc in verb_classes.get(head, ()):
                    for syn in vc.synonyms:
                        self._synonym_heads.setdefault(syn, []).append(t)
        self._ids = frozenset(ids)

    # -- token matching ----------------------------------------------------

    def _match_head(self, u: list, i: int, word: str) -> list:
        """[(tokens consumed, penalty)] ways u[i:] can say the alias verb `word`."""
        out = []
        if i >= len(u):
            return out
        tok = u[i]
        if tok == word:
            out.append((1, 0.0))
        elif _stem(tok) == _stem(word):
            out.append((1, P_STEM))
        elif nz.confusion_key(tok) == nz.confusion_key(word):
            out.append((1, P_CONFUSION))
        for vc in self._verb_classes.get(word, ()):
            for syn in vc.synonyms:
                st = syn.split()
                if st != [word] and u[i:i + len(st)] == st:
                    out.append((len(st), P_SYNONYM))
        return out

    @staticmethod
    def _match_word(tok: str, word: str) -> Optional[float]:
        if tok == word:
            return 0.0
        if _stem(tok) == _stem(word):
            return P_STEM
        if nz.confusion_key(tok) == nz.confusion_key(word):
            return P_CONFUSION
        if tok in OBJECT_SYNONYMS.get(word, ()):
            return P_SYNONYM
        return None

    def _match_alias(self, u: list, start: int, t: Template):
        """-> [(end index, penalty)] for the alias tokens of t starting at u[start]."""
        results = []
        for used, pen in self._match_head(u, start, t.alias[0]):
            i, j, penalty, ok = start + used, 1, pen, True
            while j < len(t.alias):
                if i >= len(u):
                    ok = False
                    break
                p = self._match_word(u[i], t.alias[j])
                if p is not None:
                    penalty += p
                    i += 1
                    j += 1
                elif u[i] in OPTIONAL_WORDS:
                    i += 1
                else:
                    ok = False
                    break
            if ok:
                results.append((i, penalty))
        return results

    # -- slots -------------------------------------------------------------

    def _app(self, toks: list):
        """-> (value, penalty) or None."""
        while toks and toks[0] in ("the", "my", "to", "on", "over"):
            toks = toks[1:]
        if not toks or len(toks) > MAX_APP_TOKENS:
            return None
        text = " ".join(toks)
        if _is_destination(text) or toks[0] in ("and", "then"):
            return None
        if not self.app_names:
            return text, P_RAW_APP
        from samsara.app_index import MATCH_FLOOR, rank_candidates  # noqa: PLC0415
        ranked = rank_candidates(text, list(self.app_names), lambda name: name)
        if ranked and ranked[0][0] >= MATCH_FLOOR:
            score, name = ranked[0]
            return name, (0.0 if score >= 1.0 else P_STEM)
        return text, P_UNKNOWN_APP

    def _slot(self, kind: str, name: str, toks: list):
        """-> (value, penalty) or None when toks do not say this slot."""
        if kind in ("text", "playlist"):
            for lead in TEXT_LEADERS:
                lt = lead.split()
                if toks[:len(lt)] == lt and len(toks) > len(lt):
                    toks = toks[len(lt):]
            return " ".join(toks), P_TEXT_SLOT
        while toks and toks[0] in ("the", "a", "letter", "window") and len(toks) > 1:
            toks = toks[1:]
        if kind == "int":
            if name.endswith("s"):
                numbers = []
                for tok in toks:
                    if tok in ("and", "into", "to"):
                        continue
                    n = nz.parse_number([tok])
                    if n is None:
                        n = nz.parse_ordinal(tok)
                    if n is None:
                        return None
                    numbers.append(n)
                return (numbers, 0.0) if numbers else None
            n = nz.parse_number(toks)
            if n is None and len(toks) == 1:
                n = nz.parse_ordinal(toks[0])
            return (n, 0.0) if n is not None else None
        if kind == "nato_letter":
            letters = nz.parse_letters(toks)
            if not letters:
                return None
            if name.endswith("s"):
                return letters, 0.0
            return (letters[0], 0.0) if len(letters) == 1 else None
        if kind == "app_name":
            return self._app(toks)
        if kind == "monitor":
            return _monitor(toks)
        if kind == "side":
            return _side(toks)
        return " ".join(toks), P_TEXT_SLOT

    def _slots(self, t: Template, rest: list):
        """-> (args, penalty) or None."""
        rest = nz.strip_fillers(rest) if rest else rest
        if rest and all(tok in nz.HESITATIONS or tok in ("please", "now") for tok in rest):
            rest = []
        if not t.args:
            return ({}, 0.0) if not rest else None
        kinds = [k for _n, k, _r in t.args]
        if kinds == ["app_name", "monitor"]:
            return self._send_slots(t, rest)
        if kinds == ["nato_letter", "monitor"]:
            return self._letter_monitor_slots(t, rest)
        if len(t.args) == 1:
            name, kind, required = t.args[0]
            if not rest:
                return ({}, P_MISSING_REQUIRED) if required else ({}, 0.0)
            got = self._slot(kind, name, rest)
            if got is None:
                return None
            return {name: got[0]}, got[1]
        return {t.args[0][0]: " ".join(rest)}, P_TEXT_SLOT

    def _letter_monitor_slots(self, t: Template, rest: list):
        """Parse ``window move B to monitor 2`` into typed label and monitor."""
        (label_arg, _label_kind, _label_required), (monitor_arg, _monitor_kind, monitor_required) = t.args
        if not rest:
            return ({}, P_MISSING_REQUIRED) if not monitor_required else None
        for cut in range(1, len(rest) + 1):
            label = self._slot("nato_letter", label_arg, rest[:cut])
            if label is None:
                continue
            if cut == len(rest):
                return ({label_arg: label[0]}, label[1]) if not monitor_required else None
            monitor = self._slot("monitor", monitor_arg, rest[cut:])
            if monitor is not None:
                return {label_arg: label[0], monitor_arg: monitor[0]}, label[1] + monitor[1]
        return None

    def _send_slots(self, t: Template, rest: list):
        """windows.send-shaped: optional app + required destination, either order."""
        from plugins.commands.windows import _parse_send_remainder  # noqa: PLC0415
        (app_arg, _k, _r), (mon_arg, _k2, _r2) = t.args
        if not rest:
            return {}, P_MISSING_REQUIRED
        splits = []
        app_text, dest = _parse_send_remainder(" ".join(rest))
        splits.append((app_text.split() if app_text else [], dest.split()))
        if rest[0] in ("to", "onto", "on"):                       # "move to the left screen chrome"
            for cut in range(len(rest) - 1, 1, -1):              # longest destination first
                splits.append((rest[cut:], rest[1:cut]))
        for app_toks, dest_toks in splits:
            mon = _monitor(dest_toks)
            if mon is None:
                continue
            args, penalty = {mon_arg: mon[0]}, mon[1]
            if app_toks and app_toks not in (["this"], ["it"], ["this", "window"], ["that"]):
                app = self._app(app_toks)
                if app is None:
                    continue
                args[app_arg] = app[0]
                penalty += app[1]
            return args, penalty
        return None

    # -- parsing -----------------------------------------------------------

    def candidates(self, u: list, start: int) -> list:
        if start >= len(u):
            return []
        seen, out = set(), []
        heads = [u[start], nz.confusion_key(u[start]), _stem(u[start])]
        for n in (1, 2, 3):
            heads.append(" ".join(u[start:start + n]))
        for h in heads:
            for t in self._head.get(h, []) + self._synonym_heads.get(h, []):
                if t.index not in seen:
                    seen.add(t.index)
                    out.append(t)
        return out

    def parse(self, u: list) -> Optional[Parse]:
        """The best single-command parse of the WHOLE token list, or None."""
        if not u:
            return None
        best = None
        for start in (0, 1):
            if start == 1 and u[0] not in OPTIONAL_WORDS:
                break
            for t in self.candidates(u, start):
                for end, penalty in self._match_alias(u, start, t):
                    slots = self._slots(t, u[end:])
                    if slots is None:
                        continue
                    args, slot_penalty = slots
                    conf = round(GRAMMAR_BASE - penalty - slot_penalty, 4)
                    cand = Parse(t.canonical_id, args, conf, (0, len(u)), "grammar", " ".join(t.alias), penalty)
                    key = (conf, len(t.alias), -t.index)
                    if best is None or key > best[0]:
                        best = (key, cand)
        for rule in RULES:
            got = rule(self, u)
            if got is not None and (best is None or got.confidence >= best[0][0]):   # rules are specific
                best = ((got.confidence, 99, 0), got)
        return best[1] if best else None

    def parse_chain(self, u: list) -> Optional[list]:
        """[Parse, ...]: the whole list as one command, else split on "and" /
        "then" / "and then" / "after that" where EVERY part parses."""
        single = self.parse(u)
        if single is not None:
            return [single]
        for i in range(1, len(u) - 1):
            for words in CHAIN_WORDS:
                n = len(words)
                if tuple(u[i:i + n]) != words:
                    continue
                left = self.parse(u[:i])
                if left is None:
                    continue
                right = self.parse_chain(u[i + n:])
                if right:
                    shift = i + n
                    right = [Parse(p.canonical_id, p.args, p.confidence,
                                   (p.span[0] + shift, p.span[1] + shift), p.source, p.alias, p.word_penalty)
                             for p in right]
                    return [Parse(left.canonical_id, left.args, left.confidence, (0, i), left.source,
                                  left.alias, left.word_penalty)] + right
        return None


# ---------------------------------------------------------------------------
# Destinations (windows.py grammar)
# ---------------------------------------------------------------------------

def _is_destination(text: str) -> bool:
    from plugins.commands.windows import _is_destination_text  # noqa: PLC0415
    return _is_destination_text(text)


def _monitor(toks: list):
    while toks and toks[0] in ("to", "on", "onto"):
        toks = toks[1:]
    if not toks:
        return None
    if toks[-2:] == ["other", "one"] and len(toks) <= 3:
        return Unresolved(" ".join(toks)), 0.0              # which one depends on the cursor
    text = " ".join(toks)
    if _is_destination(text):
        return text, 0.0
    return None


SIDES = ("top left", "top right", "bottom left", "bottom right", "left", "right", "top", "bottom",
         "up", "down", "center", "middle", "full", "maximized")
_SIDE_WORDS = {"upper": "top", "lower": "bottom", "centre": "center", "half": "", "side": "", "of": "",
               "the": "", "screen": "", "to": "", "corner": ""}


def _side(toks: list):
    words = [_SIDE_WORDS.get(t, t) for t in toks]
    text = " ".join(w for w in words if w)
    return (text, 0.0) if text in SIDES else None


# ---------------------------------------------------------------------------
# Rules: families that are not alias-shaped
# ---------------------------------------------------------------------------

_UP = frozenset({"up", "louder", "higher", "increase", "raise", "boost", "more"})
_DOWN = frozenset({"down", "quieter", "softer", "lower", "decrease", "reduce", "less"})
_VOLUME_NOUNS = frozenset({"volume", "sound", "audio"})
_VOLUME_OK = _UP | _DOWN | _VOLUME_NOUNS | frozenset(
    {"turn", "make", "crank", "bump", "pump", "put", "it", "the", "a", "bit", "little", "tad", "way",
     "notch", "some", "things", "everything", "music"})


def rule_volume(g: Grammar, u: list) -> Optional[Parse]:
    if not u or not set(u) <= _VOLUME_OK:
        return None
    up, down = bool(set(u) & _UP), bool(set(u) & _DOWN)
    if up == down:
        return None
    if not (set(u) & _VOLUME_NOUNS or set(u) & {"louder", "quieter", "softer"} or "it" in u):
        return None
    cid = "volume.volume_up" if up else "volume.volume_down"
    return Parse(cid, {}, RULE_CONFIDENCE, (0, len(u)), "rule") if cid in g._ids else None


_DIRECTIONS = {"up": "up", "upward": "up", "upwards": "up", "down": "down", "downward": "down",
               "downwards": "down", "left": "left", "right": "right"}
_AMOUNTS = {"little": "a_little", "bit": "a_little", "tad": "a_little", "touch": "a_little",
            "slightly": "a_little", "smidge": "a_little", "fast": "fast", "quickly": "fast", "quick": "fast",
            "lot": "fast", "far": "fast", "medium": "medium", "moderately": "medium", "high": "high"}
_SCROLL_FILLER = frozenset({"a", "the", "just", "more", "of", "by", "some", "way", "please", "one"})
_ENDS = {"top": "top", "beginning": "top", "start": "top", "bottom": "bottom", "end": "bottom"}


def rule_scroll(g: Grammar, u: list) -> Optional[Parse]:
    if not u or u[0] not in ("scroll", "page", "go", "move", "jump"):
        return None
    words = [w for w in u[1:] if w not in _SCROLL_FILLER]
    if u[0] != "scroll" and not ("page" in u or u[0] == "page"):
        return None
    if "to" in words:
        rest = [w for w in words if w != "to"]
        if len(rest) == 1 and rest[0] in _ENDS:
            cid = f"builtin.scroll_to_{_ENDS[rest[0]]}"
            return Parse(cid, {}, RULE_CONFIDENCE, (0, len(u)), "rule") if cid in g._ids else None
        return None
    dirs = [_DIRECTIONS[w] for w in words if w in _DIRECTIONS]
    amounts = [_AMOUNTS[w] for w in words if w in _AMOUNTS]
    pages = [w for w in words if w == "page"] + (["page"] if u[0] == "page" else [])
    unknown = [w for w in words if w not in _DIRECTIONS and w not in _AMOUNTS and w != "page"]
    if unknown or len(set(dirs)) != 1 or len(set(amounts)) > 1:
        return None
    d = dirs[0]
    if pages:
        cid = f"scroll.page_{d}"
    elif amounts:
        cid = f"scroll.scroll_{d}_{amounts[0]}"
        if cid not in g._ids:
            cid = f"scroll.scroll_{d}"
    else:
        cid = f"scroll.scroll_{d}"
    return Parse(cid, {}, RULE_CONFIDENCE, (0, len(u)), "rule") if cid in g._ids else None


def rule_numbered_tab(g: Grammar, u: list) -> Optional[Parse]:
    words = [w for w in u if w not in ("go", "switch", "jump", "move", "flip", "to", "the", "over", "number")]
    if len(words) != 2:
        return None
    n = None
    if words[0] == "tab":
        n = nz.parse_number(words[1:]) or nz.parse_ordinal(words[1])
    elif words[1] == "tab":
        n = nz.parse_ordinal(words[0])
    if not n:
        return None
    cid = f"builtin.tab_{nz.number_to_words(n)}"
    return Parse(cid, {}, RULE_CONFIDENCE, (0, len(u)), "rule") if cid in g._ids else None


_DICTATE_VERBS = frozenset({"tell", "message", "ask", "send", "write", "text", "ping", "dictate", "type"})


def rule_dictate_to_claude(g: Grammar, u: list) -> Optional[Parse]:
    cid = "message_claude.claude_message_prepare"
    if cid not in g._ids or len(u) < 2 or u[0] not in _DICTATE_VERBS or "claude" not in u[:5]:
        return None
    head = u[1:u.index("claude")]
    if any(w not in ("a", "message", "note", "to") for w in head):
        return None
    text = u[u.index("claude") + 1:]
    for lead in TEXT_LEADERS:
        lt = lead.split()
        if text[:len(lt)] == lt:
            text = text[len(lt):]
    args = {"text": " ".join(text)} if text else {}
    return Parse(cid, args, RULE_CONFIDENCE - (P_TEXT_SLOT if text else 0), (0, len(u)), "rule")


RULES = (rule_volume, rule_scroll, rule_numbered_tab, rule_dictate_to_claude)


def verb_class_of(word: str) -> list:
    return [vc.name for vc in VERB_CLASSES if word in vc.verbs]


def all_synonyms() -> Iterable[str]:
    for vc in VERB_CLASSES:
        yield from vc.synonyms
