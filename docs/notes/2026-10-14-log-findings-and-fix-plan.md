# 2026-10-14 日志取证 + 修复计划（CodeAgent）

昨晚（10-14 03:10–03:34）用户侧日志：`<安装目录>\data\logs\agent-2026....log`（source=`python-sidecar`）。
下面每条都标了日志行，不靠印象。

## 先纠正一个前提

**这份日志里一条超时都没有**——没有 `timeout` / `timed out` / `NETWORK` 字样，侧车侧全程健康
（`rpc models.fetch` 0.6–1.0s 全 ok，工具照跑）。用户在界面看到的
`[错误] NETWORK: TimeoutError: The read operation timed out` 是 **模型流式读取**那条链报的
（`python/llm.py` 的 `_default_opener(req, cfg.timeout_seconds)` → code=NETWORK），
它的原文落在 **主进程日志**：`%APPDATA%\CodeAgent\logs`（侧车自己的日志在 `<数据目录>\logs`，
两处不是同一份）。→ 下次取证要拿的是主进程那份；界面「设置 → 日志」那块已经能把两个目录都列出来。

## 从日志里确证的问题（按证据强度排）

### 1. 同一条断点被 resume 两次（「继续之后还一直提示」的真凶之一）

```
03:18:07.899  resume: r-20261014031730-4194 -> session s-1791918610849 (49 msgs)
03:18:16.378  resume: r-20261014031730-4194 -> session s-1791918610849 (49 msgs)   ← 同一 run，9 秒后第二次
03:19:01.761  resume: r-20261014031807-0176 -> session s-1791918610849 (50 msgs)
03:19:10.755  resume: r-20261014031807-0176 -> session s-1791918610849 (50 msgs)   ← 又一次
```
同一 run_id 在秒级内被续了两遍 = 界面/后端没有「这条断点已经有人在续」的闸。
一晚上 9 次 resume（03:14:18 / 03:14:36 / 03:18:07 / 03:18:16 / 03:19:01 / 03:19:10 /
03:20:34 / 03:28:48 / 03:30:38），跑完的轮次也还留着可续标记。

### 2. 跑完的轮次没被收尾 → 下次启动被扫成「可续」

配合上面第 1 条：`sidecar.py` chat 那轮的 `finally`（约 3302–3352）只做落库/标题/附件，
**没有把本轮 run 记录收尾**；旧断点只在 `chat.done` 那条路径上收（约 3290）。
于是正常结束的轮次留在 `running`，下次开程序 `run_checkpoint.sweep_on_start` 把它扫成
`interrupted`（可续），banner 就回来了 —— 哪怕上一轮其实跑完了。

### 3. 模型层（guard）启动即降级

```
03:10:21.477  warn  model layer degraded (unavailable): stale ready line: response stream misaligned; child discarded
```
子进程握手把「陈旧 ready 行」吃掉了 → guard 模型层判为不可用。
（同一 session 里后面又出现 `guard` 的 p 值，说明后面起了新的子进程，但启动这次是废的。）

### 4. 记忆子进程全程没起来（整晚静默丢数据）

```
memory: memory write skipped: memory subprocess is not up        ← 出现 ~20 次
memory: memory prefetch skipped: memory subprocess is not up
```
03:11 → 03:34 每一轮都如此。也就是说这一晚的 memory 写入/预取**一条都没落**。

### 5. 自动自愈对真模型无效

```
03:15:06  heal: diagnosis unusable (输出不是合法 JSON 对象), retrying with JSON-only reminder
03:15:06  heal: model gave no actionable patch:
03:15:06  auto heal tool_failure/TOO_LARGE -> failed
03:23:23  auto heal tool_failure/NONZERO_EXIT -> failed
```
两次触发、两次失败（真实模型不按 JSON 契约回）。**桩模型全通 ≠ 真模型能用**。

### 6. guard 弹卡噪音来源（用户「安全提示太烦」）

一晚 8 次拦截，p 值：0.663 / 0.953 / 0.929 / 0.951 / 0.917 / 0.738 / 0.817 / 0.700。
其中 **5 次落在「不确定带 0.35–0.85」**，策略是「安全优先，已拦截待确认」——
即：模型不太确定时也拦。噪音主要来自这条口径，而不是 ≥0.85 那几次。

### 7. 长工具调用（不是超时，但观感像卡死）

`run_shell` 耗时：22694ms / 6524ms / 4858ms / 80051ms(!) / 20337ms。
这些都 `ok` 收尾了，但 80 秒那次期间界面完全没反馈 → 和用户说的「像这样我就只能等着」是同一体验问题。

