"""Keep stopped streaming captures recoverable without changing decode gates (1042).

The explicit retry slot remains separate from opt-in incident diagnostics.
History uses its normal privacy policy; only accepted previews may become text.
"""
import logging

from samsara import decode_recovery

logger = logging.getLogger(__name__)
# PLACEHOLDER COPY (1042)
RETRY = 'Audio kept; say try again.'
EMPTY = 'Nothing heard. ' + RETRY
CANCELLED = 'Recording stopped. ' + RETRY


def _audio(session):
    # An explicit retry decodes the original take, not the new "try again" PCM.
    audio = getattr(session, '_decode_audio', None)
    return audio if audio is not None else getattr(session, '_stopped_audio_1042', None)


def retain_stopped(session, reason):
    if getattr(session, '_retry_retained_1042', False):
        return True
    audio = _audio(session)
    if audio is None or not getattr(audio, 'size', 0):
        return False
    try:
        params = getattr(session, '_decode_params', None)
        if params is None:
            params = session._worker._final_params()
        decode_recovery.retain(session.app, audio, params, reason)
        session._retry_retained_1042 = True
        return True
    except Exception:
        logger.exception('[STREAM] stopped capture could not be retained')
        return False


def capture_stopped(session, audio):
    session._stopped_audio_1042 = audio
    if session.cancel_event.is_set():
        cancelled(session)
    return audio


def _prepare_final(session, text, raw_text, duration_s):
    """Preserve a preview or explain the retained PCM before empty delivery."""
    # Tutorial captures already own their empty-result display and Home feedback.
    if getattr(session, '_tutorial_owner', None) is not None or str(text or '').strip():
        return text, raw_text, duration_s
    audio = _audio(session)
    if audio is not None and getattr(audio, 'size', 0):
        duration_s = len(audio) / session.app.model_rate
    retained = retain_stopped(session, 'streaming_empty')
    preview = getattr(session, '_last_partial', '')
    if isinstance(preview, str) and preview.strip():
        session._fallback_notice_rejected = True
        session._decode_failure = None
        session._quiet_empty_capture_899 = False
        return preview, preview, duration_s
    if retained:
        existing = getattr(session, '_decode_failure', None)
        session._decode_failure = ('Audio capture stopped. ' + RETRY
                                   if existing == decode_recovery.LOST else
                                   'Words unclear. ' + RETRY if isinstance(existing, str) and existing else EMPTY)
        session._quiet_empty_capture_899 = False
    else:
        # No PCM can be recovered; never claim it was kept.
        session._decode_failure = 'No audio was captured. Check the microphone.'
        session._quiet_empty_capture_899 = False
    return text, raw_text, duration_s


def cancelled(session):
    """Cancellation still prevents injection, while retaining work once."""
    retained = retain_stopped(session, 'streaming_cancelled')
    complete_incident(session, '')
    preview = getattr(session, '_last_partial', '')
    preview = preview.strip() if isinstance(preview, str) else ''
    if not retained and not preview:
        return
    audio = _audio(session)
    duration_s = len(audio) / getattr(session.app, 'model_rate', 16000) if retained else 0
    # PLACEHOLDER COPY (1042)
    message = 'Recording stopped. Preview saved in History.' if preview else CANCELLED
    if not getattr(session, '_recovery_history_1042', False):
        session._recovery_history_1042 = True
        try:
            session.app._log_history(
                raw_text=preview, display_text=preview or message,
                duration_ms=int(duration_s * 1000), mode='streaming', status='failed')
        except Exception:
            logger.exception('[STREAM] stopped capture History write failed')
            session._recovery_history_1042 = False
    if not getattr(session, '_recovery_notice_1042', False):
        session._recovery_notice_1042 = True
        from samsara.failure_reasons import post_reason
        post_reason(session.app, 'long_failure', headline=message)
    logger.info('[STREAM] stopped capture retained audio=%s preview_chars=%d', retained, len(preview))


# 1052a: the retry slot above remains available even when disk diagnostics are off.
import json
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timezone

