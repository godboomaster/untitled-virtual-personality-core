import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { api } from '../api';
import type { NoteEntry } from '../api';
import { usePersonaAvatars } from '../avatarStore';
import { confirmDialog } from '../dialogStore';
import { useI18n } from '../i18n';
import type { Persona } from '../mockData';

/* Заметки пользователя на главной: своя книжка у каждой персоны (персона
   пишет в неё по «запиши в заметки …» в чате и читает по «что у меня в
   заметках?»). Здесь — выбор персоны, список (свежие сверху), добавление,
   правка и удаление. Файл книжки общий с ботом — он мог дописать заметку
   из чата, поэтому список перечитывается раз в полминуты и при возврате на
   вкладку. Без бэкенда — моковые заметки, как в остальном прототипе. */

const POLL_MS = 30_000;
// Длинная заметка свёрнута до нескольких строк
const LONG_CHARS = 280;
const LONG_LINES = 5;
const PICK_KEY = 'vpc.notes.persona';

// Моковые заметки по персонам (module-level — переживают перемонтирование)
const mockNotes = new Map<string, NoteEntry[]>();

function readPick(): string | null {
  try {
    return localStorage.getItem(PICK_KEY);
  } catch {
    return null;
  }
}

function savePick(id: string) {
  try {
    localStorage.setItem(PICK_KEY, id);
  } catch {
    /* приватное окно — выбор просто не запомнится */
  }
}

interface NotesWidgetProps {
  personas: Persona[];
  live: boolean; // данные с бэкенда
  mock: boolean; // прототип без бэкенда — моковые заметки
  noLink: boolean; // бэкенд есть, ядро не на связи
  counts?: Record<string, number>; // число заметок персон из сводки главной
}

