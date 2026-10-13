$ErrorActionPreference = "Stop"
# v18 self-check (task t_bebacdab): -PythonArchive wins over registry
# REUSE. Runs on pwsh 7 (Linux) via AST extraction like v17.
$f = Join-Path $PSScriptRoot "..\install.ps1"

# 1. BOM + ASCII
$bytes = [IO.File]::ReadAllBytes($f)
$bom = ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
$nonAscii = 0
for ($i = 3; $i -lt $bytes.Count; $i++) { if ($bytes[$i] -gt 0x7F) { $nonAscii++ } }
Write-Output "UTF8-BOM: $bom ; non-ASCII bytes after BOM: $nonAscii"

# 2. whole-file parse (zero errors required)
$errs = $null; $toks = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f), [ref]$toks, [ref]$errs)
Write-Output "Parser errors: $($errs.Count)"
if ($errs.Count -gt 0) { foreach ($e in $errs) { Write-Output ("  " + $e.Extent.StartLineNumber + ": " + $e.Message) } }

$pass = 0; $fail = 0
function Assert([bool]$Cond, [string]$Name) {
    if ($Cond) { $script:pass++; Write-Output "PASS: $Name" }
    else { $script:fail++; Write-Output "FAIL: $Name" }
}

$full = $ast.Extent.Text

# 3. structural gates on the v18 edits
Assert ($full -match 'function Test-LocalPythonArchiveIntent') "v18 helper Test-LocalPythonArchiveIntent defined"
Assert ($full -match 'it takes priority over the registered Python \$PythonVersion') "v18 reuse-override warning present in Install-PythonRuntime"
Assert ($full -match 'it takes priority over discovered interpreters') "v18 offline discovery-demotion present"
Assert ($full -match 'reusing an EXTERNAL interpreter -- the venv will depend on it') "v18 external-interpreter dependency warning present"

# 4. behavior: Test-LocalPythonArchiveIntent (extract + run with fakes)
$wanted = @("Test-LocalPythonArchiveIntent")
$fnAsts = @($ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $args[0].Name }, $true))
Assert ($fnAsts.Count -eq 1) "AST extraction found the v18 helper"
$src = $fnAsts[0].Extent.Text

$root = Join-Path ([IO.Path]::GetTempPath()) ("v18self-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $root -Force | Out-Null
try {
    Invoke-Expression $src

    # a) .exe intent
    $PythonArchive = Join-Path $root "py.exe"
    [IO.File]::WriteAllBytes($PythonArchive, [byte[]](0x4D,0x5A) * 1024)
    $r = Test-LocalPythonArchiveIntent
    Assert ($r.Intent -and $r.Path -eq $PythonArchive) "a) .exe file -> archive intent true"

    # b) valid gzip tar.gz intent
    $PythonArchive = Join-Path $root "cpython.tar.gz"
    [IO.File]::WriteAllBytes($PythonArchive, [byte[]](0x1F,0x8B,0x08,0x00,0,0,0,0,0,0,0,0,0,0,0,0,0,0))
    $r = Test-LocalPythonArchiveIntent
    Assert ($r.Intent) "b) gzip-magic tar.gz -> archive intent true"

    # c) non-gzip garbage -> no intent
    $PythonArchive = Join-Path $root "junk.tar.gz"
    [IO.File]::WriteAllBytes($PythonArchive, [byte[]](0x00,0x11,0x22,0x33))
    $r = Test-LocalPythonArchiveIntent
    Assert (-not $r.Intent) "c) non-gzip tar.gz -> no intent (falls to discovery)"

    # d) missing path -> no intent
    $PythonArchive = Join-Path $root "nope.exe"
    $r = Test-LocalPythonArchiveIntent
    Assert (-not $r.Intent) "d) nonexistent archive -> no intent"

    # e) not given -> no intent
    $PythonArchive = ""
    $r = Test-LocalPythonArchiveIntent
    Assert (-not $r.Intent) "e) empty -PythonArchive -> no intent"

    # 5. the Install-PythonRuntime reuse-override is guarded by
    #    "$archive -eq $PythonArchive" so the REUSE fast-path still
    #    fires for download/cached-exe sources (structural check).
    #    The other "reuse" occurrence is the switch in the Phase-0
    #    check table, which we intentionally do NOT touch.
    $reuseBlock = [regex]::Matches($full, [regex]::Escape('if ($regCheck.Action -eq "reuse")'))
    Assert ($reuseBlock.Count -eq 1) "installer-path reuse branch exists (and is the only if-reuse)"
} finally {
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}

Write-Output ("RESULT: pass=" + $pass + " fail=" + $fail)
if ($fail -gt 0) { exit 1 }
