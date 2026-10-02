/* Контракт скина — единый источник правды о том, что приложение даёт
   скину и что принимает от него: обязательные и необязательные hook-точки,
   поля снапшота, события скин → хост, JS API window.vpc, правила песочницы.

   Отсюда собирается markdown-описание контракта (buildSkinContractDoc) —
   его отдают нейросети вместе с файлом-шаблоном («вот контракт, переделай
   скин»). Описания полей снапшота привязаны к TS-типам через satisfies:
   новое поле в payloads.ts без описания здесь — ошибка компиляции. */

import { REQUIRED } from './engine';
import type { SkinScreen } from './engine';
import { SKIN_CONTRACT_VERSION } from './meta';
import { SKIN_LABELS } from './payloads';
import type {
  SkinChatMessage,
  SkinContext,
  SkinDossier,
  SkinEnv,
  SkinPersonaInfo,
  SkinRoomState,
  SkinStatePayload,
  SkinWeather,
} from './payloads';
import { BRIDGE_SOURCE } from './bridgeSource';

export type SkinDocLocale = 'ru' | 'en';

// Двуязычный текст описания
export interface SkinDocText {
  ru: string;
  en: string;
}

// Описания всех ключей типа T (включая необязательные)
type FieldDocs<T> = Record<keyof T, SkinDocText>;

const tx = (ru: string, en: string): SkinDocText => ({ ru, en });

/* ── Поля снапшота хост → скин ({vpc:'host', type:'state', payload}) ── */

export const PAYLOAD_FIELDS = {
  persona: tx('текущая персона (см. persona.*)', 'current persona (see persona.*)'),
  env: tx('окружение: тема, язык, подписи UI, время суток, погода (см. env.*)', 'environment: theme, locale, UI labels, time of day, weather (see env.*)'),
  typing: tx('персона печатает ответ', 'persona is typing a reply'),
  messages: tx('лента чата, от старых к новым (см. message.*)', 'chat feed, oldest first (see message.*)'),
  reply: tx('плашка «ответ на…»: {author, text} или null', '"replying to" bar: {author, text} or null'),
  personas: tx('список персон: {id, name, statusText, active}', 'persona list: {id, name, statusText, active}'),
  context: tx('контекстная панель чата, строки уже локализованы (см. context.*)', 'chat context panel, strings already localized (see context.*)'),
  todos: tx('дела: {id, text, done}', 'todos: {id, text, done}'),
  inventory: tx('инвентарь: {icon, name, description, tag}', 'inventory: {icon, name, description, tag}'),
  dossier: tx('данные досье (см. dossier.*)', 'dossier data (see dossier.*)'),
  files: tx('файлы персоны: {id, name, kind, size, date, description}', 'persona files: {id, name, kind, size, date, description}'),
  settings: tx('параметры генерации: {temperature, maxTokens, topP, stmSize}', 'generation settings: {temperature, maxTokens, topP, stmSize}'),
  providers: tx('LLM-провайдеры: {id, name, local, model, active, backup, keyLabel}', 'LLM providers: {id, name, local, model, active, backup, keyLabel}'),
  providerModels: tx('те же провайдеры для карточки «Модели провайдеров»', 'same providers for the "Provider models" card'),
  featureFlags: tx('флаги фич: {id, label, enabled}', 'feature flags: {id, label, enabled}'),
  room: tx('состояние комнаты (см. room.*)', 'room state (see room.*)'),
  feed: tx('лента событий комнаты: {time, text}', 'room activity feed: {time, text}'),
} satisfies FieldDocs<SkinStatePayload>;

export const PERSONA_FIELDS = {
  id: tx('идентификатор', 'identifier'),
  name: tx('имя', 'name'),
  statusText: tx('статус присутствия, локализован', 'presence status, localized'),
  model: tx('модель LLM', 'LLM model'),
  mood: tx('настроение', 'mood'),
  pastime: tx('текущее занятие', 'current pastime'),
  avatar: tx('аватар, data-URL', 'avatar, data-URL'),
} satisfies FieldDocs<SkinPersonaInfo>;

export const MESSAGE_FIELDS = {
  id: tx('стабильный id сообщения (ключ сверки ленты)', 'stable message id (feed reconciliation key)'),
  role: tx("'user' | 'persona'", "'user' | 'persona'"),
  text: tx('текст', 'text'),
  time: tx('время, строкой', 'time, as a string'),
  image: tx('картинка, data-URL', 'image, data-URL'),
  quote: tx('цитата ответа: {author, text}', 'replied-to quote: {author, text}'),
} satisfies FieldDocs<SkinChatMessage>;

export const ENV_FIELDS = {
  theme: tx("тема приложения: 'light' | 'dark' (дублируется в html[data-theme])", "app theme: 'light' | 'dark' (mirrored to html[data-theme])"),
  locale: tx("язык интерфейса: 'ru' | 'en' (дублируется в html[lang])", "UI language: 'ru' | 'en' (mirrored to html[lang])"),
  labels: tx('локализованные подписи UI по ключам меток (см. «Подписи»)', 'localized UI labels by label key (see "Labels")'),
  timeOfDay: tx("'morning' | 'day' | 'evening' | 'night' (дублируется в html[data-vpc-time-of-day])", "'morning' | 'day' | 'evening' | 'night' (mirrored to html[data-vpc-time-of-day])"),
  localTime: tx("локальное время пользователя 'HH:MM'", "user's local time 'HH:MM'"),
  weather: tx('погода, если задано местоположение (см. weather.*)', 'weather, if a location is configured (see weather.*)'),
} satisfies FieldDocs<SkinEnv>;

