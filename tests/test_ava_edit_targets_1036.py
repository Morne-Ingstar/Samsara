"""E1-E4/E6: synthetic sources, providers and transports; no app/network."""
from dataclasses import replace
from types import SimpleNamespace
import ast
from pathlib import Path

import pytest

from samsara import ai_preferences, clipboard, execution_policy, teach_patterns
from samsara.ava_edit import actions, intent, model, selection, session, targets
from samsara.draft_document import DraftDocument
from tests.test_ava_edit_105 import _build

SOURCE = 'The tool. Helps me every day.'
REWRITE = 'The tool helps me every day.'
TARGET = (101, 102, 103)


@pytest.fixture
def rig(monkeypatch):
    app, manager, window = _build(chunk=SOURCE)
    state = SimpleNamespace(app=app, manager=manager, window=window, calls=[],
        selected=targets.SelectionCapture('selected', SOURCE, TARGET, (10, 38)),
        mutations=[], timers=[], now=0., real_rewrite=model.rewrite)
    monkeypatch.setattr(selection, 'capture_result', lambda *_: state.selected)
    monkeypatch.setattr(clipboard, '_paste_target', lambda: state.selected.target)
    monkeypatch.setattr(teach_patterns, 'grab_selection_block', lambda: state.selected.text)
    monkeypatch.setattr(session.thread_registry, 'spawn', lambda _name, fn, **_: fn())
    monkeypatch.setattr(session.thread_registry, 'timer',
                        lambda _name, _delay, fn, **_: state.timers.append(fn))

    def rewrite(text, instruction, _app, **_):
        state.calls.append((text, instruction))
        return (REWRITE if text == SOURCE else text[:1].upper() + text[1:]), None

    def paste(text, before_paste):
        if not before_paste():
            return False
        state.mutations.append(text)
        return True

    monkeypatch.setattr(model, 'rewrite', rewrite)
    monkeypatch.setattr(clipboard, 'paste_with_preservation', paste)
    session.clear()
    yield state
    session.clear()


def typed(rig, target='auto', instruction='clean up', *, snapshot=None, utterance='fix this'):
    return actions.from_action(rig.app, {'target': target, 'instruction': instruction},
        snapshot=snapshot or targets.snapshot(rig.app, clock=lambda: rig.now),
        original_utterance=utterance)


def test_equivalent_doors_bind_selection_ahead_of_unrelated_draft(rig):
    rig.manager.draft_document.append('Unrelated pending draft.')
    assert session.handle_utterance(rig.app, 'fix this')
    terse = session.pending()
    assert terse.bound_target.kind == 'selection'
    assert not rig.mutations and rig.window.calls == []
    original = terse.proposal
    session.discard(rig.app)
    assert actions.propose_and_stage(rig.app, typed(rig))
    conversational = session.pending()
    assert conversational.proposal.rewritten == original.rewritten == REWRITE
    assert conversational.proposal.original == original.original == SOURCE
    assert conversational.bound_target.selection.span == (10, 38)
    assert rig.calls[0] == rig.calls[1]
    assert not rig.mutations
    assert session.apply(rig.app)['applied']
    assert rig.mutations == [REWRITE]
    assert rig.manager.draft_document.text == 'Unrelated pending draft.'


@pytest.mark.parametrize('mode', ['DICTATE', 'COMMAND', 'AVA'])
@pytest.mark.parametrize('phrase', ['clean up', 'clean this up'])
def test_bare_cleanup_keeps_dictate_draft_scope_and_command_selection(rig, monkeypatch, mode, phrase):
    from samsara.session_modes import SessionMode
    rig.manager.mode = SessionMode[mode]
    draft = 'The draft. Helps me every day.'
    rig.manager.draft_document.append(draft)
    monkeypatch.setattr(model, 'rewrite',
        lambda text, *_args, **_kwargs: (text.replace('. Helps', ' helps'), None))
    assert session.handle_utterance(rig.app, phrase)
    staged = session.pending()
    assert staged.bound_target.kind == ('draft' if mode == 'DICTATE' else 'selection')
    assert staged.proposal.original == (draft if mode == 'DICTATE' else SOURCE)
    assert rig.manager.draft_document.text == draft
    assert not rig.mutations and not rig.window.calls