import numpy as np

_incident_audit = ContextVar('incident_native_audit_1052a', default=None)


def incident_folder():
    from samsara.paths import samsara_home_dir
    return samsara_home_dir() / 'debug_holds'


def _enabled(app):
    return app.config.get('diagnostics', {}).get('keep_incident_holds', False) is True


class IncidentStore:
    """One serialized writer; disk work and PCM conversion never run on capture."""
    def __init__(self, folder, maximum=5):
        self.folder = folder
        try:
            self.maximum = max(1, min(5, int(maximum)))
        except (TypeError, ValueError):
            self.maximum = 5
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='incident-holds')
        self.previous = None
        self.lock = threading.RLock()
        self.generation = 0

    def submit(self, record):
        with self.lock:
            if record['generation'] != self.generation or record.get('submitted'):
                return
            record['submitted'] = True
            generation = record['generation']
            # Freeze metadata before a following hold can change its reasons.
            audio, metadata = record['audio'], json.loads(json.dumps(record['metadata']))
        return self.worker.submit(self._write, audio, metadata, generation)

    def _write(self, audio, metadata, generation):
        try:
            with self.lock:
                if generation != self.generation or not getattr(self, 'enabled', lambda: True)():
                    return
            self.folder.mkdir(parents=True, exist_ok=True)
            stem = 'hold_' + metadata['timestamp'].replace(':', '').replace('+', '_') + '_' + uuid.uuid4().hex[:8]
            wav = self.folder / (stem + '.wav')
            sidecar = wav.with_suffix('.json')
            try:
                pcm = np.rint(np.clip(audio, -1, 32767 / 32768) * 32768).astype('<i2')
                with wave.open(str(wav), 'wb') as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(16000)
                    output.writeframes(pcm.tobytes())
                sidecar.write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding='utf-8')
            except Exception:
                wav.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
                raise
            for old in sorted(self.folder.glob('hold_*.wav'), key=lambda p: p.name, reverse=True)[self.maximum:]:
                old.unlink(missing_ok=True)
                old.with_suffix('.json').unlink(missing_ok=True)
        except Exception:
            # Never log transcripts, audio or decoder context on failure.
            logger.warning('[INCIDENT] local archive write failed')

    def clear(self):
        with self.lock:
            self.generation += 1
            self.previous = None
        def remove():
            with self.lock:
                if self.folder.exists():
                    for path in self.folder.glob('hold_*'):
                        if path.suffix in ('.wav', '.json'):
                            path.unlink(missing_ok=True)
        return self.worker.submit(remove)


def _store(app):
    store = getattr(app, '_incident_store_1052a', None)
    if store is None:
        store = IncidentStore(incident_folder(), app.config.get('diagnostics', {}).get('incident_holds_max', 5))
        store.enabled = lambda: _enabled(app)
        app._incident_store_1052a = store
    return store


def begin_incident(session):
    """Called on hold start, before any final can finish out of order."""
    if not _enabled(session.app):
        return
    store = _store(session.app)
    now = time.monotonic()
    with store.lock:
        previous = store.previous
        if previous is not None and 0 <= now - previous['released'] <= 30:
            previous['metadata']['reasons'].append('followed_by_hold')
            store.submit(previous)
        store.previous = None
        first = not getattr(session.app, '_incident_first_1052a', False)
        session.app._incident_first_1052a = True
        session._incident_1052a = {'first': first, 'gate': [], 'signals': set(), 'started': now,
                                  'generation': store.generation,
                                  'timestamp': datetime.now(timezone.utc).isoformat(timespec='microseconds')}
        # A new hold may start before the previous final returns.
        prior = getattr(session.app, '_incident_session_1052a', None)
        if prior is not None and getattr(prior, '_incident_release_1052a', None) is not None:
            if 0 <= now - prior._incident_release_1052a <= 30:
                prior._incident_1052a['signals'].add('followed_by_hold')
        session.app._incident_session_1052a = session


