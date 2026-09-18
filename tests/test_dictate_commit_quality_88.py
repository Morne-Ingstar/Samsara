"""Queue 88: the commit must never hand back text worse than the fragments it
replaces, and a draft recovery must show up where the owner is looking.

Two live defects, both from 2026-09-15:

A. 16:23:58. The hands-free commit re-decoded a 107.6 s stitched buffer
   (under the 120 s cap, so it ran) and pasted 1,140 characters with almost
   all the punctuation gone -- while the staged fragments it replaced were
   correctly punctuated sentence by sentence. Queue 81's ellipsis join was
   NOT involved: only two seams in that whole session joined, both by the
   clauses 81 documents, and the re-decode replaces the joined text wholesale
   anyway.

B. 16:19:33. "bring back my draft" after a cancel restored the session's
   staged buffer -- the log says so and the eventual commit begins with
   exactly the recovered text -- but the live preview box stayed empty,
   because it is rebuilt on every session entry and only ever learns about
   text through on_utterance_final. Success was reported, nothing appeared.

Pure session_modes / streaming tests: `dictation` is never imported (Samsara
is running while these run).
"""

import types

import pytest

from samsara.session_modes import (
    RECOVER_DRAFT_PHRASES,
    SPOKEN_NOTICES_EVERYTHING,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
    outcome_chip,
    redecode_is_poorer,
    sentence_mark_density,
)
from samsara.streaming import DictatePreviewSession

GOOD_SIGNALS = UtteranceSignals(
    has_contiguous_speech=True, transcript_confident=True, compression_ratios=(1.2,),
)


# ---------------------------------------------------------------------------
# The evidence: what was staged at 16:21-16:23 and what the commit produced.
# Copied verbatim from ~/.samsara/logs/samsara.log.
# ---------------------------------------------------------------------------

#: The six pairs the brief quotes, as (staged fragment, what the commit made
#: of it). Each fragment was logged as its own `dictate_staged` outcome.
EVIDENCE_FRAGMENTS = [
    " When they come up, the, the window's faded, so you can't really see it.",
    " I mean, if you squint, you kind of can.",
    " I'm assuming we haven't.",
    " The streaming window has instantly become like three times.",
    " Nicer.",
    " Could we have something that lets us move the location of the streaming"
    " window, just like the indicator?",
]

#: The corresponding stretch of the 1,140-character commit, verbatim.
EVIDENCE_REDECODE = (
    "When they come up the the windows faded, so you can't really see it. I mean if you"
    " squint you kind of can I'm assuming we haven't You know blah blah blah I guess I'll"
    " just keep talking because of your number four there Show numbers in the cloud desktop"
    " app with the fade and the click through The streaming window has instantly become like"
    " three times nicer oh Could we have something that lets us move the location of the"
    " streaming window just like the indicator"
)

#: 16:27:39, same session, seven minutes later: a re-decode that was BETTER
#: than its fragments and must keep winning.
GOOD_STAGED = (
    "Well, here's a thought. Seeing as that marquee of hints, all of which are useful."
    " Uh, it's kinda useless right now because it's on an opaque window."
    " That might be a good idea for something to add to the home window."
    " Um, certainly sounds more useful than the top card it currently is."
    " Just like a a streaming sequence of like a hundred different hints. About"
    " different useful commands and little tidbits about features and"
    " like, hey, did you know you could? Enable this or if you're"
    " getting cut off, then maybe you should adjust this or blah blah blah blah blah,"
    " you know?"
)
GOOD_REDECODE = (
    "Well, here's a thought. Seeing as that marquee of hints, all of which are useful, is"
    " kind of useless right now because it's on an opaque window, that might be a good idea"
    " for something to add to the home window. It certainly sounds more useful than the top"
    " card it currently is. Just like a streaming sequence of like a hundred different hints"
    " about different useful commands and little tidbits about features and, hey, did you"
    " know you could enable this or if you're getting cut off, then maybe you should adjust"
    " this or blah blah blah blah blah. You know? "
)

#: 16:34:31: the staged fragments had NO sentence punctuation at all and the
#: re-decode supplied it. This is the case the re-decode exists for.
UNPUNCTUATED_STAGED = (
    "in the warp terminal panes like for the code sessions I'm running"
    " warp assigns each one like a title um and I wasn't sure where it got that title from"
    " but i scrolled up to the very top like the first prompt I submitted in that terminal"
    " and it seems like that title was a description or like a short description of"
    " what that first prompt was about uh how does warp get those titles for the"
    " terminal windows and is there a possibility of us being in control of them that way"
    " I always know which prompt is currently being worked on instead of just the first one"
    " that happened in that terminal window"
)
UNPUNCTUATED_REDECODE = (
    "In the warp terminal panes, like for the code sessions I'm running, warp assigns each"
    " one like a title. And I wasn't sure where it got that title from, but I scrolled up to"
    " the very top, like the first prompt I submitted in that terminal, and it seems like"
    " that title was a description, or like a short description of what that first prompt was"
    " about. How does warp get those titles for the terminal windows? And is there a"
    " possibility of us being in control of them? That way I always know which prompt is"
    " currently being worked on instead of just the first one that happened in that terminal"
    " window. "
)

