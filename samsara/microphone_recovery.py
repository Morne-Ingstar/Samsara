"""Device recovery orchestration, separate from the desktop bootstrap.

Endpoint callbacks only schedule work. The existing output watcher thread does
the debounced work; missing and fallback inputs are compared when needed. A saved
name remains the preference; its numeric index is corrected only on a name match.
"""
from contextlib import nullcontext, ExitStack, contextmanager
import logging
import json
import sys
import threading
import time

from samsara.audio_engine import device_resolver
from samsara.recovery_notifications import notice_for

log = logging.getLogger('Samsara')
# Stand-in wording (550), reviewed in ASK.json.
MISSING_MESSAGE = 'No microphone. Connect a microphone to dictate.'
FALLBACK_MESSAGE = 'Your chosen microphone is not connected. Using the system default microphone.'
BOOT_MIC_GRACE_SECONDS = 5.0
INPUT_REFRESH_TIMEOUT = 120.0


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
    lock = getattr(app, '_config_lock', None)
    persist = getattr(app, 'persist_config', None)
    corrected = False
    with lock or nullcontext():
        choice = dict(app.config)
    try:
        device, fallback = device_resolver.select_input(sd, choice)
    except device_resolver.DeviceNotFound:
        device, fallback = None, False
    with lock or nullcontext():
        if any(app.config.get(key) != choice.get(key) for key in
               ('microphone', 'microphone_name', 'microphone_hostapi')):
            return  # A deliberate selection made during enumeration wins.
        app._capture_device, app._using_default_mic = device, fallback
        name = choice.get('microphone_name')
        if (callable(persist) and device is not None and not fallback and name
                and name not in ('System default', 'Default')
                and device != choice.get('microphone')):
            app.config['microphone'] = device
            corrected = True
    if corrected:
        persist()  # Recovery-only owners without a config writer retain their saved choice.


def selection_name(app):
    """Label the active selection while retaining the saved reconnect preference."""
    from samsara.audio_devices import microphone_choice

    name = app.config.get('microphone_name')
    if (getattr(app, '_using_default_mic', False)
            or name in ('System default', 'Default')
            or (not name and app.config.get('microphone') is None)):
        return 'System default'
    devices = getattr(app, 'available_mics', None)
    if devices is not None:
        choice = microphone_choice(devices, app.config)
        return choice['name'] if choice else 'System default'
    return name or app.get_current_microphone_name() or 'System default'


def select_microphone(app, mic_id, switch=None):
    """A named tray choice wins even when its ID used to belong to another mic."""
    entry = next((mic for mic in app.available_mics if mic['id'] == mic_id), None)
    if entry is None:
        return
    if (app.config.get('microphone') == mic_id
            and app.config.get('microphone_name') != entry['name']):
        with getattr(app, '_config_lock', None) or nullcontext():
            app.config['microphone_name'] = entry['name']
        app.persist_config()
        app.refresh_audio_devices()  # Existing idle admission queues a busy capture.
    else:
        (switch or app.switch_microphone)(mic_id)


def select_system_default(app, switch=None):
    """Use the existing selection service, including its same-null-ID recovery."""
    old_id = app.config.get('microphone')
    old_name = app.config.get('microphone_name')
    if old_id is None and old_name == 'System default':
        return
    lock = vars(app).get('_config_lock')
    with lock or nullcontext():
        app.config['microphone_name'] = 'System default'
    try:
        if old_id is not None:
            (switch or app.switch_microphone)(None)
        else:
            app.refresh_audio_devices()
    except Exception:
        with lock or nullcontext():
            if old_name is None:
                app.config.pop('microphone_name', None)
            else:
                app.config['microphone_name'] = old_name
        raise
    if old_id is None and old_name != 'System default':
        app.persist_config()  # switch_microphone's same-ID guard skips saving.
    ace = vars(app).get('_ace_engine')
    if ace is None:
        if old_id is not None:
            app.refresh_audio_devices()  # The old switch only restarts an existing engine.
    else:
        import sounddevice as sd
        reconcile(app, sd)
        set_state(app, ace.device_lost)


