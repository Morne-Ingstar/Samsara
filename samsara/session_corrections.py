"""Word-correction delegates over the existing SessionModeManager state.

The facade owns the document, stack, lock, clock and callbacks. Imports flow
from these lazily loaded helpers to the canonical session contracts; dynamic
manager calls preserve overrides and the original lock/callback boundaries.
"""
from __future__ import annotations

import re
from typing import Optional

from samsara.log import describe_text
from samsara.session_modes import (
    ACKNOWLEDGEMENT, QUESTION, DispatchOutcome, SessionMode, UtteranceSignals,
    WORD_CORRECTION_CANCEL_PHRASES, WORD_CORRECTION_TTL_S,
    chunk_ends_terminal, is_clear_draft, is_dictate_commit, is_recover_draft,
    log, match_ava_invocation, match_switch_word, passes_switch_anti_hallucination_gate,
)


def _undo_last_correction_outcome(self) -> DispatchOutcome:
    try:
        result = self._correction_undo_fn() or {}
    except Exception as exc:
        log.warning("[SESSION] correction undo failed: %s", exc)
        result = {}
    if not result.get("undone"):
        self._speak("There is no correction to take back.", ACKNOWLEDGEMENT)
        return DispatchOutcome(kind="correction_undo_nothing")
    wrong, right = result.get("wrong", ""), result.get("right", "")
    self._speak(f'Forgot "{wrong}" to "{right}".'[:60], ACKNOWLEDGEMENT)
    return DispatchOutcome(kind="correction_undone", detail={
        "wrong": wrong, "right": right, "where": result.get("where", ""),
    })


def request_word_correction(self, word: str, occurrence: int = 0, *,
                            expected_count: int = 0,
                            source: str = "preview_click") -> dict:
    """Arm a correction for one word of the staged draft: the NEXT
    utterance replaces it instead of being dictated. Returns
    {"ok": bool, ...}; a refusal never changes anything."""
    word = (word or "").strip()
    with self._dispatch_lock:
        if not self._buffer_dictate_until_commit or self.mode is not SessionMode.DICTATE:
            return {"ok": False, "reason": "not_dictating"}
        if not word:
            return {"ok": False, "reason": "no_word"}
        if not self._dictate_pending_buffer:
            return {"ok": False, "reason": "no_draft"}
        if self._live_surface_enabled:
            self._parked_hold = True
            self._draft.mark_manual_commit()
        self._pending_correction = {
            "word": word, "occurrence": max(0, int(occurrence)),
            "expected_count": max(0, int(expected_count)),
            "expires": self._clock() + WORD_CORRECTION_TTL_S,
            "source": source,
        }
    log.info("[SESSION] word correction armed for %r (occurrence %d, source %s)",
             describe_text(word), occurrence, source)
    self._speak(f'Say the replacement for "{word}".', QUESTION)
    return {"ok": True, "word": word, "occurrence": occurrence,
            "ttl_s": WORD_CORRECTION_TTL_S}


def select_live_surface_word(self, selection: object) -> dict:
    """Validate and park one revisioned surface selection.

    The surface is deliberately not allowed to infer a word from its
    rendered text.  Its coordinates identify a stable document segment;
    a new decoder result changes the revision and makes this selection
    stale.  Parking happens here, under the same document lock as the
    edit, so review can never trigger an automatic delivery.
    """
    if not self._live_surface_enabled or not isinstance(selection, dict):
        return {"ok": False, "reason": "unavailable"}
    with self._dispatch_lock:
        try:
            document_id = str(selection["document_id"])
            revision = int(selection["revision"])
            segment_id = str(selection["segment_id"])
            start, end = int(selection["start"]), int(selection["end"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "reason": "invalid_selection"}
        if document_id != self._draft.document_id or revision != self._draft.revision:
            return {"ok": False, "reason": "stale"}
        segment = next((item for item in self._draft.segments if item.id == segment_id), None)
        if segment is None or start < 0 or end <= start or end > len(segment.text):
            return {"ok": False, "reason": "stale"}
        word = segment.text[start:end]
        if not word:
            return {"ok": False, "reason": "stale"}
        self._parked_hold = True
        self._draft.mark_manual_commit()
        accepted = dict(selection)
        accepted.update({"document_id": self._draft.document_id,
                         "revision": self._draft.revision, "word": word})
        return {"ok": True, "selection": accepted}


def apply_live_surface_correction(self, selection: object, replacement: str) -> dict:
    """Apply one local review edit, refusing stale document coordinates.

    This route intentionally does not create a general replacement rule or
    attribute a whole capture as a word-level training clip.  The existing
    correction-capture path remains reserved for its attributable audio.
    """
    if not self._live_surface_enabled or not isinstance(selection, dict):
        return {"ok": False, "reason": "unavailable"}
    replacement = str(replacement or "").strip()
    if not replacement:
        return {"ok": False, "reason": "empty"}
    with self._dispatch_lock:
        try:
            result = self._draft.edit(str(selection["document_id"]), int(selection["revision"]),
                                      str(selection["segment_id"]), int(selection["start"]),
                                      int(selection["end"]), replacement)
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "reason": "invalid_selection"}
        if not result.accepted:
            return {"ok": False, "reason": result.reason}
        self._parked_hold = True
        self._last_dictate_ended_terminal = chunk_ends_terminal(self._draft.text)
        return {"ok": True, "revision": result.revision, "chars": result.chars}


