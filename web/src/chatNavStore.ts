// Сигнал «открыть чат с конкретной персоной»: Home (карточки присутствия
// в STATUS) запрашивает переход, раздел Chat подхватывает и выбирает её.
// Простейший module-store по образцу personaCreateStore.
import { useSyncExternalStore } from 'react';

let pending: string | null = null;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

// Запросить открытие чата с персоной (перед навигацией в раздел chat)
export function requestChatPersona(personaId: string) {
  pending = personaId;
  emit();
}

// Погасить запрос после того, как Chat его обработал
export function consumeChatPersonaRequest() {
  pending = null;
  emit();
}

// Вернуться к странице всех чатов: повторный клик по «Чат» в сайдбаре.
// Счётчик, а не флаг: каждый клик — новый запрос
let overviewRequests = 0;

export function requestChatOverview() {
  overviewRequests += 1;
  emit();
}

export function useChatOverviewRequest(): number {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => overviewRequests,
  );
}

export function useChatPersonaRequest(): string | null {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => pending,
  );
}
