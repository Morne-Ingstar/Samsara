"""373: isolated decisions, persistence, consent, startup and keyboard layout."""
from copy import deepcopy
import json
from pathlib import Path
import re
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QLocale, Qt
from shiboken6 import isValid
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QLabel, QScrollArea

from samsara import config_defaults
from samsara.onboarding.first_run_recommend import MODEL_REASONS, recommend_language, recommend_model
from samsara.ui import first_run_pages_qt as pages, first_run_wizard_qt as wizard
from samsara.ui import network_voice_notice


@pytest.fixture
def make_window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(pages, 'cuda_chosen', lambda _: False)
    monkeypatch.setattr(QLocale, 'system', lambda: QLocale('en_US'))
    monkeypatch.setattr(pages.autostart, 'sync_run_entry', Mock())
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: False)

    def meter(win):
        win._mic_capture_ready = True
        win._next_btn.setEnabled(True)
    monkeypatch.setattr(wizard._WizardWindow, '_start_meter', meter)
    windows = []

    def make(existing=None):
        path = tmp_path / f'config_{len(windows)}.json'
        if existing is not None:
            path.write_text(json.dumps(existing), encoding='utf-8')
        window = wizard._WizardWindow(path)
        window._mics = [{'id': 7, 'name': 'Synthetic microphone'},
                        {'id': 8, 'name': 'Second synthetic microphone'}]
        window._mic_result.emit('_load_done_', '')
        if window._config['microphone'] is None:
            window._config['microphone'] = 7
        window._decisions.receive_facts({'cpu_cores': 8, 'ram_gb': 16, 'gpu_usable': False})
        window.resize(1000, 800)
        window.show()
        qapp.processEvents()
        windows.append(window)
        return window
    yield make
    for window in windows:
        if isValid(window):
            window.close()
            window.deleteLater()
    qapp.processEvents()


def visit(window, name):
    window._step = [name for name, _ in wizard._STEPS].index(name)
    window._show_step()


def choose(group, value):
    button = next(b for b in group.buttons() if b.property('_value') == value)
    button.click()


def finish(window):
    for _ in range(len(wizard._STEPS) + 1):
        if window.result is not None:
            return window.result
        window._go_next()
    pytest.fail('Wizard did not finish within its step count')


def test_steps_preserve_listening_display_and_remove_use_case():
    assert [name for name, _ in wizard._STEPS] == [
        'Welcome', 'Microphone', 'Components', 'Recognition', 'Talking',
        'Music', 'Voice', 'Listening display', 'Position', 'Complete']
    assert not hasattr(wizard, '_USE_CASE_CONFIGS')


@pytest.mark.parametrize('facts,expected', [
    ({'cpu_cores': 2}, ('tiny', 'low_cores')),
    ({'cpu_cores': 4, 'ram_gb': 7}, ('tiny', 'low_ram')),
    ({'cpu_cores': 4, 'ram_gb': 8, 'gpu_usable': True, 'cuda_chosen': True}, ('small', 'gpu_cuda')),
    ({'gpu_usable': True, 'cuda_chosen': False}, ('base', 'cuda_not_chosen')),
    ({'gpu_usable': False}, ('base', 'no_gpu')),
    ({}, ('base', 'unknown_hardware')),
    ({'cpu_cores': None, 'ram_gb': None}, ('base', 'unknown_hardware')),
    ({'cpu_cores': 0, 'ram_gb': 0}, ('base', 'unknown_hardware')),
])
def test_model_rules(facts, expected):
    assert recommend_model(facts) == expected
    assert expected[1] in MODEL_REASONS


@pytest.mark.parametrize('locale,expected', [('de_DE', 'de'), ('fr-FR', 'fr'), ('xx_YY', 'en'), ('', 'en'), (None, 'en')])
def test_language_rules(locale, expected):
    assert recommend_language(locale, ['en', 'de', 'fr', 'auto']) == expected


