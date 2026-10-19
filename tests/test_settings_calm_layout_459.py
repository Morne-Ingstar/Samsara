"""Real Settings geometry and native control contracts for the calm layout."""
import os
from pathlib import Path
os.environ.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts'))
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QLabel, QSlider, QWidget
import pytest

from samsara.ui import theme
from samsara.ui.settings.calm_controls import PercentSpinBox, SegmentedChoice
from tests.test_settings_pages import _StubApp, _schema_defaults


def settle(qapp):
    for _ in range(16):
        qapp.processEvents()


@pytest.fixture(params=('dark', 'light'))
def settings(request, qapp, qt_widget_scope):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.settings_qt import _SettingsWindow
    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
        patches.setattr(ComponentsPage, 'refresh_async', lambda self: None)
        theme.set_theme(request.param, refresh=False)
        config = _schema_defaults()
        config['wake_profiles'] = [{'id': 'Fixture', 'phrase': 'computer', 'target_process': 'fixture.exe',
                                    'send_word': 'send', 'enabled': True}]
        config['ducking'] = {'level': .10, 'hands_free_level': .15, 'hands_free_idle_level': .80,
                             'hands_free_enabled': True, 'recording_mode': 'duck'}
        window = _SettingsWindow(_StubApp(config))
        try:
            # Lazy source controls can be parentless. Keep their entire visit
            # within one test, and destroy the window while its patches exist.
            with qt_widget_scope(window):
                window.resize(1160, 900)
                window.show()
                settle(qapp)
                # Every geometry case starts from the same complete visit;
                # iterating lazy pages can otherwise grow the page dictionary.
                tuple(window._destination_rows)
                settle(qapp)
                yield window
        finally:
            theme.set_theme('dark', refresh=False)


def reveal(window, name, qapp):
    page = window._pages[name]
    window._stack.setCurrentWidget(page)
    page.advanced.reveal()
    settle(qapp)
    return page


