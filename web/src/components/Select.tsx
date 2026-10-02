import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import Icon from './icons';

/* Выпадающий список в HUD-стиле вместо нативного <select>: системный попап
   (macOS/Windows) не стилизуется CSS и выбивается из дизайна. Триггер —
   кнопка с классом .input, меню рендерится порталом в body (не обрезается
   скроллящимися колонками и модалками). Клавиатура как у <select>:
   стрелки/Home/End, Enter/пробел — выбор, Esc/Tab — закрыть, буквы — поиск.
   Динамика: меню раскрывается от поля и сворачивается обратно, пункты
   появляются каскадом, подсветка скользит между пунктами. */

export interface SelectOption {
  value: string;
  label: string;
  hint?: string; // пояснение второй строкой в меню (в поле не показывается)
}

interface SelectProps {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  id?: string;
  className?: string;
  style?: CSSProperties;
  title?: string;
  disabled?: boolean;
}

const MENU_MAX_HEIGHT = 280;
const MENU_GAP = 4;
const CLOSE_MS = 120; // длительность анимации сворачивания (.select-menu--closing)

let uid = 0;

export default function Select({ value, options, onChange, id, className, style, title, disabled }: SelectProps) {
  const [open, setOpen] = useState(false);
  const [closing, setClosing] = useState(false); // меню ещё в DOM, играет анимация сворачивания
  const [active, setActive] = useState(-1); // подсвеченный пункт (мышь/стрелки)
  const [pos, setPos] = useState<{ left: number; width: number; top?: number; bottom?: number; maxHeight: number; up: boolean } | null>(null);
  // Скользящая подсветка: координаты активного пункта внутри меню
  const [hl, setHl] = useState<{ top: number; height: number } | null>(null);
  const hlPlaced = useRef(false); // первая установка — без анимации переезда
  const closeTimer = useRef<number | undefined>(undefined);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const typeahead = useRef({ text: '', at: 0 });
  const [listId] = useState(() => `vpc-select-${++uid}`);

  const selectedIndex = options.findIndex((o) => o.value === value);
  const current = options[selectedIndex];

  const shown = open && !closing; // логически открыт (для клавиатуры и aria)

  const openMenu = () => {
    if (disabled || !options.length) return;
    window.clearTimeout(closeTimer.current);
    setActive(selectedIndex >= 0 ? selectedIndex : 0);
    setClosing(false);
    setOpen(true);
  };

  const close = () => {
    if (!open || closing) return;
    setClosing(true);
    closeTimer.current = window.setTimeout(() => {
      setOpen(false);
      setClosing(false);
      setPos(null);
      setHl(null);
      hlPlaced.current = false;
    }, CLOSE_MS);
  };

  useEffect(() => () => window.clearTimeout(closeTimer.current), []);

  const choose = (i: number) => {
    const opt = options[i];
    close();
    triggerRef.current?.focus();
    if (opt && opt.value !== value) onChange(opt.value);
  };

  // Позиция меню: под триггером, а если снизу не хватает места — над ним
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect();
      if (!r) return;
      const below = window.innerHeight - r.bottom - MENU_GAP - 8;
      const above = r.top - MENU_GAP - 8;
      const up = below < Math.min(MENU_MAX_HEIGHT, 160) && above > below;
      setPos({
        left: r.left,
        width: r.width,
        ...(up ? { bottom: window.innerHeight - r.top + MENU_GAP } : { top: r.bottom + MENU_GAP }),
        maxHeight: Math.min(MENU_MAX_HEIGHT, up ? above : below),
        up,
      });
    };
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [open]);

  // Закрытие: клик мимо, прокрутка страницы под меню
  useEffect(() => {
    if (!shown) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!menuRef.current?.contains(t) && !triggerRef.current?.contains(t)) close();
    };
    const onScroll = (e: Event) => {
      if (!menuRef.current?.contains(e.target as Node)) close();
    };
    document.addEventListener('mousedown', onDown);
    window.addEventListener('scroll', onScroll, true);
    return () => {
      document.removeEventListener('mousedown', onDown);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [shown]);

  // Подсвеченный пункт: подсветка переезжает на него и он всегда в зоне
  // видимости (стрелки, поиск по буквам)
  useLayoutEffect(() => {
    if (!open || active < 0 || !pos) return;
    const el = menuRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    if (!el) return;
    setHl({ top: el.offsetTop, height: el.offsetHeight });
    el.scrollIntoView({ block: 'nearest' });
  }, [open, active, pos]);

  useEffect(() => {
    if (hl) hlPlaced.current = true;
  }, [hl]);

  // Поиск по первым буквам, как у нативного списка (буфер сбрасывается через 700мс)
  const findByTyping = (ch: string): number => {
    const now = Date.now();
    const buf = now - typeahead.current.at > 700 ? ch : typeahead.current.text + ch;
    typeahead.current = { text: buf, at: now };
    const q = buf.toLowerCase();
    const start = Math.max(active, 0);
    for (let k = 0; k < options.length; k++) {
      // одна буква — ищем со следующего пункта (повторное нажатие листает), фраза — с текущего
      const i = (start + k + (buf.length === 1 ? 1 : 0)) % options.length;
      if (options[i].label.toLowerCase().startsWith(q)) return i;
    }
    return -1;
  };

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return;
    const last = options.length - 1;
    if (!shown) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) {
        e.preventDefault();
        openMenu();
      }
      return;
    }
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        setActive((i) => Math.min(i + 1, last));
        break;
      case 'ArrowUp':
        e.preventDefault();
        setActive((i) => Math.max(i - 1, 0));
        break;
      case 'Home':
        e.preventDefault();
        setActive(0);
        break;
      case 'End':
        e.preventDefault();
        setActive(last);
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        if (active >= 0) choose(active);
        break;
      case 'Escape':
        e.preventDefault();
        e.stopPropagation(); // Esc закрывает список, а не модалку под ним
        close();
        break;
      case 'Tab':
        close();
        break;
      default:
        if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
          const i = findByTyping(e.key);
          if (i >= 0) setActive(i);
        }
    }
  };

  return (
    <>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        className={'input select-trigger' + (shown ? ' select-trigger--open' : '') + (className ? ` ${className}` : '')}
        style={style}
        title={title ?? current?.label}
        disabled={disabled}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={shown}
        aria-controls={shown ? listId : undefined}
        aria-activedescendant={shown && active >= 0 ? `${listId}-${active}` : undefined}
        onClick={() => (shown ? close() : openMenu())}
        onKeyDown={onKeyDown}
      >
        {/* key — чтобы новое значение въезжало анимацией при каждой смене */}
        <span key={value} className="select-value">{current?.label ?? value}</span>
        <span className="select-chevron">
          <Icon name="chevronDown" size={14} />
        </span>
      </button>
      {open &&
        pos &&
        createPortal(
          <div
            ref={menuRef}
            id={listId}
            className={'select-menu' + (pos.up ? ' select-menu--up' : '') + (closing ? ' select-menu--closing' : '')}
            role="listbox"
            style={{ left: pos.left, minWidth: pos.width, top: pos.top, bottom: pos.bottom, maxHeight: pos.maxHeight }}
          >
            {hl && (
              <div
                className={'select-highlight' + (hlPlaced.current ? '' : ' select-highlight--instant')}
                style={{ transform: `translateY(${hl.top}px)`, height: hl.height }}
                aria-hidden="true"
              />
            )}
            {options.map((o, i) => (
              <div
                key={o.value}
                id={`${listId}-${i}`}
                data-index={i}
                role="option"
                aria-selected={i === selectedIndex}
                className={
                  'select-option' +
                  (i === selectedIndex ? ' select-option--selected' : '') +
                  (i === active ? ' select-option--active' : '')
                }
                // каскад появления: задержка по номеру пункта (ограничена, чтобы длинные списки не тянулись)
                style={{ '--i': Math.min(i, 12) } as CSSProperties}
                onMouseEnter={() => setActive(i)}
                // mousedown, а не click: фокус не уходит с триггера
                onMouseDown={(e) => {
                  e.preventDefault();
                  choose(i);
                }}
              >
                <span className="select-option-label">{o.label}</span>
                {o.hint && <span className="select-option-hint">{o.hint}</span>}
              </div>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