export default function NotesWidget({ personas, live, mock, noLink, counts }: NotesWidgetProps) {
  const { t, lang } = useI18n();
  const avatars = usePersonaAvatars();
  const locale = lang === 'ru' ? 'ru-RU' : 'en-US';

  const [picked, setPicked] = useState<string | null>(readPick);
  const persona = personas.find((p) => p.id === picked) ?? personas[0];
  const pid = persona?.id ?? null;

  // Список вместе с персоной, чей он: после переключения старый не
  // показывается, пока грузится новый
  const [list, setList] = useState<{ pid: string; items: NoteEntry[] } | null>(null);
  const items = list && list.pid === pid ? list.items : null;
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // Число заметок персон, чьи списки уже открывали (моки, старый сервер)
  const [ownCounts, setOwnCounts] = useState<Record<string, number>>({});
  useEffect(() => {
    if (list) setOwnCounts((c) => (c[list.pid] === list.items.length ? c : { ...c, [list.pid]: list.items.length }));
  }, [list]);

  // Правка списка персоны p (функцией — от актуального списка, а не от
  // снимка до await); у моков — и в их хранилище
  const updateList = useCallback(
    (p: string, fn: (prev: NoteEntry[]) => NoteEntry[]) =>
      setList((prev) => {
        if (!prev || prev.pid !== p) return prev;
        const next = fn(prev.items);
        if (!live) mockNotes.set(p, next);
        return { pid: p, items: next };
      }),
    [live],
  );

  // Моки: заметки только у первой персоны (у остальных книжка пустая)
  const firstId = personas[0]?.id;
  const mockFor = useCallback(
    (id: string): NoteEntry[] => {
      let list = mockNotes.get(id);
      if (!list) {
        const now = Date.now() / 1000;
        const mk = (n: number, text: string, ago: number, source: NoteEntry['source']): NoteEntry => ({
          id: `mock-${id}-${n}`,
          text,
          created_at: now - ago,
          updated_at: null,
          author_name: '',
          owner: true,
          source,
        });
        list = id === firstId
          ? [mk(3, t('notes.mock3'), 3600 * 2, 'home'), mk(2, t('notes.mock2'), 86400, 'chat'), mk(1, t('notes.mock1'), 86400 * 3, 'chat')]
          : [];
        mockNotes.set(id, list);
      }
      return list;
    },
    [firstId, t],
  );

  // Загрузка заметок выбранной персоны + опрос; stale — ответ для другой
  // персоны (переключились, пока шёл запрос) не подменяет список. Через
  // ref: главная перерисовывается каждую секунду (часы), и зависимость от
  // свежих колбэков перезапускала бы опрос (и закрывала правку) так же часто
  const reqSeq = useRef(0);
  const loadRef = useRef<() => void>(() => {});
  loadRef.current = () => {
    if (!pid) return;
    if (!live) {
      setList(mock ? { pid, items: [...mockFor(pid)] } : null);
      return;
    }
    const seq = ++reqSeq.current;
    api
      .getNotes(pid)
      .then((r) => seq === reqSeq.current && setList({ pid, items: r.items }))
      .catch(() => seq === reqSeq.current && setList((prev) => (prev?.pid === pid ? prev : { pid, items: [] })));
  };
  const load = () => loadRef.current();

  useEffect(() => {
    setEditing(null);
    setError(null);
    loadRef.current();
    if (!live) return;
    const timer = setInterval(() => loadRef.current(), POLL_MS);
    const onFocus = () => document.visibilityState === 'visible' && loadRef.current();
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onFocus);
    };
  }, [pid, live, mock]);

  const pick = (id: string) => {
    setPicked(id);
    savePick(id);
  };

  const add = async () => {
    const text = draft.trim();
    if (!text || !pid || busy) return;
    if (!live) {
      const now = Date.now() / 1000;
      updateList(pid, (prev) => [{ id: `mock-${Date.now()}`, text, created_at: now, updated_at: null, author_name: '', owner: true, source: 'home' }, ...prev]);
      setDraft('');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { note } = await api.addNote(pid, text);
      updateList(pid, (prev) => [note, ...prev.filter((x) => x.id !== note.id)]);
      setDraft('');
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : t('notes.saveFailed'));
    } finally {
      setBusy(false);
    }
  };

  const saveEdit = async () => {
    if (!editing || !pid) return;
    const text = editing.text.trim();
    if (!text) return;
    if (!live) {
      const id = editing.id;
      updateList(pid, (prev) => prev.map((n) => (n.id === id ? { ...n, text, updated_at: Date.now() / 1000 } : n)));
      setEditing(null);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { note } = await api.updateNote(pid, editing.id, text);
      updateList(pid, (prev) => prev.map((n) => (n.id === note.id ? note : n)));
      setEditing(null);
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : t('notes.saveFailed'));
      load(); // заметку могли удалить из чата
    } finally {
      setBusy(false);
    }
  };

  const remove = async (n: NoteEntry) => {
    if (!pid) return;
    const ok = await confirmDialog({
      title: t('notes.deleteTitle'),
      message: n.text.length > 160 ? `${n.text.slice(0, 159)}…` : n.text,
      confirmLabel: t('common.delete'),
      danger: true,
    });
    if (!ok) return;
    if (live) {
      try {
        await api.deleteNote(pid, n.id);
      } catch {
        load(); // уже удалена (из чата) — просто перечитываем
        return;
      }
    }
    updateList(pid, (prev) => prev.filter((x) => x.id !== n.id));
  };

  const toggleExpand = (id: string) =>
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const when = (ts: number) =>
    new Date(ts * 1000).toLocaleString(locale, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

  // Открытая персона — по списку; остальные — по сводке главной (её
  // перечитывают раз в минуту), без неё — по последнему открытию
  const countOf = (id: string): number | undefined =>
    id === pid && items ? items.length : (counts?.[id] ?? ownCounts[id]);

  return (
    <div className="notes bracketed">
      <div className="corner tl" />
      <div className="corner tr" />
      <div className="corner bl" />
      <div className="corner br" />

      {personas.length === 0 ? (
        <div className="notes-empty">{t('notes.noPersonas')}</div>
      ) : (
        <>
          {/* Чья книжка: персоны — метками с аватаром и числом заметок */}
          <div className="notes-personas" role="tablist">
            {personas.map((p) => {
              const n = countOf(p.id);
              return (
                <button
                  key={p.id}
                  type="button"
                  role="tab"
                  aria-selected={p.id === pid}
                  className={`notes-persona${p.id === pid ? ' notes-persona--on' : ''}`}
                  style={p.color ? ({ '--note-color': p.color } as CSSProperties) : undefined}
                  onClick={() => pick(p.id)}
                >
                  <span className="notes-persona-avatar">
                    {avatars[p.id] ? <img src={avatars[p.id]} alt="" /> : p.name.charAt(0)}
                  </span>
                  <span className="notes-persona-name">{p.name}</span>
                  {n !== undefined && n > 0 && <span className="notes-persona-count">{n}</span>}
                </button>
              );
            })}
          </div>

          {noLink && !live ? (
            <div className="notes-empty">{t('notes.noLink')}</div>
          ) : (
            <>
              <div className="notes-add">
                <textarea
                  className="input notes-input"
                  rows={draft.includes('\n') ? 4 : 2}
                  placeholder={t('notes.placeholder')}
                  value={draft}
                  maxLength={4000}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                      e.preventDefault();
                      void add();
                    }
                  }}
                />
                <button type="button" className="btn btn--primary" disabled={!draft.trim() || busy} onClick={() => void add()}>
                  {t('notes.add')}
                </button>
              </div>
              {error && <div className="notes-error">{error}</div>}

              {items !== null && items.length === 0 && <div className="notes-empty">{t('notes.empty')}</div>}
              <ul className="notes-list">
                {(items ?? []).map((n) => {
                  const long = n.text.length > LONG_CHARS || n.text.split('\n').length > LONG_LINES;
                  const open = expanded.has(n.id);
                  const isEditing = editing?.id === n.id;
                  return (
                    <li key={n.id} className="note">
                      {isEditing ? (
                        <div className="note-edit">
                          <textarea
                            className="input notes-input"
                            rows={Math.min(10, Math.max(3, editing.text.split('\n').length + 1))}
                            value={editing.text}
                            maxLength={4000}
                            autoFocus
                            onChange={(e) => setEditing({ id: n.id, text: e.target.value })}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                                e.preventDefault();
                                void saveEdit();
                              } else if (e.key === 'Escape') {
                                setEditing(null);
                              }
                            }}
                          />
                          <div className="note-edit-actions">
                            <button type="button" className="btn btn--primary" disabled={!editing.text.trim() || busy} onClick={() => void saveEdit()}>
                              {t('common.save')}
                            </button>
                            <button type="button" className="btn btn--ghost" onClick={() => setEditing(null)}>
                              {t('common.cancel')}
                            </button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <div className={`note-text${long && !open ? ' note-text--clamped' : ''}`}>{n.text}</div>
                          {long && (
                            <button type="button" className="note-more" onClick={() => toggleExpand(n.id)}>
                              {open ? t('notes.less') : t('notes.more')}
                            </button>
                          )}
                        </>
                      )}
                      <div className="note-meta">
                        <span className="note-time" title={n.updated_at ? `${t('notes.edited')} ${when(n.updated_at)}` : undefined}>
                          {when(n.created_at)}
                          {n.updated_at ? ` · ${t('notes.edited')}` : ''}
                        </span>
                        <span className="badge note-source">{n.source === 'home' ? t('notes.fromHome') : t('notes.fromChat')}</span>
                        {!n.owner && n.author_name && <span className="note-author">{t('notes.by', { name: n.author_name })}</span>}
                        {!isEditing && (
                          <span className="note-actions">
                            <button type="button" className="btn btn--icon" title={t('common.edit')} onClick={() => setEditing({ id: n.id, text: n.text })}>
                              ✎
                            </button>
                            <button type="button" className="btn btn--icon" title={t('common.delete')} onClick={() => void remove(n)}>
                              ✕
                            </button>
                          </span>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
              <div className="notes-hint">{t('notes.hint')}</div>
            </>
          )}
        </>
      )}
    </div>
  );
}
