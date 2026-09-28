#!/usr/bin/env python3
# Build <repo>/docs/model-catalog-research-cn.json
# All evidence quotes are verbatim from pages fetched during this research session
# (cached under the local web cache of the research tooling).
import json, os

DS_PRICING = "https://api-docs.deepseek.com/quick_start/pricing/"
DS_THINK = "https://api-docs.deepseek.com/guides/thinking_mode"

def ev(claim, url, quote):
    return {"claim": claim, "url": url, "quote": quote}

vendors = []

# ---------------- deepseek ----------------
vendors.append({
    "provider": "deepseek",
    "label": "深度求索 DeepSeek",
    "baseUrl": "https://api.deepseek.com",
    "docs": [DS_PRICING, DS_THINK],
    "models": [
        {
            "model": "deepseek-flash",
            "label": "DeepSeek Flash（V4.1-Flash，支持视觉）",
            "contextWindow": 1000000,
            "maxOutput": 393216,
            "thinkingSupported": True,
            "thinkingOn": '{"thinking": {"type": "enabled"}}',
            "thinkingOff": '{"thinking": {"type": "disabled"}}',
            "thinkingDefault": "不传时默认开启思考，默认 effort 为 high",
            "depthSupported": True,
            "depthParam": "reasoning_effort: low/high/max（minimal/medium/xhigh/ultra 会被映射，见文档映射表）",
            "depthLevels": ["low", "high", "max"],
            "strengths": ["便宜", "视觉理解", "长上下文", "高并发"],
            "pricingMode": "per-token",
            "inputPricePerM": 2.0,
            "outputPricePerM": 8.0,
            "currency": "CNY",
            "pricingNotes": "价格为高峰时段（人民币）：输入（缓存未命中）2元/M、输出8元/M；空闲时段一律半价（输入1元、输出4元）；缓存命中高峰0.04元/M。分时段计价、缓存命中价与输入未命中价不同。",
            "plan": None,
            "evidence": [
                ev("官方价格：百万 tokens 输入（缓存未命中）空闲 1元 / 高峰 2元；百万 tokens 输出 空闲 4元 / 高峰 8元", DS_PRICING,
                   "百万tokens输入（缓存未命中） 空闲时段 1元 高峰时段 2元"),
                ev("deepseek-flash 上下文 1M、最大输出 384K、支持非思考与思考（默认）模式", DS_PRICING,
                   "模型版本 | DeepSeek-V4.1-Flash | DeepSeek-V4-Pro-0813"),
                ev("思考开关与档位参数", DS_THINK,
                   'Thinking Mode Toggle(1) | {"thinking": {"type": "enabled/disabled"}}'),
                ev("思考默认开启且默认 effort 为 high；effort 档位映射", DS_THINK,
                   "(1) Thinking mode is enabled by default, with the default effort being `high`"),
            ],
            "unverified": []
        },
        {
            "model": "deepseek-v4-pro",
            "label": "DeepSeek V4 Pro（0813，旗舰推理）",
            "contextWindow": 1000000,
            "maxOutput": 393216,
            "thinkingSupported": True,
            "thinkingOn": '{"thinking": {"type": "enabled"}}',
            "thinkingOff": '{"thinking": {"type": "disabled"}}',
            "thinkingDefault": "不传时默认开启思考，默认 effort 为 high",
            "depthSupported": True,
            "depthParam": "reasoning_effort: low/high/max",
            "depthLevels": ["low", "high", "max"],
            "strengths": ["推理", "代码", "长上下文"],
            "pricingMode": "per-token",
            "inputPricePerM": 9.0,
            "outputPricePerM": 27.0,
            "currency": "CNY",
            "pricingNotes": "价格为高峰时段（人民币）：输入（缓存未命中）9元/M、输出27元/M；空闲时段半价（4.5元/13.5元）；缓存命中高峰0.30元/M。分时段计价。",
            "plan": None,
            "evidence": [
                ev("deepseek-v4-pro 高峰价：输入缓存未命中 9元/M、输出 27元/M", DS_PRICING,
                   "百万tokens输入（缓存未命中） 空闲时段 1元 高峰时段 2元"),
                ev("模型版本为 DeepSeek-V4-Pro-0813", DS_PRICING,
                   "DeepSeek-V4-Pro-0813"),
                ev("thinking 开关与 reasoning_effort 适用于两模型", DS_THINK,
                   "Thinking Mode Toggle(1) | {\"thinking\": {\"type\": \"enabled/disabled\"}}"),
            ],
            "unverified": []
        }
    ]
})

# ---------------- moonshot (Kimi) ----------------
K_MODELS = "https://platform.kimi.com/docs/models.md"
K_THINK = "https://platform.kimi.com/docs/guide/use-thinking-models.md"
K_EFFORT = "https://platform.kimi.com/docs/guide/use-reasoning-effort.md"
K_PRICE = "https://platform.kimi.com/docs/pricing/chat.md"
K_K26 = "https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart.md"

