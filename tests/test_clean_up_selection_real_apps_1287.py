"""1287: selection capture, bound Apply and whole-command regressions."""
from types import SimpleNamespace
import sys

import pytest

from samsara import clipboard as cb, injection_safety, teach_patterns as tp
from samsara.ava_edit import actions, intent, selection, session
from tests.test_clean_up_selection_632 import RESULT, SOURCE, app, executor, rig  # noqa: F401

_native_selection = selection._native_selection


def _focus_api(monkeypatch, label, rig):
    def class_name(_handle, buffer, _size):
        buffer.value = label
        return len(label)

    def window_pid(_handle, pid):
        pid._obj.value = rig.pid
        return rig.target[1]

    rig.pid = 23
    monkeypatch.setattr(cb, "_user32", SimpleNamespace(
        GetClassNameW=class_name, GetWindowThreadProcessId=window_pid))
    monkeypatch.setattr(injection_safety, "window_integrity",
                        lambda _hwnd: SimpleNamespace(confirmed_ok=True))
    monkeypatch.setattr(selection, "_native_selection", _native_selection)


@pytest.mark.parametrize("window_class,child", [
    ("RichEditD2DPT", 103), ("Chrome_RenderWidgetHostHWND", 103),
    ("_WwG", 103), ("Scintilla", 103), ("Chrome_RenderWidgetHostHWND", 0),
])
def test_explicit_pipeline_copies_once_and_pastes_in_same_process(
        app, rig, monkeypatch, window_class, child):
    rig.target = (101, 102, child)
    _focus_api(monkeypatch, window_class, rig)
    assert session.handle_utterance(app, "clean up this paragraph, please.")
    staged = session.pending()
    assert staged.proposal.original == SOURCE
    assert staged.bound_target.selection.clipboard_process == 23
    assert rig.text == "original clipboard"
    assert rig.keys == [("ctrl", "c")]

    rig.target = (101, 102, 415)  # Chromium and Word can replace their child.
    monkeypatch.setattr(selection, "capture_result", lambda *_:
                        pytest.fail("active apply must not ask the passive adapter"))
    assert session.apply(app)["applied"]
    assert rig.keys == [("ctrl", "c"), ("ctrl", "v")]
    assert rig.text == "original clipboard" and rig.pasted == [RESULT]


@pytest.mark.parametrize("change", ["hwnd", "process"])
def test_active_apply_refuses_foreground_change_and_keeps_proposal(app, rig, monkeypatch, change):
    _focus_api(monkeypatch, "_WwG", rig)
    assert session.propose_and_stage(app, "clean up selection")
    staged = session.pending()
    if change == "hwnd":
        rig.target = (400, 102, 103)
    else:
        rig.pid += 1
    assert not session.apply(app)["applied"]
    assert session.pending() is staged
    assert rig.keys == [("ctrl", "c")]
    assert "original selection" in app.spoken[-1]
    assert "Return to the selection" in app.chips[-1][0]


def test_keyboard_waits_for_release_but_stops_at_deadline(rig, monkeypatch):
    def held_until(deadline):
        return lambda: rig.clock < deadline

    monkeypatch.setattr(tp, "_copy_modifiers_down", held_until(0.12))
    assert tp.grab_selection_block() == SOURCE
    assert rig.clock >= 0.12
    assert rig.keys == [("ctrl", "c")]

    monkeypatch.setattr(tp, "_copy_modifiers_down", held_until(10))
    start = rig.clock
    assert tp.grab_selection_block() is None
    assert 0.6 <= rig.clock - start <= 0.62
    assert rig.keys == [("ctrl", "c")]


def test_partial_image_restores_text_but_unknown_snapshot_keeps_clipboard(rig, monkeypatch, caplog):
    saved = cb.ClipboardSnapshot({cb.CF_UNICODETEXT: b"known text"})
    saved.lost[cb.CF_DIB] = "delayed image unavailable"
    saved.fake_text = rig.text
    saved.source_seq = rig.seq

    def partial_restore(snapshot):
        rig.text = snapshot.fake_text
        snapshot.restore_status = "put_back"
        return False

    monkeypatch.setattr(cb, "save_clipboard", lambda: saved)
    monkeypatch.setattr(cb, "restore_clipboard", partial_restore)
    assert tp.grab_selection_block_result().state == "selected"
    assert rig.text == "original clipboard"
    assert "image can only be restored in part" in caplog.text
    saved.lost[0] = "clipboard owner timed out"
    assert tp.grab_selection_block() is None
    assert rig.keys == [("ctrl", "c")]


