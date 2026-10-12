import { parseDocument } from 'yaml';
import type { ApiPersona } from '../api';
import type { OfflineKit } from './kit';
import { detectLanguage, languageLine } from './language';

/* Персоны, созданные на телефоне без ядра. Создать персону — это записать
   YAML на ядро, а его нет: YAML ждёт здесь (свой ключ — снимок списка
   персон с ядра его не перезапишет), персона сразу видна в списках и
   отвечает без ядра по киту из своего YAML (buildLocalKit). Ядро на
   связи — personaSync.ts переносит её туда, дальше она обычная.
   Без зависимостей приложения: scripts/test_offline_personas.py гоняет
   модуль под Node. */

export type LocalPersonaStatus = 'pending' | 'error';

export interface LocalPersona {
  id: string;
  name: string;
  description: string;
  yaml: string;
  created_at: number; // unix-секунды
  status: LocalPersonaStatus; // pending — ждёт ядра; error — ядро не приняло (400/422)
  error?: string; // ответ ядра при error — на карточке персоны
  color?: string | null;
}

// Персона из списка (ApiPersona) с пометкой «только на телефоне»
export type LocalApiPersona = ApiPersona & { local: { status: LocalPersonaStatus; error?: string } };

const KEY = 'vpc-offline-new-personas';
const listeners = new Set<() => void>();
const renameListeners = new Set<(from: string, to: string) => void>();

// Формат id и служебные имена — как у ядра (app/api/security.PERSONA_ID_RE,
// app/api/settings_api.py: _SHARED_DATA_NAMES, _reserved_id): такую персону
// ядро не примет, а узнать об этом без него нельзя
export const LOCAL_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const RESERVED_IDS = new Set(['tg', 'default', 'skins', 'skin_gen', 'settings_probe', 'persona_drafts', 'personas']);

export function isReservedId(id: string): boolean {
  const p = id.toLowerCase();
  return RESERVED_IDS.has(p) || p.startsWith('api_');
}

export type LocalPersonaErrorCode = 'yaml' | 'no_prompt' | 'bad_id' | 'reserved' | 'exists' | 'storage';

export class LocalPersonaError extends Error {
  code: LocalPersonaErrorCode;
  id: string;
  constructor(code: LocalPersonaErrorCode, id = '', detail = '') {
    super(detail || code);
    this.code = code;
    this.id = id;
  }
}

// ── Хранилище ─────────────────────────────────────────────────────────
// Список читается на каждом рендере (слияние в apiData): разбор JSON — только
// когда строка в хранилище сменилась, и тот же массив — пока она прежняя

let cachedRaw: string | null | undefined;
let cachedList: LocalPersona[] = [];

export function localPersonas(): LocalPersona[] {
  let raw: string | null;
  try {
    raw = localStorage.getItem(KEY);
  } catch {
    return [];
  }
  if (raw === cachedRaw) return cachedList;
  cachedRaw = raw;
  try {
    const v = raw ? (JSON.parse(raw) as LocalPersona[]) : [];
    cachedList = Array.isArray(v) ? v : [];
  } catch {
    cachedList = [];
  }
  return cachedList;
}

export function localPersona(id: string): LocalPersona | null {
  return localPersonas().find((p) => p.id === id) ?? null;
}

/** Персона создана на телефоне и ещё не перенесена на ядро. */
export function isLocalPersona(id: string): boolean {
  return localPersona(id) !== null;
}

// strict — сбой записи наружу: новая персона, не попавшая в хранилище,
// пропала бы с перезапуском, а модалка сказала бы «создана»
function save(list: LocalPersona[], strict = false) {
  try {
    if (list.length) localStorage.setItem(KEY, JSON.stringify(list));
    else localStorage.removeItem(KEY);
  } catch (e) {
    if (strict) throw new LocalPersonaError('storage', '', e instanceof Error ? e.message : String(e));
  }
  listeners.forEach((l) => l());
}

export function onLocalPersonasChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** id сменился при переносе на ядро (занят) — чат переключается на новый. */
export function onLocalPersonaRenamed(fn: (from: string, to: string) => void): () => void {
  renameListeners.add(fn);
  return () => {
    renameListeners.delete(fn);
  };
}

