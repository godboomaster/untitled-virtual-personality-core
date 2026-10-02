import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { useI18n, useMockData } from '../i18n';
import { api } from '../api';
import type { HomeOverview } from '../api';
import { useApiOnline } from '../apiData';
import { useInbox } from '../inboxStore';
import { usePersonaAvatars } from '../avatarStore';
import { playFlip } from '../flip';
import type { FlipRects } from '../flip';

/* Страница всех чатов — вход в раздел «Чат»: ни один чат ещё не открыт,
   поэтому здесь нет ни отметки «прочитано», ни присутствия (оно глушит фон
   персоны), и боты персон не поднимаются. Клик по карточке открывает чат:
   карточки уезжают в список персон слева (FLIP, см. flip.ts). */

// Превью и состояние персон — из той же сводки, что и главная (GET /api/home)
const POLL_MS = 20000;

interface Props {
  onOpen: (personaId: string) => void;
  // Прямоугольники строк списка персон, если пришли из открытого чата
  flipFrom: FlipRects | null;
}

export default function ChatOverview({ onOpen, flipFrom }: Props) {
  const { lang, t } = useI18n();
  const { personas, chatByPersona } = useMockData();
  const apiOnline = useApiOnline();
  const avatars = usePersonaAvatars();
  const { messages, unread, generating, lastTs, controlMode } = useInbox();
  const [home, setHome] = useState<HomeOverview | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const flipping = useRef(flipFrom !== null);

  useEffect(() => {
    if (!apiOnline) {
      setHome(null);
      return;
    }
    let alive = true;
    const load = () =>
      api
        .getHome()
        .then((o) => alive && setHome(o))
        .catch(() => {});
    void load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [apiOnline]);

  // Возврат из чата: карточки выезжают из строк списка на свои места.
  // Один раз на монтирование (StrictMode повторяет эффект — см. ChatRoom)
  const flipPlayed = useRef(false);
  useLayoutEffect(() => {
    if (flipPlayed.current) return;
    flipPlayed.current = true;
    void playFlip(rootRef.current, flipFrom);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const locale = lang === 'ru' ? 'ru-RU' : 'en-US';
  // Сегодня — время, вчера — «вчера», раньше — дата
  const when = (ts: number | null | undefined): string => {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
    if (ts >= startOfToday) return d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
    if (ts >= startOfToday - 86400) return t('chat.yesterday');
    return d.toLocaleDateString(locale, { day: '2-digit', month: '2-digit' });
  };

  // Последняя известная реплика: свежее из фоновых сообщений сессии и
  // сводки сервера; без бэкенда — из моков
  const preview = (id: string): { text: string; you: boolean; ts: number | null; time?: string } | null => {
    const inboxLast = messages[id]?.[messages[id].length - 1];
    const srv = home?.personas[id]?.last_message ?? null;
    if (inboxLast && (!srv || inboxLast.ts > srv.ts)) return { text: inboxLast.text, you: false, ts: inboxLast.ts };
    if (srv) return { text: srv.text, you: srv.role === 'user', ts: srv.ts };
    if (!apiOnline) {
      const list = chatByPersona[id];
      const m = list?.[list.length - 1];
      if (m) return { text: m.text, you: m.role === 'user', ts: m.ts ?? null, time: m.time };
    }
    return null;
  };

  // Порядок как в списке персон чата: самая свежая переписка — первой
  const sorted = [...personas].sort((a, b) => (lastTs[b.id] ?? 0) - (lastTs[a.id] ?? 0));
  const totalUnread = sorted.reduce((sum, p) => sum + (unread[p.id] ?? 0), 0);

  return (
    <div className="chat-overview" ref={rootRef}>
      <div className="chat-overview-head">
        <div>
          <div className="home-eyebrow">{t('chat.overviewEyebrow')}</div>
          <h2 className="chat-overview-title">{t('chat.overviewTitle')}</h2>
          <p className="chat-overview-lead">{t('chat.overviewLead')}</p>
        </div>
        <div className="chat-overview-stats">
          <span>
            {t('chat.overviewChats')} <b>{String(sorted.length).padStart(2, '0')}</b>
          </span>
          <span className={totalUnread > 0 ? 'chat-overview-stats--hot' : undefined}>
            {t('chat.overviewUnread')} <b>{String(totalUnread).padStart(2, '0')}</b>
          </span>
        </div>
      </div>

      <div className="chat-overview-grid">
        {sorted.map((p, i) => {
          const pv = preview(p.id);
          const state = home?.personas[p.id]?.state;
          const activity = state ? [state.pastime, state.location].filter(Boolean).join(' · ') : '';
          const n = unread[p.id] ?? 0;
          const typing = generating[p.id] === true;
          return (
            <button
              key={p.id}
              type="button"
              data-flip-id={p.id}
              className={`chat-card${flipping.current ? '' : ' stagger-item'}${n > 0 ? ' chat-card--unread' : ''}`}
              style={{
                animationDelay: `${i * 40}ms`,
                ...(p.color ? ({ '--persona-color': p.color } as CSSProperties) : {}),
              }}
              onClick={() => onOpen(p.id)}
            >
              <div className="chat-card-head">
                <div className="avatar avatar--large">
                  {avatars[p.id] ? <img src={avatars[p.id]} alt={p.name} /> : p.name.charAt(0)}
                </div>
                <div className="chat-card-id">
                  <div className="chat-card-name">
                    {p.name}
                    {n > 0 && (
                      <span className="unread-dot" title={t('chat.unread')}>
                        {n}
                      </span>
                    )}
                  </div>
                  <div className="chat-card-status">
                    <span className="status-led" />
                    {typing ? t('status.typing') : t(`status.${p.status}`)}
                    {state?.mood && <span className="chat-card-mood"> · {state.mood}</span>}
                  </div>
                </div>
                <div className="chat-card-time">{pv?.time ?? when(pv?.ts ?? lastTs[p.id])}</div>
              </div>
              <div className={`chat-card-preview${pv ? '' : ' chat-card-preview--empty'}`}>
                {typing ? (
                  <span>
                    {t('status.typing')}
                    <span className="typing-dots" aria-hidden="true">
                      <span>.</span>
                      <span>.</span>
                      <span>.</span>
                    </span>
                  </span>
                ) : pv ? (
                  <>
                    {pv.you && <span className="chat-card-you">{t('chat.previewYou')}</span>}
                    {pv.text}
                  </>
                ) : (
                  t('chat.previewEmpty')
                )}
              </div>
              {(activity || controlMode[p.id]) && (
                <div className="chat-card-foot">
                  {controlMode[p.id] && <span className="chat-card-badge">{t('cc.title')}</span>}
                  {activity && <span className="chat-card-activity">{activity}</span>}
                </div>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