export const WEATHER_FIELDS = {
  text: tx('строка погоды как есть (город, температура, описание по-английски)', 'weather line as is (city, temperature, English description)'),
  city: tx('город', 'city'),
  tempC: tx('температура, °C', 'temperature, °C'),
  condition: tx("'clear' | 'cloudy' | 'fog' | 'rain' | 'snow' | 'storm' (дублируется в html[data-vpc-weather])", "'clear' | 'cloudy' | 'fog' | 'rain' | 'snow' | 'storm' (mirrored to html[data-vpc-weather])"),
} satisfies FieldDocs<SkinWeather>;

export const CONTEXT_FIELDS = {
  pastime: tx('занятие · место', 'pastime · place'),
  mood: tx('настроение', 'mood'),
  trend: tx('тренд настроения', 'mood trend'),
  initiative: tx('строка вероятности самоинициативы', 'proactivity probability line'),
  lastReply: tx('давность последнего ответа пользователя', 'time since the user last replied'),
  nextReminder: tx('ближайшее напоминание', 'next reminder'),
  learning: tx("активный курс ('' — нет)", "active course ('' — none)"),
  features: tx('подписи включённых модулей', 'labels of enabled modules'),
} satisfies FieldDocs<SkinContext>;

export const DOSSIER_FIELDS = {
  facts: tx('факты LTM: {id, category, fact}', 'LTM facts: {id, category, fact}'),
  reminders: tx('напоминания: {id, time, text, repeat, active, date, clock}', 'reminders: {id, time, text, repeat, active, date, clock}'),
  initiatives: tx('история инициатив: {typeLabel, text, time, outcome}', 'initiative history: {typeLabel, text, time, outcome}'),
  diary: tx('дневник: {date, text}', 'diary: {date, text}'),
  stm: tx('буфер STM: {id, role, author, text, time}', 'STM buffer: {id, role, author, text, time}'),
  courses: tx('учебные курсы (статистика, прогресс, темы, словарь)', 'learning courses (stats, progress, topics, vocabulary)'),
  initState: tx('параметры и состояние самоинициативы', 'proactivity parameters and state'),
} satisfies FieldDocs<SkinDossier>;

export const ROOM_FIELDS = {
  pastimeLabel: tx('занятие', 'pastime'),
  pastimePlace: tx('место в комнате', 'spot in the room'),
  duration: tx('длительность занятия', 'pastime duration'),
  x: tx('позиция аватара, % ширины сцены (→ --vpc-x)', 'avatar position, % of scene width (→ --vpc-x)'),
  y: tx('позиция аватара, % высоты сцены (→ --vpc-y)', 'avatar position, % of scene height (→ --vpc-y)'),
  mood: tx('настроение', 'mood'),
  energy: tx('энергия', 'energy'),
  pet: tx("питомец: 'cat' | 'crow' | 'none'", "pet: 'cat' | 'crow' | 'none'"),
  petLabel: tx('подпись питомца', 'pet label'),
  bg: tx('пользовательский фон, data-URL', 'custom background, data-URL'),
  sprite: tx('спрайт аватара, data-URL', 'avatar sprite, data-URL'),
  spot: tx(
    "место: desk | window | shelf | bed | chair | floor | away | item:<название> (необязательно)",
    "spot: desk | window | shelf | bed | chair | floor | away | item:<name> (optional)",
  ),
  pose: tx(
    'поза: stand | sit | read | write | look | sleep | away | with_you | glance (необязательно)',
    'pose: stand | sit | read | write | look | sleep | away | with_you | glance (optional)',
  ),
  timeOfDay: tx("время суток: 'morning' | 'day' | 'evening' | 'night' (необязательно)", "time of day: 'morning' | 'day' | 'evening' | 'night' (optional)"),
  pastimeSince: tx('начало занятия, epoch-секунды (необязательно)', 'pastime start, epoch seconds (optional)'),
} satisfies FieldDocs<SkinRoomState>;

const FIELD_GROUPS: [string, Record<string, SkinDocText>][] = [
  ['payload', PAYLOAD_FIELDS],
  ['persona', PERSONA_FIELDS],
  ['message', MESSAGE_FIELDS],
  ['env', ENV_FIELDS],
  ['env.weather', WEATHER_FIELDS],
  ['context', CONTEXT_FIELDS],
  ['dossier', DOSSIER_FIELDS],
  ['room', ROOM_FIELDS],
];

/* ── Hook-точки ── */

// Обязательные точки — из валидатора движка (engine.REQUIRED)
export const REQUIRED_HOOKS: Record<SkinScreen, { selector: string; label: string }[]> = {
  chat: REQUIRED.chat.map(([selector, label]) => ({ selector, label })),
  dossier: REQUIRED.dossier.map(([selector, label]) => ({ selector, label })),
  room: REQUIRED.room.map(([selector, label]) => ({ selector, label })),
};

export interface SkinSlotDoc {
  name: string; // значение data-vpc
  screens: SkinScreen[];
  desc: SkinDocText;
}

