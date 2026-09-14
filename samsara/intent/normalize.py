"""Pure text normalisation for the intent grammar.

Every function here is pure (same input, same output, no I/O). Tokens are the
registry's own matching view (command_catalog.normalize_phrase ->
command_registry.view_tokens): lowercase, non-word characters removed per
token -- so a normalised utterance compares directly with catalog aliases.

Reused tables, never copied (see docs/INTENT_GRAMMAR.md "Reuse"):
  numbers     plugins/commands/show_numbers._WORD_TO_NUM / _parse_spoken_number
  ordinals    plugins/commands/windows._ORDINALS
  letters     plugins/commands/window_switcher.PHONETIC / _parse_letters
  confusions  samsara/command_catalog.WHISPER_CONFUSIONS (+ EXTRA_CONFUSIONS)
The plugin modules are imported lazily: in the app (and in the catalog
loader) they are already loaded, so the import is a dictionary lookup.
"""
from __future__ import annotations

from functools import lru_cache

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def tokens(text: str) -> list:
    """The registry's matching view of text as a token list."""
    from samsara.command_catalog import normalize_phrase  # noqa: PLC0415
    return normalize_phrase(text or "").split()


# ---------------------------------------------------------------------------
# Fillers and politeness
# ---------------------------------------------------------------------------

#: Stripped repeatedly from the START of an utterance, longest first.
LEADING_FILLERS = (
    "would you mind", "would you please", "could you please", "can you please",
    "could you just", "can you just", "would you just", "will you please",
    "i want you to", "i need you to", "i would like you to", "id like you to",
    "go ahead and", "be a dear and", "do me a favor and", "do me a favour and",
    "could you", "can you", "would you", "will you",
    "hey samsara", "okay samsara", "ok samsara",
    "please", "kindly", "hey", "hi", "yo", "um", "uh", "umm", "uhh", "er", "erm", "hmm",
    "so", "okay", "ok", "alright", "right so", "just", "now",
)

#: Stripped repeatedly from the END of an utterance, longest first.
TRAILING_FILLERS = (
    "if you dont mind", "if you do not mind", "if you can", "if you could",
    "for me please", "for me", "right now", "real quick", "thank you", "thanks",
    "please", "now", "okay", "ok",
)

#: Trailing fillers whose first word can also be the command's last word
#: ("snap right now"): filler_variants() tries the utterance both ways.
AMBIGUOUS_TRAILING = ("right now",)

#: Pure hesitations removed wherever they occur.
HESITATIONS = frozenset({"um", "uh", "umm", "uhh", "er", "erm", "hmm"})


def _phrase_tuples(phrases) -> tuple:
    return tuple(sorted((tuple(p.split()) for p in phrases), key=len, reverse=True))


_LEADING = _phrase_tuples(LEADING_FILLERS)
_TRAILING = _phrase_tuples(TRAILING_FILLERS)


def strip_fillers(toks: list, keep: tuple = ()) -> list:
    """Tokens without hesitations, leading fillers/politeness and trailing
    politeness (trailing phrases in `keep` are left alone). Never strips an
    utterance to nothing: if only fillers were said, the original tokens come
    back (a bare "please" is not a command, but it is not silence either)."""
    trailing = tuple(p for p in _TRAILING if " ".join(p) not in keep)
    out = [t for t in toks if t not in HESITATIONS]
    changed = True
    while changed and out:
        changed = False
        for phrase in _LEADING:
            n = len(phrase)
            if len(out) > n and tuple(out[:n]) == phrase:
                out = out[n:]
                changed = True
                break
        for phrase in trailing:
            n = len(phrase)
            if len(out) > n and tuple(out[-n:]) == phrase:
                out = out[:-n]
                changed = True
                break
    return out or list(toks)


