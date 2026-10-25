"""825: real hub routing, distinct rendered icons and setup-free card examples."""
import json
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import QLabel, QPushButton

from samsara.ui import command_cheatsheet_qt, home_qt, home_signals, main_window_qt
from tests.test_home_qt import _app, _FakeSettings, _hub, _Store


def record(phrase='open notepad', **changes):
    row = dict(phrase=phrase, canonical_id='builtin.' + phrase.replace(' ', '_'),
               aliases=[phrase], kind='builtin', plugin='builtin', pack='core',
               args=[], scope=None, risk='ui', whole_utterance=False)
    row.update(changes)
    return row


@pytest.fixture
def hub(qapp, monkeypatch, qt_widget_scope):
    _FakeSettings.values = {}
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(home_qt, '_user_word_count', lambda app: 0)
    monkeypatch.setattr(home_qt, 'whats_new_note', lambda *a, **kw: None)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda app: 0)
    monkeypatch.setattr(home_signals, '_user_words_exist', lambda: True)
    monkeypatch.setattr(home_signals, 'generic_hints', lambda app: [])
    monkeypatch.setattr(home_signals, '_diag_records', lambda app, limit: [])
    monkeypatch.setattr(main_window_qt._MainWindow, '_geometry_screens',
                        lambda self: ([(0, 0, 1920, 1080)], (0, 0, 1920, 1080)))
    app = _app(history_store=_Store(text=''))
    app.config.update(ava={'provider_policy': 'off'}, ava_command_session={'enabled': False})
    app.open_settings = Mock()
    app.cheat_sheet = Mock()
    app.create_icon_image = lambda: main_window_qt.MarkFrame('idle', 'armed')
    window = _hub(qapp, width=900, height=650, app=app)
    with qt_widget_scope(window):
        yield window, app


def test_commands_nav_order_and_distinct_icons():
    assert main_window_qt.NAV_ORDER[:3] == ('Home', 'Commands', 'History')
    icons = main_window_qt.NAV_ICONS
    assert set(icons) == set(main_window_qt.NAV_ORDER)
    assert len(set(icons.values())) == len(icons)
    assert set(icons.values()) <= home_qt.ICON_GLYPHS.keys()
    assert len(set(home_qt.ICON_GLYPHS.values())) == len(home_qt.ICON_GLYPHS)
    assert icons[home_qt.VOICE_HELP] != 'guides'
    assert home_qt.ICON_GLYPHS['memos'] == chr(0x1F4CC)


@pytest.mark.parametrize('phrase', ['samsara command list', 'open samsara command list',
                                  'open commands', 'go to commands'])
def test_commands_voice_phrases_open_the_hub_page(phrase, monkeypatch):
    from collections import deque
    from types import SimpleNamespace
    from pathlib import Path
    from samsara import samsara_navigation as nav
    from samsara.command_registry import CommandMatcher
    from samsara.handlers import CommandContext, MethodHandler
    from samsara.ui import qt_runtime

    commands = json.loads((Path(__file__).resolve().parents[1] / 'commands.json')
                          .read_text(encoding='utf-8'))['commands']
    matcher = CommandMatcher()
    matcher.load_builtins(commands)
    matcher.freeze()
    entry, remainder = matcher.match(phrase)
    assert entry is not None and not remainder
    pending = deque()
    monkeypatch.setattr(qt_runtime, 'post', pending.append)
    app = nav.SamsaraNavigationMixin()
    app._schedule_ui = pending.append
    app.show_main_window = Mock()
    app.open_hub_page = Mock(return_value=True)
    assert MethodHandler().execute(entry.data, CommandContext(app=app))
    while pending:
        pending.popleft()()
    app.show_main_window.assert_called_once_with()
    app.open_hub_page.assert_called_once_with('Commands')
    # Keep the separately reachable Settings tool as well as the new hub page.
    assert nav.PLACES['commands'].kind == 'tool'
    assert nav.PLACES['commands'].target == 'Command library'


def test_every_glyph_renders_in_the_windows_icon_font(qapp):
    # Qt's inFont(QChar) cannot represent supplementary-plane pin/book symbols.
    font = QFont('Segoe UI Symbol', 14)
    assert 'Segoe UI Symbol' in QFontDatabase.families()
    metrics = QFontMetrics(font)
    for name, glyph in home_qt.ICON_GLYPHS.items():
        supported = (metrics.inFont(glyph) if ord(glyph) <= 0xFFFF
                     else metrics.inFontUcs4(ord(glyph)))
        assert supported, (name, f'U+{ord(glyph):04X}')


