"""1258: bounded automatic repair of the untouched trailing dictated span.

Originals and speech PCM stay in 1251's capped store. This state holds indices,
not another audio history. Decode uses the existing two-argument callback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
import logging
import re
import threading
import time

from samsara import draft_reread
from samsara.session_text import (chunk_ends_terminal, redecode_is_poorer,
                                span_needs_redecode, stitch_span_audio)


log = logging.getLogger(__name__)
CAP_S = 18.0  # Leaves room for compressed seams below the decoder's 20 s cap.
SENTENCE_S = 10.0
CUDA_SETTLE_S = .350
CPU_PAUSE_S = 1.8
POLL_S = .050


def _source(manager):
    doc = manager.draft_document
    editor = getattr(manager, '_draft_command_editor', None)
    cursor = (editor.key, editor.cursor, editor.anchor) if editor is not None else None
    return manager._session_epoch, doc.document_id, doc.revision, doc.text, cursor


def _app(manager):
    return (getattr(manager._agent_dispatch_fn, '__self__', None)
            or getattr(manager._commit_redecode_fn, '__self__', None))


def _activity(manager):
    # The capture path already records onset before decoding/dispatch. The
    # monotonic counter catches speech that starts AND ends during a decode.
    from samsara import execution_policy as ep
    with ep._speech.lock:
        onset = ep._speech.last_onset
        pending = onset > ep._speech.last_dispatch_start > 0
    return onset, pending or bool(getattr(_app(manager), 'is_speaking', False))


@dataclass
class Span:
    source: tuple
    prefix: str
    start: int
    durations: list = field(default_factory=list)
    uncertain: bool = False
    closing: bool = False
    generation: int = 0
    timer: object = None
    attempted: int = -1


def managed(manager):
    span = getattr(manager, '_open_span', None)
    return span is not None and span.source[:2] == _source(manager)[:2]


def can_join(manager):
    """A decoder seam may only remove punctuation inside an unsealed span."""
    return not managed(manager) or bool(manager._open_span.durations)


def _cancel(span):
    span.generation += 1
    if span.timer is not None:
        span.timer.cancel()
        span.timer = None


def seal(manager):
    if not managed(manager):
        return
    span = manager._open_span
    _cancel(span)
    span.source = _source(manager)
    span.prefix = manager.dictate_pending_buffer
    span.start = len(manager._dictate_pending_audio)
    span.durations.clear()
    span.uncertain = span.closing = False
    span.attempted = -1


def _speech_clip(manager, index):
    refs = manager._dictate_pending_audio
    if index >= len(refs) or refs[index] is None:
        return None
    ref = refs[index]
    entry = getattr(manager, '_reread_pcm', {}).get(id(ref))
    return entry[1] if entry is not None and entry[0]() is ref else None


def _duration(manager, audio):
    try:
        return len(audio) / _app(manager).model_rate
    except (AttributeError, TypeError, ZeroDivisionError):
        return 0.0


def _enabled(manager):
    from samsara.session_modes import SessionMode
    return (manager.mode is SessionMode.DICTATE and manager._buffer_dictate_until_commit
            and not manager._draft.edited and not manager._parked_hold
            and manager._commit_redecode_fn is not None)


def stage(method):
    @wraps(method)
    def staged(manager, chunk_raw, audio_ref=None):
        with manager._dispatch_lock:
            before = _source(manager)
            count = len(manager._dictate_pending_audio)
            duration = _duration(manager, audio_ref)
            if managed(manager):
                span = manager._open_span
                projected = sum(span.durations) + duration + .160 * len(span.durations)
                if (span.source != before or span.closing or projected > CAP_S
                        or '\n' in chunk_raw):
                    seal(manager)
                _cancel(span)
            outcome = method(manager, chunk_raw, audio_ref)
            if outcome.kind != 'dictate_staged':
                return outcome
            top = manager._stack.peek()
            if top is not None and top.kind == 'dictation_staged_chunk':
                top.extra['prior_buffer'] = before[3]
            if not _enabled(manager) or len(manager._dictate_pending_audio) != count + 1:
                seal(manager)
                return outcome
            clip = _speech_clip(manager, count)
            if clip is None or duration <= 0 or duration > CAP_S or '\n' in manager._draft.text[len(before[3]):]:
                seal(manager)
                return outcome
            if not managed(manager):
                manager._open_span = Span(before, before[3], count)
            span = manager._open_span
            span.durations.append(duration)
            span.uncertain |= bool(top and top.extra.get('uncertain_seam'))
            span.source = _source(manager)
            elapsed = sum(span.durations) + .160 * (len(span.durations) - 1)
            sentence_end = re.search(r'(?<!\.)[.!?]["\u201d\u2019]?$', span.source[3]) is not None
            span.closing = elapsed >= CAP_S or (elapsed >= SENTENCE_S and sentence_end)
            _schedule(manager, span)
            return outcome
    return staged


def _schedule(manager, span):
    if not span_needs_redecode(span.durations, span.uncertain):
        if span.closing:
            seal(manager)
        return
    app = _app(manager)
    # Missing/unknown device is deliberately treated as CPU.
    device = getattr(app, 'device_type', None)
    if device is None:
        device = getattr(getattr(getattr(app, 'model', None), 'model', None), 'device', '')
    delay = CUDA_SETTLE_S if str(device).lower().startswith('cuda') else CPU_PAUSE_S
    generation, onset = span.generation, _activity(manager)[0]
    deadline = time.monotonic() + delay

    def poll():
        with manager._dispatch_lock:
            if not _current(manager, span, generation, onset):
                return
            remaining = deadline - time.monotonic()
            if remaining > 0:
                _timer(span, poll, min(POLL_S, remaining))
                return
            span.timer = None
        _decode(manager, span, generation, onset)

    _timer(span, poll, min(POLL_S, delay))


def _timer(span, callback, delay):
    timer = span.timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()


def _current(manager, span, generation, onset):
    return (getattr(manager, '_open_span', None) is span and _enabled(manager)
            and span.generation == generation and span.source == _source(manager)
            and _activity(manager) == (onset, False))


def _decode(manager, span, generation, onset, *, sending=False, consumed=False):
    with manager._dispatch_lock:
        if not _current(manager, span, generation, onset):
            return
        if not span_needs_redecode(span.durations, span.uncertain):
            return
        if span.attempted == len(span.durations):
            if span.closing:
                seal(manager)
            return
        clips = [_speech_clip(manager, i) for i in range(span.start, len(manager._dictate_pending_audio))]
        if any(clip is None for clip in clips):
            seal(manager)
            return
        from samsara.ava_edit import session
        if session.pending() is not None or not draft_reread._busy.acquire(blocking=False):
            return
        source, prefix = span.source, span.prefix
        decoder = manager._commit_redecode_fn
        owner = getattr(decoder, '__self__', None)
        model = getattr(owner, 'model', None)
    try:
        audio = stitch_span_audio(clips)
        if audio is None:
            return
        # No initial_prompt: FINISH IT 1 explicitly retains the frozen callback.
        text = decoder(source[3][len(prefix):].strip(), [audio])
        if text and consumed:
            from samsara.session_modes import strip_redecoded_commit_word
            text = strip_redecoded_commit_word(text)
        if text and manager._prepare_dictate_fn is not None:
            text = manager._prepare_dictate_fn(text.strip())
        with manager._dispatch_lock:
            if (not _current(manager, span, generation, onset)
                    or manager._commit_redecode_fn != decoder or getattr(owner, 'model', None) is not model):
                return
            span.attempted = len(span.durations)
            if not text or not text.strip() or redecode_is_poorer(source[3][len(prefix):], text):
                return
            join = ' ' if prefix and not prefix[-1].isspace() else ''
            replacement = prefix + join + text
            if replacement != source[3]:
                manager._draft.replace_text(replacement)
                manager._draft.delivery_prepared = manager._prepare_dictate_fn is not None
                manager._last_dictate_ended_terminal = chunk_ends_terminal(replacement)
                draft_reread.checkpoint(manager)
                span.source = _source(manager)
                top = manager._stack.peek()
                if top is not None and top.kind == 'dictation_staged_chunk':
                    top.extra['span_buffer'] = replacement
                if not sending:
                    _refresh(manager)
    except Exception:
        log.warning('[SESSION] Open-span re-decode unavailable; staged draft retained')
    finally:
        draft_reread._busy.release()
        with manager._dispatch_lock:
            if span.generation == generation and span.closing:
                seal(manager)


def _refresh(manager):
    app = _app(manager)
    surface = getattr(app, 'live_surface', None)
    if surface is not None:
        surface.sync_draft(manager)  # Controller posts the revision onto its UI queue.
    preview = getattr(app, '_dictate_preview', None)
    if preview is not None and not getattr(preview, '_closed', False):
        preview._resync_from_draft(manager)
        preview._render('')  # Existing queued Compact overlay update.


def commit(method):
    @wraps(method)
    def committed(manager, *args, **kwargs):
        if managed(manager):
            span = manager._open_span
            _cancel(span)
            _decode(manager, span, span.generation, _activity(manager)[0], sending=True,
                    consumed=kwargs.get('consumed_trailing_commit_word', False))
        return method(manager, *args, **kwargs)
    return committed


def dispatch(method):
    @wraps(method)
    def dispatched(manager, *args, **kwargs):
        if managed(manager):
            _cancel(manager._open_span)
        outcome = method(manager, *args, **kwargs)
        if outcome.kind != 'dictate_staged':
            seal(manager)  # Includes cursor-only commands with no document revision.
        return outcome
    return dispatched


def scratch(method):
    @wraps(method)
    def scratched(manager):
        item = manager._stack.peek()
        if (item is not None and item.kind == 'dictation_staged_chunk'
                and 'prior_buffer' in item.extra and not manager._draft.edited
                and not manager._parked_hold
                and (manager.dictate_pending_buffer.endswith(item.payload)
                     or manager.dictate_pending_buffer == item.extra.get('span_buffer'))):
            manager._dictate_pending_buffer = item.extra['prior_buffer']
            if manager._dictate_pending_audio:
                manager._dictate_pending_audio.pop()
            manager._stack.pop()
            text = manager.dictate_pending_buffer
            manager._last_dictate_ended_terminal = chunk_ends_terminal(text) if text else None
            draft_reread.checkpoint(manager)
            seal(manager)
            return True
        return method(manager)
    return scratched
