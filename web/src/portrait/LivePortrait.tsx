import { useEffect, useMemo, useRef, useState } from 'react';
import { VOWEL_SHEET, VOWEL_SHEETS } from './portraitTypes';
import type { PortraitData, PortraitSheet, PortraitSheets, Vowel } from './portraitTypes';
import { clamp, useBlink, useGaze, useMouth } from './motion';
import type { Mouth } from './motion';
import { emotionOf } from './emotionHold';
import { useVowelMouth } from './visemes';
import type { SpeechCue } from './visemes';

/* Живой портрет персоны — 2D-«витубер» из листов ракурсов головы
   (app/api/portrait_api.py). Как она смотрит, моргает и говорит — общий
   движок motion.ts (тот же у аватара персоны без своей картинки); здесь —
   только кадры: взгляд округляется до клетки сетки, глаза и рот выбирают
   лист. Листов рта или моргания нет — эта часть просто не двигается.
   В покое портрет «дышит» (CSS, без анимации при prefers-reduced-motion).

   Листы «рот на гласную» (VA…VO) есть — в речи рот идёт по гласным текста
   (speech, visemes.ts); гласной без листа — B или C по раскрытию рта.
   Эмоция (emotion — id) со своим основным листом — весь набор листов её:
   глаза, рот и гласные — только её (чего у неё нет, то не двигается).
   Листы эмоции сначала расшифровываются, лицо меняется, когда они готовы:
   без пустого кадра посреди смены.

   Клетка на экране — onCell: по её прозрачности портрет на рабочем столе
   понимает, кликнули по фигуре или мимо (desktop/src/portrait/). */

// Лист под глаза и рот: нужного нет — ближайший (гласная → рот по раскрытию:
// открыт → приоткрыт → закрыт)
function pickSheet(avail: ReadonlySet<PortraitSheet>, eyesClosed: boolean, mouth: Mouth, vowel: Vowel | null): PortraitSheet {
  if (!eyesClosed && vowel && avail.has(VOWEL_SHEET[vowel])) return VOWEL_SHEET[vowel];
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
  attentive = false,
  nod = 0,
  reading = null,
  emotion = null,
  speech = null,
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
  /** Занята разговором (не только пока рот ходит) — за курсором не следит */
  attentive?: boolean;
  /** Сменился — кивнуть (услышала реплику) */
  nod?: number;
  /** Читает облачко: его середина в координатах взгляда (−1…1, x — вправо,
   *  y — вниз, как у курсора); null — не читает */
  reading?: { x: number; y: number } | null;
  /** id эмоции на лице (portraitEmotion); null или без своего листа A — обычное */
  emotion?: string | null;
  /** Что говорит — для рта по гласным */
  speech?: SpeechCue | null;
  label: string;
  className?: string;
  onCell?: (r: number, c: number) => void;
}) {
  const { rows, cols } = data;
  // Эмоция: её листы расшифрованы — лицо её (ready — id готовой)
  const emo = emotionOf(data, emotion);
  const [ready, setReady] = useState('');
  useEffect(() => {
    if (!emo) return;
    let alive = true;
    const urls = Object.values(emo.sheets).filter((u): u is string => !!u);
    void Promise.all(
      urls.map((u) => {
        const img = new Image();
        img.src = u;
        return img.decode().catch(() => {});
      }),
    ).then(() => alive && setReady(emo.id));
    return () => {
      alive = false;
    };
  }, [emo]);
  const face: PortraitSheets = emo && ready === emo.id ? emo.sheets : data.sheets;
  const keys = useMemo(() => (Object.keys(face) as PortraitSheet[]).filter((k) => face[k]), [face]);
  const avail = useMemo(() => new Set(keys), [keys]);
  const vowels = VOWEL_SHEETS.some((k) => avail.has(k));
  const elRef = useRef<HTMLDivElement>(null);
  const [cell, setCell] = useState({ r: Math.floor(rows / 2), c: Math.floor(cols / 2) });
  const cellCb = useRef(onCell);
  cellCb.current = onCell;

  // Взгляд — до клетки сетки; клетка сменилась — новый кадр
  const last = useRef({ r: -1, c: -1 });
  const kick = useGaze(
    elRef,
    { speaking, listening, thinking, petted, asleep, attentive, nod, reading },
    (x, y) => {
      const c = clamp(Math.round(((x + 1) / 2) * (cols - 1)), 0, cols - 1);
      const r = clamp(Math.round(((y + 1) / 2) * (rows - 1)), 0, rows - 1);
      if (r === last.current.r && c === last.current.c) return;
      last.current = { r, c };
      setCell(last.current);
      cellCb.current?.(r, c);
    },
  );
  // Другая сетка — клетку пересчитать
  useEffect(() => {
    last.current = { r: -1, c: -1 };
    kick();
  }, [rows, cols, kick]);

  // Спит — глаза и так закрыты: таймеры моргания не нужны
  const blink = useBlink(elRef, avail.has('D') && !asleep);
  const plain = useMouth(elRef, speaking && !vowels && (avail.has('B') || avail.has('C')));
  const sung = useVowelMouth(elRef, speaking && vowels, speech);
  const mouth = vowels ? sung.open : plain;

  const shown = pickSheet(avail, blink || petted || asleep, mouth, vowels ? sung.vowel : null);
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
        {/* Все листы лежат слоями: смена листа — без перезагрузки картинки.
            Обычные лежат и под эмоцией: вернуться к ним — мгновенно */}
        {(Object.keys(data.sheets) as PortraitSheet[]).map(
          (k) =>
            data.sheets[k] && (
              <div
                key={k}
                className="live-portrait-sheet"
                style={{
                  backgroundImage: `url(${data.sheets[k]})`,
                  backgroundSize: size,
                  backgroundPosition: pos,
                  opacity: face === data.sheets && k === shown ? 1 : 0,
                }}
              />
            ),
        )}
        {face !== data.sheets &&
          keys.map((k) => (
            <div
              key={`${emo?.id}.${k}`}
              className="live-portrait-sheet"
              style={{
                backgroundImage: `url(${face[k]})`,
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
