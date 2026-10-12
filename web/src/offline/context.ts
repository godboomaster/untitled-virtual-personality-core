import type { OfflineKit } from './kit';
import { OfflineLlm, pluginCode } from './plugin';
export { wantsSearch } from './gate';
import type { WebResult } from './plugin';

/* Что ядро добавляет к ответу и что телефон без ноутбука добывает сам:
   строка окружения (местные дата и время, погода Open-Meteo по городу из
   настроек) и веб-поиск DuckDuckGo. Правила к ним, гейт поиска и описания
   погодных кодов — из кита: те же, что в промпте ядра
   (app/core/persona.py, app/features/search_gate.py, env_context.py). */

const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const WEATHER_TTL_MS = 30 * 60_000;
const WEATHER_RETRY_MS = 2 * 60_000;

const pad = (n: number) => String(n).padStart(2, '0');

/** «Friday, 10.10.2026, 13:05» — как строка времени ядра (get_env_line). */
export function timeLine(d = new Date()): string {
  return `${WEEKDAYS[d.getDay()]}, ${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

let weatherCache: { key: string; at: number; line: string | null } | null = null;

const signed = (x: number) => `${x >= 0 ? '+' : ''}${Math.round(x)}`;

/** «Город: +7°C (feels like +5°C), rain, wind 4 m/s» — как _fetch_weather_line ядра. */
async function weatherLine(env: NonNullable<OfflineKit['env']>): Promise<string | null> {
  const key = `${env.lat},${env.lon}`;
  if (weatherCache && weatherCache.key === key) {
    const age = Date.now() - weatherCache.at;
    if (age < (weatherCache.line ? WEATHER_TTL_MS : WEATHER_RETRY_MS)) return weatherCache.line;
  }
  let line: string | null = null;
  try {
    const ctl = new AbortController();
    const timer = window.setTimeout(() => ctl.abort(), 6000);
    const url =
      `https://api.open-meteo.com/v1/forecast?latitude=${env.lat}&longitude=${env.lon}` +
      '&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m&timezone=auto';
    const r = await fetch(url, { signal: ctl.signal }).finally(() => window.clearTimeout(timer));
    const cur = r.ok ? ((await r.json()) as { current?: Record<string, number> }).current : undefined;
    if (cur) {
      const parts: string[] = [];
      const temp = cur.temperature_2m;
      if (typeof temp === 'number') {
        let t = `${signed(temp)}°C`;
        const feels = cur.apparent_temperature;
        if (typeof feels === 'number' && Math.abs(feels - temp) >= 2) t += ` (feels like ${signed(feels)}°C)`;
        parts.push(t);
      }
      const desc = env.codes?.[String(cur.weather_code)];
      if (desc) parts.push(desc);
      const wind = cur.wind_speed_10m;
      if (typeof wind === 'number' && wind >= 3) parts.push(`wind ${Math.round(wind)} m/s`);
      if (parts.length) line = `${env.city || '?'}: ${parts.join(', ')}`;
    }
  } catch {
    line = null; // нет интернета — без погоды
  }
  weatherCache = { key, at: Date.now(), line };
  return line;
}

/** Блок окружения для промпта: погода (если есть) и время + правило ядра. */
export async function envNote(kit: OfflineKit): Promise<string> {
  const weather = kit.env ? await weatherLine(kit.env) : null;
  const line = weather ? `${weather} | ${timeLine()}` : timeLine();
  return `User's current environment: ${line}\n${kit.rules.env}`;
}

// Поисковик, принявший телефон за бота, — пропускать полчаса (не стучаться
// снова и не ждать его зря)
const BLOCK_MS = 30 * 60_000;
const blockedUntil: Record<string, number> = {};

async function wikiSearch(query: string, max: number): Promise<WebResult[]> {
  const lang = /[а-яё]/i.test(query) ? 'ru' : 'en';
  const url =
    `https://${lang}.wikipedia.org/w/api.php?action=query&list=search&format=json&origin=*` +
    `&srlimit=${max}&srsearch=${encodeURIComponent(query)}`;
  const ctl = new AbortController();
  const timer = window.setTimeout(() => ctl.abort(), 8000);
  try {
    const r = await fetch(url, { signal: ctl.signal });
    const data = (await r.json()) as { query?: { search?: { title: string; snippet: string }[] } };
    return (data.query?.search ?? []).map((x) => ({
      title: x.title,
      body: x.snippet.replace(/<[^>]+>/g, '').replace(/&quot;/g, '"').replace(/&amp;/g, '&'),
      href: `https://${lang}.wikipedia.org/wiki/${encodeURIComponent(x.title.replace(/ /g, '_'))}`,
    }));
  } finally {
    window.clearTimeout(timer);
  }
}

/** Веб-поиск с телефона, как ddgs у ядра — несколько поисковиков по очереди:
 * DuckDuckGo, Bing (страницы выдачи — скрытым WebView), Википедия (API).
 * Без интернета или если никто не ответил — пусто. */
export async function searchWeb(query: string, max: number): Promise<WebResult[]> {
  const q = query.slice(0, 300);
  for (const engine of ['ddg', 'bing'] as const) {
    if ((blockedUntil[engine] ?? 0) > Date.now()) continue;
    try {
      const { results } = await OfflineLlm.webSearch({ query: q, max, engine });
      if (results.length) return results;
    } catch (e) {
      const code = pluginCode(e);
      if (code === 'offline') return []; // нет интернета — и Википедия не ответит
      if (code === 'blocked') blockedUntil[engine] = Date.now() + BLOCK_MS;
    }
  }
  try {
    return await wikiSearch(q, Math.min(max, 3));
  } catch {
    return [];
  }
}

/** Выдача для промпта — как format_web_results ядра, с правилами ядра. */
export function formatWeb(kit: OfflineKit, results: WebResult[], opts: { max: number; snippet: number }): string {
  const parts = results
    .filter((r) => r.body)
    .slice(0, opts.max)
    .map((r, i) => {
      const body = r.body.length > opts.snippet ? `${r.body.slice(0, opts.snippet)}...` : r.body;
      return `${i + 1}. ${r.title}\n   ${body}\n   Source: ${r.href}`;
    });
  return parts.length ? `WEB SEARCH RESULTS:\n${parts.join('\n\n')}\n\n${kit.rules.web}` : '';
}
