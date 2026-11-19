import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api } from '../../api';
import type { AnimTrigger, PortraitAnim } from '../animTypes';
import { setPortrait, usePortrait } from '../portraitStore';
import { PORTRAIT_SHEETS, sheetKey } from '../portraitTypes';
import { alignCell, analyzeAtlas, figureCenter, frameBox, frameDiff, placed, slotted } from './frameAnalysis';
import type { FrameData } from './frameAnalysis';
import {
  dropPerm,
  evenLines,
  insertSlot,
  layerCell,
  moveLine,
  movePerm,
  NO_ADJUST,
  nudge,
  permute,
  SLOTS_MAX,
  placePivot,
  regrid,
  scopeCells,
  stretchLines,
  totalAdjust,
  turn,
  wholeOnly,
  withBox,
  withCell,
  withScreenEdge,
  wrapCell,
} from './frameMath';
import type { FrameAdjust, FrameAlign, FrameArea, FrameAtlas, FrameCut, FrameFix, FrameKind, FrameLayer, FrameScope, FrameSourceData, FrameTiming, ScreenEdge } from './frameTypes';

/* Состояние редактора кадров: что загружено, слои, правки, история отмен.
   Правки живут здесь, пока не нажали «Сохранить»: нарезка пробуется на ядре
   без записи на диск, сдвиг, масштаб и поворот считает сам редактор.
   Несохранённое лежит в localStorage (черновик на персону) — смена персоны
   и перезагрузка страницы работу не теряют; лист успели поменять — черновик
   не подходит.

   Порядок кадров анимации (order): атлас нарезки — клетки по листу (base),
   а всё остальное — кадры по порядку анимации: data, номера кадров у слоёв,
   правки fix.cells и время holds. Переставили или удалили кадр — его правка
   и время едут с ним (permute). */

export interface FrameSource {
  key: string;
  kind: FrameKind;
  id: string;
  name: string;
  fps: number;
  maxGrid: number;
  frameMax: number;
  sheetW: number;
  sheetH: number;
  updatedAt: number | null;
  // Как нарезан атлас, что сейчас на экране
  rows: number;
  cols: number;
  fw: number;
  fh: number;
  cutCount: number; // сколько кадров у нарезки (у анимации — сколько играется на ядре)
  atlas: string;
  img: HTMLImageElement;
  base: FrameData; // клетки по листу
  atlasCut: string;
  // Что правим и что на ядре
  cut: FrameCut;
  fix: FrameFix;
  timing: FrameTiming | null;
  order: number[] | null;
  savedCut: string;
  savedFix: string;
  savedTiming: string;
  savedOrder: string;
  // Из этого (settle): кадры по порядку и сколько их играется
  data: FrameData;
  count: number;
}

export interface SourceOption {
  key: string;
  kind: FrameKind;
  id: string;
  name: string;
  triggers: AnimTrigger[]; // когда играет (у листов портрета пусто)
}

interface Edits {
  cut: FrameCut;
  fix: FrameFix;
  timing: FrameTiming | null;
  order: number[] | null;
}
type Snapshot = Record<string, Edits>;

interface Draft {
  layers?: FrameLayer[];
  edits?: Record<string, Edits & { at: number | null }>;
}

const DRAFT_PREFIX = 'vpc-frames:';
const HISTORY = 100;
const COALESCE_MS = 900; // правки одним движением — один шаг отмены
const CUT_DELAY_MS = 450;
const ATLAS_SLACK = 2; // px: редактор и ядро меряют фигуру с разной точностью

export const sourceKey = (kind: FrameKind, id: string) => `${kind}:${id}`;

function readDraft(personaId: string): Draft {
  try {
    const raw = localStorage.getItem(DRAFT_PREFIX + personaId);
    const d = raw ? (JSON.parse(raw) as Draft) : {};
    return d && typeof d === 'object' ? d : {};
  } catch {
    return {};
  }
}

function writeDraft(personaId: string, draft: Draft) {
  try {
    localStorage.setItem(DRAFT_PREFIX + personaId, JSON.stringify(draft));
  } catch {
    /* без памяти — черновик до перезагрузки */
  }
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('image'));
    img.src = url;
  });
}

async function atlasFields(d: FrameAtlas) {
  const img = await loadImage(d.atlas);
  return {
    rows: d.rows,
    cols: d.cols,
    fw: d.fw,
    fh: d.fh,
    cutCount: d.count,
    atlas: d.atlas,
    img,
    base: analyzeAtlas(img, d.rows, d.cols, d.fw, d.fh),
  };
}

