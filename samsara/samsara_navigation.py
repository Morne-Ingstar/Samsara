"""Voice navigation to Samsara's existing surfaces (819).

No windows, services or profile files are touched at import time. Opening is
posted to the UI thread; wrapper construction posts finish before activation.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    label: str
    kind: str
    target: str
    wrapper: str = ""


PLACES = {
    "home": Place("Home", "hub", "Home"),
    "history": Place("History", "hub", "History"),
    "dictionary": Place("Dictionary", "hub", "Dictionary"),
    "snippets": Place("Snippets", "hub", "Snippets"),
    "memos": Place("Memos", "hub", "Memos"),
    "voice_help": Place("Voice help", "hub", "Voice help"),
    "settings": Place("Settings", "settings", "General"),
    "general": Place("General", "settings", "General"),
    "dictation": Place("Dictation", "settings", "Dictation"),
    "audio": Place("Audio", "settings", "Audio"),
    "ava": Place("Ava", "settings", "Ava"),
    "shortcuts": Place("Shortcuts", "settings", "Shortcuts"),
    "help": Place("Help", "settings", "Help"),
    "commands": Place("Commands", "tool", "Command library"),
    "alarms": Place("Alarms", "tool", "Alarms"),
    "health": Place("Health", "tool", "Health"),
    "music": Place("Music", "tool", "Music"),
    "components": Place("Components", "tool", "Components"),
    "integrations": Place("Integrations", "tool", "Integrations"),
    "connect_ai_app": Place("Connect an AI app", "setting", "agents"),
    "guides": Place("Guides & tutorials", "guides", "Home"),
    "tutorial": Place("Tutorial", "tutorial", ""),
    "microphone_guide": Place("Mic Setup Guide", "method", "open_mic_setup_guide", "mic_setup_wizard"),
    "ava_setup": Place("Ava Setup Guide", "method", "open_ava_guide", "ava_guide"),
    "quick_reference": Place("Quick Reference", "method", "open_quick_reference", "_quick_reference_qt"),
    "command_reference": Place("Command Reference", "reference", "", "cheat_sheet"),
    "reminders": Place("Reminders", "reminders", ""),
    "tasks": Place("Tasks", "tasks", ""),
    "listening_display": Place("Listening display", "display", ""),
    "updates": Place("Updates", "updates", ""),
    "about": Place("About Samsara", "hub", "Home"),
    "documentation": Place("Documentation", "link", "documentation"),
    "discord": Place("Discord", "link", "discord"),
    "support": Place("Support", "link", "support"),
    "diagnostics": Place("Dictation Diagnostics", "method", "open_dictation_diagnostics", "_diagnostics_qt"),
    "live_log": Place("Live Log", "method", "open_log_viewer", "_log_viewer_qt"),
    "log": Place("Main log", "method", "open_log_file"),
    "transcribe_file": Place("Transcribe a file", "method", "open_transcribe_file", "_transcribe_file_qt"),
    "benchmark_review": Place("Benchmark Review", "method", "open_benchmark_review", "_benchmark_review_qt"),
    "correction": Place("Correct Last Dictation", "method", "open_correction_capture"),
    "stress_test": Place("Stress Test Wizard", "method", "open_stress_test_wizard", "_stress_wizard_qt"),
    "wake_word_debug": Place("Wake word debug", "method", "open_wake_word_debug", "wake_word_debug_window"),
    "config_folder": Place("Config folder", "method", "open_config_folder"),
}

def reference_group(row):
    """Presentation group only; canonical IDs and execution stay unchanged."""
    phrase = row.get("phrase", "")
    phrases = {'samsara ' + key.replace('_', ' ') for key in PLACES}
    return "samsara" if phrase in phrases else row.get("plugin")


def _surface(wrapper):
    from samsara.ui.window_activation import surface_requested
    window = getattr(wrapper, "_window", None)
    if window is None:
        window = getattr(wrapper, "window", None)
    if callable(window):  # QWidget.window(), rather than a wrapper property.
        window = window()
    if window is not None:
        surface_requested(window)


def _open_hub(app, page, *, guides=False):
    from samsara.ui import qt_runtime
    # open_page intentionally declines on a cold hub. Queue its first build
    # before selecting, so the first spoken Dictionary never lands on Home.
    app.show_main_window()

    def select():
        if not app.open_hub_page(page):
            raise RuntimeError("Samsara could not open " + page)
        if guides:
            def reveal():
                home = app.main_window._window._panel_cache["Home"]
                home.show_guides(True)
            qt_runtime.post(reveal)
    qt_runtime.post(select)


def _open(app, place):
    from samsara.ui import qt_runtime
    kind = place.kind
    if kind in ("hub", "guides"):
        _open_hub(app, place.target, guides=kind == "guides")
    elif kind in ("settings", "tool", "setting"):
        from samsara.ui.settings_qt import SettingsQt
        controller = getattr(app, "_settings_qt", None)
        if controller is None:
            controller = app._settings_qt = SettingsQt(app)
        if kind == "settings":
            controller.show_page(place.target)
        elif kind == "tool":
            controller.show_tool(place.target)
        else:
            controller.show_setting(place.target)
    elif kind == "method":
        getattr(app, place.target)()
        if place.wrapper:
            qt_runtime.post(lambda: _surface(getattr(app, place.wrapper, None)))
    elif kind == "tutorial":
        from samsara.ui.tutorial_qt import TutorialWindow
        from samsara.ui.window_activation import surface_requested
        window = getattr(app, '_samsara_tutorial', None)
        if window is None:
            window = app._samsara_tutorial = TutorialWindow(app)
        surface_requested(window)
    elif kind == "reference":
        app.cheat_sheet.show_filtered("")
        qt_runtime.post(lambda: _surface(app.cheat_sheet))
    elif kind == "reminders":
        from samsara.ui.status_overlay import get_overlay
        overlay = get_overlay()
        overlay.show(notification_manager=getattr(app, "notification_manager", None),
                     alarm_manager=getattr(app, "alarm_manager", None))
        qt_runtime.post(lambda: _surface(overlay))
    elif kind == "tasks":
        from plugins.commands.tasks import handle_show_tasks, _get_overlay
        handle_show_tasks(app)
        qt_runtime.post(lambda: _surface(_get_overlay()))
    elif kind == "display":
        app._listening_display_visible = True
        app.apply_listening_indicator_settings()
    elif kind == "updates":
        from samsara.ui.update_qt import show_update_dialog
        from samsara.ui.window_activation import surface_requested
        surface_requested(show_update_dialog(app))
    elif kind == "link":
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from samsara.support_feedback import DOCUMENTATION_URL, SUPPORT_URL
        from samsara.ui.community_links import DISCORD_URL
        url = {"documentation": DOCUMENTATION_URL, "discord": DISCORD_URL,
               "support": SUPPORT_URL}[place.target]
        if not QDesktopServices.openUrl(QUrl(url)):
            raise RuntimeError("Samsara could not open " + place.label)


def open_place(app, place_id):
    """Validate synchronously; use the app's normal scheduler for UI errors."""
    place = PLACES[place_id]
    app._schedule_ui(lambda: _open(app, place))


