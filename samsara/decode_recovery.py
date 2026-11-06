"""One VAD-chunked hold decoder, with bounded native work and retained PCM.

The native decoder cannot be interrupted through faster-whisper's API. A
deadline abandons its result, not its thread: the registered job keeps the
model lock until lazy iteration ends. Further holds can record; they never
create more native jobs while that one is outstanding.
"""
from dataclasses import dataclass, field
from copy import copy
import threading
import time

import numpy as np

from samsara.log import get_logger
from samsara.vocabulary_decode import transcribe_with_vocabulary
from samsara.runtime import thread_registry
from samsara.hold_final_policy import audited_batch

logger = get_logger(__name__)
# PLACEHOLDER COPY (1125)
UNCLEAR = "Didn't catch that - Try again"
LOST = "Didn't catch that - Try again"
# 1118: independent speech evidence required before bypassing a native skip.
NO_SPEECH_RETRY_TOTAL_S = .032
NO_SPEECH_RETRY_RUN_S = .032
_admission = threading.Lock()


@dataclass
class RetainedAudio:
    audio: np.ndarray
    params: dict
    reason: str
    ranges: tuple = ()
    expires_at: float = 0.
    hold_seq_id: str = ''
    audit: object = None
    history_entry_id: int | None = None


@dataclass
class _Job:
    done: threading.Event = field(default_factory=threading.Event)
    abandoned: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: Exception = None


def _cpu(app):
    return getattr(app, 'device_type', None) == 'cpu'


class _Deadline(float):
    """The cold CPU model load is preparation, not decoding budget (917b)."""
    def __new__(cls, value, app):
        from samsara.gpu_fallback import ResilientSpeechModel
        result = super().__new__(cls, value)
        model = getattr(app, 'model', None)
        result.model = model if isinstance(model, ResilientSpeechModel) else None
        result.load_seconds = result.model.recovery_load_seconds() if result.model else 0.0
        return result


def _remaining(deadline):
    loading = 0.0
    if isinstance(deadline, _Deadline) and deadline.model is not None:
        loading = max(0.0, deadline.model.recovery_load_seconds() - deadline.load_seconds)
    return float(deadline) - time.monotonic() + loading


def _deadline(app, audio_s=0):
    from math import ceil
    default = (8.0 if _cpu(app) else 15.0) * max(1, ceil(audio_s / 25))
    configured = None
    try:
        configured = app.config.get('dictation', {}).get('final_decode_timeout_s')
        seconds = float(default if configured is None else configured)
        if not np.isfinite(seconds):
            seconds = default
    except (AttributeError, TypeError, ValueError):
        seconds = default
    return _Deadline(time.monotonic() + min(120.0 if configured is None else 30.0, max(.05, seconds)), app)


def _run(app, operation, deadline, cancel):
    """Bound lock waiting AND lazy iteration; at most one outstanding job/app."""
    while True:
        if cancel.is_set() or _remaining(deadline) <= 0:
            raise TimeoutError('hold decode deadline or cancellation')
        with _admission:
            previous = getattr(app, '_hold_decode_job', None)
            if previous is None or previous.done.is_set():
                job = _Job()
                app._hold_decode_job = job
                break
        cancel.wait(min(.02, max(0, _remaining(deadline))))

    def work():
        acquired = False
        try:
            while not acquired:
                if job.abandoned.is_set() or cancel.is_set() or _remaining(deadline) <= 0:
                    return
                acquired = app.model_lock.acquire(timeout=min(.02, max(0, _remaining(deadline))))
            if job.abandoned.is_set() or cancel.is_set() or _remaining(deadline) <= 0:
                return
            job.result = operation(job.abandoned)
        except Exception as exc:
            job.error = exc
        finally:
            if acquired:
                app.model_lock.release()
            job.done.set()

    try:
        thread_registry.spawn('hold-final-decode', work, daemon=True)
    except Exception:
        job.done.set()
        raise
    while not job.done.wait(min(.02, max(0, _remaining(deadline)))):
        if cancel.is_set() or _remaining(deadline) <= 0:
            job.abandoned.set()
            logger.info('[DECODE] hold final abandoned; native completion will be ignored')
            raise TimeoutError('hold decode deadline or cancellation')
    if cancel.is_set() or _remaining(deadline) <= 0:
        raise TimeoutError('hold decode deadline or cancellation')
    if job.error is not None:
        raise job.error
    if job.result is None:
        raise TimeoutError('hold model lock deadline')
    return job.result


