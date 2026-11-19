import { api, ApiError, isNativeApp } from '../api';

/* Город для погоды, заданный на телефоне («Настройки» → «Без связи с ядром»).
   У ядра он свой (настройки местоположения на компьютере) и приходит в ките;
   без компьютера — отсюда: координаты по названию даёт геокодер Open-Meteo,
   погоду по ним телефон и так берёт у Open-Meteo (context.ts). При связи с
   ядром город уходит в его настройки, если там местоположение выключено
   (syncPhoneLocation) — один раз на город. */

export interface PhoneLocation {
  city: string;
  lat: number;
  lon: number;
}

const KEY = 'vpc-phone-location';
const listeners = new Set<() => void>();

export function phoneLocation(): PhoneLocation | null {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || 'null') as PhoneLocation | null;
    return v && typeof v.lat === 'number' && typeof v.lon === 'number' ? v : null;
  } catch {
    return null;
  }
}

export function setPhoneLocation(loc: PhoneLocation | null) {
  try {
    if (loc) localStorage.setItem(KEY, JSON.stringify(loc));
    else localStorage.removeItem(KEY);
  } catch {
    /* хранилище недоступно */
  }
  listeners.forEach((l) => l());
}

export function onPhoneLocationChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export interface GeoMatch extends PhoneLocation {
  label: string; // «Казань, Татарстан, Россия» — выбрать из нескольких
}

/** Города по названию (геокодер Open-Meteo, без ключа). */
export async function findCity(name: string, lang: string): Promise<GeoMatch[]> {
  const url =
    `https://geocoding-api.open-meteo.com/v1/search?count=5&format=json` +
    `&language=${encodeURIComponent(lang)}&name=${encodeURIComponent(name.trim())}`;
  const ctl = new AbortController();
  const timer = window.setTimeout(() => ctl.abort(), 8000);
  try {
    const r = await fetch(url, { signal: ctl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = (await r.json()) as {
      results?: { name: string; latitude: number; longitude: number; admin1?: string; country?: string }[];
    };
    return (data.results ?? []).map((x) => ({
      city: x.name,
      lat: Math.round(x.latitude * 1e4) / 1e4,
      lon: Math.round(x.longitude * 1e4) / 1e4,
      label: [x.name, x.admin1, x.country].filter(Boolean).join(', '),
    }));
  } finally {
    window.clearTimeout(timer);
  }
}

const SENT_KEY = 'vpc-phone-location-sent';

/** Ядро на связи: город телефона — ядру, если у него местоположение
 * выключено (заданное на компьютере не трогаем). */
export async function syncPhoneLocation(): Promise<void> {
  const loc = phoneLocation();
  if (!isNativeApp() || !loc) return;
  const sig = `${loc.lat},${loc.lon}`;
  try {
    if (localStorage.getItem(SENT_KEY) === sig) return;
  } catch {
    return;
  }
  try {
    const core = await api.getLocation();
    // По координатам ("geo"): название ядро геокодировало бы заново и могло
    // выбрать другой город с тем же именем
    if (core.mode === 'off') await api.setLocation({ mode: 'geo', lat: loc.lat, lon: loc.lon });
    localStorage.setItem(SENT_KEY, sig);
  } catch (e) {
    // 4xx — ядро не приняло: больше не пробуем этот город; сеть — при следующей связи
    if (e instanceof ApiError) {
      try {
        localStorage.setItem(SENT_KEY, sig);
      } catch {
        /* хранилище недоступно */
      }
    }
  }
}
