import { fallbackKit, getKit } from './kit';
import type { OfflineKit } from './kit';
import { OfflineLlm, pluginCode } from './plugin';
import { envNote, formatWeb, searchWeb, wantsSearch } from './context';
import { askTimePlan, extractSystem, looksLikeReminder, parseExtraction, planFromText, planReminder, scheduledNote } from './reminders';
import type { LocalReminder, ReminderPlan } from './reminders';
import { getMemory, memoryHasVectors, memoryHistory } from './memory';
import { formatRecall, searchDocs, vecBaseline } from './recall';
import { queryVector, releaseEmbedder } from './embed';
import { unpackVec } from './vectors';
import type { RecallDoc } from './recall';
import { cleanAnswer, LOCAL_HISTORY_MAX, localPrompt, normalizeHistory, selfMemoryNote } from './prompt';
import type { HistoryLine, MemoryContext } from './prompt';
import { dialogueLanguage, languageLine, languageName } from './language';

/* Кто отвечает без ноутбука: сначала облачная модель по ключу (если есть
   интернет и ключи), иначе — Gemma на телефоне (если модель скачана). Нет
   ни того ни другого — OfflineUnavailable: реплика ждёт связи в очереди.
   К промпту — как у ядра: местные время и погода, выдача поиска (если гейт
   пропускает), напоминания; просьбу «напомни» разбирает перенос парсера
   ядра, необычную — модель в JSON, код проверяет и ставит будильник
   (reminders.ts). Память — как в ответе ядра: хвост переписки, реплики
   постарше к теме вопроса (memory.ts; по словам и по смыслу — embed.ts),
   факты, досье, личная память персоны. */

export interface OfflineAnswer {
  text: string;
  source: 'cloud' | 'local';
  provider?: string;
  reminder?: LocalReminder; // поставлено по этой реплике
  fallback?: boolean; // кита с ноутбука нет — ответ по имени и описанию персоны
}

// no_kit — данных персоны на телефоне нет; no_route — ни облака, ни модели
// (cloud: почему не облако — no_providers, offline, failed); model_broken —
// файл модели испорчен и удалён
export class OfflineUnavailable extends Error {
  reason: 'no_kit' | 'no_route' | 'model_broken';
  cloud: string;
  constructor(reason: 'no_kit' | 'no_route' | 'model_broken', cloud = '') {
    super(reason);
    this.reason = reason;
    this.cloud = cloud;
  }
}

const CLOUD_HISTORY = 24; // реплик переписки в запросе к облаку
const CLOUD_RECALL = 5; // реплик постарше к теме вопроса (как у ядра)
const CLOUD_FACTS_RELEVANT = 10; // фактов к теме сверх тех, что в ките

/** Память к ответу: реплики старше окна истории и факты — к теме вопроса. */
async function gatherMemory(persona: string, kit: OfflineKit, text: string, history: HistoryLine[], window: number): Promise<MemoryContext> {
  const mem = await getMemory(persona).catch(() => null);
  const recent = new Set(history.slice(-window).map((h) => h.text.trim()));
  recent.add(text.trim());
  const seen = new Set<string>();
  const older: RecallDoc[] = [];
  const vectors = memoryHasVectors(mem);
  const add = (role: 'user' | 'assistant', raw: string, ts?: number, vec?: string) => {
    const t = raw.trim();
    if (!t || recent.has(t) || seen.has(t)) return;
    seen.add(t);
    older.push({ role, text: t, ts, vec: vectors ? unpackVec(vec) : null });
  };
  for (const m of memoryHistory(kit, mem)) add(m.role, m.text, m.ts, m.vec);
  for (const h of history.slice(0, -window)) add(h.role, h.text);
  const fromMemory = !!mem && kit.version >= 3 && mem.facts.length > 0;
  const facts: RecallDoc[] = (fromMemory ? mem.facts : kit.facts).map((f, i) => ({
    role: 'fact' as const,
    text: f,
    vec: fromMemory && vectors ? unpackVec(mem.facts_vec?.[i]) : null,
  }));
  // По смыслу — если модель векторов скачана (нет — только по словам); фон
  // сходства — по всей памяти сразу: фактов одних для него мало
  const all = [...older, ...facts];
  const qvec = await queryVector(text, all).catch(() => null);
  const base = qvec ? vecBaseline(qvec, all) : null;
  return {
    recall: searchDocs(text, older, CLOUD_RECALL, qvec, base),
    facts: searchDocs(text, facts, CLOUD_FACTS_RELEVANT, qvec, base),
  };
}

