import { registerPlugin } from '@capacitor/core';
import type { PluginListenerHandle } from '@capacitor/core';
import { api, getApiToken, getApiUrl, isLinkMode, isNativeApp } from './api';
import { offlinePersonasFull } from './apiData';
import { onLocalPersonasChange } from './offline/localPersonas';
import { isStandalone } from './standalone';
import { getKit } from './offline/kit';
import type { HistoryLine } from './offline/prompt';
import { replyOffline } from './offline/reply';
import { addOfflineMessage, offlineMessages } from './offline/store';
import { actionNote } from './phone/notes';
import type { PhoneActionReport } from './phone/notes';

/* Приложение на Android: «Слушать по имени» — скажите имя персоны
   («Коннор, какая погода?»), и она ответит голосом, даже при выключенном
   экране. Слушает нативная служба переднего плана с микрофоном
   (WakeWordService, плагин WakeWord в web/android): речь распознаётся на
   телефоне (sherpa-onnx), на ядро уходит только реплика с именем —
   POST /api/chat в тот же чат, что у приложения.

   Только для подключения по адресу и токену (как фоновые уведомления):
   по каналу VPC Link служба ходить не умеет. Выбор «включено» хранит сама
   служба (его можно снять и кнопкой «Выключить» в шторке); при запуске
   приложения ей передаются свежие адрес и токен.

   Ядро не ответило — служба спрашивает приложение (initWakeOffline): ответ
   без ядра, как на реплику в чате без связи (облако или модель телефона),
   реплика и ответ — в переписке на телефоне; служба озвучивает ответ. */

export type WakeState =
  | 'off'
  | 'loading'
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'paused'
  | 'no_names'
  | 'mic'
  | 'model';

export interface WakeStatus {
  enabled: boolean;
  running: boolean;
  state: WakeState;
  // Имена, на которые служба откликается (по одному на персону)
  names: string[];
  lastError: string | null;
  micGranted: boolean;
  // Право «Поверх других приложений»: без него голосом при свёрнутом
  // приложении не открыть «Часы» и «открой X» (фонарик — можно). Нет в
  // старой сборке — undefined
  overlayAllowed?: boolean;
  // Разбор команд телефону голосом (V8 из WebView): ready | unsupported
  // (старый WebView — команды уйдут текстом) | no_bundle | error | off
  phoneEngine?: string;
}

interface WakeWordPlugin {
  start(opts: Connection): Promise<{ started: boolean; reason?: 'mic-denied' }>;
  stop(): Promise<void>;
  configure(opts: Connection): Promise<void>;
  status(): Promise<WakeStatus>;
  openOverlaySettings(): Promise<void>;
  offlineReply(opts: { id: string; text: string }): Promise<void>;
  takePending(): Promise<{ items: PendingVoice[] }>;
  addListener(event: 'offlineAsk', fn: (e: OfflineAsk) => void): Promise<PluginListenerHandle>;
}

// Голосом, а ядро не ответило: ответить без него
interface OfflineAsk {
  id: string;
  persona: string;
  message: string;
  action?: PhoneActionReport; // команда телефону уже исполнена службой
}

// Услышано, пока окна приложения не было (или написано в окне «Написать»
// виджета портрета, когда ядро не отвечало)
interface PendingVoice {
  persona: string;
  message: string;
  ts: number;
  action?: PhoneActionReport; // команда телефону уже исполнена
}

// Адрес ядра или телефон без компьютера: тогда имена персон (trigger_words)
// службе даёт приложение, отвечает телефон
type Connection = { baseUrl: string; token: string } | { standalone: true; personas: unknown[] };

const WakeWord = registerPlugin<WakeWordPlugin>('WakeWord');

function connection(): Connection | null {
  if (isStandalone()) return { standalone: true, personas: offlinePersonasFull() };
  if (isLinkMode() || !getApiUrl()) return null;
  return { baseUrl: getApiUrl(), token: getApiToken() };
}

/** Переключатель есть: приложение на телефоне. */
export function wakeAvailable(): boolean {
  return isNativeApp();
}

export type WakeResult = 'ok' | 'mic-denied' | 'error';

