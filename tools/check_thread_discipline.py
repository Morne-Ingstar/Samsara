"""Enforcement: assert that raw threading.Thread(...)/threading.Timer(...)
construction is confined to samsara/runtime/thread_registry.py.

Run:
    python tools/check_thread_discipline.py        # exits 0 if clean, 1 if violations

Rule
----
FORBIDDEN everywhere in the production source tree (samsara/, plugins/,
dictation.py, tools/) except samsara/runtime/thread_registry.py:
  - threading.Thread(          e.g.  threading.Thread(target=...)
  - threading.Timer(           e.g.  threading.Timer(delay, fn)

Threads must be created via samsara.runtime.thread_registry.spawn(), and
one-shot delayed calls via thread_registry.timer(). Where the construction
itself isn't ours to change (subclassed Thread, deferred/conditional start),
construct as before and immediately hand it to thread_registry.register().

tests/ and dist/ are out of scope: tests legitimately construct raw threads
as test infrastructure (concurrency stress tests, harnesses), and dist/ is
gitignored build output, not source.

tools/ scope rule (37): a tools/ module is in scope only when the app
imports it in-process (a `from tools.x import ...` / `import tools.x`
anywhere under samsara/, plugins/, dictation.py or scripts/*.spec -- e.g.
tools/dump_command_metadata.py, tools/release_preflight.py). Everything
else under tools/ is a standalone script that runs in its own process and
exits: registry membership there is meaningless (nothing joins it at
shutdown, nothing dumps it), and forcing it would make boot profilers and
probes import the app's package and its log configuration into processes
that must stay isolated. The set is computed from the import graph on
every run, so a tools/ module that starts being imported in-process is
checked automatically.

Genuine, reviewed exceptions (dead code, standalone throwaway harnesses,
explicitly out-of-scope subprocess management, etc.) are listed in
tools/thread_discipline_allow.txt, one "relative/path.py:lineno" per line.
"""

import re
import sys
from pathlib import Path

ROOT      = Path(__file__).parents[1]
REGISTRY  = ROOT / "samsara" / "runtime" / "thread_registry.py"
CHECKER   = ROOT / "tools" / "check_thread_discipline.py"
ALLOWLIST = ROOT / "tools" / "thread_discipline_allow.txt"

SCAN_ROOTS = [
    ROOT / "samsara",
    ROOT / "plugins",
    ROOT / "dictation.py",
]
# Where an in-process import of a tools/ module can appear (see the tools/
# scope rule in the module docstring).
IMPORTER_ROOTS = [
    ROOT / "samsara",
    ROOT / "plugins",
    ROOT / "dictation.py",
    ROOT / "scripts",
]
TOOLS = ROOT / "tools"

_PATTERNS = ("threading.Thread(", "threading.Timer(")
_TOOLS_IMPORT = re.compile(r"^\s*(?:from\s+tools\.([A-Za-z0-9_.]+)\s+import|import\s+tools\.([A-Za-z0-9_.]+))", re.M)


def _in_process_tools_files() -> list[Path]:
    """tools/ files the app imports in-process (the tools/ scope rule)."""
    modules: set[str] = set()
    for root in IMPORTER_ROOTS:
        files = [root] if root.is_file() else sorted(root.rglob("*.py")) + sorted(root.rglob("*.spec"))
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            for m in _TOOLS_IMPORT.finditer(text):
                modules.add(m.group(1) or m.group(2))
    found: set[Path] = set()
    for dotted in sorted(modules):
        parts = dotted.split(".")
        for depth in range(1, len(parts) + 1):
            rel = Path(*parts[:depth])
            for candidate in (TOOLS / rel.with_suffix(".py"), TOOLS / rel / "__init__.py"):
                if candidate.is_file():
                    found.add(candidate)
    return sorted(found)


def _load_allowlist() -> set[str]:
    if not ALLOWLIST.exists():
        return set()
    allowed = set()
    for line in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        allowed.add(line)
    return allowed


def _iter_py_files():
    for root in SCAN_ROOTS:
        if root.is_file():
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.py"))
    yield from _in_process_tools_files()


allowed = _load_allowlist()
violations = []

for py_file in _iter_py_files():
    if py_file.resolve() == REGISTRY.resolve():
        continue  # thread_registry.py owns this pattern
    if py_file.resolve() == CHECKER.resolve():
        continue  # this file's own docstring/pattern string mention the target text

    rel = py_file.relative_to(ROOT).as_posix()
    try:
        lines = py_file.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        print(f"WARNING: could not read {rel}: {exc}", file=sys.stderr)
        continue

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if any(pattern in line for pattern in _PATTERNS):
            key = f"{rel}:{lineno}"
            if key in allowed:
                continue
            violations.append((rel, lineno, stripped))

if violations:
    print(f"Thread discipline check FAILED — {len(violations)} violation(s):\n")
    for rel, lineno, src in violations:
        print(f"  {rel}:{lineno}")
        print(f"    {src}")
    sys.exit(1)

print("Thread discipline check PASSED — no unregistered threading.Thread(/threading.Timer( sites")
sys.exit(0)
