import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import InfoButton from '../components/InfoButton';
import Select from '../components/Select';
import type { HelpKey } from '../helpTexts';
import { api, ApiError, getApiUrl, isLinkMode } from '../api';
import type {
  ServiceCheck,
  ServiceFeedItem,
  ServiceLevel,
  ServiceRule,
  ServicesNotify,
  ServicesStatus,
  ServiceState,
} from '../api';
import { useApiOnline } from '../apiData';
import { confirmDialog } from '../dialogStore';
import { useI18n } from '../i18n';

/* «Сервисы»: внешние источники событий (Gmail и Google Диск, GitHub,
   Telegram-аккаунт, почта по IMAP, RSS, вебхук) и уведомления о них от
   персоны. Разбор «сообщать или нет» идёт в ядре (app/integrations):
   здесь — подключения, настройки уведомлений, правила и лента событий с
   оценками «как надо было» (по ним учится разбор). Статус и лента
   перечитываются, пока вкладка видна. */

const POLL_MS = 15_000;
const LEVELS: ServiceLevel[] = ['urgent', 'notify', 'digest', 'ignore'];
const SOURCES = ['gmail', 'drive', 'github', 'telegram', 'imap', 'rss', 'webhook'] as const;

type T = (key: string, vars?: Record<string, string | number>) => string;

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

function timeLabel(ts: number | null | undefined, locale: string): string {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString(locale, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function Switch({ checked, onChange, title, disabled }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  title?: string;
  disabled?: boolean;
}) {
  return (
    <label className="switch" title={title}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch-slider" />
    </label>
  );
}

function ToggleRow({ label, hint, checked, onChange, disabled }: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="svc-toggle">
      <Switch checked={checked} onChange={onChange} title={label} disabled={disabled} />
      <div>
        <div className="svc-toggle-label">{label}</div>
        {hint && <div className="field-hint" style={{ marginTop: 2 }}>{hint}</div>}
      </div>
    </div>
  );
}

function StateLine({ state, t, locale }: { state?: ServiceState; t: T; locale: string }) {
  if (!state) return null;
  if (state.auth_error) return <p className="svc-error">{t('services.authLost')}</p>;
  if (state.last_error) return <p className="svc-error">{t('services.lastError', { error: state.last_error })}</p>;
  if (state.last_ok) return <p className="field-hint">{t('services.lastOk', { time: timeLabel(state.last_ok, locale) })}</p>;
  return <p className="field-hint">{t('services.notChecked')}</p>;
}

function CheckResult({ res, t }: { res: ServiceCheck | null; t: T }) {
  if (!res) return null;
  if (res.ok) return <p className="svc-ok">{t('services.checkOk', { account: res.account ?? '' })}</p>;
  return <p className="svc-error">{res.error || t('services.checkFail')}</p>;
}

function ServiceCard({ title, badge, children, help }: {
  title: string;
  badge: { text: string; on: boolean; warn?: boolean };
  children: ReactNode;
  help?: HelpKey;
}) {
  return (
    <div className="card svc-card">
      <div className="card-title-row">
        <h3 className="card-title">
          {title}
          {help && <InfoButton helpKey={help} />}
        </h3>
        <span className={'badge' + (badge.warn ? ' svc-badge-warn' : badge.on ? ' badge--success' : ' badge--muted')}>
          {badge.text}
        </span>
      </div>
      {children}
    </div>
  );
}

