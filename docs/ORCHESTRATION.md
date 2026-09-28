# 对话编排层（system prompt 组装）

设计依据：项目内部设计文档（不在本仓）。本文只写**实现落点**和**与设计文档的差异**，口径以代码为准。

## 一句话

每一轮对话，sidecar 现拼一次系统提示词，形状固定为：

```
base 角色
+ ## 可用 skills   （每个 skill 的 name + description + when_to_use）
+ ## 可用工具      （每个工具的 name + 一行说明）
+ [历史 messages]  （经上下文机制压缩后）
```

## 代码落点

| 文件 | 职责 |
|---|---|
| `python/prompt_build.py` | 纯组装：把工作区、时间、skills、tools 拼成提示词字符串。无 I/O、不调模型、不做策略 |
| `python/skills_registry.py` | 扫两个目录、解析 `SKILL.md` frontmatter、提供正文 |
| `python/tools.py` | `read_skill` / `list_skills` 两个工具（描述英文，与仓库其它工具一致） |
| `python/sidecar.py` | `_system_prompt_for()`：每轮调用一次组装，作为 `LoopContext.system_prompt` 注入 |

## 三个刻意的决定

1. **提示词归属移到后端。** 之前提示词是渲染进程里两行写死的字符串（`electron/renderer.ts` 的 `SYSTEM_PROMPT`，仍会随参数传过来但**已被忽略**）。现在由 sidecar 统一组装，前后端不会各说一套。前端不用改、不用重新编译。
2. **工具 schema 不进提示词。** 定稿写的是「name+desc+schema」，实现只放 name + 一行说明。原因：当前 17 个工具的完整 JSON schema 每一轮都要随 OpenAI `tools` 参数发一次，再在提示词里重复一遍等于每轮白烧两三 k token，而模型拿不到额外信息。**这是与设计文档的唯一一处偏差**，若要求对齐请说，改回来只是加几行。
3. **skills 只扫一次（启动时），改完重启生效。** 定稿 v1 里写的是 chokidar 监听，Python 侧实现成启动扫描 + 内存缓存；热重载没做，`docs/SKILL-AUTHORING.md` 里如实写了「重启生效」。

## 调试开关

设环境变量 `DESK_AGENT_LOG_PROMPT=1`，每轮把拼好的提示词原文打进 sidecar 日志（含字符数、skill 数、工具数）。提示词里不含任何密钥，但会含工作区路径，所以默认关闭。

## 验收

- 单测：`tests/test_prompt_build.py`（19 条，含 skills 段的**逐字节**格式、无 skill 时整段不出现、降级不崩）
- `tests/test_skills_registry.py`（详见该文件）
- 权限：`read_skill` / `list_skills` 是读操作，不会触发审批。判定全部在 `python/guard.py`（只按**命令模式**判危险命令，没有分级、没有工作区概念），`python/permissions.py` 只是转发薄壳。
- 真机行为验证记录在项目内部文档里（不在本仓）

## 安全护栏

- skill 正文是从用户目录读来的内容，属于**可信度未知的输入**，只作为工具结果喂给模型，不走任何"读取即执行"的路径。
- skill 目录下的任何脚本文件都不被代码引用（`skills_registry.py` 只读 `SKILL.md` 这一个文件，有静态单测断言）。要执行程序只能走 `run_shell`，而 `run_shell` 会过 `python/guard.py` 的命令判定：命中危险模式才在会话里出审批卡片。
- **命令审批是防手滑的 UX 护栏，不是安全边界。** `guard.py` 是黑名单正则（命令名必须落在命令位上），必然存在绕过面：绝对路径（`/bin/rm`）、`$IFS` 拆词、变量间接（`X=rm; $X`）、`xargs rm`、`find . -delete`、`python -c "shutil.rmtree(...)"` 都不拦。真正的安全边界在**网络层** `python/web/egress.py`（私网拒绝 + DNS 钉死 + 跨站重定向重验 + 字节上限）与**密钥治理**（key 只走 `.env`，界面不回读），命令层不承诺这些。