"""Hands-free entry into AVA mode (2026-09-11 live-log incident).

Three defects, all reproduced from C:\\Users\\Morne\\.samsara\\logs\\samsara.log:

  19:59:44  mode=command  "ava mode"  -> command_miss (beep, nothing happened)
  20:00:21  mode=dictate  "Ava mode." -> dictate_staged (typed as literal text)
  20:00:30  mode=dictate  "Ava."      -> the command registry resolved it to
            ask_ollama's "hey ava" command (which claims the alias "ava"),
            so it was dispatched as a COMMAND: the reserved-command COMMIT
            policy pasted the staged thought first, handle_ask_ava spoke
            "Yes? How can I help?" and returned None, and the session
            reported hands_free_command_failed.

Root cause of the first two: "ava mode" was deliberately absent from
_WHOLE_UTTERANCE_SWITCHES, so it fell through to the registry, where "ava
mode" PREFIX-matched the "ava" alias with remainder "mode" and was sent to
the LLM as a question (that is the 180-char TTS suppressed at 19:59:46).

No module-level `import dictation` -- nothing here needs it, and importing it
attaches a second RotatingFileHandler to the OWNER'S live log.
"""
import types

import pytest

from samsara.session_modes import (
    DEFAULT_AVA_INVOCATIONS,
    HandsFreeCommandMatch,
    PendingTextPolicy,
    SessionMode,
    SessionModeManager,
    SwitchMatch,
    UtteranceSignals,
    match_switch_word,
)

_SIG = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


