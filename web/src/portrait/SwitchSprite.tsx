import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import AnimSprite from './AnimSprite';
import { animAnchor, animSeconds, animSink, type PortraitAnim } from './animTypes';

/* Анимация перехода между режимами персоны на месте головы (голосовой
   режим, PiP; useLiveAvatar). Блок растянут на родителя; голова — его
   .live-portrait (нет её — квадрат по меньшей стороне родителя внизу по
   середине). Кадр стоит низом к низу головы, серединой пола — к её середине,
   длинная сторона — голова × scale, как на рабочем столе. Играет один раз
   (с повторами анимации) и зовёт onEnd; не доиграла за разумное время
   (вкладка в фоне) — всё равно onEnd. */

const SLACK_MS = 3000;

interface Box {
  x: number; // середина головы от левого края блока
  bottom: number; // низ головы от низа блока
  size: number; // сторона головы
}

export default function SwitchSprite({ anim, onEnd, view }: { anim: PortraitAnim; onEnd: () => void; view?: Window }) {
  const ref = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<Box | null>(null);
  const onEndRef = useRef(onEnd);
  onEndRef.current = onEnd;

  useLayoutEffect(() => {
    const el = ref.current;
    const parent = el?.parentElement;
    if (!el || !parent) return;
    const measure = () => {
      const p = parent.getBoundingClientRect();
      const head = parent.querySelector('.live-portrait')?.getBoundingClientRect();
      const next: Box = head
        ? { x: head.left - p.left + head.width / 2, bottom: p.bottom - head.bottom, size: head.width }
        : { x: p.width / 2, bottom: 0, size: Math.min(p.width, p.height) };
      if (next.size > 0) setBox(next);
    };
    measure();
    const Obs = (view as (Window & typeof globalThis) | undefined)?.ResizeObserver ?? ResizeObserver;
    const ro = new Obs(measure);
    ro.observe(parent);
    return () => ro.disconnect();
  }, [view]);

  useEffect(() => {
    const w = view ?? window;
    const timer = w.setTimeout(() => onEndRef.current(), animSeconds(anim) * 1000 + SLACK_MS);
    return () => w.clearTimeout(timer);
  }, [anim, view]);

  return (
    <div ref={ref} className="avatar-switch" aria-hidden>
      {box && (
        <AnimSprite
          anim={anim}
          size={Math.round(box.size)}
          view={view}
          onEnd={() => onEndRef.current()}
          className="avatar-switch-sprite"
          style={{ left: box.x - animAnchor(anim, box.size), bottom: box.bottom - animSink(anim, box.size) }}
        />
      )}
    </div>
  );
}
