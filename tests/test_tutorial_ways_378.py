"""Queue 378: config-backed passive capture overview; no running app needed."""
import re
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QLabel, QScrollArea, QWidget

from samsara.ui import home_qt, quick_reference_qt as reference, tutorial_qt
from samsara import session_modes
from samsara.onboarding.mode_details import MODE_DETAILS


def enlarge_text(window, scale=1.5):
    """Enlarge actual text at the existing width, rather than scaling a PNG."""
    for widget in [window, *window.findChildren(QWidget)]:
        sheet = getattr(widget, '_378_text_sheet', widget.styleSheet())
        widget._378_text_sheet = sheet
        if sheet:
            widget.setStyleSheet(re.sub(
                r'(font-size\s*:\s*)(\d+(?:\.\d+)?)px',
                lambda match: f'{match[1]}{round(float(match[2]) * scale)}px', sheet,
            ))
    window._step_indicator.set_ui_scale(scale)


@pytest.fixture
def build(qapp):
    windows = []

    def create(config=None):
        window = tutorial_qt.TutorialWindow(SimpleNamespace(config=config or {}))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window._step = 3
        window._show_step()
        window.show()
        qapp.processEvents()
        windows.append(window)
        return window

    yield create
    for window in windows:
        window.close()
        window.deleteLater()
    qapp.processEvents()


def row_text(window, key):
    frame = window._current_page.findChild(QWidget, f'tutorialWay_{key}')
    assert frame is not None
    return '\n'.join(label.text() for label in frame.findChildren(QLabel))


def test_five_steps_and_passive_navigation(build, qapp):
    window = build()
    assert [key for key, _ in window._steps] == ['welcome', 'dictation', 'command', 'ways', 'display', 'done']
    interface = QAccessible.queryAccessibleInterface(window._step_indicator)
    assert interface.childCount() == 6
    assert window._next_btn.isEnabled() and window._back_btn.isEnabled()
    assert not getattr(window._app, '_tutorial_hooks', {})
    assert window._hint_timer is None
    window._go_back()
    assert window._steps[window._step][0] == 'command'
    assert 'command' in window._app._tutorial_hooks
    window._skip_step()
    assert window._steps[window._step][0] == 'ways'
    assert window._next_btn.isEnabled() and not window._app._tutorial_hooks
    window._go_next()
    assert window._steps[window._step][0] == 'display'
    window._go_next()
    assert window._steps[window._step][0] == 'done'
    window._go_back()
    assert window._steps[window._step][0] == 'display'
    window._go_back()
    assert window._next_btn.isEnabled() and window._step_lbl.text() == 'Step 4 of 6'


def test_defaults_match_shared_controls(build):
    window = build()
    for row, key in [('main', 'hotkey'), ('continuous', 'continuous_hotkey'),
                     ('commands', 'command_hotkey'), ('wake', 'wake_word_hotkey')]:
        assert reference._pretty_key_combo(reference._HOTKEY_FALLBACKS[key]) in row_text(window, row)
    shared = reference._resolve_session_phrases(window._app)
    assert shared['hands_free_toggle']['button'] in row_text(window, 'hands_free')
    assert f'"{session_modes.DEFAULT_DICTATE_COMMIT_PHRASE}"' in row_text(window, 'hands_free')
    assert f'"{home_qt.wake_phrase({})}"' in row_text(window, 'wake')
    assert [row['key'] for row in tutorial_qt._ways_rows(window._app)] == [
        'main', 'continuous', 'commands', 'wake', 'hands_free',
    ]


@pytest.mark.parametrize('row,key', [
    ('main', 'hotkey'), ('continuous', 'continuous_hotkey'),
    ('commands', 'command_hotkey'), ('wake', 'wake_word_hotkey'),
])
def test_each_custom_key_replaces_default(build, row, key):
    window = build({key: 'ctrl+alt+shift+f12'})
    text = row_text(window, row)
    assert reference._pretty_key_combo('ctrl+alt+shift+f12') in text
    assert reference._pretty_key_combo(reference._HOTKEY_FALLBACKS[key]) not in text


def test_custom_button_and_phrase_replace_defaults(build):
    window = build({'command_mode': {'enabled': True, 'button': 'mouse5', 'mode': 'toggle'},
                    'wake_word_config': {'phrase': 'hello custom wake'}})
    shared = reference._resolve_session_phrases(window._app)
    assert reference._pretty_button('mouse5') in row_text(window, 'hands_free')
    assert reference._resolve_session_phrases(SimpleNamespace(config={}))['hands_free_toggle']['button'] not in row_text(window, 'hands_free')
    assert '"hello custom wake"' in row_text(window, 'wake')
    assert f'"{home_qt.wake_phrase({})}"' not in row_text(window, 'wake')
    assert shared['hands_free_toggle']['mode'] == 'toggle'