// Скалярные слоты [data-vpc="…"]: текст подставляется внутрь элемента
export const TEXT_SLOTS: SkinSlotDoc[] = [
  { name: 'persona-name', screens: ['chat', 'dossier', 'room'], desc: tx('имя персоны', 'persona name') },
  { name: 'persona-status', screens: ['chat', 'dossier', 'room'], desc: tx('статус', 'status') },
  { name: 'persona-model', screens: ['chat', 'dossier'], desc: tx('модель LLM', 'LLM model') },
  { name: 'persona-mood', screens: ['chat'], desc: tx('настроение', 'mood') },
  { name: 'persona-pastime', screens: ['chat'], desc: tx('занятие', 'pastime') },
  { name: 'ctx-pastime', screens: ['chat'], desc: tx('занятие · место', 'pastime · place') },
  { name: 'ctx-mood', screens: ['chat'], desc: tx('настроение', 'mood') },
  { name: 'ctx-trend', screens: ['chat'], desc: tx('тренд настроения', 'mood trend') },
  { name: 'ctx-initiative', screens: ['chat'], desc: tx('вероятность самоинициативы', 'proactivity probability') },
  { name: 'ctx-last-reply', screens: ['chat'], desc: tx('последний ответ пользователя', 'last user reply') },
  { name: 'ctx-next-reminder', screens: ['chat'], desc: tx('ближайшее напоминание', 'next reminder') },
  { name: 'ctx-learning', screens: ['chat'], desc: tx('активный курс', 'active course') },
  { name: 'ini-probability', screens: ['dossier'], desc: tx('вероятность, %', 'probability, %') },
  { name: 'ini-threshold', screens: ['dossier'], desc: tx('порог молчания, мин', 'silence threshold, min') },
  { name: 'ini-max-per-day', screens: ['dossier'], desc: tx('максимум в день', 'max per day') },
  { name: 'ini-interval', screens: ['dossier'], desc: tx('интервал проверки, мин', 'check interval, min') },
  { name: 'ini-adaptive', screens: ['dossier'], desc: tx("адаптивный порог ('✓' / '—')", "adaptive threshold ('✓' / '—')") },
  { name: 'ini-bayes', screens: ['dossier'], desc: tx("байесовский фидбек ('✓' / '—')", "Bayesian feedback ('✓' / '—')") },
  { name: 'ini-ignore-streak', screens: ['dossier'], desc: tx('серия игнорирования', 'ignore streak') },
  { name: 'ini-today', screens: ['dossier'], desc: tx('инициатив сегодня', 'initiatives today') },
  { name: 'ini-mood', screens: ['dossier'], desc: tx('эмоциональное состояние', 'emotional state') },
  { name: 'ini-stages', screens: ['dossier'], desc: tx('шкала состояний', 'state scale') },
  { name: 'ini-silence-text', screens: ['dossier'], desc: tx('подпись прогресса молчания', 'silence progress caption') },
  { name: 'room-pastime', screens: ['room'], desc: tx('занятие · место (обязателен в комнате)', 'pastime · place (required in room)') },
  { name: 'room-mood', screens: ['room'], desc: tx('настроение', 'mood') },
  { name: 'room-energy', screens: ['room'], desc: tx('энергия', 'energy') },
  { name: 'room-place', screens: ['room'], desc: tx('место в комнате', 'spot in the room') },
  { name: 'room-pet-label', screens: ['room'], desc: tx('подпись питомца', 'pet label') },
  { name: 'local-time', screens: ['chat', 'dossier', 'room'], desc: tx("локальное время 'HH:MM' (v2; часовой пояс — из настроек приложения)", "local time 'HH:MM' (v2; time zone from the app settings)") },
  { name: 'weather', screens: ['chat', 'dossier', 'room'], desc: tx('строка погоды (v2; пусто — погоды нет)', 'weather line (v2; empty — no weather)') },
  { name: 'weather-temp', screens: ['chat', 'dossier', 'room'], desc: tx("температура '+7°C' (v2; пусто — погоды нет)", "temperature '+7°C' (v2; empty — no weather)") },
];

