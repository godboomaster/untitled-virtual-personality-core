import { useEffect, useState } from 'react';
import { api, ApiError } from '../api';
import type { LlmEconomy } from '../api';
import { useApiOnline } from '../apiData';
import { useI18n } from '../i18n';

/* «Экономия нейронки» (Настройки): где вместо вызова модели работает код —
   глобальная настройка ядра (app/core/llm_economy), общая для всех персон.
   Выключено — код (по умолчанию), включено — модель, как раньше. Карточка
   только при живом ядре: настройка хранится в нём. */

export default function LlmEconomyCard() {
  const { t } = useI18n();
  const apiOnline = useApiOnline();
  const [data, setData] = useState<LlmEconomy | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!apiOnline) return;
    api.getLlmEconomy().then(setData).catch(() => {});
  }, [apiOnline]);

  if (!apiOnline || !data) return null;

  const toggle = (key: string, on: boolean) => {
    setBusy(key);
    setError(null);
    // Новое положение — сразу; ответ ядра (всё состояние) — источник правды
    setData((d) => (d ? { ...d, items: { ...d.items, [key]: on } } : d));
    api
      .setLlmEconomy({ [key]: on })
      .then(setData)
      .catch((e) => {
        setError(e instanceof ApiError ? e.message : String(e));
        api.getLlmEconomy().then(setData).catch(() => {});
      })
      .finally(() => setBusy(null));
  };

  return (
    <div className="card">
      <h2 className="card-title">{t('llmEconomy.title')}</h2>
      <p className="field-hint" style={{ marginTop: 0 }}>{t('llmEconomy.subtitle')}</p>
      {Object.entries(data.groups).map(([group, keys]) => (
        <div key={group} className="features-group">
          <div className="features-group-title">{t(`llmEconomy.group.${group}`)}</div>
          {keys.map((key) => (
            <div key={key} className="svc-toggle">
              <label className="switch" title={t(`llmEconomy.${key}`)}>
                <input
                  type="checkbox"
                  checked={Boolean(data.items[key])}
                  disabled={busy === key}
                  onChange={(e) => toggle(key, e.target.checked)}
                />
                <span className="switch-slider" />
              </label>
              <div>
                <div className="svc-toggle-label">{t(`llmEconomy.${key}`)}</div>
                <div className="field-hint" style={{ marginTop: 2 }}>{t(`llmEconomy.${key}.hint`)}</div>
              </div>
            </div>
          ))}
        </div>
      ))}
      {error && <div className="field-hint">{t('ui.commentPrefix')}{t('llmEconomy.saveError', { error })}</div>}
    </div>
  );
}
