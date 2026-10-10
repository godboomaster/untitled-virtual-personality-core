import type { OfflineKit } from './kit';

/* Поиск по памяти персоны без ноутбука — как search_relevant ядра, только
   по словам (BM25 по основам слов), а не по векторам: модели эмбеддингов на
   телефоне нет, а для сотен сообщений слов хватает. Без зависимостей —
   scripts/test_offline_recall.py гоняет его под Node. */

// Служебные слова — не тема (как «и», «что», «the»)
const STOP = new Set(
  (
    'и в во не что он на я с со как а то все всё она так его но да ты к у же вы за бы по только ее её мне было вот от ' +
    'меня еще ещё нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь ' +
    'там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже ' +
    'себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее неё ' +
    'сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про ' +
    'всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более ' +
    'всегда конечно всю между это мне тебе ему мой твой твоя твои мои наш ваш привет пока спасибо давай ладно ок окей ' +
    'просто очень вообще знаешь помнишь скажи расскажи думаешь хочу можешь как-то что-то кто-то ' +
    'мою моё мое мои моих моим твою твоё твое свой свою своё свое свои меня мной тобой нам вами ' +
    'the a an and or but if then so to of in on at by for with about from as is are was were be been being it its ' +
    'this that these those i you he she we they me him her us them my your his our their do does did have has had ' +
    'what which who whom how when where why can could will would should just not no yes ok okay hi hello thanks please ' +
    'tell remember know think'
  ).split(/\s+/),
);

// Окончания — длинные раньше коротких: «работаю», «работать», «работал»,
// «работу» → «работ»
const ENDINGS = (
  'иями ями ами ого его ому ему ыми ими ешь ете ишь ите ать ять ить еть уть ала ила ела али или ели ует уют ' +
  'ая яя ое ее ой ей ый ий ых их ом ем ам ям ах ях ов ев ью ия ие ют ут ят ат ет ит аю яю ую ал ил ел ' +
  'а я у ю о е ы и ь й ing ed es s'
)
  .split(' ')
  .sort((a, b) => b.length - a.length);

/** Основа слова: срез окончания (основа ≥ 3) и не длиннее 6 букв — так
 * «уведомления», «уведомлений» и «уведомлять» дают одно «уведом». */
export function stemWord(word: string): string {
  let w = word.toLowerCase().replace(/ё/g, 'е');
  for (const e of ENDINGS) {
    if (w.endsWith(e) && w.length - e.length >= 3) {
      w = w.slice(0, -e.length);
      break;
    }
  }
  return w.slice(0, 6);
}

/** Слова темы: без служебных и коротких, по основам. */
export function terms(text: string): string[] {
  const out: string[] = [];
  for (const m of text.toLowerCase().replace(/ё/g, 'е').matchAll(/[\p{L}\p{N}]+(?:-[\p{L}\p{N}]+)*/gu)) {
    const w = m[0];
    if (STOP.has(w)) continue;
    if (/^\p{N}+$/u.test(w) ? w.length < 2 : w.length < 3) continue;
    out.push(stemWord(w));
  }
  return out;
}

export interface RecallDoc {
  role: 'user' | 'assistant' | 'fact';
  text: string;
  ts?: number;
}

const K1 = 1.2;
const B = 0.75;
const REL_SCORE = 0.35; // от лучшего: слабее — не к теме
// Тема — хотя бы одно редкое слово запроса: есть не больше чем в RARE_SHARE
// реплик (и не меньше чем в двух) — «дела» из «как дела?» темой не считается
const RARE_SHARE = 0.08;

/** Документы к теме запроса по BM25 — лучшие сверху; к теме ничего — пусто. */
export function searchDocs(query: string, docs: RecallDoc[], limit: number): RecallDoc[] {
  const q = [...new Set(terms(query))];
  if (!q.length || !docs.length) return [];
  const docTerms = docs.map((d) => terms(d.text));
  const df = new Map<string, number>();
  for (const ts of docTerms) for (const t of new Set(ts)) df.set(t, (df.get(t) ?? 0) + 1);
  const n = docs.length;
  const rareMax = Math.max(2, Math.floor(n * RARE_SHARE));
  const avg = docTerms.reduce((s, t) => s + t.length, 0) / n || 1;
  const scored: { doc: RecallDoc; score: number; i: number }[] = [];
  docTerms.forEach((ts, i) => {
    if (!ts.length) return;
    const tf = new Map<string, number>();
    for (const t of ts) tf.set(t, (tf.get(t) ?? 0) + 1);
    let score = 0;
    let rare = false;
    for (const t of q) {
      const f = tf.get(t);
      if (!f) continue;
      const d = df.get(t) ?? 0;
      if (d <= rareMax) rare = true;
      const idf = Math.log(1 + (n - d + 0.5) / (d + 0.5));
      score += (idf * f * (K1 + 1)) / (f + K1 * (1 - B + (B * ts.length) / avg));
    }
    if (score > 0 && rare) scored.push({ doc: docs[i], score, i });
  });
  if (!scored.length) return [];
  // При равенстве — свежее (позже в списке)
  scored.sort((a, b) => b.score - a.score || b.i - a.i);
  const top = scored[0].score;
  return scored
    .filter((s) => s.score >= top * REL_SCORE)
    .slice(0, limit)
    .map((s) => s.doc);
}

const pad = (n: number) => String(n).padStart(2, '0');

/** Метка «10.10 14:32» (+год, если не текущий) — как _format_msg_ts ядра. */
function tsLabel(ts?: number): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const year = d.getFullYear() !== new Date().getFullYear() ? `.${d.getFullYear()}` : '';
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}${year} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Найденные реплики для модели телефона — в реплику пользователя перед
 * вопросом: из конца длинного системного текста маленькая модель их не
 * берёт. Кто что сказал — словами: «User:/You:» модель путает. */
export function recallNote(found: RecallDoc[], maxChars = 200): string {
  const lines = found
    .filter((d) => d.role !== 'fact')
    .sort((a, b) => (a.ts ?? 0) - (b.ts ?? 0))
    .map((d) => {
      const label = tsLabel(d.ts);
      return `- ${label ? `[${label}] ` : ''}${d.role === 'user' ? 'the user wrote' : 'you replied'}: ${d.text.slice(0, maxChars)}`;
    });
  if (!lines.length) return '';
  return `(Note from the app, not from the user — from your earlier conversation, use it if it helps:\n${lines.join('\n')})`;
}

/** Найденные реплики — строками блока ядра «Earlier in the conversation…». */
export function formatRecall(kit: OfflineKit, found: RecallDoc[], maxChars = 200): string {
  const lines = found
    .filter((d) => d.role !== 'fact')
    .sort((a, b) => (a.ts ?? 0) - (b.ts ?? 0))
    .map((d) => {
      const label = tsLabel(d.ts);
      return `  ${d.role === 'user' ? 'User' : 'Assistant'}${label ? ` [${label}]` : ''}: ${d.text.slice(0, maxChars)}`;
    });
  if (!lines.length) return '';
  const head = kit.rules?.recall_head || 'Earlier in the conversation, related things were discussed:';
  return [head, lines.join('\n'), '', kit.rules?.recall ?? ''].join('\n').trim();
}
