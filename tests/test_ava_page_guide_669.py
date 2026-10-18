from types import SimpleNamespace

import pytest


class _App:
    def __init__(self):
        self.config = {}

    def open_ava_guide(self):
        self.guide_opened = True

    def update_config(self, changes, save=True):
        self.config.update(changes)

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass

    def play_sound(self, *args, **kwargs):
        pass


@pytest.fixture
def ava_page(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui.settings_qt import PAGE_MODULES, _SettingsWindow

    monkeypatch.setattr(autostart, "sync_run_entry", lambda *args, **kwargs: None)
    window = _SettingsWindow(_App())
    ava_index = next(i for i, module in enumerate(PAGE_MODULES) if module.TITLE == "Ava")
    page = window._ensure_destination(ava_index)
    yield window, page
    window.deleteLater()


def test_ava_page_guide_is_first_and_opens_guide(ava_page):
    window, page = ava_page

    assert page.base.itemAt(0).widget() is page.base_rows[0]
    assert window._widgets["ava_setup_guide_button"].text() == "Set up Ava..."
    window.app.guide_opened = False
    window._widgets["ava_setup_guide_button"].click()
    assert window.app.guide_opened is True


@pytest.mark.parametrize(
    "ready, expected",
    [
        (False, "Ava is not set up yet. Use the guide to get it ready."),
        (True, "Ava is ready. You can change or test the setup in the guide."),
    ],
)
def test_ava_guide_line_tracks_cached_readiness(ava_page, monkeypatch, ready, expected):
    from samsara import ava_readiness

    window, _page = ava_page
    monkeypatch.setattr(
        ava_readiness, "readiness_for", lambda _app: SimpleNamespace(ready=ready)
    )

    window._refresh_conditions()

    assert window._widgets["ava_setup_guide_line"].text() == expected
