$ErrorActionPreference = "Stop"
# v20 selfcheck (task t_3ea985be): GUI uninstall entry. Structural gates on
# install-gui.ps1 + behavior tests of the data-dir helper functions
# extracted via AST. Runs on pwsh 7 (Linux); the GUI itself only runs on
# Windows -- this file verifies syntax, wiring, and the pure-PowerShell
# decision logic that Linux CAN execute.
$repo = Split-Path -Parent $PSScriptRoot        # scripts/
$f = Join-Path $repo "install-gui.ps1"
$installer = Join-Path $repo "install.ps1"

$script:pass = 0; $script:fail = 0
function Assert([bool]$Cond, [string]$Name) {
    if ($Cond) { $script:pass++; Write-Output "PASS: $Name" }
    else { $script:fail++; Write-Output "FAIL: $Name" }
}

# 1. BOM check (must keep UTF-8 BOM; the file is Chinese-text UI)
$bytes = [IO.File]::ReadAllBytes($f)
$bom = ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
Write-Output ("install-gui.ps1 UTF8-BOM: " + $bom)
Assert $bom "install-gui.ps1 keeps UTF-8 BOM"

# 2. whole-file parse (zero errors required) -- catches every syntax slip
$errs = $null; $toks = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f), [ref]$toks, [ref]$errs)
Write-Output ("install-gui.ps1 Parser errors: " + $errs.Count)
if ($errs.Count -gt 0) { foreach ($e in $errs) { Write-Output ("  " + $e.Extent.StartLineNumber + ": " + $e.Message) } }
Assert ($errs.Count -eq 0) "install-gui.ps1 parses clean"

# installer parses too (we call -Uninstall into it)
$errs2 = $null; $toks2 = $null
$ast2 = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $installer), [ref]$toks2, [ref]$errs2)
Assert ($errs2.Count -eq 0) "install.ps1 parses clean"

$full = $ast.Extent.Text

# 3. PS 5.1 regression gate (from t_fc1dd5a0): no bare $var ± N inside
#    New-Object argument lists. NOTE: the regex cannot see past the ')'
#    of the already-parenthesized safe form, so a SAFE `($y - 2)` shows
#    up as a match ending in `($y - 2`; the distinguishing feature of a
#    BARE form is the arithmetic NOT being preceded by an opening paren.
$bareHits = [regex]::Matches($full, 'New-Object [A-Za-z.]+\([^)]*\$\w+\s*[-+*/%]\s*\d') |
    Where-Object { $_.Value -notmatch '\(\$\w+\s*[-+]\s*\d+$' }
Assert ($bareHits.Count -eq 0) "no bare arithmetic in New-Object args (PS 5.1 trap)"

# 4. v20 structural gates -- the uninstall entry actually exists and is
#    wired end to end.
Assert ($full -match 'RadioButton') "mode radio buttons present"
Assert ($full -match 'function Invoke-GuiUninstall') "Invoke-GuiUninstall defined"
Assert ($full -match '\$rdoUninstall\.Checked\)\s*\{\s*Invoke-GuiUninstall') "button click routes to Invoke-GuiUninstall in uninstall mode"
Assert ($full -match '\-Uninstall') "-Uninstall delegated to install.ps1"
Assert ($full -match 'if \(\$purge\) \{ \$psiArgs \+= "-PurgeData" \}') "-PurgeData added ONLY when user checks the box"
Assert ($full -match '\$chkPurgeData\.Enabled = \$false') "purge checkbox disabled in install mode"
Assert ($full -match 'DialogResult\)\::Yes\) -ne \[System\.Windows\.Forms\.DialogResult\]::Yes' -or
        $full -match 'DialogResult\]::Yes\)' ) "confirm gates present (Yes required)"
# two separate confirms in the uninstall flow
$confirmCount = ([regex]::Matches($full, '卸载清单确认')).Count + ([regex]::Matches($full, '最终确认')).Count
Assert ($confirmCount -ge 2) "two-step uninstall confirmation present"
# post-run verification block
Assert ($full -match '卸载后复查') "post-uninstall re-verification prints to log area"
Assert ($full -match 'Get-PythonCoreResidue') "registry PythonCore residue re-check present"
# BOTH data layouts handled
Assert ($full -match 'Join-Path \$env:APPDATA "CodeAgent"') "legacy %APPDATA%\CodeAgent layout covered"
Assert ($full -match 'Join-Path \$TargetDir "data"') "new in-tree data dir covered"
Assert ($full -match 'Join-Path \$TargetDir "userdata"') "new in-tree userdata dir covered"
# default KEEP data: purge checkbox text states it, and it is off by default
Assert ($full -match '\$chkPurgeData\.Checked\b' ) "purge state read from checkbox"
Assert ($full -notmatch '\$chkPurgeData\.Checked = \$true') "purge checkbox never force-checked"

