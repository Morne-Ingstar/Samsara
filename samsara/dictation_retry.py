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
    """Exclude one delivered span, including changed words between its anchors."""
    if not delivered:
        return text
    tokens = list(re.finditer(r"\w+(?:['’]\w+)*", text))
    known = re.findall(r"\w+(?:['’]\w+)*", delivered.casefold())
    matches = SequenceMatcher(None, known, [m.group().casefold() for m in tokens], autojunk=False)
    stopwords = {'a', 'an', 'and', 'as', 'at', 'be', 'by', 'for', 'from', 'in', 'is',
                 'it', 'of', 'on', 'or', 'that', 'the', 'this', 'to', 'was', 'with'}
    anchors = [block for block in matches.get_matching_blocks() if block.size and
               (block.size > 1 or known[block.a] not in stopwords or len(known) == 1 and block.b == 0)]
    if not anchors:
        return text
    first, last = anchors[0], anchors[-1]
    # The equal/replace/insert opcodes BETWEEN the anchors all describe the
    # same delivered span. Extend only by the unmatched delivered edge length;
    # a trailing replace opcode may also contain genuinely undelivered words.
    leading, trailing = known[:first.a], known[last.a + last.size:]
    start = max(0, first.b - (len(leading) if any(w not in stopwords for w in leading) else 0))
    end = min(len(tokens), last.b + last.size +
              (len(trailing) if any(w not in stopwords for w in trailing) else 0))
    excluded = set(range(start, end))
    return ' '.join(text[m.start():tokens[i + 1].start() if i + 1 < len(tokens) else len(text)].strip()
                    for i, m in enumerate(tokens) if i not in excluded).strip()


def _retry_window(audio, rate, evidence, start, end):
    """Keep the normal long-take context, then expand to confirmed pauses."""
    from samsara import decode_recovery as decoder
    from samsara.audio_engine.capture_snapshot import spans
    if len(audio) > 20 * rate:
        # A short clause in a long take must retain the context its normal
        # final had. Nearest pauses alone can isolate it and change its words.
        chunks = [c for c in decoder.chunk_audio(audio, rate, evidence)
                  if c.core_start < end and c.core_end > start]
        if chunks:
            start, end = min(start, chunks[0].core_start), max(end, chunks[-1].core_end)
    left, right = max(0, start - round(.25 * rate)), min(len(audio), end + round(.25 * rate))
    for a, b in spans(audio):
        if a <= start < b:
            local = decoder._slice_evidence(evidence, rate, len(audio), a, b)
            pauses = decoder._pauses(local, rate, b - a)
            left = max((a + (x + y) // 2 for x, y in pauses if a + (x + y) // 2 <= start),
                       default=max(a, left))
        if a < end <= b:
            local = decoder._slice_evidence(evidence, rate, len(audio), a, b)
            pauses = decoder._pauses(local, rate, b - a)
            right = min((a + (x + y) // 2 for x, y in pauses if a + (x + y) // 2 >= end),
                        default=min(b, right))
    return left, right


def _range_segments(segments, start, end, duration):
    """Keep missed-range words only; context is recognition input, not output."""
    from copy import copy
    kept = []
    for segment in segments:
        a, b = getattr(segment, 'start', None), getattr(segment, 'end', None)
        if a is None or b is None:
            if start <= 0 and end >= duration:
                kept.append(segment)
            continue
        if a >= end or b <= start:
            continue
        words = getattr(segment, 'words', None)
        if words:
            words = [w for w in words if w.start < end and w.end > start]
            if not words:
                continue
            segment = copy(segment)
            segment.words, segment.text = words, ''.join(w.word for w in words)
            segment.start, segment.end = words[0].start, words[-1].end
        elif a < start or b > end:
            # Without word timing a boundary-crossing segment can include
            # delivered context. Let the exact-range fallback recover it.
            continue
        kept.append(segment)
    return kept


def decode_slot(app, slot, guard=None):
    from samsara import decode_recovery as decoder
    from samsara.hold_final_policy import current_audit
    result = decoder.DecodedHold()
    deadline, cancel = decoder._deadline(app, len(slot.audio) / app.model_rate), threading.Event()
    token = current_audit.set(slot.audit or current_audit.get())
    try:
        from samsara.speech_presence import measure
        evidence = measure(slot.audio, app.model_rate)
        for missed in slot.ranges:
            a, b = max(0, round(missed.start * app.model_rate)), min(len(slot.audio), round(missed.end * app.model_rate))
            if b <= a:
                result.missed.append(missed)
                continue
            if not missed.delivered and b - a <= 4.5 * app.model_rate and (a > 0 or b < len(slot.audio)):
                # A short, wholly missing segment has precise bounds and no
                # delivered anchors to remove neighboring words safely.
                left, right = a, b
            else:
                left, right = _retry_window(slot.audio, app.model_rate, evidence, a, b)
            params = dict(slot.params, word_timestamps=True)
            if right - left <= 4.5 * app.model_rate:
                params['no_speech_threshold'] = None
            part = decoder._transcribe(app, slot.audio[left:right], params, deadline, cancel, guard)
            part.segments = _range_segments(part.segments,
                                           (a - left) / app.model_rate, (b - left) / app.model_rate,
                                           (right - left) / app.model_rate)
            if missed.delivered:
                text = remaining_text(''.join(s.text for s in part.segments).strip(), missed.delivered)
                part.segments = decoder._replace_text(part.segments, text) if text else []
            if not part.segments:
                # One bounded recovery stage, sharing the original deadline.
                params = dict(slot.params, no_speech_threshold=None)
                if missed.delivered:
                    params['initial_prompt'] = ' '.join(missed.delivered.split()[-64:])
                part = decoder._transcribe(app, slot.audio[a:b], params, deadline, cancel, guard, recovery=True)
                left = a
                if missed.delivered:
                    text = remaining_text(''.join(s.text for s in part.segments).strip(), missed.delivered)
                    part.segments = decoder._replace_text(part.segments, text) if text else []
            if not part.segments and not part.missed:
                part.missed.append(MissedRange((a - left) / app.model_rate, (b - left) / app.model_rate))
            origin = left / app.model_rate
            result.segments.extend(decoder._offset(s, origin) for s in part.segments)
            result.info = part.info
            result.warning |= part.warning
            result.failures.extend((max(x + origin, missed.start), min(y + origin, missed.end), why)
                                   for x, y, why in part.failures if x + origin < missed.end and y + origin > missed.start)
            result.missed.extend(MissedRange(max(x.start + origin, missed.start),
                                            min(x.end + origin, missed.end), x.delivered)
                                 for x in part.missed if x.start + origin < missed.end and x.end + origin > missed.start)
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
