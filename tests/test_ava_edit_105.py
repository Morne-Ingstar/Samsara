"""Queue 105 -- the reconciled ava_edit package (design B).

Queue 102 was built twice at once and the two halves did not import. This
file replaces tests/test_ava_edit_102.py, which tested the design that lost.
It is not a port of that file: it is a port of its INVARIANTS onto the
surviving contract -- session.handle_utterance, a module-level staged slot,
and SessionModeManager.apply_edit.

The six invariants, each with its own section below:

  1. A proposal is never applied without an explicit apply.
  2. Any model failure, timeout or malformed response leaves the text
     byte-identical -- never partial.
  3. The sanitizer rejects a rewrite that answers the instruction instead of
     editing the text.
  4. Apply is ONE undo: after applying, a single "scratch that" restores the
     original exactly.
  5. Demo pacing changes timing only -- the applied result is byte-identical
     at both settings.
  6. The proposal is staged against the text it was made from: if the buffer
     changed underneath, apply refuses rather than applying to different text.

Nothing here touches a network, a model, Qt or dictation.py. model.rewrite
takes an injectable call_fn, and the apply path drives a REAL
SessionModeManager whose delivery callables write to a fake window, so
"byte-identical" is asserted against the characters that would have reached
the user's text box rather than against a mock's call list.
"""
import dataclasses
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samsara.ava_edit as ava_edit
from samsara.ava_edit import intent, model, pacing, sanitize, session
from samsara.ava_edit.proposal import Change, Proposal, diff_changes, tokenize
from samsara.session_modes import (
    SessionMode,
    SessionModeManager,
    StackItem,
    chip_ttl_ms,
    outcome_chip,
)

SRC = "i went to the shop and i bought some bread and then i walked home"
BETTER = "I went to the shop, bought some bread, and then walked home."
INSTRUCTION = "make that more formal"
EXE = "notepad.exe"
HWND = 4242


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _Window:
    """The text box on the other side of the keystrokes.

    remove(n) deletes n characters from the end and paste(s) appends, which
    is what injection_safety.delete_backwards and _paste_preserving_clipboard
    do to a focused field. Holding the real characters is the point: every
    "byte-identical" assertion below reads .text, not a mock's call list.
    """

    def __init__(self, text=""):
        self.text = text
        self.calls = []          # ("remove", n) | ("paste", s), in order
        self.remove_ok = True
        self.paste_ok = True

    def remove(self, n):
        self.calls.append(("remove", n))
        if not self.remove_ok:
            return False
        if n:
            self.text = self.text[:-n]
        return True

    def paste(self, text):
        self.calls.append(("paste", text))
        if not self.paste_ok:
            return False
        self.text += text
        return True


class _App:
    """The attribute surface samsara.ava_edit reads off DictationApp.

    Explicit, not a Mock: a Mock auto-creates truthy attributes, which would
    silently defeat the very guards these tests exist to prove. Every name
    here is one DictationApp really has -- _show_outcome_chip (dictation.py
    8505), _speak_session_notice (6965), _paste_preserving_clipboard (6804),
    history_db (2473).
    """

    def __init__(self, manager=None, window=None, config=None):
        self._session_mode_manager = manager
        self.config = config if config is not None else {}
        self.chips = []          # (label, kind)
        self.spoken = []
        self.history = []
        self._window = window
        self.history_db = types.SimpleNamespace(
            add=lambda **kw: self.history.append(kw))

    def _show_outcome_chip(self, label, kind, ttl_ms="default"):
        self.chips.append((label, kind))

    def _speak_session_notice(self, text, category="confirmation"):
        self.spoken.append(text)

    def _paste_preserving_clipboard(self, text):
        return self._window.paste(text)


def _build(chunk=SRC, *, exe=EXE, hwnd=HWND, config=None):
    """A real SessionModeManager holding one dictation chunk, a fake window
    behind its delivery callables, and the app surface wired to both."""
    window = _Window(chunk)
    inject_calls = []
    manager = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=lambda: exe,
        foreground_hwnd_resolver=lambda: hwnd,
        # The FORMATTING pipeline. If an apply or an undo ever went through
        # here the text would come back re-formatted, not restored -- so
        # these tests assert it is never called (see TestApplyIsOneUndo).
        inject_fn=lambda *a, **k: inject_calls.append(a),
        format_dictate_fn=lambda t: t.upper(),
        remove_chars_fn=window.remove,
        command_dispatch_fn=lambda *a, **k: None,
        agent_dispatch_fn=lambda *a, **k: None,
        clock=lambda: 1000.0,
    )
    manager.mode = SessionMode.DICTATE
    if chunk:
        manager._stack.push(StackItem(
            kind="dictation_chunk", payload=chunk, mode=SessionMode.DICTATE,
            timestamp=1000.0,
            extra={"target_process": exe, "hwnd": hwnd},
        ))
    app = _App(manager, window, config=config)
    app.inject_calls = inject_calls
    return app, manager, window


