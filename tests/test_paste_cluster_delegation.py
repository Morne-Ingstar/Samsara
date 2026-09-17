"""The DictationApp paste/undo surface delegates to samsara.paste."""

from types import SimpleNamespace
from unittest.mock import Mock


def test_paste_cluster_methods_are_thin_delegates(monkeypatch):
    import dictation

    app = SimpleNamespace()
    paste = dictation._paste

    foreground = Mock(return_value="notepad.exe")
    wants_typed = Mock(return_value=False)
    flight_foreground = Mock(return_value=None)
    monkeypatch.setattr(paste, "foreground_process_name", foreground)
    monkeypatch.setattr(paste, "foreground_wants_typed_injection", wants_typed)
    monkeypatch.setattr(paste, "flight_foreground_process_name", flight_foreground)

    assert dictation.DictationApp._foreground_process_name(app) == "notepad.exe"
    assert dictation.DictationApp._foreground_wants_typed_injection(app) is False
    assert dictation.DictationApp._flight_foreground_process_name() is None
    foreground.assert_called_once_with()
    wants_typed.assert_called_once_with(app)
    flight_foreground.assert_called_once_with()

    central = Mock(return_value=(True, False))
    monkeypatch.setattr(paste, "paste_preserving_clipboard", central)
    guard = Mock()
    assert dictation.DictationApp._paste_preserving_clipboard(
        app, "text", before_paste=guard, return_delivery_confirmation=True,
    ) == (True, False)
    central.assert_called_once_with(
        app,
        "text",
        before_paste=guard,
        return_delivery_confirmation=True,
        paste_with_preservation_fn=dictation.paste_with_preservation,
        type_text_unicode_fn=dictation.type_text_unicode,
        get_foreground_hwnd_fn=dictation._get_foreground_hwnd,
    )

    for method_name, helper_name, args in (
        ("_deliver_text_to_focused_editor", "deliver_text_to_focused_editor", ("text",)),
        ("_arm_undo_timer", "arm_undo_timer", ()),
        ("_clear_undo", "clear_undo", ()),
    ):
        helper = Mock(return_value=None)
        monkeypatch.setattr(paste, helper_name, helper)
        assert getattr(dictation.DictationApp, method_name)(app, *args) is None
        helper.assert_called_once_with(app, *args)

    record = Mock(return_value=None)
    monkeypatch.setattr(paste, "record_undoable_paste", record)
    dictation.DictationApp._record_undoable_paste(app, "text")
    record.assert_called_once_with(
        app,
        "text",
        target_hwnd=dictation._UNDO_TARGET_UNSET,
        get_foreground_hwnd_fn=dictation._get_foreground_hwnd,
    )

    undo = Mock(return_value=True)
    monkeypatch.setattr(paste, "undo_last_dictation", undo)
    assert dictation.DictationApp.undo_last_dictation(app) is True
    undo.assert_called_once_with(
        app,
        get_foreground_hwnd_fn=dictation._get_foreground_hwnd,
    )
