"""Queue 259: the tutorial's final recap is readable and its guide actions fit."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea

from samsara.ui import theme
from samsara.ui import tutorial_qt
from samsara.ui.tutorial_qt import TutorialWindow
from tests._theme_stub_app import StubApp


def test_done_page_keeps_body_text_and_open_targets_unclipped(qapp, qt_widget_scope):
    previous = theme.active_theme()
    theme.set_theme("dark", refresh=False)
    try:
        with qt_widget_scope(TutorialWindow(StubApp())) as tutorial:
            tutorial._step = next(i for i, (key, _title) in enumerate(tutorial._steps) if key == "done")
            tutorial._show_step()
            tutorial.show()
            # A fixed number of short slices settles queued screen-fit/layout work.
            # Never spin until the global application queue becomes empty.
            for _ in range(3):
                qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
            page = tutorial._current_page
            assert isinstance(page, QScrollArea)
            content = page.widget()
            assert content is not None

            clipped = []
            for label in content.findChildren(QLabel):
                if not label.text().strip():
                    continue
                needed = (label.heightForWidth(label.width()) if label.hasHeightForWidth()
                          else label.fontMetrics().height())
                if label.height() < needed:
                    clipped.append((label.text(), label.height(), needed))
            assert not clipped, f"tutorial finish labels clipped to less than one line: {clipped}"

            body_fragments = (
                # Queue 315 already replaced the readiness promise with observed Settings state.
                # 934: plain command-key and Ava wording replace the F9 jargon.
                "Dictated text", "Ran a command", "Hold ", "Manage Ava",
                "Hands-free", "Fine-tune your microphone", "Install Ollama",
            )
            body_labels = [
                label for label in content.findChildren(QLabel)
                if any(fragment in label.text() for fragment in body_fragments)
            ]
            assert len(body_labels) == len(body_fragments)
            assert all(label.font().pixelSize() == theme.TYPE_BODY for label in body_labels)

            open_buttons = [
                button for button in content.findChildren(QPushButton)
                if button.text() == "Open →"
            ]
            assert len(open_buttons) == 2  # 473: only microphone and Ava guides remain.
            assert all(button.minimumHeight() >= theme.HIT_TARGET_MIN for button in open_buttons)
            assert all(button.height() >= theme.HIT_TARGET_MIN for button in open_buttons)

            assert content.height() >= content.layout().minimumSize().height()
    finally:
        theme.set_theme(previous, refresh=False)


_KEPT = {'level': 'Kept: Brief', 'voice': 'Voice kept: Network, 1×, 80%',
         'pace': 'Pace kept: Standard'}


def _settle(qapp):
    for _ in range(8):
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)


@pytest.mark.parametrize('scale', [1, 1.25, 1.5, 2])
@pytest.mark.parametrize('state', ['none', 'level', 'voice', 'pace', 'all',
                                   'incomplete', 'skipped', 'long_keys'])
def test_done_recap_keeps_incoming_width_and_truthful_states(
        qapp, qt_widget_scope, monkeypatch, scale, state):
    # 934: reproduce F9 at a compact width as well as the existing Try width.
    monkeypatch.setattr(tutorial_qt, '_command_count', lambda app: 562)
    config = {'undo_hotkey': 'ctrl+alt+shift+backspace',
              'cancel_hotkey': 'ctrl+alt+shift+escape',
              'command_hotkey': 'ctrl+alt+shift+page_down'} if state == 'long_keys' else {}
    with qt_widget_scope(TutorialWindow(StubApp(config))) as tutorial:
        area = QRect(0, 0, round(1366 / scale), round(728 / scale))
        screen = SimpleNamespace(availableGeometry=lambda: area)
        monkeypatch.setattr(tutorial, 'screen', lambda: screen)
        completed = {1} if state == 'incomplete' else (set() if state in ('none', 'skipped') else {1, 2})
        tutorial._completed = completed
        tutorial._skipped_steps = {1, 2} if state == 'skipped' else set()
        kept = dict(_KEPT) if state in ('all', 'long_keys') else ({state: _KEPT[state]} if state in _KEPT else {})
        tutorial._try_page.kept = kept
        tutorial.show()
        for width in (620, 1000):
            tutorial._step = 4
            tutorial._show_step()
            tutorial.resize(min(width, tutorial.maximumWidth()), tutorial.height())
            tutorial._fit_available_screen()
            _settle(qapp)
            incoming_width = tutorial.width()
            tutorial._step = len(tutorial._steps) - 1
            tutorial._show_step()
            _settle(qapp)
            assert tutorial.width() == incoming_width
            assert area.contains(tutorial.frameGeometry())
            page = tutorial._current_page
            content = page.widget()
            assert content.width() <= page.viewport().width()
            assert page.horizontalScrollBar().maximum() == 0
            summary = tutorial._try_summary
            if kept:
                assert summary.isVisible()
                for value in kept.values():
                    assert value.split(': ', 1)[1] in summary.text()
                # The saved settings occupy the same card below the exercises.
                summary_rect = QRect(summary.mapTo(content, QPoint()), summary.size())
                for icon, label in tutorial._done_checks.values():
                    assert summary_rect.top() > label.mapTo(content, QPoint()).y() + label.height() - 1
            else:
                assert not summary.isVisible()
            for step, (icon, label) in tutorial._done_checks.items():
                assert icon.text() == ('✓' if step in completed else '—')
                expected = 'Completed' if step in completed else ('Skipped' if state == 'skipped' else 'Not completed')
                assert icon.accessibleName() == expected
            surfaces = [w for w in content.findChildren(QLabel) + content.findChildren(QPushButton)
                        if w.isVisible() and w.text()]
            for widget in surfaces:
                rect = QRect(widget.mapTo(content, QPoint()), widget.size())
                assert content.rect().contains(rect), widget.text()
                if isinstance(widget, QLabel):
                    needed = widget.heightForWidth(widget.width()) if widget.hasHeightForWidth() else widget.fontMetrics().height()
                    assert widget.height() >= needed, widget.text()
            for index, first in enumerate(surfaces):
                first_rect = QRect(first.mapTo(content, QPoint()), first.size())
                for second in surfaces[index + 1:]:
                    assert not first_rect.intersects(QRect(second.mapTo(content, QPoint()), second.size())), (first.text(), second.text())


def test_done_guide_and_support_actions_are_keyboard_reachable_with_scroll(
        qapp, qt_widget_scope, monkeypatch):
    # 934: Tab must bring each action into the smallest bounded work area.
    opened = []
    monkeypatch.setattr(tutorial_qt, '_command_count', lambda app: 562)
    monkeypatch.setattr(tutorial_qt, 'open_support_tab', lambda app: opened.append('support'))
    with qt_widget_scope(TutorialWindow(StubApp())) as tutorial:
        screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 683, 364))
        monkeypatch.setattr(tutorial, 'screen', lambda: screen)
        monkeypatch.setattr(tutorial, '_open_guide', opened.append)
        tutorial._try_page.kept = dict(_KEPT)
        tutorial._step = len(tutorial._steps) - 1
        tutorial._show_step()
        tutorial.show()
        _settle(qapp)
        page = tutorial._current_page
        assert page.verticalScrollBar().maximum() > 0
        actions = [tutorial._support_btn] + [b for b in page.findChildren(QPushButton) if b.text() == 'Open →']
        for target in actions:
            tutorial._back_btn.setFocus()
            _settle(qapp)
            for _ in range(20):
                QTest.keyClick(qapp.focusWidget() or tutorial, Qt.Key.Key_Tab)
                _settle(qapp)
                if qapp.focusWidget() is target:
                    break
            assert qapp.focusWidget() is target, target.text()
            assert page.viewport().rect().contains(QRect(target.mapTo(page.viewport(), QPoint()), target.size()))
            QTest.keyClick(target, Qt.Key.Key_Space)
        assert opened == ['support', 'open_mic_setup_guide', 'open_ava_guide']


@pytest.mark.parametrize('enabled', [False, True])
def test_done_only_suggests_commands_in_enabled_packs(qapp, qt_widget_scope, monkeypatch, enabled):
    # 934: the recap must not invite a command disabled in Settings.
    records = [{'canonical_id': tutorial_qt._NUMBERS_COMMAND_ID,
                'phrase': 'number these controls', 'pack': 'accessibility'}]
    monkeypatch.setattr(tutorial_qt, '_tutorial_catalog', lambda app: records)
    monkeypatch.setattr(tutorial_qt, '_command_count', lambda app: 562)
    with qt_widget_scope(TutorialWindow(StubApp({'command_packs': {'accessibility': enabled}}))) as tutorial:
        tutorial._step = len(tutorial._steps) - 1
        tutorial._show_step()
        text = ' '.join(label.text() for label in tutorial._current_page.findChildren(QLabel))
        assert ('"number these controls"' in text) is enabled
