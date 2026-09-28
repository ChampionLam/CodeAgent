import { Children, isValidElement, useRef, useState, type ReactNode } from 'react';
import { Icon } from '../Icon';
import './CodeBlock.css';

interface Props {
  children?: ReactNode;
}

function pickLang(children: ReactNode): string {
  const arr = Children.toArray(children);
  const first = arr[0];
  if (isValidElement(first)) {
    const cls = (first.props as { className?: string }).className ?? '';
    const m = /language-([\w+-]+)/.exec(cls);
    if (m) return m[1];
  }
  return 'text';
}

const LANG_LABEL: Record<string, string> = {
  ts: 'TypeScript',
  tsx: 'TSX',
  typescript: 'TypeScript',
  js: 'JavaScript',
  jsx: 'JSX',
  javascript: 'JavaScript',
  py: 'Python',
  python: 'Python',
  bash: 'Bash',
  sh: 'Shell',
  shell: 'Shell',
  json: 'JSON',
  yaml: 'YAML',
  yml: 'YAML',
  sql: 'SQL',
  css: 'CSS',
  html: 'HTML',
  text: '纯文本'
};

/**
 * Code block chrome: language badge, copy button, line-numbered scroll area.
 * Highlighting is applied upstream by rehype-highlight (hljs spans).
 */
export function CodeBlock({ children }: Props) {
  const preRef = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);

  const lang = pickLang(children);
  const label = LANG_LABEL[lang] ?? lang.toUpperCase();

  const rawText = () => preRef.current?.textContent ?? '';

  const onCopy = async () => {
    const text = rawText();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard API can be blocked in insecure contexts — fall back silently.
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  };

  const lineCount = rawText().split('\n').length;

  return (
    <div className="codeblock">
      <div className="codeblock__head">
        <span className="codeblock__lang">
          <span className="codeblock__lang-dot" />
          {label}
        </span>
        <span className="codeblock__meta">
          <span className="codeblock__lines">{lineCount} 行</span>
          <button
            className={`codeblock__copy ${copied ? 'is-copied' : ''}`}
            onClick={onCopy}
            aria-label="复制代码"
          >
            {copied ? <Icon.Check size={12} /> : <Icon.Copy size={12} />}
            <span>{copied ? '已复制' : '复制'}</span>
          </button>
        </span>
      </div>
      <pre className="codeblock__pre" ref={preRef} data-lang={lang}>
        {children}
      </pre>
    </div>
  );
}