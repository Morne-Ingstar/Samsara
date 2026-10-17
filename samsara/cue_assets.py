"""Cue file construction and cache loading behind DictationApp's facade.

The host owns sound paths, configuration and the cache sample rate. Its facade
keeps all output-device routing, stream, lock and watcher startup.
"""

import logging
import wave

import numpy as np

logger = logging.getLogger("Samsara")


def _setup_sounds(self, app_root):
    """Set up sound files - create defaults if needed"""
    self.sounds_dir = app_root / 'sounds'
    self.sounds_dir.mkdir(exist_ok=True)

    # Sound file names
    self.sound_files = {
        'start': self.sounds_dir / 'start.wav',
        'stop': self.sounds_dir / 'stop.wav',
        'success': self.sounds_dir / 'success.wav',
        'error': self.sounds_dir / 'error.wav'
    }

    # Generate default sounds if they don't exist
    sample_rate = 44100

    def generate_tone(frequency, duration, volume=0.5):
        """Generate a sine wave tone"""
        n_samples = int(sample_rate * duration)
        t = np.linspace(0, duration, n_samples, False)
        tone = np.sin(2 * np.pi * frequency * t) * volume

        # Fade in/out to prevent clicks
        fade_samples = min(int(sample_rate * 0.01), n_samples // 4)
        if fade_samples > 0:
            tone[:fade_samples] *= np.linspace(0, 1, fade_samples)
            tone[-fade_samples:] *= np.linspace(1, 0, fade_samples)

        return tone

    def save_wav(filepath, audio_data):
        """Save audio data as WAV file"""
        with wave.open(str(filepath), 'w') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            # Convert float to 16-bit int
            audio_int = (audio_data * 32767).astype(np.int16)
            wav_file.writeframes(audio_int.tobytes())

    # Create default sounds if they don't exist
    if not self.sound_files['start'].exists():
        # Rising tone
        tone = generate_tone(660, 0.12, volume=0.6)
        save_wav(self.sound_files['start'], tone)

    if not self.sound_files['stop'].exists():
        # Falling tone
        tone = generate_tone(440, 0.1, volume=0.5)
        save_wav(self.sound_files['stop'], tone)

    if not self.sound_files['success'].exists():
        # Happy arpeggio
        t1 = generate_tone(523, 0.08, volume=0.5)
        gap = np.zeros(int(sample_rate * 0.02))
        t2 = generate_tone(659, 0.08, volume=0.5)
        t3 = generate_tone(784, 0.12, volume=0.5)
        audio = np.concatenate([t1, gap, t2, gap, t3])
        save_wav(self.sound_files['success'], audio)

    if not self.sound_files['error'].exists():
        # Low double beep
        t1 = generate_tone(220, 0.15, volume=0.5)
        gap = np.zeros(int(sample_rate * 0.08))
        t2 = generate_tone(196, 0.18, volume=0.5)
        audio = np.concatenate([t1, gap, t2])
        save_wav(self.sound_files['error'], audio)


def _load_sound_cache(self):
    """Pre-load all sound files into memory, normalized to common sample rate.

    Supports WAV natively, and MP3/OGG/FLAC if pydub is installed.

    Loads in two passes:
      1. Legacy hard-coded names (start/stop/success/error) from
         self.sound_files -- backed by sounds/<name>.* so user "Browse..."
         customisations still win.
      2. Auto-discover any other .wav files in the active theme directory
         (sounds/themes/<sound_theme>/) by file-stem. This is how the
         Phase-2 earcon vocabulary (capture_started, capture_saved,
         agent_routing, etc.) is loaded -- no hard-coded list needed.
    """
    self._sound_cache = {}
    target_sr = self._sound_stream_sr

    # Check for pydub support (enables MP3, OGG, FLAC, etc.)
    try:
        from pydub import AudioSegment
        HAS_PYDUB = True
    except ImportError:
        HAS_PYDUB = False

    for sound_type, sound_file in self.sound_files.items():
        # Look for sound file with various extensions
        sound_path = None
        base_path = sound_file.with_suffix('')
        for ext in ['.wav', '.mp3', '.ogg', '.flac', '.m4a']:
            test_path = base_path.with_suffix(ext)
            if test_path.exists():
                sound_path = test_path
                break

        # Also check the original path as-is
        if sound_path is None and sound_file.exists():
            sound_path = sound_file

        if sound_path is None:
            continue

        try:
            suffix = sound_path.suffix.lower()

            # Use pydub for non-WAV formats
            if suffix != '.wav' and HAS_PYDUB:
                audio_seg = AudioSegment.from_file(str(sound_path))
                # Convert to mono, target sample rate
                audio_seg = audio_seg.set_channels(1).set_frame_rate(target_sr)
                # Get raw samples as numpy array
                samples = np.array(audio_seg.get_array_of_samples()).astype(np.float32)
                # Normalize to -1 to 1
                samples = samples / (2 ** (audio_seg.sample_width * 8 - 1))
                audio_array = samples.reshape(-1, 1)
                self._sound_cache[sound_type] = audio_array
                continue
            elif suffix != '.wav':
                # Non-WAV without pydub - skip
                logger.info(f"[AUDIO] Skipping {sound_path.name} - install pydub for MP3/OGG support")
                continue

            # Native WAV loading
            with wave.open(str(sound_path), 'rb') as wf:
                sample_rate = wf.getframerate()
                n_channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                audio_data = wf.readframes(wf.getnframes())

            if sample_width == 1:
                dtype = np.uint8
            elif sample_width == 2:
                dtype = np.int16
            else:
                dtype = np.int32

            audio_array = np.frombuffer(audio_data, dtype=dtype).astype(np.float32)

            if sample_width == 1:
                audio_array = (audio_array - 128) / 128.0
            else:
                audio_array = audio_array / (2 ** (sample_width * 8 - 1))

            # Mix stereo to mono
            if n_channels == 2:
                audio_array = audio_array.reshape(-1, 2).mean(axis=1)

            # Resample to target rate if needed
            if sample_rate != target_sr:
                duration = len(audio_array) / sample_rate
                new_length = int(duration * target_sr)
                indices = np.linspace(0, len(audio_array) - 1, new_length)
                audio_array = np.interp(indices, np.arange(len(audio_array)), audio_array)

            # Ensure mono float32 column vector for stream write
            audio_array = audio_array.astype(np.float32).reshape(-1, 1)

            self._sound_cache[sound_type] = audio_array
        except Exception as e:
            logger.exception(f"[AUDIO] Failed to load {sound_path}: {e}")

    # Pass 2: auto-discover extended earcons in the active theme dir.
    # Anything not already in the cache (legacy 4 win) gets loaded by
    # file-stem so new earcons drop in without code changes.
    try:
        theme_name = self.config.get('sound_theme', 'cute') if hasattr(self, 'config') else 'cute'
        themes_root = self.sounds_dir / 'themes' / theme_name
        if themes_root.is_dir():
            for wav_path in sorted(themes_root.glob('*.wav')):
                name = wav_path.stem
                if name in self._sound_cache:
                    continue  # legacy 4 or already loaded
                try:
                    with wave.open(str(wav_path), 'rb') as wf:
                        sample_rate = wf.getframerate()
                        n_channels = wf.getnchannels()
                        sample_width = wf.getsampwidth()
                        audio_data = wf.readframes(wf.getnframes())

                    if sample_width == 1:
                        dtype = np.uint8
                    elif sample_width == 2:
                        dtype = np.int16
                    else:
                        dtype = np.int32

                    audio_array = np.frombuffer(audio_data, dtype=dtype).astype(np.float32)
                    if sample_width == 1:
                        audio_array = (audio_array - 128) / 128.0
                    else:
                        audio_array = audio_array / (2 ** (sample_width * 8 - 1))
                    if n_channels == 2:
                        audio_array = audio_array.reshape(-1, 2).mean(axis=1)
                    if sample_rate != target_sr:
                        duration = len(audio_array) / sample_rate
                        new_length = int(duration * target_sr)
                        indices = np.linspace(0, len(audio_array) - 1, new_length)
                        audio_array = np.interp(indices, np.arange(len(audio_array)), audio_array)
                    audio_array = audio_array.astype(np.float32).reshape(-1, 1)
                    self._sound_cache[name] = audio_array
                except Exception as e:
                    logger.exception(f"[AUDIO] Failed to load extended earcon {wav_path.name}: {e}")
    except Exception as e:
        logger.exception(f"[AUDIO] Extended-earcon discovery failed: {e}")