export function notifyLocalPersonaRenamed(from: string, to: string) {
  renameListeners.forEach((fn) => fn(from, to));
}

// ── YAML ──────────────────────────────────────────────────────────────

type Obj = Record<string, unknown>;
const asObj = (v: unknown): Obj => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Obj) : {});

export interface ParsedPersona {
  id: string;
  name: string;
  description: string;
  systemPrompt: string;
  data: Obj;
}

/** YAML персоны → поля; проверки — как create_persona ядра (кроме «уже есть»). */
export function parsePersonaYaml(yaml: string): ParsedPersona {
  const doc = parseDocument(yaml);
  if (doc.errors.length) throw new LocalPersonaError('yaml', '', doc.errors[0].message.split('\n')[0]);
  const data = doc.toJS() as unknown;
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new LocalPersonaError('yaml', '', 'persona.yaml must be a mapping');
  const o = data as Obj;
  const systemPrompt = typeof o.system_prompt === 'string' ? o.system_prompt.trim() : '';
  if (!systemPrompt) throw new LocalPersonaError('no_prompt');
  const id = String(o.id ?? '').trim();
  if (!LOCAL_ID_RE.test(id)) throw new LocalPersonaError('bad_id', id);
  if (isReservedId(id)) throw new LocalPersonaError('reserved', id);
  const name = o.name != null && String(o.name).trim() ? String(o.name).trim() : id;
  return { id, name, description: o.description != null ? String(o.description) : '', systemPrompt, data: o };
}

/** Тот же YAML с другим id: правка одной строки (комментарии и вид блоков
 * остаются), не вышло — через разбор документа. */
export function withYamlId(yaml: string, id: string): string {
  const line = yaml.replace(/^id:[ \t]*[^\n]*$/m, `id: ${id}`);
  try {
    if (String(asObj(parseDocument(line).toJS()).id ?? '') === id) return line;
  } catch {
    /* не разобрался — ниже через документ */
  }
  const doc = parseDocument(yaml);
  doc.setIn(['id'], id);
  return doc.toString({ lineWidth: 0 });
}

// ── Цвет метки ────────────────────────────────────────────────────────
// Как persona_color ядра (app/api/runtime.py): явный color из YAML, иначе
// палитра по crc32 id — на ядре персона получит тот же цвет

const PALETTE = ['#e0683a', '#3f8cff', '#3fae6b', '#b06fd6', '#c9a227', '#38b6a5', '#d64f6e', '#7a9a3a'];

