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

import collections
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
from samsara.runtime import thread_registry

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

        # FIX 1 (2026-07-10 hotkey word-loss investigation): tracks the
        # hotkey-deafness suppression state so _process_frame logs only on
        # the ENGAGE/RELEASE transitions, not every 100ms frame.
        self._hotkey_suppressed_last: bool = False

        # Toggle-session utterances are captured serially on this poll thread
        # but transcribed asynchronously. A FIFO drain prevents a fast next
        # utterance from being silently dropped while Whisper handles the
        # previous one, and preserves capture/paste order.
        self._toggle_queue = collections.deque()
        self._toggle_queue_lock = threading.Lock()
        self._toggle_worker_active = False
        self._last_toggle_activity_touch = 0.0

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
        self._thread = thread_registry.spawn(
            "wake-consumer", self._poll_loop, daemon=True
        )

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

    def discard_stale_wake_utterance(self) -> None:
        """FIX 1 (2026-07-10 hotkey word-loss investigation): discard any
        in-progress WAKE-WORD-mode utterance -- never flush it -- the
        moment a hotkey recording starts. Called proactively from
        dictation.py's start_recording() at the same point _hotkey_
        recording flips True, rather than waiting for the next poll frame,
        so a frozen/stale wake-mode buffer never sits around to be
        (incorrectly) flushed once hotkey recording ends.

        No-ops when toggle-command-mode or AI-command-mode is servicing
        the in-progress utterance instead -- those must keep running (see
        _process_frame's hotkey-deafness guard) and must NOT be discarded;
        e.g. a hotkey press mid-DICTATE-chunk must not eat the chunk."""
        app = self._app
        if self._is_toggle_cmd(app) or self._is_ai_cmd_mode(app):
            return
        if self._utterance_frames or app.is_speaking:
            logger.debug("[SEAM] Discarding in-progress wake-mode utterance "
                         "-- hotkey recording just started")
        self.abort_utterance()

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

    @classmethod
    def _is_toggle_dictate(cls, app) -> bool:
        manager = getattr(app, '_session_mode_manager', None)
        return (
            cls._is_toggle_cmd(app)
            and manager is not None
            and manager.mode is SessionMode.DICTATE
        )

    @classmethod
    def _hard_cap_applies(cls, app) -> bool:
        """Whether the 7-second noise/echo discard applies to this lane."""
        return (
            app.app_state not in ('long_dictation', 'quick_dictation', 'wake_session')
            and not cls._is_toggle_dictate(app)
        )

    def _enqueue_toggle_utterance(self, buffer_copy: list) -> None:
        """Append one captured utterance and ensure one FIFO worker drains it."""
        # Capture itself is session activity. Refresh before queueing so a
        # slow CPU transcription cannot time the session out underneath
        # speech that has already arrived.
        touch_activity = getattr(self._app, '_touch_session_activity', None)
        if touch_activity is not None:
            touch_activity()

        with self._toggle_queue_lock:
            self._toggle_queue.append(buffer_copy)
            if self._toggle_worker_active:
                return
            self._toggle_worker_active = True

        thread_registry.spawn(
            'cmd-utt-queue', self._drain_toggle_utterances, daemon=True,
        )

    def _touch_toggle_speech_activity(self, now: float) -> None:
        """Keep inactivity tied to actual silence during sustained speech."""
        if not self._is_toggle_cmd(self._app):
            return
        if now - self._last_toggle_activity_touch < 1.0:
            return
        self._last_toggle_activity_touch = now
        touch_activity = getattr(self._app, '_touch_session_activity', None)
        if touch_activity is not None:
            touch_activity()

    def _drain_toggle_utterances(self) -> None:
        while True:
            with self._toggle_queue_lock:
                if not self._toggle_queue:
                    self._toggle_worker_active = False
                    return
                buffer_copy = self._toggle_queue.popleft()

            if not self._is_toggle_cmd(self._app):
                logger.info('[CMD-UTT] Session ended -- discarding queued post-exit utterance')
                continue
            try:
                self._app._handle_command_mode_utterance(buffer_copy, SAMPLE_RATE)
            except Exception as exc:
                logger.exception(f'[CMD-UTT] FIFO worker error: {exc}')

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

        # ── FIX 1 (2026-07-10 hotkey word-loss investigation): full
        # deafness during hotkey recording ─────────────────────────────────
        # Supersedes the old pre-onset-only "Hotkey-recording suppression"
        # guard (formerly here, inside the `if not app.is_speaking:` block
        # below) -- that guard stopped applying the moment app.is_speaking
        # went True, so once WakeConsumer detected its OWN speech onset
        # mid-hotkey-hold it kept running a full, separate utterance
        # (prebuffer prepend, VAD, buffering, eventual flush) concurrently
        # with the hotkey capture -- confirmed via the [SEAM] diagnostic
        # added in commit 0e837ba. That utterance also contends for the
        # dedicated app._vad_model / app._vad_lock used by the hotkey path's
        # contiguous-speech gate. The current ONNX wrapper is stateless
        # between calls and the shared lock serializes inference, but running
        # a redundant wake utterance here still wastes work and delays gates.
        #
        # Toggle-command-mode and AI-command-mode are explicitly exempted:
        # both are active, user-initiated listening sessions that must
        # keep servicing regardless of a concurrent hotkey press (a hotkey
        # press mid-DICTATE-chunk must not eat the chunk). The always-live
        # global abort phrase lives entirely inside SessionModeManager.
        # dispatch_utterance(), reached only via _flush() -> app._handle_
        # command_mode_utterance(), which only ever fires from THIS same
        # poll loop's toggle-command branch -- so exempting
        # _is_toggle_cmd(app) here is what keeps global abort reachable
        # while a hotkey is held. AI-command-mode gets the same exemption
        # for consistency with the poll loop's own outer gate (top of
        # _poll_loop), which already treats it as an equally "actively
        # listening" state.
        hotkey_suppress = (
            app._hotkey_recording
            and not self._is_toggle_cmd(app)
            and not self._is_ai_cmd_mode(app)
        )
        if hotkey_suppress:
            if not self._hotkey_suppressed_last:
                logger.debug(
                    "[SEAM] WakeConsumer suppression ENGAGED (hotkey recording "
                    "active) -- wake-word speech detection fully skipped "
                    "(no RMS/VAD/OWW/onset/buffering) until hotkey recording ends"
                )
                self._hotkey_suppressed_last = True
            return   # cursor already advanced (frame already read in _poll_loop)
        if self._hotkey_suppressed_last:
            logger.debug("[SEAM] WakeConsumer suppression RELEASED (hotkey recording ended)")
            self._hotkey_suppressed_last = False

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
                # Manual-commit DICTATE uses silence only to produce internal
                # transcript chunks; it no longer pastes on this boundary. A
                # short gap therefore makes the sole-word "end" commit fast
                # without forcing the speaker to race natural pauses.
                silence_threshold = cm_cfg.get('dictate_utterance_silence_s', 0.65)
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
            self._touch_toggle_speech_activity(time.monotonic())
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
                prebuffer_frames = PREBUFFER_FRAMES
                if self._is_toggle_dictate(app):
                    # Never rewind farther than the silence boundary that
                    # separated two staged chunks. Otherwise a short manual-
                    # commit gap can re-include the previous chunk's tail and
                    # create duplicated words.
                    prebuffer_frames = min(
                        PREBUFFER_FRAMES,
                        max(2, int(silence_threshold * 1000 / FRAME_MS)),
                    )
                self._reader.rewind(prebuffer_frames)
                for _ in range(prebuffer_frames):
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
                # Diagnostic (2026-07-10 hotkey word-loss investigation,
                # updated by FIX 1): this branch is now UNREACHABLE while a
                # plain hotkey recording is active -- _process_frame's
                # top-level hotkey-deafness guard returns before speech
                # onset is ever evaluated. The only way to reach this with
                # _hotkey_recording=True is the intentional toggle-command-
                # mode/AI-command-mode exemption (that servicing must keep
                # running concurrently with a hotkey press). The stateless
                # ONNX model's whole inference call is serialized by
                # app._vad_lock, so this remaining overlap can briefly BLOCK
                # the gate's scan (or vice versa) but cannot interleave two
                # runs on the dedicated InferenceSession.
                if getattr(app, '_hotkey_recording', False):
                    logger.debug(
                        "[SEAM] Wake-consumer speech onset occurred WHILE "
                        "_hotkey_recording=True -- reached only via the "
                        "toggle-command-mode/AI-command-mode exemption "
                        "(see FIX 1). VAD lock contention possible, "
                        "inference serialized by the shared VAD lock."
                    )
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
            if buffer_s >= 7.0 and self._hard_cap_applies(app):
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
            thread_registry.spawn(
                'ai-cmd-utt',
                app._handle_ai_command_utterance,
                args=(buffer_copy, SAMPLE_RATE),
                daemon=True,
            )
            return

        # Toggle command mode: bypass OWW gate and execute as a single command
        # utterance.  The WakeConsumer re-arms automatically for the next
        # utterance; no external re-arm call is needed.
        if self._is_toggle_cmd(app):
            self._enqueue_toggle_utterance(buffer_copy)
            return

        _has_wake_profiles = any(
            t.get('enabled', True)
            for t in getattr(app, 'config', {}).get('wake_profiles', [])
        )
        _primary_oww_eligible = (
            app._wake_detector is not None
            and app._wake_detector.is_available
            and app.app_state == 'asleep'
            and not app.wake_word_triggered
        )
        _primary_oww_hit = bool(
            _primary_oww_eligible and app._oww_wake_detected
        )
        # With no profile fallbacks, the primary OWW model remains a strict
        # pre-filter. Enabled profiles must still reach Whisper when Jarvis did
        # not fire, because those profiles may have no OWW model of their own.
        if _primary_oww_eligible and not _primary_oww_hit and not _has_wake_profiles:
            if app._wake_detector is not None:
                app._wake_detector.reset()
            return

        # Preserve the detector result across the async dispatch. An OWW hit
        # has already supplied the cheap energy/shape prefilter, so the legacy
        # RMS gate must not reject the same buffer before Whisper can confirm
        # the phrase. Whisper confirmation remains mandatory.
        # A primary OWW hit remains authoritative even when wake profiles are
        # enabled. Previously profile presence forced this False, so a 0.99
        # Jarvis detection was subsequently discarded by the adaptive RMS
        # gate. Profiles only relax the no-hit path; they must not erase a hit.
        oww_confirmed = _primary_oww_hit
        app._oww_wake_detected = False

        if app.app_state == 'long_dictation':
            with app._dictation_finalize_lock:
                app._pending_transcriptions += 1
            thread_registry.spawn(
                "wake_consumer._process_wake_word_buffer_tracked",
                app._process_wake_word_buffer_tracked,
                args=(buffer_copy, SAMPLE_RATE),
                daemon=True,
            )
        else:
            thread_registry.spawn(
                "wake_consumer.process_wake_word_buffer",
                app.process_wake_word_buffer,
                args=(buffer_copy, SAMPLE_RATE),
                kwargs={"oww_confirmed": oww_confirmed},
                daemon=True,
            )

    def __repr__(self) -> str:
        return (
            f"WakeConsumer(running={self._running}, "
            f"frames={len(self._utterance_frames)})"
        )