/** Переключатель во вкладке «Подключение»: включение спрашивает
 * микрофон (и уведомления на Android 13+) и запускает службу. */
export async function setWakeWord(on: boolean): Promise<WakeResult> {
  if (!isNativeApp()) return 'error';
  if (!on) {
    await WakeWord.stop().catch(() => {});
    return 'ok';
  }
  const conn = connection();
  if (!conn) return 'error';
  try {
    const r = await WakeWord.start(conn);
    return r.started ? 'ok' : r.reason === 'mic-denied' ? 'mic-denied' : 'error';
  } catch {
    return 'error';
  }
}

export function wakeStatus(): Promise<WakeStatus> {
  return WakeWord.status();
}

/** Экран Android с правом «Поверх других приложений» для этого приложения. */
export function openOverlaySettings(): Promise<void> {
  return WakeWord.openOverlaySettings();
}

/** Запуск приложения (ядро ответило): включено — служба получает свежие
 * адрес и токен и (пере)запускается; канал VPC Link — служба не нужна. Без
 * компьютера — свежий список персон (создали или удалили на телефоне). */
export function syncWakeWord() {
  if (!isNativeApp()) return;
  void WakeWord.status()
    .then((s) => {
      if (!s.enabled) return;
      return WakeWord.configure(connection() ?? { baseUrl: '', token: '' });
    })
    .catch(() => {});
}

/** Смена сервера или отвязка: прежнее ядро служба больше не спрашивает
 * (выбор «включено» остаётся — с новым адресом она запустится снова). */
export function forgetWakeServer() {
  if (!isNativeApp()) return;
  void WakeWord.configure({ baseUrl: '', token: '' }).catch(() => {});
}

// История для ответа без ядра: хвост переписки из кита и переписка на телефоне
function voiceHistory(persona: string): HistoryLine[] {
  const kit = (getKit(persona)?.history ?? []).map((h) => ({
    role: h.role === 'user' ? ('user' as const) : ('assistant' as const),
    text: h.text,
    ts: h.ts,
  }));
  return [...kit, ...offlineMessages(persona).map((m) => ({ role: m.role, text: m.text, ts: m.ts }))];
}

/** Ответ без ядра на реплику голосом; '' — ответить некому (реплика ждёт в очереди). */
async function voiceReply(persona: string, text: string, action?: PhoneActionReport): Promise<string> {
  try {
    const res = await replyOffline(persona, text, voiceHistory(persona), { actionNote: action ? actionNote(action) : undefined });
    return res.text;
  } catch {
    return '';
  }
}

/** Запуск приложения: служба голоса без ядра спрашивает ответ у него, а
 * услышанное, пока окна не было, — в переписку: ядро на связи — ответит
 * оно (чат отправит очередь), нет — ответ без него. */
export function initWakeOffline() {
  if (!isNativeApp()) return;
  // Без компьютера имена знает только приложение: персону создали или
  // удалили — служба слушает новые
  onLocalPersonasChange(() => {
    if (isStandalone()) syncWakeWord();
  });
  if (isStandalone()) syncWakeWord();
  void WakeWord.addListener('offlineAsk', (e) => {
    void voiceReply(e.persona, e.message, e.action).then((text) => WakeWord.offlineReply({ id: e.id, text }));
  }).catch(() => {});
  // При запуске и каждый раз, когда приложение снова на экране
  const takePending = () => void WakeWord.takePending()
    .then(async ({ items }) => {
      if (!items.length) return;
      const core = await api.health({ signal: AbortSignal.timeout(5000) }).then(
        () => true,
        () => false,
      );
      const byPersona = new Map<string, PendingVoice[]>();
      for (const it of items) byPersona.set(it.persona, [...(byPersona.get(it.persona) ?? []), it]);
      for (const [persona, list] of byPersona) {
        const last = core ? null : list[list.length - 1];
        for (const it of list) if (it !== last) addOfflineMessage(persona, { role: 'user', text: it.message, source: 'queued' });
        if (last) await voiceReply(persona, last.message, last.action);
      }
    })
    .catch(() => {});
  takePending();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') takePending();
  });
}
