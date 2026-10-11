/* Язык ответа без ноутбука — как у ядра (app/core/language.py): по текущей
   реплике, в ней нет букв — по прежним репликам пользователя, иначе язык
   чата из кита. В ките строка языка готовая, но она — с момента его
   загрузки: на английскую реплику персона отвечала по-русски.
   Без зависимостей приложения (prompt.ts проверяется под Node). */

const NAMES: Record<string, string> = { ru: 'Russian', en: 'English' };

// Обёртки для файлов и картинок код пишет по-английски — это не речь пользователя
const SYNTHETIC_PREFIXES = ['the user sent'];

/** Язык одного текста: 'ru' / 'en' / null (букв нет или синтетика). */
export function detectLanguage(text: string): string | null {
  if (!text) return null;
  const low = text.trim().toLowerCase();
  if (SYNTHETIC_PREFIXES.some((p) => low.startsWith(p))) return null;
  const cyr = (text.match(/[а-яё]/gi) ?? []).length;
  const lat = (text.match(/[a-z]/gi) ?? []).length;
  if (cyr > 0 && cyr >= lat) return 'ru';
  if (lat > 0) return 'en';
  return null;
}

/** Язык, на котором пользователь говорит сейчас; fallback — язык чата из кита. */
export function dialogueLanguage(
  text: string,
  history: { role: string; text: string }[],
  fallback?: string | null,
): string | null {
  const now = detectLanguage(text);
  if (now) return now;
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i].role !== 'user') continue;
    const lang = detectLanguage(history[i].text);
    if (lang) return lang;
  }
  return fallback ?? null;
}

export function languageName(code: string | null | undefined): string | null {
  return (code && NAMES[code]) || null;
}

/** Строка языка для системного текста — как user_language_line ядра. */
export function languageLine(code: string | null | undefined): string {
  const name = languageName(code);
  if (!name) return "The user's language is not known yet: write all free-text output in the language of the user's messages.";
  return `The user speaks ${name} in the dialogue: write all free-text output in ${name}.`;
}
