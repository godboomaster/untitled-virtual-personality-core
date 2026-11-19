import { useCallback, useEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';

/* Движение живой головы — общее для портрета из листов (LivePortrait) и
   аватара персоны без своей картинки (PersonaAvatar, голова из «Комнаты»).
   Движок по мотивам tomari-guruguru (app.jsx, talk-app.jsx; © 2026 rotejin,
   MIT): взгляд — точка от −1 до 1 по каждой оси, сглаженная; моргание — с
   живым разбросом (иногда двойное, иногда медленное).

   «Пилот» — сама персона, а не человек перед камерой (useGaze):
   - водите мышью или касаетесь экрана — смотрит туда (FOLLOW_MS после
     последнего движения); пока говорит или занята разговором (attentive:
     на рабочем столе — пока висит облачко) — нет, смотрит на собеседника;
     followWithin — только пока курсор над ближайшим предком по селектору
     (аватары в списке чатов не смотрят на курсор в другом конце экрана);
   - иначе смотрит на собеседника, изредка отводя взгляд (calm — реже: в
     списке аватаров много); думает над ответом (thinking) — взгляд вверх и
     в сторону; слушает — прямо;
   - услышала реплику (nod сменился) — коротко кивает: голова вниз и
     обратно, потом думает;
   - читает сообщение пользователя в облачке рядом (reading — середина
     облачка в тех же координатах взгляда) — голова к облачку, взгляд
     строками: несколько скачков слева направо, возврат к началу следующей
     строки, строка за строкой чуть ниже; курсор и думание ждут;
   - её гладят по голове или она спит — голова чуть опущена.
   Рот (useMouth) — слогами: открыт / приоткрыт / закрыт, с паузами между
   «словами». Синтез речи (браузерный и Android) звук наружу не отдаёт,
   поэтому рот — по ритму речи, а не по громкости.

   Энергия: цикл кадров крутится, только пока взгляд в пути. Дошёл до цели —
   стоит до движения курсора, смены режима или таймера следующего взгляда в
   сторону, поэтому неподвижная голова кадров не тратит.

   Курсор, кадры и таймеры — того окна, где голова показана (viewOf): в
   PiP-окне («Вынести портрет») она смотрит на курсор над этим окном и не
   замирает, когда вкладка приложения ушла в фон. */

export type Mouth = 0 | 1 | 2; // закрыт, приоткрыт, открыт

export interface GazeCues {
  speaking?: boolean;
  listening?: boolean;
  thinking?: boolean;
  petted?: boolean;
  asleep?: boolean;
  /** Занята разговором (не только пока рот ходит) — за курсором не следит */
  attentive?: boolean;
  /** Сменился — кивнуть (услышала реплику) */
  nod?: number;
  /** Читает облачко: его середина в координатах взгляда (−1…1, x — вправо,
   *  y — вниз, как у курсора); null — не читает */
  reading?: { x: number; y: number } | null;
}

export interface GazeOptions {
  /** Смотреть на курсор, только пока он над ближайшим предком по селектору */
  followWithin?: string;
  /** Отводить взгляд реже */
  calm?: boolean;
}

const FOLLOW_MS = 3000;
// Кивок: голова вниз (на ряд и при сетке 3×3) на столько, потом обратно
const NOD_MS = 260;
const NOD_Y = 0.5;
const SMOOTH = 0.22; // доля пути к цели за кадр при 60 Гц
// Чтение облачка: строка — столько, скачков взгляда в строке; ширина и
// высота «страницы» вокруг середины облачка (в координатах взгляда);
// строк до возврата наверх
const READ_LINE_MS = 1100;
const READ_STEPS = 3;
const READ_HALF_W = 0.25;
const READ_HALF_H = 0.18;
const READ_LINES = 3;
// Ближе к цели — уже на месте: цикл кадров останавливается
const SETTLED = 0.01;
// Курсор у края области followWithin ещё считается над ней
const WITHIN_MARGIN = 24;

export const rand = (a: number, b: number) => a + Math.random() * (b - a);
// Окно, в документе которого лежит элемент (главное или PiP)
export const viewOf = (el: Element | null): Window => el?.ownerDocument.defaultView ?? window;
export const clamp = (v: number, a: number, b: number) => Math.min(b, Math.max(a, v));

/** Взгляд: onMove(x, y) — на каждом кадре, пока голова поворачивается.
 *  Возвращает kick — разбудить остановленный цикл кадров. */
export function useGaze(
  elRef: RefObject<Element | null>,
  cues: GazeCues,
  onMove: (x: number, y: number) => void,
  { followWithin, calm = false }: GazeOptions = {},
): () => void {
  const {
    speaking = false,
    listening = false,
    thinking = false,
    petted = false,
    asleep = false,
    attentive = false,
    nod = 0,
    reading = null,
  } = cues;
  // Режим взгляда — в ref: цикл кадров читает свежий, не перезапускаясь
  const mode = useRef({ speaking, listening, thinking, petted, asleep, attentive, reading });
  mode.current = { speaking, listening, thinking, petted, asleep, attentive, reading };
  const moveCb = useRef(onMove);
  moveCb.current = onMove;
  // Разбудить остановленный цикл кадров (режим сменился)
  const kick = useRef<() => void>(() => {});
  // Кивает до этого времени (часы окна головы)
  const nodUntil = useRef(0);
  // С какого времени читает (часы окна головы): с него строки и скачки
  const readFrom = useRef(0);

  useEffect(() => {
    const view = viewOf(elRef.current);
    const pointer = { x: 0, y: 0, at: -Infinity, within: !followWithin };
    const cur = { x: 0, y: 0 };
    // Отвести взгляд: куда и до какого времени; следующий — когда
    const nextGlanceIn = (talk: boolean) => (calm ? rand(8000, 20000) : talk ? rand(2000, 4500) : rand(3000, 7000));
    let glance = { x: 0, y: 0, until: 0 };
    let nextGlance = view.performance.now() + (calm ? rand(3000, 15000) : rand(2500, 5000));
    let prev = view.performance.now();
    let raf = 0;
    let wakeTimer = 0;
    let running = false;

    const start = () => {
      view.clearTimeout(wakeTimer);
      wakeTimer = 0;
      if (running) return;
      running = true;
      prev = view.performance.now();
      raf = view.requestAnimationFrame(tick);
    };
    kick.current = start;

    const onPointer = (e: PointerEvent) => {
      const el = elRef.current;
      if (!el) return;
      if (followWithin) {
        const scope = el.closest(followWithin)?.getBoundingClientRect();
        const within =
          !!scope &&
          e.clientX > scope.left - WITHIN_MARGIN &&
          e.clientX < scope.right + WITHIN_MARGIN &&
          e.clientY > scope.top - WITHIN_MARGIN &&
          e.clientY < scope.bottom + WITHIN_MARGIN;
        // Курсор ходит вдали — голове до него дела нет, кадры не нужны
        if (!within && !pointer.within) return;
        pointer.within = within;
      }
      const rect = el.getBoundingClientRect();
      const range = Math.max(260, rect.width * 1.1);
      pointer.x = clamp((e.clientX - (rect.left + rect.width / 2)) / range, -1, 1);
      pointer.y = clamp((e.clientY - (rect.top + rect.height * 0.45)) / range, -1, 1);
      pointer.at = view.performance.now();
      start();
    };

    function tick(now: number) {
      const { speaking: talk, listening: hear, thinking: think, petted: pet, asleep: sleep, attentive: busy, reading: read } =
        mode.current;
      let tx = 0;
      let ty = 0;
      // Когда цель сменится сама (кончится слежение за курсором, взгляд в
      // сторону): к этому времени остановленный цикл надо разбудить
      let wakeAt = Infinity;
      if (pet || sleep) {
        // Гладят или спит: голова чуть вниз, под руку
        ty = 0.35;
      } else if (now < nodUntil.current) {
        // Кивок: вниз, на собеседника
        ty = NOD_Y;
        wakeAt = nodUntil.current;
      } else if (read && !talk) {
        // Читает: строка — READ_STEPS скачков слева направо по облачку,
        // потом к началу следующей, на строку ниже; после READ_LINES — снова
        // сверху. Цель меняется на границе скачка — к ней и будим
        const step = READ_LINE_MS / READ_STEPS;
        const n = Math.floor(Math.max(0, now - readFrom.current) / step);
        const col = n % READ_STEPS;
        const line = Math.floor(n / READ_STEPS) % READ_LINES;
        const cx = clamp(read.x, -1 + READ_HALF_W, 1 - READ_HALF_W);
        const cy = clamp(read.y, -1 + READ_HALF_H, 1 - READ_HALF_H);
        tx = cx - READ_HALF_W + (2 * READ_HALF_W * col) / Math.max(1, READ_STEPS - 1);
        ty = cy - READ_HALF_H + (2 * READ_HALF_H * line) / Math.max(1, READ_LINES - 1);
        wakeAt = readFrom.current + (n + 1) * step;
      } else if (!hear && !talk && !busy && pointer.within && now - pointer.at < FOLLOW_MS) {
        tx = pointer.x;
        ty = pointer.y;
        wakeAt = pointer.at + FOLLOW_MS;
      } else if (think) {
        // Думает: вверх и в сторону, иногда в другую
        if (now > glance.until) glance = { x: Math.random() < 0.5 ? -0.55 : 0.55, y: -0.5, until: now + rand(1400, 2800) };
        tx = glance.x;
        ty = glance.y;
        wakeAt = glance.until;
      } else if (!hear) {
        // На собеседника; изредка — короткий взгляд в сторону
        if (now > nextGlance) {
          const k = talk ? 0.3 : 0.45;
          glance = { x: rand(-k, k), y: rand(-k * 0.6, k * 0.6), until: now + rand(450, 1200) };
          nextGlance = now + nextGlanceIn(talk);
        }
        if (now < glance.until) {
          tx = glance.x;
          ty = glance.y;
          wakeAt = glance.until;
        } else {
          wakeAt = nextGlance;
        }
      }
      const dt = Math.min(100, now - prev);
      prev = now;
      const k = 1 - Math.pow(1 - SMOOTH, dt / 16.7);
      cur.x += (tx - cur.x) * k;
      cur.y += (ty - cur.y) * k;
      const settled = Math.abs(tx - cur.x) < SETTLED && Math.abs(ty - cur.y) < SETTLED;
      if (settled) {
        cur.x = tx;
        cur.y = ty;
      }
      moveCb.current(cur.x, cur.y);
      if (!settled) {
        raf = view.requestAnimationFrame(tick);
        return;
      }
      // На месте: кадры не нужны до события или до смены цели по времени
      running = false;
      if (wakeAt !== Infinity) wakeTimer = view.setTimeout(start, Math.max(16, wakeAt - now + 1));
    }
    view.addEventListener('pointermove', onPointer);
    view.addEventListener('pointerdown', onPointer);
    start();
    return () => {
      view.removeEventListener('pointermove', onPointer);
      view.removeEventListener('pointerdown', onPointer);
      view.cancelAnimationFrame(raf);
      view.clearTimeout(wakeTimer);
      kick.current = () => {};
    };
  }, [elRef, followWithin, calm]);

  // Сменился режим — новая цель взгляда: цикл кадров, если стоял, — в путь
  const isReading = !!reading;
  useEffect(() => {
    kick.current();
  }, [speaking, listening, thinking, petted, asleep, attentive, isReading]);

  useEffect(() => {
    if (!nod) return;
    nodUntil.current = viewOf(elRef.current).performance.now() + NOD_MS;
    kick.current();
  }, [nod, elRef]);

  // Начала читать (или облачко сдвинулось) — с первой строки, сразу в путь;
  // кивок перед чтением дойдёт первым
  const readKey = reading ? `${reading.x.toFixed(2)}:${reading.y.toFixed(2)}` : '';
  useEffect(() => {
    if (!readKey) return;
    readFrom.current = Math.max(viewOf(elRef.current).performance.now(), nodUntil.current);
    kick.current();
  }, [readKey, elRef]);

  return useCallback(() => kick.current(), []);
}

/** Моргание: обычно раз в 2–4,5 с, иногда чаще или реже, двойное, медленное.
 *  enabled=false (спит, моргать нечем) — глаза открыты, таймеров нет. */
export function useBlink(elRef: RefObject<Element | null>, enabled: boolean): boolean {
  const [blink, setBlink] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    const view = viewOf(elRef.current);
    let alive = true;
    let timer = 0;
    const once = (dur: number, after: () => void) => {
      setBlink(true);
      timer = view.setTimeout(() => {
        if (!alive) return;
        setBlink(false);
        timer = view.setTimeout(after, rand(120, 220));
      }, dur);
    };
    const blinkNow = () => {
      if (!alive) return;
      const roll = Math.random();
      if (roll < 0.22) once(rand(80, 120), () => alive && once(rand(70, 110), schedule));
      else if (roll < 0.28) once(rand(260, 420), schedule);
      else once(rand(90, 150), schedule);
    };
    const schedule = () => {
      if (!alive) return;
      const u = Math.random();
      const wait = u < 0.12 ? rand(700, 1500) : u < 0.82 ? rand(1800, 4500) : rand(4500, 9000);
      timer = view.setTimeout(blinkNow, wait);
    };
    schedule();
    return () => {
      alive = false;
      view.clearTimeout(timer);
      setBlink(false);
    };
  }, [enabled, elRef]);
  return blink;
}

