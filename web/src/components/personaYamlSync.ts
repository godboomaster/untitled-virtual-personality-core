import { isMap, isScalar, parseDocument, Scalar, visit } from 'yaml';
import type { Document } from 'yaml';

/* Синхронизация формы создания персоны с вставленным/загруженным persona.yaml.
   YAML → форма: поля заполняются из текста (чего нет в тексте — дефолт).
   Форма → YAML: правка поля точечно меняет свой ключ в документе, остальное
   (color, llm, export_server, комментарии) остаётся как было. */

export const simpleFlags = [
  'rate_limit',
  'moderation',
  'punish_block',
  'web_search',
  'file_upload',
  'self_memory',
  'todo',
  'reminder',
  'inventory',
] as const;

// Все ключи features, включая вложенные learning и proactive
export type FeatureKey = (typeof simpleFlags)[number] | 'learning' | 'proactive';

export const allFeatureKeys: FeatureKey[] = [...simpleFlags, 'learning', 'proactive'];

// Уровень интеллекта (app/core/intellect.py): 'none' — блок intellect в yaml
// не пишется вовсе, персона работает в legacy-режиме без уровневых механик
export type IntellectTier = 'none' | 'primitive' | 'normal' | 'bot';

const tiers: IntellectTier[] = ['primitive', 'normal', 'bot'];

// Состояние формы конфига
export interface FormState {
  id: string;
  name: string;
  version: string;
  description: string;
  stmSize: number;
  intellectTier: IntellectTier;
  features: Record<FeatureKey, boolean>;
  learning: { quizEvery: number; silenceThreshold: number; minInterval: number; maxInterval: number };
  proactive: { checkInterval: number; silenceThresholdMin: number; probability: number; maxDaily: number };
  triggerWords: string;
  systemPrompt: string;
  temperature: number;
  maxTokens: number;
  topP: number;
}

// Дефолты LLM-настроек по уровню интеллекта: bot — высокая точность
// (temperature/top_p), но max_tokens выше — полный разбор сложной просьбы
// (код, расчёты) требует длинных ответов; normal — тёплый, свободный стиль;
// primitive — стереотипная короткая речь инстинктивного существа.
// Подставляются при выборе tier, дальше правятся вручную.
export const tierSettings: Record<Exclude<IntellectTier, 'none'>, { temperature: number; maxTokens: number; topP: number }> = {
  primitive: { temperature: 0.5, maxTokens: 800, topP: 0.85 },
  normal: { temperature: 0.85, maxTokens: 3000, topP: 0.92 },
  bot: { temperature: 0.7, maxTokens: 4000, topP: 0.9 },
};

// Транслитерация имени в id (латиница, snake_case)
export function transliterate(s: string): string {
  const map: Record<string, string> = {
    а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'yo', ж: 'zh', з: 'z',
    и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r',
    с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'c', ч: 'ch', ш: 'sh', щ: 'sch',
    ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya',
  };
  return s
    .toLowerCase()
    .split('')
    .map((ch) => map[ch] ?? (/[a-z0-9]/.test(ch) ? ch : '_'))
    .join('')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '');
}

// Дефолты формы: начальное состояние и подстановка для ключей, которых нет во вставленном YAML
export const defaultForm = (name = ''): FormState => ({
  id: transliterate(name),
  name,
  version: '1.0',
  description: '',
  stmSize: 50,
  intellectTier: 'normal',
  features: Object.fromEntries(allFeatureKeys.map((k) => [k, k === 'self_memory'])) as Record<FeatureKey, boolean>,
  learning: { quizEvery: 3, silenceThreshold: 3, minInterval: 600, maxInterval: 604800 },
  proactive: { checkInterval: 30, silenceThresholdMin: 30, probability: 1.0, maxDaily: 20 },
  triggerWords: '',
  systemPrompt: '',
  // начальные LLM-дефолты соответствуют выбранному tier (normal)
  temperature: tierSettings.normal.temperature,
  maxTokens: tierSettings.normal.maxTokens,
  topP: tierSettings.normal.topP,
});

