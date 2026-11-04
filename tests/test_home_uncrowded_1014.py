"""1014: Home fits the desktop and release notes never occupy its card slot."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QLabel, QPushButton

from samsara.speech_model_transfer import Progress
from samsara.ui import home_qt, home_signals, main_window_qt, theme, update_qt
from tests.test_home_qt import _FakeSettings, _app, HINT


def settle(qapp):
    for _ in range(60):
        qapp.processEvents()


@pytest.fixture(autouse=True)
def isolate(monkeypatch, qapp, qt_widget_scope):
    old_font, old_theme = QFont(qapp.font()), theme.active_theme()
    qapp.setFont(QFont('Segoe UI', 9))
    _FakeSettings.values = {}
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(home_qt, '_user_word_count', lambda app: 0)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda app: 0)
    monkeypatch.setattr(home_signals, '_diag_records', lambda *a: [])
    monkeypatch.setattr(home_signals, 'diagnostic_hints', lambda app: [])
    monkeypatch.setattr(home_signals, 'ava_state', lambda app: home_signals.CapabilityState(home_signals.AVA, home_signals.OFF))
    monkeypatch.setattr(main_window_qt._MainWindow, '_save_geometry', lambda self: None)
    monkeypatch.setattr(main_window_qt._MainWindow, '_restore_geometry', lambda self: None)
    monkeypatch.setattr(update_qt, '_dialog', None)
    yield
    qapp.setFont(old_font)
    theme.set_theme(old_theme, refresh=False)


def host(qapp, width=1065, height=698, state='normal'):
    app = _app(model_loaded=state != 'downloading', _speech_model_phase='downloading' if state == 'downloading' else 'ready')
    app._speech_model_progress = Progress(72_000_000, 145_000_000, 52)
    app.config['updates'] = {'automatic_checks': True}
    app.tray_icon = SimpleNamespace(_available_update=SimpleNamespace(version='0.40.4', installer_link_only=False) if state == 'update-available' else None)
    app.history_store.query = lambda **kw: []
    win = main_window_qt._MainWindow(app)
    win._poll_timer.stop()
    win.apply_theme()
    win.setFixedSize(width, height)
    win.show()
    win._refresh_status()
    settle(qapp)
    return win, win._panel_cache['Home']


@pytest.mark.parametrize('palette', ['dark', 'light'])
@pytest.mark.parametrize('scale', [1, 1.25])
@pytest.mark.parametrize('desktop', [(1366, 768), (1920, 1080)])
@pytest.mark.parametrize('state', ['normal', 'downloading', 'update-available'])
def test_every_required_section_fits_the_real_host(qapp, palette, scale, desktop, state):
    theme.set_theme(palette, refresh=False)
    # Equivalent logical client budget; proof renders also set QT_SCALE_FACTOR.
    width = min(1065, int(desktop[0] / scale) - 20)
    height = min(906, int(desktop[1] / scale) - 70)
    win, home = host(qapp, width, height, state)
    try:
        viewport = home._scroll.viewport()
        assert home._scroll.verticalScrollBar().maximum() == 0
        assert home._scroll.horizontalScrollBar().maximum() == 0
        assert not home._hint_card.isVisibleTo(home)
        assert home._cards_cols == 3
        assert home_qt.overflowing_widgets(home) == []
        for widget in (*home.cards, home._identity):
            assert widget.isVisibleTo(home)
            assert viewport.rect().contains(widget.mapTo(viewport, QPoint()))
            assert viewport.rect().contains(widget.mapTo(viewport, widget.rect().bottomRight()))
        for card in (home._words_card, home._infinity_card):
            for label in card.findChildren(QLabel):
                assert label.contentsRect().height() >= label.heightForWidth(label.width())
                assert label.width() >= QFontMetrics(label.font()).horizontalAdvance(label.text()) or label.wordWrap()
        update = home.findChild(home_qt.QWidget, 'homeUpdateLine')
        for button in (update.button, home._whats_new_button):
            assert button.width() >= QFontMetrics(button.font()).horizontalAdvance(button.text())
            assert button.height() >= 44
            assert button.sizePolicy().horizontalPolicy() == home_qt.QSizePolicy.Policy.Fixed
            assert not button.font().underline()
        assert home._whats_new_button.x() >= update.button.geometry().right()
        for button in home._links.values():
            assert button.isVisibleTo(home)
            assert home.rect().contains(button.mapTo(home, button.rect().bottomRight()))
        if state == 'downloading':
            assert '72.0 / 145.0 MB (49%)' in home._notice_text.text()
            assert '52s' in home._notice_text.text()
            assert win._lbl_mode.text() == 'Downloading speech model'
            assert '145.0' not in win._lbl_mode.text()
        elif state == 'update-available':
            assert update.status.text() == 'Version 0.40.4 is available.'
            assert update.button.text() == 'Install v0.40.4…'
    finally:
        win.close()
        win.deleteLater()
        settle(qapp)


def test_open_marks_seen_and_reopens_without_losing_diagnostics(qapp, monkeypatch):
    monkeypatch.setattr(home_qt, 'whats_new_note', lambda seen, **kw: None if seen == '9.9.9' else ('9.9.9', 'New.'))
    monkeypatch.setattr(home_signals, 'diagnostic_hints', lambda app: [HINT])
    opened = []
    monkeypatch.setattr(home_qt, 'open_url', opened.append)
    win, home = host(qapp)
    try:
        button = home._whats_new_button
        assert button.property('unread') and not button.icon().isNull()
        assert home._hint is HINT and home._hint_card.isVisibleTo(home)
        assert not _FakeSettings.values.get(home_qt.WHATS_NEW_SEEN_KEY)
        button.click()
        settle(qapp)
        dialog = button.dialog
        assert dialog.isVisible()
        assert dialog.findChild(QLabel, 'releaseSummary').text() == 'New.'
        assert _FakeSettings.values[home_qt.WHATS_NEW_SEEN_KEY] == '9.9.9'
        assert button.text() == "What's new"
        assert home._hint is HINT
        full = next(b for b in dialog.findChildren(QPushButton) if b.text() == home_qt.CHANGELOG_LINK)
        full.click()
        assert opened == [home_qt.CHANGELOG_URL]
        dialog.close()
        settle(qapp)
        button.click()
        settle(qapp)
        assert button.dialog.isVisible()
        button.dialog.close()
    finally:
        win.close()
        win.deleteLater()
        settle(qapp)


def test_seen_version_is_still_available_and_future_version_is_not_announced(qapp):
    _FakeSettings.values[home_qt.WHATS_NEW_SEEN_KEY] = home_qt.__version__
    win, home = host(qapp)
    try:
        assert home._whats_new_button.text() == "What's new"
        home._whats_new_button.click()
        assert home._whats_new_button.dialog.findChild(QLabel, 'releaseSummary').text().startswith('0.41.1 -')
        assert 'signed' not in home._whats_new_button.dialog.findChild(QLabel, 'releaseSummary').text()
        home._whats_new_button.dialog.close()
    finally:
        win.close()


@pytest.mark.parametrize('version,expected', [
    ('0.40.3', '0.40.3 - a more forgiving microphone setup, and a safer uninstall.'),
    ('0.40.4', '0.40.4 - faster start, simpler setup, a better microphone picker, and a cleaner install and uninstall.'),
])
def test_corrected_in_app_release_summaries(tmp_path, version, expected):
    path = tmp_path / 'CHANGELOG.md'
    path.write_text(f'# Changelog\n\n## [{version}]\n\nOld summary.\n', encoding='utf-8')
    assert home_qt.whats_new_note(None, path, current=version) == (version, expected)
    assert home_qt.whats_new_note(version, path, current=version) is None
    assert home_qt.whats_new_note(None, path, current='0.39.0') is None
