import { useEffect, useReducer } from 'react';
import { api, apiHost, setApiUrl } from '../api';
import { useI18n } from '../i18n';
import { getLinkConfig, linkState, onLinkState, reconnectLink, setLinkConfig } from './client.ts';

/* «Настройки» → «Подключение» (приложение на телефоне): к какому ноутбуку
   подключено и как — по защищённому каналу (Wi-Fi или посредник) или по
   адресу. Отвязка/смена сервера возвращают на экран подключения. */

export default function ConnectionCard() {
  const { t } = useI18n();
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => onLinkState(force), []);
  const link = getLinkConfig();

  const unlink = async () => {
    // Сначала просим ноутбук забыть этот телефон (если он на связи), потом —
    // забываем ключ сами: без ключа телефон всё равно не подключится
    if (link?.id) await api.unpairLink(link.id).catch(() => {});
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
    </div>
  );
}
