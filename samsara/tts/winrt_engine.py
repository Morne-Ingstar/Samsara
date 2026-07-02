"""WinRTEngine: TTS via Windows Runtime SpeechSynthesis.

Phase 1b upgrades over Phase 1a:
  - Persistent TTS OutputStream owned by the engine (opened at __init__).
    Eliminates 5-10ms per-utterance stream open overhead. Falls back to
    ephemeral streams if WASAPI exclusive-mode blocks the persistent open.
  - Volume fade: set_volume(handle, volume, fade_ms=5) interpolates linearly
    across fade_ms milliseconds instead of changing instantaneously.
  - Granular engine state: get_engine_state() returns 'idle'|'synthesizing'|
    'playing'|'cancelling' so AudioCoordinator can distinguish synthesis lag
    from actual playback for interrupt-grace-period timing.

Phase 1b still pending (Phase 2):
  - queue_mode: only "append" is enforced; other modes degrade to append.
  - category: accepted and stored, not yet used for arbitration.
"""

import collections
import logging
import threading
import uuid
from typing import Callable, Deque, List, Optional, Tuple

import numpy as np

from .audio_utils import parse_wav, resample_pcm
from .engine_base import SpeechHandle, TTSEngine, VoiceInfo
from .exceptions import EngineUnavailableError, RenderError
from .winrt_helper import get_helper

logger = logging.getLogger(__name__)

_TARGET_SR = 44100  # matches dictation.py _sound_stream_sr
_CHUNK_FRAMES = 4096  # ~93ms at 44100 Hz


def _import_winsdk():
    """Import WinRT speech classes, raising EngineUnavailableError on failure."""
    try:
        from winsdk.windows.media.speechsynthesis import SpeechSynthesizer, VoiceGender
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
    """Per-speak() state including volume fade tracking."""

    def __init__(self, handle: SpeechHandle, on_done: Optional[Callable], initial_volume: float = 1.0):
        self.handle = handle
        self.on_done = on_done
        self.cancelled = threading.Event()
        self.thread: Optional[threading.Thread] = None

        # engine_state tracks finer-grained state than the handle._state.
        # Written by the worker thread; read by get_engine_state().
        # Values: 'pending' | 'synthesizing' | 'playing' | 'cancelling' | 'done' | 'failed'
        self.engine_state: str = 'pending'
        self._state_lock = threading.Lock()

        # Volume fade state. All three are protected by _vol_lock.
        # current_volume: actual volume being applied right now
        # target_volume: volume we're fading toward
        # volume_step: per-sample delta (positive = fade up, negative = fade down; 0 = at target)
        self._vol_lock = threading.Lock()
        self.current_volume = float(initial_volume)
        self.target_volume = float(initial_volume)
        self.volume_step = 0.0

    def set_engine_state(self, state: str) -> None:
        with self._state_lock:
            self.engine_state = state
        self.handle._state = state

    def get_engine_state(self) -> str:
        with self._state_lock:
            return self.engine_state

    def set_volume_fade(self, target: float, fade_ms: int) -> None:
        """Set a new fade target. Thread-safe; callable from any thread."""
        target = max(0.0, min(1.0, target))
        fade_samples = int(fade_ms * _TARGET_SR / 1000)
        with self._vol_lock:
            self.target_volume = target
            if fade_samples <= 0:
                self.current_volume = target
                self.volume_step = 0.0
            else:
                delta = target - self.current_volume
                self.volume_step = delta / max(fade_samples, 1)

    def apply_volume_to_chunk(self, chunk: np.ndarray) -> np.ndarray:
        """Apply per-sample volume (with active fade) to a float32 chunk.

        Returns a new float32 array. Updates current_volume and volume_step
        in-place as the fade progresses.
        """
        with self._vol_lock:
            cur = self.current_volume
            step = self.volume_step
            tgt = self.target_volume

        n = len(chunk)
        if step == 0.0:
            result = (chunk * cur).astype(np.float32)
        else:
            steps = np.arange(n, dtype=np.float32)
            vols = cur + step * steps
            if step > 0:
                vols = np.minimum(vols, tgt)
            else:
                vols = np.maximum(vols, tgt)
            result = (chunk * vols).astype(np.float32)
            new_cur = float(vols[-1])
            new_step = 0.0 if new_cur == tgt else step
            with self._vol_lock:
                self.current_volume = new_cur
                self.volume_step = new_step

        return result


