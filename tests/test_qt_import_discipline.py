"""Queue 179: Qt objects must not be born while an app module is imported.

Queue 173 once created a QObject at module scope in ``theme.py``.  Qt's
application lives on its own thread here, so that QObject was born on the
wrong thread before QApplication existed and crashed every startup.

The scanner covers samsara/, plugins/ and dictation.py.  It rejects a known
Qt object constructor at module/class scope or in a function default, since
all three expressions run while the module is imported.  A call inside a
function or lambda runs later and is intentionally allowed.

Exceptions require ``qt-import: exempt -- <reason of at least 12 characters>``
on the same or preceding line.  The pinned count makes every exception a
reviewable change to this guard rather than a quiet permanent loophole.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCAN_DIRS = ("samsara", "plugins")
SCAN_FILES = ("dictation.py",)
SCAN_SUFFIXES = (".py",)

#: Pinned number of `qt-import: exempt` markers applied to real violations.
EXPECTED_EXEMPTIONS = 0

EXEMPT_RE = re.compile(r"qt-import:\s*exempt\s*--\s*(.{12,})", re.I)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    kind: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.text}"


def _qt_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return Qt constructors and Qt module aliases imported by this file.

    Names are resolved only from PySide6 imports in the current source file;
    a project helper merely named ``Query`` therefore cannot trigger this rule.
    """
    constructors: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if not node.module.startswith("PySide6"):
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name.startswith("Q"):
                    constructors.add(bound)
                elif node.module == "PySide6":
                    modules.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("PySide6"):
                    continue
                # `import PySide6.QtCore as QtCore` binds QtCore; without an
                # alias Python binds PySide6, which also covers PySide6.QtGui.QIcon.
                modules.add(alias.asname or alias.name.split(".")[0])
    return constructors, modules


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_qt_base(base: ast.AST, constructors: set[str], modules: set[str],
                local_subclasses: set[str]) -> bool:
    if isinstance(base, ast.Name):
        return base.id in constructors or base.id in local_subclasses
    if isinstance(base, ast.Attribute):
        return base.attr.startswith("Q") and _root_name(base) in modules
    return False


def _local_qt_subclasses(tree: ast.AST, constructors: set[str],
                         modules: set[str]) -> set[str]:
    """Resolve same-file subclass chains; cross-file inheritance is unknown."""
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    known: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name in known:
                continue
            if any(_is_qt_base(base, constructors, modules, known)
                   for base in node.bases):
                known.add(node.name)
                changed = True
    return known


def _is_type_checking(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute)
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
        and test.attr == "TYPE_CHECKING"
    )


def _is_main_guard(test: ast.AST) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    values = [test.left, *test.comparators]
    return any(isinstance(value, ast.Name) and value.id == "__name__"
               for value in values) and any(
                   isinstance(value, ast.Constant) and value.value == "__main__"
                   for value in values)


class _ImportConstructionVisitor(ast.NodeVisitor):
    """Visit only expressions that evaluate as the module/class is defined."""

    def __init__(self, rel: str, constructors: set[str], modules: set[str],
                 local_subclasses: set[str]):
        self.rel = rel
        self.constructors = constructors
        self.modules = modules
        self.local_subclasses = local_subclasses
        self.violations: list[Violation] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_signature(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _visit_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Decorators and defaults evaluate when the def statement executes.
        for decorator in node.decorator_list:
            self.visit(decorator)
        defaults = [*node.args.defaults, *(default for default in node.args.kw_defaults
                                           if default is not None)]
        for default in defaults:
            self.visit(default)
        # The function body (including nested functions) is deferred until call time.

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # A lambda's body is deferred too. Lambdas have no default arguments.
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Class decorators and keywords are evaluated while defining the class;
        # then every class-body expression is evaluated at import time.
        for decorator in node.decorator_list:
            self.visit(decorator)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for child in node.body:
            self.visit(child)

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test) or _is_main_guard(node.test):
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = ""
        is_qt = False
        if isinstance(node.func, ast.Name):
            name = node.func.id
            is_qt = name in self.local_subclasses or (
                name.startswith("Q") and name in self.constructors)
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
            is_qt = name.startswith("Q") and _root_name(node.func.value) in self.modules
        if is_qt:
            self.violations.append(Violation(
                self.rel, node.lineno, "Qt object constructed at import time",
                ast.unparse(node)[:100],
            ))
        self.generic_visit(node)


