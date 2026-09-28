# 多模型配置 —— 接口契约（前后端共同遵守）

适用范围：桌面 Agent（`<repo>`，真机环境）。
本文件是唯一契约来源。前后端各自实现，字段名/错误码不得私自改动。

## 0. 硬约束（安全）

1. **key 只从环境变量 / `.env` 读**：不进 config.json、不进日志、不进 RPC 响应、不进审计库。
2. 界面**只能知道某个模型"是否已配置 key"**（布尔），永远拿不到值。
3. 面板输入的 key 由 sidecar 写入 env 文件（一行 `NAME=value`），写后立即设置进当前进程
   `os.environ`，无需重启即生效。
4. 任何日志/异常消息里出现 key 的值都算 bug。
5. 写 config.json 必须**原子替换 + 保留其它段**（`capabilities` 等不能被覆盖掉）。

## 1. 配置文件结构（config.json）

新增顶层 `models` 段；**旧格式必须继续可用**：`models.items` 缺失时，用旧 `model` 段合成
一条（id 固定 `"legacy"`，label 用 `model.model` 的值），保证老部署不改配置就能跑。

```json
{
  "model": { "baseUrl": "...", "model": "...", "apiKeyEnv": "...", "maxTokens": 4096, "timeoutSeconds": 180 },
  "models": {
    "defaultId": "minimax-m3",
    "items": [
      {
        "id": "minimax-m3",
        "label": "MiniMax M3",
        "provider": "minimax",
        "baseUrl": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M3",
        "apiKeyEnv": "MINIMAX_CN_API_KEY",
        "maxTokens": 4096,
        "timeoutSeconds": 180,
        "inputModalities": ["text", "image"],
        "contextWindow": 1000000,
        "thinking": null,
        "thinkingDepth": null,
        "temperature": null,
        "topP": null
      }
    ]
  }
}
```

字段规则：
- `id`：URL 安全的 slug（`[a-z0-9._-]+`），后端强制校验；重复即拒绝。
- `label`：界面显示名，可中文。
- `provider`：见第 3 节模板 key，仅作归类/预填提示；**thinking 参数映射（第 6 节）按它选路**。
- `baseUrl`：OpenAI 兼容端点根（不含 `/chat/completions`）。
- `apiKeyEnv`：环境变量名，`[A-Z][A-Z0-9_]*`。
- `model`：模型名，自由文本（**不预置"主流模型清单"**，由用户填）。
- `inputModalities`：仅声明了 `image` 才允许图片输入（沿用 `appconfig.model_declares_image`）。

### 1.1 生成参数字段（v2 新增）

每条 `models.items[]` 增加以下四个字段；**旧配置缺失时一律按默认值加载，不许报错**：

| 字段 | 类型 | 默认 | 校验（越界/类型错 → `MODEL_INVALID`） | 语义 |
| --- | --- | --- | --- | --- |
| `contextWindow` | int | `128000` | `1000..10000000`，必须整数（bool 不算） | 该模型的上下文窗口 token 数，即上下文机制 v1 的「窗口」（第 7 节）。**估算值**，不是精确 token 计数。 |
| `thinkingDepth` | `string \| null` | `null` | 取值为**这条模型目录里的档位**（`depth.levels`；glm-5.3 = `low/high/max`，openai = `low/medium/high`）；目录里没有该模型时退回上游默认 `low/medium/high`；其它 → `MODEL_INVALID` | `null` = 不干预（请求体**不能出现任何**深度键）；非 null 时：厂商有深度参数（见第 6 节「深度」列）就写进请求体，没有就把**软引导**写进系统提示词，并在 `models.presets` 里以 `softThinkingDepth=true` 标明。`thinking=false` 时深度被忽略（请求体与提示词都不出现）。 |
| `thinking` | `true \| false \| null` | `null` | 只接受三态；其它类型 → `MODEL_INVALID` | `null` = 不干预（请求体**不能出现任何**思考相关键）；`true`/`false` = 强制开/关，按第 6 节映射表转成厂商参数；映射表标「未适配」的 provider **不传参数**并在日志/事件标注。 |
| `temperature` | number \| null | `null` | `null` 或 `0..2`（含边界）的 number | 采样温度。非 `null` 时进 payload 的 `temperature`；`null` 时**键不出现**。 |
| `topP` | number \| null | `null` | `null` 或 `0..1`（含边界）的 number | 核采样。非 `null` 时进 payload 的 `top_p`；`null` 时**键不出现**。 |
| `thinkingSupported` | bool | `true` | 布尔；非布尔 → `MODEL_INVALID` | 这条**线路**吃不吃思考参数。`false` = 一个思考键都不发（不管 `thinking` / `thinkingDepth` 怎么设），会话级覆盖会被丢弃并在 notes 里说明；界面据此不显示「思考强度」行（`thinkingAdapted=false`）。
**这是兜底开关，不是常规配置**：常规情形由**目录**逐模型判定（见 §6.0）。
历史备注：2026-09-25 一度用它把「百炼 MAAS 上的 glm-5.3」标成 `false`（理由是 `enable_thinking=false` → `400 restricted to True`）——**这是个误判**：那条模型不是没有旋钮，而是「旋钮叫 `reasoning_effort`、且思考不可关」。用户当场纠正（「怎么没有思考强度设置,肯定有啊」）。判线路能力前必须查目录里那条模型的 `thinking` / `depth`。 |

