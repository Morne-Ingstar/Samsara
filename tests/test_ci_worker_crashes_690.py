"""Owned Qt destruction and closed-session dispatch regressions (690)."""
from contextlib import nullcontext

import pytest
from PySide6.QtCore import QPropertyAnimation, QRect, QTimer
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from samsara.live_surface.model import VisibleForm
from samsara.ui import theme
from tests import test_command_scope_68 as command_scope
from tests import test_live_surface_qt as surface_tests
from tests import test_recording_mark_455 as recording_tests
from tests import test_wake_session_policy as wake_tests


@pytest.mark.parametrize("fail", (False, True))
def test_owned_widget_stops_activity_and_deletes_even_after_assertion(
        qapp, qt_widget_scope, qt_deferred_delete, monkeypatch, fail):
    widget = QWidget()
    timer = QTimer(widget)
    timer.start(0)
    animation = QPropertyAnimation(widget, b"geometry", widget)
    animation.setStartValue(widget.geometry())
    animation.setEndValue(QRect(10, 10, 120, 120))
    animation.setDuration(10000)
    animation.start()
    other = QWidget()
    ticks = []
    other_timer = QTimer(other)
    other_timer.timeout.connect(lambda: ticks.append(None))
    other_timer.start(0)
    other.deleteLater()
    observed = []
    close = widget.close

    def checked_close():
        observed.append((not timer.isActive(),
                         animation.state() == QPropertyAnimation.State.Stopped))
        return close()

    monkeypatch.setattr(widget, "close", checked_close)
    destroyed = []
    widget.destroyed.connect(lambda *_: destroyed.append(True))
    try:
        expected = pytest.raises(AssertionError, match="synthetic assertion") if fail else nullcontext()
        with expected:
            with qt_widget_scope(widget):
                if fail:
                    raise AssertionError("synthetic assertion")
        assert observed == [(True, True)]
        assert destroyed == [True] and not isValid(widget)
        assert not isValid(timer) and not isValid(animation)
        # Cleanup must not run another test's queued deletes or callbacks.
        assert isValid(other) and other_timer.isActive() and not ticks
    finally:
        if isValid(widget):
            with qt_widget_scope(widget):
                pass
        if isValid(other):
            other_timer.stop()
            qt_deferred_delete(other)


def test_recording_compact_test_destroys_its_native_widget(qapp, qt_widget_scope, monkeypatch):
    created = []
    original = recording_tests.ListeningIndicator

    class ObservedIndicator(original):
        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(recording_tests, "ListeningIndicator", ObservedIndicator)
    recording_tests.test_compact_capture_selects_recording_without_changing_eye_or_spin(
        qapp, True, "DICTATE", qt_widget_scope)
    assert len(created) == 1 and not isValid(created[0])


def test_recording_palette_render_does_not_refresh_unrelated_windows(qapp, monkeypatch):
    from unittest.mock import Mock

    refresh = Mock()
    monkeypatch.setattr(theme, "refresh_all", refresh)
    previous = theme.active_theme()
    palette = "light" if previous == "dark" else "dark"
    recording_tests.test_large_recording_matches_listening_geometry_and_alpha(
        qapp, monkeypatch, palette, False, 32, 0)
    assert theme.active_theme() == previous
    refresh.assert_not_called()


@pytest.mark.parametrize("form", (VisibleForm.STATUS, VisibleForm.LIVE, VisibleForm.REVIEW))
def test_palette_card_is_shown_and_deleted_before_the_next_palette(
        qapp, qt_widget_scope, monkeypatch, form):
    created, grabbed = [], []
    original = surface_tests.LiveSurfaceWidget

    class ObservedSurface(original):
        def __init__(self, *args, **kwargs):
            assert all(not isValid(widget) for widget in created)
            super().__init__(*args, **kwargs)
            created.append(self)

        def grab(self, *args, **kwargs):
            assert self.isVisible()
            grabbed.append(self)
            return super().grab(*args, **kwargs)

    previous = theme.active_theme()
    monkeypatch.setattr(surface_tests, "LiveSurfaceWidget", ObservedSurface)
    try:
        surface_tests.test_card_has_a_distinct_surface_and_border_in_both_palettes(
            qapp, form, qt_widget_scope)
        assert len(created) == 2 and grabbed == created
        assert all(not isValid(widget) for widget in created)
    finally:
        theme.set_theme(previous)


def test_cheatsheet_test_deletes_its_window_when_an_assertion_fails(
        qapp, qt_widget_scope, tmp_path, monkeypatch):
    from samsara.ui import command_cheatsheet_qt as sheet

    created = []
    original = sheet._CheatSheetWindow

    class ObservedSheet(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    # Nonempty synthetic rows deliberately fail the first assertion without
    # triggering the window's fallback catalogue for an empty command list.
    from types import SimpleNamespace
    rows = [{"phrase": "synthetic", "canonical_id": "synthetic", "risk": "read", "scope": None}]
    executor = SimpleNamespace(_matcher=SimpleNamespace(list_commands=lambda: rows))
    monkeypatch.setattr(sheet, "_CheatSheetWindow", ObservedSheet)
    with pytest.raises(AssertionError):
        command_scope.test_cheatsheet_window_hides_not_live_and_says_how_many(
            qapp, executor, tmp_path, monkeypatch, qt_widget_scope)
    assert len(created) == 1 and not isValid(created[0])


def test_flush_test_restores_spawn_tracker_before_rig_teardown(monkeypatch):
    fixture = wake_tests.rig.__wrapped__(monkeypatch)
    make = next(fixture)
    tracker = wake_tests.wake_module.thread_registry.spawn
    try:
        wake_tests.test_flush_drops_capture_when_timer_closed_session_mid_frame(make, monkeypatch)
        assert wake_tests.wake_module.thread_registry.spawn is tracker
    finally:
        fixture.close()
