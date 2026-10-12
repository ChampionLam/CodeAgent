$ErrorActionPreference = "Stop"
# v20 selfcheck (task t_ed26cc98): empty-registry-root crash / dangling
# registration dead-end / -UsePbs resolution. Runs on pwsh 7 (Linux)
# via AST extraction, same pattern as v17/v18/v19.
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

$full = $ast.Extent.Text

# 3. v20 structural gates
Assert ($full -match '\[switch\]\$IgnoreRegistryState') "v20 -IgnoreRegistryState param declared"
Assert ($full -match 'v20 CRITICAL FIX') "v20 Get-RegistrySubKeys fix comment present"
# comma-return poison removed from CODE (the v20 header comment
# legitimately mentions the old construct in backticks)
$codeNoComm = ([regex]::Replace($full, '#[^\r\n]*', ''))
Assert ($codeNoComm -notmatch 'return\s*,\$out') "v19 comma-return poison removed from code (comments excluded)"
Assert ($full -match [regex]::Escape('Where-Object { $_ -is [string] -and $_.Trim() }')) "v20 helper filters non-strings"
# 4 caller guards (defense in depth)
Assert (([regex]::Matches($full, [regex]::Escape('foreach ($tagPath in @((Get-RegistrySubKeys $regRoot) | Where-Object { $_ }))'))).Count -eq 1) "caller guard: Get-PythonCandidates registry sweep"
Assert (([regex]::Matches($full, [regex]::Escape('foreach ($tagPath in @($tagDirs | Where-Object { $_ }))'))).Count -eq 3) "caller guards: 3 tagDirs loops (1119/1617/1683 pattern)"
# defect 2: broken no longer unconditional throw; new switch documented
Assert ($full -match 'registration is broken AND no alternative route was selected') "v20 broken-gate message rewritten"
Assert ($full -match 'msiexec /x \{GUID\}') "v20 GUID-level removal recipe present"
Assert ($full -match 'if \(\$IgnoreRegistryState\) \{\s*return @\{ Action = "ignored"') "v20 Test-PinnedPythonRegistration honors -IgnoreRegistryState"
Assert ($full -notmatch 'exit 1602') "no stale exit-code claims"
# defect 3: multi-dir walk + loud downgrade
Assert ($full -match 'Sort-Object -Descending') "v20 Get-PbsArchive walks date dirs newest-first"
Assert ($full -match 'all date directories searched') "v20 loud resolution-failure message"
Assert ($full -match 'DOWNGRADE: falling back to the OFFICIAL INSTALLER') "v20 downgrade states registry consequence"

# 4. BEHAVIOR tests: extract Get-RegistrySubKeys + Get-UrlText +
#    Get-PbsArchive via AST and run them for real.
function Extract-Function([string]$Name, [string]$Text) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    if (-not $fn) { throw "function $Name not found" }
    return $fn.Extent.Text
}
$fnSub = Extract-Function 'Get-RegistrySubKeys' $full
$fnUrl = Extract-Function 'Get-UrlText' $full
$fnPbs = Extract-Function 'Get-PbsArchive' $full
Invoke-Expression $fnSub
Invoke-Expression $fnUrl
Invoke-Expression $fnPbs

