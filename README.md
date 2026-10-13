# CodeAgent

跑在自己电脑上的桌面 AI Agent。Electron 做壳与渲染，Python 做 sidecar（agent 循环、工具、技能、子代理都在这一侧），模型走任意 OpenAI 兼容接口。

> 状态：早期版本（v0.1.0）。目前主要在 Windows 上开发与验证（开发机是 Linux，目标机是 Windows）。

## 它是什么

不是一个"聊天框包一层壳"，而是一个能真正动手的本地 agent：

- **17 个工具**：读写文件、改文件、删路径、列目录、搜内容、执行命令、读/列/管理技能、存/删长期规则、跨会话检索、派子代理、抓网页、搜网页、生成图片。工具可在界面上逐个开关，关掉就不进模型的函数表。
- **技能**：`SKILL.md`（frontmatter + 正文）形式的操作手册，提示词里只带一行短描述，正文用 `read_skill` 按需读。内置技能只读；模型想存新技能会**先问你**，你点头才写。
- **子代理**：`delegate` 工具一次派最多 3 个节点并行干活，结果合并成一条消息回灌；节点失败会如实标 `failed`。可用内存或核数不够时**显式降级**并说明理由，不硬撑。
- **断点续跑**：任务中断（关应用、断网、限流）能续，半截正文也算断点，由你点"继续"。
- **审批**：危险操作在会话里出卡片让你裁决，不是弹一个拦不住的窗。**这是防手滑的护栏，不是安全边界**——见下节「安全边界与限制」。
- **多模型**：任意 OpenAI 兼容端点（官方 / 中转 / 本地 Ollama），key 只写 `.env`，界面不回读、不显示、不入日志。

## 架构

```
渲染层 (ui/, React + Vite)          ← IPC (preload 白名单桥)
      ↓
Electron 主进程 (electron/)          ← 单实例锁、窗口、sidecar 生命周期
      ↓ stdio + JSON-RPC 帧
Python sidecar (python/)             ← agent 循环、工具执行、技能、子代理、会话库
      ↓
模型 (OpenAI 兼容) / 本机文件系统 / 网络
```

- 只有主进程能碰 Node API，渲染层通过 `preload.ts` 暴露的窄接口说话。
- 会话与断点存 SQLite（`sessions.db` / `runs/`），落在 Electron 的 userData 目录下，不在仓库里。
- 工具层不 import 配置模块，配置读写集中在 sidecar（有测试盯着这条边界）。

## 目录

| 目录 | 内容 |
|---|---|
| `electron/` | 主进程、preload、渲染层入口与打包产物 |
| `python/` | sidecar：`agent_loop.py`（循环）、`tools.py`（工具）、`subagent.py`（子代理）、`skills_registry.py`（技能）、`llm.py`（模型）、`modelconfig.py`（配置与预置） |
| `ui/` | 界面源码（React + Vite）。构建产物拷进 `electron/ui/` 由应用加载 |
| `resources/skills/` | 内置技能 |
| `docs/` | 设计文档（子代理规格、权限、技能与工具、模型配置契约…） |
| `tests/` | 75 个测试文件（67 个 Python unittest + 8 个 Node 原生 test） |
| `scripts/` | 冒烟脚本 |

## 快速开始（Windows）

**目标平台是 Windows**（开发与验证都在 Windows 上做），所以快速开始以 PowerShell 为准。Linux / macOS 用来改代码、跑测试，见下一节。

依赖：**Node ≥ 20**（`npm test` 要 **Node ≥ 22**）、**Python ≥ 3.10**。

```powershell
# 1) 应用依赖
npm install

# 2) sidecar 依赖（图片 + 文档提取 + 纯文本 + OCR 兜底）—— 一条命令装齐
#    建 python\.venv，装上 requirements.txt 与 requirements-docs.txt，末尾自检
#    并列出 Pillow / anydoc / pymupdf / pypdf / openpyxl / python-docx 装没装。
#    意义：PDF / Word / Excel / PPT / CSV 是**功能自带**能力，部署时就该可用，
#    不该等用户拖进来才发现库没装。
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-venv.ps1
#    国内网络装 anydoc 的 Rust 轮子慢，可临时换源（只作用于当次，不改机器 pip 配置）：
#    powershell -ExecutionPolicy Bypass -File scripts\bootstrap-venv.ps1 -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple

# 3) 构建界面（产物落到 electron\ui\，应用从那里加载）+ 顺带拷好 config.json 与空白 .env
powershell -ExecutionPolicy Bypass -File scripts\build-ui.ps1

# 4) 填密钥（值只写 .env；变量名与 config.json 里的 apiKeyEnv 对应，别写进 config.json）
notepad .env
#    .env 必须是 UTF-8 **不带 BOM**：PowerShell 5.1 的 `>` 会写成 UTF-16，应用读不出来。
#    build-ui.ps1 用 [IO.File]::WriteAllText + UTF8Encoding($false) 写，别手工重定向覆盖它。

# 5) 跑起来
npm start
```

