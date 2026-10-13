import { useCallback, useEffect, useState } from 'react';
import { getAgent } from '../state/live';
import type { LogDescribe } from '../state/live';
import { Icon } from '../components/Icon';
import {
  dirSubtitle,
  formatInterfaceError,
  formatLogOpenError,
  missingDirHint,
  readInterfaceError,
  sidecarFailure,
  stderrExcerpt,
  subscribeInterfaceError
} from './logPanel';
import './SettingsPage.css';

/**
 * 设置页的「日志」区（2026-10-14）。
 *
 * 「程序有问题都可以找到」：日志目录是**实际生效**的那两个（由主进程解析，界面
 * 不自己拼），能一键用系统文件管理器打开、能复制路径发给别人；侧车不是 ready
 * 的时候，这块把 lastError、重启次数、最近几行侧车输出摆出来 —— 用户不用去
 * 翻控制台，也不用对着空下拉猜为什么。
 */
export function LogsPanel() {
  const [info, setInfo] = useState<LogDescribe | null>(null);
  const [loadError, setLoadError] = useState('');
  const [openError, setOpenError] = useState('');
  const [copied, setCopied] = useState('');
  // 刚打开过的目录：按钮上给一句「已打开」，顺带让「还没生成」的提示随重取消失。
  const [opened, setOpened] = useState('');
  // 模型接口报错：设置页各处（获取列表 / 校验 / 保存）报到这里，与侧车故障放一起看。
  const [interfaceError, setInterfaceError] = useState(() => readInterfaceError());

  const bridge = getAgent()?.logs;

  const refresh = useCallback((): void => {
    if (!bridge) return;
    void bridge.describe()
      .then(d => { setInfo(d); setLoadError(''); })
      .catch(e => setLoadError(e instanceof Error ? e.message : String(e)));
  }, [bridge]);

  useEffect(() => {
    if (!bridge) return;
    refresh();
    // 侧车状态一变就重取：崩溃那一刻 stderr 里已经有 traceback，界面要跟得上。
    const off = getAgent()?.onStatus?.(() => refresh());
    return () => { off?.(); };
  }, [bridge, refresh]);

  useEffect(() => subscribeInterfaceError(() => setInterfaceError(readInterfaceError())), []);

  const open = (kind: 'main' | 'sidecar'): void => {
    if (!bridge) return;
    setOpenError('');
    void bridge.open(kind)
      .then(r => {
        if (!r.ok) {
          setOpenError(formatLogOpenError(r.reason ?? ''));
          return;
        }
        // 打开会顺手把没生成的目录建出来 —— 不重取一次，界面就还在说「这个目录
        // 还没生成」，跟刚刚发生的事自相矛盾（真机实测踩到）。
        setOpened(kind);
        window.setTimeout(() => setOpened(prev => (prev === kind ? '' : prev)), 2500);
        refresh();
      })
      .catch(e => setOpenError(e instanceof Error ? e.message : String(e)));
  };

  const copy = (text: string, key: string): void => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(key);
      window.setTimeout(() => setCopied(prev => (prev === key ? '' : prev)), 1600);
    }).catch(() => setCopied(''));
  };

  const dirs = info?.dirs ?? [];
  const failure = info ? sidecarFailure(info.sidecar) : '';
  const tail = info ? stderrExcerpt(info.stderrTail) : [];
  const iface = formatInterfaceError(interfaceError);

  return (
    <section className="card">
      <div className="card__head">
        <div>
          <div className="card__title">日志</div>
          <div className="card__sub">
            {bridge
              ? dirSubtitle(dirs)
              : '当前环境拿不到日志入口（要用 Electron 启动应用）。'}
          </div>
        </div>
        {bridge && (
          <div className="card__foot card__foot--inline">
            <button className="btn btn--ghost btn--sm" onClick={refresh}>
              <Icon.Refresh size={13} /> 刷新
            </button>
          </div>
        )}
      </div>

      <div className="card__body card__body--flush">
        {loadError && <div className="logdir__hint logdir__hint--bad">读日志目录失败：{loadError}</div>}

        {dirs.map(d => (
          <div className="logdir" key={d.kind}>
            <div className="logdir__head">
              <span className="logdir__label">{d.label}</span>
              <div className="logdir__actions">
                <button className="btn btn--ghost btn--sm" onClick={() => open(d.kind)}>
                  <Icon.Folder size={13} /> {opened === d.kind ? '已打开' : '打开日志目录'}
                </button>
                <button
                  className="btn btn--ghost btn--sm"
                  onClick={() => copy(d.path, d.kind)}
                  title="复制绝对路径，方便直接发给别人"
                >
                  <Icon.Copy size={13} /> {copied === d.kind ? '已复制' : '复制目录路径'}
                </button>
              </div>
            </div>
            {/* 路径就是文本，用户要能看见、能选中 */}
            <div className="logdir__path">{d.path}</div>
            {!d.exists && <div className="logdir__hint">{missingDirHint(d)}</div>}
          </div>
        ))}

        {openError && <div className="logdir__hint logdir__hint--bad">{openError}</div>}

        {(failure || iface) && (
          <div className="logfail">
            {failure && <div className="logfail__line">{failure}</div>}
            {iface && <div className="logfail__line">{iface}</div>}
            {tail.length > 0 && (
              <>
                <div className="logfail__head">
                  侧车最近的输出（{tail.length} 行，完整内容在日志目录里）
                </div>
                <pre className="logfail__body">{tail.join('\n')}</pre>
              </>
            )}
          </div>
        )}
      </div>
    </section>
  );
}