def _staged(app, rewrite=BETTER):
    """Put a real accepted proposal in the slot, the way stage() would."""
    proposal = Proposal.build(session.source_text(app), rewrite,
                              instruction=INSTRUCTION)
    assert proposal.ok
    session.stage(app, proposal)
    return proposal


@pytest.fixture(autouse=True)
def _clean_slot():
    """The staged slot is module-level, like execution_policy's pending
    operation. A leaked proposal would make the next test lie."""
    session.clear()
    yield
    session.clear()


# ---------------------------------------------------------------------------
# The headline gate: the package imports, and every exported name resolves
# ---------------------------------------------------------------------------

class TestThePackageImports:
    def test_every_name_in_all_exists(self):
        missing = [n for n in ava_edit.__all__ if not hasattr(ava_edit, n)]
        assert missing == [], (
            f"__all__ promises names the package does not have: {missing}")

    def test_the_proposal_surface_session_py_calls(self):
        """Every attribute and constructor session.py reaches for, in one
        place, so a future rename breaks here rather than at runtime."""
        p = Proposal.build(SRC, BETTER, instruction=INSTRUCTION)   # session.py:165
        assert p.ok and p.reason == ""                             # session.py:174-176
        assert p.original == SRC                                   # session.py:222, 277
        assert p.rewritten == BETTER                               # session.py:230, 277
        assert isinstance(p.change_count, int)                     # session.py:181, 236
        assert isinstance(p.chip_label(), str)                     # session.py:179
        assert isinstance(p.describe(), str)                       # session.py:175, 180
        n = Proposal.none("no instruction", original=SRC, instruction=INSTRUCTION)
        assert n.ok is False and n.reason == "no instruction"      # session.py:151-163
        assert diff_changes(SRC, BETTER)                           # __init__.py:54
        assert isinstance(Change("replace", "a", "b").describe(), str)

    def test_model_rewrite_is_the_shape_session_py_unpacks(self):
        """session.py:156 does `raw, reason = model.rewrite(...)`."""
        raw, reason = model.rewrite(SRC, INSTRUCTION, None,
                                    call_fn=lambda _u: (BETTER, None))
        assert raw == BETTER and reason is None
        raw, reason = model.rewrite(SRC, INSTRUCTION, None,
                                    call_fn=lambda _u: (None, "boom"))
        assert raw is None and reason == "boom"

    def test_tokenize_is_lossless(self):
        """Word-level diffing is only safe if the tokens rebuild the input."""
        for text in (SRC, BETTER, "  leading and  trailing  ", "", "one"):
            assert "".join(tokenize(text)) == text


# ---------------------------------------------------------------------------
# 1. A proposal is never applied without an explicit apply
# ---------------------------------------------------------------------------

class TestNothingAppliesWithoutTheWord:
    def test_proposing_does_not_touch_the_text(self, monkeypatch):
        app, manager, window = _build()
        monkeypatch.setattr(model, "rewrite", lambda *a, **k: (BETTER, None))
        p = session.propose(SRC, INSTRUCTION, app)
        assert p.ok
        assert window.text == SRC
        assert window.calls == []
        assert manager.stack_depth == 1, "still just the dictation chunk"

    def test_staging_shows_a_chip_and_applies_nothing(self):
        app, _manager, window = _build()
        p = Proposal.build(SRC, BETTER, instruction=INSTRUCTION)
        assert session.stage(app, p) is True
        assert window.text == SRC
        assert window.calls == []
        assert session.pending() is not None
        assert app.chips[-1] == (p.chip_label(), "pending")

    @pytest.mark.parametrize("utterance", [
        "what do you think of that",
        "read that back to me",
        "make that shorter",
        "yes",
        "ok",
        "sure",
        "applying",
        "apply the brakes in the car",
        "tell me a joke",
    ])
    def test_no_other_utterance_applies_it(self, utterance):
        app, _manager, window = _build()
        _staged(app)
        claimed = session.handle_utterance(app, utterance)
        assert claimed is False, "an utterance that is not the word goes to Ava"
        assert window.text == SRC
        assert session.pending() is not None, "the proposal still stands"

    @pytest.mark.parametrize("word", ["apply", "apply it", "apply that",
                                      "apply the edit", "do it", "make it so"])
    def test_only_the_apply_vocabulary_applies(self, word):
        app, _manager, window = _build()
        _staged(app)
        assert session.handle_utterance(app, word) is True
        assert window.text == BETTER

    def test_a_discarded_proposal_cannot_be_applied_later(self):
        app, _manager, window = _build()
        _staged(app)
        assert session.handle_utterance(app, "never mind") is True
        assert session.pending() is None
        assert window.text == SRC
        assert session.handle_utterance(app, "apply") is False
        assert window.text == SRC

    def test_an_expired_proposal_cannot_be_applied(self, monkeypatch):
        """An "apply" said much later, about something else entirely, must
        not land on a proposal the user has stopped looking at."""
        app, _manager, window = _build()
        _staged(app)
        staged = session.pending()
        monkeypatch.setattr(staged, "_clock",
                            lambda: staged.created_at + session.PROPOSAL_TTL_S + 1.0)
        assert session.pending() is None
        result = session.apply(app)
        assert result["applied"] is False
        assert window.text == SRC

    def test_apply_with_nothing_staged_is_a_refusal_not_a_crash(self):
        app, _manager, window = _build()
        result = session.apply(app)
        assert result == {"applied": False, "reason": "nothing staged", "chars": 0}
        assert window.text == SRC

    def test_an_edit_request_with_no_session_buffer_is_not_claimed(self):
        """No dictated text means there is nothing to edit, so the utterance
        is not an edit request at all and must reach Ava untouched."""
        app, _manager, _window = _build(chunk="")
        assert session.is_available(app) is False
        assert session.handle_utterance(app, "make that shorter") is False

    def test_no_session_manager_at_all_falls_through_to_ava(self):
        app = _App(manager=None, window=_Window())
        assert session.source_text(app) == ""
        assert session.handle_utterance(app, "make that shorter") is False


