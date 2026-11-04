"""Request-local edit sources. Never reconstruct a source after router latency."""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from uuid import uuid4

TTL_S = 120.0
TARGETS = frozenset({'auto', 'selection', 'draft', 'last_sentence', 'last_dictation'})


@dataclass(frozen=True, repr=False)
class SelectionCapture:
    # Unknown is a limitation, never evidence that the control has no selection.
    state: str = 'unsupported'
    text: str = ''
    target: tuple | None = None
    span: tuple[int, int] | None = None
    draft_range: tuple[int, int] | None = None


@dataclass(frozen=True, repr=False)
class BoundTarget:
    kind: str
    full_text: str
    start: int
    end: int
    manager: object = None
    document_id: str = ''
    revision: int = -1
    chunk: object = None
    chunk_identity: tuple = ()
    selection: SelectionCapture | None = None

    @property
    def text(self):
        return self.full_text[self.start:self.end]

    def replacement(self, rewritten):
        return self.full_text[:self.start] + rewritten + self.full_text[self.end:]


@dataclass(repr=False)
class RequestSnapshot:
    request_id: str
    created_at: float
    generation: int
    epoch: int
    manager: object
    mode: object
    provider_scope: object
    selection: SelectionCapture
    draft: BoundTarget | None
    latest: BoundTarget | None
    committed: BoundTarget | None
    clock: object = time.monotonic
    released: bool = False
    expiry_timer: object = None

    def release(self):
        self.released = True
        self.selection = SelectionCapture()
        self.draft = self.latest = self.committed = None
        timer, self.expiry_timer = self.expiry_timer, None
        if timer is not None:
            timer.cancel()


@dataclass(frozen=True, repr=False)
class EditRequest:
    target: str
    instruction: str
    snapshot: RequestSnapshot
    original_utterance: str = ''

    def __post_init__(self):
        if self.target not in TARGETS:
            raise ValueError('invalid edit target')
        if (not isinstance(self.instruction, str) or not self.instruction.strip()
                or len(self.instruction) > 2000 or '\0' in self.instruction):
            raise ValueError('invalid edit instruction')


def provider_scope(app):
    """Pure authorization/destination snapshot; no probe, keys or upload."""
    from copy import deepcopy
    from samsara import ai_preferences
    from samsara.ava_edit import session
    cfg = getattr(app, 'config', {}) or {}
    state = ai_preferences.runtime_state(cfg, 'editing')
    # Consent is retained in memory only, so even revoke/regrant changes scope.
    return (session.enabled(app), state, deepcopy(cfg.get('ava', {}).get('consent')),
            cfg.get('ava_edit', {}).get('model'), cfg.get('ollama', {}).get('model'),
            cfg.get('cloud_llm', {}).get('model'))


def _chunk_identity(item):
    extra = getattr(item, 'extra', {})
    return (getattr(item, 'timestamp', None), extra.get('hwnd'),
            extra.get('target_process'), extra.get('suppressed'), extra.get('expired'))


def snapshot(app, *, selection_needed=True, clock=time.monotonic):
    from samsara import execution_policy
    from samsara.ava_edit import selection
    manager = getattr(app, '_session_mode_manager', None)
    generation = execution_policy.capture_generation(app)
    epoch = getattr(app, '_ava_edit_epoch', 0)
    scope = provider_scope(app)
    created_at = clock()
    draft = latest = committed = None
    if manager is not None:
        # The manager lock serializes append/replace and the source versions.
        from contextlib import nullcontext
        with getattr(manager, '_dispatch_lock', nullcontext()):
            document = getattr(manager, 'draft_document', None)
            if document is not None and document.text:
                draft = BoundTarget('draft', document.text, 0, len(document.text),
                                    manager, document.document_id, document.revision)
                span = document.latest_dictation_range
                if span is not None:
                    latest = BoundTarget('draft', document.text, *span, manager,
                                         document.document_id, document.revision)
            item = manager.top_dictation_chunk()
            if item is not None and item.payload:
                identity = _chunk_identity(item)
                if identity[1] is not None and identity[2] and not any(identity[3:]):
                    committed = BoundTarget('committed', item.payload, 0, len(item.payload),
                                            manager, chunk=item, chunk_identity=identity)
    captured = selection.capture_result(app) if selection_needed else SelectionCapture()
    result = RequestSnapshot(uuid4().hex, created_at, generation,
        epoch, manager, getattr(manager, 'mode', None), scope,
        captured, draft, latest, committed, clock)
    # Text expires even when a queued router request is never consumed.
    from samsara.runtime import thread_registry
    result.expiry_timer = thread_registry.timer('ava-edit-source-expiry', TTL_S, result.release, daemon=True)
    return result


