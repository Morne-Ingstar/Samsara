"""Bounded hold decoding and one retained, in-memory retry (618).

The native decoder cannot be interrupted through faster-whisper's API. A
deadline abandons its result, not its thread: the registered job keeps the
model lock until lazy iteration ends. Further holds can record; they never
create more native jobs while that one is outstanding.
"""
from dataclasses import dataclass, field
import threading
import time

import numpy as np

from samsara.log import get_logger
from samsara.runtime import thread_registry

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


def _deadline(app):
    default = 8.0 if _cpu(app) else 15.0
    try:
        seconds = float(app.config.get('dictation', {}).get('final_decode_timeout_s', default))
        if not np.isfinite(seconds):
            seconds = default
    except (AttributeError, TypeError, ValueError):
        seconds = default
    return _Deadline(time.monotonic() + min(30.0, max(.05, seconds)), app)


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
    result.update(temperature=.2 if retry else 0.0, best_of=1,
                  beam_size=min(3, result.get('beam_size', 3)),
                  max_new_tokens=min(tokens, result.get('max_new_tokens') or tokens))
    return result


def _params(app, params, audio_s, *, retry=False):
    result = dict(params)
    result['condition_on_previous_text'] = False
    if _cpu(app):
        # The single recovery pass IS the retry; no hidden temperature ladder.
        result = cpu_decode_params(result, audio_s, retry=retry)
    if retry:
        result.update(initial_prompt=None, temperature=.2 if _cpu(app) else 0.0,
                      vad_filter=False, condition_on_previous_text=False)
    return result


def _transcribe(app, audio, params, deadline, cancel):
    def decode(abandoned):
        segments, info = app.model.transcribe(audio, **params)
        kept = []
        segments = iter(segments)
        while True:
            if abandoned.is_set() or cancel.is_set() or _remaining(deadline) <= 0:
                raise TimeoutError('hold decode deadline or cancellation')
            try:
                segment = next(segments)
            except StopIteration:
                break
            if abandoned.is_set() or cancel.is_set() or _remaining(deadline) <= 0:
                raise TimeoutError('hold decode deadline or cancellation')
            kept.append(segment)
        return kept, info
    return _run(app, decode, deadline, cancel)


def _chunks(audio, rate):
    """One clean-slate re-decode in bounded 5 s chunks (no VAD clipping)."""
    width = max(1, int(rate * 5))
    return [audio[i:i + width] for i in range(0, len(audio), width)]


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
    """Recover only after the caller's whole-capture speech gate admitted PCM."""
    session = worker._session
    app = session.app
    session._fallback_notice_rejected = False
    session._decode_audio = audio
    session._decode_params = dict(params)
    deadline, cancel = _deadline(app), session.cancel_event
    params.update(_params(app, params, len(audio) / app.model_rate))
    try:
        segments, info = _transcribe(app, audio, params, deadline, cancel)
        text = ''.join(seg.text for seg in segments).strip()
        reason = worker._decoder_guard(segments, text, params, duration_s=len(audio) / app.model_rate)
        requested = _retry_request(app, text) if not reason else None
        if requested is not None:
            audio, params = requested.audio, dict(requested.params)
            session._decode_audio = audio
            reason = 'explicit_retry'
        if text and not reason:
            return segments, info, text
        partial = getattr(session, '_last_partial', '')
        if isinstance(partial, str) and partial and reason != 'explicit_retry':
            # Only on_partial's already-guarded text; never the rejected final.
            session._fallback_notice_rejected = True
            retain(app, audio, params, 'partial_only')
            return [], info, partial
        recovered, pieces = [], []
        retry_params = _params(app, params, 5, retry=True)
        for chunk in _chunks(audio, app.model_rate):
            segs, info = _transcribe(app, chunk, retry_params, deadline, cancel)
            piece = ''.join(seg.text for seg in segs).strip()
            if not piece or worker._decoder_guard(segs, piece, retry_params,
                                                  duration_s=len(chunk) / app.model_rate):
                raise ValueError('hold recovery rejected')
            recovered.extend(segs)
            pieces.append(piece)
        joined = ' '.join(pieces)
        if worker._decoder_guard(recovered, joined, retry_params, duration_s=len(audio) / app.model_rate):
            raise ValueError('combined hold recovery rejected')
        return recovered, info, joined
    except Exception as exc:
        reason = 'timeout' if isinstance(exc, TimeoutError) else 'decode_rejected'
        retain(app, audio, params, reason)
        session._decode_failure = UNCLEAR
        logger.info('[DECODE] hold recovery failed reason=%s', reason)
        return [], None, ''


def deliver_failure(session, duration_s):
    message = getattr(session, '_decode_failure', None)
    if not isinstance(message, str) or not message:
        return False
    logger.info('[STREAM] final failed; audio retained when complete')
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
        retain(app, session._decode_audio, session._decode_params, 'superseded')
    finally:
        lock.release()
    session.cancel()
    # _stop_in_flight may be a release worker waiting for this final.
    app._stop_in_flight = False


def decode_hold(app, audio, params, duration_s):
    """Batch free-form seam; leave Command/Ava paths with their own decoder."""
    from samsara.audio_tools import _HotkeyDecodeResult, _suspected_silent_data_loss
    from samsara.transcript_gates import _apply_segment_quality_gates
    cancel, deadline = threading.Event(), _deadline(app)
    effective = _params(app, params, duration_s)
    original = None
    try:
        segs, info = _transcribe(app, audio, effective, deadline, cancel)
        text, low = _apply_segment_quality_gates(segs, effective, duration_s)
        if text and not app._filter_dictation_language(text, info):
            return _HotkeyDecodeResult('', False, segs, getattr(info, 'language', None), 'hold_618', True)
        original = _HotkeyDecodeResult(text, low, segs, getattr(info, 'language', None), 'hold_618')
        requested = _retry_request(app, text)
        suspected = _suspected_silent_data_loss(text, audio, app.model_rate, duration_s)
        if text and requested is None and not suspected:
            return original
        if requested is not None:
            audio, params = requested.audio, requested.params
        pieces, recovered = [], []
        retry_params = _params(app, params, 5, retry=True)
        for chunk in _chunks(audio, app.model_rate):
            segs, info = _transcribe(app, chunk, retry_params, deadline, cancel)
            piece, low = _apply_segment_quality_gates(segs, retry_params, len(chunk) / app.model_rate)
            if not piece or not app._filter_dictation_language(piece, info):
                raise ValueError('hold recovery rejected')
            pieces.append(piece)
            recovered.extend(segs)
        text = ' '.join(pieces)
        if suspected and original.text and len(original.text) > len(text):
            return original
        return _HotkeyDecodeResult(text, low, recovered, getattr(info, 'language', None), 'hold_618')
    except Exception:
        if original is not None and original.text and requested is None:
            return original
        retain(app, audio, params, 'decode_failed')
        try:
            app._show_outcome_chip(UNCLEAR, 'warning')
        except Exception:
            pass
        return _HotkeyDecodeResult('', False, [], None, 'failed_618', True)
