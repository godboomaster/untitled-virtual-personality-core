import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { confirmDialog } from '../dialogStore';
import { pluginCode } from './plugin';
import { onWebchatStatus, setWebchatPrefs, useWebchatPrefs, WebChat, webchatReason } from './webchat';
import type { WebchatSiteStatus } from './webchat';
import { orderedSites } from './webchatSites';
import type { WebchatId, WebchatSite } from './webchatSites';

/* «Провайдеры» → «Веб-чаты без ключей»: какие сайты отвечают за персон без
   ядра (место в цепочке — у каждой персоны своё: досье → «Настройки»;
   включённый встаёт туда до Gemma). По умолчанию выключены — переписка уходит
   на сайт, только если его включили. Вход (DeepSeek) и
   капча — вкладкой сайта на весь экран; «Сбросить» стирает куки и входы
   сайтов (свой профиль WebView, данные приложения не трогает). */

interface Probe {
  busy?: boolean;
  ok?: boolean;
  detail?: string;
}

// Отвечает — зелёный; нужен человек (вход, проверка, лимит) — обычный; прочее — приглушённый
const STATE_BADGE: Record<string, string> = {
  ok: 'badge--success',
  login: '',
  challenge: '',
  limit: '',
};

export default function WebChatsCard() {
  const { t } = useI18n();
  const prefs = useWebchatPrefs();
  const [status, setStatus] = useState<Record<string, WebchatSiteStatus>>({});
  const [profile, setProfile] = useState<'separate' | 'shared' | null>(null);
  const [probe, setProbe] = useState<Record<string, Probe>>({});
  const [resetNote, setResetNote] = useState<string | null>(null);

  const load = () => {
    WebChat.status()
      .then((r) => {
        setStatus(Object.fromEntries(r.sites.map((s) => [s.id, s])));
        setProfile(r.profile);
      })
      .catch(() => {});
  };
  useEffect(() => {
    load();
    return onWebchatStatus(load);
  }, []);

  const sites = orderedSites(prefs);

  const toggle = (id: WebchatId, on: boolean) => setWebchatPrefs({ ...prefs, on: { ...prefs.on, [id]: on } });

  const setProbeOf = (id: WebchatId, p: Probe) => setProbe((cur) => ({ ...cur, [id]: p }));

  // Проверка: сайт без входа — короткий вопрос в свежий чат; со входом —
  // только открыть и посмотреть, пускает ли (чат персоны не тратим)
  const check = (s: WebchatSite) => {
    setProbeOf(s.id, { busy: true });
    const run = s.account
      ? WebChat.check({ site: s.id }).then((r) => {
          if (r.state !== 'ok') throw Object.assign(new Error(r.state), { code: r.state });
          return t('webchat.checkOpened');
        })
      : WebChat.ask({ site: s.id, persona: '', prompt: t('webchat.testPrompt'), fresh: true, timeoutMs: 45000 }).then(
          (r) => `${(r.ms / 1000).toFixed(1)} ${t('webchat.sec')} — ${r.text.slice(0, 60)}`,
        );
    run
      .then((detail) => setProbeOf(s.id, { ok: true, detail }))
      .catch((e) => setProbeOf(s.id, { ok: false, detail: t(`webchat.state.${webchatReason(pluginCode(e) || 'failed')}`) }))
      .finally(load);
  };

  const open = (s: WebchatSite) => {
    setProbeOf(s.id, { busy: true });
    WebChat.open({ site: s.id, title: t(s.account || s.login ? 'webchat.loginTitle' : 'webchat.openTitle', { site: s.name }), done: t('webchat.done') })
      .then((r) => {
        const state = r.state === 'ready' ? 'ok' : r.state;
        setProbeOf(s.id, state === 'ok' || state === 'loading' ? {} : { ok: false, detail: t(`webchat.state.${webchatReason(state)}`) });
      })
      .catch(() => setProbeOf(s.id, {}))
      .finally(load);
  };

  const reset = async () => {
    const ok = await confirmDialog({ message: t('webchat.resetConfirm'), confirmLabel: t('webchat.reset'), danger: true });
    if (!ok) return;
    WebChat.reset()
      .then((r) => setResetNote(t(r.wiped ? 'webchat.resetDone' : 'webchat.resetLater')))
      .catch(() => setResetNote(t('webchat.resetBusy')))
      .finally(() => {
        setProbe({});
        load();
      });
  };

  return (
    <div className="card">
      <h2 className="card-title">{t('webchat.title')}</h2>
      <p className="field-hint" style={{ marginTop: 0 }}>
        {t('webchat.lead')}
      </p>
      <ul className="memory-list">
        {sites.map((s) => {
          const on = prefs.on[s.id] === true;
          const st = status[s.id];
          const state = st?.state ?? '';
          const p = probe[s.id] ?? {};
          return (
            <li key={s.id} className="apikey-row">
              <div className="apikey-main">
                <label className="switch" title={t(on ? 'webchat.turnOff' : 'webchat.turnOn')}>
                  <input type="checkbox" checked={on} onChange={(e) => toggle(s.id, e.target.checked)} />
                  <span className="switch-slider" />
                </label>
                <span className="provider-name">{s.name}</span>
                {state && (
                  <span className={`badge ${STATE_BADGE[state] ?? 'badge--muted'}`}>{t(`webchat.state.${webchatReason(state)}`)}</span>
                )}
                {s.account && <span className="badge badge--muted">{t('webchat.account')}</span>}
              </div>
              <div className="offline-model-row">
                <button type="button" className="btn btn--ghost btn--chip" disabled={p.busy} onClick={() => check(s)}>
                  {p.busy ? t('webchat.checking') : t('webchat.check')}
                </button>
                {(s.account || s.login || state === 'challenge') && (
                  <button type="button" className="btn btn--ghost btn--chip" disabled={p.busy} onClick={() => open(s)}>
                    {t(s.account || s.login ? 'webchat.login' : 'webchat.open')}
                  </button>
                )}
              </div>
              {p.detail && <p className="field-hint">{p.ok ? '✓ ' : t('ui.commentPrefix')}{p.detail}</p>}
              {s.account && (
                <p className="field-hint">
                  {t('webchat.accountHint', { n: st?.newChatsLeft ?? 6 })}
                </p>
              )}
              {s.login && <p className="field-hint">{t('webchat.loginHint')}</p>}
            </li>
          );
        })}
      </ul>
      <div className="offline-model-row">
        <button type="button" className="btn btn--ghost" onClick={() => void reset()}>
          {t('webchat.reset')}
        </button>
      </div>
      {resetNote && <p className="field-hint">{resetNote}</p>}
      <p className="field-hint">{t(profile === 'shared' ? 'webchat.profileShared' : 'webchat.profileSeparate')}</p>
    </div>
  );
}
