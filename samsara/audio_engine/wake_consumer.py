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
etc.) is read/written on this thread, same as the old PortAudio callback
thread. The policy invariants are unchanged.

== Epoch change ==

A device_epoch change while the poll loop is running means the audio
stream was interrupted. The consumer aborts the current utterance,
resets speech state, and continues from the new epoch.
"""

import collections
import logging
import math
import threading
import time
from dataclasses import dataclass

import numpy as np

from .frame import FRAME_MS, PREBUFFER_FRAMES, SAMPLE_RATE
from .ring import EMPTY
from .wake_prefilter import oww_prefilter

from samsara.constants import (
    DEFAULT_MIN_SPEECH_DURATION,
    DEFAULT_SPEECH_THRESHOLD,
    WAKE_DETECTION_SILENCE,
    WAKE_SPEECH_THRESHOLD_CAP,
)
from samsara.session_modes import SessionMode
from samsara.log import get_logger
from samsara.runtime import thread_registry
from samsara import flight_recorder

logger = get_logger(__name__)

# Streaming-preview tail window (dictation.py's DictatePreviewSession): the
# toggle-DICTATE lane is exempted from the 7s hard cap (_hard_cap_applies),
# so an in-progress buffer can grow arbitrarily long while the user keeps
# talking. Decoding only the most recent slice keeps each preview tick's
# Whisper cost bounded regardless of utterance length.
_PREVIEW_TAIL_S = 8.0

# Ava command session half-duplex guard (2026-07-23 G3 live-test finding):
# how long after AudioCoordinator leaves SPEAKING to keep the session fully
# deaf. Covers playback-engine tail latency (buffered audio still audible
# briefly after the coordinator's own state transition) so the mic doesn't
# pick up the last of Ava's own voice as the start of the next utterance.
_AVA_CMD_TTS_TAIL_S = 0.3
_THREAD_JOIN_TIMEOUT_S = 2.0

# ── Fatal classification (queue 109, Astra F6) ───────────────────────────────
# A fatal in the poll loop ends hands-free listening. For a user who cannot
# type, that is the loss of their input method, so the two things that happen
# next -- what the app tells them, and whether it tries again by itself -- are
# decided HERE rather than at the call site, where each caller would guess
# differently.
FATAL_TRANSIENT = "transient"
FATAL_PERMANENT = "permanent"

#: The waits before each automatic restart, so the cap IS the schedule: three
#: automatic restarts for one transient fault, then the app stops and asks.
#: Backing off matters because a device that has just gone (a USB mic pulled,
#: an exclusive-mode grab by another app) is usually still gone 2 s later.
FATAL_RETRY_DELAYS_S = (2.0, 5.0, 15.0)
FATAL_RETRY_CAP = len(FATAL_RETRY_DELAYS_S)

#: Never retried, and named rather than inferred. Restarting after one of
#: these re-enters the same code with the same inputs and raises again on the
#: very next frame: an automatic retry would be an infinite loop that also
#: hides the bug behind a restart storm. They are programming errors; the
#: recovery for them is a fix, and in the meantime an honest explanation and a
#: restart the USER asks for.
_PROGRAMMING_ERRORS = (
    TypeError, AttributeError, NameError, KeyError, IndexError, ValueError,
    ZeroDivisionError, RuntimeError, AssertionError, ImportError, NotImplementedError,
)

#: Reason sentences. Plain words, no exception text: the class name and the
#: message go in the evidence line, which is checkable without being the
#: headline a user has to parse.
FATAL_REASONS = {
    FATAL_TRANSIENT: "Hands-free stopped: the microphone stopped feeding it.",
    FATAL_PERMANENT: "Hands-free stopped after an internal error.",
}


def _transient_types() -> tuple:
    """Exception types a restart can plausibly clear.

    OSError covers the device and stream failures that actually end this loop
    (it is also the base of TimeoutError and the connection errors), and
    PortAudioError is added when sounddevice is importable -- it is not an
    OSError, and it is the single most likely fatal on this path.
    """
    types = [OSError]
    try:
        import sounddevice                                    # noqa: PLC0415
        error = getattr(sounddevice, "PortAudioError", None)
        if isinstance(error, type) and issubclass(error, BaseException):
            types.append(error)
    except Exception:                                          # pragma: no cover
        pass
    return tuple(types)


def classify_fatal(exc) -> str:
    """FATAL_TRANSIENT or FATAL_PERMANENT for one poll-loop exception.

    The programming errors are checked FIRST and by name, so the
    classification cannot drift if an exception type is ever made to inherit
    from OSError: anything not positively recognised as a device failure is
    permanent, which is the safe direction -- a permanent classification
    costs one manual restart, a wrong transient one costs a retry loop.
    """
    if isinstance(exc, _PROGRAMMING_ERRORS):
        return FATAL_PERMANENT
    if isinstance(exc, _transient_types()):
        return FATAL_TRANSIENT
    return FATAL_PERMANENT


@dataclass(frozen=True)
class HandsFreeFault:
    """What the app remembers about a fatal until listening is back.

    Persistent on purpose: it is cleared by a SUCCESSFUL restart and by
    nothing else. A chip that expires, or a beep, leaves a user who cannot
    type with no record of what happened -- which is the whole of F6.
    """
    reason: str                   # one sentence, for the user
    classification: str           # FATAL_TRANSIENT | FATAL_PERMANENT
    exception: str                # "PortAudioError: Device unavailable"
    at: float                     # time.time()
    attempts: int = 0             # automatic restarts already spent
    retrying: bool = False        # another automatic restart is scheduled

    @property
    def evidence(self) -> str:
        spent = f"{self.attempts} of {FATAL_RETRY_CAP} automatic restarts used"
        return (f"the wake listener stopped with {self.exception} "
                f"({self.classification}; {spent})")


def fault_from(exc, *, at: float, attempts: int = 0, retrying: bool = False) -> HandsFreeFault:
    """The fault record for one poll-loop exception."""
    classification = classify_fatal(exc)
    detail = str(exc).strip()
    name = type(exc).__name__
    return HandsFreeFault(
        reason=FATAL_REASONS[classification],
        classification=classification,
        exception=f"{name}: {detail}" if detail else name,
        at=at, attempts=attempts, retrying=retrying,
    )


def wake_session_policy(config):
    """Read session policy without changing legacy phrase-string configs."""
    legacy = config.get('wake_word_config', {})
    settings = dict(legacy.get('session', {}))
    wake_word = config.get('wake_word', {})
    if isinstance(wake_word, dict):
        settings.update(wake_word.get('session', {}))
    policy = settings.get('prebuffer_policy', 'discard')
    if policy not in ('discard', 'keep'):
        policy = 'discard'
    result = {'prebuffer_policy': policy}
    for key, default in (('post_wake_guard_ms', 150.0), ('max_session_s', 20.0),
                         ('inactivity_timeout_s', 10.0)):
        try:
            value = float(settings.get(key, default))
        except (TypeError, ValueError):
            value = default
        if not math.isfinite(value) or value < 0 or (key != 'post_wake_guard_ms' and value == 0):
            value = default
        result[key] = value
    return result


# Queue 44 (2026-09-15): prebuffer_policy=discard dropped the whole 1.5 s
# rewind on EVERY onset of a post-wake capture, so a late Silero onset left the
# retained buffer starting mid-word (owner WAVs: 5 of 9 in-session buffers;
# offline replay of the owner's voice: first word changed in 16/20 normal and
# 13/15 soft clips, 1/20 and 2/15 with the rewind kept). With a session open the
# rewind is kept, but never earlier than confirmed_at + post_wake_guard_ms, so
# audio from before the wake confirmation still cannot leak into the capture.
# False restores the old discard exactly. Read with the same legacy/canonical
# merge as wake_session_policy; this is its only read site.
RETAIN_PREBUFFER_IN_SESSION_KEY = 'retain_prebuffer_in_session'
RETAIN_PREBUFFER_IN_SESSION_DEFAULT = True


def retain_prebuffer_in_session(config) -> bool:
    legacy = config.get('wake_word_config', {})
    settings = dict(legacy.get('session', {})) if isinstance(legacy, dict) else {}
    wake_word = config.get('wake_word', {})
    if isinstance(wake_word, dict):
        settings.update(wake_word.get('session', {}))
    value = settings.get(RETAIN_PREBUFFER_IN_SESSION_KEY, RETAIN_PREBUFFER_IN_SESSION_DEFAULT)
    return value if isinstance(value, bool) else RETAIN_PREBUFFER_IN_SESSION_DEFAULT


def _samples_before(t_capture, n_samples, not_before):
    """Samples of a block (t_capture = end of block) captured before not_before."""
    frame_start = t_capture - n_samples / SAMPLE_RATE
    return min(n_samples, max(0, math.ceil((not_before - frame_start) * SAMPLE_RATE)))


# In-session near-silence floor (2026-09-14 live-log incident). Inside an
# open wake session the adaptive RMS gate is not applied (see
# in_session_rms_gate); only whole-buffer RMS below this value is skipped.
# The owner's real in-session speech buffers measured 0.0042-0.0105 RMS, and
# a buffer's RMS is diluted by the trailing silence window that closes it, so
# quieter genuine speech is expected. 0.002 is ~2.1x (-6.4 dB) below the
# quietest observed speech buffer and equals dictation.py's _ABS_FLOOR_MIN
# (the asleep gate's own "zeroed/DC buffer" floor), so the in-session rule
# is never stricter than the absolute component of the asleep gate.
IN_SESSION_NEAR_SILENCE_RMS = 0.002

# Config key (wake_word_config.audio.*, default True via config_defaults):
# False restores the adaptive gate inside open sessions without a rebuild.
BYPASS_ADAPTIVE_GATE_IN_SESSION_KEY = 'wake_word_config.audio.bypass_adaptive_gate_in_session'


def wake_capture_session_open(app) -> bool:
    """True when the user has already deliberately woken the app: an open
    wake session, quick/long dictation, or the post-wake command window.
    Same definition WakeConsumer._post_wake_admission uses for post-wake
    capture."""
    return (
        getattr(app, 'app_state', None) in ('wake_session', 'quick_dictation', 'long_dictation')
        or bool(getattr(app, 'wake_word_triggered', False))
    )


def in_session_rms_gate(audio_rms: float):
    """Skip decision for a buffer captured inside an open session.

    Returns (skip, threshold, rule). The adaptive ambient-floor gate exists to
    stop background speech waking an ASLEEP app; once a session is open it can
    only lose the user's words (including end/cancel words), so only genuinely
    empty buffers are skipped here.
    """
    threshold = IN_SESSION_NEAR_SILENCE_RMS
    return audio_rms < threshold, threshold, 'in_session_near_silence'


class WakeStopResult(list):
    """Buffered frames plus explicit join status; compatible with existing flush callers."""
    def __init__(self, frames=(), *, stopped: bool):
        super().__init__(frames)
        self.stopped = stopped


class WakeConsumer:
    """Polls the ACE ring and runs the full wake word policy loop.

    Args:
        engine: AudioCaptureEngine.
        app:    DictationApp — policy state lives here.
        on_fatal: Optional callback(exc) after fatal cleanup on the poll thread.
                  Production passes DictationApp._on_wake_consumer_fatal, which
                  records a persistent fault and schedules the retries a
                  transient failure is allowed (queue 109). The bare
                  app.play_sound('error') fallback remains for test doubles and
                  is NOT recovery: a beep is all a user got before 109.
    """

    def __init__(self, engine, app, *, on_fatal=None) -> None:
        self._engine  = engine
        self._app     = app
        self._reader  = engine.register_consumer("wake")
        self._running = False
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self.on_fatal = on_fatal  # Optional callback(exc); defaults to the app error earcon.
        self._fatal_reported = False
        self._frame_log_times = {}

        # Local utterance buffer (replaces app.speech_buffer for wake path)
        self._utterance_frames: list = []   # float32 arrays at SAMPLE_RATE
        self._buffer_rms_history: list = []
        self._last_epoch: int | None  = None

        # FIX 1 (2026-07-10 hotkey word-loss investigation): tracks the
        # hotkey-deafness suppression state so _process_frame logs only on
        # the ENGAGE/RELEASE transitions, not every 100ms frame.
        self._hotkey_suppressed_last: bool = False

        # Ava command session half-duplex guard (2026-07-23 G3 live-test
        # finding) -- see _AVA_CMD_TTS_TAIL_S and _process_frame.
        self._ava_cmd_tts_suppressed_last: bool = False
        self._ava_cmd_tts_was_speaking: bool = False
        self._ava_cmd_tts_speaking_end: "float | None" = None

        # Toggle-session utterances are captured serially on this poll thread
        # but transcribed asynchronously. A FIFO drain prevents a fast next
        # utterance from being silently dropped while Whisper handles the
        # previous one, and preserves capture/paste order.
        self._toggle_queue = collections.deque()
        self._toggle_queue_lock = threading.Lock()
        self._toggle_worker_active = False
        self._silero_was_speech = False
        self._rms_was_speech = None
        self._rms_fallback_warned = False
        self._await_wake_onset = False
        self._wake_admission = None
        self._guard_discarded_samples = 0
        self._hands_free_capture_duck_token: int | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """Whether the poll thread is servicing frames right now.

        Public because Home has to answer "is hands-free actually listening?"
        and the only truthful answer is this flag (queue 109, F12): config
        says what was asked for, this says what is happening. It goes False in
        stop(), in _handle_fatal and in _poll_loop's finally, so it cannot
        stay True on a thread that has died.
        """
        return self._running

    def start(self) -> bool:
        """Start a fresh listener; refuse while any previous poller is alive."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                logger.error("[ACE] Wake start refused: thread %s is still alive", self._thread.name)
                return False
            self._utterance_frames = []
            self._buffer_rms_history = []
            self._last_epoch = None
            self._await_wake_onset = False
            self._silero_was_speech = False
            self._rms_was_speech = None
            self._wake_admission = None
            self._guard_discarded_samples = 0
            self._app._wake_rearm_needs_onset = False
            self._app.is_speaking = False
            self._app.silence_start = None
            self._fatal_reported = False
            self._stop_event = threading.Event()
            self._reader.snap_to_head()
            self._running = True
            self._thread = thread_registry.spawn(
                "wake-consumer", self._poll_loop, args=(self._stop_event,), daemon=True
            )
            return True

    def stop(self) -> WakeStopResult:
        """Return buffered frames and .stopped; a timeout exposes no partial audio.

        The result remains a list for the app's existing final-flush caller.
        Inspect .stopped for join status, independently of whether audio exists.
        """
        with self._lifecycle_lock:
            self._running = False
            self._stop_event.set()
            thread = self._thread
            if thread is not None:
                if thread is threading.current_thread():
                    return WakeStopResult(stopped=False)
                thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
                if thread.is_alive():
                    logger.error("[ACE] Wake stop incomplete: thread %s is still alive", thread.name)
                    return WakeStopResult(stopped=False)
                self._thread = None
            self._close_capture_duck()
            self._app.is_speaking = False
            self._app.silence_start = None
            remaining, self._utterance_frames = self._utterance_frames, []
            self._buffer_rms_history = []
            return WakeStopResult(remaining, stopped=True)

    def _close_capture_duck(self) -> None:
        token, self._hands_free_capture_duck_token = self._hands_free_capture_duck_token, None
        if token is not None:
            self._close_hands_free_duck_safe(self._app, token)

    def deactivate(self) -> None:
        """Stop and unregister from engine on app shutdown."""
        if not self.stop().stopped:
            return
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
            self._log_frame(logging.DEBUG, 'abort_vad_reset', "abort_utterance: vad reset failed: %s", e)
        # Hands-free capture-window ducking: device death mid-capture is a
        # discard, same as a too-short buffer -- close the window here
        # (also covers discard_stale_wake_utterance() below, which
        # delegates to this method).
        close_token = self._hands_free_capture_duck_token
        self._close_hands_free_duck_safe(app, close_token)
        self._hands_free_capture_duck_token = None

    def flush_utterance(self) -> bool:
        """Force whatever audio is currently buffered to dispatch now,
        bypassing the VAD silence threshold -- called by long_dictation's
        hard-cap timer so audio that's mid-capture when the cap fires isn't
        silently dropped (see dictation.py:_finalize_dictation_hardcap).

        Unlike abort_utterance(), this does not touch app.is_speaking or
        app.silence_start: capture continues seamlessly if the user is
        still talking, only the audio accumulated so far is flushed. Safe
        to call from any thread -- the tuple-assignment swap of
        _utterance_frames is the same atomic-under-the-GIL pattern stop()
        already relies on, rather than a lock on this consumer's hot path.

        Returns True if a buffer was dispatched, False if nothing to flush.
        """
        buffer_copy, self._utterance_frames = self._utterance_frames, []
        self._buffer_rms_history = []
        if not buffer_copy:
            return False
        token, self._hands_free_capture_duck_token = self._hands_free_capture_duck_token, None
        self._flush(buffer_copy, token)
        return True

    @staticmethod
    def _open_hands_free_duck_safe(app) -> int | None:
        """Best-effort call to dictation.py's _open_hands_free_capture_
        duck -- see _close_hands_free_duck_safe's docstring for why this
        is a safe/no-op-tolerant accessor rather than a direct call."""
        open_duck = getattr(app, '_open_hands_free_capture_duck', None)
        if open_duck is not None:
            try:
                return open_duck()
            except Exception as exc:
                WakeConsumer._log_duck_failure(app, 'open', exc)
        return None

    @staticmethod
    def _close_hands_free_duck_safe(app, owner_token: int | None = None) -> None:
        """Best-effort call to dictation.py's _close_hands_free_capture_
        duck -- a no-op if hands-free ducking isn't wired (e.g. a minimal
        test double for `app`) or disabled in config. Centralized here so
        every capture-window-close call site (silence-timeout discard,
        abort_utterance, _flush's dispatch wrapper, the toggle-mode FIFO
        worker) shares one safe accessor instead of repeating the
        getattr(...) dance."""
        if owner_token is None:
            logger.debug('[DUCK] Ignoring capture duck close without an owner')
            return
        close_duck = getattr(app, '_close_hands_free_capture_duck', None)
        if close_duck is not None:
            try:
                close_duck(owner_token)
            except Exception as exc:
                WakeConsumer._log_duck_failure(app, 'close', exc)

    @staticmethod
    def _log_duck_failure(app, operation, exc):
        key = f'_wake_duck_{operation}_last_error_log'
        now = time.monotonic()
        if now - getattr(app, key, float('-inf')) >= 5.0:
            setattr(app, key, now)
            logger.debug("[WAKE] Capture duck %s failed: %s", operation, exc)

    def discard_stale_wake_utterance(self) -> None:
        """FIX 1 (2026-07-10 hotkey word-loss investigation): discard any
        in-progress WAKE-WORD-mode utterance -- never flush it -- the
        moment a hotkey recording starts. Called proactively from
        dictation.py's start_recording() at the same point _hotkey_
        recording flips True, rather than waiting for the next poll frame,
        so a frozen/stale wake-mode buffer never sits around to be
        (incorrectly) flushed once hotkey recording ends.

        No-ops when toggle-command-mode is servicing the in-progress
        utterance instead AND hands_free.suspend_on_hold is disabled --
        that reproduces the original exemption exactly (see
        _process_frame's hotkey-deafness guard) for the "off" comparison
        setting.

        hands_free.suspend_on_hold (2026-09-11, default true): the
        exemption above used to be unconditional, which is exactly the
        double-capture bug this fixes -- a hold-to-record utterance and
        the toggle lane's own in-progress buffer both captured the same
        words, one going out via the hotkey path's paste and the other via
        the toggle session's own eventual flush/dispatch. With the setting
        on (default), toggle-command-mode is treated the SAME as AI-
        command-mode below: any utterance it was mid-capturing is
        discarded now, immediately, rather than sitting frozen through the
        hold and resuming with stale + fresh audio mixed together
        (matching AI-command-mode's own 2026-07-19 nag-incident fix and
        rationale, extended here to toggle mode). The suspended lane
        resumes cleanly from the CURRENT ring position once the hold ends
        -- _process_frame never rewinds the reader, it simply stops
        reading the accumulated-frames list while _hotkey_recording holds
        (see the hotkey_suppress block there).

        AI-command-mode is NOT exempted here at all (2026-07-19 nag
        incident fix, matching its removal from the hotkey_suppress gate):
        any utterance it was mid-capturing gets discarded like plain
        wake-word mode's would, rather than sitting frozen through the
        hotkey hold and then resuming with stale + fresh audio mixed
        together once the hold ends."""
        app = self._app
        if self._is_toggle_cmd(app) and not self._suspend_on_hold_enabled(app):
            return
        if self._utterance_frames or app.is_speaking:
            logger.debug("[SEAM] Discarding in-progress wake-mode utterance "
                         "-- hotkey recording just started")
        self.abort_utterance()

    def snapshot_dictate_preview_audio(self) -> "np.ndarray | None":
        """Thread-safe, read-only snapshot of the in-progress toggle-DICTATE
        utterance for the streaming-preview overlay (dictation.py's
        DictatePreviewSession, called from its own preview-tick thread --
        NEVER from this poll thread).

        Returns None outside the DICTATE lane (see _is_toggle_dictate) or
        when nothing has been buffered yet. Only the most recent
        _PREVIEW_TAIL_S seconds are returned -- the DICTATE lane is exempt
        from the 7s hard cap, so the buffer can otherwise grow unbounded.

        `self._utterance_frames` is a plain list. This poll thread's own
        _process_frame mutates it in place via `.append()`; stop() and
        flush_utterance() may instead rebind it to a fresh list from another
        thread. Never locked, by design (adding a lock here would risk
        stalling the audio hot path) -- safety comes from `.append()` and
        `list(...)` each being individually atomic under the GIL, NOT from
        avoiding in-place mutation (it happens every frame). So this read
        can only ever observe the list fully-before or fully-after one such
        mutation, never a torn one. Elements themselves are float32 arrays
        produced fresh per frame via `.astype()` (a copy, not a ring view),
        so holding them past this call is safe.
        """
        app = self._app
        if not self._is_toggle_dictate(app):
            return None
        frames = list(self._utterance_frames)
        if not frames:
            return None
        tail_frame_count = max(1, int(_PREVIEW_TAIL_S * 1000 / FRAME_MS))
        if len(frames) > tail_frame_count:
            frames = frames[-tail_frame_count:]
        return np.concatenate(frames)

    # ── Poll loop ─────────────────────────────────────────────────────────────

    @staticmethod
    def _is_toggle_cmd(app) -> bool:
        """True when toggle command mode is active (not wake-word or hold mode)."""
        return (
            getattr(app, 'command_mode_active', False)
            and app.config.get('command_mode', {}).get('mode', 'hold') == 'toggle'
        )

    @staticmethod
    def _suspend_on_hold_enabled(app) -> bool:
        """hands_free.suspend_on_hold (default true): whether a hotkey hold
        suspends the toggle-command-mode lane (see the hotkey_suppress
        block below and discard_stale_wake_utterance) instead of the old
        always-exempt behaviour. False reproduces today's/pre-fix
        behaviour exactly, for comparison."""
        return bool(app.config.get('hands_free', {}).get('suspend_on_hold', True))

    @staticmethod
    def _is_ai_cmd_mode(app) -> bool:
        """True when the Ava command session (D3, Ava Front Door spec v2 --
        replaces the old "AI command mode") is active. Name kept as-is
        (not renamed to _is_ava_cmd_session) -- purely internal, and
        renaming would touch every call site below for no behavior
        change."""
        return getattr(app, 'ava_command_session_active', False)

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

    def _enqueue_toggle_utterance(self, buffer_copy: list, owner_token: int | None) -> None:
        """Append one captured utterance and ensure one FIFO worker drains it."""
        with self._toggle_queue_lock:
            self._toggle_queue.append((buffer_copy, owner_token))
            if self._toggle_worker_active:
                return
            self._toggle_worker_active = True

        thread_registry.spawn(
            'cmd-utt-queue', self._drain_toggle_utterances, daemon=True,
        )

    def _touch_toggle_speech_activity(self) -> None:
        """Extend inactivity only on a fresh Silero non-speech -> speech edge."""
        if not self._is_toggle_cmd(self._app):
            return
        touch_activity = getattr(self._app, '_touch_session_activity', None)
        if touch_activity is not None:
            touch_activity(speech_onset=True)

    def _post_wake_admission(self):
        app = self._app
        if self._is_toggle_cmd(app) or self._is_ai_cmd_mode(app) or app._hotkey_recording:
            return None
        if app.app_state not in ('wake_session', 'quick_dictation', 'long_dictation') and not app.wake_word_triggered:
            return None
        return getattr(app, '_wake_capture_admission', None)

    def _trim_post_wake_frame(self, frame, chunk, admission):
        """The callback timestamp is the end of a captured 100 ms block."""
        confirmed_at, policy, guard_ms = admission
        if policy == 'keep':
            return chunk
        skip = _samples_before(frame.t_capture, len(chunk), confirmed_at + guard_ms / 1000.0)
        self._guard_discarded_samples += skip
        return chunk[skip:]

    def _record_post_wake_capture(self, admission, retained_discarded_ms=None):
        """retained_discarded_ms is set when the in-session rewind was kept: it
        is the part of the rewind that fell before confirmation + guard."""
        _, policy, guard_ms = admission
        if retained_discarded_ms is not None:
            discarded_ms = retained_discarded_ms
        elif policy == 'discard':
            discarded_ms = PREBUFFER_FRAMES * FRAME_MS + self._guard_discarded_samples * 1000 / SAMPLE_RATE
        else:
            discarded_ms = 0.0
        retained = retained_discarded_ms is not None
        self._guard_discarded_samples = 0
        self._log_frame(logging.DEBUG, 'capture_policy',
                        '[WAKE-POLICY] policy=%s discarded_ms=%.3f post_wake_guard_ms=%.3f '
                        'retain_in_session=%s',
                        policy, discarded_ms, guard_ms, retained)
        flight_recorder.record('wake.capture_policy', policy=policy,
                               discarded_ms=discarded_ms, post_wake_guard_ms=guard_ms,
                               retain_in_session=retained)

    def _drain_toggle_utterances(self) -> None:
        while True:
            with self._toggle_queue_lock:
                if not self._toggle_queue:
                    self._toggle_worker_active = False
                    return
                buffer_copy, owner_token = self._toggle_queue.popleft()

            if not self._is_toggle_cmd(self._app):
                logger.info('[CMD-UTT] Session ended -- discarding queued post-exit utterance')
                flight_recorder.record('session.dispatch', op='dropped', kind='toggle_command', reason='session_ended')
                self._close_hands_free_duck_safe(self._app, owner_token)
                continue
            flight_recorder.record('session.dispatch', op='started', kind='toggle_command', thread='cmd-utt-queue')
            try:
                self._app._handle_command_mode_utterance(buffer_copy, SAMPLE_RATE)
            except Exception as exc:
                logger.exception(f'[CMD-UTT] FIFO worker error: {exc}')
                flight_recorder.record('session.dispatch', op='completed', kind='toggle_command', exc=True)
            else:
                flight_recorder.record('session.dispatch', op='completed', kind='toggle_command', exc=False)
            finally:
                self._close_hands_free_duck_safe(self._app, owner_token)

    def _log_frame(self, level, key, message, *args) -> None:
        """Bound recurring frame diagnostics, independently for each condition."""
        now = time.monotonic()
        if now - self._frame_log_times.get(key, float('-inf')) >= 5.0:
            self._frame_log_times[key] = now
            logger.log(level, message, *args)

    def _handle_fatal(self, exc: Exception) -> None:
        if self._fatal_reported:
            return
        self._fatal_reported = True
        self._running = False
        self._stop_event.set()
        logger.error("[ACE] Wake consumer stopped after fatal error: %s", exc,
                     exc_info=(type(exc), exc, exc.__traceback__))
        app = self._app
        self._close_capture_duck()
        with self._toggle_queue_lock:
            queued = list(self._toggle_queue)
            self._toggle_queue.clear()
        for _frames, token in queued:
            if token is not None:
                self._close_hands_free_duck_safe(app, token)

        def cleanup(name):
            action = getattr(app, name, None)
            if callable(action):
                try:
                    action()
                    return True
                except Exception as cleanup_exc:
                    logger.warning("[ACE] Wake fatal cleanup %s failed: %s", name, cleanup_exc)
            return False

        # Do not consult config during failure cleanup: malformed config may
        # be the very exception that killed the poll loop.
        was_toggle = bool(getattr(app, 'command_mode_active', False))
        was_ava = bool(getattr(app, 'ava_command_session_active', False))
        if was_toggle:
            if not cleanup('exit_command_mode'):
                cleanup('_cancel_command_mode_inactivity_timer')
                cleanup('_release_streaming_preview')
        if was_ava:
            if not cleanup('exit_ava_command_session'):
                cleanup('_cancel_ava_cmd_inactivity_timer')
        if (getattr(app, 'app_state', 'asleep') in ('wake_session', 'quick_dictation', 'long_dictation')
                or getattr(app, 'wake_word_triggered', False)):
            if not cleanup('_end_wake_session'):
                cleanup('_restore_audio')
                cleanup('_indicator_reset')
            # A failing app hook must not leave a session latched.
            app.app_state = 'asleep'
        with getattr(app, '_wake_session_lock', self._lifecycle_lock):
            for name in ('_wake_session_inactivity_timer', 'wake_word_timer', '_dictation_finalize_timer',
                         '_dictation_hardcap_timer', '_dictation_failsafe_timer'):
                timer = getattr(app, name, None)
                if timer is not None:
                    try:
                        timer.cancel()
                    except Exception as cleanup_exc:
                        logger.warning("[ACE] Wake fatal timer cleanup failed: %s", cleanup_exc)
                    setattr(app, name, None)
        if was_toggle:
            app.command_mode_active = False
        if was_ava:
            app.ava_command_session_active = False
        app.wake_word_active = False
        app.wake_word_triggered = False
        app._oww_wake_detected = False
        app._wake_rearm_needs_onset = False
        app._wake_capture_admission = None
        app._wake_session_started_at = None
        app._wake_session_deadline = None
        app._wake_session_expires_at = None
        app.wake_dictation_mode = None
        app.wake_dictation_buffer = []
        app.wake_dictation_start_time = None
        app._dictation_silence_timeout = None
        app._dictation_require_end = False
        app._dictation_paused = False
        app.is_speaking = False
        app.silence_start = None
        reasons = getattr(app, '_wake_consumer_reasons', None)
        if reasons is not None:
            reasons.clear()
        self._utterance_frames = []
        self._buffer_rms_history = []
        self._await_wake_onset = False
        self._silero_was_speech = False
        self._rms_was_speech = None
        self._wake_admission = None
        self._guard_discarded_samples = 0
        try:
            if self.on_fatal is not None:
                self.on_fatal(exc)
            elif callable(getattr(app, 'play_sound', None)):
                app.play_sound('error')
        except Exception as callback_exc:
            logger.warning("[ACE] Wake on_fatal callback failed: %s", callback_exc)

    def _poll_loop(self, stop_event=None) -> None:
        app = self._app
        stop_event = self._stop_event if stop_event is None else stop_event
        try:
            while self._running and not stop_event.is_set():
                if not (
                    app.wake_word_active
                    or self._is_toggle_cmd(app)
                    or self._is_ai_cmd_mode(app)
                ):
                    time.sleep(0.005)
                    continue

                frame = self._reader.read_next()
                if stop_event.is_set():
                    break
                if frame is EMPTY:
                    time.sleep(0.005)
                    continue

                self._process_frame(frame)
        except Exception as exc:
            self._handle_fatal(exc)
        finally:
            self._running = False
            self._close_capture_duck()

    def _process_frame(self, frame) -> None:  # noqa: C901 (complexity mirrors legacy callback)
        app = self._app

        if app.app_state == 'wake_session':
            app._expire_wake_session()
        if getattr(app, '_wake_rearm_needs_onset', False) is True:
            self.abort_utterance()
            self._await_wake_onset = True
            app._wake_rearm_needs_onset = False

        # Epoch-change detection: abort utterance and reset state
        if self._last_epoch is not None and frame.device_epoch != self._last_epoch:
            self._log_frame(logging.WARNING, 'frame_562', "[ACE] Wake path: epoch change — aborting utterance, resetting state")
            self._utterance_frames   = []
            self._buffer_rms_history = []
            app.is_speaking   = False
            app.silence_start = None
            self._close_hands_free_duck_safe(app, self._hands_free_capture_duck_token)
            self._hands_free_capture_duck_token = None
            try:
                app._vad_reset()
            except Exception as e:
                self._log_frame(logging.DEBUG, 'frame_572', f"_vad_reset failed after epoch change: {e}")
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
        # Toggle-command-mode used to be UNCONDITIONALLY exempted here: an
        # active, user-initiated listening session that must keep
        # servicing regardless of a concurrent hotkey press (a hotkey
        # press mid-DICTATE-chunk must not eat the chunk), and the
        # always-live global abort phrase lives entirely inside
        # SessionModeManager.dispatch_utterance(), reached only via
        # _flush() -> app._handle_command_mode_utterance(), which only
        # ever fires from THIS same poll loop's toggle-command branch --
        # so exempting _is_toggle_cmd(app) here is what kept global abort
        # reachable while a hotkey was held.
        #
        # hands_free.suspend_on_hold (2026-09-11, default true): that
        # unconditional exemption is exactly the reported double-capture
        # bug -- the SAME words landed both in the hold-to-record path's
        # paste and in the toggle lane's own in-progress buffer. The
        # exemption is now conditional on the setting being OFF (an
        # explicit "reproduce the old behaviour" comparison mode); with it
        # on (default), toggle-command-mode goes fully deaf for the
        # duration of the hold, same as AI-command-mode below, and global
        # abort is correspondingly unreachable while held (the trade the
        # task accepted: a hold means the user is using the keyboard/hold
        # path right now, not trying to voice-command the session
        # simultaneously). discard_stale_wake_utterance() drops the same
        # exemption the same way, so any in-progress toggle utterance is
        # discarded the INSTANT the hold starts rather than waiting for
        # this poll loop's next frame.
        #
        # AI-command-mode is DELIBERATELY NOT exempted (2026-07-19 nag
        # incident): letting it keep servicing during a hotkey hold meant
        # its utterance loop kept transcribing and speaking "I didn't catch
        # a command in that" WHILE a hold-to-dictate recording was in
        # progress (log-confirmed: [OK] dictation and the AI-CMD nag firing
        # in the same second). AI-command-mode has no global-abort-style
        # reachability requirement the way toggle-command-mode does, so it
        # now goes fully deaf for the duration of the hotkey hold, same as
        # plain wake-word mode.
        toggle_cmd_exempt = self._is_toggle_cmd(app) and not self._suspend_on_hold_enabled(app)
        hotkey_suppress = (
            app._hotkey_recording
            and not toggle_cmd_exempt
        )
        if hotkey_suppress:
            if not self._hotkey_suppressed_last:
                self._log_frame(logging.DEBUG, 'hotkey_suppressed',
                    "[SEAM] WakeConsumer suppression ENGAGED (hotkey recording "
                    "active) -- wake-word speech detection fully skipped "
                    "(no RMS/VAD/OWW/onset/buffering) until hotkey recording ends"
                )
                self._hotkey_suppressed_last = True
                # hands_free.suspend_on_hold: pause the toggle session's own
                # inactivity timer for the hold's duration so a long hold
                # cannot silently time the session out. No-op (nothing to
                # pause) for plain wake-word/AI-command-mode, or when no
                # timer is currently running.
                if self._is_toggle_cmd(app):
                    pause = getattr(app, '_pause_command_mode_inactivity_for_hold', None)
                    if pause is not None:
                        try:
                            pause()
                        except Exception as e:
                            self._log_frame(logging.DEBUG, 'hold_timer_pause_failed',
                                            "inactivity timer pause failed: %s", e)
            return   # cursor already advanced (frame already read in _poll_loop)
        if self._hotkey_suppressed_last:
            self._log_frame(logging.DEBUG, 'frame_625', "[SEAM] WakeConsumer suppression RELEASED (hotkey recording ended)")
            self._hotkey_suppressed_last = False
            # hands_free.suspend_on_hold: resume from HERE (the current ring
            # position -- nothing above rewinds the reader; suppression
            # only ever skipped this method's OWN accumulation, never the
            # poll loop's read_next() calls, see _poll_loop) with whatever
            # inactivity time was actually left when the hold started.
            if self._is_toggle_cmd(app):
                resume = getattr(app, '_resume_command_mode_inactivity_after_hold', None)
                if resume is not None:
                    try:
                        resume()
                    except Exception as e:
                        self._log_frame(logging.DEBUG, 'hold_timer_resume_failed',
                                        "inactivity timer resume failed: %s", e)

        # ── Ava command session half-duplex guard (2026-07-23 G3 live-test
        # finding): full deafness while the session's own TTS is playing,
        # plus a short tail after playback completes, so the mic never
        # captures Ava's own voice as if it were the next user utterance
        # (log-confirmed: an utterance transcribed as ", open up, I'm
        # listening, open up tab" -- "I'm listening" is the session's own
        # TTS, captured into the SAME buffer as the surrounding real
        # speech). Mirrors FIX 1's hotkey_suppress pattern above: full
        # deafness (no RMS/VAD/OWW/onset/buffering), not just a downstream
        # discard, so a mid-utterance TTS onset can't silently extend or
        # corrupt an in-progress buffer either. Gates on AudioCoordinator's
        # SPEAKING state (ac.is_speaking), not app.is_speaking -- that flag
        # means something unrelated here (VAD-detected USER speech onset,
        # set just below in the speech-accumulation block).
        if self._is_ai_cmd_mode(app):
            coordinator = getattr(app, 'audio_coordinator', None)
            tts_speaking = bool(coordinator is not None and coordinator.is_speaking)
            if tts_speaking:
                self._ava_cmd_tts_speaking_end = None  # still speaking -- no tail yet
            elif self._ava_cmd_tts_speaking_end is None and self._ava_cmd_tts_was_speaking:
                self._ava_cmd_tts_speaking_end = time.monotonic()  # tail window starts now
            self._ava_cmd_tts_was_speaking = tts_speaking

            in_tail = (
                self._ava_cmd_tts_speaking_end is not None
                and (time.monotonic() - self._ava_cmd_tts_speaking_end) < _AVA_CMD_TTS_TAIL_S
            )
            if tts_speaking or in_tail:
                if not self._ava_cmd_tts_suppressed_last:
                    self._log_frame(logging.DEBUG, 'ava_tts_suppressed',
                        "[SEAM] Ava command session suppression ENGAGED (session "
                        "TTS speaking or in its tail) -- speech detection fully "
                        "skipped until playback + tail complete"
                    )
                    self._ava_cmd_tts_suppressed_last = True
                return   # cursor already advanced (frame already read in _poll_loop)
            if self._ava_cmd_tts_suppressed_last:
                self._log_frame(logging.DEBUG, 'frame_665', "[SEAM] Ava command session suppression RELEASED (TTS + tail complete)")
                self._ava_cmd_tts_suppressed_last = False
        elif self._ava_cmd_tts_was_speaking or self._ava_cmd_tts_speaking_end is not None:
            # Left the session without a clean SPEAKING->tail->RELEASE
            # cycle (e.g. exit mid-TTS) -- don't let stale state leak into
            # a future re-entry.
            self._ava_cmd_tts_was_speaking = False
            self._ava_cmd_tts_speaking_end = None
            self._ava_cmd_tts_suppressed_last = False

        # Convert int16 ring frame -> float32 at SAMPLE_RATE
        # Ring stores raw (non-AEC) audio — correct for both VAD and Whisper.
        raw_chunk = frame.pcm.astype(np.float32) / 32767.0   # shape: (FRAME_SIZE,)

        admission = self._post_wake_admission()
        if admission != self._wake_admission:
            self._wake_admission = admission
            self._guard_discarded_samples = 0
            if admission is not None:
                # A confirmation starts a new capture, including when Whisper
                # confirmed on its worker while this poller was buffering TV.
                self._utterance_frames = []
                self._buffer_rms_history = []
                app.is_speaking = False
                app.silence_start = None

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
            speech_threshold = min(speech_threshold, WAKE_SPEECH_THRESHOLD_CAP)
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
        silero_onset = False
        if app._vad_available:
            try:
                is_speech = app._vad_is_speech(raw_chunk, src_rate=SAMPLE_RATE)
                silero_onset = bool(is_speech and not self._silero_was_speech)
                self._silero_was_speech = bool(is_speech)
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
                    self._log_frame(logging.DEBUG, 'frame_767', f"_vad_reset failed after VAD inference error: {e}")
                if app._vad_consec_errors >= 50:
                    self._log_frame(logging.WARNING, 'frame_769', "[VAD] 50 consecutive errors — disabling VAD for session, RMS only")
                    app._vad_available = False
                is_speech = rms > speech_threshold
        else:
            is_speech = rms > speech_threshold

        rms_positive = rms > speech_threshold
        rms_onset = bool(rms_positive and self._rms_was_speech is False)
        self._rms_was_speech = rms_positive
        # When Silero is down (missing model or 50 consecutive inference
        # errors), silero_onset is structurally always False, so an RMS onset
        # is the only signal that can clear the wake-onset latch -- without it
        # hands-free goes deaf until restart (review: critical, :687).
        # It must NOT renew the session timer: an RMS edge cannot tell the
        # owner from a TV, and the 2026-09-10 session policy re-arms only on
        # a Silero speech onset so a media monologue cannot keep the mic open.
        # With VAD unavailable a session therefore runs to its inactivity
        # deadline and the user re-wakes. Degraded, loud (warning below), and
        # bounded -- by design. See test_rms_fallback_does_not_count_as_silero_onset.
        vad_unavailable_onset = not app._vad_available and rms_onset
        if self._await_wake_onset:
            fallback_onset = vad_unavailable_onset
            if not (silero_onset or fallback_onset):
                return
            self._await_wake_onset = False
            if fallback_onset and not self._rms_fallback_warned:
                self._rms_fallback_warned = True
                logger.warning("[VAD] Wake rearmed on RMS onset after quiet; Silero unavailable")
        if silero_onset:
            self._touch_toggle_speech_activity()
            if app.app_state == 'wake_session':
                app._restart_wake_session_timer(speech_onset=True)

        if admission is not None:
            raw_chunk = self._trim_post_wake_frame(frame, raw_chunk, admission)
            if not len(raw_chunk):
                return
            rms = float(np.sqrt(np.mean(raw_chunk ** 2)))

        # OWW pre-filter (data already at 16kHz — no resample needed). The
        # gain is samsara.audio_engine.wake_prefilter's, shared with the mic
        # setup guide's wake test so both score the same signal (queue 67).
        if (app.app_state == 'asleep'
                and not app.wake_word_triggered
                and app._wake_detector is not None
                and app._wake_detector.is_available):
            _oww_chunk = oww_prefilter(raw_chunk, rms)
            if app._wake_detector.detected(_oww_chunk):
                app._oww_wake_detected = True
                app._wake_detector.reset()
                flight_recorder.record('wake.oww_confirm', app_state=app.app_state)

        # Wake-profile OWW pre-filters: a profile with a loaded model
        # participates as a real pre-filter too, same as the primary
        # detector above, instead of merely disabling the primary gate for
        # every phrase whenever any profile is enabled (see _flush's gate).
        if (app.app_state == 'asleep'
                and not app.wake_word_triggered
                and not app._oww_wake_detected):
            for _profile_detector in getattr(app, '_wake_profile_detectors', {}).values():
                if _profile_detector is None or not _profile_detector.is_available:
                    continue
                _oww_chunk = oww_prefilter(raw_chunk, rms)
                if _profile_detector.detected(_oww_chunk):
                    app._oww_wake_detected = True
                    _profile_detector.reset()
                    flight_recorder.record('wake.oww_confirm', app_state=app.app_state, profile=True)
                    break

        # ── Speech accumulation ───────────────────────────────────────────────
        if is_speech:
            speech_onset   = not app.is_speaking
            app.is_speaking   = True
            app.silence_start = None

            if speech_onset:
                flight_recorder.record(
                    'wake.session_open', app_state=app.app_state,
                    wake_word_triggered=bool(app.wake_word_triggered),
                )
                # Hands-free capture-window ducking (2026-07-24): OPEN at the
                # earliest possible signal that an utterance is being captured
                # only when a deliberately latched session owns the turn
                # (toggle / AI-command mode). In passive wake-word mode, the
                # wake phrase itself must remain at idle-duck (0.8) audibility,
                # and deep ducking is deferred until wake confirmation.
                if self._is_toggle_cmd(app) or self._is_ai_cmd_mode(app):
                    self._hands_free_capture_duck_token = self._open_hands_free_duck_safe(app)
                # Ring prebuffer rewind: replaces the legacy _prebuffer deque drain.
                # Rewind PREBUFFER_FRAMES and re-read them into the utterance buffer.
                # The current frame (raw_chunk) is included in the re-read since the
                # cursor was at this position before the rewind. Do NOT append
                # raw_chunk again after — that would double the onset frame.
                # (ARC audit: double-appending of speech onset frame)
                prebuffer_frames = PREBUFFER_FRAMES
                retain_not_before = None
                if admission is not None:
                    if (admission[1] == 'discard'
                            and wake_capture_session_open(app)
                            and retain_prebuffer_in_session(app.config)):
                        # Queue 44: keep the rewind, clamped at confirmation +
                        # guard (recorded after the re-read, below).
                        retain_not_before = admission[0] + admission[2] / 1000.0
                    else:
                        self._record_post_wake_capture(admission)
                        if admission[1] == 'discard':
                            prebuffer_frames = 0
                            self._utterance_frames.append(raw_chunk)
                            self._buffer_rms_history.append(rms)
                if self._is_toggle_dictate(app):
                    # Never rewind farther than the silence boundary that
                    # separated two staged chunks. Otherwise a short manual-
                    # commit gap can re-include the previous chunk's tail and
                    # create duplicated words.
                    prebuffer_frames = min(
                        PREBUFFER_FRAMES,
                        max(2, int(silence_threshold * 1000 / FRAME_MS)),
                    )
                if prebuffer_frames:
                    self._reader.rewind(prebuffer_frames)
                clamped_samples = 0
                for _ in range(prebuffer_frames):
                    pb_frame = self._reader.read_next()
                    if pb_frame is EMPTY:
                        break
                    pb_pcm = pb_frame.pcm.astype(np.float32) / 32767.0
                    if retain_not_before is not None:
                        skip = _samples_before(pb_frame.t_capture, len(pb_pcm), retain_not_before)
                        clamped_samples += skip
                        pb_pcm = pb_pcm[skip:]
                        if not len(pb_pcm):
                            continue
                    self._utterance_frames.append(pb_pcm)
                    self._buffer_rms_history.append(
                        float(np.sqrt(np.mean(pb_pcm ** 2)))
                    )
                if retain_not_before is not None:
                    self._record_post_wake_capture(
                        admission, retained_discarded_ms=clamped_samples * 1000 / SAMPLE_RATE)
                if prebuffer_frames and self._utterance_frames:
                    self._log_frame(logging.DEBUG, 'frame_864', f"[PRE] Prepended {sum(len(c) for c in self._utterance_frames) * 1000 // SAMPLE_RATE}ms pre-buffer to wake onset")
                # Diagnostic (2026-07-10 hotkey word-loss investigation,
                # updated by FIX 1, narrowed 2026-07-19 nag incident): this
                # branch is now UNREACHABLE while a plain hotkey recording
                # is active -- _process_frame's top-level hotkey-deafness
                # guard returns before speech onset is ever evaluated. The
                # only way to reach this with _hotkey_recording=True is the
                # intentional toggle-command-mode exemption (that servicing
                # must keep running concurrently with a hotkey press).
                # AI-command-mode no longer has this exemption (see the
                # hotkey_suppress comment above), so it can't reach here
                # either. The stateless ONNX model's whole inference call is
                # serialized by app._vad_lock, so this remaining overlap can
                # briefly BLOCK the gate's scan (or vice versa) but cannot
                # interleave two runs on the dedicated InferenceSession.
                if getattr(app, '_hotkey_recording', False):
                    self._log_frame(logging.DEBUG, 'toggle_hotkey_onset',
                        "[SEAM] Wake-consumer speech onset occurred WHILE "
                        "_hotkey_recording=True -- reached only via the "
                        "toggle-command-mode exemption "
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
                    self._log_frame(logging.DEBUG, 'frame_902', f"[CAP] Stuck buffer ({buf_s:.1f}s, var={variance:.6f}) — discarding")
                    flight_recorder.record(
                        'wake.session_close', reason='stuck_buffer',
                        buffer_s=buf_s, variance=variance,
                    )
                    self._utterance_frames   = []
                    self._buffer_rms_history = []
                    app.is_speaking   = False
                    app.silence_start = None
                    try:
                        app._vad_reset()
                    except Exception as e:
                        self._log_frame(logging.DEBUG, 'frame_914', f"_vad_reset failed after stuck-buffer discard: {e}")
                    self._close_hands_free_duck_safe(app, self._hands_free_capture_duck_token)
                    self._hands_free_capture_duck_token = None
                    return

            # Hard buffer cap (same as legacy callback)
            buffer_s = len(self._utterance_frames) * (FRAME_MS / 1000.0)
            if buffer_s >= 7.0 and self._hard_cap_applies(app):
                self._log_frame(logging.DEBUG, 'frame_922', f"[CAP] Buffer at {buffer_s:.1f}s cap — discarding (likely noise/echo)")
                flight_recorder.record(
                    'wake.session_close', reason='hard_buffer_cap', buffer_s=buffer_s,
                )
                self._utterance_frames   = []
                self._buffer_rms_history = []
                app.is_speaking   = False
                app.silence_start = None
                try:
                    app._vad_reset()
                except Exception as e:
                    self._log_frame(logging.DEBUG, 'frame_933', f"_vad_reset failed after hard buffer cap: {e}")
                self._close_hands_free_duck_safe(app, self._hands_free_capture_duck_token)
                self._hands_free_capture_duck_token = None
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
                        flight_recorder.record(
                            'wake.session_close', reason='silence_flush',
                            speech_s=speech_s,
                        )
                        self._flush(buffer_copy, self._hands_free_capture_duck_token)
                    else:
                        # Too short to count as a real utterance -- the
                        # capture window this onset opened closes here
                        # (buffer discarded, never reaches _flush at all).
                        flight_recorder.record(
                            'wake.session_close', reason='too_short',
                            speech_s=speech_s,
                        )
                        self._close_hands_free_duck_safe(app, self._hands_free_capture_duck_token)
                        self._hands_free_capture_duck_token = None

    @staticmethod
    def _wrap_with_duck_close(app, fn, owner_token: int | None = None, kind: str = "unknown"):
        """Wrap a dispatched utterance-processing function so the capture-
        window duck always closes once it returns -- success, an internal
        early return, or an exception (finally-shape: the ducking design
        explicitly requires errors to still restore). Runs on whatever
        thread the wrapped function itself runs on (a freshly spawned
        daemon thread for every call site below), not this poll thread."""
        session_started_at = (
            getattr(app, '_wake_session_started_at', None)
            if kind == 'wake_buffer' and app.app_state == 'wake_session' else None
        )

        def _wrapped(*args, **kwargs):
            flight_recorder.record('session.dispatch', op='started', kind=kind)
            try:
                if session_started_at is not None:
                    app._expire_wake_session()
                    if (app.app_state != 'wake_session'
                            or app._wake_session_started_at != session_started_at):
                        flight_recorder.record('session.dispatch', op='dropped',
                                               kind=kind, reason='session_ended')
                        return
                fn(*args, **kwargs)
            except Exception:
                flight_recorder.record('session.dispatch', op='completed', kind=kind, exc=True)
                raise
            else:
                flight_recorder.record('session.dispatch', op='completed', kind=kind, exc=False)
            finally:
                WakeConsumer._close_hands_free_duck_safe(app, owner_token)
        return _wrapped

    def _flush(self, buffer_copy: list, owner_token: int | None = None) -> None:
        """Dispatch utterance to process_wake_word_buffer, respecting OWW gate."""
        app = self._app
        # Dispatch now owns this token. A later short capture/abort must not
        # close the owner of an utterance still waiting for Whisper.
        if owner_token is not None and self._hands_free_capture_duck_token == owner_token:
            self._hands_free_capture_duck_token = None

        # Ava command session (D3): route utterance to the waterfall queue.
        if self._is_ai_cmd_mode(app):
            flight_recorder.record('session.dispatch', op='enqueued', kind='ava_command', thread='ava-cmd-utt')
            thread_registry.spawn(
                'ava-cmd-utt',
                self._wrap_with_duck_close(app, app._handle_ava_command_utterance, owner_token, kind='ava_command'),
                args=(buffer_copy, SAMPLE_RATE),
                daemon=True,
            )
            return

        # Toggle command mode: bypass OWW gate and execute as a single command
        # utterance.  The WakeConsumer re-arms automatically for the next
        # utterance; no external re-arm call is needed. Capture-window close
        # happens inside _drain_toggle_utterances (the actual processing
        # point), not here (enqueueing is not processing).
        if self._is_toggle_cmd(app):
            flight_recorder.record('session.dispatch', op='enqueued', kind='toggle_command')
            self._enqueue_toggle_utterance(buffer_copy, owner_token)
            return

        if getattr(app, '_wake_rearm_needs_onset', False) is True:
            flight_recorder.record('session.dispatch', op='dropped',
                                   kind='wake_buffer', reason='session_ended')
            self._close_hands_free_duck_safe(app, owner_token)
            return

        # Only a profile with NO loaded detector still needs the Whisper
        # fallback exemption below -- one with a real model was already
        # consulted as a pre-filter in _process_frame above, so a hit there
        # already set app._oww_wake_detected. Previously any enabled profile
        # (model-backed or not) disabled the strict gate for every phrase.
        _profile_detectors = getattr(app, '_wake_profile_detectors', {})
        _has_modelless_wake_profiles = any(
            t.get('enabled', True)
            and not getattr(_profile_detectors.get(t.get('id', '')), 'is_available', False)
            for t in getattr(app, 'config', {}).get('wake_profiles', [])
        )
        _primary_oww_eligible = (
            app._wake_detector is not None
            and app._wake_detector.is_available
            and app.app_state == 'asleep'
            and not app.wake_word_triggered
        )
        _oww_hit = bool(
            _primary_oww_eligible and app._oww_wake_detected
        )
        # With no model-less profile fallbacks, the OWW pre-filters (primary
        # plus any profile detector) remain a strict gate. Enabled profiles
        # without their own model must still reach Whisper when nothing fired.
        if _primary_oww_eligible and not _oww_hit and not _has_modelless_wake_profiles:
            if app._wake_detector is not None:
                app._wake_detector.reset()
            flight_recorder.record('session.dispatch', op='dropped', kind='oww_gate_no_hit')
            # Rejected before any dispatch -- this IS the capture window's
            # close (nothing else will ever process this buffer).
            self._close_hands_free_duck_safe(app, owner_token)
            return

        # Preserve the detector result across the async dispatch. An OWW hit
        # has already supplied the cheap energy/shape prefilter, so the legacy
        # RMS gate must not reject the same buffer before Whisper can confirm
        # the phrase. Whisper confirmation remains mandatory.
        # A primary OWW hit remains authoritative even when wake profiles are
        # enabled. Previously profile presence forced this False, so a 0.99
        # Jarvis detection was subsequently discarded by the adaptive RMS
        # gate. Profiles only relax the no-hit path; they must not erase a hit.
        oww_confirmed = _oww_hit
        app._oww_wake_detected = False
        # Passive wake-mode deep ducking is now moved to OWW dispatch (command
        # utterance protection) so wake-phrase audibility stays at idle-duck.
        owner_token = (
            self._open_hands_free_duck_safe(app)
            if owner_token is None
            else owner_token
        )
        # The app queue owns passive dispatch tokens too; never publish them
        # back into the consumer's next capture window.
        app.process_wake_word_buffer(
            buffer_copy, SAMPLE_RATE, oww_confirmed=oww_confirmed,
            owner_token=owner_token, tracked=app.app_state == 'long_dictation',
        )

    def __repr__(self) -> str:
        return (
            f"WakeConsumer(running={self._running}, "
            f"frames={len(self._utterance_frames)})"
        )
