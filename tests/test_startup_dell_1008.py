"""1008: isolated fresh-start DSP, timing, logging and Qt regressions."""
import ast
import builtins
import logging
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
RATIOS = [(160, 441), (1, 3), (1, 6)]


def _source_tree(path):
    return ast.parse((ROOT / path).read_text(encoding="utf-8"))


@pytest.fixture
def filters(monkeypatch, tmp_path):
    from samsara.audio_engine import engine
    monkeypatch.setattr(engine, "_FILTER_CACHE", {})
    monkeypatch.setattr(engine, "_filter_cache_path",
                        lambda up, down: tmp_path / f"{up}_{down}.npz")
    return engine


@pytest.mark.parametrize("up,down", RATIOS)
def test_filter_cold_and_cache_paths_under_100ms(filters, monkeypatch, up, down, record_property):
    # Deny the import even when scipy is already warm in the test process.
    original = builtins.__import__
    def no_scipy(name, *args, **kwargs):
        if name == "scipy" or name.startswith("scipy."):
            raise AssertionError("fresh ACE filter must not import scipy")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_scipy)
    start = time.perf_counter()
    cold = filters._get_polyphase_filter(up, down)
    cold_ms = (time.perf_counter() - start) * 1000
    assert cold_ms < 100, cold_ms
    assert not cold[0].flags.writeable
    start = time.perf_counter()
    assert filters._get_polyphase_filter(up, down) is cold
    memory_ms = (time.perf_counter() - start) * 1000
    assert memory_ms < 100
    filters._FILTER_CACHE.clear()
    monkeypatch.setattr(filters, "_design_polyphase_filter",
                        Mock(side_effect=AssertionError("cache must avoid redesign")))
    start = time.perf_counter()
    disk = filters._get_polyphase_filter(up, down)
    disk_ms = (time.perf_counter() - start) * 1000
    assert disk_ms < 100, disk_ms
    np.testing.assert_array_equal(cold[0], disk[0])
    assert cold[1:] == disk[1:]
    assert not disk[0].flags.writeable
    for name, value in (("cold_ms", cold_ms), ("memory_ms", memory_ms), ("disk_ms", disk_ms)):
        record_property(name, value)


@pytest.mark.parametrize("up,down", RATIOS + [(2, 3)])
def test_filter_taps_and_output_match_scipy(filters, up, down):
    from scipy.signal import firwin, resample_poly
    half = 10 * max(up, down)
    reference = firwin(2 * half + 1, 1 / max(up, down), window=("kaiser", 5.0)) * up
    padded = np.concatenate((np.zeros(down - half % down), reference))
    padded = np.pad(padded, (0, -len(padded) % up))
    taps, phases, remove = filters._design_polyphase_filter(up, down)
    np.testing.assert_allclose(taps, padded, rtol=0, atol=1e-14)
    assert phases == len(padded) // up
    assert remove == (half + down - half % down) // down
    # Full-scale random audio and tones include boundary/transient behavior.
    count = 1600 * down // up
    samples = np.random.default_rng(1008).uniform(-1, 1, count).astype(np.float32)
    for x in (samples, np.sin(np.arange(count) * 0.17).astype(np.float32), np.zeros(count, np.float32)):
        actual = filters._polyphase_resample(x, up, down, taps, phases, remove, 1600)
        np.testing.assert_allclose(actual, resample_poly(x, up, down).astype(np.float32),
                                   rtol=0, atol=1e-6)


def test_corrupt_cache_falls_back_to_fast_design(filters):
    filters._filter_cache_path(160, 441).write_bytes(b"invalid npz")
    state = filters._get_polyphase_filter(160, 441)
    expected = filters._design_polyphase_filter(160, 441)
    np.testing.assert_array_equal(state[0], expected[0])


