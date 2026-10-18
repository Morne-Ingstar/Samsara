"""Real control pixels: app borders suppress native rectangles; other types keep HEAD focus."""
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAccessible, QPainterPath
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QLineEdit, QMessageBox, QPushButton, QSlider,
    QSpinBox, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)
from samsara.ui import home_qt, theme
from samsara.ui.focus_ring import install, keyboard_stylesheet


KINDS = ('home', 'back', 'next', 'combo', 'spin', 'edit', 'search',
         'message', 'tabs', 'unchecked', 'checked', 'slider')
# HEAD native indications for these types occupy the interior by design.
NATIVE = {'message', 'tabs', 'unchecked', 'checked', 'slider'}


def settle(app):
    for _ in range(8):
        app.processEvents()


def make_control(kind, window):
    if kind == 'home':
        return home_qt._CapabilityCard('Guides & tutorials', 'Tutorial', 'Learn the controls')
    if kind in ('back', 'next'):
        from samsara.ui.tutorial_qt import TutorialWindow
        from tests.test_home_qt import _app
        tutorial = TutorialWindow(_app())
        tutorial._install_hook = lambda *args: None
        tutorial._step = 3
        tutorial._show_step()
        tutorial.setParent(window)
        tutorial.setWindowFlags(Qt.WindowType.Widget)
        window.layout().addWidget(tutorial)
        window._tutorial = tutorial
        return tutorial._back_btn if kind == 'back' else tutorial._next_btn
    if kind in ('combo', 'spin', 'edit'):
        from samsara.ui.settings.presentation import style
        window.setStyleSheet(style())
        widget = {'combo': QComboBox, 'spin': QSpinBox, 'edit': QLineEdit}[kind]()
        if kind == 'combo':
            widget.addItems(['Dark', 'Light'])
        else:
            widget.setReadOnly(True)  # Isolate ring pixels from caret/select-all.
            if kind == 'spin':
                widget.setValue(42)
            else:
                widget.setText('Search commands')
        widget.setParent(window)
        window._input_layout = theme._SettingsInputLayout(window)
        window._input_layout.apply(widget)
        return widget
    if kind == 'search':
        from samsara.ui.command_reference_detail import style_search
        widget = QLineEdit()
        widget.setReadOnly(True)
        widget.setText('Search commands')
        style_search(widget)
        return widget
    if kind == 'message':
        message = QMessageBox(QMessageBox.Icon.Question, 'Synthetic dialog', 'Keep focus?',
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, window)
        window._message = message
        # A Qt-created button wrapper, not a Python-constructed QPushButton.
        message.setWindowFlags(Qt.WindowType.Widget)
        window.layout().addWidget(message)
        return message.button(QMessageBox.StandardButton.No)
    if kind == 'tabs':
        tabs = QTabWidget()
        tabs.addTab(QWidget(), 'First')
        tabs.addTab(QWidget(), 'Second')
        window.layout().addWidget(tabs)
        return tabs.tabBar()
    if kind in ('checked', 'unchecked'):
        widget = QCheckBox('Choice')
        widget.setChecked(kind == 'checked')
        return widget
    widget = QSlider(Qt.Orientation.Horizontal)
    widget.setValue(50)
    return widget