class SamsaraNavigationMixin:
    """Zero-argument methods for the existing MethodHandler protocol."""

    def open_samsara_home(self):
        open_place(self, "home")

    def open_samsara_history(self):
        open_place(self, "history")

    def open_samsara_dictionary(self):
        open_place(self, "dictionary")

    def open_samsara_snippets(self):
        open_place(self, "snippets")

    def open_samsara_memos(self):
        open_place(self, "memos")

    def open_samsara_voice_help(self):
        open_place(self, "voice_help")

    def open_samsara_settings(self):
        open_place(self, "settings")

    def open_samsara_general(self):
        open_place(self, "general")

    def open_samsara_dictation(self):
        open_place(self, "dictation")

    def open_samsara_audio(self):
        open_place(self, "audio")

    def open_samsara_ava(self):
        open_place(self, "ava")

    def open_samsara_shortcuts(self):
        open_place(self, "shortcuts")

    def open_samsara_help(self):
        open_place(self, "help")

    def open_samsara_commands(self):
        open_place(self, "commands")

    def open_samsara_alarms(self):
        open_place(self, "alarms")

    def open_samsara_health(self):
        open_place(self, "health")

    def open_samsara_music(self):
        open_place(self, "music")

    def open_samsara_components(self):
        open_place(self, "components")

    def open_samsara_integrations(self):
        open_place(self, "integrations")

    def open_samsara_connect_ai_app(self):
        open_place(self, "connect_ai_app")

    def open_samsara_guides(self):
        open_place(self, "guides")

    def open_samsara_tutorial(self):
        open_place(self, "tutorial")

    def open_samsara_microphone_guide(self):
        open_place(self, "microphone_guide")

    def open_samsara_ava_setup(self):
        open_place(self, "ava_setup")

    def open_samsara_quick_reference(self):
        open_place(self, "quick_reference")

    def open_samsara_command_reference(self):
        open_place(self, "command_reference")

    def open_samsara_reminders(self):
        open_place(self, "reminders")

    def open_samsara_tasks(self):
        open_place(self, "tasks")

    def open_samsara_listening_display(self):
        open_place(self, "listening_display")

    def open_samsara_updates(self):
        open_place(self, "updates")

    def open_samsara_about(self):
        open_place(self, "about")

    def open_samsara_documentation(self):
        open_place(self, "documentation")

    def open_samsara_discord(self):
        open_place(self, "discord")

    def open_samsara_support(self):
        open_place(self, "support")

    def open_samsara_diagnostics(self):
        open_place(self, "diagnostics")

    def open_samsara_live_log(self):
        open_place(self, "live_log")

    def open_samsara_log(self):
        open_place(self, "log")

    def open_samsara_transcribe_file(self):
        open_place(self, "transcribe_file")

    def open_samsara_benchmark_review(self):
        open_place(self, "benchmark_review")

    def open_samsara_correction(self):
        open_place(self, "correction")

    def open_samsara_stress_test(self):
        open_place(self, "stress_test")

    def open_samsara_wake_word_debug(self):
        open_place(self, "wake_word_debug")

    def open_samsara_config_folder(self):
        open_place(self, "config_folder")
