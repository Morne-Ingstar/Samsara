"""Dell first-install fixes, with no app bootstrap, hardware or live settings."""
import ast
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication, QScrollArea

from samsara import __version__
from samsara.ui import first_run_wizard_qt as setup, home_qt, theme, tutorial_qt
from samsara.ui import first_run_pages_qt as pages
from samsara.ui import window_activation
from tests.test_first_run_walkthrough_373 import make_window, visit
from tests.test_home_qt import _FakeSettings, _app, _hub

_record_version = pages.mark_setup_version_seen


class MemorySettings(_FakeSettings):
    def sync(self):
        pass


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    MemorySettings.values = {}
    monkeypatch.setattr(home_qt, 'QSettings', MemorySettings)


def settle(qapp):
    for _ in range(12):
        qapp.processEvents()


def startup_tutorial(app, result):
    """Execute the two real startup seams without importing dictation."""
    source = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8'))
    assignment = next(node for node in ast.walk(source) if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Attribute) and t.attr == '_launch_tutorial_after_wizard'
                              for t in node.targets))
    launch = next(node for node in ast.walk(source) if isinstance(node, ast.If)
                  and '_launch_tutorial_after_wizard' in ast.unparse(node.test))
    code = ast.fix_missing_locations(ast.Module(body=[assignment, launch], type_ignores=[]))
    exec(compile(code, 'startup_tutorial_seams', 'exec'),
         dict(self=app, wizard_result=result, logger=Mock()))


@pytest.mark.parametrize('learn', [False, True])
@pytest.mark.parametrize('already_learned', [False, True])
def test_finish_choice_controls_real_startup_construction(make_window, monkeypatch, learn, already_learned):
    window = make_window()
    app = SimpleNamespace(config={'tutorial_complete': already_learned}, _schedule_ui=lambda fn: fn())
    window._samsara_app = app
    visit(window, 'Complete')
    construct = Mock(return_value=SimpleNamespace(show=Mock(), move=Mock(), width=lambda: 700, height=lambda: 500))
    monkeypatch.setattr(tutorial_qt, 'TutorialWindow', construct)
    surface = Mock()
    monkeypatch.setattr(window_activation, 'surface_requested', surface)
    (window._tutorial_btn if learn else window._next_btn).click()
    assert window.result['first_run_complete'] is True
    startup_tutorial(app, window.result)
    assert construct.call_count == int(learn)
    assert surface.call_count == int(learn)
    if learn:
        assert construct.call_args.kwargs['from_setup'] is True
        surface.assert_called_once_with(app._tutorial_window)


def test_cancelled_setup_does_not_construct_tutorial(monkeypatch):
    construct = Mock()
    monkeypatch.setattr(tutorial_qt, 'TutorialWindow', construct)
    app = SimpleNamespace(config={}, _tutorial_start_from_setup=True, _schedule_ui=lambda fn: fn())
    startup_tutorial(app, None)
    construct.assert_not_called()


def test_learn_requests_show_raise_and_activation(monkeypatch):
    window = SimpleNamespace(show=Mock(), move=Mock(), width=lambda: 700, height=lambda: 500,
                             isMinimized=lambda: False, raise_=Mock(), activateWindow=Mock())
    monkeypatch.setattr(tutorial_qt, 'TutorialWindow', lambda *a, **k: window)
    monkeypatch.setattr(window_activation, '_active_modal', lambda: None)
    monkeypatch.setattr(window_activation, 'confirm_foreground', lambda _: None)
    tutorial_qt.show_tutorial(SimpleNamespace(config={}))
    window.raise_.assert_called_once()
    window.activateWindow.assert_called_once()
    assert window.show.called


def test_setup_learn_waits_until_home_is_queued(qapp, monkeypatch):
    app = SimpleNamespace(config={}, _tutorial_start_from_setup=True, _startup_shell_ready=Event())
    window = SimpleNamespace(show=Mock(), move=Mock(), width=lambda: 700, height=lambda: 500)
    construct = Mock(return_value=window)
    monkeypatch.setattr(tutorial_qt, 'TutorialWindow', construct)
    surface = Mock()
    monkeypatch.setattr(window_activation, 'surface_requested', surface)
    tutorial_qt.show_tutorial(app)
    timer = app._setup_tutorial_timer
    try:
        tutorial_qt.show_tutorial(app)
        assert app._setup_tutorial_timer is timer  # repeated requests coalesce
        timer.timeout.emit()
        construct.assert_not_called()
        app._startup_shell_ready.set()
        timer.timeout.emit()
        construct.assert_called_once()
        surface.assert_called_once_with(window)
        assert app._setup_tutorial_timer is None
        assert app._tutorial_start_from_setup is False
    finally:
        timer.stop()
        timer.deleteLater()


def test_hands_free_text_follows_yes_no_and_current_binding(make_window, qapp):
    window = make_window()
    visit(window, 'Talking')
    decisions = window._decisions
    offer = decisions.hands_free_offer
    for enabled in [False, True, False, True]:
        offer.segments.buttons[offer.combo.findData(enabled)].click()
        settle(qapp)
        text = offer.description.text()
        assert ('Tap Right Ctrl' in text) is enabled
        assert ('Choose Yes above' in text) is not enabled
        assert 'Settings' not in text
        assert offer.description.width() >= window._current_page.viewport().width() - 60
    decisions.hands_free._combo = 'f8'
    decisions.hands_free.bindingChanged.emit('f8')
    assert 'F8' in offer.description.text()
    assert window._config['command_mode']['enabled'] is False  # no save before collect
    window._collect_step()
    assert window._config['command_mode']['enabled'] is True


