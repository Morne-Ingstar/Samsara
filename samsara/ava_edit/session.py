"""The staged proposal: one slot, one chip, and the word that applies it.

STAGE, DON'T FIRE. propose() calls the model and puts the result here. It
applies nothing. The text in the user's window is untouched until apply()
runs, and apply() runs only for is_apply_utterance(). Any other utterance
leaves the text exactly as it was -- discard words clear the slot, and
anything else falls through to Ava as an ordinary turn with the proposal
still standing.

ONE slot, module-level, like execution_policy's pending operation: a second
proposal supersedes the first visibly rather than queueing behind it, so a
late "apply" can never reach a proposal the user has stopped looking at.

The chip is the surface. `pending` is the one chip kind with no TTL
(listening_indicator._NO_TTL_KINDS), so a staged proposal stays on screen
until it is answered -- and the indicator renders chip-only even when the
user has the listening pill switched off. That is what makes this usable
without a mouse and without hearing.
"""

from __future__ import annotations

import threading
import time

from samsara.ava_edit import intent, model, pacing, sanitize
from samsara.ava_edit.proposal import Proposal
from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)

#: A staged proposal is a question the user has been asked. It expires so an
#: "apply" said ten minutes later, to something else entirely, cannot land on
#: it. Generous, because reading a diff back to yourself takes time.
PROPOSAL_TTL_S = 120.0

_lock = threading.RLock()
_pending: "PendingProposal | None" = None


class PendingProposal:
    """A proposal that has been shown and not yet answered."""

    __slots__ = ("proposal", "created_at", "_clock")

    def __init__(self, proposal: Proposal, clock=time.monotonic):
        self.proposal = proposal
        self._clock = clock
        self.created_at = clock()

    def expired(self) -> bool:
        return (self._clock() - self.created_at) > PROPOSAL_TTL_S


# ---------------------------------------------------------------------------
# Chip -- the outcome-chip consumer for edit proposals
# ---------------------------------------------------------------------------

def _chip(app, outcome_kind: str, detail: "dict | None" = None) -> None:
    """Show the chip for an edit outcome, never raise.

    The LABELS live in session_modes.outcome_chip, not here. That module is
    the documented source of truth for chip vocabulary (see
    listening_indicator.py's module note and Docs/OUTCOME_CHIP.md), and the
    "edit_proposed" / "edit_applied" / "edit_refused" / "edit_discarded"
    entries are already defined there. Keeping the words in one place is why
    "proposed edit: 3 changes - say apply" reads like every other chip in
    the app instead of like a second vocabulary bolted on beside it.

    The chip is pushed DIRECTLY through app._show_outcome_chip rather than by
    returning a DispatchOutcome, because this path hangs off the Ava agent
    dispatch fn -- downstream of dispatch_utterance, which is the only thing
    that turns outcomes into chips. execution_policy._chip does exactly the
    same thing for the same reason.

    TTL comes from chip_ttl_ms(), so "edit_proposed" inherits pending's
    no-TTL rule: a staged proposal stays on screen until it is answered.
    """
    show = getattr(app, "_show_outcome_chip", None)
    if show is None:
        return
    try:
        from samsara.session_modes import chip_ttl_ms, outcome_chip  # noqa: PLC0415
        chip = outcome_chip(outcome_kind, detail or {})
        if chip is None:
            return
        label, chip_kind = chip
        show(label, chip_kind, chip_ttl_ms(outcome_kind, chip_kind))
    except Exception as exc:
        logger.debug(f"[AVA-EDIT] chip failed: {exc}")


def _say(app, text: str) -> None:
    """Speak, never raise. This is where the DIFF is read out -- see the
    module note in __init__ about why the diff goes to the ear."""
    speak = getattr(app, "_speak_session_notice", None)
    if speak is None:
        return
    try:
        speak(text)
    except Exception as exc:
        logger.debug(f"[AVA-EDIT] spoken diff failed: {exc}")


# ---------------------------------------------------------------------------
# The slot
# ---------------------------------------------------------------------------

def pending() -> "PendingProposal | None":
    """The live proposal, or None. An expired one is dropped here rather
    than being allowed to answer a much later 'apply'."""
    global _pending
    with _lock:
        if _pending is not None and _pending.expired():
            logger.info("[AVA-EDIT] staged proposal expired unanswered")
            _pending = None
        return _pending


