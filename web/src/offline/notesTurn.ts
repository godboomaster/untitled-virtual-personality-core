import { getKit } from './kit';
import { localNotes, newNoteId, saveLocalNotes } from './localNotes';
import type { NoteEntry } from '../api';
import type { HistoryLine } from './prompt';
import {
  MAX_NOTES,
  MAX_TEXT,
  parseNotesRequest,
  promptAsk,
  promptDeleted,
  promptDeleteUnclear,
  promptFull,
  promptNothingToSave,
  promptSaved,
  promptShow,
  renderNotesList,
  resolveDelete,
} from './notesParse';

/* «Запиши в заметки …» без ядра — как _notes_turn ядра: запись и удаление
   делает код (книжка телефона, localNotes.ts — уйдёт в ядро при связи),
   модели — итог для ответа своими словами, «покажи заметки» — список
   отдельным сообщением после ответа. code — тот же итог одной строкой, если
   ответить некому (ни облака, ни модели на телефоне): заметка записана, и
   сказать об этом может само приложение. */

export interface NotesTurn {
  note: string; // для модели (prompt_* ядра)
  list?: string; // «покажи заметки» — отдельным сообщением
  code?: string; // ответ без модели
  claimed: boolean; // просьба (не вопрос по заметкам): напоминания и поиск ход не берут
}

const RU = (lang: string | null | undefined) => lang !== 'en';

function clip(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit - 1).trimEnd()}…`;
}

/** Заметки телефона по порядку записи (номера — как у ядра). */
function chronological(persona: string): NoteEntry[] {
  return [...localNotes(persona)].sort((a, b) => a.created_at - b.created_at);
}

/** Просьба про заметки → итог; null — реплика не про заметки. */
export function notesTurn(persona: string, text: string, history: HistoryLine[], lang?: string | null): NotesTurn | null {
  const kit = getKit(persona);
  const names = kit?.names?.length ? kit.names : [kit?.name ?? '', persona].filter(Boolean);
  const req = parseNotesRequest(text, names);
  if (!req) return null;
  const notes = chronological(persona);
  const ru = RU(lang);

  if (req.kind === 'add') {
    let body = req.text;
    // «Запиши это в заметки» — последний ответ персоны
    if (req.ref || !body) {
      const last = req.ref ? [...history].reverse().find((h) => h.role === 'assistant')?.text.trim() ?? '' : '';
      body = last;
    }
    if (!body) return { claimed: true, note: promptNothingToSave(), code: ru ? 'Что записать в заметки?' : 'What should I write down?' };
    if (notes.length >= MAX_NOTES) {
      return { claimed: true, note: promptFull(), code: ru ? 'Заметок уже 500 — старые можно удалить на главной.' : 'Notes are full (500) — delete old ones on the home page.' };
    }
    const note: NoteEntry = {
      id: newNoteId(),
      text: body.slice(0, MAX_TEXT),
      created_at: Date.now() / 1000,
      updated_at: null,
      author_name: '',
      owner: true,
      source: 'chat',
    };
    // В хранилище — свежие сверху, как в блоке на главной
    saveLocalNotes(persona, [note, ...localNotes(persona)]);
    return { claimed: true, note: promptSaved(note.text), code: `${ru ? 'Записано в заметки' : 'Saved to notes'}: «${clip(note.text, 300)}»` };
  }

  if (req.kind === 'delete') {
    const picked = resolveDelete(req, notes);
    if (!picked || !picked.length) {
      return {
        claimed: true,
        note: promptDeleteUnclear(notes),
        code: notes.length
          ? `${ru ? 'Какую заметку удалить? Назовите номер.' : 'Which note should I delete? Give its number.'}\n${renderNotesList(notes, lang)}`
          : ru
            ? 'Заметок нет — удалять нечего.'
            : 'There are no notes to delete.',
      };
    }
    const gone = new Set(picked.map((n) => n.id));
    saveLocalNotes(persona, localNotes(persona).filter((n) => !gone.has(n.id)));
    const items = picked.map((n) => `— ${clip(n.text, 200)}`).join('\n');
    return { claimed: true, note: promptDeleted(picked, notes.length - picked.length), code: `${ru ? 'Удалено из заметок' : 'Deleted from notes'}:\n${items}` };
  }

  if (req.kind === 'show') {
    const list = notes.length ? renderNotesList(notes, lang) : undefined;
    return { claimed: true, note: promptShow(notes), list, code: list ? undefined : renderNotesList([], lang) };
  }

  return { claimed: false, note: promptAsk(notes) };
}
