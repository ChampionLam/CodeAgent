# bootstrap-venv.ps1 -- one-shot: create the sidecar venv and install every
# library the app's own features need (documents, OCR fallback, images).
#
# Why this exists (user, 2026-09-26): "deploying the project should install the
# libraries that its own features rely on". The requirements files were split
# into "required" and "optional"; nothing ever installed the optional half, so
# PDF/Word/CSV attachments were dead on a fresh machine. This script installs
# both halves in one go.
#
# Usage (from the repo root):
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap-venv.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap-venv.ps1 -Force   # rebuild venv
param(
    [string]$Root = "",
    [switch]$Force,
    # Optional pip index (CN networks: https://pypi.tuna.tsinghua.edu.cn/simple).
    # Passed per-run only; it does not touch the machine's pip config.
    [string]$IndexUrl = ""
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is NOT available while PowerShell evaluates param() defaults
# (learned the hard way on 2026-09-26: Root silently became a relative path and
# every requirements file was "skipped" -- the run looked successful while
# installing nothing). Resolve it here instead.
if (-not $Root) {
    $Root = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path (Join-Path $Root "python"))) {
    throw "[bootstrap] no python/ under resolved root: $Root"
}
Write-Host "[bootstrap] root: $Root"
$venv = Join-Path $Root "python\.venv"
$py = Join-Path $venv "Scripts\python.exe"

if ($Force -and (Test-Path $venv)) {
    Write-Host "[bootstrap] removing existing venv: $venv"
    Remove-Item -Recurse -Force $venv
}

if (-not (Test-Path $py)) {
    $base = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $base) {
        $base = (Get-Command python3 -ErrorAction SilentlyContinue).Source
    }
    if (-not $base) {
        throw "[bootstrap] no python on PATH; install Python 3.10+ first"
    }
    Write-Host "[bootstrap] creating venv with $base"
    & $base -m venv $venv
}

Write-Host "[bootstrap] upgrading pip"
& $py -m pip install --upgrade pip --quiet

foreach ($req in @("python\requirements.txt", "python\requirements-docs.txt")) {
    $path = Join-Path $Root $req
    if (-not (Test-Path $path)) {
        # Fail loud: silently skipping a requirements file is how a deploy ends
        # up "successful" while installing nothing.
        throw "[bootstrap] missing requirements file: $path"
    }
    Write-Host "[bootstrap] installing $req"
    if ($IndexUrl) {
        & $py -m pip install -i $IndexUrl -r $path
    } else {
        & $py -m pip install -r $path
    }
    if ($LASTEXITCODE -ne 0) { throw "[bootstrap] pip failed on $req" }
}

Write-Host "[bootstrap] self-check"
& $py -c @"
import sys
print('  python', sys.version.split()[0])
for mod, label in (('PIL', 'Pillow'), ('anydoc', 'anydoc'), ('fitz', 'pymupdf'),
                   ('pypdf', 'pypdf'), ('openpyxl', 'openpyxl'), ('docx', 'python-docx')):
    try:
        __import__(mod)
        print('  OK   ', label)
    except Exception as exc:
        print('  MISS ', label, '->', type(exc).__name__)
"@
Write-Host "[bootstrap] done"
