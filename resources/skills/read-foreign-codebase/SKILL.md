---
name: read-foreign-codebase
description: 快速摸清陌生代码库——目录结构、入口、依赖、约定，先看后说
when_to_use: 用户丢来一个不熟悉的仓库/项目，要求"看看这是什么/怎么跑/在哪改"时
version: "1.0"
author: CodeAgent built-in
---

# 陌生代码库速读

一句话：**结构 → 入口 → 依赖 → 约定**，四步看完再开口。凭目录名猜架构必错。

## 步骤

1. **目录结构**：`run_shell` 看根目录与关键子目录（src/python/app/lib/tests/docs）。README 先读——它是作者自己写的地图。
2. **技术栈与依赖**：读 manifest（package.json / pyproject.toml / requirements.txt / go.mod / Cargo.toml），记下运行时版本和关键库。**没在 manifest 里见过的库，不假设可用。**
3. **入口与运行方式**：找 main/入口文件、scripts/启动脚本、Makefile、CI 配置（.github/workflows 最能反映"作者怎么验证这个项目"）。给出一条能跑起来的命令。
4. **约定**：找 AGENTS.md / CLAUDE.md / CONTRIBUTING / .editorconfig；看测试怎么组织、日志怎么打。这些决定你后续改代码的姿势。
5. **产出摸底摘要**：这是什么项目、技术栈、入口在哪、怎么跑/怎么测、约定是什么——五项各一两行。落盘成文件，回复给路径加要点。

## 出岔子的时候

- 仓库缺 README/manifest：如实说"看不到 X"，从代码本身（import 图、目录名）推断并标注这是推断。
- 用户问的具体位置：用 `search_files` 定位到文件和行号再答，不许凭"应该在某处"作答。
