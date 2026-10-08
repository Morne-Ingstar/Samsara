"""Queue 80: session-control words -- clear-the-draft command, the whole-utterance
guard any fast path must keep, and the re-runnable stage-timing harness.

Never imports dictation (the app may be running); dictation.py's one new
helper is compiled from source with ast, as tests/test_session_safety_50.py does.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from samsara import execution_policy
from samsara.session_modes import (
    CLEAR_DRAFT_CONFIRM_TTL_S,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
    is_clear_draft,
    is_dictate_commit,
    outcome_chip,
    split_trailing_dictate_commit,
)

ROOT = Path(__file__).resolve().parents[1]
GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))
NOISE = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(50.0,))


class _NoMatch:
    matched = False
    phrase = None
    state = None
    awaiting_confirmation = False


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _manager(clock=None):
    speak = Mock()
    inject = Mock()
    mgr = SessionModeManager(
        abort_phrases=["cancel", "abort"],
        foreground_exe_resolver=Mock(return_value="notepad.exe"),
        foreground_hwnd_resolver=Mock(return_value=1),
        inject_fn=inject,
        remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(return_value=_NoMatch()),
        agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True,
        hands_free_command_probe_fn=Mock(return_value=None),
        clock=clock or _Clock(),
        speak_fn=speak,
    )
    mgr.reset(SessionMode.DICTATE)
    return mgr, speak, inject


def _stage(mgr, *chunks):
    for chunk in chunks:
        assert mgr.dispatch_utterance(chunk, GOOD).kind == "dictate_staged"


# --- 4. clear the whole staged draft ------------------------------------------

def test_scratch_everything_clears_at_once_with_a_take_back_chip():
    mgr, speak, inject = _manager()
    _stage(mgr, "First thought.", "second thought", "third thought")
    before = mgr.dictate_pending_buffer

    out = mgr.dispatch_utterance("Scratch everything.", GOOD)

    assert out.kind == "dictate_draft_cleared"
    assert out.detail["cleared_chars"] == len(before)
    assert mgr.dictate_pending_buffer == ""
    speak.assert_not_called()
    assert outcome_chip(out.kind, out.detail)[0] == 'Draft cleared - say "bring it back"'
    inject.assert_not_called()


def test_clear_clears_text_audio_and_undo_entries_without_a_yes():
    mgr, speak, inject = _manager()
    for i, chunk in enumerate(("One.", "two", "three")):
        mgr.dispatch_utterance(chunk, UtteranceSignals(
            has_contiguous_speech=True, compression_ratios=(1.3,), audio_ref=f"audio-{i}"))
    out = mgr.dispatch_utterance("scratch all of that", GOOD)

    assert out.kind == "dictate_draft_cleared"
    assert out.detail["cleared_chars"] > 0
    assert mgr.dictate_pending_buffer == ""
    assert mgr._dictate_pending_audio == []
    assert not [i for i in mgr._stack._items if i.kind == "dictation_staged_chunk"]
    speak.assert_not_called()
    assert mgr._clear_draft_undo._snapshot is not None
    inject.assert_not_called()  # nothing was pasted on the way
    # Nothing left to commit: "end" reports an empty commit and pastes nothing.
    after = mgr.dispatch_utterance("end", GOOD)
    assert after.detail.get("empty") is True
    inject.assert_not_called()


def test_no_after_a_clear_is_a_new_thought():
    mgr, speak, _ = _manager()
    _stage(mgr, "Keep this.")
    mgr.dispatch_utterance("scratch everything", GOOD)
    out = mgr.dispatch_utterance("no", GOOD)
    assert out.kind == "dictate_staged"
    assert mgr.dictate_pending_buffer.strip() == "no"
    speak.assert_not_called()


def test_any_other_utterance_after_clear_is_dictated_as_usual():
    mgr, _, _ = _manager()
    _stage(mgr, "Keep this.")
    mgr.dispatch_utterance("scratch everything", GOOD)
    assert mgr.dispatch_utterance("and another sentence", GOOD).kind == "dictate_staged"
    # A later "yes" is also ordinary dictation.
    out = mgr.dispatch_utterance("yes", GOOD)
    assert out.kind != "dictate_draft_cleared"
    assert "Keep this" not in mgr.dictate_pending_buffer
    assert "another sentence" in mgr.dictate_pending_buffer


def test_take_back_window_expires():
    clock = _Clock()
    mgr, _, _ = _manager(clock)
    _stage(mgr, "Keep this.")
    mgr.dispatch_utterance("scratch everything", GOOD)
    clock.now += CLEAR_DRAFT_CONFIRM_TTL_S + 1
    assert mgr.dispatch_utterance("bring it back", GOOD).kind == "dictate_recover_nothing"
    assert mgr.dictate_pending_buffer == ""


def test_a_clear_the_gate_distrusts_never_discards_text():
    mgr, _, _ = _manager()
    _stage(mgr, "Keep this.")
    assert mgr.dispatch_utterance("scratch everything", NOISE).kind != "dictate_draft_cleared"
    assert "Keep this" in mgr.dictate_pending_buffer
    assert mgr.dispatch_utterance("scratch everything", GOOD).kind == "dictate_draft_cleared"


def test_nothing_staged_is_silent():
    mgr, speak, _ = _manager()
    out = mgr.dispatch_utterance("scratch everything", GOOD)
    assert out.kind == "dictate_clear_nothing"
    speak.assert_not_called()


@pytest.mark.parametrize("sentence", [
    "We should scratch everything we planned for today.",
    "Scratch everything? No, just the intro.",
    "I told him to scratch all of that and start again",
])
def test_clear_phrase_inside_a_dictated_sentence_is_dictation(sentence):
    assert not is_clear_draft(sentence)
    mgr, speak, _ = _manager()
    _stage(mgr, "Draft.")
    assert mgr.dispatch_utterance(sentence, GOOD).kind == "dictate_staged"
    speak.assert_not_called()


def test_catalog_row_is_destructive_whole_utterance_with_a_stable_id():
    rows = json.loads((ROOT / "commands_catalog.json").read_text(encoding="utf-8"))["commands"]
    row = next(r for r in rows if r["canonical_id"] == "builtin.scratch_everything")
    assert row["risk"] == "destructive"
    assert row["whole_utterance"] is True
    assert row["aliases"] == ["scratch everything"]
    cmd = json.loads((ROOT / "commands.json").read_text(encoding="utf-8"))["commands"]["scratch everything"]
    assert execution_policy.classify_builtin(cmd) == execution_policy.RISK_DESTRUCTIVE


def _compile_dictation_method(name):
    source = (ROOT / "dictation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            namespace = {"logger": Mock()}
            exec(compile(module, "dictation.py", "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in dictation.py")


def test_full_feedback_is_a_short_clear_receipt_through_the_coordinator():
    """The real session-notice seam speaks only the short receipt in Full."""
    from samsara.tts.coordinator import AudioCoordinator

    engine = Mock()
    app = Mock()
    app.command_mode_active = True
    app.config = {"command_mode": {"tts_char_limit": 50}, "tts": {"feedback_level": "full"}}
    coordinator = AudioCoordinator(app, engine, {})
    coordinator.transition_to = Mock()
    app.audio_coordinator = coordinator

    speak_notice = _compile_dictation_method("_speak_session_notice")
    import threading
    from samsara.stage_timing import current_stage_timing
    speak_notice.__globals__.update(threading=threading, current_stage_timing=current_stage_timing)
    mgr = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=Mock(return_value="notepad.exe"),
        inject_fn=Mock(), remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(return_value=_NoMatch()), agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True,
        speak_fn=lambda text, category: speak_notice(app, text, category),
    )
    mgr.reset(SessionMode.DICTATE)
    mgr.set_spoken_notices("everything")
    _stage(mgr, "A sentence long enough to count several words in the draft.")
    mgr.dispatch_utterance("scratch everything", GOOD)

    spoken = engine.speak.call_args
    assert spoken is not None, "Full feedback was suppressed"
    assert spoken.args[0] == "Draft cleared"
    assert spoken.kwargs["category"] == "confirmation"


def test_dictation_wires_speak_fn_into_the_session_manager():
    source = (ROOT / "dictation.py").read_text(encoding="utf-8")
    assert "speak_fn=self._speak_session_notice" in source


# --- 2. the invariant any session-control fast path must keep -----------------

@pytest.mark.parametrize("sentence", [
    "We reached the end.",
    "At the end of the day it works.",
    "end of story, we move on",
    "That's the end",
    "the end",
    "And",
    "nd",
    "Let's scratch that idea",
])
def test_end_word_never_commits_from_inside_a_dictated_sentence(sentence):
    """Whatever recognises "end" faster (keyword spotter, early decode, shorter
    endpoint) may only change WHEN a chunk closes -- never widen WHAT commits.
    A dictated sentence containing the word stages; nothing is pasted."""
    assert not is_dictate_commit(sentence)
    assert split_trailing_dictate_commit(sentence) is None
    mgr, _, inject = _manager()
    _stage(mgr, "Draft.")
    out = mgr.dispatch_utterance(sentence, GOOD)
    assert out.kind in ("dictate_staged",)
    inject.assert_not_called()


@pytest.mark.parametrize("utterance", ["End.", "end", "END", "Okay end", "It works. End."])
def test_real_end_still_commits(utterance):
    mgr, _, inject = _manager()
    _stage(mgr, "Draft.")
    assert mgr.dispatch_utterance(utterance, GOOD).kind == "dictate_committed"
    inject.assert_called_once()


# --- 1. the stage-timing harness stays re-runnable ----------------------------

_SAMPLE_LOG = """\
2026-09-15 05:17:01,015 - INFO - [CMD-UTT] capture mode=dictate duration=1.5s
2026-09-15 05:17:01,015 - DEBUG - [CMD-UTT] Transcribing 1.5s utterance
2026-09-15 05:17:01,016 - INFO - Processing audio with duration 00:01.500
2026-09-15 05:17:01,146 - DEBUG - [CMD-UTT] "End."
2026-09-15 05:17:01,149 - DEBUG - [GATE] pass: max contiguous speech 256ms at 0.48s (buffer 1.5s)
2026-09-15 05:17:01,149 - INFO - Processing audio with duration 00:08.600
2026-09-15 05:17:01,513 - DEBUG - [SMART] disabled -- skipping
2026-09-15 05:17:01,831 - INFO - [PASTE] Ctrl+V sent chars=95 hwnd=204540
2026-09-15 05:17:01,845 - INFO - [SESSION] mode=dictate outcome=dictate_committed detail={}
"""


def test_stage_harness_reconstructs_each_stage_from_a_log(tmp_path):
    sys.path.insert(0, str(ROOT / "perf_artifacts"))
    try:
        import session_control_latency as harness
    finally:
        sys.path.pop(0)
    log = tmp_path / "samsara.log"
    log.write_text(_SAMPLE_LOG, encoding="utf-8")
    result = harness.analyse([log])
    row = result["rows"][0]
    assert result["one_word_end_commits"] == 1
    assert row["endpoint_wait"] == pytest.approx(1500 - 480 - 256, abs=0.01)
    assert row["queue_wait"] == pytest.approx(1, abs=1)
    assert row["decode_word"] == pytest.approx(130, abs=0.01)
    assert row["gate"] == pytest.approx(3, abs=0.01)
    assert row["commit_redecode"] == pytest.approx(364, abs=0.01)
    assert row["redecode_audio_s"] == pytest.approx(8.6)
    assert row["smart_to_paste"] == pytest.approx(318, abs=0.01)
    assert row["speech_offset_to_paste"] == pytest.approx(764 + 816, abs=0.01)