// Вопрос о своём («моя кошка», «my dog»): интернет о нём не знает, а
// маленькая модель телефона, получив выдачу, пересказывает её вместо памяти
const PERSONAL_RE = /(?<![\p{L}\p{N}_])(?:мо[йяеёи]|мою|моего|моей|моему|моим|моих|моими|my|mine)(?![\p{L}\p{N}_])/iu;

function newRequestId(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

const CLOUD_RESULTS = { max: 5, snippet: 500 };
const LOCAL_RESULTS = { max: 3, snippet: 300 }; // телефону — короче промпт
// Генерация на телефоне: ниже обычной температура — маленькая модель при
// 0.8 сыпала опечатками по-русски, особенно капсом
const LOCAL_SAMPLING = { temperature: 0.6, topP: 0.9 };

/** Системный текст кита (облако) со строкой языка по текущей реплике
 * вместо строки с момента загрузки кита. */
function withLanguage(kit: OfflineKit, line: string): string {
  const old = kit.parts.language;
  if (old && kit.system.includes(old)) return kit.system.replace(old, line);
  return `${kit.system}\n\n${line}`;
}

/** Ответ модели телефона целиком (без вывода в ленту) — для разбора просьбы. */
async function localOnce(system: string, text: string, maxTokens: number): Promise<string> {
  const r = await OfflineLlm.localChat({ requestId: newRequestId(), system, history: [], text, maxTokens, temperature: 0.1 });
  return r.text;
}

export async function answerOffline(
  persona: string,
  text: string,
  history: HistoryLine[],
  onToken?: (chunk: string) => void,
  onLocalStart?: () => void, // ответ взяла модель телефона (до первого слова — секунды)
  actionNote?: string, // итог команды телефону (phone/notes.ts): ответ — по нему
): Promise<OfflineAnswer> {
  const kit = getKit(persona) ?? fallbackKit(persona, text);
  if (!kit) throw new OfflineUnavailable('no_kit');
  const fallback = !!kit.fallback;

  // Кит старого ядра (до версии 2) или запасной — без поиска, погоды и напоминаний
  const full = kit.version >= 2 && !!kit.rules;
  // Напоминание: разобрано без модели — заметка готова; 'model' — время
  // разберёт модель (облако, иначе телефон)
  let plan: ReminderPlan | undefined;
  let needModel = false;
  if (full && kit.reminders_enabled) {
    const r = planFromText(kit, text);
    if (r === 'model') needModel = true;
    else if (r) plan = r;
  }
  // Просьба о напоминании или команда телефону — не вопрос к интернету
  const reminderTalk = needModel || !!plan || looksLikeReminder(text) || !!actionNote;
  // Поиск, погода и память — общие для облака и телефона; без интернета — пусто
  const [env, results, memory] = await Promise.all([
    full ? envNote(kit) : Promise.resolve(''),
    full && !reminderTalk && wantsSearch(kit, text) ? searchWeb(text, CLOUD_RESULTS.max) : Promise.resolve([]),
    gatherMemory(persona, kit, text, history, LOCAL_HISTORY_MAX),
  ]);
  const scheduled = full ? scheduledNote(kit) : '';
  // Язык ответа — по текущей реплике (как у ядра), а не язык чата из кита
  const lang = dialogueLanguage(text, history, kit.lang);
  const langLine = languageLine(lang);

  // 1. Облако: ключи и перебор провайдеров — в нативном плагине
  let cloud = '';
  try {
    if (needModel && !plan) {
      const raw = await OfflineLlm.cloudChat({
        persona,
        messages: [{ role: 'system', content: extractSystem(kit) }, { role: 'user', content: text }],
        maxTokens: 150,
        temperature: 0,
      });
      const ex = parseExtraction(raw.text);
      plan = ex ? planReminder(kit, ex) : askTimePlan(kit, '');
    }
    const known = new Set(kit.facts);
    const moreFacts = memory.facts.filter((f) => !known.has(f.text));
    const system = [
      withLanguage(kit, langLine),
      moreFacts.length ? `More facts about the user:\n${moreFacts.map((f) => `- ${f.text}`).join('\n')}` : '',
      kit.dossier ?? '',
      selfMemoryNote(kit),
      formatRecall(kit, memory.recall),
      scheduled,
      env,
      formatWeb(kit, results, CLOUD_RESULTS),
      plan?.note,
      actionNote,
    ]
      .filter(Boolean)
      .join('\n\n');
    const r = await OfflineLlm.cloudChat({
      persona,
      messages: [{ role: 'system', content: system }, ...normalizeHistory([...history.slice(-CLOUD_HISTORY), { role: 'user', text }])],
    });
    const answer = cleanAnswer(r.text);
    if (answer) return { text: answer, source: 'cloud', provider: r.provider, reminder: plan?.reminder, fallback };
  } catch (e) {
    // нет ключей, нет интернета или провайдеры не ответили — к модели телефона
    cloud = pluginCode(e);
  }

  // 2. Модель на телефоне — без модели векторов в памяти: вектор вопроса уже
  // посчитан, а Gemma нужна вся память, что есть
  const status = await OfflineLlm.modelStatus().catch(() => null);
  if (!status?.downloaded) throw new OfflineUnavailable('no_route', cloud);
  releaseEmbedder();
  const requestId = newRequestId();
  onLocalStart?.();
  const handle = await OfflineLlm.addListener('localToken', (d) => {
    if (d.requestId === requestId) onToken?.(d.text);
  });
  try {
    // Просьбу о напоминании облако разобрать не успело — разбирает телефон
    if (needModel && !plan) {
      const ex = parseExtraction(await localOnce(extractSystem(kit), text, 120));
      plan = ex ? planReminder(kit, ex) : askTimePlan(kit, '');
    }
    const web = PERSONAL_RE.test(text) ? '' : formatWeb(kit, results, LOCAL_RESULTS);
    const notes = [scheduled, env, web, plan?.note, actionNote].filter((x): x is string => !!x);
    const local = localPrompt(kit, memory, notes, history, text, langLine);
    // Заметки в конце длинного промпта маленькая модель пропускает: о
    // напоминании и об итоге команды телефону «подтверждает» по прежним
    // ответам, язык берёт из истории и характера — они и в реплике
    const name = languageName(lang);
    const appNote = [plan?.note, actionNote, name ? `Reply in ${name}.` : ''].filter(Boolean).join(' ');
    const prompt = appNote ? `${local.text}\n\n(Note from the app, not from the user: ${appNote})` : local.text;
    const r = await OfflineLlm.localChat({ requestId, system: local.system, history: local.history, text: prompt, ...LOCAL_SAMPLING });
    return { text: cleanAnswer(r.text), source: 'local', reminder: plan?.reminder, fallback };
  } catch (e) {
    const code = pluginCode(e);
    if (code === 'model_missing') throw new OfflineUnavailable('no_route', cloud);
    if (code === 'model_broken') throw new OfflineUnavailable('model_broken', cloud);
    throw e;
  } finally {
    void handle.remove();
  }
}