def _boot_grace(app, lost, notify=False):
    if getattr(app, '_mic_boot_ready', False):
        return False
    deadline = getattr(app, '_mic_boot_deadline', None)
    if deadline is None and hasattr(app, '_no_microphone'):
        # A watcher observing an established outage/fallback is not a boot.
        app._mic_boot_ready = True
        return False
    if notify or (not lost and not getattr(app, '_using_default_mic', False)):
        app._mic_boot_ready = True
        app._mic_boot_scan_complete = True
        return False
    if deadline is None:
        deadline = app._mic_boot_deadline = time.monotonic() + BOOT_MIC_GRACE_SECONDS
    if time.monotonic() >= deadline:
        app._mic_boot_ready = True
        return False
    return True


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
    deferred = _boot_grace(app, lost, notify)
    app._no_microphone = bool(lost)
    if changed and not lost:
        changes_for(app).request_wake()
    # Start alongside boot grace, not after it; runtime endpoint flaps get the
    # same policy. Only tray balloons wait; availability/chips stay immediate.
    condition = ('missing' if lost else
                 'fallback' if getattr(app, '_using_default_mic', False) else None)
    for other in ('missing', 'fallback'):
        if other != condition:
            previous = getattr(app, f'_mic_{other}_notice', None)
            if previous is not None:
                previous.recovered()
    if condition is not None:
        notice = notice_for(app, f'_mic_{condition}_notice')
        def show_warning():
            tray = getattr(app, 'tray_icon', None)
            if tray is None:
                return False
            if condition == 'missing':
                tray.notify_warning('No microphone', MISSING_MESSAGE)
                app._mic_warning_shown = True
            else:
                tray.notify_warning('Microphone', FALLBACK_MESSAGE)
                app._mic_fallback_shown = True
            return True

        notice.interrupted(show_warning, lambda: (
            app._no_microphone if condition == 'missing' else
            not app._no_microphone and getattr(app, '_using_default_mic', False)))
    if condition is None:
        # 1141: the mic problem ended, so its Windows-warning message goes too.
        try:
            from samsara.live_surface.feedback import WINDOWS_WARNING_OWNER, schedule_owner_clear
            schedule_owner_clear(app, WINDOWS_WARNING_OWNER)
        except Exception:
            pass
    if deferred:
        return  # Keep actual availability; defer only startup warning surfaces.
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
    """Protect speech and drafts; an idle always-on listener is pausable."""
    if any(bool(getattr(app, name, False)) for name in
           ('recording', '_hotkey_recording', '_stop_in_flight', '_streaming_session',
            'is_speaking', 'command_mode_active', 'ava_mode_active',
            'ava_command_session_active')):
        return True
    wake = getattr(app, '_wake_consumer', None)
    continuous = getattr(app, '_continuous_consumer', None)
    manager = getattr(app, '_session_mode_manager', None)
    return bool(getattr(app, '_ava_question', None)
                or getattr(wake, '_utterance_frames', ())
                or getattr(wake, '_toggle_queue', ())
                or getattr(wake, '_toggle_worker_active', False)
                or getattr(continuous, '_is_speaking', False)
                or getattr(continuous, '_speech_frames', ())
                or getattr(manager, '_dictate_pending_audio', ()))


@contextmanager
def _refresh_guard(app, sd, lost):
    with rescan_transaction(app, sd), ExitStack() as paused:
        if not lost:
            checks = []
            for active, name in (('wake_word_active', '_wake_consumer'),
                                 ('continuous_active', '_continuous_consumer')):
                consumer = getattr(app, name, None)
                running = getattr(consumer, 'running', getattr(consumer, '_running', False))
                if running or getattr(app, active, False):
                    pause = getattr(consumer, 'pause_if_idle', None)
                    if pause is None:
                        raise RescanDeferred('Listener cannot pause safely')
                    checks.append(paused.enter_context(pause()))
            # A frame arriving while the other listener was being paused wins.
            for check in checks:
                check()
            # Speech that won admission before the reservations stays intact.
            if capture_busy(app):
                raise RescanDeferred('Capture session is open')
        yield


# Callback progress, rather than queued data, proves a cue is playing. Allow
# one second for the first/next callback; a progressing cue never times out.
CUE_PROGRESS_TIMEOUT = 1.0


def _discard_cue_locked(app):
    app._playback_buffer = getattr(app, '_playback_buffer', ())[:0]
    timing = getattr(app, '_stage_timing_playback', None)
    app._stage_timing_playback = None
    app._cue_wait_since = None
    app._audio_output_idle_after = 0
    return timing


def discard_cue(app):
    with getattr(app, '_buffer_lock', None) or nullcontext():
        timing = _discard_cue_locked(app)
    if timing is not None:
        timing.feedback_finished()


