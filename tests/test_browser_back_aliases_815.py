"""Queue 815: browser history phrases stay restricted to browser windows."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from samsara import command_scope as cs  # noqa: E402
from tools.dump_command_metadata import build_executor  # noqa: E402

BRAVE = cs.MatchContext.for_app("brave.exe", title="Example - Brave")
OUTSIDE = cs.MatchContext.for_app("notepad.exe", title="Untitled - Notepad")
BACK = {
    "back", "back page", "page back", "previous page", "return page",
    "go to the previous page", "last page",
}
FORWARD = {"forward page", "page forward", "next page"}


@pytest.fixture(scope="module")
def matcher():
    return build_executor()._matcher


@pytest.mark.parametrize("phrase", sorted(BACK))
def test_back_phrases_are_browser_only(phrase, matcher):
    found = matcher.match_detail(phrase, BRAVE)
    assert found is not None and found.entry.phrase == "go back"
    outside = matcher.match_detail(phrase, OUTSIDE)
    assert outside is None or outside.entry.phrase != "go back"


@pytest.mark.parametrize("phrase", sorted(FORWARD))
def test_forward_phrases_are_browser_only(phrase, matcher):
    found = matcher.match_detail(phrase, BRAVE)
    assert found is not None and found.entry.phrase == "go forward"
    outside = matcher.match_detail(phrase, OUTSIDE)
    assert outside is None or outside.entry.phrase != "go forward"


def test_forward_keeps_existing_stremio_owner(matcher):
    found = matcher.match_detail("forward", BRAVE)
    assert found is None or found.entry.phrase != "go forward"
