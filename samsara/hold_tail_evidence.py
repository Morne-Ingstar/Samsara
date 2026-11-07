"""1174a: bounded tail policy inputs and numeric, original-timeline evidence.

Energy and alignment are observations, never a diagnosis or delivery gate.
No transcript, retained audio, settings, devices, or storage are owned here.
"""
from contextvars import ContextVar
from functools import wraps
from math import isfinite

import numpy as np
from samsara.hold_diagnostics import best_effort

MIN_POSSIBLE_GAP_MS = 600
UNTRUSTED_TAIL_CAP_MS = 600
_decode = ContextVar('tail_endpoint_observation', default=None)


def with_decode_context(operation):
    state = _decode.get()
    @wraps(operation)
    def run(*args, **kwargs):
        token = _decode.set(state)
        try:
            return operation(*args, **kwargs)
        finally:
            _decode.reset(token)
    return run


def timeline_offset(audio, rate):
    state = _decode.get()
    if state is None or not np.shares_memory(state['audio'], audio):
        return 0.
    return (audio.ctypes.data - state['audio'].ctypes.data) / audio.itemsize / rate


def level(pcm):
    data = np.asarray(pcm, dtype=np.float64)
    return float(np.sqrt(np.mean(data * data))) if data.size else 0.


class Ambient:
    """Only a complete, contiguous, pre-press rewind may estimate ambient.

    Post-duck/TTS-suppressed opening speech is never ambient. A timestamped
    pre-press window is still acoustic evidence, not proof of a quiet room.
    """
    def __init__(self, before, count, enabled):
        self.before, self.count, self.enabled = before, count, enabled
        self.frames = []
        self.sequence_end = None
        self.rewound = False

    def rewind(self, reader):
        # The ring cursor proves which samples predate activation, even when
        # a device supplies timestamps in a different clock domain.
        if getattr(reader, '_bus', None) is not None:
            self.sequence_end = reader._read_cursor
        reader.rewind(self.count)
        self.rewound = True

    def observe(self, frame, width):
        if not self.enabled or len(self.frames) >= self.count:
            return
        prepress = (self.sequence_end - self.count <= frame.seq < self.sequence_end
                    if self.sequence_end is not None else
                    self.before - self.count * width <= frame.t_capture <= self.before)
        if not prepress:
            return
        self.frames.append((frame.seq, frame.device_epoch, level(frame.pcm) / 32767.))

    def threshold(self, floor, ratio, prebuffer=()):
        frames, source = self.frames, 'prepress_p20'
        # Preserve the batch reader contract for legacy adapters that expose
        # rewound PCM but no ring cursor. Never use this for streaming/live PCM.
        if self.enabled and self.rewound and self.sequence_end is None and not frames:
            frames = [(seq, epoch, level(pcm) / 32767.) for seq, epoch, pcm in prebuffer]
            source = 'rewound_p20'
        if (not self.enabled or len(frames) != self.count or len({x[1] for x in frames}) != 1
                or any(b[0] != a[0] + 1 for a, b in zip(frames, frames[1:]))):
            return float(floor), 'configured_floor', 0.
        values = [x[2] for x in frames]
        if not np.isfinite(values).all():
            return float(floor), 'configured_floor', 0.
        noise = float(np.percentile(values, 20))
        return max(float(floor), noise * ratio), source, noise


def release_limit(source, requested_ms):
    """1174a n1: unknown room tone gets a smaller, fixed latency budget."""
    return min(requested_ms, UNTRUSTED_TAIL_CAP_MS) if source == 'configured_floor' else requested_ms


def endpoint(value, duration, origin=0.):
    """Unknown is None, including estimates outside the supplied PCM."""
    try:
        if isinstance(value, bool):
            return None
        value, duration, origin = float(value), float(duration), float(origin)
        if all(isfinite(x) for x in (value, duration, origin)) and 0 <= value <= duration and origin >= 0:
            return round((value + origin) * 1000)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def endpoints(segments, duration, origin=0.):
    if not segments:
        return None, None
    ends = []
    for segment in segments:
        start = endpoint(getattr(segment, 'start', None), duration)
        end = endpoint(getattr(segment, 'end', None), duration, origin)
        if start is None or end is None or start + round(origin * 1000) >= end:
            return None, None
        ends.append(end)
    index = max(range(len(ends)), key=ends.__getitem__)
    segment = segments[index]
    words = getattr(segment, 'words', None)
    word_end = None
    if words:
        times = [(endpoint(getattr(w, 'start', None), duration, origin),
                  endpoint(getattr(w, 'end', None), duration, origin)) for w in words]
        first = endpoint(segment.start, duration, origin)
        if all(a is not None and b is not None and first <= a < b <= ends[index] for a, b in times):
            word_end = max(b for a, b in times)
    return max(ends), word_end