兼容规则：
- `models.upsert` 入参**缺字段 = 保留原值**（更新已有条目时不把原配置清空）；**显式传 `null` = 清空/回默认**。
- 旧的 config.json 没有这些字段 → 加载时填默认值，行为与 v1 完全一致（请求体里不带这些键）。

## 2. RPC（sidecar，沿用现有 JSON-RPC 外壳）

### `models.list` → `{ defaultId, items: [...] }`
每项含：`id,label,provider,baseUrl,model,apiKeyEnv,maxTokens,timeoutSeconds,inputModalities,contextWindow,thinking,thinkingDepth,temperature,topP,hasKey,effectiveMaxTokens,maxTokensNote`

其中 `effectiveMaxTokens` / `maxTokensNote` 是**只读派生值**（第 6.2 节）：前者 = 这次请求实际会发的输出预算
（开思考时可能高于配置的 `maxTokens`），后者 = 抬高时的一句说明，没抬高为 `null`。**不写回配置**。

`provider` 读出时会**自愈**：config 里写的是占位值（如旧格式合成的 `legacy`）或为空时，按 `baseUrl` 主机名
解析成第 3 节的 provider key（`api.minimaxi.com` → `minimax-cn`、`localhost`/`127.0.0.1` → `ollama-local` …），
否则第 6 节能映射、思考开关却静默失效。解析只改返回值，落盘发生在下次 `models.upsert`。
（`hasKey` = `os.environ[apiKeyEnv]` 非空；**不含任何 value**）。
另带 `activeId`（当前默认解析结果，见第 4 节）。
旧配置缺这四个字段时，返回值里**已填默认值**（`contextWindow=128000`，其余 `null`）。

### `models.presets` → `{ presets: [...] }`
主流对接方式模板，每项：`key,label,baseUrl,keyEnvHint,needsKey(bool),notes,thinkingSupported(bool),thinkingDepthLevels(string[]),softThinkingDepth(bool),thinkingAdapted(bool)`

每项还带 `thinkingCanDisable(bool)`。

`thinkingAdapted` = 这条模型/线路**能不能收发思考参数**（界面据此决定显不显示「思考强度」这一行）。
判据 = `llm.model_thinking_profile()` 里有可发的片段（`on`/`off` 至少一个非空）**且**条目没有声明
`thinkingSupported: false`。前端只读这一个布尔，不自己猜。

`thinkingDepthLevels` = 该模型**真有的**深度档位（空数组 = 没有；例：glm-5.3 是 `["low","high","max"]`）；
`thinkingCanDisable` = 能不能关思考（glm-5.3 为 `false`：厂商侧始终思考，界面不给「不思考」——发了就是 400）；
`softThinkingDepth` = 支持思考但无深度参数，前端给「软引导」下拉。
三者与 `thinkingAdapted` **同源**：后端 `llm.model_thinking_profile()`（目录优先，见 §6.0）→ `decorate_thinking()`，
前端不自己判断。
（例：openai / deepseek / minimax-cn / minimax-intl / dashscope-bailian / moonshot / zhipu / siliconflow / ollama-local / custom）。
模板**只填 baseUrl 与 key 变量名建议**，不给模型名。`thinkingSupported` 来自第 6 节映射表，
前端用它决定 thinking 控件是否显示「该厂商未适配思考开关」的灰字提示。

