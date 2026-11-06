"""1172: serialize native typing while Qt continues servicing its events.

Only native pairs run on the worker. The synchronous receipt returns to the
original caller through a local Qt event loop, so session transactions and
undo/feedback stay on their owning thread. Admission stays closed throughout
that transaction: a reentrant delivery cancels it and is refused, leaving the
newer words with the caller's existing History/draft recovery path.
"""
from contextvars import ContextVar
import threading

from samsara.runtime import thread_registry


_scope = ContextVar('typed_delivery_scope', default=None)
_state_lock = threading.Lock()
_native_lane = threading.Lock()
_jobs = set()


def cancel(app):
    """Cancel this app's active delivery without waiting for Qt or the worker."""
    with _state_lock:
        event = getattr(app, '_typed_delivery_cancel', None)
        if event is not None:
            event.set()


def _cancel_all():
    with _state_lock:
        for event in _jobs:
            event.set()


thread_registry.on_shutdown(_cancel_all)


def deliver(fn, app, text, **kwargs):
    """Keep one entire paste/typing transaction per app, including receipts."""
    with _state_lock:
        active = getattr(app, '_typed_delivery_cancel', None)
        if active is not None:
            active.set()
            if kwargs.get('return_delivery_confirmation'):
                from samsara.paste import DeliveryResult
                from samsara.clipboard import DeliveryOutcome
                return DeliveryResult(False, False, DeliveryOutcome.UNVERIFIED)
            return False
        cancelled = threading.Event()
        app._typed_delivery_cancel = cancelled
    token = _scope.set(cancelled)
    try:
        # Also fence clipboard delivery if Stop arrived during preparation.
        guard = kwargs.get('before_paste')
        kwargs['before_paste'] = lambda: not cancelled.is_set() and (guard is None or guard())
        return fn(app, text, **kwargs)
    finally:
        _scope.reset(token)
        with _state_lock:
            app._typed_delivery_cancel = None


def run_paced(operation):
    """Run native pairs in one shared lane; Qt callers await a queued receipt.

    Non-Qt callers already run outside the display thread and use the same
    serialization/cancellation boundary directly. No application is started.
    A Qt shutdown returns None (unknown dispatch), never a successful receipt.
    """
    from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QThread, Qt, Signal, Slot
    app = QCoreApplication.instance()
    cancelled = _scope.get()
    if cancelled is None:
        cancelled = threading.Event()
    with _state_lock:
        _jobs.add(cancelled)

    def execute():
        try:
            with _native_lane:
                return operation(cancelled)
        finally:
            with _state_lock:
                _jobs.discard(cancelled)

    if app is None or QThread.currentThread() != app.thread():
        return execute()

    loop = QEventLoop()

    class Reply(QObject):
        ready = Signal(object)

        def __init__(self):
            super().__init__()
            self.result = None
            self.error = None

        @Slot(object)
        def receive(self, outcome):
            self.result, self.error = outcome
            loop.quit()

    reply = Reply()
    reply.ready.connect(reply.receive, Qt.ConnectionType.QueuedConnection)

    def shutdown():
        cancelled.set()
        loop.quit()

    app.aboutToQuit.connect(shutdown)

    def worker():
        try:
            outcome = (execute(), None)
        except Exception as exc:
            outcome = (None, exc)
        reply.ready.emit(outcome)

    try:
        thread_registry.spawn('typing.native_pairs', worker)
        loop.exec()
        if reply.error is not None:
            raise reply.error
        return reply.result
    finally:
        app.aboutToQuit.disconnect(shutdown)
