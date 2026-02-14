#!/usr/bin/env python3
"""Generate many sound variations for Samsara - audition and pick favorites"""

import wave
import struct
import math
import random
from pathlib import Path

SAMPLE_RATE = 44100

def write_wav(filename, samples):
    with wave.open(str(filename), 'w') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for sample in samples:
            clamped = max(-32767, min(32767, int(sample)))
            wav.writeframes(struct.pack('<h', clamped))

# ============================================================================
# CHIRPY SOUNDS - Bird-like, bright, cheerful
# ============================================================================

def chirp(t, base_freq, chirp_speed=30, freq_range=400):
    """Single chirp with frequency sweep"""
    freq = base_freq + freq_range * math.exp(-t * chirp_speed)
    phase = 2 * math.pi * freq * t
    tone = math.sin(phase) * 0.8 + math.sin(phase * 2) * 0.2
    env = math.exp(-t * 15) * (1 - math.exp(-t * 200))
    return tone * env

def generate_chirpy_start():
    """Quick double chirp - 'chirp-chirp!' ready"""
    duration = 0.25
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        sample = 0
        
        # First chirp
        if t < 0.12:
            sample += chirp(t, 1200, chirp_speed=35, freq_range=600) * 0.6
        
        # Second chirp (slightly higher)
        if t > 0.08:
            t2 = t - 0.08
            sample += chirp(t2, 1400, chirp_speed=40, freq_range=500) * 0.7
        
        samples.append(sample * 32767 * 0.5)
    return samples

def generate_chirpy_stop():
    """Descending chirp - 'bwee' done"""
    duration = 0.15
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        # Descending sweep
        freq = 1600 - 800 * (t / duration)
        phase = 2 * math.pi * freq * t
        tone = math.sin(phase)
        env = (1 - t/duration) ** 0.5 * (1 - math.exp(-t * 150))
        samples.append(tone * env * 32767 * 0.4)
    return samples

def generate_chirpy_success():
    """Happy triple chirp ascending"""
    duration = 0.35
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        sample = 0
        
        for start, freq in [(0.0, 1100), (0.09, 1400), (0.18, 1800)]:
            if t >= start:
                nt = t - start
                sample += chirp(nt, freq, chirp_speed=25, freq_range=300) * 0.6
        
        samples.append(sample * 32767 * 0.5)
    return samples

def generate_chirpy_error():
    """Low wobble chirp - gentle 'nope'"""
    duration = 0.2
    samples = []
    
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        wobble = 1 + 0.1 * math.sin(2 * math.pi * 8 * t)
        freq = 600 * wobble * (1 - t * 0.3)
        phase = 2 * math.pi * freq * t
        tone = math.sin(phase)
        env = math.exp(-t * 6) * (1 - math.exp(-t * 100))
        samples.append(tone * env * 32767 * 0.35)
    return samples

# ============================================================================
# VARIATION GENERATORS - Create many options to audition
# ============================================================================

def gen_bloop_variation(base_freq, pitch_drop, decay_speed, vol=0.5):
    """Generate a bloop with specific parameters"""
    duration = 0.2
    samples = []
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        pitch_env = 1.0 + pitch_drop * math.exp(-t * 25)
        phase = 2 * math.pi * base_freq * pitch_env * t
        tone = math.sin(phase) * 0.7 + math.sin(phase * 2) * 0.2
        env = math.exp(-t * decay_speed) * (1 - math.exp(-t * 100))
        samples.append(tone * env * 32767 * vol)
    return samples

def gen_chirp_variation(base_freq, sweep_range, sweep_speed, vol=0.5):
    """Generate a chirp with specific parameters"""
    duration = 0.15
    samples = []
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        freq = base_freq + sweep_range * math.exp(-t * sweep_speed)
        phase = 2 * math.pi * freq * t
        tone = math.sin(phase)
        env = math.exp(-t * 12) * (1 - math.exp(-t * 150))
        samples.append(tone * env * 32767 * vol)
    return samples

def gen_double_bloop(freq1, freq2, gap, vol=0.5):
    """Two bloops in sequence"""
    duration = 0.3
    samples = []
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        sample = 0
        
        # First bloop
        if t < 0.15:
            pitch = freq1 * (1.0 + 0.2 * math.exp(-t * 25))
            phase = 2 * math.pi * pitch * t
            tone = math.sin(phase) * 0.7 + math.sin(phase * 2) * 0.2
            env = math.exp(-t * 12) * (1 - math.exp(-t * 100))
            sample += tone * env
        
        # Second bloop
        if t > gap:
            t2 = t - gap
            pitch = freq2 * (1.0 + 0.2 * math.exp(-t2 * 25))
            phase = 2 * math.pi * pitch * t2
            tone = math.sin(phase) * 0.7 + math.sin(phase * 2) * 0.2
            env = math.exp(-t2 * 12) * (1 - math.exp(-t2 * 100))
            sample += tone * env
        
        samples.append(sample * 32767 * vol)
    return samples

def gen_triple_ascend(freqs, vol=0.5):
    """Three ascending notes"""
    duration = 0.4
    samples = []
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        sample = 0
        
        for idx, freq in enumerate(freqs):
            start = idx * 0.1
            if t >= start:
                nt = t - start
                pitch = freq * (1.0 + 0.15 * math.exp(-nt * 30))
                phase = 2 * math.pi * pitch * nt
                tone = math.sin(phase) * 0.7 + math.sin(phase * 2) * 0.2
                env = math.exp(-nt * 10) * (1 - math.exp(-nt * 100))
                sample += tone * env * (0.6 + idx * 0.15)
        
        samples.append(sample * 32767 * vol)
    return samples

