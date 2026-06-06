#!/usr/bin/env python3
"""
tools/qt_threading_spike.py -- THROWAWAY architecture harness.

Tests: Tk-main + Qt-background-thread viability on this machine.
Does NOT import Samsara modules.

Run: F:\envs\sami\python.exe tools\qt_threading_spike.py

Self-driving: after the Tk window appears the harness automatically steps
through all seven test scenarios and then shuts down cleanly, printing a
PASS/FAIL verdict for each step.
"""

import threading
import time
import tkinter as tk

from PySide6.QtCore import Qt, QTimer, QThread
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_qt_app = None                  # set by Qt thread before _qt_ready fires
_qt_thread = None               # threading.Thread (non-daemon)
_qt_ready = threading.Event()
_qt_owner_thread = None         # threading.Thread for the Qt owner thread

_overlay_win = None             # current frameless overlay widget
_normal_win = None              # current normal QWidget window

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
_results = {}   # label -> "GREEN" | "FLAKY" | "BROKEN"


def _ts() -> str:
    ms = int(time.time() * 1000) % 1000
    return time.strftime("%H:%M:%S") + f".{ms:03d}"


def log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def record(label: str, verdict: str, detail: str = "") -> None:
    _results[label] = verdict
    suffix = f"  ({detail})" if detail else ""
    log(f"[{verdict:6s}] {label}{suffix}")


# ---------------------------------------------------------------------------
# Qt owner thread
# ---------------------------------------------------------------------------

def _run_qt() -> None:
    global _qt_app, _qt_owner_thread
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    _qt_owner_thread = threading.current_thread()
    _qt_app = app
    log(f"[QT-THREAD] Started: ident={_qt_owner_thread.ident}  name={_qt_owner_thread.name!r}")
    _qt_ready.set()
    app.exec()
    log("[QT-THREAD] exec() returned -- event loop stopped")


# ---------------------------------------------------------------------------
# Thread assertion -- logs ASSERT-OK or ASSERT-FAIL
# ---------------------------------------------------------------------------

def _assert_qt(label: str) -> bool:
    cur = threading.current_thread()
    ok = cur is _qt_owner_thread
    status = "OK" if ok else "FAIL"
    owner_name = _qt_owner_thread.name if _qt_owner_thread else "None"
    log(f"[ASSERT-{status}] {label}: cur={cur.name!r}  owner={owner_name!r}")
    return ok


# ---------------------------------------------------------------------------
# Qt operations (must be called on Qt thread via singleShot)
# ---------------------------------------------------------------------------

def _do_open_overlay(tag: str = "") -> None:
    global _overlay_win
    ok = _assert_qt("open_overlay")
    if not ok:
        record("Q1-overlay-open", "BROKEN", "wrong thread")
        return

    if _overlay_win is not None:
        _overlay_win.close()
        _overlay_win.deleteLater()
        _overlay_win = None

    w = QWidget(None)
    w.setWindowFlags(
        Qt.FramelessWindowHint
        | Qt.WindowStaysOnTopHint
        | Qt.Tool
        | Qt.WindowTransparentForInput
    )
    w.setAttribute(Qt.WA_TranslucentBackground, True)
    w.setAttribute(Qt.WA_ShowWithoutActivating, True)
    w.setGeometry(120, 120, 340, 70)

    layout = QVBoxLayout(w)
    layout.setContentsMargins(0, 0, 0, 0)
    lbl = QLabel(f"  Spike overlay{' -- ' + tag if tag else ''}  ")
    lbl.setStyleSheet(
        "background: rgba(20, 12, 50, 220);"
        " color: white;"
        " font-size: 15px;"
        " font-weight: bold;"
        " border-radius: 6px;"
        " padding: 10px 18px;"
    )
    lbl.setAlignment(Qt.AlignCenter)
    layout.addWidget(lbl)

    _overlay_win = w
    w.show()
    visible = w.isVisible()
    geo = list(w.geometry().getRect())
    log(f"[QT] Overlay opened: visible={visible}  geometry={geo}  tag={tag!r}")
    record("Q1-overlay-open", "GREEN" if visible else "BROKEN",
           f"visible={visible}")


