"""Queue 103 -- the app should not talk at you unless it is asking you something.

The incident: clicking "Clear draft" on the streaming preview produced a loud
Edge TTS voice telling the owner to say "bring back my draft" -- a sentence the
chip was already showing. Nothing in this app speaks except Ava, so a voice
arriving after a MOUSE CLICK reads as a malfunction.

Every one of the twelve `_speak` call sites in session_modes.py is classified
QUESTION or ACKNOWLEDGEMENT, and this file asserts the class AND the behaviour
of every one of them at every value the setting can take.

The `off` value does NOT exist -- see TestOffIsNotOffered for the evidence that
it would strand someone, which is why the brief's own instruction was to drop
it rather than ship it.
"""
from unittest.mock import Mock

import pytest

from samsara.config_defaults import DEFAULTS, cfg_get
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.session_modes import (
    ACKNOWLEDGEMENT,
    QUESTION,
    RECOVER_DRAFT_PHRASES,
    SPOKEN_NOTICES_EVERYTHING,
    SPOKEN_NOTICES_KEY,
    SPOKEN_NOTICES_QUESTIONS,
    CommandDispatchResult,
    SessionMode,
    SessionModeManager,
    should_speak,
)

EXE = "notepad.exe"
HWND = 4242


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _manager(spoken_notices=None, **kwargs):
    """A real manager with a recording speak_fn."""
    spoken = []
    manager = SessionModeManager(
        abort_phrases=["cancel", "abort"],
        foreground_exe_resolver=lambda: EXE,
        foreground_hwnd_resolver=lambda: HWND,
        inject_fn=lambda text, focus_guard=None: text,
        remove_chars_fn=Mock(return_value=True),
        command_dispatch_fn=lambda text: CommandDispatchResult(matched=False, phrase=None),
        agent_dispatch_fn=Mock(),
        on_scratch_result=Mock(),
        buffer_dictate_until_commit=True,
        speak_fn=lambda text, category: spoken.append((text, category)),
        clock=_Clock(),
        **kwargs,
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    if spoken_notices is not None:
        manager.set_spoken_notices(spoken_notices)
    manager.spoken = spoken
    return manager


def _signals():
    """UtteranceSignals good enough to clear the anti-hallucination gate."""
    from samsara.session_modes import UtteranceSignals
    return UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,),
                            transcript_confident=True)


def _with_draft(manager, text="the quick brown fox jumped over the lazy dog"):
    manager._dictate_pending_buffer = text
    return manager


# ---------------------------------------------------------------------------
# The policy function, on its own
# ---------------------------------------------------------------------------

class TestThePolicy:
    @pytest.mark.parametrize("setting", [
        SPOKEN_NOTICES_QUESTIONS, SPOKEN_NOTICES_EVERYTHING, "off", "", "typo", None,
    ])
    def test_a_question_is_never_silent_at_any_setting(self, setting):
        """The load-bearing property. A question nobody heard is a hang: the
        app is holding state and waiting for an answer the user does not know
        it wants. No config value may produce that."""
        assert should_speak(QUESTION, setting) is True

    def test_acknowledgements_are_silent_at_the_default(self):
        assert should_speak(ACKNOWLEDGEMENT, SPOKEN_NOTICES_QUESTIONS) is False

    def test_everything_speaks_both_classes(self):
        assert should_speak(QUESTION, SPOKEN_NOTICES_EVERYTHING) is True
        assert should_speak(ACKNOWLEDGEMENT, SPOKEN_NOTICES_EVERYTHING) is True

    @pytest.mark.parametrize("junk", ["off", "", "QUESTIONS", "  ", "yes", None, 0])
    def test_an_unrecognised_value_falls_back_to_the_default_not_to_silence(self, junk):
        """A hand-edited config.json must not be able to mute a question, and
        must not be able to make the app loud again by accident either."""
        assert should_speak(QUESTION, junk) is True
        assert should_speak(ACKNOWLEDGEMENT, junk) is False

    def test_the_setter_normalises_and_never_raises(self):
        m = _manager()
        assert m.set_spoken_notices("EVERYTHING") == SPOKEN_NOTICES_EVERYTHING
        assert m.set_spoken_notices("  everything  ") == SPOKEN_NOTICES_EVERYTHING
        assert m.set_spoken_notices("nonsense") == SPOKEN_NOTICES_QUESTIONS
        assert m.set_spoken_notices(None) == SPOKEN_NOTICES_QUESTIONS
        assert m.set_spoken_notices("off") == SPOKEN_NOTICES_QUESTIONS

    def test_a_manager_nobody_configured_uses_the_default(self):
        """The incident is fixed with no wiring at all: an app that never
        calls set_spoken_notices still gets 'questions'."""
        assert _manager().spoken_notices == SPOKEN_NOTICES_QUESTIONS


