"""Queue 99: "scratch that <n> times" and "again".

Counted repetition of the SAME scratch -- N pops of exactly the path one
"scratch that" takes -- plus the edges that make it honest: a count larger
than the five-deep stack, one undo for the whole run, and a partial failure
that stops rather than lying.

Nothing here is a new deletion mechanism: every pop is _do_scratch_that().
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import command_catalog
from samsara.session_modes import (
    SCRATCH_AGAIN_TTL_S,
    SCRATCH_COUNT_MAX,
    CommandDispatchResult,
    SessionMode,
    SessionModeManager,
    UnitOfWorkStack,
    UtteranceSignals,
    is_scratch_again,
    match_scratch_count,
    outcome_chip,
)

GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _manager(clock=None, **kwargs):
    clock = clock or _Clock()
    manager = SessionModeManager(
        abort_phrases=["cancel", "abort"],
        foreground_exe_resolver=lambda: "editor.exe",
        foreground_hwnd_resolver=lambda: 101,
        inject_fn=lambda text, focus_guard=None: text,
        remove_chars_fn=Mock(return_value=True),
        command_dispatch_fn=lambda text: CommandDispatchResult(matched=False, phrase=None),
        agent_dispatch_fn=Mock(),
        on_scratch_result=Mock(),
        buffer_dictate_until_commit=True,
        clock=clock,
        **kwargs,
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    manager._clockobj = clock
    return manager


def _stage(manager, *chunks):
    """Say `chunks` into the buffered DICTATE lane, so each becomes one staged
    chunk on the undo stack -- the eight-fragments-in-a-draft case."""
    for chunk in chunks:
        manager.dispatch_utterance(chunk, GOOD)
    return manager


def _staged_count(manager):
    return sum(1 for item in manager._stack._items if item.kind == "dictation_staged_chunk")


# ---------------------------------------------------------------------------
# 1. The phrases
# ---------------------------------------------------------------------------

class TestTheGrammar:
    @pytest.mark.parametrize("phrase,count", [
        ("scratch that twice", 2),
        ("scratch that thrice", 3),
        ("scratch that once", 1),
        ("scratch that two times", 2),
        ("scratch that three times", 3),
        ("scratch that 3 times", 3),
        ("scratch that 4 times", 4),
        ("scratch that ten times", 10),
        ("scratch that 1 time", 1),
        ("Scratch that TWICE.", 2),
    ])
    def test_counted_forms_parse(self, phrase, count):
        assert match_scratch_count(phrase) == count

    @pytest.mark.parametrize("phrase", [
        "scratch that",                       # the bare form keeps its own path
        "scratch everything",
        "scratch that idea twice",            # remainder is not a count
        "let's scratch that idea twice",      # does not start with the phrase
        "we should scratch that three times before the deadline",
        "scratch that 4000 times",            # beyond SCRATCH_COUNT_MAX
        "scratch that zero times",
        "scratch that times",
        "twice",
    ])
    def test_these_are_not_counted_scratches(self, phrase):
        assert match_scratch_count(phrase) is None

    def test_the_whole_utterance_guard_holds_in_the_lane(self):
        """The brief's case: "let's scratch that idea twice" is TYPED."""
        manager = _manager()
        _stage(manager, "first thought", "second thought")
        before = manager.dictate_pending_buffer
        outcome = manager.dispatch_utterance("let's scratch that idea twice", GOOD)
        assert outcome.kind not in ("scratch_success", "scratch_refuse")
        assert "scratch that idea twice" in manager.dictate_pending_buffer.lower()
        assert manager.dictate_pending_buffer != before

    def test_again_is_whole_utterance_only(self):
        assert is_scratch_again("again") is True
        for phrase in ("say that again", "again and again", "do it again"):
            assert is_scratch_again(phrase) is False

    def test_the_count_uses_the_shared_number_parser(self):
        """No fourth parser (queue 45): samsara.intent.normalize.parse_number
        is the shared front door, and it already reuses show_numbers'
        _WORD_TO_NUM / _parse_spoken_number. Compounds it understands, this
        understands."""
        from samsara.intent import normalize

        assert normalize.parse_number(["twenty", "one"]) == 21
        assert match_scratch_count("scratch that twenty one times") is None, \
            "beyond SCRATCH_COUNT_MAX, refused rather than truncated"
        assert SCRATCH_COUNT_MAX >= UnitOfWorkStack.MAX_SIZE


