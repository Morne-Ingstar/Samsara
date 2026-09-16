"""Spoken number words -> digits, only where the words are being used as numbers.

The `format_numbers` setting (default on) used to rewrite EVERY standalone
number word: "like the one in it's" was delivered as "like the 1 in it's", and
the owner had been retyping every prose "one" by hand (queue 55, 2026-09-14).
It also rebuilt the text with ' '.join(text.split()), collapsing line breaks.

The rule now follows ordinary writing style (AP / Chicago spell out zero to
nine in prose) and converts only in a number context:

  * ten and above (ten .. ninety, "twenty one" / "twenty-one") -> digits;
  * zero .. nine -> digits only when
      - a number cue comes right before it ("page one" -> "page 1",
        "volume two", "step three", "number four", "version five"), or
      - it is part of a run with another number word or a digit
        ("one two three" -> "1 2 3", "five 5" -> "5 5"), or
      - "percent" follows it ("three percent" -> "3 percent");
  * hundred / thousand / million / billion -> digits only right after another
    number word or digit ("two hundred" -> "2 100" as before); alone ("a
    hundred times", "a million reasons") they stay words.

The ambiguous middle -- a bare small number before a noun ("one thing", "two
options", "the top three") -- stays a word: a wrong digit in prose is the bug
this module exists to fix, and a wanted digit is one "page"/"number" cue away.
Whitespace and punctuation between words are preserved exactly.
"""
from __future__ import annotations

import re

SMALL = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4,
    'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
}
TEENS_AND_TENS = {
    'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19,
    'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
    'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
}
MULTIPLIERS = {
    'hundred': 100, 'thousand': 1000, 'million': 1000000, 'billion': 1000000000,
}
_TENS = ('twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety')
_ONES = ('one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine')

#: A small number right after one of these is a number, not a word.
NUMBER_CUES = frozenset({
    # Deliberately NOT "no": "no one came" is prose.
    'page', 'pages', 'volume', 'vol', 'chapter', 'section', 'step', 'steps', 'number',
    'version', 'level', 'item', 'line', 'lines', 'room', 'floor', 'question',
    'figure', 'fig', 'table', 'episode', 'season', 'track', 'verse', 'grade', 'gate',
    'platform', 'route', 'channel', 'apartment', 'unit', 'suite', 'round', 'part',
    'act', 'scene', 'rule', 'option', 'slide', 'row', 'column', 'window', 'tab',
    'player', 'team', 'phase', 'stage', 'tier', 'size', 'port', 'build', 'issue',
})
_FOLLOWING_CUES = frozenset({'percent', 'percentage'})

_COMPOUND_RE = re.compile(
    r'\b(' + '|'.join(_TENS) + r')[\s-](' + '|'.join(_ONES) + r')\b', re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def _kind(word: str):
    w = word.lower()
    if w.isdigit():
        return 'digit'
    if w in SMALL:
        return 'small'
    if w in TEENS_AND_TENS:
        return 'large'
    if w in MULTIPLIERS:
        return 'multiplier'
    return None


def _value(word: str) -> str:
    w = word.lower()
    for table in (SMALL, TEENS_AND_TENS, MULTIPLIERS):
        if w in table:
            return str(table[w])
    return word


def format_spoken_numbers(text: str) -> str:
    """Apply the number-context rule above. Pure; never changes non-number text."""
    if not text:
        return text
    tens = {w: v for w, v in TEENS_AND_TENS.items() if w in _TENS}
    text = _COMPOUND_RE.sub(lambda m: str(tens[m.group(1).lower()] + SMALL[m.group(2).lower()]), text)

    tokens = list(_TOKEN_RE.finditer(text))
    kinds = [_kind(m.group(0)) for m in tokens]

    def joined(i: int, j: int) -> bool:
        """True when tokens i and j are separated only by spaces or a hyphen
        (a comma or full stop between them ends a run)."""
        gap = text[tokens[i].end():tokens[j].start()]
        return gap.strip(' -') == ''

    out, last = [], 0
    for i, m in enumerate(tokens):
        kind = kinds[i]
        if kind is None or kind == 'digit':
            continue
        prev_num = i > 0 and kinds[i - 1] is not None and joined(i - 1, i)
        next_num = (i + 1 < len(tokens) and kinds[i + 1] is not None
                    and kinds[i + 1] != 'multiplier' and joined(i, i + 1))
        if kind == 'large':
            convert = True
        elif kind == 'multiplier':
            convert = prev_num
        else:  # small
            prev_word = tokens[i - 1].group(0).lower() if i > 0 and joined(i - 1, i) else ''
            next_word = tokens[i + 1].group(0).lower() if i + 1 < len(tokens) and joined(i, i + 1) else ''
            next_multiplier = i + 1 < len(tokens) and kinds[i + 1] == 'multiplier' and joined(i, i + 1)
            convert = (prev_num or next_num or next_multiplier
                       or prev_word in NUMBER_CUES or next_word in _FOLLOWING_CUES)
        if convert:
            out.append(text[last:m.start()])
            out.append(_value(m.group(0)))
            last = m.end()
    out.append(text[last:])
    return ''.join(out)