def cue_output_ready(app):
    stream = getattr(app, '_sound_stream', None)
    try:
        return stream is not None and bool(getattr(stream, 'active', True))
    except Exception:
        return False  # A disconnected/closed stream cannot drain the cue.


def cue_busy(app):
    timing = None
    with getattr(app, '_buffer_lock', None) or nullcontext():
        queued = bool(len(getattr(app, '_playback_buffer', ())))
        if not queued:
            app._cue_wait_since = None
            if not cue_output_ready(app):
                app._audio_output_idle_after = 0
            return False
        now = time.monotonic()
        since = getattr(app, '_cue_wait_since', None)
        if since is None:
            app._cue_wait_since = since = now
        progress = getattr(app, '_sound_callback_at', since)
        playing = cue_output_ready(app) and now - max(since, progress) < CUE_PROGRESS_TIMEOUT
        if not playing:
            timing = _discard_cue_locked(app)
    if timing is not None:
        timing.feedback_finished()
    return playing


def output_busy(app):
    tts = getattr(app, 'tts_engine', None)
    local = getattr(tts, '_fallback_engine', None)
    for owner in (tts, local):
        if owner is not None and owner.is_speaking():
            return True
    return cue_busy(app)


@contextmanager
def _take(lock, owner):
    if lock is None:
        yield
        return
    if not lock.acquire(blocking=False):
        raise RescanDeferred(f'Audio owner is busy: {owner}')
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
    # 1217: prepare_rescan runs synchronously inside ACE.refresh. The outer
    # transaction already holds Edge's NON-reentrant worker lock. Reacquiring
    # it after closing capture used to refuse our own admitted transaction.
    owner = threading.get_ident()
    if getattr(app, '_rescan_owner', None) == owner:
        yield
        return
    with ExitStack() as held:
        held.enter_context(_take(getattr(router, '_dispatch_lock', None), 'TTS dispatch'))
        held.enter_context(_take(app._audio_refresh_lock, 'sound output lifecycle'))
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
        held.enter_context(_take(getattr(tts, '_worker_lock', None), 'TTS worker'))
        guard = getattr(sd, '_samsara_stream_guard', None)
        if guard is None:
            raise RescanDeferred('Audio stream ownership is not registered')
        owned = (getattr(ace, 'rescan_stream', None), getattr(app, '_sound_stream', None),
                 getattr(local, '_tts_stream', None))
        try:
            held.enter_context(guard.reserve(owned))
        except RescanDeferred as exc:
            streams = tuple(getattr(guard, 'streams', {}).values())
            names = [type(s).__name__ for s in streams
                     if all(s is not allowed for allowed in owned)]
            raise RescanDeferred(f'{exc}; busy_owner={",".join(names) or str(exc)}') from exc
        app._rescan_owner = owner
        try:
            yield
        finally:
            app._rescan_owner = None


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
            # Proof of BOTH a successful backend reset and a fresh enumeration.
            app._input_rescan_serial = getattr(app, '_input_rescan_serial', 0) + 1
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
    serial = getattr(app, '_input_rescan_serial', 0)
    ace = getattr(app, '_ace_engine', None)
    lost = getattr(ace, 'device_lost', getattr(app, '_no_microphone', False))
    if ace is not None and not ace.running and not lost:
        raise RescanDeferred('Capture is deliberately restarting')
    if getattr(app, 'recording', False) or (capture_busy(app) and not lost):
        raise RescanDeferred('Capture session is open')
    if ace is not None:
        ace.refresh(app.config, capture_rate=app.capture_rate,
                    guard=lambda: _refresh_guard(app, sd, lost))
        set_state(app, ace.device_lost)
    else:
        with _refresh_guard(app, sd, lost):
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
    if not getattr(app, '_no_microphone', False):
        changes_for(app).request_wake()
    if getattr(app, '_input_rescan_serial', 0) == serial:
        raise RuntimeError('Input refresh did not reinitialise PortAudio and reread devices')
    return app.available_mics


def request_refresh(app):
    """UI callers always get their last list; deferred intent stays queued."""
    changes = changes_for(app)
    changes.request_input()
    changes._refresh_input(changes.clock())
    return app.available_mics


def capture_endpoints():
    """Windows endpoint identities, not PortAudio's cached table; no stream IO."""
    from pycaw.pycaw import AudioUtilities
    devices = AudioUtilities.GetAllDevices(data_flow=1, device_state=1)
    return frozenset((d.id, d.FriendlyName) for d in devices)