$root = Join-Path ([IO.Path]::GetTempPath()) ("v20self-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $root -Force | Out-Null
try {
    # 4a. ROOT EXISTS, ZERO subkeys -> must return a real empty array
    #     and every caller pattern must survive Split-Path -Leaf.
    $emptyRoot = Join-Path $root "PythonCore"
    New-Item -ItemType Directory -Path $emptyRoot -Force | Out-Null
    $r1 = @(Get-RegistrySubKeys $emptyRoot)
    Assert ($null -ne $r1) "empty root: @() wrapper gives a non-null array"
    Assert (@($r1).Count -eq 0) "empty root: returns ZERO elements (was 1 empty-array element in v19)"
    Assert (-not ($r1 | Where-Object { -not ($_ -is [string]) })) "empty root: no non-string elements"
    # the exact v19 crash pattern: foreach + Split-Path -Leaf
    $crashed = $false
    try {
        foreach ($tagPath in @((Get-RegistrySubKeys $emptyRoot) | Where-Object { $_ })) {
            $tagName = [string](Split-Path -Leaf $tagPath)
        }
        foreach ($tagPath in @($r1 | Where-Object { $_ })) {
            $tagName = [string](Split-Path -Leaf $tagPath)
        }
    } catch { $crashed = $true }
    Assert (-not $crashed) "empty root: caller pattern (foreach + Split-Path) does not throw"

    # 4b. root with ONE subkey -> returns exactly one real path
    $oneRoot = Join-Path $root "PythonCore2"
    New-Item -ItemType Directory -Path (Join-Path $oneRoot "3.12") -Force | Out-Null
    $r2 = Get-RegistrySubKeys $oneRoot
    Assert (@($r2).Count -eq 1) "one subkey: exactly 1 element"
    Assert (@($r2)[0] -is [string]) "one subkey: element is a string"
    Assert ((Split-Path -Leaf @($r2)[0]) -eq "3.12") "one subkey: leaf is the subkey name"

    # 4c. missing root -> empty
    $r3 = Get-RegistrySubKeys (Join-Path $root "nope")
    Assert (@($r3).Count -eq 0) "missing root: zero elements"

    # 4d. tagDirs-accumulation pattern (three roots, first empty)
    $tagDirs = @(Get-RegistrySubKeys $emptyRoot)
    $tagDirs += @(Get-RegistrySubKeys $oneRoot)
    $tagDirs += @(Get-RegistrySubKeys (Join-Path $root "nope"))
    Assert (@($tagDirs).Count -eq 1) "accumulation across roots: 1 element total"
    $leafOk = $true
    foreach ($tagPath in @($tagDirs | Where-Object { $_ })) {
        $n = [string](Split-Path -Leaf $tagPath)
        if ($n -ne "3.12") { $leafOk = $false }
    }
    Assert ($leafOk) "accumulation: Split-Path -Leaf works on every element"

    # 4e. Get-PbsArchive LIVE against npmmirror (v20 root cause: the
    # latest dir 20261009 only has 3.12.15; 3.12.10 is in an OLD dir).
    # Field-verified facts: 3.12.10 -> dir 20250529, sha 3e0fd10e...
    $pbs = Get-PbsArchive "https://registry.npmmirror.com/-/binary/python-build-standalone/" "3.12.10"
    Assert ($null -ne $pbs) "LIVE: PBS 3.12.10 resolves (newest-first multi-dir walk + optional-patch regex)"
    if ($pbs) {
        Assert ($pbs.Date -eq "20250529") "LIVE: resolved from the 20250529 dir (not the 20261009 latest)"
        Assert ($pbs.MaskedName -match '^cpython-3\.12\.10\+') "LIVE: masked name matches pinned version (regex fix: '+' directly after x.y.z)"
        Assert ($pbs.Url -eq "https://registry.npmmirror.com/-/binary/python-build-standalone/20250529/cpython-3.12.10%2B20250529-x86_64-pc-windows-msvc-install_only.tar.gz") "LIVE: URL = date dir + demasked name (+ percent-encoded as %2B)"
        Assert ($pbs.Sha256 -match '^[0-9a-f]{64}$') "LIVE: sha256 resolved from SHA256SUMS"
        Assert ($pbs.Sha256 -eq "3e0fd10e4bd60e66baad8be2a0e60775bb389e97e4f4a07138c34599c356aaed") "LIVE: sha256 equals the field-verified value"
    }
    # version NOT on the mirror at all -> null, no throw
    $pbsNo = $null
    $threwNo = $false
    try { $pbsNo = Get-PbsArchive "https://registry.npmmirror.com/-/binary/python-build-standalone/" "3.12.99" } catch { $threwNo = $true }
    Assert (-not $threwNo -and $null -eq $pbsNo) "LIVE: nonexistent version resolves null without throwing"
} finally {
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}

Write-Output ""
Write-Output ("RESULT: pass={0} fail={1}" -f $script:pass, $script:fail)
if ($script:fail -gt 0) { exit 1 } else { exit 0 }
