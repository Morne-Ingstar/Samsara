"""518: optional sentence delivery, synthetic targets and offscreen displays."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.hands_free_delivery import HandsFreeDelivery, finish_sentence_display
from samsara.session_modes import (InjectionDelivery, SessionMode, SessionModeManager,
                                   UtteranceSignals, outcome_chip)
from tests.test_command_keeps_draft_447 import lane, production
from tests.test_hands_free_exact_363 import registered_executor
from tests.test_hold_types_with_kept_draft_508 import kept

SIGNALS = UtteranceSignals(True, (1.2,), True)
FIRST = 'Synthetic first sentence.'
SECOND = 'Synthetic second sentence.'


@pytest.fixture
def rig():
    app = SimpleNamespace(config={'hands_free': {'send_mode': 'as_i_speak'}},
                          add_to_history=Mock(), _log_history=Mock(), _show_outcome_chip=Mock())
    target = ['synthetic.exe', 99]
    calls, removed = [], []
    state = {'ok': True, 'confirmed': True, 'drift': False, 'trailing': False}

    def inject(text, guard):
        if state['drift']:
            target[1] += 1
        if not guard() or not state['ok']:
            return False
        delivered = getattr(manager, '_sentence_prefix', '') + text.strip() + (' ' if state['trailing'] else '')
        calls.append((tuple(target), delivered))
        app.add_to_history(delivered.strip(), is_command=False)
        return InjectionDelivery(delivered, confirmed=state['confirmed'])

    manager = SessionModeManager(
        abort_phrases=['cancel'], foreground_exe_resolver=lambda: target[0],
        foreground_hwnd_resolver=lambda: target[1], inject_fn=inject,
        remove_chars_fn=lambda count: removed.append(count) or True,
        command_dispatch_fn=Mock(), agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True, live_surface_enabled=True)
    manager._hands_free_delivery = HandsFreeDelivery(app)
    manager.reset(SessionMode.DICTATE)
    return SimpleNamespace(app=app, manager=manager, target=target, calls=calls,
                           removed=removed, state=state)


@pytest.mark.parametrize('trailing', [False, True])
def test_two_sentences_use_shared_commit_guard_receipt_and_exact_spacing(rig, trailing):
    r = rig
    r.state['trailing'] = trailing
    for text in (FIRST, SECOND):
        result = r.manager.dispatch_utterance(text, SIGNALS)
        assert result.kind == 'dictate_committed' and result.detail['immediate']
        assert r.manager.dictate_pending_buffer == ''
        assert not r.manager.has_parked_hold
    expected = FIRST + ' Synthetic second sentence.' + (' ' if trailing else '')
    assert ''.join(text for target, text in r.calls) == expected
    assert r.app.add_to_history.call_count == 2
    assert r.manager.top_dictation_chunk().payload == r.calls[-1][1]


def test_each_sentence_chooses_current_window_without_leading_space_on_new_target(rig):
    r = rig
    r.manager.dispatch_utterance(FIRST, SIGNALS)
    r.target[:] = ['other.exe', 100]
    r.manager.dispatch_utterance(SECOND, SIGNALS)
    assert r.calls == [(('synthetic.exe', 99), FIRST), (('other.exe', 100), SECOND)]


def test_focus_change_between_sentence_target_choice_and_commit_is_refused(rig):
    r = rig
    windows = iter([99, 100, 100, 100])
    r.manager._foreground_hwnd_resolver = lambda: next(windows, 100)
    result = r.manager.dispatch_utterance(FIRST, SIGNALS)
    assert result.kind == 'dictate_commit_blocked_focus_lock'
    assert result.detail['saved_to_history']
    assert not r.calls and not r.manager.dictate_pending_buffer


@pytest.mark.parametrize('failure', ['drift', 'clipboard', 'unknown target', 'admin', 'exception'])
def test_failed_or_refused_sentence_saved_only_to_history_and_session_continues(rig, failure):
    r = rig
    if failure == 'drift':
        r.state['drift'] = True
    elif failure == 'clipboard':
        r.state['ok'] = False
    elif failure == 'unknown target':
        r.target[1] = None
    elif failure == 'admin':
        r.manager._window_integrity_fn = lambda: SimpleNamespace(blocked=True, describe=lambda: 'synthetic admin')
    else:
        r.manager._inject_fn = Mock(side_effect=RuntimeError('synthetic failure'))
    original_inject = r.manager._inject_fn
    result = r.manager.dispatch_utterance(FIRST, SIGNALS)
    assert result.detail['saved_to_history']
    assert 'saved to History' in outcome_chip(result.kind, result.detail)[0]
    assert r.manager.dictate_pending_buffer == ''
    assert r.manager.mode is SessionMode.DICTATE
    assert not r.manager.retype_last_suppressed()
    assert r.manager.commit_pending_dictation().detail['empty']
    r.app.add_to_history.assert_called_once_with(FIRST, is_command=False)
    r.app._log_history.assert_called_once()
    assert r.calls == []
    if failure == 'exception':
        r.manager._inject_fn = lambda text, guard: InjectionDelivery(text, confirmed=True) if guard() else False
    else:
        r.manager._inject_fn = original_inject
    r.target[:] = ['other.exe', 100]
    r.manager._window_integrity_fn = None
    r.state.update(ok=True, drift=False)
    assert r.manager.dispatch_utterance(SECOND, SIGNALS).kind == 'dictate_committed'


def test_default_stages_and_live_setting_change_preserves_waiting_words(rig):
    r = rig
    r.app.config['hands_free'].pop('send_mode')
    assert r.manager.dispatch_utterance(FIRST, SIGNALS).kind == 'dictate_staged'
    before = r.manager.draft_document.segments
    r.app.config['hands_free']['send_mode'] = 'as_i_speak'
    assert r.manager.draft_document.segments == before and not r.calls
    assert r.manager.dispatch_utterance(SECOND, SIGNALS).detail['immediate']
    assert r.manager.dictate_pending_buffer == FIRST
    assert r.manager.draft_document.segments == before
    assert r.calls == [(('synthetic.exe', 99), SECOND)]
    assert r.manager.commit_pending_dictation().kind == 'dictate_committed'
    assert [text for target, text in r.calls] == [SECOND, FIRST]
    assert r.manager.dispatch_utterance(FIRST, SIGNALS).detail['immediate']
    r.app.config['hands_free']['send_mode'] = 'staged'
    assert r.manager.dispatch_utterance(SECOND, SIGNALS).kind == 'dictate_staged'


@pytest.mark.parametrize('ok', [True, False], ids=['confirmed', 'failed'])
def test_setting_change_preserves_parked_document_and_scratch_targets_only_sent_sentence(rig, ok):
    r = rig
    r.manager.stage_parked_hold(FIRST)
    before = (r.manager.draft_document.document_id, r.manager.draft_document.revision,
              r.manager.draft_document.segments, r.manager.recoverable_draft)
    r.state['ok'] = ok
    result = r.manager.dispatch_utterance(SECOND, SIGNALS)
    assert bool(result.detail.get('saved_to_history')) is not ok
    assert r.manager.dictate_pending_buffer == FIRST and r.manager.has_parked_hold
    assert before == (r.manager.draft_document.document_id, r.manager.draft_document.revision,
                      r.manager.draft_document.segments, r.manager.recoverable_draft)
    scratch = r.manager.dispatch_utterance('scratch that', SIGNALS)
    assert scratch.kind == ('scratch_success' if ok else 'scratch_refuse')
    assert r.removed == ([len(SECOND)] if ok else [])
    assert r.manager.dictate_pending_buffer == FIRST


def test_new_session_resets_join_after_failed_sentence_disarmed_the_stack(rig):
    r = rig
    r.manager.dispatch_utterance(FIRST, SIGNALS)
    r.state['ok'] = False
    r.manager.dispatch_utterance(SECOND, SIGNALS)
    r.manager.reset(SessionMode.DICTATE)
    r.state['ok'] = True
    r.manager.dispatch_utterance(SECOND, SIGNALS)
    assert r.calls[-1][1] == SECOND


@pytest.mark.parametrize('confirmed', [True, False])
def test_scratch_uses_existing_confirmed_paste_undo_only(rig, confirmed):
    r = rig
    r.manager.dispatch_utterance(FIRST, SIGNALS)
    r.state['confirmed'] = confirmed
    result = r.manager.dispatch_utterance(SECOND, SIGNALS)
    assert bool(result.detail.get('delivery_unverified')) is not confirmed
    result = r.manager.dispatch_utterance('scratch that', SIGNALS)
    assert result.kind == ('scratch_success' if confirmed else 'scratch_refuse')
    assert r.removed == ([len(r.calls[-1][1])] if confirmed else [])
    if not confirmed:
        assert r.manager.dispatch_utterance('scratch that', SIGNALS).kind == 'scratch_refuse'
        assert not r.removed


def test_empty_paste_word_quiet_and_exit_keeps_nothing(rig):
    from samsara import session_modes
    r = rig
    r.manager.dispatch_utterance(FIRST, SIGNALS)
    result = r.manager.dispatch_utterance(session_modes.DICTATE_COMMIT_PHRASE, SIGNALS)
    assert result.kind == 'dictate_committed' and result.detail['empty']
    assert outcome_chip(result.kind, result.detail) == ('nothing to send', 'accent')
    r.manager.dispatch_utterance('cancel', SIGNALS)
    r.manager.retain_draft()
    r.manager.reset()
    assert not r.manager.retained_draft and not r.manager.recoverable_draft


def test_failed_delivery_disarms_older_paste_undo_without_losing_sentence_spacing(rig):
    r = rig
    r.manager.dispatch_utterance(FIRST, SIGNALS)
    r.state['ok'] = False
    assert r.manager.dispatch_utterance(SECOND, SIGNALS).detail['saved_to_history']
    assert r.manager.dispatch_utterance('scratch that', SIGNALS).kind == 'scratch_refuse'
    assert not r.removed
    r.state['ok'] = True
    r.manager.dispatch_utterance('Synthetic next sentence.', SIGNALS)
    assert r.calls[-1][1] == ' Synthetic next sentence.'


def test_next_sentence_after_scratch_keeps_the_surviving_sentence_seam(rig):
    r = rig
    r.manager.dispatch_utterance(FIRST, SIGNALS)
    r.manager.dispatch_utterance(SECOND, SIGNALS)
    assert r.manager.dispatch_utterance('scratch that', SIGNALS).kind == 'scratch_success'
    r.manager.dispatch_utterance('Synthetic replacement sentence.', SIGNALS)
    assert r.calls[-1][1] == ' Synthetic replacement sentence.'


def test_command_and_ava_lanes_do_not_use_sentence_delivery(rig):
    r = rig
    r.manager.dispatch_utterance('command mode', SIGNALS)
    r.manager.dispatch_utterance('synthetic command', SIGNALS)
    r.manager._command_dispatch_fn.assert_called_once()
    r.manager.dispatch_utterance('ava mode', SIGNALS)
    r.manager.dispatch_utterance('Please explain the synthetic example.', SIGNALS)
    r.manager._agent_dispatch_fn.assert_called_once()
    assert not r.calls and not r.manager.dictate_pending_buffer


@pytest.mark.parametrize('mode', ['staged', 'as_i_speak'])
def test_primary_hold_during_session_still_types_once_in_either_mode(kept, mode):
    r = kept(live=True)
    r.app.config['hands_free'] = {'send_mode': mode}
    r.manager._hands_free_delivery = HandsFreeDelivery(r.app)
    before = r.manager.dictate_pending_buffer
    r.session._deliver_final('Synthetic independent hold.', None, 1.0, 0)
    r.app._paste_preserving_clipboard.assert_called_once_with('Synthetic independent hold.')
    assert r.manager.dictate_pending_buffer == before
    r.appended.assert_not_called()


@pytest.mark.parametrize('trailing', [False, True])
@pytest.mark.parametrize('clipboard_only', [False, True])
def test_real_host_callback_formats_once_pastes_and_records_exact_bytes(lane, monkeypatch, trailing, clipboard_only):
    app, make, clock, effects, injected = lane
    app.config.update(hands_free={'send_mode': 'as_i_speak'}, add_trailing_space=trailing,
                      auto_paste=not clipboard_only,
                      smart_corrections={'modes': {'wake': False}}, cleanup_mode='verbatim')
    app._apply_verbatim_if_active = lambda text: None
    app.process_transcription = Mock(side_effect=lambda text: text[0].upper() + text[1:])
    app._apply_formatting_tokens = Mock(side_effect=lambda text: text)
    app._paste_preserving_clipboard = Mock(return_value=(True, True))
    app._notify_main_window = Mock()
    app.play_sound = Mock()
    app._answer_cancel_window = lambda text: None
    app._pop_pending_action_for_scratch = lambda: None
    copy = Mock()
    monkeypatch.setattr(sys.modules['dictation'].pyperclip, 'copy', copy)
    manager = sys.modules['dictation'].DictationApp._ensure_session_mode_manager(app)
    manager._foreground_exe_resolver = lambda: 'synthetic.exe'
    manager._foreground_hwnd_resolver = lambda: 99
    manager._window_integrity_fn = None
    manager.reset(SessionMode.DICTATE)
    waiting = 'Synthetic earlier draft.'
    manager._stage_dictate_chunk(waiting, audio_ref='synthetic earlier audio')
    # 814 / 888a: the earlier draft is prepared before display, outside these two sends.
    app.process_transcription.assert_called_once_with(waiting)
    app.process_transcription.reset_mock()
    app._apply_formatting_tokens.reset_mock()
    before = manager.draft_document.segments
    for text in (FIRST, SECOND):
        result = manager.dispatch_utterance(text, SIGNALS)
        assert result.kind == 'dictate_committed' and result.detail['text']
        assert manager.dictate_pending_buffer == waiting
        assert manager.draft_document.segments == before
        assert manager._dictate_pending_audio == ['synthetic earlier audio']
    if clipboard_only:
        expected = [FIRST, 'Synthetic second sentence.']
        assert [call.args[0] for call in copy.call_args_list] == [text + (' ' if trailing else '') for text in expected]
        app._paste_preserving_clipboard.assert_not_called()
        assert all(call.kwargs['status'] == 'buffered' for call in app._log_history.call_args_list)
        assert manager.dictate_pending_buffer == waiting
        assert manager.dispatch_utterance('scratch that', SIGNALS).kind == 'scratch_refuse'
        return
    chunks = [call.args[0] for call in app._paste_preserving_clipboard.call_args_list]
    assert ''.join(chunks) == FIRST + ' Synthetic second sentence.' + (' ' if trailing else '')
    assert app.process_transcription.call_count == 2
    assert app._apply_formatting_tokens.call_count == 2
    assert app._log_history.call_count == 2
    assert manager.top_dictation_chunk().payload == chunks[-1]


def test_settings_row_search_save_and_staging_default(qapp):
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults
    app = _StubApp(_schema_defaults())
    app.config.setdefault('hands_free', {})['exact_commands'] = False
    app.config['echo_cancellation'] = {'enabled': True, 'latency_ms': 120}
    window = _SettingsWindow(app)
    try:
        combo = window._widgets['hands_free_send_mode']
        assert combo.currentData() == SETTINGS_SCHEMA['hands_free.send_mode']['default'] == 'staged'
        assert [combo.itemText(i) for i in range(combo.count())] == ['When I say send', 'As I speak']
        combo.setCurrentIndex(1)
        updates = window._collect_destination_updates({})
        assert updates['hands_free'] == {'exact_commands': False, 'send_mode': 'as_i_speak'}
        assert updates['echo_cancellation'] == app.config['echo_cancellation']
        combo.setCurrentIndex(0)
        staged = window._collect_destination_updates({})
        assert staged['hands_free']['send_mode'] == 'staged'
        assert staged['echo_cancellation'] == app.config['echo_cancellation']
        combo.setCurrentIndex(1)
        assert app.config['hands_free']['send_mode'] == 'staged'
        window._search_edit.setText('Hands-free sends')
        window._apply_search_filter()
        row = window._rows_by_widget['hands_free_send_mode']
        assert not row.isHidden() and row.page_name == 'Dictation'
        destination = os.environ.get('SAMSARA_518_PICTURES')
        if destination:
            from samsara.ui import theme
            for mode in ('dark', 'light'):
                theme.set_theme(mode, refresh=False)
                window.apply_theme()
                window.resize(1080, 780)
                window.show()
                qapp.processEvents()
                qapp.processEvents()
                row.grab().save(str(Path(destination) / f'Settings_{mode}_row_100.png'))
                window.grab().save(str(Path(destination) / f'Settings_{mode}_100.png'))
    finally:
        window.deleteLater()


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
def test_two_sentences_display_speaking_result_then_rest_without_insert(qapp, rig, monkeypatch, full):
    from samsara.live_surface.controller import LiveSurfaceController
    from samsara.live_surface.model import CaptureState, DocumentState, Lane, LiveSurfaceModel
    from samsara.live_surface.partials import CapturePartials
    from samsara.live_surface.placement import Screen, Rect
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    from samsara.ui import theme
    theme.set_theme('dark', refresh=False)
    r = rig
    r.app._session_mode_manager = r.manager
    r.app.config['ui'] = {'live_surface': {'enabled': full}}
    monkeypatch.setattr('samsara.ui.qt_runtime.post', lambda fn: fn())
    clock = [0.0]
    controller = LiveSurfaceController(r.app, model=LiveSurfaceModel(clock=lambda: clock[0]),
        widget_factory=lambda source: LiveSurfaceWidget(source, reduced_motion=True))
    controller._screens = lambda: [Screen('synthetic', Rect(0, 0, 1280, 720), True)]
    r.app.live_surface = controller if full else None
    indicator = None
    if full:
        controller.idle_capture = CaptureState.HANDS_FREE_LISTENING
        controller.idle_lane = Lane.HANDS_FREE_DICTATE
        controller.start()
    else:
        from samsara.ui.listening_indicator import ListeningIndicator
        indicator = ListeningIndicator()
        indicator._now_ms = lambda: clock[0] * 1000
        indicator.set_idle_animation(False)
        indicator.set_session_mode('dictate', '#19c6df')
        indicator.set_listening(True)
        indicator.show()
        r.app._show_outcome_chip = indicator.show_outcome
    destination = os.environ.get('SAMSARA_518_PICTURES')

    def picture(name):
        qapp.processEvents()
        widget = controller.widget if full else indicator
        if destination:
            widget.grab().save(str(Path(destination) / f'{"Full" if full else "Compact"}_dark_100_{name}.png'))

    try:
        for index, text in enumerate((FIRST, SECOND), 1):
            if full:
                token = controller.begin_capture(Lane.HANDS_FREE_DICTATE)
                partials = CapturePartials(controller, Lane.HANDS_FREE_DICTATE, token)
                partials.partial(text)
            else:
                indicator.show_outcome(text, 'live', None)
            picture(f'{index}_speaking')
            result = r.manager.dispatch_utterance(text, SIGNALS)
            if full:
                controller.sync_draft(r.manager)
                partials.stop()
            finish_sentence_display(r.app, result)
            picture(f'{index}_result')
            if full:
                assert controller.view().document is DocumentState.DELIVERED
                assert controller.view().text == result.detail['text'].strip()
                assert result.detail['text'].strip() in controller.widget._band.label.text()
                assert controller.widget._band.label.isVisible()
                from PySide6.QtWidgets import QPushButton
                assert not any(button.isVisible() and 'Insert text' in button.text()
                               for button in controller.widget.findChildren(QPushButton))
            else:
                assert indicator._chip_label == result.detail['text'].strip()
            assert not r.manager.dictate_pending_buffer
            clock[0] += 10  # Beyond the shared reading-time policy's 8 s cap.
            if full:
                controller.refresh_expired()
                assert controller.view().document is DocumentState.EMPTY
                assert controller.view().text == ''
                assert not controller.view().notice.message
            else:
                indicator.clear_outcome()
                assert indicator._chip_label is None
            picture(f'{index}_rest')
    finally:
        controller.close()
        if indicator:
            indicator.destroy()
            indicator.deleteLater()


def test_full_receipt_keeps_an_earlier_waiting_draft_visible(rig, monkeypatch):
    from samsara.live_surface.controller import LiveSurfaceController
    from samsara.live_surface.model import DocumentState
    r = rig
    r.app._session_mode_manager = r.manager
    r.manager.stage_parked_hold(FIRST)
    monkeypatch.setattr('samsara.ui.qt_runtime.post', lambda fn: fn())
    controller = LiveSurfaceController(r.app)
    r.app.live_surface = controller
    result = r.manager.dispatch_utterance(SECOND, SIGNALS)
    controller.sync_draft(r.manager)
    finish_sentence_display(r.app, result)
    assert controller.view().document is DocumentState.PARKED_DRAFT
    assert controller.view().text == FIRST
    assert SECOND in controller.view().notice.message
    assert r.manager.dictate_pending_buffer == FIRST