### `models.fetch` → `{ models: [...], count, baseUrl }`

拉取该厂商**真实可用**的模型名，供界面下拉选择（用户不该手打模型名）。

入参：`{ provider?, baseUrl?, apiKeyEnv?, apiKey? }`
- `baseUrl` 缺省时按 `provider` 从模板取（选好厂商就不用填地址）。
- `apiKey` 优先于环境变量：用户刚粘的 key 也能直接试，不必先保存；两者都没有就发无 key 请求（本地模型如 Ollama 需要）。
- 实现 = `GET {baseUrl}/models`（OpenAI 兼容），解析 `data[].id` / `models[].id` / 裸数组，去重排序。
- **key 只进 `Authorization` 头**：不回显、不落盘（用户没保存就不写 .env）、不进错误文案、不进日志。
- 响应上限 1 MiB；错误码：`MODEL_FETCH_UNAUTHORIZED`（401/403，key 问题）、`MODEL_FETCH_FAILED`（连不上/非 JSON/过大）、
  `MODEL_FETCH_EMPTY`（列表为空）、`MODEL_INVALID`（baseUrl 为空）。错误文案面向用户，不含堆栈。

界面口径：**基础字段只有四项**（对接方式、API Key、模型、显示名称）；选厂商自动带出 `baseUrl` + `contextWindow` +
环境变量名；模型字段 = 「获取模型列表」按钮 + 下拉；其余（地址、变量名、输出预算、窗口、超时、温度、top_p、
思考模式/深度、图片输入）全部收进默认**收起**的「高级设置」。

### `models.upsert` → `{ id }`
入参：`{ id?, label, provider?, baseUrl, model, apiKeyEnv, maxTokens?, timeoutSeconds?, inputModalities?, contextWindow?, thinking?, thinkingDepth?, temperature?, topP?, apiKey? }`

可选字段缺省时的语义：生成参数沿用已存值（不因漏传而清空）；`provider` 缺省时保留已存值，仍为空则按 `baseUrl` 解析后落盘。
- `id` 缺省时由 label/模型名生成 slug（冲突则拒绝，不自动加后缀）。
- 带 `apiKey`（非空字符串）时写 env 文件并注入 `os.environ[apiKeyEnv]`；**不回显**。
- 生成参数四字段：**缺 = 保留原值**（仅更新已有条目时），**显式 null = 清空/回默认**；新条目缺 = 默认值。
- 原子写回 config.json，保留其它段。
- 校验失败返回错误码 `MODEL_INVALID`（message 说明哪个字段）。

### `models.remove` → `{ removed: id }`
入参 `{ id }`。删最后一条 → `MODEL_LAST`；删当前默认 → 自动把默认换成剩余第一条并在响应里带 `newDefaultId`。

### `models.setDefault` → `{ defaultId }`
入参 `{ id }`，id 不存在 → `MODEL_NOT_FOUND`。

### `chat` 增加可选 `modelId`
- 传了就用它（会话级覆盖），没传用 `models.defaultId`。
- 该模型 `hasKey` 为假 → 错误码 `MODEL_KEY_MISSING`，message 里带环境变量名（**不带值**）。
- `modelId` 不存在 → `MODEL_NOT_FOUND`。
- 现有 `model` 参数保留兼容（等价于按模型名临时覆盖 baseUrl 之外的字段不变）。

## 3. env 文件定位

优先级：
1. 环境变量 `DESK_AGENT_ENV_FILE` 指定的文件；
2. 否则 `<repo root>/.env`。

启动脚本（`E:\desk-agent-deps\run-app.cmd`）会设 `DESK_AGENT_ENV_FILE=E:\desk-agent-deps\.env`，
即目标机上 key 仍集中在那一个文件里，**不做迁移**。写入时：保留原有其它行与注释，
同名的键就地更新（不追加重复行），无则追加到末尾。

