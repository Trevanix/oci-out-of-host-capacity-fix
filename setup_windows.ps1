# Windows setup: creates .venv with Python 3.12 (3.13/3.11 also work), installs the requirements
# and runs the setup wizard. Run from this folder:
#   powershell -ExecutionPolicy Bypass -File setup_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Newer Pythons (3.14+) have no prebuilt wheels yet for some oci dependencies (crc32c, python-pkcs11).
$version = $null
foreach ($v in "3.12", "3.13", "3.11") {
    & py "-$v" -c "pass" 2>$null
    if ($LASTEXITCODE -eq 0) { $version = $v; break }
}
if (-not $version) {
    Write-Host "Python 3.12 not found. Install it from https://www.python.org/downloads/ (tick 'py launcher') and run this again."
    exit 1
}

if (-not (Test-Path .venv)) { & py "-$version" -m venv .venv }
& .venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit 1 }

& .venv\Scripts\python.exe setup_wizard.py
