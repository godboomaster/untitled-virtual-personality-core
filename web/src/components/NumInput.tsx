import { useRef, useState } from 'react';

/* Числовое поле формы. Пока печатают, в поле то, что напечатано: можно стереть
   всё или пройти через число за границей. Наружу уходят только допустимые
   числа, остальное поправляется при уходе из поля: пустое — на значение до
   правки (при стирании по цифре промежуточные числа уже ушли наружу), за
   границей — на границу. Если пустое поле тоже значение, задайте onClear. */

interface NumInputProps {
  value: number | null | undefined;
  min: number;
  max: number;
  step?: number; // дробный шаг — дробные значения, иначе только целые
  placeholder?: string;
  onChange: (v: number) => void;
  onClear?: () => void;
}

export default function NumInput({ value, min, max, step = 1, placeholder, onChange, onClear }: NumInputProps) {
  const [text, setText] = useState<string | null>(null);
  const before = useRef(value);

  const parse = (s: string | null) => {
    const v = Number(s);
    if (!s || !Number.isFinite(v)) return null;
    return Number.isInteger(step) ? Math.round(v) : v;
  };

  return (
    <input
      className="input"
      type="number"
      min={min}
      max={max}
      step={step}
      placeholder={placeholder}
      value={text ?? value ?? ''}
      onFocus={() => {
        before.current = value;
      }}
      onChange={(e) => {
        setText(e.target.value);
        const v = parse(e.target.value);
        if (v === null) onClear?.();
        else if (v >= min && v <= max) onChange(v);
      }}
      onBlur={() => {
        const v = parse(text);
        if (v !== null) {
          if (v < min || v > max) onChange(Math.max(min, Math.min(max, v)));
        } else if (text !== null && !onClear && before.current != null && before.current !== value) {
          onChange(before.current);
        }
        setText(null);
      }}
    />
  );
}
