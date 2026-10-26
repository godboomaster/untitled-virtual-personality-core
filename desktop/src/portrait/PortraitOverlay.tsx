import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { useEffect, useRef, useState, type WheelEvent } from 'react';
import LivePortrait from '../../../web/src/portrait/LivePortrait';
import type { PortraitData } from '../../../web/src/portrait/portraitTypes';
import { t } from '../i18n';
import { buildMask, maskHit, type HitMask } from './hitMask';

/* Портрет на рабочем столе — страница прозрачного окна поверх всех окон
   (src-tauri/src/portrait.rs). Движок — тот же LivePortrait, что в
   голосовом режиме веба.

   Окно пропускает клики насквозь, пока курсор не над фигурой: панель шлёт
   положение курсора (portrait-cursor), страница смотрит альфу кадра под ним
   (hitMask) и говорит панели, ловить ли мышь (portrait_hit). Заодно портрет
   смотрит на курсор по всему экрану. Над фигурой:
   - клик — погладить: глаза закрыты, голова под руку, сердечко;
   - зажать и потянуть — переставить окно;
   - колёсико — больше или меньше;
   - правая кнопка — меню: чей портрет, убрать с рабочего стола. */

interface Loaded {
  persona: string | null;
  name?: string;
  data: PortraitData | null;
  offline: boolean;
  gone?: boolean;
}

const RETRY_MS = 15_000; // бот не ответил — спросить снова
const REFRESH_MS = 30 * 60_000; // атласы могли перезалить в вебе
const PAT_MS = 1100; // глаза закрыты после последнего поглаживания
const DRAG_PX = 5; // кнопка зажата и мышь сдвинулась — уже перетаскивание
const HEART_MS = 900;
const WHEEL_MS = 60;

const SQUASH: Keyframe[] = [
  { transform: 'scale(1, 1)' },
  { transform: 'scale(1.035, 0.94)', offset: 0.35 },
  { transform: 'scale(0.99, 1.015)', offset: 0.7 },
  { transform: 'scale(1, 1)' },
];

export default function PortraitOverlay() {
  const [st, setSt] = useState<Loaded | null>(null);
  const [reload, setReload] = useState(0);
  const [petted, setPetted] = useState(false);
  const [hearts, setHearts] = useState<{ id: number; x: number; y: number }[]>([]);
  const rootRef = useRef<HTMLDivElement>(null);
  const mask = useRef<HitMask | null>(null);
  const cell = useRef({ r: 0, c: 0 });
  const hit = useRef(false);
  const press = useRef<{ x: number; y: number } | null>(null);
  const patTimer = useRef(0);
  const heartId = useRef(0);
  const wheel = useRef({ acc: 0, timer: 0 });

  const data = st?.data?.sheets.A ? st.data : null;

  // ── Загрузка: сразу, по выбору персоны в меню и по таймеру ──
  useEffect(() => {
    let alive = true;
    let timer = 0;
    invoke<Loaded>('portrait_load')
      .then((res) => {
        if (!alive) return;
        // Тот же портрет (бот не ответил — из кэша) — без перерисовки и новой маски
        setSt((prev) =>
          prev?.data && res.data && prev.persona === res.persona && prev.data.updated_at === res.data.updated_at
            ? { ...prev, offline: res.offline }
            : res,
        );
        timer = window.setTimeout(() => setReload((n) => n + 1), res.offline || !res.persona ? RETRY_MS : REFRESH_MS);
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
    const un = listen('portrait-reload', () => setReload((n) => n + 1));
    return () => {
      void un.then((f) => f());
    };
  }, []);

  useEffect(() => {
    mask.current = null;
    if (!data) return;
    cell.current = { r: Math.floor(data.rows / 2), c: Math.floor(data.cols / 2) };
    let alive = true;
    buildMask(data)
      .then((m) => {
        if (alive) mask.current = m;
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [data]);

  // ── Над фигурой ли точка окна ──
  const solidAt = (x: number, y: number): boolean => {
    const el = document.elementFromPoint(x, y);
    if (!el) return false;
    if (el.closest('[data-solid]')) return true;
    const body = rootRef.current?.querySelector('.live-portrait-body');
    if (!body || !mask.current) return false;
    const r = body.getBoundingClientRect();
    return maskHit(mask.current, cell.current.r, cell.current.c, (x - r.left) / r.width, (y - r.top) / r.height);
  };

  const setHit = (next: boolean) => {
    if (next === hit.current) return;
    hit.current = next;
    void invoke('portrait_hit', { hit: next });
  };

  useEffect(() => {
    const un = listen<{ x: number; y: number }>('portrait-cursor', (e) => {
      const { x, y } = e.payload;
      // Взгляд — на курсор, где бы он ни был: LivePortrait слушает pointermove окна
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: x, clientY: y }));
      setHit(press.current !== null || solidAt(x, y));
    });
    return () => {
      void un.then((f) => f());
    };
    // solidAt и setHit читают только ref — подписка одна на всё время окна
  }, []);

  // ── Погладить ──
  const pat = (x: number, y: number) => {
    setPetted(true);
    window.clearTimeout(patTimer.current);
    patTimer.current = window.setTimeout(() => setPetted(false), PAT_MS);
    rootRef.current?.querySelector('.live-portrait')?.animate(SQUASH, { duration: 320, easing: 'ease-out' });
    const id = ++heartId.current;
    setHearts((h) => [...h.slice(-5), { id, x, y }]);
    window.setTimeout(() => setHearts((h) => h.filter((v) => v.id !== id)), HEART_MS);
  };

  useEffect(() => () => window.clearTimeout(patTimer.current), []);

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
      onPointerDown={(e) => {
        // Без портрета ловить нечего: клик по подсказке — её собственный
        if (e.button !== 0 || !data || !solidAt(e.clientX, e.clientY)) return;
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
        if (p && data) pat(p.x, p.y);
      }}
      onPointerCancel={() => {
        press.current = null;
      }}
      onContextMenu={(e) => {
        e.preventDefault();
        if (solidAt(e.clientX, e.clientY)) void invoke('portrait_menu');
      }}
      onWheel={(e) => {
        if (data && solidAt(e.clientX, e.clientY)) onWheel(e);
      }}
    >
      {data && (
        <LivePortrait
          data={data}
          petted={petted}
          label={st?.name ?? ''}
          onCell={(r, c) => {
            cell.current = { r, c };
          }}
        />
      )}
      {hint && (
        <button type="button" className="portrait-hint" data-solid onClick={() => void invoke('portrait_menu')}>
          <span>{hint}</span>
          <span className="portrait-hint-dim">{t('portraitMenu')}</span>
        </button>
      )}
      {hearts.map((h) => (
        <svg key={h.id} className="portrait-heart" style={{ left: h.x, top: h.y }} viewBox="0 0 24 22" aria-hidden>
          <path d="M12 21 2.6 11.6A5.6 5.6 0 0 1 12 3.9a5.6 5.6 0 0 1 9.4 7.7Z" />
        </svg>
      ))}
    </div>
  );
}