def test_fresh_next_only_is_schema_defaults(make_window):
    window = make_window()
    result = finish(window)
    for key in ('mode', 'wake_word_enabled', 'language', 'ducking.recording_mode',
                'tts.feedback_level', 'tts.enabled', 'tts.engine', 'launch_at_login'):
        assert config_defaults.cfg_get(result, key) == config_defaults.cfg_get({}, key), key
    assert result['model_size'] == 'base'
    assert '_use_case' not in result
    pages.autostart.sync_run_entry.assert_not_called()


def test_complete_has_only_startup_checkbox_and_preserves_hints(make_window):
    window = make_window({'hints_enabled': False})
    visit(window, 'Complete')
    assert window._current_page.findChildren(QCheckBox) == [window._decisions.launch]
    result = finish(window)
    assert result['hints_enabled'] is False


def test_rerun_preselects_and_preserves_nested_answers(make_window):
    existing = {'_use_case': 'legacy', 'model_size': 'small', 'language': 'auto',
        'mode': 'toggle', 'hotkey': 'f8', 'wake_word_enabled': True,
        'wake_word_config': {'phrase': 'computer', 'audio': {'extra': 17}},
        'command_mode': {'button': 'mouse5', 'enabled': True, 'inactivity_timeout_s': 88},
        'ducking': {'recording_mode': 'pause', 'enabled': True},
        'tts': {'feedback_level': 'full', 'engine': 'edge', 'network_voice_accepted': True},
        'launch_at_login': True, 'continuous_hotkey': 'f9', 'wake_word_hotkey': 'f10', 'command_hotkey': 'f11'}
    window = make_window(existing)
    d = window._decisions
    assert not d.reason.isVisibleTo(window._pages[3])
    assert d.language.currentData() == 'auto'
    assert d.selected(d.mode) == 'toggle'
    assert d.hands_free.combo == 'mouse5'
    assert d.selected(d.wake) is True
    assert d.phrase.currentText() == 'computer'
    assert d.selected(d.music_group) == 'pause'
    assert d.selected(d.level) == 'full' and d.selected(d.engine) == 'edge'
    assert d.launch.isChecked()
    result = finish(window)
    for key in existing:
        if isinstance(existing[key], dict):
            for subkey, value in existing[key].items():
                if key == 'wake_word_config' and subkey == 'audio':
                    assert result[key][subkey]['extra'] == 17
                else:
                    assert result[key][subkey] == value
        else:
            assert result[key] == existing[key]
    pages.autostart.sync_run_entry.assert_called_once_with(True)


def test_rerun_preselects_existing_microphone_without_changing_the_builder(make_window):
    window = make_window({'microphone': 8})
    assert window._mic_combo.currentText() == 'Second synthetic microphone'
    assert finish(window)['microphone'] == 8


def test_each_answer_changes_only_its_owned_keys(make_window, monkeypatch):
    window = make_window()
    d = window._decisions
    visit(window, 'Recognition')
    before = deepcopy(window._config)
    choose(window._model_group, 'tiny')
    d.language.setCurrentIndex(d.language.findData('fr'))
    window._collect_step()
    before.update(model_size='tiny', language='fr')
    assert window._config == before
    visit(window, 'Talking')
    choose(d.mode, 'toggle')
    choose(d.wake, True)
    d.hands_free._combo = 'mouse4'
    d.phrase.setCurrentIndex(d.phrase.findData('computer'))
    window._hotkey_btns['hotkey']._combo = 'f8'
    window._collect_step()
    before.update(mode='toggle', wake_word_enabled=True, hotkey='f8')
    before['command_mode']['button'] = 'mouse4'
    before['wake_word_config']['phrase'] = 'computer'
    assert window._config == before
    visit(window, 'Music')
    choose(d.music_group, 'off')
    window._collect_step()
    before['ducking']['recording_mode'] = 'off'
    assert window._config == before
    visit(window, 'Voice')
    choose(d.level, 'full')
    window._collect_step()
    before['tts']['feedback_level'] = 'full'
    assert window._config == before


