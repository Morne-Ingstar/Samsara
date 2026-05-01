#!/usr/bin/env python3
"""High-quality sound generation for Samsara - smooth, no artifacts"""

import wave
import struct
import math
from pathlib import Path

SAMPLE_RATE = 44100

def write_wav(filename, samples):
    """Write samples with normalization to prevent clipping"""
    # Normalize to prevent clipping
    max_val = max(abs(s) for s in samples) if samples else 1
    if max_val > 32767:
        samples = [s * 32767 / max_val for s in samples]
    
    with wave.open(str(filename), 'w') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for sample in samples:
            clamped = max(-32767, min(32767, int(sample)))
            wav.writeframes(struct.pack('<h', clamped))

def smooth_value(current, target, smoothing=0.99):
    """One-pole lowpass filter for smoothing"""
    return current * smoothing + target * (1 - smoothing)

def generate_with_fade(duration, generator_func, fade_in=0.01, fade_out=0.05, silence_pad=0.05):
    """Generate samples with proper fade in/out and silence padding"""
    num_samples = int(SAMPLE_RATE * duration)
    fade_in_samples = int(SAMPLE_RATE * fade_in)
    fade_out_samples = int(SAMPLE_RATE * fade_out)
    silence_samples = int(SAMPLE_RATE * silence_pad)
    
    samples = []
    for i in range(num_samples):
        t = i / SAMPLE_RATE
        
        # Get raw sample
        raw = generator_func(t, duration)
        
        # Apply fade in (smooth cosine curve)
        if i < fade_in_samples:
            fade = 0.5 * (1 - math.cos(math.pi * i / fade_in_samples))
            raw *= fade
        
        # Apply fade out (smooth cosine curve) - longer fade
        elif i > num_samples - fade_out_samples:
            remaining = num_samples - i
            fade = 0.5 * (1 - math.cos(math.pi * remaining / fade_out_samples))
            raw *= fade
        
        samples.append(raw * 32767)
    
    # Add silence padding at the end (prevents cutoff artifacts)
    samples.extend([0] * silence_samples)
    
    return samples

# ============================================================================
# CHIRPY THEME - Clean, artifact-free
# ============================================================================

def chirpy_start_gen(t, duration):
    """Double chirp - clean version"""
    sample = 0
    
    # First chirp at t=0
    if t < 0.12:
        freq = 1200 + 500 * math.exp(-t * 35)
        env = math.exp(-t * 15) * min(t * 100, 1.0)  # Smooth attack
        sample += math.sin(2 * math.pi * freq * t) * env * 0.5
    
    # Second chirp at t=0.08
    if t > 0.07 and t < 0.2:
        t2 = t - 0.07
        freq = 1400 + 400 * math.exp(-t2 * 40)
        env = math.exp(-t2 * 15) * min(t2 * 100, 1.0)
        sample += math.sin(2 * math.pi * freq * t2) * env * 0.6
    
    return sample * 0.7

def chirpy_stop_gen(t, duration):
    """Descending chirp"""
    freq = 1400 - 600 * (t / duration)
    env = (1 - (t/duration)**0.5) * min(t * 80, 1.0)
    return math.sin(2 * math.pi * freq * t) * env * 0.5

def chirpy_success_gen(t, duration):
    """Triple ascending chirp"""
    sample = 0
    
    notes = [(0.0, 1100), (0.09, 1400), (0.18, 1700)]
    
    for start, base_freq in notes:
        if t >= start and t < start + 0.15:
            nt = t - start
            freq = base_freq + 300 * math.exp(-nt * 30)
            env = math.exp(-nt * 12) * min(nt * 100, 1.0)
            sample += math.sin(2 * math.pi * freq * nt) * env * 0.45
    
    return sample

def chirpy_error_gen(t, duration):
    """Gentle low wobble"""
    wobble = 1 + 0.08 * math.sin(2 * math.pi * 10 * t)
    freq = 500 * wobble * (1 - t * 0.25)
    env = (1 - (t/duration)**0.7) * min(t * 60, 1.0)
    return math.sin(2 * math.pi * freq * t) * env * 0.4

