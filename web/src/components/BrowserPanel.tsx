import { useEffect, useRef, useState } from 'react';
import { ApiError, streamControlView } from '../api';
import { useI18n } from '../i18n';

/* Окно браузера агента рядом с чатом (режим управления): живые кадры
   вкладки, на которой работает агент, адрес и заголовок. Только просмотр —
   управлять агентом можно словами в чате. Поток кадров открыт, пока панель
   видна и вкладка браузера-клиента активна; скрытая вкладка его закрывает. */

type ViewStatus = 'connecting' | 'live' | 'no_browser' | 'no_tab' | 'error' | 'off';

interface Props {
  personaId: string;
  personaName: string;
  onClose: () => void;
}

export default function BrowserPanel({ personaId, personaName, onClose }: Props) {
  const { t } = useI18n();
  const imgRef = useRef<HTMLImageElement>(null);
  const [status, setStatus] = useState<ViewStatus>('connecting');
  const [hasFrame, setHasFrame] = useState(false);
  const [meta, setMeta] = useState({ url: '', title: '', private: false });

  useEffect(() => {
    let stopped = false;
    let ctrl: AbortController | null = null;
    let timer: number | undefined;
    let retry = 2000;
    const hidden = () => document.visibilityState === 'hidden';
    const connect = () => {
      if (stopped || hidden()) return;
      const c = new AbortController();
      ctrl = c;
      setStatus((s) => (s === 'live' ? s : 'connecting'));
      streamControlView(
        personaId,
        (e) => {
          if (e.frame) {
            // Кадр — прямо в <img>, без перерисовки компонента на каждый кадр
            if (imgRef.current) imgRef.current.src = `data:image/jpeg;base64,${e.frame}`;
            setHasFrame(true);
            setStatus('live');
            retry = 2000;
            const next = { url: e.url ?? '', title: e.title ?? '', private: !!e.private };
            setMeta((prev) => (prev.url === next.url && prev.title === next.title && prev.private === next.private ? prev : next));
          } else if (e.status) {
            setStatus(e.status);
          }
        },
        c.signal,
      )
        .catch((err) => {
          if (stopped || c.signal.aborted) return;
          // 409 — режим управления уже выключен: панель скоро исчезнет сама
          setStatus(err instanceof ApiError && err.status === 409 ? 'off' : 'error');
          retry = Math.min(retry * 2, 10000);
        })
        .finally(() => {
          if (!stopped && !c.signal.aborted && !hidden()) timer = window.setTimeout(connect, retry);
        });
    };
    const onVisibility = () => {
      if (hidden()) {
        ctrl?.abort();
        window.clearTimeout(timer);
      } else if (!ctrl || ctrl.signal.aborted) {
        connect();
      }
    };
    connect();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stopped = true;
      ctrl?.abort();
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [personaId]);

  const overlay = status === 'live' ? (hasFrame ? null : t('bw.st.waiting')) : t(`bw.st.${status}`);
  return (
    <aside className="chat-browser" aria-label={t('bw.label')}>
      <div className="bw-top">
        <i />
        <i />
        <i />
        <div className="bw-tab" title={meta.title}>
          <span className="bw-tab-icon">◐</span>
          <span className="bw-tab-title">{meta.title || t('bw.tabEmpty')}</span>
        </div>
        <button type="button" className="bw-close" title={t('bw.close')} onClick={onClose}>
          ✕
        </button>
      </div>
      <div className="bw-addr">
        <span className="bw-nav" aria-hidden="true">⟵ ⟶ ⟳</span>
        <div className="bw-url" title={meta.url}>{meta.url}</div>
        {meta.private && <span className="bw-private" title={t('bw.privateTitle')}>{t('bw.private')}</span>}
        <span className="bw-agent">
          <span className="status-led" />
          {t('bw.agent', { name: personaName })}
        </span>
      </div>
      <div className={`bw-view${hasFrame ? '' : ' bw-view--empty'}${overlay && hasFrame ? ' bw-view--stale' : ''}`}>
        <img ref={imgRef} alt={meta.title || t('bw.label')} />
        {overlay && (
          <div className="bw-overlay">
            <span className="bw-overlay-logo">◉</span>
            {overlay}
          </div>
        )}
      </div>
    </aside>
  );
}