## 4. 生效规则（全局默认 + 会话覆盖）

- 全局默认 = `config.json.models.defaultId`。
- 会话覆盖 = 界面把 `modelId` 随 `chat` 传进来；sidecar 不需要记住会话状态。
- 界面侧：会话没有覆盖时显示并跟随全局默认；设过覆盖的会话显示自己的模型。
- 每次 `models.list` 返回的 `activeId` 是**全局默认**，不随会话变。

## 5. 单测要求

新增测试必须覆盖：
1. 旧配置（只有 `model` 段）能合成 legacy 条目且 `models.list` 不报错。
2. `hasKey` 语义：设置了环境变量为真、未设置为假；**响应里不含 key 值**。
3. `upsert` 新增/改名；id 冲突拒绝；非法 `apiKeyEnv`（小写/带连字符）拒绝。
4. 删最后一条 → `MODEL_LAST`；删默认 → 默认自动顺延。
5. env 文件写入：更新已有键不产生重复行、保留注释、新增键追加；写入后 `os.environ` 立即生效。
6. config.json 其它段（`capabilities`）在 upsert/remove 后字节级保持（解析后等价）。
7. `chat` 的 `modelId` 解析：不存在 → `MODEL_NOT_FOUND`；无 key → `MODEL_KEY_MISSING`。
8. 生成参数四字段：旧配置缺字段加载填默认值；upsert 缺字段保留原值 / 显式 null 清空；越界与类型错 → `MODEL_INVALID`。
9. thinking 三态在请求体上的落点：null → 无任何思考键；已适配 provider 的 true/false → 对应厂商参数；未适配 provider → 不传键且事件/日志标注。
10. thinkingDepth 落点：**先看这条模型目录里有没有深度参数**（§6.0），有 → 请求体出对应键（glm-5.3 + 百炼 MAAS → `reasoning_effort: low|high|max`）；没有 → 请求体**一个深度键都不出**（走软引导），且 `thinking=false` 时既不传键也不引导；模型侧「不许关思考」（`thinking.off=null`）却被要求关 → 保持开启并退到最低档位（`reasoning_effort=low`，官方迁移提示同式），**绝不发 `enable_thinking=false`**。
11. provider 自愈：`provider="legacy"`（或空）配 MiniMax 主机名解析成 `minimax-cn`；已填合法 provider 时以它为准，不被主机名覆盖。
12. `models.upsert` 对 thinkingDepth 的校验：按**这条模型目录里的档位**校验（glm-5.3 的 `max` 必须放行——2026-09-25 前写死 low/medium/high，用户选「极致」会被判成「取值不认识」丢掉）；目录里查不到该模型才退回 `low/medium/high`；未传该字段时沿用已存值。
12b. 输出预算抬高（第 6.2 节）：`minimax-cn` + 思考非关 + `maxTokens=4096` → 请求体 `max_tokens` 是 16384、
   配置值仍 4096、`maxTokensNote` 非空；思考强制关 → 请求体仍是 4096 且无说明；`openai` 等未实测厂商 → 一律不动。
10. `temperature`/`topP` null → 键不出现；非 null → 键出现且取值正确。
11. 上下文机制 v1（第 7 节）可离线验证的验收条目：系统提示字节级不变、L0/L1 零 LLM 调用、
    状态不前进不重试、反抖动熔断不出第三次压缩、tool 配对与角色交替边界。

现有测试基线必须保持全绿（本机 socket 全禁跑法见仓库说明），不得为了新功能放宽既有断言。

## 6.0 能力来源：目录（模型级）优先，provider 表兜底

**思考能力是模型级事实，不是线路级。** 同一个模型换条线路、或者同一家厂商的新模型，
档位与「能不能关」都可能不同，所以发送侧按这个顺序取：

1. `model_catalog.json` 里**这条模型**的 `thinking` / `depth`（逐模型取证，带官方原文引用）——
   命中就用它，`thinking.on` / `thinking.off` 里存的请求体片段**逐字照发**；
2. 没命中才退回本节的 provider 表（经验默认值，保持旧行为）。

