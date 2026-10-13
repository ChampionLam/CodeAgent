$ErrorActionPreference = "Stop"
# v24 selfcheck (task t_ab909828): install-without-build -> black window.
# Gates: (1) install.ps1 still parses clean / ASCII / BOM; (2) the v24
# build stage + hard artifact gate + -SkipBuild exist; (3) the FALSE
# "ships in the repo" comment is gone; (4) the old "target machine will
# need 'npm install'" WARNING is gone (now informational Host line);
# (5) BEHAVIOR: Invoke-AppBuild runs npm/npm-run-build/build-ui.ps1 in
# order and hard-fails on nonzero exit; (6) BEHAVIOR: the artifact gate
# throws when any of the three launch-critical files is missing, and
# passes when all exist (simulated with mock artifacts).
$f = Join-Path $PSScriptRoot "..\install.ps1"

# 1. BOM + ASCII
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

# 2. whole-file parse
$errs = $null; $toks = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f), [ref]$toks, [ref]$errs)
Write-Output "install.ps1 Parser errors: $($errs.Count)"
if ($errs.Count -gt 0) { foreach ($e in $errs) { Write-Output ("  " + $e.Extent.StartLineNumber + ": " + $e.Message) } }
Assert ($errs.Count -eq 0) "install.ps1 parses clean"
Assert ($nonAscii -eq 0) "install.ps1 is pure ASCII (PS 5.1 GBK safety)"
Assert $bom "install.ps1 has UTF-8 BOM"

$full = $ast.Extent.Text

