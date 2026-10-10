"""Final callbacks respect cancellation and the real completion lifetime."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from samsara import streaming


def session_stub(parking=False, direct=False):
    session = object.__new__(streaming.StreamingSession)
    session._state_lock = threading.Lock()
    session._paste_lock = threading.Lock()
    session._capture_cleanup_lock = threading.Lock()
    session._capture_cleaned = False
    session._finished_notified = False
    session.cancel_event = threading.Event()
    session.stop_event = threading.Event()
    session._state = session.STATE_FINALIZING
    session._parking_enabled = parking
    session._direct_paste = direct
    session._last_pasted = False
    session._overlay = Mock()
    session._target_hwnd = 123
    session._focus_unchanged = Mock(return_value=True)
    session.app = SimpleNamespace(
        config={'auto_paste': True}, _tutorial_hooks={},
        _paste_preserving_clipboard=Mock(return_value=True),
        add_to_history=Mock(), _log_history=Mock(), _notify_main_window=Mock(),
        _schedule_ui=Mock(),
    )
    session.app._streaming_session = session
    from tests.test_tutorial_dictation_489 import compile_method, method
    session.app.recording = False
    session.app._on_streaming_session_finished = compile_method(method(
        'dictation.py', '_on_streaming_session_finished')).__get__(session.app)
    session._overlay.flash_done_and_fade.side_effect = lambda finish: finish()
    return session


@pytest.mark.parametrize('parking', [False, True], ids=['Compact', 'Full'])
def test_cancel_after_final_is_queued(parking):
    session = session_stub(parking)
    session.on_final('synthetic sample')
    queued = session.app._schedule_ui.call_args
    session.cancel()
    callback, *args = queued.args
    callback(*args)
    session.app._paste_preserving_clipboard.assert_not_called()
    session.app.add_to_history.assert_not_called()
    session.app._log_history.assert_not_called()
    assert session._state != session.STATE_PASTING


def test_deferred_direct_final_has_no_side_effect_after_cancel(monkeypatch):
    session = session_stub(direct=True)
    session.cancel()
    keys, release, clipboard = Mock(), Mock(), Mock()
    monkeypatch.setattr(streaming, '_release_held_modifiers', release)
    monkeypatch.setattr(streaming, 'pyautogui', SimpleNamespace(hotkey=keys))
    monkeypatch.setattr(streaming, 'pyperclip', SimpleNamespace(copy=clipboard))
    monkeypatch.setattr(streaming, 'time', SimpleNamespace(sleep=lambda _: None))
    session._direct_paste_final('synthetic sample')
    for effect in (keys, release, clipboard, session.app._paste_preserving_clipboard):
        effect.assert_not_called()


def test_deferred_retry_does_not_paste_or_copy_after_cancel(monkeypatch):
    session = session_stub()
    session.cancel_event.set()
    clipboard = Mock()
    monkeypatch.setattr(streaming, 'pyperclip', SimpleNamespace(copy=clipboard))
    session._paste_retry_then_clipboard('synthetic sample')
    session.app._paste_preserving_clipboard.assert_not_called()
    clipboard.assert_not_called()


def test_direct_worker_keeps_history_and_pastes_after_display_finishes(monkeypatch):
    session = session_stub(direct=True)
    jobs = []
    monkeypatch.setattr(streaming.thread_registry, 'spawn',
                        lambda name, fn, **kw: jobs.append((fn, kw)))
    session._deliver_final('synthetic sample', None, 1, 0)
    session.app._log_history.assert_called_once()
    assert session.app._streaming_session is None
    assert session._state == session.STATE_DONE
    monkeypatch.setattr(streaming, '_release_held_modifiers', Mock())
    monkeypatch.setattr(streaming, 'time', SimpleNamespace(sleep=lambda _: None))
    def paste(text, before_paste):
        assert before_paste() is True
        return True
    session.app._paste_preserving_clipboard.side_effect = paste
    callback, keywords = jobs[0]
    callback(*keywords['args'])
    session.app._paste_preserving_clipboard.assert_called_once()
    session.app._log_history.assert_called_once()


def test_cancelled_deferred_worker_leaves_partial_and_final_recoverable(monkeypatch):
    session = session_stub(direct=True)
    jobs = []
    monkeypatch.setattr(streaming.thread_registry, 'spawn',
                        lambda name, fn, **kw: jobs.append((fn, kw)))
    session._deliver_final('synthetic sample', None, 1, 0)
    session.cancel_event.set()  # cancellation while the deferred work is pending
    session._last_pasted = True
    keys = Mock()
    monkeypatch.setattr(streaming, 'pyautogui', SimpleNamespace(hotkey=keys))
    callback, keywords = jobs[0]
    callback(*keywords['args'])
    keys.assert_not_called()
    session.app._paste_preserving_clipboard.assert_not_called()
    session.app._log_history.assert_called_once()  # saved before dispatch


def test_direct_paste_failure_after_undo_keeps_final_in_history(monkeypatch):
    session = session_stub(direct=True)
    jobs, effects = [], []
    monkeypatch.setattr(streaming.thread_registry, 'spawn',
                        lambda name, fn, **kw: jobs.append((fn, kw)))
    session.app._log_history.side_effect = lambda **kw: effects.append('history')
    session._deliver_final('synthetic sample', None, 1, 0)
    session._last_pasted = True
    monkeypatch.setattr(streaming, '_release_held_modifiers', Mock())
    monkeypatch.setattr(streaming, 'time', SimpleNamespace(sleep=lambda _: None))
    monkeypatch.setattr(streaming, 'pyautogui',
                        SimpleNamespace(hotkey=lambda *keys: effects.append('undo')))
    monkeypatch.setattr(streaming, 'pyperclip', SimpleNamespace(copy=Mock()))
    session.app._paste_preserving_clipboard.side_effect = RuntimeError('synthetic failure')
    callback, keywords = jobs[0]
    callback(*keywords['args'])
    assert effects == ['history', 'undo']
    session.app._log_history.assert_called_once()
