"""Source safety guards for the native 994b smoke scenarios.

The Windows/Inno smoke is a separate build step, not a pytest child process.
These guards preserve its important ordering and identity invariants on every
platform, without requiring an installed application or reading user state.
"""
from pathlib import Path


ISS = (Path(__file__).resolve().parents[1] / "installer/samsara.iss").read_text(
    encoding="utf-8"
)


def body(start, end):
    return ISS.split(start, 1)[1].split(end, 1)[0]


def test_exiting_during_image_query_is_success_before_fallback():
    identify = body("function OpenInstallProcess(", "function InstallProcesses(")
    failed_query = identify.split("Matches := IsInstallImage(Name);", 1)[1]
    assert failed_query.index("if WaitForSingleObject(Handle, 0) = 0 then exit;") < (
        failed_query.index("GetModuleFileNameEx(Handle, 0, Name, 32768)")
    )
    failed_fallback = failed_query.split("Size < 32768", 1)[1].split("end else begin", 1)[1]
    assert failed_fallback.index("if WaitForSingleObject(Handle, 0) = 0 then exit;") < (
        failed_fallback.index("RaiseException")
    )
    assert "Cannot verify the Samsara executable path" not in ISS


def test_unreadable_primary_path_uses_independent_exact_image_check():
    identify = body("function OpenInstallProcess(", "function InstallProcesses(")
    assert "K32GetModuleFileNameExW@kernel32.dll" in ISS
    assert identify.count("Matches := IsInstallImage(Name);") == 2
    assert "(Size > 0) and (Size < 32768)" in identify  # reject truncated paths
    assert "if Matches then begin" in identify


def test_path_aliases_compare_volume_and_file_id():
    identity = body("function IsInstallImage(", "function OpenInstallProcess(")
    for field in ("Volume", "IndexHigh", "IndexLow"):
        assert f"ActualInfo.{field} = InstallInfo.{field}" in identity
    assert "OpenPayloadFile(Name, 0, 7" in identity
    assert "ExpandConstant('{app}\\{#AppExeName}')" in identity
    assert "ClosePayloadHandle(ActualFile)" in identity
    assert "ClosePayloadHandle(InstallFile)" in identity


def test_wait_and_graceful_request_use_retained_handles_without_requery():
    shutdown = body("procedure RequestInstallQuit(", "procedure EnsureInstallStopped();")
    assert "Processes[I].Handle" in shutdown
    for query in ("InstallProcesses()", "OpenInstallProcess(", "QueryFullProcessImageName("):
        assert query not in shutdown
    request = shutdown.split("function WaitForInstallExit", 1)[0]
    assert request.index("GetWindowThreadProcessId(Window, Pid)") < request.index(
        "if (Pid = Processes[I].Pid)"
    ) < request.index("PostMessage(Window, QuitMessage, 0, 0)")
    assert "WaitForSingleObject(Processes[I].Handle, 0) <> 0" in request


def test_only_verified_snapshot_handles_are_retained_and_cleaned():
    scan = body("function InstallProcesses()", "procedure CloseInstallProcesses(")
    assert scan.index("CompareText(Name, '{#AppExeName}')") < scan.index(
        "Handle := OpenInstallProcess(Entry.Pid)"
    ) < scan.index("Result[Count].Handle := Handle")
    assert "except" in scan and "ClosePayloadHandle(Result[I].Handle)" in scan
    stop = body("procedure EnsureInstallStopped();", "{ Check every payload file")
    assert stop.count("Processes := InstallProcesses();") == 1
    assert "finally\n    CloseInstallProcesses(Processes);" in stop


def test_force_rechecks_original_liveness_after_reopening_pid():
    stop = body("procedure EnsureInstallStopped();", "{ Check every payload file")
    force = stop.split("Handle := OpenProcess($100001, False, Processes[I].Pid)", 1)[1]
    assert force.index("if WaitForSingleObject(Processes[I].Handle, 0) = 0 then continue;") < (
        force.index("TerminateProcess(Handle, 0)")
    )
    failure = force.split("if not TerminateProcess(Handle, 0) then", 1)[1]
    assert failure.index("WaitForSingleObject(Processes[I].Handle, 0) <> 0") < (
        failure.index("RaiseException")
    )
    assert "if Handle <> 0 then ClosePayloadHandle(Handle);" in force


def test_visible_wait_has_ten_second_grace_period_before_force():
    wait = body("function WaitForInstallExit(", "procedure EnsureInstallStopped();")
    assert "for I := 0 to 39 do" in wait and "Sleep(250)" in wait
    assert "Closing Samsara... %d seconds remaining" in wait
    assert "WaitForm.Refresh();" in wait
    assert "while PeekShutdownMessage(Msg, 0, 0, 0, 1) do" in wait
    assert "DispatchShutdownMessage(Msg);" in wait
    stop = body("procedure EnsureInstallStopped();", "{ Check every payload file")
    assert stop.index("WaitForm.Show();") < stop.index("RequestInstallQuit(Processes);") < (
        stop.index("if WaitForInstallExit(Processes, WaitForm, Status) then exit;")
    ) < stop.index("TerminateProcess(Handle, 0)")
    assert "UninstallProgressForm." not in stop  # absent during usAppMutexCheck
    assert "WaitForm.Free();" in stop


def test_cancel_and_silent_abort_before_any_shutdown_request():
    stop = body("procedure EnsureInstallStopped();", "{ Check every payload file")
    request = stop.index("RequestInstallQuit(Processes);")
    assert stop.index("if UninstallSilent() then") < request
    assert stop.index("IDCANCEL) <> IDOK then") < request
    assert stop[:request].count("UninstallCloseCancelled := True; Abort;") == 2
    handler = body("procedure CurUninstallStepChanged", "{ The ids to fetch")
    assert "if not UninstallCloseCancelled then\n        SuppressibleMsgBox" in handler
