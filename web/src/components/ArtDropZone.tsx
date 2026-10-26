import { useRef, useState } from 'react';
import { useI18n } from '../i18n';

// Зона загрузки картинки арт-мастерской: file input + drag&drop + статус с
// миниатюрой (спрайт, фон комнаты, позы, листы живого портрета)
export default function ArtDropZone({
  title,
  spec,
  thumb,
  onPick,
  onEdit,
  onClear,
}: {
  title: string;
  spec: string;
  thumb?: string; // dataUrl превью, если ассет загружен
  onPick: (f: File) => void;
  onEdit?: () => void;
  onClear: () => void;
}) {
  const { t } = useI18n();
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <div
      className={`art-drop ${over ? 'art-drop--over' : ''}`}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        const f = e.dataTransfer.files?.[0];
        if (f) onPick(f);
      }}
      onClick={() => inputRef.current?.click()}
    >
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onPick(f);
          e.target.value = '';
        }}
      />
      {thumb ? (
        <img className="art-thumb" src={thumb} alt={t('art.previewAlt', { title })} />
      ) : (
        <div className="art-drop-icon">⇪</div>
      )}
      <div className="art-drop-title">{title}</div>
      <div className="art-drop-spec">{spec}</div>
      <div className={`art-drop-status ${thumb ? 'art-drop-status--ok' : ''}`}>
        {thumb ? t('art.loaded') : t('art.notLoaded')}
      </div>
      {thumb && (
        <div className="art-drop-actions" onClick={(e) => e.stopPropagation()}>
          {onEdit && (
            <button type="button" className="btn btn--ghost" onClick={onEdit}>
              {t('art.calibrate')}
            </button>
          )}
          <button type="button" className="btn btn--danger" onClick={onClear}>
            {t('common.delete')}
          </button>
        </div>
      )}
    </div>
  );
}
