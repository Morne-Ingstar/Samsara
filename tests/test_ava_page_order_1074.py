"""1074: cloud setup is immediate; diagnostic rows are last; PC facts stay typeable on failure."""
import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit, QDoubleSpinBox, QSpinBox

from samsara import ai_preferences, hardware_check as hw
from samsara.ui import ava_guide_qt as guide, settings_qt
from samsara.ui.settings import ava_page
from tests.test_settings import _StubApp


@pytest.fixture
def windows(qapp, monkeypatch, qt_widget_scope):
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.autostart.sync_run_entry', lambda *a, **kw: None)
    monkeypatch.setattr(settings_qt.window_activation, 'confirm_foreground', lambda *a, **kw: None)
    created = []

    def make(route='cloud', provider='deepseek', app=None):
        if app is None:
            app = _StubApp()
            app.config = {'cloud_llm': {'provider': provider, 'api_key': '', 'model': ''},
                          'ava_edit': {'demo_pacing': 'cinematic'}}
            if route != 'off':
                consent = ai_preferences.accept_consent(
                    app.config, provider=ai_preferences.provider_for(app.config, route),
                    cloud=route == 'cloud', accepted_at='2026-11-03T00:00:00Z')
                app.config = ai_preferences.apply_accepted_ava(app.config, policy=route, consent=consent)
            else:
                app.config['ava'] = {'provider_policy': 'off'}
        win = settings_qt._SettingsWindow(app)
        created.append(win)
        win.show_tab('Ava')
        win.show()
        qapp.processEvents()
        return win

    yield make
    for win in created:
        with qt_widget_scope(win):
            pass


@pytest.mark.parametrize('route', ['off', 'local', 'cloud'])
def test_cloud_rows_are_main_only_in_cloud(windows, route):
    win = windows(route)
    page = win._pages['Ava']
    assert not page.advanced.toggle.isChecked()
    assert [row.title for row in page.base_rows[:5]] == [
        'Ava runs', 'Cloud provider', 'Cloud credential', 'Cloud model override', 'Local model']
    for key in ('cloud_api_key', 'cloud_model'):
        row = win._rows_by_widget[key]
        assert row in page.base_rows
        assert row.disclosure is None
        assert row.isVisibleTo(page) is (route == 'cloud')


def test_advanced_order_includes_group_before_log_and_consent(windows):
    win = windows()
    page = win._pages['Ava']
    assert [row[0] for row in ava_page.ADVANCED] == [
        'Keep model warm', 'Warm at startup', 'Web search', 'Cloud timeout',
        'Ava personality', 'Remembered turns', 'Editing timeout', 'Local editing model',
        'Automatic corrections', 'Correction model', 'Correct hold dictation',
        'Correct wake dictation', 'Correct streaming', 'Repair disfluencies',
        'Keep a conversation log', 'Ava consent']
    actual = [page.advanced.rows.itemAt(i).widget() for i in range(page.advanced.rows.count())]
    # Hairline separators are layout siblings, not preference rows.
    actual = [widget for widget in actual if hasattr(widget, 'title') or widget is page.smart_corrections]
    assert actual[-3:] == [page.smart_corrections,
        win._rows_by_widget['ava_conversation_log_enabled'], win._rows_by_widget['ava:Read this again']]


@pytest.mark.parametrize('provider', ['deepseek', 'openrouter'])
def test_apply_and_close_persists_cloud_fields_cancel_reopen_preserves_saved(windows, tmp_path, provider):
    win = windows(provider=provider)
    app = win.app
    saved = tmp_path / 'config.json'
    app.save_config = lambda: saved.write_text(json.dumps(app.config), encoding='utf-8')
    # Synthetic credential is never sent to a provider.
    win._widgets['cloud_api_key'].setText('synthetic-only')
    win._widgets['cloud_model'].setText('synthetic/model')
    win._apply_and_close()
    config = json.loads(saved.read_text(encoding='utf-8'))
    assert config['cloud_llm']['api_key'] == 'synthetic-only'
    assert config['cloud_llm']['model'] == 'synthetic/model'
    second = windows(app=app)
    second._widgets['cloud_api_key'].setText('unsaved')
    second._widgets['cloud_model'].setText('unsaved/model')
    second.close()  # Same close path as Cancel.
    third = windows(app=app)
    assert third._widgets['cloud_api_key'].text() == 'synthetic-only'
    assert third._widgets['cloud_model'].text() == 'synthetic/model'