@pytest.mark.parametrize('state', ['verified_empty', 'unsupported', 'failed'])
@pytest.mark.parametrize('target', ['auto', 'selection'])
def test_empty_and_unknown_never_silently_retarget_explicit_selection(rig, state, target):
    rig.selected = targets.SelectionCapture(state)
    rig.manager.draft_document.append(SOURCE)
    request = typed(rig, target)
    result = actions.propose_and_stage(rig.app, request)
    assert result is (target == 'auto' and state == 'verified_empty')
    assert not rig.mutations and not rig.window.calls
    if not result:
        assert rig.calls == [] and session.pending() is None
    else:
        assert session.pending().bound_target.kind == 'draft'


def test_explicit_draft_ignores_selection_and_no_draft_has_no_fallback(rig):
    request = typed(rig, 'draft', utterance='fix my draft')
    assert not actions.propose_and_stage(rig.app, request)
    assert rig.calls == []  # Committed chunk/selection both exist, still no draft.
    rig.manager.draft_document.append(SOURCE)
    assert actions.propose_and_stage(rig.app, typed(rig, 'draft', utterance='fix my draft'))
    assert session.pending().bound_target.kind == 'draft'
    assert session.apply(rig.app)['applied']
    assert rig.manager.draft_document.text == REWRITE
    assert not rig.mutations and not rig.window.calls


@pytest.mark.parametrize('door', ['terse', 'typed'])
def test_no_source_is_identical_and_never_calls_model(rig, door):
    rig.selected = targets.SelectionCapture('verified_empty')
    rig.manager._stack.pop()
    if door == 'terse':
        assert session.handle_utterance(rig.app, 'fix this')
    else:
        assert not actions.propose_and_stage(rig.app, typed(rig))
    assert rig.app.spoken[-1] == 'nothing to edit'
    assert not rig.calls and not rig.mutations


@pytest.mark.parametrize('target', ['last_sentence', 'last_dictation'])
def test_draft_subrange_preserves_prefix_and_suffix_exactly(rig, target):
    prefix = 'First thought.  '
    rig.manager.draft_document.append(prefix)
    rig.manager.draft_document.append('last thought is ready.')
    request = typed(rig, target, 'Capitalise the first letter.', utterance='edit this')
    assert actions.propose_and_stage(rig.app, request)
    bound = session.pending().bound_target
    assert bound.text == 'last thought is ready.' and bound.start == len(prefix)
    assert rig.calls[-1][0] != request.original_utterance
    assert rig.manager.draft_document.text == prefix + 'last thought is ready.'
    assert session.apply(rig.app)['applied']
    assert rig.manager.draft_document.text == prefix + 'Last thought is ready.'
    assert not rig.window.calls


def test_draft_selection_binds_exact_interior_range_without_os_paste(rig):
    prefix, suffix = 'Before. ', ' After.'
    rig.manager.draft_document.append(prefix + SOURCE + suffix)
    rig.selected = targets.SelectionCapture('selected', SOURCE, TARGET,
                                           draft_range=(len(prefix), len(prefix) + len(SOURCE)))
    assert actions.propose_and_stage(rig.app, typed(rig, 'selection'))
    assert session.apply(rig.app)['applied']
    assert rig.manager.draft_document.text == prefix + REWRITE + suffix
    assert not rig.mutations and not rig.window.calls


def test_collapsed_latest_provenance_refuses_instead_of_whole_draft(rig):
    document = rig.manager.draft_document
    document.append('First. ')
    document.append(SOURCE)
    document.replace_text(document.text)
    assert document.latest_dictation_range is None
    assert not actions.propose_and_stage(rig.app, typed(rig, 'last_dictation'))
    assert not rig.calls
    document.append('new latest segment.')
    start, end = document.latest_dictation_range
    assert document.text[start:end] == 'new latest segment.'


def test_committed_last_sentence_uses_whole_chunk_transport_and_undo(rig):
    prefix = 'First thought.  '
    text = prefix + 'last thought is ready.'
    rig.manager.top_dictation_chunk().payload = text
    rig.window.text = text
    assert actions.propose_and_stage(rig.app, typed(rig, 'last_sentence', 'Capitalise first letter.'))
    assert not rig.window.calls
    assert session.apply(rig.app)['applied']
    assert rig.window.text == prefix + 'Last thought is ready.'
    assert rig.window.calls[0] == ('remove', len(text))
    assert rig.manager._do_scratch_that()
    assert rig.window.text == text


