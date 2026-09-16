"""Queue 100: the trailing wrap's scope, idempotence, and the 2026-09-15
19:52 "in quotes across two utterances" incident.

The incident, from the live log (hands-free DICTATE):

    19:52:53  [CMD-UTT] "Thank you."
              [SESSION] dictate_staged text='Thank you.'   pending_chars=10
    19:52:54  [CMD-UTT] "In quotes."
              [SESSION] dictate_staged text=' In quotes.'  pending_chars=21
    19:52:56  [CMD-UTT] "End."
              [PASTE] Ctrl+V sent chars=13
              [SESSION] dictate_committed text='Thank you.". '  chars=13

WHAT ACTUALLY PRODUCED THAT STRING. Not a double application of the wrap: on
the buffered hands-free lane the token pass runs exactly once, at commit, and
over the real staged chunks it delivers the wrapped draft. The incident string
is character-for-character `apply_formatting_tokens('Thank you. Close quote. ')`
-- the `close quote` SIMPLE token emitting one explicit delimiter. So the words
that reached the token pass were "Close quote", not "In quotes", and the only
step on this path that can replace the staged words wholesale is the commit
re-decode (dictation._dictate_commit_redecode, command_mode.dictate_commit_
redecode, default True). That half is out of this queue's scope and is reported,
not fixed; `test_incident_shape_is_pinned` pins it so a future change to it is
deliberate.

WHAT WAS FIXED HERE (see samsara/formatting_tokens.py's module docstring):
  * the wrap's scope is documented as option (a) -- the whole text handed to
    the single delivery-time call, which on the buffered lane is the whole
    staged draft;
  * a wrap is emitted only by _wrap(), which cannot emit half of one;
  * apply_formatting_tokens is now idempotent (it was not: a substituted
    "new line" moved a mid-text trigger into a trailing position on a second
    pass), which matters because retype_last_suppressed really does run the
    pass twice over the same text;
  * the wrap no longer swallows the add_trailing_space separator.

These tests drive the REAL SessionModeManager staging/commit path. The commit
`inject_fn` mirrors dictation.py's `_inject_fn` pipeline; `test_inject_pipeline
_order_still_matches_dictation` pins that order against dictation.py's source
so the mirror cannot drift silently. `dictation` itself is not imported --
Samsara is running while these tests run.
"""
import itertools
import re
from pathlib import Path

import pytest

from samsara.formatting_tokens import (
    TRAILING_WRAPS,
    apply_formatting_tokens,
)
from samsara.session_modes import (
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
)

ROOT = Path(__file__).resolve().parent.parent

SIGNALS = UtteranceSignals(
    has_contiguous_speech=True, transcript_confident=True, compression_ratios=(1.2,),
)


# ---------------------------------------------------------------------------
# The real staging/commit path
# ---------------------------------------------------------------------------

def _commit_pipeline(text: str, *, add_trailing_space: bool = True) -> str:
    """dictation.py's `_inject_fn` tail, minus the steps that need a model.

    process_transcription / clean_text / smart_correct are identity for all
    text used here (verified against the real functions in the queue 100
    reproduction, reports/100/artifacts/repro.py); what matters for scope and
    idempotence is that add_trailing_space runs and the token pass runs LAST,
    exactly once, over the complete joined buffer.
    """
    if add_trailing_space:
        text = text + " "
    return apply_formatting_tokens(text)


def _manager(delivered: list, *, redecode=None, buffered=True):
    def inject(text, commit_focus_guard=None):
        out = _commit_pipeline(text)
        delivered.append(out)
        return out

    mgr = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 4242,
        inject_fn=inject,
        format_dictate_fn=apply_formatting_tokens,
        remove_chars_fn=lambda n: None,
        command_dispatch_fn=lambda text: None,
        agent_dispatch_fn=lambda *a: None,
        buffer_dictate_until_commit=buffered,
        commit_redecode_fn=(None if redecode is None else (lambda joined, refs: redecode)),
    )
    mgr.reset(initial_mode=SessionMode.DICTATE)
    return mgr


