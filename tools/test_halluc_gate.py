"""Unit tests for the Whisper hallucination-gate helpers in dictation.py.

Exercises DictationApp._buffer_has_contiguous_speech (Fix 3), its ZCR+energy
fallback _zcr_energy_contiguous_speech (Fix 5), and the _fade_edges helper
(Fix 4) with synthetic buffers -- no audio hardware, no Qt, no full Samsara
boot.

Uses a lightweight duck-typed stand-in for DictationApp's `self`
(types.SimpleNamespace with the real methods bound via types.MethodType).
This matches the existing pattern in tests/test_dictation_app.py of calling
DictationApp.method(app, ...) against a minimal object rather than a fully
constructed app.

For the "real speech" case, a synthetic sine tone is deliberately NOT used:
Silero's neural VAD correctly rejects a pure tone (it lacks the broadband,
harmonic structure of a human voice), which would make that check look like
a false failure rather than a real one. Instead this reuses an existing TTS
speech asset already in the repo (assets/sounds/ava_cues/ready_01.wav) --
genuine speech audio, no new binary fixture added.

Run with: F:\\envs\\sami\\python.exe tools\\test_halluc_gate.py
"""

import sys
import threading
import types
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dictation

SAMPLE_RATE = 16000
READY_CUE_WAV = Path(__file__).resolve().parents[1] / "assets" / "sounds" / "ava_cues" / "ready_01.wav"


def _load_wav_float32(path):
    """Read a mono WAV file as float32 in [-1, 1], return (audio, sample_rate)."""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)
        sampwidth = w.getsampwidth()
    if sampwidth == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth}")
    return audio, rate


def _try_load_silero():
    """Best-effort load of the real Silero VAD model (mirrors _load_vad_model).

    Returns the model, or None if torch/the model can't be loaded in this
    process (e.g. no cached download, torch not installed).
    """
    if not dictation._TORCH_AVAILABLE:
        return None
    try:
        import torch
        model, _utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad", model="silero_vad", trust_repo=True,
        )
        return model
    except Exception as e:
        print(f"  (Silero load failed, will use ZCR fallback: {e})")
        return None


def _make_fake_app(vad_model):
    """Minimal duck-typed stand-in for DictationApp's `self`."""
    fake = types.SimpleNamespace()
    fake._vad_lock = threading.Lock()
    fake._vad_model = vad_model
    fake._vad_available = vad_model is not None
    fake._buffer_has_contiguous_speech = types.MethodType(
        dictation.DictationApp._buffer_has_contiguous_speech, fake)
    fake._zcr_energy_contiguous_speech = types.MethodType(
        dictation.DictationApp._zcr_energy_contiguous_speech, fake)
    return fake


def _click_buffer(duration_s=1.0, click_ms=20, click_pos_s=0.5):
    """1s of silence with a 20ms high-amplitude impulse (simulated click)."""
    n = int(duration_s * SAMPLE_RATE)
    audio = np.zeros(n, dtype=np.float32)
    click_n = int(click_ms / 1000.0 * SAMPLE_RATE)
    start = int(click_pos_s * SAMPLE_RATE)
    rng = np.random.default_rng(0)
    audio[start:start + click_n] = rng.uniform(-0.95, 0.95, click_n).astype(np.float32)
    return audio


def _white_noise_buffer(duration_s=2.0, amplitude=0.03):
    """2s of low-level white noise -- energy present but never contiguous voice."""
    n = int(duration_s * SAMPLE_RATE)
    rng = np.random.default_rng(1)
    return (rng.standard_normal(n).astype(np.float32) * amplitude)


def run_gate_cases(label, app):
    """Run the four required synthetic cases against `app`. Returns (passed, total)."""
    print(f"\n--- {label} ---")
    passed = 0
    total = 0

    def check(name, expected, audio, rate=SAMPLE_RATE):
        nonlocal passed, total
        total += 1
        actual = app._buffer_has_contiguous_speech(audio, rate)
        ok = actual == expected
        passed += int(ok)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: expected={expected} actual={actual}")

    check("pure silence (2s zeros)", False, np.zeros(int(2.0 * SAMPLE_RATE), dtype=np.float32))
    check("20ms click impulse in 1s silence", False, _click_buffer())
    check("2s low-level white noise", False, _white_noise_buffer())

    if READY_CUE_WAV.exists():
        speech_audio, speech_rate = _load_wav_float32(READY_CUE_WAV)
        check("real speech clip (ava_cues/ready_01.wav)", True, speech_audio, speech_rate)
    else:
        total += 1
        print(f"  [SKIP] real speech clip: {READY_CUE_WAV} not found")

    return passed, total


def run_fade_check():
    print("\n--- Fix 4: _fade_edges unit check ---")
    n = SAMPLE_RATE  # 1s buffer of constant amplitude 1.0
    audio = np.ones(n, dtype=np.float32)
    faded = dictation._fade_edges(audio, SAMPLE_RATE, dictation._FADE_MS)

    ok = True
    if abs(faded[0]) > 1e-6:
        ok = False
        print(f"  [FAIL] first sample should be ~0, got {faded[0]}")
    else:
        print(f"  [PASS] first sample is ~0 (fade-in start): {faded[0]}")

    if abs(faded[-1]) > 1e-6:
        ok = False
        print(f"  [FAIL] last sample should be ~0, got {faded[-1]}")
    else:
        print(f"  [PASS] last sample is ~0 (fade-out end): {faded[-1]}")

    mid = n // 2
    if abs(faded[mid] - 1.0) > 1e-6:
        ok = False
        print(f"  [FAIL] mid-buffer sample should be unchanged (1.0), got {faded[mid]}")
    else:
        print(f"  [PASS] mid-buffer sample unchanged: {faded[mid]}")

    return ok


def main():
    silero_model = _try_load_silero()

    all_passed = True
    total_checks = 0
    total_passed = 0

    if silero_model is not None:
        p, t = run_gate_cases("_buffer_has_contiguous_speech via real Silero VAD",
                               _make_fake_app(silero_model))
        total_passed += p
        total_checks += t
        all_passed &= (p == t)
    else:
        print("\nSilero VAD could not be loaded in this process -- exercising the "
              "ZCR fallback path instead (Fix 5), as _buffer_has_contiguous_speech "
              "falls back to it automatically when _vad_available is False.")

    # Always exercise the ZCR fallback directly (Fix 5), regardless of whether
    # Silero loaded above -- it's an independent code path and deserves its
    # own direct verification.
    p, t = run_gate_cases("_buffer_has_contiguous_speech via ZCR+energy fallback (forced)",
                           _make_fake_app(None))
    total_passed += p
    total_checks += t
    all_passed &= (p == t)

    fade_ok = run_fade_check()
    all_passed &= fade_ok

    print(f"\nRESULT: {total_passed}/{total_checks} gate checks passed, "
          f"fade check {'PASSED' if fade_ok else 'FAILED'}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
