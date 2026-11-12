"""1170: decoded words need support on the original speech timeline.

Reuse the hold's permissive Silero islands (250 ms padding, >.1 probability).
Do not judge words by volume: a soft word inside speech remains eligible.
Unknown detector evidence keeps the existing decoder guards in charge.
Word timestamps may span thinking pauses; overlap fraction is not confidence.
"""
from copy import copy
from math import isfinite
import logging
import re


WORD_PROBABILITY_FLOOR = .01
SHORT_WORD_S = .133
# 1183: wider cues select a corroboration decode, never delete a word alone.
FRAGMENT_PROBABILITY = .2
FRAGMENT_DURATION_S = .25
CONFIRM_CONTEXT_S = 6.
CONFIRM_PAD_S = .5


def _number(value):
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _overlaps(item, bounds, rate, origin, *, fraction=.6, point=False):
    times = [getattr(item, key, None) for key in ('start', 'end')]
    if any(value is None for value in times):
        return None
    start, end = map(_number, times)
    if start is None or end is None:
        return False
    if not 0 <= start <= end or (start == end and not point):
        return False
    start, end = start + origin, end + origin
    if start == end:
        # 1216 A: word alignment can collapse a spoken word to an instant.
        # Bounds already include the guard's speech padding; add none here.
        return any(a / rate <= start <= b / rate for a, b in bounds)
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


def _supported_word(word, segment, bounds, rate, origin, params):
    if _anomalous_word(word):
        return False
    start, end = (_number(getattr(word, key, None)) for key in ('start', 'end'))
    if start is not None and start == end and any(
            other is not word
            and _number(getattr(other, 'start', None)) == start
            and _number(getattr(other, 'end', None)) == end
            for other in getattr(segment, 'words', None) or ()):
        # 1216 A: several words collapsed onto one instant cannot each claim
        # that instant's speech. Keep the original rejection for this ambiguous
        # alignment; only an isolated point can supply a word's support.
        return False
    if _overlaps(word, bounds, rate, origin, point=True) is True:
        return True
    # 1192: Whisper can stretch a genuine word across seconds of silence, or
    # place it on the quiet part of a confidently decoded speech segment.
    # A duration ratio cannot disprove that word. Require measured speech in
    # this segment and complete, good decoder telemetry before relaxing it.
    score = _number(getattr(segment, 'avg_logprob', None))
    threshold = _number(params.get('log_prob_threshold', -1.))
    silence = _number(getattr(segment, 'no_speech_prob', None))
    compression = _number(getattr(segment, 'compression_ratio', None))
    if (score is None or threshold is None or score < threshold
            or silence is None or not 0 <= silence <= .6
            or compression is None or not 0 <= compression <= 2.4
            or _overlaps(segment, bounds, rate, origin, fraction=0.) is not True):
        return False
    overlap = _overlaps(word, bounds, rate, origin, fraction=0.)
    if overlap is True:
        return True
    # With no local VAD support, the word itself must be more likely than not
    # and timed inside that supported segment. Missing/invalid timing and
    # unknown word confidence cannot borrow speech from another word.
    probability = _number(getattr(word, 'probability', None))
    a, b = (_number(getattr(word, key, None)) for key in ('start', 'end'))
    start, end = (_number(getattr(segment, key, None)) for key in ('start', 'end'))
    return (probability is not None and .5 <= probability <= 1.
            and a is not None and b is not None and start is not None and end is not None
            and 0 <= start <= a < b <= end)


def _leading_prefix(segment, following, bounds, rate, origin, params):
    """1216 B: corroborate the decoder's displaced 0-2 s opening fragment.

    Only an unfinished single word from the first native window qualifies.
    Its immediate, accepted continuation must share complete healthy native
    telemetry. Re-anchor the fragment's duration immediately before that
    continuation: the first speech onset must lie in that span, allowing the
    same 250 ms alignment tolerance used to pad the measured speech islands.
    This is evidence for keeping text, not a replacement decoder timestamp.
    """
    if (origin != 0. or not bounds or getattr(segment, 'words', None)
            or _number(getattr(segment, 'start', None)) != 0.
            or _number(getattr(segment, 'end', None)) != 2.
            or getattr(segment, 'seek', None) != 0
            or getattr(following, 'seek', None) != 0
            or len(_token(segment.text)) != 1
            or not segment.text.strip()[-1:].isalnum()
            or len(_token(following.text)) < 2):
        return False
    threshold = _number(params.get('log_prob_threshold', -1.))
    score, silence, compression, temperature = values = [
        _number(getattr(segment, key, None)) for key in
        ('avg_logprob', 'no_speech_prob', 'compression_ratio', 'temperature')]
    if (threshold is None or score is None or score < threshold
            or silence is None or not 0 <= silence <= .6
            or compression is None or not 0 <= compression <= 2.4
            or temperature != 0.
            or values != [_number(getattr(following, key, None)) for key in
                          ('avg_logprob', 'no_speech_prob', 'compression_ratio', 'temperature')]):
        return False
    words = getattr(following, 'words', None)
    anchor = _number(getattr(words[0] if words else following, 'start', None))
    if anchor is None or anchor <= 2.:
        return False
    onset, end = (value / rate for value in bounds[0])
    return anchor - 2. <= onset <= anchor + .25 and anchor <= end


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
    segments = list(segments)
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
            supported = [word for word in words
                         if _supported_word(word, segment, bounds, rate, origin, params)]
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
            # 1198: without word times, a segment can include a long pause
            # before a short spoken phrase even at low decoder confidence.
            # Speech duration divided by segment duration cannot disprove it.
            # Require local speech; unsafe fallbacks were rejected above.
            # Missing timing still cannot support uncertain legacy output.
            overlaps = _overlaps(segment, bounds, rate, origin, fraction=0.)
            if overlaps is False or (overlaps is None and (uncertain or not bounds)):
                removed_segments += 1
                continue
        kept.append(segment)
    # Only the immediate next segment can corroborate a rejected opening.
    # Requiring its original identity also rules out a partially rejected
    # continuation; no rejected words can supply their own support.
    if (len(segments) >= 2 and kept and kept[0] is segments[1]
            and _leading_prefix(segments[0], segments[1], bounds, rate, origin, params)):
        kept.insert(0, segments[0])
        removed_segments -= 1
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
        yield from screen(list(segments), bounds, rate, 0., params)

    return screened(), info


