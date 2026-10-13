$ErrorActionPreference = "Stop"
# v28 selfcheck (task t_eea5a4ac): guard model download from ModelScope.
# PRECISION NOTE: the delivery is FP16 (corrected twice mid-task; final
# table per operator note 2026-10-13): decision_bert.onnx + .onnx.data
# (ONNX external-data pair, both managed) + calibrator.json + tokenizer.
# Gates:
#  (1) install.ps1 still parses clean / ASCII / BOM;
#  (2) v28 structural gates (params, function, call site, report line,
#      no modelscope SDK dependency, fp16 table pinned incl. .data);
#  (3) BEHAVIOR: Get-Sha256SumsMap parses the 3 SHA256SUMS line shapes;
#  (4) BEHAVIOR: Install-GuardModel with a mock download function:
#        a. verified local file -> counted OK, never touched;
#        b. corrupt local file + network mock serving correct bytes
#           -> self-heals (re-download replaces it), sha256 verified;
#        c. corrupt local file + network ALWAYS serving bad bytes
#           -> file not left in place, MISSING recorded, WARNING
#           + curl self-heal line printed, NO throw (fail-soft);
#        d. offline + all files missing -> all required files in
#           MISSING, no throw;
#        e. .onnx present but .onnx.data missing -> .data in MISSING
#           (the external-data pair must be COMPLETE);
#        f. no expected sha256 source (SHA256SUMS absent, not pinned)
#           -> size-matched file reported UNVERIFIED, never MISSING,
#           never deleted;
#        g. serve.py / README.md in the model dir are never touched;
#        h. a WRONG local SHA256SUMS beats the built-in table (priority).
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

# v28 structural gates
Assert ($full -match '\[string\]\$ModelScopeRepo') "v28 -ModelScopeRepo param present"
Assert ($full -match '\[switch\]\$SkipModelDownload') "v28 -SkipModelDownload switch present"
Assert ($full -match 'function Install-GuardModel') "v28 Install-GuardModel function present"
Assert ($full -match 'function Get-Sha256SumsMap') "v28 Get-Sha256SumsMap helper present"
Assert ($full -match 'python\\models\\decision_bert') "v28 targets python\models\decision_bert"
Assert ($full -match [regex]::Escape('https://modelscope.cn/api/v1/models')) "v28 uses the ModelScope file-direct API base"
Assert ($full -match [regex]::Escape('repo?Revision=master&FilePath=')) "v28 uses Revision/FilePath file-direct URL shape"
# fp16 table (final correction)
Assert ($full -match [regex]::Escape('"decision_bert.onnx"')) "v28 pins the fp16 onnx"
Assert ($full -match [regex]::Escape('"decision_bert.onnx.data"')) "v28 pins the fp16 .onnx.data (external-data pair)"
Assert ($full -match [regex]::Escape('"calibrator.json"')) "v28 pins the fp16 calibrator"
Assert ($full -match [regex]::Escape('321505')) "v28 fp16 onnx byte size present"
Assert ($full -match [regex]::Escape('205880832')) "v28 fp16 .data byte size present"
Assert ($full -match [regex]::Escape('0757d5bec4321c8c13a42c0afef9c1b47c0ef2e78b564791ff3196b81864a89b')) "v28 fp16 onnx sha256 present"
Assert ($full -match [regex]::Escape('ed14e73b19adeaff711f1d3a20bffa2a681546bc1ef6e1c7ff34d61a645cc629')) "v28 fp16 .data sha256 present"
Assert ($full -match [regex]::Escape('9809736bfde493c557837f5ec13142acba03d9a8b80ec9464fa787b60367597c')) "v28 fp16 calibrator sha256 present"
Assert ($full -match [regex]::Escape('015d0ec928f26e28530d6c9399bdfd727ca39c04d34bc1b6a8ab10662a5208d7')) "v28 fp16 tokenizer sha256 present"
Assert ($full -notmatch 'decision_bert_v[0-9]_int8\.onnx') "v28 stale v4/v6-int8 filenames GONE"
Assert ($full -match 'Install-GuardModel -ModelDir') "v28 stage is invoked at the install path"
Assert ($full -match 'guard model:  DEGRADED') "v28 completion report has a DEGRADED line"
Assert (-not ($full -match 'Import-Module\s+modelscope|pip\s+install\s+modelscope')) "v28 adds NO modelscope SDK dependency"
Assert ($full -match [regex]::Escape('$guardModelNetwork = (-not $Offline) -and (-not $SkipModelDownload)')) "v28 -Offline/-SkipModelDownload gate the network"