def cpu_decode_params(params, audio_s=None, *, retry=False):
    """Shared CPU bounds for ordinary holds and a CUDA fallback retry."""
    result = dict(params)
    tokens = 192 if audio_s is None else min(192, max(32, int(audio_s * 12 + 24)))
    temperature = (0.0, 0.2) if isinstance(params.get('temperature'), (tuple, list)) else .2 if retry else 0.0
    result.update(temperature=temperature, best_of=1,
                  beam_size=min(3, result.get('beam_size', 3)),
                  max_new_tokens=min(tokens, result.get('max_new_tokens') or tokens))
    return result


def _params(app, params, audio_s, *, retry=False):
    result = dict(params)
    result.update(condition_on_previous_text=False, vad_filter=False,
                  without_timestamps=False, temperature=temperature_ladder(app))
    if _cpu(app):
        result = cpu_decode_params(result, audio_s, retry=retry)
        result['beam_size'] = min(5, params.get('beam_size', 3))
        result['temperature'] = temperature_ladder(app)
    if retry:
        result['temperature'] = .4
    return result


def temperature_ladder(app):
    return (0.0, 0.2) if _cpu(app) else (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def _pauses(evidence, rate, samples):
    """Confirmed >=350 ms Silero runs below .1, in source sample coordinates."""
    if not _valid_evidence(evidence, rate, samples):
        return []
    pauses, first = [], None
    for index, score in enumerate((*evidence.probabilities, 1.)):
        if score < .1 and first is None:
            first = index
        elif score >= .1 and first is not None:
            a, b = first * 512 / 16000, min(index * 512, evidence.samples) / 16000
            if b - a >= .35:
                pauses.append((round(a * rate), round(b * rate)))
            first = None
    return pauses


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    core_start: int
    core_end: int


def chunk_audio(audio, rate, evidence, *, recovery=False):
    """Keep the original timeline; disjoint cores, 250 ms context on each side."""
    size, pad, width = len(audio), round(.25 * rate), round((4.5 if recovery else 24.5) * rate)
    if size <= (4.5 if recovery else 20) * rate:
        return [Chunk(0, size, 0, size)] if size else []
    pauses = _pauses(evidence, rate, size)
    chunks, start = [], 0
    while start < size:
        end = min(size, start + width)
        if end < size:
            seams = [min((a + b) // 2, end) for a, b in pauses
                     if start + rate <= (a + b) // 2 and a <= end]
            end = max(seams, default=end)
        chunks.append(Chunk(max(0, start - pad), min(size, end + pad), start, end))
        start = end
    return chunks


def _speech_bounds(evidence, rate, samples, capture=None):
    """Padded original-time speech islands, never compacted or amplified."""
    if not _valid_evidence(evidence, rate, samples):
        return None
    spans, first, peak = [], None, 0.
    boundary = max(0, round(capture.offsets()[2] * rate / 1000)) if capture is not None else 0
    for index, score in enumerate((*evidence.probabilities, 0.)):
        if score > .1:
            if first is None:
                first = index
            peak = max(peak, score)
        elif first is not None:
            end = min(index * 512, evidence.samples) / 16000
            start = first * 512 / 16000
            if end - start >= NO_SPEECH_RETRY_RUN_S - 1e-9 or peak > .5:
                a, b = max(boundary, round((start - .25) * rate)), min(samples, round((end + .25) * rate))
                if b <= a or end * rate <= boundary:
                    first, peak = None, 0.
                    continue
                if spans and a <= spans[-1][1]:
                    spans[-1] = (spans[-1][0], b)
                else:
                    spans.append((a, b))
            first, peak = None, 0.
    return spans


def _valid_evidence(evidence, rate, samples):
    scores = getattr(evidence, 'probabilities', ())
    return (getattr(evidence, 'known', False) and bool(scores)
            and evidence.samples == round(samples * 16000 / rate)
            and len(scores) == (evidence.samples + 511) // 512
            and np.isfinite(scores).all() and all(0 <= p <= 1 for p in scores))


def _replace_text(segments, text):
    """Keep telemetry for untouched segments; replace only an aggregate edit."""
    if not segments or text == ''.join(s.text for s in segments).strip():
        return segments
    item = copy(segments[0])
    item.text = ' ' + text
    item.end = getattr(segments[-1], 'end', getattr(item, 'end', 0.))
    item.words = None
    return [item] if text else []


def _offset(segment, seconds):
    item = copy(segment)
    for name in ('start', 'end'):
        value = getattr(segment, name, None)
        if value is not None:
            setattr(item, name, value + seconds)
    if getattr(segment, 'words', None):
        item.words = [_offset(word, seconds) for word in segment.words]
    return item


def _clip_timing(segments, duration):
    """Whisper timestamps may extend into its padded 30 s input window."""
    result = []
    for segment in segments:
        item = copy(segment)
        a, b = getattr(item, 'start', None), getattr(item, 'end', None)
        if a is not None and b is not None:
            if not np.isfinite([a, b]).all() or not 0 <= a <= b:
                raise ValueError('invalid chunk timestamps')
            item.start, item.end = min(a, duration), min(b, duration)
        result.append(item)
    return result


def _aligned(segments, bounds, rate, offset):
    """Sparse words need >=60% overlap with padded speech evidence (807)."""
    kept = []
    for segment in segments:
        words = []
        for word in getattr(segment, 'words', None) or ():
            a, b = word.start + offset, word.end + offset
            if np.isfinite([a, b]).all() and 0 <= a < b:
                overlap = sum(max(0., min(b, y / rate) - max(a, x / rate)) for x, y in bounds)
                if overlap + 1e-9 >= .6 * (b - a):
                    words.append(word)
        if words:
            item = copy(segment)
            item.words, item.text = words, ''.join(w.word for w in words)
            item.start, item.end = words[0].start, words[-1].end
            kept.append(item)
    return kept


@dataclass
class DecodedHold:
    segments: list = field(default_factory=list)
    info: object = None
    failures: list = field(default_factory=list)
    last_failed: bool = False
    warning: bool = False
    low: bool = False
    missed: list = field(default_factory=list)
    completed_s: float = 0.


def _guard_segments(segments, guard, params, pcm, origin, missed):
    from samsara.dictation_retry import MissedRange
    if not segments or not guard:
        return list(segments), None
    local_reasons = {'compression_ratio', 'fallback_exhausted', 'low_logprob_at_noise_floor',
                     'no_speech', 'segment_quality'}

    def verdict(items):
        try:
            value = guard(items, ''.join(s.text for s in items).strip(), params, pcm)
            return value if isinstance(value, tuple) else (value, None)
        except Exception:
            return 'segment_quality', None

    whole = verdict(segments)
    if whole[0] and whole[0] not in local_reasons:
        # Unknown/global vetoes apply to the result, not just its first segment.
        missed.append(MissedRange(origin + min(getattr(s, 'start', 0.) for s in segments),
                                  origin + max(getattr(s, 'end', 0.) for s in segments)))
        return [], whole[0]
    if not whole[0] and whole[1] is None:
        # 1130: a result the whole-hold guard accepts is kept as is. Re-judging each
        # segment alone dropped a low-confidence spoken tail the 1026 policy keeps.
        return list(segments), None
    kept, reason = [], None
    for item in segments:
        rejection, replacement = whole if len(segments) == 1 else verdict([item])
        if rejection:
            reason = rejection
            missed.append(MissedRange(origin + getattr(item, 'start', 0.),
                                      origin + getattr(item, 'end', 0.)))
        else:
            kept.extend(_replace_text([item], replacement) if replacement is not None else [item])
            if replacement is not None and replacement.strip() != item.text.strip():
                missed.append(MissedRange(origin + getattr(item, 'start', 0.),
                                          origin + getattr(item, 'end', 0.), replacement))
    return kept, reason


def _can_retry_no_speech(evidence, rate, samples, chunk, params, capture):
    """A native empty result exposes no skip telemetry; corroborate locally.

    Require one measured 32 ms speech frame above .1 in THIS
    source chunk, outside press/earcon grace. Unknown evidence fails closed.
    """
    if params.get('no_speech_threshold') is None or not _valid_evidence(evidence, rate, samples):
        return False
    grace = capture.offsets()[0] / 1000 if capture is not None else 0.
    start, end = max(chunk.start / rate, grace), chunk.end / rate
    total = longest = run = 0.
    for index, score in enumerate(evidence.probabilities):
        width = max(0., min(end, (index + 1) * .032, evidence.samples / 16000)
                    - max(start, index * .032))
        run = run + width if width and score > .1 else 0.
        total += width if score > .1 else 0.
        longest = max(longest, run)
    return total + 1e-9 >= NO_SPEECH_RETRY_TOTAL_S and longest + 1e-9 >= NO_SPEECH_RETRY_RUN_S


def _transcribe(app, audio, params, deadline, cancel, guard=None, capture=None, *, recovery=False, inline=None):
    from samsara.hold_final_policy import current_audit
    from samsara.speech_presence import measure
    from samsara.transcript_gates import gate_repetition_segments, gate_repetition_text
    audit = current_audit.get()
    from samsara.dictation_retry import MissedRange
    progress = DecodedHold()

    def decode(abandoned):
        token = current_audit.set(audit)
        result = DecodedHold()
        def check():
            if abandoned.is_set() or cancel.is_set() or _remaining(deadline) <= 0:
                raise TimeoutError('hold decode deadline or cancellation')
        try:
            check()
            evidence = measure(audio, app.model_rate)
            from samsara.hold_diagnostics import presence
            presence(audit, evidence, capture)
            bounds = _speech_bounds(evidence, app.model_rate, len(audio), capture=capture)
            if app.config.get('dictation', {}).get('speech_presence_gate', True) is False:
                bounds = None
            sparse = (len(audio) > 10 * app.model_rate and bounds is not None
                      and sum(b-a for a, b in bounds) < .15 * len(audio))
            chunks = chunk_audio(audio, app.model_rate, evidence, recovery=recovery)
            previous_chunk, previous_segments = None, []
            for index, chunk in enumerate(chunks):
                check()
                if bounds is not None and not any(a < chunk.end and b > chunk.start for a, b in bounds):
                    continue  # Confirmed silence is not a missing spoken range.
                if sparse:
                    # Keep a source view with 250 ms VAD context, not minutes of
                    # confirmed silence around one sentence. Never join islands.
                    spoken = [(a, b) for a, b in bounds if a < chunk.end and b > chunk.start]
                    chunk = Chunk(max(chunk.start, spoken[0][0]), min(chunk.end, spoken[-1][1]),
                                  chunk.core_start, chunk.core_end)
                pcm = audio[chunk.start:chunk.end]
                effective = _params(app, params, len(pcm) / app.model_rate, retry=recovery)
                missed_start = len(result.missed)
                effective['word_timestamps'] = len(chunks) > 1 or sparse or params.get('word_timestamps', False)
                reason, accepted, relaxed, chunk_warning = None, [], params.get('no_speech_threshold', .6) is None, False
                first_pass = None
                try:
                    for retry in (False, True):
                        check()
                        if retry and not relaxed:
                            effective = _params(app, effective, len(pcm) / app.model_rate, retry=True)
                        iterator, result.info = transcribe_with_vocabulary(app, pcm, path='hold_final', **effective)
                        raw = []
                        iteration_failed = False
                        try:
                            for segment in iterator:
                                check()
                                raw.append(segment)
                        except Exception:
                            if cancel.is_set():
                                raise
                            iteration_failed = True
                        if not retry and _cpu(app):
                            # A resilient model may have switched devices during iteration.
                            effective['temperature'] = temperature_ladder(app)
                        if not retry and not iteration_failed:
                            from samsara.borderline_confidence import recover
                            raw, result.info, effective = recover(app, pcm, effective, raw, result.info, check)
                        if (not raw and not retry and _can_retry_no_speech(
                                evidence, app.model_rate, len(audio), chunk, effective, capture)):
                            # One additional decode of the same PCM, under the same
                            # deadline. Compression/logprob and all output gates stay.
                            effective = dict(effective, no_speech_threshold=None)
                            relaxed = True
                            joined = getattr(audit, '_hold_evidence_1121', None)
                            if joined is not None:
                                joined.values['no_speech_retries'] += 1
                            logger.info('[HOLD_FINAL] no_speech_retry start_s=%.3f end_s=%.3f attempt=1 hold_seq_id=%s',
                                        chunk.start / app.model_rate, chunk.end / app.model_rate,
                                        getattr(audit, 'hold_seq_id', 'unknown'))
                            continue
                        missing_timing = len(chunks) > 1 and any(
                            getattr(s, 'start', None) is None or getattr(s, 'end', None) is None for s in raw)
                        raw = _clip_timing(raw, len(pcm) / app.model_rate)
                        screened = gate_repetition_segments(raw)
                        combined = gate_repetition_text(screened.text)
                        if audit is not None and (screened.changed or combined.changed):
                            audit.drop('repetition_strip', max(1, len(raw) - len(screened.segments)))
                        chunk_warning = screened.changed or combined.changed
                        needs_retry = screened.needs_retry or combined.needs_retry
                        accepted = list(screened.segments)
                        if needs_retry:
                            reason = 'repetition_loop'
                            if not retry:
                                first_pass = (list(raw), list(accepted))
                                continue
                            if combined.needs_retry:
                                accepted = []
                            accepted, _ = _guard_segments(accepted, guard, effective, pcm,
                                                         chunk.start / app.model_rate, result.missed)
                        else:
                            if combined.stripped:
                                accepted = _replace_text(accepted, combined.text)
                            if sparse:
                                accepted = _aligned(accepted, bounds, app.model_rate, chunk.start / app.model_rate)
                            text = ''.join(s.text for s in accepted).strip()
                            # A relaxed native skip must not admit known silence
                            # sign-offs, including in the batch path (1118).
                            from samsara.hallucination_filter import signoff_failure_reason
                            verdict = (signoff_failure_reason(text, accepted, len(pcm) / app.model_rate,
                                       app.config, user_held_key=True) if relaxed and text else None)
                            signoff = verdict
                            if signoff:
                                accepted = []
                            # 1125: quality rejection belongs to its segment, never
                            # the other successfully decoded words in this chunk.
                            accepted, reason = _guard_segments(accepted, guard, effective, pcm,
                                                               chunk.start / app.model_rate, result.missed)
                            if accepted:
                                reason = None
                            text = ''.join(s.text for s in accepted).strip()
                            reason = signoff or reason or ('empty_spoken_chunk' if not text else None)
                            reason = reason or ('missing_chunk_timestamps' if missing_timing else None)
                        if iteration_failed:
                            reason = 'chunk_decode_failed'
                        break  # At most one extra decode: loop OR native empty.
                except TimeoutError:
                    raise
                except Exception:
                    reason = 'chunk_decode_failed'
                if first_pass is not None and (not accepted or reason == 'chunk_decode_failed'):
                    # A failed loop retry cannot erase safe words from attempt one.
                    raw, fallback = first_pass
                    accepted, _ = _guard_segments(fallback, guard, effective, pcm,
                                                 chunk.start / app.model_rate, result.missed)
                    chunk_warning = True
                    reason = 'repetition_loop'
                if chunk_warning:
                    for item in raw:
                        matching = next((s for s in accepted if s is item or getattr(s, '_original', None) is item), None)
                        if matching is None or matching.text != item.text:
                            result.missed.append(MissedRange(
                                chunk.start / app.model_rate + getattr(item, 'start', 0.),
                                chunk.start / app.model_rate + getattr(item, 'end', len(pcm) / app.model_rate),
                                matching.text if matching is not None else ''))
                if reason and not accepted:
                    result.missed.append(MissedRange(chunk.core_start / app.model_rate, chunk.core_end / app.model_rate))
                if not recovery:
                    from samsara.hold_segment_recovery import recover_failed_ranges
                    accepted, reason, recovered = recover_failed_ranges(
                        app, audio, chunk, accepted, reason, result, missed_start,
                        params, deadline, cancel, guard, abandoned)
                    if recovered and not result.missed[missed_start:]:
                        chunk_warning = False
                from samsara.segment_speech_guard import screen
                accepted = screen(accepted, bounds, app.model_rate, chunk.start / app.model_rate, effective)
                if reason:
                    start, end = chunk.core_start / app.model_rate, chunk.core_end / app.model_rate
                    result.failures.append((start, end, reason))
                    result.last_failed = index == len(chunks) - 1
                    if reason == 'empty_spoken_chunk' and audit is not None:
                        audit.empty_spoken_chunk = True
                    logger.warning('[HOLD_FINAL] failed start_s=%.3f end_s=%.3f reason=%s audio_retained=1 hold_seq_id=%s',
                                   start, end, reason, getattr(audit, 'hold_seq_id', 'unknown'))
                    # Keep accepted segments even when iteration failed afterwards.
                    if not accepted and not result.missed[missed_start:]:
                        result.missed.append(MissedRange(start, end))
                    elif accepted and reason == 'chunk_decode_failed':
                        tail = max(getattr(s, 'end', 0.) for s in accepted) + chunk.start / app.model_rate
                        if tail < end:
                            result.missed.append(MissedRange(tail, end))
                result.warning |= chunk_warning
                from samsara.hold_text import append_chunk
                shifted = [_offset(s, chunk.start / app.model_rate) for s in accepted]
                shared = ((max(previous_chunk.start, chunk.start) / app.model_rate,
                           min(previous_chunk.end, chunk.end) / app.model_rate)
                          if previous_chunk is not None else None)
                append_chunk(result.segments, shifted, previous_segments, shared)
                previous_chunk, previous_segments = chunk, shifted
                nonlocal progress
                progress = DecodedHold(segments=list(result.segments), info=result.info,
                    failures=list(result.failures), missed=list(result.missed), warning=result.warning,
                    completed_s=chunk.core_end / app.model_rate)
            return result
        finally:
            current_audit.reset(token)
    try:
        result = decode(inline) if inline is not None else _run(app, decode, deadline, cancel)
    except TimeoutError:
        if cancel.is_set():
            raise
        snapshot = progress
        snapshot.failures.append((snapshot.completed_s, len(audio) / app.model_rate, 'timeout'))
        snapshot.missed.append(MissedRange(snapshot.completed_s, len(audio) / app.model_rate))
        snapshot.last_failed = True
        result = snapshot
    from samsara.hold_text import guard_segments
    result.segments = guard_segments(app, result.segments)
    return result


def partial_decode(worker, audio, params):
    """Freeze accepted segments at VAD pauses; decode only the bounded tail."""
    from samsara.speech_presence import measure
    from samsara.transcript_gates import gate_repetition_segments
    app, rate = worker._session.app, worker._session.app.model_rate
    prior = getattr(worker, '_confirmed_partial_segments', [])
    origin = getattr(worker, '_partial_origin', 0)
    previous = getattr(worker, '_partial_segments', [])
    if len(audio) > 15 * rate and previous:
        evidence = measure(audio[origin:], rate)
        seams = [(a + b) // 2 + origin for a, b in _pauses(evidence, rate, len(audio) - origin)
                 if a + origin > origin and b + origin <= getattr(worker, '_partial_end', 0)]
        for boundary in sorted(seams, reverse=True):
            if not any(getattr(s, 'start', -1) * rate < boundary < getattr(s, 'end', float('inf')) * rate for s in previous):
                frozen = [s for s in previous if getattr(s, 'end', float('inf')) * rate <= boundary]
                if frozen:
                    prior = prior + frozen
                    origin = boundary
                    previous = [s for s in previous if s not in frozen]
                    break
    if len(audio) - origin > 25 * rate:
        # Continuous speech has no pause: keep accepted words before a bounded tail.
        boundary = len(audio) - round(24.5 * rate)
        boundary = min((round(s.end * rate) for s in previous
                        if boundary <= getattr(s, 'end', -1) * rate <= len(audio)), default=boundary)
        frozen = [s for s in previous if getattr(s, 'end', float('inf')) * rate <= boundary]
        prior = prior + frozen
        origin = max(origin, boundary)
    iterator, info = transcribe_with_vocabulary(app, audio[origin:], path='hold_partial', **params)
    screened = gate_repetition_segments(_clip_timing(list(iterator), (len(audio) - origin) / rate))
    segments = list(screened.segments)
    text = ''.join(s.text for s in segments).strip()
    if screened.needs_retry or worker._decoder_guard(segments, text, params, partial=True):
        return None, info
    shifted = [_offset(s, origin / rate) for s in segments]
    worker._confirmed_partial_segments, worker._partial_origin = prior, origin
    worker._partial_segments, worker._partial_end = shifted, len(audio)
    return prior + shifted, info


def retain(app, audio, params, reason, ranges=None, *, source=None):
    if audio is not None and len(audio):
        from samsara.dictation_retry import MissedRange, NOTE_MS
        ranges = ranges or [MissedRange(0., len(audio) / app.model_rate)]
        ordered = []
        for part in sorted(set(ranges), key=lambda x: (x.start, x.end)):
            if ordered and part.start <= ordered[-1].end and not part.delivered and not ordered[-1].delivered:
                ordered[-1] = MissedRange(ordered[-1].start, max(part.end, ordered[-1].end))
            else:
                ordered.append(part)
        from samsara.hold_final_policy import current_audit, FinalAudit
        from samsara.hold_diagnostics import get
        from uuid import uuid4
        audit = getattr(source, 'audit', None) or current_audit.get() or FinalAudit()
        joined = getattr(audit, '_hold_evidence_1121', None) or get(app)
        audit.hold_seq_id = (getattr(source, 'hold_seq_id', '') or getattr(audit, 'hold_seq_id', '')
                             or getattr(joined, 'hold_seq_id', '') or uuid4().hex)
        if joined is not None:
            audit._hold_evidence_1121 = joined
        app._hold_retry_audio = RetainedAudio(np.array(audio, copy=True), dict(params), reason,
            tuple(ordered), time.monotonic() + NOTE_MS / 1000, audit.hold_seq_id, audit)
        app._hold_retry_audio.history_entry_id = getattr(source, 'history_entry_id', None)


def _retry_request(app, text):
    from samsara.dictation_retry import claim
    return claim(app, text)


def capture_problem(app, audio, session=None):
    """Capture gaps are evidence, never a veto on the PCM that arrived."""
    consumer = getattr(app, '_dictation_consumer', None)
    loss = getattr(consumer, 'capture_loss', False) is True
    app._capture_loss_1125 = loss and audio is not None and bool(len(audio))
    if loss:
        from samsara.audio_engine.frame import FRAME_MS
        frames = int(getattr(consumer, 'dropped_frames', 0) or 0)
        logger.info('[CAP] gap dropped_frames=%d known_gap_ms=%d overflow_gap_ms=unknown captured_samples=%d; decoding captured samples',
                    frames, frames * FRAME_MS, len(audio) if audio is not None else 0)
        incident = getattr(session, '_incident_1052a', None)
        if incident is not None:
            incident['signals'].add('capture_gap')
    return False


def _capture_omission(app, decoded, audio):
    if getattr(app, '_capture_loss_1125', False):
        from samsara.dictation_retry import MissedRange
        text = ''.join(s.text for s in decoded.segments).strip()
        # Confirmed silence is still silent, even when the device overflowed.
        if text or decoded.failures or decoded.missed:
            if not decoded.missed:
                decoded.missed.append(MissedRange(0., len(audio) / app.model_rate, text))
            decoded.warning = True
        app._capture_loss_1125 = False


def stream_final(worker, audio, params):
    """Decode release PCM once per chunk; no whole-preview or 5 s recovery."""
    session, app = worker._session, worker._session.app
    session._fallback_notice_rejected = False
    session._decode_audio, session._decode_params = audio, dict(params)
    session._hold_failed_ranges, session._hold_last_chunk_failed = (), False
    session._hold_quality_warning, session._decode_failure, session._hold_low_confidence = False, None, False
    deadline, cancel = _deadline(app, len(audio) / app.model_rate), session.cancel_event
    def guard(segments, text, effective, pcm):
        return worker._decoder_guard(segments, text, effective, duration_s=len(pcm) / app.model_rate, audio=pcm)
    requested = None
    try:
        decoded = _transcribe(app, audio, params, deadline, cancel, guard, getattr(worker, '_speech_presence_capture', None))
        _capture_omission(app, decoded, audio)
        text = ''.join(s.text for s in decoded.segments).strip()
        requested = _retry_request(app, text) if not decoded.failures else None
        if requested is not None:
            from samsara.dictation_retry import decode_slot
            audio, params = requested.audio, dict(requested.params)
            session._decode_audio, session._decode_params = audio, params
            decoded = decode_slot(app, requested, guard)
            text = ''.join(s.text for s in decoded.segments).strip()
        session._hold_failed_ranges = tuple(decoded.failures)
        session._hold_last_chunk_failed = decoded.last_failed
        session._hold_quality_warning = bool(decoded.missed or decoded.warning)
        if decoded.failures or decoded.missed:
            retain(app, audio, params, decoded.failures[0][2] if decoded.failures else 'repetition_loop',
                   decoded.missed if text else None, source=requested)
            if not text:
                session._decode_failure = UNCLEAR
        elif decoded.warning:
            retain(app, audio, params, 'repetition_loop', source=requested)
        from samsara.borderline_confidence import low_confidence
        session._hold_low_confidence = low_confidence(decoded.segments, params)
        from samsara.hold_final_policy import preserve_preview_tail
        return decoded.segments, decoded.info, preserve_preview_tail(session, text)
    except Exception as exc:
        reason = 'timeout' if isinstance(exc, TimeoutError) else 'decode_rejected'
        retain(app, audio, params, reason, source=requested)
        session._hold_failed_ranges = ((0., len(audio) / app.model_rate, reason),)
        session._decode_failure = UNCLEAR
        joined = getattr(session, '_hold_evidence_1121', None)
        logger.warning('[HOLD_FINAL] failed start_s=0 end_s=%.3f reason=%s audio_retained=1 hold_seq_id=%s',
                       len(audio) / app.model_rate, reason, getattr(joined, 'hold_seq_id', 'unknown'))
        return [], None, ''


def deliver_failure(session, duration_s):
    from samsara.dictation_retry import show_note
    if getattr(session, '_hold_quality_warning', False):
        show_note(session.app, empty=bool(getattr(session, '_decode_failure', None)))
        session._hold_quality_warning = False
        session._retry_note_shown_1125 = True
    message = getattr(session, '_decode_failure', None)
    if not isinstance(message, str) or not message:
        return False
    logger.info('[STREAM] final failed; audio retained when complete')
    if getattr(session, '_direct_paste', False) and getattr(session, '_last_pasted', False):
        thread_registry.spawn('streaming-failed-undo', session._undo_direct_paste, daemon=True)
    if not getattr(session, '_retry_note_shown_1125', False):
        show_note(session.app, empty=True)
        session._retry_note_shown_1125 = True
    overlay = session._overlay
    show = getattr(overlay, 'show_empty_result', None)
    if callable(show):
        show('')
    else:
        from samsara.streaming import StreamingOverlayQt
        overlay.update_text('', StreamingOverlayQt.STATE_DONE)
    try:
        session.app._log_history(raw_text='', display_text=message,
                                duration_ms=int(duration_s * 1000), mode='streaming', status='failed')
    except Exception:
        pass
    session._mark_done()
    overlay.flash_done_and_fade(session._mark_done)
    return True


def _warn(app):
    from samsara.transcript_gates import RepetitionResult, emit_loop_warning
    try:
        emit_loop_warning(app, RepetitionResult('', dropped=1))
    except Exception:
        logger.debug('[HOLD_FINAL] quality warning unavailable')


def supersede_final(app):
    """A fresh hold can cancel a decode after capture has fully stopped."""
    session = getattr(app, '_streaming_session', None)
    if session is None or getattr(app, 'recording', False):
        return
    if getattr(session, '_state', None) != 'finalizing':
        return
    # Never steal an accumulator while its final snapshot is being made.
    lock = getattr(session, '_capture_cleanup_lock', None)
    if lock is None or not lock.acquire(blocking=False):
        return
    try:
        if not session._capture_cleaned or getattr(session, '_decode_audio', None) is None:
            return
        from samsara.hold_recording_recovery import retain_stopped
        retain_stopped(session, 'superseded')
    finally:
        lock.release()
    session.cancel()
    # _stop_in_flight may be a release worker waiting for this final.
    app._stop_in_flight = False


@audited_batch
def decode_hold(app, audio, params, duration_s):
    """Batch and streaming share chunking, temperature policy and loop retry."""
    from samsara.audio_tools import _HotkeyDecodeResult
    from samsara.transcript_gates import _apply_segment_quality_gates
    cancel, deadline = threading.Event(), _deadline(app, duration_s)
    requested = None
    low = False
    def guard(segments, text, effective, pcm):
        nonlocal low
        quality = _apply_segment_quality_gates(segments, effective, len(pcm) / app.model_rate,
                                               audio=pcm, rate=app.model_rate)
        low |= quality[1]
        if not quality[0]:
            return 'segment_quality', ''
        return None, quality[0]
    try:
        decoded = _transcribe(app, audio, params, deadline, cancel, guard)
        _capture_omission(app, decoded, audio)
        text = ''.join(s.text for s in decoded.segments).strip()
        requested = _retry_request(app, text) if not decoded.failures else None
        if requested is not None:
            from samsara.dictation_retry import decode_slot
            audio, params = requested.audio, dict(requested.params)
            decoded = decode_slot(app, requested, guard)
            text = ''.join(s.text for s in decoded.segments).strip()
        if decoded.failures or decoded.missed:
            retain(app, audio, params, decoded.failures[0][2] if decoded.failures else 'repetition_loop',
                   decoded.missed if text else None, source=requested)
        elif decoded.warning:
            retain(app, audio, params, 'repetition_loop', source=requested)
        language_rejected = bool(text and not app._filter_dictation_language(text, decoded.info))
        if language_rejected:
            text = ''
        if (decoded.warning or decoded.failures or decoded.missed) and not language_rejected:
            from samsara.dictation_retry import show_note
            show_note(app, empty=not text)
        return _HotkeyDecodeResult(text, low, decoded.segments, getattr(decoded.info, 'language', None),
                                   'hold_1052d', language_rejected or bool(decoded.failures and not text))
    except Exception:
        retain(app, audio, params, 'decode_failed', source=requested)
        try:
            from samsara.dictation_retry import show_note
            show_note(app, empty=True)
        except Exception:
            pass
        return _HotkeyDecodeResult('', False, [], None, 'failed_1052d', True)