所以 `dashscope-bailian` 这一行只描述「百炼公有云上的 qwen 系」怎么发参数；**跑在百炼 MAAS 上的
glm-5.3 走的是第 1 条**：`reasoning_effort: low/high/max`、思考不可关（§6.1 表末行）。

反面教材（2026-09-25）：当时只看 provider 表，把 glm-5.3 判成「这条线路没有思考参数」，
界面把「思考强度」整行藏了。用户一看就说不对（「怎么没有思考强度设置,肯定有啊,你的检索有问题」）——
目录里那条模型早就写着三档 low/high/max 且 `off=null`，是发送侧没查目录。

## 6. thinking 参数映射表（provider → 请求体参数）

逐家以厂商文档核实（核实不了的标「未适配」，不猜）。`thinking=null` 时**任何一家都不传任何思考相关键**。
「未适配」的 provider：后端发请求时**不传**该参数、在审计/日志里标注未适配，前端在设置面板显示灰字
「该厂商未适配思考开关」。映射键 = 模型条目的 `provider` 字段（第 1 节）。

| provider | 参数名 | thinking=true | thinking=false | 依据 |
| --- | --- | --- | --- | --- |
| `minimax-cn` / `minimax-intl` | `thinking`（对象） | `{"type": "adaptive"}` | `{"type": "disabled"}` | MiniMax 官方 OpenAI 兼容文档「Thinking Control」：*「If `thinking` is omitted, thinking is on by default… Set `thinking: {"type": "adaptive"}` to explicitly keep thinking on… Set `thinking: {"type": "disabled"}` to skip thinking」*（platform.minimax.io/docs/api-reference/text-openai-api，MiniMax-M3 Request Parameters 表同式）。 |
| `dashscope-bailian` | `enable_thinking`（布尔） | `true` | `false` | 阿里云百炼「深度思考」文档：*「Use the enable_thinking parameter to switch between thinking and non-thinking on a per-request basis… Set to true — the model reasons before responding. Set to false — the model responds directly」*，OpenAI 兼容模式经 `extra_body` 透传为顶层键（help.aliyun.com/en/model-studio/deep-thinking）。注意文档同时说明 thinking-only 模型无法关闭。 |
| `deepseek` | `thinking`（对象） | `{"type": "enabled"}` | `{"type": "disabled"}` | DeepSeek 官方 Chat Completions API 参考：*「thinking object nullable — Controls the switch between thinking and non-thinking mode. type string, possible values [enabled, disabled], default enabled」*（api-docs.deepseek.com/api/create-chat-completion）。旧的 deepseek-chat/deepseek-reasoner 模型名区分法已被官方标记为废弃路线（对应 v4-flash 的非思考/思考模式）。 |
| `moonshot` | `thinking`（对象） | `{"type": "enabled"}` | `{"type": "disabled"}` | Kimi API Platform「Chat Completions API」K2.x 请求参数表：*「`thinking.type`：`"enabled" | "disabled"` — Thinking switch（kimi-k2.7-code is always enabled and cannot be disabled）」*（platform.kimi.ai/docs/api/chat）。注意 kimi-k3 用顶层 `reasoning_effort` 且思考常开，本轮不单独适配。 |
| `zhipu` | `thinking`（对象） | `{"type": "enabled"}` | `{"type": "disabled"}` | 智谱开放平台「深度思考」文档（docs.bigmodel.cn/cn/guide/capabilities/thinking）给出 `thinking = {"type": "enabled"}` 的调用式；GLM-5.3 文档明示 *「不再支持 thinking.type: 'disabled'… 如果您的应用当前使用 disabled，请在更新模型 ID 之前改为 enabled」*（docs.bigmodel.cn/cn/guide/models/text/glm-5.3）——即参数形状成立，但对「思考常开」的模型强制关会失败。按形状适配，关不掉的模型由服务端报错兜底。 |
| `siliconflow` | `enable_thinking`（布尔） | `true` | `false` | SiliconFlow Chat Completions API 参数表：*「enable_thinking: type boolean — Switches between thinking and non-thinking modes. Default is True」*，并列出支持的模型清单（docs.siliconflow.cn/en/api-reference/chat-completions/chat-completions）。仅对清单内模型有效。 |
| `openai` | `reasoning_effort`（字符串） | `"high"` | `"none"` | OpenAI Chat Completions 参考：*「reasoning_effort: Currently supported values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`」*，`none` 用于不做推理（developers.openai.com/api/reference，Reasoning guide）。注：非推理模型不接受该参数会报错，`thinking=null` 是对旧模型的唯一安全值；「开」映射为 `high` 是本项目的自定选择（OpenAI 无布尔开关，只有档位）。 |
| `ollama-local` | — | **未适配** | **未适配** | Ollama OpenAI 兼容层公开的 supported/unsupported 字段表里没有任何思考开关（thinking 控制 只存在于原生 `/api/chat`，不在 `/v1/chat/completions`）；硬塞会 400 或被静默忽略。核实不到兼容层的等价参数 → 按未适配处理。 |
| `custom` / 其它未列值 | — | **未适配** | **未适配** | 无法核实任意第三方 OpenAI 兼容端点是否接受哪个思考参数；宁可少传不可错传（400 比不干预更糟）。 |

### 6.1 思考深度（v3 新增）

**深度不是每家都有。** 有厂商级参数的才写请求体，没有的只能给「软引导」（一行系统提示词，
界面明确标成「软引导」，不承诺效果）。核实结论：

| provider | 深度是否可控 | 落法 | 依据 |
|---|---|---|---|
| `openai` | 可控（硬） | `reasoning_effort` = `low` / `medium` / `high`（本项目只暴露这三档；厂商还有 `minimal`/`xhigh`/`max`） | 同 §6 openai 行的官方枚举（developers.openai.com/api/reference）。 |
| `minimax-cn` / `minimax-intl` | **不可控** | 软引导 | 官方 OpenAI 兼容文档的参数表里 `thinking.type` 只有 `disabled` / `adaptive` 两个值，**没有任何深度/预算参数**（platform.minimax.io/docs/api-reference/text-openai-api）。实测复核（2026-09-24，MiniMax-M3）：`thinking={"type":"enabled","budget_tokens":256}` → HTTP 2013 `invalid thinking.type: "enabled" (allowed: adaptive, disabled)`；同参数重复跑，思考长度 143～11216 字（任务驱动，非档位驱动），说明长度不是可拧的旋钮。 |
| `dashscope-bailian` + `glm-5.3`（百炼 MAAS 也走这条） | 可控（硬，三档） | `reasoning_effort` = `low` / `high` / `max`（默认 `max`）；**思考不可关** | 目录 `model_catalog.json` 该条（status=exact）：智谱文档 *「GLM-5.3 会始终启用思考功能，支持三个思考强度级别：`low`、`high` 和 `max`，并不再支持禁用思考功能。」*、迁移提示 *「把 `thinking.type` 改成 `enabled`，并把 `reasoning_effort` 设置为 `low`」*（docs.bigmodel.cn/cn/guide/models/text/glm-5.3）。实测复核（2026-09-25，该模型的百炼 MAAS 终点）：`reasoning_effort` 取值不在 `low/high/max` → `400 'reasoning_effort' must be one of: 'low', 'high', 'max'`；`enable_thinking=false` → `400 The value of the enable_thinking parameter is restricted to True`；`thinking_budget=512/8192` 收下但不起作用（同题 reasoning_tokens 无规律）。 |
| `deepseek` / `moonshot` / `zhipu` / `siliconflow` / 其它 `dashscope-bailian` 模型 | 本轮**不暴露** | 软引导 | 这些家/这些模型本轮只核实了「开/关」参数（§6 各行），深度参数未经逐模型核实 → 按「核实不了就不传」处理，不猜。 |
| `ollama-local` / `custom` | 不暴露 | 无 | 与 §6 同：连开关都未适配。 |

**软引导**（`prompt_build.thinking_depth_section`）只在「用户设了深度 + 厂商没有硬参数 + 思考没被强制关」
时出现，内容是一行中文提示（轻/中/深三档措辞）。它是引导不是保证，所以界面文案必须写清。

### 6.2 输出预算：开思考时抬高 maxTokens（v3 新增）

**问题（实测，2026-09-24 MiniMax-M3，同一道需要推演的问题）**：

| 组 | 参数 | 结果 |
|---|---|---|
| A | `thinking={"type":"adaptive"}` + `max_tokens=4096` | `finish_reason=length`，正文 **0 字**，思考 10697 字，`completion_tokens=4096`（其中 `reasoning_tokens=4096`） |
| B | `thinking={"type":"disabled"}` + `max_tokens=4096` | `finish_reason=stop`，正文 1213 字 |

→ **思考与正文共用同一个输出预算**；预算被思考吃光时**不报错，返回空正文**（比报错更糟）。

**处置（参照 Hermes Agent，不自己发明）**：全局默认 cap 4096 保留（与其 `agent.max_tokens` 默认同值），
但 Hermes Agent 自身就带这个补丁（见其 `agent/transports/chat_completions.py`）：

> A global Hermes cap of 4096 is enough for visible text, but ... thinking can exhaust it on the first
> request ...

其 `_raise_gemini_thinking_max_tokens()` 在开思考时抬高 cap；`plugins/model-providers/meta-ai` 更直接，
`default_max_tokens=16384`，注释为「small caps can finish with empty content」。

本项目对齐：`llm.effective_max_tokens(provider, thinking, max_tokens)`
- 只在**实测过共享预算**的 provider 上生效：`minimax-cn` / `minimax-intl`；其余家**不动**（没实测就不猜）。
- `thinking is False`（强制关）→ 用用户的值（实测 4096 够用）。
- 否则 `max(max_tokens, 16384)`。
- **配置值不改**；界面显示 `effectiveMaxTokens`（实际发送值）+ `maxTokensNote`（抬高说明）。

### 6.3 输入区控制条：会话级覆盖（v3 新增）

界面上的**模型名 / 模型切换 / 思考开关 / 思考深度 / 上下文长度**全部放在输入框下方那一排
（`components/chat/ComposerControls.tsx`），顶栏不再显示模型名，会话标题也不再顶在顶部
（标题只留在左栏列表里）。这一排改的东西**不是模型配置**，而是**本会话的请求覆盖**：

| 控件 | 取值 | 语义 |
|---|---|---|
| 模型 | 模型 id | 本会话用哪个模型（沿用既有 `modelId` 参数） |
| 思考开关 | 跟随模型设置 / 强制开启 / 强制关闭 | 跟随 = 不传 `thinking` 键；强制 = 本轮覆盖 |
| 思考深度 | 跟随 / low / medium / high | 只有厂商真有深度旋钮时才进请求体，其余靠提示词软引导 |
| 上下文长度 | 跟随模型设置 / 65,536 / 131,072 / 262,144 / 1,000,000 | 改的是**本会话的压缩触发线**，不是模型窗口 |

**请求参数（chat 流）**：

```jsonc
{
  "sessionId": "s-…", "text": "…",
  "modelId": "m-…",
  "overrides": {          // 全缺省时整个键不出现，老路径请求体逐字节不变
    "thinking": false,    // true / false / null（null = 不干预）
    "thinkingDepth": "high",
    "contextWindow": 131072
  }
}
```

**实现口径（`llm.apply_turn_overrides`）**：

- 只改内存里这一轮的 cfg 副本，**不写回 `config.json`**，不影响别的会话
- 覆盖在上下文窗口校验**之前**应用，所以压缩触发线按覆盖后的窗口算
- 取值不认识时**不静默吞掉**：返回一句人话，经 `chat.notice` 事件在对话里显示
- 界面本地持久化（`localStorage: desk-turn-overrides`，按会话 id 存），重开还在

## 7. 上下文机制 v1（contextWindow 的语义）

依据：`concepts/desktop-agent-context-mechanism-v1.md`（2026-09-22 定稿方案，参数为硬数字）。
**库是唯一事实源、上下文是派生视图：本机制只做视图投影（状态位），不物理丢弃任何消息。**

### 7.1 触发与预算（硬数字）

- 预留（输出）= `min(16384, 窗口 × 25%)`。
- 实际触发点 = `min(窗口 × 80%, 窗口 − 预留)`。
- 保留尾部 = `窗口 × 16%`；单条工具结果配额 = `窗口 × 30%`；聚合工具结果配额 = `窗口 × 50%`。
- 保留最近 5 条工具结果不做 L0 替换。
- 加载期校验：`保留尾部 < 实际触发点` 不成立 → 拒绝启用（`MODEL_INVALID`），不带病运行。
- token 度量为**估算**（ASCII ≈ 4 字符/token、CJK ≈ 1 字符/token，故意高估宁早压），界面展示占用
  用服务端返回的真实 `usage.prompt_tokens`。

### 7.2 三级降级（顺序不能反）

1. **L0（0 次 LLM）**：旧工具输出（最近 5 条之外）换一行结构化摘要（工具名 + 关键入参 + 退出码 + 输出行数）
   + 相同结果去重。
2. **L1（0 次 LLM）**：大输出落盘，上下文里只留「路径 + 头尾预览」，模型按需读取；单条超 `窗口 × 30%`
   落盘，聚合超 `窗口 × 50%` 时从最老开始落盘直至回到配额内。
3. **L2（1 次 LLM）**：头保护 + 尾保护（16%）+ 中段摘要（摘要用主模型）。**摘要若不小于被遮内容则抛错不执行。**
   每次先算免费手段（L0/L1）能削减多少：够则 0 次 LLM 停下；不够才升级 L2。

### 7.3 五条硬边界（代码级校验）

1. 压缩不得触及系统提示 / 工具 schema / 会话前缀。
2. 摘要必须小于被遮内容，否则不执行。
3. 不得切断 tool_call 与结果的配对（孤儿 tool 消息必须连带处理，否则服务端 400）。
4. 保持 user/assistant 角色交替（同角色连排是多数 chat API 的隐性约定）。
5. 系统提示字节稳定：时间戳只到「天」精度。

### 7.4 状态机与挂载点

- 挂载点一 = 发送前 pre-step：常量 → 读占用 → 判定越线 → 免费削减 → 不够才 L2 → 重新度量，够了就停。
- 挂载点二 = 请求被拒后：识别超额（明确错误码/文本、通用 400+大会话、断连+大会话，识别要宽）→
  分流（输入超窗走压缩；仅输出上限超窗只降本次 max_tokens，**绝不缩窗口**）→ 仅服务端明示真限才缩窗口 →
  失败分级（鉴权/配额/网络/空内容/截断 = 中止类，原样保留消息）→ 压缩后重试（判据：必须产生可验证的
  状态变化）→ 耗尽重试 2 次进 TRIPPED，给强制可见警告 + 两个出口。
- 反抖动：连续两次压缩各省 < 10% → 熔断进 COOLING，不再做第三次压缩。
- 状态：`IDLE / FREE_SCAN / COMPACTING / OVERFLOW_RECOVERING / COOLING / TRIPPED`。
  v1 的计数/代数**只做内存级**（不落 SQLite；004 迁移与软归档落库为下一轮范围）。
- 重试纪律：压力压缩重试 1 次；超额恢复重试 2 次。

### 7.5 事件（chat 流内新增）

压缩/降级发生时向前端发：

```json
{"type":"evt","event":"context.compacted","data":{
  "id":"<chatRequestId>","level":"L0|L1|L2","state":"FREE_SCAN|COMPACTING|...",
  "estimatedTokens":<压缩前估算>,"tokensAfter":<压缩后估算>,
  "llmCalls":0|1,"keptTailTokens":<估算>,
  "summaryNote":"已压缩早期对话以适配上下文（原文保留在会话库中）"}}
```

前端在聊天区顶部显示一行**不阻塞**的提示（如「已压缩早期对话以适配上下文窗口」）。
窗口过小被拒绝启用时走 `chat.error`（`code=MODEL_INVALID`）。
挂载点二命中超额时同样先发 `context.compacted`（trigger 标在 `state`/后续重试里）再重试请求。

## 8. 本轮不做（与定稿方案的对齐说明）

- L2 摘要只做主模型（方案「开放问题」v1 定的就是主模型）。
- 状态机计数/代数为内存级：不做 004 迁移（sessions.context_length / messages.active/compacted 状态位、
  compaction_records、spilled_outputs 落库）、不做 FTS5 trigram 检索工具——这批依赖会话存储层，下一轮做。
- 落盘（L1）文件写在 sidecar 数据目录 `spill/` 下，索引仅内存；原文以文件形式保全。
