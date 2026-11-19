# Tray icon that shows whether the launcher is working. It only watches; it never starts or stops the launcher.
#   green  = running and trying ("Out of host capacity" is normal)
#   yellow = not running, no attempt for a while (PC asleep?), or stopped with an error
#   green check = instance created
# Start: powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File tray_windows.ps1   (start_windows.ps1 does it for you)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Global\OciFreeLauncherTray", [ref]$created)
if (-not $created) { exit 0 }  # one icon is enough

function New-DotIcon([System.Drawing.Color]$color, [bool]$check) {
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.FillEllipse((New-Object System.Drawing.SolidBrush $color), 2, 2, 28, 28)
    $g.DrawEllipse((New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(90, 0, 0, 0)), 2), 2, 2, 28, 28)
    if ($check) {
        $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), 4
        $g.DrawLines($pen, [System.Drawing.Point[]]@((New-Object System.Drawing.Point 9, 16), (New-Object System.Drawing.Point 14, 21), (New-Object System.Drawing.Point 23, 11)))
    }
    $g.Dispose()
    [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$icons = @{
    ok     = New-DotIcon ([System.Drawing.Color]::FromArgb(46, 184, 92)) $false
    warn   = New-DotIcon ([System.Drawing.Color]::FromArgb(240, 180, 20)) $false
    done   = New-DotIcon ([System.Drawing.Color]::FromArgb(46, 184, 92)) $true
}

function Get-Status {
    if (Test-Path INSTANCE_CREATED) { return @("done", "OCI launcher: instance created!") }
    $proc = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match 'main\.py (launch|resize)' } | Select-Object -First 1
    $started = if ($proc) { $proc.CreationDate } else { [datetime]::MinValue }
    foreach ($f in "UNHANDLED_ERROR.log", "ERROR_IN_CONFIG.log") {
        if ((Test-Path $f) -and (Get-Item $f).LastWriteTime -ge $started) { return @("warn", "OCI launcher stopped with an error ($f)") }
    }
    if (-not $proc) { return @("warn", "OCI launcher is not running") }
    $last = Select-String -Path launch_instance.log -Pattern "launch_instance--" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if (-not $last) { return @("ok", "OCI launcher: starting...") }
    $when = [datetime]::ParseExact($last.Line.Substring(0, 19), "yyyy-MM-dd HH:mm:ss", $null)
    $mins = [int]((Get-Date) - $when).TotalMinutes
    if ($mins -ge 6) { return @("warn", "OCI launcher: no attempt for $mins min (PC asleep?)") }
    $count = (Select-String -Path launch_instance.log -Pattern "launch_instance--" | Where-Object { $_.Line.Substring(0, 19) -ge $started.ToString("yyyy-MM-dd HH:mm:ss") }).Count
    return @("ok", "OCI launcher: trying ($count attempts), all OK")
}

$tray = New-Object System.Windows.Forms.NotifyIcon
$menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$menu.Items.Add("Open log", $null, { Start-Process notepad.exe (Join-Path $PSScriptRoot "launch_instance.log") })
[void]$menu.Items.Add("Open folder", $null, { Start-Process explorer.exe $PSScriptRoot })
[void]$menu.Items.Add("Hide this icon", $null, { $tray.Visible = $false; [System.Windows.Forms.Application]::Exit() })
$tray.ContextMenuStrip = $menu
$tray.Add_DoubleClick({ Start-Process notepad.exe (Join-Path $PSScriptRoot "launch_instance.log") })

$update = {
    try { $state, $text = Get-Status } catch { $state, $text = "warn", "OCI launcher: cannot read status" }
    $tray.Icon = $icons[$state]
    $tray.Text = $text.Substring(0, [Math]::Min(63, $text.Length))  # Windows limit for tray tooltips
}
& $update
$tray.Visible = $true
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 20000
$timer.Add_Tick($update)
$timer.Start()
[System.Windows.Forms.Application]::Run()
$tray.Dispose()
