import { useRef, useState } from 'react';
import { ApiError, isNativeApp } from '../api';
import type { SentFile } from '../api';
import { useI18n } from '../i18n';
import { formatFileSize, isNoAppError, openSavedFile, saveSentFile } from '../sentFiles';
import type { SavedFile } from '../sentFiles';

/* Карточка файла с компьютера ядра в пузыре ответа («пришли файл X»):
   имя, размер и кнопка. В браузере — скачать; в приложении — сохранить в
   «Загрузки» и сразу открыть приложением телефона, потом — «Открыть». */
export default function SentFileCard({ file }: { file: SentFile }) {
  const { t, lang } = useI18n();
  const native = isNativeApp();
  const [state, setState] = useState<'idle' | 'busy' | 'saved' | 'error'>('idle');
  const [note, setNote] = useState('');
  const saved = useRef<SavedFile | null>(null);

  const open = async () => {
    if (!saved.current) return;
    try {
      await openSavedFile(saved.current);
      setNote('');
    } catch (e) {
      setNote(isNoAppError(e) ? t('chat.fileNoApp') : t('chat.fileFailed', { detail: String((e as Error)?.message ?? e) }));
    }
  };

  const save = async () => {
    setState('busy');
    setNote('');
    try {
      saved.current = await saveSentFile(file);
      setState('saved');
      if (native) await open();
    } catch (e) {
      setState('error');
      setNote(t('chat.fileFailed', { detail: e instanceof ApiError ? e.message : String((e as Error)?.message ?? e) }));
    }
  };

  return (
    <div className="sent-file">
      <span className="sent-file-glyph" aria-hidden="true">▤</span>
      <span className="sent-file-meta">
        <span className="sent-file-name" title={file.name}>{file.name}</span>
        <span className="sent-file-size">
          {formatFileSize(file.size, lang)}
          {state === 'saved' && native && ` · ${t('chat.fileSaved')}`}
        </span>
        {note && <span className="sent-file-note">{note}</span>}
      </span>
      {state === 'saved' && native ? (
        <button type="button" className="btn btn--ghost btn--chip" onClick={() => void open()}>
          {t('chat.fileOpen')}
        </button>
      ) : (
        <button type="button" className="btn btn--ghost btn--chip" disabled={state === 'busy'} onClick={() => void save()}>
          {state === 'busy' ? t('chat.fileBusy') : native ? t('chat.fileSave') : t('chat.fileDownload')}
        </button>
      )}
    </div>
  );
}
