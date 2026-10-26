import { useRef } from 'react';
import LivePortrait from './LivePortrait';
import { hitOnBody, useHitMask } from './hitMask';
import { pokePet } from './portraitStore';
import type { PortraitData } from './portraitTypes';
import PatHearts from './PatHearts';
import { usePat } from './usePat';

/* Живой портрет, который можно погладить, — голосовой режим и PiP-окно веба
   (на рабочем столе своё окно, desktop/src/portrait/). Касание или клик по
   фигуре (маска по альфе листа, мимо головы — ничего) — погладить; провести
   по голове пальцем или мышью с нажатой кнопкой — гладить, сердечко на
   каждые STROKE_PX пути. Ядру — pokePet: персона узнает, если включены
   «тычки». */

const TAP_PX = 8; // сдвиг меньше — ещё касание, а не проведение
const STROKE_PX = 60;

interface Press {
  x: number;
  y: number;
  travel: number;
  stroked: boolean;
}

export default function PettablePortrait({
  personaId,
  data,
  speaking,
  listening,
  thinking,
  label,
}: {
  personaId: string;
  data: PortraitData;
  speaking?: boolean;
  listening?: boolean;
  thinking?: boolean;
  label: string;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const mask = useHitMask(data);
  const cell = useRef({ r: Math.floor(data.rows / 2), c: Math.floor(data.cols / 2) });
  const press = useRef<Press | null>(null);
  const over = useRef(false);
  const { petted, hearts, pat } = usePat(() => pokePet(personaId));

  const onFigure = (x: number, y: number) =>
    hitOnBody(mask.current, cell.current, wrap.current?.querySelector('.live-portrait-body'), x, y);

  const patAt = (x: number, y: number) => {
    const el = wrap.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    pat(x - box.left, y - box.top, el.querySelector('.live-portrait'));
  };

  return (
    <div
      ref={wrap}
      className="portrait-pettable"
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        press.current = { x: e.clientX, y: e.clientY, travel: 0, stroked: false };
      }}
      onPointerMove={(e) => {
        const p = press.current;
        if (!p) {
          // Курсор над головой — рука: здесь можно погладить
          const hit = e.pointerType === 'mouse' && onFigure(e.clientX, e.clientY);
          if (hit !== over.current) {
            over.current = hit;
            e.currentTarget.classList.toggle('portrait-pettable--over', hit);
          }
          return;
        }
        p.travel += Math.hypot(e.clientX - p.x, e.clientY - p.y);
        p.x = e.clientX;
        p.y = e.clientY;
        if (p.travel >= STROKE_PX && onFigure(e.clientX, e.clientY)) {
          p.travel = 0;
          p.stroked = true;
          patAt(e.clientX, e.clientY);
        }
      }}
      onPointerUp={(e) => {
        const p = press.current;
        press.current = null;
        if (p && !p.stroked && p.travel <= TAP_PX && onFigure(e.clientX, e.clientY)) patAt(e.clientX, e.clientY);
      }}
      onPointerCancel={() => {
        press.current = null;
      }}
      onPointerLeave={(e) => {
        press.current = null;
        over.current = false;
        e.currentTarget.classList.remove('portrait-pettable--over');
      }}
    >
      <LivePortrait
        data={data}
        speaking={speaking}
        listening={listening}
        thinking={thinking}
        petted={petted}
        label={label}
        onCell={(r, c) => {
          cell.current = { r, c };
        }}
      />
      <PatHearts hearts={hearts} />
    </div>
  );
}
