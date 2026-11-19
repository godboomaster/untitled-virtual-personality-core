import { useCallback, useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import type { LoreFileView } from '../api';
import { confirmDialog } from '../dialogStore';
import LoreFileModal from './LoreFileModal';
import { outline, fileExt } from '../offline/loreChunk';
import {
  addPhoneLore,
  phoneLoreChunks,
  phoneLoreFiles,
  PhoneLoreError,
  PHONE_LORE_MAX_CHARS,
  removePhoneLore,
} from '../offline/localLore';
import type { LocalLoreFile } from '../offline/memory';

/* Вкладка «Лор» в приложении на телефоне без связи с ядром: файлы с ядра
   (копия на телефоне — посмотреть куски) и загруженные здесь — txt, md, docx
   читаются и режутся на телефоне (offline/localLore.ts), персона ищет по ним
   сразу, а на ядро они уходят при связи. PhonePendingLore — те же файлы
   телефона, когда ядро уже на связи: ждут переноса или ядро их не приняло. */

type Files = Awaited<ReturnType<typeof phoneLoreFiles>>;

function useFmt() {
  const { lang } = useI18n();
  const chars = (n: number) => (n >= 1000 ? `${Math.round(n / 100) / 10}k` : String(n));
  const date = (ts: number) =>
    new Date(ts * 1000).toLocaleDateString(lang === 'ru' ? 'ru-RU' : 'en-US', { day: '2-digit', month: '2-digit', year: 'numeric' });
  return { chars, date };
}

async function localView(personaId: string, f: LocalLoreFile): Promise<LoreFileView> {
  const chunks = await phoneLoreChunks(personaId, f.name);
  return {
    name: f.name,
    chars: f.chars,
    chunks: f.chunks,
    ts: f.ts,
    ext: f.ext,
    original: false,
    blocks: outline(f.text),
    pieces: chunks.map((c) => ({ s: c.s, t: c.t })),
  };
}

function LocalFileRow({
  personaId,
  file,
  onOpen,
  onDelete,
}: {
  personaId: string;
  file: LocalLoreFile;
  onOpen: (v: LoreFileView) => void;
  onDelete: (f: LocalLoreFile) => void;
}) {
  const { t } = useI18n();
  const fmt = useFmt();
  return (
    <li className="lore-file">
      <div className="files-item">
        <div className="files-main">
          <div className="files-name">{file.name}</div>
          <div className="files-meta">
            <span>{t('lore.phoneFileMeta', { chars: fmt.chars(file.chars), chunks: file.chunks, date: fmt.date(file.ts) })}</span>
          </div>
          <div className="files-desc">
            {file.error ? `⚠ ${t('lore.phoneRejected', { msg: file.error })}` : t('lore.phonePending')}
          </div>
          {!file.vectors && !file.error && <div className="files-desc">{t('lore.phoneWordsOnly')}</div>}
        </div>
        <div className="files-actions">
          <button type="button" className="btn btn--ghost" onClick={() => void localView(personaId, file).then(onOpen)}>
            {t('lore.open')}
          </button>
          <button type="button" className="btn btn--danger" onClick={() => onDelete(file)}>
            {t('common.delete')}
          </button>
        </div>
      </div>
    </li>
  );
}

function useDeleteLocal(personaId: string, reload: () => void) {
  const { t } = useI18n();
  return async (f: LocalLoreFile) => {
    const ok = await confirmDialog({
      title: t('lore.phoneDeleteTitle', { name: f.name }),
      message: t('lore.phoneDelete'),
      confirmLabel: t('common.delete'),
      danger: true,
    });
    if (!ok) return;
    await removePhoneLore(personaId, f.name);
    reload();
  };
}

export default function PhoneLorePanel({ personaId }: { personaId: string }) {
  const { t } = useI18n();
  const [files, setFiles] = useState<Files | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [opened, setOpened] = useState<LoreFileView | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const reload = useCallback(() => {
    void phoneLoreFiles(personaId).then(setFiles);
  }, [personaId]);

  useEffect(() => {
    setFiles(null);
    setError('');
    setNote('');
    reload();
  }, [reload]);

  const onDelete = useDeleteLocal(personaId, reload);

  const upload = async (f: File) => {
    setBusy(true);
    setError('');
    setNote('');
    setProgress(null);
    try {
      const entry = await addPhoneLore(personaId, f, (done, total) => setProgress({ done, total }));
      setNote(entry.vectors ? t('lore.phoneAdded', { name: entry.name }) : t('lore.phoneAddedWords', { name: entry.name }));
      reload();
    } catch (e) {
      if (e instanceof PhoneLoreError) {
        setError(t(`lore.phoneErr_${e.code}`, { max: Math.round(PHONE_LORE_MAX_CHARS / 1000), msg: e.message }));
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  const openCore = async (name: string) => {
    const chunks = await phoneLoreChunks(personaId, name);
    setOpened({
      name,
      chars: chunks.reduce((n, c) => n + c.t.length, 0),
      chunks: chunks.length,
      ts: 0,
      ext: fileExt(name),
      original: false,
      blocks: [],
      pieces: chunks.map((c) => ({ s: c.s, t: c.t })),
    });
  };

  const core = files?.core ?? [];
  const local = files?.local ?? [];
  return (
    <div className="card">
      {opened && <LoreFileModal personaId={personaId} name={opened.name} local={opened} onClose={() => setOpened(null)} />}
      <div className="card-title-row">
        <h3 className="card-title">{t('lore.title')}</h3>
        <button
          type="button"
          className="btn btn--primary"
          disabled={busy}
          aria-label={t('lore.upload')}
          title={t('lore.upload')}
          onClick={() => fileRef.current?.click()}
        >
          +
        </button>
      </div>
      <input
        ref={fileRef}
        type="file"
        hidden
        accept=".txt,.md,.docx"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void upload(f);
          e.target.value = '';
        }}
      />
      <div className="field-hint">{t('lore.phoneHint')}</div>
      {busy && (
        <div className="field-hint">
          {progress && progress.total > 0
            ? t('lore.phoneEmbedding', { done: progress.done, total: progress.total })
            : t('lore.phoneReading')}
        </div>
      )}
      {error && <div className="field-hint">⚠ {error}</div>}
      {note && <div className="field-hint">✓ {note}</div>}

      {files && !core.length && !local.length ? (
        <div className="files-empty">{t('lore.empty')}</div>
      ) : (
        <ul className="memory-list">
          {local.map((f) => (
            <LocalFileRow key={`l:${f.name}`} personaId={personaId} file={f} onOpen={setOpened} onDelete={(x) => void onDelete(x)} />
          ))}
          {core.map((f) => (
            <li key={`c:${f.name}`} className="lore-file">
              <div className="files-item">
                <div className="files-main">
                  <div className="files-name">{f.name}</div>
                  <div className="files-meta">
                    <span>{t('lore.phoneCoreMeta', { chunks: f.chunks })}</span>
                  </div>
                </div>
                <div className="files-actions">
                  <button type="button" className="btn btn--ghost" onClick={() => void openCore(f.name)}>
                    {t('lore.open')}
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Ядро на связи, а на телефоне остались свои файлы лора: ждут переноса
 * или ядро их не приняло. Нет таких — ничего. */
export function PhonePendingLore({ personaId }: { personaId: string }) {
  const { t } = useI18n();
  const [local, setLocal] = useState<LocalLoreFile[]>([]);
  const [opened, setOpened] = useState<LoreFileView | null>(null);
  const reload = useCallback(() => {
    void phoneLoreFiles(personaId).then((f) => setLocal(f.local));
  }, [personaId]);
  useEffect(() => {
    reload();
    const timer = window.setInterval(reload, 15_000); // перенос идёт в фоне
    return () => window.clearInterval(timer);
  }, [reload]);
  const onDelete = useDeleteLocal(personaId, reload);
  if (!local.length) return null;
  return (
    <div className="card">
      {opened && <LoreFileModal personaId={personaId} name={opened.name} local={opened} onClose={() => setOpened(null)} />}
      <h3 className="card-title">{t('lore.phoneLeftTitle')}</h3>
      <ul className="memory-list">
        {local.map((f) => (
          <LocalFileRow key={f.name} personaId={personaId} file={f} onOpen={setOpened} onDelete={(x) => void onDelete(x)} />
        ))}
      </ul>
    </div>
  );
}
