# CodeAgent 可视化安装/卸载向导 (v20, task t_3ea985be)
# PowerShell 5.1 + WinForms -- 无任何第三方依赖,不需要 Python/Node/管理员权限。
# 收集参数后调用 scripts/install.ps1 完成实际安装/卸载(复用,不重写)。
#
# v20 (t_3ea985be): NEW UNINSTALL MODE (radio buttons at the top).
# Flow: pick install dir -> preview EXACTLY what will be deleted (install
# dir, registry entries, shortcuts) vs what is KEPT (user data, BOTH
# layouts) -> second confirm dialog -> reuse `install.ps1 -Uninstall
# [-PurgeData]` with live log streaming -> post-run re-verification
# (dir really gone / PythonCore residue / shortcuts / data dirs) printed
# into the log area. Data layouts covered (t_27714fcb moves the default
# data dir into the install tree): legacy %APPDATA%\CodeAgent and new
# <InstallDir>\data + <InstallDir>\userdata. User data is KEPT by
# default; only the explicit "delete data too" checkbox adds -PurgeData.
# The GUI itself pre-deletes the legacy dir ONLY when its
# .codeagent-install ownership marker names THIS install dir (same gate
# as install.ps1's purge proof) -- never silently.
# 用法: 双击 install-gui.bat,或右键 install-gui.ps1 -> 使用 PowerShell 运行。
#
# 【PS 5.1 陷阱,勿再踩】在 New-Object <Type>(a, $var - 3) 这类参数表里,
# 逗号比二元运算符结合得更紧:PS 5.1 会解析成 (a, $var) - 3,对 Object[] 做
# op_Subtraction,运行即崩「不包含名为 op_Subtraction 的方法」。PS 7 宽容,
# 所以 Linux 上的一切静态检查/单测都发现不了。凡 New-Object 参数表里的
# $var ± N 必须写成 ($var - N)。

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
$Script:GuiRoot = Split-Path -Parent $PSScriptRoot   # 仓库根
$Script:Installer = Join-Path $PSScriptRoot "install.ps1"
if (-not (Test-Path -LiteralPath $Script:Installer)) {
    [System.Windows.Forms.MessageBox]::Show(
        "找不到安装脚本: $Script:Installer`n请确认 install-gui.ps1 与 install.ps1 在同一目录的父目录结构未被打散。",
        "CodeAgent 安装向导", "OK", "Error") | Out-Null
    exit 1
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

[System.Windows.Forms.Application]::EnableVisualStyles()

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

# 就地探测一个解释器:能跑起来就回 "<可运行: 版本>",否则回 "<不可运行: 原因>"。
function Get-ExeProbe([string]$ExePath) {
    if (-not $ExePath -or -not $ExePath.Trim()) { return $null }
    if (-not (Test-Path -LiteralPath $ExePath)) { return "文件不存在" }
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $ExePath
        $psi.Arguments = "--version"
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        # 20 秒超时,防 WindowsApps 假 python.exe 挂住
        $p = [System.Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit(20000)) {
            try { $p.Kill() } catch { }
            return "启动后 20 秒无响应(疑似应用商店占位 stub)"
        }
        $out = $p.StandardOutput.ReadToEnd().Trim()
        if (-not $out) { $out = $p.StandardError.ReadToEnd().Trim() }
        if ($p.ExitCode -ne 0) { return "退出码 $($p.ExitCode)" }
        return $out
    } catch {
        return "无法启动: $($_.Exception.Message)"
    }
}

# 选择一个 exe 文件
function Pick-Exe([string]$Title) {
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Title = $Title
    $dlg.Filter = "可执行文件 (*.exe)|*.exe|所有文件 (*.*)|*.*"
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $dlg.FileName }
    return $null
}

# 选择一个目录
function Pick-Dir([string]$Title) {
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = $Title
    $dlg.ShowNewFolderButton = $true
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $dlg.SelectedPath }
    return $null
}

# ---------------------------------------------------------------------------
# 窗体
# ---------------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = "CodeAgent 可视化安装向导"
$form.Size = New-Object System.Drawing.Size(500, 790)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false

$y = 12
function New-Label([string]$Text, [int]$Top) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $Text
    $l.Location = New-Object System.Drawing.Point(16, $Top)
    $l.Size = New-Object System.Drawing.Size(468, 20)
    $form.Controls.Add($l)
    return $l
}

# --- 第 0 步:安装 / 卸载 模式选择 (v20) -------------------------------------
New-Label "模式:安装 / 卸载" $y | Out-Null
$y += 22
$rdoInstall = New-Object System.Windows.Forms.RadioButton
$rdoInstall.Text = "安装(全新/覆盖更新)"
$rdoInstall.Checked = $true
$rdoInstall.Location = New-Object System.Drawing.Point(16, ($y - 2))
$rdoInstall.Size = New-Object System.Drawing.Size(160, 22)
$rdoInstall.Add_CheckedChanged({
    # data-purge checkbox only makes sense in uninstall mode
    if ($rdoInstall.Checked) { $chkPurgeData.Enabled = $false }
})
$form.Controls.Add($rdoInstall)
$rdoUninstall = New-Object System.Windows.Forms.RadioButton
$rdoUninstall.Text = "卸载(删除本安装)"
$rdoUninstall.Location = New-Object System.Drawing.Point(200, ($y - 2))
$rdoUninstall.Size = New-Object System.Drawing.Size(160, 22)
$rdoUninstall.Add_CheckedChanged({
    if ($rdoUninstall.Checked) { $chkPurgeData.Enabled = $true }
})
$form.Controls.Add($rdoUninstall)
$y += 32