#: 16:50:34: the second degraded commit of the same afternoon, 41.9 s of audio
#: -- so this is not a duration threshold, it is the transcript itself.
SHORT_BAD_STAGED = (
    "Check 1-2. Check 1-2. Check 1-2. Ok, well we might need to change those again because"
    " I just found out that now that the streaming window increases in size."
    " Once it goes up, it doesn't shrink back down again. It stays large."
    " So like right now, all the things I'm saying, I can't see because they're at the top"
    " of the window, and I believe it just keeps going up. Yeah, it's a little bit odd."
    " But I love the clear draft button, and I love the scroll bar. That's awesome."
)
SHORT_BAD_REDECODE = (
    "Check one two check one two check one two okay well we might need to change those again"
    " because I just found out that now that the streaming window increases in size once it"
    " goes up it doesn't shrink back down again it stays large so like right now all the"
    " things I'm saying I can't see because they're at the top of the window and I believe it"
    " just keeps going up yeah it's a little bit odd but I love the clear draft button and I"
    " love the scroll bar that's awesome. "
)


# ---------------------------------------------------------------------------
# A. The decision rule, on its own.
# ---------------------------------------------------------------------------

class TestRedecodeQualityRule:
    def test_the_incident_redecode_is_rejected(self):
        staged = "".join(EVIDENCE_FRAGMENTS)
        assert redecode_is_poorer(staged, EVIDENCE_REDECODE)

    def test_the_second_degraded_commit_is_rejected(self):
        assert redecode_is_poorer(SHORT_BAD_STAGED, SHORT_BAD_REDECODE)

    def test_a_good_redecode_is_kept(self):
        assert not redecode_is_poorer(GOOD_STAGED, GOOD_REDECODE)

    def test_a_redecode_that_supplies_missing_punctuation_is_kept(self):
        """The whole point of re-decoding: staged text with no sentence marks
        at all can only be improved, so the rule must never reject it."""
        assert sentence_mark_density(UNPUNCTUATED_STAGED) == 0.0
        assert not redecode_is_poorer(UNPUNCTUATED_STAGED, UNPUNCTUATED_REDECODE)

    def test_a_run_on_is_kept_when_the_staged_text_is_no_better(self):
        """Both guards must fire. A staged buffer that is itself a run-on has
        nothing to offer, so the re-decode still wins."""
        staged = " ".join(["word"] * 120)
        redecoded = " ".join(["word"] * 118) + "."
        assert not redecode_is_poorer(staged, redecoded)

    def test_one_stray_full_stop_in_the_staged_text_is_not_an_advantage(self):
        """REDECODE_MIN_STAGED_MARKS: a single mark is noise, not punctuation."""
        staged = " ".join(["word"] * 60) + ". " + " ".join(["word"] * 60)
        redecoded = " ".join(["word"] * 118)
        assert not redecode_is_poorer(staged, redecoded)

    @pytest.mark.parametrize("staged,redecoded", [
        ("", "anything at all here."),
        ("Something staged. Here.", ""),
        ("   ", "Some text."),
    ])
    def test_empty_sides_never_reject(self, staged, redecoded):
        assert not redecode_is_poorer(staged, redecoded)

    def test_density_counts_every_sentence_mark(self):
        assert sentence_mark_density("One. Two? Three! Four…") == 1.0
        assert sentence_mark_density("no marks here at all") == 0.0
        assert sentence_mark_density("") == 0.0


# ---------------------------------------------------------------------------
# A. The commit path, end to end through a real SessionModeManager.
# ---------------------------------------------------------------------------

def _make_manager(redecode_fn, injected):
    manager = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 4242,
        inject_fn=lambda text, check=None: injected.append(text) or True,
        remove_chars_fn=lambda n: True,
        command_dispatch_fn=lambda text: None,
        agent_dispatch_fn=lambda text, context=None: None,
        buffer_dictate_until_commit=True,
        commit_redecode_fn=redecode_fn,
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    return manager


def _stage_all(manager, fragments):
    for fragment in fragments:
        manager.dispatch_utterance(fragment.strip(), GOOD_SIGNALS)


