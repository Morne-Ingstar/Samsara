"""WakeConsumer — ACE-04C: ring consumer for wake word listening mode.

Replaces wake_word_audio_callback + the wake PortAudio InputStream with a
daemon thread that polls the ACE engine ring. All VAD, OWW, state machine,
and flush policy is preserved exactly from wake_word_audio_callback —
only the audio source changes from PortAudio indata to ring frames.

== Key differences from wake_word_audio_callback ==

  Old: PortAudio callback thread, indata is float32 at capture_rate.
       self._prebuffer deque maintained per-chunk; drained into
       speech_buffer on speech_onset.

  New: daemon poll thread, frame.pcm is int16 at SAMPLE_RATE (16kHz).
       On speech_onset: reader.rewind(PREBUFFER_FRAMES) — structural
       prebuffer, impossible to forget. Prebuffer frames re-read and
       prepended to utterance buffer.

== Thread safety ==

All app state (is_speaking, silence_start, app_state, _oww_wake_detected,
speech_buffer, buffer_lock, etc.) is read/written on this thread, same as
the old PortAudio callback thread. The policy invariants are unchanged.

== Epoch change ==

A device_epoch change while the poll loop is running means the audio
stream was interrupted. The consumer aborts the current utterance,
resets speech state, and continues from the new epoch.
"""

import threading
import time

import numpy as np

from .frame import FRAME_MS, PREBUFFER_FRAMES, SAMPLE_RATE
from .ring import EMPTY

from samsara.constants import (
    DEFAULT_MIN_SPEECH_DURATION,
    DEFAULT_SPEECH_THRESHOLD,
    WAKE_DETECTION_SILENCE,
)
from samsara.session_modes import SessionMode
from samsara.log import get_logger

logger = get_logger(__name__)


