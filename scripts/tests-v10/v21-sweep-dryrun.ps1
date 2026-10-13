$ErrorActionPreference = "Stop"
# Dry-run the v21 Invoke-CredentialSweep against a COPY of the source
# tree shaped like a REAL finished install: app tree minus dev junk
# (tests/docs/.github/tests-v10/backup-*/... NOT copied) plus
# node_modules. Proves: no false positives on the real tree AND the
# fixtures with fake sk- keys never arrive.
$f = (Join-Path $PSScriptRoot "install.ps1")
$errs = $null; $toks = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$toks, [ref]$errs)
if ($errs.Count -gt 0) { throw "parse errors" }
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-CredentialSweep' }, $true)
Invoke-Expression $fn.Extent.Text

$src = (Split-Path -Parent $PSScriptRoot)
$dst = Join-Path ([IO.Path]::GetTempPath()) ("v21dry-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $dst -Force | Out-Null

# mirror the copy: app tree minus dev junk; node_modules separately
$devDirs = @("node_modules", ".git", "runtime", ".install-state.json", "codeagent",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".coverage", "htmlcov",
    ".cache", "tests", "test", "__pycache__", ".nyc_output", "coverage", "backup-*",
    "backups", "docs", ".github", ".venv", "tests-v10", "git-hooks")
function Copy-Filtered([string]$From, [string]$To, $ExcludeDirs, $ExcludeFiles) {
    $stack = New-Object System.Collections.Stack
    $stack.Push((Get-Item -LiteralPath $From -Force))
    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        $rel = $dir.FullName.Substring($From.Length).TrimStart('\','/')
        $target = if ($rel) { Join-Path $To $rel } else { $To }
        if (-not (Test-Path -LiteralPath $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
        foreach ($child in (Get-ChildItem -LiteralPath $dir.FullName -Force -ErrorAction SilentlyContinue)) {
            if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            if ($child.PSIsContainer) {
                $skip = $false
                foreach ($d in $ExcludeDirs) { if ($child.Name -like $d) { $skip = $true; break } }
                if (-not $skip) { $stack.Push($child) }
            } else {
                $skip = $false
                foreach ($x in $ExcludeFiles) { if ($child.Name -like $x) { $skip = $true; break } }
                if (-not $skip) {
                    Copy-Item -LiteralPath $child.FullName -Destination (Join-Path $target $child.Name) -Force
                }
            }
        }
    }
}
Copy-Filtered $src $dst $devDirs @(".env", ".env.*", "*.env", "config.json", "config.json.*",
    "config.json.bak-*", "*.bak", "*.bak-*", "*.backup", "*.old", "*.orig",
    "*credentials*", "*secret*", "*.key", "*.pem", ".install-state.json", "*.log",
    "tsconfig.test.json", "*.pyc", "*.tsbuildinfo")
Copy-Filtered (Join-Path $src "node_modules") (Join-Path $dst "node_modules") @("codeagent") @()

try {
    Invoke-CredentialSweep -Root $dst -SkipFiles @()
    Write-Output "SWEEP PASSED on the simulated real install tree"
    $top = @(Get-ChildItem -LiteralPath $dst -Force | ForEach-Object { $_.Name }) | Sort-Object
    Write-Output ("top-level: " + ($top -join ", "))
} catch {
    Write-Output "SWEEP THREW on the simulated install tree:"
    Write-Output $_.Exception.Message
    exit 1
} finally {
    Remove-Item -LiteralPath $dst -Recurse -Force -ErrorAction SilentlyContinue
}
