"""
PySide6 "Stress Test Wizard" window for Samsara.

Walks the user through samsara.stress_tests' scripted battery of dictation
trials, captures the samsara.diagnostics.DiagRecord produced by each attempt
(reusing samsara.diagnostics -- never duplicating signal capture), and
produces a plain-text findings report.

Close-and-destroy window lifecycle (same family as voice_training_qt.py,
NOT the persistent hide-don't-destroy family used by history_qt.py/
diagnostics_qt.py): closing the window resets wizard state so reopening it
always starts a fresh run from step 1.

CAPTURE MECHANISM (reworked -- see the original defect below): each step
arms samsara.diagnostics' one-shot completion hook
(add_one_shot_hook/remove_one_shot_hook) before showing its instructions.
The user performs the step with their REAL dictation hotkey; the very same
diagnostics.record() call that hotkey completion already makes (the
canonical tap point -- the same call sites that write history) fires the
hook with the resulting DiagRecord, which IS this step's result. No
clipboard/paste watching, no polling a Qt widget for stray text. The hook
fires on whatever thread dictation.py's transcription worker is on, so the
callback here immediately marshals back to the Qt thread via
qt_runtime.post() before touching any widget.

ORIGINAL DEFECT (why this rework exists): the previous version watched an
EDITABLE QTextEdit for whatever OS-level paste happened to land in it, on
the theory that a real hotkey dictation would paste its output there.  That
never actually confirms real dictation ran at all -- it's a passive side
channel dependent on OS focus/paste timing, indistinguishable from stale
clipboard content or an unrelated paste. It is what produced the reported
"pasted clipboard contents" / "fail: no text captured" behavior. The target
box below is now populated directly from the captured DiagRecord.text (or a
status message) after the hook fires, and CAN be a literal
setReadOnly(True) widget -- the old caveat about read-only blocking
programmatic paste no longer applies since nothing pastes into it anymore.

Every step is skippable and the wizard is cancellable at any point without
leaving the hook armed: _disarm_capture() runs at the top of _show_step
(before arming the next step), in closeEvent, and in _show_final_screen --
diagnostics.remove_one_shot_hook() is idempotent, so calling it defensively
in all of these is safe.
"""

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QFrame, QStackedWidget,
    QTableWidget, QTableWidgetItem, QHeaderView,
)

from samsara.ui import qt_runtime, theme
from samsara.ui.quick_reference_qt import _pretty_key_combo
from samsara import diagnostics
from samsara.stress_tests import build_battery, generate_report

from samsara.log import get_logger

logger = get_logger(__name__)

# accidental_tap/silent_hold: how long to wait with nothing captured before
# declaring a clean pass ("no output produced, as expected"). Short window --
# these steps are ABOUT the immediate/near-silent case.
_NO_OUTPUT_WINDOW_S = 5.0
_NO_OUTPUT_STEP_IDS = ('accidental_tap', 'silent_hold')

# All steps: outer safety net if NOTHING is captured at all (no DiagRecord of
# any kind) -- most likely cause is the user didn't hold the right hotkey.
# Never auto-fails; shows a message and leaves Retry/Skip available.
_CAPTURE_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------

