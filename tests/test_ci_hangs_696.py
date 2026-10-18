"""Real layout paths stay nonmodal and retire native owners before patches expire."""
from contextlib import nullcontext
from time import monotonic
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QAbstractAnimation, QCoreApplication, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QWidget
from shiboken6 import isValid

from samsara.ui import theme
from tests import test_full_display_clear_label_466 as full_tests
from tests import test_installed_updates_554 as update_tests
from tests import test_theme_switch_speed_667 as switch_tests
from tests import test_tutorial_done_layout_259 as tutorial_tests


@pytest.mark.parametrize("fail", (False, True))
def test_done_layout_is_nonmodal_bounded_and_destroyed(qapp, monkeypatch, qt_widget_scope, fail):
    created, slices = [], []
    native_events = QApplication.processEvents

    def forbidden(*args, **kwargs):
        pytest.fail("The done-page layout path entered a nested/modal event loop")

    def bounded_events(flags, milliseconds):
        assert 0 < milliseconds <= 5
        slices.append(milliseconds)
        assert len(slices) <= 3
        return native_events(flags, milliseconds)

    class ObservedTutorial(tutorial_tests.TutorialWindow):
        def __init__(self, *args):
            super().__init__(*args)
            created.append(self)

        def show(self):
            super().show()
            if fail:
                raise AssertionError("synthetic show failure")

    monkeypatch.setattr(tutorial_tests, "TutorialWindow", ObservedTutorial)
    for cls in (QCoreApplication, QApplication, QEventLoop, QDialog):
        monkeypatch.setattr(cls, "exec", forbidden)
        monkeypatch.setattr(cls, "exec_", forbidden)
    monkeypatch.setattr(QApplication, "processEvents", staticmethod(bounded_events))
    previous = theme.active_theme()
    started = monotonic()
    expected = pytest.raises(AssertionError, match="synthetic show failure") if fail else nullcontext()
    with expected:
        tutorial_tests.test_done_page_keeps_body_text_and_open_targets_unclipped(qapp, qt_widget_scope)
    assert monotonic() - started < 10
    assert len(created) == 1 and not isValid(created[0])
    assert theme.active_theme() == previous
    if not fail:
        assert slices


@pytest.mark.parametrize("fail", (False, True))
def test_updates_delete_only_the_owned_dialog_before_patches_expire(
        qapp, monkeypatch, qt_widget_scope, qt_deferred_delete, fail):
    from samsara.ui import update_qt

    created, destroyed, stopped = [], [], []
    other = QWidget()
    other.deleteLater()

    class ObservedDialog(update_qt._UpdateDialog):
        def __init__(self, *args):
            super().__init__(*args)
            created.append(self)
            self.probe_timer = QTimer(self)
            self.probe_timer.start(0)
            active_patch = update_qt.check_for_update
            self.destroyed.connect(lambda *_: destroyed.append(update_qt.check_for_update is active_patch))

        def findChild(self, *args):
            if fail:
                raise AssertionError("synthetic assertion failure")
            return super().findChild(*args)

        def closeEvent(self, event):
            stopped.append(not self.probe_timer.isActive())
            super().closeEvent(event)

    monkeypatch.setattr(update_qt, "_UpdateDialog", ObservedDialog)
    try:
        expected = pytest.raises(AssertionError, match="synthetic assertion failure") if fail else nullcontext()
        with expected:
            update_tests.test_updates_window_has_installer_link_and_no_network(qapp, monkeypatch, qt_widget_scope)
        assert len(created) == 1 and not isValid(created[0])
        assert stopped == [True] and destroyed == [True]
        assert isValid(other)
    finally:
        qt_deferred_delete(other)


@pytest.mark.parametrize("case", ("dark", "light", "latest", "parked"))
def test_full_label_paths_destroy_widgets_with_activity_stopped(
        qapp, monkeypatch, qt_widget_scope, case):
    created, stopped = [], []
    native = full_tests.LiveSurfaceWidget

    class ObservedSurface(native):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append((self, self.findChildren(QWidget)))

        def hide(self):
            if self.isVisible():
                stopped.append(all(not timer.isActive() for timer in self.findChildren(QTimer)) and
                               all(animation.state() == QAbstractAnimation.State.Stopped
                                   for animation in self.findChildren(QAbstractAnimation)))
            return super().hide()

    monkeypatch.setattr(full_tests, "LiveSurfaceWidget", ObservedSurface)
    if case in ("dark", "light"):
        full_tests.test_clear_label_and_padding_with_runtime_font(qapp, monkeypatch, qt_widget_scope, case)
    elif case == "latest":
        full_tests.test_latest_counter_uses_the_same_text_padding_rule(qapp, qt_widget_scope)
    else:
        full_tests.test_parked_draft_text_controls_fit_the_effective_font(qapp, qt_widget_scope)
    assert len(created) == 1
    widget, children = created[0]
    assert not isValid(widget) and all(not isValid(child) for child in children)
    assert stopped and all(stopped)


def test_scrollbar_switch_does_not_refresh_unrelated_window(qapp, monkeypatch, qt_widget_scope):
    with qt_widget_scope(QWidget()) as other:
        other.apply_theme = Mock()
        other.show()
        switch_tests.test_scrollbars_replace_once_preserving_prefix(qapp, monkeypatch)
        other.apply_theme.assert_not_called()


def test_shared_style_spies_are_restored_before_owned_deletion(qapp, monkeypatch, qt_widget_scope):
    from contextlib import contextmanager

    observed = []

    @contextmanager
    def checked_scope(widget):
        with qt_widget_scope(widget):
            yield widget
            # A native QStyle may be shared by multiple widgets. Its methods must
            # be native again before close/delete can trigger polish callbacks.
            observed.append((not hasattr(widget.style().polish, "__code__"),
                             not hasattr(widget.style().unpolish, "__code__")))

    switch_tests.test_equal_inline_and_class_helpers_do_no_style_work(
        qapp, monkeypatch, checked_scope, theme.style_card)
    switch_tests.test_changed_class_still_repolishes(qapp, monkeypatch, checked_scope)
    assert observed == [(True, True), (True, True)]


@pytest.mark.parametrize("fail", (False, True))
def test_controller_close_runs_after_activity_stops_and_before_owned_delete(
        qapp, qt_widget_scope, qt_deferred_delete, fail):
    from types import SimpleNamespace

    owner = SimpleNamespace(widget=QWidget())
    widget = owner.widget
    timer = QTimer(widget)
    timer.start(0)
    other = QWidget()
    other.deleteLater()
    observed = []

    def close():
        observed.append(not timer.isActive() and isValid(widget))
        widget.hide()
        widget.deleteLater()
        owner.widget = None

    try:
        expected = pytest.raises(AssertionError, match="synthetic failure") if fail else nullcontext()
        with expected:
            with qt_widget_scope(widget, close=close):
                if fail:
                    raise AssertionError("synthetic failure")
        assert observed == [True]
        assert owner.widget is None and not isValid(widget) and not isValid(timer)
        assert isValid(other)
    finally:
        qt_deferred_delete(other)
