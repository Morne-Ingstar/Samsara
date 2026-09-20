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


def _value(segment: object, name: str):
    if isinstance(segment, Mapping):
        return segment.get(name)
    return getattr(segment, name, None)


def _split_context(segments: object) -> tuple[Sequence[object], float | None, bool]:
    if isinstance(segments, SignoffContext):
        return segments.segments, segments.audio_duration_s, segments.enabled
    return segments or (), None, True


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

    seg_list, audio_duration_s, enabled = _split_context(segments)
    if not enabled:
        return False, "disabled"
    no_speech = [_value(segment, "no_speech_prob") for segment in seg_list]
    if no_speech and all(value is not None and float(value) >= 0.5 for value in no_speech):
        return True, "all_no_speech>=0.5"

    logprobs = [_value(segment, "avg_logprob") for segment in seg_list]
    if any(value is not None and float(value) <= -1.0 for value in logprobs):
        return True, "avg_logprob<=-1.0"

    if audio_duration_s is not None and audio_duration_s < 1.5:
        return True, "audio<1.5s"

    if _tail_is_longer_than_speech(seg_list, audio_duration_s):
        return True, "tail>speech"

    return False, "audio_not_corroborated"