type Path = string[];

// Простые поля формы и их ключи в persona.yaml. intellect, trigger_words и
// system_prompt пишутся отдельно — у них своя форма в документе.
const fieldPaths: [(f: FormState) => string | number | boolean, Path][] = [
  [(f) => f.id, ['id']],
  [(f) => f.name, ['name']],
  [(f) => f.version, ['version']],
  [(f) => f.description, ['description']],
  [(f) => f.stmSize, ['stm_size']],
  ...simpleFlags.map((k): [(f: FormState) => boolean, Path] => [(f) => f.features[k], ['features', k]]),
  [(f) => f.features.learning, ['features', 'learning', 'enabled']],
  [(f) => f.learning.quizEvery, ['features', 'learning', 'quiz_every']],
  [(f) => f.learning.silenceThreshold, ['features', 'learning', 'silence_threshold']],
  [(f) => f.learning.minInterval, ['features', 'learning', 'min_interval_seconds']],
  [(f) => f.learning.maxInterval, ['features', 'learning', 'max_interval_seconds']],
  [(f) => f.features.proactive, ['features', 'proactive', 'enabled']],
  [(f) => f.proactive.checkInterval, ['features', 'proactive', 'check_interval_minutes']],
  [(f) => f.proactive.silenceThresholdMin, ['features', 'proactive', 'silence_threshold_minutes']],
  [(f) => f.proactive.probability, ['features', 'proactive', 'initiative_probability']],
  [(f) => f.proactive.maxDaily, ['features', 'proactive', 'max_daily_initiatives']],
  [(f) => f.temperature, ['settings', 'temperature']],
  [(f) => f.maxTokens, ['settings', 'max_tokens']],
  [(f) => f.topP, ['settings', 'top_p']],
];

const splitWords = (s: string) =>
  s
    .split(',')
    .map((w) => w.trim())
    .filter(Boolean);

type Obj = Record<string, unknown>;
const asObj = (v: unknown): Obj => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Obj) : {});
const str = (v: unknown, d: string) => (v === null || v === undefined ? d : String(v));
const num = (v: unknown, d: number) => (v !== null && v !== '' && Number.isFinite(Number(v)) ? Number(v) : d);
const bool = (v: unknown) => v === true || v === 'true';

// learning/proactive бывают и сокращённой формой `learning: true`
const section = (v: unknown): Obj => (v && typeof v === 'object' ? asObj(v) : { enabled: bool(v) });

// YAML → форма. base — дефолты формы: всё, чего нет в тексте, берётся оттуда.
export function formFromYaml(text: string, base: FormState): { form: FormState } | { error: string } {
  const doc = parseDocument(text);
  if (doc.errors.length) return { error: doc.errors[0].message.split('\n')[0] };
  const y = doc.toJS() as unknown;
  if (!y || typeof y !== 'object' || Array.isArray(y)) return { error: 'persona.yaml must be a mapping (key: value)' };

  const root = y as Obj;
  const feats = asObj(root.features);
  const learning = section(feats.learning);
  const proactive = section(feats.proactive);
  const settings = asObj(root.settings);
  const tier = asObj(root.intellect).tier;
  const words = feats.trigger_words;

  return {
    form: {
      id: str(root.id, base.id),
      name: str(root.name, base.name),
      version: str(root.version, base.version),
      description: str(root.description, base.description),
      stmSize: num(root.stm_size, base.stmSize),
      intellectTier: tiers.includes(tier as IntellectTier) ? (tier as IntellectTier) : 'none',
      features: {
        ...(Object.fromEntries(simpleFlags.map((k) => [k, bool(feats[k])])) as Record<FeatureKey, boolean>),
        learning: bool(learning.enabled),
        proactive: bool(proactive.enabled),
      },
      learning: {
        quizEvery: num(learning.quiz_every, base.learning.quizEvery),
        silenceThreshold: num(learning.silence_threshold, base.learning.silenceThreshold),
        minInterval: num(learning.min_interval_seconds, base.learning.minInterval),
        maxInterval: num(learning.max_interval_seconds, base.learning.maxInterval),
      },
      proactive: {
        checkInterval: num(proactive.check_interval_minutes, base.proactive.checkInterval),
        silenceThresholdMin: num(proactive.silence_threshold_minutes, base.proactive.silenceThresholdMin),
        probability: num(proactive.initiative_probability, base.proactive.probability),
        maxDaily: num(proactive.max_daily_initiatives, base.proactive.maxDaily),
      },
      triggerWords: Array.isArray(words) ? words.map((w) => String(w)).join(', ') : str(words, ''),
      systemPrompt: str(root.system_prompt, '').replace(/\s+$/, ''),
      temperature: num(settings.temperature, base.temperature),
      maxTokens: num(settings.max_tokens, base.maxTokens),
      topP: num(settings.top_p, base.topP),
    },
  };
}

