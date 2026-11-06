"""1137: missing cross-door/feedback checks; synthetic effects and sources only."""
import json
from types import SimpleNamespace, MethodType
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava
from samsara import ava_capability_protocol as wire, ava_pending, session_feedback
from samsara.ava_edit import actions, intent, session
from samsara.session_modes import SessionMode, UtteranceSignals
from samsara.ui import quick_reference_qt as qr
from tests.test_ava_plan_and_ask_624 import rig
from tests.test_ava_capabilities_1036 import make_executor
from tests.test_ava_capability_protocol_1036 import runtime, context, deliver, offered
from tests.test_ava_edit_targets_1036 import rig as edit_rig


@pytest.mark.parametrize('target,label', [
    ('selection', 'your selection'), ('draft', 'your draft'),
    ('last_sentence', 'your last sentence'), ('last_dictation', 'what you just said'),
])
def test_e1_resolved_target_reaches_the_proposal_chip(edit_rig, target, label):
    r = edit_rig
    r.manager.draft_document.append('the draft needs capitals.')
    request = actions.from_action(r.app, {'target': target, 'instruction': 'clean up'},
        snapshot=actions.ingress_payload(r.app, 'edit').snapshot, original_utterance='edit')
    assert actions.propose_and_stage(r.app, request)
    assert not r.mutations
    assert label in r.app.chips[-1][0]


@pytest.mark.parametrize('target', ['your selection', 'your draft', 'your last sentence', 'what you just said'])
def test_e1_target_copy_has_both_spoken_choices_and_preserves_pending(target):
    label, severity = session_feedback.outcome_chip('edit_proposed', {'changes': 1, 'target': target})
    # 1137 n08: retain the display width by putting the choices on line two.
    assert label == f'Proposed edit to {target}:\n1 change - say apply or discard'
    assert severity == 'pending'
    assert session_feedback.chip_ttl_ms('edit_proposed', severity) is None
    refused, severity = session_feedback.outcome_chip('edit_refused', {'reason': 'the text changed', 'target': target})
    assert target in refused and 'the text changed' in refused and severity == 'warning'


def test_c5_invented_action_does_not_request_nearby_commands(runtime, monkeypatch):
    retry = Mock(side_effect=AssertionError('refusals must not ask a model for nearby commands'))
    monkeypatch.setattr(ava, 'ask_model', retry)
    ctx = context(runtime, 'teleport the moon')
    result = ava.handle_response(runtime.app, wire.Response('CONFIRM Ready.\nACTION teleport moon', ctx),
        original_text=ctx.utterance, generation=ctx.generation)
    assert result.state == 'refused'
    assert 'no Samsara command' in runtime.spoken[-1]
    assert 'what can I say' in runtime.spoken[-1]
    assert not runtime.app.synthetic_effects and not retry.called


@pytest.mark.parametrize('shape', ['legacy', 'plan', 'context_free'])
def test_c5_punctuation_edit_cannot_open_correction_learning(rig, make_executor, monkeypatch, shape):
    effects = []
    ex = make_executor([dict(phrase='samsara correction',
        func=lambda *_: effects.append('opened') or True)], count=0)
    rig.app.command_executor = ex
    ex._app = rig.app
    utterance = 'Could you fix the punctuation in the selection?'
    ctx = wire.new_request(rig.app, utterance)
    ctx.capabilities.lookup(query='samsara correction')
    response = ('CONFIRM Ready.\nACTION samsara correction' if shape != 'plan' else
        json.dumps({'type': 'plan', 'plan': [offered(ctx, 'samsara correction')]}))
    reply = response if shape == 'context_free' else wire.Response(response, ctx)
    result = ava.handle_response(rig.app, reply, original_text=utterance, generation=0)
    assert result.state == 'refused' and effects == []
    if shape != 'context_free':
        assert ctx.snapshot.released


