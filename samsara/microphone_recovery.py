"""Device recovery orchestration, separate from the desktop bootstrap.

Endpoint callbacks only schedule work. The existing output watcher thread does
the debounced work; only a missing input is polled. Saved preferences never
become a cache of PortAudio's current numeric device IDs.
"""
from contextlib import nullcontext, ExitStack, contextmanager
import logging
import json
import sys
import threading
import time

from samsara.audio_engine import device_resolver

log = logging.getLogger('Samsara')
# Stand-in wording (550), reviewed in ASK.json.
MISSING_MESSAGE = 'No microphone. Connect a microphone to dictate.'
FALLBACK_MESSAGE = 'Your chosen microphone is not connected. Using the system default microphone.'


def needs_setup(config_path, *, sleep=time.sleep):
    """A completed setup stays complete when no microphone is connected."""
    if not config_path.exists():
        return True
    for attempt in range(3):
        try:
            with config_path.open('r') as stream:
                config = json.load(stream)
            return not bool(config.get('first_run_complete', False))
        except OSError as exc:
            log.warning('[CONFIG] pre-wizard check attempt %d failed: %s', attempt + 1, exc)
            if attempt < 2:
                sleep(0.1)
        except (ValueError, AttributeError):
            return True
    log.warning('[CONFIG] pre-wizard check failed 3x -- skipping wizard (assuming valid config)')
    return False


def reconcile(app, sd):
    try:
        app._capture_device, app._using_default_mic = device_resolver.select_input(sd, app.config)
    except device_resolver.DeviceNotFound:
        app._capture_device, app._using_default_mic = None, False


def input_available(app, sd):
    reconcile(app, sd)
    return app._capture_device is not None


def calibration_identity(app, sd):
    index, _fallback = device_resolver.select_input(sd, app.config)
    info = sd.query_devices(index, kind='input')
    return {'device_id': index, 'device_name': info['name'],
            'capture_rate': int(app.capture_rate),
            'multiplier': float(app.config.get('cal_multiplier', 3.0))}


def set_state(app, lost, *, notify=False):
    changed = getattr(app, '_no_microphone', None) != bool(lost)
    app._no_microphone = bool(lost)
    indicator = getattr(app, 'listening_indicator', None)
    # Repeat the idempotent update: the chosen display/tray may be constructed
    # after ACE's initial failure, or replaced when the display setting changes.
    if indicator is not None:
        app._schedule_ui(indicator.set_device_lost, bool(lost))
    tray = getattr(app, 'tray_icon', None)
    if tray is not None:
        if lost:
            tray.title = 'Samsara - No microphone'
        elif getattr(app, '_using_default_mic', False):
            tray.title = 'Samsara - Using system default microphone; chosen microphone not connected'
        elif changed:
            app._update_tray_tooltip()
        # One tray warning per outage; every deliberate hold still gets a chip.
        if lost and not getattr(app, '_mic_warning_shown', False):
            tray.notify_warning('No microphone', MISSING_MESSAGE)
            app._mic_warning_shown = True
        elif not lost and getattr(app, '_using_default_mic', False):
            if not getattr(app, '_mic_fallback_shown', False):
                tray.notify_warning('Microphone', FALLBACK_MESSAGE)
                app._mic_fallback_shown = True
    if not lost:
        app._mic_warning_shown = False
    if not getattr(app, '_using_default_mic', False):
        app._mic_fallback_shown = False
    if lost and notify:
        app._show_outcome_chip(MISSING_MESSAGE, 'error')
    if changed:
        log.info('[MIC] Capture state: %s', 'unavailable' if lost else 'ready')


from samsara.portaudio_guard import RescanDeferred


def capture_busy(app):
    """A live utterance/listening session must keep its capture stream."""
    return any(bool(getattr(app, name, False)) for name in
               ('recording', 'continuous_active', 'wake_word_active', 'command_mode_active',
                'ava_mode_active', 'ava_command_session_active'))