def copy_sent_receipt_as_draft(self, text: str) -> dict:
    """Make a deliberate, editable copy of one sent receipt.

    Receipts are otherwise read-only.  Refusing when any draft exists
    prevents a tap from silently combining old delivered text with a new
    unsent thought.
    """
    if not self._live_surface_enabled:
        return {"ok": False, "reason": "unavailable"}
    text = str(text or "")
    with self._dispatch_lock:
        if self._draft.text:
            return {"ok": False, "reason": "draft_pending"}
        if not text.strip():
            return {"ok": False, "reason": "empty"}
        self._draft.append(text)
        self._draft.mark_manual_commit()
        self._parked_hold = True
        return {"ok": True, "revision": self._draft.revision, "chars": self._draft.chars}


def pending_word_correction(self) -> Optional[dict]:
    """The armed correction, or None (also None once it has expired)."""
    pending = self._pending_correction
    if pending is None:
        return None
    if self._clock() > pending["expires"]:
        return None
    return dict(pending)


def cancel_word_correction(self, reason: str = "cancelled") -> bool:
    with self._dispatch_lock:
        had = self._pending_correction is not None
        self._pending_correction = None
    if had:
        log.info("[SESSION] word correction %s", reason)
    return had


def _consume_word_correction(self, text: str, normalized: str,
                             signals: UtteranceSignals) -> Optional[DispatchOutcome]:
    """None when no correction is armed, when it has expired, or when this
    utterance is a control phrase that should run normally (the correction
    is dropped first, so a control word is never eaten)."""
    pending = self._pending_correction
    if pending is None:
        return None
    if self._clock() > pending["expires"] or self.mode is not SessionMode.DICTATE:
        self._pending_correction = None
        log.info("[SESSION] word correction expired unanswered; the words are dictation")
        return None
    if normalized in WORD_CORRECTION_CANCEL_PHRASES:
        self._pending_correction = None
        self._speak("Correction cancelled.", ACKNOWLEDGEMENT)
        return DispatchOutcome(kind="dictate_correction_cancelled",
                               detail={"word": pending["word"]})
    if (is_dictate_commit(text) or is_clear_draft(text) or is_recover_draft(text)
            or match_switch_word(text, current_mode=self.mode) is not None
            or match_ava_invocation(text, self._ava_invocations)):
        # A real control word wins and still does its own job.
        self._pending_correction = None
        log.info("[SESSION] word correction dropped: %r is a control phrase", describe_text(text))
        return None
    if not passes_switch_anti_hallucination_gate(signals):
        # Degraded audio must not edit the draft; the click stays armed so
        # the user can simply say the word again.
        log.info("[SESSION] replacement refused by the anti-hallucination gate for %r", describe_text(text))
        return DispatchOutcome(kind="dictate_correction_refused",
                               detail={"word": pending["word"]})
    self._pending_correction = None
    return self._apply_pending_word_correction(pending, text.strip())


def choose_word_correction(self, replacement: str) -> DispatchOutcome:
    """Apply a preview choice through the same replacement/capture path
    as a spoken answer.  A tap is already the deliberate input, so it does
    not pass through the speech-quality gate.
    """
    with self._dispatch_lock:
        pending = self._pending_correction
        if (pending is None or self._clock() > pending["expires"]
                or self.mode is not SessionMode.DICTATE):
            self._pending_correction = None
            return DispatchOutcome(kind="dictate_correction_unavailable")
        self._pending_correction = None
    return self._apply_pending_word_correction(pending, replacement)


