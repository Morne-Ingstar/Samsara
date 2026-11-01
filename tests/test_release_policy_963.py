"""Tag automation prepares drafts; publication and Latest require a decision."""

from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["release.yml", "release-build.yml"])
def test_automated_release_steps_stay_draft_and_never_select_latest(filename):
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    )
    release_steps = []
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            action = step.get("uses", "").lower()
            if action.startswith("softprops/action-gh-release@"):
                release_steps.append(step)
                inputs = step["with"]
                assert inputs.get("draft") is True, step["name"]
                assert inputs.get("make_latest") is False, step["name"]
            else:
                # An alternate release action needs an explicit policy review.
                assert "release" not in action, step.get("name", action)

            # The current workflows use the action exclusively. Prevent a CLI
            # or direct API publication step from bypassing its draft policy.
            script = step.get("run", "")
            assert not re.search(
                r"\bgh\s+(?:release\s+(?:create|edit)|api\b[^\n]*releases)"
                r"|(?:github\.)?(?:rest\.)?repos\.(?:createRelease|updateRelease)"
                r"|(?:api\.github\.com|/repos/)[^\s]*?/releases",
                script,
                re.IGNORECASE,
            ), step.get("name", script)
    assert len(release_steps) == 1


def test_release_instructions_separate_prerelease_from_owner_latest_decision():
    instructions = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    assert "gh release edit <tag> --draft=false --prerelease --latest=false" in instructions
    assert "gh release edit <tag> --prerelease=false --latest" in instructions
    assert "Only on the owner's explicit word" in instructions
