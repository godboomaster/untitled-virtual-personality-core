import { getApiUrl, isLinkMode, isNativeApp } from './api';

/* Приложение на телефоне без компьютера: скачан только APK, ядра нет.
   Выбирается кнопкой «Пользоваться без компьютера» на экране подключения
   (BootGate) и запоминается: дальше приложение открывается сразу, без
   опроса ядра, — персоны отвечают облаком по ключам телефона или Gemma на
   телефоне (web/src/offline). «Подключить компьютер» (вкладка
   «Подключение») снимает выбор и возвращает экран подключения; всё, что
   накопилось на телефоне, переезжает на компьютер при первой связи
   (offline/state.ts syncWithCore). */

const KEY = 'vpc-standalone';

export function standaloneChosen(): boolean {
  try {
    return localStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}

export function setStandaloneChosen(on: boolean) {
  try {
    if (on) localStorage.setItem(KEY, '1');
    else localStorage.removeItem(KEY);
  } catch {
    /* хранилище недоступно — выбор проживёт до перезапуска */
  }
}

/** Сейчас без компьютера: выбран этот режим, и адрес ядра не задан. */
export function isStandalone(): boolean {
  return isNativeApp() && standaloneChosen() && !getApiUrl() && !isLinkMode();
}
