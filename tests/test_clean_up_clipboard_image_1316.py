"""The explicit selection command preserves the clipboard or asks first."""
from types import SimpleNamespace

import pytest

from samsara import ava_pending, clipboard as cb, teach_patterns as tp
from samsara.ava_edit import actions, session
from tests.test_clean_up_selection_632 import SOURCE, app, rig  # noqa: F401
from tests.test_clean_up_selection_real_apps_1287 import _focus_api


@pytest.fixture
def clipboard_backend(app, rig, monkeypatch):
    _focus_api(monkeypatch, "Scintilla", rig)
    backend = SimpleNamespace(
        formats=[cb.CF_UNICODETEXT],
        contents={cb.CF_UNICODETEXT: b"original unicode"},
        snapshot={cb.CF_UNICODETEXT: b"original unicode"},
        owner=201, restored=[], enum_error=0,
    )

    def enumerate_format(previous):
        available = iter(backend.formats)
        if previous:
            for fmt in available:
                if fmt == previous:
                    break
        return next(available, 0)

    def save():
        snapshot = cb.ClipboardSnapshot(backend.snapshot)
        snapshot.source_seq = rig.seq
        snapshot.fake_text = rig.text
        snapshot.lost = getattr(backend, "lost", {}).copy()
        return snapshot

    def restore(snapshot):
        backend.restored.append(snapshot)
        backend.contents = dict(snapshot)
        rig.text = snapshot.fake_text
        rig.seq += 1
        snapshot.restore_status = "put_back"
        return snapshot.complete

    app.listening_indicator = SimpleNamespace(
        show_outcome=lambda label, *_: app.chips.append((label,)))
    app._schedule_ui = lambda callback, *args: callback(*args)
    monkeypatch.setattr(cb._user32, "EnumClipboardFormats", enumerate_format, raising=False)
    monkeypatch.setattr(cb._user32, "GetClipboardOwner", lambda: backend.owner, raising=False)
    monkeypatch.setattr(cb._user32, "CloseClipboard", lambda: None, raising=False)
    monkeypatch.setattr(cb, "_kernel32", SimpleNamespace(GetLastError=lambda: backend.enum_error))
    monkeypatch.setattr(cb, "_open_clipboard_with_retry", lambda: True)
    monkeypatch.setattr(cb, "save_clipboard", save)
    monkeypatch.setattr(cb, "restore_clipboard", restore)
    monkeypatch.setattr(tp, "_selection_clipboard_inventory", _clipboard_inventory)
    return backend


_clipboard_inventory = tp._selection_clipboard_inventory


def _add_unsaved(clipboard_backend, fmt):
    clipboard_backend.formats.append(fmt)
    clipboard_backend.contents[fmt] = b"\x00\x0f\x81\xfe"


@pytest.mark.parametrize("readable_image", [False, True])
def test_complete_snapshot_proceeds(app, rig, clipboard_backend, readable_image):
    if readable_image:
        _add_unsaved(clipboard_backend, cb.CF_DIB)
        clipboard_backend.snapshot = clipboard_backend.contents.copy()
    original = clipboard_backend.contents.copy()
    assert session.propose_and_stage(app, "clean up selection")
    assert session.pending().proposal.original == SOURCE
    assert rig.keys == [("ctrl", "c")]
    assert clipboard_backend.contents == original
    assert len(clipboard_backend.restored) == 1


@pytest.mark.parametrize("fmt,name,word", [
    (cb.CF_DIBV5, None, "picture"),
    (0xC123, "PNG", "picture"),
    (cb.CF_HDROP, None, "what you copied"),
    (0xC124, "Custom", "what you copied"),
])
def test_missing_format_asks_and_cancel_preserves_bytes(
        app, rig, clipboard_backend, monkeypatch, fmt, name, word):
    _add_unsaved(clipboard_backend, fmt)
    if name is not None:
        def format_name(_fmt, buffer, _size):
            buffer.value = name
            return len(name)
        monkeypatch.setattr(cb._user32, "GetClipboardFormatNameW", format_name, raising=False)
    original = clipboard_backend.contents.copy()
    assert session.handle_utterance(app, "clean up selection")
    question = app._ava_question
    assert question.opened and word in question.text
    assert "clean up anyway" in app.chips[-1][0]
    assert not rig.keys and not clipboard_backend.restored and rig.seq == 10
    assert session.pending() is None
    assert ava_pending.consume(app, "cancel")
    assert clipboard_backend.contents == original
    assert not rig.keys and not clipboard_backend.restored and rig.seq == 10


