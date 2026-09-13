"""Sleep phrases end the latched hands-free session from any lane, match the
WHOLE utterance only, and never discard a staged draft (SAMSARA_VISION.md
section 1; Astra review #10). Real SessionModeManager, fake side effects."""
import pytest

from samsara import session_modes as sm
from samsara.session_modes import (
    GLOBAL_SESSION_EXIT_PHRASES,
    SESSION_SLEEP_PHRASES,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
    outcome_chip,
)

SIGNALS = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


class _Rig:
    def __init__(self, extra_sleep_phrases=None, abort_phrases=None):
        self.aborts = 0
        self.injected = []
        self.manager = SessionModeManager(
            abort_phrases=list(abort_phrases if abort_phrases is not None
                               else ["cancel", *GLOBAL_SESSION_EXIT_PHRASES]),
            foreground_exe_resolver=lambda: "obsidian.exe",
            foreground_hwnd_resolver=lambda: 7,
            inject_fn=self._inject,
            remove_chars_fn=lambda n: None,
            command_dispatch_fn=lambda text: sm.CommandDispatchResult(matched=False),
            agent_dispatch_fn=lambda *a: None,
            on_abort=self._abort,
            buffer_dictate_until_commit=True,
            extra_sleep_phrases=extra_sleep_phrases,
        )

    def _inject(self, text, commit_focus_guard=None):
        self.injected.append(text)
        return text

    def _abort(self):
        # What dictation.py's on_abort does: exit_command_mode() -> reset().
        self.aborts += 1
        self.manager.reset()

    def open_session(self):
        # What dictation.py's enter_command_mode does for a toggle session.
        self.manager.reset(initial_mode=SessionMode.DICTATE)

    def say(self, text):
        return self.manager.dispatch_utterance(text, SIGNALS)


def test_sleep_phrases_are_global_exit_phrases():
    assert set(SESSION_SLEEP_PHRASES) == {"go to sleep", "samsara sleep", "sleep now"}
    assert set(SESSION_SLEEP_PHRASES) <= set(GLOBAL_SESSION_EXIT_PHRASES)
    # The existing exits are unchanged.
    assert GLOBAL_SESSION_EXIT_PHRASES[:3] == ("stop listening", "exit hands free", "exit command mode")


@pytest.mark.parametrize("phrase", ["go to sleep", "Go to sleep.", "samsara sleep", "Sleep now!",
                                    "um, go to sleep"])
@pytest.mark.parametrize("lane", [SessionMode.DICTATE, SessionMode.COMMAND, SessionMode.AVA])
def test_sleep_exits_from_any_lane_as_mode_switch_asleep(phrase, lane):
    rig = _Rig()
    rig.open_session()
    rig.manager.mode = lane
    outcome = rig.say(phrase)
    assert outcome.kind == "mode_switch"
    assert outcome.detail["mode"] == "asleep" and outcome.detail["sleep"] is True
    assert rig.aborts == 1
    assert outcome_chip(outcome.kind, outcome.detail) == ("asleep", "accent")


@pytest.mark.parametrize("prose", [
    "the kids need to go to sleep",
    "go to sleep early tonight",
    "I told samsara sleep is overrated",
])
def test_sleep_inside_a_sentence_is_dictation_not_an_exit(prose):
    rig = _Rig()
    rig.open_session()
    outcome = rig.say(prose)
    assert rig.aborts == 0
    assert outcome.kind == "dictate_staged"
    assert rig.manager.dictate_pending_buffer.strip() == prose


def test_staged_draft_survives_sleep_and_returns_on_the_next_session():
    rig = _Rig()
    rig.open_session()
    rig.say("First half of the thought.")
    rig.say("second half")
    staged = rig.manager.dictate_pending_buffer
    assert staged

    outcome = rig.say("go to sleep")
    assert outcome.detail["draft_retained_chars"] == len(staged)
    assert rig.manager.dictate_pending_buffer == ""      # session state reset...
    assert rig.manager.retained_draft == staged          # ...draft set aside
    assert rig.injected == []                            # nothing typed

    rig.open_session()
    assert rig.manager.dictate_pending_buffer == staged
    assert rig.manager.retained_draft == ""
    # The restored draft is a normal pending thought: scratch pops a chunk, end commits.
    assert rig.say("scratch that").kind == "scratch_success"
    assert rig.manager.dictate_pending_buffer == "First half of the thought."
    assert rig.say("end").kind == "dictate_committed"
    assert rig.injected == ["First half of the thought."]


def test_exit_resets_do_not_restore_and_explicit_discard_destroys():
    rig = _Rig()
    rig.open_session()
    rig.say("keep me")
    rig.say("sleep now")
    rig.manager.reset()                                  # another exit-style reset
    assert rig.manager.retained_draft == "keep me"
    rig.manager.discard_retained_draft()
    rig.open_session()
    assert rig.manager.dictate_pending_buffer == ""


def test_sleep_with_nothing_staged_retains_nothing():
    rig = _Rig()
    rig.open_session()
    outcome = rig.say("go to sleep")
    assert outcome.detail["draft_retained_chars"] == 0
    assert rig.manager.retained_draft == ""


def test_other_exit_phrases_keep_their_anywhere_match_and_abort_outcome():
    rig = _Rig()
    rig.open_session()
    assert rig.say("okay stop listening now").kind == "abort"
    assert rig.aborts == 1


def test_configured_abort_phrases_add_whole_utterance_sleep_exits():
    rig = _Rig(extra_sleep_phrases=["that's all folks"])
    rig.open_session()
    rig.say("draft text")
    assert rig.say("That's all, folks").kind == "mode_switch"
    assert rig.manager.retained_draft == "draft text"
    rig2 = _Rig(extra_sleep_phrases=["that's all folks"])
    rig2.open_session()
    assert rig2.say("and that's all folks for today").kind == "dictate_staged"


def test_sleep_phrase_passed_as_an_abort_phrase_is_still_whole_utterance():
    # dictation.py passes GLOBAL_SESSION_EXIT_PHRASES in abort_phrases.
    rig = _Rig(abort_phrases=["go to sleep"])
    rig.open_session()
    assert rig.say("time to go to sleep").kind == "dictate_staged"
    assert rig.aborts == 0
