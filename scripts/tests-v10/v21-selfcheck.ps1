$ErrorActionPreference = "Stop"
# v21 selfcheck (task t_69654390): leak-hole fix (config.json.bak-*
# / credential sweep) + dev-junk exclusion + completion layout print.
# Runs on pwsh 7 (Linux) via AST extraction, same pattern as v20.
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
Assert ($nonAscii -eq 0) "install.ps1 is pure ASCII (PS 5.1 GBK safety)"
Assert $bom "install.ps1 has UTF-8 BOM"

$full = $ast.Extent.Text

# 3. v21 structural gates -- copy exclusions
Assert ($full -match [regex]::Escape('"*.bak", "*.bak-*", "*.backup", "*.old", "*.orig"')) "v21 /XF backup patterns present"
Assert ($full -match [regex]::Escape('"*credentials*", "*secret*", "*.key", "*.pem"')) "v21 /XF credential patterns present"
Assert ($full -match [regex]::Escape('"config.json", "config.json.*", "config.json.bak-*"')) "v21 /XF config backup patterns present"
Assert ($full -match [regex]::Escape('".env", ".env.*", "*.env", "*.env.*"')) "v21 /XF env backup patterns present"
Assert ($full -match [regex]::Escape('"tsconfig.test.json"')) "v21 /XF tsconfig.test.json"
# dev-junk /XD
Assert ($full -match [regex]::Escape('"backup-*", "backups"')) "v21 /XD backup-* dirs"
Assert ($full -match [regex]::Escape('"tests", "test", "__pycache__"')) "v21 /XD tests dirs"
Assert ($full -match [regex]::Escape('".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"')) "v21 /XD cache dirs"
Assert ($full -match [regex]::Escape('foreach ($d in @("docs", ".github"))')) "v21 /XD root docs/.github"
# sanity exclude list mirrors copy set
Assert ($full -match [regex]::Escape('"htmlcov", ".cache", "tests", "test", "__pycache__"')) "v21 Get-TreeStats excludes mirror /XD"
# completion report prints the layout
Assert ($full -match [regex]::Escape('copied:      top-level: ')) "v21 completion prints top-level layout"
Assert ($full -match 'dev-era residue still present at top level') "v21 dev-residue warning present"
# content sweep present as Invoke-CredentialSweep
Assert ($full -match [regex]::Escape("function Invoke-CredentialSweep([string]`$Root, [string[]]`$SkipFiles)")) "v21 sweep extracted as Invoke-CredentialSweep"
Assert ($full -match [regex]::Escape('$skPattern = ''(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}''')) "v21 sk- pattern has a word boundary"
Assert ($full -match 'credential sweep OK') "v21 sweep success message present"
Assert ($full -match [regex]::Escape('10MB')) "v21 sweep skips >10MB payloads"
Assert ($full -match [regex]::Escape('Invoke-CredentialSweep -Root $InstallDir -SkipFiles $finalSkip')) "v21 FINAL whole-tree sweep after all copies"

# 4. BEHAVIOR tests: extract Assert-BlankConfig via AST and run it
#    against real directory trees.
function Extract-Function([string]$Name) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    if (-not $fn) { throw "function $Name not found" }
    return $fn.Extent.Text
}
$fnBlank = Extract-Function 'Assert-BlankConfig'
$fnSweep = Extract-Function 'Invoke-CredentialSweep'
# v23 (t_d37fca26): the two functions now call Test-SecretValue;
# extract it too or the standalone runs break.
$fnSecret = Extract-Function 'Test-SecretValue'
Invoke-Expression $fnSecret
Invoke-Expression $fnBlank
Invoke-Expression $fnSweep

