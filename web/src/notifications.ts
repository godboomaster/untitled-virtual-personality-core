/* Уведомления о новых сообщениях персон: системные всплывающие
   (Web Notifications), звук (Web Audio — несколько пресетов на выбор,
   без аудиофайлов) и счётчик непрочитанных в заголовке вкладки.
   Показываются, только когда вкладка не в фокусе — как в мессенджерах;
   включаются и выключаются в настройках (localStorage, ключи vpc-notify*). */

import { getPersonaAvatar } from './avatarStore';

const BASE_TITLE = document.title;
const FRESH_WINDOW_SEC = 180; // старше — не уведомляем (хвосты inbox после перезагрузки страницы)

/** Имя события «открыть чат с персоной» (клик по системному уведомлению) */
export const OPEN_CHAT_EVENT = 'vpc:open-chat';

const APP_ICON = '/favicon.png'; // PNG: SVG-иконки Chrome в уведомлениях не показывает

/* ===== Настройки (localStorage) ===== */

export function notificationsEnabled(): boolean {
  return localStorage.getItem('vpc-notify') !== 'off';
}

export function setNotificationsEnabled(on: boolean) {
  localStorage.setItem('vpc-notify', on ? 'on' : 'off');
}

export type NotifySoundId = 'ding' | 'pop' | 'chime' | 'bell' | 'none';

export function notifySoundId(): NotifySoundId {
  const saved = localStorage.getItem('vpc-notify-sound');
  return saved === 'pop' || saved === 'chime' || saved === 'bell' || saved === 'none' ? saved : 'ding';
}

export function setNotifySoundId(id: NotifySoundId) {
  localStorage.setItem('vpc-notify-sound', id);
}

/** Громкость 0..1 */
export function notifyVolume(): number {
  const v = Number.parseFloat(localStorage.getItem('vpc-notify-volume') ?? '');
  return Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0.35;
}

export function setNotifyVolume(v: number) {
  localStorage.setItem('vpc-notify-volume', String(Math.min(1, Math.max(0, v))));
}

export type NotifyIconMode = 'persona' | 'app';

/** Иконка уведомления: аватар персоны (своя картинка, иначе буквенный)
 *  или иконка приложения */
export function notifyIconMode(): NotifyIconMode {
  return localStorage.getItem('vpc-notify-icon') === 'app' ? 'app' : 'persona';
}

export function setNotifyIconMode(mode: NotifyIconMode) {
  localStorage.setItem('vpc-notify-icon', mode);
}

/* ===== Разрешение и показ ===== */

/** Состояние разрешения системных уведомлений (для настроек и диагностики) */
export function notifyPermissionState(): 'unsupported' | 'default' | 'granted' | 'denied' {
  if (!('Notification' in window)) return 'unsupported';
  return Notification.permission;
}

/** Запросить разрешение системных уведомлений. Вызывать только по
 *  пользовательскому жесту (требование Safari); повторно не спрашиваем. */
export function ensureNotifyPermission() {
  if (!('Notification' in window) || Notification.permission !== 'default') return;
  void Notification.requestPermission();
}

/** Уведомить о сообщении персоны: системное всплывающее + звук.
 *  Если вкладка на экране и в фокусе — сообщение и так видно, всё тихо.
 *  Каждое решение логируется в консоль с префиксом [vpc-notify]. */
export function notifyBotMessage(personaId: string, personaName: string, text: string, ts?: number) {
  if (!notificationsEnabled()) {
    console.info('[vpc-notify] пропущено: выключено в настройках');
    return;
  }
  if (!document.hidden && document.hasFocus()) {
    console.info('[vpc-notify] пропущено: вкладка на экране и в фокусе');
    return;
  }
  if (ts && Date.now() / 1000 - ts > FRESH_WINDOW_SEC) {
    console.info('[vpc-notify] пропущено: сообщение старше 3 минут');
    return;
  }
  if (notifyPermissionState() !== 'granted') {
    console.warn('[vpc-notify] разрешение системных уведомлений не выдано:', notifyPermissionState());
    playNotifySound();
    return;
  }
  console.info('[vpc-notify] уведомление:', personaName, '—', text.slice(0, 80));
  showSystemNotification(personaId, personaName, text);
  playNotifySound();
}

function showSystemNotification(personaId: string, personaName: string, text: string) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const notification = new Notification(personaName, {
    body: text.slice(0, 200),
    tag: `vpc-${personaId}`, // новое сообщение персоны заменяет её предыдущее уведомление
    icon:
      notifyIconMode() === 'persona'
        ? (getPersonaAvatar(personaId) ?? buildPersonaIcon(personaId, personaName))
        : APP_ICON,
  });
  notification.onclick = () => {
    window.focus();
    notification.close();
    window.dispatchEvent(new CustomEvent(OPEN_CHAT_EVENT, { detail: personaId }));
  };
}

