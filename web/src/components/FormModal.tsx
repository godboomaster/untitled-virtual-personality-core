import { useEffect } from 'react';
import { createPortal } from 'react-dom';
import type { ReactNode } from 'react';
import { useI18n } from '../i18n';
import { BACK_OVERLAY, useBackHandler } from '../backStack';

/* Общая обёртка модального окна с формой: оверлей, панель в HUD-стиле,
   шапка с заголовком, подвал «Отмена / Создать». Закрытие — крестик,
   клик по оверлею, Esc. Используется модалкой создания персоны
   и маленькими формами добавления (напоминание, to-do). Рендерится порталом
   в body: иначе position: fixed считается от предка с transform (анимация
   раздела, hover карточки) — модалка уезжает вверх, низ страницы не затемнён. */

interface FormModalProps {
  title: string;
  badge?: string; // технический бейдж в шапке (например, PROC_03)
  children: ReactNode; // поля формы
  submitLabel?: string;
  submitDisabled?: boolean;
  wide?: boolean; // широкая панель (~560px) для больших форм
  xl?: boolean; // очень широкая панель (~900px), двухпанельные формы
  onSubmit: () => void;
  onClose: () => void;
}

export default function FormModal({
  title,
  badge,
  children,
  submitLabel,
  submitDisabled,
  wide,
  xl,
  onSubmit,
  onClose,
}: FormModalProps) {
  const { t } = useI18n();
  useBackHandler(true, BACK_OVERLAY, onClose);
  // Закрытие по Esc
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return createPortal(
    <div className="pcreate-overlay" onClick={onClose}>
      <div
        className={`pcreate-panel bracketed ${wide ? 'pcreate-panel--wide' : ''} ${xl ? 'pcreate-panel--xl' : ''}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="corner tl" />
        <div className="corner tr" />
        <div className="corner bl" />
        <div className="corner br" />
        <div className="pcreate-head">
          <span className="pcreate-title">{title}</span>
          {badge && <span className="badge">{badge}</span>}
          <button type="button" className="pxe-close" onClick={onClose} aria-label={t('common.close')}>✕</button>
        </div>
        <div className="pcreate-body">{children}</div>
        <div className="pcreate-foot">
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn btn--primary" disabled={submitDisabled} onClick={onSubmit}>
            {submitLabel ?? t('common.create')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
