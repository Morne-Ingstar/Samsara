"""535 always available Compact gestures and shared loss-safe quit rule.

The tray is real Qt, the manager is real, and only the final process teardown
is replaced. No app bootstrap, live profile, child process, or device access.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import kept_session_draft
from samsara.session_modes import SessionMode
from samsara.ui import quit_draft_dialog_qt
from tests.test_kept_draft_leftovers_510 import quit_method
from tests.test_kept_draft_survives_505 import TEXT, end_session, make_manager
from tests.test_tray_qt import _make_app


def prepare(full, state):
    manager, inserted = make_manager(full=full)
    if state == "kept":
        end_session(manager)
    elif state == "paused hold":
        # Defensive parked state: Compact normally keeps a retained snapshot.
        # Build the real parked document, then prove display choice cannot
        # bypass the shared decision even if that latch is present in Compact.
        manager._live_surface_enabled = True
        manager.pause_draft(source="pause")
        manager._live_surface_enabled = full
    elif state == "empty":
        manager.clear_pending_draft()
        # An empty paused latch must not make a quit question appear.
        manager._parked_hold = True
    return manager, inserted


def tray_exit(qapp, manager, full):
    from samsara.ui.tray_qt import SamsaraTrayQt

    app = _make_app()
    app.config["ui"] = {"listening_display": "full" if full else "compact"}
    app._session_mode_manager = manager
    app._live_surface_hold_parking_enabled = lambda: full
    teardown = Mock()

    def quit_app():
        if quit_method()(app):
            teardown()

    app.quit_app = quit_app
    tray = SamsaraTrayQt(app)
    tray._rebuild_menu()
    try:
        next(a for a in tray._menu.actions() if a.text() == "Exit").trigger()
    finally:
        tray.stop()
        tray._menu.deleteLater()
        tray._tray.deleteLater()
    return teardown


@pytest.mark.parametrize("full", [True, False], ids=["Full", "Compact"])
@pytest.mark.parametrize("state", ["staged", "kept", "paused hold"])
@pytest.mark.parametrize("choice", ["keep", "send", "discard"])
def test_tray_exit_asks_for_all_unsent_states(qapp, monkeypatch, full, state, choice):
    manager, inserted = prepare(full, state)
    before = (manager.dictate_pending_buffer, manager.retained_draft,
              manager.draft_document.segments, manager.mode)
    ask = Mock(return_value=choice)
    monkeypatch.setattr(quit_draft_dialog_qt, "ask_quit_draft", ask)

    teardown = tray_exit(qapp, manager, full)

    ask.assert_called_once_with()
    assert teardown.call_count == (0 if choice == "keep" else 1)
    if choice == "keep":
        assert (manager.dictate_pending_buffer, manager.retained_draft,
                manager.draft_document.segments, manager.mode) == before
        assert inserted == []
    else:
        assert inserted == ([TEXT] if choice == "send" else [])
        assert not manager.dictate_pending_buffer
        assert not manager.retained_draft


@pytest.mark.parametrize("full", [True, False], ids=["Full", "Compact"])
def test_tray_exit_without_words_does_not_ask(qapp, monkeypatch, full):
    manager, inserted = prepare(full, "empty")
    ask = Mock()
    monkeypatch.setattr(quit_draft_dialog_qt, "ask_quit_draft", ask)
    tray_exit(qapp, manager, full).assert_called_once_with()
    ask.assert_not_called()
    assert inserted == []


@pytest.mark.parametrize("full", [True, False], ids=["Full", "Compact"])
@pytest.mark.parametrize("state", ["staged", "kept", "paused hold"])
def test_failed_quit_send_preserves_words_and_cancels_exit(qapp, monkeypatch, full, state):
    manager, inserted = prepare(full, state)
    manager._inject_fn = lambda *args: False
    monkeypatch.setattr(quit_draft_dialog_qt, "ask_quit_draft", lambda: "send")
    tray_exit(qapp, manager, full).assert_not_called()
    assert (manager.dictate_pending_buffer or manager.retained_draft) == TEXT
    assert kept_session_draft.quit_draft_waiting(None, manager)
    assert inserted == []


@pytest.mark.parametrize("full", [True, False], ids=["Full", "Compact"])
def test_question_failure_preserves_live_staged_draft(qapp, monkeypatch, full):
    manager, _ = prepare(full, "staged")
    monkeypatch.setattr(quit_draft_dialog_qt, "ask_quit_draft",
                        Mock(side_effect=RuntimeError("Synthetic dialog failure")))
    tray_exit(qapp, manager, full).assert_not_called()
    assert manager.dictate_pending_buffer == TEXT
    assert manager.mode is SessionMode.DICTATE


@pytest.mark.parametrize("text", ["", "   "])
def test_whitespace_and_absent_manager_are_not_waiting(text):
    manager = SimpleNamespace(retained_draft=text, dictate_pending_buffer=text,
                              has_parked_hold=True)
    assert not kept_session_draft.quit_draft_waiting(None, manager)
    assert not kept_session_draft.quit_draft_waiting(None, None)


@pytest.fixture
def pill(qapp):
    from samsara.ui.listening_indicator import ListeningIndicator
    widget = ListeningIndicator()
    widget.set_custom_position(qapp.primaryScreen().name(), .5, .5)
    widget.show()
    yield widget
    widget.destroy()
    widget.deleteLater()


def pointer(widget, kind, global_pos):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    local = QPointF(widget.mapFromGlobal(global_pos))
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
    event = QMouseEvent(kind, local, QPointF(global_pos), button, buttons, Qt.KeyboardModifier.NoModifier)
    widget.event(event)


def placement_handler(app):
    # Use the shipped save seam without importing or bootstrapping dictation.
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8-sig'))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == '_on_indicator_placement_committed')
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<placement>', 'exec'), namespace)
    return namespace[method.name].__get__(app)


def test_press_and_move_without_move_mode_moves_and_saves(pill, qapp):
    from PySide6.QtCore import QEvent, QPoint, Qt
    app = SimpleNamespace(update_config_and_save=Mock())
    handler = placement_handler(app)
    pill.placement_committed.connect(lambda payload: handler(payload))
    start = pill.mapToGlobal(pill._layout()[2].center().toPoint())
    before = pill.pos()
    assert not pill._pill_window.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert pill.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    pointer(pill, QEvent.Type.MouseButtonPress, start)
    pointer(pill, QEvent.Type.MouseMove, start + QPoint(80, -70))
    assert pill.pos() != before
    pointer(pill, QEvent.Type.MouseButtonRelease, start + QPoint(80, -70))
    app.update_config_and_save.assert_called_once()
    changes = app.update_config_and_save.call_args.args[0]
    assert changes['listening_indicator_position'] == 'custom'
    assert changes['listening_indicator_custom_position'] == pill._custom_position
    dropped = pill.pos()
    pill._reposition()
    assert pill.pos() == dropped
    assert not pill._unlocked


def test_plain_click_and_small_motion_do_not_move_or_save(pill, qapp):
    from PySide6.QtCore import QEvent, QPoint
    save = Mock()
    pill.placement_committed.connect(save)
    start = pill.mapToGlobal(pill._layout()[2].center().toPoint())
    before = pill.pos()
    pointer(pill, QEvent.Type.MouseButtonPress, start)
    pointer(pill, QEvent.Type.MouseMove, start + QPoint(qapp.startDragDistance() - 1, 0))
    pointer(pill, QEvent.Type.MouseButtonRelease, start)
    assert pill.pos() == before
    assert not pill._dragging
    save.assert_not_called()


def test_transparent_corners_and_receipt_gap_are_outside_input_region(pill):
    from PySide6.QtCore import QPoint, QPointF
    pill.show_outcome('staged', 'pending', None)
    _, _, body, chip = pill._layout()
    corner = body.topLeft().toPoint()
    gap = QPoint(int(body.center().x()), int(min(body.bottom(), chip.bottom()) + 2))
    assert pill.rect().contains(corner) and pill.rect().contains(gap)
    for point in (corner, gap, chip.center().toPoint()):
        assert not pill._begin_pointer(QPointF(point), QPointF(pill.mapToGlobal(point)))
    target = pill._pill_window
    assert target.rect().contains(QPoint(0, 0))
    assert not target.mask().contains(QPoint(0, 0))
    assert target.mask().contains(target.rect().center())
    assert target.size() == body.size().toSize()
    from PySide6.QtCore import Qt
    assert pill.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert target.isVisible()


def test_receipt_only_window_accepts_no_input(pill):
    from PySide6.QtCore import Qt
    pill.hide()
    pill.show_outcome('staged', 'pending', None)
    assert pill._chip_only
    assert pill.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert not pill._pill_window.isVisible()


def test_receipts_and_docking_do_not_reposition_pill_during_drag(pill):
    from PySide6.QtCore import QEvent, QPoint
    start = pill.mapToGlobal(pill._layout()[2].center().toPoint())
    pointer(pill, QEvent.Type.MouseButtonPress, start)
    pointer(pill, QEvent.Type.MouseMove, start + QPoint(60, 40))
    before = pill.mapToGlobal(pill._layout()[2].topLeft().toPoint())
    pill.show_outcome('A longer receipt that changes the window width', 'pending', None)
    pill._reposition()
    assert pill.mapToGlobal(pill._layout()[2].topLeft().toPoint()) == before
    assert pill._pill_window.geometry().topLeft() == before
    pill.clear_outcome()
    assert pill.mapToGlobal(pill._layout()[2].topLeft().toPoint()) == before
    pointer(pill, QEvent.Type.MouseButtonRelease, start + QPoint(60, 40))


def test_drag_snaps_to_top_center_and_saves_existing_preset(pill, qapp):
    from PySide6.QtCore import QEvent, QPoint
    save = Mock()
    pill.placement_committed.connect(save)
    area = qapp.primaryScreen().availableGeometry()
    start = pill.mapToGlobal(pill._layout()[2].center().toPoint())
    grab_offset = start - pill.pos()
    drop = QPoint(area.x() + (area.width() - pill.width()) // 2, area.y() + 1) + grab_offset
    pointer(pill, QEvent.Type.MouseButtonPress, start)
    pointer(pill, QEvent.Type.MouseMove, drop)
    pointer(pill, QEvent.Type.MouseButtonRelease, drop)
    save.assert_called_once_with({'type': 'preset', 'position': 'top-center'})
    assert pill._corner == 'top-center'


@pytest.mark.parametrize('offset', [-5000, 5000])
def test_drag_release_clamps_offscreen_drop(pill, qapp, offset):
    from PySide6.QtCore import QEvent, QPoint
    start = pill.mapToGlobal(pill._layout()[2].center().toPoint())
    pointer(pill, QEvent.Type.MouseButtonPress, start)
    pointer(pill, QEvent.Type.MouseMove, start + QPoint(offset, offset))
    pointer(pill, QEvent.Type.MouseButtonRelease, start + QPoint(offset, offset))
    assert qapp.primaryScreen().availableGeometry().contains(pill.geometry())


def test_touch_press_and_move_drags_and_commits(pill, qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    device = QTest.createTouchDevice()
    save = Mock()
    pill.placement_committed.connect(save)
    target = pill._pill_window
    qapp.processEvents()  # Deliver show/window creation before native touch injection.
    local = target.rect().center()
    before = pill.pos()
    QTest.touchEvent(target, device).press(0, local, target).commit()
    qapp.processEvents()
    QTest.touchEvent(target, device).move(0, local + QPoint(60, -40), target).commit()
    qapp.processEvents()
    assert pill.pos() != before
    QTest.touchEvent(target, device).release(0, local, target).commit()
    qapp.processEvents()
    save.assert_called_once()


@pytest.mark.parametrize('display', ['full', 'compact'])
def test_tools_has_no_move_entry_in_either_display(qapp, display):
    from samsara.ui.tray_qt import SamsaraTrayQt
    from tests.test_tray_qt import _submenu
    app = _make_app()
    app.config['ui'] = {'listening_display': display}
    tray = SamsaraTrayQt(app)
    tray._rebuild_menu()
    try:
        assert 'Move listening indicator...' not in {a.text() for a in _submenu(tray._menu, 'Tools').actions()}
        app.enter_indicator_move_mode.assert_not_called()
    finally:
        tray.stop()
        tray._menu.deleteLater()
        tray._tray.deleteLater()


@pytest.mark.parametrize('label', ['staged', 'Kept draft'])
def test_bottom_receipt_drop_saves_pill_origin_and_reshows_it(pill, qapp, label):
    from PySide6.QtCore import QEvent, QPoint
    pill.set_position('bottom-center')
    pill.show_outcome(label, 'pending', None)
    body = pill._layout()[2]
    # 775 / 890: the receipt grows to its measured two-line height.
    receipt = pill._layout()[3]
    assert body.y() == receipt.bottom() + 6
    offset = QPoint(24, 18)
    start = pill.mapToGlobal(body.topLeft().toPoint()) + offset
    area = qapp.primaryScreen().availableGeometry()
    drop = area.center()
    save = Mock()
    pill.placement_committed.connect(save)
    pointer(pill, QEvent.Type.MouseButtonPress, start)
    pointer(pill, QEvent.Type.MouseMove, drop)
    pointer(pill, QEvent.Type.MouseButtonRelease, drop)
    expected = drop - offset
    assert pill.mapToGlobal(pill._layout()[2].topLeft().toPoint()) == expected
    assert pill._layout()[2].y() == 0  # Receipt now grows below the pill.
    payload = save.call_args.args[0]
    pill.hide()
    pill.set_custom_position(payload['screen'], payload['cx'], payload['cy'])
    pill.show()
    assert pill.mapToGlobal(pill._layout()[2].topLeft().toPoint()) == expected


@pytest.mark.parametrize('cause', ['hide', 'deactivate', 'ungrab', 'timeout', 'monitor'])
def test_lost_release_recovers_docking(pill, qapp, cause):
    from PySide6.QtCore import QEvent, QPoint
    start = pill.mapToGlobal(pill._layout()[2].center().toPoint())
    pointer(pill, QEvent.Type.MouseButtonPress, start)
    pointer(pill, QEvent.Type.MouseMove, start + QPoint(50, 50))
    assert pill._press_origin is not None
    if cause == 'hide':
        pill.hide()
    elif cause in ('deactivate', 'ungrab'):
        kind = QEvent.Type.WindowDeactivate if cause == 'deactivate' else QEvent.Type.UngrabMouse
        qapp.sendEvent(pill._pill_window, QEvent(kind))
    elif cause == 'timeout':
        assert pill._drag_watchdog.interval() == 4000
        pill._drag_watchdog.timeout.emit()
    else:
        qapp.screenRemoved.emit(qapp.primaryScreen())
    assert pill._press_origin is None
    assert not pill._dragging
    assert not pill._drag_watchdog.isActive()
    pill.set_position('top-left')
    pill.show()
    assert pill._pill_window.geometry().topLeft() == qapp.primaryScreen().availableGeometry().topLeft() + QPoint(8, 8)


def test_receipt_transitions_do_not_change_visible_window_flags(pill, qapp):
    flags = pill.windowFlags(), pill._pill_window.windowFlags()
    hide = Mock()
    pill._pill_window.installEventFilter(_HideObserver(pill._pill_window, hide))
    pill.show_outcome('staged', 'pending', None)
    pill.clear_outcome()
    assert (pill.windowFlags(), pill._pill_window.windowFlags()) == flags
    hide.assert_not_called()
    pill.hide()
    pill.show_outcome('staged', 'pending', None)
    assert pill.windowFlags() == flags[0]
    pill.show()
    assert (pill.windowFlags(), pill._pill_window.windowFlags()) == flags


from PySide6.QtCore import QObject


class _HideObserver(QObject):
    def __init__(self, parent, hidden):
        super().__init__(parent)
        self.hidden = hidden

    def eventFilter(self, watched, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Type.Hide:
            self.hidden()
        return False


def test_native_pill_events_drag_without_keyboard_focus(pill, qapp):
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtWidgets import QLineEdit
    edit = QLineEdit()
    edit.show()
    edit.setFocus()
    qapp.processEvents()
    target = pill._pill_window
    save = Mock()
    pill.placement_committed.connect(save)
    start = target.mapToGlobal(target.rect().center())
    assert target.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert target.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert target.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    pointer(target, QEvent.Type.MouseButtonPress, start)
    pointer(target, QEvent.Type.MouseMove, start + QPoint(70, -45))
    pointer(target, QEvent.Type.MouseButtonRelease, start + QPoint(70, -45))
    save.assert_called_once()
    assert not target.hasFocus()
    assert qapp.focusWidget() is edit
    edit.hide()
    edit.deleteLater()


def test_modal_dialog_blocks_pill_drag(pill, qapp):
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtWidgets import QDialog
    dialog = QDialog()
    dialog.setModal(True)
    dialog.show()
    qapp.processEvents()
    before = pill.pos()
    start = pill._pill_window.mapToGlobal(pill._pill_window.rect().center())
    pointer(pill._pill_window, QEvent.Type.MouseButtonPress, start)
    pointer(pill._pill_window, QEvent.Type.MouseMove, start + QPoint(80, 50))
    pointer(pill._pill_window, QEvent.Type.MouseButtonRelease, start + QPoint(80, 50))
    assert pill._press_origin is None
    assert pill.pos() == before
    dialog.hide()
    dialog.deleteLater()


def test_render_and_grab_include_receipt_without_mask_shift(pill):
    from PySide6.QtGui import QImage, QColor
    from samsara.ui import theme
    pill.show_outcome('MISS', 'error', None)
    image = QImage(pill.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    pill.render(image)
    grabbed = pill.grab().toImage()
    assert grabbed.size() == image.size()
    _, _, body, chip = pill._layout()
    for picture in (image, grabbed):
        sample = picture.pixelColor(int(chip.x()+4), int(chip.center().y()))
        expected = QColor(theme.ERROR)
        assert sample == expected
        assert picture.pixelColor(int(body.x()+4), int(body.center().y())).alpha() > 0
    assert pill.mask().isEmpty()
    assert not pill._capture_composite


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('state', ['staged', 'kept', 'paused hold'])
def test_failed_send_shows_existing_failure_receipt(monkeypatch, full, state):
    manager, _ = prepare(full, state)
    manager._inject_fn = lambda *args: False
    app = SimpleNamespace(_session_mode_manager=manager, _show_outcome_chip=Mock())
    monkeypatch.setattr(quit_draft_dialog_qt, 'ask_quit_draft', lambda: 'send')
    assert not quit_method()(app)
    app._show_outcome_chip.assert_called_once_with('Delivery failed; draft kept', 'error')
    assert (manager.retained_draft or manager.dictate_pending_buffer) == TEXT


@pytest.mark.parametrize('route', ['archive', 'installer'])
@pytest.mark.parametrize('allowed', [False, True])
def test_update_resolves_draft_before_helper(qapp, monkeypatch, tmp_path, allowed, route):
    from samsara.ui import update_qt
    events, scheduled = [], []
    def decision():
        events.append('decision')
        return allowed
    app = SimpleNamespace(_resolve_parked_draft_before_quit=decision, quit_app=Mock())
    release = SimpleNamespace(version='0.22.1', asset_size=100)
    from samsara import updater, update_installer
    prepared_class = update_installer.PreparedInstaller if route == 'installer' else updater.PreparedUpdate
    prepared = object.__new__(prepared_class)
    object.__setattr__(prepared, 'install_dir', tmp_path)
    archive_helper = Mock(return_value='archive helper')
    installer_helper = Mock(return_value='installer helper')
    monkeypatch.setattr(updater, '_archive_helper_script', archive_helper)
    monkeypatch.setattr(update_installer, 'helper', installer_helper)
    monkeypatch.setattr(update_installer, 'archive_guard', lambda *_: None)
    def launch_route(prepared, *, current_pid):
        events.append(updater._helper_script(prepared, current_pid))
    launch = Mock(side_effect=launch_route)
    monkeypatch.setattr(update_qt, 'launch_prepared_update', launch)
    # Replacing the bound native method itself caused the crash fixed by 555.
    monkeypatch.setattr(update_qt, 'QTimer', SimpleNamespace(
        singleShot=lambda delay, callback: scheduled.append(callback)))
    dialog = update_qt._UpdateDialog(app, initial_release=release)
    dialog._phase = 'preparing'
    dialog._on_prepare_finished(prepared, '')
    if allowed:
        assert events == ['decision', route + ' helper']
        assert len(scheduled) == 1
        scheduled[0]()
        app.quit_app.assert_called_once_with(_restart_draft_resolved=True)
    else:
        assert events == ['decision']
        launch.assert_not_called()
        assert scheduled == []
        app.quit_app.assert_not_called()
        assert dialog._phase == 'available'
        assert dialog._primary.isEnabled()
        archive_helper.assert_not_called()
        installer_helper.assert_not_called()
    dialog.hide()
    dialog.deleteLater()


@pytest.mark.parametrize('route', ['tray', 'voice'])
def test_keep_cancels_restart_before_any_helper(qapp, monkeypatch, route):
    from samsara import app_restart
    from samsara.ui.tray_qt import SamsaraTrayQt
    from plugins.commands import core_utils
    from samsara.ui import qt_runtime
    app = _make_app()
    app._resolve_parked_draft_before_quit = Mock(return_value=False)
    app.quit_app = Mock()
    tray = SamsaraTrayQt(app)
    app.tray_icon = tray
    helper = Mock()
    monkeypatch.setattr(app_restart, 'start_handoff', helper)
    monkeypatch.setattr(app_restart, 'build_relaunch', Mock())
    monkeypatch.setattr(qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(qt_runtime, 'post', lambda action: action())
    if route == 'voice':
        core_utils.restart_app(app)
    else:
        tray._restart_samsara()
    helper.assert_not_called()
    app.quit_app.assert_not_called()
    assert not tray._restart_pending
    tray.stop()
    tray._tray.deleteLater()


def test_showing_native_pill_and_receipt_preserves_editor_focus(qapp):
    from PySide6.QtWidgets import QLineEdit
    from samsara.ui.listening_indicator import ListeningIndicator
    widget = ListeningIndicator()
    editor = QLineEdit()
    try:
        editor.show()
        editor.activateWindow()
        editor.setFocus()
        qapp.processEvents()
        assert qapp.focusWidget() is editor
        widget.show()
        qapp.processEvents()
        assert qapp.focusWidget() is editor
        widget.show_outcome('staged', 'pending', None)
        qapp.processEvents()
        assert qapp.focusWidget() is editor
        widget.hide()
        qapp.processEvents()
        assert qapp.focusWidget() is editor
    finally:
        widget.destroy()
        widget.deleteLater()
        editor.hide()
        editor.deleteLater()


def test_right_click_and_wheel_are_swallowed_without_a_gesture(pill, qapp):
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent, QWheelEvent, QContextMenuEvent
    target = pill._pill_window
    local = QPointF(target.rect().center())
    global_pos = QPointF(target.mapToGlobal(local.toPoint()))
    before = pill.pos()
    events = [
        QMouseEvent(QEvent.Type.MouseButtonPress, local, global_pos,
                    Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
                    Qt.KeyboardModifier.NoModifier),
        QContextMenuEvent(QContextMenuEvent.Reason.Mouse, local.toPoint(), global_pos.toPoint()),
        QWheelEvent(local, global_pos, QPoint(), QPoint(0,120),
                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.NoScrollPhase, False)]
    for event in events:
        qapp.sendEvent(target, event)
        assert event.isAccepted()
    assert pill.pos() == before
    assert pill._press_origin is None


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('finished', [False, True], ids=['just-sent', 'finished'])
def test_sent_words_and_finished_session_quit_without_question(qapp, monkeypatch, full, finished):
    manager, inserted = prepare(full, 'staged')
    assert manager.commit_pending_dictation().kind == 'dictate_committed'
    if finished:
        end_session(manager)
    ask = Mock()
    monkeypatch.setattr(quit_draft_dialog_qt, 'ask_quit_draft', ask)
    tray_exit(qapp, manager, full).assert_called_once_with()
    ask.assert_not_called()
    assert inserted == [TEXT]
    assert not manager.dictate_pending_buffer and not manager.retained_draft


def test_destroy_retires_native_pill_and_allows_safe_reshow(pill):
    import shiboken6
    previous = pill._pill_window
    pill.destroy()
    assert not shiboken6.isValid(previous)
    assert pill._pill_window is None
    pill.show()
    assert pill._pill_window is not previous
    assert pill._pill_window.isVisible()
