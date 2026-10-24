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
import time
import uuid
from collections import OrderedDict
from typing import Callable, List, Optional
from samsara.log import safe_exception
from samsara.output_devices import output_sample_rate
from samsara.runtime import thread_registry

from .audio_utils import resample_pcm
from .engine_base import SpeechHandle, TTSEngine, VoiceInfo
from .exceptions import EngineUnavailableError, RenderError
from .mp3_decode import mp3_to_pcm

logger = logging.getLogger(__name__)

_DEFAULT_VOICE = "en-US-AvaNeural"

# Session-only PCM: bounded even when confirmations contain varying details.
_SHORT_REPLY_CHARS = 160
_CACHE_ENTRIES = 32
_CACHE_BYTES = 4 * 1024 * 1024
_CACHE_ITEM_BYTES = 882000  # ten seconds of 44.1 kHz mono int16

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


class _SynthesisRevoked(Exception):
    """The active Edge request lost TTS authorization."""


class _PartialAudio(bytes):
    """Best-effort audio from exhausted retries; never reusable in the cache."""


def _synthesize_mp3(voice_id: str, text: str, rate: str, volume: str,
                    authorize=None, max_attempts=3, timeout=10.0) -> bytes:
    """Synthesize text via edge-tts, return raw MP3 bytes.

    edge-tts streams audio over a websocket that can drop mid-utterance on
    longer texts, ending the async loop early with no exception and yielding
    a silently-truncated MP3. We estimate a minimum expected byte count from
    the text length and retry if the stream comes back suspiciously short.
    timeout bounds first audio and each subsequent audio gap, never total
    duration. Metadata alone cannot keep an inaudible request alive.
    """
    import edge_tts

    # Conservative lower bound: ~12 chars/sec of speech, ~3500 bytes/sec at
    # edge-tts's MP3 bitrate. Anything well under this for the given text
    # length means the stream was very likely cut short. Floor of 2KB so
    # short replies never trip the check.
    min_expected_bytes = max(2048, int(len(text) / 12.0 * 3500))

    def _require_authorized():
        if authorize is not None and not authorize():
            raise _SynthesisRevoked()

    async def _do():
        _require_authorized()
        communicate = edge_tts.Communicate(text, voice_id, rate=rate, volume=volume)
        buf = io.BytesIO()
        got_audio = False
        stream = communicate.stream().__aiter__()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        error = None
        try:
            while True:
                _require_authorized()
                chunk = await asyncio.wait_for(stream.__anext__(),
                                               max(0.0, deadline - loop.time()))
                _require_authorized()
                if chunk["type"] == "audio" and chunk["data"]:
                    buf.write(chunk["data"])
                    got_audio = True
                    deadline = loop.time() + timeout
        except StopAsyncIteration:
            pass
        except _SynthesisRevoked:
            raise
        except Exception as exc:
            error = exc
        finally:
            await stream.aclose()
        return buf.getvalue(), got_audio, error

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
    for attempt in range(1, max_attempts + 1):
        _require_authorized()
        try:
            audio, got_audio, error = _run_isolated()
        except _SynthesisRevoked:
            raise
        except Exception as exc:  # websocket / network blip
            status = getattr(exc, 'status', None)
            logger.warning("[EdgeTTS] stream attempt %d failed (status=%s, type=%s)",
                           attempt, status if isinstance(status, int) else 'unknown',
                           type(exc).__name__)
            if isinstance(status, int) and 400 <= status < 500:
                break
            continue

        # Keep the best partial result even if a later attempt returns nothing.
        if len(audio) > len(mp3):
            mp3 = audio
        if error is not None:
            status = getattr(error, 'status', None)
            logger.warning("[EdgeTTS] stream attempt %d failed (status=%s, type=%s)",
                           attempt, status if isinstance(status, int) else 'unknown',
                           type(error).__name__)
            if not got_audio and isinstance(status, int) and 400 <= status < 500:
                break  # A handshake refusal cannot recover by repeating it now.
            continue

        if got_audio and len(audio) >= min_expected_bytes:
            if attempt > 1:
                logger.info("[EdgeTTS] stream recovered on attempt %d", attempt)
            return audio

        logger.warning(
            "[EdgeTTS] short stream on attempt %d: %d bytes "
            "(expected >= %d for %d chars) -- retrying",
            attempt, len(mp3), min_expected_bytes, len(text),
        )

    # All retries exhausted. Return whatever we last got (partial speech is
    # better than silence); only fail hard if we got nothing at all.
    if mp3:
        logger.error(
            "[EdgeTTS] still short after %d attempts (%d bytes) -- "
            "speaking partial audio", max_attempts, len(mp3),
        )
        return _PartialAudio(mp3)
    raise RenderError("edge-tts returned no audio")


