$ErrorActionPreference = "Stop"
# v23 selfcheck (task t_d37fca26): credential-sweep FALSE POSITIVE fix
# -- tokenizer.json's unk_token must not trip the security gate, while
# a real sk- key in a .bak backup and a secret-shaped apiKey in .json
# must STILL throw. Runs on pwsh 7 (Linux) via AST extraction.
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

# 3. v23 structural gates -- the fuzzy field regex is GONE, the
#    exact allowlist + value shape check + model artifact exemption
#    are present.
Assert ($full -notmatch [regex]::Escape('.*_?token|.*_?secret|.*_?credential|.*_?password')) "v23 fuzzy `.*_?token` field regex REMOVED"
Assert ($full -match [regex]::Escape('access_?token|auth_?token|id_?token|refresh_?token|session_?token|client_?secret')) "v23 exact token/secret field allowlist present"
Assert ($full -match 'function Test-SecretValue') "v23 value-shape check Test-SecretValue present"
Assert ($full -match 'sk\?xk' -or $full -match [regex]::Escape('(?:sk|xk)-[A-Za-z0-9_\-]{8,}$')) "v23 sk- key shape kept"
Assert ($full -match [regex]::Escape('(tokenizer|tokenizer_config|vocab|special_tokens_map|added_tokens|merges)\.(json|txt|bin)')) "v23 model artifact name exemption present"

# 4. BEHAVIOR tests: extract the functions via AST and run them.
function Extract-Function([string]$Name) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    if (-not $fn) { throw "function $Name not found" }
    return $fn.Extent.Text
}
$fnSecret = Extract-Function 'Test-SecretValue'
$fnBlank = Extract-Function 'Assert-BlankConfig'
$fnSweep = Extract-Function 'Invoke-CredentialSweep'
Invoke-Expression $fnSecret
Invoke-Expression $fnBlank
Invoke-Expression $fnSweep

