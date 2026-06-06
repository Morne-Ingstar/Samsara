"""Enforcement: assert that QApplication creation and exec() are confined to qt_runtime.

Run:
    python tools/check_qt_discipline.py        # exits 0 if clean, 1 if violations

Rules
-----
FORBIDDEN outside samsara/ui/qt_runtime.py:
  - QApplication([               e.g.  QApplication([])   — app creation
  - qt_app.exec()                      e.g.  qt_app.exec()   — top-level loop

ALLOWED everywhere:
  - dlg.exec()  /  dialog.exec()  /  QDialog.exec()  — modal dialog exec
  - QApplication.instance()                           — querying existing app
  - QApplication.clipboard()  / .screens() / etc.    — static utility methods
  - QApplication.primaryScreen() / .screens()         — read-only

The check distinguishes by looking for the literal tokens  QApplication([
and the pattern  <var>.exec()  where var does NOT look like a dialog
(heuristic: variable names containing 'dlg', 'dialog', 'wiz', 'menu').
"""

import re
import sys
from pathlib import Path

ROOT       = Path(__file__).parents[1]
UI_DIR     = ROOT / "samsara" / "ui"
RUNTIME    = UI_DIR / "qt_runtime.py"

# Patterns that are FORBIDDEN outside qt_runtime.py
_FORBIDDEN = [
    # Creating a QApplication
    (re.compile(r'QApplication\(\['), "QApplication([…]) — app creation"),
    # Top-level exec():  <identifier>.exec()  where identifier looks like an app var
    # Heuristic: match  qt_app.exec() / app.exec() / <anything without 'dlg'/'dialog'>
    (
        re.compile(r'\bqt_app\.exec\(\)|\bapp\.exec\(\)'),
        "app.exec() — top-level event loop",
    ),
]

# Patterns that indicate a line is a FALSE POSITIVE (allowed dialog exec)
_ALLOWED_EXEC = re.compile(
    r'\b(dlg|dialog|wiz|wizard|menu|combo)\b.*\.exec\(\)',
    re.IGNORECASE,
)

violations = []

for py_file in sorted(UI_DIR.rglob("*.py")):
    if py_file.resolve() == RUNTIME.resolve():
        continue  # qt_runtime.py owns these patterns
    rel = py_file.relative_to(ROOT)
    try:
        lines = py_file.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        print(f"WARNING: could not read {rel}: {exc}", file=sys.stderr)
        continue

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # skip pure comment lines
        for pattern, description in _FORBIDDEN:
            if pattern.search(line):
                if ".exec()" in line and _ALLOWED_EXEC.search(line):
                    continue  # modal dialog — allowed
                violations.append((rel, lineno, stripped, description))

if violations:
    print(f"Qt discipline check FAILED — {len(violations)} violation(s):\n")
    for rel, lineno, src, desc in violations:
        print(f"  {rel}:{lineno}  [{desc}]")
        print(f"    {src}")
    sys.exit(1)

print(f"Qt discipline check PASSED — no violations in {UI_DIR.relative_to(ROOT)}")
sys.exit(0)