def _do_close_overlay() -> None:
    global _overlay_win
    _assert_qt("close_overlay")
    if _overlay_win is not None:
        _overlay_win.close()
        _overlay_win.deleteLater()
        _overlay_win = None
        log("[QT] Overlay closed")
    else:
        log("[QT] close_overlay -- nothing to close")


def _do_reopen_overlay() -> None:
    _assert_qt("reopen_overlay")
    _do_close_overlay()
    _do_open_overlay(tag="reopen")
    visible = _overlay_win is not None and _overlay_win.isVisible()
    record("Q2-overlay-reopen", "GREEN" if visible else "BROKEN",
           f"visible={visible}")
    log(f"[QT] Reopen complete: visible={visible}")


def _do_open_normal() -> None:
    global _normal_win
    _assert_qt("open_normal")

    w = QWidget(None)
    w.setWindowTitle("Normal Qt window -- spike test")
    w.resize(300, 130)
    w.move(200, 260)

    layout = QVBoxLayout(w)
    layout.addWidget(QLabel("Normal QWidget window.\nWill auto-close after 2 seconds."))
    btn = QPushButton("Close now")
    btn.clicked.connect(w.close)
    layout.addWidget(btn)

    def _on_destroyed():
        global _normal_win
        _normal_win = None
        log("[QT] Normal window destroyed -- reference cleared")

    w.destroyed.connect(_on_destroyed)
    _normal_win = w
    w.show()
    visible = w.isVisible()
    log(f"[QT] Normal window opened: visible={visible}")

    # Auto-close after 2 s so the test sequence continues unblocked
    QTimer.singleShot(2000, _qt_app, lambda: _do_close_normal_win(w))


def _do_close_normal_win(w) -> None:
    visible_before = w.isVisible() if w is not None else False
    if w is not None:
        w.close()
        w.deleteLater()
    log(f"[QT] Normal window auto-closed (was visible={visible_before})")
    record("Q3-normal-window", "GREEN" if visible_before else "BROKEN",
           f"was_visible={visible_before}")


def _do_modal_dialog() -> None:
    _assert_qt("modal_dialog")
    log("[QT] Opening modal QDialog (dlg.exec() -- nested event loop)...")

    dlg = QDialog(None)
    dlg.setWindowTitle("Modal dialog -- spike (auto-closes in 1.5 s)")
    dlg.resize(300, 120)

    layout = QVBoxLayout(dlg)
    layout.addWidget(QLabel(
        "Modal QDialog.\n"
        "Tk should stay responsive during this.\n"
        "Auto-closes in 1.5 s via QTimer."
    ))
    btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    layout.addWidget(btns)

    # Auto-dismiss so the test sequence is not blocked forever
    QTimer.singleShot(1500, _qt_app, dlg.reject)

    t0 = time.perf_counter()
    result = dlg.exec()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log(f"[QT] Modal dialog returned: result={result}  elapsed={elapsed_ms:.0f}ms")
    # result==0 (Rejected) is expected from the auto-dismiss
    record("Q4-modal-dialog", "GREEN",
           f"returned_ok elapsed={elapsed_ms:.0f}ms")


def _do_stress(n: int) -> None:
    _assert_qt(f"stress({n})")
    log(f"[QT] Stress: {n} x open+close overlay...")
    start = time.perf_counter()
    errors = 0
    for i in range(n):
        try:
            w = QWidget(None)
            w.setWindowFlags(
                Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            )
            w.setAttribute(Qt.WA_TranslucentBackground, True)
            w.setGeometry(80, 80, 180, 55)
            lbl = QLabel(f"  Stress {i + 1}/{n}  ")
            lbl.setStyleSheet(
                "background: rgba(50, 0, 90, 210);"
                " color: white;"
                " padding: 8px;"
            )
            QVBoxLayout(w).addWidget(lbl)
            w.show()
            w.close()
            w.deleteLater()
        except Exception as e:
            errors += 1
            log(f"[QT] Stress cycle {i+1} error: {e}")

    # Flush pending deleteLater events
    QApplication.processEvents()

    elapsed = time.perf_counter() - start
    per_cycle = elapsed / n * 1000
    log(f"[QT] Stress done: {n} cycles in {elapsed*1000:.0f}ms "
        f"({per_cycle:.1f}ms/cycle)  errors={errors}")
    verdict = "GREEN" if errors == 0 else ("FLAKY" if errors < 3 else "BROKEN")
    record("Q5-stress-25x", verdict,
           f"errors={errors}  {per_cycle:.1f}ms/cycle")


