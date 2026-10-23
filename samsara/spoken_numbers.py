"""Whole-number parsing for commands, independent of dictation formatting.

Homophones are opt-in at callers that already accepted them.  Text extraction
is separate from whole-input parsing so prose cannot become a bare choice.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from samsara.number_format import SMALL, TEENS_AND_TENS

NUMBER_WORDS = {**SMALL, **TEENS_AND_TENS}
ORDINAL_WORDS = dict(zip(
    ("first second third fourth fifth sixth seventh eighth ninth tenth eleventh "
     "twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth "
     "nineteenth twentieth").split(), range(1, 21)))
ORDINAL_WORDS.update({"thirtieth": 30, "fortieth": 40, "fiftieth": 50,
                      "sixtieth": 60, "seventieth": 70, "eightieth": 80,
                      "ninetieth": 90, "hundredth": 100})


def parse_spoken_number(text: str, *, homophones: Mapping[str, int] | None = None) -> int | None:
    """Parse digits, cardinal words or ordinals; reject surrounding junk."""
    normalized = " ".join((text or "").lower().strip().split())
    if re.fullmatch(r"[0-9]+(?:st|nd|rd|th)?", normalized):
        return int(re.sub(r"(?:st|nd|rd|th)$", "", normalized))
    if homophones and normalized in homophones:
        return homophones[normalized]
    parts = normalized.replace("-", " ").split()
    if not parts:
        return None
    if len(parts) == 1:
        return NUMBER_WORDS.get(parts[0], ORDINAL_WORDS.get(parts[0]))
    if len(parts) == 2:
        tens = NUMBER_WORDS.get(parts[0], -1)
        ones = NUMBER_WORDS.get(parts[1], ORDINAL_WORDS.get(parts[1], -1))
        if tens >= 20 and tens % 10 == 0 and 1 <= ones <= 9:
            return tens + ones
    # Hundreds and thousands compose only with a smaller valid remainder.
    for scale_word, scale in (("thousand", 1000), ("hundred", 100)):
        if parts.count(scale_word) == 1:
            pivot = parts.index(scale_word)
            head = parse_spoken_number(" ".join(parts[:pivot]))
            tail = parts[pivot + 1:]
            if tail[:1] == ["and"]:
                tail = tail[1:]
                if not tail:
                    return None
            rest = parse_spoken_number(" ".join(tail)) if tail else 0
            if head is not None and 1 <= head < (10 if scale == 100 else 1000) and rest is not None and rest < scale:
                return head * scale + rest
    return None


# Longest valid phrase at each position, with word boundaries (never "someone").
_ATOM = r"(?:[0-9]+(?:st|nd|rd|th)?|" + "|".join(
    sorted((*NUMBER_WORDS, *ORDINAL_WORDS, "hundred", "thousand"), key=len, reverse=True)) + r")"
_RUN = re.compile(r"(?<!\w)" + _ATOM + r"(?:(?:[ -]+|\s+and\s+)" + _ATOM + r")*(?!\w)", re.I)


def spoken_number_matches(text: str):
    """Yield (start, end, value) for numbers inside a command remainder."""
    for match in _RUN.finditer(text or ""):
        tokens = list(re.finditer(_ATOM, match.group(), re.I))
        i = 0
        while i < len(tokens):
            for j in range(len(tokens), i, -1):
                start, end = tokens[i].start(), tokens[j - 1].end()
                value = parse_spoken_number(match.group()[start:end])
                if value is not None:
                    yield match.start() + start, match.start() + end, value
                    i = j
                    break
            else:
                i += 1


def number_words_to_digits(text: str) -> str:
    """Normalize numeric spans in an already selected command argument."""
    result, previous = [], 0
    for start, end, number in spoken_number_matches(text):
        source = text[start:end]
        result.extend((text[previous:start], source if source.isascii() and source.isdigit() else str(number)))
        previous = end
    return "".join((*result, text[previous:]))
