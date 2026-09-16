"""Queue 129: colour lives in theme.py, and only in theme.py.

Why a test and not a style note: an agent editing one window sees
`color: #8A8A92` as a reasonable grey in a diff, not as the reason that
window will be the one black dialog in the middle of a light app. Samsara was
dark-only for its whole life, so every surface could get away with its own
literal. The moment a second palette exists, each one is a window that does
not switch -- and a half-themed accessibility tool is worse than a dark-only
one, because the user has been told the setting works.

What it enforces over samsara/, plugins/ and dictation.py:

  1. No colour literal in a STRING anywhere in the app: no `#rrggbb`, no
     `rgb()/rgba()`, no CSS/Qt colour name in a `color:`-style declaration.
     Comments are not scanned -- a hex in prose renders nothing.
  2. No `QColor(r, g, b)` / `QPen(QColor(...))` built from numbers. Painted
     colour comes from `theme.qcolor(theme.TOKEN)` like every other kind.
  3. Nothing evaluated at IMPORT time may capture a theme colour: a module
     constant (`_BG = theme.BG0`), a class attribute
     (`_IDLE = f"...{theme.BG2}"`) or a default argument
     (`def _label(..., color=theme.TEXT_PRIMARY)`). Each reads the token once
     and keeps that string for the life of the process, so a palette switch
     repaints everything except it. This is the same bug as a literal, one
     indirection further away, and all three kinds survived the first pass of
     this queue's own audit -- the default-argument one was visible as
     near-white card titles on a white card in ui_proof/129/home_light.png.

Exceptions: a violation is allowed only on a line carrying the comment
`colour-token: exempt -- <reason of at least 12 characters>` (same line or
the line above). Exemptions are counted and the count is pinned below, so
adding one is a visible edit to THIS file with its reason in the diff.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

import pytest

from samsara.ui import theme

ROOT = Path(__file__).resolve().parents[1]

SCAN_DIRS = ("samsara", "plugins")
SCAN_FILES = ("dictation.py",)
SCAN_SUFFIXES = (".py", ".qss", ".css")

#: theme.py IS the palette; it is the one file allowed to spell a colour out.
PALETTE_FILE = "samsara/ui/theme.py"

#: Pinned number of `colour-token: exempt` markers in the app. Change it only
#: together with the reason on each exempted line.
EXPECTED_EXEMPTIONS = 8

EXEMPT_RE = re.compile(r"colour-token:\s*exempt\s*--\s*(.{12,})", re.I)

_HEX = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3}(?:[0-9a-fA-F]{2})?)?\b")
_RGB_FUNC = re.compile(r"\brgba?\(\s*[\d.]+\s*,", re.I)
#: A colour NAME, but only where CSS is actually taking a colour -- otherwise
#: every string containing the word "black" would fail.
_NAMED = re.compile(
    r"""(?:^|[;{\s"'])(?:[a-z-]*colou?r|background)\s*:\s*"""
    r"(white|black|red|green|blue|gray|grey|yellow|cyan|magenta|orange|silver)\b",
    re.I)

#: Every token name the palette binds, so a constant capturing one is found
#: by name rather than by guessing.
COLOUR_TOKENS = (set(theme.PALETTE_TOKENS) | set(theme.DERIVED_TOKENS)) - {"POLARITY"}
_TOKEN_ATTR = re.compile(r"theme\.(" + "|".join(sorted(COLOUR_TOKENS)) + r")\b")
#: Helpers that RESOLVE a token into a concrete string; calling one at module
#: level freezes the result exactly as reading the token would.
_TOKEN_CALL = re.compile(
    r"(?:theme\.)?(?:mix|_mix|tint|wash|_rgba|qcolor|_tint)\s*\(")

#: Names that mention a colour token but are not themselves a colour.
NOT_A_COLOUR = frozenset({"PALETTE_TOKENS", "DERIVED_TOKENS", "COLOUR_TOKENS"})


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    kind: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.text}"


def _literal_violations(source: str, rel: str):
    """Colour literals, found in string TOKENS so comments are left alone."""
    out = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out
    for tok in tokens:
        if tok.type != tokenize.STRING:
            continue
        body = tok.string
        if _HEX.search(body):
            out.append(Violation(rel, tok.start[0], "hex colour literal",
                                 _HEX.search(body).group(0)))
        if _RGB_FUNC.search(body):
            out.append(Violation(rel, tok.start[0], "rgb()/rgba() literal",
                                 _RGB_FUNC.search(body).group(0)))
        named = _NAMED.search(body)
        if named:
            out.append(Violation(rel, tok.start[0], "named colour",
                                 named.group(0).strip()))
    return out


def _painted_violations(tree, rel: str):
    """QColor(...) / QPen(QColor(...)) built from numbers instead of a token."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "QColor" or not node.args:
            continue
        if all(isinstance(a, ast.Constant) and isinstance(a.value, (int, float))
               for a in node.args):
            out.append(Violation(rel, node.lineno, "QColor from numbers",
                                 ast.unparse(node)[:80]))
    return out


def _frozen_violations(tree, rel: str):
    """Constants that capture a theme colour when the file is imported.

    Module level AND class body: a class attribute is evaluated when the class
    statement runs, which is import time too. dictation.py's `_MODE_OVERLAY`
    dict was exactly that, and it is why this walks both."""
    out = []
    scopes = [("module constant", tree.body)]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            scopes.append((f"{node.name} class attribute", node.body))
    for kind, body in scopes:
        for node in body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id in NOT_A_COLOUR:
                continue
            text = ast.unparse(node.value)
            if _TOKEN_ATTR.search(text) or _TOKEN_CALL.search(text):
                out.append(Violation(rel, node.lineno, f"{kind} freezes a token",
                                     f"{target.id} = {text[:60]}"))
    out.extend(_default_argument_violations(tree, rel))
    return out


def _default_argument_violations(tree, rel: str):
    """`def _label(..., color=theme.TEXT_PRIMARY)`.

    A default is evaluated when the `def` runs, so it is import-time too --
    and this one is worse than a constant, because it is invisible at the
    call site. Home's card titles rendered as the dark palette's near-white
    on a white card because of exactly this."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d]
        for default in defaults:
            text = ast.unparse(default)
            if _TOKEN_ATTR.search(text) or _TOKEN_CALL.search(text):
                out.append(Violation(rel, node.lineno,
                                     "default argument freezes a token",
                                     f"{node.name}(... = {text[:50]})"))
    return out


def _app_files():
    for directory in SCAN_DIRS:
        for path in sorted((ROOT / directory).rglob("*")):
            if path.suffix in SCAN_SUFFIXES and "__pycache__" not in path.parts:
                yield path
    for name in SCAN_FILES:
        yield ROOT / name


def scan_source(source: str, rel: str):
    out = _literal_violations(source, rel)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    out.extend(_painted_violations(tree, rel))
    out.extend(_frozen_violations(tree, rel))
    return out


def scan_app():
    """(violations, exemptions) over the whole app."""
    violations, exemptions = [], []
    for path in _app_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == PALETTE_FILE:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        for violation in scan_source(source, rel):
            here = lines[violation.line - 1] if 0 < violation.line <= len(lines) else ""
            above = lines[violation.line - 2] if violation.line >= 2 else ""
            if EXEMPT_RE.search(here) or EXEMPT_RE.search(above):
                exemptions.append(violation)
            else:
                violations.append(violation)
    return violations, exemptions


# --- the standard ---------------------------------------------------------------

def test_no_colour_literal_outside_theme():
    violations, _ = scan_app()
    assert not violations, (
        "colour must come from samsara/ui/theme.py, not from a literal. "
        f"{len(violations)} violation(s):\n"
        + "\n".join(str(v) for v in violations[:60])
    )


def test_exemptions_are_pinned():
    _, exemptions = scan_app()
    assert len(exemptions) == EXPECTED_EXEMPTIONS, (
        "colour-token exemptions changed; update EXPECTED_EXEMPTIONS only with a "
        "reason on each line:\n" + "\n".join(str(v) for v in exemptions)
    )


def test_exemption_needs_a_reason():
    assert EXEMPT_RE.search("x = 1  # colour-token: exempt -- a screen blackout, not a surface")
    assert not EXEMPT_RE.search("x = 1  # colour-token: exempt")
    assert not EXEMPT_RE.search("x = 1  # colour-token: exempt -- because")


# --- the scanner itself ---------------------------------------------------------

@pytest.mark.parametrize("source, kind", [
    ('x = "color: #8A8A92;"', "hex colour literal"),
    ('x = "background: rgba(255,255,255,0.06);"', "rgb()/rgba() literal"),
    ('x = "QLabel { color: white; }"', "named colour"),
    ('x = QColor(18, 18, 22, 230)', "QColor from numbers"),
    ("_BG = theme.BG0", "module constant freezes a token"),
    ('_DIM = theme.mix(theme.ACCENT, theme.BG0, 0.5)', "module constant freezes a token"),
    ('class W:\n    _SS = f"color: {theme.ACCENT};"', "W class attribute freezes a token"),
    ('def f(color=theme.TEXT_PRIMARY):\n    return color',
     "default argument freezes a token"),
])
def test_scanner_flags_each_kind(source, kind):
    kinds = {v.kind for v in scan_source(source, "probe.py")}
    assert kind in kinds, f"{source!r} -> {kinds}"


@pytest.mark.parametrize("source", [
    # Inside a function the token is read per call, which is the whole point.
    'def f():\n    return f"color: {theme.TEXT_SECONDARY};"',
    'def f():\n    return f"background: {theme.wash(0.06)};"',
    'def f():\n    return theme.qcolor(theme.ACCENT)',
    'def _ss():\n    return f"background: {theme.BG0};"',
    'BASE = "plain"',
    '# a comment mentioning #8A8A92 renders nothing\nx = 1',
])
def test_scanner_passes_token_usage(source):
    assert not scan_source(source, "probe.py")


def test_a_deliberately_introduced_literal_fails_the_app_scan(tmp_path, monkeypatch):
    """The scan must actually reach the app, not silently walk an empty tree."""
    probe = tmp_path / "samsara" / "ui"
    probe.mkdir(parents=True)
    (probe / "regression.py").write_text(
        'BAD = "color: #123456;"\n', encoding="utf-8")
    monkeypatch.setattr("tests.test_colour_tokens.ROOT", tmp_path)
    monkeypatch.setattr("tests.test_colour_tokens.SCAN_FILES", ())
    violations, _ = scan_app()
    assert [v.kind for v in violations] == ["hex colour literal"]
