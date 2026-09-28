/**
 * 对话里的文件附件（2026-09-27）。
 *
 * 后端只给元数据（path/name/size/kind），这里负责：人读的大小、图标名、
 * 以及「打开 / 在文件夹中显示 / 复制路径」三个动作 —— 前两个走主进程 IPC。
 */
import type { MessageArtifact } from '../types';

export function humanSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 三分口径：图片缩略图、文档/代码/文本图标、其余通用图标。 */
export function iconKind(kind: string): 'image' | 'doc' | 'code' | 'text' | 'other' {
  return (['image', 'doc', 'code', 'text'] as const).includes(kind as 'image')
    ? (kind as 'image')
    : 'other';
}

export const isImageArtifact = (a: MessageArtifact): boolean => a.kind === 'image';

type FileBridge = {
  openPath?: (path: string) => Promise<{ ok: boolean; reason?: string }>;
  showItemInFolder?: (path: string) => Promise<{ ok: boolean; reason?: string }>;
};

function bridge(): FileBridge | null {
  const w = window as unknown as { agent?: FileBridge };
  return w.agent ?? null;
}

export async function openArtifact(path: string): Promise<boolean> {
  const b = bridge();
  if (!b?.openPath) return false;
  try {
    return (await b.openPath(path)).ok;
  } catch {
    return false;
  }
}

export async function revealArtifact(path: string): Promise<boolean> {
  const b = bridge();
  if (!b?.showItemInFolder) return false;
  try {
    return (await b.showItemInFolder(path)).ok;
  } catch {
    return false;
  }
}

export async function copyArtifactPath(path: string): Promise<boolean> {
  try {
    await navigator.clipboard?.writeText(path);
    return true;
  } catch {
    return false;
  }
}
