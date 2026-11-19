param(
    [Parameter(Mandatory = $true)][string]$ScratchRoot,
    [string]$App,
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$Report = (Join-Path $PSScriptRoot '..\..\reports\receipts'),
    [Parameter(Mandatory = $true)][string]$Corpus,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
# Runner-only entry point. The worker must never execute this script.
$arguments = @((Join-Path $PSScriptRoot 'daily.py'), '--scratch-root', $ScratchRoot, '--corpus', $Corpus, '--report-root', $Report)
if ($App) { $arguments += @('--app', $App) }
if ($Force) { $arguments += '--force' }
& $Python @arguments
exit $LASTEXITCODE
