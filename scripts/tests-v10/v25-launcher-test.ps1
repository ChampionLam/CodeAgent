$ErrorActionPreference = "Stop"
# v25 launcher-line BEHAVIOR test (t_27714fcb): extract nothing -- the
# line-construction logic is plain string building; re-execute the exact
# expressions from install.ps1 for both cases and verify the resulting
# .bat content, incl. the PS 5.1 array-split hazard (complete lines).
$script:pass = 0; $script:fail = 0
function Assert([bool]$Cond, [string]$Name) {
    if ($Cond) { $script:pass++; Write-Output "PASS: $Name" }
    else { $script:fail++; Write-Output "FAIL: $Name" }
}

# --- case 1: DEFAULT (no -DataDir) ---
$DataDir = "C:\Apps\CodeAgent\data"
$Script:DataDirExplicit = $false
$resolvedData = [Environment]::ExpandEnvironmentVariables($DataDir)
$userProfileArg = ' --user-data-dir="%~dp0userdata"'
if ($Script:DataDirExplicit) {
    $userProfileArg = ' --user-data-dir="' + $resolvedData + '"'
}
$envDataLine = 'set "DESK_AGENT_DATA_DIR=' + $resolvedData + '"'
$pathSetup = 'set "PATH=%~dp0runtime\node;%PATH%"'
$electronLine = '  "%~dp0node_modules\electron\dist\electron.exe" "%~dp0."' + $userProfileArg
$npmLine = '  call npm start --' + $userProfileArg
$lines = @(
    "@echo off",
    'cd /d "%~dp0"',
    $pathSetup,
    $envDataLine,
    'if exist "%~dp0node_modules\electron\dist\electron.exe" (',
    $electronLine,
    ') else (',
    $npmLine,
    ')'
)
$bat = Join-Path ([IO.Path]::GetTempPath()) ("v25bat-" + [Guid]::NewGuid().ToString("N") + ".bat")
[IO.File]::WriteAllLines($bat, $lines, (New-Object Text.UTF8Encoding($false)))
$written = @(Get-Content -LiteralPath $bat)
Assert ($written -contains $electronLine) "default: electron line written verbatim"
Assert ($written -contains $envDataLine) "default: DESK_AGENT_DATA_DIR line written verbatim"
Assert ($written[3] -eq $envDataLine) "default: env line at fixed position (before the branch)"
Assert ($envDataLine -eq 'set "DESK_AGENT_DATA_DIR=C:\Apps\CodeAgent\data"') "default: env var points at <InstallDir>\data"
Assert ($electronLine -match '%~dp0userdata') "default: electron --user-data-dir=%~dp0userdata"
Assert ($electronLine -notmatch '\\data"') "default: --user-data-dir does NOT point at \data (no double nesting)"
$fixed = ($lines | Where-Object { $_ -match '^set ' } | Measure-Object).Count
Assert ($fixed -eq 2) "default: both set lines present ($fixed)"
# array-split hazard: every array element must be a complete line
$ok = $true
foreach ($l in $written) {
    if ($l -match '--user-data-dir' -and $l -notmatch 'electron\.exe|npm start') { $ok = $false }
}
Assert $ok "default: no --user-data-dir fragment on its own line"

# --- case 2: EXPLICIT -DataDir ---
$DataDir = "E:\CAData"
$Script:DataDirExplicit = $true
$resolvedData = [Environment]::ExpandEnvironmentVariables($DataDir)
$userProfileArg = ' --user-data-dir="%~dp0userdata"'
if ($Script:DataDirExplicit) {
    $userProfileArg = ' --user-data-dir="' + $resolvedData + '"'
}
$envDataLine2 = 'set "DESK_AGENT_DATA_DIR=' + $resolvedData + '"'
$electronLine2 = '  "%~dp0node_modules\electron\dist\electron.exe" "%~dp0."' + $userProfileArg
Assert ($electronLine2 -eq '  "%~dp0node_modules\electron\dist\electron.exe" "%~dp0." --user-data-dir="E:\CAData"') "explicit: --user-data-dir is the explicit dir (v1..v24 semantics)"
Assert ($envDataLine2 -eq 'set "DESK_AGENT_DATA_DIR=E:\CAData"') "explicit: DESK_AGENT_DATA_DIR is the explicit dir"

# --- case 3: env-var expansion in the env line (use a var that exists on
# both platforms: expand a synthetic Windows-style var against a faked
# environment instead of USERPROFILE, which only exists on Windows) ---
[Environment]::SetEnvironmentVariable("CA_V25_PROBE", "D:\probed", "Process")
$DataDir = "%CA_V25_PROBE%\CAData"
$Script:DataDirExplicit = $true
$expanded = [Environment]::ExpandEnvironmentVariables($DataDir)
Assert ($expanded -eq "D:\probed\CAData") "env expansion works ($expanded)"

Remove-Item -LiteralPath $bat -Force -ErrorAction SilentlyContinue
Write-Output ""
Write-Output ("v25 launcher behavior: {0} pass, {1} fail" -f $script:pass, $script:fail)
if ($script:fail -gt 0) { exit 1 }
exit 0