class _IncidentLogs(logging.Handler):
    def __init__(self, session):
        super().__init__()
        self.session, self.thread = session, threading.get_ident()

    def emit(self, record):
        from samsara.hold_final_policy import current_audit
        audit = current_audit.get()
        own = getattr(self.session, '_hold_final_audit', None)
        if record.thread != self.thread and (audit is None or audit is not own):
            return
        data = self.session._incident_1052a
        # Only numeric GATE/HOLD_FINAL records may be persisted. Other log
        # messages can contain text: retain their category, never their body.
        template = str(record.msg)
        if template.startswith(('[GATE]', '[HOLD_FINAL]')) and len(data['gate']) < 64:
            data['gate'].append(record.getMessage())
        for prefix, signal in (('[GUARD]', 'guard_discard'), ('[QUALITY]', 'quality_warning'),
                               ('[HOLD_WINDOWS] unresolved', 'lost_range'),
                               ('[DECODE]', 'decode_recovery')):
            if template.startswith(prefix):
                data['signals'].add(signal)


def run_incident_final(worker):
    session = worker._session
    if not hasattr(session, '_incident_1052a'):
        return worker._final_pass()
    if not hasattr(session, '_incident_release_1052a'):
        incident_released(session)
    handler = _IncidentLogs(session)
    source = logging.getLogger('Samsara')
    source.addHandler(handler)
    try:
        return worker._final_pass()
    finally:
        source.removeHandler(handler)


def observe_incident_decode(model, args, params):
    """Observe effective native parameters, including internal CPU retries."""
    audit = _incident_audit.get()
    if audit is None:
        return model.transcribe(*args, **params)
    audio = args[0] if args else params['audio']
    attempt = {'sample_count': len(audio), 'params': {key: params.get(key) for key in (
        'temperature', 'condition_on_previous_text', 'without_timestamps', 'beam_size',
        'best_of', 'max_new_tokens', 'log_prob_threshold', 'compression_ratio_threshold',
        'no_speech_threshold')}, 'segments': []}
    if not hasattr(audit, 'incident_attempts'):
        audit.incident_attempts = []
    audit.incident_attempts.append(attempt)
    if not hasattr(audit, 'incident_audio'):
        audit.incident_audio = np.array(audio, dtype=np.float32, copy=True)
    segments, info = model.transcribe(*args, **params)
    def observed():
        for segment in segments:
            values = {}
            for key in ('start', 'end', 'avg_logprob', 'compression_ratio', 'no_speech_prob', 'temperature'):
                value = getattr(segment, key, None)
                values[key] = float(value) if isinstance(value, (int, float)) and np.isfinite(value) else None
            attempt['segments'].append(values)
            yield segment
    return observed(), info


def incident_model(app):
    """Observe the native model boundary after silence trimming, before gates."""
    from samsara.hold_final_policy import current_audit
    audit = current_audit.get()
    if audit is None or not _enabled(app):
        return app.model

    class ObservedModel:
        def __getattr__(self, name):
            return getattr(app.model, name)

        def transcribe(self, audio, **params):
            from samsara.gpu_fallback import ResilientSpeechModel
            token = _incident_audit.set(audit)
            try:
                if isinstance(app.model, ResilientSpeechModel):
                    return app.model.transcribe(audio, **params)
                return observe_incident_decode(app.model, (audio,), params)
            finally:
                _incident_audit.reset(token)
    return ObservedModel()