@pytest.mark.parametrize('phase', ['routing', 'rewrite', 'apply'])
@pytest.mark.parametrize('change', ['generation', 'consent', 'provider', 'draft_revision', 'session'])
def test_stale_scope_refuses_at_every_boundary(rig, monkeypatch, phase, change):
    rig.manager.draft_document.append(SOURCE)
    request = typed(rig, 'draft')

    def mutate():
        if change == 'generation':
            execution_policy.bump_generation(rig.app, 'synthetic')
        elif change == 'consent':
            rig.app.config['ava']['consent'] = {}
        elif change == 'provider':
            rig.app.config['ollama']['model'] = 'different-model'
        elif change == 'draft_revision':
            rig.manager.draft_document.replace_text(SOURCE)
        else:
            rig.app._session_mode_manager = None

    if phase == 'routing':
        mutate()
        assert not actions.propose_and_stage(rig.app, request)
        assert not rig.calls
    elif phase == 'rewrite':
        def rewrite(*_, **_kwargs):
            mutate()
            return REWRITE, None
        monkeypatch.setattr(model, 'rewrite', rewrite)
        assert not actions.propose_and_stage(rig.app, request)
        assert session.pending() is None
    else:
        assert actions.propose_and_stage(rig.app, request)
        mutate()
        assert not session.apply(rig.app)['applied']
    assert not rig.mutations and not rig.window.calls
    assert rig.manager.draft_document.text == SOURCE


@pytest.mark.parametrize('change', ['range', 'control', 'text'])
def test_same_text_at_other_range_or_control_cannot_apply(rig, change):
    assert actions.propose_and_stage(rig.app, typed(rig))
    staged = session.pending()
    rig.selected = replace(rig.selected, **{
        'range': {'span': (50, 78)}, 'control': {'target': (101, 102, 999)},
        'text': {'text': 'Other text.'}}[change])
    assert not session.apply(rig.app)['applied']
    assert not rig.mutations and session.pending() is staged


@pytest.mark.parametrize('stop', ['discard', 'clear', 'expiry'])
def test_late_worker_cannot_populate_slot(rig, monkeypatch, stop):
    request = typed(rig)
    def rewrite(*_, **_kwargs):
        if stop == 'discard':
            session.discard(rig.app)
        elif stop == 'clear':
            session.clear()
        else:
            rig.now = 121.
        return REWRITE, None
    monkeypatch.setattr(model, 'rewrite', rewrite)
    assert not actions.propose_and_stage(rig.app, request)
    assert session.pending() is None and not rig.mutations


def test_expiry_releases_unused_router_source_and_cannot_recapture(rig):
    payload = actions.ingress_payload(rig.app, 'Could you fix this?', SOURCE)
    assert SOURCE not in payload and payload.snapshot.selection.text == SOURCE
    assert SOURCE not in repr(payload.snapshot)
    rig.timers[-1]()
    assert payload.snapshot.released and payload.snapshot.selection.text == ''
    rig.selected = replace(rig.selected, text='New focus text.')
    request = typed(rig, snapshot=payload.snapshot)
    assert not actions.propose_and_stage(rig.app, request)
    assert not rig.calls


def test_specific_instruction_is_preserved_and_has_no_cleanup_fallback(rig, monkeypatch):
    instruction = 'Fix punctuation only; preserve the wording.'
    monkeypatch.setattr(model, 'rewrite', lambda text, command, app, **_:
        (rig.calls.append((text, command)) or None, 'no model available'))
    assert not actions.propose_and_stage(rig.app, typed(rig, instruction=instruction))
    assert rig.calls == [(SOURCE, instruction)]
    assert session.pending() is None
    assert intent.cleanup_instruction('Make this warmer.')[0] is False


def test_oversized_source_is_refused_without_truncation_even_offline(rig):
    rig.selected = replace(rig.selected, text='x' * 4001)
    assert not actions.propose_and_stage(rig.app, typed(rig))
    assert not rig.calls and not rig.mutations


@pytest.mark.parametrize('target,utterance', [('auto', 'fix my draft'), ('draft', 'fix the selection'),
                                            ('selection', 'edit the last sentence')])
def test_model_cannot_override_explicit_user_target(rig, target, utterance):
    with pytest.raises(ValueError, match='contradictory'):
        typed(rig, target, utterance=utterance)


@pytest.mark.parametrize('args', [{'target': 'apply', 'instruction': 'fix'},
    {'target': 'auto', 'instruction': ''}, {'target': 'auto', 'instruction': 'x\0y'},
    {'target': 'auto', 'instruction': 'x' * 2001},
    {'target': 'auto', 'instruction': 'fix', 'source': SOURCE}])
