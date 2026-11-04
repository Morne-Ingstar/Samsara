"""1073: General owns mic choice; moved rows retain staged persistence/search."""
import copy
import json
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QComboBox, QPushButton, QLabel

from samsara.ui.settings import general_page, audio_page
from samsara.ui.settings.presentation import SettingRow
from samsara.ui.settings_qt import _SettingsWindow
from tests.test_settings import _StubApp
from tests.test_settings_pages import _schema_defaults


@pytest.fixture
def window(qapp, monkeypatch, qt_widget_scope, tmp_path):
    monkeypatch.setattr('samsara.autostart.sync_run_entry', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.ui.first_run_qt.ComponentsPage.refresh_async', lambda *a: None)
    monkeypatch.setattr('samsara.ava_readiness.recheck_after_settings', lambda *a: None)
    app = _StubApp()
    app.config = _schema_defaults()
    app.available_mics = [{'id': 7, 'name': 'Fixture microphone'}]
    app.switch_microphone = Mock()
    saved = tmp_path / 'settings.json'
    app.save_config = Mock(side_effect=lambda: saved.write_text(json.dumps(app.config)))
    win = _SettingsWindow(app)
    monkeypatch.setattr(win, '_replace_tts_runtime', lambda *a: None)
    win._saved_fixture = saved
    with qt_widget_scope(win):
        yield win


def test_main_and_advanced_order_and_single_picker(window):
    assert [r[2].split(',')[0] for r in general_page.BASE] == [
        'mic_combo', 'lang_combo', 'listening_display', 'adv_indicator_pos',
        'ui_scale_combo', 'theme_combo', 'hints_enabled', 'notifications_level', 'launch_at_login']
    assert [r[0] for r in general_page.ADVANCED] == [
        'Start hidden', 'Floating windows', 'Show recent words on Home', 'Idle preview']
    keys = [r[2] for r in audio_page.BASE]
    assert keys[keys.index('tts_engine'):] == [
        'tts_engine', 'tts_voice_combo,tts:Test Voice', 'tts_speed', 'tts_volume_slider',
        'ava_captions', 'ava_mute_audio']
    window.show_tab('Audio')
    row = window._pages['Audio'].findChild(SettingRow, 'microphoneSetupGuide')
    assert row.description == 'Test the microphone Samsara listens to.'
    assert not row.findChildren(QComboBox)
    # 1120 adds the quick level check beside the existing comparison.
    assert [b.text() for b in row.findChildren(QPushButton)] == ['Check mic', 'Mic score...']
    assert 'audio_mic_combo' not in window._widgets
    assert not hasattr(window._widgets['mic_combo'], '_audio_mic_combo')
    assert window._widgets['audio_mic_setup_guide_button'].text() == 'Microphone setup'


def test_session_end_has_advanced_audio_feedback_heading(window):
    window.show_tab('Audio')
    page = window._pages['Audio']
    layout = page.advanced.rows
    heading = None
    found = False
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if isinstance(widget, QLabel) and widget.property('role') == 'settingsGroup':
            heading = widget.text()
        if widget is window._rows_by_widget['cmd_exit_earcon']:
            assert heading == 'Audio Feedback'
            assert widget.disclosure is page.advanced
            found = True
    assert found


@pytest.mark.parametrize('level,visible', [('silent', False), ('brief', True)])
def test_speech_volume_tracks_voice_speed_visibility(window, level, visible):
    window.show_tab('Audio')
    choice = window._widgets['tts_feedback_level']
    choice.setCurrentIndex(choice.findData(level))
    window._refresh_conditions()
    for key in ('tts_voice_combo', 'tts_speed', 'tts_volume_slider'):
        row = window._rows_by_widget[key]
        assert not row.isHidden() == visible
        assert row.disclosure is None


@pytest.mark.parametrize('key,page', [
    ('microphone', 'General'), ('microphone_name', 'General'), ('language', 'General'),
    ('listening_indicator_position', 'General'), ('tts.volume', 'Audio'),
    ('command_mode.exit_earcon', 'Audio')])
def test_moved_rows_remain_searchable_on_their_page(window, key, page):
    bindings = window.config_row_inventory()
    matches = [(host, row) for candidate, host, row in bindings if candidate == key]
    assert len(matches) == 1
    assert matches[0][0] == page
    window._search_edit.setText(key)
    window._apply_search_filter()
    assert not matches[0][1].isHidden()


@pytest.mark.parametrize('control', ['microphone', 'indicator', 'volume', 'earcon'])
def test_apply_and_close_writes_original_keys(window, control):
    window.show_tab('Audio' if control in ('volume', 'earcon') else 'General')
    before = copy.deepcopy(window.app.config)
    if control == 'microphone':
        window._widgets['mic_combo'].setCurrentIndex(1)
    elif control == 'indicator':
        window._widgets['adv_indicator_pos'].setCurrentText('top-right')
    elif control == 'volume':
        window._widgets['tts_volume_slider'].setValue(37)
    else:
        previous_earcon = window._widgets['cmd_exit_earcon'].isChecked()
        window._widgets['cmd_exit_earcon'].setChecked(not previous_earcon)
    assert window.app.config == before
    window.app.save_config.assert_not_called()
    window._apply_button.click()
    window.app.save_config.assert_called_once()
    persisted = json.loads(window._saved_fixture.read_text())
    if control == 'microphone':
        assert persisted['microphone'] == 7
        assert persisted['microphone_name'] == 'Fixture microphone'
        window.app.switch_microphone.assert_called_once_with(7)
    elif control == 'indicator':
        assert persisted['listening_indicator_position'] == 'top-right'
    elif control == 'volume':
        assert persisted['tts']['volume'] == .37
    else:
        assert persisted['command_mode']['exit_earcon'] is not previous_earcon
