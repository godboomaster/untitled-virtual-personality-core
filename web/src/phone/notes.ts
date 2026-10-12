/* Заметка персоне по итогу команды телефону — зеркало
   app/features/phone_actions.py (action_note): без связи с ядром её
   собирает телефон, с ядром — ядро из отчёта. scripts/test_phone_actions.py
   сверяет оба на одних отчётах. Модуль без импортов — гоняется под Node. */

export type PhoneActionKind = 'alarm' | 'timer' | 'alarms_list' | 'alarm_off' | 'open_app' | 'torch';
export type PhoneActionStatus =
  | 'ok'
  | 'ask'
  | 'no_handler'
  | 'unsupported_date'
  | 'not_found'
  | 'ambiguous'
  | 'busy'
  | 'background'
  | 'failed';

/** Итог команды — уходит ядру как ChatRequest.phone_action. */
export interface PhoneActionReport {
  kind: PhoneActionKind;
  status: PhoneActionStatus;
  hour?: number;
  minute?: number;
  days?: number[]; // повтор: 1 — воскресенье … 7 — суббота
  nearest?: 'today' | 'tomorrow';
  seconds?: number;
  label?: string;
  app?: string;
  candidates?: string[];
  on?: boolean;
}

const DAY_NAMES: Record<number, string> = {
  1: 'Sunday', 2: 'Monday', 3: 'Tuesday', 4: 'Wednesday', 5: 'Thursday', 6: 'Friday', 7: 'Saturday',
};

const NOT_DONE = 'Say so honestly and briefly.';

