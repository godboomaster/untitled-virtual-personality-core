import { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { api, isNativeApp } from '../api';
import type { LoreInfo, LoreSearchResult } from '../api';
import { useApiOnline } from '../apiData';
import { useOfflineMode } from '../offline/state';
import Collapsible from './Collapsible';
import LoreFileModal from './LoreFileModal';
import PhoneLorePanel, { PhonePendingLore } from './PhoneLorePanel';

/* Вкладка «Лор» в досье: история и мир персонажа отдельными файлами. В ответ
   модель получает не весь текст, а куски к реплике (app/core/lore.py). Поле
   «Проверить запрос» показывает ровно то, что найдётся на ходу персоны: без
   него промах поиска не заметить, пока персона не начнёт ошибаться. */

const SKIP_KEYS: Record<string, string> = {
  smalltalk: 'lore.skipSmalltalk',
  off_topic: 'lore.skipOffTopic',
  no_lore: 'lore.skipNoLore',
};

export default function LorePanel({ personaId }: { personaId: string }) {
  const { lang, t } = useI18n();
  // Список персон остаётся загруженным и без связи: на телефоне режим без
  // ядра — отдельный признак
  const offlineMode = useOfflineMode();
  const apiOnline = useApiOnline() && !offlineMode;
  const [info, setInfo] = useState<LoreInfo | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [opened, setOpened] = useState<string | null>(null); // файл в окне просмотра

  const [query, setQuery] = useState('');
  const [previous, setPrevious] = useState('');
  const [searching, setSearching] = useState(false);
  const [result, setResult] = useState<LoreSearchResult | null>(null);

  useEffect(() => {
    setInfo(null);
    setError('');
    setResult(null);
    setQuery('');
    setPrevious('');
    if (!apiOnline) return;
    let stale = false;
    api.getLore(personaId).then((r) => !stale && setInfo(r)).catch(() => {});
    return () => {
      stale = true;
    };
  }, [apiOnline, personaId]);

  // Приложение на телефоне без ядра — свой лор телефона (txt, md, docx)
  if (!apiOnline && isNativeApp()) return <PhoneLorePanel personaId={personaId} />;
  if (!apiOnline) {
    return (
      <div className="card">
        <h3 className="card-title">{t('lore.title')}</h3>
        <div className="files-empty">{t('lore.needCore')}</div>
      </div>
    );
  }

  const fmtChars = (n: number) => (n >= 1000 ? `${Math.round(n / 100) / 10}k` : String(n));
  const fmtDate = (ts: number) =>
    new Date(ts * 1000).toLocaleDateString(lang === 'ru' ? 'ru-RU' : 'en-US', { day: '2-digit', month: '2-digit', year: 'numeric' });

  const onUpload = (f: File) => {
    setUploading(true);
    setError('');
    setResult(null);
    api
      .uploadLore(personaId, f)
      .then(setInfo)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setUploading(false));
  };

  const onDelete = (name: string) => {
    setError('');
    setResult(null);
    api
      .deleteLore(personaId, name)
      .then(setInfo)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  };

  const onSearch = () => {
    const q = query.trim();
    if (!q || searching) return;
    setSearching(true);
    setError('');
    api
      .searchLore(personaId, q, previous.trim())
      .then(setResult)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSearching(false));
  };

  const files = info?.files ?? [];
  return (
    <>
      {opened && <LoreFileModal personaId={personaId} name={opened} onClose={() => setOpened(null)} />}
      {isNativeApp() && <PhonePendingLore personaId={personaId} />}
      <div className="card">
        <div className="card-title-row">
          <h3 className="card-title">{t('lore.title')}</h3>
          {files.length > 0 && (
            <span className="badge">
              {t('lore.badge', { chars: fmtChars(info?.chars ?? 0), chunks: info?.chunks ?? 0 })}
            </span>
          )}
          <button
            type="button"
            className="btn btn--primary"
            disabled={uploading}
            aria-label={t('lore.upload')}
            title={t('lore.upload')}
            onClick={() => fileInputRef.current?.click()}
          >
            +
          </button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          hidden
          accept=".txt,.md,.docx,.doc,.odt,.rtf,.pdf,.html,.epub"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onUpload(f);
            e.target.value = '';
          }}
        />
        <div className="field-hint">{t('lore.hint')}</div>
        {uploading && <div className="field-hint">{t('lore.uploading')}</div>}
        {error && <div className="field-hint">⚠ {error}</div>}

        {files.length === 0 ? (
          <div className="files-empty">{t('lore.empty')}</div>
        ) : (
          <ul className="memory-list">
            {files.map((f) => (
              <li key={f.name} className="lore-file">
                <div className="files-item">
                  <div className="files-main">
                    <div className="files-name">{f.name}</div>
                    <div className="files-meta">
                      <span>{t('lore.fileMeta', { chars: fmtChars(f.chars), chunks: f.chunks, date: fmtDate(f.ts) })}</span>
                    </div>
                  </div>
                  <div className="files-actions">
                    <button type="button" className="btn btn--ghost" onClick={() => setOpened(f.name)}>
                      {t('lore.open')}
                    </button>
                    <button type="button" className="btn btn--danger" onClick={() => onDelete(f.name)}>
                      {t('common.delete')}
                    </button>
                  </div>
                </div>
                {f.sections.some((s) => s.title) && (
                  <Collapsible title={t('lore.sections')} summary={String(f.sections.length)}>
                    <ul className="lore-sections">
                      {f.sections.map((s) => (
                        <li key={s.title}>
                          <span>{s.title || t('lore.noSection')}</span>
                          <span className="lore-count">{s.chunks}</span>
                        </li>
                      ))}
                    </ul>
                  </Collapsible>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {files.length > 0 && (
        <div className="card">
          <h3 className="card-title">{t('lore.checkTitle')}</h3>
          <div className="field-hint">{t('lore.checkHint')}</div>
          <form
            className="lore-check"
            onSubmit={(e) => {
              e.preventDefault();
              onSearch();
            }}
          >
            <input
              className="input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('lore.queryPh')}
              aria-label={t('lore.queryPh')}
            />
            <input
              className="input"
              value={previous}
              onChange={(e) => setPrevious(e.target.value)}
              placeholder={t('lore.previousPh')}
              aria-label={t('lore.previousPh')}
            />
            <button type="submit" className="btn btn--primary" disabled={searching || !query.trim()}>
              {t('lore.check')}
            </button>
          </form>

          {result && (
            <div className="lore-result">
              {result.skipped ? (
                <div className="files-empty">{t(SKIP_KEYS[result.skipped] ?? 'lore.nothing')}</div>
              ) : result.hits.length === 0 ? (
                <div className="files-empty">{t('lore.nothing')}</div>
              ) : (
                <>
                  <div className="field-hint">
                    {result.weak ? t('lore.weak', { n: result.hits.length }) : t('lore.found', { n: result.hits.length })}
                  </div>
                  <ul className="memory-list">
                    {result.hits.map((h, i) => (
                      <li key={i} className="lore-hit">
                        {h.section && <div className="lore-hit-section">{h.section}</div>}
                        <div className="lore-hit-text">{h.text}</div>
                        <div className="files-meta">
                          <span>{h.file}</span>
                          {h.words && <span className="badge">{t('lore.byWord')}</span>}
                        </div>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </>
  );
}
