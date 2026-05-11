"""WinRTEngine: TTS via Windows Runtime SpeechSynthesis.

Uses winsdk.windows.media.speechsynthesis.SpeechSynthesizer to render
text to a WAV byte stream, then routes the PCM through a sounddevice
OutputStream so Samsara owns the audio path (enables future ducking and
AudioCoordinator integration).

Phase 1a scope:
  - queue_mode: "append" only; other modes degrade to append (logged)
  - category: stored but not yet used for priority arbitration
  - set_volume fade_ms: instant change (5 ms interpolation is Phase 1b)
  - speak() spawns an ephemeral OutputStream per utterance; a persistent
    TTS stream owned by AudioCoordinator comes in Phase 1b
"""

import logging
import threading
import uuid
from typing import Callable, List, Optional

import numpy as np

from .audio_utils import parse_wav, resample_pcm
from .engine_base import SpeechHandle, TTSEngine, VoiceInfo
from .exceptions import EngineUnavailableError, RenderError
from .winrt_helper import get_helper

logger = logging.getLogger(__name__)

# Earcon stream runs at 44100 Hz (confirmed in dictation.py _sound_stream_sr).
# All TTS audio is normalized to this rate before playback so the same
# sounddevice output format assumption holds everywhere.
_TARGET_SR = 44100
_CHUNK_FRAMES = 4096


def _import_winsdk():
    """Import WinRT speech classes, raising EngineUnavailableError on failure."""
    try:
        from winsdk.windows.media.speechsynthesis import (
            SpeechSynthesizer,
            VoiceGender,
        )
        from winsdk.windows.storage.streams import DataReader, InputStreamOptions
        return SpeechSynthesizer, VoiceGender, DataReader, InputStreamOptions
    except ImportError as exc:
        raise EngineUnavailableError(
            "WinRT speech requires the winsdk package. "
            "Install with: pip install winsdk"
        ) from exc


async def _read_stream_bytes(stream) -> bytes:
    """Read all bytes from a WinRT SpeechSynthesisStream asynchronously."""
    from winsdk.windows.storage.streams import DataReader, InputStreamOptions

    size = int(stream.size)
    reader = DataReader(stream)
    reader.input_stream_options = InputStreamOptions.READ_AHEAD
    loaded = await reader.load_async(size)
    buf = reader.read_buffer(loaded)
    return bytes(buf)


class _Utterance:
    """Internal state for a single in-flight speak() call."""

    def __init__(self, handle: SpeechHandle, on_done: Optional[Callable]):
        self.handle = handle
        self.on_done = on_done
        self.cancelled = threading.Event()
        self.volume = 1.0        # modified by set_volume()
        self._volume_lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None

    def get_volume(self) -> float:
        with self._volume_lock:
            return self.volume

    def set_volume(self, v: float):
        with self._volume_lock:
            self.volume = max(0.0, min(1.0, v))


