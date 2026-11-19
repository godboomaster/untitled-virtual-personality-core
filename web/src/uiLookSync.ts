import { useEffect } from 'react';
import { api, isNativeApp } from './api';
import type { UiLook } from './api';
import { useSkinThemeState } from './skinTheme';

/* Выбор темы оформления — ещё и ядру (PUT /api/settings/ui-look): портрет на
   рабочем столе живёт в окне панели, куда выбор в localStorage браузера не
   виден, и берёт его у ядра — голова рисуется скином сразу после смены.
   Уходит выбор целиком (общий скин, свои темы персон, схема в шапке) — при
   открытии веба и при каждой смене; то же, что уже ушло, второй раз не шлём.
   Телефон не шлёт: в приложении свой выбор, портрет на столе — у компьютера. */

let sent = '';

export function useUiLookSync(online: boolean) {
  const s = useSkinThemeState();
  useEffect(() => {
    if (!online || isNativeApp()) return;
    const look: UiLook = { skin: s.global, personas: s.personas, theme: s.preference };
    const key = JSON.stringify(look);
    if (key === sent) return;
    // Короткая пауза: перебор скинов в галерее — одним запросом
    const timer = window.setTimeout(() => {
      api.setUiLook(look).then(
        () => {
          sent = key;
        },
        () => {
          /* ядро старее (404) или недоступно — попробуем при следующей смене */
        },
      );
    }, 300);
    return () => window.clearTimeout(timer);
  }, [online, s.global, s.personas, s.preference]);
}
