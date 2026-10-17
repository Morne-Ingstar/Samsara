# Samsara Dev - one-time setup for a test PC that runs the newest master from source.
# Run in PowerShell:
#   irm https://raw.githubusercontent.com/Morne-Ingstar/Samsara/master/tools/laptop_dev_setup.ps1 | iex
# Creates %USERPROFILE%\SamsaraDev (code + venv) and a "Samsara Dev" desktop shortcut.
# Each launch pulls the latest master, updates packages if requirements changed, closes the
# installed Samsara, and runs from source with its own settings folder (%USERPROFILE%\.samsara-dev),
# so the installed beta's settings are left alone.
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:USERPROFILE "SamsaraDev"
$Repo = Join-Path $Root "Samsara"
$Venv = Join-Path $Root "venv"
$Home2 = Join-Path $env:USERPROFILE ".samsara-dev"

function Need($cmd, $wingetId) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "Installing $wingetId ..."
        winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements | Out-Host
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    }
}
Need git "Git.Git"
$py = $null
try { $py = (& py -3.11 -c "import sys;print(sys.executable)") 2>$null } catch {}
if (-not $py) {
    Write-Host "Installing Python 3.11 ..."
    winget install --id Python.Python.3.11 -e --accept-source-agreements --accept-package-agreements | Out-Host
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $py = (& py -3.11 -c "import sys;print(sys.executable)")
}
New-Item -ItemType Directory -Force $Root, $Home2 | Out-Null
if (-not (Test-Path (Join-Path $Repo ".git"))) {
    git clone https://github.com/Morne-Ingstar/Samsara.git $Repo
}
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) { & $py -m venv $Venv }
& (Join-Path $Venv "Scripts\python.exe") -m pip install --upgrade pip -q
& (Join-Path $Venv "Scripts\python.exe") -m pip install -r (Join-Path $Repo "requirements.txt")
Copy-Item (Join-Path $Repo "requirements.txt") (Join-Path $Root "requirements.installed") -Force

# First run: copy the installed beta's settings so the dev copy starts configured.
$Installed = Join-Path $env:USERPROFILE ".samsara"
if ((Test-Path $Installed) -and -not (Get-ChildItem $Home2 -Force | Select-Object -First 1)) {
    Copy-Item (Join-Path $Installed "*") $Home2 -Recurse -Force -ErrorAction SilentlyContinue
}

$Launcher = Join-Path $Root "Samsara Dev.cmd"
@"
@echo off
title Samsara Dev
cd /d "$Repo"
echo Updating to the newest master...
git fetch -q origin && git reset -q --hard origin/master
git log -1 --format="Running %%h  %%s  (%%cr)"
fc /b requirements.txt "$Root\requirements.installed" >nul 2>&1 || (
  echo Packages changed - updating...
  "$Venv\Scripts\python.exe" -m pip install -q -r requirements.txt && copy /y requirements.txt "$Root\requirements.installed" >nul
)
taskkill /im Samsara.exe /f >nul 2>&1
set SAMSARA_HOME_DIR=$Home2
"$Venv\Scripts\python.exe" dictation.py %*
if errorlevel 1 pause
"@ | Set-Content -Encoding ASCII $Launcher

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Samsara Dev.lnk"))
$lnk.TargetPath = $Launcher
$lnk.WorkingDirectory = $Repo
$ico = Join-Path $Repo "assets\icon\samsara.ico"
if (Test-Path $ico) { $lnk.IconLocation = $ico }
$lnk.Save()
Write-Host "Done. Double-click 'Samsara Dev' on the desktop. It always runs the newest master."
