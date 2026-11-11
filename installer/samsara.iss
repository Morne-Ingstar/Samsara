; Samsara installer -- Inno Setup 6 (34).
;
; Installs the app and its bundled runtime files, including wake-word models.
; Optional packs are available later from Settings > Advanced > Components.
; No component selection or downloads run during installation (1016).
; Existing add-on files are preserved on upgrade.
;
; Build: tools\build_installer.cmd (reads the version from
; samsara\__init__.py, runs ISCC). Compile-time defines:
;   AppVersion   e.g. 0.23.0-beta.1   (required)
;   DistDir      the PyInstaller output folder (default ..\dist\Samsara)
;   OutputDir    where SamsaraSetup-<version>.exe goes (default ..\dist)
;
; Uninstaller only: /WIPEDATA explicitly removes Samsara's user data and
; Systran faster-whisper downloads, including during silent uninstall.
;
; Accessibility: Inno Setup's wizard is fully keyboard-driven (Tab/Shift+Tab
; through every control, Alt+N / Alt+B for
; Next / Back, Enter for the default button) and has no hover-only
; controls; the wizard is per-monitor DPI aware, so at 150% Windows
; scaling every page, list and button scales with the text.

#ifndef AppVersion
  #error AppVersion is required, e.g. ISCC /DAppVersion=0.23.0-beta.1 samsara.iss (tools\build_installer.cmd does this)
#endif
#ifndef DistDir
  #define DistDir "..\dist\Samsara"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#define AppName "Samsara"
#define AppPublisher "Samsara"
#define AppURL "https://github.com/Morne-Ingstar/Samsara"
#define AppExeName "Samsara.exe"

