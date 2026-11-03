"""Usage calculations and real Qt entry points; fixture DBs only."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from samsara.history import HistoryManager
from samsara.history_store import HistoryStore
from samsara.usage_stats import read_stats, summarize
from samsara.ui.stats_qt import StatsPage, StatsCard, DailyBars


@pytest.fixture
def store(tmp_path):
    manager = HistoryManager(tmp_path / 'history.db')
    yield HistoryStore(manager)
    manager._conn.close()


def add(store, stamp, text='one two three', **kw):
    manager = store._manager
    ident = manager.add(text, **kw)
    manager._conn.execute('UPDATE history SET timestamp=? WHERE id=?', (stamp.isoformat(), ident))
    manager._conn.commit()
    return ident


def test_fixture_totals_calendar_durations_and_streak(store):
    now = datetime(2026, 11, 3, 12)
    add(store, now, duration_ms=2000)
    add(store, now - timedelta(days=1), text='four five', duration_ms=5000)
    add(store, now - timedelta(days=2), text='six', mode='toggle', duration_ms=1000)
    add(store, now - timedelta(days=4), text='older words', duration_ms=9000)
    add(store, now, entry_type='command', matched_command='Undo that', duration_ms=10000)
    add(store, now, entry_type='wake_command', matched_command='Undo that')
    add(store, now, status='failed', duration_ms=999999)
    add(store, now + timedelta(days=1), text='future')
    result = read_stats(store, now)
    assert (result['today'], result['week'], result['total']) == (3, 5, 8)
    assert (result['dictations'], result['commands']) == (4, 2)
    assert result['average_hold'] == pytest.approx(7 / 3)
    assert result['longest_hold'] == 9000
    assert result['holds_per_day'] == 0.6
    assert result['duration'] == 17000
    assert result['streak'] == 3
    assert result['top_commands'] == [('Undo that', 2)]
    assert len(result['daily']) == 30
    assert result['daily'][-1] == (now.date(), 3)


def test_paging_does_not_truncate_all_time(store):
    now = datetime(2026, 11, 3, 12)
    store._manager._conn.executemany(
        'INSERT INTO history (timestamp, raw_text, display_text) VALUES (?, ?, ?)',
        [(now.isoformat(), 'word', 'word')] * 1005)
    store._manager._conn.commit()
    changes = store._manager._conn.total_changes
    assert read_stats(store, now)['total'] == 1005
    assert store._manager._conn.total_changes == changes


def test_privacy_failures_and_top_ten(store):
    now = datetime(2026, 11, 3, 12)
    add(store, now, privacy_reason='protected', duration_ms=1000)
    add(store, now, entry_type='command', privacy_reason='protected', matched_command='secret')
    for i in range(12):
        add(store, now, entry_type='command', matched_command=f'command {i}')
    result = read_stats(store, now)
    assert result['total'] == 0
    assert result['duration'] == 1000
    assert len(result['top_commands']) == 10
    assert 'secret' not in str(result)


def test_yesterday_streak_and_bad_timestamp():
    now = datetime(2026, 11, 3)
    rows = [dict(timestamp='broken'), dict(timestamp='2026-11-02', display_text='word')]
    assert summarize(rows, now)['streak'] == 1
    assert summarize(rows, now + timedelta(days=1))['streak'] == 0


def test_empty_state(qapp, store):
    page = StatsPage(SimpleNamespace(history_store=store))
    assert 'Nothing yet - dictate something' in [w.text() for w in page.findChildren(QLabel)]
    assert read_stats(None)['total'] == 0
    page.deleteLater()


@pytest.fixture
def host(qapp, monkeypatch, qt_widget_scope):
    from samsara.ui import home_qt, home_signals, main_window_qt
    from tests.test_home_qt import _app, _FakeSettings
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(main_window_qt._MainWindow, '_restore_geometry', lambda self: None)
    monkeypatch.setattr(main_window_qt._MainWindow, '_save_geometry', lambda self: None)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda app: 0)
    monkeypatch.setattr(home_signals, '_diag_records', lambda *a: [])
    monkeypatch.setattr(home_signals, 'diagnostic_hints', lambda app: [])
    monkeypatch.setattr(home_qt, '_user_word_count', lambda app: 0)
    app = _app()
    window = main_window_qt._MainWindow(app)
    window._poll_timer.stop()
    with qt_widget_scope(window):
        window.show()
        qapp.processEvents()
        yield window


@pytest.mark.parametrize('activation', ['click', 'enter', 'space'])
def test_card_opens_real_stats_page(host, qapp, activation):
    card = host._panel_cache['Home']._words_card
    assert isinstance(card, StatsCard)
    assert card.accessibleName() == 'Words today - open stats'
    assert card.focusPolicy() == Qt.StrongFocus
    card.setFocus()
    qapp.processEvents()
    assert card._hint.isVisible()
    if activation == 'click':
        QTest.mouseClick(card, Qt.LeftButton)
    else:
        QTest.keyClick(card, Qt.Key_Return if activation == 'enter' else Qt.Key_Space)
    qapp.processEvents()
    assert isinstance(host._stack.currentWidget(), StatsPage)


def test_reopening_refreshes_and_chart_is_accessible(host, store, qapp):
    host._app.history_store = store
    page = host._activate('Stats')
    assert page.stats['total'] == 0
    host._activate('Home')
    add(store, datetime.now() - timedelta(seconds=1))
    host._activate('Stats')
    qapp.processEvents()
    assert page.stats['total'] == 3
    chart = page.findChild(DailyBars)
    assert '3 words' in chart.accessibleDescription()
    host.resize(700, 500)
    qapp.processEvents()
    assert page.horizontalScrollBar().maximum() == 0


@pytest.mark.parametrize('phrase', ['show my stats', 'open stats'])
def test_voice_aliases_schedule_existing_navigation(monkeypatch, phrase):
    from plugins.commands import stats
    from samsara import plugin_commands, samsara_navigation
    plugin_commands._reinstall_module_commands(stats)
    called = []
    monkeypatch.setattr(samsara_navigation, '_open_hub', lambda app, page: called.append(page))
    app = SimpleNamespace(_schedule_ui=lambda callback: callback())
    entry = plugin_commands._REGISTRY[phrase]
    entry['func'](app, '')
    assert called == ['Stats']
