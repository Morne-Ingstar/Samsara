<#
.SYNOPSIS
  Authenticode-sign files with Azure Artifact Signing, then verify each one.

.DESCRIPTION
  Usage:  powershell -File scripts\sign.ps1 <file> [<file> ...]

  Signs with signtool + the Azure.CodeSigning.Dlib using the az CLI login
  (no secrets are read or stored here), then runs `signtool verify /pa` on
  every file. Exits non-zero if any signing or verification step fails.

  Only sign executables Samsara itself builds. Never sign third-party
  DLL/PYD files: the Basic Artifact Signing plan is 5,000 signatures/month.

  Overrides (parameter or environment variable):
    -SignTool / SAMSARA_SIGNTOOL        signtool.exe
    -Dlib     / SAMSARA_SIGN_DLIB       Azure.CodeSigning.Dlib.dll (x64)
    -Metadata / SAMSARA_SIGN_METADATA   metadata.json (Endpoint,
                                        CodeSigningAccountName,
                                        CertificateProfileName)
    -Timestamp / SAMSARA_SIGN_TIMESTAMP RFC 3161 timestamp server
#>
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)][string[]]$Files,
    [string]$SignTool = $(if ($env:SAMSARA_SIGNTOOL) { $env:SAMSARA_SIGNTOOL } else { 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe' }),
    [string]$Dlib = $(if ($env:SAMSARA_SIGN_DLIB) { $env:SAMSARA_SIGN_DLIB } else { Join-Path $env:LOCALAPPDATA 'Microsoft\MicrosoftTrustedSigningClientTools\Azure.CodeSigning.Dlib.dll' }),
    [string]$Metadata = $(if ($env:SAMSARA_SIGN_METADATA) { $env:SAMSARA_SIGN_METADATA } else { Join-Path $env:USERPROFILE 'signing\metadata.json' }),
    [string]$Timestamp = $(if ($env:SAMSARA_SIGN_TIMESTAMP) { $env:SAMSARA_SIGN_TIMESTAMP } else { 'http://timestamp.acs.microsoft.com' })
)
$ErrorActionPreference = 'Stop'

if (-not $Files -or $Files.Count -eq 0) { Write-Error 'sign.ps1: no files given'; exit 2 }
foreach ($need in @($SignTool, $Dlib, $Metadata)) {
    if (-not (Test-Path -LiteralPath $need)) { Write-Error "sign.ps1: required file not found: $need"; exit 2 }
}
$resolved = @()
foreach ($f in $Files) {
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { Write-Error "sign.ps1: file not found: $f"; exit 2 }
    $resolved += (Resolve-Path -LiteralPath $f).Path
}

& $SignTool sign /v /fd SHA256 /tr $Timestamp /td SHA256 /dlib $Dlib /dmdf $Metadata @resolved
if ($LASTEXITCODE -ne 0) { Write-Error "sign.ps1: signtool sign failed (exit $LASTEXITCODE)"; exit 1 }

foreach ($f in $resolved) {
    & $SignTool verify /pa $f
    if ($LASTEXITCODE -ne 0) { Write-Error "sign.ps1: signtool verify failed for $f (exit $LASTEXITCODE)"; exit 1 }
}
Write-Host "sign.ps1: signed and verified $($resolved.Count) file(s)"
exit 0
