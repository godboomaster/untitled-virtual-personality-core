type T = (key: string, vars?: Record<string, string | number>) => string;

// «только что» / «N мин назад» / «сегодня в 14:02» / «вчера в 09:30» / «03.10»
// (главная, контекст чата). ts — unix-секунды; нет — «ещё не было»
export function agoLabel(ts: number | null | undefined, now: Date, t: T, locale: string): string {
  if (!ts) return t('home.never');
  const sec = Math.max(0, now.getTime() / 1000 - ts);
  if (sec < 60) return t('home.agoNow');
  if (sec < 3600) return t('home.agoMin', { n: Math.floor(sec / 60) });
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (ts >= startOfToday) return t('home.agoToday', { t: hm });
  if (ts >= startOfToday - 86400) return t('home.agoYesterday', { t: hm });
  return d.toLocaleDateString(locale, { day: '2-digit', month: '2-digit' });
}
