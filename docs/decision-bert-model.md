# DecisionBert v6 fp16 —— 模型文件投放说明 + 模型卡

guard 第二层（模型提示层）的推理文件**不入 git 仓库，部署时人工投放到
`python/models/decision_bert/`**（或走 install.ps1 从魔搭拉取，见下）。
本文就是照着放的说明，兼作模型卡。

> **模型只做提示层，不是硬闸门，最终决定权在用户手里。** 模型层任何故障
> （文件没投放、依赖缺失、推理超时、子进程死亡、词表不匹配）都 fail-open
> 降级为纯正则判定，不影响 agent 可用性。

- 更新：2026-10-13（v4 int8 → v6 fp16 切换）
- 数据出处：模型训练侧的交付包（2026-10-02，README + MANIFEST.sha256，
  含 1963 条 Windows 命令的实机测试）与训练侧评测报告第 5 节。本文所有
  字节数与 sha256 均为对盘上真实文件的实测（`stat` + `sha256sum`），
  非转述；与交付包有出入的数字一律以本仓实测为准。
- 仓库内代码参照：`python/guard_model.py`（宿主 client）、
  `python/guard_model_proc.py`（子进程管理）、
  `python/models/decision_bert/serve.py`（推理子进程）、
  `python/requirements-guard.txt`（运行依赖）。
  `python/models/decision_bert/README.md` 是指向本文的指路 stub。

## 一、要投放哪些文件

目标目录：仓库根下的 `python/models/decision_bert/`（平铺，不要建子目录）。
`python/guard_model.py` 的 `default_model_path()` /
`default_calibrator_path()` / `default_tokenizer_path()` 写死了这些文件名，
放错名字或错子目录等于没放。

| 文件 | 相对路径（仓库根起算） | 必需 | 字节数（实测） | sha256（实测） |
|---|---|---|---|---|
| fp16 ONNX 模型 | `python/models/decision_bert/decision_bert.onnx` | 必需 | 321,505 | `0757d5bec4321c8c13a42c0afef9c1b47c0ef2e78b564791ff3196b81864a89b` |
| fp16 权重（外部数据） | `python/models/decision_bert/decision_bert.onnx.data` | 必需 | 205,880,832（196.3 MiB） | `ed14e73b19adeaff711f1d3a20bffa2a681546bc1ef6e1c7ff34d61a645cc629` |
| 校准器 | `python/models/decision_bert/calibrator.json` | 必需 | 1,113 | `9809736bfde493c557837f5ec13142acba03d9a8b80ec9464fa787b60367597c` |
| v6 扩表 tokenizer | `python/models/decision_bert/tokenizer.json` | 必需 | 736,019 | `015d0ec928f26e28530d6c9399bdfd727ca39c04d34bc1b6a8ab10662a5208d7` |

说明：

- **`.onnx` 与 `.onnx.data` 是 ONNX 外部数据格式**：两个文件必须同目录、
  **都不能改名**（`.onnx` 内部按名字引用 `.data`，改名即加载失败）。
  投放/校验/迁移时必须覆盖 `.data`，只看 `.onnx` 等于只校了一层皮。
- `.data` 是 196.3 MiB，**超过 GitHub 100 MiB 单文件硬线** → 模型只能走
  魔搭（ModelScope）或 Releases 分发，永不进 git 仓库文件。
  分发仓（公开 · apache-2.0，2026-10-13 落地）：**魔搭
  `ChampionLam/commands-guard-fp16`**，免 token 直链
  `https://www.modelscope.cn/api/v1/models/ChampionLam/commands-guard-fp16/repo?Revision=master&FilePath=<文件名>`，
  仓内 `SHA256SUMS` 是校验真相源（下载侧不要再硬编码第二份）。
  install.ps1 的 guard-model 阶段（v28+）会从魔搭拉取这四件并按上表
  sha256 校验。
- `tokenizer.json` 是 `tokenizers` 库直接加载的格式，词表已内嵌
  （22,765 行，见第二节），代码只用它。fp16 交付包不再带 vocab.txt，
  没有「可选口径」文件。
- 目录里的 `serve.py` 是仓库代码（推理子进程脚本），**不在模型文件投放
  清单里**，别用模型交付包里的任何内容覆盖它。
- 校准器虽只有 1,113 字节，也按投放产物处理、不入 git：它必须与模型
  成对管理（见第二节），单独入库容易和模型文件配错版本。

### 投放与校验

```bash
cd <desk-agent 仓库根>
mkdir -p python/models/decision_bert

# 从交付包拷贝（字节数 / sha256 见上表，拷完必须逐项核对）
cp <交付包>/decision_bert.onnx      python/models/decision_bert/
cp <交付包>/decision_bert.onnx.data python/models/decision_bert/
cp <交付包>/calibrator.json         python/models/decision_bert/
cp <交付包>/tokenizer.json          python/models/decision_bert/

# 校验：任一项与上表不符就别上线，删掉重拷
cd python/models/decision_bert
sha256sum decision_bert.onnx decision_bert.onnx.data calibrator.json tokenizer.json
stat -c '%n %s' decision_bert.onnx decision_bert.onnx.data calibrator.json tokenizer.json
```