// Элементы-контролы и особые точки [data-vpc="…"]
export const CONTROL_HOOKS: SkinSlotDoc[] = [
  { name: 'input', screens: ['chat'], desc: tx('поле ввода (<input>/<textarea>), Enter = отправка', 'message input (<input>/<textarea>), Enter sends') },
  { name: 'send', screens: ['chat'], desc: tx('кнопка отправки', 'send button') },
  { name: 'messages', screens: ['chat'], desc: tx('контейнер ленты; + <template data-vpc="message">', 'feed container; + <template data-vpc="message">') },
  { name: 'typing-indicator', screens: ['chat'], desc: tx('получает data-active, пока персона печатает', 'gets data-active while the persona is typing') },
  { name: 'persona-avatar', screens: ['chat', 'dossier', 'room'], desc: tx('<img> получает src, иначе — первая буква имени текстом', '<img> gets src, otherwise the first letter of the name as text') },
  { name: 'open-dossier', screens: ['chat'], desc: tx('клик = открыть досье', 'click = open dossier') },
  { name: 'close-dossier', screens: ['dossier'], desc: tx('клик = закрыть досье', 'click = close dossier') },
  { name: 'attach-image', screens: ['chat'], desc: tx('клик = выбрать картинку к сообщению', 'click = pick an image for the message') },
  { name: 'attach-preview', screens: ['chat'], desc: tx('превью выбранной картинки (hidden, пока её нет)', 'preview of the picked image (hidden when none)') },
  { name: 'cancel-attach', screens: ['chat'], desc: tx('клик = убрать картинку', 'click = remove the image') },
  { name: 'reply-bar', screens: ['chat'], desc: tx('плашка «ответ на…»: hidden ставит приложение; поля reply-author / reply-text; кнопка [data-vpc-action="cancel-reply"]', '"replying to" bar: app toggles hidden; fields reply-author / reply-text; button [data-vpc-action="cancel-reply"]') },
  { name: 'clear-chat', screens: ['dossier'], desc: tx('клик = очистить диалог (только в опасной зоне досье)', 'click = clear the dialog (dossier danger zone only)') },
  { name: 'ini-silence-bar', screens: ['dossier'], desc: tx('полоса прогресса молчания: style.width в %', 'silence progress bar: style.width in %') },
  { name: 'room-scene', screens: ['room'], desc: tx('корень сцены комнаты', 'room scene root') },
  { name: 'room-avatar', screens: ['room'], desc: tx('аватар; приложение ставит CSS-переменные --vpc-x / --vpc-y (%)', 'avatar; the app sets CSS vars --vpc-x / --vpc-y (%)') },
  { name: 'room-bg', screens: ['room'], desc: tx('<img> пользовательского фона', 'custom background <img>') },
  { name: 'room-sprite', screens: ['room'], desc: tx('<img> спрайта аватара', 'avatar sprite <img>') },
  { name: 'room-pet', screens: ['room'], desc: tx("питомец: data-pet='cat|crow', hidden без питомца", "pet: data-pet='cat|crow', hidden when none") },
];

export interface SkinListDoc {
  box: string; // data-vpc контейнера
  tpl: string; // data-vpc шаблона <template>
  fields: string[]; // data-vpc-field внутри шаблона
  root?: string; // атрибуты, которые получает корень клона
  count?: string; // ключ data-vpc-count
  itemActions?: string[]; // data-vpc-item-action внутри элемента
  screens: SkinScreen[];
}

// Повторяющиеся списки: контейнер + <template>-образец одного элемента
export const LIST_HOOKS: SkinListDoc[] = [
  { box: 'messages', tpl: 'message', fields: ['text', 'time', 'image', 'quote-author', 'quote-text'], root: 'data-role="user|persona"', itemActions: ['reply'], screens: ['chat'] },
  { box: 'persona-list', tpl: 'persona-item', fields: ['name', 'status'], root: 'data-active (selected persona); click = switch persona', count: 'personas', screens: ['chat'] },
  { box: 'todo-list', tpl: 'todo-item', fields: ['text'], root: 'data-done', count: 'todos', itemActions: ['toggle-todo', 'edit-todo', 'delete-todo'], screens: ['chat', 'dossier'] },
  { box: 'inventory-list', tpl: 'inventory-item', fields: ['icon', 'name', 'description', 'tag'], count: 'inventory', screens: ['chat'] },
  { box: 'feature-list', tpl: 'feature-item', fields: ['label'], count: 'features', screens: ['chat'] },
  { box: 'room-inventory', tpl: 'inventory-item', fields: ['icon', 'name', 'description', 'tag'], count: 'inventory', screens: ['room'] },
  { box: 'room-feed', tpl: 'feed-item', fields: ['time', 'text'], count: 'feed', screens: ['room'] },
  { box: 'stm-list', tpl: 'stm-item', fields: ['author', 'role', 'text', 'time'], root: 'data-role="user|persona"', count: 'stm', itemActions: ['delete-stm'], screens: ['dossier'] },
  { box: 'fact-list', tpl: 'fact-item', fields: ['category', 'fact'], count: 'facts', itemActions: ['edit-fact', 'delete-fact'], screens: ['dossier'] },
  { box: 'diary-list', tpl: 'diary-item', fields: ['date', 'text'], count: 'diary', screens: ['dossier'] },
  { box: 'reminder-list', tpl: 'reminder-item', fields: ['time', 'text', 'repeat', 'date', 'clock'], root: 'data-active="true|false"', count: 'reminders', itemActions: ['toggle-reminder', 'edit-reminder', 'delete-reminder'], screens: ['dossier'] },
  { box: 'initiative-list', tpl: 'initiative-item', fields: ['type-label', 'text', 'time', 'outcome'], count: 'initiatives', screens: ['dossier'] },
  { box: 'course-list', tpl: 'course-item', fields: ['subject', 'status', 'lessons', 'topics', 'words', 'quiz', 'frequency', 'next', 'progress', 'topics-list', 'vocab-list', 'quiz-line'], root: 'data-status="active|paused|finished"', count: 'courses', screens: ['dossier'] },
  { box: 'course-history-list', tpl: 'course-history-item', fields: ['subject', 'status', 'lessons'], root: 'data-status', count: 'courseHistory', screens: ['dossier'] },
  { box: 'file-list', tpl: 'file-item', fields: ['name', 'kind', 'size', 'date', 'description'], count: 'files', itemActions: ['download-file'], screens: ['dossier'] },
  { box: 'provider-list', tpl: 'provider-item', fields: ['name', 'model', 'local', 'key-label'], root: 'data-main="true|false", data-backup="true|false"', count: 'providers', itemActions: ['make-main', 'toggle-backup'], screens: ['dossier'] },
  { box: 'pmodel-list', tpl: 'pmodel-item', fields: ['name', 'local', 'model (<input data-vpc-input="model" data-vpc-onchange="set-model">)'], count: 'pmodels', screens: ['dossier'] },
  { box: 'feature-flag-list', tpl: 'feature-flag-item', fields: ['label'], root: 'data-enabled="true|false"', count: 'featureFlags', itemActions: ['toggle-feature'], screens: ['dossier'] },
];

