"""Every user-visible surface, constructible headlessly, in one place.

Queue 129 needs the same list twice: once for the switch test (does this
surface change when the palette does?) and once for the visual proof (render
it in both themes and look). Keeping the constructors here means the proof
and the test cannot drift apart -- a surface added to one is added to both.

Nothing here touches ~/.samsara/config.json or needs a live DictationApp;
Samsara may well be running while these are built. The widget CLASSES are
constructed directly, not the thread-safe facades around them, because a
facade posts to the samsara-qt runtime thread and a test has its own
QApplication.
"""

from __future__ import annotations

from pathlib import Path

from tests._theme_stub_app import StubApp, build_settings_window


def _main_window_page(page: str):
    """The hub window, showing one of its pages. Home, History, Memos,
    Snippets and Dictionary all live inside it, styled by its one sheet."""
    def build():
        from samsara.ui.main_window_qt import _MainWindow
        window = _MainWindow(StubApp())
        window._activate(page)
        return window
    return build


def _settings(tab: int):
    def build():
        window = build_settings_window()
        window._stack.setCurrentIndex(tab)
        return window
    return build


def _history_view():
    """The history list on its own, with no store behind it (the empty state)."""
    from samsara.ui.history_view import HistoryView
    return HistoryView(None)


def _quick_reference():
    from samsara.ui.quick_reference_qt import _QuickReferenceWindow
    return _QuickReferenceWindow(StubApp())


def _command_reference():
    from samsara.ui.command_cheatsheet_qt import _CheatSheetWindow
    from samsara.paths import samsara_home_dir
    return _CheatSheetWindow(
        execute_cb=lambda *a, **k: None,
        commands_cb=lambda: {},
        palette_path=Path(samsara_home_dir()) / "cheatsheet_palette.json",
    )


def _first_run_wizard():
    from samsara.ui.first_run_wizard_qt import _WizardWindow
    from samsara.paths import samsara_config_path
    return _WizardWindow(samsara_config_path(), None)


def _mic_wizard():
    from samsara.ui.mic_setup_wizard_qt import _WizardWindow
    return _WizardWindow(StubApp())


def _tutorial():
    from samsara.ui.tutorial_qt import TutorialWindow
    return TutorialWindow(StubApp())


def _splash():
    from samsara.ui.splash_qt import _SplashWidget
    return _SplashWidget()


def _listening_indicator():
    from samsara.ui.listening_indicator import ListeningIndicator
    return ListeningIndicator()


def _dictionary_panel():
    from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
    return DictionaryPanelQt(StubApp())


def _status_overlay():
    """The Reminders & Alarms window, built directly rather than through its
    facade -- the facade posts to the samsara-qt runtime thread."""
    from samsara.ui.status_overlay import _StatusWindow
    return _StatusWindow(None, None)


def _streaming_preview():
    """The hands-free dictation preview: the app's one continuous
    alive-and-listening signal, and a floating surface with its own sheet."""
    from samsara.streaming import _StreamingWidget
    return _StreamingWidget(dim=False)._w


#: name -> zero-arg constructor. The name is also the PNG's filename.
SURFACES = {
    "home": _main_window_page("Home"),
    "history": _main_window_page("History"),
    "memos": _main_window_page("Memos"),
    "snippets": _main_window_page("Snippets"),
    "dictionary": _main_window_page("Dictionary"),
    "settings_general": _settings(0),
    "settings_modes": _settings(1),
    "settings_commands": _settings(2),
    "settings_sounds": _settings(3),
    "settings_tts": _settings(4),
    "settings_advanced": _settings(8),
    "settings_help": _settings(9),
    "first_run_wizard": _first_run_wizard,
    "mic_wizard": _mic_wizard,
    "tutorial": _tutorial,
    "command_reference": _command_reference,
    "quick_reference": _quick_reference,
    "listening_indicator": _listening_indicator,
    "splash": _splash,
    "history_view": _history_view,
    "dictionary_panel": _dictionary_panel,
    "status_overlay": _status_overlay,
    "streaming_preview": _streaming_preview,
}
