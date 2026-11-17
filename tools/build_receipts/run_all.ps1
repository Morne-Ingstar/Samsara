param(
    [Parameter(Mandatory=$true)][string]$App,
    [Parameter(Mandatory=$true)][Alias('Home')][string]$ProfilePath,
    [string]$Python = 'F:\envs\sami\python.exe',
    [string]$Report = 'C:\Users\Morne\Documents\Claude\reports\1241'
)
$ErrorActionPreference = 'Stop'
# Runner-only entry point. The worker must never execute this script.
& $Python (Join-Path $PSScriptRoot 'runner.py') --app $App --home $ProfilePath --report $Report
exit $LASTEXITCODE
