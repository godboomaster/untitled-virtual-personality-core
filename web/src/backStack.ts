import { useEffect, useRef } from 'react';

/* Системная кнопка «Назад» (приложение на Android): закрывает верхнее из
   открытого — меню, окно или диалог, панель (досье, голос, браузер агента),
   переписку (к списку чатов). Нечего закрывать — приложение сворачивается
   (main.tsx). Компонент регистрирует обработчик, пока ему есть что
   закрыть; срабатывает обработчик старшего уровня, внутри уровня — открытый
   последним. На компьютере стек не используется: там те же окна закрывает Esc. */

export const BACK_SCREEN = 1; // экран внутри раздела (переписка → список чатов)
export const BACK_PANEL = 2; // панель поверх экрана (досье, голосовой режим, браузер)
export const BACK_OVERLAY = 3; // окно, диалог, лайтбокс
export const BACK_MENU = 4; // выдвижное меню разделов

interface Entry {
  level: number;
  seq: number;
  run: () => void;
}

const entries = new Set<Entry>();
let seq = 0;

/** Обработать «Назад»: true — что-то закрыто, false — закрывать нечего. */
export function handleBack(): boolean {
  let top: Entry | null = null;
  for (const e of entries) {
    if (!top || e.level > top.level || (e.level === top.level && e.seq > top.seq)) top = e;
  }
  if (!top) return false;
  top.run();
  return true;
}

/** Пока active — «Назад» вызывает fn (последнюю переданную). */
export function useBackHandler(active: boolean, level: number, fn: () => void) {
  const fnRef = useRef(fn);
  useEffect(() => {
    fnRef.current = fn;
  });
  useEffect(() => {
    if (!active) return;
    const entry: Entry = { level, seq: ++seq, run: () => fnRef.current() };
    entries.add(entry);
    return () => {
      entries.delete(entry);
    };
  }, [active, level]);
}
