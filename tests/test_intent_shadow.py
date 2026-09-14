"""Shadow-mode intent gate over DICTATE (queue 36): observer only.

The gate records what it WOULD have done for each finalised DICTATE utterance
and must never change what the app does. dictation.py's hook method is bound
onto small stand-ins (dictation is imported inside helpers, never at module
level); the session state machine is the real SessionModeManager.
"""

import copy
import inspect
import json
import re
import statistics
import threading
import time
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from samsara.intent import shadow as sh
from samsara.session_modes import (
    CommandDispatchResult,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
)

REPO = Path(__file__).resolve().parent.parent

EXPECTED_KEYS = {"v", "ts", "text", "delivery", "would", "confidence", "tier", "elapsed_us",
                 "t12_us", "suggestions", "chain", "app"}

#: The DICTATE regression corpus: ordinary dictation (incl. phrases that look
#: like commands), the look-alike commit words, control phrases, a hands-free
#: command, scratch-that and a mode switch -- replayed on and off.
CORPUS = [
    "dictate mode",
    "hello world",
    "the meeting moved to thursday",
    "open chrome",
    "close the tab before you leave",
    "yesterday I told my sister we should close tab before dinner",
    "and then we went home",
    "the end",
    "weekend",
    "scratch that",
    "remind me to buy milk",
    "new line",
    "end",
    "scroll down a little",
    "please take a screenshot of the error",
    "command mode",
    "open chrome",
    "dictate mode",
    "thank you so much",
    "cancel",
]


def _signals(text):
    return UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2,),
                            audio_ref=f"C:/audio/{abs(hash(text)) % 997}.wav")


def _manager(buffered):
    mocks = {
        "foreground": Mock(return_value="notepad.exe"),
        "foreground_hwnd": Mock(return_value=12345),
        "inject": Mock(return_value=True),
        "remove_chars": Mock(),
        "command_dispatch": Mock(return_value=CommandDispatchResult(matched=False)),
        "agent_dispatch": Mock(),
        "on_mode_change": Mock(), "on_focus_lock_revert": Mock(), "on_scratch_result": Mock(),
        "on_abort": Mock(), "on_switch_dispatch_error": Mock(),
    }
    mgr = SessionModeManager(
        abort_phrases=["cancel", "abort"],
        foreground_exe_resolver=mocks["foreground"],
        foreground_hwnd_resolver=mocks["foreground_hwnd"],
        inject_fn=mocks["inject"],
        remove_chars_fn=mocks["remove_chars"],
        command_dispatch_fn=mocks["command_dispatch"],
        agent_dispatch_fn=mocks["agent_dispatch"],
        on_mode_change=mocks["on_mode_change"],
        on_focus_lock_revert=mocks["on_focus_lock_revert"],
        on_scratch_result=mocks["on_scratch_result"],
        on_abort=mocks["on_abort"],
        on_switch_dispatch_error=mocks["on_switch_dispatch_error"],
        buffer_dictate_until_commit=buffered,
        clock=lambda: 1000.0,
    )
    return mgr, mocks


def _state(mgr):
    """Every non-callable session attribute, deep-copied and rendered."""
    snapshot = {}
    for name, value in sorted(vars(mgr).items()):
        if callable(value) and not isinstance(value, (list, dict, set, tuple)):
            continue
        snapshot[name] = repr(copy.deepcopy(value)) if not name.startswith("_stack") else repr(
            [(i.kind, i.payload, i.mode, i.extra) for i in getattr(value, "_items", [])] or value)
    return snapshot


@pytest.fixture(scope="module")
def resolver():
    from samsara import command_catalog as cc
    from samsara.intent.resolve import IntentResolver
    return IntentResolver(cc.load_catalog_json())


class _NoThread:
    """spawn() stand-in: records the worker, never starts a thread (tests drain)."""

    def __init__(self):
        self.spawned = []

    def __call__(self, name, target, daemon=True, **kw):
        self.spawned.append(name)
        return types.SimpleNamespace(is_alive=lambda: True, name=name)


def _drain(shadow):
    while True:
        try:
            item = shadow._queue.get_nowait()
        except Exception:
            return
        if item is not sh._STOP:
            shadow.record(*item)