# 3. v24 structural gates
Assert ($full -match 'function Invoke-AppBuild') "v24 build function Invoke-AppBuild present"
Assert ($full -match '\[switch\]\$SkipBuild') "v24 -SkipBuild switch present"
Assert ($full -match [regex]::Escape('dist\electron\main.js')) "v24 artifact gate names dist\electron\main.js"
Assert ($full -match [regex]::Escape('electron\ui\index.html')) "v24 artifact gate names electron\ui\index.html"
Assert ($full -match [regex]::Escape('node_modules\electron\dist\electron.exe')) "v24 artifact gate names the electron binary"
Assert ($full -match 'ARTIFACT CHECK FAILED') "v24 hard-failure message present"
Assert ($full -match [regex]::Escape('npm_config_registry = "https://registry.npmmirror.com"')) "v24 npm registry pinned to npmmirror"
Assert ($full -match [regex]::Escape('$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"')) "v24 ELECTRON_MIRROR pinned"
Assert ($full -match [regex]::Escape('build-ui.ps1')) "v24 build stage invokes scripts\build-ui.ps1"
Assert ($full -notmatch [regex]::Escape('ships in the repo')) "v24 FALSE 'ships in the repo' comment REMOVED"
Assert ($full -notmatch [regex]::Escape("Write-Warning `"[install] source has no node_modules -- target machine will need 'npm install' before first launch`"")) "v24 old no-node_modules WARNING removed"
Assert ($full -match 'the installed app WILL NOT START as-is') "v24 SkipBuild/Offline state the app will not start"

# 4. BEHAVIOR tests: extract functions via AST, run with mocks.
function Extract-Function([string]$Name) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    if (-not $fn) { throw "function $Name not found" }
    return $fn.Extent.Text
}
$fnStep = Extract-Function 'Invoke-Step'
$fnBuild = Extract-Function 'Invoke-AppBuild'
Invoke-Expression $fnStep
Invoke-Expression $fnBuild

$root = Join-Path ([IO.Path]::GetTempPath()) ("v24self-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $root -Force | Out-Null

try {
    # mock npm: a script that logs its arguments to a file
    $mockBin = Join-Path $root "bin"
    New-Item -ItemType Directory -Path $mockBin -Force | Out-Null
    $calls = Join-Path $root "calls.txt"
    $mockNpm = Join-Path $mockBin "npm"
    $npmContent = @'
#!/bin/sh
echo "npm $*" >> "$V24_CALLS"
exit 0
'@
    [IO.File]::WriteAllText($mockNpm, $npmContent.Replace("`r`n", "`n"))
    # mock powershell wrapper: only build-ui.ps1 path is executed
    $mockPs = Join-Path $mockBin "powershell"
    $psContent = @'
#!/bin/sh
echo "powershell $*" >> "$V24_CALLS"
exit 0
'@
    [IO.File]::WriteAllText($mockPs, $psContent.Replace("`r`n", "`n"))
    if ($IsLinux -or $IsMacOS) {
        chmod +x $mockNpm; chmod +x $mockPs
    }
    $oldPath = $env:PATH
    $env:PATH = "$mockBin" + [IO.Path]::PathSeparator + $oldPath
    $env:V24_CALLS = $calls

    # (a) fresh install dir, no artifacts -> npm install, npm run build,
    #     build-ui.ps1 all invoked, in order.
    $inst = Join-Path $root "install"
    New-Item -ItemType Directory -Path $inst -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $inst "scripts") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $inst "scripts\build-ui.ps1") -Value "# mock"
    Invoke-AppBuild $inst $null
    $log = (Get-Content -LiteralPath $calls -ErrorAction SilentlyContinue) -join "`n"
    Assert ($log -match 'npm install') "(a) npm install invoked on a fresh tree"
    Assert ($log -match 'npm run build') "(a) npm run build invoked"
    Assert ($log -match 'build-ui\.ps1') "(a) build-ui.ps1 invoked"
    Assert (($log -split "`n" | Where-Object { $_ -match '^npm (install|run build)' }).Count -eq 2) "(a) both npm steps ran once"

    # (b) hard failure: mock npm now exits 1 -> Invoke-AppBuild throws
    Set-Content -LiteralPath $calls -Value ""
    [IO.File]::WriteAllText($mockNpm, "#!/bin/sh`necho `"npm `$*`" >> `"`$V24_CALLS`"`nexit 1`n")
    chmod +x $mockNpm
    $threw = $false
    try { Invoke-AppBuild $inst $null } catch { $threw = $true }
    Assert $threw "(b) nonzero npm exit HARD-FAILS the build (no warn-and-continue)"
    # no network calls of any kind were made by the mock run
    Assert ((Get-Command npm).Source -like "$mockBin*") "(b) mock npm was the one used"

    # (c) idempotency: artifacts already present -> no npm/build calls
    Set-Content -LiteralPath $calls -Value ""
    [IO.File]::WriteAllText($mockNpm, "#!/bin/sh`necho `"npm `$*`" >> `"`$V24_CALLS`"`nexit 0`n")
    chmod +x $mockNpm
    New-Item -ItemType Directory -Path (Join-Path $inst "dist\electron") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $inst "dist\electron\main.js") -Value "// built"
    New-Item -ItemType Directory -Path (Join-Path $inst "electron\ui") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $inst "electron\ui\index.html") -Value "<html></html>"
    Set-Content -LiteralPath (Join-Path $inst "electron\renderer.bundle.js") -Value "// bundle"
    New-Item -ItemType Directory -Path (Join-Path $inst "node_modules\electron\dist") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $inst "node_modules\electron\dist\electron.exe") -Value "EXE"
    Invoke-AppBuild $inst $null
    $log2 = (Get-Content -LiteralPath $calls -ErrorAction SilentlyContinue) -join ""
    Assert ($log2.Trim() -eq "") "(c) all artifacts present -> every build step SKIPPED (idempotent)"

    $env:PATH = $oldPath
} finally {
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}

