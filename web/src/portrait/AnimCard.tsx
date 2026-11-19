import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { Persona } from '../mockData';
import { ApiError, api } from '../api';
import { useI18n } from '../i18n';
import ArtDropZone from '../components/ArtDropZone';
import Collapsible from '../components/Collapsible';
import KeyBindings from './KeyBindings';
import AnimLinks from './AnimLinks';
import AnimSoundField from './AnimSoundField';
import NumInput from '../components/NumInput';
import Select from '../components/Select';
import { useNarrowScreen } from '../useNarrowScreen';
import { formatCombo, useKeyWords } from './animKeys';
import AnimSize from './AnimSize';
import AnimSprite, { type SpriteTravel } from './AnimSprite';
import { ANIM_MOVES, ANIM_SETTINGS_DEFAULT, animBox, animGrid, animSeconds, animsFor, NEW_ANIM } from './animTypes';
import type { AnimFaces, AnimMove, AnimSettings, AnimTrigger, AnimUpdate, PortraitAnim, PortraitAnims } from './animTypes';
import { buildAnimPrompt, otherSide, readAsDataUrl } from './animPrompts';
import type { Framing, PromptKind, Side } from './animPrompts';

/* Вкладка «Анимации» раздела «Аватар»: свои листы кадров для портрета на
   рабочем столе — ест, читает, идёт… Слева список и настройки поведения,
   справа превью и вкладки выбранной анимации. На узком экране видна одна
   панель. Нарезка и фон — на ядре (app/api/portrait_anim.py).

   Шаги: без анимации с поводом «Шаги» портрет сам не гуляет. Хватит одной
   стороны — в другую те же кадры отражаются (в превью — «Отзеркалить»);
   хотите свои кадры для другой стороны — вторая анимация «Шаги» с другим
   направлением (кнопка у направления), тогда в ту сторону играет она.

   «Движения на рабочем столе»: ходит, держат
   мышью, летит и падает — есть ли свои кадры на это и «+ Добавить»: новая
   анимация сразу с нужным поводом, названием и промптом.

   Связки (шаг «Когда играть», AnimLinks): после и перед какими другими
   играет и как часто. Звук (шаг «Звук», AnimSoundField): по умолчанию нет;
   новый файл уходит с «Сохранить» (sound_url), убрать — sound_url "".

   Аватар режима (avatar — «персона@режим», app/api/avatar_modes.py): у
   режима свои анимации. Переход — анимация с поводом «Смена режима» в
   аватаре того режима, в который переходят; «Откуда» — из каких режимов
   (ничего не выбрано — из любого). Карточка «Переходы» — какие есть и
   «+ Добавить переход». У персоны без режимов повода и карточки нет. */

const RATE_STEPS = [0, 1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 45, 60];
const PREVIEW = 132;
// Шаги в превью: следующий круг — с места последнего кадра, за край — с другой стороны
const PREVIEW_TRAVEL: SpriteTravel = { mode: 'carry' };
const THUMB = 36;
const EDITOR_TABS = [
  { id: 'sheet', label: 'anim.step.sheet' },
  { id: 'triggers', label: 'anim.when' },
  { id: 'playback', label: 'anim.editor.playback' },
] as const;
type EditorTab = (typeof EDITOR_TABS)[number]['id'];

// Поводы группами: сама, что вы с ней делаете, что с ней происходит; смена
// режима — только у персоны с режимами
const TRIGGER_GROUPS: { id: string; triggers: AnimTrigger[] }[] = [
  { id: 'self', triggers: ['idle', 'walk', 'llm'] },
  { id: 'you', triggers: ['pet', 'grab', 'fall', 'land', 'jump', 'angry'] },
  { id: 'events', triggers: ['hello', 'message', 'think', 'drop', 'sleep', 'wake', 'break', 'focus'] },
  { id: 'modes', triggers: ['mode'] },
];

/** Режим персоны для карточки: id ("" — обычный характер) и как зовут */
export interface AvatarModeRef {
  id: string;
  name: string;
}

const PROMPT_KINDS: readonly PromptKind[] = ['action', 'walk', 'grab', 'fall'];
// Сохранённая анимация → тело PUT; кадров «авто» — если их столько, сколько непустых клеток
function toUpdate(a: PortraitAnim): AnimUpdate {
  return {
    name: a.name,
    rows: a.rows,
    cols: a.cols,
    frames: a.filled != null && a.frames === a.filled ? null : a.frames,
    fps: a.fps,
    loops: a.loops,
    loop: a.loop ?? false,
    scale: a.scale,
    align: a.align,
    bg: a.bg,
    tolerance: a.tolerance,
    offset_x: a.offset_x,
    offset_y: a.offset_y,
    faces: a.faces,
    triggers: a.triggers,
    switch_from: a.switch_from ?? [],
    keys: a.keys ?? [],
    after: a.after ?? [],
    before: a.before ?? [],
    chance: a.chance ?? 100,
    sound_loop: a.sound_loop ?? false,
    volume: a.volume ?? 80,
    weight: a.weight,
    enabled: a.enabled,
  };
}