`config.example.json` 是**空白模板**（模型/端点都留空）——用谁家的模型由你决定：启动后在界面「设置 → 添加模型」里选对接方式即可，也可以直接编辑 `config.json`。

## 快速开始（Linux / macOS）

只用于改代码、跑测试；不是发布目标。

```bash
npm install
bash scripts/bootstrap-venv.sh
cd ui && npm install && npm run build && cd ..
rm -rf electron/ui && mkdir -p electron/ui && cp -r ui/dist/* electron/ui/
cp config.example.json config.json && printf 'OPENAI_API_KEY=...\n' > .env
npm start
```

## 测试

```powershell
npm test                                                       # TS 侧（99 条）
python -m unittest discover -s tests -t tests -p "test_*.py"    # Python 侧（1219 条）
```

Linux / macOS 上把 `python` 换成 `python3`。

本仓当前实测（Linux / Python 3.11，`python/requirements.txt` + `requirements-docs.txt` + `requirements-guard.txt` 三份装齐）：Python 全量 **1219 条 OK**（跳过 2 条专门的 Windows 探测用例——反过来 Windows 上跳过的是那 2 条符号链接用例，建符号链接要管理员权限）、TS **99 条全过**、`npm run typecheck` 与 `npm run build` 退出码 0。

> PowerShell 下不用管通配符：`npm test` 里的 `tests/*.test.ts` 由 node 自己展开（`--test` 从 Node 21 起认 glob）；Node 20 上请用 POSIX shell 或升级 node。
> CI 目前只在 Linux 上跑（Python 测试 / Node 测试 / typecheck / UI 构建，见 `.github/workflows/ci.yml`），**Windows 侧还没进 CI**——Windows 上的数字要在真机上手工跑（上述条数是本仓在 Linux 上跑出来的，未代填 Windows 数字）。

## 配置说明

- `config.json`（**不进仓库**）：模型列表、默认模型、工具开关、工作区等用户设置。
- `.env`（**不进仓库**）：密钥值。`config.json` 里只写变量**名**（如 `OPENROUTER_API_KEY`），值从环境读。
- `config.example.json`：可以入库的示例骨架。
- 用户数据（会话库 `sessions.db`、断点 `runs/`、用量、工作区）都在 Electron 的 userData 目录下（`%APPDATA%\CodeAgent` / `~/Library/Application Support/CodeAgent` / `~/.config/CodeAgent`），**不在仓库里**。

## 首次配置（安装包用户）

安装脚本产出的目标目录是**全空配置**：源码树里开发用的 `.env` / `config.json` **永远不会被拷过去**（拷贝按文件名排除，且拷完有硬断言——目标里若出现非空密钥，安装直接失败停止）。装完之后你自己填：

1. **模型**：编辑 `<InstallDir>\config.json`。它初始是 `config.example.json` 的空白模板：`model.baseUrl` / `model.model` 都为空，没有可用模型。填上你要用的 OpenAI 兼容端点与模型名（变量名只填 `apiKeyEnv`，**别把密钥值写进这个文件**）。
2. **密钥**：编辑 `<InstallDir>\.env`，初始为 0 字节空文件。按行写 `变量名=值`，变量名与 `config.json` 里 `apiKeyEnv` 对应。文件保持 UTF-8 **不带 BOM**（PowerShell 5.1 的 `>` 重定向会写成 UTF-16 导致读不出，别用 `>` 覆盖它）。

装完 `config.json` 和 `.env` 都还是空的——**不填完这两个文件，聊天功能起不来**。安装完成的输出里也会明确写一行 `config is BLANK -- edit config.json (models) and .env (keys) before use`。

**运行时来源（同版本已装则复用）：** 安装器启动时会查册本机是否已**登记**过钉死版本 Python 3.12.10（注册表三处 PythonCore + ARP 卸载项 + WiX Dependencies）。已登记且解释器实跑通过 → **直接复用它建 venv，不再下载、不再跑安装器**。已登记但文件已删（悬空登记）→ **明确报错**并给出可执行指引（先 `/repair /quiet` 再 `/uninstall /quiet`，或手动清理注册表）——绝不静默跳过。因为**官方安装器是"每版本单实例"：同一版本装不了第二份**，机器上已有同版本登记时，官方安装器会静默退出 0 却什么都不装（日志特征 `WixBundleInstalled = 1` 或 Detect 阶段各包 `Present`）。这正是本脚本装完必查 `runtime\python\python.exe` 真实存在的原因。

