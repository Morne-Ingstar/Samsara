"""EMPIRICAL A/B bench for hands-free idle ducking (2026-07-24 amendment
item 5): "Ship only on measured improvement."

Runs the REAL adaptive wake-gate (dictation.DictationApp._wake_audio_is_
below_gate -- the exact per-utterance EMA noise-floor gate every wake-word
buffer passes through, see dictation.py:8289) against media-mixed audio,
duck OFF vs idle-duck ON, and reports whether the idle duck measurably
helps.

SCOPE / HONEST LIMITATIONS (read before trusting the numbers):
  - This benchmarks the adaptive RMS gate specifically -- the mechanism
    the idle duck exists to protect (see the "ducking" default_config
    block's own comment: AEC doesn't converge, so the 1500ms pre-buffer
    and the wake detector both hear media at whatever level is playing).
    It does NOT run the real OpenWakeWord model or Whisper transcription
    end-to-end: there is no recorded "hey samsara"/wake-phrase audio
    fixture in this repo to test true wake-word-model accuracy against,
    and running full Whisper decoding here would multiply runtime for a
    benchmark whose bottleneck (the gate) doesn't involve it at all.
  - "Speech" ground truth is a real recording (tests/fixtures/audio/
    long_dictation_*.wav -- genuine dictated speech with natural pauses),
    not synthetic. "Media" is synthetic pink-ish noise at a calibrated
    RMS, standing in for music/video bleed-through picked up by the mic
    -- there is no royalty-free real media clip in this repo to mix in
    instead, and noise is a defensible worst-case proxy (real media has
    more spectral structure a real gate might partially reject on other
    grounds; this is deliberately not more favorable than that).
  - "speech_pass_rate" is the proxy for wake rate / command accuracy:
    the fraction of real-speech-labeled 100ms chunks the gate accepts
    (doesn't reject as noise) -- rejecting real speech here is exactly
    what would make a user's command or wake phrase silently vanish.
  - "false_activation_rate" is measured on a media-ONLY run (no speech
    mixed in at all): the fraction of chunks the gate WRONGLY accepts as
    speech-like when nothing but media is present.

Usage:
    python -m tools.hands_free_duck_ab_bench
"""
from __future__ import annotations

import statistics
import sys
import types
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation

FRAME_MS = 100
SAMPLE_RATE = 16000
FRAME_SIZE = SAMPLE_RATE * FRAME_MS // 1000  # 1600

FIXTURES = [
    "tests/fixtures/audio/long_dictation_39s.wav",
    "tests/fixtures/audio/long_dictation_55s.wav",
    "tests/fixtures/audio/long_dictation_96s.wav",
]

# Calibrated media level: chosen below typical dictated-speech RMS in the
# fixtures (measured ~0.03-0.08) so the baseline scenario is "background
# media, not drowning out the user" -- a realistic bleed-through level,
# not a worst-case blast. Documented assumption, not measured from a real
# playback+recapture loop (not available in this environment).
MEDIA_RMS_NATURAL = 0.05
IDLE_DUCK_LEVEL = 0.8  # ducking.hands_free_idle_level default (this amendment)

SPEECH_LABEL_RMS_THRESHOLD = 0.02  # clean-signal RMS above this == "speech" chunk


