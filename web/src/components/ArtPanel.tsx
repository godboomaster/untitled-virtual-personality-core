import { useState } from 'react';
import type { ReactNode } from 'react';
import type { Persona } from '../mockData';
import ArtCalibrator from './ArtCalibrator';
import ArtDropZone from './ArtDropZone';
import { POSE_KEYS, updatePersonaArt, usePersonaArt } from '../artStore';
import type { PersonaArt, PoseKey, RoomBgAsset, SpriteAsset } from '../artStore';
import { useI18n } from '../i18n';

/* Арт-мастерская персоны (встраивается в раздел «Комната»):
   1) промпт-пак спрайта/фона для внешних генераторов;
   2) загрузка готовых изображений (спрайт / фон комнаты — вкладки одного окна);
   3) промпт-пак и слоты загрузки по позам (art.sprites[pose]).
   Загружаются обычные изображения: файл сохраняется как есть, без
   нормализации и пиксель-арт обработки. Карточка «Стиль персонажа»
   (референс + описание, подмешивались в промпты) убрана 03.11.2026;
   хранилище стиля (artStore, /room/style) осталось — для нового места. */

// Якорь спрайта по умолчанию: низ по центру («ступни» на полу)
const SPRITE_ANCHOR = { x: 0.5, y: 0.98 };

// Точки пола фона по умолчанию (совпадают с точками перемещения в комнате)
const FLOOR_POINTS = {
  desk: { x: 0.15, y: 0.8 },
  shelf: { x: 0.5, y: 0.8 },
  window: { x: 0.85, y: 0.8 },
};

const POSE_LABEL_KEYS: Record<PoseKey, string> = {
  stand: 'art.poses.stand',
  sit: 'art.poses.sit',
  read: 'art.poses.read',
  write: 'art.poses.write',
  look: 'art.poses.look',
  sleep: 'art.poses.sleep',
};

// Английское описание позы для технического промпта (генератор — англоязычный)
const POSE_ACTION_EN: Record<PoseKey, string> = {
  stand: 'standing idle, relaxed neutral pose',
  sit: 'sitting on a chair, seen from the side',
  read: 'sitting, reading a book held in both hands',
  write: 'sitting at a desk, writing in a notebook',
  look: 'standing, looking off to the side (as if through a window)',
  sleep: 'lying down asleep, eyes closed',
};

// Промпт для спрайта персоны (англ. шаблон + описание персоны)
function buildSpritePrompt(p: Persona): string {
  return [
    '2D character illustration, full body, side view, facing right.',
    `Character: ${p.name} — ${p.description}`,
    'Tech requirements: 512x512 px, PNG with transparent background, single character, centered, feet touching the bottom edge, no floor, no baked-in shadow, no text.',
  ].join('\n');
}

// Промпт для фона комнаты (англ. шаблон + описание персоны)
function buildRoomPrompt(p: Persona): string {
  return [
    '2D interior illustration, side view of a room.',
    `Room for the character ${p.name} — ${p.description}`,
    'Wide interior: back wall, a window, furniture matching the character personality. The lower third of the image must be an empty floor plane (the character walks there).',
    'Tech requirements: 1536x640 px, muted colors, no characters in the scene, no text.',
  ].join('\n');
}

// Промпт для конкретной позы: тот же персонаж, меняется только поза
function buildPosePrompt(p: Persona, pose: PoseKey): string {
  return [
    '2D character illustration, full body, side view, facing right.',
    `Character: ${p.name} — ${p.description}`,
    `Pose: ${POSE_ACTION_EN[pose]}.`,
    'Tech requirements: 512x512 px, PNG with transparent background, single character, centered, same canvas size and scale as the character’s other poses, feet touching the bottom edge at the same point, no floor, no baked-in shadow, no text.',
  ].join('\n');
}

type EditingState = { type: 'sprite' } | { type: 'roomBg' } | { type: 'pose'; pose: PoseKey };

// Что калибровать сейчас (если ассет ещё не загружен — калибровка не открывается)
function resolveEditing(art: PersonaArt | undefined, editing: EditingState | null) {
  if (!editing) return null;
  if (editing.type === 'sprite') {
    if (!art?.sprite) return null;
    return {
      assetType: 'sprite' as const,
      sourceDataUrl: art.sprite.dataUrl,
      initialAnchor: art.sprite.anchor,
      initialFloorPoints: undefined as RoomBgAsset['floorPoints'] | undefined,
    };
  }
  if (editing.type === 'roomBg') {
    if (!art?.roomBg) return null;
    return {
      assetType: 'roomBg' as const,
      sourceDataUrl: art.roomBg.dataUrl,
      initialAnchor: undefined,
      initialFloorPoints: art.roomBg.floorPoints,
    };
  }
  const sprite = art?.sprites?.[editing.pose];
  if (!sprite) return null;
  return {
    assetType: 'sprite' as const,
    sourceDataUrl: sprite.dataUrl,
    initialAnchor: sprite.anchor,
    initialFloorPoints: undefined as RoomBgAsset['floorPoints'] | undefined,
  };
}