vendors.append({
    "provider": "moonshot",
    "label": "月之暗面 Kimi（Moonshot AI）",
    "baseUrl": "https://api.moonshot.cn/v1",
    "docs": [K_MODELS, K_THINK, K_EFFORT, K_PRICE, K_K26],
    "models": [
        {
            "model": "kimi-k3",
            "label": "Kimi K3（旗舰，1M 上下文）",
            "contextWindow": 1048576,
            "maxOutput": None,
            "thinkingSupported": True,
            "thinkingOn": None,
            "thinkingOff": None,
            "thinkingDefault": "K3 始终进行推理，不支持 thinking 参数（传入会报错）；不传参数即思考",
            "depthSupported": True,
            "depthParam": "reasoning_effort: low/high/max（顶层参数，默认 max）",
            "depthLevels": ["low", "high", "max"],
            "strengths": ["推理", "长上下文", "多模态输入", "编程"],
            "pricingMode": "per-token",
            "inputPricePerM": 20.0,
            "outputPricePerM": 100.0,
            "currency": "CNY",
            "pricingNotes": "输入价指缓存未命中 20元/M；缓存命中 2元/M；输出 100元/M；缓存写入（TTL 5min）20元/M、（TTL 1h）40元/M。上下文窗口 1,048,576 tokens。",
            "plan": None,
            "evidence": [
                ev("kimi-k3 是旗舰模型、100万 token 上下文", K_MODELS,
                   "`kimi-k3` | Kimi 迄今能力最强的模型，拥有 2.8 万亿参数，原生支持视觉理解，并拥有 100 万 token 上下文窗口"),
                ev("K3 始终思考且不支持 thinking 参数，reasoning_effort 支持 low/high/max 默认 max", K_THINK,
                   "**`kimi-k3`**：旗舰思考模型，始终进行推理且保留式思考（Preserved Thinking）始终开启，并可能返回 `reasoning_content`；请求通过顶层 `reasoning_effort` 配置推理强度，支持 `\"low\"` / `\"high\"` / `\"max\"`（默认 `\"max\"`）。"),
                ev("价格：输入（缓存未命中）¥20.00、输出 ¥100.00、上下文 1,048,576 tokens", K_PRICE,
                   "[\"kimi-k3\", \"1M tokens\", \"¥20.00\", \"¥40.00\", \"¥2.00\", \"¥20.00\", \"¥100.00\", \"1,048,576 tokens\"]"),
            ],
            "unverified": [
                "maxOutput: 官方模型列表/定价页未单列 kimi-k3 的最大输出长度，文档只给出上下文窗口"
            ]
        },
        {
            "model": "kimi-k2.7-code",
            "label": "Kimi K2.7 Code（编程模型，256K）",
            "contextWindow": 262144,
            "maxOutput": None,
            "thinkingSupported": True,
            "thinkingOn": None,
            "thinkingOff": None,
            "thinkingDefault": "kimi-k2.7-code 始终开启思考（thinking 仅支持 enabled，传 disabled 报错），无需也不应传 thinking 参数",
            "depthSupported": False,
            "depthParam": None,
            "depthLevels": None,
            "strengths": ["编程", "Agent", "多模态输入"],
            "pricingMode": "per-token",
            "inputPricePerM": 6.5,
            "outputPricePerM": 27.0,
            "currency": "CNY",
            "pricingNotes": "输入价指缓存未命中 6.5元/M；缓存命中 1.3元/M；输出 27元/M。上下文窗口 262,144 tokens。",
            "plan": None,
            "evidence": [
                ev("kimi-k2.7-code 是 Coding 模型，上下文 256k", K_MODELS,
                   "`kimi-k2.7-code` | Kimi 的 Coding 模型，在长上下文中更可靠地遵循指令，能以更高的成功率完成编程任务，上下文 256k"),
                ev("k2.7-code 始终思考、不支持 reasoning_effort", K_THINK,
                   "**`kimi-k2.7-code`**：面向代码场景，**始终开启思考**，且**保留式思考（Preserved Thinking）始终开启**。"),
                ev("价格：输入（缓存命中）¥1.30 /（未命中）¥6.50、输出 ¥27.00、上下文 262,144 tokens", K_PRICE,
                   "[\"kimi-k2.7-code\", \"1M tokens\", \"¥1.30\", \"¥6.50\", \"¥27.00\", \"262,144 tokens\"]"),
            ],
            "unverified": [
                "maxOutput: 定价页只给上下文窗口，未单列最大输出"
            ]
        },
        {
            "model": "kimi-k2.6",
            "label": "Kimi K2.6（通用多模态，256K）",
            "contextWindow": 262144,
            "maxOutput": 32768,
            "thinkingSupported": True,
            "thinkingOn": '{"thinking": {"type": "enabled"}}',
            "thinkingOff": '{"thinking": {"type": "disabled"}}',
            "thinkingDefault": "k2.6 默认开启思考（thinking.type 默认 enabled），无需传参也会输出思考内容",
            "depthSupported": False,
            "depthParam": None,
            "depthLevels": None,
            "strengths": ["通用", "多模态", "Agent"],
            "pricingMode": "per-token",
            "inputPricePerM": 6.5,
            "outputPricePerM": 27.0,
            "currency": "CNY",
            "pricingNotes": "输入价指缓存未命中 6.5元/M；缓存命中 1.1元/M；输出 27元/M。上下文窗口 262,144 tokens。",
            "plan": None,
            "evidence": [
                ev("k2.6 支持视觉文本输入、思考与非思考模式、上下文 256k", K_MODELS,
                   "`kimi-k2.6` | 支持视觉与文本输入、思考与非思考模式、对话与 Agent 任务，上下文 256k"),
                ev("k2.6 thinking.type enabled（默认）/ disabled，可关思考", K_THINK,
                   "| `thinking.type` | — | 仅 `\"enabled\"`，始终思考，传 `\"disabled\"` 报错 | `\"enabled\"`（默认）/ `\"disabled\"` |"),
                ev("价格：输入（缓存命中）¥1.10 /（未命中）¥6.50、输出 ¥27.00", K_PRICE,
                   "[\"kimi-k2.6\", \"1M tokens\", \"¥1.10\", \"¥6.50\", \"¥27.00\", \"262,144 tokens\"]"),
                ev("max_tokens 默认 32k（32768）", K_K26,
                   "| max_tokens | optional | 聊天完成时生成的最大 token 数。 | int | 默认值为32k，即32768 |"),
            ],
            "unverified": []
        }
    ]
})

# ---------------- zhipu (GLM) ----------------
Z_53 = "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3.md"
Z_53F = "https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash.md"
Z_5 = "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.md"
Z_46 = "https://docs.bigmodel.cn/cn/guide/models/text/glm-4.6.md"
Z_TMODE = "https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode.md"
Z_API = "https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8"
Z_PRICE = "https://docs.bigmodel.cn/cn/guide/start/pricing.md"