# ============================================================================
# CUTE THEME - Clean bloops
# ============================================================================

def cute_start_gen(t, duration):
    """Double bloop"""
    sample = 0
    
    # First bloop
    if t < 0.14:
        freq = 659 * (1 + 0.25 * math.exp(-t * 25))
        env = math.exp(-t * 12) * min(t * 80, 1.0)
        tone = math.sin(2 * math.pi * freq * t) * 0.7
        tone += math.sin(4 * math.pi * freq * t) * 0.2
        sample += tone * env * 0.6
    
    # Second bloop
    if t > 0.08:
        t2 = t - 0.08
        freq = 784 * (1 + 0.25 * math.exp(-t2 * 25))
        env = math.exp(-t2 * 12) * min(t2 * 80, 1.0)
        tone = math.sin(2 * math.pi * freq * t2) * 0.7
        tone += math.sin(4 * math.pi * freq * t2) * 0.2
        sample += tone * env * 0.7
    
    return sample * 0.6

def cute_stop_gen(t, duration):
    """Descending bloop"""
    freq = 600 * (1 - t * 1.2)
    freq = max(freq, 350)
    env = (1 - (t/duration)**0.6) * min(t * 60, 1.0)
    tone = math.sin(2 * math.pi * freq * t) * 0.7
    tone += math.sin(4 * math.pi * freq * t) * 0.2
    return tone * env * 0.5

def cute_success_gen(t, duration):
    """C-E-G ascending bloops"""
    sample = 0
    notes = [(0.0, 523, 0.6), (0.1, 659, 0.7), (0.2, 784, 0.85)]
    
    for start, freq, vol in notes:
        if t >= start and t < start + 0.18:
            nt = t - start
            f = freq * (1 + 0.2 * math.exp(-nt * 25))
            env = math.exp(-nt * 10) * min(nt * 80, 1.0)
            tone = math.sin(2 * math.pi * f * nt) * 0.7
            tone += math.sin(4 * math.pi * f * nt) * 0.2
            sample += tone * env * vol
    
    return sample * 0.5

def cute_error_gen(t, duration):
    """Soft wobble"""
    wobble = 1 + 0.06 * math.sin(2 * math.pi * 12 * t)
    freq = 300 * wobble * (1 - t * 0.3)
    env = (1 - (t/duration)**0.6) * min(t * 50, 1.0)
    return math.sin(2 * math.pi * freq * t) * env * 0.4

# ============================================================================
# WARM THEME - Smooth pads
# ============================================================================

def warm_pad(t, freq, detune=0.003):
    """Warm layered pad tone"""
    phase = 2 * math.pi * freq * t
    tone = math.sin(phase) * 0.5
    tone += math.sin(phase * (1 + detune)) * 0.25  # Slight detune for warmth
    tone += math.sin(phase * 2) * 0.15
    tone += math.sin(phase * 0.5) * 0.1  # Sub
    return tone

def warm_start_gen(t, duration):
    """Warm chord swell"""
    # Cmaj7 voicing
    freqs = [130.81, 164.81, 196.00, 246.94]
    
    # Smooth swell envelope
    env = math.sin(math.pi * t / duration) ** 0.4
    attack = min(t * 8, 1.0)
    
    sample = sum(warm_pad(t, f) * 0.22 for f in freqs)
    return sample * env * attack * 0.5

def warm_stop_gen(t, duration):
    """Settling resolution"""
    env = (1 - (t/duration)**0.4) * min(t * 15, 1.0)
    sample = warm_pad(t, 196.00) * 0.35 + warm_pad(t, 130.81) * 0.3
    return sample * env * 0.5

def warm_success_gen(t, duration):
    """Rising resolution"""
    sample = 0
    notes = [(0.0, 261.63, 0.18), (0.12, 329.63, 0.18), (0.24, 392.00, 0.22)]
    
    for start, freq, hold in notes:
        if start <= t < start + hold + 0.08:
            nt = t - start
            # Smooth envelope for each note
            if nt < 0.03:
                env = nt / 0.03
            elif nt > hold:
                env = max(0, 1 - (nt - hold) / 0.08)
            else:
                env = 1.0
            sample += warm_pad(nt, freq) * env * 0.3
    
    return sample * 0.55