def filler_variants(toks: list) -> list:
    """Distinct stripped readings, least stripped first: the ambiguous
    trailing phrases kept, then everything stripped."""
    out = []
    for variant in (strip_fillers(toks, keep=AMBIGUOUS_TRAILING), strip_fillers(toks)):
        if variant not in out:
            out.append(variant)
    return out


# ---------------------------------------------------------------------------
# Whisper confusions
# ---------------------------------------------------------------------------

#: Look-alike groups beyond command_catalog.WHISPER_CONFUSIONS (09a). Extend
#: from the correction dictionary later; order inside a group does not matter.
EXTRA_CONFUSIONS = (
    ("tab", "tap", "tabs"), ("close", "clothes", "clothe"), ("write", "right", "rite", "wright"),
    ("mute", "moot", "mutes"), ("pause", "paws", "pores", "pours"), ("next", "necks"),
    ("scroll", "scrole", "stroll"), ("cursor", "curser"), ("maximize", "maximise"),
    ("minimize", "minimise"), ("colour", "color"), ("centre", "center"),
    ("won", "one"), ("to", "too", "two"), ("for", "four", "fore"),
    ("snap", "snapp"), ("sight", "site", "cite"), ("mail", "male"), ("pane", "pain"),
    ("reed", "read"), ("seen", "scene"), ("whole", "hole"), ("no", "know"), ("by", "buy", "bye"),
    ("sale", "sail"), ("steal", "steel"), ("week", "weak"), ("hour", "our"), ("cue", "queue"),
    ("volume", "volumes"), ("window", "windo"), ("desk", "disk"),
)


@lru_cache(maxsize=1)
def _confusion_keys() -> dict:
    """token -> group representative, groups merged when they share a token
    (union-find), representative = alphabetically first non-digit member."""
    from samsara.command_catalog import WHISPER_CONFUSIONS  # noqa: PLC0415
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for group in tuple(WHISPER_CONFUSIONS) + EXTRA_CONFUSIONS:
        first = group[0]
        for other in group[1:]:
            ra, rb = find(first), find(other)
            if ra != rb:
                parent[rb] = ra
    members = {}
    for tok in list(parent):
        members.setdefault(find(tok), []).append(tok)
    keys = {}
    for group in members.values():
        words = sorted(t for t in group if not t.isdigit()) or sorted(group)
        for tok in group:
            keys[tok] = words[0]
    return keys


def confusion_key(token: str) -> str:
    """The comparison key of one token: digits read as number words first
    ("4" -> "four"), then its Whisper look-alike group's representative
    ("four", "for", "fore" share one key). Unknown tokens are their own key."""
    word = number_to_words(int(token)) if token.isdigit() and len(token) <= 2 else token
    return _confusion_keys().get(word, word)


def collapse_confusions(toks: list) -> list:
    return [confusion_key(t) for t in toks]


# ---------------------------------------------------------------------------
# Numbers and ordinals (reused from show_numbers / windows)
# ---------------------------------------------------------------------------


def _word_to_num() -> dict:
    from plugins.commands.show_numbers import _WORD_TO_NUM  # noqa: PLC0415
    return _WORD_TO_NUM


@lru_cache(maxsize=1)
def _num_to_word() -> dict:
    return {v: k for k, v in _word_to_num().items()}


def ordinals() -> dict:
    from plugins.commands.windows import _ORDINALS  # noqa: PLC0415
    return _ORDINALS


_ORDINAL_SUFFIX = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6}


def number_to_words(n: int) -> str:
    """0-99 as show_numbers' words ("thirty seven"); other values as digits."""
    table = _num_to_word()
    if n in table:
        return table[n]
    if 20 < n < 100 and n % 10:
        return f"{table[n - n % 10]} {table[n % 10]}"
    return str(n)


