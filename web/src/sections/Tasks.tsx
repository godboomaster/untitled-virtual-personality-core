import { useEffect, useState } from 'react';
import type { Reminder, TodoItem } from '../mockData';
import { useI18n, useMockData } from '../i18n';
import { api } from '../api';
import type { ReminderEntry, TodoEntry } from '../api';
import { useApiOnline } from '../apiData';
import FormModal from '../components/FormModal';
import InfoButton from '../components/InfoButton';
import Select from '../components/Select';
import { alertDialog } from '../dialogStore';
import { parseReminderWhen } from '../reminderWhen';
import { fmtRecurrence, formFromRecurrence, recurrenceFromForm, REPEAT_KINDS } from '../reminderRepeat';

// «ЧЧ:ММ» с ведущими нулями — для предзаполнения полей формы и сравнения дат
const pad2 = (n: number) => String(n).padStart(2, '0');

// Добавленные через модалки элементы и правки моковых (module-level —
// переживают перемонтирование секции при переключении вкладок досье)
const addedByPersona: Record<string, { reminders: Reminder[]; todos: TodoItem[] }> = {};
const editedReminders: Record<number, Reminder> = {};
const editedTodos: Record<number, TodoItem> = {};
// Переопределения done для моковых задач (чекбокс реально переключает)
const doneOverrides: Record<number, boolean> = {};

function addedFor(personaId: string) {
  return (addedByPersona[personaId] ??= { reminders: [], todos: [] });
}

// Человекочитаемое время из полей даты и времени («03.08, 10:00»)
function composeTime(date: string, time: string, noDateLabel: string): string {
  const d = date ? date.split('-').reverse().join('.') : '';
  if (d && time) return `${d}, ${time}`;
  if (d) return d;
  if (time) return time;
  return noDateLabel;
}

// Человекочитаемый повтор: «по дням недели» раскрывается в дни
function composeRepeat(repeat: string, days: string[], customDaysLabel: string): string {
  if (repeat === customDaysLabel) return days.length ? days.join(', ') : customDaysLabel;
  return repeat;
}

// Разбор мокового повтора обратно в поля формы (для предзаполнения при редактировании)
function parseRepeat(repeat: string, repeatOptions: string[], weekdays: string[], customDaysLabel: string): { kind: string; days: string[] } {
  if (repeatOptions.includes(repeat)) return { kind: repeat, days: [] };
  const parts = repeat.split(',').map((s) => s.trim());
  if (parts.length && parts.every((p) => weekdays.includes(p))) return { kind: customDaysLabel, days: parts };
  return { kind: repeat, days: [] }; // нестандартный — добавится как опция
}

interface TasksProps {
  personaId?: string; // фиксированная персона (модалка «Досье») — без табов персон
  embedded?: boolean; // встраивание без заголовка раздела
}

