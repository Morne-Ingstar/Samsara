"""Parser for spoken letter-spelling sequences ("M O R N E", "em oh are en
ee", "capital M O R N E", "M as in Mike, O, R, N, E").

Pure, no app/I-O dependency -- exists so samsara/teach_patterns.py's voice-
teaching flow has a SPELLING-TRUTH source that does not depend on Whisper
getting a whole word right, only on it getting individual LETTER NAMES
right (or a disambiguation word right, which is usually a much easier
transcription target than a bare letter).

Why this exists (see samsara/teach_patterns.py's module docstring for the
full anti-bootstrap rationale): a taught word captured from the SAME ASR
being taught can silently store a misheard spelling. Letter-by-letter
spelling, cross-checked against a curated homophone map, is one of three
truth channels (the others are buffer-sourced correction targets and
selection/clipboard text grabs) that make persisted spellings trustworthy
independent of whatever the whole-word transcription said.

Tolerance strategy: Whisper renders spoken letter NAMES inconsistently
("M" sometimes transcribes as "em", "R" as "are", "U" as "you", "W" as
"double u", etc.) -- this is a rendering-of-a-letter-NAME problem, not a
spelling-of-a-WORD problem, so a small closed homophone map per letter
handles it completely; no fuzzy matching needed here (unlike the buffer-
target resolution in teach_patterns.py, which DOES need fuzzy matching
because whole-word transcriptions vary in open-ended ways).
"""
import re
import unicodedata

# ── Letter homophone map ────────────────────────────────────────────────────
#
# Each entry: the set of tokens (lowercase, already stripped of punctuation)
# that should resolve to that letter. Includes the bare letter itself (a
# single-char token), the letter's NATO-ish spoken name, and the specific
# Whisper mis-transcriptions observed/expected for that name. Multi-word
# names (e.g. "double u") are matched as token PAIRS before falling back to
# single-token lookup -- see _MULTI_TOKEN_LETTERS below.

_LETTER_HOMOPHONES = {
    'a': {'a', 'ay', 'eh'},
    'b': {'b', 'bee', 'be'},
    'c': {'c', 'see', 'sea', 'si'},
    'd': {'d', 'dee'},
    'e': {'e', 'ee'},
    'f': {'f', 'eff', 'ef'},
    'g': {'g', 'gee', 'jee'},
    'h': {'h', 'aitch', 'haitch'},
    'i': {'i', 'eye', 'aye'},
    'j': {'j', 'jay'},
    'k': {'k', 'kay'},
    'l': {'l', 'el', 'ell'},
    'm': {'m', 'em'},
    'n': {'n', 'en'},
    'o': {'o', 'oh'},
    'p': {'p', 'pee', 'pea'},
    'q': {'q', 'cue', 'queue'},
    'r': {'r', 'are', 'ar'},
    's': {'s', 'ess', 'es'},
    't': {'t', 'tee', 'tea'},
    'u': {'u', 'you', 'yoo', 'yew'},
    'v': {'v', 'vee'},
    'w': {'w'},  # 'double u' handled as a multi-token entry below
    'x': {'x', 'ex', 'ecks'},
    'y': {'y', 'why'},
    'z': {'z', 'zee', 'zed'},
}

# Multi-word letter names, longest-token-count first so a greedy scan tries
# the longer phrase before falling back to shorter/single-token lookups.
_MULTI_TOKEN_LETTERS = [
    (('double', 'u'), 'w'),
    (('double-u',), 'w'),  # Whisper sometimes renders it hyphenated as one token
    (('doubleu',), 'w'),
]

# Reverse index: token -> letter, built once from the single-token sets.
_TOKEN_TO_LETTER = {
    token: letter
    for letter, tokens in _LETTER_HOMOPHONES.items()
    for token in tokens
}

_PUNCTUATION_WORDS = {
    'hyphen': '-',
    'dash': '-',
    'apostrophe': "'",
    'space': ' ',
}

_CAPITAL_MARKERS = {'capital', 'cap', 'uppercase', 'upper'}

_AS_IN_START = ('as',)  # "as in <word>" -- see _consume_as_in below


def _strip_token(tok: str) -> str:
    """Lowercase, NFC-normalize, strip surrounding punctuation Whisper
    tends to attach to a lone spoken letter (periods, commas)."""
    tok = unicodedata.normalize('NFC', tok).strip().lower()
    return tok.strip('.,;:!?')


def _tokenize(text: str) -> "list[str]":
    # Commas are deliberately kept as word-separators (not folded into a
    # token) so "M, O, R" and "M O R" parse identically -- spoken letter
    # lists are read with or without pause-commas depending on how the ASR
    # rendered pauses, and neither form should behave differently.
    raw = text.replace(',', ' ').split()
    return [_strip_token(t) for t in raw if _strip_token(t)]