def _stub_app(tmp_path, *, enabled=True, resolver=None, spawn=None):
    import dictation

    app = types.SimpleNamespace()
    app.config = {"intent": {"shadow_enabled": enabled, "shadow_dir": str(tmp_path / "shadow")}}
    app._intent_shadow_observe = dictation.DictationApp._intent_shadow_observe.__get__(app)
    if resolver is not None:
        app._intent_shadow = sh.IntentShadow(lambda: resolver, lambda: app.config,
                                             spawn=spawn or _NoThread(), pid_fn=lambda: 4242,
                                             name_fn=lambda pid: "notepad.exe")
    return app


def _replay(buffered, app=None):
    mgr, mocks = _manager(buffered)
    outcomes = []
    for text in CORPUS:
        was_dictate = mgr.mode is SessionMode.DICTATE
        outcome = mgr.dispatch_utterance(text, _signals(text))
        outcomes.append((outcome.kind, repr(outcome.detail)))
        if app is not None and was_dictate:
            app._intent_shadow_observe(text, outcome)
            _drain(app._intent_shadow)
    calls = {name: [re.sub(r" at 0x[0-9A-Fa-f]+", "", repr(c)) for c in m.mock_calls]
             for name, m in mocks.items()}
    return outcomes, _state(mgr), calls


# ---------------------------------------------------------------------------
# 1. The gate never mutates the session
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("buffered", [True, False], ids=["buffered-hands-free", "immediate-inject"])
def test_outcomes_session_state_and_side_effects_identical_with_shadow_on_and_off(
        tmp_path, resolver, buffered):
    off = _replay(buffered)
    app = _stub_app(tmp_path, resolver=resolver)
    on = _replay(buffered, app)
    assert on[0] == off[0]                 # every DispatchOutcome kind + detail
    assert on[1] == off[1]                 # every session attribute, stack included
    assert on[2] == off[2]                 # every inject/command/agent/callback call
    lines = _lines(tmp_path)
    assert lines, "the shadow actually ran"
    assert {l["delivery"] for l in lines} == {"staged" if buffered else "injected"}
    dictated = [t for t, (k, _d) in zip(CORPUS, on[0]) if k in sh.OBSERVED_OUTCOMES]
    assert [l["text"] for l in lines] == dictated


def test_observer_only_ever_sees_text_and_the_outcome_kind():
    import dictation
    src = inspect.getsource(dictation.DictationApp._intent_shadow_observe)
    code = src.split('"""', 2)[2]                              # the body, without the docstring
    assert "shadow.observe(str(text), str(getattr(outcome, 'kind', '')))" in code
    assert "_session_mode_manager" not in code and "detail" not in code
    assert "outcome" in code and code.count("outcome") == 1    # read once, for its kind


def test_hook_sits_after_dispatch_and_outcome_handling():
    import dictation
    src = inspect.getsource(dictation.DictationApp._handle_command_mode_utterance)
    dispatch = src.index("outcome = manager.dispatch_utterance(text, signals)")
    handled = src.index("self._handle_session_dispatch_outcome(outcome, text)")
    preview = src.index("self._dictate_preview.on_utterance_final(")
    hook = src.index("self._intent_shadow_observe(text, outcome)")
    assert dispatch < handled < preview < hook
    assert src.count("self._intent_shadow_observe(") == 1


# ---------------------------------------------------------------------------
# 2. Nothing escapes
# ---------------------------------------------------------------------------

class _Boom:
    def resolve(self, text):
        raise RuntimeError("resolver exploded")


def test_exception_inside_resolve_cannot_escape(tmp_path, caplog):
    config = {"intent": {"shadow_dir": str(tmp_path)}}
    shadow = sh.IntentShadow(lambda: _Boom(), lambda: config, spawn=_NoThread(), pid_fn=lambda: None)
    for _ in range(3):
        assert shadow.observe("open chrome", "dictate_staged") is True
    _drain(shadow)
    lines = _lines(tmp_path, folder=tmp_path)
    assert [l["would"] for l in lines] == ["miss"] * 3 and lines[0]["error"] == "RuntimeError"
    assert shadow.errors == 3
    assert sum("[INTENT-SHADOW]" in r.getMessage() for r in caplog.records) == 1   # logged once


