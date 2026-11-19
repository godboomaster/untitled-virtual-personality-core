import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import type { Persona } from '../../mockData';
import { useI18n } from '../../i18n';
import Icon from '../../components/icons';
import PersonaColorPicker from '../../components/PersonaColorPicker';
import Select from '../../components/Select';
import FrameAdd from './FrameAdd';
import { FrameThumb, NumField, Seg } from './frameFields';
import { figureCenter, frameBox, placed } from './frameAnalysis';
import FrameStage from './FrameStage';
import FrameTimeline from './FrameTimeline';
import { frameMs, stepOf } from './frameTiming';
import type { Guides, StageItem } from './frameDraw';
import { cellAdjust, cutOf, edgeLineAt, edgeLinePos, layerCell, NO_ADJUST, nudge, pivotAt, SCALE_MAX, SCALE_MIN, scopeCells, screenEdgeOf, signed, SLOTS_MAX, totalAdjust, turn, withAll, withCell, wrapCell } from './frameMath';
import { alwaysLoops, ANIM_MOVES } from '../animTypes';
import { travelOf } from '../animTravel';
import type { AnimMove } from '../animTypes';
import type { FrameAddKind } from './FrameAdd';
import { FRAME_SCOPES } from './frameTypes';
import type { FrameAdjust, FrameAlign, FrameArea, FrameBg, FrameBlend, FrameFix, FrameLayer, FrameScope, ScreenEdge } from './frameTypes';
import { totalCells, useFrameEditor } from './useFrameEditor';
import type { SourceOption } from './useFrameEditor';

// Рамка кадра (frameBox) → края от середины пола: так её задают (fix.box)
function areaOf(b: { w: number; h: number; sink: number; ax: number }): FrameArea {
  const l = Math.round(b.ax);
  return { l, r: b.w - l, t: b.h - b.sink, b: b.sink };
}

/* Вкладка «Кадры» раздела «Аватар»: нарезанные листы кладутся слоями друг на
   друга, чтобы кадры совпадали. Слева одна карточка, кнопкой — «Кадры» или
   «Слои». Кадры — сетка кадров любого листа: портрет, движения на рабочем
   столе (ходит, держат мышью, падает), свои анимации; чего нет —
   загружается здесь же. Клик открывает кадр в выбранном слое, Shift+клик
   кладёт его новым слоем (своего «+» у кадра нет: его принимали за «добавить
   кадр в анимацию» — это «+ Кадр с листа»); у анимации кадр можно
   перетащить на другое место и убрать «×». В середине холст, справа — правка текущего кадра (сдвиг, масштаб,
   поворот вокруг центра, подгонка к опорному слою) или нарезка листа. Под
   холстом у анимации полоска времени: когда идёт каждый кадр и играть ли по
   кругу. Всё применяется только по «Сохранить». */

const BLENDS: readonly FrameBlend[] = ['normal', 'flicker'];
const SCREEN_EDGES: readonly ScreenEdge[] = ['bottom', 'left', 'right', 'top'];
const BG_KEY = 'vpc-frames-bg'; // фон холста: прозрачный или свой цвет — на этом браузере
const BG_COLOR = '#808080';
const HEX = /^#[0-9a-f]{6}$/i;

function readBg(): { mode: 'checker' | 'color'; color: string } {
  try {
    const v = JSON.parse(localStorage.getItem(BG_KEY) ?? 'null') as { mode?: string; color?: string } | null;
    return { mode: v?.mode === 'color' ? 'color' : 'checker', color: v?.color && HEX.test(v.color) ? v.color : BG_COLOR };
  } catch {
    return { mode: 'checker', color: BG_COLOR };
  }
}

// Знак на образце цвета: чёрный или белый, что виднее
const inkOn = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? '#000000' : '#ffffff';
};
const FLICK_MS = 400;
const DEV_LOUD = 3; // px: кадр заметно отошёл от опорного
const MAX_LAG_MS = 500; // вкладка спала — не наверстывать кадры разом
const DRAG_PX = 6; // кадр в сетке поехал, а не щелчок
// Калька (onion skin): соседние кадры под выбранным — бледно и своим цветом.
// Цвета и прозрачность настраиваются, помнятся в этом браузере
const ONION_KEY = 'vpc-frames-onion';
interface OnionLook {
  prev: string; // цвет предыдущего кадра
  next: string; // и следующего
  alpha: number; // насколько их видно, 0.05..1
}
const ONION_DEFAULT: OnionLook = { prev: '#ff3b30', next: '#34c759', alpha: 0.35 };

function readOnion(): OnionLook {
  try {
    const v = JSON.parse(localStorage.getItem(ONION_KEY) ?? 'null') as Partial<OnionLook> | null;
    const alpha = Number(v?.alpha);
    return {
      prev: v?.prev && HEX.test(v.prev) ? v.prev : ONION_DEFAULT.prev,
      next: v?.next && HEX.test(v.next) ? v.next : ONION_DEFAULT.next,
      alpha: Number.isFinite(alpha) ? Math.max(0.05, Math.min(1, alpha)) : ONION_DEFAULT.alpha,
    };
  } catch {
    return ONION_DEFAULT;
  }
}
const SNAP_DEG = 15; // поворот с Shift