// Действия форм [data-vpc-form] + [data-vpc-action] (поля — data-vpc-input)
export const FORM_ACTIONS: { action: string; inputs: string[]; screens: SkinScreen[] }[] = [
  { action: 'add-todo', inputs: ['text'], screens: ['dossier'] },
  { action: 'add-reminder', inputs: ['date', 'clock', 'text', 'repeat'], screens: ['dossier'] },
  { action: 'add-fact', inputs: ['category', 'fact'], screens: ['dossier'] },
  { action: 'add-course', inputs: ['subject', 'frequency'], screens: ['dossier'] },
  { action: 'trim-stm', inputs: ['count'], screens: ['dossier'] },
  { action: 'add-inventory-item', inputs: ['name', 'icon'], screens: ['room'] },
  { action: 'cancel-reply', inputs: [], screens: ['chat'] },
];

// Инпуты настроек [data-vpc-setting="…"]
export const SETTING_KEYS = [
  'temperature', 'maxTokens', 'topP', 'stmSize',
  'iniSilence', 'iniProbability', 'iniMaxPerDay', 'iniInterval', 'iniAdaptive', 'iniBayes',
] as const;

// Атрибуты разметки, которые понимает bridge
export const MARKUP_ATTRS: { attr: string; desc: SkinDocText }[] = [
  { attr: 'data-vpc-field="name"', desc: tx('поле внутри <template>; пустое значение — элемент убирается из клона', 'field inside a <template>; an empty value removes the element from the clone') },
  { attr: 'data-vpc-optional', desc: tx('контейнер убирается, если в нём не осталось ни одного поля', 'container is removed when no field is left inside') },
  { attr: 'data-vpc-bar', desc: tx('поле-полоса: style.width = значение в %', 'bar field: style.width = value in %') },
  { attr: 'data-vpc-count="key"', desc: tx('получает число записей списка', 'receives the list item count') },
  { attr: 'data-vpc-limit="N"', desc: tx('на контейнере списка: старшие элементы сверх N получают data-extra, контейнер — data-collapsed', 'on a list container: items beyond N get data-extra, the container gets data-collapsed') },
  { attr: 'data-vpc-expand="list"', desc: tx('кнопка раскрытия свёрнутого списка (переключает data-expanded)', 'expand button for a collapsed list (toggles data-expanded)') },
  { attr: 'data-vpc-form / data-vpc-input / data-vpc-action', desc: tx('форма: кнопка действия собирает значения инпутов формы; Enter = клик', 'form: the action button collects the form inputs; Enter = click') },
  { attr: 'data-vpc-for="fact|todo|reminder"', desc: tx('форма правки: кнопка edit-* элемента заполняет её, отправка уходит с id', 'edit form: an item edit-* button fills it, submit carries the id') },
  { attr: 'data-vpc-item-action="…"', desc: tx('кнопка действия над элементом списка (bridge знает id)', 'item action button (bridge knows the id)') },
  { attr: 'data-vpc-onchange="…"', desc: tx('инпут в элементе списка: change → действие с его значением', 'input inside a list item: change → action with its value') },
  { attr: 'data-vpc-setting="key"', desc: tx('инпут настройки: приложение ставит значение и принимает change (чекбокс — checked)', 'settings input: the app sets the value and accepts change (checkbox — checked)') },
  { attr: 'data-vpc-label="key"', desc: tx('v2: текст элемента заменяется локализованной подписью; без ключа остаётся исходный текст. Ставь на отдельный <span> — содержимое заменяется целиком', 'v2: element text is replaced by the localized label; without the key the original text stays. Put it on a dedicated <span> — the content is replaced entirely') },
  { attr: 'data-vpc-label-placeholder / -title / -aria="key"', desc: tx('v2: то же для атрибутов placeholder / title / aria-label', 'v2: same for placeholder / title / aria-label attributes') },
];

// Атрибуты <html>, которые выставляет приложение
export const HTML_ATTRS: { attr: string; desc: SkinDocText }[] = [
  { attr: 'data-vpc-active="chat|dossier|room"', desc: tx('активный экран — он должен быть виден', 'active screen — it must be visible') },
  { attr: 'data-vpc-contract="N"', desc: tx('версия контракта скина (из меты; нет меты — 1)', 'skin contract version (from meta; no meta — 1)') },
  { attr: 'data-theme="light|dark"', desc: tx('v2: тема приложения — поддержи обе через html[data-theme="light"]', 'v2: app theme — support both via html[data-theme="light"]') },
  { attr: 'lang="ru|en"', desc: tx('v2: язык интерфейса', 'v2: UI language') },
  { attr: 'data-vpc-time-of-day="morning|day|evening|night"', desc: tx('v2: время суток пользователя', "v2: user's time of day") },
  { attr: 'data-vpc-weather="clear|cloudy|fog|rain|snow|storm"', desc: tx('v2: погода (нет атрибута — погода неизвестна)', 'v2: weather (no attribute — unknown)') },
];