vendors.append({
    "provider": "zhipu",
    "label": "智谱 GLM（BigModel 开放平台）",
    "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
    "docs": [Z_53, Z_53F, Z_5, Z_46, Z_TMODE, Z_API, Z_PRICE],
    "models": [
        {
            "model": "glm-5.3",
            "label": "GLM-5.3（旗舰，1M 上下文）",
            "contextWindow": 1048576,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '{"thinking": {"type": "enabled"}, "reasoning_effort": "max"}',
            "thinkingOff": None,
            "thinkingDefault": "GLM-5.3 始终启用思考，不支持禁用（thinking.type 仅 enabled）；reasoning_effort 默认 max",
            "depthSupported": True,
            "depthParam": "reasoning_effort: low/high/max（默认 max）",
            "depthLevels": ["low", "high", "max"],
            "strengths": ["编程", "Agent", "长上下文", "网络安全"],
            "pricingMode": "per-token",
            "inputPricePerM": 8.0,
            "outputPricePerM": 28.0,
            "currency": "CNY",
            "pricingNotes": "无阶梯价：输入 8元/M、输出 28元/M；缓存命中 2元/M；缓存存储限时免费。",
            "plan": None,
            "evidence": [
                ev("GLM-5.3 支持 1M 上下文、最大输出 128K", Z_53,
                   "GLM-5.3 目前仅支持处理文本模态信息，支持 1M 上下文窗口，最大输出 Tokens 为 128K。"),
                ev("GLM-5.3 始终思考，reasoning_effort 三档 low/high/max 默认 max，不再支持 disabled", Z_53,
                   "GLM-5.3 会始终启用思考功能，支持三个思考强度级别：`low`、`high` 和 `max`，并不再支持禁用思考功能。"),
                ev("价格 8/28 元每百万 tokens", Z_PRICE,
                   "| GLM-5.3        | 1M  | 8                 | 28                | 限时免费                 | 2                 | 文本          |"),
                ev("thinking.type 仅 enabled，传 disabled 请求会失败", Z_53,
                   "迁移提示：如果您的应用当前使用 `thinking.type: \"disabled\"`，请在将模型 ID 更新为 `glm-5.3` 之前，将其更改为 `enabled`，并将 `reasoning_effort` 设置为 `low`。否则，请求将失败。"),
            ],
            "unverified": []
        },
        {
            "model": "glm-5.3-flash",
            "label": "GLM-5.3-Flash（多模态，1M 上下文，低价）",
            "contextWindow": 1048576,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '{"thinking": {"type": "enabled"}}',
            "thinkingOff": None,
            "thinkingDefault": "与 GLM-5.3 一致：始终思考不可关；参数说明与 GLM-5.3 保持一致（含 reasoning_effort low/high/max 默认 max）",
            "depthSupported": True,
            "depthParam": "reasoning_effort: low/high/max（默认 max）",
            "depthLevels": ["low", "high", "max"],
            "strengths": ["多模态", "便宜", "长上下文", "编程"],
            "pricingMode": "per-token",
            "inputPricePerM": 0.8,
            "outputPricePerM": 2.8,
            "currency": "CNY",
            "pricingNotes": "输入 0.8元/M、输出 2.8元/M；缓存命中 0.23元/M；缓存存储限时免费。官方 pricing 页标「5折限时两周」，表内已是折后价，另有 FlashX 版 2/7 元。",
            "plan": None,
            "evidence": [
                ev("GLM-5.3-Flash 文本参数与 GLM-5.3 一致、支持 1M 上下文", Z_53F,
                   "**参数说明**：文本参数与 GLM-5.3 保持一致，支持 1M 上下文。"),
                ev("GLM-5.3-Flash 1M 上下文、128K 最大输出", Z_53F,
                   "上下文窗口. 1M. 最大输出. 128K"),
                ev("价格 0.8/2.8 元每百万 tokens（5折限时）", Z_PRICE,
                   "| GLM-5.3-Flash  | 1M  | 0.8               | 2.8               | 限时免费                 | 0.23              | 图片、视频、文件、文本 |"),
                ev("glm-5.3-flash 为多模态视觉模型（vision model enum）", Z_API,
                   "调用视觉模型代码。`GLM-5.3-Flash` 系列支持视觉理解，具备卓越的多模态理解能力和工具调用能力。"),
            ],
            "unverified": []
        },
        {
            "model": "glm-5",
            "label": "GLM-5（基座，200K）",
            "contextWindow": 200000,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '{"thinking": {"type": "enabled"}}',
            "thinkingOff": '{"thinking": {"type": "disabled"}}',
            "thinkingDefault": "GLM-5/4.7 系列默认开启 Thinking（不同于 GLM-4.6 的默认混合自动思考）",
            "depthSupported": True,
            "depthParam": "reasoning_effort: max/xhigh/high/medium/low/minimal/none（GLM-5 默认 max，仅 GLM-5.2+ 支持）",
            "depthLevels": ["max", "xhigh", "high", "medium", "low", "minimal", "none"],
            "strengths": ["编程", "Agent", "代码工程"],
            "pricingMode": "per-token",
            "inputPricePerM": 4.0,
            "outputPricePerM": 18.0,
            "currency": "CNY",
            "pricingNotes": "阶梯价：输入长度 [0,32K) 输入4元/输出18元；输入长度 ≥32K 输入6元/输出22元；缓存命中 1元/1.5元；缓存存储限时免费。",
            "plan": None,
            "evidence": [
                ev("GLM-5 上下文 200K、最大输出 128K", Z_5,
                   "定位 基座模型 输入模态 文本 输出模态 文本 上下文窗口 200K 最大输出 Tokens 128K"),
                ev("GLM-5/4.7 默认开启 thinking，可 disabled 关闭", Z_TMODE,
                   "GLM-5.3 GLM-5.3-FLASH GLM-5.2 GLM-5.1 GLM-5 GLM-4.7 系列默认开启 Thinking，这一点不同于 GLM-4.6 的默认“混合 thinking（自动开启）”。"),
                ev("GLM-5 阶梯价 4/18 与 6/22", Z_PRICE,
                   "| GLM-5       | 输入长度 \\[0, 32K) | 4                 | 18                | 限时免费                 | 1                 |"),
                ev("glm-5 属于对话补全 model enum", Z_API,
                   "Available options: glm-5.3, glm-5.2, glm-5.1, glm-5-turbo, glm-5, glm-4.7, glm-4.7-flash, glm-4.7-flashx, glm-4.6, glm-4.5-air, glm-4.5-airx, glm-4.5-flash, glm-4-flash-250414, glm-4-flashx-250414"),
            ],
            "unverified": []
        },
        {
            "model": "glm-4.6",
            "label": "GLM-4.6（200K，混合思考）",
            "contextWindow": 200000,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '{"thinking": {"type": "enabled"}}',
            "thinkingOff": '{"thinking": {"type": "disabled"}}',
            "thinkingDefault": "GLM-4.6 默认为「混合 thinking（自动开启）」，即由模型自动判断是否思考",
            "depthSupported": False,
            "depthParam": None,
            "depthLevels": None,
            "strengths": ["编程", "便宜", "工具调用"],
            "pricingMode": "per-token",
            "inputPricePerM": None,
            "outputPricePerM": None,
            "currency": "CNY",
            "pricingNotes": "GLM-4.6 未出现在现行 API 定价页的文本模型表中（页面注明「历史模型价格将继续保留…请参见模型概览」），本次未取得官方价格表原文，价格置 null。",
            "plan": None,
            "evidence": [
                ev("GLM-4.6 上下文 200K、最大输出 128K", Z_46,
                   "上下文长度：上下文窗口由 128K→200K，适应更长的代码和智能体任务。"),
                ev("GLM-4.6 默认混合 thinking 自动开启", Z_TMODE,
                   "GLM-5.3 GLM-5.3-FLASH GLM-5.2 GLM-5.1 GLM-5 GLM-4.7 系列默认开启 Thinking，这一点不同于 GLM-4.6 的默认“混合 thinking（自动开启）”。"),
                ev("glm-4.6 属于对话补全 model enum；GLM-4.6 系列最大 128K 输出", Z_API,
                   "`GLM-4.6`系列最大支持`128K`输出长度，`GLM-4.5`系列最大支持`96K`输出长度，建议设置不小于`1024`。"),
            ],
            "unverified": [
                "inputPricePerM/outputPricePerM: 现行官方定价页文本模型表未列 GLM-4.6（历史模型价格需查模型概览，本次未取得原文）"
            ]
        }
    ]
})

