import { useRef, useState } from 'react';
import { cutOf, totalAdjust } from './frameMath';
import type { FrameSource } from './useFrameEditor';

// Поля панелей редактора кадров: число с шагом, переключатель, миниатюра

// box — кадр с правками: он бывает больше кадра нарезки (frameBox), фигура
// в нём стоит низом по середине на полу (sink px выше низа), как на холсте. Атлас — в --fe-atlas у родителя.
// cell — кадр по порядку анимации, в атласе нарезки это клетка cutOf;
// sheet — cell уже клетка листа, без правок (кадры листа для «+ Кадр с листа»)
export function FrameThumb({ s, cell, box, sheet = false }: { s: FrameSource; cell: number; box: { w: number; h: number; sink?: number; ax?: number; floor?: number }; sheet?: boolean }) {
  const a = sheet ? { dx: 0, dy: 0, s: 1, r: 0, px: 0, py: 0 } : totalAdjust(s.fix, cell);
  const k = sheet ? cell : cutOf(s.order, cell);
  const c = k % s.cols;
  const r = Math.floor(k / s.cols);
  // Как на холсте: поворот вокруг центра, масштаб от низа-середины, сдвиг
  const turn = a.r ? ` translate(${(a.px / s.fw) * 100}%, ${(a.py / s.fh) * 100}%) rotate(${a.r}deg) translate(${(-a.px / s.fw) * 100}%, ${(-a.py / s.fh) * 100}%)` : '';
  return (
    <span className="fe-thumb" style={{ aspectRatio: `${box.w} / ${box.h}` }}>
      <span
        className="fe-thumb-img"
        style={{
          left: `${(((box.ax ?? box.w / 2) - s.fw / 2) / box.w) * 100}%`,
          // Низ кадра нарезки — под полом рамки на floor (низ экрана анимации)
          bottom: `${(((box.sink ?? 0) - (box.floor ?? 0)) / box.h) * 100}%`,
          width: `${(s.fw / box.w) * 100}%`,
          height: `${(s.fh / box.h) * 100}%`,
          backgroundSize: `${s.cols * 100}% ${s.rows * 100}%`,
          backgroundPosition: `${s.cols > 1 ? (c / (s.cols - 1)) * 100 : 0}% ${s.rows > 1 ? (r / (s.rows - 1)) * 100 : 0}%`,
          transform: `translate(${(a.dx / s.fw) * 100}%, ${(a.dy / s.fh) * 100}%) scale(${a.s})${turn}`,
        }}
      />
    </span>
  );
}

export function NumField({
  label,
  value,
  step = 1,
  min,
  max,
  suffix,
  onChange,
}: {
  label: string;
  value: number;
  step?: number;
  min?: number;
  max?: number;
  suffix?: string;
  onChange: (v: number) => void;
}) {
  const [text, setText] = useState<string | null>(null); // пока печатают
  const before = useRef(value); // значение до правки: стёртое поле вернётся к нему
  const clampV = (v: number) => Math.max(min ?? -Infinity, Math.min(max ?? Infinity, v));
  const parse = (s: string) => (s === '' ? NaN : Number(s.replace(',', '.').replace('−', '-')));
  return (
    <label className="fe-num">
      <span className="fe-num-label">{label}</span>
      <span className="fe-num-box">
        <button type="button" className="fe-num-step" tabIndex={-1} aria-label={`${label} −`} onClick={() => onChange(clampV(value - step))}>
          −
        </button>
        <input
          className="fe-num-input"
          inputMode="decimal"
          value={text ?? String(value)}
          onFocus={() => {
            before.current = value;
          }}
          onChange={(e) => {
            setText(e.target.value);
            const v = parse(e.target.value);
            if (Number.isFinite(v)) onChange(clampV(v));
          }}
          onBlur={() => {
            if (text !== null && !Number.isFinite(parse(text)) && before.current !== value) onChange(before.current);
            setText(null);
          }}
        />
        {suffix && <span className="fe-num-suffix">{suffix}</span>}
        <button type="button" className="fe-num-step" tabIndex={-1} aria-label={`${label} +`} onClick={() => onChange(clampV(value + step))}>
          +
        </button>
      </span>
    </label>
  );
}

export function Seg<T extends string>({ value, options, label, onChange }: { value: T; options: readonly T[]; label: (v: T) => string; onChange: (v: T) => void }) {
  return (
    <div className="fe-seg" role="group">
      {options.map((o) => (
        <button key={o} type="button" className={`fe-seg-btn ${o === value ? 'fe-seg-btn--on' : ''}`} aria-pressed={o === value} onClick={() => onChange(o)}>
          {label(o)}
        </button>
      ))}
    </div>
  );
}
