/* Библиотека скинов и их назначения персонам.

   Скин — запись библиотеки: до трёх файлов экранов (чат, досье, комната),
   метаданные из <meta name="vpc-skin-*"> и перекраска (цвета CSS-переменных
   + сдвиг тона, см. recolor.ts). Персоне назначается скин из библиотеки;
   отсутствующий или сломанный экран показывается дефолтным UI.

   Где хранится:
   - бэкенд доступен — на сервере (data/skins/, app/api/skins_api.py), общая
     для всех браузеров; здесь — кеш в памяти, HTML грузится по скину лениво;
   - бэкенд недоступен (мок-режим) — в localStorage: индекс библиотеки +
     каждый уникальный файл под своим ключом (одинаковый HTML — один раз).
     Запись обёрнута в try/catch: переполнение квоты не роняет применение,
     а показывается в панели скинов. Когда бэкенд появляется, локальная
     библиотека переносится на сервер.
   Встроенные скины (presets/, сейчас их нет — PRESETS пуст) поставляются
   с приложением: не удаляются, правка цветов встроенного создаёт
   пользовательскую копию.

   Старый формат (vpc-skin:<персона> — файлы прямо под персоной, legacy-
   комбинированный файл дублировался на каждый экран) при первой загрузке
   превращается в записи библиотеки + назначения, после чего ключи удаляются.

   Флаги «экран сломан» (runtime-ошибка скина) относятся к файлу: хранятся
   по скину и экрану вместе с подписью файла и снимаются, когда файл экрана
   заменили. Это локальная диагностика браузера — только в localStorage. */

import { useCallback, useEffect, useReducer } from 'react';
import { api, ApiError } from '../api';
import type { ApiSkinMeta } from '../api';
import { useApiOnline } from '../apiData';
import { detectScreens } from './engine';
import type { SkinScreen } from './engine';
import { readSkinMeta } from './meta';
import { applyColorOverrides } from './recolor';

export const SKIN_SCREENS: SkinScreen[] = ['chat', 'dossier', 'room'];

export type PersonaSkins = Partial<Record<SkinScreen, string>>;
export type BrokenMap = Partial<Record<SkinScreen, string>>;

export interface SkinEntry {
  id: string;
  name: string;
  nameKey?: string; // i18n-ключ имени встроенного скина
  author?: string;
  version?: string;
  contract: number;
  // экран → подпись файла (sha256 на сервере, локальный хэш офлайн);
  // сам HTML — loadSkinFiles / useSkinFiles
  screens: Partial<Record<SkinScreen, string>>;
  sizes: Partial<Record<SkinScreen, number>>; // байт
  colors: Record<string, string>; // переопределения CSS-переменных
  hueShift?: number;
  builtin?: boolean;
  createdAt: number;
  updatedAt: number;
}

export type SkinStoreMode = 'loading' | 'server' | 'local';

// Ошибка операции: key — ключ i18n (skin.err*), detail — текст сервера
export class SkinStoreError extends Error {
  key: string;
  detail: string;
  constructor(key: string, detail = '') {
    super(detail || key);
    this.key = key;
    this.detail = detail;
  }
}

export interface SkinInput {
  name: string;
  author?: string;
  version?: string;
  contract?: number;
  screens: PersonaSkins;
  colors?: Record<string, string>;
  hueShift?: number;
}

export interface SkinPatch {
  name?: string;
  screens?: Partial<Record<SkinScreen, string | null>>; // null — убрать экран
  colors?: Record<string, string>;
  hueShift?: number;
}

const LEGACY_SKIN_PREFIX = 'vpc-skin:';
const LEGACY_BROKEN_PREFIX = 'vpc-skin-broken:';
const LIBRARY_KEY = 'vpc-skin-library';
const FILE_PREFIX = 'vpc-skin-file:';
// Скрытые («удалённые») встроенные скины в офлайн-режиме
const HIDDEN_KEY = 'vpc-skin-hidden-builtins';
const BROKEN_KEY = 'vpc-skin-broken-v2';
const RETRY_SERVER_MS = 10000;

// ── Встроенные скины ──

function builtin(id: string, nameKey: string, name: string, html: string): SkinEntry {
  const file = 'builtin:' + id;
  const meta = readSkinMeta(html);
  const screens: SkinEntry['screens'] = {};
  const sizes: SkinEntry['sizes'] = {};
  for (const s of detectScreens(html)) {
    screens[s] = file;
    sizes[s] = sizeOf(html);
  }
  return {
    id: 'builtin-' + id,
    name: meta.name ?? name,
    nameKey,
    author: meta.author,
    version: meta.version,
    contract: meta.contract,
    screens,
    sizes,
    colors: {},
    builtin: true,
    createdAt: 0,
    updatedAt: 0,
  };
}