export function crc32(s: string): number {
  let c = ~0;
  for (const b of new TextEncoder().encode(s)) {
    c ^= b;
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return ~c >>> 0;
}

export function personaColor(id: string, data: Obj): string {
  const explicit = data.color;
  if (typeof explicit === 'string' && explicit.trim()) return explicit.trim();
  return PALETTE[crc32(id) % PALETTE.length];
}

// ── Создание, правка, удаление ────────────────────────────────────────

/** Сохранить новую персону на телефоне. existingIds — id из списка персон
 * (снимок с ядра); занятый id, служебный, без system_prompt —
 * LocalPersonaError. */
export function addLocalPersona(yaml: string, existingIds: string[], now = Date.now() / 1000): LocalPersona {
  const p = parsePersonaYaml(yaml);
  const list = localPersonas();
  // Без учёта регистра: на macOS/Windows Connor.yaml и connor.yaml — один файл
  const taken = new Set([...existingIds, ...list.map((x) => x.id)].map((x) => x.toLowerCase()));
  if (taken.has(p.id.toLowerCase())) throw new LocalPersonaError('exists', p.id);
  const persona: LocalPersona = {
    id: p.id,
    name: p.name,
    description: p.description,
    yaml,
    created_at: now,
    status: 'pending',
    color: personaColor(p.id, p.data),
  };
  save([...list, persona], true);
  return persona;
}

/** Перенос на ядро: id занят — новый id в YAML и в записи. */
export function renameLocalPersona(from: string, to: string): LocalPersona | null {
  const list = localPersonas();
  const cur = list.find((p) => p.id === from);
  if (!cur) return null;
  const yaml = withYamlId(cur.yaml, to);
  let color = cur.color ?? null;
  try {
    color = personaColor(to, parsePersonaYaml(yaml).data);
  } catch {
    /* YAML с ошибкой — цвет прежний */
  }
  const next: LocalPersona = { ...cur, id: to, yaml, color };
  save(list.map((p) => (p.id === from ? next : p)));
  return next;
}

export function setLocalPersonaError(id: string, error: string) {
  save(localPersonas().map((p) => (p.id === id ? { ...p, status: 'error' as const, error } : p)));
}

export function removeLocalPersona(id: string) {
  const list = localPersonas();
  if (list.some((p) => p.id === id)) save(list.filter((p) => p.id !== id));
}

/** Персона в виде ответа GET /api/personas — для списков приложения. */
export function localApiPersona(p: LocalPersona): LocalApiPersona {
  let data: Obj = {};
  try {
    data = parsePersonaYaml(p.yaml).data;
  } catch {
    /* сохранён проверенным; сломанный — без фич и настроек */
  }
  return {
    id: p.id,
    name: p.name,
    description: p.description,
    color: p.color ?? null,
    features: asObj(data.features),
    settings: asObj(data.settings) as ApiPersona['settings'],
    builtin: false,
    customized: false,
    local: { status: p.status, ...(p.error ? { error: p.error } : {}) },
  };
}

// ── Кит из YAML ───────────────────────────────────────────────────────

// Правила без кита с ядра (на телефоне нет ни одного): полный характер есть,
// а напоминаний, поиска и погоды без заметок ядра нет — не обещать их
export const LOCAL_PERSONA_RULES =
  "You are answering from the user's phone: the computer you normally run on is unreachable, " +
  'so your tools are unavailable right now. Stay fully in character and keep replies short and ' +
  "conversational. Don't invent facts about the user. Don't claim to set reminders, add to-dos, " +
  "search the web or control the computer — if asked, say you'll do it once you're back on the computer.";

const EMPTY_NOTES = { set: '', ask_time: '', past: '', recurring: '', scheduled: '', extract: '' };

/**
 * Кит персоны с телефона: характер — весь system_prompt (не имя с описанием,
 * как у запасного), памяти и истории нет. Общее для всех персон — правила,
 * заметки напоминаний, гейт поиска, погода — из кита с ядра (donor), если
 * он есть на телефоне: тогда без ядра работают и напоминания, и поиск.
 * lang — язык интерфейса (запасной; ответ — на языке реплики).
 */
export function buildLocalKit(p: LocalPersona, donor: OfflineKit | null, lang?: string | null): OfflineKit {
  let parsed: ParsedPersona | null = null;
  try {
    parsed = parsePersonaYaml(p.yaml);
  } catch {
    /* сохранён проверенным — сюда не попасть */
  }
  const character = parsed?.systemPrompt ?? '';
  const feats = asObj(parsed?.data.features);
  const short = typeof parsed?.data.short_prompt === 'string' ? parsed.data.short_prompt.trim() : '';
  const full = !!donor && donor.version >= 2 && !!donor.rules;
  const code = lang || detectLanguage(character);
  const language = languageLine(code);
  const rules = full ? donor.parts.rules : LOCAL_PERSONA_RULES;
  const style = donor?.parts.style ?? '';
  return {
    version: full ? donor.version : 1,
    persona: p.id,
    name: p.name,
    generated_at: p.created_at,
    fetched_at: 0,
    local: true,
    system: [character, rules, style, language].filter(Boolean).join('\n\n'),
    parts: {
      character,
      rules,
      style,
      language,
      short,
      ...(donor?.parts.rules_local ? { rules_local: donor.parts.rules_local } : {}),
    },
    ...(short ? { short_prompt: { source: 'yaml' as const, status: 'ready' as const, at: p.created_at } } : {}),
    lang: code,
    facts: [],
    history: [],
    history_first_ts: 0,
    history_total: 0,
    dossier: '',
    self_memory: '',
    env: full ? donor.env : null,
    reminders: [],
    // Как у ядра: напоминания — по флагу персоны; без заметок ядра — нечем
    reminders_enabled: full && feats.reminder === true,
    rules: full ? donor.rules : { web: '', env: '' },
    notes: full ? donor.notes : EMPTY_NOTES,
    search_gate: full ? donor.search_gate : { address_words: [], smalltalk: [], self_patterns: [] },
  };
}