def test_silent_hides_engine_and_does_not_change_it(make_window):
    window = make_window({'tts': {'engine': 'edge', 'network_voice_accepted': True}})
    visit(window, 'Voice')
    choose(window._decisions.level, 'silent')
    window._collect_step()
    assert window._config['tts']['enabled'] is False
    assert window._config['tts']['engine'] == 'edge'
    assert window._decisions.engine_area.isHidden()
    choose(window._decisions.level, 'brief')
    assert not window._decisions.engine_area.isHidden()


@pytest.mark.parametrize('accepted', [False, True])
def test_network_requires_explicit_acceptance(make_window, monkeypatch, accepted):
    window = make_window()
    visit(window, 'Voice')
    request = Mock(return_value=accepted)
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', request)
    choose(window._decisions.engine, 'edge')
    window._collect_step()
    request.assert_called_once_with(window)
    assert window._config['tts']['engine'] == ('edge' if accepted else 'winrt')
    assert window._config['tts']['network_voice_accepted'] is accepted


def test_programmatic_network_selection_cannot_bypass_acceptance(make_window):
    window = make_window()
    visit(window, 'Voice')
    window._decisions.engine.buttons()[1].setChecked(True)
    window._collect_step()
    assert window._config['tts']['engine'] == 'winrt'


def test_notice_has_all_three_fixed_facts():
    facts = network_voice_notice.notice_facts()
    assert len(facts) == 3
    for term in ('everything', 'Microsoft', 'replies', 'dictation', 'remembered'):
        assert term in facts[0]
    assert 'internet connection' in facts[1]
    assert 'Nothing is sent' in facts[2] and 'local voice' in facts[2]


def test_notice_parent_is_a_real_qt_widget(make_window):
    window = make_window()
    dialog = network_voice_notice.NetworkVoiceNotice(window._decisions.owner)
    assert dialog.parentWidget() is window
    dialog.close()


def test_removed_recorders_never_change_existing_keys(make_window):
    expected = {'continuous_hotkey': 'f9', 'wake_word_hotkey': 'f10', 'command_hotkey': 'f11'}
    window = make_window(expected)
    assert set(window._hotkey_btns) == {'hotkey', 'command_mode.button'}
    result = finish(window)
    assert {key: result[key] for key in expected} == expected


@pytest.mark.parametrize('failure', [False, True])
def test_windows_startup_calls_seam_once_and_failure_completes(make_window, monkeypatch, failure):
    window = make_window()
    window._decisions.launch.setChecked(True)
    seam = Mock(side_effect=OSError('synthetic refusal') if failure else None)
    monkeypatch.setattr(pages.autostart, 'sync_run_entry', seam)
    result = finish(window)
    seam.assert_called_once_with(True)
    assert result['launch_at_login'] is not failure


def test_model_override_survives_back_and_hardware_updates(make_window, monkeypatch):
    window = make_window()
    visit(window, 'Recognition')
    choose(window._model_group, 'tiny')
    window._go_back()
    monkeypatch.setattr(pages, 'cuda_chosen', lambda _: True)
    window._decisions.receive_facts({'gpu_usable': True, 'cpu_cores': 8, 'ram_gb': 16})
    window._go_next()
    assert window._decisions.selected(window._model_group) == 'tiny'


def test_closing_ignores_pending_hardware_results(make_window):
    window = make_window()
    before = deepcopy(window._config)
    window.close()
    window._decisions.receive_facts({'cpu_cores': 2})
    assert window._config == before


def test_window_destruction_releases_callbacks_safely(make_window, qapp):
    import gc
    window = make_window()
    visit(window, 'Talking')
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    gc.collect()
    assert not isValid(window)


def test_rerun_retains_advanced_model_and_continuous_mode_until_a_pick(make_window):
    window = make_window({'model_size': 'medium', 'mode': 'continuous'})
    assert window._model_group.checkedButton() is None
    assert window._decisions.mode.checkedButton() is None
    result = finish(window)
    assert result['model_size'] == 'medium'
    assert result['mode'] == 'continuous'


