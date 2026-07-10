"""Tests for samsara.ui.stress_wizard_qt: the reworked capture mechanism.

Headless-safe: qt_runtime.post() is monkeypatched to run its callback
immediately in-process (matching test_main_window_qt.py's established
precedent) rather than exercising the real background-thread event loop.
Widgets are constructed directly against the session-scoped `qapp` fixture.

Every test clears samsara.diagnostics' ring buffer AND one-shot hook list
before/after itself -- both are module-level globals, and a leaked hook
from one test's window would fire against a LATER test's diagnostics.record()
call otherwise (touching a closed window from a prior test).
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samsara.diagnostics as diagnostics
from samsara.ui import stress_wizard_qt as sw


def _make_app(hotkey="ctrl+shift", voice_training_window=None):
    return types.SimpleNamespace(
        config={"hotkey": hotkey}, voice_training_window=voice_training_window,
    )


@pytest.fixture(autouse=True)
def _clear_diagnostics_state():
    diagnostics.clear()
    diagnostics._one_shot_hooks.clear()
    yield
    diagnostics._one_shot_hooks.clear()
    diagnostics.clear()


@pytest.fixture
def immediate_post(monkeypatch):
    """Run qt_runtime.post()'s callback immediately instead of marshaling
    to a real background Qt thread -- matches test_main_window_qt.py's
    precedent for qt_runtime-adjacent tests."""
    monkeypatch.setattr(sw.qt_runtime, "post", lambda cb: cb())


def _hotkey_rec(text, **overrides):
    kwargs = dict(
        mode="hotkey", audio_s=1.0, model_name="base", device="cpu",
        compute_type="int8", text=text,
    )
    kwargs.update(overrides)
    return diagnostics.DiagRecord(**kwargs)


# ============================================================================
# Construction + live-config instruction text
# ============================================================================

class TestConstruction:
    def test_constructs_without_error(self, qapp):
        win = sw._StressWizardWindow(_make_app())
        try:
            assert win.windowTitle() == "Samsara Stress Test Wizard"
            assert len(win._battery) == 9  # no voice_training_window -> no jargon step
        finally:
            win.close()

    def test_first_step_instruction_uses_live_hotkey(self, qapp):
        win = sw._StressWizardWindow(_make_app(hotkey="ctrl+alt+q"))
        try:
            assert "Ctrl+Alt+Q" in win._instruction_label.text()
            assert "{hotkey}" not in win._instruction_label.text()
        finally:
            win.close()

    def test_instruction_reflects_hotkey_change_on_next_step_shown(self, qapp):
        app = _make_app(hotkey="ctrl+shift")
        win = sw._StressWizardWindow(app)
        try:
            assert "Ctrl+Shift" in win._instruction_label.text()
            app.config["hotkey"] = "ctrl+alt+z"
            win._show_step(win._step_idx)  # simulate Retry re-showing the step
            assert "Ctrl+Alt+Z" in win._instruction_label.text()
        finally:
            win.close()


# ============================================================================
# Capture hook -- armed on step start, resolves on a real hotkey completion
# ============================================================================

class TestCaptureHook:
    def test_hook_armed_on_first_step(self, qapp):
        win = sw._StressWizardWindow(_make_app())
        try:
            assert len(diagnostics._one_shot_hooks) == 1
        finally:
            win.close()

    def test_hotkey_record_resolves_the_step(self, qapp, immediate_post):
        win = sw._StressWizardWindow(_make_app())  # step 0 = short_word, expects "testing"
        try:
            diagnostics.record(_hotkey_rec("testing"))
            assert win._next_btn.isEnabled() is True
            assert win._results[-1]["passed"] is True
            assert win._target_box.toPlainText() == "testing"
        finally:
            win.close()

    def test_hotkey_record_wrong_word_fails_the_step(self, qapp, immediate_post):
        win = sw._StressWizardWindow(_make_app())
        try:
            diagnostics.record(_hotkey_rec("resting"))
            assert win._results[-1]["passed"] is False
        finally:
            win.close()

    def test_unrelated_mode_record_is_ignored_and_rearms(self, qapp, immediate_post):
        """A wake-word/command-mode recording firing coincidentally must
        NOT be treated as this step's result -- the hook re-registers
        itself to keep waiting for the real hotkey completion."""
        win = sw._StressWizardWindow(_make_app())
        try:
            diagnostics.record(diagnostics.DiagRecord(
                mode="wake", audio_s=1.0, model_name="base", device="cpu",
                compute_type="int8", text="jarvis something",
            ))
            assert win._results == []
            assert win._next_btn.isEnabled() is False
            assert len(diagnostics._one_shot_hooks) == 1  # still listening

            diagnostics.record(_hotkey_rec("testing"))
            assert win._results[-1]["passed"] is True
        finally:
            win.close()

    def test_gated_outcome_on_silent_hold_step_is_a_pass(self, qapp, immediate_post):
        win = sw._StressWizardWindow(_make_app())
        try:
            win._show_step(3)  # silent_hold (index matches build_battery order)
            assert win._battery[win._step_idx].id == "silent_hold"

            diagnostics.record(diagnostics.DiagRecord(
                mode="hotkey", audio_s=0.3, model_name="base", device="cpu",
                compute_type="int8", text="", outcome="gated",
            ))
            assert win._results[-1]["passed"] is True
            assert "gate" in win._results[-1]["reason"].lower()
        finally:
            win.close()


# ============================================================================
# No-leaked-callback discipline -- skip / retry / close
# ============================================================================

class TestUnhookDiscipline:
    def test_skip_disarms_current_and_arms_next_exactly_once(self, qapp):
        win = sw._StressWizardWindow(_make_app())
        try:
            first_hook = diagnostics._one_shot_hooks[0]
            win._on_skip()
            assert len(diagnostics._one_shot_hooks) == 1
            # still the SAME bound method (same window instance) -- just
            # re-registered for the new step, not a second/duplicate hook.
            assert diagnostics._one_shot_hooks[0] == first_hook
        finally:
            win.close()

    def test_retry_disarms_and_rearms_same_step(self, qapp):
        win = sw._StressWizardWindow(_make_app())
        try:
            step_before = win._step_idx
            win._on_retry()
            assert win._step_idx == step_before
            assert len(diagnostics._one_shot_hooks) == 1
        finally:
            win.close()

    def test_close_unhooks_completely_no_leaked_callback(self, qapp):
        win = sw._StressWizardWindow(_make_app())
        assert len(diagnostics._one_shot_hooks) == 1

        win.close()

        assert diagnostics._one_shot_hooks == []

    def test_closed_window_ignores_late_hook_fire(self, qapp, immediate_post):
        """Defensive guard: even if a record() call were already in flight
        when close() ran, the callback must no-op rather than touch a
        closed window's widgets."""
        win = sw._StressWizardWindow(_make_app())
        callback = diagnostics._one_shot_hooks[0]
        win.close()

        rec = _hotkey_rec("testing")
        callback(rec)  # simulate a hook firing that slipped past remove_one_shot_hook

    def test_skipping_every_step_never_leaves_a_hook_armed_and_reaches_final_screen(self, qapp):
        win = sw._StressWizardWindow(_make_app())
        try:
            for _ in range(len(win._battery)):
                win._on_skip()
            assert diagnostics._one_shot_hooks == []
            assert win._stack.currentWidget() is win._final_page
            assert all(r["passed"] is None for r in win._results)
        finally:
            win.close()


