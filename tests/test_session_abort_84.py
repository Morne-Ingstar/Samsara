"""Queue 84: destructive session controls are whole-utterance, and no abort or
clear destroys a draft irrecoverably.

The incident this file exists for (2026-09-15 15:12:29): "Have it stop
listening to you, or something like that." matched the abort phrase "stop
listening" from inside the sentence, ended the session and discarded a
477-character draft. The owner re-dictated it 22 s later.

Pure session_modes tests -- no dictation import (Samsara runs while these run).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import session_modes as sm
from samsara.session_modes import (
    GLOBAL_SESSION_EXIT_PHRASES, RECOVER_DRAFT_PHRASES, RECOVERABLE_DRAFT_TTL_S,
    SessionMode, UtteranceSignals, is_recover_draft, outcome_chip,
)

#: The utterance that destroyed the draft.
INCIDENT_SENTENCE = "Have it stop listening to you, or something like that."

GOOD_SIGNALS = UtteranceSignals(
    has_contiguous_speech=True, transcript_confident=True, compression_ratios=(1.2,),
)
NOISY_SIGNALS = UtteranceSignals(
    has_contiguous_speech=False, transcript_confident=False, compression_ratios=(4.0,),
)

DEFAULT_ABORTS = ["cancel", "cancel dictation", "abort", *GLOBAL_SESSION_EXIT_PHRASES]


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_manager(abort_phrases=None, extra_sleep_phrases=None, clock=None):
    """A DICTATE-lane manager wired like dictation.py's, with the session end
    doing what exit_command_mode does: reset()."""
    spoken = []
    injected = []
    aborted = []
    stopped = []
    clock = clock or _Clock()

    def _on_abort():
        aborted.append(True)
        manager.reset(SessionMode.DICTATE)

    manager = sm.SessionModeManager(
        abort_phrases=list(abort_phrases if abort_phrases is not None else DEFAULT_ABORTS),
        extra_sleep_phrases=extra_sleep_phrases,
        foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 4242,
        inject_fn=lambda text, focused=True: injected.append(text),
        remove_chars_fn=lambda n: True,
        command_dispatch_fn=lambda text: None,
        agent_dispatch_fn=lambda text: None,
        buffer_dictate_until_commit=True,
        on_abort=_on_abort,
        stop_fn=lambda reason: stopped.append(reason) or {},
        speak_fn=lambda text, category="confirmation": spoken.append(text),
        clock=clock,
    )
    manager.reset(SessionMode.DICTATE)
    manager.spoken, manager.injected, manager.aborted = spoken, injected, aborted
    manager.stopped, manager.clock = stopped, clock
    return manager


def stage(manager, text="The quarterly plan needs another paragraph before Friday."):
    outcome = manager.dispatch_utterance(text, GOOD_SIGNALS)
    assert outcome.kind == "dictate_staged", outcome.kind
    return outcome


# ---------------------------------------------------------------------------
# 1. Whole-utterance matching
# ---------------------------------------------------------------------------

def test_the_incident_sentence_does_not_end_the_session_or_touch_the_draft():
    manager = make_manager()
    stage(manager)
    before = manager._dictate_pending_buffer

    outcome = manager.dispatch_utterance(INCIDENT_SENTENCE, GOOD_SIGNALS)

    assert outcome.kind == "dictate_staged"
    assert manager.aborted == []
    assert manager._dictate_pending_buffer.startswith(before)
    assert INCIDENT_SENTENCE in manager._dictate_pending_buffer


@pytest.mark.parametrize("phrase", ["cancel", "cancel dictation", "abort", "stop listening",
                                    "exit hands free", "exit command mode"])
def test_a_bare_destructive_phrase_still_aborts(phrase):
    manager = make_manager()
    stage(manager)

    outcome = manager.dispatch_utterance(phrase, GOOD_SIGNALS)

    assert outcome.kind == "abort"
    assert manager.aborted == [True]


@pytest.mark.parametrize("spoken", ["Cancel.", " CANCEL ", "cancel!", "um cancel", "okay abort"])
def test_case_punctuation_and_filler_words_still_abort(spoken):
    manager = make_manager()

    assert manager.dispatch_utterance(spoken, GOOD_SIGNALS).kind == "abort"


@pytest.mark.parametrize("sentence", [
    INCIDENT_SENTENCE,
    "We should cancel the meeting tomorrow.",
    "Tell it to abort the launch sequence.",
    "I will exit command mode when I am done.",
    "Please cancel dictation now.",
    "You can exit hands free mode from the tray.",
    "file a cancelation request",
])
def test_sentences_containing_a_phrase_are_dictation(sentence):
    manager = make_manager()

    outcome = manager.dispatch_utterance(sentence, GOOD_SIGNALS)

    assert outcome.kind == "dictate_staged"
    assert manager.aborted == []


def test_the_abort_path_works_mid_draft_and_is_not_gated():
    """The abort must survive bad audio: a user who cannot type must never be
    stuck in a latched session because the anti-hallucination gate distrusted
    the microphone."""
    manager = make_manager()
    stage(manager)

    outcome = manager.dispatch_utterance("cancel", NOISY_SIGNALS)

    assert outcome.kind == "abort"
    assert manager.aborted == [True]


def test_user_configured_abort_phrases_end_the_session_whole_utterance_only():
    manager = make_manager(extra_sleep_phrases=["that's all folks"])

    assert manager.dispatch_utterance("that's all folks", GOOD_SIGNALS).kind == "mode_switch"
    manager2 = make_manager(extra_sleep_phrases=["that's all folks"])
    assert manager2.dispatch_utterance(
        "and that's all folks for today", GOOD_SIGNALS).kind == "dictate_staged"


def test_a_bare_stop_is_dictated_now_that_the_stop_word_changed():
    """Queue 116. The stop is whole-utterance only, so "stop the music" was
    never at risk -- but a bare "Stop." is common in real dictation, and once
    the branch is live losing it means the word is eaten instead of typed."""
    manager = make_manager()
    stage(manager)
    outcome = manager.dispatch_utterance("Stop.", GOOD_SIGNALS)
    assert outcome.kind != "stopped"
    assert "stop" in manager._dictate_pending_buffer.lower()


def test_every_session_ending_phrase_is_whole_utterance():
    """The full list, so a phrase added later cannot quietly go back to
    matching inside a sentence."""
    manager = make_manager()
    ending = [*DEFAULT_ABORTS, *sm.SESSION_SLEEP_PHRASES, *sm.SESSION_STOP_PHRASES]
    for phrase in ending:
        assert manager._matches_abort_phrase(phrase) or phrase in sm.SESSION_STOP_PHRASES, phrase
        assert not manager._matches_abort_phrase(f"I said {phrase} to the app just now"), phrase
    assert sm.ABORT_PHRASES_ARE_WHOLE_UTTERANCE is True
    assert not hasattr(manager, "_abort_patterns")


def test_stop_and_sleep_keep_their_existing_behaviour():
    manager = make_manager()
    stage(manager)

    # Queue 116 changed the WORD (("stop",) -> ("halt", "cease")) and wired
    # the branch up for real; the behaviour this test pins is unchanged, so it
    # reads the constant instead of a literal and survives the next change.
    stop = manager.dispatch_utterance(sm.SESSION_STOP_PHRASES[0], GOOD_SIGNALS)
    assert stop.kind == "stopped" and manager.aborted == []
    assert manager._dictate_pending_buffer                     # draft untouched

    sleep = manager.dispatch_utterance("go to sleep", GOOD_SIGNALS)
    assert sleep.kind == "mode_switch" and sleep.detail["sleep"] is True
    assert sleep.detail["draft_retained_chars"] > 0
    # on_abort -> reset(DICTATE) -- a sleep draft is auto-restored there, and
    # that difference from an aborted draft is the point (see the test below).
    assert manager._dictate_pending_buffer


# ---------------------------------------------------------------------------
# 2. Nothing discards a draft irrecoverably
# ---------------------------------------------------------------------------

def test_an_aborted_draft_is_kept_and_can_be_brought_back():
    manager = make_manager()
    # Draft recovery is an ACKNOWLEDGEMENT. The default spoken-notices policy
    # speaks questions only; use the explicit accessibility setting here to
    # keep asserting the recovery guidance is available aloud.
    manager.set_spoken_notices(sm.SPOKEN_NOTICES_EVERYTHING)
    stage(manager)
    draft = manager._dictate_pending_buffer

    outcome = manager.dispatch_utterance("cancel", GOOD_SIGNALS)

    assert outcome.detail["recoverable_chars"] == len(draft)
    assert manager._dictate_pending_buffer == ""               # the session ended
    assert manager.recoverable_draft == draft
    assert any("draft is kept" in line for line in manager.spoken)

    recovered = manager.dispatch_utterance(RECOVER_DRAFT_PHRASES[0], GOOD_SIGNALS)

    assert recovered.kind == "dictate_draft_recovered"
    assert recovered.detail["source"] == "cancel"
    assert manager._dictate_pending_buffer == draft
    assert manager.recoverable_draft == ""                     # taken, not duplicated


@pytest.mark.parametrize("phrase", RECOVER_DRAFT_PHRASES)
def test_every_recovery_phrase_works(phrase):
    manager = make_manager()
    stage(manager)
    manager.dispatch_utterance("cancel", GOOD_SIGNALS)

    assert manager.dispatch_utterance(phrase, GOOD_SIGNALS).kind == "dictate_draft_recovered"


def test_recovered_text_goes_in_front_of_a_draft_started_since():
    manager = make_manager()
    stage(manager, "First thought.")
    manager.dispatch_utterance("cancel", GOOD_SIGNALS)
    stage(manager, "Second thought.")

    outcome = manager.dispatch_utterance("bring back my draft", GOOD_SIGNALS)

    assert outcome.detail["prepended"] is True
    buffer = manager._dictate_pending_buffer
    assert buffer.index("First thought.") < buffer.index("Second thought.")


def test_recovery_is_refused_plainly_when_there_is_nothing_to_recover():
    manager = make_manager()
    manager.set_spoken_notices(sm.SPOKEN_NOTICES_EVERYTHING)

    outcome = manager.dispatch_utterance("bring back my draft", GOOD_SIGNALS)

    assert outcome.kind == "dictate_recover_nothing"
    assert manager._dictate_pending_buffer == ""
    assert "no draft to bring back" in " ".join(manager.spoken)


def test_a_recoverable_draft_expires():
    manager = make_manager()
    stage(manager)
    manager.dispatch_utterance("cancel", GOOD_SIGNALS)

    manager.clock.advance(RECOVERABLE_DRAFT_TTL_S + 1)

    assert manager.recoverable_draft == ""
    assert manager.dispatch_utterance(
        "bring back my draft", GOOD_SIGNALS).kind == "dictate_recover_nothing"


def test_a_recovered_draft_is_not_restored_automatically_on_the_next_session():
    """Unlike a draft set aside by "go to sleep": an abort is usually meant,
    so the next thought must not silently start with the old one."""
    manager = make_manager()
    stage(manager)
    manager.dispatch_utterance("cancel", GOOD_SIGNALS)

    manager.reset(SessionMode.DICTATE)

    assert manager._dictate_pending_buffer == ""
    assert manager.recoverable_draft != ""                     # still available on request


def test_a_confirmed_clear_is_recoverable_too():
    """Queue 80's "scratch everything" is deliberate, but a deliberate clear
    can also be a mistake."""
    manager = make_manager()
    stage(manager)
    draft = manager._dictate_pending_buffer

    asked = manager.dispatch_utterance("scratch everything", GOOD_SIGNALS)
    assert asked.kind == "dictate_clear_awaiting_confirmation"
    cleared = manager.dispatch_utterance("yes", GOOD_SIGNALS)

    assert cleared.kind == "dictate_draft_cleared"
    assert cleared.detail["recoverable_chars"] == len(draft)
    assert manager._dictate_pending_buffer == ""
    assert manager.recoverable_draft == draft

    recovered = manager.dispatch_utterance("bring back my draft", GOOD_SIGNALS)
    assert recovered.detail["source"] == "scratch everything"
    assert manager._dictate_pending_buffer == draft


def test_a_sentence_containing_the_recovery_phrase_is_dictation():
    manager = make_manager()
    stage(manager)
    manager.dispatch_utterance("cancel", GOOD_SIGNALS)

    outcome = manager.dispatch_utterance(
        "I had to restore my draft from a backup yesterday.", GOOD_SIGNALS)

    assert outcome.kind == "dictate_staged"
    assert manager.recoverable_draft != ""                     # nothing consumed it
    assert is_recover_draft("restore my draft") and not is_recover_draft("restore my draft file")


def test_a_recovery_phrase_on_distrusted_audio_is_dictated_not_acted_on():
    manager = make_manager()
    stage(manager)
    manager.dispatch_utterance("cancel", GOOD_SIGNALS)

    outcome = manager.dispatch_utterance("bring back my draft", NOISY_SIGNALS)

    assert outcome.kind == "dictate_staged"
    assert manager.recoverable_draft != ""


# ---------------------------------------------------------------------------
# Chips: the user has to see that the text still exists
# ---------------------------------------------------------------------------

def test_the_chips_say_where_the_text_went():
    assert outcome_chip("abort", {"recoverable_chars": 477})[0] == \
        "cancelled, say bring back my draft"
    assert outcome_chip("abort", {"recoverable_chars": 0}) is None
    assert outcome_chip("abort", {}) is None
    assert outcome_chip("dictate_draft_recovered", {})[0] == "draft back"
    assert outcome_chip("dictate_recover_nothing", {})[0] == "nothing to bring back"
    assert "bring back my draft" in outcome_chip("dictate_draft_cleared", {})[0]
    for kind in ("dictate_draft_recovered", "dictate_recover_nothing"):
        assert sm.is_mapped_outcome(kind), kind


# ---------------------------------------------------------------------------
# 3. Per-lane vocabulary -- the report's table, pinned
# ---------------------------------------------------------------------------

def test_the_dictation_lane_commit_vocabulary_is_exactly_end():
    """The report claims "end" is the only commit word in this lane. If a
    phrase is ever added here, this test makes that a deliberate act: every
    extra word is another way for prose to end a session."""
    assert set(sm._DICTATE_COMMIT_HOMOPHONES) == {"end"}
    assert sm.DICTATE_COMMIT_PHRASE == "end"
    manager = make_manager()
    stage(manager)
    for word in ("over", "done", "send", "and", "finish", "stop dictating"):
        outcome = manager.dispatch_utterance(word, GOOD_SIGNALS)
        assert outcome.kind != "dictate_committed", word
    assert manager.dispatch_utterance("end", GOOD_SIGNALS).kind == "dictate_committed"


def test_the_wake_lane_keeps_its_own_end_words_and_this_lane_does_not_borrow_them():
    """The two lanes differ on purpose, so the difference is read from the
    source rather than asserted from memory. dictation.py is read as text:
    importing it while Samsara runs is not allowed."""
    source = (Path(__file__).resolve().parent.parent / "dictation.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "_WAKE_SESSION_SEND_WORDS  = ['over', 'send']" in source
    assert "end_words = ww_config.get('end_words', ['over', 'done'])" in source
    # ... and none of those words is a control phrase in the dictation lane.
    manager = make_manager()
    for word in ("over", "done", "send"):
        assert not manager._matches_abort_phrase(word), word
        assert not sm.is_dictate_commit(word), word
