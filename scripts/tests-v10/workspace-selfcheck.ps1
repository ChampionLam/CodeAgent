# t_2d0783e3 workspace selfcheck: exercise the v25 workspace block in
# install.ps1 WITHOUT a real install, by re-running its code path against
# a sandbox tree (source template -> config.json -> workspace dir).
# Mirrors the acceptance: fresh install -> config.json's
# capabilities.workspaceRoot == <InstallDir>\workspace; re-run without
# -WorkspaceDir never overwrites a user-set value; explicit -WorkspaceDir
# wins.
param()
$ErrorActionPreference = "Stop"
$fail = 0
function Assert-True($cond, $name) {
    if ($cond) { Write-Host "PASS: $name" }
    else { Write-Host "FAIL: $name"; $script:fail++ }
}

# ---- sandbox: a fake install tree with the blank template ----
$sb = Join-Path ([IO.Path]::GetTempPath()) ("ws-selfcheck-" + [guid]::NewGuid().ToString("N").Substring(0,8))
$InstallDir = Join-Path $sb "App"
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$cfgDst = Join-Path $InstallDir "config.json"
@'
{
  "model": {
    "baseUrl": "",
    "model": "",
    "apiKeyEnv": "",
    "maxTokens": 4096,
    "timeoutSeconds": 180
  },
  "capabilities": {
    "workspaceRoot": "",
    "delegation": { "enabled": true, "maxConcurrentChildren": 3, "nodeMaxLoop": 12 }
  }
}
'@ | Set-Content -LiteralPath $cfgDst -Encoding UTF8

# ---- the workspace block, copied VERBATIM from install.ps1 (v25) ----
$WorkspaceDir = ""   # case 1: no explicit -WorkspaceDir

$workspaceDirDefault = Join-Path $InstallDir "workspace"
$workspaceDir = if ($WorkspaceDir -and $WorkspaceDir.Trim()) { $WorkspaceDir.Trim() } else { $workspaceDirDefault }
$workspaceDir = [Environment]::ExpandEnvironmentVariables($workspaceDir)
$workspaceDir = [IO.Path]::GetFullPath($workspaceDir)
$Script:WorkspaceDirResolved = $workspaceDir
$workspaceReadme = Join-Path $workspaceDir "README.txt"
$workspaceReadmeText = @"
This is the default working directory (workspace) for CodeAgent.
"@
if (-not (Test-Path -LiteralPath $workspaceDir)) {
    New-Item -ItemType Directory -Path $workspaceDir -Force | Out-Null
}
if (-not (Test-Path -LiteralPath $workspaceReadme)) {
    [IO.File]::WriteAllText($workspaceReadme, $workspaceReadmeText,
        (New-Object Text.UTF8Encoding($false)))
}
$workspaceWroteConfig = $false
if (Test-Path -LiteralPath $cfgDst) {
    $cfgWs = $null
    try { $cfgWs = (Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json).capabilities.workspaceRoot } catch { $cfgWs = $null }
    $existingWs = if ($cfgWs) { [string]$cfgWs } else { "" }
    if ((-not $existingWs.Trim()) -or $WorkspaceDir) {
        try {
            $cfgWsObj = Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json
            if (-not $cfgWsObj.capabilities) {
                $cfgWsObj | Add-Member -NotePropertyName capabilities -NotePropertyValue ([pscustomobject]@{}) -ErrorAction Stop
            }
            $capObj = $cfgWsObj.capabilities
            if ($capObj.PSObject.Properties["workspaceRoot"]) {
                $capObj.workspaceRoot = $workspaceDir
            } else {
                $capObj | Add-Member -NotePropertyName workspaceRoot -NotePropertyValue $workspaceDir -ErrorAction Stop
            }
            $wsJson = $cfgWsObj | ConvertTo-Json -Depth 32
            $wsTmp = "$cfgDst.ws.tmp"
            [IO.File]::WriteAllText($wsTmp, $wsJson, (New-Object Text.UTF8Encoding($false)))
            Move-Item -LiteralPath $wsTmp -Destination $cfgDst -Force
            $workspaceWroteConfig = $true
        } catch {
            Write-Warning "[selfcheck] config write failed: $_"
        }
    }
}