class TestCommitPrefersTheBetterText:
    def test_the_six_evidence_pairs_keep_their_punctuation(self):
        """The bar from the brief: committing the six staged fragments must
        preserve at least the punctuation they already had."""
        injected = []
        manager = _make_manager(lambda text, audio: EVIDENCE_REDECODE, injected)
        _stage_all(manager, EVIDENCE_FRAGMENTS)
        staged = manager.dictate_pending_buffer

        outcome = manager.commit_pending_dictation()

        assert outcome.kind == "dictate_committed"
        committed = injected[-1]
        assert committed == staged
        for fragment in EVIDENCE_FRAGMENTS:
            assert fragment.strip() in committed
        assert sentence_mark_density(committed) >= sentence_mark_density(staged)

    def test_a_poorer_redecode_falls_back_to_the_staged_text(self):
        injected = []
        manager = _make_manager(lambda text, audio: SHORT_BAD_REDECODE, injected)
        manager.dispatch_utterance(SHORT_BAD_STAGED, GOOD_SIGNALS)

        manager.commit_pending_dictation()

        assert injected[-1].strip() == SHORT_BAD_STAGED.strip()

    def test_a_better_redecode_still_replaces_the_staged_text(self):
        injected = []
        manager = _make_manager(lambda text, audio: GOOD_REDECODE, injected)
        manager.dispatch_utterance(GOOD_STAGED, GOOD_SIGNALS)

        manager.commit_pending_dictation()

        assert injected[-1] == GOOD_REDECODE

    def test_a_redecode_of_unpunctuated_fragments_still_wins(self):
        injected = []
        manager = _make_manager(lambda text, audio: UNPUNCTUATED_REDECODE, injected)
        manager.dispatch_utterance(UNPUNCTUATED_STAGED, GOOD_SIGNALS)

        manager.commit_pending_dictation()

        assert injected[-1] == UNPUNCTUATED_REDECODE

    def test_no_redecode_at_all_still_commits_the_staged_text(self):
        injected = []
        manager = _make_manager(lambda text, audio: None, injected)
        manager.dispatch_utterance("A sentence. And another one.", GOOD_SIGNALS)

        manager.commit_pending_dictation()

        assert injected[-1] == "A sentence. And another one."

    def test_a_failing_redecode_still_commits_the_staged_text(self):
        def _boom(text, audio):
            raise RuntimeError("model went away")

        injected = []
        manager = _make_manager(_boom, injected)
        manager.dispatch_utterance("A sentence. And another one.", GOOD_SIGNALS)

        manager.commit_pending_dictation()

        assert injected[-1] == "A sentence. And another one."


# ---------------------------------------------------------------------------
# B. "bring back my draft" across a session boundary, and in the box.
# ---------------------------------------------------------------------------

class _FakeOverlay:
    def __init__(self):
        self.calls = []
        self.prompts = []

    def set_transcript(self, finalized, partial, link_words=False):
        self.calls.append((list(finalized), partial))

    def set_prompt(self, text):
        self.prompts.append(text)

    def set_interaction_callbacks(self, on_word_clicked, on_clear):
        pass

    def scroll_draft(self, where):
        pass

    def show(self):
        pass

    def close(self):
        pass


def _make_recovery_manager():
    """Wired like dictation.py's, with the abort ending the session exactly as
    exit_command_mode does (reset), so the session boundary is real."""
    spoken = []
    injected = []

    def _on_abort():
        manager.reset(SessionMode.DICTATE)

    manager = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 4242,
        inject_fn=lambda text, check=None: injected.append(text) or True,
        remove_chars_fn=lambda n: True,
        command_dispatch_fn=lambda text: None,
        agent_dispatch_fn=lambda text, context=None: None,
        buffer_dictate_until_commit=True,
        on_abort=_on_abort,
        speak_fn=lambda text, category="confirmation": spoken.append(text),
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    manager.spoken, manager.injected = spoken, injected
    return manager


def _make_preview(manager):
    """The real DictatePreviewSession with a fake overlay -- same construction
    tests/test_streaming_preview_box.py uses, so this exercises production
    code, not a re-implementation."""
    app = types.SimpleNamespace(_session_mode_manager=manager)
    app._ensure_session_mode_manager = lambda: manager
    preview = DictatePreviewSession.__new__(DictatePreviewSession)
    preview.app = app
    preview._closed = False
    preview._finalized = []
    preview._generation = 0
    preview._overlay = _FakeOverlay()
    return preview


def _drive(manager, preview, text):
    """One utterance, through dispatch and into the preview, with exactly the
    flags dictation.py passes."""
    outcome = manager.dispatch_utterance(text, GOOD_SIGNALS)
    preview.on_utterance_final(
        text,
        scratch_success=(outcome.kind == "scratch_success"),
        dictate_committed=(outcome.kind == "dictate_committed"),
        draft_recovered=(outcome.kind == "dictate_draft_recovered"),
    )
    return outcome