// [id, i18n-ключ имени, имя, HTML] — HTML через import './presets/<id>.html?raw'
const PRESETS: [string, string, string, string][] = [];
const PRESET_FILES: Record<string, string> = Object.fromEntries(
  PRESETS.map(([id, , , html]) => ['builtin:' + id, html]),
);
const BUILTINS: SkinEntry[] = PRESETS.map(([id, nameKey, name, html]) => builtin(id, nameKey, name, html));

// ── Состояние ──

let mode: SkinStoreMode = 'loading';
let userEntries: SkinEntry[] = [];
let assignments: Record<string, string> = {};
// Встроенные скины, удалённые пользователем из библиотеки
let hiddenBuiltins: string[] = [];
let lastError: SkinStoreError | null = null;
// Загруженные файлы: подпись → HTML (одинаковый файл в памяти один раз)
const files = new Map<string, string>(Object.entries(PRESET_FILES));
// Какие файлы уже лежат в localStorage (офлайн-режим)
const localFiles = new Set<string>();
const loading = new Map<string, Promise<void>>();
const loadFailedAt = new Map<string, number>();

let version = 0;
const listeners = new Set<() => void>();
// Счётчик записей на сервер, уже применённых к кешу: список, запрошенный
// до такой записи, а полученный после, её не содержит — перечитываем
let serverWrites = 0;

function emit() {
  version++;
  listeners.forEach((l) => l());
}

function setError(err: unknown): SkinStoreError {
  const e =
    err instanceof SkinStoreError
      ? err
      : err instanceof ApiError
        ? new SkinStoreError('skin.errServer', err.message)
        : new SkinStoreError('skin.errServer', err instanceof Error ? err.message : String(err));
  lastError = e;
  emit();
  return e;
}

export function dismissSkinError() {
  lastError = null;
  emit();
}

// ── localStorage (все обращения — в try/catch) ──

function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function lsSet(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    throw new SkinStoreError('skin.errQuota');
  }
}

function lsRemove(key: string) {
  try {
    localStorage.removeItem(key);
  } catch {
    /* нет доступа к storage — ключ останется */
  }
}

function lsKeys(prefix: string): string[] {
  const out: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(prefix)) out.push(k);
    }
  } catch {
    /* storage недоступен */
  }
  return out;
}

// Подпись файла для офлайн-режима (cyrb53 + длина): ключ дедупликации
function fileSig(str: string): string {
  let h1 = 0xdeadbeef ^ str.length;
  let h2 = 0x41c6ce57 ^ str.length;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return 'l' + (h2 >>> 0).toString(16).padStart(8, '0') + (h1 >>> 0).toString(16).padStart(8, '0') + str.length.toString(16);
}

// Положить файл в кеш под локальной подписью (при коллизии — с суффиксом)
function putLocalFile(html: string): string {
  let sig = fileSig(html);
  while (files.has(sig) && files.get(sig) !== html) sig += '_';
  files.set(sig, html);
  return sig;
}

function readLocalLibrary(): { entries: SkinEntry[]; assignments: Record<string, string> } {
  try {
    const raw = lsGet(LIBRARY_KEY);
    if (!raw) return { entries: [], assignments: {} };
    const data = JSON.parse(raw) as { entries?: SkinEntry[]; assignments?: Record<string, string> };
    return { entries: data.entries ?? [], assignments: data.assignments ?? {} };
  } catch {
    return { entries: [], assignments: {} };
  }
}

function localFile(sig: string): string | undefined {
  const html = lsGet(FILE_PREFIX + sig);
  if (html == null) return undefined;
  files.set(sig, html);
  localFiles.add(sig);
  return html;
}

// Записать офлайн-библиотеку: сначала новые файлы, затем индекс; при сбое
// записанные в этот раз файлы убираются, исключение — SkinStoreError(errQuota)
function persistLocal(entries: SkinEntry[], assigned: Record<string, string>) {
  const needed = new Set<string>();
  for (const e of entries) for (const sig of Object.values(e.screens)) if (sig) needed.add(sig);
  const written: string[] = [];
  try {
    for (const sig of needed) {
      if (localFiles.has(sig)) continue;
      const html = files.get(sig);
      if (html == null) continue;
      lsSet(FILE_PREFIX + sig, html);
      localFiles.add(sig);
      written.push(sig);
    }
    lsSet(LIBRARY_KEY, JSON.stringify({ entries, assignments: assigned }));
  } catch (e) {
    for (const sig of written) {
      lsRemove(FILE_PREFIX + sig);
      localFiles.delete(sig);
    }
    throw e;
  }
  // Файлы, на которые больше никто не ссылается
  for (const key of lsKeys(FILE_PREFIX)) {
    const sig = key.slice(FILE_PREFIX.length);
    if (!needed.has(sig)) {
      lsRemove(key);
      localFiles.delete(sig);
    }
  }
}