def test_richedit_reads_delayed_unicode_after_image_backup(app, rig, monkeypatch):
    _focus_api(monkeypatch, "RichEditD2DPT", rig)
    saved = cb.ClipboardSnapshot({cb.CF_DIB: b"fixture bitmap", cb.CF_DIBV5: b"fixture bitmap v5"})
    saved.fake_text = ""
    saved.source_seq = rig.seq
    rig.text = ""
    rich_edit_offers_text = False
    reads = []
    monkeypatch.setattr(cb, "save_clipboard", lambda: saved)
    monkeypatch.setattr(cb._user32, "IsClipboardFormatAvailable",
                        lambda fmt: fmt == cb.CF_UNICODETEXT and rich_edit_offers_text,
                        raising=False)

    def hotkey(*args):
        nonlocal rich_edit_offers_text
        rig.keys.append(args)
        rich_edit_offers_text = True  # RichEdit advertises Unicode before rendering it.

    def read():
        assert rich_edit_offers_text
        assert rig.seq == saved.source_seq  # Polling alone cannot observe this copy.
        reads.append(True)
        rig.seq += 1  # GetClipboardData renders the text and advances the sequence.
        return SOURCE

    monkeypatch.setattr(sys.modules["pyautogui"], "hotkey", hotkey)
    monkeypatch.setattr(sys.modules["pyperclip"], "paste", read)
    assert session.propose_and_stage(app, "clean up selection")
    assert session.pending().proposal.original == SOURCE
    assert reads == [True] and rig.keys == [("ctrl", "c")]
    assert rig.restored == [saved]
    assert saved.seq == saved.source_seq and saved.pasted_text == SOURCE
    assert saved[cb.CF_DIB] == b"fixture bitmap"


@pytest.mark.parametrize("already_has_text", [False, True])
def test_grab_does_not_read_a_noop_copy(rig, monkeypatch, caplog, already_has_text):
    saved = cb.ClipboardSnapshot({cb.CF_UNICODETEXT: b"fixture text"} if already_has_text
                                 else {cb.CF_DIB: b"fixture bitmap"})
    saved.source_seq = rig.seq
    rig.copy = False
    monkeypatch.setattr(cb, "save_clipboard", lambda: saved)
    # Even a misleading first availability sample cannot turn known
    # pre-copy text into fresh text after a no-op Ctrl+C.
    monkeypatch.setattr(cb, "_user32", SimpleNamespace(
        IsClipboardFormatAvailable=lambda fmt: fmt == cb.CF_UNICODETEXT
        and already_has_text and bool(rig.keys)))
    monkeypatch.setattr(sys.modules["pyperclip"], "paste",
                        lambda: pytest.fail("Ctrl+C did not produce fresh text"))

    with caplog.at_level("DEBUG", logger=tp.__name__):
        assert tp.grab_selection_block() is None
    assert rig.keys == [("ctrl", "c")] and not rig.restored
    assert "no clipboard update after Ctrl+C" in caplog.text


def test_failed_restore_warns_without_raising(rig, monkeypatch, caplog):
    def fail(_snapshot):
        raise RuntimeError("restore contention")

    monkeypatch.setattr(cb, "restore_clipboard", fail)
    assert tp.grab_selection_block_result().state == "failed"
    assert "did not fully restore" in caplog.text
    assert "restore contention" not in caplog.text


def test_focus_change_while_waiting_prevents_copy(app, rig, monkeypatch):
    _focus_api(monkeypatch, "Scintilla", rig)

    def modifiers():
        if rig.clock < 0.1:
            return True
        rig.target = (3, 4, 5)
        return False

    monkeypatch.setattr(tp, "_copy_modifiers_down", modifiers)
    assert not session.propose_and_stage(app, "clean up selection")
    assert not rig.keys


