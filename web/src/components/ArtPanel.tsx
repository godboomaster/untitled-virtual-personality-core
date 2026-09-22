import { useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { Persona } from '../mockData';
import ArtCalibrator from './ArtCalibrator';
import { updatePersonaArt, usePersonaArt } from '../artStore';
import { useI18n } from '../i18n';
import type { RoomBgAsset, SpriteAsset } from '../artStore';

/* Арт-панель персоны (встраивается в раздел «Комната»): промпт-паки для
   внешних генераторов и загрузка пользовательских изображений (спрайт /
   фон комнаты — вкладки одного окна). Загружаются обычные изображения:
   файл сохраняется как есть, без нормализации и пиксель-арт обработки. */

// Якорь спрайта по умолчанию: низ по центру («ступни» на полу)
const SPRITE_ANCHOR = { x: 0.5, y: 0.98 };

// Точки пола фона по умолчанию (совпадают с точками перемещения в комнате)
const FLOOR_POINTS = {
  desk: { x: 0.15, y: 0.8 },
  shelf: { x: 0.5, y: 0.8 },
  window: { x: 0.85, y: 0.8 },
};

// Промпт для спрайта персоны (англ. шаблон + описание из моков)
function buildSpritePrompt(p: Persona): string {
  return [
    '2D character illustration, full body, side view, facing right.',
    `Character: ${p.name} — ${p.description}`,
    'Tech requirements: 512x512 px, PNG with transparent background, single character, centered, feet touching the bottom edge, no floor, no baked-in shadow, no text.',
  ].join('\n');
}

// Промпт для фона комнаты (англ. шаблон + описание из моков)
function buildRoomPrompt(p: Persona): string {
  return [
    '2D interior illustration, side view of a room.',
    `Room for the character ${p.name} — ${p.description}`,
    'Wide interior: back wall, a window, furniture matching the character personality. The lower third of the image must be an empty floor plane (the character walks there).',
    'Tech requirements: 1536x640 px, muted colors, no characters in the scene, no text.',
  ].join('\n');
}

// Зона загрузки: file input + drag&drop + статус с миниатюрой
function DropZone({
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

export default function ArtPanel({ persona, constructorSlot }: { persona: Persona; constructorSlot?: ReactNode }) {
  const { t } = useI18n();
  const art = usePersonaArt(persona.id);

  const [promptTab, setPromptTab] = useState<'sprite' | 'roomBg'>('sprite');
  // Вкладка зоны загрузки: конструктор / спрайт / фон — переключаются в одном окне
  const [uploadTab, setUploadTab] = useState<'avatar' | 'sprite' | 'roomBg'>('avatar');
  const [copied, setCopied] = useState<string | null>(null);
  // Открытая калибровка меток (ступни спрайта / точки пола фона)
  const [editing, setEditing] = useState<{ type: 'sprite' | 'roomBg' } | null>(null);

  // Прочитать файл как dataURL и сохранить как есть (обычное изображение)
  const readAndSave = (type: 'sprite' | 'roomBg', file: File) => {
    if (!file.type.startsWith('image/')) return;
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result);
      if (type === 'sprite') updatePersonaArt(persona.id, { sprite: { dataUrl, anchor: SPRITE_ANCHOR } });
      else updatePersonaArt(persona.id, { roomBg: { dataUrl, floorPoints: FLOOR_POINTS } });
    };
    reader.readAsDataURL(file);
  };

  const copy = (key: string, text: string) => {
    navigator.clipboard?.writeText(text).catch(() => {});
    setCopied(key);
    setTimeout(() => setCopied(null), 1500);
  };

  const prompts = {
    sprite: buildSpritePrompt(persona),
    roomBg: buildRoomPrompt(persona),
  };

  return (
    <>
      {/* Промпт-пак */}
      <div className="card">
        <div className="card-title-row">
          <h3 className="card-title">{t('art.promptPack')}</h3>
          <span className="badge">ART_01</span>
        </div>
        <div className="tabs">
          <button
            type="button"
            className={`tab ${promptTab === 'sprite' ? 'tab--active' : ''}`}
            onClick={() => setPromptTab('sprite')}
          >
            {t('art.sprite')}
          </button>
          <button
            type="button"
            className={`tab ${promptTab === 'roomBg' ? 'tab--active' : ''}`}
            onClick={() => setPromptTab('roomBg')}
          >
            {t('art.roomBg')}
          </button>
        </div>
        <pre className="art-prompt">{prompts[promptTab]}</pre>
        <div className="art-prompt-actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => copy(promptTab, prompts[promptTab])}
          >
            {copied === promptTab ? t('common.copied') : t('common.copy')}
          </button>
        </div>
        <div className="field-hint" style={{ marginTop: 12 }}>
          {t('art.hint')}
        </div>
      </div>

      {/* Загрузка арта: конструктор / спрайт / фон — вкладки одного окна */}
      <div className="card">
        <div className="card-title-row">
          <h3 className="card-title">{t('art.upload')}</h3>
          <span className="badge">ART_02</span>
        </div>
        <div className="tabs">
          <button
            type="button"
            className={`tab ${uploadTab === 'avatar' ? 'tab--active' : ''}`}
            onClick={() => setUploadTab('avatar')}
          >
            {t('art.avatarBuilder')}
          </button>
          <button
            type="button"
            className={`tab ${uploadTab === 'sprite' ? 'tab--active' : ''}`}
            onClick={() => setUploadTab('sprite')}
          >
            {t('art.sprite')}
          </button>
          <button
            type="button"
            className={`tab ${uploadTab === 'roomBg' ? 'tab--active' : ''}`}
            onClick={() => setUploadTab('roomBg')}
          >
            {t('art.roomBg')}
          </button>
        </div>
        <div className="art-upload-body">
          {uploadTab === 'avatar' && constructorSlot}
          {uploadTab === 'sprite' && (
            <DropZone
              title={t('art.sprite')}
              spec={t('art.spriteSpec')}
              thumb={art?.sprite?.dataUrl}
              onPick={(f) => readAndSave('sprite', f)}
              onEdit={art?.sprite ? () => setEditing({ type: 'sprite' }) : undefined}
              onClear={() => updatePersonaArt(persona.id, { sprite: undefined })}
            />
          )}
          {uploadTab === 'roomBg' && (
            <DropZone
              title={t('art.roomBg')}
              spec={t('art.roomBgSpec')}
              thumb={art?.roomBg?.dataUrl}
              onPick={(f) => readAndSave('roomBg', f)}
              onEdit={art?.roomBg ? () => setEditing({ type: 'roomBg' }) : undefined}
              onClear={() => updatePersonaArt(persona.id, { roomBg: undefined })}
            />
          )}
        </div>
      </div>

      {/* Калибровка меток поверх раздела */}
      {editing && (editing.type === 'sprite' ? art?.sprite : art?.roomBg) && (
        <ArtCalibrator
          assetType={editing.type}
          sourceDataUrl={editing.type === 'sprite' ? art!.sprite!.dataUrl : art!.roomBg!.dataUrl}
          initialAnchor={art?.sprite?.anchor}
          initialFloorPoints={art?.roomBg?.floorPoints}
          onCancel={() => setEditing(null)}
          onApply={(result) => {
            if (editing.type === 'sprite') updatePersonaArt(persona.id, { sprite: result as SpriteAsset });
            else updatePersonaArt(persona.id, { roomBg: result as RoomBgAsset });
            setEditing(null);
          }}
        />
      )}
    </>
  );
}
