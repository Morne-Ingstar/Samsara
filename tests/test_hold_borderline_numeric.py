"""Numeric hold regressions with generated PCM and model segments."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from tools.voice_corpus import RATE
from tools.voice_corpus.replay import run_take


@pytest.fixture
def replay(tmp_path, monkeypatch):
    for name in ("HOME", "USERPROFILE", "SAMSARA_HOME_DIR", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    from samsara import speech_presence

    def run(pcm, release, transcribe, *, spans=(), config=None):
        def measure(audio, rate):
            assert rate == RATE
            starts = np.arange((len(audio) + 511) // 512) * 512 / RATE
            scores = np.zeros(len(starts))
            for start, end in spans:
                scores[(starts >= start) & (starts < end)] = .9
            return speech_presence.Presence(known=True, frames=len(scores), samples=len(audio),
                                           probabilities=tuple(scores), max_probability=float(scores.max()))

        monkeypatch.setattr(speech_presence, "measure", measure)
        return run_take(pcm, release, NS(transcribe=transcribe), config, partials=False)

    return run


def _no_segments(_audio, **_params):
    return iter(()), NS(language="en")


def _segment(text, start, end, *, no_speech_prob=.01, avg_logprob=-.2):
    return NS(text=text, start=start, end=end, no_speech_prob=no_speech_prob,
              avg_logprob=avg_logprob, compression_ratio=1., temperature=0., words=None)


def test_loud_release_tail_admits_cutoff_frame_deterministically(replay):
    for release, samples in ((.4, 17600), (.6, 20800)):
        level = round(32767 * 10 ** (-12.9 / 20))
        pcm = np.full(samples, level, dtype="<i2")
        for _ in range(20):
            result = replay(pcm, release, _no_segments)
            assert result["captured_samples"] == samples
            assert result["pcm_preserved"]
            assert result["tail"]["frames"] == 7
            assert result["tail"]["elapsed_ms"] == 600
            assert result["tail"]["reason"] == "max"


def test_release_tail_ends_on_quiet_run_and_uses_configured_floor(replay):
    pcm = np.zeros(199400, dtype="<i2")
    # A longer source can still stop after the first quiet window.
    result = replay(pcm, 11.228, _no_segments)
    assert result["captured_samples"] == 185600
    assert result["tail"]["frames"] == 3
    assert result["tail"]["reason"] == "silence"
    assert result["pcm_preserved"]

    pcm[1600:6400] = 500
    for threshold, frames in ((.008, 5), (.02, 2)):
        result = replay(pcm, .1, _no_segments, config={
            "recording_tail_silence_ms": 200, "recording_tail_speech_threshold": threshold,
        })
        assert result["captured_samples"] == (1 + frames) * 1600
        assert result["tail"]["reason"] == "silence"
        assert result["pcm_preserved"]


def test_relaxed_retry_signoff_cannot_deliver(replay, monkeypatch):
    from samsara import hold_final_policy

    noise = np.random.default_rng(1389).standard_normal(round(.9 * RATE))
    spectrum = np.fft.rfft(noise)
    spectrum[0] = 0
    spectrum[1:] /= np.sqrt(np.arange(1, len(spectrum)))
    pink = np.fft.irfft(spectrum)
    pcm = np.rint(np.clip(pink * (10 ** (-12.8 / 20) / np.std(pink)), -1, 1) * 32767).astype("<i2")
    attempts, reasons = [], []

    def transcribe(audio, **params):
        attempts.append((params["no_speech_threshold"], audio))
        # This whole-utterance phrase is in the relaxed-retry signoff list.
        segments = [] if len(attempts) == 1 else [
            _segment("Thank you.", .25, .8, no_speech_prob=.62, avg_logprob=-.75)
        ]
        return iter(segments), NS(language="en")

    final_decode = hold_final_policy.final_decode

    def observe(worker, audio, params):
        result = final_decode(worker, audio, params)
        reasons.extend(entry[2] for entry in worker._session._hold_failed_ranges)
        return result

    monkeypatch.setattr(hold_final_policy, "final_decode", observe)
    result = replay(pcm, .3, transcribe, spans=((.25, .85),))
    assert [threshold for threshold, _ in attempts] == [.6, None]
    assert np.shares_memory(attempts[0][1], attempts[1][1])
    assert reasons == ["signoff:all_no_speech>=0.5"]
    assert result["decode_failed"]
    assert result["delivery_count"] == 0 and not result["text"]
    assert result["pcm_preserved"]


def test_quiet_aligned_late_speech_delivers_two_words(replay):
    pcm = np.zeros(round(9.7 * RATE), dtype="<i2")
    start, end = round(7.5 * RATE), round(8.2 * RATE)
    t = np.arange(end - start) / RATE
    envelope = np.maximum(0., np.sin(2 * np.pi * t / .35)) ** 4
    pcm[start:end] = np.rint(32767 * 10 ** (-26 / 20) * envelope * np.sin(2 * np.pi * 180 * t))

    def transcribe(audio, **_params):
        peak = np.max(np.abs(audio[start:end]))
        segments = [_segment("Amber lantern", 7.5, 8.2)] if peak > .01 else []
        return iter(segments), NS(language="en")

    spoken = replay(pcm, 9.4, transcribe, spans=((7.5, 8.2),))
    quiet = replay(np.zeros_like(pcm), 9.4, transcribe)
    assert spoken["captured_samples"] == quiet["captured_samples"] == len(pcm)
    assert spoken["pcm_preserved"] and quiet["pcm_preserved"]
    assert len(spoken["text"].split()) == 2 and spoken["delivery_count"] == 1
    assert not quiet["text"] and quiet["delivery_count"] == 0
