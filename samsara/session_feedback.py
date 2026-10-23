"""Pure, Qt-free outcome-chip presentation shared through session_modes."""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Outcome chip vocabulary -- what the listening indicator SAYS happened
# ---------------------------------------------------------------------------
#
# Before this, a MISS, a mode switch, a refused commit and a failed command
# all produced the same beep, and a deaf user got nothing at all. The chip is
# the visual half: one short label per dispatch outcome, coloured by meaning.
# Pure and Qt-free so the whole vocabulary is testable; the listening
# indicator only ever renders what this returns.

#: Chip kinds. Colours come from samsara.ui.theme in the indicator:
#: success=SUCCESS, error=ERROR, warning=WARNING, accent=ACCENT,
#: pending=ACCENT with no TTL, live=ERROR with no TTL (a hold in progress).
CHIP_KINDS = ("success", "error", "warning", "accent", "pending", "live")

#: Default time on screen, in ms. None = stays until replaced.
CHIP_TTL_MS = 1800
_CHIP_TTL_OVERRIDES = {
    "dictate_committed": 900,
    "dictate_injected": 900,
    "dictate_clipboard_choice": None,
    # A miss or a suggested correction must remain long enough to read.
    "command_miss": 4000,
    "command_suggestion_pending": 8000,
}

# OWNER COPY: command-lane wording stays together for owner revision.
COMMAND_CHIP_COPY = {
    "ava_done": "{done}",
    "ava_chain_done": "Completed (Ava): {done}",
    "chain_done": "Done: {done}",
    "chain_yes": 'Say "{step}" on its own; it needs your yes.',
    "chain_refused": 'I could not do "{step}": {reason}. Nothing ran.',
    "chain_stopped": "Done: {done} · stopped at {step}: {reason}",
    "miss": "heard: {heard} — not a command",
    "pack_off": "heard: {heard} — {pack} is off",
    "suggestion": "heard: {heard} — did you mean {phrase}? say yes or no",
    "awaiting": "heard: {heard} — {phrase}? yes or no",
    "executed": "heard: {heard} — ran {phrase}",
    "working": "heard: {heard} — {phrase}…",
    "failed": "heard: {heard} — {reason}",
    "declined": "heard: {heard} — suggestion cancelled",
}
_COMMAND_HEARD_MAX = 72

#: Outcomes that deliberately show NOTHING. "empty" is a discarded
#: near-silence decode (never activity); "abort" has its own loud session-exit
#: feedback, and since queue 84 it shows a chip ONLY when it set a draft aside
#: (see _abort_chip), because then the user has to know the text still exists.
#: dictation_chunk is a StackItem kind, not an outcome kind -- listed only
#: because the queue-41 brief names it, and harmless here.
# The edit handler emits its own thinking/proposal/result feedback. Its
# consumed marker must leave that feedback in place on both displays.
_NO_CHIP = frozenset({"empty", "abort", "dictation_chunk", "ava_edit_handled"})

_REASON_MAX = 24

# The chip glyphs are built with chr() so this source file stays pure ASCII
# (non-ASCII literals have caused encoding trouble on this machine). They are
# symbols, not emoji, and render natively in Qt.
CHIP_CHECK = chr(0x2713)      # check mark
CHIP_CROSS = chr(0x2717)      # ballot x
CHIP_ARROW = chr(0x2192)      # rightwards arrow
CHIP_ELLIPSIS = chr(0x2026)   # horizontal ellipsis
CHIP_DASH = chr(0x2014)       # em dash, for "undone 5 -- nothing left"


def _short(text, limit=_REASON_MAX) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + CHIP_ELLIPSIS


#: What execution_policy.stop_all reports it cleared -> what to call it on a
#: chip, in the order a user would care about. "speech" first because "Ava
#: kept talking" is the symptom the stop exists to answer.
_STOPPED_LABELS = (
    ("speech", lambda n: "speech"),
    ("in_flight", lambda n: "Ava"),
    ("pending", lambda n: "confirm"),
    ("queued", lambda n: f"{n} queued"),
    ("schedule", lambda n: "schedule"),
)



