"""Tests for the AVA substance gate's dictation.py wiring:
DictationApp._handle_session_dispatch_outcome (soft-miss earcon + inactivity
timer touch for rejected micro-utterances).

The pure gate logic (is_substantive_utterance) and the SessionModeManager
dispatch behavior (rejected utterance never reaches the mocked agent) are
covered in tests/test_session_modes.py -- this file covers only the
dictation.py-side side effects, using the REAL bound method on a lightweight
stub (not a reimplementation), same pattern as tests/test_session_badge.py.
"""
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_stub():
    import dictation as _d
    from samsara.session_modes import DispatchOutcome

    class _Stub:
        _handle_session_dispatch_outcome = _d.DictationApp._handle_session_dispatch_outcome

        def __init__(self):
            self.config = {'command_mode': {'inactivity_timeout_s': 30}}
            self._sounds = []
            self.reset_calls = []

        def play_sound(self, name, **_kwargs):
            self._sounds.append(name)

        def _reset_command_mode_inactivity_timer(self, timeout_s):
            self.reset_calls.append(timeout_s)

    return _Stub(), DispatchOutcome


class TestHandleSessionDispatchOutcome:
    def test_rejected_utterance_fires_soft_miss_earcon(self):
        stub, DispatchOutcome = _make_stub()
        outcome = DispatchOutcome(kind="ava_rejected_not_substantive", detail={"text": "uh"})
        stub._handle_session_dispatch_outcome(outcome, "uh")
        assert "scratch_refuse" in stub._sounds

    def test_rejected_utterance_touches_inactivity_timer(self):
        stub, DispatchOutcome = _make_stub()
        outcome = DispatchOutcome(kind="ava_rejected_not_substantive", detail={"text": "uh"})
        stub._handle_session_dispatch_outcome(outcome, "uh")
        assert stub.reset_calls == [30]

    def test_accepted_ava_dispatch_touches_inactivity_timer_no_earcon(self):
        stub, DispatchOutcome = _make_stub()
        outcome = DispatchOutcome(kind="ava_dispatched", detail={"text": "what time is it"})
        stub._handle_session_dispatch_outcome(outcome, "what time is it")
        assert stub.reset_calls == [30]
        assert "scratch_refuse" not in stub._sounds

    def test_other_outcomes_do_not_touch_timer_or_earcon(self):
        """COMMAND/DICTATE outcomes are untouched by this AVA-only wiring."""
        stub, DispatchOutcome = _make_stub()
        for kind in ("command_executed", "command_miss", "dictate_injected",
                     "dictate_suppressed_focus_lock", "mode_switch",
                     "scratch_success", "scratch_refuse", "abort", "empty"):
            outcome = DispatchOutcome(kind=kind, detail={})
            stub._handle_session_dispatch_outcome(outcome, "text")
        assert stub.reset_calls == []
        assert stub._sounds == []