// Это правят в «Кадрах» (полоска времени и нарезка), не в карточке: при
// сохранении карточки — как записано, а не из черновика. Он мог открыться до
// правки там, и старое значение тихо откатило бы её
function framesOwned(a: PortraitAnim): Partial<AnimUpdate> {
  const u = toUpdate(a);
  return {
    fps: u.fps,
    loops: u.loops,
    loop: u.loop,
    align: u.align,
    bg: u.bg,
    tolerance: u.tolerance,
    offset_x: u.offset_x,
    offset_y: u.offset_y,
  };
}

function Thumb({ anim }: { anim: PortraitAnim }) {
  const { w, h } = animBox({ ...anim, scale: 1 }, THUMB);
  return (
    <span className="anim-thumb" aria-hidden>
      <span
        style={{
          width: w,
          height: h,
          backgroundImage: anim.atlas ? `url(${anim.atlas})` : undefined,
          backgroundSize: `${anim.cols * w}px ${animGrid(anim).rows * h}px`,
        }}
      />
    </span>
  );
}

export default function AnimCard({
  persona,
  rev = 0,
  avatar = persona.id,
  mode = '',
  modes = [],
  onChanged,
}: {
  persona: Persona;
  rev?: number;
  /** Чьи анимации: id персоны или «персона@режим» (аватар режима) */
  avatar?: string;
  /** Режим этого аватара ("" — обычный) */
  mode?: string;
  /** Все режимы персоны с обычным ("" первым); нет режимов — пусто */
  modes?: AvatarModeRef[];
  /** Анимации записаны (раздел «Аватар» обновит, что есть у режимов) */
  onChanged?: () => void;
}) {
  const { t } = useI18n();
  const narrow = useNarrowScreen();
  const keyWords = useKeyWords();
  const [data, setData] = useState<PortraitAnims | null>(null);
  const [loadError, setLoadError] = useState(false);
  // Что правим: id сохранённой, 'new' — новая, null — ничего
  const [sel, setSel] = useState<string | null>(null);
  const [editorTab, setEditorTab] = useState<EditorTab>('sheet');
  const [mobileEditorOpen, setMobileEditorOpen] = useState(false);
  const [draft, setDraft] = useState<AnimUpdate>(NEW_ANIM);
  const [pending, setPending] = useState<string | null>(null); // новый лист, ещё не сохранён
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [played, setPlayed] = useState(false);
  const [sure, setSure] = useState(false); // второе нажатие «Удалить»
  const [rate, setRate] = useState(RATE_STEPS.indexOf(ANIM_SETTINGS_DEFAULT.every_min));
  const [promptKind, setPromptKind] = useState<PromptKind>('action');
  const [framing, setFraming] = useState<Framing>('body');
  const [action, setAction] = useState('');
  const [copied, setCopied] = useState(false);
  const [hint, setHint] = useState<AnimTrigger | null>(null); // повод под мышью — его пояснение
  const [flipped, setFlipped] = useState(false); // превью шагов — в другую сторону (отражение)
  const editorRef = useRef<HTMLDivElement>(null);
  const catalogTitleRef = useRef<HTMLHeadingElement>(null);
  const actionsRef = useRef<HTMLDetailsElement>(null);
  const wasEditing = useRef(false);
  const editorId = useId();
  const showMobileEditor = sel !== null && mobileEditorOpen;

  useEffect(() => {
    let alive = true;
    setData(null);
    setLoadError(false);
    setSel(null);
    api
      .getPortraitAnims(avatar)
      .then((d) => {
        if (alive) setData(d);
      })
      .catch(() => {
        if (alive) setLoadError(true);
      });
    return () => {
      alive = false;
    };
  }, [avatar]);

  // Редактор кадров переписал атласы: черновик карточки не трогаем
  useEffect(() => {
    if (!rev) return;
    let alive = true;
    api
      .getPortraitAnims(avatar)
      .then((d) => {
        if (alive) setData(d);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [rev, avatar]);

  const settings: AnimSettings = data?.settings ?? ANIM_SETTINGS_DEFAULT;
  useEffect(() => {
    const i = RATE_STEPS.indexOf(settings.every_min);
    setRate(i >= 0 ? i : RATE_STEPS.findIndex((v) => v >= settings.every_min));
  }, [settings.every_min]);

  useEffect(() => {
    if (narrow && (showMobileEditor || wasEditing.current)) {
      const heading = showMobileEditor ? editorRef.current : catalogTitleRef.current;
      heading?.focus({ preventScroll: true });
      heading?.scrollIntoView({ block: 'start' });
    }
    wasEditing.current = showMobileEditor;
  }, [showMobileEditor, narrow]);

  useEffect(() => {
    const closeActions = (event: PointerEvent) => {
      const menu = actionsRef.current;
      if (menu?.open && event.target instanceof Node && !menu.contains(event.target)) menu.open = false;
    };
    document.addEventListener('pointerdown', closeActions);
    return () => document.removeEventListener('pointerdown', closeActions);
  }, []);

  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const count = EDITOR_TABS.length;
    let next: number;
    switch (event.key) {
      case 'ArrowLeft': next = (index + count - 1) % count; break;
      case 'ArrowRight': next = (index + 1) % count; break;
      case 'Home': next = 0; break;
      case 'End': next = count - 1; break;
      default: return;
    }
    event.preventDefault();
    const { id } = EDITOR_TABS[next];
    setEditorTab(id);
    document.getElementById(`${editorId}-tab-${id}`)?.focus();
  };

  const saved = sel && sel !== 'new' ? (data?.anims.find((a) => a.id === sel) ?? null) : null;

  const open = (id: string | null, fresh: AnimUpdate = NEW_ANIM) => {
    setSel(id);
    setMobileEditorOpen(id !== null);
    if (actionsRef.current) actionsRef.current.open = false;
    if (id === 'new') setEditorTab('sheet');
    setPending(null);
    setError('');
    setSure(false);
    setPlayed(false);
    setFlipped(false);
    const a = id && id !== 'new' ? data?.anims.find((x) => x.id === id) : null;
    setDraft(a ? toUpdate(a) : { ...fresh });
  };

  const selectAnimation = (id: string) => {
    if (id === sel) setMobileEditorOpen(true);
    else open(id);
  };

  const run = async (call: () => Promise<PortraitAnims>, after?: (d: PortraitAnims) => void) => {
    setBusy(true);
    setError('');
    try {
      const d = await call();
      setData(d);
      after?.(d);
      onChanged?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t('anim.error'));
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    if (!sel) return;
    const body: AnimUpdate = { ...draft, ...(saved && framesOwned(saved)), name: draft.name.trim() || t('anim.untitled') };
    if (pending) body.data_url = pending;
    const before = new Set(data?.anims.map((a) => a.id));
    void run(
      () => api.putPortraitAnim(avatar, sel, body),
      (d) => {
        setPending(null);
        // Новая — дальше правим её саму
        const id = sel === 'new' ? d.anims.find((a) => !before.has(a.id))?.id : sel;
        const a = d.anims.find((x) => x.id === id);
        if (a) {
          setSel(a.id);
          setDraft(toUpdate(a));
        }
      },
    );
  };

  const remove = () => {
    if (!saved) return;
    if (!sure) {
      setSure(true);
      return;
    }
    void run(
      () => api.deletePortraitAnim(avatar, saved.id),
      () => open(null),
    );
  };

  const toggle = (a: PortraitAnim) => void run(() => api.putPortraitAnim(avatar, a.id, { ...toUpdate(a), enabled: !a.enabled }));

  const putSettings = (next: AnimSettings) => void run(() => api.putPortraitAnimSettings(avatar, next));

  const play = () => {
    if (!saved) return;
    setPlayed(false);
    api
      .playPortraitAnim(avatar, saved.id)
      .then(() => {
        setPlayed(true);
        setTimeout(() => setPlayed(false), 2500);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : t('anim.error')));
  };

  const pick = (file: File) => {
    if (!file.type.startsWith('image/')) return;
    void readAsDataUrl(file).then((url) => {
      setPending(url);
      if (!draft.name.trim()) setDraft((d) => ({ ...d, name: file.name.replace(/\.[^.]+$/, '').slice(0, 40) }));
    });
  };

  const set = <K extends keyof AnimUpdate>(k: K, v: AnimUpdate[K]) => setDraft((d) => ({ ...d, [k]: v }));
  // Отметили «Шаги» — фигура в кадрах обычно идёт вбок (промпт — вправо)
  const flip = (tr: AnimTrigger) =>
    setDraft((d) => {
      const on = !d.triggers.includes(tr);
      return {
        ...d,
        triggers: on ? [...d.triggers, tr] : d.triggers.filter((x) => x !== tr),
        faces: tr === 'walk' && on && d.faces === 'none' ? 'right' : d.faces,
      };
    });

  // Шаги: куда идёт в кадрах и чем идёт в другую сторону — своей анимацией
  // шагов в ту сторону (если есть) или этими же кадрами, отражёнными
  const walking = draft.triggers.includes('walk');
  const side: Side | null = walking && draft.faces !== 'none' ? draft.faces : null;
  const ownOther = side ? (data?.anims.find((a) => a.id !== sel && a.enabled && a.triggers.includes('walk') && a.faces === otherSide(side)) ?? null) : null;
  const dirWord = (f: Side) => t(`anim.walkDir.${f}`);
  const cap = (v: string) => v.charAt(0).toUpperCase() + v.slice(1);
  // Свои кадры для другой стороны — новая анимация шагов с той же сеткой
  const addOtherSide = () => {
    if (!side || !saved) return;
    const other = otherSide(side);
    open('new', {
      ...NEW_ANIM,
      name: `${saved.name || t('anim.untitled')} (${dirWord(other)})`.slice(0, 40),
      rows: saved.rows,
      cols: saved.cols,
      fps: saved.fps,
      scale: saved.scale,
      align: saved.align,
      triggers: ['walk'],
      faces: other,
    });
    setPromptKind('walk');
  };
  const canAddOther = !!side && !ownOther && !!saved && saved.triggers.includes('walk') && saved.faces === side;

  // Движения: новая анимация сразу с поводом, названием и промптом
  const addMove = (m: AnimMove) => {
    open('new', {
      ...NEW_ANIM,
      name: t(`anim.move.name.${m}`),
      triggers: [m],
      faces: m === 'walk' ? 'right' : 'none',
    });
    setPromptKind(m);
  };

  // Переходы в этот режим: из каких режимов можно выбрать (кроме него самого)
  const hasModes = modes.length > 1;
  const modeName = (id: string) => modes.find((m) => m.id === id)?.name ?? (id || t('avatarMode.base'));
  const fromModes = modes.filter((m) => m.id !== mode);
  const switches = (data?.anims ?? []).filter((a) => a.triggers.includes('mode'));
  const addSwitch = () => {
    open('new', {
      ...NEW_ANIM,
      name: t('anim.switch.newName', { mode: modeName(mode) }).slice(0, 40),
      triggers: ['mode'],
      switch_from: [],
      loops: 1,
    });
    setPromptKind('action');
    setAction(`transforming into ${modeName(mode)}`);
  };
  const flipFrom = (id: string | null) =>
    setDraft((d) => {
      const cur = d.switch_from ?? [];
      if (id === null) return { ...d, switch_from: [] };
      return { ...d, switch_from: cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id] };
    });

  // Превью: сохранённые кадры с частотой и числом кадров из формы
  // Кадров у анимации: в редакторе кадров их убирают и добавляют с листа
  // (клетка не раз) — не столько, сколько клеток
  const limit = saved?.slots && saved.rows === draft.rows && saved.cols === draft.cols ? saved.slots : draft.rows * draft.cols;
  const previewAnim: PortraitAnim | null =
    saved && !pending && saved.rows === draft.rows && saved.cols === draft.cols
      ? {
          ...saved,
          frames: Math.max(1, Math.min(limit, draft.frames ?? saved.filled ?? saved.frames)),
          scale: 1,
        }
      : null;

  const prompt = buildAnimPrompt(promptKind, persona.name, action, draft.rows, draft.cols, framing, draft.faces === 'left' ? 'left' : 'right');
  const copy = () => {
    void navigator.clipboard?.writeText(prompt).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  // Звук: выбрали новый или убрали — из черновика, иначе — записанный
  const soundUrl = draft.sound_url !== undefined ? draft.sound_url || null : (saved?.sound_url ?? null);
  const soundName = draft.sound_url !== undefined ? (draft.sound_name ?? '') : (saved?.sound_name ?? '');
  // Сколько идёт анимация за раз (по кругу — неважно): длиннее ли звук
  const animSec = saved && !pending && !saved.loop ? animSeconds(saved) : null;

  const canSave = !!sel && !busy && (sel !== 'new' || !!pending);
  const sheet = pending ?? saved?.atlas;

  // Связки в строке списка: «после «Падает»», «перед «Машет»»
  const linkMeta = (a: PortraitAnim) =>
    (['after', 'before'] as const).flatMap((k) => {
      const ids = a[k] ?? [];
      const found = ids.map((id) => data?.anims.find((x) => x.id === id)).filter((x): x is PortraitAnim => !!x);
      return found.length ? [t(`anim.links.meta.${k}`, { names: found.map((x) => `«${x.name || t('anim.untitled')}»`).join(', ') })] : [];
    });

  const rateLabel = RATE_STEPS[rate] ? t('anim.rateEvery', { min: RATE_STEPS[rate] }) : t('anim.rateOff');
  // Гулять — только своими шагами
  const canWalk = animsFor(data, 'walk').length > 0;

  return (
    <div className="anim-tab">
      <div className="anim-catalog" hidden={narrow && showMobileEditor}>
        <div className="card anim-card">
          <div className="card-title-row">
            <h3 className="card-title" ref={catalogTitleRef} tabIndex={-1}>{t('anim.title')}</h3>
            {data && (
              <button
                type="button"
                className={`btn ${sel === 'new' ? 'btn--primary' : 'btn--ghost'} anim-add`}
                disabled={busy}
                onClick={() => selectAnimation('new')}
              >
                {t('anim.add')}
              </button>
            )}
          </div>
          {loadError && <div className="avatar-note">{t('anim.offline')}</div>}
          {error && (!sel || (narrow && !showMobileEditor)) && <div className="field-hint portrait-error" role="alert">{error}</div>}

          {data && (
            <div className="anim-list">
              {data.anims.length === 0 && <div className="field-hint">{t('anim.empty')}</div>}
              {data.anims.map((a) => (
                <div key={a.id} className={`anim-row ${sel === a.id ? 'anim-row--active' : ''} ${a.enabled ? '' : 'anim-row--off'}`}>
                  <button
                    type="button"
                    className="anim-row-main"
                    aria-pressed={sel === a.id}
                    disabled={busy}
                    onClick={() => selectAnimation(a.id)}
                  >
                    <Thumb anim={a} />
                    <span className="anim-row-text">
                      <span className="anim-row-name">{a.name || t('anim.untitled')}</span>
                      <span className="anim-row-meta">
                        {a.triggers.length || a.keys?.length || a.after?.length || a.before?.length || settings.sleep_anim === a.id
                          ? [
                              ...(settings.sleep_anim === a.id ? [t('anim.sleep.title')] : []),
                              ...a.triggers.map((tr) => t(`anim.trigger.${tr}`)),
                              ...(a.keys ?? []).map((k) => formatCombo(k, keyWords)),
                              ...linkMeta(a),
                            ].join(' · ')
                          : t('anim.noTriggers')}
                        {a.sound ? ` · ${t('anim.sound.meta')}` : ''}
                      </span>
                    </span>
                  </button>
                  <label className="switch" title={t(a.enabled ? 'anim.disable' : 'anim.enable')}>
                    <input type="checkbox" checked={a.enabled} disabled={busy} onChange={() => toggle(a)} />
                    <span className="switch-slider" />
                  </label>
                </div>
              ))}
            </div>
          )}
        </div>

        {data && (
          <div className="card anim-behavior">
            <Collapsible title={t('anim.behavior.title')}>
              <section className="anim-behavior-section">
                <div className="card-title-row">
                  <h3 className="card-title">{t('anim.selfTitle')}</h3>
                </div>
                <div className="field anim-rate">
                  <div className="field-label-row anim-rate-head">
                    <label className="field-label" htmlFor={`anim-rate-${persona.id}`}>
                      {t('anim.rate')}
                    </label>
                    <output className="anim-rate-value" htmlFor={`anim-rate-${persona.id}`}>{rateLabel}</output>
                  </div>
                  <input
                    id={`anim-rate-${persona.id}`}
                    type="range"
                    min={0}
                    max={RATE_STEPS.length - 1}
                    step={1}
                    value={Math.max(0, rate)}
                    onChange={(e) => setRate(Number(e.target.value))}
                    onPointerUp={() => putSettings({ ...settings, every_min: RATE_STEPS[rate] })}
                    onKeyUp={() => putSettings({ ...settings, every_min: RATE_STEPS[rate] })}
                  />
                  <div className="field-hint">{t('anim.rateHint')}</div>
                </div>
                <label className={`anim-switch-row ${canWalk ? '' : 'anim-switch-row--off'}`}>
                  <span className="switch">
                    <input
                      type="checkbox"
                      checked={settings.walk && canWalk}
                      disabled={!canWalk || busy}
                      onChange={() => putSettings({ ...settings, walk: !settings.walk })}
                    />
                    <span className="switch-slider" />
                  </span>
                  <span>{t('anim.walk')}</span>
                </label>
                <div className="field-hint">{t(canWalk ? 'anim.walkHint' : 'anim.walkNeed')}</div>
              </section>
              <section className="anim-behavior-section">
                <div className="field">
                  <label className="field-label" htmlFor={`anim-sleep-${persona.id}`}>{t('anim.sleep.title')}</label>
                  <Select
                    id={`anim-sleep-${persona.id}`}
                    value={settings.sleep_anim ?? ''}
                    disabled={busy}
                    options={[
                      { value: '', label: t('anim.sleep.none') },
                      ...data.anims.filter((a) => a.atlas && a.frames > 0).map((a) => ({
                        value: a.id,
                        label: a.name || t('anim.untitled'),
                        ...(!a.enabled ? { hint: t('anim.sleep.disabled') } : {}),
                      })),
                    ]}
                    onChange={(id) => putSettings({ ...settings, sleep_anim: id || null })}
                  />
                  <div className="field-hint">{t('anim.sleep.hint')}</div>
                  {data.anims.some((a) => a.id === settings.sleep_anim && !a.enabled) && (
                    <div className="field-hint">{t('anim.sleep.disabled')}</div>
                  )}
                </div>
              </section>
              <section className="anim-behavior-section anim-moves">
                <div className="card-title-row">
                  <h3 className="card-title">{t('anim.moves.title')}</h3>
                </div>
                <div className="anim-moves-list">
                  {ANIM_MOVES.map((m) => {
                    const own = animsFor(data, m);
                    return (
                      <div key={m} className="anim-move">
                        <div className="anim-move-text">
                          <span className="anim-move-name">{t(`anim.move.${m}`)}</span>
                          {own.length > 0 ? (
                            <span className="anim-move-has">
                              {own.map((a, i) => (
                                <span key={a.id}>
                                  {i > 0 && ', '}
                                  <button type="button" className="anim-move-link" disabled={busy} onClick={() => selectAnimation(a.id)}>
                                    {a.name || t('anim.untitled')}
                                  </button>
                                </span>
                              ))}
                            </span>
                          ) : (
                            <span className="anim-move-none">{t(`anim.move.none.${m}`)}</span>
                          )}
                        </div>
                        <button type="button" className="btn btn--ghost anim-move-add" disabled={busy} onClick={() => addMove(m)}>
                          {t('anim.move.add')}
                        </button>
                      </div>
                    );
                  })}
                </div>
                <div className="field-hint">{t('anim.moves.hint')}</div>
              </section>
              {hasModes && (
                <section className="anim-behavior-section anim-moves">
                  <div className="card-title-row">
                    <h3 className="card-title">{t('anim.switch.title', { mode: modeName(mode) })}</h3>
                  </div>
                  <div className="anim-moves-list">
                    {switches.length > 0 ? (
                      switches.map((a) => (
                        <div key={a.id} className="anim-move">
                          <div className="anim-move-text">
                            <button type="button" className="anim-move-link anim-move-name" disabled={busy} onClick={() => selectAnimation(a.id)}>
                              {a.name || t('anim.untitled')}
                            </button>
                            <span className="anim-move-has">
                              {a.switch_from?.length
                                ? t('anim.switch.fromList', { list: a.switch_from.map(modeName).join(', ') })
                                : t('anim.switch.fromAny')}
                            </span>
                          </div>
                        </div>
                      ))
                    ) : (
                      <div className="anim-move">
                        <div className="anim-move-text">
                          <span className="anim-move-none">{t('anim.switch.none', { mode: modeName(mode) })}</span>
                        </div>
                      </div>
                    )}
                  </div>
                  <div className="art-prompt-actions">
                    <button type="button" className="btn btn--ghost anim-move-add" disabled={busy} onClick={addSwitch}>
                      {t('anim.switch.add')}
                    </button>
                  </div>
                  <div className="field-hint">{t('anim.switch.hint', { mode: modeName(mode) })}</div>
                </section>
              )}
            </Collapsible>
          </div>
        )}
      </div>

      {!sel && !narrow && !!data?.anims.length && (
        <p className="card anim-editor-empty">{t('anim.editor.choose')}</p>
      )}
      {sel && (
        <div className="avatar-main anim-editor-pane" hidden={narrow && !showMobileEditor}>
          <div className="card anim-editor">
            <div className="card-title-row anim-editor-title" ref={editorRef} tabIndex={-1}>
              {narrow && (
                <button type="button" className="btn btn--ghost" onClick={() => setMobileEditorOpen(false)}>
                  {t('anim.editor.back')}
                </button>
              )}
              <h3 className="card-title">{sel === 'new' ? t('anim.newTitle') : draft.name.trim() || t('anim.untitled')}</h3>
              {saved && (
                <details className="anim-editor-menu" ref={actionsRef}
                  onToggle={(event) => { if (!event.currentTarget.open) setSure(false); }}
                  onKeyDown={(event) => {
                    if (event.key !== 'Escape' || !event.currentTarget.open) return;
                    event.stopPropagation();
                    event.currentTarget.open = false;
                    event.currentTarget.querySelector('summary')?.focus();
                  }}
                >
                  <summary className="btn btn--ghost" aria-label={t('anim.editor.actions')}>⋯</summary>
                  <div className="anim-editor-menu-panel">
                    {sure && <span className="anim-confirm-text">{t('anim.deleteSure', { name: saved.name })}</span>}
                    <div className="anim-editor-menu-actions">
                      <button type="button" className="btn btn--danger" disabled={busy} onClick={remove}>
                        {sure ? t('common.delete') : t('anim.delete')}
                      </button>
                      {sure && (
                        <button type="button" className="btn btn--ghost" onClick={() => setSure(false)}>
                          {t('common.cancel')}
                        </button>
                      )}
                    </div>
                  </div>
                </details>
              )}
            </div>

            {/* Превью и главное: название, включена, проиграть */}
            <div className="anim-editor-head">
              <div className="anim-preview-col">
                <div className="anim-preview">
                  {previewAnim ? (
                    <AnimSprite anim={previewAnim} size={PREVIEW} loop mirror={!!side && flipped} travel={walking ? PREVIEW_TRAVEL : undefined} />
                  ) : (
                    <div className="anim-preview-empty">{t(pending ? 'anim.previewAfterSave' : 'anim.previewNone')}</div>
                  )}
                </div>
                {/* Шаги вбок: как пойдёт в другую сторону — те же кадры, отражённые */}
                {side && previewAnim && (
                  <div className="anim-preview-dir">
                    <span>
                      {flipped
                        ? t('anim.previewDirMirrored', { dir: dirWord(otherSide(side)) })
                        : t('anim.previewDir', { dir: dirWord(side) })}
                    </span>
                    <button
                      type="button"
                      className="btn btn--ghost anim-mirror-btn"
                      aria-pressed={flipped}
                      title={t('anim.previewMirrorHint')}
                      onClick={() => setFlipped((v) => !v)}
                    >
                      {t('anim.previewMirror')}
                    </button>
                  </div>
                )}
              </div>
              <div className="anim-editor-main">
                <div className="field">
                  <label className="field-label" htmlFor={`anim-name-${persona.id}`}>
                    {t('anim.name')}
                  </label>
                  <input
                    id={`anim-name-${persona.id}`}
                    className="input"
                    value={draft.name}
                    maxLength={40}
                    placeholder={t('anim.namePlaceholder')}
                    onChange={(e) => set('name', e.target.value)}
                  />
                  <div className="field-hint">{t('anim.nameHint')}</div>
                </div>
                <div className="anim-editor-actions">
                  <label className="anim-switch-row">
                    <span className="switch">
                      <input type="checkbox" checked={draft.enabled} onChange={() => set('enabled', !draft.enabled)} />
                      <span className="switch-slider" />
                    </span>
                    <span>{t('anim.enabled')}</span>
                  </label>
                  {saved && (
                    <button type="button" className="btn btn--ghost" disabled={busy} onClick={play} title={t('anim.playHint')}>
                      {played ? t('anim.played') : t('anim.play')}
                    </button>
                  )}
                </div>
              </div>
            </div>

            <div className="tabs anim-editor-tabs" role="tablist" aria-label={t('anim.editor.tabs')}>
              {EDITOR_TABS.map(({ id, label }, index) => (
                <button
                  key={id}
                  id={`${editorId}-tab-${id}`}
                  type="button"
                  className={`tab ${editorTab === id ? 'tab--active' : ''}`}
                  role="tab"
                  aria-controls={`${editorId}-panel-${id}`}
                  aria-selected={editorTab === id}
                  tabIndex={editorTab === id ? 0 : -1}
                  onClick={() => setEditorTab(id)}
                  onKeyDown={(event) => onTabKey(event, index)}
                >
                  {t(label)}
                </button>
              ))}
            </div>

            <div
              className="anim-editor-panel"
              id={`${editorId}-panel-sheet`}
              role="tabpanel"
              aria-labelledby={`${editorId}-tab-sheet`}
              hidden={editorTab !== 'sheet'}
              tabIndex={0}
            >
              <Collapsible key={`prompt-${sel}`} title={t('anim.promptTitle')} summary={t(sheet ? 'anim.promptSummaryRedo' : 'anim.promptSummary')}>
                <div className="tabs anim-prompt-tabs">
                  {PROMPT_KINDS.map((k) => (
                    <button key={k} type="button" className={`tab ${promptKind === k ? 'tab--active' : ''}`} onClick={() => setPromptKind(k)}>
                      {t(`anim.prompt.${k}`)}
                    </button>
                  ))}
                </div>
                {/* Держат и падает — всегда во весь рост: полей нет */}
                {(promptKind === 'action' || promptKind === 'walk') && (
                  <div className="field-grid anim-prompt-fields">
                    {promptKind === 'action' && (
                      <div className="field">
                        <label className="field-label" htmlFor={`anim-action-${persona.id}`}>
                          {t('anim.action')}
                        </label>
                        <input
                          id={`anim-action-${persona.id}`}
                          className="input"
                          value={action}
                          maxLength={160}
                          placeholder={t('anim.actionPlaceholder')}
                          onChange={(e) => setAction(e.target.value)}
                        />
                      </div>
                    )}
                    <div className="field">
                      <label className="field-label">{t('anim.framing')}</label>
                      <Select
                        value={framing}
                        options={[
                          { value: 'body', label: t('anim.framingBody') },
                          { value: 'bust', label: t('anim.framingBust') },
                        ]}
                        onChange={(v) => setFraming(v === 'bust' ? 'bust' : 'body')}
                      />
                    </div>
                  </div>
                )}
                <pre className="art-prompt avatar-prompt">{prompt}</pre>
                <div className="art-prompt-actions">
                  <button type="button" className="btn btn--primary" onClick={copy}>
                    {copied ? t('common.copied') : t('portrait.copyPrompt')}
                  </button>
                </div>
                <div className="field-hint">{t('anim.promptHint', { rows: draft.rows, cols: draft.cols })}</div>
              </Collapsible>

              <ArtDropZone
                title={t('anim.sheet')}
                spec={t('anim.spec')}
                thumb={sheet}
                onPick={pick}
                onClear={pending ? () => setPending(null) : undefined}
              />
              <div className="field-grid anim-sheet-grid">
                <div className="field">
                  <label className="field-label">{t('portrait.rows')}</label>
                  <NumInput value={draft.rows} min={1} max={12} onChange={(v) => set('rows', v)} />
                </div>
                <div className="field">
                  <label className="field-label">{t('portrait.cols')}</label>
                  <NumInput value={draft.cols} min={1} max={12} onChange={(v) => set('cols', v)} />
                </div>
                <div className="field">
                  <label className="field-label">{t('anim.frames')}</label>
                  <NumInput value={draft.frames} min={1} max={limit} placeholder={t('anim.framesAuto')}
                    onChange={(v) => set('frames', v)} onClear={() => set('frames', null)} />
                </div>
              </div>
              <div className="field-hint">{t('anim.sheetHint')}</div>
            </div>

            <div
              className="anim-editor-panel"
              id={`${editorId}-panel-triggers`}
              role="tabpanel"
              aria-labelledby={`${editorId}-tab-triggers`}
              hidden={editorTab !== 'triggers'}
              tabIndex={0}
            >
              <div className="anim-trigger-groups">
                {TRIGGER_GROUPS.filter((g) => g.id !== 'modes' || hasModes || draft.triggers.includes('mode')).map((g) => (
                  <div key={g.id} className="anim-chip-group">
                    <div className="anim-chip-group-label">{t(`anim.group.${g.id}`)}</div>
                    <div className="anim-chips" role="group" aria-label={t(`anim.group.${g.id}`)}>
                      {g.triggers.map((tr) => (
                        <button
                          key={tr}
                          type="button"
                          className="anim-chip"
                          aria-pressed={draft.triggers.includes(tr)}
                          title={t(`anim.triggerHint.${tr}`)}
                          onClick={() => flip(tr)}
                          onMouseEnter={() => setHint(tr)}
                          onMouseLeave={() => setHint(null)}
                          onFocus={() => setHint(tr)}
                          onBlur={() => setHint(null)}
                        >
                          {t(`anim.trigger.${tr}`)}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
              <div className="field-hint anim-chip-hint">
                {hint ? `${t(`anim.trigger.${hint}`)} — ${t(`anim.triggerHint.${hint}`)}` : t('anim.whenHint')}
              </div>
              {/* Переход: в этот режим — из каких (ничего не выбрано — из любого) */}
              {draft.triggers.includes('mode') && (
                <div className="field anim-walk">
                  <div className="field-label">{t('anim.switch.from', { mode: modeName(mode) })}</div>
                  <div className="anim-chips" role="group" aria-label={t('anim.switch.from', { mode: modeName(mode) })}>
                    <button type="button" className="anim-chip" aria-pressed={!draft.switch_from?.length} onClick={() => flipFrom(null)}>
                      {t('anim.switch.any')}
                    </button>
                    {fromModes.map((m) => (
                      <button
                        key={m.id || '-'}
                        type="button"
                        className="anim-chip"
                        aria-pressed={!!draft.switch_from?.includes(m.id)}
                        onClick={() => flipFrom(m.id)}
                      >
                        {m.name}
                      </button>
                    ))}
                  </div>
                  <div className="field-hint">{t('anim.switch.fromHint', { mode: modeName(mode) })}</div>
                </div>
              )}
              <KeyBindings
                keys={draft.keys ?? []}
                onChange={(k) => set('keys', k)}
                others={(data?.anims ?? []).filter((a) => a.id !== sel).flatMap((a) => (a.keys ?? []).map((combo) => ({ combo, name: a.name })))}
              />
              <AnimLinks
                selfId={sel === 'new' ? null : sel}
                anims={data?.anims ?? []}
                value={{ after: draft.after ?? [], before: draft.before ?? [], chance: draft.chance ?? 100 }}
                onChange={(v) => setDraft((d) => ({ ...d, ...v }))}
              />
              {draft.triggers.includes('idle') && (
                <div className="field-grid anim-grid">
                  <div className="field">
                    <label className="field-label">{t('anim.weight')}</label>
                    <NumInput value={draft.weight} min={1} max={10} onChange={(v) => set('weight', v)} />
                  </div>
                </div>
              )}
              {walking && (
                <div className="field anim-walk">
                  <div className="field-label">{t('anim.walkFaces')}</div>
                  <div className="anim-chips" role="group" aria-label={t('anim.walkFaces')}>
                    {(['right', 'left', 'none'] as const).map((f) => (
                      <button
                        key={f}
                        type="button"
                        className="anim-chip"
                        aria-pressed={draft.faces === f}
                        onClick={() => set('faces', f as AnimFaces)}
                      >
                        {t(`anim.walkFaces.${f}`)}
                      </button>
                    ))}
                  </div>
                  <div className="field-hint">
                    {!side
                      ? t('anim.walkFront')
                      : ownOther
                        ? t('anim.walkOwn', { a: cap(dirWord(side)), b: dirWord(otherSide(side)), name: ownOther.name || t('anim.untitled') })
                        : t('anim.walkMirrored', { a: cap(dirWord(side)), b: dirWord(otherSide(side)) })}
                  </div>
                  {canAddOther && side && (
                    <div className="art-prompt-actions">
                      <button type="button" className="btn btn--ghost" onClick={addOtherSide}>
                        {t('anim.walkOther', { b: dirWord(otherSide(side)) })}
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>

            <div
              className="anim-editor-panel"
              id={`${editorId}-panel-playback`}
              role="tabpanel"
              aria-labelledby={`${editorId}-tab-playback`}
              hidden={editorTab !== 'playback'}
              tabIndex={0}
            >
              <div className="anim-playback-settings">
                <section>
                  <h4 className="field-label">{t('anim.step.play')}</h4>
                  {/* Скорость, повторы и «Зациклить» — на полоске времени в «Кадрах» */}
                  <AnimSize avatar={avatar} anim={saved && !pending ? saved : null} scale={draft.scale} onChange={(v) => set('scale', v)} />
                  <div className="field-hint">{t('anim.playInFrames')}</div>
                </section>
                <section>
                  <h4 className="field-label">{t('anim.step.sound')}</h4>
                  {editorTab === 'playback' && (!narrow || showMobileEditor) && (
                    <AnimSoundField
                      value={{ url: soundUrl, name: soundName, volume: draft.volume ?? 80, loop: !!draft.sound_loop }}
                      animSec={animSec}
                      onPick={(url, name) => setDraft((d) => ({ ...d, sound_url: url, sound_name: name }))}
                      onClear={() => setDraft((d) => ({ ...d, sound_url: '', sound_name: '' }))}
                      onVolume={(v) => set('volume', v)}
                      onLoop={(v) => set('sound_loop', v)}
                    />
                  )}
                </section>
              </div>
            </div>

            {sel === 'new' && !pending && <div className="field-hint">{t('anim.needSheet')}</div>}
            {busy && <div className="field-hint">{t('anim.busy')}</div>}
            {error && <div className="field-hint portrait-error" role="alert">{error}</div>}
            <div className="anim-editor-foot">
              <button type="button" className="btn btn--primary" disabled={!canSave} onClick={save}>
                {t('common.save')}
              </button>
              <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => open(null)}>
                {t('common.cancel')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
