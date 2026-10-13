$ErrorActionPreference = "Stop"
# v24 sweep-scope selfcheck (2026-10-13 real-machine blocker, standalone).
#
# Why standalone: v24-selfcheck.ps1 section 5 runs a real npm behavior
# test that throws in an offline/extension-less-mock environment, so
# anything after it never executes. This file tests ONLY the credential
# sweep's scope + pattern, extracts Invoke-CredentialSweep from
# install.ps1 and runs it against simulated trees.
#
# Real-machine evidence (on the test machine, install log):
#   [install] SECURITY FAILURE: credentials leaked from source tree.
#   <install>\python\.venv\Lib\site-packages\cryptography\hazmat\
#     primitives\serialization\ssh.py: literal key material "sk-ssh..."
# The match was the OpenSSH security-key TYPE IDENTIFIER
# "sk-ssh-ed25519@openssh.com", not a credential -- and cryptography is
# pip-installed by this installer itself (mnemosyne-memory -> mcp ->
# pyjwt[crypto]), so every fresh install was stopped by its own gate.

$f = Join-Path $PSScriptRoot "..\install.ps1"
$bytes = [IO.File]::ReadAllBytes($f)
$nonAscii = 0
for ($i = 3; $i -lt $bytes.Count; $i++) { if ($bytes[$i] -gt 0x7F) { $nonAscii++ } }

$script:pass = 0; $script:fail = 0
function Assert([bool]$Cond, [string]$Name) {
    if ($Cond) { $script:pass++; Write-Output "PASS: $Name" }
    else { $script:fail++; Write-Output "FAIL: $Name" }
}

Assert ($nonAscii -eq 0) "install.ps1 is pure ASCII"

$errs = $null; $toks = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f), [ref]$toks, [ref]$errs)
Assert ($errs.Count -eq 0) "install.ps1 parses clean under PS $($PSVersionTable.PSVersion)"

$full = $ast.Extent.Text
$fnStart = $full.IndexOf('function Invoke-CredentialSweep(')
Assert ($fnStart -ge 0) "Invoke-CredentialSweep locatable"
if ($fnStart -lt 0) { Write-Output ("v24 sweep-scope: pass=$($script:pass) fail=$($script:fail)"); exit 1 }

$rest = $full.Substring($fnStart)
$fnEnd = $rest.IndexOf("`r`n}")
if ($fnEnd -lt 0) { $fnEnd = $rest.IndexOf("`n}") }
Assert ($fnEnd -gt 0) "function end boundary found"
if ($fnEnd -lt 0) { Write-Output ("v24 sweep-scope: pass=$($script:pass) fail=$($script:fail)"); exit 1 }
Invoke-Expression $rest.Substring(0, $fnEnd + 2)