/* ── События скин → хост ({vpc:'skin', type, …}) ── */
export const SKIN_EVENTS: { type: string; fields: string; desc: SkinDocText }[] = [
  { type: 'ready', fields: '', desc: tx('bridge загрузился — хост шлёт снапшот', 'bridge loaded — the host sends a snapshot') },
  { type: 'send', fields: 'text, image?, sid', desc: tx('отправка сообщения; image — data:image/… до ~8 МБ (bridge ужимает крупнее); частые отправки ждут в очереди (раз в 500 мс); хост отвечает {vpc:\'host\', type:\'send-result\', sid, ok} — при отказе bridge возвращает текст и картинку в поле ввода', 'send a message; image — data:image/… up to ~8 MB (the bridge downscales larger ones); rapid sends are queued (once per 500 ms); the host replies {vpc:\'host\', type:\'send-result\', sid, ok} — on refusal the bridge puts the text and image back into the input') },
  { type: 'clear', fields: '', desc: tx('очистить диалог', 'clear the dialog') },
  { type: 'select-persona', fields: 'id', desc: tx('переключить персону', 'switch persona') },
  { type: 'open-dossier', fields: '', desc: tx('открыть досье', 'open dossier') },
  { type: 'close-dossier', fields: '', desc: tx('закрыть досье', 'close dossier') },
  { type: 'action', fields: 'action, values, id', desc: tx('действие записи; values — строки, до 32 ключей', 'write action; values — strings, up to 32 keys') },
  { type: 'set-setting', fields: 'key, value', desc: tx('изменить настройку', 'change a setting') },
  { type: 'zoom-image', fields: 'src', desc: tx('показать картинку крупно (только data:/blob:)', 'show an image enlarged (data:/blob: only)') },
  { type: 'key', fields: 'key, code, ctrlKey, metaKey, altKey, shiftKey', desc: tx('хоткей при фокусе в скине: Escape и сочетания с Ctrl/Meta/Alt', 'hotkey while focus is in the skin: Escape and Ctrl/Meta/Alt combos') },
  { type: 'error', fields: 'message', desc: tx('ошибка JS скина — приложение отключит скин', 'skin JS error — the app disables the skin') },
];
// Каждое событие bridge несёт gen — метку документа (события прошлого документа хост отбрасывает)

/* ── JS API скина window.vpc (v2) ── */
export const VPC_API: { sig: string; desc: SkinDocText }[] = [
  { sig: 'vpc.contract', desc: tx('версия контракта приложения', 'app contract version') },
  { sig: 'vpc.skinContract', desc: tx('версия контракта, объявленная скином', 'contract version declared by the skin') },
  { sig: 'vpc.state', desc: tx('последний снапшот (null до первого)', 'last snapshot (null before the first one)') },
  { sig: 'vpc.on(event, fn(detail, state)) → unsubscribe()', desc: tx('подписка на событие', 'subscribe to an event') },
  { sig: 'vpc.send(text, image?)', desc: tx('отправить сообщение (image — data-URL)', 'send a message (image — data-URL)') },
  { sig: 'vpc.action(name, values, id?)', desc: tx('действие записи, как [data-vpc-action]', 'write action, like [data-vpc-action]') },
  { sig: 'vpc.setSetting(key, value)', desc: tx('изменить настройку, как [data-vpc-setting]', 'change a setting, like [data-vpc-setting]') },
  { sig: 'vpc.selectPersona(id)', desc: tx('переключить персону', 'switch persona') },
  { sig: 'vpc.openDossier() / vpc.closeDossier()', desc: tx('открыть / закрыть досье', 'open / close dossier') },
  { sig: 'vpc.zoom(src)', desc: tx('показать картинку крупно', 'show an image enlarged') },
];

export const VPC_API_EVENTS: { event: string; desc: SkinDocText }[] = [
  { event: 'state', desc: tx('каждый применённый снапшот (detail — снапшот)', 'every applied snapshot (detail — the snapshot)') },
  { event: 'message', desc: tx('каждое новое сообщение ленты (detail — сообщение); история (первый рендер, смена персоны, догрузка истории в пустую ленту) и замена id того же сообщения не считаются', 'every new feed message (detail — the message); history (first render, persona switch, history loaded into an empty feed) and an id change of the same message are not counted') },
  { event: 'typing', desc: tx('смена индикатора «печатает» (detail — boolean)', 'typing indicator change (detail — boolean)') },
  { event: 'mood', desc: tx('смена настроения (detail — строка)', 'mood change (detail — string)') },
  { event: 'theme', desc: tx("смена темы (detail — 'light' | 'dark')", "theme change (detail — 'light' | 'dark')") },
  { event: 'persona', desc: tx('смена персоны (detail — persona)', 'persona switch (detail — persona)') },
];

