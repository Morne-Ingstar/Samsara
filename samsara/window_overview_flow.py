"""Interruptible overview readback; no waits run on the command or Qt thread."""
import sys
import threading
import time

from samsara.log import get_logger
from samsara.runtime import thread_registry

log = get_logger(__name__)


def observe_audio_start(handle, started):
    """Record engine playback onset separately from cheap queue submission."""
    def observe():
        tick = threading.Event()
        while time.perf_counter() - started < 15:
            latency = getattr(handle, 'audio_started_ms', None)
            state = getattr(handle, '_state', None)
            if isinstance(latency, (int, float)) or state in {'playing', 'done'}:
                elapsed = latency if isinstance(latency, (int, float)) else (
                    time.perf_counter() - started) * 1000
                log.info('Overview close audio started: elapsed_ms=%.1f', elapsed)
                return
            if state in {'cancelled', 'cancelling', 'failed'}:
                return
            tick.wait(.02)
        log.info('Overview close audio onset not observed within 15000 ms')
    thread_registry.spawn('overview.confirm-latency', observe, daemon=True)


def before_command(app, text, *, replies=False):
    # Do not load a Windows plugin just to dispatch an unrelated command.
    overview = sys.modules.get('plugins.commands.window_switcher')
    return bool(overview and overview.overview_command_input(app, text, replies=replies))


class ReadSequence:
    """One producer, one outstanding TTS item, completion-paced and cancellable."""

    def __init__(self, coordinator, items, highlight, finished=lambda: None):
        self.coordinator = coordinator
        self.items = tuple(items)  # frozen visible-card count is the hard cap
        self.highlight = highlight
        self.finished = finished
        self.cancelled = threading.Event()
        self._lock = threading.RLock()
        self._active = False

    def start(self):
        thread_registry.spawn('overview.read', self.run, daemon=True)

    def cancel(self):
        with self._lock:
            self.cancelled.set()
            if self._active:
                self.coordinator.cancel_speech()
                self._active = False
            self.highlight(None)

    def _pause(self, seconds):
        return self.cancelled.wait(seconds)

    def run(self):
        try:
            for index, (number, text) in enumerate(self.items):
                done = threading.Event()
                with self._lock:
                    if self.cancelled.is_set():
                        return
                    started = time.perf_counter()
                    self._active = True
                    handle = self.coordinator.speak(
                        text, category='readback', queue_mode='append', on_done=done.set)
                    self.highlight(number)
                if str(getattr(handle, 'utterance_id', '')).startswith('noop-'):
                    done.set()  # muted/disabled speech has no completion callback
                logged = False
                while not done.wait(.02):
                    if self.cancelled.is_set():
                        return
                    if not logged and self.coordinator.engine.get_engine_state() == 'playing':
                        log.info('Overview read audio started: number=%d elapsed_ms=%.1f',
                                 number, (time.perf_counter() - started) * 1000)
                        logged = True
                with self._lock:
                    self._active = False
                    if self.cancelled.is_set():
                        return
                    self.highlight(None)
                if index + 1 < len(self.items) and self._pause(1.5 if (index + 1) % 6 == 0 else .25):
                    return
        except Exception:
            log.exception('Window overview readback failed')
        finally:
            with self._lock:
                self.highlight(None)
                self.finished()