export default function Services() {
  const { t, lang } = useI18n();
  const locale = lang === 'ru' ? 'ru-RU' : 'en-US';
  const online = useApiOnline();
  const [st, setSt] = useState<ServicesStatus | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [feedItems, setFeedItems] = useState<ServiceFeedItem[]>([]);
  const [feedSource, setFeedSource] = useState<string>('');
  const [flash, setFlash] = useState<string | null>(null);
  const loadRef = useRef<() => void>(() => {});

  const load = useCallback(() => {
    api.getServices()
      .then((s) => {
        setSt(s);
        setLoadErr(null);
      })
      .catch((e) => setLoadErr(errText(e)));
    api.getServicesFeed(80, feedSource || undefined)
      .then((r) => setFeedItems(r.items))
      .catch(() => {});
  }, [feedSource]);
  loadRef.current = load;

  useEffect(() => {
    if (!online) return;
    loadRef.current();
    const tick = () => {
      if (document.visibilityState === 'visible') loadRef.current();
    };
    const timer = setInterval(tick, POLL_MS);
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [online, feedSource]);

  const say = (msg: string) => {
    setFlash(msg);
    window.setTimeout(() => setFlash((cur) => (cur === msg ? null : cur)), 6000);
  };

  const saveNotify = (patch: Partial<ServicesNotify>) => {
    setSt((cur) => cur && { ...cur, settings: { ...cur.settings, notify: { ...cur.settings.notify, ...patch } } });
    api.putServicesSettings({ notify: patch }).then(() => load()).catch((e) => say(errText(e)));
  };

  const saveService = (name: string, patch: Record<string, unknown>) => {
    setSt((cur) => {
      if (!cur) return cur;
      const services = { ...cur.settings.services } as Record<string, Record<string, unknown>>;
      services[name] = { ...services[name], ...patch };
      return { ...cur, settings: { ...cur.settings, services: services as unknown as ServicesStatus['settings']['services'] } };
    });
    api.putServicesSettings({ services: { [name]: patch } }).then(() => load()).catch((e) => say(errText(e)));
  };

  if (!online) {
    return (
      <div className="section services">
        <Header t={t} />
        <div className="card"><p className="field-hint">{t('services.needCore')}</p></div>
      </div>
    );
  }
  if (!st) {
    return (
      <div className="section services">
        <Header t={t} />
        <div className="card"><p className="field-hint">{loadErr ?? t('services.loading')}</p></div>
      </div>
    );
  }

  return (
    <div className="section services">
      <Header t={t} />
      {flash && <div className="svc-flash" role="status">{flash}</div>}
      {!st.hub.active && st.hub.started && <div className="card svc-warn">{t('services.standby')}</div>}

      <Overview st={st} t={t} locale={locale} say={say} reload={load} />
      <NotifyCard st={st} t={t} save={saveNotify} />

      <h2 className="svc-group-title">{t('services.connections')}</h2>
      <div className="svc-grid">
        <GoogleCard st={st} t={t} locale={locale} say={say} reload={load} saveService={saveService} />
        <GithubCard st={st} t={t} locale={locale} say={say} reload={load} saveService={saveService} />
        <TelegramCard st={st} t={t} locale={locale} say={say} reload={load} saveService={saveService} />
        <ImapCard st={st} t={t} locale={locale} say={say} reload={load} saveService={saveService} />
        <RssCard st={st} t={t} locale={locale} say={say} reload={load} saveService={saveService} />
        <WebhookCard st={st} t={t} say={say} reload={load} saveService={saveService} />
      </div>

      <RulesCard st={st} t={t} say={say} reload={load} />
      <FeedCard
        items={feedItems}
        source={feedSource}
        setSource={setFeedSource}
        t={t}
        locale={locale}
        say={say}
        reload={load}
      />
    </div>
  );
}

function Header({ t }: { t: T }) {
  return (
    <div className="section-header">
      <div>
        <h1 className="section-title">{t('nav.services')}</h1>
        <p className="section-subtitle">
          {t('services.subtitle')}
          <InfoButton helpKey="services.about" />
        </p>
      </div>
    </div>
  );
}

// ── Сводка и «не беспокоить» ────────────────────────────────────────────

function Overview({ st, t, locale, say, reload }: { st: ServicesStatus; t: T; locale: string; say: (m: string) => void; reload: () => void }) {
  const [busy, setBusy] = useState(false);
  const dnd = st.settings.dnd_until > st.now;
  const persona = st.personas.find((p) => p.id === st.persona);
  const run = (p: Promise<unknown>, ok: string) => {
    setBusy(true);
    p.then(() => {
      say(ok);
      reload();
    })
      .catch((e) => say(errText(e)))
      .finally(() => setBusy(false));
  };
  return (
    <div className="card svc-overview">
      <div className="svc-stats">
        <div><span className="svc-stat-num">{st.counts.today}</span><span className="svc-stat-label">{t('services.statToday')}</span></div>
        <div><span className="svc-stat-num">{st.counts.notified_today}</span><span className="svc-stat-label">{t('services.statNotified')}</span></div>
        <div><span className="svc-stat-num">{st.counts.digest}</span><span className="svc-stat-label">{t('services.statDigest')}</span></div>
        <div><span className="svc-stat-num">{st.counts.queued}</span><span className="svc-stat-label">{t('services.statQueued')}</span></div>
      </div>
      <p className="field-hint">
        {persona ? t('services.whoNotifies', { name: persona.name }) : t('services.noPersona')}
        {persona?.muted && <> · <span className="svc-error-inline">{t('services.personaMuted')}</span></>}
      </p>
      <div className="svc-actions">
        <button type="button" className="btn btn--ghost" disabled={busy}
          onClick={() => run(api.testServicesNotify().then((r) => {
            if (!r.ok) throw new Error(r.detail === 'busy' ? t('services.testBusy') : t('services.testFail'));
          }), t('services.testSent'))}>
          {t('services.test')}
        </button>
        <button type="button" className="btn btn--ghost" disabled={busy || st.counts.digest === 0}
          onClick={() => run(api.servicesDigestNow(), t('services.digestSent'))}>
          {t('services.digestNow')}
        </button>
        {dnd ? (
          <>
            <span className="badge badge--active">{t('services.dndUntil', { time: timeLabel(st.settings.dnd_until, locale) })}</span>
            <button type="button" className="btn btn--ghost" disabled={busy}
              onClick={() => run(api.setServicesDnd(0), t('services.dndOff'))}>
              {t('services.dndStop')}
            </button>
          </>
        ) : (
          <>
            <button type="button" className="btn btn--ghost" disabled={busy}
              onClick={() => run(api.setServicesDnd(60), t('services.dndOn'))}>
              {t('services.dnd1h')}
            </button>
            <button type="button" className="btn btn--ghost" disabled={busy}
              onClick={() => run(api.setServicesDnd(180), t('services.dndOn'))}>
              {t('services.dnd3h')}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// ── Настройки уведомлений ───────────────────────────────────────────────

function NotifyCard({ st, t, save }: { st: ServicesStatus; t: T; save: (p: Partial<ServicesNotify>) => void }) {
  const n = st.settings.notify;
  const [interests, setInterests] = useState(n.interests);
  const [digest, setDigest] = useState(n.digest_times.join(', '));
  useEffect(() => setInterests(n.interests), [n.interests]);
  useEffect(() => setDigest(n.digest_times.join(', ')), [n.digest_times]);
  const tgMissing = !st.telegram_delivery.token || !st.telegram_delivery.owner;
  return (
    <div className="card">
      <h2 className="card-title">{t('services.notifyTitle')}</h2>
      <div className="field-grid">
        <div>
          <label className="field-label" htmlFor="svc-persona">{t('services.persona')}</label>
          <Select
            id="svc-persona"
            value={n.persona ?? ''}
            options={[
              { value: '', label: t('services.personaAuto') },
              ...st.personas.map((p) => ({ value: p.id, label: p.name })),
            ]}
            onChange={(v) => save({ persona: v || null })}
          />
        </div>
        <div>
          <label className="field-label" htmlFor="svc-engine">{t('services.engine')}</label>
          <Select
            id="svc-engine"
            value={n.engine}
            options={(['local', 'persona', 'rules'] as const).map((e) => ({ value: e, label: t(`services.engine.${e}`) }))}
            onChange={(v) => save({ engine: v as ServicesNotify['engine'] })}
          />
        </div>
        <div>
          <label className="field-label" htmlFor="svc-style">{t('services.style')}</label>
          <Select
            id="svc-style"
            value={n.style}
            options={(['plain', 'persona'] as const).map((e) => ({ value: e, label: t(`services.style.${e}`) }))}
            onChange={(v) => save({ style: v as ServicesNotify['style'] })}
          />
        </div>
      </div>
      <p className="field-hint">{t(`services.engineHint.${n.engine}`)}</p>

      <div className="field" style={{ marginTop: 14 }}>
        <label className="field-label">{t('services.channels')}</label>
        <ToggleRow label={t('services.channelWeb')} checked={n.channels.web}
          onChange={(v) => save({ channels: { ...n.channels, web: v } })} />
        <ToggleRow label={t('services.channelTelegram')}
          hint={tgMissing ? t('services.channelTelegramMissing') : undefined}
          checked={n.channels.telegram}
          onChange={(v) => save({ channels: { ...n.channels, telegram: v } })} />
      </div>

      <div className="field">
        <label className="field-label" htmlFor="svc-interests">{t('services.interests')}</label>
        <textarea
          id="svc-interests"
          className="input svc-textarea"
          rows={3}
          maxLength={2000}
          placeholder={t('services.interestsPlaceholder')}
          value={interests}
          onChange={(e) => setInterests(e.target.value)}
          onBlur={() => interests !== n.interests && save({ interests })}
        />
        <p className="field-hint">{t('services.interestsHint')}</p>
      </div>

      <div className="field">
        <label className="field-label">{t('services.quiet')}</label>
        <div className="svc-inline">
          <Switch checked={n.quiet.enabled} onChange={(v) => save({ quiet: { ...n.quiet, enabled: v } })} />
          <input type="time" className="input svc-time" value={n.quiet.from} disabled={!n.quiet.enabled}
            onChange={(e) => e.target.value && save({ quiet: { ...n.quiet, from: e.target.value } })} />
          <span>—</span>
          <input type="time" className="input svc-time" value={n.quiet.to} disabled={!n.quiet.enabled}
            onChange={(e) => e.target.value && save({ quiet: { ...n.quiet, to: e.target.value } })} />
        </div>
        <ToggleRow label={t('services.quietUrgent')} checked={n.quiet.urgent}
          onChange={(v) => save({ quiet: { ...n.quiet, urgent: v } })} />
      </div>

      <div className="field-grid">
        <div>
          <label className="field-label" htmlFor="svc-digest">{t('services.digestTimes')}</label>
          <input id="svc-digest" className="input" value={digest} placeholder="09:00, 19:00"
            onChange={(e) => setDigest(e.target.value)}
            onBlur={() => save({ digest_times: digest.split(/[,\s;]+/).filter(Boolean) })} />
        </div>
        <div>
          <label className="field-label" htmlFor="svc-rate">{t('services.maxPerHour')}</label>
          <input id="svc-rate" className="input" type="number" min={1} max={60} value={n.max_per_hour}
            onChange={(e) => {
              const v = Number(e.target.value);
              if (v >= 1 && v <= 60) save({ max_per_hour: v });
            }} />
        </div>
        <div>
          <label className="field-label" htmlFor="svc-lang">{t('services.language')}</label>
          <Select
            id="svc-lang"
            value={n.language}
            options={(['auto', 'ru', 'en'] as const).map((e) => ({ value: e, label: t(`services.language.${e}`) }))}
            onChange={(v) => save({ language: v as ServicesNotify['language'] })}
          />
        </div>
      </div>
      <p className="field-hint">{t('services.digestHint')}</p>
    </div>
  );
}

// ── Подключения ─────────────────────────────────────────────────────────

interface CardProps {
  st: ServicesStatus;
  t: T;
  locale: string;
  say: (m: string) => void;
  reload: () => void;
  saveService: (name: string, patch: Record<string, unknown>) => void;
}

function useCheck(service: string) {
  const [res, setRes] = useState<ServiceCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const run = () => {
    setBusy(true);
    api.checkService(service)
      .then(setRes)
      .catch((e) => setRes({ ok: false, error: errText(e) }))
      .finally(() => setBusy(false));
  };
  return { res, busy, run };
}

async function confirmDisconnect(t: T, name: string): Promise<boolean> {
  return confirmDialog({
    title: t('services.disconnectTitle', { name }),
    message: t('services.disconnectText'),
    confirmLabel: t('services.disconnect'),
    danger: true,
  });
}

function DisconnectButton({ account, name, t, say, reload }: { account: string; name: string; t: T; say: (m: string) => void; reload: () => void }) {
  return (
    <button type="button" className="btn btn--ghost"
      onClick={async () => {
        if (!(await confirmDisconnect(t, name))) return;
        api.disconnectService(account).then(() => {
          say(t('services.disconnected', { name }));
          reload();
        }).catch((e) => say(errText(e)));
      }}>
      {t('services.disconnect')}
    </button>
  );
}

function GoogleCard({ st, t, locale, say, reload, saveService }: CardProps) {
  const g = st.accounts.google;
  const s = st.settings.services;
  const [cid, setCid] = useState('');
  const [secret, setSecret] = useState('');
  const [jsonText, setJsonText] = useState('');
  const [waiting, setWaiting] = useState(false);
  const [pasted, setPasted] = useState('');
  const [editClient, setEditClient] = useState(false);
  const check = useCheck('gmail');
  const pollRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (pollRef.current) window.clearInterval(pollRef.current);
  }, []);

  const saveClient = () => {
    api.setGoogleClient(jsonText.trim() ? { json_text: jsonText } : { client_id: cid.trim(), client_secret: secret.trim() })
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
    api.startGoogleLogin()
      .then(({ url }) => {
        window.open(url, '_blank', 'noopener');
        setWaiting(true);
        const started = Date.now();
        if (pollRef.current) window.clearInterval(pollRef.current);
        pollRef.current = window.setInterval(() => {
          api.googleLoginResult().then((r) => {
            if (r.result || Date.now() - started > 10 * 60_000) {
              if (pollRef.current) window.clearInterval(pollRef.current);
              setWaiting(false);
              if (r.result?.ok) {
                say(t('services.googleConnected', { email: r.result.email ?? '' }));
                saveService('gmail', { enabled: true });
              } else if (r.result) say(r.result.error ?? t('services.checkFail'));
              reload();
            }
          }).catch(() => {});
        }, 2000);
      })
      .catch((e) => say(errText(e)));
  };

  const finish = () => {
    api.finishGoogleLogin(pasted.trim()).then((r) => {
      if (r.ok) {
        setPasted('');
        setWaiting(false);
        say(t('services.googleConnected', { email: r.email ?? '' }));
        saveService('gmail', { enabled: true });
      } else say(r.error ?? t('services.checkFail'));
      reload();
    }).catch((e) => say(errText(e)));
  };

  const badge = g.auth_error
    ? { text: t('services.badgeReauth'), on: false, warn: true }
    : g.connected ? { text: g.email || t('services.badgeOn'), on: true } : { text: t('services.badgeOff'), on: false };

  return (
    <ServiceCard title={t('services.google')} badge={badge} help="services.google">
      {(!g.client || editClient) && (
        <>
          <ol className="svc-steps">
            <li>{t('services.googleStep1')} <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">console.cloud.google.com</a></li>
            <li>{t('services.googleStep2')}</li>
            <li>{t('services.googleStep3')}</li>
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
        </>
      )}
      {g.client && !editClient && (!g.connected || g.auth_error) && (
        <>
          <p className="field-hint">{t('services.googleReady', { id: g.client_id.slice(0, 18) + '…' })}</p>
          <div className="svc-actions">
            <button type="button" className="btn btn--primary" onClick={login}>{t('services.googleLogin')}</button>
            <button type="button" className="btn btn--ghost" onClick={() => setEditClient(true)}>{t('services.googleChangeClient')}</button>
          </div>
          {(waiting || isLinkMode()) && (
            <div className="field" style={{ marginTop: 10 }}>
              <p className="field-hint">{waiting ? t('services.googleWaiting') : ''} {t('services.googlePasteHint')}</p>
              <div className="svc-inline">
                <input className="input" placeholder="http://127.0.0.1:…/?state=…&code=…" value={pasted} onChange={(e) => setPasted(e.target.value)} />
                <button type="button" className="btn btn--ghost" disabled={!pasted.trim()} onClick={finish}>{t('services.googleFinish')}</button>
              </div>
            </div>
          )}
        </>
      )}
      {g.connected && !g.auth_error && (
        <>
          <ToggleRow label={t('services.gmailToggle')} hint={g.gmail ? undefined : t('services.scopeMissing')}
            checked={s.gmail.enabled && g.gmail} disabled={!g.gmail}
            onChange={(v) => saveService('gmail', { enabled: v })} />
          <StateLine state={st.status.gmail} t={t} locale={locale} />
          <ToggleRow label={t('services.driveToggle')} hint={g.drive ? t('services.driveHint') : t('services.scopeMissing')}
            checked={s.drive.enabled && g.drive} disabled={!g.drive}
            onChange={(v) => saveService('drive', { enabled: v })} />
          {s.drive.enabled && (
            <div className="svc-sub">
              <ToggleRow label={t('services.driveShared')} checked={s.drive.shared} onChange={(v) => saveService('drive', { shared: v })} />
              <ToggleRow label={t('services.driveEdits')} checked={s.drive.edits} onChange={(v) => saveService('drive', { edits: v })} />
              <StateLine state={st.status.drive} t={t} locale={locale} />
            </div>
          )}
          <p className="field-hint">{t('services.googleChat')}</p>
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

function GithubCard({ st, t, locale, say, reload, saveService }: CardProps) {
  const gh = st.accounts.github;
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const check = useCheck('github');
  const connect = () => {
    setBusy(true);
    api.setGithubToken(token.trim())
      .then((r) => {
        if (r.ok) {
          setToken('');
          say(t('services.githubConnected', { login: r.login ?? '' }));
        } else say(r.error ?? t('services.checkFail'));
        reload();
      })
      .catch((e) => say(errText(e)))
      .finally(() => setBusy(false));
  };
  const state = st.status.github;
  const badge = state.auth_error
    ? { text: t('services.badgeReauth'), on: false, warn: true }
    : gh.connected ? { text: gh.login || t('services.badgeOn'), on: true } : { text: t('services.badgeOff'), on: false };
  return (
    <ServiceCard title="GitHub" badge={badge} help="services.github">
      {(!gh.connected || state.auth_error) && (
        <>
          <p className="field-hint">
            {t('services.githubHint')}{' '}
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
          <ToggleRow label={t('services.githubToggle')} checked={st.settings.services.github.enabled}
            onChange={(v) => saveService('github', { enabled: v })} />
          <StateLine state={state} t={t} locale={locale} />
          <p className="field-hint">{t('services.githubChat')}</p>
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

function TelegramCard({ st, t, locale, say, reload, saveService }: CardProps) {
  const tg = st.accounts.telegram;
  const opts = st.settings.services.telegram;
  const [apiId, setApiId] = useState('');
  const [apiHash, setApiHash] = useState('');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [step, setStep] = useState<'start' | 'code' | 'password'>(tg.login_step ?? 'start');
  const [busy, setBusy] = useState(false);
  const after = (r: { ok: boolean; next?: 'code' | 'password'; error?: string; user?: { name: string } }) => {
    if (!r.ok) {
      say(r.error ?? t('services.checkFail'));
      return;
    }
    if (r.next) setStep(r.next);
    if (r.user) {
      setStep('start');
      setCode('');
      setPassword('');
      say(t('services.telegramConnected', { name: r.user.name }));
      reload();
    }
  };
  const go = (p: Promise<{ ok: boolean; next?: 'code' | 'password'; error?: string; user?: { name: string } }>) => {
    setBusy(true);
    p.then(after).catch((e) => say(errText(e))).finally(() => setBusy(false));
  };
  const state = st.status.telegram;
  const badge = state.auth_error
    ? { text: t('services.badgeReauth'), on: false, warn: true }
    : tg.connected ? { text: tg.user?.name || t('services.badgeOn'), on: true } : { text: t('services.badgeOff'), on: false };

  return (
    <ServiceCard title="Telegram" badge={badge} help="services.telegram">
      {!tg.available && <p className="svc-error">{t('services.telegramNoLib')}</p>}
      {tg.available && (!tg.connected || state.auth_error) && (
        <>
          <p className="field-hint">
            {t('services.telegramHint')}{' '}
            <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">my.telegram.org/apps</a>
          </p>
          {step === 'start' && (
            <>
              <div className="field-grid">
                <input className="input" placeholder="api_id" inputMode="numeric" value={apiId} onChange={(e) => setApiId(e.target.value)} />
                <input className="input" placeholder="api_hash" value={apiHash} onChange={(e) => setApiHash(e.target.value)} />
              </div>
              <div className="svc-inline" style={{ marginTop: 6 }}>
                <input className="input" placeholder="+7 900 000-00-00" inputMode="tel" value={phone} onChange={(e) => setPhone(e.target.value)} />
                <button type="button" className="btn btn--primary"
                  disabled={busy || !/^\d+$/.test(apiId.trim()) || apiHash.trim().length < 16 || phone.trim().length < 6}
                  onClick={() => go(api.telegramLogin({ api_id: Number(apiId.trim()), api_hash: apiHash.trim(), phone: phone.trim() }))}>
                  {t('services.telegramSendCode')}
                </button>
              </div>
            </>
          )}
          {step === 'code' && (
            <div className="svc-inline">
              <input className="input" placeholder={t('services.telegramCode')} inputMode="numeric" value={code} onChange={(e) => setCode(e.target.value)} />
              <button type="button" className="btn btn--primary" disabled={busy || code.trim().length < 3} onClick={() => go(api.telegramCode(code.trim()))}>
                {t('services.telegramSignIn')}
              </button>
              <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => setStep('start')}>{t('common.cancel')}</button>
            </div>
          )}
          {step === 'password' && (
            <div className="svc-inline">
              <input className="input" type="password" placeholder={t('services.telegramPassword')} value={password} onChange={(e) => setPassword(e.target.value)} />
              <button type="button" className="btn btn--primary" disabled={busy || !password} onClick={() => go(api.telegramPassword(password))}>
                {t('services.telegramSignIn')}
              </button>
            </div>
          )}
          <p className="field-hint">{t('services.telegramPrivacy')}</p>
        </>
      )}
      {tg.connected && !state.auth_error && (
        <>
          <ToggleRow label={t('services.telegramToggle')} checked={opts.enabled} onChange={(v) => saveService('telegram', { enabled: v })} />
          {opts.enabled && (
            <div className="svc-sub">
              <ToggleRow label={t('services.tgPrivate')} checked={opts.private} onChange={(v) => saveService('telegram', { private: v })} />
              <ToggleRow label={t('services.tgMentions')} checked={opts.mentions} onChange={(v) => saveService('telegram', { mentions: v })} />
              <ToggleRow label={t('services.tgGroups')} checked={opts.groups} onChange={(v) => saveService('telegram', { groups: v })} />
              <ToggleRow label={t('services.tgChannels')} checked={opts.channels} onChange={(v) => saveService('telegram', { channels: v })} />
              <ToggleRow label={t('services.tgSkipRead')} checked={opts.skip_read} onChange={(v) => saveService('telegram', { skip_read: v })} />
              <div style={{ marginTop: 8 }}>
                <label className="field-label" htmlFor="svc-tg-muted">{t('services.tgMuted')}</label>
                <Select id="svc-tg-muted" value={opts.muted}
                  options={(['digest', 'ignore', 'normal'] as const).map((m) => ({ value: m, label: t(`services.tgMuted.${m}`) }))}
                  onChange={(v) => saveService('telegram', { muted: v })} />
              </div>
            </div>
          )}
          <StateLine state={state} t={t} locale={locale} />
          <div className="svc-actions">
            <DisconnectButton account="telegram" name="Telegram" t={t} say={say} reload={reload} />
          </div>
        </>
      )}
    </ServiceCard>
  );
}

function ImapCard({ st, t, locale, say, reload, saveService }: CardProps) {
  const im = st.accounts.imap;
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [host, setHost] = useState('');
  const [busy, setBusy] = useState(false);
  const check = useCheck('imap');
  const preset = () => {
    if (!user.includes('@') || host) return;
    api.imapPreset(user.trim()).then((p) => p.host && setHost(p.host)).catch(() => {});
  };
  const connect = () => {
    setBusy(true);
    api.connectImap({ user: user.trim(), password, host: host.trim() || undefined })
      .then((r) => {
        if (r.ok) {
          setPassword('');
          say(t('services.imapConnected', { host: r.host ?? '' }));
        } else say(r.error ?? t('services.checkFail'));
        reload();
      })
      .catch((e) => say(errText(e)))
      .finally(() => setBusy(false));
  };
  const state = st.status.imap;
  const badge = state.auth_error
    ? { text: t('services.badgeReauth'), on: false, warn: true }
    : im.connected ? { text: im.user, on: true } : { text: t('services.badgeOff'), on: false };
  return (
    <ServiceCard title={t('services.imap')} badge={badge} help="services.imap">
      {(!im.connected || state.auth_error) && (
        <>
          <p className="field-hint">{t('services.imapHint')}</p>
          <input className="input" placeholder="name@yandex.ru" value={user} onChange={(e) => setUser(e.target.value)} onBlur={preset} />
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
          <ToggleRow label={t('services.imapToggle')} checked={st.settings.services.imap.enabled} onChange={(v) => saveService('imap', { enabled: v })} />
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

function RssCard({ st, t, locale, say, saveService }: CardProps) {
  const rss = st.settings.services.rss;
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const add = () => {
    const u = url.trim();
    setBusy(true);
    api.checkRssFeed(u)
      .then((r) => {
        const f = r.feeds?.[0];
        if (!r.ok || !f) {
          say(f?.error ?? r.error ?? t('services.rssBad'));
          return;
        }
        setUrl('');
        saveService('rss', { feeds: [...rss.feeds, { url: u, title: f.title ?? '' }], enabled: true });
        say(t('services.rssAdded', { title: f.title || u, n: f.items ?? 0 }));
      })
      .catch((e) => say(errText(e)))
      .finally(() => setBusy(false));
  };
  const badge = rss.feeds.length
    ? { text: t('services.rssCount', { n: rss.feeds.length }), on: rss.enabled }
    : { text: t('services.badgeOff'), on: false };
  return (
    <ServiceCard title={t('services.rss')} badge={badge} help="services.rss">
      <p className="field-hint">{t('services.rssHint')}</p>
      {rss.feeds.length > 0 && (
        <>
          <ToggleRow label={t('services.rssToggle')} checked={rss.enabled} onChange={(v) => saveService('rss', { enabled: v })} />
          <ul className="svc-list">
            {rss.feeds.map((f) => (
              <li key={f.url}>
                <span className="svc-list-text" title={f.url}>{f.title || f.url}</span>
                <button type="button" className="btn btn--chip"
                  onClick={() => saveService('rss', { feeds: rss.feeds.filter((x) => x.url !== f.url) })}>
                  {t('common.delete')}
                </button>
              </li>
            ))}
          </ul>
          <StateLine state={st.status.rss} t={t} locale={locale} />
        </>
      )}
      <div className="svc-inline">
        <input className="input" placeholder="https://…/feed.xml" value={url} onChange={(e) => setUrl(e.target.value)} />
        <button type="button" className="btn btn--ghost" disabled={busy || !/^https?:\/\/\S+$/.test(url.trim())} onClick={add}>
          {t('common.add')}
        </button>
      </div>
    </ServiceCard>
  );
}

function WebhookCard({ st, t, say, reload, saveService }: Omit<CardProps, 'locale'>) {
  const wh = st.accounts.webhook;
  const on = st.settings.services.webhook.enabled;
  const full = wh.path && !wh.path.includes('…') ? `${getApiUrl()}${wh.path}` : '';
  const regen = async () => {
    if (wh.token && !(await confirmDialog({
      title: t('services.webhookNewTitle'),
      message: t('services.webhookNewText'),
      confirmLabel: t('services.webhookNew'),
    }))) return;
    api.regenerateWebhook().then(() => reload()).catch((e) => say(errText(e)));
  };
  return (
    <ServiceCard title={t('services.webhook')} badge={on && wh.token ? { text: t('services.badgeOn'), on: true } : { text: t('services.badgeOff'), on: false }} help="services.webhook">
      <p className="field-hint">{t('services.webhookHint')}</p>
      {wh.token && (
        <>
          <ToggleRow label={t('services.webhookToggle')} checked={on} onChange={(v) => saveService('webhook', { enabled: v })} />
          {full ? (
            <>
              <div className="svc-inline">
                <input className="input" readOnly value={full} onFocus={(e) => e.target.select()} />
                <button type="button" className="btn btn--ghost"
                  onClick={() => navigator.clipboard?.writeText(full).then(() => say(t('common.copied')))}>
                  {t('common.copy')}
                </button>
              </div>
              <pre className="svc-code">{`curl -X POST ${full} \\\n  -H 'Content-Type: application/json' \\\n  -d '{"title": "Backup failed", "text": "disk full", "priority": "high"}'`}</pre>
            </>
          ) : (
            <p className="field-hint">{t('services.webhookHidden')}</p>
          )}
        </>
      )}
      <div className="svc-actions">
        <button type="button" className="btn btn--ghost" onClick={regen}>
          {wh.token ? t('services.webhookNew') : t('services.webhookCreate')}
        </button>
        {wh.token && <DisconnectButton account="webhook" name={t('services.webhook')} t={t} say={say} reload={reload} />}
      </div>
    </ServiceCard>
  );
}

// ── Правила ─────────────────────────────────────────────────────────────

function RulesCard({ st, t, say, reload }: { st: ServicesStatus; t: T; say: (m: string) => void; reload: () => void }) {
  const [action, setAction] = useState<ServiceRule['action']>('never');
  const [source, setSource] = useState('any');
  const [match, setMatch] = useState('');
  const rules = st.settings.rules;
  const add = () => {
    api.addServiceRule({ action, source, match: match.trim() })
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
      <p className="field-hint">{t('services.rulesHint')}</p>
      {rules.length > 0 && (
        <ul className="svc-list">
          {rules.map((r) => (
            <li key={r.id}>
              <span className={`badge svc-rule-${r.action}`}>{t(`services.action.${r.action}`)}</span>
              <span className="svc-list-text">«{r.match}»{r.source !== 'any' && <span className="field-hint"> · {t(`services.src.${r.source}`)}</span>}</span>
              <button type="button" className="btn btn--chip"
                onClick={() => api.deleteServiceRule(r.id).then(reload).catch((e) => say(errText(e)))}>
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
          options={[{ value: 'any', label: t('services.src.any') }, ...SOURCES.map((s) => ({ value: s, label: t(`services.src.${s}`) }))]}
          onChange={setSource} />
        <input className="input" placeholder={t('services.rulePlaceholder')} value={match} maxLength={200}
          onChange={(e) => setMatch(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && match.trim() && add()} />
        <button type="button" className="btn btn--primary" disabled={!match.trim()} onClick={add}>{t('common.add')}</button>
      </div>
      {st.settings.examples.length > 0 && (
        <p className="field-hint" style={{ marginTop: 10 }}>
          {t('services.examples', { n: st.settings.examples.length })}{' '}
          <button type="button" className="btn btn--chip"
            onClick={() => api.clearServiceExamples().then(reload).catch((e) => say(errText(e)))}>
            {t('services.examplesClear')}
          </button>
        </p>
      )}
    </div>
  );
}

// ── Лента ───────────────────────────────────────────────────────────────

function FeedCard({ items, source, setSource, t, locale, say, reload }: {
  items: ServiceFeedItem[];
  source: string;
  setSource: (s: string) => void;
  t: T;
  locale: string;
  say: (m: string) => void;
  reload: () => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const rate = (it: ServiceFeedItem, level: ServiceLevel) => {
    api.rateServiceItem(it.id, level)
      .then((r) => {
        say(r.rule
          ? t('services.ratedRule', { match: r.rule.match, action: t(`services.action.${r.rule.action}`) })
          : t('services.rated'));
        reload();
      })
      .catch((e) => say(errText(e)));
  };
  return (
    <div className="card">
      <div className="card-title-row">
        <h2 className="card-title">{t('services.feed')}<InfoButton helpKey="services.feed" /></h2>
        {items.length > 0 && (
          <button type="button" className="btn btn--chip"
            onClick={async () => {
              if (!(await confirmDialog({ title: t('services.feedClearTitle'), message: t('services.feedClearText'), confirmLabel: t('services.feedClear'), danger: true }))) return;
              api.clearServicesFeed().then(reload).catch((e) => say(errText(e)));
            }}>
            {t('services.feedClear')}
          </button>
        )}
      </div>
      <div className="badge-row" style={{ marginBottom: 12 }}>
        {['', ...SOURCES].map((s) => (
          <button key={s || 'all'} type="button" className={'btn btn--chip' + (source === s ? ' svc-chip-on' : '')} onClick={() => setSource(s)}>
            {s ? t(`services.src.${s}`) : t('services.src.all')}
          </button>
        ))}
      </div>
      {items.length === 0 && <p className="field-hint">{t('services.feedEmpty')}</p>}
      <ul className="svc-feed">
        {items.map((it) => {
          const lvl = it.feedback ?? it.level;
          return (
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
                  <div className="svc-rate">
                    <span className="field-hint">{t('services.rateAsk')}</span>
                    {LEVELS.map((l) => (
                      <button key={l} type="button" className={'btn btn--chip' + (lvl === l && it.feedback ? ' svc-chip-on' : '')} onClick={() => rate(it, l)}>
                        {t(`services.level.${l}`)}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
