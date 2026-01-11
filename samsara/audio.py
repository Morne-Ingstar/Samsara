"""
Samsara Audio Module

Handles audio capture and playback functionality.
"""

import threading
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

# Optional dependency - may not be available in test environments
try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    sd = None
    HAS_SOUNDDEVICE = False

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False


class AudioCapture:
    """Handles microphone audio capture with various modes."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        dtype: Any = np.float32,
        device: Optional[int] = None,
    ):
        """
        Initialize audio capture.

        Args:
            sample_rate: Audio sample rate (default 16000 for Whisper)
            channels: Number of audio channels
            dtype: NumPy data type for audio
            device: Audio device ID, None for default
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.dtype = dtype
        self.device = device

        self._stream: Optional[Any] = None  # sd.InputStream when available
        self._recording = False
        self._audio_data: List[np.ndarray] = []
        self._callback: Optional[Callable] = None

    @property
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._recording

    def set_device(self, device_id: Optional[int]) -> None:
        """Set the audio input device."""
        self.device = device_id

    def start(self, callback: Optional[Callable] = None, blocksize: int = 0) -> None:
        """
        Start recording audio.

        Args:
            callback: Optional callback for streaming audio data
            blocksize: Block size for streaming (0 for default)
        """
        if self._recording:
            return

        if not HAS_SOUNDDEVICE:
            raise RuntimeError("sounddevice is not available")

        self._audio_data = []
        self._callback = callback

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=self.dtype,
            callback=self._audio_callback if callback else self._buffer_callback,
            device=self.device,
            blocksize=blocksize,
        )
        self._stream.start()
        self._recording = True

    def stop(self) -> Optional[np.ndarray]:
        """
        Stop recording and return captured audio.

        Returns:
            NumPy array of captured audio, or None if no data
        """
        if not self._recording:
            return None

        self._recording = False

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if self._audio_data:
            audio = np.concatenate(self._audio_data, axis=0).flatten()
            self._audio_data = []
            return audio

        return None

    def _buffer_callback(self, indata, frames, time_info, status):
        """Internal callback that buffers audio data."""
        if self._recording:
            self._audio_data.append(indata.copy())

    def _audio_callback(self, indata, frames, time_info, status):
        """Internal callback that passes data to external callback."""
        if self._recording and self._callback:
            self._callback(indata.copy(), frames, time_info, status)

    @staticmethod
    def get_devices(show_all: bool = False) -> List[Dict[str, Any]]:
        """
        Get list of available audio input devices.

        Args:
            show_all: Include virtual/system devices if True

        Returns:
            List of device info dictionaries
        """
        if not HAS_SOUNDDEVICE:
            return []

        devices = sd.query_devices()
        microphones = []
        seen_names = set()

        skip_keywords = [
            'Stereo Mix', 'Wave Out Mix', 'What U Hear', 'Loopback',
            'CABLE', 'Virtual Audio', 'VB-Audio', 'Voicemeeter',
            'Sound Mapper', 'Primary Sound', 'Wave Speaker', 'Wave Microphone',
            'Stream Wave', 'Chat Capture', 'Hands-Free', 'HF Audio', 'Input ()',
            'Line In (', 'VDVAD', 'SteelSeries Sonar', 'OCULUSVAD',
            'VAD Wave', 'wc4400_8200', 'Microsoft Sound Mapper',
        ]

        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                name = device['name']

                if name in seen_names:
                    continue

                if not show_all:
                    if any(kw.lower() in name.lower() for kw in skip_keywords):
                        continue
                    if name.strip() == "Microphone ()":
                        continue
                    if '@System32\\drivers\\' in name:
                        continue

                seen_names.add(name)
                microphones.append({
                    'id': i,
                    'name': name,
                    'channels': device['max_input_channels'],
                })

        return microphones

    @staticmethod
    def test_device(device_id: int, duration: float = 2.0) -> float:
        """
        Test a microphone device and return RMS level.

        Args:
            device_id: Device ID to test
            duration: Test duration in seconds

        Returns:
            RMS audio level (higher = more sound detected)
        """
        if not HAS_SOUNDDEVICE:
            return 0.0

        audio = sd.rec(
            int(16000 * duration),
            samplerate=16000,
            channels=1,
            dtype=np.float32,
            device=device_id,
        )
        sd.wait()
        return float(np.sqrt(np.mean(audio ** 2)))


