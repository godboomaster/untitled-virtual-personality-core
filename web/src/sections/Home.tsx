import { useEffect, useMemo, useState } from 'react';
import type { Section } from '../App';
import { useI18n, useMockData } from '../i18n';
import type { InitiativeOutcome } from '../mockData';
import { requestPersonaCreate } from '../personaCreateStore';
import { requestChatPersona } from '../chatNavStore';
import { useInbox } from '../inboxStore';
import PersonaAvatar from '../components/PersonaAvatar';
import CalendarWidget from '../components/CalendarWidget';
import NotesWidget from '../components/NotesWidget';
import PageTabs from '../components/PageTabs';
import { api, isNativeApp } from '../api';
import type { HomeOverview } from '../api';
import { useApiOnline, useCoreHealth } from '../apiData';
import { localLast } from '../offline/lastMessage';
import { isStandalone } from '../standalone';
import { agoLabel } from '../timeAgo';
import QrCode from '../link/QrCode';

// Сводка главной с бэкенда обновляется раз в минуту
const HOME_POLL_MS = 60_000;
// Строк в ленте «пока вас не было»
const FEED_LIMIT = 8;
// Репозиторий проекта: QR-код в подвале ведёт сюда (камерой телефона или нажатием)
const REPO_URL = 'https://github.com/godboomaster/untitled-virtual-personality-core';

interface HomeProps {
  onNavigate: (s: Section) => void;
}

// Строка ленты «пока вас не было»: событие одной персоны одного из типов
interface FeedRow {
  kind: 'initiative' | 'diary' | 'reminder';
  persona: string;
  text: string;
  time: string;
  outcome?: InitiativeOutcome;
  ts?: number; // для сортировки живой ленты
}