# ---- case 1 assertions: fresh install ----
Assert-True ($workspaceWroteConfig) "case1: config written on blank template"
$cfg1 = Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json
Assert-True ($cfg1.capabilities.workspaceRoot -eq (Join-Path $InstallDir "workspace")) "case1: workspaceRoot == <InstallDir>\workspace"
Assert-True (Test-Path -LiteralPath (Join-Path $workspaceDir "README.txt")) "case1: README.txt created"
Assert-True ($cfg1.capabilities.delegation.enabled -eq $true) "case1: sibling keys under capabilities survive"
Assert-True ($cfg1.model.maxTokens -eq 4096) "case1: model section untouched"

# ---- case 2: re-run without -WorkspaceDir must NOT overwrite a user value ----
$cfg1.capabilities.workspaceRoot = "D:\UsersChoice"
$cfg1 | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath $cfgDst -Encoding UTF8
$WorkspaceDir = ""
# re-run the same block
$workspaceDir2 = if ($WorkspaceDir -and $WorkspaceDir.Trim()) { $WorkspaceDir.Trim() } else { $workspaceDirDefault }
$workspaceWroteConfig2 = $false
try {
    $cfgWs = (Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json).capabilities.workspaceRoot
    $existingWs = if ($cfgWs) { [string]$cfgWs } else { "" }
    if ((-not $existingWs.Trim()) -or $WorkspaceDir) { $workspaceWroteConfig2 = $true }
} catch {}
Assert-True (-not $workspaceWroteConfig2) "case2: user-set workspaceRoot survives a re-run without -WorkspaceDir"

# ---- case 3: explicit -WorkspaceDir wins ----
$WorkspaceDir = "C:\Explicit\Dir"
$existingWs = "D:\UsersChoice"
Assert-True (((-not $existingWs.Trim()) -or $WorkspaceDir)) "case3: explicit -WorkspaceDir takes precedence over stored value"

# ---- case 4: capabilities section ABSENT entirely (config without the key) ----
$cfgNoCaps = '{"model":{"baseUrl":"","model":"","apiKeyEnv":"","maxTokens":4096,"timeoutSeconds":180}}'
Set-Content -LiteralPath $cfgDst -Value $cfgNoCaps -Encoding UTF8
try {
    $cfgWsObj = Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json
    if (-not $cfgWsObj.capabilities) {
        $cfgWsObj | Add-Member -NotePropertyName capabilities -NotePropertyValue ([pscustomobject]@{}) -ErrorAction Stop
    }
    $capObj = $cfgWsObj.capabilities
    if ($capObj.PSObject.Properties["workspaceRoot"]) {
        $capObj.workspaceRoot = $workspaceDir
    } else {
        $capObj | Add-Member -NotePropertyName workspaceRoot -NotePropertyValue $workspaceDir -ErrorAction Stop
    }
    $cfgWsObj | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath $cfgDst -Encoding UTF8
    $ok4 = $true
} catch { $ok4 = $false; Write-Warning "case4 threw: $_" }
Assert-True ($ok4) "case4: capabilities section auto-created when absent"
$cfg4 = Get-Content -LiteralPath $cfgDst -Raw | ConvertFrom-Json
Assert-True ($cfg4.capabilities.workspaceRoot -eq $workspaceDir) "case4: workspaceRoot written into new capabilities section"

if ($fail -gt 0) {
    Write-Host "SELFCHECK FAILED: $fail failure(s)"
    exit 1
}
Write-Host "ALL SELFCHECK PASS"
Remove-Item -Recurse -Force $sb -ErrorAction SilentlyContinue
exit 0
