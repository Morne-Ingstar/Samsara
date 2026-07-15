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

from samsara.output_devices import output_sample_rate
from samsara.runtime import thread_registry

from .audio_utils import resample_pcm
from .engine_base import SpeechHandle, TTSEngine, VoiceInfo
from .exceptions import EngineUnavailableError, RenderError

logger = logging.getLogger(__name__)

_DEFAULT_VOICE = "en-US-AvaNeural"

# Curated English voice list — full list available via edge_tts.list_voices()
_BUILTIN_VOICES = [
    # ── US — newest / highest quality ──
    VoiceInfo("en-US-AvaNeural",     "Ava (Natural HD, en-US)",      "en-US", "female"),
    VoiceInfo("en-US-EmmaNeural",    "Emma (Natural HD, en-US)",     "en-US", "female"),
    VoiceInfo("en-US-AndrewNeural",  "Andrew (Natural HD, en-US)",   "en-US", "male"),
    VoiceInfo("en-US-BrianNeural",   "Brian (Natural HD, en-US)",    "en-US", "male"),
    # ── US — multilingual variants (same identity, switch languages mid-text) ──
    VoiceInfo("en-US-AvaMultilingualNeural",    "Ava — Multilingual (en-US)",    "en-US", "female"),
    VoiceInfo("en-US-EmmaMultilingualNeural",   "Emma — Multilingual (en-US)",   "en-US", "female"),
    VoiceInfo("en-US-AndrewMultilingualNeural", "Andrew — Multilingual (en-US)", "en-US", "male"),
    VoiceInfo("en-US-BrianMultilingualNeural",  "Brian — Multilingual (en-US)",  "en-US", "male"),
    # ── US — older but solid ──
    VoiceInfo("en-US-JennyNeural",   "Jenny (en-US)",                "en-US", "female"),
    VoiceInfo("en-US-AriaNeural",    "Aria (en-US)",                 "en-US", "female"),
    VoiceInfo("en-US-MichelleNeural","Michelle (en-US)",             "en-US", "female"),
    VoiceInfo("en-US-GuyNeural",     "Guy (en-US)",                  "en-US", "male"),
    VoiceInfo("en-US-EricNeural",    "Eric (en-US)",                 "en-US", "male"),
    VoiceInfo("en-US-ChristopherNeural", "Christopher (en-US)",      "en-US", "male"),
    VoiceInfo("en-US-RogerNeural",   "Roger (en-US)",                "en-US", "male"),
    # ── Accents ──
    VoiceInfo("en-GB-SoniaNeural",   "Sonia (en-GB)",                "en-GB", "female"),
    VoiceInfo("en-GB-RyanNeural",    "Ryan (en-GB)",                 "en-GB", "male"),
    VoiceInfo("en-GB-LibbyNeural",   "Libby (en-GB)",                "en-GB", "female"),
    VoiceInfo("en-AU-NatashaNeural", "Natasha (en-AU)",              "en-AU", "female"),
    VoiceInfo("en-IE-EmilyNeural",   "Emily (en-IE)",                "en-IE", "female"),
    VoiceInfo("en-IE-ConnorNeural",  "Connor (en-IE)",               "en-IE", "male"),
    VoiceInfo("en-ZA-LeahNeural",    "Leah (en-ZA)",                 "en-ZA", "female"),
    VoiceInfo("en-ZA-LukeNeural",    "Luke (en-ZA)",                 "en-ZA", "male"),
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
    """Synthesize text via edge-tts, return raw MP3 bytes.

    edge-tts streams audio over a websocket that can drop mid-utterance on
    longer texts, ending the async loop early with no exception and yielding
    a silently-truncated MP3. We estimate a minimum expected byte count from
    the text length and retry if the stream comes back suspiciously short.
    """
    import edge_tts

    # Conservative lower bound: ~12 chars/sec of speech, ~3500 bytes/sec at
    # edge-tts's MP3 bitrate. Anything well under this for the given text
    # length means the stream was very likely cut short. Floor of 2KB so
    # short replies never trip the check.
    min_expected_bytes = max(2048, int(len(text) / 12.0 * 3500))

    async def _do():
        communicate = edge_tts.Communicate(text, voice_id, rate=rate, volume=volume)
        buf = io.BytesIO()
        got_audio = False
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
                got_audio = True
        return buf.getvalue(), got_audio

    def _run_isolated():
        """Run _do() on a private event loop in this thread.

        The app's main process runs a Windows proactor (IocpProactor) event
        loop for Qt/other async work. Calling asyncio.run() on the synthesis
        coroutine from a worker thread can still contend with that loop's
        subprocess/socket machinery, causing edge-tts's websocket stream to
        end early and return silently-truncated audio. Giving synthesis its
        own dedicated loop, fully isolated in a separate thread, removes that
        contention. The result is passed back via a mutable container.
        """
        result = {}

        def _thread_target():
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                result['value'] = loop.run_until_complete(_do())
            except Exception as exc:  # surface to caller below
                result['error'] = exc
            finally:
                try:
                    loop.close()
                finally:
                    asyncio.set_event_loop(None)

        t = thread_registry.spawn('edge-tts-synth', _thread_target, daemon=True)
        t.join()
        if 'error' in result:
            raise result['error']
        return result['value']

    mp3 = b""
    for attempt in range(1, 4):  # up to 3 tries
        try:
            mp3, got_audio = _run_isolated()
        except Exception as exc:  # websocket / network blip
            logger.warning("[EdgeTTS] stream attempt %d failed: %s", attempt, exc)
            continue

        if got_audio and len(mp3) >= min_expected_bytes:
            if attempt > 1:
                logger.info("[EdgeTTS] stream recovered on attempt %d", attempt)
            return mp3

        logger.warning(
            "[EdgeTTS] short stream on attempt %d: %d bytes "
            "(expected >= %d for %d chars) -- retrying",
            attempt, len(mp3), min_expected_bytes, len(text),
        )

    # All retries exhausted. Return whatever we last got (partial speech is
    # better than silence); only fail hard if we got nothing at all.
    if mp3:
        logger.error(
            "[EdgeTTS] still short after 3 attempts (%d bytes) -- "
            "speaking partial audio", len(mp3),
        )
        return mp3
    raise RenderError("edge-tts returned no audio after 3 attempts")


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

    def __init__(self, output_device: Optional[int] = None):
        _import_edge_tts()
        self._voices = list(_BUILTIN_VOICES)
        self._lock = threading.Lock()
        self._active_handle: Optional[SpeechHandle] = None
        self._state = "idle"
        self._cancelled = False
        self._output_device = output_device
        logger.info("[TTS] EdgeTTSEngine initialized (Azure Neural voices via edge-tts)")

    def set_output_device(self, device_id: Optional[int]) -> None:
        """Route subsequent speech to a Samsara-specific output device."""
        self._output_device = device_id

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
                    except Exception as e:
                        logger.debug(f"[EdgeTTS] on_done callback failed: {e}")

        t = thread_registry.spawn("EdgeTTS-worker", _worker, daemon=True)
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
        """Play raw 16-bit mono PCM through sounddevice.

        Uses an explicit OutputStream written in chunks rather than the
        sd.play() convenience wrapper. sd.play() was observed truncating
        long utterances (~20s) when a capture stream was concurrently active
        in the app, while reporting normal completion. Streaming the buffer
        in blocks avoids that wrapper's single-shot buffer behaviour and is
        robust to device contention. Respects self._cancelled for barge-in.
        """
        try:
            import numpy as np
            import sounddevice as sd
            import time

            source_audio = (
                np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
                / 32768.0
            )
            expected_s = len(source_audio) / float(sample_rate)
            blocksize = 2048

            t0 = time.time()
            frames_written = 0
            def _prepare(device):
                device_rate = output_sample_rate(
                    sd, device, fallback=sample_rate,
                )
                device_audio = resample_pcm(
                    source_audio, sample_rate, device_rate,
                )
                stream = sd.OutputStream(
                    samplerate=device_rate,
                    channels=1,
                    dtype='float32',
                    blocksize=blocksize,
                    device=device,
                )
                return stream, device_audio, device_rate

            try:
                stream_context, audio, playback_rate = _prepare(
                    self._output_device
                )
            except Exception as exc:
                if self._output_device is None:
                    raise
                logger.warning(
                    "[EdgeTTS] output device %s unavailable (%s); falling back to system default",
                    self._output_device, exc,
                )
                self._output_device = None
                stream_context, audio, playback_rate = _prepare(None)

            total_frames = len(audio)
            with stream_context as stream:
                idx = 0
                while idx < total_frames:
                    with self._lock:
                        if self._cancelled:
                            break
                    block = audio[idx:idx + blocksize]
                    stream.write(block)
                    frames_written += len(block)
                    idx += blocksize

            played_s = frames_written / float(playback_rate)
            logger.debug(
                "[EdgeTTS] playback done: %.1fs of %.1fs (%.0f%%) in %.1fs wall",
                played_s, expected_s,
                (played_s / expected_s * 100.0) if expected_s else 0.0,
                time.time() - t0,
            )
        except Exception as exc:
            logger.error("[EdgeTTS] playback error: %s", exc)