def clear() -> None:
    global _pending
    with _lock:
        _pending = None


def _stage(proposal: Proposal) -> PendingProposal:
    global _pending
    with _lock:
        _pending = PendingProposal(proposal)
        return _pending


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------

def enabled(app) -> bool:
    """ava_edit.enabled. True by default: this is the flagship, and it can
    only ever STAGE a proposal on its own -- the word "apply" is what makes
    anything happen. Off means an edit request is an ordinary Ava turn again,
    with no chip and no staged slot."""
    from samsara import config_defaults

    cfg = getattr(app, "config", None) or {}
    section = cfg.get("ava_edit")
    if not isinstance(section, dict):
        return bool(config_defaults.DEFAULTS["ava_edit.enabled"])
    return bool(section.get(
        "enabled", config_defaults.DEFAULTS["ava_edit.enabled"]))


def source_text(app) -> str:
    """The text an edit would edit: the most recent committed dictation
    chunk. Empty string when there is nothing -- which is what makes the
    whole path unreachable with no session buffer (see is_available)."""
    manager = getattr(app, "_session_mode_manager", None)
    if manager is None:
        return ""
    try:
        item = manager.top_dictation_chunk()
    except Exception as exc:
        logger.debug(f"[AVA-EDIT] could not read the session buffer: {exc}")
        return ""
    return item.payload if item is not None else ""


def is_available(app) -> bool:
    """False when nothing in this path is reachable: no session buffer means
    no text to edit, so an edit request is not an edit request at all and
    must fall through to Ava untouched."""
    return bool(source_text(app))


def propose(text: str, instruction: str, app) -> Proposal:
    """Ask the model for the full rewrite, compute the diff locally, and gate
    the result through the sanitizer.

    Blocking (one model call). Returns a Proposal that is either ok, or
    Proposal.none(reason) -- and none() carries rewritten == original, so a
    failure cannot become a partial edit even if the caller ignores `ok`.
    """
    original = text or ""
    if not original.strip():
        return Proposal.none("there is nothing to edit", original=original,
                             instruction=instruction)
    if not (instruction or "").strip():
        return Proposal.none("no instruction", original=original, instruction=instruction)

    raw, reason = model.rewrite(original, instruction, app)
    if raw is None:
        return Proposal.none(reason, original=original, instruction=instruction)

    refusal = sanitize.check(raw, original, instruction)
    if refusal is not None:
        logger.info(f"[AVA-EDIT] rewrite refused by the sanitizer: {refusal}")
        return Proposal.none(refusal, original=original, instruction=instruction)

    return Proposal.build(original, sanitize.clean(raw), instruction=instruction)


def stage(app, proposal: Proposal) -> bool:
    """Show the proposal and leave it standing. Applies NOTHING.

    Returns True when a proposal is now staged and waiting for the word.
    """
    if not proposal.ok:
        _chip(app, "edit_refused", {"reason": proposal.reason})
        _say(app, proposal.describe())
        logger.info(f"[AVA-EDIT] not staged: {proposal.reason}")
        return False
    _stage(proposal)
    _chip(app, "edit_proposed", {"changes": proposal.change_count})
    _say(app, proposal.describe())
    logger.info(f"[AVA-EDIT] staged: {proposal.change_count} change(s), awaiting apply")
    return True


def propose_and_stage(app, instruction: str) -> bool:
    """The whole propose half, for a caller that has only the instruction.
    Blocking -- run it off the utterance thread."""
    original = source_text(app)
    if not original:
        return False
    _chip(app, "edit_thinking")
    return stage(app, propose(original, instruction, app))


# ---------------------------------------------------------------------------
# Apply -- only on the word
# ---------------------------------------------------------------------------