def _apply_pending_word_correction(self, pending: dict, replacement: str) -> DispatchOutcome:
    """The one apply/capture route shared by spoken and clicked answers."""
    result = self.replace_draft_word(
        pending["word"], pending["occurrence"], replacement,
        expected_count=pending.get("expected_count") or 0,
    )
    if not result.get("ok"):
        self._speak("I could not find that word any more.", ACKNOWLEDGEMENT)
        return DispatchOutcome(kind="dictate_correction_failed", detail={
            "word": pending["word"], "reason": result.get("reason", "unknown"),
        })
    captured = None
    if self._correction_capture_fn is not None:
        try:
            captured = self._correction_capture_fn(
                pending["word"], result["replacement"], result.get("context", ""),
                result.get("audio"))
        except Exception as exc:  # capture must never break dispatch
            log.warning("[SESSION] correction capture failed: %s", exc)
    return DispatchOutcome(kind="dictate_word_corrected", detail={
        "word": pending["word"], "replacement": result["replacement"],
        "pending_chars": len(self._dictate_pending_buffer),
        "captured": captured,
    })


def replace_draft_word(self, word: str, occurrence: int = 0, replacement: str = "",
                       *, expected_count: int = 0) -> dict:
    """Replace ONE occurrence of `word` in the staged draft.

    Refuses rather than guesses: if the word is gone, if the number of
    occurrences no longer matches what the caller saw, or if the index is
    out of range, nothing is edited (the user dictated more while the
    click was armed). The top staged chunk is edited alongside the buffer
    when the word falls inside it, so "scratch that" -- which requires the
    draft to still END with that chunk -- keeps working."""
    word = (word or "").strip()
    replacement = " ".join((replacement or "").split())
    if not word or not replacement:
        return {"ok": False, "reason": "empty"}
    pattern = re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)")
    with self._dispatch_lock:
        buffer = self._dictate_pending_buffer
        if not buffer:
            return {"ok": False, "reason": "no_draft"}
        matches = list(pattern.finditer(buffer))
        if not matches:
            matches = list(re.finditer(
                r"(?<!\w)" + re.escape(word) + r"(?!\w)", buffer, re.IGNORECASE))
        if not matches:
            return {"ok": False, "reason": "not_found"}
        if expected_count and len(matches) != expected_count:
            return {"ok": False, "reason": "draft_changed"}
        if occurrence >= len(matches):
            return {"ok": False, "reason": "draft_changed"}
        match = matches[occurrence]
        start, end = match.start(), match.end()
        new_buffer = buffer[:start] + replacement + buffer[end:]
        # Queue 85: the audio that produced the WRONG word is the training
        # signal the dictionary's long game wants. Attribute it only when
        # exactly ONE staged chunk contains the word -- with the word in
        # several chunks there is no way to tell which utterance it came
        # from, and a mislabelled clip is worse than none.
        audio = None
        chunks = [item for item in self._stack._items
                  if item.kind == "dictation_staged_chunk"]
        holders = [i for i, item in enumerate(chunks) if pattern.search(item.payload)]
        if len(holders) == 1 and holders[0] < len(self._dictate_pending_audio):
            audio = self._dictate_pending_audio[holders[0]]
        top = self._stack.peek()
        if (top is not None and top.kind == "dictation_staged_chunk"
                and buffer.endswith(top.payload)):
            payload_start = len(buffer) - len(top.payload)
            if start >= payload_start:
                top.payload = (top.payload[:start - payload_start] + replacement
                               + top.payload[end - payload_start:])
        # Preserve a word correction as its own document transaction when
        # the word is wholly inside one stable segment.  A decoder can
        # theoretically split a word across chunks; retain the legacy
        # whole-buffer replacement for that exceptional case rather than
        # guessing at a partial segment edit.
        segment_start = 0
        edited = False
        for segment in self._draft.segments:
            segment_end = segment_start + len(segment.text)
            if start >= segment_start and end <= segment_end:
                edit = self._draft.edit(
                    self._draft.document_id, self._draft.revision, segment.id,
                    start - segment_start, end - segment_start, replacement,
                )
                edited = edit.accepted
                break
            segment_start = segment_end
        if not edited:
            self._dictate_pending_buffer = new_buffer
            self._draft.edited = True
        self._last_dictate_ended_terminal = (
            chunk_ends_terminal(new_buffer) if new_buffer else None)
        context = new_buffer[max(0, start - 40):start + len(replacement) + 40].strip()
    log.info("[SESSION] draft word corrected: %r -> %r (occurrence %d)",
             describe_text(word), describe_text(replacement), occurrence)
    return {"ok": True, "replacement": replacement, "before": buffer,
            "after": new_buffer, "context": context, "audio": audio}
