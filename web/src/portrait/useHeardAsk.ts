import { useEffect, useState } from 'react';
import { ApiError, api } from '../api';

/* Пауза до ответа для вынесенного портрета (PiP): лента реплик ядра
   (/api/said, как у портрета на рабочем столе) — вы написали персоне
   откуда угодно (чат веба, телефон, поле портрета на столе): heard с
   текстом сообщения — облачко сбоку, кивок, «читает», потом думает; ответ
   или конец хода (settled) облачко убирает. Опрос — только пока портрет
   открыт. */

export interface HeardAsk {
  id: number;
  text: string;
}

const WAIT_SEC = 25;
const RETRY_MS = 5000;
// Услышала, а конца хода нет (ядро перезапустили посреди ответа) — не дольше
const HEARD_MS = 3 * 60_000;

export function useHeardAsk(personaId: string, view: Window) {
  const [ask, setAsk] = useState<HeardAsk | null>(null);
  const [heard, setHeard] = useState(false);
  const [nod, setNod] = useState(0);

  useEffect(() => {
    let stop = false;
    let ids = 0;
    let heardTimer = 0;
    const abort = new AbortController();
    const settle = () => {
      view.clearTimeout(heardTimer);
      setHeard(false);
      setAsk(null);
    };
    void (async () => {
      let since: number | null = null;
      let epoch = '';
      while (!stop) {
        try {
          const r = await api.waitSaid(since, epoch, WAIT_SEC, abort.signal);
          if (stop) return;
          // Первый ответ — только номер; номер другого запуска ядра — ядро
          // само не повторяет устаревшие события паузы
          if (since != null) {
            for (const m of r.messages) {
              if (m.persona !== personaId) continue;
              if (m.kind === 'heard') {
                setNod((n) => n + 1);
                setHeard(true);
                const text = m.text.trim();
                setAsk(text ? { id: ++ids, text } : null);
                view.clearTimeout(heardTimer);
                heardTimer = view.setTimeout(settle, HEARD_MS);
              } else if (m.kind === 'settled') settle();
              // Персона ответила — ваше сообщение больше не держим
              else if (m.text.trim()) setAsk(null);
            }
          }
          since = r.cursor;
          epoch = r.epoch;
        } catch (e) {
          if (stop || (e instanceof ApiError && e.status === 404)) return;
          await new Promise((res) => view.setTimeout(res, RETRY_MS));
        }
      }
    })();
    return () => {
      stop = true;
      abort.abort();
      view.clearTimeout(heardTimer);
      setAsk(null);
      setHeard(false);
    };
  }, [personaId, view]);

  return { ask, heard, nod };
}
