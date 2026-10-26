import { useEffect, useMemo, useRef, useState } from 'react';
import type { PortraitData, PortraitSheet } from './portraitTypes';

/* Живой портрет персоны — 2D-«витубер» из листов ракурсов головы
   (app/api/portrait_api.py). Движок по мотивам tomari-guruguru (app.jsx,
   talk-app.jsx; © 2026 rotejin, MIT): взгляд — точка от −1 до 1 по каждой оси,
   сглаженная и округлённая до клетки сетки; моргание — с живым разбросом
   (иногда двойное, иногда медленное).

   «Пилот» — сама персона, а не человек перед камерой:
   - водите мышью или касаетесь экрана — смотрит туда (FOLLOW_MS после
     последнего движения);
   - иначе смотрит на собеседника, изредка отводя взгляд; думает над
     ответом (thinking) — взгляд вверх и в сторону; слушает — прямо;
   - говорит (speaking) — рот ходит слогами: открыт / приоткрыт / закрыт,
     с паузами между «словами». Синтез речи (браузерный и Android) звук
     наружу не отдаёт, поэтому рот — по ритму речи, а не по громкости;
   - её гладят по голове (petted, usePat) — глаза закрыты, голова чуть
     опущена;
   - спит (asleep, портрет на рабочем столе) — то же, но надолго: не
     моргает, за курсором не следит, дышит медленнее;
   - в покое «дышит» (CSS, без анимации при prefers-reduced-motion).
   Листов рта или моргания нет — эта часть просто не двигается.

   Энергия: цикл кадров крутится, только пока взгляд в пути. Дошёл до цели —
   стоит до движения курсора, смены режима или таймера следующего взгляда в
   сторону, поэтому неподвижный портрет (на рабочем столе он на экране
   всегда) кадров не тратит.

   Курсор, кадры и таймеры — того окна, где портрет показан (viewOf): в
   PiP-окне («Вынести портрет») он смотрит на курсор над этим окном и не
   замирает, когда вкладка приложения ушла в фон. Клетка на экране —
   onCell: по её прозрачности портрет на рабочем столе понимает, кликнули по
   фигуре или мимо (desktop/src/portrait/). */

type Mouth = 0 | 1 | 2; // закрыт, приоткрыт, открыт

const FOLLOW_MS = 3000;
const SMOOTH = 0.22; // доля пути к цели за кадр при 60 Гц
// Ближе к цели — уже на месте: цикл кадров останавливается
const SETTLED = 0.01;

const rand = (a: number, b: number) => a + Math.random() * (b - a);
// Окно, в документе которого лежит элемент (главное или PiP)
const viewOf = (el: HTMLElement | null): Window => el?.ownerDocument.defaultView ?? window;
const clamp = (v: number, a: number, b: number) => Math.min(b, Math.max(a, v));

// Лист под глаза и рот: нужного нет — ближайший (рот: открыт → приоткрыт → закрыт)
function pickSheet(avail: ReadonlySet<PortraitSheet>, eyesClosed: boolean, mouth: Mouth): PortraitSheet {
  const order: Mouth[] = mouth === 2 ? [2, 1, 0] : mouth === 1 ? [1, 2, 0] : [0];
  const open: PortraitSheet[] = ['A', 'B', 'C'];
  const shut: PortraitSheet[] = ['D', 'E', 'F'];
  if (eyesClosed) for (const m of order) if (avail.has(shut[m])) return shut[m];
  for (const m of order) if (avail.has(open[m])) return open[m];
  return 'A';
}