# ---------------------------------------------------------------------------
# 2. Any failure leaves the text byte-identical -- never partial
# ---------------------------------------------------------------------------

class TestFailureIsAlwaysWhole:
    @pytest.mark.parametrize("call_fn,expect", [
        (lambda u: (None, "the model took too long"), "took too long"),
        (lambda u: (None, "the model could not be reached"), "could not be reached"),
        (lambda u: ("", None), "returned nothing"),
        (lambda u: (None, None), "returned nothing"),
        (lambda u: ("   \n  ", None), "returned nothing"),
    ])
    def test_the_model_layer_names_every_failure(self, call_fn, expect):
        raw, reason = model.rewrite(SRC, INSTRUCTION, None, call_fn=call_fn)
        assert raw is None
        assert expect in reason

    def test_a_raising_model_is_a_refusal_not_a_crash(self):
        def boom(_u):
            raise RuntimeError("connection reset")
        raw, reason = model.rewrite(SRC, INSTRUCTION, None, call_fn=boom)
        assert raw is None and reason == "the model could not be reached"

    def test_no_model_available_is_a_refusal(self, monkeypatch):
        monkeypatch.setattr(model, "resolve_backend", lambda app: None)
        raw, reason = model.rewrite(SRC, INSTRUCTION, None)
        assert raw is None and reason == "no model available"

    def test_an_over_long_source_never_reaches_a_model(self):
        called = []
        raw, reason = model.rewrite(
            "x " * model.MAX_SOURCE_CHARS, INSTRUCTION, None,
            call_fn=lambda u: called.append(u) or (BETTER, None))
        assert raw is None and reason == "that text is too long to edit"
        assert called == []

    def test_empty_source_and_empty_instruction_never_reach_a_model(self):
        called = []

        def spy(user_message):
            called.append(user_message)
            return BETTER, None

        assert model.rewrite("", INSTRUCTION, None,
                             call_fn=spy)[1] == "there is no text to edit"
        assert model.rewrite(SRC, "  ", None,
                             call_fn=spy)[1] == "no instruction given"
        assert called == []

    @pytest.mark.parametrize("reply,expect", [
        ("Sure! Here's a more formal version: I went out.", "replied"),
        ("```\n" + BETTER + "\n```", "off-script"),
        ("<think>the user wants formal</think>" + BETTER, "off-script"),
        ("Bread is a staple food in many households worldwide.", "not derived"),
        (INSTRUCTION, "repeated"),
        (SRC, "would not change anything"),
        (SRC.replace(" ", "  "), "would not change anything"),
    ])
    def test_every_bad_reply_becomes_a_whole_refusal(self, monkeypatch, reply, expect):
        app, _manager, window = _build()
        monkeypatch.setattr(model, "rewrite", lambda *a, **k: (reply, None))
        p = session.propose(SRC, INSTRUCTION, app)
        assert p.ok is False
        assert expect in p.reason
        assert p.rewritten == p.original == SRC
        assert window.text == SRC, "the text must not have moved"

    @pytest.mark.parametrize("reason", [
        "the model took too long", "the model could not be reached",
        "the model returned nothing", "no model available",
    ])
    def test_a_model_failure_becomes_a_whole_refusal(self, monkeypatch, reason):
        app, _manager, window = _build()
        monkeypatch.setattr(model, "rewrite", lambda *a, **k: (None, reason))
        p = session.propose(SRC, INSTRUCTION, app)
        assert p.ok is False and p.reason == reason
        assert window.text == SRC

    def test_a_refusal_carries_the_original_as_its_rewrite(self):
        """The property that makes a careless caller safe: `rewritten` on a
        refusal is the ORIGINAL, so applying it anyway is a no-op, not an
        erasure."""
        p = Proposal.none("the model took too long", original=SRC,
                          instruction=INSTRUCTION)
        assert p.rewritten == p.original == SRC
        assert p.change_count == 0

    def test_applying_a_refusal_anyway_changes_nothing(self):
        """Belt and braces: even if a caller ignored `ok`, apply_edit sees
        rewritten == original and refuses with "no change"."""
        app, manager, window = _build()
        session._wire_raw_injector(app, manager)
        p = Proposal.none("the model took too long", original=SRC)
        result = manager.apply_edit(p.rewritten)
        assert result["applied"] is False and result["reason"] == "no change"
        assert window.text == SRC
        assert window.calls == []

    def test_a_refusal_is_never_staged(self):
        app, _manager, window = _build()
        p = Proposal.none("the model took too long", original=SRC)
        assert session.stage(app, p) is False
        assert session.pending() is None
        assert window.text == SRC
        assert app.chips[-1][1] == "warning"
        assert "did not change anything" in app.spoken[-1]

    def test_an_interrupted_deletion_is_reported_not_dressed_up(self):
        app, _manager, window = _build()
        _staged(app)
        window.remove_ok = False
        result = session.apply(app)
        assert result["applied"] is False
        assert "interrupted" in result["reason"]

    def test_a_handler_bug_costs_the_edit_not_the_ava_turn(self, monkeypatch):
        """handle_utterance sits in front of the agent dispatch path."""
        app, _manager, _window = _build()
        monkeypatch.setattr(
            session.intent, "is_edit_request",
            lambda text: (_ for _ in ()).throw(RuntimeError("x")))
        assert session.handle_utterance(app, "make that shorter") is False


