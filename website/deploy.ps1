[CmdletBinding()]
param(
    [string]$SshHost = "the-arcana",
    [string]$RemoteRoot = "/home/morne/projects/arcana"
)

$ErrorActionPreference = "Stop"
$source = Join-Path $PSScriptRoot "samsara"
$required = @(
    "index.html",
    "docs\index.html",
    "compare\index.html",
    "business\index.html",
    "support\index.html"
)

foreach ($relative in $required) {
    $path = Join-Path $source $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing website file: $path"
    }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$remote = "$RemoteRoot/samsara"
$backup = "$RemoteRoot/backups/samsara-$stamp"

ssh $SshHost "set -e; mkdir -p '$RemoteRoot/backups'; cp -a '$remote' '$backup'"
if ($LASTEXITCODE -ne 0) {
    throw "Remote backup failed; nothing was deployed."
}

scp -r $source "${SshHost}:$RemoteRoot/"
if ($LASTEXITCODE -ne 0) {
    throw "Website upload failed. Restore from $backup if any files changed."
}

Write-Host "Deployed Samsara website. Backup: $backup"