def output_busy(app):
    tts = getattr(app, 'tts_engine', None)
    local = getattr(tts, '_fallback_engine', None)
    for owner in (tts, local):
        if owner is not None and owner.is_speaking():
            return True
    with getattr(app, '_buffer_lock', None) or nullcontext():
        return bool(len(getattr(app, '_playback_buffer', ())))


@contextmanager
def _take(lock):
    if lock is None:
        yield
        return
    if not lock.acquire(blocking=False):
        raise RescanDeferred('Audio owner is busy')
    try:
        yield
    finally:
        lock.release()


@contextmanager
def rescan_transaction(app, sd):
    """Nonblocking admission, before ACE or any output is touched.

    The engine lock is held by refresh(). Other opens/closes use the common
    sounddevice base guard. Dispatch and earcon enqueue cannot race this check.
    """
    router = getattr(app, 'audio_coordinator', None)
    tts = getattr(app, 'tts_engine', None)
    local = getattr(tts, '_fallback_engine', None) or tts
    ace = getattr(app, '_ace_engine', None)
    with ExitStack() as held:
        held.enter_context(_take(getattr(router, '_dispatch_lock', None)))
        held.enter_context(_take(app._audio_refresh_lock))
        if output_busy(app):
            app._rescan_idle_since = None
            raise RescanDeferred('Speech or earcon is playing')
        idle_since = getattr(app, '_rescan_idle_since', None)
        if idle_since is None:
            app._rescan_idle_since = time.monotonic()
            raise RescanDeferred('Waiting for output tail')
        if time.monotonic() - idle_since < 0.25:
            raise RescanDeferred('Waiting for output tail')
        # An empty software buffer may still have a last block in hardware.
        if time.monotonic() < getattr(app, '_audio_output_idle_after', 0):
            raise RescanDeferred('Output tail is playing')
        held.enter_context(_take(getattr(tts, '_worker_lock', None)))
        guard = getattr(sd, '_samsara_stream_guard', None)
        if guard is None:
            raise RescanDeferred('Audio stream ownership is not registered')
        owned = (getattr(ace, 'rescan_stream', None), getattr(app, '_sound_stream', None),
                 getattr(local, '_tts_stream', None))
        held.enter_context(guard.reserve(owned))
        yield


def prepare_rescan(app, sd):
    """Called inside admitted refresh, with ACE closed; never cancel speech."""
    tts = getattr(app, 'tts_engine', None)
    local = getattr(tts, '_fallback_engine', None) or tts
    with rescan_transaction(app, sd):
        sound_ready = hasattr(app, '_sound_stream')
        if sound_ready:
            app.stop_sound_stream()
        try:
            if local is not None and hasattr(local, '_close_persistent_stream'):
                error = local._close_persistent_stream()
                if error is not None:
                    raise RescanDeferred('Speech stream did not close')
            # All non-owned streams were rejected before any owner was touched.
            with sd._samsara_stream_guard.reserve():
                sd._terminate()
                sd._initialize()
            app.available_mics = app.get_available_microphones()
            reconcile(app, sd)
            if app._capture_device is not None:
                app.capture_rate = int(sd.query_devices(app._capture_device, kind='input')['default_samplerate'])
                app._run_calibration_if_auto()
        finally:
            for restart in (getattr(app, '_start_sound_stream', None) if sound_ready else None,
                            getattr(tts, 'restart_stream', None)):
                if restart is not None:
                    try:
                        restart()
                    except Exception:
                        log.warning('[MIC] Output remains unavailable', exc_info=True)
    return dict(app.config, _capture_rate=app.capture_rate)


def refresh(app):
    """User Refresh or a queued capture-endpoint change authorizes one scan."""
    import sounddevice as sd
    ace = getattr(app, '_ace_engine', None)
    lost = getattr(ace, 'device_lost', getattr(app, '_no_microphone', False))
    if ace is not None and not ace.running and not lost:
        raise RescanDeferred('Capture is deliberately restarting')
    if getattr(app, 'recording', False) or (capture_busy(app) and not lost):
        raise RescanDeferred('Capture session is open')
    if ace is not None:
        ace.refresh(app.config, capture_rate=app.capture_rate,
                    guard=lambda: rescan_transaction(app, sd))
        set_state(app, ace.device_lost)
    else:
        with rescan_transaction(app, sd):
            app._prepare_audio_rescan()
            if getattr(app, '_capture_device', None) is None:
                set_state(app, True)
                return app.available_mics
            app._start_ace_engine()
            if getattr(app, '_ace_engine', None) is not None:
                if getattr(app, 'wake_word_active', False):
                    app._wake_consumer.start()
                if getattr(app, 'continuous_active', False):
                    app._continuous_consumer.start()
    return app.available_mics


