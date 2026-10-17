"""Selection edits with fake clipboard, keys, windows, clock, model and speech."""
from types import ModuleType, SimpleNamespace
import sys

import pytest

from samsara import clipboard as cb, teach_patterns as tp
from samsara.ava_edit import intent, model, sanitize, selection, session
from samsara.ava_edit.proposal import Proposal

SOURCE = "I went to the. Store and. Bought some milk."
RESULT = "I went to the store and bought some milk."
TARGET = (101, 102, 103)


@pytest.fixture
def rig(monkeypatch):
    state = SimpleNamespace(text="original clipboard", selected=SOURCE,
                            target=TARGET, seq=10, copy=True, clock=0.,
                            keys=[], restored=[], snapshots=[], pasted=[])
    clip = ModuleType("pyperclip")
    keys = ModuleType("pyautogui")

    def read():
        return state.text

    def copy(text):
        state.text = text
        state.seq += 1

    def hotkey(*args):
        state.keys.append(args)
        if args == ("ctrl", "c") and state.copy:
            copy(state.selected)
        elif args == ("ctrl", "v"):
            state.pasted.append(state.text)
            state.selected = state.text

    def save():
        saved = cb.ClipboardSnapshot({1: b"original format", 2: b"binary format"})
        saved.fake_text = state.text
        state.snapshots.append(saved)
        return saved

    def restore(saved):
        state.restored.append(saved)
        state.text = saved.fake_text
        state.seq += 1
        return True

    def sleep(seconds):
        state.clock += max(seconds, .02)

    clip.paste, clip.copy, keys.hotkey = read, copy, hotkey
    monkeypatch.setitem(sys.modules, "pyperclip", clip)
    monkeypatch.setitem(sys.modules, "pyautogui", keys)
    monkeypatch.setattr(cb, "pyperclip", clip)
    monkeypatch.setattr(cb, "HAS_PYPERCLIP", True)
    monkeypatch.setattr(cb, "save_clipboard", save)
    monkeypatch.setattr(cb, "restore_clipboard", restore)
    monkeypatch.setattr(cb, "get_clipboard_sequence_number", lambda: state.seq)
    monkeypatch.setattr(cb, "_paste_target", lambda: state.target)
    monkeypatch.setattr(cb, "message_paste_to_focused_control", lambda: cb.DeliveryOutcome.SENT)
    monkeypatch.setattr(cb, "wait_for_clipboard_ready", lambda *_: "ready")
    monkeypatch.setattr(tp.time, "sleep", sleep)
    monkeypatch.setattr(tp.time, "monotonic", lambda: state.clock)
    monkeypatch.setattr(session.thread_registry, "spawn", lambda _name, fn, **_: fn())
    monkeypatch.setattr(session, "enabled", lambda _: False)
    monkeypatch.setattr(model, "rewrite", lambda *_: pytest.fail("unexpected model call"))
    session.clear()
    yield state
    session.clear()


@pytest.fixture
def app():
    obj = SimpleNamespace(config={}, chips=[], spoken=[], history=[],
                          command_matching_enabled=True, command_mode_active=True)
    obj._show_outcome_chip = lambda *args: obj.chips.append(args)
    obj._speak_session_notice = obj.spoken.append
    obj.history_db = SimpleNamespace(add=lambda **kw: obj.history.append(kw))
    return obj


@pytest.mark.parametrize("case", ["success", "empty", "timeout", "read_error", "key_error", "seq_error"])
def test_capture_restores_every_mutating_path(rig, monkeypatch, case):
    if case == "empty":
        rig.selected = ""
    if case == "timeout":
        rig.copy = False
    if case == "read_error":
        monkeypatch.setattr(sys.modules["pyperclip"], "paste",
                            lambda: (_ for _ in ()).throw(RuntimeError("read failed")))
    if case == "key_error":
        monkeypatch.setattr(sys.modules["pyautogui"], "hotkey",
                            lambda *_: (_ for _ in ()).throw(RuntimeError("key failed")))
    if case == "seq_error":
        monkeypatch.setattr(cb, "get_clipboard_sequence_number",
                            lambda: (_ for _ in ()).throw(RuntimeError("sequence failed")))
    if case in {"key_error", "seq_error"}:
        with pytest.raises(RuntimeError):
            tp.grab_selection_block()
    else:
        assert tp.grab_selection_block() == (SOURCE if case == "success" else None)
    assert rig.text == "original clipboard"
    assert rig.restored == rig.snapshots
    assert all(s.seq is None for s in rig.snapshots)


def test_block_is_raw_and_teaching_bounds_are_unchanged(rig):
    rig.selected = "  A long selected passage with five or more words.\nNext line.  "
    assert tp.grab_selection_block() == rig.selected
    assert tp.grab_selection_text() is None
    rig.selected = "  selected word.  "
    assert tp.grab_selection_text() == "selected word"


@pytest.mark.parametrize("length, accepted", [(20_000, True), (20_001, False)])
def test_block_cap_never_truncates(rig, length, accepted):
    rig.selected = "x" * length
    assert tp.grab_selection_block() == (rig.selected if accepted else None)
    assert rig.text == "original clipboard"


