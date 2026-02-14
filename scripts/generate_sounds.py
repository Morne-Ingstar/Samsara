#!/usr/bin/env python3
"""Generate sound themes for Samsara"""

import wave
import struct
import math
from pathlib import Path

SAMPLE_RATE = 44100

def write_wav(filename, samples):
    """Write samples to a WAV file"""
    with wave.open(str(filename), 'w') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for sample in samples:
            clamped = max(-32767, min(32767, int(sample)))
            wav.writeframes(struct.pack('<h', clamped))

# ============================================================================
# CUTE THEME - Playful bloops, Nintendo/Duolingo vibes
# ============================================================================

def cute_bloop(t, freq, duration=0.1):
    """Cute bouncy bloop with pitch drop"""
    # Pitch drops slightly for bouncy feel
    pitch_env = 1.0 + 0.3 * math.exp(-t * 25)
    
    # Rounded square-ish wave (warm but defined)
    phase = 2 * math.pi * freq * pitch_env * t
    tone = math.sin(phase) * 0.7
    tone += math.sin(phase * 2) * 0.2  # Adds body
    tone += math.sin(phase * 3) * 0.1  # Adds sparkle
    
    # Bouncy envelope - quick attack, natural decay
    env = math.exp(-t * 12) * (1 - math.exp(-t * 100))
    
    return tone * env

def generate_cute_start():
    """Two quick ascending bloops - 'boop-boop!' ready"""
    duration = 0.28
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        sample = 0
        
        # First bloop: E5
        if t < 0.14:
            sample += cute_bloop(t, 659.25) * 0.7
        
        # Second bloop: G5 (higher, slightly delayed)
        if t > 0.09:
            t2 = t - 0.09
            sample += cute_bloop(t2, 783.99) * 0.8
        
        samples.append(sample * 32767 * 0.5)
    
    return samples

def generate_cute_stop():
    """Single descending bloop - 'bwoop' done"""
    duration = 0.18
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        
        # Descending pitch
        freq = 600 * (1.0 - t * 1.5)
        freq = max(freq, 350)
        
        phase = 2 * math.pi * freq * t
        tone = math.sin(phase) * 0.7 + math.sin(phase * 2) * 0.2
        
        env = math.exp(-t * 10) * (1 - math.exp(-t * 80))
        
        samples.append(tone * env * 32767 * 0.5)
    
    return samples

def generate_cute_success():
    """Triumphant three-note jingle - 'da-da-DA!'"""
    duration = 0.4
    samples = []
    
    # C5-E5-G5 (major triad, ascending)
    notes = [(0.00, 523.25, 0.7), (0.10, 659.25, 0.8), (0.20, 783.99, 1.0)]
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        sample = 0
        
        for start, freq, vol in notes:
            if t >= start:
                nt = t - start
                sample += cute_bloop(nt, freq) * vol
        
        samples.append(sample * 32767 * 0.45)
    
    return samples

def generate_cute_error():
    """Gentle 'wonk' - not harsh, just a soft nope"""
    duration = 0.2
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        
        # Low pitch with wobble
        wobble = 1 + 0.05 * math.sin(2 * math.pi * 12 * t)
        freq = 280 * wobble * (1 - t * 0.4)
        
        phase = 2 * math.pi * freq * t
        tone = math.sin(phase) * 0.8 + math.sin(phase * 2) * 0.15
        
        env = math.exp(-t * 8) * (1 - math.exp(-t * 60))
        
        samples.append(tone * env * 32767 * 0.4)
    
    return samples

# ============================================================================
# WARM THEME - OS boot sound vibes
# ============================================================================

def soft_pad(t, freq):
    """Warm pad tone"""
    phase = 2 * math.pi * freq * t
    tone = math.sin(phase) * 0.6
    tone += math.sin(phase * 2) * 0.25
    tone += math.sin(phase * 0.5) * 0.15
    return tone

def smooth_env(t, duration, attack=0.1, release=0.3):
    """Smooth envelope"""
    if t < attack:
        return (t / attack) ** 0.5
    elif t > duration - release:
        return ((duration - t) / release) ** 0.5
    return 1.0

def generate_warm_start():
    """Warm chord swell"""
    duration = 0.5
    samples = []
    freqs = [130.81, 164.81, 196.00, 246.94]  # Cmaj7
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        env = smooth_env(t, duration, 0.15, 0.2)
        vol = math.sin(math.pi * t / duration) ** 0.3
        
        sample = sum(soft_pad(t, f) * 0.25 for f in freqs)
        samples.append(sample * env * vol * 32767 * 0.4)
    
    return samples

