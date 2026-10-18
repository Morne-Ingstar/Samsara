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

    __slots__ = ("proposal", "created_at", "_clock", "selection_target", "selection_mode", "draft_target")

    def __init__(self, proposal: Proposal, clock=time.monotonic, selection_target=None,
                 selection_mode="replace"):
        self.proposal = proposal
        self._clock = clock
        self.created_at = clock()
        self.selection_target = selection_target
        self.selection_mode = selection_mode
        self.draft_target = None

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
    from samsara.ui import qt_runtime
    if qt_runtime.is_alive():
        from samsara.ui.live_surface_qt import sync_cleanup_buttons
        qt_runtime.post(lambda: sync_cleanup_buttons(app))
    show = getattr(app, "_show_outcome_chip", None)
    if show is None:
        return
    try:
        from samsara.session_modes import chip_ttl_ms, outcome_chip  # noqa: PLC0415
        chip = outcome_chip(outcome_kind, detail or {})
        if chip is None:
            return
        label, chip_kind = chip
        staged = pending()
        if outcome_kind == "edit_proposed" and staged and staged.draft_target is not None:
            label = label.split(" - ", 1)[0].replace("proposed edit:", "Proposed edit:")
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


def _stage(proposal: Proposal, selection_target=None, selection_mode="replace", draft_target=None) -> PendingProposal:
    global _pending
    with _lock:
        _pending = PendingProposal(proposal, selection_target=selection_target,
                                   selection_mode=selection_mode)
        _pending.draft_target = draft_target
        return _pending


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------

def enabled(app) -> bool:
    """Whether editing is both configured and authorized to use a model."""
    from samsara import config_defaults
    from samsara import ai_preferences

    cfg = getattr(app, "config", None) or {}
    section = cfg.get("ava_edit")
    if not isinstance(section, dict):
        configured = bool(config_defaults.DEFAULTS["ava_edit.enabled"])
    else:
        configured = bool(section.get(
            "enabled", config_defaults.DEFAULTS["ava_edit.enabled"]))
    return configured and ai_preferences.runtime_state(cfg, "editing").allowed


def source_text(app) -> str:
    """The text an edit would edit: the most recent committed dictation
    chunk. Selection capture supplies its own source when this is empty."""
    manager = getattr(app, "_session_mode_manager", None)
    if manager is None:
        return ""
    draft = getattr(manager, "dictate_pending_buffer", None)
    if isinstance(draft, str) and draft:
        return draft
    try:
        item = manager.top_dictation_chunk()
    except Exception as exc:
        logger.debug(f"[AVA-EDIT] could not read the session buffer: {exc}")
        return ""
    return item.payload if item is not None else ""


def is_available(app) -> bool:
    """Availability of the existing staged-draft route, not model readiness.

    Selection requests can still use the deterministic cleanup floor.
    """
    return bool(source_text(app))


def propose(text: str, instruction: str, app, *, offline=False, cleanup=False,
            transform="") -> Proposal:
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

    if offline:
        from samsara.ava_edit.selection import deterministic_cleanup
        raw, reason = deterministic_cleanup(original), None
    else:
        raw, reason = model.rewrite(original, instruction, app)
        if cleanup and raw is None and reason == "no model available":
            from samsara.ava_edit.selection import deterministic_cleanup
            raw = deterministic_cleanup(original)
    if raw is None:
        return Proposal.none(reason, original=original, instruction=instruction)

    if transform in {"translate", "summarize"}:
        from samsara.ava_edit.selection import check_transform
        refusal = check_transform(raw, original, instruction, transform)
    else:
        refusal = sanitize.check(raw, original, instruction)
    if refusal is None and cleanup and sanitize.similarity(original, sanitize.clean(raw)) < 0.85:
        refusal = "not derived from the text"
    if refusal is not None:
        logger.info(f"[AVA-EDIT] rewrite refused by the sanitizer: {refusal}")
        return Proposal.none(refusal, original=original, instruction=instruction)

    return Proposal.build(original, sanitize.clean(raw), instruction=instruction)


def stage(app, proposal: Proposal, *, selection_target=None, selection_mode="replace", draft_target=None) -> bool:
    """Show the proposal and leave it standing. Applies NOTHING.

    Returns True when a proposal is now staged and waiting for the word.
    """
    if not proposal.ok:
        _chip(app, "edit_refused", {"reason": proposal.reason})
        _say(app, proposal.describe())
        logger.info(f"[AVA-EDIT] not staged: {proposal.reason}")
        return False
    _stage(proposal, selection_target, selection_mode, draft_target)
    _chip(app, "edit_proposed", {"changes": proposal.change_count})
    description = proposal.describe()
    if draft_target is not None:
        description = description.replace("Say apply to make it so.", "Choose Accept or Cancel.")
    _say(app, description)
    if selection_mode == "below":
        _say(app, "The summary will be added below the selection.")  # Stand-in copy (635).
    logger.info(f"[AVA-EDIT] staged: {proposal.change_count} change(s), awaiting apply")
    return True