/** Кадры по порядку и сколько играется — из нарезки, порядка и времени */
function settle(s: FrameSource): FrameSource {
  const slots = s.order?.length ?? s.rows * s.cols;
  const want = s.kind === 'anim' ? (s.timing?.frames ?? s.cutCount) : s.cutCount;
  const data = slotted(s.base, s.order);
  const count = Math.max(1, Math.min(slots, want));
  return data === s.data && count === s.count ? s : { ...s, data, count };
}

async function buildSource(d: FrameSourceData): Promise<FrameSource> {
  const fields = await atlasFields(d);
  const cut = JSON.stringify(d.cut);
  const order = d.order ?? null;
  // Атлас на ядре меньше, чем нужно правкам: сохранён при старом пределе и
  // обрезан. Редактор рисует кадры сам и этого не видно — предложить сохранить
  const box = frameBox(slotted(fields.base, order), d.fix, d.frame_max);
  const stale = d.saved_w != null && d.saved_h != null && (box.w > d.saved_w + ATLAS_SLACK || box.h > d.saved_h + ATLAS_SLACK);
  return settle({
    key: sourceKey(d.kind, d.id),
    kind: d.kind,
    id: d.id,
    name: d.name,
    fps: d.fps,
    maxGrid: d.max_grid,
    frameMax: d.frame_max,
    sheetW: d.sheet_w,
    sheetH: d.sheet_h,
    updatedAt: d.updated_at ?? null,
    ...fields,
    atlasCut: cut,
    cut: d.cut,
    fix: d.fix,
    timing: d.timing,
    order,
    savedCut: cut,
    savedFix: stale ? '' : JSON.stringify(d.fix),
    savedTiming: JSON.stringify(d.timing),
    savedOrder: JSON.stringify(order),
    data: fields.base,
    count: d.count,
  });
}

function snapshotOf(sources: Record<string, FrameSource>): Snapshot {
  const out: Snapshot = {};
  for (const s of Object.values(sources)) out[s.key] = { cut: s.cut, fix: s.fix, timing: s.timing, order: s.order };
  return out;
}

const animOption = (a: Pick<PortraitAnim, 'id' | 'name' | 'triggers'>): SourceOption => ({
  key: sourceKey('anim', a.id),
  kind: 'anim',
  id: a.id,
  name: a.name,
  triggers: a.triggers,
});

/** Сколько кадров в листе, считая те, что анимация не играет (без удалённых) */
export const totalCells = (s: Pick<FrameSource, 'rows' | 'cols' | 'order'>) => s.order?.length ?? s.rows * s.cols;

const isDirty = (s: FrameSource) =>
  JSON.stringify(s.cut) !== s.savedCut ||
  JSON.stringify(s.fix) !== s.savedFix ||
  JSON.stringify(s.timing) !== s.savedTiming ||
  JSON.stringify(s.order) !== s.savedOrder;

function newLayer(source: string, cell: number, linked: boolean, opacity: number): FrameLayer {
  return { id: `l${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`, source, cell, linked, opacity, color: null, visible: true };
}

function defaultLayers(options: SourceOption[]): FrameLayer[] {
  const sheets = options.filter((o) => o.kind === 'sheet');
  if (sheets.length >= 2) return [newLayer(sheets[0].key, 0, true, 1), newLayer(sheets[1].key, 0, true, 0.5)];
  const first = options[0];
  if (!first) return [];
  // Один лист — первый кадр стоит опорой, поверх него листаем остальные
  return [newLayer(first.key, 0, false, 1), newLayer(first.key, 0, true, 0.5)];
}