def gen_chime(freq, harmonics=[1, 2, 3], decay=8, vol=0.4):
    """Bell/chime tone"""
    duration = 0.4
    samples = []
    for i in range(int(SAMPLE_RATE * duration)):
        t = i / SAMPLE_RATE
        sample = 0
        for idx, h in enumerate(harmonics):
            weight = 1.0 / (idx + 1)
            sample += math.sin(2 * math.pi * freq * h * t) * weight
        env = math.exp(-t * decay) * (1 - math.exp(-t * 50))
        samples.append(sample * env * 32767 * vol)
    return samples

def main():
    base_dir = Path(__file__).parent / 'sounds'
    
    # Create chirpy theme
    print("Creating chirpy theme...")
    chirpy_dir = base_dir / 'themes' / 'chirpy'
    chirpy_dir.mkdir(parents=True, exist_ok=True)
    
    write_wav(chirpy_dir / 'start.wav', generate_chirpy_start())
    write_wav(chirpy_dir / 'stop.wav', generate_chirpy_stop())
    write_wav(chirpy_dir / 'success.wav', generate_chirpy_success())
    write_wav(chirpy_dir / 'error.wav', generate_chirpy_error())
    print("  ✓ chirpy theme created")
    
    # Create variations folder
    var_dir = base_dir / 'variations'
    var_dir.mkdir(exist_ok=True)
    
    print("\nGenerating START sound variations...")
    start_dir = var_dir / 'start'
    start_dir.mkdir(exist_ok=True)
    
    variations = []
    
    # Single bloops at different pitches
    for i, freq in enumerate([400, 500, 600, 700, 800, 900, 1000]):
        name = f"bloop_{freq}hz"
        write_wav(start_dir / f"{i+1:02d}_{name}.wav", gen_bloop_variation(freq, 0.3, 12))
        variations.append(name)
    
    # Chirps
    for i, (freq, sweep) in enumerate([(800, 400), (1000, 500), (1200, 600), (1400, 500), (1600, 400)]):
        name = f"chirp_{freq}hz"
        write_wav(start_dir / f"{i+8:02d}_{name}.wav", gen_chirp_variation(freq, sweep, 30))
        variations.append(name)
    
    # Double bloops
    pairs = [(500, 700), (600, 800), (700, 900), (400, 600), (500, 800), (600, 900), (800, 1000)]
    for i, (f1, f2) in enumerate(pairs):
        name = f"double_{f1}_{f2}"
        write_wav(start_dir / f"{i+13:02d}_{name}.wav", gen_double_bloop(f1, f2, 0.08))
        variations.append(name)
    
    # Chirpy doubles
    for i, (f1, f2) in enumerate([(1000, 1300), (1100, 1400), (1200, 1500), (900, 1200)]):
        name = f"chirpy_{f1}_{f2}"
        write_wav(start_dir / f"{i+20:02d}_{name}.wav", gen_double_bloop(f1, f2, 0.06, vol=0.45))
        variations.append(name)
    
    # Chimes
    for i, freq in enumerate([440, 523, 587, 659, 784]):
        name = f"chime_{freq}hz"
        write_wav(start_dir / f"{i+24:02d}_{name}.wav", gen_chime(freq))
        variations.append(name)
    
    print(f"  ✓ Created {len(variations)} start variations")
    
    print("\nGenerating SUCCESS sound variations...")
    success_dir = var_dir / 'success'
    success_dir.mkdir(exist_ok=True)
    
    success_vars = []
    
    # Different major triads
    triads = [
        ([523, 659, 784], "C_maj"),      # C-E-G
        ([587, 740, 880], "D_maj"),      # D-F#-A
        ([659, 831, 988], "E_maj"),      # E-G#-B
        ([698, 880, 1047], "F_maj"),     # F-A-C
        ([784, 988, 1175], "G_maj"),     # G-B-D
        ([440, 554, 659], "A_maj"),      # A-C#-E
        ([494, 622, 740], "B_maj"),      # B-D#-F#
    ]
    
    for i, (freqs, name) in enumerate(triads):
        write_wav(success_dir / f"{i+1:02d}_triad_{name}.wav", gen_triple_ascend(freqs))
        success_vars.append(name)
    
    # Pentatonic sequences
    penta = [
        ([523, 587, 659], "penta_1"),
        ([659, 784, 880], "penta_2"),
        ([784, 880, 1047], "penta_3"),
        ([440, 523, 659], "penta_4"),
        ([587, 659, 784], "penta_5"),
    ]
    
    for i, (freqs, name) in enumerate(penta):
        write_wav(success_dir / f"{i+8:02d}_{name}.wav", gen_triple_ascend(freqs, vol=0.45))
        success_vars.append(name)
    
    # Octave jumps
    for i, base in enumerate([262, 330, 392, 440, 523]):
        name = f"octave_{base}"
        write_wav(success_dir / f"{i+13:02d}_{name}.wav", gen_triple_ascend([base, base*1.5, base*2], vol=0.4))
        success_vars.append(name)
    
    print(f"  ✓ Created {len(success_vars)} success variations")
    
    # Copy chirpy as new default
    print("\nSetting 'chirpy' as default...")
    import shutil
    for wav in chirpy_dir.glob('*.wav'):
        shutil.copy2(wav, base_dir / wav.name)
    
    print(f"""
✨ Done! 

THEMES: cute, warm, zen, chirpy, classic

VARIATIONS TO AUDITION:
  sounds/variations/start/   - {len(variations)} options
  sounds/variations/success/ - {len(success_vars)} options

Play them in Windows Explorer or any audio player to find your favorites!
Then copy your picks to sounds/ folder (or sounds/themes/custom/).
""")

if __name__ == '__main__':
    main()
