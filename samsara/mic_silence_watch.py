"""Observe capture liveness and the level of completed hold attempts.

An open stream can deliver quiet PCM or digital zeros indefinitely. Keep the
low-level test for an explicit hold, where the user has requested input.
"""
import logging
import math
import threading
import time

from samsara.audio_engine.frame import FRAME_MS
from samsara.wake_noise_notice import WakeNoiseNotice


DEAD_RMS = .00015
STALL_SECONDS = 10.
HOLD_SECONDS = .1
TITLE = 'No sound from your microphone'  # PLACEHOLDER COPY (1252)
HELP = ('Check the mic cable, the selected input on your audio interface, '
        'the mute switch and the Windows input level. '
        'You can also choose another microphone in Settings > Audio.')  # PLACEHOLDER COPY (1252)
RECOVERED = 'Microphone sound is back.'  # PLACEHOLDER COPY (1252)
_create_lock = threading.RLock()
log = logging.getLogger('Samsara')


def capture_ducked(app, *, hold):
    owners = getattr(app, '_hands_free_capture_duck_owners', ())
    # A negative token is this hold's media-output duck. It does not mute the
    # microphone; captured evidence separately excludes its setup/restore work.
    ducked = any(token >= 0 for token in owners) if hold else bool(
        owners or getattr(app, '_hands_free_capture_ducker', None) is not None)
    return ducked or getattr(app, '_hands_free_capture_duck_starting', False)


def excluded(app, *, hold=False):
    """Observe existing playback/duck state, including the TTS echo tail."""
    coordinator = getattr(app, 'audio_coordinator', None)
    return bool(
        getattr(coordinator, 'is_speaking', False)
        or time.monotonic() - getattr(app, '_tts_last_speaking', float('-inf')) < .3
        or capture_ducked(app, hold=hold)
        or time.monotonic() < getattr(app, '_audio_output_idle_after', 0.)
        or getattr(app, '_playback_buffer', ()) is not None
        and len(getattr(app, '_playback_buffer', ())) > 0)


def clean_hold_seconds(app, evidence, frame_count):
    frame_s = FRAME_MS / 1000.
    frame_ends = getattr(evidence, 'frame_ends', None)
    if frame_ends is None:
        # Older observers without capture timestamps still need the live guards.
        if excluded(app, hold=True):
            return 0.
        frame_ends = (evidence.stopped - remaining * frame_s
                      for remaining in range(frame_count - 1, -1, -1))
        intervals = evidence.intervals
        cutoff = evidence.stopped
    else:
        if len(frame_ends) != frame_count or capture_ducked(app, hold=True):
            return 0.
        intervals = list(evidence.warning_intervals)
        coordinator = getattr(app, 'audio_coordinator', None)
        speech_windows = getattr(coordinator, 'speech_windows', None)
        if callable(speech_windows):
            intervals.extend((w.start_ns / 1e9, w.end_ns / 1e9) for w in speech_windows())
        cutoff = evidence.capture_cutoff if evidence.capture_cutoff is not None else math.inf
    clean = sum(end <= cutoff and not any(start < stop and start < end and end - frame_s < stop
                                         for start, stop in intervals)
                for end in frame_ends)
    return clean * frame_s


def device_name(app):
    from samsara.mic_score import microphone_name
    return microphone_name(app) or 'System default microphone'  # PLACEHOLDER COPY (1252)


