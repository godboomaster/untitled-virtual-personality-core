/* Overlay-хранилище записываемых данных персоны: правки, сделанные через
   скин (добавленные/изменённые/удалённые дела, напоминания, факты, выбор
   моделей и провайдеров, флаги фич, параметры генерации и инициативы).
   Живёт поверх моковых данных в localStorage, привязано к id персоны.
   Паттерн — как у skinStore. */

import { useCallback, useEffect, useRef, useState } from 'react';

const PREFIX = 'vpc-overlay:';
const CHANGE_EVENT = 'vpc-overlays-updated';

export interface PersonaOverlay {
  addedTodos: { id: number; text: string; done: boolean }[];
  toggledTodos: number[]; // id моковых дел с перевёрнутым статусом «сделано»
  editedTodos: Record<number, string>; // id → новый текст (моковые дела)
  deletedTodos: number[];
  addedReminders: { id: number; time: string; text: string; repeat: string; active: boolean }[];
  toggledReminders: number[];
  editedReminders: Record<number, { time: string; text: string; repeat: string }>;
  deletedReminders: number[];
  addedFacts: { id: number; category: string; fact: string }[];
  editedFacts: Record<number, { category: string; fact: string }>;
  deletedFacts: number[]; // id удалённых моковых фактов
  stmTrimmed: number; // сколько реплик срезано с хвоста STM-буфера
  deletedStm: number[]; // id поштучно удалённых реплик STM
  addedCourses: { id: number; subject: string; frequency: string }[];
  mainProvider: string | null; // id основного провайдера (null — моковый)
  backupToggled: string[]; // id провайдеров с перевёрнутым статусом «на подхвате»
  toggledFeatures: string[]; // id фич с перевёрнутым флагом
  // параметры самоинициативы (probability — в процентах)
  init: {
    silence?: number;
    probability?: number;
    maxPerDay?: number;
    interval?: number;
    adaptive?: boolean;
    bayes?: boolean;
  };
  models: Record<string, string>; // providerId → модель
  gen: { temperature?: number; maxTokens?: number; topP?: number; stmSize?: number };
}

const EMPTY: PersonaOverlay = {
  addedTodos: [],
  toggledTodos: [],
  editedTodos: {},
  deletedTodos: [],
  addedReminders: [],
  toggledReminders: [],
  editedReminders: {},
  deletedReminders: [],
  addedFacts: [],
  editedFacts: {},
  deletedFacts: [],
  stmTrimmed: 0,
  deletedStm: [],
  addedCourses: [],
  mainProvider: null,
  backupToggled: [],
  toggledFeatures: [],
  init: {},
  models: {},
  gen: {},
};

function load(personaId: string): PersonaOverlay {
  try {
    const raw = localStorage.getItem(PREFIX + personaId);
    if (!raw) return EMPTY;
    return { ...EMPTY, ...(JSON.parse(raw) as Partial<PersonaOverlay>) };
  } catch {
    return EMPTY;
  }
}