### 8. 其它

- `chat persist: tool call <uuid>: no message to link, skipping row`（~10 次）：工具调用行落库时找不到挂载消息。
- 侧车**是**写自己 JSONL 的（`log dir: <安装目录>\data\logs` 且内容齐）→ 之前「测试机上源码树侧车不写日志」那条可以结案：那是测试机上那份源码树/产物旧，不是产品缺陷。

## 真机验证（2026-10-14 凌晨，测试机隔离实例 + 停机桩模型）

靶机：一台独立 Windows 测试机（headless 起在非交互会话），用独立 `--user-data-dir` +
独立 data 目录，桩模型 `D:\ca_stub.py`（127.0.0.1:8896，按 state.json 决定停多久/回什么）。
用户主用机与其安装目录全程未动。

### 1. 超时（用户报「特别容易超时」）—— 真根因是顺序错，不是窗口

A/B 两臂，同一个场景（桩收到含标记的请求就停 400s，客户端读超时 150s）：

| 臂 | 代码 | 桩收到的请求 | 界面 |
|---|---|---|---|
| A | 修复前 | **只有 1 次**，之后没有任何重试请求 | `[错误] NETWORK: TimeoutError: timed out` + 「上次任务未完成 … 网络/接口异常 · 第 1 轮，进度已保存」+ 继续/忽略 |
| B | 修复后 | 第 1 次停 → 超时 → **自动重试** → 第 2 次拿到回复 | 「网络中断，2 秒后第 1 次重试」→「…第 2 次重试」→ 正文正常输出，该条消息无报错 |

A 臂与用户截图逐字一致。**注意**：A 臂之后我把 `NET_RETRY_WINDOW` 从 120 改到 300，
真机重跑 B 臂**毫无变化、仍然只发 1 次请求** —— 因为整段重试逻辑位于
`governor.on_request_error()` 的 `return` 之后，而 governor 对任何非溢出错误都回
`{"action":"propagate"}`，那段代码是死代码。修法：把「瞬时故障」判定提到 governor 之前。

### 2. Windows 上 `os.kill(pid, 0)` 不是探活 —— 强杀一次后应用永久起不来

真机取证（`D:\ca_pidprobe.py`）：

```
os.kill(dead_pid, 0) -> 没有抛异常（老代码因此把死进程判成"活着"）
新探针: dead -> False, self -> True, 99999999 -> False
```

现场：陈锁导致侧车连续 3 次 `sidecar duplicate: data-dir lock held by pid=8212`，
主进程 `SIDECAR_DEAD: max restarts exhausted`，界面「crash」。
修复后真机复验：故意造一个真死 pid 的陈锁 → 侧车正常接管（锁 pid 变 14348）、应用起来、
CDP 就绪、日志无 duplicate。

### 3. config.json / .env 的 UTF-8 BOM

真机现场：PS `Set-Content -Encoding UTF8` 写的 config.json 带 BOM →
`不是合法 JSON: Unexpected UTF-8 BOM` → 模型配置整套失效、界面「未配置」。修复后真机日志
`ocr fallback wired: {...}` 正常，不再有 BOM 警告。单测红绿：HEAD 4 失败（报错即真机那句），
修复后 10 过。

### 4. 审批记忆（会话内允许 / 一直允许）真机验证

- 危险命令卡片上四个按钮俱在：拒绝 / **本会话内允许** / **一直允许** / 允许，档位显示「递归删除」
- 点「本会话内允许」→ 卡片变「已允许（本次会话内不再问）」→ 桩连续返回同一危险命令，
  **连跑 4 次都没再弹卡片**（最后被既有的同参失败上限拦下收尾）
- 点「一直允许」→ 卡片变「已允许（以后不再问同一操作）」→ `config.json` 落盘：
  `capabilities.approvalAllow = [{"fp":"cmd:rm -rf ./dist","tool":"run_shell","preview":"rm -rf ./dist","at":"2026-10-14T05:05:14"}]`

### 5. 这次踩到的验证方法坑（下次别再犯）

- **整树部署**：只推 4 个 .py 会得到「新 llm.py + 旧 net_kit.py」的混合构建，真机上表现为
  重试时报 `TypeError: '_Response' object is not iterable` —— 那是 11bcf85 之前的旧
  net_kit（预读响应体、不可迭代），不是产品现存 bug。现在改为整棵 python 源码树一起推，
  7 个关键文件哈希逐个核对。
