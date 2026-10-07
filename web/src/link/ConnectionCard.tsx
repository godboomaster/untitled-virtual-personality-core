import { useEffect, useReducer, useState } from 'react';
import { api, apiHost, setApiUrl } from '../api';
import { useI18n } from '../i18n';
import { bgInboxStatus, bgNotifyEnabled, setBgNotify, stopBgInbox } from '../bgInbox';
import type { BgNotifyResult } from '../bgInbox';
import { getLinkConfig, linkState, onLinkState, reconnectLink, setLinkConfig } from './client.ts';

/* «Настройки» → «Подключение» (приложение на телефоне): к какому ноутбуку
   подключено и как — по защищённому каналу (Wi-Fi или посредник) или по
   адресу. Отвязка/смена сервера возвращают на экран подключения.
   Здесь же — уведомления, когда приложение закрыто (фоновая служба,
   bgInbox.ts): пока только для подключения по адресу. */

export default function ConnectionCard() {
  const { t } = useI18n();
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => onLinkState(force), []);
  const link = getLinkConfig();

  const unlink = async () => {
    // Сначала просим ноутбук забыть этот телефон (если он на связи), потом —
    // забываем ключ сами: без ключа телефон всё равно не подключится
    if (link?.id) await api.unpairLink(link.id).catch(() => {});
    stopBgInbox();
    setLinkConfig(null);
    window.location.reload();
  };

  const state = linkState();
  return (
    <div className="card">
      <h2 className="card-title">{t('settings.connection')}</h2>
      <div className="field">
        <label className="field-label">{t('settings.connectionServer')}</label>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
          <code style={{ overflowWrap: 'anywhere' }}>{apiHost()}</code>
          {link ? (
            <button type="button" className="btn btn--ghost" onClick={() => void unlink()}>
              {t('link.unpairThis')}
            </button>
          ) : (
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => {
                stopBgInbox();
                setApiUrl('');
                window.location.reload();
              }}
            >
              {t('boot.serverChange')}
            </button>
          )}
        </div>
      </div>
      {link && (
        <div className="field">
          <label className="field-label">{t('link.path')}</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
            <span>{t(`link.state.${state}`)}</span>
            <button type="button" className="btn btn--ghost btn--chip" onClick={() => reconnectLink()}>
              {t('link.reconnect')}
            </button>
          </div>
          <p className="field-hint">{link.u ? t('link.relayHint', { url: link.u }) : t('link.noRelayHint')}</p>
        </div>
      )}
      <BackgroundNotify linkMode={Boolean(link)} />
    </div>
  );
}

// Переключатель «Уведомления, когда приложение закрыто» и состояние службы
function BackgroundNotify({ linkMode }: { linkMode: boolean }) {
  const { t } = useI18n();
  const [on, setOn] = useState(bgNotifyEnabled);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BgNotifyResult | null>(null);
  const [status, setStatus] = useState<{ running: boolean; lastOk: number; lastError: string | null } | null>(null);

  // Состояние службы: на связи / нет связи — раз в несколько секунд, пока открыто
  useEffect(() => {
    if (linkMode || !on) {
      setStatus(null);
      return;
    }
    let alive = true;
    const read = () =>
      bgInboxStatus()
        .then((s) => alive && setStatus(s))
        .catch(() => {});
    // Первое чтение — с задержкой: служба только что запущена и ещё не
    // успела подняться (иначе мелькнёт «служба остановлена»)
    const first = window.setTimeout(read, 1500);
    const timer = window.setInterval(read, 5000);
    return () => {
      alive = false;
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [linkMode, on, busy]);

  if (linkMode) {
    return (
      <div className="field">
        <label className="field-label">{t('settings.bgNotify')}</label>
        <p className="field-hint">{t('settings.bgNotifyLinkOnly')}</p>
      </div>
    );
  }

  const toggle = async (next: boolean) => {
    setBusy(true);
    setResult(null);
    const r = await setBgNotify(next);
    setBusy(false);
    setResult(r === 'ok' ? null : r);
    setOn(r === 'ok' ? next : bgNotifyEnabled());
  };

  let statusText: string | null = null;
  if (on && status) {
    if (!status.running) statusText = t('settings.bgNotifyStopped');
    else if (status.lastError) statusText = t('settings.bgNotifyOffline', { err: status.lastError });
    else if (status.lastOk) statusText = t('settings.bgNotifyOnline');
  }

  return (
    <div className="field">
      <label className="field-label">{t('settings.bgNotify')}</label>
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
        <label className="switch" title={t('settings.bgNotify')}>
          <input type="checkbox" checked={on} disabled={busy} onChange={(e) => void toggle(e.target.checked)} />
          <span className="switch-slider" />
        </label>
        {statusText && <span>{statusText}</span>}
      </div>
      {result === 'denied' && <p className="field-hint">{t('settings.bgNotifyDenied')}</p>}
      {result === 'error' && <p className="field-hint">{t('settings.bgNotifyFailed')}</p>}
      <p className="field-hint">{t('settings.bgNotifyHint')}</p>
    </div>
  );
}
