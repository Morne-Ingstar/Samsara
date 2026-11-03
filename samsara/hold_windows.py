"""Recover untranscribed long-hold intervals without replacing decoded words.

Keep the normal whole-capture decode (earlier unconditional splitting degraded
speech). Only gaps in its timestamp coverage get independent bounded probes.
Everything runs inside the caller's existing model lock and deadline.
"""
from copy import copy
import logging
import re

import numpy as np

logger = logging.getLogger('Samsara')
MAX_WINDOW_S = 25
CONTEXT_S = .4
MIN_GAP_S = 1.


def _gaps(segments, duration):
    end = 0.
    gaps = []
    for segment in sorted(segments, key=lambda s: float(s.start)):
        start, stop = float(segment.start), float(segment.end)
        if not np.isfinite([start, stop]).all() or start < 0 or stop < start:
            raise ValueError('invalid hold timestamps')
        # Whisper can timestamp into its final padded window. Coverage is
        # bounded by captured PCM, while original segment telemetry is kept.
        start, stop = min(start, duration), min(stop, duration)
        if start - end >= MIN_GAP_S:
            gaps.append((end, start))
        end = max(end, stop)
    if duration - end >= MIN_GAP_S:
        gaps.append((end, duration))
    return gaps


def _signal(audio, rate):
    """Only measured silence can waive recovery; quiet unknown PCM stays safe."""
    if not len(audio) or not np.any(audio):
        return False
    from samsara.speech_presence import _presence
    try:
        evidence = _presence.measure(audio, rate)
        return evidence.passes() if evidence.known else True
    except Exception:
        return True


def _windows(audio, rate, start, end):
    """Cut near a quiet frame when available, with context at forced seams."""
    first = max(0, round((start - CONTEXT_S) * rate))
    last = min(len(audio), round((end + CONTEXT_S) * rate))
    width, overlap = round(MAX_WINDOW_S * rate), round(CONTEXT_S * rate)
    while first < last:
        stop = min(last, first + width)
        if stop < last:
            # Prefer a pause in the final five seconds; never discard PCM.
            frame = max(1, round(.02 * rate))
            candidates = np.arange(max(first + width - 5 * rate, first + frame), stop, frame, dtype=int)
            energy = [float(np.mean(np.square(audio[i:i + frame], dtype=np.float64))) for i in candidates]
            if energy and min(energy) < 1.e-8:
                stop = int(candidates[int(np.argmin(energy))]) + frame // 2
        yield first, stop
        if stop == last:
            break
        first = stop - overlap


def _offset(segment, seconds):
    item = copy(segment)
    item.start, item.end = segment.start + seconds, segment.end + seconds
    if getattr(segment, 'words', None):
        item.words = []
        for original in segment.words:
            word = copy(original)
            word.start, word.end = original.start + seconds, original.end + seconds
            item.words.append(word)
    return item


def _append(result, segment, *, recovered):
    """Deduplicate only recovery's overlapping context, never original words."""
    if result and recovered and segment.start < result[-1].end:
        previous = list(re.finditer(r'\w+', result[-1].text))
        words = list(re.finditer(r'\w+', segment.text))
        for count in range(min(len(previous), len(words), 12), 0, -1):
            if [w.group().casefold() for w in previous[-count:]] == [
                    w.group().casefold() for w in words[:count]]:
                if count == len(words):
                    return
                segment.text = segment.text[words[count].start():]
                if getattr(segment, 'words', None):
                    segment.words = segment.words[count:]
                break
    if result and segment.text and not segment.text[0].isspace() and not result[-1].text.endswith((' ', '\n')):
        segment = copy(segment)
        segment.text = ' ' + segment.text
    result.append(segment)


def _trim_following_context(segment, original):
    """The end of a middle-gap probe may repeat the next native segment."""
    for following in original:
        if not segment.start < following.start < segment.end:
            continue
        words = list(re.finditer(r'\w+', segment.text))
        next_words = list(re.finditer(r'\w+', following.text))
        for count in range(min(len(words), len(next_words), 12), 0, -1):
            if [w.group().casefold() for w in words[-count:]] == [
                    w.group().casefold() for w in next_words[:count]]:
                if count == len(words):
                    return None
                segment.text = segment.text[:words[-count].start()].rstrip()
                if getattr(segment, 'words', None):
                    segment.words = segment.words[:-count]
                break
    return segment


def transcribe(app, audio, params, decode, *, path='hold_final', check=lambda: None):
    """Return lazy segments; retain complete PCM if a spoken interval stays empty."""
    check()
    initial, info = decode(app, audio, path=path, **params)
    rate = app.model_rate
    if len(audio) <= 30 * rate:
        return initial, info

    def covered():
        original = []
        for segment in initial:
            check()
            original.append(segment)
        try:
            missing = _gaps(original, len(audio) / rate)
        except (AttributeError, TypeError, ValueError, OverflowError):
            # Unknown timing cannot authorize rewriting the decoder's text.
            if path == 'hold_final':
                _retain(app, audio, params, 0., len(audio) / rate, 'unknown_timestamps')
            yield from original
            return
        recovered = []
        retry = dict(params, initial_prompt=None, without_timestamps=False,
                     condition_on_previous_text=False)
        for start, end in missing:
            check()
            span = audio[round(start * rate):round(end * rate)]
            if not _signal(span, rate):
                continue
            for first, last in _windows(audio, rate, start, end):
                check()
                try:
                    segments, _ = decode(app, audio[first:last], path=path, **retry)
                    for segment in segments:
                        check()
                        # Bad recovery telemetry cannot erase the valid original.
                        if not (np.isfinite([segment.start, segment.end]).all()
                                and 0 <= segment.start <= segment.end):
                            raise ValueError('invalid recovery timestamps')
                        item = _trim_following_context(_offset(segment, first / rate), original)
                        if item is not None:
                            recovered.append(item)
                except TimeoutError:
                    raise
                except Exception:
                    if path == 'hold_final':
                        _retain(app, audio, params, first / rate, last / rate, 'window_decode_failed')
        # Original text always wins over a conflicting overlapping probe.
        result = []
        for segment, is_recovered in sorted(
                [(s, False) for s in original] + [(s, True) for s in recovered],
                key=lambda pair: (pair[0].start, pair[1])):
            if is_recovered and any(segment.start >= s.start and segment.end <= s.end for s in original):
                continue
            _append(result, segment, recovered=is_recovered)
        if path == 'hold_final':
            for start, end in _gaps(result, len(audio) / rate):
                check()
                if _signal(audio[round(start * rate):round(end * rate)], rate):
                    _retain(app, audio, params, start, end, 'untranscribed_window')
        if recovered:
            logger.info('[HOLD_WINDOWS] path=%s gaps=%d recovered_segments=%d', path, len(missing), len(recovered))
        yield from result
    return covered(), info


def _retain(app, audio, params, start, end, reason):
    from samsara.decode_recovery import retain
    retain(app, audio, params, reason)
    logger.warning('[HOLD_WINDOWS] unresolved start_s=%.3f end_s=%.3f reason=%s audio_retained=1',
                   start, end, reason)


def partial_decode(worker, audio, params):
    from samsara.vocabulary_decode import transcribe_with_vocabulary
    return transcribe(worker._session.app, audio, params, transcribe_with_vocabulary,
                      path='hold_partial')
