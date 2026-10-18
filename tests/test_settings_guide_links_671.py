from types import SimpleNamespace

import pytest

from tests.test_settings_pages import _StubApp, _schema_defaults


@pytest.fixture
def settings_window(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui.settings_qt import _SettingsWindow

    monkeypatch.setattr(autostart, "sync_run_entry", lambda *args, **kwargs: None)
    app = _StubApp(_schema_defaults())
    window = _SettingsWindow(app)
    yield window
    window.deleteLater()


def test_audio_microphone_guide_button_calls_app_method(settings_window):
    from samsara.ui.settings_qt import PAGE_MODULES

    settings_window.app.open_mic_setup_guide = lambda: setattr(
        settings_window.app, "mic_guide_opened", True
    )
    audio_index = next(i for i, module in enumerate(PAGE_MODULES) if module.TITLE == "Audio")
    settings_window._ensure_destination(audio_index)
    button = settings_window._widgets["audio_mic_setup_guide_button"]

    assert button.text() == "Set up microphone..."
    settings_window.app.mic_guide_opened = False
    button.click()
    assert settings_window.app.mic_guide_opened is True


def test_commands_header_button_calls_quick_reference(settings_window):
    settings_window.app.open_quick_reference = lambda: setattr(
        settings_window.app, "quick_reference_opened", True
    )
    settings_window._select_tool("Command library")
    button = settings_window._widgets["commandListButton"]

    assert button.text() == "Command list..."
    settings_window.app.quick_reference_opened = False
    button.click()
    assert settings_window.app.quick_reference_opened is True


@pytest.mark.parametrize(
    "ready, expected",
    [
        (False, "Ava is not set up yet. Use the guide to get it ready."),
        (True, "Ava is ready. You can change or test the setup in the guide."),
    ],
)
def test_ava_guide_line_uses_cached_readiness(settings_window, monkeypatch, ready, expected):
    from samsara import ava_readiness
    from samsara.ui.settings_qt import PAGE_MODULES

    monkeypatch.setattr(
        ava_readiness, "readiness_for", lambda _app: SimpleNamespace(ready=ready)
    )
    ava_index = next(i for i, module in enumerate(PAGE_MODULES) if module.TITLE == "Ava")
    settings_window._ensure_destination(ava_index)

    assert settings_window._widgets["ava_setup_guide_line"].text() == expected