def _construction_violations(tree: ast.AST, rel: str) -> list[Violation]:
    constructors, modules = _qt_names(tree)
    local_subclasses = _local_qt_subclasses(tree, constructors, modules)
    visitor = _ImportConstructionVisitor(rel, constructors, modules, local_subclasses)
    visitor.visit(tree)
    return visitor.violations


def _app_files():
    for directory in SCAN_DIRS:
        for path in sorted((ROOT / directory).rglob("*")):
            if path.suffix in SCAN_SUFFIXES and "__pycache__" not in path.parts:
                yield path
    for name in SCAN_FILES:
        yield ROOT / name


def scan_source(source: str, rel: str) -> list[Violation]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return _construction_violations(tree, rel)


def scan_app():
    """Return ``(violations, exemptions)`` over the whole application."""
    violations, exemptions = [], []
    for path in _app_files():
        rel = path.relative_to(ROOT).as_posix()
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

def test_no_qt_object_constructed_at_import_time():
    violations, _ = scan_app()
    assert not violations, (
        "Qt objects must be constructed on the Qt runtime thread, not during "
        "a module import. "
        f"{len(violations)} violation(s):\n" + "\n".join(str(v) for v in violations[:60])
    )


def test_exemptions_are_pinned():
    _, exemptions = scan_app()
    assert len(exemptions) == EXPECTED_EXEMPTIONS, (
        "qt-import exemptions changed; update EXPECTED_EXEMPTIONS only with a "
        "reason on each exempted line:\n" + "\n".join(str(v) for v in exemptions)
    )


def test_exemption_needs_a_reason():
    assert EXEMPT_RE.search("x = 1  # qt-import: exempt -- constructed by managed runtime")
    assert not EXEMPT_RE.search("x = 1  # qt-import: exempt")
    assert not EXEMPT_RE.search("x = 1  # qt-import: exempt -- because")


# --- the scanner itself ---------------------------------------------------------

def test_regression_theme_signal_at_module_scope_is_reported():
    source = """from PySide6.QtCore import QObject, Signal
class _ThemeChangeSignal(QObject):
    changed = Signal()
_SIG = _ThemeChangeSignal()
"""
    violations = scan_source(source, "theme_probe.py")
    assert [(v.line, v.kind) for v in violations] == [
        (4, "Qt object constructed at import time"),
    ]


@pytest.mark.parametrize("source", [
    "from PySide6.QtCore import QTimer\ndef build():\n    return QTimer()\n",
    "from PySide6.QtCore import QObject, Signal\nclass Signals(QObject):\n    changed = Signal()\n",
    "from PySide6.QtCore import QTimer\nif TYPE_CHECKING:\n    timer = QTimer()\n",
    "from PySide6.QtCore import QTimer\nif __name__ == '__main__':\n    timer = QTimer()\n",
    "class QueryBuilder:\n    pass\nthing = QueryBuilder()\n",
])
def test_scanner_ignores_deferred_or_non_qt_calls(source):
    assert not scan_source(source, "probe.py")


@pytest.mark.parametrize("source", [
    "from PySide6.QtCore import QTimer\ntimer = QTimer()\n",
    "from PySide6.QtGui import QColor\nclass Palette:\n    colour = QColor('red')\n",
    "from PySide6.QtCore import QTimer\ndef f(timer=QTimer()):\n    return timer\n",
])
def test_scanner_flags_module_class_and_default_construction(source):
    violations = scan_source(source, "probe.py")
    assert [v.kind for v in violations] == ["Qt object constructed at import time"]

