# DecisionBert v4 int8 —— 模型文件投放说明 + 模型卡

guard 第二层（模型提示层）的推理文件**不入 git 仓库，部署时人工投放到
`python/models/decision_bert/`**。本文就是照着放的说明，兼作模型卡。

> **模型只做提示层，不是硬闸门，最终决定权在用户手里。** 模型层任何故障
> （文件没投放、依赖缺失、推理超时、子进程死亡）都 fail-open 降级为纯正则
> 判定，不影响 agent 可用性。

- 更新：2026-10-01
- 数据出处：模型训练侧的交付说明（仓外文档，非本仓内容：交付文件清单、
  输入输出契约、决策流与阈值、三精度对比、局限）与
  `calibrator_full_v4_int8.json`（内含 `paired_model`、各头温度、
  `judgment_change_vs_fp32_pct`）。本文所有字节数与 sha256 均为对盘上真实
  文件的实测（2026-10-01，`stat` + `sha256sum`），非转述；与那份交付说明有
  出入的数字见文末附录，一律以本仓实测为准。
- 仓库内代码参照：`python/guard_model.py`（宿主 client）、
  `python/guard_model_proc.py`（子进程管理）、
  `python/models/decision_bert/serve.py`（推理子进程）、
  `python/requirements-guard.txt`（运行依赖）。
  `python/models/decision_bert/README.md` 是指向本文的指路 stub。

## 一、要投放哪些文件

目标目录：仓库根下的 `python/models/decision_bert/`（平铺，不要建子目录）。
`python/guard_model.py` 的 `default_model_path()` / `default_calibrator_path()`
/ `default_tokenizer_path()` 写死了这些文件名，放错名字或错子目录等于没放。

| 文件 | 相对路径（仓库根起算） | 必需 | 字节数（实测） | sha256（实测） |
|---|---|---|---|---|
| int8 ONNX 模型 | `python/models/decision_bert/decision_bert_v4_int8.onnx` | 必需 | 102,452,905（约 97.7 MiB） | `a5c552829d65a8bc060fd45f838a2ed0da1846b88fe1527ed62921d32d6e9ccf` |
| int8 专属校准器 | `python/models/decision_bert/calibrator_full_v4_int8.json` | 必需 | 1,335 | `1a1c529230c964c653168b2dfd2aafb64141ddc2cbdd9230fe1f9ccb34b8401f` |
| BERT 中文 tokenizer | `python/models/decision_bert/tokenizer.json` | 必需 | 268,943 | `55c5edcfb8936895e019b2d8f97dcb547734f25b29f95009155f4c8c15649aae` |
| 词表（备用口径） | `python/models/decision_bert/vocab.txt` | 可选 | 109,540 | `45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c` |

说明：

- `tokenizer.json` 是 `tokenizers` 库直接加载的格式，词表已内嵌，代码只用它。
  `vocab.txt` 是同底座（bert-base-chinese）的原始词表，留作
  `transformers.AutoTokenizer` 口径复验或人工核对，不投放不影响运行。
- 目录里的 `serve.py` 是仓库代码（推理子进程脚本），**不在模型文件投放清单里**，
  别用模型交付包里的任何内容覆盖它。
- 校准器虽只有 1,335 字节，也按投放产物处理、不入 git：它必须与 ONNX 成对
  管理（见第二节），单独入库容易和模型文件配错版本。

### 投放与校验

```bash
cd <desk-agent 仓库根>
mkdir -p python/models/decision_bert

# 从模型交付处拷贝（字节数 / sha256 见上表，拷完必须逐项核对）
cp <模型交付目录>/decision_bert_v4_int8.onnx   python/models/decision_bert/
cp <模型交付目录>/calibrator_full_v4_int8.json python/models/decision_bert/
cp <模型交付目录>/tokenizer.json               python/models/decision_bert/
cp <模型交付目录>/vocab.txt                    python/models/decision_bert/   # 可选

# 校验：任一项与上表不符就别上线，删掉重拷
cd python/models/decision_bert
sha256sum decision_bert_v4_int8.onnx calibrator_full_v4_int8.json tokenizer.json vocab.txt
stat -c '%n %s' decision_bert_v4_int8.onnx calibrator_full_v4_int8.json tokenizer.json vocab.txt
```

运行依赖见 `python/requirements-guard.txt`（`onnxruntime>=1.27.0`、
`tokenizers>=0.23.1`、`numpy>=1.26`），部署时随 venv 一起装；没装或文件没
  投放都只是模型层静默降级，进程照常起。

