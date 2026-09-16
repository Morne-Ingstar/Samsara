"""Audio helpers shared by live dictation and file transcription.

Extracted verbatim from dictation.py (queue 151).  The functions here carry
no application state and must never import dictation.
"""

import collections
from datetime import datetime
import logging
import math
from pathlib import Path
import wave

import numpy as np

from samsara.constants import MODEL_SAMPLE_RATE


logger = logging.getLogger("Samsara")


_FADE_MS = 50


# --- Silent data-loss sanity check (2026-07-16 incident) ---
# A 35.3s hold-to-record hotkey dictation delivered only 80 chars -- the
# START of the utterance stitched directly onto its END, ~25s of genuine
# continuous mid-recording speech gone. Root-caused to faster-whisper
# itself: on the FIRST 30s decode window (the only window that receives
# initial_prompt as decoder context under condition_on_previous_text=
# False -- see _build_hotkey_transcribe_params), the model can terminate
# generation far short of the window's actual content while still
# reporting a segment nominally spanning the whole window, with signals
# (avg_logprob, compression_ratio) that look individually unremarkable --
# invisible to _is_quality_exhausted, which only sees the few tokens that
# WERE generated. Reproduced deterministically against the live 540-char
# command-vocabulary initial_prompt on the incident WAV, and found in ~37%
# of recent >30s hotkey captures (~/.samsara/debug) when re-decoded with
# the same prompt -- but decode-parameter sweeps (chunk_length, beam_size,
# no_speech_threshold, vad_filter -- vad_filter=True does avoid it here,
# but that param is locked False, see tests/test_transcription_params.py)
# showed the failure itself is NOT reliably deterministic run-to-run
# (almost certainly CUDA/float16 numeric nondeterminism tipping a
# borderline decode), so no single decode-param change can be proven to
# eliminate it. This is therefore a fail-loud backstop, not a cure: catch
# "long recording, implausibly little text" after the fact and surface it
# loudly instead of silently delivering truncated text as if it were the
# whole utterance.
_SANITY_MIN_DURATION_S = 15.0
                             # Below this, natural pauses/short utterances
                             # make chars/sec too noisy a signal on its own
                             # (a genuinely short, unhurried sentence can
                             # legitimately read low) -- only worth checking
                             # once there's enough audio for the ratio to
                             # mean something.
_SANITY_MIN_CPS = 3.0
                             # Chars/sec floor below which a long decode looks
                             # suspicious. Calibrated against real captures
                             # (~/.samsara/debug, 2026-07-15/16): genuine slow/
                             # deliberate dictation with COMPLETE sentences
                             # measured 3.4-9.8 cps; confirmed truncated
                             # decodes (this incident and others found via the
                             # same audit) measured 0.16-2.96 cps. Set below
                             # the observed complete-speech floor so normal
                             # unhurried dictation never trips this.
_SANITY_MIN_SPEECH_COVERAGE = 0.5
                             # Corroboration required before flagging: at least
                             # half the buffer must read as vocal-energy-
                             # present. A genuinely quiet/mostly-silent long
                             # hold producing little text is correctly quiet,
                             # not a decode failure -- this is what tells the
                             # two cases apart.
_SANITY_RMS_WINDOW_S = 0.5
_SANITY_RMS_FLOOR_DB = -40.0
                             # dBFS noise-floor cutoff for "this window has
                             # vocal energy". Coarse and VAD-free by design --
                             # this only corroborates a WARNING-level heuristic,
                             # not a hard gate, so a cheap RMS scan is
                             # preferable to taking the VAD model's lock on
                             # every long hotkey decode.


def _speech_rms_coverage(audio, sample_rate,
                          window_s=_SANITY_RMS_WINDOW_S,
                          floor_db=_SANITY_RMS_FLOOR_DB):
    """Fraction (0.0-1.0) of `audio`'s windows whose RMS exceeds floor_db.

    A coarse, VAD-free "does this sound like it has speech in it" signal --
    not phoneme-accurate, only used to corroborate the delivered-chars-vs-
    duration sanity check (_apply_segment_quality_gates callers), never as
    a hard gate. Empty/silent audio returns 0.0.
    """
    win = max(1, int(window_s * sample_rate))
    n_windows = len(audio) // win
    if n_windows == 0:
        return 0.0
    audio64 = np.asarray(audio, dtype=np.float64)
    above = 0
    for i in range(n_windows):
        chunk = audio64[i * win:(i + 1) * win]
        rms = math.sqrt(float(np.mean(chunk ** 2))) + 1e-12
        if 20 * math.log10(rms) > floor_db:
            above += 1
    return above / n_windows