class TestRecoveryAcrossTheSessionBoundary:
    def test_the_incident_the_draft_comes_back_into_the_box(self):
        """2026-09-15 16:19: cancel, session ends, hands-free restarts, "bring
        back my draft". The draft came back into the buffer even before this
        fix -- what did nothing was the box."""
        manager = _make_recovery_manager()
        preview = _make_preview(manager)
        _drive(manager, preview, "Have it stop listening to you or something like that")
        draft = manager.dictate_pending_buffer
        assert preview._finalized  # the box shows it while it is being dictated

        _drive(manager, preview, "cancel")            # session ends here
        preview_after_restart = _make_preview(manager)  # a new session: empty box
        assert preview_after_restart._finalized == []

        outcome = _drive(manager, preview_after_restart, "bring back my draft")

        assert outcome.kind == "dictate_draft_recovered"
        assert manager.dictate_pending_buffer == draft
        assert preview_after_restart._finalized == [draft]
        assert preview_after_restart._overlay.calls[-1] == ([draft], "")

    def test_recovery_does_not_need_the_original_session_to_exist(self):
        """The owner's hypothesis, tested directly: the slot outlives reset()."""
        manager = _make_recovery_manager()
        _drive_text = "A whole thought worth keeping."
        manager.dispatch_utterance(_drive_text, GOOD_SIGNALS)
        manager.dispatch_utterance("cancel", GOOD_SIGNALS)

        manager.reset(SessionMode.DICTATE)   # and again, for good measure
        manager.reset(SessionMode.DICTATE)

        outcome = manager.dispatch_utterance("bring back my draft", GOOD_SIGNALS)
        assert outcome.kind == "dictate_draft_recovered"
        assert _drive_text in manager.dictate_pending_buffer

    def test_a_recovery_onto_a_newer_draft_shows_both_in_order(self):
        manager = _make_recovery_manager()
        manager.dispatch_utterance("First thought.", GOOD_SIGNALS)
        manager.dispatch_utterance("cancel", GOOD_SIGNALS)
        preview = _make_preview(manager)
        _drive(manager, preview, "Second thought.")

        _drive(manager, preview, "bring back my draft")

        shown = preview._finalized[0]
        assert shown.index("First thought.") < shown.index("Second thought.")
        assert shown == manager.dictate_pending_buffer


class TestEmptySlotNeverReportsSuccess:
    def test_the_phrase_says_so_and_reports_nothing_recovered(self):
        manager = _make_recovery_manager()

        outcome = manager.dispatch_utterance("bring back my draft", GOOD_SIGNALS)

        assert outcome.kind == "dictate_recover_nothing"
        assert "no draft to bring back" not in " ".join(manager.spoken)
        assert outcome_chip("dictate_recover_nothing", {}) == (
            "nothing to bring back", "warning")

    def test_the_acknowledgement_is_spoken_when_everything_is_allowed(self):
        manager = _make_recovery_manager()
        manager.set_spoken_notices(SPOKEN_NOTICES_EVERYTHING)

        outcome = manager.dispatch_utterance("bring back my draft", GOOD_SIGNALS)

        assert outcome.kind == "dictate_recover_nothing"
        assert "no draft to bring back" in " ".join(manager.spoken)

    @pytest.mark.parametrize("phrase", RECOVER_DRAFT_PHRASES)
    def test_the_phrase_is_never_left_in_the_box_as_dictation(self, phrase):
        """Queue 88: with an empty slot nothing resyncs, so the words would
        otherwise be appended as if the owner had dictated them -- under a chip
        that says nothing was brought back."""
        manager = _make_recovery_manager()
        preview = _make_preview(manager)

        outcome = _drive(manager, preview, phrase)

        assert outcome.kind == "dictate_recover_nothing"
        assert preview._finalized == []
        assert manager.dictate_pending_buffer == ""

    def test_a_whitespace_only_draft_is_never_stashed(self):
        """Nothing to put back must read as nothing to put back, not as a
        success that restores an empty string."""
        manager = _make_recovery_manager()
        manager._dictate_pending_buffer = "   \n  "

        assert manager._stash_recoverable_draft("cancel") == 0
        assert manager.recoverable_draft == ""

    def test_a_blank_slot_written_by_anything_else_is_refused(self):
        manager = _make_recovery_manager()
        manager._recoverable_draft = {
            "buffer": "   ", "audio": [], "last_ended_terminal": None,
            "stack_items": [], "source": "cancel", "stashed_at": manager._clock(),
        }

        outcome = manager.dispatch_utterance("bring back my draft", GOOD_SIGNALS)

        assert outcome.kind == "dictate_recover_nothing"
        assert manager._recoverable_draft is None
