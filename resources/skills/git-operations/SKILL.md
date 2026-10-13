---
name: git-operations
description: Git 操作规范——动手前先看状态，只提交相关改动，不重写历史，不代推代合并
when_to_use: 用户要求提交、看改动历史、建分支、对比版本，或排查"git 报错了"时
version: "1.0"
author: CodeAgent built-in
---

# Git 操作规范

一句话：**动手前先 `git status` / `git log`，动手后报告实际结果**。Git 的错误大多来自"以为在干净的分支上"。

## 步骤

1. **先看现场**：`git status`、`git branch --show-current`、`git log --oneline -5`。有未暂存的陌生改动就先报给用户，不要把它卷进自己的提交。
2. **只提交相关改动**。用明确的路径 `git add <files>`，不用 `git add -A` 一锅端；提交信息写清做了什么和为什么（一行主题 + 必要的正文）。
3. **不越权**：commit 前问一句除非用户已明确要求；**push、merge 到主干、rebase、reset --hard、force push 一律等用户点头**，改写已推送的历史绝不主动做。
4. **排查 git 报错**：先读完整报错（`git status` 的 hint、拒绝原因），常见类先查——未配置身份、行尾/权限、远端凭据、分支落后需要先 pull。修复后重跑命令确认。
5. **对比与考古**：看"什么时候引入的"用 `git log --follow <file>` / `git blame`，先拿到证据再下"这行是 X 改的"的结论。

## 出岔子的时候

- detached HEAD / 冲突：说明现状，给出选项（建分支保留 / abort 回退），让用户选，不要自作主张 reset。
- 用户没说要提交：只改工作区，改完报"未提交，要提交说一声"。
