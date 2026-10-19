"""Focused UI coverage for Live Log restoration and Advanced audio copy."""

import threading
from types import SimpleNamespace

from PySide6.QtWidgets import QCheckBox, QLabel, QMainWindow

from samsara.ui import log_viewer_qt


class _SettingsApp:
    def __init__(self):
        self.config = {}
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(
            commands={}, find_command=lambda _phrase: None,
        )
        self.hints = None
        self.alarm_manager = None

    def play_sound(self, *_args, **_kwargs):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


def test_live_log_existing_window_posts_restore_then_focus(monkeypatch):
    viewer = log_viewer_qt.LogViewerQt(SimpleNamespace())
    window = SimpleNamespace(
        showNormal=lambda: None,
        raise_=lambda: None,
        activateWindow=lambda: None,
    )
    viewer._window = window
    posted = []
    monkeypatch.setattr(
        log_viewer_qt.qt_runtime,
        "post",
        lambda callback: posted.append(callback.__name__),
    )

    viewer.show()

    assert posted == ["<lambda>", "<lambda>", "<lambda>"]


def test_live_log_existing_minimized_window_is_restored(monkeypatch, qapp):
    viewer = log_viewer_qt.LogViewerQt(SimpleNamespace())
    window = QMainWindow()
    viewer._window = window
    monkeypatch.setattr(log_viewer_qt.qt_runtime, "post", lambda callback: callback())
    try:
        window.show()
        window.showMinimized()
        qapp.processEvents()
        assert window.isMinimized()

        viewer.show()
        qapp.processEvents()

        assert not window.isMinimized()
        assert window.isVisible()
    finally:
        window.close()


def test_advanced_audio_copy_is_honest_and_distinguishes_both_ducking_features(qapp, qt_widget_scope):
    from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES

    with qt_widget_scope(_SettingsWindow(_SettingsApp())) as window:
        audio = window._pages['Audio']
        text = " ".join(widget.text() for cls in (QLabel, QCheckBox)
                        for widget in audio.findChildren(cls))
        text += ' ' + ' '.join(row.full_help for row in window._destination_rows
                               if row.page_name == 'Audio')
        assert "Experimental Echo Cancellation" not in text
        assert "3–8% echo reduction" not in text
        assert not any(key.startswith('echo_cancellation.')
                       for key, _page, _widget in window.config_row_inventory())
        assert 'adv_aec_enabled' not in window._rows_by_widget
        assert 'adv_aec_latency' not in window._rows_by_widget
        assert "Playback Reduction While Dictating" in text
        assert "Absolute volume level for other apps" in text
        assert "Fraction of other apps' current volume" not in text
