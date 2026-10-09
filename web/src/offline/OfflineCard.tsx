import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { confirmDialog } from '../dialogStore';
import { OfflineLlm, pluginCode } from './plugin';
import type { ModelStatus } from './plugin';

/* «Настройки» → «Без ноутбука» (приложение на телефоне): как персона
   отвечает без ноутбука и модель на телефоне — Gemma 4 E2B (≈2,6 ГБ):
   скачать (по умолчанию только по Wi-Fi), докачать, остановить, удалить. */

const MODEL_BYTES = 2.6e9;

export default function OfflineCard() {
  const { t, lang } = useI18n();
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [metered, setMetered] = useState(false);

  const read = () => {
    OfflineLlm.modelStatus()
      .then(setStatus)
      .catch(() => {});
  };

  useEffect(() => {
    read();
    const timer = window.setInterval(read, 3000);
    const handles = [
      OfflineLlm.addListener('modelProgress', (d) =>
        setStatus((prev) => (prev ? { ...prev, downloading: true, bytes: d.bytes, total: d.total } : prev))),
      OfflineLlm.addListener('modelDone', () => read()),
      OfflineLlm.addListener('modelError', (d) => {
        if (d.message !== 'cancelled') setError(d.message);
        read();
      }),
    ];
    return () => {
      window.clearInterval(timer);
      handles.forEach((h) => void h.then((x) => x.remove()));
    };
  }, []);

  const gb = (bytes: number) => {
    const s = (bytes / 1e9).toFixed(1);
    return lang === 'ru' ? s.replace('.', ',') : s;
  };

  const download = (allowMetered = false) => {
    setError(null);
    setMetered(false);
    OfflineLlm.downloadModel({ allowMetered })
      .catch((e: unknown) => {
        if (pluginCode(e) === 'metered') setMetered(true);
        else setError(e instanceof Error ? e.message : String(e));
      })
      .finally(read);
  };

  const remove = async () => {
    if (!status) return;
    const ok = await confirmDialog({
      message: t('offline.model.deleteConfirm', { size: gb(status.size) }),
      confirmLabel: t('offline.model.delete'),
      danger: true,
    });
    if (ok) await OfflineLlm.deleteModel().catch(() => {});
    read();
  };

  let line = t('offline.model.none');
  if (status?.downloading) line = t('offline.model.downloading', { got: gb(status.bytes), total: gb(status.total || MODEL_BYTES) });
  else if (status?.downloaded) {
    line = t('offline.model.ready', { size: gb(status.size) });
    if (status.loaded) line += ` · ${t('offline.model.loaded', { backend: (status.backend ?? '').toUpperCase() })}`;
  } else if (status && status.partial > 0) line = t('offline.model.partial', { got: gb(status.partial) });

  const lowSpace = status && !status.downloaded && status.freeBytes > 0 && status.freeBytes < MODEL_BYTES - status.partial + 3e8;

  return (
    <div className="card">
      <h2 className="card-title">{t('offline.card.title')}</h2>
      <p className="field-hint">{t('offline.card.lead')}</p>
      <div className="field">
        <label className="field-label">{t('offline.model.label')}</label>
        <div className="offline-model-row">
          <span>{line}</span>
          {status && !status.downloaded && !status.downloading && (
            <button type="button" className="btn btn--primary btn--chip" onClick={() => download(false)}>
              {status.partial > 0 ? t('offline.model.resume') : t('offline.model.download')}
            </button>
          )}
          {status?.downloading && (
            <button type="button" className="btn btn--ghost btn--chip" onClick={() => void OfflineLlm.cancelDownload()}>
              {t('offline.model.cancel')}
            </button>
          )}
          {status?.downloaded && (
            <button type="button" className="btn btn--ghost btn--chip" onClick={() => void remove()}>
              {t('offline.model.delete')}
            </button>
          )}
        </div>
        {status?.downloading && status.total > 0 && (
          <progress className="offline-progress" max={status.total} value={status.bytes} />
        )}
        {metered && (
          <div className="offline-model-row">
            <p className="field-hint">{t('offline.model.metered')}</p>
            <button type="button" className="btn btn--ghost btn--chip" onClick={() => download(true)}>
              {t('offline.model.downloadMetered')}
            </button>
          </div>
        )}
        {lowSpace && <p className="field-hint">{t('offline.model.space', { free: gb(status.freeBytes) })}</p>}
        {error && <p className="field-hint">{t('offline.model.error', { err: error })}</p>}
      </div>
      <p className="field-hint">{t('offline.card.keys')}</p>
    </div>
  );
}
