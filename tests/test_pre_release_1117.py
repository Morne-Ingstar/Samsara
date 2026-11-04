"""1117: failed recovery stays actionable; text and workers follow shared policy."""
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QPushButton

from samsara import clipboard, hold_recording_recovery as recovery
from samsara.runtime import thread_registry
from samsara.ui import home_qt, home_signals, stats_qt, theme
from tests.test_home_qt import _app, _FakeSettings, _stopped_app
from tests.test_incident_holds_1052a import session as incident_session


@pytest.fixture
def home(qapp, monkeypatch, qt_widget_scope):
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda app: 0)
    monkeypatch.setattr(home_signals, '_diag_records', lambda *args: [])
    monkeypatch.setattr(home_signals, 'ava_state', lambda app: home_signals.CapabilityState(
        home_signals.AVA, home_signals.READY, '', 'inert readiness'))

    pages = []

    def make(app):
        page = home_qt.HomePage(app)
        pages.append(page)
        page.resize(760, 900)
        page.show()
        qapp.processEvents()
        return page

    yield make
    for page in pages:
        with qt_widget_scope(page):
            pass


def test_repeated_failed_restart_then_success(home):
    app = _stopped_app()
    app.restart_succeeds = False
    page = home(app)
    for _ in range(2):
        page._notice_btn.click()
        page.refresh()
        assert page.notice is not None
        assert page._notice_text.text()
        assert page._notice_btn.isVisibleTo(page)
        assert page._notice_btn.text() == home_signals.RESTART_HANDS_FREE_LABEL
    app.restart_succeeds = True
    page._notice_btn.click()
    assert len(app.restart_calls) == 3
    assert page.notice is None
    assert not page._notice_card.isVisibleTo(page)


def test_dismiss_after_failed_restart_stays_dismissed(home):
    app = _stopped_app()
    app.restart_succeeds = False
    page = home(app)
    page._notice_btn.click()
    dismiss = next(b for b in page._notice_card.findChildren(QPushButton)
                   if b.text() == 'Dismiss')
    dismiss.click()
    page.refresh()
    assert page.notice is None
    assert not page._notice_card.isVisibleTo(page)


@pytest.mark.parametrize('fixed', [False, True])
def test_microphone_setup_dismisses_only_when_device_returns(home, fixed):
    app = _app(available_mics=[])
    calls = []

    def setup():
        calls.append(True)
        if fixed:
            app.available_mics = [{'id': 7, 'name': 'Synthetic mic'}]

    app.open_mic_setup_guide = setup
    page = home(app)
    page._notice_btn.click()
    assert calls == [True]
    assert (page.notice is None) == fixed
    assert page._notice_card.isVisibleTo(page) == (not fixed)


@pytest.mark.parametrize('scale', [1.0, 1.5])
def test_stats_hint_and_page_follow_accessibility_scale(qapp, qt_widget_scope, scale):
    previous = QFont(qapp.font())
    font = QFont('Segoe UI')
    font.setPointSizeF(home_qt.BASE_POINT_SIZE * scale)
    qapp.setFont(font)
    try:
        card = stats_qt.StatsCard()
        page = stats_qt.StatsPage(SimpleNamespace(history_store=None))
        with qt_widget_scope(card), qt_widget_scope(page):
            card.bind(lambda page: None)
            card.resize(200, 100)
            card.show()
            card.setFocus()
            page.show()
            qapp.processEvents()
            assert card._hint.font().pixelSize() == round(theme.TYPE_MIN * scale)
            assert card.rect().contains(card._hint.geometry())
            labels = {label.text(): label for label in page.findChildren(QLabel)}
            assert labels['Stats'].font().pixelSize() == round(theme.TYPE_TITLE * scale)
            assert labels['Nothing yet - dictate something'].font().pixelSize() == round(theme.TYPE_BODY * scale)
    finally:
        qapp.setFont(previous)


