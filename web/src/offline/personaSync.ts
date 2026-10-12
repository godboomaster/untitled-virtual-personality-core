import { api, ApiError, isNativeApp } from '../api';
import { coreHealth, refetchPersonas, upsertCachedPersona } from '../apiData';
import { forgetKit, getKit, refreshKit, renameKit, storeKit } from './kit';
import type { OfflineKit } from './kit';
import {
  localApiPersona,
  localPersona,
  localPersonas,
  notifyLocalPersonaRenamed,
  removeLocalPersona,
  renameLocalPersona,
  setLocalPersonaError,
} from './localPersonas';
import type { LocalPersona } from './localPersonas';
import { clearOfflineMessages, moveOfflineMessages } from './store';
import { moveLocalReminders } from './reminders';

/* Перенос на ядро персон, созданных на телефоне без него (localPersonas.ts).
   Ядро на связи — YAML уходит обычным созданием (POST /api/personas), раньше
   переписки: иначе ей некуда лечь (404). id на ядре занят (другая персона
   или память удалённой — 409) — берём свободный «id_2», «id_3»… и переносим
   под него всё, что телефон хранит под старым: старую память на ядре не
   трогаем (memory: keep/fresh не шлём). Ядро не приняло YAML (400/422) —
   персона остаётся на телефоне с ошибкой на карточке. Перенесена — кит с
   ядра сразу: он же запускает на ядре краткое описание для модели
   телефона, его телефон дозапрашивает, пока оно составляется. */

const MAX_TRIES = 20; // попыток с новым id на персону за один перенос
const SHORT_POLL_MS = 60_000;
const SHORT_POLL_MAX_MS = 15 * 60_000;

type MoveResult = { kind: 'done'; id: string } | { kind: 'error' | 'later' | 'offline' };

const lower = (s: string) => s.toLowerCase();

/** Свободный id: base_2, base_3… — не занят ни на ядре, ни на телефоне. */
export function freeId(base: string, taken: Set<string>, self = ''): string {
  const local = new Set(localPersonas().filter((p) => p.id !== self).map((p) => lower(p.id)));
  for (let n = 2; ; n++) {
    const sfx = `_${n}`;
    const id = base.slice(0, 64 - sfx.length) + sfx;
    if (!taken.has(lower(id)) && !local.has(lower(id))) return id;
  }
}

/** Всё, что телефон хранит под id персоны, — под новым id. */
function renameLocal(cur: LocalPersona, to: string): LocalPersona {
  const next = renameLocalPersona(cur.id, to) ?? { ...cur, id: to };
  moveOfflineMessages(cur.id, to);
  renameKit(cur.id, to);
  moveLocalReminders(cur.id, to);
  notifyLocalPersonaRenamed(cur.id, to);
  return next;
}

/** Под этим id на ядре — наша же персона: прошлый перенос дошёл, а ответ
 * потерялся (обрыв связи). Ошибка сети — наружу. */
async function isOurs(p: LocalPersona): Promise<boolean> {
  try {
    const r = await api.getPersonaYaml(p.id);
    return r.yaml.trim() === p.yaml.trim();
  } catch (e) {
    if (e instanceof ApiError) return false;
    throw e;
  }
}

/** Персона на ядре: в списке и снимке сразу (кит из YAML — замена киту с
 * ядра, пока тот не пришёл), из персон телефона — прочь. */
function finalize(p: LocalPersona) {
  const kit = getKit(p.id);
  if (kit) storeKit(p.id, kit);
  const { local: _local, ...persona } = localApiPersona(p);
  upsertCachedPersona(persona);
  removeLocalPersona(p.id);
}

async function moveOne(p: LocalPersona, taken: Set<string>): Promise<MoveResult> {
  let cur = p;
  try {
    for (let i = 0; i < MAX_TRIES; i++) {
      if (taken.has(lower(cur.id))) {
        if (await isOurs(cur)) {
          finalize(cur);
          return { kind: 'done', id: cur.id };
        }
        // Новый id — до запроса: оборвётся связь — при следующей попытке он же
        cur = renameLocal(cur, freeId(p.id, taken, cur.id));
      }
      try {
        await api.createPersona(cur.yaml);
        finalize(cur);
        return { kind: 'done', id: cur.id };
      } catch (e) {
        if (!(e instanceof ApiError)) throw e;
        // Занят: файл есть или осталась память удалённой персоны
        if (e.status === 409) {
          taken.add(lower(cur.id));
          continue;
        }
        if (e.status === 400 || e.status === 422) {
          setLocalPersonaError(cur.id, e.message);
          return { kind: 'error' };
        }
        return { kind: 'later' }; // 401, 5xx — ядро не готово, попробуем при следующей связи
      }
    }
    return { kind: 'later' };
  } catch {
    return { kind: 'offline' };
  }
}

let syncing: Promise<string[]> | null = null;

/** Ядро на связи: персоны с телефона — на него. → id перенесённых (на ядре). */
export function syncLocalPersonas(): Promise<string[]> {
  if (syncing) return syncing;
  syncing = (async () => {
    const moved: string[] = [];
    if (!isNativeApp() || !localPersonas().length) return moved;
    let taken: Set<string>;
    try {
      taken = new Set((await api.getPersonas()).map((p) => lower(p.id)));
    } catch {
      return moved; // нет связи — персоны ждут
    }
    for (const p of [...localPersonas()]) {
      const r = await moveOne(p, taken);
      if (r.kind === 'offline') break;
      if (r.kind === 'done') {
        moved.push(r.id);
        taken.add(lower(r.id));
      }
    }
    if (moved.length) await refetchPersonas();
    return moved;
  })().finally(() => {
    syncing = null;
  });
  return syncing;
}

const following = new Set<string>();

/** Перенесённая персона: кит с ядра сейчас и, пока краткое описание для
 * модели телефона составляется (short_prompt.status = pending), — раз в
 * pollMs, не дольше maxMs и только при связи. → последний кит. */
export async function followShortPrompt(
  persona: string,
  { pollMs = SHORT_POLL_MS, maxMs = SHORT_POLL_MAX_MS }: { pollMs?: number; maxMs?: number } = {},
): Promise<OfflineKit | null> {
  if (following.has(persona)) return getKit(persona);
  following.add(persona);
  const until = Date.now() + maxMs;
  try {
    for (;;) {
      if (coreHealth() !== 'down') {
        const before = Date.now();
        await refreshKit(persona);
        const kit = getKit(persona);
        // Кит не обновился (ошибка) — в хранилище прежний: судить по нему нельзя
        const fresh = !!kit && !kit.local && kit.fetched_at >= before;
        if (fresh && kit.short_prompt?.status !== 'pending') return kit;
      }
      if (Date.now() + pollMs > until) return getKit(persona);
      await new Promise((r) => setTimeout(r, pollMs));
    }
  } finally {
    following.delete(persona);
  }
}

/** «Удалить с телефона»: персона, которой ещё нет на ядре, и всё, что
 * телефон хранит под её id. Персону с ядра не трогает. */
export function forgetLocalPersona(id: string) {
  if (!localPersona(id)) return;
  removeLocalPersona(id);
  clearOfflineMessages(id);
  forgetKit(id);
  moveLocalReminders(id, null);
}