export default function ArtPanel({ persona, constructorSlot }: { persona: Persona; constructorSlot?: ReactNode }) {
  const { t } = useI18n();
  const art = usePersonaArt(persona.id);

  const [promptTab, setPromptTab] = useState<'sprite' | 'roomBg'>('sprite');
  // Вкладка зоны загрузки: конструктор / спрайт / фон — переключаются в одном окне
  const [uploadTab, setUploadTab] = useState<'avatar' | 'sprite' | 'roomBg'>('avatar');
  const [poseTab, setPoseTab] = useState<PoseKey>('stand');
  const [copied, setCopied] = useState<string | null>(null);
  // Открытая калибровка меток (ступни спрайта/позы или точки пола фона)
  const [editing, setEditing] = useState<EditingState | null>(null);

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

  // Загрузка изображения позы: как есть, без нормализации (как спрайт/фон)
  const readAndSavePose = (pose: PoseKey, file: File) => {
    if (!file.type.startsWith('image/')) return;
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result);
      updatePersonaArt(persona.id, { sprites: { ...art?.sprites, [pose]: { dataUrl, anchor: SPRITE_ANCHOR } } });
    };
    reader.readAsDataURL(file);
  };

  const clearPose = (pose: PoseKey) => {
    const next: Partial<Record<PoseKey, SpriteAsset>> = { ...art?.sprites };
    delete next[pose];
    updatePersonaArt(persona.id, { sprites: next });
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
  const posePrompt = buildPosePrompt(persona, poseTab);
  const editingAsset = resolveEditing(art, editing);

  return (
    <>
      {/* Промпт-пак */}
      <div className="card">
        <div className="card-title-row">
          <h3 className="card-title">{t('art.promptPack')}</h3>
          <span className="badge hud-code">ART_01</span>
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
          <span className="badge hud-code">ART_02</span>
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
            <ArtDropZone
              title={t('art.sprite')}
              spec={t('art.spriteSpec')}
              thumb={art?.sprite?.dataUrl}
              onPick={(f) => readAndSave('sprite', f)}
              onEdit={art?.sprite ? () => setEditing({ type: 'sprite' }) : undefined}
              onClear={() => updatePersonaArt(persona.id, { sprite: undefined })}
            />
          )}
          {uploadTab === 'roomBg' && (
            <ArtDropZone
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

      {/* Позы: отдельный промпт + слот загрузки на каждую позу. Во всю ширину
          мастерской: промпт позы слева, её загрузка справа — как пара
          «Промпт-пак» → «Загрузка» над ней */}
      <div className="card art-poses-card">
        <div className="card-title-row">
          <h3 className="card-title">{t('art.poses.title')}</h3>
          <span className="badge hud-code">ART_03</span>
        </div>
        <div className="tabs art-pose-tabs">
          {POSE_KEYS.map((pose) => (
            <button
              key={pose}
              type="button"
              className={`tab ${poseTab === pose ? 'tab--active' : ''}`}
              onClick={() => setPoseTab(pose)}
            >
              {t(POSE_LABEL_KEYS[pose])}
            </button>
          ))}
        </div>
        <div className="art-poses-body">
          <div>
            <pre className="art-prompt">{posePrompt}</pre>
            <div className="art-prompt-actions">
              <button type="button" className="btn btn--primary" onClick={() => copy(`pose:${poseTab}`, posePrompt)}>
                {copied === `pose:${poseTab}` ? t('common.copied') : t('common.copy')}
              </button>
            </div>
            <div className="field-hint" style={{ marginTop: 12 }}>
              {t('art.poses.promptHint')}
            </div>
          </div>
          <div className="art-pose-upload">
            <ArtDropZone
              title={t(POSE_LABEL_KEYS[poseTab])}
              spec={t('art.poses.uploadSpec')}
              thumb={art?.sprites?.[poseTab]?.dataUrl}
              onPick={(f) => readAndSavePose(poseTab, f)}
              onEdit={art?.sprites?.[poseTab] ? () => setEditing({ type: 'pose', pose: poseTab }) : undefined}
              onClear={() => clearPose(poseTab)}
            />
            <div className="field-hint">{t('art.poses.fallbackNote')}</div>
          </div>
        </div>
      </div>

      {/* Живой портрет и его анимации — в разделе «Аватар» (sections/Avatar.tsx) */}

      {/* Калибровка меток поверх раздела */}
      {editing && editingAsset && (
        <ArtCalibrator
          assetType={editingAsset.assetType}
          sourceDataUrl={editingAsset.sourceDataUrl}
          initialAnchor={editingAsset.initialAnchor}
          initialFloorPoints={editingAsset.initialFloorPoints}
          onCancel={() => setEditing(null)}
          onApply={(result) => {
            if (editing.type === 'sprite') updatePersonaArt(persona.id, { sprite: result as SpriteAsset });
            else if (editing.type === 'roomBg') updatePersonaArt(persona.id, { roomBg: result as RoomBgAsset });
            else {
              const pose = editing.pose;
              updatePersonaArt(persona.id, { sprites: { ...art?.sprites, [pose]: result as SpriteAsset } });
            }
            setEditing(null);
          }}
        />
      )}
    </>
  );
}