def test_settings_then_commands_opens_inside_same_hub_and_home_card_reuses_it(hub, qapp):
    window, app = hub
    window._nav_btns['Settings'].click()
    app.open_settings.assert_called_once_with()
    window._nav_btns['Commands'].click()
    page = window._stack.currentWidget()
    assert window._nav_btns['Commands'].isChecked()
    assert window._panel_cache['Commands'] is page
    assert not page.isWindow()
    assert page.window() is window
    assert page._list.count() > 0
    assert page._title_bar._opacity_slider.isHidden()
    assert page._title_bar._close_button.isHidden()
    assert page._resize_grip.isHidden()
    assert page._filter.width() < page.width()
    assert page._category_bar._combo.width() < page.width() - 100
    page._filter.setText('open notepad')
    assert page._list.count() == 1
    page._on_item_clicked(page._list.item(0))
    assert page._detail_card.isVisibleTo(page)
    window._nav_btns['Home'].click()
    home = window._stack.currentWidget()
    home._cards[0].clicked.emit()
    assert window._stack.currentWidget() is page
    assert page._filter.text() == ''
    assert not page._detail_card.isVisibleTo(page)
    app.cheat_sheet.show.assert_not_called()
    app.cheat_sheet.show_filtered.assert_not_called()


def test_home_filtered_reference_action_keeps_query(hub):
    window, _ = hub
    home = window._stack.currentWidget()
    home._route('cheatsheet', 'snap right')
    page = window._stack.currentWidget()
    assert page is window._panel_cache['Commands']
    assert page._filter.text() == 'snap right'
    assert page._list.count() == 1


def test_embedded_pins_persist_without_overwriting_floating_geometry(hub, tmp_path):
    window, app = hub
    palette = tmp_path / 'reference.json'
    original = dict(pinned=['snap right'], opacity=0.72,
                    geometry=dict(x=3000, y=4000, w=800, h=900))
    palette.write_text(json.dumps(original), encoding='utf-8')
    app.cheat_sheet._palette_path = palette
    window._nav_btns['Commands'].click()
    page = window._stack.currentWidget()
    assert page._pinned == {'snap right'}
    page._toggle_pin('snap left')
    saved = json.loads(palette.read_text(encoding='utf-8'))
    assert saved['pinned'] == ['snap left', 'snap right']
    assert saved['geometry'] == original['geometry']
    assert saved['opacity'] == original['opacity']
    assert page.pos().x() == page.pos().y() == 0


@pytest.mark.parametrize('size', [(900, 650), (700, 500)])
def test_home_still_has_three_cards_and_all_nav_rows_are_reachable(hub, qapp, size):
    window, _ = hub
    window.resize(*size)
    qapp.processEvents()
    home = window._stack.currentWidget()
    assert len(home._cards) == 3
    sidebar = window._nav_btns['Home'].parentWidget()
    for button in window._nav_btns.values():
        assert button.isVisibleTo(window)
        assert sidebar.rect().contains(button.geometry())
    if size == (900, 650):
        assert home._cards_cols == 3


def test_card_is_plain_text_with_one_primary_action_and_a_visible_packs_button(hub):
    window, _ = hub
    home = window._stack.currentWidget()
    commands = home._cards[0]
    # 878: the value slot is removed; the description and packs action remain.
    assert commands._value.text() == "" and not commands._value.isVisibleTo(commands)
    for label in commands.findChildren(QLabel):
        assert '<a' not in label.text().lower()
        assert not label.openExternalLinks()
        assert label.textInteractionFlags() == Qt.TextInteractionFlag.NoTextInteraction
        assert not label.font().underline()
    buttons = commands.findChildren(QPushButton)
    assert [b.text() for b in buttons] == ['Command packs']
    assert buttons[0].isVisibleTo(commands)
    assert buttons[0].width() < commands.width()
    assert 'border:' in buttons[0].styleSheet()
    assert 'Voice help' in home._cards[1]._description.text()



def test_standalone_reference_keeps_its_window_controls(qapp, tmp_path, qt_widget_scope):
    window = command_cheatsheet_qt._CheatSheetWindow(
        lambda phrase: None, lambda: None, tmp_path / 'palette.json', config_cb=lambda: {})
    with qt_widget_scope(window):
        assert window.isWindow()
        assert not window._embedded
        assert not window._title_bar._close_button.isHidden()
        assert not window._title_bar._opacity_slider.isHidden()
