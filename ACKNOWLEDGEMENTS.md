# 致谢 / Acknowledgements

本项目（CodeAgent，桌面 Agent）在设计与部分实现上参照并改编了
[Hermes Agent](https://github.com/NousResearch/hermes-agent)（Nous Research，MIT License）的公开做法。

具体涉及的方面（不限于）：

- **能力层与提示词**：把可独立配置的能力（视觉、图像生成）拆成独立的 provider + 注册表；
  系统提示词里的技能索引、按需读取的渐进披露，以及「工具没装就不提它」的 gating 写法。
- **技能（skills）**：一个技能一个目录 + `SKILL.md`（frontmatter + 正文）的存储形态，
  frontmatter 先校验再落盘、路径不逃逸、批量操作原子回滚、内置技能只读的管理约定。
- **子代理**：一次派一批任务、立刻返回句柄不阻塞主对话、结果合并成一条消息回灌、
  子代理不允许再派子代理的扁平深度，以及子代理的工具禁用清单。
- **权限与审批**：不做权限分级、不引入工作区概念，只按**命令模式**判定危险操作；
  危险命令分成「不可逆」与「影响面大但有救」两档，且正则必须锚到命令位置，
  避免把命令行里的引号内容误判成命令。
- **联网搜索**：keyed（用户自配 key）/ keyless（匿名公共 MCP 端点）两层结构，
  并采用匿名公共 MCP 端点作为零凭证兜底。
- **思考预算**：思考内容与正文共享同一输出预算的处理思路，以及在开思考时抬高输出上限的做法。
- **记忆子系统**：注入预算（按条截断 + 总量截断）的边界设计，以及「包默认值与 schema 默认值
  互相矛盾，必须逐项显式钉死」的启动断言思路。

以上均按本仓库的工具面重新实现（部分为等价实现而非移植），代码与设计文档中保留了逐处说明；
上游项目的具体文件位置与实现细节请以 [Hermes Agent 仓库](https://github.com/NousResearch/hermes-agent) 为准。

感谢 Hermes Agent 与 Nous Research 的开源工作。

---

## Hermes Agent — MIT License

上游项目：https://github.com/NousResearch/hermes-agent

以下为该项目的 MIT 许可证全文，随本项目的分发一并保留：

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```