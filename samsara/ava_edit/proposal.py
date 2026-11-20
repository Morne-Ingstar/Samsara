"""The Proposal value type, the LOCAL diff, and how a diff is read aloud.

The locked design (queue 102): Ava returns the FULL rewritten text and never
an edit command or a character offset. Small models are reliable at rewriting
prose and unreliable at emitting structured edits, so nothing here ever asks
one to. Samsara computes the diff itself, from old text and new text.

One invariant runs through this whole package and is worth stating once:

    THE DIFF IS PRESENTATION. THE REWRITE IS THE PAYLOAD.

What gets applied is always `Proposal.rewritten`, verbatim. The diff is used to
count changes and to tell the user what is about to happen -- never to
reconstruct the result. A diff bug can therefore mis-COUNT or mis-READ, but it
can never corrupt the text.

A SECOND invariant, which is what makes a refusal safe for a careless caller:

    A REFUSAL CARRIES THE ORIGINAL AS ITS REWRITE.

`Proposal.none(...)` sets `rewritten = original`. There is no partial Proposal
by construction, and a caller that ignores `ok` and applies `rewritten` anyway
writes the text back byte-identical. SessionModeManager.apply_edit then also
refuses it ("no change"), so the two guards are independent.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

#: Split into words and the whitespace between them, so "".join(tokens) is
#: exactly the input. Diffing whole words (not characters) keeps the change
#: count meaningful to a human: "three changes" should mean three words or
#: phrases, not thirty-one characters.
_TOKEN_RE = re.compile(r"(\s+)")
_SPOKEN_WORDS = re.compile(r"[^\W_]+")
_PUNCTUATION_NAMES = {
    ".": "period", ",": "comma", "?": "question mark", "!": "exclamation mark",
    ":": "colon", ";": "semicolon", "'": "apostrophe", '"': "quotation mark",
    "\u2019": "curly apostrophe", "\u2018": "opening quotation mark",
    "\u201c": "opening quotation mark", "\u201d": "closing quotation mark",
    "-": "hyphen", "\u2013": "en dash", "\u2014": "em dash",
    "\u2026": "ellipsis", "(": "opening parenthesis", ")": "closing parenthesis",
}

REPLACE, INSERT, DELETE = "replace", "insert", "delete"

#: How many changes describe() will actually recite before it summarises.
#: Being read twelve changes is not review, it is noise.
SPEAK_LIMIT = 3


def tokenize(text: str) -> list:
    """Word and whitespace tokens. "".join(tokenize(t)) == t, exactly."""
    return [t for t in _TOKEN_RE.split(text or "") if t != ""]


def _spoken_change(change):
    # PLACEHOLDER COPY (1288)
    old_words = _SPOKEN_WORDS.findall(change.old)
    new_words = _SPOKEN_WORDS.findall(change.new)
    if [word.lower() for word in old_words] == [word.lower() for word in new_words]:
        old_marks, new_marks = (
            [char for char in text if unicodedata.category(char).startswith("P")]
            for text in (change.old, change.new))
        descriptions = []
        for verb, marks in (("removed", Counter(old_marks) - Counter(new_marks)),
                            ("added", Counter(new_marks) - Counter(old_marks))):
            for mark, count in marks.items():
                name = _PUNCTUATION_NAMES.get(mark, "punctuation mark")
                plural = "ellipses" if name == "ellipsis" else f"{name}s"
                descriptions.append(f"{verb} {count} {plural}" if count > 1 else f"{verb} a {name}")
        if old_marks and old_marks == new_marks and old_words == new_words and not descriptions:
            descriptions.append("moved punctuation")
        if old_words != new_words:
            changed_case = [new for old, new in zip(old_words, new_words) if old != new]
            details = f' of "{changed_case[0]}"' if len(changed_case) == 1 else ""
            descriptions.append(f"corrected capitalization{details}")
        if descriptions:
            return " and ".join(descriptions)
    if old_words and len(old_words) == len(new_words):
        from samsara.ava_edit.sanitize import _same_sound
        replacements = [(old, new) for old, new in zip(old_words, new_words)
                        if old.lower() != new.lower()]
        if replacements and all(_same_sound(old.lower(), new.lower()) for old, new in replacements):
            return " and ".join(f'changed the spelling of "{old}" to {" ".join(new)}'
                                for old, new in replacements)
    return change.describe()


@dataclass(frozen=True)
class Change:
    """One human-sized edit. `old` and `new` are the joined word runs."""

    kind: str
    old: str
    new: str

    def describe(self) -> str:
        if self.kind == INSERT:
            return f'add "{self.new}"'
        if self.kind == DELETE:
            return f'remove "{self.old}"'
        return f'"{self.old}" becomes "{self.new}"'


def diff_changes(original: str, rewritten: str) -> tuple:
    """The word-level diff from `original` to `rewritten`, in document order.

    Pure and local. Never calls a model, never raises on odd input. This is
    the function __init__'s module note points at when it says Samsara
    computes the diff itself.
    """
    a, b = tokenize(original), tokenize(rewritten)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old = "".join(a[i1:i2]).strip()
        new = "".join(b[j1:j2]).strip()
        if not old and not new:
            # Whitespace-only churn: real for the string, not a change a
            # person would count or want read out.
            continue
        if tag == "insert" or not old:
            changes.append(Change(INSERT, "", new))
        elif tag == "delete" or not new:
            changes.append(Change(DELETE, old, ""))
        else:
            changes.append(Change(REPLACE, old, new))
    return tuple(changes)


@dataclass(frozen=True)
class Proposal:
    """A staged rewrite. Nothing here has touched the user's text.

    `ok` False means nothing may be applied and `reason` says why. Build one
    only through `build()` or `none()` -- the constructor is not the API, and
    going around it is how a zero-change proposal would reach a chip.
    """

    ok: bool
    original: str
    rewritten: str
    instruction: str = ""
    changes: tuple = ()
    reason: str = ""

    # -- construction --------------------------------------------------------

    @classmethod
    def none(cls, reason: str, *, original: str = "", instruction: str = "") -> "Proposal":
        """A refusal. `rewritten` is the ORIGINAL, not "" -- see the module
        docstring: a refusal that is applied anyway must be a no-op, not an
        erasure."""
        text = original if isinstance(original, str) else ""
        return cls(ok=False, original=text, rewritten=text,
                   instruction=instruction or "", changes=(), reason=reason or "")

    @classmethod
    def build(cls, original: str, rewritten: str, *, instruction: str = "") -> "Proposal":
        """An accepted rewrite, with the diff computed locally.

        Returns a REFUSAL, not an ok proposal, when the diff is empty -- the
        texts are identical, or they differ only in whitespace. Both are
        invisible to the user, and staging one would put "0 changes" on a
        pending chip that apply_edit would then refuse anyway. Guarding here
        rather than at the call site means no caller can stage an empty edit.
        """
        source = original if isinstance(original, str) else ""
        result = rewritten if isinstance(rewritten, str) else ""
        changes = diff_changes(source, result)
        if not changes:
            return cls.none("that would not change anything",
                            original=source, instruction=instruction)
        return cls(ok=True, original=source, rewritten=result,
                   instruction=instruction or "", changes=changes, reason="")

    # -- what the surfaces read ----------------------------------------------

    @property
    def change_count(self) -> int:
        return len(self.changes)

    def chip_label(self) -> str:
        """What the pending outcome chip says. Short, and it names the word
        that resolves it -- the user must never have to guess the verb.

        Deliberately byte-identical to session_modes.outcome_chip's
        "edit_proposed" label, so the chip says the same thing whichever of
        the two routes drew it.
        """
        if not self.ok:
            return "no edit"
        n = self.change_count
        unit = "change" if n == 1 else "changes"
        return f"proposed edit: {n} {unit} - say apply"

    def describe(self) -> str:
        """The diff as something Ava can READ ALOUD.

        This is the surface that matters for the user this app is for: a
        motor-impaired user may not be looking at the chip, and hearing the
        diff is the only review channel that needs neither a mouse nor a
        steady gaze. Long diffs are summarised rather than recited.

        A refusal says so plainly and says that nothing moved, because that
        is the fact the user needs to be sure of.
        """
        if not self.ok:
            reason = self.reason or "I could not make that edit"
            return f"I did not change anything: {reason}."
        n = self.change_count
        unit = "change" if n == 1 else "changes"
        spoken = ", ".join(_spoken_change(change) for change in self.changes[:SPEAK_LIMIT])
        if n > SPEAK_LIMIT:
            spoken += f", and {n - SPEAK_LIMIT} more"
        return f"{n} {unit}. {spoken}. Say apply to make it so."