# ---------------------------------------------------------------------------
# 3. The sanitizer rejects an answer rather than an edit
# ---------------------------------------------------------------------------

class TestSanitizer:
    def test_the_floor_is_the_documented_value(self):
        assert sanitize.SIMILARITY_FLOOR == 0.40

    def test_the_threshold_sits_in_an_empty_band(self):
        """The 0.40 floor is only defensible if real edits and real answers
        do not overlap around it. Assert the separation instead of trusting
        it. Queue 102 measured edits at 0.77-0.93 and answers at 0.23-0.31."""
        edits = [BETTER,
                 "I went to the shop for bread and walked home.",
                 "i go to the shop and i buy some bread and then i walk home"]
        answers = ["That sounds like a pleasant errand entirely unrelated to shops.",
                   "The weather today is bright and clear with little wind about.",
                   "Bread is a staple food eaten in many households around the world."]
        worst_edit = min(sanitize.similarity(SRC, e) for e in edits)
        best_answer = max(sanitize.similarity(SRC, a) for a in answers)
        assert best_answer < sanitize.SIMILARITY_FLOOR <= worst_edit, (
            f"answers reach {best_answer:.2f}, edits fall to {worst_edit:.2f}, "
            f"floor {sanitize.SIMILARITY_FLOOR}")

    def test_it_rejects_a_rewrite_that_answers_the_instruction(self):
        answered = ("That sounds like a pleasant errand. Bread is a staple in "
                    "many households.")
        assert sanitize.check(answered, SRC, INSTRUCTION) is not None

    def test_it_rejects_an_answer_to_a_QUESTION_in_the_source(self):
        """The crispest form of the failure: the source asks something and
        the rewrite replies instead of editing."""
        q = "what time does the shop close on sunday"
        assert sanitize.check("The shop closes at five on Sunday.", q,
                              "fix the capitals") is not None
        assert sanitize.check("What time does the shop close on Sundays?", q,
                              "fix the capitals") is None

    @pytest.mark.parametrize("bad", [
        "Sure! Here is the revised text: I went to the shop.",
        "Here's a shorter version of your sentence.",
        "I've made it more formal for you.",
        "Revised: I went to the shop.",
        "I'm sorry, I can't help with that.",
        "As an AI language model, I cannot edit text.",
    ])
    def test_it_rejects_every_shape_of_the_model_talking_to_you(self, bad):
        assert sanitize.check(bad, SRC, INSTRUCTION) is not None

    @pytest.mark.parametrize("good", [
        BETTER,
        "I went to the shop for bread and walked home.",
        "i go to the shop and i buy some bread and then i walk home",
        "I went to the shop, bought bread, then walked home.",
    ])
    def test_it_accepts_a_real_edit(self, good):
        assert sanitize.check(good, SRC, INSTRUCTION) is None

    def test_it_rejects_a_rewrite_that_doubles_the_text(self):
        assert sanitize.check(SRC + " " + SRC + " " + SRC, SRC, INSTRUCTION) is not None

    def test_it_rejects_empty_and_whitespace(self):
        assert sanitize.check("", SRC, INSTRUCTION) is not None
        assert sanitize.check("   \n  ", SRC, INSTRUCTION) is not None

    def test_an_answer_never_reaches_the_slot(self, monkeypatch):
        """End to end: the gate is wired into the only propose path."""
        app, _manager, window = _build()
        monkeypatch.setattr(
            model, "rewrite",
            lambda *a, **k: ("That sounds like a pleasant errand. Bread is a "
                             "staple in many households.", None))
        assert session.propose_and_stage(app, INSTRUCTION) is False
        assert session.pending() is None
        assert window.text == SRC

    def test_the_honest_limit_of_a_shape_gate(self):
        """Recorded, not asserted as good: a reply that reuses enough of the
        source's own words passes every guard. sanitize.py says so in its
        docstring -- it rejects the SHAPES of an answer and requires the
        rewrite be derived from the source; it cannot decide semantically.
        This case scores 0.51, above the 0.40 floor, and is staged.

        It is bounded by the rest of the design rather than by this gate:
        nothing is applied without the word, the diff is read aloud first,
        and one "scratch that" takes it back.
        """
        passes = "The shop sells bread, milk and the morning papers."
        assert sanitize.similarity(SRC, passes) > sanitize.SIMILARITY_FLOOR
        assert sanitize.check(passes, SRC, INSTRUCTION) is None