def propose_and_stage(app, instruction: str) -> bool:
    """The whole propose half, for a caller that has only the instruction.
    Blocking -- run it off the utterance thread."""
    from samsara.ava_edit import selection
    if selection.should_capture(app, instruction):
        return selection.propose_and_stage(app, instruction)
    manager = getattr(app, "_session_mode_manager", None)
    document = getattr(manager, "draft_document", None)
    if document is not None and document.text:
        return _propose_draft(app, instruction, manager)
    if intent.selection_request(instruction) == "draft":
        return False
    original = source_text(app)
    if not original:
        return False
    _chip(app, "edit_thinking")
    return stage(app, propose(original, instruction, app))


def _propose_draft(app, instruction, manager):
    import re
    from samsara.ava_edit.selection import DEFAULT_INSTRUCTION

    with manager._dispatch_lock:
        document = manager.draft_document
        original = document.text
        target = (manager, document.document_id, document.revision, original)
    scope = intent.draft_cleanup_scope(instruction)
    start = 0
    if scope == "last sentence":
        boundaries = list(re.finditer(r'[.!?]["\x27\u201d\u2019]*\s+(?=\S)', original.rstrip()))
        start = boundaries[-1].end() if boundaries else 0
    source = original[start:]
    _chip(app, "edit_thinking")
    proposal = propose(source, DEFAULT_INSTRUCTION if scope else instruction, app,
                       offline=bool(scope) and not enabled(app), cleanup=bool(scope))
    return stage(app, proposal, draft_target=(*target, start))


# ---------------------------------------------------------------------------
# Apply -- only on the word
# ---------------------------------------------------------------------------

def apply(app) -> dict:
    """Apply the staged proposal through the scratch-that stack, at the
    configured pacing, as one undoable unit.

    Returns SessionModeManager.apply_edit's result dict, or a same-shaped
    refusal. Staged-draft proposals clear on an answer; selection proposals
    remain pending when their original target can no longer be verified.
    """
    staged = pending()
    if staged is None:
        _chip(app, "edit_refused", {"reason": "nothing staged"})
        return {"applied": False, "reason": "nothing staged", "chars": 0}
    if staged.selection_target is not None:
        from samsara.ava_edit import selection
        return selection.apply(app, staged)
    if staged.draft_target is not None:
        manager, document_id, revision, original, start = staged.draft_target
        if getattr(app, "_session_mode_manager", None) is not manager:
            return {"applied": False, "reason": "the session changed", "chars": 0}
        with manager._dispatch_lock:
            document = manager.draft_document
            valid = (document.document_id == document_id and document.revision == revision
                     and document.text == original)
            if valid:
                from copy import deepcopy
                from samsara.session_modes import StackItem
                before = deepcopy(document)
                document.replace_text(original[:start] + staged.proposal.rewritten, edited=True)
                manager._clear_draft_undo.drop()
                manager._last_dictate_ended_terminal = None
                manager._stack.push(StackItem(kind="draft_edit_apply", payload=document.text,
                    mode=manager.mode, timestamp=manager._clock(), extra={"document": before}))
        clear()
        _chip(app, "edit_applied" if valid else "edit_refused",
              {"changes": staged.proposal.change_count, "reason": "the text changed"})
        surface = getattr(app, "live_surface", None)
        if valid and surface is not None:
            surface.sync_draft(manager)
        preview = getattr(app, "_dictate_preview", None)
        if valid and preview is not None:
            preview._resync_from_draft(manager)
            preview._render("")
        return {"applied": valid, "reason": "" if valid else "the text changed",
                "chars": len(staged.proposal.rewritten) if valid else 0}
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
        staged = pending()
        from samsara.ava_edit import selection
        capture = selection.should_capture(app, text)
        manager = getattr(app, "_session_mode_manager", None)
        draft = getattr(manager, "dictate_pending_buffer", "")
        cleanup = bool(draft) and intent.draft_cleanup_scope(text) is not None
        if not (capture or cleanup or (staged and (staged.selection_target is not None
                                                 or staged.draft_target is not None))) and not enabled(app):
            return False

        if pending() is not None:
            if intent.is_apply_utterance(text):
                preview = getattr(app, "_dictate_preview", None)
                if preview is not None:
                    preview._edit_reply_text = text.strip()
                apply(app)
                return True
            if intent.is_discard_utterance(text):
                preview = getattr(app, "_dictate_preview", None)
                if preview is not None:
                    preview._edit_reply_text = text.strip()
                discard(app)
                return True
            # Anything else: the proposal stands, the text is untouched, and
            # the utterance goes to Ava. Not claimed.
            return False

        if capture or (intent.is_edit_request(text) and is_available(app)):
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