# ============================================================================
# Timeout paths -- never a false FAIL
# ============================================================================

class TestTimeouts:
    def test_no_output_window_elapsed_is_a_clean_pass(self, qapp):
        win = sw._StressWizardWindow(_make_app())
        try:
            win._show_step(3)  # silent_hold
            win._on_no_output_window_elapsed()
            assert win._results[-1]["passed"] is True
            assert "as expected" in win._results[-1]["reason"]
        finally:
            win.close()

    def test_capture_timeout_does_not_record_a_result_or_disable_next(self, qapp):
        win = sw._StressWizardWindow(_make_app(hotkey="ctrl+shift"))
        try:
            win._on_capture_timeout()
            assert win._results == []
            assert win._next_btn.isEnabled() is False
            assert "Ctrl+Shift" in win._status_label.text()
            assert "no dictation detected" in win._status_label.text().lower()
        finally:
            win.close()

    def test_capture_timeout_leaves_hook_armed_for_a_late_arrival(self, qapp, immediate_post):
        win = sw._StressWizardWindow(_make_app())
        try:
            win._on_capture_timeout()
            assert len(diagnostics._one_shot_hooks) == 1

            diagnostics.record(_hotkey_rec("testing"))
            assert win._results[-1]["passed"] is True
        finally:
            win.close()
