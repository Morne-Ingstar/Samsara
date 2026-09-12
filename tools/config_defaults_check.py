"""Enforcement: no config key may be read with two different literal
defaults, and no literal default may disagree with samsara.config_defaults.

Run:
    python tools/config_defaults_check.py     # exits 0 if clean, 1 if violations

Background
----------
docs/reviews/build_and_config_audit.md Part B3c found 12 config keys read
with conflicting literal defaults across the codebase (e.g.
`wake_word_config.phrase` defaulting to 'jarvis' in one file and 'samsara'
in another -- a fresh install would get a different wake word depending on
which code path ran first). Those were fixed by routing every read through
samsara.config_defaults.DEFAULTS. This script is the regression gate: it
statically finds every `<expr>.get('key', <literal>)` call in the repo,
resolves `<expr>` back to a dotted config path where possible (the same
alias-following samsara/config_schema.py-adjacent code does by hand), and
fails if:

  1. a key that IS in config_defaults.DEFAULTS is ever read with a literal
     default that disagrees with the table, or
  2. a key that is NOT in the table is read with two or more different
     literal defaults (this catches the *next* drifting key before it
     needs its own audit).

Two deliberate limitations, same shape as the audit's own scanner:

  - `<expr>` can only be traced back to a dotted path through a simple,
    single-file, best-effort alias chain (`x = <root>.get('section', {})`
    then `x.get('leaf', default)`, including inline chaining). Reads whose
    base object cannot be traced to `config`/`self.config`/`self._config`/
    `app.config`/`app._config` are skipped ("unattributed"), exactly like
    the audit's manually-verified unattributed bucket -- a bare `.get()` on
    an unrelated dict (a profile, an event, a settings-delta dict) must not
    be treated as a config-key conflict just because it happens to share a
    leaf name like "phrase" or "mode".
  - Non-literal defaults (a variable or call, e.g. `.get('x', SOME_CONST)`)
    are excluded rather than guessed at, same as the audit's `<expr>` rule.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIRS = {
    "tests", "dist", "build", ".git", "__pycache__", "perf_artifacts",
    "node_modules", ".venv", "venv",
    # tools/ scripts read their OWN standalone config dicts (benchmark
    # harnesses, offline probes) that happen to reuse short key names like
    # "device"/"compute_type" for an unrelated domain (whisper decode
    # tuning, not the live app config) -- the audit excluded tools/ from
    # its B3c conflicting-defaults count for the same reason.
    "tools",
}

# The table itself (and this checker) define/reference defaults on purpose;
# scanning them would just report the table as conflicting with itself.
SELF_EXEMPT = {
    ROOT / "samsara" / "config_defaults.py",
    ROOT / "samsara" / "config_schema.py",
    ROOT / "tools" / "config_defaults_check.py",
}

_LITERAL_SCALARS = (str, int, float, bool, type(None))

# Root expressions that mean "the live config dict" -- self.config,
# self._config, app.config, app._config, and a bare top-level `config` name
# (common in plugins/ and tools/ where config is a function parameter).
_ROOT_ATTRS = {("self", "config"), ("self", "_config"), ("app", "config"), ("app", "_config")}


def literal_value(node):
    """Return (True, value) if `node` is a literal this checker understands,
    else (False, None). Mirrors the audit's `<expr>` exclusion for anything
    else (names, calls, f-strings, ...)."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, _LITERAL_SCALARS):
            return True, node.value
        return False, None
    if isinstance(node, (ast.List, ast.Tuple)):
        values = []
        for elt in node.elts:
            ok, val = literal_value(elt)
            if not ok:
                return False, None
            values.append(val)
        return True, values
    if isinstance(node, ast.Dict) and not node.keys:
        return True, {}
    return False, None


def _subscript_key(node):
    sl = node.slice
    if isinstance(sl, ast.Index):  # py<3.9 compat
        sl = sl.value
    return literal_value(sl)


