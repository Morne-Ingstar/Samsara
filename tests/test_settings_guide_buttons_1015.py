"""Persistent, compact Settings guide actions use existing wizard routes."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPoint
from PySide6.QtWidgets import QPushButton

from samsara import ava_readiness
from samsara.ui import settings_qt
from samsara.ui.settings.presentation import GuideHeader
from tests.test_ava_page_guide_669 import _App
from tests.test_settings_dell_1010 import _configured


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.cuda_detect.request_cuda_status', lambda cb: None)
    monkeypatch.setattr(settings_qt.window_activation, 'confirm_foreground', lambda *a: None)
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda app: SimpleNamespace(ready=False))
    win = settings_qt._SettingsWindow(_App())
    yield win
    win.hide()
    win.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize('size', [(1366, 768), (1920, 1080)])
@pytest.mark.parametrize('name,label,key', [
    ('Ava', 'Ava guide', 'ava_setup_guide_button'),
    ('Dictation', 'Microphone setup', 'dictation_mic_setup_guide_button'),
    ('Audio', 'Microphone setup', 'audio_mic_setup_guide_button'),
])
def test_each_guide_page_has_one_compact_action_opposite_title(window, qapp, size, name, label, key):
    window.resize(*size)
    window.show_tab(name)
    qapp.processEvents()
    page = window._pages[name]
    header = page.guide_header
    button = window._widgets[key]
    assert isinstance(header, GuideHeader)
    assert header.findChildren(QPushButton) == [button]
    assert button.text() == button.accessibleName() == label
    assert button.isVisible() and button.isEnabled()
    assert button.property('settingsToolAction') is True
    origin = button.mapTo(header, QPoint(0, 0))
    title_origin = page.heading.mapTo(header, QPoint(0, 0))
    assert origin.x() > header.width() / 2
    assert abs(origin.x() + button.width() - header.width()) <= 1
    assert abs(origin.y() + button.height() / 2 - title_origin.y() - page.heading.height() / 2) <= 1
    assert 44 <= button.height() <= 48
    assert button.width() < 300
    assert page.verticalScrollBar().value() == 0


def test_ava_setup_changes_keep_guide_geometry_and_user_disclosure_choice(window, qapp):
    window.show_tab('Ava')
    qapp.processEvents()
    page = window._pages['Ava']
    button = window._widgets['ava_setup_guide_button']
    geometry = button.geometry()
    page.advanced.reveal()
    for config in (_configured(), {}, _configured('cloud')):
        window.app.config = config
        window._refresh_conditions()
        qapp.processEvents()
        assert window._pages['Ava'] is page
        assert button.isVisible() and button.isEnabled()
        assert button.geometry() == geometry
        assert page.advanced.toggle.isChecked()
        assert page.advanced.toggle.accessibleName() == 'Advanced'
        assert window._widgets['ava_conversation_log_enabled'].isVisible()
        assert window._rows_by_widget['providerRoute'].isVisible()


@pytest.mark.parametrize('name,key,callback', [
    ('Ava', 'ava_setup_guide_button', 'open_ava_guide'),
    ('Dictation', 'dictation_mic_setup_guide_button', 'open_mic_setup_guide'),
    ('Audio', 'audio_mic_setup_guide_button', 'open_mic_setup_guide'),
])
def test_header_action_opens_existing_guide_without_saving(window, qapp, name, key, callback):
    window.show_tab(name)
    qapp.processEvents()
    called = []
    setattr(window.app, callback, lambda: called.append(callback))
    before = deepcopy(window.app.config)
    window._widgets[key].click()
    assert called == [callback]
    assert window.app.config == before


@pytest.mark.parametrize('enabled', [False, True])
def test_wake_section_action_is_available_even_when_wake_is_off(window, qapp, monkeypatch, enabled):
    window.app.config = {'wake_word_enabled': enabled}
    window.show_tab('Dictation')
    qapp.processEvents()
    page = window._pages['Dictation']
    header = page.wake_guide_header
    button = window._widgets['wake_setup_guide_button']
    assert isinstance(header, GuideHeader)
    assert header.findChildren(QPushButton) == [button]
    assert button.text() == 'Wake word setup'
    assert button.isVisible() and button.isEnabled()
    assert button.property('settingsToolAction') is True
    assert button.sizePolicy() == page.guide_header.button.sizePolicy()
    assert button.height() == page.guide_header.button.height()
    called = []
    monkeypatch.setattr('samsara.ui.mic_setup_wizard_qt.open_wake_calibration',
                        lambda app, *, parent: called.append((app, parent)))
    before = deepcopy(window.app.config)
    button.click()
    assert called == [(window.app, window)]
    assert window.app.config == before


@pytest.mark.parametrize('name', ['General', 'Shortcuts', 'Help'])
def test_pages_without_their_own_guide_gain_no_header_button(window, qapp, name):
    window.show_tab(name)
    qapp.processEvents()
    assert not hasattr(window._pages[name], 'guide_header')
