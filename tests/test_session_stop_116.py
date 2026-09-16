"""Queue 116: the emergency stop, wired, and its word changed.

`SessionModeManager` has accepted a `stop_fn` since it was written and
dictation.py never passed one, so the stop branch and sleep's stop-first step
were dead in the shipped app: saying the stop word did nothing and fell
through to ordinary dispatch, and "go to sleep" disarmed capture without
halting anything in flight. Queue 110 found this and deliberately left it,
because wiring it changes what a spoken word does.

The owner's decision, recorded so it is not reversed: `("stop",)` becomes
`("halt", "cease")`. The match has always been whole-utterance only, so
"stop the music" was never at risk -- but a bare "Stop." is common enough in
real dictation to lose, and once the branch is live, losing it means the word
is eaten instead of typed. Two words because a panic control should have more
than one way in.

Nothing here imports dictation.py. The construction-site wiring is asserted
against the source text, and the callable it passes is rebuilt from the same
pieces (execution_policy.stop_all with chip=False) against a fake app.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from samsara import session_modes as sm
from samsara.session_modes import (
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
    outcome_chip,
)

GOOD_SIGNALS = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))
DICTATION = "Halt the process and cease the other one."


def _manager(*, stop_fn=None, stop_phrases=None, buffer=True):
    return SessionModeManager(
        abort_phrases=["cancel", "abort"],
        foreground_exe_resolver=Mock(return_value="notepad.exe"),
        foreground_hwnd_resolver=Mock(return_value=12345),
        inject_fn=Mock(),
        remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(return_value=None),
        agent_dispatch_fn=Mock(),
        on_mode_change=Mock(),
        on_focus_lock_revert=Mock(),
        on_scratch_result=Mock(),
        on_abort=Mock(),
        on_switch_dispatch_error=Mock(),
        buffer_dictate_until_commit=buffer,
        stop_fn=stop_fn,
        stop_phrases=stop_phrases,
    )


def _staged(mgr, text=DICTATION):
    """A DICTATE draft the stop must not touch."""
    mgr.force_mode(SessionMode.DICTATE)
    mgr.dispatch_utterance(text, GOOD_SIGNALS)
    assert mgr.dictate_pending_buffer, "the fixture needs a real draft"
    return mgr.dictate_pending_buffer


# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------

def test_the_stop_words_are_halt_and_cease():
    assert sm.SESSION_STOP_PHRASES == ("halt", "cease")
    assert "stop" not in sm.SESSION_STOP_PHRASES


@pytest.mark.parametrize("word", ["halt", "cease", "Halt.", "Cease!", "  halt  "])
def test_each_stop_word_runs_the_stop(word):
    calls = []
    mgr = _manager(stop_fn=lambda reason: calls.append(reason) or {"speech": True})
    before = _staged(mgr)

    outcome = mgr.dispatch_utterance(word, GOOD_SIGNALS)

    assert calls == ["stop"]
    assert outcome.kind == "stopped"
    # The draft survives BYTE-IDENTICAL, the mode is unchanged.
    assert mgr.dictate_pending_buffer == before
    assert outcome.detail["draft_kept_chars"] == len(before)
    assert outcome.detail["mode_retained"] is SessionMode.DICTATE
    assert mgr.mode is SessionMode.DICTATE


def test_the_microphone_and_the_session_are_untouched():
    """A stop is not an abort and not a sleep: nothing disarms, nothing ends."""
    mgr = _manager(stop_fn=lambda reason: {"in_flight": True})
    mgr.force_mode(SessionMode.DICTATE)
    on_abort = mgr._on_abort

    outcome = mgr.dispatch_utterance("halt", GOOD_SIGNALS)

    assert outcome.kind == "stopped"
    on_abort.assert_not_called()                 # the session is not ended
    assert mgr.mode is SessionMode.DICTATE       # the lane is not switched
    assert outcome.detail["mode_retained"] is SessionMode.DICTATE


def test_stop_is_no_longer_a_stop_word_and_is_dictated():
    calls = []
    mgr = _manager(stop_fn=lambda reason: calls.append(reason) or {})
    mgr.force_mode(SessionMode.DICTATE)

    outcome = mgr.dispatch_utterance("Stop.", GOOD_SIGNALS)

    assert calls == []
    assert outcome.kind != "stopped"
    assert "stop" in mgr.dictate_pending_buffer.lower()


@pytest.mark.parametrize("text", [
    "halt the process",
    "I told him to halt",
    "cease and desist",
    "we should cease trading",
    "halt, and then cease",
])
def test_a_stop_word_inside_a_sentence_is_dictation(text):
    """Whole-utterance only. This is the property that made changing the word
    a safety improvement rather than a workaround."""
    calls = []
    mgr = _manager(stop_fn=lambda reason: calls.append(reason) or {})
    mgr.force_mode(SessionMode.DICTATE)

    outcome = mgr.dispatch_utterance(text, GOOD_SIGNALS)

    assert calls == [], text
    assert outcome.kind != "stopped", text
    assert mgr.dictate_pending_buffer, text


def test_without_a_stop_fn_the_word_is_still_only_text():
    """Unwired is the state this prompt found, and it must stay honest: the
    module cannot stop anything on its own and never pretends to."""
    mgr = _manager(stop_fn=None)
    mgr.force_mode(SessionMode.DICTATE)
    outcome = mgr.dispatch_utterance("halt", GOOD_SIGNALS)
    assert outcome.kind != "stopped"


# ---------------------------------------------------------------------------
# The config key
# ---------------------------------------------------------------------------

def test_the_config_key_exists_and_matches_the_built_in_default():
    from samsara import config_defaults
    from samsara.config_schema import SETTINGS_SCHEMA

    spec = SETTINGS_SCHEMA["command_mode.stop_phrases"]
    assert spec["type"] == "list" and spec["item_type"] == "str"
    assert spec["default"] == list(sm.SESSION_STOP_PHRASES) == ["halt", "cease"]
    assert config_defaults.DEFAULTS["command_mode.stop_phrases"] == ["halt", "cease"]


def test_the_config_key_adds_a_word_that_works_without_a_code_change():
    calls = []
    mgr = _manager(stop_fn=lambda reason: calls.append(reason) or {},
                   stop_phrases=["halt", "cease", "belay"])

    assert mgr.dispatch_utterance("belay", GOOD_SIGNALS).kind == "stopped"
    assert calls == ["stop"]


def test_the_config_key_replaces_rather_than_extends():
    """"Change a word" has to be possible, not only "add one"."""
    mgr = _manager(stop_fn=lambda reason: {}, stop_phrases=["belay"])
    assert mgr.dispatch_utterance("belay", GOOD_SIGNALS).kind == "stopped"
    mgr2 = _manager(stop_fn=lambda reason: {}, stop_phrases=["belay"])
    mgr2.force_mode(SessionMode.DICTATE)
    assert mgr2.dispatch_utterance("halt", GOOD_SIGNALS).kind != "stopped"


@pytest.mark.parametrize("bad", [None, [], ["", "   "], [".", "!"]])
def test_an_unusable_config_value_falls_back_to_the_built_ins(bad):
    """A typo in config.json must not be a way to lose the emergency stop."""
    mgr = _manager(stop_fn=lambda reason: {}, stop_phrases=bad)
    assert mgr.dispatch_utterance("halt", GOOD_SIGNALS).kind == "stopped"
    assert mgr._stop_phrases == frozenset({"halt", "cease"})


# ---------------------------------------------------------------------------
# Sleep runs the stop FIRST
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", list(sm.SESSION_SLEEP_PHRASES))
def test_sleep_stops_first_then_sleeps_and_keeps_the_draft(phrase):
    order = []
    mgr = _manager(stop_fn=lambda reason: order.append(("stop", reason)) or {"speech": True})
    mgr._on_abort = Mock(side_effect=lambda: order.append(("abort", None)))
    before = _staged(mgr)

    outcome = mgr.dispatch_utterance(phrase, GOOD_SIGNALS)

    assert order == [("stop", "sleep"), ("abort", None)], phrase
    assert outcome.kind == "mode_switch"
    assert outcome.detail["sleep"] is True and outcome.detail["stopped"] is True
    assert outcome.detail["draft_retained_chars"] == len(before)
    assert mgr._retained_draft["buffer"] == before      # kept byte-identical


def test_sleep_without_a_stop_fn_reports_that_it_did_not_stop():
    mgr = _manager(stop_fn=None)
    outcome = mgr.dispatch_utterance("go to sleep", GOOD_SIGNALS)
    assert outcome.detail["sleep"] is True and outcome.detail["stopped"] is False


# ---------------------------------------------------------------------------
# The chip says WHAT was halted
# ---------------------------------------------------------------------------

def test_a_stop_that_halts_nothing_says_so_and_is_not_a_success():
    label, kind = outcome_chip("stopped", {"cleared": {"generation": 7}})
    assert label == "nothing to halt"
    assert kind == "warning"
    assert kind != "success"


@pytest.mark.parametrize("cleared, expected", [
    ({"speech": True}, "halted speech"),
    ({"in_flight": True}, "halted Ava"),
    ({"pending": True}, "halted confirm"),
    ({"queued": 3}, "halted 3 queued"),
    ({"schedule": True}, "halted schedule"),
    ({"speech": True, "in_flight": True}, "halted speech, Ava"),
])
def test_the_chip_names_what_was_halted(cleared, expected):
    label, kind = outcome_chip("stopped", {"cleared": cleared})
    assert label == expected
    assert kind == "warning"


def test_the_chip_never_truncates_a_word_away():
    """Every combination has to fit the chip, because a stop chip reading
    "halted 12 queued, sched..." is a worse answer than "halted 12 queued +1"."""
    import itertools

    keys = ["speech", "in_flight", "pending", "queued", "schedule"]
    for count in range(1, len(keys) + 1):
        for combo in itertools.combinations(keys, count):
            for queued in (1, 12, 347):
                cleared = {k: (queued if k == "queued" else True) for k in combo}
                label, _ = outcome_chip("stopped", {"cleared": cleared})
                assert sm.CHIP_ELLIPSIS not in label, (combo, queued, label)
                assert len(label) <= sm._REASON_MAX, (combo, queued, label)
                assert label.startswith("halted ")


def test_the_generation_alone_is_not_something_halted():
    """cleared always carries a generation; that is bookkeeping, not an
    effect, and must never make an empty stop look successful."""
    for cleared in ({}, {"generation": 1}, {"generation": 99, "queued": 0, "speech": False}):
        assert outcome_chip("stopped", {"cleared": cleared})[0] == "nothing to halt"


def test_a_stopped_outcome_with_no_detail_does_not_crash():
    assert outcome_chip("stopped", None)[0] == "nothing to halt"
    assert outcome_chip("stopped", {})[0] == "nothing to halt"
    assert outcome_chip("stopped", {"cleared": "nonsense"})[0] == "nothing to halt"


# ---------------------------------------------------------------------------
# The construction site in dictation.py
# ---------------------------------------------------------------------------

_SOURCE = (REPO / "dictation.py").read_text(encoding="utf-8", errors="replace")


def _manager_call() -> ast.Call:
    """The SessionModeManager(...) call, found in the AST rather than by
    grep, so a moved construction site is still checked."""
    tree = ast.parse(_SOURCE)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "SessionModeManager"):
            return node
    raise AssertionError("SessionModeManager is not constructed in dictation.py")


def test_the_construction_site_passes_a_stop_fn_and_the_phrase_list():
    """The whole defect in one assertion: this keyword was never passed."""
    keywords = {kw.arg for kw in _manager_call().keywords}
    assert "stop_fn" in keywords, "the stop branch is dead again"
    assert "stop_phrases" in keywords


def test_the_stop_fn_is_the_execution_policy_stop():
    """It must be the real stop, and it must not double-chip: the
    DispatchOutcome chip says what was halted, the generic one would not."""
    body = re.search(r"\n        def _session_stop\(reason: str\) -> dict:.*?\n            return cleared\n",
                     _SOURCE, re.S)
    assert body, "dictation.py no longer defines the stop callable"
    text = body.group(0)
    assert "execution_policy.stop_all(" in text
    assert "chip=False" in text
    assert "bump=False" not in text, "the stop must bump the generation FIRST"


def test_the_stop_phrases_come_from_the_config_key():
    call = _manager_call()
    passed = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    assert "stop_phrases" in passed["stop_phrases"] or "stop_phrases" in str(passed)
    assert "'stop_phrases'" in passed["stop_phrases"]
    assert "command_mode" in passed["stop_phrases"]


# ---------------------------------------------------------------------------
# What stop_all actually does, on a fake app
# ---------------------------------------------------------------------------

class _Coordinator:
    def __init__(self, speaking=True):
        self.is_speaking = speaking
        self.cancelled = 0

    def cancel_speech(self):
        self.cancelled += 1


class _App:
    def __init__(self, speaking=True):
        import threading

        self.audio_coordinator = _Coordinator(speaking)
        self.chips = []
        self._ava_session_dispatch_queue = []
        # A real lock, because stop_all only reads _ava_session_request_in_flight
        # inside the locked branch -- the shipped app always has one.
        self._ava_session_dispatch_lock = threading.Lock()
        self._ava_session_request_in_flight = False

    def _show_outcome_chip(self, label, kind):
        self.chips.append((label, kind))


def test_stop_all_cancels_speech_and_reports_it():
    """Queue 110 put TTS cancellation into stop_all. "Ava keeps talking" is
    the symptom the owner reported, so this is the load-bearing half."""
    from samsara import execution_policy

    app = _App(speaking=True)
    cleared = execution_policy.stop_all(app, "voice stop (stop)", chip=False)

    assert app.audio_coordinator.cancelled == 1
    assert cleared["speech"] is True
    assert app.chips == []                       # chip=False really means no chip


def test_stop_all_reports_no_speech_when_nothing_was_being_said():
    from samsara import execution_policy

    app = _App(speaking=False)
    cleared = execution_policy.stop_all(app, "voice stop (stop)", chip=False)
    assert app.audio_coordinator.cancelled == 1  # cancelling is still safe
    assert cleared["speech"] is False
    assert outcome_chip("stopped", {"cleared": cleared})[0] == "nothing to halt"


def test_stop_all_bumps_the_generation_before_anything_else():
    from samsara import execution_policy

    app = _App(speaking=True)
    first = execution_policy.stop_all(app, "a", chip=False)["generation"]
    second = execution_policy.stop_all(app, "b", chip=False)["generation"]
    assert second > first


def test_a_mid_ava_stop_drains_the_queue_so_no_reply_arrives_afterwards():
    """A stop mid-answer cancels the turn: the queue is emptied, so no
    resolved action executes and no reply is delivered after it."""
    from samsara import execution_policy

    app = _App(speaking=True)
    app._ava_session_dispatch_queue = ["question one", "question two"]
    app._ava_session_request_in_flight = True

    cleared = execution_policy.stop_all(app, "voice stop (stop)", chip=False)

    assert app._ava_session_dispatch_queue == []
    assert cleared["queued"] >= 2 and cleared["in_flight"] is True
    assert cleared["speech"] is True
    label, kind = outcome_chip("stopped", {"cleared": cleared})
    assert label.startswith("halted speech") and kind == "warning"


def test_the_stop_survives_an_app_with_no_coordinator_and_a_raising_engine():
    """A panic control that can raise is not a panic control."""
    from samsara import execution_policy

    bare = _App()
    bare.audio_coordinator = None
    assert execution_policy.stop_all(bare, "x", chip=False)["speech"] is False

    class _Angry(_Coordinator):
        def cancel_speech(self):
            raise RuntimeError("engine gone")

    angry = _App()
    angry.audio_coordinator = _Angry()
    execution_policy.stop_all(angry, "x", chip=False)     # must not raise


# ---------------------------------------------------------------------------
# Settings exposure
# ---------------------------------------------------------------------------

def test_the_stop_phrases_have_a_settings_control_beside_the_other_phrases():
    from samsara.ui.settings import modes_qt

    source = Path(modes_qt.__file__).read_text(encoding="utf-8", errors="replace")
    assert "'cmd_stop_phrases'" in source
    assert "cmd_cfg['stop_phrases']" in source
    # Beside the existing session-phrase editor, not on a page of its own.
    assert source.index("cmd_stop_phrases") < source.index("cmd_abort_phrases")


def test_the_settings_warnings_explain_the_traps():
    from samsara.ui.settings import modes_qt

    empty = modes_qt._stop_phrase_warnings([])
    assert empty and "cannot be switched off" in empty[0]
    assert modes_qt._stop_phrase_warnings(["halt", "cease"]) == []
    assert modes_qt._stop_phrase_warnings(["go to sleep"])


def test_the_spoken_help_reads_the_new_words_without_being_edited():
    """The wizard tip, the quick reference and the Modes note all read the
    constant, so the word change reaches the user's documentation for free."""
    from samsara.ui.settings import modes_qt

    note = modes_qt._button_behavior_note()
    assert '"halt" / "cease"' in note
    assert '"stop"' not in note