def test_explicit_anyway_retries_once_and_restores_saved_formats(app, rig, clipboard_backend):
    _add_unsaved(clipboard_backend, cb.CF_DIB)
    clipboard_backend.lost = {cb.CF_DIB: "unavailable"}
    assert session.propose_and_stage(app, "clean up selection")
    assert not rig.keys
    assert ava_pending.consume(app, "Clean up anyway.")
    assert session.pending().proposal.original == SOURCE
    assert rig.keys == [("ctrl", "c")]
    assert clipboard_backend.contents == clipboard_backend.snapshot
    assert not ava_pending.consume(app, "clean up anyway")


@pytest.mark.parametrize("change", ["sequence", "owner", "format", "focus", "process", "edit_epoch", "yes"])
def test_consent_does_not_transfer(app, rig, clipboard_backend, change):
    _add_unsaved(clipboard_backend, cb.CF_DIB)
    assert session.propose_and_stage(app, "clean up selection")
    if change == "sequence":
        rig.seq += 1
    elif change == "owner":
        clipboard_backend.owner += 1
    elif change == "format":
        _add_unsaved(clipboard_backend, cb.CF_DIBV5)
    elif change == "focus":
        rig.target = (301, 302, 303)
    elif change == "process":
        rig.pid += 1
    elif change == "edit_epoch":
        app._ava_edit_epoch = 1
    assert ava_pending.consume(app, "yes" if change == "yes" else "anyway")
    assert not rig.keys and not clipboard_backend.restored
    assert session.pending() is None


@pytest.mark.parametrize("approved", [False, True])
def test_restore_failure_is_reported_without_staging(app, rig, clipboard_backend, monkeypatch, approved):
    def partial_restore(snapshot):
        clipboard_backend.contents = {}
        snapshot.restore_status = "failed" if approved else "put_back"
        return False

    monkeypatch.setattr(cb, "restore_clipboard", partial_restore)
    if approved:
        _add_unsaved(clipboard_backend, cb.CF_DIB)
        assert session.propose_and_stage(app, "clean up selection")
        assert ava_pending.consume(app, "anyway")
    else:
        assert not session.propose_and_stage(app, "clean up selection")
    assert rig.keys == [("ctrl", "c")]
    assert session.pending() is None
    assert "Clipboard restore failed" in app.chips[-1][0]
    assert "couldn't be fully restored" in app.spoken[-1]


def test_failed_enumeration_refuses_even_with_complete_snapshot(app, rig, clipboard_backend):
    clipboard_backend.enum_error = 5
    assert not session.propose_and_stage(app, "clean up selection")
    assert "Clipboard check failed" in app.chips[-1][0]
    assert "couldn't be checked" in app.spoken[-1]
    assert not rig.keys


def test_router_capture_cannot_bypass_confirmation(app, rig, clipboard_backend):
    _add_unsaved(clipboard_backend, cb.CF_DIB)
    ingress = actions.ingress_payload(app, "clean up this passage")
    assert not rig.keys
    request = actions.from_action(app, {"target": "selection", "instruction": "clean up"},
                                  snapshot=ingress.snapshot, original_utterance=ingress.utterance)
    assert actions.propose_and_stage(app, request)
    assert app._ava_question.opened
    assert ava_pending.consume(app, "anyway")
    assert session.pending().proposal.original == SOURCE
    assert rig.keys == [("ctrl", "c")]
