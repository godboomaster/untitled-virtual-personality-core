import { Capacitor } from '@capacitor/core';
import wasmUrl from 'onnxruntime-web/ort-wasm-simd-threaded.wasm?url';
import { isNativeApp } from '../api';
import { OfflineLlm } from './plugin';
import type { EmbedReply, EmbedRequest } from './embedWorker';
import type { RecallDoc } from './recall';

/* Поиск по смыслу без ноутбука: вектор вопроса считает модель ядра на
   телефоне (embedCore.ts в воркере embedWorker.ts), векторы реплик и
   фактов приходят с ноутбука готовыми (memory.ts). Модель скачивается
   кнопкой в «Без ноутбука» (≈135 МБ); нет её или не загрузилась — поиск
   только по словам, ответ от этого не ждёт. Воркер закрывается после
   простоя и перед ответом Gemma — память модели уходит только вместе с ним
   (WASM не возвращает её системе). */

const MODEL_FILE = 'model_quantized.onnx';
const TOKENIZER_FILE = 'tokenizer.json';
const IDLE_MS = 5 * 60_000; // как у Gemma: модель занимает ≈450 МБ
const LOAD_TIMEOUT_MS = 60_000; // первая загрузка на телефоне — секунды
const EMBED_TIMEOUT_MS = 10_000;
const RETRY_MS = 5 * 60_000; // не загрузилась — не пробовать каждый вопрос
const DOC_CACHE = 400; // векторов реплик, посчитанных на телефоне

let worker: Worker | null = null;
let ready: Promise<void> | null = null;
let failedAt = 0;
let lastError = '';
let idleTimer = 0;
let seq = 0;
const pending = new Map<number, { resolve: (v: Float32Array[]) => void; reject: (e: Error) => void }>();
const docCache = new Map<string, Float32Array>();

const STOP_REASONS = new Set(['stopped', 'idle', 'released']);

function stop(reason = 'stopped') {
  window.clearTimeout(idleTimer);
  worker?.terminate();
  worker = null;
  ready = null;
  for (const p of pending.values()) p.reject(new Error(reason));
  pending.clear();
}

type Request = EmbedRequest extends infer R ? (R extends unknown ? Omit<R, 'id'> : never) : never;

function call(msg: Request, transfer: Transferable[], timeoutMs: number): Promise<Float32Array[]> {
  const w = worker;
  if (!w) return Promise.reject(new Error('no worker'));
  const id = ++seq;
  return new Promise<Float32Array[]>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      pending.delete(id);
      reject(new Error(`timeout ${timeoutMs / 1000} s`));
    }, timeoutMs);
    pending.set(id, {
      resolve: (v) => {
        window.clearTimeout(timer);
        resolve(v);
      },
      reject: (e) => {
        window.clearTimeout(timer);
        reject(e);
      },
    });
    w.postMessage({ ...msg, id } as EmbedRequest, transfer);
  });
}

async function readFile(url: string): Promise<ArrayBuffer> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url.split('/').pop()}: HTTP ${res.status}`);
  return res.arrayBuffer();
}

async function load(): Promise<void> {
  const st = await OfflineLlm.embedStatus();
  if (!st.downloaded) throw new Error('not_downloaded');
  const file = (name: string) => Capacitor.convertFileSrc(`${st.dir}/${name}`);
  const [tokenizer, model, wasm] = await Promise.all([
    readFile(file(TOKENIZER_FILE)),
    readFile(file(MODEL_FILE)),
    readFile(wasmUrl),
  ]);
  const w = new Worker(new URL('./embedWorker.ts', import.meta.url), { type: 'module' });
  w.onmessage = (e: MessageEvent<EmbedReply>) => {
    const p = pending.get(e.data.id);
    if (!p) return;
    pending.delete(e.data.id);
    if (e.data.error !== undefined) p.reject(new Error(e.data.error));
    else p.resolve(e.data.vecs ?? []);
  };
  w.onerror = (e) => stop(e.message || 'worker error');
  worker = w;
  await call({ type: 'init', tokenizer, model, wasm }, [tokenizer, model, wasm], LOAD_TIMEOUT_MS);
}

/** Модель скачана и не сломалась недавно — можно считать векторы. */
async function usable(): Promise<boolean> {
  if (!isNativeApp() || Date.now() - failedAt < RETRY_MS) return false;
  if (ready) return true;
  try {
    return (await OfflineLlm.embedStatus()).downloaded;
  } catch {
    return false;
  }
}

/** Векторы текстов; модели нет или сбой — null (искать только по словам). */
export async function embedTexts(texts: string[]): Promise<Float32Array[] | null> {
  if (!texts.length || !(await usable())) return null;
  try {
    if (!ready) ready = load();
    await ready;
    window.clearTimeout(idleTimer);
    const vecs = await call({ type: 'embed', texts }, [], EMBED_TIMEOUT_MS + texts.length * 1000);
    idleTimer = window.setTimeout(() => stop('idle'), IDLE_MS);
    return vecs;
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    // Выгрузили посреди счёта (перед Gemma, простой) — не сбой модели
    if (STOP_REASONS.has(message)) return null;
    lastError = message;
    failedAt = Date.now();
    console.warn('[offline] модель векторов:', lastError);
    stop(lastError);
    return null;
  }
}

/** Выгрузить модель векторов (воркер закрывается — память WASM уходит
 * только с ним, ≈450 МБ): перед ответом Gemma на телефоне и после удаления
 * модели. Следующий вопрос загрузит её заново (секунды). */
export function releaseEmbedder(): void {
  stop('released');
}

/** Режим без ноутбука включился — загрузить модель заранее, чтобы первый
 * ответ её не ждал (если скачана). */
export function warmEmbedder(): void {
  void usable().then((ok) => {
    if (ok && !ready) void embedTexts(['…']);
  });
}

/** Последняя ошибка модели векторов (для «Проверить»); сбросить — заново. */
export function embedError(reset = false): string {
  const e = lastError;
  if (reset) {
    lastError = '';
    failedAt = 0;
  }
  return e;
}

/** Вектор вопроса для поиска по смыслу и векторы реплик, у которых их нет
 * (написаны без ноутбука, max — самых свежих). Смысл искать не в чем (у
 * памяти нет векторов с ноутбука) или модели нет — null. */
export async function queryVector(query: string, docs: RecallDoc[], max = 16): Promise<Float32Array | null> {
  if (!docs.some((d) => d.vec)) return null;
  const missing = docs.filter((d) => !d.vec);
  for (const d of missing) d.vec = docCache.get(d.text) ?? null;
  const todo = [...new Set(missing.filter((d) => !d.vec).map((d) => d.text))].slice(-max);
  const vecs = await embedTexts([query, ...todo]);
  if (!vecs) return null;
  todo.forEach((text, i) => {
    docCache.delete(text);
    docCache.set(text, vecs[i + 1]);
    if (docCache.size > DOC_CACHE) docCache.delete(docCache.keys().next().value!);
  });
  for (const d of missing) d.vec ??= docCache.get(d.text) ?? null;
  return vecs[0];
}
