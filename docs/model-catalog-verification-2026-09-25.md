# 预置模型目录 · 引用核验与红旗判定（2026-09-25）

目录文件：`docs/model-catalog-research-cn.json`（国内 7 家 / 22 模型）、`docs/model-catalog-research-intl.json`（国际 4 家 / 15 模型）；合成产物 `python/model_catalog.json`（37 模型）。

## 一、引用可机上核验率（逐字比对 67 个官方页面缓存）

| 线 | 统计（归一化器实测） |
|---|---|
| CN | exact=53 remapped=0 render_diff=12 partial=0 value_conflict=0 failed=0 short=0 |
| INTL | exact=27 remapped=0 render_diff=7 partial=3 value_conflict=5 failed=0 short=0 |

口径：`exact` = 引用是页面原文（逐字命中）；`remapped` = 引用原为改写/译文，已按页面对应片段回写成页面原文，再跑一遍即 exact；`render_diff` = 页面确有该事实、只是排版/表格渲染不同；`flagged_unverified` = 见下表，逐条标了未核到；`fetch_failed` = 页面抓不到（本轮已降为 0）。

## 二、50 条红旗判定

判定只允许四档：`page_text_diff`（页面有、引用写法不同，已回写成页面原文）、`quote_rewritten_fact_ok`（引用是复述但页面确有等价数字，已换成页面原文）、`partial_match` / `not_on_page`（**页面没有这段文本 → 不改写、不背书**，在该模型条目的 `unverified` 里挂一条带 evidence 编号的说明）。

判定分布：{('CN', 'not_on_page'): 15, ('INTL', 'page_text_diff'): 4, ('INTL', 'partial_match'): 4, ('INTL', 'not_on_page'): 27}

| # | 线 | 模型 | 判定 | 最长公共子串 |”引用是否页面原文“ |
|---|---|---|---|---|---|
| 1 | CN | zhipu glm-5.3-flash ev3 | not_on_page | 11 | 否 |
| 2 | CN | dashscope-bailian qwen3.8-max ev2 | not_on_page | 16 | 否 |
| 3 | CN | dashscope-bailian qwen3.8-max ev3 | not_on_page | 13 | 否 |
| 4 | CN | dashscope-bailian qwen3.8-flash ev2 | not_on_page | 16 | 否 |
| 5 | CN | dashscope-bailian qwen3.8-flash ev3 | not_on_page | 14 | 否 |
| 6 | CN | dashscope-bailian qwen3.7-plus ev1 | not_on_page | 12 | 否 |
| 7 | CN | dashscope-bailian qwen3.7-plus ev3 | not_on_page | 12 | 否 |
| 8 | CN | dashscope-bailian qwen3.7-max ev1 | not_on_page | 13 | 否 |
| 9 | CN | siliconflow deepseek-ai/DeepSeek-V3.2 ev1 | not_on_page | 27 | 否 |
| 10 | CN | siliconflow moonshotai/Kimi-K3 ev2 | not_on_page | 16 | 否 |
| 11 | CN | siliconflow zai-org/GLM-5.3 ev1 | not_on_page | 13 | 否 |
| 12 | CN | siliconflow zai-org/GLM-5.3 ev2 | not_on_page | 15 | 否 |
| 13 | CN | siliconflow deepseek-ai/DeepSeek-V4-Flash ev3 | not_on_page | 15 | 否 |
| 14 | CN | siliconflow deepseek-ai/DeepSeek-V4-Pro ev0 | not_on_page | 24 | 否 |
| 15 | CN | siliconflow deepseek-ai/DeepSeek-V4-Pro ev2 | not_on_page | 13 | 否 |
| 16 | INTL | openai gpt-6-astra ev3 | page_text_diff | 62 | 是/已改写 |
| 17 | INTL | anthropic claude-opus-5-5 ev0 | partial_match | 58 | 否 |
| 18 | INTL | anthropic claude-opus-5-5 ev1 | not_on_page | 13 | 否 |
| 19 | INTL | anthropic claude-opus-5-5 ev2 | partial_match | 85 | 否 |
| 20 | INTL | anthropic claude-opus-5-5 ev4 | not_on_page | 12 | 否 |
| 21 | INTL | anthropic claude-opus-5-5 ev5 | page_text_diff | 60 | 是/已改写 |
| 22 | INTL | anthropic claude-opus-5-5 ev6 | partial_match | 40 | 否 |
| 23 | INTL | anthropic claude-sonnet-5 ev0 | partial_match | 69 | 否 |
| 24 | INTL | anthropic claude-sonnet-5 ev1 | not_on_page | 34 | 否 |
| 25 | INTL | anthropic claude-sonnet-5 ev2 | not_on_page | 13 | 否 |
| 26 | INTL | anthropic claude-sonnet-5 ev3 | page_text_diff | 69 | 是/已改写 |
| 27 | INTL | anthropic claude-sonnet-5 ev4 | not_on_page | 13 | 否 |
| 28 | INTL | anthropic claude-sonnet-5 ev5 | not_on_page | 14 | 否 |
| 29 | INTL | anthropic claude-fable-5-1 ev0 | not_on_page | 13 | 否 |
| 30 | INTL | anthropic claude-fable-5-1 ev1 | page_text_diff | 69 | 是/已改写 |
| 31 | INTL | anthropic claude-fable-5-1 ev3 | not_on_page | 15 | 否 |
| 32 | INTL | anthropic claude-fable-5-1 ev4 | not_on_page | 14 | 否 |
| 33 | INTL | anthropic claude-haiku-4-5 ev0 | not_on_page | 13 | 否 |
| 34 | INTL | anthropic claude-haiku-4-5 ev1 | not_on_page | 14 | 否 |
| 35 | INTL | anthropic claude-haiku-4-5 ev2 | not_on_page | 26 | 否 |
| 36 | INTL | anthropic claude-haiku-4-5 ev3 | not_on_page | 26 | 否 |
| 37 | INTL | anthropic claude-haiku-4-5 ev4 | not_on_page | 14 | 否 |
| 38 | INTL | anthropic claude-haiku-4-5 ev5 | not_on_page | 19 | 否 |
| 39 | INTL | google gemini-3.8-flash ev5 | not_on_page | 16 | 否 |
| 40 | INTL | xai grok-4.7 ev1 | not_on_page | 18 | 否 |
| 41 | INTL | xai grok-4.7 ev2 | not_on_page | 19 | 否 |
| 42 | INTL | xai grok-4.7 ev3 | not_on_page | 16 | 否 |
| 43 | INTL | xai grok-4.6 ev0 | not_on_page | 25 | 否 |
| 44 | INTL | xai grok-4.6 ev1 | not_on_page | 16 | 否 |
| 45 | INTL | xai grok-4.6 ev2 | not_on_page | 18 | 否 |
| 46 | INTL | xai grok-4.6 ev3 | not_on_page | 15 | 否 |
| 47 | INTL | xai grok-4.5 ev0 | not_on_page | 25 | 否 |
| 48 | INTL | xai grok-4.5 ev1 | not_on_page | 16 | 否 |
| 49 | INTL | xai grok-4.5 ev2 | not_on_page | 15 | 否 |
| 50 | INTL | xai grok-4.5 ev3 | not_on_page | 17 | 否 |