def test_catalog_that_cannot_build_is_a_miss_not_a_crash(tmp_path):
    config = {"intent": {"shadow_dir": str(tmp_path)}}

    def _factory():
        raise OSError("registry unavailable")

    shadow = sh.IntentShadow(_factory, lambda: config, spawn=_NoThread(), pid_fn=lambda: None)
    shadow.observe("hello there", "dictate_injected")
    shadow.observe("open chrome", "dictate_injected")
    _drain(shadow)
    lines = _lines(tmp_path, folder=tmp_path)
    assert [l["would"] for l in lines] == ["miss", "miss"] and shadow.errors == 1


def test_hook_swallows_anything_the_shadow_throws(tmp_path):
    app = _stub_app(tmp_path)
    app._intent_shadow = types.SimpleNamespace(observe=MagicMock(side_effect=ValueError("broken")))
    outcome = types.SimpleNamespace(kind="dictate_staged", detail={})
    for _ in range(2):
        app._intent_shadow_observe("hello", outcome)          # must not raise
    assert app._intent_shadow_errors == 2


def test_record_never_raises_on_unwritable_folder(tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    config = {"intent": {"shadow_dir": str(blocker / "shadow")}}
    shadow = sh.IntentShadow(lambda: _Boom(), lambda: config, spawn=_NoThread(), pid_fn=lambda: None)
    assert shadow.record("hi", "staged", datetime.now(), None) is None
    assert shadow.errors >= 1


# ---------------------------------------------------------------------------
# 3. Disabled means nothing at all
# ---------------------------------------------------------------------------

def test_disabled_creates_no_shadow_no_thread_no_file(tmp_path, monkeypatch):
    import samsara.intent.shadow as shadow_module
    monkeypatch.setattr(shadow_module, "IntentShadow", MagicMock(side_effect=AssertionError("constructed")))
    app = _stub_app(tmp_path, enabled=False)
    outcome = types.SimpleNamespace(kind="dictate_staged", detail={})
    app._intent_shadow_observe("open chrome", outcome)
    assert not hasattr(app, "_intent_shadow")
    assert not (tmp_path / "shadow").exists()


def test_disabled_mid_session_writes_nothing(tmp_path, resolver):
    spawn = _NoThread()
    config = {"intent": {"shadow_enabled": True, "shadow_dir": str(tmp_path)}}
    shadow = sh.IntentShadow(lambda: resolver, lambda: config, spawn=spawn, pid_fn=lambda: None)
    config["intent"]["shadow_enabled"] = False
    assert shadow.observe("open chrome", "dictate_staged") is False
    assert spawn.spawned == [] and list(tmp_path.iterdir()) == []


def test_only_dictation_outcomes_are_observed(tmp_path, resolver):
    spawn = _NoThread()
    config = {"intent": {"shadow_dir": str(tmp_path)}}
    shadow = sh.IntentShadow(lambda: resolver, lambda: config, spawn=spawn, pid_fn=lambda: None)
    for kind in ("mode_switch", "scratch_success", "dictate_committed", "command_executed",
                 "hands_free_command_executed", "abort", "empty"):
        assert shadow.observe("open chrome", kind) is False
    assert spawn.spawned == []


def test_config_key_and_defaults():
    from samsara.config_schema import SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["intent.shadow_enabled"] == {"type": "bool", "default": True, "tab": "advanced"}
    assert sh.shadow_enabled({}) is True and sh.DEFAULT_ENABLED is True
    assert sh.shadow_enabled({"intent": {"shadow_enabled": False}}) is False
    assert sh.shadow_dir({"intent": {"shadow_dir": "D:/x"}}) == Path("D:/x")
    assert sh.shadow_dir({}).name == "shadow"


# ---------------------------------------------------------------------------
# 4. Privacy: no window title, no audio
# ---------------------------------------------------------------------------

def test_log_line_schema_has_no_title_and_no_audio(tmp_path, resolver):
    app = _stub_app(tmp_path, resolver=resolver)
    mgr, _ = _manager(True)
    mgr.dispatch_utterance("dictate mode", _signals("dictate mode"))
    text = "please take a screenshot of the error"
    signals = _signals(text)
    outcome = mgr.dispatch_utterance(text, signals)
    app._intent_shadow_observe(text, outcome)
    _drain(app._intent_shadow)
    [line] = _lines(tmp_path)
    assert set(line) == EXPECTED_KEYS
    raw = (tmp_path / "shadow").glob("intent-*.jsonl").__next__().read_text(encoding="utf-8")
    assert signals.audio_ref not in raw and ".wav" not in raw
    assert "title" not in raw.lower()
    assert line["app"] == "notepad.exe" and line["delivery"] == "staged"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}", line["ts"])


