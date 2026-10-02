import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { useI18n } from '../i18n';

/* Выбор цвета метки персоны (карточка «Персоны»): кнопка-образец и HUD-поповер
   с палитрой и полем HEX. Системный color-picker браузера не используем —
   выбивается из дизайна. Поповер — порталом в body, позиция fixed от кнопки
   (карточка с transform при hover утащила бы его за собой). */

// Первые 8 — палитра бэкенда по умолчанию (app/api/runtime._COLOR_PALETTE):
// приглушённые тона, различимые на светлой и тёмной теме
const PALETTE = [
  '#e0683a', '#3f8cff', '#3fae6b', '#b06fd6',
  '#c9a227', '#38b6a5', '#d64f6e', '#7a9a3a',
  '#6a5fd0', '#d97fb0', '#5b8ea6', '#a8744f',
  '#8a8f98', '#e0a33a', '#4fb3d9', '#9c5a5a',
];

const HEX_RE = /^#[0-9a-f]{6}$/i;
const POPOVER_WIDTH = 232;

interface PersonaColorPickerProps {
  color: string | null | undefined;
  // Сохранить цвет; null — вернуть цвет по умолчанию. Reject — показать ошибку
  onChange: (color: string | null) => Promise<void>;
  className?: string;
}

export default function PersonaColorPicker({ color, onChange, className }: PersonaColorPickerProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number } | null>(null);
  const [hex, setHex] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const btnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const current = (color ?? '').toLowerCase();

  const close = () => {
    setOpen(false);
    setPos(null);
  };

  // Позиция: под кнопкой (или над ней, если снизу мало места), не за краем окна
  useLayoutEffect(() => {
    if (!open) return;
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    const left = Math.min(Math.max(8, r.right - POPOVER_WIDTH), window.innerWidth - POPOVER_WIDTH - 8);
    const up = window.innerHeight - r.bottom < 260 && r.top > window.innerHeight - r.bottom;
    setPos(up ? { left, bottom: window.innerHeight - r.top + 6 } : { left, top: r.bottom + 6 });
  }, [open]);

  // Закрытие: клик мимо, Esc, прокрутка страницы
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!popRef.current?.contains(target) && !btnRef.current?.contains(target)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close();
        btnRef.current?.focus();
      }
    };
    const onScroll = (e: Event) => {
      if (!popRef.current?.contains(e.target as Node)) close();
    };
    document.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('scroll', onScroll, true);
    return () => {
      document.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [open]);

  const apply = async (next: string | null) => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await onChange(next);
      close();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const hexValid = HEX_RE.test(hex.trim());

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className={'persona-icon-btn color-picker-btn' + (open ? ' persona-icon-btn--on' : '') + (className ? ` ${className}` : '')}
        title={t('personas.color')}
        aria-label={t('personas.color')}
        aria-expanded={open}
        onClick={() => {
          if (open) return close();
          setHex(current);
          setError('');
          setOpen(true);
        }}
      >
        <span className="color-picker-swatch" style={{ background: color ?? 'transparent' }} />
      </button>
      {open &&
        pos &&
        createPortal(
          <div
            ref={popRef}
            className="color-popover"
            role="dialog"
            aria-label={t('personas.color')}
            style={{ left: pos.left, top: pos.top, bottom: pos.bottom, width: POPOVER_WIDTH }}
          >
            <div className="color-popover-title">{t('personas.color')}</div>
            <div className="color-popover-grid">
              {PALETTE.map((c, i) => (
                <button
                  key={c}
                  type="button"
                  className={'color-popover-cell' + (c === current ? ' color-popover-cell--active' : '')}
                  style={{ '--c': c, '--i': i } as CSSProperties}
                  title={c}
                  aria-label={c}
                  disabled={busy}
                  onClick={() => void apply(c)}
                />
              ))}
            </div>
            <div className="color-popover-hex">
              <span className="color-popover-preview" style={{ background: hexValid ? hex.trim() : 'transparent' }} />
              <input
                className="input"
                value={hex}
                placeholder="#rrggbb"
                maxLength={7}
                spellCheck={false}
                disabled={busy}
                onChange={(e) => {
                  const v = e.target.value.trim();
                  setHex(v && !v.startsWith('#') ? `#${v}` : v);
                  setError('');
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && hexValid) void apply(hex.trim().toLowerCase());
                }}
              />
              <button
                type="button"
                className="btn btn--primary"
                disabled={!hexValid || busy || hex.trim().toLowerCase() === current}
                onClick={() => void apply(hex.trim().toLowerCase())}
              >
                OK
              </button>
            </div>
            {error && <div className="color-popover-error">// {error}</div>}
            <button type="button" className="color-popover-reset" disabled={busy} onClick={() => void apply(null)}>
              {t('personas.colorReset')}
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}
