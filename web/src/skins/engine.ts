// Движок скинов: валидация загруженного файла и подготовка его к
// изолированному рендеру (CSP + эталонный bridge + выбор экрана).

import { BRIDGE_SOURCE, BRIDGE_START, BRIDGE_END } from './bridgeSource';

export type SkinScreen = 'chat' | 'room' | 'dossier';

export const SKIN_MAX_BYTES = 3 * 1024 * 1024; // 3 МБ — лимит файла скина

const CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'";

export interface SkinValidation {
  ok: boolean;
  errors: string[];
  // Экраны, найденные в файле (per-screen файлы содержат один,
  // legacy-файлы — все три сразу)
  screens: SkinScreen[];
}

// Обязательные точки контракта по экранам: [селектор, описание]
const REQUIRED: Record<SkinScreen, [string, string][]> = {
  chat: [
    ['[data-vpc-screen="chat"] [data-vpc="messages"]', 'контейнер ленты [data-vpc="messages"] в экране чата'],
    ['[data-vpc-screen="chat"] template[data-vpc="message"]', 'шаблон сообщения <template data-vpc="message">'],
    ['[data-vpc-screen="chat"] [data-vpc="input"]', 'поле ввода [data-vpc="input"]'],
    ['[data-vpc-screen="chat"] [data-vpc="send"]', 'кнопка отправки [data-vpc="send"]'],
  ],
  room: [
    ['[data-vpc-screen="room"] [data-vpc="room-scene"]', 'сцена [data-vpc="room-scene"] в экране комнаты'],
    ['[data-vpc-screen="room"] [data-vpc="room-avatar"]', 'аватар [data-vpc="room-avatar"] в экране комнаты'],
    ['[data-vpc-screen="room"] [data-vpc="room-pastime"]', 'подпись занятия [data-vpc="room-pastime"] в экране комнаты'],
  ],
  dossier: [],
};

const SCREENS: SkinScreen[] = ['chat', 'dossier', 'room'];

// Какие экраны содержит файл скина
export function detectScreens(html: string): SkinScreen[] {
  return SCREENS.filter((s) => html.includes('data-vpc-screen="' + s + '"'));
}

