import { useEffect, useReducer } from 'react';

/* Переписка без ноутбука: хранится на телефоне (localStorage), пока не уйдёт
   на ноутбук (state.ts: syncOffline → POST …/offline/import). source — кто ответил:
   cloud — облачная модель с телефона, local — Gemma на телефоне; у реплики
   пользователя queued — ответа пока не было. */

export type OfflineSource = 'cloud' | 'local' | 'queued';

export interface OfflineMsg {
  id: string; // для ядра: повтор по id не записывается дважды
  num: number; // id пузыря в ленте (ChatMessage.id — число)
  role: 'user' | 'assistant';
  text: string;
  ts: number; // unix-секунды
  source: OfflineSource;
  provider?: string;
  reminder?: { id: string; task: string; at: number }; // поставлено этой репликой
}

const PREFIX = 'vpc-offline-msgs:';
const listeners = new Set<() => void>();
let seq = 0;

function read(persona: string): OfflineMsg[] {
  try {
    const raw = localStorage.getItem(PREFIX + persona);
    const list = raw ? (JSON.parse(raw) as OfflineMsg[]) : [];
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

function write(persona: string, list: OfflineMsg[]) {
  try {
    if (list.length) localStorage.setItem(PREFIX + persona, JSON.stringify(list));
    else localStorage.removeItem(PREFIX + persona);
  } catch {
    /* хранилище недоступно — переписка проживёт до перезапуска только в ленте */
  }
  listeners.forEach((l) => l());
}

function newId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  }
}

export function offlineMessages(persona: string): OfflineMsg[] {
  return read(persona);
}

export function addOfflineMessage(
  persona: string,
  msg: Pick<OfflineMsg, 'role' | 'text' | 'source'> & Pick<Partial<OfflineMsg>, 'provider' | 'reminder'>,
): OfflineMsg {
  const full: OfflineMsg = {
    ...msg,
    id: newId(),
    num: Date.now() * 10 + (seq++ % 10),
    ts: Date.now() / 1000,
  };
  write(persona, [...read(persona), full]);
  return full;
}

export function updateOfflineMessage(persona: string, id: string, patch: Partial<OfflineMsg>) {
  write(persona, read(persona).map((m) => (m.id === id ? { ...m, ...patch } : m)));
}

export function removeOfflineMessages(persona: string, ids: string[]) {
  const drop = new Set(ids);
  write(persona, read(persona).filter((m) => !drop.has(m.id)));
}

/** Переписка — под новым id (персона с телефона сменила id при переносе на ядро). */
export function moveOfflineMessages(from: string, to: string) {
  const list = read(from);
  if (!list.length || from === to) return;
  write(to, [...read(to), ...list]);
  write(from, []);
}

/** Вся переписка персоны — прочь (персона удалена с телефона, на ядре её нет). */
export function clearOfflineMessages(persona: string) {
  write(persona, []);
}

/** Персоны, у которых есть неотправленная на ноутбук переписка. */
export function personasWithOffline(): string[] {
  const out: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k?.startsWith(PREFIX)) out.push(k.slice(PREFIX.length));
    }
  } catch {
    /* хранилище недоступно */
  }
  return out;
}

/** Хвост реплик пользователя без ответа (после последнего ответа). */
export function unansweredTail(list: OfflineMsg[]): OfflineMsg[] {
  let i = list.length;
  while (i > 0 && list[i - 1].role === 'user' && list[i - 1].source === 'queued') i--;
  return list.slice(i);
}

export function useOfflineMessages(persona: string): OfflineMsg[] {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    listeners.add(force);
    return () => {
      listeners.delete(force);
    };
  }, []);
  return read(persona);
}
