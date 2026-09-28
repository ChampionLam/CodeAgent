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
| `tests/` | 73 个测试文件（65 个 Python unittest + 8 个 Node 原生 test） |
| `scripts/` | 冒烟脚本 |

## 快速开始

依赖：**Node ≥ 20**（`npm test` 建议 **Node ≥ 22**，见下）、**Python ≥ 3.10**（sidecar 的图片 / 文档 / 纯文本读取要装依赖，见第 2 步）。

```bash
# 1) 应用依赖
npm install

# 2) sidecar 依赖（图片 + 文档提取 + 纯文本 + OCR 兜底）—— 一条命令装齐
#    Windows：
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-venv.ps1
#    Linux / macOS：
bash scripts/bootstrap-venv.sh
#    脚本会建 python/.venv，并把 requirements.txt 与 requirements-docs.txt 一起装上，
#    末尾自检并列出 Pillow / anydoc / pymupdf / pypdf / openpyxl / python-docx 的装没装。
#    意义：PDF / Word / Excel / PPT / CSV 这些是**功能自带**能力，部署时就该可用，
#    不该等到用户拖进来才发现库没装。

# 3) 构建界面（产物要落到 electron/ui/，应用从那里加载）
cd ui && npm install && npm run build && cd ..
rm -rf electron/ui && mkdir -p electron/ui && cp -r ui/dist/* electron/ui/

# 4) 配置：拷一份空白模板，再填你自己的模型与密钥
cp config.example.json config.json
#   · config.example.json 是**空白模板**（模型/端点都留空）——用谁家的模型由你决定，
#     启动后在界面「设置 → 添加模型」里选对接方式即可，也可以直接编辑 config.json。
#   · 密钥值只写 .env（变量名与 config.json 里的 apiKeyEnv 对应），别写进 config.json：
printf 'OPENAI_API_KEY=...\n' > .env

# 5) 跑起来
npm start
```

Windows（PowerShell）—— 第 1、2 步上面已给了两个平台各自的命令，第 3、4 步换成：

```powershell
# 3) 构建界面 + 落 electron/ui/，并顺带拷好 config.json 与空白 .env
powershell -ExecutionPolicy Bypass -File scripts\build-ui.ps1

# 4) 把 key 填进 .env（脚本已写好空白模板）
notepad .env
#    .env 必须是 UTF-8 **不带 BOM**：PowerShell 5.1 的 `>` 会写成 UTF-16，
#    应用读不出来。build-ui.ps1 用 [IO.File]::WriteAllText + UTF8Encoding($false) 写。

# 5) 跑起来
npm start
```

> PowerShell 下不用管通配符：`npm test` 里的 `tests/*.test.ts` 由 node 自己展开（`--test` 从 Node 21 起直接认 glob），实测 Node 22 上把通配符原样传入也能跑满 99 条；Node 20 上请用 POSIX shell 或升级 node。CI 目前跑在 Linux 上（Python 测试 / Node 测试 / typecheck / UI 构建，见 `.github/workflows/ci.yml`），Windows 侧还没进 CI。

测试：

```bash
npm test                                        # TS 侧
python3 -m unittest discover -s tests -t tests -p "test_*.py"   # Python 侧（1175 个用例）
```

## 配置说明

- `config.json`（**不进仓库**）：模型列表、默认模型、工具开关、工作区等用户设置。
- `.env`（**不进仓库**）：密钥值。`config.json` 里只写变量**名**（如 `OPENROUTER_API_KEY`），值从环境读。
- `config.example.json`：可以入库的示例骨架。
- 用户数据（会话库 `sessions.db`、断点 `runs/`、用量、工作区）都在 Electron 的 userData 目录下（`%APPDATA%\CodeAgent` / `~/Library/Application Support/CodeAgent` / `~/.config/CodeAgent`），**不在仓库里**。

## 安全边界与限制（请先读这一节）

- **命令审批是防手滑的 UX 护栏，不是安全边界。** `python/guard.py` 是黑名单正则（要求命令名落在命令位上），必然有绕过面：`/bin/rm -rf /`、`rm$IFS-rf$IFS/`、`X=rm; $X -rf /`、`xargs rm`、`find . -delete`、`python -c "import shutil; shutil.rmtree('/')"` 都不会被拦（这些是实测结论，不是推测）。它拦的是**手滑**，不是恶意。
- 真正的安全边界在**网络层**（`python/web/egress.py`：私网拒绝、DNS 钉死、跨站重定向重验、字节上限）与**密钥治理**（key 只走 `.env`，界面不回读、不显示、不入日志）。
- agent 以**你当前用户的权限**运行：没有容器 / 沙箱隔离，也没有权限分级与工作区概念。第一次运行前请想清楚这一点。
- 普通操作（任意目录读写、跑普通 shell、联网）不逐次弹审批；只有 `guard.py` 判为危险模式的命令才会在会话里出审批卡片，且没有「总是允许」。

## License

MIT，见 [LICENSE](LICENSE)。

## 致谢

本项目的设计与部分实现参照并改编自 [Hermes Agent](https://github.com/NousResearch/hermes-agent)
（Nous Research，MIT License）：能力层、skills、子代理、命令模式审批、联网搜索分层、思考预算等处的
具体出处逐条列在 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)，上游许可证全文也随该文件一并保留。