@pytest.mark.parametrize('name', ['Recognition', 'Talking', 'Music', 'Voice', 'Listening display', 'Complete'])
def test_back_next_keeps_answer(make_window, name):
    window = make_window({'model_size': 'small', 'language': 'fr', 'mode': 'toggle',
        'wake_word_enabled': True, 'ducking': {'recording_mode': 'pause'},
        'tts': {'feedback_level': 'full'}})
    while wizard._STEPS[window._step][0] != name:
        window._go_next()
    window._collect_step()
    before = deepcopy(window._config)
    window._go_back()
    window._go_next()
    window._collect_step()
    assert window._config == before


def test_keyboard_arrows_and_enter_advance(make_window, qapp):
    window = make_window()
    visit(window, 'Talking')
    buttons = window._decisions.mode.buttons()
    buttons[0].setFocus()
    QTest.keyClick(buttons[0], Qt.Key.Key_Down)
    assert buttons[1].isChecked()
    QTest.keyClick(buttons[1], Qt.Key.Key_Return)
    assert wizard._STEPS[window._step][0] == 'Music'


def test_progress_labels_fit_at_minimum_width_and_150_percent(make_window, qapp):
    from PySide6.QtGui import QFont, QFontMetrics
    from samsara.ui import theme
    from tools.wizard_screenshots import scale_walkthrough_text
    window = make_window()
    window.resize(720, 680)
    scale_walkthrough_text(window, 1.5)
    for _ in range(4):
        qapp.processEvents()
    strip = window._step_indicator
    metrics = QFontMetrics(theme.qfont(round(theme.TYPE_MIN * 1.5), weight=QFont.Weight.DemiBold))
    for index, text in enumerate(strip.steps):
        assert strip.visualItemRect(strip.item(index)).width() >= metrics.horizontalAdvance(text)


def test_shared_mode_details_and_all_choice_help(make_window):
    from samsara.onboarding.mode_details import MODE_DETAILS
    window = make_window()
    d = window._decisions
    assert d.mode_detail('hold') == MODE_DETAILS['hold']
    assert d.mode_detail('tap') == MODE_DETAILS['tap']
    assert d.mode_detail('wake_word') == MODE_DETAILS['wake_word']
    assert len(d.details) == 13  # 2 talking + hands-free + 2 wake + 3 music + 3 level + 2 engine
    for button, expanded in d.details:
        button.click()
        assert not expanded.isHidden() and expanded.text()
        button.click()
        assert expanded.isHidden()


def test_music_labels_match_settings_and_schema():
    source = (Path(__file__).resolve().parents[1] / 'samsara/ui/settings/advanced_qt.py').read_text(encoding='utf-8')
    choices = pages.music_choices()
    assert [value for value, _, _ in choices] == config_defaults.SETTINGS_SCHEMA['ducking.recording_mode']['options']
    for value, title, _ in choices:
        assert f"'{value}': '{title}'" in source


@pytest.mark.parametrize('name', [name for name, _ in wizard._STEPS])
def test_labels_fit_at_150_percent(make_window, qapp, name):
    from tools.wizard_screenshots import scale_walkthrough_text
    window = make_window()
    scale_walkthrough_text(window, 1.5)
    visit(window, name)
    qapp.processEvents()
    # Wrapped text must fit its allocated line box; unwrapped labels must fit width.
    for item in window._current_page.findChildren(QLabel):
        if not item.isVisibleTo(window._current_page) or not item.text():
            continue
        rectangle = item.contentsRect()
        metrics = item.fontMetrics()
        if item.wordWrap():
            bounds = metrics.boundingRect(rectangle, int(Qt.TextFlag.TextWordWrap), item.text())
            assert bounds.width() <= rectangle.width() + 2, (name, item.text())
            assert bounds.height() <= rectangle.height() + 2, (name, item.text(), bounds.height(), rectangle.height())
        else:
            assert metrics.horizontalAdvance(item.text()) <= rectangle.width() + 2, (name, item.text())