@pytest.mark.parametrize('mode,name,action', [
    ('hold', 'Hold to talk', 'Hold'), ('toggle', 'Tap to talk', 'Tap'),
    ('continuous', 'Continuous on the main key', 'Tap'),
])
def test_main_mode_wording(build, mode, name, action):
    text = row_text(build({'mode': mode}), 'main')
    assert text.startswith(name + '\n' + action)


@pytest.mark.parametrize('mode,action', [('hold', 'Hold'), ('toggle', 'Tap')])
def test_session_mode_wording(build, mode, action):
    text = row_text(build({'command_mode': {'enabled': True, 'mode': mode}}), 'hands_free')
    assert text.startswith('Hands-free\n' + action)


@pytest.mark.parametrize('row,config', [
    ('wake', {'wake_word_enabled': False}),
    ('hands_free', {'command_mode': {'enabled': False}}),
])
def test_disabled_rows_present_and_name_current_settings_page(build, row, config):
    text = row_text(build(config), row)
    assert 'Off' in text and 'Settings -> Dictation' in text


def test_enabled_rows_omit_off_instruction(build):
    window = build({'wake_word_enabled': True, 'command_mode': {'enabled': True}})
    for row in ('wake', 'hands_free'):
        assert 'Off' not in row_text(window, row)
        assert 'turn it on' not in row_text(window, row)


def test_resolved_values_are_used_without_another_key_table(monkeypatch):
    app = SimpleNamespace(config={})
    controls = reference._resolve_hotkeys(app)
    for index, row in enumerate(controls):
        row['value'] = f'RESOLVED_{index}'
    monkeypatch.setattr(reference, '_resolve_hotkeys', lambda _app: controls)
    rows = tutorial_qt._ways_rows(app)
    assert [f'RESOLVED_{i}' in row['start'] for i, row in zip((0, 1, 3, 2, 5), rows)] == [True] * 5


@pytest.mark.parametrize('scale', [1.0, 1.5])
def test_text_fits_at_fixed_width_and_scrolls_when_height_limited(build, qapp, scale):
    window = build({'hotkey': 'ctrl+alt+shift+f12'})
    if scale != 1:
        enlarge_text(window, scale)
    window.setMaximumHeight(722)
    window.resize(620, 722)
    qapp.processEvents()
    page = window._current_page
    assert isinstance(page, QScrollArea)
    assert page.widget().width() <= page.viewport().width()
    for label in page.widget().findChildren(QLabel):
        if not label.isVisible():
            continue
        needed = label.heightForWidth(label.width()) if label.hasHeightForWidth() else label.fontMetrics().height()
        assert label.height() >= needed, label.text()
        assert max(label.fontMetrics().horizontalAdvance(word) for word in label.text().split()) <= label.contentsRect().width(), label.text()
    if scale == 1.5:
        detail = page.findChild(QWidget, 'tutorialWay_main').findChildren(QLabel)[1]
        assert detail.font().pixelSize() == 24
        assert page.verticalScrollBar().maximum() > 0
    page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
    qapp.processEvents()
    assert page.findChild(QWidget, 'tutorialWay_hands_free').isVisible()


@pytest.mark.parametrize('word', ['banana', '', 'two words'])
def test_commit_word_matches_accepted_config(build, monkeypatch, word):
    # Invalid updates keep the current runtime word, even when it was customized.
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', 'retained')
    config = {'command_mode': {'enabled': True, 'dictate_commit_word': word}}
    expected = word if word == 'banana' else 'retained'
    window = build(config)
    assert f'"{expected}"' in row_text(window, 'hands_free')
    assert f'"{expected}"' in tutorial_qt._hands_free_text(config)
    assert session_modes.DEFAULT_DICTATE_COMMIT_PHRASE not in row_text(window, 'hands_free')
    # Done deliberately preserves recovery-only copy, with no redundant paste word.
    window._go_next()  # Ways -> Display
    window._go_next()  # Display -> Done
    assert 'scratch that' in '\n'.join(l.text() for l in window._current_page.findChildren(QLabel))


def test_each_detail_button_accessibility_and_exclusive_shared_copy(build, qapp):
    window = build({'command_mode': {'enabled': True, 'mode': 'toggle'}})
    assert set(MODE_DETAILS) == {'hold', 'tap', 'continuous', 'commands_only', 'wake_word', 'hands_free'}
    for row in tutorial_qt._ways_rows(window._app):
        frame = window._current_page.findChild(QWidget, f'tutorialWay_{row["key"]}')
        from PySide6.QtWidgets import QPushButton
        buttons = frame.findChildren(QPushButton)
        assert len(buttons) == 1
        button, detail = window._way_details[row['key']]
        assert button.text() == '?' and button.accessibleName() == f'More about {row["name"]}'
        assert button.width() >= 44 and button.height() >= 44
        assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
        button.click()
        qapp.processEvents()
        assert detail.isVisible() and button.isChecked()
        assert sum(label.isVisible() for _, label in window._way_details.values()) == 1
    assert window._way_details['main'][1].text() == MODE_DETAILS['hold']
    window._way_details['hands_free'][0].click()
    assert not any(label.isVisible() for _, label in window._way_details.values())


