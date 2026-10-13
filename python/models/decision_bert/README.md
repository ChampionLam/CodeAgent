# DecisionBert v6 fp16 模型文件目录

模型文件**不入 git，部署时人工投放到本目录**（或由 install.ps1 从魔搭
拉取）。完整的投放说明 + 模型卡见：

**`docs/decision-bert-model.md`**（仓库根起算）

速查（详见上表那份文档）：

| 文件 | 必需 | 字节数 |
|---|---|---|
| `decision_bert.onnx` | 必需 | 321,505 |
| `decision_bert.onnx.data` | 必需 | 205,880,832 |
| `calibrator.json` | 必需 | 1,113 |
| `tokenizer.json` | 必需 | 736,019 |

硬规矩：

- **`.onnx` + `.onnx.data` 是 ONNX 外部数据格式：同目录、都不改名**，
  校验要覆盖 `.data`（`.onnx` 只是壳）。
- **三件必须成套**：tokenizer 词表（22,765 行）必须与模型 embedding
  行数一致 —— 不一致 serve.py 启动即报错（fail loud）。配 v4 旧词表
  （21,128）不会报错、只会静默掉分，这条断言就是防它的。
- 校准器用 fp16 这份 `calibrator.json`（与 fp32 共用）；
  `calibrator_full_*_int8.json` 是给 int8 的，**不要用**。

本目录的 `serve.py` 是仓库代码（推理子进程），不是投放产物，别覆盖。
文件没投放时 guard 静默降级为纯正则，进程照常起。

注意：仓库根 `.gitignore` 对本目录是**文件级**排除——只忽略 `*.onnx`、
`*.onnx.data`、`tokenizer.json`、`vocab.txt`、`calibrator*.json` 这些投放
产物；本文件与 `serve.py` 都是仓库代码，正常跟踪（`git check-ignore`
可自查）。