class SilenceWatch:
    def __init__(self, app, deliver=None):
        self.app = app
        self.deliver = deliver
        self.lock = threading.RLock()
        self.active = False
        self.cause = self.warning_device = self.warning_at = None
        self.notice_version = None
        self.engine = self.reader = None
        self.stream = self.stream_seen_at = self.last_frame_at = self.frame_epoch = None
        self.was_excluded = False
        self.excluded_until = float('-inf')
        self.wake_noise_notice = WakeNoiseNotice(app)

    def transition(self, cause, source, device, maximum, seconds):
        active = cause is not None
        if active == self.active and (not active or device == self.warning_device):
            if active and cause != 'hold':
                self.cause = cause  # A transport fault can follow a quiet hold in one outage.
            return
        self.active = active
        self.cause = cause
        self.warning_device = device if active else None
        self.warning_at = time.perf_counter() if active else None
        self.notice_version = object()
        name = device_name(self.app)
        log.info('[MIC_SILENT] state=%s source=%s max_rms=%.7f floor=%.7f seconds=%.2f',
                 'silent' if active else 'recovered', source, maximum, DEAD_RMS, seconds)
        # Capture's observer queues UI work only; failures never escape it.
        try:
            if self.deliver is None:
                present(self.app, active, name, announce_recovery=maximum > DEAD_RMS)
            else:
                self.deliver(active, name)
        except Exception:
            log.debug('[MIC_SILENT] notice delivery unavailable')

    def hold(self, levels, evidence):
        if len(levels) == 0 or not all(math.isfinite(x) and x >= 0 for x in levels):
            return
        maximum = max(levels)
        with self.lock:
            engine = getattr(self.app, '_ace_engine', None)
            device = (engine, getattr(engine, 'capture_name', None))
            if maximum > DEAD_RMS:
                if self.cause == 'hold':
                    self.transition(None, 'hold', device, maximum, len(levels) * .1)
                return
            clean = clean_hold_seconds(self.app, evidence, len(levels))
            if clean >= HOLD_SECONDS:
                self.transition('hold', 'hold', device, maximum, clean)

    def detach(self):
        engine, reader = self.engine, self.reader
        self.engine = self.reader = None
        self.stream = self.stream_seen_at = self.last_frame_at = self.frame_epoch = None
        self.wake_noise_notice.begin_poll(0., active=False)
        if engine is not None and reader is not None:
            engine.unregister_consumer(reader)

    def poll(self):
        with self.lock:
            self._poll()

    def _poll(self):
        import numpy as np
        from samsara.audio_engine.frame import FRAME_MS
        from samsara.audio_engine.ring import EMPTY
        engine = getattr(self.app, '_ace_engine', None)
        if engine is None or not getattr(engine, '_running', False):
            self.detach()
            return
        if engine is not self.engine:
            self.detach()
            self.reader = engine.register_consumer('mic-silence-watch-1252')
            self.engine = engine
        blocked = excluded(self.app)
        now = time.perf_counter()
        # Also exclude a batch straddling the end of a suppression window.
        skip = blocked or self.was_excluded
        self.was_excluded = blocked
        if skip:
            self.excluded_until = now
        stream = getattr(engine, 'rescan_stream', None)
        if stream is not self.stream:
            self.stream = stream
            self.stream_seen_at = now if stream is not None else None
            self.last_frame_at = None
        device = (engine, getattr(engine, 'capture_name', None))
        holding = bool(getattr(self.app, 'recording', False)
                       or getattr(self.app, '_hotkey_recording', False))
        listening = bool(getattr(self.app, 'continuous_active', False)
                         or getattr(self.app, 'wake_word_active', False))
        check_noise = self.wake_noise_notice.begin_poll(
            now, active=getattr(self.app, 'wake_word_active', False))
        before = self.reader.overrun_count
        resumed = False
        maximum = 0.
        caught_up = False
        # Bounded work, even if a synthetic writer produces frames without pause.
        for _ in range(1000 // FRAME_MS * 2):
            frame = self.reader.read_next()
            if frame is EMPTY:
                caught_up = True
                break
            discontinuity = self.reader.overrun_count != before
            before = self.reader.overrun_count
            fresh = 0. <= now - frame.t_capture <= 1.
            after_warning = fresh and self.warning_at is not None and frame.t_capture > self.warning_at
            if fresh:
                changed_epoch = self.frame_epoch is not None and frame.device_epoch != self.frame_epoch
                self.frame_epoch = frame.device_epoch
                self.last_frame_at = frame.t_capture
                resumed |= after_warning and (
                    self.cause != 'hold' or changed_epoch or device != self.warning_device)
            ineligible = (not fresh or skip or discontinuity or holding
                          or frame.t_capture <= self.excluded_until)
            if not check_noise and (ineligible or not after_warning):
                continue
            pcm = frame.pcm.astype(np.float64) / 32767.
            rms = float(np.sqrt(np.mean(pcm * pcm)))
            if after_warning and not ineligible:
                maximum = max(maximum, rms)
            if check_noise:
                self.wake_noise_notice.observe(
                    rms, frame.t_capture, frame.seq, (*device, frame.device_epoch), skip=ineligible)
        # Recheck after reading. The active engine may have changed during a rescan.
        if (getattr(self.app, '_ace_engine', None) is not engine or not engine._running
                or getattr(engine, 'capture_name', None) != device[1]
                or getattr(engine, 'rescan_stream', None) is not stream):
            return
        changes = getattr(self.app, '_microphone_changes', None)
        removed = getattr(changes, 'missing_capture', None) == device
        heartbeat = self.last_frame_at if self.last_frame_at is not None else self.stream_seen_at
        if removed or getattr(engine, 'device_lost', False):
            self.transition('removed', 'endpoint', device, 0., 0.)
        elif (listening or holding) and caught_up and heartbeat is not None and now - heartbeat >= STALL_SECONDS:
            self.transition('stalled', 'callbacks', device, 0., now - heartbeat)
        elif resumed or maximum > DEAD_RMS:
            self.transition(None, 'callbacks', device, maximum, 0.)


def watch_for(app):
    with _create_lock:
        watch = vars(app).get('_mic_silence_watch_1252')
        if watch is None:
            watch = app._mic_silence_watch_1252 = SilenceWatch(app)
        return watch


def observe_hold(consumer, levels, evidence):
    app = getattr(consumer, '_app', None)
    if (app is not None and not getattr(consumer, 'capture_loss', False)
            and not getattr(consumer, 'dropped_frames', 0)):
        watch_for(app).hold(levels, evidence)


def poll(app):
    watch_for(app).poll()


def close(app):
    watch = vars(app).get('_mic_silence_watch_1252')
    if watch is not None:
        with watch.lock:
            watch.detach()


def present(app, active, name, *, announce_recovery=True):
    """Reuse the important mic-warning path and add its missing action button.

    This nonmodal notice is independent of the Compact/Full display choice.
    It never activates itself or changes the current capture session.
    """
    watch = vars(app).get('_mic_silence_watch_1252')
    version = None if watch is None else watch.notice_version

    def show():
        if (watch is None or vars(app).get('_mic_silence_watch_1252') is not watch
                or watch.active != active or watch.notice_version is not version):
            return  # Drop a stale queued warning/recovery.
        from samsara.live_surface.feedback import schedule_feedback, schedule_owner_clear
        window = getattr(app, '_mic_silence_notice_1252', None)
        if not active:
            if window is not None:
                window.hide()
            schedule_owner_clear(app, 'capture_mic_silent')
            if announce_recovery:
                schedule_feedback(app, RECOVERED, 'success', 3000)
            return
        body = TITLE + '\n' + name + '\n\n' + HELP
        schedule_feedback(app, body, 'warning', None, source='capture_mic_silent')
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout
        from samsara.ui import theme
        from samsara.ui.mic_check_qt import open_mic_check
        if window is None:
            window = QDialog()
            window.setWindowTitle(TITLE)
            window.setModal(False)
            window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
            window.setStyleSheet(theme.build_stylesheet() + f'QDialog {{background:{theme.BG0}; color:{theme.TEXT_PRIMARY};}}')
            layout = QVBoxLayout(window)
            layout.setContentsMargins(24, 20, 24, 20)
            window.message = QLabel()
            window.message.setTextFormat(Qt.TextFormat.PlainText)
            window.message.setWordWrap(True)
            layout.addWidget(window.message)
            row = QHBoxLayout()
            check = QPushButton('Check my mic')  # PLACEHOLDER COPY (1252)
            check.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            check.clicked.connect(lambda: open_mic_check(app))
            row.addWidget(check)
            dismiss = QPushButton('Dismiss')  # PLACEHOLDER COPY (1252)
            dismiss.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            dismiss.clicked.connect(window.reject)
            window.rejected.connect(lambda: schedule_owner_clear(app, 'capture_mic_silent'))
            row.addWidget(dismiss)
            row.addStretch()
            layout.addLayout(row)
            window.resize(520, 230)
            app._mic_silence_notice_1252 = window
        window.message.setText(body)
        window.show()
    app._schedule_ui(show)