投放后 30 秒自检（走 guard 真实链路）：

```bash
python - <<'EOF'
import sys; sys.path.insert(0, '<desk-agent>/python')
import guard_model as g
gm = g.GuardModel()
print('warmup:', gm.warmup() is not None, '| reason:', gm.degradation_reason)
for c in ('ls -la', 'rm -rf /'):
    print(c, gm.predict(c))
gm.shutdown()
EOF
```

预期：`ls -la` → p≈0.013（安全）；`rm -rf /` → p≈0.977（该弹卡）。
两个数都漂了 = 三件套不成套（或混进了旧版文件）。

### 升级路径（已有 v4 int8 的旧装）

v4 三件（`decision_bert_v4_int8.onnx` / `calibrator_full_v4_int8.json` /
旧 `tokenizer.json`）属过期件：直接删掉（本地留备份随意），换成上表四件，
**不要两份并存** —— 并存正是 v4 时代混淆的根源。int8 / fp32 版本也不与
fp16 混放同仓（理由见 4.1 与交付报告）。全新安装不带模型：install.ps1
会从魔搭拉取；旧装升级时安装器按期望表核对、不符会告警并给出自愈指引。

### git 约定（现状与坑）

模型大文件不入 git。仓库根 `.gitignore` 用**文件级排除**：只忽略大体积
模型产物（`*.onnx` / `*.onnx.data` / `tokenizer.json` / `vocab.txt` /
`calibrator*.json`），`serve.py` 与 `README.md` 这类纯代码/文档**可以正常
跟踪**。早一版曾用目录级 `python/models/` 排除再配 `!` 例外 —— git
无法 re-include 被排除目录下的文件，例外全部失效（实测），现已改成
文件级。兜底规则只为将来新增其他模型目录时挡住整目录。

路径可用环境变量覆盖（见 `python/guard_model.py`）：`DESK_GUARD_MODEL`、
`DESK_GUARD_CALIBRATOR`、`DESK_GUARD_TOKENIZER`。

## 二、硬规矩：三件必须成套（词表 + 校准器 + 模型）

### 2.1 词表：v6 扩了表，配旧词表 = 静默掉分（已用断言堵死）

v6 训练时 `resize_vocab: true`：词表从 bert-base-chinese 原版 21,128 扩到
**22,765 行**（+1,637，其中 1,628 行按词片均值初始化）。模型 embedding
也是 22,765 行。

坑：v6 模型配 v4 旧 tokenizer（21,128）**不报错** —— ORT 的 Gather 只在
token id ≥ 22,765 时才报越界，而旧词表的所有 id 都在 21,128 以内，
差的那 1,637 行模型侧永远查不到，**分词与权重错位，p 静默漂移**
（实测：`ls -la` 的 p 从 0.013 漂到 0.134，方向还不固定；`rm -rf /`
碰巧不变，更没参照感）。

对策（2026-10-13 起）：`serve.py` 启动时断言 **tokenizer 词表行数 ==
模型 embedding 行数**，不一致直接 `load failed` 拒绝起 —— fail loud。
宿主侧 `predict` 返回 None 并打 warn 日志（带双方行数），guard 降级
纯正则。这层断言不依赖 onnx 包：用必然越界的探测 id 触发 ORT Gather
错误、从错误信息里解析行数（探测失败也按 fail-loud 处理，宁可拒绝起）。
回归测试：`tests/test_guard_model_live.py` 的
`VocabMismatchFailLoudTests`。

### 2.2 校准器：fp16 与 fp32 共用这份，int8 那份不要用

当前校准器 `calibrator.json`（即训练侧 `calibrator_full_v6.json`）是为
fp32/fp16 拟合的温度（fp16 的 logit 分布与 fp32 逐位一致，见 4.1）：

| 头 | 温度 |
|---|---|
| noul | 1.2958 |
| level | 1.3284 |
| cost | 1.4461 |

`runs/` 里另有 `calibrator_full_v6_int8.json`（温度 1.14 / 1.18 / 1.27，
为 int8 单独拟合）—— **那是给 int8 的，fp16 不要用**：套错了不报错，
只会让阈值刻度静默失准。int8 若哪天真要上，必须连模型带校准器一起换，
单独换任何一件都是坑。

## 三、模型卡

### 3.1 底座与结构

- 底座：`bert-base-chinese`（v6 起词表扩到 22,765）
- 三个头（ONNX 三个输出）：
  - `noul`：弹卡二分类，输出 `[batch]` 的 logit，推理时 sigmoid
  - `level`：危险等级 L0-L3，输出 `[batch, 4]` 的 logit，softmax
  - `cost`：撤销代价 低/中/高，输出 `[batch, 3]` 的 logit，softmax
