"""Hotkey, keyboard-listener, CapsLock, and mouse-hotkey implementations.

``DictationApp`` keeps thin, same-signature delegates so app-facing callers and
settings/tray seams remain stable.  These methods accept the app object as
``self`` and intentionally retain their existing behavior.
"""
import logging
import sys
import time

import keyboard

from samsara import flight_recorder
from samsara.constants import DEFAULT_CONTINUOUS_COMMIT_HOTKEY, DEFAULT_CONTINUOUS_COMMIT_TRIGGER
from samsara.runtime import thread_registry
from samsara.session_modes import SessionMode

logger = logging.getLogger("Samsara")

_MOUSE_HOTKEY_BUTTONS = ('mouse4', 'mouse5')
_OTHER_HOTKEY_KEYS = (
    ('continuous_hotkey', 'ctrl+alt+d'), ('wake_word_hotkey', 'ctrl+alt+w'),
    ('command_hotkey', 'ctrl+alt+c'), ('memo_hotkey', 'ctrl+alt+m'),
    ('undo_hotkey', 'ctrl+alt+z'), ('correction_hotkey', 'ctrl+alt+r'),
    ('cancel_hotkey', 'escape'),
)
_AVA_CMD_TAP_DEBOUNCE_MS = 40


def _dictation_helper(name):
    """Use dictation's compatibility helpers, including test monkeypatches."""
    return getattr(sys.modules['dictation'], name)


def _raw_key_pressed(name):
    return _dictation_helper('_raw_key_pressed')(name)


def _get_pynput_command_key(button_name):
    return _dictation_helper('_get_pynput_command_key')(button_name)


def _matches_pynput_key(key, target):
    return _dictation_helper('_matches_pynput_key')(key, target)


