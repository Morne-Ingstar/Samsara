"""Conservative final screen for short Whisper sign-off hallucinations.

This module is deliberately pure.  It needs only the text Whisper returned
and the telemetry already attached to its segments; it never loads a model or
imports the application.
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
    "thank you for watching",
    "thanks for watching",
    "thank you",
    "thanks",
    "like and subscribe",
    "subscribe",
    "see you next time",
    "goodbye",
    "the end",
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
    speech_level=None, speech_floor=DECODER_SPEECH_FLOOR,
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
