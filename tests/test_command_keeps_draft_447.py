"""447: real exact matching/dispatch with synthetic foreground and paste seams."""
import json
import sys
import threading
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import session_modes, streaming
from samsara.hotkeys import HotkeyCluster
from samsara.live_surface.controller import LiveSurfaceController
from samsara.session_modes import PendingTextPolicy, SessionMode
from samsara.streaming import DictatePreviewSession, StreamingSession
from tests.test_hands_free_exact_363 import (
    SIGNALS, lane as _exact_lane, registered_executor,
)


COMMANDS = [
    'switch window', 'window switch', 'window switch bravo', 'focus notepad',
    'minimize', 'open', 'close', 'close window', 'show commands', 'command reference',
    'submit', 'press tab', 'next field', 'next tab', 'copy', 'bold',
    'enter', 'previous field', 'back field', 'back tab', 'italic', 'underline',
    'previous tab', 'go back', 'go forward', 'escape',
]
PASTE_FIRST = {'submit', 'enter', 'press tab', 'next field', 'previous field',
              'back field', 'back tab', 'bold', 'italic', 'underline'}
UNAVAILABLE = ['next window', 'open notepad']
ARGUMENT_COMMANDS = {'window switch', 'window switch bravo', 'focus notepad', 'open', 'close'}
SENTENCES = ['Synthetic first sentence.', 'Synthetic second sentence.']


@pytest.fixture(scope='module')
def production():
    # The maintained no-bootstrap adapter supplies the original whole class.
    # Do not call 363's fixture: its 311 helper rewrites/executes source AST.
    app_class = sys.modules['dictation'].DictationApp

    def dispatch(app, text):
        return app._447_command_dispatch(text)

    return {'_probe_media_transport': app_class._probe_media_transport,
            '_probe_hands_free_command': app_class._probe_hands_free_command,
            '_command_dispatch_fn': dispatch}


@pytest.fixture
def lane(production, monkeypatch, registered_executor):
    values = _exact_lane.__wrapped__(production, monkeypatch, registered_executor)
    app = values[0]
    # Reach the nested dispatcher through its real enclosing method. Only
    # callbacks to unrelated app services are inert; matching/dispatch is real.
    for name in ('_apply_formatting_tokens', '_ava_session_agent_dispatch_fn',
                 '_dictate_commit_redecode', '_pop_pending_action_for_scratch',
                 '_answer_cancel_window', '_stage_hands_free_cancel_window',
                 '_handle_deferred_session_outcome', '_speak_session_notice'):
        setattr(app, name, Mock())
    adapter = sys.modules['dictation']
    monkeypatch.setattr(adapter, 'increment_command_count', Mock())
    # Manager wiring installs the configured commit word; restore the module
    # defaults after each case so other gate files see their original state.
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', session_modes.DICTATE_COMMIT_PHRASE)
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', session_modes._DICTATE_COMMIT_HOMOPHONES)
    wired = adapter.DictationApp._ensure_session_mode_manager(app)
    app._447_command_dispatch = wired._command_dispatch_fn
    app._session_mode_manager = None
    return values


