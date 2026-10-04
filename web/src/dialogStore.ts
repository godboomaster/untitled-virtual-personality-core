// Диалоги подтверждения/сообщения на странице вместо window.confirm/alert
// (системное окно браузера выбивается из дизайна и блокирует вкладку).
// Промис-API: `if (!(await confirmDialog({...}))) return;`. Показывает их
// DialogHost, смонтированный один раз в App. Module-store по образцу artStore.
import { useSyncExternalStore } from 'react';

export interface DialogOptions {
  title?: string;
  message: string;
  confirmLabel?: string; // по умолчанию «ОК» / «Подтвердить»
  cancelLabel?: string;
  danger?: boolean; // разрушительное действие: кнопка btn--danger, фокус на «Отмене»
  choices?: DialogChoice[]; // варианты choiceDialog
}

// Вариант ответа в choiceDialog
export interface DialogChoice {
  value: string;
  label: string;
  primary?: boolean; // основная кнопка (btn--primary), остальные — btn--ghost
}

export interface DialogRequest extends DialogOptions {
  kind: 'confirm' | 'alert' | 'choice';
  // confirm/alert — true/false; choice — value варианта или false (отмена)
  resolve: (answer: boolean | string) => void;
}

// Очередь: второй диалог, запрошенный при открытом первом, ждёт своей очереди
let queue: DialogRequest[] = [];
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

function push(kind: DialogRequest['kind'], opts: DialogOptions): Promise<boolean | string> {
  return new Promise((resolve) => {
    queue = [...queue, { ...opts, kind, resolve }];
    emit();
  });
}

// Подтверждение: true — «да», false — отмена/Esc/клик мимо
export function confirmDialog(opts: DialogOptions): Promise<boolean> {
  return push('confirm', opts).then((a) => a === true);
}

// Сообщение с одной кнопкой; промис резолвится при закрытии
export function alertDialog(opts: DialogOptions): Promise<void> {
  return push('alert', opts).then(() => undefined);
}

// Выбор из нескольких вариантов (+ «Отмена»), когда «да/нет» мало:
// value выбранного варианта, null — отмена/Esc/клик мимо
export function choiceDialog(opts: DialogOptions & { choices: DialogChoice[] }): Promise<string | null> {
  return push('choice', opts).then((a) => (typeof a === 'string' ? a : null));
}

// Закрыть текущий диалог с ответом (зовёт DialogHost)
export function settleDialog(answer: boolean | string) {
  const [head, ...rest] = queue;
  if (!head) return;
  queue = rest;
  emit();
  head.resolve(answer);
}

export function useCurrentDialog(): DialogRequest | null {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => queue[0] ?? null,
  );
}