@pytest.mark.parametrize('reason,fragment', [
    ('registry_unavailable', "can't read Samsara's commands"),
    ('no_command', 'no Samsara command'), ('lookup_incomplete', "right command yet"),
    ('pack_disabled', 'pack which is off'), ('out_of_scope', 'different app or context'),
    ('adapter_unavailable', 'cannot run it through this route'),
    ('execution_failed', 'failed'), ('execution_unknown', "couldn't verify"),
])
def test_c5_local_inability_codes_remain_distinct(runtime, reason, fragment):
    result = wire.refusal(runtime.app, reason)
    assert result.reason == reason and result.state != 'completed'
    assert fragment in runtime.spoken[-1]
    assert not runtime.app.synthetic_effects


@pytest.mark.parametrize('case,expected', [
    ('missing', 'no_command'), ('beyond_page', 'lookup_incomplete'),
    ('exhausted', 'lookup_incomplete'), ('registry', 'registry_unavailable'),
])
def test_c2_c5_unavailable_is_computed_from_live_evidence(runtime, case, expected):
    ctx = context(runtime, 'fixture operation' if case == 'beyond_page' else 'teleport moon')
    if case == 'exhausted':
        for _ in range(3):
            ctx.capabilities.lookup(query='fixture')
    if case == 'registry':
        ctx.capabilities = None
    result = deliver(ctx, {'type': 'unavailable'})
    assert result.reason == expected and result.state == 'refused'
    assert not runtime.app.synthetic_effects


def test_c5_edit_capability_is_not_absent_when_registry_has_no_edit_command(runtime):
    ctx = context(runtime, 'Could you fix punctuation in the selection?')
    result = deliver(ctx, {'type': 'unavailable'})
    assert result.reason == 'lookup_incomplete'
    assert 'no Samsara command' not in runtime.spoken[-1]


def test_c5_model_cannot_supply_an_inability_reason(runtime):
    ctx = context(runtime, 'reviewed navigation')
    result = deliver(ctx, {'type': 'unavailable', 'reason': 'no_command'})
    assert result.state == 'refused' and result.reason != 'no_command'
    assert not runtime.app.synthetic_effects


@pytest.mark.parametrize('entry,reason,fragment', [
    ({'pack': 'off'}, 'pack_disabled', 'off pack'),
    ({'scope': {'apps': ['fixture.exe']}}, 'out_of_scope', 'fixture.exe'),
    ({'param_schema': {'level': {'type': 'int'}}}, 'adapter_unavailable', 'cannot run'),
])
def test_c5_unavailable_names_actual_registry_restriction(rig, make_executor, entry, reason, fragment):
    ex = make_executor([dict(phrase='unique operation', **entry)], count=0)
    rig.app.command_executor = ex
    ex._app = rig.app
    ctx = wire.new_request(rig.app, 'unique operation')
    result = deliver(ctx, {'type': 'unavailable'})
    assert result.reason == reason and fragment in rig.spoken[-1]
    assert not rig.effects


@pytest.mark.parametrize('reason,fragment', [('device_lost', 'device lost'),
    ('delivery_unknown', "couldn't verify")])
def test_c5_executor_failure_uses_local_reason(runtime, reason, fragment):
    from samsara.command_registry import DispatchResult, DispatchState
    result = ava._action_outcome(runtime.app, 'reviewed navigation',
        DispatchResult(DispatchState.FAILED, detail={'reason': reason}))
    assert result.state == 'failed' and fragment in runtime.spoken[-1]
    assert ava._outcome_chip(runtime.app, result)[1] == 'error'
    if reason == 'delivery_unknown':
        assert "couldn't verify" in ava._outcome_chip(runtime.app, result)[0]


def test_c5_queued_is_never_a_completed_receipt(runtime):
    from samsara.command_registry import DispatchResult, DispatchState
    result = ava._action_outcome(runtime.app, 'reviewed navigation', DispatchResult(DispatchState.QUEUED))
    assert result.state == 'queued' and ava._outcome_chip(runtime.app, result)[1] != 'success'


