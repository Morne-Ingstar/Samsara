; Samsara installer -- Inno Setup 6 (34).
;
; Installs the CORE app only, per user by default (no admin), and offers
; the optional components from the release manifest on a Components page.
; The big components are NOT bundled: after the files are copied the
; installer runs the app's own fetcher (Samsara.exe --fetch-components ...,
; samsara/ui/first_run_qt.py) for the selected ids, which downloads with
; SHA-256 verification, resumes, and can be cancelled. Cancelling, or having
; no network, leaves a working core install; the Finished page says so.
; Anything skipped here stays available in the first-run wizard and in
; Settings forever.
;
; Build: tools\build_installer.cmd (reads the version from
; samsara\__init__.py and the component sizes from manifest.json, writes
; installer\manifest_defines.iss, runs ISCC). Compile-time defines:
;   AppVersion   e.g. 0.23.0-beta.1   (required)
;   DistDir      the PyInstaller output folder (default ..\dist\Samsara)
;   OutputDir    where SamsaraSetup-<version>.exe goes (default ..\dist)
;   ManifestUrl  the release manifest URL (default: the tag's asset)
;
; Command-line switches the installer accepts (besides Inno's own):
;   /ComponentsDir="D:\folder"   fetch components from a local folder of the
;                                release ZIPs instead of the network
;   /NoFetch                     skip the post-install fetch entirely
;
; Accessibility: Inno Setup's wizard is fully keyboard-driven (Tab/Shift+Tab
; through every control, Space toggles a component, Alt+N / Alt+B for
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
#ifndef ManifestUrl
  #define ManifestUrl "https://github.com/Morne-Ingstar/Samsara/releases/download/v" + AppVersion + "/manifest.json"
#endif

; Component sizes and availability, generated from manifest.json by
; tools\build_installer.cmd. Without the file every size is unknown (0)
; and the command model is treated as not yet available.
#ifexist "manifest_defines.iss"
  #include "manifest_defines.iss"
#endif
#ifndef CudaPackSize
  #define CudaPackSize 0
#endif
#ifndef WakeWordModelsSize
  #define WakeWordModelsSize 0
#endif
#ifndef CommandModelSize
  #define CommandModelSize 0
#endif
#ifndef CommandModelAvailable
  #define CommandModelAvailable 0
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
ShowComponentSizes=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "full";    Description: "Full (everything your machine can use)"
Name: "minimal"; Description: "Core application only"
Name: "custom";  Description: "Custom"; Flags: iscustom

; Component names must be Inno identifiers (no hyphens); ManifestId() in
; [Code] maps each to its manifest id, and tests/test_installer_script.py
; asserts every mapped id exists in the manifest. Sizes come from the
; manifest (ExtraDiskSpaceRequired shows them on the page). Every default
; is chosen so that pressing Enter through the wizard gives a working
; install: core is fixed; wake-word models are preselected; the CUDA pack
; is preselected only when an NVIDIA GPU is detected (see [Code]); the
; command model is disabled until the manifest says it is available.
[Components]
Name: "core";             Description: "Samsara (required)";                                  Types: full minimal custom; Flags: fixed
Name: "wake_word_models"; Description: "Hands-free: wake-word models";                        Types: full custom; ExtraDiskSpaceRequired: {#WakeWordModelsSize}
Name: "cuda_pack";        Description: "GPU acceleration: NVIDIA CUDA pack";                  Types: full; ExtraDiskSpaceRequired: {#CudaPackSize}
Name: "command_model";    Description: "Command model for the hands-free command lane";       ExtraDiskSpaceRequired: {#CommandModelSize}

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: core

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Run]
; The component fetch itself runs from [Code] (CurStepChanged) so its exit
; code can shape the Finished page; this entry only offers to start the app.
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
var
  GpuDetected: Integer;        { 1 = NVIDIA present, 0 = none, -1 = detection failed }
  FetchOutcome: Integer;       { -1 = not run, else the fetcher's exit code }
  FetchRequested: String;

{ Inno component name -> manifest component id (docs/RELEASE_MANIFEST.md). }
function ManifestId(const Name: String): String;
begin
  Result := '';
  if Name = 'core' then Result := 'core';
  if Name = 'cuda_pack' then Result := 'cuda-pack';
  if Name = 'wake_word_models' then Result := 'wake-word-models';
  if Name = 'command_model' then Result := 'command-model';
end;

function ComponentIndex(const Name: String): Integer;
var
  I: Integer;
begin
  Result := -1;
  for I := 0 to WizardForm.ComponentsList.Items.Count - 1 do
    if CompareText(WizardForm.ComponentsList.ItemSubItem[I], Name) = 0 then
      Result := I;
end;

{ NVIDIA present? nvcuda.dll ships with the NVIDIA driver; when that check
  cannot run, a WMI video-controller query is the second opinion. Any
  exception means "could not detect", never "no GPU". }
function DetectNvidia(): Integer;
var
  Locator, Service, Items, Item: Variant;
  I: Integer;
begin
  Result := -1;
  try
    if FileExists(ExpandConstant('{sys}\nvcuda.dll')) then begin
      Result := 1;
      exit;
    end;
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\cimv2');
    Items := Service.ExecQuery('SELECT Name FROM Win32_VideoController');
    Result := 0;
    for I := 0 to Items.Count - 1 do begin
      Item := Items.ItemIndex(I);
      if Pos('NVIDIA', Uppercase(String(Item.Name))) > 0 then
        Result := 1;
    end;
  except
    Result := -1;
  end;
end;

procedure InitializeWizard();
begin
  FetchOutcome := -1;
  FetchRequested := '';
  GpuDetected := DetectNvidia();
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Idx: Integer;
  Msg: String;
begin
  if CurPageID = wpSelectComponents then begin
    { GPU acceleration: disabled WITH the reason visible when no NVIDIA
      device is present; selectable with a neutral note when detection
      itself failed; preselected when a GPU is present. Never hidden. }
    Idx := ComponentIndex('cuda_pack');
    if Idx >= 0 then begin
      if GpuDetected = 0 then begin
        WizardForm.ComponentsList.Checked[Idx] := False;
        WizardForm.ComponentsList.ItemEnabled[Idx] := False;
        WizardForm.ComponentsList.ItemCaption[Idx] := 'GPU acceleration: NVIDIA CUDA pack (No NVIDIA GPU detected)';
      end else if GpuDetected = 1 then begin
        WizardForm.ComponentsList.ItemEnabled[Idx] := True;
        WizardForm.ComponentsList.ItemCaption[Idx] := 'GPU acceleration: NVIDIA CUDA pack (NVIDIA GPU detected)';
        if WizardForm.TypesCombo.ItemIndex = 0 then
          WizardForm.ComponentsList.Checked[Idx] := True;
      end else begin
        WizardForm.ComponentsList.ItemEnabled[Idx] := True;
        WizardForm.ComponentsList.ItemCaption[Idx] := 'GPU acceleration: NVIDIA CUDA pack (Could not detect a GPU; choose it if you have an NVIDIA card)';
      end;
    end;
    { Command model: declared in the manifest, shown disabled with
      "Coming soon" until manifest_defines.iss says it is available. }
    Idx := ComponentIndex('command_model');
    if Idx >= 0 then begin
      #if CommandModelAvailable == 0
      WizardForm.ComponentsList.Checked[Idx] := False;
      WizardForm.ComponentsList.ItemEnabled[Idx] := False;
      WizardForm.ComponentsList.ItemCaption[Idx] := 'Command model for the hands-free command lane (Coming soon)';
      #else
      WizardForm.ComponentsList.ItemEnabled[Idx] := True;
      #endif
    end;
  end;

  if CurPageID = wpFinished then begin
    Msg := WizardForm.FinishedLabel.Caption;
    if FetchRequested = '' then
      Msg := Msg + #13#10#13#10 + 'Optional components can be added any time from Settings.'
    else if FetchOutcome = 0 then
      Msg := Msg + #13#10#13#10 + 'Optional components installed: ' + FetchRequested + '.'
    else if FetchOutcome = 5 then
      Msg := Msg + #13#10#13#10 + 'Component download cancelled. Samsara itself is installed and works; the components can be added later from Settings.'
    else if FetchOutcome = 4 then
      Msg := Msg + #13#10#13#10 + 'The components could not be downloaded (no network). Samsara itself is installed and works; the components can be added later from Settings, or installed from the release ZIPs (docs\INSTALL.md).'
    else
      Msg := Msg + #13#10#13#10 + 'Some components could not be installed (code ' + IntToStr(FetchOutcome) + '). Samsara itself is installed and works; try again later from Settings.';
    WizardForm.FinishedLabel.Caption := Msg;
  end;
end;

{ The ids to fetch, in manifest form, from the checked components. }
function SelectedComponentIds(): String;
var
  I: Integer;
  Id: String;
begin
  Result := '';
  for I := 0 to WizardForm.ComponentsList.Items.Count - 1 do begin
    if WizardForm.ComponentsList.Checked[I] and WizardForm.ComponentsList.ItemEnabled[I] then begin
      Id := ManifestId(WizardForm.ComponentsList.ItemSubItem[I]);
      if (Id <> '') and (Id <> 'core') then begin
        if Result <> '' then Result := Result + ',';
        Result := Result + Id;
      end;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Params, ComponentsDir, ManifestPath: String;
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    if ExpandConstant('{param:NoFetch|0}') <> '0' then exit;
    FetchRequested := SelectedComponentIds();
    if FetchRequested = '' then exit;
    ManifestPath := ExpandConstant('{#ManifestUrl}');
    ComponentsDir := ExpandConstant('{param:ComponentsDir|}');
    if ComponentsDir <> '' then begin
      if FileExists(AddBackslash(ComponentsDir) + 'manifest.json') then
        ManifestPath := AddBackslash(ComponentsDir) + 'manifest.json';
    end;
    Params := '--fetch-components ' + FetchRequested + ' --manifest "' + ManifestPath + '" --progress-window';
    if ComponentsDir <> '' then
      Params := Params + ' --components-dir "' + ComponentsDir + '"';
    Log('Fetching components: ' + Params);
    { waits for the fetcher; its own window shows progress and a Cancel
      button. A non-zero code never fails the install: core is complete. }
    if not Exec(ExpandConstant('{app}\{#AppExeName}'), Params, ExpandConstant('{app}'),
                SW_SHOW, ewWaitUntilTerminated, ResultCode) then
      ResultCode := 6;
    FetchOutcome := ResultCode;
    Log('Component fetch exit code: ' + IntToStr(ResultCode));
  end;
end;
