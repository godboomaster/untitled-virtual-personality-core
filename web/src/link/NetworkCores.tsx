import { useEffect, useState } from 'react';
import { api, setApiToken, setApiUrl } from '../api';
import { useI18n } from '../i18n';
import { discoverCores } from '../coreFinder';
import type { DiscoveredCore } from '../coreFinder';
import { forgetCoreId, knownCoreId } from '../coreId';
import { confirmDialog } from '../dialogStore';
import { stopBgInbox } from '../bgInbox';
import { forgetWakeServer } from '../wakeWord';
import { getLinkConfig, setLinkConfig } from './client.ts';
import { isStandalone, setStandaloneChosen } from '../standalone';
import { stopPhoneServices } from '../offline/phoneServices';

/* Вкладка «Подключение» (приложение на телефоне): ядра в этой сети — ответы
   на «кто тут ядро?» (coreFinder.ts). Своё ядро отмечено (id из /api/health),
   к другому, открытому по адресу, можно подключиться: прежний токен ему не
   отдаём — экран запуска спросит токен нового ядра. */

const WAIT_MS = 1500;

export default function NetworkCores() {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [cores, setCores] = useState<DiscoveredCore[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const search = async () => {
    setBusy(true);
    setError(null);
    try {
      setCores((await discoverCores(WAIT_MS)).found);
    } catch (e) {
      setCores(null);
      const code = (e as { code?: string }).code;
      setError(code === 'no_wifi' ? t('connection.netNoWifi') : t('connection.netFailed', { err: e instanceof Error ? e.message : String(e) }));
    } finally {
      setBusy(false);
    }
  };

  // Вкладку открыли — сразу спросить сеть (одна секунда, без перебора адресов)
  useEffect(() => {
    void search();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const mine = isStandalone() ? '' : knownCoreId();

  const connect = async (c: DiscoveredCore) => {
    const addr = `${c.host}:${c.api}`;
    if (!(await confirmDialog({ message: t('connection.netConnectConfirm', { name: c.name, addr }), confirmLabel: t('connection.netConnect') }))) return;
    // Прежнее подключение: служба уведомлений и голос — не к нему, канал —
    // отвязать на ноутбуке, без компьютера — сервисы телефона больше не нужны
    stopBgInbox();
    forgetWakeServer();
    const link = getLinkConfig();
    if (link) {
      if (link.id) await api.unpairLink(link.id).catch(() => {});
      setLinkConfig(null);
    }
    if (isStandalone()) await stopPhoneServices().catch(() => {});
    setStandaloneChosen(false);
    setApiToken('');
    forgetCoreId();
    setApiUrl(`http://${addr}`);
    window.location.reload();
  };

  return (
    <div className="card">
      <h2 className="card-title">{t('connection.netCard')}</h2>
      <p className="field-hint">{t('connection.netLead')}</p>
      <div className="field">
        <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => void search()}>
          {busy ? t('boot.finding') : t('connection.netSearch')}
        </button>
      </div>
      {error && <p className="field-hint">{error}</p>}
      {!busy && cores?.length === 0 && <p className="field-hint">{t('connection.netNone')}</p>}
      {cores && cores.length > 0 && (
        <ul className="link-devices">
          {cores.map((c) => {
            const current = Boolean(mine) && c.id === mine;
            return (
              <li key={`${c.host}|${c.api}|${c.link}`} className="link-device">
                <span className={`status-led${current ? '' : ' status-led--off'}`} />
                <span className="net-core-text">
                  <span className="link-device-name">{c.name}</span>
                  <span className="link-device-meta">
                    {c.api > 0 ? `${c.host}:${c.api}` : c.host}
                    {current
                      ? ` · ${t('connection.netCurrent')}`
                      : c.api <= 0
                        ? ` · ${t('connection.netQrOnly')}`
                        : !c.token
                          ? ` · ${t('connection.netNoToken')}`
                          : ''}
                  </span>
                </span>
                {!current && c.api > 0 && (
                  <button type="button" className="btn btn--ghost btn--chip" onClick={() => void connect(c)}>
                    {t('connection.netConnect')}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
