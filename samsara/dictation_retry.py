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
_claim_lock = threading.Lock()


class CompactRetryNotice:
    """Compact display hooks; share the retry lifetime and ownership here."""

    def show_retry_notice(self, label, callback):
        from samsara.live_surface.feedback import Feedback, FeedbackKind
        self._dictation_retry_callback = callback
        self.show_feedback(label, 'accent', NOTE_MS, Feedback(FeedbackKind.INFORMATION, owner=OWNER))

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


def claim(app, text='try again'):
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
        if slot is not None:
            app._hold_retry_audio = None
        return slot


def begin_capture(app):
    """A phrase begun inside the window may finish decoding afterwards."""
    slot = live(app)
    app._dictation_retry_capture = (slot, time.monotonic() + 30.) if slot is not None else None


def clear_note(app):
    """Clear the display at capture start; keep its bounded voice retry window."""
    indicator = getattr(app, 'listening_indicator', None)
    clear = getattr(indicator, 'clear_retry_notice', None)
    if callable(clear):
        app._schedule_ui(clear)


def show_note(app, *, empty=False):
    label = EMPTY if empty else MISSED
    slot = live(app)
    if slot is not None:
        slot.expires_at = time.monotonic() + NOTE_MS / 1000
    indicator = getattr(app, 'listening_indicator', None)
    show = getattr(indicator, 'show_retry_notice', None)
    if callable(show):
        indicator._dictation_retry_app = app
        app._schedule_ui(show, label, lambda: request(app))
    else:
        show = getattr(app, '_show_outcome_chip', None) or getattr(indicator, 'show_outcome', None)
        if callable(show):
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
    result = decoder.DecodedHold()
    deadline, cancel = decoder._deadline(app, len(slot.audio) / app.model_rate), threading.Event()
    params = dict(slot.params, no_speech_threshold=None)
    for missed in slot.ranges:
        a, b = round(missed.start * app.model_rate), round(missed.end * app.model_rate)
        part = decoder._transcribe(app, slot.audio[a:b], params, deadline, cancel, guard)
        if not part.segments and not part.missed:
            part.missed.append(MissedRange(0., (b - a) / app.model_rate))
        if missed.delivered:
            text = remaining_text(''.join(s.text for s in part.segments).strip(), missed.delivered)
            part.segments = decoder._replace_text(part.segments, text) if text else []
        result.segments.extend(part.segments)
        result.info = part.info
        result.warning |= part.warning
        result.failures.extend((x + missed.start, y + missed.start, why) for x, y, why in part.failures)
        result.missed.extend(MissedRange(x.start + missed.start, x.end + missed.start, x.delivered)
                             for x in part.missed)
    return result


def request(app, text='try again'):
    slot = claim(app, text)
    if slot is None:
        return False
    clear_note(app)
    from samsara.runtime import thread_registry
    def run():
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
                decoder.retain(app, slot.audio, slot.params, 'retry_failed', slot.ranges)
                show_note(app, empty=True)
                return
            if text:
                delivered = app._paste_preserving_clipboard(text)
                if delivered is False:
                    decoder.retain(app, slot.audio, slot.params, 'delivery_failed', slot.ranges)
                    show_note(app, empty=True)
                    return
            if decoded.missed:
                decoder.retain(app, slot.audio, slot.params, 'retry_failed', decoded.missed)
                show_note(app, empty=not text)
        except Exception:
            decoder.retain(app, slot.audio, slot.params, 'retry_failed', slot.ranges)
            show_note(app, empty=True)
    thread_registry.spawn('dictation-retry', run, daemon=True)
    return True
