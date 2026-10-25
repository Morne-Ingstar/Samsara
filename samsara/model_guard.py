"""Protect speech choices at the config save/load boundary (845).

Authorization names exact values for one app and one save, never a generic
"user is in Settings" flag. Persisted provenance travels in the same atomic
config write; the separate checkpoint contains only the previous start's
speech choices, so an old writer cannot silently establish a new baseline.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import logging
from pathlib import Path
import re
import traceback

from samsara.config_defaults import DEFAULTS

logger = logging.getLogger(__name__)
KEYS = ('model_size', 'performance_mode', 'compute_type')
MARKER = '_speech_model_user_choice'
_authorization = ContextVar('speech_model_authorization', default=None)
_SAFE = {
    'model_size': {'tiny', 'base', 'small', 'medium', 'large', 'large-v1',
                   'large-v2', 'large-v3', 'large-v3-turbo', 'turbo',
                   'tiny.en', 'base.en', 'small.en', 'medium.en'},
    'performance_mode': {'fast', 'balanced', 'accurate'},
    'compute_type': {'default', 'auto', 'int8', 'int8_float16', 'int8_float32',
                     'int8_bfloat16', 'int16', 'float16', 'float32', 'bfloat16'},
}


def choices(config, defaults=DEFAULTS):
    return {key: copy.deepcopy(config.get(key, defaults[key])) for key in KEYS}


def _label(values):
    names = {'model_size': 'Model', 'performance_mode': 'Performance', 'compute_type': 'Precision'}
    return ', '.join(f'{names[key]}: {value if isinstance(value, str) and value in _SAFE[key] else "unknown"}'
                     for key, value in values.items())


def _caller():
    # Do not format_stack(): it includes source lines and can expose text.
    frames = traceback.extract_stack(limit=9)[:-2]
    return ' > '.join(f'{re.sub("[^a-zA-Z0-9_.-]", "_", Path(f.filename).name)}:{f.lineno}:{f.name}'
                      for f in frames)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


class ModelGuard:
    def __init__(self, app, defaults=None):
        self.app = app
        self.defaults = {**DEFAULTS, **(defaults or {})}
        self.accepted = self.project(app.config)
        self.reported = set()
        self.pending_notices = []
        self.notice_sink = None
        self.restored = False
        self.started = False
        self.checkpoint = Path(app.config_path).parent / 'config_backups/model_guard.json'
        self.profile = hashlib.sha256(str(Path(app.config_path).resolve()).casefold().encode()).hexdigest()

    def project(self, config):
        return choices(config, self.defaults)

    def report(self, before, attempted, caller):
        signature = json.dumps([before, attempted], sort_keys=True)
        if signature in self.reported:
            return
        self.reported.add(signature)
        logger.warning('[MODEL-GUARD] %s -> %s; kept user choice; caller=%s',
                       _label(before), _label(attempted), caller)
        # PLACEHOLDER COPY (845)
        message = f'Speech model was about to change to {_label(attempted)}; kept {_label(before)}.'
        if self.notice_sink is None:
            self.pending_notices.append(message)
        else:
            self.notice_sink(message)

    def start(self):
        self.started = True
        previous = _read(self.checkpoint)
        if previous is None:
            # First guarded start: LKG is a previous successful start; rolling
            # snapshots are pre-write states and provide weaker legacy evidence.
            previous = _read(self.checkpoint.parent / 'last_known_good.json')
            if previous is None:
                try:
                    backups = sorted(self.checkpoint.parent.glob('config-*.json'),
                                     key=lambda p: p.stat().st_mtime_ns, reverse=True)
                except OSError:
                    backups = []
                previous = next((data for p in backups if (data := _read(p)) is not None), None)
        current = self.accepted
        marker = self.app.config.get(MARKER)
        authorized = (isinstance(marker, dict) and marker.get('source') in ('settings', 'voice')
                      and marker.get('profile') == self.profile and marker.get('values') == current)
        if previous is not None:
            before = self.project(previous)
            if before != current and not authorized:
                self.report(before, current, 'startup/config_backups')
                self.accepted = before
                self.app.config.update(copy.deepcopy(before))
                self.restored = True
        self.write_checkpoint()

    def write_checkpoint(self):
        try:
            self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.checkpoint.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.accepted), encoding='utf-8')
            temporary.replace(self.checkpoint)
        except OSError:
            # Deliberately omit exception text: paths can contain private data.
            logger.warning('[MODEL-GUARD] Could not save previous-start checkpoint')

    def protect(self, merged):
        authorization = _authorization.get()
        expected = authorization[1] if authorization and authorization[0] is self.app else {}
        attempted = self.project(merged)
        if attempted == self.accepted:
            return merged
        result = copy.deepcopy(merged)
        blocked = {key: attempted[key] for key in KEYS
                   if attempted[key] != self.accepted[key]
                   and (key not in expected or attempted[key] != expected[key])}
        if blocked:
            self.report({key: self.accepted[key] for key in blocked}, blocked, _caller())
            for key in blocked:
                result[key] = copy.deepcopy(self.accepted[key])
                self.app.config[key] = copy.deepcopy(self.accepted[key])
        # Imported markers never authorize an in-process save.
        if self.project(result) != self.accepted:
            result[MARKER] = {'source': authorization[2], 'profile': self.profile,
                              'values': self.project(result)}
        return result


def loaded(app, defaults=None):
    """Called before migrations can mutate or save the loaded configuration."""
    app._model_guard = ModelGuard(app, defaults)
    app._model_guard.start()


def finish_load(app):
    if getattr(app, '_config_load_failed', False):
        return
    guard = getattr(app, '_model_guard', None)
    if not isinstance(guard, ModelGuard) or not guard.started:
        loaded(app)
        guard = app._model_guard
    if guard.restored:
        app.save_config()
        guard.restored = False


def protect_save(app, merged):
    guard = getattr(app, '_model_guard', None)
    if not isinstance(guard, ModelGuard):
        # First-run saves and lightweight hosts: establish only from disk or
        # the last loaded snapshot, never from already-mutated memory.
        baseline = getattr(app, '_config_last_disk_snapshot', None)
        if not isinstance(baseline, dict) or not any(key in baseline for key in KEYS):
            baseline = _read(Path(app.config_path))
        if not isinstance(baseline, dict) or not any(key in baseline for key in KEYS):
            return merged
        guard = app._model_guard = ModelGuard(app)
        guard.accepted = guard.project(baseline)
    return guard.protect(merged)


def saved(app):
    """Advance accepted values only after the atomic config write succeeds."""
    guard = getattr(app, '_model_guard', None)
    if isinstance(guard, ModelGuard):
        guard.accepted = guard.project(app.config)
    elif any(key in app.config for key in KEYS):
        app._model_guard = ModelGuard(app)


@contextmanager
def user_change(app, updates, *, source):
    """Use only at explicit Settings Apply / explicit speech voice commands."""
    if source not in ('settings', 'voice'):
        raise ValueError('Unsupported speech choice source')
    token = _authorization.set((app, {k: copy.deepcopy(v) for k, v in updates.items() if k in KEYS}, source))
    try:
        yield
    finally:
        _authorization.reset(token)


def settings_choices(window, updates):
    opened = getattr(window, '_config_at_open', window.app.config)
    return {key: copy.deepcopy(updates[key]) for key in KEYS
            if key in updates and updates[key] != opened.get(key, DEFAULTS[key])}


def save_settings(app, updates):
    with user_change(app, updates, source='settings'):
        app.save_config()


_dialogs = set()


def show_notice(message, parent=None):
    """Qt-thread notice shared by Compact and Full, independent of tray policy."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    from samsara.ui import theme
    dialog = QMessageBox(QMessageBox.Icon.Warning, 'Speech settings protected',
                         message, QMessageBox.StandardButton.Ok, parent)
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setStyleSheet(f'QMessageBox {{ background: {theme.BG0}; color: {theme.TEXT_PRIMARY}; }}')
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    _dialogs.add(dialog)
    dialog.finished.connect(lambda _result: _dialogs.discard(dialog))
    dialog.show()
    return dialog


def attach_notices(app):
    """Called once the real Qt tray exists; flush warnings raised during load."""
    from samsara.ui import qt_runtime
    guard = getattr(app, '_model_guard', None)
    if not isinstance(guard, ModelGuard):
        return
    guard.notice_sink = lambda message: qt_runtime.post(lambda: show_notice(message))
    for message in guard.pending_notices:
        guard.notice_sink(message)
    guard.pending_notices.clear()
