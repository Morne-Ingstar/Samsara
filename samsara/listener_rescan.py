"""1206: lossless idle-only reservations shared by the always-on listeners."""
from contextlib import contextmanager

from samsara.portaudio_guard import RescanDeferred


@contextmanager
def pause_listener_if_idle(listener):
    """1206: reserve lifecycle and frame admission without draining buffers."""
    if not listener._lifecycle_lock.acquire(blocking=False):
        raise RescanDeferred('Listener lifecycle is changing')
    try:
        if not listener._rescan_lock.acquire(blocking=False):
            raise RescanDeferred('Listener is processing audio')
        try:
            check = lambda: check_listener_idle(listener)
            check()
            yield check
        finally:
            listener._rescan_lock.release()
    finally:
        listener._lifecycle_lock.release()


def check_listener_idle(listener):
    from samsara.microphone_recovery import capture_busy
    # Rechecked after ALL listeners are reserved, immediately before closing.
    if (capture_busy(listener._app)
            or getattr(listener, '_utterance_frames', ())
            or getattr(listener, '_toggle_queue', ())
            or getattr(listener, '_toggle_worker_active', False)
            or getattr(listener, '_is_speaking', False)
            or getattr(listener, '_speech_frames', ())):
        raise RescanDeferred('Listener has speech or uncommitted audio')
    if (listener._running and
            listener._reader._read_cursor != listener._reader._bus.write_cursor):
        raise RescanDeferred('Listener has unread audio')


def poll_listener_frame(listener, stop_event):
    from samsara.audio_engine.ring import EMPTY
    # A frame cannot be read outside admission and classified during a rescan.
    if not listener._rescan_lock.acquire(blocking=False):
        return False
    try:
        frame = listener._reader.read_next()
        if stop_event.is_set() or frame is EMPTY:
            return False
        listener._process_frame(frame)
        return True
    finally:
        listener._rescan_lock.release()