def _suspected_silent_data_loss(text, audio, sample_rate, audio_duration):
    """True if a long decode delivered implausibly little text despite the
    buffer actually sounding like it has sustained speech in it -- see the
    module comment above _SANITY_MIN_DURATION_S for the incident this
    guards against. Corroborates chars/sec against RMS coverage so a
    genuinely quiet/short recording is never flagged, only a long one that
    sounds like it should have produced far more text than it did.
    """
    if audio_duration < _SANITY_MIN_DURATION_S:
        return False
    cps = len(text) / audio_duration if audio_duration else 0.0
    if cps >= _SANITY_MIN_CPS:
        return False
    return _speech_rms_coverage(audio, sample_rate) >= _SANITY_MIN_SPEECH_COVERAGE


# One hotkey decode pass's full result -- what DictationApp._decode_hotkey_
# audio() returns and what _apply_retry_on_suspected_loss() compares between
# the original decode and its retry.
_HotkeyDecodeResult = collections.namedtuple(
    '_HotkeyDecodeResult', ['text', 'low_confidence', 'seg_list', 'detected_lang', 'diag_path',
                           'language_rejected'], defaults=[False],
)


def _apply_retry_on_suspected_loss(original, retry_fn, audio, sample_rate, audio_duration,
                                    is_command_lane):
    """Auto-retry recovery for _suspected_silent_data_loss (SPARK P0 fix,
    2026-07-18; see the decode-matrix module comment above
    _SANITY_MIN_DURATION_S).

    Free-form hotkey decodes already drop vocabulary from initial_prompt
    (see _build_hotkey_transcribe_params) -- the decode matrix showed that
    alone recovers both incident WAVs 10/10. This retry is the safety net
    for whatever the matrix didn't catch: faster-whisper is not perfectly
    deterministic run-to-run against identical params (the matrix's own
    baseline non-determinism section), so a second attempt at the SAME
    audio can independently succeed where the first didn't, and if the user
    has an explicit Priority-1 config['initial_prompt'] override set, this
    retry drops even that as a maximal last-resort attempt (retry_fn is
    expected to decode with initial_prompt="" specifically, not just
    whatever the original params already had).

    retry_fn is called AT MOST ONCE, and never at all for a command-lane
    decode (matcher-side recognition on short 1-3s utterances -- this
    mechanism exists for long free-form prose, and firing it there would
    just double the latency of every command miss for no benefit).

    Returns (result: _HotkeyDecodeResult, suspected_loss: bool, retried: bool).
    When both the original and the retry fail the sanity check, delivers
    whichever has more characters (never silently drops the longer one) and
    suspected_loss is still True so the caller's diagnostics outcome stays
    suspected_loss and the debug WAV dump (already unconditional, upstream
    of any decode attempt) is the only recovery record needed.
    """
    if original.language_rejected:
        # An intentional rejection is empty speech, not missing dictation.
        return original, False, False
    suspected_loss = _suspected_silent_data_loss(original.text, audio, sample_rate, audio_duration)
    if not suspected_loss or is_command_lane:
        return original, suspected_loss, False

    logger.warning(
        "[RETRY] Suspected silent data loss on free-form decode -- "
        "retrying once with initial_prompt=''"
    )
    retry = retry_fn()
    if retry.language_rejected:
        # The original passed the language gate; preserve that accepted text.
        return original, suspected_loss, True
    retry_suspected = _suspected_silent_data_loss(retry.text, audio, sample_rate, audio_duration)
    if not retry_suspected:
        logger.info(
            f"[RETRY] Recovered: {len(retry.text)} chars (was {len(original.text)})"
        )
        return retry, False, True

    winner = retry if len(retry.text) > len(original.text) else original
    logger.warning(
        f"[RETRY] Retry also failed the sanity check ({len(retry.text)} chars vs "
        f"original {len(original.text)}) -- delivering the longer of the two "
        f"({len(winner.text)} chars), outcome=suspected_loss"
    )
    return winner, True, True