def test_e5_queued_edit_keeps_its_apply_discard_question(edit_rig, monkeypatch):
    monkeypatch.setattr(ava, '_ai_state', lambda _app: SimpleNamespace(allowed=True))
    ctx = wire.new_request(edit_rig.app, 'Could you fix punctuation in the selection?')
    result = deliver(ctx, {'type': 'action', 'id': 'edit_text',
        'args': {'target': 'selection', 'instruction': 'Fix punctuation only.'}})
    assert result.state == 'queued' and 'your selection' in edit_rig.app.chips[-1][0]
    # The editor already owns a visible Apply/Discard question; never replace
    # it with the generic command's "needs a yes" receipt.
    assert ava._outcome_chip(edit_rig.app, result) is None


@pytest.mark.parametrize('text', [
    'make me a coffee', 'make that', 'explain the phrase fix the selection',
    'she said fix the selection yesterday', '"fix the selection"',
])
def test_h1_ambient_text_stays_in_the_draft(edit_rig, text):
    r = edit_rig
    r.manager.mode = SessionMode.DICTATE
    r.manager._buffer_dictate_until_commit = True
    r.manager._agent_dispatch_fn = MethodType(lambda self, *_: None, r.app)
    r.manager._format_dictate_fn = lambda value: value
    before = r.manager.dictate_pending_buffer
    r.manager.dispatch_utterance(text, UtteranceSignals(True, (1.0,)))
    assert session.pending() is None and not r.calls and not r.mutations
    assert text in r.manager.dictate_pending_buffer and r.manager.dictate_pending_buffer != before


def test_h1_existing_draft_cleanup_and_discard_exception(edit_rig):
    r = edit_rig
    r.manager.mode = SessionMode.DICTATE
    r.manager._buffer_dictate_until_commit = True
    r.manager._agent_dispatch_fn = MethodType(lambda self, *_: None, r.app)
    r.manager.draft_document.append('the draft needs capitals.')
    signals = UtteranceSignals(True, (1.0,))
    assert r.manager.dispatch_utterance('clean up', signals).kind == 'ava_edit_handled'
    assert session.pending().bound_target.kind == 'draft' and not r.mutations
    assert r.manager.dispatch_utterance('discard', signals).kind == 'ava_edit_handled'
    assert session.pending() is None and not r.mutations


@pytest.mark.parametrize('display', ['Full', 'Compact'])
def test_e5_both_displays_show_target_and_retire_expired_question(edit_rig, qapp, monkeypatch, display):
    from samsara.live_surface.feedback import schedule_feedback
    from samsara.ui import qt_runtime
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.ui.listening_indicator import ListeningIndicator
    r = edit_rig
    r.app.config['ui'] = {'live_surface': {'enabled': True}}
    r.app._schedule_ui = lambda fn, *a, **kw: fn(*a, **kw)
    monkeypatch.setattr(qt_runtime, 'post', lambda *_: None)
    controller = LiveSurfaceController(r.app) if display == 'Full' else None
    indicator = LiveSurfaceIndicatorAdapter(controller) if controller else ListeningIndicator()
    r.app.listening_indicator = indicator
    r.app._show_outcome_chip = lambda label, kind, ttl: schedule_feedback(r.app, label, kind, ttl)
    try:
        assert session.handle_utterance(r.app, 'fix this')
        if controller:
            controller.drain()
        label = controller.view().notice.message if controller else indicator._chip_label
        assert 'your selection' in label
        question = r.app._ava_question
        monkeypatch.setattr(ava_pending, 'time', SimpleNamespace(monotonic=lambda: question.deadline + 1))
        ava_pending._tick(r.app, question)
        if controller:
            controller.refresh_expired()
            controller.drain()
        else:
            indicator._clear_outcome()
        label = controller.view().notice.message if controller else indicator._chip_label
        assert not label or 'say apply' not in label
        assert session.pending() is None and r.app._ava_question is None and not r.mutations
    finally:
        if not controller:
            indicator.hide()
            indicator.deleteLater()


def test_h1_quick_reference_explains_the_existing_mode_boundary():
    description = qr._MODE_DESCRIPTIONS[SessionMode.DICTATE]
    assert 'For selection edits, switch to Ava or command mode' in description
    assert 'ordinary speech stays in your draft' in description
    assert 'Draft cleanup and apply or discard for a waiting edit still work.' in description


