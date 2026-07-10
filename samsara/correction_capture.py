"""Extraction of atomic word/phrase substitutions from a user's manual
correction of a dictation, for the correction-capture hotkey feature
(samsara/ui/correction_capture_qt.py).

Tribunal-mandated strictness: only ATOMIC substitutions are ever offered as
learnable corrections-dictionary pairs. Anything that looks like a rewrite,
a pure insertion/deletion, or a case/punctuation-only edit is excluded from
`learnable` and reported in `rejected` with a human-readable reason so the
capture window can show the user why it wasn't offered.

Pure function, no I/O, no persistence -- extraction never writes to the
corrections dictionary. Review-gated persistence (per-pair confirmation)
happens entirely in the UI layer.
"""

import difflib
import re
from dataclasses import dataclass, field

_MAX_PHRASE_WORDS = 4
_DEFAULT_MAX_EDIT_RATIO = 0.5

_PUNCT_STRIP_RE = re.compile(r"[^\w\s']")


@dataclass
class ExtractionResult:
    learnable: list = field(default_factory=list)   # list[(wrong, right)]
    rejected: list = field(default_factory=list)     # list[(wrong, right, reason)]


def _tokenize(text: str) -> list:
    return (text or "").split()


def _is_case_only(a: str, b: str) -> bool:
    return a != b and a.lower() == b.lower()


def _strip_punct(s: str) -> str:
    return _PUNCT_STRIP_RE.sub('', s.lower())


def _is_punctuation_only(a: str, b: str) -> bool:
    stripped_a, stripped_b = _strip_punct(a), _strip_punct(b)
    return a != b and stripped_a == stripped_b and stripped_a != ''


def _spans_from_opcodes(opcodes, orig_words, corr_words) -> list:
    """(tag, wrong_words, right_words) per opcode, tag in
    {'equal', 'replace', 'delete', 'insert'}."""
    spans = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            spans.append(('equal', orig_words[i1:i2], corr_words[j1:j2]))
        elif tag == 'replace':
            spans.append(('replace', orig_words[i1:i2], corr_words[j1:j2]))
        elif tag == 'delete':
            spans.append(('delete', orig_words[i1:i2], []))
        elif tag == 'insert':
            spans.append(('insert', [], corr_words[j1:j2]))
    return spans


def _merge_adjacent_replaces(spans: list) -> list:
    """Collapse a replace / equal(<=1 word) / replace run into a single
    replace span (repeating for longer chains); pass everything else
    through unchanged. A gap of 2+ equal words is left unmerged -- each
    replace stays an independent candidate."""
    merged = []
    i = 0
    n = len(spans)
    while i < n:
        tag, wrong, right = spans[i]
        if tag != 'replace':
            merged.append((tag, wrong, right))
            i += 1
            continue

        acc_wrong, acc_right = list(wrong), list(right)
        j = i + 1
        while (j + 1 < n and spans[j][0] == 'equal' and len(spans[j][1]) <= 1
                and spans[j + 1][0] == 'replace'):
            acc_wrong += spans[j][1]
            acc_right += spans[j][2]
            acc_wrong += spans[j + 1][1]
            acc_right += spans[j + 1][2]
            j += 2
        merged.append(('replace', acc_wrong, acc_right))
        i = j
    return merged


def extract_corrections(
    original: str, corrected: str, max_edit_ratio: float = _DEFAULT_MAX_EDIT_RATIO,
) -> ExtractionResult:
    """Diff `original` against the user's `corrected` text and classify
    each changed region as a learnable atomic substitution or a rejected
    candidate (with a reason). Never raises on empty input."""
    orig_words = _tokenize(original)
    corr_words = _tokenize(corrected)
    result = ExtractionResult()

    if not orig_words and not corr_words:
        return result

    matcher = difflib.SequenceMatcher(a=orig_words, b=corr_words, autojunk=False)
    opcodes = matcher.get_opcodes()

    # Whole-text rewrite gate: if more than max_edit_ratio of the text
    # changed, offer nothing -- every changed span is reported as rejected
    # so the review window can still show the user what was seen.
    edit_ratio = 1.0 - matcher.ratio()
    if edit_ratio > max_edit_ratio:
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == 'equal':
                continue
            wrong = " ".join(orig_words[i1:i2])
            right = " ".join(corr_words[j1:j2])
            result.rejected.append((wrong, right, "looks like a rewrite"))
        return result

    spans = _spans_from_opcodes(opcodes, orig_words, corr_words)
    merged = _merge_adjacent_replaces(spans)

    for tag, wrong_words, right_words in merged:
        if tag == 'equal':
            continue

        wrong = " ".join(wrong_words)
        right = " ".join(right_words)

        if tag == 'insert':
            result.rejected.append((wrong, right, "insertion with no corresponding original text"))
            continue
        if tag == 'delete':
            result.rejected.append((wrong, right, "deletion with no replacement text"))
            continue

        # tag == 'replace'
        if not wrong_words or not right_words:
            result.rejected.append((wrong, right, "insertion/deletion, not a substitution"))
            continue
        if len(wrong_words) > _MAX_PHRASE_WORDS or len(right_words) > _MAX_PHRASE_WORDS:
            result.rejected.append((wrong, right, "too long -- looks like a rewrite"))
            continue
        if _is_case_only(wrong, right):
            result.rejected.append((wrong, right, "case-only difference"))
            continue
        if _is_punctuation_only(wrong, right):
            result.rejected.append((wrong, right, "punctuation-only difference"))
            continue

        result.learnable.append((wrong, right))

    return result
