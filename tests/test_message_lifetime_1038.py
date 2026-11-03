"""1038: real display handoffs, persistent problems and readable auxiliary notices."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QMainWindow, QPushButton, QWidget

from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.model import LiveSurfaceModel
from samsara.message_durations import message_duration_ms
from samsara.message_lifetime import (MessageSlot, consume_acknowledgement,
    take_acknowledgement, retire_acknowledgement)
from samsara.ui.listening_indicator import ListeningIndicator


@pytest.mark.parametrize('length,expected', [(0,2500), (20,2500), (21,2560), (50,4300), (100,7300), (145,10000), (200,10000)])
def test_reading_time(length, expected):
    assert message_duration_ms('x'*length, 'accent', 1) == expected
    assert message_duration_ms('x'*length, 'success', 99999) == expected


@pytest.fixture(params=['Full', 'Compact'])
def display(request, qapp, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}})
    if request.param == 'Full':
        from samsara.ui.live_surface_qt import LiveSurfaceWidget
        controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: clock[0]),
            widget_factory=lambda c: LiveSurfaceWidget(c, reduced_motion=True))
        controller.start()
        indicator = LiveSurfaceIndicatorAdapter(controller)
        flush = controller.drain
        text = lambda: controller.view().notice.message
        expire = controller.refresh_expired
        band = lambda: controller.widget._chip or controller.widget._band
    else:
        controller = None
        indicator = ListeningIndicator()
        indicator.set_idle_animation(False)
        monkeypatch.setattr(indicator, '_now_ms', lambda: round(clock[0]*1000))
        flush = lambda: None
        text = lambda: indicator._chip_label or ''
        expire = indicator._clear_outcome
        band = lambda: indicator._message_window
    app.listening_indicator = indicator
    app._schedule_ui = lambda fn,*args: fn(*args)
    def show(label, kind='accent'):
        indicator.show_outcome(label, kind, 1)
        flush()
        qapp.processEvents()
    yield SimpleNamespace(app=app, i=indicator, c=controller, clock=clock, show=show,
                          flush=flush, text=text, expire=expire, band=band)
    if controller:
        controller.close()
    else:
        indicator.destroy()
        indicator.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize('kind', ['error','warning'])
def test_problem_survives_timer_clear_and_hide_then_click_releases_waiter(display, qapp, kind):
    d = display
    d.show('Synthetic problem', kind)
    d.show('Later information')
    for seconds in (1, 11, 600):
        d.clock[0] = seconds
        d.i.clear_outcome()
        d.i.hide()
        d.flush()
        d.expire()
        assert d.text() == 'Synthetic problem'
    QTest.mouseClick(d.band(), Qt.MouseButton.LeftButton)
    d.flush()
    assert d.text() == 'Later information'
    d.clock[0] += 2.499
    d.expire()
    assert d.text() == 'Later information'
    d.clock[0] += .001
    d.expire()
    assert d.text() == ''


def test_information_queues_and_never_goes_blank_early(display):
    d = display
    d.show('First information')
    d.clock[0] = .001
    d.show('Latest information')
    d.i.clear_outcome(); d.i.hide(); d.flush()
    assert d.text() == 'First information'
    d.clock[0] = 2.5
    d.expire()
    assert d.text() == 'Latest information'
    d.clock[0] = 5
    d.expire()
    assert d.text() == ''


def test_queued_problem_cannot_be_lost_to_later_information(display):
    d = display
    d.show('First problem', 'error')
    d.show('Second problem', 'warning')
    d.show('Later information')
    d.i.dismiss_error(); d.flush()
    assert d.text() == 'Second problem'
    d.clock[0] = 600; d.expire()
    assert d.text() == 'Second problem'
    d.i.dismiss_error(); d.flush()
    assert d.text() == 'Later information'


@pytest.mark.parametrize('kind', ['error','warning'])
def test_next_capture_retires_problem_and_old_waiter(display, kind):
    d = display
    d.show('Previous problem', kind)
    d.show('Old background information')
    d.i.set_listening(True); d.flush()
    d.show('New action result', 'success')
    assert d.text() == 'New action result'
    d.clock[0] = 30
    d.expire()
    assert d.text() == ''


def test_long_error_has_full_hover_accessibility_and_single_announcement(display, qapp, monkeypatch):
    d, announcements = display, []
    monkeypatch.setattr(QAccessible, 'updateAccessibility', lambda e: announcements.append(e.message()))
    text = ('The requested command could not run because the target window is unavailable. '
            'Open the window and try again. Your draft is safe, and nothing was sent to another application. ')
    text = (text + 'Please check the selected window.')[:200]
    assert len(text) == 200
    d.show(text, 'error')
    assert d.band().toolTip() == text
    assert text in (d.band().accessibleName() + d.band().accessibleDescription())
    if d.c:
        # 1038 FINISH IT 2: full visible text, without relying on hover.
        assert not d.band().elided
        assert ' '.join(d.band().label.text().split()) == ' '.join(text.split())
    for _ in range(3):
        d.band().update(); qapp.processEvents(); d.expire()
    assert sum(text in message for message in announcements) == 1
    d.clock[0] = 120
    d.expire()
    assert d.text() == text


@pytest.mark.parametrize('kind', ['error', 'warning'])
@pytest.mark.parametrize('length', [200, 800])
def test_problem_expands_to_fit_every_painted_line_then_shrinks(display, qapp, monkeypatch, kind, length):
    from samsara.ui import theme, result_message_rendering
    from samsara.ui.indicator_message_layout import message_lines, message_line_spacing
    from PySide6.QtCore import QRectF
    d = display
    d.show('Ready', 'success')
    normal_height = d.c.widget.height() if d.c else d.i.height()
    # Explicit newlines and an unbroken token exercise both wrap paths.
    text = ('A synthetic command failed.\n' + 'abcdefghij'*8 + ' Please try again. ')*20
    text = text[:length]
    d.clock[0] = 20; d.expire()
    d.show(text, kind)
    if d.c:
        band = d.band()
        visible_text = text if band.chip else kind + ': ' + text
        assert not band.elided
        assert ' '.join(band.label.text().split()) == ' '.join(visible_text.split())
        lines, _ = message_lines(visible_text, band.label.font(), band.label.width(), max_lines=None)
        assert band.label.height() >= len(lines)*message_line_spacing(visible_text, band.label.font())
        assert d.c.widget.rect().contains(band.geometry())
    else:
        painted = []
        original = result_message_rendering.paint_result
        def record(painter, rect, line, font, ink):
            painted.append((QRectF(rect), line, ink.name()))
            return original(painter, rect, line, font, ink)
        monkeypatch.setattr(result_message_rendering, 'paint_result', record)
        window = d.band()
        image = window.grab()
        assert not image.isNull() and painted
        assert ' '.join(line for _, line, _ in painted) == ' '.join(text.split())
        assert all(QRectF(window.rect()).contains(rect) for rect, _, _ in painted)
        fill = theme._mix(theme.ERROR if kind == 'error' else theme.WARNING, theme.BG0, .80)
        assert all(theme.contrast_ratio(ink, fill) >= 4.5 for _, _, ink in painted)
    d.i.dismiss_error(); d.flush(); qapp.processEvents()
    d.show('Ready', 'success')
    assert (d.c.widget.height() if d.c else d.i.height()) == normal_height


@pytest.mark.parametrize('word', ['dismiss','okay','OK.'])
def test_spoken_acknowledgement_is_scoped_to_the_problem_capture(display, word):
    d = display
    assert not consume_acknowledgement(d.app, word)
    d.show('Synthetic problem', 'warning')
    d.i.set_listening(True); d.flush()
    receipt = take_acknowledgement(d.app)
    d.i.set_listening(False); d.flush()  # Key release precedes final decoding.
    assert consume_acknowledgement(d.app, word, receipt=receipt)
    assert not d.text()
    assert not consume_acknowledgement(d.app, word, receipt=receipt)


@pytest.mark.parametrize('ending', ['cancelled', 'empty', 'dictation', 'replacement'])
@pytest.mark.parametrize('word', ['okay', 'ok', 'dismiss'])
@pytest.mark.parametrize('route', ['command_key', 'ava_hold'])
def test_intervening_capture_does_not_swallow_later_acknowledgement(display, cancel_capture, ending, word, route):
    from samsara.ava_pending import consume

    d = display
    d.show('Synthetic problem', 'error')
    d.i.set_listening(True); d.flush()
    if ending == 'cancelled':
        cancel_capture(d.app)
    elif ending == 'dictation':
        # Ordinary dictation bypasses ava_pending.consume entirely.
        take_acknowledgement(d.app, enabled=False)
    elif ending == 'empty':
        receipt = take_acknowledgement(d.app)
        retire_acknowledgement(d.app, receipt)
    # A replacement may arrive without any consume call for its predecessor.
    d.i.set_listening(False); d.flush()
    d.i.set_listening(True); d.flush()
    receipt = take_acknowledgement(d.app)
    d.i.set_listening(False); d.flush()
    normal_route = Mock()
    if not consume(d.app, word, capture_path=route, acknowledgement=receipt):
        normal_route(word)
    normal_route.assert_called_once_with(word)


@pytest.fixture(scope='module')
def cancel_capture():
    """Exercise the production cancellation seam without importing bootstrap."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DictationApp')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_cancel_recording_impl')
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'dictation.py', 'exec'), namespace)
    def cancel(app):
        app.recording = False
        app._streaming_session = None
        namespace['_cancel_recording_impl'](app)
    return cancel


