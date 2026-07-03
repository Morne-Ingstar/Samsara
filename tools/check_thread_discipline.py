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

Genuine, reviewed exceptions (dead code, standalone throwaway harnesses,
explicitly out-of-scope subprocess management, etc.) are listed in
tools/thread_discipline_allow.txt, one "relative/path.py:lineno" per line.
"""

import sys
from pathlib import Path

ROOT      = Path(__file__).parents[1]
REGISTRY  = ROOT / "samsara" / "runtime" / "thread_registry.py"
CHECKER   = ROOT / "tools" / "check_thread_discipline.py"
ALLOWLIST = ROOT / "tools" / "thread_discipline_allow.txt"

SCAN_ROOTS = [
    ROOT / "samsara",
    ROOT / "plugins",
    ROOT / "tools",
    ROOT / "dictation.py",
]

_PATTERNS = ("threading.Thread(", "threading.Timer(")


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