def _stage_and_commit(chunks, *, redecode=None):
    delivered = []
    mgr = _manager(delivered, redecode=redecode)
    staged = []
    for chunk in chunks:
        outcome = mgr.dispatch_utterance(chunk, SIGNALS)
        assert outcome.kind == "dictate_staged", outcome.kind
        staged.append(outcome.detail["text"])
    buffer_text = mgr.dictate_pending_buffer
    outcome = mgr._commit_dictate_buffer(target_mode=None)
    assert outcome.kind == "dictate_committed", outcome.kind
    return staged, buffer_text, delivered[-1]


class TestTheIncident:
    INCIDENT = 'Thank you.". '

    def test_the_staged_chunks_match_the_live_log(self):
        staged, buffer_text, _ = _stage_and_commit(["Thank you.", "In quotes."])
        assert staged == ["Thank you.", " In quotes."]
        assert buffer_text == "Thank you. In quotes."
        assert len(buffer_text) == 21          # the log's pending_chars

    def test_two_chunks_wrap_the_whole_staged_draft(self):
        """Option (a): the pause is not a boundary the wrap respects."""
        _, _, delivered = _stage_and_commit(["Thank you.", "In quotes."])
        assert delivered == '"Thank you." '
        assert delivered != self.INCIDENT

    def test_the_incident_string_is_never_produced_from_these_chunks(self):
        for redecode in (None, "Thank you. In quotes.", "Thank you.  In quotes."):
            _, _, delivered = _stage_and_commit(
                ["Thank you.", "In quotes."], redecode=redecode)
            assert delivered != self.INCIDENT, redecode

    @pytest.mark.parametrize("trigger,expected", [
        ("In quotes.", '"Thank you." '),
        ("In asterisks.", "*Thank you.* "),
        ("In bold.", "**Thank you.** "),
        ("In parens.", "(Thank you.) "),
    ])
    def test_every_wrap_spans_the_two_chunks(self, trigger, expected):
        _, _, delivered = _stage_and_commit(["Thank you.", trigger])
        assert delivered == expected

    def test_incident_shape_is_pinned(self):
        """The ONE substitution that reproduces the incident byte for byte.

        Not a wrap at all -- `close quote` is an explicit delimiter the
        speaker names, and emitting it alone is correct, tested behaviour
        (tests/test_formatting_tokens.py: 'hello close quote' -> 'hello"').
        The defect is that the words reaching the pass were not the words the
        user spoke. This pins the mechanism so the upstream fix, when it is
        made, can be aimed at the right step.
        """
        _, _, delivered = _stage_and_commit(
            ["Thank you.", "In quotes."], redecode="Thank you. Close quote.")
        assert delivered == self.INCIDENT

    def test_single_utterance_still_wraps(self):
        _, _, delivered = _stage_and_commit(["Thank you. In quotes."])
        assert delivered == '"Thank you." '
        assert apply_formatting_tokens("Thank you. In quotes.") == '"Thank you."'
        assert apply_formatting_tokens("Thank you in quotes") == '"Thank you"'


class TestTrailingSeparatorSurvivesTheWrap:
    """add_trailing_space runs immediately before the token pass, and the
    trigger's tail class used to eat its space -- so every wrapped commit ran
    straight into the next one."""

    def test_wrapped_commit_keeps_its_separator(self):
        _, _, delivered = _stage_and_commit(["Thank you.", "In quotes."])
        assert delivered.endswith(" ")

    def test_two_commits_do_not_run_together(self):
        _, _, first = _stage_and_commit(["Thank you.", "In quotes."])
        _, _, second = _stage_and_commit(["And then."])
        assert (first + second).startswith('"Thank you." And then.')

    @pytest.mark.parametrize("text,expected", [
        ("hello in quotes", '"hello"'),          # nothing there to keep
        ("hello in quotes.", '"hello"'),         # punctuation belongs to the trigger
        ("hello in quotes ", '"hello" '),
        ("hello in quotes. ", '"hello" '),
        ("hello in quotes .", '"hello"'),
    ])
    def test_only_the_whitespace_run_survives(self, text, expected):
        assert apply_formatting_tokens(text) == expected


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