def test_replacement_capture_rejects_delayed_receipt_and_retains_its_own(display):
    d = display
    d.show('First problem', 'error')
    d.i.set_listening(True); d.flush()
    old = take_acknowledgement(d.app)
    d.i.set_listening(False); d.flush()
    d.show('Second problem', 'warning')
    d.i.set_listening(True); d.flush()
    current = take_acknowledgement(d.app)
    # A delayed result and its finalizer cannot consume/retire a newer capture.
    assert not consume_acknowledgement(d.app, 'okay', receipt=old)
    retire_acknowledgement(d.app, old)
    assert consume_acknowledgement(d.app, 'okay', receipt=current)


def test_unrelated_route_cannot_borrow_active_capture_receipt(display):
    from samsara.ava_pending import consume

    d = display
    d.show('Synthetic problem', 'error')
    d.i.set_listening(True); d.flush()
    assert not consume(d.app, 'okay', capture_path='wake')
    receipt = take_acknowledgement(d.app)
    assert consume(d.app, 'okay', capture_path='command_key', acknowledgement=receipt)


def test_non_acknowledgement_retires_one_use_receipt(display):
    d = display
    d.show('Synthetic problem', 'error')
    d.i.set_listening(True); d.flush()
    receipt = take_acknowledgement(d.app)
    assert not consume_acknowledgement(d.app, 'open calculator', receipt=receipt)
    assert not consume_acknowledgement(d.app, 'okay', receipt=receipt)