def apply(app) -> dict:
    """Apply the staged proposal through the scratch-that stack, at the
    configured pacing, as one undoable unit.

    Returns SessionModeManager.apply_edit's result dict, or a same-shaped
    refusal. The slot is cleared either way: a proposal is answered once.
    """
    staged = pending()
    if staged is None:
        _chip(app, "edit_refused", {"reason": "nothing staged"})
        return {"applied": False, "reason": "nothing staged", "chars": 0}
    clear()

    manager = getattr(app, "_session_mode_manager", None)
    if manager is None:
        _chip(app, "edit_refused", {"reason": "nothing staged"})
        return {"applied": False, "reason": "no session", "chars": 0}

    proposal = staged.proposal
    # The text must still be the text we proposed against. Between the
    # proposal and the word, the user may have dictated more, scratched, or
    # had an edit applied -- and rewriting a DIFFERENT chunk with this
    # rewrite would be the worst failure this feature could have.
    if source_text(app) != proposal.original:
        _chip(app, "edit_refused", {"reason": "the text changed"})
        _say(app, "The text changed, so I did not apply that edit.")
        logger.info("[AVA-EDIT] apply refused: the session buffer moved under the proposal")
        return {"applied": False, "reason": "the text changed", "chars": 0}

    _wire_raw_injector(app, manager)
    pace = pacing.resolve(app)
    result = manager.apply_edit(
        proposal.rewritten,
        before_delete_s=pace.before_delete_s,
        after_delete_s=pace.after_delete_s,
    )
    if result.get("applied"):
        n = proposal.change_count
        _chip(app, "edit_applied", {"changes": n})
        _log_history(app, proposal)
    else:
        _chip(app, "edit_refused", {"reason": result.get("reason")})
    return result


def discard(app) -> bool:
    """Throw the staged proposal away. Nothing was applied, so there is
    nothing to undo -- the text is byte-identical to before the proposal."""
    if pending() is None:
        return False
    clear()
    _chip(app, "edit_discarded")
    logger.info("[AVA-EDIT] staged proposal discarded")
    return True


def _wire_raw_injector(app, manager) -> None:
    """Give the manager the RAW delivery callable the apply and undo paths
    need. _paste_preserving_clipboard is what dictation's own _inject_fn
    calls once it has finished formatting -- taking it directly is what keeps
    the rewrite, and the restored original, byte-exact."""
    paste = getattr(app, "_paste_preserving_clipboard", None)
    if paste is None:
        return
    try:
        manager.set_raw_inject_fn(lambda text: paste(text))
    except Exception as exc:
        logger.debug(f"[AVA-EDIT] could not wire the raw injector: {exc}")


def _log_history(app, proposal: Proposal) -> None:
    """Record the applied edit. The history store is the right home for
    this -- a persistent log of what the app did -- even though it is the
    wrong source to edit FROM (it carries no window identity)."""
    store = getattr(app, "history_db", None)
    if store is None:
        return
    try:
        store.add(raw_text=proposal.original, display_text=proposal.rewritten,
                  entry_type="edit")
    except Exception as exc:
        logger.debug(f"[AVA-EDIT] history append failed: {exc}")


# ---------------------------------------------------------------------------
# The one entry point the dispatch call site uses
# ---------------------------------------------------------------------------

def handle_utterance(app, text: str) -> bool:
    """Claim this Ava utterance if it belongs to the edit path.

    Returns True when the edit path consumed it (so the caller must NOT hand
    it to the agent), False to let it go to Ava exactly as before.

    Order matters. An answer to a standing proposal is read FIRST, because
    while a proposal is staged those words mean nothing else. Only then is a
    fresh edit request considered.
    """
    try:
        if not (text or "").strip():
            return False
        if not enabled(app):
            return False

        if pending() is not None:
            if intent.is_apply_utterance(text):
                apply(app)
                return True
            if intent.is_discard_utterance(text):
                discard(app)
                return True
            # Anything else: the proposal stands, the text is untouched, and
            # the utterance goes to Ava. Not claimed.
            return False

        if intent.is_edit_request(text) and is_available(app):
            _propose_off_thread(app, text)
            return True
    except Exception as exc:
        # This sits in front of the agent dispatch path. A bug here must
        # cost the user an edit, never their Ava turn.
        logger.warning(f"[AVA-EDIT] edit path error, falling through to Ava: {exc}")
        return False
    return False


def _propose_off_thread(app, instruction: str) -> None:
    """The model call blocks; the utterance thread must not. Daemon thread,
    same fire-and-forget posture the Ava agent dispatch itself uses."""
    def _run():
        try:
            propose_and_stage(app, instruction)
        except Exception as exc:
            logger.warning(f"[AVA-EDIT] proposal failed: {exc}")
            _chip(app, "edit_refused", {"reason": "it failed"})

    thread_registry.spawn("ava-edit-propose", _run, daemon=True)
