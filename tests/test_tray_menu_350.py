from __future__ import annotations

import os
from types import SimpleNamespace

from PIL import Image

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.paths import newest_python_mtime
from samsara.ui.tray_qt import SamsaraTrayQt, build_restart_argv


def _menu_app():
    app = SimpleNamespace(
        config={
            "microphone": "mic-1",
            "mode": "hold",
            "wake_word_config": {"phrase": "samsara"},
            "wake_word_enabled": False,
            "streaming_mode": False,
            "gesture": {"enabled": False},
            "listening_indicator_enabled": False,
            "cleanup_mode": "clean",
        },
        available_mics=[{"id": "mic-1", "name": "Test Microphone"}],
        _mouse_hook=None,
        snoozed=False,
        cheat_sheet=None,
        create_icon_image=lambda: Image.new("RGBA", (16, 16)),
        get_current_microphone_name=lambda: "Test Microphone",
        _mic_refresh_blocked=lambda: True,
        mouse_hotkey_status=lambda: (_ for _ in ()).throw(RuntimeError("section failed")),
    )
    return app


def test_failed_builder_section_keeps_exit_and_adds_disabled_row(qapp):
    tray = SamsaraTrayQt(_menu_app())
    try:
        tray._rebuild_menu()
        actions = tray._menu.actions()
        by_text = {action.text(): action for action in actions}

        assert "Exit" in by_text
        fallback = by_text["Mouse hotkey unavailable - see log"]
        assert not fallback.isEnabled()
    finally:
        tray.stop()


def test_newer_python_source_mtime_is_detected_under_given_root(tmp_path):
    source = tmp_path / "samsara.py"
    source.write_text("value = 1\n", encoding="utf-8")
    before = newest_python_mtime(tmp_path)
    assert before is not None

    changed = before + 1_000_000
    os.utime(source, ns=(changed, changed))

    assert newest_python_mtime(tmp_path) > before


def test_restart_argv_reuses_interpreter_and_process_arguments():
    from pathlib import Path

    assert build_restart_argv("python-test.exe", ["dictation.py", "--quiet"]) == [
        "python-test.exe", str(Path(__file__).resolve().parents[1] / "dictation.py"),
        "--quiet",
    ]


def test_source_schema_enables_code_change_watch_by_default():
    assert SETTINGS_SCHEMA["diagnostics.watch_code_changes"]["default"] is True
