"""939: offline explanations inside actual hosts; no microphone or real browser."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton, QWidget
from shiboken6 import delete

from samsara import help_terms, command_stats
from samsara.ui import context_help_qt as help_ui, tutorial_qt, theme
from tests._theme_stub_app import StubApp


def settle(qapp):
    for _ in range(8):
        qapp.processEvents()


@pytest.fixture(params=['dark', 'light'])
def tutorial(qapp, monkeypatch, request):
    previous = theme.active_theme()
    theme.set_theme(request.param, refresh=False)
    monkeypatch.setattr(tutorial_qt, '_command_count', lambda _: 2)
    monkeypatch.setattr(tutorial_qt, '_tutorial_catalog', lambda _: None)
    window = tutorial_qt.TutorialWindow(StubApp({'ui': {'live_surface': {'enabled': True}},
        'command_mode': {'enabled': True, 'mode': 'toggle'}}))
    calls = []
    window._context_help.browser = lambda url: calls.append(url) or False
    window.setStyleSheet(theme.build_stylesheet())
    window.show()
    settle(qapp)
    yield window, calls
    window.close()
    window.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    theme.set_theme(previous, refresh=False)


def show_page(window, key, qapp):
    # 940: help cards on retained lessons use the actual optional entry route.
    if key in window._optional_steps:
        window._open_optional(key)
    else:
        window._optional_key = None
        window._step = next(i for i, (k, _) in enumerate(window._steps) if k == key)
        window._show_step()
    settle(qapp)


def test_six_packaged_terms_and_unknown_never_becomes_a_url():
    assert set(help_terms.TERMS) == {'command-reference', 'wake-word', 'hands-free',
                                   'listening-display', 'command-packs', 'ava'}
    assert help_terms.guide_url('https://example.test/') is None
    missing = help_terms.explanation('missing')
    assert missing.body == 'This explanation is unavailable in this version.'
    assert missing.guide is None
    for term_id in help_terms.TERMS:
        assert help_terms.guide_url(term_id).startswith('https://morneis.com/samsara/docs/')


def test_malformed_packaged_guide_is_hidden(monkeypatch):
    term = help_terms.TERMS['ava']
    monkeypatch.setitem(help_terms.TERMS, 'ava', replace(term, guide='https://example.test/?secret=1'))
    assert help_terms.explanation('ava').guide is None


@pytest.mark.parametrize('mode,opens,expected', [('toggle', True, 'start a hands-free session'),
    ('hold', True, 'listen for a command'), ('toggle', False, 'listen for a command')])
def test_wake_uses_current_config_without_mutation(mode, opens, expected):
    config = {'command_mode': {'mode': mode}, 'wake_word_config': {'phrase': 'Jarvis', 'opens_session': opens}}
    saved = deepcopy(config)
    text = help_terms.explanation('wake-word', config).body
    assert expected in text and 'calibration' in text and 'Jarvis' in text
    assert config == saved


def test_keyboard_card_focus_close_tab_and_browser_failure(tutorial, qapp):
    window, calls = tutorial
    show_page(window, 'command', qapp)
    origin = window._current_page.findChildren(help_ui.HelpButton)[0]
    origin.setFocus()
    assert window._context_help.active is None and calls == []
    QTest.keyClick(origin, Qt.Key.Key_Return)
    settle(qapp)
    card = window._context_help.active
    assert card.close_button.hasFocus()
    assert card.body.text() == help_terms.TERMS['command-reference'].body
    assert card.accessibleDescription() == card.body.text()
    assert card.close_button.height() >= 44
    assert card.guide_button.width() < card.width()
    assert not card.body.font().underline()
    assert calls == []
    QTest.keyClick(card.close_button, Qt.Key.Key_Tab)
    assert card.guide_button.hasFocus()
    QTest.keyClick(card.guide_button, Qt.Key.Key_Backtab)
    assert card.close_button.hasFocus()
    QTest.keyClick(card.close_button, Qt.Key.Key_Tab)
    QTest.keyClick(card.guide_button, Qt.Key.Key_Return)
    assert calls == [help_terms.guide_url('command-reference')]
    assert 'Could not open' in card.status.text() and card.isVisible()
    assert card.guide_button.hasFocus()
    QTest.keyClick(card.guide_button, Qt.Key.Key_Escape)
    assert window._context_help.active is None and origin.hasFocus()
    assert not window._context_help._installed


def test_replacement_toggle_and_page_leave_clear_card(tutorial, qapp):
    window, calls = tutorial
    show_page(window, 'command', qapp)
    buttons = [b for b in window._current_page.findChildren(help_ui.HelpButton) if b.text().endswith(' ?')]
    buttons[0].click()
    old = window._context_help.active
    buttons[1].click()
    assert not old.isVisible() and not buttons[0].isChecked()
    assert window._context_help.active.term.id == 'wake-word'
    buttons[1].click()
    assert window._context_help.active is None and buttons[1].hasFocus()
    buttons[0].click()
    show_page(window, 'done', qapp)
    assert window._context_help.active is None and not window._context_help._installed
    assert calls == []


def test_ways_reuses_one_explanation_and_current_session_words(tutorial, qapp):
    window, _ = tutorial
    window._app.config['command_mode'].update(dictate_commit_word='banana', abort_phrases=['leave now'])
    show_page(window, 'ways', qapp)
    origin, card = window._way_details['hands_free']
    origin.click()
    settle(qapp)
    assert card.isVisible() and "'banana'" in card.text() and "'leave now'" in card.text()
    assert sum(detail.isVisible() for _, detail in window._way_details.values()) == 1
    assert card.close_button.hasFocus()
    window._way_details['main'][0].click()
    assert window._context_help.active is None and not card.isVisible()
    window._app.config['hands_free'] = {'send_mode': 'as_i_speak'}
    origin.click()
    assert 'Hands-free sends: As I speak.' in card.text()
    assert "'banana' to paste" not in card.text()


def test_exception_browser_handoff_success_and_host_hide(tutorial, qapp):
    window, calls = tutorial
    show_page(window, 'display', qapp)
    origin = next(b for b in window._current_page.findChildren(help_ui.HelpButton) if b.text().endswith(' ?'))
    origin.click()
    card = window._context_help.active
    def rejected(_):
        raise OSError('browser missing')
    window._context_help.browser = rejected
    card.guide_button.click()
    assert 'Could not open' in card.status.text() and card.isVisible()
    window._context_help.browser = lambda _: True
    card.guide_button.click()
    assert card.status.text() == 'Opened in your browser. This explanation stays here.'
    window.hide()
    assert window._context_help.active is None and not window._context_help._installed


def test_deleted_origin_and_unknown_card_fallback(tutorial, qapp):
    window, calls = tutorial
    show_page(window, 'command', qapp)
    page = window._current_page.widget()
    origin = help_ui.term_button('missing')
    page.layout().insertWidget(0, origin)
    card = help_ui.ExplanationCard(window._context_help)
    page.layout().insertWidget(1, card)
    window._context_help.toggle('missing', origin, card, page=page)
    assert card.guide_button.isHidden()
    assert 'unavailable' in card.body.text()
    delete(origin)
    card.close_button.click()
    assert window._skip_step_btn.hasFocus() and calls == []


@pytest.fixture
def reference(qapp, monkeypatch, tmp_path):
    from samsara.ui import command_cheatsheet_qt as cs
    from samsara.ui.main_window_qt import _MainWindow
    rows = [dict(phrase=f'command {i}', canonical_id=f'fixture.{i}', aliases=[],
        description='Ask Ava for help.' if i == 0 else 'A useful description.',
        examples=[], plugin='builtin', pack='core', args=[]) for i in range(30)]
    monkeypatch.setattr(cs, 'catalog_rows', lambda _: rows)
    monkeypatch.setattr(cs, '_scope_context', lambda: None)
    monkeypatch.setattr(cs, '_disabled_packs', lambda *a: set())
    monkeypatch.setattr(command_stats, 'get_top_commands', lambda _: [])
    monkeypatch.setattr(_MainWindow, '_restore_geometry', lambda _: None)
    monkeypatch.setattr(_MainWindow, '_save_geometry', lambda _: None)
    monkeypatch.setattr(_MainWindow, '_activate', lambda *a: None)
    fake = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}},
        config_path=tmp_path / 'config.json', available_mics=[], update_config_and_save=lambda _: True)
    host = _MainWindow(fake)
    host._poll_timer.stop()
    host._header_mark.stop()
    executed, packs, browser = [], [], []
    page = cs._CheatSheetWindow(executed.append, lambda: [], tmp_path / 'palette.json',
        embedded=True, config_cb=lambda: {}, open_packs_cb=lambda: packs.append(True))
    page._context_help.browser = lambda url: browser.append(url) or False
    host._stack.addWidget(page)
    host._stack.setCurrentWidget(page)
    host.resize(1100, 820)
    host.apply_theme()
    host.show()
    settle(qapp)
    yield host, page, executed, packs, browser
    host.hide()
    host.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_reference_help_preserves_filter_selection_expansion_and_pack_action(reference, qapp):
    host, page, executed, packs, browser = reference
    page._filter.setText('command')
    page._list.setCurrentRow(0)
    page._on_item_clicked(page._list.currentItem())
    settle(qapp)
    assert page._detail_card.phrase == 'command 0'
    assert page._help_ava.isVisible()
    # A pointer activation used to dismiss any command disclosure outside its row.
    QTest.mouseClick(page._help_command_reference, Qt.MouseButton.LeftButton)
    settle(qapp)
    assert page._filter.text() == 'command' and page._list.currentRow() == 0
    assert page._detail_card.phrase == 'command 0'
    assert page._help_card.term.id == 'command-reference'
    assert page._help_region.isVisible()
    page._help_command_packs.click()
    assert page._help_card.term.id == 'command-packs' and packs == []
    page._help_card.close_button.click()
    assert page._help_command_packs.hasFocus()
    assert page._list.horizontalScrollBar().maximum() == 0
    page._title_bar._packs_button.click()
    assert packs == [True] and executed == [] and browser == []


def test_ava_help_only_for_matching_command_and_first_escape_keeps_detail(reference, qapp):
    _, page, executed, packs, browser = reference
    page._on_item_clicked(page._list.item(0))
    page._help_ava.click()
    settle(qapp)
    assert page._help_card.term.id == 'ava'
    QTest.keyClick(page._help_card.close_button, Qt.Key.Key_Escape)
    assert page._context_help.active is None and page._detail_card.isVisible()
    assert page._help_ava.hasFocus()
    page._on_item_clicked(page._list.item(1))
    assert page._help_ava.isHidden()
    page._help_command_reference.click()
    page.hide()
    assert page._context_help.active is None and not page._context_help._installed
    assert executed == [] and packs == [] and browser == []