# ---------------------------------------------------------------------------
# The config key
# ---------------------------------------------------------------------------

class TestTheConfigKey:
    def test_it_is_registered_in_the_sounds_tab_with_the_right_default(self):
        entry = SETTINGS_SCHEMA[SPOKEN_NOTICES_KEY]
        assert entry["default"] == SPOKEN_NOTICES_QUESTIONS
        assert entry["tab"] == "sounds"
        assert entry["type"] == "enum"

    def test_the_default_reaches_cfg_get_without_any_config(self):
        assert cfg_get({}, SPOKEN_NOTICES_KEY) == SPOKEN_NOTICES_QUESTIONS
        assert DEFAULTS[SPOKEN_NOTICES_KEY] == SPOKEN_NOTICES_QUESTIONS

    def test_a_nested_config_value_is_read_back(self):
        """The settings page writes config["feedback"]["spoken_notices"].
        cfg_get walks the dotted path, so a FLAT "feedback.spoken_notices"
        key would be invisible -- this pins the shape both sides agree on."""
        cfg = {"feedback": {"spoken_notices": "everything"}}
        assert cfg_get(cfg, SPOKEN_NOTICES_KEY) == SPOKEN_NOTICES_EVERYTHING


class TestOffIsNotOffered:
    """The brief: verify before offering `off`; if a question cannot be made
    answerable without speech, drop the value. It cannot be, so it is dropped."""

    def test_off_is_not_a_legal_value(self):
        assert "off" not in SETTINGS_SCHEMA[SPOKEN_NOTICES_KEY]["options"]
        assert set(SETTINGS_SCHEMA[SPOKEN_NOTICES_KEY]["options"]) == {
            SPOKEN_NOTICES_QUESTIONS, SPOKEN_NOTICES_EVERYTHING}

    def test_the_spelling_prompt_has_no_chip_to_fall_back_on(self):
        """request_word_correction returns a plain dict, never a
        DispatchOutcome, so it never reaches outcome_chip. Speech and the
        preview's own prompt line are its only surfaces -- which is exactly
        why `off` would be a trap."""
        from samsara import session_modes
        m = _with_draft(_manager())
        result = m.request_word_correction("quick", 0, expected_count=1,
                                           source="preview_click")
        assert isinstance(result, dict) and result["ok"] is True
        assert not hasattr(result, "kind")
        assert session_modes.outcome_chip("correction_armed") == ("? correction_armed", "warning"), \
            "there is no chip kind for the spelling prompt"

    def test_no_question_class_prompt_is_ever_silent(self):
        """The whole reason `off` is gone, stated as an invariant over the
        real manager: at every legal setting, both QUESTION sites speak."""
        for setting in (SPOKEN_NOTICES_QUESTIONS, SPOKEN_NOTICES_EVERYTHING):
            m = _with_draft(_manager(setting))
            m._request_clear_draft()
            assert any("Say yes to clear it" in t for t, _c in m.spoken), setting

            m2 = _with_draft(_manager(setting))
            m2.request_word_correction("quick", 0, expected_count=1, source="preview_click")
            assert any("Say the replacement" in t for t, _c in m2.spoken), setting


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------

class TestTheIncident:
    def test_clicking_clear_draft_says_nothing_at_the_default(self):
        """THE regression test. confirm_clear_draft("clear button") is exactly
        what streaming.py's Clear draft button calls."""
        m = _with_draft(_manager(SPOKEN_NOTICES_QUESTIONS))
        outcome = m.confirm_clear_draft("clear button")
        assert m.spoken == [], f"the button must not talk: {m.spoken}"
        assert outcome.kind == "dictate_draft_cleared"

    def test_the_chip_still_carries_it(self):
        """Silenced, not lost: the sentence the owner heard is still on the
        chip, which is the whole justification for silencing it."""
        from samsara.session_modes import outcome_chip
        m = _with_draft(_manager(SPOKEN_NOTICES_QUESTIONS))
        outcome = m.confirm_clear_draft("clear button")
        label, kind = outcome_chip(outcome.kind, outcome.detail)
        assert RECOVER_DRAFT_PHRASES[0] in label
        assert kind == "success"

    def test_the_same_click_still_speaks_at_everything(self):
        m = _with_draft(_manager(SPOKEN_NOTICES_EVERYTHING))
        m.confirm_clear_draft("clear button")
        assert any("Draft cleared" in t for t, _c in m.spoken)

    def test_the_yes_no_confirmation_still_speaks_at_the_default(self):
        """The brief's other named requirement: asking still talks."""
        m = _with_draft(_manager(SPOKEN_NOTICES_QUESTIONS))
        outcome = m._request_clear_draft()
        assert outcome.kind == "dictate_clear_awaiting_confirmation"
        assert len(m.spoken) == 1
        assert "Say yes to clear it, or no to keep it" in m.spoken[0][0]

    def test_asking_speaks_but_answering_does_not(self):
        """The two halves of one exchange land in different classes, and this
        is the clearest demonstration of the policy: the question is heard,
        the receipt is not."""
        m = _with_draft(_manager(SPOKEN_NOTICES_QUESTIONS))
        m._request_clear_draft()
        assert len(m.spoken) == 1, "the question is heard"
        m._answer_pending_clear("no", _signals())
        assert len(m.spoken) == 1, "the receipt for the answer is not"


