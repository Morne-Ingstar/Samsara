"""Queue 83: a minimum readable type size, enforced over the whole app.

Why a test and not a style note: an agent editing a window sees `font-size:
11px` as a number that looks reasonable in a diff, not as text a low-vision
user cannot read. Every window used to be fixed locally and by eye, and the
next change wrote 11 again. This test is the standard, and it fails the build.

What it enforces (samsara/, plugins/, dictation.py -- the app's own UI; the
marketing site under website/ is not the app):

  1. The floors live HERE, not only in theme.py, so lowering a token fails:
     absolute floor 14 px, body floor 16 px, weight floor 400. See the
     justification block above the type scale in samsara/ui/theme.py.
  2. No numeric font size anywhere in a string: `font-size: 12px`, `12pt`,
     `font: 12px ...` (QSS, inline HTML, CSS).
  3. A font size in an f-string must be a theme token: `{theme.TYPE_BODY}px`
     (or Home's `{_px(TYPE[...])}`, which scales a token by the Windows text
     size). A local (`{size}px`), arithmetic (`{TYPE_BODY - 2}px`) or unknown
     name bypasses the scale and fails.
  4. No point sizes: `QFont("Segoe UI", 9)`, `setPointSize(...)`. Use
     `theme.qfont(theme.TYPE_*)` or `setPixelSize(theme.TYPE_*)`.
  5. `setPixelSize(x)` only with a token (or `_px(token)` / arithmetic on a
     local inside a helper that is exempted with a reason).
  6. No thin weights: `font-weight` 100-300 / light / lighter, QFont.Light /
     Thin / ExtraLight, or a Light/Semilight/Thin face name.

Exceptions: a violation is allowed only on a line carrying the comment
`type-floor: exempt -- <reason of at least 12 characters>` (same line or the
line above). Exemptions are counted and the count is pinned below, so adding
one is a visible edit to THIS file with its reason in the diff. Files a
running queue owns can be listed in PENDING_FILES with that queue's id; the
test reports them, and fails once the file is clean so the entry is removed.
"""

from __future__ import annotations

import ast
import io
import sys
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FLOOR_ABSOLUTE_PX = 14
FLOOR_BODY_PX = 16
FLOOR_WEIGHT = 400

SCAN_DIRS = ("samsara", "plugins")
SCAN_FILES = ("dictation.py",)
SCAN_SUFFIXES = (".py", ".qss", ".css")

#: Reading-text roles: these tokens must meet the BODY floor.
BODY_ROLE_TOKENS = ("TYPE_BODY", "TYPE_SECONDARY", "TYPE_CREED", "FONT_SIZE_BODY")

#: Pinned number of `type-floor: exempt` markers in the app. Change it only
#: together with the reason on the exempted line.
EXPECTED_EXEMPTIONS = 3

#: path (posix, repo-relative) -> why it is not migrated yet.
PENDING_FILES: dict[str, str] = {
    # Queue 75 is editing both files right now (streaming preview idle fade and
    # its Settings controls). Migrating them here would collide with that work.
    "samsara/streaming.py": "queue 75 owns it; FONT_SIZE/DIM_FONT_SIZE/HINT_FONT_SIZE -> theme tokens when it lands",
    "samsara/ui/settings/modes_qt.py": "queue 75 owns it; 10 literal sizes -> theme tokens when it lands",
}

EXEMPT_RE = re.compile(r"type-floor:\s*exempt\s*--\s*(.{12,})", re.I)

_NUM_SIZE = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt|em|rem|%)?", re.I)
_EXPR_SIZE = re.compile(r"font-size\s*:\s*\{([^{}]+)\}", re.I)
_SHORTHAND = re.compile(r"(?<![-\w])font\s*:\s*[^;{}\"']*?\b\d+(?:\.\d+)?\s*(px|pt)\b", re.I)
_THIN_WEIGHT = re.compile(r"font-weight\s*:\s*([1-3]00|lighter|light|thin)\b", re.I)
_THIN_FACE = re.compile(r"\b(?:Segoe UI|Arial|Calibri|Helvetica)\s+(?:Light|Semilight|SemiLight|Thin|ExtraLight)\b", re.I)
_TOKEN_NAME = re.compile(r"^(?:[A-Za-z_]\w*\.)?(TYPE_[A-Z_]+|FONT_SIZE_[A-Z_]+)$")
_PX_HELPER = re.compile(r"^_px\(\s*(?:[A-Za-z_]\w*\.)?(?:TYPE_[A-Z_]+|FONT_SIZE_[A-Z_]+|TYPE\[[^\]]+\])\s*\)$")


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    text: str

    def __str__(self):
        return f"{self.path}:{self.line}: {self.rule}: {self.text}"


def _theme_tokens() -> dict[str, int]:
    from samsara.ui import theme

    return {name: getattr(theme, name) for name in dir(theme)
            if re.match(r"^(TYPE_[A-Z_]+|FONT_SIZE_[A-Z_]+)$", name)
            and isinstance(getattr(theme, name), int)}