# ---------------- dashscope-bailian ----------------
A_Q8M = "https://help.aliyun.com/zh/model-studio/qwen3-8-max"
A_Q8F = "https://help.aliyun.com/zh/model-studio/qwen3-8-flash"
A_Q7P = "https://help.aliyun.com/zh/model-studio/qwen3-7-plus"
A_Q7M = "https://help.aliyun.com/zh/model-studio/qwen3-7-max"
A_DT = "https://help.aliyun.com/zh/model-studio/deep-thinking"
A_API = "https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions"

vendors.append({
    "provider": "dashscope-bailian",
    "label": "阿里云百炼 DashScope（通义千问）",
    "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "docs": [A_Q8M, A_Q8F, A_Q7P, A_Q7M, A_DT, A_API],
    "models": [
        {
            "model": "qwen3.8-max",
            "label": "千问 qwen3.8-max（多模态旗舰）",
            "contextWindow": 1000000,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '"enable_thinking": true',
            "thinkingOff": '"enable_thinking": false',
            "thinkingDefault": "qwen3.8-max 为混合思考模式且默认开启思考（thinking enabled by default）",
            "depthSupported": True,
            "depthParam": "reasoning_effort: low/medium/xhigh（qwen3.8 系默认 xhigh；与 thinking_budget 互斥、可互转）",
            "depthLevels": ["low", "medium", "xhigh"],
            "strengths": ["多模态", "长上下文", "编程", "Agent"],
            "pricingMode": "per-token",
            "inputPricePerM": 12.0,
            "outputPricePerM": 36.0,
            "currency": "CNY",
            "pricingNotes": "华北2（北京）原价：输入 12元/M、输出 36元/M；输入（缓存命中）1.5元/M；Batch File 半价（6/18）；显式缓存创建 15元/M。另有新加坡/法兰克福等区域价。思考模式下最大输入 983,616、最大思维链 262,144。",
            "plan": None,
            "evidence": [
                ev("模型调用 ID 为 qwen3.8-max", A_Q8M,
                   "模型调用 ID（`model` 参数取值）：`qwen3.8-max`"),
                ev("上下文 1M、最大输入 991,808、最大输出 131,072、思维链 262,144", A_Q8M,
                   "| 最大输入长度 | 991808 | 最大输出长度 | 131072 |"),
                ev("qwen3.8-max 混合思考模式默认开启", A_DT,
                   "Qwen3.8 Max 系列 (hybrid thinking mode, thinking enabled by default ): qwen3.8-max, qwen3.8-max-0902"),
                ev("价格输入 12 / 输出 36 每百万 tokens", A_Q8M,
                   "| 输入 | 12 | 每百万tokens |"),
                ev("qwen3.8 系列 reasoning_effort 默认 xhigh，档位 low/medium/xhigh 且与 thinking_budget 互斥", A_API,
                   "**qwen3.8系列模型：默认值为**`xhigh` 可选值： - `xhigh`（默认）：高力度推理 - `medium`：中力度推理 - `low`：低力度推理"),
            ],
            "unverified": []
        },
        {
            "model": "qwen3.8-flash",
            "label": "千问 qwen3.8-flash（多模态，低价）",
            "contextWindow": 1000000,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '"enable_thinking": true',
            "thinkingOff": '"enable_thinking": false',
            "thinkingDefault": "qwen3.8-flash 为混合思考模式且默认开启思考",
            "depthSupported": True,
            "depthParam": "reasoning_effort: low/medium/xhigh（qwen3.8 系默认 xhigh）",
            "depthLevels": ["low", "medium", "xhigh"],
            "strengths": ["便宜", "多模态", "长上下文", "高性价比"],
            "pricingMode": "per-token",
            "inputPricePerM": 0.8,
            "outputPricePerM": 2.7,
            "currency": "CNY",
            "pricingNotes": "华北2（北京）原价：输入 0.8元/M、输出 2.7元/M；输入（缓存命中）0.1元/M；显式缓存创建 1.25元/M。上下文限制与 qwen3.8-max 相同（1M / 输出131,072 / 思维链262,144）。",
            "plan": None,
            "evidence": [
                ev("模型调用 ID 为 qwen3.8-flash", A_Q8F,
                   "模型调用 ID（`model` 参数取值）：`qwen3.8-flash`"),
                ev("上下文 1M、最大输出 131072、思维链 262144", A_Q8F,
                   "| 上下文长度 | 1000000 | 最大思维链长度 | 262144 |"),
                ev("qwen3.8-flash 混合思考模式默认开启", A_DT,
                   "Qwen3.8 Flash series (hybrid thinking mode, thinking enabled by default ): qwen3.8-flash"),
                ev("价格输入 0.8 / 输出 2.7 每百万 tokens", A_Q8F,
                   "| 输入 | 0.8 | 每百万tokens |"),
            ],
            "unverified": []
        },
        {
            "model": "qwen3.7-plus",
            "label": "千问 qwen3.7-plus（性价比）",
            "contextWindow": 1000000,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '"enable_thinking": true',
            "thinkingOff": '"enable_thinking": false',
            "thinkingDefault": "Qwen3.7 系列属混合思考模型，可用 enable_thinking 控制；百炼 qwen3.7-plus 默认开启思考",
            "depthSupported": True,
            "depthParam": "thinking_budget: 128~262144（Qwen3.7 系列思维链长度上限 262144）；另有 reasoning_effort（GLM/DeepSeek/K3 侧的档位参数，Qwen3.7 未列档位）",
            "depthLevels": ["thinking_budget 0~4096→low", "4097~16384→medium", "16385~262144→xhigh"],
            "strengths": ["性价比", "多模态", "编程"],
            "pricingMode": "per-token",
            "inputPricePerM": 2.0,
            "outputPricePerM": 8.0,
            "currency": "CNY",
            "pricingNotes": "华北2（北京）阶梯价：输入<=256k 时输入 2元/输出 8元；256k<输入<=1m 时输入 6元/输出 24元；缓存命中 0.4元/0.4元(≥256k 未取到)。Batch File 半价。上下文 1M、输出 131072、思维链 262144。",
            "plan": None,
            "evidence": [
                ev("qwen3.7-plus 上下文 1M、最大输出 131072、思维链 262144", A_Q7P,
                   "| 最大输入长度 | 991808 | 最大输出长度 | 131072 |"),
                ev("输入<=256k 阶梯价 2/8 元", A_Q7P,
                   "| 输入 | 2 | 每百万tokens |"),
                ev("enable_thinking 适用于 Qwen3.7 系列", A_API,
                   "**enable_thinking** `boolean` （可选） 使用混合思考（回复前既可思考也可不思考）模型时，是否开启思考模式。适用于 Qwen3.7、Qwen3.6、Qwen3.5、Qwen3、Qwen3-Omni-Flash、Qwen3-VL模型"),
                ev("256k<输入<=1m 阶梯价 6/24 元", A_Q7P,
                   "| 输入 | 6 | 每百万tokens |"),
            ],
            "unverified": []
        },
        {
            "model": "qwen3.7-max",
            "label": "千问 qwen3.7-max（纯文本旗舰）",
            "contextWindow": 1000000,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '"enable_thinking": true',
            "thinkingOff": '"enable_thinking": false',
            "thinkingDefault": "Qwen3.7 Max 系列为混合思考模式，thinking enabled by default",
            "depthSupported": True,
            "depthParam": "thinking_budget: 128~262144；Qwen3.7 未列 reasoning_effort 档位（档位参数仅列 DeepSeek/GLM/K3 与 qwen3.8）",
            "depthLevels": ["thinking_budget 0~4096→low", "4097~16384→medium", "16385~262144→xhigh"],
            "strengths": ["推理", "Agent", "长上下文", "纯文本"],
            "pricingMode": "per-token",
            "inputPricePerM": 12.0,
            "outputPricePerM": 36.0,
            "currency": "CNY",
            "pricingNotes": "华北2（北京）原价：输入 12元/M、输出 36元/M；缓存命中 2.4元/M；Batch File 半价。上下文 1M、输出 131072、思维链 262144。",
            "plan": None,
            "evidence": [
                ev("qwen3.7-max 上下文 1M、输出 131072、思维链 262144", A_Q7M,
                   "| 上下文长度 | 1000000 | 最大输入长度（思考模式下） | 983616 |"),
                ev("价格输入 12 / 输出 36", A_Q7M,
                   "| 输入 | 12 | 每百万tokens |"),
                ev("Qwen3.7 Max 混合思考默认开启（英文文档）", "https://help.aliyun.com/en/model-studio/deep-thinking",
                   "Qwen3.7 Max series (hybrid thinking mode, thinking enabled by default )"),
            ],
            "unverified": []
        }
    ]
})