def tail_snapshot(values):
    """Compare valid endpoints; rejected raw output explains accepted gaps."""
    raw = values.get('decoder_raw_end_ms')
    accepted = values.get('decoder_accepted_end_ms')
    speech = values.get('last_speech_end_ms')
    duration = values.get('captured_audio_ms', -1)
    def valid(x):
        return (isinstance(duration, (int, float)) and isfinite(duration) and duration >= 0
                and isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(x) and 0 <= x <= duration)
    comparable = values.get('decoder_timeline_source') == 'capture_samples'
    gap = max(0, speech - accepted) if comparable and valid(speech) and valid(accepted) else None
    raw_gap = max(0, speech - raw) if comparable and valid(speech) and valid(raw) else None
    reduction = max(0, raw - accepted) if valid(raw) and valid(accepted) else None
    possible = bool(gap is not None and raw_gap is not None
        and gap >= MIN_POSSIBLE_GAP_MS and raw_gap >= MIN_POSSIBLE_GAP_MS
        and values.get('last_speech_source') == 'vad_p01'
        and values.get('tail_vad_run_ms', 0) >= 64
        and values.get('decoder_timeline_source') == 'capture_samples')
    return dict(decoder_gap_ms=gap, decoder_raw_gap_ms=raw_gap,
        decoder_accepted_reduction_ms=reduction, possible_missing_tail=int(possible),
        tail_observation_source=('speech_after_raw_decode' if possible else
            'accepted_reduction' if reduction else 'unknown' if gap is None else 'endpoints_only'))


@best_effort
def observe_raw(segments, audio, rate, chunk_start, chunk_samples):
    state = _decode.get()
    if state is None:
        return
    root = state['audio']
    if not np.shares_memory(root, audio):
        state['unknown'] = True
        return
    origin = (audio.ctypes.data - root.ctypes.data) / audio.itemsize / rate + chunk_start / rate
    end, _ = endpoints(segments, chunk_samples / rate, origin)
    # The caller observes only the chosen retry/fallback, never its predecessor.
    state['raw'].append(end)


def _publish(state, audio, rate, result):
    @best_effort
    def publish():
        from samsara.hold_final_policy import current_audit
        from samsara.hold_diagnostics import get
        joined = get(current_audit.get())
        if joined is not None:
            root = state['audio']
            origin = (audio.ctypes.data - root.ctypes.data) / audio.itemsize / rate
            accepted, word = endpoints(result.segments, len(audio) / rate, origin)
            raw = tuple(state['raw'])
            with joined.lock:
                matched = (not state['unknown'] and
                    abs(joined.values['captured_audio_ms'] - len(root) / rate * 1000) <= 1)
                source = ('capture_samples' if len(audio) == len(root) and origin == 0
                          else 'capture_range_samples') if matched else 'unknown'
                joined.values.update(decoder_raw_end_ms=(max(raw) if raw and all(x is not None for x in raw) else None),
                    decoder_accepted_end_ms=accepted, decoder_word_end_ms=word,
                    decoder_timeline_source=source)
                joined.values.update(tail_snapshot(joined.values))
    publish()


def observed_decode(operation):
    """Nested range recovery uses source views; the outer result wins merging.

    Retained retry slices keep their original ndarray base. Coordinate offsets
    therefore include sparse trims, chunk context, resplits and retry ranges.
    Observations never affect decoding, even when malformed telemetry fails.
    """
    @wraps(operation)
    def run(app, audio, *args, **kwargs):
        outer = _decode.get()
        root = audio
        while (isinstance(root.base, np.ndarray) and root.base.ndim == 1
               and root.base.dtype == audio.dtype and root.base.flags.c_contiguous):
            root = root.base
        state = outer or dict(audio=root, raw=[], unknown=False)
        token = _decode.set(state)
        try:
            result = operation(app, audio, *args, **kwargs)
            if outer is None:
                _publish(state, audio, app.model_rate, result)
            return result
        finally:
            _decode.reset(token)
    return run
