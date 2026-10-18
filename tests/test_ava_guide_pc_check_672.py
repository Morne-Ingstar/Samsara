"""672: one optional PC-check control, with its own explanation and result."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox
from PySide6.QtTest import QTest

from samsara import hardware_check as hw
from samsara.ui import ava_guide_qt as ag, theme


@pytest.fixture(params=[('full', 'dark'), ('compact', 'dark'),
                        ('full', 'light'), ('compact', 'light')])
def window(request, qapp, monkeypatch, tmp_path):
    mode, palette = request.param
    previous = theme.active_theme()
    theme.set_theme(palette, refresh=False)
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    monkeypatch.setattr(ag, '_ping_ollama', lambda **kw: (False, []))
    monkeypatch.setattr(ag.thread_registry, 'spawn', lambda _name, fn, **kw: fn())
    probe = Mock(return_value=hw.Hardware(
        (hw.GPU('NVIDIA Test', 12 * hw.GIB),), 32 * hw.GIB, 'Test CPU', 6, True))
    monkeypatch.setattr(hw, 'check_hardware', probe)
    w = ag._WizardWindow(SimpleNamespace(config={'ui': {'listening_display': mode}}))
    w._go_to(w._STEP_CHECK)
    w.show()
    qapp.processEvents()
    yield w, probe
    w.close()
    w.deleteLater()
    qapp.processEvents()
    theme.set_theme(previous, refresh=False)


def test_check_is_one_group_and_does_not_read_until_pressed(window, qapp):
    w, probe = window
    group = w._pc_check
    assert group.accessibleName() == 'PC check'
    for child in (w._check_explanation, w._check_btn, w._hardware_status):
        assert child.parentWidget() is group
    assert w._check_explanation.text() == ag.COPY['permission']
    assert w._check_explanation.property('role') == 'description'
    assert not w._pages[w._STEP_CHECK].findChildren(QCheckBox)
    assert w._check_btn.text() == 'Run the PC check'
    assert w._check_btn.isEnabled()
    assert w._hardware_status.isHidden()
    assert w._next_btn.isEnabled()
    probe.assert_not_called()
    w._check_btn.click()
    qapp.processEvents()
    probe.assert_called_once_with(consent=True)
    assert w._current_step == w._STEP_CHECK
    assert w._gpu.text() == 'NVIDIA Test'
    assert w._vram.value() == 12 and w._ram.value() == 32
    assert w._cpu.text() == 'Test CPU' and w._cores.value() == 6
    assert w._hardware_status.isVisible()
    for fact in ('NVIDIA Test', '12 GB graphics memory', '32 GB system memory', 'Test CPU', '6 cores'):
        assert fact in w._hardware_status.text()
    assert w._check_btn.text() == w._check_btn.accessibleName() == 'Run again'
    assert w._app.config == {'ui': {'listening_display': w._app.config['ui']['listening_display']}}
    w._check_btn.setFocus()
    QTest.keyClick(w._check_btn, Qt.Key.Key_Space)
    assert probe.call_count == 2


def test_check_stays_grouped_during_work_and_failure_can_retry(window, monkeypatch):
    w, probe = window
    jobs = []
    monkeypatch.setattr(ag.thread_registry, 'spawn', lambda _name, fn, **kw: jobs.append(fn))
    w._gpu.setText('My entry')
    probe.side_effect = OSError('private diagnostic')
    w._check_btn.click()
    assert w._hardware_status.text() == 'Checking this PC…'
    assert not w._check_btn.isEnabled()
    w._check_pc()
    assert len(jobs) == 1
    jobs.pop()()
    assert w._hardware_status.text() == ag.COPY['check_failed']
    assert w._hardware_status.isVisible() and w._check_btn.isEnabled()
    assert w._gpu.text() == 'My entry' and w._next_btn.isEnabled()
    probe.side_effect = None
    w._check_btn.click()
    jobs.pop()()
    assert w._check_btn.text() == 'Run again'
    w._ram.setValue(24)
    assert w._facts is None
    assert w._hardware_status.isHidden()


def test_check_handles_missing_graphics_and_processor(window):
    w, probe = window
    probe.return_value = hw.Hardware((), 8 * hw.GIB, '', 4, None)
    w._check_btn.click()
    assert 'Graphics card not found' in w._hardware_status.text()
    assert 'Processor not identified' in w._hardware_status.text()
    assert w._gpu.text() == '' and w._vram.value() == 0
    assert w._ram.value() == 8