_CORPUS_WORDS = [
    "hello", "world", "new line", "new paragraph", "insert tab", "bullet",
    "bullet point", "asterisk", "double asterisk", "open quote", "close quote",
    "open paren", "close paren", "in quotes", "in bold", "in asterisks",
    "in parens",
]


def _corpus():
    for k in (1, 2, 3):
        for combo in itertools.product(_CORPUS_WORDS, repeat=k):
            joined = " ".join(combo)
            for variant in (joined, joined + ".", joined + " ", joined + ". "):
                yield variant


class TestIdempotence:
    def test_the_regression_case(self):
        """A substituted "new line" is whitespace, and the old tail class
        accepted whitespace -- so the second pass saw a trailing trigger that
        was never in what the user said."""
        once = apply_formatting_tokens("hello in quotes new line")
        assert once == "hello in quotes\n"      # trigger is mid-text: literal
        assert apply_formatting_tokens(once) == once

    @pytest.mark.parametrize("text", [
        "hello in quotes new line",
        "hello in quotes new paragraph",
        "hello in quotes insert tab",
        "hello in bold new line",
        "hello in parens insert tab",
    ])
    def test_a_trigger_followed_by_a_separator_token_stays_literal(self, text):
        once = apply_formatting_tokens(text)
        assert apply_formatting_tokens(once) == once
        assert '"' not in once or "quote" in text.lower()

    def test_every_corpus_input_is_idempotent(self):
        bad = []
        for text in _corpus():
            once = apply_formatting_tokens(text)
            twice = apply_formatting_tokens(once)
            if twice != once:
                bad.append((text, once, twice))
        assert not bad, f"{len(bad)} non-idempotent inputs, e.g. {bad[:3]}"

    def test_a_double_pass_over_wrapped_text_adds_no_second_wrap(self):
        for text in ("hello in quotes", "hello in bold", "hello in parens",
                     "hello in asterisks"):
            once = apply_formatting_tokens(text)
            assert apply_formatting_tokens(once) == once
            assert once.count('"') in (0, 2)

    def test_retyping_a_suppressed_chunk_does_not_reformat_it(self):
        """session_modes.retype_last_suppressed re-injects an ALREADY-formatted
        payload through inject_fn, which runs the token pass again. That second
        pass must be a no-op."""
        delivered = []
        mgr = _manager(delivered, buffered=False)
        focused = ["notepad.exe"]
        mgr._foreground_exe_resolver = lambda: focused[0]
        mgr._dictate_target_process = "notepad.exe"
        mgr._last_dictate_ended_terminal = None

        focused[0] = "explorer.exe"          # focus lock fails: chunk suppressed
        outcome = mgr.dispatch_utterance("hello in quotes new line", SIGNALS)
        assert outcome.kind == "dictate_suppressed_focus_lock"
        suppressed = mgr._stack.peek().payload
        assert suppressed == "hello in quotes\n"

        focused[0] = "notepad.exe"           # focus back: "retype that"
        assert mgr.retype_last_suppressed() is True
        # inject_fn adds the trailing space; the token pass must change nothing
        # else about the already-formatted payload.
        assert delivered[-1] == suppressed + " "


# ---------------------------------------------------------------------------
# A partial wrap must be structurally impossible
# ---------------------------------------------------------------------------

_TRIGGERS = [t for t, _b, _a in TRAILING_WRAPS]


