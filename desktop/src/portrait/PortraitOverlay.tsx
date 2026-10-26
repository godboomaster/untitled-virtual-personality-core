import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { useEffect, useRef, useState, type WheelEvent } from 'react';
import { hitOnBody, useHitMask } from '../../../web/src/portrait/hitMask';
import LivePortrait from '../../../web/src/portrait/LivePortrait';
import type { PortraitData } from '../../../web/src/portrait/portraitTypes';
import PatHearts from '../../../web/src/portrait/PatHearts';
import { usePat } from '../../../web/src/portrait/usePat';
import { t } from '../i18n';

/* Портрет на рабочем столе — страница прозрачного окна поверх всех окон
   (src-tauri/src/portrait.rs). Движок — тот же LivePortrait, что в
   голосовом режиме веба.

   Окно пропускает клики насквозь, пока курсор не над фигурой: панель шлёт
   положение курсора (portrait-cursor), страница смотрит альфу кадра под ним
   (hitMask) и говорит панели, ловить ли мышь (portrait_hit). Заодно портрет
   смотрит на курсор по всему экрану. Над фигурой:
   - клик — погладить: глаза закрыты, голова под руку, сердечко; ядру —
     portrait_pet (персона узнает, если включены «тычки»);
   - зажать и потянуть — переставить окно;
   - колёсико — больше или меньше;
   - правая кнопка — меню: чей портрет, облачка, убрать с рабочего стола.

   Персона написала (portrait-say) — реплика облачком над головой, рот
   ходит; облачко висит, пока его можно прочесть (вас нет — ждёт вашего
   возвращения), клик по нему — чат в вебе.

   Спит — глаза закрыты, голова опущена, за курсором не следит: когда вас нет
   у компьютера (portrait-away), когда персона спит в «Комнате» (asleep с
   ядра), а если ядро этого не знает — ночью по часам компьютера. Погладили —
   просыпается и не спит WAKE_MS после последнего касания, потом засыпает
   снова. */

interface Loaded {
  persona: string | null;
  name?: string;
  data: PortraitData | null;
  offline: boolean;
  gone?: boolean;
  asleep?: boolean | null;
  away?: boolean;
  bubbles?: boolean;
  bubbleH?: number;
}

const RETRY_MS = 15_000; // бот не ответил и показать нечего — спросить снова
const OFFLINE_MS = 60_000; // бот не ответил, портрет с диска или из кэша
// Спит ли персона и не правили ли портрет: атласы качаются, только если правили
const REFRESH_MS = 10 * 60_000;
const DRAG_PX = 5; // кнопка зажата и мышь сдвинулась — уже перетаскивание
const WHEEL_MS = 60;
// Реплика: рот ходит по длине текста, облачко висит, пока его можно прочесть
const TALK_MS = { perChar: 55, min: 1200, max: 6000 };
const READ_MS = { perChar: 60, min: 7000, max: 25_000 };
// Ночь по часам компьютера — когда ядро не знает, спит ли персона
const NIGHT_FROM_H = 0;
const NIGHT_TO_H = 7;
// Погладили спящую — не спит столько после последнего касания
const WAKE_MS = 5 * 60_000;

const span = (text: string, k: { perChar: number; min: number; max: number }) =>
  Math.min(k.max, Math.max(k.min, text.length * k.perChar));

function isNight(d = new Date()): boolean {
  const h = d.getHours();
  return h >= NIGHT_FROM_H && h < NIGHT_TO_H;
}

// Сколько до ближайшей границы ночи (начала или конца)
function msToNightEdge(d = new Date()): number {
  const edge = new Date(d);
  edge.setMinutes(0, 0, 0);
  if (isNight(d)) edge.setHours(NIGHT_TO_H);
  else {
    edge.setHours(NIGHT_FROM_H);
    if (edge <= d) edge.setDate(edge.getDate() + 1);
  }
  return Math.max(1000, edge.getTime() - d.getTime() + 1000);
}