# ---------------------------------------------------------------------------
# 4. Apply is ONE undo
# ---------------------------------------------------------------------------

class TestTheConfigToggle:
    """ava_edit.enabled and ava_edit.demo_pacing are real, registered keys --
    the brief asks for a toggle, and a toggle nobody can find is not one."""

    def test_both_keys_are_in_the_settings_schema(self):
        from samsara.config_schema import SETTINGS_SCHEMA
        assert SETTINGS_SCHEMA["ava_edit.enabled"]["default"] is True
        pacing_entry = SETTINGS_SCHEMA["ava_edit.demo_pacing"]
        assert pacing_entry["default"] == "instant"
        assert set(pacing_entry["options"]) == set(pacing.names())

    def test_off_means_an_edit_request_is_an_ordinary_ava_turn(self):
        app, _manager, window = _build(config={"ava_edit": {"enabled": False}})
        before = window.text
        assert session.handle_utterance(app, "make that more formal") is False
        assert session.pending() is None, "nothing may be staged while off"
        assert window.text == before

    def test_off_does_not_swallow_the_apply_word_either(self):
        """With the feature off there is no proposal, so "apply" must fall
        through to Ava rather than being eaten by a dead code path."""
        app, _manager, _window = _build(config={"ava_edit": {"enabled": False}})
        assert session.handle_utterance(app, "apply") is False

    def test_on_by_default_when_the_section_is_missing_entirely(self):
        app, _manager, _window = _build(config={})
        assert session.enabled(app) is True


