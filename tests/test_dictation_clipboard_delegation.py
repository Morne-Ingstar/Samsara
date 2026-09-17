"""DictationApp must use the sequence-guarded central clipboard path."""
from types import SimpleNamespace
from unittest.mock import ANY, Mock

import dictation

import pytest


@pytest.fixture(autouse=True)
def _no_real_sendinput(monkeypatch):
    """type_text_unicode now genuinely injects keystrokes (25bc7ee fixed the
    INPUT struct), so every test here must stub it: (a) so pytest never types
    into the developer's desktop, (b) so these tests keep exercising the
    clipboard-paste path they were written for."""
    import dictation
    monkeypatch.setattr(dictation, 'type_text_unicode', lambda text: False)



def _app():
    app = SimpleNamespace(
        config={"clipboard_delay": 0.23},
        adaptive_learner=SimpleNamespace(record_transcription=Mock()),
        _record_undoable_paste=Mock(),
        # per-target routing (2026-08-02): these tests exercise the
        # clipboard path, so the stub app reports a non-browser target
        _foreground_wants_typed_injection=lambda: False,
        # flight-recorder seam (P1): recording-only helper the stub must carry
        _flight_foreground_process_name=lambda: None,
    )
    app._paste_preserving_clipboard = (
        dictation.DictationApp._paste_preserving_clipboard.__get__(app)
    )
    return app


def test_shortcut_only_delivery_is_not_recorded_as_undoable(monkeypatch):
    app = _app()
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", lambda: 4242)

    def _central(*_args, before_paste=None, **_kwargs):
        assert before_paste is not None
        assert before_paste() is True
        return True

    central = Mock(side_effect=_central)
    monkeypatch.setattr(dictation, "paste_with_preservation", central)

    assert app._paste_preserving_clipboard("dictated text") is True

    central.assert_called_once_with(
        "dictated text",
        paste_delay=dictation.CLIPBOARD_PASTE_DELAY,
        restore_delay=0.23,
        before_paste=ANY,
        incomplete_snapshot_fallback=ANY,
    )
    app._record_undoable_paste.assert_not_called()
    app.adaptive_learner.record_transcription.assert_not_called()


def test_session_call_gets_explicit_unconfirmed_shortcut_result(monkeypatch):
    app = _app()
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", lambda: 4242)
    monkeypatch.setattr(dictation, "paste_with_preservation", Mock(return_value=True))

    assert app._paste_preserving_clipboard(
        "dictated text", return_delivery_confirmation=True,
    ) == (True, False)


def test_failed_central_paste_does_not_record_success(monkeypatch):
    app = _app()
    monkeypatch.setattr(
        dictation, "paste_with_preservation", Mock(return_value=False),
    )

    assert app._paste_preserving_clipboard("retained text") is False

    app._record_undoable_paste.assert_not_called()
    app.adaptive_learner.record_transcription.assert_not_called()


def test_incomplete_clipboard_snapshot_uses_typed_delivery_only_for_typed_targets(monkeypatch):
    app = _app()
    app._foreground_wants_typed_injection = lambda: True
    monkeypatch.setattr(dictation, "_get_foreground_hwnd", lambda: 4242)
    monkeypatch.setattr(dictation, "type_text_unicode", lambda text: text == "retained text")

    def _central(*_args, incomplete_snapshot_fallback=None, **_kwargs):
        assert incomplete_snapshot_fallback is not None
        return incomplete_snapshot_fallback()

    monkeypatch.setattr(dictation, "paste_with_preservation", _central)

    assert app._paste_preserving_clipboard("retained text") is True
    app._record_undoable_paste.assert_called_once_with("retained text", target_hwnd=4242)
