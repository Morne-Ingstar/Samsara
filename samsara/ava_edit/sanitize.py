"""The gate between what the model said and what may be staged for apply.

Pure functions -- no I/O, no app, no side effects -- so every guard is
testable on its own.

Honest statement of what this can and cannot do: a sanitizer cannot decide
semantically whether a model "edited" or "answered". What it CAN do is
reject the recognisable SHAPES of an answer, and require that the rewrite
is measurably derived from the source. That is what is implemented. Each
guard returns a short reason string (which becomes Proposal.none's reason,
and reaches the user on the chip) or None to pass.

Modelled on samsara/smart_corrections.py's _sanitize_output, which solved
the same problem for a much narrower edit (a correction is near-identity).
An edit legitimately deviates further -- "make it shorter" may halve the
text -- so the thresholds here are looser and the answer-shape guards do
the work that a tight similarity floor does there.
"""

from __future__ import annotations

import difflib
import re

#: G5. difflib ratio over normalised text, below which the rewrite is not
#: recognisably derived from the source. 0.40, not smart_corrections'
#: effective ~0.85: SequenceMatcher.ratio() is 2*M/(len(a)+len(b)), so a
#: rewrite that faithfully keeps HALF the source's characters already scores
#: only 2*0.5/1.5 = 0.67, and one that keeps a third scores 0.50. 0.40 admits
#: an aggressive but honest "make it much shorter" while still rejecting a
#: reply that merely shares a topic with the source.
SIMILARITY_FLOOR = 0.40

#: G7. An edit does not double the text. An answer, an elaboration or a
#: "here are three options" does.
GROWTH_CEILING = 2.0

#: G8. A single-line source rewritten into a multi-line block is a
#: structured answer (a list, a table, a preamble + body), not edited prose.
MAX_INJECTED_NEWLINES = 1

#: G3. The opening frames of a model talking TO you instead of emitting the
#: text. Matched at the very start of the output only, case-insensitively.
_PREAMBLE_RE = re.compile(
    r"^\s*(?:"
    r"sure[,!.]|certainly[,!.]|of course[,!.]|absolutely[,!.]|"
    r"here(?:'s| is| are)\b|"
    r"i(?:'ve| have| will|'ll| can)\b|"
    r"the (?:revised|corrected|edited|updated|rewritten|shortened|new) (?:text|version|sentence)\b|"
    r"(?:revised|corrected|edited|updated|rewritten|result|output|answer)\s*:|"
    r"as an ai\b|"
    r"i'm sorry\b|i am sorry\b|i cannot\b|i can't\b"
    r")",
    re.IGNORECASE,
)

_THINK_OPEN_RE = re.compile(r"<think\b", re.IGNORECASE)

#: G6. A source that ASKS something. If the rewrite stops asking, the model
#: answered the text instead of editing it -- the single crispest signature
#: of the failure this gate exists for, and the one the brief names.
_INTERROGATIVE_OPENERS = frozenset({
    "what", "whats", "why", "how", "when", "where", "who",
    "whose", "which", "is", "are", "was", "were", "do", "does", "did", "can",
    "could", "should", "would", "will", "shall", "am", "have", "has", "may",
})

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace, drop non-word characters. Used only
    for MEASURING similarity -- never for anything that gets applied."""
    return " ".join(_WORD_RE.findall((text or "").lower()))


def similarity(original: str, rewritten: str) -> float:
    """G10's measure: difflib.SequenceMatcher ratio over _norm()'d text, in
    [0.0, 1.0]. Reported so a refusal can say the number it failed on."""
    a, b = _norm(original), _norm(rewritten)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t.rstrip().endswith("?"):
        return True
    words = _norm(t).split()
    return bool(words) and words[0] in _INTERROGATIVE_OPENERS


def clean(raw: str) -> str:
    """Strip the wrappers a compliant model still sometimes adds -- a
    trailing newline, one layer of matching quotes. NOT a guard: this runs
    before the guards and only removes packaging, never content."""
    text = (raw or "").strip()
    if len(text) >= 2:
        # chr() not literals: this repo keeps sources pure ASCII because
        # non-ASCII literals have caused encoding trouble on this machine
        # (same reason session_modes.py builds its chip glyphs with chr()).
        pairs = (('"', '"'), (chr(0x2018), chr(0x2019)), (chr(0x201C), chr(0x201D)))
        for open_c, close_c in pairs:
            if text[0] == open_c and text[-1] == close_c:
                text = text[1:-1].strip()
                break
    return text


def check(raw: str, original: str, instruction: str = "") -> "str | None":
    """Run every guard in order. Returns the reason the rewrite is refused,
    or None when it may be staged.

    The guards, and what each rejects:

      G1 empty            - the model returned nothing usable.
      G2 artifact         - a <think> block or a code fence the SOURCE did
                            not have: the model went off-script. Rejected
                            outright rather than stripped, because stripping
                            can eat real dictated content that happened to
                            sit next to the artifact.
      G3 preamble         - the output OPENS by talking to the user
                            ("Sure,", "Here's the revised text:", "I've
                            shortened it"). The signature of a model that
                            replied rather than emitted prose.
      G4 instruction echo - the rewrite contains the instruction itself; the
                            model repeated the command back.
      G5 not derived      - similarity() below SIMILARITY_FLOOR: the rewrite
                            is not recognisably the source's text.
      G6 answered         - the source asks a question and the rewrite does
                            not. The model answered the text.
      G7 grew             - more than GROWTH_CEILING x the source's word
                            count. Edits do not double text; answers do.
      G8 restructured     - a single-line source came back as a multi-line
                            block: a list or a preamble+body answer.
    """
    if not (raw or "").strip():
        return "empty"

    if _THINK_OPEN_RE.search(raw) and not _THINK_OPEN_RE.search(original):
        return "model went off-script"
    if "```" in raw and "```" not in original:
        return "model went off-script"

    text = clean(raw)
    if not text:
        return "empty"

    if _PREAMBLE_RE.match(text) and not _PREAMBLE_RE.match(original):
        return "model replied instead of editing"

    norm_instruction = _norm(instruction)
    if (norm_instruction and len(norm_instruction) >= 8
            and norm_instruction in _norm(text)
            and norm_instruction not in _norm(original)):
        return "model repeated the instruction"

    ratio = similarity(original, text)
    if ratio < SIMILARITY_FLOOR:
        return f"not derived from the text ({ratio:.2f} < {SIMILARITY_FLOOR:.2f})"

    if _is_question(original) and not _is_question(text):
        return "model answered instead of editing"

    orig_words = original.split()
    new_words = text.split()
    if orig_words and len(new_words) > GROWTH_CEILING * len(orig_words):
        return "rewrite grew too much"

    if original.count("\n") == 0 and text.count("\n") > MAX_INJECTED_NEWLINES:
        return "model returned a structured answer"

    return None
