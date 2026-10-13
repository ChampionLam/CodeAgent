# v27 selfcheck (task t_8ce86a47): memory engine install layer.
# Runs on pwsh 7 (Linux) via AST extraction, same pattern as v20-v25.
$ErrorActionPreference = "Stop"

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

# 3. v27 structural gates
# 3a. memory requirements file referenced and installed fail-soft
Assert ($full -match [regex]::Escape('$memoryReq = "python\requirements-memory.txt"')) "v27 memory req path constant"
Assert ($full -match [regex]::Escape('pip failed on $memoryReq (mirror + PyPI)')) "v27 memory mirror+PyPI fallback reasons"
Assert ($full -match [regex]::Escape('Write-Warning "[install] memory engine NOT installed')) "v27 memory fail-soft warning (never abort)"
Assert (-not ($full -match 'throw \[string\].*memory')) "v27 memory failure never throws"
# 3b. verification block: find_spec probe + subprocess env hand-off + cleanup
Assert ($full -match [regex]::Escape('import importlib.util, os, sys')) "v27 verify script imports importlib.util"
Assert ($full -match [regex]::Escape('find_spec("mnemosyne")')) "v27 verify uses the sidecar's exact find_spec probe"
Assert ($full -match [regex]::Escape('import mnemosyne.mcp_server')) "v27 verify imports the -m entrypoint module"
Assert ($full -match [regex]::Escape('$env:MNEMOSYNE_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"')) "v27 env hand-off sets embedding model key"
Assert ($full -match [regex]::Escape('$env:MNEMOSYNE_LLM_ENABLED = "false"')) "v27 env hand-off pins LLM off"
Assert ($full -match [regex]::Escape('Remove-Item Env:MNEMOSYNE_EMBEDDING_MODEL')) "v27 env hand-off cleaned up after probe"
# 3c. summary lines (requirement 3)
Assert ($full -match 'memory engine: installed \(') "v27 completion summary: installed"
Assert ($full -match 'memory engine: not installed \(') "v27 completion summary: not installed(reason)"
# 3d. offline branches
Assert ($full -match [regex]::Escape('--no-index --find-links $WheelDir -r $memoryPath')) "v27 offline WheelDir branch"
Assert ($full -match [regex]::Escape('no venv python (offline install without venv)')) "v27 offline no-venv reason"
# 3e. no embedding-model download in the installer (final user decision)
Assert (-not ($full -match 'hf-mirror\.com.*Download|Download.*hf-mirror\.com')) "v27 installer does NOT download the embedding model"
# 3e. guard model IS downloaded by the installer from ModelScope
#     (user decision, task t_eea5a4ac, supersedes the old v27
#     "no modelscope" directive): pull + sha256 + skip flags + fail-soft
Assert ($full -match [regex]::Escape('https://modelscope.cn/api/v1/models')) "v27 installer pulls guard model from ModelScope file-direct API"
Assert ($full -match [regex]::Escape('[string]$ModelScopeRepo = "ChampionLam/commands-guard-fp16"')) "v27 default ModelScope repo is ChampionLam/commands-guard-fp16"
Assert (-not ($full -match 'commans[^d]')) "v27 no stale 'commans' (typo) repo reference"
Assert ($full -match '\[switch\]\$SkipModelDownload') "v27 -SkipModelDownload skip branch present"
Assert ($full -match 'Get-Sha256SumsMap|sha256.*VERIF|sha256-verified') "v27 guard model files sha256-verified"
Assert ($full -match [regex]::Escape('$guardModelNetwork = (-not $Offline) -and (-not $SkipModelDownload)')) "v27 -Offline/-SkipModelDownload disable guard model network"
Assert ($full -match [regex]::Escape('Write-Warning "[install] guard model:')) "v27 guard model failures are fail-soft warnings"
Assert ($full -match 'SHA256SUMS') "v27 local SHA256SUMS priority over built-in table kept"
Assert ($full -match [regex]::Escape('HF_ENDPOINT')) "v27 mentions HF_ENDPOINT mirror (app-side lazy fetch context)"
# 3f. wheel-dir note: memory not silently skipped in the $reqs loop's offline path
Assert ($full -match [regex]::Escape('$Script:MemoryReason = "offline wheels incomplete')) "v27 offline wheels-missing reason surfaced"

# 4. requirements-memory.txt content (check the REQUIREMENT LINES only,
# comments may mention mnemosyne-hermes/[llm] to explain why NOT to use them)
$reqPath = Join-Path $PSScriptRoot "..\..\python\requirements-memory.txt"
Assert (Test-Path $reqPath) "python\requirements-memory.txt exists"
$reqText = Get-Content -LiteralPath $reqPath -Raw
$reqLines = @(($reqText -split "`r?`n") | Where-Object { $_ -and ($_ -notmatch '^\s*#') })
$depLines = @($reqLines | Where-Object { $_ -match 'mnemosyne|fastembed|llm' })
Assert ($reqText -match 'mnemosyne-memory\[mcp,embeddings\]>=3\.15\.1') "requirements pins mnemosyne-memory[mcp,embeddings]>=3.15.1"
Assert (@($depLines | Where-Object { $_ -match 'mnemosyne-hermes' }).Count -eq 0) "requirement lines do NOT pull mnemosyne-hermes"
Assert (@($depLines | Where-Object { $_ -match '\[llm\]' }).Count -eq 0) "requirement lines do NOT pull the llm extra"

# 5. bootstrap-venv scripts carry the same layer
$bvSh = Get-Content -LiteralPath (Join-Path $PSScriptRoot "..\bootstrap-venv.sh") -Raw
Assert ($bvSh -match 'requirements-memory\.txt') "bootstrap-venv.sh installs memory layer"
Assert ($bvSh -match 'memory features unavailable') "bootstrap-venv.sh fail-soft wording"
$bvPs = Get-Content -LiteralPath (Join-Path $PSScriptRoot "..\bootstrap-venv.ps1") -Raw
Assert ($bvPs -match 'requirements-memory\.txt') "bootstrap-venv.ps1 installs memory layer"
Assert ($bvPs -match 'memory features unavailable') "bootstrap-venv.ps1 fail-soft wording"
# bootstrap-venv.ps1 must keep CRLF (batch/PS 5.1 discipline)
Assert ((Get-Content -LiteralPath (Join-Path $PSScriptRoot "..\bootstrap-venv.ps1") -Raw) -match "`r`n") "bootstrap-venv.ps1 keeps CRLF line endings"

Write-Output ""
Write-Output ("RESULT: pass={0} fail={1}" -f $script:pass, $script:fail)
if ($script:fail -gt 0) { exit 1 } else { exit 0 }
