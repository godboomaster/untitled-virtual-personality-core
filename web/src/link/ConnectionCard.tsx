import { useEffect, useReducer, useState } from 'react';
import { api, apiHost, setApiUrl } from '../api';
import { useI18n } from '../i18n';
import { bgInboxStatus, bgNotifyEnabled, retryBgNotifyPermission, setBgNotify, stopBgInbox } from '../bgInbox';
import type { BgInboxStatus, BgNotifyResult } from '../bgInbox';
import { forgetWakeServer, openOverlaySettings, setWakeWord, wakeStatus } from '../wakeWord';
import type { WakeResult, WakeStatus } from '../wakeWord';
import { getLinkConfig, linkState, onLinkState, reconnectLink, setLinkConfig } from './client.ts';

/* «Настройки» → «Подключение» (приложение на телефоне): к какому ноутбуку
   подключено и как — по защищённому каналу (Wi-Fi или посредник) или по
   адресу. Отвязка/смена сервера возвращают на экран подключения.
   Здесь же — уведомления, когда приложение закрыто (фоновая служба,
   bgInbox.ts), и «Слушать по имени» (wakeWord.ts): обе пока только для
   подключения по адресу. */

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
    forgetWakeServer();
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
                forgetWakeServer();
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
      <WakeWordToggle linkMode={Boolean(link)} />
    </div>
  );
}

// Переключатель «Уведомления, когда приложение закрыто» и состояние службы
function BackgroundNotify({ linkMode }: { linkMode: boolean }) {
  const { t, lang } = useI18n();
  const [on, setOn] = useState(bgNotifyEnabled);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BgNotifyResult | null>(null);
  const [status, setStatus] = useState<BgInboxStatus | null>(null);

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

  // Уведомления запрещены в настройках Android: служба не запущена (или
  // ничего не покажет) — вместо «на связи» подсказка и кнопка «Разрешить»
  const blocked = on && status?.notificationsEnabled === false;
  const allow = async () => {
    setBusy(true);
    setResult(null);
    const r = await retryBgNotifyPermission();
    setBusy(false);
    setResult(r === 'ok' ? null : r);
  };

  let statusText: string | null = null;
  if (on && status && !blocked) {
    if (!status.running) statusText = t('settings.bgNotifyStopped');
    else if (status.lastError) statusText = t('settings.bgNotifyOffline', { err: status.lastError });
    else if (status.lastOk) {
      // Время последнего ответа ядра: оно отвечает не реже раза в 5 минут,
      // время старше — связь застряла
      const time = new Date(status.lastOk).toLocaleTimeString(lang === 'ru' ? 'ru-RU' : 'en-US', {
        hour: '2-digit',
        minute: '2-digit',
      });
      statusText = t('settings.bgNotifyOnline', { time });
    }
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
        {blocked && (
          <button type="button" className="btn btn--ghost btn--chip" disabled={busy} onClick={() => void allow()}>
            {t('settings.bgNotifyAllow')}
          </button>
        )}
      </div>
      {(result === 'denied' || (blocked && result !== 'error')) && (
        <p className="field-hint">{t('settings.bgNotifyDenied')}</p>
      )}
      {result === 'error' && <p className="field-hint">{t('settings.bgNotifyFailed')}</p>}
      <p className="field-hint">{t('settings.bgNotifyHint')}</p>
    </div>
  );
}

// Переключатель «Слушать по имени» (голосовая активация) и что служба делает
function WakeWordToggle({ linkMode }: { linkMode: boolean }) {
  const { t } = useI18n();
  const [status, setStatus] = useState<WakeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<WakeResult | null>(null);

  // Состояние службы: включена ли (её можно выключить и из шторки), что
  // слышит и какие имена знает — раз в пару секунд, пока открыто
  useEffect(() => {
    if (linkMode) return;
    let alive = true;
    const read = () =>
      wakeStatus()
        .then((s) => alive && setStatus(s))
        .catch(() => {});
    read();
    const timer = window.setInterval(read, 2000);
    // Вернулись из настроек Android (право «Поверх других») — сразу заново
    const onVisible = () => {
      if (document.visibilityState === 'visible') read();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      alive = false;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [linkMode, busy]);

  if (linkMode) {
    return (
      <div className="field">
        <label className="field-label">{t('settings.wake')}</label>
        <p className="field-hint">{t('settings.wakeLinkOnly')}</p>
      </div>
    );
  }

  const on = Boolean(status?.enabled);
  const toggle = async (next: boolean) => {
    setBusy(true);
    setResult(null);
    const r = await setWakeWord(next);
    setResult(r === 'ok' ? null : r);
    setBusy(false);
  };

  let stateText: string | null = null;
  if (on && status) {
    if (!status.running) stateText = t('settings.wakeState.off');
    else stateText = t(`settings.wakeState.${status.state}`);
  }

  return (
    <div className="field">
      <label className="field-label">{t('settings.wake')}</label>
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
        <label className="switch" title={t('settings.wake')}>
          <input type="checkbox" checked={on} disabled={busy || !status} onChange={(e) => void toggle(e.target.checked)} />
          <span className="switch-slider" />
        </label>
        {stateText && <span>{stateText}</span>}
      </div>
      {on && status?.running && status.names.length > 0 && (
        <p className="field-hint">{t('settings.wakeNames', { names: status.names.join(', ') })}</p>
      )}
      {result === 'mic-denied' && <p className="field-hint">{t('settings.wakeMicDenied')}</p>}
      {result === 'error' && <p className="field-hint">{t('settings.wakeFailed')}</p>}
      {on && status?.overlayAllowed === false && (
        <div style={{ margin: '8px 0' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
            <span>{t('settings.wakeOverlay')}</span>
            <button type="button" className="btn btn--ghost btn--chip" onClick={() => void openOverlaySettings().catch(() => {})}>
              {t('settings.wakeOverlayAllow')}
            </button>
          </div>
          <p className="field-hint">{t('settings.wakeOverlayHint')}</p>
        </div>
      )}
      <p className="field-hint">{t('settings.wakeHint')}</p>
    </div>
  );
}