def generate_warm_stop():
    """Settling resolution"""
    duration = 0.35
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        env = smooth_env(t, duration, 0.05, 0.15)
        
        sample = soft_pad(t, 196.00) * 0.4 + soft_pad(t, 130.81) * 0.3
        samples.append(sample * env * 32767 * 0.4)
    
    return samples

def generate_warm_success():
    """Uplifting resolution"""
    duration = 0.5
    samples = []
    notes = [(0.0, 261.63, 0.2), (0.12, 329.63, 0.22), (0.25, 392.00, 0.28)]
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        sample = 0
        
        for start, freq, hold in notes:
            if start <= t < start + hold + 0.1:
                nt = t - start
                env = smooth_env(nt, hold + 0.1, 0.04, 0.08)
                sample += soft_pad(nt, freq) * env * 0.35
        
        samples.append(sample * 32767 * 0.45)
    
    return samples

def generate_warm_error():
    """Pensive tone"""
    duration = 0.3
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        env = smooth_env(t, duration, 0.06, 0.15)
        
        sample = soft_pad(t, 146.83) * 0.4 + soft_pad(t, 138.59) * 0.2
        samples.append(sample * env * 32767 * 0.35)
    
    return samples

# ============================================================================
# ZEN THEME - Singing bowls and chimes
# ============================================================================

def bowl_tone(t, freq):
    """Singing bowl harmonics"""
    tone = math.sin(2 * math.pi * freq * t)
    tone += 0.5 * math.sin(2 * math.pi * freq * 2.4 * t)
    tone += 0.25 * math.sin(2 * math.pi * freq * 3.8 * t)
    return tone * math.exp(-t * 5)

def generate_zen_start():
    """Singing bowl tap"""
    duration = 0.4
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        attack = min(t * 50, 1.0)
        sample = bowl_tone(t, 520) * attack * 0.35
        samples.append(sample * 32767)
    
    return samples

def generate_zen_stop():
    """Soft descending tone"""
    duration = 0.25
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        freq = 550 - 170 * (t / duration)
        env = math.sin(math.pi * t / duration) ** 0.5
        
        tone = math.sin(2 * math.pi * freq * t)
        samples.append(tone * env * 32767 * 0.3)
    
    return samples

def generate_zen_success():
    """Gentle chimes"""
    duration = 0.45
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        sample = 0
        
        for time_offset, freq, vol in [(0.05, 523, 1.0), (0.15, 659, 0.8), (0.28, 784, 0.6)]:
            env = math.exp(-((t - time_offset) ** 2) / 0.01)
            sample += math.sin(2 * math.pi * freq * t) * env * vol
        
        samples.append(sample * 32767 * 0.3)
    
    return samples

def generate_zen_error():
    """Low soft tone"""
    duration = 0.3
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        env = math.sin(math.pi * t / duration)
        tone = math.sin(2 * math.pi * 220 * t) + 0.5 * math.sin(2 * math.pi * 165 * t)
        samples.append(tone * env * 32767 * 0.2)
    
    return samples

# ============================================================================
# MAIN
# ============================================================================

def generate_theme(name, generators, base_path):
    """Generate all sounds for a theme"""
    theme_dir = base_path / 'themes' / name
    theme_dir.mkdir(parents=True, exist_ok=True)
    
    for sound_name, gen_func in generators.items():
        samples = gen_func()
        write_wav(theme_dir / f'{sound_name}.wav', samples)
    
    print(f"  ✓ {name}")

def main():
    sounds_dir = Path(__file__).parent / 'sounds'
    
    print("Generating sound themes for Samsara...\n")
    
    # Cute theme
    generate_theme('cute', {
        'start': generate_cute_start,
        'stop': generate_cute_stop,
        'success': generate_cute_success,
        'error': generate_cute_error,
    }, sounds_dir)
    
    # Warm theme
    generate_theme('warm', {
        'start': generate_warm_start,
        'stop': generate_warm_stop,
        'success': generate_warm_success,
        'error': generate_warm_error,
    }, sounds_dir)
    
    # Zen theme
    generate_theme('zen', {
        'start': generate_zen_start,
        'stop': generate_zen_stop,
        'success': generate_zen_success,
        'error': generate_zen_error,
    }, sounds_dir)
    
    # Set cute as default (copy to main sounds folder)
    print("\nSetting 'cute' as default theme...")
    import shutil
    for wav in (sounds_dir / 'themes' / 'cute').glob('*.wav'):
        shutil.copy2(wav, sounds_dir / wav.name)
    
    print("\n✨ Done! Themes available: cute, warm, zen, classic")
    print("Default theme set to: cute")

if __name__ == '__main__':
    main()
