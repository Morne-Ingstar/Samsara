"""Serialize every sounddevice stream lifetime against global reinitialisation.

Installed before the first wizard. Instrument the common base, not individual
factories: RawStream, InputStream and play/rec convenience streams all use it.
No stream is closed by this registry. Owners retain exclusive close authority.
"""
from contextlib import contextmanager
from functools import wraps
import threading


class RescanDeferred(RuntimeError):
    pass


class StreamGuard:
    def __init__(self):
        self.lock = threading.RLock()
        self.streams = {}  # Strong references until close; never lose a live handle.

    @contextmanager
    def reserve(self, owned=()):
        if not self.lock.acquire(blocking=False):
            raise RescanDeferred('Stream open/close in progress')
        try:
            allowed = {id(s) for s in owned if s is not None}
            if any(key not in allowed for key in self.streams):
                raise RescanDeferred('Another audio stream is open')
            yield
        finally:
            self.lock.release()


def install(sd):
    existing = getattr(sd, '_samsara_stream_guard', None)
    if existing is not None:
        return existing
    guard = StreamGuard()
    base = sd._StreamBase
    original_init, original_close = base.__init__, base.close

    @wraps(original_init)
    def initialise(stream, *args, **kwargs):
        with guard.lock:
            original_init(stream, *args, **kwargs)
            guard.streams[id(stream)] = stream

    @wraps(original_close)
    def close(stream, *args, **kwargs):
        with guard.lock:
            result = original_close(stream, *args, **kwargs)
            guard.streams.pop(id(stream), None)
            return result

    base.__init__, base.close = initialise, close
    # Device-info pointers also belong to PortAudio. Finish copying query
    # results before a reset can free its cached table on another thread.
    def serial_query(query):
        @wraps(query)
        def call(*args, **kwargs):
            with guard.lock:
                return query(*args, **kwargs)
        return call
    for name in ('query_devices', 'query_hostapis'):
        query = getattr(sd, name, None)
        if query is not None:
            setattr(sd, name, serial_query(query))
    sd._samsara_stream_guard = guard
    return guard
