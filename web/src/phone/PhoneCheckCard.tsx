import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { useApiPersonas } from '../apiData';
import { useInbox } from '../inboxStore';
import {
  clearPhoneLog,
  explainPhrase,
  onPhoneLogChange,
  phoneActionsAvailable,
  phoneLog,
  phoneStatus,
  runPhoneCommand,
} from './actions';
import type { PhoneHandler } from './actions';
import type { PhoneCommand } from './parse';

/* «Настройки» → «Команды телефону» (приложение на телефоне): проверка — что
   умеет этот телефон (кто примет будильник и таймер, фонарик, видно ли
   приложения), где команды не выполняются (режим управления); разбор и
   пробное выполнение любой фразы; журнал последних команд из чата — с тем,
   чем кончилась каждая или почему не выполнялась. */

type StepState = 'ok' | 'fail' | 'warn';

interface Step {
  key: string;
  state: StepState;
  text: string;
  vars?: Record<string, string | number>;
  detail?: string;
}

// Persona id проверки: переспросы («на какое время?») — свои, не чатов
const CHECK_PERSONA = '__phone_check__';

const pad = (n: number) => String(n).padStart(2, '0');

export default function PhoneCheckCard() {
  const { t, lang } = useI18n();
  const personas = useApiPersonas();
  const { controlMode } = useInbox();
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [checking, setChecking] = useState(false);
  const [phrase, setPhrase] = useState('');
  const [explained, setExplained] = useState<string | null>(null);
  const [ran, setRan] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState(phoneLog);

  useEffect(() => onPhoneLogChange(() => setLog(phoneLog())), []);

  const loc = lang === 'ru' ? 'ru-RU' : 'en-US';
  const dayNames = (days: number[]) =>
    [...days]
      .sort((a, b) => ((a + 5) % 7) - ((b + 5) % 7))
      .map((d) => new Date(2026, 9, 3 + d).toLocaleDateString(loc, { weekday: 'short' })) // 04.10.2026 — воскресенье
      .join(', ');

  const resultText = (result: string) => t(`phone.result.${result}`);

  const describe = (cmd: PhoneCommand | null): string => {
    if (!cmd) return t('phone.cmd.none');
    switch (cmd.kind) {
      case 'alarm': {
        let when = t('phone.cmd.noTime');
        if (cmd.relative) when = t('phone.cmd.in', { min: Math.round(cmd.relative / 60) });
        else if (cmd.time) when = `${pad(cmd.time.hour)}:${pad(cmd.time.minute)}`;
        if (cmd.days.length) when += ` · ${dayNames(cmd.days)}`;
        if (cmd.label) when += ` · «${cmd.label}»`;
        return t('phone.cmd.alarm', { when });
      }
      case 'timer':
        return t('phone.cmd.timer', {
          dur: cmd.seconds ? t('phone.cmd.minutes', { n: Math.round((cmd.seconds / 60) * 10) / 10 }) : t('phone.cmd.noDuration'),
        });
      case 'torch':
        return t('phone.cmd.torch', { state: t(cmd.on ? 'phone.cmd.on' : 'phone.cmd.off') });
      case 'open_app':
        return t('phone.cmd.open_app', { name: cmd.name });
      default:
        return t(`phone.cmd.${cmd.kind}`);
    }
  };

  const handlerStep = (key: string, h: PhoneHandler | null, missingState: StepState): Step =>
    h
      ? { key, state: 'ok', text: 'phone.check.handler', vars: { label: h.label }, detail: h.package }
      : { key, state: missingState, text: 'phone.check.noClock' };

  const runCheck = async () => {
    setChecking(true);
    const out: Step[] = [];
    try {
      if (!phoneActionsAvailable()) {
        out.push({ key: 'plugin', state: 'fail', text: 'phone.check.pluginMissing' });
        return;
      }
      out.push({ key: 'plugin', state: 'ok', text: 'phone.check.pluginOk' });
      try {
        const st = await phoneStatus();
        out.push({ key: 'device', state: 'ok', text: 'phone.check.device', vars: { device: st.device, sdk: st.sdk } });
        out.push(handlerStep('alarm', st.alarm, 'fail'));
        out.push(handlerStep('timer', st.timer, 'warn'));
        out.push(handlerStep('alarms', st.alarms, 'warn'));
        out.push({ key: 'torch', state: st.torch ? 'ok' : 'warn', text: st.torch ? 'phone.check.torchOk' : 'phone.check.torchNo' });
        out.push(
          st.apps > 0
            ? { key: 'apps', state: 'ok', text: 'phone.check.apps', vars: { n: st.apps } }
            : { key: 'apps', state: 'fail', text: 'phone.check.appsNone' },
        );
      } catch (e) {
        out.push({ key: 'device', state: 'fail', text: 'phone.check.failed', detail: e instanceof Error ? e.message : String(e) });
      }
      const on = Object.entries(controlMode).filter(([, v]) => v).map(([id]) => personas?.find((p) => p.id === id)?.name ?? id);
      out.push(
        on.length
          ? { key: 'control', state: 'warn', text: 'phone.check.controlOn', vars: { names: on.join(', ') } }
          : { key: 'control', state: 'ok', text: 'phone.check.controlOff' },
      );
    } finally {
      setSteps(out);
      setChecking(false);
    }
  };

  const explain = async () => {
    const text = phrase.trim();
    if (!text) return;
    setBusy(true);
    setRan(null);
    try {
      const r = await explainPhrase(text);
      let line = describe(r.cmd);
      if (r.app) line += ` — ${t('phone.cmd.appFound', { app: r.app })}`;
      else if (r.candidates) line += ` — ${t('phone.cmd.appMany', { list: r.candidates.join(', ') })}`;
      else if (r.cmd?.kind === 'open_app') line += ` — ${r.appsError ? r.appsError : t('phone.cmd.appNone')}`;
      setExplained(line);
    } finally {
      setBusy(false);
    }
  };

  const run = async () => {
    const text = phrase.trim();
    if (!text) return;
    setBusy(true);
    try {
      const r = await runPhoneCommand(CHECK_PERSONA, text);
      setRan(t('phone.try.ran', { result: r ? resultText(r.status) : resultText('not_command') }));
    } finally {
      setBusy(false);
    }
  };

  const time = (ts: number) => new Date(ts).toLocaleTimeString(loc, { hour: '2-digit', minute: '2-digit' });

  return (
    <div className="card">
      <h2 className="card-title">{t('phone.card.title')}</h2>
      <p className="field-hint">{t('phone.card.lead')}</p>

      <div className="field">
        <div className="offline-model-row">
          <button type="button" className="btn btn--ghost" disabled={checking} onClick={() => void runCheck()}>
            {checking ? t('phone.check.running') : t('phone.check.button')}
          </button>
        </div>
        {steps && steps.length > 0 && (
          <ul className="offline-check">
            {steps.map((s) => (
              <li key={s.key} className={`offline-check-item offline-check-item--${s.state}`}>
                <span className="offline-check-mark" aria-hidden="true">
                  {s.state === 'ok' ? '✓' : s.state === 'fail' ? '✗' : '!'}
                </span>
                <span>
                  <b>{t(`phone.check.step.${s.key}`)}:</b> {t(s.text, s.vars)}
                  {s.detail && <span className="offline-check-detail">{s.detail}</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="field">
        <label className="field-label" htmlFor="phone-try">{t('phone.try.label')}</label>
        <input
          id="phone-try"
          className="input"
          type="text"
          value={phrase}
          placeholder={t('phone.try.ph')}
          onChange={(e) => {
            setPhrase(e.target.value);
            setExplained(null);
            setRan(null);
          }}
          onKeyDown={(e) => e.key === 'Enter' && void explain()}
        />
        <div className="offline-model-row">
          <button type="button" className="btn btn--ghost btn--chip" disabled={busy || !phrase.trim()} onClick={() => void explain()}>
            {t('phone.try.explain')}
          </button>
          <button type="button" className="btn btn--ghost btn--chip" disabled={busy || !phrase.trim() || !phoneActionsAvailable()} onClick={() => void run()}>
            {t('phone.try.run')}
          </button>
        </div>
        {explained && <p className="field-hint">{explained}</p>}
        {ran && <p className="field-hint">{ran}</p>}
      </div>

      <div className="field">
        <label className="field-label">{t('phone.log.title')}</label>
        {log.length ? (
          <ul className="offline-check">
            {log.map((e) => {
              const good = e.result === 'ok' || e.result === 'ask' || e.result === 'ambiguous';
              return (
                <li key={`${e.ts}-${e.text}`} className={`offline-check-item offline-check-item--${good ? 'ok' : 'warn'}`}>
                  <span className="offline-check-mark" aria-hidden="true">{good ? '✓' : '!'}</span>
                  <span>
                    <b>{time(e.ts)}</b> «{e.text}» — {e.kind ? `${t(`phone.kind.${e.kind}`)}: ` : ''}
                    {resultText(e.result)}
                    {e.detail && <span className="offline-check-detail">{e.detail}</span>}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="field-hint">{t('phone.log.empty')}</p>
        )}
        {log.length > 0 && (
          <div className="offline-model-row">
            <button type="button" className="btn btn--ghost btn--chip" onClick={clearPhoneLog}>
              {t('phone.log.clear')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
