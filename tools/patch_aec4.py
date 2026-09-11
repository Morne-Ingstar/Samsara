path = r'C:\Users\Morne\Projects\Samsara-dev\samsara\echo_cancel.py'
with open(path, 'r') as f:
    t = f.read()

# Add divergence detection after the AEC process call
old = """        # Apply adaptive filter at 16kHz
        cleaned_16k = self._aec.process(mic_16k, ref)"""

new = """        # Apply adaptive filter at 16kHz
        cleaned_16k = self._aec.process(mic_16k, ref)

        # Divergence safety: if filter is amplifying, reset weights
        cleaned_energy = float(np.mean(cleaned_16k ** 2))
        mic_energy = float(np.mean(mic_16k ** 2))
        if cleaned_energy > mic_energy * 2.0 and mic_energy > 1e-10:
            print("[AEC] DIVERGENCE detected — resetting filter weights")
            self._aec.reset()
            cleaned_16k = mic_16k  # pass through unfiltered this block"""

if old in t:
    t = t.replace(old, new)
    with open(path, 'w') as f:
        f.write(t)
    print("Added divergence safety check")
else:
    print("Pattern not found")