def test_status_strip_uses_same_queue_and_cannot_lose_error_on_clear(qapp):
    from samsara.ui.message_status_qt import MessageStatusBar
    from samsara.failure_reasons import _home_message
    window = QMainWindow()
    bar = MessageStatusBar(window)
    window.setStatusBar(bar)
    clock = [0.0]; bar.messages.clock = lambda: clock[0]
    app = SimpleNamespace(main_window=SimpleNamespace(_window=window))
    window.show()
    try:
        _home_message(app, 'Capture failed', None)
        bar.showMessage('Later information', 1)
        clock[0] = 600
        bar.clearMessage(); bar._render()
        assert bar.currentMessage() == 'Capture failed'
        assert bar._label.toolTip() == bar._label.accessibleName() == 'Capture failed'
        bar.dismiss()
        assert bar.currentMessage() == 'Later information'
        assert bar.messages.current.deadline == 602.5
        clock[0] = 602.5; bar._render()
        assert not bar.currentMessage()
    finally:
        window.close(); window.deleteLater(); qapp.processEvents()


def test_home_next_action_dismisses_both_problem_and_status(qapp):
    from samsara.ui.home_notice_qt import build_problem_notice, refresh_problem_notice
    from samsara.ui.message_status_qt import MessageStatusBar
    window = QMainWindow()
    window.setStatusBar(MessageStatusBar(window))
    content = QWidget(window); window.setCentralWidget(content)
    card, label, button = build_problem_notice(8, lambda: QWidget(content),
        lambda text, **_: QLabel(text), lambda text, **_: QPushButton(text), Mock())
    outside = QPushButton('Next action', content); outside.move(200,200)
    notice = SimpleNamespace(text='Synthetic error', capability='mic', evidence='synthetic',
                             action_label='Troubleshoot', action='audio')
    window.resize(500,400); window.show()
    refresh_problem_notice(card, label, button, notice, 'dictation', 'Troubleshoot')
    window.statusBar().show_feedback('Synthetic status error', 'error')
    QTest.mouseClick(outside, Qt.MouseButton.LeftButton)
    assert card.isHidden() and not window.statusBar().currentMessage()
    window.close(); window.deleteLater(); qapp.processEvents()


