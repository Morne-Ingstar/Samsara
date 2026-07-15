"""Safety regressions for the single-level dictated-text undo command."""

from unittest.mock import Mock

import pytest

import dictation


def _undo_app(*, text="dictated text", target_hwnd=101):
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app._last_dictation_text = text
    app._last_dictation_length = len(text) if text else 0
    app._last_dictation_hwnd = target_hwnd
    app._undo_timer = Mock()
    app.play_sound = Mock()
    return app


def _paste_app():
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.config = {"clipboard_delay": 0.23}
    app.adaptive_learner = Mock()
    app._record_undoable_paste = Mock()
    return app


def test_matching_foreground_uses_only_native_ctrl_z(monkeypatch):
    app = _undo_app(text="a fairly long dictated sentence", target_hwnd=700)
    hotkey = Mock()
    press = Mock()
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", Mock(return_value=700))
    monkeypatch.setattr(dictation.pyautogui, "hotkey", hotkey)
    monkeypatch.setattr(dictation.pyautogui, "press", press)

    assert app.undo_last_dictation() is True

    hotkey.assert_called_once_with("ctrl", "z")
    press.assert_not_called()
    app.play_sound.assert_called_once_with("success")
    assert app._last_dictation_text is None
    assert app._last_dictation_length == 0
    assert app._last_dictation_hwnd is None
    assert app._undo_timer is None


def test_successful_undo_is_one_shot(monkeypatch):
    app = _undo_app(target_hwnd=700)
    hotkey = Mock()
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", Mock(return_value=700))
    monkeypatch.setattr(dictation.pyautogui, "hotkey", hotkey)

    assert app.undo_last_dictation() is True
    assert app.undo_last_dictation() is False

    hotkey.assert_called_once_with("ctrl", "z")
    assert app.play_sound.call_args_list[-1].args == ("error",)


def test_mismatched_foreground_fails_closed_and_retains_retry(monkeypatch):
    app = _undo_app(text="keep me", target_hwnd=111)
    timer = app._undo_timer
    hotkey = Mock()
    press = Mock()
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", Mock(return_value=222))
    monkeypatch.setattr(dictation.pyautogui, "hotkey", hotkey)
    monkeypatch.setattr(dictation.pyautogui, "press", press)

    assert app.undo_last_dictation() is False

    hotkey.assert_not_called()
    press.assert_not_called()
    app.play_sound.assert_called_once_with("error")
    assert app._last_dictation_text == "keep me"
    assert app._last_dictation_length == len("keep me")
    assert app._last_dictation_hwnd == 111
    assert app._undo_timer is timer


@pytest.mark.parametrize(
    ("saved_hwnd", "foreground_hwnd"),
    [
        (None, 111),
        (0, 111),
        (111, None),
        (111, 0),
    ],
)
def test_missing_window_identity_fails_closed(
    monkeypatch, saved_hwnd, foreground_hwnd,
):
    app = _undo_app(target_hwnd=saved_hwnd)
    hotkey = Mock()
    press = Mock()
    monkeypatch.setattr(
        dictation, "_get_foreground_hwnd", Mock(return_value=foreground_hwnd),
    )
    monkeypatch.setattr(dictation.pyautogui, "hotkey", hotkey)
    monkeypatch.setattr(dictation.pyautogui, "press", press)

    assert app.undo_last_dictation() is False

    hotkey.assert_not_called()
    press.assert_not_called()
    app.play_sound.assert_called_once_with("error")


def test_ctrl_z_injection_failure_retains_undo_for_retry(monkeypatch):
    app = _undo_app(text="retry me", target_hwnd=500)
    timer = app._undo_timer
    hotkey = Mock(side_effect=RuntimeError("input injection unavailable"))
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", Mock(return_value=500))
    monkeypatch.setattr(dictation.pyautogui, "hotkey", hotkey)

    assert app.undo_last_dictation() is False

    assert app._last_dictation_text == "retry me"
    assert app._last_dictation_length == len("retry me")
    assert app._last_dictation_hwnd == 500
    assert app._undo_timer is timer
    app.play_sound.assert_called_once_with("error")


def test_record_undoable_paste_captures_foreground_for_compatibility(monkeypatch):
    app = _undo_app(text=None, target_hwnd=None)
    app._arm_undo_timer = Mock()
    foreground = Mock(return_value=808)
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", foreground)

    app._record_undoable_paste("new text")

    assert app._last_dictation_text == "new text"
    assert app._last_dictation_length == len("new text")
    assert app._last_dictation_hwnd == 808
    app._arm_undo_timer.assert_called_once_with()


def test_explicitly_missing_pre_paste_target_stays_fail_closed(monkeypatch):
    app = _undo_app(text=None, target_hwnd=None)
    app._arm_undo_timer = Mock()
    foreground = Mock(return_value=909)
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", foreground)

    app._record_undoable_paste("new text", target_hwnd=None)

    assert app._last_dictation_hwnd is None
    foreground.assert_not_called()


def test_paste_records_window_captured_immediately_before_ctrl_v(monkeypatch):
    app = _paste_app()
    target = Mock(return_value=4242)
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", target)

    def central_paste(text, *, paste_delay, restore_delay, before_paste):
        assert text == "delivered text"
        assert before_paste() is True
        return True

    monkeypatch.setattr(dictation, "paste_with_preservation", central_paste)

    assert app._paste_preserving_clipboard("delivered text") is True

    app._record_undoable_paste.assert_called_once_with(
        "delivered text", target_hwnd=4242,
    )


def test_rejected_pre_paste_guard_does_not_capture_or_record_window(monkeypatch):
    app = _paste_app()
    foreground = Mock(return_value=999)
    caller_guard = Mock(return_value=False)
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", foreground)

    def central_paste(text, *, paste_delay, restore_delay, before_paste):
        return bool(before_paste())

    monkeypatch.setattr(dictation, "paste_with_preservation", central_paste)

    assert app._paste_preserving_clipboard(
        "not delivered", before_paste=caller_guard,
    ) is False

    caller_guard.assert_called_once_with()
    foreground.assert_not_called()
    app._record_undoable_paste.assert_not_called()
