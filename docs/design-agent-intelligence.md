# CodeAgent 智能化/自愈 + MCP + 程序更新 + 日志 + Skills 总体设计

状态：设计稿（本轮只出文档，不改代码）
日期：2026-10-11
作者：coder（任务 t_93a115b4）

## 0. 目标与现状（先核实，再设计）

目标：把 CodeAgent 从「能跑」升级为「出错能自己救自己、能挂外部工具、能自升级、出事能查」的桌面 agent。对标 Hermes Agent 的成熟做法，但按 CodeAgent 的体量（Python sidecar + Electron 壳，约 2 万行）裁剪，不照搬其全部机制。

现状（已逐文件核实，引用真实函数名）：

| 能力 | 现状 | 证据 |
|---|---|---|
| 自愈 | 只有**工具级**自愈：同签名失败预算 `TOOL_FAIL_STREAK_BUDGET=2`、工具总失败预算 `TOOL_FAIL_TOTAL_BUDGET=6`、冷却、`_tool_refusal_note()` 给模型纠偏提示（python/agent_loop.py）；网络错误指数退避 `NET_RETRY_MAX=3`；工具失败经验库 `tool_lessons.LessonStore`（python/tool_lessons.py，落 data/tool_lessons.json，经 `_lessons_block()` 注入系统提示词）。**没有**：进程级崩溃恢复、模型生成的修复补丁、沙箱验证、回滚 | agent_loop.py:36-44, 87-97；sidecar.py:327-359 |
| MCP | 完全没有 | 无任何 mcp 引用 |
| 程序更新 | 完全没有；version 0.1.0（package.json）；安装器 install.ps1 是手动脚本 | package.json:4 |
| 日志 | 仅 `log(level, msg)` 写 stderr（sidecar.py:93），无结构化字段、无轮转、无落盘保证；Electron 侧 stderr 由 main.ts 收进 sidecarClient 的输出管道 | sidecar.py:93-97 |
| skills | 读写闭环已齐：`skills_registry.py` 有 `scan/list_skills/get_skill/read_body/create_skill/patch_skill/apply_operations`（原子回滚）；工具侧有 `ReadSkillTool/ListSkillsTool/SkillManageTool`（tools.py:793/833/935）；内置 2 个 skill | skills_registry.py, tools.py |

已有但未利用好的资产（设计将复用，不重造）：
- `run_checkpoint.RunStore` + `_run_store()`：运行断点（data/runs/），已经区分 done/interrupted/failed
- `audit.AuditStore` + `_audit_store()`：审计库，已按 run 记录
- `_emit()` 事件总线（sidecar.py:300）：所有新机制对 UI 的通报走它
- `tools.register/get/execute/schemas/disabled` 注册表（tools.py:1155-1262）：MCP 工具回灌的挂点
- `permissions.judge()` + `_wait_for_approval()` 审批槽：MCP 工具权限的挂点

---

## 1. 智能化/自愈（最重要）

### 1.1 设计原则

三层递进，每层兜住上一层的失败：

- L0 规则自愈（已有，P0 补强）：不加模型调用，纯代码判定。零成本、零延迟、可单测。
- L1 检索自愈（P1）：出错时把 `tool_lessons` + 结构化日志里的历史结论喂给模型，让它在正常对话循环里自己避开坑。不加新循环，只加证据。
- L2 补丁自愈（P2）：模型生成修复建议/补丁 → 沙箱验证 → 应用或回滚。**这条最贵也最危险，明确放 P2 且默认关闭。**