[Setup]
AppId={{7C0E4D2B-5A57-4F5E-9C7E-6B1B3F2D8A34}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#GetVersionNumbersString(DistDir + "\" + AppExeName)}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#GetVersionNumbersString(DistDir + "\" + AppExeName)}
VersionInfoTextVersion={#GetVersionNumbersString(DistDir + "\" + AppExeName)}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoDescription=Samsara voice control and dictation
VersionInfoCompany={#AppPublisher}
VersionInfoCopyright=Copyright Samsara contributors. Licensed under AGPL-3.0.
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
; Per-user by default (no admin): {autopf} resolves to %LOCALAPPDATA%\Programs
; when running unelevated. The dialog lets the user pick "all users" instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=SamsaraSetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardResizable=yes
WizardSizePercent=120
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile=..\LICENSE
SetupLogging=yes
AppMutex=Local\Samsara.SingleInstance
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; AppUserModelID: "MorneIngstar.Samsara"; Tasks: startmenushortcut
Name: "{autoprograms}\{#AppName}\Uninstall {#AppName}"; Filename: "{uninstallexe}"; Tasks: startmenushortcut
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; AppUserModelID: "MorneIngstar.Samsara"; Tasks: desktopicon

[Tasks]
Name: "startmenushortcut"; Description: "Create a Start Menu shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
var
  UninstallCloseCancelled: Boolean;
  UninstallWipeData: Boolean;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then begin
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'Optional components can be added any time from Settings.';
    WizardForm.FinishedLabel.AdjustHeight();
    WizardForm.RunList.Top := WizardForm.FinishedLabel.Top +
      WizardForm.FinishedLabel.Height + ScaleY(16);
    WizardForm.RunList.Height := WizardForm.FinishedPage.ClientHeight -
      WizardForm.RunList.Top;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  { 1005: foreground the uninstaller before Inno's own confirmation. }
  if not UninstallSilent() then BringToFrontAndRestore();
  Result := True;
end;

{ 994: never target an executable by name alone. Profile overrides have
  different mutexes, so enumerate every process from this exact install. }
function FindWindowEx(Parent, After: HWND; ClassName, Title: String): HWND;
  external 'FindWindowExW@user32.dll stdcall';
function GetWindowThreadProcessId(Window: HWND; var ProcessId: Cardinal): Cardinal;
  external 'GetWindowThreadProcessId@user32.dll stdcall';
function RegisterWindowMessage(Name: String): Cardinal;
  external 'RegisterWindowMessageW@user32.dll stdcall';
function PostMessage(Window: HWND; Msg, WParam, LParam: Cardinal): Boolean;
  external 'PostMessageW@user32.dll stdcall';
function OpenPayloadFile(Name: String; Access, Share: Cardinal; Security: Integer;
  Creation, Flags: Cardinal; Template: Integer): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function ClosePayloadHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

type
  TProcessEntry = record
    Size, Usage, Pid, Heap, ModuleId, Threads, ParentPid: Cardinal;
    Priority: Integer;
    Flags: Cardinal;
    ExeFile: array[0..259] of Char;
  end;

function CreateToolhelp32Snapshot(Flags, Pid: Cardinal): THandle;
  external 'CreateToolhelp32Snapshot@kernel32.dll stdcall';
function Process32First(Snapshot: THandle; var Entry: TProcessEntry): Boolean;
  external 'Process32FirstW@kernel32.dll stdcall';
function Process32Next(Snapshot: THandle; var Entry: TProcessEntry): Boolean;
  external 'Process32NextW@kernel32.dll stdcall';
function OpenProcess(Access: Cardinal; Inherit: Boolean; Pid: Cardinal): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function QueryFullProcessImageName(Process: THandle; Flags: Cardinal;
  Name: String; var Size: Cardinal): Boolean;
  external 'QueryFullProcessImageNameW@kernel32.dll stdcall';
function GetModuleFileNameEx(Process: THandle; Module: THandle;
  Name: String; Size: Cardinal): Cardinal;
  external 'K32GetModuleFileNameExW@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function TerminateProcess(Process: THandle; ExitCode: Cardinal): Boolean;
  external 'TerminateProcess@kernel32.dll stdcall';

type
  TInstallProcess = record
    Pid: Cardinal;
    Handle: THandle;
  end;
  TInstallProcesses = array of TInstallProcess;
  TFileIdentity = record
    Attributes, CreatedLow, CreatedHigh, AccessedLow, AccessedHigh: Cardinal;
    WrittenLow, WrittenHigh, Volume, SizeHigh, SizeLow, Links: Cardinal;
    IndexHigh, IndexLow: Cardinal;
  end;

function GetFileInformationByHandle(Handle: THandle; var Info: TFileIdentity): Boolean;
  external 'GetFileInformationByHandle@kernel32.dll stdcall';

{ 994b: compare file identities as well as spelling (case, 8.3, long paths).
  Neither fallback ever treats an executable name alone as install identity. }
function IsInstallImage(const Name: String): Boolean;
var
  Expected: String;
  ActualFile, InstallFile: THandle;
  ActualInfo, InstallInfo: TFileIdentity;
begin
  Expected := ExpandConstant('{app}\{#AppExeName}');
  Result := CompareText(Name, Expected) = 0;
  if Result then exit;
  ActualFile := OpenPayloadFile(Name, 0, 7, 0, 3, 0, 0);
  if ActualFile = THandle(-1) then exit;
  try
    InstallFile := OpenPayloadFile(Expected, 0, 7, 0, 3, 0, 0);
    if InstallFile = THandle(-1) then exit;
    try
      if GetFileInformationByHandle(ActualFile, ActualInfo) and
        GetFileInformationByHandle(InstallFile, InstallInfo) then
        Result := (ActualInfo.Volume = InstallInfo.Volume) and
          (ActualInfo.IndexHigh = InstallInfo.IndexHigh) and
          (ActualInfo.IndexLow = InstallInfo.IndexLow);
    finally
      ClosePayloadHandle(InstallFile);
    end;
  finally
    ClosePayloadHandle(ActualFile);
  end;
end;

function OpenInstallProcess(Pid: Cardinal): THandle;
var
  Handle: THandle;
  Size: Cardinal;
  Name: String;
  Matches: Boolean;
begin
  Result := 0;
  Handle := OpenProcess($101000, False, Pid); { QUERY_LIMITED_INFORMATION | SYNCHRONIZE }
  if Handle = 0 then begin
    if DLLGetLastError() = 87 then exit; { exited since snapshot }
    RaiseException('Windows did not allow Samsara to be closed. Uninstall cancelled; no files were removed.');
  end;
  try
    if WaitForSingleObject(Handle, 0) = 0 then exit;
    Size := 32768;
    Name := StringOfChar(#0, Size);
    if QueryFullProcessImageName(Handle, 0, Name, Size) then begin
      SetLength(Name, Size);
      Matches := IsInstallImage(Name);
    end else begin
      if WaitForSingleObject(Handle, 0) = 0 then exit;
      Size := GetModuleFileNameEx(Handle, 0, Name, 32768);
      if (Size > 0) and (Size < 32768) then begin
        SetLength(Name, Size);
        Matches := IsInstallImage(Name);
      end else begin
        if WaitForSingleObject(Handle, 0) = 0 then exit;
        RaiseException('Windows did not allow the running application to be identified. Uninstall cancelled; no files were removed.');
      end;
    end;
    { A process that exited during ANY identity check is already closed. }
    if WaitForSingleObject(Handle, 0) = 0 then exit;
    if Matches then begin
      Result := Handle;
      Handle := 0;
    end;
  finally
    if Handle <> 0 then ClosePayloadHandle(Handle);
  end;
end;

function InstallProcesses(): TInstallProcesses;
var
  Snapshot, Handle: THandle;
  Entry: TProcessEntry;
  Name: String;
  I, Count: Integer;
  More: Boolean;
begin
  SetArrayLength(Result, 0);
  Snapshot := CreateToolhelp32Snapshot(2, 0); { TH32CS_SNAPPROCESS }
  if Snapshot = THandle(-1) then RaiseException('Could not check running applications.');
  try
    { Inno 6 runs 32-bit even when installing the 64-bit application.
      PROCESSENTRY32W is nine DWORDs plus 260 WCHARs (556 bytes). }
    Entry.Size := 556;
    More := Process32First(Snapshot, Entry);
    while More do begin
      Name := '';
      for I := 0 to 259 do begin
        if Entry.ExeFile[I] = #0 then break;
        Name := Name + Entry.ExeFile[I];
      end;
      if CompareText(Name, '{#AppExeName}') = 0 then begin
        Handle := OpenInstallProcess(Entry.Pid);
        if Handle <> 0 then begin
          Count := GetArrayLength(Result);
          SetArrayLength(Result, Count + 1);
          Result[Count].Pid := Entry.Pid;
          Result[Count].Handle := Handle;
        end;
      end;
      More := Process32Next(Snapshot, Entry);
    end;
    if DLLGetLastError() <> 18 then RaiseException('Could not finish checking running applications.');
  except
    for I := 0 to GetArrayLength(Result) - 1 do ClosePayloadHandle(Result[I].Handle);
    ClosePayloadHandle(Snapshot);
    RaiseException(GetExceptionMessage());
  end;
  ClosePayloadHandle(Snapshot);
end;

procedure CloseInstallProcesses(var Processes: TInstallProcesses);
var
  I: Integer;
begin
  for I := 0 to GetArrayLength(Processes) - 1 do
    ClosePayloadHandle(Processes[I].Handle);
  SetArrayLength(Processes, 0);
end;

function InstallIsRunning(const Processes: TInstallProcesses): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 0 to GetArrayLength(Processes) - 1 do
    if WaitForSingleObject(Processes[I].Handle, 0) <> 0 then begin
      Result := True;
      exit;
    end;
end;

procedure RequestInstallQuit(const Processes: TInstallProcesses);
var
  I: Integer;
  Window: HWND;
  Pid, QuitMessage: Cardinal;
begin
  QuitMessage := RegisterWindowMessage('Samsara.QuitForUninstall');
  { A failed graceful request still gets the bounded wait and force fallback. }
  if QuitMessage = 0 then exit;
  for I := 0 to GetArrayLength(Processes) - 1 do begin
    if WaitForSingleObject(Processes[I].Handle, 0) = 0 then continue;
    Window := FindWindowEx(0, 0, '', '');
    while Window <> 0 do begin
      GetWindowThreadProcessId(Window, Pid);
      { Check liveness AFTER reading the window PID: prevent PID reuse. }
      if (Pid = Processes[I].Pid) and
        (WaitForSingleObject(Processes[I].Handle, 0) <> 0) then
        if UninstallSilent() then PostMessage(Window, QuitMessage, 1, 0)
        else PostMessage(Window, QuitMessage, 0, 0);
      Window := FindWindowEx(0, Window, '', '');
    end;
  end;
end;

type
  TShutdownMessage = record
    Window: HWND;
    Msg, WParam, LParam, Time: Cardinal;
    X, Y: Integer;
    Reserved: Cardinal;
  end;

function PeekShutdownMessage(var Msg: TShutdownMessage;
  Window: HWND; First, Last, Remove: Cardinal): Boolean;
  external 'PeekMessageW@user32.dll stdcall';
function TranslateShutdownMessage(var Msg: TShutdownMessage): Boolean;
  external 'TranslateMessage@user32.dll stdcall';
function DispatchShutdownMessage(var Msg: TShutdownMessage): Integer;
  external 'DispatchMessageW@user32.dll stdcall';

function WaitForInstallExit(const Processes: TInstallProcesses;
  WaitForm: TSetupForm; Status: TNewStaticText): Boolean;
var
  I: Integer;
  Msg: TShutdownMessage;
begin
  Result := False;
  for I := 0 to 39 do begin
    if not InstallIsRunning(Processes) then begin Result := True; exit; end;
    { PLACEHOLDER COPY (994b). Keep the native wait window responsive;
      retained handles, not repeated path queries, determine completion. }
    if WaitForm <> nil then begin
      Status.Caption :=
        Format('Closing Samsara... %d seconds remaining', [10 - I div 4]);
      WaitForm.Refresh();
    end;
    while PeekShutdownMessage(Msg, 0, 0, 0, 1) do begin { PM_REMOVE }
      if Msg.Msg = $12 then begin { WM_QUIT: cancel before payload removal }
        UninstallCloseCancelled := True;
        Abort;
      end;
      TranslateShutdownMessage(Msg);
      DispatchShutdownMessage(Msg);
    end;
    Sleep(250);
  end;
  Result := not InstallIsRunning(Processes);
end;

procedure EnsureInstallStopped();
var
  Processes: TInstallProcesses;
  Handle: THandle;
  I: Integer;
  WaitForm: TSetupForm;
  Status: TNewStaticText;
begin
  UninstallCloseCancelled := False;
  Processes := InstallProcesses();
  try
    if GetArrayLength(Processes) = 0 then exit;
    if not UninstallSilent() then begin
      BringToFrontAndRestore();
      if SuppressibleTaskDialogMsgBox('Samsara is running. Close it and continue?',
      'Samsara will be asked to exit. If it is still running after 10 seconds, it will be force-closed. Save your work first.',
        mbConfirmation, MB_OKCANCEL, ['Close Samsara', 'Cancel'], 0, IDCANCEL) <> IDOK then begin UninstallCloseCancelled := True; Abort; end;
    end;
    { The uninstaller's progress form does not exist at usAppMutexCheck. }
    WaitForm := nil;
    Status := nil;
    try
      if not UninstallSilent() then begin
      WaitForm := CreateCustomForm(ScaleX(380), ScaleY(76), False, False);
      WaitForm.Caption := 'Uninstall Samsara';
      WaitForm.BorderIcons := [];
      Status := TNewStaticText.Create(WaitForm);
      Status.Parent := WaitForm;
      Status.Left := ScaleX(16);
      Status.Top := ScaleY(24);
      Status.Width := WaitForm.ClientWidth - ScaleX(32);
      WaitForm.FlipAndCenterIfNeeded(True, nil, False);
      WaitForm.Show();
      end;
      RequestInstallQuit(Processes);
      if WaitForInstallExit(Processes, WaitForm, Status) then exit;
      { 1005: unattended uninstall cannot resolve a pending draft. Keep the
        app and retry uninstaller intact rather than forcing past that choice. }
      if UninstallSilent() then begin UninstallCloseCancelled := True; Abort; end;
      if WaitForm <> nil then begin
        Status.Caption := 'Force-closing Samsara...';
        WaitForm.Refresh();
      end;
      for I := 0 to GetArrayLength(Processes) - 1 do begin
        if WaitForSingleObject(Processes[I].Handle, 0) = 0 then continue;
        Handle := OpenProcess($100001, False, Processes[I].Pid); { SYNCHRONIZE | TERMINATE }
        try
          { Recheck the retained handle AFTER reopening: a reused PID must
            never inherit the old process's verified install identity. }
          if WaitForSingleObject(Processes[I].Handle, 0) = 0 then continue;
          if Handle = 0 then
            RaiseException('Windows did not allow Samsara to be closed. Uninstall cancelled; no files were removed.');
          if not TerminateProcess(Handle, 0) then
            if WaitForSingleObject(Processes[I].Handle, 0) <> 0 then
              RaiseException('Windows did not allow Samsara to be closed. Uninstall cancelled; no files were removed.');
        finally
          if Handle <> 0 then ClosePayloadHandle(Handle);
        end;
      end;
      if not WaitForInstallExit(Processes, WaitForm, Status) then
        RaiseException('Samsara is still running. Uninstall cancelled; no files were removed.');
    finally
      if WaitForm <> nil then WaitForm.Free();
    end;
  finally
    CloseInstallProcesses(Processes);
  end;
end;

{ Check every payload file before deleting any, then remove it ourselves.
  An error aborts at usUninstall, BEFORE Inno removes the Apps entry or its
  retry executable/log. No reboot-delete of locked payload is permitted. }
type
  TPayloadFindData = record
    Attributes, CreatedLow, CreatedHigh, AccessedLow, AccessedHigh: Cardinal;
    WrittenLow, WrittenHigh, SizeHigh, SizeLow, Reserved0, Reserved1: Cardinal;
    Name: array[0..259] of Char;
    AlternateName: array[0..13] of Char;
  end;

function FindFirstPayload(Name: String; var Data: TPayloadFindData): THandle;
  external 'FindFirstFileW@kernel32.dll stdcall';
function FindNextPayload(Handle: THandle; var Data: TPayloadFindData): Boolean;
  external 'FindNextFileW@kernel32.dll stdcall';
function ClosePayloadSearch(Handle: THandle): Boolean;
  external 'FindClose@kernel32.dll stdcall';

function CleanPayload(const Dir: String; RemoveFiles, Root: Boolean): Boolean;
var
  FindRec: TPayloadFindData;
  Name, Path, Uninstaller: String;
  Search, Handle: THandle;
  I: Integer;
begin
  Result := True;
  Uninstaller := ExpandConstant('{uninstallexe}');
  Search := FindFirstPayload(AddBackslash(Dir) + '*', FindRec);
  if Search = THandle(-1) then begin
    Result := DLLGetLastError() = 2; { empty directory, not access denied }
    exit;
  end;
  try
    repeat
      Name := '';
      for I := 0 to 259 do begin
        if FindRec.Name[I] = #0 then break;
        Name := Name + FindRec.Name[I];
      end;
      if (Name <> '.') and (Name <> '..') then begin
        Path := AddBackslash(Dir) + Name;
        if Root and ((CompareText(Path, Uninstaller) = 0)
          or (CompareText(Path, ChangeFileExt(Uninstaller, '.dat')) = 0)
          or (CompareText(Path, ChangeFileExt(Uninstaller, '.msg')) = 0)) then continue;
        if (FindRec.Attributes and $400) <> 0 then begin Result := False; exit; end;
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then begin
          if not CleanPayload(Path, RemoveFiles, False) then begin Result := False; exit; end;
          if RemoveFiles and not RemoveDir(Path) then begin Result := False; exit; end;
        end else if RemoveFiles then begin
          if not DeleteFile(Path) then begin Result := False; exit; end;
        end else begin
          Handle := OpenPayloadFile(Path, $10000, 0, 0, 3, 0, 0);
          if Handle = THandle(-1) then begin Result := False; exit; end;
          ClosePayloadHandle(Handle);
        end;
      end;
    until not FindNextPayload(Search, FindRec);
    if DLLGetLastError() <> 18 then Result := False;
  finally
    ClosePayloadSearch(Search);
  end;
end;

{ 994c: opt in only, after shutdown; silent runs never show this page. }
procedure ChooseUninstallData();
var
  Form: TSetupForm;
  Wipe: TNewCheckBox;
  Explanation: TNewStaticText;
  RemoveButton, CancelButton: TNewButton;
  I: Integer;
begin
  UninstallWipeData := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/WIPEDATA') = 0 then UninstallWipeData := True;
  if UninstallWipeData or UninstallSilent() then exit;
  Form := CreateCustomForm(ScaleX(720), ScaleY(144), False, False);
  try
    { Use the custom form's initialized DPI/font scale for its final bounds. }
    Form.ClientWidth := ScaleX(720);
    Form.ClientHeight := ScaleY(144);
    { PLACEHOLDER COPY (994c) }
    Form.Caption := 'Uninstall Samsara';
    Wipe := TNewCheckBox.Create(Form);
    Wipe.Parent := Form;
    Wipe.SetBounds(ScaleX(16), ScaleY(20), ScaleX(688), ScaleY(24));
    Wipe.Caption := 'Also remove my Samsara settings, history and downloaded speech models (start fresh next time)';
    Wipe.Checked := False;
    Explanation := TNewStaticText.Create(Form);
    Explanation.Parent := Form;
    Explanation.SetBounds(ScaleX(16), ScaleY(54), ScaleX(688), ScaleY(24));
    Explanation.Caption := 'Leave unticked to keep your settings, history and downloaded speech models for reinstalling.';
    RemoveButton := TNewButton.Create(Form);
    RemoveButton.Parent := Form;
    RemoveButton.SetBounds(ScaleX(512), ScaleY(102), ScaleX(88), ScaleY(26));
    RemoveButton.Caption := 'Uninstall';
    RemoveButton.ModalResult := mrOk;
    RemoveButton.Default := True;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.SetBounds(ScaleX(608), ScaleY(102), ScaleX(96), ScaleY(26));
    CancelButton.Caption := 'Cancel';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    Form.ActiveControl := Wipe;
    Form.FlipAndCenterIfNeeded(True, nil, False);
    if Form.ShowModal() <> mrOk then begin UninstallCloseCancelled := True; Abort; end;
    UninstallWipeData := Wipe.Checked;
  finally
    Form.Free();
  end;
end;

function DataPathAttributes(Name: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function DataPathWithin(const Child, Parent: String): Boolean;
begin
  Result := Pos(Uppercase(AddBackslash(Parent)), Uppercase(AddBackslash(Child))) = 1;
end;

function DataFoundName(const Data: TPayloadFindData): String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to 259 do begin
    if Data.Name[I] = #0 then break;
    Result := Result + Data.Name[I];
  end;
end;

{ Refuse drive roots, ambiguous relative/UNC paths and redirected ancestors.
  Do not follow junctions, even when they are inside an explicitly chosen home. }
procedure CheckDataPath(const Path: String);
var
  Current: String;
  Attributes: Cardinal;
begin
  if (Length(Path) < 4) or (Copy(Path, 2, 2) <> ':\') then
    RaiseException('The Samsara data folder is unsafe to remove. No data was removed.');
  Current := Path;
  while Length(Current) > 3 do begin
    Attributes := DataPathAttributes(Current);
    if Attributes = $FFFFFFFF then begin
      if (DLLGetLastError() <> 2) and (DLLGetLastError() <> 3) then
        RaiseException('The Samsara data folder cannot be checked. No data was removed.');
    end else if (Attributes and $400) <> 0 then
      RaiseException('A data folder is a link or junction. Uninstall cancelled to protect other files.');
    Current := ExtractFileDir(Current);
  end;
end;

function UninstallDataTargets(): TArrayOfString;
var
  Home, Profile, Hub, Name: String;
  FindRec: TPayloadFindData;
  Search: THandle;
  Count: Integer;
begin
  Profile := GetEnv('USERPROFILE');
  CheckDataPath(Profile);
  Profile := RemoveBackslashUnlessRoot(ExpandFileName(Profile));
  Hub := Profile + '\.cache\huggingface\hub';
  Home := GetEnv('SAMSARA_HOME_DIR');
  if Home = '' then Home := Profile + '\.samsara';
  CheckDataPath(Home);
  Home := RemoveBackslashUnlessRoot(ExpandFileName(Home));
  CheckDataPath(Home);
  { An override owns its whole directory, but must never name a broad shared
    location, the install payload, or the shared cache (in either direction). }
  if DataPathWithin(Profile, Home) or
    DataPathWithin(ExpandConstant('{app}'), Home) or
    DataPathWithin(Home, ExpandConstant('{app}')) or
    DataPathWithin(ExpandConstant('{win}'), Home) or
    DataPathWithin(Home, ExpandConstant('{win}')) or
    DataPathWithin(ExpandConstant('{userdocs}'), Home) or
    DataPathWithin(ExpandConstant('{localappdata}'), Home) or
    DataPathWithin(ExpandConstant('{userappdata}'), Home) or
    DataPathWithin(Hub, Home) or DataPathWithin(Home, Hub) then
    RaiseException('The Samsara data folder is a shared or protected location. No data was removed.');
  SetArrayLength(Result, 1);
  Result[0] := Home;
  CheckDataPath(Hub);
  if not DirExists(Hub) then exit;
  Search := FindFirstPayload(Hub + '\models--Systran--faster-whisper-*', FindRec);
  if Search = THandle(-1) then begin
    if DLLGetLastError() <> 2 then
      RaiseException('The speech model folders cannot be checked. No data was removed.');
    exit;
  end;
  try
    repeat
      Name := DataFoundName(FindRec);
      if ((FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and
        (Length(Name) > Length('models--Systran--faster-whisper-')) then begin
        Count := GetArrayLength(Result);
        SetArrayLength(Result, Count + 1);
        Result[Count] := Hub + '\' + Name;
        CheckDataPath(Result[Count]);
      end;
    until not FindNextPayload(Search, FindRec);
    if DLLGetLastError() <> 18 then
      RaiseException('The speech model folders cannot be checked. No data was removed.');
  finally
    ClosePayloadSearch(Search);
  end;
end;

{ Check the complete selection before deleting anything. Directory junctions
  abort; file symlinks (normal HF snapshots) are opened/deleted as links only. }
function CleanUserData(const Dir: String; RemoveFiles: Boolean): Boolean;
var
  FindRec: TPayloadFindData;
  Path, Name: String;
  Handle, Search: THandle;
  Attributes: Cardinal;
begin
  Result := False;
  CheckDataPath(Dir);
  Attributes := DataPathAttributes(Dir);
  if Attributes = $FFFFFFFF then begin
    Result := (DLLGetLastError() = 2) or (DLLGetLastError() = 3);
    exit;
  end;
  if (Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then exit;
  Search := FindFirstPayload(AddBackslash(Dir) + '*', FindRec);
  if Search <> THandle(-1) then begin
    try
      repeat
        Name := DataFoundName(FindRec);
        if (Name <> '.') and (Name <> '..') then begin
          Path := AddBackslash(Dir) + Name;
          if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then begin
            if not CleanUserData(Path, RemoveFiles) then exit;
          end else if RemoveFiles then begin
            if not DeleteFile(Path) then exit;
          end else begin
            Handle := OpenPayloadFile(Path, $10000, 0, 0, 3, $200000, 0);
            if Handle = THandle(-1) then exit;
            ClosePayloadHandle(Handle);
          end;
        end;
      until not FindNextPayload(Search, FindRec);
      if DLLGetLastError() <> 18 then exit;
    finally
      ClosePayloadSearch(Search);
    end;
  end else if DLLGetLastError() <> 2 then exit;
  Result := True;
  if RemoveFiles then Result := RemoveDir(Dir);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Targets: TArrayOfString;
  I: Integer;
begin
  if (CurUninstallStep = usAppMutexCheck) or (CurUninstallStep = usUninstall) then begin
    try
      EnsureInstallStopped();
      if CurUninstallStep <> usUninstall then exit;
      ChooseUninstallData();
      if UninstallWipeData then begin
        Targets := UninstallDataTargets();
        for I := 0 to GetArrayLength(Targets) - 1 do
          if not CleanUserData(Targets[I], False) then
            RaiseException('Samsara data is in use or cannot be removed. No data was removed; close other programs and retry.');
      end;
      if not CleanPayload(ExpandConstant('{app}'), False, True) then
        RaiseException('Files are in use or cannot be removed. Close other programs and retry uninstall.');
      if UninstallWipeData then
        for I := 0 to GetArrayLength(Targets) - 1 do
          if not CleanUserData(Targets[I], True) then
            RaiseException('Some Samsara data could not be removed. Samsara remains in Apps so you can retry uninstall.');
      if not CleanPayload(ExpandConstant('{app}'), True, True) then
        RaiseException('Some files could not be removed. Samsara remains in Apps so you can retry uninstall.');
    except
      if not UninstallCloseCancelled then
        SuppressibleMsgBox(GetExceptionMessage(), mbError, MB_OK, IDOK);
      Abort;
    end;
  end;
end;