def test_action_cannot_supply_authority_or_invalid_arguments(rig, args):
    with pytest.raises(ValueError):
        actions.from_action(rig.app, args, snapshot=targets.snapshot(rig.app), original_utterance='fix this')
    assert not rig.mutations and not rig.calls


def test_copy_timeout_result_is_unknown_and_clipboard_exception_is_content_free(monkeypatch):
    monkeypatch.setattr(teach_patterns, 'grab_selection_block', lambda *_: None)
    assert teach_patterns.grab_selection_block_result().state == 'failed'
    def fail(*_):
        raise RuntimeError(SOURCE)
    monkeypatch.setattr(teach_patterns, 'grab_selection_block', fail)
    result = teach_patterns.grab_selection_block_result()
    assert result.state == 'failed' and SOURCE not in repr(result)


@pytest.mark.parametrize('text', ['Dr. Smith is ready.', 'A. Smith is ready.'])
def test_ambiguous_last_sentence_refuses(text):
    assert targets.last_sentence_span(text) is None


@pytest.mark.parametrize('text', ['Could you fix the punctuation in the selection?',
    'Explain the phrase fix the selection.', 'make me a coffee', 'I said fix the selection later',
    'clean this up later', 'tidy that up later', 'punctuate this later', '"fix that please"'])
def test_matcher_placement_stays_narrow(text):
    assert not intent.is_edit_request(text)


def test_ingress_seam_is_net_zero_and_request_owned():
    # No dictation import/bootstrap: inspect the checked-in seam only.
    source = (Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DictationApp')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and node.name == '_ava_session_agent_dispatch_fn')
    calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'ingress_payload']
    assert len(calls) == 1


def test_cloud_consent_change_discards_late_rewrite(rig, monkeypatch):
    # 1036b: boolean presence only; no credential or network is involved.
    cfg = {'cloud_llm': {'provider': 'openai', 'model': 'fixture-model', 'api_key': True}}
    provider = ai_preferences.provider_for(cfg, 'cloud')
    consent = ai_preferences.accept_consent(cfg, features=('editing',), provider=provider,
        cloud=True, accepted_at='2026-10-17T00:00:00Z')
    rig.app.config = ai_preferences.apply_preferences(cfg, policy='cloud', ava=False,
        editing=True, consent=consent)
    request = typed(rig)
    calls = []
    def cloud(app, message, timeout):
        assert SOURCE in message and ai_preferences.runtime_state(app.config, 'editing').allowed
        calls.append('cloud')
        app.config['ava']['consent'] = {}
        return REWRITE, None
    monkeypatch.setattr(model, 'rewrite', rig.real_rewrite)
    monkeypatch.setattr(model, '_call_cloud', cloud)
    assert not actions.propose_and_stage(rig.app, request)
    assert calls == ['cloud']
    assert session.pending() is None and not rig.mutations


@pytest.mark.parametrize('phase', ['routing', 'rewrite'])
def test_selection_identity_cannot_move_during_router_or_rewrite(rig, monkeypatch, phase):
    request = typed(rig)
    def move():
        rig.selected = replace(rig.selected, span=(100, 128))
    if phase == 'routing':
        move()
    else:
        def rewrite(*_, **_kwargs):
            move()
            return REWRITE, None
        monkeypatch.setattr(model, 'rewrite', rewrite)
    assert not actions.propose_and_stage(rig.app, request)
    assert session.pending() is None and not rig.mutations


@pytest.mark.parametrize('phase', ['routing', 'rewrite', 'apply'])
@pytest.mark.parametrize('change', ['range', 'control'])
def test_draft_selection_identity_is_preserved(rig, monkeypatch, phase, change):
    rig.manager.draft_document.append(SOURCE + ' ' + SOURCE)
    rig.selected = targets.SelectionCapture('selected', SOURCE, TARGET,
                                           draft_range=(0, len(SOURCE)))
    request = typed(rig, 'selection')
    def move():
        rig.selected = replace(rig.selected, **({'draft_range': (len(SOURCE) + 1,
            2 * len(SOURCE) + 1)} if change == 'range' else {'target': (101, 102, 999)}))
    if phase == 'routing':
        move()
        assert not actions.propose_and_stage(rig.app, request)
        assert not rig.calls
    elif phase == 'rewrite':
        def rewrite(*_, **_kwargs):
            move()
            return REWRITE, None
        monkeypatch.setattr(model, 'rewrite', rewrite)
        assert not actions.propose_and_stage(rig.app, request)
    else:
        assert actions.propose_and_stage(rig.app, request)
        move()
        assert not session.apply(rig.app)['applied']
    assert rig.manager.draft_document.text == SOURCE + ' ' + SOURCE
    assert not rig.mutations and not rig.window.calls


