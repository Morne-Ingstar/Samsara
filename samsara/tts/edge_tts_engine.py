"""
Edge TTS engine for Samsara — uses Microsoft Edge's Read Aloud API
via the edge-tts Python library.

Provides the same interface as WinRTEngine so Samsara can swap engines
transparently. Requires an active internet connection to synthesize.

Voice IDs are Edge voice short names, e.g.:
    en-US-AvaNeural
    en-US-JennyNeural
    en-US-GuyNeural
    en-US-AriaNeural

Latency: ~200-600ms per utterance (network round-trip to Azure).
Quality: Azure Neural TTS — same voices as Azure Cognitive Services.
"""

import asyncio
import io
import logging
import threading
import uuid
from typing import Callable, List, Optional

from .engine_base import SpeechHandle, TTSEngine, VoiceInfo
from .exceptions import EngineUnavailableError, RenderError

logger = logging.getLogger(__name__)

_DEFAULT_VOICE = "en-US-AvaNeural"

# Curated English voice list — full list available via edge_tts.list_voices()
_BUILTIN_VOICES = [
    VoiceInfo("en-US-AvaNeural",    "Ava (Natural HD, en-US)",    "en-US", "female"),
    VoiceInfo("en-US-JennyNeural",  "Jenny (Natural HD, en-US)",  "en-US", "female"),
    VoiceInfo("en-US-AriaNeural",   "Aria (Natural HD, en-US)",   "en-US", "female"),
    VoiceInfo("en-US-GuyNeural",    "Guy (Natural HD, en-US)",    "en-US", "male"),
    VoiceInfo("en-US-EricNeural",   "Eric (Natural HD, en-US)",   "en-US", "male"),
    VoiceInfo("en-GB-SoniaNeural",  "Sonia (Natural HD, en-GB)",  "en-GB", "female"),
    VoiceInfo("en-GB-RyanNeural",   "Ryan (Natural HD, en-GB)",   "en-GB", "male"),
    VoiceInfo("en-AU-NatashaNeural","Natasha (Natural HD, en-AU)","en-AU", "female"),
]


def _import_edge_tts():
    try:
        import edge_tts
        return edge_tts
    except ImportError as exc:
        raise EngineUnavailableError(
            "edge-tts is not installed. Run: pip install edge-tts"
        ) from exc


def _synthesize_mp3(voice_id: str, text: str, rate: str, volume: str) -> bytes:
    """Synthesize text via edge-tts, return raw MP3 bytes."""
    import edge_tts

    async def _do():
        communicate = edge_tts.Communicate(text, voice_id, rate=rate, volume=volume)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    return asyncio.run(_do())


def _mp3_to_pcm(mp3_bytes: bytes, target_sr: int = 44100) -> bytes:
    """Convert MP3 bytes to 16-bit mono PCM at target_sr using pydub."""
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
        seg = seg.set_frame_rate(target_sr).set_channels(1).set_sample_width(2)
        return seg.raw_data
    except ImportError:
        raise RenderError(
            "pydub is required to decode edge-tts audio. "
            "Install with: pip install pydub\n"
            "pydub also requires ffmpeg on PATH for MP3 decoding."
        )
    except Exception as exc:
        raise RenderError(f"MP3 decode failed: {exc}") from exc


class EdgeTTSEngine(TTSEngine):
    """TTS engine backed by Microsoft Edge's Read Aloud API (edge-tts).

    Requires internet connection. Voice quality is Azure Neural TTS.
    Drop-in replacement for WinRTEngine.
    """

    def __init__(self):
        _import_edge_tts()
        self._voices = list(_BUILTIN_VOICES)
        self._lock = threading.Lock()
        self._active_handle: Optional[SpeechHandle] = None
        self._state = "idle"
        self._cancelled = False
        logger.info("[TTS] EdgeTTSEngine initialized (Azure Neural voices via edge-tts)")

    # ------------------------------------------------------------------
    # TTSEngine interface
    # ------------------------------------------------------------------

    def list_voices(self) -> List[VoiceInfo]:
        return list(self._voices)

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
        handle = SpeechHandle(utterance_id=str(uuid.uuid4()))

        with self._lock:
            self._cancelled = False
            self._active_handle = handle

        voice = voice_id or _DEFAULT_VOICE

        # edge-tts rate/volume as relative percentage strings
        rate_pct = int((speed - 1.0) * 100)
        rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
        vol_pct = int((volume - 1.0) * 100)
        vol_str = f"+{vol_pct}%" if vol_pct >= 0 else f"{vol_pct}%"

        def _worker():
            try:
                with self._lock:
                    self._state = "synthesizing"

                logger.debug("[EdgeTTS] synthesizing: %r (voice=%s)", text[:50], voice)
                mp3_bytes = _synthesize_mp3(voice, text, rate_str, vol_str)

                with self._lock:
                    if self._cancelled:
                        return
                    self._state = "playing"

                pcm = _mp3_to_pcm(mp3_bytes)

                with self._lock:
                    if self._cancelled:
                        return

                self._play_pcm(pcm)

            except EngineUnavailableError:
                raise
            except Exception as exc:
                logger.error("[EdgeTTS] error: %s", exc)
            finally:
                with self._lock:
                    self._state = "idle"
                    self._active_handle = None
                handle._state = "done"
                if on_done:
                    try:
                        on_done()
                    except Exception:
                        pass

        t = threading.Thread(target=_worker, daemon=True, name="EdgeTTS-worker")
        t.start()
        return handle

    def cancel(self, handle: SpeechHandle) -> None:
        with self._lock:
            if self._active_handle and self._active_handle.utterance_id == handle.utterance_id:
                self._cancelled = True
                self._state = "cancelling"

    def cancel_all(self) -> None:
        with self._lock:
            self._cancelled = True
            self._state = "cancelling"

    def set_volume(self, handle: SpeechHandle, volume: float, fade_ms: int = 5) -> None:
        # Phase 1: no per-utterance volume adjustment after synthesis starts
        pass

    def is_speaking(self) -> bool:
        with self._lock:
            return self._state in ("synthesizing", "playing")

    def get_engine_state(self) -> str:
        with self._lock:
            return self._state

    def shutdown(self) -> None:
        self.cancel_all()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _play_pcm(self, pcm_bytes: bytes, sample_rate: int = 44100) -> None:
        """Play raw 16-bit mono PCM through sounddevice."""
        try:
            import numpy as np
            import sounddevice as sd
            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            sd.play(audio, samplerate=sample_rate, blocking=True)
        except Exception as exc:
            logger.error("[EdgeTTS] playback error: %s", exc)
