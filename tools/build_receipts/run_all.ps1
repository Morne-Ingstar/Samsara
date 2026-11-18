param(
    [string]$App = 'D:\samsara_tmp1241\app',
    [string]$Python = 'F:\envs\sami\python.exe',
    [string]$Report = (Join-Path $PSScriptRoot '..\..\reports\receipts'),
    [string]$Corpus = 'D:\SamsaraCorpus',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
# Runner-only entry point. The worker must never execute this script.
$arguments = @((Join-Path $PSScriptRoot 'daily.py'), '--app', $App, '--corpus', $Corpus, '--report-root', $Report)
if ($Force) { $arguments += '--force' }
& $Python @arguments
exit $LASTEXITCODE
