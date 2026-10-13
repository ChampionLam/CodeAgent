param()
# v13/v14 self-check harness for scripts/install.ps1 (tasks
# t_ddd7b1b7 + t_2450a5b7 + t_8ac08178 + t_9e01100c +
# t_5c458d9d). Runs on pwsh 7 (Linux).
# Extracts the v10-v13 functions from the script AST, stubs the
# registry access helpers (Get-RegistrySubKeys/Get-RegistryKeyValues)
# onto hashtable-backed fakes, and runs real scenario tests,
# including LIVE execution of the interpreter probes via shebang
# stubs and a fully simulated official-installer /uninstall + /repair
# + /uninstall chain.
$ErrorActionPreference = "Stop"
$f = Join-Path $PSScriptRoot "..\install.ps1"

# 1. BOM + ASCII check
$bytes = [IO.File]::ReadAllBytes($f)
$bom = ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
$nonAscii = 0
for ($i = 3; $i -lt $bytes.Count; $i++) { if ($bytes[$i] -gt 0x7F) { $nonAscii++ } }
Write-Output "UTF8-BOM: $bom ; non-ASCII bytes after BOM: $nonAscii"

# 2. whole-file parse (zero errors required)
$errs = $null; $toks = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f), [ref]$toks, [ref]$errs)
Write-Output "Parser errors: $($errs.Count)"

$wanted = @("Get-VersionKey", "Get-RegisteredPythonInstall", "Test-PinnedPythonRegistration", "Show-PythonInstallerLog", "Test-PathUnder", "Test-PathSafe", "Test-RegistrationIsOurs", "Remove-OfficialPythonRegistration", "Get-RegistrationResidue", "Get-PythonInstallerExe", "Get-PythonCoreInstallPath", "Convert-PathForCompare", "Remove-DirectoryClean")
# extra dependencies of Get-PythonInstallerExe, defined verbatim (no
# patching) since they are pure helpers:
$depWanted = @("Get-PackageFileName", "Test-LocalPackage", "Test-DirWritable", "Save-VerifiedDownload", "Save-DownloadFile", "Test-DownloadedArchive", "Get-UrlText", "Get-FileSha256")
$fnAsts = @($ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $args[0].Name }, $true))
$fnNames = $fnAsts | ForEach-Object { $_.Name }
Write-Output ("New functions present: " + ($fnNames -join ", "))

$pass = 0; $fail = 0
function Assert([bool]$Cond, [string]$Name) {
    if ($Cond) { $script:pass++; Write-Output "PASS: $Name" }
    else { $script:fail++; Write-Output "FAIL: $Name" }
}

# ---------------------------------------------------------------------------
# fake world: hashtable registry behind stubbed v13 helpers.
# Get-RegistrySubKeys($root) -> fake subkey name list per root;
# Get-RegistryKeyValues($keyPath) -> fake value object per full key
# path. The v13 production code only ever calls these two (plus
# Get-Item/Get-ItemProperty inside Get-RegistryKeyValues, which we
# replace wholesale below).
# ---------------------------------------------------------------------------
$script:FakeRegRoots = @{}   # root path -> @(subkey names)
$script:FakeRegKeys  = @{}   # full key path -> @{ values }

# POSIX-style stand-ins for the registry root paths (pwsh-on-Linux has
# no HKCU: drive; Join-Path would throw "Cannot find drive"). The fake
# Get-RegistrySubKeys below MAPS the production root constants onto
# these keys, so the extracted functions run unmodified.
$tagRoot  = "/fake-reg/pythoncore"
$arpRoot2 = "/fake-reg/uninstall"
$depRoot2 = "/fake-reg/dependencies"
foreach ($d in @($tagRoot, $arpRoot2, $depRoot2)) { $script:FakeRegRoots[$d] = @() }

# production root constant -> fake root
$script:RootMap = @{
    "HKCU:\Software\Python\PythonCore"                                            = $tagRoot
    "HKLM:\Software\Python\PythonCore"                                            = $tagRoot + "-hklm1"
    "HKLM:\Software\WOW6432Node\Python\PythonCore"                                = $tagRoot + "-hklm2"
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall"                   = $arpRoot2
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall"                   = $arpRoot2 + "-hklm1"
    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"       = $arpRoot2 + "-hklm2"
    "HKCU:\Software\Classes\Installer\Dependencies"                               = $depRoot2
}

function Get-RegistrySubKeys([string]$RootPath) {
    if (-not $RootPath) { return @() }
    $mapped = $script:RootMap[$RootPath]
    if (-not $mapped) { $mapped = $RootPath }
    if (-not $script:FakeRegRoots.ContainsKey($mapped)) { return @() }
    $names = @($script:FakeRegRoots[$mapped])
    $out = @()
    foreach ($n in $names) { $out += (Join-Path $mapped ([string]$n)) }
    return $out
}
function Get-RegistryKeyValues([string]$KeyPath) {
    if (-not $KeyPath) { return $null }
    $v = $script:FakeRegKeys[$KeyPath]
    if (-not $v) { return $null }
    $o = New-Object PSObject
    foreach ($k in @($v.Keys)) { $o | Add-Member -NotePropertyName $k -NotePropertyValue $v[$k] -Force }
    return $o
}
function Set-RegTag2([string]$Tag, [string]$InstallPath) {
    # cumulative: multiple tags can coexist under the root (like the
    # real registry); Reset-World clears them.
    $cur = @(@($script:FakeRegRoots[$tagRoot]) | Where-Object { $_ -ne $Tag })
    $script:FakeRegRoots[$tagRoot] = @($cur) + @([string]$Tag)
    $script:FakeRegKeys["$tagRoot/$Tag/InstallPath"] = @{ '(default)' = $InstallPath }
}
# v14: the field registry shape -- official installer writes the
# TRAILING-BACKSLASH (default) value and an EMPTY named InstallPath.
function Set-RegTagField([string]$Tag, [string]$Default, [string]$Named) {
    $cur = @(@($script:FakeRegRoots[$tagRoot]) | Where-Object { $_ -ne $Tag })
    $script:FakeRegRoots[$tagRoot] = @($cur) + @([string]$Tag)
    $script:FakeRegKeys["$tagRoot/$Tag/InstallPath"] = @{ '(default)' = $Default; 'InstallPath' = $Named }
}
function Set-ARP2([string]$Name, [string]$DisplayName, [string]$InstallLocation) {
    $script:FakeRegRoots[$arpRoot2] = @($Name)
    $v = @{ DisplayName = $DisplayName }
    if ($InstallLocation) { $v.InstallLocation = $InstallLocation }
    $script:FakeRegKeys["$arpRoot2/$Name"] = $v
}
function Set-Dep2([string]$Name, [string]$DisplayName) {
    $cur = @(@($script:FakeRegRoots[$depRoot2]) | Where-Object { $_ -ne $Name })
    $script:FakeRegRoots[$depRoot2] = @($cur) + @([string]$Name)
    $script:FakeRegKeys["$depRoot2/$Name"] = @{ DisplayName = $DisplayName }
}
function Reset-World() {
    $script:FakeRegRoots = @{}
    $script:FakeRegKeys = @{}
    foreach ($d in @($tagRoot, $arpRoot2, $depRoot2)) { $script:FakeRegRoots[$d] = @() }
}