def test_missing_sequence_does_not_read_old_clipboard(rig, monkeypatch):
    monkeypatch.setattr(cb, "get_clipboard_sequence_number", lambda: None)
    assert tp.grab_selection_block() is None
    assert rig.keys == []
    assert rig.text == "original clipboard"


def test_incomplete_snapshot_never_copies_or_restores_partial_data(rig, monkeypatch):
    saved = cb.ClipboardSnapshot({1: b"partial"})
    saved.lost[2] = "unavailable format"
    monkeypatch.setattr(cb, "save_clipboard", lambda: saved)
    assert tp.grab_selection_block() is None
    assert rig.keys == rig.restored == []


def test_genuinely_empty_original_is_restored_by_real_empty_restore_branch(rig, monkeypatch):
    rig.text = ""
    monkeypatch.setattr(cb, "save_clipboard", lambda: cb.ClipboardSnapshot())
    def empty():
        rig.text = ""
        return True
    monkeypatch.setattr(cb, "_user32", SimpleNamespace(EmptyClipboard=empty, CloseClipboard=lambda: None))
    monkeypatch.setattr(cb, "_open_clipboard_with_retry", lambda: True)
    monkeypatch.setattr(cb, "restore_clipboard", cb._restore_clipboard_impl)
    assert tp.grab_selection_block() == SOURCE
    assert rig.text == ""


def test_restore_failure_prevents_staging(rig, app, monkeypatch):
    monkeypatch.setattr(cb, "restore_clipboard", lambda _: False)
    assert session.handle_utterance(app, "clean up selection")
    assert session.pending() is None
    assert not rig.pasted


@pytest.mark.parametrize("text, expected", [
    (SOURCE, RESULT),
    ("I  Went to the Store.", "I went to the store."),
    ("I met Alice in London.\nNASA is here.", "I met Alice in London.\nNASA is here."),
    ("I spoke to Dr. Smith.", "I spoke to Dr. Smith."),
    ("A proper sentence ends here. Another complete sentence begins here.",
     "A proper sentence ends here. Another complete sentence begins here."),
    ("We went home. and then we ate dinner.", "We went home and then we ate dinner."),
])
def test_deterministic_cleanup(text, expected):
    assert selection.deterministic_cleanup(text) == expected


@pytest.mark.parametrize("text", ["clean up selection", "clean up the selection", "clean this up",
                                 "fix the selection", "punctuate selection", "um, Ava, clean this up."])
def test_triggers_reach_existing_stage_without_model_offline(rig, app, text):
    assert session.handle_utterance(app, text)
    staged = session.pending()
    assert staged.proposal.rewritten == RESULT
    assert staged.selection_target == TARGET
    assert not rig.pasted
    assert rig.text == "original clipboard"


def test_every_edit_verb_reuses_teaching_selection_vocabulary(monkeypatch):
    monkeypatch.setitem(tp._SOURCE_KEYWORD_TO_KIND, "chosen passage", "selection")
    for verb in intent.EDIT_VERBS:
        assert intent.selection_request(f"{verb} the chosen passage") == "selection"
    assert intent.selection_request("fix clipboard") is None


@pytest.mark.parametrize("text", ["I said so clean this up later", "clean this up later",
                                 '"clean up selection"', "please tell Ava to clean this up"])
def test_embedded_or_quoted_request_is_not_consumed(rig, app, text, monkeypatch):
    monkeypatch.setattr(session, "enabled", lambda _: True)
    assert not session.handle_utterance(app, text)
    assert intent.selection_request(text) is None
    assert not rig.keys


@pytest.mark.parametrize("text", ["clean this up", "fix that"])
def test_bare_reference_prefers_existing_staged_draft(rig, app, monkeypatch, text):
    monkeypatch.setattr(session, "enabled", lambda _: True)
    app._session_mode_manager = SimpleNamespace(top_dictation_chunk=lambda: SimpleNamespace(payload=SOURCE))
    calls = []
    monkeypatch.setattr(session, "propose", lambda original, instruction, _app:
                        calls.append((original, instruction)) or Proposal.build(original, RESULT))
    assert session.handle_utterance(app, text)
    assert calls == [(SOURCE, text)]
    assert session.pending().selection_target is None
    assert not rig.keys


def test_explicit_selection_overrides_draft_and_uses_default_instruction(rig, app, monkeypatch):
    monkeypatch.setattr(session, "enabled", lambda _: True)
    app._session_mode_manager = SimpleNamespace(top_dictation_chunk=lambda: SimpleNamespace(payload="different draft"))
    seen = []
    monkeypatch.setattr(model, "rewrite", lambda original, instruction, _app:
                        (seen.append((original, instruction)) or RESULT, None))
    assert session.handle_utterance(app, "clean up selection")
    assert seen == [(SOURCE, selection.DEFAULT_INSTRUCTION)]
    assert session.pending().selection_target == TARGET


@pytest.mark.parametrize("raw", ["Here is the edited text: something else.",
                                "I walked into a shop and purchased a bottle of milk."])
