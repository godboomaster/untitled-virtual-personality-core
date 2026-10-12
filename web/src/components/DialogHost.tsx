import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useI18n } from '../i18n';
import { settleDialog, useCurrentDialog } from '../dialogStore';
import { BACK_OVERLAY, useBackHandler } from '../backStack';

/* Показ диалогов из dialogStore (confirmDialog/alertDialog) — HUD-панель
   поверх страницы. Esc и клик мимо — отмена, Enter — кнопка в фокусе.
   У разрушительных действий (danger) и выбора из вариантов (choice) фокус
   сразу на «Отмене», чтобы случайный Enter ничего не удалил и не выбрал. Монтируется один раз в App, рендерится
   порталом в body: #root — свой stacking context (z-index: 1), а модалки
   (FormModal) — порталы в body, изнутри #root диалог оказался бы под ними. */

export default function DialogHost() {
  const { t } = useI18n();
  const dialog = useCurrentDialog();
  const confirmRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useBackHandler(Boolean(dialog), BACK_OVERLAY, () => settleDialog(false));

  useEffect(() => {
    if (!dialog) return;
    // Фокус — после появления панели; прежний фокус вернём при закрытии
    const prev = document.activeElement as HTMLElement | null;
    const cancelFirst = dialog.kind === 'choice' || (dialog.danger && dialog.kind === 'confirm');
    (cancelFirst ? cancelRef : confirmRef).current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopImmediatePropagation(); // Esc закрывает диалог, а не модалку под ним
        settleDialog(false);
      }
    };
    // capture — раньше обработчиков Esc у модалок (FormModal слушает window)
    window.addEventListener('keydown', onKey, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      prev?.focus?.();
    };
  }, [dialog]);

  if (!dialog) return null;
  const isConfirm = dialog.kind === 'confirm';
  const isChoice = dialog.kind === 'choice';

  return createPortal(
    <div className="dialog-overlay" onMouseDown={() => settleDialog(false)}>
      <div
        className={'dialog-panel bracketed' + (dialog.danger ? ' dialog-panel--danger' : '')}
        role={isConfirm || isChoice ? 'alertdialog' : 'dialog'}
        aria-modal="true"
        aria-labelledby="vpc-dialog-title"
        aria-describedby="vpc-dialog-message"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="corner tl" />
        <div className="corner tr" />
        <div className="corner bl" />
        <div className="corner br" />
        <div className="dialog-title" id="vpc-dialog-title">
          <span className="dialog-mark" aria-hidden="true">
            {dialog.danger ? '!' : isConfirm || isChoice ? '?' : 'i'}
          </span>
          {dialog.title ?? (isConfirm || isChoice ? t('dialog.confirmTitle') : t('dialog.alertTitle'))}
        </div>
        <p className="dialog-message" id="vpc-dialog-message">
          {dialog.message}
        </p>
        {isChoice ? (
          // Варианты столбиком (подписи длинные), «Отмена» — последней
          <div className="dialog-actions dialog-actions--choice">
            {(dialog.choices ?? []).map((c) => (
              <button
                key={c.value}
                type="button"
                className={'btn ' + (c.primary ? 'btn--primary' : 'btn--ghost')}
                onClick={() => settleDialog(c.value)}
              >
                {c.label}
              </button>
            ))}
            <button ref={cancelRef} type="button" className="btn btn--ghost" onClick={() => settleDialog(false)}>
              {dialog.cancelLabel ?? t('common.cancel')}
            </button>
          </div>
        ) : (
          <div className="dialog-actions">
            {isConfirm && (
              <button ref={cancelRef} type="button" className="btn btn--ghost" onClick={() => settleDialog(false)}>
                {dialog.cancelLabel ?? t('common.cancel')}
              </button>
            )}
            <button
              ref={confirmRef}
              type="button"
              className={'btn ' + (dialog.danger ? 'btn--danger' : 'btn--primary')}
              onClick={() => settleDialog(true)}
            >
              {dialog.confirmLabel ?? (isConfirm ? t('dialog.confirm') : t('dialog.ok'))}
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