# 5. BEHAVIOR tests: extract the pure functions via AST and run them with
#    a fake Windows-style filesystem under the temp dir.
function Extract-Function([string]$Name) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    if (-not $fn) { throw "function $Name not found" }
    return $fn.Extent.Text
}
$fnCands  = Extract-Function 'Get-DataDirCandidates'
$fnRec    = Extract-Function 'Get-RecordedDataDir'
$fnOwned  = Extract-Function 'Test-LegacyDirOwnedBy'
$fnCount  = Extract-Function 'Get-FileCount'
Invoke-Expression $fnCands
Invoke-Expression $fnRec
Invoke-Expression $fnOwned
Invoke-Expression $fnCount

$root = Join-Path ([IO.Path]::GetTempPath()) ("v20self-" + [Guid]::NewGuid().ToString("N"))
$target = Join-Path $root "CodeAgent"
try {
    # Linux/pwsh7 has no $env:APPDATA -- set one so the GUI code paths work
    if (-not $env:APPDATA) { $env:APPDATA = (Join-Path $root "appdata"); New-Item -ItemType Directory -Path $env:APPDATA -Force | Out-Null }

    # fake install tree: install dir + in-tree data/userdata + record
    New-Item -ItemType Directory -Path (Join-Path $target "data") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $target "userdata") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $target "data/sessions.db") -Value "db"
    Set-Content -LiteralPath (Join-Path $target ".install-state.json") -Value '{"dataDir":"D:\\elsewhere\\data"}'

    # (a) candidates: 3 entries, in-tree ones exist, legacy normally absent
    $cands = Get-DataDirCandidates $target
    Assert ($cands.Count -eq 3) "candidates: exactly 3 data dirs (legacy + data + userdata)"
    Assert ($cands[1].Exists -eq $true) "in-tree data dir detected as existing"
    Assert ($cands[2].Exists -eq $true) "in-tree userdata dir detected as existing"
    Assert ($cands[0].Path -eq (Join-Path $env:APPDATA "CodeAgent")) "candidate[0] is the legacy %APPDATA% dir"

    # (b) recorded data dir read from .install-state.json
    $rec = Get-RecordedDataDir $target
    Assert ($rec -eq "D:\elsewhere\data") "recorded dataDir read from .install-state.json"

    # (c) record absent -> empty string
    $noRec = Get-RecordedDataDir (Join-Path $root "no-such-dir")
    Assert ($noRec -eq "") "record read returns empty for missing dir"

    # (d) ownership proof: no marker -> NOT owned
    $legacyDir = Join-Path $root "legacy"
    New-Item -ItemType Directory -Path $legacyDir -Force | Out-Null
    Assert ((Test-LegacyDirOwnedBy $legacyDir $target) -eq $false) "legacy dir WITHOUT marker: not owned"

    # (e) ownership proof: marker naming a DIFFERENT install -> NOT owned
    Set-Content -LiteralPath (Join-Path $legacyDir ".codeagent-install") -Value '{"app":"CodeAgent","installDir":"C:\\other\\CodeAgent"}'
    Assert ((Test-LegacyDirOwnedBy $legacyDir $target) -eq $false) "marker naming ANOTHER install: not owned"

    # (f) ownership proof: marker naming THIS install -> owned (case + trailing-slash tolerant)
    Set-Content -LiteralPath (Join-Path $legacyDir ".codeagent-install") -Value ('{"app":"CodeAgent","installDir":"' + ($target -replace '\\','\\') + '\\"}')
    Assert ((Test-LegacyDirOwnedBy $legacyDir $target) -eq $true) "marker naming THIS install (trailing slash): owned"

    # (g) corrupted marker -> NOT owned (fail-safe)
    Set-Content -LiteralPath (Join-Path $legacyDir ".codeagent-install") -Value 'not json at all'
    Assert ((Test-LegacyDirOwnedBy $legacyDir $target) -eq $false) "corrupted marker: not owned (fail-safe)"

    # (h) file count counts nested files
    $cnt = Get-FileCount $target
    Assert ($cnt -ge 2) "file count sees the tree (got $cnt)"
    Assert ((Get-FileCount (Join-Path $root "no-such-dir")) -eq 0) "file count of missing dir is 0"

    # (i) install-side salvage wiring: the install flow must probe the
    # salvage dirs (CodeAgent-data / CodeAgent-userdata under parent)
    Assert ($full -match 'CodeAgent-data') "install flow probes salvage dir CodeAgent-data"
    Assert ($full -match 'CodeAgent-userdata') "install flow probes salvage dir CodeAgent-userdata"

    # (j) safe-command assembly: the uninstall invocation quotes -InstallDir
    $quotePat = @'
"-InstallDir", ('"' + $TargetDir + '"')
'@
    Assert ($full.Contains($quotePat.Trim())) "uninstall -InstallDir is quoted (v19.2 lesson)"

    # (k) the event-handler log path is passed script-scoped (PS 5.1
    #     Register-ObjectEvent closure loss)
    Assert ($full -match 'script:uninstallGuiLogFile') "log path crosses Register-ObjectEvent via script scope"
} finally {
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}

Write-Output ""
if ($script:fail -gt 0) { Write-Output ("RESULT: " + $script:fail + " FAILED"); exit 1 }
Write-Output ("RESULT: ALL PASS (" + $script:pass + " checks)")
exit 0