// Проверка файла скина перед применением. Ошибки формулируются так,
// чтобы их можно было отдать нейросети-редактору как отчёт.
export function validateSkin(html: string): SkinValidation {
  const errors: string[] = [];

  if (new Blob([html]).size > SKIN_MAX_BYTES) {
    errors.push(`Файл больше ${Math.round(SKIN_MAX_BYTES / 1024 / 1024)} МБ — уменьшите inline-ассеты.`);
  }

  const doc = new DOMParser().parseFromString(html, 'text/html');
  if (!doc.documentElement || doc.querySelector('parsererror')) {
    errors.push('Файл не является валидным HTML-документом.');
    return { ok: false, errors, screens: [] };
  }

  const screens = detectScreens(html);
  if (screens.length === 0) {
    errors.push('Не найден ни один экран: в файле должен быть блок [data-vpc-screen="chat"], [data-vpc-screen="dossier"] или [data-vpc-screen="room"].');
  }

  for (const screen of screens) {
    for (const [selector, label] of REQUIRED[screen]) {
      if (!doc.querySelector(selector)) {
        errors.push(`Не найдена обязательная точка: ${label}.`);
      }
    }
  }

  // В шаблоне сообщения обязательно поле текста
  const msgTpl = doc.querySelector<HTMLTemplateElement>('[data-vpc-screen="chat"] template[data-vpc="message"]');
  if (msgTpl && !msgTpl.content.querySelector('[data-vpc-field="text"]')) {
    errors.push('В шаблоне сообщения нет обязательного поля [data-vpc-field="text"].');
  }

  // Внешние ресурсы запрещены: CSP их всё равно заблокирует, но лучше
  // честно сказать редактору, что скин будет выглядеть сломанным
  if (/<\s*(script|link|img|iframe|source|video|audio)[^>]+\bsrc\s*=\s*["']?\s*https?:/i.test(html) ||
      /<\s*link[^>]+\bhref\s*=\s*["']?\s*https?:/i.test(html) ||
      /url\(\s*["']?\s*https?:/i.test(html) ||
      /@import/i.test(html)) {
    errors.push('Найдены внешние ресурсы (http/https, @import, url()). Сеть заблокирована: используйте inline data-URI или CSS/SVG.');
  }

  return { ok: errors.length === 0, errors, screens };
}

// Есть ли в скине отдельный экран (например, досье).
export function skinHasScreen(html: string, screen: SkinScreen): boolean {
  return html.includes('data-vpc-screen="' + screen + '"');
}

/* Shell-тема: скин может объявить у себя переменные --vpc-shell-*, и тогда
   приложение перекрашивает каркас (левый бар, топбар), пока открыт экран
   персоны со скином. Значения могут ссылаться на собственные переменные
   скина через var() — здесь они разрешаются в литералы. Чисто regex'ами
   (без DOM), чтобы работало и вне браузерного рендера. */

// Соответствие переменных скина переменным каркаса приложения
const SHELL_VAR_MAP: Record<string, string[]> = {
  'vpc-shell-bg': ['--bg'],
  'vpc-shell-panel': ['--bg-panel', '--bg-panel-2'],
  'vpc-shell-text': ['--text'],
  'vpc-shell-dim': ['--text-dim', '--accent-dim'],
  'vpc-shell-border': ['--border', '--border-soft'],
  'vpc-shell-accent': ['--accent', '--accent-dark'],
  'vpc-shell-topbar': ['--topbar-bg'],
  'vpc-shell-font': ['--font'],
  'vpc-shell-font-disp': ['--font-disp'],
  'vpc-shell-font-mono': ['--font-mono'],
  'vpc-shell-radius': ['--radius', '--radius-sm'],
  'vpc-shell-shadow': ['--shadow-card', '--shadow-card-hover'],
};

// Значение безопасно для подстановки в CSS каркаса: никаких тегов, импортов
// и сетевых url() (data:-URI разрешены — сеть скину всё равно закрыта CSP)
function safeCssValue(v: string): boolean {
  if (v.length > 400) return false;
  const low = v.toLowerCase();
  if (low.includes('<') || low.includes('>') || low.includes('@')) return false;
  if (low.includes('expression(') || low.includes('javascript')) return false;
  if (/url\(\s*["']?\s*(?!data:)/i.test(v)) return false;
  return true;
}

// Извлекает shell-тему из файла скина: переменные каркаса → литеральные значения
export function extractShellTheme(html: string): Record<string, string> {
  // Все CSS-переменные скина (--name: value;) — из <style>-блоков файла
  const props: Record<string, string> = {};
  const re = /(--[a-zA-Z0-9-]+)\s*:\s*([^;}{]+);/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html))) props[m[1]] = m[2].trim();

  const out: Record<string, string> = {};
  for (const [shellName, appVars] of Object.entries(SHELL_VAR_MAP)) {
    let value = props['--' + shellName];
    if (!value) continue;
    // Разрешаем var(--x) и var(--x, fallback) ссылками на переменные скина
    for (let i = 0; i < 6 && /var\(--/.test(value); i++) {
      value = value.replace(
        /var\(\s*(--[a-zA-Z0-9-]+)\s*(?:,\s*([^)]*))?\)/g,
        (whole: string, name: string, fb?: string) =>
          props[name] != null ? props[name] : fb != null ? fb.trim() : whole,
      );
    }
    if (/var\(--/.test(value)) continue; // не разрешилась — пропускаем
    if (!safeCssValue(value)) continue;
    for (const appVar of appVars) out[appVar] = value;
  }
  return out;
}

// Подготовка скина к рендеру: вырезает опасные/внешние теги, вставляет
// CSP-мету, заменяет bridge-блок эталонным, выставляет активный экран.
export function prepareSkin(html: string, screen: SkinScreen): string {
  const doc = new DOMParser().parseFromString(html, 'text/html');

  // Защитная чистка: эти теги не нужны скину и потенциально опасны
  doc
    .querySelectorAll('base, link, iframe, object, embed, meta[http-equiv], script[src]')
    .forEach((el) => el.remove());

  // CSP: никакой сети, только inline скрипты/стили и data:-ассеты
  const meta = doc.createElement('meta');
  meta.setAttribute('http-equiv', 'Content-Security-Policy');
  meta.setAttribute('content', CSP);
  doc.head.insertBefore(meta, doc.head.firstChild);

  // Активный экран
  doc.documentElement.setAttribute('data-vpc-active', screen);

  let out = '<!DOCTYPE html>\n' + doc.documentElement.outerHTML;

  // Bridge: заменить блок между маркерами эталонной версией;
  // если маркеры выпилены — дописать перед </body>
  const bridgeTag = '<script>' + BRIDGE_SOURCE + '</' + 'script>';
  const start = out.indexOf(BRIDGE_START);
  const end = out.indexOf(BRIDGE_END);
  if (start !== -1 && end !== -1 && end > start) {
    out = out.slice(0, start) + BRIDGE_START + ' ==== -->\n' + bridgeTag + '\n<!-- ==== ' + BRIDGE_END + out.slice(end + BRIDGE_END.length);
  } else if (out.includes('</body>')) {
    out = out.replace('</body>', bridgeTag + '\n</body>');
  } else {
    out += bridgeTag;
  }

  return out;
}
