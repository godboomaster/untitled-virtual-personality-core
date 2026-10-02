/* Общий хук окружения скина: тема + локаль + подписи UI + время суток +
   погода — снапшот SkinEnv для payloads.buildChatPayload/buildRoomPayload
   (и предпросмотра в SkinPanel). Погода тянется с бэкенда, только
   пока он онлайн; время суток обновляется раз в минуту — этого достаточно,
   а лишний трафик в скин гасит сам SkinFrame (сигнатура снапшота + rAF).

   enabled: false (у персоны нет активного скина) — ни поллинга погоды,
   ни минутного тикера: экран без скина не перерисовывается зря.

   Время — в часовом поясе, заданном в настройках бэкенда (TIMEZONE), если
   он задан явно; иначе — часы браузера (бэкенд тогда живёт по системному
   поясу той же машины). */

import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { buildSkinEnv } from './payloads';
import type { SkinEnv } from './payloads';
import { useAppTheme } from '../useAppTheme';

// Между обновлениями погоды (getEnvPreview дёргает геосервис — часто не нужно)
const WEATHER_REFRESH_MS = 12 * 60 * 1000;

// Общий кеш погоды и пояса на все экземпляры хука: раздел «Комната» и её
// PiP-окно (и экраны со скином) открыты одновременно — один сетевой запрос
// на период, а не по запросу на каждый экземпляр. Поля нет — запрос не удался
interface EnvShared {
  at: number;
  line?: string | null;
  tz?: string | null;
}
let envShared: EnvShared | null = null;
let envInflight: Promise<EnvShared> | null = null;

function loadEnvShared(): Promise<EnvShared> {
  // Свежий ответ (моложе половины периода) — без сети
  if (envShared && Date.now() - envShared.at < WEATHER_REFRESH_MS / 2) return Promise.resolve(envShared);
  if (!envInflight) {
    envInflight = Promise.allSettled([api.getEnvPreview(), api.getTimezone()])
      .then(([w, z]) => {
        const next: EnvShared = { at: Date.now() };
        if (w.status === 'fulfilled') next.line = w.value.line;
        if (z.status === 'fulfilled') next.tz = z.value.source === 'env' && z.value.timezone ? z.value.timezone : null;
        envShared = next;
        return next;
      })
      .finally(() => {
        envInflight = null;
      });
  }
  return envInflight;
}

// Часы и минуты «сейчас» в поясе tz — как Date, у которой getHours/getMinutes
// дают время этого пояса (buildSkinEnv берёт только их)
function zonedNow(now: Date, tz: string | null): Date {
  if (!tz) return now;
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
    }).formatToParts(now);
    const h = Number(parts.find((p) => p.type === 'hour')?.value);
    const m = Number(parts.find((p) => p.type === 'minute')?.value);
    if (Number.isNaN(h) || Number.isNaN(m)) return now;
    return new Date(2000, 0, 1, h, m);
  } catch {
    return now; // неизвестный браузеру пояс
  }
}

export function useSkinEnv(args: {
  locale: string;
  t: (key: string, vars?: Record<string, string | number>) => string;
  personaName?: string;
  apiOnline: boolean;
  enabled?: boolean;
}): SkinEnv {
  const { locale, t, personaName, apiOnline } = args;
  const enabled = args.enabled ?? true;
  const theme = useAppTheme();

  // Строка погоды бэкенда (GET /api/settings/env-preview) — только онлайн;
  // buildSkinEnv сам разбирает строку (parseSkinWeather). Там же — явно
  // заданный часовой пояс (GET /api/settings/timezone)
  const [weatherLine, setWeatherLine] = useState<string | null>(null);
  const [timezone, setTimezone] = useState<string | null>(null);
  useEffect(() => {
    if (!apiOnline || !enabled) {
      setWeatherLine(null);
      return;
    }
    let stale = false;
    const load = () => {
      void loadEnvShared().then((res) => {
        if (stale) return;
        if (res.line !== undefined) setWeatherLine(res.line);
        if (res.tz !== undefined) setTimezone(res.tz);
      });
    };
    load();
    const timer = window.setInterval(load, WEATHER_REFRESH_MS);
    return () => {
      stale = true;
      window.clearInterval(timer);
    };
  }, [apiOnline, enabled]);

  // Минутный тикер — timeOfDay/localTime не залипают, но не дёргают снапшот
  // чаще, чем видно в UI
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!enabled) return;
    setNow(new Date());
    const timer = window.setInterval(() => setNow(new Date()), 60000);
    return () => window.clearInterval(timer);
  }, [enabled]);
  const zoned = useMemo(() => zonedNow(now, apiOnline ? timezone : null), [now, apiOnline, timezone]);
  const minuteKey = zoned.getHours() * 60 + zoned.getMinutes();

  return useMemo(
    () => buildSkinEnv({ theme, locale, t, now: zoned, weather: weatherLine, personaName }),
    // zoned намеренно не в зависимостях — минутный тик отражён через minuteKey,
    // а сам объект Date не должен пересобирать env на каждый рендер
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [theme, locale, t, minuteKey, weatherLine, personaName],
  );
}
