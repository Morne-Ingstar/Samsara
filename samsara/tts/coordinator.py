"""AudioCoordinator: centralized audio state machine for Samsara.

Manages four exclusive states (IDLE, LISTENING, THINKING, SPEAKING) with
thread-safe transitions and side effects: earcon ducking, wake-word threshold
management, interrupt-on-speech detection, and thinking_pulse scheduling.

Phase 1b scope:
  - queue_mode and category enforcement deferred to Phase 2
  - thinking_pulse_enabled defaults False (no THINKING routes wired yet)
  - Speech budget (repetition suppression, long-response heads-up): Phase 2

Design principles (from tts_architecture_spec_v2.md):
  - All state mutations go through transition_to() under _state_lock
  - Side effects happen AFTER transition commits, never during
  - Duck depth counter prevents double-restore on overlapping earcons
  - Interrupt grace period measured from engine 'playing' state, not speak()
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from .engine_base import SpeechHandle, TTSEngine

logger = logging.getLogger(__name__)

# States
IDLE = 'IDLE'
LISTENING = 'LISTENING'
THINKING = 'THINKING'
SPEAKING = 'SPEAKING'

# Valid state transitions: {from_state: {to_state, ...}}
_VALID_TRANSITIONS: Dict[str, set] = {
    IDLE:      {LISTENING, SPEAKING},           # SPEAKING: for direct coordinator.speak() calls
    LISTENING: {THINKING, SPEAKING, IDLE},
    THINKING:  {SPEAKING, IDLE},
    SPEAKING:  {IDLE, LISTENING},
}

# Known earcon durations in ms. Used to time the duck restore.
# Approximate values based on the generated WAV lengths.
_EARCON_DURATIONS_MS: Dict[str, int] = {
    'start': 280,       'stop': 180,
    'success': 400,     'error': 280,
    'capture_started': 160,  'capture_saved': 220,
    'agent_routing': 130,    'agent_response': 220,
    'confirm_required': 250, 'action_complete': 150,
    'thinking_pulse': 70,
}
_DEFAULT_EARCON_DURATION_MS = 300


class AudioCoordinator:
    """Centralized audio state machine.

    Attach to the Samsara app via:
        app.audio_coordinator = AudioCoordinator(app, engine, config)

    Then call coordinator.speak() from plugins instead of engine.speak()
    directly so state transitions, threshold adjustments, and ducking happen
    automatically.
    """

    def __init__(self, app, engine: TTSEngine, config: dict = None):
        self.app = app
        self.engine = engine
        cfg = config or {}

        # Configuration with defaults.
        self._duck_factor = float(cfg.get('duck_factor', 0.7))
        self._duck_default_ms = int(cfg.get('duck_default_duration_ms', _DEFAULT_EARCON_DURATION_MS))
        self._duck_fade_ms = int(cfg.get('duck_fade_ms', 5))
        self._interrupt_grace_ms = int(cfg.get('interrupt_grace_period_ms', 200))
        self._wake_thresh_mult = float(cfg.get('speaking_wake_threshold_multiplier', 1.5))
        self._vad_thresh_mult = float(cfg.get('speaking_vad_threshold_multiplier', 0.6))
        self._thinking_pulse_interval_ms = int(cfg.get('thinking_pulse_interval_ms', 1000))
        self._thinking_pulse_enabled = bool(cfg.get('thinking_pulse_enabled', False))

        # State machine.
        self._state = IDLE
        self._state_lock = threading.Lock()

        # Active TTS handle in SPEAKING state.
        self._active_handle: Optional[SpeechHandle] = None

        # Earcon duck depth counter.
        # Protected by _state_lock so the duck decision and counter update
        # are atomic — prevents the race where two earcons fire before the
        # first duck takes effect, leading to _duck_depth==2 with only one
        # set_volume(duck) call made.
        self._duck_depth = 0
        self._duck_timers: List[threading.Timer] = []

        # Interrupt-on-speech polling.
        self._interrupt_poll_active = False
        self._interrupt_poll_thread: Optional[threading.Thread] = None
        self._interrupt_grace_active = False
        self._interrupt_grace_timer: Optional[threading.Timer] = None
        self._active_interruptible = True

        # Thinking-pulse timer chain.
        self._thinking_pulse_timer: Optional[threading.Timer] = None

        # Wake-word / VAD threshold stash (restore on SPEAKING exit).
        self._original_speech_threshold: Optional[float] = None

        # State-change listener registry.
        self._listeners: List[Callable] = []
        self._listeners_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_speaking(self) -> bool:
        """True while the coordinator is in the SPEAKING state."""
        return self._state == SPEAKING

    def speak(
        self,
        text: str,
        *,
        category: str = "general",
        queue_mode: str = "append",
        voice_id: Optional[str] = None,
        speed: Optional[float] = None,
        volume: Optional[float] = None,
        on_done: Optional[Callable] = None,
        interruptible: bool = True,
    ) -> SpeechHandle:
        """High-level TTS entry point. Manages state, thresholds, and ducking.

        Plugins and subsystems should call this instead of engine.speak().
        """
        # Suppress long TTS responses while command mode is active so the
        # assistant doesn't talk over the user's next command.
        if getattr(self.app, 'command_mode_active', False):
            char_limit = self.app.config.get('command_mode', {}).get('tts_char_limit', 50)
            if len(text) > char_limit:
                logger.info(
                    "AudioCoordinator: TTS suppressed (%d chars > %d) in command mode",
                    len(text), char_limit,
                )
                return SpeechHandle(utterance_id='noop-cmd-mode')

        tts_cfg = self.app.config.get('tts', {})
        effective_speed = speed if speed is not None else tts_cfg.get('speed', 1.0)
        effective_volume = volume if volume is not None else tts_cfg.get('volume', 0.8)
        effective_voice = voice_id or tts_cfg.get('voice_id')

        # Wrap caller's on_done so we get the IDLE transition.
        def _on_tts_done():
            self.transition_to(IDLE, context={'reason': 'tts_complete'})
            if on_done:
                try:
                    on_done()
                except Exception:
                    logger.exception("AudioCoordinator: on_done callback raised")

        handle = self.engine.speak(
            text,
            voice_id=effective_voice,
            speed=effective_speed,
            volume=effective_volume,
            category=category,
            queue_mode=queue_mode,
            on_done=_on_tts_done,
        )

        self._active_handle = handle
        self._active_interruptible = interruptible
        self.transition_to(SPEAKING, context={'utterance_id': handle.utterance_id, 'text': text})
        return handle

    def cancel_speech(self) -> None:
        """Cancel any in-progress TTS and return to IDLE."""
        self.engine.cancel_all()
        self.transition_to(IDLE, context={'reason': 'cancel_speech'})

    def on_earcon_starting(self, sound_type: str) -> None:
        """Hook called by play_sound before an earcon plays.

        If TTS is actively playing, ducks the TTS volume, then schedules a
        restore after the earcon's known duration.
        """
        # Only duck during SPEAKING when we have an active engine stream.
        if self.engine.get_engine_state() != 'playing':
            return

        with self._state_lock:
            if self._state != SPEAKING:
                return
            handle = self._active_handle
            if handle is None:
                return

            duration_ms = _EARCON_DURATIONS_MS.get(sound_type, self._duck_default_ms)
            was_zero = self._duck_depth == 0
            self._duck_depth += 1

            if was_zero:
                # First earcon: initiate the duck.
                self.engine.set_volume(handle, self._duck_factor, fade_ms=self._duck_fade_ms)

        # Schedule restore outside the lock to avoid holding it during Timer
        # construction (the Timer callback itself acquires the lock).
        t = threading.Timer(duration_ms / 1000.0, self._on_duck_restore)
        t.daemon = True
        with self._state_lock:
            self._duck_timers.append(t)
        t.start()

    def transition_to(self, new_state: str, context: dict = None) -> bool:
        """Request a state transition. Thread-safe; validates legality.

        Returns True if the transition was applied, False if it was rejected
        (invalid transition or already in the target state).
        """
        context = context or {}
        with self._state_lock:
            old_state = self._state
            if old_state == new_state:
                return False
            allowed = _VALID_TRANSITIONS.get(old_state, set())
            if new_state not in allowed:
                logger.warning(
                    "AudioCoordinator: rejected transition %s → %s", old_state, new_state
                )
                return False
            self._state = new_state

        # Side effects happen AFTER the transition commits and outside the lock.
        self._on_transition(old_state, new_state, context)
        self._notify_listeners(old_state, new_state, context)
        logger.debug("AudioCoordinator: %s → %s  context=%s", old_state, new_state, context)
        return True

    def get_state(self) -> str:
        with self._state_lock:
            return self._state

    def register_state_listener(self, callback: Callable) -> None:
        """Subscribe to (old_state, new_state, context_dict) callbacks."""
        with self._listeners_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unregister_state_listener(self, callback: Callable) -> None:
        with self._listeners_lock:
            self._listeners = [l for l in self._listeners if l is not callback]

    def shutdown(self) -> None:
        """Cancel all pending speech, timers, and polling threads."""
        self.engine.cancel_all()
        self._stop_interrupt_poll()
        self._stop_thinking_pulse()
        self._cancel_duck_timers()
        self._restore_thresholds()
        with self._state_lock:
            self._state = IDLE

    # ------------------------------------------------------------------
    # State-transition side effects
    # ------------------------------------------------------------------

    def _on_transition(self, old: str, new: str, context: dict) -> None:
        """Apply side effects for the (old → new) transition."""
        if new == SPEAKING:
            self._on_enter_speaking(context)
        elif old == SPEAKING:
            self._on_exit_speaking(old, new, context)

        if new == THINKING:
            self._on_enter_thinking()
        elif old == THINKING:
            self._stop_thinking_pulse()

        if new == IDLE:
            self._on_enter_idle()

    def _on_enter_speaking(self, context: dict) -> None:
        self._stop_thinking_pulse()
        self._apply_speaking_thresholds()
        self._start_interrupt_poll()

    def _on_exit_speaking(self, old: str, new: str, context: dict) -> None:
        self._stop_interrupt_poll()
        self._cancel_duck_timers()
        self._restore_thresholds()
        with self._state_lock:
            self._duck_depth = 0

    def _on_enter_thinking(self) -> None:
        self._apply_thinking_thresholds()
        if self._thinking_pulse_enabled:
            self._schedule_thinking_pulse()

    def _on_enter_idle(self) -> None:
        self._active_handle = None

    # ------------------------------------------------------------------
    # Interrupt-on-speech polling
    # ------------------------------------------------------------------

    def _start_interrupt_poll(self) -> None:
        self._interrupt_poll_active = True
        self._interrupt_grace_active = True  # blocked until grace timer fires

        # Start grace period timer — unlocks interrupts once engine is 'playing'
        # AND the grace window has elapsed.
        self._schedule_interrupt_grace()

        t = threading.Thread(
            target=self._interrupt_poll_loop,
            daemon=True,
            name='tts-interrupt-poll',
        )
        self._interrupt_poll_thread = t
        t.start()

    def _schedule_interrupt_grace(self) -> None:
        """Wait until engine is in 'playing' state, then start grace timer."""
        def _watch_for_playing():
            deadline = time.monotonic() + 10.0  # safety cap
            while time.monotonic() < deadline:
                if not self._interrupt_poll_active:
                    return
                if self.engine.get_engine_state() == 'playing':
                    break
                time.sleep(0.02)
            # Engine is now playing (or timed out). Start the grace timer.
            if not self._interrupt_poll_active:
                return
            t = threading.Timer(
                self._interrupt_grace_ms / 1000.0,
                self._on_grace_expired,
            )
            t.daemon = True
            self._interrupt_grace_timer = t
            t.start()

        threading.Thread(target=_watch_for_playing, daemon=True,
                         name='tts-grace-watch').start()

    def _on_grace_expired(self) -> None:
        self._interrupt_grace_active = False

    def _stop_interrupt_poll(self) -> None:
        self._interrupt_poll_active = False
        self._interrupt_grace_active = False
        if self._interrupt_grace_timer is not None:
            self._interrupt_grace_timer.cancel()
            self._interrupt_grace_timer = None

    def _interrupt_poll_loop(self) -> None:
        """Poll app.is_speaking. On speech onset (after grace), fire interrupt."""
        while self._interrupt_poll_active:
            if self.get_state() != SPEAKING:
                break
            if self._interrupt_grace_active:
                time.sleep(0.025)
                continue
            if getattr(self.app, 'is_speaking', False):
                if not getattr(self, '_active_interruptible', True):
                    time.sleep(0.025)
                    continue
                self._on_user_speech_interrupt()
                break
            time.sleep(0.025)

    def _on_user_speech_interrupt(self) -> None:
        """Cancel TTS and transition to LISTENING when user speaks."""
        logger.info("AudioCoordinator: user speech detected — interrupting TTS")
        if self._active_handle:
            self.engine.cancel(self._active_handle)
        self.transition_to(LISTENING, context={'reason': 'user_speech_interrupt'})

    # ------------------------------------------------------------------
    # Earcon ducking internals
    # ------------------------------------------------------------------

    def _on_duck_restore(self) -> None:
        """Called by threading.Timer when an earcon's duration has elapsed."""
        with self._state_lock:
            self._duck_depth = max(0, self._duck_depth - 1)
            restore = self._duck_depth == 0
            handle = self._active_handle if self._state == SPEAKING else None

        if restore and handle is not None:
            self.engine.set_volume(handle, 1.0, fade_ms=self._duck_fade_ms)

    def _cancel_duck_timers(self) -> None:
        with self._state_lock:
            timers = list(self._duck_timers)
            self._duck_timers.clear()
        for t in timers:
            t.cancel()

    # ------------------------------------------------------------------
    # Thinking pulse
    # ------------------------------------------------------------------

    def _schedule_thinking_pulse(self) -> None:
        if not self._thinking_pulse_enabled:
            return

        def _pulse():
            if self.get_state() != THINKING:
                return
            if hasattr(self.app, 'play_sound'):
                self.app.play_sound('thinking_pulse')
            self._thinking_pulse_timer = threading.Timer(
                self._thinking_pulse_interval_ms / 1000.0, _pulse
            )
            self._thinking_pulse_timer.daemon = True
            self._thinking_pulse_timer.start()

        self._thinking_pulse_timer = threading.Timer(
            self._thinking_pulse_interval_ms / 1000.0, _pulse
        )
        self._thinking_pulse_timer.daemon = True
        self._thinking_pulse_timer.start()

    def _stop_thinking_pulse(self) -> None:
        if self._thinking_pulse_timer is not None:
            self._thinking_pulse_timer.cancel()
            self._thinking_pulse_timer = None

    # ------------------------------------------------------------------
    # Wake-word / VAD threshold management
    # ------------------------------------------------------------------

    def _apply_speaking_thresholds(self) -> None:
        """Raise wake-word threshold so TTS bleed doesn't self-trigger."""
        try:
            ww_audio = self.app.config.get('wake_word_config', {}).get('audio', {})
            orig = float(ww_audio.get('speech_threshold', 0.03))
            self._original_speech_threshold = orig
            raised = orig * self._wake_thresh_mult
            ww_audio['speech_threshold'] = raised
            logger.debug(
                "AudioCoordinator: speech_threshold %0.4f → %0.4f (SPEAKING)",
                orig, raised,
            )
        except Exception:
            logger.exception("AudioCoordinator: failed to apply speaking thresholds")

    def _apply_thinking_thresholds(self) -> None:
        """Slightly raise wake-word threshold in THINKING state."""
        try:
            ww_audio = self.app.config.get('wake_word_config', {}).get('audio', {})
            orig = float(ww_audio.get('speech_threshold', 0.03))
            if self._original_speech_threshold is None:
                self._original_speech_threshold = orig
            # Modest raise — user may say "cancel" to abort the agent call
            raised = orig * 1.2
            ww_audio['speech_threshold'] = raised
        except Exception:
            logger.exception("AudioCoordinator: failed to apply thinking thresholds")

    def _restore_thresholds(self) -> None:
        """Restore wake-word threshold to the pre-SPEAKING value."""
        if self._original_speech_threshold is None:
            return
        try:
            ww_audio = self.app.config.get('wake_word_config', {}).get('audio', {})
            ww_audio['speech_threshold'] = self._original_speech_threshold
            logger.debug(
                "AudioCoordinator: speech_threshold restored to %0.4f",
                self._original_speech_threshold,
            )
        except Exception:
            logger.exception("AudioCoordinator: failed to restore thresholds")
        finally:
            self._original_speech_threshold = None

    # ------------------------------------------------------------------
    # Listener notification
    # ------------------------------------------------------------------

    def _notify_listeners(self, old: str, new: str, context: dict) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(old, new, context)
            except Exception:
                logger.exception("AudioCoordinator: state listener raised")
