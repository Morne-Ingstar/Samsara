"""Synthetic pre-move traces and host-boundary checks for extraction 654."""
from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from samsara import session_modes as sm


GOOD = sm.UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,),
                           transcript_confident=True)
BAD = sm.UtteranceSignals(has_contiguous_speech=False, compression_ratios=(4.0,))
FIXTURE = Path(__file__).with_name("fixtures") / "session_corrections_pre_move.json"
CASES = (
    "click", "spoken", "gate", "expiry_equal", "expiry_after", "cancel",
    "control", "failed_capture", "missing", "duplicates", "count_changed",
    "case_first", "case_fallback", "ambiguous_audio", "segment_fallback",
    "scratch", "surface", "surface_stale", "receipt", "compact_request",
    "undo", "undo_nothing", "undo_error", "cancel_api", "compact_scratch",
)


class RecordingLock:
    """A real reentrant lock which also records observable nesting."""
    def __init__(self, events):
        self.lock = threading.RLock()
        self.depth = 0
        self.events = events

    def __enter__(self):
        self.lock.acquire()
        self.depth += 1
        self.events.append(["acquire", self.depth])
        return self

    def __exit__(self, *exc):
        self.events.append(["release", self.depth])
        self.depth -= 1
        self.lock.release()


def host(live=True):
    now, events = [100.0], []
    manager = sm.SessionModeManager(
        abort_phrases=["cancel"], foreground_exe_resolver=lambda: "synthetic.exe",
        foreground_hwnd_resolver=lambda: 101,
        inject_fn=lambda *_: pytest.fail("unexpected injection"),
        remove_chars_fn=lambda *_: pytest.fail("unexpected removal"),
        command_dispatch_fn=lambda *_: sm.CommandDispatchResult(False, None),
        agent_dispatch_fn=lambda *_: pytest.fail("unexpected model call"),
        clock=lambda: now[0], buffer_dictate_until_commit=True,
        live_surface_enabled=live,
    )
    manager.reset(sm.SessionMode.DICTATE)
    manager._dispatch_lock = RecordingLock(events)
    # Patch the facade method itself to verify dynamic speech dispatch, without TTS.
    manager._speak = lambda text, category: events.append(
        ["speak", manager._dispatch_lock.depth, text, category])
    manager.set_correction_capture_fn(lambda *args: events.append(
        ["capture", manager._dispatch_lock.depth, *args]) or "queued")
    manager.set_correction_undo_fn(lambda: {"undone": True, "wrong": "cobalt",
                                           "right": "silver", "where": "review"})
    manager.set_draft_scroll_fn(lambda *_: None)
    manager.set_surface_review_action_fn(lambda *_: True)
    return manager, now, events


def seed(manager, *chunks):
    for index, chunk in enumerate(chunks):
        manager._draft.append(chunk)
        manager._stack.push(sm.StackItem(kind="dictation_staged_chunk", payload=chunk,
                                        mode=sm.SessionMode.DICTATE, timestamp=100.0))
        manager._dictate_pending_audio.append("audio-" + str(index))


def selection(manager):
    return dict(document_id=manager._draft.document_id, revision=manager._draft.revision,
                segment_id=manager._draft.segments[0].id, start=0, end=6)


