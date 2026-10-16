"""Exercise the workflow's range selector without Git, processes, or a profile."""
import ast
import json
from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40
BEFORE = "b" * 40
FIRST = "c" * 40


def selector():
    workflow = yaml.safe_load((ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["gitleaks"]["steps"] if step.get("id") == "range")
    script = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    node = next(node for node in ast.parse(script).body
                if isinstance(node, ast.FunctionDef) and node.name == "select_log_opts")
    namespace = {"re": re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<workflow selector>", "exec"), namespace)
    return namespace["select_log_opts"]


def select(event="push", before=BEFORE, head=HEAD, pr_base=BEFORE, pr_head=HEAD,
           pushed=None, available=(BEFORE, HEAD, FIRST)):
    return selector()(event, before, head, pr_base, pr_head,
                      [] if pushed is None else pushed, available.__contains__)


def test_normal_push_bounds_all_incoming_commits():
    assert select() == f"{BEFORE}..{HEAD}"


def test_pull_request_uses_the_proposed_head():
    assert select(event="pull_request", head=FIRST) == f"{BEFORE}..{HEAD}"


@pytest.mark.parametrize("before", ["", "0" * 40, BEFORE])
def test_missing_previous_tip_scans_every_pushed_commit_and_head(before):
    assert select(before=before, available=(FIRST, HEAD), pushed=[FIRST, HEAD]) == (
        f"--no-walk {FIRST} {HEAD}"
    )


def test_new_branch_with_no_new_commit_still_scans_tip():
    assert select(before="0" * 40, available=(HEAD,)) == f"--no-walk {HEAD}"


def test_fallback_deduplicates_commits_without_discarding_earlier_changes():
    assert select(before="", pushed=[FIRST, FIRST, HEAD]) == f"--no-walk {FIRST} {HEAD}"


@pytest.mark.parametrize("kwargs", [
    {"event": "schedule"},
    {"event": "pull_request", "available": (HEAD,)},
    {"head": "--all"},
    {"head": "d" * 40},
    {"before": "", "pushed": ["--all"]},
    {"before": "", "pushed": ["d" * 40]},
    {"before": "", "pushed": [HEAD] * 2048},
    {"before": "", "pushed": "invalid"},
])
def test_unbounded_or_unavailable_commits_fail_closed(kwargs):
    with pytest.raises(ValueError):
        select(**kwargs)


@pytest.mark.parametrize("path", [
    ".github/workflows/security.yml",
    "tests/test_secret_guard_581.py",
    "tests/test_ci_green_597.py",
])
def test_guard_sources_do_not_trigger_custom_rules(path):
    from tests.test_secret_guard_581 import RULES, _findings
    source = (ROOT / path).read_text(encoding="utf-8")
    assert not any(_findings(rule, path, source) for rule in RULES.values())


def test_synthetic_new_printer_credential_is_not_allowlisted():
    # This verifies the configured custom rule only; scanner integration must
    # also be run where the pinned Gitleaks binary and Git writes are allowed.
    from tests.test_secret_guard_581 import RULES, _findings
    rule = RULES["printer-device-credential"]
    value = "SYNTHETIC" + "597" * 4
    content = json.dumps({"flashforge_check_code": value})
    match = re.search(rule["regex"], content)
    assert match.group(rule["secretGroup"]) == value
    assert not any(re.search(pattern, value) for item in rule["allowlists"] for pattern in item["regexes"])
    assert _findings(rule, "plugins/commands/synthetic_probe.py", content) == [value]
