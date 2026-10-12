import type { OfflineHistoryLine } from '../api';
import { getKit } from './kit';
import { getMemory, memoryHistory } from './memory';
import { offlineMessages } from './store';

/* «Память» в досье без ноутбука: копия с телефона — вся переписка (память с
   ноутбука, свежий хвост кита и реплики без ноутбука), факты и личная
   память персоны (дневник) из кита. Только посмотреть: правит её ядро. */

export interface PhoneMemoryView {
  history: OfflineHistoryLine[];
  facts: string[];
  diary: { date: string; text: string }[];
}

export async function phoneMemoryView(persona: string): Promise<PhoneMemoryView> {
  const kit = getKit(persona);
  const mem = kit ? await getMemory(persona) : null;
  const seen = new Set<string>();
  const history: OfflineHistoryLine[] = [];
  const add = (m: OfflineHistoryLine) => {
    const key = `${m.role}|${m.ts.toFixed(3)}|${m.text}`;
    if (!m.text.trim() || seen.has(key)) return;
    seen.add(key);
    history.push(m);
  };
  if (kit) {
    for (const m of memoryHistory(kit, mem)) add(m);
    for (const m of kit.history) add(m);
  }
  for (const m of offlineMessages(persona)) add({ role: m.role, text: m.text, ts: m.ts });
  history.sort((a, b) => a.ts - b.ts);

  const facts = mem && kit && kit.version >= 3 && mem.facts.length ? mem.facts : (kit?.facts ?? []);

  // Блок личной памяти ядра: «Story: …» — сводка, «- [вчера] …» — эпизоды и наблюдения
  const diary: { date: string; text: string }[] = [];
  for (const raw of (kit?.self_memory ?? '').split('\n')) {
    const line = raw.trim();
    const story = /^Story:\s*(.+)$/.exec(line);
    if (story) {
      diary.unshift({ date: 'Σ', text: story[1] });
      continue;
    }
    const item = /^-\s*(?:\[([^\]]+)\]\s*)?(.+)$/.exec(line);
    if (item) diary.push({ date: item[1] ?? '', text: item[2] });
  }
  return { history, facts, diary };
}
