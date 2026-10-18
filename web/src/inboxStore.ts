import { useEffect, useReducer } from 'react';
import { api, ApiError, isNativeApp } from './api';
import { OfflineLlm } from './offline/plugin';
import type { AnswerOptions, PendingReply, TaskCard } from './api';
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
// persona id → карточка задачи агента (живая или недавний итог)
let taskCards: Record<string, TaskCard | null> = {};
// persona id → last_ts СЕРВЕРА как есть (без локальных touchActivity): метка
// последнего сообщения STM, тот же float, что timestamp реплики в истории.
// lastTs выше для сортировки смешивает её с часами браузера — сравнивать
// со временем истории можно только эту
let serverLastTs: Record<string, number> = {};
// persona id → реплики с других устройств, на которые персона сейчас
// отвечает. gone — когда сервер перестал их отдавать (ответ лёг в историю):
// пузырь держится до перечитки истории (settlePending), иначе он пропадал бы
// на миг между опросом и перечиткой
export interface PendingItem extends PendingReply {
  gone?: number;
}
let pending: Record<string, PendingItem[]> = {};
const PENDING_GONE_MS = 30000;
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
  return { messages, unread, generating, lastTs, serverLastTs, controlMode, answerOptions, taskCards, pending };
}

// Реплики в обработке из ответа inbox: новые — в список, пропавшие — с
// отметкой gone (снимет перечитка истории или PENDING_GONE_MS)
function setPending(persona: string, live: PendingReply[]) {
  const now = Date.now();
  const same = (a: PendingReply, b: PendingReply) => a.ts === b.ts && a.text === b.text;
  const prev = pending[persona] ?? [];
  const kept = prev
    .filter((p) => !live.some((l) => same(l, p)))
    .map((p) => (p.gone ? p : { ...p, gone: now }))
    .filter((p) => now - (p.gone ?? now) < PENDING_GONE_MS);
  const next = [...live, ...kept].sort((a, b) => a.ts - b.ts);
  if (JSON.stringify(next) === JSON.stringify(prev)) return;
  pending = { ...pending, [persona]: next };
  emit();
}

// История перечитана: ответившие реплики уже в ней — их копии больше не нужны
export function settlePending(persona: string) {
  const list = pending[persona];
  if (!list?.some((p) => p.gone)) return;
  pending = { ...pending, [persona]: list.filter((p) => !p.gone) };
  emit();
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

// Карточка задачи агента (ответ /api/chat, поллинг inbox; null — нет)
export function setTaskCard(persona: string, value: TaskCard | null) {
  if (JSON.stringify(taskCards[persona] ?? null) === JSON.stringify(value)) return;
  taskCards = { ...taskCards, [persona]: value };
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
// Долгий опрос изменений (useInboxPolling): сколько ядро держит пустой
// ответ и пауза после сбоя
const CHANGES_WAIT_SEC = 25;
const CHANGES_RETRY_MS = 5000;
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
  // Персон с телефона (offline/localPersonas.ts) на ядре ещё нет — их inbox там 404
  const ids = personas.filter((p) => !p.local).map((p) => p.id).join(',');
  useEffect(() => {
    if (!apiOnline || !ids) return;
    let stop = false;
    // Опросы одной персоны идут внахлёст (тик, быстрый опрос, сигнал
    // изменений): состояние берём только из самого свежего ответа, иначе
    // запоздавший старый снова включал бы «печатает». Сообщения — из любого:
    // сервер отдал их этому клиенту один раз
    const sent: Record<string, number> = {};
    const applied: Record<string, number> = {};
    const pollId = (id: string) => {
      // focused — heartbeat активности вкладки: гейт фоновой работы бота.
      // Только для персоны ОТКРЫТОГО чата: поллер опрашивает всех, и общий
      // флаг отмечал бы присутствие сразу у всех персон (см. presence.ts)
      const focusedId = tabActive() ? getFocusedPersona() : null;
      const n = (sent[id] = (sent[id] ?? 0) + 1);
      api
        .getInbox(id, id === focusedId)
        .then((r) => {
          if (stop) return;
          if (n > (applied[id] ?? 0)) {
            applied[id] = n;
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
            // Карточка задачи — обновляется на месте, пока агент работает
            if ('task' in r) setTaskCard(id, r.task ?? null);
            // Реплики с других устройств, на которые персона сейчас отвечает
            if (Array.isArray(r.pending)) setPending(id, r.pending);
            // Серверная метка последнего сообщения — по ней чат догружает
            // историю, если STM дописан позже, чем её прочитали (поздний ответ)
            const srv = typeof r.last_ts === 'number' ? r.last_ts : 0;
            if ((serverLastTs[id] ?? 0) !== srv) {
              serverLastTs = { ...serverLastTs, [id]: srv };
              emit();
            }
            // Свежесть переписки с бэкенда (сортировка списка персон)
            if (r.last_ts) touchActivity(id, r.last_ts);
          }
          if (r.messages.length === 0) return;
          // Напоминание пришло от ядра — будильнику телефона оно больше не нужно
          const refs = r.messages.map((m) => m.ref).filter((x): x is string => !!x);
          if (refs.length && isNativeApp()) void OfflineLlm.markDelivered({ refs }).catch(() => {});
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
    // Долгий опрос изменений: ядро отвечает, как только у персоны началась
    // или кончилась генерация, что-то легло в историю или пришло фоновое
    // сообщение, — эти персоны опрашиваются сразу, а не на тике раз в 15 с.
    // Тик остаётся подстраховкой; старое ядро без /api/changes (404) — только он
    const abort = new AbortController();
    void (async () => {
      let since: number | null = null;
      let epoch = '';
      while (!stop) {
        try {
          const r = await api.waitChanges(since, epoch, CHANGES_WAIT_SEC, abort.signal);
          if (stop) return;
          // Первый ответ — только номер: всех и так опросили при старте
          if (since != null) {
            if (r.personas == null) poll();
            else r.personas.forEach((id) => {
              if (known.has(id)) pollId(id);
            });
          }
          since = r.version;
          epoch = r.epoch;
        } catch (e) {
          if (stop || (e instanceof ApiError && e.status === 404)) return;
          // Номер не сбрасываем: ядро отдаст всё, что изменилось за время
          // сбоя, а после его перезапуска (другой epoch) — «опросить всех»
          await new Promise((res) => setTimeout(res, CHANGES_RETRY_MS));
        }
      }
    })();
    return () => {
      stop = true;
      abort.abort();
      clearInterval(timer);
      pollOne = null;
      armFastPoll(); // pollOne снят — таймер быстрого опроса гасится
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, ids]);
}