# --- v20: 卸载模式的数据目录探测/预览辅助 ------------------------------------

# Probe all candidate data dirs for an install dir. Returns an array of
# hashtables: @{ Path; Exists; Desc } -- layout-agnostic (legacy
# %APPDATA%\CodeAgent AND new in-tree data/userdata from t_27714fcb).
function Get-DataDirCandidates([string]$TargetDir) {
    $c = @()
    $legacy = Join-Path $env:APPDATA "CodeAgent"
    $c += @{ Path = $legacy;  Desc = "老布局:用户数据(会话/日志/配置)" }
    $c += @{ Path = (Join-Path $TargetDir "data");     Desc = "新布局:侧车数据(会话 db/日志)" }
    $c += @{ Path = (Join-Path $TargetDir "userdata"); Desc = "新布局:浏览器档案/缓存" }
    foreach ($e in $c) {
        $e.Exists = (Test-Path -LiteralPath $e.Path)
    }
    return $c
}

# Read the install-time .install-state.json record (dataDir actually used
# by the install being uninstalled). Returns "" when absent/unreadable.
function Get-RecordedDataDir([string]$TargetDir) {
    $recPath = Join-Path $TargetDir ".install-state.json"
    if (-not (Test-Path -LiteralPath $recPath)) { return "" }
    try {
        $rec = Get-Content -LiteralPath $recPath -Raw | ConvertFrom-Json -ErrorAction Stop
        return [string]$rec.dataDir
    } catch { return "" }
}