def default_capture_id():
    """Read Windows' live default, independent of PortAudio's cached table."""
    from pycaw.pycaw import AudioUtilities
    try:
        return AudioUtilities.GetMicrophone().GetId()
    except OSError:
        return None  # No default capture endpoint while every input is absent.


def follows_default(app):
    cfg = getattr(app, 'config', {})
    return (getattr(app, '_using_default_mic', False)
            or cfg.get('microphone_name') in ('System default', 'Default')
            or (not cfg.get('microphone_name') and cfg.get('microphone') is None))


def restart_outputs(app):
    """Preserve HEAD's default-output rule, without interrupting speech."""
    if getattr(app, 'output_device', None) is not None:
        return
    with _take(getattr(getattr(app, 'audio_coordinator', None), '_dispatch_lock', None), 'TTS dispatch'):
        with _take(app._audio_refresh_lock, 'sound output lifecycle'):
            if output_busy(app) or time.monotonic() < getattr(app, '_audio_output_idle_after', 0):
                raise RescanDeferred('Output is playing')
            app.stop_sound_stream()
            app._start_sound_stream()
            tts = getattr(app, 'tts_engine', None)
            if tts is not None:
                tts.restart_stream()


def matching_capture_endpoints(endpoints, name):
    # WinMM/MME exposes only the first 31 characters of a FriendlyName.
    # Include exact matches in the same ambiguity check as truncated matches.
    if not name:
        return ()
    return tuple(identity for identity, friendly in endpoints
                 if friendly == name or (len(name) == 31 and friendly[:31] == name))


