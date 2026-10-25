"""855: phrases lead each row; catalog descriptions and one example follow."""
import pytest
from PySide6.QtCore import QEvent, Qt

from samsara import command_catalog, command_stats
from samsara.ui import command_cheatsheet_qt as cs, theme
from samsara.ui.command_reference_detail import row_help_lines


def fixture_commands():
    return [dict(
        phrase=f'sample command {i}', canonical_id=f'fixture.{i}',
        plugin='builtin', pack='core', kind='builtin', args=[],
        aliases=[f'sample command {i}'],
        display_aliases=[f'alternate wording {j} with several words' for j in range(8)],
        description='Use the selected sample to demonstrate this command.',
        examples=[f'sample command {i} with reviewed words', 'unused second example'],
        risk='destructive', whole_utterance=True,
    ) for i in range(12)]


@pytest.fixture(params=[('dark', False), ('light', False), ('dark', True), ('light', True)])
def panel(qapp, tmp_path, monkeypatch, request):
    palette, embedded = request.param
    old = theme.active_theme()
    theme.set_theme(palette, refresh=False)
    monkeypatch.setattr(cs, 'catalog_rows', lambda _: fixture_commands())
    monkeypatch.setattr(cs, '_scope_context', lambda: None)
    monkeypatch.setattr(cs, '_disabled_packs', lambda: set())
    monkeypatch.setattr(cs._CheatSheetWindow, '_foreground_screen', lambda _: None)
    monkeypatch.setattr(command_stats, 'get_top_commands', lambda _: [])
    win = cs._CheatSheetWindow(lambda _: pytest.fail('Reading must be inert'),
                               lambda: [], tmp_path / 'palette.json', embedded=embedded)
    win.show()
    qapp.processEvents()
    yield win
    win.hide()
    win.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    theme.set_theme(old, refresh=False)


def test_row_order_and_accessibility_match_catalog(panel):
    view = panel._list
    item = view.item(0)
    row = item.data(Qt.ItemDataRole.UserRole + 3)
    parts, _ = view.itemDelegate().text_layout(view.model().index(0, 0), view.viewport().width())
    expected = [row['phrase'].title(), 'also: ' + ', '.join(row['display_aliases']),
                row['description'], 'Say: ' + row['examples'][0], '[destructive, whole utterance]']
    # 864: collapsed row shows phrase + description; remaining copy is disclosed.
    assert [part[0] for part in parts] == [expected[0], expected[2]]
    panel._on_item_clicked(item)
    card = panel._detail_card
    assert card.labels[1].text().lower() == expected[1]
    assert card.labels[2].text() == expected[2]
    assert card.labels[3].text() == 'Say it like this:\n' + '\n'.join(row['examples'])
    assert card.metadata.text() == 'destructive, whole utterance'
    accessible = item.text()
    assert all(text in accessible for text in expected)
    assert [accessible.index(text) for text in expected] == sorted(accessible.index(text) for text in expected)
    assert 'unused second example' not in accessible
    assert item.data(Qt.ItemDataRole.UserRole) == row['phrase']


def test_help_wraps_and_last_row_is_fully_reachable(panel, qapp):
    view = panel._list
    delegate = view.itemDelegate()
    index = view.model().index(0, 0)
    width = view.viewport().width()
    parts, height = delegate.text_layout(index, width)
    assert height == view.visualItemRect(view.item(0)).height()
    # 864: a compact line has parallel columns, expanded details wrap instead.
    assert delegate.text_layout(index, width // 2)[1] == height
    assert parts[0][3].right() < parts[1][3].left()
    panel._on_item_clicked(view.item(0))
    assert panel._detail_card.labels[1].wordWrap()
    assert view.visualItemRect(view.item(0)).height() > height
    panel._detail_card.dismiss()
    view.scrollToBottom()
    qapp.processEvents()
    last = view.visualItemRect(view.item(view.count() - 1))
    assert 0 <= last.top() <= last.bottom() <= view.viewport().rect().bottom()


@pytest.mark.parametrize('description,examples,expected', [
    ('', [], []), ('  ', [], []),
    ('Catalog description.', [], ['Catalog description.']),
    ('', ['reviewed sample'], ['Say: reviewed sample']),
    ('Catalog description.', ['first sample', 'second sample'],
     ['Catalog description.', 'Say: first sample']),
])
def test_missing_catalog_copy_is_omitted(description, examples, expected):
    row = dict(phrase='sample', description=description, examples=examples)
    assert row_help_lines(row) == expected
    assert cs.row_text(row).splitlines() == ['Sample'] + expected


def test_every_catalog_row_keeps_available_help():
    records = command_catalog.load_catalog_json()
    assert records
    for record in records:
        row = dict(record, phrase=command_catalog.canonical_phrase(record))
        text = cs.row_text(row)
        if record.get('description', '').strip():
            assert record['description'].strip() in text, record['canonical_id']
        if record.get('examples'):
            assert 'Say: ' + record['examples'][0] in text, record['canonical_id']
