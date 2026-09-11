"""Wake dispatch primitives, importable without the desktop app or models."""

from collections import deque
import threading

from samsara import flight_recorder
from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)


def dispatch_queue_depth(config):
    """Number of waiting utterances; the currently decoding utterance is separate."""
    wake = config.get('wake_word', {})
    session = wake.get('session', {}) if isinstance(wake, dict) else {}
    try:
        depth = int(session.get('dispatch_queue_depth', 4))
    except (TypeError, ValueError, OverflowError):
        return 4
    return depth if depth > 0 else 4


class TranscriptionOwners:
    """A completion can release only its own token in its own lane."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tokens = {}

    def claim(self, lane, *, only_if_idle=False):
        with self._lock:
            if only_if_idle and lane in self._tokens:
                return None
            token = object()
            self._tokens[lane] = token
            return token

    def release(self, lane, token):
        with self._lock:
            if self._tokens.get(lane) is token:
                self._tokens.pop(lane, None)

    def current(self, lane):
        with self._lock:
            return self._tokens.get(lane)


class WakeDispatchQueue:
    """One FIFO worker with bounded waiting audio and exactly-once cleanup."""

    def __init__(self, config):
        self.depth = dispatch_queue_depth(config)
        self._lock = threading.Lock()
        self._pending = deque()
        self._active = False

    @staticmethod
    def _finish(job):
        try:
            job[1]()
        except Exception:
            logger.exception('[WAKE] Dispatch cleanup failed')

    def enqueue(self, run, finish):
        dropped = []
        with self._lock:
            if len(self._pending) >= self.depth:
                dropped.append(self._pending.popleft())
                logger.warning('[WAKE] Dispatch queue full (depth %s) -- dropping oldest utterance',
                               self.depth)
                flight_recorder.record('wake.dispatch_dropped', reason='overflow',
                                       queue_depth=self.depth, policy='oldest')
            self._pending.append((run, finish))
            if not self._active:
                self._active = True
                try:
                    thread_registry.spawn('wake-utt-queue', self._drain, daemon=True)
                except Exception:
                    self._active = False
                    dropped.extend(self._pending)
                    self._pending.clear()
                    logger.exception('[WAKE] Dispatch worker could not start')
                    flight_recorder.record('wake.dispatch_dropped', reason='worker_start_failed',
                                           count=len(dropped))
        for job in dropped:
            self._finish(job)

    def _drain(self):
        while True:
            with self._lock:
                if not self._pending:
                    self._active = False
                    return
                job = self._pending.popleft()
            try:
                job[0]()
            except Exception:
                logger.exception('[WAKE] Dispatch failed; continuing FIFO')
            finally:
                self._finish(job)