# ---- 3/4. BEHAVIOR tests ----
function Extract-Function([string]$Name) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    if (-not $fn) { throw "function $Name not found" }
    return $fn.Extent.Text
}
# dependencies of Install-GuardModel that live at top level
Invoke-Expression (Extract-Function 'Get-Sha256SumsMap')
Invoke-Expression (Extract-Function 'Get-ModelScopeText')
Invoke-Expression (Extract-Function 'Install-GuardModel')
# sha256 helper (verbatim from install.ps1)
Invoke-Expression (Extract-Function 'Get-FileSha256')

# 3. Get-Sha256SumsMap parsing
$m = Get-Sha256SumsMap "abc`n0757d5bec4321c8c13a42c0afef9c1b47c0ef2e78b564791ff3196b81864a89b  decision_bert.onnx`nC147...  short`n`n1111111111111111111111111111111111111111111111111111111111111111  ./vocab.txt`n"
Assert ($m.Count -eq 2) "Get-Sha256SumsMap parses 2 valid lines (skips junk + blank)"
Assert ($m['decision_bert.onnx'] -eq '0757d5bec4321c8c13a42c0afef9c1b47c0ef2e78b564791ff3196b81864a89b') "Get-Sha256SumsMap maps onnx hash"
Assert ($m['vocab.txt'] -eq '1111111111111111111111111111111111111111111111111111111111111111') "Get-Sha256SumsMap handles ./-prefixed names"

# reset the $Script:GuardModel* table + $ModelScopeRepo for behavior runs
function Reset-GuardState {
    $Script:GuardModelDir = $null
    $Script:GuardModelOk = 0
    $Script:GuardModelMissing = @()
    $Script:GuardModelUnverified = @()
}

# mock downloader: writes bytes from a script-scoped table
$Script:MockFiles = @{}     # name -> byte[] to "download"
$Script:MockFail = $false   # when true, "downloads" fail (throw)
function Save-DownloadFile([string]$Url, [string]$Out) {
    if ($Script:MockFail) { throw "mock network failure" }
    $name = [uri]::UnescapeDataString(($Url -replace '^.*FilePath=',''))
    if (-not $Script:MockFiles.ContainsKey($name)) { throw "mock 404: $name" }
    [IO.File]::WriteAllBytes($Out, $Script:MockFiles[$name])
}

function Get-FileSha256Fn([byte[]]$B) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($B))).Replace('-','').ToLower() } finally { $sha.Dispose() }
}
$rng = New-Object Random(42)
function New-Bytes([int]$N) { $b = New-Object byte[] $N; $rng.NextBytes($b); return ,$b }
# small stand-ins for speed (calibrator 1113 B real size; .onnx 321505 B
# real size -- both cheap). Sizes are overridden per scenario with the
# FAKE matching table so the logic is exercised without 196 MiB.
$calBytes = New-Bytes 1113
$onnBytes = New-Bytes 321505
$calHash  = Get-FileSha256Fn $calBytes
$onnHash  = Get-FileSha256Fn $onnBytes

# scenario helper: point GuardModelFiles at a fake table
function Set-FakeTable([object[]]$T) { $Script:GuardModelFiles = $T }

function New-TmpDir {
    $d = Join-Path ([IO.Path]::GetTempPath()) ("v28guard-" + [guid]::NewGuid().ToString("N").Substring(0,8))
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}