### git 约定（现状与坑）

模型大文件不入 git（公开仓不许进 97.7 MiB 的 ONNX）。仓库根 `.gitignore`
（2026-10-01 起）用**文件级排除**：只忽略大体积模型产物
（`*.onnx` / `*.onnx.data` / `tokenizer.json` / `vocab.txt` /
`calibrator*.json`），`serve.py` 与 `README.md` 这类纯代码/文档**可以正常
跟踪**（两文件当前已在 untracked 列表里，待首次 `git add`）。早一版曾用
目录级 `python/models/` 排除再配 `!` 例外 —— git 无法 re-include 被排除
目录下的文件，例外全部失效（实测 `git check-ignore` 命中目录规则、
`git add` 被拒），现已改成文件级。兜底规则 `python/models/*` +
`!python/models/decision_bert/` 只为将来新增其他模型目录时挡住整目录
（对 decision_bert 目录本身无影响，文件级规则已放行其代码文件）。

路径可用环境变量覆盖（见 `python/guard_model.py`）：`DESK_GUARD_MODEL`、
`DESK_GUARD_CALIBRATOR`、`DESK_GUARD_TOKENIZER`。

## 二、硬规矩：校准器必须与精度配对

**int8 只能用 `calibrator_full_v4_int8.json`。** 套 fp32 那份
（`calibrator_full_v4.json`）不会报错，只会让阈值**静默失准**——实测 level
ECE 从 0.0470 恶化到 0.0553。

原因：温度和模型精度绑定。int8 的 logit 分布更平，温度是单独拟合的（两份
JSON 实测读数）：

| 头 | int8 温度（本校准器） | fp32 温度（别用在这） |
|---|---|---|
| noul | 1.18 | 1.349 |
| level | 1.19 | 1.398 |
| cost | 1.16 | 1.344 |

`calibrator_full_v4_int8.json` 的 `provenance.paired_model` 明确写着
`decision_bert_v4_int8.onnx`（温度在留出验证集 val_c.jsonl 前 1200 条上网格
搜索拟合，测试集零参与）——投放时两份文件必须来自同一次交付，校验完
sha256 再配对。配错的症状是「不报错、阈值刻度整体漂移」，和 raw/cal 刻度
混用是同一类坑，出了问题很难从日志里看出来。

## 三、模型卡

### 3.1 底座与结构

- 底座：`bert-base-chinese`
- 三个头（ONNX 三个输出）：
  - `noul`：弹卡二分类，输出 `[batch]` 的 logit，推理时 sigmoid
  - `level`：危险等级 L0-L3，输出 `[batch, 4]` 的 logit，softmax
  - `cost`：撤销代价 低/中/高，输出 `[batch, 3]` 的 logit，softmax
- 判断 cost 头维度：**3 档是当前 v4**；遇到 5 档的一律作废（v1 遗留产物，
  档数不兼容，加载即出错）。

### 3.2 输入契约（必须逐字一致）

```
[工具] {tool} [目录] {cwd} [命令] {command}
```

例：`[工具] run_shell [目录] /home/user [命令] rm -rf /tmp/build`

- `tool` 固定 `run_shell`、`cwd` 固定 `/home/user`（训练时的常量
  `DEFAULT_TOOL` / `DEFAULT_CWD`，对接时照抄，别自作主张改）
- tokenizer：BERT 中文，`max_length=128`，`truncation=True`，padding 到
  batch 内最长或固定 128
- 曾踩过的坑：训练用 `/home/user`、评测写死 `/tmp`，一个词的差异就让指标
  暴跌——输入串漂移只静默掉分，**不报错**。

### 3.3 输出与校准顺序

ONNX 输出的是 **logit，不是概率**。校准（RLCD 温度缩放）必须接，且顺序
**先除温度、再激活**：

```python
p_noul  = sigmoid(noul_logit  / T["noul"])
p_level = softmax(level_logit / T["level"])
p_cost  = softmax(cost_logit  / T["cost"])
```

温度从 `calibrator_full_v4_int8.json` 的 `heads.<name>.temperature` 读。
raw 和校准后是两套刻度：同一阈值 0.5 在两者上对应不同操作点（实测 raw 0.4
的漏拦 14.7% 约对应校准后 0.35），不接校准等于阈值白调。

### 3.4 决策流与阈值

