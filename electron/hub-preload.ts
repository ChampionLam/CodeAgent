/**
 * hub 窗体的 preload。窗体是独立页面（不是 React 应用），所以这里只暴露
 * hub 需要的那点东西：提问、截图、附件路径、隐藏、主题。
 * 主窗那套 window.agent 不在这里重复暴露 —— hub 不做会话/模型/工具管理。
 */
import { contextBridge, ipcRenderer, webUtils } from "electron";

type EventPayload = { chatId?: string; event?: string; data?: Record<string, unknown> };

const hub = {
  ask(text: string, images: string[]): Promise<{ chatId?: string; model?: string }> {
    return ipcRenderer.invoke("hub:ask", { text, images }) as Promise<{ chatId?: string; model?: string }>;
  },
  cancel(): Promise<unknown> {
    return ipcRenderer.invoke("hub:cancel");
  },
  snip(): Promise<boolean> {
    return ipcRenderer.invoke("hub:snip") as Promise<boolean>;
  },
  saveTempImage(dataUrl: string, name: string): Promise<string> {
    return ipcRenderer.invoke("agent:saveTempImage", { dataUrl, name }) as Promise<string>;
  },
  hide(): void {
    ipcRenderer.send("hub:hide");
  },
  resize(height: number): void {
    ipcRenderer.send("hub:resize", height);
  },
  copy(text: string): void {
    ipcRenderer.send("hub:copy", text);
  },
  openInMain(): void {
    ipcRenderer.send("hub:openInMain");
  },
  pathOf(file: File): string {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return "";
    }
  },
  theme(): Promise<string> {
    return ipcRenderer.invoke("hub:theme") as Promise<string>;
  },
  onEvent(cb: (payload: EventPayload) => void): void {
    ipcRenderer.on("agent:event", (_e, payload: EventPayload) => cb(payload));
  },
  onSnip(cb: (file: string) => void): void {
    ipcRenderer.on("snip:attached", (_e, file: string) => cb(file));
  },
  onShown(cb: () => void): void {
    ipcRenderer.on("hub:shown", () => cb());
  },
};

contextBridge.exposeInMainWorld("hub", hub);