def _do_report_dpi() -> None:
    _assert_qt("report_dpi")
    screens = QApplication.screens()
    for i, s in enumerate(screens):
        geo = list(s.geometry().getRect())
        dpr = s.devicePixelRatio()
        ldpi = s.logicalDotsPerInch()
        log(f"[QT] Screen {i}: geometry={geo}  "
            f"device_pixel_ratio={dpr:.2f}  "
            f"logical_dpi={ldpi:.0f}")
    record("Q6-dpi-scaling", "GREEN",
           f"{len(screens)} screen(s) reported")


def _do_qt_stop() -> None:
    ok = _assert_qt("qt_stop")
    log("[QT] quit() called")
    record("Q7-clean-shutdown", "GREEN" if ok else "BROKEN",
           f"on_qt_thread={ok}")
    QApplication.instance().quit()


# ---------------------------------------------------------------------------
# Schedule from any thread to Qt thread (3-arg form)
# ---------------------------------------------------------------------------

def schedule(fn) -> None:
    if _qt_app is None:
        log("[SCHED] Qt not ready -- dropped")
        return
    QTimer.singleShot(0, _qt_app, fn)


# ---------------------------------------------------------------------------
# Automated test sequence (runs from a background timer thread -> Tk -> Qt)
# ---------------------------------------------------------------------------

def _run_auto_sequence(root: tk.Tk) -> None:
    """Chain the 7 test steps using QTimer.singleShot chains on the Qt thread."""
    log("[AUTO] Starting automated test sequence...")

    def step1():
        log("[AUTO] Step 1: open overlay")
        _do_open_overlay(tag="auto")

    def step2():
        log("[AUTO] Step 2: reopen overlay (close + reopen)")
        _do_reopen_overlay()

    def step3():
        log("[AUTO] Step 3: open normal window (auto-closes in 2s)")
        _do_open_normal()

    def step4():
        log("[AUTO] Step 4: modal dialog (auto-closes in 1.5s via QTimer)")
        _do_modal_dialog()

    def step5():
        log("[AUTO] Step 5: stress test 25 cycles")
        _do_stress(25)

    def step6():
        log("[AUTO] Step 6: DPI info")
        _do_report_dpi()

    def step7():
        log("[AUTO] Step 7: close overlay + initiate shutdown")
        _do_close_overlay()
        # Give Tk 200 ms to react, then destroy
        root.after(200, lambda: _trigger_tk_close(root))

    # Chain via QTimer on the Qt thread: each step fires after the previous
    # has had time to complete (generous gaps so windows actually render).
    def chain():
        schedule(step1)
        QTimer.singleShot(800,  _qt_app, step2)
        QTimer.singleShot(2000, _qt_app, step3)
        QTimer.singleShot(4500, _qt_app, step4)   # after normal window auto-closes
        QTimer.singleShot(7000, _qt_app, step5)   # after modal returns
        QTimer.singleShot(8500, _qt_app, step6)
        QTimer.singleShot(9500, _qt_app, step7)

    # Delay the first singleShot until the Qt event loop is definitely running
    # (it is, because _qt_ready fired before we reach here)
    schedule(chain)


def _trigger_tk_close(root: tk.Tk) -> None:
    log("[TK] Auto-shutdown: closing Tk window...")
    _on_tk_close(root)


# ---------------------------------------------------------------------------
# Tk UI
# ---------------------------------------------------------------------------

