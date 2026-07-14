"""DictationApp must use the sequence-guarded central clipboard path."""
from types import SimpleNamespace
from unittest.mock import Mock

import dictation


def _app():
    app = SimpleNamespace(
        config={"clipboard_delay": 0.23},
        adaptive_learner=SimpleNamespace(record_transcription=Mock()),
        _record_undoable_paste=Mock(),
    )
    app._paste_preserving_clipboard = (
        dictation.DictationApp._paste_preserving_clipboard.__get__(app)
    )
    return app


def test_success_delegates_to_sequence_guarded_path(monkeypatch):
    app = _app()
    central = Mock(return_value=True)
    monkeypatch.setattr(dictation, "paste_with_preservation", central)

    assert app._paste_preserving_clipboard("dictated text") is True

    central.assert_called_once_with(
        "dictated text",
        paste_delay=dictation.CLIPBOARD_PASTE_DELAY,
        restore_delay=0.23,
    )
    app._record_undoable_paste.assert_called_once_with("dictated text")
    app.adaptive_learner.record_transcription.assert_called_once_with("dictated text")


def test_failed_central_paste_does_not_record_success(monkeypatch):
    app = _app()
    monkeypatch.setattr(
        dictation, "paste_with_preservation", Mock(return_value=False),
    )

    assert app._paste_preserving_clipboard("retained text") is False

    app._record_undoable_paste.assert_not_called()
    app.adaptive_learner.record_transcription.assert_not_called()