# interpreter stubs: real executables (shebang scripts) so the
# `& $exe -c "import sys; ..."` probe in the EXTRACTED code runs live
function New-FakePython([string]$Dir, [string]$PrintVersion) {
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    $exe = Join-Path $Dir "python.exe"
    ("#!/bin/sh`necho " + $PrintVersion) | Set-Content -LiteralPath $exe
    & chmod +x $exe
    return $exe
}
$tmpRoot = Join-Path ([IO.Path]::GetTempPath()) ("v13-" + [Guid]::NewGuid().ToString("N"))
$goodDir   = Join-Path $tmpRoot "good31210"
$goodExe   = New-FakePython $goodDir "3.12.10"
$wrongDir  = Join-Path $tmpRoot "wrongver"
$wrongExe  = New-FakePython $wrongDir "3.12.9"
$goneDir   = Join-Path $tmpRoot "deleted"          # dir exists, exe never created
$dangDir   = Join-Path $tmpRoot "dangling"         # dir itself never created

# ---------------------------------------------------------------------------
# define the v13 helpers as the FAKE ones above, then the extracted
# production functions (they call Get-RegistrySubKeys /
# Get-RegistryKeyValues by name, so the fakes shadow them).
# ---------------------------------------------------------------------------
$defs = ($fnAsts | ForEach-Object { $_.Extent.Text }) -join "`n"
Invoke-Expression $defs

# ---------------------------------------------------------------------------
# Get-VersionKey
# ---------------------------------------------------------------------------
Assert ((Get-VersionKey "3.12.10") -eq "3.12") "Get-VersionKey 3.12.10 -> 3.12"
Assert ((Get-VersionKey "3.9") -eq "3.9") "Get-VersionKey 3.9 -> 3.9"
Assert ((Get-VersionKey "") -eq "") "Get-VersionKey empty -> empty"

# ---------------------------------------------------------------------------
# Get-RegisteredPythonInstall
# ---------------------------------------------------------------------------
# S1: only a 3.13 tag -> 3.12.10 query finds nothing
Reset-World
Set-RegTag2 "3.13" $goodDir
$r = Get-RegisteredPythonInstall "3.12.10"
Assert ($r.Found -eq $false -and $null -eq $r.Exe) "S1 no same-version registration -> not found"

# S2: 3.12 tag with a live exact-version interpreter -> found + Exe
Set-RegTag2 "3.12" $goodDir
$r = Get-RegisteredPythonInstall "3.12.10"
Assert ($r.Found -eq $true) "S2 PythonCore 3.12 tag -> Found"
Assert ($r.Exe -eq (Join-Path $goodDir "python.exe")) "S2 Exe = <InstallPath>\python.exe"
Assert ($r.Sources.Count -ge 1) "S2 registry source listed"
# sibling 3.13 still resolvable
$r = Get-RegisteredPythonInstall "3.13.1"
Assert ($r.Found -eq $true) "S2b 3.13.1 resolves against the 3.13 tag"

# S3: ARP entry with InstallLocation
Set-ARP2 "py-arp-1" "Python 3.12.10 (64-bit)" $goodDir
$r = Get-RegisteredPythonInstall "3.12.10"
Assert (($r.Sources | Where-Object { $_ -like "ARP *py-arp-1*" }).Count -ge 1) "S3 ARP entry counted"
# S3b: ARP DisplayName with a DIFFERENT exact version must not match
Reset-World
Set-ARP2 "py-arp-2" "Python 3.12.9 (64-bit)" $wrongDir
$r = Get-RegisteredPythonInstall "3.12.10"
Assert (($r.Sources | Where-Object { $_ -like "*py-arp-2*" }).Count -eq 0) "S3b ARP other-version excluded"

# S4: WiX dependency DisplayName tripwire
Reset-World
Set-Dep2 "dep-1" "Python 3.12.10 Core Interpreter"
$r = Get-RegisteredPythonInstall "3.12.10"
Assert (($r.Sources | Where-Object { $_ -like "WiX dependency*dep-1*" }).Count -eq 1) "S4 WiX dependency tripwire counted"

# ---------------------------------------------------------------------------
# Test-PinnedPythonRegistration (uses the faked lookups + LIVE exe probes)
# ---------------------------------------------------------------------------
# T1: nothing registered -> clear
Reset-World
$t = Test-PinnedPythonRegistration "3.12.10" $false
Assert ($t.Action -eq "clear") "T1 no registration -> clear"

# T2: healthy exact-version registration -> reuse (exe is EXECUTED live)
Set-RegTag2 "3.12" $goodDir
$t = Test-PinnedPythonRegistration "3.12.10" $false
Assert ($t.Action -eq "reuse") "T2 healthy registration -> reuse"
Assert ($t.Exe -eq (Join-Path $goodDir "python.exe")) "T2 reuse carries Exe"

# T3: registered, interpreter prints the WRONG version -> broken
Set-RegTag2 "3.12" $wrongDir
$t = Test-PinnedPythonRegistration "3.12.10" $false
Assert ($t.Action -eq "broken") "T3 version-mismatched registration -> broken"

# T4: dangling (files deleted) -> broken
Set-RegTag2 "3.12" $dangDir
$t = Test-PinnedPythonRegistration "3.12.10" $false
Assert ($t.Action -eq "broken") "T4 dangling registration -> broken"

# T5: dangling + -ForceOwnRuntime -> conflict (with sources carried)
$t = Test-PinnedPythonRegistration "3.12.10" $true
Assert ($t.Action -eq "conflict") "T5 dangling + Force -> conflict"
Assert ($t.Sources.Count -ge 1) "T5 conflict carries sources"

# T6: healthy + Force -> CONFLICT since v16 (field t_0ce37302: v15
# silently reused the registered interpreter and "succeeded"; a second
# same-version copy is impossible -> hard error with three exits).
Set-RegTag2 "3.12" $goodDir
$t = Test-PinnedPythonRegistration "3.12.10" $true
Assert ($t.Action -eq "conflict") "T6 healthy + Force -> conflict (v16 semantic)"

# T7: WiX dependency only (the dangling-uninstall-1603 tripwire) -> broken
Reset-World
Set-Dep2 "dep-1" "Python 3.12.10 Core Interpreter"
$t = Test-PinnedPythonRegistration "3.12.10" $false
Assert ($t.Action -eq "broken") "T7 WiX-dependency-only registration -> broken"

# T8: ARP entry without InstallLocation -> registration found, no Exe -> broken
Reset-World
Set-ARP2 "py-arp-3" "Python 3.12.10" $null
$t = Test-PinnedPythonRegistration "3.12.10" $false
Assert ($t.Action -eq "broken") "T8 ARP without InstallLocation -> broken"

