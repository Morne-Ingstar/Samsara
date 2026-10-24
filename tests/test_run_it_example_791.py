"""791: Run it uses the reviewed example for commands that need words."""
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QRect
from PySide6.QtCore import QCoreApplication, QEvent


def _open_card(qapp, monkeypatch, tmp_path, command):
    from samsara.ui import command_cheatsheet_qt as sheet

    monkeypatch.setattr(sheet, 'catalog_rows', lambda _commands: [command])
    monkeypatch.setattr(sheet, '_scope_context', lambda: None)
    monkeypatch.setattr(sheet._CheatSheetWindow, '_foreground_screen', lambda _self: None)
    execute = Mock()
    win = sheet._CheatSheetWindow(
        execute, lambda: [], tmp_path / 'palette.json',
        config_cb=lambda: {'command_packs': {'hermes': True}},
    )
    win.resize(640, 720)
    win.show()
    qapp.processEvents()
    win._detail_card.open(command, win._list, QRect(0, 60, 300, 44))
    qapp.processEvents()
    return win, execute


def _close_window(qapp, win):
    win.hide()
    win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.fixture
def example_command():
    return dict(phrase='ask hermes', canonical_id='hermes.ask', kind='plugin', plugin='hermes',
                aliases=[], shown_aliases=[], description='Ask Hermes a question.',
                args=[dict(name='question', type='string', required=True)],
                examples=['ask hermes explain how rainbows form'], pack='hermes',
                risk='safe', whole_utterance=False)


def test_run_it_executes_first_reviewed_example(qapp, monkeypatch, tmp_path, example_command):
    win, execute = _open_card(qapp, monkeypatch, tmp_path, example_command)
    try:
        button = win._detail_card.run_button
        assert button.isVisible()
        assert button.text() == 'Run it'
        assert button.accessibleName() == 'Run it: ask hermes explain how rainbows form'
        button.click()
        assert execute.call_args.args[0] == 'ask hermes explain how rainbows form'
    finally:
        _close_window(qapp, win)


def test_argument_command_without_example_hides_run_it(qapp, monkeypatch, tmp_path, example_command):
    example_command['examples'] = []
    win, execute = _open_card(qapp, monkeypatch, tmp_path, example_command)
    try:
        assert not win._detail_card.run_button.isVisible()
        assert execute.call_count == 0
    finally:
        _close_window(qapp, win)


def test_plain_command_still_runs_its_phrase(qapp, monkeypatch, tmp_path, example_command):
    example_command.update(args=[], examples=[], pack='core')
    win, execute = _open_card(qapp, monkeypatch, tmp_path, example_command)
    try:
        button = win._detail_card.run_button
        assert button.isVisible()
        assert button.accessibleName() == 'Run it: ask hermes'
        button.click()
        assert execute.call_args.args[0] == 'ask hermes'
    finally:
        _close_window(qapp, win)