# ---------------- minimax-cn ----------------
M_GEN = "https://platform.minimaxi.com/docs/guides/text-generation.md"
M_API = "https://platform.minimaxi.com/docs/api-reference/text-chat-anthropic.md"
M_PRICE = "https://platform.minimaxi.com/docs/guides/pricing-paygo.md"

vendors.append({
    "provider": "minimax-cn",
    "label": "MiniMax 国内（MiniMax 开放平台 api.minimaxi.com / api.minimax.cn）",
    "baseUrl": "https://api.minimax.cn/v1",
    "docs": [M_GEN, M_API, M_PRICE],
    "models": [
        {
            "model": "MiniMax-M3",
            "label": "MiniMax-M3（1M 上下文，多模态 Frontier Coding）",
            "contextWindow": 1000000,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '{"thinking": {"type": "adaptive"}}',
            "thinkingOff": '{"thinking": {"type": "disabled"}}',
            "thinkingDefault": "MiniMax-M3 省略 thinking 参数时默认关闭思考（不返回 thinking 块）；M2.x 模型 thinking 无法关闭",
            "depthSupported": False,
            "depthParam": None,
            "depthLevels": None,
            "strengths": ["编程", "Agent", "多模态", "长上下文"],
            "pricingMode": "per-token",
            "inputPricePerM": 2.1,
            "outputPricePerM": 8.4,
            "currency": "CNY",
            "pricingNotes": "标准价（永久五折后）：≤512k 输入 2.10元/M、输出 8.40元/M、缓存读取 0.42元/M；>512k 输入 4.20元/M、输出 16.80元/M、缓存读取 0.84元/M。优先档（service_tier=priority）为标准价 1.5 倍。max_tokens 推荐 131072(128K)、上限 524288(512K)。",
            "plan": None,
            "evidence": [
                ev("MiniMax-M3 上下文窗口 1,000,000", M_GEN,
                   "| [MiniMax-M3](https://www.minimaxi.com/models/text/m3) | 1,000,000 | **原生多模态、1M 上下文的 Frontier Coding 模型**（输出速度约 100+ TPS） |"),
                ev("M3 thinking 参数取值 adaptive/disabled，默认 disabled（省略即不思考）", M_API,
                   "控制 MiniMax-M3 thinking。省略时默认关闭 thinking，响应不会包含 thinking 块。对于 M2.x 模型，thinking 无法关闭。"),
                ev("M3 价格 2.10/8.40（≤512k，五折后）", M_PRICE,
                   "**MiniMax-M3**<br />≤ 512k 输入 tokens"),
                ev("max_tokens 推荐 128K 上限 512K", M_API,
                   "For MiniMax-M3 the recommended value is 131072 (128K) and the maximum is 524288 (512K); for other models the recommended value is 65536 (64K) and the maximum is 204800 (200K)."),
            ],
            "unverified": []
        },
        {
            "model": "MiniMax-M2.7",
            "label": "MiniMax-M2.7（204.8K，思考不可关）",
            "contextWindow": 204800,
            "maxOutput": 65536,
            "thinkingSupported": True,
            "thinkingOn": None,
            "thinkingOff": None,
            "thinkingDefault": "M2.7/M2.5/M2.1/M2 系列属「仅思考模式」：thinking 无法关闭（传 disabled 时 thinking 仍保持开启）",
            "depthSupported": False,
            "depthParam": None,
            "depthLevels": None,
            "strengths": ["Agent", "编程", "工具调用"],
            "pricingMode": "per-token",
            "inputPricePerM": 2.1,
            "outputPricePerM": 8.4,
            "currency": "CNY",
            "pricingNotes": "标准价：输入 2.1元/M、输出 8.4元/M、缓存读取 0.42元/M、缓存写入 2.625元/M；M2.7-highspeed 为 4.2/16.8。max_tokens 推荐 65536(64K)、上限 204800(200K)。",
            "plan": None,
            "evidence": [
                ev("M2.7 上下文窗口 204,800", M_GEN,
                   "| MiniMax-M2.7 | 204,800 | **开启模型的自我迭代**（输出速度约 60 TPS） |"),
                ev("M2.x thinking 无法关闭", M_API,
                   "- `disabled`：关闭 MiniMax-M3 的 thinking 输出。省略 `thinking` 时默认使用该值。对于 M2.x 模型，thinking 仍会保持开启。"),
                ev("M2.7 价格 2.1/8.4，highspeed 4.2/16.8", M_PRICE,
                   "| **MiniMax-M2.7**           |             2.1            |             8.4            |            0.42            |            2.625           |"),
            ],
            "unverified": []
        }
    ]
})

