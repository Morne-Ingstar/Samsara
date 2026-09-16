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
from dataclasses import dataclass

#: Split into words and the whitespace between them, so "".join(tokens) is
#: exactly the input. Diffing whole words (not characters) keeps the change
#: count meaningful to a human: "three changes" should mean three words or
#: phrases, not thirty-one characters.
_TOKEN_RE = re.compile(r"(\s+)")

REPLACE, INSERT, DELETE = "replace", "insert", "delete"

#: How many changes describe() will actually recite before it summarises.
#: Being read twelve changes is not review, it is noise.
SPEAK_LIMIT = 3


def tokenize(text: str) -> list:
    """Word and whitespace tokens. "".join(tokenize(t)) == t, exactly."""
    return [t for t in _TOKEN_RE.split(text or "") if t != ""]


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
        if n > SPEAK_LIMIT:
            spoken = "; ".join(c.describe() for c in self.changes[:SPEAK_LIMIT])
            return (f"{n} {unit}: {spoken}; and {n - SPEAK_LIMIT} more. "
                    "Say apply to make it so.")
        spoken = "; ".join(c.describe() for c in self.changes)
        return f"{n} {unit}: {spoken}. Say apply to make it so."
