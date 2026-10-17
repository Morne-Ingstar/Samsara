"""Home's status layouts must settle, including after long-lived display use."""
from collections import Counter
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QBoxLayout

from samsara.ui import home_qt, home_signals, main_window_qt, theme
from samsara.ui.widgets.status_label import StatusLabel, StatusRow


def _pump(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


class _Settings:
    def __init__(self, *_args):
        pass

    def value(self, _key, default=None, **_kwargs):
        return default

    def setValue(self, *_args):
        pass


@pytest.fixture
def home(qapp, monkeypatch):
    # Build the real hub, without its app/bootstrap, registry or user files.
    monkeypatch.setattr(home_qt, 'QSettings', _Settings)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda *_: 0)
    monkeypatch.setattr(home_signals, '_user_words_exist', lambda: True)
    monkeypatch.setattr(home_signals, 'generic_hints', lambda *_: [])
    monkeypatch.setattr(home_signals, '_diag_records', lambda *_: [])
    app = SimpleNamespace(
        config={'mode': 'hold', 'wake_word_enabled': False,
                'ollama': {'enabled': False}, 'ava_declined': True},
        available_mics=[])
    paints = Counter()
    watched = []
    original = StatusLabel.paintEvent

    def paint(label, event):
        if label in watched:
            paints[label.text()] += 1
        original(label, event)

    monkeypatch.setattr(StatusLabel, 'paintEvent', paint)
    window = main_window_qt._MainWindow(app)
    watched.extend(window.findChildren(StatusLabel))
    window.show()
    _pump(250)
    paints.clear()
    try:
        yield SimpleNamespace(window=window, app=app, paints=paints)
    finally:
        window.force_close()
        window.deleteLater()
        qapp.processEvents()


def _assert_idle(home):
    # Separate label idleness from Home's independent two-second status poll.
    home.window._poll_timer.stop()
    _pump(250)
    home.paints.clear()
    _pump(5000)
    assert not home.paints, home.paints


def test_home_status_painting_settles(home):
    assert len(home.window.findChildren(StatusLabel)) == 3
    _pump(2000)
    assert all(home.paints[name] < 10 for name in ('MODE', 'WAKE', 'MIC')), home.paints
    _assert_idle(home)


def test_one_status_change_has_at_most_two_paints_then_stays_idle(home):
    # A change can need a second paint while Qt redistributes the layout.
    home.window._poll_timer.stop()
    home.app.config['mode'] = 'toggle'
    home.window._refresh_status()
    _pump(250)
    assert 1 <= home.paints['MODE'] <= 2, home.paints
    assert all(home.paints[name] <= 2 for name in ('WAKE', 'MIC')), home.paints
    _assert_idle(home)


def test_home_idle_cpu_stays_below_tenth_of_one_core(home, request):
    import psutil

    # Keep Home's normal polling enabled; measure the offscreen process over
    # the same 20-second interval as the original before/after probe.
    process = psutil.Process()
    process.cpu_percent(None)
    _pump(20000)
    cpu = process.cpu_percent(None)
    request.node.user_properties.append(('idle_cpu_percent_one_core', cpu))
    assert cpu < 10.0, f'idle CPU: {cpu}% of one core'


@pytest.mark.parametrize('action', ['hide', 'showMinimized'])
def test_status_has_no_paints_when_hidden_or_minimized(home, action):
    getattr(home.window, action)()
    _pump(250)
    home.paints.clear()
    _pump(500)
    assert not home.paints


@pytest.mark.parametrize('display', ['Full', 'Compact'])
def test_status_settles_after_200_changes_and_receipt_expiries(home, monkeypatch, display):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    from samsara.ui.listening_indicator import ListeningIndicator

    clock = [0.0]
    controller = None
    if display == 'Full':
        monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
        controller = LiveSurfaceController(
            SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}}),
            model=LiveSurfaceModel(clock=lambda: clock[0]))
        indicator = LiveSurfaceIndicatorAdapter(controller)

        def flush():
            controller.drain()
            return controller.view().notice
    else:
        indicator = ListeningIndicator()
        indicator.set_idle_animation(False)
        monkeypatch.setattr(indicator, '_now_ms', lambda: round(clock[0] * 1000))

        def flush():
            return SimpleNamespace(message=indicator._chip_label or '',
                                   expires_at=indicator._chip_deadline)

    try:
        for i in range(200):
            home.app.config.update(mode='hold' if i % 2 else 'toggle',
                                   wake_word_enabled=bool(i % 2),
                                   wake_word_config={'phrase': 'synthetic wake ' + str(i)})
            home.window._refresh_status()
            indicator.show_outcome('Synthetic receipt ' + str(i), 'success', 10)
            receipt = flush()
            assert receipt.message
            # 591: clear/hide must preserve the reading dwell, then expiry
            # removes the receipt. Advance a fake clock instead of sleeping.
            indicator.clear_outcome()
            indicator.hide()
            assert flush().message == receipt.message
            # Compact samples whole milliseconds; avoid float roundoff just
            # below the deadline when hundreds of dwell periods accumulate.
            clock[0] = receipt.expires_at + .001
            if controller:
                indicator.reconcile_interactions()
            else:
                indicator._clear_outcome()
            assert flush().message == ''
            _pump(1)
        _pump(250)
        _assert_idle(home)
    finally:
        if controller:
            controller.close()
        else:
            indicator.destroy()
            indicator.deleteLater()


def test_status_layout_still_stacks_and_unstacks(home):
    row = home.window.findChild(StatusRow)
    segment = row.layout().itemAt(0).widget().layout()
    # Long values force stacking even within the offscreen monitor bounds.
    home.window._lbl_mode.setText('Synthetic long mode ' * 8)
    _pump(100)
    assert segment.direction() == QBoxLayout.Direction.TopToBottom
    assert segment.itemAt(0).alignment() == Qt.AlignmentFlag.AlignLeft
    home.window._lbl_mode.setText('Hold')
    home.window._lbl_wake.setText('Off')
    home.window._lbl_mic.setText('Default')
    _pump(100)
    assert segment.direction() == QBoxLayout.Direction.LeftToRight
    assert segment.itemAt(0).alignment() == Qt.AlignmentFlag(0)
    _assert_idle(home)


def test_status_icon_colour_cache_tracks_palette(home, monkeypatch):
    calls = []
    qcolor = theme.qcolor

    def colour(token):
        calls.append(token)
        return qcolor(token)

    monkeypatch.setattr(theme, 'qcolor', colour)
    label = home.window.findChildren(StatusLabel)[0]
    # Warm the icon cache; repainting unchanged ink must not parse it again.
    label.repaint()
    calls.clear()
    label.repaint()
    assert not calls
    monkeypatch.setattr(theme, 'TEXT_SECONDARY', theme.TEXT_PRIMARY)
    label.repaint()
    assert calls == [theme.TEXT_PRIMARY]