def test_successful_setup_offers_the_next_active_fault(home):
    app = _stopped_app(available_mics=[])
    app.open_mic_setup_guide = lambda: setattr(app, 'available_mics', [
        {'id': 7, 'name': 'Synthetic mic'}])
    page = home(app)
    assert page.notice.capability == home_signals.DICTATION
    page._notice_btn.click()
    assert page.notice.capability == home_signals.HANDS_FREE
    assert page._notice_btn.text() == home_signals.RESTART_HANDS_FREE_LABEL
    assert page._notice_card.isVisibleTo(page)


@pytest.mark.parametrize('next_fault', [False, True])
def test_microphone_recovery_completes_on_a_later_refresh(home, next_fault):
    app = _stopped_app(available_mics=[]) if next_fault else _app(available_mics=[])
    app.open_mic_setup_guide = lambda: None  # Setup returns before the replug.
    page = home(app)
    original = page.notice
    page._notice_btn.click()
    for _ in range(2):
        page.refresh()
        assert page.notice == original
        assert page._notice_card.isVisibleTo(page)
    app.available_mics = [{'id': 7, 'name': 'Reconnected USB mic'}]
    page.refresh()
    if next_fault:
        assert page.notice.capability == home_signals.HANDS_FREE
        assert page._notice_btn.text() == home_signals.RESTART_HANDS_FREE_LABEL
    else:
        assert page.notice is None
        assert page._notice_card.isHidden()
        # A later disconnect is a new actionable fault after resolution.
        app.available_mics = []
        page.refresh()
        assert page.notice.capability == home_signals.DICTATION
        assert page._notice_card.isVisibleTo(page)


def test_clipboard_timeout_worker_is_registered_daemon_and_single_flight(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delayed():
        entered.set()
        try:
            assert release.wait(2)
            return clipboard.ClipboardSnapshot()
        finally:
            finished.set()

    monkeypatch.setattr(clipboard, '_save_clipboard_impl', delayed)
    monkeypatch.setattr(clipboard, '_SNAPSHOT_TIMEOUT_S', 0.05)
    try:
        result = clipboard.save_clipboard()
        assert entered.is_set()
        assert result.lost[0] == 'clipboard backup timed out'
        workers = [entry for entry in thread_registry.snapshot()
                   if entry['name'].startswith('clipboard-snapshot') and entry['alive']]
        assert len(workers) == 1 and workers[0]['daemon']
        assert clipboard.save_clipboard().lost[0] == 'clipboard backup still busy'
    finally:
        release.set()
        assert finished.wait(2)
        # The wrapper releases the single-flight lock after the backend returns.
        assert clipboard._snapshot_worker_lock.acquire(timeout=2)
        clipboard._snapshot_worker_lock.release()


def test_incident_timer_is_registered_daemon_and_cancelled_on_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    app = SimpleNamespace(config={'diagnostics': {'keep_incident_holds': True}}, model_rate=16000)
    try:
        hold = incident_session(app)
        recovery.complete_incident(hold, '')
        store = app._incident_store_1052a
        first = store.expiry
        workers = [entry for entry in thread_registry.snapshot()
                   if entry['name'] == first.name and entry['alive']]
        assert len(workers) == 1 and workers[0]['daemon']
        assert first.interval == 30
        hold = incident_session(app)
        recovery.complete_incident(hold, '')
        assert first.finished.is_set()
        assert store.expiry is not first
    finally:
        store = getattr(app, '_incident_store_1052a', None)
        if store is not None:
            store.expiry.cancel()
            store.worker.shutdown(wait=True)


def test_ci_pytest_temp_paths_avoid_appdata_and_soundfile_is_pinned():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    commands = [line for line in workflow.splitlines() if ' -- pytest ' in line or ' -m pytest ' in line]
    assert len(commands) == 2
    for command in commands:
        assert '--basetemp="${{ github.workspace }}/.pytest-tmp/' in command
        assert 'appdata' not in command.lower()
    requirements = (root / 'requirements-test.txt').read_text(encoding='utf-8').splitlines()
    assert any(line.startswith('soundfile==') for line in requirements)