/* Буквенный аватар персоны (как в списке чатов): круг с цветом по хэшу id
   и первой буквой имени. Canvas → PNG data-URL — то, что понимает icon
   уведомления без файлов на диске. */
const ICON_PALETTE = ['#5b8cff', '#9a6bff', '#3fbf9f', '#e0804f', '#d05f8f', '#4fa8d8'];
const iconCache = new Map<string, string>();

function buildPersonaIcon(personaId: string, personaName: string): string {
  const cached = iconCache.get(personaId);
  if (cached) return cached;
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (!ctx) return APP_ICON;
  let hash = 0;
  for (const ch of personaId) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  ctx.fillStyle = ICON_PALETTE[hash % ICON_PALETTE.length];
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#ffffff';
  ctx.font = '700 64px Inter, Arial, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText((personaName.trim()[0] ?? '?').toUpperCase(), size / 2, size / 2 + 4);
  const url = canvas.toDataURL('image/png');
  iconCache.set(personaId, url);
  return url;
}

/* ===== Звук ===== */

/* Пресеты — ноты {частота, задержка от старта, длительность, тембр,
   относительная громкость}; общий мастер-гейн = выбранная громкость.
   Контекст подвешен политикой автоплея до первого клика по странице:
   если ещё suspended — тихо пропускаем. */
type SoundNote = { freq: number; at: number; dur: number; type?: OscillatorType; level?: number };
const SOUND_PRESETS: Record<Exclude<NotifySoundId, 'none'>, SoundNote[]> = {
  ding: [
    { freq: 880, at: 0, dur: 0.6 },
    { freq: 1174.66, at: 0.12, dur: 0.5 },
  ],
  pop: [
    { freq: 523.25, at: 0, dur: 0.12, type: 'triangle' },
    { freq: 659.25, at: 0.09, dur: 0.16, type: 'triangle' },
  ],
  chime: [
    { freq: 659.25, at: 0, dur: 0.9, level: 0.8 },
    { freq: 987.77, at: 0.15, dur: 0.85, level: 0.7 },
    { freq: 1318.51, at: 0.3, dur: 0.8, level: 0.5 },
  ],
  bell: [
    { freq: 587.33, at: 0, dur: 1.3 },
    { freq: 1174.66, at: 0, dur: 1.1, level: 0.5 },
    { freq: 1760, at: 0, dur: 0.9, level: 0.25 },
  ],
};

let audioCtx: AudioContext | null = null;

/** Проиграть выбранный звук оповещения на выбранной громкости */
export function playNotifySound() {
  const presetId = notifySoundId();
  if (presetId === 'none') return;
  const notes = SOUND_PRESETS[presetId];
  const volume = notifyVolume();
  if (volume <= 0) return;
  try {
    audioCtx ??= new AudioContext();
    if (audioCtx.state === 'suspended') {
      void audioCtx.resume(); // разблокировка после жеста; звук будет со следующего раза
      return;
    }
    const now = audioCtx.currentTime;
    const master = audioCtx.createGain();
    master.gain.value = volume;
    master.connect(audioCtx.destination);
    for (const n of notes) {
      const t0 = now + n.at;
      const osc = audioCtx.createOscillator();
      osc.type = n.type ?? 'sine';
      osc.frequency.value = n.freq;
      const gain = audioCtx.createGain();
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(n.level ?? 1, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + n.dur);
      osc.connect(gain);
      gain.connect(master);
      osc.start(t0);
      osc.stop(t0 + n.dur + 0.05);
    }
  } catch {
    /* звук — необязательная часть уведомления */
  }
}

/** Предпрослушивание из настроек: то же, что и боевое срабатывание,
 *  но без проверки фокуса (клик по настройкам уже разблокирует звук) */
export function previewNotifySound() {
  playNotifySound();
}

/* ===== Прочее ===== */

/** Тестовое уведомление из настроек: при необходимости дозапрашивает
 *  разрешение (клик — это пользовательский жест), показывает пробное
 *  уведомление и звук; возвращает человекочитаемый результат проверки */
export async function testNotification(): Promise<string> {
  if (!('Notification' in window)) {
    return 'Notification API недоступен: страница должна быть открыта по localhost или HTTPS';
  }
  if (Notification.permission === 'default') await Notification.requestPermission();
  if (Notification.permission !== 'granted') {
    return 'Разрешение не выдано — кликни значок 🔒 слева от адреса → Уведомления → Разрешить';
  }
  new Notification('Virtual Persona Core', {
    body: 'Тест: уведомления работают ✓',
    tag: 'vpc-test',
    icon: APP_ICON,
  });
  playNotifySound();
  return 'Отправлено ✓ Баннера нет? Системные настройки → Уведомления → [браузер] → включить и выбрать стиль «Баннеры»; также проверь Фокус-режим';
}

/** Счётчик непрочитанных в заголовке вкладки: «(3) Virtual Persona Core» */
export function setUnreadTitle(total: number) {
  document.title = total > 0 ? `(${total}) ${BASE_TITLE}` : BASE_TITLE;
}