- PS 5.1 写 JSON 一律用 `[IO.File]::WriteAllText(..., UTF8Encoding($false))`，否则自带 BOM
  会同时坑产品和桩。
- 杀自己实例的进程要按 `ExecutablePath -like '<实例目录>\*'` 并排除桩，别用命令行匹配
  （会把桩一起杀掉，于是「超时测试」变成「连不上模型」）。
- `winps.py` 自带 180s 子进程超时，分钟级实验要在靶机上 WMI 后台起、隔一会儿收文件。

## 真根因：「特别容易超时」其实是连接上限漏进了读超时（2026-10-14 真机定位）

`python/net_kit.py` 里 `CONNECT_TIMEOUT_SECONDS = 5.0` 是**逐地址连接**上限，`connect_prefer_ipv4`
把它 `sock.settimeout(per_try)` 设在 socket 上；连接成功后这个 socket 又被
`_PinnedPlainConnection` / `_PinnedTLSConnection` 直接复用，而 `_exchange` 从来没把超时
改回请求口径 —— 于是「5 秒」一路带到后面的读写：**模型 5 秒没吐出第一个字节就被判成读超时**，
配置里的 `timeoutSeconds`（180/150）在这条路上根本不生效。

真机取证：请求 09:51:26 发出、09:51:31 报 `TimeoutError`，正好 5 秒；对应界面
`[错误] NETWORK: TimeoutError: timed out`（用户截图原文）。

修法（最小）：`_exchange` 在发请求前 `sock.settimeout(float(timeout) or HTTP_TIMEOUT_SECONDS)`
—— 连接仍按 cap 封顶，读出网响应等多久由调用方决定。
红绿：`tests/test_net_read_timeout.py`（真 socket + 本地停机服务器；连接上限压到 0.4s、
服务器停 1.2s、请求口径 5s）—— HEAD 上 0.4 秒就超时（红），修好后等到响应（绿）。

真机复验（桩停 60 秒不吐字）：+20 秒界面仍是「生成中」、**全程零超时零重试**，60 秒后
拿到模型回复；修复前同一场景 5 秒即报超时并弹「网络中断，2 秒后重试」。
**也就是说：断网重试只是把这个问题盖住了，真身是这里。**

## 其他四项的真机验证（2026-10-14 下午）

| 项 | 真机证据 |
|---|---|
| mermaid 刷屏 + 光标被拽回 | `errorBlocks: 0`（不再有 Syntax error 炸弹块）、`scrollChanges: 1`（滚动位置全程没动）、`activeElements: ['TEXTAREA.composer__input']`（光标一直留在输入框）；图表正常渲染 |
| 图片不能直接输出 | 相对路径 `generated/gen.png` 与绝对路径 `probe.png` 都渲染成功（`imgLoaded: 2 / imgFailed: 0`）。两处根因：工具卡把产物媒体渲染在「展开详情」分支里（用户看不到）；匹配器 `imageRefsIn` 只认绝对路径而解析器 `resolveImageSrc` 支持相对路径（口径不一致）。顺带修掉本轮自己引入的回归：相对路径规则咬了绝对路径的尾巴，图片标记被插进路径中间 |
| 长任务时不能发消息 | 忙时发第二条 → 界面「已排队 1 条：本轮结束后自动发送」→ 桩日志显示第二条只在第一轮结束后发出（10:02:33，晚于 Q1 请求 60 秒整）；停止键与发送键并存 |
| 断点续跑 | 跑完的轮次现在收尾（真机 run 记录 `status=done`）；`pending_for_session` 在有 running 后继时返回 None、且已被接手的断点不再算待续（单测 5 条，HEAD 上 2 挂） |

## 已修（2026-10-14 夜，工作区未提交）

### 超时：重试窗口比一次读超时还短 → 「一次超时 = 立刻中断」

根因（代码确证）：`net_retry_started` 在**本轮开始**就打上（`agent_loop.py:576` 旧代码
`net_retry_started = time.monotonic()`），而窗口 `NET_RETRY_WINDOW = 120.0`（旧值）；
单次读超时 `cfg.timeout_seconds = 180`（`llm.py:46`）。于是超时那一刻
`elapsed(≥180) < 窗口(120)` 不成立 → **重试被跳过**，而提示词/注释里写着「断短自动重试
2-3 次」——对超时类故障从来没生效过。用户点一次「继续」才走得下去，正是「特别容易超时」。

改法（两处，最小改动）：
- `python/agent_loop.py:113` `NET_RETRY_WINDOW = 120.0 → 300.0`（窗口必须容得下一次完整超时）
- `python/agent_loop.py:576` `net_retry_started = None`；`:672-679` 窗口从**第一次可重试错误**起算

