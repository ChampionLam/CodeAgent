/**
 * 侧栏「会话 / 项目」两个分区的纯函数核心（2026-10-12 用户需求）。
 *
 *  - splitSidebarSessions：按 kind 把一次过滤后的列表拆成两半；未知/缺省
 *    kind 一律归「会话」（与 types.ts「缺省按普通会话处理」同一契约）。
 *  - 项目分区要短：默认只列最近 N 条（PROJECT_PREVIEW_N），调用方用
 *    slicePreviewProjects 截断并渲染「查看全部（共 X）」出口。
 *  - 折叠状态存 localStorage（read/writeSidebarSections）：纯界面偏好，
 *    与 thinkingDisplay / appSettings 同一存法，重启后保持。
 *
 * 纯函数、零依赖，方便单测（tests/sidebarSections.test.ts）。
 */
import type { Session } from './types';

export const PROJECT_PREVIEW_N = 5;
const LS_KEY = 'sidebar.sections.v1';

export interface SidebarSectionsState {
  /** 「项目」分区是否折叠（false = 展开）。 */
  projectCollapsed: boolean;
}

export interface SidebarSections {
  chats: Session[];
  projects: Session[];
}

/** 拆列表：chat（含 kind 缺省/未知）进会话，project 进项目。 */
export function splitSidebarSessions(sessions: Session[]): SidebarSections {
  const chats: Session[] = [];
  const projects: Session[] = [];
  for (const s of sessions) {
    if (s.kind === 'project') projects.push(s);
    else chats.push(s);
  }
  return { chats, projects };
}

/** 项目分区截断：列表已按新在前排序，取前 N 条。 */
export function slicePreviewProjects(projects: Session[]): Session[] {
  return projects.slice(0, PROJECT_PREVIEW_N);
}

/** 读折叠状态。读不到/损坏 → 默认展开（false）。 */
export function readSidebarSections(): SidebarSectionsState {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw === null) return { projectCollapsed: false };
    const v = JSON.parse(raw);
    return { projectCollapsed: typeof v?.projectCollapsed === 'boolean' ? v.projectCollapsed : false };
  } catch {
    return { projectCollapsed: false };
  }
}

/** 写折叠状态。失败静默：纯偏好，不值得炸界面。 */
export function writeSidebarSections(v: SidebarSectionsState): void {
  try { localStorage.setItem(LS_KEY, JSON.stringify(v)); } catch { /* ignore */ }
}
