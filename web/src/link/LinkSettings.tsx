import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../api';
import type { LinkStatus } from '../api';
import { useI18n } from '../i18n';
import { agoLabel } from '../timeAgo';
import { confirmDialog } from '../dialogStore';
import QrCode from './QrCode';

/* «Настройки» → «Телефон» (на компьютере): подключить телефон по QR-коду,
   список сопряжённых телефонов, отвязка. Канал — app/link (VPC Link).
   Внизу — токен API для запасного входа по адресу и токену. */

const POLL_MS = 3000; // пока карточка открыта — видно, кто в сети
const POLL_PAIRING_MS = 1500; // пока показан QR — быстрее, чтобы сразу увидеть телефон

export default function LinkSettings() {
  const { t, lang } = useI18n();
  const locale = lang === 'ru' ? 'ru-RU' : 'en-US';
  const [status, setStatus] = useState<LinkStatus | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [offer, setOffer] = useState<{ uri: string; expires: number; known: string[] } | null>(null);
  const [paired, setPaired] = useState<string | null>(null); // имя только что сопряжённого
  const [now, setNow] = useState(() => Date.now());
  const [copied, setCopied] = useState(false);
  // undefined — ещё не загружен (или старое ядро без /api/token), null — не задан
  const [token, setToken] = useState<string | null | undefined>(undefined);
  const [tokenShown, setTokenShown] = useState(false);
  const [tokenCopied, setTokenCopied] = useState(false);

  const load = useCallback(() => {
    api
      .getLink()
      .then((s) => {
        setStatus(s);
        setFailed(null);
      })
      .catch((e: unknown) => setFailed(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(() => {
      if (!document.hidden) load();
      setNow(Date.now());
    }, offer ? POLL_PAIRING_MS : POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, offer]);

  // Токен — один раз при открытии, не в опросе: к нажатию «Скопировать» он
  // уже здесь, и буфер обмена получает его прямо в обработчике клика
  useEffect(() => {
    api
      .getServerToken()
      .then((r) => setToken(r.token))
      .catch(() => setToken(undefined));
  }, []);

  // Новый телефон в списке — QR-код больше не нужен
  useEffect(() => {
    if (!offer || !status) return;
    const fresh = status.devices.find((d) => !offer.known.includes(d.id));
    if (fresh) {
      setOffer(null);
      setPaired(fresh.name);
    } else if (offer.expires * 1000 < Date.now()) {
      setOffer(null);
    }
  }, [status, offer]);

  const startPairing = async () => {
    setPaired(null);
    try {
      const r = await api.pairLink();
      setOffer({ uri: r.uri, expires: r.expires, known: status?.devices.map((d) => d.id) ?? [] });
      load();
    } catch (e) {
      setFailed(e instanceof ApiError ? e.message : String(e));
    }
  };

  const cancelPairing = () => {
    setOffer(null);
    void api.cancelPairLink().catch(() => {});
  };

  const unpair = async (id: string, name: string) => {
    if (!(await confirmDialog({ message: t('link.unpairConfirm', { name }), confirmLabel: t('link.unpair'), danger: true }))) return;
    try {
      await api.unpairLink(id);
    } catch (e) {
      setFailed(e instanceof ApiError ? e.message : String(e));
    }
    load();
  };

  const copyCode = () => {
    if (!offer) return;
    navigator.clipboard
      ?.writeText(offer.uri)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {});
  };

  const copyToken = () => {
    if (!token) return;
    // Буфер недоступен (страница не по localhost/https) — показываем токен,
    // чтобы его можно было выделить вручную
    if (!navigator.clipboard) {
      setTokenShown(true);
      return;
    }
    navigator.clipboard
      .writeText(token)
      .then(() => {
        setTokenCopied(true);
        window.setTimeout(() => setTokenCopied(false), 1500);
      })
      .catch(() => setTokenShown(true));
  };

  const left = offer ? Math.max(0, Math.round(offer.expires - now / 1000)) : 0;
  const relayLine = !status?.relay
    ? t('link.relayNone')
    : status.relay_connected
      ? t('link.relayOk', { url: status.relay })
      : t('link.relayDown', { url: status.relay, err: status.relay_error ?? '…' });

  return (
    <div className="card link-card">
      <h2 className="card-title">{t('link.title')}</h2>
      <p className="field-hint">{t('link.lead')}</p>

      {status && !status.enabled && <p className="field-hint">{t('link.disabled')}</p>}

      {offer ? (
        <div className="link-pairing">
          <QrCode text={offer.uri} />
          <div className="link-pairing-text">
            <p>{t('link.pairSteps')}</p>
            <p className="field-hint">{t('link.pairExpires', { m: Math.floor(left / 60), s: String(left % 60).padStart(2, '0') })}</p>
            <div className="link-pairing-actions">
              <button type="button" className="btn btn--ghost" onClick={copyCode}>
                {copied ? t('common.copied') : t('link.copyCode')}
              </button>
              <button type="button" className="btn btn--ghost" onClick={cancelPairing}>
                {t('common.cancel')}
              </button>
            </div>
          </div>
        </div>
      ) : (
        status?.enabled !== false && (
          <div className="field">
            <button type="button" className="btn btn--primary" onClick={() => void startPairing()}>
              {t('link.pair')}
            </button>
            {paired && <p className="field-hint">{t('link.paired', { name: paired })}</p>}
          </div>
        )
      )}

      {status && status.devices.length > 0 && (
        <div className="field">
          <label className="field-label">{t('link.devices')}</label>
          <ul className="link-devices">
            {status.devices.map((d) => (
              <li key={d.id} className="link-device">
                <span className={`status-led${d.online ? '' : ' status-led--off'}`} />
                <span className="link-device-name">{d.name}</span>
                <span className="link-device-meta">
                  {d.online === 'lan'
                    ? t('link.onlineLan')
                    : d.online === 'relay'
                      ? t('link.onlineRelay')
                      : t('link.lastSeen', { ago: agoLabel(d.last_seen, new Date(now), t, locale) })}
                </span>
                <button type="button" className="btn btn--ghost btn--chip" onClick={() => void unpair(d.id, d.name)}>
                  {t('link.unpair')}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {status && (
        <div className="field">
          <label className="field-label">{t('link.paths')}</label>
          <p className="field-hint">
            {status.lan_error
              ? t('link.lanError', { port: status.port, err: status.lan_error })
              : status.running
                ? t('link.lanOk', { addr: status.lan.map((ip) => `${ip}:${status.port}`).join(', ') || '—' })
                : t('link.lanIdle')}
          </p>
          <p className="field-hint">{relayLine}</p>
        </div>
      )}

      {token !== undefined && (
        <div className="field">
          <label className="field-label">{t('link.token')}</label>
          <p className="field-hint">{token ? t('link.tokenHint') : t('link.tokenNone')}</p>
          {token && (
            <div className="link-token">
              <code className="link-token-value">{tokenShown ? token : '•'.repeat(16)}</code>
              <button type="button" className="btn btn--ghost btn--chip" onClick={() => setTokenShown((v) => !v)}>
                {tokenShown ? t('link.tokenHide') : t('link.tokenShow')}
              </button>
              <button type="button" className="btn btn--ghost btn--chip" onClick={copyToken}>
                {tokenCopied ? t('common.copied') : t('common.copy')}
              </button>
            </div>
          )}
        </div>
      )}
      {failed && <p className="field-hint">{failed}</p>}
    </div>
  );
}
