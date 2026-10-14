/**
 * 流式期间的 mermaid 渲染调度。
 *
 * 背景（2026-10-14 真机复现）：```mermaid 块在模型还在往外吐字时，每个 delta 都会让
 * 组件收到一个新的 code —— 组件原来的写法是「code 一变就先 setSvg(null) 再重画」。
 * 于是同一张图被反复抹掉重画：内容高度塌缩又撑开，用户往上翻看时视口被拽回图里，
 * 光标也留在图上；而渲染失败的那几次，mermaid 还会把错误图（Syntax error 那个炸弹
 * 块）留在页面里，越积越多。
 *
 * 口径：
 *   * 源码必须**稳定** delayMs 才渲染 —— 还在流式就继续等；
 *   * 同一段源码只渲染一次（重复调用直接跳过）；
 *   * 渲染失败也算「这段源码处理过了」，别每帧重试；源码变了再重试。
 *
 * 独立成纯逻辑模块是为了能测：React 组件那层只负责把 render 结果塞进 DOM。
 */
export const RENDER_DEBOUNCE_MS = 280;

export class MermaidScheduler {
  private lastHandled: string | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly render: (code: string) => void,
    private readonly delayMs: number = RENDER_DEBOUNCE_MS,
  ) {}

  /** 源码变了：重置防抖计时器；同一段源码不重复排队。 */
  update(code: string): void {
    if (code === this.lastHandled) return;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      if (code === this.lastHandled) return;
      this.lastHandled = code;
      this.render(code);
    }, this.delayMs);
  }

  /** 组件卸载：取消未触发的渲染。 */
  dispose(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  /** 测试与「源码视图」切换用：当前是否已有排队中的渲染。 */
  get pending(): boolean {
    return this.timer !== null;
  }
}