// false — не записалось (квота localStorage общая с офлайн-библиотекой
// скинов, или хранилище недоступно): правка не применена
function store(personaId: string, overlay: PersonaOverlay): boolean {
  try {
    localStorage.setItem(PREFIX + personaId, JSON.stringify(overlay));
  } catch {
    return false;
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
  return true;
}

// Память id ушла в архив (персона «с чистого листа»): локальные правки досье забыть
export function forgetOverlay(personaId: string) {
  try {
    if (localStorage.getItem(PREFIX + personaId) === null) return;
    localStorage.removeItem(PREFIX + personaId);
  } catch {
    return; // хранилище недоступно — забывать нечего
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

// Смена id персоны: локальные правки досье переезжают под новый id
export function renameOverlay(oldId: string, newId: string) {
  const raw = localStorage.getItem(PREFIX + oldId);
  if (raw === null) return;
  try {
    localStorage.setItem(PREFIX + newId, raw);
    localStorage.removeItem(PREFIX + oldId);
  } catch {
    return;
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

function nextId(items: { id: number }[]): number {
  return items.reduce((max, i) => Math.max(max, i.id), 0) + 1 + Date.now() % 1000;
}

export interface PersonaOverlayApi extends PersonaOverlay {
  addTodo: (text: string) => void;
  toggleTodo: (id: number) => void;
  updateTodo: (id: number, text: string) => void;
  deleteTodo: (id: number) => void;
  addReminder: (time: string, text: string, repeat: string) => void;
  toggleReminder: (id: number) => void;
  updateReminder: (id: number, value: { time: string; text: string; repeat: string }) => void;
  deleteReminder: (id: number) => void;
  addFact: (category: string, fact: string) => void;
  updateFact: (id: number, category: string, fact: string) => void;
  deleteFact: (id: number) => void;
  trimStm: (count: number) => void;
  deleteStm: (id: number) => void;
  addCourse: (subject: string, frequency: string) => void;
  makeMainProvider: (providerId: string) => void;
  toggleBackup: (providerId: string) => void;
  toggleFeature: (featureId: string) => void;
  setInit: (key: 'silence' | 'probability' | 'maxPerDay' | 'interval' | 'adaptive' | 'bayes', value: number | boolean) => void;
  setModel: (providerId: string, model: string) => void;
  setGen: (key: 'temperature' | 'maxTokens' | 'topP' | 'stmSize', value: number) => void;
}

// onStoreError — правку не удалось сохранить (показать человеку)
export function usePersonaOverlay(personaId: string, onStoreError?: () => void): PersonaOverlayApi {
  const [, setTick] = useState(0);
  const onErrorRef = useRef(onStoreError);
  useEffect(() => {
    onErrorRef.current = onStoreError;
  });

  useEffect(() => {
    const bump = () => setTick((n) => n + 1);
    window.addEventListener(CHANGE_EVENT, bump);
    window.addEventListener('storage', bump);
    return () => {
      window.removeEventListener(CHANGE_EVENT, bump);
      window.removeEventListener('storage', bump);
    };
  }, []);

  const mutate = useCallback(
    (fn: (o: PersonaOverlay) => PersonaOverlay) => {
      if (!store(personaId, fn(load(personaId)))) onErrorRef.current?.();
    },
    [personaId],
  );

  const o = load(personaId);

  return {
    ...o,
    addTodo: (text) =>
      mutate((c) => ({ ...c, addedTodos: [...c.addedTodos, { id: nextId(c.addedTodos), text, done: false }] })),
    toggleTodo: (id) =>
      mutate((c) => {
        const added = c.addedTodos.find((td) => td.id === id);
        if (added) {
          return { ...c, addedTodos: c.addedTodos.map((td) => (td.id === id ? { ...td, done: !td.done } : td)) };
        }
        return {
          ...c,
          toggledTodos: c.toggledTodos.includes(id)
            ? c.toggledTodos.filter((x) => x !== id)
            : [...c.toggledTodos, id],
        };
      }),
    updateTodo: (id, text) =>
      mutate((c) =>
        c.addedTodos.some((td) => td.id === id)
          ? { ...c, addedTodos: c.addedTodos.map((td) => (td.id === id ? { ...td, text } : td)) }
          : { ...c, editedTodos: { ...c.editedTodos, [id]: text } },
      ),
    deleteTodo: (id) =>
      mutate((c) =>
        c.addedTodos.some((td) => td.id === id)
          ? { ...c, addedTodos: c.addedTodos.filter((td) => td.id !== id) }
          : { ...c, deletedTodos: [...c.deletedTodos, id] },
      ),
    addReminder: (time, text, repeat) =>
      mutate((c) => ({
        ...c,
        addedReminders: [...c.addedReminders, { id: nextId(c.addedReminders), time, text, repeat, active: true }],
      })),
    toggleReminder: (id) =>
      mutate((c) => {
        const added = c.addedReminders.find((r) => r.id === id);
        if (added) {
          return { ...c, addedReminders: c.addedReminders.map((r) => (r.id === id ? { ...r, active: !r.active } : r)) };
        }
        return {
          ...c,
          toggledReminders: c.toggledReminders.includes(id)
            ? c.toggledReminders.filter((x) => x !== id)
            : [...c.toggledReminders, id],
        };
      }),
    updateReminder: (id, value) =>
      mutate((c) =>
        c.addedReminders.some((r) => r.id === id)
          ? { ...c, addedReminders: c.addedReminders.map((r) => (r.id === id ? { ...r, ...value } : r)) }
          : { ...c, editedReminders: { ...c.editedReminders, [id]: value } },
      ),
    deleteReminder: (id) =>
      mutate((c) =>
        c.addedReminders.some((r) => r.id === id)
          ? { ...c, addedReminders: c.addedReminders.filter((r) => r.id !== id) }
          : { ...c, deletedReminders: [...c.deletedReminders, id] },
      ),
    addFact: (category, fact) =>
      mutate((c) => ({ ...c, addedFacts: [...c.addedFacts, { id: nextId(c.addedFacts), category, fact }] })),
    updateFact: (id, category, fact) =>
      mutate((c) =>
        c.addedFacts.some((f) => f.id === id)
          ? { ...c, addedFacts: c.addedFacts.map((f) => (f.id === id ? { ...f, category, fact } : f)) }
          : { ...c, editedFacts: { ...c.editedFacts, [id]: { category, fact } } },
      ),
    deleteFact: (id) =>
      mutate((c) =>
        c.addedFacts.some((f) => f.id === id)
          ? { ...c, addedFacts: c.addedFacts.filter((f) => f.id !== id) }
          : { ...c, deletedFacts: [...c.deletedFacts, id] },
      ),
    trimStm: (count) => mutate((c) => ({ ...c, stmTrimmed: c.stmTrimmed + Math.max(0, count) })),
    deleteStm: (id) => mutate((c) => ({ ...c, deletedStm: [...c.deletedStm, id] })),
    addCourse: (subject, frequency) =>
      mutate((c) => ({ ...c, addedCourses: [...c.addedCourses, { id: nextId(c.addedCourses), subject, frequency }] })),
    makeMainProvider: (providerId) => mutate((c) => ({ ...c, mainProvider: providerId })),
    toggleBackup: (providerId) =>
      mutate((c) => ({
        ...c,
        backupToggled: c.backupToggled.includes(providerId)
          ? c.backupToggled.filter((x) => x !== providerId)
          : [...c.backupToggled, providerId],
      })),
    toggleFeature: (featureId) =>
      mutate((c) => ({
        ...c,
        toggledFeatures: c.toggledFeatures.includes(featureId)
          ? c.toggledFeatures.filter((x) => x !== featureId)
          : [...c.toggledFeatures, featureId],
      })),
    setInit: (key, value) => mutate((c) => ({ ...c, init: { ...c.init, [key]: value } })),
    setModel: (providerId, model) => mutate((c) => ({ ...c, models: { ...c.models, [providerId]: model } })),
    setGen: (key, value) => mutate((c) => ({ ...c, gen: { ...c.gen, [key]: value } })),
  };
}
