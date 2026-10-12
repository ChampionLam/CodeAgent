# CodeAgent 可视化安装向导 (v19, task t_941de5b6)
# PowerShell 5.1 + WinForms -- 无任何第三方依赖,不需要 Python/Node/管理员权限。
# 收集参数后调用 scripts/install.ps1 完成实际安装(复用,不重写)。
# 用法: 双击 install-gui.bat,或右键 install-gui.ps1 -> 使用 PowerShell 运行。

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
$form.Size = New-Object System.Drawing.Size(720, 660)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false

$y = 12
function New-Label([string]$Text, [int]$Top) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $Text
    $l.Location = New-Object System.Drawing.Point(16, $Top)
    $l.Size = New-Object System.Drawing.Size(680, 20)
    $form.Controls.Add($l)
    return $l
}

# --- 第一步:Python / Node 路径 ---------------------------------------------
New-Label "第 1 步:已有的 Python / Node(选填。留空 = 我没有,帮我装进 CodeAgent 目录)" $y | Out-Null
$y += 26

$lblPy = New-Object System.Windows.Forms.Label
$lblPy.Text = "python.exe 路径:"
$lblPy.Location = New-Object System.Drawing.Point(16, $y)
$lblPy.Size = New-Object System.Drawing.Size(100, 20)
$form.Controls.Add($lblPy)
$txtPy = New-Object System.Windows.Forms.TextBox
$txtPy.Location = New-Object System.Drawing.Point(120, $y - 3)
$txtPy.Size = New-Object System.Drawing.Size(440, 24)
$form.Controls.Add($txtPy)
$btnPyBrowse = New-Object System.Windows.Forms.Button
$btnPyBrowse.Text = "浏览…"
$btnPyBrowse.Location = New-Object System.Drawing.Point(570, $y - 4)
$btnPyBrowse.Size = New-Object System.Drawing.Size(70, 26)
$form.Controls.Add($btnPyBrowse)
$btnPyProbe = New-Object System.Windows.Forms.Button
$btnPyProbe.Text = "检测"
$btnPyProbe.Location = New-Object System.Drawing.Point(646, $y - 4)
$btnPyProbe.Size = New-Object System.Drawing.Size(56, 26)
$form.Controls.Add($btnPyProbe)
$y += 32
$lblPyResult = New-Label "" $y
$lblPyResult.ForeColor = [System.Drawing.Color]::Gray
$y += 30

$lblNode = New-Object System.Windows.Forms.Label
$lblNode.Text = "node.exe 路径:"
$lblNode.Location = New-Object System.Drawing.Point(16, $y)
$lblNode.Size = New-Object System.Drawing.Size(100, 20)
$form.Controls.Add($lblNode)
$txtNode = New-Object System.Windows.Forms.TextBox
$txtNode.Location = New-Object System.Drawing.Point(120, $y - 3)
$txtNode.Size = New-Object System.Drawing.Size(440, 24)
$form.Controls.Add($txtNode)
$btnNodeBrowse = New-Object System.Windows.Forms.Button
$btnNodeBrowse.Text = "浏览…"
$btnNodeBrowse.Location = New-Object System.Drawing.Point(570, $y - 4)
$btnNodeBrowse.Size = New-Object System.Drawing.Size(70, 26)
$form.Controls.Add($btnNodeBrowse)
$btnNodeProbe = New-Object System.Windows.Forms.Button
$btnNodeProbe.Text = "检测"
$btnNodeProbe.Location = New-Object System.Drawing.Point(646, $y - 4)
$btnNodeProbe.Size = New-Object System.Drawing.Size(56, 26)
$form.Controls.Add($btnNodeProbe)
$y += 32
$lblNodeResult = New-Label "" $y
$lblNodeResult.ForeColor = [System.Drawing.Color]::Gray
$y += 36

# --- 第二步:安装目录 --------------------------------------------------------
New-Label "第 2 步:安装位置(将在此目录下创建 CodeAgent 子目录并全部装进去)" $y | Out-Null
$y += 26
$txtDir = New-Object System.Windows.Forms.TextBox
$txtDir.Location = New-Object System.Drawing.Point(16, $y - 3)
$txtDir.Size = New-Object System.Drawing.Size(544, 24)
$form.Controls.Add($txtDir)
$btnDirBrowse = New-Object System.Windows.Forms.Button
$btnDirBrowse.Text = "浏览…"
$btnDirBrowse.Location = New-Object System.Drawing.Point(570, $y - 4)
$btnDirBrowse.Size = New-Object System.Drawing.Size(132, 26)
$form.Controls.Add($btnDirBrowse)
$y += 32
$lblDirHint = New-Label "目标 = <上面的目录>\CodeAgent(支持中文、空格路径;全程无需管理员权限)" $y
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
$lblStep.Location = New-Object System.Drawing.Point(150, $y + 7)
$lblStep.Size = New-Object System.Drawing.Size(550, 20)
$y += 44

$chkNoShortcut = New-Object System.Windows.Forms.CheckBox
$chkNoShortcut.Text = "不创建桌面/开始菜单快捷方式"
$chkNoShortcut.Location = New-Object System.Drawing.Point(16, $y)
$chkNoShortcut.Size = New-Object System.Drawing.Size(250, 22)
$form.Controls.Add($chkNoShortcut)
$y += 30

$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Multiline = $true
$txtLog.ReadOnly = $true
$txtLog.ScrollBars = "Vertical"
$txtLog.Font = New-Object System.Drawing.Font("Consolas", 8.5)
$txtLog.Location = New-Object System.Drawing.Point(16, $y)
$txtLog.Size = New-Object System.Drawing.Size(686, 320)
$form.Controls.Add($txtLog)

function Log-Line([string]$s) {
    if ($txtLog.TextLength -gt 200000) { $txtLog.Text = $txtLog.Text.Substring(100000) }
    $txtLog.AppendText($s + "`r`n")
    $txtLog.SelectionStart = $txtLog.TextLength
    $txtLog.ScrollToCaret()
    [System.Windows.Forms.Application]::DoEvents()
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
    if ($parent -notmatch '^[A-Za-z]:\') {
        [System.Windows.Forms.MessageBox]::Show("安装目录需要是一个本地磁盘的绝对路径,例如 D:\software。当前输入: $parent", "提示", "OK", "Warning") | Out-Null
        return
    }
    $target = Join-Path (Join-Path $parent "CodeAgent") ""

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
            $uninstall = " powershell -NoProfile -ExecutionPolicy Bypass -File `"<仓库>\scripts\install.ps1`" -InstallDir `"$target`" -Uninstall -PurgeData"
            $msg = "安装完成!`n`n  启动:双击 $bat`n  (或桌面/开始菜单的 CodeAgent 快捷方式)`n`n"
            $msg += "首次使用请编辑:`n  $target\config.json (模型配置)`n  $target\.env (API 密钥)`n`n"
            $msg += "目录结构(全部自包含,整个目录可整体搬走/删除):`n  $target\runtime\python    Python 运行时`n  $target\runtime\node      Node 运行时`n  $target\runtime\cache     pip/npm 包缓存`n  $target\python\.venv    虚拟环境`n`n"
            $msg += "卸载/清理:删除整个 $target 目录即可(注册表、系统目录均无残留;`n  %APPDATA%\CodeAgent 用户数据目录默认保留,如需一并删除:`n  $uninstall)"
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