$root = Join-Path ([IO.Path]::GetTempPath()) ("v23self-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$example = Join-Path $root "config.example.json"
Set-Content -LiteralPath $example -Value '{"model":{"baseUrl":"","model":"","apiKeyEnv":""}}'

try {
    # (a) REQUIRED REGRESSION: a tokenizer.json with
    #     unk_token/pad_token/cls_token (symbolic values) -> PASS.
    $model = Join-Path $root "model"
    $decDir = Join-Path $model "python\models\decision_bert"
    New-Item -ItemType Directory -Path $decDir -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $decDir "tokenizer.json") -Value '{"tokenizer_class":"BertTokenizer","unk_token":"[UNK]","pad_token":"[PAD]","cls_token":"[CLS]","mask_token":"[MASK]","sep_token":"[SEP]","do_lower_case":true,"model":{"vocab":{"a":0,"b":1}}}'
    $threw = $false
    try { Invoke-CredentialSweep -Root $model -SkipFiles @() } catch { $threw = $true }
    Assert (-not $threw) "(a) tokenizer.json with unk_token/pad_token PASSES the sweep"

    # (a-2) same, through Assert-BlankConfig (the install path)
    $threw = $false
    try { Assert-BlankConfig -InstallDir $model -EnvExistedBefore:$false -CfgExistedBefore:$false -ExampleJson $example } catch { $threw = $true }
    Assert (-not $threw) "(a-2) install-path assert also passes with the model tree"

    # (a-3) vocab.txt / special_tokens_map.json / model.onnx present -> PASS
    Set-Content -LiteralPath (Join-Path $decDir "vocab.txt") -Value "[UNK]`n[PAD]"
    Set-Content -LiteralPath (Join-Path $decDir "special_tokens_map.json") -Value '{"unk_token":"[UNK]","pad_token":"[PAD]"}'
    [IO.File]::WriteAllBytes((Join-Path $decDir "model.onnx"), [byte[]](8,0,0,0,1,2,3,4,0,5))
    $threw = $false
    try { Invoke-CredentialSweep -Root $model -SkipFiles @() } catch { $threw = $true }
    Assert (-not $threw) "(a-3) vocab.txt / special_tokens_map.json / onnx also pass"

    # (b) REQUIRED REGRESSION: config.json.bak with an sk- shaped key
    #     -> MUST THROW (sk- literal detection, kept from v21).
    $skb = Join-Path $root "skb"
    New-Item -ItemType Directory -Path $skb -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $skb "config.json.bak-20261013") -Value '{"apiKey":"sk-abc123def456ghi789"}'
    $threw = $false; $msg = ""
    try { Invoke-CredentialSweep -Root $skb -SkipFiles @() } catch { $threw = $true; $msg = $_.Exception.Message }
    Assert $threw "(b) .bak with sk- shaped apiKey THROWS"
    Assert ($msg -match 'literal key material') "(b) throw is the literal-key finding"

    # (b-2) a LONG secret-shaped value in a non-sk field (e.g. password)
    #       in a .json -> still THROWS (field name + value shape).
    $pw = Join-Path $root "pw"
    New-Item -ItemType Directory -Path $pw -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $pw "settings.json") -Value '{"password":"p455w0rd-1234567890-abcdef"}'
    $threw = $false; $msg = ""
    try { Invoke-CredentialSweep -Root $pw -SkipFiles @() } catch { $threw = $true; $msg = $_.Exception.Message }
    Assert $threw "(b-2) long secret-shaped password in .json THROWS"
    Assert ($msg -match 'live field') "(b-2) throw is the live-field finding"

    # (b-3) sessionToken with a long opaque value -> THROWS (allowlist)
    $st = Join-Path $root "st"
    New-Item -ItemType Directory -Path $st -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $st "auth.json") -Value '{"sessionToken":"4f8a3b2c1d9e7f0a6b5c4d3e2f1a0b9c8d7e6f5a"}'
    $threw = $false
    try { Invoke-CredentialSweep -Root $st -SkipFiles @() } catch { $threw = $true }
    Assert $threw "(b-3) valued sessionToken THROWS"

    # (c) REQUIRED REGRESSION: empty template config.json + 0-byte
    #     .env -> PASS.
    $tmpl = Join-Path $root "tmpl"
    New-Item -ItemType Directory -Path $tmpl -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $tmpl "config.json") -Value '{"model":{"baseUrl":"","model":"","apiKeyEnv":""}}'
    Set-Content -LiteralPath (Join-Path $tmpl ".env") -Value ""
    $threw = $false
    try { Assert-BlankConfig -InstallDir $tmpl -EnvExistedBefore:$false -CfgExistedBefore:$false -ExampleJson $example } catch { $threw = $true }
    Assert (-not $threw) "(c) empty template config.json + 0-byte .env PASS"

    # (d) NON-credentials never throw:
    #   - short symbolic value on a credential-ish name
    #   - apiKeyEnv variable NAME
    #   - "password" holding an env var NAME
    #   - package.json with dep names containing "token"
    #   - "secret" holding a URL-ish string
    $ok = Join-Path $root "ok"
    New-Item -ItemType Directory -Path $ok -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $ok "notes.json") -Value '{"token":"[UNK]","secret":"none"}'
    Set-Content -LiteralPath (Join-Path $ok "model.json") -Value '{"apiKeyEnv":"DASHSCOPE_API_KEY"}'
    Set-Content -LiteralPath (Join-Path $ok "sys.json") -Value '{"password":"DASHSCOPE_API_KEY"}'
    Set-Content -LiteralPath (Join-Path $ok "package.json") -Value '{"dependencies":{"@esfx/canceltoken":"1.0.0"},"token":"typescript is fine"}'
    Set-Content -LiteralPath (Join-Path $ok "readme.md") -Value "uses task-management and risk-assessment, plus token-summary keys."
    $threw = $false
    try { Invoke-CredentialSweep -Root $ok -SkipFiles @() } catch { $threw = $true }
    Assert (-not $threw) "(d) symbolic values / var names / manifests / boring words all PASS"

    # (e) Test-SecretValue unit checks
    Assert (Test-SecretValue "sk-abcdefgh1234") "unit: sk- key is a secret"
    Assert (Test-SecretValue "0123456789abcdef0123456789abcdef") "unit: 32-char opaque blob is a secret"
    Assert (-not (Test-SecretValue "[UNK]")) "unit: [UNK] is not a secret"
    Assert (-not (Test-SecretValue "[PAD]")) "unit: [PAD] is not a secret"
    Assert (-not (Test-SecretValue "DASHSCOPE_API_KEY")) "unit: env var NAME is not a secret"
    Assert (-not (Test-SecretValue "${MY_TOKEN}")) "unit: variable reference is not a secret"
    Assert (-not (Test-SecretValue "https://api.example.com/v1")) "unit: URL is not a secret"
    Assert (-not (Test-SecretValue "none")) "unit: 'none' is not a secret"
    Assert (-not (Test-SecretValue "short12chars")) "unit: 12-char short value is not a secret"

    # (f) a fake tokenizer.json (deep, like the field box layout) is
    #     parsed but never scanned as a config -- regression for the
    #     exact field report (268,943 B, unk_token).
    $big = Join-Path $root "big"
    New-Item -ItemType Directory -Path $big -Force | Out-Null
    $bigJson = '{"tokenizer_class":"BertTokenizer","unk_token":"[UNK]"'
    for ($i = 0; $i -lt 5000; $i++) { $bigJson += ',"tok' + $i + '":' + $i }
    $bigJson += '}'
    Set-Content -LiteralPath (Join-Path $big "tokenizer.json") -Value $bigJson
    $threw = $false
    try { Invoke-CredentialSweep -Root $big -SkipFiles @() } catch { $threw = $true }
    Assert (-not $threw) "(f) large generated tokenizer.json PASSES"
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ""
Write-Output ("RESULT: pass=$script:pass fail=$script:fail")
if ($script:fail -gt 0) { exit 1 }
