"""Retired app integration, without importing or booting dictation."""
import ast
import copy
from pathlib import Path
import textwrap
from types import SimpleNamespace
from unittest.mock import Mock, call

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))


def app_source():
    source = (ROOT / "dictation.py").read_text(encoding="utf-8-sig")
    cls = next(n for n in ast.parse(source).body
               if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    return source, cls


def construct(enabled):
    from samsara.echo_cancel import EchoCanceller, LoopbackCapture
    source, _ = app_source()
    # Extract only the existing AEC block, never the application constructor.
    start = source.index("        aec_config = self.config.get('echo_cancellation', {})")
    end = source.index("        # Hands-free audio ducking", start)
    block = textwrap.dedent(source[start:end])
    app = SimpleNamespace(config={"echo_cancellation": {
        "enabled": enabled, "latency_ms": 85.0}}, capture_rate=16000)
    saved = copy.deepcopy(app.config)
    log = Mock()
    with pytest.MonkeyPatch.context() as patch:
        loopback_start = Mock(return_value=True)
        patch.setattr(LoopbackCapture, "start", loopback_start)
        patch.setattr(EchoCanceller, "_apply_cached_latency", Mock())
        exec(compile(block, "<aec-construction-405>", "exec"), {
            "self": app, "EchoCanceller": EchoCanceller, "logger": log,
            "time": SimpleNamespace(perf_counter=lambda: 0.0),
        })
    return app, saved, log, loopback_start


@pytest.mark.parametrize("enabled", [True, False])
def test_saved_setting_never_starts_loopback_or_changes_config(enabled):
    app, saved, log, start = construct(enabled)
    start.assert_not_called()
    assert app.config == saved
    assert app.echo_canceller.enabled is False
    assert app.echo_canceller.is_active is False
    if enabled:
        log.info.assert_called_once_with(
            "[AEC] echo cancellation is retired; the saved setting is ignored")
    else:
        log.info.assert_not_called()


def test_compatibility_answers_match_disabled_canceller(monkeypatch):
    from samsara.echo_cancel import EchoCanceller, LoopbackCapture, AdaptiveEchoCanceller
    app, _, _, start = construct(True)
    old_inactive = EchoCanceller(enabled=False, sample_rate=16000, latency_ms=85.0)
    assert app.echo_canceller.is_active == old_inactive.is_active is False
    assert app.echo_canceller.last_ref_rms == old_inactive.last_ref_rms is None
    mic = np.zeros((1024, 1), dtype=np.float32)
    ref = Mock(side_effect=AssertionError("reference audio must not be read"))
    filt = Mock(side_effect=AssertionError("filter must not run"))
    stop = Mock()
    monkeypatch.setattr(LoopbackCapture, "get_recent", ref)
    monkeypatch.setattr(LoopbackCapture, "stop", stop)
    monkeypatch.setattr(AdaptiveEchoCanceller, "process", filt)
    assert app.echo_canceller.process(mic) is old_inactive.process(mic) is mic
    app.echo_canceller.stop()
    start.assert_not_called()
    stop.assert_not_called()
    ref.assert_not_called()
    filt.assert_not_called()


def test_calibration_only_logs_retirement():
    _, cls = app_source()
    method = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "calibrate_echo_cancellation")
    log = Mock()
    # Only the exact production definition, as allowed by the maintained adapter.
    namespace = {"logger": log}
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 str(ROOT / "dictation.py"), "exec"), namespace)
    # Any app access (audio, config, worker, saving or feedback) fails immediately.
    namespace["calibrate_echo_cancellation"](object())
    log.info.assert_called_once_with("[AEC] echo cancellation calibration is retired")
    assert log.mock_calls == [call.info("[AEC] echo cancellation calibration is retired")]


def test_schema_defaults_and_audio_page_exclude_retired_rows():
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.ui.settings import audio_page
    assert SETTINGS_SCHEMA["echo_cancellation.enabled"]["default"] is False
    assert SETTINGS_SCHEMA["echo_cancellation.latency_ms"]["default"] == 30.0
    for key in ("echo_cancellation.enabled", "echo_cancellation.latency_ms"):
        assert SETTINGS_SCHEMA[key]["settings_ui"] is False
        assert SETTINGS_SCHEMA[key]["ui_less_reason"]
        assert SETTINGS_SCHEMA[key]["help"]["why"] == (
            "This option is inactive because echo cancellation was retired.")
    owner = SimpleNamespace(make_page=Mock())
    audio_page.build(owner)
    _, _, base, advanced = owner.make_page.call_args.args
    assert all(not row[3].startswith("echo_cancellation.") for row in base + advanced)


def test_settings_search_and_apply_keep_legacy_config_hidden(qapp, tmp_path, monkeypatch):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults
    monkeypatch.setattr(autostart, "sync_run_entry", lambda *a, **kw: None)
    monkeypatch.setattr(ComponentsPage, "refresh_async", lambda self: None)
    config = _schema_defaults()
    config["echo_cancellation"] = {"enabled": True, "latency_ms": 85.0}
    window = _SettingsWindow(_StubApp(config))
    try:
        assert window._source_store.isHidden()
        assert not any(key.startswith("echo_cancellation.")
                       for key, _, _ in window.config_row_inventory())
        for key in ("adv_aec_enabled", "adv_aec_latency"):
            assert key not in window._rows_by_widget
            assert window._source_store.isAncestorOf(window._widgets[key])
        window._search_edit.setText("echo")
        window._apply_search_filter()
        updates = window._collect_destination_updates({})
        assert updates.get("echo_cancellation", config["echo_cancellation"]) == config["echo_cancellation"]
        assert config["echo_cancellation"] == {"enabled": True, "latency_ms": 85.0}
        window._search_edit.clear()
        window._apply_search_filter()
        window._stack.setCurrentWidget(window._pages["Audio"])
        window._pages["Audio"].advanced.toggle.setChecked(True)
        window.show()
        qapp.processEvents()
        page = window._pages["Audio"]
        page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
        qapp.processEvents()
        assert window.grab().save(str(tmp_path / "audio-settings.png"))
    finally:
        window.deleteLater()


def test_tray_has_no_echo_cancellation_action(qapp, tmp_path):
    from samsara.ui.tray_qt import SamsaraTrayQt
    from tests.test_tray_qt import _make_app, _all_actions
    app = _make_app()
    app.config["echo_cancellation"] = {"enabled": True}
    tray = SamsaraTrayQt(app)
    tray._rebuild_menu()
    assert not any("echo cancellation" in action.text().lower()
                   for action in _all_actions(tray._menu))
    app.calibrate_echo_cancellation.assert_not_called()
    developer = next(action.menu() for action in tray._menu.actions()
                     if action.menu() and "Developer" in action.text())
    developer.show()
    qapp.processEvents()
    assert developer.grab().save(str(tmp_path / "developer-menu.png"))
    developer.hide()