class TestApplyIsOneUndo:
    def test_a_single_scratch_that_restores_the_original_exactly(self):
        app, manager, window = _build()
        _staged(app)
        assert session.apply(app)["applied"] is True
        assert window.text == BETTER
        assert manager.stack_depth == 1, "an apply must be exactly ONE unit of work"
        assert manager._stack.peek().kind == "edit_apply"

        assert manager._do_scratch_that() is True
        assert window.text == SRC, "byte-identical, not a re-formatting of it"

    def test_one_unit_however_many_changes_it_made(self):
        app, manager, window = _build()
        many = "I strolled to the bakery, purchased a loaf, and later returned home."
        p = Proposal.build(SRC, many, instruction=INSTRUCTION)
        assert p.change_count >= 3
        session.stage(app, p)
        assert session.apply(app)["applied"] is True
        assert manager.stack_depth == 1
        assert manager._do_scratch_that() is True
        assert window.text == SRC

    def test_the_undo_leaves_the_chunk_a_second_scratch_can_take_back(self):
        app, manager, window = _build()
        _staged(app)
        session.apply(app)
        assert manager._do_scratch_that() is True
        item = manager._stack.peek()
        assert item.kind == "dictation_chunk" and item.payload == SRC
        assert manager._do_scratch_that() is True
        assert window.text == ""

    def test_neither_apply_nor_undo_goes_through_the_formatting_pipeline(self):
        """inject_fn runs process_transcription -> clean_text ->
        smart_correct. Through it, "scratch that" would give the user a NEW
        FORMATTING of their sentence, not their sentence."""
        app, manager, window = _build()
        _staged(app)
        session.apply(app)
        manager._do_scratch_that()
        assert app.inject_calls == [], "the edit path must use the RAW injector only"
        assert window.text == SRC

    def test_the_stack_item_carries_what_the_undo_needs(self):
        app, manager, _window = _build()
        _staged(app)
        session.apply(app)
        item = manager._stack.peek()
        assert item.kind == "edit_apply"
        assert item.payload == BETTER
        assert item.extra["original"] == SRC
        assert item.extra["target_process"] == EXE
        assert item.extra["hwnd"] == HWND

    def test_the_restore_is_byte_exact_including_surrounding_whitespace(self):
        """Queue 102, found by mutation testing: changing the undo to inject
        original.strip() left the whole suite green, because every other
        source here happens to have no surrounding whitespace.

        That whitespace is not hypothetical. dictation.py's _inject_fn ends
        with `if config['add_trailing_space']: formatted = formatted + " "`,
        so a committed chunk NORMALLY ends in a space, and that space is part
        of what the user's document contains. An undo that trims it hands
        back a sentence that no longer joins to the next one -- a silent,
        one-character corruption of the exact thing "scratch that" promises
        to restore.
        """
        padded = "  " + SRC + "  "
        app, manager, window = _build(chunk=padded)
        assert session.source_text(app) == padded
        session.stage(app, Proposal.build(padded, BETTER, instruction=INSTRUCTION))
        assert session.apply(app)["applied"] is True
        assert window.text == BETTER

        assert manager._do_scratch_that() is True
        assert window.text == padded
        assert window.text is not padded or window.text == padded
        assert len(window.text) == len(padded), "no character may be trimmed"
        assert manager._stack.peek().payload == padded

    def test_the_restore_keeps_a_trailing_space_the_pipeline_added(self):
        """The narrowest case of the above, written separately because it is
        the one that actually ships: add_trailing_space is on by default."""
        chunk = SRC + " "
        app, manager, window = _build(chunk=chunk)
        session.stage(app, Proposal.build(chunk, BETTER, instruction=INSTRUCTION))
        session.apply(app)
        assert manager._do_scratch_that() is True
        assert window.text == chunk
        assert window.text.endswith(" "), "the pipeline's trailing space survives the undo"

    def test_the_applied_rewrite_is_byte_exact_too(self):
        """The same exactness in the other direction: what the sanitizer
        approved is what reaches the window, not a re-spaced version of it."""
        app, _manager, window = _build()
        spaced = "I went  to the shop, bought bread, and walked home. "
        session.stage(app, Proposal.build(SRC, spaced, instruction=INSTRUCTION))
        assert session.apply(app)["applied"] is True
        assert window.text == spaced
        assert len(window.text) == len(spaced)

    def test_the_slot_is_cleared_so_one_word_cannot_apply_twice(self):
        app, _manager, window = _build()
        _staged(app)
        session.apply(app)
        assert session.pending() is None
        assert session.apply(app)["applied"] is False
        assert window.text == BETTER

    def test_an_applied_edit_is_logged_to_history(self):
        app, _manager, _window = _build()
        _staged(app)
        session.apply(app)
        assert app.history == [
            {"raw_text": SRC, "display_text": BETTER, "entry_type": "edit"}]


# ---------------------------------------------------------------------------
# 5. Demo pacing changes timing only
# ---------------------------------------------------------------------------

class TestPacingIsPresentationOnly:
    def _apply_at(self, name):
        app, manager, window = _build(config={"ava_edit": {"demo_pacing": name}})
        _staged(app)
        dwells = []
        session._wire_raw_injector(app, manager)
        pace = pacing.resolve(app)
        result = manager.apply_edit(
            session.pending().proposal.rewritten,
            before_delete_s=pace.before_delete_s,
            after_delete_s=pace.after_delete_s,
            dwell_fn=dwells.append,
        )
        assert result["applied"] is True
        return window.text, window.calls, dwells

    def test_the_applied_text_is_byte_identical_at_both_settings(self):
        instant_text, instant_calls, instant_dwells = self._apply_at("instant")
        cine_text, cine_calls, cine_dwells = self._apply_at("cinematic")
        assert instant_text == cine_text == BETTER
        assert instant_calls == cine_calls, (
            "pacing must not change WHICH characters are removed or delivered")
        assert sum(instant_dwells) == 0
        assert sum(cine_dwells) > 0, "cinematic must actually be slower"

    def test_the_text_is_delivered_in_exactly_one_call_at_both_settings(self):
        """There is no setting at which a partial string can reach the
        window, because the rewrite is never sliced."""
        for name in ("instant", "cinematic"):
            _text, calls, _dwells = self._apply_at(name)
            assert calls == [("remove", len(SRC)), ("paste", BETTER)], name

    def test_pacing_carries_dwell_and_nothing_else(self):
        """The structural reason invariant 5 holds: a Pacing has no field
        that could describe the text, the split, or the call count."""
        names = {f.name for f in dataclasses.fields(pacing.Pacing)}
        assert names == {"name", "before_delete_s", "after_delete_s"}
        for name in pacing.names():
            p = pacing._PACINGS[name]
            assert isinstance(p.before_delete_s, float)
            assert isinstance(p.after_delete_s, float)

    def test_session_apply_hands_the_rewrite_through_untouched(self):
        """The one call site: whatever the pacing, apply_edit receives
        proposal.rewritten verbatim and two floats."""
        seen = {}
        app, manager, _window = _build(
            config={"ava_edit": {"demo_pacing": "cinematic"}})
        p = _staged(app)

        def spy(rewritten, *, before_delete_s=0.0, after_delete_s=0.0, dwell_fn=None):
            seen.update(rewritten=rewritten, before=before_delete_s,
                        after=after_delete_s)
            return {"applied": True, "reason": "applied", "chars": len(rewritten)}

        manager.apply_edit = spy
        session.apply(app)
        cine = pacing._PACINGS[pacing.CINEMATIC]
        assert seen["rewritten"] == p.rewritten == BETTER
        assert (seen["before"], seen["after"]) == (cine.before_delete_s,
                                                   cine.after_delete_s)

    @pytest.mark.parametrize("configured", [
        "cinematographic", "", None, 42, {"nested": True},
    ])
    def test_an_unknown_pacing_falls_back_to_instant_not_slow(self, configured):
        app = _App(config={"ava_edit": {"demo_pacing": configured}}, window=_Window())
        assert pacing.resolve(app) is pacing._PACINGS[pacing.INSTANT]

    def test_no_config_at_all_is_instant(self):
        assert pacing.resolve(_App(window=_Window())).name == pacing.INSTANT
        assert pacing.resolve(None).name == pacing.INSTANT


