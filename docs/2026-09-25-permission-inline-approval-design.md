# 权限模型重做 + 内联审批（2026-09-25 用户钦定）

用户原话：
- 「把权限分级去掉，没有什么工作区的概念。权限参照同类 agent 的通行做法（看命令模式，不看路径）」
- 「权限审批的不要再出个什么弹窗，很烦，应该作为一条消息在会话层与用户交互，默认命令信息是折叠的，用户可以点看详细命令，用户只需要在会话里面点击一下允许就可以，还可以拒绝什么的」

## 1. 删掉的东西（不是改，是删）

- `python/permissions.py`：`Level` 枚举、`TOOL_BASE_LEVEL`、`CAN_ALWAYS_ALLOW`、`is_inside_workspace` / `_abs_target` 这套**按路径**判级的逻辑、`Rule.scope="workspace"`。
- 「工作区」这个概念本身：不再在系统提示词里写「工作区根目录」（`prompt_build.environment_section`）、不再有 `workspaceRoot` 请求参数、不再有 `capabilities.workspaceRoot` 配置、顶栏不再显示 workspace 路径。
- UI：`PermissionModal`（弹窗）、顶栏的「权限 N 条 / 模拟审批」、设置页的权限规则页。
- 数据库那套 `permission_rules`（L1 路径规则 / L2 命令前缀规则）不再参与判定；表可留但**判定不再读它**。

## 2. 新的判定（看命令模式，不看路径）

参照口径（源自 Hermes Agent 的 approval 设计，本仓在 `python/guard.py` 中按自家工具面重新实现）：agent 可以在任意目录干活，被拦的是**会造成安全影响的操作**。两组正则：

- `HARDLINE_PATTERNS` —— 不可逆、没有补救路径：`rm -rf /`、`rm -rf` 系统目录或 `$HOME`、`mkfs`、`dd ... of=/dev/sdX`、`> /dev/sdX`、fork bomb、`kill -1`、shutdown/reboot/halt/poweroff、`init 0/6`、`systemctl poweroff`。命中即**必须**用户点头，不给记住。
- `DANGEROUS_PATTERNS` —— 影响面大但有救：装包、杀进程、docker 走掉数据卷一步、`git push --force` 之类。命中则问一次。

其余一律**直接放行**，包括在任意目录读写文件、跑普通 shell、联网（联网的安全边界在 `web/egress.py`：私网拒绝 + DNS 钉死 + 跨站重定向重验 + 字节上限，不靠弹窗）。

Windows 侧要补的类目（现有 danger.py 只有 POSIX 口径）：
`format`、`diskpart ... clean`、`del /f /s /q C:\*`、`rd /s /q C:\`、`vssadmin delete shadows`、`bcdedit`、`reg delete HKLM`、`cipher /w`、`shutdown /r|/s`、`Stop-Computer`/`Restart-Computer`、`taskkill /F /IM *`。

正则要**锚到命令位置**（行首、`;`/`&&`/`||`/`|` 之后、`$(`/反引号内、`sudo`/`env`/`exec` 之后），否则 `git commit -m "don't rm -rf /"` 这种把命令当参数的会误伤（同类 agent 踩过这个坑，见 Hermes Agent issue #93392）。

## 3. 审批的交互形态（去掉弹窗）

审批不再是一个全局 modal 状态，而是**会话里的一条消息**：

```
┌ 需要你确认 ─────────────────────────────┐
│ 执行命令   ▸ 展开详情                    │   ← 命令默认折叠
│ 原因：递归删除系统目录                   │
│ [ 允许 ]  [ 拒绝 ]                      │
└────────────────────────────────────────┘
```

- 这条消息落在**它所属的那条助手回合里**（就和工具卡同一条消息流），不是浮层、不遮对话、不需要返回。
- 命令/参数原文默认折叠（用户原话「默认命令信息是折叠的，用户可以点看详细命令」），点开才见全文。
- 用户点「允许」→ 工具继续执行，这条卡就地变成「已允许」并展示输出；点「拒绝」→ 该工具调用标「已拒绝」，把拒绝结果回给模型让它改路线（不要卡死）。
- 不做「总是允许」：用户没要，而且这套口径里危险命令本来就不该被一次点头永久放行。**要记规则的话只能是命令前缀，但那属于以后的事，本轮不做。**
- 审批期间对话输入不禁用（可以继续打字），但该会话的生成处于等待状态 —— 这条要跟「subagent 不阻塞主线程」那件事分开，别混做。

## 4. 数据面

- sidecar 侧：`approval.request` 事件不再驱动 modal；改成 produce 一条 `message.kind = "approval"` 的记录（或复用工具卡的 `status: "awaiting"` 态 + 附加 `approvalId`）。
- UI 侧：`pendingPermission` 这个全局状态删掉；审批卡从消息流渲染；点击后走 `approval.respond {id, allow}`。
- 审计：允许/拒绝都要落审计（谁批的、原命令、命中哪条规则），审计不是给用户看的，是事后追责用的。

## 5. 完成标准

1. 弹窗组件、权限规则页、等级标签、工作区参数/文案全部消失；`grep -rn "L0\|L1\|L2\|L3\|workspace" python/ desktop-agent-ui/src` 只剩与「上下文压缩 L0/L1/L2」相关的命中（那是另一件事，别误删）。
2. 危险命令进会话卡片，可展开、可允许、可拒绝；点拒绝后模型能继续对话、不卡死、不死循环问权限。
3. 普通操作（任意目录读写、普通 shell、联网）**一次审批都不弹**。
4. 测试：`tests/test_permissions.py`、`test_approval_rules.py` 按新口径重写；新增钉死「危险模式必须被拦、普通命令必须放行」的正反用例；933 基线不许红。Windows 类目要有专门用例（本机是 Linux，但真机是 Windows）。
5. 真机验证：在 Windows 上跑一条普通命令（不许弹）+ 一条命中危险模式的命令（必须弹在会话里）。