def trace(case):
    manager, now, events = host(live=case not in {"compact_request", "compact_scratch"})
    document, lock, stack = manager.draft_document, manager._dispatch_lock, manager._stack
    document_id = document.document_id
    callbacks = {name: getattr(manager, name) for name in (
        "_correction_capture_fn", "_correction_undo_fn", "_draft_scroll_fn",
        "_surface_review_action_fn", "_speak")}
    results = []
    if case == "receipt":
        results.append(manager.copy_sent_receipt_as_draft("cobalt receipt"))
        results.append(manager.copy_sent_receipt_as_draft("another receipt"))
    elif case.startswith("undo"):
        if case == "undo_nothing":
            manager.set_correction_undo_fn(lambda: {})
        elif case == "undo_error":
            def fail():
                raise ValueError("synthetic failure")
            manager.set_correction_undo_fn(fail)
        callbacks["_correction_undo_fn"] = manager._correction_undo_fn
        results.append(manager._undo_last_correction_outcome())
    else:
        chunks = ("cobalt panel.",)
        if case in {"duplicates", "count_changed", "case_first"}:
            chunks = ("Cobalt cobalt cobalt.",)
        elif case == "case_fallback":
            chunks = ("COBALT panel.",)
        elif case == "ambiguous_audio":
            chunks = ("cobalt ", "cobalt panel.")
        elif case == "segment_fallback":
            chunks = ("co", "balt panel.")
        seed(manager, *chunks)
        original_ids = [part.id for part in document.segments]
        if case.startswith("surface"):
            selected = manager.select_live_surface_word(selection(manager))
            results.append(selected)
            if case == "surface_stale":
                manager._draft.append(" later")
            results.append(manager.apply_live_surface_correction(selected["selection"], "silver"))
        elif case in {"case_first", "case_fallback", "ambiguous_audio", "segment_fallback"}:
            results.append(manager.replace_draft_word("cobalt", replacement="silver"))
        else:
            word = "absent" if case == "missing" else "cobalt"
            count = 3 if case == "count_changed" else 0
            results.append(manager.request_word_correction(
                word, 1 if case == "duplicates" else 0, expected_count=count,
                source="synthetic_click"))
            pending = manager.pending_word_correction()
            pending["word"] = "mutated copy"
            assert manager._pending_correction["word"] == word
            if case == "failed_capture":
                def fail_capture(*args):
                    events.append(["capture_error", lock.depth, *args])
                    raise ValueError("synthetic failure")
                manager.set_correction_capture_fn(fail_capture)
                callbacks["_correction_capture_fn"] = fail_capture
            if case.startswith("expiry"):
                now[0] += 45.0 + (0.001 if case == "expiry_after" else 0)
                results.append(manager.pending_word_correction())
                assert manager._pending_correction is not None  # read never clears
            if case == "cancel_api":
                results.extend([manager.cancel_word_correction(), manager.cancel_word_correction()])
            elif case == "control":
                results.append(manager._consume_word_correction(
                    sm.DICTATE_COMMIT_PHRASE, sm.DICTATE_COMMIT_PHRASE, GOOD))
            elif case in {"spoken", "gate", "cancel"}:
                text = {"cancel": "never mind"}.get(case, "silver")
                results.append(manager.dispatch_utterance(text, BAD if case == "gate" else GOOD))
            else:
                results.append(manager.choose_word_correction("silver"))
                if case in {"scratch", "compact_scratch"}:
                    results.append(manager._do_scratch_that())
        # Successful local edits retain stable segment IDs; the exceptional
        # whole-buffer fallback and scratch are independently pinned by traces.
        if case in {"click", "spoken", "surface", "duplicates"}:
            assert [part.id for part in document.segments] == original_ids
    assert manager.draft_document is document
    assert manager._draft.document_id == document_id
    assert manager._dispatch_lock is lock and manager._stack is stack
    assert all(getattr(manager, name) is callback for name, callback in callbacks.items())
    ids = {document.document_id: "document"}
    def clean(value):
        if isinstance(value, sm.DispatchOutcome):
            return {"kind": value.kind, "detail": clean(value.detail)}
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [clean(item) for item in value]
        if isinstance(value, str) and (value in ids or len(value) == 32):
            return ids.setdefault(value, "segment-" + str(len(ids)))
        return value
    return clean(dict(results=results, events=events,
        pending=manager._pending_correction, revision=document.revision,
        segments=[(part.id, part.text) for part in document.segments],
        transactions=[(item.kind, item.segment_id, item.before) for item in document._transactions],
        parked=manager._parked_hold, manual=document.manual_commit, edited=document.edited,
        stack=[(item.kind, item.payload) for item in stack._items],
        audio=manager._dictate_pending_audio, seam=manager._last_dictate_ended_terminal))


@pytest.mark.parametrize("case", CASES)
def test_pre_move_trace(case):
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert trace(case) == expected["traces"][case]


def test_choose_releases_lock_before_dynamic_apply(monkeypatch):
    manager, _, events = host()
    seed(manager, "cobalt panel")
    manager.request_word_correction("cobalt")
    events.clear()
    sentinel = object()
    def apply(pending, replacement):
        assert manager._dispatch_lock.depth == 0
        events.append(["dynamic_apply", replacement, pending["word"]])
        return sentinel
    monkeypatch.setattr(manager, "_apply_pending_word_correction", apply)
    assert manager.choose_word_correction("silver") is sentinel
    assert events == [["acquire", 1], ["release", 1],
                      ["dynamic_apply", "silver", "cobalt"]]


def test_spoken_dynamic_apply_and_dynamic_replace(monkeypatch):
    manager, _, events = host()
    seed(manager, "cobalt panel")
    manager.request_word_correction("cobalt")
    calls = []
    def replace(*args, **kwargs):
        calls.append((manager._dispatch_lock.depth, args, kwargs))
        return {"ok": True, "replacement": "silver", "context": "synthetic", "audio": "clip"}
    monkeypatch.setattr(manager, "replace_draft_word", replace)
    original_apply = manager._apply_pending_word_correction
    def apply(*args):
        calls.append(("apply", manager._dispatch_lock.depth))
        return original_apply(*args)
    monkeypatch.setattr(manager, "_apply_pending_word_correction", apply)
    assert manager.dispatch_utterance("silver", GOOD).kind == "dictate_word_corrected"
    assert calls == [("apply", 1), (1, ("cobalt", 0, "silver"), {"expected_count": 0})]
    assert events[-2] == ["capture", 1, "cobalt", "silver", "synthetic", "clip"]


@pytest.mark.parametrize("field,value", [("document_id", "other"), ("revision", -1),
    ("segment_id", "other"), ("start", -1), ("end", 1000)])
def test_selection_validates_each_coordinate(field, value):
    manager, _, _ = host()
    seed(manager, "cobalt panel")
    chosen = selection(manager)
    chosen[field] = value
    before = manager._draft.text, manager._draft.revision
    assert manager.select_live_surface_word(chosen) == {"ok": False, "reason": "stale"}
    assert (manager._draft.text, manager._draft.revision) == before


def test_commit_predicate_uses_live_facade_globals(monkeypatch):
    manager, _, _ = host()
    seed(manager, "cobalt panel")
    manager.request_word_correction("cobalt")
    # Configure through the canonical setter too: the helper must use the live
    # grammar function rather than a copied commit-word binding.
    old = sm.DICTATE_COMMIT_PHRASE
    try:
        sm.set_commit_phrase("deliver")
        assert manager._consume_word_correction("deliver", "deliver", GOOD) is None
        assert manager._pending_correction is None
        assert manager._draft.text == "cobalt panel"
    finally:
        sm.set_commit_phrase(old)
