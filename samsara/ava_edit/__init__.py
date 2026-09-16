"""Ava gets hands, phase 1: propose an edit, show it, apply only on the word.

The locked design (ARC tribunal, July -- do not redesign):

  * Ava returns the FULL rewritten text. She never emits an edit command and
    never emits a character offset: small models are reliable at rewriting
    and unreliable at both of those. See model.SYSTEM_PROMPT.
  * Samsara computes the diff LOCALLY, from old text and new text, and
    choreographs the edits itself. See proposal.diff_changes.
  * STAGE, DON'T FIRE. The proposal is shown and nothing is applied until
    the user says "apply". Any other utterance leaves the text untouched. A
    refused or malformed proposal returns the text unchanged -- never
    partially applied. See session.handle_utterance and Proposal.none.
  * Apply is ATOMIC through the scratch-that stack: one unit of work, one
    chip, one undo. See SessionModeManager.apply_edit / _undo_edit_apply.
  * Choreography is PRESENTATION ONLY -- the `ava_edit.demo_pacing` config
    key is instant for daily use and cinematic for recording, and cannot
    change what is applied. See pacing.py, which is built so it structurally
    cannot.

Scope of v1, deliberately: single-verb edits over the session buffer -- the
most recent committed dictation chunk. NOT in scope: selection grab (editing
text Samsara did not dictate), whole-field UIA editing, and compound
multi-step instructions.

WHERE THE DIFF IS SHOWN. Two places, both usable with no mouse:

  1. The outcome chip, as a `pending` chip -- the one kind with no TTL
     (listening_indicator._NO_TTL_KINDS), so it stays until answered instead
     of vanishing after 1.8 s. It carries the count and the word:
     "proposed edit: 3 changes -- say apply". The listening indicator renders
     chip-only even when the user has the pill turned off, so this reaches a
     user who has disabled the overlay.
  2. Spoken, through the session's existing notice channel, as
     Proposal.describe(): "3 changes: 'colour' to 'color'; remove 'very';
     ... Say apply to make it so."

The chip is a glance and the voice is the detail. A panel was NOT added: it
would need a mouse to dismiss, it would cover the text being discussed, and
the ONE prerequisite that makes reading the diff aloud safe is already in
place -- wake_consumer's Ava-session half-duplex guard means the app is
fully deaf while it speaks, so it cannot transcribe its own reading of the
diff back as the next utterance and trigger its own edit loop.

Entry point for the dispatch call site: session.handle_utterance(app, text).
"""

from samsara.ava_edit.intent import (
    is_apply_utterance,
    is_discard_utterance,
    is_edit_request,
)
from samsara.ava_edit.pacing import CINEMATIC, INSTANT, Pacing
from samsara.ava_edit.proposal import Change, Proposal, diff_changes
from samsara.ava_edit.session import (
    apply,
    clear,
    discard,
    handle_utterance,
    is_available,
    pending,
    propose,
    propose_and_stage,
    source_text,
    stage,
)

__all__ = [
    "Change",
    "CINEMATIC",
    "INSTANT",
    "Pacing",
    "Proposal",
    "apply",
    "clear",
    "diff_changes",
    "discard",
    "handle_utterance",
    "is_apply_utterance",
    "is_available",
    "is_discard_utterance",
    "is_edit_request",
    "pending",
    "propose",
    "propose_and_stage",
    "source_text",
    "stage",
]