def _split_audio_at_silences(
    audio,
    sample_rate,
    *,
    min_silence_s=0.3,
    max_chunk_s=25.0,
    silence_threshold=0.015,
):
    """Split long audio at silence boundaries for chunked Whisper transcription.

    Splits the waveform at pauses rather than at arbitrary 30-second
    boundaries so Whisper never straddles a word.  Does NOT discard any
    samples — every sample appears in exactly one returned chunk.

    Args:
        audio: float32 mono array at sample_rate Hz.
        sample_rate: samples per second (e.g. 16000).
        min_silence_s: minimum quiet-region duration to use as a split.
        max_chunk_s: target maximum chunk length; chunks are force-split
            here when no silence is found within the window.
        silence_threshold: per-window RMS below which a 100 ms window is
            counted as silence. 0.015 ≈ -36 dBFS; covers breath/room tone
            between sentences in typical microphone recordings.

    Returns:
        list of float32 arrays, always at least one element.
        Returns [audio] unchanged when the full recording fits in one chunk.
    """
    if len(audio) / sample_rate <= max_chunk_s:
        return [audio]

    win_samples = max(1, int(sample_rate * 0.1))   # 100 ms analysis windows
    n_windows   = len(audio) // win_samples
    if n_windows == 0:
        return [audio]

    # Vectorised RMS per window — much faster than a Python loop
    trimmed   = audio[:n_windows * win_samples]
    frames    = trimmed.reshape(n_windows, win_samples)
    rms       = np.sqrt(np.mean(frames ** 2, axis=1))
    is_silent = rms < silence_threshold

    # Collect candidate split points: centre of each silence run ≥ min_silence_s
    min_silent_wins = max(1, int(min_silence_s / 0.1))
    split_samples   = []
    i = 0
    while i < n_windows:
        if is_silent[i]:
            j = i
            while j < n_windows and is_silent[j]:
                j += 1
            if (j - i) >= min_silent_wins:
                split_samples.append(int(((i + j) // 2) * win_samples))
            i = j
        else:
            i += 1

    # Build chunks greedily: advance to the latest silence split within
    # max_chunk_s, or force-split there if no silence is found.
    max_chunk_samp = int(sample_rate * max_chunk_s)
    chunks, start  = [], 0
    while start < len(audio):
        target = start + max_chunk_samp
        if target >= len(audio):
            chunks.append(audio[start:])
            break
        candidates = [s for s in split_samples if start < s <= target]
        end        = candidates[-1] if candidates else target
        chunks.append(audio[start:end])
        start = end

    return chunks if chunks else [audio]


def _fade_edges(audio, sample_rate, fade_ms=_FADE_MS):
    """Apply a linear fade-in/out to the first/last fade_ms of audio.

    Kills the high-energy transient from the physical hotkey press/release,
    which Whisper otherwise hears as "click click click". Returns a new
    array; does not modify the input in place.

    Clamps the fade length to at most half the buffer so in/out ramps never
    overlap on a very short buffer (the hotkey path already skips anything
    under 0.51s, but this stays safe regardless of caller).
    """
    n = len(audio)
    fade_samples = int(sample_rate * fade_ms / 1000.0)
    fade_samples = min(fade_samples, n // 2)
    if fade_samples <= 0:
        return audio
    out = np.asarray(audio, dtype=np.float32).copy()
    ramp_in = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
    ramp_out = np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
    out[:fade_samples] *= ramp_in
    out[-fade_samples:] *= ramp_out
    return out


def _dump_hotkey_buffer(audio, sample_rate) -> None:
    """Opt-in diagnostic (config: debug.dump_hotkey_buffers, off by
    default): write the exact assembled hotkey buffer -- post-prepend,
    PRE-fade -- to ~/.samsara/debug/hotkey_<timestamp>.wav, so the raw
    seam (unmasked by the 50ms edge fade) can be listened to directly.
    2026-07-10 hotkey word-loss investigation. Never raises -- a dump
    failure must not affect the transcription it's diagnosing."""
    try:
        out_dir = Path.home() / ".samsara" / "debug"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        path = out_dir / f"hotkey_{ts}.wav"
        pcm_int16 = np.clip(
            np.asarray(audio, dtype=np.float32) * 32767.0, -32768, 32767
        ).astype(np.int16)
        with wave.open(str(path), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm_int16.tobytes())
        logger.debug(f"[SEAM] Dumped hotkey buffer -> {path}")
    except Exception as e:
        logger.debug(f"[SEAM] hotkey buffer dump failed (non-fatal): {e}")


def resample_audio(audio, orig_sr, target_sr=MODEL_SAMPLE_RATE):
    """Resample audio from orig_sr to target_sr using linear interpolation.

    Good enough for speech -- Whisper is robust to minor artifacts.
    Returns the input unchanged if rates already match.
    """
    if orig_sr == target_sr:
        return audio
    duration = len(audio) / orig_sr
    new_length = int(duration * target_sr)
    old_indices = np.linspace(0, len(audio) - 1, num=len(audio))
    new_indices = np.linspace(0, len(audio) - 1, num=new_length)
    return np.interp(new_indices, old_indices, audio).astype(np.float32)
