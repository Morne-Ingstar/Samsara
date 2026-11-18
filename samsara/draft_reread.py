"""1251: RAM-only draft audio and an explicit, reviewable hold decode."""
from __future__ import annotations

import threading
import weakref
import numpy as np

MAX_AUDIO_BYTES = 16_000 * 4 * 120  # Total retained original + speech PCM budget.
_busy = threading.Lock()


def notice(app, message):
    # PLACEHOLDER COPY (1251)
    show = getattr(app, '_show_outcome_chip', None)
    if callable(show):
        show(message, 'error', 8000)


def speech_pcm(audio, rate):
    """Use the bundled VAD's normal speech extraction, without disk I/O."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    pcm = np.asarray(audio, dtype=np.float32)
    if (rate != 16000 or pcm.ndim != 1 or not len(pcm)
            or pcm.nbytes > MAX_AUDIO_BYTES or not np.isfinite(pcm).all()):
        return None
    spans = get_speech_timestamps(pcm, VadOptions(), sampling_rate=rate)
    if not spans:
        return None
    return np.concatenate([pcm[s['start']:s['end']] for s in spans])


def retain(manager, audio):
    """One entry per accepted chunk; None preserves alignment after eviction."""
    app = getattr(manager._agent_dispatch_fn, '__self__', None)
    refs = manager._dictate_pending_audio
    pool = getattr(manager, '_reread_pcm', None)
    if pool is None:
        pool = manager._reread_pcm = {}
    try:
        pcm = speech_pcm(audio, getattr(app, 'model_rate', 16000)) if audio is not None else None
    except Exception:
        pcm = None  # Incomplete evidence must never become a partial rewrite.
    # Preserve the exact existing automatic commit input and its model identity.
    refs.append(audio)
    if pcm is not None:
        key = id(audio)
        try:
            pool[key] = (weakref.ref(audio, lambda _: pool.pop(key, None)), pcm)
        except TypeError:
            pass
    total = sum(getattr(ref, 'nbytes', 0) for ref in refs if ref is not None)
    total += sum(entry[1].nbytes for entry in pool.values())
    dropped = False
    for index, ref in enumerate(refs):
        if total <= MAX_AUDIO_BYTES:
            break
        if ref is not None:
            total -= getattr(ref, 'nbytes', 0)
            entry = pool.pop(id(ref), None)
            if entry is not None:
                total -= entry[1].nbytes
            refs[index] = None
            dropped = True
    # Recovery/undo may still own older originals. Bound the new speech pool too.
    while total > MAX_AUDIO_BYTES and pool:
        entry = pool.pop(next(iter(pool)))
        total -= entry[1].nbytes
        dropped = True
    if dropped:
        notice(app, 'Older draft audio was released; Re-read needs the complete recording.')
    models = {key: value for key, value in getattr(manager, '_dictate_audio_models', {}).items()
              if value[0]() is not None}
    owner = getattr(manager._commit_redecode_fn, '__self__', None)
    try:
        models[id(audio)] = (weakref.ref(audio), weakref.ref(owner.model))
    except (AttributeError, TypeError):
        pass  # Missing provenance keeps the optional re-decode path.
    manager._dictate_audio_models = models


def checkpoint(manager):
    doc = manager.draft_document
    manager._reread_source = (doc.document_id, doc.revision, doc.text)


def request(app):
    """Snapshot under the dispatch lock, then decode off the UI/voice thread."""
    manager = getattr(app, '_session_mode_manager', None)
    if manager is None:
        notice(app, 'No draft to re-read.')
        return False
    from samsara.ava_edit import session
    from samsara.session_modes import SessionMode
    with manager._dispatch_lock:
        document = manager.draft_document
        if (manager.mode is not SessionMode.DICTATE or not document.text
                or not manager._buffer_dictate_until_commit):
            notice(app, 'No hands-free draft to re-read.')
            return False
        if document.edited or manager._parked_hold:
            notice(app, 'Re-read is unavailable because this draft has been edited or restored.')
            return False
        refs = manager._dictate_pending_audio
        pool = getattr(manager, '_reread_pcm', {})
        if (not refs or any(ref is None for ref in refs)
                or any(id(ref) not in pool or pool[id(ref)][0]() is not ref for ref in refs)
                or getattr(manager, '_reread_source', None) != (
                    document.document_id, document.revision, document.text)):
            notice(app, 'The complete audio for this draft is no longer available.')
            return False
        if session.pending() is not None or not _busy.acquire(blocking=False):
            notice(app, 'Finish the pending change before re-reading.')
            return False
        target = (manager, document.document_id, document.revision, document.text, 0)
        audio = np.concatenate([pool[id(ref)][1] for ref in refs])
    from samsara.runtime import thread_registry
    try:
        thread_registry.spawn('draft-reread', lambda: _decode(app, target, audio), daemon=True)
    except Exception:
        _busy.release()
        notice(app, 'Re-read could not start; your draft is unchanged.')
        return False
    return True


def _decode(app, target, audio):
    from samsara.ava_edit import session, draft_review
    from samsara.ava_edit.proposal import Proposal
    from samsara.command_clip_trim import build_params
    from samsara.decode_recovery import decode_hold
    manager, document_id, revision, original, _ = target
    try:
        # Existing free-form hold parameters and decoder, unchanged.
        params = build_params(app, is_command=False)
        result = decode_hold(app, audio, params, len(audio) / app.model_rate)
        text = result.text
        if result.language_rejected or not text:
            notice(app, 'Re-read did not produce a replacement; your draft is unchanged.')
            return
        prepare = manager._prepare_dictate_fn
        if prepare is not None:
            text = prepare(text)
        if not text or not text.strip():
            notice(app, 'Re-read did not produce a replacement; your draft is unchanged.')
            return
        with manager._dispatch_lock:
            doc = manager.draft_document
            if (getattr(app, '_session_mode_manager', None) is not manager
                    or doc.document_id != document_id or doc.revision != revision
                    or doc.text != original or doc.edited):
                notice(app, 'The draft changed during Re-read; your edits were kept.')
                return
            proposal = Proposal.build(original, text, instruction='Re-read')
            if not proposal.ok:
                notice(app, 'Re-read found no change to offer.')
                return
            if session._stage_owned(app, proposal, draft_target=target):
                draft_review.show(app)
            else:
                notice(app, 'Finish the pending change before re-reading.')
    except Exception:
        notice(app, 'Re-read failed; your draft is unchanged.')
    finally:
        _busy.release()


def install_compact(widget, callback):
    """Extend only the existing session-preview action row."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
    preview = getattr(callback, '__self__', None)
    app = getattr(preview, 'app', None)
    if app is None:
        return
    # PLACEHOLDER COPY (1251)
    button = QPushButton('Re-read', widget._controls)
    button.setAccessibleName('Re-read draft audio')
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setFixedWidth(button.sizeHint().width() + 16)
    button.clicked.connect(lambda: request(app))
    widget._controls.layout().insertWidget(1, button)
    widget._reread_btn = button


def configure_compact(widget, callback):
    widget.enable_cleanup(callback)
    install_compact(widget._w, callback)
