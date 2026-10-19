"""Theme-switch work bounds and real fixture lifetimes; synthetic UI state only."""
import copy
import time
from contextlib import ExitStack

import pytest
from PySide6.QtCore import QEventLoop, QSettings
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from shiboken6 import isValid

from samsara.ui import focus_ring, theme


def test_keyboard_rewrite_cache_is_bounded_and_hits():
    rewrite = focus_ring.keyboard_stylesheet
    rewrite.cache_clear()
    sheet = 'QPushButton:focus { border: 2px solid cyan; }'
    revised = rewrite(sheet)
    assert '[keyboardFocus="true"]' in revised and 'outline: none' in revised
    assert rewrite(sheet) == revised
    assert rewrite.cache_info().hits == 1
    maximum = rewrite.cache_info().maxsize
    assert maximum == 4096
    for index in range(maximum + 2):
        rewrite(f'/* {index} */')
    assert rewrite.cache_info().currsize == maximum


def test_scrollbars_replace_once_preserving_prefix(qapp, monkeypatch):
    previous, sheet = theme.active_theme(), qapp.styleSheet()
    prefix = 'QWidget { font-size: 14px; }\n'
    focus_ring.install(qapp)
    qapp.setStyleSheet(prefix)
    theme.install_app_scrollbars(qapp)
    original = QApplication.setStyleSheet
    calls = []
    try:
        with monkeypatch.context() as patch:
            # This test owns the app scrollbar suffix, not other tests' windows.
            patch.setattr(QApplication, 'topLevelWidgets', staticmethod(lambda: []))
            patch.setattr(QApplication, 'setStyleSheet',
                          lambda app, value: (calls.append(value), original(app, value))[-1])
            target = 'light' if previous == 'dark' else 'dark'
            theme.set_theme(target)
            assert len(calls) == 1
            assert calls[0] == prefix + theme._SCROLLBAR_MARKER + theme.SCROLLBAR_QSS
            theme.install_app_scrollbars(qapp)
            assert len(calls) == 1
    finally:
        theme.set_theme(previous, refresh=False)
        qapp.setStyleSheet(sheet)


@pytest.mark.parametrize('style', [theme.make_primary, theme.make_secondary, theme.make_ghost,
                                 theme.style_card, theme.style_instruction_box,
                                 theme.style_success_banner, theme.style_guide_card, theme.style_tip_frame,
                                 theme.style_footer])
def test_equal_inline_and_class_helpers_do_no_style_work(qapp, monkeypatch, qt_widget_scope, style):
    focus_ring.install(qapp)
    widget = QPushButton('Synthetic')
    with qt_widget_scope(widget):
        style(widget)
        calls = []
        native = QWidget.setStyleSheet
        with monkeypatch.context() as patch:
            patch.setattr(QWidget, 'setStyleSheet',
                          lambda w, value: (calls.append(value), native(w, value))[-1])
            # Restore shared native style methods before any widget destruction.
            patch.setattr(widget.style(), 'unpolish', lambda *_: calls.append('unpolish'))
            patch.setattr(widget.style(), 'polish', lambda *_: calls.append('polish'))
            style(widget)
            assert calls == []


def test_equal_inline_refresh_does_not_reassign(qapp, monkeypatch, qt_widget_scope):
    widget = QWidget()
    with qt_widget_scope(widget):
        widget.setStyleSheet('padding: 7px;')
        widget.show()
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
        native = QWidget.setStyleSheet
        calls = []
        with monkeypatch.context() as patch:
            patch.setattr(QApplication, 'topLevelWidgets', staticmethod(lambda: [widget]))
            patch.setattr(QWidget, 'setStyleSheet',
                          lambda w, value: (calls.append(w), native(w, value))[-1])
            theme.refresh_all()
            assert widget not in calls


def test_changed_class_still_repolishes(qapp, monkeypatch, qt_widget_scope):
    widget = QWidget()
    calls = []
    with qt_widget_scope(widget):
        theme.style_card(widget)
        with monkeypatch.context() as patch:
            patch.setattr(widget.style(), 'unpolish', lambda w: calls.append(('unpolish', w.property('class'))))
            patch.setattr(widget.style(), 'polish', lambda w: calls.append(('polish', w.property('class'))))
            theme.style_tip_frame(widget)
            assert calls == [('unpolish', 'tipFrame'), ('polish', 'tipFrame')]


def test_full_fixture_delivers_delete_before_patches_end(qapp, qt_widget_scope):
    from tests.test_full_display_band_456 import surface
    from samsara.live_surface import controller
    with pytest.MonkeyPatch.context() as patch:
        fixture = surface.__wrapped__(qapp, patch, qt_widget_scope)
        c, indicator, _ = next(fixture)
        indicator.show_outcome('Synthetic result', 'success', 1800)
        c.drain()
        widget = c.widget
        children = widget.findChildren(QWidget)
        patched_post = controller.qt_runtime.post
        destroyed = []
        widget.destroyed.connect(lambda: destroyed.append(controller.qt_runtime.post is patched_post))
        with pytest.raises(StopIteration):
            next(fixture)
        assert destroyed == [True]
        assert not isValid(widget) and all(not isValid(w) for w in children)
        assert widget not in qapp.topLevelWidgets()


