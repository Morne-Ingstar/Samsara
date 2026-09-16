"""Queue 60: leaving the DICTATE lane (e.g. "Oracle." -> AVA) killed the process.

2026-09-14 22:57:31 and 23:09:36, ~/.samsara/logs: "[SESSION] mode change dictate
-> ava", then a native "Windows fatal exception: access violation" on the
samsara-qt thread while the hands-free utterance thread sat in
dictation._update_streaming_preview. Cause: the DICTATE preview's top-level
QWidget was destroyed by Python refcounting on the non-Qt thread that dropped
the last reference, while the Qt thread still dispatched events for it.

Native crashes cannot be caught in-process, so the end-to-end regression runs a
child Python that drives the real SessionModeManager, the real dictation
preview wiring (compiled out of dictation.py with ast, never imported), the real
DictatePreviewSession and the real samsara.ui.qt_runtime.
"""
import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from samsara import session_modes
from samsara.session_modes import (
    CommandDispatchResult, SessionMode, SessionModeManager, UtteranceSignals, outcome_chip,
)

ROOT = Path(__file__).resolve().parents[1]
GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))
INVOCATIONS = ["oracle", "hey ava", "hey eva"]      # the owner's ava_invocations


def _manager(on_mode_change=None, on_error=None):
    mgr = SessionModeManager(
        abort_phrases=["cancel"],
        ava_invocations=INVOCATIONS,
        foreground_exe_resolver=Mock(return_value="notepad.exe"),
        foreground_hwnd_resolver=Mock(return_value=1),
        inject_fn=Mock(side_effect=lambda text, guard=None: text),
        remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(return_value=CommandDispatchResult(matched=False)),
        agent_dispatch_fn=Mock(),
        on_mode_change=on_mode_change,
        on_switch_dispatch_error=on_error,
        buffer_dictate_until_commit=True,
        clock=lambda: 1.0,
    )
    return mgr


# ---------------------------------------------------------------------------
# A mode change whose side effects raise: loud, and a defined state
# ---------------------------------------------------------------------------

def test_mode_change_side_effect_raising_surfaces_error_and_leaves_defined_state(caplog):
    on_error = Mock()
    mgr = _manager(on_mode_change=Mock(side_effect=RuntimeError("preview teardown failed")), on_error=on_error)
    mgr.force_mode(SessionMode.DICTATE)      # entry side effect fails too; must not raise
    assert mgr.mode is SessionMode.DICTATE
    on_error.reset_mock()

    outcome = mgr.dispatch_utterance("Oracle.", GOOD)

    assert outcome.kind == "mode_switch"
    assert outcome.detail["mode"] is SessionMode.AVA
    assert "RuntimeError: preview teardown failed" in outcome.detail["side_effect_error"]
    assert mgr.mode is SessionMode.AVA                       # defined: the switch stands
    on_error.assert_called_once()
    assert outcome_chip(outcome.kind, outcome.detail) == ("→ AVA: display error", "warning")
    assert any("side effects failed; mode is ava" in r.getMessage() for r in caplog.records)
    # The session keeps working: the next utterance goes to Ava.
    assert mgr.dispatch_utterance("what time is it in Tokyo", GOOD).kind == "ava_dispatched"


def test_clean_mode_change_reports_no_side_effect_error():
    mgr = _manager(on_mode_change=Mock())
    mgr.force_mode(SessionMode.DICTATE)
    outcome = mgr.dispatch_utterance("hey ava", GOOD)
    assert outcome.kind == "mode_switch" and "side_effect_error" not in outcome.detail
    assert outcome_chip(outcome.kind, outcome.detail) == ("→ AVA", "accent")


@pytest.mark.parametrize("source", [SessionMode.DICTATE, SessionMode.COMMAND])
@pytest.mark.parametrize("phrase", INVOCATIONS)
def test_every_invocation_phrase_switches_to_ava_from_both_source_modes(source, phrase):
    """Phrase hypothesis, disproved at the dispatch layer: every invocation from
    either source mode takes the identical path (_do_switch -> _switch_mode ->
    on_mode_change). The fatal step was the preview teardown on leaving DICTATE."""
    seen = []
    mgr = _manager(on_mode_change=lambda mode: seen.append((mode, threading.current_thread().name)))
    mgr.force_mode(source)
    seen.clear()
    outcome = mgr.dispatch_utterance(phrase.title() + ".", GOOD)
    assert outcome.kind == "mode_switch" and mgr.mode is SessionMode.AVA
    assert seen == [(SessionMode.AVA, threading.current_thread().name)]


# ---------------------------------------------------------------------------
# The overlay's widget is only ever destroyed on the Qt (owner) thread
# ---------------------------------------------------------------------------

def test_overlay_closed_and_dropped_on_a_worker_thread_is_destroyed_on_the_qt_thread(qapp):
    from samsara import streaming
    overlay = streaming.StreamingOverlayQt()
    overlay.show()
    qapp.processEvents()
    widget = overlay._widget
    assert widget is not None and widget in streaming._LIVE_WIDGETS
    destroyed_on = []
    widget._w.destroyed.connect(lambda *_: destroyed_on.append(threading.current_thread().name))
    del widget

    def worker():                          # the hands-free utterance thread's role
        nonlocal overlay
        overlay.update_text("it's going away", "listening")
        overlay.close()
        overlay = None                     # last reference dropped HERE, off the Qt thread
    t = threading.Thread(target=worker, name="cmd-utt-queue")
    t.start()
    t.join()
    assert destroyed_on == []              # nothing was destroyed on the worker
    for _ in range(5):
        qapp.processEvents()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed_on == [threading.current_thread().name]   # the Qt (owner) thread
    assert not streaming._LIVE_WIDGETS