export default function Tasks({ personaId: fixedId, embedded }: TasksProps) {
  const { lang, t } = useI18n();
  const { personas, remindersByPersona, todosByPersona } = useMockData();
  const apiOnline = useApiOnline();

  // Варианты повтора напоминания и дни недели — из словаря текущей локали
  const repeatOptions = t('tasks.repeatOptions').split('|');
  const weekdays = t('tasks.weekdays').split('|');
  const customDaysLabel = repeatOptions[repeatOptions.length - 1];

  // Напоминания и задачи индивидуальны для каждой персоны
  const [selectedId, setSelectedId] = useState(() => fixedId ?? personas[0].id);
  const persona = personas.find((p) => p.id === (fixedId ?? selectedId)) ?? personas[0];

  // Модалка: kind — что редактируем, id — null для нового элемента
  const [modal, setModal] = useState<{ kind: 'reminder' | 'todo'; id: number | null } | null>(null);
  const [reminderText, setReminderText] = useState('');
  const [reminderDate, setReminderDate] = useState('');
  const [reminderTime, setReminderTime] = useState('');
  const [reminderRepeat, setReminderRepeat] = useState(() => repeatOptions[0]);
  const [reminderDays, setReminderDays] = useState<string[]>([]);
  const [todoText, setTodoText] = useState('');
  const [, setVersion] = useState(0); // триггер перерендера после правок

  // Данные бэкенда: дела и напоминания выбранной персоны
  const [apiTodos, setApiTodos] = useState<TodoEntry[] | null>(null);
  const [apiReminders, setApiReminders] = useState<ReminderEntry[] | null>(null);

  useEffect(() => {
    setApiTodos(null);
    setApiReminders(null);
    if (!apiOnline) return;
    const id = persona.id;
    let stale = false;
    api.getTodo(id).then((r) => !stale && setApiTodos(r.items)).catch(() => {});
    api.getReminders(id).then((r) => !stale && setApiReminders(r.items)).catch(() => {});
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id]);

  // unix-секунды → «03.08.2026, 10:00»
  const fmtDateTime = (ts: number) =>
    new Date(ts * 1000).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US', {
      day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });

  const added = addedFor(persona.id);
  const reminders: Reminder[] = apiOnline
    ? (apiReminders ?? []).map((r) => ({
        id: r.index,
        text: r.task,
        time: r.trigger_at ? fmtDateTime(r.trigger_at) : t('tasks.noDate'),
        repeat: fmtRecurrence(r.recurrence, t),
        active: r.active !== false,
      }))
    : [
        ...(remindersByPersona[persona.id] ?? []).map((r) => editedReminders[r.id] ?? r),
        ...added.reminders,
      ];
  const todos: TodoItem[] = apiOnline
    ? (apiTodos ?? []).map((x) => ({ id: x.index, text: x.task, done: false }))
    : [
        ...(todosByPersona[persona.id] ?? []).map((t) => {
          const edited = editedTodos[t.id] ?? t;
          // done: переопределение кликом важнее мокового значения
          return doneOverrides[t.id] !== undefined ? { ...edited, done: doneOverrides[t.id] } : edited;
        }),
        ...added.todos,
      ];

  // Переключить выполнение задачи (моковой или добавленной)
  const toggleTodo = (t: TodoItem) => {
    // В ядре «сделано» = пункт удаляется из списка
    if (apiOnline) {
      api.removeTodo(persona.id, t.id).then((r) => setApiTodos(r.items)).catch(() => {});
      return;
    }
    const idx = added.todos.findIndex((x) => x.id === t.id);
    if (idx >= 0) added.todos[idx] = { ...added.todos[idx], done: !added.todos[idx].done };
    else doneOverrides[t.id] = !t.done;
    setVersion((v) => v + 1);
  };

  // Удаление напоминания (API) — по стабильному id (номер строки мог сдвинуться)
  const deleteReminder = (r: Reminder) => {
    if (!apiOnline) return;
    const raw = apiReminders?.find((x) => x.index === r.id);
    if (!raw) {
      api.getReminders(persona.id).then((res) => setApiReminders(res.items)).catch(() => {});
      void alertDialog({ message: t('skin.actStale') });
      return;
    }
    api.cancelReminderById(persona.id, raw.id).then((res) => setApiReminders(res.items)).catch(() => {});
  };

  // Пауза/продолжение напоминания: на паузе оно не срабатывает
  const toggleReminder = (r: Reminder) => {
    if (apiOnline) {
      const raw = apiReminders?.find((x) => x.index === r.id);
      if (!raw) {
        api.getReminders(persona.id).then((res) => setApiReminders(res.items)).catch(() => {});
        void alertDialog({ message: t('skin.actStale') });
        return;
      }
      api
        .updateReminder(persona.id, raw.id, { active: !r.active })
        .then((res) => setApiReminders(res.items))
        .catch((e) => alertDialog({ message: e instanceof Error ? e.message : String(e) }));
      return;
    }
    const idx = added.reminders.findIndex((x) => x.id === r.id);
    if (idx >= 0) added.reminders[idx] = { ...added.reminders[idx], active: !r.active };
    else editedReminders[r.id] = { ...r, active: !r.active };
    setVersion((v) => v + 1);
  };

  // Удаление дела (API)
  const deleteTodo = (td: TodoItem) => {
    if (!apiOnline) return;
    api.removeTodo(persona.id, td.id).then((r) => setApiTodos(r.items)).catch(() => {});
  };

  // Открыть модалку напоминания: пустую (новое) или предзаполненную (редактирование)
  const openReminderModal = (r?: Reminder) => {
    if (r) {
      let { kind, days } = parseRepeat(r.repeat, repeatOptions, weekdays, customDaysLabel);
      setReminderText(r.text);
      if (apiOnline) {
        // Дата/время — из точного trigger_at сырой записи, а не из
        // отформатированной строки списка (иначе при сохранении без правки
        // срок сместится на округление отображения)
        const raw = apiReminders?.find((x) => x.index === r.id);
        // Повтор — из расписания сервера, а не из подписи списка
        const form = formFromRecurrence(raw?.recurrence ?? null);
        kind = repeatOptions[REPEAT_KINDS.indexOf(form.kind)] ?? repeatOptions[0];
        days = form.days.map((d) => weekdays[d]).filter(Boolean);
        if (raw?.trigger_at) {
          const d = new Date(raw.trigger_at * 1000);
          setReminderDate(`${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`);
          setReminderTime(`${pad2(d.getHours())}:${pad2(d.getMinutes())}`);
        } else {
          setReminderDate('');
          setReminderTime('');
        }
      } else {
        setReminderTime(r.time.match(/(\d{1,2}:\d{2})/)?.[1] ?? '');
        setReminderDate('');
      }
      setReminderRepeat(kind);
      setReminderDays(days);
      setModal({ kind: 'reminder', id: r.id });
    } else {
      setReminderText('');
      setReminderDate('');
      setReminderTime('');
      setReminderRepeat(repeatOptions[0]);
      setReminderDays([]);
      setModal({ kind: 'reminder', id: null });
    }
  };

  const openTodoModal = (t?: TodoItem) => {
    setTodoText(t?.text ?? '');
    setModal({ kind: 'todo', id: t?.id ?? null });
  };

  const submitReminder = () => {
    const text = reminderText.trim();
    if (!text || !modal) return;
    if (apiOnline) {
      // Дату/время разбираем через parseReminderWhen (локальные даты, «завтра»,
      // ISO — не new Date(строка), которая «01.10» читает как 2001 год, а ISO
      // «2026-10-01» — как полночь UTC); нет ни даты, ни времени — через час.
      const locale = lang === 'en' ? 'en' : 'ru';
      const when = parseReminderWhen(reminderDate, reminderTime, locale);
      if (!when.ok) {
        const key = when.error === 'date' ? 'skin.actBadDate' : when.error === 'time' ? 'skin.actBadTime' : 'skin.actPast';
        const vars = when.error === 'date' ? { v: reminderDate } : when.error === 'time' ? { v: reminderTime } : undefined;
        void alertDialog({ message: t(key, vars) });
        return;
      }
      // Повтор из формы → расписание сервера (время сервер берёт из срока
      // напоминания — по часовому поясу пользователя, а не браузера)
      const kindIdx = repeatOptions.indexOf(reminderRepeat);
      const recurrence = recurrenceFromForm(
        REPEAT_KINDS[kindIdx >= 0 ? kindIdx : 0] ?? 'once',
        reminderDays.map((d) => weekdays.indexOf(d)).filter((d) => d >= 0),
      );
      if (recurrence === 'no-days') {
        void alertDialog({ message: t('tasks.pickDays') });
        return;
      }
      if (modal.id != null) {
        // Правка на месте по стабильному id (атомарно)
        const raw = apiReminders?.find((x) => x.index === modal.id);
        if (!raw) {
          setModal(null);
          api.getReminders(persona.id).then((r) => setApiReminders(r.items)).catch(() => {});
          void alertDialog({ message: t('skin.actStale') });
          return;
        }
        // Повтор отправляем всегда: выбранный в форме заменяет прежний,
        // «разовое» (null) снимает его
        const patch: { task: string; trigger_at?: number; recurrence: typeof recurrence } = { task: text, recurrence };
        // Поля даты/времени были заполнены (из raw.trigger_at или человеком) —
        // пересчитываем срок; пусты (напоминание без trigger_at) — не трогаем
        if (reminderDate.trim() || reminderTime.trim()) patch.trigger_at = Math.round(when.at.getTime() / 1000);
        api
          .updateReminder(persona.id, raw.id, patch)
          .then((r) => setApiReminders(r.items))
          .catch((e) => alertDialog({ message: e instanceof Error ? e.message : String(e) }));
      } else {
        const delay = Math.max(60, Math.round((when.at.getTime() - Date.now()) / 1000));
        api
          .addReminder(persona.id, text, delay, recurrence)
          .then((r) => setApiReminders(r.items))
          .catch((e) => alertDialog({ message: e instanceof Error ? e.message : String(e) }));
      }
      setModal(null);
      return;
    }
    const item: Reminder = {
      id: modal.id ?? Date.now(),
      text,
      time: composeTime(reminderDate, reminderTime, t('tasks.noDate')),
      repeat: composeRepeat(reminderRepeat, reminderDays, customDaysLabel),
      active: true,
    };
    if (modal.id != null) {
      const idx = added.reminders.findIndex((r) => r.id === modal.id);
      if (idx >= 0) added.reminders[idx] = item;
      else editedReminders[modal.id] = item; // правка мокового элемента
    } else {
      added.reminders.push(item);
    }
    setVersion((v) => v + 1);
    setModal(null);
  };

  const submitTodo = () => {
    const text = todoText.trim();
    if (!text || !modal) return;
    if (apiOnline) {
      // Правка = удалить старый пункт + добавить новый
      if (modal.id != null) {
        api.removeTodo(persona.id, modal.id)
          .then(() => api.addTodo(persona.id, text))
          .then((r) => setApiTodos(r.items))
          .catch(() => {});
      } else {
        api.addTodo(persona.id, text).then((r) => setApiTodos(r.items)).catch(() => {});
      }
      setModal(null);
      return;
    }
    if (modal.id != null) {
      const idx = added.todos.findIndex((t) => t.id === modal.id);
      if (idx >= 0) added.todos[idx] = { ...added.todos[idx], text };
      else {
        const prev = editedTodos[modal.id] ?? todos.find((t) => t.id === modal.id);
        if (prev) editedTodos[modal.id] = { ...prev, text };
      }
    } else {
      added.todos.push({ id: Date.now(), text, done: false });
    }
    setVersion((v) => v + 1);
    setModal(null);
  };

  const toggleDay = (d: string) => {
    setReminderDays((prev) => (prev.includes(d) ? prev.filter((x) => x !== d) : [...prev, d]));
  };

  // Опции select'а повтора: нестандартное значение из мока добавляем как есть
  const repeatChoices = repeatOptions.includes(reminderRepeat)
    ? repeatOptions
    : [...repeatOptions, reminderRepeat];

  return (
    <div className={embedded ? undefined : 'section'}>
      {!embedded && (
        <div className="section-header">
          <div>
            <h1 className="section-title">{t('tasks.title')}</h1>
            <p className="section-subtitle">{t('tasks.subtitle')}</p>
          </div>
          <span className="badge badge--active">{persona.name}</span>
        </div>
      )}

      {/* Переключатель персон (скрыт при фиксированной персоне) */}
      {!fixedId && (
        <div className="tabs room-persona-tabs">
          {personas.map((p) => (
            <button
              key={p.id}
              type="button"
              className={`tab ${p.id === persona.id ? 'tab--active' : ''}`}
              onClick={() => setSelectedId(p.id)}
            >
              {p.name}
            </button>
          ))}
        </div>
      )}

      <div className="two-col">
        {/* Напоминания */}
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              {t('tasks.reminders')}
              <InfoButton helpKey="tasks.reminders" />
            </h2>
            <button className="btn btn--primary" onClick={() => openReminderModal()}>{t('tasks.addReminder')}</button>
          </div>
          <ul className="memory-list">
            {reminders.map((r, i) => (
              <li key={r.id} className="reminder-item stagger-item" style={{ animationDelay: `${i * 50}ms` }}>
                <div className="reminder-main">
                  <div className="memory-item-text">{r.text}</div>
                  <div className="reminder-meta">
                    {r.time} · {r.repeat}
                    <InfoButton helpKey="tasks.reminderRepeat" />
                  </div>
                </div>
                <div className="reminder-side">
                  <label className="switch">
                    <input type="checkbox" checked={r.active} onChange={() => toggleReminder(r)} />
                    <span className="switch-slider" />
                  </label>
                  <InfoButton helpKey="tasks.reminderSwitch" />
                  <button className="btn btn--icon" title={t('common.edit')} onClick={() => openReminderModal(r)}>✎</button>
                  <button className="btn btn--icon" title={t('common.delete')} onClick={() => deleteReminder(r)}>✕</button>
                </div>
              </li>
            ))}
          </ul>
        </div>

        {/* To-do */}
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              {t('tasks.todo')}
              <InfoButton helpKey="tasks.todo" />
            </h2>
            <button className="btn btn--primary" onClick={() => openTodoModal()}>{t('tasks.addReminder')}</button>
          </div>
          <ul className="memory-list">
            {todos.map((todo, i) => (
              <li key={todo.id} className="todo-item stagger-item" style={{ animationDelay: `${i * 50}ms` }}>
                <label className="todo-label">
                  <input type="checkbox" checked={todo.done} onChange={() => toggleTodo(todo)} />
                  <span className={todo.done ? 'todo-text todo-text--done' : 'todo-text'}>{todo.text}</span>
                </label>
                <button className="btn btn--icon" title={t('common.edit')} onClick={() => openTodoModal(todo)}>✎</button>
                <button className="btn btn--icon" title={t('common.delete')} onClick={() => deleteTodo(todo)}>✕</button>
              </li>
            ))}
          </ul>
          <div className="todo-progress">
            {t('tasks.progress', { done: todos.filter((t) => t.done).length, total: todos.length })}
            <InfoButton helpKey="tasks.todoProgress" />
          </div>
        </div>
      </div>

      {/* Модалка напоминания: добавление и редактирование */}
      {modal?.kind === 'reminder' && (
        <FormModal
          title={modal.id != null ? t('tasks.editReminder') : t('tasks.newReminder')}
          badge="REM_01"
          submitLabel={modal.id != null ? t('common.save') : t('common.add')}
          submitDisabled={!reminderText.trim()}
          onSubmit={submitReminder}
          onClose={() => setModal(null)}
        >
          <div className="field">
            <label className="field-label" htmlFor="task-rem-text">{t('tasks.textLabel')}</label>
            <input
              id="task-rem-text"
              className="input"
              placeholder={t('tasks.reminderPh')}
              value={reminderText}
              onChange={(e) => setReminderText(e.target.value)}
              autoFocus
            />
          </div>
          <div className="field-grid">
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="field-label" htmlFor="task-rem-date">{t('tasks.date')}</label>
              <input
                id="task-rem-date"
                className="input"
                type="date"
                value={reminderDate}
                onChange={(e) => setReminderDate(e.target.value)}
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="field-label" htmlFor="task-rem-time">{t('tasks.time')}</label>
              <input
                id="task-rem-time"
                className="input"
                type="time"
                value={reminderTime}
                onChange={(e) => setReminderTime(e.target.value)}
              />
            </div>
          </div>
          <div className="field" style={{ marginBottom: 0, marginTop: 14 }}>
            <label className="field-label" htmlFor="task-rem-repeat">{t('tasks.repeat')}</label>
            <Select
              id="task-rem-repeat"
              value={reminderRepeat}
              options={repeatChoices.map((r) => ({ value: r, label: r }))}
              onChange={setReminderRepeat}
            />
          </div>
          {reminderRepeat === customDaysLabel && (
            <div className="day-chips">
              {weekdays.map((d) => (
                <button
                  key={d}
                  type="button"
                  className={`day-chip ${reminderDays.includes(d) ? 'day-chip--on' : ''}`}
                  onClick={() => toggleDay(d)}
                >
                  {d}
                </button>
              ))}
            </div>
          )}
        </FormModal>
      )}

      {/* Модалка to-do: добавление и редактирование */}
      {modal?.kind === 'todo' && (
        <FormModal
          title={modal.id != null ? t('tasks.editTodo') : t('tasks.newTodo')}
          badge="TODO_01"
          submitLabel={modal.id != null ? t('common.save') : t('common.add')}
          submitDisabled={!todoText.trim()}
          onSubmit={submitTodo}
          onClose={() => setModal(null)}
        >
          <div className="field" style={{ marginBottom: 0 }}>
            <label className="field-label" htmlFor="task-todo-text">{t('tasks.todoTextLabel')}</label>
            <input
              id="task-todo-text"
              className="input"
              placeholder={t('tasks.todoPh')}
              value={todoText}
              onChange={(e) => setTodoText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && submitTodo()}
              autoFocus
            />
          </div>
        </FormModal>
      )}
    </div>
  );
}