def test_shadow_module_never_reads_a_window_title():
    src = Path(sh.__file__).read_text(encoding="utf-8")
    for forbidden in ("GetWindowText", "window_title", "WindowText", "audio_ref"):
        assert forbidden not in src.replace("NEVER a window title", "").replace("no window title", "")\
            .replace("never a window title", "").replace("no audio path", ""), forbidden


def test_would_have_done_labels(tmp_path, resolver):
    config = {"intent": {"shadow_dir": str(tmp_path)}}
    shadow = sh.IntentShadow(lambda: resolver, lambda: config, spawn=_NoThread(), pid_fn=lambda: None)
    for text in ("open chrome", "yesterday I told my sister we should close tab before dinner",
                 "clothes tab", "next tab and then close tab"):
        shadow.observe(text, "dictate_staged")
    _drain(shadow)
    by_text = {l["text"]: l for l in _lines(tmp_path, folder=tmp_path)}
    assert by_text["open chrome"]["would"] == "command:builtin.open_chrome"
    assert by_text["open chrome"]["tier"] == "exact" and by_text["open chrome"]["confidence"] == 1.0
    assert by_text["yesterday I told my sister we should close tab before dinner"]["would"] == "dictate"
    assert by_text["clothes tab"]["would"] == "suggest:builtin.close_tab"   # destructive look-alike
    assert by_text["next tab and then close tab"]["chain"] == ["builtin.next_tab", "builtin.close_tab"]
    for line in by_text.values():
        assert isinstance(line["elapsed_us"], int) and line["elapsed_us"] >= 0
        assert line["app"] is None


def test_one_file_per_local_day(tmp_path, resolver):
    config = {"intent": {"shadow_dir": str(tmp_path)}}
    shadow = sh.IntentShadow(lambda: resolver, lambda: config, spawn=_NoThread(), pid_fn=lambda: None)
    shadow.record("hello", "staged", datetime(2026, 9, 14, 23, 59), None)
    shadow.record("hello", "staged", datetime(2026, 9, 15, 0, 1), None)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["intent-2026-09-14.jsonl", "intent-2026-09-15.jsonl"]


# ---------------------------------------------------------------------------
# 5. Off the hot path, through the thread registry
# ---------------------------------------------------------------------------

def test_worker_runs_on_a_thread_registry_thread(tmp_path, resolver):
    from samsara.runtime import thread_registry
    config = {"intent": {"shadow_dir": str(tmp_path)}}
    names = []

    def _spawn(name, target, daemon=True, **kw):
        def _wrapped():
            names.append(threading.current_thread().name)
            target()
        return thread_registry.spawn(name, _wrapped, daemon=daemon)

    shadow = sh.IntentShadow(lambda: resolver, lambda: config, spawn=_spawn, pid_fn=lambda: None)
    try:
        assert shadow.observe("open chrome", "dictate_staged")
        deadline = time.monotonic() + 5
        while shadow.written == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert shadow.written == 1 and names and names[0].startswith("intent-shadow")
    finally:
        shadow.stop()
    src = Path(sh.__file__).read_text(encoding="utf-8")
    assert "threading.Thread(" not in src and "thread_registry.spawn" in src


