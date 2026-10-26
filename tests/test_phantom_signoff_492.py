"""492: synthetic sign-off evidence at real recogniser guard boundaries."""
from samsara.log import describe_text, safe_exception
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import config_defaults
from samsara.hallucination_filter import SignoffContext, is_signoff_hallucination
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals
from tests.test_silent_hold_387 import make_worker, _segment


def _batch_boundary(lane):
    """Execute the actual guard statements, without executing app bootstrap."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    guards = []
    for method in cls.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        for node in ast.walk(method):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            if isinstance(node.value.func, ast.Name) and node.value.func.id == "is_signoff_hallucination":
                guards.append((method, node))
    # Hands-free has the adjacent return branch; hold blanks text instead.
    method, guard = next((m, g) for m, g in guards if
                         ("command_mode_utterance" in m.name) == (lane == "hands-free"))
    parent = next(n for n in ast.walk(method) if isinstance(getattr(n, "body", None), list) and guard in n.body)
    index = parent.body.index(guard)
    body = parent.body[index:index + 2]
    assert isinstance(body[1], ast.If) and isinstance(body[1].test, ast.Name) and body[1].test.id == "_dropped"
    body = [*body, ast.Return(ast.Name("text", ast.Load()))]
    fn = ast.FunctionDef(name="boundary", args=ast.arguments(posonlyargs=[], args=[ast.arg("self"), ast.arg("text"), ast.arg("seg_list"), ast.arg("audio_duration")], kwonlyargs=[], kw_defaults=[], defaults=[]), body=body, decorator_list=[])
    ns = dict(describe_text=describe_text, safe_exception=safe_exception,
              SignoffContext=SignoffContext, is_signoff_hallucination=is_signoff_hallucination,
              config_defaults=config_defaults, logger=logging.getLogger("492.synthetic"),
              _capture_mode="dictate")
    # Hold uses the same segments under its diagnostic variable name.
    fn.body.insert(0, ast.Assign([ast.Name("_diag_all_segs", ast.Store())], ast.Name("seg_list", ast.Load())))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "guard492", "exec"), ns)
    return ns["boundary"]


@pytest.mark.parametrize("lane", ["hands-free", "hold"])
@pytest.mark.parametrize("text,weak,kept", [("Thanks.", True, False), ("Thanks.", False, True), ("Thanks for helping with the document.", True, True)])
def test_batch_guard_prevents_phantom_staging(lane, text, weak, kept):
    segment = _segment(text=text, no_speech_prob=.55 if weak else .1, avg_logprob=-.3,
                       start=0., end=1.5)
    app = SimpleNamespace(config={}, _log_cmd_utt_dropped=Mock(), _show_outcome_chip=Mock())
    result = _batch_boundary(lane)(app, text, [segment], 1.5)
    manager = SessionModeManager(abort_phrases=[], foreground_exe_resolver=lambda: "synthetic.exe",
                                 inject_fn=Mock(), remove_chars_fn=Mock(),
                                 command_dispatch_fn=Mock(), agent_dispatch_fn=Mock(), live_surface_enabled=True,
                                 buffer_dictate_until_commit=True)
    manager.reset(SessionMode.DICTATE)
    if result:
        if lane == "hands-free":
            manager.dispatch_utterance(result, UtteranceSignals(True, (1.2,), True))
        else:
            manager.stage_parked_hold(result)
    assert bool(manager.dictate_pending_buffer) is kept
    if not kept and lane == "hands-free":
        # 899: pin 891's specific recovery copy after an attempted decode.
        assert app._show_outcome_chip.call_args.args[0] == 'Nothing heard - check the mic, or speak louder'


@pytest.mark.parametrize("text,weak,kept", [("Thanks.", True, False), ("Thanks.", False, True), ("Thanks for helping with the document.", True, True)])
def test_streaming_hold_final_guard(make_worker, text, weak, kept):
    worker, session = make_worker([_segment(text=text, no_speech_prob=.55 if weak else .1,
                                          avg_logprob=-.3, start=0., end=1.5)])
    import numpy as np
    session._stop_capture.return_value = np.zeros(24000, dtype=np.float32)
    worker._final_pass()
    final = session.on_final.call_args.args[0]
    assert bool(final) is kept
    if not kept:
        session.app.process_transcription.assert_not_called()


def test_thanks_missing_evidence_fails_open_and_toggle_remains_available():
    assert is_signoff_hallucination("Thanks.", SignoffContext([{}], 1.5))[0] is False
    assert is_signoff_hallucination("Thanks.", SignoffContext([{"no_speech_prob": .9}], 1.5, False)) == (False, "disabled")