export default function Home({ onNavigate }: HomeProps) {
  const { t, lang } = useI18n();
  // Локальное время устройства пользователя: живые часы в hero-блоке
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);
  const locale = lang === 'ru' ? 'ru-RU' : 'en-US';
  const clock = now.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const dateLine = now.toLocaleDateString(locale, { weekday: 'short', day: 'numeric', month: 'short' });
  // Непрочитанные фоновые сообщения персон (напоминания, инициативы)
  const { unread } = useInbox();
  const {
    personas,
    remindersByPersona,
    diaryByPersona,
    initiativeByPersona,
  } = useMockData();

  // Живые данные главной (GET /api/home); null — бэкенд недоступен или ещё
  // грузится: тогда блоки показывают моковые данные, как и остальной UI
  const online = useApiOnline();
  const [overview, setOverview] = useState<HomeOverview | null>(null);
  useEffect(() => {
    if (!online) {
      setOverview(null);
      return;
    }
    let alive = true;
    const load = () =>
      api
        .getHome()
        .then((o) => alive && setOverview(o))
        .catch(() => {});
    void load();
    const timer = setInterval(load, HOME_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [online]);
  const live = overview !== null;
  const ov = (id: string) => overview?.personas[id];
  // Нет связи с ядром: занятие неизвестно (так и пишем), время последней
  // реплики — из копии переписки на телефоне. Моки (демо прототипа) — только
  // совсем без бэкенда: у телефона без ноутбука персоны настоящие, и
  // выдуманные факты, напоминания и инициативы выглядели бы как их
  const health = useCoreHealth();
  const noLink = !live && (!online || health === 'down');
  const mock = !online;
  const local = useMemo(
    () => (noLink && online ? new Map(personas.map((p) => [p.id, localLast(p.id)])) : null),
    [noLink, online, personas],
  );

  // «5 мин назад» / «вчера в 14:20» / «12.08» — от живых часов now
  const ago = (ts: number | null | undefined): string => agoLabel(ts, now, t, locale);

  // Приветствие по времени суток
  const hour = new Date().getHours();
  const greetKey =
    hour >= 5 && hour < 12
      ? 'home.greetMorning'
      : hour < 17
        ? 'home.greetDay'
        : hour < 23
          ? 'home.greetEvening'
          : 'home.greetNight';

  // Тикер «мысли персон»: последняя запись дневника и последняя инициатива каждой
  const thoughts = useMemo(
    () =>
      live
        ? personas
            .flatMap((p) => (overview.personas[p.id]?.events ?? []).slice(0, 1).map((e) => ({ name: p.name, text: e.text, ts: e.ts })))
            .sort((a, b) => b.ts - a.ts)
            .slice(0, 6)
        : !mock
          ? []
          : personas
        .flatMap((p) => {
          const items: { name: string; text: string }[] = [];
          const diary = (diaryByPersona[p.id] ?? [])[0];
          if (diary) items.push({ name: p.name, text: diary.text });
          const init = (initiativeByPersona[p.id] ?? []).at(-1);
          if (init) items.push({ name: p.name, text: init.text });
          return items;
        })
        .slice(0, 6),
    [live, mock, overview, personas, diaryByPersona, initiativeByPersona],
  );
  const [thoughtIdx, setThoughtIdx] = useState(0);

  // Ротация тикера каждые ~5 секунд (fade — через remount по key)
  useEffect(() => {
    if (thoughts.length === 0) return;
    const timer = setInterval(() => setThoughtIdx((i) => (i + 1) % thoughts.length), 5000);
    return () => clearInterval(timer);
  }, [thoughts.length]);

  const thought = thoughts.length ? thoughts[thoughtIdx % thoughts.length] : undefined;

  // Лента «пока вас не было»: живая — события каждой персоны после вашего
  // последнего сообщения ей (инициативы и записи дневника), свежие сверху;
  // мок — по 2 события каждого типа
  const feedRows: FeedRow[] = useMemo(() => {
    if (live) {
      return personas
        .flatMap((p) => {
          const o = overview.personas[p.id];
          const since = o?.last_user_ts ?? 0;
          return (o?.events ?? [])
            .filter((e) => e.ts > since)
            .map((e) => ({ kind: e.kind, persona: p.name, text: e.text, time: '', ts: e.ts }));
        })
        .sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0))
        .slice(0, FEED_LIMIT);
    }
    if (!mock) return [];
    const initRows: FeedRow[] = personas
      .flatMap((p) => {
        const e = (initiativeByPersona[p.id] ?? []).at(-1);
        return e ? [{ kind: 'initiative' as const, persona: p.name, text: e.text, time: e.time, outcome: e.outcome }] : [];
      })
      .slice(0, 2);
    const diaryRows: FeedRow[] = personas
      .flatMap((p) => {
        const d = (diaryByPersona[p.id] ?? [])[0];
        return d ? [{ kind: 'diary' as const, persona: p.name, text: d.text, time: d.date }] : [];
      })
      .slice(0, 2);
    const reminderRows: FeedRow[] = personas
      .flatMap((p) =>
        (remindersByPersona[p.id] ?? [])
          .filter((r) => r.active)
          .slice(0, 1)
          .map((r) => ({ kind: 'reminder' as const, persona: p.name, text: r.text, time: r.time })),
      )
      .slice(0, 2);
    return [...initRows, ...diaryRows, ...reminderRows];
  }, [live, mock, overview, personas, initiativeByPersona, diaryByPersona, remindersByPersona]);

  // Карточки «Состояния»: чей последний ответ свежее — та впереди, дальше по
  // убыванию; время то же, что в строке «последний ответ» карточки (сводка
  // ядра или копия переписки на телефоне). Без времени (ответа не было,
  // демо) — в конце, в прежнем порядке
  const presenceOrder = useMemo(() => {
    const lastTs = (id: string) => (live ? overview.personas[id]?.last_user_ts : local?.get(id)?.lastUserTs) ?? -1;
    return [...personas].sort((a, b) => lastTs(b.id) - lastTs(a.id));
  }, [live, overview, local, personas]);

  // Блок «Заметки»: персоны, у которых они не выключены (notes: false), и
  // число заметок каждой из сводки
  const notesPersonas = useMemo(
    () => (live ? personas.filter((p) => overview.personas[p.id]?.notes_enabled !== false) : personas),
    [live, overview, personas],
  );
  const notesCounts = useMemo(
    () => (live ? Object.fromEntries(personas.map((p) => [p.id, overview.personas[p.id]?.notes_count ?? 0])) : undefined),
    [live, overview, personas],
  );

  return (
    <div className="section home">
      {/* Hero: приветствие, заголовок, живой тикер мыслей и точки входа */}
      <div className="home-hero bracketed">
        <div className="corner tl plus" />
        <div className="corner tr" />
        <div className="corner bl" />
        <div className="corner br plus" />
        <div className="home-readout top-right">
          CORE <span className="val">v0.1.0</span>
          <br />
          MODE <span className="live-tag">LIVE</span>
          <br />
          LOCAL <span className="val">{clock}</span>
          <br />
          <span className="home-readout-date">{dateLine}</span>
        </div>
        <div className="home-eyebrow">{t('home.eyebrow')}</div>
        <div className="home-greet">{t(greetKey)}</div>
        <h1 className="home-title glitch" data-text={t('home.title')}>
          {t('home.title')}
        </h1>
        <p className="home-lead">{t('home.lead')}</p>
        {/* Тикер «мысли персоны»: последние записи дневников и инициатив */}
        {thought && (
          <div className="home-ticker">
            <span className="home-ticker-label">{t('home.tickerLabel')}</span>
            <span key={thoughtIdx} className="home-ticker-text">
              {thought.name}: {thought.text}
            </span>
          </div>
        )}
        <div className="home-cta-row">
          <button
            className="btn btn--primary"
            onClick={() => {
              requestPersonaCreate();
              onNavigate('personas');
            }}
          >
            {t('home.ctaInit')}
          </button>
          <button className="btn btn--ghost" onClick={() => onNavigate('chat')}>
            {t('home.ctaChat')}
          </button>
          <button className="btn btn--ghost" onClick={() => onNavigate('start')}>
            {t('home.ctaGuide')}
          </button>
        </div>
        <div className="home-readout bottom-right">
          PERSONAS <span className="val">{String(personas.length).padStart(3, '0')}</span>
        </div>
      </div>

      <div className="chrome-bar" />

      {/* На телефоне блоки — вкладками, как досье (PageTabs); на широком — подряд,
          строка заголовков сверху переносит к блоку (jump); шапка и подвал — всегда */}
      <PageTabs
        page="home"
        jump
        tabs={[
          {
            id: 'status',
            label: t('home.onlineTitle'),
            content: (
              <>
                {/* Присутствие: кто сейчас в сети и чем занят */}
                <div>
                  <div className="home-block-head">
                    <span className="home-block-title">{t('home.onlineTitle')}</span>
                    <span className="home-block-num">02 / STATUS</span>
                  </div>
                  <div className="home-presence-grid">
                    {presenceOrder.map((p) => {
                      // Занятие — только живое состояние персоны (living); без
                      // бэкенда — объяснение, демо-занятия не подставляем
                      const o = ov(p.id);
                      const activity = live
                        ? o?.state
                          ? [o.state.pastime, o.state.location].filter(Boolean).join(' · ')
                          : t('home.noState')
                        : noLink ? t(isStandalone() ? 'home.noLinkStandalone' : 'home.noLink') : '—';
                      const mood = live ? o?.state?.mood : undefined;
                      const localTs = local?.get(p.id)?.lastUserTs;
                      const lastReply = live ? ago(o?.last_user_ts) : localTs ? ago(localTs) : local ? t('home.unknown') : p.lastReply;
                      return (
                        <button
                          key={p.id}
                          type="button"
                          className="home-presence"
                          onClick={() => {
                            requestChatPersona(p.id);
                            onNavigate('chat');
                          }}
                        >
                          <div className="home-presence-head">
                            <div className="avatar">
                              <PersonaAvatar persona={p} followWithin=".home-presence-grid" />
                            </div>
                            <div>
                              <div className="home-presence-name">
                                {p.name}
                                {(unread[p.id] ?? 0) > 0 && (
                                  <span className="unread-dot" title={t('chat.unread')}>
                                    {unread[p.id]}
                                  </span>
                                )}
                              </div>
                              <div className="home-presence-status">
                                <span className="status-led" />
                                {t(`status.${p.status}`)}
                                {mood && <span className="home-presence-mood"> · {mood}</span>}
                              </div>
                            </div>
                          </div>
                          <div className={'home-presence-activity' + ((live && !o?.state) || noLink ? ' home-presence-activity--none' : '')}>
                            {activity}
                          </div>
                          {(unread[p.id] ?? 0) > 0 ? (
                            <div className="home-presence-reply home-presence-reply--unread">
                              {t('home.unreadMsg', { n: unread[p.id] })}
                            </div>
                          ) : (
                            <div className="home-presence-reply">{t('home.lastReplyLabel', { t: lastReply })}</div>
                          )}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </>
            ),
          },
          {
            id: 'events',
            label: t('pageTabs.events'),
            content: (
              <>
                {/* Лента событий «пока вас не было» */}
                <div>
                  <div className="home-block-head">
                    <span className="home-block-title">{t('home.feedTitle')}</span>
                    <span className="home-block-num">03 / EVENTS</span>
                  </div>
                  <div className="home-feed">
                    {live && feedRows.length === 0 && <div className="home-feed-empty">{t('home.feedEmpty')}</div>}
                    {noLink && !mock && <div className="home-feed-empty">{t('home.feedNoLink')}</div>}
                    {feedRows.map((row, i) => (
                      <div key={i} className="home-feed-row">
                        <span className={`badge home-feed-badge--${row.kind}`}>
                          {row.kind === 'initiative'
                            ? t('home.badgeInitiative')
                            : row.kind === 'diary'
                              ? t('home.badgeDiary')
                              : t('home.badgeReminder')}
                        </span>
                        <span className="home-feed-persona">{row.persona}</span>
                        <span className="home-feed-text">{row.text}</span>
                        {row.outcome && (
                          <span
                            className={`badge ${
                              row.outcome === 'answered'
                                ? 'badge--success'
                                : row.outcome === 'ignored'
                                  ? 'badge--muted'
                                  : ''
                            }`}
                          >
                            {t(`outcome.${row.outcome}`)}
                          </span>
                        )}
                        <span className="home-feed-time">{row.ts ? ago(row.ts) : row.time}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </>
            ),
          },
          {
            id: 'notes',
            label: t('notes.title'),
            content: (
              <>
                {/* Заметки пользователя: своя книжка у каждой персоны */}
                <div>
                  <div className="home-block-head">
                    <span className="home-block-title">{t('notes.title')}</span>
                    <span className="home-block-num">04 / NOTES</span>
                  </div>
                  <NotesWidget personas={notesPersonas} live={live} mock={mock} noLink={noLink} phone={isNativeApp() && noLink} counts={notesCounts} />
                </div>
              </>
            ),
          },
          {
            id: 'calendar',
            label: t('calendar.title'),
            content: (
              <>
                {/* Общий календарь всех персон */}
                <div>
                  <div className="home-block-head">
                    <span className="home-block-title">{t('calendar.title')}</span>
                    <span className="home-block-num">05 / PLAN</span>
                  </div>
                  <CalendarWidget />
                </div>
              </>
            ),
          },
        ]}
      />

      {/* Подвал: девиз и QR-код репозитория */}
      <div className="home-foot">
        <span className="home-foot-stencil">{t('home.footStencil')}</span>
        <div className="home-foot-right">
          <a className="home-foot-qr" href={REPO_URL} target="_blank" rel="noreferrer" title={t('home.footRepo')} aria-label={t('home.footRepo')}>
            <QrCode text={REPO_URL} size={90} />
          </a>
        </div>
      </div>
    </div>
  );
}