$tmpRoot = Join-Path $env:TEMP ("ca-sweepscope-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null
Write-Output "simulated trees under: $tmpRoot"

try {
    # (a) a REAL key shape in the app tree -> the gate must still FAIL.
    $simA = Join-Path $tmpRoot 'a'
    New-Item -ItemType Directory -Path (Join-Path $simA 'python') -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $simA 'python\dev_backup.env') -Value ('OPENAI_API_KEY=sk-' + ('A' * 48)) -Encoding ascii
    $threwA = $false
    try { Invoke-CredentialSweep -Root $simA -SkipFiles @() } catch { $threwA = $true }
    Assert $threwA "(a) realistic sk-<48> key in the app tree -> sweep FAILS (gate still works)"
    Assert ($script:credFindings.Count -ge 1) "(a2) offending file reported: $($script:credFindings[0])"

    # (b) cryptography's sk-ssh identifier inside .venv -> must PASS.
    $simB = Join-Path $tmpRoot 'b'
    $cryDir = Join-Path $simB 'python\.venv\Lib\site-packages\cryptography\hazmat\primitives\serialization'
    New-Item -ItemType Directory -Path $cryDir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $cryDir 'ssh.py') -Value 'KEY_TYPES = ["sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"]' -Encoding ascii
    $threwB = $false
    try { Invoke-CredentialSweep -Root $simB -SkipFiles @() } catch { $threwB = $true }
    Assert (-not $threwB) "(b) cryptography sk-ssh identifier inside .venv -> sweep PASSES (no false positive)"
    Assert ($script:sweepSkipped.Count -ge 1) "(b2) venv tree reported as skipped, not scanned"

    # (b3) the skipped-venv rule must NOT be a blanket bypass: the same
    # identifier placed in the APP tree (not under .venv) must not fail
    # either, because it is shorter than a real key body.
    $simB3 = Join-Path $tmpRoot 'b3'
    New-Item -ItemType Directory -Path $simB3 -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $simB3 'notes.py') -Value 'TYPES = ["sk-ssh-ed25519@openssh.com"]' -Encoding ascii
    $threwB3 = $false
    try { Invoke-CredentialSweep -Root $simB3 -SkipFiles @() } catch { $threwB3 = $true }
    Assert (-not $threwB3) "(b3) sk-ssh-... outside .venv also passes (pattern, not path, decides)"

    # (c) node_modules dependency source -> must PASS.
    $simC = Join-Path $tmpRoot 'c'
    $nmDir = Join-Path $simC 'node_modules\some-pkg'
    New-Item -ItemType Directory -Path $nmDir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $nmDir 'index.js') -Value ('const t = "sk-' + ('B' * 40) + '";') -Encoding ascii
    $threwC = $false
    try { Invoke-CredentialSweep -Root $simC -SkipFiles @() } catch { $threwC = $true }
    Assert (-not $threwC) "(c) key-shaped string inside node_modules -> sweep PASSES"

    # (d) a still-live key in a nested app dir -> must FAIL (no blind spot).
    $simD = Join-Path $tmpRoot 'd'
    $deepDir = Join-Path $simD 'electron\out'
    New-Item -ItemType Directory -Path $deepDir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $deepDir 'config.json') -Value ('{"apiKey":"sk-proj-' + ('C' * 64) + '"}') -Encoding ascii
    $threwD = $false
    try { Invoke-CredentialSweep -Root $simD -SkipFiles @() } catch { $threwD = $true }
    Assert $threwD "(d) nested app dir with a live apiKey field -> sweep FAILS"
} finally {
    Remove-Item -Recurse -Force $tmpRoot -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# v24 structural checks for the other two real-machine blockers. These read
# install.ps1 as text, so they need no npm/electron/network -- they guard the
# fixes that the real machine proved were needed.
# ---------------------------------------------------------------------------
$instPath = Join-Path (Join-Path $PSScriptRoot '..') 'install.ps1'
$instText = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $instPath).Path)

$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile($instPath, [ref]$null, [ref]$parseErrors) | Out-Null
Assert ($parseErrors.Count -eq 0) "install.ps1 parses clean under THIS PowerShell ($($PSVersionTable.PSVersion)) -- syntax of the v24 edits"

Assert ($instText -notmatch "(?m)^\s*[^#\r\n]*& \`$venvPy -c \`$envScript") "memory probe no longer hands a multi-line script to a native exe via -c (comment text excluded)"
Assert ($instText -match 'ca-memprobe-') "memory probe runs from a temp .py file"
Assert ($instText -notmatch "& \`$venvPy -c @'") "dependency probe no longer hands a here-string to a native exe via -c"
Assert ($instText -match 'ca-depcheck-') "dependency probe runs from a temp .py file"
Assert ($instText -match "ErrorActionPreference = 'Continue'") "fail-soft native calls lower the preference so stderr cannot abort the install"
Assert ($instText -match 'electron runtime binary \(postinstall repair\)') "electron runtime binary is repaired when npm install left it out"
Assert ($instText -match 'node install\.js') "the repair runs electron's own install.js (mirror-driven download)"
Assert ($instText -match 'IconLocation') "shortcuts get the shipped icon instead of a generic .bat icon"
Assert ($instText -match 'shortcut updated') "existing shortcuts are updated, not skipped"
Assert ($instText -match 'WindowStyle = 7') "shortcut console is minimised (no cmd window sitting on the desktop)"

Write-Output ""
Write-Output ("v24 sweep-scope: pass=$($script:pass) fail=$($script:fail)")
if ($script:fail -gt 0) { exit 1 }
exit 0