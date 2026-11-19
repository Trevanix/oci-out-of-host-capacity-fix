# Starts the launcher in the background (no console window; it survives closing this window).
#   powershell -ExecutionPolicy Bypass -File start_windows.ps1                    # launch now
#   powershell -ExecutionPolicy Bypass -File start_windows.ps1 resize             # grow A1 to 4/24
#   powershell -ExecutionPolicy Bypass -File start_windows.ps1 autostart          # launch now + every time you log in
#   powershell -ExecutionPolicy Bypass -File start_windows.ps1 remove-autostart   # undo autostart (do it after success)
# Progress: tray icon (green = OK, yellow = problem) and launch_instance.log. Stop: Stop-Process -Id <the PID printed below>
param([ValidateSet("launch", "resize", "autostart", "remove-autostart")][string]$Command = "launch")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$TaskName = "OCI free instance launcher"
$TrayTask = "OCI free instance launcher tray"
$TrayArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSScriptRoot\tray_windows.ps1`""

function Start-Tray {  # the tray script exits by itself if an icon is already showing
    Start-Process powershell.exe -ArgumentList $TrayArgs -WindowStyle Hidden
}

if ($Command -eq "remove-autostart") {
    foreach ($t in $TaskName, $TrayTask) { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host "Autostart removed. A launcher that is already running keeps running until it succeeds or you stop it."
    exit 0
}

if ($Command -in "launch", "autostart") {
    & .venv\Scripts\python.exe main.py check
    if ($LASTEXITCODE -ne 0) { Write-Host "Fix the problems above first."; exit 1 }
}

if ($Command -eq "autostart") {
    # A Windows scheduled task survives reboots and closed windows; one copy at a time.
    $action = New-ScheduledTaskAction -Execute "$PSScriptRoot\.venv\Scripts\pythonw.exe" -Argument "main.py launch" -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Description "Retries the Oracle Always Free instance until it is created. Remove after success: start_windows.ps1 remove-autostart" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    $trayAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $TrayArgs -WorkingDirectory $PSScriptRoot
    Register-ScheduledTask -TaskName $TrayTask -Description "Tray icon for the OCI launcher" -Action $trayAction -Trigger $trigger -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-ScheduledTask -TaskName $TrayTask
    Write-Host "Running now and at every log-in (task '$TaskName'). A pop-up will appear when it succeeds."
    Write-Host "After success run:  powershell -ExecutionPolicy Bypass -File start_windows.ps1 remove-autostart"
    Write-Host "Follow it with:  Get-Content launch_instance.log -Wait -Tail 5"
    exit 0
}

$p = Start-Process .venv\Scripts\pythonw.exe -ArgumentList "main.py", $Command -WorkingDirectory $PSScriptRoot -PassThru
Start-Tray
Write-Host "Running in the background, PID $($p.Id). A pop-up will appear when it succeeds."
Write-Host "Follow it with:  Get-Content launch_instance.log -Wait -Tail 5"
