param()
# v17 self-check for the -PurgeData / -Yes fixes (task t_79cfad8e).
# Runs on pwsh 7 (Linux). Extracts the v17 functions from install.ps1
# via AST and runs real scenario tests against a fake filesystem.
$ErrorActionPreference = "Stop"
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

$wanted = @("Resolve-DataDir", "Write-DataOwnershipMarker", "Test-DataDirBelongsToInstall",
            "Invoke-PurgeData", "Convert-PathForCompare", "Test-PathUnder", "Test-PathSafe",
            "Remove-DirectoryClean", "Resolve-PathSafe")
$fnAsts = @($ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $args[0].Name }, $true))
Write-Output ("v17 functions found: " + (($fnAsts | ForEach-Object { $_.Name }) -join ", "))
$src = ""
foreach ($fn in $fnAsts) { $src += $fn.Extent.Text + "`n" }

$yesParam = ($ast.Extent.Text -match '\[switch\]\$Yes')
Write-Output "param -Yes present: $yesParam"
$purgeBlock = ($ast.Extent.Text -match 'dataNote = Join-Path \$env:APPDATA')
Write-Output "legacy hardcoded dataNote purge: $purgeBlock (must be False)"

$pass = 0; $fail = 0
function Assert([bool]$Cond, [string]$Name) {
    if ($Cond) { $script:pass++; Write-Output "PASS: $Name" }
    else { $script:fail++; Write-Output "FAIL: $Name" }
}

# ---- fake environment ----
$fakeHome = Join-Path ([IO.Path]::GetTempPath()) ("v17fake-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $fakeHome -Force | Out-Null
$env:APPDATA = $fakeHome
$Script:AppDirName = "CodeAgent"
$Script:DataOwnershipFile = ".codeagent-install"
$Script:UninstallRecordedDataDir = ""
function Test-DirWritable([string]$Dir) { return $true }

Invoke-Expression $src

$root = Join-Path ([IO.Path]::GetTempPath()) ("v17self-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $root -Force | Out-Null
function New-Dir([string]$p) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
function Rm-Tree([string]$p) { if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force } }
function Write-Marker([string]$installDir, [string]$dataDir) {
    # exactly what the install side now writes
    New-Dir $dataDir
    @{ app = $Script:AppDirName; installDir = $installDir; markedAt = "2026-10-12T00:00:00" } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dataDir $Script:DataOwnershipFile) -Encoding UTF8
    New-Dir $installDir
    @{ installed = $true; installedAt = "2026-10-12T00:00:00"; installDir = $installDir; dataDir = $dataDir } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installDir ".install-state.json") -Encoding UTF8
}

# ---------- a) explicit -DataDir install + purge ----------
Write-Output "`n=== scenario a) explicit DataDir ==="
$instA = Join-Path $root "ca-a"
$dataA = Join-Path $root "ca-a-data"
Write-Marker $instA $dataA
$Script:UninstallRecordedDataDir = $dataA
New-Item -ItemType Directory -Path (Join-Path $dataA "sub") -Force | Out-Null
Set-Content -LiteralPath (Join-Path $dataA "sub\f.txt") "x"
Rm-Tree $instA
$ok = Invoke-PurgeData $dataA $instA $false
Assert $ok "a) purge returns true"
Assert (-not (Test-PathSafe $dataA)) "a) explicit data dir actually deleted"

# ---------- b) default install (no DataDir) + purge ----------
Write-Output "`n=== scenario b) default DataDir ==="
$defaultData = Join-Path $env:APPDATA $Script:AppDirName
New-Dir $defaultData
$instB = Join-Path $root "ca-b"
Write-Marker $instB $defaultData
$Script:UninstallRecordedDataDir = $defaultData
Rm-Tree $instB
$ok = Invoke-PurgeData "" $instB $false
Assert $ok "b) purge returns true"
Assert (-not (Test-PathSafe $defaultData)) "b) default data dir deleted"