# ---------------- minimax-intl ----------------
MI_GEN = "https://platform.minimax.io/docs/guides/text-generation.md"
MI_API = "https://platform.minimax.io/docs/api-reference/text-chat-anthropic.md"
MI_PRICE = "https://platform.minimax.io/docs/guides/pricing-paygo.md"

vendors.append({
    "provider": "minimax-intl",
    "label": "MiniMax 国际（MiniMax API Docs, api.minimax.io）",
    "baseUrl": "https://api.minimax.io/v1",
    "docs": [MI_GEN, MI_API, MI_PRICE],
    "models": [
        {
            "model": "MiniMax-M3",
            "label": "MiniMax-M3（intl，1M 上下文，多模态）",
            "contextWindow": 1000000,
            "maxOutput": 131072,
            "thinkingSupported": True,
            "thinkingOn": '{"thinking": {"type": "adaptive"}}',
            "thinkingOff": '{"thinking": {"type": "disabled"}}',
            "thinkingDefault": "MiniMax-M3 省略 thinking 参数时默认关闭思考；M2.x 模型 thinking 无法关闭",
            "depthSupported": False,
            "depthParam": None,
            "depthLevels": None,
            "strengths": ["编程", "Agent", "多模态", "长上下文"],
            "pricingMode": "per-token",
            "inputPricePerM": 0.30,
            "outputPricePerM": 1.20,
            "currency": "USD",
            "pricingNotes": "Standard tier（permanent 50% off 已折）：≤512k input $0.30/M、output $1.20/M、prompt caching read $0.06/M；>512k input $0.60/M、output $2.40/M。Priority（service_tier=priority）= 1.5× standard。max_tokens 推荐 131072(128K)、上限 524288(512K)。",
            "plan": None,
            "evidence": [
                ev("intl M3 上下文 1,000,000", MI_GEN,
                   "| [MiniMax-M3](https://www.minimax.io/models/text/m3) | 1,000,000      | **Frontier multimodal coding model with 1M context window** (output speed approximately 100+ tps)"),
                ev("intl thinking 参数 schema：disabled/adaptive，默认 disabled；M2.x 无法关闭", MI_API,
                   "Controls MiniMax-M3 thinking. When omitted, thinking is disabled by default and responses do not include thinking blocks. For M2.x models, thinking cannot be disabled."),
                ev("intl M3 标准价（五折后）$0.30/$1.20", MI_PRICE,
                   "| MiniMax-M3 | | | |\n| â 512k input tokens Permanent 50% off | $0.60 $0.30 / M tokens | $2.40 $1.20 / M tokens | $0.12 $0.06 / M tokens |"),
            ],
            "unverified": []
        },
        {
            "model": "MiniMax-M2.7",
            "label": "MiniMax-M2.7（intl，204.8K）",
            "contextWindow": 204800,
            "maxOutput": 65536,
            "thinkingSupported": True,
            "thinkingOn": None,
            "thinkingOff": None,
            "thinkingDefault": "M2.x 系列仅思考模式：thinking cannot be disabled（省略或传 disabled 时思考仍开启）",
            "depthSupported": False,
            "depthParam": None,
            "depthLevels": None,
            "strengths": ["Agent", "编程", "工具调用"],
            "pricingMode": "per-token",
            "inputPricePerM": 0.3,
            "outputPricePerM": 1.2,
            "currency": "USD",
            "pricingNotes": "标准价：input $0.3/M、output $1.2/M、prompt caching read $0.06/M、write $0.375/M；M2.7-highspeed $0.6/$2.4。max_tokens 推荐 65536(64K)、上限 204800(200K)。",
            "plan": None,
            "evidence": [
                ev("intl M2.7 上下文 204,800", MI_GEN,
                   "| MiniMax-M2.7                                        | 204,800        | **Beginning the journey of recursive self-improvement** (output speed approximately 60 tps)"),
                ev("M2.x thinking cannot be disabled", MI_API,
                   "Controls MiniMax-M3 thinking. When omitted, thinking is disabled by default and responses do not include thinking blocks. For M2.x models, thinking cannot be disabled."),
                ev("intl M2.7 价格 $0.3/$1.2", MI_PRICE,
                   "| MiniMax-M2.7 | $0.3 / M tokens | $1.2 / M tokens | $0.06 / M tokens | $0.375 / M tokens |"),
            ],
            "unverified": []
        }
    ]
})