def request_valid(app, captured):
    from samsara import execution_policy
    return (not captured.released and captured.clock() - captured.created_at <= TTL_S
            and execution_policy.is_current(app, captured.generation)
            and getattr(app, '_ava_edit_epoch', 0) == captured.epoch
            and getattr(app, '_session_mode_manager', None) is captured.manager
            and getattr(captured.manager, 'mode', None) == captured.mode
            and provider_scope(app) == captured.provider_scope)


def source_valid(app, bound, *, selection_text=True):
    if bound.selection is not None:
        from samsara.ava_edit import selection
        if not selection._target_matches(bound.selection.target):
            return False
        if not selection_text and bound.kind == 'selection':
            return True
        current = selection.capture_result(app)
        if not (current.state == 'selected' and current.text == bound.text
                and current.target == bound.selection.target
                and current.span == bound.selection.span
                and current.draft_range == bound.selection.draft_range):
            return False
        if bound.kind == 'selection':
            return True
    manager = getattr(app, '_session_mode_manager', None)
    if manager is not bound.manager:
        return False
    if bound.kind == 'draft':
        document = getattr(manager, 'draft_document', None)
        return (document is not None and document.document_id == bound.document_id
                and document.revision == bound.revision and document.text == bound.full_text)
    item = manager.top_dictation_chunk()
    if not (item is bound.chunk and item.payload == bound.full_text
            and _chunk_identity(item) == bound.chunk_identity):
        return False
    from samsara.session_modes import check_focus_lock
    return (manager._foreground_hwnd_resolver() == bound.chunk_identity[1]
            and check_focus_lock(bound.chunk_identity[2], manager._foreground_exe_resolver()))


def last_sentence_span(text):
    """Conservative final-sentence span; uncertain abbreviations refuse."""
    end = len(text)
    if not text.strip():
        return None
    boundaries = list(re.finditer(r'[.!?]["\x27\u201d\u2019]*\s+(?=\S)', text.rstrip()))
    start = boundaries[-1].end() if boundaries else 0
    if boundaries:
        prefix = text[:boundaries[-1].start()]
        word = re.search(r'(\w+)$', prefix)
        if word and (word[1].lower() in {'dr', 'mr', 'mrs', 'ms', 'prof', 'etc', 'vs'}
                     or len(word[1]) == 1 or prefix.endswith('.')):
            return None
    return start, end


def bind(request):
    from dataclasses import replace
    captured, target = request.snapshot, request.target
    if captured.released:
        return None, 'source expired'
    selected = captured.selection
    if target in {'auto', 'selection'}:
        if selected.state == 'selected':
            if selected.draft_range is not None:
                if captured.draft is None:
                    return None, 'the text changed'
                start, end = selected.draft_range
                if not 0 <= start < end <= len(captured.draft.full_text):
                    return None, 'the text changed'
                if captured.draft.full_text[start:end] != selected.text:
                    return None, 'the text changed'
                return replace(captured.draft, start=start, end=end, selection=selected), ''
            return BoundTarget('selection', selected.text, 0, len(selected.text),
                               selection=selected), ''
        if target == 'selection':
            return None, 'Select some text first'
        if selected.state != 'verified_empty':
            return None, 'selection unknown'
    bound = captured.draft if target in {'auto', 'draft'} else None
    if target == 'last_dictation':
        # A draft with destroyed provenance must not become a committed chunk.
        bound = captured.latest if captured.draft is not None else captured.committed
    elif target == 'last_sentence':
        bound = captured.draft or captured.committed
        span = last_sentence_span(bound.full_text) if bound else None
        if span is None:
            return None, 'sentence range unknown'
        bound = replace(bound, start=span[0], end=span[1])
    elif target == 'auto':
        bound = bound or captured.committed
    return (bound, '') if bound is not None else (None, 'nothing to edit')
