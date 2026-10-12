import type { OfflineKit } from './kit';
import type { ChatMsg } from './plugin';
import { recallNote } from './recall';
import type { RecallDoc } from './recall';

/* Промпт ответа без ноутбука из кита и памяти — общие части облака и модели
   телефона. Без зависимостей приложения: scripts/test_offline_recall.py
   проверяет бюджет промпта под Node. */

// Модель телефона: окно 4096 токенов на промпт и ответ (до 400) — промпт
// не длиннее LOCAL_PROMPT_CHARS (≈ 3 символа русского текста на токен);
// не влезает — короче характер, личная память, факты, потом история.
// История короткая: длинную маленькая модель не держит, а свои прежние
// ответы из неё копирует слово в слово
export const LOCAL_PROMPT_CHARS = 9000;
export const LOCAL_HISTORY_MAX = 8;
const LOCAL_HISTORY_MIN = 2;
const LOCAL_LINE_MAX = 600; // символов одной реплики истории
const LOCAL_HISTORY_ROOM = 1500; // меньше места истории — ужать промпт
const LOCAL_LEVELS = [
  { character: 4000, self: 1200, facts: 25, recall: 3 },
  { character: 3000, self: 600, facts: 15, recall: 2 },
  { character: 2000, self: 0, facts: 8, recall: 2 },
];

// Правила модели телефона — здесь, а не в ките: ими пользуется только она,
// и правка приходит с приложением, без запуска ядра. Маленькая модель
// понимает всё буквально: «your tools are unavailable» превращалось в «у меня
// нет доступа к инструментам» в ответ на «Привет», свои прежние ответы она
// копировала слово в слово — поэтому коротко, об инструментах только по
// просьбе. Запрета на капс нет: Gemma поняла его как «без заглавных вообще»
// («я коннор, rk800»). Язык действий — отдельно: примеры в описании персоны на
// её языке, и в английском ответе оставалось «*Наклоняю голову.*». Копия для
// прежних сборок — OFFLINE_RULES_LOCAL ядра (kit.parts.rules_local)
export const LOCAL_RULES =
  "Reply directly to the user's latest message, in character, briefly and conversationally. " +
  "Don't repeat your earlier replies word for word. Write everything in the reply's language, " +
  'including actions in asterisks. Reminders are handled by the app: confirm one only when a note ' +
  "from the app says it was set. If asked to do something on the computer (open sites, to-dos, " +
  "inventory), say you'll do it once you're back on the computer. Don't invent facts about the user.";

export interface HistoryLine {
  role: 'user' | 'assistant';
  text: string;
  ts?: number; // unix-секунды — свежая ли реплика без ответа (withoutStaleUnanswered)
}

// Реплики пользователя без ответа в конце истории идут вместе с новой: это
// очередь без связи, модель отвечает на все. Но только свежие — старая
// («открой телеграм» час назад, ответ на которую не попал в историю) уводила
// ответ в неё: на «мне грустно» — «Открываю приложение телеграм»
export const STALE_UNANSWERED_SEC = 30 * 60;

/** История без старых реплик пользователя в хвосте (без ответа); реплика без
 * времени остаётся — не знаем, старая ли. */
export function withoutStaleUnanswered(lines: HistoryLine[], nowSec = Date.now() / 1000): HistoryLine[] {
  let start = lines.length;
  while (start > 0 && lines[start - 1].role === 'user') start--;
  if (start === lines.length) return lines;
  const tail = lines.slice(start).filter((l) => l.ts == null || nowSec - l.ts <= STALE_UNANSWERED_SEC);
  return tail.length === lines.length - start ? lines : [...lines.slice(0, start), ...tail];
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

/** Текст не длиннее max — по абзацу или строке, если так теряется не больше
 * половины, иначе по символу. */
function cutText(text: string, max: number): string {
  if (text.length <= max) return text;
  const cut = text.slice(0, max);
  const para = cut.lastIndexOf('\n\n');
  if (para >= max / 2) return cut.slice(0, para);
  const line = cut.lastIndexOf('\n');
  return line >= max / 2 ? cut.slice(0, line) : cut;
}

// Строка-разделитель промпта (────, ====)
const SEPARATOR_RE = /^[\s\-─━═=_*~#]+$/;
// Слово капсом от 4 букв (ХАРАКТЕР, НЕЛЬЗЯ): заголовки разделов промпта
const CAPS_WORD_RE = /(?<![\p{L}\p{N}])\p{Lu}{4,}(?![\p{L}\p{N}])/gu;

/** Полный характер для модели телефона, пока краткого описания нет: без
 * разделителей и заголовков капсом — маленькая модель подхватывает капс, а
 * русский капс у неё рассыпается на опечатки («ТАКТИВАЦИЯ»). */
export function plainCharacter(text: string): string {
  return text
    .split('\n')
    .filter((l) => !SEPARATOR_RE.test(l))
    .map((l) => {
      // Заголовок целиком капсом («РЕЧЬ И ГОЛОС») — как предложение
      if (/\p{Lu}{2}/u.test(l) && !/\p{Ll}/u.test(l)) return l.toLowerCase().replace(/\p{L}/u, (c) => c.toUpperCase());
      return l.replace(CAPS_WORD_RE, (w, at: number) => (l.slice(0, at).trim() ? w.toLowerCase() : w[0] + w.slice(1).toLowerCase()));
    })
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** Характер для модели телефона: краткое описание (составлено на ноутбуке
 * или задано в YAML), нет его — полный характер без капса, по абзацам до max. */
export function localCharacter(kit: OfflineKit, max: number = LOCAL_LEVELS[0].character): string {
  const short = kit.parts.short?.trim();
  return cutText(short || plainCharacter(kit.parts.character), max);
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
  language?: string, // строка языка по текущей реплике (language.ts); нет — из кита
  nowSec = Date.now() / 1000,
): { system: string; history: ChatMsg[]; text: string } {
  const p = kit.parts;
  history = withoutStaleUnanswered(history, nowSec);
  let system = '';
  let userText = text;
  for (const [i, lv] of LOCAL_LEVELS.entries()) {
    const recall = recallNote(memory.recall.slice(0, lv.recall));
    userText = recall ? `${recall}\n\n${text}` : text;
    system = [
      localCharacter(kit, lv.character),
      LOCAL_RULES,
      p.style,
      localFacts(kit, memory, lv.facts),
      kit.dossier ?? '',
      selfMemoryNote(kit, lv.self),
      ...notes,
      language ?? p.language,
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
