import { useEffect, useRef, useState } from 'react';
import type { PersonaFile } from '../mockData';
import { useI18n, useMockData } from '../i18n';
import { api } from '../api';
import type { FileEntry } from '../api';
import { useApiOnline } from '../apiData';

/* Вкладка «Файлы» в досье персоны. При доступном бэкенде — реальные
   загруженные документы (база для поиска): список с размером и датой,
   загрузка, скачивание текста, удаление. Без бэкенда — моковый прототип
   (файлы, «отправленные персоной»: уроки, дампы, экспорты). */

// Глиф-иконка по типу файла (подписи фильтров — из словаря i18n)
const kindIcons: Record<PersonaFile['kind'], string> = {
  lesson: '▤',
  dump: '◫',
  export: '⇩',
  image: '▨',
  document: '▪',
};

// Настоящее скачивание файла через Blob
function downloadBlob(name: string, content: string) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  // Освобождаем URL после того, как браузер забрал ссылку
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export default function FilesPanel({ personaId }: { personaId: string }) {
  const { lang, t } = useI18n();
  const { filesByPersona } = useMockData();
  const apiOnline = useApiOnline();
  const [deleted, setDeleted] = useState<number[]>([]);
  const [filter, setFilter] = useState<'all' | PersonaFile['kind']>('all');

  // Файлы с бэкенда
  const [apiFiles, setApiFiles] = useState<FileEntry[] | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  // При смене персоны сбрасываем фильтр и локальные удаления
  useEffect(() => {
    setDeleted([]);
    setFilter('all');
  }, [personaId]);

  useEffect(() => {
    setApiFiles(null);
    setUploadError('');
    if (!apiOnline) return;
    let stale = false;
    api.getFiles(personaId).then((r) => !stale && setApiFiles(r.files)).catch(() => {});
    return () => {
      stale = true;
    };
  }, [apiOnline, personaId]);

  const fmtSize = (chars: number) => (chars >= 1000 ? `${Math.round(chars / 100) / 10}k` : String(chars));
  const fmtDate = (ts: number) =>
    ts
      ? new Date(ts).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US', {
          day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
        })
      : '—';

  const onUpload = (f: File) => {
    setUploading(true);
    setUploadError('');
    api
      .uploadFile(personaId, f)
      .then((r) => setApiFiles(r.files))
      .catch((e) => setUploadError(e instanceof Error ? e.message : String(e)))
      .finally(() => setUploading(false));
  };

  const onDelete = (filename: string) => {
    api.deleteFile(personaId, filename).then((r) => setApiFiles(r.files)).catch(() => {});
  };

  const onDownload = (filename: string) => {
    api
      .getFileContent(personaId, filename)
      .then((r) => downloadBlob(filename, r.content))
      .catch(() => {});
  };

  // ── Режим бэкенда: реальные документы ──
  if (apiOnline) {
    const files = apiFiles ?? [];
    return (
      <div className="card">
        <div className="card-title-row">
          <h3 className="card-title">{t('files.title')}</h3>
          <span className="badge">{t('files.badge', { n: files.length, kb: files.reduce((n, f) => n + Math.round(f.size / 1000), 0) })}</span>
          <button
            type="button"
            className="btn btn--primary"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            +
          </button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onUpload(f);
            e.target.value = '';
          }}
        />
        {uploadError && <div className="field-hint">⚠ {uploadError}</div>}

        {files.length === 0 ? (
          <div className="files-empty">{t('files.empty')}</div>
        ) : (
          <ul className="memory-list">
            {files.map((f) => (
              <li key={f.filename} className="files-item">
                <div className="files-icon" title={t('files.kind.document')}>{kindIcons.document}</div>
                <div className="files-main">
                  <div className="files-name">{f.filename}</div>
                  <div className="files-meta">
                    <span className="badge">{t('files.kind.document')}</span>
                    <span>{fmtSize(f.size)} · {fmtDate(f.timestamp)}</span>
                  </div>
                </div>
                <div className="files-actions">
                  <button type="button" className="btn btn--ghost" onClick={() => onDownload(f.filename)}>
                    {t('common.download')}
                  </button>
                  <button type="button" className="btn btn--danger" onClick={() => onDelete(f.filename)}>
                    {t('common.delete')}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  // ── Моковый режим (без бэкенда) ──
  const files = (filesByPersona[personaId] ?? []).filter((f) => !deleted.includes(f.id));
  // Показываем фильтры только по типам, реально существующим у персоны
  const kinds = [...new Set(files.map((f) => f.kind))];
  const effectiveFilter = filter !== 'all' && !kinds.includes(filter) ? 'all' : filter;
  const visible = effectiveFilter === 'all' ? files : files.filter((f) => f.kind === effectiveFilter);
  // Суммарный размер — приблизительно, по числам из человекочитаемых строк
  const totalKb = files.reduce((n, f) => n + (parseInt(f.size, 10) || 0), 0);

  return (
    <div className="card">
      <div className="card-title-row">
        <h3 className="card-title">{t('files.title')}</h3>
        <span className="badge">{t('files.badge', { n: files.length, kb: totalKb })}</span>
      </div>

      {files.length === 0 ? (
        <div className="files-empty">{t('files.empty')}</div>
      ) : (
        <>
          {/* Фильтр по типу файла */}
          <div className="room-options files-filters">
            <button
              type="button"
              className={`room-option ${effectiveFilter === 'all' ? 'room-option--selected' : ''}`}
              onClick={() => setFilter('all')}
            >
              {t('files.all')}
            </button>
            {kinds.map((k) => (
              <button
                key={k}
                type="button"
                className={`room-option ${effectiveFilter === k ? 'room-option--selected' : ''}`}
                onClick={() => setFilter(k)}
              >
                {t(`files.kindPlural.${k}`)}
              </button>
            ))}
          </div>

          {/* Список файлов */}
          <ul className="memory-list">
            {visible.map((f) => (
              <li key={f.id} className="files-item">
                <div className="files-icon" title={t(`files.kind.${f.kind}`)}>{kindIcons[f.kind]}</div>
                <div className="files-main">
                  <div className="files-name">{f.name}</div>
                  <div className="files-desc">{f.description}</div>
                  <div className="files-meta">
                    <span className="badge">{t(`files.kind.${f.kind}`)}</span>
                    <span>{f.size} · {f.date}</span>
                  </div>
                </div>
                <div className="files-actions">
                  <button type="button" className="btn btn--ghost" onClick={() => downloadBlob(f.name, f.content)}>
                    {t('common.download')}
                  </button>
                  <button
                    type="button"
                    className="btn btn--danger"
                    onClick={() => setDeleted((d) => [...d, f.id])}
                  >
                    {t('common.delete')}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