$root = Join-Path ([IO.Path]::GetTempPath()) ("v21self-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$example = Join-Path $root "config.example.json"
Set-Content -LiteralPath $example -Value '{"model":{"baseUrl":"","model":"","apiKeyEnv":""}}'

try {
    # 4a. CLEAN tree -> passes, prints sweep OK
    $clean = Join-Path $root "clean"
    New-Item -ItemType Directory -Path $clean -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $clean "app.js") -Value "console.log(1)"
    Set-Content -LiteralPath (Join-Path $clean "config.json") -Value '{"model":{"baseUrl":"","model":"","apiKeyEnv":""}}'
    Set-Content -LiteralPath (Join-Path $clean ".env") -Value ""
    $threw = $false
    try { Assert-BlankConfig -InstallDir $clean -EnvExistedBefore:$false -CfgExistedBefore:$false -ExampleJson $example }
    catch { $threw = $true }
    Assert (-not $threw) "clean tree passes the v21 sweep"

    # 4b. config.json.bak-* with field NAMES but empty values (the
    #     actual field finding) -> json sweep: fields empty -> PASSES
    #     (field names alone are not a credential), but the FILE is
    #     excluded by /XF anyway.
    $bakNames = Join-Path $root "baknames"
    New-Item -ItemType Directory -Path $bakNames -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $bakNames "config.json.bak-20261009-173158") -Value '{"apiKey":"","DASHSCOPE_API_KEY":""}'
    $threw = $false
    try { Assert-BlankConfig -InstallDir $bakNames -EnvExistedBefore:$false -CfgExistedBefore:$false -ExampleJson $example }
    catch { $threw = $true }
    Assert (-not $threw) "bak with empty key values passes (names only, /XF governs the file)"

    # 4c. config.json.bak-* with a VALUED apiKey -> json sweep does NOT
    #     fire (extension is .bak-... not .json)... but the sk- sweep
    #     catches a literal key. Two sub-cases:
    # 4c-1. valued apiKey in a .json file -> sweep THROWS
    #     (v23: value must be secret-shaped; a real key is >=16 chars,
    #     so the fixture uses a realistic 40-char fake key.)
    $valuedJson = Join-Path $root "valuedjson"
    New-Item -ItemType Directory -Path $valuedJson -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $valuedJson "settings.json") -Value '{"apiKey":"abc123realkey0123456789xyz"}'
    $threw = $false; $msg = ""
    try { Assert-BlankConfig -InstallDir $valuedJson -EnvExistedBefore:$false -CfgExistedBefore:$false -ExampleJson $example }
    catch { $threw = $true; $msg = $_.Exception.Message }
    Assert $threw "valued apiKey in .json throws"
    Assert ($msg -match 'credentials leaked') "throw is the SECURITY FAILURE message"

    # 4c-2. literal sk- key in a NON-json file (e.g. .bak) -> sweep THROWS
    $skBak = Join-Path $root "skbak"
    New-Item -ItemType Directory -Path $skBak -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $skBak "config.json.bak-20261013") -Value '{"apiKey":"sk-abcdefgh12345678"}'
    $threw = $false; $msg = ""
    try { Assert-BlankConfig -InstallDir $skBak -EnvExistedBefore:$false -CfgExistedBefore:$false -ExampleJson $example }
    catch { $threw = $true; $msg = $_.Exception.Message }
    Assert $threw "literal sk- in .bak file throws"
    Assert ($msg -match 'literal key material') "finding names the file and key material"

    # 4d. binary file with a zero byte -> skipped, no throw
    $bin = Join-Path $root "bin"
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $bin "payload.bin"), [byte[]](0,1,0,2,3))
    $threw = $false
    try { Assert-BlankConfig -InstallDir $bin -EnvExistedBefore:$false -CfgExistedBefore:$false -ExampleJson $example }
    catch { $threw = $true }
    Assert (-not $threw) "binary-looking file is skipped by the sweep"

    # 4e. UTF-16LE file with sk- key -> caught (dual decode)
    $u16 = Join-Path $root "u16"
    New-Item -ItemType Directory -Path $u16 -Force | Out-Null
    $u16bytes = [Text.Encoding]::Unicode.GetBytes('token = sk-utf16key12345')
    # prepend the FF FE BOM so the decoder branch triggers
    $u16bytes = [byte[]](0xFF,0xFE) + $u16bytes
    [IO.File]::WriteAllBytes((Join-Path $u16 "note.txt"), $u16bytes)
    $threw = $false
    try { Assert-BlankConfig -InstallDir $u16 -EnvExistedBefore:$false -CfgExistedBefore:$false -ExampleJson $example }
    catch { $threw = $true }
    Assert $threw "UTF-16LE file with sk- is caught (BOM branch)"

    # 4f. apiKeyEnv (variable NAME, not a value) must NOT trigger
    $envRef = Join-Path $root "envref"
    New-Item -ItemType Directory -Path $envRef -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $envRef "model.json") -Value '{"apiKeyEnv":"DASHSCOPE_API_KEY"}'
    $threw = $false
    try { Assert-BlankConfig -InstallDir $envRef -EnvExistedBefore:$false -CfgExistedBefore:$false -ExampleJson $example }
    catch { $threw = $true }
    Assert (-not $threw) "apiKeyEnv variable NAME does not trigger the sweep"

    # 4g. WORD BOUNDARY: "task-management" / "risk-assessment" must NOT
    #     match the sk- pattern (would hard-fail every install).
    $boring = Join-Path $root "boring"
    New-Item -ItemType Directory -Path $boring -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $boring "readme.md") -Value "uses task-management and risk-assessment, plus task-summary keys."
    $threw = $false
    try { Invoke-CredentialSweep -Root $boring -SkipFiles @() } catch { $threw = $true }
    Assert (-not $threw) "ordinary words (task-management etc.) do NOT match the sk- pattern"

    # 4h. SkipFiles: a user-owned .env with a real key is skipped on
    #     the FINAL sweep (their own config is not a leak).
    $userEnv = Join-Path $root "userenv"
    New-Item -ItemType Directory -Path $userEnv -Force | Out-Null
    $envPath = Join-Path $userEnv ".env"
    Set-Content -LiteralPath $envPath -Value ("DASHSCOPE_API_KEY=" + ("s" + "k-") + "dummy-not-a-real-secret")
    $threw = $false
    try { Invoke-CredentialSweep -Root $userEnv -SkipFiles @($envPath) } catch { $threw = $true }
    Assert (-not $threw) "user-owned .env is skipped via -SkipFiles"
    # ... but the same file WITHOUT the skip still throws (defense).
    $threw = $false
    try { Invoke-CredentialSweep -Root $userEnv -SkipFiles @() } catch { $threw = $true }
    Assert $threw "same .env without skip still throws"
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ""
Write-Output ("RESULT: pass=$script:pass fail=$script:fail")
if ($script:fail -gt 0) { exit 1 }