/** Мимолётная эмоция в покое: раз в 25–70 с на 1,4–2,4 с — вид глаз из pool
 *  (room/avatarEyes.idlePool); null — свои глаза персоны. Голов на странице
 *  много — у каждой свой случайный ритм, хором не меняются. */
export function useIdleExpression(elRef: RefObject<Element | null>, enabled: boolean, pool: readonly number[]): number | null {
  const [kind, setKind] = useState<number | null>(null);
  const poolKey = pool.join(',');
  useEffect(() => {
    const kinds = poolKey ? poolKey.split(',').map(Number) : [];
    if (!enabled || !kinds.length) {
      setKind(null);
      return;
    }
    const view = viewOf(elRef.current);
    let alive = true;
    let timer = 0;
    const schedule = () => {
      timer = view.setTimeout(show, rand(25000, 70000));
    };
    const show = () => {
      if (!alive) return;
      setKind(kinds[Math.floor(Math.random() * kinds.length)]);
      timer = view.setTimeout(() => {
        if (!alive) return;
        setKind(null);
        schedule();
      }, rand(1400, 2400));
    };
    schedule();
    return () => {
      alive = false;
      view.clearTimeout(timer);
    };
  }, [enabled, elRef, poolKey]);
  return enabled ? kind : null;
}

