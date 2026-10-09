# DecisionBert int8 模型文件目录

模型文件**不入 git，部署时人工投放到本目录**。完整的投放说明 + 模型卡见：

**`docs/decision-bert-model.md`**（仓库根起算）

速查（详见上表那份文档）：

| 文件 | 必需 | 字节数 |
|---|---|---|
| `decision_bert_v4_int8.onnx` | 必需 | 102,452,905 |
| `calibrator_full_v4_int8.json` | 必需 | 1,335 |
| `tokenizer.json` | 必需 | 268,943 |
| `vocab.txt` | 可选 | 109,540 |

硬规矩：**校准器必须与精度配对——int8 只能用
`calibrator_full_v4_int8.json`**，套 fp32 的温度会静默失准
（level ECE 0.0470 → 0.0553）。

本目录的 `serve.py` 是仓库代码（推理子进程），不是投放产物，别覆盖。
文件没投放时 guard 静默降级为纯正则，进程照常起。

注意：仓库根 `.gitignore` 对本目录是**文件级**排除——只忽略 `*.onnx`、
`*.onnx.data`、`tokenizer.json`、`vocab.txt`、`calibrator*.json` 这些投放
产物；本文件与 `serve.py` 都是仓库代码，正常跟踪（`git check-ignore` 可自查）。