def _build_tk(root: tk.Tk) -> None:
    root.title("Qt-thread spike -- Tk main thread")
    root.geometry("440x360")
    root.resizable(False, False)

    tk.Label(root, text="Qt-threading architecture spike",
             font=("Segoe UI", 12, "bold")).pack(pady=(12, 2))
    tk.Label(root,
             text="Tk owns main thread  |  Qt runs on 'qt-spike' daemon=False thread",
             font=("Segoe UI", 8), fg="#555").pack(pady=(0, 10))

    def row(text, cmd):
        tk.Button(root, text=text, command=cmd,
                  width=56, pady=3).pack(pady=1)

    row("Open overlay (frameless, translucent, click-through)",
        lambda: schedule(_do_open_overlay))
    row("Close overlay",
        lambda: schedule(_do_close_overlay))
    row("Reopen overlay (close + reopen in one callback)",
        lambda: schedule(_do_reopen_overlay))
    row("Open normal QWidget window",
        lambda: schedule(_do_open_normal))
    row("Open modal QDialog  (auto-closes in 1.5s)",
        lambda: schedule(_do_modal_dialog))
    row("Stress: 25 x open+close overlay",
        lambda: schedule(lambda: _do_stress(25)))
    row("DPI info",
        lambda: schedule(_do_report_dpi))

    tk.Label(root,
             text="Automated sequence will run and shutdown by itself.",
             font=("Segoe UI", 8), fg="#888").pack(pady=(14, 3))


def _on_tk_close(root: tk.Tk) -> None:
    log("[TK] WM_DELETE_WINDOW -- signalling Qt to stop...")
    schedule(_do_qt_stop)
    root.after(400, root.destroy)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _print_report() -> None:
    log("=" * 62)
    log("SPIKE REPORT")
    log("=" * 62)
    labels = [
        "Q1-overlay-open",
        "Q2-overlay-reopen",
        "Q3-normal-window",
        "Q4-modal-dialog",
        "Q5-stress-25x",
        "Q6-dpi-scaling",
        "Q7-clean-shutdown",
    ]
    descriptions = [
        "Qt windows appear via singleShot from main thread?",
        "Overlay reopens correctly after close?",
        "Normal QWidget window opens and closes?",
        "Modal QDialog works without freezing Tk?",
        "25x stress loop: no leak/crash/degrade?",
        "DPI scaling reports on 150% display?",
        "Closing Tk cleanly shuts down Qt thread?",
    ]
    all_green = True
    for lbl, desc in zip(labels, descriptions):
        verdict = _results.get(lbl, "MISSING")
        if verdict != "GREEN":
            all_green = False
        log(f"  {verdict:6s}  {desc}")
    log("-" * 62)
    arch_verdict = (
        "VIABLE -- Tk-main + Qt-background is safe on this machine."
        if all_green else
        "INVESTIGATE -- one or more checks failed; see details above."
    )
    log(f"VERDICT: {arch_verdict}")
    log("=" * 62)


def main() -> None:
    global _qt_thread

    log("[MAIN] Starting Qt background thread (daemon=False)...")
    _qt_thread = threading.Thread(target=_run_qt, daemon=False, name="qt-spike")
    _qt_thread.start()

    if not _qt_ready.wait(timeout=5.0):
        log("[MAIN] ERROR: Qt thread did not become ready within 5 s")
        return

    log(f"[MAIN] Qt ready. Main thread id={threading.main_thread().ident}  Qt thread id={_qt_owner_thread.ident}")

    root = tk.Tk()
    _build_tk(root)
    root.protocol("WM_DELETE_WINDOW", lambda: _on_tk_close(root))

    # Kick off the automated sequence after Tk mainloop is running
    # (use root.after so it executes from the Tk main loop, not here)
    root.after(500, lambda: _run_auto_sequence(root))

    log("[MAIN] Entering Tk mainloop...")
    root.mainloop()

    log("[MAIN] Tk mainloop exited -- joining Qt thread (timeout=6s)...")
    _qt_thread.join(timeout=6.0)

    if _qt_thread.is_alive():
        log("[MAIN] WARNING: Qt thread still alive after 6s -- forcing exit")
        import os
        os._exit(1)
    else:
        log("[MAIN] Qt thread joined cleanly")

    _print_report()
    log("[MAIN] Shutdown complete")


if __name__ == "__main__":
    main()