def test_openrouter_picker_stays_with_main_model_row(windows, monkeypatch):
    win = windows(provider='openrouter')
    row = win._rows_by_widget['cloud_model']
    picker = win._widgets['openrouter_picker']
    button = win._widgets['openrouter_picker_button']
    assert row.isAncestorOf(picker) and row.isAncestorOf(button)
    assert button.width() < row.width() / 2
    calls = []
    monkeypatch.setattr(picker, 'load', lambda: calls.append(True))
    win._widgets['cloud_api_key'].setText('synthetic-only')
    button.click()
    assert picker.isVisibleTo(row)
    assert calls == [True]


def test_demo_pacing_loads_from_config_and_settings_cannot_change_it(windows, tmp_path):
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.config_defaults import DEFAULTS
    from samsara.ava_edit import pacing

    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'ava_edit': {'demo_pacing': 'cinematic'}}), encoding='utf-8')
    loaded = json.loads(path.read_text(encoding='utf-8'))
    assert pacing.resolve(SimpleNamespace(config=loaded)) is pacing._PACINGS['cinematic']
    assert DEFAULTS['ava_edit.demo_pacing'] == 'instant'
    assert SETTINGS_SCHEMA['ava_edit.demo_pacing']['settings_ui'] is False
    win = windows()
    assert not any('demo_pacing' in key for key in win._widgets)
    assert not any(key == 'ava_edit.demo_pacing' for key, _, _ in win.config_row_inventory())
    assert 'ava_edit' not in win._source_saves['ava'][0]({})
    win._widgets['ava_edit_enabled'].setChecked(True)
    assert win._source_saves['ava'][0]({})['ava_edit']['demo_pacing'] == 'cinematic'
    win._apply_and_close()
    assert win.app.config['ava_edit']['demo_pacing'] == 'cinematic'


@pytest.fixture
def pc(qapp, monkeypatch, qt_widget_scope):
    monkeypatch.setattr(guide.thread_registry, 'spawn', lambda _name, fn, **kw: fn())
    with qt_widget_scope(guide._WizardWindow(SimpleNamespace(config={}))) as win:
        win._go_to(win._STEP_CHECK)
        win.show()
        qapp.processEvents()
        yield win


def controls(pc):
    return (pc._gpu, pc._vram, pc._ram, pc._cpu, pc._cores)


def test_detected_controls_are_frameless_facts_and_recommendation_uses_them(pc, monkeypatch):
    facts = hw.Hardware((hw.GPU('NVIDIA Test', 12*hw.GIB),), 32*hw.GIB, 'Test CPU', 6, True)
    monkeypatch.setattr(hw, 'check_hardware', lambda **kw: facts)
    pc._check_btn.click()
    assert [type(w) for w in controls(pc)] == [QLineEdit, QDoubleSpinBox, QDoubleSpinBox, QLineEdit, QSpinBox]
    assert all(w.isReadOnly() and not w.hasFrame() for w in controls(pc))
    assert pc._gpu.placeholderText() == pc._cpu.placeholderText() == ''
    pc._ram.setFocus()
    QTest.keyClicks(pc._ram, '8')
    assert pc._ram.value() == 32
    pc._recommend()
    assert pc._local_choice.isChecked()
    assert pc._model_combo.currentText() == 'qwen2.5:7b'


@pytest.mark.parametrize('result', [None, 'error', hw.Hardware((), 0, '', 0, None)])
def test_failed_or_empty_detection_keeps_manual_fallback(pc, monkeypatch, result):
    pc._fill_hardware(hw.Hardware((hw.GPU('NVIDIA Test', 12*hw.GIB),), 32*hw.GIB, 'Test CPU', 6, True))
    def check(**kw):
        if result == 'error':
            raise OSError('synthetic check failure')
        return result
    monkeypatch.setattr(hw, 'check_hardware', check)
    pc._check_btn.click()
    assert all(not w.isReadOnly() and w.hasFrame() for w in controls(pc))
    assert pc._hardware_status.text() == guide.COPY['check_failed']
    pc._gpu.clear()
    QTest.keyClicks(pc._gpu, 'NVIDIA Manual')
    pc._vram.setValue(8)
    pc._ram.setValue(24)
    pc._cpu.setText('Manual CPU')
    pc._cores.setValue(6)
    assert pc._facts is None
    assert pc._hardware_status.isHidden()
    pc._recommend()
    assert pc._local_choice.isChecked()
    assert guide.COPY['manual_cpu'] in pc._hardware_verdict.text()
