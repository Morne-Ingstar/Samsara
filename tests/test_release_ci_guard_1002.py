"""Tag builds must not upload CI binaries over locally signed release assets."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name):
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 resolver treats the key `on` as boolean True.
    triggers = workflow.get("on", workflow.get(True, {}))
    return workflow, triggers


def _release_steps(workflow):
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses", "").lower().startswith("softprops/action-gh-release@")
    ]


def test_tag_build_only_uploads_artifacts_and_manual_release_upload_is_guarded():
    build, build_triggers = _load("release-build.yml")
    release, release_triggers = _load("release.yml")

    assert "tags" in build_triggers["push"]
    assert "workflow_run" not in release_triggers
    assert "workflow_dispatch" in release_triggers

    build_dispatch = build_triggers["workflow_dispatch"]["inputs"]
    release_dispatch = release_triggers["workflow_dispatch"]["inputs"]
    for inputs in (build_dispatch, release_dispatch):
        assert inputs["upload_to_release"]["type"] == "boolean"
        assert inputs["upload_to_release"]["default"] is False

    build_steps = build["jobs"]["build-cpu"]["steps"]
    build_uploads = _release_steps(build)
    release_jobs = list(release["jobs"].values())
    release_uploads = _release_steps(release)
    assert len(build_uploads) == len(release_uploads) == 1
    assert "workflow_dispatch" in build_uploads[0]["if"]
    assert "inputs.upload_to_release" in build_uploads[0]["if"]
    assert "workflow_dispatch" in release_jobs[0]["if"]
    assert "inputs.upload_to_release" in release_jobs[0]["if"]

    for steps in (build_steps, release_jobs[0]["steps"]):
        names = [step.get("name", "") for step in steps]
        guard_index = names.index("Refuse release upload when an installer already exists")
        upload_index = next(
            i for i, step in enumerate(steps)
            if step.get("uses", "").lower().startswith("softprops/action-gh-release@")
        )
        assert guard_index < upload_index
        guard = steps[guard_index]
        assert "SamsaraSetup-*.exe" in guard["run"]
        assert "throw" in guard["run"]
        assert steps[upload_index]["with"]["overwrite_files"] is False


def test_release_workflows_have_no_asset_clobber_or_overwrite_option():
    for name in ("release-build.yml", "release.yml"):
        source = (WORKFLOWS / name).read_text(encoding="utf-8").lower()
        assert "--clobber" not in source
        assert "overwrite_files: true" not in source

