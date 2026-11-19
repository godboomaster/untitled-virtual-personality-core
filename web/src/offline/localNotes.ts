import { api, isNativeApp } from '../api';
import type { NoteEntry } from '../api';
import { isLocalPersona } from './localPersonas';

/* Заметки, записанные на телефоне без ядра (блок «Заметки» на главной).
   Книжка персоны живёт на ядре (app/features/user_notes.py); без связи с ним
   телефон ведёт свою — по персоне, в localStorage, — а при связи отдаёт её
   ядру (POST …/notes/import: со своими id и временем, повтор по id ядро не
   пишет) и забывает. id — как у ядра (12 hex): ядро примет его как есть. */

const PREFIX = 'vpc-offline-notes:';
const CHUNK = 100; // заметок за запрос — как NotesImportRequest ядра

function read(persona: string): NoteEntry[] {
  try {
    const v = JSON.parse(localStorage.getItem(PREFIX + persona) || '[]') as NoteEntry[];
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function write(persona: string, list: NoteEntry[]) {
  try {
    if (list.length) localStorage.setItem(PREFIX + persona, JSON.stringify(list));
    else localStorage.removeItem(PREFIX + persona);
  } catch {
    /* хранилище недоступно */
  }
}

/** Заметки телефона — свежие сверху (как список ядра). */
export function localNotes(persona: string): NoteEntry[] {
  return read(persona);
}

export function saveLocalNotes(persona: string, list: NoteEntry[]) {
  write(persona, list);
}

export function newNoteId(): string {
  const b = new Uint8Array(6);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

/** Персона сменила id (перенос на ядро) — заметки под новым. */
export function moveLocalNotes(from: string, to: string) {
  const list = read(from);
  if (!list.length) return;
  write(to, [...list, ...read(to)]);
  write(from, []);
}

function personasWithNotes(): string[] {
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

/** Ядро на связи: заметки телефона — ему. */
export async function syncLocalNotes(): Promise<void> {
  if (!isNativeApp()) return;
  for (const persona of personasWithNotes()) {
    // Персоны с телефона на ядре ещё нет — её заметки уйдут после переноса
    if (isLocalPersona(persona)) continue;
    const list = read(persona);
    for (let i = 0; i < list.length; i += CHUNK) {
      const part = list.slice(i, i + CHUNK);
      try {
        await api.importNotes(
          persona,
          part.map((n) => ({ id: n.id, text: n.text, created_at: n.created_at, updated_at: n.updated_at })),
        );
        const sent = new Set(part.map((n) => n.id));
        write(persona, read(persona).filter((n) => !sent.has(n.id)));
      } catch {
        // Нет сети — при следующей связи; 400 (у персоны на ядре заметки
        // выключены) — остаются на телефоне
        break;
      }
    }
  }
}