def request_refresh(app):
    """UI callers always get their last list; deferred intent stays queued."""
    try:
        return refresh(app)
    except Exception:
        changes_for(app).request_input()
        return app.available_mics


def capture_endpoints():
    """Windows endpoint identities, not PortAudio's cached table; no stream IO."""
    from pycaw.pycaw import AudioUtilities
    devices = AudioUtilities.GetAllDevices(data_flow=1, device_state=1)
    return frozenset((d.id, d.FriendlyName) for d in devices)


def restart_outputs(app):
    """Preserve HEAD's default-output rule, without interrupting speech."""
    if getattr(app, 'output_device', None) is not None:
        return
    with _take(getattr(getattr(app, 'audio_coordinator', None), '_dispatch_lock', None)):
        with _take(app._audio_refresh_lock):
            if output_busy(app) or time.monotonic() < getattr(app, '_audio_output_idle_after', 0):
                raise RescanDeferred('Output is playing')
            app.stop_sound_stream()
            app._start_sound_stream()
            tts = getattr(app, 'tts_engine', None)
            if tts is not None:
                tts.restart_stream()


class DeviceChanges:
    def __init__(self, app, *, clock=time.monotonic, endpoints=capture_endpoints,
                 render_id=lambda: None):
        self.app, self.clock = app, clock
        self.endpoints, self.render_id = endpoints, render_id
        self._lock = threading.Lock()
        self._pending = None
        self._input_pending = self._output_pending = False
        self._next_probe = self._retry_at = 0.0
        self._waiting_since = None
        self.notifications = True
        self._capture = None
        self._render = None
        self._reported = set()

    def warn_once(self, message):
        if message not in self._reported:
            self._reported.add(message)
            log.warning(message)

    def prime(self):
        self._render = self.render_id()
        self._capture = self.endpoints()

    def notify(self, *_args):
        with self._lock:
            self._pending = self.clock()

    def request_input(self):
        self._input_pending = True
        if self._waiting_since is None:
            self._waiting_since = self.clock()

    def observe_output(self):
        render = self.render_id()
        old_render = self._render
        self._render = render
        if render != old_render and getattr(self.app, 'output_device', None) is None:
            self._output_pending = True

    def _observe(self):
        self.observe_output()
        current = self.endpoints()
        before, self._capture = self._capture, current
        if before is None:
            if current and self.app._no_microphone:
                self.request_input()  # Initial observation failed; a device is now visible.
            return
        if current == before:
            return
        ace = getattr(self.app, '_ace_engine', None)
        lost = getattr(ace, 'device_lost', self.app._no_microphone)
        name = getattr(ace, 'capture_name', None)
        # An unrelated endpoint must never cycle a healthy chosen input.
        was_present = any(n == name for _, n in before)
        still_present = any(n == name for _, n in current)
        removed = was_present and not still_present
        if lost or removed or getattr(self.app, '_using_default_mic', False):
            if removed and ace is not None:
                ace.mark_device_lost()
            self.request_input()

    def tick(self):
        if not hasattr(self.app, '_no_microphone'):
            return
        now = self.clock()
        set_state(self.app, self.app._no_microphone)
        with self._lock:
            pending = self._pending
            due = pending is not None and now - pending >= 0.75
        poll = not self.notifications and self.app._no_microphone and now >= self._next_probe
        if due or poll:
            try:
                self._observe()
            except Exception:
                self.warn_once('[MIC] Endpoint observation unavailable; will retry')
            else:
                with self._lock:
                    if self._pending == pending:
                        self._pending = None
            self._next_probe = now + 5.0
        if now < self._retry_at:
            return
        try:
            if self._input_pending:
                refresh(self.app)
                self._input_pending = False
                self._output_pending = False  # Rescan already restored outputs.
            elif self._output_pending:
                restart_outputs(self.app)
                self._output_pending = False
            self._waiting_since = None
        except RescanDeferred:
            if self._waiting_since is None:
                self._waiting_since = now
            # No blocking wait. After 30s, keep the request with a 5s backoff;
            # never force closure, cancel speech or abandon the user's request.
            self._retry_at = now + (5.0 if now - self._waiting_since >= 30 else 0.5)
        except Exception:
            self._retry_at = now + 5.0
            log.warning('[MIC] Device refresh deferred', exc_info=True)


