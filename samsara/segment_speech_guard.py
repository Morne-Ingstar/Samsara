"""1170: decoded words need support on the original speech timeline.

Reuse the hold's permissive Silero islands (250 ms padding, >.1 probability).
Do not judge words by volume: a soft word inside speech remains eligible.
Unknown detector evidence keeps the existing decoder guards in charge.
"""
from copy import copy
from math import isfinite
import logging


WORD_PROBABILITY_FLOOR = .01
SHORT_WORD_S = .133


def _number(value):
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _overlaps(item, bounds, rate, origin, *, fraction=.6):
    times = [getattr(item, key, None) for key in ('start', 'end')]
    if any(value is None for value in times):
        return None
    start, end = map(_number, times)
    if start is None or end is None:
        return False
    if not 0 <= start < end:
        return False
    start, end = start + origin, end + origin
    overlap = sum(max(0., min(end, b / rate) - max(start, a / rate)) for a, b in bounds)
    return overlap > 0 and overlap + 1e-9 >= fraction * (end - start)


def _anomalous_word(word):
    """Whisper's probability/duration cues, with a much narrower .01 floor.

    Alignment itself can put a tiny invented word on a real speech boundary.
    Require BOTH near-zero probability and an abnormally short duration;
    quiet speech is never rejected by volume or segment confidence alone.
    """
    probability = _number(getattr(word, 'probability', None))
    start, end = (_number(getattr(word, key, None)) for key in ('start', 'end'))
    return (probability is not None and probability < WORD_PROBABILITY_FLOOR
            and start is not None and end is not None and 0 <= end - start < SHORT_WORD_S)


def screen(segments, bounds, rate, origin, params):
    """Remove unsupported words only after the existing acceptance decisions.

    ``bounds`` are padded speech spans in original capture samples; segment
    and word times are relative to this decode's PCM. Do not modify decoder
    objects or their confidence telemetry. Hold refusal/recovery gates see the
    original output first; a replay replaces its predecessor as before.
    """
    if bounds is None:
        return list(segments)
    from samsara.hold_final_policy import current_audit
    audit = current_audit.get()
    kept, removed_words, removed_segments = [], 0, 0
    for segment in segments:
        score = _number(getattr(segment, 'avg_logprob', None))
        threshold = _number(params.get('log_prob_threshold', -1.))
        uncertain = score is not None and threshold is not None and score < threshold
        temperature = _number(getattr(segment, 'temperature', None))
        compression = _number(getattr(segment, 'compression_ratio', None))
        no_speech = _number(getattr(segment, 'no_speech_prob', None))
        # A fallback that still reports silence/loops is not corroborated by
        # speech elsewhere in its segment or elsewhere in the recording.
        unsafe = uncertain and temperature is not None and temperature > 0 and (
            (compression is not None and compression > 2.4)
            or (no_speech is not None and no_speech >= .5))
        words = getattr(segment, 'words', None)
        if unsafe:
            removed_segments += 1
            removed_words += len(words or ())
            continue
        if words:
            supported = [word for word in words if _overlaps(word, bounds, rate, origin) is True
                         and not _anomalous_word(word)]
            removed_words += len(words) - len(supported)
            if not supported:
                removed_segments += 1
                continue
            if len(supported) != len(words):
                item = copy(segment)
                item.words, item.text = supported, ''.join(word.word for word in supported)
                item.start, item.end = supported[0].start, supported[-1].end
                kept.append(item)
                continue
        else:
            # Legacy decoders can omit word times. Require a majority overlap
            # for uncertain pieces; confident segments may span a thinking
            # pause and still contain a short, genuine spoken phrase. Missing
            # segment timing leaves confident legacy output to existing guards.
            overlaps = _overlaps(segment, bounds, rate, origin,
                                 fraction=.6 if uncertain else 0.)
            if overlaps is False or (overlaps is None and (uncertain or not bounds)):
                removed_segments += 1
                continue
        kept.append(segment)
    if removed_words or removed_segments:
        if audit is not None:
            audit.drop('unsupported_speech', max(1, removed_segments))
        logging.getLogger('Samsara').info(
            '[SPEECH_ALIGN] segments_dropped=%d words_dropped=%d origin_s=%.3f',
            removed_segments, removed_words, origin)
    return kept


def screen_commit(result, audio, rate, params):
    """Apply the same speech check to optional whole-draft re-decoding."""
    import numpy as np
    if not isinstance(audio, np.ndarray) or audio.ndim != 1:
        return result  # Non-PCM adapters cannot provide independent speech timing.
    from samsara.decode_recovery import _speech_bounds
    from samsara.speech_presence import measure
    bounds = _speech_bounds(measure(audio, rate), rate, len(audio))
    segments, info = result

    def screened():
        for segment in segments:
            yield from screen([segment], bounds, rate, 0., params)

    return screened(), info
