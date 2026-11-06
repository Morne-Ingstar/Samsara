"""installer/samsara.iss and tools/build_installer.cmd, parsed as text (Inno
Setup is not needed to run these): Setup copies the bundled app with no
optional component selection or downloads (1016); the build command versions
from the same source as the build and names the output as required."""

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
SPEC = REPO / "scripts" / "samsara.spec"


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
        # 1016: the installer now copies the app without optional selection/fetch.
        assert "[Components]" not in iss and "[Types]" not in iss

    def test_component_ids_are_inno_identifiers_and_manifest_ids_are_manifest_ids(self, iss):
        # 1016: the installer now copies the app without optional selection/fetch.
        assert "ManifestId" not in _section(iss, "Code")

    def test_core_is_fixed_and_in_every_type(self, iss):
        # 1016: the installer now copies the app without optional selection/fetch.
        assert "Components:" not in _section(iss, "Files")

    def test_defaults_give_a_working_install_on_enter(self, iss):
        # 1016: the installer now copies the app without optional selection/fetch.
        assert "Source:" in _section(iss, "Files")
        assert "[Components]" not in iss

    def test_sizes_come_from_the_manifest_defines(self, iss):
        # 1016: the installer now copies the app without optional selection/fetch.
        assert "ExtraDiskSpaceRequired" not in iss
        assert "ShowComponentSizes" not in _section(iss, "Setup")


class TestGpuAndComingSoon:
    def test_gpu_component_is_disabled_with_reason_never_hidden(self, iss):
        # 1016: the installer now copies the app without optional selection/fetch.
        assert "cuda_pack" not in iss
        assert "ComponentsList" not in _section(iss, "Code")

    def test_gpu_component_is_not_in_the_full_type_when_detection_says_no(self, iss):
        # 1016: the installer now copies the app without optional selection/fetch.
        assert "[Types]" not in iss and "CanUseCuda" not in iss

    def test_command_model_follows_the_manifest_available_flag(self, iss):
        # 1016: the installer now copies the app without optional selection/fetch.
        assert "command_model" not in iss and "CommandModelAvailable" not in iss

    def test_build_cmd_generates_the_availability_define_from_the_manifest(self):
        cmd = CMD.read_text(encoding="utf-8")
        # The manifest step moved out of the .cmd into a script: cmd cannot parse it inline (v0.30.0-beta.3 release build).
        assert "write_manifest_defines.py" in cmd
        cmd = (CMD.parent / "write_manifest_defines.py").read_text(encoding="utf-8")
        assert "manifest_defines.iss" in cmd
        assert "Available" in cmd and "'command-model': 'CommandModel'" in cmd
        assert "'cuda-pack': 'CudaPack'" in cmd and "'wake-word-models': 'WakeWordModels'" in cmd
        assert "'gesture-control': 'GestureControl'" in cmd


class TestInstallBehaviour:
    def test_uninstall_fresh_start_is_explicit_and_silent_safe(self, iss):
        # 994c: preserve data by default, including unattended uninstall.
        choose = iss.split("procedure ChooseUninstallData();", 1)[1].split(
            "function DataPathAttributes", 1
        )[0]
        assert "Wipe.Checked := False;" in choose
        assert "CompareText(ParamStr(I), '/WIPEDATA') = 0" in choose
        assert choose.index("if UninstallWipeData or UninstallSilent() then exit;") < (
            choose.index("CreateCustomForm")
        )
        assert "Leave unticked to keep your settings, history and downloaded speech models" in choose

    def test_per_user_by_default_admin_optional(self, iss):
        setup = _section(iss, "Setup")
        assert "PrivilegesRequired=lowest" in setup
        assert "PrivilegesRequiredOverridesAllowed=dialog" in setup
        assert "DefaultDirName={autopf}" in setup

    def test_fetch_is_the_apps_headless_mode_and_never_fails_the_install(self, iss):
        # 1016: the installer now copies the app without optional selection/fetch.
        assert "--fetch-components" not in _section(iss, "Code")
        assert "Optional components can be added any time from Settings." in iss

    def test_no_big_component_is_bundled(self, iss):
        # 1016: the installer now copies the app without optional selection/fetch.
        files = _section(iss, "Files")
        assert files.count("Source:") == 1 and "Components:" not in files

    def test_version_and_output_name(self, iss):
        assert "#ifndef AppVersion" in iss and "#error AppVersion is required" in iss
        assert "OutputBaseFilename=SamsaraSetup-{#AppVersion}" in _section(iss, "Setup")

    def test_keyboard_and_scaling_notes_are_in_the_script(self, iss):
        head = iss[: iss.index("[Setup]")]
        assert "keyboard" in head.lower() and "150%" in head
        assert "WizardResizable=yes" in iss and "WizardSizePercent=120" in iss


class TestFrozenBuildData:
    def test_command_catalog_is_staged_at_the_frozen_data_root(self):
        spec = SPEC.read_text(encoding="utf-8")
        # Match commands.json's existing `datas` destination: PyInstaller's
        # onedir `_MEIPASS` root (`_internal`), where command_catalog.ROOT
        # resolves at runtime.
        assert "datas.append((str(app_dir / 'commands_catalog.json'), '.'))" in spec


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
        assert "%LOCALAPPDATA%\\Programs\\Inno Setup 6\\ISCC.exe" in cmd
        assert 'installer\\samsara.iss' in cmd

    def test_signed_build_is_opt_in_and_unsigned_path_has_no_signing_args(self):
        cmd = CMD.read_text(encoding="utf-8")
        compile_line = next(line for line in cmd.splitlines() if '"%ISCC%" /Qp' in line)
        assert 'if "%SAMSARA_SIGN%"=="1"' in cmd
        assert "/DSignedBuild=1" in cmd and "/Ssamsara=" in cmd
        assert "%SIGN_ARGS%" in compile_line
        assert compile_line.split("%SIGN_ARGS%", 1)[1].startswith(' "/DAppVersion=')

    def test_installer_signing_directives_are_conditional(self):
        setup = _section(ISS.read_text(encoding="utf-8"), "Setup")
        assert re.search(
            r"#ifdef SignedBuild\s+SignTool=samsara\s+SignedUninstaller=yes\s+#endif",
            setup,
        )

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
        # 914: the guide names the exact assets for its versioned release page.
        assert "/ComponentsDir=" in install and "Samsara-Windows-v0.42.0.zip" in install
        assert "SmartScreen" in install
        readme = README.read_text(encoding="utf-8")
        assert "SignPath" in readme and "--fetch-components" in readme
        # 108: the dispatch is real now, so the README documents where it
        # LIVES rather than a snippet someone still has to paste in. Both
        # placement facts are asserted because both are load-bearing.
        assert "_dispatch_startup_argument" in readme, "the dictation.py dispatch is documented"
        assert "single-instance lock" in readme and "heavy import" in readme
