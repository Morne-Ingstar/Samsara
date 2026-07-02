#!/usr/bin/env python3
"""Generate the unified-session-mode earcon vocabulary for all themes.

Adds the following earcons to every theme directory under sounds/themes/:

  mode_command       - single confident grounded blip, "back at the hub"
  mode_dictate       - soft rising-then-falling sweep, "now listening for prose"
  mode_ava           - two short ascending chimes, "now talking to the agent" --
                       rhythmically distinct from both of the above: one blip
                       (command) and one continuous sweep (dictate) vs two
                       crisp rising notes here
  focus_lock_revert  - descending two-blip, warning (not alarming) -- focus
                       moved, injection suppressed, auto-reverted to COMMAND
  scratch_success    - quick descending "erase" swipe
  scratch_refuse     - flat, muted, harmonic-free double-blip -- "can't do that"
                       (deliberately un-melodic so it reads as a refusal, not
                       a lesser success)

Existing earcons (start, stop, success, error, and the Phase-2 Smart Actions
set from generate_earcons.py) are NOT touched.

Pipeline: same as generate_earcons.py -- 44.1 kHz mono 16-bit PCM, cosine
fade-in/out, per-theme pitch/duration/harmonic/gain tuning. Duplicated here
(not imported) so this stays a self-contained sibling script, matching the
existing generate_earcons.py / generate_sounds.py pattern.

Run:  python scripts/generate_session_mode_earcons.py
"""

import math
import struct
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 44100

THEMES = {
    'cute':    {'pitch': 1.20, 'dur': 0.90, 'harm': 0.35, 'gain': 0.55},
    'chirpy':  {'pitch': 1.35, 'dur': 0.75, 'harm': 0.45, 'gain': 0.50},
    'warm':    {'pitch': 0.70, 'dur': 1.25, 'harm': 0.20, 'gain': 0.45},
    'zen':     {'pitch': 0.85, 'dur': 1.40, 'harm': 0.55, 'gain': 0.40},
    'classic': {'pitch': 1.00, 'dur': 1.00, 'harm': 0.10, 'gain': 0.55},
}


# ---------------------------------------------------------------------------
# Low-level synthesis helpers (mirrors generate_earcons.py)
# ---------------------------------------------------------------------------

def _cosine_fade(samples, fade_in=0.008, fade_out=0.04):
    n = len(samples)
    n_in = int(SAMPLE_RATE * fade_in)
    n_out = int(SAMPLE_RATE * fade_out)
    for i in range(min(n_in, n)):
        f = 0.5 * (1 - math.cos(math.pi * i / max(n_in, 1)))
        samples[i] *= f
    for i in range(min(n_out, n)):
        f = 0.5 * (1 - math.cos(math.pi * i / max(n_out, 1)))
        samples[n - 1 - i] *= f


def _tone(t, freq, harm=0.2):
    phase = 2 * math.pi * freq * t
    s = math.sin(phase)
    if harm > 0:
        s += harm * math.sin(2 * phase)
        s += (harm * 0.4) * math.sin(3 * phase)
    return s / (1 + 1.4 * harm)


def _write_wav(path, samples, gain=0.6):
    peak = max((abs(s) for s in samples), default=1.0) or 1.0
    scale = (32767 * gain) / peak
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'w') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for s in samples:
            v = int(max(-32767, min(32767, s * scale)))
            wav.writeframes(struct.pack('<h', v))


def _silence(seconds):
    return [0.0] * int(SAMPLE_RATE * seconds)


# ---------------------------------------------------------------------------
# Per-earcon recipes
# ---------------------------------------------------------------------------

def make_mode_command(tune):
    """Enter COMMAND (the hub): single, confident, grounded blip."""
    pitch, dur_k, harm = tune['pitch'], tune['dur'], tune['harm']
    duration = 0.12 * dur_k
    freq = 540 * pitch

    n = int(SAMPLE_RATE * duration)
    samples = [0.0] * n
    for i in range(n):
        t = i / SAMPLE_RATE
        env = (1 - math.exp(-t * 100)) * math.exp(-t * 16)
        samples[i] = _tone(t, freq, harm) * env
    _cosine_fade(samples, 0.005, 0.02)
    return samples


def make_mode_dictate(tune):
    """Enter DICTATE: soft rise-and-fall sweep -- 'now listening for prose'."""
    pitch, dur_k, harm = tune['pitch'], tune['dur'], tune['harm']
    duration = 0.24 * dur_k
    f_start = 480 * pitch
    f_end = 760 * pitch

    n = int(SAMPLE_RATE * duration)
    samples = [0.0] * n
    for i in range(n):
        t = i / SAMPLE_RATE
        prog = i / max(n - 1, 1)
        freq = f_start + (f_end - f_start) * prog
        env = math.sin(math.pi * prog)  # smooth rise then fall, no hard edges
        samples[i] = _tone(t, freq, harm) * env
    _cosine_fade(samples, 0.01, 0.04)
    return samples