红线（什么情况必须停下来问用户，任何层都不得越过）：
1. 需要写 workspace 之外的路径（补丁应用到 `workspace_root` 外）
2. 修复动作本身是高危工具（`RunShellTool` 里的删除/格式化类命令、`DeletePathTool` 非沙箱目标）
3. L2 要动 CodeAgent 自己的代码（python/*.py、electron/*.ts）—— self-patch 永远走「出 PR 式建议」而不是直接改
4. 同一 run 内 L2 已试过 1 次仍失败（`SELF_HEAL_BUDGET = 1`）—— 连续自愈失败必须升级为问用户，不许无限循环烧钱
5. 沙箱验证产生的任何成本外溢（新依赖安装等）

### 1.2 错误分类（L0 的输入）

新增 `python/error_taxonomy.py`，一个纯函数模块：

```
ErrorKind = Literal[
    "process_crash",     # sidecar/子进程退出码非 0 / 看门狗超时
    "tool_failure",      # ToolResult.ok=False（含 code）
    "model_anomaly",     # EVENT_ERROR：解析失败/超限/异常输出
    "dependency_missing" # ImportError/命令不存在（exit code 127/9009）
    "unknown",
]
class ErrorFacts:   # dataclass
    kind: ErrorKind
    source: str            # "sidecar"|"agent_loop"|"tool:<name>"|"electron"
    run_id: str | None
    session_id: str | None
    traceback: str | None
    recent_events: list    # 最近 N 条 _emit 事件（裁剪到 2KB）
    context: dict          # 工具名/参数摘要/错误码等
def classify(err: BaseException | dict) -> ErrorFacts
```

分类来源（全部是现成信号，不用新增埋点）：
- `agent_loop` 已 yield 的 `EVENT_ERROR` dict（code/httpStatus/retryable）
- `tools.execute()` 返回的 `ToolResult`（`_fail(code, message)`，tools.py:46）
- `skills_registry.SkillWriteError`、`ProtocolError/FrameError`（sidecar.py:113/117）
- Electron 侧 sidecarClient 收到的子进程 exit code

### 1.3 L0：规则自愈补强（P0）

只做两件事，都有明确判据：

a) **进程崩溃恢复（P0 核心）**
- sidecar.py `main()`（1897 行）外层包 `except BaseException`：先 `log("critical", traceback)` 落盘（见 §4 崩溃保证），再 `sys.exit(CRASH_EXIT_CODE=86)`。
- Electron `sidecarClient.ts` 已监听子进程退出；新增规则：非正常退出 → 自动重启 sidecar（指数退避，上限 3 次/5 分钟，超过则弹窗问用户而不是无限重启）。
- 重启后 sidecar 读 `RunStore`：`running` 状态的 run 判为 interrupted（现有机制 `run.pending`/`run.active` RPC 已具备），UI 给「继续」入口 —— 这条链路**已经存在**，P0 只需把崩溃时的 exit code 与重启计数接进去。
- 验收判据：kill -9 sidecar 子进程，5 秒内自动重启，未完成 run 显示「已中断，可继续」，且 data/logs/ 下有崩溃 traceback 记录。

b) **依赖缺失自愈提示（P0）**
- `error_taxonomy` 识别 `dependency_missing` 后，不自动安装（红线 5），而是 yield 一条结构化 `EVENT_NOTICE{code:"DEP_MISSING", fix_hint}`，UI 渲染成「缺 XXX，点此查看安装建议」。
- 验收判据：卸载一个 skill 用到的依赖后触发，模型回复里带出明确修复建议而非裸 traceback。

### 1.4 L1：检索式自愈（P1）

新模块 `python/self_heal.py`：

```
def heal_context(facts: ErrorFacts) -> dict | None:
    """命中历史经验则返回 {summary, matched_lessons, suggested_action}"""
```

- 数据源三处：`LessonStore`（`_key(tool,args)` 前缀匹配 + kind 匹配）、audit 库里同 kind 的历史结论、结构化日志 §4 的 `events` 索引。
- 命中后作为一条 system 风格提示注入下一轮 `_pre_step()`（挂 `context_mechanism` 现有降级管道旁边，不动消息存储）。
- 失败兜底：`heal_context` 全程 try/except 返回 None（与 `_lessons_block()` 同款防御，sidecar.py:315），自愈模块自己挂了绝不能影响主对话。
- 验收判据：人为制造一次 `read_file` 编码错误进 lessons；下次同错误发生时，模型第一次重试即改对参数，且审计里能看到 heal_context 命中记录。

### 1.5 L2：补丁式自愈（P2，默认关闭）

流程（对齐任务书：诊断→生成→沙箱→应用/回滚→沉淀）：

```
SelfHealLoop:
  1. classify() -> ErrorFacts
  2. 组诊断提示词（ErrorFacts + 相关文件内容，走 _attach_images 同款裁剪）
  3. 调模型（独立小调用，不占用主对话 max_loop）-> 产出 {diagnosis, patch?: {files: [{path, old, new}], commands?: []}}
  4. 沙箱：把 workspace 复制到 data/heal-sandbox/<run_id>/（浅拷贝+硬链接），
     应用 patch，跑用户指定的或推断的验证命令
  5. 通过 -> 经用户确认（红线 2 触发时）-> 应用到真 workspace；失败 -> 丢弃沙箱，报告 diff
  6. 结论写入 LessonStore（新 entry 类型 "heal_outcome"）供 L1 命中
```

- 回滚：应用前对每个被改文件做 `skills_registry._atomic_write` 同款临时文件替换，旧内容存 `data/heal-backup/<ts>/<path>`，一个 run 结束未再报错才清 backup。
- 配置开关：`capabilities.selfHeal.enabled: false`（默认关，见 §6 配置扩展——不改 config.example.json，本设计只定义 schema）。
- 验收判据：默认关闭时可验证的只有「开关存在且默认 false、开启后走完整流程并产出 lessons entry」。

---

## 2. MCP client

### 2.1 范围

只做 **client 侧**（挂外部 MCP server 用），不做 server 侧。传输支持 stdio 与 Streamable HTTP（SSE 按 2025 规范已并入 HTTP，旧 SSE-only server 通过 HTTP 回退支持）。

### 2.2 配置与生命周期

新文件 `config.mcp.json`（放用户数据目录，与 config.json 同级；不动 config.example.json）：

```json
{
  "servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/some/root"],
      "env": {},
      "disabled": false
    },
    "remote-x": {
      "transport": "http",
      "url": "https://.../mcp",
      "headers": {"Authorization": "Bearer ${MCP_TOKEN_X}"}
    }
  }
}
```

- `${ENV}` 占位符复用 `appconfig.read_env_file()`（appconfig.py:73）解析，凭证永不落盘。
- 起停：sidecar `main()` 启动时 lazy 拉起（首次用到才 spawn，`SIDECAR_CAPABILITIES` 加 `mcp.list/mcp.toggle`）；进程退出时在 `finally` 里向 stdio server 发 MCP shutdown 再 kill。stdio server 崩溃 → 由 §1.3a 的同一看门狗逻辑重启（共享计数）。
- 新模块 `python/mcp_client.py`：`class McpSession(transport, config) -> discover() -> tools: list[ToolSpec]`、`call(name, args)`。**不引入 mcp SDK 依赖**：MCP 消息层就是 JSON-RPC，自实现（约 300 行），与现有 `read_frame/write_frame` 帧协议风格一致，且避免 requirements.txt 增重 —— 这是体量裁剪的关键决策。若后续维护成本超预期再换官方 SDK（记入风险）。

### 2.3 工具回灌

新文件 `python/mcp_tools.py`：

```
def sync_registry(): 
    """对每个 enabled server: McpSession.discover() 得到 tool list，
    每个 MCP tool 包一个 McpProxyTool(Tool)：
      name = "mcp_<server>_<tool>"（前缀防撞现有 13 个工具名）
      run() -> tools._ok/_fail 包 McpSession.call 结果
    经 tools.register() 进现有注册表（tools.py:1155）"""
```

- 名称冲突：`tools.get(name)`（tools.py:1164）已按名取，前缀方案天然避开。
- 描述冲突（skills §5 同款问题）：MCP tool 描述直接来自 server，不进系统提示词的「能力与边界」节正文，只进 `tools.schemas()`；提示词能力节按实际注册动态生成，自动跟随。
- 超时：每个 MCP call 30s 默认（MCP server 挂死不能拖死主循环），可按 server 配置。
- 权限与审批：`McpProxyTool` 走现有 `permissions.judge()` + `_wait_for_approval()` 链路。默认策略：**所有 MCP 工具首次调用必须审批**（guard 里新增规则类 `mcp_default_ask`），用户可对单 server 设 `trustLevel: "ask"|"alwaysAllow"`。审批 UI 复用现有 approval 槽，payload 里标 `source: "mcp:<server>"`。

### 2.4 分期

- P0：config.mcp.json schema + `mcp_client.py`（stdio + http）+ `mcp_tools.py` 回灌 + 首次审批 + `mcp.list/mcp.toggle` RPC + UI 设置页一节「MCP 服务器」。
- P1：`trustLevel` 细化、call 超时配置、server 健康状态显示。
- P2：不做 server 侧、不做 MCP sampling/roots 等高级能力。

### 2.5 server 管理 RPC（t_d295723c，2026-10-12）

「技能与工具」面板新增 **MCP 页签**：列表（名称/transport/状态/restarts/serverInfo/工具数）、每 server 启停开关、表单新增/更新、删除、手动重载。引擎侧纯增量扩契约——`mcp.list` / `mcp.toggle` 的入参、返回结构、错误码一字不改，新增四个方法：

| 方法 | 入参 | 返回 |
|---|---|---|
| `mcp.describe` | 无 | `{servers: [{name, transport, enabled, ready, dead, restarts, serverInfo, command, url, toolCount}], skipped: [{name, reason}]}` |
| `mcp.upsert` | `{name, server: {...}}` | describe 同构载荷；校验失败回 `CONFIG_ERROR` |
| `mcp.remove` | `{name}` | describe 同构载荷；不存在回 `NOT_FOUND` |
| `mcp.reload` | 无 | describe 同构载荷（重同步：起停 server + 增删工具） |

要点：

- `describe` 读配置全集再与 REGISTRY 运行时状态按名字合并——`mcp.list` 只报有活跃会话的 server，停用项不出现，面板开关需要全集所以单独补视图。
- `upsert` 字段校验复用 `mcp_client.parse_server_config` + stdio/http 必填规则；凭证护栏：env/headers/url 值里出现明文密钥特征（sk-/ghp_/AKIA/Bearer xxx/JWT）且非 `${ENV}` 占位符 → `CONFIG_ERROR`，配置一字不动。占位符原样落盘，真实值只进 `.env`（`appconfig.read_env_file()` 运行时展开）。
- `remove` = 配置删除 + 重同步（`_teardown_server` 停进程、摘 `mcp_<server>_*` 工具）。
- 写回走与 `mcp.toggle` 同一套落盘语义（优先 `config.mcp.json`，主 config 已有 mcp 段则写主文件；临时文件 + `os.replace` 原子写）。
- UI 侧通道：`agent:mcpDescribe / mcpUpsert / mcpRemove / mcpReload / mcpToggle`（electron main 薄转发，preload `window.agent.mcp*`）。MCP 工具调用的人工审批护栏不受本改动影响。

验收判据（P0）：配一个 `@modelcontextprotocol/server-filesystem`，模型在对话里能列出并调用其工具，审计库有记录，关掉 server 后 schema 里工具消失。

---

## 3. 程序更新

### 3.1 更新源与版本检查

- 更新源可配：GitHub Releases（默认，`source: "github", repo: "owner/codeagent"`）或自建静态目录（`source: "static", baseUrl: "https://…/updates/"`，公司内网场景）。
- `update.check`：请求 `<source>/latest.json`：

```json
{"version": "0.2.0", "notes": "...", "files": {
  "win-x64": {"url": "...zip", "size": 12345678, "sha256": "…"},
  "linux-x64": {"...": "..."}}}
```

版本比较用 `packaging.version` 或自写 semver 比较（requirements 已有 python 生态）。

### 3.2 下载与校验（三重）

1. sha256 校验（zip 整包）—— 硬失败，删包报错
2. 体积上限（默认 500MB）—— 防 URL 被劫持到无限流
3. 摘要核对：notes 里的版本号与文件名/manifest 版本一致

下载到 `userData/updates/download-<version>.zip`，断点续传（Range 请求），失败可重试，不占安装目录。

### 3.3 原子替换与回滚

- 解压到 `userData/updates/staged-<version>/`，**staged 目录与安装目录分离**。
- 应用时机：用户确认后，退出 app（走现有 `before-quit` 确认链路，electron/main.ts:974）→ 由独立 updater 小脚本（不依赖 Electron）执行：把 `install-dir` rename 为 `install-dir.bak-<ts>`，staged rename 进去，成功则删 bak，失败则反向 rename 回滚 → 重启 app。
- 原子性：rename 在同一卷上原子；跨卷（用户自定义安装盘）退化为 copy+swap 并先做全量 bak。
- 回滚点：`install-dir.bak-<ts>` 保留至下次成功启动后 24h。

### 3.4 数据不变性

- 用户数据（会话库 `sessions.db`、`data/` 目录、config.json、config.mcp.json、skills 用户根 `~/.desktop-agent/skills`）**全部在 userData，不在安装目录** —— 现状即如此（`_DATA_DIR` 逻辑 sidecar.py:310）。设计补一条硬规则：安装包 manifest 里列出不允许写入的路径清单，updater 应用前校验 staged 目录不含对这些路径的写操作（zip 内路径黑名单：`data/`、`config*.json`、`sessions.db`、`skills/`）。
- migrations 兼容：`python/migrations/` 已存在；约定「更新后首次启动 sidecar 跑 migration，失败则告警并继续用旧 schema 只读降级」——不做自动降级安装（不做的优化，见 §7）。

### 3.5 离线包

- `update.applyLocal {path: "…zip"}` RPC：跳过下载，同样走 staged→校验→原子替换。与 install.ps1 的 `-Offline` 精神一致（该脚本 v16 已验证过本地源解析链路，经验写进了 lessons）。
- 路径：任意本地 zip/目录（目录则先打 zip 再走同一管道，保证校验逻辑单一）。

### 3.6 分期

- P0：`update.check` + 手动「检查更新」按钮 + 应用本地包（applyLocal）。
- P1：自动后台检查（默认开，下载默认关）、断点续传、GitHub/自建源切换 UI。
- P2：增量更新、签名校验（等有代码签名证书再说）。

验收判据（P0）：手工构造 latest.json + 本地 zip，`update.applyLocal` 后版本号变化、数据目录零变化、伪造 sha256 的包被拒并留有错误记录。

---

## 4. 日志

### 4.1 结构与落盘

改造 `sidecar.log()`（sidecar.py:93）为 `python/logging_kit.py`：

```
class LogRecord(TypedDict):
    ts: str          # ISO8601 带时区
    level: str       # debug|info|warn|error|critical
    source: str      # "sidecar"|"agent_loop"|"tool:<name>"|"mcp:<server>"|"heal"
    run_id: str|None
    session_id: str|None
    msg: str
    data: dict       # 可选结构化负载（截断 2KB）
def log(level, msg, *, source="sidecar", run_id=None, session_id=None, data=None)
```

- 落盘：`data/logs/sidecar-YYYYMMDD.jsonl`（JSONL，一行一条，grep/jq 友好），与 stderr 双写（stderr 保持现有格式，Electron 侧调试用）。
- 旧 `log()` 调用点全部兼容（多出的 kwargs 有默认值，签名向后兼容，零调用点改动）。

### 4.2 轮转与保留

- 按日切（文件名即日期）；保留 `log_retention_days: 14`（可配）；删除在启动时做，不占运行时。
- 单文件上限 20MB，超限截断旧的一半（保尾部——错误通常在尾部）。

### 4.3 级别配置

- `config.json` 的 `capabilities.logLevel: "info"`（默认）；`DESK_AGENT_LOG_LEVEL` 环境变量可临时覆盖（调试用，不落盘）。
- debug 级别才记每轮消息摘要（隐私折中：只记长度/工具名/角色，不记全文）。

### 4.4 崩溃时的 traceback 保证

- `agent_loop._run()`（agent_loop.py:387）的兜底 except 目前把异常转成 EVENT_ERROR 就丢了 traceback —— 改为先 `log("error", …, data={"traceback": tb})` 落盘再 yield。
- sidecar `main()` 顶层（§1.3a）与线程入口处：`sys.excepthook` + `threading.excepthook` 双保险，critical 级别写 `data/logs/crash-<ts>.log`（纯文本，含完整 traceback + 最近 50 条日志行引用）。
- 落盘失败兜底：写 `data/logs/` 失败时降级写系统临时目录，并在 stderr 打一条指路信息。**「日志写不进去」本身绝不能抛**。

### 4.5 与自愈的接口

`self_heal.heal_context()` 按次序检索：崩溃日志（process_crash）→ 当日 jsonl 里 `source` 相同的最近 error 级记录（`_tail` 读文件尾 200 行，不整载内存）→ LessonStore。§1.4 的验收即依赖此索引。

分期：P0 全量（结构化 + 轮转 + 崩溃保证 + heal 检索接口占位），P1 完成 heal_context 真实实现，P2 UI 查看器（设置页「日志」入口，按 run_id/session_id 过滤）。

---

## 5. Skills

现状已强（读写闭环 + 原子性 + 内置保护，见 §0）。补三块：

### 5.1 校验与 lint（P0）

- 已有：`parse_skill_md()`（frontmatter 解析）、`_validate_name()`、`_validate_frontmatter()`、rel_path 逃逸检查。
- 补：`skills_registry.lint(name) -> list[LintIssue]`，规则：
  a) description 非空且 ≤ 1024 字符，**开头必须是自包含触发句**（首句含动词/场景词，检查「以句号/问号结尾的第一句是否含有 when/使用/触发 类语义」——先用启发式正则，中文英文各一套）
  b) frontmatter 必填 name（与目录同名，已有）+ description；建议 version/author
  c) body 非空且 ≤ 64KB（提示词注入成本上限）
  d) 引用的 rel_path 文件真实存在（`_resolve_rel_path` 反查）
- lint 结果进 `skills.list` 返回（`issues: []`），UI 标黄不阻断。
- 验收判据：对现有 2 个内置 skill 跑 lint 全绿；构造一个空 description 的 skill 能被标红。

### 5.2 索引注入与「模型自己选」（P0，微调现有机制）

现状：`_system_prompt_for()`（sidecar.py:1976）已注入 skill 索引。补强：
- 索引格式固定为「name + description 首 57 字符」（对齐 Hermes 约定，与 docs/SKILL-AUTHORING.md 现有口径统一），超长截断。
- **模型自选**：索引里每条带一行「需要全文时调用 read_skill(name)」——现有 `ReadSkillTool` 即全文读取入口，不加新机制。这是「渐进披露」：正文不进系统提示词，按需取。
- 每次对话开始时索引里只放「内置 + 本 workspace 项目 skills」（`_project_skills_of(session_id)` 已存在，sidecar.py:760）——按项目过滤已经做了，P0 只补首 57 字符截断统一。

### 5.3 与工具描述的冲突避免

规则（写死，不留两套口径）：
- skill 描述进**系统提示词**（一次性、静态、按需展开）；工具描述进**每次请求的 tools schema**（`tools.schemas()`）。
- skill 的 `whenToUse` 明确写「当用户要 X 时用本 skill **而不是** Y 工具」这类排他句；lint 规则 a) 的触发句检查倒逼作者写清场景。
- 新工具注册（含 MCP，§2.3）不进任何 skill 正文；`SkillManageTool` 的 create 校验加一条：body 里出现 `mcp_` 前缀工具名时给 warning（提示这可能是工具职责不是 skill 职责）。

分期：P0 全部三条（lint 是纯函数模块，验收快）；P1 `skills.search {query}` RPC（FTS 或子串匹配）供 UI 与模型搜索；P2 skill 使用统计（usage ledger，对齐 Hermes 但非必需）。

---

## 6. 配置 schema 汇总（只定义，不改 config.example.json）

新增段（全部带默认值，旧 config.json 无这些段也能跑）：

```
"logging":   {"level": "info", "retentionDays": 14}
"selfHeal":  {"enabled": false, "budget": 1, "sandboxDir": "<data>/heal-sandbox"}
"mcp":       {"configPath": "<userData>/config.mcp.json", "callTimeoutSeconds": 30}
"update":    {"source": "github", "repo": "", "autoCheck": true, "autoDownload": false}
"skills":    {"indexTruncate": 57}
```

向后兼容规则：读取用 `appconfig.section(name, default)`（appconfig.py:50）—— 现有函数即支持缺省，零新机制。

---

## 7. 风险与明确不做的事

风险：
1. `mcp_client.py` 自实现 JSON-RPC 层：MCP 规范仍在演化，自实现可能踩 spec 更新。缓解：只实现 initialize/tools-list/tools-call 三方法，面窄；若踩坑换官方 SDK 是局部替换。
2. L2 自愈的最大风险是**模型生成错误补丁毁 workspace**：沙箱 + 用户确认 + bak 备份三重兜底，且默认关闭。
3. 更新器独立脚本：Windows 下文件占用（Electron 退出不完全）可能导致 rename 失败。缓解：脚本重试 3 次 + 任务管理器提示；v16 安装器的 lessons 里有同类经验。
4. 日志 jsonl 增大磁盘占用：14 天 × 20MB 上限 ≈ 280MB 最坏情况，对桌面机可接受；超限时截旧保新。
5. skills lint 的触发句启发式可能误报：只标黄不阻断（§5.1 已述）。

明确不做：
- 不做 MCP server 侧（CodeAgent 不对外供工具）
- 不做自动依赖安装（红线 5，只提示）
- 不做更新失败后的自动降级安装（人工处理，报告诊断）
- 不做 skill 使用统计 ledger（P2 之后再议）
- 不做跨设备同步/云端更新（本地优先原则）
- 不改 config.example.json、不 commit、不动现有 13 个工具的注册顺序

## 8. 总分期表

| 期 | 内容 | 验收锚点 |
|---|---|---|
| P0 | 日志结构化+轮转+崩溃保证；进程崩溃自动恢复+依赖缺失提示；MCP client(stdio/http)+回灌+审批；update.check+applyLocal；skills lint+索引截断统一 | §1.3a/b、§2.4、§3.6、§5.1 判据 |
| P1 | L1 检索自愈 heal_context；MCP trustLevel/健康状态；自动检查+断点续传；skills.search | §1.4 判据 |
| P2 | L2 补丁自愈（默认关）；日志 UI 查看器；skills usage 统计 | §1.5 判据 |

依赖关系：日志(P0) 先行，L1/L2 与崩溃恢复都吃它的索引；MCP 与更新互相独立可并行。