# ---------- c) ownership mismatch -> refuse ----------
Write-Output "`n=== scenario c) ownership mismatch ==="
$instC = Join-Path $root "ca-c"
$dataB = Join-Path $root "ca-other-data"
$instOther = Join-Path $root "ca-other"
New-Dir $dataB
@{ app = "CodeAgent"; installDir = $instOther; markedAt = "2026-10-12" } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dataB $Script:DataOwnershipFile) -Encoding UTF8
New-Dir $instOther
Set-Content -LiteralPath (Join-Path $instOther ".keep") "x"
$Script:UninstallRecordedDataDir = ""
$ok = Invoke-PurgeData $dataB $instC $false
Assert (-not $ok) "c) refused (returns false -> INCOMPLETE exit 1)"
Assert (Test-PathSafe $dataB) "c) mismatched data dir SURVIVES"

# ---------- c2) explicit but unprovable -> refuse ----------
Write-Output "`n=== scenario c2) explicit but unprovable ==="
$dataUnmarked = Join-Path $root "unmarked-data"
New-Dir $dataUnmarked
Set-Content -LiteralPath (Join-Path $dataUnmarked "user.db") "x"
$Script:UninstallRecordedDataDir = ""
$ok = Invoke-PurgeData $dataUnmarked $instC $false
Assert (-not $ok) "c2) refused"
Assert (Test-PathSafe $dataUnmarked) "c2) unmarked data dir SURVIVES"

# ---------- d) Resolve-DataDir rule set ----------
Write-Output "`n=== scenario d) resolution rules ==="
$r1 = Resolve-DataDir "D:\explicit" $null
Assert ($r1.DataDir -eq "D:\explicit" -and $r1.Source -eq "explicit") "d1) explicit wins"
$instD = Join-Path $root "ca-d"
New-Dir $instD
@{ dataDir = "E:\recorded" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $instD ".install-state.json") -Encoding UTF8
$r2 = Resolve-DataDir "" $instD
Assert ($r2.DataDir -eq "E:\recorded" -and $r2.Source -eq "record") "d2) install record beats default"
$r3 = Resolve-DataDir "" (Join-Path $root "no-such-install")
Assert ($r3.DataDir -eq $defaultData -and $r3.Source -eq "default") "d3) falls back to default"

# ---------- e) -Yes prompt suppression: parse-level guarantee ----------
Write-Output "`n=== scenario e) -Yes parse checks ==="
$txt = $ast.Extent.Text
$confirmIdx = $txt.IndexOf("function Confirm-InstallDir")
$confirmEnd = $txt.IndexOf("function Get-UrlText")
$confirmBody = $txt.Substring($confirmIdx, $confirmEnd - $confirmIdx)
Assert ($confirmBody -match '\$Yes') "e1) Confirm-InstallDir honors -Yes"
$phaseIdx = $txt.IndexOf("Phase 0 found issues")
$phaseBody = $txt.Substring($phaseIdx, 800)
Assert ($phaseBody -match '\$Yes') "e2) Phase-0 continue prompt honors -Yes"
$interIdx = $txt.IndexOf("interactive default")
$interBody = $txt.Substring($interIdx, 700)
Assert ($interBody -match '\$Yes') "e3) interactive default prompt honors -Yes"

# ---------- f) marker recorded at install ----------
Write-Output "`n=== scenario f) install marker carries dataDir ==="
$instF = Join-Path $root "ca-f"
$dataF = Join-Path $root "ca-f-data"
New-Dir $dataF
$Script:ResolvedDataDir = (Resolve-DataDir $dataF $instF).DataDir
if (Test-PathSafe $Script:ResolvedDataDir) { Write-DataOwnershipMarker $Script:ResolvedDataDir $instF }
New-Dir $instF
@{ installed = $true; installedAt = "s"; installDir = $instF; dataDir = $Script:ResolvedDataDir } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $instF ".install-state.json") -Encoding UTF8
$rec = Get-Content -LiteralPath (Join-Path $instF ".install-state.json") -Raw | ConvertFrom-Json
Assert ([string]$rec.dataDir -eq $dataF) "f1) .install-state.json records dataDir"
Assert (Test-PathSafe (Join-Path $dataF $Script:DataOwnershipFile)) "f2) ownership marker written into data dir"
$Script:UninstallRecordedDataDir = $dataF
$ok = Invoke-PurgeData "" $instF $false
Assert $ok "f3) record-based purge succeeds after fresh install"
Assert (-not (Test-PathSafe $dataF)) "f4) data dir gone"

Rm-Tree $root
Rm-Tree $fakeHome
Write-Output "`nRESULT: pass=$pass fail=$fail"
if ($fail -gt 0) { exit 1 } else { exit 0 }