def test_plain_rows_survive_theme_refresh(settings, qapp):
    settings.apply_theme()
    for name in ('Audio', 'Dictation', 'General'):
        page = reveal(settings, name, qapp)
        background = theme.qcolor(theme.BG0)
        for row in settings._destination_rows:
            if row.page_name != name or not row.isVisibleTo(page):
                continue
            # Both the old fill and hover border would differ from the page.
            row.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
            capture = page.widget().grab().toImage()
            dpr = capture.devicePixelRatio()
            origin = row.mapTo(page.widget(), QPoint())
            # Also sample inside the former card's 8px margin/rounded corner,
            # while remaining above the new row's text and control ink.
            for x, y in ((2, 2), (theme.SPACE_1//2, theme.SPACE_2-2)):
                assert capture.pixelColor(round((origin.x()+x)*dpr), round((origin.y()+y)*dpr)) == background, row.title
            row.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
            assert row.heading.font().weight() == 400
            assert not row.heading.font().underline()


@pytest.mark.parametrize('width', (780, 1160))
def test_controls_share_one_right_edge_and_text_fits(settings, qapp, width):
    settings.resize(width, 900)
    for name in settings._pages:
        page = reveal(settings, name, qapp)
        edges = []
        for row in settings._destination_rows:
            if row.page_name != name or not row.isVisibleTo(page):
                continue
            edges.append(row.value.mapTo(page.widget(), QPoint(row.value.width(), 0)).x())
            for label in (row.heading, row.detail):
                assert label.height() >= label.heightForWidth(label.width()), (name, row.title)
            assert row.value.mapTo(page.widget(), QPoint(row.value.width(), 0)).x() <= page.widget().width()
            for control in row.findChildren(QWidget):
                if control.isVisibleTo(row) and control.accessibleName() and control.focusPolicy() & Qt.FocusPolicy.TabFocus:
                    assert control.width() >= 44 and control.height() >= 44, (name, row.title, control.accessibleName())
        assert max(edges) - min(edges) <= 1, (name, edges)
        assert page.widget().width() <= page.viewport().width(), name
    settings.resize(1160, 900)


def test_switch_preserves_native_accessibility_space_and_full_hit_target(settings, qapp):
    page = reveal(settings, 'Audio', qapp)
    control = settings._widgets['sound_feedback']
    iface = QAccessible.queryAccessibleInterface(control)
    assert iface.role() == QAccessible.Role.CheckBox
    assert iface.state().checkable
    assert control.accessibleName() and control.accessibleDescription()
    assert control.width() >= 44 and control.height() >= 44
    control.setFocus()
    before = control.isChecked()
    QTest.keyClick(control, Qt.Key.Key_Space)
    assert control.isChecked() != before
    assert iface.state().checked == control.isChecked()
    # This corner is outside the native square-indicator hit area.
    QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=QPoint(control.width()-2, 2))
    assert control.isChecked() == before
    control.setEnabled(False)
    QTest.keyClick(control, Qt.Key.Key_Space)
    assert control.isChecked() == before
    assert iface.state().disabled
    control.setEnabled(True)


def test_segment_arrows_and_cancelled_consent_follow_canonical_combo(settings, qapp, monkeypatch):
    reveal(settings, 'Audio', qapp)
    combo = settings._widgets['tts_feedback_level']
    cluster = combo.parentWidget()
    assert isinstance(cluster, SegmentedChoice)
    original = combo.currentIndex()
    selected = cluster.buttons[original]
    selected.setFocus()
    QTest.keyClick(selected, Qt.Key.Key_Right)
    assert combo.currentIndex() == (original + 1) % combo.count()
    active = cluster.group.checkedButton()
    assert QAccessible.queryAccessibleInterface(active).state().checked
    assert QAccessible.queryAccessibleInterface(active).role() == QAccessible.Role.RadioButton
    assert sum(button.focusPolicy() == Qt.FocusPolicy.StrongFocus for button in cluster.buttons) == 1
    combo.setEnabled(False)
    assert all(not button.isEnabled() for button in cluster.buttons)
    combo.setEnabled(True)
    assert all(button.isEnabled() for button in cluster.buttons)
    combo.setCurrentIndex(original)
    from samsara.ui import ava_consent_qt
    monkeypatch.setattr(ava_consent_qt, 'request_consent', lambda *a, **k: None)
    monkeypatch.setattr(ava_consent_qt, 'consent_required', lambda *a, **k: True)
    route = settings._widgets['providerRoute']
    cluster = route.parentWidget()
    if isinstance(cluster, SegmentedChoice):
        cluster.buttons[2].click()
    else:
        # Long route labels remain a dropdown; its consent handlers still own state.
        route.setCurrentIndex(2)
    assert route.currentData() == 'off'
    if isinstance(cluster, SegmentedChoice):
        assert cluster.group.checkedId() == route.currentIndex()


@pytest.mark.parametrize('key, config_key', (
    ('adv_ducking_level', 'level'), ('adv_hands_free_ducking_level', 'hands_free_level'),
    ('adv_hands_free_idle_level', 'hands_free_idle_level')))
def test_percent_edit_keeps_fractional_storage_bounds_and_step(settings, qapp, key, config_key):
    reveal(settings, 'Audio', qapp)
    control = settings._widgets[key]
    assert isinstance(control, PercentSpinBox)
    assert control.singleStep() == .05 and control.minimum() == 0 and control.maximum() == 1
    initial = control.value()
    for value in (0, .05, .10, .15, .80, 1):
        control.setValue(value)
        assert control.text() == f'{round(value*100)}%'
        assert control.valueFromText(control.text()) == value
    control.lineEdit().setText('25%')
    control.interpretText()
    assert control.value() == .25
    assert control._canonical_control.value() == .25
    accessible = QAccessible.queryAccessibleInterface(control)
    assert accessible.text(QAccessible.Text.Value) == control.textFromValue(control.value()) == '25'
    assert control.suffix() == '%'
    updates = settings._collect_destination_updates({})
    assert updates['ducking'][config_key] == .25
    control.stepUp()
    assert control.value() == .30 and control.text() == '30%'
    control.setValue(initial)


def test_slider_background_matches_page_outside_groove(settings, qapp):
    page = reveal(settings, 'Audio', qapp)
    slider = settings._widgets['sound_volume_slider']
    row = settings._rows_by_widget['sound_volume_slider']
    slider.clearFocus()
    capture = page.widget().grab().toImage()
    dpr = capture.devicePixelRatio()
    origin = slider.mapTo(page.widget(), QPoint())
    background = theme.qcolor(theme.BG0)
    # Probe the complete top/bottom strips, including the old slab's edge.
    for y in (2, slider.height()-3):
        for x in range(2, slider.width()-2):
            assert capture.pixelColor(round((origin.x()+x)*dpr), round((origin.y()+y)*dpr)) == background


def test_groups_preserve_rows_and_hide_when_search_has_no_matching_members(settings, qapp):
    from samsara.ui.settings.calm_layout import GroupHeading
    from samsara.ui.settings.dictation_page import BASE, ADVANCED
    page = reveal(settings, 'Dictation', qapp)
    groups = page.findChildren(GroupHeading)
    assert groups and len([row for row in settings._destination_rows if row.page_name == 'Dictation']) == len(BASE) + len(ADVANCED) + 1
    settings._search_edit.setText('manual threshold')
    settings._apply_search_filter()
    settle(qapp)
    assert all(any(not row.isHidden() for row in group.rows) == (not group.isHidden()) for group in groups)
    settings._search_edit.setText('no-such-settings-row')
    settings._apply_search_filter()
    settle(qapp)
    assert page.advanced.no_match.isVisibleTo(page)
    settings._search_edit.clear()
    settings._apply_search_filter()
    settle(qapp)


def test_wrapped_control_does_not_overlap_status_copy(qapp):
    from PySide6.QtWidgets import QPushButton
    from samsara.ui.settings.presentation import SettingRow
    from samsara.ui.settings.calm_layout import RowLayout
    control = QPushButton('Fixture control')
    control.setFixedSize(320, 44)
    row = SettingRow('Fixture setting', 'Fixture description', [control])
    warning = QLabel('Fixture status')
    warning.setMinimumHeight(44)
    row.grid.addWidget(warning, row.grid.rowCount(), 0, 1, 3)
    row.resize(476, 200)
    row._calm_layout = RowLayout(row)
    row.show()
    settle(qapp)
    assert not row.value.geometry().intersects(warning.geometry())
    assert warning.y() >= row.value.geometry().bottom()
    row.hide()
    row.deleteLater()


def test_continuing_groups_have_one_heading_at_the_first_member(settings, qapp):
    from samsara.ui.settings.calm_layout import GroupHeading
    for name in settings._pages:
        page = reveal(settings, name, qapp)
        groups = sorted(page.findChildren(GroupHeading),
                        key=lambda group: group.mapTo(page.widget(), QPoint()).y())
        titles = [group.text() for group in groups]
        assert all(left != right for left, right in zip(titles, titles[1:])), (name, titles)
        for group in groups:
            visible = [row for row in group.rows if row.isVisibleTo(page)]
            if group.isVisibleTo(page) and visible:
                assert group.mapTo(page.widget(), QPoint()).y() < visible[0].mapTo(page.widget(), QPoint()).y()
    audio = reveal(settings, 'Audio', qapp)
    speech = [group for group in audio.findChildren(GroupHeading) if group.text() == 'Text-to-Speech']
    assert len(speech) == 1
    assert speech[0].rows[0] is settings._rows_by_widget['tts_feedback_level']
    voice = [group for group in audio.findChildren(GroupHeading) if group.text() == 'Voice']
    assert len(voice) == 1
    assert voice[0].rows[0] is settings._rows_by_widget['tts_engine']
    settings._search_edit.setText('Speech engine')
    settings._apply_search_filter()
    settle(qapp)
    assert not voice[0].isHidden()
    # 479 makes Speech engine an everyday choice; search need not reveal Advanced.
    audio.advanced.toggle.setChecked(False)
    settings._apply_search_filter()
    assert not audio.advanced.toggle.isChecked()
    settings._search_edit.clear()
    settings._apply_search_filter()
    settle(qapp)


def test_group_refresh_survives_deleted_members_and_queued_destruction(qapp):
    from PySide6.QtCore import QCoreApplication, QEvent
    from samsara.ui.settings.calm_layout import GroupHeading
    row = QWidget()
    heading = GroupHeading('Fixture group', [row])
    row.show()
    row.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    heading.refresh()
    assert heading.rows == [] and heading.isHidden()
    heading.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    settle(qapp)
