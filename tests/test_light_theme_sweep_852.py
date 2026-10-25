"""Regression checks for the 852 light-theme render sweep."""
from pathlib import Path
import re

from samsara.ui import theme

# No production UI module needed a correction after the rendered survey.
# Keep this list scoped to production sources actually changed by this sweep.
TOUCHED_UI_FILES: tuple[str, ...] = ()
_LITERAL_HEX = re.compile(r"(?i)(?<![0-9a-f])#[0-9a-f]{6}(?![0-9a-f])")
_LITERAL_QCOLOR = re.compile(r"(?i)\bQColor\s*\(\s*['\"]\s*#[0-9a-f]{6}")


def test_touched_ui_sources_use_theme_tokens_for_colors() -> None:
    repo = Path(__file__).resolve().parents[1]
    for relative in TOUCHED_UI_FILES:
        if Path(relative).as_posix() == "samsara/ui/theme.py":
            continue
        source = (repo / relative).read_text(encoding="utf-8")
        assert not _LITERAL_HEX.search(source), f"literal hex colour in {relative}"
        assert not _LITERAL_QCOLOR.search(source), f"literal QColor in {relative}"


def test_dark_and_light_theme_token_sets_match() -> None:
    assert set(theme.PALETTES["dark"]) == set(theme.PALETTES["light"])
