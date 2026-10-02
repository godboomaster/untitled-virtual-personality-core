import { useEffect } from 'react';
import { api } from './api';

// Сообщаем бэкенду, чей чат сейчас открыт и активна ли вкладка (видима и в
// фокусе) — пока активна, бот не делает побочных дел ПО ЭТОМУ чату
// (самоинициатива, извлечение фактов и т.п.). Мгновенный сигнал на смену
// состояния + heartbeat через поллинг инбокса (параметр focused в
// useInboxPolling); на бэке отметка живёт по TTL и ключуется парой
// (персона, чат) — см. app/core/presence.py.
//
// Отчёт живёт в экране чата, а не в App, потому что там известна выбранная
// персона: иначе одна открытая вкладка морозила бы фон всех персон и всех
// чатов, включая чаты вне веб-интерфейса.

export const tabActive = () => !document.hidden && document.hasFocus();

// Персона открытого чата — нужна поллеру инбоксов: heartbeat focused=1
// отправляется только для неё, иначе он отмечал бы присутствие сразу у всех
// персон (поллер опрашивает всех). null — чат не открыт вообще.
let focusedPersona: string | null = null;
export const getFocusedPersona = () => focusedPersona;

// Подписка на смену персоны открытого чата (комната показывает позу
// «с тобой», пока чат этой персоны открыт) — без поллинга
const focusListeners = new Set<() => void>();
export function onFocusedPersonaChange(cb: () => void): () => void {
  focusListeners.add(cb);
  return () => {
    focusListeners.delete(cb);
  };
}
function setFocusedPersona(next: string | null) {
  if (focusedPersona === next) return;
  focusedPersona = next;
  focusListeners.forEach((l) => l());
}

export function usePresenceReporting(apiOnline: boolean, persona: string | null) {
  useEffect(() => {
    if (!apiOnline || !persona) return;
    setFocusedPersona(persona);
    const report = () => {
      api.setPresence(tabActive(), persona).catch(() => {});
    };
    report();
    document.addEventListener('visibilitychange', report);
    window.addEventListener('focus', report);
    window.addEventListener('blur', report);
    return () => {
      document.removeEventListener('visibilitychange', report);
      window.removeEventListener('focus', report);
      window.removeEventListener('blur', report);
      if (focusedPersona === persona) setFocusedPersona(null);
      // Переключили персону/чат или ушли с экрана чата — снимаем отметку у
      // СТАРОГО ключа сразу, не дожидаясь TTL: иначе фон той персоны стоял бы
      // ещё до минуты после того, как её чат закрыли
      api.setPresence(false, persona).catch(() => {});
    };
  }, [apiOnline, persona]);
}