class HotkeyCluster:
    def parse_hotkey(self, hotkey_str):
        """Parse hotkey string into set of key names.

        Mouse bindings ('mouse4'/'mouse5') have no keys: empty set. They are
        driven by the mouse hook (_on_mouse_button), never by the keyboard.
        """
        if hotkey_str.strip().lower() in _MOUSE_HOTKEY_BUTTONS:
            return set()
        parts = hotkey_str.lower().split('+')
        keys = set()
        for part in parts:
            part = part.strip()
            if part in ('ctrl', 'control'):
                keys.add('ctrl')
            elif part in ('shift',):
                keys.add('shift')
            elif part in ('alt',):
                keys.add('alt')
            elif part in ('win', 'super', 'cmd'):
                keys.add('win')
            else:
                keys.add(part)
        return keys

    def get_key_name(self, key):
        """Get normalized key name"""
        try:
            if hasattr(key, 'char') and key.char:
                return key.char.lower()
            elif hasattr(key, 'name'):
                name = key.name.lower()
                if 'ctrl' in name:
                    return 'ctrl'
                elif 'shift' in name:
                    return 'shift'
                elif 'alt' in name:
                    return 'alt'
                elif 'win' in name or 'super' in name or 'cmd' in name:
                    return 'win'
                return name
        except Exception as e:
            logger.debug(f"Key name normalization failed: {e}")
        return None

    def get_active_keys(self):
        """Get keys pressed within the hotkey window (legacy, kept for compatibility)"""
        now = time.time()
        active_keys = set()
        for key, press_time in list(self.key_press_times.items()):
            if now - press_time < self.hotkey_window:
                active_keys.add(key)
            elif key not in self.current_keys:
                # Clean up old entries
                del self.key_press_times[key]
        # Also include currently held keys
        return active_keys | self.current_keys

    def check_hotkey_state(self, hotkey_str):
        """Check if all keys in a hotkey combo are currently pressed using state-based detection.

        This uses the keyboard library's is_pressed() for reliable simultaneous key detection,
        regardless of the order keys were pressed.
        """
        required_keys = self.parse_hotkey(hotkey_str)
        if not required_keys:
            # A mouse binding is never "held" on the keyboard.
            return False

        for key in required_keys:
            # Hook-free state check -- see _raw_key_pressed (tribunal fix)
            if not _raw_key_pressed(key):
                return False

        return True

    def get_pressed_keys_debug(self):
        """Return a string of currently pressed keys for debugging"""
        pressed = []
        for key in ['ctrl', 'shift', 'alt', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', 'esc']:
            try:
                if _raw_key_pressed(key):
                    pressed.append(key)
            except Exception as e:
                logger.debug(f"is_pressed check failed for {key!r}: {e}")
        return '+'.join(pressed) if pressed else 'none'

    def _hands_free_dictation_commit_available(self) -> bool:
        """Whether the local commit key may paste a buffered thought now.

        This deliberately does not construct a session manager: outside an
        already-active latched hands-free session the key is inert.
        """
        manager = self._session_mode_manager
        return bool(
            self.command_mode_active
            and self.config.get('command_mode', {}).get('mode', 'hold') == 'toggle'
            and manager is not None
            and manager.mode is SessionMode.DICTATE
            and manager.buffer_dictate_until_commit
            and manager.dictate_pending_buffer
        )

    def _commit_pending_hands_free_dictation(self):
        """Commit through SessionModeManager's transactional paste path."""
        if not self._hands_free_dictation_commit_available():
            logger.debug('[HOTKEY] Paste staged thought ignored: no active buffered thought')
            return None

        manager = self._session_mode_manager
        try:
            outcome = manager.commit_pending_dictation()
            logger.info(
                '[SESSION] local dictate commit outcome=%s detail=%s',
                outcome.kind,
                outcome.detail,
            )
            self._handle_session_dispatch_outcome(outcome, "")
            if self._dictate_preview is not None:
                # Same signal shape the voice path uses (see
                # _handle_command_mode_utterance's own on_utterance_final
                # call) -- this trusted local (keyboard) trigger has no
                # spoken utterance text of its own, so final_text is "".
                # commit_pending_dictation() only ever returns a
                # control-style outcome (dictate_committed /
                # dictate_commit_unavailable / dictate_commit_blocked_focus_lock
                # / dictate_commit_failed), never ordinary dictated content,
                # so an empty final_text can never wrongly append or
                # suppress a real transcript line -- only the
                # dictate_committed flag matters here. A successful commit
                # (outcome.kind == "dictate_committed") clears the overlay's
                # already-delivered lines, same as the voice path; any
                # other outcome (nothing pending, blocked focus lock, paste
                # failure) leaves the transcript untouched. Own try/except,
                # separate from the outer one below -- a failure notifying
                # the preview must never be mistaken for the commit itself
                # having failed (which plays the error earcon).
                try:
                    self._dictate_preview.on_utterance_final(
                        "", dictate_committed=(outcome.kind == 'dictate_committed'),
                    )
                except Exception as e:
                    logger.debug(f'[DICTATE-PREVIEW] on_utterance_final failed: {e}')
            return outcome
        except Exception as exc:
            logger.exception('[SESSION] Local dictate commit failed unexpectedly: %s', exc)
            try:
                self.play_sound('error')
            except Exception as sound_exc:
                logger.debug('[SESSION] Local commit error earcon failed: %s', sound_exc)
            return None

    def on_key_press(self, key):
        """Handle key press - uses state-based checking for reliable simultaneous key detection"""
        key_name = self.get_key_name(key)
        if key_name:
            self.current_keys.add(key_name)
            self.key_press_times[key_name] = time.time()

        self._check_command_mode_key(key, pressed=True)

        # While snoozed, still track key state and allow alarm hotkeys,
        # but skip all dictation/recording hotkeys
        if self.snoozed:
            # Check for alarm hotkeys even while snoozed
            if hasattr(self, 'alarm_manager') and self.alarm_manager.is_nagging():
                complete_hotkey = self.alarm_manager.complete_hotkey
                dismiss_hotkey = self.alarm_manager.dismiss_hotkey
                if self.check_hotkey_state(complete_hotkey):
                    self.alarm_manager.complete()
                    self.play_sound('success')
                    return
                if self.check_hotkey_state(dismiss_hotkey):
                    self.alarm_manager.dismiss()
                    self.play_sound('stop')
                    return
            return

        mode = self.config.get('mode', 'hold')

        # Get hotkey configs. A mouse main hotkey whose hook is disabled runs
        # on the keyboard fallback (35) -- a runtime override, never a config edit.
        main_hotkey = getattr(self, '_main_hotkey_override', None) or self.config['hotkey']
        cont_hotkey = self.config.get('continuous_hotkey', 'ctrl+alt+d')
        wake_hotkey = self.config.get('wake_word_hotkey', 'ctrl+alt+w')
        command_hotkey = self.config.get('command_hotkey', 'ctrl+alt+c')
        cancel_hotkey = self.config.get('cancel_hotkey', 'escape')
        memo_hotkey = self.config.get('memo_hotkey', 'ctrl+alt+m')

        if (self.check_hotkey_state(memo_hotkey)
                and not self.hotkey_pressed and not self.recording):
            logger.debug(f"[MEMO] Hotkey detected: {memo_hotkey}")
            self._memo_recording = True
            self.hotkey_pressed = True
            self.play_sound('capture_started', use_winsound=True)
            self.start_recording(streaming=False, play_earcon=False)
            if not self.recording:
                self._memo_recording = False
                self.hotkey_pressed = False
            return

        # Use state-based detection - checks if keys are CURRENTLY held, regardless of press order
        # This is more reliable than event-based tracking for simultaneous key combos

        # Check for command-only hotkey (hold to record, match commands only, no text output)
        if self.check_hotkey_state(command_hotkey) and not self.hotkey_pressed and not self.recording:
            if self._stop_in_flight:
                logger.debug("[HOTKEY] Ignored re-trigger while stop in flight")
                return
            logger.debug(f"[HOTKEY] Command hotkey detected: {command_hotkey}")
            self.hotkey_pressed = True
            self.command_mode_recording = True
            self.start_recording(streaming=False)
            return

        # Undo hotkey (works in any mode, edge-triggered)
        undo_hotkey = self.config.get('undo_hotkey', 'ctrl+alt+z')
        if self.check_hotkey_state(undo_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Undo hotkey detected: {undo_hotkey}")
            self.hotkey_pressed = True
            thread_registry.spawn("dictation.undo_last_dictation", self.undo_last_dictation, daemon=True)
            return

        # Correction report hotkey (works in any mode, edge-triggered)
        correction_hotkey = self.config.get('correction_hotkey', 'ctrl+alt+r')
        if self.check_hotkey_state(correction_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Correction hotkey detected: {correction_hotkey}")
            self.hotkey_pressed = True
            self._schedule_ui(self._report_correction_dialog)
            return

        # Correction CAPTURE hotkey (works in any mode, edge-triggered) --
        # opens samsara/ui/correction_capture_qt.py pre-filled with the last
        # dictation. Distinct from correction_hotkey above (the older
        # threshold-based "Report Correction" dialog).
        capture_correction_hotkey = self.config.get('hotkeys', {}).get('capture_correction', 'ctrl+alt+x')
        if self.check_hotkey_state(capture_correction_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Capture correction hotkey detected: {capture_correction_hotkey}")
            self.hotkey_pressed = True
            thread_registry.spawn(
                "dictation.open_correction_capture", self.open_correction_capture, daemon=True)
            return

        # Check for wake word enable/disable toggle (works in any mode)
        if self.check_hotkey_state(wake_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Wake word hotkey detected: {wake_hotkey}")
            self.hotkey_pressed = True
            new_state = not self.config.get('wake_word_enabled', False)
            thread_registry.spawn("dictation.set_wake_word_enabled", self.set_wake_word_enabled,
                                   args=(new_state,), daemon=True)
            return

        # Trusted local equivalent of saying the sole commit word in the
        # buffered hands-free DICTATE lane. It never starts/stops audio and is
        # inert in hold/toggle recording, COMMAND/AVA, and continuous mode.
        dictate_commit_hotkey = self.config.get(
            'dictate_commit_hotkey', DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
        )
        if (self._hands_free_dictation_commit_available()
                and self.check_hotkey_state(dictate_commit_hotkey)
                and not self.hotkey_pressed):
            logger.debug('[HOTKEY] Paste staged thought detected: %s', dictate_commit_hotkey)
            self.hotkey_pressed = True
            thread_registry.spawn(
                'dictation.commit_pending_hands_free',
                self._commit_pending_hands_free_dictation,
                daemon=True,
            )
            return

        # Check for continuous mode toggle (works in any mode)
        if self.check_hotkey_state(cont_hotkey) and not self.hotkey_pressed:
            logger.debug(f"[HOTKEY] Continuous mode hotkey detected: {cont_hotkey}")
            self.hotkey_pressed = True
            self.toggle_continuous_mode()
            return

        # Continuous-mode manual commit hotkey. ONLY live while continuous
        # mode is actually running with trigger == "key" -- never in
        # hold/toggle modes, and never in continuous mode's default
        # "silence" trigger.
        if (mode == 'continuous' and self.continuous_active
                and self.config.get('continuous_commit_trigger', DEFAULT_CONTINUOUS_COMMIT_TRIGGER) == 'key'):
            commit_hotkey = self.config.get('continuous_commit_hotkey', DEFAULT_CONTINUOUS_COMMIT_HOTKEY)
            if self.check_hotkey_state(commit_hotkey) and not self.hotkey_pressed:
                logger.debug(f"[HOTKEY] Continuous commit hotkey detected: {commit_hotkey}")
                self.hotkey_pressed = True
                if self._continuous_consumer is not None:
                    self._continuous_consumer.commit_now()
                return

        # Check for cancel recording hotkey (only when recording)
        if self.check_hotkey_state(cancel_hotkey) and self.recording:
            logger.debug(f"[HOTKEY] Cancel hotkey detected: {cancel_hotkey}")
            self.cancel_recording()
            return

        # Check for alarm hotkeys (when an alarm is nagging)
        if hasattr(self, 'alarm_manager') and self.alarm_manager.is_nagging():
            complete_hotkey = self.alarm_manager.complete_hotkey
            dismiss_hotkey = self.alarm_manager.dismiss_hotkey

            # Check for complete hotkey (user did the task, gets streak credit)
            if self.check_hotkey_state(complete_hotkey):
                logger.debug(f"[HOTKEY] Alarm complete hotkey detected: {complete_hotkey}")
                self.alarm_manager.complete()
                self.play_sound('success')  # Success sound for completion
                return

            # Check for dismiss hotkey (just silence, no credit, breaks streak)
            if self.check_hotkey_state(dismiss_hotkey):
                logger.debug(f"[HOTKEY] Alarm dismiss hotkey detected: {dismiss_hotkey}")
                self.alarm_manager.dismiss()
                self.play_sound('stop')  # Neutral sound for dismissal
                return

        # Handle main hotkey based on mode.
        # Belt-and-braces: require BOTH the OS keyboard state (via
        # check_hotkey_state) AND pynput's event-tracked self.current_keys
        # to agree that every required key is held. This catches stale
        # OS state from synthesized events leaving e.g. shift "pressed"
        # when the user only physically holds ctrl.
        required_keys = self.parse_hotkey(main_hotkey)
        main_event_held = required_keys.issubset(self.current_keys)
        if self.check_hotkey_state(main_hotkey) and main_event_held:
            if self.hotkey_pressed:
                # A hotkey action is in progress. If a key-down still owns it
                # (this press, or another combo physically held) this is
                # auto-repeat: ignore. Otherwise the flag was stranded by a
                # lost release; a fresh press must never be ignored for good.
                if getattr(self, '_hold_down_key', False) or self._other_hotkey_held():
                    return
                logger.warning("[HOTKEY] hotkey_pressed was stranded (no key down owns it); "
                               "clearing it so this press counts | %s", self._hotkey_state_text())
                self.hotkey_pressed = False
            if self._stop_in_flight:
                logger.debug("[HOTKEY] Ignored re-trigger while stop in flight | %s",
                             self._hotkey_state_text())
                return
            # Toggle-off comes BEFORE the ownership guard (2026-09-13, matching
            # _on_main_hotkey_mouse): the guard below is about STARTING a
            # recording while another one owns capture. toggle_active is only
            # ever set by the main hotkey itself (key or mouse path), so a
            # main-hotkey press may always end it (28: no owner check -- a
            # toggle the other path started must not leave this press dead).
            # Capture owned by a streaming / wake / command session has
            # toggle_active False and is still protected below.
            if (mode == 'toggle' and self.toggle_active
                    and getattr(self, '_streaming_session', None) is None):
                logger.debug(f"[HOTKEY] Main hotkey toggle-off: {main_hotkey}")
                # The toggle-off press is a physical key-down too: mark it so
                # auto-repeat cannot start a new toggle before the release.
                self.hotkey_pressed = True
                self._hold_down_key = True
                self._main_hotkey_toggle_off('key')
                return
            if self.recording or getattr(self, '_streaming_session', None) is not None:
                logger.info("[HOTKEY] Main hotkey ignored -- another recording owns capture | %s",
                            self._hotkey_state_text())
                return
            logger.debug(f"[HOTKEY] Main hotkey detected: {main_hotkey} (mode: {mode})")
            self._main_hotkey_source = 'key'
            self.hotkey_pressed = True
            self._hold_down_key = True
            if mode == 'hold':
                # Ctrl+Shift always drives batch mode -- streaming uses
                # CapsLock as its dedicated hotkey.
                self.start_recording(streaming=False)
            elif mode == 'toggle':
                self.toggle_active = True
                self.start_recording(streaming=False)
                if not self.recording and self._start_recording_declined():
                    # No capture started (model still loading, app stopping):
                    # no toggle is on, so the next press starts one instead of
                    # "stopping" nothing.
                    logger.debug("[HOTKEY] Toggle start declined by start_recording | %s",
                                 self._hotkey_state_text())
                    self.toggle_active = False
            elif mode == 'continuous':
                # In continuous mode, main hotkey toggles continuous listening
                self.toggle_continuous_mode()

    def _start_recording_declined(self) -> bool:
        """The reasons start_recording returns without capturing that the
        hotkey guards do not already cover (model not loaded, app stopping)."""
        return (not getattr(self, 'model_loaded', True)
                or not getattr(self, '_running', True))

    def _other_hotkey_held(self) -> bool:
        """True while any configured non-main keyboard hotkey is physically
        held (OS state) -- its action legitimately owns hotkey_pressed."""
        for key, default in _OTHER_HOTKEY_KEYS:
            combo = self.config.get(key, default)
            try:
                if combo and self.check_hotkey_state(combo):
                    return True
            except Exception as e:
                logger.debug(f"_other_hotkey_held({combo!r}): {e}")
        commit = self.config.get('continuous_commit_hotkey')
        try:
            if commit and self.check_hotkey_state(commit):
                return True
        except Exception as e:
            logger.debug(f"_other_hotkey_held(commit): {e}")
        return False

    def _hotkey_state_text(self) -> str:
        """The hotkey state machine in one line, for every guard's log."""
        g = lambda name, default=False: getattr(self, name, default)
        cfg = getattr(self, 'config', {}) or {}
        return (
            f"snoozed={g('snoozed')} hotkey_pressed={g('hotkey_pressed')}"
            f" hold_down_key={g('_hold_down_key')} hold_down_mouse={g('_hold_down_mouse')}"
            f" source={g('_main_hotkey_source', 'key')} recording={g('recording')}"
            f" streaming={g('_streaming_session', None) is not None}"
            f" stop_in_flight={g('_stop_in_flight')} toggle_active={g('toggle_active')}"
            f" mode={cfg.get('mode', 'hold')} hotkey={cfg.get('hotkey')!r}"
        )

    def on_key_release(self, key):
        """Handle key release - uses state-based checking for reliable detection"""
        key_name = self.get_key_name(key)
        if key_name and key_name in self.current_keys:
            self.current_keys.discard(key_name)

        self._check_command_mode_key(key, pressed=False)

        mode = self.config.get('mode', 'hold')

        # Get hotkey configs (runtime keyboard fallback first, see on_key_press)
        main_hotkey = getattr(self, '_main_hotkey_override', None) or self.config['hotkey']
        cont_hotkey = self.config.get('continuous_hotkey', 'ctrl+alt+d')
        wake_hotkey = self.config.get('wake_word_hotkey', 'ctrl+alt+w')
        command_hotkey = self.config.get('command_hotkey', 'ctrl+alt+c')
        memo_hotkey = self.config.get('memo_hotkey', 'ctrl+alt+m')

        # Reset hotkey flag when no hotkey combo is currently pressed
        # Use state-based checking for reliable detection. The MAIN combo
        # counts as released when EITHER the OS state or pynput's event-
        # tracked current_keys says so (the press side requires both to
        # agree that it is held): a stale GetAsyncKeyState bit can no longer
        # strand the press flags.
        main_pressed = (self.check_hotkey_state(main_hotkey)
                        and self.parse_hotkey(main_hotkey).issubset(self.current_keys))
        cont_pressed = self.check_hotkey_state(cont_hotkey)
        wake_pressed = self.check_hotkey_state(wake_hotkey)
        command_pressed = self.check_hotkey_state(command_hotkey)
        memo_pressed = self.check_hotkey_state(memo_hotkey)

        def _deferred_stop():
            try:
                self.stop_recording()
            finally:
                self._stop_in_flight = False

        # The keyboard's main-hotkey key-down is over once the combo is up,
        # whatever other combos are held.
        if getattr(self, '_hold_down_key', False) and not main_pressed:
            self._hold_down_key = False

        if not main_pressed and not cont_pressed and not wake_pressed and not command_pressed and not memo_pressed:
            if self.hotkey_pressed:
                if self._memo_recording and self.recording:
                    logger.debug("[MEMO] Hotkey released, stopping recording")
                    self._stop_in_flight = True
                    thread_registry.spawn('stop-memo', _deferred_stop, daemon=True)
                    self.hotkey_pressed = False
                elif self.command_mode_recording and self.recording:
                    logger.debug(f"[HOTKEY] Command hotkey released, stopping recording")
                    flight_recorder.record(
                        'hold_recording.stop_triggered',
                        reason='command_hotkey_release', key=key_name,
                        main_hotkey=main_hotkey, command_hotkey=command_hotkey,
                    )
                    self._stop_in_flight = True
                    thread_registry.spawn('stop-rec', _deferred_stop, daemon=True)
                    self.hotkey_pressed = False
                elif (mode == 'hold' and self.recording
                        and getattr(self, '_main_hotkey_source', 'key') == 'key'):
                    # The keyboard's hold press ended. A mouse-held recording
                    # never sets hotkey_pressed and keeps source 'mouse', so it
                    # is untouched here: its own release stops it.
                    logger.debug(f"[HOTKEY] Main hotkey released, stopping recording")
                    flight_recorder.record(
                        'hold_recording.stop_triggered',
                        reason='main_hotkey_release', key=key_name,
                        main_hotkey=main_hotkey,
                    )
                    self._stop_in_flight = True
                    thread_registry.spawn('stop-rec', _deferred_stop, daemon=True)
                    self.hotkey_pressed = False
                else:
                    logger.debug("[HOTKEY] Keyboard hotkey released: nothing to stop | %s",
                                 self._hotkey_state_text())
                    self.hotkey_pressed = False

    # ---- CapsLock streaming hotkey --------------------------------------

    def _install_capslock_hook(self):
        """Hook CapsLock with the keyboard library so it drives streaming
        dictation without ever toggling the system caps state.

        suppress=True means the OS never sees the CapsLock event -- no
        toggle, no LED change, no caps. Our callback decides whether to
        start/stop streaming based on the live streaming_mode config.

        IMPORTANT: This hook is only installed when streaming_mode is
        actually enabled. When streaming is off, we leave CapsLock alone
        so it works as a normal Windows toggle. set_streaming_mode()
        installs/uninstalls the hook dynamically when the user toggles it.

        We register an atexit cleanup so the hook is released even if
        Samsara crashes or is killed via Task Manager -- without this
        the user can be left with a CapsLock key the OS thinks is
        permanently consumed."""
        # Bail out if streaming mode is off -- no need to grab CapsLock
        if not self.config.get('streaming_mode', False):
            self._capslock_hook = None
            return

        try:
            self._capslock_hook = keyboard.hook_key(
                'caps lock', self._on_capslock_event, suppress=True)
        except Exception as e:
            logger.error(f"[CAPSLOCK] Failed to install hook: {e}")
            self._capslock_hook = None
            return

        import atexit
        hook_ref = self._capslock_hook

        def _cleanup_capslock_hook():
            try:
                keyboard.unhook(hook_ref)
            except Exception as e:
                logger.debug(f"[CAPSLOCK] atexit unhook failed: {e}")

        atexit.register(_cleanup_capslock_hook)

    def _uninstall_capslock_hook(self):
        """Release the CapsLock hook so the OS gets the key back. Called
        when streaming_mode is toggled off so CapsLock works normally."""
        if getattr(self, '_capslock_hook', None) is None:
            return
        try:
            keyboard.unhook(self._capslock_hook)
            logger.info("[CAPSLOCK] Hook released — CapsLock returned to OS")
        except Exception as e:
            logger.exception(f"[CAPSLOCK] Failed to release hook: {e}")
        self._capslock_hook = None
        self._capslock_held = False

    def _on_capslock_event(self, event):
        """Hooked CapsLock handler. Runs on the keyboard library's hook
        thread -- spawn worker threads for blocking work."""
        try:
            if self.snoozed:
                return
            if not self.config.get('streaming_mode', False):
                return  # event still suppressed; we just don't trigger
            if not self.model_loaded:
                return

            if event.event_type == keyboard.KEY_DOWN:
                if self._capslock_held:
                    return  # ignore auto-repeat while held
                self._capslock_held = True
                thread_registry.spawn(
                    "capslock-start", self._capslock_start_streaming, daemon=True)
            elif event.event_type == keyboard.KEY_UP:
                if not self._capslock_held:
                    return
                self._capslock_held = False
                thread_registry.spawn(
                    "capslock-stop", self._capslock_stop_streaming, daemon=True)
        except Exception as e:
            logger.exception(f"[CAPSLOCK] event handler crashed: {e}")

    def _capslock_start_streaming(self):
        """Worker: start a streaming-mode recording. Wrapped so we can
        guard against re-entry if the user hammers CapsLock."""
        try:
            with self._capslock_lifecycle_lock:
                if not self.config.get('streaming_mode', False):
                    return
                if self.recording or getattr(self, '_streaming_session', None) is not None:
                    logger.info("[CAPSLOCK] streaming start ignored -- another recording owns capture")
                    return
                logger.info("[CAPSLOCK] press -> streaming start")
                self.start_recording(streaming=True)
                self._capslock_streaming_session = getattr(
                    self, '_streaming_session', None,
                )
        except Exception as e:
            logger.exception(f"[CAPSLOCK] start failed: {e}")

    def _capslock_stop_streaming(self):
        """Worker: stop the streaming recording on CapsLock release."""
        try:
            with self._capslock_lifecycle_lock:
                owned = getattr(self, '_capslock_streaming_session', None)
                if (owned is None
                        or getattr(self, '_streaming_session', None) is not owned
                        or not self.recording):
                    return
                self._capslock_streaming_session = None
                logger.info("[CAPSLOCK] release -> streaming stop")
                self.stop_recording()
        except Exception as e:
            logger.exception(f"[CAPSLOCK] stop failed: {e}")

    # ---- Mouse 4/5: command mode and the main record hotkey ---------------

    def _mouse_hook_bindings(self):
        """(bound_buttons, suppress_buttons) the mouse hook needs right now.

        command_mode.button honours its suppress_button flag; a mouse main
        hotkey always suppresses (the OS must never see a record click).
        """
        buttons, suppress = set(), set()
        cfg = self.config.get('command_mode', {}) or {}
        btn = cfg.get('button', 'rctrl')
        if btn in _MOUSE_HOTKEY_BUTTONS:
            buttons.add(btn)
            if cfg.get('suppress_button', True):
                suppress.add(btn)
        hotkey = str(self.config.get('hotkey', '') or '').strip().lower()
        if hotkey in _MOUSE_HOTKEY_BUTTONS:
            buttons.add(hotkey)
            suppress.add(hotkey)
        return frozenset(buttons), frozenset(suppress)

    def _install_mouse_listener(self):
        """Start the one Win32 low-level mouse hook shared by every Mouse 4/5
        binding (command_mode.button and/or the main hotkey).

        Not installed when nothing is bound to a mouse button. Keyboard
        sources (rctrl, f13, key combos) are handled by on_key_press/release.
        """
        buttons, suppress = self._mouse_hook_bindings()
        if not buttons:
            self._mouse_hook = None
            return

        try:
            from samsara.mouse_hook import MouseHook
            self._mouse_hook = MouseHook(
                on_button_event=self._on_mouse_button,
                suppress_buttons=suppress,
                # getattr: partial app stand-ins (tests) may not carry it.
                on_hook_failed=getattr(self, '_on_mouse_hook_failed', None),
            )
            self._mouse_hook.start()
            if not getattr(self._mouse_hook, 'installed', True):
                # start() returns whether or not the hook thread managed to
                # install (28): say so in the log instead of "started", and
                # drop the object so refresh_mouse_hook() can try again.
                install_error = getattr(self._mouse_hook, 'install_error', '') or 'unknown reason'
                logger.error(
                    f"[MOUSE] Mouse hook did NOT install (bound={sorted(buttons)}): {install_error}"
                )
                self._mouse_hook = None
                fallback = getattr(self, '_mouse_fallback_to_keyboard', None)
                if callable(fallback):
                    fallback("mouse hook did not install", install_error)
                return
            logger.info(
                f"[MOUSE] Mouse hook started (bound={sorted(buttons)}, suppress={sorted(suppress)})"
            )
        except Exception as e:
            logger.exception(f"[MOUSE] Mouse hook failed to start: {e}")
            self._mouse_hook = None

    def refresh_mouse_hook(self, rearm=True):
        """Install / reinstall / remove the mouse hook to match the effective
        main hotkey and command_mode binding.

        Called from every path that changes them: settings apply (settings_qt),
        update_config, the external config-file watcher (rearm=False) and the
        tray / Settings "Re-enable mouse hotkey" (35). The hook forwards every
        X button event and the callback routes by live config, so only the
        need for a hook, its suppress set and its liveness matter.

        rearm=True (a user action) clears a released / given-up state and
        reinstalls a missing or dead hook for unchanged bindings -- the owner's
        "set Mouse 4, nothing happened, Apply again" case. rearm=False leaves a
        deliberate release in force until the bindings really change.
        """
        buttons, suppress = self._mouse_hook_bindings()
        hook = getattr(self, '_mouse_hook', None)
        released = getattr(self, '_mouse_hook_released_bindings', None)
        if released is not None:
            if not rearm and hook is None and (buttons, suppress) == released:
                return
        # Whatever happens next, the old fallback / release no longer applies.
        # (Plain attributes, not a helper: partial app stand-ins bind this method.)
        if released is not None or not buttons:
            self._mouse_hook_released_bindings = None
            self._main_hotkey_override = None
            self._mouse_hotkey_disabled_reason = None
        if hook is None and not buttons:
            return
        if (hook is not None and buttons and hook.suppress_buttons == suppress
                and getattr(hook, 'installed', True)):
            return
        if hook is not None:
            try:
                hook.stop()
            except Exception as e:
                logger.exception(f"[MOUSE] Mouse hook stop failed: {e}")
            self._mouse_hook = None
        self._install_mouse_listener()

    def _on_mouse_hook_failed(self, reason):
        """MouseHook gave up after losing the hook repeatedly (dispatcher thread).
        It has already stopped suppressing and unhooked; fall back to the
        keyboard hotkey and say so."""
        logger.error(f"[MOUSE] mouse hook disabled: {reason}")
        self._mouse_hook = None
        self._mouse_hook_released_bindings = self._mouse_hook_bindings()
        self._mouse_fallback_to_keyboard("hands-free mouse control disabled", reason)

    def release_mouse_buttons(self):
        """Tray "Release mouse buttons": uninstall the mouse hook immediately.

        Panic release -- the owner must never need end-process to get the
        mouse back. The hook stays off until the mouse bindings change in
        settings or Samsara restarts; the main hotkey falls back to the
        keyboard hotkey meanwhile.
        """
        hook = getattr(self, '_mouse_hook', None)
        self._mouse_hook = None
        if hook is not None:
            try:
                hook.release("Release mouse buttons (tray)")
            except Exception as e:
                logger.exception(f"[MOUSE] release failed: {e}")
        logger.warning("[MOUSE] mouse buttons released by the user")
        self._mouse_hook_released_bindings = self._mouse_hook_bindings()
        self._mouse_fallback_to_keyboard("mouse buttons released", "released from the tray")

    def _mouse_fallback_to_keyboard(self, why, reason=''):
        """The mouse hook is gone. Loud and reversible (35):

        - a mouse main hotkey runs on the default keyboard hotkey through a
          RUNTIME override (_main_hotkey_override); config['hotkey'] still
          says mouse4, so Settings shows the user's choice and never saves
          the fallback;
        - a mouse-held recording is stopped (its release will never arrive);
        - an outcome chip, a tray balloon, the tray menu ("Re-enable mouse
          hotkey") and the Settings hotkey row (mouse_hotkey_status) all say
          the mouse hotkey is disabled and why.
        """
        from samsara import config_defaults
        hotkey = str(self.config.get('hotkey', '') or '').strip().lower()
        fallback = config_defaults.DEFAULTS['hotkey']
        self._mouse_hotkey_disabled_reason = f"{why}: {reason}" if reason else why
        if hotkey in _MOUSE_HOTKEY_BUTTONS:
            self._main_hotkey_override = fallback
            logger.warning(f"[MOUSE] {why}: main hotkey {hotkey} runs on {fallback} until re-enabled "
                           f"(config unchanged, not saved)")
        if getattr(self, '_hold_down_mouse', False):
            self._hold_down_mouse = False
            if getattr(self, '_main_hotkey_source', 'key') == 'mouse' and getattr(self, 'recording', False):
                self._main_hotkey_source = 'key'
                thread_registry.spawn('stop-rec', self.stop_recording, daemon=True)
        try:
            self._show_outcome_chip(f"mouse hotkey off - using {fallback}", "warning")
        except Exception as e:
            logger.debug(f"[MOUSE] fallback chip failed: {e}")
        notify = getattr(getattr(self, 'tray_icon', None), 'notify_warning', None)
        if callable(notify):
            try:
                notify("Samsara mouse hotkey disabled",
                       f"{self._mouse_hotkey_disabled_reason}. {fallback} records meanwhile. "
                       f"Tray menu > Re-enable mouse hotkey to try again.")
            except Exception as e:
                logger.debug(f"[MOUSE] fallback balloon failed: {e}")

    def mouse_hotkey_status(self):
        """{'state': 'n/a' | 'active' | 'disabled', 'reason', 'fallback', 'buttons'}
        for the tray menu and the Settings hotkey row (35)."""
        buttons, _suppress = self._mouse_hook_bindings()
        if not buttons:
            return {'state': 'n/a', 'reason': '', 'fallback': None, 'buttons': []}
        hook = getattr(self, '_mouse_hook', None)
        if hook is not None and getattr(hook, 'installed', False):
            return {'state': 'active', 'reason': '', 'fallback': None, 'buttons': sorted(buttons)}
        reason = (getattr(self, '_mouse_hotkey_disabled_reason', None)
                  or getattr(hook, 'install_error', '') or 'mouse hook not installed')
        return {'state': 'disabled', 'reason': reason,
                'fallback': getattr(self, '_main_hotkey_override', None), 'buttons': sorted(buttons)}

    def reenable_mouse_hotkey(self):
        """Tray / Settings "Re-enable mouse hotkey": reinstall the hook now,
        without a settings round-trip or a restart. A no-op when the hook is
        already active. Returns the resulting mouse_hotkey_status()['state']."""
        status = self.mouse_hotkey_status()
        if status['state'] == 'active':
            logger.info("[MOUSE] re-enable mouse hotkey: already active, nothing to do")
            return 'active'
        if status['state'] == 'n/a':
            logger.info("[MOUSE] re-enable mouse hotkey: no mouse button is bound")
            return 'n/a'
        logger.info("[MOUSE] re-enable mouse hotkey requested (%s)", status['reason'])
        self.refresh_mouse_hook(rearm=True)
        after = self.mouse_hotkey_status()
        if after['state'] == 'active':
            try:
                self._show_outcome_chip("mouse hotkey active", "success")
            except Exception as e:
                logger.debug(f"[MOUSE] re-enable chip failed: {e}")
        else:
            logger.warning("[MOUSE] re-enable mouse hotkey failed: %s", after['reason'])
        return after['state']

    def _on_mouse_button(self, button_name, pressed):
        """Single mouse-hook callback: command mode, then the main hotkey.

        Runs on the MouseHook DISPATCHER thread, never the Win32 hook thread
        (32): start_recording() here may take hundreds of ms and must not
        hold up the system's mouse input.

        Logs every event on entry (28): the owner could not see this path
        at all before, because every guard returned in silence.
        """
        hotkey = str(self.config.get('hotkey', '') or '').strip().lower()
        logger.debug("[MOUSE] event button=%s pressed=%s main_hotkey=%r | %s",
                     button_name, pressed, hotkey, self._hotkey_state_text())
        try:
            self._on_command_button(button_name, pressed)
        except Exception as e:
            # Command mode must never mute the main hotkey behind it.
            logger.exception(f"[MOUSE] command-mode handler failed: {e}")
        if button_name == hotkey:
            self._on_main_hotkey_mouse(pressed)
        else:
            logger.debug("[MOUSE] %s is not the main hotkey (%r): main path skipped",
                         button_name, hotkey)

    def _main_hotkey_toggle_off(self, source: str) -> None:
        """End the toggle recording the main hotkey started -- shared by the
        keyboard path (on_key_press) and the mouse path (_on_main_hotkey_mouse).
        Toggle only; hold and continuous never call this.

        Touches toggle state only. The physical-press flags belong to the
        caller (the key path marks its key-down before calling; the mouse
        path's edge trigger already covers auto-repeat), so a toggle-off can
        never leave a press flag behind for the other path to trip on.
        """
        self._main_hotkey_source = source
        self.toggle_active = False
        try:
            self.stop_recording()
        finally:
            # Nothing is capturing for the hotkey any more. stop_recording
            # leaves this set while hotkey_pressed is True (a held key), which
            # for a toggle-off is exactly the case -- and a stale True keeps
            # the wake listener deaf (wake_consumer._post_wake_admission).
            self._hotkey_recording = False

    def _mouse_guard(self, why: str) -> None:
        logger.debug("[HOTKEY] Mouse main hotkey: %s | %s", why, self._hotkey_state_text())

    def _on_main_hotkey_mouse(self, pressed):
        """Main record hotkey bound to Mouse 4/5 -- the keyboard main-hotkey
        semantics (on_key_press / on_key_release) driven by the button.

        Edge-triggered on press through _hold_down_mouse, the mouse's OWN
        physical-press flag: it never reads or writes hotkey_pressed, so a
        keyboard flag stranded by a lost release cannot mute the mouse and a
        lost mouse release cannot mute the keyboard. Guards match the keyboard
        path: snoozed, another recording owning capture, stop in flight; in
        toggle mode a press ends the toggle first (before any guard about
        STARTING). Every guard logs what it saw.
        """
        mode = self.config.get('mode', 'hold')
        if pressed:
            if getattr(self, '_hold_down_mouse', False):
                self._mouse_guard("press ignored: button already down (auto-repeat)")
                return
            self._hold_down_mouse = True
            if self.snoozed:
                self._mouse_guard("press ignored: snoozed")
                return
            if mode == 'toggle' and self.toggle_active:
                logger.debug("[HOTKEY] Main hotkey (mouse) toggle-off | %s", self._hotkey_state_text())
                self._main_hotkey_toggle_off('mouse')
                return
            if self.recording or getattr(self, '_streaming_session', None) is not None:
                logger.info("[HOTKEY] Mouse main hotkey ignored -- another recording owns capture | %s",
                            self._hotkey_state_text())
                return
            if self._stop_in_flight:
                self._mouse_guard("press ignored: stop in flight")
                return
            logger.debug(f"[HOTKEY] Main hotkey (mouse) pressed: {self.config.get('hotkey')} (mode: {mode})")
            self._main_hotkey_source = 'mouse'
            if mode == 'hold':
                self.start_recording(streaming=False)
                if not self.recording:
                    self._mouse_guard("start_recording declined the hold press")
            elif mode == 'toggle':
                self.toggle_active = True
                self.start_recording(streaming=False)
                if not self.recording and self._start_recording_declined():
                    self._mouse_guard("start_recording declined the toggle press; toggle stays off")
                    self.toggle_active = False
            elif mode == 'continuous':
                self.toggle_continuous_mode()
            return

        if not getattr(self, '_hold_down_mouse', False):
            self._mouse_guard("release ignored: no press was seen")
            return
        self._hold_down_mouse = False
        if self._main_hotkey_source != 'mouse':
            self._mouse_guard("release: the keyboard owns the main hotkey, nothing to do")
            return
        if mode == 'toggle' and self.toggle_active:
            # A mouse-started toggle keeps 'mouse' as its owner until it is
            # stopped, so the keyboard path (on_key_press) leaves it alone.
            self._mouse_guard("release: toggle stays on until the next press")
            return
        self._main_hotkey_source = 'key'
        if mode == 'hold' and self.recording:
            def _deferred_stop():
                try:
                    self.stop_recording()
                finally:
                    self._stop_in_flight = False

            logger.debug("[HOTKEY] Main hotkey (mouse) released, stopping recording")
            flight_recorder.record(
                'hold_recording.stop_triggered',
                reason='main_hotkey_mouse_release',
                main_hotkey=self.config.get('hotkey'),
            )
            self._stop_in_flight = True
            thread_registry.spawn('stop-rec', _deferred_stop, daemon=True)
            return
        self._mouse_guard("release: nothing recording, nothing to stop")

    def _on_command_button(self, button_name, pressed):
        """Mouse hook callback — routes the configured button to command mode."""
        cfg = self.config.get('command_mode', {})
        if not cfg.get('enabled', False):
            return
        if button_name != cfg.get('button', 'rctrl'):
            return
        mode = cfg.get('mode', 'hold')
        if mode == 'hold':
            if pressed:
                self.enter_command_mode()
            else:
                self.exit_command_mode()
        else:  # toggle
            if pressed:
                if self.command_mode_active:
                    self.exit_command_mode()
                else:
                    self.enter_command_mode()

    def _check_command_mode_key(self, key, pressed: bool) -> None:
        """Route keyboard events to the command mode state machine.

        Called from on_key_press / on_key_release for every key event.
        No-ops unless command_mode.button is a keyboard source.
        """
        # SURGICAL ALT GUARD (Ava Front Door spec v2, D3 "Windows Alt-key
        # guard" -- Auditor catch): Left-Alt is the OS menu key. Any
        # keypress that is NOT the Ava command session's own toggle key,
        # while that session is latched, drops the latch IMMEDIATELY.
        # This keyboard listener has never suppressed OS-level key
        # delivery (pynput_keyboard.Listener is constructed without
        # suppress=True -- see its construction site), so Alt+<key>
        # navigation (Alt+Tab, Alt+F4, menu access) was never literally
        # blocked; the real risk this guard closes is SOFTWARE state --
        # without it, an unrelated Alt-combo (or ANY other key) left the
        # session latched and listening in the background indefinitely,
        # exactly the kind of latch a motor-impaired user mid alt-tabbing
        # must not be stuck fighting. Checked first, before any other
        # per-feature branch below, and deliberately does not `return` --
        # the key event still falls through to every other handler this
        # method and on_key_press/on_key_release already run for it.
        if self.ava_command_session_active:
            ava_cmd_cfg = self.config.get('ava_command_session', {})
            ava_cmd_key_name = ava_cmd_cfg.get('key', 'left_alt')
            ava_cmd_target = _get_pynput_command_key(ava_cmd_key_name)
            is_own_key = (
                ava_cmd_target is not None and _matches_pynput_key(key, ava_cmd_target)
            )
            if not is_own_key:
                logger.info(
                    "[AVA-CMD] Surgical latch-drop: non-session key pressed while latched"
                )
                self.exit_ava_command_session()

        cfg = self.config.get('command_mode', {})
        cmd_enabled = cfg.get('enabled', False)
        btn_name = cfg.get('button', 'rctrl')
        is_mouse = btn_name in ('mouse4', 'mouse5')

        # Command mode keyboard handling (skip if disabled or mouse source)
        if cmd_enabled and not is_mouse:
            target = _get_pynput_command_key(btn_name)
            if _matches_pynput_key(key, target):
                mode = cfg.get('mode', 'hold')
                # Edge-trigger: collapse OS key auto-repeat (and any phantom
                # press/release pairs from the LL hook) to a single rising edge
                # on press and a single falling edge on release.  Without this,
                # a held key fires enter/exit ~30x/sec → earcon chirp storm.
                if pressed:
                    if self._command_mode_key_held:
                        return  # auto-repeat — already handled the real press
                    self._command_mode_key_held = True
                else:
                    if not self._command_mode_key_held:
                        return  # phantom release with no matching real press
                    self._command_mode_key_held = False
                if mode == 'hold':
                    if pressed:
                        self._dispatch_session_transition(self.enter_command_mode)
                    else:
                        self._dispatch_session_transition(self.exit_command_mode)
                else:  # toggle
                    if pressed:
                        if self.command_mode_active:
                            self._dispatch_session_transition(self.exit_command_mode)
                        else:
                            self._dispatch_session_transition(self.enter_command_mode)
                return

        # Right Alt → Ava mode (mutual exclusion with command mode)
        if not self.config.get('ava_mode_enabled', True):
            return
        ava_key_name = self.config.get('ava_mode_key', 'right_alt')
        ava_target = _get_pynput_command_key(ava_key_name)
        if ava_target is not None and _matches_pynput_key(key, ava_target):
            if pressed:
                if self._ava_mode_key_held:
                    return  # auto-repeat
                self._ava_mode_key_held = True
            else:
                if not self._ava_mode_key_held:
                    return  # phantom release
                self._ava_mode_key_held = False
            if pressed and not self.ava_mode_active and not self.command_mode_active:
                self.enter_ava_mode()
            elif not pressed and self.ava_mode_active:
                self.exit_ava_mode()
            return

        # Ava command session (D3, TAP-to-toggle-on-RELEASE; mutual
        # exclusion with command mode and ava mode)
        #
        # Tap-toggle debounce (2026-07-23 G3 live-test finding; supersedes
        # the 2026-07-19 incident's Fix 3 / P1 guard for THIS control
        # only): D3 is a tap-toggle, not a hold control like Right-Alt
        # Ava -- a clean, fast press-release IS the intended gesture, not
        # an accidental one. Reusing Right-Alt's 200ms
        # command_mode.enter_debounce_ms (designed to reject an
        # accidental brief tap on a control meant to be HELD) silently
        # ate legitimate taps here instead: an 80ms or 150ms press-release
        # never toggled the session at all. Uses _AVA_CMD_TAP_DEBOUNCE_MS
        # (~40ms) instead -- just enough to reject genuine keyboard-
        # hardware contact bounce / phantom double-fire, not human tap
        # speed. Right-Alt Ava's own >=200ms hold guard (just above) is
        # UNCHANGED. A press only arms and timestamps the key; the toggle
        # itself fires on release, and only if held for at least the tap
        # floor -- symmetric for BOTH activation and deactivation presses.
        ava_cmd_cfg = self.config.get('ava_command_session', {})
        if ava_cmd_cfg.get('enabled', True):
            ava_cmd_key_name = ava_cmd_cfg.get('key', 'left_alt')
            ava_cmd_target = _get_pynput_command_key(ava_cmd_key_name)
            if ava_cmd_target is not None and _matches_pynput_key(key, ava_cmd_target):
                if pressed:
                    if self._ava_cmd_key_held:
                        return  # auto-repeat -- already armed by the real press
                    self._ava_cmd_key_held = True
                    self._ava_cmd_key_press_time = time.monotonic()
                    return
                # Release
                if not self._ava_cmd_key_held:
                    return  # phantom release with no matching real press
                self._ava_cmd_key_held = False
                hold_ms = (time.monotonic() - self._ava_cmd_key_press_time) * 1000
                if hold_ms < _AVA_CMD_TAP_DEBOUNCE_MS:
                    logger.debug(f"[AVA-CMD] Ghost tap ({hold_ms:.0f}ms) — ignored")
                    return
                if self.ava_command_session_active:
                    self.exit_ava_command_session()
                else:
                    self.enter_ava_command_session()
