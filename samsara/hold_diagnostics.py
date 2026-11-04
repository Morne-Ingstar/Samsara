"""1121: numeric observations of one original hold; never retain its words."""
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from itertools import count
import logging
import threading
import time
import uuid


_sequence = count(1)
_process = uuid.uuid4().hex[:8]
_active = None
delivery_context = ContextVar('hold_delivery_1121', default=None)
STAGES = ('none', 'cleanup', 'formatting', 'corrections', 'paste_abort')


def best_effort(observation):
    @wraps(observation)
    def run(*args, **kwargs):
        try:
            return observation(*args, **kwargs)
        except Exception:
            logging.getLogger('Samsara').warning('[HOLD_EVIDENCE] observation_failed=1')
    return run


def words(text):
    return len((text or '').split())


@dataclass
class Evidence:
    hold_seq_id: str = field(default_factory=lambda: f'{next(_sequence)}-{_process}')
    values: dict = field(default_factory=lambda: dict(
        captured_audio_ms=-1, release_to_stop_ms=-1, last_speech_end_ms=-1,
        last_speech_source='unknown', cutoff_set=-1, cue_or_duck_overlap_ms=0,
        cue_events=0, duck_events=0, speech_present=-1,
        decoded_chars=-1, decoded_words=-1, request_chars=-1, request_words=-1,
        delivered_words=-1, delivery_abort_reason='pending', delivery_confirmed=-1,
        delivery_stage='none', cleanup_word_delta=0, formatting_word_delta=0,
        corrections_word_delta=0, paste_abort_word_delta=0, no_speech_retries=0))
    lock: object = field(default_factory=threading.RLock)
    intervals: list = field(default_factory=list)
    stopped: float = None
    bound: bool = False

    def snapshot(self):
        with self.lock:
            spans = sorted((a, min(b, self.stopped) if self.stopped is not None else b)
                           for a, b in self.intervals)
            total, end = 0., float('-inf')
            for start, stop in spans:
                total += max(0., stop - max(start, end))
                end = max(end, stop)
            self.values['cue_or_duck_overlap_ms'] = round(total * 1000)
            return dict(hold_seq_id=self.hold_seq_id, **self.values)


def begin(app):
    global _active
    evidence = Evidence()
    app._hold_evidence_1121 = evidence
    _active = (app, evidence)
    return evidence


def bind(session):
    evidence = getattr(session.app, '_hold_evidence_1121', None)
    if not isinstance(evidence, Evidence) or evidence.bound:
        evidence = begin(session.app)
    evidence.bound = True
    session._hold_evidence_1121 = evidence
    return evidence


def get(owner):
    evidence = getattr(owner, '_hold_evidence_1121', None)
    if isinstance(evidence, Evidence):
        return evidence
    evidence = getattr(getattr(owner, '_session', None), '_hold_evidence_1121', None)
    return evidence if isinstance(evidence, Evidence) else None


def cue(app, duration_s):
    evidence = get(app)
    if evidence is not None and getattr(app, 'recording', False) and evidence.stopped is None:
        now = time.monotonic()
        with evidence.lock:
            evidence.intervals.append((now, now + max(0., duration_s)))
            evidence.values['cue_events'] += 1


def observe_duck(operation):
    """Time duck engage/restore work only when a hold is recording."""
    @wraps(operation)
    def run(*args, **kwargs):
        active = _active
        evidence = (active[1] if active is not None and
                    getattr(active[0], 'recording', False) and active[1].stopped is None else None)
        started = time.monotonic()
        try:
            return operation(*args, **kwargs)
        finally:
            if evidence is not None:
                with evidence.lock:
                    evidence.intervals.append((started, time.monotonic()))
                    evidence.values['duck_events'] += 1
    return run


@best_effort
def presence(owner, result, grace_ms=0):
    evidence = get(owner)
    if evidence is None:
        return
    if not isinstance(grace_ms, (int, float)):
        try:
            grace_ms = grace_ms.offsets()[0]
        except (AttributeError, ValueError):
            grace_ms = 0
    with evidence.lock:
        evidence.values['speech_present'] = int(result.passes(grace_ms)) if result.known else -1
        probabilities = getattr(result, 'probabilities', ())
        if result.known and probabilities:
            end = max((min(result.samples, (i + 1) * 512) / 16
                       for i, p in enumerate(probabilities) if p > .1), default=0)
            evidence.values.update(last_speech_end_ms=round(end), last_speech_source='vad_p01')