// Офлайн-изменение: новое состояние применяется, только если записалось
function commitLocal(entries: SkinEntry[], assigned: Record<string, string>) {
  persistLocal(entries, assigned);
  userEntries = entries;
  assignments = assigned;
  emit();
}

function clearLocalLibrary() {
  lsRemove(LIBRARY_KEY);
  for (const key of lsKeys(FILE_PREFIX)) lsRemove(key);
  localFiles.clear();
}

// ── Флаги «экран сломан» ──

type BrokenFlags = Record<string, Partial<Record<SkinScreen, { msg: string; file: string }>>>;

let brokenFlags: BrokenFlags = (() => {
  try {
    return JSON.parse(lsGet(BROKEN_KEY) ?? '{}') as BrokenFlags;
  } catch {
    return {};
  }
})();

function saveBrokenFlags() {
  try {
    lsSet(BROKEN_KEY, JSON.stringify(brokenFlags));
  } catch {
    /* квота — флаг поживёт до перезагрузки */
  }
}

export function markBroken(skinId: string, screen: SkinScreen, message: string) {
  const entry = findEntry(skinId);
  const file = entry?.screens[screen];
  if (!file) return;
  if (brokenFlags[skinId]?.[screen]?.msg === message && brokenFlags[skinId]?.[screen]?.file === file) return;
  brokenFlags = { ...brokenFlags, [skinId]: { ...brokenFlags[skinId], [screen]: { msg: message, file } } };
  saveBrokenFlags();
  emit();
}

// Сломанные экраны скина: флаг действует, пока файл экрана тот же
export function skinBroken(entry: SkinEntry | null | undefined): BrokenMap {
  const out: BrokenMap = {};
  if (!entry) return out;
  const flags = brokenFlags[entry.id] ?? {};
  for (const s of SKIN_SCREENS) {
    const f = flags[s];
    if (f && f.file === entry.screens[s]) out[s] = f.msg;
  }
  return out;
}

function dropBrokenFlags(skinId: string) {
  if (!(skinId in brokenFlags)) return;
  const rest = { ...brokenFlags };
  delete rest[skinId];
  brokenFlags = rest;
  saveBrokenFlags();
}

// ── Преобразования ──

function fromApi(m: ApiSkinMeta): SkinEntry {
  return {
    id: m.id,
    name: m.name,
    author: m.author ?? undefined,
    version: m.version ?? undefined,
    contract: m.contract || 1,
    screens: m.screens,
    sizes: m.sizes,
    colors: m.colors ?? {},
    hueShift: m.hue_shift || 0,
    createdAt: m.created_at,
    updatedAt: m.updated_at,
  };
}

// Файлы экранов → список уникальных файлов + ссылки экранов на него
function packFiles(screens: Partial<Record<SkinScreen, string | null>>) {
  const list: string[] = [];
  const refs: Partial<Record<SkinScreen, number | null>> = {};
  for (const s of SKIN_SCREENS) {
    const html = screens[s];
    if (html === undefined) continue;
    if (html === null) {
      refs[s] = null;
      continue;
    }
    let i = list.indexOf(html);
    if (i === -1) i = list.push(html) - 1;
    refs[s] = i;
  }
  return { list, refs };
}

function sizeOf(html: string): number {
  return new Blob([html]).size;
}

// Метаданные нового скина из его файлов (первый файл с <meta> побеждает)
function metaFromFiles(screens: PersonaSkins) {
  for (const s of SKIN_SCREENS) {
    const html = screens[s];
    if (html) {
      const m = readSkinMeta(html);
      if (m.name || m.author || m.version || m.contract > 1) return m;
    }
  }
  return { name: undefined, author: undefined, version: undefined, contract: 1 };
}

// ── Миграция старого формата (файлы прямо под персоной) ──

interface LegacySkin {
  personaId: string;
  screens: PersonaSkins;
}

function readLegacy(): LegacySkin[] {
  const out: LegacySkin[] = [];
  for (const key of lsKeys(LEGACY_SKIN_PREFIX)) {
    const raw = lsGet(key);
    if (!raw) continue;
    const personaId = key.slice(LEGACY_SKIN_PREFIX.length);
    let screens: PersonaSkins = {};
    if (!raw.startsWith('{')) {
      for (const s of detectScreens(raw)) screens[s] = raw;
    } else {
      try {
        screens = JSON.parse(raw) as PersonaSkins;
      } catch {
        screens = {};
      }
    }
    out.push({ personaId, screens });
  }
  return out;
}

function dropLegacy(personaId: string) {
  lsRemove(LEGACY_SKIN_PREFIX + personaId);
  lsRemove(LEGACY_BROKEN_PREFIX + personaId);
}