const cleanText = (text: unknown) => String(text ?? '').replace(/"/g, "'").split(/\s+/).filter(Boolean).join(' ').slice(0, 80);

const hm = (r: PhoneActionReport) => `${String(r.hour ?? 0).padStart(2, '0')}:${String(r.minute ?? 0).padStart(2, '0')}`;

function daysText(days: number[] | undefined): string {
  const s = [...new Set((days ?? []).filter((d) => d >= 1 && d <= 7))].sort((a, b) => ((a + 5) % 7) - ((b + 5) % 7));
  if (!s.length) return '';
  if (s.length === 7) return ' every day';
  if (s.join() === '2,3,4,5,6') return ' on weekdays';
  if (s.join() === '7,1') return ' on weekends';
  return ` every ${s.map((d) => DAY_NAMES[d]).join(', ')}`;
}

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`;

function duration(seconds: number | undefined): string {
  const sec = Math.trunc(seconds ?? 0);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const parts = ([[h, 'hour'], [m, 'minute'], [s, 'second']] as [number, string][]).filter(([n]) => n).map(([n, u]) => plural(n, u));
  return parts.join(' ') || '0 seconds';
}

function labelText(r: PhoneActionReport): string {
  const label = cleanText(r.label);
  return label ? ` labelled "${label}"` : '';
}

/** Что просили — для заметки «не дали из фона». */
function asked(r: PhoneActionReport): string {
  if (r.kind === 'alarm') return `an alarm at ${hm(r)}`;
  if (r.kind === 'timer') return `a timer for ${duration(r.seconds)}`;
  if (r.kind === 'open_app') return `to open "${cleanText(r.app)}"`;
  if (r.kind === 'torch') return `to turn the flashlight ${r.on ? 'on' : 'off'}`;
  return 'to show the alarms';
}

export function actionNote(r: PhoneActionReport): string {
  const { kind, status } = r;
  const app = cleanText(r.app);
  // Голосом при свёрнутом приложении: Android не даёт открыть часы или
  // другое приложение из фона без права «Поверх других приложений»
  if (status === 'background') {
    return `The user asked by voice for ${asked(r)}, but Android does not let the app open the clock or other apps while it is in the background, so nothing was done. ${NOT_DONE} Tell the user to open the app, or to allow it "Display over other apps" (the app's settings, under listening for a name) so it works with the screen off. Do NOT say that it was done.`;
  }
  if (kind === 'alarm') {
    const t = hm(r);
    if (status === 'ok') {
      return `The app on the user's phone has just set an alarm in the phone's clock app for ${t}${daysText(r.days)}${labelText(r)}. Confirm it briefly, in your own style.`;
    }
    if (status === 'ask') {
      return 'The user wants an alarm on the phone but did not say the time, so nothing was set yet. Ask briefly what time to set it for.';
    }
    if (status === 'no_handler') {
      return `The user asked for an alarm at ${t}, but the phone has no clock app that accepts alarms from other apps, so NO alarm was set. ${NOT_DONE} The user can set it in the clock app by hand. Do NOT say the alarm was set.`;
    }
    if (status === 'unsupported_date') {
      const when = r.nearest === 'today' ? 'today' : 'tomorrow';
      return `The user asked for an alarm at ${t} on a particular day, but the phone's clock app can only set the nearest ${t} (${when}) or repeat it on days of the week, so NO alarm was set. ${NOT_DONE} Do NOT say the alarm was set.`;
    }
    return `The app on the user's phone tried to set an alarm for ${t}, but it failed, so NO alarm was set. ${NOT_DONE} Do NOT say the alarm was set.`;
  }
  if (kind === 'timer') {
    const dur = duration(r.seconds);
    if (status === 'ok') {
      return `The app on the user's phone has just started a timer for ${dur}${labelText(r)} in the phone's clock app. Confirm it briefly, in your own style.`;
    }
    if (status === 'ask') {
      return 'The user wants a timer on the phone but did not say for how long, so nothing was started yet. Ask briefly for how long.';
    }
    if (status === 'no_handler') {
      return `The user asked for a timer for ${dur}, but the phone has no clock app that accepts timers from other apps, so NO timer was started. ${NOT_DONE} Do NOT say the timer was started.`;
    }
    return `The app on the user's phone tried to start a timer for ${dur}, but it failed, so NO timer was started. ${NOT_DONE} Do NOT say the timer was started.`;
  }
  if (kind === 'alarms_list') {
    if (status === 'ok') {
      return "The app on the user's phone has just opened the list of alarms in the phone's clock app. You cannot see the alarms themselves. Reply briefly.";
    }
    return `The user asked to see their alarms, but the phone has no clock app that can show them, so nothing was opened. ${NOT_DONE}`;
  }
  if (kind === 'alarm_off') {
    if (status === 'ok') {
      return "The user asked to turn off or delete an alarm. The app cannot change or delete alarms by itself, so it has opened the list of alarms in the phone's clock app for the user to do it there. Say so briefly. Do NOT say any alarm was turned off or deleted.";
    }
    return `The user asked to turn off or delete an alarm, but the app cannot change alarms and found no clock app to open, so nothing was changed. ${NOT_DONE} Do NOT say any alarm was turned off or deleted.`;
  }
  if (kind === 'open_app') {
    if (status === 'ok') return `The app on the user's phone has just opened "${app}" on the phone. Confirm it briefly, in your own style.`;
    if (status === 'not_found') {
      return `The user asked to open "${app}" on the phone, but no installed app with that name was found, so nothing was opened. ${NOT_DONE} Do NOT say that anything was opened.`;
    }
    if (status === 'ambiguous') {
      const listed = (r.candidates ?? []).map((c, i) => `${i + 1}. ${cleanText(c)}`).join('; ');
      return `The user asked to open "${app}" on the phone, and several installed apps match: ${listed}. Nothing was opened yet. Ask briefly which one, listing them with these numbers; the user can answer with a number or a name.`;
    }
    return `The app on the user's phone tried to open "${app}", but it failed, so nothing was opened. ${NOT_DONE} Do NOT say that it was opened.`;
  }
  if (kind === 'torch') {
    const state = r.on ? 'on' : 'off';
    if (status === 'ok') return `The app on the user's phone has just turned the flashlight ${state}. Confirm it briefly, in your own style.`;
    if (status === 'no_handler') return `The user asked to turn the flashlight ${state}, but the phone has no flashlight, so nothing happened. ${NOT_DONE}`;
    if (status === 'busy') {
      return `The user asked to turn the flashlight ${state}, but the camera is busy with another app, so nothing happened. ${NOT_DONE}`;
    }
    return `The app on the user's phone tried to turn the flashlight ${state}, but it failed, so nothing happened. ${NOT_DONE}`;
  }
  return `The app on the user's phone could not do what the user asked, so nothing was done. ${NOT_DONE} Do NOT say that it was done.`;
}