export default function LivePortrait({
  data,
  speaking = false,
  listening = false,
  thinking = false,
  petted = false,
  asleep = false,
  label,
  className = '',
  onCell,
}: {
  data: PortraitData;
  speaking?: boolean;
  listening?: boolean;
  thinking?: boolean;
  petted?: boolean;
  asleep?: boolean;
  label: string;
  className?: string;
  onCell?: (r: number, c: number) => void;
}) {
  const { rows, cols } = data;
  const keys = useMemo(
    () => (Object.keys(data.sheets) as PortraitSheet[]).filter((k) => data.sheets[k]),
    [data.sheets],
  );
  const avail = useMemo(() => new Set(keys), [keys]);
  const elRef = useRef<HTMLDivElement>(null);
  const [cell, setCell] = useState({ r: Math.floor(rows / 2), c: Math.floor(cols / 2) });
  const [blink, setBlink] = useState(false);
  const [mouth, setMouth] = useState<Mouth>(0);

  // Режим взгляда — в ref: цикл кадров читает свежий, не перезапускаясь
  const mode = useRef({ speaking, listening, thinking, petted, asleep });
  mode.current = { speaking, listening, thinking, petted, asleep };
  const cellCb = useRef(onCell);
  cellCb.current = onCell;
  // Разбудить остановленный цикл кадров (режим сменился)
  const kick = useRef<() => void>(() => {});

  // ── Взгляд ──
  useEffect(() => {
    const view = viewOf(elRef.current);
    const pointer = { x: 0, y: 0, at: -Infinity };
    const cur = { x: 0, y: 0 };
    // Отвести взгляд: куда и до какого времени; следующий — когда
    let glance = { x: 0, y: 0, until: 0 };
    let nextGlance = view.performance.now() + rand(2500, 5000);
    let last = { r: -1, c: -1 };
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
      const rect = el.getBoundingClientRect();
      const range = Math.max(260, rect.width * 1.1);
      pointer.x = clamp((e.clientX - (rect.left + rect.width / 2)) / range, -1, 1);
      pointer.y = clamp((e.clientY - (rect.top + rect.height * 0.45)) / range, -1, 1);
      pointer.at = view.performance.now();
      start();
    };

    function tick(now: number) {
      const { speaking: talk, listening: hear, thinking: think, petted: pet, asleep: sleep } = mode.current;
      let tx = 0;
      let ty = 0;
      // Когда цель сменится сама (кончится слежение за курсором, взгляд в
      // сторону): к этому времени остановленный цикл надо разбудить
      let wakeAt = Infinity;
      if (pet || sleep) {
        // Гладят или спит: голова чуть вниз, под руку
        ty = 0.35;
      } else if (!hear && now - pointer.at < FOLLOW_MS) {
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
          nextGlance = now + (talk ? rand(2000, 4500) : rand(3000, 7000));
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
      const c = clamp(Math.round(((cur.x + 1) / 2) * (cols - 1)), 0, cols - 1);
      const r = clamp(Math.round(((cur.y + 1) / 2) * (rows - 1)), 0, rows - 1);
      if (r !== last.r || c !== last.c) {
        last = { r, c };
        setCell(last);
        cellCb.current?.(r, c);
      }
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
  }, [rows, cols]);

  // Сменился режим — новая цель взгляда: цикл кадров, если стоял, — в путь
  useEffect(() => {
    kick.current();
  }, [speaking, listening, thinking, petted, asleep]);

  // ── Моргание: обычно раз в 2–4,5 с, иногда чаще или реже, двойное, медленное.
  // Спит — глаза и так закрыты: таймеры моргания не нужны ──
  const canBlink = avail.has('D') && !asleep;
  useEffect(() => {
    if (!canBlink) return;
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
  }, [canBlink]);

  // ── Рот: слоги «открыт → прикрыт», паузы между словами ──
  const canTalk = avail.has('B') || avail.has('C');
  useEffect(() => {
    if (!speaking || !canTalk) {
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
  }, [speaking, canTalk]);

  const shown = pickSheet(avail, blink || petted || asleep, mouth);
  const size = `${cols * 100}% ${rows * 100}%`;
  const pos = `${cols > 1 ? (cell.c / (cols - 1)) * 100 : 0}% ${rows > 1 ? (cell.r / (rows - 1)) * 100 : 0}%`;
  return (
    <div
      ref={elRef}
      className={`live-portrait ${speaking ? 'live-portrait--speaking' : ''} ${asleep ? 'live-portrait--asleep' : ''} ${className}`}
      role="img"
      aria-label={label}
    >
      <div className="live-portrait-body">
        {/* Все листы лежат слоями: смена листа — без перезагрузки картинки */}
        {keys.map((k) => (
          <div
            key={k}
            className="live-portrait-sheet"
            style={{
              backgroundImage: `url(${data.sheets[k]})`,
              backgroundSize: size,
              backgroundPosition: pos,
              opacity: k === shown ? 1 : 0,
            }}
          />
        ))}
      </div>
    </div>
  );
}