# ---------------------------------------------------------------------------
# 6. The proposal is staged against the text it was made from
# ---------------------------------------------------------------------------

class TestStagedAgainstItsOwnText:
    def test_apply_refuses_when_the_buffer_moved_underneath(self):
        """Rewriting a DIFFERENT chunk with this rewrite would be the worst
        failure this feature could have (session.py:222)."""
        app, manager, window = _build()
        _staged(app)
        moved = "something else entirely that i said afterwards"
        manager._stack.pop()
        manager._stack.push(StackItem(
            kind="dictation_chunk", payload=moved, mode=SessionMode.DICTATE,
            timestamp=1000.0, extra={"target_process": EXE, "hwnd": HWND}))
        window.text = moved

        result = session.apply(app)
        assert result["applied"] is False
        assert "changed" in result["reason"]
        assert window.text == moved
        assert window.calls == [], "not one character was touched"

    def test_apply_refuses_when_the_chunk_is_gone(self):
        app, manager, window = _build()
        _staged(app)
        manager._stack.pop()
        window.text = ""
        result = session.apply(app)
        assert result["applied"] is False
        assert window.calls == []

    def test_apply_refuses_when_focus_moved_to_another_app(self):
        app, manager, window = _build()
        _staged(app)
        manager._foreground_exe_resolver = lambda: "chrome.exe"
        result = session.apply(app)
        assert result["applied"] is False and result["reason"] == "focus moved"
        assert window.text == SRC
        assert window.calls == []

    def test_apply_refuses_when_another_window_of_the_same_app_is_in_front(self):
        app, manager, window = _build()
        _staged(app)
        manager._foreground_hwnd_resolver = lambda: HWND + 1
        result = session.apply(app)
        assert result["applied"] is False and result["reason"] == "the window changed"
        assert window.text == SRC
        assert window.calls == []

    def test_apply_refuses_when_the_raw_injector_is_not_wired(self):
        """Fail-closed: no raw delivery means the edit path is unavailable,
        not that it falls back to the formatting pipeline."""
        app, _manager, window = _build()
        _staged(app)
        app._paste_preserving_clipboard = None
        result = session.apply(app)
        assert result["applied"] is False
        assert result["reason"] == "the edit path is not wired"
        assert window.text == SRC


# ---------------------------------------------------------------------------
# The chip and the voice -- the two surfaces the diff is shown on
# ---------------------------------------------------------------------------

class TestTheSurfaces:
    def test_the_proposal_chip_is_pending_and_never_expires(self):
        app, _manager, _window = _build()
        p = _staged(app)
        label, kind = app.chips[-1]
        assert kind == "pending"
        assert chip_ttl_ms("edit_proposed", kind) is None, (
            "a staged proposal must survive until it is answered")
        assert label == p.chip_label()

    def test_the_chip_label_matches_session_modes_own_wording(self):
        """Two routes can draw this chip. They must say the same thing."""
        one = Proposal.build("the cat sat on the mat", "the dog sat on the mat")
        assert one.change_count == 1
        assert one.chip_label() == outcome_chip("edit_proposed", {"changes": 1})[0]
        many = Proposal.build(SRC, BETTER)
        assert many.chip_label() == outcome_chip(
            "edit_proposed", {"changes": many.change_count})[0]

    def test_the_chip_names_the_word_that_resolves_it(self):
        p = Proposal.build("the cat sat on the mat", "the dog sat on the mat")
        label = p.chip_label()
        assert "say apply" in label
        assert "1 change" in label and "1 changes" not in label

    def test_the_spoken_diff_reads_the_changes_and_names_the_verb(self):
        p = Proposal.build("the cat sat on the mat", "the dog sat on the mat")
        spoken = p.describe()
        assert '"cat" becomes "dog"' in spoken
        assert "say apply" in spoken.lower()

    def test_a_long_diff_is_summarised_not_recited(self):
        p = Proposal.build(SRC, BETTER)
        assert p.change_count > 3
        spoken = p.describe()
        assert "more" in spoken
        assert spoken.count(";") <= 3, "being read every change is noise, not review"

    def test_a_refusal_says_out_loud_that_nothing_moved(self):
        p = Proposal.none("the model took too long", original=SRC)
        assert p.describe() == "I did not change anything: the model took too long."
        assert p.chip_label() == "no edit"

    def test_a_chip_that_cannot_draw_does_not_take_the_edit_path_down(self):
        app, _manager, window = _build()
        app._show_outcome_chip = lambda *a, **k: (
            _ for _ in ()).throw(RuntimeError("no ui"))
        p = Proposal.build(SRC, BETTER, instruction=INSTRUCTION)
        assert session.stage(app, p) is True
        assert session.pending() is not None
        assert session.apply(app)["applied"] is True
        assert window.text == BETTER


