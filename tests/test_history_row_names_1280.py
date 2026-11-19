"""1280: synthetic History entries expose useful names through the Qt model."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from samsara.ui import history_view as hv


@pytest.fixture
def view(qapp, monkeypatch):
    # Render synchronously and keep settings and clipboard off the desktop.
    monkeypatch.setattr(hv.HistoryView, '_reload', lambda self: None)
    class Settings:
        def __init__(self, *args):
            pass

        def value(self, key, default=None, **kwargs):
            return default

        def setValue(self, *args):
            pass

    monkeypatch.setattr(hv, 'QSettings', Settings)
    page = hv.HistoryView(None)
    yield page
    page.close()
    page.deleteLater()
    qapp.processEvents()


def row(entry_id, text='Synthetic blue boat', **fields):
    return dict(id=entry_id, timestamp=fields.pop('timestamp', '2026-11-19T10:20:30'),
                display_text=text, raw_text=text, entry_type='dictation',
                status=fields.pop('status', 'success'), **fields)


def items(view):
    return [view._list.item(i) for i in range(view._list.count())
            if view._list.item(i).data(Qt.ItemDataRole.UserRole)]


def name(view, item):
    # Read through the public model role used by accessibility clients.
    return view._list.model().data(view._list.indexFromItem(item), Qt.ItemDataRole.AccessibleTextRole)


def test_similar_rows_in_same_minute_have_distinct_names(view):
    view._render_rows([row(2, timestamp='2026-11-19T10:20:31'), row(1)], append=False)
    names = [name(view, item) for item in items(view)]
    assert names == ['2026-11-19 10:20:31, Synthetic blue boat',
                     '2026-11-19 10:20:30, Synthetic blue boat']
    assert all(item.text() == '' for item in items(view))  # No visible model text added.


def test_excerpt_distinguishes_rows_with_same_timestamp(view):
    view._render_rows([row(2, 'Synthetic blue boat'), row(1, 'Synthetic red boat')], append=False)
    names = [name(view, item) for item in items(view)]
    assert names[0] != names[1]
    assert names[0].endswith('Synthetic blue boat')
    assert names[1].endswith('Synthetic red boat')


@pytest.mark.parametrize(('fields', 'expected'), [
    ({'entry_type': 'failed', 'status': 'failed'}, 'Failed'),
    ({'status': 'failed'}, 'Not typed'),
    ({'entry_type': 'command', 'status': 'failed'}, 'Failed'),
    ({'status': 'empty', 'mode': 'hold'}, 'No speech'),
])
def test_failure_names_describe_outcome(view, fields, expected):
    entry = row(1, '', **{k: v for k, v in fields.items() if k != 'entry_type'})
    entry.update(fields)
    view._render_rows([entry], append=False)
    assert name(view, items(view)[0]) == f'2026-11-19 10:20:30, {expected}'


def test_long_excerpt_collapses_whitespace_and_stays_short(view):
    text = 'Synthetic\n  blue\tboat ' + 'sails ' * 20
    view._render_rows([row(1, text)], append=False)
    excerpt = name(view, items(view)[0]).split(', ', 1)[1]
    assert excerpt.startswith('Synthetic blue boat sails')
    assert len(excerpt) == 40
    assert excerpt.endswith('\u2026')
    assert '\n' not in excerpt and '\t' not in excerpt


@pytest.mark.parametrize('reason', ['unverified_target', 'password_field'])
def test_concealed_words_are_not_exposed_in_name(view, reason):
    view._render_rows([row(1, 'Synthetic concealed words', privacy_reason=reason)], append=False)
    spoken = name(view, items(view)[0])
    assert 'Synthetic concealed words' not in spoken
    assert ('Could not check where' if reason == 'unverified_target' else 'password field') in spoken


def test_append_and_reload_supply_current_names(view):
    view._render_rows([row(2)], append=False)
    view._render_rows([row(1, timestamp='2026-11-18T10:20:30')], append=True)
    names = [name(view, item) for item in items(view)]
    assert names[0] != names[1]
    assert names[1].startswith('2026-11-18 10:20:30, ')
    view._render_rows([row(2, 'Synthetic replacement')], append=False)
    assert len(items(view)) == 1
    assert name(view, items(view)[0]).endswith('Synthetic replacement')


def test_arrows_and_enter_copy_selected_words(view, qapp, monkeypatch):
    copied = []
    monkeypatch.setattr(view, '_set_clipboard', lambda text, *args: copied.append(text))
    view._render_rows([row(2, 'Synthetic first\nline'), row(1, 'Synthetic second')], append=False)
    view.show()
    view._list.setCurrentItem(items(view)[0])
    view._list.setFocus()
    qapp.processEvents()
    QTest.keyClick(view._list, Qt.Key.Key_Down)
    assert view._list.currentItem() is items(view)[1]
    QTest.keyClick(view._list, Qt.Key.Key_Return)
    assert copied == ['Synthetic second']
    QTest.keyClick(view._list, Qt.Key.Key_Up)
    QTest.keyClick(view._list, Qt.Key.Key_Return)
    assert copied == ['Synthetic second', 'Synthetic first\nline']
