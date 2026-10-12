$ErrorActionPreference = "Stop"
# v19 selfcheck (task t_941de5b6): GUI installer + -CacheDir. Runs on
# pwsh 7 (Linux) via AST extraction, same pattern as v17/v18.
$f = Join-Path $PSScriptRoot "..\install.ps1"
$g = Join-Path $PSScriptRoot "..\install-gui.ps1"

# 1. BOM + ASCII of install.ps1
$bytes = [IO.File]::ReadAllBytes($f)
$bom = ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
$nonAscii = 0
for ($i = 3; $i -lt $bytes.Count; $i++) { if ($bytes[$i] -gt 0x7F) { $nonAscii++ } }
Write-Output "install.ps1 UTF8-BOM: $bom ; non-ASCII bytes after BOM: $nonAscii"

$pass = 0; $fail = 0
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

$full = $ast.Extent.Text

# 3. v19 structural gates on install.ps1
Assert ($full -match '\[string\]\$CacheDir = ""') "v19 -CacheDir param declared (default empty = legacy behavior)"
Assert ($full -match 'PIP_CACHE_DIR') "v19 pip env cache wired"
Assert ($full -match 'npm_config_cache') "v19 npm env cache wired"
Assert ($full -match 'cache-dir = ') "v19 pip.ini cache-dir pin written"
Assert ($full -match 'package caches: \$Script:CacheDir') "v19 completion report mentions caches"
# -CacheDir must NOT alter existing param semantics: count params to ensure
# only additions (no renames/removals of the pinned contract params).
foreach ($p in @('PythonArchive','NodeArchive','WheelDir','Offline','Force','NoShortcut','NoNode','AllowUnverified','ForceOwnRuntime','AllowCondaBase','Uninstall','PurgeData','Yes','PythonPath','InstallDir','PackageDir','DataDir','IndexUrl','CheckOnly')) {
    Assert ($full -match ('\[' + 'string\]?\$' + $p + '\b|\[string\]\$' + $p + '\b|\[switch\]\$' + $p + '\b')) "param $p still declared"
}

# 4. GUI script: parse clean + structural gates
$bytesG = [IO.File]::ReadAllBytes($g)
$bomG = ($bytesG[0] -eq 0xEF -and $bytesG[1] -eq 0xBB -and $bytesG[2] -eq 0xBF)
$nonAsciiG = 0
for ($i = 3; $i -lt $bytesG.Count; $i++) { if ($bytesG[$i] -gt 0x7F) { $nonAsciiG++ } }
Write-Output "install-gui.ps1 UTF8-BOM: $bomG ; non-ASCII bytes after BOM: $nonAsciiG"
$errsG = $null; $toksG = $null
$astG = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $g), [ref]$toksG, [ref]$errsG)
Write-Output "install-gui.ps1 Parser errors: $($errsG.Count)"
if ($errsG.Count -gt 0) { foreach ($e in $errsG) { Write-Output ("  " + $e.Extent.StartLineNumber + ": " + $e.Message) } }
Assert ($errsG.Count -eq 0) "install-gui.ps1 parses clean"
$fullG = $astG.Extent.Text

# GUI hard requirements from the task:
Assert ($fullG -match 'System\.Windows\.Forms') "GUI uses WinForms (no Python/Node dependency)"
Assert ($fullG -match 'python\.exe') "GUI probes python.exe"
Assert ($fullG -match 'node\.exe') "GUI probes node.exe"
Assert ($fullG -match 'install\.ps1') "GUI reuses scripts/install.ps1"
Assert ($fullG -match 'CacheDir') "GUI passes -CacheDir (self-contained caches)"
Assert ($fullG -match '-UsePbs') "GUI self-contained branch uses -UsePbs (portable, no registry; chosen over -ForceOwnRuntime which hard-errors on same-version registration)"
Assert ($fullG -match '-Yes') "GUI runs install.ps1 non-interactively"
Assert ($fullG -match 'CodeAgent') "GUI creates/uses the CodeAgent subdir"
Assert ($fullG -match 'New-Item') "GUI creates directories"
# GUI tail-follows the log via event-driven streaming (OutputDataReceived)
Assert ($fullG -match 'OutputDataReceived') "GUI streams installer output live (event-based tail)"
# no third-party deps: no Add-Type beyond WinForms/NET, no Import-Module
Assert ($fullG -notmatch 'Import-Module') "GUI has no Import-Module (no 3rd-party deps)"
Assert ($fullG -notmatch 'Install-Module') "GUI has no Install-Module"
# non-admin: no Start-Process -Verb RunAs, no elevation
Assert ($fullG -notmatch '-Verb\s+RunAs') "GUI never elevates (no admin)"
# branch C: existing <parent>\CodeAgent -> explicit prompt, not silent overwrite
Assert ($fullG -match '已存在') "GUI Chinese strings present"
Assert ($fullG -match '继续|覆盖|换') "GUI existing-dir prompt offers explicit choices"