// Одинаковые наборы файлов у разных персон → одна запись библиотеки
function legacyGroups(legacy: LegacySkin[]) {
  const groups = new Map<string, { screens: PersonaSkins; personas: string[] }>();
  for (const l of legacy) {
    if (!SKIN_SCREENS.some((s) => l.screens[s])) {
      dropLegacy(l.personaId);
      continue;
    }
    const key = SKIN_SCREENS.map((s) => (l.screens[s] ? fileSig(l.screens[s]!) : '-')).join('|');
    const g = groups.get(key);
    if (g) g.personas.push(l.personaId);
    else groups.set(key, { screens: l.screens, personas: [l.personaId] });
  }
  return [...groups.values()];
}

function legacyName(screens: PersonaSkins, personaId: string): string {
  return metaFromFiles(screens).name ?? `Skin · ${personaId}`;
}

// Офлайн: старые скины → локальная библиотека
function migrateLegacyLocal() {
  const groups = legacyGroups(readLegacy());
  if (!groups.length) return;
  let entries = userEntries;
  let assigned = assignments;
  for (const g of groups) {
    const entry = buildLocalEntry({ name: legacyName(g.screens, g.personas[0]), screens: g.screens });
    entries = [...entries, entry];
    assigned = { ...assigned };
    for (const p of g.personas) if (!(p in assigned)) assigned[p] = entry.id;
  }
  try {
    commitLocal(entries, assigned);
    groups.forEach((g) => g.personas.forEach(dropLegacy));
  } catch (e) {
    setError(e);
  }
}

// Онлайн: локальная библиотека и старые скины → сервер. Назначения, уже
// заданные на сервере, не перетираются; 404 персоны — её уже нет.
// Не перенесённое (сбой сети/сервера) остаётся локально до следующей загрузки.
async function migrateToServer() {
  const local = readLocalLibrary();
  const groups = legacyGroups(readLegacy());
  // Назначения без записей — тоже работа: это не перенесённые в прошлый раз
  // назначения уже загруженных (серверных) или встроенных скинов
  if (!local.entries.length && !groups.length && !Object.keys(local.assignments).length) return;
  let failed = '';

  const assignIfFree = async (personaId: string, skinId: string) => {
    if (personaId in assignments) return true;
    try {
      await api.setPersonaSkin(personaId, skinId);
      assignments = { ...assignments, [personaId]: skinId };
      serverWrites++;
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return true;
      failed = e instanceof Error ? e.message : String(e);
      return false;
    }
  };

  // Что не удалось перенести: записи (сбой загрузки) и назначения (сбой
  // назначения уже загруженного скина — ссылаются на серверный id)
  const remaining: SkinEntry[] = [];
  const pending: Record<string, string> = {};
  const localIds = new Set(local.entries.map((e) => e.id));

  for (const e of local.entries) {
    const screens: PersonaSkins = {};
    for (const s of SKIN_SCREENS) {
      const sig = e.screens[s];
      const html = sig ? (files.get(sig) ?? localFile(sig)) : undefined;
      if (html) screens[s] = html;
    }
    if (!Object.keys(screens).length) continue;
    let created: SkinEntry;
    try {
      created = await serverCreate({
        name: e.name,
        author: e.author,
        version: e.version,
        contract: e.contract,
        screens,
        colors: e.colors,
        hueShift: e.hueShift,
      });
    } catch (err) {
      failed = err instanceof Error ? err.message : String(err);
      remaining.push(e);
      for (const [p, sid] of Object.entries(local.assignments)) if (sid === e.id) pending[p] = sid;
      continue;
    }
    for (const [p, sid] of Object.entries(local.assignments)) {
      if (sid === e.id && !(await assignIfFree(p, created.id))) pending[p] = created.id;
    }
  }
  // Назначения встроенных (и уже загруженных ранее) скинов — как есть
  for (const [p, sid] of Object.entries(local.assignments)) {
    if (!localIds.has(sid) && !(await assignIfFree(p, sid))) pending[p] = sid;
  }

  for (const g of groups) {
    let created: SkinEntry;
    try {
      created = await serverCreate({ name: legacyName(g.screens, g.personas[0]), screens: g.screens });
    } catch (err) {
      failed = err instanceof Error ? err.message : String(err);
      continue;
    }
    g.personas.forEach(dropLegacy);
    for (const p of g.personas) if (!(await assignIfFree(p, created.id))) pending[p] = created.id;
  }

  if (!remaining.length && !Object.keys(pending).length) {
    clearLocalLibrary();
  } else {
    try {
      persistLocal(remaining, pending);
    } catch {
      /* квота — останется прежняя локальная библиотека */
    }
  }
  emit();
  if (failed) setError(new SkinStoreError('skin.errMigrate', failed));
}

// ── Загрузка библиотеки ──

let initStarted = false;
let serverInflight: Promise<void> | null = null;
let lastServerAttempt = 0;

function readLocalHidden(): string[] {
  try {
    const data = JSON.parse(lsGet(HIDDEN_KEY) ?? '[]');
    return Array.isArray(data) ? data.filter((id): id is string => typeof id === 'string') : [];
  } catch {
    return [];
  }
}