**极性（2026-10-01 核定，不许再反）**：p 是**弹卡概率，高 p = 危险**。
源码依据：上游训练定义 `noul = (action != allow)`（action ∈ {ask, block}
记 1），标签映射 ACTION_HINT = {0:"放行", 1:"弹卡"}；实测 ls -la p=0.023、rm -rf / p=0.944、
find . -delete p=0.970。**绝不许把高 p 读成「模型很确定安全」** —— 本文
旧版曾写 `p >= 0.85 → allow`，方向完全反了：照那套，`find . -delete`
（p=0.970）会被当最安全的命令直接放行。

推荐决策流（模型只是第②层）：

```
工具调用
  ↓ ① 正则硬闸（guard.py 的 hardline/dangerous）：已知高危形态直接拦
  ↓ ② 模型 noul（校准后弹卡概率 p，高 p = 危险）：
       p >= 0.85        → 弹卡（模型高置信危险，UI 标「模型很确定」）
       0.35 <= p < 0.85 → 弹卡（模型没把握，安全优先，照常出审批卡）
       p < 0.35         → 放行 + 记入未知池
  ↓ ③ 未知池定期人工/大模型仲裁 → 回流训练集 → 重训
```

- **两个阈值之上都是弹卡**，区别只在 UI 标注：p >= 0.85 不是放行线，
  只是高置信标注线；p < 0.35 才放行并记入未知池。
- 低置信区（p<0.35）占 test 的 16.2%、区内错误率 57.7%（高置信区只有
  5.1%）——确实标记了「模型没把握」的区域，值得回流。
- `level` / `cost` **只做展示**（如「判定为高危」「撤销代价：高」），不做
  自动决策。
- 阈值缺省 0.85（`confident`，高置信标注线）/ 0.35（`ask`，弹卡门槛），
  可用 `config.json` 的 guard 段 `confident` / `ask` 覆盖（兼容旧键
  `model_confident_threshold` / `model_ask_threshold`，见
  `python/guard_model.py` 的 `_read_thresholds`）。
- 阈值扫描参考（v4 基线口径：test 5200 条、校准后概率；int8 下漏拦再高
  3-11pp，见 4.1）：

| 阈值 | 漏拦率 | 误拦率 | F1 |
|---|---|---|---|
| 0.60 | 0.256 | 0.110 | 0.724 |
| 0.50 | 0.185 | 0.153 | 0.718 |
| 0.40 | 0.138 | 0.185 | 0.709 |
| 0.30 | 0.099 | 0.222 | 0.689 |
| 0.20 | 0.070 | 0.269 | 0.652 |

安全优先建议 0.30-0.40：漏拦是事故，误拦只是一张卡。设完阈值不是一劳永逸，
流量分布变化后要重扫。

### 3.5 运行形态与降级

- 推理住独立子进程（`serve.py`），空闲 10 分钟自动 reap 把内存还给 OS：
  int8 常驻 RSS 约 304 MB，冷启动约 0.36 s，单条推理 p50 约 116 ms
  （CodeAgent 真实部署环境实测：onnxruntime + tokenizers、无 GPU；上游
  上游交付说明的三方对比里 int8 为 61.3 ms，口径不同，见附录 4）。
- 单条推理超时默认 5 s；超时 / 子进程死亡 / spawn 两次失败 → 模型层标记
  不可用，guard 降级纯正则（fail-open），**绝不把异常抛进判定路径**。
- 文件没投放（目录里只有 serve.py 和 README）同样静默降级，进程照常起。

## 四、能力边界（选型前必读，如实不美化）

### 4.1 int8 相对 fp32 的代价——这是选 int8 时明知的取舍

**int8 相对 fp32 判决改变 5-9%，漏拦率上涨 3-11pp，闸门整体变松。**
恰好是安全侧最不能接受的方向。

- 判决改变：level 9.0%、cost 5.125%（校准器 JSON `judgment_change_vs_fp32_pct`
  口径；上游交付说明按 600 条计为 level 54 条 = 9.0%、cost 27 条 = 4.5%）、
  noul 32/600 ≈ 5.3%。
- 漏拦涨幅（同阈值逐格对比，600 条、raw 概率口径）：

| 阈值 | fp32 漏拦/误拦 | int8 漏拦/误拦 | int8 漏拦涨幅 |
|---|---|---|---|
| 0.70 | 0.328 / 0.098 | 0.437 / 0.067 | +10.9pp |
| 0.60 | 0.246 / 0.144 | 0.328 / 0.115 | +8.2pp |
| 0.50 | 0.208 / 0.192 | 0.246 / 0.161 | +3.8pp |
| 0.40 | 0.153 / 0.223 | 0.180 / 0.221 | +2.7pp |
| 0.30 | 0.131 / 0.261 | 0.148 / 0.266 | +1.7pp |

  「3-11pp」对应 0.50-0.70 档；0.30/0.40 档收窄到约 2-3pp，但方向不变：
  漏拦涨、误拦降。
