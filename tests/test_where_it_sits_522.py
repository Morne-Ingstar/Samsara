"""One interactive desktop, using synthetic Qt fixtures without bootstrap."""
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton
import pytest

from samsara.onboarding.display_placement import placement_updates
from samsara.ui.tutorial_display_qt import DisplayPlacementPage
from samsara.ui.widgets import indicator_demo
from samsara.ui.widgets.indicator_demo import CAPTIONS, DesktopSketch, POSITIONS
from samsara.ui.widgets.indicator_place import DRAG_CAPTION


@pytest.fixture
def page_factory(qapp, monkeypatch):
    pages = []
    def create(display='full', setup=False, reduced=False):
        monkeypatch.setattr(indicator_demo, 'system_reduced_motion', lambda: reduced)
        config = {'ui': {'listening_display': display}}
        config.update(placement_updates(config, 'bottom-left', 'synthetic-screen'))
        page = DisplayPlacementPage(config, setup=setup)
        page.resize(620, 400)
        page.show()
        qapp.processEvents()
        if page.picker.timer:
            page.picker.timer.stop()
        pages.append(page)
        return page
    yield create
    for page in pages:
        page.hide()
        page.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize('display', ['full', 'compact'])
@pytest.mark.parametrize('setup', [True, False])
def test_one_desktop_and_one_caption(page_factory, display, setup):
    page = page_factory(display, setup)
    assert page.findChildren(DesktopSketch) == [page.picker.desktop]
    assert page.findChildren(QLabel) == [page.reason, page.picker.caption]
    assert page.reason.text() == CAPTIONS[0]
    for beat in range(5):
        page.picker.set_frame(beat, .8)
        expected = CAPTIONS[beat] if beat in (1, 2, 3) else DRAG_CAPTION
        assert page.picker.caption.text() == expected
        assert page.picker.caption.accessibleName() == expected
        assert page.picker.caption.isVisibleTo(page)


@pytest.mark.parametrize('display', ['full', 'compact'])
def test_selected_slot_animates_and_moves_immediately(page_factory, display):
    page = page_factory(display)
    picker = page.picker
    selected = picker.targets['bottom-left']
    quiet = picker.targets['top-right']
    picker.set_frame(0)
    rest = selected.grab().toImage()
    outline = quiet.grab().toImage()
    picker.set_frame(1, .1)
    listening = selected.grab().toImage()
    picker.set_frame(1, .6)
    assert rest != listening != selected.grab().toImage()
    assert quiet.grab().toImage() == outline
    picker.set_frame(2, .8)
    assert picker.desktop.result_message()
    typed = selected.grab().toImage()
    picker.set_frame(3, .8)
    assert typed != selected.grab().toImage()
    beat, phase = picker.beat, picker.desktop.phase
    QTest.mouseClick(quiet, Qt.MouseButton.LeftButton)
    assert picker.value() == 'top-right'
    assert (picker.beat, picker.desktop.phase) == (beat, phase)
    assert quiet.grab().toImage() != outline
    assert not selected.isChecked() and quiet.isChecked()
    assert picker.desktop.indicator_rect().center().x() > picker.desktop.width()/2
    assert picker.desktop.indicator_rect().center().y() < picker.desktop.height()/2
    for position in POSITIONS:
        picker.choose(position)
        for frame in range(5):
            picker.set_frame(frame, .8)
            assert picker.desktop.work_rect().contains(picker.desktop.indicator_rect())


@pytest.mark.parametrize('display', ['full', 'compact'])
def test_reduced_motion_is_a_single_still(page_factory, display):
    page = page_factory(display, reduced=True)
    picker = page.picker
    assert picker.reduced_motion and picker.desktop.still
    assert picker.timer is None and not picker.findChildren(QTimer)
    assert picker.steps == []
    assert picker.beat == 1 and picker.caption.text() == CAPTIONS[1]
    before = picker.desktop.grab().toImage()
    for time in (0, 2500, 5300, 8200, 11000):
        picker.seek(time)
        assert picker.desktop.grab().toImage() == before
    picker.choose('top-right')
    assert picker.beat == 1 and picker.caption.text() == CAPTIONS[1]


def test_six_native_targets_tab_arrows_and_enter(page_factory, qapp):
    page = page_factory()
    picker = page.picker
    assert len(page.findChildren(QPushButton)) == 6
    controls = list(picker.targets.values())
    for position, button in picker.targets.items():
        assert button.accessibleName() == position.replace('-', ' ')
        assert button.width() >= 44 and button.height() >= 44
        assert button.property('walkthrough_detail') is True
    assert all(not a.geometry().intersects(b.geometry())
               for i, a in enumerate(controls) for b in controls[i+1:])
    selected = picker.targets['bottom-left']
    selected.setFocus(Qt.FocusReason.TabFocusReason)
    # Native exclusive buttons are one Tab stop; arrows reach the six choices.
    QTest.keyClick(selected, Qt.Key.Key_Tab)
    assert qapp.focusWidget() is selected
    seen = {selected}
    for key in (Qt.Key.Key_Right, Qt.Key.Key_Right, Qt.Key.Key_Up,
                Qt.Key.Key_Left, Qt.Key.Key_Left, Qt.Key.Key_Down):
        QTest.keyClick(qapp.focusWidget(), key)
        seen.add(qapp.focusWidget())
    assert seen == set(controls)
    QTest.keyClick(picker.targets['bottom-left'], Qt.Key.Key_Right)
    assert picker.value() == 'bottom-center'
    assert qapp.focusWidget() is picker.targets['bottom-center']
    chosen = []
    picker.placement_chosen.connect(chosen.append)
    QTest.keyClick(qapp.focusWidget(), Qt.Key.Key_Return)
    assert chosen == ['bottom-center']


@pytest.mark.parametrize('scale', [1, 1.5])
@pytest.mark.parametrize('display', ['full', 'compact'])
def test_setup_height_and_targets_fit_shorter_page(page_factory, scale, display):
    page = page_factory(display, setup=True)
    page.resize(round(860/scale), 400)
    page.layout().activate()
    # 519's existing page needs 298 DIPs at the default 100/150% setup width.
    assert page.minimumSizeHint().height() < 298
    assert page.picker.desktop.height() == 190
    for beat in range(5):
        page.picker.set_frame(beat, .8)
        assert page.minimumSizeHint().height() < 298
    for button in page.picker.targets.values():
        assert page.picker.desktop.work_rect().contains(QRectF(button.geometry()))


def test_display_switch_updates_one_preview(page_factory):
    page = page_factory()
    page.set_display('compact')
    assert len(page.findChildren(DesktopSketch)) == 1
    assert page.picker.display == page.picker.desktop.display == 'compact'
    assert page.picker.desktop.accessibleName().startswith('Compact')
    assert page.picker.value() == 'bottom-left'