仓库卫生（开发侧）：`.gitignore` 忽略 `config.json` 与 `.env`（`config.example.json` 是可入库的空模板），pre-commit 钩子同样拦截这两类文件。

## 安全边界与限制（请先读这一节）

- **命令审批是防手滑的 UX 护栏，不是安全边界。** `python/guard.py` 是黑名单正则（要求命令名落在命令位上），必然有绕过面：`/bin/rm -rf /`、`rm$IFS-rf$IFS/`、`X=rm; $X -rf /`、`xargs rm`、`find . -delete`、`python -c "import shutil; shutil.rmtree('/')"` 都不会被拦（这些是实测结论，不是推测）。它拦的是**手滑**，不是恶意。
- 真正的安全边界在**网络层**（`python/web/egress.py`：私网拒绝、DNS 钉死、跨站重定向重验、字节上限）与**密钥治理**（key 只走 `.env`，界面不回读、不显示、不入日志）。
- **第二层（可选）：本地判定模型提示层。** 正则放行的 `run_shell` 命令会再过一次本地小模型（DecisionBert int8，跑在独立子进程里，空闲后自动收回内存），它给的只是「要不要问用户」的提示概率，不是硬闸门。模型文件约 97.7 MiB **不进仓库**，要自己投放到 `python/models/decision_bert/`（投放说明与模型卡见 `docs/decision-bert-model.md`）；文件没投放、依赖缺失或推理超时都会**静默降级为纯正则**，不影响 agent 可用性。
- agent 以**你当前用户的权限**运行：没有容器 / 沙箱隔离，也没有权限分级与工作区概念。第一次运行前请想清楚这一点。
- 普通操作（任意目录读写、跑普通 shell、联网）不逐次弹审批；只有 `guard.py` 判为危险模式的命令才会在会话里出审批卡片，且没有「总是允许」。

## 给别人的机器部署

上面「快速开始」是给自己开发用的（要 Node、要 npm install、要手敲命令）。把 CodeAgent 装到**另一台 Windows 机器**——哪怕它什么都没装过（没有 Python、没有 Node）——用安装脚本。

**推荐流程：先体检，再安装**（v3 起）：

```powershell
# 第 1 步：环境体检（dry-run，只读、绝不写任何文件、退出码 0）
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -CheckOnly
```

它也会打印一张对齐的体检表（OK/MISSING/SKIP 三态标记）：Windows 版本/架构、是否管理员、目标卷与临时目录的可用磁盘空间、PATH 上的 python/python3/py 启动器（含 WindowsApps 商店占位符识别）、node/npm、tar、`<InstallDir>` 下已有的 runtime\python / runtime\node / python\.venv / CodeAgent.bat / `.install-state.json`、数据目录位置（`-DataDir` 或 Electron 默认 `%APPDATA%\CodeAgent`）、是否有 CodeAgent/electron 进程在跑、源码树与目标目录里的 junction/符号链接扫描、源码树体积与磁盘占用预估、三个镜像（Python/Node/pip）逐个可达性与延迟（`-ProbeTimeoutSec` 可调，默认 10 秒）。表后面跟结论段：哪些必装、哪些可跳过、预计下载量（MB 级估算）、预计磁盘占用、需要用户先解决什么（磁盘不够 / 镜像不通换源或用离线归档 / 关掉在跑的进程 / 清掉目标目录里的陈旧链接）。

看完报告、处理完红旗之后，再跑真安装（安装开头会自动再跑一遍同样的体检并打印报告）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