class TestNoPartialWrap:
    def test_a_wrap_is_emitted_in_exactly_one_place(self):
        src = (ROOT / "samsara" / "formatting_tokens.py").read_text(encoding="utf-8")
        body = src.split('"""', 2)[2]        # past the module docstring
        assert body.count("_WRAPS[") == 1, (
            "_WRAPS must be read only by _wrap(); a second reader is a second "
            "place a half wrap could be emitted")
        wrap_fn = body.split("def _wrap(")[1].split("\ndef ")[0]
        returns = [ln for ln in wrap_fn.splitlines() if ln.strip().startswith("return")]
        assert len(returns) == 1, (
            "_wrap() must have one return, so it cannot emit one delimiter "
            f"without the other; found {returns}")

    def test_no_corpus_output_has_a_delimiter_the_module_invented(self):
        """Delimiters the SPEAKER asked for by name (open/close quote, open/
        close paren) are counted out; whatever is left must be balanced,
        because it can only have come from a wrap, which emits pairs."""
        bad = []
        for text in _corpus():
            out = apply_formatting_tokens(text)
            low = text.lower()
            spoken_q = low.count("open quote") + low.count("close quote")
            spoken_lp = low.count("open paren")
            spoken_rp = low.count("close paren")
            if (out.count('"') - spoken_q) % 2 != 0:
                bad.append(("quote", text, out))
            if (out.count("(") - spoken_lp) != (out.count(")") - spoken_rp):
                bad.append(("paren", text, out))
        assert not bad, f"{len(bad)} unbalanced outputs, e.g. {bad[:3]}"

    def test_commit_path_output_is_never_half_wrapped(self):
        """The same invariant over the real staging/commit path, two chunks at
        a time -- the shape the incident had."""
        firsts = ["Thank you.", "hello", "bullet one", "wait, what?"]
        seconds = [f"{t.capitalize()}." for t in _TRIGGERS] + [
            t for t in _TRIGGERS] + ["and more.", "close quote."]
        for a in firsts:
            for b in seconds:
                _, _, out = _stage_and_commit([a, b])
                spoken_q = (a + " " + b).lower().count("close quote")
                assert (out.count('"') - spoken_q) % 2 == 0, (a, b, out)
                assert out.count("(") == out.count(")"), (a, b, out)

    @pytest.mark.parametrize("trigger", _TRIGGERS)
    def test_a_trigger_with_nothing_before_it_stays_literal(self, trigger):
        """Never an empty pair, never a dropped utterance -- the words are
        typed, which is visible and undoable."""
        for text in (trigger, trigger + ".", " " + trigger + ". "):
            assert apply_formatting_tokens(text) == text


# ---------------------------------------------------------------------------
# The mirror of dictation.py's pipeline must not drift
# ---------------------------------------------------------------------------

def test_inject_pipeline_order_still_matches_dictation():
    """_commit_pipeline above mirrors dictation.py's `_inject_fn`. Pin the two
    facts it depends on: add_trailing_space runs BEFORE the token pass, and the
    token pass is the LAST thing before delivery."""
    src = (ROOT / "dictation.py").read_text(encoding="utf-8")
    inject = src.split("def _inject_fn(text: str, commit_focus_guard=None):")[1]
    inject = inject.split("\n        def ")[0]
    # Code only -- _inject_fn's own docstring names these steps in prose.
    inject = inject.split('"""', 2)[-1]
    order = [m.group(1) for m in re.finditer(
        r"(add_trailing_space|_apply_formatting_tokens|_paste_preserving_clipboard)", inject)]
    assert order.index("add_trailing_space") < order.index("_apply_formatting_tokens")
    assert (order.index("_apply_formatting_tokens")
            < order.index("_paste_preserving_clipboard"))
    assert order.count("_apply_formatting_tokens") == 1, (
        "the commit path must apply formatting tokens exactly once")


def test_every_delivery_site_applies_the_pass_once():
    """The four delivery sites, and the count at each. A second call added to
    any of them is what queue 100 was about."""
    counts = {
        "dictation.py": 3,      # _inject_fn (commit), _output_dictation, hotkey
        "samsara/streaming.py": 1,
    }
    for rel, expected in counts.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        calls = len(re.findall(r"(?<!def )_apply_formatting_tokens\(", src))
        assert calls == expected, f"{rel}: {calls} call sites, expected {expected}"