def test_keyboard_enter_space_and_escape(build, qapp):
    from PySide6.QtTest import QTest
    window = build()
    button, detail = window._way_details['main']
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Return)
    assert detail.isVisible()
    QTest.keyClick(button, Qt.Key.Key_Space)
    assert not detail.isVisible()
    QTest.keyClick(button, Qt.Key.Key_Space)
    QTest.keyClick(button, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert not detail.isVisible() and not button.isChecked()


@pytest.mark.parametrize('scale', [1.0, 1.5])
def test_open_detail_scrolls_and_fits(build, qapp, scale):
    window = build({'command_mode': {'enabled': True, 'mode': 'toggle'}})
    window._way_details['hands_free'][0].click()
    enlarge_text(window, scale)
    window.setMaximumHeight(722)
    window.resize(620, 722)
    qapp.processEvents()
    page = window._current_page
    for label in page.widget().findChildren(QLabel):
        if label.isVisible():
            needed = label.heightForWidth(label.width()) if label.hasHeightForWidth() else label.fontMetrics().height()
            assert label.height() >= needed, label.text()
            assert max(label.fontMetrics().horizontalAdvance(w) for w in label.text().split()) <= label.contentsRect().width()
    assert page.verticalScrollBar().maximum() > 0
    assert page.horizontalScrollBar().maximum() == 0


def test_detail_follows_timing_catalog_ava_and_exit_config(build):
    window = build({'silence_threshold': 3, 'accessibility': {'speech_pace': 'relaxed'},
                    'ava_command_session': {'interpret_misses': True},
                    'command_mode': {'mode': 'toggle', 'dictate_commit_word': 'banana',
                                     'abort_phrases': ['leave now']}})
    assert '7.5 seconds' in window._way_details['continuous'][1].text()
    assert "'banana'" in window._way_details['hands_free'][1].text()
    assert "'leave now'" in window._way_details['hands_free'][1].text()
    assert 'Ava can work out' in window._way_details['commands'][1].text()
    phrase = tutorial_qt._catalog_phrase(window._catalog, tutorial_qt._HELP_COMMAND_ID)
    assert phrase in window._way_details['commands'][1].text()
    default = build()
    assert 'about two seconds' in default._way_details['continuous'][1].text()
    assert 'Ava can work out' not in default._way_details['commands'][1].text()
    manual = build({'mode': 'toggle', 'continuous_commit_trigger': 'key'})
    assert 'Each time you pause' not in manual._way_details['continuous'][1].text()
    assert 'commit key' in manual._way_details['continuous'][1].text()
    assert manual._way_details['main'][1].text() == MODE_DETAILS['tap']


def test_compact_closed_cards_and_centered_targets(build, qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QFrame, QPushButton
    window = build({'wake_word_enabled': True, 'command_mode': {'enabled': True, 'mode': 'toggle'}})
    for _ in range(8):
        qapp.processEvents()
    page = window._current_page
    assert window.height() <= window._default_window_height
    assert page.verticalScrollBar().maximum() == 0
    for key, (button, detail) in window._way_details.items():
        frame = page.findChild(QFrame, f'tutorialWay_{key}')
        assert frame.height() <= 92 + 2  # Brief's measured-outcome tolerance.
        assert (button.width(), button.height()) == (44, 44)
        position = button.mapTo(frame, QPoint())
        assert abs(position.y() + button.height()/2 - frame.height()/2) <= 2
        assert frame.findChildren(QPushButton) == [button]
        assert frame.layout().contentsMargins().top() == 12
        assert frame.layout().contentsMargins().bottom() == 12
        column = frame.layout().itemAt(0).layout().itemAt(0).layout()
        assert column.spacing() == 4
        closed = frame.height()
        button.click()
        for _ in range(8):
            qapp.processEvents()
        assert abs(frame.height() - closed - detail.height() - column.spacing()) <= 2
        button.click()
    window._step = 1
    window._show_step()
    qapp.processEvents()
    assert window.height() == window._default_window_height


def test_shared_strip_has_twelve_pixel_gap_on_every_page(build, qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QFrame
    window = build()
    indicator = window._step_indicator
    for index in range(len(window._steps)):
        window._step = index
        window._show_step()
        qapp.processEvents()
        root = window.centralWidget().layout()
        strip_index = next(i for i in range(root.count())
                           if root.itemAt(i).widget() is indicator)
        separator = root.itemAt(strip_index + 2).widget()
        rule = separator.findChild(QFrame)
        rule_top = rule.mapTo(window, QPoint()).y()
        strip_bottom = indicator.mapTo(window, QPoint()).y() + indicator.height()
        assert rule_top - strip_bottom == 12