@pytest.mark.parametrize('target', ['your selection', 'your draft', 'your last sentence', 'what you just said'])
def test_n08_two_line_proposal_fits_both_displays_without_widening(qapp, target):
    from dataclasses import replace
    from samsara.live_surface.model import Notice, NoticeKind, VisibleForm
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui.indicator_message_layout import message_lines
    from tests.test_live_surface_qt import _view

    old, _ = session_feedback.outcome_chip('edit_proposed', {'changes': 1})
    label, _ = session_feedback.outcome_chip('edit_proposed', {'changes': 1, 'target': target})
    view = replace(_view(VisibleForm.STATUS), notice=Notice(NoticeKind.PENDING, old, None))
    full = LiveSurfaceWidget(view)
    compact = ListeningIndicator()
    try:
        old_width = full.width()
        full.refresh(replace(view, notice=Notice(NoticeKind.PENDING, label, None)), animate=False)
        assert full.width() <= old_width
        band = full._chip or full._band
        assert not band.elided and band.label.text().splitlines() == label.splitlines()
        # n08 keeps the selection picture narrow; other labels may be longer.
        compact_limit = old if target == 'your selection' else label.replace('\n', ' ')
        assert compact._chip_width(label) <= compact._chip_width(compact_limit)
        lines, elided = message_lines(label, compact._chip_font(), compact._chip_width(label) - 24)
        assert not elided and lines == label.splitlines()
    finally:
        full.close()
        compact.destroy()


@pytest.mark.parametrize('word', ['finish', 'send', 'submit'])
def test_n08_help_words_match_paste_and_submit_effects(monkeypatch, word):
    from samsara import session_modes
    from tests.test_hands_free_session import _build, _match, GOOD
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', 'finish')
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'finish'}))
    manager, _, events = _build(probe=lambda text: _match(text,
        policy=session_modes.PendingTextPolicy.COMMIT) if text == 'submit' else None)
    manager.dispatch_utterance('Synthetic message.', GOOD)
    manager.dispatch_utterance(word, GOOD)
    assert events == [('paste', 'Synthetic message.')] + ([('command', 'submit')] if word == 'submit' else [])
    assert not manager.dictate_pending_buffer
    # 1137 n10: command words resolve at display time from the active registries.
    description = qr._dictate_description(SimpleNamespace(config={}))
    assert '"finish" (the default) or "send" to paste' in description
    assert '"submit" to paste it and press Enter to send' in description
    assert '"end"' not in description


def test_n10_legacy_refusal_does_not_borrow_another_responses_context(runtime, monkeypatch):
    context(runtime, 'teleport the moon')
    retry = Mock(return_value=ava.AvaReply('No command was run.', 'fixture', 1))
    monkeypatch.setattr(ava, 'ask_model', retry)
    result = ava.handle_response(runtime.app, 'CONFIRM Ready.\nACTION teleport moon',
                                 original_text='teleport the moon', generation=0)
    assert result.state == 'spoken' and result.reason == 'action_refused_conversational'
    assert runtime.spoken[-1] == 'No command was run.'
    retry.assert_called_once()
    assert not runtime.app.synthetic_effects


def test_n10_help_refreshes_active_words_and_does_not_invent_missing_commands(monkeypatch):
    from samsara import session_modes
    app = SimpleNamespace(config={})
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', 'ready')
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'ready'}))
    records = [{'canonical_id': 'builtin.submit', 'aliases': ['deliver'], 'phrase': 'deliver'},
               {'canonical_id': 'fixture.alias', 'aliases': ['send'], 'phrase': 'send'}]
    monkeypatch.setattr(qr.command_catalog, 'guidance_catalog', lambda _rows: records)
    description = qr._resolve_modes_overview(app)['modes'][1]['description']
    assert '"ready" or "send" to paste' in description
    assert '"deliver" to paste it and press Enter to send' in description
    assert 'the default' not in description and '"finish"' not in description
    records.clear()
    description = qr._resolve_modes_overview(app)['modes'][1]['description']
    assert 'Say "ready" to paste' in description
    assert 'command guidance is unavailable' in description
    assert '"send"' not in description and '"deliver"' not in description
