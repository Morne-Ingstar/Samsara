"""One VAD-chunked hold decoder, with bounded native work and retained PCM.

The native decoder cannot be interrupted through faster-whisper's API. A
deadline abandons its result, not its thread: the registered job keeps the
model lock until lazy iteration ends. Further holds can record; they never
create more native jobs while that one is outstanding.
"""
from dataclasses import dataclass, field
from copy import copy
import re
import threading
import time

import numpy as np

from samsara.log import get_logger
from samsara.vocabulary_decode import transcribe_with_vocabulary
from samsara.runtime import thread_registry
from samsara.hold_final_policy import audited_batch

logger = get_logger(__name__)
# Stand-ins: owner wording is collected in ASK.json.
UNCLEAR = 'Words unclear. Say try again.'
LOST = 'Audio lost. Dictate again.'
_admission = threading.Lock()


@dataclass
class RetainedAudio:
    audio: np.ndarray
    params: dict
    reason: str


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


def chunk_audio(audio, rate, evidence):
    """Keep the original timeline; disjoint cores, 250 ms context on each side."""
    size, pad, width = len(audio), round(.25 * rate), round(24.5 * rate)
    if size <= 20 * rate:
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
            if end - start >= .064 - 1e-9 or peak > .5:
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


def _append(kept, segments):
    """Deduplicate only matching text in the chunks' shared padding."""
    for segment in segments:
        item = segment
        if kept and getattr(item, 'start', float('inf')) < getattr(kept[-1], 'end', 0):
            left = re.findall(r'\w+', ''.join(s.text for s in kept).casefold())
            right = list(re.finditer(r'\w+', item.text))
            overlap = 0
            for width in range(1, min(len(left), len(right)) + 1):
                if left[-width:] == [w.group().casefold() for w in right[:width]]:
                    overlap = width
            if overlap:
                if overlap == len(right):
                    continue
                item = copy(item)
                item.text = ' ' + item.text[right[overlap].start():]
        kept.append(item)


@dataclass
class DecodedHold:
    segments: list = field(default_factory=list)
    info: object = None
    failures: list = field(default_factory=list)
    last_failed: bool = False
    warning: bool = False
    low: bool = False


def _transcribe(app, audio, params, deadline, cancel, guard=None, capture=None):
    from samsara.hold_final_policy import current_audit
    from samsara.speech_presence import measure
    from samsara.transcript_gates import gate_repetition_segments, gate_repetition_text
    audit = current_audit.get()

    def decode(abandoned):
        token = current_audit.set(audit)
        result = DecodedHold()
        def check():
            if abandoned.is_set() or cancel.is_set() or _remaining(deadline) <= 0:
                raise TimeoutError('hold decode deadline or cancellation')
        try:
            check()
            evidence = measure(audio, app.model_rate)
            bounds = _speech_bounds(evidence, app.model_rate, len(audio), capture=capture)
            if app.config.get('dictation', {}).get('speech_presence_gate', True) is False:
                bounds = None
            sparse = (len(audio) > 10 * app.model_rate and bounds is not None
                      and sum(b-a for a, b in bounds) < .15 * len(audio))
            chunks = chunk_audio(audio, app.model_rate, evidence)
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
                effective = _params(app, params, len(pcm) / app.model_rate)
                effective['word_timestamps'] = sparse or params.get('word_timestamps', False)
                reason, accepted = None, []
                try:
                    for retry in (False, True):
                        check()
                        if retry:
                            effective = _params(app, effective, len(pcm) / app.model_rate, retry=True)
                        iterator, result.info = transcribe_with_vocabulary(app, pcm, path='hold_final', **effective)
                        raw = []
                        for segment in iterator:
                            check()
                            raw.append(segment)
                        if not retry and _cpu(app):
                            # A resilient model may have switched devices during iteration.
                            effective['temperature'] = temperature_ladder(app)
                        missing_timing = len(chunks) > 1 and any(
                            getattr(s, 'start', None) is None or getattr(s, 'end', None) is None for s in raw)
                        raw = _clip_timing(raw, len(pcm) / app.model_rate)
                        screened = gate_repetition_segments(raw)
                        combined = gate_repetition_text(screened.text)
                        result.warning |= screened.changed or combined.changed
                        needs_retry = screened.needs_retry or combined.needs_retry
                        accepted = list(screened.segments)
                        if needs_retry:
                            reason = 'repetition_loop'
                            if not retry:
                                continue
                        else:
                            if combined.stripped:
                                accepted = _replace_text(accepted, combined.text)
                            if sparse:
                                accepted = _aligned(accepted, bounds, app.model_rate, chunk.start / app.model_rate)
                            text = ''.join(s.text for s in accepted).strip()
                            verdict = guard(accepted, text, effective, pcm) if text and guard else None
                            if isinstance(verdict, tuple):
                                reason, replacement = verdict
                                accepted = _replace_text(accepted, replacement)
                            else:
                                reason = verdict
                            reason = reason or ('empty_spoken_chunk' if not text else None)
                            reason = reason or ('missing_chunk_timestamps' if missing_timing else None)
                        break  # Only a loop may cause the one .4 re-decode.
                except TimeoutError:
                    raise
                except Exception:
                    reason = 'chunk_decode_failed'
                if reason:
                    start, end = chunk.core_start / app.model_rate, chunk.core_end / app.model_rate
                    result.failures.append((start, end, reason))
                    result.last_failed = index == len(chunks) - 1
                    result.warning = True
                    logger.warning('[HOLD_FINAL] failed start_s=%.3f end_s=%.3f reason=%s audio_retained=1', start, end, reason)
                    # Good segments survive a failed loop retry; the failed loop never does.
                    if reason not in ('repetition_loop', 'missing_chunk_timestamps'):
                        continue
                _append(result.segments, [_offset(s, chunk.start / app.model_rate) for s in accepted])
            return result
        finally:
            current_audit.reset(token)
    return _run(app, decode, deadline, cancel)


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