@pytest.fixture
def rig(lane, monkeypatch):
    app, make, clock, effects, injected = lane
    target = {'exe': 'chrome.exe', 'hwnd': 123}
    pastes = []
    events = []
    paste_ok = [True]
    compact = []
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda fn: fn())

    class Overlay:
        STATE_DONE = streaming.StreamingOverlayQt.STATE_DONE

        def __init__(self, *args, **kwargs):
            pass

        def set_interaction_callbacks(self, *args):
            pass

        def set_transcript(self, lines, partial, **kwargs):
            compact[:] = [list(lines), partial]

        def set_prompt(self, text):
            pass

        def set_literal_badge(self, on):
            pass

    monkeypatch.setattr(streaming, 'StreamingOverlayQt', Overlay)

    def build(live):
        manager = make(live)
        manager._foreground_exe_resolver = lambda: target['exe']
        manager._foreground_hwnd_resolver = lambda: target['hwnd']

        def paste(text, guard=None):
            assert guard is None or guard()
            events.append(('paste', text))
            pastes.append({'text': text, 'target': dict(target), 'callers': [
                f'{frame.name}:{frame.lineno}' for frame in traceback.extract_stack()
                if frame.name in ('_dispatch_hands_free_command', '_commit_dictate_buffer',
                                  '_do_switch', '_deliver_final', 'commit_pending_dictation')]})
            return paste_ok[0]

        manager._inject_fn = paste
        dispatch = manager._command_dispatch_fn

        def effect(text):
            events.append(('effect', text))
            return dispatch(text)

        manager._command_dispatch_fn = effect
        app.is_speaking = False
        app.device_type = 'cuda'
        app.live_surface = LiveSurfaceController(app) if live else None
        preview = DictatePreviewSession(app)

        def show(text='', committed=False):
            preview.on_utterance_final(text, dictate_committed=committed)
            if live:
                app.live_surface.sync_draft(manager)

        def visible():
            if live:
                return app.live_surface.view().text.strip()
            return ' '.join(compact[0]).strip()

        for sentence in SENTENCES:
            assert manager.dispatch_utterance(sentence, SIGNALS).kind == 'dictate_staged'
            show(sentence)
        clock[0] += 3
        return SimpleNamespace(app=app, manager=manager, preview=preview, show=show,
                               visible=visible, target=target, pastes=pastes, effects=effects,
                               events=events, paste_ok=paste_ok)

    return build


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('command', COMMANDS)
@pytest.mark.parametrize('variation', ['settled', 'partial-open', 'foreground-moved', 'command-only'])
def test_exact_command_pending_policy(rig, live, command, variation, record_property):
    r = rig(live)
    before = r.manager.dictate_pending_buffer
    document_before = (r.manager.draft_document.document_id, r.manager.draft_document.revision,
                       r.manager.draft_document.segments, r.manager.draft_document.manual_commit)
    shown_before = r.visible()
    assert shown_before == before.strip()
    # 415: argument-taking commands need explicit intent even with a held draft.
    spoken = 'command ' + command if command in ARGUMENT_COMMANDS else command
    probe = r.app._probe_hands_free_command(spoken)
    assert probe is not None
    assert probe.pending_policy is (PendingTextPolicy.COMMIT if command in PASTE_FIRST
                                    else PendingTextPolicy.PRESERVE)
    if variation == 'partial-open':
        r.preview._render('Synthetic unfinished partial')
    if variation == 'foreground-moved':
        r.target.update(exe='writer.exe', hwnd=456)
    # Substitute the command effect only, after the production matcher/policy.
    dispatch = r.manager._command_dispatch_fn

    def move(text):
        result = dispatch(text)
        r.target.update(exe='writer.exe', hwnd=456)
        return result

    r.manager._command_dispatch_fn = move
    if variation == 'command-only':
        # Hold command-only capture dispatches straight to the executor; it
        # does not switch the persistent hands-free manager's lane.
        r.app.command_mode_recording = True
        result = move(spoken)
        kind = result.state
    else:
        result = r.manager.dispatch_utterance(spoken, SIGNALS)
        kind = result.kind
        r.show(spoken)
    record_property('447_row', json.dumps({
        'display': 'Full' if live else 'Compact', 'command': command,
        'variation': variation, 'policy': probe.pending_policy.value,
        'outcome': kind, 'pastes': r.pastes, 'effects': len(r.effects),
    }))
    assert len(r.effects) == 1
    assert r.manager.mode is SessionMode.DICTATE
    if command in PASTE_FIRST and variation != 'command-only':
        assert [paste['text'] for paste in r.pastes] == [before]
        assert r.events == [('paste', before), ('effect', probe.dispatch_text)]
        assert not r.manager.dictate_pending_buffer
        assert r.visible() == ''
    else:
        # Command-only capture already bypassed the held-draft dispatcher;
        # preserve that route's pre-447 behavior, including field commands.
        assert r.pastes == []
        assert r.manager.dictate_pending_buffer == before
        assert (r.manager.draft_document.document_id, r.manager.draft_document.revision,
                r.manager.draft_document.segments, r.manager.draft_document.manual_commit) == document_before
        assert r.visible() == shown_before


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('command', sorted(PASTE_FIRST))
def test_failed_field_paste_blocks_effect_and_keeps_draft(rig, live, command):
    r = rig(live)
    before = r.manager.dictate_pending_buffer
    shown_before = r.visible()
    r.paste_ok[0] = False
    outcome = r.manager.dispatch_utterance(command, SIGNALS)
    assert outcome.kind == 'hands_free_command_blocked'
    assert outcome.detail['commit_outcome'] == 'dictate_commit_failed'
    assert [paste['text'] for paste in r.pastes] == [before]
    assert r.events == [('paste', before)]
    assert not r.effects
    assert r.manager.dictate_pending_buffer == before
    assert r.visible() == shown_before
    assert r.manager.mode is SessionMode.DICTATE


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('canonical', ['submit', 'switch window', 'synthetic user command'])
def test_alias_uses_canonical_pending_policy(rig, live, canonical, monkeypatch):
    r = rig(live)
    # This policy unit test supplies synthetic admission; 415 tests real registry aliases.
    monkeypatch.setattr(r.app.command_executor, 'accepts_dictation_command', lambda text, app: True)
    monkeypatch.setattr(r.app.command_executor, 'find_exact_command', lambda text: canonical)
    probe = r.app._probe_hands_free_command('synthetic alias')
    assert probe.phrase == canonical
    assert probe.pending_policy is (PendingTextPolicy.COMMIT if canonical == 'submit'
                                    else PendingTextPolicy.PRESERVE)


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('command', UNAVAILABLE + sorted(ARGUMENT_COMMANDS))
def test_unclaimed_variations_remain_dictation(rig, live, command, record_property):
    r = rig(live)
    before = r.manager.dictate_pending_buffer
    assert r.app._probe_hands_free_command(command) is None
    outcome = r.manager.dispatch_utterance(command, SIGNALS)
    record_property('447_row', json.dumps({'display': 'Full' if live else 'Compact',
        'command': command, 'variation': 'unclaimed', 'outcome': outcome.kind,
        'pastes': r.pastes, 'effects': len(r.effects)}))
    assert outcome.kind == 'dictate_staged'
    # Continuing prose joins the preceding thought without its final stop.
    assert r.manager.dictate_pending_buffer == before.rstrip('.') + ' ' + command
    assert not r.pastes and not r.effects


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('trigger', ['finish', 'trailing-finish', 'local-insert',
                                    'local-key', 'surface-insert', 'quit-send', 'lane-change'])