# Ownership proof, same rule as install.ps1: the marker INSIDE the
# candidate dir must name THIS install dir. No marker -> not ours.
function Test-LegacyDirOwnedBy([string]$LegacyDir, [string]$TargetDir) {
    if (-not (Test-Path -LiteralPath $LegacyDir)) { return $false }
    $mk = Join-Path $LegacyDir ".codeagent-install"
    if (-not (Test-Path -LiteralPath $mk)) { return $false }
    try {
        $m = Get-Content -LiteralPath $mk -Raw | ConvertFrom-Json -ErrorAction Stop
        $mi = [string]$m.installDir
        if ($mi.Trim()) {
            return ($mi.Trim().TrimEnd('\').ToLower() -eq $TargetDir.TrimEnd('\').ToLower())
        }
    } catch { }
    return $false
}

# Best-effort count of files under a dir (preview only; 0 on error).
function Get-FileCount([string]$Dir) {
    if (-not (Test-Path -LiteralPath $Dir)) { return 0 }
    try {
        return @(Get-ChildItem -LiteralPath $Dir -Recurse -File -Force -ErrorAction SilentlyContinue).Count
    } catch { return 0 }
}

# --- 第一步:Python / Node 路径 ---------------------------------------------
New-Label "第 1 步:已有的 Python / Node" $y | Out-Null
$y += 26

$lblPy = New-Object System.Windows.Forms.Label
$lblPy.Text = "python.exe 路径:"
$lblPy.Location = New-Object System.Drawing.Point(16, $y)
$lblPy.Size = New-Object System.Drawing.Size(96, 20)
$form.Controls.Add($lblPy)
$txtPy = New-Object System.Windows.Forms.TextBox
$txtPy.Location = New-Object System.Drawing.Point(118, ($y - 3))
$txtPy.Size = New-Object System.Drawing.Size(214, 24)
$form.Controls.Add($txtPy)
$btnPyBrowse = New-Object System.Windows.Forms.Button
$btnPyBrowse.Text = "浏览…"
$btnPyBrowse.Location = New-Object System.Drawing.Point(346, ($y - 4))
$btnPyBrowse.Size = New-Object System.Drawing.Size(68, 26)
$form.Controls.Add($btnPyBrowse)
$btnPyProbe = New-Object System.Windows.Forms.Button
$btnPyProbe.Text = "检测"
$btnPyProbe.Location = New-Object System.Drawing.Point(420, ($y - 4))
$btnPyProbe.Size = New-Object System.Drawing.Size(56, 26)
$form.Controls.Add($btnPyProbe)
$y += 32
$lblPyResult = New-Label "" $y
$lblPyResult.ForeColor = [System.Drawing.Color]::Gray
$y += 30

$lblNode = New-Object System.Windows.Forms.Label
$lblNode.Text = "node.exe 路径:"
$lblNode.Location = New-Object System.Drawing.Point(16, $y)
$lblNode.Size = New-Object System.Drawing.Size(96, 20)
$form.Controls.Add($lblNode)
$txtNode = New-Object System.Windows.Forms.TextBox
$txtNode.Location = New-Object System.Drawing.Point(118, ($y - 3))
$txtNode.Size = New-Object System.Drawing.Size(214, 24)
$form.Controls.Add($txtNode)
$btnNodeBrowse = New-Object System.Windows.Forms.Button
$btnNodeBrowse.Text = "浏览…"
$btnNodeBrowse.Location = New-Object System.Drawing.Point(346, ($y - 4))
$btnNodeBrowse.Size = New-Object System.Drawing.Size(68, 26)
$form.Controls.Add($btnNodeBrowse)
$btnNodeProbe = New-Object System.Windows.Forms.Button
$btnNodeProbe.Text = "检测"
$btnNodeProbe.Location = New-Object System.Drawing.Point(420, ($y - 4))
$btnNodeProbe.Size = New-Object System.Drawing.Size(56, 26)
$form.Controls.Add($btnNodeProbe)
$y += 32
$lblNodeResult = New-Label "" $y
$lblNodeResult.ForeColor = [System.Drawing.Color]::Gray
$y += 36

# --- 第二步:安装目录 --------------------------------------------------------
New-Label "第 2 步:安装位置" $y | Out-Null
$y += 26
$txtDir = New-Object System.Windows.Forms.TextBox
$txtDir.Location = New-Object System.Drawing.Point(16, ($y - 3))
$txtDir.Size = New-Object System.Drawing.Size(330, 24)
$form.Controls.Add($txtDir)
$btnDirBrowse = New-Object System.Windows.Forms.Button
$btnDirBrowse.Text = "浏览…"
$btnDirBrowse.Location = New-Object System.Drawing.Point(352, ($y - 4))
$btnDirBrowse.Size = New-Object System.Drawing.Size(126, 26)
$form.Controls.Add($btnDirBrowse)
$y += 32
$lblDirHint = New-Label "目标 = <上面的目录>\CodeAgent" $y
$lblDirHint.ForeColor = [System.Drawing.Color]::Gray
$y += 30

# --- 安装按钮 / 进度 ---------------------------------------------------------
$btnInstall = New-Object System.Windows.Forms.Button
$btnInstall.Text = "开始安装"
$btnInstall.Location = New-Object System.Drawing.Point(16, $y)
$btnInstall.Size = New-Object System.Drawing.Size(120, 34)
$btnInstall.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($btnInstall)
$lblStep = New-Label "等待开始" ($y + 7)
$lblStep.ForeColor = [System.Drawing.Color]::DimGray
$lblStep.Location = New-Object System.Drawing.Point(150, ($y + 7))
$lblStep.Size = New-Object System.Drawing.Size(334, 20)
$y += 44

$chkNoShortcut = New-Object System.Windows.Forms.CheckBox
$chkNoShortcut.Text = "不创建桌面/开始菜单快捷方式"
$chkNoShortcut.Location = New-Object System.Drawing.Point(16, $y)
$chkNoShortcut.Size = New-Object System.Drawing.Size(250, 22)
$form.Controls.Add($chkNoShortcut)
$y += 30

# v20 (t_3ea985be): uninstall-mode data purge switch. OFF by default:
# user data is KEPT; only an explicit check adds -PurgeData. Disabled
# while install mode is selected.
$chkPurgeData = New-Object System.Windows.Forms.CheckBox
$chkPurgeData.Text = "卸载时连用户数据一起删除(默认保留)"
$chkPurgeData.Location = New-Object System.Drawing.Point(16, $y)
$chkPurgeData.Size = New-Object System.Drawing.Size(300, 22)
$chkPurgeData.Enabled = $false
$form.Controls.Add($chkPurgeData)
$y += 28

$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Multiline = $true
$txtLog.ReadOnly = $true
$txtLog.ScrollBars = "Vertical"
$txtLog.Font = New-Object System.Drawing.Font("Consolas", 8.5)
$txtLog.Location = New-Object System.Drawing.Point(16, $y)
$txtLog.Size = New-Object System.Drawing.Size(468, 330)
$form.Controls.Add($txtLog)

function Log-Line([string]$s) {
    if ($txtLog.TextLength -gt 200000) { $txtLog.Text = $txtLog.Text.Substring(100000) }
    $txtLog.AppendText($s + "`r`n")
    $txtLog.SelectionStart = $txtLog.TextLength
    $txtLog.ScrollToCaret()
    [System.Windows.Forms.Application]::DoEvents()
}

# ---------------------------------------------------------------------------
# v20 (t_3ea985be): 卸载执行。纯复用 install.ps1 -Uninstall,这里只做
# 清单收集 -> 两次确认 -> 数据保留/搬移 -> 起子进程流式刷日志 -> 复查打印。
# ---------------------------------------------------------------------------
function Get-PythonCoreResidue([string]$Tag) {
    # Count PythonCore\<tag> keys on the three surfaces install.ps1 cleans.
    $n = 0
    foreach ($root in @(
        "HKCU:\Software\Python\PythonCore",
        "HKLM:\Software\Python\PythonCore",
        "HKLM:\Software\WOW6432Node\Python\PythonCore"
    )) {
        try {
            if (Test-Path $root) {
                $ks = @(Get-ChildItem -Path $root -ErrorAction SilentlyContinue |
                    Where-Object { $_.PSChildName -eq $Tag })
                $n += $ks.Count
            }
        } catch { }
    }
    return $n
}

function Invoke-GuiUninstall([string]$TargetDir) {
    # ---- 0) 安装目录必须存在 ----
    if (-not (Test-Path -LiteralPath $TargetDir)) {
        [System.Windows.Forms.MessageBox]::Show(
            "没有找到这个安装目录:`n  $TargetDir`n`n确认路径无误?(卸载以目录存在为准。)",
            "CodeAgent 卸载", "OK", "Warning") | Out-Null
        return
    }

    $btnInstall.Enabled = $false
    try {
        $lblStep.ForeColor = [System.Drawing.Color]::DimGray
        $lblStep.Text = "卸载:正在收集清单…"

        # ---- 1) 收集事实 ----
        $parent = Split-Path -Parent $TargetDir
        $recData = Get-RecordedDataDir $TargetDir
        $cands = Get-DataDirCandidates $TargetDir
        $legacy = $cands[0]
        $inTreeData = $cands[1]
        $inTreeUserData = $cands[2]
        $legacyOwned = Test-LegacyDirOwnedBy $legacy.Path $TargetDir
        $purge = $chkPurgeData.Checked
        $installFiles = Get-FileCount $TargetDir
        $deskLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) "CodeAgent.lnk"
        $smLnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\CodeAgent.lnk"
        $deskLnkExists = (Test-Path -LiteralPath $deskLnk)
        $smLnkExists = (Test-Path -LiteralPath $smLnk)

        # ---- 2) 第一次确认:清单(删什么 / 留什么 / 能不能恢复) ----
        $msg = "【将删除】`n"
        $msg += "  1. 安装目录(约 $installFiles 个文件):`n     $TargetDir`n"
        $msg += "     含 Python/Node 运行时、虚拟环境、包缓存、程序文件。`n"
        $msg += "  2. 本安装创建的注册表登记(Python 3.12 的 PythonCore/卸载项,`n"
        $msg += "     只删指向 $TargetDir 的,别人装的 Python 不动)。`n"
        $msg += "  3. 快捷方式:"
        if ($deskLnkExists) { $msg += " 桌面" }
        if ($smLnkExists) { $msg += " 开始菜单" }
        if (-not ($deskLnkExists -or $smLnkExists)) { $msg += " (未发现,跳过)" }
        $msg += "`n`n"
        $msg += "【将保留】`n"
        if ($purge) {
            $msg += "  (已勾选连数据一起删)无 —— 用户数据也会删除:`n"
        } else {
            $msg += "  1. 新布局用户数据 <安装目录>\data"
            if ($inTreeData.Exists) {
                $msg += " -> 删除前整体搬出到:`n     $parent\CodeAgent-data`n"
            } else { $msg += "(不存在,跳过)`n" }
            $msg += "  2. 新布局浏览器档案 <安装目录>\userdata"
            if ($inTreeUserData.Exists) {
                $msg += " -> 删除前整体搬出到:`n     $parent\CodeAgent-userdata`n"
            } else { $msg += "(不存在,跳过)`n" }
            $msg += "  3. 老布局 %APPDATA%\CodeAgent"
            if ($legacy.Exists) {
                if ($legacyOwned) { $msg += "(存在,有本安装归属标记,但卸载脚本默认不动它)`n" }
                else { $msg += "(存在,不归本安装所有 —— 不会删,需要时请手动处理)`n" }
            } else { $msg += "(不存在,跳过)`n" }
        }
        $msg += "`n【能不能恢复】`n"
        $msg += "  安装目录与注册表项删除后不可恢复;卸载干净后重装 = 全新安装。`n"
        if (-not $purge) {
            $msg += "  保留的数据:重装到同一目录时向导会自动搬回原位;会话/配置不丢。`n"
        }
        if ($recData.Trim() -and ($recData.Trim().TrimEnd('\').ToLower() -ne $legacy.Path.TrimEnd('\').ToLower())) {
            $msg += "`n【安装记录】`n  本安装当时实际使用的数据目录: $recData`n  (不在上面清单里 —— 卸载脚本按归属规则单独处理,见日志。)`n"
        }
        $msg += "`n确认按上面清单执行卸载?"
        Log-Line $msg
        $ans = [System.Windows.Forms.MessageBox]::Show($msg, "卸载清单确认", "YesNo", "Warning")
        if ($ans -ne [System.Windows.Forms.DialogResult]::Yes) {
            $lblStep.Text = "已取消卸载"
            return
        }

        # ---- 3) 第二次确认(破坏性动作前的最后一道门) ----
        $msg2 = "即将开始删除。`n`n删除:$TargetDir(约 $installFiles 个文件)+ 本安装的注册表项 + 快捷方式。`n"
        if ($purge) { $msg2 += "用户数据也一并删除(不可恢复)。`n" }
        else { $msg2 += "用户数据保留(搬出到 $parent\CodeAgent-data 等处)。`n" }
        $msg2 += "`n现在开始?"
        $ans2 = [System.Windows.Forms.MessageBox]::Show($msg2, "最终确认", "YesNo", "Warning")
        if ($ans2 -ne [System.Windows.Forms.DialogResult]::Yes) {
            $lblStep.Text = "已取消卸载"
            return
        }

        # ---- 4) 卸载日志放临时目录(安装目录马上要删,放不进去) ----
        $logFile = Join-Path ([IO.Path]::GetTempPath()) ("codeagent-uninstall-gui-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
        try { [IO.File]::WriteAllText($logFile, ("=== CodeAgent GUI uninstall " + (Get-Date).ToString("s") + " ===`r`n"), (New-Object Text.UTF8Encoding($true))) } catch { }
        Log-Line "[gui] 卸载日志: $logFile"

        # ---- 5) 保留数据:搬出安装树(在安装目录被删之前!) ----
        if (-not $purge) {
            foreach ($pair in @(
                @{ Src = $inTreeData.Path;     Dst = (Join-Path $parent "CodeAgent-data") },
                @{ Src = $inTreeUserData.Path; Dst = (Join-Path $parent "CodeAgent-userdata") }
            )) {
                if (Test-Path -LiteralPath $pair.Src) {
                    $lblStep.Text = "卸载:搬出用户数据 $($pair.Src) …"
                    Log-Line ("[gui] 保留数据: " + $pair.Src + " -> " + $pair.Dst)
                    try {
                        if (Test-Path -LiteralPath $pair.Dst) {
                            Log-Line ("[gui] 目标已存在,改为合并搬入: " + $pair.Dst)
                            # robocopy /E /MOVE: merge into existing dst
                            $null = & robocopy $pair.Src $pair.Dst /E /MOVE /R:2 /W:1 /NFL /NDL /NJH /NJS
                        } else {
                            Move-Item -LiteralPath $pair.Src -Destination $pair.Dst -Force
                        }
                        if (Test-Path -LiteralPath $pair.Src) {
                            Log-Line ("[gui] 警告: 源目录未完全清空: " + $pair.Src)
                        }
                    } catch {
                        Log-Line ("[gui] 搬出失败(继续卸载,数据可能随安装目录被删): " + $_.Exception.Message)
                    }
                }
            }
        }

        # ---- 6) 起子进程:install.ps1 -Uninstall(输出实时刷进日志区) ----
        $psiArgs = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", ('"' + $Script:Installer + '"'),
            "-InstallDir", ('"' + $TargetDir + '"'),
            "-Uninstall", "-Yes"
        )
        if ($purge) { $psiArgs += "-PurgeData" }
        Log-Line ("[gui] 调用: powershell " + ($psiArgs -join " "))
        try { [IO.File]::AppendAllText($logFile, ("powershell " + ($psiArgs -join " ") + "`r`n"), (New-Object Text.UTF8Encoding($true))) } catch { }

        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = "powershell.exe"
        $psi.Arguments = ($psiArgs -join " ")
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
        $psi.WorkingDirectory = $Script:GuiRoot

        $lblStep.Text = "卸载:正在执行(详情见下方日志)…"
        $proc = New-Object System.Diagnostics.Process
        $proc.StartInfo = $psi
        $outHandler = {
            param($sender, $e)
            if ($e.Data) {
                $line = $e.Data
                $txtLog.AppendText($line + "`r`n")
                if ($line -match '^\[uninstall\]') { $lblStep.Text = $line }
                $txtLog.SelectionStart = $txtLog.TextLength
                $txtLog.ScrollToCaret()
                [System.Windows.Forms.Application]::DoEvents()
                try { [IO.File]::AppendAllText($script:uninstallGuiLogFile, $line + "`r`n", (New-Object Text.UTF8Encoding($true))) } catch { }
            }
        }
        $errHandler = {
            param($sender, $e)
            if ($e.Data) {
                $txtLog.AppendText("[stderr] " + $e.Data + "`r`n")
                $txtLog.SelectionStart = $txtLog.TextLength
                $txtLog.ScrollToCaret()
                [System.Windows.Forms.Application]::DoEvents()
                try { [IO.File]::AppendAllText($script:uninstallGuiLogFile, ("[stderr] " + $e.Data + "`r`n"), (New-Object Text.UTF8Encoding($true))) } catch { }
            }
        }
        # event handlers run in a dynamic scope -- pass the log path via a
        # script-scoped variable so they can see it (closure vars are lost
        # across Register-ObjectEvent boundaries in PS 5.1)
        $script:uninstallGuiLogFile = $logFile
        $null = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action $outHandler
        $null = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $errHandler
        $null = $proc.Start()
        $proc.BeginOutputReadLine()
        $proc.BeginErrorReadLine()
        while (-not $proc.HasExited) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 150
        }
        Unregister-Event -SourceIdentifier "OutputDataReceived" -ErrorAction SilentlyContinue
        Get-Job | Where-Object { $_.State -eq "Running" } | Stop-Job -ErrorAction SilentlyContinue
        Get-Job | Remove-Job -Force -ErrorAction SilentlyContinue
        $rc = $proc.ExitCode
        Log-Line ("[gui] uninstall 退出码: $rc")

        # ---- 7) 复查并打印 ----
        $lblStep.Text = "卸载:复查中…"
        Log-Line ""
        Log-Line "[gui] === 卸载后复查 ==="
        $allOk = ($rc -eq 0)
        $dirGone = -not (Test-Path -LiteralPath $TargetDir)
        Log-Line ("[gui] 安装目录已不存在: " + $(if ($dirGone) { "YES" } else { "NO(有残留!)" }))
        if (-not $dirGone) { $allOk = $false }
        $pyRes = Get-PythonCoreResidue "3.12"
        Log-Line ("[gui] 注册表 PythonCore 3.12 残留: $pyRes 处" + $(if ($pyRes -gt 0) { "(注意:可能是别人装的 Python,卸载脚本按归属判断,不一定是问题)" } else { "" }))
        $lnkDesk = Test-Path -LiteralPath $deskLnk
        $lnkSm = Test-Path -LiteralPath $smLnk
        Log-Line ("[gui] 桌面快捷方式已删: " + $(if (-not $lnkDesk) { "YES" } else { "NO(仍存在)" }))
        Log-Line ("[gui] 开始菜单快捷方式已删: " + $(if (-not $lnkSm) { "YES" } else { "NO(仍存在)" }))
        # data dir states (recompute: in-tree dirs were either moved out or purged)
        $cands2 = Get-DataDirCandidates $TargetDir
        foreach ($d in $cands2) {
            $state = ""
            if ($purge) {
                $state = $(if ($d.Exists) { "仍存在(未按要求删除!)" } else { "已删除" })
                if ($d.Path -eq $legacy.Path -and $d.Exists -and -not $legacyOwned) { $state = "仍存在(不归本安装,按安全规则不动)" }
            } else {
                if ($d.Path -eq $legacy.Path) {
                    $state = $(if ($d.Exists) { "已保留(老布局,默认不动)" } else { "不存在(无需保留)" })
                } else {
                    $state = $(if ($d.Exists) { "仍存在(搬出未完成!)" } else { "已搬出保留" })
                }
            }
            Log-Line ("[gui] 数据目录 " + $d.Path + " : " + $state)
        }
        if (-not $purge) {
            foreach ($keep in @((Join-Path $parent "CodeAgent-data"), (Join-Path $parent "CodeAgent-userdata"))) {
                if (Test-Path -LiteralPath $keep) { Log-Line ("[gui] 保留的数据在: " + $keep) }
            }
            Log-Line ("[gui] 重装到同一目录时,这些数据会被自动搬回 " + $TargetDir)
        }
        if ($allOk) {
            $lblStep.ForeColor = [System.Drawing.Color]::Green
            $lblStep.Text = "卸载完成"
            $msgDone = "卸载完成。`n`n"
            $msgDone += $(if ($purge) { "用户数据已按选择一并删除。" } else { "用户数据已保留(重装同目录自动搬回)。" })
            $msgDone += "`n日志: $logFile"
            [System.Windows.Forms.MessageBox]::Show($msgDone, "CodeAgent 卸载完成", "OK", "Information") | Out-Null
        } else {
            $lblStep.ForeColor = [System.Drawing.Color]::Firebrick
            $lblStep.Text = "卸载未完全干净(退出码 $rc)—— 见日志区复查结果"
            [System.Windows.Forms.MessageBox]::Show(
                "卸载完成但有残留(退出码 $rc),详见日志区与:`n$logFile`n`n残留的常见原因:文件被占用(关掉正在运行的 CodeAgent 后重试一次)。", "卸载未完全干净", "OK", "Warning") | Out-Null
        }
    } catch {
        $lblStep.ForeColor = [System.Drawing.Color]::Firebrick
        $lblStep.Text = "出错: $($_.Exception.Message)"
        Log-Line ("[gui] 异常: " + $_.Exception.ToString())
        [System.Windows.Forms.MessageBox]::Show("卸载过程中出错:`n$($_.Exception.Message)", "出错", "OK", "Error") | Out-Null
    } finally {
        $btnInstall.Enabled = $true
    }
}

$btnPyBrowse.Add_Click({
    $p = Pick-Exe "选择已有的 python.exe"
    if ($p) { $txtPy.Text = $p; $lblPyResult.Text = "" }
})
$btnNodeBrowse.Add_Click({
    $p = Pick-Exe "选择已有的 node.exe"
    if ($p) { $txtNode.Text = $p; $lblNodeResult.Text = "" }
})
$btnDirBrowse.Add_Click({
    $p = Pick-Dir "选择 CodeAgent 的安装父目录"
    if ($p) { $txtDir.Text = $p }
})
$btnPyProbe.Add_Click({
    $lblPyResult.Text = "检测中…"
    [System.Windows.Forms.Application]::DoEvents()
    $r = Get-ExeProbe $txtPy.Text.Trim()
    if ($null -eq $r) { $lblPyResult.Text = "留空 = 帮我安装(装进 CodeAgent 目录,自包含)" }
    elseif ($r -match "^(Python \d|无法启动|退出码|文件不存在|启动后)") {
        $lblPyResult.ForeColor = [System.Drawing.Color]::Firebrick
        $lblPyResult.Text = "<不可用: $r>"
    } else {
        $lblPyResult.ForeColor = [System.Drawing.Color]::Green
        $lblPyResult.Text = "<可运行: $r>"
    }
})
$btnNodeProbe.Add_Click({
    $lblNodeResult.Text = "检测中…"
    [System.Windows.Forms.Application]::DoEvents()
    $r = Get-ExeProbe $txtNode.Text.Trim()
    if ($null -eq $r) { $lblNodeResult.Text = "留空 = 帮我安装(装进 CodeAgent 目录,自包含)" }
    elseif ($r -match "^(v\d|无法启动|退出码|文件不存在|启动后)") {
        $lblNodeResult.ForeColor = [System.Drawing.Color]::Firebrick
        $lblNodeResult.Text = "<不可用: $r>"
    } else {
        $lblNodeResult.ForeColor = [System.Drawing.Color]::Green
        $lblNodeResult.Text = "<可运行: $r>"
    }
})

# ---------------------------------------------------------------------------
# 安装执行
# ---------------------------------------------------------------------------
$btnInstall.Add_Click({
    # ---- 参数收集与校验 ----
    $pyPath = $txtPy.Text.Trim()
    $nodePath = $txtNode.Text.Trim()
    $parent = $txtDir.Text.Trim()
    if (-not $parent) {
        [System.Windows.Forms.MessageBox]::Show("请先填写安装父目录(第 2 步)。", "提示", "OK", "Warning") | Out-Null
        return
    }
    if ($parent -notmatch '^[A-Za-z]:\\') {
        [System.Windows.Forms.MessageBox]::Show("安装目录需要是一个本地磁盘的绝对路径,例如 D:\software。当前输入: $parent", "提示", "OK", "Warning") | Out-Null
        return
    }
    $target = (Join-Path $parent "CodeAgent").TrimEnd('\')   # v19.2: no trailing separator!
    # A trailing backslash inside the quoted -InstallDir argument becomes
    # \" (an escaped quote) and breaks the whole command line: install.ps1
    # exits 1 with no output at all (real-machine repro 2026-10-13 09:51).

    # v20 (t_3ea985be): UNINSTALL MODE takes its own path; everything
    # below is the install flow.
    if ($rdoUninstall.Checked) {
        Invoke-GuiUninstall $target
        return
    }

    # v20: INSTALL with a previous uninstall's salvage present -> move the
    # kept data back into the target BEFORE install.ps1 runs, so the
    # user's sessions/config survive a reinstall (keeps -Uninstall's
    # "deinstall then reinstall" promise symmetric). Only automatic when
    # the install would otherwise create a fresh dir; never overwrites
    # non-salvage content silently.
    foreach ($pair in @(
        @{ Src = (Join-Path $parent "CodeAgent-data");     Dst = (Join-Path $target "data") },
        @{ Src = (Join-Path $parent "CodeAgent-userdata"); Dst = (Join-Path $target "userdata") }
    )) {
        if ((Test-Path -LiteralPath $pair.Src) -and (Test-Path -LiteralPath $target) -and
            (-not (Test-Path -LiteralPath $pair.Dst))) {
            $ans = [System.Windows.Forms.MessageBox]::Show(
                "检测到上次卸载时保留的用户数据:`n  $($pair.Src)`n`n搬回 $($pair.Dst) 吗?(推荐:会话/配置不丢。选「否」= 继续留着,不搬。)",
                "发现保留的用户数据", "YesNo", "Question")
            if ($ans -eq [System.Windows.Forms.DialogResult]::Yes) {
                try {
                    Log-Line ("[gui] 搬回用户数据: " + $pair.Src + " -> " + $pair.Dst)
                    Move-Item -LiteralPath $pair.Src -Destination $pair.Dst -Force
                } catch {
                    [System.Windows.Forms.MessageBox]::Show("搬回失败(不影响安装,数据仍在 $pair.Src):`n$($_.Exception.Message)", "提示", "OK", "Warning") | Out-Null
                }
            }
        }
    }

    # 探测填了的路径是否可用(不阻塞,给红字提示,用户自己决定)
    if ($pyPath) {
        $r = Get-ExeProbe $pyPath
        if ($null -ne $r -and $r -notmatch '^Python \d') {
            $ans = [System.Windows.Forms.MessageBox]::Show(
                "python.exe 检测结果: $r`n`n仍然使用这个路径吗?(选「否」会清空,改为帮我安装)", "Python 路径不可用", "YesNo", "Warning")
            if ($ans -ne [System.Windows.Forms.DialogResult]::Yes) { $txtPy.Text = ""; $pyPath = "" }
        }
    }
    if ($nodePath) {
        $r = Get-ExeProbe $nodePath
        if ($null -ne $r -and $r -notmatch '^v\d') {
            $ans = [System.Windows.Forms.MessageBox]::Show(
                "node.exe 检测结果: $r`n`n仍然使用这个路径吗?(选「否」会清空,改为帮我安装)", "Node 路径不可用", "YesNo", "Warning")
            if ($ans -ne [System.Windows.Forms.DialogResult]::Yes) { $txtNode.Text = ""; $nodePath = "" }
        }
    }

    # ---- 分支 C:<父目录>\CodeAgent 已存在 -> 明确提示,不许静默覆盖 ----
    $prelaidNode = $false
    if (Test-Path -LiteralPath $target) {
        $hasMarker = Test-Path -LiteralPath (Join-Path $target ".install-state.json")
        $msg = "目标目录已存在:`n  $target`n`n"
        if ($hasMarker) {
            $msg += "检测到之前的安装标记。选择「是」= 在原目录续装/覆盖更新(用户配置 config.json/.env 会保留);"
            $msg += "选择「否」= 换一个目录。`n要怎么处理?"
        } else {
            $msg += "目录非空且没有本向导的安装标记。选择「是」= 继续装进这个目录(内容可能被覆盖更新);"
            $msg += "选择「否」= 换一个目录。`n要怎么处理?"
        }
        $ans = [System.Windows.Forms.MessageBox]::Show($msg, "CodeAgent 目录已存在", "YesNo", "Warning")
        if ($ans -ne [System.Windows.Forms.DialogResult]::Yes) {
            $lblStep.Text = "已取消:请更换父目录后重试"
            return
        }
    } else {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }

    $logFile = Join-Path $target "install-gui.log"

    $btnInstall.Enabled = $false
    $lblStep.Text = "准备中…"
    try {
        # ---- 预置:用户给了 node.exe -> 复制整个 node 目录到 <target>\runtime\node,
        # install.ps1 会检测到 runtime\node\node.exe 并复用,不再下载。
        if ($nodePath -and -not (Test-Path -LiteralPath (Join-Path $target "runtime\node\node.exe"))) {
            $lblStep.Text = "步骤 1/4:复制已有 Node 到目标目录…"
            Log-Line "[gui] 复制已有 Node: $nodePath -> $target\runtime\node"
            $nodeSrcDir = Split-Path -Parent $nodePath
            $nodeDst = Join-Path $target "runtime\node"
            New-Item -ItemType Directory -Path $nodeDst -Force | Out-Null
            Copy-Item -LiteralPath (Join-Path $nodeSrcDir "*") -Destination $nodeDst -Recurse -Force
            $prelaidNode = $true
            Log-Line ("[gui] Node 预置完成: " + (Join-Path $nodeDst "node.exe"))
        }

        # ---- 组装 install.ps1 参数(只做增量,不改既有语义) ----
        $psiArgs = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", ('"' + $Script:Installer + '"'),
            "-InstallDir", ('"' + $target + '"'),
            "-CacheDir", ('"' + (Join-Path $target "runtime\cache") + '"'),  # v19: pip/npm 缓存全部收进安装树
            "-Yes"
        )
        $modeDesc = ""
        if ($pyPath) {
            # 分支 A:复用用户解释器(-PythonPath 是既有参数)
            $psiArgs += @("-PythonPath", ('"' + $pyPath + '"'))
            $modeDesc = "复用已有 Python"
        } else {
            # 分支 B:自包含 -> PBS 便携版 Python(不写注册表、无需管理员、整目录可删)
            $psiArgs += @("-UsePbs")
            $modeDesc = "自包含安装 Python(便携版,不写注册表)"
        }
        if (-not $nodePath -and -not $prelaidNode) {
            $modeDesc += ";Node 将下载装进目标目录"
        }
        if ($chkNoShortcut.Checked) { $psiArgs += "-NoShortcut" }
        Log-Line ("[gui] 目标目录: " + $target)
        Log-Line ("[gui] 模式: " + $modeDesc)
        Log-Line ("[gui] 完整日志: " + $logFile)
        $cmdLine = "powershell " + ($psiArgs -join " ")
        Log-Line ("[gui] 调用: " + $cmdLine)
        try { [IO.File]::WriteAllText($logFile, ("=== CodeAgent GUI install " + (Get-Date).ToString("s") + " ===`r`n" + $cmdLine + "`r`n"), (New-Object Text.UTF8Encoding($true))) } catch { }

        # ---- 进程:行流式读输出 -> GUI + 日志文件 ----
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = "powershell.exe"
        $psi.Arguments = ($psiArgs -join " ")
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
        $psi.WorkingDirectory = $Script:GuiRoot

        $lblStep.Text = "步骤 2/4:正在安装(详情见下方日志)…"
        $proc = New-Object System.Diagnostics.Process
        $proc.StartInfo = $psi
        # 注册同步事件回调,避免 ReadToEnd() 死锁
        $outHandler = {
            param($sender, $e)
            if ($e.Data) {
                $line = $e.Data
                $txtLog.AppendText($line + "`r`n")
                if ($line -match '^\[install\]') {
                    # 步骤文案:把 [install] 行摘要到步骤标签
                    $lblStep.Text = $line
                }
                $txtLog.SelectionStart = $txtLog.TextLength
                $txtLog.ScrollToCaret()
                [System.Windows.Forms.Application]::DoEvents()
                try { [IO.File]::AppendAllText($logFile, $line + "`r`n", (New-Object Text.UTF8Encoding($true))) } catch { }
            }
        }
        $errHandler = {
            param($sender, $e)
            if ($e.Data) {
                $txtLog.AppendText("[stderr] " + $e.Data + "`r`n")
                $txtLog.SelectionStart = $txtLog.TextLength
                $txtLog.ScrollToCaret()
                [System.Windows.Forms.Application]::DoEvents()
                try { [IO.File]::AppendAllText($logFile, "[stderr] " + $e.Data + "`r`n", (New-Object Text.UTF8Encoding($true))) } catch { }
            }
        }
        $null = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action $outHandler
        $null = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $errHandler
        $null = $proc.Start()
        $proc.BeginOutputReadLine()
        $proc.BeginErrorReadLine()
        while (-not $proc.HasExited) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 150
        }
        Unregister-Event -SourceIdentifier ("OutputDataReceived") -ErrorAction SilentlyContinue
        Get-Job | Where-Object { $_.State -eq "Running" } | Stop-Job -ErrorAction SilentlyContinue
        Get-Job | Remove-Job -Force -ErrorAction SilentlyContinue
        $rc = $proc.ExitCode
        Log-Line ("[gui] install.ps1 退出码: $rc")

        # ---- 收尾 ----
        if ($rc -eq 0) {
            $lblStep.ForeColor = [System.Drawing.Color]::Green
            $lblStep.Text = "步骤 4/4:安装完成"
            $bat = Join-Path $target "CodeAgent.bat"
            $msg = "安装完成!`n`n  启动:双击 $bat`n  (或桌面/开始菜单的 CodeAgent 快捷方式)`n`n"
            $msg += "首次使用请编辑:`n  $target\config.json (模型配置)`n  $target\.env (API 密钥)`n`n"
            $msg += "目录结构(全部自包含,整个目录可整体搬走/删除):`n  $target\runtime\python    Python 运行时`n  $target\runtime\node      Node 运行时`n  $target\runtime\cache     pip/npm 包缓存`n  $target\python\.venv    虚拟环境`n`n"
            $msg += "卸载:重新打开本向导,顶部选「卸载」,选中这个安装目录即可。`n  (向导会列清单、二次确认、默认保留用户数据,复查后收尾。)"
            Log-Line $msg
            [System.Windows.Forms.MessageBox]::Show($msg, "CodeAgent 安装完成", "OK", "Information") | Out-Null
        } else {
            $lblStep.ForeColor = [System.Drawing.Color]::Firebrick
            $lblStep.Text = "安装失败(退出码 $rc)—— 日志见 $logFile"
            [System.Windows.Forms.MessageBox]::Show(
                "安装失败,退出码 $rc。`n完整日志: $logFile`n`n常见原因:网络不通(镜像下载失败)、磁盘空间不足。`n修复后可直接重试,已下载的包会被复用。", "安装失败", "OK", "Error") | Out-Null
        }
    } catch {
        $lblStep.ForeColor = [System.Drawing.Color]::Firebrick
        $lblStep.Text = "出错: $($_.Exception.Message)"
        Log-Line ("[gui] 异常: " + $_.Exception.ToString())
        [System.Windows.Forms.MessageBox]::Show("安装过程中出错:`n$($_.Exception.Message)`n`n日志: $logFile", "出错", "OK", "Error") | Out-Null
    } finally {
        $btnInstall.Enabled = $true
    }
})

[void]$form.ShowDialog()