class AudioPlayer:
    """Handles audio feedback sound playback."""

    DEFAULT_SAMPLE_RATE = 44100

    def __init__(
        self,
        sounds_dir: Optional[Path] = None,
        volume: float = 0.5,
        enabled: bool = True,
    ):
        """
        Initialize audio player.

        Args:
            sounds_dir: Directory for sound files
            volume: Playback volume (0.0 to 1.0)
            enabled: Whether audio feedback is enabled
        """
        if sounds_dir is None:
            sounds_dir = Path(__file__).parent.parent / 'sounds'
        self.sounds_dir = Path(sounds_dir)
        self.sounds_dir.mkdir(exist_ok=True)

        self.volume = max(0.0, min(1.0, volume))
        self.enabled = enabled

        self.sound_files = {
            'start': self.sounds_dir / 'start.wav',
            'stop': self.sounds_dir / 'stop.wav',
            'success': self.sounds_dir / 'success.wav',
            'error': self.sounds_dir / 'error.wav',
        }

        self._ensure_default_sounds()

    def set_volume(self, volume: float) -> None:
        """Set playback volume (0.0 to 1.0)."""
        self.volume = max(0.0, min(1.0, volume))

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable audio feedback."""
        self.enabled = enabled

    def play(self, sound_type: str) -> None:
        """
        Play an audio feedback sound.

        Args:
            sound_type: Type of sound ('start', 'stop', 'success', 'error')
        """
        if not self.enabled:
            return

        sound_file = self.sound_files.get(sound_type)
        if sound_file is None or not sound_file.exists():
            return

        threading.Thread(
            target=self._play_file,
            args=(sound_file,),
            daemon=True,
        ).start()

    def _play_file(self, filepath: Path) -> None:
        """Play a WAV file with volume control."""
        try:
            with wave.open(str(filepath), 'rb') as wf:
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

            audio_array = audio_array * self.volume

            if n_channels == 2:
                audio_array = audio_array.reshape(-1, 2)

            if HAS_SOUNDDEVICE:
                sd.play(audio_array, sample_rate)
                return

        except Exception:
            pass

        # Fallback to winsound if sounddevice failed or unavailable
        if HAS_WINSOUND:
            try:
                winsound.PlaySound(
                    str(filepath),
                    winsound.SND_FILENAME | winsound.SND_ASYNC,
                )
            except Exception:
                pass

    def _ensure_default_sounds(self) -> None:
        """Generate default sounds if they don't exist."""
        if not self.sound_files['start'].exists():
            self._generate_tone(self.sound_files['start'], 660, 0.12, 0.6)

        if not self.sound_files['stop'].exists():
            self._generate_tone(self.sound_files['stop'], 440, 0.1, 0.5)

        if not self.sound_files['success'].exists():
            self._generate_arpeggio(
                self.sound_files['success'],
                [(523, 0.08), (659, 0.08), (784, 0.12)],
                0.02,
                0.5,
            )

        if not self.sound_files['error'].exists():
            self._generate_arpeggio(
                self.sound_files['error'],
                [(220, 0.15), (196, 0.18)],
                0.08,
                0.5,
            )

    def _generate_tone(
        self,
        filepath: Path,
        frequency: float,
        duration: float,
        volume: float = 0.5,
    ) -> None:
        """Generate a sine wave tone and save as WAV."""
        n_samples = int(self.DEFAULT_SAMPLE_RATE * duration)
        t = np.linspace(0, duration, n_samples, False)
        tone = np.sin(2 * np.pi * frequency * t) * volume

        fade_samples = min(int(self.DEFAULT_SAMPLE_RATE * 0.01), n_samples // 4)
        if fade_samples > 0:
            tone[:fade_samples] *= np.linspace(0, 1, fade_samples)
            tone[-fade_samples:] *= np.linspace(1, 0, fade_samples)

        self._save_wav(filepath, tone)

    def _generate_arpeggio(
        self,
        filepath: Path,
        notes: List[tuple],
        gap_duration: float,
        volume: float = 0.5,
    ) -> None:
        """Generate an arpeggio (sequence of tones) and save as WAV."""
        parts = []
        gap = np.zeros(int(self.DEFAULT_SAMPLE_RATE * gap_duration))

        for i, (freq, dur) in enumerate(notes):
            n_samples = int(self.DEFAULT_SAMPLE_RATE * dur)
            t = np.linspace(0, dur, n_samples, False)
            tone = np.sin(2 * np.pi * freq * t) * volume

            fade_samples = min(int(self.DEFAULT_SAMPLE_RATE * 0.01), n_samples // 4)
            if fade_samples > 0:
                tone[:fade_samples] *= np.linspace(0, 1, fade_samples)
                tone[-fade_samples:] *= np.linspace(1, 0, fade_samples)

            parts.append(tone)
            if i < len(notes) - 1:
                parts.append(gap)

        audio = np.concatenate(parts)
        self._save_wav(filepath, audio)

    def _save_wav(self, filepath: Path, audio_data: np.ndarray) -> None:
        """Save audio data as WAV file."""
        with wave.open(str(filepath), 'w') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.DEFAULT_SAMPLE_RATE)
            audio_int = (audio_data * 32767).astype(np.int16)
            wav_file.writeframes(audio_int.tobytes())

    def set_custom_sound(self, sound_type: str, filepath: Path) -> bool:
        """
        Set a custom sound file for a sound type.

        Args:
            sound_type: Type of sound to replace
            filepath: Path to custom WAV file

        Returns:
            True if successful
        """
        if sound_type not in self.sound_files:
            return False

        import shutil
        try:
            shutil.copy(filepath, self.sound_files[sound_type])
            return True
        except Exception:
            return False

    def reset_sound(self, sound_type: str) -> bool:
        """
        Reset a sound to its default.

        Args:
            sound_type: Type of sound to reset

        Returns:
            True if successful
        """
        if sound_type not in self.sound_files:
            return False

        filepath = self.sound_files[sound_type]
        if filepath.exists():
            filepath.unlink()

        self._ensure_default_sounds()
        return True