# ---------------------------------------------------------------------------
# 2. Counting, and the shortfall
# ---------------------------------------------------------------------------

class TestCountedScratch:
    @pytest.mark.parametrize("phrase,expected", [
        ("scratch that twice", 2),
        ("scratch that 3 times", 3),
        ("scratch that three times", 3),
    ])
    def test_it_pops_the_right_count(self, phrase, expected):
        manager = _manager()
        _stage(manager, "one.", "two.", "three.", "four.")
        outcome = manager.dispatch_utterance(phrase, GOOD)
        assert outcome.kind == "scratch_success"
        assert outcome.detail["scratched"] == expected
        assert _staged_count(manager) == 4 - expected

    def test_a_count_larger_than_the_stack_pops_what_exists_and_says_so(self):
        """The stack holds five. "ten times" must not silently do five and
        claim ten."""
        manager = _manager()
        _stage(manager, "one.", "two.", "three.")
        outcome = manager.dispatch_utterance("scratch that ten times", GOOD)
        assert outcome.kind == "scratch_success"
        assert outcome.detail == {"requested": 10, "scratched": 3, "reason": "empty"}
        assert _staged_count(manager) == 0
        label, kind = outcome_chip("scratch_success", outcome.detail)
        assert label.startswith("undone 3")
        assert "nothing left" in label
        assert kind == "warning", "a shortfall is never a plain success chip"

    def test_the_stack_ceiling_is_the_real_ceiling(self):
        """Six chunks staged, the stack keeps the last five, so "scratch that
        six times" reaches five and reports the shortfall."""
        manager = _manager()
        _stage(manager, "a.", "b.", "c.", "d.", "e.", "f.")
        assert _staged_count(manager) == UnitOfWorkStack.MAX_SIZE
        outcome = manager.dispatch_utterance("scratch that 6 times", GOOD)
        assert outcome.detail["scratched"] == UnitOfWorkStack.MAX_SIZE
        assert outcome.detail["reason"] == "empty"

    def test_nothing_staged_is_a_refusal_not_a_success(self):
        manager = _manager()
        outcome = manager.dispatch_utterance("scratch that twice", GOOD)
        assert outcome.kind == "scratch_refuse"
        assert outcome.detail["scratched"] == 0
        assert outcome_chip("scratch_refuse", outcome.detail)[1] == "warning"

    def test_one_chip_for_the_whole_count(self):
        manager = _manager()
        _stage(manager, "one.", "two.", "three.")
        outcome = manager.dispatch_utterance("scratch that 3 times", GOOD)
        assert outcome_chip(outcome.kind, outcome.detail) == ("undone 3", "success")

    def test_the_earcon_fires_once_not_n_times(self):
        manager = _manager()
        _stage(manager, "one.", "two.", "three.")
        manager._on_scratch_result.reset_mock()
        manager.dispatch_utterance("scratch that 3 times", GOOD)
        manager._on_scratch_result.assert_called_once_with(True)


# ---------------------------------------------------------------------------
# 3. "again"
# ---------------------------------------------------------------------------