def test_settings_fixture_delivers_delete_inside_patch_context(qapp, qt_deferred_delete):
    from tests.test_accessibility_settings_tools_417 import tools_window
    from samsara import health_store
    fixture = tools_window.__wrapped__(qapp, qt_deferred_delete)
    widget = next(fixture)
    children = widget.findChildren(QWidget)
    patched_get_today = health_store.get_today
    destroyed = []
    widget.destroyed.connect(lambda: destroyed.append(health_store.get_today is patched_get_today))
    with pytest.raises(StopIteration):
        next(fixture)
    assert destroyed == [True]
    assert not isValid(widget) and all(not isValid(w) for w in children)
    assert widget not in qapp.topLevelWidgets()


def test_ui_shaped_switch_has_generous_six_second_budget(qapp, monkeypatch, tmp_path, qt_widget_scope,
                                                      record_property):
    from samsara import autostart, cuda_detect
    from samsara.runtime import thread_registry
    from samsara.ui import home_qt, main_window_qt, settings_qt
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    from tests.test_main_window_theme_live_173 import _App
    from tests.test_settings_pages import _schema_defaults
    monkeypatch.setattr(home_qt, 'QSettings', lambda *_a, **_k:
                        QSettings(str(tmp_path / 'private.ini'), QSettings.Format.IniFormat))
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *_a, **_k: None)
    monkeypatch.setattr(cuda_detect, 'request_cuda_status', lambda *_a, **_k: None)
    monkeypatch.setattr(thread_registry, 'spawn', lambda *_a, **_k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda *_a, **_k: None)
    previous, app_sheet = theme.active_theme(), qapp.styleSheet()
    try:
        with ExitStack() as owned:
            theme.set_theme('light', refresh=False)
            owner = _App()
            config = copy.deepcopy(_schema_defaults())
            config.update(owner.config)
            config['ui'] = dict(config.get('ui', {}), theme='light', live_surface={'enabled': True})
            owner.config = config
            hub = main_window_qt._MainWindow(owner)
            owned.enter_context(qt_widget_scope(hub))
            hub._poll_timer.stop()
            settings = settings_qt._SettingsWindow(owner)
            owned.enter_context(qt_widget_scope(settings))
            controller = LiveSurfaceController(owner, model=LiveSurfaceModel())
            controller.widget = LiveSurfaceWidget(controller, reduced_motion=True)
            owned.enter_context(qt_widget_scope(controller.widget, close=controller.close))
            owner.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
            windows = [hub, settings, controller.widget]
            for window in windows:
                window.show()
            for _ in range(3):
                qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
            elapsed = {'dark': [], 'light': []}
            with monkeypatch.context() as patch:
                patch.setattr(QApplication, 'topLevelWidgets', staticmethod(lambda: windows))
                # Independent reference: reassign the app and every inline QSS
                # on this exact tree through Qt, without theme hooks or token
                # rewriting. This includes Python/coverage and native Qt cost.
                reference_sheet = qapp.styleSheet()
                inline_sheets = [(widget, widget.styleSheet()) for window in windows
                                 for widget in [window, *window.findChildren(QWidget)]
                                 if widget.styleSheet()]
                calibration = []
                for sample in range(3):
                    started = time.perf_counter()
                    QApplication.setStyleSheet(qapp, reference_sheet + f'\n/* calibration {sample} */')
                    for widget, sheet in inline_sheets:
                        QWidget.setStyleSheet(widget, sheet)
                    for _ in range(3):
                        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
                    calibration.append(time.perf_counter() - started)
                qapp.setStyleSheet(reference_sheet)
                # Six seconds remains the floor. Coverage measurements put a
                # switch at about 2.2 reference passes; allow three on slower
                # runners while still rejecting a persistent 3x regression.
                budget = max(6.0, 3 * min(calibration))
                for _ in range(3):
                    for palette in ('dark', 'light'):
                        started = time.perf_counter()
                        theme.set_theme(palette)
                        for _ in range(3):
                            qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
                        elapsed[palette].append(time.perf_counter() - started)
                        assert theme.active_theme() == palette
                record_property('theme_switch_seconds', elapsed)
                record_property('theme_switch_native_seconds', calibration)
                record_property('theme_switch_budget_seconds', budget)
                print(f'theme switch: {elapsed}; native: {calibration}; budget: {budget:.3f}s')
                assert max(min(samples) for samples in elapsed.values()) < budget, (elapsed, calibration, budget)
    finally:
        theme.set_theme(previous, refresh=False)
        qapp.setStyleSheet(app_sheet)