@pytest.mark.parametrize('palette', ('dark', 'light'))
@pytest.mark.parametrize('kind', KINDS)
def test_focus_pixels_geometry_and_accessibility(qapp, kind, palette):
    install(qapp)
    theme.set_theme(palette, refresh=False)
    window = QWidget()
    window.setStyleSheet(theme.build_stylesheet())
    layout = QVBoxLayout(window)
    widget = make_control(kind, window)
    if kind not in ('message', 'tabs', 'back', 'next'):
        layout.addWidget(widget)
    neighbour = QPushButton('Neighbour')
    layout.addWidget(neighbour)
    window.resize(330, 240)
    window.show()
    window.activateWindow()
    settle(qapp)
    try:
        neighbour.setFocus(Qt.FocusReason.OtherFocusReason)
        widget.setFocus(Qt.FocusReason.MouseFocusReason)
        settle(qapp)
        geometry = (widget.geometry(), widget.contentsRect(), widget.sizeHint(), neighbour.geometry())
        children = [(child, child.geometry()) for child in widget.findChildren(QWidget) if child.isVisibleTo(widget)]
        before = widget.grab().toImage()
        neighbour.setFocus(Qt.FocusReason.OtherFocusReason)
        widget.setFocus(Qt.FocusReason.TabFocusReason)
        settle(qapp)
        if kind == 'spin':
            widget.lineEdit().deselect()
        elif isinstance(widget, QLineEdit):
            widget.deselect()
        after = widget.grab().toImage()
        assert before.size() == after.size()
        assert geometry == (widget.geometry(), widget.contentsRect(), widget.sizeHint(), neighbour.geometry())
        assert all(child.geometry() == rect for child, rect in children)
        focused = qapp.focusWidget()
        assert focused.property('keyboardFocus') is True
        assert QAccessible.queryAccessibleInterface(focused).state().focused
        changed = [(x, y) for y in range(after.height()) for x in range(after.width())
                   if before.pixel(x, y) != after.pixel(x, y)]
        assert changed, (kind, 'lost keyboard indication')
        if kind not in NATIVE:
            # Settings margin is vertical only: the border begins at x=0,
            # y=margin, never margin pixels inside the left/right edge.
            margin = max(0, (theme.HIT_TARGET_MIN - theme.TYPE_BODY - theme.SPACE_2) // 2) if kind in ('combo', 'spin', 'edit') else 0
            dpr = after.devicePixelRatio()
            interior = QPainterPath()
            interior.addRoundedRect(QRectF(3, margin+3, widget.width()-6,
                                          widget.height()-2*margin-6), 5, 5)
            assert not [(x, y) for x, y in changed
                        if interior.contains(QPointF((x+.5)/dpr, (y+.5)/dpr))]
            if kind == 'combo':
                edge = [x for x, y in changed if round((margin+10)*dpr) <= y < after.height()-round((margin+10)*dpr)]
                assert min(edge) == 0 and max(edge) == after.width()-1
        # Same real focus, no keyboard indication after a mouse-focus event.
        neighbour.setFocus(Qt.FocusReason.OtherFocusReason)
        widget.setFocus(Qt.FocusReason.MouseFocusReason)
        settle(qapp)
        assert not qapp.focusWidget().property('keyboardFocus')
        assert widget.grab().toImage() == before
    finally:
        window.hide()
        window.deleteLater()
        settle(qapp)
        theme.set_theme('dark', refresh=False)


def test_shared_rewriter_is_idempotent_and_preserves_native_only_rules():
    source = 'QPushButton:focus {border:2px solid teal;} QRadioButton:focus {color:teal;} QSlider::handle:horizontal:focus {border-color:teal;}'
    result = keyboard_stylesheet(source)
    assert 'border: 1px solid teal; outline: none;' in result
    assert result.count('outline:') == 1
    assert keyboard_stylesheet(result) == result


@pytest.mark.parametrize('kind', ('settings', 'reference'))
def test_search_text_position_and_size_hint_stay_fixed(qapp, kind):
    from PySide6.QtWidgets import QStyleOptionFrame
    from samsara.ui.settings.sidebar import SearchGlyph, search_qss
    from samsara.ui.command_reference_detail import style_search
    install(qapp)
    window = QWidget()
    layout = QVBoxLayout(window)
    edit = QLineEdit('Search commands')
    edit.setReadOnly(True)
    if kind == 'settings':
        edit.setObjectName('settingsSearchField')
        edit.setStyleSheet(search_qss())
        SearchGlyph(edit)
    else:
        style_search(edit)
    layout.addWidget(edit)
    neighbour = QPushButton('Neighbour')
    layout.addWidget(neighbour)
    window.show()
    window.activateWindow()
    settle(qapp)
    def state():
        option = QStyleOptionFrame()
        edit.initStyleOption(option)
        rect = edit.style().subElementRect(edit.style().SubElement.SE_LineEditContents, option, edit)
        return rect.x(), edit.sizeHint(), edit.geometry(), neighbour.geometry()
    try:
        neighbour.setFocus(Qt.FocusReason.OtherFocusReason)
        settle(qapp)
        before = state()
        edit.setFocus(Qt.FocusReason.TabFocusReason)
        settle(qapp)
        assert edit.property('keyboardFocus') is True
        assert edit.property('settingsKeyboardFocus') is True
        assert state() == before
        assert 'border: 2px solid' in edit.styleSheet()
        assert 'outline: none' in edit.styleSheet()
    finally:
        window.hide()
        window.deleteLater()
        settle(qapp)


def assert_button_focus(qapp, button, *, tab=False):
    """Compare ink with a fixed pointer/geometry; never activate the button."""
    button.window().activateWindow()
    # HEAD's focus repolish can settle inherited QSS minimum widths (notably
    # Ava's pre-sized footer). Compare stable rendered states, as K2 requires.
    button.setFocus(Qt.FocusReason.TabFocusReason)
    settle(qapp)
    button.clearFocus()
    settle(qapp)
    assert not button.hasFocus()
    before = button.grab().toImage()
    geometry = (button.geometry(), button.contentsRect(), button.sizeHint())
    if tab:
        # Begin on the actual predecessor, then let Qt deliver the Tab.
        previous = button.previousInFocusChain()
        for _ in range(300):
            if previous.isVisible() and previous.isEnabled() and previous.focusPolicy() & Qt.FocusPolicy.TabFocus:
                break
            previous = previous.previousInFocusChain()
        previous.setFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClick(previous, Qt.Key.Key_Tab)
    else:
        button.clearFocus()
        button.setFocus(Qt.FocusReason.TabFocusReason)
    settle(qapp)
    assert qapp.focusWidget() is button, button.text()
    assert button.property('keyboardFocus') is True
    after = button.grab().toImage()
    assert before.size() == after.size()
    assert geometry[:2] == (button.geometry(), button.contentsRect())
    # Setup's fixed-size details buttons already change sizeHint at HEAD;
    # their actual geometry stays fixed. Preserve that documented exception.
    if type(button).__name__ == '_DetailsButton':
        assert button.sizeHint().width() == geometry[2].width() + 2
        assert button.sizeHint().height() == geometry[2].height() + 2
    else:
        assert geometry[2] == button.sizeHint()
    assert before != after, (button.window().windowTitle(), type(button).__name__,
                             button.text(), 'lost focus indication')
    # Suppression is legal only while the app's own visible border changes.
    # Painted _StepDot/_Target controls keep their HEAD interior focus sign;
    # only the QSS controls use the edge border. Either must visibly differ.


@pytest.fixture
def quiet_windows(qapp, monkeypatch, tmp_path):
    """Real windows, synthetic state, no capture/hooks/provider workers."""
    from samsara import autostart
    from samsara.runtime import thread_registry
    from samsara.ui import ava_guide_qt, first_run_wizard_qt, mic_setup_wizard_qt, stress_wizard_qt
    from samsara.ui import command_cheatsheet_qt
    from samsara.ui.first_run_qt import ComponentsPage
    from tests.test_home_qt import _app
    from tests.test_settings_pages import _StubApp, _schema_defaults
    monkeypatch.setattr(thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
    monkeypatch.setattr(first_run_wizard_qt._WizardWindow, '_start_meter', lambda self: None)
    monkeypatch.setattr(mic_setup_wizard_qt._WizardWindow, '_ensure_audio_running', lambda self: None)
    monkeypatch.setattr(stress_wizard_qt._StressWizardWindow, '_arm_capture', lambda self: None)
    from samsara.ui.tutorial_qt import TutorialWindow
    monkeypatch.setattr(TutorialWindow, '_install_hook', lambda *a, **k: None)
    monkeypatch.setattr(command_cheatsheet_qt, 'catalog_rows', lambda _: [dict(
        phrase='open notepad', canonical_id='builtin.notepad', kind='builtin',
        description='Open the Notepad text editor.', aliases=['open notepad'],
        args=[], plugin='builtin', pack='core')])
    monkeypatch.setattr(command_cheatsheet_qt, '_scope_context', lambda: None)
    monkeypatch.setattr(command_cheatsheet_qt, '_disabled_packs', lambda: set())
    monkeypatch.setattr(command_cheatsheet_qt._CheatSheetWindow, '_foreground_screen', lambda _: None)
    install(qapp)
    made = []

    def build(kind):
        from samsara.ui.settings_qt import _SettingsWindow
        from samsara.ui.quick_reference_qt import _QuickReferenceWindow
        from samsara.ui.network_voice_notice import NetworkVoiceNotice
        app = _app()
        app.voice_training_window = None
        app.get_available_microphones = lambda: []
        constructors = {
            'ava': lambda: ava_guide_qt._WizardWindow(app),
            'first': lambda: first_run_wizard_qt._WizardWindow(tmp_path / 'config.json'),
            'mic': lambda: mic_setup_wizard_qt._WizardWindow(app),
            'stress': lambda: stress_wizard_qt._StressWizardWindow(app),
            'tutorial': lambda: TutorialWindow(app),
            'quick': lambda: _QuickReferenceWindow(app),
            'reference': lambda: command_cheatsheet_qt._CheatSheetWindow(lambda _: None, lambda: [], tmp_path / 'palette.json'),
            'settings': lambda: _SettingsWindow(_StubApp(_schema_defaults())),
            'settings_again': lambda: _SettingsWindow(_StubApp(_schema_defaults())),
            'network': NetworkVoiceNotice,
        }
        window = constructors[kind]()
        made.append(window)
        if kind in ('settings', 'settings_again'):
            window._ensure_preferences()
        if kind == 'settings_again':
            theme.apply_design_scale(window)
        window.show()
        window.activateWindow()
        settle(qapp)
        return window

    yield build
    for window in made:
        window.hide()
        window.deleteLater()
    settle(qapp)


def test_real_ava_next_has_visible_tab_focus(qapp, quiet_windows):
    window = quiet_windows('ava')
    assert_button_focus(qapp, window._next_btn, tab=True)


@pytest.mark.parametrize('palette', ('dark', 'light'))
def test_inline_accent_without_primary_class_keeps_visible_focus(qapp, palette):
    theme.set_theme(palette, refresh=False)
    window = QWidget()
    layout = QVBoxLayout(window)
    button = QPushButton('Inline primary')
    button.setStyleSheet(f'QPushButton {{ background: {theme.ACCENT}; }}')
    layout.addWidget(button)
    try:
        theme.apply_design_scale(window)
        window.show()
        settle(qapp)
        assert not button.objectName() and button.property('class') is None
        assert_button_focus(qapp, button)
    finally:
        window.hide()
        window.deleteLater()
        settle(qapp)
        theme.set_theme('dark', refresh=False)


def test_repeated_settings_scale_is_stable_and_hotkey_restyle_keeps_focus(qapp, quiet_windows):
    window = quiet_windows('settings')
    buttons = real_buttons(window)
    assert any(button.objectName() == 'helpButton' for button in buttons)
    # Construction has already applied the scale once.
    before = {button: button.styleSheet() for button in buttons}
    spin = window._widgets['adv_silence']
    spin_state = (spin.styleSheet(), spin.buttonSymbols(), spin.sizeHint())
    theme.apply_design_scale(window)
    settle(qapp)
    assert before == {button: button.styleSheet() for button in buttons}
    assert spin_state == (spin.styleSheet(), spin.buttonSymbols(), spin.sizeHint())
    from samsara.ui.settings.shared_qt import _HotkeyButton
    hotkey = next(button for button in buttons if isinstance(button, _HotkeyButton))
    sheet = hotkey.styleSheet()
    window._settings_input_layout.style_field(hotkey)
    settle(qapp)
    assert hotkey.styleSheet() == sheet
    assert '[keyboardFocus="true"]:focus' in sheet and 'outline: none' in sheet


def real_buttons(window):
    """Visit destination controls, excluding retained source scaffolding."""
    store = getattr(window, '_source_store', None)
    return [button for button in window.findChildren(QPushButton)
            if store is None or not store.isAncestorOf(button)]


@pytest.mark.parametrize('kind', ('ava', 'first', 'mic', 'stress', 'tutorial', 'quick',
                                 'reference', 'settings', 'network', 'settings_again'))
def test_every_real_window_button_keeps_focus_pixels(qapp, quiet_windows, kind):
    window = quiet_windows(kind)
    buttons = real_buttons(window)
    assert buttons
    for button in buttons:
        # Visit the real stacked page/disclosure. Disabled controls are enabled
        # only in this synthetic fixture to prove their eventual enabled style.
        ancestor = button
        while ancestor is not window:
            parent = ancestor.parentWidget()
            if isinstance(parent, QStackedWidget):
                parent.setCurrentWidget(ancestor)
            ancestor.show()
            ancestor.setEnabled(True)
            ancestor = parent
            assert ancestor is not None, (kind, button.text())
        settle(qapp)
        assert_button_focus(qapp, button)
