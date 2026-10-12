# Registers a Windows scheduled task that starts Atlas PAPER trading when you log in.
# Remove it again with:  Unregister-ScheduledTask -TaskName "Atlas paper trading" -Confirm:$false
# Stop a running session cleanly by creating the file data\STOP_ATLAS (delete it to allow the next start).
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script = Join-Path $repo "scripts\windows\start_atlas_paper.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Days 7)
Register-ScheduledTask -TaskName "Atlas paper trading" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Atlas PAPER trading (simulated fills only; no real orders)" -Force | Out-Null
Write-Output "Registered 'Atlas paper trading' to start at logon for $env:USERNAME."