export default function FrameEditor({
  persona,
  onSaved,
  avatar = persona.id,
}: {
  persona: Persona;
  onSaved: () => void;
  /** Чьи кадры: id персоны или «персона@режим» (аватар режима) */
  avatar?: string;
}) {
  const { t, lang } = useI18n();
  const rootRef = useRef<HTMLDivElement>(null);
  // Вкладка скрыта — ни запросов, ни таймеров
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => setVisible(entries.some((e) => e.isIntersecting)));
    io.observe(el);
    return () => io.disconnect();
  }, []);

  const ed = useFrameEditor(avatar, visible, onSaved, t('frames.error'));
  const { active, activeLayer, cell, layers, sources } = ed;

  const [mode, setMode] = useState<'overlay' | 'slice'>('overlay');
  const [blend, setBlend] = useState<FrameBlend>('normal');
  const [bgMode, setBgMode] = useState(() => readBg().mode);
  const [bgColor, setBgColor] = useState(() => readBg().color);
  const bg: FrameBg = bgMode === 'checker' ? 'checker' : bgColor;
  useEffect(() => {
    try {
      localStorage.setItem(BG_KEY, JSON.stringify({ mode: bgMode, color: bgColor }));
    } catch {
      /* без памяти — до перезагрузки */
    }
  }, [bgMode, bgColor]);
  const [scope, setScope] = useState<FrameScope>('frame');
  const [hand, setHand] = useState(false);
  const [turnTool, setTurnTool] = useState(false); // «Поворот» на холсте
  const [onion, setOnion] = useState(false); // «Калька»: соседние кадры под выбранным
  const [onionLook, setOnionLook] = useState(readOnion);
  const lookOnion = (patch: Partial<OnionLook>) => setOnionLook((cur) => ({ ...cur, ...patch }));
  useEffect(() => {
    try {
      localStorage.setItem(ONION_KEY, JSON.stringify(onionLook));
    } catch {
      /* без памяти — до перезагрузки */
    }
  }, [onionLook]);
  const [side, setSide] = useState<'frames' | 'layers'>('frames'); // что в карточке слева
  const [guides, setGuides] = useState<Guides>({ on: true, x: 0, y: 0 });
  const [playing, setPlaying] = useState(false);
  const [flick, setFlick] = useState(0);
  const [fitScale, setFitScale] = useState(false);
  const [adding, setAdding] = useState<FrameAddKind | null>(null); // грузим лист, которого нет
  const dragBase = useRef<FrameFix | null>(null);
  const dragTag = useRef('');

  const { stepFrame, ensureSheet, nudgeActive, setActiveFix, undo, redo } = ed;
  const activeTotal = active ? totalCells(active) : 0;

  // Проигрывание — как на рабочем столе: со временем кадров, по кругу или
  // заданное число раз, потом снова первый кадр. played — какой кадр идёт,
  // считая с начала всех повторов; frameAt — когда он должен был появиться:
  // следующий считается от этого времени, иначе отставание отрисовки копится
  // и анимация здесь идёт медленнее, чем на самом деле
  const played = useRef(0);
  const frameAt = useRef(0);
  const triggersOf = (key: string) => ed.options.find((o) => o.key === key)?.triggers ?? [];
  const forever = !active?.timing || active.timing.loop || alwaysLoops(triggersOf(active.key));
  const frameTime = !active ? 0 : active.timing ? frameMs(active.timing, cell) : stepOf(active.fps);
  const lastPlayed = active ? active.count * (active.timing?.loops ?? 1) : 0;

  // Шаги: фигура в кадрах уходит вперёд — по кругу следующий круг идёт с
  // места последнего кадра, как на рабочем столе (animTravel.ts). laps —
  // сколько кругов прошло с «Проиграть»: на столько кругов дальше рисуются
  // кадры этого листа, ушли за край холста — входят с другой стороны
  const [laps, setLaps] = useState(0);
  const activeTriggers = active ? triggersOf(active.key) : [];
  const walks = activeTriggers.includes('walk');
  const walkTravel = useMemo(() => {
    if (!active || !walks) return null;
    const at = Array.from({ length: active.count }, (_, i) => placed(active.data, i, totalAdjust(active.fix, i))?.center ?? null);
    return travelOf(at, active.data.fw);
  }, [active, walks]);
  const carry = playing && forever && walkTravel ? laps * walkTravel.cycle : 0;

  useEffect(() => {
    if (!playing || !visible || !active) return;
    const due = frameAt.current + Math.max(16, frameTime);
    const id = setTimeout(() => {
      frameAt.current = Math.max(due, performance.now() - MAX_LAG_MS);
      played.current += 1;
      if (!forever && played.current >= lastPlayed) {
        setPlaying(false);
        stepFrame(-cell);
      } else {
        if (cell >= active.count - 1) setLaps((v) => v + 1);
        stepFrame(1);
      }
    }, Math.max(0, due - performance.now()));
    return () => clearTimeout(id);
  }, [playing, visible, active, cell, forever, frameTime, lastPlayed, stepFrame]);

  const togglePlay = () => {
    if (playing || !active) return setPlaying(false);
    const last = active.count - 1;
    if (!forever && cell >= last) {
      stepFrame(-cell);
      played.current = 0;
    } else played.current = Math.min(cell, last);
    frameAt.current = performance.now();
    setLaps(0);
    setPlaying(true);
  };

  useEffect(() => {
    if (blend !== 'flicker' || !visible) return;
    const id = setInterval(() => setFlick((v) => v + 1), FLICK_MS);
    return () => clearInterval(id);
  }, [blend, visible]);

  const activeKey = active?.key;
  useEffect(() => {
    if (mode === 'slice' && activeKey) void ensureSheet(activeKey);
  }, [mode, activeKey, ensureSheet]);

  const shownLayers = layers.filter((l) => l.visible && sources[l.source]);
  // Кто идёт по кругу дальше: выбранный слой и листаемые вместе с ним того же листа
  const walking = (l: FrameLayer) =>
    !!carry && !!activeLayer && l.source === active?.key && (l.id === activeLayer.id || (activeLayer.linked && l.linked));
  // Калька — только когда кадр правят: не при проигрывании и не в «Мигании»
  const onionOn = onion && mode === 'overlay' && blend === 'normal' && !playing;
  const items: StageItem[] = shownLayers.flatMap((l) => {
    const s = sources[l.source];
    const c = layerCell(l, totalCells(s));
    const box = frameBox(s.data, s.fix, s.frameMax);
    const frame = (i: number, opacity: number, tint: string | null, moves: boolean): StageItem => {
      const k = cutOf(s.order, i); // клетка атласа нарезки
      return {
        img: s.img,
        sx: (k % s.cols) * s.fw,
        sy: Math.floor(k / s.cols) * s.fh,
        fw: s.fw,
        fh: s.fh,
        boxW: box.w,
        boxH: box.h,
        boxSink: box.sink,
        boxAx: box.ax,
        boxFloor: box.floor,
        adjust: totalAdjust(s.fix, i),
        opacity,
        tint,
        moves,
      };
    };
    const own = frame(c, l.opacity, l.color ?? null, walking(l));
    if (!onionOn || l.id !== activeLayer?.id) return [own];
    // Соседи по времени; у зацикленной анимации за последним — первый
    const played = c < s.count;
    const n = played ? s.count : totalCells(s);
    const round = played && s.kind === 'anim' && forever;
    const prev = c > 0 ? c - 1 : round ? n - 1 : -1;
    const next = c < n - 1 ? c + 1 : round ? 0 : -1;
    return [
      ...(prev >= 0 && prev !== c ? [frame(prev, onionLook.alpha, onionLook.prev, false)] : []),
      ...(next >= 0 && next !== c && next !== prev ? [frame(next, onionLook.alpha, onionLook.next, false)] : []),
      own,
    ];
  });

  const onDrag = (dx: number, dy: number, phase: 'start' | 'move') => {
    if (!active || !activeLayer?.visible) return;
    if (phase === 'start') {
      dragBase.current = active.fix;
      dragTag.current = `drag:${Date.now()}`;
    }
    if (dragBase.current) setActiveFix(nudge(dragBase.current, scope, cell, activeTotal, active.cols, dx, dy), dragTag.current);
  };

  // Поворот на холсте: deg — с начала движения, от правки на тот момент
  const onTurn = (deg: number, phase: 'start' | 'move', snap: boolean) => {
    if (!active || !activeLayer?.visible) return;
    if (phase === 'start') {
      dragBase.current = active.fix;
      dragTag.current = `turn:${Date.now()}`;
    }
    const base = dragBase.current;
    if (base) setActiveFix(turn(base, scope, cell, activeTotal, active.cols, deg, (i) => figureCenter(active.data, i), snap ? SNAP_DEG : 0), dragTag.current);
  };

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const el = e.target as HTMLElement;
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      if (e.shiftKey) redo();
      else undo();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const step = e.shiftKey ? 10 : 1;
    const moves: Record<string, [number, number]> = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    };
    const m = moves[e.key];
    if (m && mode === 'overlay') {
      e.preventDefault();
      nudgeActive(scope, m[0], m[1], 1, 'keys');
    } else if (e.key === '[' || e.key === ']') {
      e.preventDefault();
      const ds = (e.shiftKey ? 0.02 : 0.005) * (e.key === ']' ? 1 : -1);
      nudgeActive(scope, 0, 0, 1 + ds, 'keys-scale');
    } else if (e.key === 'o' || e.key === 'O' || e.key === 'щ' || e.key === 'Щ') {
      e.preventDefault();
      setOnion((v) => !v);
    } else if (e.key === ',' || e.key === '.') {
      e.preventDefault();
      stepFrame(e.key === '.' ? 1 : -1);
    } else if (e.key === ' ' && el.tagName !== 'BUTTON') {
      e.preventDefault();
      togglePlay();
    }
  };

  // Подписи по назначению: имя анимации часто просто имя файла
  const moveOf = (o: SourceOption): AnimMove | undefined => ANIM_MOVES.find((m) => o.triggers.includes(m));
  const portraitSheets = ed.options.filter((o) => o.kind === 'sheet');
  const moveAnims = (m: AnimMove) => ed.options.filter((o) => moveOf(o) === m);
  const ownAnims = ed.options.filter((o) => o.kind === 'anim' && !moveOf(o));
  const sourceName = (key: string) => {
    const o = ed.options.find((x) => x.key === key);
    if (!o) return key;
    if (o.kind === 'sheet') {
      // Лист эмоции «id.лист»: имя эмоции и что за лист
      const [emo, slot] = o.id.includes('.') ? o.id.split('.') : ['', o.id];
      return emo ? `${o.name.split(' · ')[0]} · ${t(`frames.sheet.${slot}`)}` : t(`frames.sheet.${o.id}`);
    }
    const m = moveOf(o);
    if (!m) return o.name || t('anim.untitled');
    const same = moveAnims(m);
    return same.length > 1 ? `${t(`anim.move.${m}`)} ${same.indexOf(o) + 1}` : t(`anim.move.${m}`);
  };

  const adj: FrameAdjust = !active ? NO_ADJUST : scope === 'sheet' ? (active.fix.all ?? NO_ADJUST) : cellAdjust(active.fix, cell);
  const setAdj = (patch: Partial<FrameAdjust>) => {
    if (!active) return;
    const n = { ...adj, ...patch };
    if (scope === 'sheet') setActiveFix(withAll(active.fix, n), 'inspector');
    else nudgeActive(scope, n.dx - adj.dx, n.dy - adj.dy, n.s / adj.s, 'inspector');
  };

  // Поворот — у каждого кадра свой; поля показывают выбранный кадр, а правят
  // всё, что в «Что двигаем». Центр — где метка на холсте: px от
  // низа-середины кадра, как у направляющих
  const own = active ? cellAdjust(active.fix, cell) : NO_ADJUST;
  const rot = own.r ?? 0;
  const hasPivot = own.px != null || own.py != null;
  const center = active ? (hasPivot ? [own.px ?? 0, own.py ?? 0] : (figureCenter(active.data, cell) ?? [0, 0])) : [0, 0];
  const pivot: [number, number] | null = active ? pivotAt({ ...totalAdjust(active.fix, cell), px: center[0], py: center[1] }) : null;
  const setRot = (v: number) => ed.turnActive(scope, v - rot, 'inspector-turn');
  const setPivot = (x: number, y: number) => ed.pivotActive(scope, x, y, 'inspector-pivot');
  const pivotToFigure = () => {
    if (!active) return;
    const next = scopeCells(scope, cell, activeTotal, active.cols).reduce((f, i) => {
      const c = figureCenter(active.data, i);
      return c ? withCell(f, i, { ...cellAdjust(f, i), px: c[0], py: c[1] }) : f;
    }, active.fix);
    setActiveFix(next);
  };

  // «Лист», «Ряд», «Столбец» меняют общий масштаб, а свой у кадра — поверх
  // него: такой кадр так и остаётся крупнее или мельче остальных. Показать
  // какие и дать убрать (сдвиг кадра остаётся)
  const ownScaled =
    active && scope !== 'frame'
      ? scopeCells(scope, cell, activeTotal, active.cols).filter((i) => Math.abs(cellAdjust(active.fix, i).s - 1) > 1e-6)
      : [];
  const clearOwnScale = () => {
    if (!active) return;
    setActiveFix(ownScaled.reduce((f, i) => withCell(f, i, { ...cellAdjust(f, i), s: 1 }), active.fix));
  };
  const pct = (s: number) => (s * 100).toLocaleString(lang, { maximumFractionDigits: 1 });

  // Край экрана выбранной анимации: низ (линия над низом фигуры) или другой
  const screenEdge = active ? screenEdgeOf(active.fix) : { side: 'bottom' as const, at: 0 };

  const dev = ed.deviations.get(cell);
  const activeBox = active ? frameBox(active.data, active.fix, active.frameMax) : null;
  // Своя рамка анимации (как её возьмёт ядро — в пределах кадра): края на холсте и числа
  const activeArea = active?.kind === 'anim' && activeBox?.own ? areaOf(activeBox) : null;
  const isRefActive = ed.refLayer?.id === activeLayer?.id;
  const busy = ed.saving || !!ed.aligning;
  const sheetImg = activeKey ? (ed.sheets[activeKey] ?? null) : null;
  const canSlice = !!active && active.sheetW > 0 && !!active.cut.xs && !!active.cut.ys;

  const renderLayer = (l: FrameLayer, index: number) => {
    const s = sources[l.source];
    const on = l.id === activeLayer?.id;
    const c = s ? layerCell(l, totalCells(s)) : 0;
    return (
      <li
        key={l.id}
        className={`fe-layer ${on ? 'fe-layer--on' : ''} ${l.visible ? '' : 'fe-layer--off'}`}
        style={s ? ({ '--fe-atlas': `url(${s.atlas})` } as CSSProperties) : undefined}
        onClick={(e) => {
          // Кнопки строки делают своё и слой не выбирают: «Опорный» иначе
          // выбирал бы сам опорный слой, и сравнивать стало бы не с чем
          // Поповер цвета — портал, клики из него тоже всплывают сюда
          if (!(e.target as HTMLElement).closest('button:not(.fe-layer-name), input, .color-popover')) ed.selectLayer(l.id);
        }}
      >
        <div className="fe-layer-head">
          <button
            type="button"
            className={`fe-eye ${l.visible ? 'fe-eye--on' : ''}`}
            aria-pressed={l.visible}
            title={t('frames.layer.visible')}
            aria-label={t('frames.layer.visible')}
            onClick={() => ed.patchLayer(l.id, { visible: !l.visible })}
          >
            <Icon name="eye" size={14} />
          </button>
          <span className="fe-layer-thumb">{s && <FrameThumb s={s} cell={c} box={frameBox(s.data, s.fix, s.frameMax)} />}</span>
          <button type="button" className="fe-layer-name" aria-pressed={on}>
            <span className="fe-layer-title">
              {sourceName(l.source)}
            </span>
            <span className="fe-layer-frame">{s ? t('frames.frameTitle', { n: c + 1 }) : t('frames.loadingShort')}</span>
          </button>
          <button
            type="button"
            className={`fe-ref ${ed.refLayer?.id === l.id ? 'fe-ref--on' : ''}`}
            aria-pressed={ed.refLayer?.id === l.id}
            title={t('frames.layer.refHint')}
            onClick={() => ed.setRefId(l.id)}
          >
            {t('frames.layer.ref')}
          </button>
        </div>
        <div className="fe-layer-row">
          <button type="button" className="fe-mini" aria-label={t('frames.layer.prev')} title={t('frames.layer.prev')} onClick={() => ed.stepFrame(-1, l.id)}>
            ‹
          </button>
          <button type="button" className="fe-mini" aria-label={t('frames.layer.next')} title={t('frames.layer.next')} onClick={() => ed.stepFrame(1, l.id)}>
            ›
          </button>
          <button
            type="button"
            className={`fe-ref ${l.linked ? 'fe-ref--on' : ''}`}
            aria-pressed={l.linked}
            title={t('frames.layer.linkedHint')}
            onClick={() => ed.patchLayer(l.id, { linked: !l.linked })}
          >
            {t('frames.layer.linked')}
          </button>
          <PersonaColorPicker
            className="fe-ref"
            color={l.color}
            text={t('frames.layer.colorShort')}
            hint={t('frames.layer.colorHint')}
            label={t('frames.layer.color')}
            resetLabel={t('frames.layer.colorReset')}
            onChange={async (color) => ed.patchLayer(l.id, { color })}
          />
        </div>
        <div className="fe-layer-row">
          <input
            type="range"
            min={0}
            max={100}
            value={Math.round(l.opacity * 100)}
            aria-label={t('frames.layer.opacity')}
            title={`${t('frames.layer.opacity')}: ${Math.round(l.opacity * 100)}%`}
            onChange={(e) => ed.patchLayer(l.id, { opacity: Number(e.target.value) / 100 })}
          />
          <button type="button" className="fe-mini" disabled={index === layers.length - 1} aria-label={t('frames.layer.up')} title={t('frames.layer.up')} onClick={() => ed.moveLayer(l.id, 1)}>
            ↑
          </button>
          <button type="button" className="fe-mini" disabled={index === 0} aria-label={t('frames.layer.down')} title={t('frames.layer.down')} onClick={() => ed.moveLayer(l.id, -1)}>
            ↓
          </button>
          <button type="button" className="fe-mini" disabled={layers.length < 2} aria-label={t('frames.layer.remove')} title={t('frames.layer.remove')} onClick={() => ed.removeLayer(l.id)}>
            ×
          </button>
        </div>
      </li>
    );
  };

  const browsed = ed.browseKey ? (sources[ed.browseKey] ?? null) : null;
  const browsedBox = browsed ? frameBox(browsed.data, browsed.fix, browsed.frameMax) : { w: 1, h: 1 };
  const browsedTotal = browsed ? totalCells(browsed) : 0;
  // Переставлять и удалять кадры — только у анимации: у листа портрета место
  // кадра — ракурс головы
  const reorderable = browsed?.kind === 'anim';

  // Кадр в сетке тащат мышью: куда встанет — перед каким кадром (to)
  const gridRef = useRef<HTMLDivElement>(null);
  const press = useRef<{ from: number; x: number; y: number } | null>(null);
  const dragged = useRef(false); // после перетаскивания щелчок не открывает кадр
  const [moving, setMoving] = useState<{ from: number; to: number } | null>(null);
  // «+ Кадр с листа»: клетки листа, щелчок — ещё один кадр анимации
  const [picking, setPicking] = useState(false);
  // Только что добавленный кадр: открыт, в сетке подсвечен, под листом — строка о нём
  const [added, setAdded] = useState<{ key: string; at: number; cut: number } | null>(null);
  const pickRef = useRef<HTMLDivElement>(null);
  const sheetCells = browsed ? browsed.rows * browsed.cols : 0;
  const uses = (k: number) => (browsed?.order ? browsed.order.filter((v) => v === k).length : 1);
  const slotAt = (x: number, y: number): number => {
    const els = Array.from(gridRef.current?.querySelectorAll<HTMLElement>('.fe-cell') ?? []);
    if (!els.length) return 0;
    let row = -1;
    for (let i = 0; i < els.length; i++) {
      const r = els[i].getBoundingClientRect();
      if (y < r.top - 2 || y > r.bottom + 2) continue;
      row = i;
      if (x < r.left + r.width / 2) return i;
    }
    if (row >= 0) return row + 1;
    return y < els[0].getBoundingClientRect().top ? 0 : els.length;
  };
  const gridDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    dragged.current = false;
    const el = (e.target as HTMLElement).closest<HTMLElement>('.fe-cell');
    if (!reorderable || e.button !== 0 || e.shiftKey || !el || (e.target as HTMLElement).closest('.fe-cell-del')) return;
    press.current = { from: Number(el.dataset.i), x: e.clientX, y: e.clientY };
  };
  const gridMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const p = press.current;
    if (!p) return;
    if (!moving) {
      if (Math.abs(e.clientX - p.x) + Math.abs(e.clientY - p.y) < DRAG_PX) return;
      gridRef.current?.setPointerCapture(e.pointerId);
      dragged.current = true;
    }
    // У края сетки — листать её
    const g = gridRef.current;
    if (g) {
      const r = g.getBoundingClientRect();
      if (e.clientY < r.top + 18) g.scrollTop -= 10;
      else if (e.clientY > r.bottom - 18) g.scrollTop += 10;
    }
    setMoving({ from: p.from, to: slotAt(e.clientX, e.clientY) });
  };
  const gridUp = () => {
    const m = moving;
    press.current = null;
    setMoving(null);
    if (m && browsed) ed.moveCell(browsed.key, m.from, m.to);
  };
  const gridCancel = () => {
    press.current = null;
    setMoving(null);
  };
  // Открыли кадры листа — показать их: ниже подсказок они бывают за краем окна
  useEffect(() => {
    if (picking) pickRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [picking]);
  // Другой лист — строка о добавленном уже не про него
  useEffect(() => setAdded(null), [browsed?.key]);
  // Добавленный кадр — в видимую часть сетки (она листается своей прокруткой)
  useEffect(() => {
    const g = gridRef.current;
    const el = added && g?.querySelector<HTMLElement>(`.fe-cell[data-i="${added.at}"]`);
    if (!g || !el) return;
    const gr = g.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (r.top < gr.top || r.bottom > gr.bottom) g.scrollTop += r.top - gr.top - (gr.height - r.height) / 2;
  }, [added]);
  const addFromSheet = (k: number) => {
    if (!browsed) return;
    const at = ed.addFromSheet(browsed.key, k);
    if (at == null) return;
    if (activeLayer) ed.openFrame(activeLayer.id, browsed.key, at);
    setAdded({ key: browsed.key, at, cut: k });
  };
  // Какие кадры этого листа сейчас лежат в слоях
  const onLayers = new Set(browsed ? layers.filter((l) => l.source === browsed.key).map((l) => layerCell(l, totalCells(browsed))) : []);
  const browse = (key: string) => {
    setAdding(null);
    ed.setBrowse(key);
  };
  const chip = (o: SourceOption, label: string) => (
    <button
      key={o.key}
      type="button"
      className={`fe-chip ${!adding && o.key === ed.browseKey ? 'fe-chip--on' : ''}`}
      aria-pressed={!adding && o.key === ed.browseKey}
      title={o.kind === 'sheet' ? sourceName(o.key) : o.name}
      onClick={() => browse(o.key)}
    >
      {label}
    </button>
  );
  const addChip = (kind: FrameAddKind, label: string) => (
    <button key={kind} type="button" className={`fe-chip fe-chip--add ${adding === kind ? 'fe-chip--on' : ''}`} aria-pressed={adding === kind} onClick={() => setAdding(kind)}>
      {label}
    </button>
  );
  const framesBody = (
    <>
      <div className="fe-sources" role="group" aria-label={t('frames.chipsLabel')}>
        {portraitSheets.length > 0 && (
          <div className="fe-source-row">
            <span className="fe-label">{t('frames.group.portrait')}</span>
            <div className="fe-chips">{portraitSheets.map((o) => chip(o, o.name))}</div>
          </div>
        )}
        <div className="fe-source-row">
          <span className="fe-label">{t('frames.group.moves')}</span>
          <div className="fe-chips">
            {ANIM_MOVES.flatMap((m) => {
              const own = moveAnims(m);
              return own.length ? own.map((o) => chip(o, sourceName(o.key))) : [addChip(m, `+ ${t(`anim.move.${m}`)}`)];
            })}
          </div>
        </div>
        <div className="fe-source-row">
          <span className="fe-label">{t('frames.group.anims')}</span>
          <div className="fe-chips">
            {ownAnims.map((o) => chip(o, sourceName(o.key)))}
            {addChip('action', t('frames.add.new'))}
          </div>
        </div>
      </div>
      {adding ? (
        <FrameAdd
          key={adding}
          persona={persona}
          avatar={avatar}
          kind={adding}
          onDone={(a) => {
            setAdding(null);
            ed.animAdded(a);
          }}
          onCancel={() => setAdding(null)}
        />
      ) : !browsed ? (
        <p className="fe-note">{t(ed.browseKey ? 'frames.loading' : 'frames.empty')}</p>
      ) : (
        <div
          ref={gridRef}
          className={`fe-grid ${moving ? 'fe-grid--moving' : ''}`}
          style={{ gridTemplateColumns: `repeat(${browsed.cols}, minmax(0, 1fr))`, '--fe-atlas': `url(${browsed.atlas})` } as CSSProperties}
          onPointerDown={gridDown}
          onPointerMove={gridMove}
          onPointerUp={gridUp}
          onPointerCancel={gridCancel}
        >
          {Array.from({ length: browsedTotal }, (_, i) => {
            const own = browsed.key === active?.key;
            const d = own ? ed.deviations.get(i) : undefined;
            const size = d ? Math.max(Math.abs(d.dx), Math.abs(d.dy)) : 0;
            const open = () => activeLayer && ed.openFrame(activeLayer.id, browsed.key, i);
            const drop = moving && moving.to !== moving.from && moving.to !== moving.from + 1 ? moving.to : -1;
            return (
              <div
                key={i}
                data-i={i}
                className={[
                  'fe-cell',
                  own && i === cell ? 'fe-cell--on' : onLayers.has(i) ? 'fe-cell--layer' : '',
                  i >= browsed.count ? 'fe-cell--off' : '',
                  moving?.from === i ? 'fe-cell--drag' : '',
                  added?.key === browsed.key && added.at === i ? 'fe-cell--new' : '',
                  drop === i ? 'fe-cell--before' : '',
                  drop === browsedTotal && i === browsedTotal - 1 ? 'fe-cell--after' : '',
                ].join(' ')}
              >
                <button
                  type="button"
                  className="fe-cell-open"
                  title={`${t('frames.cellOf', { n: i + 1, cut: cutOf(browsed.order, i) + 1 })}${i >= browsed.count ? `. ${t('frames.offCell')}` : ''}`}
                  aria-label={t('frames.cellOf', { n: i + 1, cut: cutOf(browsed.order, i) + 1 })}
                  aria-pressed={own && i === cell}
                  onClick={(e) => {
                    if (dragged.current) return;
                    if (e.shiftKey) ed.addFrame(browsed.key, i);
                    else open();
                  }}
                >
                  <FrameThumb s={browsed} cell={i} box={browsedBox} />
                  <span className="fe-cell-n">
                    {i + 1}
                    {browsed.fix.cells?.[i] ? '*' : ''}
                  </span>
                  {size > 0 && <span className={`fe-cell-dev ${size >= DEV_LOUD ? 'fe-cell-dev--loud' : ''}`}>{size}</span>}
                </button>
                {reorderable && browsedTotal > 1 && (
                  <button
                    type="button"
                    className="fe-cell-del"
                    aria-label={t('frames.cell.remove', { n: i + 1 })}
                    title={t('frames.cell.remove', { n: i + 1 })}
                    onClick={() => ed.deleteCell(browsed.key, i)}
                  >
                    ×
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
      {!adding && browsed && reorderable && (
        <div className="fe-pick" ref={pickRef}>
          <button
            type="button"
            className={`fe-chip fe-chip--add fe-pick-toggle ${picking ? 'fe-chip--on' : ''}`}
            aria-expanded={picking}
            disabled={browsedTotal >= SLOTS_MAX}
            title={t('frames.pick.hint')}
            onClick={() => setPicking((v) => !v)}
          >
            {t(picking ? 'frames.pick.close' : 'frames.pick.open')}
          </button>
          {picking && (
            <>
              <div
                className="fe-grid fe-grid--sheet"
                style={{ gridTemplateColumns: `repeat(${browsed.cols}, minmax(0, 1fr))`, '--fe-atlas': `url(${browsed.atlas})` } as CSSProperties}
              >
                {Array.from({ length: sheetCells }, (_, k) => {
                  const n = uses(k);
                  return (
                    <div key={k} className={`fe-cell ${n === 0 ? 'fe-cell--off' : ''}`}>
                      <button
                        type="button"
                        className="fe-cell-open"
                        aria-label={t('frames.pick.add', { n: k + 1 })}
                        title={t('frames.pick.add', { n: k + 1 })}
                        disabled={browsedTotal >= SLOTS_MAX}
                        onClick={() => addFromSheet(k)}
                      >
                        <FrameThumb s={browsed} cell={k} box={{ w: browsed.fw, h: browsed.fh }} sheet />
                        <span className="fe-cell-n">{k + 1}</span>
                        <span className="fe-cell-dev" title={t('frames.pick.uses', { n })}>
                          {n === 0 ? '—' : `×${n}`}
                        </span>
                        <span className="fe-pick-plus" aria-hidden>
                          +
                        </span>
                      </button>
                    </div>
                  );
                })}
              </div>
              <p className="fe-note fe-pick-added" role="status">
                {added?.key === browsed.key ? t('frames.pick.added', { cut: added.cut + 1, n: added.at + 1 }) : ''}
              </p>
              <p className="fe-note">{t('frames.pick.note')}</p>
            </>
          )}
        </div>
      )}
      {!adding && browsed && (
        <>
          <div className="fe-actions">
            <button type="button" className="btn btn--ghost" title={t('frames.soloHint')} onClick={() => ed.soloSource(browsed.key)}>
              {t('frames.solo')}
            </button>
            {browsed.order && (
              <button type="button" className="btn btn--ghost" title={t('frames.restoreOrderHint')} onClick={() => ed.restoreOrder(browsed.key)}>
                {t('frames.restoreOrder')}
              </button>
            )}
          </div>
          <p className="fe-note">
            {t('frames.pickHint')}
            {reorderable ? ` ${t('frames.moveHint')}` : ''}
            {browsed.key === active?.key && ed.deviations.size > 0 ? ` ${t('frames.gridHint')}` : ''}
            {browsed.count < browsedTotal ? ` ${t('frames.offHint')}` : ''}
          </p>
        </>
      )}
    </>
  );

  const framePanel = active && (
    <div className="card fe-panel">
      <div className="fe-panel-head">
        <h3 className="fe-panel-title">{t('frames.frameTitle', { n: cell + 1 })}</h3>
        <span className="fe-panel-note">{t('frames.framePlace', { row: Math.floor(cell / active.cols) + 1, col: (cell % active.cols) + 1 })}</span>
      </div>
      <span className="fe-label">{t('frames.scopeLabel')}</span>
      <Seg value={scope} options={FRAME_SCOPES} label={(v) => t(`frames.scope.${v}`)} onChange={setScope} />
      <div className="fe-nums">
        <NumField label="X" value={adj.dx} suffix="px" onChange={(v) => setAdj({ dx: Math.round(v) })} />
        <NumField label="Y" value={adj.dy} suffix="px" onChange={(v) => setAdj({ dy: Math.round(v) })} />
      </div>
      <NumField
        label={t('frames.scale')}
        value={Math.round(adj.s * 1000) / 10}
        step={0.5}
        min={SCALE_MIN * 100}
        max={SCALE_MAX * 100}
        suffix="%"
        onChange={(v) => setAdj({ s: v / 100 })}
      />
      {/* Ползунок — по степеням двойки: середина — 100 %, влево до 25 %, вправо до 400 % */}
      <input
        type="range"
        className="fe-scale"
        min={Math.round(Math.log2(SCALE_MIN) * 100)}
        max={Math.round(Math.log2(SCALE_MAX) * 100)}
        step={1}
        value={Math.round(Math.log2(Math.max(SCALE_MIN, Math.min(SCALE_MAX, adj.s))) * 100)}
        aria-label={t('frames.scale')}
        aria-valuetext={`${Math.round(adj.s * 100)} %`}
        onChange={(e) => setAdj({ s: 2 ** (Number(e.target.value) / 100) })}
      />
      {ownScaled.length > 0 && active && (
        <div className="fe-own-scale">
          <p className="fe-note">
            {t('frames.ownScale', {
              list: ownScaled.map((i) => t('frames.ownScaleItem', { n: i + 1, pct: pct(cellAdjust(active.fix, i).s) })).join(', '),
            })}
          </p>
          <button type="button" className="btn btn--ghost" onClick={clearOwnScale}>
            {t('frames.ownScaleClear')}
          </button>
        </div>
      )}
      <NumField label={t('frames.rotate')} value={rot} step={1} min={-180} max={180} suffix="°" onChange={(v) => setRot(v)} />
      <input
        type="range"
        className="fe-scale"
        min={-180}
        max={180}
        step={0.5}
        value={rot}
        aria-label={t('frames.rotate')}
        onChange={(e) => setRot(Number(e.target.value))}
      />
      <span className="fe-label">{t('frames.pivot')}</span>
      <div className="fe-nums">
        <NumField label="X" value={Math.round(pivot?.[0] ?? 0)} suffix="px" onChange={(v) => setPivot(v, pivot?.[1] ?? 0)} />
        <NumField label="Y" value={Math.round(pivot?.[1] ?? 0)} suffix="px" onChange={(v) => setPivot(pivot?.[0] ?? 0, v)} />
      </div>
      <div className="fe-actions">
        <button type="button" className="btn btn--ghost" title={t('frames.pivot.figureHint')} onClick={pivotToFigure}>
          {t('frames.pivot.figure')}
        </button>
        <button type="button" className={`fe-tool ${turnTool ? 'fe-tool--on' : ''}`} aria-pressed={turnTool} title={t('frames.turnHint')} onClick={() => setTurnTool((v) => !v)}>
          {t('frames.pivot.place')}
        </button>
      </div>
      <div className="fe-actions">
        <button type="button" className="btn btn--ghost" onClick={() => ed.resetFix(scope)}>
          {t('frames.reset')}
        </button>
      </div>
      {activeBox && !activeBox.own && activeBox.cut && (
        <p className="fe-note fe-note--warn">
          {t('frames.clipNote', { w: activeBox.w, h: activeBox.h })}
          {active.kind === 'anim' ? ` ${t('frames.clipAnim')}` : ''}
        </p>
      )}
      {activeBox && !activeBox.own && !activeBox.cut && (activeBox.w > active.fw || activeBox.h > active.fh) && (
        <p className="fe-note">
          {t('frames.grownNote', { w: activeBox.w, h: activeBox.h })}
          {activeBox.sink > 0 ? ` ${t('frames.sinkNote', { n: activeBox.sink })}` : ''}
          {active.kind === 'anim' && Math.max(activeBox.w, activeBox.h) > Math.max(active.fw, active.fh)
            ? ` ${t('frames.grownSmaller', { p: Math.round((Math.max(active.fw, active.fh) / Math.max(activeBox.w, activeBox.h)) * 100) })}`
            : ''}
        </p>
      )}
      {active.kind === 'anim' && activeBox && (
        <>
          {/* Край экрана: какой линией фигура стоит на нижнем крае или из-за
              какого края подглядывает (сбоку, сверху) */}
          <div className="fe-divider" />
          <span className="fe-label">{t('frames.edge')}</span>
          <Seg
            value={screenEdge.side}
            options={SCREEN_EDGES}
            label={(v) => t(`frames.edge.${v}`)}
            onChange={(v) => ed.setScreenEdge(v, v === 'bottom' ? 0 : Math.round((v === 'top' ? active.fh : active.fw) * 0.45))}
          />
          {screenEdge.side === 'bottom' && (
            <Seg
              value={screenEdge.at > 0 ? 'own' : 'feet'}
              options={['feet', 'own'] as const}
              label={(v) => t(`frames.floor.${v}`)}
              onChange={(v) => ed.setScreenEdge('bottom', v === 'own' ? Math.round(active.fh * 0.45) : 0)}
            />
          )}
          {(screenEdge.side !== 'bottom' || screenEdge.at > 0) && (
            <NumField
              label={t(screenEdge.side === 'bottom' ? 'frames.floor.lift' : `frames.edge.at.${screenEdge.side}`)}
              value={screenEdge.at}
              min={0}
              max={screenEdge.side === 'left' || screenEdge.side === 'right' ? active.fw : active.fh}
              suffix="px"
              onChange={(v) => ed.setScreenEdge(screenEdge.side, Math.max(0, Math.round(v)), 'edge:num')}
            />
          )}
          <p className="fe-note">
            {screenEdge.side === 'bottom'
              ? t(screenEdge.at > 0 ? 'frames.floor.ownHint' : 'frames.floor.feetHint', { n: screenEdge.at })
              : t(`frames.edge.hint.${screenEdge.side}`)}
          </p>

          <div className="fe-divider" />
          <span className="fe-label">{t('frames.area')}</span>
          <Seg
            value={activeBox.own ? 'own' : 'auto'}
            options={['auto', 'own'] as const}
            label={(v) => t(`frames.area.${v}`)}
            onChange={(v) => ed.setArea(v === 'own' ? areaOf(activeBox) : null)}
          />
          {activeArea ? (
            <>
              <div className="fe-nums">
                {(['l', 'r', 't', 'b'] as const).map((k) => (
                  <NumField
                    key={k}
                    label={t(`frames.area.${k}`)}
                    value={activeArea[k]}
                    min={k === 'b' ? 0 : 1}
                    max={active.frameMax}
                    suffix="px"
                    onChange={(v) => ed.setArea({ ...activeArea, [k]: Math.max(k === 'b' ? 0 : 1, Math.round(v)) }, `area:${k}`)}
                  />
                ))}
              </div>
              <p className="fe-note">{t('frames.area.hint', { w: activeBox.w, h: activeBox.h })}</p>
              {activeBox.over && <p className="fe-note fe-note--warn">{t('frames.area.over')}</p>}
            </>
          ) : (
            <p className="fe-note">{t('frames.area.autoHint')}</p>
          )}
        </>
      )}

      <div className="fe-divider" />
      <span className="fe-label">{t('frames.compare')}</span>
      {isRefActive ? (
        <p className="fe-note">{t('frames.refSame')}</p>
      ) : (
        <>
          <dl className="fe-stats">
            <div>
              <dt>{t('frames.devX')}</dt>
              <dd>{dev ? `${signed(dev.dx)} px` : '—'}</dd>
            </div>
            <div>
              <dt>{t('frames.devY')}</dt>
              <dd>{dev ? `${signed(dev.dy)} px` : '—'}</dd>
            </div>
            <div>
              <dt>{t('frames.diff')}</dt>
              <dd>{ed.diffPercent == null ? '—' : `${ed.diffPercent.toFixed(1)} %`}</dd>
            </div>
          </dl>
          <span className="fe-label">{t('frames.alignLabel', { scope: t(`frames.scopeOf.${scope}`) })}</span>
          <div className="fe-align">
            {(['image', 'bottom', 'center'] as FrameAlign[]).map((how) => (
              <button key={how} type="button" className="btn btn--ghost" disabled={busy} title={t(`frames.align.${how}Hint`)} onClick={() => void ed.align(how, scope, fitScale)}>
                {t(`frames.align.${how}`)}
              </button>
            ))}
          </div>
          <label className="fe-check">
            <input type="checkbox" checked={fitScale} onChange={(e) => setFitScale(e.target.checked)} />
            <span>{t('frames.align.fitScale')}</span>
          </label>
          {ed.aligning && <p className="fe-note">{t('frames.aligning', { done: ed.aligning[0], total: ed.aligning[1] })}</p>}
        </>
      )}
    </div>
  );

  const slicePanel = active && (
    <div className="card fe-panel">
      <div className="fe-panel-head">
        <h3 className="fe-panel-title">{t('frames.sliceTitle')}</h3>
        <span className="fe-panel-note">{sourceName(active.key)}</span>
      </div>
      {!canSlice ? (
        <p className="fe-note">{t('frames.noSheet')}</p>
      ) : (
        <>
          <div className="fe-nums">
            <NumField label={t('frames.rows')} value={active.cut.rows} min={1} max={active.maxGrid} onChange={(v) => ed.setGrid(Math.round(v), active.cut.cols)} />
            <NumField label={t('frames.cols')} value={active.cut.cols} min={1} max={active.maxGrid} onChange={(v) => ed.setGrid(active.cut.rows, Math.round(v))} />
          </div>
          <span className="fe-label">{t('frames.margins')}</span>
          <div className="fe-nums">
            <NumField label={t('frames.margin.left')} value={active.cut.xs![0]} min={0} onChange={(v) => ed.setMargin('left', Math.round(v))} />
            <NumField label={t('frames.margin.right')} value={active.sheetW - active.cut.xs![active.cut.cols]} min={0} onChange={(v) => ed.setMargin('right', Math.round(v))} />
            <NumField label={t('frames.margin.top')} value={active.cut.ys![0]} min={0} onChange={(v) => ed.setMargin('top', Math.round(v))} />
            <NumField label={t('frames.margin.bottom')} value={active.sheetH - active.cut.ys![active.cut.rows]} min={0} onChange={(v) => ed.setMargin('bottom', Math.round(v))} />
          </div>
          <div className="fe-actions">
            <button type="button" className="btn btn--ghost" onClick={ed.evenOut}>
              {t('frames.evenOut')}
            </button>
          </div>
          <div className="fe-divider" />
          <span className="fe-label">{t('frames.bg')}</span>
          <Select
            value={active.cut.bg}
            options={[
              { value: 'auto', label: t('frames.bg.auto') },
              { value: 'keep', label: t('frames.bg.keep') },
            ]}
            onChange={(v) => ed.setCutField({ bg: v as 'auto' | 'keep' })}
          />
          {active.cut.bg === 'auto' && (
            <>
              <span className="fe-label">{t('frames.tolerance', { v: active.cut.tolerance || t('frames.auto') })}</span>
              <input type="range" min={0} max={120} value={active.cut.tolerance} aria-label={t('frames.toleranceShort')} onChange={(e) => ed.setCutField({ tolerance: Number(e.target.value) })} />
            </>
          )}
          {active.kind === 'anim' && (
            <>
              <span className="fe-label">{t('frames.alignMode')}</span>
              <Select
                value={active.cut.align ?? 'grid'}
                options={[
                  { value: 'grid', label: t('anim.alignGrid'), hint: t('frames.alignMode.gridHint') },
                  { value: 'feet', label: t('anim.alignFeet'), hint: t('frames.alignMode.feetHint') },
                ]}
                onChange={(v) => ed.setCutField({ align: v as 'grid' | 'feet' })}
              />
            </>
          )}
          <p className="fe-note">{t(active.kind === 'sheet' ? 'frames.slice.sheetNote' : 'frames.slice.animNote')}</p>
        </>
      )}
    </div>
  );

  // Новый слой — с листа в сетке слева: со своего листа следующий кадр (тот же
  // лёг бы точно поверх и разницы не показал), с другого — первый
  const newLayerFrom = browsed ?? active;
  const addLayer = () => {
    if (newLayerFrom) ed.addFrame(newLayerFrom.key, newLayerFrom.key === active?.key ? wrapCell(cell + 1, newLayerFrom.count) : 0);
  };

  const dirtyNames = ed.dirty.map((s) => sourceName(s.key)).join(', ');
  // Нарезка: на клетках листа — номера кадров анимации, удалённые — «×»
  const sliceOrder = active?.order ?? null;
  const sliceCells = active ? active.rows * active.cols : 0;
  const sliceLabels = useMemo(
    () =>
      sliceOrder
        ? Array.from({ length: sliceCells }, (_, k) => {
            // Клетка идёт не раз — все её кадры: «2, 4»
            const at = sliceOrder.flatMap((v, j) => (v === k ? [j + 1] : []));
            return at.length ? at.join(', ') : null;
          })
        : undefined,
    [sliceOrder, sliceCells],
  );
  // Полоска — у анимации из сетки кадров, а у листа портрета — выбранного слоя
  const timed = mode !== 'overlay' ? null : browsed?.timing ? browsed : active?.timing ? active : null;

  return (
    <div ref={rootRef} className="fe-root" tabIndex={-1} onKeyDown={onKey}>
      {!ed.ready ? (
        <div className="card fe-empty">{ed.error || t('frames.loading')}</div>
      ) : (
        <>
          <div className="fe-bar">
            <Seg value={mode} options={['overlay', 'slice'] as const} label={(v) => t(`frames.mode.${v}`)} onChange={setMode} />
            {mode === 'overlay' && <Seg value={blend} options={BLENDS} label={(v) => t(`frames.blend.${v}`)} onChange={setBlend} />}
            <div className="fe-bgs" role="group" aria-label={t('frames.bgLabel')}>
              <button
                type="button"
                className={`fe-bg fe-bg--checker ${bgMode === 'checker' ? 'fe-bg--on' : ''}`}
                aria-pressed={bgMode === 'checker'}
                aria-label={t('frames.stageBg.checker')}
                title={t('frames.stageBg.checker')}
                onClick={() => setBgMode('checker')}
              />
              {/* Свой цвет: щелчок — сразу этот фон и выбор цвета (системный) */}
              <label className={`fe-bg fe-bg--color ${bgMode === 'color' ? 'fe-bg--on' : ''}`} style={{ background: bgColor }} title={t('frames.stageBg.color')}>
                <input
                  type="color"
                  className="fe-bg-input"
                  value={bgColor}
                  aria-label={t('frames.stageBg.color')}
                  onClick={() => setBgMode('color')}
                  onChange={(e) => {
                    setBgColor(e.target.value);
                    setBgMode('color');
                  }}
                />
                <span className="fe-bg-caret" style={{ color: inkOn(bgColor) }} aria-hidden>
                  ▾
                </span>
              </label>
            </div>
            <button type="button" className={`fe-tool ${hand ? 'fe-tool--on' : ''}`} aria-pressed={hand} title={t('frames.handHint')} onClick={() => setHand((v) => !v)}>
              {t('frames.hand')}
            </button>
            {mode === 'overlay' && (
              <>
                <button type="button" className={`fe-tool ${guides.on ? 'fe-tool--on' : ''}`} aria-pressed={guides.on} title={t('frames.guidesHint')} onClick={() => setGuides((g) => ({ ...g, on: !g.on }))}>
                  {t('frames.guides')}
                </button>
                <button type="button" className={`fe-tool ${turnTool ? 'fe-tool--on' : ''}`} aria-pressed={turnTool} title={t('frames.turnHint')} onClick={() => setTurnTool((v) => !v)}>
                  {t('frames.turn')}
                </button>
                <div className="fe-onion" role="group" aria-label={t('frames.onion')}>
                  <button type="button" className={`fe-tool ${onion ? 'fe-tool--on' : ''}`} aria-pressed={onion} title={t('frames.onionHint')} onClick={() => setOnion((v) => !v)}>
                    {t('frames.onion')}
                  </button>
                  {onion && (
                    <>
                      {(['prev', 'next'] as const).map((side) => (
                        <label key={side} className="fe-bg fe-bg--color" style={{ background: onionLook[side] }} title={t(`frames.onion.${side}`)}>
                          <input
                            type="color"
                            className="fe-bg-input"
                            value={onionLook[side]}
                            aria-label={t(`frames.onion.${side}`)}
                            onChange={(e) => lookOnion({ [side]: e.target.value })}
                          />
                          <span className="fe-bg-caret" style={{ color: inkOn(onionLook[side]) }} aria-hidden>
                            ▾
                          </span>
                        </label>
                      ))}
                      <label className="fe-onion-alpha" title={t('frames.onion.alphaHint')}>
                        <input
                          type="range"
                          min={5}
                          max={100}
                          step={5}
                          value={Math.round(onionLook.alpha * 100)}
                          aria-label={t('frames.onion.alpha')}
                          onChange={(e) => lookOnion({ alpha: Number(e.target.value) / 100 })}
                        />
                        <span className="fe-onion-value">{Math.round(onionLook.alpha * 100)}%</span>
                      </label>
                      <button
                        type="button"
                        className="fe-tool fe-onion-reset"
                        disabled={JSON.stringify(onionLook) === JSON.stringify(ONION_DEFAULT)}
                        title={t('frames.onion.resetHint')}
                        aria-label={t('frames.onion.resetHint')}
                        onClick={() => setOnionLook(ONION_DEFAULT)}
                      >
                        ↺
                      </button>
                    </>
                  )}
                </div>
                <button type="button" className={`fe-tool ${playing ? 'fe-tool--on' : ''}`} aria-pressed={playing} onClick={togglePlay}>
                  {t(playing ? 'frames.stop' : 'frames.play')}
                </button>
              </>
            )}
            <div className="fe-bar-end">
              <button type="button" className="fe-tool" disabled={!ed.canUndo} onClick={undo}>
                {t('frames.undo')}
              </button>
              <button type="button" className="fe-tool" disabled={!ed.canRedo} onClick={redo}>
                {t('frames.redo')}
              </button>
              <button type="button" className="btn btn--primary" disabled={!ed.dirty.length || busy || ed.cutting} onClick={() => void ed.save()}>
                {t(ed.saving ? 'frames.saving' : 'frames.save')}
              </button>
            </div>
          </div>
          {ed.error && (
            <p className="fe-error" role="alert">
              {ed.error}
            </p>
          )}

          <div className="fe-work">
            <aside className="fe-side fe-side--left">
              <div className="card fe-panel">
                <Seg
                  value={side}
                  options={['frames', 'layers'] as const}
                  label={(v) => (v === 'frames' ? t('frames.grid') : t('frames.layersCount', { n: layers.length }))}
                  onChange={setSide}
                />
                {side === 'frames' ? (
                  framesBody
                ) : (
                  <>
                    <div className="fe-panel-head">
                      <span className="fe-panel-note">{t('frames.layersNote')}</span>
                      <button type="button" className="fe-chip fe-chip--add" disabled={!newLayerFrom} title={t('frames.layer.addHint')} onClick={addLayer}>
                        {t('frames.layer.add')}
                      </button>
                    </div>
                    <ul className="fe-layers">{layers.map((l, i) => ({ l, i })).reverse().map(({ l, i }) => renderLayer(l, i))}</ul>
                  </>
                )}
              </div>
            </aside>

            <div className={`fe-center ${timed ? 'fe-center--timed' : ''}`}>
              <FrameStage
                mode={mode}
                hand={hand}
                bg={bg}
                items={items}
                carry={carry}
                blend={blend}
                flick={flick}
                guides={guides}
                onGuides={setGuides}
                onDrag={onDrag}
                turn={turnTool && mode === 'overlay' && !!active}
                pivot={pivot}
                onTurn={onTurn}
                onPivot={(x, y, tag) => ed.pivotActive(scope, x, y, tag)}
                area={mode === 'overlay' ? activeArea : null}
                onArea={ed.setArea}
                edgeLine={active?.kind === 'anim' ? { side: screenEdge.side, pos: edgeLinePos(screenEdge.side, screenEdge.at, active.fw, active.fh) } : null}
                onEdgeLine={(pos, tag) => active && ed.setScreenEdge(screenEdge.side, edgeLineAt(screenEdge.side, pos, active.fw, active.fh), tag)}
                sheet={sheetImg}
                sheetW={active?.sheetW ?? 0}
                sheetH={active?.sheetH ?? 0}
                xs={active?.cut.xs ?? null}
                ys={active?.cut.ys ?? null}
                cell={cutOf(active?.order ?? null, cell)}
                labels={sliceLabels}
                onLine={ed.dragLine}
                onPickCell={(k) => {
                  // Клетка листа → кадр анимации; удалённую не открыть
                  const i = active?.order ? active.order.indexOf(k) : k;
                  if (activeLayer && i >= 0) ed.openFrame(activeLayer.id, activeLayer.source, i);
                }}
              />
              {timed?.timing && (
                <FrameTimeline
                  key={timed.key}
                  source={timed}
                  timing={timed.timing}
                  cell={timed.key === active?.key ? cell : null}
                  always={alwaysLoops(triggersOf(timed.key))}
                  playing={playing && timed.key === active?.key}
                  onPlay={() => {
                    // Полоска другого листа — сперва на холст его первый кадр
                    if (timed.key === active?.key || !activeLayer) return togglePlay();
                    ed.openFrame(activeLayer.id, timed.key, 0);
                    played.current = 0;
                    frameAt.current = performance.now();
                    setLaps(0);
                    setPlaying(true);
                  }}
                  onPick={(i) => activeLayer && ed.openFrame(activeLayer.id, timed.key, i)}
                  onChange={(timing, tag) => ed.setTiming(timed.key, timing, tag)}
                  onMove={(from, to) => ed.moveCell(timed.key, from, to)}
                  onDelete={totalCells(timed) > 1 ? (i) => ed.deleteCell(timed.key, i) : null}
                />
              )}
              <p className="fe-hint">
                {ed.cutting
                  ? t('frames.cutting')
                  : ed.dirty.length
                    ? t('frames.unsaved', { names: dirtyNames })
                    : `${onionOn ? `${t('frames.hint.onion')} ` : ''}${t(mode === 'slice' ? 'frames.hint.slice' : turnTool && blend === 'normal' ? 'frames.hint.turn' : `frames.hint.${blend}`)}`}
              </p>
            </div>

            <aside className="fe-side fe-side--right">{mode === 'overlay' ? framePanel : slicePanel}</aside>
          </div>
        </>
      )}
    </div>
  );
}
