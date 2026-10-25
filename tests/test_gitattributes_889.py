"""Line-ending rules for repository assets and byte-pinned fixtures (889)."""
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ATTRIBUTES = ROOT / ".gitattributes"
EXPECTED_BINARY_EXTENSIONS = {".ico", ".jpg", ".mp4", ".png", ".wav"}
HASHED_LF_PATHS = {
    "assets/icon/samsara.svg",
    "assets/brand/samsara-mark.svg",
    "tests/fixtures/session_corrections_pre_move.json",
    "tests/fixtures/session_feedback_pre_move.json",
    "tests/fixtures/session_text_pre_move.json",
}


def _rules():
    return [line.split() for line in ATTRIBUTES.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _binary_extensions():
    found = set()
    for current, directories, filenames in os.walk(ROOT):
        directories[:] = [name for name in directories
                          if name not in {".git", "__pycache__", ".pytest_cache", ".venv", "venv"}]
        for filename in filenames:
            path = Path(current, filename)
            with path.open("rb") as stream:
                sample = stream.read(8192)
            try:
                sample.decode("utf-8")
                is_binary = b"\x00" in sample
            except UnicodeDecodeError:
                is_binary = True
            if is_binary:
                found.add(path.suffix.lower())
    return found


def test_every_binary_extension_in_tree_is_marked_binary():
    binary_extensions = _binary_extensions()
    assert binary_extensions == EXPECTED_BINARY_EXTENSIONS
    rules = _rules()
    for extension in sorted(binary_extensions):
        assert [f"*{extension}", "binary"] in rules


def test_byte_pinned_icons_and_golden_fixtures_are_lf():
    rules = _rules()
    for relative in sorted(HASHED_LF_PATHS):
        assert [f"/{relative}", "text", "eol=lf"] in rules
        assert (ROOT / relative).is_file()


def test_default_classifies_remaining_files_as_text_auto():
    assert ["*", "text=auto"] in _rules()

