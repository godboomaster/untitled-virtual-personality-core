import { useEffect } from 'react';
import { api } from './api';

/* Сообщаем бэкенду, чей чат сейчас открыт и активна ли вкладка (видима и в
   фокусе) — пока активна, бот не делает побочных дел ПО ЭТОМУ чату
   (самоинициатива, извлечение фактов и т.п.). Мгновенный сигнал на смену
   состояния + heartbeat через поллинг инбокса (параметр focused в
   useInboxPolling); на бэке отметка живёт по TTL и ключуется парой
   (персона, чат) — см. app/core/presence.py.

   Раньше сигнал был безымянный («вкладка активна»), и одна открытая вкладка
   морозила фон ВСЕХ персон и всех чатов, включая Telegram-чаты. Поэтому
   отчёт живёт в экране чата, где известна выбранная персона, а не в App. */

export const tabActive = () => !document.hidden && document.hasFocus();

/* Персона открытого чата — нужна поллеру инбоксов: heartbeat focused=1
   отправляется только для неё, иначе он отмечал бы присутствие сразу у всех
   персон (поллер опрашивает всех). null — чат не открыт вообще. */
let focusedPersona: string | null = null;
export const getFocusedPersona = () => focusedPersona;

export function usePresenceReporting(apiOnline: boolean, persona: string | null) {
  useEffect(() => {
    if (!apiOnline || !persona) return;
    focusedPersona = persona;
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
      if (focusedPersona === persona) focusedPersona = null;
      // Переключили персону/чат или ушли с экрана чата — снимаем отметку у
      // СТАРОГО ключа сразу, не дожидаясь TTL: иначе фон той персоны стоял бы
      // ещё до минуты после того, как её чат закрыли
      api.setPresence(false, persona).catch(() => {});
    };
  }, [apiOnline, persona]);
}