def _mp3_to_pcm(mp3_bytes: bytes, target_sr: int = 44100) -> bytes:
    """Convert network MP3 to 16-bit mono PCM without a child when PyAV is present."""
    return mp3_to_pcm(mp3_bytes, target_sr)


class EdgeTTSEngine(TTSEngine):
    """TTS engine backed by Microsoft Edge's Read Aloud API (edge-tts).

    Requires internet connection. Voice quality is Azure Neural TTS.
    Drop-in replacement for WinRTEngine.
    """

    def __init__(self, output_device: Optional[int] = None, output_device_resolver=None,
                 *, fallback_voice_id: Optional[str] = None):
        _import_edge_tts()
        self._voices = list(_BUILTIN_VOICES)
        self._lock = threading.RLock()
        self._active_handle: Optional[SpeechHandle] = None
        self._state = "idle"
        self._cancelled = False
        self._output_device = output_device
        self._output_device_resolver = output_device_resolver
        self._output_revision = 0
        self._authorization_checker = None
        self._jobs = {}
        self._worker_lock = threading.Lock()
        self._fallback_engine = None
        self._fallback_listener = None
        self._fallback_voice_id = fallback_voice_id
        self._fallback_voice_resolver = None
        self._short_reply_cache = OrderedDict()
        self._cache_bytes = 0
        self._failure_count = 0
        self._retry_at = 0.0
        self._shutdown = False
        logger.info("[TTS] EdgeTTSEngine initialized (Azure Neural voices via edge-tts)")

    def set_authorization_checker(self, checker) -> None:
        """Bind the current app authorization; checked again by the worker."""
        self._authorization_checker = checker

    def _authorized(self, *, check_cancelled: bool = True) -> bool:
        with self._lock:
            if check_cancelled and self._cancelled:
                return False
        checker = self._authorization_checker
        return checker is None or bool(checker())

    def set_output_device(self, device_id: Optional[int]) -> None:
        """Route subsequent speech to a Samsara-specific output device."""
        with self._lock:
            self._output_device = device_id
            self._output_revision += 1
            local = self._fallback_engine
        if local is not None:
            local.set_output_device(device_id)

    def restart_stream(self) -> None:
        """Forward the existing default-output watcher to the Windows stream.

        Network playback opens a fresh stream for every utterance.
        """
        with self._lock:
            self._output_revision += 1
            local = self._fallback_engine
        if local is not None:
            local.restart_stream()

    def set_fallback_listener(self, callback) -> None:
        """Non-speech notification hook; called when Windows takes an utterance."""
        self._fallback_listener = callback

    def set_fallback_voice_resolver(self, callback) -> None:
        """Resolve an explicitly selected Windows voice, never an OS default."""
        self._fallback_voice_resolver = callback

    def _cached_pcm(self, key):
        with self._lock:
            pcm = self._short_reply_cache.get(key)
            if pcm is not None:
                self._short_reply_cache.move_to_end(key)
            return pcm

    def _cache_pcm(self, key, pcm):
        if key is None or len(pcm) > _CACHE_ITEM_BYTES:
            return
        with self._lock:
            if self._shutdown:
                return
            previous = self._short_reply_cache.pop(key, b'')
            self._cache_bytes += len(pcm) - len(previous)
            self._short_reply_cache[key] = pcm
            while (len(self._short_reply_cache) > _CACHE_ENTRIES
                   or self._cache_bytes > _CACHE_BYTES):
                _, evicted = self._short_reply_cache.popitem(last=False)
                self._cache_bytes -= len(evicted)

    def _playback_device(self):
        """Resolve output immediately before this utterance opens its stream."""
        if callable(self._output_device_resolver):
            try:
                return self._output_device_resolver()
            except Exception as exc:
                logger.warning("[EdgeTTS] output resolver failed; using Windows default: %s", exc)
                return None
        return self._output_device

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
        # voice_result is pending/network/windows/failed/cancelled. Consumers may
        # inspect network_failed during fallback, or both fields after on_done.
        # network_bypassed means cooldown used Windows without a Network request.
        handle = SpeechHandle(utterance_id=str(uuid.uuid4()))
        handle.voice_result = 'pending'
        handle.network_failed = False
        handle.network_bypassed = False
        handle.audio_cache_hit = False
        handle.audio_started_ms = None
        started = time.perf_counter()
        cache_key = ((voice_id or _DEFAULT_VOICE, text, speed, pitch, volume)
                     if 0 < len(text) <= _SHORT_REPLY_CHARS else None)
        job = {'handle': handle, 'cancelled': threading.Event(), 'local': None}
        with self._lock:
            self._jobs[handle.utterance_id] = job

        def authorized():
            return (not job['cancelled'].is_set() and not self._shutdown
                    and self._authorized(check_cancelled=False))

        def finish():
            with self._lock:
                self._jobs.pop(handle.utterance_id, None)
                if self._active_handle is handle:
                    self._state = 'idle'
                    self._active_handle = None
            handle._state = 'done'
            if on_done:
                try:
                    on_done()
                except Exception:
                    safe_exception(logger, '[EdgeTTS] on_done callback failed', level=logging.DEBUG)

        def worker():
            try:
                # Serial playback also serializes circuit-breaker decisions.
                with self._worker_lock:
                    if not authorized():
                        handle.voice_result = 'cancelled'
                        return
                    with self._lock:
                        self._active_handle = handle
                        self._state = 'synthesizing'
                        pcm = self._cached_pcm(cache_key) if cache_key is not None else None
                        handle.audio_cache_hit = pcm is not None
                        bypass = pcm is None and time.monotonic() < self._retry_at
                        probe = self._retry_at > 0 and not bypass
                    handle.network_bypassed = bypass
                    if not bypass:
                        cacheable = False
                        try:
                            if pcm is None:
                                rate_pct = int((speed - 1.0) * 100)
                                vol_pct = int((volume - 1.0) * 100)
                                mp3 = _synthesize_mp3(
                                    voice_id or _DEFAULT_VOICE, text,
                                    f'{rate_pct:+d}%', f'{vol_pct:+d}%',
                                    authorize=authorized, max_attempts=1 if probe else 3,
                                )
                                pcm = _mp3_to_pcm(mp3)
                                cacheable = not isinstance(mp3, _PartialAudio)
                            if not pcm:
                                raise RenderError('Network voice decoded no audio')
                        except _SynthesisRevoked:
                            handle.voice_result = 'cancelled'
                            return
                        except Exception:
                            if not authorized():
                                handle.voice_result = 'cancelled'
                                return
                            safe_exception(logger, '[EdgeTTS] Network voice unavailable',
                                           level=logging.WARNING)
                            with self._lock:
                                self._failure_count += 1
                                if self._failure_count >= 2:
                                    self._retry_at = time.monotonic() + 600.0
                        else:
                            if not authorized():
                                handle.voice_result = 'cancelled'
                                return
                            with self._lock:
                                if not handle.audio_cache_hit:
                                    self._failure_count = 0
                                    self._retry_at = 0.0
                                self._state = 'playing'

                            def on_start():
                                handle.audio_started_ms = (time.perf_counter() - started) * 1000
                                logger.debug('[EdgeTTS] first audio write: %.0fms (cache=%s)',
                                             handle.audio_started_ms, handle.audio_cache_hit)

                            self._play_pcm(pcm, authorize=authorized, on_start=on_start)
                            handle.voice_result = 'network' if authorized() else 'cancelled'
                            if authorized() and cacheable:
                                self._cache_pcm(cache_key, pcm)
                            return
                    handle.network_failed = not bypass
                    self._speak_windows(job, text, speed, pitch, volume, category,
                                        queue_mode, authorized)
            except Exception:
                handle.voice_result = 'failed' if authorized() else 'cancelled'
                safe_exception(logger, '[EdgeTTS] speech failed', level=logging.WARNING)
            finally:
                finish()

        if not authorized():
            handle.voice_result = 'cancelled'
            finish()
        else:
            try:
                thread_registry.spawn('EdgeTTS-worker', worker, daemon=True)
            except Exception:
                handle.voice_result = 'failed'
                finish()
                raise
        return handle

    def _speak_windows(self, job, text, speed, pitch, volume, category, queue_mode,
                       authorized):
        resolver = self._fallback_voice_resolver
        voice_id = resolver() if resolver is not None else self._fallback_voice_id
        if not isinstance(voice_id, str) or not voice_id:
            # Edge's ID cannot identify a Windows voice. With no explicit
            # local choice, keep the existing text instead of changing voices.
            job['handle'].voice_result = 'failed'
            logger.warning('[EdgeTTS] no selected Windows fallback voice; keeping text only')
            return
        completed = threading.Event()
        with self._lock:
            if not authorized():
                job['handle'].voice_result = 'cancelled'
                return
            local = self._fallback_engine
            device, revision = self._output_device, self._output_revision
        if local is None:
            # _worker_lock serializes builders. Voice enumeration and opening
            # an output stream must not block cancel_all or state inspection.
            from .winrt_engine import WinRTEngine
            local = WinRTEngine(output_device=device,
                                output_device_resolver=self._output_device_resolver)
            with self._lock:
                stopped = self._shutdown
                if not stopped:
                    self._fallback_engine = local
                changed = revision != self._output_revision
                device = self._output_device
            if stopped:
                local.shutdown()
                job['handle'].voice_result = 'cancelled'
                return
            if changed:
                local.set_output_device(device)
        with self._lock:
            if not authorized():
                job['handle'].voice_result = 'cancelled'
                return
            job['local'] = local.speak(text, voice_id=voice_id,
                                      speed=speed, pitch=pitch, volume=volume,
                                      category=category, queue_mode=queue_mode,
                                      on_done=completed.set)
        notified = False
        while True:
            if not authorized():
                local.cancel(job['local'])
                job['handle'].voice_result = 'cancelled'
                return
            state = job['local']._state
            # done also confirms successful playback if a short utterance
            # finished between polls. Failed synthesis must never show a chip.
            if not notified and state in ('playing', 'done'):
                notified = True
                if self._fallback_listener:
                    try:
                        self._fallback_listener()
                    except Exception:
                        safe_exception(logger, '[EdgeTTS] fallback notification failed', level=logging.DEBUG)
            if completed.is_set() or state in ('done', 'failed', 'cancelled', 'cancelling'):
                break
            completed.wait(.02)
        job['handle'].voice_result = (
            'cancelled' if not authorized() or job['local']._state in ('cancelled', 'cancelling') else
            'failed' if job['local']._state == 'failed' else 'windows')

    def cancel(self, handle: SpeechHandle) -> None:
        with self._lock:
            job = self._jobs.get(handle.utterance_id)
            if job:
                job['cancelled'].set()
                if job['local'] is not None:
                    self._fallback_engine.cancel(job['local'])

    def cancel_all(self) -> None:
        with self._lock:
            self._cancelled = True
            for job in self._jobs.values():
                job['cancelled'].set()
            if self._fallback_engine is not None:
                self._fallback_engine.cancel_all()

    def set_volume(self, handle: SpeechHandle, volume: float, fade_ms: int = 5) -> None:
        with self._lock:
            job = self._jobs.get(handle.utterance_id)
            if job and job['local'] is not None:
                self._fallback_engine.set_volume(job['local'], volume, fade_ms=fade_ms)

    def is_speaking(self) -> bool:
        with self._lock:
            return bool(self._jobs)

    def get_engine_state(self) -> str:
        with self._lock:
            active = self._active_handle
            job = self._jobs.get(active.utterance_id) if active else None
            if job and job['local'] is not None:
                return self._fallback_engine.get_engine_state()
            return self._state

    def shutdown(self) -> None:
        self._shutdown = True
        self.cancel_all()
        with self._lock:
            self._short_reply_cache.clear()
            self._cache_bytes = 0
            if self._fallback_engine is not None:
                self._fallback_engine.shutdown()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _play_pcm(self, pcm_bytes: bytes, sample_rate: int = 44100,
                  authorize=None, on_start=None) -> None:
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

            device = self._playback_device()
            try:
                stream_context, audio, playback_rate = _prepare(device)
            except Exception as exc:
                if device is None:
                    raise
                logger.warning(
                    "[EdgeTTS] output device %s unavailable (%s); falling back to system default",
                    device, exc,
                )
                stream_context, audio, playback_rate = _prepare(None)

            total_frames = len(audio)
            with stream_context as stream:
                idx = 0
                while idx < total_frames:
                    if authorize is not None:
                        if not authorize():
                            break
                    else:
                        with self._lock:
                            if self._cancelled:
                                break
                    block = audio[idx:idx + blocksize]
                    stream.write(block)
                    if frames_written == 0 and on_start is not None:
                        on_start()
                    frames_written += len(block)
                    idx += blocksize

            played_s = frames_written / float(playback_rate)
            logger.debug(
                "[EdgeTTS] playback done: %.1fs of %.1fs (%.0f%%) in %.1fs wall",
                played_s, expected_s,
                (played_s / expected_s * 100.0) if expected_s else 0.0,
                time.time() - t0,
            )
        except Exception:
            safe_exception(logger, '[EdgeTTS] playback error', level=logging.ERROR)
            raise