class WinRTEngine(TTSEngine):
    """TTS engine backed by Windows Runtime SpeechSynthesis.

    Raises EngineUnavailableError at construction if winsdk is not installed.
    """

    def __init__(self):
        SpeechSynthesizer, VoiceGender, _, _ = _import_winsdk()
        self._SpeechSynthesizer = SpeechSynthesizer
        self._VoiceGender = VoiceGender

        self._helper = get_helper()

        # Build voice list from the static class-level all_voices property.
        self._voices: List[VoiceInfo] = self._build_voice_list()

        # Track active utterances for cancel / is_speaking.
        self._active: dict[str, _Utterance] = {}
        self._active_lock = threading.Lock()

    # ------------------------------------------------------------------
    # TTSEngine interface
    # ------------------------------------------------------------------

    def speak(
        self,
        text: str,
        *,
        voice_id: Optional[str] = None,
        speed: float = 1.0,
        pitch: float = 1.0,
        volume: float = 1.0,
        category: str = "general",
        queue_mode: str = "append",
        on_done: Optional[Callable] = None,
    ) -> SpeechHandle:
        """Synthesize text and play it asynchronously.

        Returns a SpeechHandle immediately. Playback happens on a daemon
        thread so the caller is never blocked.

        Phase 1a: queue_mode values other than "append" degrade silently to
        "append". category is accepted and stored but not acted on.
        """
        if queue_mode != "append":
            logger.debug(
                "queue_mode=%r not yet implemented; treating as 'append'", queue_mode
            )

        uid = str(uuid.uuid4())
        handle = SpeechHandle(utterance_id=uid, _state="pending")
        utterance = _Utterance(handle, on_done)
        utterance.set_volume(volume)

        with self._active_lock:
            self._active[uid] = utterance

        t = threading.Thread(
            target=self._playback_worker,
            args=(utterance, text, voice_id, speed, pitch, volume),
            daemon=True,
            name=f"tts-{uid[:8]}",
        )
        utterance.thread = t
        handle._state = "playing"
        t.start()
        return handle

    def cancel(self, handle: SpeechHandle) -> None:
        """Signal a specific utterance to stop at the next chunk boundary."""
        with self._active_lock:
            utterance = self._active.get(handle.utterance_id)
        if utterance:
            utterance.cancelled.set()
            handle._state = "cancelled"

    def cancel_all(self) -> None:
        """Signal all active utterances to stop."""
        with self._active_lock:
            utterances = list(self._active.values())
        for u in utterances:
            u.cancelled.set()
            u.handle._state = "cancelled"

    def set_volume(
        self, handle: SpeechHandle, volume: float, fade_ms: int = 5
    ) -> None:
        """Adjust the playback volume for a live utterance.

        Phase 1a: fade_ms is ignored; change is instantaneous.
        """
        with self._active_lock:
            utterance = self._active.get(handle.utterance_id)
        if utterance:
            utterance.set_volume(volume)

    def is_speaking(self) -> bool:
        """True if any playback thread is currently active."""
        with self._active_lock:
            return bool(self._active)

    def list_voices(self) -> List[VoiceInfo]:
        """Return the cached list of installed OneCore voices."""
        return list(self._voices)

    def shutdown(self) -> None:
        """Cancel all utterances and join playback threads."""
        self.cancel_all()
        with self._active_lock:
            threads = [u.thread for u in self._active.values() if u.thread]
        for t in threads:
            t.join(timeout=1.0)
        self._helper.shutdown()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_voice_list(self) -> List[VoiceInfo]:
        voices = []
        for v in self._SpeechSynthesizer.all_voices:
            gender_val = getattr(v, "gender", None)
            from winsdk.windows.media.speechsynthesis import VoiceGender as VG
            if gender_val == VG.MALE:
                gender = "male"
            elif gender_val == VG.FEMALE:
                gender = "female"
            else:
                gender = "unknown"
            voices.append(
                VoiceInfo(
                    voice_id=v.id,
                    display_name=v.display_name,
                    language=v.language,
                    gender=gender,
                )
            )
        return voices

    def _synthesize(self, text: str, voice_id: Optional[str], speed: float, pitch: float) -> bytes:
        """Synthesize text → raw WAV bytes via WinRT (runs on calling thread)."""
        synth = self._SpeechSynthesizer()

        # Set voice if specified; otherwise use the OS default.
        if voice_id:
            for v in self._SpeechSynthesizer.all_voices:
                if v.id == voice_id:
                    synth.voice = v
                    break
            else:
                logger.warning("Voice id %r not found; using default", voice_id)

        # Apply speed/pitch via WinRT options BEFORE synthesis (WinRT constraint).
        opts = synth.options
        opts.speaking_rate = max(0.5, min(6.0, speed))
        opts.audio_pitch = max(0.0, min(2.0, pitch))

        # Run both async WinRT calls in one coroutine so they share the event-loop
        # turn. Calling run_sync twice with nested WinRT objects causes the second
        # call to receive an IAsyncOperation that can't be submitted a second time.
        async def _do_synthesis():
            stream = await synth.synthesize_text_to_stream_async(text)
            return await _read_stream_bytes(stream)

        raw_bytes = self._helper.run_sync(_do_synthesis())
        synth.close()
        return raw_bytes

    def _playback_worker(
        self,
        utterance: _Utterance,
        text: str,
        voice_id: Optional[str],
        speed: float,
        pitch: float,
        volume: float,
    ):
        """Daemon thread: synthesize + stream PCM to sounddevice."""
        uid = utterance.handle.utterance_id
        try:
            # --- Synthesis ---
            raw_bytes = self._synthesize(text, voice_id, speed, pitch)
            if utterance.cancelled.is_set():
                return

            # --- Decode WAV ---
            pcm, sr, _ = parse_wav(raw_bytes)
            pcm = resample_pcm(pcm, sr, _TARGET_SR)

            # Apply initial volume (further changes via set_volume() during playback)
            pcm = pcm * utterance.get_volume()

            # --- Stream to sounddevice ---
            self._stream_pcm(pcm, utterance)

        except Exception as exc:
            logger.exception("TTS playback failed for utterance %s: %s", uid, exc)
            utterance.handle._state = "failed"
        finally:
            with self._active_lock:
                self._active.pop(uid, None)
            utterance.handle._state = utterance.handle._state if utterance.handle._state in ("cancelled", "failed") else "done"
            if utterance.on_done and not utterance.cancelled.is_set():
                try:
                    utterance.on_done()
                except Exception:
                    logger.exception("on_done callback raised for utterance %s", uid)

    def _stream_pcm(self, pcm: np.ndarray, utterance: _Utterance):
        """Write PCM chunks to a sounddevice OutputStream.

        Opens an ephemeral stream per utterance (Phase 1a). A persistent
        TTS stream shared across utterances comes in Phase 1b with the
        AudioCoordinator.
        """
        import sounddevice as sd

        try:
            stream = sd.OutputStream(
                samplerate=_TARGET_SR,
                channels=1,
                dtype="float32",
                blocksize=_CHUNK_FRAMES,
            )
        except sd.PortAudioError as exc:
            logger.error(
                "TTS stream unavailable: another process may have exclusive "
                "audio access. (%s)", exc
            )
            utterance.handle._state = "failed"
            return

        with stream:
            stream.start()
            offset = 0
            total = len(pcm)
            while offset < total:
                if utterance.cancelled.is_set():
                    break
                end = min(offset + _CHUNK_FRAMES, total)
                chunk = pcm[offset:end].reshape(-1, 1)
                # Re-apply live volume so set_volume() during playback works.
                chunk = chunk * utterance.get_volume()
                stream.write(chunk)
                offset = end