function loadLocal() {
  const local = readLocalLibrary();
  userEntries = local.entries;
  assignments = local.assignments;
  hiddenBuiltins = readLocalHidden();
  for (const key of lsKeys(FILE_PREFIX)) localFiles.add(key.slice(FILE_PREFIX.length));
  mode = 'local';
  migrateLegacyLocal();
  emit();
}

function connectServer(): Promise<void> {
  if (serverInflight) return serverInflight;
  lastServerAttempt = Date.now();
  serverInflight = (async () => {
    let list: ApiSkinMeta[];
    let assigned: Record<string, string>;
    let hidden: string[];
    try {
      // Пока шёл запрос, своя запись (создание/назначение) могла лечь в кеш —
      // ответ тогда старше кеша и затёр бы её: запрашиваем заново
      for (let attempt = 0; ; attempt++) {
        const writes = serverWrites;
        const [s, a] = await Promise.all([api.getSkins(), api.getSkinAssignments()]);
        list = s.skins;
        hidden = s.hidden_builtins ?? [];
        assigned = a.assignments;
        if (serverWrites === writes || attempt >= 2) break;
      }
    } catch {
      // бэкенд недоступен (или без библиотеки скинов) — офлайн-режим;
      // сбой перечитывания уже загруженной с сервера библиотеки — оставляем кеш
      if (mode === 'loading') loadLocal();
      return;
    }
    mode = 'server';
    userEntries = list.map(fromApi);
    assignments = assigned;
    // Скрытые офлайн встроенные скины — перенести на сервер
    const localHidden = readLocalHidden().filter((id) => !hidden.includes(id));
    for (const id of localHidden) {
      try {
        await api.deleteSkin(id);
        hidden = [...hidden, id];
      } catch {
        /* не вышло — останется в браузере до следующей попытки */
      }
    }
    if (localHidden.every((id) => hidden.includes(id))) lsRemove(HIDDEN_KEY);
    hiddenBuiltins = hidden;
    emit();
    await migrateToServer();
  })().finally(() => {
    serverInflight = null;
  });
  return serverInflight;
}

// Первая загрузка; бэкенд поднялся после офлайн-старта — переключиться на сервер
function ensureInit(online: boolean) {
  if (!initStarted) {
    initStarted = true;
    void connectServer();
    return;
  }
  if (online && mode === 'local' && Date.now() - lastServerAttempt > RETRY_SERVER_MS) void connectServer();
}

// Операции ждут окончания первой загрузки: иначе запись ушла бы не туда
function ready(): Promise<void> {
  if (!initStarted) ensureInit(false);
  return mode === 'loading' && serverInflight ? serverInflight : Promise.resolve();
}

// Перечитать библиотеку с сервера (изменения из другого браузера)
export function refetchSkins(): Promise<void> {
  if (mode === 'local') return Promise.resolve();
  return connectServer();
}

// ── Доступ к записям и файлам ──

function findBuiltin(id: string): SkinEntry | undefined {
  return BUILTINS.find((b) => b.id === id);
}

export function findEntry(id: string | null | undefined): SkinEntry | undefined {
  if (!id) return undefined;
  return findBuiltin(id) ?? userEntries.find((e) => e.id === id);
}

export function listSkins(): SkinEntry[] {
  return [...BUILTINS.filter((b) => !hiddenBuiltins.includes(b.id)), ...userEntries];
}

function fileOf(sig: string | undefined): string | undefined {
  if (!sig) return undefined;
  return files.get(sig) ?? (mode === 'local' ? localFile(sig) : undefined);
}

// Исходные файлы экранов скина, если уже загружены (без перекраски)
export function skinFilesNow(entry: SkinEntry): PersonaSkins {
  const out: PersonaSkins = {};
  for (const s of SKIN_SCREENS) {
    const html = fileOf(entry.screens[s]);
    if (html) out[s] = html;
  }
  return out;
}

function filesLoaded(entry: SkinEntry): boolean {
  return SKIN_SCREENS.every((s) => !entry.screens[s] || fileOf(entry.screens[s]) != null);
}

// Догрузить файлы скина с сервера (один запрос на скин; дедуп параллельных)
export function loadSkinFiles(id: string): Promise<PersonaSkins> {
  const entry = findEntry(id);
  if (!entry) return Promise.resolve({});
  if (filesLoaded(entry) || mode !== 'server') return Promise.resolve(skinFilesNow(entry));
  let p = loading.get(id);
  if (!p) {
    p = api
      .getSkin(id)
      .then((r) => {
        for (const [sig, html] of Object.entries(r.files)) files.set(sig, html);
        loadFailedAt.delete(id);
        emit();
      })
      .catch((e) => {
        loadFailedAt.set(id, Date.now());
        // Хуки видят сбой (loading снимается), а по истечении паузы —
        // повторная перерисовка запустит новую попытку (useSkinFiles)
        emit();
        setTimeout(emit, RETRY_SERVER_MS + 50);
        // скин удалили в другом браузере — перечитать библиотеку
        if (e instanceof ApiError && e.status === 404) void refetchSkins();
      })
      .finally(() => {
        loading.delete(id);
      });
    loading.set(id, p);
  }
  return p.then(() => skinFilesNow(findEntry(id) ?? entry));
}

