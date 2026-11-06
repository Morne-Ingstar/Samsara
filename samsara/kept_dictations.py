"""Owner-selected audio evidence. Private profile storage; never log content.

The last completed take stays in RAM for ten minutes, even with collection off.
Disk writes use one serialized worker and opaque identifiers. Delete invalidates
queued writes and the RAM slot, so an old completion cannot restore a take.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import json
import logging
import re
import shutil
import threading
import time
import uuid
import wave

import numpy as np

from samsara.benchmark_store import _to_int16_pcm
from samsara.paths import samsara_home_dir

log = logging.getLogger(__name__)
_init_lock = threading.Lock()
_wake = ContextVar('kept_wake_audio', default=None)
LAST_TTL_S = 600
KEEP_PHRASES = {'keep that', 'save that dictation'}


def _warning():
    # Exception messages and tracebacks may contain private text or paths.
    try:
        log.warning('[KEPT] operation_failed=1')
    except Exception:
        pass


def is_keep(text):
    return str(text or '').strip().rstrip('.!?').casefold() in KEEP_PHRASES


class KeptStore:
    def __init__(self, folder, config=None, clock=time.monotonic):
        self.folder = Path(folder)
        self.config = config or (lambda: {})
        self.clock = clock
        self.lock = threading.RLock()
        self.worker = None
        self.generation = 0
        self.last = None
        self.armed = False
        self.deleted = set()
        self.order = 0

    def _rows(self):
        rows = []
        if self.folder.exists():
            for path in self.folder.glob('*.json'):
                if not re.fullmatch(r'[0-9a-f]{32}', path.stem):
                    continue
                try:
                    row = json.loads(path.read_text(encoding='utf-8'))
                    if row['id'] == path.stem and self.audio_path(row['id']).is_file():
                        rows.append(row)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return sorted(rows, key=lambda row: (row['timestamp'], row.get('order', 0)), reverse=True)

    def list(self):
        with self.lock:
            return self._rows()

    def audio_path(self, take_id):
        if not isinstance(take_id, str) or not re.fullmatch(r'[0-9a-f]{32}', take_id):
            raise ValueError('Invalid take identifier')
        return self.folder / (take_id + '.wav')

    def _queue(self, take, explicit=False):
        if self.worker is None:
            self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='kept-dictations')
        # Snapshot before the next capture or delivery updates this take.
        snapshot = dict(take, metadata=dict(take['metadata']))
        self.worker.submit(self._write, snapshot, explicit)

    def _write(self, take, explicit):
        try:
            with self.lock:
                if take['generation'] != self.generation or take['metadata']['id'] in self.deleted:
                    return
                if not explicit and not self.config().get('enabled', False):
                    return
                self.folder.mkdir(parents=True, exist_ok=True)
                row = take['metadata']
                wav = self.audio_path(row['id'])
                sidecar = wav.with_suffix('.json')
                try:
                    if not wav.exists():
                        with wave.open(str(wav), 'wb') as output:
                            output.setnchannels(1)
                            output.setsampwidth(2)
                            output.setframerate(row['sample_rate'])
                            output.writeframes(_to_int16_pcm(take['audio']).tobytes())
                    temp = sidecar.with_suffix('.tmp')
                    temp.write_text(json.dumps(row, ensure_ascii=False, allow_nan=False), encoding='utf-8')
                    temp.replace(sidecar)
                except Exception:
                    wav.unlink(missing_ok=True)
                    sidecar.unlink(missing_ok=True)
                    sidecar.with_suffix('.tmp').unlink(missing_ok=True)
                    raise
                maximum = max(1, min(500, int(self.config().get('maximum', 50))))
                for old in self._rows()[maximum:]:
                    self._remove(old['id'])
        except Exception:
            _warning()

    def remember(self, audio, rate, raw, final, *, path='hold', model='', device='',
                 status='ready', take_id=None, generation=None):
        if audio is None or not np.asarray(audio).size or rate <= 0:
            return
        with self.lock:
            if generation is not None and generation != self.generation:
                return
            take_id = take_id or uuid.uuid4().hex
            if take_id in self.deleted:
                return
            pcm = np.array(audio, copy=True).reshape(-1)
            self.order = max(self.order + 1, time.time_ns())
            row = dict(id=take_id, order=self.order, timestamp=datetime.now(timezone.utc).isoformat(),
                       raw_transcript=raw or '', final_text=final or '', path=path,
                       model=model, device=device, status=status, sample_rate=int(rate),
                       duration_s=len(pcm) / rate)
            explicit = self.armed or (self.last is not None and self.last['metadata']['id'] == take_id
                                      and self.last.get('explicit', False))
            self.last = dict(audio=pcm, metadata=row, generation=self.generation,
                             expires=self.clock() + LAST_TTL_S, explicit=explicit)
            self.armed = False
            if explicit or self.config().get('enabled', False):
                self._queue(self.last, explicit)
            return take_id

    def keep(self):
        with self.lock:
            if self.last is not None and self.last['expires'] >= self.clock():
                self.last['explicit'] = True
                self._queue(self.last, True)
                return 'Kept in History > Kept'
            self.last = None
            self.armed = True
            return 'The next dictation will be kept'

    def _remove(self, take_id):
        wav = self.audio_path(take_id)
        wav.unlink(missing_ok=True)
        wav.with_suffix('.json').unlink(missing_ok=True)
        wav.with_suffix('.tmp').unlink(missing_ok=True)

    def delete(self, take_id):
        with self.lock:
            self.deleted.add(take_id)
            self._remove(take_id)
            if self.last is not None and self.last['metadata']['id'] == take_id:
                self.last = None

    def clear(self):
        with self.lock:
            self.generation += 1
            self.last = None
            self.armed = False
            self.deleted.clear()
            if self.folder.exists():
                for path in self.folder.iterdir():
                    if re.fullmatch(r'[0-9a-f]{32}', path.stem) and path.suffix in ('.wav', '.json', '.tmp'):
                        path.unlink(missing_ok=True)

    def export(self, take_id, destination):
        with self.lock:
            source = self.audio_path(take_id)
            if not any(row['id'] == take_id for row in self._rows()):
                raise ValueError('Take is unavailable')
            # Explicit export only. A new opaque subfolder never overwrites files.
            target = Path(destination) / uuid.uuid4().hex
            target.mkdir(parents=True, exist_ok=False)
            try:
                shutil.copyfile(source, target / source.name)
                shutil.copyfile(source.with_suffix('.json'), target / source.with_suffix('.json').name)
            except Exception:
                for child in target.iterdir():
                    child.unlink()
                target.rmdir()
                raise
            return target

    def flush(self):
        if self.worker is not None:
            self.worker.submit(lambda: None).result(timeout=10)


def store_for(app):
    with _init_lock:
        store = getattr(app, '_kept_dictations', None)
        if store is None:
            store = app._kept_dictations = KeptStore(
                samsara_home_dir() / 'kept_dictations',
                lambda: (app.config.get('kept_dictations') or {}))
        return store


def capture(app, audio, raw, final, path, **kwargs):
    try:
        policy = getattr(app, '_game_mode', None)
        model = (getattr(policy, '_active_name', None) if getattr(policy, 'active', False)
                 else getattr(policy, '_primary_name', None)) or app.config.get('model_size', '')
        device = getattr(getattr(app, 'model', None), '_device', None) or getattr(
            app, 'device_type', app.config.get('device', ''))
        return store_for(app).remember(audio, getattr(app, 'model_rate', 16000), raw, final,
            path=path, model=str(model), device=str(device), **kwargs)
    except Exception:
        _warning()


def request_keep(app, *, voice=False):
    try:
        store = store_for(app)
        current = getattr(app, '_streaming_session', None)
        active = (current is not None and getattr(current, '_state', '') not in ('done', 'cancelled'))
        if not voice and (active or getattr(app, 'recording', False)):
            with store.lock:
                store.armed = True
            message = 'This dictation will be kept'
        else:
            message = store.keep()
            if not voice:
                store.armed = True
                if store.last is not None:
                    message = 'Kept the last dictation; the next one will be kept too'
        # Shared feedback works with Compact and Full, and contains no content.
        if hasattr(app, '_show_outcome_chip'):
            app._schedule_ui(app._show_outcome_chip, message, 'success')
        return message
    except Exception:
        _warning()
        return 'Could not keep the dictation'


def capture_held(session, text, raw):
    try:
        if getattr(session, '_tutorial_owner', None) is not None:
            return
        if getattr(session, '_kept_observed', False):
            return
        session._kept_observed = True
        take_id = getattr(session, '_kept_take_id', None)
        audio = getattr(session, '_decode_audio', None)
        if audio is None:
            audio = getattr(session, '_stopped_audio_1042', None)
        session._kept_take_id = capture(session.app, audio, raw, text, 'hold', take_id=take_id,
            generation=getattr(session, '_kept_generation', None),
            status='ready' if text else 'audio retained')
    except Exception:
        _warning()


def begin_held(session):
    try:
        session._kept_generation = store_for(session.app).generation
    except Exception:
        _warning()


def handle_held_control(session, text):
    if not is_keep(text) or getattr(session, '_tutorial_owner', None) is not None:
        return False
    message = request_keep(session.app, voice=True)
    from samsara.streaming import StreamingOverlayQt
    session._overlay.update_text(message, StreamingOverlayQt.STATE_DONE)
    session._overlay.flash_done_and_fade(session._mark_done)
    return True


def observe_commit(method):
    @wraps(method)
    def observed(manager, *args, **kwargs):
        if getattr(manager, '_kept_dispatching', False):
            return method(manager, *args, **kwargs)
        app = getattr(getattr(manager, '_commit_redecode_fn', None), '__self__', None)
        try:
            refs = list(getattr(manager, '_dictate_pending_audio', ()))
            raw = getattr(manager, '_kept_raw', '') or getattr(manager, '_dictate_pending_buffer', '')
            generation = getattr(manager, '_kept_generation', store_for(app).generation) if app else None
        except Exception:
            _warning()
            return method(manager, *args, **kwargs)
        result = method(manager, *args, **kwargs)
        try:
            if app and result.kind == 'dictate_committed':
                arrays = [ref for ref in refs if ref is not None and np.asarray(ref).size]
                if arrays:
                    capture(app, np.concatenate(arrays), raw, result.detail.get('text', ''),
                        'hands-free', status='sent', generation=generation,
                        take_id=getattr(manager, '_kept_take_id', None))
                manager._kept_raw = ''
                manager._kept_take_id = None
                manager.__dict__.pop('_kept_generation', None)
        except Exception:
            _warning()
        return result
    return observed


def held_delivery(session, text, sent, confirmed):
    """Replace the proposed text with the actual bytes from the paste receipt."""
    try:
        store = store_for(session.app)
        take_id = getattr(session, '_kept_take_id', None)
        with store.lock:
            last = store.last
            if last is None or last['metadata']['id'] != take_id:
                return
            last['metadata']['final_text'] = text if sent else ''
            last['metadata']['status'] = 'typed' if confirmed else 'sent, unconfirmed' if sent else 'not typed'
            if last.get('explicit') or store.config().get('enabled', False):
                store._queue(last, last.get('explicit', False))
    except Exception:
        _warning()


def dispatch_session(manager, raw, signals, onset_context, timing):
    """Keep stage timing and serialization around the observed dispatch."""
    timing.mark("dispatch_start")
    with manager._dispatch_lock:
        outcome = _dispatch_session(manager, raw, signals, onset_context)
    timing.mark("action_done")
    return outcome


def _dispatch_session(manager, raw, signals, onset_context):
    """Observe the real result, including staged thoughts and final commit bytes."""
    app = getattr(getattr(manager, '_commit_redecode_fn', None), '__self__', None)
    if app is None:
        return manager._dispatch_utterance_locked(raw, signals, onset_context)
    if (is_keep(raw) and getattr(signals, 'has_contiguous_speech', False)
            and getattr(getattr(manager, 'mode', None), 'value', '') in ('command', 'dictate')):
        from samsara.session_modes import DispatchOutcome
        return DispatchOutcome('dictation_kept', {'message': request_keep(app, voice=True)})
    try:
        refs = list(getattr(manager, '_dictate_pending_audio', ()))
        if not refs and not getattr(manager, '_dictate_pending_buffer', ''):
            manager._kept_raw = ''
            manager._kept_take_id = None
            manager.__dict__.pop('_kept_generation', None)
        pending_raw = getattr(manager, '_kept_raw', '')
        generation = getattr(manager, '_kept_generation', store_for(app).generation)
    except Exception:
        _warning()
        return manager._dispatch_utterance_locked(raw, signals, onset_context)
    manager._kept_dispatching = True
    try:
        outcome = manager._dispatch_utterance_locked(raw, signals, onset_context)
    finally:
        manager._kept_dispatching = False
    try:
        audio = getattr(signals, 'audio_ref', None)
        if outcome.kind == 'dictate_staged':
            manager._kept_generation = generation
            manager._kept_raw = (pending_raw + ' ' + raw).strip()
            refs = manager._dictate_pending_audio
            arrays = [ref for ref in refs if ref is not None and np.asarray(ref).size]
            if arrays:
                manager._kept_take_id = capture(app, np.concatenate(arrays), manager._kept_raw, '',
                    'hands-free', status='staged', generation=generation,
                    take_id=getattr(manager, '_kept_take_id', None))
        elif outcome.kind in ('dictate_committed', 'dictate_injected'):
            arrays = [ref for ref in refs if ref is not None and np.asarray(ref).size]
            if audio is not None and np.asarray(audio).size and not any(ref is audio for ref in refs):
                arrays.append(audio)
            if arrays:
                capture(app, np.concatenate(arrays),
                    (pending_raw if str(raw).strip().rstrip('.!?').casefold() == 'send' else
                     (pending_raw + ' ' + raw).strip()), outcome.detail.get('text', ''),
                    'hands-free', status='sent', generation=generation,
                    take_id=getattr(manager, '_kept_take_id', None))
            manager._kept_raw = ''
            manager._kept_take_id = None
            manager.__dict__.pop('_kept_generation', None)
        elif not getattr(manager, '_dictate_pending_buffer', ''):
            manager._kept_raw = ''
            manager._kept_take_id = None
            manager.__dict__.pop('_kept_generation', None)
    except Exception:
        _warning()
    return outcome


def begin_wake(app, buffer, src_rate=None):
    """Start observation at runtime; class creation has no capture side effects."""
    try:
        rate = src_rate or getattr(app, 'capture_rate', 16000)
        parts = getattr(app, '_kept_wake_parts', [])
        if not getattr(app, 'wake_dictation_buffer', None):
            parts = []
        context = dict(app=app, parts=parts + [(buffer, rate)], generation=store_for(app).generation,
                       consumed=False)
        return _wake.set(context)
    except Exception:
        _warning()


def end_wake(app, token):
    if token is None:
        return
    try:
        context = _wake.get()
        app._kept_wake_parts = (context['parts'] if not context['consumed']
            and getattr(app, 'wake_dictation_buffer', None) else [])
        app._kept_wake_generation = context['generation']
    except Exception:
        _warning()
    finally:
        _wake.reset(token)


def note_wake_output(app, text):
    context = _wake.get()
    if context is not None and context['app'] is app:
        context['raw'] = text
    elif getattr(app, '_kept_wake_parts', None):
        app._kept_wake_raw = text


def wake_audio(method):
    @wraps(method)
    def observed(app, buffer, src_rate=None, **kwargs):
        # Context-local PCM cannot be replaced by another decoding thread.
        token = begin_wake(app, buffer, src_rate)
        try:
            return method(app, buffer, src_rate, **kwargs)
        finally:
            end_wake(app, token)
    return observed


def wake_output(method):
    @wraps(method)
    def observed(app, text):
        context = _wake.get()
        token = None
        if context is None and getattr(app, '_kept_wake_parts', None):
            context = dict(app=app, parts=app._kept_wake_parts,
                generation=getattr(app, '_kept_wake_generation', -1), consumed=False)
            token = _wake.set(context)
        if context is not None:
            context['raw'] = text
        try:
            return method(app, text)
        finally:
            if token is not None:
                app._kept_wake_parts = []
                _wake.reset(token)
    return observed


def wake_history(app, raw, final, mode, entry_type, status='success'):
    delayed = False
    try:
        context = _wake.get()
        if context is None and getattr(app, '_kept_wake_parts', None):
            delayed = True
            context = dict(app=app, parts=app._kept_wake_parts,
                generation=getattr(app, '_kept_wake_generation', -1),
                raw=getattr(app, '_kept_wake_raw', raw), consumed=False)
        if mode != 'wake' or entry_type != 'dictation' or context is None or context['app'] is not app:
            return
        from samsara.audio_tools import resample_audio
        audio = np.concatenate([resample_audio(np.concatenate(frames), rate, app.model_rate)
                                for frames, rate in context['parts']])
        capture(app, audio, context.get('raw', raw), final if status == 'success' else '', 'wake',
                status='sent' if status == 'success' else 'not typed', generation=context['generation'])
        context['consumed'] = True
    except Exception:
        _warning()
    finally:
        if delayed and context['consumed']:
            app._kept_wake_parts = []
            app.__dict__.pop('_kept_wake_raw', None)

# PLACEHOLDER COPY (1161): ordinary review labels and status messages chosen by the worker.
