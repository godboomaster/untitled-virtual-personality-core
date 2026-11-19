import { useCallback, useEffect, useState } from 'react';
import { isLinkMode } from '../api';
import { useI18n } from '../i18n';
import {
  openAppInfo,
  openPhoneControlSettings,
  phoneControlStatus,
  phoneScreenSnapshot,
  syncPhoneControl,
} from './control';
import type { PhoneControlStatus, ScreenSnapshot } from './control';

/* «Настройки» → «Управление экраном» (приложение на телефоне): включена ли
   служба спец. возможностей, на связи ли она с ядром, переход в настройки
   Android (и в «О приложении» — там Android 13+ прячет «Разрешить
   ограниченные настройки» для приложений не из магазина) и пробный снимок:
   что ядро увидит на экране. Сами команды — в любом чате персоны. */

type Mark = 'ok' | 'fail' | 'warn';

export default function PhoneControlCard() {
  const { t } = useI18n();
  const [st, setSt] = useState<PhoneControlStatus | null | undefined>(undefined);
  const [snap, setSnap] = useState<ScreenSnapshot | null>(null);
  const [snapErr, setSnapErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    void phoneControlStatus().then(setSt);
  }, []);

  useEffect(() => {
    refresh();
    // Вернулись из настроек Android — служба могла включиться
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        syncPhoneControl();
        refresh();
      }
    };
    document.addEventListener('visibilitychange', onVisible);
    const timer = window.setInterval(refresh, 5000);
    return () => {
      document.removeEventListener('visibilitychange', onVisible);
      window.clearInterval(timer);
    };
  }, [refresh]);

  const trySnapshot = async () => {
    setBusy(true);
    setSnap(null);
    setSnapErr(null);
    try {
      setSnap(await phoneScreenSnapshot());
    } catch (e) {
      setSnapErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const rows: { key: string; mark: Mark; text: string; detail?: string }[] = [];
  if (st === null) {
    rows.push({ key: 'plugin', mark: 'fail', text: t('phoneCtl.pluginMissing') });
  } else if (st) {
    rows.push(
      st.enabled
        ? { key: 'service', mark: st.running ? 'ok' : 'warn', text: t(st.running ? 'phoneCtl.serviceOn' : 'phoneCtl.serviceStarting') }
        : { key: 'service', mark: 'fail', text: t('phoneCtl.serviceOff') },
    );
    if (st.enabled && st.running) {
      const mark: Mark = st.state === 'online' ? 'ok' : st.state === 'connecting' ? 'warn' : 'fail';
      rows.push({
        key: 'core',
        mark,
        text: t(isLinkMode() ? 'phoneCtl.state.link' : `phoneCtl.state.${st.state}`),
        detail: st.state === 'offline' || st.state === 'auth' ? st.error : undefined,
      });
    }
  }

  const shown = (snap?.elements ?? []).filter((e) => (e.t || e.h) && (e.c || e.ed || e.k === 'text')).slice(0, 8);

  return (
    <div className="card">
      <h2 className="card-title">{t('phoneCtl.title')}</h2>
      <p className="field-hint">{t('phoneCtl.lead')}</p>

      {rows.length > 0 && (
        <ul className="offline-check">
          {rows.map((r) => (
            <li key={r.key} className={`offline-check-item offline-check-item--${r.mark}`}>
              <span className="offline-check-mark" aria-hidden="true">
                {r.mark === 'ok' ? '✓' : r.mark === 'fail' ? '✗' : '!'}
              </span>
              <span>
                <b>{t(`phoneCtl.step.${r.key}`)}:</b> {r.text}
                {r.detail && <span className="offline-check-detail">{r.detail}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}

      {st && (
        <div className="field">
          <div className="offline-model-row">
            <button type="button" className="btn btn--ghost" onClick={() => void openPhoneControlSettings().catch(() => {})}>
              {t('phoneCtl.openSettings')}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              disabled={busy || !st.running}
              onClick={() => void trySnapshot()}
            >
              {busy ? t('phoneCtl.snapRunning') : t('phoneCtl.snap')}
            </button>
          </div>
          {!st.enabled && (
            <p className="field-hint">
              {t('phoneCtl.restricted')}{' '}
              <button type="button" className="btn btn--ghost btn--chip" onClick={() => void openAppInfo().catch(() => {})}>
                {t('phoneCtl.appInfo')}
              </button>
            </p>
          )}
          {snapErr && <p className="field-hint">{t('phoneCtl.snapFailed', { err: snapErr })}</p>}
          {snap && st.running && (
            <div className="field-hint">
              {snap.screen_off
                ? t('phoneCtl.snapOff')
                : t('phoneCtl.snapApp', { app: snap.app || snap.pkg || '?', n: snap.elements.length })}
              {shown.length > 0 && (
                <ul className="offline-check">
                  {shown.map((e) => (
                    <li key={e.i} className="offline-check-item offline-check-item--ok">
                      <span className="offline-check-mark" aria-hidden="true">·</span>
                      <span>
                        {t(`phoneCtl.kind.${e.k}`)}: «{e.ed ? e.h || e.t : e.t || e.h}»
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
      <p className="field-hint">{t('phoneCtl.safety')}</p>
    </div>
  );
}
