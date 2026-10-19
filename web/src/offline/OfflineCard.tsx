import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { confirmDialog } from '../dialogStore';
import { OfflineLlm, pluginCode } from './plugin';
import type { EmbedStatus, ModelStatus } from './plugin';
import { onRemindersChange } from './reminders';
import { runOfflineCheck } from './check';
import { releaseEmbedder } from './embed';
import type { CheckStep } from './check';
import { getKit, kitPersonas } from './kit';
import ShortPromptModal from './ShortPromptModal';

/* «Настройки» → «Без ноутбука» (приложение на телефоне): как персона
   отвечает без ноутбука и модель на телефоне — Gemma 4 E2B (≈2,6 ГБ):
   скачать (по умолчанию только по Wi-Fi), докачать, остановить, удалить.
   Так же — модель поиска по смыслу в памяти (≈135 МБ, EmbedField). */

const MODEL_BYTES = 2.6e9;
const EMBED_BYTES = 135e6;

/** Краткие описания персон для Gemma (ShortPromptModal) — посмотреть; список —
 * персоны, чьи данные есть на телефоне. */
function ShortPromptsField() {
  const { t } = useI18n();
  const [open, setOpen] = useState<{ id: string; name: string } | null>(null);
  const list = kitPersonas()
    .map((id) => {
      const kit = getKit(id);
      return { id, name: kit?.name || id, ready: !!kit?.parts.short?.trim() };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
  return (
    <div className="field">
      <label className="field-label">{t('offline.short.label')}</label>
      {list.length ? (
        <div className="offline-model-row">
          {list.map((p) => (
            <button
              key={p.id}
              type="button"
              className="btn btn--ghost btn--chip"
              title={p.ready ? undefined : t('offline.short.notReady')}
              onClick={() => setOpen({ id: p.id, name: p.name })}
            >
              {p.ready ? p.name : `${p.name} · ${t('offline.short.cutTag')}`}
            </button>
          ))}
        </div>
      ) : (
        <p className="field-hint">{t('offline.short.none')}</p>
      )}
      <p className="field-hint">{t('offline.short.hint')}</p>
      {open && <ShortPromptModal persona={open.id} name={open.name} onClose={() => setOpen(null)} />}
    </div>
  );
}

/** Модель поиска по смыслу (embed.ts): скачать, докачать, удалить. */
function EmbedField() {
  const { t, lang } = useI18n();
  const [status, setStatus] = useState<EmbedStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [metered, setMetered] = useState(false);

  const read = () => {
    OfflineLlm.embedStatus()
      .then(setStatus)
      .catch(() => {});
  };

  useEffect(() => {
    read();
    const timer = window.setInterval(read, 3000);
    const handles = [
      OfflineLlm.addListener('embedProgress', (d) =>
        setStatus((prev) => (prev ? { ...prev, downloading: true, bytes: d.bytes, total: d.total } : prev))),
      OfflineLlm.addListener('embedDone', () => read()),
      OfflineLlm.addListener('embedError', (d) => {
        if (d.message !== 'cancelled') setError(d.message);
        read();
      }),
    ];
    return () => {
      window.clearInterval(timer);
      handles.forEach((h) => void h.then((x) => x.remove()));
    };
  }, []);

  const mb = (bytes: number) => String(Math.round(bytes / 1e6));
  const gb = (bytes: number) => {
    const s = (bytes / 1e9).toFixed(1);
    return lang === 'ru' ? s.replace('.', ',') : s;
  };

  const download = (allowMetered = false) => {
    setError(null);
    setMetered(false);
    OfflineLlm.downloadEmbed({ allowMetered })
      .catch((e: unknown) => {
        if (pluginCode(e) === 'metered') setMetered(true);
        else setError(e instanceof Error ? e.message : String(e));
      })
      .finally(read);
  };

  const remove = async () => {
    if (!status) return;
    const ok = await confirmDialog({
      message: t('offline.embed.deleteConfirm', { size: mb(status.size) }),
      confirmLabel: t('offline.model.delete'),
      danger: true,
    });
    if (ok) {
      releaseEmbedder();
      await OfflineLlm.deleteEmbed().catch(() => {});
    }
    read();
  };

  let line = t('offline.embed.none');
  if (status?.downloading) line = t('offline.embed.downloading', { got: mb(status.bytes), total: mb(status.total || EMBED_BYTES) });
  else if (status?.downloaded) line = t('offline.embed.ready', { size: mb(status.size) });
  else if (status && status.partial > 0) line = t('offline.embed.partial', { got: mb(status.partial) });

  const lowSpace = status && !status.downloaded && status.freeBytes > 0 && status.freeBytes < EMBED_BYTES - status.partial + 1e8;

  return (
    <div className="field">
      <label className="field-label">{t('offline.embed.label')}</label>
      <div className="offline-model-row">
        <span>{line}</span>
        {status && !status.downloaded && !status.downloading && (
          <button type="button" className="btn btn--primary btn--chip" onClick={() => download(false)}>
            {status.partial > 0 ? t('offline.model.resume') : t('offline.model.download')}
          </button>
        )}
        {status?.downloading && (
          <button type="button" className="btn btn--ghost btn--chip" onClick={() => void OfflineLlm.cancelEmbed()}>
            {t('offline.model.cancel')}
          </button>
        )}
        {status?.downloaded && (
          <button type="button" className="btn btn--ghost btn--chip" onClick={() => void remove()}>
            {t('offline.model.delete')}
          </button>
        )}
      </div>
      {status?.downloading && status.total > 0 && <progress className="offline-progress" max={status.total} value={status.bytes} />}
      {metered && (
        <div className="offline-model-row">
          <p className="field-hint">{t('offline.embed.metered')}</p>
          <button type="button" className="btn btn--ghost btn--chip" onClick={() => download(true)}>
            {t('offline.model.downloadMetered')}
          </button>
        </div>
      )}
      {lowSpace && <p className="field-hint">{t('offline.embed.space', { free: gb(status.freeBytes) })}</p>}
      {error && <p className="field-hint">{t('offline.model.error', { err: error })}</p>}
      <p className="field-hint">{t('offline.embed.hint')}</p>
    </div>
  );
}

export default function OfflineCard() {
  const { t, lang } = useI18n();
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [metered, setMetered] = useState(false);
  const [alarms, setAlarms] = useState<{ pending: number; exact: boolean } | null>(null);
  const [check, setCheck] = useState<CheckStep[] | null>(null);
  const [checking, setChecking] = useState(false);

  const runCheck = async () => {
    setChecking(true);
    setCheck([]);
    try {
      await runOfflineCheck(setCheck);
    } finally {
      setChecking(false);
      read();
    }
  };

  const read = () => {
    OfflineLlm.modelStatus()
      .then(setStatus)
      .catch(() => {});
    OfflineLlm.reminderStatus()
      .then(setAlarms)
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
    const offReminders = onRemindersChange(read);
    return () => {
      window.clearInterval(timer);
      offReminders();
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
    // Модель одна на телефон: своей нет, а в приложении Арродеса есть — считает оно
    line = status.via === 'peer' ? t('offline.model.peer', { size: gb(status.size) }) : t('offline.model.ready', { size: gb(status.size) });
    if (status.loaded) line += ` · ${t('offline.model.loaded', { backend: (status.backend ?? '').toUpperCase() })}`;
  } else if (status && status.partial > 0) line = t('offline.model.partial', { got: gb(status.partial) });
  // Удалить можно только свою (модель Арродеса — его)
  const own = status ? (status.own ?? status.downloaded) : false;

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
          {own && (
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
      <EmbedField />
      <ShortPromptsField />
      {alarms && (
        <div className="field">
          <label className="field-label">{t('offline.reminders.label')}</label>
          <div className="offline-model-row">
            <span>{alarms.pending ? t('offline.reminders.status', { n: alarms.pending }) : t('offline.reminders.none')}</span>
          </div>
          {!alarms.exact && (
            <div className="offline-model-row">
              <p className="field-hint">{t('offline.reminders.inexact')}</p>
              <button type="button" className="btn btn--ghost btn--chip" onClick={() => void OfflineLlm.openExactAlarmSettings()}>
                {t('offline.reminders.allow')}
              </button>
            </div>
          )}
          <p className="field-hint">{t('offline.reminders.hint')}</p>
        </div>
      )}
      <p className="field-hint">{t('offline.card.keys')}</p>
      <div className="field">
        <div className="offline-model-row">
          <button type="button" className="btn btn--ghost" disabled={checking} onClick={() => void runCheck()}>
            {checking ? t('offline.check.buttonRunning') : t('offline.check.button')}
          </button>
        </div>
        {check && check.length > 0 && (
          <ul className="offline-check">
            {check.map((s) => (
              <li key={s.key} className={`offline-check-item offline-check-item--${s.state}`}>
                <span className="offline-check-mark" aria-hidden="true">
                  {s.state === 'ok' ? '✓' : s.state === 'fail' ? '✗' : s.state === 'warn' ? '!' : '…'}
                </span>
                <span>
                  <b>{t(`offline.check.step.${s.key}`)}:</b> {t(s.text, s.vars)}
                  {s.detail && <span className="offline-check-detail">{s.detail}</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
