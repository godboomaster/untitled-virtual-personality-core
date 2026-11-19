/* Веб-чаты без ключей (WebChatPlugin.java): какие сайты есть и как из
   промпта облака сделать один текст для поля ввода сайта. Без импортов —
   сверяется под Node (scripts/test_phone_webchat.py). */

export type WebchatId = 'duckai' | 'chatgpt' | 'google' | 'deepseek' | 'qwen' | 'kimi' | 'zai' | 'claude';

export interface WebchatSite {
  id: WebchatId;
  name: string;
  host: string;
  account: boolean; // нужен вход — один чат на персону, новых чатов в день мало
  login?: boolean; // войти можно, но не обязательно (ChatGPT: вошли — свой чат на персону)
  maxInput: number; // символов в поле: длиннее — история урезается
}

// Порядок по умолчанию — порядок перебора
export const WEBCHAT_SITES: WebchatSite[] = [
  // Замер ядра: ≤16 тыс. символов целиком проходят
  { id: 'duckai', name: 'Duck.ai', host: 'duck.ai', account: false, maxInput: 16000 },
  { id: 'chatgpt', name: 'ChatGPT', host: 'chatgpt.com', account: false, login: true, maxInput: 12000 },
  // Поле AI Mode: ~9 тыс. уходит, ~12 тыс. — бэкенд молча падает (замер ядра)
  { id: 'google', name: 'Google AI Mode', host: 'google.com', account: false, maxInput: 8000 },
  { id: 'deepseek', name: 'DeepSeek', host: 'chat.deepseek.com', account: true, maxInput: 30000 },
  // Веб-чаты ядра со входом (app/features/web_llm.py ADAPTERS); Google вход
  // в приложении не пускает — Google AI Mode только без входа
  { id: 'qwen', name: 'Qwen', host: 'chat.qwen.ai', account: true, maxInput: 30000 },
  { id: 'kimi', name: 'Kimi', host: 'kimi.com', account: true, maxInput: 30000 },
  { id: 'zai', name: 'Z.ai', host: 'chat.z.ai', account: true, maxInput: 30000 },
  { id: 'claude', name: 'Claude', host: 'claude.ai', account: true, maxInput: 30000 },
];

export interface PromptMsg {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

const INSTRUCTIONS = 'Instructions (follow strictly, do not restate them):';

/** Сообщения → один текст, как веб-чатам ядра (WebChatLLM._join_messages):
 * system — блоком инструкций в начале, дальше реплики с префиксами ролей. */
export function joinMessages(messages: PromptMsg[]): string {
  const sys: string[] = [];
  const convo: string[] = [];
  for (const m of messages) {
    const text = m.content.trim();
    if (!text) continue;
    if (m.role === 'system') sys.push(text);
    else convo.push(`${m.role === 'user' ? 'User' : 'Assistant'}: ${text}`);
  }
  const parts = sys.length ? [`${INSTRUCTIONS}\n${sys.join('\n\n')}`, ...convo] : convo;
  return parts.join('\n\n').trim();
}

/** Текст под поле сайта: не влезает — убираем реплики со старых, последняя
 * (вопрос) и инструкции остаются; без них не влезает — null (сайт пропускаем). */
export function fitPrompt(messages: PromptMsg[], maxInput: number): string | null {
  const system = messages.filter((m) => m.role === 'system');
  const convo = messages.filter((m) => m.role !== 'system');
  for (let from = 0; from < convo.length; from++) {
    const text = joinMessages([...system, ...convo.slice(from)]);
    if (text.length <= maxInput) return text;
  }
  return null;
}

export interface WebchatPrefs {
  order: WebchatId[];
  on: Partial<Record<WebchatId, boolean>>;
}

/** Порядок перебора: сохранённый, новые сайты — в конец; выключенные — мимо. */
export function enabledSites(prefs: WebchatPrefs): WebchatSite[] {
  return orderedSites(prefs).filter((s) => prefs.on[s.id] === true);
}

export function orderedSites(prefs: WebchatPrefs): WebchatSite[] {
  const known = new Map(WEBCHAT_SITES.map((s) => [s.id, s]));
  const out: WebchatSite[] = [];
  for (const id of prefs.order) {
    const s = known.get(id);
    if (s && !out.includes(s)) out.push(s);
  }
  for (const s of WEBCHAT_SITES) if (!out.includes(s)) out.push(s);
  return out;
}