def test_config_step_clocks_exclude_wizard_without_importing_entrypoint(monkeypatch):
    from samsara import boot
    tree = _source_tree("dictation.py")
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    reset = next(n for n in init.body if isinstance(n, ast.Expr)
                 and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == "_boot.begin_thread")
    index = init.body.index(reset)
    diag_reset = init.body[index + 1]
    wizard_line = next(n.lineno for n in ast.walk(init) if isinstance(n, ast.Call)
                       and ast.unparse(n.func) == "wizard.run")
    load_line = next(n.lineno for n in ast.walk(init) if isinstance(n, ast.Call)
                     and ast.unparse(n.func) == "self.load_config")
    assert wizard_line < reset.lineno <= diag_reset.lineno < load_line
    assert ast.unparse(diag_reset) == "_bdiag_tp[0] = time.perf_counter()"
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(boot.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(boot.time, "perf_counter", lambda: clock.now)
    lines = []
    timer = boot._BootStageTimer(log=lines.append)
    namespace = {"_boot": timer, "_bdiag_tp": [0.0], "time": boot.time}
    clock.now = 97.0  # first-run wizard wait
    exec(compile(ast.Module(body=[reset, diag_reset], type_ignores=[]), "<config timing seam>", "exec"), namespace)
    clock.now += 0.025
    timer("config load")
    assert "config load: 25ms" in lines[0]
    assert "total 97025ms" in lines[0]
    assert (clock.now - namespace["_bdiag_tp"][0]) * 1000 == pytest.approx(25)


def test_qt_font_diagnostics_debug_once_per_font(monkeypatch):
    from PySide6 import QtCore
    from samsara import boot
    monkeypatch.setattr(QtCore, "qInstallMessageHandler", lambda handler: None)
    log = Mock()
    handler = boot.install_qt_message_handler(log)
    for font in ("Synthetic A", "Synthetic B", "Synthetic A"):
        for script in (20, 21):
            handler(QtCore.QtMsgType.QtWarningMsg, None,
                    f'OpenType support missing for "{font}", script {script}')
    assert log.log.call_count == 2
    assert all(call.args[0] == logging.DEBUG for call in log.log.call_args_list)
    log.reset_mock()
    for kind, level in ((QtCore.QtMsgType.QtWarningMsg, logging.WARNING),
                        (QtCore.QtMsgType.QtCriticalMsg, logging.ERROR)):
        handler(kind, None, "synthetic diagnostic")
        assert log.log.call_args.args[0] == level
    handler(QtCore.QtMsgType.QtCriticalMsg, None,
            'OpenType support missing for "Synthetic A", script 20')
    assert log.log.call_args.args[0] == logging.ERROR


def test_startup_home_tab_order_stays_in_same_window(qapp, monkeypatch):
    from PySide6.QtWidgets import QWidget
    from samsara.ui import home_qt, home_signals
    from tests.test_home_qt import _app, _FakeSettings
    monkeypatch.setattr(home_qt, "QSettings", _FakeSettings)
    monkeypatch.setattr(home_signals, "corrections_waiting", lambda app: 0)
    monkeypatch.setattr(home_signals, "_user_words_exist", lambda: True)
    monkeypatch.setattr(home_signals, "generic_hints", lambda app: [])
    monkeypatch.setattr(home_signals, "_diag_records", lambda app, limit: [])
    pairs, invalid = [], []
    original = QWidget.setTabOrder
    def tracked(first, second):
        pairs.append((first, second))
        if first.window() is not second.window():
            invalid.append((first.accessibleName(), second.accessibleName()))
        original(first, second)
    monkeypatch.setattr(QWidget, "setTabOrder", tracked)
    window = home_qt.HomePage(_app())
    try:
        assert pairs
        assert not invalid, invalid
        # Keep the intended links through the hidden guide tray, rather than
        # suppressing the warning or dropping those controls from the chain.
        chain = window._tab_chain
        guides = list(window._guide_btns.values())
        start = chain.index(guides[0])
        assert chain[start:start + len(guides)] == guides
        assert all(widget.window() is window for widget in chain)
    finally:
        from PySide6.QtCore import QCoreApplication, QEvent
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)


def test_main_and_fallback_log_handlers_write_utf8(tmp_path, monkeypatch):
    # Extract only the logging declaration/configuration, never entrypoint boot.
    import sys
    import threading
    from logging.handlers import RotatingFileHandler
    from samsara import log
    tree = _source_tree("dictation.py")
    handler_class = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                         and n.name == "_SafeRotatingFileHandler")
    handler_assign = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "file_handler" for t in n.targets))
    namespace = {"_RotatingFileHandler": RotatingFileHandler, "LOG_FILE": tmp_path / "main.log"}
    exec(compile(ast.Module(body=[handler_class, handler_assign], type_ignores=[]),
                 "<logging declarations>", "exec"), namespace)
    main = namespace["file_handler"]
    # Exercise the production fallback with a detached root, preserving pytest's handlers.
    root = logging.Logger("isolated-1008", logging.DEBUG)
    monkeypatch.setattr(log.logging, "getLogger", lambda *args: root)
    monkeypatch.setattr(log, "_configured", False)
    monkeypatch.setattr(log, "_configure_lock", threading.Lock())
    monkeypatch.setattr(log, "SAMSARA_LOG_FILE", tmp_path / "fallback.log")
    monkeypatch.setattr(sys, "stdout", None)
    log._fallback_configure()
    message = "[ACE] Engine started \u2014 hold / caf\u00e9 \u2713"
    try:
        for handler in (main, *root.handlers):
            handler.handle(logging.LogRecord("synthetic", logging.INFO, "", 0, message, (), None))
            handler.flush()
            data = Path(handler.baseFilename).read_bytes()
            assert message in data.decode("utf-8")
            assert b"\xe2\x80\x94" in data
    finally:
        main.close()
        for handler in root.handlers:
            handler.close()


def test_splash_close_uses_qt_timer_without_worker(qapp, monkeypatch):
    from samsara.ui import splash_qt
    queued, timers, calls = [], [], []
    monkeypatch.setattr(splash_qt.qt_runtime, "post", queued.append)
    monkeypatch.setattr(splash_qt.QTimer, "singleShot", lambda *args: timers.append(args))
    monkeypatch.setattr(splash_qt.time, "time", lambda: 10.0)
    splash = splash_qt.SplashScreenQt.__new__(splash_qt.SplashScreenQt)
    splash._start_time = 10.0
    splash._widget = SimpleNamespace(_begin_close=lambda: calls.append("closed"))
    splash.close()
    assert len(queued) == 1 and not timers and not calls
    queued.pop()()
    delay, context, close = timers.pop()
    assert delay >= splash_qt._MIN_DISPLAY_S * 1000
    assert context is splash._widget
    close()
    assert calls == ["closed"]
    splash._widget = None
    splash.close()
    queued.pop()()
    assert not timers


def test_faulthandler_remains_enabled(tmp_path, monkeypatch):
    import faulthandler
    from samsara import boot
    monkeypatch.setattr(boot, "migrate_before_boot", lambda: None)
    enable = Mock()
    monkeypatch.setattr(faulthandler, "enable", enable)
    stream = boot._enable_faulthandler(tmp_path)
    try:
        enable.assert_called_once_with(file=stream, all_threads=True)
        assert not stream.closed
    finally:
        stream.close()
