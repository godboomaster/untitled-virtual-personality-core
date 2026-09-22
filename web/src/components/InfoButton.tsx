import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import type { HelpKey } from '../helpTexts';
import { useHelpTexts, useI18n } from '../i18n';

interface InfoButtonProps {
  helpKey: HelpKey;
}

// Круглая контурная кнопка «?» — открывает модалку с пояснением.
// Закрытие: клик вне карточки, Esc, крестик или повторный клик по «?».
export default function InfoButton({ helpKey }: InfoButtonProps) {
  const [open, setOpen] = useState(false);
  const { t, hintsEnabled } = useI18n();
  const help = useHelpTexts()[helpKey];

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open]);

  // Подсказки выключены в общих настройках — кнопка «?» не рендерится
  if (!hintsEnabled) return null;

  return (
    <>
      <button
        type="button"
        className="info-btn"
        aria-label={t('info.explain', { title: help.title })}
        title={help.title}
        onClick={(e) => {
          // Кнопка часто живёт внутри <label> — не даём клику переключить чекбокс/радио.
          e.preventDefault();
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        ?
      </button>
      {open &&
        createPortal(
          <div className="info-overlay" onClick={() => setOpen(false)}>
            <div
              className="info-modal"
              role="dialog"
              aria-modal="true"
              aria-label={help.title}
              onClick={(e) => e.stopPropagation()}
            >
              <button
                type="button"
                className="info-modal-close"
                aria-label={t('common.close')}
                onClick={() => setOpen(false)}
              >
                ✕
              </button>
              <h3 className="info-modal-title">{help.title}</h3>
              <p className="info-modal-body">{help.body}</p>
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