def _load_wav_float32(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SAMPLE_RATE, f"{path}: expected {SAMPLE_RATE}Hz"
        assert w.getsampwidth() == 2, f"{path}: expected 16-bit PCM"
        raw = w.readframes(w.getnframes())
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm


def _synthesize_media_noise(n_samples: int, target_rms: float, seed: int) -> np.ndarray:
    """Pink-ish noise (1/f-weighted via cumulative sum + high-pass trend
    removal), normalized to target_rms. Deterministic (fixed seed) so
    reruns are comparable."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n_samples).astype(np.float32)
    pink = np.cumsum(white)
    pink -= np.mean(pink)
    # Remove slow drift (cumsum of white noise is a random walk -- a
    # simple first-difference high-pass keeps it audio-band-shaped
    # instead of a low-frequency wander).
    pink = np.diff(pink, prepend=pink[0])
    current_rms = float(np.sqrt(np.mean(pink ** 2))) or 1.0
    return pink * (target_rms / current_rms)


def _make_app() -> "types.SimpleNamespace":
    app = types.SimpleNamespace()
    app.config = {"wake_word_config": {"audio": {"adaptive_gate": True}}}
    app.audio_coordinator = None  # never TTS-frozen for this bench
    app._wake_noise_floor = None
    app._wake_gate_freeze_until = 0.0
    app._wake_gate_frozen = types.MethodType(dictation.DictationApp._wake_gate_frozen, app)
    app._wake_audio_is_below_gate = types.MethodType(
        dictation.DictationApp._wake_audio_is_below_gate, app,
    )
    return app


def _chunk_rms(signal: np.ndarray) -> list[float]:
    n_chunks = len(signal) // FRAME_SIZE
    out = []
    for i in range(n_chunks):
        chunk = signal[i * FRAME_SIZE:(i + 1) * FRAME_SIZE]
        out.append(float(np.sqrt(np.mean(chunk ** 2))))
    return out


def run_condition(speech: "np.ndarray | None", media_level: float, seed: int) -> dict:
    """Run one (speech-or-None, media_level) condition through a FRESH
    adaptive-gate app instance (own noise-floor state) and return per-
    chunk accept/reject decisions plus ground-truth speech labels."""
    n_samples = len(speech) if speech is not None else SAMPLE_RATE * 30
    media = _synthesize_media_noise(n_samples, MEDIA_RMS_NATURAL * media_level, seed)
    mixed = (speech + media) if speech is not None else media
    clean_rms_chunks = _chunk_rms(speech) if speech is not None else [0.0] * (n_samples // FRAME_SIZE)
    mixed_rms_chunks = _chunk_rms(mixed)

    app = _make_app()
    accepted = []
    for rms in mixed_rms_chunks:
        below_gate = app._wake_audio_is_below_gate(rms)
        accepted.append(not below_gate)

    speech_labels = [r >= SPEECH_LABEL_RMS_THRESHOLD for r in clean_rms_chunks]
    return {"accepted": accepted, "speech_labels": speech_labels}


def _rate(numerator_flags: list[bool]) -> float:
    return sum(numerator_flags) / len(numerator_flags) if numerator_flags else 0.0


def main() -> int:
    print("=" * 78)
    print("Hands-free idle-duck A/B bench -- adaptive wake-gate, media-mixed audio")
    print(f"media natural RMS={MEDIA_RMS_NATURAL}  idle_duck_level={IDLE_DUCK_LEVEL}  "
          f"speech label threshold={SPEECH_LABEL_RMS_THRESHOLD}")
    print("=" * 78)

    speech_pass_off: list[float] = []
    speech_pass_idle: list[float] = []

    for path in FIXTURES:
        speech = _load_wav_float32(path)

        off = run_condition(speech, media_level=1.0, seed=1)
        idle = run_condition(speech, media_level=IDLE_DUCK_LEVEL, seed=1)

        off_speech_accept = [a for a, s in zip(off["accepted"], off["speech_labels"]) if s]
        idle_speech_accept = [a for a, s in zip(idle["accepted"], idle["speech_labels"]) if s]

        off_rate = _rate(off_speech_accept)
        idle_rate = _rate(idle_speech_accept)
        speech_pass_off.append(off_rate)
        speech_pass_idle.append(idle_rate)

        n_speech_chunks = sum(off["speech_labels"])
        print(f"\n{Path(path).name}  ({n_speech_chunks} speech-labeled chunks "
              f"of {len(off['speech_labels'])} total)")
        print(f"  speech_pass_rate  duck OFF : {off_rate:.1%}")
        print(f"  speech_pass_rate  idle ON  : {idle_rate:.1%}")

    print("\n" + "-" * 78)
    print("Media-only (no speech) -- false-activation rate")
    off_media_only = run_condition(None, media_level=1.0, seed=2)
    idle_media_only = run_condition(None, media_level=IDLE_DUCK_LEVEL, seed=2)
    off_false_rate = _rate(off_media_only["accepted"])
    idle_false_rate = _rate(idle_media_only["accepted"])
    print(f"  false_activation_rate  duck OFF : {off_false_rate:.1%}")
    print(f"  false_activation_rate  idle ON  : {idle_false_rate:.1%}")

    print("\n" + "=" * 78)
    print("SUMMARY TABLE")
    print("=" * 78)
    mean_off = statistics.mean(speech_pass_off)
    mean_idle = statistics.mean(speech_pass_idle)
    print(f"{'metric':<28}{'duck OFF':>12}{'idle ON (0.8)':>16}{'delta':>10}")
    print(f"{'speech_pass_rate (mean)':<28}{mean_off:>11.1%} {mean_idle:>15.1%} "
          f"{mean_idle - mean_off:>+9.1%}")
    print(f"{'false_activation_rate':<28}{off_false_rate:>11.1%} {idle_false_rate:>15.1%} "
          f"{idle_false_rate - off_false_rate:>+9.1%}")

    print("\nSHIP GATE: idle duck must not regress speech_pass_rate and must not")
    print("increase false_activation_rate (per item 5, \"ship only on measured")
    print("improvement\").")
    speech_ok = mean_idle >= mean_off
    false_ok = idle_false_rate <= off_false_rate
    verdict = "PASS" if (speech_ok and false_ok) else "FAIL"
    print(f"  [{'PASS' if speech_ok else 'FAIL'}] speech_pass_rate: {mean_idle:.1%} >= {mean_off:.1%}")
    print(f"  [{'PASS' if false_ok else 'FAIL'}] false_activation_rate: {idle_false_rate:.1%} <= {off_false_rate:.1%}")
    print(f"  VERDICT: {verdict}")

    print("\n" + "=" * 78)
    print("SENSITIVITY: does the improvement hold across other media loudness levels?")
    print("(same fixtures/methodology, MEDIA_RMS_NATURAL varied)")
    print("=" * 78)
    print(f"{'media_rms':>10}{'speech_pass OFF':>18}{'speech_pass IDLE':>18}{'delta':>10}")
    speech_data = [_load_wav_float32(p) for p in FIXTURES]
    for level in (0.025, MEDIA_RMS_NATURAL, 0.08, 0.15):
        offs, idles = [], []
        for speech in speech_data:
            off = run_condition(speech, media_level=1.0 * (level / MEDIA_RMS_NATURAL), seed=1)
            idle = run_condition(speech, media_level=IDLE_DUCK_LEVEL * (level / MEDIA_RMS_NATURAL), seed=1)
            off_acc = [a for a, s in zip(off["accepted"], off["speech_labels"]) if s]
            idle_acc = [a for a, s in zip(idle["accepted"], idle["speech_labels"]) if s]
            offs.append(_rate(off_acc))
            idles.append(_rate(idle_acc))
        m_off, m_idle = statistics.mean(offs), statistics.mean(idles)
        print(f"{level:>10.3f}{m_off:>17.1%} {m_idle:>17.1%} {m_idle - m_off:>+9.1%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