class DeviceChanges:
    def __init__(self, app, *, clock=time.monotonic, endpoints=capture_endpoints,
                 render_id=lambda: None, input_id=lambda: None):
        self.app, self.clock = app, clock
        self.endpoints, self.render_id = endpoints, render_id
        self.input_id = input_id
        self._input_default = None
        self._input_attempts = 0
        self._wake_pending = False
        self._wake_attempts = 0
        self._wake_retry_at = 0.0
        self._lock = threading.Lock()
        self._input_refresh_lock = threading.Lock()
        self._pending = None
        self._input_pending = self._output_pending = False
        self._next_probe = self._retry_at = 0.0
        self._waiting_since = None
        self._input_deferred = False
        self._input_busy_owner = 'none'
        self._input_before = []
        self._input_selection = None
        self._input_stale = False
        self.notifications = True
        self._capture = None
        self._render = None
        self._name_ambiguity_reported = False
        self._reported = set()

    def warn_once(self, message):
        if message not in self._reported:
            self._reported.add(message)
            log.warning(message)

    def prime(self):
        self._render = self.render_id()
        self._capture = self.endpoints()
        self._input_default = self.input_id()

    def notify(self, *_args):
        with self._lock:
            self._pending = self.clock()

    def request_input(self):
        self._input_stale = True
        if not self._input_pending:
            self._input_attempts = 0
            self._input_deferred = False
            self._input_busy_owner = 'none'
            # 1217: startup recovery must not depend on audit data being ready.
            self._input_before = [mic['name'] for mic in getattr(self.app, 'available_mics', ())]
            self._input_selection = getattr(self.app, 'config', {}).get('microphone_name', 'System default')
            self._waiting_since = self.clock()
            self._retry_at = 0.0
        self._input_pending = True
        if self._waiting_since is None:
            self._waiting_since = self.clock()

    def request_wake(self):
        if not self._wake_pending:
            self._wake_attempts = 0
            self._wake_retry_at = 0.0
        self._wake_pending = True

    def _finish_input(self, now, *, rescanned, reason):
        after = [mic['name'] for mic in getattr(self.app, 'available_mics', ())]
        selected = getattr(self.app, 'config', {}).get('microphone_name', 'System default')
        log.info('[MIC] Input refresh: rescanned=%s devices_before=%d devices_after=%d '
                 'added=%s removed=%s selected=%s selection_changed=%s waited_ms=%d busy_owner=%s',
                 rescanned, len(self._input_before), len(after),
                 sorted(set(after) - set(self._input_before)),
                 sorted(set(self._input_before) - set(after)), selected,
                 selected != self._input_selection,
                 round((self.clock() - self._waiting_since) * 1000), self._input_busy_owner)
        log.info('[MIC] Input refresh %s', reason)
        self._input_pending = self._input_deferred = False
        self._input_stale = not rescanned
        self._waiting_since = None

    def _refresh_input(self, now):
        # UI Refresh and the watcher may arrive together. Only one owns the
        # request's scan, deadline, completion log and state cleanup.
        if not self._input_refresh_lock.acquire(blocking=False):
            return
        try:
            if self._input_pending:
                self._attempt_input(now)
        finally:
            self._input_refresh_lock.release()

    def _attempt_input(self, now):
        if now < self._retry_at:
            return
        if now - self._waiting_since >= INPUT_REFRESH_TIMEOUT:
            self._finish_input(now, rescanned=False, reason='given up after 2 minutes')
            return
        try:
            refresh(self.app)
        except RescanDeferred as exc:
            self._input_busy_owner = str(exc).rsplit('busy_owner=', 1)[-1].removeprefix('Audio owner is busy: ')
            if not self._input_deferred:
                log.info('[MIC] Input refresh deferred: %s', exc)
                self._input_deferred = True
            self._retry_at = now + (5.0 if now - self._waiting_since >= 30 else 0.5)
            # A busy capture cannot starve default-output routing.
            if self._output_pending:
                try:
                    restart_outputs(self.app)
                except RescanDeferred:
                    pass
                else:
                    self._output_pending = False
        except Exception:
            self._input_attempts += 1
            log.warning('[MIC] Device refresh attempt %d/3 failed',
                        self._input_attempts, exc_info=True)
            self._retry_at = now + 5.0
            if self._input_attempts >= 3:
                self._finish_input(now, rescanned=False,
                                   reason='given up after 3 failed attempts')
        else:
            self._finish_input(now, rescanned=True, reason='completed')
            self._output_pending = False  # Rescan already restored outputs.
            self._restore_wake(now)

    def _restore_wake(self, now):
        app = self.app
        if not self._wake_pending or now < self._wake_retry_at:
            return
        if (not getattr(app, 'config', {}).get('wake_word_enabled', False)
                or getattr(app, 'snoozed', False)):
            self._wake_pending = False
            return
        if getattr(app, '_no_microphone', False):
            return  # Arrival/recovery will retry; never start on a missing mic.
        if (getattr(app, 'loading_model', False)
                or getattr(app, '_wake_models_state', None) == 'loading'
                or getattr(app, '_wake_start_pending', False)):
            return  # Loading/activation must not spend the device restart budget.
        if (hasattr(app, '_wake_consumer_reasons')
                and not app._wake_consumer_reasons
                and not getattr(app, 'wake_word_active', False)
                and getattr(app, '_hands_free_fault', None) is None
                and not getattr(app, '_wake_start_pending', False)):
            self._wake_pending = False  # Preserve a deliberate runtime switch-off.
            return
        consumer = getattr(app, '_wake_consumer', None)
        if (getattr(consumer, 'running', False)
                and getattr(app, 'wake_word_active', False)
                and getattr(app, '_hands_free_fault', None) is None):
            self._wake_pending = False  # The existing epoch path already resumed.
            return
        from samsara.audio_engine.wake_consumer import (
            FATAL_PERMANENT, FATAL_RETRY_DELAYS_S, fault_from)
        fault = getattr(app, '_hands_free_fault', None)
        if getattr(fault, 'classification', None) == FATAL_PERMANENT:
            self._wake_pending = False
            return  # A device event cannot repair a programming error.
        self._wake_attempts += 1
        try:
            restart = getattr(app, 'restart_hands_free', None)
            if callable(restart):
                restart(source='device-change')
        except Exception:
            log.warning('[WAKE] Device restart failed', exc_info=True)
        consumer = getattr(app, '_wake_consumer', None)
        running = (bool(getattr(consumer, 'running', False))
                   and bool(getattr(app, 'wake_word_active', False)))
        cap = len(FATAL_RETRY_DELAYS_S)
        log.info('[WAKE] Device restart attempt %d/%d: running=%d',
                 self._wake_attempts, cap, running)
        self._wake_pending = not running and self._wake_attempts < cap
        if self._wake_pending:
            self._wake_retry_at = now + FATAL_RETRY_DELAYS_S[self._wake_attempts - 1]
        elif not running:
            # PLACEHOLDER COPY (1127): Home supplies the working restart action.
            app._hands_free_fault = fault_from(
                OSError('Microphone changed'), at=time.time(),
                attempts=self._wake_attempts, retrying=False)
            publish = getattr(app, '_publish_hands_free_fault', None)
            if callable(publish):
                publish()

    def observe_output(self):
        render = self.render_id()
        old_render = self._render
        self._render = render
        if render != old_render and getattr(self.app, 'output_device', None) is None:
            self._output_pending = True

    def _observe(self):
        self.observe_output()
        default = self.input_id()
        old_default, self._input_default = self._input_default, default
        if default != old_default and follows_default(self.app):
            self.request_input()
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
        # 1206: input additions must refresh the list even with a healthy preference.
        previous = matching_capture_endpoints(before, name)
        present = matching_capture_endpoints(current, name)
        if len(previous) > 1 or len(present) > 1:
            if not self._name_ambiguity_reported:
                log.warning('[MIC] Capture endpoint name is ambiguous; leaving audio unchanged')
                self._name_ambiguity_reported = True
            removed = False
        else:
            removed = bool(previous) and previous != present
        configured = getattr(self.app, 'config', {}).get('microphone_name')
        configured_before = matching_capture_endpoints(before, configured)
        configured_present = matching_capture_endpoints(current, configured)
        # A return needs one identifiable endpoint after absence; a change
        # among ambiguous matches must not cycle a healthy chosen input.
        returned = not configured_before and len(configured_present) == 1
        # Ambiguous aliases returning are not new inputs (1177); unrelated
        # additions still refresh the list without changing the preference.
        added = [endpoint for endpoint in current - before if endpoint[0] not in present]
        if lost or removed or follows_default(self.app) or returned or added:
            if removed and ace is not None:
                ace.mark_device_lost()
            self.request_input()

    def tick(self):
        if not hasattr(self.app, '_no_microphone'):
            return
        now = self.clock()
        set_state(self.app, self.app._no_microphone)
        deadline = getattr(self.app, '_mic_boot_deadline', None)
        if deadline is not None and not getattr(self.app, '_mic_boot_scan_complete', False):
            # Re-enumerate cached PortAudio endpoints through the existing idle
            # transaction even if Windows has not delivered a notification yet.
            self.request_input()
            if time.monotonic() >= deadline:
                self.app._mic_boot_scan_complete = True
        with self._lock:
            pending = self._pending
            due = pending is not None and now - pending >= 0.75
        # 1217: notifications can be lost even after successful registration.
        poll = now >= self._next_probe
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
        self._restore_wake(now)
        try:
            if self._input_pending:
                self._refresh_input(now)
            elif self._output_pending:
                restart_outputs(self.app)
                self._output_pending = False
            if not self._input_pending:
                self._waiting_since = None
        except RescanDeferred as exc:
            self._retry_at = now + 0.5
        except Exception:
            self._retry_at = now + 5.0
            log.warning('[MIC] Output refresh deferred', exc_info=True)


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
    changes.input_id = default_capture_id
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