def _make_manager(*, probe_fn=None, ava_ready_probe_fn=None, agent_dispatch_fn=...,
                  pasted=None, invocations=None):
    if agent_dispatch_fn is ...:
        agent_dispatch_fn = lambda text, context: None
    manager = SessionModeManager(
        abort_phrases=["stop listening"],
        foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 111,
        inject_fn=(lambda text, check=None: (pasted.append(text) or True))
        if pasted is not None else (lambda text, check=None: True),
        remove_chars_fn=lambda n: None,
        command_dispatch_fn=lambda text: types.SimpleNamespace(matched=True, phrase=text),
        agent_dispatch_fn=agent_dispatch_fn,
        buffer_dictate_until_commit=True,
        hands_free_command_probe_fn=probe_fn,
        ava_ready_probe_fn=ava_ready_probe_fn,
        ava_invocations=list(invocations or DEFAULT_AVA_INVOCATIONS),
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    return manager


# ---------------------------------------------------------------------------
# 1. "ava mode" is a first-class switch word
# ---------------------------------------------------------------------------

class TestAvaModeIsASwitchWord:
    @pytest.mark.parametrize("spoken", ["ava mode", "Ava mode.", "ava-mode",
                                        "AVA MODE", "um ava mode"])
    def test_whole_utterance_forms_switch_to_ava(self, spoken):
        match = match_switch_word(spoken)

        assert match is not None, f"{spoken!r} did not match as a switch word"
        assert match.target_mode is SessionMode.AVA
        assert match.is_prefix is False

    @pytest.mark.parametrize("spoken", [
        "we should use ava mode later",
        "ava mode is the one I want",
        "switch me into ava mode when you can",
    ])
    def test_prefix_form_is_not_a_switch(self, spoken):
        """The 2026-07-18 incident guard: "Ava <something> Mode" spoken as
        ordinary dictation must stay dictation."""
        assert match_switch_word(spoken) is None

    def test_bare_ava_is_not_a_switch_word(self):
        """Bare "ava" is an ordinary content word -- it must never switch on
        its own (Ava is still reachable via the configured invocations)."""
        assert match_switch_word("ava") is None
        assert match_switch_word("Ava.") is None

    def test_switch_from_command_mode(self):
        manager = _make_manager()
        manager.reset(initial_mode=SessionMode.COMMAND)

        outcome = manager.dispatch_utterance("ava mode", _SIG)

        assert outcome.kind == "mode_switch"
        assert manager.mode is SessionMode.AVA

    def test_switch_from_dictate_mode(self):
        manager = _make_manager()

        outcome = manager.dispatch_utterance("Ava mode.", _SIG)

        assert outcome.kind == "mode_switch"
        assert manager.mode is SessionMode.AVA

    def test_no_longer_staged_as_dictated_text(self):
        """20:00:21 regression: "Ava mode." was typed into the document."""
        manager = _make_manager()

        outcome = manager.dispatch_utterance("Ava mode.", _SIG)

        assert outcome.kind != "dictate_staged"
        assert "ava" not in manager.dictate_pending_buffer.lower()

    def test_already_in_ava_is_not_a_self_switch(self):
        manager = _make_manager()
        manager.reset(initial_mode=SessionMode.AVA)

        assert match_switch_word("ava mode", current_mode=SessionMode.AVA) is None


# ---------------------------------------------------------------------------
# 2. Reserved-phrase route: an Ava invocation is a switch, not a dispatch
# ---------------------------------------------------------------------------

def _ava_front_door_probe(text):
    """Stands in for dictation.py's _probe_hands_free_command resolving bare
    "ava"/"Ava." to ask_ollama's command, whose CANONICAL phrase is "hey ava"
    (verified against the real matcher: "ava" -> entry.phrase "hey ava")."""
    normalized = " ".join((text or "").lower().replace(".", " ").split())
    if normalized in ("ava", "hey ava"):
        return HandsFreeCommandMatch(dispatch_text="ava", phrase="hey ava",
                                     pending_policy=PendingTextPolicy.COMMIT)
    return None


class TestReservedPhraseRoutesToAvaSwitch:
    def test_bare_ava_becomes_a_mode_switch_not_a_command_dispatch(self):
        manager = _make_manager(probe_fn=_ava_front_door_probe)

        outcome = manager.dispatch_utterance("Ava.", _SIG)

        assert outcome.kind == "mode_switch", (
            "bare 'Ava.' still went down the registry-dispatch path"
        )
        assert manager.mode is SessionMode.AVA

    def test_hey_ava_in_dictate_switches(self):
        manager = _make_manager(probe_fn=_ava_front_door_probe)

        outcome = manager.dispatch_utterance("hey ava", _SIG)

        assert outcome.kind == "mode_switch"
        assert manager.mode is SessionMode.AVA

    def test_no_hands_free_command_failed_outcome(self):
        """20:00:30 regression: outcome was hands_free_command_failed."""
        manager = _make_manager(probe_fn=_ava_front_door_probe)
        manager.dispatch_utterance("hello there", _SIG)

        outcome = manager.dispatch_utterance("Ava.", _SIG)

        assert outcome.kind != "hands_free_command_failed"

    def test_pending_text_handled_exactly_like_the_command_mode_switch(self):
        """The contract is PARITY with "command mode", not a special case.

        Both switches route through _do_switch, which commits a pending
        DICTATE buffer when leaving DICTATE. This asserts the two paths agree
        -- the bug was the paste happening as a side effect of a FAILED
        command dispatch, not of an intentional switch.
        """
        ava_pasted, cmd_pasted = [], []

        ava_mgr = _make_manager(probe_fn=_ava_front_door_probe, pasted=ava_pasted)
        ava_mgr.dispatch_utterance("hello there", _SIG)
        ava_pending = ava_mgr.dictate_pending_buffer
        ava_outcome = ava_mgr.dispatch_utterance("Ava.", _SIG)

        cmd_mgr = _make_manager(probe_fn=_ava_front_door_probe, pasted=cmd_pasted)
        cmd_mgr.dispatch_utterance("hello there", _SIG)
        cmd_pending = cmd_mgr.dictate_pending_buffer
        cmd_outcome = cmd_mgr.dispatch_utterance("command mode", _SIG)

        assert ava_pending == cmd_pending, "test rig staged different text"
        assert ava_outcome.kind == cmd_outcome.kind == "mode_switch"
        assert ava_pasted == cmd_pasted, (
            "AVA switch handled pending text differently from 'command mode'"
        )
        assert ava_mgr.dictate_pending_buffer == cmd_mgr.dictate_pending_buffer

    def test_switch_with_no_pending_text_pastes_nothing(self):
        """With an empty buffer the switch must be side-effect free."""
        pasted = []
        manager = _make_manager(probe_fn=_ava_front_door_probe, pasted=pasted)

        outcome = manager.dispatch_utterance("Ava.", _SIG)

        assert outcome.kind == "mode_switch"
        assert pasted == [], "switching to AVA pasted something on its own"

    def test_non_ava_reserved_command_still_dispatches(self):
        """The new branch must only capture Ava invocations."""
        def _probe(text):
            if " ".join((text or "").lower().split()) == "submit":
                return HandsFreeCommandMatch(dispatch_text="submit", phrase="submit",
                                             pending_policy=PendingTextPolicy.COMMIT)
            return None

        manager = _make_manager(probe_fn=_probe)

        outcome = manager.dispatch_utterance("submit", _SIG)

        assert outcome.kind == "hands_free_command_executed"


# ---------------------------------------------------------------------------
# 3. AVA entry is loud on failure
# ---------------------------------------------------------------------------

class TestAvaEntryFailsLoudly:
    def test_missing_agent_dispatch_fn_refuses_and_stays_put(self):
        manager = _make_manager(agent_dispatch_fn=None)

        outcome = manager.dispatch_utterance("ava mode", _SIG)

        assert outcome.kind == "ava_entry_failed"
        assert "dispatch function" in outcome.detail["reason"]
        assert manager.mode is SessionMode.DICTATE, "mode changed despite refusal"

    def test_probe_reason_is_surfaced_verbatim(self):
        manager = _make_manager(
            ava_ready_probe_fn=lambda: "the Ava plugin is disabled in settings")

        outcome = manager.dispatch_utterance("ava mode", _SIG)

        assert outcome.kind == "ava_entry_failed"
        assert outcome.detail["reason"] == "the Ava plugin is disabled in settings"
        assert outcome.detail["mode_retained"] is SessionMode.DICTATE

    def test_probe_exception_is_reported_not_swallowed(self):
        def _boom():
            raise RuntimeError("ollama socket closed")

        manager = _make_manager(ava_ready_probe_fn=_boom)

        outcome = manager.dispatch_utterance("ava mode", _SIG)

        assert outcome.kind == "ava_entry_failed"
        assert "ollama socket closed" in outcome.detail["reason"]
        assert manager.mode is SessionMode.DICTATE

    def test_warning_is_logged_with_the_reason(self, caplog):
        import logging

        manager = _make_manager(ava_ready_probe_fn=lambda: "Ollama is not running")
        with caplog.at_level(logging.WARNING):
            manager.dispatch_utterance("ava mode", _SIG)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Ollama is not running" in r.getMessage() for r in warnings), (
            "AVA entry refusal was not logged at WARNING with its reason"
        )

    def test_ready_probe_returning_none_allows_entry(self):
        manager = _make_manager(ava_ready_probe_fn=lambda: None)

        outcome = manager.dispatch_utterance("ava mode", _SIG)

        assert outcome.kind == "mode_switch"
        assert manager.mode is SessionMode.AVA

    def test_no_probe_wired_preserves_previous_behaviour(self):
        manager = _make_manager()

        outcome = manager.dispatch_utterance("ava mode", _SIG)

        assert outcome.kind == "mode_switch"
        assert manager.mode is SessionMode.AVA

    def test_reserved_phrase_route_is_refused_too(self):
        """Both Ava routes go through the same readiness gate."""
        manager = _make_manager(probe_fn=_ava_front_door_probe,
                                ava_ready_probe_fn=lambda: "Ollama is not running")

        outcome = manager.dispatch_utterance("Ava.", _SIG)

        assert outcome.kind == "ava_entry_failed"
        assert manager.mode is SessionMode.DICTATE


# ---------------------------------------------------------------------------
# Config default (item 5)
# ---------------------------------------------------------------------------

class TestAvaInvocationDefaults:
    def test_ava_invocations_default_is_present(self):
        from samsara import config_defaults

        assert "ava_invocations" in config_defaults.DEFAULTS
        assert config_defaults.DEFAULTS["ava_invocations"] == [
            "hey ava", "so ava", "oracle",
        ]

    def test_ava_mode_is_not_an_invocation(self):
        """It is a switch word now -- listing it in both places would give
        one phrase two different entry paths."""
        from samsara import config_defaults

        assert "ava mode" not in config_defaults.DEFAULTS["ava_invocations"]
        assert "ava mode" not in DEFAULT_AVA_INVOCATIONS