def _string_spans(source: str):
    """(start_line, text) for every string literal, f-strings whole (3.11 and 3.12+)."""
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def at(pos):
        return offsets[pos[0] - 1] + pos[1]

    fstart = None
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        name = tokenize.tok_name.get(tok.type, "")
        if name == "STRING":
            yield tok.start[0], source[at(tok.start):at(tok.end)]
        elif name == "FSTRING_START":
            fstart = tok.start
        elif name == "FSTRING_END" and fstart is not None:
            yield fstart[0], source[at(fstart):at(tok.end)]
            fstart = None


def scan_source(source: str, path: str, tokens: dict[str, int]):
    """All violations in one file's source (see module docstring)."""
    found: list[Violation] = []
    if path.endswith((".qss", ".css")):
        spans = [(1, source)]
    else:
        try:
            spans = list(_string_spans(source))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            spans = [(1, source)]

    def line_of(start_line, text, index):
        return start_line + text.count("\n", 0, index)

    for start_line, text in spans:
        for m in _NUM_SIZE.finditer(text):
            found.append(Violation(path, line_of(start_line, text, m.start()), "literal font size", m.group(0)))
        for m in _EXPR_SIZE.finditer(text):
            expr = m.group(1).strip()
            ok = False
            tm = _TOKEN_NAME.match(expr)
            if tm and tm.group(1) in tokens:
                ok = True
            elif _PX_HELPER.match(expr):
                ok = True
            if not ok:
                found.append(Violation(path, line_of(start_line, text, m.start()),
                                       "font size bypasses the type tokens", m.group(0)))
        for m in _SHORTHAND.finditer(text):
            found.append(Violation(path, line_of(start_line, text, m.start()), "literal font shorthand", m.group(0)))
        for m in _THIN_WEIGHT.finditer(text):
            found.append(Violation(path, line_of(start_line, text, m.start()), "thin font weight", m.group(0)))
        for m in _THIN_FACE.finditer(text):
            found.append(Violation(path, line_of(start_line, text, m.start()), "thin font face", m.group(0)))

    if path.endswith(".py"):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        for node in ast.walk(tree) if tree is not None else ():
            if isinstance(node, ast.Call):
                func = node.func
                fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if fname in ("setPointSize", "setPointSizeF"):
                    found.append(Violation(path, node.lineno, "point size (use px tokens)", ast.unparse(node)[:80]))
                elif fname == "setPixelSize" and node.args and not _is_token_expr(node.args[0], tokens):
                    found.append(Violation(path, node.lineno, "pixel size bypasses the type tokens",
                                           ast.unparse(node)[:80]))
                elif fname == "QFont" and (len(node.args) >= 2 or any(k.arg == "pointSize" for k in node.keywords)):
                    found.append(Violation(path, node.lineno, "QFont point size (use theme.qfont)",
                                           ast.unparse(node)[:80]))
            elif isinstance(node, ast.Attribute) and node.attr in ("Light", "Thin", "ExtraLight"):
                base = ast.unparse(node.value)
                if base.endswith("QFont") or base.endswith("QFont.Weight"):
                    found.append(Violation(path, node.lineno, "thin font weight", ast.unparse(node)))
    return found


def _is_token_expr(node, tokens) -> bool:
    text = ast.unparse(node)
    tm = _TOKEN_NAME.match(text)
    return bool((tm and tm.group(1) in tokens) or _PX_HELPER.match(text))


def _app_files():
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if p.suffix in SCAN_SUFFIXES and "__pycache__" not in p.parts:
                yield p
    for f in SCAN_FILES:
        yield ROOT / f


def scan_app():
    """(violations, exemptions, pending) over the whole app."""
    tokens = _theme_tokens()
    violations, exemptions, pending = [], [], {}
    for p in _app_files():
        rel = p.relative_to(ROOT).as_posix()
        source = p.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        file_violations = []
        for v in scan_source(source, rel, tokens):
            here = lines[v.line - 1] if 0 < v.line <= len(lines) else ""
            above = lines[v.line - 2] if v.line >= 2 else ""
            if EXEMPT_RE.search(here) or EXEMPT_RE.search(above):
                exemptions.append(v)
            else:
                file_violations.append(v)
        if rel in PENDING_FILES:
            pending[rel] = file_violations
        else:
            violations.extend(file_violations)
    return violations, exemptions, pending


# --- the floors themselves ------------------------------------------------------

