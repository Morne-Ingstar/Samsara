"""Queue 143: the cube inherits structural Alt-Tab eligibility."""
import pytest

from plugins.commands import window_switcher as ws


@pytest.mark.parametrize("descriptor, expected", [
    ({"visible": True, "minimized": False, "title": "Tool", "owner": 0,
      "cloaked": False, "exstyle": ws.WS_EX_TOOLWINDOW}, False),
    ({"visible": True, "minimized": False, "title": "Shell", "owner": 0,
      "cloaked": True, "exstyle": 0}, False),
    ({"visible": True, "minimized": False, "title": "Open", "owner": 123,
      "cloaked": False, "exstyle": 0}, False),
    ({"visible": True, "minimized": False, "title": "Claude", "owner": 0,
      "cloaked": False, "exstyle": 0}, True),
    ({"visible": False, "minimized": True, "title": "Obsidian", "owner": 0,
      "cloaked": False, "exstyle": 0}, True),
])
def test_alt_tab_window_fixture(descriptor, expected):
    assert ws._alt_tab_eligible(descriptor) is expected


def test_appwindow_explicitly_restores_an_owned_tool_window():
    assert ws._alt_tab_eligible({
        "visible": True, "minimized": False, "title": "Application tool",
        "owner": 123, "cloaked": False,
        "exstyle": ws.WS_EX_TOOLWINDOW | ws.WS_EX_APPWINDOW,
    })