def test_flash_fade_completion_disposes_the_widget_on_the_qt_thread(qapp):
    from samsara import streaming
    overlay = streaming.StreamingOverlayQt()
    overlay.show()
    qapp.processEvents()
    widget = overlay._widget
    done = []
    overlay.flash_done_and_fade(lambda: done.append(threading.current_thread().name))
    qapp.processEvents()
    widget._w._fade_alpha = 0.0            # skip the 40 ms animation
    widget._w._fade_step()
    assert done == [threading.current_thread().name]
    assert overlay._widget is None
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert widget not in streaming._LIVE_WIDGETS


# ---------------------------------------------------------------------------
# End to end, in a child process: the crash itself
# ---------------------------------------------------------------------------

_CHILD = textwrap.dedent(r'''
    import ast, faulthandler, json, logging, sys, threading, time, types
    faulthandler.enable()
    sys.path.insert(0, {root!r})
    from unittest.mock import Mock
    from samsara.ui import qt_runtime
    from samsara import streaming
    from samsara.session_modes import (CommandDispatchResult, SessionMode,
        SessionModeManager, UtteranceSignals)

    GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,))
    destroyed_on = []
    _orig_init = streaming._StreamingWidget.__init__
    def _init(self, dim, *args, **kwargs):      # queue 75 added idle arguments
        _orig_init(self, dim, *args, **kwargs)
        self._w.destroyed.connect(lambda *_: destroyed_on.append(threading.current_thread().name))
    streaming._StreamingWidget.__init__ = _init

    src = open({dictation!r}, encoding="utf-8").read()
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    names = {{"_update_streaming_preview", "_ensure_streaming_preview", "_release_streaming_preview"}}
    nodes = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    mod = types.ModuleType("dictation_preview_extract")
    mod.SessionMode = SessionMode
    mod.logger = logging.getLogger("dictation_preview_extract")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "dictation.py", "exec"), mod.__dict__)

    class App:
        pass
    app = App()
    app.config = {{}}
    app.model_lock = threading.Lock()
    app._hotkey_recording = False
    app._dictate_preview = None
    for name in names:
        setattr(app, name, types.MethodType(mod.__dict__[name], app))

    mgr = SessionModeManager(
        abort_phrases=["cancel"], ava_invocations=["oracle", "hey ava", "hey eva"],
        foreground_exe_resolver=lambda: "notepad.exe", foreground_hwnd_resolver=lambda: 1,
        inject_fn=lambda text, guard=None: text, remove_chars_fn=lambda n: None,
        command_dispatch_fn=lambda text: CommandDispatchResult(matched=False),
        agent_dispatch_fn=lambda text, ctx: None,
        on_mode_change=app._update_streaming_preview,   # the side effect that crashed
        buffer_dictate_until_commit=True)
    app._ensure_session_mode_manager = lambda: mgr

    SAY = {{SessionMode.DICTATE: "dictate mode", SessionMode.COMMAND: "command mode",
            SessionMode.AVA: "hey ava"}}
    results = []

    def go(source, utterance):
        mgr.force_mode(source)
        if source is SessionMode.DICTATE:           # a fresh, partly-rendered preview
            deadline = time.monotonic() + 5
            while app._dictate_preview is None or app._dictate_preview._overlay._widget is None:
                if time.monotonic() > deadline:
                    raise SystemExit("preview widget never appeared")
                time.sleep(0.005)
            for n in range(20):
                app._dictate_preview._overlay.set_transcript([f"line {{n}} it's"], "partial")
        outcome = mgr.dispatch_utterance(utterance, GOOD)
        results.append([source.value, utterance, outcome.kind, mgr.mode.value])
        time.sleep(0.02)

    def worker():
        for rep in range(3):
            for source in SessionMode:
                for target in SessionMode:
                    if source is not target:
                        go(source, SAY[target])
            for source in (SessionMode.DICTATE, SessionMode.COMMAND):
                for phrase in ("Oracle.", "Hey Ava.", "Hey Eva."):
                    go(source, phrase)
        mgr.force_mode(SessionMode.COMMAND)

    qt_runtime.ensure_started()
    t = threading.Thread(target=worker, name="cmd-utt-queue")
    t.start()
    t.join()
    time.sleep(0.5)
    qt_runtime.post(lambda: None)
    time.sleep(0.3)
    print(json.dumps({{"results": results, "destroyed_on": destroyed_on,
                      "live": len(streaming._LIVE_WIDGETS)}}), flush=True)
    import os
    os._exit(0)
''')


def test_dictate_to_ava_preview_teardown_2026_09_14_does_not_kill_the_process(tmp_path):
    script = tmp_path / "child_60.py"
    script.write_text(_CHILD.format(root=str(ROOT), dictation=str(ROOT / "dictation.py")), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (
        f"child died with {proc.returncode:#x}\nstdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-4000:]}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    results = data["results"]
    # Every pair (3 x 2) and every invocation from DICTATE and COMMAND, 3 times.
    assert len(results) == 3 * (6 + 6)
    for source, utterance, kind, mode in results:
        assert kind == "mode_switch", (source, utterance, kind)
        expected = {"dictate mode": "dictate", "command mode": "command"}.get(utterance, "ava")
        assert mode == expected, (source, utterance, mode)
    exits_from_dictate = sum(1 for source, *_ in results if source == "dictate")
    assert len(data["destroyed_on"]) >= exits_from_dictate
    assert set(data["destroyed_on"]) == {"samsara-qt"}, data["destroyed_on"]
    assert data["live"] == 0
