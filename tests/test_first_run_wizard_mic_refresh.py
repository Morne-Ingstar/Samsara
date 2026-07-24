"""Tests for first_run_wizard_qt.py's mic-refresh Qt-thread discipline fix
(2026-07-17, alongside the DictationApp.refresh_audio_devices() guard fix in
tests/test_audio_refresh.py).

_on_refresh_mics_clicked() is a .clicked signal handler -- already on the Qt
thread -- so when a live DictationApp is available it must call
refresh_audio_devices() DIRECTLY there (via _refresh_mics_via_app()) rather
than handing it to a background thread the way the old code did. That used
to be harmless because the old guard always blocked before any real work
happened; now that refresh_audio_devices() actually stops/restarts the ACE
engine, calling it off the Qt thread would violate its documented
Qt-thread-only contract for real. The no-app fallback path
(_enumerate_mics(), a plain read, no engine involved) still runs on a
background thread, unaffected.

Exercises the real unbound methods (types.MethodType pattern, matching
test_audio_refresh.py's convention) against a minimal QObject stub carrying
the real _mic_result Signal -- PySide6 Signals only bind on an actual
QObject, so a bare types.SimpleNamespace won't do here.
"""
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, Signal

from samsara.ui.first_run_wizard_qt import _WizardWindow


class _FakeWizard(QObject):
    """Minimal stand-in carrying the real _mic_result Signal, with every
    method under test bound from the real _WizardWindow class."""

    _mic_result = Signal(str, str)

    def __init__(self):
        super().__init__()
        self._samsara_app = None
        self._mic_status = None
        self._step = 0
        self._stop_meter = Mock()
        self._mic_scan_error = None
        self._enumerate_mics = Mock(return_value=[{'id': 0, 'name': 'Fallback Mic'}])

        self._on_refresh_mics_clicked = types.MethodType(
            _WizardWindow._on_refresh_mics_clicked, self)
        self._refresh_mics_via_app = types.MethodType(
            _WizardWindow._refresh_mics_via_app, self)
        self._refresh_mics = types.MethodType(_WizardWindow._refresh_mics, self)


class _FakeCombo:
    def __init__(self):
        self.items: list[str] = []
        self.enabled = False
        self._current_index = 0
        self.current_text = ""

    def blockSignals(self, *_args):
        return None

    def clear(self):
        self.items.clear()

    def addItems(self, items):
        self.items.extend(items)

    def setEnabled(self, enabled):
        self.enabled = enabled

    def addItem(self, item):
        self.items.append(item)

    def currentText(self):
        return self.current_text

    def setCurrentIndex(self, index):
        self._current_index = index


class _FakeLabel:
    def __init__(self):
        self.text = ""
        self.style = ""

    def setText(self, text):
        self.text = text

    def setStyleSheet(self, style):
        self.style = style


class _LoadWizard:
    def __init__(self):
        self._mics = []
        self._mic_scan_error = None
        self._samsara_app = None
        self._step = 2
        self._mic_combo = _FakeCombo()
        self._mic_status = _FakeLabel()
        self._mic_result = Mock()
        self._stop_meter = Mock()
        self._start_meter = Mock()
        self._enumerate_mics = Mock(return_value=[])


def _make_app(blocked=False, mics=None, raises=False):
    app = types.SimpleNamespace()
    app._mic_refresh_blocked = Mock(return_value=blocked)
    if raises:
        app.refresh_audio_devices = Mock(side_effect=RuntimeError("boom"))
    else:
        app.refresh_audio_devices = Mock(
            return_value=mics or [{'id': 1, 'name': 'Fresh Mic'}])
    return app


class TestQtThreadCallSiteWhenAppAvailable:
    def test_refresh_called_directly_not_backgrounded(self, qapp, monkeypatch):
        wizard = _FakeWizard()
        wizard._samsara_app = _make_app()
        spawn = Mock()
        monkeypatch.setattr(
            "samsara.ui.first_run_wizard_qt.thread_registry.spawn", spawn)

        wizard._on_refresh_mics_clicked()

        spawn.assert_not_called()
        wizard._samsara_app.refresh_audio_devices.assert_called_once()

    def test_blocked_emits_skipped_without_calling_refresh(self, qapp):
        wizard = _FakeWizard()
        wizard._samsara_app = _make_app(blocked=True)
        results = []
        wizard._mic_result.connect(lambda msg, color: results.append(msg))

        wizard._refresh_mics_via_app()

        wizard._samsara_app.refresh_audio_devices.assert_not_called()
        assert results == ["__refresh_skipped__"]

    def test_not_blocked_calls_refresh_and_emits_done(self, qapp):
        wizard = _FakeWizard()
        wizard._samsara_app = _make_app(
            blocked=False, mics=[{'id': 2, 'name': 'Newly Plugged'}])
        results = []
        wizard._mic_result.connect(lambda msg, color: results.append(msg))

        wizard._refresh_mics_via_app()

        wizard._samsara_app.refresh_audio_devices.assert_called_once()
        assert wizard._mics == [{'id': 2, 'name': 'Newly Plugged'}]
        assert results == ["_refresh_done_"]

    def test_refresh_raising_falls_back_to_plain_enumeration(self, qapp):
        wizard = _FakeWizard()
        wizard._samsara_app = _make_app(raises=True)
        results = []
        wizard._mic_result.connect(lambda msg, color: results.append(msg))

        wizard._refresh_mics_via_app()

        wizard._enumerate_mics.assert_called_once()
        assert wizard._mics == [{'id': 0, 'name': 'Fallback Mic'}]
        assert results == ["_refresh_done_"]