def test_sanitizer_and_cleanup_similarity_reject_rewrites(rig, app, monkeypatch, raw):
    monkeypatch.setattr(session, "enabled", lambda _: True)
    monkeypatch.setattr(session, "is_available", lambda _: True)
    monkeypatch.setattr(model, "rewrite", lambda *_: (raw, None))
    assert session.handle_utterance(app, "clean up selection")
    assert session.pending() is None
    assert app.chips[-1][1] == "warning"
    assert not rig.pasted
    assert rig.text == "original clipboard"


def test_model_unavailable_uses_same_sanitized_offline_floor(rig, app, monkeypatch):
    monkeypatch.setattr(session, "enabled", lambda _: True)
    monkeypatch.setattr(session, "is_available", lambda _: True)
    monkeypatch.setattr(model, "rewrite", lambda *_: (None, "no model available"))
    assert session.handle_utterance(app, "clean up selection")
    assert session.pending().proposal.rewritten == RESULT


@pytest.mark.parametrize("changed", ["window", "control", "selection", "focus_unknown"])
def test_apply_refuses_changed_target_and_keeps_proposal(rig, app, changed):
    assert session.handle_utterance(app, "clean up selection")
    staged = session.pending()
    if changed == "selection":
        rig.selected = "A different selection."
    else:
        rig.target = {"window": (201, 202, 203), "control": (101, 102, 204),
                      "focus_unknown": None}[changed]
    assert not session.apply(app)["applied"]
    assert session.pending() is staged
    assert not rig.pasted
    assert rig.text == "original clipboard"
    assert "original selection" in app.spoken[-1]


def test_focus_race_during_paste_preparation_restores_clipboard(rig, app, monkeypatch):
    assert session.handle_utterance(app, "clean up selection")
    staged = session.pending()
    def change(*_):
        rig.target = (201, 202, 203)
        return "ready"
    monkeypatch.setattr(cb, "wait_for_clipboard_ready", change)
    assert not session.apply(app)["applied"]
    assert session.pending() is staged
    assert not rig.pasted
    assert rig.text == "original clipboard"


def test_apply_copy_exception_keeps_pending_and_restores(rig, app, monkeypatch):
    assert session.handle_utterance(app, "clean up selection")
    staged = session.pending()
    monkeypatch.setattr(sys.modules["pyautogui"], "hotkey",
                        lambda *_: (_ for _ in ()).throw(RuntimeError("copy failed")))
    assert not session.apply(app)["applied"]
    assert session.pending() is staged
    assert not rig.pasted
    assert rig.text == "original clipboard"


def test_apply_and_discard_share_slot_and_preserve_clipboard(rig, app):
    assert session.handle_utterance(app, "clean up selection")
    assert session.handle_utterance(app, "apply")
    assert rig.pasted == [RESULT]
    assert session.pending() is None
    assert rig.text == "original clipboard"
    assert len(rig.restored) == 3  # capture, revalidate selection, paste
    rig.selected = SOURCE
    assert session.handle_utterance(app, "clean up selection")
    assert session.handle_utterance(app, "discard")
    assert session.pending() is None
    assert rig.pasted == [RESULT]


@pytest.mark.parametrize("selected", ["", "   "])
def test_nothing_selected_is_visible_and_spoken(rig, app, selected):
    rig.selected = selected
    assert session.handle_utterance(app, "clean up selection")
    assert session.pending() is None
    assert "Nothing selected" in app.chips[-1][0]
    assert app.spoken[-1] == "Nothing selected"


def test_missing_control_identity_fails_closed(rig, app):
    rig.target = (101, 102, None)
    assert session.handle_utterance(app, "clean up selection")
    assert session.pending() is None
    assert rig.keys == []


@pytest.fixture
def executor():
    from pathlib import Path
    from samsara.commands import CommandExecutor
    from samsara.plugin_commands import _import_plugin
    # conftest blocks broad plugin discovery. Exercise this real discovery
    # shim explicitly, without importing unrelated plugins/services.
    _import_plugin(Path(__file__).resolve().parents[1] / "plugins/commands/selection_edit.py")
    return CommandExecutor(Path(__file__).resolve().parents[1] / "commands.json")


def test_command_mode_proposes_applies_and_exposes_reference_metadata(rig, app, executor):
    result = executor.process_text("um, clean up the selection", app, force_commands=True)
    assert result.claimed
    assert session.pending().proposal.rewritten == RESULT
    assert executor.process_text("apply", app, force_commands=True).claimed
    assert rig.pasted == [RESULT]
    from samsara.command_catalog import build_catalog
    specs = build_catalog(executor)
    entry = next(s for s in specs if "clean up the selection" in s.aliases)
    assert entry.description.startswith("Ava:")


@pytest.mark.parametrize("text", ["clean this up later", '"clean up selection"',
                                 "I said so clean this up later"])
def test_command_mode_enforces_whole_unquoted_request(rig, app, executor, text):
    result = executor.process_text(text, app, force_commands=True)
    assert not result.claimed
    assert session.pending() is None
    assert not rig.keys