def test_router_snapshot_reads_explicit_selection_at_ingress_only(app, rig, monkeypatch):
    _focus_api(monkeypatch, "Scintilla", rig)
    passive = actions.ingress_payload(app, "Tell me about the selection tools.")
    assert passive.snapshot.selection.state == "unsupported"
    passive.release()
    payload = actions.ingress_payload(app, "clean up this passage please")
    assert rig.keys == [("ctrl", "c")]
    request = actions.from_action(app, {"target": "selection", "instruction": "clean up"},
                                  snapshot=payload.snapshot, original_utterance=payload.utterance)
    assert session.propose_and_stage(app, request)
    assert session.pending().proposal.original == SOURCE
    assert rig.keys == [("ctrl", "c")]


@pytest.mark.parametrize("phrase", [
    "clean up this paragraph", "clean up this passage thanks", "clean up selection please",
    "fix the highlighted text", "punctuate the highlighted", "tidy this text, please",
])
def test_new_whole_selection_phrases(phrase):
    assert intent.is_edit_request(phrase)
    assert intent.edit_target(phrase) == "selection"
    assert selection.command_for_utterance(phrase) is not None
    assert selection.command_for_utterance(f"she asked me to {phrase}") is None


def test_command_and_ava_keep_same_bare_request(app, rig, executor, monkeypatch):
    seen = []
    monkeypatch.setattr(session, "_propose_off_thread", lambda _app, text: seen.append(text))
    phrase = "clean this up"
    assert selection.command_for_utterance(phrase) == "clean this"
    assert executor.process_text(phrase, app, force_commands=True).claimed
    assert session.handle_utterance(app, phrase)
    assert seen == [phrase, phrase]


def test_all_reply_aliases_share_pending_grammar(app, rig):
    assert session.propose_and_stage(app, "clean up selection")
    assert "discard the edit" in intent.DISCARD_UTTERANCES
    for command, replies in (("apply", intent.APPLY_UTTERANCES), ("discard", intent.DISCARD_UTTERANCES)):
        for reply in replies:
            assert selection.command_for_utterance(reply) == command
            assert selection.command_for_utterance(f"{reply}, please") == command
            assert selection.command_for_utterance(f"she said {reply}") is None


@pytest.mark.parametrize("case,reason", [
    ("nothing", "nothing staged"), ("expired", "the edit expired"),
    ("scope", "the text changed"),
])
def test_apply_reported_refusals_retire_irrecoverable_pending(app, rig, case, reason):
    if case != "nothing":
        assert session.propose_and_stage(app, "clean up selection")
        staged = session.pending()
        if case == "expired":
            staged.created_at -= session.PROPOSAL_TTL_S + 1
        else:
            app._ava_edit_epoch += 1
    assert session.apply(app) == {"applied": False, "reason": reason, "chars": 0}
    assert reason in app.chips[-1][0]
    assert app.spoken[-1] == reason
    assert session.pending() is None
    assert not rig.pasted


def test_native_edit_apply_does_not_copy(app, rig, monkeypatch):
    _focus_api(monkeypatch, "Edit", rig)
    # The Win32 message/UTF-16 range adapter itself is covered in the 1036 gate.
    monkeypatch.setattr(selection, "_native_selection", lambda target:
                        selection_result(target))
    assert session.propose_and_stage(app, "clean up selection")
    assert session.pending().bound_target.selection.clipboard_process is None
    assert session.apply(app)["applied"]
    assert rig.keys == [("ctrl", "v")]


def selection_result(target):
    from samsara.ava_edit.targets import SelectionCapture
    return SelectionCapture("selected", SOURCE, target, (0, len(SOURCE)))



def test_console_without_a_child_does_not_receive_ctrl_c(app, rig, monkeypatch):
    rig.target = (101, 102, 0)
    _focus_api(monkeypatch, "CASCADIA_HOSTING_WINDOW_CLASS", rig)
    assert not session.propose_and_stage(app, "clean up selection")
    assert not rig.keys


def test_newer_clipboard_before_copy_is_not_replaced(rig, monkeypatch):
    saved = cb.ClipboardSnapshot({cb.CF_UNICODETEXT: b"old fixture text"})
    saved.source_seq = rig.seq - 1
    monkeypatch.setattr(cb, "save_clipboard", lambda: saved)
    assert tp.grab_selection_block() is None
    assert not rig.keys and not rig.restored