export function useFrameEditor(personaId: string, visible: boolean, onSaved: () => void, fallbackError: string) {
  const portrait = usePortrait(personaId);
  const [anims, setAnims] = useState<SourceOption[] | null>(null);
  const [sources, setSources] = useState<Record<string, FrameSource>>({});
  const sourcesRef = useRef(sources);
  const [layers, setLayers] = useState<FrameLayer[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [refId, setRefId] = useState<string | null>(null);
  const [browse, setBrowse] = useState<string | null>(null); // чьи кадры в сетке
  const [sheets, setSheets] = useState<Record<string, HTMLImageElement>>({});
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [aligning, setAligning] = useState<[number, number] | null>(null);
  const [cutting, setCutting] = useState(false);
  const [listed, setListed] = useState(false);

  const draft = useRef<Draft>(readDraft(personaId));
  const inited = useRef(false);
  const fetching = useRef(new Set<string>());
  const failedCut = useRef(new Map<string, string>());
  const cutTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const cutBusy = useRef(new Set<string>());
  const sheetUrls = useRef<string[]>([]);
  const past = useRef<Snapshot[]>([]);
  const future = useRef<Snapshot[]>([]);
  const lastEdit = useRef<{ tag: string; at: number } | null>(null);
  const [historyRev, setHistoryRev] = useState(0);

  const commit = useCallback((next: Record<string, FrameSource>) => {
    sourcesRef.current = next;
    setSources(next);
  }, []);

  const fail = useCallback(
    (e: unknown) => setError(e instanceof ApiError ? e.message : fallbackError),
    [fallbackError],
  );

  const options = useMemo<SourceOption[]>(() => {
    const out: SourceOption[] = [];
    for (const k of PORTRAIT_SHEETS) {
      if (portrait?.sheets[k]) out.push({ key: sourceKey('sheet', k), kind: 'sheet', id: k, name: k, triggers: [] });
    }
    // Листы эмоций: ключ на ядре «id.лист», подпись — имя эмоции
    for (const e of portrait?.emotions ?? []) {
      for (const k of PORTRAIT_SHEETS) {
        const id = sheetKey(k, e.id);
        if (e.sheets[k]) out.push({ key: sourceKey('sheet', id), kind: 'sheet', id, name: `${e.name} · ${k}`, triggers: [] });
      }
    }
    return [...out, ...(anims ?? [])];
  }, [portrait, anims]);

  // ── Загрузка ───────────────────────────────────────────────────────

  const loadSource = useCallback(
    async (key: string) => {
      if (fetching.current.has(key)) return;
      fetching.current.add(key);
      try {
        const [kind, id] = key.split(':') as [FrameKind, string];
        let src = await buildSource(await api.getFrames(personaId, kind, id));
        const saved = draft.current.edits?.[key];
        if (saved && saved.at === src.updatedAt) {
          src = settle({
            ...src,
            cut: saved.cut,
            fix: saved.fix,
            ...(saved.timing ? { timing: saved.timing } : {}), // черновик старой версии без времени
            ...(saved.order !== undefined ? { order: saved.order } : {}), // и без порядка кадров
          });
        }
        commit({ ...sourcesRef.current, [key]: src });
      } catch (e) {
        fail(e);
      } finally {
        fetching.current.delete(key);
      }
    },
    [personaId, commit, fail],
  );

  // Вкладка открылась: список анимаций и то, что поменяли в других вкладках
  useEffect(() => {
    if (!visible) return;
    let alive = true;
    api
      .getPortraitAnims(personaId, false)
      .then((d) => {
        if (!alive) return;
        setAnims(d.anims.map(animOption));
        setListed(true);
        for (const a of d.anims) {
          const s = sourcesRef.current[sourceKey('anim', a.id)];
          if (s && !isDirty(s) && s.updatedAt !== (a.updated_at ?? null)) void loadSource(s.key);
        }
      })
      .catch((e) => {
        if (alive) fail(e);
      });
    return () => {
      alive = false;
    };
  }, [visible, personaId, loadSource, fail]);

  useEffect(() => {
    if (!visible || !portrait) return;
    for (const s of Object.values(sourcesRef.current)) {
      if (s.kind === 'sheet' && !isDirty(s) && s.updatedAt !== (portrait.updated_at ?? null)) void loadSource(s.key);
    }
  }, [visible, portrait, loadSource]);

  // Слои: восстановить или сложить первые; источник удалили — слой долой
  useEffect(() => {
    if (!listed || !portrait || !visible) return;
    if (!inited.current) {
      if (!options.length) return;
      inited.current = true;
      const known = new Set(options.map((o) => o.key));
      const kept = (draft.current.layers ?? []).filter((l) => known.has(l.source) && typeof l.cell === 'number');
      const list = kept.length ? kept : defaultLayers(options);
      setLayers(list);
      setActiveId(list[list.length - 1]?.id ?? null);
      setRefId(list[0]?.id ?? null);
      return;
    }
    const known = new Set(options.map((o) => o.key));
    setLayers((cur) => (cur.every((l) => known.has(l.source)) ? cur : cur.filter((l) => known.has(l.source))));
    // Удалённый лист нечего и сохранять
    const gone = Object.keys(sourcesRef.current).filter((k) => !known.has(k));
    if (gone.length) {
      const next = { ...sourcesRef.current };
      gone.forEach((k) => delete next[k]);
      commit(next);
    }
  }, [options, listed, portrait, visible, commit]);

  useEffect(() => {
    for (const key of [...layers.map((l) => l.source), browse]) {
      if (key && !sourcesRef.current[key]) void loadSource(key);
    }
  }, [layers, browse, loadSource]);

  const ensureSheet = useCallback(
    async (key: string) => {
      if (sheets[key] || fetching.current.has(`sheet!${key}`)) return;
      fetching.current.add(`sheet!${key}`);
      try {
        const [kind, id] = key.split(':') as [FrameKind, string];
        const url = URL.createObjectURL(await api.getFrameSheet(personaId, kind, id));
        sheetUrls.current.push(url);
        const img = await loadImage(url);
        setSheets((cur) => ({ ...cur, [key]: img }));
      } catch (e) {
        fail(e);
      } finally {
        fetching.current.delete(`sheet!${key}`);
      }
    },
    [personaId, sheets, fail],
  );

  useEffect(() => {
    const timers = cutTimers.current;
    const urls = sheetUrls.current;
    return () => {
      timers.forEach((t) => clearTimeout(t));
      urls.forEach((u) => URL.revokeObjectURL(u));
    };
  }, []);

  // Черновик: слои и несохранённое
  useEffect(() => {
    if (!inited.current) return;
    const t = setTimeout(() => {
      const edits: NonNullable<Draft['edits']> = {};
      for (const s of Object.values(sources)) {
        if (isDirty(s)) edits[s.key] = { cut: s.cut, fix: s.fix, timing: s.timing, order: s.order, at: s.updatedAt };
      }
      draft.current = { layers, edits };
      writeDraft(personaId, draft.current);
    }, 300);
    return () => clearTimeout(t);
  }, [layers, sources, personaId]);

  // Нарезка сменилась — атлас заново, пока только на пробу
  useEffect(() => {
    for (const s of Object.values(sources)) {
      const wanted = JSON.stringify(s.cut);
      if (wanted === s.atlasCut || failedCut.current.get(s.key) === wanted || cutBusy.current.has(s.key)) continue;
      const old = cutTimers.current.get(s.key);
      if (old) clearTimeout(old);
      cutTimers.current.set(
        s.key,
        setTimeout(() => {
          cutTimers.current.delete(s.key);
          cutBusy.current.add(s.key);
          setCutting(true);
          api
            .cutFrames(personaId, s.kind, s.id, s.cut)
            .then(atlasFields)
            .then((fields) => {
              const cur = sourcesRef.current[s.key];
              if (cur) commit({ ...sourcesRef.current, [s.key]: settle({ ...cur, ...fields, atlasCut: wanted }) });
              setError('');
            })
            .catch((e) => {
              failedCut.current.set(s.key, wanted);
              fail(e);
            })
            .finally(() => {
              cutBusy.current.delete(s.key);
              setCutting(cutBusy.current.size > 0);
              // Пока резали, нарезку могли поменять ещё раз
              setSources((cur) => ({ ...cur }));
            });
        }, CUT_DELAY_MS),
      );
    }
  }, [sources, personaId, commit, fail]);

  // ── Правки и отмена ────────────────────────────────────────────────

  const edit = useCallback(
    (keys: string[], change: (s: FrameSource) => Partial<Edits>, tag?: string) => {
      const now = Date.now();
      const glued = tag && lastEdit.current?.tag === tag && now - lastEdit.current.at < COALESCE_MS;
      if (!glued) {
        past.current = [...past.current.slice(1 - HISTORY), snapshotOf(sourcesRef.current)];
        future.current = [];
        setHistoryRev((v) => v + 1);
      }
      lastEdit.current = tag ? { tag, at: now } : null;
      const next = { ...sourcesRef.current };
      for (const key of keys) {
        const s = next[key];
        if (s) next[key] = settle({ ...s, ...change(s) });
      }
      failedCut.current.clear();
      commit(next);
    },
    [commit],
  );

  const restore = useCallback(
    (snap: Snapshot) => {
      const next = { ...sourcesRef.current };
      for (const key of Object.keys(next)) {
        const e = snap[key];
        if (e) next[key] = settle({ ...next[key], cut: e.cut, fix: e.fix, timing: e.timing, order: e.order });
      }
      failedCut.current.clear();
      commit(next);
    },
    [commit],
  );

  const undo = useCallback(() => {
    const snap = past.current[past.current.length - 1];
    if (!snap) return;
    past.current = past.current.slice(0, -1);
    future.current = [...future.current, snapshotOf(sourcesRef.current)];
    lastEdit.current = null;
    restore(snap);
    setHistoryRev((v) => v + 1);
  }, [restore]);

  const redo = useCallback(() => {
    const snap = future.current[future.current.length - 1];
    if (!snap) return;
    future.current = future.current.slice(0, -1);
    past.current = [...past.current, snapshotOf(sourcesRef.current)];
    lastEdit.current = null;
    restore(snap);
    setHistoryRev((v) => v + 1);
  }, [restore]);

  // ── Слои ───────────────────────────────────────────────────────────

  const activeLayer = layers.find((l) => l.id === activeId) ?? layers[layers.length - 1] ?? null;
  const active = activeLayer ? (sources[activeLayer.source] ?? null) : null;
  const refLayer = layers.find((l) => l.id === refId) ?? layers[0] ?? null;
  const ref = refLayer ? (sources[refLayer.source] ?? null) : null;
  const cell = activeLayer && active ? layerCell(activeLayer, totalCells(active)) : 0;
  const browseKey = browse ?? activeLayer?.source ?? null;

  const patchLayer = useCallback((id: string, patch: Partial<FrameLayer>) => {
    setLayers((cur) => cur.map((l) => (l.id === id ? { ...l, ...patch } : l)));
  }, []);

  const selectLayer = useCallback(
    (id: string) => {
      setActiveId(id);
      const layer = layers.find((l) => l.id === id);
      if (layer) setBrowse(layer.source);
    },
    [layers],
  );

  // Кадр новым слоем сверху. Тот же лист уже лежит — новый стоит на своём
  // кадре: с ним и сравнивают; другой лист листается вместе с остальными
  const addFrame = useCallback(
    (key: string, i: number) => {
      const layer = newLayer(key, i, !layers.some((l) => l.source === key), 0.5);
      setLayers([...layers, layer]);
      setActiveId(layer.id);
      setBrowse(key);
    },
    [layers],
  );

  const removeLayer = useCallback((id: string) => {
    setLayers((cur) => cur.filter((l) => l.id !== id));
  }, []);

  const moveLayer = useCallback((id: string, by: number) => {
    setLayers((cur) => {
      const i = cur.findIndex((l) => l.id === id);
      const j = i + by;
      if (i < 0 || j < 0 || j >= cur.length) return cur;
      const next = [...cur];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  }, []);

  // Только этот лист: два первых кадра друг на друге, первый — опорный
  const soloSource = useCallback((key: string) => {
    const count = sourcesRef.current[key]?.count ?? 1;
    const base = newLayer(key, 0, false, 1);
    const top = newLayer(key, Math.min(1, count - 1), true, 0.5);
    setLayers([base, top]);
    setActiveId(top.id);
    setRefId(base.id);
    setBrowse(key);
  }, []);

  // Лист загрузили здесь же: сразу показать его кадры
  const animAdded = useCallback(
    (a: PortraitAnim) => {
      setAnims((cur) => [...(cur ?? []), animOption(a)]);
      setBrowse(sourceKey('anim', a.id));
      onSaved();
    },
    [onSaved],
  );

  // Кадр i листа key в слое id. Слои «Вместе» сдвигаются на столько же, но
  // при выборе кадра (walk — false) слои того же листа стоят на месте: номер
  // на другом листе — тот же ракурс, а на своём выбрали именно другой кадр,
  // иначе A 1 и A 2 в разных слоях не сложить. Клавиши и «Проиграть» (walk)
  // ведут все, разница кадров держится
  const moveFrame = useCallback((id: string, key: string, i: number, walk: boolean) => {
    setLayers((cur) => {
      const me = cur.find((l) => l.id === id);
      if (!me) return cur;
      const by = i - me.cell;
      return cur.map((l) => {
        if (l.id === id) return { ...l, source: key, cell: i };
        if (!me.linked || !l.linked || (!walk && l.source === key)) return l;
        return { ...l, cell: wrapCell(l.cell + by, sourcesRef.current[l.source]?.count ?? 1) };
      });
    });
  }, []);

  const openFrame = useCallback((id: string, key: string, i: number) => moveFrame(id, key, i, false), [moveFrame]);

  // id — стрелка у строки слоя, как выбор кадра; без него — клавиши и проигрывание
  const stepFrame = useCallback(
    (by: number, id?: string) => {
      const layer = id ? layers.find((l) => l.id === id) : activeLayer;
      const s = layer ? sourcesRef.current[layer.source] : null;
      if (layer && s) moveFrame(layer.id, layer.source, wrapCell(layerCell(layer, totalCells(s)) + by, s.count), !id);
    },
    [layers, activeLayer, moveFrame],
  );

  // ── Сдвиг и масштаб ────────────────────────────────────────────────

  const nudgeActive = useCallback(
    (scope: FrameScope, ddx: number, ddy: number, ds: number, tag?: string) => {
      if (!active) return;
      edit([active.key], (s) => ({ fix: nudge(s.fix, scope, cell, totalCells(s), s.cols, ddx, ddy, ds) }), tag);
    },
    [active, cell, edit],
  );

  // Поворот на dr градусов; кадр без центра поворота — вокруг середины фигуры
  const turnActive = useCallback(
    (scope: FrameScope, dr: number, tag?: string) => {
      if (!active) return;
      edit([active.key], (s) => ({ fix: turn(s.fix, scope, cell, totalCells(s), s.cols, dr, (i) => figureCenter(s.data, i)) }), tag);
    },
    [active, cell, edit],
  );

  // Центр поворота — в точку холста (px кадра от низа-середины)
  const pivotActive = useCallback(
    (scope: FrameScope, x: number, y: number, tag?: string) => {
      if (!active) return;
      edit([active.key], (s) => ({ fix: placePivot(s.fix, scope, cell, totalCells(s), s.cols, x, y) }), tag);
    },
    [active, cell, edit],
  );

  // Своя рамка кадра активной анимации; null — подбирать самой
  const setArea = useCallback(
    (box: FrameArea | null, tag?: string) => {
      if (active?.kind === 'anim') edit([active.key], (s) => ({ fix: withBox(s.fix, box) }), tag);
    },
    [active, edit],
  );

  // Край экрана активной анимации: низ (линия выше низа фигуры) или другой
  // (линия от края кадра) — frameMath.withScreenEdge; at — не дальше кадра
  const setScreenEdge = useCallback(
    (side: ScreenEdge, at: number, tag?: string) => {
      if (active?.kind !== 'anim') return;
      edit([active.key], (s) => ({ fix: withScreenEdge(s.fix, side, Math.min(at, side === 'left' || side === 'right' ? s.fw : s.fh)) }), tag);
    },
    [active, edit],
  );

  const setActiveFix = useCallback(
    (fix: FrameFix, tag?: string) => {
      if (active) edit([active.key], () => ({ fix }), tag);
    },
    [active, edit],
  );

  const resetFix = useCallback(
    (scope: FrameScope) => {
      if (!active) return;
      edit([active.key], (s) => {
        // Своя рамка и низ экрана — не правка кадров: у них свои переключатели
        if (scope === 'sheet')
          return { fix: { ...(s.fix.box ? { box: s.fix.box } : {}), ...(s.fix.floor ? { floor: s.fix.floor } : {}), ...(s.fix.edge ? { edge: s.fix.edge } : {}) } };
        return { fix: scopeCells(scope, cell, totalCells(s), s.cols).reduce((f, i) => withCell(f, i, NO_ADJUST), s.fix) };
      });
    },
    [active, cell, edit],
  );

  // На что равняться в кадре i активного листа
  const refFor = useCallback(
    (i: number): { data: FrameData; cell: number; adjust: FrameAdjust } | null => {
      if (!ref || !refLayer || !activeLayer || refLayer.id === activeLayer.id) return null;
      // Другой лист «Вместе» — кадр i идёт со своей парой; свой лист стоит
      // на месте: выбор кадра его не двигает (moveFrame)
      const own = layerCell(refLayer, totalCells(ref));
      const paired = refLayer.linked && activeLayer.linked && refLayer.source !== activeLayer.source;
      const c = paired ? wrapCell(own + i - cell, ref.count) : own;
      return { data: ref.data, cell: c, adjust: totalAdjust(ref.fix, c) };
    },
    [ref, refLayer, activeLayer, cell],
  );

  const align = useCallback(
    async (how: FrameAlign, scope: FrameScope, fitScale: boolean) => {
      if (!active) return;
      const cells = scopeCells(scope, cell, totalCells(active), active.cols);
      setAligning([0, cells.length]);
      let fix = sourcesRef.current[active.key]?.fix ?? active.fix;
      for (let n = 0; n < cells.length; n++) {
        const i = cells[n];
        const target = refFor(i);
        if (target) {
          const res = alignCell(how, active.data, fix, i, target.data, target.cell, target.adjust, fitScale);
          if (res) fix = withCell(fix, i, res);
        }
        setAligning([n + 1, cells.length]);
        // Отдать кадр интерфейсу: подгонка листа 12×12 заметна
        await new Promise((r) => setTimeout(r));
      }
      setAligning(null);
      setActiveFix(fix);
    },
    [active, cell, refFor, setActiveFix],
  );

  // ── Время кадров ───────────────────────────────────────────────────

  const setTiming = useCallback(
    (key: string, timing: FrameTiming, tag?: string) => {
      // Без изменений — без шага отмены: тянули первый кадр или упёрлись в соседа
      if (JSON.stringify(sourcesRef.current[key]?.timing) !== JSON.stringify(timing)) edit([key], () => ({ timing }), tag);
    },
    [edit],
  );

  // ── Порядок кадров анимации ────────────────────────────────────────

  // perm — какой прежний кадр теперь на месте n; frames — сколько играть.
  // Слои этого листа остаются на тех же картинках
  const reorder = useCallback(
    (key: string, perm: number[], frames: number) => {
      const src = sourcesRef.current[key];
      if (!src || src.kind !== 'anim' || !perm.length) return;
      const cells = src.rows * src.cols;
      edit([key], (s) => {
        const r = permute(s.order, s.fix, s.timing?.holds ?? null, perm, cells);
        return { order: r.order, fix: r.fix, timing: s.timing && { ...s.timing, holds: r.holds, frames } };
      });
      setLayers((cur) =>
        cur.map((l) => {
          if (l.source !== key) return l;
          const at = perm.indexOf(l.cell);
          return { ...l, cell: at >= 0 ? at : Math.max(0, Math.min(l.cell, perm.length - 1)) };
        }),
      );
    },
    [edit],
  );

  // Кадр from — перед кадром to (to — сколько кадров: в конец)
  const moveCell = useCallback(
    (key: string, from: number, to: number) => {
      const s = sourcesRef.current[key];
      const perm = s ? movePerm(totalCells(s), from, to) : null;
      if (s && perm) reorder(key, perm, s.count);
    },
    [reorder],
  );

  const deleteCell = useCallback(
    (key: string, j: number) => {
      const s = sourcesRef.current[key];
      if (!s || totalCells(s) < 2) return;
      reorder(key, dropPerm(totalCells(s), j), Math.max(1, s.count - (j < s.count ? 1 : 0)));
    },
    [reorder],
  );

  // Клетку листа cut — ещё одним кадром: сразу за последним играемым, и он
  // тоже играется (1‑2‑3 → 1‑2‑3‑2). Слои с кадрами дальше — за своими
  // картинками. Возвращает номер нового кадра (не вышло — null)
  const addFromSheet = useCallback(
    (key: string, cut: number): number | null => {
      const src = sourcesRef.current[key];
      if (!src || src.kind !== 'anim' || totalCells(src) >= SLOTS_MAX) return null;
      const at = src.count;
      edit([key], (s) => {
        const r = insertSlot(s.order, s.fix, s.timing?.holds ?? null, s.rows * s.cols, cut, at);
        return { order: r.order, fix: r.fix, timing: s.timing && { ...s.timing, holds: r.holds, frames: s.count + 1 } };
      });
      setLayers((cur) => cur.map((l) => (l.source === key && l.cell >= at ? { ...l, cell: l.cell + 1 } : l)));
      return at;
    },
    [edit],
  );

  // Все клетки листа снова по порядку; правки и время — у своих клеток,
  // вернувшиеся кадры играются
  const restoreOrder = useCallback(
    (key: string) => {
      const src = sourcesRef.current[key];
      if (!src?.order) return;
      const cells = src.rows * src.cols;
      edit([key], (s) => {
        const old = s.order ?? [];
        const fix: FrameFix = s.fix.all ? { all: s.fix.all } : {};
        if (s.fix.box) fix.box = s.fix.box;
        if (s.fix.floor) fix.floor = s.fix.floor;
        if (s.fix.edge) fix.edge = s.fix.edge;
        const moved: Record<string, FrameAdjust> = {};
        const holds: number[] = [];
        for (let n = 0; n < cells; n++) {
          const j = old.indexOf(n);
          const a = j >= 0 ? s.fix.cells?.[j] : undefined;
          if (a) moved[n] = a;
          holds.push(j >= 0 ? (s.timing?.holds?.[j] ?? 1) : 1);
        }
        if (Object.keys(moved).length) fix.cells = moved;
        const frames = Math.min(cells, s.count + cells - old.length);
        return { order: null, fix, timing: s.timing && { ...s.timing, holds: holds.some((v) => v !== 1) ? holds : null, frames } };
      });
      setLayers((cur) => cur.map((l) => (l.source === key ? { ...l, cell: src.order?.[l.cell] ?? l.cell } : l)));
    },
    [edit],
  );

  // ── Нарезка ────────────────────────────────────────────────────────

  const setGrid = useCallback(
    (rows: number, cols: number) => {
      if (!active) return;
      // Сетка портрета общая на все листы
      const keys =
        active.kind === 'sheet'
          ? Object.values(sourcesRef.current)
              .filter((s) => s.kind === 'sheet')
              .map((s) => s.key)
          : [active.key];
      edit(keys, (s) => ({
        cut: regrid(s.cut, rows, cols, s.sheetW, s.sheetH),
        fix: wholeOnly(s.fix),
        // Номера кадров теперь про другое; играть — сколько займут клеток
        timing: s.timing && { ...s.timing, holds: null, frames: null },
        order: null,
      }));
    },
    [active, edit],
  );

  const setCutField = useCallback(
    (patch: Partial<FrameCut>) => {
      if (active) edit([active.key], (s) => ({ cut: { ...s.cut, ...patch } }), `cut:${Object.keys(patch).join()}`);
    },
    [active, edit],
  );

  const dragLine = useCallback(
    (axis: 'x' | 'y', i: number, value: number, tag: string) => {
      if (!active) return;
      edit(
        [active.key],
        (s) => {
          const size = axis === 'x' ? s.sheetW : s.sheetH;
          const key = axis === 'x' ? 'xs' : 'ys';
          const lines = s.cut[key];
          return lines ? { cut: { ...s.cut, [key]: moveLine(lines, i, value, size) } } : {};
        },
        tag,
      );
    },
    [active, edit],
  );

  const setMargin = useCallback(
    (side: 'left' | 'right' | 'top' | 'bottom', value: number) => {
      if (!active) return;
      edit(
        [active.key],
        (s) => {
          const horizontal = side === 'left' || side === 'right';
          const key = horizontal ? 'xs' : 'ys';
          const size = horizontal ? s.sheetW : s.sheetH;
          const lines = s.cut[key];
          if (!lines) return {};
          const from = side === 'left' || side === 'top' ? value : lines[0];
          const to = side === 'right' || side === 'bottom' ? size - value : lines[lines.length - 1];
          if (from < 0 || to > size || to - from < (lines.length - 1) * 8) return {};
          return { cut: { ...s.cut, [key]: stretchLines(lines, from, to) } };
        },
        `margin:${side}`,
      );
    },
    [active, edit],
  );

  const evenOut = useCallback(() => {
    if (!active) return;
    edit([active.key], (s) => {
      const { xs, ys, rows, cols } = s.cut;
      if (!xs || !ys) return {};
      return { cut: { ...s.cut, xs: evenLines(xs[0], xs[cols], cols), ys: evenLines(ys[0], ys[rows], rows) } };
    });
  }, [active, edit]);

  // ── Сохранение ─────────────────────────────────────────────────────

  const dirty = useMemo(() => Object.values(sources).filter(isDirty), [sources]);

  const save = useCallback(async () => {
    setSaving(true);
    setError('');
    try {
      const list = Object.values(sourcesRef.current).filter(isDirty);
      let gridChanged = false;
      let sheetSaved = false;
      for (const s of list) {
        const prev = JSON.parse(s.savedCut) as FrameCut;
        if (s.kind === 'sheet') {
          sheetSaved = true;
          if (prev.rows !== s.cut.rows || prev.cols !== s.cut.cols) gridChanged = true;
        }
        const next = await buildSource(
          await api.putFrames(personaId, s.kind, s.id, { ...s.cut, fix: s.fix, timing: s.timing, ...(s.kind === 'anim' ? { order: s.order } : {}) }),
        );
        commit({ ...sourcesRef.current, [s.key]: next });
      }
      // Другая сетка резала и остальные листы
      if (gridChanged) {
        for (const s of Object.values(sourcesRef.current)) {
          if (s.kind === 'sheet' && !isDirty(s)) await loadSource(s.key);
        }
      }
      if (sheetSaved) {
        const fresh = await api.getPortrait(personaId);
        // У всех листов портрета одно время правки: сверять больше не с чем
        const next = { ...sourcesRef.current };
        for (const s of Object.values(next)) {
          if (s.kind === 'sheet') next[s.key] = { ...s, updatedAt: fresh.updated_at ?? null };
        }
        commit(next);
        setPortrait(personaId, fresh);
      }
      onSaved();
    } catch (e) {
      fail(e);
    } finally {
      setSaving(false);
    }
  }, [personaId, commit, fail, loadSource, onSaved]);

  // ── Что видно на панелях ───────────────────────────────────────────

  // Насколько кадры активного листа отходят от опорного: px вбок и вверх-вниз
  const deviations = useMemo(() => {
    const out = new Map<number, { dx: number; dy: number }>();
    if (!active) return out;
    for (let i = 0; i < totalCells(active); i++) {
      const target = refFor(i);
      if (!target) break;
      const a = placed(active.data, i, totalAdjust(active.fix, i));
      const b = placed(target.data, target.cell, target.adjust);
      if (a && b) out.set(i, { dx: Math.round(a.center - b.center), dy: Math.round(a.bottom - b.bottom) });
    }
    return out;
  }, [active, refFor]);

  const diffPercent = useMemo(() => {
    if (!active) return null;
    const target = refFor(cell);
    if (!target) return null;
    return frameDiff(active.data, cell, totalAdjust(active.fix, cell), target.data, target.cell, target.adjust);
  }, [active, cell, refFor]);

  return {
    ready: listed && !!portrait,
    options,
    sources,
    layers,
    activeLayer,
    active,
    refLayer,
    cell,
    browseKey,
    setBrowse,
    soloSource,
    animAdded,
    selectLayer,
    setRefId,
    patchLayer,
    addFrame,
    removeLayer,
    moveLayer,
    openFrame,
    stepFrame,
    nudgeActive,
    turnActive,
    pivotActive,
    setActiveFix,
    setArea,
    setScreenEdge,
    moveCell,
    deleteCell,
    addFromSheet,
    restoreOrder,
    resetFix,
    setTiming,
    align,
    aligning,
    setGrid,
    setCutField,
    setMargin,
    dragLine,
    evenOut,
    sheets,
    ensureSheet,
    cutting,
    error,
    dirty,
    save,
    saving,
    undo,
    redo,
    canUndo: past.current.length > 0,
    canRedo: future.current.length > 0,
    historyRev,
    deviations,
    diffPercent,
  };
}
