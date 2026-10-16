"""Queue 418: effective Qt names/roles, choice actions and microphone values.

Synthetic app only. No desktop bootstrap, device capture or provider requests.
"""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QSlider


@pytest.fixture
def guides(qapp, monkeypatch, tmp_path):
    from samsara.runtime import thread_registry
    from samsara.ui import ava_guide_qt as ava, first_run_wizard_qt as first
    from samsara.ui import mic_setup_wizard_qt as mic, stress_wizard_qt as stress
    from samsara.ui import wake_word_debug_qt as wake

    monkeypatch.setattr(thread_registry, "spawn", lambda *a, **kw: None)
    monkeypatch.setattr(first._WizardWindow, "_start_meter", lambda self: None)
    monkeypatch.setattr(mic._WizardWindow, "_ensure_audio_running", lambda self: None)
    monkeypatch.setattr(stress._StressWizardWindow, "_arm_capture", lambda self: None)
    app = SimpleNamespace(config={}, voice_training_window=None,
        get_available_microphones=lambda: [{"id": 1, "name": "Synthetic microphone"}])
    windows = {
        "first": first._WizardWindow(tmp_path / "config.json"),
        "ava": ava._WizardWindow(app),
        "mic": mic._WizardWindow(app),
        "stress": stress._StressWizardWindow(app),
        "wake": wake._DebugWindow(app),
    }
    try:
        yield windows
    finally:
        for window in windows.values():
            window.close()
            window.deleteLater()
        qapp.processEvents()


ROWS = [
    ("F035", "first", "choice:0", "RadioButton", "Tiny"),
    ("F036", "first", "choice:1", "RadioButton", "Base"),
    ("F037", "first", "choice:2", "RadioButton", "Small"),
    ("F039", "ava", "_key_combo", "Button", "Activation key"),
    ("F040", "ava", "_model_combo", "ComboBox", "Model:"),
    ("F060", "mic", "_device_combo", "ComboBox", "Microphone:"),
    ("F061", "stress", "_results_table", "Table", "Stress Test Results"),
    ("F069", "wake", "slider:0", "Slider", "Speech threshold:"),
    ("F070", "wake", "_thresh_spin", "SpinBox", "Speech threshold:"),
    ("F071", "wake", "slider:1", "Slider", "Min speech (s):"),
    ("F072", "wake", "_min_spin", "SpinBox", "Min speech (s):"),
    ("F073", "wake", "slider:2", "Slider", "dictate:"),
    ("F074", "wake", "_dictate_spin", "SpinBox", "dictate:"),
    ("F075", "wake", "slider:3", "Slider", "short dictate:"),
    ("F076", "wake", "_short_spin", "SpinBox", "short dictate:"),
    ("F077", "wake", "slider:4", "Slider", "long dictate:"),
    ("F078", "wake", "_long_spin", "SpinBox", "long dictate:"),
    ("F079", "wake", "_mode_combo", "ComboBox", "Force mode:"),
    ("F097", "first", "_meter", "ProgressBar", "Input Level"),
    ("F108", "mic", "_device_level_bar._bar", "ProgressBar", "Signal:"),
    ("F109", "mic", "_level_bar._bar", "ProgressBar", "Check your speaking level"),
]


def _control(window, selector):
    if selector.startswith("choice:"):
        buttons = window._model_group.buttons()  # 373: removed use-case group.
        return buttons[int(selector.split(":")[1])]
    if selector.startswith("slider:"):
        sliders = [slider for slider in window.findChildren(QSlider) if slider.isEnabled()]
        return sliders[int(selector.split(":")[1])]
    widget = window
    for attr in selector.split("."):
        widget = getattr(widget, attr)
    return widget


@pytest.mark.parametrize("row,owner,selector,role,name", ROWS, ids=[row[0] for row in ROWS])
def test_every_fixed_row_has_effective_name_and_native_role(guides, row, owner, selector, role, name):
    widget = _control(guides[owner], selector)
    iface = QAccessible.queryAccessibleInterface(widget)
    assert iface is not None, row
    assert iface.text(QAccessible.Text.Name) == name, row
    assert name and name != type(widget).__name__
    assert iface.role() == getattr(QAccessible.Role, role), row
    # The name must share its source with the existing label, heading or placeholder.
    visible_words = [label.text() for label in guides[owner].findChildren(QLabel)]
    if owner == "first":
        # First-run pages are not parented into the window until visited.
        for page in guides[owner]._pages:
            visible_words.extend(label.text() for label in page.findChildren(QLabel))
    if hasattr(widget, "placeholderText"):
        visible_words.append(widget.placeholderText())
    if owner == "mic":
        visible_words.extend(guides[owner]._STEP_TITLES)
    assert name in visible_words, row


