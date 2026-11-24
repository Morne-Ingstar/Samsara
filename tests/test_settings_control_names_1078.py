"""Keyboard-accessible names on the assembled Settings and companion windows."""
import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QAbstractButton, QLabel

from samsara.ui import settings_qt
from samsara.ui.settings import tools_page
from tests.test_friendlier_settings_595 import window


def tab_controls(root):
    """Exclude disabled/hidden controls, focus proxies and Qt's internal children.

    Backing controls behind visible radio groups have no TabFocus or are hidden.
    Scrollable controls can have TabFocus outside the current viewport.
    """
    widgets = []
    current = root.nextInFocusChain()
    while current is not root:
        if (current.isVisibleTo(root) and current.isEnabled()
                and current.focusPolicy() & Qt.FocusPolicy.TabFocus
                and not current.objectName().startswith('qt_')
                and current.focusProxy() is None):
            widgets.append(current)
        current = current.nextInFocusChain()
    return widgets


def unnamed_controls(root, scope):
    buddies = {label.buddy() for label in root.findChildren(QLabel)
               if label.isVisibleTo(root) and label.text().strip()}
    seen = {}
    for control in tab_controls(root):
        if control is not scope and not scope.isAncestorOf(control):
            continue
        name = control.objectName() or type(control).__name__
        seen[name] = seen.get(name, 0) + 1
        if (control.accessibleName().strip() or control in buddies
                or (isinstance(control, QAbstractButton) and control.text().strip())):
            continue
        yield f'{name}[{seen[name]}]'


def native_name(control):
    return QAccessible.queryAccessibleInterface(control).text(QAccessible.Text.Name)


def collect_name_audit(window, qapp, monkeypatch, qt_widget_scope):
    from samsara.ui.correction_capture_qt import CorrectionCaptureWindow
    from samsara.ui.mic_setup_wizard_qt import _WizardWindow
    from samsara.ui.profile_manager_qt import _ProfileManagerWindow

    settings_controls = {'Audio': 'tts_feedback_level', 'Ava': 'ava_personality'}
    report, fixed = {}, {}
    for name in (*settings_qt._TAB_NAMES, *tools_page.TOOLS):
        window.show_tab(name)
        page = window._stack.currentWidget()
        advanced = getattr(page, 'advanced', None)
        if advanced is not None and advanced.isVisibleTo(page):
            advanced.reveal()
        qapp.processEvents()
        report[name] = tuple(unnamed_controls(window, page))
        if name in settings_controls:
            control = window._widgets[settings_controls[name]]
            # The assembled Settings page can wrap this combo in radio buttons.
            fixed[name] = native_name(control)
    report['Settings chrome'] = tuple(
        gap for scope in (window._sidebar.parentWidget(), window._close_button.parentWidget())
        for gap in unnamed_controls(window, scope)
    )

    monkeypatch.setattr(_WizardWindow, '_ensure_audio_running', lambda self: None)
    mic = SimpleNamespace(config={}, get_available_microphones=lambda: [
        {'id': 1, 'name': 'Example microphone'}])
    profiles = SimpleNamespace(get_active_profile_names=lambda: {},
                               list_dictionary_profiles=lambda: ['Example dictionary'],
                               list_command_profiles=lambda: ['Example commands'])
    for name, build, attributes in (
        ('Microphone guide', lambda: _WizardWindow(mic), ('_device_combo',)),
        ('Profile manager', lambda: _ProfileManagerWindow(profiles), ('_dict_combo', '_cmd_combo')),
        ('Corrections capture', lambda: CorrectionCaptureWindow(SimpleNamespace(config={}), 'Example text'),
         ('_text_edit',)),
    ):
        with qt_widget_scope(build()) as surface:
            surface.show()
            qapp.processEvents()
            report[name] = tuple(unnamed_controls(surface, surface))
            controls = [getattr(surface, attr) for attr in attributes]
            assert all(control in tab_controls(surface) for control in controls)
            fixed[name] = tuple(map(native_name, controls))
            if name == 'Microphone guide':
                interface = QAccessible.queryAccessibleInterface(controls[0])
                description = interface.text(QAccessible.Text.Description)
    return report, fixed, description


_PAGE_PATH = 'samsara/ui/settings/presentation.py'
KNOWN_NAME_GAPS = (
    *((name, 'Page[1]', _PAGE_PATH)
       for name in ('General', 'Dictation', 'Audio', 'Ava', 'Shortcuts', 'Help',
                    'Alarms', 'Components', 'Integrations')),
    ('General', 'QLabel[1]', 'samsara/ui/widgets/beta_notice.py'),
    ('Command library', 'commandsPageScroll[1]', 'samsara/ui/settings/commands_qt.py'),
    ('Health', 'QScrollArea[1]', 'samsara/ui/settings/health_qt.py'),
    ('Music', 'QScrollArea[1]', 'samsara/ui/settings/music_qt.py'),
    ('Microphone guide', 'QScrollArea[1]', 'samsara/ui/mic_setup_wizard_qt.py'),
    ('Profile manager', 'QScrollArea[1]', 'samsara/ui/profile_manager_qt.py'),
)


@pytest.fixture(scope='module')
def name_audit_cache():
    # Cache inert observations, so the per-gap cases do not rebuild every page.
    return []


@pytest.fixture
def name_audit(request, name_audit_cache):
    if not name_audit_cache:
        fixtures = (request.getfixturevalue(name) for name in
                    ('window', 'qapp', 'monkeypatch', 'qt_widget_scope'))
        name_audit_cache.append(collect_name_audit(*fixtures))
    return name_audit_cache[0]


def test_keyboard_reachable_controls_have_effective_names(name_audit, record_property):
    report, fixed, description = name_audit
    record_property('accessible_name_gaps', json.dumps(report, sort_keys=True))
    assert fixed == {
        'Audio': 'Voice feedback', 'Ava': 'Ava personality',
        'Microphone guide': ('Microphone',), 'Profile manager': ('Profile:', 'Profile:'),
        'Corrections capture': ('Correction text',),
    }
    assert description == (
        "Select the microphone you'll be speaking into, then say a "
        "few words to confirm it's picking up your voice."
    )
    assert {(surface, widget) for surface, gaps in report.items() for widget in gaps} <= {
        (surface, widget) for surface, widget, _ in KNOWN_NAME_GAPS
    }


@pytest.mark.parametrize('surface,widget', [
    pytest.param(surface, widget, id=f'{surface}/{widget}',
                 marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason=path))
    for surface, widget, path in KNOWN_NAME_GAPS
])
def test_known_accessible_name_gap(name_audit, surface, widget):
    report, _, _ = name_audit
    assert widget not in report[surface]
