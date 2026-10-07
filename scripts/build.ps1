<#
.SYNOPSIS
  Builds AnkerClient: tests (optional), PyInstaller one-folder app, portable zip and (if Inno Setup is installed) the installer.

.EXAMPLE
  ./scripts/build.ps1            # full build
  ./scripts/build.ps1 -SkipTests # build without running the test suite
#>
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (python -c "import anker_client; print(anker_client.__version__)").Trim()
Write-Host "Building AnkerClient $version" -ForegroundColor Cyan

if (-not $SkipTests) {
    Write-Host "Running lint and tests..." -ForegroundColor Cyan
    python -m ruff check anker_client tests
    if ($LASTEXITCODE -ne 0) { throw "ruff failed" }
    python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
}

python packaging/make_version_info.py
if ($LASTEXITCODE -ne 0) { throw "version info generation failed" }

python -m PyInstaller packaging/ankerclient.spec --noconfirm --clean --distpath dist --workpath build/pyinstaller
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $root "dist/AnkerClient/AnkerClient.exe"
if (-not (Test-Path $exe)) { throw "Missing $exe" }
if (-not (Test-Path (Join-Path $root "dist/AnkerClient/_internal/PyQt6/Qt6/bin/QtWebEngineProcess.exe"))) {
    Write-Warning "QtWebEngineProcess.exe not found in the bundle - browser verification will be unavailable."
}

$zip = Join-Path $root "dist/AnkerClient-$version-portable.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $root "dist/AnkerClient") -DestinationPath $zip
Write-Host "Portable build: $zip" -ForegroundColor Green

if (-not $SkipInstaller) {
    $iscc = Get-Command iscc -ErrorAction SilentlyContinue
    if (-not $iscc) {
        $candidate = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
        if (Test-Path $candidate) { $iscc = Get-Item $candidate }
    }
    if ($iscc) {
        & $iscc.Source "/DAppVersion=$version" "packaging/installer.iss"
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
        Write-Host "Installer: dist/AnkerClient-$version-setup.exe" -ForegroundColor Green
    } else {
        Write-Warning "Inno Setup (iscc) not found - skipping installer. Install it from https://jrsoftware.org/isinfo.php"
    }
}