class WakeConsumer:
    """Polls the ACE ring and runs the full wake word policy loop.

    Args:
        engine: AudioCaptureEngine.
        app:    DictationApp — policy state lives here.
    """

    def __init__(self, engine, app) -> None:
        self._engine  = engine
        self._app     = app
        self._reader  = engine.register_consumer("wake")
        self._running = False
        self._thread: threading.Thread | None = None

        # Local utterance buffer (replaces app.speech_buffer for wake path)
        self._utterance_frames: list = []   # float32 arrays at SAMPLE_RATE
        self._buffer_rms_history: list = []
        self._last_epoch: int | None  = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin the wake policy loop. Idempotent."""
        if self._running:
            return
        self._utterance_frames   = []
        self._buffer_rms_history = []
        self._last_epoch         = None
        self._running = True
        # Snap to current write head — skip pre-wake-mode ring history
        self._reader.snap_to_head()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="wake-consumer"
        )
        self._thread.start()

    def stop(self) -> list:
        """Stop the policy loop and return remaining utterance frames."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        remaining = list(self._utterance_frames)
        self._utterance_frames   = []
        self._buffer_rms_history = []
        return remaining

    def deactivate(self) -> None:
        """Stop and unregister from engine on app shutdown."""
        self.stop()
        try:
            self._engine.unregister_consumer(self._reader)
        except Exception as e:
            logger.debug(f"unregister_consumer failed during deactivate: {e}")

    def abort_utterance(self) -> None:
        """Immediately discard any in-progress utterance and reset speech
        state -- called when the audio device dies mid-utterance.

        This is the same cleanup _process_frame's epoch-change branch does,
        but callable directly and immediately: an epoch bump alone only
        triggers that cleanup on the NEXT frame, and during a device outage
        no new frames arrive at all (the engine has stopped writing), so
        without this the stale buffer would sit frozen -- neither flushed
        nor discarded -- for the whole recovery window."""
        app = self._app
        self._utterance_frames   = []
        self._buffer_rms_history = []
        app.is_speaking   = False
        app.silence_start = None
        try:
            app._vad_reset()
        except Exception as e:
            logger.debug(f"abort_utterance: vad reset failed: {e}")

    # ── Poll loop ─────────────────────────────────────────────────────────────

    @staticmethod
    def _is_toggle_cmd(app) -> bool:
        """True when toggle command mode is active (not wake-word or hold mode)."""
        return (
            getattr(app, 'command_mode_active', False)
            and app.config.get('command_mode', {}).get('mode', 'hold') == 'toggle'
        )

    @staticmethod
    def _is_ai_cmd_mode(app) -> bool:
        """True when AI command mode is active."""
        return getattr(app, 'ai_command_mode_active', False)

    def _poll_loop(self) -> None:
        app = self._app
        try:
            while self._running:
                if not (
                    app.wake_word_active
                    or self._is_toggle_cmd(app)
                    or self._is_ai_cmd_mode(app)
                ):
                    time.sleep(0.005)
                    continue

                frame = self._reader.read_next()
                if frame is EMPTY:
                    time.sleep(0.005)
                    continue

                try:
                    self._process_frame(frame)
                except Exception as exc:
                    logger.exception(f"[ERROR] Wake consumer frame error: {exc}")
        except Exception as exc:
            # This poll loop is the session's ONLY audio consumer. If
            # something escapes the per-frame guard above and kills this
            # thread, the session would otherwise go deaf while staying
            # latched (command_mode_active still True) -- silently. Fail
            # LOUD instead: log, earcon, and force the toggle-mode session
            # to end rather than leave a zombie session nobody can hear.
            print(f"[ERROR] Wake consumer loop died: {exc}")
            import traceback
            traceback.print_exc()
            self._running = False
            try:
                if getattr(app, "play_sound", None):
                    app.play_sound("error")
            except Exception:
                pass
            try:
                if self._is_toggle_cmd(app):
                    app.exit_command_mode()
            except Exception:
                pass

    def _process_frame(self, frame) -> None:  # noqa: C901 (complexity mirrors legacy callback)
        app = self._app

        # Epoch-change detection: abort utterance and reset state
        if self._last_epoch is not None and frame.device_epoch != self._last_epoch:
            logger.warning("[ACE] Wake path: epoch change — aborting utterance, resetting state")
            self._utterance_frames   = []
            self._buffer_rms_history = []
            app.is_speaking   = False
            app.silence_start = None
            try:
                app._vad_reset()
            except Exception as e:
                logger.debug(f"_vad_reset failed after epoch change: {e}")
        self._last_epoch = frame.device_epoch

        # Convert int16 ring frame -> float32 at SAMPLE_RATE
        # Ring stores raw (non-AEC) audio — correct for both VAD and Whisper.
        raw_chunk = frame.pcm.astype(np.float32) / 32767.0   # shape: (FRAME_SIZE,)

        # ── Post-command echo suppression (same guard as legacy callback) ─────
        # Guards only apply BEFORE speech onset. Once app.is_speaking is True,
        # we must keep capturing to avoid dropping frames mid-utterance.
        # (ARC audit: early-return guards prematurely truncating active utterances)
        if not app.is_speaking:
            if (app._command_executed_at is not None
                    and app.app_state not in ('long_dictation', 'quick_dictation', 'wake_session')):
                elapsed = time.time() - app._command_executed_at
                if elapsed < 2.0:
                    if app.echo_canceller.is_active:
                        ref_rms = getattr(app.echo_canceller, 'last_ref_rms', None)
                        if ref_rms is not None and ref_rms > 0.05:
                            return
                else:
                    app._command_executed_at = None

            # ── Hotkey-recording suppression ──────────────────────────────────
            if app._hotkey_recording:
                return   # skip policy but cursor advances (frame already read)

            # ── TTS guard ─────────────────────────────────────────────────────
            _coordinator = getattr(app, 'audio_coordinator', None)
            if _coordinator and _coordinator.is_speaking:
                app._tts_last_speaking = time.monotonic()
                return
            if time.monotonic() - getattr(app, '_tts_last_speaking', 0.0) < 0.3:
                return

        # ── RMS on raw signal (not AEC) ───────────────────────────────────────
        rms = float(np.sqrt(np.mean(raw_chunk ** 2)))

        # ── Threshold / state selection ───────────────────────────────────────
        ww_config    = app.config.get('wake_word_config', {})
        audio_config = ww_config.get('audio', {})
        speech_threshold = audio_config.get('speech_threshold', DEFAULT_SPEECH_THRESHOLD)
        if not app._vad_available:
            speech_threshold = min(speech_threshold, 0.01)
        min_speech = audio_config.get('min_speech_duration', DEFAULT_MIN_SPEECH_DURATION)

        if self._is_toggle_cmd(app):
            # Per-utterance silence gap for the unified session. Distinct
            # from inactivity_timeout_s (30s), which ends the whole session.
            # DICTATE gets a longer gap (mid-sentence pauses shouldn't cut
            # a chunk short); COMMAND keeps the tighter default.
            cm_cfg = app.config.get('command_mode', {})
            session_mgr = getattr(app, '_session_mode_manager', None)
            if session_mgr is not None and session_mgr.mode is SessionMode.DICTATE:
                silence_threshold = cm_cfg.get('dictate_utterance_silence_s', 2.0)
            else:
                silence_threshold = cm_cfg.get('utterance_silence_s', 1.0)
        elif app.app_state == 'long_dictation':
            silence_threshold = ww_config.get('long_chunk_silence', 1.0)
        elif app.app_state in ('quick_dictation', 'wake_session') and app._dictation_silence_timeout:
            silence_threshold = app._dictation_silence_timeout
        else:
            silence_threshold = audio_config.get('wake_detection_silence', WAKE_DETECTION_SILENCE)

        # ── VAD / OWW ─────────────────────────────────────────────────────────
        # Data is already at SAMPLE_RATE (16kHz) — pass src_rate explicitly
        if app._vad_available:
            try:
                is_speech = app._vad_is_speech(raw_chunk, src_rate=SAMPLE_RATE)
                app._vad_consec_errors = 0
            except Exception as exc:
                now  = time.time()
                last = getattr(app, '_vad_error_last_log', 0.0)
                app._vad_consec_errors = getattr(app, '_vad_consec_errors', 0) + 1
                if now - last >= 30.0:
                    logger.exception(f"[VAD] inference error (suppressing 30s): {type(exc).__name__}: {exc}")
                    app._vad_error_last_log = now
                try:
                    app._vad_reset()
                except Exception as e:
                    logger.debug(f"_vad_reset failed after VAD inference error: {e}")
                if app._vad_consec_errors >= 50:
                    logger.warning("[VAD] 50 consecutive errors — disabling VAD for session, RMS only")
                    app._vad_available = False
                is_speech = rms > speech_threshold
        else:
            is_speech = rms > speech_threshold

        # OWW pre-filter (data already at 16kHz — no resample needed)
        if (app.app_state == 'asleep'
                and not app.wake_word_triggered
                and app._wake_detector is not None
                and app._wake_detector.is_available):
            _oww_chunk = raw_chunk.copy()
            if rms > 0.005:
                _oww_gain = min(0.10 / rms, 20.0)
                _oww_chunk = np.clip(_oww_chunk * _oww_gain, -1.0, 1.0)
            if app._wake_detector.detected(_oww_chunk):
                app._oww_wake_detected = True
                app._wake_detector.reset()

        # ── Speech accumulation ───────────────────────────────────────────────
        if is_speech:
            speech_onset   = not app.is_speaking
            app.is_speaking   = True
            app.silence_start = None

            if speech_onset:
                # Ring prebuffer rewind: replaces the legacy _prebuffer deque drain.
                # Rewind PREBUFFER_FRAMES and re-read them into the utterance buffer.
                # The current frame (raw_chunk) is included in the re-read since the
                # cursor was at this position before the rewind. Do NOT append
                # raw_chunk again after — that would double the onset frame.
                # (ARC audit: double-appending of speech onset frame)
                self._reader.rewind(PREBUFFER_FRAMES)
                for _ in range(PREBUFFER_FRAMES):
                    pb_frame = self._reader.read_next()
                    if pb_frame is EMPTY:
                        break
                    pb_pcm = pb_frame.pcm.astype(np.float32) / 32767.0
                    self._utterance_frames.append(pb_pcm)
                    self._buffer_rms_history.append(
                        float(np.sqrt(np.mean(pb_pcm ** 2)))
                    )
                if self._utterance_frames:
                    logger.debug(f"[PRE] Prepended {len(self._utterance_frames) * FRAME_MS}ms pre-buffer to wake onset")
            else:
                # Non-onset speech frame — append normally
                self._utterance_frames.append(raw_chunk)
                self._buffer_rms_history.append(rms)

            # Stuck-buffer detector (same as legacy callback).
            # Skip during command mode — a deliberate pause between commands
            # looks like a flat signal and would wrongly clear the buffer.
            if (app.app_state == 'asleep'
                    and not app.command_mode_active
                    and len(self._buffer_rms_history) >= 30):
                recent   = self._buffer_rms_history[-30:]
                variance = float(np.var(recent))
                if variance < 0.0001:
                    buf_s = len(self._buffer_rms_history) * (FRAME_MS / 1000.0)
                    logger.debug(f"[CAP] Stuck buffer ({buf_s:.1f}s, var={variance:.6f}) — discarding")
                    self._utterance_frames   = []
                    self._buffer_rms_history = []
                    app.is_speaking   = False
                    app.silence_start = None
                    try:
                        app._vad_reset()
                    except Exception as e:
                        logger.debug(f"_vad_reset failed after stuck-buffer discard: {e}")
                    return

            # Hard buffer cap (same as legacy callback)
            buffer_s = len(self._utterance_frames) * (FRAME_MS / 1000.0)
            if buffer_s >= 7.0 and app.app_state not in ('long_dictation', 'quick_dictation', 'wake_session'):
                logger.debug(f"[CAP] Buffer at {buffer_s:.1f}s cap — discarding (likely noise/echo)")
                self._utterance_frames   = []
                self._buffer_rms_history = []
                app.is_speaking   = False
                app.silence_start = None
                try:
                    app._vad_reset()
                except Exception as e:
                    logger.debug(f"_vad_reset failed after hard buffer cap: {e}")
                return

        else:
            # Silence
            if app.is_speaking:
                self._utterance_frames.append(raw_chunk)
                self._buffer_rms_history.append(rms)

                if app.silence_start is None:
                    app.silence_start = time.time()
                elif time.time() - app.silence_start >= silence_threshold:
                    # Enough silence — flush if sufficient speech
                    speech_s = len(self._utterance_frames) * (FRAME_MS / 1000.0)
                    if speech_s >= min_speech:
                        buffer_copy = list(self._utterance_frames)
                    else:
                        buffer_copy = None
                    self._utterance_frames   = []
                    self._buffer_rms_history = []
                    app.is_speaking   = False
                    app.silence_start = None

                    if buffer_copy is not None:
                        self._flush(buffer_copy)

    def _flush(self, buffer_copy: list) -> None:
        """Dispatch utterance to process_wake_word_buffer, respecting OWW gate."""
        app = self._app

        # AI command mode: route utterance to the AI resolver queue.
        if self._is_ai_cmd_mode(app):
            threading.Thread(
                target=app._handle_ai_command_utterance,
                args=(buffer_copy, SAMPLE_RATE),
                daemon=True,
                name='ai-cmd-utt',
            ).start()
            return

        # Toggle command mode: bypass OWW gate and execute as a single command
        # utterance.  The WakeConsumer re-arms automatically for the next
        # utterance; no external re-arm call is needed.
        if self._is_toggle_cmd(app):
            threading.Thread(
                target=app._handle_command_mode_utterance,
                args=(buffer_copy, SAMPLE_RATE),
                daemon=True,
                name='cmd-utt',
            ).start()
            return

        _has_wake_targets = any(
            t.get('enabled', True)
            for t in getattr(app, 'config', {}).get('wake_targets', [])
        )
        _oww_gated = (
            app._wake_detector is not None
            and app._wake_detector.is_available
            and app.app_state == 'asleep'
            and not app.wake_word_triggered
            and not _has_wake_targets
        )
        if _oww_gated and not app._oww_wake_detected:
            if app._wake_detector is not None:
                app._wake_detector.reset()
            return

        app._oww_wake_detected = False

        if app.app_state == 'long_dictation':
            with app._dictation_finalize_lock:
                app._pending_transcriptions += 1
            threading.Thread(
                target=app._process_wake_word_buffer_tracked,
                args=(buffer_copy, SAMPLE_RATE),
                daemon=True,
            ).start()
        else:
            threading.Thread(
                target=app.process_wake_word_buffer,
                args=(buffer_copy, SAMPLE_RATE),
                daemon=True,
            ).start()

    def __repr__(self) -> str:
        return (
            f"WakeConsumer(running={self._running}, "
            f"frames={len(self._utterance_frames)})"
        )