@pytest.mark.parametrize("page,selector", [(3, "_model_group")])
def test_choice_group_tab_arrows_activation_and_checked_state(guides, qapp, page, selector):
    window = guides["first"]
    window._step = page
    window._show_step()
    window.show()
    window.activateWindow()
    qapp.processEvents()
    group = getattr(window, selector)
    buttons = group.buttons()
    seen = set()
    window._back_btn.setFocus()
    for _ in range(30):
        focused = qapp.focusWidget()
        if focused in buttons:
            seen.add(focused)
        QTest.keyClick(focused or window, Qt.Key.Key_Tab)
    assert seen, "Tab must enter the choice group"
    # Native radio arrow navigation reaches each choice, including unchecked ones.
    buttons[0].setFocus()
    reached = {buttons[0]}
    for _ in range(len(buttons)):
        QTest.keyClick(qapp.focusWidget(), Qt.Key.Key_Down)
        reached.add(qapp.focusWidget())
    assert set(buttons) <= reached
    for button in buttons:
        calls = []
        button.clicked.connect(lambda checked, target=button: calls.append((target, checked)))
        # 373: Enter advances; tested by the walkthrough gate.
        for activation in ("click", Qt.Key.Key_Space):
            other = next(candidate for candidate in buttons if candidate is not button)
            other.setChecked(True)
            button.setFocus()
            calls.clear()
            if activation == "click":
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            else:
                QTest.keyClick(button, activation)
            assert calls == [(button, True)]
            assert group.checkedButton() is button
            for candidate in buttons:
                state = QAccessible.queryAccessibleInterface(candidate).state()
                assert state.checkable
                assert bool(state.checked) == (candidate is button)


def test_meter_value_range_and_reset_match_painted_level(guides):
    meter = guides["first"]._meter
    iface = QAccessible.queryAccessibleInterface(meter)
    value = iface.valueInterface()
    assert (value.minimumValue(), value.maximumValue(), value.currentValue()) == (0, 100, 0)
    meter.set_rms(0.02)
    assert value.currentValue() == round(meter._level * 100) > 0
    meter.reset()
    assert value.currentValue() == 0


@pytest.mark.parametrize("owner,attr", [("mic", "_device_level_bar"), ("mic", "_level_bar")])
def test_mic_guide_level_value_tracks_updates(guides, owner, attr):
    level = getattr(guides[owner], attr)
    level.set_level(0.42)
    value = QAccessible.queryAccessibleInterface(level._bar).valueInterface()
    assert (value.minimumValue(), value.maximumValue(), value.currentValue()) == (0, 100, 42)


def test_meter_show_and_semantic_announcements_preserve_focus(guides, qapp, monkeypatch):
    window = guides["first"]
    window._step = 1  # 373: microphone follows Welcome.
    window._show_step()
    window.show()
    window.activateWindow()
    qapp.processEvents()
    window._back_btn.setFocus()
    focus = qapp.focusWidget()
    assert focus is window._back_btn
    meter = window._meter
    meter.hide()
    qapp.processEvents()
    meter.show()
    qapp.processEvents()
    assert qapp.focusWidget() is focus
    assert meter.focusPolicy() == Qt.FocusPolicy.NoFocus
    events = []
    monkeypatch.setattr(QAccessible, "updateAccessibility", lambda event: events.append(event.message()))
    window._update_mic_pass(0.02)
    window._update_mic_pass(0.02)
    assert events == [window._mic_status.text()]
    assert events == ["Microphone active"]
    assert meter.accessibleDescription() == window._mic_status.text()
    window._on_mic_result("Synthetic microphone unavailable", "#ffffff")
    window._on_mic_result("Synthetic microphone unavailable", "#ffffff")
    assert events == ["Microphone active", "Synthetic microphone unavailable"]
    assert meter.accessibleDescription() == window._mic_status.text()
    assert qapp.focusWidget() is focus
    window._stop_meter()
    window._update_mic_pass(0.02)
    assert events[-1] == "Microphone active"


def test_mic_guide_progress_surfaces_preserve_existing_focus(guides, qapp):
    window = guides["mic"]
    window.show()
    window.activateWindow()
    qapp.processEvents()
    for index, name in enumerate(("_device_level_bar", "_level_bar")):
        window._stack.setCurrentIndex(index)
        window._skip_btn.setFocus()
        qapp.processEvents()
        focus = qapp.focusWidget()
        assert focus is window._skip_btn
        level = getattr(window, name)
        level.hide()
        qapp.processEvents()
        level.show()
        qapp.processEvents()
        assert level.isVisible()
        assert qapp.focusWidget() is focus
        assert level._bar.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_microphone_open_failure_announces_existing_error_without_focus_change(guides, qapp, monkeypatch):
    import sounddevice
    from samsara.ui.first_run_wizard_qt import MIC_ACCESS_DENIED_MESSAGE

    window = guides["first"]
    window._step = 1  # 373: microphone follows Welcome.
    window._show_step()
    window.show()
    window.activateWindow()
    qapp.processEvents()
    window._back_btn.setFocus()
    focus = qapp.focusWidget()
    events = []
    monkeypatch.setattr(QAccessible, "updateAccessibility", lambda event: events.append(event.message()))
    def denied(**kwargs):
        raise PermissionError("Synthetic microphone permission denial")
    monkeypatch.setattr(sounddevice, "InputStream", denied)
    assert window._open_meter_stream(1) is None
    assert events == [MIC_ACCESS_DENIED_MESSAGE]
    assert window._mic_status.text() == MIC_ACCESS_DENIED_MESSAGE
    assert window._meter.accessibleDescription() == MIC_ACCESS_DENIED_MESSAGE
    assert qapp.focusWidget() is focus
