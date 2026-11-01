"""Safety invariants for the native Inno wipe implementation.

Native disposable-profile smoke is a separate build step; these tests need
neither a compiler nor subprocesses and work in a plain repository export.
"""
from pathlib import Path


ISS = (Path(__file__).resolve().parents[1] / "installer/samsara.iss").read_text(
    encoding="utf-8"
)


def body(start, end):
    return ISS.split(start, 1)[1].split(end, 1)[0]


def test_default_and_cancel_cannot_authorize_wipe():
    choose = body("procedure ChooseUninstallData();", "function DataPathAttributes")
    assert "UninstallWipeData := False;" in choose
    assert "Wipe.Checked := False;" in choose
    assert choose.index("Form.ShowModal() <> mrOk") < choose.index(
        "UninstallWipeData := Wipe.Checked;"
    )
    assert "UninstallCloseCancelled := True; Abort;" in choose
    assert "Form.Free();" in choose


def test_silent_requires_exact_explicit_switch_and_never_shows_page():
    choose = body("procedure ChooseUninstallData();", "function DataPathAttributes")
    assert "for I := 1 to ParamCount do" in choose
    assert "CompareText(ParamStr(I), '/WIPEDATA') = 0" in choose
    assert choose.index("if UninstallWipeData or UninstallSilent() then exit;") < (
        choose.index("CreateCustomForm")
    )
    assert "{param:WIPEDATA" not in ISS  # presence/values must not imply consent


def test_targets_match_app_home_and_only_direct_systran_downloads():
    targets = body("function UninstallDataTargets()", "function CleanUserData(")
    assert "Home := GetEnv('SAMSARA_HOME_DIR');" in targets
    assert "if Home = '' then Home := Profile + '\\.samsara';" in targets
    assert "Profile := GetEnv('USERPROFILE');" in targets
    assert "Hub := Profile + '\\.cache\\huggingface\\hub';" in targets
    assert "FindFirstPayload(Hub + '\\models--Systran--faster-whisper-*', FindRec)" in targets
    assert "FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY" in targets
    assert "Length(Name) > Length('models--Systran--faster-whisper-')" in targets
    assert "Result[Count] := Hub + '\\' + Name;" in targets
    assert "DelTree(" not in ISS  # never hand a cache root/wildcard to recursive deletion


def test_broad_overrides_and_redirected_ancestors_fail_closed():
    check = body("procedure CheckDataPath(", "function UninstallDataTargets()")
    assert "Copy(Path, 2, 2) <> ':\\'" in check
    assert "Length(Path) < 4" in check
    assert "Attributes and $400" in check
    assert "Current := ExtractFileDir(Current);" in check
    targets = body("function UninstallDataTargets()", "function CleanUserData(")
    for protected in ("Profile", "Hub", "ExpandConstant('{app}')",
                      "ExpandConstant('{win}')", "ExpandConstant('{userdocs}')",
                      "ExpandConstant('{localappdata}')", "ExpandConstant('{userappdata}')"):
        assert f"DataPathWithin({protected}, Home)" in targets
    assert "DataPathWithin(Home, Hub)" in targets
    assert targets.count("CheckDataPath(Home);") == 2  # before and after normalization


def test_recursive_delete_checks_links_and_files_before_removal():
    clean = body("function CleanUserData(", "procedure CurUninstallStepChanged(")
    assert clean.index("CheckDataPath(Dir);") < clean.index("FindFirstPayload(")
    assert "if not CleanUserData(Path, RemoveFiles) then exit;" in clean
    assert "OpenPayloadFile(Path, $10000, 0, 0, 3, $200000, 0)" in clean
    assert "else if RemoveFiles then begin" in clean
    assert "if not DeleteFile(Path) then exit;" in clean
    assert "if RemoveFiles then Result := RemoveDir(Dir);" in clean
    assert "DLLGetLastError() <> 18" in clean
    assert "ClosePayloadSearch(Search);" in clean


def test_shutdown_choice_and_all_preflights_precede_deletion():
    handler = body("procedure CurUninstallStepChanged(", "{ The ids to fetch")
    sequence = (
        "EnsureInstallStopped();", "if CurUninstallStep <> usUninstall then exit;",
        "ChooseUninstallData();", "if UninstallWipeData then begin",
        "Targets := UninstallDataTargets();", "CleanUserData(Targets[I], False)",
        "CleanPayload(ExpandConstant('{app}'), False, True)",
        "if UninstallWipeData then\n", "CleanUserData(Targets[I], True)",
        "CleanPayload(ExpandConstant('{app}'), True, True)",
    )
    offsets = [handler.index(item) for item in sequence]
    assert offsets == sorted(offsets)
    assert "Samsara remains in Apps so you can retry uninstall." in handler
    assert "if not UninstallCloseCancelled then" in handler