export default function PortraitOverlay() {
  const [st, setSt] = useState<Loaded | null>(null);
  const [reload, setReload] = useState(0);
  const [away, setAway] = useState(false);
  const [night, setNight] = useState(isNight);
  const [said, setSaid] = useState<{ id: number; text: string } | null>(null);
  const [talking, setTalking] = useState(false);
  const [reading, setReading] = useState(false); // курсор над облачком — не прятать
  const [woke, setWoke] = useState(false); // погладили — проснулась
  const rootRef = useRef<HTMLDivElement>(null);
  const cell = useRef({ r: 0, c: 0 });
  const hit = useRef(false);
  const press = useRef<{ x: number; y: number } | null>(null);
  const wheel = useRef({ acc: 0, timer: 0 });
  const saidId = useRef(0);
  const talkTimer = useRef(0);
  const wakeTimer = useRef(0);
  const { petted, hearts, pat } = usePat(() => {
    void invoke('portrait_pet');
    // Каждое касание — ещё WAKE_MS без сна
    setWoke(true);
    window.clearTimeout(wakeTimer.current);
    wakeTimer.current = window.setTimeout(() => setWoke(false), WAKE_MS);
  });

  const data = st?.data?.sheets.A ? st.data : null;
  const mask = useHitMask(data);
  const bubbleH = st?.bubbles ? (st.bubbleH ?? 0) : 0;
  // Пишет — не спит; иначе спит, когда вас нет, а если недавно не гладили —
  // и когда спит в комнате или ночью
  const asleep = !said && !talking && (away || (!woke && (st?.asleep ?? night)));
  const sleepRef = useRef(asleep);
  sleepRef.current = asleep;

  // ── Загрузка: сразу, по выбору персоны в меню и по таймеру ──
  useEffect(() => {
    let alive = true;
    let timer = 0;
    invoke<Loaded>('portrait_load')
      .then((res) => {
        if (!alive) return;
        // Тот же портрет (бот не ответил или не правили — из кэша) — без
        // перерисовки и новой маски; свежие только состояния
        setSt((prev) =>
          prev?.data && res.data && prev.persona === res.persona && prev.data.updated_at === res.data.updated_at
            ? { ...res, data: prev.data }
            : res,
        );
        if (typeof res.away === 'boolean') setAway(res.away);
        const wait = !res.persona || (res.offline && !res.data?.sheets.A) ? RETRY_MS : res.offline ? OFFLINE_MS : REFRESH_MS;
        timer = window.setTimeout(() => setReload((n) => n + 1), wait);
      })
      .catch(() => {
        if (alive) timer = window.setTimeout(() => setReload((n) => n + 1), RETRY_MS);
      });
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [reload]);

  useEffect(() => {
    const un = [
      listen('portrait-reload', () => setReload((n) => n + 1)),
      listen<{ away: boolean }>('portrait-away', (e) => {
        setAway(e.payload.away);
        // Вернулись — заодно освежить, спит ли персона
        if (!e.payload.away) setReload((n) => n + 1);
      }),
      listen<{ text: string }>('portrait-say', (e) => {
        const text = e.payload.text;
        setSaid({ id: ++saidId.current, text });
        setTalking(true);
        window.clearTimeout(talkTimer.current);
        talkTimer.current = window.setTimeout(() => setTalking(false), span(text, TALK_MS));
      }),
    ];
    return () => {
      window.clearTimeout(talkTimer.current);
      window.clearTimeout(wakeTimer.current);
      un.forEach((p) => void p.then((f) => f()));
    };
  }, []);

  // Ночь началась или кончилась — один таймер до ближайшей границы
  useEffect(() => {
    const timer = window.setTimeout(() => setNight(isNight()), msToNightEdge());
    return () => window.clearTimeout(timer);
  }, [night]);

  // Облачко висит, пока его можно прочесть: вас нет или курсор над ним — ждёт
  useEffect(() => {
    if (!said || away || reading) return;
    const timer = window.setTimeout(() => setSaid(null), span(said.text, READ_MS));
    return () => window.clearTimeout(timer);
  }, [said, away, reading]);

  useEffect(() => {
    if (!data) return;
    cell.current = { r: Math.floor(data.rows / 2), c: Math.floor(data.cols / 2) };
  }, [data]);

  // Облачка выключили в меню — убрать показанное
  useEffect(() => {
    if (!bubbleH) setSaid(null);
  }, [bubbleH]);

  // ── Над фигурой ли точка окна ──
  const onFigure = (x: number, y: number): boolean =>
    hitOnBody(mask.current, cell.current, rootRef.current?.querySelector('.live-portrait-body'), x, y);

  const solidAt = (x: number, y: number): boolean => {
    if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return false;
    if (document.elementFromPoint(x, y)?.closest('[data-solid]')) return true;
    return onFigure(x, y);
  };

  const setHit = (next: boolean) => {
    if (next === hit.current) return;
    hit.current = next;
    void invoke('portrait_hit', { hit: next });
  };

  useEffect(() => {
    const un = listen<{ x: number; y: number }>('portrait-cursor', (e) => {
      const { x, y } = e.payload;
      // Взгляд — на курсор, где бы он ни был: LivePortrait слушает pointermove
      // окна. Спит — не смотрит, и кадры на это не тратим
      if (!sleepRef.current) window.dispatchEvent(new PointerEvent('pointermove', { clientX: x, clientY: y }));
      setHit(press.current !== null || solidAt(x, y));
    });
    return () => {
      void un.then((f) => f());
    };
    // solidAt и setHit читают только ref — подписка одна на всё время окна
  }, []);

  const onWheel = (e: WheelEvent) => {
    const w = wheel.current;
    w.acc += e.deltaY;
    if (w.timer) return;
    w.timer = window.setTimeout(() => {
      const factor = Math.min(1.25, Math.max(0.8, Math.exp(-w.acc * 0.0015)));
      w.acc = 0;
      w.timer = 0;
      void invoke('portrait_resize', { factor });
    }, WHEEL_MS);
  };

  const hint = !st
    ? null
    : !st.persona
      ? t(st.offline ? 'portraitWaitBot' : 'portraitNone')
      : st.gone
        ? t('portraitGone')
        : !data
          ? st.offline
            ? t('portraitWaitBot')
            : t('portraitEmpty', { name: st.name ?? st.persona })
          : null;

  return (
    <div
      ref={rootRef}
      className="portrait-overlay"
      style={{ ['--bubble-h' as string]: `${bubbleH}px` }}
      onPointerDown={(e) => {
        // Облачко и подсказка — свои кнопки; без портрета ловить нечего
        if (e.button !== 0 || !data || (e.target as Element).closest('[data-solid]')) return;
        if (!onFigure(e.clientX, e.clientY)) return;
        press.current = { x: e.clientX, y: e.clientY };
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        const p = press.current;
        if (!p || Math.hypot(e.clientX - p.x, e.clientY - p.y) <= DRAG_PX) return;
        press.current = null;
        void invoke('portrait_drag');
      }}
      onPointerUp={() => {
        const p = press.current;
        press.current = null;
        if (p && data) pat(p.x, p.y, rootRef.current?.querySelector('.live-portrait'));
      }}
      onPointerCancel={() => {
        press.current = null;
      }}
      onContextMenu={(e) => {
        e.preventDefault();
        if (solidAt(e.clientX, e.clientY)) void invoke('portrait_menu');
      }}
      onWheel={(e) => {
        if (data && onFigure(e.clientX, e.clientY)) onWheel(e);
      }}
    >
      {said && bubbleH > 0 && (
        <div className="portrait-bubble-area">
          <div
            key={said.id}
            className="portrait-bubble"
            data-solid
            role="button"
            tabIndex={-1}
            title={t('portraitBubbleOpen')}
            onPointerEnter={() => setReading(true)}
            onPointerLeave={() => setReading(false)}
            onClick={() => {
              setSaid(null);
              setReading(false);
              void invoke('portrait_open_chat');
            }}
          >
            <span className="portrait-bubble-text">{said.text}</span>
            <button
              type="button"
              className="portrait-bubble-close"
              aria-label={t('portraitBubbleClose')}
              onClick={(e) => {
                e.stopPropagation();
                setSaid(null);
                setReading(false);
              }}
            >
              <svg viewBox="0 0 10 10" aria-hidden>
                <path d="M2 2 8 8M8 2 2 8" />
              </svg>
            </button>
          </div>
        </div>
      )}
      <div className="portrait-head">
        {data && (
          <LivePortrait
            data={data}
            petted={petted}
            asleep={asleep}
            speaking={talking}
            label={st?.name ?? ''}
            onCell={(r, c) => {
              cell.current = { r, c };
            }}
          />
        )}
        {data && asleep && (
          <div className="portrait-z" aria-hidden>
            <span>z</span>
            <span>z</span>
          </div>
        )}
      </div>
      {hint && (
        <button type="button" className="portrait-hint" data-solid onClick={() => void invoke('portrait_menu')}>
          <span>{hint}</span>
          <span className="portrait-hint-dim">{t('portraitMenu')}</span>
        </button>
      )}
      <PatHearts hearts={hearts} />
    </div>
  );
}