def _bounded_open_owned(app, token, audio_ducking, opener, timeout=0.2, *, deadline=None, clock=time):
    """A slow ducking host must not hold the key listener hostage.

    Late completion only releases its own token; it cannot authorize pre-roll
    or take ownership from a newer hold. The worker performs no capture work.
    """
    from samsara.runtime import thread_registry
    if deadline is None:
        deadline = clock.perf_counter() + timeout
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
    if not done.wait(max(0.0, deadline - clock.perf_counter())):
        abandoned.set()
        # Cover the race in which work completed immediately before abandonment.
        if done.is_set():
            app._close_hands_free_capture_duck(token)
        raise TimeoutError('Optional ducking exceeded hold budget')
    return result[0] if result else (None, False)


def _open_hold_duck(app, audio_ducking, flight_recorder, clock):
    """Share one request deadline across duck setup and reused confirmation."""
    started = clock.perf_counter()
    deadline = started + 0.2
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
    confirmed = False
    try:
        owner, fresh = _bounded_open_owned(
            app, token, audio_ducking, hold_capture_onset.open_owned_duck,
            deadline=deadline, clock=clock)
        if owner != token:
            raise RuntimeError('Hold capture duck could not engage')
        while True:
            with app._hands_free_duck_lock:
                ducker = app._hands_free_capture_ducker
                starting = app._hands_free_capture_duck_starting
                owned = token in app._hands_free_capture_duck_owners
            if not owned or not app._running:
                raise RuntimeError('Hold capture duck lost ownership')
            remaining = deadline - clock.perf_counter()
            if remaining <= 0:
                raise TimeoutError('Optional ducking exceeded hold budget')
            if ducker is not None and not starting:
                if ducker.sessions_failed or ducker.last_error:
                    raise RuntimeError('Hold capture duck volume update failed')
                break
            if not starting:
                raise RuntimeError('Hold capture duck did not confirm')
            clock.sleep(min(0.005, remaining))
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
