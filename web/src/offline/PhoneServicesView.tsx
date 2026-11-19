import { useCallback, useEffect, useRef, useState } from 'react';
import InfoButton from '../components/InfoButton';
import PageTabs from '../components/PageTabs';
import Select from '../components/Select';
import type { ServiceCheck, ServiceRule } from '../api';
import { useApiPersonas } from '../apiData';
import { confirmDialog } from '../dialogStore';
import { useI18n } from '../i18n';
import { CheckResult, Header, ServiceCard, StateLine, Switch, ToggleRow } from '../sections/servicesParts';
import { errText, timeLabel } from '../sections/servicesText';
import type { T } from '../sections/servicesText';
import { PhoneServices } from './phoneServices';
import type { PhoneNotify, PhoneServicesStatus, PhoneSource } from './phoneServices';

/* «Сервисы» на телефоне без компьютера (phoneServices.ts): ленты RSS,
   GitHub, почта IMAP и Gmail — их проверяет сам телефон в фоне. Тот же вид,
   что с компьютером, без того, что здесь не работает: модели разбора нет
   (решают правила и признаки), Google Диск, Telegram-аккаунт и вебхук —
   только с компьютером (карточки серые, с объяснением). Перенесённое на
   компьютер помечено — телефон его не проверяет. */

const POLL_MS = 15_000;
const RULE_SOURCES: PhoneSource[] = ['rss', 'github', 'imap', 'gmail'];

type Say = (m: string) => void;

interface CardProps {
  st: PhoneServicesStatus;
  t: T;
  locale: string;
  say: Say;
  reload: () => void;
}

/** Первая проверка сразу после подключения: курсор («с этого момента») и «проверено в …». */
function firstCheck(service: PhoneSource, reload: () => void) {
  void PhoneServices.checkNow({ service }).finally(reload);
}

/** Разрешение на уведомления (Android 13+) — когда подключают первый сервис. */
function askNotifications(say: Say, t: T) {
  void PhoneServices.requestNotificationPermission()
    .then((r) => {
      if (!r.granted) say(t('phoneSvc.notifyDenied'));
    })
    .catch(() => {});
}

export default function PhoneServicesView() {
  const { t, lang } = useI18n();
  const locale = lang === 'ru' ? 'ru-RU' : 'en-US';
  const [st, setSt] = useState<PhoneServicesStatus | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const load = useCallback(() => {
    PhoneServices.status()
      .then((s) => {
        setSt(s);
        setLoadErr(null);
      })
      .catch((e) => setLoadErr(errText(e)));
  }, []);

  useEffect(() => {
    load();
    const tick = () => {
      if (document.visibilityState === 'visible') load();
    };
    const timer = window.setInterval(tick, POLL_MS);
    document.addEventListener('visibilitychange', tick);
    const subs = [PhoneServices.addListener('changed', load), PhoneServices.addListener('delivered', load)];
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
      subs.forEach((p) => void p.then((h) => h.remove()).catch(() => {}));
    };
  }, [load]);

  const say = (msg: string) => {
    setFlash(msg);
    window.setTimeout(() => setFlash((cur) => (cur === msg ? null : cur)), 6000);
  };

  if (!st) {
    return (
      <div className="section services">
        <Header t={t} subtitle={t('phoneSvc.subtitle')} />
        <div className="card"><p className="field-hint">{loadErr ?? t('services.loading')}</p></div>
      </div>
    );
  }

  const props: CardProps = { st, t, locale, say, reload: load };
  return (
    <div className="section services">
      <Header t={t} subtitle={t('phoneSvc.subtitle')} />
      {flash && <div className="svc-flash" role="status">{flash}</div>}
      <PageTabs
        page="services"
        tabs={[
          { id: 'overview', label: t('pageTabs.overview'), content: <Overview {...props} /> },
          { id: 'notify', label: t('services.notifyTitle'), content: <NotifyCard {...props} /> },
          {
            id: 'connections',
            label: t('services.connections'),
            content: (
              <>
                <h2 className="svc-group-title">{t('services.connections')}</h2>
                <div className="svc-grid">
                  <RssCard {...props} />
                  <GithubCard {...props} />
                  <ImapCard {...props} />
                  <GmailCard {...props} />
                  <ComputerOnly title={t('phoneSvc.drive')} hint={t('phoneSvc.driveOnly')} t={t} />
                  <ComputerOnly title="Telegram" hint={t('phoneSvc.telegramOnly')} t={t} />
                  <ComputerOnly title={t('services.webhook')} hint={t('phoneSvc.webhookOnly')} t={t} />
                </div>
              </>
            ),
          },
          { id: 'rules', label: t('services.rules'), content: <RulesCard {...props} /> },
          { id: 'feed', label: t('services.feed'), content: <FeedCard {...props} /> },
        ]}
      />
    </div>
  );
}

