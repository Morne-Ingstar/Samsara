"""Pure written-form and split/join variants for local correction proposals.

This module deliberately contains only finite data and pure functions.  It
does not inspect the correction store or make a decision about which proposal
is correct.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


Span = tuple[int, int]


# These are deliberately finite and conservative.  The first spelling is the
# spaced form used in the proposal, the second is the joined form.
SPLIT_JOIN_PAIRS: tuple[tuple[str, str], ...] = (
    ("a lot", "alot"),
    ("in to", "into"),
    ("can not", "cannot"),
    ("some time", "sometime"),
    ("every day", "everyday"),
)


_APOSTROPHE_FORMS: Mapping[str, tuple[str, ...]] = {
    "theyre": ("they're",),
    "they're": ("theyre",),
    "youre": ("you're",),
    "you're": ("youre",),
    "its": ("it's",),
    "it's": ("its",),
    "whos": ("who's",),
    "who's": ("whos",),
    "werent": ("weren't",),
    "weren't": ("werent",),
}


_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = {
    20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
    60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety",
}
_NUMBER_WORDS = {word: value for value, word in enumerate(_ONES)}
_NUMBER_WORDS.update({word: value for value, word in _TENS.items()})

_INTEGER_RE = re.compile(r"(?P<sign>[+-]?)(?P<digits>\d+)")
_WORD_INTEGER_RE = re.compile(r"(?P<sign>[+-]?)(?P<words>[a-z]+(?:[ -][a-z]+)?)", re.IGNORECASE)


def _number_words(value: int) -> str:
    if value < 20:
        return _ONES[value]
    tens, ones = divmod(value, 10)
    base = _TENS[tens * 10]
    return base if not ones else f"{base} {_ONES[ones]}"


def _parse_word_integer(words: str) -> int | None:
    normalized = " ".join(words.casefold().replace("-", " ").split())
    parts = normalized.split()
    if not parts or len(parts) > 2 or any(part not in _NUMBER_WORDS for part in parts):
        return None
    if len(parts) == 1:
        return _NUMBER_WORDS[parts[0]]
    tens, ones = (_NUMBER_WORDS[part] for part in parts)
    if tens < 20 or tens % 10 or ones > 9:
        return None
    value = tens + ones
    return value if value <= 99 else None


def integer_variants(text: str) -> tuple[str, ...]:
    """Return finite spelled/digit alternatives for one integer.

    Dates, decimals, surrounding prose, and values outside 0--99 do not
    match.  A leading ``+`` or ``-`` is retained on every returned spelling.
    """
    source = " ".join(str(text or "").strip().split())
    match = _INTEGER_RE.fullmatch(source)
    if match:
        value = int(match.group("digits"))
        if value > 99:
            return ()
        sign = match.group("sign")
        return (f"{sign}{_number_words(value)}",)

    match = _WORD_INTEGER_RE.fullmatch(source.casefold())
    if not match:
        return ()
    value = _parse_word_integer(match.group("words"))
    if value is None:
        return ()
    sign = match.group("sign")
    digit = f"{sign}{value}"
    return (digit,)


def case_variants(text: str) -> tuple[str, ...]:
    """Return the finite lower/title/upper spellings of *text*."""
    if not text:
        return ()
    return tuple(dict.fromkeys((text.lower(), text.title(), text.upper())))


def apostrophe_variants(text: str) -> tuple[str, ...]:
    """Return canonical apostrophe/no-apostrophe alternatives for *text*."""
    return _APOSTROPHE_FORMS.get(str(text or "").casefold(), ())


def written_variants(text: str) -> tuple[str, ...]:
    """Return all finite written-form variants, deterministically ordered."""
    variants = set(case_variants(text))
    variants.update(apostrophe_variants(text))
    variants.update(integer_variants(text))
    return tuple(sorted(variants, key=lambda value: (value.casefold(), value)))


def _token_text_and_span(token) -> tuple[str, Span | None]:
    """Accept the small token records used by the caller at the seam."""
    if token is None:
        return "", None
    if isinstance(token, Mapping):
        value = token.get("text", token.get("token", ""))
        token_span = token.get("span")
        if token_span is None and "start" in token and "end" in token:
            token_span = (token["start"], token["end"])
        return str(value or ""), tuple(token_span) if token_span is not None else None
    if isinstance(token, Sequence) and not isinstance(token, (str, bytes)):
        if len(token) >= 2 and isinstance(token[1], Sequence):
            return str(token[0] or ""), tuple(token[1])
        if len(token) >= 3:
            return str(token[0] or ""), (int(token[1]), int(token[2]))
    return str(token), None


def _adjacent(adjacent_tokens, side: str):
    if isinstance(adjacent_tokens, Mapping):
        return adjacent_tokens.get(side)
    if isinstance(adjacent_tokens, Sequence) and not isinstance(adjacent_tokens, (str, bytes)):
        # The public shorthand is (left, right).
        return adjacent_tokens[0 if side == "left" else 1] if len(adjacent_tokens) > (0 if side == "left" else 1) else None
    return None


def split_join_variants(
    text: str,
    span: Span,
    adjacent_tokens=None,
) -> tuple[tuple[str, Span], ...]:
    """Return known split/join alternatives and their complete spans.

    When only one word is selected, an adjacent token must carry its exact
    span (``{"text": ..., "span": (start, end)}``, or an equivalent tuple)
    so a two-word proposal can highlight both words safely.
    """
    source = " ".join(str(text or "").split()).casefold()
    result: list[tuple[str, Span]] = []
    for split, joined in SPLIT_JOIN_PAIRS:
        if source == split:
            result.append((joined, span))
        elif source == joined:
            result.append((split, span))

    left_text, left_span = _token_text_and_span(_adjacent(adjacent_tokens, "left"))
    right_text, right_span = _token_text_and_span(_adjacent(adjacent_tokens, "right"))
    if left_span is not None and left_text and right_span is not None and right_text:
        combined = f"{left_text} {right_text}".casefold()
        for split, joined in SPLIT_JOIN_PAIRS:
            if combined == split and source in {left_text.casefold(), right_text.casefold()}:
                result.append((joined, (min(left_span[0], right_span[0]), max(left_span[1], right_span[1]))))

    if right_span is not None and right_text:
        combined = f"{text} {right_text}".casefold()
        for split, joined in SPLIT_JOIN_PAIRS:
            if combined == split:
                result.append((joined, (span[0], right_span[1])))
    if left_span is not None and left_text:
        combined = f"{left_text} {text}".casefold()
        for split, joined in SPLIT_JOIN_PAIRS:
            if combined == split:
                result.append((joined, (left_span[0], span[1])))
    return tuple(dict.fromkeys(result))


__all__ = [
    "SPLIT_JOIN_PAIRS",
    "apostrophe_variants",
    "case_variants",
    "integer_variants",
    "split_join_variants",
    "written_variants",
]