class TestAgain:
    def test_again_after_a_scratch_repeats_it(self):
        manager = _manager()
        _stage(manager, "one.", "two.", "three.")
        manager.dispatch_utterance("scratch that", GOOD)
        assert _staged_count(manager) == 2
        outcome = manager.dispatch_utterance("again", GOOD)
        assert outcome.kind == "scratch_success"
        assert outcome.detail["scratched"] == 1
        assert _staged_count(manager) == 1

    def test_again_after_a_counted_scratch_repeats_one_more(self):
        manager = _manager()
        _stage(manager, "one.", "two.", "three.", "four.")
        manager.dispatch_utterance("scratch that twice", GOOD)
        manager.dispatch_utterance("again", GOOD)
        assert _staged_count(manager) == 1

    def test_again_after_anything_else_is_dictation(self):
        """The first guard has nothing to do with the clock: any other
        resolved utterance disarms it."""
        manager = _manager()
        _stage(manager, "one.", "two.")
        manager.dispatch_utterance("scratch that", GOOD)
        manager.dispatch_utterance("a new sentence.", GOOD)     # disarms
        before = _staged_count(manager)
        outcome = manager.dispatch_utterance("again", GOOD)
        assert outcome.kind not in ("scratch_success", "scratch_refuse")
        assert "again" in manager.dictate_pending_buffer.lower()
        assert _staged_count(manager) == before + 1, "it was staged as text"

    def test_again_with_no_scratch_at_all_is_dictation(self):
        manager = _manager()
        _stage(manager, "one.")
        outcome = manager.dispatch_utterance("again", GOOD)
        assert outcome.kind not in ("scratch_success", "scratch_refuse")
        assert manager.dictate_pending_buffer.lower().endswith("again.") or \
            "again" in manager.dictate_pending_buffer.lower()

    def test_again_outside_the_window_is_dictation(self):
        clock = _Clock()
        manager = _manager(clock=clock)
        _stage(manager, "one.", "two.", "three.")
        manager.dispatch_utterance("scratch that", GOOD)
        clock.advance(SCRATCH_AGAIN_TTL_S + 0.1)
        staged_before = _staged_count(manager)
        outcome = manager.dispatch_utterance("again", GOOD)
        assert outcome.kind not in ("scratch_success", "scratch_refuse")
        assert _staged_count(manager) == staged_before + 1

    def test_again_just_inside_the_window_still_scratches(self):
        clock = _Clock()
        manager = _manager(clock=clock)
        _stage(manager, "one.", "two.", "three.")
        manager.dispatch_utterance("scratch that", GOOD)
        clock.advance(SCRATCH_AGAIN_TTL_S - 0.1)
        assert manager.dispatch_utterance("again", GOOD).kind == "scratch_success"

    def test_a_failed_scratch_does_not_arm_again(self):
        manager = _manager()
        outcome = manager.dispatch_utterance("scratch that", GOOD)
        assert outcome.kind == "scratch_refuse"
        assert manager.dispatch_utterance("again", GOOD).kind not in (
            "scratch_success", "scratch_refuse")


# ---------------------------------------------------------------------------
# 4. Atomicity and partial failure
# ---------------------------------------------------------------------------

class TestOneUndoNotN:
    def test_n_pops_are_one_dispatch_one_chip_one_earcon(self):
        """Atomicity as the brief defines it: the count is ONE action. Nothing
        downstream -- the chip, the earcon, the outcome the history row is
        built from -- sees N events."""
        manager = _manager()
        _stage(manager, "one.", "two.", "three.", "four.")
        manager._on_scratch_result.reset_mock()
        outcome = manager.dispatch_utterance("scratch that 3 times", GOOD)
        assert outcome.detail["scratched"] == 3
        manager._on_scratch_result.assert_called_once_with(True)
        assert outcome_chip(outcome.kind, outcome.detail) == ("undone 3", "success")
        assert _staged_count(manager) == 1

    def test_a_counted_scratch_is_not_routed_through_the_recovery_slot(self):
        """Deliberate: queue 84's slot PREPENDS what it restores, which is
        right for an empty buffer (abort, cleared draft) and wrong here --
        the chunks the user kept are still staged, so the pre-scratch draft
        would be pasted in front of them and the surviving text would appear
        twice. A counted scratch is exactly as recoverable as the single
        scratch it repeats: not. See the queue 99 report."""
        manager = _manager()
        _stage(manager, "one.", "two.", "three.")
        manager.dispatch_utterance("scratch that twice", GOOD)
        assert manager.recoverable_draft == ""
        assert manager.dictate_pending_buffer.strip() != ""

    def test_a_bare_scratch_is_unchanged(self):
        """The single scratch keeps exactly the behaviour it had: no detail,
        the same chip, no recovery slot."""
        manager = _manager()
        _stage(manager, "one.", "two.")
        outcome = manager.dispatch_utterance("scratch that", GOOD)
        assert outcome.kind == "scratch_success"
        assert outcome.detail == {}
        assert manager.recoverable_draft == ""
        assert outcome_chip(outcome.kind, outcome.detail) == ("undone", "success")


