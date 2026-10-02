// Сигнал «открыть создание персоны»: Home запрашивает (CTA и панель
// быстрого создания), раздел Personas подхватывает и открывает модалку
// с предзаполнением. Простейший module-store по образцу artStore.
import { useSyncExternalStore } from 'react';

export interface PersonaCreatePrefill {
  name?: string;
}

let pending: PersonaCreatePrefill | null = null;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

// Запросить открытие модалки создания (с необязательным предзаполнением)
export function requestPersonaCreate(prefill: PersonaCreatePrefill = {}) {
  pending = prefill;
  emit();
}

// Погасить запрос после того, как Personas его обработал
export function consumePersonaCreateRequest() {
  pending = null;
  emit();
}

export function usePersonaCreateRequest(): PersonaCreatePrefill | null {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => pending,
  );
}
