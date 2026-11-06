"""Entry-bound History links for dictation results; never persist retry PCM."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import threading

_state = threading.local()


@dataclass
class _Entry:
    app: object
    entry_id: int | None = None
    fingerprint: bytes = b''
    used: bool = False
    generation: int = 0

    def __call__(self):
        open_entry(self.app, self.entry_id)


def _fingerprint(text):
    return sha256(str(text or '').strip().encode()).digest()


def _generation(app):
    value = getattr(app, '_history_capture_generation', 0)
    return value if isinstance(value, int) else 0


def fence_capture(app):
    app._history_capture_generation = _generation(app) + 1


def remember_entry(app, entry_id, text=''):
    if isinstance(entry_id, int) and entry_id > 0:
        fingerprint = _fingerprint(text)
        generation = _generation(app)
        _state.last = _Entry(app, entry_id, fingerprint, generation=generation)
        pending = getattr(_state, 'delivery', None)
        if (pending and pending.app is app and pending.entry_id is None
                and pending.generation == generation and pending.fingerprint == fingerprint):
            pending.entry_id = entry_id
            _state.last.used = True


def entry_for(app):
    saved = getattr(_state, 'last', None)
    return saved.entry_id if saved and saved.app is app else None


def prepare_delivery(app, text):
    """History may be written immediately before OR after delivery feedback."""
    fingerprint = _fingerprint(text)
    previous = getattr(_state, 'last', None)
    generation = _generation(app)
    if (previous and previous.app is app and not previous.used
            and previous.generation == generation and previous.fingerprint == fingerprint):
        previous.used = True
        target = _Entry(app, previous.entry_id, fingerprint, True, generation)
    else:
        target = _Entry(app, fingerprint=fingerprint, used=True, generation=generation)
    _state.delivery = target


def link_for(app, label):
    bound = getattr(_state, 'bound', None)
    if 'History' not in label:
        return None
    if bound and bound[0] is app:
        return _Entry(app, bound[1]) if bound[1] else None
    delivery = getattr(_state, 'delivery', None)
    if delivery and delivery.app is app:
        return delivery
    entry_id = entry_for(app)
    return _Entry(app, entry_id) if entry_id else None


@contextmanager
def bind_entry(app, entry_id):
    previous = getattr(_state, 'bound', None)
    _state.bound = (app, entry_id)
    try:
        yield
    finally:
        _state.bound = previous


def retry_entry(app, slot, label):
    """Keep text-free failure details on the original take's retry record."""
    entry_id = getattr(slot, 'history_entry_id', None)
    if entry_id:
        return entry_id
    store = getattr(app, 'history_db', None)
    if store is None:
        return None
    try:
        entry_id = store.add(raw_text='', display_text=label, mode='streaming',
                             status='failed', entry_type='failed')
        if slot is not None:
            slot.history_entry_id = entry_id
        return entry_id
    except Exception:
        return None


def history_label(label, entry_id):
    if not entry_id or 'History' in label:
        return label
    suffix = ' Retry details kept in History.' if "Still couldn't" in label else ' Details kept in History.'
    return label.rstrip().rstrip('.') + '.' + suffix


def open_entry(app, entry_id):
    """Marshal navigation onto Qt, including a not-yet-created hub."""
    from samsara.ui import qt_runtime, window_activation

    def show():
        main = getattr(app, 'main_window', None)
        if main is None:
            return
        window = getattr(main, '_window', None)
        if window is None and hasattr(main, '_activate'):
            window = main
        if window is None:
            main.show()
            qt_runtime.post(select)
        else:
            select()

    def select():
        main = getattr(app, 'main_window', None)
        window = getattr(main, '_window', None)
        if window is None and hasattr(main, '_activate'):
            window = main
        if window is not None:
            view = window._activate('History')
            view.open_entry(entry_id)
            window_activation.surface_requested(window)

    qt_runtime.post(show)