def test_home_problem_stays_through_poll_and_can_be_dismissed(qapp):
    from samsara.ui.home_notice_qt import build_problem_notice, refresh_problem_notice
    card, label, button = build_problem_notice(8, QWidget,
        lambda text, **_: QLabel(text), lambda text, **_: QPushButton(text), Mock())
    notice = SimpleNamespace(text='Synthetic microphone failure', capability='mic',
                             evidence='synthetic', action_label='Fix microphone', action='audio')
    assert refresh_problem_notice(card, label, button, notice, 'dictation', 'Troubleshoot') is notice
    assert refresh_problem_notice(card, label, button, None, 'dictation', 'Troubleshoot') is notice
    assert not card.isHidden() and label.toolTip() == notice.text
    dismiss = next(b for b in card.findChildren(QPushButton) if b.text() == 'Dismiss')
    dismiss.click()
    refresh_problem_notice(card, label, button, None, 'dictation', 'Troubleshoot')
    assert card.isHidden()
    card.deleteLater()


def test_windows_warning_has_persistent_in_app_copy_even_with_balloon_off(qapp):
    from samsara.notify_gate import TrayNotifications
    for preference in ('off', 'important', 'all'):
        indicator = SimpleNamespace(show_outcome=Mock(), show_feedback=Mock())
        app = SimpleNamespace(config={'notifications_level':preference}, listening_indicator=indicator)
        app._schedule_ui = lambda fn,*args: fn(*args)
        tray = SimpleNamespace(_app=app, _tray=Mock())
        TrayNotifications._deliver_notification(tray, 'important', 'Warning', 'Synthetic outage', 2, 12000)
        assert app.listening_indicator.show_outcome.call_args.args[:3] == ('Synthetic outage', 'warning', None)
        assert tray._tray.showMessage.call_count == (0 if preference == 'off' else 1)


def test_hint_timer_uses_shared_reading_time(qapp):
    from samsara.ui.hint_toast import HintToast
    text = 'Synthetic hint ' * 20
    toast = HintToast(text)
    try:
        toast.show()
        assert toast._auto_timer.interval() == message_duration_ms(text, 'accent')
    finally:
        toast._auto_timer.stop(); toast.close(); toast.deleteLater()


def test_message_slot_next_action_drops_old_background_work():
    slot = MessageSlot(clock=lambda: 0)
    slot.offer('Problem', 'error')
    slot.offer('Background result')
    slot.dismiss(next_action=True)
    assert slot.current is None and not slot.waiting


def test_footer_and_status_preserve_queued_problems():
    from samsara.message_durations import ReceiptDwell
    shown = []
    footer = ReceiptDwell(lambda text, success: shown.append(text), lambda *_: None)
    footer.show('First failure', False)
    footer.show('Second failure', False)
    footer.show('Later information')
    footer.dismiss()
    assert shown[-1] == 'Second failure' and footer.restore()
    footer.dismiss()
    assert shown[-1] == 'Later information'
    slot = MessageSlot(clock=lambda: 0)
    slot.offer('First failure', 'error'); slot.offer('Second failure', 'warning')
    slot.offer('Later information'); slot.dismiss()
    assert slot.current.text == 'Second failure'
    slot.dismiss()
    assert slot.current.text == 'Later information'


@pytest.mark.parametrize('kind', ['SUCCESS', 'WARNING', 'ERROR'])
def test_changed_draft_replaces_previous_receipt_without_blank_or_stale_waiters(kind):
    from samsara.live_surface.model import EventKind, NoticeKind, SurfaceEvent

    clock = [0.0]
    model = LiveSurfaceModel(clock=lambda: clock[0])
    def project(revision):
        return model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED,
            document_id='synthetic-action', revision=revision))
    def receipt(text, notice):
        model.consume(SurfaceEvent(EventKind.NOTICE, notice=notice, message=text))

    project(1)
    receipt('Previous action receipt', NoticeKind[kind])
    project(1)  # Polling the same draft must not dismiss an unread receipt.
    receipt('Old background information', NoticeKind.INFORMATION)
    assert model.view().notice.message == 'Previous action receipt'
    assert not project(0)  # A stale producer update is not a new action either.
    receipt('More old background information', NoticeKind.INFORMATION)
    assert model.view().notice.message == 'Previous action receipt'
    project(2)
    assert model.view().notice.message == 'Previous action receipt'  # No blank interval.
    receipt('New action receipt', NoticeKind.SUCCESS)
    assert model.view().notice.message == 'New action receipt'
    clock[0] = 100
    assert model.view().notice.kind is NoticeKind.NONE  # No stale queued work returns.
