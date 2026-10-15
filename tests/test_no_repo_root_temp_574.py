"""Keep test/tool scratch directories outside the repository (also in exports)."""

import ast
from pathlib import Path, PureWindowsPath
import re


SOURCE_DIRS = ("tests", "tools", "scripts", "perf_artifacts", ".github")
SOURCE_SUFFIXES = {".py", ".ps1", ".sh", ".bat", ".cmd", ".ini", ".yml", ".yaml", ".toml", ".spec"}
BASE_TEMP = re.compile(r"--basetemp(?:=|\s+|[\"']\s*,\s*[\"'])[\"']?([^\s\"',;\]\)]+)")
LEAK_PATH = re.compile(
    r"\bPath\(\s*[\"'](?:\./)?(?:\.pytest_tmp[^\"']*|\.release_tmp|"
    r"tmp_samsara_home|\.hf_bench_stage_home)[\"']\s*\)"
)
ROOT_EXPR = re.compile(r"\b(?:ROOT|REPO_ROOT|PROJECT_ROOT|root|repo_root|project_root)\b|\bcwd\(")


def _relative_literal(value):
    # Variables and substitutions are resolved by the caller/gate, not repo paths.
    return (not value.startswith(("$", "%", "{", "/", "~"))
            and not PureWindowsPath(value).is_absolute())


def _violations(text, python=False):
    findings = []
    for match in BASE_TEMP.finditer(text):
        if _relative_literal(match[1]):
            findings.append((text.count("\n", 0, match.start()) + 1, "relative --basetemp"))
    for match in LEAK_PATH.finditer(text):
        findings.append((text.count("\n", 0, match.start()) + 1, "repo scratch Path"))
    if python:
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name != "mkdtemp":
                continue
            directories = [kw.value for kw in node.keywords if kw.arg == "dir"]
            directories.extend(node.args[2:3])  # suffix, prefix, dir
            for directory in directories:
                if isinstance(directory, ast.Constant):
                    unsafe = isinstance(directory.value, str) and _relative_literal(directory.value)
                else:
                    unsafe = bool(ROOT_EXPR.search(ast.get_source_segment(text, directory) or ""))
                    if isinstance(directory, ast.Call) and directory.args:
                        value = directory.args[0]
                        unsafe |= (isinstance(value, ast.Constant)
                                   and isinstance(value.value, str)
                                   and _relative_literal(value.value))
                if unsafe:
                    findings.append((node.lineno, "repo mkdtemp directory"))
    return findings


def test_no_repo_root_temp():
    # Exercise the guard without creating any directories or invoking Git.
    for source in (
        'pytest --basetemp=.pytest_tmp_gate',
        'pytest --basetemp "./scratch"',
        '["--basetemp", ".pytest_tmp_gate"]',
        'tempfile.mkdtemp(dir=ROOT)',
        'mkdtemp(dir=".")',
        'tempfile.mkdtemp(dir=Path("scratch"))',
        'Path(".pytest_tmp_gate")',
    ):
        assert _violations(source, python=not source.startswith("pytest")), source
    for source in (
        'pytest --basetemp=/tmp/gate',
        r'pytest --basetemp="C:\Temp\gate"',
        'pytest --basetemp="$TEMP/gate"',
        'tempfile.mkdtemp()',
        'tempfile.mkdtemp(dir=tempfile.gettempdir())',
        'tempfile.mkdtemp(dir=tmp_path)',
        '["--basetemp", str(tmp_path)]',
    ):
        assert not _violations(source, python=not source.startswith("pytest")), source

    root = Path(__file__).resolve().parents[1]
    paths = [root / "pytest.ini"]
    for folder in SOURCE_DIRS:
        paths.extend(path for path in (root / folder).rglob("*")
                     if path.is_file() and path.suffix in SOURCE_SUFFIXES
                     and "__pycache__" not in path.parts)
    errors = []
    for path in sorted(paths):
        if path.resolve() == Path(__file__).resolve():
            continue  # The guard's deliberate unsafe examples are not creators.
        text = path.read_text(encoding="utf-8-sig")
        for line, reason in _violations(text, python=path.suffix == ".py"):
            errors.append(f"{path.relative_to(root)}:{line}: {reason}")
    assert not errors, "Use tmp_path, OS temp, or the gate temp:\n" + "\n".join(errors)