/* ── Правила песочницы ── */
export const SKIN_RULES: SkinDocText[] = [
  tx('Один файл = один экран: корневой блок [data-vpc-screen="chat|dossier|room"]; экран виден при html[data-vpc-active="…"].', 'One file = one screen: root block [data-vpc-screen="chat|dossier|room"]; the screen is visible under html[data-vpc-active="…"].'),
  tx('Атрибуты data-vpc* не удалять и не переименовывать; элементы с ними можно двигать, оборачивать и стилизовать.', 'Never remove or rename data-vpc* attributes; elements carrying them may be moved, wrapped and styled.'),
  tx('Сети нет (CSP): никаких http/https, CDN, <link>, <script src>, @import, url(http…). Картинки и шрифты — только data-URI, CSS или SVG.', 'No network (CSP): no http/https, CDN, <link>, <script src>, @import, url(http…). Images and fonts — data-URI, CSS or SVG only.'),
  tx('Блок между маркерами VPC-BRIDGE:START / VPC-BRIDGE:END приложение перезаписывает — не редактировать.', 'The block between VPC-BRIDGE:START / VPC-BRIDGE:END markers is overwritten by the app — do not edit it.'),
  tx('Скин работает в sandboxed iframe без same-origin: нет localStorage, cookies, доступа к окну приложения.', 'The skin runs in a sandboxed iframe without same-origin: no localStorage, cookies or access to the app window.'),
  tx('Документ скина не должен уходить со своей страницы: ссылки (кроме #якорей) не срабатывают, а переход через location/reload приложение считает поломкой и отключает скин.', 'The skin document must not leave its page: links (except #anchors) do nothing, and navigating via location/reload is treated as a failure — the app disables the skin.'),
  tx('Файл не больше 3 МБ. Необработанная ошибка JS скина отключает скин.', 'File size up to 3 MB. An unhandled skin JS error disables the skin.'),
  tx('Объяви версию контракта: <meta name="vpc-skin-contract" content="N">; скин новее приложения не загрузится. Без меты — v1.', 'Declare the contract version: <meta name="vpc-skin-contract" content="N">; a skin newer than the app is rejected. No meta — v1.'),
];

const SCREENS: SkinScreen[] = ['chat', 'dossier', 'room'];