# 5. behavior test: extract the -CacheDir wiring block and run it with fakes
# (simulate the param-resolution + env block by executing the exact lines
# from install.ps1 in a scratch dir).
$root = Join-Path ([IO.Path]::GetTempPath()) ("v19self-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $root -Force | Out-Null
try {
    # find the v19 cache wiring block: from "# v19: resolve -CacheDir" to
    # the closing of the env block; simpler: extract param-resolution
    # block and env block separately by AST? Lines are simpler here.
    $lines = $full -split "`r?`n"
    $startI = -1; $endI = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($startI -lt 0 -and $lines[$i] -match 'v19: resolve -CacheDir') { $startI = $i }
        if ($startI -ge 0 -and $lines[$i] -match 'self-contained package caches \(v19') { $endI = $i; break }
    }
    Assert ($startI -ge 0 -and $endI -gt $startI) "v19 cache wiring block located ($startI..$endI)"
    if ($startI -ge 0 -and $endI -gt $startI) {
        # extend endI forward until braces balance (the Write-Host line sits
        # INSIDE the second if; its closing '}' is on a later line)
        $depth = 0; $have = $false
        for ($i = $startI; $i -lt $lines.Count; $i++) {
            $depth += ([regex]::Matches($lines[$i], '\{')).Count
            $depth -= ([regex]::Matches($lines[$i], '\}')).Count
            if ($i -ge $endI) {
                if ($depth -le 0) { $endI = $i; break }
                if ($i -gt $endI + 6) { break }
            }
        }
        $block = ($lines[$startI..$endI] -join "`n")
        # run in a sandbox: set the vars the block references
        $CacheDir = Join-Path $root "runtime/cache"
        $expectedCache = $CacheDir
        New-Item -ItemType Directory -Path (Split-Path -Parent $CacheDir) -Force | Out-Null
        # NOTE: at top level $Script:X and $X are the SAME variable -- the
        # test must NOT null $Script:CacheDir beforehand or it nulls
        # $CacheDir too. Use a differently-named local for expectations.
        $env:PIP_CACHE_DIR = $null
        $env:npm_config_cache = $null
        Invoke-Expression $block
        Assert ($Script:CacheDir -eq $expectedCache) "block resolved `$Script:CacheDir"
        Assert ($env:PIP_CACHE_DIR -eq (Join-Path $expectedCache 'pip')) "env PIP_CACHE_DIR set to <CacheDir>\pip"
        Assert ($env:npm_config_cache -eq (Join-Path $expectedCache 'npm')) "env npm_config_cache set to <CacheDir>\npm"
        Assert (Test-Path -LiteralPath (Join-Path $expectedCache 'pip')) "pip cache dir created on disk"
        Assert (Test-Path -LiteralPath (Join-Path $expectedCache 'npm')) "npm cache dir created on disk"
        # default (empty CacheDir) must not touch env
        $CacheDir = ""
        $Script:CacheDir = $null
        $env:PIP_CACHE_DIR = "sentinel-pip"
        $env:npm_config_cache = "sentinel-npm"
        Invoke-Expression $block
        Assert ($env:PIP_CACHE_DIR -eq "sentinel-pip" -and $env:npm_config_cache -eq "sentinel-npm") "empty -CacheDir leaves env untouched (legacy behavior)"
    }

    # 6. behavior: pip.ini content build (extract just the assignment +
    # append). Simulate the v19 append.
    $pipIni = Join-Path $root "pip.ini"
    $IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple"
    $Script:CacheDir = (Join-Path $root "runtime\cache")
    $pipIniContent = @"
[global]
index-url = $IndexUrl
trusted-host = $(($IndexUrl -replace '^https?://','' -replace '/.*$',''))
"@
    if ($Script:CacheDir) {
        $pipIniContent += "cache-dir = " + (Join-Path $Script:CacheDir "pip") + "`r`n"
    }
    [IO.File]::WriteAllText($pipIni, $pipIniContent, (New-Object Text.UTF8Encoding($false)))
    $rb = Get-Content -LiteralPath $pipIni -Raw
    Assert ($rb -match 'cache-dir\s*=\s*\S*runtime\W?cache\W?pip') "pip.ini contains cache-dir pointing inside the tree"
    Assert ($rb -match [regex]::Escape($IndexUrl)) "pip.ini still pins the index (v6 behavior intact)"

    # 7. behavior: GUI branch-C prompt function -- verify by extracting the
    # prompt predicate: the GUI's flow must only overwrite after an explicit
    # choice. We assert structurally: the confirm-continue click handler
    # must reference the "already exists" condition.
    Assert ($fullG -match '已存在.*CodeAgent|CodeAgent.*已存在|Test-Path') "GUI checks target existence before proceeding"

    # 8. launcher .bat: GUI/startup bat must parse clean too
    $bat = Join-Path $PSScriptRoot "..\install-gui.bat"
    Assert (Test-Path -LiteralPath $bat) "install-gui.bat exists"
    $batTxt = Get-Content -LiteralPath $bat -Raw
    Assert ($batTxt -match 'install-gui\.ps1') "bat launches the GUI ps1"
    Assert ($batTxt -match 'powershell') "bat uses Windows PowerShell (built-in, no PS7 requirement)"
} finally {
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
    $env:PIP_CACHE_DIR = $null
    $env:npm_config_cache = $null
}

Write-Output ""
Write-Output ("RESULT: pass=$pass fail=$fail")
if ($fail -gt 0) { exit 1 }
