# Installs the two Windows-side pieces for the NHL dashboard, mirroring how
# the FF and Stock dashboards are wired. Run once from a normal (non-admin)
# PowerShell window as the user who owns the WSL distro:
#
#   powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\krg77\nhl-dashboard\windows\install_windows_tasks.ps1
#
# 1. Scheduled Task "NHL Dashboard Pipeline": runs run_pipeline.sh at :40
#    every hour from 8:40 AM to 9:40 PM. Hourly (not every 2h like the
#    football/stock tasks) because the NHL shows publish at different times
#    through the day - PuckTime ~noon, Sick Hockey Picks mid/late afternoon,
#    Hockey Gambling Podcast late evening - and a run with nothing new costs
#    only a few seconds of polling. :40 keeps it off the FF (:00) and Stock
#    (:20) slots. MultipleInstances=IgnoreNew + the flock in run_pipeline.sh
#    mean overlapping slots are silently dropped, never doubled up.
# 2. A Startup-folder launcher that brings the web server (port 8002) back
#    after a reboot, hidden, via the idempotent start_server.sh.

$ErrorActionPreference = 'Stop'

$taskName = 'NHL Dashboard Pipeline'
$action = New-ScheduledTaskAction -Execute 'wsl.exe' `
    -Argument '-d Ubuntu -e /home/krg77/nhl-dashboard/run_pipeline.sh'
$trigger = New-ScheduledTaskTrigger -Daily -At '8:40AM'
$repeat = New-ScheduledTaskTrigger -Once -At '8:40AM' `
    -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 13 -Minutes 1)
$trigger.Repetition = $repeat.Repetition
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 6)

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "Task '$taskName' already exists - leaving it alone."
} else {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Description 'NHL betting podcast pipeline (poll feeds, transcribe, extract bets)' | Out-Null
    Write-Host "Created scheduled task '$taskName'."
}

$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\start_nhl_dashboard.vbs'
$vbs = @'
' Starts the NHL dashboard's web server at Windows logon, hidden (no console
' window). The actual startup logic lives in start_server.sh, checked into
' the nhl-dashboard repo - this file is just a thin, hidden launcher for it.
CreateObject("WScript.Shell").Run "wsl.exe -d Ubuntu -e /home/krg77/nhl-dashboard/start_server.sh", 0, False
'@
Set-Content -Path $startup -Value $vbs -Encoding ASCII
Write-Host "Wrote startup launcher: $startup"