# ---------------------------------------------------------------------------
# The local diff -- the rewrite is the payload, the diff is presentation
# ---------------------------------------------------------------------------

class TestTheLocalDiff:
    def test_the_model_never_supplies_the_diff(self):
        """diff_changes is pure: same inputs, same answer, no app, no
        network. The model only ever provides prose."""
        a = diff_changes(SRC, BETTER)
        b = diff_changes(SRC, BETTER)
        assert a == b and len(a) > 0

    def test_identical_text_is_no_changes(self):
        assert diff_changes(SRC, SRC) == ()

    def test_a_one_word_swap_is_one_change(self):
        changes = diff_changes("the cat sat on the mat", "the dog sat on the mat")
        assert len(changes) == 1
        assert changes[0].old == "cat" and changes[0].new == "dog"

    def test_whitespace_only_difference_counts_as_no_change(self):
        assert diff_changes("hello  world", "hello world") == ()

    def test_a_zero_change_rewrite_can_never_be_staged(self):
        """Not an ok proposal with 0 changes -- a refusal. A "0 changes"
        pending chip would ask the user to apply nothing."""
        for rewrite in (SRC, SRC.replace(" ", "  "), SRC + "  "):
            p = Proposal.build(SRC, rewrite, instruction=INSTRUCTION)
            assert p.ok is False
            assert p.reason == "that would not change anything"

    def test_the_rewrite_is_what_is_applied_not_the_diff(self):
        """A diff bug can mis-count or mis-read. It cannot corrupt the text,
        because nothing reconstructs the result from the changes."""
        app, _manager, window = _build()
        p = Proposal.build(SRC, BETTER, instruction=INSTRUCTION)
        object.__setattr__(p, "changes", (Change("replace", "nonsense", "rubbish"),))
        session.stage(app, p)
        assert session.apply(app)["applied"] is True
        assert window.text == BETTER, "the payload, not the presentation"


# ---------------------------------------------------------------------------
# Voice entry -- both conditions, never one
# ---------------------------------------------------------------------------

class TestVoiceEntry:
    @pytest.mark.parametrize("utterance", [
        "make that more formal", "make this shorter",
        "rewrite that in the past tense", "fix the capitals in that",
        "shorten this", "um, tidy that up",
    ])
    def test_an_edit_request_is_recognised(self, utterance):
        assert intent.is_edit_request(utterance), utterance

    @pytest.mark.parametrize("utterance", [
        "what is the weather today", "who wrote that book", "tell me a joke",
        "what does that say", "read that back",
        "make me a coffee",          # an edit verb, but no reference
        "what did i just say",       # a reference, but no edit verb
    ])
    def test_an_ordinary_ava_turn_is_not_an_edit_request(self, utterance):
        assert not intent.is_edit_request(utterance), utterance

    def test_a_quoted_apply_is_payload_not_a_command(self):
        assert intent.is_apply_utterance('"apply"') is False
        assert intent.is_apply_utterance(chr(0x201C) + "apply" + chr(0x201D)) is False

    def test_an_ordinary_question_falls_through_unclaimed(self):
        app, _manager, _window = _build()
        assert session.handle_utterance(app, "what is the weather today") is False
        assert app.chips == []

    def test_the_full_round_trip(self, monkeypatch):
        app, manager, window = _build()
        monkeypatch.setattr(model, "rewrite", lambda *a, **k: (BETTER, None))
        # The real path runs the model off the utterance thread; run it inline
        # so the assertion is about the choreography, not about a race.
        monkeypatch.setattr(
            session, "_propose_off_thread",
            lambda a, instruction: session.propose_and_stage(a, instruction))

        assert session.handle_utterance(app, "make that more formal") is True
        assert window.text == SRC, "staged, nothing applied"
        assert app.chips[-1][1] == "pending"

        assert session.handle_utterance(app, "apply") is True
        assert window.text == BETTER
        assert app.chips[-1][1] == "success"

        assert manager._do_scratch_that() is True
        assert window.text == SRC