def test_theme_floors_meet_the_standard():
    from samsara.ui import theme

    assert theme.TYPE_FLOOR_ABSOLUTE >= FLOOR_ABSOLUTE_PX
    assert theme.TYPE_FLOOR_BODY >= FLOOR_BODY_PX
    assert theme.FONT_WEIGHT_MIN >= FLOOR_WEIGHT
    tokens = _theme_tokens()
    below = {n: v for n, v in tokens.items() if n != "TYPE_FLOOR_BODY" and v < FLOOR_ABSOLUTE_PX}
    assert not below, f"type tokens below the {FLOOR_ABSOLUTE_PX} px floor: {below}"
    body = {n: tokens[n] for n in BODY_ROLE_TOKENS if tokens[n] < FLOOR_BODY_PX}
    assert not body, f"reading-text tokens below the {FLOOR_BODY_PX} px body floor: {body}"
    assert all(v >= FLOOR_ABSOLUTE_PX for v in theme.HOME_TYPE_SCALE.values())


def test_headings_stay_above_body():
    from samsara.ui import theme

    assert theme.TYPE_MIN < theme.TYPE_BODY < theme.TYPE_EMPHASIS < theme.TYPE_HEADING < theme.TYPE_TITLE


def test_qfont_helper_sizes_in_pixels():
    from samsara.ui import theme

    font = theme.qfont(theme.TYPE_BODY)
    assert font.pixelSize() == theme.TYPE_BODY
    assert font.pointSize() == -1


# --- the whole app ----------------------------------------------------------------

def test_no_ui_text_below_the_floor_or_outside_the_tokens():
    violations, _, _ = scan_app()
    assert not violations, (
        f"{len(violations)} font size(s) below the floor or bypassing samsara/ui/theme.py tokens:\n"
        + "\n".join(str(v) for v in violations[:80])
    )


def test_exemptions_are_pinned():
    _, exemptions, _ = scan_app()
    assert len(exemptions) == EXPECTED_EXEMPTIONS, (
        "type-floor exemptions changed; update EXPECTED_EXEMPTIONS only with a reason on each line:\n"
        + "\n".join(str(v) for v in exemptions)
    )


def test_pending_files_are_still_pending():
    _, _, pending = scan_app()
    clean = [path for path, found in pending.items() if not found]
    assert not clean, f"remove from PENDING_FILES, they now pass: {clean}"


# --- the scanner catches what it claims to ------------------------------------------

_BAD = '''
from PySide6.QtGui import QFont
from samsara.ui import theme
a = "QLabel { font-size: 11px; }"
b = f"color: red; font-size: {size}px;"
c = f"font-size: {theme.TYPE_BODY - 4}px"
d = QFont("Segoe UI", 9)
e = "font: bold 12px 'Segoe UI'"
f = "font-weight: 300"
g = QFont("Segoe UI Light")
label.setFont(QFont("Segoe UI"))
font.setPointSize(10)
font.setPixelSize(13)
h = QFont.Weight.Light
i = f"font-size: {theme.NOT_A_TOKEN}px"
'''

_GOOD = '''
from samsara.ui import theme
a = f"QLabel {{ font-size: {theme.TYPE_BODY}px; }}"
b = f"font-size: {theme.TYPE_MIN}px; font-weight: 600;"
c = theme.qfont(theme.TYPE_HEADING, weight=600)
font.setPixelSize(theme.TYPE_TITLE)
d = f"font-size: {_px(TYPE['button'])}px"
# a comment that mentions font-size: 11px is not UI text
'''


def test_scanner_flags_each_kind_of_bypass():
    tokens = _theme_tokens()
    rules = sorted({(v.line, v.rule) for v in scan_source(_BAD, "bad.py", tokens)})
    lines = {line for line, _ in rules}
    for expected_line in (4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15):
        assert expected_line in lines, f"line {expected_line} of the bad sample was not flagged: {rules}"
    assert 11 not in lines  # QFont(family) alone is fine


def test_scanner_passes_token_sizes():
    assert scan_source(_GOOD, "good.py", _theme_tokens()) == []


def test_a_deliberately_introduced_small_size_fails_the_app_scan(tmp_path, monkeypatch):
    """Negative control on the real scan path: add one `font-size: 11px` to a
    copy of a real UI module and the app scan reports it."""
    real = ROOT / "samsara" / "ui" / "home_qt.py"
    fake_root = tmp_path / "repo"
    (fake_root / "samsara" / "ui").mkdir(parents=True)
    (fake_root / "plugins").mkdir()
    (fake_root / "dictation.py").write_text("", encoding="utf-8")
    source = real.read_text(encoding="utf-8") + '\n_REGRESSION = "QLabel { font-size: 11px; }"\n'
    (fake_root / "samsara" / "ui" / "home_qt.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", fake_root)
    violations, _, _ = scan_app()
    assert any("11px" in v.text and v.path.endswith("home_qt.py") for v in violations)


def test_exemption_needs_a_reason():
    assert EXEMPT_RE.search("x = 1  # type-floor: exempt -- glyph box is sized to the key cap")
    assert not EXEMPT_RE.search("x = 1  # type-floor: exempt")
    assert not EXEMPT_RE.search("x = 1  # type-floor: exempt -- too small")
