"""Queue 55: the streaming preview's rendering, and spoken-number formatting.

A. The preview label showed "it&#x27;s": set_transcript sends escaped HTML, and
   QLabel's AutoText guessed PLAIN for a line with no tag. These tests build the
   real widget and read back what Qt DISPLAYS (plain text of the rendered
   document), which is what would have caught the original bug.
B. format_numbers rewrote every prose "one" to "1" ("like the 1 in it's").

Never imports dictation: process_transcription is compiled out of the source.
"""
import ast
import html
import re
import types
from pathlib import Path

import pytest

from samsara import streaming
from samsara.number_format import format_spoken_numbers

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A. Render
# ---------------------------------------------------------------------------

def _displayed(label) -> str:
    """What the label actually shows, by Qt's own rules: RichText (or AutoText
    that Qt detects as rich) renders the HTML; anything else shows the string
    as-is, entities included."""
    from PySide6.QtGui import Qt, QTextDocument
    fmt = label.textFormat()
    text = label.text()
    is_rich = fmt == Qt.TextFormat.RichText or (
        fmt == Qt.TextFormat.AutoText and Qt.mightBeRichText(text))
    if not is_rich:
        return text
    doc = QTextDocument()
    doc.setHtml(text)
    return doc.toPlainText().replace(" ", "\n").replace(" ", "\n")


@pytest.fixture
def overlay(qapp):
    ov = streaming.StreamingOverlayQt()
    ov._widget = streaming._StreamingWidget(False)
    yield ov
    ov._widget._w.deleteLater()


def _render(qapp, ov, finals, partial=""):
    ov.set_transcript(finals, partial)
    qapp.processEvents()
    return _displayed(ov._widget._w._label)


@pytest.mark.parametrize("text", ["it's", "don't", "we've", "don't, we've, it's",
                                  "like the one in it's"])
def test_contractions_render_as_typed_in_the_streaming_window(qapp, overlay, text):
    assert _render(qapp, overlay, [text]) == text
    assert "&#" not in _render(qapp, overlay, [text])


def test_the_original_bug_is_what_autotext_would_show(qapp):
    """Documents the root cause: Qt treats this escaped, tag-free line as plain."""
    from PySide6.QtGui import Qt
    escaped = html.escape("it's")
    assert escaped == "it&#x27;s"
    assert Qt.mightBeRichText(escaped) is False


@pytest.mark.parametrize("text", [
    "salt & pepper", 'say "hi"', "x > y", "a < b",
    "• first item",                 # formatting_tokens bullet
    "it’s “quoted” — dash",
    "café naïve über",
])
def test_every_escaped_or_non_ascii_character_renders_as_typed(qapp, overlay, text):
    assert _render(qapp, overlay, [text]) == text


def test_partial_and_paused_paths_also_render_rich(qapp, overlay):
    assert _render(qapp, overlay, ["it's"], "don't") == "it's\ndon't"
    overlay.set_paused(["we've"])
    qapp.processEvents()
    assert _displayed(overlay._widget._w._label) == "we've\nPaused (hold)"


def test_formatting_token_newline_still_breaks_the_line(qapp, overlay):
    assert _render(qapp, overlay, ["line one\nline two"]) == "line one\nline two"


def test_plain_update_text_keeps_autotext_for_hold_to_stream(qapp, overlay):
    from PySide6.QtGui import Qt
    overlay.update_text("raw partial it's", "listening")
    qapp.processEvents()
    label = overlay._widget._w._label
    assert label.textFormat() == Qt.TextFormat.AutoText
    assert _displayed(label) == "raw partial it's"


# ---------------------------------------------------------------------------
# B. Numbers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "like the one in it's",              # live log 2026-09-14 22:36:54 -> 22:37:18
    "one day I will", "two options", "the top three", "no one came",
    "a hundred times", "one-on-one", "one's own", "I have one, two, three things",
    "zero chance", "four of us", "nine lives",
])
def test_prose_number_words_stay_words(text):
    assert format_spoken_numbers(text) == text


@pytest.mark.parametrize("spoken, expected", [
    ("page one", "page 1"),
    ("volume two and step three", "volume 2 and step 3"),
    ("number one fan", "number 1 fan"),
    ("twenty one", "21"),
    ("twenty-one", "21"),
    ("twenty five dollars", "25 dollars"),
    ("ten of them", "10 of them"),
    ("one two three four", "1 2 3 4"),
    ("three percent", "3 percent"),
    ("chapter nine, then one more", "chapter 9, then one more"),
])
def test_genuine_number_contexts_still_convert(spoken, expected):
    assert format_spoken_numbers(spoken) == expected


def test_whitespace_and_line_breaks_are_preserved():
    """The old rewrite rebuilt text with ' '.join(text.split())."""
    assert format_spoken_numbers("page one\n\nand  two options") == "page 1\n\nand  two options"


def _process_transcription():
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "process_transcription")
    module = types.ModuleType("process_transcription_55")
    module.__dict__["re"] = re
    exec(compile(ast.Module(body=[node], type_ignores=[]), "dictation.py", "exec"), module.__dict__)
    return module.__dict__["process_transcription"]


@pytest.mark.parametrize("fmt", [True, False])
def test_like_the_one_in_its_survives_the_commit_pipeline(fmt):
    app = types.SimpleNamespace(config={"format_numbers": fmt, "auto_capitalize": True})
    out = _process_transcription()(app, "apostrophes that are between letters like the one in it's")
    assert out == "Apostrophes that are between letters like the one in it's"


def test_commit_pipeline_still_converts_a_number_context():
    app = types.SimpleNamespace(config={"format_numbers": True, "auto_capitalize": True})
    assert _process_transcription()(app, "turn to page twenty one") == "Turn to page 21"