def test_intentional_delivery_still_pastes(rig, live, trigger, monkeypatch):
    r = rig(live)
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', 'finish')
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'finish'}))
    before = r.manager.dictate_pending_buffer
    r.target.update(exe='writer.exe', hwnd=456)
    if trigger == 'local-insert':
        outcome = r.manager.commit_pending_dictation()
    elif trigger == 'local-key':
        r.app._hands_free_dictation_commit_available = lambda: True
        r.app._handle_session_dispatch_outcome = Mock()
        r.app._dictate_preview = r.preview
        outcome = HotkeyCluster._commit_pending_hands_free_dictation(r.app)
    elif trigger == 'surface-insert':
        controller = r.app.live_surface or LiveSurfaceController(r.app)
        controller._commit()
        outcome = SimpleNamespace(kind='dictate_committed')
    elif trigger == 'quit-send':
        # Call the adapter's exact decision method; no quit or bootstrap.
        from samsara.ui import quit_draft_dialog_qt
        r.manager.pause_draft()
        r.app._live_surface_hold_parking_enabled = lambda: True
        monkeypatch.setattr(quit_draft_dialog_qt, 'ask_quit_draft', lambda: 'send')
        assert sys.modules['dictation'].DictationApp._resolve_parked_draft_before_quit(r.app)
        outcome = SimpleNamespace(kind='dictate_committed')
    else:
        phrase = {'finish': 'finish', 'trailing-finish': 'Synthetic third sentence. Finish.',
                  'lane-change': 'command mode'}[trigger]
        outcome = r.manager.dispatch_utterance(phrase, SIGNALS)
    assert outcome.kind in ('dictate_committed', 'mode_switch')
    assert len(r.pastes) == 1
    assert r.pastes[0]['text'].startswith(before)
    assert r.pastes[0]['target'] == {'exe': 'writer.exe', 'hwnd': 456}
    assert not r.manager.dictate_pending_buffer
    if trigger != 'lane-change':
        assert r.manager.mode is SessionMode.DICTATE


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
def test_finish_after_switch_uses_new_foreground(rig, live, monkeypatch):
    r = rig(live)
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'finish'}))
    before = r.manager.dictate_pending_buffer
    dispatch = r.manager._command_dispatch_fn

    def move(text):
        result = dispatch(text)
        r.target.update(exe='writer.exe', hwnd=456)
        return result

    r.manager._command_dispatch_fn = move
    assert r.manager.dispatch_utterance('switch window', SIGNALS).kind == 'hands_free_command_executed'
    assert not r.pastes
    assert r.manager.dispatch_utterance('finish', SIGNALS).kind == 'dictate_committed'
    assert [paste['text'] for paste in r.pastes] == [before]
    assert r.pastes[0]['target'] == {'exe': 'writer.exe', 'hwnd': 456}
    assert len(r.effects) == 1


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
def test_deferred_command_keeps_draft(rig, live):
    r = rig(live)
    pending = []
    r.manager._cancel_window_fn = lambda match, run: pending.append(run) or 1.0
    before = r.manager.dictate_pending_buffer
    assert r.manager.dispatch_utterance('switch window', SIGNALS).kind == 'command_cancel_window'
    assert not r.pastes and not r.effects
    pending.pop()()
    assert r.manager.dictate_pending_buffer == before
    assert len(r.effects) == 1 and not r.pastes


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('paste_ok', [True, False], ids=['delivered', 'failed'])
def test_deferred_field_command_pastes_only_when_effect_can_run(rig, live, paste_ok):
    r = rig(live)
    pending = []
    r.manager._cancel_window_fn = lambda match, run: pending.append(run) or 1.0
    before = r.manager.dictate_pending_buffer
    r.paste_ok[0] = paste_ok
    assert r.manager.dispatch_utterance('submit', SIGNALS).kind == 'command_cancel_window'
    assert not r.pastes and not r.effects
    outcome = pending.pop()()
    assert [paste['text'] for paste in r.pastes] == [before]
    assert r.manager.mode is SessionMode.DICTATE
    if paste_ok:
        assert outcome.kind == 'hands_free_command_executed'
        assert r.events == [('paste', before), ('effect', 'submit')]
        assert len(r.effects) == 1 and not r.manager.dictate_pending_buffer
    else:
        assert outcome.kind == 'hands_free_command_blocked'
        assert r.events == [('paste', before)]
        assert not r.effects and r.manager.dictate_pending_buffer == before


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
def test_focus_timer_and_session_end_do_not_deliver(rig, live):
    r = rig(live)
    before = r.manager.dictate_pending_buffer
    r.target.update(exe='writer.exe', hwnd=456)
    if live:
        r.app.live_surface.sync_draft(r.manager)
        r.app.live_surface.refresh_expired()
    r.show()
    assert r.manager.dictate_pending_buffer == before
    assert not r.pastes
    assert r.manager.dispatch_utterance('cancel dictation', SIGNALS).kind == 'abort'
    assert r.manager.recoverable_draft == before
    assert not r.pastes