def parse_number(toks: list):
    """The number the WHOLE token list says, else None. Accepts an optional
    leading "number"/"no"; digits, words and compounds via
    show_numbers._parse_spoken_number (0-99); won/to/for look-alikes of one,
    two and four. Ordinals are parse_ordinal's job."""
    toks = list(toks)
    if toks and toks[0] in ("number", "no", "num"):
        toks = toks[1:]
    if not toks or len(toks) > 2:
        return None
    words = _word_to_num()
    look_alike = {"won": "one", "to": "two", "too": "two", "for": "four", "fore": "four"}
    fixed = [look_alike.get(t, t) if len(toks) == 1 else t for t in toks]
    if not all(t.isdigit() or t in words for t in fixed):
        return None
    if len(fixed) == 2 and not (fixed[0] in words and words[fixed[0]] >= 20 and words.get(fixed[1], 10) < 10):
        return None
    from plugins.commands.show_numbers import _parse_spoken_number  # noqa: PLC0415
    return _parse_spoken_number(" ".join(fixed))


def parse_ordinal(token: str):
    """first..sixth (windows._ORDINALS) and 1st..6th -> int, else None."""
    return ordinals().get(token) or _ORDINAL_SUFFIX.get(token)


def numerals_to_words(text: str) -> str:
    """ "heading 1" -> "heading one"; "window 37" -> "window thirty seven"."""
    return " ".join(number_to_words(int(t)) if t.isdigit() and len(t) <= 2 else t for t in tokens(text))


def words_to_numerals(text: str) -> str:
    """ "heading one" -> "heading 1"; "thirty seven" -> "37"."""
    words = _word_to_num()
    toks, out, i = tokens(text), [], 0
    while i < len(toks):
        pair = parse_number(toks[i:i + 2]) if i + 1 < len(toks) and toks[i] in words else None
        if pair is not None and words[toks[i]] >= 20 and toks[i + 1] in words:
            out.append(str(pair))
            i += 2
            continue
        out.append(str(words[toks[i]]) if toks[i] in words else toks[i])
        i += 1
    return " ".join(out)


# ---------------------------------------------------------------------------
# Letters (reused from window_switcher)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _phonetic_view() -> dict:
    """window_switcher.PHONETIC keyed by the registry token view ("x-ray" ->
    "xray", "double you" -> "doubleyou" is left multi-token)."""
    from plugins.commands.window_switcher import PHONETIC  # noqa: PLC0415
    return {" ".join(tokens(k)): v for k, v in PHONETIC.items()}


def nato_letter(token: str):
    """One spoken letter token -> "A".."Z" (NATO word, letter name, or the
    bare letter), else None."""
    if len(token) == 1 and token.isalpha():
        return token.upper()
    return _phonetic_view().get(token)


def parse_letters(toks: list):
    """Letters the WHOLE token list spells ("bravo and charlie", "b c",
    "double you") -> ["B", "C"], else None (any token that is not a letter,
    a letter name or a separator makes it not-a-letter-list)."""
    from plugins.commands.window_switcher import _parse_letters  # noqa: PLC0415
    view = _phonetic_view()
    multi = {k for k in view if " " in k}
    i, count = 0, 0
    while i < len(toks):
        pair = " ".join(toks[i:i + 2])
        if pair in multi:
            i, count = i + 2, count + 1
        elif toks[i] in ("and", "then", "with", "plus"):
            i += 1
        elif nato_letter(toks[i]):
            i, count = i + 1, count + 1
        else:
            return None
    if not count:
        return None
    text = " ".join("x-ray" if t == "xray" else t for t in toks)
    letters = _parse_letters(text)
    return letters if len(letters) == count else None


# ---------------------------------------------------------------------------
# The normaliser
# ---------------------------------------------------------------------------


def normalize(text: str, *, collapse: bool = False) -> str:
    """lowercase + registry token view + fillers/politeness stripped; with
    collapse=True every token is replaced by its confusion_key (numbers read
    as words, Whisper look-alikes folded)."""
    toks = strip_fillers(tokens(text))
    return " ".join(collapse_confusions(toks) if collapse else toks)
