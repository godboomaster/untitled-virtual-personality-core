import type { OfflineKit } from './kit';
import type { ChatMsg } from './plugin';
import { recallNote } from './recall';
import type { RecallDoc } from './recall';

/* Промпт ответа без ноутбука из кита и памяти — общие части облака и модели
   телефона. Без зависимостей приложения: scripts/test_offline_recall.py
   проверяет бюджет промпта под Node. */

// Модель телефона: окно 4096 токенов на промпт и ответ (до 400) — промпт
// не длиннее LOCAL_PROMPT_CHARS (≈ 3 символа русского текста на токен);
// не влезает — короче характер, личная память, факты, потом история
export const LOCAL_PROMPT_CHARS = 9000;
export const LOCAL_HISTORY_MAX = 16;
const LOCAL_HISTORY_MIN = 2;
const LOCAL_LINE_MAX = 1200; // символов одной реплики истории
const LOCAL_HISTORY_ROOM = 1500; // меньше места истории — ужать промпт
const LOCAL_LEVELS = [
  { character: 4000, self: 1200, facts: 25, recall: 3 },
  { character: 3000, self: 600, facts: 15, recall: 2 },
  { character: 2000, self: 0, facts: 8, recall: 2 },
];

export interface HistoryLine {
  role: 'user' | 'assistant';
  text: string;
}

/** Подряд идущие реплики одной стороны — одной (часть API требует чередования),
 * в начале — с реплики пользователя. */
export function normalizeHistory(lines: HistoryLine[]): ChatMsg[] {
  const out: ChatMsg[] = [];
  for (const l of lines) {
    const text = l.text.trim();
    if (!text) continue;
    const last = out[out.length - 1];
    if (last && last.role === l.role) last.content += `\n\n${text}`;
    else out.push({ role: l.role, content: text });
  }
  while (out.length && out[0].role !== 'user') out.shift();
  return out;
}

/** Рассуждения моделей («<think>…</think>») пользователю не показываем. */
export function cleanAnswer(text: string): string {
  return text.replace(/<think>[\s\S]*?<\/think>/gi, '').replace(/^[\s\S]*<\/think>/i, '').trim();
}

/** Личная память персоны (блок ядра) с правилом; max — символов блока. */
export function selfMemoryNote(kit: OfflineKit, max = Infinity): string {
  let block = (kit.self_memory ?? '').trim();
  if (!block || max <= 0) return '';
  const end = '[END OF PERSONAL MEMORY]';
  if (block.length > max) {
    // По границе строки, если так теряется не больше половины
    let cut = block.slice(0, max);
    const nl = cut.lastIndexOf('\n');
    cut = nl >= max / 2 ? cut.slice(0, nl) : `${cut}…`;
    block = `${cut}\n${end}`;
  }
  return [block, kit.rules?.self_memory ?? ''].filter(Boolean).join('\n\n');
}

export interface MemoryContext {
  recall: RecallDoc[]; // реплики постарше к теме вопроса, лучшие сверху
  facts: RecallDoc[]; // факты к теме вопроса
}

/** Факты для модели телефона: к теме вопроса, потом последние — не больше max. */
function localFacts(kit: OfflineKit, memory: MemoryContext, max: number): string {
  const list = [...new Set([...memory.facts.map((f) => f.text), ...kit.facts.slice().reverse()])].slice(0, max);
  return list.length ? `What you know about the user:\n${list.map((f) => `- ${f}`).join('\n')}` : '';
}

/** Промпт для модели телефона: короче облачного — по бюджету символов. */
export function localPrompt(
  kit: OfflineKit,
  memory: MemoryContext,
  notes: string[], // напоминания, окружение, выдача поиска, заметка о просьбе
  history: HistoryLine[],
  text: string,
): { system: string; history: ChatMsg[]; text: string } {
  const p = kit.parts;
  let system = '';
  let userText = text;
  for (const [i, lv] of LOCAL_LEVELS.entries()) {
    const recall = recallNote(memory.recall.slice(0, lv.recall));
    userText = recall ? `${recall}\n\n${text}` : text;
    system = [
      p.character.slice(0, lv.character),
      p.rules,
      p.style,
      localFacts(kit, memory, lv.facts),
      kit.dossier ?? '',
      selfMemoryNote(kit, lv.self),
      ...notes,
      p.language,
    ]
      .filter(Boolean)
      .join('\n\n');
    if (LOCAL_PROMPT_CHARS - system.length - userText.length >= LOCAL_HISTORY_ROOM || i === LOCAL_LEVELS.length - 1) break;
  }
  // История — с конца, пока влезает (но не меньше двух реплик)
  let room = LOCAL_PROMPT_CHARS - system.length - userText.length;
  const picked: HistoryLine[] = [];
  for (const h of history.slice(-LOCAL_HISTORY_MAX).reverse()) {
    const line = { role: h.role, text: h.text.length > LOCAL_LINE_MAX ? `${h.text.slice(0, LOCAL_LINE_MAX)}…` : h.text };
    if (line.text.length > room && picked.length >= LOCAL_HISTORY_MIN) break;
    room -= line.text.length;
    picked.unshift(line);
  }
  // История кончается репликой пользователя (прежние без ответа) — она идёт
  // вместе с новой одной репликой: две подряд модель телефона путают
  const hist = normalizeHistory(picked);
  let prompt = userText;
  if (hist.length && hist[hist.length - 1].role === 'user') prompt = `${hist.pop()!.content}\n\n${userText}`;
  return { system, history: hist, text: prompt };
}