不带参数时它会先问「Install CodeAgent into which directory?」（回车 = `%LOCALAPPDATA%\CodeAgent`），然后自动：拷文件（含源机现成的 `node_modules`，运行期不再需要 Node）→ 找 Python（v8 全面探测，顺序：`-PythonPath` → 安装目录下 `runtime\python\python.exe` → `python\.venv` → PATH 上的 python/python3 → **注册表三处**（HKCU/HKLM/WOW6432Node 的 `Software\Python\PythonCore\*\InstallPath`）→ py 启动器 `py -0p` 全量清单 → **常见位置**（每个固定盘符的 `miniconda3|anaconda3` 根、`%LOCALAPPDATA%\Programs\Python\Python3*`、`%ProgramFiles%\Python3*`、`C:\Python3*`）；每个候选 exe 都会实跑一次 `import sys` 采信版本，WindowsApps 商店占位符和跑不动的 exe 都会被过滤；**conda base 环境默认跳过**（共享环境，不想把 venv 绑死在上面；要直接用 conda base 建 venv 加 `-AllowCondaBase`，或用 `-PythonPath` 显式指定任意环境的 python）；选中的那个 ≥ 3.10 即直接用、不再下载）→ **找不到才自动下载并静默安装官方 Python 3.12.10（版本写死，不做 latest 动态解析）到 `<安装目录>\runtime\python`**：主源清华 `mirrors.tuna.tsinghua.edu.cn/python/3.12.10/`，备源阿里云 `mirrors.aliyun.com/python-release/windows/`（两源 2026-10-10 实测均 200 / application/octet-stream / 26,964,224 字节）。要无视机器上已有的 Python、强制用安装器自带的固定版本，加 `-ForceOwnRuntime`。下载先过三重校验（Content-Type / 体积 26.5~27.5MB / MZ 魔数），再验 **Authenticode 签名**——镜像目录没有同源 SHA256SUMS，官方安装器由 Python Software Foundation 签发，签名校验就是完整性保证；未签名/签名人不符直接拒装。安装参数 `/quiet InstallAllUsers=0 TargetDir=<安装目录>\runtime\python PrependPath=0 Include_launcher=0 AssociateFiles=0 Shortcuts=0 Include_test=0 Include_pip=1`（per-user 私有、不写系统 PATH、不动 .py 关联、不建快捷方式、无需管理员）。装完强校验：`runtime\python.exe` 存在 + 能跑 `import sys` 且报 3.12.10 + `python -m pip --version` 可用，三者任一不满足即报错。两镜像都失败才硬报错，给出 `-PythonArchive` 本地归档 / 手动装后 `-PythonPath` 的可执行指引。`-UsePbs` 可选切回 python-build-standalone 路线（npmmirror 日期目录 + 强制 sha256）；USTC 路线已整条删除（实测其归档 URL 返回 HTML 目录页）→ 建 venv（**顺手在 `python\.venv\pip.ini` 写死清华 index-url + trusted-host**，以后任何人在这个 venv 里手动 `pip install` 也走清华源）→ 装依赖（命令行显式 `-i` 清华源，失败自动回退官方 PyPI）→ 自检 → **可选装一套私有 Node v22.20.0（版本同样写死）到 `runtime\node`**：npmmirror 源，下载体积带 34~37MB 区间校验 + zip 魔数 + **SHASUMS256.txt 同源 sha256 硬校验**；装不上只警告不失败，因为应用本身不依赖 Node，`-NoNode` 可跳过 → 生成 `CodeAgent.bat` 启动器 + 开始菜单/桌面快捷方式。

镜像源拿不到 sha256 清单时安装器**默认直接拒绝**（不装未经校验的二进制）；确要放行需显式加 `-AllowUnverified`，输出里会打大字警告。

安装全程**不需要管理员权限**：所有运行时都落在 `<InstallDir>\runtime` 下，不写系统 PATH、不写注册表。启动器全部用 `%~dp0` 相对路径，整个目录搬走也能跑。

常用参数：

```powershell
# 指定安装目录 / 数据目录 / Python 位置
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -InstallDir "D:\My App" -DataDir D:\CodeAgentData -PythonPath C:\Python312\python.exe
# 版本已钉死（Python 3.12.10 / Node v22.20.0）；-PythonVersion/-NodeVersion 仅作显式覆盖用，日常不用传
# 跳过 Node 安装（应用运行期不需要 Node）
powershell ... -NoNode
# 切回 python-build-standalone 路线（可选，默认走官方安装器）
powershell ... -UsePbs
# 无视机器上已有的 Python，强制用安装器自带的 3.12.10（conda base 之类全部忽略）
# ⚠️ 若本机已登记过 3.12.10（官方安装器装的），-ForceOwnRuntime 会直接硬报错——
#    官方安装器装不了第二份同版本。此时改用 -UsePbs 便携版，或 -PythonVersion 3.12.9
powershell ... -ForceOwnRuntime

# 完全断网（真离线）：本地归档 + 预下载 wheel 一条命令装完
#   准备：从有网机器拷来 Python 归档、Node zip 和 wheel 目录
#     Python 归档：https://registry.npmmirror.com/-/binary/python-build-standalone/
#                  进最新日期目录（如 20261009/），选 cpython-3.12.*-x86_64-pc-windows-msvc-install_only.tar.gz
#                  注意：列表里文件名中段被打了码（202****1009），真实文件名要把 **** 换成日期目录名
#                  （或直接给官方安装器 exe python-3.12.10-amd64.exe，装到 runtime\python 下也行）
#     Node zip：   https://registry.npmmirror.com/-/binary/node/v22.20.0/node-v22.20.0-win-x64.zip
#     wheel 目录： 在有网机器上 python -m pip download -r python\requirements.txt -r python\requirements-docs.txt -d C:\wheelhouse
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Offline -PythonArchive C:\Downloads\cpython-3.12.15+202****1009-x86_64-pc-windows-msvc-install_only.tar.gz -NodeArchive C:\Downloads\node-v22.20.0-win-x64.zip -WheelDir C:\wheelhouse
#   -Offline 时会跳过一切联网下载；Python 归档必给（或目标机 PATH 上有 Python），Node 归档可省

# 卸载（快捷方式 + 启动器 + 安装目录 + 本脚本装的官方 Python 的完整 WiX 登记：ARP 卸载项 + Installer\Dependencies；数据默认保留，加 -PurgeData 才删）
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
```