- 单点偏差很大：noul 概率 max|Δ| 0.558、level 居中 logit max|Δ| 4.97。
  校准后 ECE 与 fp32 相当（level/cost 0.0397 / 0.0420 vs 0.0395 / 0.0448）
  ——**别拿 ECE 判断量化能不能用**：校准修的是刻度，动不了 argmax，这是
  天花板。
- 上游交付说明的选型建议是「CPU 部署用 fp32，int8 不用」。本仓选 int8 是
  **明知取舍**：单文件 97.7 MiB（fp32 是 388 MiB）、常驻内存约 304 MB
  （fp32 约 644 MB，还要与 bge-small-zh 嵌入模型共存）、冷启动与连续吞吐
  都是三档里最快。若日后要换回 fp32：模型换 `decision_bert_v4.onnx`
  （406,954,081 字节，另附 `.data` 文件），**校准器必须同步换成
  `calibrator_full_v4.json`**，并改默认路径或环境变量。

### 4.2 模型本身的边界（v4 基线，test 5200 条）

- 弹卡 acc **0.818**（90% 验收线没到）；阈值 0.5 时漏拦 **0.185**、误拦
  **0.181**；漏拦随阈值 9.9%-18.5%。
- level acc 0.719、±1 档 0.944、macroF1 0.613；cost acc 0.794、±1 档
  0.984、macroF1 0.686；**L3 召回 0.622**（L1 召回只有 0.373，长尾档位差，
  少数类样本占比低是根本瓶颈）。
- **cwd 基本不起作用**：训练时它是常量，实测换 cwd 只引起 1.8-6.6pp 波动。
  「同一命令在不同目录危险度不同」当前版本做不到。
- **删什么不分轻重**：`rm -rf /app/1.txt` 和 `rm -rf /etc/profile` 判定接近
  （都是 L2）。模型抓的是 `rm -rf` 模式，不是删除目标；按路径分级得靠正则。
- 输入只有单条命令，没有上下文（前序命令、会话意图），能力上限可能就在这里。

## 五、定位与红线

- 模型是**提示层**，不是硬闸门：它只影响「要不要问用户」，已知高危形态的
  拦截由正则层（`guard.py` 的 hardline/dangerous）兜底，审批与放行由用户点。
- guard 整体（正则 + 模型）是防手滑的 UX 护栏，不是安全边界：正则有绕过面
  （绝对路径 `/bin/rm`、`$IFS` 拆词、变量间接执行等实测不拦），模型 acc 只有
  0.818。网络边界与密钥治理不在这层。
- **最终决定权在人手里**：模型永远不能成为自动放行/自动拒绝危险操作的唯一依据。

## 附：与上游交付说明的数字出入（以本仓实测为准）

1. **误拦率（阈值 0.5）**：上游交付说明的精度表写 0.181、其阈值表同档写
   0.153，两处内部不一致。本文正文采用精度表口径（0.185 / 0.181），阈值表
   原样收录供调阈值用。
2. **int8 cost 判决改变**：校准器 JSON 写 5.125%，上游交付说明按 600 条计
   27 条 = 4.5%，两源不一致（level 的 9.0% 两源一致）。本文并列表述为
   「5-9%」区间。
3. **漏拦涨幅 3-11pp**：上游交付说明的叙述口径；其阈值表在 0.30/0.40 档实测
   +1.7 / +2.7pp。本文按表注明。
4. **ECE 0.0470**（配对警告用）与 0.0397 是不同评测集口径，不是矛盾，引用时
   别混。同理延迟 61.3 ms（上游三方对比）与 116 ms（本仓部署环境）是不同
   环境 / 线程配置的实测。
5. **校准器大小**：上游交付说明写 1.1 KB，实测 int8 份 1,335 字节（约
   1.3 KB；fp32 份 1,111 字节才是 1.1 KB）。
6. **弹卡 acc**：上游交付说明写「84%」，其精度表为 0.818（≈82%），以
   0.818 为准。
7. **选型建议**：上游交付说明建议 CPU 用 fp32、int8 不用；本仓选 int8 是明知
   取舍（体积/内存/吞吐，见 4.1），代价已在显眼位置写明。
