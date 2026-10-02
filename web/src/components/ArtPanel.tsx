import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { Persona } from '../mockData';
import ArtCalibrator from './ArtCalibrator';
import {
  POSE_KEYS,
  updatePersonaArt,
  updatePersonaStyle,
  usePersonaArt,
  usePersonaStyle,
} from '../artStore';
import type { PersonaArt, PersonaStyle, PoseKey, RoomBgAsset, SpriteAsset } from '../artStore';
import { ApiError, api } from '../api';
import { downscaleImageFile } from '../art/imageUtils';
import { useI18n } from '../i18n';

/* Арт-мастерская персоны (встраивается в раздел «Комната»):
   1) стиль персонажа — референс-изображение + текстовое описание, которые
      сохраняются на бэкенде и подмешиваются во все промпт-паки ниже;
   2) промпт-пак спрайта/фона для внешних генераторов;
   3) загрузка готовых изображений (спрайт / фон комнаты — вкладки одного окна);
   4) промпт-пак и слоты загрузки по позам (art.sprites[pose]).
   Загружаются обычные изображения: файл сохраняется как есть, без
   нормализации и пиксель-арт обработки (кроме референса стиля — тот
   уменьшается клиентски, см. art/imageUtils). */

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

type StyleLite = Pick<PersonaStyle, 'description' | 'reference'>;

// Строка стиля для промпта: описание — если есть, иначе просто просьба
// сверяться с приложенным референсом (когда описания ещё нет)
function styleLines(style: StyleLite): string[] {
  if (style.description) {
    return [
      `Art style: ${style.description}. Match the style of the attached reference image exactly (same proportions, line, palette, shading).`,
    ];
  }
  if (style.reference) {
    return ['Match the style of the attached reference image exactly (same proportions, line, palette, shading).'];
  }
  return [];
}

// Промпт для спрайта персоны (англ. шаблон + описание из моков + стиль)
function buildSpritePrompt(p: Persona, style: StyleLite): string {
  return [
    '2D character illustration, full body, side view, facing right.',
    `Character: ${p.name} — ${p.description}`,
    ...styleLines(style),
    'Tech requirements: 512x512 px, PNG with transparent background, single character, centered, feet touching the bottom edge, no floor, no baked-in shadow, no text.',
  ].join('\n');
}

// Промпт для фона комнаты (англ. шаблон + описание из моков + стиль)
function buildRoomPrompt(p: Persona, style: StyleLite): string {
  return [
    '2D interior illustration, side view of a room.',
    `Room for the character ${p.name} — ${p.description}`,
    ...styleLines(style),
    'Wide interior: back wall, a window, furniture matching the character personality. The lower third of the image must be an empty floor plane (the character walks there).',
    'Tech requirements: 1536x640 px, muted colors, no characters in the scene, no text.',
  ].join('\n');
}

// Промпт для конкретной позы: тот же персонаж и стиль, меняется только поза
function buildPosePrompt(p: Persona, pose: PoseKey, style: StyleLite): string {
  return [
    '2D character illustration, full body, side view, facing right.',
    `Character: ${p.name} — ${p.description}`,
    `Pose: ${POSE_ACTION_EN[pose]}.`,
    ...styleLines(style),
    'Tech requirements: 512x512 px, PNG with transparent background, single character, centered, same canvas size and scale as the character’s other poses, feet touching the bottom edge at the same point, no floor, no baked-in shadow, no text.',
  ].join('\n');
}

