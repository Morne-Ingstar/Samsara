"""Game mode's real Settings section: runtime, preference and accessibility."""
import threading
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QAccessible


@pytest.fixture
def settings(qapp, monkeypatch):
    from samsara.game_mode import GameMode
    from samsara.ui import theme
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp

    monkeypatch.setattr('samsara.autostart.sync_run_entry', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.cuda_detect.request_cuda_status', lambda cb: cb(True, 'Ready'))
    monkeypatch.setattr('samsara.ui.first_run_qt.ComponentsPage.refresh_async', lambda *a: None)
    monkeypatch.setattr('samsara.ui.window_activation.surface_requested', lambda *a: None)
    original_theme = theme.active_theme()
    app = _StubApp({'model_size': 'small', 'device': 'cuda',
                    'game_mode': {'enabled': True}})
    app.model, app.device_type, app.compute_type = object(), 'cuda', 'float16'
    app.model_lock, app._hold_capture_lifecycle_lock = threading.Lock(), threading.RLock()
    app._game_mode = GameMode(app, Mock(return_value=object()), notify=Mock())
    window = _SettingsWindow(app)
    window.show_tab('Dictation')
    window._pages['Dictation'].advanced.toggle.setChecked(True)
    window.resize(1160, 950)
    window.show()
    qapp.processEvents()
    yield window
    window.close()
    window.deleteLater()
    qapp.processEvents()
    theme.set_theme(original_theme, refresh=False)


def refresh(window, qapp):
    window._widgets['game_runtime_status'].findChild(QTimer).timeout.emit()
    qapp.processEvents()


def assert_status(window, qapp, state, running):
    from samsara.ui import theme

    refresh(window, qapp)
    label = window._widgets['game_runtime_status']
    badge = window._widgets['game_runtime_badge']
    row = window._rows_by_widget['game_enabled']
    assert row.heading.font().weight() >= 600
    assert label.text() == running
    assert badge.text() == state
    for widget, text in ((label, running), (badge, state)):
        assert widget.isVisible()
        assert QAccessible.queryAccessibleInterface(widget).text(QAccessible.Text.Name) == text
        assert widget.font().pixelSize() == theme.TYPE_BODY
        assert widget.height() >= widget.heightForWidth(widget.width())
    assert QAccessible.queryAccessibleInterface(row).text(QAccessible.Text.Name) == f'Game mode: {state}. {running}'
    assert row.isAncestorOf(label)
    assert not window._rows_by_widget['adv_device'].isAncestorOf(label)
    fill = theme.ACCENT_DIM if state == 'Active' else theme.BG2
    ink = badge.palette().color(badge.foregroundRole()).name()
    assert theme.contrast_ratio(ink, fill) >= 4.5
    assert theme.contrast_ratio(label.palette().color(label.foregroundRole()).name(), theme.BG0) >= 4.5
    # Heading, status and the real native toggle share a row at both widths.
    toggle = window._widgets['game_enabled']
    header = badge.parentWidget()
    assert header.isAncestorOf(toggle)
    assert header.rect().contains(toggle.geometry())
    assert not badge.geometry().intersects(toggle.geometry())
    assert not badge.geometry().intersects(row.heading.geometry())
    assert label.mapTo(row, QPoint()).y() >= header.geometry().bottom()
    assert window._widgets['adv_device'].currentText() == 'CUDA (NVIDIA GPU)'


@pytest.mark.parametrize('palette', ['light', 'dark'])
@pytest.mark.parametrize('width', [800, 1160])
def test_off_waiting_active_and_switching(settings, qapp, palette, width):
    from samsara.ui import theme

    window, policy = settings, settings.app._game_mode
    theme.set_theme(palette, refresh=False)
    window.apply_theme()
    window.resize(width, 950)
    primary = 'Currently running: Small model \u00b7 NVIDIA GPU'
    cpu = 'Currently running: Base model (English) \u00b7 CPU'
    assert_status(window, qapp, 'Off', primary)
    assert 'Automatic detection is enabled; no game' in window._widgets['game_runtime_detail'].text()
    window.app.recording = True
    policy.set_manual(True)
    assert not policy.prepare()
    assert_status(window, qapp, 'Waiting until idle', primary)
    assert 'switch on when dictation is idle' in window._widgets['game_runtime_detail'].text()
    window.app.recording = False
    policy.prepare()
    assert policy.reconcile()
    assert_status(window, qapp, 'Active', cpu)

    # Hold a real prepare() in its constructor while the old engine stays live.
    entered, release = threading.Event(), threading.Event()

    def factory(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return object()

    policy.factory = factory
    window.app.config['game_mode']['cpu_model'] = 'small.en'
    worker = threading.Thread(target=policy.prepare)
    worker.start()
    try:
        assert entered.wait(5)
        assert_status(window, qapp, 'Switching', cpu)
        assert 'Preparing Small model (English)' in window._widgets['game_runtime_detail'].text()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert policy.reconcile()
    assert_status(window, qapp, 'Active', 'Currently running: Small model (English) \u00b7 CPU')

    window.app.recording = True
    policy.set_manual(False)
    assert_status(window, qapp, 'Waiting until idle', 'Currently running: Small model (English) \u00b7 CPU')
    assert 'switch off when dictation is idle' in window._widgets['game_runtime_detail'].text()
    window.app.recording = False
    policy._switches.clear()  # Exercise idle transitions, not policy throttling.
    assert policy.reconcile()
    assert_status(window, qapp, 'Off', primary)


def test_unapplied_toggle_and_saved_model_are_explicit(settings, qapp):
    window = settings
    toggle = window._widgets['game_enabled']
    toggle.setChecked(False)
    detail = window._widgets['game_runtime_detail']
    assert 'Disable game mode on Apply.' in detail.text()
    assert detail.accessibleName() == detail.text()
    assert 'Disable game mode on Apply.' in toggle.accessibleDescription()
    assert window.app.config['game_mode']['enabled'] is True
    window.app.config.update(model_size='large-v3', device='cpu')
    refresh(window, qapp)
    assert window._widgets['game_runtime_status'].text() == 'Currently running: Small model \u00b7 NVIDIA GPU'
    window.app.config['game_mode']['enabled'] = False
    toggle.setChecked(True)
    assert 'Enable game mode on Apply.' in detail.text()
    assert 'Game mode is disabled.' in detail.text()


def test_preparing_failed_rate_limited_and_not_ready(settings, qapp):
    from samsara.ui.settings.game_mode import _runtime_status

    window, policy = settings, settings.app._game_mode
    policy.manual = True
    assert _runtime_status(window.app)[0] == 'Preparing model'
    policy.factory.side_effect = RuntimeError('synthetic model failure')
    assert not policy.prepare()
    assert_status(window, qapp, 'Model unavailable', 'Currently running: Small model \u00b7 NVIDIA GPU')
    policy._load_failed = None
    policy.resident = object()
    policy._switches.extend([policy.clock()] * 4)
    assert _runtime_status(window.app)[0] == 'Waiting to switch'
    assert 'Recent model switches' in _runtime_status(window.app)[2]
    window.app._game_mode = None
    window.app.loading_model = True
    assert _runtime_status(window.app)[:2] == ('Starting', 'Currently running: Speech model loading')
    window.app.loading_model = False
    assert _runtime_status(window.app)[:2] == ('Not ready', 'Currently running: Speech model not ready')
