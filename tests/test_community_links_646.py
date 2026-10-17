from pathlib import Path

from PySide6.QtWidgets import QPushButton

from samsara.ui import community_links
from samsara.ui.settings import help_page
from samsara.ui.settings_qt import _SettingsWindow
from tests.test_settings_pages import _StubApp, _schema_defaults


REPO = Path(__file__).resolve().parents[1]


def test_help_page_has_one_community_row(qapp):
    source = (REPO / "samsara/ui/settings/help_page.py").read_text(encoding="utf-8")
    rows = [row for row in help_page.BASE if row[0] == "Help and community"]
    assert len(rows) == 1
    assert rows[0][2] == "communityActions"
    assert source.count("community_actions()") == 1


def test_community_row_is_on_the_lazy_help_page(qapp):
    window = _SettingsWindow(_StubApp(_schema_defaults()))
    try:
        page = window._pages["Help"]
        buttons = {button.text(): button for button in page.findChildren(QPushButton)}
        assert buttons["Discord"].objectName() == "communityDiscordButton"
        assert buttons["Email"].objectName() == "communityEmailButton"
        assert page.isAncestorOf(buttons["Discord"])
        assert page.isAncestorOf(buttons["Email"])
    finally:
        window.deleteLater()


def test_community_buttons_open_exact_urls_without_network(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(community_links.QDesktopServices, "openUrl", opened.append)
    actions = community_links.community_actions()
    buttons = {button.text(): button for button in actions.findChildren(QPushButton)}

    assert set(buttons) == {"Discord", "Email"}
    assert buttons["Discord"].accessibleName()
    assert buttons["Email"].accessibleName()
    buttons["Discord"].click()
    buttons["Email"].click()

    assert [url.toString() for url in opened] == [
        "https://discord.gg/tPYvf369hQ",
        "mailto:me@morneis.com",
    ]