def _stopped_chip(detail: dict) -> "tuple[str, str]":
    """Queue 116. The stop chip says WHAT was halted, not just that something
    was -- a chip that reads "stopped" whether it cut an answer off mid-word
    or found nothing at all teaches the user nothing and, worse, tells them a
    panic control worked when it did nothing.

    A stop that halts nothing is not a success: it says so, in warning
    colour, because the interesting case is the user expecting it to have
    caught something."""
    cleared = detail.get("cleared")
    cleared = cleared if isinstance(cleared, dict) else {}
    parts = [label(cleared.get(key)) for key, label in _STOPPED_LABELS if cleared.get(key)]
    if not parts:
        return ("nothing to halt", "warning")
    # As many as fit, then a count for the rest. Chosen by measuring rather
    # than by a fixed cap so the chip never truncates a word away mid-stop:
    # "halted 12 queued, sched..." is a worse answer than "halted 12 queued +1".
    for count in range(len(parts), 0, -1):
        text = "halted " + ", ".join(parts[:count])
        if count < len(parts):
            text += f" +{len(parts) - count}"
        if len(text) <= _REASON_MAX:
            return (text, "warning")
    return (_short("halted " + parts[0]), "warning")


def _reason(kind: str, detail: dict) -> str:
    for key in ("error", "reason"):
        value = detail.get(key) if isinstance(detail, dict) else None
        if value:
            return _short(value)
    return _short(kind.replace("_", " "))


def _first_two_words(phrase) -> str:
    return " ".join(str(phrase or "").split()[:2])


def _heard(detail: dict) -> str:
    return _short(detail.get("heard"), _COMMAND_HEARD_MAX) or "(nothing)"


def _command_copy(key: str, **values) -> str:
    return COMMAND_CHIP_COPY[key].format(**values)


def _command_chip_label(text: str, phrase) -> str:
    """Visible command text plus its catalog id for the outcome ring.

    The import is deliberately here: session_modes is the command-path core,
    while the catalog is only needed when a chip is actually built.
    """
    from samsara.outcome_ring import command_chip_label
    return command_chip_label(text, phrase)


def _mode_label(mode) -> str:
    value = getattr(mode, "value", mode)
    return str(value or "").upper()


def _abort_chip(detail: dict):
    """Queue 84: an abort is normally chipless (it has its own loud earcon),
    but when it set a draft aside the user has to be told the text still
    exists and how to ask for it back."""
    if detail.get("recoverable_chars"):
        return ("cancelled, say bring back my draft", "warning")
    return None


# Queue 429: deliberate mappings preserve the existing controller chips.
# Keep the warning wording until the owner supplies Interact chip labels.
_INTERACT_CHIPS = {
    "interact_action": ("? interact_action", "warning"),
    "interact_armed": ("? interact_armed", "warning"),
    "interact_choosing": ("? interact_choosing", "warning"),
    "interact_cleared": ("? interact_cleared", "warning"),
    "interact_confirm": ("? interact_confirm", "warning"),
    "interact_exit": ("? interact_exit", "warning"),
    "interact_expired": ("? interact_expired", "warning"),
    "interact_off": ("? interact_off", "warning"),
    "interact_refused": ("? interact_refused", "warning"),
    "interact_reply": ("? interact_reply", "warning"),
    "interact_selected": ("? interact_selected", "warning"),
    "interact_unknown": ("? interact_unknown", "warning"),
}


