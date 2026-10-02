import { useState, type ReactNode } from 'react';

/* Разворачивающийся блок внутри карточки: заголовок-кнопка со стрелкой и
   короткой сводкой, по клику содержимое раскрывается вниз на месте (не
   всплывающее окно). Открыт ли блок — запоминается в браузере по storageKey
   (удобство одного зрителя; хранилище недоступно — просто стартуем свёрнутым). */

interface CollapsibleProps {
  title: ReactNode;
  summary?: ReactNode; // сводка в свёрнутом заголовке (что внутри)
  headExtra?: ReactNode; // рядом с заголовком, вне кнопки (InfoButton)
  storageKey?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}

function readOpen(key: string | undefined, fallback: boolean): boolean {
  if (!key) return fallback;
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : v === '1';
  } catch {
    return fallback;
  }
}

export default function Collapsible({ title, summary, headExtra, storageKey, defaultOpen = false, children }: CollapsibleProps) {
  const [open, setOpen] = useState(() => readOpen(storageKey, defaultOpen));
  const toggle = () => {
    setOpen((v) => {
      const next = !v;
      if (storageKey) {
        try {
          localStorage.setItem(storageKey, next ? '1' : '0');
        } catch {
          /* приватный режим / заблокированное хранилище — только в памяти */
        }
      }
      return next;
    });
  };
  return (
    <div className={`collapsible ${open ? 'is-open' : ''}`}>
      <div className="collapsible-head-row">
        <button type="button" className="collapsible-head" aria-expanded={open} onClick={toggle}>
          <span className="collapsible-chevron" aria-hidden="true">
            ▸
          </span>
          <span className="collapsible-title">{title}</span>
          {summary && <span className="collapsible-summary">{summary}</span>}
        </button>
        {headExtra}
      </div>
      {/* grid 0fr→1fr: плавное раскрытие на высоту содержимого; свёрнутое
          содержимое inert — не ловит фокус и клики */}
      <div className="collapsible-body" inert={!open}>
        <div className="collapsible-inner">{children}</div>
      </div>
    </div>
  );
}