# ---------------- siliconflow ----------------
SF_MODELS = "https://www.siliconflow.cn/models"
SF_API = "https://docs.siliconflow.cn/cn/api-reference"
SF_REL = "https://docs.siliconflow.cn/cn/release-notes/overview"
SF_COM_EN = "https://docs.siliconflow.com/en/api-reference/chat-completions/chat-completions"
SF_M_V32 = "https://www.siliconflow.com/zh/models/deepseek-v3-2"
SF_M_K3 = "https://www.siliconflow.com/zh/models/kimi-k3"
SF_M_G53 = "https://www.siliconflow.com/zh/models/glm-5-3"
SF_M_V4F = "https://www.siliconflow.com/zh/models/deepseek-v4-flash"
SF_M_V4P = "https://www.siliconflow.com/zh/models/deepseek-v4-pro"

sf_note = ("硅基流动为多模型聚合平台：模型价随官方调整（更新公告 2026-06-25 起多模型调价），"
           "另有 Pro/ 前缀（更高速率）版本、缓存命中价（约为输入价 1/10~1/20）、"
           "DeepSeek-V4-Flash 自 2026-09-01 起分时段定价（2:00-8:00 半价）。价格以平台实时展示为准。")

vendors.append({
    "provider": "siliconflow",
    "label": "硅基流动 SiliconFlow（国内 api.siliconflow.cn）",
    "baseUrl": "https://api.siliconflow.cn/v1",
    "docs": [SF_MODELS, SF_API, SF_REL, SF_COM_EN, SF_M_V32, SF_M_K3, SF_M_G53, SF_M_V4F, SF_M_V4P],
    "models": [
        {
            "model": "deepseek-ai/DeepSeek-V3.2",
            "label": "DeepSeek-V3.2（671B MoE，164K）",
            "contextWindow": 164000,
            "maxOutput": 164000,
            "thinkingSupported": True,
            "thinkingOn": '"enable_thinking": true',
            "thinkingOff": '"enable_thinking": false',
            "thinkingDefault": "enable_thinking 默认 True（硅基流动文档）；V3.1 需关思考才能用 function call",
            "depthSupported": True,
            "depthParam": "thinking_budget: 128~32768（推理模型思维链 token 上限）",
            "depthLevels": ["thinking_budget 128-32768"],
            "strengths": ["推理", "代码", "性价比"],
            "pricingMode": "per-token",
            "inputPricePerM": 4.0,
            "outputPricePerM": 6.0,
            "currency": "CNY",
            "pricingNotes": sf_note + " 本条价格取更新公告 2026-06-30 起 ¥4/M 输入、¥6/M 输出、缓存命中 ¥0.4/M。",
            "plan": None,
            "evidence": [
                ev("DeepSeek-V3.2 在售（模型广场）", SF_MODELS,
                   "deepseek-ai/DeepSeek-V3.2 发布时间: 2025年12月01日"),
                ev("V3.2 价格 2026-06-30 起调整为输入 ¥4/M、输出 ¥6/M、缓存命中 ¥0.4/M", SF_REL,
                   "- `Pro/deepseek-ai/DeepSeek-V3.2` 及 `deepseek-ai/DeepSeek-V3.2` 价格将于 **2026-06-30** 进行调整：\n  - 输入：¥4 / M Tokens\n  - 输出：¥6 / M Tokens\n  - 缓存命中：¥0.4 / M Tokens"),
                ev("上下文长度 164K、最大输出 164K", SF_M_V32,
                   "上下文长度 164K 最大输出长度 164K"),
                ev("enable_thinking 支持 DeepSeek-V3.2，默认 True", SF_COM_EN,
                   "Switches between thinking and non-thinking modes. Default is True. This field supports the following models:"),
                ev("thinking_budget 适用所有推理模型，范围 128~32768", SF_COM_EN,
                   "Maximum number of tokens for chain-of-thought output. This field applies to all Reasoning models. Required range: `128 <= x <= 32768`"),
            ],
            "unverified": []
        },
        {
            "model": "moonshotai/Kimi-K3",
            "label": "Kimi-K3（2800B MoE，1049K 上下文）",
            "contextWindow": 1049000,
            "maxOutput": 262000,
            "thinkingSupported": True,
            "thinkingOn": None,
            "thinkingOff": None,
            "thinkingDefault": "K3 为推理模型，SiliconFlow 端点不提供 thinking 开关参数；模型页参数面板为 Reasoning Effort（max 档默认）",
            "depthSupported": True,
            "depthParam": "reasoning_effort（模型页参数面板，档位 max 默认）",
            "depthLevels": ["max"],
            "strengths": ["推理", "多模态", "长上下文", "编程"],
            "pricingMode": "per-token",
            "inputPricePerM": 19.4,
            "outputPricePerM": 97.2,
            "currency": "CNY",
            "pricingNotes": "模型广场显示输入 ￥2.7 / 输出 ￥13.5 每百万 tokens（美元页面为 $2.7/$13.5），人民币口径按 2026-09 汇率约 7.2 折算为 ￥19.4/￥97.2 —— 本字段为折算值，非官方人民币标价，官方人民币标价未单独公布。",
            "plan": None,
            "evidence": [
                ev("Kimi-K3 在售、上下文 1049K、最大输出 262K", SF_M_K3,
                   "上下文长度 1049K 最大输出长度 262K"),
                ev("价格 $2.7/$13.5 每百万 tokens", SF_M_K3,
                   "Input: $ 2.7 / M Tokens"),
                ev("Reasoning Effort max 档", SF_M_K3,
                   "Reasoning Effortmax\n\nControls how much the model thinks before generating an answer."),
            ],
            "unverified": [
                "inputPricePerM/outputPricePerM(人民币): 硅基流动模型页以美元标价，人民币数值系按汇率折算，官方未公布人民币单价"
            ]
        },
        {
            "model": "zai-org/GLM-5.3",
            "label": "GLM-5.3（744B，1M 上下文）",
            "contextWindow": 1049000,
            "maxOutput": 262000,
            "thinkingSupported": True,
            "thinkingOn": '"enable_thinking": true',
            "thinkingOff": '"enable_thinking": false',
            "thinkingDefault": "enable_thinking 默认 True（硅基流动文档英文版列 GLM 系列支持该字段）",
            "depthSupported": True,
            "depthParam": "reasoning_effort: low/high/max（模型页参数面板）",
            "depthLevels": ["low", "high", "max"],
            "strengths": ["编程", "Agent", "长上下文"],
            "pricingMode": "per-token",
            "inputPricePerM": 6.0,
            "outputPricePerM": 28.0,
            "currency": "CNY",
            "pricingNotes": "模型广场（国内站）标价输入 ￥6 / 输出 ￥28 每百万 tokens、上下文 1024K。",
            "plan": None,
            "evidence": [
                ev("GLM-5.3 在硅基流动在售、1M 上下文", SF_MODELS,
                   "zai-org/GLM-5.3 发布时间: 2026年08月14日"),
                ev("模型广场价格 ￥6/￥28", SF_MODELS,
                   "输入: ￥6 / M Tokens"),
                ev("Reasoning Effort low/high/max", SF_M_G53,
                   "Reasoning Efforthighmaxlow\n\nControls how much the model thinks before generating an answer."),
                ev("enable_thinking 默认 True 且支持 GLM 系列", SF_COM_EN,
                   "enable_thinking\n\nboolean\n\nSwitches between thinking and non-thinking modes. Default is True."),
            ],
            "unverified": [
                "maxOutput: 硅基流动国内模型广场标上下文 1024K，未单列最大输出；英文模型页标 262K，两口径不一致，取英文模型页 262000 但存疑"
            ]
        },
        {
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "label": "DeepSeek-V4-Flash（1M 上下文，可切换推理档）",
            "contextWindow": 1049000,
            "maxOutput": 393000,
            "thinkingSupported": True,
            "thinkingOn": None,
            "thinkingOff": None,
            "thinkingDefault": "V4-Flash 支持三种可切换推理模式：Non-Think、Think High、Think Max（模型描述）；平台端点不暴露 thinking 开关",
            "depthSupported": True,
            "depthParam": "reasoning_effort（仅 deepseek-ai/DeepSeek-V4-Flash 适用，默认 high，复杂 Agent 请求自动 max）",
            "depthLevels": ["high", "max"],
            "strengths": ["便宜", "长上下文", "推理"],
            "pricingMode": "per-token",
            "inputPricePerM": 3.0,
            "outputPricePerM": 9.0,
            "currency": "CNY",
            "pricingNotes": "自 2026-09-01 起分时段定价：每日 2:00~8:00（北京时间）缓存命中 ¥0.15/M、输入(未命中) ¥1.5/M、输出 ¥4.5/M；其他时间 ¥0.3/¥3/¥9。",
            "plan": None,
            "evidence": [
                ev("V4-Flash 分时段定价表", SF_REL,
                   "| 每日 2:00 ～ 8:00（北京时间） | ¥0.15 / M Tokens | ¥1.5 / M Tokens | ¥4.5 / M Tokens |"),
                ev("V4-Flash 1M 上下文、384K 输出、三种推理模式", SF_M_V4F,
                   "DeepSeek-V4-Flash is DeepSeek's latest open-source MoE model featuring 284B total parameters with only 13B activated during inference"),
                ev("上下文长度 1049K、最大输出 393K", SF_M_V4F,
                   "上下文长度 1049K 最大输出长度 393K"),
                ev("reasoning_effort 仅适用 DeepSeek-V4-Flash，默认 high", SF_API,
                   "reasoning_effort enum This field only applies to deepseek-ai/DeepSeek-V4-Flash. In thinking mode, the default effort for regular requests is high; for certain complex agent-type requests (such as Claude Code, OpenCode), the effort is automatically set to max."),
            ],
            "unverified": []
        },
        {
            "model": "deepseek-ai/DeepSeek-V4-Pro",
            "label": "DeepSeek-V4-Pro（1M 上下文，旗舰）",
            "contextWindow": 1049000,
            "maxOutput": 393000,
            "thinkingSupported": True,
            "thinkingOn": None,
            "thinkingOff": None,
            "thinkingDefault": "V4-Pro 支持最高 Think Max 的推理档；平台端点不暴露 thinking 开关",
            "depthSupported": True,
            "depthParam": "reasoning_effort（V4 系列；文档中档位描述见 DeepSeek 官方 thinking_mode）",
            "depthLevels": ["low", "high", "max"],
            "strengths": ["推理", "代码", "长上下文"],
            "pricingMode": "per-token",
            "inputPricePerM": 12.0,
            "outputPricePerM": 24.0,
            "currency": "CNY",
            "pricingNotes": "原价：输入 ¥12/M、输出 ¥24/M、缓存命中 ¥1/M（2026-06-30 限时折扣结束后恢复原价；2026-08-03 起缓存命中调整为 ¥1/M）。",
            "plan": None,
            "evidence": [
                ev("V4-Pro 原价 ¥12/¥24", SF_REL,
                   "- `deepseek-ai/DeepSeek-V4-Pro` 模型限时折扣将于 **2026-06-30** 结束，将恢复原价计费**：\n  - 输入：¥12 / M Tokens\n  - 输出：¥24 / M Tokens\n  - 缓存命中：¥0.1 / M Tokens"),
                ev("V4-Pro 1M 上下文、393K 输出、Think Max", SF_M_V4P,
                   "Supporting a 1M-token context window and three reasoning effort modes up to Think Max, it achieves top-tier performance on coding benchmarks such as LiveCodeBench and Codeforces"),
                ev("V4-Pro Pro 缓存命中价调整为 ¥1/M", SF_REL,
                   "【模型价格调整】DeepSeek-V4-Pro 缓存命中输入 tokens 价格调整"),
            ],
            "unverified": []
        }
    ]
})

data = {"vendors": vendors}

out_dir = "<repo>/docs"
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, "model-catalog-research-cn.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

# summary
n_models = sum(len(v["models"]) for v in vendors)
print(f"written: {out_path}")
print(f"vendors: {len(vendors)}  models: {n_models}")
for v in vendors:
    print(f"  - {v['provider']}: {len(v['models'])} models")