// Markdown-описание контракта для нейросети-редактора скина
export function buildSkinContractDoc(locale: SkinDocLocale): string {
  const ru = locale === 'ru';
  const L = (t: SkinDocText) => t[locale];
  const out: string[] = [];
  const h = (s: string) => out.push('', s, '');
  const scr = (s: SkinScreen[]) => s.join(', ');

  out.push(
    ru
      ? `# Контракт скина VPC v${SKIN_CONTRACT_VERSION}`
      : `# VPC skin contract v${SKIN_CONTRACT_VERSION}`,
    '',
    ru
      ? 'Скин — самодостаточный HTML-файл одного экрана (чат, досье или комната) ИИ-персоны. Приложение подставляет живые данные в hook-точки (атрибуты data-vpc*) через встроенный bridge-скрипт и принимает от скина события. Всё, что не описано ниже, — свободная зона: любая разметка, стили, анимации, свой JS.'
      : 'A skin is a self-contained HTML file for one screen (chat, dossier or room) of an AI persona. The app fills hook points (data-vpc* attributes) with live data through an injected bridge script and receives events from the skin. Everything not described below is free: any markup, styles, animations, custom JS.',
  );

  h(ru ? '## Правила' : '## Rules');
  SKIN_RULES.forEach((r) => out.push('- ' + L(r)));

  h(ru ? '## Метаданные' : '## Metadata');
  out.push(
    '```html',
    `<meta name="vpc-skin-contract" content="${SKIN_CONTRACT_VERSION}">`,
    ru ? '<meta name="vpc-skin-name" content="Название скина">' : '<meta name="vpc-skin-name" content="Skin name">',
    '<meta name="vpc-skin-author" content="…">',
    '<meta name="vpc-skin-version" content="1.0">',
    '```',
  );

  h(ru ? '## Обязательные точки' : '## Required hook points');
  for (const s of SCREENS) {
    const req = REQUIRED_HOOKS[s];
    out.push(`- **${s}**: ` + (req.length
      ? req.map((r) => '`' + r.selector + '`' + (ru ? ` — ${r.label}` : '')).join('; ')
      : ru ? 'нет обязательных точек' : 'no required points'));
  }
  out.push(ru
    ? '- В <template data-vpc="message"> обязательно поле [data-vpc-field="text"].'
    : '- <template data-vpc="message"> must contain a [data-vpc-field="text"] field.');

  h(ru ? '## Скалярные слоты [data-vpc="…"] (текст подставляется внутрь)' : '## Scalar slots [data-vpc="…"] (text is set as content)');
  TEXT_SLOTS.forEach((s) => out.push(`- \`${s.name}\` (${scr(s.screens)}) — ${L(s.desc)}`));

  h(ru ? '## Контролы и особые точки [data-vpc="…"]' : '## Controls and special points [data-vpc="…"]');
  CONTROL_HOOKS.forEach((s) => out.push(`- \`${s.name}\` (${scr(s.screens)}) — ${L(s.desc)}`));

  h(ru ? '## Списки: контейнер [data-vpc="box"] + <template data-vpc="tpl">' : '## Lists: container [data-vpc="box"] + <template data-vpc="tpl">');
  LIST_HOOKS.forEach((l) => {
    const parts = [
      (ru ? 'поля: ' : 'fields: ') + l.fields.join(', '),
      l.root ? (ru ? 'корень клона: ' : 'clone root: ') + l.root : '',
      l.itemActions ? (ru ? 'действия: ' : 'actions: ') + l.itemActions.join(', ') : '',
      l.count ? (ru ? 'счётчик: ' : 'count: ') + l.count : '',
    ].filter(Boolean);
    out.push(`- \`${l.box}\` + \`${l.tpl}\` (${scr(l.screens)}) — ${parts.join('; ')}`);
  });

  h(ru ? '## Формы и настройки' : '## Forms and settings');
  FORM_ACTIONS.forEach((f) =>
    out.push(`- [data-vpc-action="${f.action}"] (${scr(f.screens)})` +
      (f.inputs.length ? (ru ? ' — инпуты: ' : ' — inputs: ') + f.inputs.join(', ') : '')));
  out.push((ru ? '- [data-vpc-setting]: ' : '- [data-vpc-setting]: ') + SETTING_KEYS.join(', '));

  h(ru ? '## Атрибуты разметки' : '## Markup attributes');
  MARKUP_ATTRS.forEach((a) => out.push(`- \`${a.attr}\` — ${L(a.desc)}`));

  h(ru ? '## Атрибуты <html>, которые ставит приложение' : '## <html> attributes set by the app');
  HTML_ATTRS.forEach((a) => out.push(`- \`${a.attr}\` — ${L(a.desc)}`));

  h(ru ? '## Подписи (v2): ключи для data-vpc-label*' : '## Labels (v2): keys for data-vpc-label*');
  out.push(Object.keys(SKIN_LABELS).map((k) => '`' + k + '`').join(', '));

  h(ru ? '## Снапшот данных (хост → скин)' : '## Data snapshot (host → skin)');
  out.push(ru
    ? 'Приходит сообщением {vpc:\'host\', type:\'state\', payload}; bridge применяет его сам. Для своего JS: событие document \'vpc:state\' (e.detail — снапшот) или window.vpc. Все поля необязательны, кроме persona.'
    : 'Arrives as {vpc:\'host\', type:\'state\', payload}; the bridge applies it itself. For custom JS: document event \'vpc:state\' (e.detail — the snapshot) or window.vpc. All fields are optional except persona.');
  FIELD_GROUPS.forEach(([group, fields]) => {
    out.push('', `**${group}**`);
    Object.entries(fields).forEach(([k, d]) => out.push(`- \`${k}\` — ${L(d)}`));
  });

  h(ru ? '## События скин → хост' : '## Skin → host events');
  out.push(ru
    ? 'Обычно их шлёт bridge по кликам на hook-точках; вручную — через window.vpc.'
    : 'Normally the bridge sends them on hook-point clicks; manually — via window.vpc.');
  SKIN_EVENTS.forEach((e) => out.push(`- \`${e.type}\`${e.fields ? ' {' + e.fields + '}' : ''} — ${L(e.desc)}`));

  h(ru ? '## JS API: window.vpc (v2)' : '## JS API: window.vpc (v2)');
  out.push(ru
    ? 'Bridge стоит в конце <body>: в своём скрипте обращайся к window.vpc в обработчике DOMContentLoaded или события document \'vpc:ready\'. Первый снапшот тоже вызывает события state/persona/typing/mood/theme.'
    : 'The bridge sits at the end of <body>: access window.vpc inside a DOMContentLoaded or document \'vpc:ready\' handler. The first snapshot also fires state/persona/typing/mood/theme.');
  VPC_API.forEach((a) => out.push(`- \`${a.sig}\` — ${L(a.desc)}`));
  out.push('', ru ? 'События vpc.on:' : 'vpc.on events:');
  VPC_API_EVENTS.forEach((e) => out.push(`- \`${e.event}\` — ${L(e.desc)}`));
  out.push(
    '',
    '```js',
    "document.addEventListener('vpc:ready', function () {",
    "  vpc.on('message', function (m) { if (m.role === 'persona') { /* " + (ru ? 'звук, вспышка…' : 'sound, flash…') + ' */ } });',
    "  vpc.on('mood', function (mood) { document.body.dataset.mood = mood || ''; });",
    '});',
    '```',
  );

  return out.join('\n') + '\n';
}

// Dev-проверка: описанные здесь точки действительно есть в bridge
if (import.meta.env.DEV) {
  const names = [
    ...TEXT_SLOTS.map((s) => s.name),
    ...CONTROL_HOOKS.map((s) => s.name),
    ...LIST_HOOKS.flatMap((l) => [l.box, l.tpl]),
    ...SKIN_EVENTS.map((e) => e.type),
  ];
  // Обязательные точки (напр. room-scene) проверяет валидатор, а не bridge
  const haystack = BRIDGE_SOURCE + SCREENS.flatMap((s) => REQUIRED[s].map(([sel]) => sel)).join(' ');
  const missing = names.filter((n) => !haystack.includes("'" + n + "'") && !haystack.includes('"' + n + '"'));
  if (missing.length) console.warn('[skin contract] описаны, но не найдены в bridge:', missing);
}
