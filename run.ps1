# run.ps1 — Windows equivalent of run.sh for local testing
param(
    [Parameter(Mandatory=$true)][string]$InputDir,
    [Parameter(Mandatory=$true)][string]$OutputPath
)
python "$PSScriptRoot\solution.py" $InputDir $OutputPath
