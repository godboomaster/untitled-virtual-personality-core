import { useEffect, useReducer } from 'react';
import { api } from './api';
import type { AnswerOptions } from './api';
import { notifyBotMessage } from './notifications';
import { getFocusedPersona, tabActive } from './presence';
import type { Persona } from './mockData';

/* Фоновые сообщения персон (напоминания, самоинициатива, уроки):
   единый поллер на всё приложение (поднимается в App, работает на любом
   экране), сессионное хранилище сообщений и счётчики непрочитанных.
   Чат рендерит сообщения из стора; непрочитанные гасятся, когда
   соответствующий чат открыт. */

export interface InboxItem {
  id: number;
  text: string;
  ts: number; // unix-секунды
}

let messages: Record<string, InboxItem[]> = {}; // persona id → сообщения сессии
let unread: Record<string, number> = {}; // persona id → сколько непрочитанных
let generating: Record<string, boolean> = {}; // persona id → сейчас генерирует ответ
let lastTs: Record<string, number> = {}; // persona id → ts последнего сообщения в чате
let controlMode: Record<string, boolean> = {}; // persona id → режим управления (computer control)
// persona id → кнопки ответа на вопрос режима управления, которого ждёт чат
let answerOptions: Record<string, AnswerOptions | null> = {};
// persona id → last_ts СЕРВЕРА как есть (без локальных touchActivity): метка
// последнего сообщения STM, тот же float, что timestamp реплики в истории.
// lastTs выше для сортировки смешивает её с часами браузера — сравнивать
// со временем истории можно только эту
let serverLastTs: Record<string, number> = {};
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

// Подписка на хранилище: фоновые сообщения и непрочитанные по персонам
export function useInbox() {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    listeners.add(force);
    return () => {
      listeners.delete(force);
    };
  }, []);
  return { messages, unread, generating, lastTs, serverLastTs, controlMode, answerOptions };
}

// Обновить флаг режима управления (из ответа /api/chat или поллинга inbox):
// в этом режиме фронт шлёт сообщения без «реалистичной» паузы-дебаунса
export function setControlMode(persona: string, value: boolean) {
  if ((controlMode[persona] ?? false) === value) return;
  controlMode = { ...controlMode, [persona]: value };
  emit();
}

// Кнопки ответа на вопрос режима управления (ответ /api/chat, поллинг inbox;
// null — снять: человек ответил или вопроса больше нет)
export function setAnswerOptions(persona: string, value: AnswerOptions | null) {
  if (JSON.stringify(answerOptions[persona] ?? null) === JSON.stringify(value)) return;
  answerOptions = { ...answerOptions, [persona]: value };
  emit();
}

// Персона с самой свежей перепиской среди ids (первая в списке чатов);
// null — опрос ещё не принёс ни одной метки
export function latestActivePersona(ids: string[]): string | null {
  let best: string | null = null;
  let bestTs = 0;
  for (const id of ids) {
    const ts = lastTs[id] ?? 0;
    if (ts > bestTs) {
      best = id;
      bestTs = ts;
    }
  }
  return best;
}

// Отметить свежую активность в чате персоны (для сортировки списка персон по новизне)
export function touchActivity(persona: string, ts: number) {
  if (!ts || (lastTs[persona] ?? 0) >= ts) return;
  lastTs = { ...lastTs, [persona]: ts };
  emit();
}

// Гасит флаг «печатает» сразу по завершении обмена, не дожидаясь тика поллера —
// иначе индикатор висит лишние секунды
export function setGenerating(persona: string, value: boolean) {
  if ((generating[persona] ?? false) === value) return;
  generating = { ...generating, [persona]: value };
  emit();
}

// Текущий серверный last_ts персоны — для замыканий, где значение из рендера уже устарело
export function getServerLastTs(persona: string): number {
  return serverLastTs[persona] ?? 0;
}

/* Быстрый опрос: пока в открытом чате персона генерирует ответ, её inbox
   опрашивается каждые FAST_POLL_MS, а не раз в 15 с — поздний ответ
   (сервер дописал STM после перезагрузки страницы) появляется за секунды.
   Ускоряется только одна персона (открытого чата), остальные — обычный тик. */
const FAST_POLL_MS = 3000;
let fastPersona: string | null = null;
let fastTimer: ReturnType<typeof setInterval> | null = null;
// Опрос одной персоны — выставляет живой поллер (useInboxPolling), пока он поднят
let pollOne: ((id: string) => void) | null = null;