def changes_for(app):
    existing = getattr(app, '_microphone_changes', None)
    if existing is None:
        app._microphone_changes = existing = DeviceChanges(app)
    return existing


def register_notifications(notify):
    """Register Windows input AND output endpoint notifications (existing pycaw)."""
    from pycaw.callbacks import MMNotificationClient
    from pycaw.pycaw import AudioUtilities
    class Notifications(MMNotificationClient):
        on_device_added = lambda self, *args: notify()
        on_device_removed = lambda self, *args: notify()
        on_device_state_changed = lambda self, *args: notify()
        on_default_device_changed = lambda self, *args: notify()
    receiver = Notifications()
    enumerator = AudioUtilities.GetDeviceEnumerator()
    enumerator.RegisterEndpointNotificationCallback(receiver)
    return lambda: enumerator.UnregisterEndpointNotificationCallback(receiver)


def watch(app, get_render_id):
    stop = getattr(app, '_output_watcher_stop', None)
    if stop is None:
        return
    changes = changes_for(app)
    changes.render_id = get_render_id
    close = None
    com_owned = False
    changes.notifications = False
    try:
        try:
            import comtypes
            # Import itself initializes the first importing thread. Match that
            # apartment; balance only this additional successful initialization.
            comtypes.CoInitializeEx()
            com_owned = True
            from comtypes.client import PumpEvents
            close = register_notifications(changes.notify)
            changes.notifications = True
        except Exception:
            changes.warn_once('[MIC] Endpoint notifications unavailable; observing Windows endpoints')
        try:
            changes.prime()
        except Exception:
            changes.warn_once('[MIC] Initial endpoint observation unavailable; will retry')
        while not stop.wait(0.5):
            if changes.notifications:
                try:
                    # STA notification callbacks need COM dispatch on this thread.
                    PumpEvents(0.001)
                except Exception:
                    changes.notifications = False
                    changes.warn_once('[MIC] Endpoint notifications unavailable; observing Windows endpoints')
            if not changes.notifications:
                try:
                    # Output routing must survive even a failed capture query.
                    changes.observe_output()
                except Exception:
                    changes.warn_once('[MIC] Default output observation unavailable; will retry')
            try:
                changes.tick()
            except Exception:
                changes.warn_once('[MIC] Device watcher pass failed; will retry')
    finally:
        try:
            if close is not None:
                close()
        except Exception:
            changes.warn_once('[MIC] Endpoint notification cleanup failed')
        finally:
            if com_owned:
                comtypes.CoUninitialize()


def _bounded_open_owned(app, token, audio_ducking, opener, timeout=0.2):
    """A slow ducking host must not hold the key listener hostage.

    Late completion only releases its own token; it cannot authorize pre-roll
    or take ownership from a newer hold. The worker performs no capture work.
    """
    from samsara.runtime import thread_registry
    done, abandoned = threading.Event(), threading.Event()
    result = []

    def work():
        try:
            if not abandoned.is_set():
                result.append(opener(app, token, audio_ducking, log))
        except Exception:
            log.warning('[DUCK] Optional capture duck failed', exc_info=True)
        finally:
            done.set()
            if abandoned.is_set():
                try:
                    app._close_hands_free_capture_duck(token)
                except Exception:
                    log.warning('[DUCK] Late duck cleanup failed', exc_info=True)

    thread_registry.spawn('hold-duck-open', work, daemon=True)
    if not done.wait(timeout):
        abandoned.set()
        # Cover the race in which work completed immediately before abandonment.
        if done.is_set():
            app._close_hands_free_capture_duck(token)
        raise TimeoutError('Optional ducking exceeded hold budget')
    return result[0] if result else (None, False)