class TestBackgroundThreadFallbackWhenNoApp:
    def test_no_app_still_spawns_background_thread(self, qapp, monkeypatch):
        wizard = _FakeWizard()
        wizard._samsara_app = None
        spawn = Mock()
        monkeypatch.setattr(
            "samsara.ui.first_run_wizard_qt.thread_registry.spawn", spawn)

        wizard._on_refresh_mics_clicked()

        spawn.assert_called_once()
        # No app -- must never touch _mic_refresh_blocked/refresh_audio_devices.
        assert spawn.call_args.args[1] == wizard._refresh_mics

    def test_refresh_mics_fallback_uses_realtime_rescan_path(self, qapp, monkeypatch):
        wizard = _FakeWizard()
        wizard._samsara_app = None
        force_rescan = Mock()
        list_microphones = Mock(return_value=[{'id': 3, 'name': 'Refreshed Mic'}])
        results = []
        wizard._mic_result.connect(lambda msg, color: results.append(msg))

        monkeypatch.setattr("samsara.ui.first_run_wizard_qt.force_rescan", force_rescan)
        monkeypatch.setattr("samsara.ui.first_run_wizard_qt.list_microphones", list_microphones)

        wizard._refresh_mics()

        force_rescan.assert_called_once()
        list_microphones.assert_called_once_with()
        assert wizard._mics == [{'id': 3, 'name': 'Refreshed Mic'}]
        assert results == ["_refresh_done_"]


class TestMicEnumerationErrorHandlingInUi:
    def test_scanner_exception_shows_retry_message(self, monkeypatch):
        wizard = _LoadWizard()
        wizard._load_mics = types.MethodType(_WizardWindow._load_mics, wizard)
        wizard._enumerate_mics = types.MethodType(_WizardWindow._enumerate_mics, wizard)
        wizard._populate_mic_combo = types.MethodType(
            _WizardWindow._populate_mic_combo, wizard,
        )

        monkeypatch.setattr(
            "samsara.ui.first_run_wizard_qt.list_microphones",
            Mock(side_effect=RuntimeError("no api")),
        )
        wizard._load_mics()
        wizard._populate_mic_combo()

        assert wizard._mics == []
        assert wizard._mic_combo.items == ["Couldn't scan audio devices — press Refresh"]
        assert wizard._mic_status.text == "Mic scan failed (RuntimeError)"
        assert wizard._mic_status.style

    def test_empty_scan_results_show_no_microphones_detected(self, monkeypatch):
        wizard = _LoadWizard()
        wizard._load_mics = types.MethodType(_WizardWindow._load_mics, wizard)
        wizard._enumerate_mics = types.MethodType(_WizardWindow._enumerate_mics, wizard)
        wizard._populate_mic_combo = types.MethodType(
            _WizardWindow._populate_mic_combo, wizard,
        )

        monkeypatch.setattr(
            "samsara.ui.first_run_wizard_qt.list_microphones",
            Mock(return_value=[]),
        )
        wizard._load_mics()
        wizard._populate_mic_combo()

        assert wizard._mics == []
        assert wizard._mic_combo.items == ["No microphones detected"]
        assert wizard._mic_status.text == ""

    def test_devices_populate_into_combo_when_available(self, monkeypatch):
        wizard = _LoadWizard()
        wizard._load_mics = types.MethodType(_WizardWindow._load_mics, wizard)
        wizard._enumerate_mics = types.MethodType(_WizardWindow._enumerate_mics, wizard)
        wizard._populate_mic_combo = types.MethodType(
            _WizardWindow._populate_mic_combo, wizard,
        )

        monkeypatch.setattr(
            "samsara.ui.first_run_wizard_qt.list_microphones",
            Mock(return_value=[{'id': 5, 'name': 'Focusrite'}, {'id': 7, 'name': 'Desk Mic'}]),
        )
        wizard._load_mics()
        wizard._populate_mic_combo()

        assert wizard._mics == [{'id': 5, 'name': 'Focusrite'}, {'id': 7, 'name': 'Desk Mic'}]
        assert wizard._mic_combo.items == ["Focusrite", "Desk Mic"]
