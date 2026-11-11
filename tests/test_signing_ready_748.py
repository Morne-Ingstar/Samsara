"""Release identity and workflow checks; no app execution or network."""

import ast
from pathlib import Path
import shutil
import sys

import pytest

from scripts.version_info import numeric_version, release_version, write_version_resource
from tools.check_release_version import check_pe_version, read_pe_version


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["Samsara.exe", "samsara-mcp.exe"])
def test_generated_resource_matches_package(tmp_path, filename):
    output = write_version_resource(ROOT, tmp_path / "version.txt", filename)
    tree = ast.parse(output.read_text(encoding="utf-8"))
    strings = {call.args[0].value: call.args[1].value for call in ast.walk(tree)
               if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
               and call.func.id == "StringStruct"}
    version = release_version(ROOT)
    assert strings["ProductName"] == "Samsara"
    assert strings["ProductVersion"] == version
    assert strings["FileVersion"] == ".".join(map(str, numeric_version(version)))
    assert strings["OriginalFilename"] == filename
    assert strings["FileDescription"] == "Samsara voice control and dictation"
    assert strings["CompanyName"] == "Samsara"
    assert "AGPL-3.0" in strings["LegalCopyright"]
    fixed = next(call for call in ast.walk(tree)
                 if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                 and call.func.id == "FixedFileInfo")
    fields = {field.arg: ast.literal_eval(field.value) for field in fixed.keywords}
    assert fields["filevers"] == fields["prodvers"] == numeric_version(version)


@pytest.mark.parametrize("value, expected", [
    ("0.30.0-beta.9", (0, 30, 0, 9)),
    ("1.2.3", (1, 2, 3, 0)), ("1.2.3-rc1", (1, 2, 3, 1)),
])
def test_numeric_windows_version(value, expected):
    assert numeric_version(value) == expected


@pytest.mark.parametrize("value", ["v1.2.3", "1.2", "1.2.3-beta", "65536.2.3", "1.2.3-rc.65536"])
def test_invalid_windows_version_fails_closed(value):
    with pytest.raises(ValueError):
        numeric_version(value)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PE resource APIs")
def test_real_pe_resource_round_trip_without_launch(tmp_path):
    PyInstaller = pytest.importorskip("PyInstaller")
    from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file, write_version_info_to_executable

    bootloader = Path(PyInstaller.__file__).parent / "bootloader" / "Windows-64bit-intel" / "run.exe"
    target = tmp_path / "Samsara.exe"
    shutil.copyfile(bootloader, target)
    resource = write_version_resource(ROOT, tmp_path / "version.txt", target.name)
    write_version_info_to_executable(str(target), load_version_info_from_text_file(str(resource)))
    check_pe_version(target, release_version(ROOT))
    assert read_pe_version(target)["ProductName"] == "Samsara"
    with pytest.raises(ValueError, match="ProductVersion mismatch"):
        check_pe_version(target, "1.2.3")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PE resource APIs")
def test_missing_pe_resource_fails_closed(tmp_path):
    target = tmp_path / "invalid.exe"
    target.write_bytes(b"MZ-invalid")
    with pytest.raises(ValueError, match="missing PE version resource"):
        read_pe_version(target)


def test_both_spec_targets_embed_resource_and_installer_uses_app_identity():
    source = (ROOT / "scripts/samsara.spec").read_text(encoding="utf-8")
    tree = ast.parse(source)
    targets = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Name) and node.func.id == "EXE"]
    assert len(targets) == 2
    assert all(any(keyword.arg == "version" for keyword in node.keywords) for node in targets)
    installer = (ROOT / "installer/samsara.iss").read_text(encoding="utf-8")
    assert 'VersionInfoProductName={#AppName}' in installer
    assert 'VersionInfoProductTextVersion={#AppVersion}' in installer
    assert 'VersionInfoVersion={#GetVersionNumbersString(DistDir + "\\" + AppExeName)}' in installer
    assert not any(line.strip().lower().startswith("signtool=") for line in installer.splitlines())


def test_workflow_checks_pe_metadata_before_smoke_and_packaging():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/release-build.yml").read_text(encoding="utf-8"))
    # 1212: the publisher must select the renamed CPU build workflow exactly.
    publisher = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert workflow["name"] == "Release Build (CPU)"
    assert f"$run.workflowName -ne '{workflow['name']}'" in publisher
    job = workflow["jobs"]["build-cpu"]
    assert job["runs-on"] == "windows-latest"
    steps = job["steps"]
    names = [step["name"] for step in steps]
    index = names.index("Verify built PE version resources")
    assert names.index("Build with PyInstaller (CPU configuration)") < index
    assert index < names.index("Headless smoke check (boot + no unexplained crash)")
    assert index < names.index("Package CPU build")
    assert "--exe dist\\Samsara\\Samsara.exe" in steps[index]["run"]
    assert "--exe dist\\Samsara\\samsara-mcp.exe" in steps[index]["run"]


def test_published_policy_attribution_roles_and_privacy():
    # 1212: the retired policy and public signing promises must stay removed.
    assert not (ROOT / "CODE_SIGNING_POLICY.md").exists()
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "code signing policy" not in readme
    assert "signpath" not in readme


@pytest.mark.parametrize("stage, filenames", [
    ("stage-a", ["Samsara.exe", "samsara-mcp.exe"]),
    ("stage-b", ["SamsaraSetup-*.exe"]),
], ids=["stage-a-filenames0", "stage-b-filenames1"])
def test_artifact_configuration_first_party_allowlist(stage, filenames):
    # 1212: keep the original node IDs as guards against restoring SignPath files.
    assert not (ROOT / "signpath" / f"{stage}.xml").exists()
    assert not (ROOT / "signpath").exists()


def test_signing_stays_inert_until_verified_activation():
    import re
    import yaml

    # 1212: neither release workflow may retain a signing step or credentials.
    for filename in ("release-build.yml", "release.yml"):
        source = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
        assert "signpath" not in source.lower()
        workflow = yaml.safe_load(source)
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                assert not re.search(r"\b(?:sign|signing|signtool|authenticode)\b",
                                     str(step), re.IGNORECASE)
    assert not (ROOT / "scripts/sign.ps1").exists()