def warm_error_gen(t, duration):
    """Pensive low tone"""
    env = math.sin(math.pi * t / duration) * min(t * 12, 1.0)
    sample = warm_pad(t, 146.83) * 0.35 + warm_pad(t, 138.59) * 0.2
    return sample * env * 0.45

# ============================================================================
# ZEN THEME - Singing bowls
# ============================================================================

def bowl_harmonics(t, freq):
    """Singing bowl with inharmonic overtones"""
    tone = math.sin(2 * math.pi * freq * t)
    tone += 0.4 * math.sin(2 * math.pi * freq * 2.4 * t)  # Slightly sharp 2nd
    tone += 0.2 * math.sin(2 * math.pi * freq * 3.8 * t)
    tone += 0.1 * math.sin(2 * math.pi * freq * 5.1 * t)
    return tone

def zen_start_gen(t, duration):
    """Singing bowl tap"""
    env = math.exp(-t * 5) * min(t * 40, 1.0)
    return bowl_harmonics(t, 520) * env * 0.4

def zen_stop_gen(t, duration):
    """Soft descending bowl"""
    freq = 550 - 150 * (t / duration)
    env = (1 - (t/duration)**0.5) * min(t * 50, 1.0)
    return bowl_harmonics(t, freq) * env * 0.35

def zen_success_gen(t, duration):
    """Three bowl taps ascending"""
    sample = 0
    for start, freq, vol in [(0.0, 440, 0.8), (0.12, 523, 0.9), (0.25, 659, 1.0)]:
        if t >= start:
            nt = t - start
            env = math.exp(-nt * 4) * min(nt * 50, 1.0)
            sample += bowl_harmonics(nt, freq) * env * vol * 0.3
    return sample

def zen_error_gen(t, duration):
    """Low bowl hum"""
    env = math.sin(math.pi * t / duration) * min(t * 30, 1.0)
    return bowl_harmonics(t, 220) * env * 0.3

# ============================================================================
# MAIN
# ============================================================================

def main():
    sounds_dir = Path(__file__).parent / 'sounds'
    themes_dir = sounds_dir / 'themes'
    
    print("Generating high-quality sounds (no artifacts)...\n")
    
    themes = {
        'chirpy': {
            'start': (0.25, chirpy_start_gen),
            'stop': (0.18, chirpy_stop_gen),
            'success': (0.38, chirpy_success_gen),
            'error': (0.22, chirpy_error_gen),
        },
        'cute': {
            'start': (0.28, cute_start_gen),
            'stop': (0.2, cute_stop_gen),
            'success': (0.42, cute_success_gen),
            'error': (0.22, cute_error_gen),
        },
        'warm': {
            'start': (0.5, warm_start_gen),
            'stop': (0.35, warm_stop_gen),
            'success': (0.5, warm_success_gen),
            'error': (0.3, warm_error_gen),
        },
        'zen': {
            'start': (0.45, zen_start_gen),
            'stop': (0.3, zen_stop_gen),
            'success': (0.55, zen_success_gen),
            'error': (0.35, zen_error_gen),
        },
    }
    
    for theme_name, sounds in themes.items():
        theme_dir = themes_dir / theme_name
        theme_dir.mkdir(parents=True, exist_ok=True)
        
        for sound_name, (duration, gen_func) in sounds.items():
            samples = generate_with_fade(duration, gen_func, fade_in=0.008, fade_out=0.05, silence_pad=0.06)
            write_wav(theme_dir / f'{sound_name}.wav', samples)
        
        print(f"  ✓ {theme_name}")
    
    # Set chirpy as default
    print("\nSetting 'chirpy' as default...")
    import shutil
    chirpy_dir = themes_dir / 'chirpy'
    for wav in chirpy_dir.glob('*.wav'):
        shutil.copy2(wav, sounds_dir / wav.name)
    
    print("\n✨ Done! All themes regenerated with clean audio.")

if __name__ == '__main__':
    main()