def test_help_is_named_button_and_expands(make_window):
    window = make_window()
    for button, expanded in window._decisions.details:
        interface = QAccessible.queryAccessibleInterface(button)
        assert interface.role() == QAccessible.Role.Button
        assert button.accessibleName().startswith('Help: ')
        assert button.accessibleName() != '?'
        button.click()
        assert not expanded.isHidden() and expanded.text()
        button.click()
        assert expanded.isHidden()


def test_ready_primary_and_summary_only_include_offered_choices(make_window):
    window = make_window()
    visit(window, 'Complete')
    assert window._next_btn.property('class') == 'primary'
    assert window._next_btn.isDefault()
    assert window._tutorial_btn.property('class') == 'secondary'
    assert not window._tutorial_btn.autoDefault()
    lines = [item.text() for item in window._summary_labels if not item.isHidden()]
    assert len(lines) == 7
    assert 'Wake word: Off' in lines
    assert not any('jarvis' in line.lower() for line in lines)
    assert not any(line.startswith(('Music:', 'Voice:', 'Voice feedback:', 'Listening display:')) for line in lines)
    assert lines.count('Other defaults can be changed in Settings.') == 1
    window._show_optional('Music')
    window._go_next()
    assert any(item.text().startswith('Music:') for item in window._summary_labels)


@pytest.mark.parametrize('palette', ['dark', 'light'])
@pytest.mark.parametrize('checked', [False, True])
def test_ready_checkboxes_contrast_and_native_roles(make_window, qapp, palette, checked):
    old_theme = theme.active_theme()
    theme.set_theme(palette, refresh=False)
    try:
        window = make_window()
        visit(window, 'Complete')
        for checkbox in [window._decisions.launch, window._decisions.include_beta_updates]:
            checkbox.setChecked(checked)
            assert QAccessible.queryAccessibleInterface(checkbox).role() == QAccessible.Role.CheckBox
            assert checkbox.accessibleName() == checkbox.text()
            assert theme.contrast_ratio(theme.TEXT_PRIMARY, theme.BG0) >= 4.5
            assert theme.contrast_ratio(theme.TEXT_PRIMARY, theme.BG1) >= 4.5
            assert theme.contrast_ratio(theme.TEXT_ON_ACCENT, theme.ACCENT) >= 4.5
            assert not checkbox.grab().isNull()  # exercise actual indicator paint
    finally:
        theme.set_theme(old_theme, refresh=False)


@pytest.mark.parametrize('width,height', [(1920, 1040), (1366, 728), (1093, 582)])
def test_tallest_page_reserved_and_footer_fits_all_pages(make_window, qapp, monkeypatch, width, height):
    work = QRect(0, 0, width, height)
    screen = SimpleNamespace(availableGeometry=lambda: work, devicePixelRatio=lambda: 1.0)
    monkeypatch.setattr(setup._WizardWindow, 'screen', lambda self: screen)
    monkeypatch.setattr(QApplication, 'primaryScreen', lambda: screen)
    window = make_window()
    sizes = []
    for name, _ in setup._STEPS + setup._OPTIONAL_STEPS:
        visit(window, name)
        settle(qapp)
        sizes.append(window.height())
        assert work.contains(window.frameGeometry())
        assert isinstance(window._current_page, QScrollArea)
        assert window._current_page.viewport().height() > 40
        for button in [window._back_btn, window._next_btn, window._tutorial_btn]:
            if button.isVisible():
                assert window.rect().contains(QRect(button.mapTo(window, QPoint()), button.size()))
    assert len(set(sizes)) == 1
    assert sizes[0] <= height


def test_setup_completion_suppresses_current_version_but_update_still_shows(make_window, monkeypatch, tmp_path):
    window = make_window()
    monkeypatch.setattr(pages, 'mark_setup_version_seen', _record_version)
    visit(window, 'Complete')
    window._next_btn.click()
    assert MemorySettings.values[home_qt.WHATS_NEW_SEEN_KEY] == __version__
    changelog = tmp_path / 'CHANGELOG.md'
    changelog.write_text(f'## [{__version__}] - 2026-11-01\n\nCurrent release.\n', encoding='utf-8')
    assert home_qt.whats_new_note(MemorySettings.values[home_qt.WHATS_NEW_SEEN_KEY], changelog, current=__version__) is None
    changelog.write_text('## [99.0.0] - 2026-11-02\n\nNew release.\n', encoding='utf-8')
    assert home_qt.whats_new_note(MemorySettings.values[home_qt.WHATS_NEW_SEEN_KEY], changelog, current='99.0.0') == ('99.0.0', 'New release.')


def test_cancel_does_not_mark_version_seen(make_window):
    window = make_window()
    window.close()
    assert home_qt.WHATS_NEW_SEEN_KEY not in MemorySettings.values


def test_dictionary_card_fully_reachable_in_default_real_host(qapp, monkeypatch):
    monkeypatch.setattr(home_qt, 'whats_new_note', lambda *a, **k: None)
    window = _hub(qapp, width=900, height=650, app=_app(model_loaded=True))
    try:
        window.setFixedSize(900, 650)
        window.apply_theme()
        settle(qapp)
        home = window._panel_cache['Home']
        bar = home._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        settle(qapp)
        card = home.cards[-1]
        viewport = home._scroll.viewport()
        assert viewport.rect().contains(QRect(card.mapTo(viewport, QPoint()), card.size()))
    finally:
        window.close()
