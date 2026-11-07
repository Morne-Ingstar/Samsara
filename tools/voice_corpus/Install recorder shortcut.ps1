# Runner setup only: creates one Desktop CMD, never launches an app.
param([string]$DesktopDirectory = [Environment]::GetFolderPath('Desktop'))
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'Record voice corpus.cmd'
if (-not (Test-Path -LiteralPath $launcher)) { throw 'Recorder launcher is missing' }
$target = Join-Path $DesktopDirectory 'Record voice corpus.cmd'
if ((Test-Path -LiteralPath $target) -or
    (Test-Path -LiteralPath (Join-Path $DesktopDirectory 'Record voice corpus.lnk'))) {
    throw 'A recorder launcher already exists; preserve and inspect it'
}
if (-not (Test-Path -LiteralPath $DesktopDirectory -PathType Container)) { throw 'Desktop folder is missing' }
$content = "@echo off`r`ncall `"$launcher`"`r`n"
Set-Content -LiteralPath $target -Value $content -Encoding Ascii -NoNewline
Write-Output 'Record voice corpus.cmd created on the Desktop.'