// ── Сводка, проверка, «не беспокоить» ──────────────────────────────────

function Overview({ st, t, locale, say, reload }: CardProps) {
  const [busy, setBusy] = useState(false);
  const dnd = st.settings.dnd_until > st.now;
  const anyActive = Object.values(st.status).some((s) => s.active);
  const run = (p: Promise<unknown>, ok?: string) => {
    setBusy(true);
    p.then(() => {
      if (ok) say(ok);
      reload();
    })
      .catch((e) => say(errText(e)))
      .finally(() => setBusy(false));
  };
  const checkNow = () =>
    run(PhoneServices.checkNow().then((r) => {
      if (r.busy) throw new Error(t('phoneSvc.busy'));
      const errors = Object.entries(r.polled ?? {})
        .filter(([, v]) => v && !v.ok)
        .map(([k, v]) => `${t(`services.src.${k}`)}: ${v?.error ?? ''}`);
      say(errors.length
        ? t('phoneSvc.checkedErrors', { errors: errors.join('; ') })
        : t('phoneSvc.checked', { n: r.new ?? 0, m: r.delivered ?? 0 }));
    }));
  return (
    <div className="card svc-overview">
      <div className="svc-stats">
        <div><span className="svc-stat-num">{st.counts.today}</span><span className="svc-stat-label">{t('services.statToday')}</span></div>
        <div><span className="svc-stat-num">{st.counts.notified_today}</span><span className="svc-stat-label">{t('services.statNotified')}</span></div>
        <div><span className="svc-stat-num">{st.counts.digest}</span><span className="svc-stat-label">{t('services.statDigest')}</span></div>
        <div><span className="svc-stat-num">{st.counts.queued}</span><span className="svc-stat-label">{t('services.statQueued')}</span></div>
      </div>
      <p className="field-hint">
        {anyActive
          ? t('phoneSvc.schedule', { n: st.settings.interval_min, time: timeLabel(st.last_run || null, locale) })
          : t('phoneSvc.notScheduled')}
      </p>
      <p className="field-hint">
        {st.persona ? t('services.whoNotifies', { name: st.persona.name || st.persona.id }) : t('services.noPersona')}
      </p>
      {!st.notificationsEnabled && (
        <p className="svc-error">
          {t('phoneSvc.notifyOff')}{' '}
          <button type="button" className="btn btn--chip" onClick={() => {
            void PhoneServices.requestNotificationPermission().then(reload).catch(() => {});
          }}>
            {t('phoneSvc.notifyAllow')}
          </button>
        </p>
      )}
      <div className="svc-actions">
        <button type="button" className="btn btn--primary" disabled={busy || st.running || !anyActive} onClick={checkNow}>
          {busy || st.running ? t('phoneSvc.checking') : t('phoneSvc.checkNow')}
        </button>
        <button type="button" className="btn btn--ghost" disabled={busy || st.counts.digest + st.counts.queued === 0}
          onClick={() => run(PhoneServices.digestNow().then((r) => {
            if (!r.sent) throw new Error(t('services.noPersona'));
          }), t('services.digestSent'))}>
          {t('services.digestNow')}
        </button>
        <button type="button" className="btn btn--ghost" disabled={busy}
          onClick={() => run(PhoneServices.testNotify().then((r) => {
            if (!r.ok) throw new Error(t('services.noPersona'));
          }), t('services.testSent'))}>
          {t('services.test')}
        </button>
        {dnd ? (
          <>
            <span className="badge badge--active">{t('services.dndUntil', { time: timeLabel(st.settings.dnd_until, locale) })}</span>
            <button type="button" className="btn btn--ghost" disabled={busy}
              onClick={() => run(PhoneServices.setDnd({ minutes: 0 }), t('services.dndOff'))}>
              {t('services.dndStop')}
            </button>
          </>
        ) : (
          <>
            <button type="button" className="btn btn--ghost" disabled={busy}
              onClick={() => run(PhoneServices.setDnd({ minutes: 60 }), t('services.dndOn'))}>
              {t('services.dnd1h')}
            </button>
            <button type="button" className="btn btn--ghost" disabled={busy}
              onClick={() => run(PhoneServices.setDnd({ minutes: 180 }), t('services.dndOn'))}>
              {t('services.dnd3h')}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// ── Настройки уведомлений ───────────────────────────────────────────────

function NotifyCard({ st, t, say, reload }: CardProps) {
  const n = st.settings.notify;
  const personas = useApiPersonas() ?? [];
  const [digest, setDigest] = useState(n.digest_times.join(', '));
  useEffect(() => setDigest(n.digest_times.join(', ')), [n.digest_times]);
  const save = (patch: Partial<PhoneNotify>) =>
    PhoneServices.updateSettings({ notify: patch }).then(reload).catch((e) => say(errText(e)));
  return (
    <div className="card">
      <h2 className="card-title">{t('services.notifyTitle')}</h2>
      <div className="field-grid">
        <div>
          <label className="field-label" htmlFor="psvc-persona">{t('services.persona')}</label>
          <Select
            id="psvc-persona"
            value={n.persona ?? ''}
            options={[
              { value: '', label: t('services.personaAuto') },
              ...personas.map((p) => ({ value: p.id, label: p.name })),
            ]}
            onChange={(v) => void save({ persona: v || null })}
          />
        </div>
        <div>
          <label className="field-label" htmlFor="psvc-interval">{t('phoneSvc.interval')}</label>
          <Select
            id="psvc-interval"
            value={String(st.settings.interval_min)}
            options={[
              { value: '15', label: t('phoneSvc.interval15') },
              { value: '30', label: t('phoneSvc.interval30') },
            ]}
            onChange={(v) => void PhoneServices.updateSettings({ interval_min: Number(v) }).then(reload).catch((e) => say(errText(e)))}
          />
        </div>
        <div>
          <label className="field-label" htmlFor="psvc-lang">{t('services.language')}</label>
          <Select
            id="psvc-lang"
            value={n.language}
            options={(['auto', 'ru', 'en'] as const).map((e) => ({ value: e, label: t(`services.language.${e}`) }))}
            onChange={(v) => void save({ language: v as PhoneNotify['language'] })}
          />
        </div>
      </div>
      <p className="field-hint">{t('phoneSvc.engineHint')}</p>

      <div className="field">
        <label className="field-label">{t('services.quiet')}</label>
        <div className="svc-inline">
          <Switch checked={n.quiet.enabled} onChange={(v) => void save({ quiet: { ...n.quiet, enabled: v } })} />
          <input type="time" className="input svc-time" value={n.quiet.from} disabled={!n.quiet.enabled}
            onChange={(e) => e.target.value && void save({ quiet: { ...n.quiet, from: e.target.value } })} />
          <span>—</span>
          <input type="time" className="input svc-time" value={n.quiet.to} disabled={!n.quiet.enabled}
            onChange={(e) => e.target.value && void save({ quiet: { ...n.quiet, to: e.target.value } })} />
        </div>
        <ToggleRow label={t('services.quietUrgent')} checked={n.quiet.urgent}
          onChange={(v) => void save({ quiet: { ...n.quiet, urgent: v } })} />
      </div>

      <div className="field-grid">
        <div>
          <label className="field-label" htmlFor="psvc-digest">{t('services.digestTimes')}</label>
          <input id="psvc-digest" className="input" value={digest} placeholder="09:00, 19:00"
            onChange={(e) => setDigest(e.target.value)}
            onBlur={() => void save({ digest_times: digest.split(/[,\s;]+/).filter(Boolean) })} />
        </div>
        <div>
          <label className="field-label" htmlFor="psvc-rate">{t('services.maxPerHour')}</label>
          <input id="psvc-rate" className="input" type="number" min={1} max={60} value={n.max_per_hour}
            onChange={(e) => {
              const v = Number(e.target.value);
              if (v >= 1 && v <= 60) void save({ max_per_hour: v });
            }} />
        </div>
      </div>
      <p className="field-hint">{t('phoneSvc.digestHint')}</p>
    </div>
  );
}

// ── Подключения ─────────────────────────────────────────────────────────

function useCheck(service: 'github' | 'imap' | 'gmail', reload: () => void) {
  const [res, setRes] = useState<ServiceCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const run = () => {
    setBusy(true);
    PhoneServices.checkService({ service })
      .then(setRes)
      .catch((e) => setRes({ ok: false, error: errText(e) }))
      .finally(() => {
        setBusy(false);
        reload();
      });
  };
  return { res, busy, run };
}

function DisconnectButton({ account, name, t, say, reload }: {
  account: 'rss' | 'github' | 'imap' | 'google';
  name: string;
  t: T;
  say: Say;
  reload: () => void;
}) {
  return (
    <button type="button" className="btn btn--ghost"
      onClick={async () => {
        if (!(await confirmDialog({
          title: t('services.disconnectTitle', { name }),
          message: t('phoneSvc.disconnectText'),
          confirmLabel: t('services.disconnect'),
          danger: true,
        }))) return;
        PhoneServices.disconnect({ account }).then(() => {
          say(t('services.disconnected', { name }));
          reload();
        }).catch((e) => say(errText(e)));
      }}>
      {t('services.disconnect')}
    </button>
  );
}

/** Перенесено на компьютер: телефон не проверяет; можно вернуть. */
function Moved({ st, source, t, locale, say, reload }: CardProps & { source: PhoneSource }) {
  const ts = st.settings.moved[source];
  if (!ts) return null;
  return (
    <p className="field-hint">
      {t('phoneSvc.moved', { time: timeLabel(ts, locale) })}{' '}
      <button type="button" className="btn btn--chip"
        onClick={() => PhoneServices.resume({ service: source }).then(reload).catch((e) => say(errText(e)))}>
        {t('phoneSvc.resume')}
      </button>
    </p>
  );
}

function badgeFor(st: PhoneServicesStatus, source: PhoneSource, connected: boolean, label: string, t: T) {
  if (st.settings.moved[source]) return { text: t('phoneSvc.badgeMoved'), on: false };
  if (st.status[source].auth_error) return { text: t('services.badgeReauth'), on: false, warn: true };
  return connected ? { text: label || t('services.badgeOn'), on: true } : { text: t('services.badgeOff'), on: false };
}

function RssCard(props: CardProps) {
  const { st, t, locale, say, reload } = props;
  const rss = st.settings.services.rss;
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const saveRss = (patch: Record<string, unknown>) =>
    PhoneServices.updateSettings({ services: { rss: patch } }).then(reload).catch((e) => say(errText(e)));
  const add = () => {
    const u = url.trim();
    setBusy(true);
    PhoneServices.checkRss({ url: u })
      .then(async (f) => {
        if (!f.ok) {
          say(f.error ?? t('services.rssBad'));
          return;
        }
        setUrl('');
        await PhoneServices.updateSettings({
          services: { rss: { feeds: [...rss.feeds, { url: u, title: f.title ?? '' }], enabled: true } },
        });
        say(t('services.rssAdded', { title: f.title || u, n: f.items ?? 0 }));
        askNotifications(say, t);
        firstCheck('rss', reload);
      })
      .catch((e) => say(errText(e)))
      .finally(() => setBusy(false));
  };
  const badge = rss.feeds.length
    ? badgeFor(st, 'rss', rss.enabled, t('services.rssCount', { n: rss.feeds.length }), t)
    : { text: t('services.badgeOff'), on: false };
  return (
    <ServiceCard title={t('services.rss')} badge={badge} help="services.rss">
      <p className="field-hint">{t('phoneSvc.rssHint')}</p>
      <Moved {...props} source="rss" />
      {rss.feeds.length > 0 && (
        <>
          <ToggleRow label={t('services.rssToggle')} checked={rss.enabled} onChange={(v) => void saveRss({ enabled: v })} />
          <ul className="svc-list">
            {rss.feeds.map((f) => (
              <li key={f.url}>
                <span className="svc-list-text" title={f.url}>{f.title || f.url}</span>
                <button type="button" className="btn btn--chip"
                  onClick={() => void saveRss({ feeds: rss.feeds.filter((x) => x.url !== f.url) })}>
                  {t('common.delete')}
                </button>
              </li>
            ))}
          </ul>
          <StateLine state={st.status.rss} t={t} locale={locale} />
        </>
      )}
      <div className="svc-inline">
        <input className="input" placeholder="https://…/feed.xml" inputMode="url" value={url} onChange={(e) => setUrl(e.target.value)} />
        <button type="button" className="btn btn--ghost" disabled={busy || !/^https?:\/\/\S+$/.test(url.trim())} onClick={add}>
          {t('common.add')}
        </button>
      </div>
    </ServiceCard>
  );
}

function GithubCard(props: CardProps) {
  const { st, t, locale, say, reload } = props;
  const gh = st.accounts.github;
  const state = st.status.github;
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const check = useCheck('github', reload);
  const connect = () => {
    setBusy(true);
    PhoneServices.connectGithub({ token: token.trim() })
      .then((r) => {
        if (r.ok) {
          setToken('');
          say(t('services.githubConnected', { login: r.login ?? '' }));
          askNotifications(say, t);
          firstCheck('github', reload);
        } else say(r.error ?? t('services.checkFail'));
        reload();
      })
      .catch((e) => say(errText(e)))
      .finally(() => setBusy(false));
  };
  return (
    <ServiceCard title="GitHub" badge={badgeFor(st, 'github', gh.connected, gh.login, t)} help="services.github">
      <Moved {...props} source="github" />
      {(!gh.connected || state.auth_error) && (
        <>
          <p className="field-hint">
            {t('phoneSvc.githubHint')}{' '}
            <a href="https://github.com/settings/tokens" target="_blank" rel="noreferrer">github.com/settings/tokens</a>
          </p>
          <div className="svc-inline">
            <input className="input" type="password" placeholder="ghp_… / github_pat_…" value={token} onChange={(e) => setToken(e.target.value)} />
            <button type="button" className="btn btn--primary" disabled={busy || token.trim().length < 20} onClick={connect}>
              {t('services.connect')}
            </button>
          </div>
        </>
      )}
      {gh.connected && !state.auth_error && (
        <>
          <p className="field-hint">{gh.login} · {gh.token_hint}</p>
          <ToggleRow label={t('services.githubToggle')} checked={st.settings.services.github.enabled}
            onChange={(v) => void PhoneServices.updateSettings({ services: { github: { enabled: v } } }).then(reload)} />
          <StateLine state={state} t={t} locale={locale} />
          <div className="svc-actions">
            <button type="button" className="btn btn--ghost" disabled={check.busy} onClick={check.run}>{t('services.check')}</button>
            <DisconnectButton account="github" name="GitHub" t={t} say={say} reload={reload} />
          </div>
          <CheckResult res={check.res} t={t} />
        </>
      )}
    </ServiceCard>
  );
}

function ImapCard(props: CardProps) {
  const { st, t, locale, say, reload } = props;
  const im = st.accounts.imap;
  const state = st.status.imap;
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [host, setHost] = useState('');
  const [busy, setBusy] = useState(false);
  const check = useCheck('imap', reload);
  const preset = () => {
    if (!user.includes('@') || host) return;
    PhoneServices.imapPreset({ user: user.trim() }).then((p) => p.host && setHost(p.host)).catch(() => {});
  };
  const connect = () => {
    setBusy(true);
    PhoneServices.connectImap({ user: user.trim(), password, host: host.trim() || undefined })
      .then((r) => {
        if (r.ok) {
          setPassword('');
          say(t('services.imapConnected', { host: r.host ?? '' }));
          askNotifications(say, t);
          firstCheck('imap', reload);
        } else say(r.error ?? t('services.checkFail'));
        reload();
      })
      .catch((e) => say(errText(e)))
      .finally(() => setBusy(false));
  };
  return (
    <ServiceCard title={t('services.imap')} badge={badgeFor(st, 'imap', im.connected, im.user, t)} help="services.imap">
      <Moved {...props} source="imap" />
      {(!im.connected || state.auth_error) && (
        <>
          <p className="field-hint">{t('phoneSvc.imapHint')}</p>
          <input className="input" placeholder="name@yandex.ru" inputMode="email" value={user} onChange={(e) => setUser(e.target.value)} onBlur={preset} />
          <input className="input" style={{ marginTop: 6 }} type="password" placeholder={t('services.imapPassword')} value={password} onChange={(e) => setPassword(e.target.value)} />
          <div className="svc-inline" style={{ marginTop: 6 }}>
            <input className="input" placeholder={t('services.imapHost')} value={host} onChange={(e) => setHost(e.target.value)} />
            <button type="button" className="btn btn--primary" disabled={busy || !user.includes('@') || !password} onClick={connect}>
              {t('services.connect')}
            </button>
          </div>
        </>
      )}
      {im.connected && !state.auth_error && (
        <>
          <p className="field-hint">{im.user} · {im.host}</p>
          <ToggleRow label={t('services.imapToggle')} checked={st.settings.services.imap.enabled}
            onChange={(v) => void PhoneServices.updateSettings({ services: { imap: { enabled: v } } }).then(reload)} />
          <StateLine state={state} t={t} locale={locale} />
          <div className="svc-actions">
            <button type="button" className="btn btn--ghost" disabled={check.busy} onClick={check.run}>{t('services.check')}</button>
            <DisconnectButton account="imap" name={im.user} t={t} say={say} reload={reload} />
          </div>
          <CheckResult res={check.res} t={t} />
        </>
      )}
    </ServiceCard>
  );
}

function GmailCard(props: CardProps) {
  const { st, t, locale, say, reload } = props;
  const g = st.accounts.google;
  const state = st.status.gmail;
  const [cid, setCid] = useState('');
  const [secret, setSecret] = useState('');
  const [jsonText, setJsonText] = useState('');
  const [editClient, setEditClient] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [pasted, setPasted] = useState('');
  const check = useCheck('gmail', reload);
  const pollRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (pollRef.current) window.clearInterval(pollRef.current);
  }, []);

  const done = (r: { ok: boolean; error?: string; email?: string }) => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    setWaiting(false);
    if (r.ok) {
      setPasted('');
      say(t('services.googleConnected', { email: r.email ?? '' }));
      askNotifications(say, t);
      firstCheck('gmail', reload);
    } else say(r.error ?? t('services.checkFail'));
    reload();
  };

  const saveClient = () => {
    PhoneServices.setGoogleClient(jsonText.trim() ? { json_text: jsonText } : { client_id: cid.trim(), client_secret: secret.trim() })
      .then(() => {
        setCid('');
        setSecret('');
        setJsonText('');
        setEditClient(false);
        reload();
      })
      .catch((e) => say(errText(e)));
  };

  const login = () => {
    PhoneServices.googleLogin()
      .then(() => {
        setWaiting(true);
        const started = Date.now();
        if (pollRef.current) window.clearInterval(pollRef.current);
        pollRef.current = window.setInterval(() => {
          PhoneServices.googleLoginResult().then((r) => {
            if (r.result) done(r.result);
            else if (Date.now() - started > 10 * 60_000) {
              if (pollRef.current) window.clearInterval(pollRef.current);
              setWaiting(false);
            }
          }).catch(() => {});
        }, 2000);
      })
      .catch((e) => say(errText(e)));
  };

  const finish = () => {
    PhoneServices.googleFinish({ url: pasted.trim() }).then(done).catch((e) => say(errText(e)));
  };

  return (
    <ServiceCard title="Gmail" badge={badgeFor(st, 'gmail', g.connected, g.email, t)} help="services.google">
      <Moved {...props} source="gmail" />
      {(!g.client || editClient) && (
        <>
          <ol className="svc-steps">
            <li>{t('phoneSvc.gmailStep1')} <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">console.cloud.google.com</a></li>
            <li>{t('services.googleStep2')}</li>
            <li>{t('phoneSvc.gmailStep3')}</li>
          </ol>
          <div className="field">
            <input className="input" placeholder="client_id (…apps.googleusercontent.com)" value={cid} onChange={(e) => setCid(e.target.value)} />
            <input className="input" style={{ marginTop: 6 }} placeholder="client_secret" type="password" value={secret} onChange={(e) => setSecret(e.target.value)} />
            <textarea className="input svc-textarea" style={{ marginTop: 6 }} rows={2} placeholder={t('services.googleJson')} value={jsonText} onChange={(e) => setJsonText(e.target.value)} />
          </div>
          <div className="svc-actions">
            <button type="button" className="btn btn--primary" disabled={!jsonText.trim() && (!cid.trim() || !secret.trim())} onClick={saveClient}>
              {t('common.save')}
            </button>
            {editClient && <button type="button" className="btn btn--ghost" onClick={() => setEditClient(false)}>{t('common.cancel')}</button>}
          </div>
          <p className="field-hint">{t('phoneSvc.gmailImapAlt')}</p>
        </>
      )}
      {g.client && !editClient && (!g.connected || state.auth_error) && (
        <>
          <p className="field-hint">{t('services.googleReady', { id: g.client_id.slice(0, 18) + '…' })}</p>
          <div className="svc-actions">
            <button type="button" className="btn btn--primary" onClick={login}>{t('services.googleLogin')}</button>
            <button type="button" className="btn btn--ghost" onClick={() => setEditClient(true)}>{t('services.googleChangeClient')}</button>
          </div>
          {waiting && <p className="field-hint">{t('phoneSvc.gmailWaiting')}</p>}
          <div className="field" style={{ marginTop: 10 }}>
            <p className="field-hint">{t('phoneSvc.gmailPasteHint')}</p>
            <div className="svc-inline">
              <input className="input" placeholder="http://127.0.0.1:…/?state=…&code=…" value={pasted} onChange={(e) => setPasted(e.target.value)} />
              <button type="button" className="btn btn--ghost" disabled={!pasted.trim()} onClick={finish}>{t('services.googleFinish')}</button>
            </div>
          </div>
        </>
      )}
      {g.connected && !state.auth_error && (
        <>
          <ToggleRow label={t('services.gmailToggle')} hint={g.gmail ? undefined : t('services.scopeMissing')}
            checked={st.settings.services.gmail.enabled && g.gmail} disabled={!g.gmail}
            onChange={(v) => void PhoneServices.updateSettings({ services: { gmail: { enabled: v } } }).then(reload)} />
          <StateLine state={state} t={t} locale={locale} />
          <div className="svc-actions">
            <button type="button" className="btn btn--ghost" disabled={check.busy} onClick={check.run}>{t('services.check')}</button>
            <DisconnectButton account="google" name="Google" t={t} say={say} reload={reload} />
          </div>
          <CheckResult res={check.res} t={t} />
        </>
      )}
    </ServiceCard>
  );
}

/** Источник, который работает только через ядро на компьютере. */
function ComputerOnly({ title, hint, t }: { title: string; hint: string; t: T }) {
  return (
    <ServiceCard title={title} badge={{ text: t('phoneSvc.computerOnly'), on: false }} className="svc-card--computer">
      <p className="field-hint">{hint}</p>
    </ServiceCard>
  );
}

// ── Правила ─────────────────────────────────────────────────────────────

function RulesCard({ st, t, say, reload }: CardProps) {
  const [action, setAction] = useState<ServiceRule['action']>('never');
  const [source, setSource] = useState('any');
  const [match, setMatch] = useState('');
  const rules = st.settings.rules;
  const add = () => {
    PhoneServices.addRule({ action, source, match: match.trim() })
      .then(() => {
        setMatch('');
        reload();
      })
      .catch((e) => say(errText(e)));
  };
  return (
    <div className="card">
      <h2 className="card-title">
        {t('services.rules')}
        <InfoButton helpKey="services.rules" />
      </h2>
      <p className="field-hint">{t('phoneSvc.rulesHint')}</p>
      {rules.length > 0 && (
        <ul className="svc-list">
          {rules.map((r) => (
            <li key={r.id}>
              <span className={`badge svc-rule-${r.action}`}>{t(`services.action.${r.action}`)}</span>
              <span className="svc-list-text">«{r.match}»{r.source !== 'any' && <span className="field-hint"> · {t(`services.src.${r.source}`)}</span>}</span>
              <button type="button" className="btn btn--chip"
                onClick={() => PhoneServices.deleteRule({ id: r.id }).then(reload).catch((e) => say(errText(e)))}>
                {t('common.delete')}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="svc-rule-form">
        <Select value={action}
          options={(['urgent', 'always', 'digest', 'never'] as const).map((a) => ({ value: a, label: t(`services.action.${a}`) }))}
          onChange={(v) => setAction(v as ServiceRule['action'])} />
        <Select value={source}
          options={[{ value: 'any', label: t('services.src.any') }, ...RULE_SOURCES.map((s) => ({ value: s, label: t(`services.src.${s}`) }))]}
          onChange={setSource} />
        <input className="input" placeholder={t('services.rulePlaceholder')} value={match} maxLength={200}
          onChange={(e) => setMatch(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && match.trim() && add()} />
        <button type="button" className="btn btn--primary" disabled={!match.trim()} onClick={add}>{t('common.add')}</button>
      </div>
    </div>
  );
}

// ── Лента ───────────────────────────────────────────────────────────────

function FeedCard({ st, t, locale, say, reload }: CardProps) {
  const [open, setOpen] = useState<string | null>(null);
  const items = st.feed;
  return (
    <div className="card">
      <div className="card-title-row">
        <h2 className="card-title">{t('services.feed')}<InfoButton helpKey="services.feed" /></h2>
        {items.length > 0 && (
          <button type="button" className="btn btn--chip"
            onClick={async () => {
              if (!(await confirmDialog({ title: t('services.feedClearTitle'), message: t('phoneSvc.feedClearText'), confirmLabel: t('services.feedClear'), danger: true }))) return;
              PhoneServices.clearFeed().then(reload).catch((e) => say(errText(e)));
            }}>
            {t('services.feedClear')}
          </button>
        )}
      </div>
      {items.length === 0 && <p className="field-hint">{t('services.feedEmpty')}</p>}
      <ul className="svc-feed">
        {items.map((it) => (
          <li key={it.id} className={`svc-feed-item svc-level-${it.level ?? 'new'}`}>
            <div className="svc-feed-head" onClick={() => setOpen(open === it.id ? null : it.id)}>
              <span className="svc-feed-time">{timeLabel(it.ts, locale)}</span>
              <span className="badge">{t(`services.src.${it.source}`)}</span>
              <span className="svc-feed-who">{it.sender || it.group}</span>
              <span className="svc-feed-what">{it.summary || it.title}</span>
              <span className={`badge svc-lvl svc-lvl-${it.level ?? 'new'}`}>{it.level ? t(`services.level.${it.level}`) : t('services.level.new')}</span>
            </div>
            {open === it.id && (
              <div className="svc-feed-body">
                {it.title && it.title !== it.summary && <p className="svc-feed-title">{it.title}</p>}
                {it.text && <p className="svc-feed-text">{it.text}</p>}
                <p className="field-hint">
                  {t(`services.status.${it.status}`)}
                  {it.reason && <> · {t(`services.by.${it.by || 'signals'}`)}: {it.reason}</>}
                  {it.url && <> · <a href={it.url} target="_blank" rel="noreferrer">{t('services.open')}</a></>}
                </p>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
