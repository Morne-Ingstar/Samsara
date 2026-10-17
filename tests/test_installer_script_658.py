"""Start Menu shortcut tasks and uninstall link in the Inno installer."""

from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
ISS = REPO / "installer" / "samsara.iss"


def _section(text: str, name: str) -> str:
    match = re.search(rf"^\[{name}\]\s*$(?P<body>.*?)(?=^\[[A-Za-z]+\]\s*$|\Z)", text, re.M | re.S)
    assert match, f"missing [{name}] section"
    return match.group("body")


def test_start_menu_task_and_icons_are_optional_and_removed_on_uninstall():
    text = ISS.read_text(encoding="utf-8")
    tasks = _section(text, "Tasks")
    icons = _section(text, "Icons")

    assert 'Name: "startmenushortcut"' in tasks
    assert 'Description: "Create a Start Menu shortcut"' in tasks
    assert "Flags: checkedonce" in tasks
    assert 'Name: "{autoprograms}\\{#AppName}\\{#AppName}"' in icons
    assert 'Name: "{autoprograms}\\{#AppName}\\Uninstall {#AppName}"' in icons
    assert 'Filename: "{uninstallexe}"; Tasks: startmenushortcut' in icons
    assert icons.count("Tasks: startmenushortcut") == 2
    assert 'Name: "{autodesktop}\\{#AppName}"' in icons
    assert 'Flags: unchecked' in tasks
