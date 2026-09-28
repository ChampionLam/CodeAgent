/**
 * 工具的中文文案（界面用）。
 *
 * 为什么放前端而不是让 sidecar 下发：
 * tool 的英文 name/description 是**给模型的契约**（进 schema、进日志、进审批卡片），
 * 不能为了界面好看去改它；中文只是展示层的事，所以放在渲染层，缺条目时回落到英文。
 *
 * 2026-09-26 用户要求：「最好来中文介绍吧」。
 */

export type ToolCopy = { label: string; hint: string };

export const TOOL_COPY_ZH: Record<string, ToolCopy> = {
  read_file: { label: '读取文件', hint: '把文件当文本读出来，可指定行范围' },
  list_dir: { label: '列出目录', hint: '看一个目录里有哪些文件' },
  search_files: { label: '搜索内容', hint: '按正则在工作区里搜文件内容' },
  write_file: { label: '写文件', hint: '新建，或整体覆盖一个文件' },
  edit_file: { label: '修改文件', hint: '把文件里的一段原文替换掉' },
  delete_path: { label: '删除', hint: '删文件，或删一个空目录' },
  run_shell: { label: '执行命令', hint: '在工作区里跑 shell 命令' },
  read_skill: { label: '读技能', hint: '按名字打开一个技能的完整步骤' },
  list_skills: { label: '列出技能', hint: '看当前装了哪些技能' },
  skill_manage: { label: '管理技能', hint: '新建、修改、删除技能' },
  save_rule: { label: '记下规则', hint: '把要求存成长期生效的规则' },
  remove_rule: { label: '删除规则', hint: '撤销一条不再需要的规则' },
  search_conversations: { label: '搜历史会话', hint: '在自己过去的会话里找内容' },
  delegate: { label: '派子代理', hint: '并发最多 3 个子任务，结果合并回来' },
  image_generate: { label: '生成图片', hint: '用文生图模型出图' },
  web_fetch: { label: '抓取网页', hint: '取一个网页的正文' },
  web_search: { label: '搜索网页', hint: '联网搜索，先看标题和摘要' },
};

/** 取中文文案；没有条目就返回 null，由调用方回落到英文原文。 */
export function toolCopyOf(name: string): ToolCopy | null {
  return TOOL_COPY_ZH[name] || null;
}