class TestPartialFailure:
    def test_a_refused_pop_stops_the_run_and_the_chip_says_so(self):
        """If pop 3 of 5 refuses, the run stops there. The chip reports what
        actually happened, and the recovery slot still holds the whole draft,
        so the user is never left half-scratched behind a success chip."""
        manager = _manager()
        _stage(manager, "one.", "two.", "three.", "four.", "five.")
        whole_draft = manager.dictate_pending_buffer
        real_pop = manager._do_scratch_that
        calls = {"n": 0}

        def _pop():
            calls["n"] += 1
            if calls["n"] == 3:
                return False            # focus moved, window changed, ...
            return real_pop()

        manager._do_scratch_that = _pop
        outcome = manager.dispatch_utterance("scratch that 5 times", GOOD)

        assert outcome.kind == "scratch_success"
        assert outcome.detail["scratched"] == 2
        assert outcome.detail["reason"] == "failed"
        assert calls["n"] == 3, "it stopped at the refusal instead of pressing on"
        label, kind = outcome_chip(outcome.kind, outcome.detail)
        assert label == "undone 2 of 5 " + chr(0x2014) + " stopped"
        assert kind == "warning"
        # The two that did come off are gone; the rest are untouched and
        # visible, not stranded behind a success chip.
        assert _staged_count(manager) == 3
        left = manager.dictate_pending_buffer.lower()
        assert "four" not in left and "five" not in left
        assert "one" in left and "two" in left and "three" in left
        assert whole_draft.lower().startswith("one two three")

    def test_a_failure_on_the_first_pop_is_a_refusal(self):
        manager = _manager()
        _stage(manager, "one.", "two.")
        manager._do_scratch_that = lambda: False
        outcome = manager.dispatch_utterance("scratch that twice", GOOD)
        assert outcome.kind == "scratch_refuse"
        assert outcome.detail["scratched"] == 0


class TestStateStaysCoherent:
    def test_scratch_everything_after_a_counted_scratch(self):
        manager = _manager()
        _stage(manager, "one.", "two.", "three.")
        manager.dispatch_utterance("scratch that twice", GOOD)
        assert manager.dispatch_utterance("scratch everything", GOOD).kind == \
            "dictate_clear_awaiting_confirmation"
        manager.dispatch_utterance("yes", GOOD)
        assert manager.dictate_pending_buffer == ""
        assert _staged_count(manager) == 0

    def test_committing_after_a_counted_scratch_commits_what_is_left(self):
        manager = _manager()
        _stage(manager, "one.", "two.", "three.")
        left = None
        manager.dispatch_utterance("scratch that twice", GOOD)
        left = manager.dictate_pending_buffer
        outcome = manager.dispatch_utterance("end", GOOD)
        assert outcome.kind == "dictate_committed"
        assert left.strip() and manager.dictate_pending_buffer == ""

    def test_a_new_session_never_inherits_an_armed_again(self):
        manager = _manager()
        _stage(manager, "one.", "two.")
        manager.dispatch_utterance("scratch that", GOOD)
        manager.reset(initial_mode=SessionMode.DICTATE)
        assert manager._last_scratch_at is None


# ---------------------------------------------------------------------------
# 5. Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_no_new_phrase_collides_with_a_reserved_whole_utterance(self):
        reserved = command_catalog.reserved_whole_utterances()
        for phrase in ("scratch that twice", "scratch that three times",
                       "scratch that 3 times", "scratch that once"):
            assert command_catalog.normalize_phrase(phrase) not in reserved

    def test_again_is_already_a_builtin_and_is_not_duplicated(self):
        """Queue 99 found "again" already registered -- builtin.again ->
        repeat_last_command. The session lane reuses the word rather than
        adding a second catalog entry for it."""
        records = command_catalog.load_catalog_json() or []
        again = [r for r in records if "again" in r.get("aliases", [])]
        assert len(again) == 1, f"expected exactly one 'again' record, got {len(again)}"
        assert again[0]["canonical_id"] == "builtin.again"
        assert again[0]["pack"] == "core"

    def test_the_counted_forms_do_not_collide_with_any_catalog_phrase(self):
        records = command_catalog.load_catalog_json() or []
        aliases = {a for r in records for a in r.get("aliases", [])}
        for phrase in ("scratch that twice", "scratch that three times",
                       "scratch that once", "scratch that 3 times"):
            assert phrase not in aliases

    def test_the_scratch_family_keeps_its_risk_class(self):
        records = command_catalog.load_catalog_json() or []
        by_id = {r["canonical_id"]: r for r in records}
        assert by_id["builtin.scratch_that"]["risk"] == "write"
        assert by_id["builtin.scratch_everything"]["risk"] == "destructive"
        assert by_id["builtin.scratch_that"]["whole_utterance"] is True