## 三、这一轮发现的真问题（重要）

1. **国内线可用**：80 条引用里 53 条本来就是页面原文，另 12 条是排版差异，事实有据；15 条已挂 unverified。逐字率从 21/80 提到 53/80。
2. **国际线不能当生产数据源**：73 条引用 = 逐字 27 + 排版差异 7 + 部分命中 3 + 已标未核到 31 + 待判 5；合计 36 条在它自己引用的官方页面上找不到对应文本。典型例子：xAI 的 reasoning 指南页（`docs.x.ai/docs/guides/reasoning`）**没有** `reasoning_effort` 默认值的句子，而 JSON 里写着「if not specified, reasoning_effort defaults to "high". reasoning cannot be disabled.」；Anthropic `platform.claude.com` 的页面缓存只有导航壳（该域在本机区域封锁），引用无法逐字核对。**结论：国际线的引用是上游改写/组织的，不是原文摘录。**
3. **数值真实性存疑但未被证实为假**：这些都挂进了对应模型条目的 `unverified`，合成产物 `model_catalog.json` 里能查到（如 anthropic claude-haiku-4-5 挂 7 条、xai grok-4.6 挂 5 条）。**没有被证实为假的数字没有被直接改掉，也没有被默认当真。**
4. 已按官方原文改掉并留依据的数值（这轮已落）：MiniMax-M3 maxOutput → 524288、MiniMax-M2.7 → 204800（官方 anthropic 兼容页原文「recommended 131072… maximum 524288」）；硅基流动 Kimi-K3 人民币价 → 清空（官方仅美元标价）；GLM-5.3 maxOutput 标 unverified（国内广场 1024K / 国际站 262K 两口径不一致）。

## 四、还差什么

- **国际线要么重做、要么别上**。要做就得用能通的抓取路径逐条重取原文：两段式（先把页面落到 `/tmp/catalog_pages/` 再离线比对）+ `web_extract` + mintlify 站点加 `.md` 取源文；`developers.openai.com` 直抓 403、`docs.x.ai` 直抓超时、`platform.claude.com` 本机区域封锁，都只能走 `web_extract`。
- 缓存价结构化：官方已写明缓存读取价的（MiniMax 国内 0.42 元/M、国际 $0.06/M）尚未进目录字段，`pricing.cachedPerM` 目前仍是 null。
- 6 条 CN 短引用（`| 输入 | 2 | 每百万tokens |` 这种表格片段）判不了，已挂 unverified——要变成可核验的引用得回到 `百炼` 模型信息页抓价格表原文。

生成时间：2026-09-25 04:58:20