// Файл с перекраской скина; результат кешируется (перекраска 3 МБ файла
// на каждый рендер чата была бы заметна)
const effectiveCache = new Map<string, string>();

export function applyEntryColors(html: string, entry: Pick<SkinEntry, 'colors' | 'hueShift'>): string {
  if (!entry.hueShift && !Object.keys(entry.colors).length) return html;
  return applyColorOverrides(html, entry.colors, entry.hueShift ?? 0);
}

function effectiveFile(sig: string, html: string, entry: SkinEntry): string {
  if (!entry.hueShift && !Object.keys(entry.colors).length) return html;
  const key = sig + '|' + (entry.hueShift ?? 0) + '|' + JSON.stringify(entry.colors);
  let out = effectiveCache.get(key);
  if (out == null) {
    out = applyColorOverrides(html, entry.colors, entry.hueShift ?? 0);
    if (effectiveCache.size >= 12) effectiveCache.delete(effectiveCache.keys().next().value!);
    effectiveCache.set(key, out);
  }
  return out;
}

// ── Операции ──

function buildLocalEntry(input: SkinInput): SkinEntry {
  const screens: SkinEntry['screens'] = {};
  const sizes: SkinEntry['sizes'] = {};
  for (const s of SKIN_SCREENS) {
    const html = input.screens[s];
    if (!html) continue;
    screens[s] = putLocalFile(html);
    sizes[s] = sizeOf(html);
  }
  const meta = metaFromFiles(input.screens);
  const now = Date.now();
  return {
    id: 'loc_' + now.toString(36) + Math.random().toString(36).slice(2, 8),
    name: input.name,
    author: input.author ?? meta.author,
    version: input.version ?? meta.version,
    contract: input.contract ?? meta.contract,
    screens,
    sizes,
    colors: input.colors ?? {},
    hueShift: input.hueShift ?? 0,
    createdAt: now,
    updatedAt: now,
  };
}

async function serverCreate(input: SkinInput): Promise<SkinEntry> {
  const meta = metaFromFiles(input.screens);
  const { list, refs } = packFiles(input.screens);
  const r = await api.createSkin({
    name: input.name,
    author: input.author ?? meta.author ?? null,
    version: input.version ?? meta.version ?? null,
    contract: input.contract ?? meta.contract,
    files: list,
    screens: refs as Partial<Record<SkinScreen, number>>,
    colors: input.colors ?? {},
    hue_shift: input.hueShift ?? 0,
  });
  const entry = fromApi(r.skin);
  for (const s of SKIN_SCREENS) {
    const sig = entry.screens[s];
    const html = input.screens[s];
    if (sig && html) files.set(sig, html);
  }
  userEntries = [...userEntries, entry];
  serverWrites++;
  emit();
  return entry;
}

// Новый скин в библиотеке (метаданные, не заданные явно, — из <meta> файлов)
export async function createSkin(input: SkinInput): Promise<SkinEntry> {
  try {
    await ready();
    if (mode === 'server') return await serverCreate(input);
    const entry = buildLocalEntry(input);
    commitLocal([...userEntries, entry], assignments);
    return entry;
  } catch (e) {
    throw setError(e);
  }
}

// Правка скина. Встроенный скин не меняется: создаётся пользовательская
// копия с правкой, и персоны со встроенным переходят на копию.
export async function updateSkin(id: string, patch: SkinPatch): Promise<SkinEntry> {
  await ready();
  const entry = findEntry(id);
  if (!entry) throw setError(new SkinStoreError('skin.errNotFound'));
  try {
    if (entry.builtin) return await forkBuiltin(entry, patch);
    const replaced = SKIN_SCREENS.filter((s) => patch.screens?.[s] !== undefined);
    let next: SkinEntry;
    if (mode === 'server') {
      const { list, refs } = packFiles(patch.screens ?? {});
      const r = await api.updateSkin(id, {
        name: patch.name,
        colors: patch.colors,
        hue_shift: patch.hueShift,
        ...(replaced.length ? { files: list, screens: refs } : {}),
      });
      next = fromApi(r.skin);
      for (const s of replaced) {
        const sig = next.screens[s];
        const html = patch.screens?.[s];
        if (sig && html) files.set(sig, html);
      }
      userEntries = userEntries.map((e) => (e.id === id ? next : e));
      serverWrites++;
    } else {
      const screens = { ...entry.screens };
      const sizes = { ...entry.sizes };
      for (const s of replaced) {
        const html = patch.screens?.[s];
        if (html) {
          screens[s] = putLocalFile(html);
          sizes[s] = sizeOf(html);
        } else {
          delete screens[s];
          delete sizes[s];
        }
      }
      if (!SKIN_SCREENS.some((s) => screens[s])) throw new SkinStoreError('skin.errNoScreens');
      next = {
        ...entry,
        name: patch.name ?? entry.name,
        colors: patch.colors ?? entry.colors,
        hueShift: patch.hueShift ?? entry.hueShift,
        screens,
        sizes,
        updatedAt: Date.now(),
      };
      commitLocal(
        userEntries.map((e) => (e.id === id ? next : e)),
        assignments,
      );
    }
    // Файл экрана заменён — старый флаг «сломан» к нему не относится
    if (replaced.length && brokenFlags[id]) {
      const flags = { ...brokenFlags[id] };
      replaced.forEach((s) => delete flags[s]);
      brokenFlags = { ...brokenFlags, [id]: flags };
      saveBrokenFlags();
    }
    emit();
    return next;
  } catch (e) {
    throw setError(e);
  }
}

