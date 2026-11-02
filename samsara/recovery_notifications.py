"""Grace and repeat suppression for automatically recovering input devices."""
from collections import deque
import logging
import threading
import time

from samsara.runtime import thread_registry

RECOVERY_NOTICE_GRACE_SECONDS = 5.0
RECOVERY_NOTICE_REPEAT_WINDOW_SECONDS = 60.0
RECOVERY_NOTICE_REPEAT_COUNT = 3
RECOVERY_NOTICE_COOLDOWN_SECONDS = 600.0

log = logging.getLogger('Samsara')
_creation_lock = threading.Lock()


class RecoveryNotice:
    """One notice per outage; retain recent episodes/cooldown across recovery.

    A timer covers outages with no further callbacks. The generation and live
    predicate also reject an already-queued timer after recovery/replacement.
    """

    def __init__(self, *, clock=None, schedule=None):
        self.clock = clock or time.monotonic
        self.schedule = schedule or self._schedule
        self._lock = threading.RLock()
        self._episodes = deque(maxlen=RECOVERY_NOTICE_REPEAT_COUNT)
        self._last_notice = float('-inf')
        self._generation = 0
        self._active = False
        self._timer = None

    @staticmethod
    def _schedule(delay, callback):
        return thread_registry.timer('input-recovery-notice', delay, callback, daemon=True)

    def recovered(self):
        with self._lock:
            self._generation += 1
            self._active = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def interrupted(self, deliver, still_broken):
        """Repeated observations of one outage do not count as new blips.

        deliver returns False if the tray is not ready; a later observation can
        retry without consuming the cooldown or starting another grace window.
        """
        with self._lock:
            self._deliver, self._still_broken = deliver, still_broken
            now = self.clock()
            if not self._active:
                self._active = True
                self._noticed = False
                self._started = now
                self._generation += 1
                while self._episodes and now - self._episodes[0] > RECOVERY_NOTICE_REPEAT_WINDOW_SECONDS:
                    self._episodes.popleft()
                self._episodes.append(now)
                if len(self._episodes) < RECOVERY_NOTICE_REPEAT_COUNT:
                    generation = self._generation
                    self._timer = self.schedule(
                        RECOVERY_NOTICE_GRACE_SECONDS, lambda: self._fire(generation))
                    return
            if (len(self._episodes) >= RECOVERY_NOTICE_REPEAT_COUNT
                    or now - self._started >= RECOVERY_NOTICE_GRACE_SECONDS):
                self._fire(self._generation)

    def _fire(self, generation):
        with self._lock:
            if generation != self._generation or not self._active or self._noticed:
                return
            if not self._still_broken():
                self.recovered()
                return
            now = self.clock()
            if now - self._last_notice < RECOVERY_NOTICE_COOLDOWN_SECONDS:
                return
            try:
                if self._deliver() is False:
                    return
            except Exception:
                log.debug('[RECOVERY] Input warning delivery failed', exc_info=True)
                return
            self._noticed = True
            self._last_notice = now


def notice_for(app, attribute):
    """Lazily support app delegates and stand-ins, including concurrent callers."""
    with _creation_lock:
        notice = getattr(app, attribute, None)
        if notice is None:
            notice = RecoveryNotice()
            setattr(app, attribute, notice)
        return notice
