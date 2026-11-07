"""1026: preserve accepted hold tails and count final decisions without text."""
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import logging
import re


@dataclass
class FinalAudit:
    decoded: int = 0
    dropped: dict = field(default_factory=dict)
    preview_tail_words: int = 0

    def drop(self, reason, count=1):
        self.dropped[reason] = self.dropped.get(reason, 0) + count


current_audit = ContextVar('hold_final_audit', default=None)


def final_decode(worker, audio, params):
    from samsara import mic_score
    from samsara import hold_diagnostics as evidence
    audit = FinalAudit()
    worker._session._hold_final_audit = audit
    audit._hold_evidence_1121 = evidence.get(worker._session) or evidence.bind(worker._session)
    audit.hold_seq_id = audit._hold_evidence_1121.hold_seq_id
    token = current_audit.set(audit)
    try:
        result = mic_score.stream_final(worker, audio, params)
        evidence.decoded(worker._session, result[2])
        return result
    finally:
        current_audit.reset(token)


def record_guard(reason, segments):
    audit = current_audit.get()
    if audit is not None and reason:
        audit.drop(reason, len(segments))


def log_delivery(session, text):
    from samsara.hold_diagnostics import prepare
    prepare(session, text)
    audit = getattr(session, '_hold_final_audit', None)
    log_audit(audit, text)


def log_audit(audit, text):
    if isinstance(audit, FinalAudit):
        joined = getattr(audit, '_hold_evidence_1121', None)
        values = joined.snapshot() if joined is not None else {}
        logging.getLogger('Samsara').info(
            '[HOLD_FINAL] segments_decoded=%d segments_dropped=%d reasons=%s '
            'preview_tail_words=%d chars_delivered=%d hold_seq_id=%s '
            'decoded_chars=%d decoded_words=%d request_words=%d', audit.decoded,
            sum(audit.dropped.values()), audit.dropped, audit.preview_tail_words, len(text or ''),
            getattr(audit, 'hold_seq_id', 'unknown'), values.get('decoded_chars', -1),
            values.get('decoded_words', -1), values.get('request_words', -1))
        from samsara.hold_diagnostics import tail_fields
        fields = tail_fields(audit)
        logging.getLogger('Samsara').info('[TAIL_EVIDENCE] ' + ' '.join(
            f'{key}={value}' for key, value in fields.items()))


def audited_batch(decode):
    @wraps(decode)
    def run(*args, **kwargs):
        audit = FinalAudit()
        from samsara.hold_diagnostics import get, decoded
        app = args[0] if args else kwargs.get('app')
        joined = get(app)
        if joined is not None:
            audit._hold_evidence_1121 = joined
            audit.hold_seq_id = joined.hold_seq_id
        token = current_audit.set(audit)
        text = ''
        try:
            result = decode(*args, **kwargs)
            text = result.text
            if joined is not None:
                decoded(app, text)
            return result
        finally:
            current_audit.reset(token)
            log_audit(audit, text)
    return run


def preserve_preview_tail(session, text):
    """The final wins unless its last chunk failed.
    Only a matching preview tail passing the loop gate may fill that failure.
    A rejected or conflicting preview never replaces final words.
    """
    from samsara.hallucination_filter import OUTRO_PHRASES
    from samsara.transcript_gates import _is_hallucinated_segments, _trim_trailing_garbage_run, gate_repetition_text
    preview = getattr(session, '_last_partial', '')
    if not getattr(session, '_hold_last_chunk_failed', False) or not isinstance(preview, str) or not text:
        return text
    audit = current_audit.get()
    final_words = list(re.finditer(r'\w+', text))
    preview_words = list(re.finditer(r'\w+', preview))
    if len(final_words) < 3 or len(preview_words) <= len(final_words):
        return text
    if [w.group().casefold() for w in final_words] != [
            w.group().casefold() for w in preview_words[:len(final_words)]]:
        return text
    suffix = preview[preview_words[len(final_words)].start():].strip()
    if not any(char.isalnum() for char in suffix):
        return text
    words = tuple(re.findall(r'\w+', suffix.casefold()))
    if (words in {tuple(re.findall(r'\w+', p)) for p in OUTRO_PHRASES}
            or gate_repetition_text(suffix).changed
            or _is_hallucinated_segments([], suffix)
            or _trim_trailing_garbage_run(suffix) != suffix):
        return text
    if audit is not None:
        audit.preview_tail_words = len(preview_words) - len(final_words)
    # Keep final spelling and punctuation inside the prefix. Its terminal
    # sentence punctuation is superseded by the continuation's punctuation.
    return text.rstrip().rstrip('.!?…') + ' ' + suffix


def segment_level(segment, audio, rate=16000):
    """Loudest 20 ms RMS, or None when acoustic evidence is unavailable."""
    import numpy as np
    try:
        start, end = float(segment.start), float(segment.end)
        rate = float(rate)
        if (not isinstance(audio, np.ndarray) or audio.ndim != 1 or rate <= 0
                or not 0 <= start < end <= len(audio) / rate):
            return None
        span = audio[int(start * rate):int(end * rate)]
        if not span.size or not np.isfinite(span).all():
            return None
        width = max(1, round(rate * .02))
        squares = np.square(span.astype(np.float64))
        return float(np.sqrt(max(np.mean(squares[i:i + width])
                                 for i in range(0, len(span), width))))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