// Расширение файла для скачивания dataURL-референса
function extFromDataUrl(url: string): string {
  const m = /^data:image\/(\w+);/.exec(url);
  if (!m) return 'png';
  return m[1] === 'jpeg' ? 'jpg' : m[1];
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
  const style = usePersonaStyle(persona.id);

  const [promptTab, setPromptTab] = useState<'sprite' | 'roomBg'>('sprite');
  // Вкладка зоны загрузки: конструктор / спрайт / фон — переключаются в одном окне
  const [uploadTab, setUploadTab] = useState<'avatar' | 'sprite' | 'roomBg'>('avatar');
  const [poseTab, setPoseTab] = useState<PoseKey>('stand');
  const [copied, setCopied] = useState<string | null>(null);
  // Открытая калибровка меток (ступни спрайта/позы или точки пола фона)
  const [editing, setEditing] = useState<EditingState | null>(null);

  // Черновик стиля: описание/референс правятся локально, на бэкенд и в
  // общий стор уходят по кнопке «Сохранить стиль» (или сразу для превью
  // референса, чтобы не потерять картинку при закрытии панели)
  const [descDraft, setDescDraft] = useState(style.description);
  const [refDraft, setRefDraft] = useState<string | undefined>(style.reference);
  const [detecting, setDetecting] = useState(false);
  const [detectNote, setDetectNote] = useState<string | null>(null);
  const [styleSaved, setStyleSaved] = useState(false);

  // Черновик правили (и ещё не сохранили) — данные стора его не перетирают
  const styleDirty = useRef(false);

  // Смена персоны — черновик стиля переинициализируется из стора этой персоны
  useEffect(() => {
    styleDirty.current = false;
    setDescDraft(style.description);
    setRefDraft(style.reference);
    setDetectNote(null);
    setStyleSaved(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona.id]);

  // Стиль пришёл с сервера после монтирования (первая синхронизация) —
  // нетронутый черновик подхватывает его; иначе «Сохранить стиль» затёр бы
  // серверное описание пустым
  useEffect(() => {
    if (styleDirty.current) return;
    setDescDraft(style.description);
    setRefDraft(style.reference);
  }, [style.description, style.reference]);

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

  // Референс стиля: уменьшается клиентски (≤768px, ≤~1 МБ) перед сохранением
  const pickReference = (file: File) => {
    downscaleImageFile(file)
      .then((dataUrl) => {
        styleDirty.current = true;
        setRefDraft(dataUrl);
        setStyleSaved(false);
      })
      .catch(() => {
        // не изображение/не удалось декодировать — молча игнорируем файл
      });
  };

  const saveStyle = () => {
    styleDirty.current = false;
    updatePersonaStyle(persona.id, { description: descDraft, reference: refDraft });
    setStyleSaved(true);
    setTimeout(() => setStyleSaved(false), 1800);
  };

  const detectStyle = () => {
    if (!refDraft) return;
    setDetecting(true);
    setDetectNote(null);
    api
      .describeRoomStyle(persona.id, refDraft)
      .then((r) => {
        styleDirty.current = true;
        setDescDraft(r.description);
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 501) setDetectNote(t('art.style.detectUnavailable'));
        else setDetectNote(t('art.style.detectError'));
      })
      .finally(() => setDetecting(false));
  };

  const useReferenceAsSprite = () => {
    if (!refDraft) return;
    updatePersonaArt(persona.id, { sprite: { dataUrl: refDraft, anchor: SPRITE_ANCHOR } });
    setEditing({ type: 'sprite' });
  };

  const copy = (key: string, text: string) => {
    navigator.clipboard?.writeText(text).catch(() => {});
    setCopied(key);
    setTimeout(() => setCopied(null), 1500);
  };

  const prompts = {
    sprite: buildSpritePrompt(persona, style),
    roomBg: buildRoomPrompt(persona, style),
  };
  const posePrompt = buildPosePrompt(persona, poseTab, style);
  const editingAsset = resolveEditing(art, editing);

  return (
    <>
      {/* Стиль персонажа: референс + описание — уходят во все промпт-паки ниже */}
      <div className="card">
        <div className="card-title-row">
          <h3 className="card-title">{t('art.style.title')}</h3>
          <span className="badge">ART_00</span>
        </div>
        <div className="art-style-grid">
          <div>
            <DropZone
              title={t('art.style.refTitle')}
              spec={t('art.style.refSpec')}
              thumb={refDraft}
              onPick={pickReference}
              onClear={() => {
                styleDirty.current = true;
                setRefDraft(undefined);
                setStyleSaved(false);
              }}
            />
            {refDraft && (
              <div className="art-style-ref-actions">
                <button type="button" className="btn btn--ghost" onClick={useReferenceAsSprite}>
                  {t('art.style.useAsSprite')}
                </button>
                <a
                  className="btn btn--ghost"
                  href={refDraft}
                  download={`style-reference.${extFromDataUrl(refDraft)}`}
                >
                  {t('art.style.downloadReference')}
                </a>
              </div>
            )}
            <div className="field-hint">{t('art.style.refHint')}</div>
          </div>
          <div className="art-style-desc">
            <div className="field-label">{t('art.style.descLabel')}</div>
            <textarea
              className="input art-style-textarea"
              rows={4}
              maxLength={1500}
              placeholder={t('art.style.descPlaceholder')}
              value={descDraft}
              onChange={(e) => {
                styleDirty.current = true;
                setDescDraft(e.target.value);
                setStyleSaved(false);
              }}
            />
            {detectNote && <div className="art-style-note">{detectNote}</div>}
            <div className="art-style-actions">
              <button
                type="button"
                className="btn btn--ghost"
                disabled={!refDraft || detecting}
                onClick={detectStyle}
              >
                {detecting ? t('art.style.detecting') : t('art.style.detect')}
              </button>
              <button type="button" className="btn btn--primary" onClick={saveStyle}>
                {styleSaved ? t('art.style.saved') : t('art.style.save')}
              </button>
            </div>
            {!refDraft && !descDraft && <div className="field-hint">{t('art.style.none')}</div>}
          </div>
        </div>
      </div>

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
          {style.reference && (
            <a className="btn btn--ghost" href={style.reference} download={`style-reference.${extFromDataUrl(style.reference)}`}>
              {t('art.style.downloadReference')}
            </a>
          )}
        </div>
        <div className="field-hint" style={{ marginTop: 12 }}>
          {t('art.hint')}
        </div>
        {style.reference && <div className="field-hint">{t('art.style.attachHint')}</div>}
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

      {/* Позы: отдельный промпт + слот загрузки на каждую позу */}
      <div className="card">
        <div className="card-title-row">
          <h3 className="card-title">{t('art.poses.title')}</h3>
          <span className="badge">ART_03</span>
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
        <pre className="art-prompt">{posePrompt}</pre>
        <div className="art-prompt-actions">
          <button type="button" className="btn btn--primary" onClick={() => copy(`pose:${poseTab}`, posePrompt)}>
            {copied === `pose:${poseTab}` ? t('common.copied') : t('common.copy')}
          </button>
        </div>
        <div className="field-hint" style={{ marginTop: 12 }}>
          {t('art.poses.promptHint')}
        </div>
        <div className="art-pose-upload">
          <DropZone
            title={t(POSE_LABEL_KEYS[poseTab])}
            spec={t('art.poses.uploadSpec')}
            thumb={art?.sprites?.[poseTab]?.dataUrl}
            onPick={(f) => readAndSavePose(poseTab, f)}
            onEdit={art?.sprites?.[poseTab] ? () => setEditing({ type: 'pose', pose: poseTab }) : undefined}
            onClear={() => clearPose(poseTab)}
          />
        </div>
        <div className="field-hint">{t('art.poses.fallbackNote')}</div>
      </div>

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