def _open_hold_duck(app, audio_ducking, flight_recorder, clock):
    """Reuse capture ownership; wait for synchronous volume acknowledgements."""
    from samsara import hold_capture_onset
    app._hold_capture_duck_confirmed_at = None
    app._hold_capture_onset = hold_capture_onset.HoldOnset(None, None, None, False, flight_recorder.record)
    cfg = app.config.get('ducking', {}) or {}
    media_mode = cfg.get(
        'recording_mode',
        'duck' if (app.config.get('capture_duck_hold_enabled', True)
                   or cfg.get('enabled', False)) else 'off',
    )
    if media_mode != 'duck':
        audio_ducking.start_recording_media(media_mode, cfg.get('level', 0.2))
        return
    if (not app.config.get('capture_duck_hold_enabled', True)
            or not cfg.get('hands_free_enabled', True)
            or float(cfg.get('hands_free_level', 0.15)) >= 1.0):
        return
    app._hold_capture_duck_seq += 1
    token = -app._hold_capture_duck_seq
    app._hold_capture_duck_token = token
    app._hold_capture_onset.token = token
    started = clock.perf_counter()
    confirmed = False
    try:
        owner, fresh = _bounded_open_owned(app, token, audio_ducking, hold_capture_onset.open_owned_duck)
        if owner != token:
            raise RuntimeError('Hold capture duck could not engage')
        deadline = clock.perf_counter() + 2.0
        while True:
            with app._hands_free_duck_lock:
                ducker = app._hands_free_capture_ducker
                starting = app._hands_free_capture_duck_starting
                owned = token in app._hands_free_capture_duck_owners
            if not owned or not app._running:
                raise RuntimeError('Hold capture duck lost ownership')
            if ducker is not None and not starting:
                if ducker.sessions_failed or ducker.last_error:
                    raise RuntimeError('Hold capture duck volume update failed')
                break
            if not starting or clock.perf_counter() >= deadline:
                raise RuntimeError('Hold capture duck did not confirm')
            clock.sleep(0.005)
        app._hold_capture_duck_confirmed_at = clock.perf_counter()
        app._hold_capture_onset = hold_capture_onset.HoldOnset(
            token, started, app._hold_capture_duck_confirmed_at,
            fresh and getattr(ducker, 'sessions_ducked', None) == 0, flight_recorder.record)
        confirmed = True
    except Exception as exc:
        log.warning('[DUCK] Capture continues without ducking: %s', exc)
    finally:
        hold_capture_onset.finish_open(app, token, started, confirmed, clock, flight_recorder, log)
    if not confirmed:
        app._hold_capture_onset = hold_capture_onset.HoldOnset(
            None, started, clock.perf_counter(), False, flight_recorder.record)


def open_hold_duck(app, audio_ducking, flight_recorder, clock):
    """Ducking and its cleanup are optional; capture remains available."""
    try:
        _open_hold_duck(app, audio_ducking, flight_recorder, clock)
    except Exception:
        log.warning('[DUCK] Capture continues without ducking', exc_info=True)
        from samsara import hold_capture_onset
        app._hold_capture_duck_confirmed_at = None
        app._hold_capture_onset = hold_capture_onset.HoldOnset(
            None, None, clock.perf_counter(), False, flight_recorder.record)


def duck_audio(app, audio_ducking):
    try:
        cfg = app.config.get('ducking', {}) or {}
        mode = cfg.get('recording_mode', 'duck' if (
            app.config.get('capture_duck_hold_enabled', True) or cfg.get('enabled', False)) else 'off')
        audio_ducking.start_recording_media(mode, cfg.get('level', 0.2))
    except Exception:
        log.warning('[DUCK] Capture continues without media ducking', exc_info=True)
