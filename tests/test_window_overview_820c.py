"""820c: real HUD layout and dispatch; fake window/audio boundaries only."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget, QPushButton

from plugins.commands import window_switcher as ws
from samsara.command_scope import MatchContext
from samsara.ui import window_overview_qt as ui
from tests.test_window_overview_820 import FakeDwm, overview, _matcher


@pytest.fixture(autouse=True)
def no_native_icon_messages(monkeypatch):
    monkeypatch.setattr(ui._IconTask, 'run', lambda self: None)


def rows(count):
    return [dict(number=n, hwnd=1000+n, pid=2000+n, app=f'Editor {n}',
                 title=f'Document {n}', short_title=f'Document {n}',
                 monitor=(n-1) % 3 + 1, primary=(n-1) % 3 == 0)
            for n in range(1, count+1)]


def settle(qapp, count=40):
    for _ in range(count):
        qapp.processEvents()


@pytest.fixture
def make_window(qapp):
    windows = []
    def make(data=None, **kwargs):
        window = ui.OverviewWindow(data or rows(24), kwargs.pop('on_action', Mock()), Mock(),
                                   thumbnails=ui.DwmThumbnails(FakeDwm()), **kwargs)
        window.setGeometry(0, 0, 1920, 1080)
        windows.append(window)
        window.show()
        settle(qapp)
        return window
    yield make
    for window in windows:
        window.close()
    settle(qapp)


def test_24_cards_fit_without_scroll_and_25th_scrolls(make_window):
    window = make_window()
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window._columns == 6
    assert window.scroll.verticalScrollBar().maximum() == 0
    viewport = window.scroll.viewport()
    for index, card in enumerate(window._tiles.values()):
        rect = card.rect()
        rect.moveTopLeft(card.mapTo(viewport, QPoint()))
        assert viewport.rect().contains(rect)
        assert card.height() == 210
        assert window.grid.getItemPosition(index)[:2] == (index // 6, index % 6)
        assert card.preview.height() == 130
        assert 292 <= card.width() <= 294
    overflow = make_window(rows(25))
    assert overflow.scroll.verticalScrollBar().maximum() > 0


def test_controls_are_quiet_until_hover_or_selection(make_window, qapp):
    window = make_window()
    assert all(not tile.findChildren(QPushButton) for tile in window._tiles.values())
    assert not hasattr(window, 'bar')  # 869: shared footer removed.
    card = window._tiles[3]
    assert not card.quick
    card.hovered = True
    card.quick_actions()
    # 869: eleven local vector actions replace the two quick controls.
    assert len(card.quick) == 11 and all(b.isVisible() for b in card.quick)
    assert [b.action for b in card.quick] == [action for _, action in ui.ACTIONS]
    for i, button in enumerate(card.quick):
        assert button.size().width() == button.size().height() == 44
        assert f'Window 3, Editor 3:' in button.accessibleName()
        assert 'say' in button.toolTip() and button.accessibleDescription()
        assert card.tray.rect().contains(button.geometry())
        assert not any(button.geometry().intersects(other.geometry()) for other in card.quick[i+1:])
    qapp.sendEvent(card.quick[0], ui.QEvent(ui.QEvent.Type.Enter))
    assert card.tray_visible  # descendant entry never hides the tray
    card.hovered = False
    card.quick_actions()
    assert all(not b.isVisible() for b in card.quick)
    window.select(3)
    assert card.tray_visible  # 869: selected card owns its controls.
    assert all(b.isVisible() for b in card.quick)
    assert 'cube' not in card.buttons
    assert not hasattr(window, 'bar_buttons')  # 869: no shared action binding.


def test_real_number_dispatch_selects_shared_bar_and_explicit_go_activates(overview, make_window, monkeypatch):
    app, data, panel = overview
    window = make_window(data, keep_open=True, on_action=lambda n, a, k: ws.overview_action(app, n, a, k))
    panel.select.side_effect = window.select
    entry, remainder = _matcher().match('three', context=MatchContext(
        tags=frozenset({'window_overview.visible', 'window_cube.visible'})))
    assert entry.handler(app, remainder)
    assert window._selected == 3 and window._tiles[3].tray_visible  # 869: local tray.
    ws._force_focus.assert_not_called()
    panel.hide.assert_not_called()
    window._tiles[3].buttons['left'].click()
    from plugins.commands import windows as mover
    mover.snap_window.assert_called_once()
    assert ws.parse_overview_action('three go') == (3, 'go', False)
    ws.overview_action(app, 3, 'go')
    ws._force_focus.assert_called_once_with(103)


def test_mouse_right_select_left_go_and_monitor_pill_uses_shared_action(make_window):
    invoked = Mock()
    window = make_window(on_action=invoked)
    card = window._tiles[2]
    QTest.mouseClick(card, Qt.MouseButton.RightButton, pos=QPoint(100, 100))
    invoked.assert_called_with(2, 'select', False)
    QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    invoked.assert_called_with(2, 'go', False)
    QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(card.width()-20, 18))
    invoked.assert_called_with(2, 'go', False)  # 869: pill is informational header.


def test_inline_close_cancel_enter_and_remove_preserves_numbers(overview, make_window, monkeypatch):
    app, data, panel = overview
    window = make_window(data, on_action=lambda n, a, k: ws.overview_action(app, n, a, k))
    panel.confirmation.side_effect = window.show_confirmation
    panel.remove.side_effect = window.remove
    assert ws.overview_action(app, 2, 'close')
    assert window._tiles[2].confirming
    assert window._tiles[2].overlay.isVisible()
    assert not window._thumbnails.api.updated[-2][1]  # target native preview hidden
    window._escape_pressed()
    assert ws._overview_pending_close is None and not window._tiles[2].confirming
    ws.overview_action(app, 2, 'close')
    window._enter_pressed()
    assert list(window._tiles) == [1, 3]
    assert [r['number'] for r in ws._overview_rows] == [1, 3]
    assert 102 not in window._thumbnails.handles
    assert ws.overview_active(app)
    assert window.title.text() == '2 windows'


def test_duplicate_subtitle_monitor_pills_and_reading_focus(make_window):
    data = rows(3)
    data[0]['short_title'] = data[0]['app']
    window = make_window(data)
    assert window._tiles[1].subtitle == ''
    assert [c.monitor_text for c in window._tiles.values()] == ['M1', 'M2', 'M3']
    window.set_monitor(3, 2, True)
    assert window._tiles[3].monitor_text == 'M2'
    assert window._tiles[3].row['primary']
    window.focus_reading(2)
    assert [n for n, c in window._tiles.items() if c.property('reading')] == [2]
    window.focus_reading(None)
    assert not any(c.property('reading') for c in window._tiles.values())


@pytest.mark.parametrize('display', ['compact', 'full'])
@pytest.mark.parametrize('initially_visible', [False, True])
def test_indicator_hidden_state_live_and_restored(make_window, qapp, display, initially_visible):
    if display == 'compact':
        from samsara.ui.listening_indicator import ListeningIndicator
        indicator = ListeningIndicator()
        indicator.set_listening(True)
        widget = indicator
    else:
        from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
        from samsara.live_surface.model import CaptureState
        controller = LiveSurfaceController(SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}}))
        controller.widget = QWidget()
        controller.model.capture = CaptureState.RECORDING
        indicator = LiveSurfaceIndicatorAdapter(controller)
        widget = controller.widget
    try:
        widget.setVisible(initially_visible)
        window = make_window(rows(3), listening_indicator=indicator)
        assert not widget.isVisible()
        assert window.listening.text() == '● Recording'
        if display == 'compact':
            indicator.set_listening(False)
        else:
            controller.model.capture = CaptureState.TRANSCRIBING
        window._listening_state()
        assert window.listening.text() == ('● Idle' if display == 'compact' else '● Transcribing')
        window.close()
        assert widget.isVisible() == initially_visible
    finally:
        QWidget.hide(widget)
        widget.deleteLater()


def test_dock_blocks_repeated_show_without_resetting_state(make_window):
    widget = QWidget()
    widget.show()
    widget.display_state = lambda: 'capture'
    window = make_window(rows(1), listening_indicator=widget)
    widget.show()
    assert not widget.isVisible()
    assert window._dock.state() == 'Recording'
    window.close()
    assert widget.isVisible()
    widget.close()


def test_read_number_alias_routes_full_title(overview):
    app, data, _ = overview
    entry, remainder = _matcher().match('read three', context=MatchContext(
        tags=frozenset({'window_overview.visible', 'window_cube.visible'})))
    assert entry.handler(app, remainder)
    ws._speak.assert_called_once_with(app, data[2]['title'])


def test_thumbnail_registration_yields_and_aspect_fill_crop(make_window, qapp):
    window = make_window(rows(23))
    assert len(window._thumbnails.api.registered) == 23
    assert window._registered and window.paint_metrics['windows'] == 23
    assert window.paint_metrics['after_registration_ms'] < 400
    api = FakeDwm()
    crops = []
    original = api.DwmUpdateThumbnailProperties
    def update(handle, props):
        p = props._obj
        crops.append((p.flags, p.source.left, p.source.top, p.source.right, p.source.bottom))
        return original(handle, props)
    api.DwmUpdateThumbnailProperties = update
    thumbs = ui.DwmThumbnails(api)
    assert thumbs.register(1, 2)
    assert thumbs.update(2, (0, 0, 269, 130), True, (0, 76, 1920, 1004))
    assert crops == [(31, 0, 76, 1920, 1004)]
    thumbs.close()

@pytest.mark.parametrize('palette', ['dark', 'light'])
@pytest.mark.parametrize('physical,scale,columns', [((1920, 1080), 1, 6), ((1920, 1080), 1.5, 4),
                                                  ((1366, 768), 1, 4), ((1366, 768), 1.5, 2)])
def test_responsive_matrix_and_complete_last_card(make_window, qapp, palette, physical, scale, columns):
    # Logical geometry here; the separate real-platform proof harness checks DPR in fresh processes.
    from samsara.ui import theme
    from PySide6.QtCore import QRect
    theme.set_theme(palette, refresh=False)
    window = make_window(rows(23))
    window.resize(round(physical[0]/scale), round(physical[1]/scale))
    settle(qapp)
    assert window._columns == columns
    assert window.scroll.horizontalScrollBar().maximum() == 0
    assert all(card.width() >= 288 for card in window._tiles.values())
    assert (window.scroll.verticalScrollBar().maximum() > 0) == (columns != 6)
    window.select(23)
    settle(qapp)
    card = window._tiles[23]
    viewport = window.scroll.viewport()
    assert viewport.rect().contains(QRect(card.mapTo(viewport, QPoint()), card.size()))
    assert card.tray_visible
    assert all(card.tray.rect().contains(b.geometry()) for b in card.quick)
    assert window.keep.height() >= 44 and window.dismiss.height() >= 44
    controls = [window.title, window.listening, window.keep, window.dismiss]
    assert all(not a.geometry().intersects(b.geometry()) for i, a in enumerate(controls) for b in controls[i+1:])


def test_selected_and_hovered_actions_keep_bound_number_and_single_dispatch(make_window, qapp):
    invoked = Mock()
    window = make_window(on_action=invoked, keep_open=True)
    window.select(3)
    card = window._tiles[7]
    card.hovered = True
    card.quick_actions()
    assert window._tiles[3].tray_visible and card.tray_visible
    QTest.mouseClick(card.buttons['left'], Qt.MouseButton.LeftButton)
    assert invoked.call_args_list == [((7, 'left', True),)]
    invoked.reset_mock()
    window.select(3)
    window._tiles[3].setFocus()
    QTest.keyClick(window._tiles[3], Qt.Key.Key_Return)
    invoked.assert_called_once_with(3, 'go', True)
    invoked.reset_mock()
    QTest.mouseClick(card.tray, Qt.MouseButton.LeftButton, pos=QPoint(210, 105))
    invoked.assert_called_once_with(7, 'select', True)


def test_focus_tab_enter_and_cancel_priority(make_window, qapp):
    invoked = Mock()
    window = make_window(on_action=invoked)
    window.activateWindow()
    window.dismiss.setFocus()
    QTest.keyClick(window.dismiss, Qt.Key.Key_Tab)
    settle(qapp)
    assert window._tiles[1].hasFocus()
    assert window._selected == 1
    card = window._tiles[1]
    QTest.keyClick(card, Qt.Key.Key_Tab)
    assert card.buttons['left'].hasFocus()
    invoked.reset_mock()
    QTest.keyClick(card.buttons['left'], Qt.Key.Key_Return)
    invoked.assert_called_once_with(1, 'left', False)
    for button in card.quick:
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Tab)
    assert window.keep.hasFocus()
    window.show_confirmation('Close 3: Editor?')
    target = window._tiles[3]
    assert target.overlay.hasFocus()
    color = target.overlay.grab().toImage().pixelColor(5, 5)
    assert color.alpha() >= 230 and color.lightness() < 60  # painted dark overlay, including QWidget subclasses
    QTest.keyClick(target.overlay, Qt.Key.Key_Tab)
    assert target.yes.hasFocus()
    QTest.keyClick(target.yes, Qt.Key.Key_Tab)
    assert target.cancel.hasFocus()
    invoked.reset_mock()
    QTest.keyClick(target.cancel, Qt.Key.Key_Return)
    invoked.assert_called_once_with(0, 'cancel close', False)
    window.show_confirmation('')
    assert target.hasFocus() and target.tray_visible


def test_keyboard_digits_arrows_timeout_and_invalid_combination(make_window, qapp, monkeypatch):
    invoked = Mock()
    window = make_window(on_action=invoked)
    window.activateWindow()
    card = window._tiles[1]
    card.setFocus()
    clock = [100.0]
    monkeypatch.setattr(ui.time, 'monotonic', lambda: clock[0])
    QTest.keyClicks(card, '23')
    assert window._selected == 23
    assert [call.args[1] for call in invoked.call_args_list] == ['select', 'select']
    QTest.keyClicks(card, '9')  # 239 is invalid, with no fallback to 9.
    assert window._selected == 23 and window._digits == ''
    clock[0] += 1
    QTest.keyClicks(card, '2')
    assert window._selected == 2
    QTest.keyClick(window._tiles[2], Qt.Key.Key_Down)
    assert window._selected == 8 and window._tiles[8].hasFocus()
    assert window._digits == ''


def test_pressed_tray_retention_and_outside_release_cancels(make_window, qapp):
    invoked = Mock()
    window = make_window(on_action=invoked)
    card = window._tiles[7]
    card.hovered = True
    card.quick_actions()
    button = card.buttons['left']
    QTest.mousePress(button, Qt.MouseButton.LeftButton)
    card.hovered = card.selected = False
    window._selected = None
    window.setFocus()
    card.quick_actions()
    assert button.isDown() and card.tray_visible
    QTest.mouseRelease(button, Qt.MouseButton.LeftButton, pos=QPoint(-20, -20))
    assert not invoked.called


def test_native_tray_hides_restores_without_registration(make_window, qapp):
    window = make_window(rows(3))
    api = window._thumbnails.api
    before = list(api.registered)
    window.focus_reading(2)
    card = window._tiles[2]
    assert not card.tray_visible and not card.selected
    card.hovered = True
    card.quick_actions()
    handle = window._thumbnails.handles[card.row['hwnd']].value
    assert dict(api.updated)[handle] is False
    card.hovered = False
    window.setFocus()
    card.quick_actions()
    assert dict(api.updated)[handle] is True
    assert api.registered == before and not api.released
    assert not card.preview.pixmap().isNull()


def test_monitor_control_disabled_in_fixed_slot_and_theme_refresh(make_window):
    from samsara.ui import theme
    data = rows(1)
    data[0]['monitor_count'] = 1
    window = make_window(data)
    window.select(1)
    card = window._tiles[1]
    button = card.buttons['next monitor']
    assert not button.isEnabled()
    assert 'only one monitor' in button.accessibleDescription()
    assert card.buttons['close'].x() == 220
    theme.set_theme('light', refresh=False)
    window.apply_theme()
    assert theme.OVERVIEW_ACTION_SURFACE in card.tray.styleSheet()
    window.show_confirmation('Close 1: Editor?')
    assert card.yes.height() >= 44 and card.cancel.height() >= 44


def test_removal_successor_stable_numbers_and_stale_card_action(make_window):
    invoked = Mock()
    window = make_window(rows(3), on_action=invoked)
    window.select(2)
    retired = window._tiles[2]
    window.remove(2)
    assert list(window._tiles) == [1, 3] and window._selected == 3
    assert window._tiles[3].hasFocus()
    retired.buttons['left'].click()
    assert not invoked.called
    window.remove(3)
    assert list(window._tiles) == [1] and window._selected == 1


def test_panel_stale_queued_confirmation_cannot_reopen_removed_generation(monkeypatch):
    pending = []
    monkeypatch.setattr(ui.qt_runtime, 'post', pending.append)
    panel = ui.OverviewPanel()
    panel.window = Mock()
    panel.confirmation('Close 3: Editor?')
    panel._generation += 1
    pending[0]()
    panel.window.show_confirmation.assert_not_called()
