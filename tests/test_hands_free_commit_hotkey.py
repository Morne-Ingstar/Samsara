from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dictation import DictationApp
from samsara.session_modes import (
    DispatchOutcome,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
)


def _manager(*, process="codex.exe", hwnd=4242, inject=None):
    inject = inject or Mock(return_value="Complete thought.")
    manager = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=lambda: process,
        foreground_hwnd_resolver=lambda: hwnd,
        inject_fn=inject,
        remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(),
        agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True,
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    return manager, inject


def _stage(manager, text="complete thought"):
    outcome = manager.dispatch_utterance(
        text,
        UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.1,)),
    )
    assert outcome.kind == "dictate_staged"


def _app(manager, dictate_preview=None):
    app = DictationApp.__new__(DictationApp)
    app.config = {
        "command_mode": {"mode": "toggle"},
        "dictate_commit_hotkey": "ctrl+space",
    }
    app.command_mode_active = True
    app._session_mode_manager = manager
    app._handle_session_dispatch_outcome = Mock()
    app.play_sound = Mock()
    app._dictate_preview = dictate_preview
    return app


def _hotkey_app(*, active=True, pending="thought", mode="hold"):
    manager = Mock()
    manager.mode = SessionMode.DICTATE
    manager.buffer_dictate_until_commit = True
    manager.dictate_pending_buffer = pending

    app = _app(manager)
    app.command_mode_active = active
    app.config.update({
        "mode": mode,
        "hotkey": "ctrl+shift",
        "continuous_hotkey": "ctrl+alt+d",
        "wake_word_hotkey": "ctrl+alt+w",
        "command_hotkey": "ctrl+alt+c",
        "cancel_hotkey": "escape",
        "continuous_commit_trigger": "key",
        "continuous_commit_hotkey": "ctrl+space",
    })
    app.current_keys = set()
    app.key_press_times = {}
    app.hotkey_pressed = False
    app.recording = False
    app.snoozed = False
    app._stop_in_flight = False
    app.command_mode_recording = False
    app.continuous_active = mode == "continuous"
    app._continuous_consumer = Mock()
    app.get_key_name = Mock(return_value="space")
    app._check_command_mode_key = Mock()
    app.check_hotkey_state = Mock(side_effect=lambda combo: combo == "ctrl+space")
    return app


def test_active_commit_hotkey_is_edge_triggered_and_spawns_no_recording():
    app = _hotkey_app()
    spawned = []
    with patch(
        "dictation.thread_registry.spawn",
        side_effect=lambda name, target, **kwargs: spawned.append((name, target)),
    ):
        app.on_key_press("space")
        app.on_key_press("space")

    assert [name for name, _target in spawned] == [
        "dictation.commit_pending_hands_free"
    ]
    assert app.hotkey_pressed is True
    assert app.recording is False
    app._continuous_consumer.commit_now.assert_not_called()


@pytest.mark.parametrize("active,pending", [(False, "thought"), (True, "")])
def test_commit_hotkey_is_inert_without_active_session_and_pending_text(
    active, pending
):
    app = _hotkey_app(active=active, pending=pending)
    with patch("dictation.thread_registry.spawn") as spawn:
        app.on_key_press("space")
    spawn.assert_not_called()
    assert app.hotkey_pressed is False
    assert app.recording is False


def test_same_default_remains_continuous_manual_commit_when_sessions_exclusive():
    app = _hotkey_app(active=False, mode="continuous")
    app.on_key_press("space")
    app._continuous_consumer.commit_now.assert_called_once_with()
    app._session_mode_manager.commit_pending_dictation.assert_not_called()


def test_worker_commits_once_stays_in_dictate_and_routes_success_outcome():
    manager, inject = _manager()
    _stage(manager)
    app = _app(manager)

    outcome = app._commit_pending_hands_free_dictation()

    assert outcome.kind == "dictate_committed"
    assert manager.mode is SessionMode.DICTATE
    assert manager.dictate_pending_buffer == ""
    inject.assert_called_once()
    app._handle_session_dispatch_outcome.assert_called_once_with(outcome, "")


def test_worker_retains_buffer_and_routes_focus_failure_without_paste():
    manager, inject = _manager(process=None, hwnd=None)
    _stage(manager)
    app = _app(manager)

    outcome = app._commit_pending_hands_free_dictation()

    assert outcome.kind == "dictate_commit_blocked_focus_lock"
    assert manager.dictate_pending_buffer
    inject.assert_not_called()
    app._handle_session_dispatch_outcome.assert_called_once_with(outcome, "")


# ---------------------------------------------------------------------------
# Preview overlay notification (5666457/2072374 gap): the keyboard commit
# path never called on_utterance_final at all, so a keyboard-committed
# buffer left stale lines in the DICTATE preview overlay -- the voice path
# (dictation.py's _handle_command_mode_utterance) got this signal via
# 2072374, this path did not. Reuses the SAME on_utterance_final signal
# shape (dictate_committed=True/False), never a second clearing mechanism.
# ---------------------------------------------------------------------------

def test_successful_keyboard_commit_notifies_preview_and_clears():
    manager, _inject = _manager()
    _stage(manager)
    preview = Mock()
    app = _app(manager, dictate_preview=preview)

    outcome = app._commit_pending_hands_free_dictation()

    assert outcome.kind == "dictate_committed"
    preview.on_utterance_final.assert_called_once_with("", dictate_committed=True)


def test_failed_keyboard_commit_notifies_preview_without_clearing():
    manager, _inject = _manager(process=None, hwnd=None)
    _stage(manager)
    preview = Mock()
    app = _app(manager, dictate_preview=preview)

    outcome = app._commit_pending_hands_free_dictation()

    assert outcome.kind == "dictate_commit_blocked_focus_lock"
    preview.on_utterance_final.assert_called_once_with("", dictate_committed=False)


def test_no_preview_running_is_safe():
    manager, _inject = _manager()
    _stage(manager)
    app = _app(manager, dictate_preview=None)

    outcome = app._commit_pending_hands_free_dictation()  # must not raise

    assert outcome.kind == "dictate_committed"


def test_preview_notification_failure_does_not_break_the_commit():
    """Best-effort, same as the voice path's own on_utterance_final guard --
    a raising preview must never be mistaken for the commit itself having
    failed (which would incorrectly play the error earcon)."""
    manager, _inject = _manager()
    _stage(manager)
    preview = Mock()
    preview.on_utterance_final.side_effect = RuntimeError("boom")
    app = _app(manager, dictate_preview=preview)

    outcome = app._commit_pending_hands_free_dictation()  # must not raise

    assert outcome.kind == "dictate_committed"
    app._handle_session_dispatch_outcome.assert_called_once_with(outcome, "")
