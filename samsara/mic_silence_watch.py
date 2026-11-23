"""1252: passive dead-input observations; never decide capture or decoding.

Only numeric levels survive a poll. No hardware is opened and no PCM is saved.
The narrow floor includes the reported ~1e-4 RMS, not ordinary quiet speech.
"""
import logging
import math
import threading
import time

from samsara.wake_noise_notice import WakeNoiseNotice


DEAD_RMS = .00015
LISTEN_SECONDS = 10.
HOLD_SECONDS = .1
TITLE = 'No sound from your microphone'  # PLACEHOLDER COPY (1252)
HELP = ('Check the mic cable, the selected input on your audio interface, '
        'the mute switch and the Windows input level. '
        'You can also choose another microphone in Settings > Audio.')  # PLACEHOLDER COPY (1252)
RECOVERED = 'Microphone sound is back.'  # PLACEHOLDER COPY (1252)
_create_lock = threading.RLock()
log = logging.getLogger('Samsara')


def excluded(app, *, hold=False):
    """Observe existing playback/duck state, including the TTS echo tail."""
    coordinator = getattr(app, 'audio_coordinator', None)
    owners = getattr(app, '_hands_free_capture_duck_owners', ())
    # A negative token is this hold's media-output duck. It does not mute the
    # microphone; captured evidence separately excludes its setup/restore work.
    ducked = any(token >= 0 for token in owners) if hold else bool(
        owners or getattr(app, '_hands_free_capture_ducker', None) is not None)
    return bool(
        getattr(coordinator, 'is_speaking', False)
        or time.monotonic() - getattr(app, '_tts_last_speaking', float('-inf')) < .3
        or ducked or getattr(app, '_hands_free_capture_duck_starting', False)
        or time.monotonic() < getattr(app, '_audio_output_idle_after', 0.)
        or getattr(app, '_playback_buffer', ()) is not None
        and len(getattr(app, '_playback_buffer', ())) > 0)


def device_name(app):
    from samsara.mic_score import microphone_name
    return microphone_name(app) or 'System default microphone'  # PLACEHOLDER COPY (1252)


class SilenceWatch:
    def __init__(self, app, deliver=None):
        self.app = app
        self.deliver = deliver or (lambda active, name: present(app, active, name))
        self.lock = threading.RLock()
        self.active = False
        self.identity = None
        self.dead_seconds = 0.
        self.last_stamp = None
        self.last_seq = None
        self.engine = self.reader = None
        self.was_excluded = False
        self.excluded_until = float('-inf')
        self.wake_noise_notice = WakeNoiseNotice(app)

    def reset_run(self):
        self.dead_seconds = 0.
        self.last_stamp = self.last_seq = None

    def transition(self, active, source, maximum, seconds):
        if active == self.active:
            return
        self.active = active
        name = device_name(self.app)
        log.info('[MIC_SILENT] state=%s source=%s max_rms=%.7f floor=%.7f seconds=%.2f',
                 'silent' if active else 'recovered', source, maximum, DEAD_RMS, seconds)
        # Capture's observer queues UI work only; failures never escape it.
        try:
            self.deliver(active, name)
        except Exception:
            log.debug('[MIC_SILENT] notice delivery unavailable')

    def hold(self, levels, evidence):
        if len(levels) == 0 or not all(math.isfinite(x) and x >= 0 for x in levels):
            return
        maximum = max(levels)
        with self.lock:
            if maximum > DEAD_RMS:
                self.reset_run()
                self.transition(False, 'hold', maximum, len(levels) * .1)
                return
            if excluded(self.app, hold=True):
                return
            # Existing evidence times cue/duck operations on the monotonic clock.
            # Do not count affected frames as proof of a dead input. Ordinary
            # output duck setup excludes only its operation, not the whole take.
            end = evidence.stopped
            start = end - len(levels) * .1
            clean = sum(not any(start + (i + 1) * .1 > a and start + i * .1 < b
                                for a, b in evidence.intervals)
                        for i in range(len(levels))) * .1
            if clean >= HOLD_SECONDS:
                self.transition(True, 'hold', maximum, clean)

    def level(self, rms, stamp, seq, identity, *, listening, skip=False):
        with self.lock:
            if identity != self.identity:
                self.identity = identity
                self.reset_run()
                # A new endpoint is not evidence that sound has returned.
            if skip or not math.isfinite(rms) or rms < 0:
                self.reset_run()
                return
            if rms > DEAD_RMS:
                self.reset_run()
                self.transition(False, 'listening', rms, .1)
                return
            if not listening:
                self.reset_run()
                return
            contiguous = (self.last_seq is not None and seq == self.last_seq + 1
                          and 0 < stamp - self.last_stamp <= .25)
            self.dead_seconds = self.dead_seconds + .1 if contiguous else .1
            self.last_stamp, self.last_seq = stamp, seq
            if self.dead_seconds + 1e-6 >= LISTEN_SECONDS:
                self.transition(True, 'listening', rms, self.dead_seconds)

    def detach(self):
        engine, reader = self.engine, self.reader
        self.engine = self.reader = None
        self.reset_run()
        self.wake_noise_notice.begin_poll(0., active=False)
        if engine is not None and reader is not None:
            engine.unregister_consumer(reader)

    def poll(self):
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
        holding = bool(getattr(self.app, 'recording', False)
                       or getattr(self.app, '_hotkey_recording', False))
        listening = bool(getattr(self.app, 'continuous_active', False)
                         or getattr(self.app, 'wake_word_active', False)) and not holding
        check_noise = self.wake_noise_notice.begin_poll(
            now, active=getattr(self.app, 'wake_word_active', False))
        before = self.reader.overrun_count
        # Bounded work, even if a synthetic writer produces frames without pause.
        for _ in range(1000 // FRAME_MS * 2):
            frame = self.reader.read_next()
            if frame is EMPTY:
                if self.last_stamp is not None and now - self.last_stamp > .5:
                    self.reset_run()
                break
            pcm = frame.pcm.astype(np.float64) / 32767.
            rms = float(np.sqrt(np.mean(pcm * pcm)))
            discontinuity = self.reader.overrun_count != before
            before = self.reader.overrun_count
            identity = (id(engine), getattr(engine, 'capture_name', None), frame.device_epoch)
            ineligible = (skip or discontinuity or frame.t_capture <= self.excluded_until
                          or now - frame.t_capture > 1. or frame.t_capture > now or holding)
            self.level(rms, frame.t_capture, frame.seq, identity, listening=listening, skip=ineligible)
            if check_noise:
                self.wake_noise_notice.observe(
                    rms, frame.t_capture, frame.seq, identity, skip=ineligible)


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
        watch.detach()


def present(app, active, name):
    """Reuse the important mic-warning path and add its missing action button.

    This nonmodal notice is independent of the Compact/Full display choice.
    It never activates itself or changes the current capture session.
    """
    def show():
        watch = vars(app).get('_mic_silence_watch_1252')
        if watch is None or watch.active != active:
            return  # Drop a stale queued warning/recovery.
        from samsara.live_surface.feedback import schedule_feedback, schedule_owner_clear
        window = getattr(app, '_mic_silence_notice_1252', None)
        if not active:
            if window is not None:
                window.hide()
            schedule_owner_clear(app, 'capture_mic_silent')
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
            dismiss.clicked.connect(window.hide)
            row.addWidget(dismiss)
            row.addStretch()
            layout.addLayout(row)
            window.resize(520, 230)
            app._mic_silence_notice_1252 = window
        window.message.setText(body)
        window.show()
    app._schedule_ui(show)
