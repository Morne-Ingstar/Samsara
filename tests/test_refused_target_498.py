"""498: target refusal is terminal and fallback rechecks window/control."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import clipboard as cb, paste
from tests.test_clipboard_settle_474 import transport, Editor, _REAL_MESSAGE_PASTE


class ChangingEditor(Editor):
    def __init__(self, clock, *, change, **kwargs):
        super().__init__(clock, **kwargs)
        self.foreground = 123
        self.change_target = change

    def GetForegroundWindow(self):
        return self.foreground

    def move_target(self):
        if self.change_target == 'foreground':
            self.foreground = 999
        else:
            self.focus = 999

    def SendMessageTimeoutW(self, *args):
        result = super().SendMessageTimeoutW(*args)
        if args[1] == 2006 and 2179 not in self.messages:
            self.move_target()
        return result


@pytest.mark.parametrize('change', ['foreground', 'focus'])
def test_focus_change_during_message_probe_must_not_paste_into_new_window(transport, monkeypatch, change):
    editor = ChangingEditor(transport.clock, change=change)
    monkeypatch.setattr(cb, '_user32', editor)
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', _REAL_MESSAGE_PASTE)
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda hwnd=None: SimpleNamespace(confirmed_ok=True))
    receipt, fallback = Mock(), Mock()
    assert not cb.paste_with_preservation('synthetic sentence', delivery_observer=receipt,
                                         incomplete_snapshot_fallback=fallback)
    assert editor.messages == [2006]
    transport.hotkey.assert_not_called()
    receipt.assert_not_called()
    fallback.assert_not_called()
    transport.restore.assert_called_once_with(transport.saved)


@pytest.mark.parametrize('change', ['foreground', 'focus'])
@pytest.mark.parametrize('label', ['Edit', 'Scintilla'])
def test_target_change_after_probe_before_keystroke_is_not_sent(transport, monkeypatch, change, label):
    editor = ChangingEditor(transport.clock, change=change, label=label)
    monkeypatch.setattr(cb, '_user32', editor)
    def probe():
        editor.move_target()
        return cb.DeliveryOutcome.SENT if label == 'Edit' else cb.DeliveryOutcome.UNVERIFIED
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', probe)
    assert not cb.paste_with_preservation('synthetic sentence')
    transport.hotkey.assert_not_called()
    transport.restore.assert_called_once_with(transport.saved)


@pytest.mark.parametrize('label,keys', [('Edit', 1), ('Scintilla', 0)])
def test_unchanged_target_preserves_ordinary_and_confirmed_delivery(transport, monkeypatch, label, keys):
    editor = Editor(transport.clock, label=label)
    monkeypatch.setattr(cb, '_user32', editor)
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', _REAL_MESSAGE_PASTE)
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda hwnd=None: SimpleNamespace(confirmed_ok=True))
    assert cb.paste_with_preservation('synthetic sentence')
    assert transport.hotkey.call_count == keys
    transport.restore.assert_called_once_with(transport.saved)


def test_unavailable_focus_information_still_allows_ordinary_fallback(transport, monkeypatch):
    editor = Editor(transport.clock, label='Edit')
    monkeypatch.setattr(editor, 'GetGUIThreadInfo', lambda *args: False)
    monkeypatch.setattr(cb, '_user32', editor)
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', _REAL_MESSAGE_PASTE)
    assert cb.paste_with_preservation('synthetic sentence')
    transport.hotkey.assert_called_once_with('ctrl', 'v')