def _consume_as_in(tokens: "list[str]", i: int) -> "tuple[str | None, int]":
    """If tokens[i:] starts with "as in <word>", return (word, new_index)
    consuming through <word>; else (None, i).

    <word> is taken as the SINGLE next token after "in" -- disambiguation
    phrases are one word ("Mike", "Sam", "Tango"), not phrases, by
    convention in every spelling-alphabet style this mirrors.
    """
    if i < len(tokens) and tokens[i] == 'as':
        j = i + 1
        if j < len(tokens) and tokens[j] == 'in':
            k = j + 1
            if k < len(tokens):
                return tokens[k], k + 1
    return None, i


def parse_letters(text: str) -> "str | None":
    """Parse a spoken letter-spelling sequence into the literal string it
    spells, or None if any token is unrecognized (caller should treat this
    as "spelling failed -- ask the user to spell it again", never guess).

    Handles, in combination:
      - bare letters and their Whisper-homophone renderings
      - "capital <letter>" (forces that one letter uppercase)
      - "<letter> as in <word>" -- the WORD, not the isolated letter
        token, is treated as truth for which letter was meant (a lone
        spoken letter is the hardest thing for Whisper to render
        correctly; a common disambiguation word like "Mike" is not, so
        when both are present the word wins outright rather than being
        used merely to cross-check).
      - hyphen / apostrophe / space as literal punctuation
      - multi-word letter names ("double u" -> w)

    Casing policy: if the utterance never says "capital" at all, the
    assembled result is auto-title-cased (first letter of each space-
    separated word capitalized, rest lowercase) -- natural spelling of a
    proper noun ("M O R N E" -> "Morne") without requiring "capital" on
    every single letter, which nobody actually says. If "capital" is used
    ANYWHERE in the utterance, casing is taken literally exactly as
    spelled (capital only where marked) and no auto-title-casing is
    applied -- the speaker has demonstrated they're being deliberate about
    case, so respect that entirely rather than second-guessing it.
    """
    if not text:
        return None
    tokens = _tokenize(text)
    if not tokens:
        return None

    out_chars: "list[str]" = []
    any_explicit_capital = False
    i = 0
    n = len(tokens)

    while i < n:
        tok = tokens[i]

        # Multi-token letter names first (longest match wins).
        matched_multi = False
        for name_tokens, letter in _MULTI_TOKEN_LETTERS:
            span = len(name_tokens)
            if tuple(tokens[i:i + span]) == name_tokens:
                out_chars.append(letter)
                i += span
                matched_multi = True
                break
        if matched_multi:
            continue

        if tok in _CAPITAL_MARKERS:
            i += 1
            if i >= n:
                return None  # "capital" with nothing after it -- malformed
            letter_tok = tokens[i]
            word, new_i = _consume_as_in(tokens, i + 1)
            if word is not None:
                if not word or not word[0].isalpha():
                    return None
                out_chars.append(word[0].upper())
                any_explicit_capital = True
                i = new_i
                continue
            letter = _TOKEN_TO_LETTER.get(letter_tok)
            if letter is None:
                return None
            out_chars.append(letter.upper())
            any_explicit_capital = True
            i += 1
            continue

        if tok in _PUNCTUATION_WORDS:
            out_chars.append(_PUNCTUATION_WORDS[tok])
            i += 1
            continue

        # Plain letter token -- check for a trailing "as in <word>" that
        # should override it before falling back to the homophone map.
        word, new_i = _consume_as_in(tokens, i + 1)
        if word is not None:
            if not word or not word[0].isalpha():
                return None
            out_chars.append(word[0].lower())
            i = new_i
            continue

        letter = _TOKEN_TO_LETTER.get(tok)
        if letter is None:
            return None
        out_chars.append(letter)
        i += 1

    if not out_chars:
        return None

    result = ''.join(out_chars)

    if not any_explicit_capital:
        # Title-case at space AND hyphen boundaries -- a spelled sequence
        # can contain spoken "space" (a two-word vocabulary phrase) or
        # "hyphen" (a compound word like "data-lake"), and both read
        # naturally with each segment capitalized ("Data Lake",
        # "Data-Lake"). Apostrophe is deliberately NOT a capitalize
        # boundary here: "don't" is far more common in this app's usage
        # than an "O'Brien"-style name, and guessing wrong on an
        # apostrophe is worse than leaving it lowercase -- a user who
        # wants "O'Brien" cased exactly can say "capital B" explicitly,
        # which is fully deterministic already.
        result = re.sub(
            r'(^|[ -])([a-z])',
            lambda m: m.group(1) + m.group(2).upper(),
            result,
        )

    return result