class StressWizardQt:
    """Close-and-destroy window wrapper (voice_training_qt.py's pattern)."""

    def __init__(self, app):
        self.app = app
        self._window = None
        self._init_posted = False

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
            qt_runtime.post(self._window.activateWindow)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def close(self):
        if self._window is not None:
            qt_runtime.post(self._window.close)

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _StressWizardWindow(self.app)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()

    def _on_destroyed(self):
        self._window = None
        self._init_posted = False


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class _StressWizardWindow(QMainWindow):

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("Samsara Stress Test Wizard")
        self.resize(720, 620)
        self.setMinimumSize(560, 480)
        self.setStyleSheet(theme.build_stylesheet())

        self._battery = build_battery(getattr(app, 'voice_training_window', None))
        self._step_idx = 0
        self._results = []             # [{'step','passed','reason','verdicts'}]
        self._step_start_ts = None
        self._closed = False           # guards a hook callback racing closeEvent

        # Single-shot deadline timer, re-armed per step: either the short
        # _NO_OUTPUT_WINDOW_S ("did silence stay silent") or the long
        # _CAPTURE_TIMEOUT_S ("did ANYTHING get captured") -- see _show_step.
        self._capture_timer = QTimer(self)
        self._capture_timer.setSingleShot(True)
        self._timer_connected = False   # tracks whether .timeout is currently wired

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        self._stack = QStackedWidget()
        root.addWidget(self._stack, stretch=1)

        self._step_page = self._build_step_page()
        self._final_page = self._build_final_page()
        self._stack.addWidget(self._step_page)
        self._stack.addWidget(self._final_page)

        if self._battery:
            self._show_step(0)
        else:
            self._show_final_screen()

    # ------------------------------------------------------------------
    # Step page
    # ------------------------------------------------------------------

    def _build_step_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(14)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.FONT_SIZE_CAPTION}px;"
            f"font-weight:600;text-transform:uppercase;letter-spacing:0.04em;"
        )
        lay.addWidget(self._progress_label)

        self._instruction_label = QLabel("")
        self._instruction_label.setWordWrap(True)
        self._instruction_label.setStyleSheet(
            f"color:{theme.TEXT_PRIMARY};font-size:{theme.FONT_SIZE_HEADING}px;"
        )
        lay.addWidget(self._instruction_label)

        # Large quoted block -- FONT_SIZE_TITLE so it's legible from across
        # the room (accessibility). Hidden entirely for steps with no fixed
        # expected_text (e.g. accidental_tap, the dynamic jargon step).
        self._quote_frame = QFrame()
        theme.style_card(self._quote_frame)
        quote_lay = QVBoxLayout(self._quote_frame)
        quote_lay.setContentsMargins(20, 16, 20, 16)
        self._quote_label = QLabel("")
        self._quote_label.setWordWrap(True)
        self._quote_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._quote_label.setStyleSheet(
            f"color:{theme.ACCENT};font-size:{theme.FONT_SIZE_TITLE}px;font-weight:600;"
        )
        quote_lay.addWidget(self._quote_label)
        lay.addWidget(self._quote_frame)

        hint = QLabel(
            "This wizard listens for your NEXT real dictation -- perform the "
            "step above with your actual hotkey. Nothing needs focus; the "
            "box below fills in on its own once captured."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.FONT_SIZE_CAPTION}px;")
        lay.addWidget(hint)

        self._target_box = QTextEdit()
        self._target_box.setReadOnly(True)
        self._target_box.setPlaceholderText("Waiting for your dictation…")
        self._target_box.setFixedHeight(80)
        lay.addWidget(self._target_box)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.FONT_SIZE_BODY}px;"
        )
        lay.addWidget(self._status_label)

        self._result_panel = QFrame()
        theme.style_card(self._result_panel)
        result_lay = QVBoxLayout(self._result_panel)
        result_lay.setContentsMargins(16, 12, 16, 12)
        self._result_headline = QLabel("")
        self._result_headline.setWordWrap(True)
        self._result_headline.setStyleSheet(f"font-size:{theme.FONT_SIZE_BODY}px;font-weight:600;")
        result_lay.addWidget(self._result_headline)
        self._result_verdicts = QLabel("")
        self._result_verdicts.setWordWrap(True)
        self._result_verdicts.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.FONT_SIZE_CAPTION}px;"
        )
        result_lay.addWidget(self._result_verdicts)
        self._result_panel.setVisible(False)
        lay.addWidget(self._result_panel)

        lay.addStretch()

        nav = QHBoxLayout()
        nav.setSpacing(10)

        self._retry_btn = QPushButton("Retry")
        theme.make_secondary(self._retry_btn)
        self._retry_btn.setMinimumHeight(44)
        self._retry_btn.clicked.connect(self._on_retry)
        nav.addWidget(self._retry_btn)

        self._skip_btn = QPushButton("Skip")
        theme.make_secondary(self._skip_btn)
        self._skip_btn.setMinimumHeight(44)
        self._skip_btn.clicked.connect(self._on_skip)
        nav.addWidget(self._skip_btn)

        nav.addStretch()

        self._next_btn = QPushButton("Next")
        theme.make_primary(self._next_btn)
        self._next_btn.setMinimumHeight(44)
        self._next_btn.setMinimumWidth(120)
        self._next_btn.setEnabled(False)
        self._next_btn.clicked.connect(self._on_next)
        nav.addWidget(self._next_btn)

        lay.addLayout(nav)
        return page

    # ------------------------------------------------------------------
    # Final page
    # ------------------------------------------------------------------

    def _build_final_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)

        title = QLabel("Stress Test Results")
        title.setStyleSheet(
            f"color:{theme.TEXT_PRIMARY};font-size:{theme.FONT_SIZE_TITLE}px;font-weight:700;"
        )
        lay.addWidget(title)

        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.FONT_SIZE_BODY}px;"
        )
        lay.addWidget(self._summary_label)

        self._results_table = QTableWidget(0, 3)
        self._results_table.setHorizontalHeaderLabels(["Step", "Result", "Reason"])
        hh = self._results_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._results_table.verticalHeader().setVisible(False)
        self._results_table.verticalHeader().setDefaultSectionSize(28)
        self._results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self._results_table, stretch=1)

        verdicts_title = QLabel("Diagnostics verdicts observed:")
        verdicts_title.setStyleSheet(
            f"color:{theme.TEXT_PRIMARY};font-size:{theme.FONT_SIZE_BODY}px;font-weight:600;"
        )
        lay.addWidget(verdicts_title)
        self._verdicts_label = QLabel("")
        self._verdicts_label.setWordWrap(True)
        self._verdicts_label.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY};font-size:{theme.FONT_SIZE_CAPTION}px;"
        )
        lay.addWidget(self._verdicts_label)

        nav = QHBoxLayout()
        nav.addStretch()
        copy_btn = QPushButton("Copy Report")
        theme.make_secondary(copy_btn)
        copy_btn.setMinimumHeight(44)
        copy_btn.clicked.connect(self._on_copy_report)
        nav.addWidget(copy_btn)
        close_btn = QPushButton("Close")
        theme.make_primary(close_btn)
        close_btn.setMinimumHeight(44)
        close_btn.clicked.connect(self.close)
        nav.addWidget(close_btn)
        lay.addLayout(nav)

        return page

    # ------------------------------------------------------------------
    # Step flow
    # ------------------------------------------------------------------

    def _show_step(self, idx: int):
        # Disarm FIRST, unconditionally -- covers the "advance past the last
        # step into the final screen" transition below too, so no path out
        # of a step can leave the previous step's hook armed.
        self._disarm_capture()

        if idx >= len(self._battery):
            self._show_final_screen()
            return

        self._step_idx = idx
        step = self._battery[idx]

        self._target_box.clear()
        self._result_panel.setVisible(False)
        self._next_btn.setEnabled(False)

        self._progress_label.setText(
            f"Step {idx + 1} of {len(self._battery)} — {step.category}"
        )
        # Live config, never hardcoded -- reopening after a hotkey change
        # (or even mid-wizard via Settings) reflects the new value on the
        # very next step shown.
        hotkey_display = _pretty_key_combo(
            (getattr(self.app, 'config', None) or {}).get('hotkey', 'ctrl+shift')
        )
        instruction_text = step.instruction.format(hotkey=hotkey_display)
        self._instruction_label.setText(f"{step.title}: {instruction_text}")

        if step.expected_text:
            self._quote_label.setText(f'"{step.expected_text}"')
            self._quote_frame.setVisible(True)
        else:
            self._quote_frame.setVisible(False)

        self._status_label.setText("Waiting for you to dictate…")
        self._arm_capture()

        if step.id in _NO_OUTPUT_STEP_IDS:
            self._capture_timer.timeout.connect(self._on_no_output_window_elapsed)
            self._timer_connected = True
            self._capture_timer.start(int(_NO_OUTPUT_WINDOW_S * 1000))
        else:
            self._capture_timer.timeout.connect(self._on_capture_timeout)
            self._timer_connected = True
            self._capture_timer.start(int(_CAPTURE_TIMEOUT_S * 1000))

    # ------------------------------------------------------------------
    # Capture hook -- samsara.diagnostics' one-shot completion hook, the
    # canonical tap point (see module docstring). _on_diag_record fires on
    # the dictation worker thread; it does nothing but marshal to the Qt
    # thread, where all the actual logic (including thread-affinity-
    # sensitive widget access) lives in _handle_captured.
    # ------------------------------------------------------------------

    def _arm_capture(self):
        diagnostics.add_one_shot_hook(self._on_diag_record)

    def _disarm_capture(self):
        """Idempotent -- safe to call from _show_step, closeEvent, and
        _show_final_screen without checking whether a hook is armed.

        Qt's disconnect() with nothing connected doesn't raise -- it just
        emits a noisy RuntimeWarning ("Failed to disconnect... signal
        timeout()") -- so a plain try/except doesn't suppress it. Tracking
        the connection explicitly avoids ever calling disconnect() with
        nothing to disconnect.
        """
        self._capture_timer.stop()
        if self._timer_connected:
            self._capture_timer.timeout.disconnect()
            self._timer_connected = False
        diagnostics.remove_one_shot_hook(self._on_diag_record)

    def _on_diag_record(self, rec):
        qt_runtime.post(lambda: self._handle_captured(rec))

    def _handle_captured(self, rec):
        if self._closed or self._step_idx >= len(self._battery):
            return  # window closed / wizard advanced past this step already
        if rec.mode != "hotkey":
            # Unrelated activity (a wake-word session, command mode, etc.
            # firing coincidentally) -- not this step's result. Keep
            # listening for the real one; re-registering is required since
            # add_one_shot_hook() already consumed itself for THIS delivery.
            diagnostics.add_one_shot_hook(self._on_diag_record)
            return
        self._finish_step(rec)

    def _on_no_output_window_elapsed(self):
        """accidental_tap/silent_hold: nothing captured within the short
        window -- a clean pass (see _pc_no_output)."""
        self._finish_step(None)

    def _on_capture_timeout(self):
        """All steps: NOTHING captured within _CAPTURE_TIMEOUT_S -- most
        likely the user held the wrong key. Never a false FAIL: leaves
        Retry/Skip available (already always enabled) and keeps the hook
        armed in case the real dictation is just running late."""
        hotkey_display = _pretty_key_combo(
            (getattr(self.app, 'config', None) or {}).get('hotkey', 'ctrl+shift')
        )
        self._status_label.setText(
            f"No dictation detected — did you hold {hotkey_display}? "
            "Try again, or Skip this step."
        )

    def _finish_step(self, rec):
        self._disarm_capture()
        step = self._battery[self._step_idx]
        got_text = rec.text if rec is not None else None
        if got_text is not None:
            self._target_box.setPlainText(got_text or "(no speech detected)")
        try:
            passed, reason = step.pass_criteria(rec, got_text or None)
        except Exception as exc:
            logger.debug(f"[STRESS] pass_criteria failed for {step.id}: {exc}")
            passed, reason = False, f"Internal error evaluating result: {exc}"
        verdicts = list(rec.verdicts) if rec is not None else []
        self._results.append({
            'step': step, 'passed': passed, 'reason': reason, 'verdicts': verdicts,
        })
        self._render_result(passed, reason, verdicts)
        self._next_btn.setEnabled(True)
        self._status_label.setText("Result ready — Retry, Skip, or Next.")

    def _render_result(self, passed: bool, reason: str, verdicts: list):
        color = theme.SUCCESS if passed else theme.ERROR
        headline = "PASS" if passed else "FAIL"
        self._result_headline.setText(f"{headline}: {reason}")
        self._result_headline.setStyleSheet(
            f"color:{color};font-size:{theme.FONT_SIZE_BODY}px;font-weight:600;"
        )
        self._result_verdicts.setText(
            "Diagnostics: " + ", ".join(verdicts) if verdicts else "No diagnostics record captured."
        )
        self._result_panel.setVisible(True)

    def _on_retry(self):
        step = self._battery[self._step_idx]
        if self._results and self._results[-1]['step'] is step:
            self._results.pop()
        self._show_step(self._step_idx)

    def _on_skip(self):
        self._disarm_capture()
        step = self._battery[self._step_idx]
        if self._results and self._results[-1]['step'] is step:
            self._results.pop()
        self._results.append(
            {'step': step, 'passed': None, 'reason': 'Skipped', 'verdicts': []}
        )
        self._show_step(self._step_idx + 1)

    def _on_next(self):
        self._show_step(self._step_idx + 1)

    # ------------------------------------------------------------------
    # Final screen
    # ------------------------------------------------------------------

    def _show_final_screen(self):
        self._disarm_capture()
        passed = sum(1 for r in self._results if r['passed'] is True)
        failed = sum(1 for r in self._results if r['passed'] is False)
        skipped = sum(1 for r in self._results if r['passed'] is None)
        self._summary_label.setText(
            f"{passed} passed, {failed} failed, {skipped} skipped "
            f"(of {len(self._results)})"
        )

        self._results_table.setRowCount(0)
        for r in self._results:
            row = self._results_table.rowCount()
            self._results_table.insertRow(row)
            self._results_table.setItem(row, 0, QTableWidgetItem(r['step'].title))

            status = "SKIP" if r['passed'] is None else ("PASS" if r['passed'] else "FAIL")
            status_item = QTableWidgetItem(status)
            if r['passed'] is None:
                color = QColor(theme.TEXT_SECONDARY)
            elif r['passed']:
                color = QColor(theme.SUCCESS)
            else:
                color = QColor(theme.ERROR)
            status_item.setForeground(QBrush(color))
            self._results_table.setItem(row, 1, status_item)

            self._results_table.setItem(row, 2, QTableWidgetItem(r['reason']))

        all_verdicts = []
        seen = set()
        for r in self._results:
            for v in r['verdicts']:
                if v == "OK" or v in seen:
                    continue
                seen.add(v)
                all_verdicts.append(v)
        self._verdicts_label.setText(
            "\n".join(f"- {v}" for v in all_verdicts) if all_verdicts else "None."
        )

        self._stack.setCurrentWidget(self._final_page)

    def _on_copy_report(self):
        report = generate_report(self._results)
        QApplication.clipboard().setText(report)
        self._summary_label.setText(self._summary_label.text() + "  (report copied)")

    # ------------------------------------------------------------------
    def closeEvent(self, e):
        self._closed = True
        self._disarm_capture()
        e.accept()
