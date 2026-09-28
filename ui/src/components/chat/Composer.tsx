import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../state/store';
import { baseName, fileUrl, getAgent } from '../../state/live';
import { Icon } from '../Icon';
import { ComposerControls } from './ComposerControls';
import './Composer.css';

const MAX_ROWS = 8;
/** 图片与可提取文档的扩展名清单（扩展名只写在这里，别从正则 source 里切字符串）。 */
import {
  ATTACH_ACCEPT,
  ATTACH_RE,
  isDocPath,
  isImagePath
} from '../../lib/attachmentKinds';

/** Real path of a picked File: preload bridge first (Electron dropped File.path), legacy second. */
function pathOf(file: File): string {
  const agent = getAgent();
  const viaBridge = agent && agent.pathForFile ? agent.pathForFile(file) : '';
  if (viaBridge) return viaBridge;
  const legacy = (file as File & { path?: string }).path;
  return legacy ?? '';
}

export function Composer() {
  const { send, isStreaming, stopStreaming } = useApp();
  const [value, setValue] = useState('');
  const [focused, setFocused] = useState(false);
  const [images, setImages] = useState<string[]>([]);
  /** 拖拽经过时高亮输入框（不给高亮的话用户不知道这里能放）。 */
  const [dragging, setDragging] = useState(false);
  /** 拖进来但格式不认时给一句提示，3 秒后自己消失（不占常驻空间）。 */
  const [attachHint, setAttachHint] = useState<string>('');
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const hintTimer = useRef<number | null>(null);

  const flashHint = (msg: string): void => {
    setAttachHint(msg);
    if (hintTimer.current !== null) window.clearTimeout(hintTimer.current);
    hintTimer.current = window.setTimeout(() => setAttachHint(''), 3000);
  };

  // Auto-grow textarea
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    const lineHeight = 21;
    const max = lineHeight * MAX_ROWS + 20;
    ta.style.height = `${Math.min(ta.scrollHeight, max)}px`;
  }, [value]);

  // 应用自带截图（截图按钮 / Ctrl+Shift+S）在主进程里跑，成品以临时文件路径回来，直接进附件。
  useEffect(() => {
    const agent = getAgent();
    if (!agent || !agent.onSnipAttached) return;
    return agent.onSnipAttached(p => addPaths([p]));
  }, []);

  const addPaths = (paths: string[]): void => {
    const all = paths.filter(p => !!p);
    const ok = all.filter(p => ATTACH_RE.test(p));
    if (all.length > 0 && ok.length === 0) {
      flashHint('只支持文件：图片 / 文档（PDF/Word/Excel/PPT/ODT/RTF/EPUB）/ 纯文本（.log/.txt/.md/.json/.csv 等）');
    }
    if (ok.length === 0) return;
    setImages(prev => {
      const merged = [...prev];
      for (const p of ok) if (!merged.includes(p)) merged.push(p);
      return merged;
    });
  };

  /** Attach images/documents by path; the sidecar stores them and sends them to the model. */
  const addFiles = (files: FileList | File[] | null): void => {
    if (!files) return;
    addPaths(Array.from(files).map(pathOf));
  };

  /**
   * 剪贴板里的位图（截图）没有磁盘路径，附件管线只认路径 —— 先让主进程落成临时文件。
   * 落盘失败就当这次没贴，绝不把坏路径塞进附件（宁可什么都不发生，也不给用户一个坏附件）。
   */
  const attachClipboardImage = async (file: File): Promise<void> => {
    const agent = getAgent();
    if (!agent?.saveTempImage) return;
    const dataUrl = await new Promise<string>(resolve => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result ?? ''));
      fr.onerror = () => resolve('');
      fr.readAsDataURL(file);
    });
    if (!dataUrl) return;
    try {
      const saved = await agent.saveTempImage({ dataUrl, name: file.name || 'screenshot' });
      if (saved) addPaths([saved]);
    } catch {
      /* 静默失败：界面上不会出现半个附件 */
    }
  };

  const submit = () => {
    const text = value.trim();
    if ((!text && images.length === 0) || isStreaming) return;
    // Image-only turns still need an instruction for the model to act on.
    // Document attachments carry extracted text, so a generic instruction suffices.
    const onlyDocs = images.length > 0 && images.every(isDocPath);
    send(text || (onlyDocs ? '看看这些文档' : '看看这些图片'), images.length ? images : undefined);
    setValue('');
    setImages([]);
    requestAnimationFrame(() => taRef.current?.focus());
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    /* 中文/日文输入法组字期间，Enter 是「确认候选词」，绝不能当成发送。
       少了这道闸：用拼音打字按回车会把半成品直接发出去，而组字态下
       Shift+Enter 又插不进换行 —— 用户报「输入框的换行还没实现」就是撞这个。
       isComposing 是标准字段；个别 Chromium 只给 keyCode 229，一并拦。 */
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && !e.shiftKey) {
      /* 发送键由设置决定：默认 Enter 发送；选「Ctrl+Enter 发送」时裸 Enter
         交给浏览器插换行，只有带修饰键才发。 */
      e.preventDefault();
      submit();
    }
  };

  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    // 走 items 而不是 files：Windows 截图工具贴进来的位图在 files 里常常是空的。
    const files = Array.from(e.clipboardData?.items ?? [])
      .filter(it => it.kind === 'file')
      .map(it => it.getAsFile())
      .filter((f): f is File => !!f);
    if (files.length === 0) return;   // 纯文本粘贴：交给浏览器默认行为，别拦
    e.preventDefault();
    const withPath: File[] = [];
    for (const f of files) {
      if (pathOf(f)) withPath.push(f);
      else if (f.type.startsWith('image/')) void attachClipboardImage(f);
    }
    if (withPath.length) addFiles(withPath);
  };

  const canSend = (value.trim().length > 0 || images.length > 0) && !isStreaming;

  return (
    <div className="composer">
      <div className="composer__inner">
        <div
          className={`composer__box ${focused ? 'is-focused' : ''} ${isStreaming ? 'is-busy' : ''} ${dragging ? 'is-dragging' : ''}`}
          onDragOver={e => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={e => {
            e.preventDefault();
            setDragging(false);
            addFiles(e.dataTransfer?.files ?? null);
          }}
        >
          {attachHint && <div className="composer__hint">{attachHint}</div>}
          {dragging && <div className="composer__drop">松手即可作为附件</div>}
          {images.length > 0 && (
            <div className="composer__chips">
              {images.map(p => (
                <span key={p} className="composer__chip" title={p}>
                  {isImagePath(p) ? (
                    <img className="composer__chip-thumb" src={fileUrl(p)} alt="" />
                  ) : (
                    <span className="composer__chip-icon">
                      <Icon.File size={13} />
                    </span>
                  )}
                  <span className="composer__chip-name">{baseName(p)}</span>
                  <button
                    className="composer__chip-x"
                    title="移除"
                    onClick={() => setImages(prev => prev.filter(x => x !== p))}
                  >
                    <Icon.X size={11} />
                  </button>
                </span>
              ))}
            </div>
          )}

          <textarea
            ref={taRef}
            className="composer__input"
            value={value}
            onChange={e => setValue(e.target.value)}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            placeholder={isStreaming ? 'Agent 正在执行…' : '向 Agent 发送任务…'}
            rows={1}
            aria-label="消息输入框"
          />

          <div className="composer__bar">
            <div className="composer__tools">
              <input
                ref={fileRef}
                type="file"
                multiple
                accept={ATTACH_ACCEPT}
                data-testid="file-input"
                style={{ display: 'none' }}
                onChange={e => { addFiles(e.target.files); e.target.value = ''; }}
              />
              <button
                className="composer__icon"
                title="附加文件：图片 / 文档 / 纯文本（也可直接拖入或粘贴）"
                aria-label="附加文件"
                onClick={() => fileRef.current?.click()}
              >
                <Icon.Plus size={15} />
              </button>
              <button
                className="composer__icon"
                title="截图（Ctrl+Shift+S）"
                aria-label="截图"
                onClick={() => { void getAgent()?.snipStart?.(); }}
              >
                <Icon.Shot size={14} />
              </button>
            </div>

            <ComposerControls />

            {/* 回车提示：有内容时才露出来（常驻行保持极简），窄窗口由 CSS 收掉。
               之前这段 JSX 被删过，CSS 里的 kbd 样式还在，等于提示根本不可见 ——
                用户不知道有 Shift+Enter，就以为换行没做。 */}
            {value.trim() ? (
              <span className="composer__keyhint">
                <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行
              </span>
            ) : null}

            <div className="composer__actions">
              {isStreaming ? (
                <button className="composer__stop" onClick={stopStreaming} title="停止生成" aria-label="停止生成">
                  <Icon.Stop size={14} />
                </button>
              ) : (
                <button
                  className="composer__send"
                  onClick={submit}
                  disabled={!canSend}
                  title="发送"
                  aria-label="发送"
                >
                  <Icon.Send size={15} />
                </button>
              )}
            </div>
          </div>
        </div>

        </div>
    </div>
  );
}