### 三种典型用法（v7 起，`-PackageDir` 落地包目录）

安装目录、包目录、数据目录**都可以单独指定**；路径里的空格和中文字符没有问题（脚本内部全部走 `-LiteralPath` 语义）。

① **本机默认装（一条命令）**——包落在 `<InstallDir>\packages`，自动缓存，重装免下载：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

什么都不用传。首次装：下载 Python 3.12.10 官方安装器（约 27MB）与 Node v22.20.0 zip（约 36MB）落到 packages 目录（随后照样走全部校验再安装）；重装/升级同一台机：启动时先扫包目录，缓存命中且通过本地校验（魔数/体积/签名/sha256）就直接用，一个字节都不再下。

② **指定三个目录装**——程序、包缓存、数据各归各位：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 `
  -InstallDir "D:\我的 程序\CodeAgent" `
  -PackageDir "E:\共享 包\runtime-cache" `
  -DataDir    "D:\我的 数据\CodeAgent"
```

`-PackageDir` 指到只读介质（光盘/写保护目录）也行：能读的包照用，缺的包只有在目录可写时才补下载，写不进去也不报错（警告后转临时目录）。`-WheelDir` 默认取 `<PackageDir>\wheels`，显式传参优先。

③ **先预拷包、再断网装（-Offline 部署到无外网机器）**——把"下载"和"安装"拆成两步：

```powershell
# 有网机器上（或只跑一次下载）：正常装一遍，包目录会存下全部下载物
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -InstallDir C:\tmp-ca -PackageDir "D:\我的 包"

# 把整个包目录拷到 U 盘，连同仓库一起带到目标机，然后断网装：
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 `
  -InstallDir "D:\CodeAgent" -DataDir "D:\ca-data" `
  -PackageDir "E:\U盘\我的 包" -Offline
```

`-Offline` 跳过一切联网下载；Python 优先吃包目录里缓存好的官方安装器 exe（Authenticode 签名校验照跑），Node 吃缓存的 zip + sha256 sidecar。先用 `-CheckOnly` 看一眼"包目录里已有/缺哪些包"，缺什么补什么再正式装。

**断网机器怎么装（一条命令装完）**：把三样东西从有网机器拷过去，然后一条命令：

1. Python 安装包（任选其一）：
   - 官方安装器 `python-3.12.10-amd64.exe`（约 27MB，python.org 或任意镜像下载），或
   - python-build-standalone 便携包 `cpython-3.12.10+...-install_only.tar.gz`（注意上方的文件名打码说明）；
2. Node zip（可选，应用运行不需要）：`node-v22.20.0-win-x64.zip`（连同下载页的 `SHASUMS256.txt` 一起拷，校验用）；
3. wheel 目录：在有网机器上 `python -m pip download -r python\requirements.txt -r python\requirements-docs.txt -d C:\wheelhouse`（约 70MB，29 个 wheel）。

两种写法等价，任选：

```powershell
# 写法一：都拷进包目录，参数最省
#   （把 python exe、node zip、SHASUMS256.txt 拷进 E:\U盘\我的 包\，
#    wheel 解出来的文件拷进 E:\U盘\我的 包\wheels\）
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 `
  -InstallDir "D:\CodeAgent" -PackageDir "E:\U盘\我的 包" -Offline

# 写法二：显式传三个路径
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 `
  -InstallDir "D:\CodeAgent" -Offline `
  -PythonArchive "C:\Downloads\python-3.12.10-amd64.exe" `
  -NodeArchive    "C:\Downloads\node-v22.20.0-win-x64.zip" `
  -WheelDir       "C:\wheelhouse"