def complete_incident(session, text):
    data = getattr(session, '_incident_1052a', None)
    if data is None or data.get('complete') or not _enabled(session.app):
        return
    data['complete'] = True
    if data['generation'] != _store(session.app).generation:
        return
    try:
        audit = getattr(session, '_hold_final_audit', None)
        audio = getattr(audit, 'incident_audio', None)
        if audio is None:
            audio = _audio(session)
        if audio is None or not len(audio):
            return
        if getattr(session.app, 'model_rate', 16000) != 16000:
            return  # Never label another sample rate as 16 kHz.
        reasons = set(data['signals'])
        if data['first']:
            reasons.add('first_hold')
        if getattr(session, '_retry_retained_1042', False) or getattr(session, '_decode_failure', None):
            reasons.add('stopped_recovery')
        dropped = dict(getattr(audit, 'dropped', {}))
        if dropped:
            reasons.add('dropped_segment')
        attempts = getattr(audit, 'incident_attempts', [])
        if len(attempts) > 1 or any((seg.get('temperature') or 0) > 0 for a in attempts for seg in a['segments']):
            reasons.add('decode_retry')
        retained = getattr(session.app, '_hold_retry_audio', None)
        if retained is not None and getattr(retained, 'reason', '') in (
                'unknown_timestamps', 'window_decode_failed', 'untranscribed_window'):
            if np.array_equal(audio, retained.audio):
                reasons.add('lost_range')
        metadata = {'timestamp': data['timestamp'], 'sample_rate': 16000,
                    'sample_count': len(audio), 'reasons': sorted(reasons), 'delivered_text': text or '',
                    'gate_metrics': list(data['gate']), 'decode_attempts': attempts,
                    'hold_final': {'segments_decoded': getattr(audit, 'decoded', 0),
                                   'segments_dropped': sum(dropped.values()), 'reasons': dropped,
                                   'preview_tail_words': getattr(audit, 'preview_tail_words', 0),
                                   'chars_delivered': len(text or '')}}
        record = {'generation': data['generation'],
                  'audio': np.array(audio, dtype=np.float32, copy=True), 'metadata': metadata,
                  'released': getattr(session, '_incident_release_1052a', time.monotonic())}
        store = _store(session.app)
        with store.lock:
            # Clear invalidates the originating hold even during PCM preparation.
            if record['generation'] != store.generation:
                return
            if reasons:
                store.submit(record)
            store.previous = record
        def expire():
            with store.lock:
                if store.previous is record:
                    store.previous = None
        previous_timer = getattr(store, 'expiry', None)
        if previous_timer is not None:
            previous_timer.cancel()
        from samsara.runtime import thread_registry
        store.expiry = thread_registry.timer('hold-incident-expiry', 30, expire, daemon=True)
        # Do not retain a finished session through the app for 30 seconds.
        if getattr(session.app, '_incident_session_1052a', None) is session:
            session.app._incident_session_1052a = None
    except Exception:
        logger.warning('[INCIDENT] capture could not be archived')


def prepare_final(session, text, raw_text, duration_s):
    # Preserve master's final-audit/lost-range policy while always archiving.
    result = ((text, raw_text, duration_s) if getattr(session, '_hold_final_audit', None)
              or getattr(session, '_hold_failed_ranges', ())
              else _prepare_final(session, text, raw_text, duration_s))
    complete_incident(session, result[0])
    return result


def build_incident_controls(app):
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy
    host = QWidget()
    host.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    column = QVBoxLayout(host)
    column.setContentsMargins(0, 0, 0, 0)
    label = QLabel()
    label.setWordWrap(True)
    column.addWidget(label)
    row = QHBoxLayout()
    column.addLayout(row)
    # PLACEHOLDER COPY (1052a)
    open_button, clear_button = QPushButton('Open folder'), QPushButton('Clear')
    for button in (open_button, clear_button):
        button.setMinimumHeight(32)
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        row.addWidget(button)
    row.addStretch()
    def refresh():
        folder = incident_folder()
        count = len(list(folder.glob('hold_*.wav'))) if folder.exists() else 0
        label.setText(f'Incident holds: {count} kept')
    def open_folder():
        folder = incident_folder()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
    open_button.clicked.connect(open_folder)
    def clear():
        _store(app).clear()
    clear_button.clicked.connect(clear)
    timer = QTimer(host)
    timer.timeout.connect(refresh)
    timer.start(1000)
    refresh()
    return host


def incident_released(session):
    if hasattr(session, '_incident_1052a'):
        session._incident_release_1052a = time.monotonic()
