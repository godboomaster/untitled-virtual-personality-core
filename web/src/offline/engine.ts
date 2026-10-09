import { getKit } from './kit';
import type { OfflineKit } from './kit';
import { OfflineLlm, pluginCode } from './plugin';
import type { ChatMsg } from './plugin';

/* Кто отвечает без ноутбука: сначала облачная модель по ключу (если есть
   интернет и ключи), иначе — Gemma на телефоне (если модель скачана). Нет
   ни того ни другого — OfflineUnavailable: реплика ждёт связи в очереди. */

export interface OfflineAnswer {
  text: string;
  source: 'cloud' | 'local';
  provider?: string;
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
const LOCAL_HISTORY = 8; // телефону — короче: быстрее первое слово
const LOCAL_FACTS = 25;
const LOCAL_CHARACTER_MAX = 4000; // символов характера персоны для телефона

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

/** Промпт для модели телефона: короче облачного — меньше фактов и характера. */
export function localSystem(kit: OfflineKit): string {
  const p = kit.parts;
  const facts = kit.facts.slice(-LOCAL_FACTS);
  return [
    p.character.slice(0, LOCAL_CHARACTER_MAX),
    p.rules,
    p.style,
    facts.length ? `What you know about the user:\n${facts.map((f) => `- ${f}`).join('\n')}` : '',
    p.language,
  ]
    .filter(Boolean)
    .join('\n\n');
}

function newRequestId(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export async function answerOffline(
  persona: string,
  text: string,
  history: HistoryLine[],
  onToken?: (chunk: string) => void,
): Promise<OfflineAnswer> {
  const kit = getKit(persona);
  if (!kit) throw new OfflineUnavailable('no_kit');

  // 1. Облако: ключи и перебор провайдеров — в нативном плагине
  let cloud = '';
  try {
    const r = await OfflineLlm.cloudChat({
      persona,
      messages: [
        { role: 'system', content: kit.system },
        ...normalizeHistory([...history.slice(-CLOUD_HISTORY), { role: 'user', text }]),
      ],
    });
    const answer = cleanAnswer(r.text);
    if (answer) return { text: answer, source: 'cloud', provider: r.provider };
  } catch (e) {
    // нет ключей, нет интернета или провайдеры не ответили — к модели телефона
    cloud = pluginCode(e);
  }

  // 2. Модель на телефоне
  const status = await OfflineLlm.modelStatus().catch(() => null);
  if (!status?.downloaded) throw new OfflineUnavailable('no_route', cloud);
  const requestId = newRequestId();
  const handle = await OfflineLlm.addListener('localToken', (d) => {
    if (d.requestId === requestId) onToken?.(d.text);
  });
  try {
    const hist = normalizeHistory(history.slice(-LOCAL_HISTORY));
    const r = await OfflineLlm.localChat({ requestId, system: localSystem(kit), history: hist, text });
    return { text: cleanAnswer(r.text), source: 'local' };
  } catch (e) {
    const code = pluginCode(e);
    if (code === 'model_missing') throw new OfflineUnavailable('no_route', cloud);
    if (code === 'model_broken') throw new OfflineUnavailable('model_broken', cloud);
    throw e;
  } finally {
    void handle.remove();
  }
}