def make_mode_ava(tune):
    """Enter AVA: two short ascending chimes -- 'now talking to the agent'.
    Rhythmically distinct from mode_command (one blip) and mode_dictate (one
    continuous sweep): two crisp notes rising in pitch read as an attentive
    greeting rather than a hub-return or a listening-for-prose cue."""
    pitch, dur_k, harm = tune['pitch'], tune['dur'], tune['harm']
    blip_dur = 0.09 * dur_k
    gap_dur = 0.045 * dur_k
    f1 = 620 * pitch
    f2 = 880 * pitch

    def _blip(freq):
        n = int(SAMPLE_RATE * blip_dur)
        out = [0.0] * n
        for i in range(n):
            t = i / SAMPLE_RATE
            env = (1 - math.exp(-t * 90)) * math.exp(-t * 12)
            out[i] = _tone(t, freq, harm) * env
        return out

    samples = []
    samples.extend(_blip(f1))
    samples.extend(_silence(gap_dur))
    samples.extend(_blip(f2))
    _cosine_fade(samples, 0.006, 0.03)
    return samples


def make_focus_lock_revert(tune):
    """Focus moved, injection suppressed, auto-reverted: descending
    two-blip. Distinct from 'error' -- a warning, not an alarm."""
    pitch, dur_k, harm = tune['pitch'], tune['dur'], tune['harm']
    blip_dur = 0.08 * dur_k
    gap_dur = 0.04 * dur_k
    f1 = 500 * pitch
    f2 = 340 * pitch

    def _blip(freq):
        n = int(SAMPLE_RATE * blip_dur)
        out = [0.0] * n
        for i in range(n):
            t = i / SAMPLE_RATE
            env = (1 - math.exp(-t * 110)) * math.exp(-t * 15)
            out[i] = _tone(t, freq, harm) * env
        return out

    samples = []
    samples.extend(_blip(f1))
    samples.extend(_silence(gap_dur))
    samples.extend(_blip(f2))
    _cosine_fade(samples, 0.005, 0.03)
    return samples


def make_scratch_success(tune):
    """'scratch that' undid something: quick descending erase swipe."""
    pitch, dur_k, harm = tune['pitch'], tune['dur'], tune['harm']
    duration = 0.14 * dur_k
    f_start = 700 * pitch
    f_end = 320 * pitch

    n = int(SAMPLE_RATE * duration)
    samples = [0.0] * n
    for i in range(n):
        t = i / SAMPLE_RATE
        prog = i / max(n - 1, 1)
        freq = f_start + (f_end - f_start) * prog
        env = math.exp(-prog * 2.2)
        samples[i] = _tone(t, freq, harm) * env
    _cosine_fade(samples, 0.004, 0.035)
    return samples


def make_scratch_refuse(tune):
    """'scratch that' had nothing to undo / was blocked: flat, muted,
    harmonic-free double-blip at one pitch -- deliberately un-melodic so it
    reads as a refusal rather than a lesser success."""
    pitch, dur_k, _harm = tune['pitch'], tune['dur'], tune['harm']
    blip_dur = 0.06 * dur_k
    gap_dur = 0.03 * dur_k
    freq = 300 * pitch

    def _blip():
        n = int(SAMPLE_RATE * blip_dur)
        out = [0.0] * n
        for i in range(n):
            t = i / SAMPLE_RATE
            env = (1 - math.exp(-t * 130)) * math.exp(-t * 20)
            out[i] = math.sin(2 * math.pi * freq * t) * env  # no harmonics: flat on purpose
        return out

    samples = []
    samples.extend(_blip())
    samples.extend(_silence(gap_dur))
    samples.extend(_blip())
    _cosine_fade(samples, 0.004, 0.025)
    return samples


EARCONS = {
    'mode_command':      (make_mode_command,      1.00),
    'mode_dictate':       (make_mode_dictate,      1.00),
    'mode_ava':           (make_mode_ava,          1.00),
    'focus_lock_revert':  (make_focus_lock_revert, 1.00),
    'scratch_success':    (make_scratch_success,   0.95),
    'scratch_refuse':     (make_scratch_refuse,    0.85),
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def generate_theme(theme_name, tune, themes_root, overwrite=False):
    theme_dir = themes_root / theme_name
    written = []
    skipped = []
    for name, (recipe, name_gain) in EARCONS.items():
        out_path = theme_dir / f"{name}.wav"
        if out_path.exists() and not overwrite:
            skipped.append(name)
            continue
        samples = recipe(tune)
        gain = tune['gain'] * name_gain
        _write_wav(out_path, samples, gain=gain)
        written.append(name)
    print(f"  {theme_name}: wrote {len(written)} new, skipped {len(skipped)} existing")
    if written:
        print(f"    + {', '.join(written)}")
    if skipped:
        print(f"    = {', '.join(skipped)} (already present; use --force to overwrite)")


def main(argv=None):
    argv = argv or sys.argv[1:]
    overwrite = '--force' in argv

    repo_root = Path(__file__).resolve().parent.parent
    themes_root = repo_root / 'sounds' / 'themes'
    if not themes_root.exists():
        print(f"[ERROR] Themes directory not found: {themes_root}")
        return 1

    print(f"Generating session-mode earcons under {themes_root}")
    print(f"  overwrite={'YES' if overwrite else 'no'}")
    for theme_name, tune in THEMES.items():
        if not (themes_root / theme_name).exists():
            print(f"  [SKIP] theme dir missing: {theme_name}")
            continue
        generate_theme(theme_name, tune, themes_root, overwrite=overwrite)

    print("\nDone. Session-mode earcons available in every theme directory.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
