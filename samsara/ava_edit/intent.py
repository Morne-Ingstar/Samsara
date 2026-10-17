"""How the user asks for an edit, and how they apply one.

Selection requests match the whole utterance after leading fillers, in Ava
or command mode. Explicit selection words use the teaching vocabulary. Bare
references prefer the staged draft and otherwise select the capture path.
Other staged edit instructions retain detect_stage_reference's existing rule:
an opening edit verb plus a reference to the staged dictation.

Requiring both is what keeps this off ordinary Ava turns: "what did I just
say" refers to the text but is not an edit; "make me a coffee" opens with an
edit verb but refers to nothing.

"apply" is whole-utterance only, like every other control word in this
codebase (is_scratch_that, classify_reply, the switch words). "I'll apply
for the job" is dictation, not a command, and a quoted "apply" is payload.
"""

from __future__ import annotations

import re

from samsara.session_modes import detect_stage_reference

#: Opening verbs that mean "change the text I just dictated". Single-verb
#: only -- v1 does not do compound multi-step instructions (see the prompt's
#: explicit exclusions), and this list is the shape of that limit.
EDIT_VERBS = frozenset({
    "make", "fix", "change", "rewrite", "reword", "rephrase", "shorten",
    "lengthen", "correct", "tidy", "clean", "capitalise", "capitalize",
    "simplify", "formalise", "formalize", "punctuate", "trim", "tighten",
    "polish", "expand", "edit", "reformat",
})

#: Stripped before the first word is read, so "um, make that shorter" and
#: "ava, fix that" both open with their verb. Same family of fillers
#: session_modes strips for its own whole-utterance matching.
_LEADING_FILLERS = frozenset({
    "um", "uh", "er", "ah", "so", "ok", "okay", "now", "please", "ava",
    "hey", "and", "then",
})

#: The whole utterance that applies a staged proposal. "apply" alone is the
#: word the chip names; the longer forms are what people actually say.
APPLY_UTTERANCES = frozenset({
    "apply", "apply it", "apply that", "apply the edit", "apply the change",
    "apply the changes", "do it", "make it so",
})

#: The whole utterance that throws a staged proposal away. "scratch that"
#: is deliberately NOT here: while a proposal is staged, "scratch that"
#: still means the session's scratch-that, and nothing has been applied for
#: it to undo -- so discarding must have its own words.
DISCARD_UTTERANCES = frozenset({
    "no", "nope", "no thanks", "never mind", "nevermind", "cancel",
    "discard", "forget it", "leave it", "don't", "do not",
})

#: chr(), not literals: this repo keeps sources pure ASCII because
#: non-ASCII literals have caused encoding trouble on this machine
#: (same reason session_modes.py builds its chip glyphs with chr()).
_QUOTES = "\"`" + "".join(chr(c) for c in
                          (0x2018, 0x2019, 0x201C, 0x201D, 0x00AB, 0x00BB))
_TRIM = ".,!?;: \t\r\n"
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split()).strip(_TRIM)


def _is_quoted(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return any(q in raw for q in _QUOTES) or raw[:1] == "'" or raw[-1:] == "'"


def _first_word(text: str) -> str:
    for word in _WORD_RE.findall((text or "").lower()):
        if word not in _LEADING_FILLERS:
            return word
    return ""


def is_edit_request(text: str) -> bool:
    """True when this Ava utterance asks for an edit to the staged text.

    Both conditions, never one: an edit verb in first position (after
    fillers) AND session_modes' own stage-reference test.
    """
    if not (text or "").strip():
        return False
    return selection_request(text) is not None or (
        _first_word(text) in EDIT_VERBS and detect_stage_reference(text))


def selection_phrases() -> dict[str, tuple[str, ...]]:
    """Whole requests grouped by instruction; reuse the teaching vocabulary."""
    from samsara.teach_patterns import _SOURCE_KEYWORD_TO_KIND

    sources = [key for key, kind in _SOURCE_KEYWORD_TO_KIND.items()
               if kind == "selection"]
    groups = {}
    for verb in sorted(EDIT_VERBS):
        explicit = [f"{verb} {article}{source}{tail}"
                    for source in sources for article in ("", "the ")
                    for tail in ("", " up") if not tail or verb in {"clean", "tidy", "fix"}]
        if verb in {"clean", "tidy"}:
            explicit += [f"{verb} up {article}{source}"
                         for source in sources for article in ("", "the ")]
        groups[f"{verb} selection"] = tuple(explicit)
        bare = [f"{verb} {ref}{tail}" for ref in ("this", "that", "it")
                for tail in ("", " up") if not tail or verb in {"clean", "tidy", "fix"}]
        if verb in {"clean", "tidy"}:
            bare += [f"{verb} up {ref}" for ref in ("this", "that", "it")]
            bare += [f"{verb} up"]
        groups[f"{verb} this"] = tuple(bare)
    return groups


def selection_request(text: str) -> "str | None":
    """Explicit selection or bare referent, with no trailing payload."""
    instruction = selection_instruction(text)
    if instruction is not None:
        return "selection" if instruction.endswith(" selection") else "bare"
    return None


def selection_instruction(text: str) -> "str | None":
    """Canonical instruction for a whole request, after leading fillers."""
    if _is_quoted(text):
        return None
    words = _normalize(text).split()
    while words and words[0].strip(_TRIM) in _LEADING_FILLERS:
        words.pop(0)
    whole = " ".join(words)
    for instruction, phrases in selection_phrases().items():
        if whole in phrases:
            return instruction
    return None


def is_apply_utterance(text: str) -> bool:
    """Whole-utterance only, and never inside quotation marks."""
    if _is_quoted(text):
        return False
    return _normalize(text) in APPLY_UTTERANCES


def is_discard_utterance(text: str) -> bool:
    """Whole-utterance only, and never inside quotation marks."""
    if _is_quoted(text):
        return False
    return _normalize(text) in DISCARD_UTTERANCES
