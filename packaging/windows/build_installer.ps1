# Build UniqLab for Windows x64: PyInstaller onedir + Inno Setup per-user installer.
# Usage (repository root, inside the build virtualenv):  powershell -File packaging\windows\build_installer.ps1
# Requires Inno Setup 6 (iscc.exe) on PATH or in its default per-user/machine location.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root
$Version = (python -c "import uniqlab; print(uniqlab.__version__)").Trim()

if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist) { Remove-Item dist -Recurse -Force }
pyinstaller --noconfirm --clean packaging\uniqlab.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
    foreach ($candidate in @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $candidate) { $Iscc = $candidate; break }
    }
}
if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) not found" }
& $Iscc "/DAppVersion=$Version" packaging\windows\uniqlab.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$Setup = "dist\UniqLab-$Version-windows-x64.exe"
$Hash = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLower()
# LF line ending, so `sha256sum -c` on Linux/macOS reads the file name without a trailing CR.
[System.IO.File]::WriteAllText((Join-Path (Get-Location) "$Setup.sha256"), "$Hash  $(Split-Path $Setup -Leaf)`n",
    [System.Text.Encoding]::ASCII)
Write-Host "Built $Setup"
