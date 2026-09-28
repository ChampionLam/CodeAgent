# Skills 与 Tools 功能（2026-09-25 用户钦定：参考 Hermes）

用户原话：「hermes的skills能力很强,参考他」「tools和skills功能刚好也加上吧,也是这次能力的需要」。

## 一、先说清 Hermes 那边强在哪（读其公开仓库源码确认，不是印象）

| 维度 | Hermes 的做法 | 代码位置（其公开仓库内路径） |
|---|---|---|
| 存储 | 一个技能 = 一个目录 + `SKILL.md`（YAML frontmatter + 正文），可带 `references/` `scripts/` `templates/` `assets/` | `tools/skills_hub.py` |
| 工具 | **一个** `skill_manage` + 操作数组（create/edit/patch/delete/write_file/remove_file），不是一堆小工具 | `tools/skill_manager_tool.py` |
| 读 | `skills_list`（只给名字+描述）+ `skill_view`（全文，可带 `file_path` 取附档） | `tools/skills_tool.py` |
| 渐进披露 | 提示词里**只放技能索引**，正文按需读 —— 这是它省钱的关键 | `agent/system_prompt.py` |
| 校验 | frontmatter 必须 `---` 开头+闭合+能解析、name 与目录同名、description 长度上限 1024 | `skill_manager_tool.py:600` / `skill_linter.py:119` |
| 治理 | 内置/受保护/hub 安装的技能**拒写**；有 provenance（来源）、ledger（使用记录）、usage 统计 | `_background_review_write_guard` |
| 原子性 | 批量操作全成功才落盘，失败整体回滚 | 同上 |

**我们的差距**：`skills_registry.py` 只能读（`list_skills` / `get_skill` / `read_body`），**没有回写回路** —— 模型无法把学到的东西沉淀成技能。这是这次要补的第一块。

## 二、第二块：tools 能关

Hermes 的核心原则（其公开仓库 `AGENTS.md` 原话）：

> Every model tool we add is sent on every API call, so the bar for a new *core* tool is high.

**每个工具的描述都会在每一次 API 调用里发出去**。所以「注册了但用不上」的工具是纯成本：钱 + 模型注意力 + 误用概率。
我们要给的：**工具面板里能关**，关掉的工具不进 schema、不出现在提示词的「能力与边界」里（那一节本来就是按实际注册的工具现场生成的，自动跟着走）。

## 三、后端契约（先定死，界面按这个写）

### skills
```
skills.list              -> {skills:[{name, description, whenToUse, version, author, source:"builtin"|"user", dir, sha256}], skipped:[{path, reason}]}
skills.read {name}       -> {name, body, frontmatter:{...}}
skills.save {operations:[...]}   -> 与 skill_manage 工具同一套操作（create/patch/write_file/remove_file/delete），原子
skills.delete {name}     -> {removed:name}
```
规则（与工具侧完全一致，不许两套口径）：
- 只能写**用户根**（`~/.desktop-agent/skills`）；内置技能拒写，错误要说清「这是内置技能」
- frontmatter 校验、name 与目录同名、rel_path 不逃逸、批量原子回滚

### tools
```
tools.list {includeDisabled?} -> {tools:[{name, description, enabled, source}], levels, canAlwaysAllow}
tools.setEnabled {name, enabled} -> {name, enabled}
```
- 关掉的工具：不进 `tools.list` 的默认返回、不进 schema、不进提示词能力节
- 配置落 `config.json` 的 `capabilities.disabledTools: []`（**别写 .env** —— 那是凭证专用的）

## 四、界面（照设计：深色 `#0f0f10` + 靛蓝 `#6366f1`，极简，不堆控件）

设置页现在是单 tab「模型」；再加两个，**但保持极简**：

### 技能页
- 列表：名字 + 描述（描述截断）+ 来源徽章（内置 / 我的）；内置那条给「复制到我的」而不是编辑
- 点开：正文只读展示（Markdown 渲染）；「我的」才有编辑
- 新建：「新建技能」→ 表单只三个字段（名字、描述、正文），描述下面给一行灰色提示：**「开头要是一个自包含的触发句，索引里只显示开头部分」**（Hermes 的约定，别让用户写出一句没信息量的描述）
- 删除：确认一次，讲清不可恢复
- 内置技能：显示「内置，不可修改」，旁边「复制到我的」一键复制成用户技能

### 工具页
- 列表：工具名（人话）+ 一句话说明 + 开关
- 开关下面一行灰色说明：**「关掉的工具不会发给模型（省 token，也少误用）」**
- 危险操作相关的工具（run_shell / delete_path）**不给关**（它们在 guard 里拦危险命令，关了就没了执行能力），显示为「必需」且开关置灰

## 五、验收

1. 后端：`skills.save` 能建/改/删；内置拒写；路径逃逸被拦；批量失败回滚；关掉的工具真的不进 schema（用 `tools.schemas()` 断言）
2. 界面：三个 tab（模型 / 技能 / 工具）都能用；`npx tsc --noEmit` + `npm run build` 干净
3. 真机：在 Windows 上建一个技能 → 下一轮对话里模型能看到它（技能索引）+「技能工具」可用；关掉一个工具 → 模型那边就看不到它了
4. 口径守卫：`grep 工作区` 在 UI 里仍然 0 命中；界面不许出现弹窗审批（审批卡在消息流里）