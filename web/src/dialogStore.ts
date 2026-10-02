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
}

export interface DialogRequest extends DialogOptions {
  kind: 'confirm' | 'alert';
  resolve: (ok: boolean) => void;
}

// Очередь: второй диалог, запрошенный при открытом первом, ждёт своей очереди
let queue: DialogRequest[] = [];
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

function push(kind: DialogRequest['kind'], opts: DialogOptions): Promise<boolean> {
  return new Promise((resolve) => {
    queue = [...queue, { ...opts, kind, resolve }];
    emit();
  });
}

// Подтверждение: true — «да», false — отмена/Esc/клик мимо
export function confirmDialog(opts: DialogOptions): Promise<boolean> {
  return push('confirm', opts);
}

// Сообщение с одной кнопкой; промис резолвится при закрытии
export function alertDialog(opts: DialogOptions): Promise<void> {
  return push('alert', opts).then(() => undefined);
}

// Закрыть текущий диалог с ответом (зовёт DialogHost)
export function settleDialog(ok: boolean) {
  const [head, ...rest] = queue;
  if (!head) return;
  queue = rest;
  emit();
  head.resolve(ok);
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