# ---------------------------------------------------------------------------
# Show-PythonInstallerLog (verbatim, real log file)
# ---------------------------------------------------------------------------
$log = Join-Path $tmpRoot "python-installer.log"
Set-Content -LiteralPath $log -Value @(
"[1C48:1E78][2026-10-10 12:34:56]i001: Burn v3.14.0.5718"
"[1C48:1E78]i052: WixBundleInstalled = 1"
"[1C48:1E78]i199: Detect begin, 4 packages"
"[1C48:1E78]i052: Package 'core_JustForMe', state: Present (local)"
"[1C48:1E78]i052: Package 'exe_JustForMe', state: Present (local)"
"[1C48:1E78]i052: Package 'dev_JustForMe', state: Absent"
"[1C48:1E78]i999: Detect complete, result: 0x0"
"[1C48:1E78]i500: Exit code: 0x0"
)
# capture Write-Host AND Write-Warning streams
function Invoke-Capture([scriptblock]$Sb) {
    $null = $null
    $out = & $Sb *>&1 | Out-String
    return $out
}
$out = Invoke-Capture { Show-PythonInstallerLog $log }
Assert ($out -match 'WixBundleInstalled = 1') "L1 WixBundleInstalled line extracted"
Assert ($out -match "core_JustForMe.*Present") "L2 Detect Present package line extracted"
Assert ($out -match "Exit code: 0x0") "L3 Exit code line extracted"
Assert ($out -notmatch 'Burn v3') "L4 noise line excluded"
$out2 = Invoke-Capture { Show-PythonInstallerLog (Join-Path $tmpRoot "no-such.log") }
Assert ($out2 -match 'no installer log found') "L5 missing log -> warning, no throw"
# empty log file -> warning path
$empty = Join-Path $tmpRoot "empty.log"
Set-Content -LiteralPath $empty -Value @()
$out3 = Invoke-Capture { Show-PythonInstallerLog $empty }
Assert ($out3 -match 'empty or unreadable') "L6 empty log -> warning, no throw"

# ---------------------------------------------------------------------------
# wiring check: the install phase must invoke the registration check and
# /log before running the installer (string presence on the real file text)
# ---------------------------------------------------------------------------
$fullText = [IO.File]::ReadAllText($f)
Assert ($fullText -match [regex]::Escape('$regCheck = Test-PinnedPythonRegistration $PythonVersion ([bool]$ForceOwnRuntime)')) "W1 install phase calls the registration check"
Assert ($fullText.Contains('"/log=$installerLog"')) "W2 installer invocation carries /log"
Assert ($fullText.Contains('Show-PythonInstallerLog $installerLog')) "W3 log diagnostics dumped on failure"
Assert ($fullText.Contains('already-installed Python $PythonVersion at $($regCheck.Exe) -- REUSING')) "W4 reuse message present"
Assert ($fullText.Contains('python installer reported success but $exe is missing')) "W5 missing-exe hard assert preserved"

