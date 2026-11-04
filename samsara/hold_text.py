"""Held-final text guards; never infer missing letters or untimed duplicates."""
from copy import copy
import math
import re


_WORD = re.compile(r"\w+(?:['’]\w+)*")
_LEAD = re.compile(r'\A[ \t]*(?:[.,]+[ \t]+)+')
_DOTS = re.compile(r'[ \t]+(?:\.[ \t]+){2,}\.[ \t]*\Z')
_SPOKEN_DOTS = re.compile(r'\bdot(?:[ \t]+dot){2,}\b', re.IGNORECASE)


def _tokens(segments):
    """Require text/word alignment; segment timestamps alone prove no word."""
    result = []
    for index, segment in enumerate(segments):
        text = list(_WORD.finditer(segment.text))
        words = getattr(segment, 'words', None) or ()
        if len(words) != len(text):
            return []
        for position, (token, word) in enumerate(zip(text, words)):
            value = _WORD.fullmatch(word.word.strip().strip('.,!?;:()"'))
            a, b = getattr(word, 'start', None), getattr(word, 'end', None)
            if (value is None or value.group().casefold() != token.group().casefold()
                    or a is None or b is None or not math.isfinite(a) or not math.isfinite(b)
                    or not 0 <= a < b):
                return []
            result.append((token.group().casefold(), a, b, index, position, token.start()))
    return result


def append_chunk(kept, segments, previous=(), shared=None):
    """Trim a suffix/prefix only when timed words identify the same PCM.

    Both occurrences must be in the actual shared interval AND intersect in
    source time. Distinct repetitions, missing word timing, and segments in
    one chunk are retained. Matching never scans the full accepted hold.
    """
    if not shared or shared[0] >= shared[1] or not previous or not segments:
        kept.extend(segments)
        return
    left, right = _tokens(previous), _tokens(segments)
    matched = 0
    for width in range(1, min(len(left), len(right), 32) + 1):
        pairs = zip(left[-width:], right[:width])
        if all(a[0] == b[0]
               and shared[0] <= (a[1] + a[2]) / 2 <= shared[1]
               and shared[0] <= (b[1] + b[2]) / 2 <= shared[1]
               and max(a[1], b[1], shared[0]) < min(a[2], b[2], shared[1])
               for a, b in pairs):
            matched = width
    if not matched:
        kept.extend(segments)
    elif matched < len(right):
        _, _, _, index, word_index, offset = right[matched]
        item = copy(segments[index])
        item.text = ' ' + item.text[offset:]
        item.words = item.words[word_index:]
        item.start, item.end = item.words[0].start, item.words[-1].end
        kept.extend([item, *segments[index + 1:]])


def guard_segments(app, segments):
    """Guard prose edges before cleanup/formatting, preserving every letter.

    These are delivered-text invariants, not a diagnosis of decoder output.
    Verbatim and explicit spoken punctuation remain under their own policy.
    Never turn a decoder's 'Nd' into 'And' without acoustic evidence.
    """
    literal = getattr(app, '_verbatim_rule', None)
    if (app.config.get('cleanup_mode', 'clean') == 'verbatim'
            or (callable(literal) and literal())):
        return segments
    text = ''.join(s.text for s in segments)
    lead = _LEAD.match(text)
    dots = None if _SPOKEN_DOTS.search(text) else _DOTS.search(text)
    if lead is None and dots is None:
        return segments
    start, end = lead.end() if lead else 0, dots.start() if dots else len(text)
    # Slice only punctuation/space at the two edges; keep lexical content and
    # untouched segment telemetry instead of collapsing the whole hold.
    result, origin = [], 0
    for segment in segments:
        a, b = max(0, start - origin), min(len(segment.text), end - origin)
        origin += len(segment.text)
        if b <= a:
            continue
        value = segment.text[a:b]
        if value != segment.text:
            item = copy(segment)
            item.text, item.words = value, None
        else:
            item = segment
        result.append(item)
    if dots and result:
        item = copy(result[-1])
        item.text = item.text.rstrip() + '.'
        item.words = None
        result[-1] = item
    return result
