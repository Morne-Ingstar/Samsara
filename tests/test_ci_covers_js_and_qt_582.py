"""Keep both browser-extension suites and Qt discipline in each CI gate."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("workflow", "job"),
    [("ci.yml", "test"), ("release.yml", "publish-cpu")],
)
def test_workflow_runs_both_js_suites_and_offscreen_qt_check(workflow, job):
    doc = yaml.safe_load(
        (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    )
    steps = doc["jobs"][job]["steps"]
    node_setup = next(
        step for step in steps
        if step.get("uses", "").startswith("actions/setup-node@")
    )
    assert str(node_setup["with"]["node-version"]) == "22"

    js_step = next(
        step for step in steps
        if step.get("run", "").split() == [
            "node", "--test",
            "browser_extension/content-core.test.js",
            "browser_extension/bridge-auth-core.test.js",
        ]
    )
    assert steps.index(node_setup) < steps.index(js_step)

    qt_step = next(
        step for step in steps
        if step.get("run", "").strip() == "python tools/check_qt_discipline.py"
    )
    assert qt_step["env"]["QT_QPA_PLATFORM"] == "offscreen"
    for step in (js_step, qt_step):
        assert step["shell"] == "pwsh"
        assert not step.get("continue-on-error", False)
        assert "if" not in step