def test_hot_path_cost_is_a_queue_put(tmp_path, resolver):
    """The finalise path's added cost: observe() only enqueues (resolve runs later)."""
    slow_calls = []

    class _SlowResolver:
        def resolve(self, text):
            slow_calls.append(text)
            time.sleep(0.2)

    config = {"intent": {"shadow_dir": str(tmp_path)}}
    shadow = sh.IntentShadow(lambda: _SlowResolver(), lambda: config, spawn=_NoThread(), pid_fn=lambda: 1)
    costs = []
    for i in range(200):
        t0 = time.perf_counter()
        shadow.observe(f"utterance {i}", "dictate_staged")
        costs.append((time.perf_counter() - t0) * 1e6)
    assert slow_calls == []                                  # nothing resolved on the caller
    assert statistics.median(costs) < 200                    # microseconds
    assert shadow._queue.qsize() == sh.QUEUE_BOUND and shadow.dropped == 200 - sh.QUEUE_BOUND \
        if 200 > sh.QUEUE_BOUND else shadow._queue.qsize() == 200


def test_finalise_path_elapsed_unchanged_within_noise(tmp_path, resolver):
    """dispatch_utterance + hook (shadow on) vs dispatch alone (shadow off)."""
    import dictation  # noqa: F401  (bound method source)

    def _run(app):
        mgr, _ = _manager(True)
        mgr.dispatch_utterance("dictate mode", _signals("dictate mode"))
        samples = []
        for i in range(300):
            text = f"this is dictated sentence number {i}"
            t0 = time.perf_counter()
            outcome = mgr.dispatch_utterance(text, _signals(text))
            if app is not None:
                app._intent_shadow_observe(text, outcome)
            samples.append(time.perf_counter() - t0)
            if app is not None and i % 50 == 49:
                _drain(app._intent_shadow)
        return statistics.median(samples) * 1e6

    off = _run(None)
    on = _run(_stub_app(tmp_path, resolver=resolver))
    assert on - off < 250, (off, on)                         # added microseconds per utterance


# ---------------------------------------------------------------------------
# 6. shadow_report.py
# ---------------------------------------------------------------------------

def test_shadow_report_prints_every_section(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("shadow_report", REPO / "tools" / "shadow_report.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)

    entries = [
        {"text": "hello world", "would": "dictate", "tier": None, "elapsed_us": 120, "t12_us": 80},
        {"text": "open chrome", "would": "command:builtin.open_chrome", "tier": "exact",
         "elapsed_us": 60, "t12_us": 40, "app": "notepad.exe", "ts": "2026-09-14T10:00:00.000"},
        {"text": "open chrome", "would": "command:builtin.open_chrome", "tier": "exact",
         "elapsed_us": 70, "t12_us": 50, "app": "notepad.exe", "ts": "2026-09-14T10:01:00.000"},
        {"text": "go ahead and move chrome to the left screen right now please", "tier": "grammar",
         "would": "command:windows.send", "elapsed_us": 900, "t12_us": 700, "app": "warp.exe",
         "ts": "2026-09-14T10:02:00.000"},
        {"text": "open the purple elephant", "would": "suggest:app_verbs.open", "tier": "grammar",
         "elapsed_us": 400, "t12_us": 300},
        {"text": "something", "would": "miss", "tier": None, "elapsed_us": 5, "t12_us": None,
         "error": "RuntimeError"},
    ]
    path = tmp_path / "intent-2026-09-14.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n", encoding="utf-8")
    loaded, bad = report.read_entries([path])
    out = report.build_report(loaded, bad, [path])
    assert "6 utterance(s)" in out and "1 unreadable line(s)" in out
    assert re.search(r"dictate\s+1\s+16\.7%", out) and re.search(r"command\s+3\s+50\.0%", out)
    assert "2x  builtin.open_chrome" in out
    assert "1x  RuntimeError" in out
    assert "p50" in out and "decided exact" in out
    fp = out.split("False-positive candidates", 1)[1]
    assert "windows.send" in fp and "open chrome" not in fp


def _lines(tmp_path, folder=None):
    folder = folder or (tmp_path / "shadow")
    lines = []
    for path in sorted(Path(folder).glob("intent-*.jsonl")):
        lines += [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return lines
