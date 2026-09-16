"""installer/samsara.iss and tools/build_installer.cmd, parsed as text (Inno
Setup is not needed to run these): every component the installer offers
maps to a manifest component id; the GPU component is disabled with its
reason rather than removed; the command model follows the manifest's
`available` flag through a build-time define; the fetch is the app's own
headless mode; the build command versions from the same source as the
build and names the output as required."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import gen_release_manifest as gen

REPO = Path(__file__).resolve().parent.parent
ISS = REPO / "installer" / "samsara.iss"
CMD = REPO / "tools" / "build_installer.cmd"
README = REPO / "installer" / "README.md"
INSTALL_DOC = REPO / "docs" / "INSTALL.md"
WORKFLOW = REPO / ".github" / "workflows" / "release-build.yml"


@pytest.fixture(scope="module")
def iss() -> str:
    return ISS.read_text(encoding="utf-8")


def _section(text: str, name: str) -> str:
    m = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[[A-Za-z]+\]\s*$|\Z)", text, re.M | re.S)
    assert m, f"no [{name}] section"
    return m.group(1)


def _component_names(text: str) -> list:
    return re.findall(r'^Name:\s*"([a-z_]+)"', _section(text, "Components"), re.M)


def _manifest_mapping(text: str) -> dict:
    """The ManifestId() table in [Code]: Inno name -> manifest id."""
    code = _section(text, "Code")
    body = code[code.index("function ManifestId"):]
    body = body[:body.index("end;")]
    return dict(re.findall(r"if Name = '([a-z_]+)' then Result := '([a-z0-9-]+)';", body))


class TestComponentsMatchTheManifest:
    def test_every_installer_component_maps_to_a_manifest_id(self, iss):
        manifest_ids = {c["id"] for c in gen.component_specs("v0.0.0")}
        names = _component_names(iss)
        mapping = _manifest_mapping(iss)
        assert names, "no components declared"
        for name in names:
            assert name in mapping, f"installer component {name!r} has no ManifestId mapping"
            assert mapping[name] in manifest_ids, f"{name!r} maps to {mapping[name]!r}, not a manifest id"
        assert set(mapping.values()) == manifest_ids, "the installer must offer every manifest component"

    def test_component_ids_are_inno_identifiers_and_manifest_ids_are_manifest_ids(self, iss):
        for name, cid in _manifest_mapping(iss).items():
            assert re.fullmatch(r"[a-z][a-z0-9_]*", name)
            assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", cid)

    def test_core_is_fixed_and_in_every_type(self, iss):
        comp = _section(iss, "Components")
        core = [l for l in comp.splitlines() if l.startswith('Name: "core"')][0]
        assert "Flags: fixed" in core and "Types: full minimal custom" in core

    def test_defaults_give_a_working_install_on_enter(self, iss):
        comp = _section(iss, "Components")
        wake = [l for l in comp.splitlines() if 'Name: "wake_word_models"' in l][0]
        assert "Types: full custom" in wake, "wake-word models preselected"
        cmd = [l for l in comp.splitlines() if 'Name: "command_model"' in l][0]
        assert "Types:" not in cmd, "the command model is never preselected"
        assert "Name: \"full\"" in _section(iss, "Types") and "Flags: iscustom" in _section(iss, "Types")

    def test_sizes_come_from_the_manifest_defines(self, iss):
        assert '#include "manifest_defines.iss"' in iss and '#ifexist "manifest_defines.iss"' in iss
        comp = _section(iss, "Components")
        for define in ("WakeWordModelsSize", "CudaPackSize", "CommandModelSize"):
            assert f"ExtraDiskSpaceRequired: {{#{define}}}" in comp
        assert "ShowComponentSizes=yes" in _section(iss, "Setup")


class TestGpuAndComingSoon:
    def test_gpu_component_is_disabled_with_reason_never_hidden(self, iss):
        code = _section(iss, "Code")
        assert "cuda_pack" in _component_names(iss)
        assert "No NVIDIA GPU detected" in code
        assert "ItemEnabled[Idx] := False" in code
        # never conditionally compiled out: no preprocessor branch in
        # [Components], and no #if on the CUDA size define beyond its default
        assert "#if" not in _section(iss, "Components")
        assert "#if CudaPack" not in iss
        # detection failure keeps it selectable with a neutral note
        assert "Could not detect a GPU" in code
        assert "Result := -1" in code and "except" in code
        # both detection routes exist
        assert "nvcuda.dll" in code and "Win32_VideoController" in code

    def test_gpu_component_is_not_in_the_full_type_when_detection_says_no(self, iss):
        code = _section(iss, "Code")
        assert "GpuDetected = 0" in code and "Checked[Idx] := False" in code

    def test_command_model_follows_the_manifest_available_flag(self, iss):
        assert "#ifndef CommandModelAvailable" in iss and "#define CommandModelAvailable 0" in iss
        code = _section(iss, "Code")
        assert "#if CommandModelAvailable == 0" in code and "Coming soon" in code
        assert "#else" in code and "ItemEnabled[Idx] := True" in code

    def test_build_cmd_generates_the_availability_define_from_the_manifest(self):
        cmd = CMD.read_text(encoding="utf-8")
        assert "manifest_defines.iss" in cmd
        assert "Available" in cmd and "'command-model': 'CommandModel'" in cmd
        assert "'cuda-pack': 'CudaPack'" in cmd and "'wake-word-models': 'WakeWordModels'" in cmd


class TestInstallBehaviour:
    def test_per_user_by_default_admin_optional(self, iss):
        setup = _section(iss, "Setup")
        assert "PrivilegesRequired=lowest" in setup
        assert "PrivilegesRequiredOverridesAllowed=dialog" in setup
        assert "DefaultDirName={autopf}" in setup

    def test_fetch_is_the_apps_headless_mode_and_never_fails_the_install(self, iss):
        code = _section(iss, "Code")
        assert "--fetch-components" in code and "--manifest" in code and "--progress-window" in code
        assert "--components-dir" in code and "{param:ComponentsDir|}" in code
        assert "{param:NoFetch|0}" in code
        assert "ewWaitUntilTerminated" in code
        # exit codes shape the Finished page: cancelled (5) and no network (4)
        assert "FetchOutcome = 5" in code and "FetchOutcome = 4" in code
        assert "cancelled" in code.lower() and "no network" in code.lower()
        assert "Samsara itself is installed and works" in code

    def test_no_big_component_is_bundled(self, iss):
        files = _section(iss, "Files")
        assert files.count("Source:") == 1 and "Components: core" in files

    def test_version_and_output_name(self, iss):
        assert "#ifndef AppVersion" in iss and "#error AppVersion is required" in iss
        assert "OutputBaseFilename=SamsaraSetup-{#AppVersion}" in _section(iss, "Setup")

    def test_keyboard_and_scaling_notes_are_in_the_script(self, iss):
        head = iss[: iss.index("[Setup]")]
        assert "keyboard" in head.lower() and "150%" in head
        assert "WizardResizable=yes" in iss and "WizardSizePercent=120" in iss


class TestBuildCommand:
    def test_versions_from_the_same_source_as_the_build(self):
        cmd = CMD.read_text(encoding="utf-8")
        assert "tools.check_release_version import _string_assignment" in cmd
        assert "samsara/__init__.py" in cmd and "__version__" in cmd
        assert "/DAppVersion=%APP_VERSION%" in cmd
        assert "dist\\SamsaraSetup-%APP_VERSION%.exe" in cmd

    def test_requires_a_built_dist_and_inno_setup(self):
        cmd = CMD.read_text(encoding="utf-8")
        assert 'if not exist "dist\\Samsara\\Samsara.exe"' in cmd
        assert "Inno Setup 6\\ISCC.exe" in cmd and "SAMSARA_ISCC" in cmd
        assert 'installer\\samsara.iss' in cmd

    def test_ci_has_one_installer_step_and_uploads_the_exe(self):
        import yaml

        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = doc["jobs"]["build-cpu"]["steps"]
        installer_steps = [s for s in steps if "inno" in s.get("name", "").lower()]
        assert len(installer_steps) == 1
        assert installer_steps[0]["run"].strip() == "tools\\build_installer.cmd manifest.json"
        uploads = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")]
        assert any("SamsaraSetup-*.exe" in str(s["with"]["path"]) for s in uploads)
        release = [s for s in steps if s.get("name") == "Create draft GitHub Release"][0]
        assert "SamsaraSetup-*.exe" in release["with"]["files"]
        # the trigger structure of 10a is untouched
        on = doc[True] if True in doc else doc["on"]
        assert set(on) == {"push", "workflow_dispatch"} and on["push"]["tags"] == ["v*.*.*"]

    def test_docs_explain_both_routes_and_signing(self):
        install = INSTALL_DOC.read_text(encoding="utf-8")
        assert "/ComponentsDir=" in install and "Samsara-Windows-<version>.zip" in install
        assert "SmartScreen" in install
        readme = README.read_text(encoding="utf-8")
        assert "SignPath" in readme and "--fetch-components" in readme
        # 108: the dispatch is real now, so the README documents where it
        # LIVES rather than a snippet someone still has to paste in. Both
        # placement facts are asserted because both are load-bearing.
        assert "_dispatch_startup_argument" in readme, "the dictation.py dispatch is documented"
        assert "single-instance lock" in readme and "heavy import" in readme