# ---------------------------------------------------------------------------
# Every call site, both settings
# ---------------------------------------------------------------------------

class TestEveryCallSiteByClass:
    """One test per call site: assert its class, then assert the behaviour
    that class implies at BOTH settings. Twelve sites, no exceptions."""

    # -- QUESTION (2) -------------------------------------------------------

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, True), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_clear_draft_confirmation_is_a_QUESTION(self, setting, expected):
        m = _with_draft(_manager(setting))
        m._request_clear_draft()
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, True), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_spelling_prompt_is_a_QUESTION(self, setting, expected):
        m = _with_draft(_manager(setting))
        m.request_word_correction("quick", 0, expected_count=1, source="preview_click")
        assert bool(m.spoken) is expected

    # -- ACKNOWLEDGEMENT (10) ----------------------------------------------

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_nothing_staged_to_clear_is_an_ACK(self, setting, expected):
        m = _manager(setting)
        m._dictate_pending_buffer = ""
        m._request_clear_draft()
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_nothing_to_bring_back_is_an_ACK(self, setting, expected):
        m = _manager(setting)
        m._recover_draft_outcome()
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_draft_back_is_an_ACK(self, setting, expected):
        m = _with_draft(_manager(setting))
        m._stash_recoverable_draft("test")
        m._dictate_pending_buffer = ""
        m._recover_draft_outcome()
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_kept_the_draft_is_an_ACK(self, setting, expected):
        m = _with_draft(_manager(setting))
        m._request_clear_draft()
        m.spoken.clear()          # drop the QUESTION, leaving only the answer
        m._answer_pending_clear("no", _signals())
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_draft_cleared_is_an_ACK(self, setting, expected):
        m = _with_draft(_manager(setting))
        m.confirm_clear_draft("clear button")
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_no_correction_to_take_back_is_an_ACK(self, setting, expected):
        m = _with_draft(_manager(setting))
        m._undo_last_correction_outcome()
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_forgot_x_to_y_is_an_ACK(self, setting, expected):
        m = _with_draft(_manager(setting))
        m.set_correction_undo_fn(lambda: {"undone": True, "wrong": "their",
                                          "right": "there", "where": "draft"})
        m._undo_last_correction_outcome()
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_word_not_found_is_an_ACK(self, setting, expected):
        """The replacement arrives but the word has gone from the draft."""
        m = _with_draft(_manager(setting))
        m.request_word_correction("quick", 0, expected_count=1, source="preview_click")
        m.spoken.clear()                      # drop the QUESTION prompt
        m._dictate_pending_buffer = "nothing like the original"
        m._consume_word_correction("slow", "slow", _signals())
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_abort_keeps_the_draft_is_an_ACK(self, setting, expected):
        """"cancel" with a draft staged: the draft is set aside and the user
        is told how to get it back. Reporting finished work, so ACK."""
        m = _with_draft(_manager(setting))
        outcome = m.dispatch_utterance("cancel", _signals())
        assert outcome.kind == "abort"
        assert outcome.detail["recoverable_chars"], "the draft was stashed"
        assert bool(m.spoken) is expected

    @pytest.mark.parametrize("setting,expected", [
        (SPOKEN_NOTICES_QUESTIONS, False), (SPOKEN_NOTICES_EVERYTHING, True)])
    def test_site_correction_cancelled_is_an_ACK(self, setting, expected):
        m = _with_draft(_manager(setting))
        m.request_word_correction("quick", 0, expected_count=1, source="preview_click")
        m.spoken.clear()          # drop the QUESTION prompt
        m._consume_word_correction("never mind", "never mind", _signals())
        assert bool(m.spoken) is expected