function armFastPoll() {
  if (fastTimer) clearInterval(fastTimer);
  fastTimer = null;
  const id = fastPersona;
  if (!id || !pollOne) return;
  fastTimer = setInterval(() => pollOne?.(id), FAST_POLL_MS);
}

// Включить/выключить быстрый опрос персоны (null — выключить)
export function setFastPoll(persona: string | null) {
  if (fastPersona === persona) return;
  fastPersona = persona;
  armFastPoll();
}

// Опросить inbox персоны вне расписания (возврат фокуса, конец/обрыв стрима)
export function pollInboxNow(persona: string) {
  pollOne?.(persona);
}

// Отметить персону прочитанной (её чат сейчас открыт)
export function markRead(persona: string) {
  if (!unread[persona]) return;
  unread = { ...unread, [persona]: 0 };
  emit();
}

// Убрать сообщения, уже попавшие в STM (после перечитывания истории — иначе дубли)
export function pruneInbox(persona: string, inStm: Set<string>) {
  const list = messages[persona];
  if (!list?.length) return;
  const kept = list.filter((m) => !inStm.has(m.text));
  if (kept.length === list.length) return;
  messages = { ...messages, [persona]: kept };
  emit();
}

// Единый поллер inbox'ов всех персон. Вызывать один раз — в App.
export function useInboxPolling(apiOnline: boolean, personas: Persona[]) {
  const ids = personas.map((p) => p.id).join(',');
  useEffect(() => {
    if (!apiOnline || !ids) return;
    let stop = false;
    const pollId = (id: string) => {
      // focused — heartbeat активности вкладки: гейт фоновой работы бота.
      // Только для персоны ОТКРЫТОГО чата: поллер опрашивает всех, и общий
      // флаг отмечал бы присутствие сразу у всех персон (см. presence.ts)
      const focusedId = tabActive() ? getFocusedPersona() : null;
      api
        .getInbox(id, id === focusedId)
        .then((r) => {
          if (stop) return;
          // Флаг «печатает» с бэкенда (генерация переживает перезагрузку страницы)
          const gen = r.generating === true;
          if ((generating[id] ?? false) !== gen) {
            generating = { ...generating, [id]: gen };
            emit();
          }
          // Режим управления — гасит дебаунс-паузу отправки в чате
          if (typeof r.control_mode === 'boolean') setControlMode(id, r.control_mode);
          // Кнопки ответа — состояние ожидания живёт в боте и переживает
          // перезагрузку страницы
          if ('answer_options' in r) setAnswerOptions(id, r.answer_options ?? null);
          // Серверная метка последнего сообщения — по ней чат догружает
          // историю, если STM дописан позже, чем её прочитали (поздний ответ)
          const srv = typeof r.last_ts === 'number' ? r.last_ts : 0;
          if ((serverLastTs[id] ?? 0) !== srv) {
            serverLastTs = { ...serverLastTs, [id]: srv };
            emit();
          }
          // Свежесть переписки с бэкенда (сортировка списка персон)
          if (r.last_ts) touchActivity(id, r.last_ts);
          if (r.messages.length === 0) return;
          const items: InboxItem[] = r.messages.map((m, i) => ({ id: Date.now() + i, text: m.text, ts: m.ts }));
          messages = { ...messages, [id]: [...(messages[id] ?? []), ...items] };
          unread = { ...unread, [id]: (unread[id] ?? 0) + items.length };
          emit();
          // Уведомление о фоновых сообщениях (системное + звук), когда вкладка
          // не в фокусе; одно на персону за тик — по последнему тексту, чтобы
          // не проигрывать звук подряд на каждое сообщение пачки
          const personaName = personas.find((p) => p.id === id)?.name ?? id;
          const last = items[items.length - 1];
          notifyBotMessage(id, personaName, last.text, last.ts);
        })
        .catch(() => {});
    };
    const known = new Set(ids.split(','));
    const poll = () => known.forEach(pollId);
    // Внеплановый/быстрый опрос — только известных поллеру персон
    pollOne = (id) => {
      if (known.has(id)) pollId(id);
    };
    armFastPoll();
    poll();
    const timer = setInterval(poll, 15000);
    return () => {
      stop = true;
      clearInterval(timer);
      pollOne = null;
      armFastPoll(); // pollOne снят — таймер быстрого опроса гасится
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, ids]);
}
