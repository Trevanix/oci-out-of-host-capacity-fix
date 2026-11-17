# Starts the launcher in the background (no console window; it survives closing this window).
#   powershell -ExecutionPolicy Bypass -File start_windows.ps1            # launch
#   powershell -ExecutionPolicy Bypass -File start_windows.ps1 resize     # grow A1 to 4/24
# Progress: launch_instance.log. Stop: Stop-Process -Id <the PID printed below>
param([ValidateSet("launch", "resize")][string]$Command = "launch")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($Command -eq "launch") {
    & .venv\Scripts\python.exe main.py check
    if ($LASTEXITCODE -ne 0) { Write-Host "Fix the problems above first."; exit 1 }
}

$p = Start-Process .venv\Scripts\pythonw.exe -ArgumentList "main.py", $Command -WorkingDirectory $PSScriptRoot -PassThru
Write-Host "Running in the background, PID $($p.Id). A pop-up will appear when it succeeds."
Write-Host "Follow it with:  Get-Content launch_instance.log -Wait -Tail 5"
