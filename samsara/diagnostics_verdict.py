"""Plain-English health verdict for the Dictation Diagnostics panel.

Pure function -- no I/O, no Qt, no config access, nothing that could raise
into a caller. Analyzes the most recent N records from
samsara.diagnostics's ring buffer (samsara.diagnostics.recent()) and
produces a one-line plain-English headline plus a longer detail string
carrying the numbers behind it. See samsara/ui/diagnostics_qt.py for the
header band that renders this, refreshed every time the panel's table
refreshes.

`records` only needs to duck-type DiagRecord's fields actually read below
(no_speech_prob, outcome, avg_logprob, audio_s, t_transcribe_ms) -- tests
can pass plain objects/namespaces instead of constructing real DiagRecords.
"""
from __future__ import annotations

import statistics

# How many of the most-recent records to analyze. Recent enough to answer
# "how is it behaving right now", not a lifetime average.
WINDOW_N = 10

# Audio health: either signal alone is enough to call the mic weak/noisy.
# no_speech_prob is Whisper's own "this is silence" confidence (0..1) --
# median across the window above this is a persistent pattern, not one bad
# take. gated/empty outcomes are dictation.py's own upstream signal that a
# buffer either never reached the model (VAD gate) or came back with no
# usable text -- a high proportion of either is the same underlying problem
# (the model isn't hearing speech) even when no_speech_prob individually
# looks fine (e.g. a "gated" record has no no_speech_prob at all).
HIGH_NO_SPEECH_PROB = 0.5
HIGH_GATED_EMPTY_RATIO = 0.3   # >30% of the window gated or empty

# Model confidence: avg_logprob is always <= 0; closer to 0 means more
# confident. Below this on otherwise-healthy audio suggests the model
# itself (not the input signal) is the bottleneck.
LOW_LOGPROB = -0.5

# Latency: real-time factor (transcribe time / audio length) above this is
# worth a footnote -- not a health problem on its own, just a heads-up.
SLOW_REALTIME_FACTOR = 1.0


def verdict(records) -> "tuple[str, str]":
    """Return (headline, detail) for the most recent WINDOW_N records.

    headline is plain language, no jargon, no numbers -- fit for a
    prominent header label. detail carries the supporting numbers and may
    mention specific signals (no-speech probability, confidence, model
    size) by name.
    """
    window = list(records)[-WINDOW_N:]
    if not window:
        return (
            "No dictation activity yet.",
            "Dictate a few times, then reopen this panel for a health summary.",
        )

    n = len(window)

    no_speech_values = [r.no_speech_prob for r in window if r.no_speech_prob is not None]
    median_no_speech = statistics.median(no_speech_values) if no_speech_values else None
    gated_empty = sum(1 for r in window if r.outcome in ("gated", "empty"))
    gated_empty_ratio = gated_empty / n

    audio_weak = (
        (median_no_speech is not None and median_no_speech > HIGH_NO_SPEECH_PROB)
        or gated_empty_ratio > HIGH_GATED_EMPTY_RATIO
    )

    logprob_values = [r.avg_logprob for r in window if r.avg_logprob is not None]
    median_logprob = statistics.median(logprob_values) if logprob_values else None
    model_struggling = median_logprob is not None and median_logprob < LOW_LOGPROB

    rtf_values = [
        (r.t_transcribe_ms / 1000.0) / r.audio_s
        for r in window
        if getattr(r, "audio_s", 0) > 0 and getattr(r, "t_transcribe_ms", -1) >= 0
    ]
    median_rtf = statistics.median(rtf_values) if rtf_values else None
    slow = median_rtf is not None and median_rtf > SLOW_REALTIME_FACTOR

    if audio_weak:
        headline = "Your microphone signal looks weak or noisy."
        detail = (
            f"Median no-speech probability {_fmt(median_no_speech)} "
            f"({gated_empty}/{n} recent attempts gated or produced no text). "
            "Check mic placement and input gain, or run Recalibrate Mic."
        )
    elif model_struggling:
        headline = "Audio is clear but the model is struggling."
        detail = (
            f"Median model confidence (avg_logprob) is {_fmt(median_logprob)} on "
            "otherwise-clear audio -- consider a larger model, or add problem "
            "words to your vocabulary."
        )
    else:
        headline = "Audio and recognition both look healthy."
        detail = (
            "Misrecognitions at this point are model accuracy limits, not audio "
            "problems -- the Vocabulary and Voice Training tools target specific words."
        )

    if slow:
        detail += f" Latency note: median transcription took {median_rtf:.1f}x the audio length."

    return headline, detail


def _fmt(value) -> str:
    return f"{value:.2f}" if value is not None else "n/a"