async function forkBuiltin(entry: SkinEntry, patch: SkinPatch): Promise<SkinEntry> {
  const screens = skinFilesNow(entry);
  for (const s of SKIN_SCREENS) {
    const html = patch.screens?.[s];
    if (html === null) delete screens[s];
    else if (html) screens[s] = html;
  }
  const input: SkinInput = {
    name: patch.name ?? entry.name,
    author: entry.author,
    version: entry.version,
    contract: entry.contract,
    screens,
    colors: patch.colors ?? entry.colors,
    hueShift: patch.hueShift ?? entry.hueShift,
  };
  const fork = mode === 'server' ? await serverCreate(input) : buildLocalEntry(input);
  const users = Object.keys(assignments).filter((p) => assignments[p] === entry.id);
  if (mode === 'server') {
    for (const p of users) await assignSkin(p, fork.id);
  } else {
    const assigned = { ...assignments };
    users.forEach((p) => (assigned[p] = fork.id));
    commitLocal([...userEntries, fork], assigned);
  }
  return fork;
}

// Удалить скин из библиотеки; снимается со всех персон. Встроенный скин
// скрывается (вернуть — restoreBuiltinSkins)
export async function deleteSkin(id: string): Promise<void> {
  await ready();
  const entry = findEntry(id);
  if (!entry) return;
  try {
    const assigned = Object.fromEntries(Object.entries(assignments).filter(([, s]) => s !== id));
    if (entry.builtin) {
      const hidden = hiddenBuiltins.includes(id) ? hiddenBuiltins : [...hiddenBuiltins, id];
      if (mode === 'server') {
        await api.deleteSkin(id);
        assignments = assigned;
        serverWrites++;
      } else {
        lsSet(HIDDEN_KEY, JSON.stringify(hidden));
        commitLocal(userEntries, assigned);
      }
      hiddenBuiltins = hidden;
      emit();
      dropBrokenFlags(id);
      return;
    }
    const entries = userEntries.filter((e) => e.id !== id);
    if (mode === 'server') {
      await api.deleteSkin(id);
      userEntries = entries;
      assignments = assigned;
      serverWrites++;
      emit();
    } else {
      commitLocal(entries, assigned);
    }
    dropBrokenFlags(id);
  } catch (e) {
    throw setError(e);
  }
}

// Вернуть в библиотеку все скрытые встроенные скины
export async function restoreBuiltinSkins(): Promise<void> {
  await ready();
  if (!hiddenBuiltins.length) return;
  try {
    if (mode === 'server') await api.restoreBuiltinSkins();
    else lsRemove(HIDDEN_KEY);
    hiddenBuiltins = [];
    emit();
  } catch (e) {
    throw setError(e);
  }
}

// Назначить персоне скин (null — дефолтный вид)
export async function assignSkin(personaId: string, skinId: string | null): Promise<void> {
  await ready();
  if (!personaId || (assignments[personaId] ?? null) === skinId) return;
  const assigned = { ...assignments };
  if (skinId) assigned[personaId] = skinId;
  else delete assigned[personaId];
  try {
    if (mode === 'server') {
      await api.setPersonaSkin(personaId, skinId);
      assignments = assigned;
      serverWrites++;
      emit();
    } else {
      commitLocal(userEntries, assigned);
    }
  } catch (e) {
    throw setError(e);
  }
}

// Файлы, загруженные прямо на персону (старый API): обновить её
// пользовательский скин или завести новый и назначить
export async function applySkinFiles(personaId: string, htmlFiles: string[]): Promise<void> {
  await ready();
  const screens: PersonaSkins = {};
  for (const file of htmlFiles) for (const s of detectScreens(file)) screens[s] = file;
  if (!Object.keys(screens).length) return;
  const cur = findEntry(assignments[personaId]);
  if (cur && !cur.builtin) {
    await updateSkin(cur.id, { screens });
    return;
  }
  const entry = await createSkin({ name: legacyName(screens, personaId), screens });
  await assignSkin(personaId, entry.id);
}