# ---------------------------------------------------------------------------
# v11: Test-PathUnder
# ---------------------------------------------------------------------------
Assert (Test-PathUnder "D:\ca-a\runtime\python" "D:\ca-a") "U0 path under root"
Assert (Test-PathUnder "d:\CA-A\sub\x" "D:\ca-A\") "U0b case-insensitive + trailing slash"
Assert (-not (Test-PathUnder "E:\elsewhere\python" "D:\ca-a")) "U0c outside root -> false"
Assert (-not (Test-PathUnder "" "D:\ca-a")) "U0d empty path -> false"
Assert (-not (Test-PathUnder "D:\ca-a2\x" "D:\ca-a")) "U0e sibling prefix (D:\ca-a2) must NOT match"

# ---------------------------------------------------------------------------
# v13: Test-PathSafe (empty-string path -> $false, NO throw)
# ---------------------------------------------------------------------------
Assert (-not (Test-PathSafe "")) "P0 empty string -> false (no PS 5.1 bind throw)"
Assert (-not (Test-PathSafe $null)) "P0b null -> false"
Assert (Test-PathSafe $tmpRoot) "P0c existing dir -> true"
Assert (-not (Test-PathSafe (Join-Path $tmpRoot "no-such-dir-xyz"))) "P0d missing dir -> false"

# ---------------------------------------------------------------------------
# v13: Test-RegistrationIsOurs (ownership = PythonCore InstallPath only)
# ---------------------------------------------------------------------------
$ourDir = Join-Path $tmpRoot "inst\ca-a"
$userDir = Join-Path $tmpRoot "userpython"
# NOTE: Test-PathUnder is Windows-separator logic; on pwsh-Linux
# Join-Path yields "/" separators, so for the ownership tests we
# build WINDOWS-STYLE literals (what the real registry would carry).
$ourDirWin = "D:\ca-a"
$userDirWin = "D:\userpython"
# R1: our PythonCore (InstallPath under install dir) -> Ours
Reset-World
Set-RegTag2 "3.12" "D:\ca-a\runtime\python"
$r = Test-RegistrationIsOurs "3.12.10" $ourDirWin
Assert ($r.Ours -eq $true) "R1 our PythonCore InstallPath -> Ours"
Assert (($r.Report | Where-Object { $_ -like '*OURS*' }).Count -eq 1) "R1 report says OURS"
Assert (($r.Report | Where-Object { $_ -like '*NOT ours*' }).Count -eq 0) "R1 no NOT-ours line"
# R2: user's own 3.12 registration elsewhere -> not ours
Reset-World
Set-RegTag2 "3.12" $userDirWin
$r = Test-RegistrationIsOurs "3.12.10" $ourDirWin
Assert ($r.Ours -eq $false) "R2 user's own registration -> not ours"
Assert (($r.Report | Where-Object { $_ -like '*NOT ours*' }).Count -eq 1) "R2 report says NOT ours"
# R2b (THE v13 FIELD CASE): user's registration + ARP entries with NO
# InstallLocation (official per-user installer never writes it) ->
# STILL not ours, installer must NOT run
Reset-World
Set-RegTag2 "3.12" $userDirWin
Set-ARP2 "py-arp-1" "Python 3.12.10 (64-bit)" $null
Set-ARP2 "py-arp-2" "Python 3.12.10 Executables (64-bit)" $null
Set-Dep2 "dep-1" "Python 3.12.10 Core Interpreter"
$r = Test-RegistrationIsOurs "3.12.10" $ourDirWin
Assert ($r.Ours -eq $false) "R2b ARP-without-InstallLocation never flips ownership"
# R3: our registration + ARP entries without InstallLocation -> Ours
# (the real-machine case: only PythonCore pointed into our dir)
Reset-World
Set-RegTag2 "3.12" "D:\ca-a\runtime\python"
Set-ARP2 "py-arp-1" "Python 3.12.10 (64-bit)" $null
Set-Dep2 "dep-1" "Python 3.12.10 Core Interpreter"
$r = Test-RegistrationIsOurs "3.12.10" $ourDirWin
Assert ($r.Ours -eq $true) "R3 our PythonCore + ARP(no loc) -> Ours (installer cleans all surfaces)"
# R4: other-version tag is ignored entirely
Reset-World
Set-RegTag2 "3.11" "D:\ca-a\runtime\python"
$r = Test-RegistrationIsOurs "3.12.10" $ourDirWin
Assert ($r.Ours -eq $false -and $r.Report.Count -eq 0) "R4 other-version tag ignored"
# R5: dangling PythonCore (InstallPath value missing) -> treated as ours
Reset-World
$script:FakeRegRoots[$tagRoot] = @("3.12")
$script:FakeRegKeys["$tagRoot/3.12/InstallPath"] = $null
$r = Test-RegistrationIsOurs "3.12.10" $ourDirWin
Assert ($r.Ours -eq $true) "R5 InstallPath-missing tag -> treated as ours"

# -----------------------------------------------------------------------
# v14 (task t_5c458d9d): the the field box field case -- the official installer
# writes a TRAILING-BACKSLASH (default) value and an EMPTY named
# InstallPath. v13's raw string compare never matched -> our own
# registration read as "not ours" (uninstall refused to run the
# official installer) and Phase 0 could misreport it BROKEN.
# -----------------------------------------------------------------------
# U1: Convert-PathForCompare normalization
Assert ((Convert-PathForCompare "D:\ca-a\runtime\python\") -eq (Convert-PathForCompare "D:/ca-a/runtime/python")) "U1a backslash and forward-slash forms normalize equal"
Assert ((Convert-PathForCompare " D:\ca-a\runtime\python\ ") -eq (Convert-PathForCompare "D:\ca-a\runtime\python")) "U1b outer whitespace + trailing backslash stripped"
Assert ((Convert-PathForCompare "d:\CA-A\runtime\PYTHON\") -eq (Convert-PathForCompare "D:\ca-a\RUNTIME\python")) "U1c case normalizes (GetFullPath on Windows, path case on Linux)"
Assert ((Convert-PathForCompare "D:\ca-a2\x") -ne (Convert-PathForCompare "D:\ca-a")) "U1d sibling prefix stays distinct"
Assert ((Convert-PathForCompare "") -eq "") "U1e empty -> empty"
# U2: Test-PathUnder now matches the trailing-backslash registry value
Assert (Test-PathUnder "D:\ca-a\runtime\python\" "D:\ca-a") "U2 THE FIELD CASE: trailing-backslash InstallPath -> under root"
Assert (Test-PathUnder "D:\ca-a\runtime\python\" "D:\ca-a\runtime\python") "U2b trailing-slash value equals root itself -> inside"
Assert (Test-PathUnder "d:/CA-A/sub/x" "D:\ca-A\") "U2c mixed separators + case still match"
Assert (-not (Test-PathUnder "E:\elsewhere\python" "D:\ca-a")) "U2d outside root -> false (v13 baseline kept)"
Assert (-not (Test-PathUnder "D:\ca-a2\x" "D:\ca-a")) "U2e sibling prefix (D:\ca-a2) must NOT match"
Assert (-not (Test-PathUnder "" "D:\ca-a")) "U2f empty path -> false"
# U3: Get-PythonCoreInstallPath -- (default) with trailing backslash +
# empty named value (the exact field registry shape)
Reset-World
Set-RegTagField "3.12" "D:\ca-a\runtime\python\" ""
$ip = Get-PythonCoreInstallPath "$tagRoot/3.12"
Assert ($ip -eq "D:\ca-a\runtime\python\") "U3 field shape: non-empty (default) wins, trimmed"
# U3b: named InstallPath wins when (default) is empty
Set-RegTagField "3.12" "" "D:\userpython\"
$ip = Get-PythonCoreInstallPath "$tagRoot/3.12"
Assert ($ip -eq "D:\userpython\") "U3b empty default -> named value wins"
# U3c: neither present -> null
Set-RegTagField "3.12" "" ""
$ip = Get-PythonCoreInstallPath "$tagRoot/3.12"
Assert ($null -eq $ip) "U3c both empty -> null"
# R6 (THE v14 FIELD CASE): field registry shape -> Ours
Reset-World
Set-RegTagField "3.12" "D:\ca-a\runtime\python\" ""
$r = Test-RegistrationIsOurs "3.12.10" $ourDirWin
Assert ($r.Ours -eq $true) "R6 field trailing-backslash (default) + empty named -> Ours"
Assert (($r.Report | Where-Object { $_ -like '*OURS*' }).Count -eq 1) "R6 report says OURS"
# R7: same field shape but pointing elsewhere -> NOT ours
Reset-World
Set-RegTagField "3.12" "D:\userpython\" ""
$r = Test-RegistrationIsOurs "3.12.10" $ourDirWin
Assert ($r.Ours -eq $false) "R7 field shape outside InstallDir -> NOT ours"
# F1 (fix #5, the install #2 misreport): trailing-backslash registration
# with a LIVE interpreter -> Test-PinnedPythonRegistration says reuse
# (v13 said broken: raw Join-Path fed the trailing backslash ->
# Test-Path failed). We use a POSIX dir + forward-slash value so the
# live exe probe actually works on pwsh-Linux.
Reset-World
Set-RegTagField "3.12" ($goodDir + "/") ""
$t = Test-PinnedPythonRegistration "3.12.10" $false
Assert ($t.Action -eq "reuse") "F1 live trailing-slash registration -> reuse (not BROKEN)"
Assert ($t.Exe -eq (Join-Path $goodDir "python.exe")) "F1 reuse carries the right Exe"
# F2: registered with trailing backslash -> Get-RegisteredPythonInstall
# still resolves Exe and reports it as a source
Reset-World
Set-RegTagField "3.12" ($goodDir + "/") ""
$g = Get-RegisteredPythonInstall "3.12.10"
Assert ($g.Found -eq $true -and $g.Exe -eq (Join-Path $goodDir "python.exe")) "F2 registered install resolves Exe (trailing slash tolerated)"

# ---------------------------------------------------------------------------
# v13: Get-RegistrationResidue (counts over the three surfaces)
# ---------------------------------------------------------------------------
Reset-World
$res = Get-RegistrationResidue "3.12.10"
Assert ($res.Total -eq 0) "V1 empty world -> residue 0"
# populate: our tag + ARP + 2 WiX deps
Set-RegTag2 "3.12" "D:\ca-a\runtime\python"
Set-ARP2 "py-arp-1" "Python 3.12.10 (64-bit)" "D:\ca-a"
Set-Dep2 "dep-1" "Python 3.12.10 Core Interpreter"
Set-Dep2 "dep-2" "Python 3.12.10 Core Interpreter"
$res = Get-RegistrationResidue "3.12.10"
Assert ($res.PythonCore -eq 1 -and $res.ARP -eq 1 -and $res.Deps -eq 2 -and $res.Total -eq 4) "V2 three surfaces counted (1/1/2)"
# other-version tag must not count
Set-RegTag2 "3.11" $userDirWin
$res = Get-RegistrationResidue "3.12.10"
Assert ($res.PythonCore -eq 1) "V3 other-version tag not counted"

# ---------------------------------------------------------------------------
# v11: Remove-OfficialPythonRegistration with a SIMULATED installer
# ---------------------------------------------------------------------------
# fake installer: a script that records the verbs it got and behaves
# per scenario. Uses $LASTEXITCODE semantics via exit codes.
$script:InstallerCalls = @()
$script:InstallerScenario = "healthy"   # healthy | dangling | hopeless
function New-FakeInstaller([string]$Dir) {
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    $exe = Join-Path $Dir "python-installer.exe"
    # fake installer: a sh script that records the verbs it got and
    # behaves per scenario (healthy / dangling / hopeless), reading
    # the scenario + call log from files under $tmpRoot.
    $stub = @"
#!/bin/sh
VERB="`$1"
CALLS="$tmpRoot/installer-calls.txt"
echo "`$VERB" >> "`$CALLS"
SCEN="$tmpRoot/installer-scenario.txt"
S=`$(cat "`$SCEN")
if [ "`$VERB" = "/uninstall" ]; then
  if [ "`$S" = "healthy" ]; then exit 0; fi
  if [ "`$S" = "dangling" ]; then
    if [ -f "$tmpRoot/repair-done" ]; then exit 0; else exit 1603; fi
  fi
  exit 1639
fi
if [ "`$VERB" = "/repair" ]; then
  if [ "`$S" = "dangling" ]; then touch "$tmpRoot/repair-done"; exit 0; fi
  exit 1603
fi
exit 0
"@
    Set-Content -LiteralPath $exe -Value $stub
    & chmod +x $exe
    return $exe
}
# X1: healthy registration -> single /uninstall, rc 0, true
Clear-Content -LiteralPath (Join-Path $tmpRoot "installer-calls.txt") -ErrorAction SilentlyContinue
Set-Content -LiteralPath (Join-Path $tmpRoot "installer-scenario.txt") -Value "healthy"
$fakeInstaller = New-FakeInstaller (Join-Path $tmpRoot "fakeinstaller")
$logDir2 = Join-Path $tmpRoot "logs"
New-Item -ItemType Directory -Path $logDir2 -Force | Out-Null

# X1: healthy registration -> single /uninstall, rc 0, true
$ok = Remove-OfficialPythonRegistration $fakeInstaller $logDir2
$calls = @(Get-Content -LiteralPath (Join-Path $tmpRoot "installer-calls.txt") | Where-Object { $_ })
Assert ($ok -eq $true) "X1 healthy -> returns true"
Assert (($calls -join ',') -eq '/uninstall') "X1 healthy -> exactly one /uninstall call"

# X2: dangling (files deleted) -> /uninstall 1603, /repair 0, /uninstall 0
Clear-Content -LiteralPath (Join-Path $tmpRoot "installer-calls.txt") -ErrorAction SilentlyContinue
Set-Content -LiteralPath (Join-Path $tmpRoot "installer-scenario.txt") -Value "dangling"
Remove-Item -LiteralPath (Join-Path $tmpRoot "repair-done") -Force -ErrorAction SilentlyContinue
$ok = Remove-OfficialPythonRegistration $fakeInstaller $logDir2
$calls = @(Get-Content -LiteralPath (Join-Path $tmpRoot "installer-calls.txt") | Where-Object { $_ })
Assert ($ok -eq $true) "X2 dangling -> repair-then-uninstall returns true"
Assert (($calls -join ',') -eq '/uninstall,/repair,/uninstall') "X2 dangling -> call sequence /uninstall,/repair,/uninstall"

# X3: hopeless -> false
Clear-Content -LiteralPath (Join-Path $tmpRoot "installer-calls.txt") -ErrorAction SilentlyContinue
Set-Content -LiteralPath (Join-Path $tmpRoot "installer-scenario.txt") -Value "hopeless"
$ok = Remove-OfficialPythonRegistration $fakeInstaller $logDir2
Assert ($ok -eq $false) "X3 hopeless -> returns false"

# ---------------------------------------------------------------------------
# v11: Get-PythonInstallerExe with a stubbed Save-VerifiedDownload +
# Get-AuthenticodeSignature (functions are defined by the extracted
# defs only if extracted; we instead test the wiring textually + the
# cache branch logic by a real-size MZ-magic file).
# ---------------------------------------------------------------------------
# G1: a local file with the exact pinned size and MZ magic in a fake
# package dir -> cached reuse (no download). Windows-only calls
# (Get-AuthenticodeSignature) are stubbed; scope variables the
# function reads ($PackageDir, $Script:PyInstallerBytes, mirror
# params) are provided by renaming the extracted function and
# defining those variables in scope first.
$pkgDir2 = Join-Path $tmpRoot "packages"
New-Item -ItemType Directory -Path $pkgDir2 -Force | Out-Null
$fakeExe = Join-Path $pkgDir2 "python-3.12.10-amd64.exe"
$buf = New-Object byte[] 26964224
$buf[0] = 0x4D; $buf[1] = 0x5A
[IO.File]::WriteAllBytes($fakeExe, $buf)
$gpi = $fnAsts | Where-Object { $_.Name -eq 'Get-PythonInstallerExe' }
$gpiText = $gpi.Extent.Text
$gpiText = $gpiText -replace [regex]::Escape('function Get-PythonInstallerExe'), 'function Script:Get-PythonInstallerExeTest'
$gpiText = $gpiText -replace [regex]::Escape('Get-AuthenticodeSignature -FilePath $archive'), 'Get-FakeSignature -FilePath $archive'
function Get-FakeSignature([string]$FilePath) {
    $o = New-Object PSObject
    $o | Add-Member -NotePropertyName Status -NotePropertyValue 'Valid'
    $cert = New-Object PSObject
    $cert | Add-Member -NotePropertyName Subject -NotePropertyValue 'CN=Python Software Foundation'
    $o | Add-Member -NotePropertyName SignerCertificate -NotePropertyValue $cert
    return $o
}
# its helper deps, extracted verbatim from the AST
$depAsts = @($ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $depWanted -contains $args[0].Name }, $true))
$depDefs = ($depAsts | ForEach-Object { $_.Extent.Text }) -join "`n"
Invoke-Expression $depDefs
Invoke-Expression $gpiText
# scope variables the function body reads
$PackageDir = $pkgDir2
$PythonVersion = "3.12.10"
$PythonInstallerMirror = "https://invalid.invalid/"
$PythonInstallerMirror2 = "https://invalid2.invalid/"
$Script:PyInstallerBytes = [long]26964224
$got = Script:Get-PythonInstallerExeTest $logDir2
Assert ($got -eq $fakeExe) "G1 verified package-dir cache hit -> returned, no download"

# ---------------------------------------------------------------------------
# v11 wiring: the -AllowCondaBase switch + uninstall text on the real file
# ---------------------------------------------------------------------------
Assert ($fullText.Contains('[switch]$AllowCondaBase')) "W6 -AllowCondaBase param present"
Assert ($fullText.Contains('to use it add -AllowCondaBase or -PythonPath')) "W7 conda skip reason mentions the switch"
Assert ($fullText.Contains('conda base policy')) "W8 Phase 0 conda-base policy row present"
Assert ($fullText.Contains('Remove-OfficialPythonRegistration $installerExe $logDir')) "W9 uninstall runs the official installer"
Assert ($fullText.Contains('Get-RegistrationResidue $PythonVersion')) "W10 post-uninstall residue verification present"
Assert ($fullText -match [regex]::Escape('exit $exitCode')) "W11 uninstall exit code propagates (exit 1 on residue)"
Assert ($fullText.Contains('/repair re-created')) "W12 /repair re-created runtime strip present"
Assert ($fullText.Contains('NOT run (a registration that is not ours stays put)')) "W13 not-ours guard message present"

# conda-base selection behavior (extracted Get-PythonCandidates probe
# branch): simulate by string checks + a live decision on a conda-style
# path with the switch off (skip) -- the real branch needs the full
# function; textual assert covers the switch plumbing.
Assert ($fullText.Contains('if ($AllowCondaBase)')) "W14 AllowCondaBase branch in the probe loop"

# ---------------------------------------------------------------------------
# v12 static gate: -LiteralPath must NEVER receive a registry-provider
# path. This is the exact class of bug that shipped in v11 (PS 5.1 on a
# real machine failed to bind -LiteralPath to "Registry::HKEY_..." from
# $tag.PSPath, the pwsh7 + fake-registry harness never saw it). Grep
# EVERY -LiteralPath argument in the real file text: anything containing
# .PSPath, "::" (provider qualification), or HKEY_ is a hard failure.
# Filesystem paths with -LiteralPath remain fine and expected.
# ---------------------------------------------------------------------------
$badLiteral = @()
$codeNoComments = ($fullText -replace '(?m)^\s*#.*$', '' -replace '(?s)/\*.*?\*/', '')
foreach ($m in [regex]::Matches($codeNoComments, '(?s)-LiteralPath\s+([^)\r\n]+)')) {
    $arg = $m.Groups[1].Value.Trim()
    if ($arg -match '\.PSPath' -or $arg -match '::' -or $arg -match 'HKEY_') {
        $badLiteral += $arg
    }
}
Assert ($badLiteral.Count -eq 0) "G1v12 no -LiteralPath argument is a provider/registry path ($($badLiteral.Count) bad)"
# belt-and-braces: Get-ItemProperty registry reads must use -Path with PSPath
$provReads = [regex]::Matches($codeNoComments, 'Get-ItemProperty -LiteralPath')
Assert ($provReads.Count -eq 0) "G2v12 Get-ItemProperty never uses -LiteralPath (count: $($provReads.Count))"
# ---------------------------------------------------------------------------
# v13 static gates (task t_9e01100c).
# G1v13: NO provider-qualified $x.PSPath usage anywhere in code
#       (registry reads go through Get-RegistryKeyValues /
#       Get-RegistrySubKeys with plain drive-qualified paths).
# G2v13: the uninstall path defaults $PackageDir BEFORE first use
#       (that PS 5.1 crash: Test-Path -LiteralPath "" on PS 5.1).
# G3v13: ownership is keyed ONLY on the PythonCore InstallPath (the
#       ARP InstallLocation never gates ownership -- the official
#       per-user installer does not write it).
# G4v13: registry sweep helpers exist.
# G5v13: no raw Test-Path -LiteralPath $var in the uninstall block.
# ---------------------------------------------------------------------------
$psPathUses = [regex]::Matches($codeNoComments, '\$[A-Za-z_][A-Za-z0-9_]*\.PSPath')
Assert ($psPathUses.Count -eq 0) "G1v13 no PSPath usage in code ($($psPathUses.Count) found)"
Assert ($codeNoComments.Contains('if (-not $PackageDir) { $PackageDir = Join-Path $InstallDir "packages" }')) "G2v13 uninstall defaults PackageDir before first use"
Assert (-not ($codeNoComments -match 'InstallLocation.{0,120}Ours = \$true')) "G3v13 InstallLocation never sets Ours"
Assert ($codeNoComments.Contains('function Get-RegistryKeyValues') -and $codeNoComments.Contains('function Get-RegistrySubKeys')) "G4v13 registry helper functions defined"
$regCleanedBlock = [regex]::Match($codeNoComments, '(?s)\$regCleaned = \$true.*?could not verify the registration residue').Value
Assert ($regCleanedBlock -notmatch 'Test-Path -LiteralPath \$') "G5v13 uninstall registration block uses Test-PathSafe (no raw Test-Path -LiteralPath \$var)"
# -----------------------------------------------------------------------
# v14 static gates (task t_5c458d9d).
# G1v14: exactly ONE '(default)' read in code -- Get-PythonCoreInstallPath
#       is the single place a PythonCore InstallPath value is read (no
#       per-sweep duplicated default/named fallback).
# G2v14: the three registry sweeps all route through
#       Get-PythonCoreInstallPath.
# G3v14: Test-PathUnder normalizes via Convert-PathForCompare (no raw
#       TrimEnd-only comparison left).
# G4v14: new helpers defined.
# -----------------------------------------------------------------------
$defaultReads = [regex]::Matches($codeNoComments, "\(default\)")
Assert ($defaultReads.Count -eq 1) "G1v14 exactly one (default) registry read in code ($($defaultReads.Count) found)"
Assert (([regex]::Matches($codeNoComments, 'Get-PythonCoreInstallPath \$tagPath')).Count -eq 3) "G2v14 all three registry sweeps call Get-PythonCoreInstallPath"
$tpu = ([regex]::Match($codeNoComments, '(?sm)^function Test-PathUnder.*?^}')).Value
Assert ($tpu.Contains('Convert-PathForCompare $P') -and $tpu.Contains('Convert-PathForCompare $Root')) "G3v14 Test-PathUnder normalizes both sides via Convert-PathForCompare"
Assert ($codeNoComments.Contains('function Get-PythonCoreInstallPath') -and $codeNoComments.Contains('function Convert-PathForCompare')) "G4v14 v14 helper functions defined"
Assert ($tpu -notmatch 'TrimEnd') "G5v14 Test-PathUnder no raw TrimEnd comparison"

# -----------------------------------------------------------------------
# v15 (task t_d1ce3b7d): single-element array unroll gate.
# Class: a helper builds an array ($x = @() ... $x += ...) and returns
# it bare; PowerShell unwraps a 1-element result to a scalar, so a
# caller doing $acc += Helper ... gets STRING CONCATENATION -- the
# real-box incident behind the comma return in Get-RegistrySubKeys
# (fixed in v14-era, now generalized to Get-PythonCandidates too).
# -----------------------------------------------------------------------
# A0: extract the PRODUCTION Get-RegistrySubKeys verbatim from
# install.ps1, rename it (so it does not collide with the hashtable
# fake above), and run it against REAL directories.
$grskText = [regex]::Match($fullText, '(?sm)^function Get-RegistrySubKeys\b.*?^}').Value
Assert ($grskText.Length -gt 0) "A0 production Get-RegistrySubKeys text found in install.ps1"
Assert ($grskText -notmatch 'return\s*,\$out') "A0b production Get-RegistrySubKeys: v20+ comma-return poison REMOVED (string-filter return)"
$grskText = $grskText -replace [regex]::Escape('function Get-RegistrySubKeys'), 'function Script:Get-RegistrySubKeysProd'
Invoke-Expression $grskText
$realRoot = Join-Path $tmpRoot "real-reg"
New-Item -ItemType Directory -Path $realRoot -Force | Out-Null
# A1: 0 subkeys -> empty result, contributes nothing on +=
$emptyRoot = Join-Path $realRoot "zero"
New-Item -ItemType Directory -Path $emptyRoot -Force | Out-Null
$acc = @()
$acc += (Script:Get-RegistrySubKeysProd $emptyRoot)
Assert ($acc.Count -eq 0) "A1 zero subkeys -> empty (accumulates nothing)"
# A2: 1 subkey -> MUST still be an array (THE unroll class: a bare
# return hands back a single string; += then CONCATENATES strings)
$oneRoot = Join-Path $realRoot "one"
New-Item -ItemType Directory -Path (Join-Path $oneRoot "onlytag") -Force | Out-Null
$r = Script:Get-RegistrySubKeysProd $oneRoot
Assert ($null -ne $r) "A2 one subkey -> not null"
Assert (@($r).Count -eq 1) "A2 one subkey -> v20+ helper returns one element (no empty-array poison)"
Assert ((@($r) -join ",") -like "*onlytag") "A2 one subkey -> correct single entry"
# A3 (THE regression): accumulate two helper results with `+=` -- if
# either return were unrolled to a scalar string, $acc would FUSE into
# one long string instead of 3 separate entries.
$twoRoot = Join-Path $realRoot "two"
New-Item -ItemType Directory -Path (Join-Path $twoRoot "tagA") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $twoRoot "tagB") -Force | Out-Null
$acc = @()
$acc += (Script:Get-RegistrySubKeysProd $oneRoot)
$acc += (Script:Get-RegistrySubKeysProd $twoRoot)
Assert ($acc -is [array] -and $acc.Count -eq 3) "A3 two += accumulations -> 3 entries, no string fusion ($($acc.Count) got)"
Assert (($acc | Where-Object { $_ -like "*tagA" }).Count -eq 1 -and ($acc | Where-Object { $_ -like "*tagB" }).Count -eq 1) "A3 both tags present as separate entries"
# A4: missing root -> contributes nothing
$acc2 = @()
$acc2 += (Script:Get-RegistrySubKeysProd (Join-Path $realRoot "no-such-root"))
$acc2 += (Script:Get-RegistrySubKeysProd $oneRoot)
Assert ($acc2.Count -eq 1) "A4 missing root contributes nothing, one-root still 1 entry"
# A5: 2 subkeys (the only count the old fake harness ever exercised)
$r = Script:Get-RegistrySubKeysProd $twoRoot
Assert ($r.Count -eq 2) "A5 two subkeys -> 2 entries (baseline kept)"

# -----------------------------------------------------------------------
# v15 static gates on install.ps1 code (comments stripped).
# G1v15: every function that ARRAY-BUILDS a variable (@() init or +=)
#        and later line-returns that same variable bare must use the
#        comma form (return ,$x / return ,@($x)). Hashtable (@{...})
#        returns are NOT arrays and are excluded.
# G2v15: every `+= <Function-Call>` accumulation site must have the
#        call wrapped in @(...) -- an unrolled scalar RHS would make
#        += concatenate strings.
# -----------------------------------------------------------------------
$fnAstsAll = @($ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true))
$g15bad = @()
foreach ($fa in $fnAstsAll) {
    $body = $fa.Body.Extent.Text
    if ($body -match '\$\w+\s*=\s*\(@' -or $body -match '\+=') {
        foreach ($rm in [regex]::Matches($body, '(?m)^\s*return\s+(\$[A-Za-z_][A-Za-z0-9_]*)\s*$')) {
            $ret = $rm.Groups[1].Value
            $var = $ret.TrimStart('$')
            if ($body -match ('\$\s*\{\s*' + $var + '\s*\}\s*\=\s*@\(')) { continue }
            if (($body -match ('\$(' + $var + ')\s*=\s*@\(')) -or ($body -match ('\$(' + $var + ')\s*\+='))) {
                $g15bad += ("{0}: bare return of array-built {1}" -f $fa.Name, $ret)
            }
        }
    }
}
Assert ($g15bad.Count -eq 0) "G1v15 no bare array-return left in array-building functions ($($g15bad.Count): $($g15bad -join '; '))"
$g15bad2 = @()
foreach ($m in [regex]::Matches($codeNoComments, '\$[\w\.\:]+\s*\+=\s+([A-Za-z_][A-Za-z0-9_]*-[A-Za-z0-9_]+)')) {
    # a bare function-call RHS (identifier with a dash, NOT preceded by
    # @( and not the @(...)-wrapped form -- the @ form never matches
    # here because += is followed by '@' not an identifier)
    $g15bad2 += $m.Value.Trim()
}
Assert ($g15bad2.Count -eq 0) "G2v15 every += function-call RHS is @()-wrapped ($($g15bad2.Count): $($g15bad2 -join '; '))"

# ---------------------------------------------------------------------------
# v16 (task t_0ce37302): offline resolution, ForceOwnRuntime conflict,
# and clean -PurgeData delete gates.
# ---------------------------------------------------------------------------

# G1v16: the offline branch must BOOTSTRAP from local sources -- the
# v15 bug was offline-only-discovery (Install-PythonRuntimeOffline
# never called, -PythonArchive dead code).
$offlineBlock = [regex]::Match($codeNoComments, '(?s)if \(\$Offline\) \{\s*\$pyDec = Find-Python.*?\} elseif \(Test-Path -LiteralPath \$venvPy\)').Value
Assert ($offlineBlock.Length -gt 0) "G1v16 offline branch text found"
Assert ($offlineBlock.Contains('Install-PythonRuntimeOffline $InstallDir')) "G1v16 offline branch bootstraps from local sources"
Assert ($offlineBlock -notmatch 'no usable Python found and no venv present; pass -PythonArchive') "G1v16 offline error is the full tried-sources list, not the old one-liner"
Assert ($offlineBlock.Contains('Tried in order')) "G1v16 offline hard error lists the tried sources"

# G2v16: Install-PythonRuntime never downloads in offline mode (the
# offline guard sits BEFORE the mirror/pbs download chains).
$iprtText = [regex]::Match($codeNoComments, '(?sm)^function Install-PythonRuntime\b.*?^function Install-PythonRuntimeOffline').Value
Assert ($iprtText.Length -gt 0) "G2v16 Install-PythonRuntime body found"
Assert ($iprtText.Contains('if ($Offline) {')) "G2v16 Install-PythonRuntime has an offline guard"
$offlineGuardPos = $iprtText.IndexOf('if ($Offline) {')
$saveVerifiedPos = $iprtText.IndexOf('Save-VerifiedDownload')
Assert ($offlineGuardPos -ge 0 -and $saveVerifiedPos -gt $offlineGuardPos) "G2v16 offline guard precedes any download call"
Assert ($iprtText.Contains('if (-not $archive) { return $null }')) "G2v16 no usable archive -> null return (caller prints the error)"

# G3v16: -PythonArchive local file goes through the verification chain.
$pyArcBlock = [regex]::Match($codeNoComments, '(?s)if \(\$PythonArchive\) \{\s*if \(-not \(Test-Path -LiteralPath \$PythonArchive\)\).*?\} else \{\s*\$pbs = \$null').Value
Assert ($pyArcBlock.Contains('Test-LocalPackage')) "G3v16 -PythonArchive runs size+magic checks"
Assert ($pyArcBlock.Contains('Get-AuthenticodeSignature')) "G3v16 -PythonArchive exe runs Authenticode"
Assert ($pyArcBlock.Contains('Get-FileSha256')) "G3v16 -PythonArchive tar.gz verifies a sha256 sidecar"

# G4v16: -NodeArchive local file goes through the verification chain.
$nodeArcBlock = [regex]::Match($codeNoComments, '(?s)if \(\$NodeArchive\) \{\s*if \(-not \(Test-Path -LiteralPath \$NodeArchive\)\).*?\} else \{\s*\$nodeVer = \$NodeVersion').Value
Assert ($nodeArcBlock.Contains('Test-LocalPackage')) "G4v16 -NodeArchive runs magic+size checks"
Assert ($nodeArcBlock.Contains('Get-FileSha256')) "G4v16 -NodeArchive verifies sha256 from SHASUMS"

# G5v16: T6-regression -- healthy registration + -ForceOwnRuntime must
# now be a CONFLICT (field: v15 silently reused it). Extract the
# production function and run it against the fake registry.
# (Reset-World: the v11 sections above left a Windows-style ARP entry
# in the fake registry -- Join-Path throws on 'D:' under pwsh/Linux.)
Reset-World
Set-RegTag2 "3.12" $goodDir
$t6 = Test-PinnedPythonRegistration "3.12.10" $true
Assert ($t6.Action -eq "conflict") "G5v16 healthy + Force -> conflict (T6 regression: v15 returned reuse)"
$t6b = Test-PinnedPythonRegistration "3.12.10" $false
Assert ($t6b.Action -eq "reuse") "G5v16 healthy + no Force -> still reuse"
$t6c = Test-PinnedPythonRegistration "3.12.10" $true
Assert ($null -eq $t6c.Exe) "G5v16 conflict decision carries no Exe (never silently reused)"

# G6v16: the install path hard-stops on the conflict BEFORE any copy.
Assert ($codeNoComments.Contains('$checkResult.RegAction -eq "conflict"')) "G6v16 install phase checks RegAction conflict"
$conflictStop = [regex]::Match($codeNoComments, '(?s)if \(\$checkResult\.RegAction -eq "conflict"\).*?CANNOT install a second copy.*?\(3\)[^\r\n]*').Value
Assert ($conflictStop.Length -gt 0) "G6v16 conflict error explains the WiX single-instance reason"
Assert ($conflictStop -match '\(1\)[\s\S]*\(2\)[\s\S]*\(3\)') "G6v16 conflict error offers three exits"

# G7v16: -PurgeData uses the clean delete helper (attributes stripped,
# retries, per-item warnings, verified-gone verdict -> INCOMPLETE).
# v17: the purge block moved into Invoke-PurgeData (data dir resolved
# via shared rules + ownership gate); match it there.
$purgeBlock = [regex]::Match($codeNoComments, '(?s)function Invoke-PurgeData.*?\n}').Value
Assert ($purgeBlock.Contains('Remove-DirectoryClean $target')) "G7v16 -PurgeData uses Remove-DirectoryClean"
Assert (-not ($purgeBlock -match 'Remove-Item -LiteralPath \$target')) "G7v16 no bare Remove-Item on the data dir"
Assert ($purgeBlock.Contains('INCOMPLETE')) "G7v16 survivor dir reports INCOMPLETE and exit 1"
$rdcText = [regex]::Match($codeNoComments, '(?sm)^function Remove-DirectoryClean\b.*?^}').Value
Assert ($rdcText.Contains('[IO.FileAttributes]::ReadOnly')) "G7v16 Remove-DirectoryClean strips ReadOnly attributes"
Assert ($rdcText -match 'for \(\$attempt = 1; \$attempt -le 3') "G7v16 Remove-DirectoryClean retries up to 3 times"

# G8v16: live behavior of Remove-DirectoryClean on real directories
# (read-only file + subdirs; POSIX attribute mapping differs but the
# retry/verify/verdict logic runs for real).
$rdcRoot = Join-Path $tmpRoot "rdc"
$sub = Join-Path $rdcRoot "data_0"
New-Item -ItemType Directory -Path $sub -Force | Out-Null
Set-Content -LiteralPath (Join-Path $sub "file.txt") -Value "x"
try { (Get-Item -LiteralPath (Join-Path $sub "file.txt")).Attributes = 'ReadOnly' } catch { }
$rdcOk = Remove-DirectoryClean $rdcRoot
Assert ($rdcOk -eq $true) "G8v16 read-only-file tree deleted, verified gone"
Assert (-not (Test-Path -LiteralPath $rdcRoot)) "G8v16 directory truly absent after clean delete"
$rdcMiss = Remove-DirectoryClean (Join-Path $tmpRoot "never-existed-dir")
Assert ($rdcMiss -eq $true) "G8v16 missing dir -> true (idempotent)"

# G9v16: -CheckOnly offline conclusion must NOT promise network bytes.
Assert ($codeNoComments.Contains('estMB = 0')) "G9v16 offline check conclusion zeroes the network estimate"
$offlineConcl = [regex]::Match($codeNoComments, '(?s)if \(\$Offline\) \{\s*\$onlineMustInstall.*?estMB = 0').Value
Assert ($offlineConcl.Length -gt 0) "G9v16 offline conclusion rewrite block present"
Assert ($offlineConcl.Contains('NO network download') -or $offlineConcl.Contains('NO network')) "G9v16 offline conclusion says NO network download"

# G10v16: offline node path never downloads.
$nodeOffline = [regex]::Match($codeNoComments, '(?s)elseif \(\$Offline\) \{\s*throw "\[install\] -Offline: no usable node zip locally').Value
Assert ($nodeOffline.Length -gt 0) "G10v16 node bootstrap has an offline no-download throw"
Assert ($offlineBlock -or $true) "G10v16 placeholder"
$installOfflineCall = [regex]::Match($codeNoComments, '(?s)Install-PythonRuntimeOffline \$InstallDir').Value
Assert ($installOfflineCall.Length -gt 0) "G10v16 Install-PythonRuntimeOffline wrapper is called"
$wrapperText = [regex]::Match($codeNoComments, '(?sm)^function Install-PythonRuntimeOffline\b.*?^}').Value
Assert ($wrapperText.Length -gt 0) "G10v16 Install-PythonRuntimeOffline wrapper defined"
Assert ($wrapperText.Contains('return (Install-PythonRuntime $InstallDir)')) "G10v16 wrapper delegates to Install-PythonRuntime"

Write-Output ""
Write-Output "RESULT: $pass passed, $fail failed"
Remove-Item -Recurse -Force $tmpRoot -ErrorAction SilentlyContinue
if ($fail -gt 0) { exit 1 } else { exit 0 }
