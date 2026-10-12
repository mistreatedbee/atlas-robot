# Starts Atlas PAPER trading under the supervisor (restarts after crashes, heartbeat in logs\heartbeat.json).
# Simulated fills only: nothing here can place a real order. Run from the repository folder.
# Requires: MT5 running and logged in (investor password recommended), `pip install -e ".[dev,mt5]"` done.
param(
    [string]$Experiment = "experiments\baseline_random_entry_sample.toml",
    [double]$SessionMinutes = 240
)
$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$atlas = Join-Path (Get-Location) ".venv\Scripts\atlas.exe"
if (-not (Test-Path $atlas)) { throw "Atlas is not installed in .venv (see docs/14_vault_mt5_integration.md)" }
& $atlas supervise --experiment $Experiment --session-minutes $SessionMinutes --equity-from-account
exit $LASTEXITCODE
