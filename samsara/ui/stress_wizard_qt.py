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

Design note on the "target box": the wizard does NOT capture audio itself --
the user dictates via their normal hotkey/wake flow, which pastes text into
whatever has focus. The target box below therefore CANNOT be a literal
Qt read-only widget (QLineEdit/QTextEdit.setReadOnly(True) blocks
programmatic paste too, not just user keystrokes -- it would silently
swallow the very dictation output this wizard needs to read back). It's an
ordinary editable QTextEdit instead, with on-screen copy telling the user
not to type into it manually -- "read-only" in intent, not in Qt's literal
sense.
"""

import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QFrame, QStackedWidget,
    QTableWidget, QTableWidgetItem, QHeaderView,
)

from samsara.ui import qt_runtime, theme
from samsara import diagnostics
from samsara.stress_tests import build_battery, generate_report

from samsara.log import get_logger

logger = get_logger(__name__)

_POLL_INTERVAL_MS = 500
_NO_OUTPUT_WINDOW_S = 5.0
_NO_OUTPUT_STEP_IDS = ('accidental_tap', 'silent_hold')


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
        self._no_output_deadline = None

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll)

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
            "Click the box below to focus it, then use your normal "
            "dictation hotkey. Don't type into it by hand."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.FONT_SIZE_CAPTION}px;")
        lay.addWidget(hint)

        self._target_box = QTextEdit()
        self._target_box.setPlaceholderText("Dictated text will appear here…")
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
        if idx >= len(self._battery):
            self._show_final_screen()
            return

        self._step_idx = idx
        step = self._battery[idx]

        self._poll_timer.stop()
        self._target_box.clear()
        self._result_panel.setVisible(False)
        self._next_btn.setEnabled(False)

        self._progress_label.setText(
            f"Step {idx + 1} of {len(self._battery)} — {step.category}"
        )
        self._instruction_label.setText(f"{step.title}: {step.instruction}")

        if step.expected_text:
            self._quote_label.setText(f'"{step.expected_text}"')
            self._quote_frame.setVisible(True)
        else:
            self._quote_frame.setVisible(False)

        self._step_start_ts = datetime.now().isoformat()
        self._no_output_deadline = (
            time.monotonic() + _NO_OUTPUT_WINDOW_S
            if step.id in _NO_OUTPUT_STEP_IDS else None
        )
        self._status_label.setText("Waiting for you to dictate…")
        self._poll_timer.start()

    def _poll(self):
        if self._step_idx >= len(self._battery):
            return

        got_text = self._target_box.toPlainText().strip()

        new_rec = None
        try:
            records = diagnostics.recent(200)
            if records and records[-1].ts > self._step_start_ts:
                new_rec = records[-1]
        except Exception as exc:
            logger.debug(f"[STRESS] diagnostics.recent() failed: {exc}")

        if self._no_output_deadline is not None:
            # accidental_tap/silent_hold: fail fast on any sign of output;
            # otherwise wait out the full window before declaring a pass.
            if got_text or (new_rec is not None and new_rec.text):
                self._finish_step(new_rec, got_text)
            elif time.monotonic() >= self._no_output_deadline:
                self._finish_step(new_rec, got_text)
            return

        if new_rec is not None:
            self._finish_step(new_rec, got_text)

    def _finish_step(self, rec, got_text: str):
        self._poll_timer.stop()
        step = self._battery[self._step_idx]
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
        self._poll_timer.stop()
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
        self._poll_timer.stop()
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
        self._poll_timer.stop()
        e.accept()
