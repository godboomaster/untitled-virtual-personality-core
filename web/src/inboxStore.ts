import { useEffect, useReducer } from 'react';
import { api } from './api';
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
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

/** Подписка на хранилище: фоновые сообщения и непрочитанные по персонам */
export function useInbox() {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    listeners.add(force);
    return () => {
      listeners.delete(force);
    };
  }, []);
  return { messages, unread, generating, lastTs, controlMode };
}

/** Обновить флаг режима управления (из ответа /api/chat или поллинга inbox):
 * в режиме управления фронт шлёт сообщения без «реалистичной» паузы-дебаунса */
export function setControlMode(persona: string, value: boolean) {
  if ((controlMode[persona] ?? false) === value) return;
  controlMode = { ...controlMode, [persona]: value };
  emit();
}

/** Отметить свежую активность в чате персоны (сортировка списка по новизне) */
export function touchActivity(persona: string, ts: number) {
  if (!ts || (lastTs[persona] ?? 0) >= ts) return;
  lastTs = { ...lastTs, [persona]: ts };
  emit();
}

/** Гашение флага «печатает» при локальном завершении обмена: ответ уже у нас,
 * не ждём следующий тик поллера (иначе индикатор висит лишние секунды) */
export function setGenerating(persona: string, value: boolean) {
  if ((generating[persona] ?? false) === value) return;
  generating = { ...generating, [persona]: value };
  emit();
}

/** Отметить персону прочитанной (её чат сейчас открыт) */
export function markRead(persona: string) {
  if (!unread[persona]) return;
  unread = { ...unread, [persona]: 0 };
  emit();
}

/** Убрать сообщения, уже попавшие в STM (после перечитывания истории — иначе дубли) */
export function pruneInbox(persona: string, inStm: Set<string>) {
  const list = messages[persona];
  if (!list?.length) return;
  const kept = list.filter((m) => !inStm.has(m.text));
  if (kept.length === list.length) return;
  messages = { ...messages, [persona]: kept };
  emit();
}

/** Единый поллер inbox'ов всех персон. Вызывать один раз — в App. */
export function useInboxPolling(apiOnline: boolean, personas: Persona[]) {
  const ids = personas.map((p) => p.id).join(',');
  useEffect(() => {
    if (!apiOnline || !ids) return;
    let stop = false;
    const poll = () => {
      // focused — heartbeat активности вкладки: гейт фоновой работы бота.
      // Только для персоны ОТКРЫТОГО чата: поллер опрашивает всех, и общий
      // флаг отмечал бы присутствие сразу у всех персон (см. presence.ts)
      const focusedId = tabActive() ? getFocusedPersona() : null;
      ids.split(',').forEach((id) => {
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
      });
    };
    poll();
    const timer = setInterval(poll, 15000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, ids]);
}
