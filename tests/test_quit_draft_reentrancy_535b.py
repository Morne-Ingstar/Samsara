"""535b: real nested Qt quit decisions and real kept/live draft resolution."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import kept_session_draft
from samsara.session_modes import SessionMode
from samsara.ui import quit_draft_dialog_qt
from tests.test_kept_draft_leftovers_510 import quit_method
from tests.test_kept_draft_survives_505 import SIGNALS, TEXT, end_session, make_manager
from tests.test_tray_qt import _make_app


LIVE = 'Synthetic newer sentence.'
COMBINED = TEXT + ' ' + LIVE


def coexist():
    manager, inserted = make_manager(full=False)
    older_audio, newer_audio = object(), object()
    manager._dictate_pending_audio = [older_audio]
    end_session(manager)
    manager.reset(SessionMode.COMMAND)
    assert manager.dispatch_utterance('dictate mode', SIGNALS).kind == 'mode_switch'
    assert manager.dispatch_utterance(LIVE, SIGNALS).kind == 'dictate_staged'
    manager._dictate_pending_audio = [newer_audio]
    assert manager.retained_draft == TEXT
    assert manager.dictate_pending_buffer == LIVE
    return manager, inserted, [older_audio, newer_audio]


@pytest.mark.parametrize('full', [False, True], ids=['Compact', 'Full'])
@pytest.mark.parametrize('choice', ['keep', 'send', 'discard'])
def test_kept_and_live_words_both_resolve(full, choice):
    manager, inserted, audio = coexist()
    # Both displays share the resolver, even if this state originated in Compact.
    manager._live_surface_enabled = full
    before = (manager.retained_draft, manager.dictate_pending_buffer,
              manager.draft_document.segments, manager.stack_depth)
    assert kept_session_draft.resolve_quit_choice(manager, choice) is (choice != 'keep')
    assert inserted == ([COMBINED] if choice == 'send' else [])
    if choice == 'keep':
        assert (manager.retained_draft, manager.dictate_pending_buffer,
                manager.draft_document.segments, manager.stack_depth) == before
        assert manager._retained_draft['audio'] == audio[:1]
        assert manager._dictate_pending_audio == audio[1:]
    else:
        assert not manager.retained_draft
        assert not manager.dictate_pending_buffer
        assert not manager._dictate_pending_audio
        assert not manager.has_parked_hold
        if choice == 'discard':
            assert not any(i.kind == 'dictation_staged_chunk' for i in manager._stack._items)
        else:
            assert manager._stack.peek().kind == 'dictation_chunk'
            assert manager._stack.peek().payload == COMBINED


def test_failed_combined_send_keeps_metadata_and_retry_delivers_once():
    manager, inserted, audio = coexist()
    older = manager._retained_draft['stack_items']
    newer = [i for i in manager._stack._items if i.kind == 'dictation_staged_chunk']
    terminal = manager._last_dictate_ended_terminal
    app = SimpleNamespace(_show_outcome_chip=Mock())
    manager._inject_fn = lambda *args: False
    assert not kept_session_draft.resolve_quit_choice(manager, 'send', app=app)
    assert not inserted and not manager.dictate_pending_buffer
    assert manager.retained_draft == COMBINED
    assert manager._retained_draft['audio'] == audio
    merged_items = manager._retained_draft['stack_items']
    assert merged_items[:len(older)] == older
    assert [item.payload for item in merged_items[len(older):]] == [' ' + newer[0].payload]
    assert merged_items[-1].extra == newer[-1].extra
    assert manager._retained_draft['last_ended_terminal'] == terminal
    app._show_outcome_chip.assert_called_once_with('Delivery failed; draft kept', 'error')
    manager._inject_fn = lambda text, *args: inserted.append(text) or True
    assert kept_session_draft.resolve_quit_choice(manager, 'send', app=app)
    assert inserted == [COMBINED]
    assert not manager.retained_draft and not manager.dictate_pending_buffer


def test_failed_combined_send_can_undo_newer_words_after_resuming():
    manager, _, audio = coexist()
    manager._inject_fn = lambda *args: False
    assert not kept_session_draft.resolve_quit_choice(manager, 'send')
    manager.reset(SessionMode.DICTATE)
    assert manager._do_scratch_that()
    assert manager.dictate_pending_buffer == TEXT
    assert manager._dictate_pending_audio == audio[:1]


@pytest.mark.parametrize('full', [False, True], ids=['Compact', 'Full'])
@pytest.mark.parametrize('choice', ['keep', 'send', 'discard'])
@pytest.mark.parametrize('event', ['update finishes', 'voice restart'])
def test_exit_question_never_stacks_in_nested_qt_loop(qapp, monkeypatch, full, choice, event):
    from PySide6.QtCore import QTimer
    from samsara import app_restart
    from samsara.ui import qt_runtime, update_qt
    from samsara.ui.tray_qt import SamsaraTrayQt
    from plugins.commands import core_utils

    manager, inserted = make_manager(full=full)
    app = _make_app()
    app.config['ui'] = {'listening_display': 'full' if full else 'compact'}
    app._session_mode_manager = manager
    app._quit_draft_pending = False
    resolve = quit_method()
    app._resolve_parked_draft_before_quit = lambda: resolve(app)
    teardown = Mock()

    def quit_app(**kwargs):
        if kwargs.get('_restart_draft_resolved') or resolve(app):
            teardown()

    app.quit_app = quit_app
    tray = SamsaraTrayQt(app)
    app.tray_icon = tray
    tray._rebuild_menu()
    release = SimpleNamespace(version='0.22.1', asset_size=100)
    update = update_qt._UpdateDialog(app, initial_release=release)
    update._phase = 'preparing'
    launch, handoff = Mock(), Mock()
    monkeypatch.setattr(update_qt, 'launch_prepared_update', launch)
    monkeypatch.setattr(app_restart, 'start_handoff', handoff)
    monkeypatch.setattr(app_restart, 'build_relaunch', Mock(return_value=([], '.')))
    monkeypatch.setattr(qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(qt_runtime, 'post', lambda action: QTimer.singleShot(0, action))
    dialogs, errors = [], []
    real_dialog = quit_draft_dialog_qt.QuitDraftDialog

    def nested_event(dialog):
        try:
            assert qapp.activeModalWidget() is dialog
            if event == 'update finishes':
                update._on_prepare_finished(object(), '')
            else:
                core_utils.restart_app(app)
                qapp.processEvents()  # Deliver the voice command's posted Qt action.
            assert len(dialogs) == 1
            assert app._quit_draft_pending
            assert manager.dictate_pending_buffer == TEXT
            launch.assert_not_called()
            handoff.assert_not_called()
            teardown.assert_not_called()
        except BaseException as exc:
            errors.append(exc)
        finally:
            dialog._choose(choice)

    def tracked_dialog():
        dialog = real_dialog()
        dialogs.append(dialog)
        # A broken guard still closes its second dialog, so the test cannot hang.
        QTimer.singleShot(0, lambda: nested_event(dialog) if len(dialogs) == 1
                          else dialog._choose('keep'))
        return dialog

    monkeypatch.setattr(quit_draft_dialog_qt, 'QuitDraftDialog', tracked_dialog)
    try:
        next(a for a in tray._menu.actions() if a.text() == 'Exit').trigger()
        if errors:
            raise errors[0]
        assert len(dialogs) == 1
        assert not app._quit_draft_pending
        assert teardown.call_count == (choice != 'keep')
        assert inserted == ([TEXT] if choice == 'send' else [])
        assert manager.dictate_pending_buffer == (TEXT if choice == 'keep' else '')
        launch.assert_not_called()
        handoff.assert_not_called()
        if event == 'update finishes':
            assert update._phase == 'available' and update._primary.isEnabled()
        else:
            assert not tray._restart_pending
    finally:
        update.hide()
        update.deleteLater()
        tray.stop()
        tray._menu.deleteLater()
        tray._tray.deleteLater()
        for dialog in dialogs:
            dialog.hide()
            dialog.deleteLater()


@pytest.mark.parametrize('failure', ['dialog', 'send'])
def test_pending_flag_released_after_exception_and_next_attempt_can_ask(monkeypatch, failure):
    manager, _ = make_manager(full=False)
    app = SimpleNamespace(_session_mode_manager=manager)
    ask = Mock(side_effect=RuntimeError('Synthetic decision failure')) if failure == 'dialog' else Mock(return_value='send')
    monkeypatch.setattr(quit_draft_dialog_qt, 'ask_quit_draft', ask)
    if failure == 'send':
        monkeypatch.setattr(manager, 'commit_pending_dictation', Mock(side_effect=RuntimeError('Synthetic send failure')))
    resolve = quit_method()
    assert not resolve(app)
    assert not app._quit_draft_pending
    assert manager.dictate_pending_buffer == TEXT
    ask.side_effect = None
    ask.return_value = 'keep'
    assert not resolve(app)
    assert ask.call_count == 2


def test_pending_flag_covers_delivery_after_question_returns(monkeypatch):
    manager, inserted = make_manager(full=False)
    app = SimpleNamespace(_session_mode_manager=manager)
    resolve = quit_method()
    ask = Mock(return_value='send')
    monkeypatch.setattr(quit_draft_dialog_qt, 'ask_quit_draft', ask)

    def inject(text, *args):
        assert app._quit_draft_pending
        assert not resolve(app)
        inserted.append(text)
        return True

    manager._inject_fn = inject
    assert resolve(app)
    assert inserted == [TEXT]
    ask.assert_called_once_with()
    assert not app._quit_draft_pending
