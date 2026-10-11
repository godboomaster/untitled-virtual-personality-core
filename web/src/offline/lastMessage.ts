import { getKit } from './kit';
import { offlineMessages } from './store';

/* Последняя реплика по копии переписки на телефоне — хвост с ноутбука (кит)
   и написанное без него (store.ts): без связи с ядром «Главная» и список
   чатов показывают её, а не прочерк и «сообщений пока нет». Кит читается
   из localStorage целиком — звать не на каждый рендер (useMemo). */

export interface LocalLast {
  last: { text: string; role: 'user' | 'assistant'; ts: number } | null;
  lastUserTs: number | null;
}

export function localLast(persona: string): LocalLast {
  let last: LocalLast['last'] = null;
  let lastUserTs: number | null = null;
  for (const m of [...(getKit(persona)?.history ?? []), ...offlineMessages(persona)]) {
    if (!last || m.ts >= last.ts) last = { text: m.text, role: m.role, ts: m.ts };
    if (m.role === 'user' && (lastUserTs == null || m.ts > lastUserTs)) lastUserTs = m.ts;
  }
  return { last, lastUserTs };
}
