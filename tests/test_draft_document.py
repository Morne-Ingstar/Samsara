from samsara.draft_document import DraftDocument, RECOVERABLE_DRAFT_TTL_S
from samsara.session_modes import SessionMode, SessionModeManager


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


def test_staged_edited_stale_refused_and_undo_orders_edit_before_segment():
    doc = DraftDocument()
    first = doc.append("hello ")
    second = doc.append("world")
    before_edit = doc.revision
    stale = doc.edit(doc.document_id, before_edit - 1, second, 0, 5, "there")
    assert not stale.accepted and stale.reason == "stale_revision"

    edited = doc.edit(doc.document_id, before_edit, second, 0, 5, "there")
    assert edited.accepted and doc.text == "hello there" and doc.edited
    assert doc.undo() and doc.text == "hello world"
    assert doc.undo() and doc.text == "hello "
    assert doc.undo() and doc.text == ""
    assert not doc.undo(), "an empty draft undo is consumed locally"
    assert first not in {segment.id for segment in doc.segments}


def test_manual_latch_survives_append_and_clears_only_on_clear():
    doc = DraftDocument()
    doc.append("one")
    doc.mark_manual_commit()
    doc.append(" two")
    assert doc.manual_commit
    doc.clear()
    assert not doc.manual_commit and doc.text == ""


def test_clear_stashes_and_recovery_has_ttl_without_expiring_active_document():
    clock = Clock()
    doc = DraftDocument(clock=clock)
    doc.append("first thought")
    assert doc.stash_recoverable("clear") == len("first thought")
    doc.clear()
    assert doc.text == "" and doc.recoverable_text == "first thought"
    restored = doc.recover()
    assert restored == {"chars": 13, "source": "clear", "prepended": False}
    assert doc.text == "first thought"
    clock.now += RECOVERABLE_DRAFT_TTL_S + 1
    assert doc.text == "first thought", "the active document has no expiry"


def test_recovery_prepends_in_chronological_order():
    doc = DraftDocument()
    doc.append("First thought.")
    doc.stash_recoverable("cancel")
    doc.clear()
    doc.append("Second thought.")
    recovered = doc.recover(prepend=True)
    assert recovered and recovered["prepended"]
    assert doc.text == "First thought. Second thought."


def test_manager_skips_quality_redecode_only_after_a_manual_edit():
    calls = []
    manager = SessionModeManager(
        abort_phrases=["cancel", "abort"],
        foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 1,
        inject_fn=lambda text, check=None: True,
        remove_chars_fn=lambda n: True,
        command_dispatch_fn=lambda text: None,
        agent_dispatch_fn=lambda text, context=None: None,
        buffer_dictate_until_commit=True,
        commit_redecode_fn=lambda text, audio: calls.append(text) or "replacement",
    )
    manager.reset(SessionMode.DICTATE)
    manager._stage_dictate_chunk("hello world")
    manager.commit_pending_dictation()
    assert calls == ["hello world"]
    assert not manager.draft_document.manual_commit

    manager._stage_dictate_chunk("hello world")
    assert manager.replace_draft_word("world", replacement="there")["ok"]
    assert manager.draft_document.undo()
    assert manager.dictate_pending_buffer == "hello world"
    assert manager.replace_draft_word("world", replacement="there")["ok"]
    manager.commit_pending_dictation()
    assert calls == ["hello world"]