红绿证据（真输出，同一份用例）：
```
修复后： tests/test_agent_loop_resume.py ... 20 passed in 14.08s
退回 HEAD（修复前）：
  AssertionError: 1 != 2 : 一次慢超时之后必须还有一次自动重试
  1 failed, 19 deselected
```
用例：`tests/test_agent_loop_resume.py::NetRetryTest::test_slow_timeout_is_still_retried`
（窗口压到 0.05s、第 1 次尝试先耗 0.1s 再报 NETWORK，模拟「慢的那次调用」）

### 审批记忆：`tests/test_agent_loop.py` 三处断言按新口径更新 + 新增 2 条

- `test_dangerous_command_asks_with_tier`：危险档现在 `canAlwaysAllow/canSessionAllow` 均为 true
- `test_hardline_command_asks_with_tier`：hardline 两个都 false（不可逆永不记忆）
- 新增 `test_remembered_approval_skips_the_card`（点头后同一操作不再弹卡、且照样真执行）
- 新增 `test_hardline_is_never_remembered`（hardline 一个字都不许记）

全量：`pytest tests python/tests` → **1503 passed, 3 skipped, 0 failed**（51.8s）

## 修复计划（按用户钦定顺序）

1. **超时**：先拿主进程日志（`%APPDATA%\CodeAgent\logs`）定位真报错点；改三处 —— 读超时口径（现在 180s 一个数兜整轮，慢首字/长任务吃亏）、重试次数与退避（窗口太短，公司网一抖废一轮）、超时当下保住已吐出的正文（别再让用户点「继续」才看得到全）。
2. **resume 反复**：① `finally` 里按真实结局收尾 run 记录（成功 finish、出错才 interrupt）；② 同 run_id 单一在飞闸（UI + sidecar 双侧幂等，第二次数秒内的重复续跑直接忽略）。
3. **安全提示噪音**：审批记忆（本会话内允许 / 一直允许）代码已落（`python/approval_memory.py` + agent_loop/sidecar/UI），待补测试；另议「不确定带 0.35–0.85 是否必须拦」。
4. **长任务期间不能发消息**：`ui/src/components/chat/Composer.tsx:127/181` 按 `isStreaming` 闸死 → 改成能打字 + 排队/插话。
5. **mermaid 刷屏 + 光标被拽回**：`MermaidBlock.tsx:65` 渲染 id 用 `Date.now()`（每次重画新节点、旧的不清）、`initialize`（:20）没开 `suppressErrorRendering`、无 hash 缓存 → 稳定 key + 围栏闭合后再渲染 + hash 去重 + 失败清残留 + 用户上滚时暂停自动滚底。
6. **图片不能直接输出**：机制在（附件卡片 + `resolveImageSrc` + `[[file: 路径]]`），是**提示词/技能没教模型**它能内嵌；待确认 `MarkdownView` 对 `![](路径)` 是否走 resolver。
7. **guard 子进程握手**（`stale ready line`）：按 skill `child-process-protocol-debugging` 的口径修 ready 行消费。
8. **memory 子进程起不来**：整晚静默丢写，优先级不低（用户以为记住了、其实没记）。
9. **自愈对真模型无效**：JSON 契约要兜（解析容错 / 结构化输出），否则这条能力等于没有。

## 当前工作区状态（2026-10-14 夜）

- 已推送：日志入口（`21f3229`，`agent:logDescribe` / `agent:logOpen` + 失败原因块）。
- **未提交**（在树里）：审批记忆 —— 新增 `python/approval_memory.py`；`python/agent_loop.py`（`allow_session` 决策、裁决前查记忆、hardline 不记忆）；`python/audit.py`（DECISIONS 加 `allow_session`）；`python/sidecar.py`（`_approval_memory_for` + `_always_allow_list`/`_persist_always_allow` 落 `capabilities.approvalAllow` + `approval.list`/`approval.forget` RPC + deps 接线）；`ui/src/types.ts`、`ui/src/state/store.tsx`（`approveApproval(id, scope)`）、`ui/src/components/chat/ApprovalCard.tsx`（本会话内允许 / 一直允许）。
- 验证过：`py_compile` 四个文件 OK；`ui tsc -b --force` = 0；`pytest`（permissions / run_checkpoint / resume_flow / agent_loop_resume / python/tests）**157 passed, 1 skipped**。
- **没做**：新模块的单测、根 `npm test` 全量复跑、真机验证、提交。