$base = New-TmpDir
try {
    # --- scenario A: verified local files never touched ---
    Reset-GuardState
    $dirA = Join-Path $base "A"; New-Item -ItemType Directory -Path $dirA -Force | Out-Null
    $dstCal = Join-Path $dirA "calibrator.json"
    [IO.File]::WriteAllBytes($dstCal, $calBytes)
    $beforeWrite = (Get-Item -LiteralPath $dstCal).LastWriteTimeUtc
    Set-FakeTable @(
        @{ Name = "calibrator.json"; Bytes = [long]1113; Sha256 = $calHash; Required = $true }
    )
    $Script:MockFiles = @{}   # nothing downloadable: proves no re-download
    $ModelScopeRepo = "owner/repo"
    Install-GuardModel -ModelDir $dirA -AllowNetwork:$true
    Assert ($Script:GuardModelOk -eq 1 -and $Script:GuardModelMissing.Count -eq 0) "A: verified local file counted OK"
    Assert ((Get-Item -LiteralPath $dstCal).LastWriteTimeUtc -eq $beforeWrite) "A: verified local file NOT re-downloaded"

    # --- scenario B: corrupt local file, network serves good bytes -> self-heal ---
    Reset-GuardState
    $dirB = Join-Path $base "B"; New-Item -ItemType Directory -Path $dirB -Force | Out-Null
    $bad = $calBytes.Clone(); $bad[10] = $bad[10] -bxor 0xFF
    [IO.File]::WriteAllBytes((Join-Path $dirB "calibrator.json"), $bad)
    Set-FakeTable @(
        @{ Name = "calibrator.json"; Bytes = [long]1113; Sha256 = $calHash; Required = $true }
    )
    $Script:MockFiles = @{ "calibrator.json" = $calBytes }
    Install-GuardModel -ModelDir $dirB -AllowNetwork:$true
    $healed = Get-FileSha256 (Join-Path $dirB "calibrator.json")
    Assert ($healed -eq $calHash) "B: corrupt file self-healed by re-download (sha256 restored)"
    Assert ($Script:GuardModelOk -eq 1 -and $Script:GuardModelMissing.Count -eq 0) "B: healed file counted OK"
    Assert (-not (Test-Path -LiteralPath ((Join-Path $dirB "calibrator.json") + ".download"))) "B: no leftover .download temp file"

    # --- scenario C: network always bad -> fail-soft, MISSING, no throw ---
    Reset-GuardState
    $dirC = Join-Path $base "C"; New-Item -ItemType Directory -Path $dirC -Force | Out-Null
    Set-FakeTable @(
        @{ Name = "calibrator.json"; Bytes = [long]1113; Sha256 = $calHash; Required = $true }
    )
    $bad2 = $calBytes.Clone(); $bad2[5] = $bad2[5] -bxor 0x01
    $Script:MockFiles = @{ "calibrator.json" = $bad2 }  # server serves wrong bytes
    $threw = $false
    try { Install-GuardModel -ModelDir $dirC -AllowNetwork:$true } catch { $threw = $true }
    Assert (-not $threw) "C: fail-soft -- Install-GuardModel NEVER throws on persistent mismatch"
    Assert ($Script:GuardModelMissing -contains "calibrator.json") "C: persistent mismatch recorded as MISSING"
    Assert (-not (Test-Path -LiteralPath (Join-Path $dirC "calibrator.json"))) "C: bad download NOT left in place"

    # --- scenario D: offline, all missing -> all required MISSING, no throw ---
    Reset-GuardState
    $dirD = Join-Path $base "D"; New-Item -ItemType Directory -Path $dirD -Force | Out-Null
    Set-FakeTable @(
        @{ Name = "decision_bert.onnx";      Bytes = [long]10; Sha256 = "0" * 64; Required = $true }
        @{ Name = "decision_bert.onnx.data"; Bytes = [long]10; Sha256 = "1" * 64; Required = $true }
    )
    $threw = $false
    try { Install-GuardModel -ModelDir $dirD -AllowNetwork:$false } catch { $threw = $true }
    Assert (-not $threw) "D: offline never throws"
    Assert ($Script:GuardModelMissing.Count -eq 2) "D: both required files recorded MISSING offline"

    # --- scenario E: .onnx present, .onnx.data absent -> .data MISSING ---
    Reset-GuardState
    $dirE = Join-Path $base "E"; New-Item -ItemType Directory -Path $dirE -Force | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $dirE "decision_bert.onnx"), $onnBytes)
    Set-FakeTable @(
        @{ Name = "decision_bert.onnx";      Bytes = [long]321505; Sha256 = $onnHash; Required = $true }
        @{ Name = "decision_bert.onnx.data"; Bytes = [long]10;     Sha256 = "2" * 64; Required = $true }
    )
    $Script:MockFiles = @{}   # no network content: offline-equivalent verdict
    Install-GuardModel -ModelDir $dirE -AllowNetwork:$false
    Assert ($Script:GuardModelMissing -contains "decision_bert.onnx.data") "E: missing .onnx.data is MISSING (external-data pair must be complete)"
    Assert ($Script:GuardModelOk -eq 1) "E: present verified .onnx still OK"

    # --- scenario F: no expected sha256 (not pinned, no SHA256SUMS) -> UNVERIFIED ---
    Reset-GuardState
    $dirF = Join-Path $base "F"; New-Item -ItemType Directory -Path $dirF -Force | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $dirF "tokenizer.json"), $calBytes)
    Set-FakeTable @(
        @{ Name = "tokenizer.json"; Bytes = [long]1113; Sha256 = $null; Required = $true }
    )
    Install-GuardModel -ModelDir $dirF -AllowNetwork:$false
    Assert ($Script:GuardModelMissing.Count -eq 0) "F: size-matched no-hash file is NOT MISSING"
    Assert ($Script:GuardModelUnverified -contains "tokenizer.json") "F: size-matched no-hash file reported UNVERIFIED"
    Assert ((Get-Item -LiteralPath (Join-Path $dirF "tokenizer.json")).Length -eq 1113) "F: unverified local file left in place"

    # --- scenario G: repo code untouched + WRONG local SHA256SUMS wins ---
    Reset-GuardState
    $dirG = Join-Path $base "G"; New-Item -ItemType Directory -Path $dirG -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $dirG "SHA256SUMS"), ("{0}  calibrator.json`n" -f ("9" * 64)))
    [IO.File]::WriteAllBytes((Join-Path $dirG "calibrator.json"), $calBytes)
    [IO.File]::WriteAllText((Join-Path $dirG "serve.py"), "# repo code")
    [IO.File]::WriteAllText((Join-Path $dirG "README.md"), "# readme")
    Set-FakeTable @(
        @{ Name = "calibrator.json"; Bytes = [long]1113; Sha256 = $calHash; Required = $true }
    )
    $Script:MockFiles = @{ "calibrator.json" = $calBytes }
    Install-GuardModel -ModelDir $dirG -AllowNetwork:$true
    # local sums say 999.. (wrong) -> file fails -> network serves the SAME
    # bytes -> still 999.. mismatch -> missing. That proves SHA256SUMS won.
    Assert ($Script:GuardModelMissing -contains "calibrator.json") "G: local SHA256SUMS takes priority over the built-in table"
    Assert ((Get-Item -LiteralPath (Join-Path $dirG "serve.py")).Length -eq 11) "G: serve.py untouched"
    Assert ((Get-Item -LiteralPath (Join-Path $dirG "README.md")).Length -eq 8) "G: README.md untouched"
    Assert (Test-Path -LiteralPath (Join-Path $dirG "SHA256SUMS")) "G: user's SHA256SUMS untouched"
} finally {
    Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ""
Write-Output ("v28 selfcheck: {0} PASS, {1} FAIL" -f $script:pass, $script:fail)
if ($script:fail -gt 0) { exit 1 } else { exit 0 }