// Смена id персоны: бэкенд уже перенёс назначение — здесь кеш (и офлайн-
// библиотека), плюс не перенесённые ещё старые ключи
export function renameSkins(oldId: string, newId: string) {
  for (const prefix of [LEGACY_SKIN_PREFIX, LEGACY_BROKEN_PREFIX]) {
    const raw = lsGet(prefix + oldId);
    if (raw === null) continue;
    try {
      lsSet(prefix + newId, raw);
      lsRemove(prefix + oldId);
    } catch {
      /* квота — скин останется под старым id */
    }
  }
  if (!(oldId in assignments)) return;
  const assigned = { ...assignments, [newId]: assignments[oldId] };
  delete assigned[oldId];
  if (mode === 'local') {
    try {
      commitLocal(userEntries, assigned);
    } catch (e) {
      setError(e);
    }
    return;
  }
  assignments = assigned;
  emit();
}

// ── React-хуки ──

// Подписка на стор + запуск загрузки библиотеки
function useSkinStore(): number {
  const [, force] = useReducer((x: number) => x + 1, 0);
  const online = useApiOnline();
  useEffect(() => {
    listeners.add(force);
    return () => {
      listeners.delete(force);
    };
  }, []);
  useEffect(() => {
    ensureInit(online);
  }, [online]);
  return version;
}

export interface SkinLibraryState {
  entries: SkinEntry[]; // встроенные + пользовательские
  assignments: Record<string, string>; // персона → id скина
  hiddenBuiltins: number; // сколько встроенных скинов скрыто из библиотеки
  mode: SkinStoreMode;
  error: SkinStoreError | null;
}

export function useSkinLibrary(): SkinLibraryState {
  useSkinStore();
  // Скрытые, которых в приложении больше нет (убранный пресет), не в счёт:
  // восстанавливать нечего
  const hidden = hiddenBuiltins.filter((id) => findBuiltin(id)).length;
  return { entries: listSkins(), assignments, hiddenBuiltins: hidden, mode, error: lastError };
}

// Исходные файлы экранов скина (без перекраски); грузит их при необходимости
export function useSkinFiles(skinId: string | null): { files: PersonaSkins; loading: boolean } {
  useSkinStore();
  const entry = findEntry(skinId);
  const loaded = entry ? filesLoaded(entry) : true;
  const failedAt = skinId ? loadFailedAt.get(skinId) : undefined;
  useEffect(() => {
    if (entry && !loaded && !(failedAt && Date.now() - failedAt < RETRY_SERVER_MS)) void loadSkinFiles(entry.id);
  }, [entry, loaded, failedAt]);
  // Недавний сбой загрузки — не «грузится»: иначе индикатор висел бы до повтора
  const recentlyFailed = failedAt != null && Date.now() - failedAt < RETRY_SERVER_MS;
  return { files: entry ? skinFilesNow(entry) : {}, loading: !loaded && mode === 'server' && !recentlyFailed };
}

export interface PersonaSkinState {
  // Файлы скина по экранам — с перекраской (нет ключа — экран дефолтный)
  skins: PersonaSkins;
  // Тексты runtime-ошибок по экранам, из-за которых они отключены
  broken: BrokenMap;
  // Назначенный скин (null — дефолтный вид)
  skinId: string | null;
  entry: SkinEntry | null;
  apply: (files: string[]) => void;
  // Снять скин с персоны (вернуть дефолтный вид)
  reset: () => void;
  reportBroken: (screen: SkinScreen, message: string) => void;
}

export function usePersonaSkin(personaId: string): PersonaSkinState {
  // Подписка на стор — внутри useSkinFiles; назначения читаются в том же рендере
  const entry = findEntry(assignments[personaId]) ?? null;
  const { files: raw } = useSkinFiles(entry?.id ?? null);

  const skins: PersonaSkins = {};
  if (entry) {
    for (const s of SKIN_SCREENS) {
      const sig = entry.screens[s];
      const html = raw[s];
      if (sig && html) skins[s] = effectiveFile(sig, html, entry);
    }
  }
  const skinId = entry?.id ?? null;

  const apply = useCallback(
    (htmlFiles: string[]) => {
      applySkinFiles(personaId, htmlFiles).catch(() => {});
    },
    [personaId],
  );
  const reset = useCallback(() => {
    assignSkin(personaId, null).catch(() => {});
  }, [personaId]);
  const reportBroken = useCallback(
    (screen: SkinScreen, message: string) => {
      if (skinId) markBroken(skinId, screen, message);
    },
    [skinId],
  );

  return { skins, broken: skinBroken(entry), skinId, entry, apply, reset, reportBroken };
}