```

解释器来源顺序（断网时按序找，都没有才报错）：`<InstallDir>\runtime\python`（已铺）→ 现成 venv → PATH/注册表里能跑的 Python → `-PythonArchive`（本地包，走完整校验链：体积区间 + magic 字节 + exe 的 Authenticode 签名 / tar.gz 的 gzip magic + sha256 sidecar）→ 包目录里缓存的官方安装器 exe（同样校验签名）。venv 依赖用 wheel 目录离线装（`pip --no-index --find-links`），装完做 import 自检。本地包校验不过 = 当没有，自动落到下一个来源，最后报错会列出每个来源试到哪一步。`-CheckOnly` 在 `-Offline` 下的结论段只写"本地包/无下载"，不再出现"需下载 177MB"之类与行为不符的字样。

**路 B：手动装（脚本出问题时兜底）**

就是「快速开始（Windows）」那五步：`npm install` → `bootstrap-venv.ps1` → `build-ui.ps1` → 填 `.env` → `npm start`。要求目标机有 Node ≥ 20 和 Python ≥ 3.10。

**装完后东西都在哪 / 怎么改：**

- 程序目录：你指定的安装目录（默认 `%LOCALAPPDATA%\CodeAgent`），程序本体、venv、`CodeAgent.bat` 都在里面。
- 私有运行时：`<InstallDir>\runtime\python`（官方安装器静默装的 Python 3.12.10）与 `<InstallDir>\runtime\node`（可选 Node v22.20.0）。不占系统 PATH；官方 Python 装完会登记 per-user 卸载项（ARP + WiX `Installer\Dependencies`），`-Uninstall` **会调用官方安装器本身把这套登记一并清掉**——归属判定只看 `PythonCore\<tag>\InstallPath` 是否指向 `<InstallDir>` 内（官方 per-user 安装器不写 ARP 的 `InstallLocation`，所以该字段不能作为归属依据）；判定为 ours 时官方安装器链会把 PythonCore + ARP + WiX Dependencies 一并清干净，用户自己装的其它 Python 打印 "NOT ours" 后原样保留（此时残留计数不为 0 属正常，不算失败）。实测已知坑：**文件已被删的悬空登记直接 `/uninstall` 会 1603**，脚本会自动先 `/repair /quiet` 把文件补回一致、再 `/uninstall /quiet` 清干净（两步日志都落在 PackageDir）；`/repair` 可能把 `<InstallDir>\runtime` 重新铺出来，脚本会再删一次。安装器 exe 优先用 PackageDir 缓存，没有则走清华→阿里云下载，校验链与安装时完全一致（Content-Type / 体积带 / MZ 魔数 / Authenticode PSF 签名）。**注意官方安装器是"每版本单实例"：同一台机器装不了第二份 3.12.10**——机器上已登记过 3.12.10 时安装器会直接复用它建 venv（不再下载）；登记悬空（文件被删）时会明确报错并给出修复指引，绝不静默跳过。真要一份完全私有的副本：用 `-UsePbs` 便携版（不登记注册表），或换一个未被占用的补丁版本（如 `-PythonVersion 3.12.9`）。安装器运行时全程带 `/log <PackageDir>\python-installer.log`，失败时自动把日志里的 `WixBundleInstalled`、Detect 阶段各包状态、Exit code 三类行打出来，方便自查。**conda base 策略**：探测发现 conda 根目录的 python 会默认跳过（共享环境，不把 venv 绑死在上面）；如要直接用 conda base 建 venv，加 `-AllowCondaBase`，或用 `-PythonPath` 显式指定任意环境里的 python。
- venv 的 pip 源：`python\.venv\pip.ini` 里写死了清华 index-url + trusted-host，在这个 venv 里任何手动 `pip install` 也走清华源。
- 数据目录（会话库、断点、用量）：默认 `%APPDATA%\CodeAgent`（Electron 的 userData）。想挪走用 `-DataDir`——它给启动器加 `--user-data-dir` 参数；⚠️ 这个开关对本应用是否生效**待 Windows 实测**（开发机是 Linux，没法验证），没验证前别把 `-DataDir` 当迁移数据的可靠手段。
- 换源：pip 用 `-IndexUrl`；Python 归档镜像用 `-PbsMirror`；Node 用 `-NodeMirror`（只作用于当次安装，不改机器配置）。
- 脚本可重复跑：已完成的步骤（文件拷贝、venv、已存在的 runtime）会识别并跳过，不会炸。
- 拷贝阶段对路径链接有四重防御（v4，源自一次真机事故）：npm workspace 会在 `ui\node_modules\codeagent` 生成一个指回仓库根的 junction，robocopy 默认会跟进它，把仓库递归拷进自己肚子里（实测 1.1GB 源树被拷成 6.6GB）。防御① robocopy 全程带 `/XJ`（永不穿越 junction/symlink）；② 已知自链接目录再显式 `/XD` 排除做双保险；③ `python\.venv` 按名字排除（venv 绑定源机器的解释器路径，跨机是垃圾）；④ 拷贝完成后做理智断言——目标树的字节数/文件数超过源树（同口径排除后）1.5 倍即硬报错停机，宁可装不上也不吃光磁盘。这类链接问题在 Linux 开发机上无法复现，所以全部写成脚本内运行时断言，不依赖"应该没事"。

**脚本验证状态（诚实口径）：**v5 快照已在真实机器验收通过（2026-10-10，净化 PATH 的裸机：官方安装器清华源装成功、版本钉死一致、三份 requirements 装齐、自检 5 包全 OK、Node v22.20.0 sha256 通过、启动器 PATH 前置仅进程内、`--user-data-dir` 同行、端到端 schtasks 拉起 userData 落指定目录、落地 1313MB）。v6（Authenticode 校验、固定版本、venv pip.ini、PBS 降为 `-UsePbs`、卸载清注册表项）在 v5 基础上改动、待真机复验。v7（`-PackageDir` 包缓存与离线复用、Phase 0 路径/包清点、全脚本 `-LiteralPath` 语义）**未在真 Windows 机器上跑过**：在 PowerShell 7.4.6（Linux）上做了真实执行冒烟——`-CheckOnly` 带空格 + 中文路径跑通（三路径报告、包清点表、缓存命中判 OK / 坏包判"NOT usable, will re-download"、缓存命中时对应镜像行转 SKIP、严格零写入退出码 0），并从脚本 AST 提取 v7 下载/复用函数做了带真下载的复用验证（详见任务记录）；Windows 侧（Get-AuthenticodeSignature、安装器 /quiet、真实只读介质）待真机验证。v8（解释器全面探测：注册表三处 / `py -0p` / 常见位置+全盘符 conda 扫描、候选实跑验证、conda base 默认跳过、`-ForceOwnRuntime`、磁盘剩余对不存在路径也按盘符取、`-Uninstall` 清 PythonCore 注册表项）**未在真 Windows 机器上跑过**：在 PowerShell 7.4.6（Linux）上做了真实执行冒烟——`-CheckOnly` 三场景（PATH 有 python 判 NO DOWNLOAD / 净化 PATH 判官方安装器 / `-ForceOwnRuntime` 判强制自带版）、AST 提取的探测/决策函数 8 组单测全过（版本判定、conda/stub 匹配、显式优先、坏 exe 过滤、ForceOwnRuntime 三分支、不可运行候选跳过后 PATH 接管）；Windows 侧（注册表枚举、`py -0p`、conda 盘符扫描、PythonCore 清理）待真机验证。v10（同版本登记预检：官方安装器每版本单实例，已登记→复用，悬空登记→硬报错并给修复指引；`-ForceOwnRuntime` 撞同版本登记→硬报错；安装器全程 `/log`，失败自动打出 `WixBundleInstalled` / Detect 状态 / Exit code）**未在真 Windows 机器上跑过**：在 PowerShell 7.4.6（Linux）上做了模拟注册表冒烟（Get-RegisteredPythonInstall 三源判 found/miss、Test-PinnedPythonRegistration 四分支 clear/reuse/broken/conflict、日志三行族提取）；Windows 侧（真实 HKCU/HKLM 枚举、`/log` 参数行为）待真机验证。v11（-Uninstall 清官方安装器 WiX 登记：归属判定→调官方安装器 `/uninstall`、失败先 `/repair` 再 `/uninstall`→三处残留复查全 0 否则退出码 1；`-AllowCondaBase` 开关；conda skip 提示带上开关名）**未在真 Windows 机器上跑过**：在 PowerShell 7.4.6（Linux）上用模拟注册表 + 伪安装器 stub 做了真实执行冒烟——归属判定 6 场景（含"NOT ours, left untouched"、悬空 InstallPath、其它版本忽略）、残留计数三面、`/uninstall`→`/uninstall,/repair,/uninstall`→失败三条调用序列、包目录缓存命中（伪 MZ 文件过体积+魔数+stub 签名）全部断言通过；Windows 侧（真实安装器 `/uninstall`/`/repair` 行为、真实注册表清理）待真机验证。PS 5.1 ParseFile 零错误，UTF-8 with BOM，注释与输出全英文。v12（真机事故修复：v11 的注册表读取把带 provider 前缀的路径（`$tag.PSPath`，形如 `Registry::HKEY_...`）传给了 `Get-ItemProperty -LiteralPath`，真机 Windows PowerShell 5.1 的 `-LiteralPath` 不接受这种形式，参数绑定直接报错，导致 -Uninstall 的登记评估中止、三处残留全在）——修复为所有经 `$x.PSPath` 的注册表读取一律 `-Path`（同机上人工用同形式验证可用；ARP 读取一直就是 -Path 且真机正常），文件系统的 -LiteralPath 用法不变；自检新增静态闸门：脚本里任何 `-LiteralPath` 实参含 `.PSPath` / `::` / `HKEY_` 即硬失败，pwsh 7 模拟注册表的 harness 之前跑不出这类 PS 5.1 绑定行为，现在提交前就会被拦下。66/66 自检通过；修复本身待 the field box 真机复验。v13（第二次真机事故修复：v12 在 the field box 真机 PS 5.1 上 `-Uninstall` 仍报"无法将参数绑定到参数 LiteralPath，因为该参数为空字符串"——两处根因：① 真机上空字符串传 `-LiteralPath` 直接抛绑定错误（pwsh 7 只返回 False，Linux harness 复现不了）；裸 `-Uninstall` 时 `$PackageDir` 还是空串，因为参数默认化代码在 `exit` 之后只服务安装路径；② 归属判定依赖 ARP `InstallLocation`，而官方 per-user 安装器根本不写这个字段，导致 8 条 ARP + 8 条 WiX Dependencies 全被判 "NOT ours" 永不清理）——修复：卸载路径提前默认 `$PackageDir`、新增 `Test-PathSafe`（判空后再 `Test-Path -LiteralPath`）；归属规则重做为只看 `PythonCore\<tag>\InstallPath` 是否落在 `<InstallDir>` 内，ours 时由官方安装器 `/uninstall`（1603 时先 `/repair`）连带清掉三处登记；所有注册表读取收敛到 `Get-RegistrySubKeys`/`Get-RegistryKeyValues` 两个 helper（普通盘符路径 + `-Path`，全脚本零 `$x.PSPath`）；静态闸门 G1v13-G5v13（无 PSPath、PackageDir 提前默认、InstallLocation 不参与归属、helper 存在、卸载登记块无裸 `Test-Path -LiteralPath $var`）。75/75 自检通过（pwsh 7.4.6）；真机复验待 the field box 重跑。v14（第三次真机事故修复：v13 归属判定方向对了但比较错了——the field box 真机抓到的注册表原文显示官方安装器把路径写在**带尾反斜杠的 (default) 值**里（`D:\t\runtime\python\`），具名 `InstallPath` 值是空的；v13 拿裸字符串去比对，永不命中，自家登记被判成别人的（ARP/WiX 永不清理），Phase 0 还会把健康的登记误报成 BROKEN）——修复：新增 `Get-PythonCoreInstallPath`（三处注册表扫描统一从这一个函数读 InstallPath 值，(default) 与具名值都读、取非空的、Trim 后返回）；新增 `Convert-PathForCompare`（Trim、去尾分隔符、统一分隔符、Windows 上 `GetFullPath`）+ 重写 `Test-PathUnder`（两侧归一化后做大小写不敏感的"前缀+分隔符"判定，相等也算 ours）；三处残留为 0 否则退出码 1 的闸门、/repair 再清理、v12/v13 静态闸门全部保留；新增 G1v14-G5v14 静态闸门（代码里 (default) 读取仅剩一处、三处扫描必须走 Get-PythonCoreInstallPath、Test-PathUnder 必须经 Convert-PathForCompare 归一化）。100/100 自检通过（pwsh 7.4.6，含 the field box 原始注册表形状的 R6/U2 复现场景与"健康登记不再误报 BROKEN"的 F1/F2 复用场景）；真机复验待 the field box 重跑。v15（静态闸门固化「单元素数组被解包 + 字符串拼接」事故类：Get-RegistrySubKeys 返回数组在 0/1 个元素时被 PowerShell 解包成标量/无输出，调用点 `$x += Helper` 退化为字符串拼接、路径被拼成一坨导致自家登记被误判——v14 已修好 `return ,$out`，v15 把这一类收口成闸门）——改动：Get-PythonCandidates 的裸 `return $out` 同步加逗号（唯一同类残留，其余 `+=` 均为字面量/hashtable 属性追加或已套 @()）；自检新增两道静态闸门：G1v15 用 AST 遍历全部函数，任何「函数体内以 @() 初始化或 += 构造数组、且裸 return 该变量」命中即失败（hashtable 返回排除）；G2v15 正则扫全部 `+= <函数调用>` 调用点，未套 @() 命中即失败；并从 install.ps1 AST 提取生产版 Get-RegistrySubKeys 真跑 0/1/2 个子键场景（含 A3 回归断言：两次单元素 `+=` 累加必须得 3 条独立条目、不得字符串融合；闸门逻辑已用故意写坏的样本验证会命中）。112/112 自检通过（pwsh 7.4.6）；真机复验待 the field box 重跑。v1 核心链路（拷文件、建 venv、清华源装依赖、自检、启动器 `--user-data-dir`、单实例锁）已在真实中文 Windows 10 / PowerShell 5.1 上实测通过；v2~v4 各节见仓库历史说明。

## License

MIT，见 [LICENSE](LICENSE)。

## 致谢

本项目的设计与部分实现参照并改编自 [Hermes Agent](https://github.com/NousResearch/hermes-agent)
（Nous Research，MIT License）：能力层、skills、子代理、命令模式审批、联网搜索分层、思考预算等处的
具体出处逐条列在 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)，上游许可证全文也随该文件一并保留。