def _token(word):
    return tuple(re.findall(r"\w+(?:['’]\w+)*", word.casefold()))


def _fragment(segment, previous):
    words = getattr(segment, 'words', None) or ()
    anchors = getattr(previous, 'words', None) or ()
    if len(words) != 1 or len(_token(segment.text)) != 1 or len(anchors) < 3:
        return None
    word, anchor = words[0], anchors[-1]
    probability = _number(getattr(word, 'probability', None))
    start, end, prior_end = (_number(getattr(item, key, None)) for item, key in
                             ((word, 'start'), (word, 'end'), (anchor, 'end')))
    if (probability is None or not 0 <= probability < FRAGMENT_PROBABILITY
            or start is None or end is None or prior_end is None
            or not 0 < end - start <= FRAGMENT_DURATION_S
            or not 0 <= start - prior_end <= .1):
        return None
    return word, anchor


def confirm_fragments(app, audio, segments, params, check):
    """1183: corroborate a weak appended fragment inside a speech island.

    Alignment can reuse the preceding word's last sound for another word.
    Only one short, deterministic context decode per chunk is attempted, in
    the existing native job/lock/deadline. It must recognize the same neighbor
    at the same time with >=.5 probability, and omit the suspected word.
    Failure, missing timing, uncertainty or a spoken repeat keeps the original.
    No replacement text, volume threshold or word blacklist is involved.
    The caller publishes accepted text before entering this optional work.
    """
    from samsara.vocabulary_decode import transcribe_with_vocabulary
    from samsara.hold_final_policy import current_audit
    from samsara.transcript_gates import gate_repetition_text
    for index in range(1, len(segments)):
        pair = _fragment(segments[index], segments[index - 1])
        if pair is None:
            continue
        word, anchor = pair
        rate = app.model_rate
        start = max(0, round((word.start - CONFIRM_CONTEXT_S) * rate))
        end = min(len(audio), round((word.end + CONFIRM_PAD_S) * rate))
        if start >= end:
            return segments
        try:
            check()
            effective = dict(params, temperature=0., word_timestamps=True,
                             condition_on_previous_text=False, vad_filter=False)
            iterator, _ = transcribe_with_vocabulary(app, audio[start:end], path='hold_final', **effective)
            confirmed = []
            for item in iterator:
                check()
                confirmed.append(item)
            check()
        except Exception:
            return segments
        # Native fallback/loops are not independent confirmation of an omission.
        if not confirmed or gate_repetition_text(''.join(s.text for s in confirmed)).changed:
            return segments
        for item in confirmed:
            score = _number(getattr(item, 'avg_logprob', None))
            silence = _number(getattr(item, 'no_speech_prob', None))
            compression = _number(getattr(item, 'compression_ratio', None))
            if (score is None or score < -1. or silence is None or silence > .6
                    or compression is None or compression > 2.4
                    or _number(getattr(item, 'temperature', None)) != 0.):
                return segments
        tokens = _token(''.join(s.text for s in confirmed))
        if any(token in tokens for token in _token(word.word)):
            return segments
        matched = False
        for item in confirmed:
            for candidate in getattr(item, 'words', None) or ():
                probability = _number(getattr(candidate, 'probability', None))
                a, b = (_number(getattr(candidate, key, None)) for key in ('start', 'end'))
                if (probability is not None and probability >= .5
                        and a is not None and b is not None and 0 <= a < b <= (end - start) / rate
                        and _token(candidate.word) == _token(anchor.word)
                        and abs(b + start / rate - anchor.end) <= .35):
                    matched = True
        if matched:
            audit = current_audit.get()
            if audit is not None:
                audit.drop('unsupported_speech')
            logging.getLogger('Samsara').info('[SPEECH_ALIGN] confirmed_fragments_dropped=1')
            return segments[:index] + segments[index + 1:]
        return segments
    return segments