class WinRTEngine(TTSEngine):
    """TTS engine backed by Windows Runtime SpeechSynthesis.

    Raises EngineUnavailableError at construction if winsdk is not installed.
    """

    def __init__(self):
        SpeechSynthesizer, VoiceGender, _, _ = _import_winsdk()
        self._SpeechSynthesizer = SpeechSynthesizer
        self._VoiceGender = VoiceGender

        self._helper = get_helper()

        self._voices: List[VoiceInfo] = self._build_voice_list()

        # Active utterance tracking (all access under _active_lock).
        self._active: dict = {}
        self._active_lock = threading.Lock()

        # Persistent TTS OutputStream and its shared buffer.
        # The buffer holds (float32_array, utterance_uid) tuples so cancel()
        # can purge a specific utterance's queued audio.
        self._tts_buffer: Deque[Tuple[np.ndarray, str]] = collections.deque()
        self._tts_buffer_lock = threading.Lock()
        self._tts_stream = None
        self._using_persistent_stream = False
        self._open_persistent_stream()

    # ------------------------------------------------------------------
    # Persistent stream management
    # ------------------------------------------------------------------

    def _open_persistent_stream(self) -> None:
        """Open the persistent TTS OutputStream. Falls back gracefully."""
        import sounddevice as sd
        try:
            self._tts_stream = sd.OutputStream(
                samplerate=_TARGET_SR,
                channels=1,
                dtype='float32',
                callback=self._tts_callback,
                blocksize=_CHUNK_FRAMES,
            )
            self._tts_stream.start()
            self._using_persistent_stream = True
            logger.info("TTS persistent stream opened at %d Hz", _TARGET_SR)
        except Exception as exc:
            logger.warning(
                "TTS persistent stream failed to open (%s). "
                "Falling back to per-utterance ephemeral streams.", exc
            )
            self._tts_stream = None
            self._using_persistent_stream = False

    def _tts_callback(self, outdata, frames, time_info, status):
        """Drain TTS buffer into the audio callback output. Called by PortAudio."""
        with self._tts_buffer_lock:
            output = np.zeros(frames, dtype=np.float32)
            remaining = frames
            offset = 0
            while remaining > 0 and self._tts_buffer:
                chunk, uid = self._tts_buffer[0]
                take = min(len(chunk), remaining)
                output[offset:offset + take] = chunk[:take]
                if take < len(chunk):
                    self._tts_buffer[0] = (chunk[take:], uid)
                else:
                    self._tts_buffer.popleft()
                offset += take
                remaining -= take
        outdata[:, 0] = output

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
        """Synthesize text and play it asynchronously. Returns immediately."""
        if queue_mode != "append":
            logger.debug("queue_mode=%r not implemented; treating as 'append'", queue_mode)

        uid = str(uuid.uuid4())
        handle = SpeechHandle(utterance_id=uid, _state="pending")
        utterance = _Utterance(handle, on_done, initial_volume=volume)

        with self._active_lock:
            self._active[uid] = utterance

        t = threading.Thread(
            target=self._playback_worker,
            args=(utterance, text, voice_id, speed, pitch),
            daemon=True,
            name=f"tts-{uid[:8]}",
        )
        utterance.thread = t
        t.start()
        return handle

    def cancel(self, handle: SpeechHandle) -> None:
        """Signal a specific utterance to stop. Purges buffered audio."""
        with self._active_lock:
            utterance = self._active.get(handle.utterance_id)
        if utterance:
            utterance.cancelled.set()
            utterance.set_engine_state('cancelling')
            # Purge this utterance's queued chunks from the persistent buffer.
            uid = handle.utterance_id
            with self._tts_buffer_lock:
                self._tts_buffer = collections.deque(
                    (chunk, u) for chunk, u in self._tts_buffer if u != uid
                )

    def cancel_all(self) -> None:
        """Signal all active utterances to stop and clear the buffer."""
        with self._active_lock:
            utterances = list(self._active.values())
        for u in utterances:
            u.cancelled.set()
            u.set_engine_state('cancelling')
        with self._tts_buffer_lock:
            self._tts_buffer.clear()

    def set_volume(self, handle: SpeechHandle, volume: float, fade_ms: int = 5) -> None:
        """Adjust playback volume for a live utterance with linear fade."""
        with self._active_lock:
            utterance = self._active.get(handle.utterance_id)
        if utterance:
            utterance.set_volume_fade(volume, fade_ms)

    def is_speaking(self) -> bool:
        """True if any utterance is in synthesizing or playing state."""
        with self._active_lock:
            return bool(self._active)

    def get_engine_state(self) -> str:
        """Return 'idle', 'synthesizing', 'playing', or 'cancelling'."""
        with self._active_lock:
            if not self._active:
                return 'idle'
            states = [u.get_engine_state() for u in self._active.values()]
        # Priority: synthesizing > playing > cancelling (most informative first)
        if 'synthesizing' in states:
            return 'synthesizing'
        if 'playing' in states:
            return 'playing'
        if 'cancelling' in states:
            return 'cancelling'
        return 'idle'

    def list_voices(self) -> List[VoiceInfo]:
        return list(self._voices)

    def restart_stream(self) -> None:
        """Close and reopen the persistent TTS OutputStream on the current default device.

        Called by the output-device watcher in dictation.py when the Windows
        default audio output changes. Drops buffered audio for the old device.
        """
        with self._tts_buffer_lock:
            self._tts_buffer.clear()
        if self._tts_stream is not None:
            try:
                self._tts_stream.abort()
                self._tts_stream.close()
            except Exception as e:
                logger.debug(f"TTS stream abort/close failed during restart: {e}")
            self._tts_stream = None
        self._using_persistent_stream = False
        self._open_persistent_stream()
        logger.info('[TTS] Stream restarted after output device change')

    def shutdown(self) -> None:
        """Cancel all utterances, join threads, close the persistent stream.

        The WinRTHelper event loop is a process-level singleton and is NOT
        shut down here. The helper lives for the lifetime of the process so
        that multiple WinRTEngine instances can share it without the first
        to shut down killing the shared loop.
        """
        self.cancel_all()
        with self._active_lock:
            threads = [u.thread for u in self._active.values() if u.thread]
        for t in threads:
            t.join(timeout=1.0)
        if self._tts_stream is not None:
            try:
                self._tts_stream.abort()
                self._tts_stream.close()
            except Exception as e:
                logger.debug(f"TTS stream abort/close failed during shutdown: {e}")
            self._tts_stream = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_voice_list(self) -> List[VoiceInfo]:
        """Enumerate available TTS voices.

        Tries the WinRT SpeechSynthesizer.all_voices API first (works on
        newer winsdk builds).  Falls back to a direct registry scan when
        the WinRT API is unavailable or returns an empty list — this also
        picks up Natural HD voices (Ava, Aria, Jenny, etc.) that have been
        unlocked via tools/Enable-NaturalVoices.ps1.
        """
        # --- WinRT path (preferred) ---
        try:
            from winsdk.windows.media.speechsynthesis import VoiceGender as VG
            all_voices = getattr(self._SpeechSynthesizer, 'all_voices', None)
            if all_voices is not None and len(all_voices) > 0:
                voices = []
                for v in all_voices:
                    gender_val = getattr(v, 'gender', None)
                    if gender_val == VG.MALE:
                        gender = 'male'
                    elif gender_val == VG.FEMALE:
                        gender = 'female'
                    else:
                        gender = 'unknown'
                    voices.append(VoiceInfo(
                        voice_id=v.id,
                        display_name=v.display_name,
                        language=v.language,
                        gender=gender,
                    ))
                if voices:
                    return voices
        except Exception as exc:
            logger.debug("WinRT voice enumeration failed, using registry fallback: %s", exc)

        # --- Registry fallback ---
        # Reads from both the standard Speech_OneCore path and the legacy
        # Speech path.  Natural HD voices copied by Enable-NaturalVoices.ps1
        # end up in Speech_OneCore\Voices\Tokens and will appear here.
        return self._build_voice_list_from_registry()

    def _build_voice_list_from_registry(self) -> List[VoiceInfo]:
        """Read TTS voice tokens directly from the Windows registry."""
        import winreg
        voices = []
        seen_ids = set()

        registry_paths = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Speech\Voices\Tokens"),
        ]

        for hive, base_path in registry_paths:
            try:
                base_key = winreg.OpenKey(hive, base_path)
            except OSError:
                continue

            with base_key:
                i = 0
                while True:
                    try:
                        token_name = winreg.EnumKey(base_key, i)
                        i += 1
                    except OSError:
                        break

                    token_path = base_path + "\\" + token_name
                    try:
                        token_key = winreg.OpenKey(hive, token_path)
                        display_name, _ = winreg.QueryValueEx(token_key, "")
                    except OSError:
                        continue

                    # Build a stable voice_id from the registry path
                    hive_prefix = "HKEY_LOCAL_MACHINE" if hive == winreg.HKEY_LOCAL_MACHINE else "HKEY_CURRENT_USER"
                    voice_id = f"{hive_prefix}\\{token_path}"

                    if voice_id in seen_ids:
                        winreg.CloseKey(token_key)
                        continue
                    seen_ids.add(voice_id)

                    # Read attributes subkey for gender / language
                    gender = 'unknown'
                    language = 'en-US'
                    try:
                        attr_key = winreg.OpenKey(hive, token_path + "\\Attributes")
                        try:
                            g, _ = winreg.QueryValueEx(attr_key, "Gender")
                            gender = g.lower() if g.lower() in ('male', 'female') else 'unknown'
                        except OSError as e:
                            logger.debug(f"Registry Gender lookup failed for {token_path}: {e}")
                        try:
                            lang_code, _ = winreg.QueryValueEx(attr_key, "Language")
                            # Language is stored as a hex locale id (e.g. "409" = 0x409 = en-US).
                            # Always parse as hex — the value is LCID in hex, never decimal.
                            try:
                                import locale
                                lcid = int(lang_code, 16)
                                language = locale.windows_locale.get(lcid, 'en-US')
                                # windows_locale returns "en_US" style — normalise to "en-US"
                                language = language.replace('_', '-') if language else 'en-US'
                            except Exception:
                                language = 'en-US'
                        except OSError as e:
                            logger.debug(f"Registry Language lookup failed for {token_path}: {e}")
                        winreg.CloseKey(attr_key)
                    except OSError as e:
                        logger.debug(f"Registry Attributes key read failed for {token_path}: {e}")

                    winreg.CloseKey(token_key)

                    voices.append(VoiceInfo(
                        voice_id=voice_id,
                        display_name=display_name,
                        language=language,
                        gender=gender,
                    ))

        return voices

    def _synthesize(self, text: str, voice_id: Optional[str], speed: float, pitch: float) -> bytes:
        """Synthesize text → WAV bytes via WinRT. Blocks until synthesis completes."""
        synth = self._SpeechSynthesizer()
        if voice_id:
            # Try WinRT all_voices first (works on newer winsdk builds)
            matched = False
            try:
                all_voices = getattr(self._SpeechSynthesizer, 'all_voices', None)
                if all_voices:
                    for v in all_voices:
                        if v.id == voice_id:
                            synth.voice = v
                            matched = True
                            break
            except Exception as e:
                logger.debug(f"WinRT all_voices voice matching failed: {e}")

            # Fallback: set voice via WinRT try_set_default_voice_async using
            # the registry token. This supports Natural HD voices unlocked by
            # Enable-NaturalVoices.ps1 whose voice_id is a registry path.
            if not matched:
                try:
                    # Build a token-id string WinRT understands from the
                    # registry path voice_id (e.g. "HKEY_LOCAL_MACHINE\...")
                    async def _set_voice():
                        result = await synth.try_set_default_voice_async(voice_id)
                        return result
                    self._helper.run_sync(_set_voice())
                    matched = True
                except Exception as exc:
                    logger.warning(
                        "Could not set voice %r via WinRT token: %s. Using default.", voice_id, exc
                    )

            if not matched:
                logger.warning("Voice id %r not found; using default", voice_id)

        opts = synth.options
        opts.speaking_rate = max(0.5, min(6.0, speed))
        opts.audio_pitch = max(0.0, min(2.0, pitch))

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
    ):
        """Daemon thread: synthesize → decode → push PCM → signal completion."""
        uid = utterance.handle.utterance_id
        try:
            utterance.set_engine_state('synthesizing')

            raw_bytes = self._synthesize(text, voice_id, speed, pitch)
            if utterance.cancelled.is_set():
                return

            pcm, sr, _ = parse_wav(raw_bytes)
            pcm = resample_pcm(pcm, sr, _TARGET_SR)

            if utterance.cancelled.is_set():
                return

            utterance.set_engine_state('playing')

            if self._using_persistent_stream:
                self._push_chunks(pcm, utterance, uid)
            else:
                self._stream_pcm_ephemeral(pcm, utterance)

        except Exception as exc:
            logger.exception("TTS playback failed for utterance %s: %s", uid, exc)
            utterance.set_engine_state('failed')
        finally:
            with self._active_lock:
                self._active.pop(uid, None)
            if utterance.get_engine_state() not in ('cancelled', 'cancelling', 'failed'):
                utterance.set_engine_state('done')
            if utterance.on_done and not utterance.cancelled.is_set():
                try:
                    utterance.on_done()
                except Exception:
                    logger.exception("on_done callback raised for utterance %s", uid)

    def _push_chunks(self, pcm: np.ndarray, utterance: _Utterance, uid: str) -> None:
        """Push PCM data into the persistent-stream buffer in chunk-sized pieces."""
        offset = 0
        total = len(pcm)
        while offset < total:
            if utterance.cancelled.is_set():
                with self._tts_buffer_lock:
                    self._tts_buffer = collections.deque(
                        (c, u) for c, u in self._tts_buffer if u != uid
                    )
                break
            end = min(offset + _CHUNK_FRAMES, total)
            raw_chunk = pcm[offset:end]
            scaled = utterance.apply_volume_to_chunk(raw_chunk)
            with self._tts_buffer_lock:
                self._tts_buffer.append((scaled, uid))
            offset = end

        # Wait for buffer to drain so on_done fires AFTER the audio plays.
        # Poll at 10ms intervals; bail early on cancel.
        if not utterance.cancelled.is_set():
            while True:
                with self._tts_buffer_lock:
                    remaining = sum(len(c) for c, u in self._tts_buffer if u == uid)
                if remaining == 0:
                    break
                if utterance.cancelled.is_set():
                    break
                import time
                time.sleep(0.01)

    def _stream_pcm_ephemeral(self, pcm: np.ndarray, utterance: _Utterance) -> None:
        """Fallback: open a per-utterance OutputStream and write chunks directly."""
        import sounddevice as sd
        try:
            stream = sd.OutputStream(
                samplerate=_TARGET_SR,
                channels=1,
                dtype='float32',
                blocksize=_CHUNK_FRAMES,
            )
        except sd.PortAudioError as exc:
            logger.error(
                "TTS stream unavailable (exclusive audio access?): %s", exc
            )
            utterance.set_engine_state('failed')
            return

        with stream:
            stream.start()
            offset = 0
            total = len(pcm)
            while offset < total:
                if utterance.cancelled.is_set():
                    break
                end = min(offset + _CHUNK_FRAMES, total)
                chunk = utterance.apply_volume_to_chunk(pcm[offset:end])
                stream.write(chunk.reshape(-1, 1))
                offset = end
