// Движок скинов: валидация загруженного файла и подготовка его к
// изолированному рендеру (CSP + эталонный bridge + выбор экрана).

import { BRIDGE_SOURCE, BRIDGE_GEN_PLACEHOLDER } from './bridgeSource';
import { readSkinMeta, SKIN_CONTRACT_VERSION } from './meta';
import type { SkinMeta } from './meta';

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
  // Метаданные <meta name="vpc-skin-*"> (без vpc-skin-contract — контракт v1)
  meta: SkinMeta;
}

// Обязательные точки контракта по экранам: [селектор, описание]
export const REQUIRED: Record<SkinScreen, [string, string][]> = {
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
  const meta = readSkinMeta(html);

  if (new Blob([html]).size > SKIN_MAX_BYTES) {
    errors.push(`Файл больше ${Math.round(SKIN_MAX_BYTES / 1024 / 1024)} МБ — уменьшите inline-ассеты.`);
  }

  const doc = new DOMParser().parseFromString(html, 'text/html');
  if (!doc.documentElement || doc.querySelector('parsererror')) {
    errors.push('Файл не является валидным HTML-документом.');
    return { ok: false, errors, screens: [], meta };
  }

  // Скин новее приложения: hook-точки/события, на которые он рассчитан,
  // здесь могут не работать
  if (meta.contract > SKIN_CONTRACT_VERSION) {
    errors.push(
      `Скин рассчитан на более новую версию приложения: контракт v${meta.contract}, ` +
      `приложение поддерживает v${SKIN_CONTRACT_VERSION}. Обновите приложение или ` +
      `укажите <meta name="vpc-skin-contract" content="${SKIN_CONTRACT_VERSION}">, ` +
      'если скин не использует новых возможностей.',
    );
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

  return { ok: errors.length === 0, errors, screens, meta };
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

// Значение безопасно для подстановки в CSS каркаса. Белый список: каркас
// — документ приложения без CSP, любая загрузка отсюда ушла бы в сеть.
// Никаких url()/image-set()/строк/экранирований — только литералы
// цветов/размеров, ключевые слова и функции цвета, calc и градиенты.
// Шрифтам дополнительно разрешены имена семейств в кавычках
const CSS_SAFE_FUNCS = new Set([
  'rgb', 'rgba', 'hsl', 'hsla', 'hwb', 'lab', 'lch', 'oklab', 'oklch', 'color-mix',
  'var', 'calc', 'min', 'max', 'clamp', 'linear-gradient', 'radial-gradient',
]);
const FONT_VARS = new Set(['vpc-shell-font', 'vpc-shell-font-disp', 'vpc-shell-font-mono']);

function safeCssValue(v: string, font: boolean): boolean {
  if (!v || v.length > 400) return false;
  if (font) {
    // Список семейств: идентификаторы и имена в кавычках через запятую
    return /^\s*(?:"[\w .-]*"|'[\w .-]*'|[a-zA-Z][\w .-]*)(?:\s*,\s*(?:"[\w .-]*"|'[\w .-]*'|[a-zA-Z][\w .-]*))*\s*$/.test(v);
  }
  // Только буквы/цифры/пробелы и безобидная пунктуация: без \, кавычек, ;{}<>@!
  if (!/^[a-zA-Z0-9\s#%.,()+\-*/_]*$/.test(v)) return false;
  const funcs = v.match(/[a-zA-Z-]+(?=\()/g) ?? [];
  if (funcs.some((f) => !CSS_SAFE_FUNCS.has(f.toLowerCase()))) return false;
  // Скобки без имени функции (например, «(url)» после склейки) не пропускаем
  return !/(^|[^a-zA-Z-])\(/.test(v);
}

// Флаг !important в конце значения (перекраска пишет переменные с ним)
const stripImportant = (v: string) => v.replace(/\s*!\s*important\s*$/i, '').trim();

// CSS-правила, привязанные к теме приложения: html[data-theme="light"] { … }
const THEME_BLOCK_RE = /\[data-theme\s*=\s*["']?(light|dark)["']?\s*\][^{}]*\{([^{}]*)\}/g;
const CSS_VAR_RE = /(--[a-zA-Z0-9-]+)\s*:\s*([^;}{]+);/g;

// Извлекает shell-тему из файла скина: переменные каркаса → литеральные значения.
// Базовая палитра — переменные вне блоков [data-theme]; с theme поверх неё
// ложатся переменные из блоков этой темы
export function extractShellTheme(html: string, theme?: 'light' | 'dark'): Record<string, string> {
  // Все CSS-переменные скина (--name: value;) — из <style>-блоков файла
  const props: Record<string, string> = {};
  const themed: string[] = [];
  const base = html.replace(THEME_BLOCK_RE, (_whole: string, t: string, body: string) => {
    if (t === theme) themed.push(body);
    return '';
  });
  let m: RegExpExecArray | null;
  for (const src of [base, ...themed]) {
    CSS_VAR_RE.lastIndex = 0;
    while ((m = CSS_VAR_RE.exec(src))) props[m[1]] = stripImportant(m[2]);
  }

  const out: Record<string, string> = {};
  const shell: Record<string, string> = {};
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
    value = stripImportant(value);
    if (/var\(/.test(value)) continue; // не разрешилась — пропускаем
    if (!safeCssValue(value, FONT_VARS.has(shellName))) continue;
    shell[shellName] = value;
    for (const appVar of appVars) out[appVar] = value;
  }

  // Производные переменные каркаса, которых скин не задаёт: без них
  // полупрозрачные подложки и приглушённый текст остались бы от темы
  // приложения и могли бы слиться с палитрой скина
  // (смешиваются только цвета: градиент в color-mix недопустим)
  const color = (name: string) => (shell[name] && !/gradient\(/i.test(shell[name]) ? shell[name] : null);
  const mix = (a: string, pct: number, b: string) => `color-mix(in srgb, ${a} ${pct}%, ${b})`;
  const bg = color('vpc-shell-bg');
  const text = color('vpc-shell-text');
  const dim = color('vpc-shell-dim');
  const accent = color('vpc-shell-accent');
  if (!shell['vpc-shell-topbar'] && bg) out['--topbar-bg'] = mix(bg, 82, 'transparent');
  if (text) out['--bg-hover'] = mix(text, 6, 'transparent');
  if ((dim || text) && bg) out['--text-muted'] = dim ? mix(dim, 70, bg) : mix(text!, 45, bg);
  if (accent) {
    out['--accent-soft'] = mix(accent, 12, 'transparent');
    out['--border-accent'] = accent;
  }
  return out;
}

// Маркеры блока bridge — комментарии <!-- ==== VPC-BRIDGE:START … --> / <!-- ==== VPC-BRIDGE:END ==== -->
const BRIDGE_START_RE = /^\s*=+\s*VPC-BRIDGE:START\b/;
const BRIDGE_END_RE = /^\s*=+\s*VPC-BRIDGE:END\s*=*\s*$/;

export interface PrepareSkinOptions {
  // Метка документа: bridge прикладывает её к событиям (SkinFrame
  // отсеивает события прошлых документов того же iframe)
  gen?: string;
  // Скрипт, который исполнится раньше любых скриптов скина (зонд смоук-теста)
  headScript?: string;
}

// Эталонный bridge вместо блока между маркерами. Блок заменяется на месте,
// только если оба маркера — соседи прямо в <head>/<body>; иначе bridge
// дописывается в конец <body>. Всё — в DOM, без правки сериализованной строки
function placeBridge(doc: Document, script: HTMLScriptElement) {
  const walker = doc.createTreeWalker(doc, NodeFilter.SHOW_COMMENT);
  let start: Comment | null = null;
  let end: Comment | null = null;
  for (let n = walker.nextNode() as Comment | null; n; n = walker.nextNode() as Comment | null) {
    if (!start) {
      if (BRIDGE_START_RE.test(n.data)) start = n;
    } else if (BRIDGE_END_RE.test(n.data)) {
      end = n;
      break;
    }
  }
  const parent = start?.parentNode;
  if (start && end && parent && end.parentNode === parent && (parent === doc.body || parent === doc.head)) {
    while (start.nextSibling && start.nextSibling !== end) parent.removeChild(start.nextSibling);
    parent.insertBefore(script, end);
    return;
  }
  doc.body!.appendChild(script);
}

// Подготовка скина к рендеру: вырезает опасные/внешние теги, заменяет
// bridge-блок эталонным, вставляет CSP-мету первой в <head>, выставляет
// активный экран. Итог — сериализация DOM без строковых вставок
export function prepareSkin(html: string, screen: SkinScreen, opts: PrepareSkinOptions = {}): string {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const root = doc.documentElement;

  // Защитная чистка: эти теги не нужны скину и потенциально опасны
  doc
    .querySelectorAll(
      'base, link, iframe, frame, frameset, object, embed, portal, fencedframe, meta[http-equiv], script[src]',
    )
    .forEach((el) => el.remove());
  root.removeAttribute('manifest');
  if (!doc.body) root.appendChild(doc.createElement('body'));
  let head = doc.head;
  if (!head) {
    head = doc.createElement('head');
    root.insertBefore(head, root.firstChild);
  }

  // Bridge: заменить блок между маркерами эталонной версией
  const bridge = doc.createElement('script');
  bridge.textContent = BRIDGE_SOURCE.replace(BRIDGE_GEN_PLACEHOLDER, (opts.gen ?? '').replace(/[^\w-]/g, ''));
  placeBridge(doc, bridge);

  // CSP: никакой сети, только inline скрипты/стили и data:-ассеты. Мета —
  // самый первый узел <head>: политика действует только на то, что ниже неё,
  // а до <head> в сериализации идёт лишь тег <html> (без манифеста)
  const meta = doc.createElement('meta');
  meta.setAttribute('http-equiv', 'Content-Security-Policy');
  meta.setAttribute('content', CSP);
  if (opts.headScript) {
    const probe = doc.createElement('script');
    probe.textContent = opts.headScript;
    head.insertBefore(probe, head.firstChild);
  }
  head.insertBefore(meta, head.firstChild);
  // Перед <head> ничего не остаётся (там бывают лишь комментарии/пробелы)
  while (root.firstChild && root.firstChild !== head) root.removeChild(root.firstChild);

  // Активный экран
  root.setAttribute('data-vpc-active', screen);
  // Версия контракта скина — bridge читает её отсюда (v1 — файл без меты)
  root.setAttribute('data-vpc-contract', String(readSkinMeta(html).contract));

  return '<!DOCTYPE html>\n' + root.outerHTML;
}
