$ErrorActionPreference = "Stop"
# v25 selfcheck (task t_27714fcb): data dir default into <InstallDir>\data,
# launcher dual switch (user-data-dir + DESK_AGENT_DATA_DIR), robocopy
# data exclusion, uninstall purge-before-delete, ownership marker rules,
# legacy APPDATA hint, MCP template disabled. Runs on pwsh 7 (Linux) via
# AST extraction, same pattern as v20-v24.

$f = Join-Path $PSScriptRoot "..\install.ps1"

# 1. BOM + ASCII of install.ps1
$bytes = [IO.File]::ReadAllBytes($f)
$bom = ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
$nonAscii = 0
for ($i = 3; $i -lt $bytes.Count; $i++) { if ($bytes[$i] -gt 0x7F) { $nonAscii++ } }
Write-Output "install.ps1 UTF8-BOM: $bom ; non-ASCII bytes after BOM: $nonAscii"

$script:pass = 0; $script:fail = 0
function Assert([bool]$Cond, [string]$Name) {
    if ($Cond) { $script:pass++; Write-Output "PASS: $Name" }
    else { $script:fail++; Write-Output "FAIL: $Name" }
}

# 2. whole-file parse (zero errors required)
$errs = $null; $toks = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f), [ref]$toks, [ref]$errs)
Write-Output "install.ps1 Parser errors: $($errs.Count)"
if ($errs.Count -gt 0) { foreach ($e in $errs) { Write-Output ("  " + $e.Extent.StartLineNumber + ": " + $e.Message) } }
Assert ($errs.Count -eq 0) "install.ps1 parses clean"
Assert ($nonAscii -eq 0) "install.ps1 is pure ASCII (PS 5.1 GBK safety)"
Assert $bom "install.ps1 has UTF-8 BOM"

$full = $ast.Extent.Text

# 3. v25 structural gates
# 3a. early data-dir default resolution right after Confirm-InstallDir
Assert ($full -match [regex]::Escape('$DataDir = Join-Path $InstallDir "data"')) "v25 DataDir default = <InstallDir>\data"
Assert ($full -match [regex]::Escape('$Script:DataDirExplicit = [bool]($DataDir -and $DataDir.Trim())')) "v25 explicit-flag captured (early + reused)"
Assert ($full -match [regex]::Escape('Write-Host ("[install] data dir: {0} (source: {1})"')) "v25 data dir printed in summary"
# 3b. launcher dual switch
Assert ($full -match [regex]::Escape('$userProfileArg = '' --user-data-dir="%~dp0userdata"''')) "v25 launcher --user-data-dir=%~dp0userdata"
Assert ($full -match [regex]::Escape('$envDataLine = ''set "DESK_AGENT_DATA_DIR=''')) "v25 launcher sets DESK_AGENT_DATA_DIR"
Assert ($full -match [regex]::Escape('throw "launcher self-check failed: DESK_AGENT_DATA_DIR line not written verbatim ($bat)"')) "v25 launcher self-check for env line"
# 3c. robocopy data exclusion
Assert ($full -match [regex]::Escape('@("node_modules", ".git", ".install-state.json", "runtime", "codeagent", "data")')) "v25 robocopy /XD source data dir"
Assert ($full -match [regex]::Escape('"sessions.db", "sessions.db-*", "audit.db", "audit.db-*"')) "v25 robocopy /XF db files"
Assert ($full -match [regex]::Escape('"htmlcov", ".cache", "tests", "test", "__pycache__",')) "v21 mirror list still present"
Assert ($full -match [regex]::Escape('.nyc_output", "coverage", "backups", "docs", ".github", ".venv", "data"')) "v25 Get-TreeStats excludes data"
# 3d. uninstall ordering: purge gate text must appear BEFORE the tree delete
$purgeIdx = $full.IndexOf("if (`$PurgeData) {`n        `$purgeOk = Invoke-PurgeData")
$deleteIdx = $full.IndexOf("if (Test-PathSafe `$InstallDir) { Remove-Item -LiteralPath `$InstallDir -Recurse -Force }`n    else")
Assert ($purgeIdx -ge 0) "v25 uninstall runs purge gate"
Assert ($deleteIdx -ge 0) "v25 uninstall tree delete present"
Assert (($purgeIdx -ge 0) -and ($deleteIdx -ge 0) -and ($purgeIdx -lt $deleteIdx)) "v25 purge gate runs BEFORE tree delete"
Assert ($full -match [regex]::Escape('$Script:UninstallPurgeFailed = $true')) "v25 purge failure tracked"
# 3e. legacy APPDATA hint
Assert ($full -match [regex]::Escape('$Script:LegacyDataHint = "legacy"')) "v25 legacy APPDATA detection"
Assert ($full -match 'an old user data directory exists at') "v25 legacy report line"
# 3f. ownership marker: late recompute REMOVED (uses early value)
$lateRecompute = $full -match [regex]::Escape('$Script:DataDirExplicit = [bool]($DataDir -and $DataDir.Trim())' + "`r`n" + '$Script:ResolvedDataDir')
Assert (-not $lateRecompute) "v25 late explicit-recompute removed (early value kept)"
Assert ($full -match [regex]::Escape('$Script:ResolvedDataDir = (Resolve-DataDir $DataDir $InstallDir).DataDir')) "v25 resolved data dir recorded"
# 3g. final report
Assert ($full -match [regex]::Escape('data dir:    $Script:ResolvedDataDir')) "v25 final report shows resolved data dir"
Assert ($full -match [regex]::Escape('(default: <InstallDir>\data -- sidecar logs/sessions.db; Chromium profile in <InstallDir>\userdata)')) "v25 final report default note"

# 4. config.example.json: filesystem MCP disabled
$cfgPath = Join-Path $PSScriptRoot "..\..\config.example.json"
$cfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
Assert ($cfg.mcp.servers.filesystem.enabled -eq $false) "config.example.json filesystem MCP enabled:false"
Assert ($cfg.mcp.servers.'remote-x'.enabled -eq $false) "config.example.json remote-x still enabled:false"

# 5. electron/main.ts: respects inherited DESK_AGENT_DATA_DIR
$mainTs = Join-Path $PSScriptRoot "..\..\electron\main.ts"
$mt = Get-Content -LiteralPath $mainTs -Raw
Assert ($mt -match [regex]::Escape('DESK_AGENT_DATA_DIR: process.env.DESK_AGENT_DATA_DIR || userDataSubdir("data")')) "electron/main.ts respects inherited DESK_AGENT_DATA_DIR"
# compiled output too
$mainJs = Join-Path $PSScriptRoot "..\..\dist\electron\main.js"
$mj = Get-Content -LiteralPath $mainJs -Raw
Assert ($mj -match [regex]::Escape('DESK_AGENT_DATA_DIR: process.env.DESK_AGENT_DATA_DIR || userDataSubdir("data")')) "dist/electron/main.js rebuilt with the fix"

Write-Output ""
Write-Output ("v25 selfcheck: {0} pass, {1} fail" -f $script:pass, $script:fail)
if ($script:fail -gt 0) { exit 1 }
exit 0
