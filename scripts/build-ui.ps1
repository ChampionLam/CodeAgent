# Build the UI and stage its output where the app loads it (electron/ui/).
# Also writes a blank .env / config.json if they do not exist yet.
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\build-ui.ps1
#
# Why this script exists: the README's one-liner (rm -rf / mkdir -p / cp -r /
# printf) does not run in PowerShell, and PowerShell 5.1's `>` operator writes
# UTF-16 -- an .env written that way cannot be read by the app. So .env is
# written here as UTF-8 **without BOM**.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[build-ui] npm install (ui/)"
npm --prefix ui install
if ($LASTEXITCODE -ne 0) { throw "ui npm install failed" }

Write-Host "[build-ui] npm run build (ui/)  -- tsc -b && vite build"
npm --prefix ui run build
if ($LASTEXITCODE -ne 0) { throw "ui build failed" }

$dest = Join-Path $root "electron\ui"
Write-Host "[build-ui] staging ui/dist -> electron/ui"
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
New-Item -ItemType Directory -Path $dest | Out-Null
Copy-Item (Join-Path $root "ui\dist\*") $dest -Recurse -Force

$envPath = Join-Path $root ".env"
if (Test-Path $envPath) {
    Write-Host "[build-ui] .env already exists -- left untouched"
} else {
    $enc = New-Object System.Text.UTF8Encoding($false)   # no BOM
    [System.IO.File]::WriteAllText($envPath, "OPENAI_API_KEY=...`n", $enc)
    Write-Host "[build-ui] wrote a blank .env (UTF-8, no BOM) -- fill in your key"
}

$cfg = Join-Path $root "config.json"
if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $root "config.example.json") $cfg
    Write-Host "[build-ui] copied config.example.json -> config.json"
} else {
    Write-Host "[build-ui] config.json already exists -- left untouched"
}

Write-Host "[build-ui] done. Next: npm start"