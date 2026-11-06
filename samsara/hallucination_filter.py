"""Conservative final screen for short Whisper sign-off hallucinations.

Policies use decoder telemetry and optional PCM; the decode adapter never
loads a model or imports the application.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence


# Short, whole-utterance phrases repeatedly emitted by Whisper for silence or
# clipped speech.  "you" is intentionally retained because it is a measured
# output from the prompt-contamination corpus; the audio corroboration below
# is mandatory for every entry, especially that one-word entry.
SIGNOFF_PHRASES = (
    "thank you",
    "thanks",
    "goodbye",
    "bye",
    "you",
)

_PHRASE_TOKENS = tuple(
    tuple(re.findall(r"\w+", phrase.casefold())) for phrase in SIGNOFF_PHRASES
)


@dataclass(frozen=True)
class SignoffContext:
    """Segments plus the capture duration that a segment alone cannot know.

    ``segments`` may contain faster-whisper objects or mapping-like test
    doubles.  The public filter continues to take exactly ``(text, segments)``
    so callers without a capture duration can simply pass their segment list.
    """

    segments: Sequence[object]
    audio_duration_s: float | None = None
    enabled: bool = True
    user_held_key: bool = False


def _value(segment: object, name: str):
    if isinstance(segment, Mapping):
        return segment.get(name)
    return getattr(segment, name, None)


# 387 owner log, 01:45:25.340: compression 18.444 > decoder ceiling 2.4.
DECODER_COMPRESSION_RATIO_THRESHOLD = 2.4
# 01:45:22.010/23.099: partial logprobs -2.423/-3.173 failed -1.0.
DECODER_LOGPROB_THRESHOLD = -1.0
# 01:45:19.781/20.868: native no-speech skips at .789/.639 > .6.
DECODER_NO_SPEECH_THRESHOLD = 0.6
# 01:45:28.790: original last rung 1.0 returned logprob -4.766 anyway.
DECODER_TOP_TEMPERATURE = 1.0
# 01:45:24.010: measured hold level and configured floor both .0080.
DECODER_SPEECH_FLOOR = 0.008


def decoder_failure_reason(
    segments, *, log_prob_threshold=DECODER_LOGPROB_THRESHOLD,
    no_speech_threshold=DECODER_NO_SPEECH_THRESHOLD,
    top_temperature=DECODER_TOP_TEMPERATURE, partial=False,
    speech_level=None, speech_floor=DECODER_SPEECH_FLOOR, audio=None, rate=16000,
) -> str | None:
    """Reject a failed decode from its existing signals, never energy alone.

    Missing/non-finite telemetry fails open. A low hold level corroborates
    an all-low-logprob result even before its last fallback rung; a clean
    whispered decode is untouched. Partials have no recovery ladder, so a
    failed segment must not be displayed or typed while recording.
    """
    from math import isfinite

    def number(value):
        try:
            value = float(value)
            return value if isfinite(value) else None
        except (TypeError, ValueError):
            return None

    segments = list(segments)
    if not segments:
        return None
    compression = [number(_value(seg, 'compression_ratio')) for seg in segments]
    if any(value is not None and value > DECODER_COMPRESSION_RATIO_THRESHOLD
           for value in compression):
        return 'compression_ratio'

    threshold = number(log_prob_threshold)
    if threshold is None:
        return None
    logprobs = [number(_value(seg, 'avg_logprob')) for seg in segments]
    failed = [value is not None and value < threshold for value in logprobs]
    if partial:
        return 'partial_logprob' if any(failed) else None
    if not all(failed):
        return None

    from samsara.borderline_confidence import supported
    if supported(segments, threshold, no_speech_threshold, audio=audio, rate=rate,
                 speech_level=speech_level, zero_temperature=True):
        return None

    top = number(top_temperature)
    temperatures = [number(_value(seg, 'temperature')) for seg in segments]
    if top is not None and all(value is not None and value >= top for value in temperatures):
        return 'fallback_exhausted'
    level, floor = number(speech_level), number(speech_floor)
    if level is not None and floor is not None and level <= floor:
        return 'low_logprob_at_noise_floor'
    no_speech = [number(_value(seg, 'no_speech_prob')) for seg in segments]
    silence_threshold = number(no_speech_threshold)
    if silence_threshold is not None and all(
        value is not None and value > silence_threshold for value in no_speech
    ):
        return 'no_speech'
    return None


def _split_context(segments: object) -> tuple[Sequence[object], float | None, bool, bool]:
    if isinstance(segments, SignoffContext):
        return segments.segments, segments.audio_duration_s, segments.enabled, segments.user_held_key
    return segments or (), None, True, False


def _is_phrase_sequence(tokens: list[str]) -> bool:
    """Whether every token belongs to adjoining complete list phrases."""
    if not tokens:
        return False
    reachable = [False] * (len(tokens) + 1)
    reachable[0] = True
    for start in range(len(tokens)):
        if not reachable[start]:
            continue
        for phrase in _PHRASE_TOKENS:
            end = start + len(phrase)
            if end <= len(tokens) and tuple(tokens[start:end]) == phrase:
                reachable[end] = True
    return reachable[-1]


def _tail_is_longer_than_speech(segments: Sequence[object], audio_duration_s: float | None) -> bool:
    """True only for a substantial post-speech hold tail, never a normal gap."""
    if audio_duration_s is None or audio_duration_s <= 0 or not segments:
        return False
    spans = []
    ends = []
    for segment in segments:
        start, end = _value(segment, "start"), _value(segment, "end")
        if start is None or end is None:
            return False
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            return False
        if end < start:
            return False
        spans.append(end - start)
        ends.append(end)
    speech_s = sum(spans)
    tail_s = audio_duration_s - max(ends)
    return speech_s > 0 and tail_s > speech_s


def is_signoff_hallucination(text, segments) -> tuple[bool, str]:
    """Return a sign-off-hallucination verdict and its fixed reason token.

    A phrase-shaped transcript is never enough: all four audio paths are
    corroborators.  Missing telemetry fails open, which is the safe outcome
    for an accessibility dictation path.
    """
    tokens = re.findall(r"\w+", (text or "").casefold())
    if not _is_phrase_sequence(tokens):
        return False, "not_signoff"

    seg_list, audio_duration_s, enabled, user_held_key = _split_context(segments)
    if not enabled:
        return False, "disabled"
    no_speech = [_value(segment, "no_speech_prob") for segment in seg_list]
    if no_speech and all(value is not None and float(value) >= 0.5 for value in no_speech):
        return True, "all_no_speech>=0.5"

    logprobs = [_value(segment, "avg_logprob") for segment in seg_list]
    if any(value is not None and float(value) <= -1.0 for value in logprobs):
        return True, "avg_logprob<=-1.0"

    if not user_held_key and audio_duration_s is not None and audio_duration_s < 1.5:
        return True, "audio<1.5s"

    if _tail_is_longer_than_speech(seg_list, audio_duration_s):
        return True, "tail>speech"

    return False, "audio_not_corroborated"


def signoff_failure_reason(text, segments, duration_s=None, config=None, *, user_held_key=False) -> str | None:
    """Adapt the shared verdict to decoder guards that return a reason or None."""
    from samsara.config_defaults import cfg_get

    dropped, reason = is_signoff_hallucination(
        text, SignoffContext(segments, audio_duration_s=duration_s, user_held_key=user_held_key,
                             enabled=cfg_get(config or {}, "dictation.signoff_filter")),
    )
    return "signoff:" + reason if dropped else None


# 1021: English video outros need final-segment and acoustic evidence.
OUTRO_PHRASES = (
    "thank you for watching", "thanks for watching", "like and subscribe",
    "subscribe", "see you next time", "the end",
    # 1133: wording seen in owner reports and Whisper's known outro habits.
    "thank you for watching the video", "thanks for watching the video",
    "thanks for watching this video", "please subscribe",
    "please like and subscribe", "please subscribe to my channel",
    "subscribe to my channel", "see you in the next video",
    "see you in the next one", "see you next time",
    "i will see you next time", "i ll see you next time",
    "ill see you next time", "i ll see you in the next video",
    "i will see you in the next video",
)
_OUTRO_TOKENS = {tuple(re.findall(r"\w+", phrase)) for phrase in OUTRO_PHRASES}
_OUTRO_CONNECTORS = {"and"}
# 1133: a recorded mic's idle noise sits around this level; a made-up outro over
# audio this quiet AND scored low-confidence by the decoder is not speech.
# Anything louder (quiet speakers included, see 1026) is kept.
OUTRO_NOISE_CEILING = .0005


def _finite_number(value):
    from math import isfinite
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _is_outro_sequence(tokens):
    """One known outro phrase, or several joined only by 'and' ("see you next
    time and thanks for watching"). Any other word means real content."""
    if not tokens:
        return False
    if tokens in _OUTRO_TOKENS:
        return True
    reachable = [False] * (len(tokens) + 1)
    reachable[0] = True
    for start in range(len(tokens)):
        if not reachable[start]:
            continue
        if tokens[start] in _OUTRO_CONNECTORS and start:
            reachable[start + 1] = True
        for phrase in _OUTRO_TOKENS:
            end = start + len(phrase)
            if end <= len(tokens) and tokens[start:end] == phrase:
                reachable[end] = True
    return reachable[-1] and tokens[-1] not in _OUTRO_CONNECTORS and tokens[0] not in _OUTRO_CONNECTORS


def _low_confidence(segment):
    no_speech = _finite_number(_value(segment, "no_speech_prob"))
    logprob = _finite_number(_value(segment, "avg_logprob"))
    return ((no_speech is not None and no_speech >= .5)
            or (logprob is not None and logprob <= -1.0))


def _outro_is_unsupported(segment, audio, rate):
    """Whole known phrase plus a near-silent PCM span; fail open.

    Measure the loudest 20 ms frame so a short spoken word cannot disappear
    into a long quiet timestamp span. Clear speech overrides weak scores.
    """
    import numpy as np

    tokens = tuple(re.findall(r"\w+", (_value(segment, "text") or "").casefold()))
    if not _is_outro_sequence(tokens):
        return False
    start = _finite_number(_value(segment, "start"))
    end = _finite_number(_value(segment, "end"))
    if audio is not None:
        span = None
        if start is not None and end is not None and 0 <= start < end <= len(audio) / rate:
            span = audio[int(start * rate):int(end * rate)]
        elif audio.size and np.max(np.abs(audio)) <= .0001:
            # Even missing timestamps cannot hide an entirely silent capture.
            span = audio
        if span is not None and span.size:
            width = max(1, round(rate * .02))
            squares = np.square(span.astype(np.float64))
            level = float(np.sqrt(max(np.mean(squares[i:i + width])
                                     for i in range(0, len(span), width))))
            if level > DECODER_SPEECH_FLOOR:
                return False
            if level <= .0001:
                return True
            if level <= OUTRO_NOISE_CEILING and _low_confidence(segment):
                return True
    # 1026: low confidence cannot establish silence, even below the speech
    # floor. Quiet speech and missing alignment must survive.
    return False


def transcribe_without_outro(model, audio, params, *, rate=16000, enabled=True):
    """Decode once, shorten digital silence, and screen only the final segment.

    Never threshold-trim quiet speech. Only an exact-zero suffix >=800 ms
    is shortened, retaining 300 ms padding and all nonzero samples. A
    bounded one-segment lookahead preserves lazy decoding/cancellation.
    """
    import numpy as np

    pcm = None
    if isinstance(audio, np.ndarray) and audio.ndim == 1 and audio.dtype.kind == 'f':
        rate = _finite_number(rate)
        if rate is not None and rate > 0 and np.isfinite(audio).all():
            pcm = audio
    if not enabled:
        segments, info = model.transcribe(audio, **params)
        def counted():
            from samsara.hold_final_policy import current_audit
            audit = current_audit.get()
            for segment in segments:
                if audit is not None:
                    audit.decoded += 1
                yield segment
        return counted(), info
    if pcm is not None:
        nonzero = np.flatnonzero(pcm)
        # All-silent captures stay on the decoder's native silence policy.
        if nonzero.size and len(pcm) - nonzero[-1] - 1 >= rate * .8:
            audio = pcm[:min(len(pcm), int(nonzero[-1] + 1 + rate * .3))]
    segments, info = model.transcribe(audio, **params)

    def screened():
        from samsara.hold_final_policy import current_audit
        audit = current_audit.get()
        pending = None
        try:
            for segment in segments:
                if audit is not None:
                    audit.decoded += 1
                if pending is not None:
                    yield pending
                pending = segment
        except Exception:
            # 1125: lookahead must not lose a decoded segment when the next
            # native pull fails. The same final-outro screen still applies.
            if pending is not None and not _outro_is_unsupported(pending, pcm, rate):
                yield pending
            raise
        if pending is not None:
            if _outro_is_unsupported(pending, pcm, rate):
                if audit is not None:
                    audit.drop('near_silent_outro')
                import logging
                logging.getLogger('Samsara').info('[HALLUCINATION] dropped final outro (audio/confidence)')
            else:
                yield pending

    return screened(), info