- 训练口径（report_full_v6.json）：60,075 条训练 / 6,163 条验证，
  Windows 命令语料；resize_vocab=true、new_emb_init=pieces。

### 3.2 输入契约（必须逐字一致）

```
[工具] {tool} [目录] {cwd} [命令] {command}
```

例：`[工具] run_shell [目录] /home/user [命令] rm -rf /tmp/build`

- `tool` 固定 `run_shell`、`cwd` 固定 `/home/user`（训练时的常量
  `DEFAULT_TOOL` / `DEFAULT_CWD`，对接时照抄，别自作主张改）
- tokenizer：BERT 中文（v6 扩表版），`max_length=128`，`truncation=True`
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

温度从 `calibrator.json` 的 `heads.<name>.temperature` 读。raw 和校准后
是两套刻度：同一阈值 0.5 在两者上对应不同操作点，不接校准等于阈值白调。

### 3.4 决策流与阈值

**极性（2026-10-01 核定，不许再反）**：p 是**弹卡概率，高 p = 危险**。
实测 `ls -la` p=0.013、`rm -rf /` p=0.977、`find . -delete` p=0.971。
**绝不许把高 p 读成「模型很确定安全」**。

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
- `level` / `cost` **只做展示**，不做自动决策。
- 阈值缺省 0.85（`confident`）/ 0.35（`ask`），可用 `config.json` 的
  guard 段覆盖（见 `python/guard_model.py` 的 `_read_thresholds`）。
- v6 的阈值扫描没重跑（沿用 v4 基线口径做参考）；流量分布变化后要重扫。

### 3.5 运行形态与降级

- 推理住独立子进程（`serve.py`），空闲 10 分钟自动 reap 把内存还给 OS。
  fp16 实测（交付包 1963 条 Windows 命令）：冷启动 1.63 s、热路径单条
  p50 86.3 ms / p95 188.1 ms、常驻 RSS 431 MB、降级 0 次。
- 单条推理超时默认 15 s（盖住最慢冷启动再留余量）；超时 / 子进程死亡 /
  spawn 两次失败 → 模型层标记不可用，guard 降级纯正则（fail-open），
  **绝不把异常抛进判定路径**。
- 文件没投放（目录里只有 serve.py 和 README）同样静默降级，进程照常起。

## 四、能力边界（选型前必读，如实不美化）

### 4.1 为什么是 fp16（2026-10-13 用户拍板）

三档实测（`v6_quant_and_agent_report.md` §5，963 条）：

| 精度 | 判决 vs fp32 | 常驻 RSS | 冷启动 | 备注 |
|---|---|---|---|---|
| fp32 | —（基准） | 670.9 MB | — | 体积 388 MiB |
| **fp16** | **逐位一致（0~1 条改变）** | **431.1 MB** | 1.63 s | 交付选择：省 240 MB、判决不劣化 |
| int8 | level 6.25% / cost 4.125% 改变 | — | — | **不推荐**：判决改变方向是放松闸门 |

- fp16 与 fp32 判决几乎逐位一致，量化代价可忽略；比 fp32 省 240 MB
  常驻内存（还要与 bge-small-zh 嵌入模型共存）。
- int8 的判决改变（level 6.25%、cost 4.125%）方向是**漏拦变多、闸门
  变松** —— 安全侧最不能接受的方向，故不推荐、不分发。若哪天要换
  int8：模型、校准器（int8 专属温度那份）必须一起换，路径与投放说明
  也要整体重写。
- 实机基线（1963 条 Windows 命令，fp16 走 guard 真实链路）：
  漏拦 10.16% / 误拦 26.98% / acc 80.58%。

### 4.2 模型本身的边界（v6 基线口径）

- 弹卡 acc 0.806（实机 1963 条）；训练侧验证集 noul_acc 0.824。
- **cwd 基本不起作用**：训练时它是常量。「同一命令在不同目录危险度
  不同」当前版本做不到。
- **删什么不分轻重**：模型抓的是 `rm -rf` 模式，不是删除目标；按路径
  分级得靠正则。
- 输入只有单条命令，没有上下文（前序命令、会话意图），能力上限可能
  就在这里。
- v6 词表扩的 1,637 个新词在实机 Windows 语料上验证过；非 Windows
  语料的覆盖没有专项数据。

## 五、定位与红线

- 模型是**提示层**，不是硬闸门：已知高危形态的拦截由正则层
  （`guard.py` 的 hardline/dangerous）兜底，审批与放行由用户点。
- guard 整体（正则 + 模型）是防手滑的 UX 护栏，不是安全边界：正则有
  绕过面（绝对路径 `/bin/rm`、`$IFS` 拆词、变量间接执行等实测不拦），
  模型 acc 只有 0.8 左右。网络边界与密钥治理不在这层。
- **最终决定权在人手里**：模型永远不能成为自动放行/自动拒绝危险操作的
  唯一依据。