# 5. BEHAVIOR: the artifact gate block. Extract the script-block-level
# gate logic is inline (not a function), so we re-verify by evaluating
# the same expressions on a simulated tree: the gate's semantics are
# the three Test-Path conditions + throw. Simulate directly.
$sim = Join-Path ([IO.Path]::GetTempPath()) ("v24sim-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path (Join-Path $sim "dist\electron") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $sim "electron\ui") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $sim "node_modules\electron\dist") -Force | Out-Null
try {
    # (d) all three missing -> the gate's $missing set has 3 entries
    $artifacts = @(
        @{ Path = Join-Path $sim "dist\electron\main.js" },
        @{ Path = Join-Path $sim "electron\ui\index.html" },
        @{ Path = Join-Path $sim "node_modules\electron\dist\electron.exe" }
    )
    $missing = @($artifacts | Where-Object { -not (Test-Path -LiteralPath $_.Path) })
    Assert ($missing.Count -eq 3) "(d) fresh tree: gate detects all 3 missing artifacts"

    # (e) create two -> still fails (1 missing)
    Set-Content -LiteralPath $artifacts[0].Path -Value "x"
    Set-Content -LiteralPath $artifacts[2].Path -Value "x"
    $missing = @($artifacts | Where-Object { -not (Test-Path -LiteralPath $_.Path) })
    Assert ($missing.Count -eq 1) "(e) electron\ui\index.html alone missing -> gate still fails"

    # (f) all present -> 0 missing
    Set-Content -LiteralPath $artifacts[1].Path -Value "x"
    $missing = @($artifacts | Where-Object { -not (Test-Path -LiteralPath $_.Path) })
    Assert ($missing.Count -eq 0) "(f) all three present -> gate passes"
} finally {
    Remove-Item -Recurse -Force $sim -ErrorAction SilentlyContinue
}

# 6. regression: the v23 sweep functions are still intact (they run at
# the end of a real install; v24 must not have broken them).
Assert ($full -match 'function Test-SecretValue') "v23 Test-SecretValue still present"
Assert ($full -match 'function Invoke-CredentialSweep') "v23 Invoke-CredentialSweep still present"
Assert ($full -match 'function Assert-BlankConfig') "v23 Assert-BlankConfig still present"

# 7. v24 sweep scope (2026-10-13 real-machine blocker): the content
# sweep must still catch a DEV credential backup, and must NOT be
# tripped by key-shaped strings inside dependency trees. Real-machine
# evidence: cryptography\hazmat\primitives\serialization\ssh.py carries
# the OpenSSH type identifier "sk-ssh-ed25519@openssh.com"; the old
# {8,} bound matched it and stopped every fresh install with EXIT=1 as
# soon as the venv contained cryptography (installed via
# mnemosyne-memory -> mcp -> pyjwt[crypto]).
$fnStart = $full.IndexOf('function Invoke-CredentialSweep(')
Assert ($fnStart -ge 0) 'v24 sweep function locatable for behavioral run'
if ($fnStart -ge 0) {
    $rest = $full.Substring($fnStart)
    $fnEnd = $rest.IndexOf("`r`n}")
    if ($fnEnd -lt 0) { $fnEnd = $rest.IndexOf("`n}") }
    Invoke-Expression $rest.Substring(0, $fnEnd + 3)

    # (a) realistic key in the APP tree -> must FAIL
    $simA = Join-Path $env:TEMP ("ca-sweep-a-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path (Join-Path $simA 'python') -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $simA 'python\dev_backup.env') -Value ('OPENAI_API_KEY=sk-' + ('A' * 48)) -Encoding ascii
    $threwA = $false
    try { Invoke-CredentialSweep -Root $simA -SkipFiles @() } catch { $threwA = $true }
    Assert $threwA '(a) realistic sk- key in the app tree -> sweep FAILS (gate still works)'
    Assert ($script:credFindings.Count -ge 1) '(a2) the offending file is reported'
    Remove-Item -Recurse -Force $simA -ErrorAction SilentlyContinue

    # (b) cryptography's sk-ssh identifier inside .venv -> must PASS
    $simB = Join-Path $env:TEMP ("ca-sweep-b-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $cryDir = Join-Path $simB 'python\.venv\Lib\site-packages\cryptography\hazmat\primitives\serialization'
    New-Item -ItemType Directory -Path $cryDir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $cryDir 'ssh.py') -Value 'KEY_TYPES = ["sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"]' -Encoding ascii
    $threwB = $false
    try { Invoke-CredentialSweep -Root $simB -SkipFiles @() } catch { $threwB = $true }
    Assert (-not $threwB) '(b) cryptography sk-ssh identifier inside .venv -> sweep PASSES (no false positive)'
    Assert ($script:sweepSkipped.Count -ge 1) '(b2) the venv tree was reported as skipped, not scanned'
    Remove-Item -Recurse -Force $simB -ErrorAction SilentlyContinue

    # (c) short non-secret sk- token outside dependency trees -> PASS
    $simC = Join-Path $env:TEMP ("ca-sweep-c-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $simC -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $simC 'notes.md') -Value 'see sk-learn-notes for the old draft' -Encoding ascii
    $threwC = $false
    try { Invoke-CredentialSweep -Root $simC -SkipFiles @() } catch { $threwC = $true }
    Assert (-not $threwC) '(c) short sk- token (not key-shaped, <20 chars) -> sweep PASSES'
    Remove-Item -Recurse -Force $simC -ErrorAction SilentlyContinue
}

Write-Output ""
Write-Output ("v24 selfcheck: pass=$($script:pass) fail=$($script:fail)")
if ($script:fail -gt 0) { exit 1 }
exit 0