// Сериализация в стиле исходника: списки без отступа (`key:\n- item`, как пишет
// PyYAML) — так и оставляем; строки не переносим, чтобы длинные значения не
// переформатировались
function stringify(doc: Document, source: string): string {
  const indentSeq = !/^( *)[^\s#-][^\n]*:\n\1- /m.test(source);
  return doc.toString({ lineWidth: 0, indentSeq });
}

// `learning: true` → `learning: {enabled: true}`, чтобы в секцию можно было писать ключи
function ensureMap(doc: Document, path: Path) {
  const node = doc.getIn(path, true);
  if (node !== undefined && !isMap(node)) {
    doc.setIn(path, doc.createNode({ enabled: bool(doc.getIn(path)) }));
  }
}

// Форма → YAML: в текст вносятся только поля, изменившиеся между prev и next.
// Невалидный текст не трогаем (форма в этот момент с ним не синхронизирована).
export function yamlWithForm(text: string, prev: FormState, next: FormState): string {
  const doc = parseDocument(text);
  if (doc.errors.length) return text;
  let changed = false;

  for (const [get, path] of fieldPaths) {
    if (get(prev) === get(next)) continue;
    if (path.length === 3) ensureMap(doc, path.slice(0, 2));
    doc.setIn(path, get(next));
    changed = true;
  }

  if (prev.intellectTier !== next.intellectTier) {
    if (next.intellectTier === 'none') doc.deleteIn(['intellect']);
    else doc.setIn(['intellect', 'tier'], next.intellectTier);
    changed = true;
  }

  if (prev.triggerWords !== next.triggerWords) {
    const words = splitWords(next.triggerWords);
    if (words.length) doc.setIn(['features', 'trigger_words'], doc.createNode(words));
    else doc.deleteIn(['features', 'trigger_words']);
    changed = true;
  }

  if (prev.systemPrompt !== next.systemPrompt) {
    // Многострочный промпт — блоком `|`, как в app/personas/*.yaml
    const node = new Scalar(next.systemPrompt.includes('\n') ? `${next.systemPrompt}\n` : next.systemPrompt);
    if (next.systemPrompt.includes('\n')) node.type = Scalar.BLOCK_LITERAL;
    doc.setIn(['system_prompt'], node);
    changed = true;
  }

  if (!changed) return text;
  return stringify(doc, text);
}

// Многострочные строки → блок `|`. Файлы, записанные старым safe_dump,
// хранят system_prompt в кавычках с \n и слэшами переноса — в редакторе
// это нечитаемо. Остальной текст (комментарии, порядок ключей) сохраняется;
// если переделывать нечего — возвращается исходный текст как есть.
export function prettifyYaml(text: string): string {
  const doc = parseDocument(text);
  if (doc.errors.length) return text;
  let changed = false;
  visit(doc, {
    Scalar(_key, node) {
      if (
        isScalar(node) &&
        typeof node.value === 'string' &&
        node.value.includes('\n') &&
        node.type !== Scalar.BLOCK_LITERAL
      ) {
        node.type = Scalar.BLOCK_LITERAL;
        changed = true;
      }
    },
  });
  return changed ? stringify(doc, text) : text;
}
