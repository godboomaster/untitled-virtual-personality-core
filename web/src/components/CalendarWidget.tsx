import { useEffect, useMemo, useState } from 'react';
import type { CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../api';
import type { CalendarEntry, CalendarKind, TodoEntry } from '../api';
import { useApiOnline } from '../apiData';
import { useI18n, useMockData } from '../i18n';
import Select from './Select';

/* Общий календарь всех персон (месячная сетка на главной). Записи хранятся
   на бэкенде (data/calendar.json) и дополняются активными напоминаниями
   персон — readonly-строки с меткой персоны (source='reminder'). Свои записи
   можно привязать к персоне — она помечается её цветом. Без бэкенда —
   моковый режим, как в остальных секциях прототипа. */

const KINDS: CalendarKind[] = ['todo', 'reminder', 'note', 'event'];

// Палитра меток моковых персон (у реальных цвет приходит с бэкенда)
const FALLBACK_COLORS = ['#e0683a', '#3f8cff', '#3fae6b', '#b06fd6', '#c9a227', '#38b6a5', '#d64f6e', '#7a9a3a'];
const NEUTRAL = '#8a8a8a';

// Локальная дата → 'YYYY-MM-DD' (без UTC-сдвига toISOString)
function isoDate(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function addDays(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
}

// 42 ячейки сетки: недели с понедельника, покрывающие месяц
function gridCells(year: number, month: number): Date[] {
  const first = new Date(year, month, 1);
  const shift = (first.getDay() + 6) % 7; // сдвиг до понедельника
  const start = addDays(first, -shift);
  return Array.from({ length: 42 }, (_, i) => addDays(start, i));
}

// Моковые записи (module-level — переживают перемонтирование секции)
let mockItems: CalendarEntry[] | null = null;

interface FormState {
  editingId: string | null;
  title: string;
  kind: CalendarKind;
  time: string;
  persona: string; // '' — без персоны
  note: string;
}

const emptyForm: FormState = { editingId: null, title: '', kind: 'todo', time: '', persona: '', note: '' };

export default function CalendarWidget() {
  const { t, lang } = useI18n();
  const { personas } = useMockData();
  const apiOnline = useApiOnline();
  const locale = lang === 'ru' ? 'ru-RU' : 'en-US';

  const today = new Date();
  const todayISO = isoDate(today);
  const [cursor, setCursor] = useState({ y: today.getFullYear(), m: today.getMonth() });
  const [items, setItems] = useState<CalendarEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [todoOptions, setTodoOptions] = useState<TodoEntry[]>([]);

  const colorOf = (personaId: string | null | undefined): string => {
    if (!personaId) return NEUTRAL;
    const i = personas.findIndex((p) => p.id === personaId);
    if (i < 0) return NEUTRAL;
    return personas[i].color ?? FALLBACK_COLORS[i % FALLBACK_COLORS.length];
  };
  const nameOf = (personaId: string | null | undefined): string | null =>
    personas.find((p) => p.id === personaId)?.name ?? null;

  // Моковый набор привязан к «сегодня», чтобы сетка не была пустой
  if (!apiOnline && mockItems === null) {
    const pid = (i: number) => personas[i % Math.max(personas.length, 1)]?.id ?? null;
    const mk = (offset: number, title: string, kind: CalendarKind, persona: string | null, time: string | null = null): CalendarEntry => ({
      id: `mock-${offset}-${kind}`,
      title,
      date: isoDate(addDays(today, offset)),
      time,
      kind,
      persona,
      persona_name: nameOf(persona),
      color: persona ? colorOf(persona) : null,
      note: '',
      done: false,
      created_at: null,
      source: 'calendar',
      readonly: false,
    });
    mockItems = [
      mk(0, t('calendar.seed1'), 'note', null),
      mk(1, t('calendar.seed2'), 'todo', pid(0), '10:00'),
      mk(3, t('calendar.seed3'), 'event', pid(1), '19:00'),
    ];
  }

  const cells = useMemo(() => gridCells(cursor.y, cursor.m), [cursor.y, cursor.m]);

  // Загрузка записей видимого диапазона (сетка 6 недель)
  useEffect(() => {
    if (!apiOnline) {
      setItems(mockItems ?? []);
      return;
    }
    let stale = false;
    api
      .getCalendar(isoDate(cells[0]), isoDate(cells[cells.length - 1]))
      .then((r) => !stale && setItems(r.items))
      .catch(() => {});
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, cursor.y, cursor.m]);

  // To-do выбранной персоны — для «взять из списка дел»
  useEffect(() => {
    setTodoOptions([]);
    if (!apiOnline || !form.persona) return;
    let stale = false;
    api.getTodo(form.persona).then((r) => !stale && setTodoOptions(r.items)).catch(() => {});
    return () => {
      stale = true;
    };
  }, [apiOnline, form.persona]);

  // Esc закрывает модалку дня
  useEffect(() => {
    if (!selected) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelected(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected]);

  const byDate = useMemo(() => {
    const map = new Map<string, CalendarEntry[]>();
    for (const e of items) {
      const arr = map.get(e.date) ?? [];
      arr.push(e);
      map.set(e.date, arr);
    }
    return map;
  }, [items]);

  const upsert = (entry: CalendarEntry) =>
    setItems((prev) => {
      const i = prev.findIndex((x) => x.id === entry.id);
      if (i < 0) return [...prev, entry];
      const next = [...prev];
      next[i] = entry;
      return next;
    });

  const submit = () => {
    const title = form.title.trim();
    if (!title || !selected) return;
    const payload = {
      title,
      date: selected, // дата записи = день открытой модалки
      time: form.time || null,
      kind: form.kind,
      persona: form.persona || null,
      note: form.note.trim(),
    };
    if (apiOnline) {
      const req = form.editingId
        ? api.updateCalendarEntry(form.editingId, payload)
        : api.addCalendarEntry(payload);
      req.then(upsert).catch(() => {});
    } else if (mockItems) {
      if (form.editingId) {
        const i = mockItems.findIndex((x) => x.id === form.editingId);
        if (i >= 0) {
          mockItems[i] = {
            ...mockItems[i],
            ...payload,
            persona_name: nameOf(payload.persona),
            color: payload.persona ? colorOf(payload.persona) : null,
          };
        }
      } else {
        mockItems.push({
          id: `mock-${Date.now()}`,
          ...payload,
          persona_name: nameOf(payload.persona),
          color: payload.persona ? colorOf(payload.persona) : null,
          done: false,
          created_at: null,
          source: 'calendar',
          readonly: false,
        });
      }
      setItems([...mockItems]);
    }
    setForm(emptyForm);
  };

  const toggleDone = (e: CalendarEntry) => {
    if (e.readonly) return;
    if (apiOnline) {
      api.updateCalendarEntry(e.id, { done: !e.done }).then(upsert).catch(() => {});
    } else if (mockItems) {
      const m = mockItems.find((x) => x.id === e.id);
      if (m) m.done = !m.done;
      setItems([...mockItems]);
    }
  };

  const remove = (e: CalendarEntry) => {
    if (e.readonly) return;
    if (apiOnline) {
      api.deleteCalendarEntry(e.id)
        .then(() => setItems((prev) => prev.filter((x) => x.id !== e.id)))
        .catch(() => {});
    } else if (mockItems) {
      mockItems = mockItems.filter((x) => x.id !== e.id);
      setItems([...mockItems]);
    }
  };

  const startEdit = (e: CalendarEntry) =>
    setForm({ editingId: e.id, title: e.title, kind: e.kind, time: e.time ?? '', persona: e.persona ?? '', note: e.note });

  const openDay = (iso: string) => {
    setSelected(iso);
    setForm(emptyForm);
  };

  const shiftMonth = (n: number) =>
    setCursor((c) => {
      const d = new Date(c.y, c.m + n, 1);
      return { y: d.getFullYear(), m: d.getMonth() };
    });

  const weekdays = useMemo(() => {
    const monday = new Date(2024, 0, 1); // заведомо понедельник
    return Array.from({ length: 7 }, (_, i) => addDays(monday, i).toLocaleDateString(locale, { weekday: 'short' }));
  }, [locale]);

  const monthLabel = new Date(cursor.y, cursor.m, 1).toLocaleDateString(locale, { month: 'long', year: 'numeric' });
  const dayEntries = selected ? byDate.get(selected) ?? [] : [];
  const selectedLabel = selected
    ? new Date(`${selected}T00:00:00`).toLocaleDateString(locale, { day: 'numeric', month: 'long', weekday: 'short' })
    : '';

  return (
    <div className="cal bracketed">
      <div className="corner tl" />
      <div className="corner tr" />
      <div className="corner bl" />
      <div className="corner br" />

      <div className="cal-head">
        <button type="button" className="btn btn--icon" onClick={() => shiftMonth(-1)} aria-label="−1">‹</button>
        <span className="cal-month">{monthLabel}</span>
        <button type="button" className="btn btn--icon" onClick={() => shiftMonth(1)} aria-label="+1">›</button>
        <button type="button" className="btn btn--ghost cal-today" onClick={() => setCursor({ y: today.getFullYear(), m: today.getMonth() })}>
          {t('calendar.today')}
        </button>
      </div>

      <div className="cal-weekdays">
        {weekdays.map((w) => (
          <span key={w}>{w}</span>
        ))}
      </div>

      <div className="cal-grid">
        {cells.map((d) => {
          const iso = isoDate(d);
          const entries = byDate.get(iso) ?? [];
          const out = d.getMonth() !== cursor.m;
          return (
            <button
              key={iso}
              type="button"
              className={`cal-day${out ? ' cal-day--out' : ''}${iso === todayISO ? ' cal-day--today' : ''}`}
              onClick={() => openDay(iso)}
            >
              <span className="cal-day-num">{d.getDate()}</span>
              <span className="cal-chips">
                {entries.slice(0, 3).map((e) => (
                  <span
                    key={e.id}
                    className={`cal-chip${e.done ? ' cal-chip--done' : ''}`}
                    style={{ '--cal-color': e.color ?? NEUTRAL } as CSSProperties}
                    title={e.title}
                  >
                    {e.time && <b>{e.time} </b>}
                    {e.title}
                  </span>
                ))}
                {entries.length > 3 && <span className="cal-more">{t('calendar.more', { n: entries.length - 3 })}</span>}
              </span>
            </button>
          );
        })}
      </div>

      {/* Модалка дня: записи + форма добавления/правки (порталом в body —
          fixed внутри карточки с transform уезжал бы вместе с ней) */}
      {selected && createPortal(
        <div className="pcreate-overlay" onClick={() => setSelected(null)}>
          <div className="pcreate-panel pcreate-panel--wide bracketed" onClick={(e) => e.stopPropagation()}>
            <div className="corner tl" />
            <div className="corner tr" />
            <div className="corner bl" />
            <div className="corner br" />
            <div className="pcreate-head">
              <span className="pcreate-title">{selectedLabel}</span>
              <span className="badge">CAL</span>
              <button type="button" className="pxe-close" onClick={() => setSelected(null)} aria-label={t('common.close')}>
                ✕
              </button>
            </div>
            <div className="pcreate-body">
              {dayEntries.length === 0 && <div className="cal-empty">{t('calendar.empty')}</div>}
              <ul className="cal-list">
                {dayEntries.map((e) => (
                  <li key={e.id} className="cal-entry">
                    <span className="cal-dot" style={{ '--cal-color': e.color ?? NEUTRAL } as CSSProperties} />
                    <span className="badge">{t(`calendar.kind.${e.kind}`)}</span>
                    {e.time && <span className="cal-entry-time">{e.time}</span>}
                    <span className={e.done ? 'cal-entry-title cal-entry-title--done' : 'cal-entry-title'}>{e.title}</span>
                    {e.persona_name && (
                      <span className="cal-tag" style={{ '--cal-color': e.color ?? NEUTRAL } as CSSProperties}>
                        {e.persona_name}
                      </span>
                    )}
                    <span className="cal-entry-actions">
                      {e.readonly ? (
                        <span className="cal-lock" title={t('calendar.readonlyHint')}>◔</span>
                      ) : (
                        <>
                          <button type="button" className="btn btn--icon" title={t('calendar.toggleDone')} onClick={() => toggleDone(e)}>
                            {e.done ? '↩' : '✓'}
                          </button>
                          <button type="button" className="btn btn--icon" title={t('common.edit')} onClick={() => startEdit(e)}>
                            ✎
                          </button>
                          <button type="button" className="btn btn--icon" title={t('common.delete')} onClick={() => remove(e)}>
                            ✕
                          </button>
                        </>
                      )}
                    </span>
                  </li>
                ))}
              </ul>

              <div className="cal-form">
                <div className="cal-form-title">{form.editingId ? t('calendar.editEntry') : t('calendar.newEntry')}</div>
                <div className="field">
                  <label className="field-label" htmlFor="cal-f-title">{t('calendar.titleLabel')}</label>
                  <input
                    id="cal-f-title"
                    className="input"
                    placeholder={t('calendar.titlePh')}
                    value={form.title}
                    onChange={(e) => setForm({ ...form, title: e.target.value })}
                  />
                </div>
                <div className="field-grid">
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label className="field-label" htmlFor="cal-f-kind">{t('calendar.kind')}</label>
                    <Select
                      id="cal-f-kind"
                      value={form.kind}
                      options={KINDS.map((k) => ({ value: k, label: t(`calendar.kind.${k}`) }))}
                      onChange={(v) => setForm({ ...form, kind: v as CalendarKind })}
                    />
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label className="field-label" htmlFor="cal-f-time">{t('tasks.time')}</label>
                    <input
                      id="cal-f-time"
                      className="input"
                      type="time"
                      value={form.time}
                      onChange={(e) => setForm({ ...form, time: e.target.value })}
                    />
                  </div>
                </div>
                <div className="field-grid" style={{ marginTop: 14 }}>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label className="field-label" htmlFor="cal-f-persona">{t('calendar.personaLabel')}</label>
                    <Select
                      id="cal-f-persona"
                      value={form.persona}
                      options={[
                        { value: '', label: t('calendar.personaNone') },
                        ...personas.map((p) => ({ value: p.id, label: p.name })),
                      ]}
                      onChange={(v) => setForm({ ...form, persona: v })}
                    />
                  </div>
                  {apiOnline && form.persona && todoOptions.length > 0 && (
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label className="field-label" htmlFor="cal-f-todo">{t('calendar.fromTodo')}</label>
                      <Select
                        id="cal-f-todo"
                        value=""
                        options={[
                          { value: '', label: t('calendar.todoPick') },
                          ...todoOptions.map((x) => ({ value: String(x.index), label: x.task })),
                        ]}
                        onChange={(v) => {
                          const item = todoOptions.find((x) => String(x.index) === v);
                          if (item) setForm((f) => ({ ...f, title: item.task, kind: 'todo' }));
                        }}
                      />
                    </div>
                  )}
                </div>
                <div className="field" style={{ marginBottom: 0, marginTop: 14 }}>
                  <label className="field-label" htmlFor="cal-f-note">{t('calendar.noteLabel')}</label>
                  <input
                    id="cal-f-note"
                    className="input"
                    placeholder={t('calendar.notePh')}
                    value={form.note}
                    onChange={(e) => setForm({ ...form, note: e.target.value })}
                  />
                </div>
              </div>
            </div>
            <div className="pcreate-foot">
              {form.editingId && (
                <button type="button" className="btn btn--ghost" onClick={() => setForm(emptyForm)}>
                  {t('common.cancel')}
                </button>
              )}
              <button type="button" className="btn btn--primary" disabled={!form.title.trim()} onClick={submit}>
                {form.editingId ? t('common.save') : t('common.add')}
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}
