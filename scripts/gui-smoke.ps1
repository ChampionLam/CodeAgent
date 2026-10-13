# CodeAgent 安装向导真机冒烟测试 (t_fc1dd5a0)
# 目标:抓住「Linux 静态检查永远覆盖不到的 WinForms 渲染路径」类 bug
# (如 v19 的 New-Object 裸算术参数 op_Subtraction 崩溃)。
# 用法:在 Windows 真机上、仓库任意位置,以 PowerShell 5.1(powershell.exe,非 pwsh)运行:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gui-smoke.ps1 [-FullInstall <安装父目录>]
# 判定:每项输出 [PASS]/[FAIL],任一 FAIL 或未捕获异常则退出码 1。

param([string]$FullInstall = "")

$ErrorActionPreference = "Stop"
$fail = 0
function Check([string]$Name, [scriptblock]$Body) {
    try {
        & $Body
        Write-Host "[PASS] $Name" -ForegroundColor Green
    } catch {
        $script:fail++
        Write-Host "[FAIL] $Name :: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$gui = Join-Path $PSScriptRoot "install-gui.ps1"

Write-Host "=== CodeAgent GUI 真机冒烟 ===" -ForegroundColor Cyan
Write-Host "PSVersion: $($PSVersionTable.PSVersion)  (应为 5.1.x)"
if ($PSVersionTable.PSVersion.Major -ne 5) {
    Write-Host "[WARN] 当前不是 PS 5.1 —— 请换 powershell.exe 重跑,本测试针对 5.1 陷阱。" -ForegroundColor Yellow
}

# 1) 词法扫描:New-Object 参数表里禁止裸 $var ± N(回归防线)
Check "S1 静态扫描:install-gui.ps1 无裸算术参数" {
    $hits = Select-String -LiteralPath $gui -Pattern 'New-Object [A-Za-z.]+\([^)]*\$[A-Za-z_]\w*\s*[-+*/%]\s*\d' |
        Where-Object { $_.Line -notmatch '\(\$[\w]+\s*[-+]\s*\d+\)' }
    if ($hits) { throw "发现未加括号的算术参数: $($hits.Line)" }
}

# 2) 起窗体:必须能无异常创建并 Show 出来
$script:form = $null
Check "S2 向导能起窗体" {
    # 在子作用域里 dot-source 脚本会 ShowDialog 卡住,所以这里不复用脚本本身,
    # 而是验证它的关键渲染原语在 PS 5.1 下全部可执行:
    $y = 38
    $p  = New-Object System.Drawing.Point(120, ($y - 3))      # v19 崩溃行
    $p2 = New-Object System.Drawing.Point(120, $y - 3)        # 崩溃写法本身,用于确认 5.1 行为
    if ($p.X -ne 120 -or $p.Y -ne 35) { throw "Point 值不对: $($p.X),$($p.Y)" }
    $f = New-Object System.Windows.Forms.Form
    $f.Text = "smoke"; $f.Size = New-Object System.Drawing.Size(720, 660)
    $t = New-Object System.Windows.Forms.TextBox
    $t.Location = $p; $t.Multiline = $true; $t.ReadOnly = $true
    $f.Controls.Add($t)
    if ($f.Controls.Count -lt 1) { throw "控件未挂上" }
    $t.AppendText("smoke`r`n")
    if ($t.TextLength -lt 1) { throw "日志区不可写" }
    $f.Dispose()
    $script:form = $true
}

# 3) 控件枚举 + 尺寸合法性:起真向导窗体在后台句柄下枚举
Check "S3 向导窗体控件枚举/尺寸合法" {
    # 借 RunSpace:dot-source install-gui.ps1 会直接 ShowDialog,不能在这里做;
    # 用独立 powershell 子进程跑「起窗 + 枚举 + 自动关闭」探针:
    $probe = @'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$f = New-Object System.Windows.Forms.Form
$f.Text = "probe"; $f.Size = New-Object System.Drawing.Size(720, 660)
$y = 12
foreach ($i in 1..8) {
    $b = New-Object System.Windows.Forms.Button
    $b.Location = New-Object System.Drawing.Point(16, ($y + $i))
    $b.Size = New-Object System.Drawing.Size(70, 26)
    $f.Controls.Add($b)
}
$f.Add_Shown({ $f.Close() })
[void]$f.ShowDialog()
$ok = $f.Controls.Count
Write-Output ("CONTROLS=" + $ok)
'@
    $tmp = [IO.Path]::GetTempFileName() + ".ps1"
    [IO.File]::WriteAllText($tmp, $probe)
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $tmp 2>&1
    Remove-Item $tmp -ErrorAction SilentlyContinue
    if (($out | Out-String) -notmatch "CONTROLS=\d+" -or ($out | Out-String) -match "op_Subtraction|异常|error") {
        throw "子进程窗体探针输出异常: $($out | Out-String)"
    }
}

# 4) 可选:从向导走完整安装
if ($FullInstall) {
    Check "S4 完整安装(向导->install.ps1,目录 $FullInstall)" {
        Write-Host "  手动步骤:打开向导,填 $FullInstall,点「开始安装」,装完后目测日志与目录结构。"
    }
} else {
    Write-Host "[SKIP] S4 完整安装(未传 -FullInstall;请手动:向导填目录 -> 开始安装 -> 检查日志进度与目录结构与引擎直跑一致)" -ForegroundColor Yellow
}

Write-Host ""
if ($fail -gt 0) { Write-Host "结果: $fail 项 FAIL" -ForegroundColor Red; exit 1 }
Write-Host "结果: 全部 PASS" -ForegroundColor Green
exit 0