@pytest.mark.parametrize('live', [True, False], ids=['Full', 'Compact'])
def test_hold_release_still_delivers_its_own_text(rig, live, monkeypatch):
    r = rig(live)
    # A hold delivers its own words while the hands-free draft stays waiting.
    app = SimpleNamespace(config={'auto_paste': True, 'ui': {'live_surface': {'enabled': live}}},
        command_mode_active=True, _session_mode_manager=r.manager, _ensure_session_mode_manager=lambda: r.manager,
        _paste_preserving_clipboard=Mock(return_value=True), _hold_park_requested=False,
        _verbatim_rule=lambda: None, live_surface=None, _dictation_consumer=None,
        _on_streaming_session_finished=Mock(), _log_history=Mock(), add_to_history=Mock(),
        _filter_dictation_language=lambda text, info: text)
    session = StreamingSession.__new__(StreamingSession)
    session.app = app
    session._state = StreamingSession.STATE_FINALIZING
    session._state_lock = threading.Lock()
    session.cancel_event = threading.Event()
    session._parking_enabled = live
    session._target_hwnd = 123
    session._parking_requested = False
    session._direct_paste = False
    session._last_pasted = False
    session._overlay = Mock()
    monkeypatch.setattr(streaming, '_user32', SimpleNamespace(GetForegroundWindow=lambda: 123))
    before = r.manager.dictate_pending_buffer
    session._deliver_final('Synthetic released hold.', None, 1.0, 0)
    assert before.strip()
    app._paste_preserving_clipboard.assert_called_once_with('Synthetic released hold.')
    assert r.manager.dictate_pending_buffer == before
    assert not r.pastes