/** Эмоция на ms, когда счётчик trigger вырос (пришло новое сообщение и т. п.);
 *  первое значение и уменьшение (прочитали) — без вспышки. */
export function useFlashExpression(trigger: number, kind: number, ms = 2500): number | null {
  const [on, setOn] = useState(false);
  const last = useRef(trigger);
  useEffect(() => {
    const grew = trigger > last.current;
    last.current = trigger;
    if (!grew) return;
    setOn(true);
    const timer = window.setTimeout(() => setOn(false), ms);
    return () => window.clearTimeout(timer);
  }, [trigger, ms]);
  return on ? kind : null;
}

/** Рот, пока говорит: слоги «открыт → прикрыт», паузы между словами */
export function useMouth(elRef: RefObject<Element | null>, speaking: boolean): Mouth {
  const [mouth, setMouth] = useState<Mouth>(0);
  useEffect(() => {
    if (!speaking) {
      setMouth(0);
      return;
    }
    const view = viewOf(elRef.current);
    let alive = true;
    let timer = 0;
    const syllable = () => {
      if (!alive) return;
      if (Math.random() < 0.12) {
        setMouth(0);
        timer = view.setTimeout(syllable, rand(150, 300));
        return;
      }
      setMouth(Math.random() < 0.55 ? 2 : 1);
      timer = view.setTimeout(() => {
        if (!alive) return;
        setMouth(Math.random() < 0.3 ? 1 : 0);
        timer = view.setTimeout(syllable, rand(45, 90));
      }, rand(75, 140));
    };
    syllable();
    return () => {
      alive = false;
      view.clearTimeout(timer);
    };
  }, [speaking, elRef]);
  return mouth;
}