def outcome_chip(kind: str, detail: Optional[dict] = None) -> "tuple[str, str] | None":
    """(label, chip_kind) for a DispatchOutcome kind, or None for no chip.

    Every kind DispatchOutcome is constructed with in this module is mapped;
    tests/test_outcome_chip.py enumerates them from this file's AST so a new
    kind without a chip fails the suite. An unmapped kind that still reaches
    here at runtime returns ("? <kind>", "warning") -- visibly wrong on
    purpose, so it gets noticed and mapped; the caller logs it at WARNING.
    """
    detail = detail if isinstance(detail, dict) else {}
    if detail.get('chain_feedback_sent'):
        return None  # 399: executor already emitted the utterance's one shared chip.
    if (kind in ('command_executed', 'hands_free_command_executed')
            and detail.get('origin') == 'model' and detail.get('state') == 'completed'):
        return (_command_copy('ava_done', done=detail.get('ava_done') or detail.get('phrase') or 'command'), 'success')
    if kind == 'ava_command_chain':
        done = detail.get('chain_done') or []
        if detail.get('state') == 'completed':
            return (_command_copy('ava_chain_done', count=len(done), done=' · '.join(done)), 'success')
        return outcome_chip('command_chain', detail)
    if kind == 'command_chain' or 'chain_count' in detail:
        done = detail.get('chain_done') or []
        step = detail.get('chain_stopped') or 'chain'
        reason = detail.get('reason') or 'stopped'
        reason = {'target_timeout': 'window did not change', 'too_many_steps': 'four commands at most',
                  'stale': 'cancelled', 'pack_disabled': 'pack is off',
                  'out_of_scope': 'command unavailable here', 'invalid_args': 'could not understand that step',
                  'queued': 'waiting for that step'}.get(reason, reason)
        if detail.get('state') == 'completed':
            return (_command_copy('chain_done', count=len(done), done=' · '.join(done)), 'success')
        if done:
            return (_command_copy('chain_stopped', count=len(done), done=' · '.join(done),
                                  step=step, reason=reason), 'warning')
        if reason == 'confirmation_required':
            return (_command_copy('chain_yes', step=step), 'warning')
        return (_command_copy('chain_refused', step=step, reason=reason), 'warning')
    if kind in ("active_capture_stopped", "active_capture_cancelled",
                "active_capture_failed"):
        default_kind = "success" if kind == "active_capture_stopped" else "warning"
        return (_short(detail.get("label") or kind.replace("_", " "), 56),
                detail.get("chip_kind") or default_kind)

    if kind == "abort":
        return _abort_chip(detail)
    if kind in _NO_CHIP:
        return None

    if kind == "command_miss":
        heard = _heard(detail)
        if detail.get("pack"):
            return (_command_copy("pack_off", heard=heard, pack=_short(detail["pack"])), "warning")
        return (_command_copy("miss", heard=heard), "error")
    if kind == "command_suggestion_pending":
        phrase = _short(detail.get("phrase"), _COMMAND_HEARD_MAX)
        return (_command_chip_label(
            _command_copy("suggestion", heard=_heard(detail), phrase=phrase), detail.get("phrase")),
            "warning")
    if kind == "command_suggestion_declined":
        return (_command_copy("declined", heard=_heard(detail)), "warning")
    if kind == "personal_alias_offer":
        missed = _short(detail.get("miss"))
        canonical = _short(detail.get("canonical"))
        return (f"Save '{missed}' as another way to say '{canonical}'? — say save alias", "pending")
    if kind == "command_awaiting_confirmation":
        phrase = _first_two_words(detail.get("phrase"))
        return (_command_chip_label(
            _command_copy("awaiting", heard=_heard(detail), phrase=phrase or "that"),
            detail.get("phrase")), "pending")
    if kind == "command_cancel_window":
        verb = _first_two_words(detail.get("phrase"))
        text = (f"running {verb}{CHIP_ELLIPSIS} say no" if verb else f"running{CHIP_ELLIPSIS} say no")
        return (_command_chip_label(text, detail.get("phrase")), "pending")
    if kind == "stopped":
        return _stopped_chip(detail)
    if kind == "pending_reply":
        answer = str(detail.get("answer") or "")
        if answer == "approved":
            return ("confirmed", "success")
        if answer == "extended":
            return ("waiting", "pending")
        if answer == "rejected":
            return ("cancelled", "warning")
        why = answer.split(":", 1)[1] if answer.startswith("refused:") else answer
        return (f"refused: {_short(why)}", "warning")
    if kind in ("command_executed", "hands_free_command_executed"):
        if (kind == "hands_free_command_executed" and detail.get("state") == "completed"
                and not detail.get("media_action") and detail.get("origin") != "model"):
            return (f"→ command: {detail.get('phrase') or 'command'}", "success")
        if detail.get("state") == "completed" and detail.get("media_action"):
            return (detail["message"], "success")
        phrase = _first_two_words(detail.get("phrase"))
        heard = _heard(detail)
        if detail.get("origin") == "model" and detail.get("state") == "completed":
            return (f"{CHIP_CHECK} {phrase or 'command'} (Ava)", "success")
        if detail.get("state") in ("queued", "matched"):
            return (_command_chip_label(
                _command_copy("working", heard=heard, phrase=phrase or "working"), detail.get("phrase")),
                "accent")
        return (_command_chip_label(
            _command_copy("executed", heard=heard, phrase=phrase or "that"), detail.get("phrase")),
            "success")
    if kind == "command_failed" or (kind == "hands_free_command_failed"
                                    and detail.get("state") in ("rejected", "cancelled")):
        state = detail.get("state")
        verb = _first_two_words(detail.get("phrase"))
        if state == "rejected":
            reason = f"refused: {verb}" if verb else "refused"
        elif state == "cancelled":
            reason = f"cancelled: {verb}" if verb else "cancelled"
        elif not detail.get("reason") and not detail.get("error") and verb:
            reason = f"{CHIP_CROSS} {verb}"
        else:
            reason = f"{CHIP_CROSS} {_reason(kind, detail)}"
        return (_command_chip_label(
            _command_copy("failed", heard=_heard(detail), reason=reason), detail.get("phrase")),
            "error" if state not in ("rejected", "cancelled") else "warning")
    if kind == "mode_switch":
        if detail.get("sleep"):
            return ("asleep", "accent")
        mode = _mode_label(detail.get("mode"))
        if detail.get("side_effect_error"):
            # The mode changed but its display/teardown did not (queue 60).
            return (f"{CHIP_ARROW} {mode}: display error" if mode else f"{CHIP_ARROW} display error", "warning")
        return (f"{CHIP_ARROW} {mode}" if mode else CHIP_ARROW, "accent")

    if detail.get('saved_to_history'):
        label, severity = outcome_chip(kind, {k: v for k, v in detail.items() if k != 'saved_to_history'})
        return (f'{label} - saved to History', severity)
    if kind == "dictate_clipboard_choice":
        return (detail.get("reason", "Clipboard has a large image Samsara can't put back."), "warning")
    if kind == "dictate_commit_failed":
        detail = detail or {}
        reason = (_reason(kind, detail) if detail.get("error") or detail.get("reason")
                  else "Text wasn't inserted. Draft kept.")
        if detail.get("clipboard_reason"):
            reason = detail.get("error") or detail.get("reason") or reason
        return (f"{CHIP_CROSS} {reason}", "error")

    if kind in ("ava_entry_failed", "hands_free_command_failed",
                "mode_switch_failed", "prefix_switch_failed"):
        text = f"{CHIP_CROSS} {_reason(kind, detail)}"
        if kind == "hands_free_command_failed":
            text = _command_chip_label(text, detail.get("phrase"))
        return (text, "error")

    if kind in ("dictate_commit_blocked_focus_lock", "dictate_suppressed_focus_lock"):
        if detail.get("clipboard_image_removed"):
            return ("Image removed from clipboard. Focus changed; draft kept.", "error")
        return ("refused: focus lock", "warning")
    if kind == "dictate_blocked_elevated":
        # Queue 50: the window runs as administrator; Windows would drop the
        # keystrokes silently. Never a success chip.
        return (f"{CHIP_CROSS} can't type: admin window", "error")
    if kind == "hands_free_command_blocked":
        commit = str(detail.get("commit_outcome", ""))
        why = "focus lock" if "focus" in commit else "blocked"
        return (_command_chip_label(f"refused: {why}", detail.get("phrase")), "warning")
    if kind in ("dictate_commit_refused", "hands_free_command_refused"):
        text = "refused: unclear"
        if kind == "hands_free_command_refused":
            text = _command_chip_label(text, detail.get("phrase"))
        return (text, "warning")
    # Found in source, not named in the queue-41 brief -- mapped on purpose.
    if kind == "draft_parked":
        return ("draft parked", "pending")
    if kind == "draft_pause_refused":
        return ("can't park: unclear", "warning")
    if kind == "draft_pause_unavailable":
        return ("nothing to park", "warning")
    if kind == "surface_review_action":
        return ("review", "accent")

    if kind == "dictate_commit_unavailable":
        return ("refused: nothing staged", "warning")
    if kind == "scratch_refuse":
        return (detail.get('message', "refused: undo"), "warning")
    # Queue 80: clear the whole staged draft.
    if kind == "dictate_draft_cleared":
        if detail.get("voice_clear"):
            return ('Draft cleared - say "bring it back"', "success")  # Stand-in.
        return ("draft cleared, say bring back my draft", "success")
    # Queue 84: the draft an abort or a clear set aside, put back on request.
    if kind == "dictate_draft_recovered":
        return ("draft back", "success")
    if kind == "dictate_recover_nothing":
        return ("nothing to bring back", "warning")
    if kind == "dictate_clear_nothing":
        return ("refused: nothing staged", "warning")
    if kind == "dictate_clear_refused":
        return ("refused: unclear", "warning")
    # Queue 85: a word clicked in the preview, replaced by the next utterance.
    if kind == "dictate_word_corrected":
        word = _short(detail.get("replacement") or "", 16)
        return (f"{CHIP_CHECK} fixed: {word}" if word else f"{CHIP_CHECK} word fixed", "success")
    if kind == "dictate_correction_cancelled":
        return ("correction cancelled", "warning")
    if kind == "dictate_correction_unavailable":
        return ("nothing to correct", "warning")
    if kind == "dictate_correction_refused":
        return ("refused: say it again", "warning")
    if kind == "dictate_correction_failed":
        return ("word not found", "warning")
    if kind == "draft_scrolled":
        return (f"draft {_short(detail.get('where') or '', 10)}", "accent")
    if kind == "correction_undone":
        return (f"forgot {_short(detail.get('wrong') or '', 14)}", "success")
    if kind == "correction_undo_nothing":
        return ("nothing to take back", "warning")

    if kind in ("dictate_committed", "dictate_injected"):
        if detail.get('clipboard_image_removed'):
            sent = 'Sent, unconfirmed.' if detail.get('delivery_unverified') else 'Text inserted.'
            return (f"{sent} Image removed from clipboard.", 'warning')
        if detail.get('copied_to_clipboard'):
            return ('copied to clipboard', 'success')
        if detail.get('empty') and detail.get('immediate'):
            return ('nothing to send', 'accent')
        if detail.get("delivery_unverified"):
            return ("sent, unconfirmed", "warning")
        return ("typed", "success")
    if kind in ("dictate_staged", "dictation_staged_chunk"):
        return ("staged", "pending")
    if kind == "scratch_success":
        if detail.get('message'):
            return (detail['message'], 'success')
        # Queue 99: one chip for however many chunks came off, and it never
        # claims a count it did not reach. "scratch that ten times" against a
        # stack of five says five, and says why there is no more.
        asked, did = detail.get("requested"), detail.get("scratched")
        if not asked or not did or asked == did == 1:
            return ("undone", "success")
        if did >= asked:
            return (f"undone {did}", "success")
        if detail.get("reason") == "empty":
            return (f"undone {did} {CHIP_DASH} nothing left", "warning")
        return (f"undone {did} of {asked} {CHIP_DASH} stopped", "warning")

    # Queue 102 -- Ava's edit proposals. "edit_proposed" is deliberately a
    # PENDING chip: chip_ttl_ms() gives pending no TTL, so the proposal stays
    # on screen until the user answers it instead of expiring after 1.8 s and
    # leaving them with a staged edit they can no longer see.
    if kind == "edit_thinking":
        # The model call is the one slow step the user cannot see. Without
        # this the chip sits on whatever came before for several seconds and
        # the app looks like it ignored them. Accent, with a TTL, because it
        # is superseded by the proposal (or the refusal) that follows.
        return (f"reading it back{CHIP_ELLIPSIS}", "accent")
    if kind == "edit_proposed":
        n = detail.get("changes") or 0
        unit = "change" if n == 1 else "changes"
        return (f"proposed edit: {n} {unit} - say apply", "pending")
    if kind == "edit_applied":
        n = detail.get("changes") or 0
        unit = "change" if n == 1 else "changes"
        return (f"edited {CHIP_CHECK} {n} {unit}", "success")
    if kind == "edit_discarded":
        return ("edit dropped", "warning")
    if kind == "edit_refused":
        reason = str(detail.get("reason") or "")[:_REASON_MAX]
        return ((f"no edit {CHIP_DASH} {reason}" if reason else "no edit"), "warning")

    if kind == "ava_dispatched":
        return (f"Ava{CHIP_ELLIPSIS}", "pending")
    if kind == "ava_rejected_not_substantive":
        return ("Ava: nothing to do", "warning")

    if kind in _INTERACT_CHIPS:
        return _INTERACT_CHIPS[kind]

    return (f"? {kind}", "warning")


def is_mapped_outcome(kind: str) -> bool:
    """Recognise deliberate mappings, including preserved Interact labels."""
    if kind in _INTERACT_CHIPS:
        return True
    chip = outcome_chip(kind)
    return chip is None or not chip[0].startswith("? ")


def chip_ttl_ms(outcome_kind: str, chip_kind: str) -> "int | None":
    """How long a chip stays up. None = until replaced (pending / live)."""
    if chip_kind in ("pending", "live"):
        return None
    return _CHIP_TTL_OVERRIDES.get(outcome_kind, CHIP_TTL_MS)