def captured(consumer, frames, audit):
    global _active
    evidence = get(consumer)
    if evidence is None:
        evidence = Evidence()
        consumer._hold_evidence_1121 = evidence
    import numpy as np
    from samsara.audio_engine.frame import FRAME_MS
    levels = [float(np.sqrt(np.mean(frame * frame))) for frame in frames]
    with evidence.lock:
        evidence.stopped = time.monotonic()
        evidence.values.update(captured_audio_ms=len(frames) * FRAME_MS,
            release_to_stop_ms=round(max(0., evidence.stopped - audit['released_at']) * 1000),
            cutoff_set=int(consumer._capture_not_after is not None))
        if evidence.values['last_speech_source'] != 'vad_p01':
            evidence.values.update(last_speech_end_ms=max(
                (i + 1 for i, level in enumerate(levels) if level >= audit['threshold']), default=0) * FRAME_MS,
                last_speech_source='energy_tail_floor')
    if _active is not None and _active[1] is evidence:
        _active = None
    return evidence.snapshot()


def decoded(session, text):
    evidence = get(session) or bind(session)
    with evidence.lock:
        evidence.values.update(decoded_chars=len(text or ''), decoded_words=words(text),
                               request_chars=len(text or ''), request_words=words(text))


def stage(session, name, before, after):
    evidence = get(session)
    if evidence is not None:
        delta = words(after) - words(before)
        with evidence.lock:
            evidence.values[name + '_word_delta'] += delta
            evidence.values.update(request_chars=len(after or ''), request_words=words(after))
            if delta:
                evidence.values['delivery_stage'] = name
    return after


def prepare(session, text):
    evidence = get(session) or bind(session)
    with evidence.lock:
        evidence.values.update(request_chars=len(text or ''), request_words=words(text))
        if not text:
            evidence.values.update(delivered_words=0, delivery_abort_reason='empty_final')


@best_effort
def receipt(session, text, sent, confirmed, reason=None):
    evidence = get(session)
    if evidence is None:
        return
    with evidence.lock:
        evidence.values.update(request_chars=len(text or ''), request_words=words(text),
            delivered_words=words(text) if sent and confirmed else -1 if sent else 0,
            delivery_confirmed=int(bool(confirmed)),
            delivery_abort_reason=reason or ('none' if confirmed and sent else 'unverified' if sent else 'paste_failed'))
        evidence.values['paste_abort_word_delta'] = -words(text) if not sent else 0
        if not sent:
            evidence.values['delivery_stage'] = 'paste_abort'
    fields = evidence.snapshot()
    logging.getLogger('Samsara').info('[DELIVERY] ' + ' '.join(f'{k}={v}' for k, v in fields.items()))
    from samsara.hold_recording_recovery import update_incident
    update_incident(session)


def paste_call(session, text, operation, **kwargs):
    token = delivery_context.set(session)
    try:
        result = operation(text, **kwargs)
        # Some synthetic/legacy clients return only a boolean: it is dispatch,
        # never acknowledgement. The real paste seam reports before returning.
        evidence = get(session)
        if evidence is not None and evidence.values['delivery_abort_reason'] == 'pending':
            receipt(session, text, bool(result[0]) if isinstance(result, tuple) else result is not False, False)
        return result
    except Exception:
        receipt(session, text, False, False, 'paste_exception')
        raise
    finally:
        delivery_context.reset(token)


def paste_result(text, sent, confirmed, reason=None):
    session = delivery_context.get()
    if session is not None:
        receipt(session, text, sent, confirmed, reason)


@best_effort
def finished(session):
    evidence = get(session)
    if evidence is None:
        return
    if evidence.values['delivery_abort_reason'] == 'pending':
        reason = ('cancelled' if session.cancel_event.is_set() else
                  'decode_failed' if getattr(session, '_decode_failure', None) else 'not_requested')
        # Keep counts only; receipt() must not need the original request again.
        with evidence.lock:
            evidence.values.update(delivered_words=0, delivery_abort_reason=reason,
                                   delivery_stage='paste_abort')
            evidence.values['paste_abort_word_delta'] = -max(0, evidence.values['request_words'])
        logging.getLogger('Samsara').info('[DELIVERY] ' + ' '.join(
            f'{k}={v}' for k, v in evidence.snapshot().items()))
    from samsara.hold_recording_recovery import update_incident
    update_incident(session)