def retain(app, audio, params, reason):
    if audio is not None and len(audio):
        app._hold_retry_audio = RetainedAudio(np.array(audio, copy=True), dict(params), reason)


def _retry_request(app, text):
    literal = getattr(app, '_verbatim_rule', None)
    if callable(literal) and literal():
        return None
    retained = getattr(app, '_hold_retry_audio', None)
    return retained if text.strip().casefold().rstrip('.!?') == 'try again' else None


def capture_problem(app, audio):
    consumer = getattr(app, '_dictation_consumer', None)
    if getattr(consumer, 'capture_loss', False) is not True:
        return False
    try:
        app._show_outcome_chip(LOST, 'error')
    except Exception:
        logger.debug('[CAP] lost-audio feedback unavailable')
    return True


def stream_final(worker, audio, params):
    """Decode release PCM once per chunk; no whole-preview or 5 s recovery."""
    session, app = worker._session, worker._session.app
    session._fallback_notice_rejected = False
    session._decode_audio, session._decode_params = audio, dict(params)
    session._hold_failed_ranges, session._hold_last_chunk_failed = (), False
    session._hold_quality_warning, session._decode_failure = False, None
    deadline, cancel = _deadline(app, len(audio) / app.model_rate), session.cancel_event
    def guard(segments, text, effective, pcm):
        return worker._decoder_guard(segments, text, effective, duration_s=len(pcm) / app.model_rate)
    try:
        decoded = _transcribe(app, audio, params, deadline, cancel, guard, getattr(worker, '_speech_presence_capture', None))
        text = ''.join(s.text for s in decoded.segments).strip()
        requested = _retry_request(app, text) if not decoded.failures else None
        if requested is not None:
            audio, params = requested.audio, dict(requested.params)
            session._decode_audio, session._decode_params = audio, params
            deadline = _deadline(app, len(audio) / app.model_rate)
            decoded = _transcribe(app, audio, params, deadline, cancel, guard, getattr(worker, '_speech_presence_capture', None))
            text = ''.join(s.text for s in decoded.segments).strip()
        session._hold_failed_ranges = tuple(decoded.failures)
        session._hold_last_chunk_failed = decoded.last_failed
        session._hold_quality_warning = decoded.warning
        if decoded.failures:
            retain(app, audio, params, decoded.failures[0][2])
            if not text:
                from samsara.transcript_gates import LOOP_WARNING
                session._decode_failure = LOOP_WARNING
        from samsara.hold_final_policy import preserve_preview_tail
        return decoded.segments, decoded.info, preserve_preview_tail(session, text)
    except Exception as exc:
        reason = 'timeout' if isinstance(exc, TimeoutError) else 'decode_rejected'
        retain(app, audio, params, reason)
        session._hold_failed_ranges = ((0., len(audio) / app.model_rate, reason),)
        session._decode_failure = UNCLEAR
        logger.warning('[HOLD_FINAL] failed start_s=0 end_s=%.3f reason=%s audio_retained=1', len(audio) / app.model_rate, reason)
        return [], None, ''


def deliver_failure(session, duration_s):
    if getattr(session, '_hold_quality_warning', False):
        _warn(session.app)
        session._hold_quality_warning = False
    message = getattr(session, '_decode_failure', None)
    if not isinstance(message, str) or not message:
        return False
    logger.info('[STREAM] final failed; audio retained when complete')
    if getattr(session, '_direct_paste', False) and getattr(session, '_last_pasted', False):
        thread_registry.spawn('streaming-failed-undo', session._undo_direct_paste, daemon=True)
    overlay = session._overlay
    show = getattr(overlay, 'show_empty_result', None)
    if callable(show):
        show(message)
    else:
        from samsara.streaming import StreamingOverlayQt
        overlay.update_text(message, StreamingOverlayQt.STATE_DONE)
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
        text = ''.join(s.text for s in decoded.segments).strip()
        requested = _retry_request(app, text) if not decoded.failures else None
        if requested is not None:
            audio, params = requested.audio, dict(requested.params)
            deadline = _deadline(app, len(audio) / app.model_rate)
            decoded = _transcribe(app, audio, params, deadline, cancel, guard)
            text = ''.join(s.text for s in decoded.segments).strip()
        if decoded.failures:
            retain(app, audio, params, decoded.failures[0][2])
        if decoded.warning:
            _warn(app)
        language_rejected = bool(text and not app._filter_dictation_language(text, decoded.info))
        if language_rejected:
            text = ''
        return _HotkeyDecodeResult(text, low, decoded.segments, getattr(decoded.info, 'language', None),
                                   'hold_1052d', language_rejected or bool(decoded.failures and not text))
    except Exception:
        retain(app, audio, params, 'decode_failed')
        try:
            app._show_outcome_chip(UNCLEAR, 'warning')
        except Exception:
            pass
        return _HotkeyDecodeResult('', False, [], None, 'failed_1052d', True)
