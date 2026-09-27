"""The shared wizard progress bar and its four adopted call sites."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible

from samsara.ui import theme
from samsara.ui.widgets.step_indicator import StepIndicator, _upcoming_ink


ROOT = Path(__file__).resolve().parents[1]
STEPS = ["Device", "Level", "Wake word", "Done"]
CALL_SITES = (
    "mic_setup_wizard_qt.py",
    "first_run_wizard_qt.py",
    "ava_guide_qt.py",
    "tutorial_qt.py",
)


def test_states_and_backtracking(qapp):
    indicator = StepIndicator(STEPS)
    indicator.set_done(0)
    indicator.set_current(1)
    assert [indicator.state(index) for index in range(4)] == [
        "done", "current", "upcoming", "upcoming"
    ]
    indicator.set_done(1)
    assert indicator.state(1) == "done"
    indicator.set_current(1)
    assert indicator.state(1) == "current"
    indicator.set_progress(2, skipped={1})
    assert [indicator.state(index) for index in range(4)] == [
        "done", "skipped", "current", "upcoming"
    ]
    indicator.set_progress(1, skipped={1})
    assert indicator.state(1) == "current"
    assert indicator.state(2) == "upcoming"
    indicator.set_progress(2)
    assert indicator.state(1) == "done"
    with pytest.raises(IndexError):
        indicator.set_current(4)


def test_accessible_list_names_and_hit_height(qapp):
    indicator = StepIndicator(STEPS)
    indicator.set_progress(2, skipped={1})
    interface = QAccessible.queryAccessibleInterface(indicator)
    assert interface.role() == QAccessible.Role.List
    assert interface.childCount() == len(STEPS)
    assert indicator.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert [interface.child(index).text(QAccessible.Text.Name) for index in range(4)] == [
        "Step 1 of 4, Device, done",
        "Step 2 of 4, Level, skipped",
        "Step 3 of 4, Wake word, current",
        "Step 4 of 4, Done, upcoming",
    ]
    assert all(interface.child(index).role() == QAccessible.Role.ListItem for index in range(4))
    assert all(indicator.item(index).flags() == Qt.ItemFlag.NoItemFlags for index in range(4))
    assert indicator.height() >= theme.HIT_TARGET_MIN
    indicator.set_ui_scale(1.5)
    assert indicator.height() >= round(theme.HIT_TARGET_MIN * 1.5)


def test_upcoming_ink_meets_aa_on_both_surfaces(qapp):
    previous = theme.active_theme()
    try:
        for palette in ("dark", "light"):
            theme.set_theme(palette, refresh=False)
            ink = _upcoming_ink().name()
            assert theme.contrast_ratio(ink, theme.BG1) >= 4.5
            assert theme.contrast_ratio(ink, theme.BG2) >= 4.5
    finally:
        theme.set_theme(previous, refresh=False)


def test_no_literal_pixel_sizes_outside_theme_tokens():
    path = ROOT / "samsara/ui/widgets/step_indicator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    dimension_methods = {
        "setFixedHeight", "setFixedWidth", "setMinimumHeight", "setMinimumWidth",
        "setMaximumHeight", "setMaximumWidth", "setSpacing", "setContentsMargins",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not re.search(r"\b\d+(?:\.\d+)?(?:px|pt)\b", node.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in dimension_methods:
                assert all(
                    not (isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)))
                    for arg in node.args
                )


def test_only_four_step_bars_adopt_widget():
    for filename in CALL_SITES:
        source = (ROOT / "samsara/ui" / filename).read_text(encoding="utf-8")
        assert "from samsara.ui.widgets.step_indicator import StepIndicator" in source
        assert "self._step_indicator = StepIndicator(" in source
        assert "self._step_indicator.set_progress(" in source
        assert "self._dots" not in source
    mic = (ROOT / "samsara/ui/mic_setup_wizard_qt.py").read_text(encoding="utf-8")
    ava = (ROOT / "samsara/ui/ava_guide_qt.py").read_text(encoding="utf-8")
    assert 'dot = QLabel("*")' in mic
    assert 'self._ollama_dot = QLabel("●")' in ava
    assert 'self._model_status_dot = QLabel("●")' in ava
