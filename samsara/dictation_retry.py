"""Short-lived, missed-range dictation retry shared by every capture lane."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import threading
import time


# PLACEHOLDER COPY (1125)
MISSED = 'Missed a few words'
EMPTY = "Didn't catch that"
NOTE_MS = 8000
OWNER = 'dictation_retry'
# PLACEHOLDER COPY (1165)
RETRY_FAILED = "Still couldn't catch those words."
RETRY_DONE = 'Recovered words inserted.'
RETRY_EXPIRED = 'That retry has expired.'
_claim_lock = threading.Lock()


class CompactRetryNotice:
    """Compact display hooks; share the retry lifetime and ownership here."""

    def show_retry_notice(self, label, callback, *, history=None):
        from samsara.live_surface.feedback import Feedback, FeedbackKind
        self._dictation_retry_callback = callback
        self.show_feedback(label, 'accent', NOTE_MS, Feedback(FeedbackKind.INFORMATION, owner=OWNER, history=history))

    def clear_retry_notice(self):
        self._waiting_outcomes.remove(lambda row: getattr(row[3], 'owner', '') == OWNER)
        if getattr(self._chip_feedback, 'owner', '') == OWNER:
            self._chip_deadline = 0
            self._clear_outcome()

    def _report_width(self):
        chip = self._active_chip()
        if chip and getattr(self._chip_feedback, 'owner', '') == OWNER:
            return 100
        return 76 if chip and chip[1] in ('error', 'warning') else 0


@dataclass(frozen=True)
class MissedRange:
    start: float
    end: float
    delivered: str = ''


def live(app):
    slot = getattr(app, '_hold_retry_audio', None)
    return slot if slot is not None and time.monotonic() < getattr(slot, 'expires_at', 0.) else None


def claim(app, text='try again', *, expected=None):
    literal = getattr(app, '_verbatim_rule', None)
    if callable(literal) and literal():
        return None
    with _claim_lock:
        captured = getattr(app, '_dictation_retry_capture', None)
        app._dictation_retry_capture = None
        if str(text).strip().casefold().rstrip('.!?') != 'try again':
            return None
        slot = live(app)
        if (slot is None and captured is not None and captured[0] is getattr(app, '_hold_retry_audio', None)
                and time.monotonic() < captured[1]):
            slot = captured[0]
        if expected is not None and slot is not expected:
            return None
        if slot is not None:
            app._hold_retry_audio = None
            from samsara.hold_final_policy import current_audit
            audit = current_audit.get()
            if audit is not None:
                audit.retry_history_slot = slot
        return slot


def begin_capture(app):
    """A phrase begun inside the window may finish decoding afterwards."""
    from samsara.dictation_history import fence_capture
    fence_capture(app)
    slot = live(app)
    app._dictation_retry_capture = (slot, time.monotonic() + 30.) if slot is not None else None


def clear_note(app):
    """Clear the display at capture start; keep its bounded voice retry window."""
    indicator = getattr(app, 'listening_indicator', None)
    clear = getattr(indicator, 'clear_retry_notice', None)
    if callable(clear):
        app._schedule_ui(clear)


def show_note(app, *, empty=False, label=None):
    label = label or (EMPTY if empty else MISSED)
    slot = live(app)
    if slot is not None:
        slot.expires_at = time.monotonic() + NOTE_MS / 1000
    from samsara.dictation_history import retry_entry, bind_entry, link_for, history_label
    entry_id = retry_entry(app, slot, label)
    label = history_label(label, entry_id)
    indicator = getattr(app, 'listening_indicator', None)
    show = getattr(indicator, 'show_retry_notice', None)
    if callable(show):
        with bind_entry(app, entry_id):
            history = link_for(app, label)
        indicator._dictation_retry_app = app
        def display():
            callback = lambda: request(app, expected=slot) if slot is not None else _result_note(app, RETRY_EXPIRED)
            show(label, callback, **({'history': history} if history else {}))
        app._schedule_ui(display)
    else:
        show = getattr(app, '_show_outcome_chip', None) or getattr(indicator, 'show_outcome', None)
        if callable(show):
            with bind_entry(app, entry_id):
                show(label + ' - Try again', 'info', NOTE_MS)


def remaining_text(text, delivered):
    """A stripped run has no sub-segment timing: exclude its delivered words."""
    if not delivered:
        return text
    tokens = list(re.finditer(r"\w+(?:['’]\w+)*", text))
    known = re.findall(r"\w+(?:['’]\w+)*", delivered.casefold())
    matches = SequenceMatcher(None, known, [m.group().casefold() for m in tokens], autojunk=False)
    excluded = {i for block in matches.get_matching_blocks() for i in range(block.b, block.b + block.size)}
    return ' '.join(text[m.start():tokens[i + 1].start() if i + 1 < len(tokens) else len(text)].strip()
                    for i, m in enumerate(tokens) if i not in excluded).strip()


def decode_slot(app, slot, guard=None):
    from samsara import decode_recovery as decoder
    from samsara.hold_final_policy import current_audit
    result = decoder.DecodedHold()
    deadline, cancel = decoder._deadline(app, len(slot.audio) / app.model_rate), threading.Event()
    params = dict(slot.params, no_speech_threshold=None)
    token = current_audit.set(slot.audit or current_audit.get())
    try:
        for missed in slot.ranges:
            a, b = max(0, round(missed.start * app.model_rate)), min(len(slot.audio), round(missed.end * app.model_rate))
            part = decoder._transcribe(app, slot.audio[a:b], params, deadline, cancel, guard, recovery=True)
            if not part.segments and not part.missed:
                part.missed.append(MissedRange(0., (b - a) / app.model_rate))
            if missed.delivered:
                text = remaining_text(''.join(s.text for s in part.segments).strip(), missed.delivered)
                part.segments = decoder._replace_text(part.segments, text) if text else []
            result.segments.extend(decoder._offset(s, missed.start) for s in part.segments)
            result.info = part.info
            result.warning |= part.warning
            result.failures.extend((x + missed.start, y + missed.start, why) for x, y, why in part.failures)
            result.missed.extend(MissedRange(x.start + missed.start, x.end + missed.start, x.delivered)
                                 for x in part.missed)
    finally:
        current_audit.reset(token)
    return result


def request(app, text='try again', *, expected=None):
    slot = claim(app, text, expected=expected)
    if slot is None:
        if expected is not None:
            _result_note(app, RETRY_EXPIRED, slot=expected)
        return False
    clear_note(app)
    # Keep the take reachable even if a newer capture replaces the retry offer.
    app._last_dictation_retry = slot
    from samsara.runtime import thread_registry
    def failed(ranges):
        with _claim_lock:
            if getattr(app, '_hold_retry_audio', None) is None:
                decoder.retain(app, slot.audio, slot.params, 'retry_failed', ranges, source=slot)
            elif app._hold_retry_audio.hold_seq_id != slot.hold_seq_id:
                _result_note(app, RETRY_FAILED, slot=slot)
                return
        show_note(app, empty=True, label=RETRY_FAILED)
    def run():
        from samsara.dictation_history import capture_history
        with capture_history(app, slot):
            recover()
    def recover():
        from samsara import decode_recovery as decoder
        try:
            from samsara.transcript_gates import _apply_segment_quality_gates
            def guard(segments, text, params, pcm):
                quality = _apply_segment_quality_gates(segments, params, len(pcm) / app.model_rate,
                                                       audio=pcm, rate=app.model_rate)
                return (None, quality[0]) if quality[0] else ('segment_quality', '')
            decoded = decode_slot(app, slot, guard)
            text = ''.join(s.text for s in decoded.segments).strip()
            if text and not app._filter_dictation_language(text, decoded.info):
                failed(slot.ranges)
                return
            if text:
                for name in ('add_to_history', '_notify_main_window'):
                    callback = getattr(app, name, None)
                    if callable(callback):
                        callback(text)
                history = getattr(app, '_log_history', None)
                if callable(history):
                    history(raw_text=text, display_text=text, mode='streaming', status='recovered', entry_type='dictation')
                receipt = app._paste_preserving_clipboard(text, return_delivery_confirmation=True)
                sent, confirmed = receipt if isinstance(receipt, tuple) else (bool(receipt), bool(receipt))
                if not sent:
                    failed(slot.ranges)
                    return
                from samsara.clipboard import DeliveryOutcome
                from samsara.paste import show_delivery_result
                if not confirmed:
                    show_delivery_result(app, sent, DeliveryOutcome.UNVERIFIED)
            if decoded.missed or not text:
                failed(decoded.missed or slot.ranges)
            elif confirmed:
                _result_note(app, RETRY_DONE, 'success', slot=slot)
        except Exception:
            failed(slot.ranges)
    from samsara import decode_recovery as decoder
    try:
        thread_registry.spawn('dictation-retry', run, daemon=True)
    except Exception:
        failed(slot.ranges)
    return True


def _result_note(app, label, kind='info', *, slot=None):
    if slot is not None:
        from samsara.dictation_history import retry_entry, bind_entry, history_label
        entry_id = retry_entry(app, slot, label)
        with bind_entry(app, entry_id):
            return _result_note(app, history_label(label, entry_id), kind)
    show = getattr(app, '_show_outcome_chip', None)
    if callable(show):
        show(label, kind, NOTE_MS)
        return
    indicator = getattr(app, 'listening_indicator', None)
    show = getattr(indicator, 'show_outcome', None)
    if callable(show):
        app._schedule_ui(show, label, kind, NOTE_MS)