def resolve_path(node, alias_map):
    """Best-effort: resolve `node` to a dotted config path ("" for the
    live config root itself), or None if it can't be attributed."""
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and (node.value.id, node.attr) in _ROOT_ATTRS:
            return ""
        return None
    if isinstance(node, ast.Name):
        if node.id == "config":
            return ""
        return alias_map.get(node.id)
    if isinstance(node, ast.Subscript):
        base = resolve_path(node.value, alias_map)
        if base is None:
            return None
        ok, key = _subscript_key(node)
        if ok and isinstance(key, str):
            return f"{base}.{key}" if base else key
        return None
    if isinstance(node, ast.Call):
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and len(node.args) >= 1):
            base = resolve_path(node.func.value, alias_map)
            if base is None:
                return None
            ok, key = literal_value(node.args[0])
            if ok and isinstance(key, str):
                return f"{base}.{key}" if base else key
        return None
    return None


def _direct_children_no_defs(node):
    """Like ast.iter_child_nodes, but a scope boundary (function/class) is
    yielded without descending into it -- callers walk each scope (module
    top-level, then every function) independently so a local alias named
    `cfg` in one method can't leak its meaning into another."""
    for child in ast.iter_child_nodes(node):
        yield child


def _walk_scope(node):
    """All descendants of `node` EXCLUDING the bodies of nested
    function/class defs (those are separate scopes, walked on their own)."""
    stack = list(_direct_children_no_defs(node))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(_direct_children_no_defs(n))


def _get_calls_in_scope(scope_node):
    """Fixed-point alias resolution + every literal-default `.get()` call
    found directly in this scope (not in a nested function)."""
    alias_map = {}
    for _ in range(6):
        changed = False
        for n in _walk_scope(scope_node):
            if (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)):
                name = n.targets[0].id
                if name in alias_map:
                    continue
                path = resolve_path(n.value, alias_map)
                if path is not None:
                    alias_map[name] = path
                    changed = True
        if not changed:
            break

    sites = []
    for n in _walk_scope(scope_node):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and len(n.args) == 2):
            continue
        base = resolve_path(n.func.value, alias_map)
        if base is None:
            continue
        ok_key, key = literal_value(n.args[0])
        if not (ok_key and isinstance(key, str)):
            continue
        ok_def, default = literal_value(n.args[1])
        if not ok_def:
            continue
        dotted = f"{base}.{key}" if base else key
        sites.append((dotted, default, n.lineno))
    return sites


def collect_sites(py_file: Path):
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"WARNING: could not parse {py_file}: {exc}", file=sys.stderr)
        return []

    sites = []
    scopes = [tree]
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(n)
    for scope in scopes:
        for dotted, default, lineno in _get_calls_in_scope(scope):
            sites.append((dotted, default, py_file, lineno))
    return sites


def iter_python_files():
    for path in sorted(ROOT.rglob("*.py")):
        if path in SELF_EXEMPT:
            continue
        rel_parts = path.relative_to(ROOT).parts
        if any(part in EXCLUDE_DIRS for part in rel_parts):
            continue
        yield path


def find_violations():
    from samsara.config_defaults import DEFAULTS

    all_sites: dict[str, list] = {}
    for py_file in iter_python_files():
        for dotted, default, path, lineno in collect_sites(py_file):
            all_sites.setdefault(dotted, []).append((default, path, lineno))

    violations = []
    for key, sites in all_sites.items():
        distinct = {}
        for default, path, lineno in sites:
            distinct.setdefault(repr(default), (default, []))[1].append((path, lineno))

        if key in DEFAULTS:
            table_default = DEFAULTS[key]
            for default, path, lineno in sites:
                if default != table_default:
                    violations.append(
                        f"{key}: {path.relative_to(ROOT)}:{lineno} uses default "
                        f"{default!r}, disagrees with config_defaults.DEFAULTS[{key!r}] "
                        f"= {table_default!r}"
                    )
        elif len(distinct) > 1:
            detail = "; ".join(
                f"{default!r} at " + ", ".join(f"{p.relative_to(ROOT)}:{l}" for p, l in locs)
                for default, locs in distinct.values()
            )
            violations.append(
                f"{key}: read with {len(distinct)} different literal defaults "
                f"and not yet in config_defaults.DEFAULTS -- {detail}"
            )

    return sorted(violations)


def main():
    violations = find_violations()
    if violations:
        print(f"config defaults check FAILED -- {len(violations)} violation(s):\n")
        for v in violations:
            print(f"  {v}")
        return 1
    print("config defaults check PASSED -- no conflicting or stale literal defaults")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