@pytest.mark.parametrize('change', ['consent', 'provider', 'generation', 'source'])
def test_backend_resolution_cannot_upload_an_invalidated_source(rig, monkeypatch, change):
    request = typed(rig)
    calls = []
    def resolve(app):
        if change == 'consent':
            app.config['ava']['consent'] = {}
        elif change == 'provider':
            app.config['ollama']['model'] = 'changed-model'
        elif change == 'generation':
            execution_policy.bump_generation(app, 'synthetic')
        else:
            rig.selected = replace(rig.selected, span=(100, 128))
        return 'ollama'
    monkeypatch.setattr(model, 'rewrite', rig.real_rewrite)
    monkeypatch.setattr(model, 'resolve_backend', resolve)
    monkeypatch.setattr(model, '_call_ollama', lambda *_: calls.append('upload') or (REWRITE, None))
    assert not actions.propose_and_stage(rig.app, request)
    assert calls == [] and session.pending() is None
    assert not rig.mutations and not rig.window.calls


def test_queued_deterministic_worker_uses_ingress_source(rig, monkeypatch):
    workers = []
    monkeypatch.setattr(session.thread_registry, 'spawn', lambda name, fn, **_: workers.append(fn))
    assert session.handle_utterance(rig.app, 'fix this')
    rig.selected = replace(rig.selected, text='Another paragraph.')
    workers[0]()
    assert not rig.calls and session.pending() is None


def test_pending_edit_is_never_superseded_by_adapter(rig):
    assert actions.propose_and_stage(rig.app, typed(rig))
    staged = session.pending()
    assert not actions.propose_and_stage(rig.app, typed(rig))
    assert session.pending() is staged and len(rig.calls) == 1


@pytest.mark.parametrize('kind', ['password', 'readonly', 'elevated', 'terminal', 'selected', 'empty', 'race', 'failure'])
def test_native_adapter_protected_unknown_and_range_outcomes(rig, monkeypatch, kind):
    import ctypes
    from samsara import injection_safety
    sent = []
    text = 'A ' + chr(0x1F642) + ' word.'
    raw = text.encode('utf-16-le') + b'\0\0'
    reads = []

    def name(hwnd, buffer, length):
        buffer.value = 'ConsoleWindowClass' if kind == 'terminal' else 'Edit'
        return len(buffer.value)

    def send(hwnd, message, wparam, lparam, flags, timeout, result):
        sent.append(message)
        if kind == 'failure':
            return 0
        if message == 0xB0:
            reads.append(message)
            start, end = (2, 2) if kind == 'empty' else (2, 4)
            if kind == 'race' and len(reads) > 1:
                start, end = 5, 7
            ctypes.cast(wparam, ctypes.POINTER(ctypes.c_ulong))[0] = start
            ctypes.cast(lparam, ctypes.POINTER(ctypes.c_ulong))[0] = end
            result._obj.value = 0
        elif message == 0xE:
            result._obj.value = len(raw) // 2 - 1
        else:
            ctypes.memmove(lparam, raw, len(raw))
            result._obj.value = len(raw) // 2 - 1
        return 1

    api = SimpleNamespace(GetClassNameW=name, SendMessageTimeoutW=send,
        GetWindowLongW=lambda *_: 0x20 if kind == 'password' else 0x800 if kind == 'readonly' else 0)
    monkeypatch.setattr(clipboard, '_user32', api)
    monkeypatch.setattr(injection_safety, 'window_integrity',
                        lambda _: SimpleNamespace(confirmed_ok=kind != 'elevated'))
    monkeypatch.setattr(selection, '_draft_selection', lambda *_: None)
    # rig replaces the high-level adapter; call the production adapter directly.
    result = selection._native_selection(TARGET) if kind != 'failure' else None
    if kind == 'failure':
        with pytest.raises(RuntimeError):
            selection._native_selection(TARGET)
    elif kind in {'password', 'readonly', 'elevated', 'terminal'}:
        assert result.state == 'unsupported' and not sent
    elif kind == 'selected':
        assert result.state == 'selected' and result.text == chr(0x1F642) and result.span == (2, 4)
    elif kind == 'empty':
        assert result.state == 'verified_empty'
    else:
        assert result.state == 'failed'
