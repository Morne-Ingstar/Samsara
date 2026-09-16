"""How the user asks for an edit, and how they apply one.

NO new trigger. The entry is the EXISTING Ava address path: the user is
already addressing Ava (hands-free Ava lane or hold-to-talk), and
session_modes.detect_stage_reference -- the function that already decides
whether an Ava utterance is talking about the staged dictation -- decides
whether "that" means the text. An edit request is simply an Ava utterance
that BOTH refers to the staged text AND opens with an edit verb.

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
    return _first_word(text) in EDIT_VERBS and detect_stage_reference(text)


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
