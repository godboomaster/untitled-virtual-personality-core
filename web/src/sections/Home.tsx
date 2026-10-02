import { useEffect, useMemo, useState } from 'react';
import type { Section } from '../App';
import { useI18n, useMockData } from '../i18n';
import type { InitiativeOutcome } from '../mockData';
import { requestPersonaCreate } from '../personaCreateStore';
import { requestChatPersona } from '../chatNavStore';
import { useInbox } from '../inboxStore';
import { usePersonaAvatars } from '../avatarStore';
import CalendarWidget from '../components/CalendarWidget';
import { api } from '../api';
import type { HomeOverview } from '../api';
import { useApiOnline } from '../apiData';

// Сводка главной с бэкенда обновляется раз в минуту
const HOME_POLL_MS = 60_000;
// Строк в ленте «пока вас не было»
const FEED_LIMIT = 8;

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
  const avatars = usePersonaAvatars();
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
    ltmByPersona,
    remindersByPersona,
    initiativeStateByPersona,
    diaryByPersona,
    initiativeByPersona,
    roomConfigs,
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

  // «5 мин назад» / «вчера в 14:20» / «12.08» — от живых часов now
  const ago = (ts: number | null | undefined): string => {
    if (!ts) return t('home.never');
    const sec = Math.max(0, now.getTime() / 1000 - ts);
    if (sec < 60) return t('home.agoNow');
    if (sec < 3600) return t('home.agoMin', { n: Math.floor(sec / 60) });
    const d = new Date(ts * 1000);
    const hm = d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
    if (ts >= startOfToday) return t('home.agoToday', { t: hm });
    if (ts >= startOfToday - 86400) return t('home.agoYesterday', { t: hm });
    return d.toLocaleDateString(locale, { day: '2-digit', month: '2-digit' });
  };

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
    [live, overview, personas, diaryByPersona, initiativeByPersona],
  );
  const [thoughtIdx, setThoughtIdx] = useState(0);

  // Ротация тикера каждые ~5 секунд (fade — через remount по key)
  useEffect(() => {
    if (thoughts.length === 0) return;
    const timer = setInterval(() => setThoughtIdx((i) => (i + 1) % thoughts.length), 5000);
    return () => clearInterval(timer);
  }, [thoughts.length]);

  const thought = thoughts.length ? thoughts[thoughtIdx % thoughts.length] : undefined;

  // Досье-карточки модулей консоли
  const modules: { index: string; title: string; desc: string; target: Section; status: string }[] = [
    { index: 'MOD_01', title: t('home.modChatTitle'), desc: t('home.modChatDesc'), target: 'chat', status: 'ACTIVE' },
    { index: 'MOD_02', title: t('home.modDossierTitle'), desc: t('home.modDossierDesc'), target: 'chat', status: 'ACTIVE' },
    { index: 'MOD_03', title: t('home.modRoomTitle'), desc: t('home.modRoomDesc'), target: 'room', status: 'ACTIVE' },
    { index: 'MOD_04', title: t('home.modPersonasTitle'), desc: t('home.modPersonasDesc'), target: 'personas', status: 'ACTIVE' },
    { index: 'MOD_05', title: t('home.modSettingsTitle'), desc: t('home.modSettingsDesc'), target: 'settings', status: 'ACTIVE' },
    { index: 'MOD_06', title: t('home.modStartTitle'), desc: t('home.modStartDesc'), target: 'start', status: 'GUIDE' },
  ];

  // Агрегированная телеметрия ядра: суммы по всем персонам
  const sumLive = (pick: (o: NonNullable<ReturnType<typeof ov>>) => number) =>
    personas.reduce((n, p) => n + (overview?.personas[p.id] ? pick(overview.personas[p.id]) : 0), 0);
  const totalLtmFacts = live
    ? sumLive((o) => o.ltm_facts)
    : Object.values(ltmByPersona).reduce((n, facts) => n + facts.length, 0);

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
  }, [live, overview, personas, initiativeByPersona, diaryByPersona, remindersByPersona]);

  // Строки журнала системы для терминального блока
  const logLines: React.ReactNode[] = [
    <> &gt; CORE.BOOT &nbsp;v0.1.0 &nbsp;&nbsp;// <b>OK</b></>,
    <> &gt; PERSONA.LOAD &nbsp;connor · arrodes · verso +3 &nbsp;&nbsp;// <b>{t('home.logPersonaLoad', { n: personas.length })}</b></>,
    <> &gt; LTM.SYNC &nbsp;{t('home.logLtm', { n: totalLtmFacts })} &nbsp;&nbsp;// <b>OK</b></>,
    <> &gt; STM.BUFFER &nbsp;{t('home.logStm')} &nbsp;&nbsp;// <b>OK</b></>,
    <> &gt; DIARY.WRITE &nbsp;{t('home.logDiary')} &nbsp;&nbsp;// <b>OK</b></>,
    <> &gt; INITIATIVE.LOOP &nbsp;{t('home.logInitiative')} &nbsp;&nbsp;// <span className="bright">MONITORING</span></>,
    <> &gt; ROOM.RENDER &nbsp;{t('home.logRoom')} &nbsp;&nbsp;// <b>SYNCED</b></>,
    <> &gt; ART.PIPELINE &nbsp;{t('home.logArt')} &nbsp;&nbsp;// <b>IN-ROOM</b></>,
    <> &gt; OPERATOR &nbsp;{t('home.logOperator')} &nbsp;&nbsp;// <b>CONNECTED</b></>,
  ];

  // Журнал системы: строки появляются по очереди, как в терминале
  const [visibleLines, setVisibleLines] = useState(0);

  useEffect(() => {
    if (visibleLines >= logLines.length) return;
    const timer = setTimeout(() => setVisibleLines((v) => v + 1), 420);
    return () => clearTimeout(timer);
  }, [visibleLines, logLines.length]);

  const activeReminders = live
    ? sumLive((o) => o.reminders_active)
    : Object.values(remindersByPersona).reduce((n, rs) => n + rs.filter((r) => r.active).length, 0);
  const initiativesToday = live
    ? sumLive((o) => o.initiatives_today)
    : Object.values(initiativeStateByPersona).reduce((n, s) => n + s.initiativesToday, 0);
  const initiativesMax = live
    ? sumLive((o) => o.initiatives_max)
    : Object.values(initiativeStateByPersona).reduce((n, s) => n + s.maxPerDay, 0);

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
        <div className="home-eyebrow">Virtual Persona Core // Ops Console</div>
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

      {/* Присутствие: кто сейчас в сети и чем занят */}
      <div>
        <div className="home-block-head">
          <span className="home-block-title">{t('home.onlineTitle')}</span>
          <span className="home-block-num">02 / STATUS</span>
        </div>
        <div className="home-presence-grid">
          {personas.map((p) => {
            // Занятие: живое состояние персоны (living) или мок комнаты
            const o = ov(p.id);
            // Мок — только своей персоны: занятия Коннора другим не подставляем
            const mockPastime = roomConfigs[p.id]?.pastimes[0];
            const activity = live
              ? o?.state
                ? [o.state.pastime, o.state.location].filter(Boolean).join(' · ')
                : t('home.noState')
              : mockPastime
                ? `${mockPastime.label} · ${mockPastime.place}`
                : t('home.noState');
            const mood = live ? o?.state?.mood : undefined;
            const lastReply = live ? ago(o?.last_user_ts) : p.lastReply;
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
                    {avatars[p.id] ? <img src={avatars[p.id]} alt={p.name} /> : p.name.charAt(0)}
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
                <div className={'home-presence-activity' + (live && !o?.state ? ' home-presence-activity--none' : '')}>
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

      {/* Телеметрия ядра */}
      <div className="home-telemetry">
        <div className="home-tele-cell">
          <div className="corner tl" />
          <div className="corner br" />
          <div className="home-tele-label">{t('home.telePersonas')}</div>
          <div className="home-tele-value">{personas.length}</div>
        </div>
        <div className="home-tele-cell">
          <div className="corner tl" />
          <div className="corner br" />
          <div className="home-tele-label">{t('home.teleLtm')}</div>
          <div className="home-tele-value">{totalLtmFacts}</div>
        </div>
        <div className="home-tele-cell">
          <div className="corner tl" />
          <div className="corner br" />
          <div className="home-tele-label">{t('home.teleReminders')}</div>
          <div className="home-tele-value">{activeReminders}</div>
        </div>
        <div className="home-tele-cell">
          <div className="corner tl" />
          <div className="corner br" />
          <div className="home-tele-label">{t('home.teleInitiatives')}</div>
          <div className="home-tele-value">
            {initiativesToday}
            <span> / {initiativesMax}</span>
          </div>
        </div>
      </div>

      {/* Лента событий «пока вас не было» */}
      <div>
        <div className="home-block-head">
          <span className="home-block-title">{t('home.feedTitle')}</span>
          <span className="home-block-num">03 / EVENTS</span>
        </div>
        <div className="home-feed">
          {live && feedRows.length === 0 && <div className="home-feed-empty">{t('home.feedEmpty')}</div>}
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

      {/* Общий календарь всех персон */}
      <div>
        <div className="home-block-head">
          <span className="home-block-title">{t('calendar.title')}</span>
          <span className="home-block-num">04 / PLAN</span>
        </div>
        <CalendarWidget />
      </div>

      {/* Модули консоли */}
      <div>
        <div className="home-block-head">
          <span className="home-block-title">{t('home.modulesTitle')}</span>
          <span className="home-block-num">05 / MODULES</span>
        </div>
        <div className="home-dossier-grid">
          {modules.map((m) => (
            <button key={m.index} type="button" className="home-dossier" onClick={() => onNavigate(m.target)}>
              <span className="home-dossier-index">{m.index}</span>
              <h3>{m.title}</h3>
              <p>{m.desc}</p>
              <span className="home-dossier-foot">
                <span>{m.status}</span>
                <span className="go">{t('home.modOpen')}</span>
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* Журнал системы */}
      <div>
        <div className="home-block-head">
          <span className="home-block-title">{t('home.syslogTitle')}</span>
          <span className="home-block-num">06 / SYSLOG</span>
        </div>
        <div className="home-term bracketed">
          <div className="corner tl" />
          <div className="corner tr" />
          <div className="corner bl" />
          <div className="corner br" />
          <div className="home-term-titlebar">
            <span className="lights"><i /><i /><i /></span>
            <span>vpc-core — syslog — 80×24</span>
            <span>PID 01337</span>
          </div>
          {logLines.slice(0, visibleLines).map((line, i) => (
            <div key={i} className="home-term-line">{line}</div>
          ))}
          <div className="home-term-line">
            <span className="bright">&gt;</span> {t('home.waitingOperator')}<span className="home-term-cursor" />
          </div>
        </div>
      </div>

      {/* Подвал: технические мелочи */}
      <div className="home-foot">
        <span className="home-foot-stencil">{t('home.footStencil')}</span>
        <div className="home-foot-right">
          <div className="barcode" aria-hidden="true">
            <i style={{ width: 2 }} /><i style={{ width: 1 }} /><i style={{ width: 3 }} /><i style={{ width: 1 }} />
            <i style={{ width: 1 }} /><i style={{ width: 2 }} /><i style={{ width: 4 }} /><i style={{ width: 1 }} />
            <i style={{ width: 1 }} /><i style={{ width: 2 }} /><i style={{ width: 1 }} /><i style={{ width: 3 }} />
            <i style={{ width: 1 }} /><i style={{ width: 1 }} /><i style={{ width: 2 }} />
          </div>
          <span>SN // VPC-0100-XK42</span>
        </div>
      </div>
    </div>
  );
}
