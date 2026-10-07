/* VPC Link на телефоне: защищённый канал до ноутбука без VPN.

   Телефон знает ключ ноутбука (сопряжение по QR-коду) и говорит с ним по
   Noise (noise.ts). Пути: напрямую по локальной сети (ws://адрес:порт ноутбука)
   или через посредника (wss://…/v1/peer/<комната>), который видит только
   шифртекст. Внутри — обычные HTTP-запросы к API ядра: linkFetch отдаёт
   настоящий Response со стримом, так что остальной код веба не меняется.
   Протокол кадров — как в app/link/tunnel.py. */

import { generatePrivate, HandshakeState, NoiseError } from './noise.ts';
import type { Transport } from './noise.ts';

const PROLOGUE = new TextEncoder().encode('vpc-link/1');
const MODE_CONNECT = 0x01;
const MODE_PAIR = 0x02;
const REQ = 0x01, REQ_DATA = 0x02, REQ_END = 0x03;
const RES = 0x04, RES_DATA = 0x05, RES_END = 0x06;
const CANCEL = 0x07, ERROR = 0x08, PING = 0x09, PONG = 0x0a;
const PAIR_CONFIRM = 0x0b, PAIRED = 0x0c;

const CHUNK = 32 * 1024;
const LAN_TIMEOUT_MS = 2500; // ноутбук в этой же сети отвечает за доли секунды
const RELAY_DELAY_MS = 1200; // посреднику — фора локальной сети
const RELAY_TIMEOUT_MS = 12000;
const PING_EVERY_MS = 20000;
const PONG_TIMEOUT_MS = 10000;

const STORAGE_KEY = 'vpc-link';

/** Что телефон помнит о ноутбуке (localStorage; резервная копия Android выключена). */
export interface LinkConfig {
  v: 1;
  k: string; // открытый ключ ноутбука, base64url
  r: string; // комната у посредника
  n: string; // имя ноутбука
  u: string; // адрес посредника ('' — только локальная сеть)
  l: string[]; // адреса ноутбука в локальной сети, host:port
  sk: string; // свой закрытый ключ, base64url
  id?: string; // id устройства у ноутбука
}

/** Содержимое QR-кода: vpclink:<base64url(JSON)>. */
export interface PairingOffer {
  v: 1;
  k: string;
  r: string;
  o: string; // id предложения
  p: string; // одноразовый секрет, base64url
  n: string;
  u: string;
  l: string[];
}

export type LinkState = 'off' | 'connecting' | 'lan' | 'relay' | 'offline';

// ── base64url ──

export function b64u(bytes: Uint8Array): string {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function unb64u(text: string): Uint8Array {
  const s = atob(text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

const enc = new TextEncoder();
const dec = new TextDecoder();

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let pos = 0;
  for (const p of parts) {
    out.set(p, pos);
    pos += p.length;
  }
  return out;
}

// Байты от @noble — Uint8Array<ArrayBufferLike>; WebSocket.send в типах
// DOM хочет именно ArrayBuffer (не SharedArrayBuffer) — так и есть
function wsSend(ws: WebSocket, data: Uint8Array) {
  ws.send(data as Uint8Array<ArrayBuffer>);
}

function frame(kind: number, stream: number, body: Uint8Array = new Uint8Array(0)): Uint8Array {
  const out = new Uint8Array(5 + body.length);
  out[0] = kind;
  new DataView(out.buffer).setUint32(1, stream);
  out.set(body, 5);
  return out;
}

// ── настройки ──

let storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null = null;
function store() {
  if (storage) return storage;
  try {
    return localStorage;
  } catch {
    return null;
  }
}

/** Для тестов в Node: своё хранилище вместо localStorage. */
export function useStorage(s: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>) {
  storage = s;
}

export function getLinkConfig(): LinkConfig | null {
  try {
    const raw = store()?.getItem(STORAGE_KEY);
    const cfg = raw ? (JSON.parse(raw) as LinkConfig) : null;
    return cfg && cfg.v === 1 && cfg.k && cfg.sk ? cfg : null;
  } catch {
    return null;
  }
}

export function setLinkConfig(cfg: LinkConfig | null) {
  try {
    if (cfg) store()?.setItem(STORAGE_KEY, JSON.stringify(cfg));
    else store()?.removeItem(STORAGE_KEY);
  } catch {
    /* хранилище недоступно */
  }
  if (!cfg) dropSession();
}

export function parsePairingUri(text: string): PairingOffer | null {
  const m = /vpclink:([A-Za-z0-9_-]+)/.exec(text.trim());
  if (!m) return null;
  try {
    const o = JSON.parse(dec.decode(unb64u(m[1]))) as PairingOffer;
    if (o.v !== 1 || !o.k || !o.r || !o.o || !o.p) return null;
    if (unb64u(o.k).length !== 32 || unb64u(o.p).length !== 32) return null;
    return { ...o, u: o.u || '', l: Array.isArray(o.l) ? o.l.map(String) : [] };
  } catch {
    return null;
  }
}

// ── состояние для интерфейса ──

let state: LinkState = getLinkConfig() ? 'connecting' : 'off';
const listeners = new Set<() => void>();

function setState(next: LinkState) {
  if (next === state) return;
  state = next;
  listeners.forEach((l) => l());
}

export function linkState(): LinkState {
  return state;
}

export function onLinkState(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// ── соединение ──

interface Hello {
  name?: string;
  lan?: string[];
  port?: number;
}

interface Opened {
  ws: WebSocket;
  transport: Transport;
  hello: Hello;
  via: 'lan' | 'relay';
  pending: Uint8Array[]; // кадры, пришедшие сразу за рукопожатием
}

// Наследник TypeError: для вызывающего кода это обычный сетевой сбой fetch
class LinkClosed extends TypeError {
  code: number;
  constructor(code: number, reason: string) {
    super(reason || `closed ${code}`);
    this.code = code;
  }
}

function openOne(url: string, via: 'lan' | 'relay', timeoutMs: number, mode: number,
  makeHs: () => HandshakeState, payload: Uint8Array, cancelled: () => boolean): Promise<Opened> {
  return new Promise((resolve, reject) => {
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      reject(e);
      return;
    }
    ws.binaryType = 'arraybuffer';
    const hs = makeHs();
    let done = false;
    const pending: Uint8Array[] = [];
    let opened: Opened | null = null;
    const fail = (err: Error) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try {
        ws.close();
      } catch {
        /* уже закрыт */
      }
      reject(err);
    };
    const timer = setTimeout(() => fail(new Error(`${via}: timeout`)), timeoutMs);
    ws.onopen = () => {
      if (cancelled()) return fail(new Error('cancelled'));
      wsSend(ws, concat(Uint8Array.of(mode), hs.writeMessage(payload)));
    };
    ws.onmessage = (ev) => {
      const data = new Uint8Array(ev.data as ArrayBuffer);
      if (opened) {
        pending.push(data);
        return;
      }
      try {
        const hello = JSON.parse(dec.decode(hs.readMessage(data)) || '{}') as Hello;
        opened = { ws, transport: hs.transport(), hello, via, pending };
        done = true;
        clearTimeout(timer);
        if (cancelled()) {
          ws.close();
          reject(new Error('cancelled'));
          return;
        }
        resolve(opened);
      } catch (e) {
        fail(e instanceof Error ? e : new Error(String(e)));
      }
    };
    ws.onerror = () => fail(new Error(`${via}: connection error`));
    ws.onclose = (ev) => fail(new LinkClosed(ev.code, ev.reason));
  });
}

/** Подключение: локальные адреса сразу, посредник — с небольшой задержкой;
 *  выигрывает первое завершённое рукопожатие, остальные закрываются. */
async function connect(target: { k: string; u: string; r: string; l: string[] }, sk: Uint8Array, mode: number,
  payload: Uint8Array, psk?: Uint8Array): Promise<Opened> {
  const laptop = unb64u(target.k);
  const prologue = concat(PROLOGUE, Uint8Array.of(mode));
  const makeHs = () => new HandshakeState(mode === MODE_PAIR ? 'IKpsk2' : 'IK', true, prologue, { s: sk, rs: laptop, psk });
  let winner = false;
  const cancelled = () => winner;
  const attempts: Promise<Opened>[] = target.l.map((addr) =>
    openOne(`ws://${addr}/`, 'lan', LAN_TIMEOUT_MS, mode, makeHs, payload, cancelled));
  if (target.u) {
    const relayUrl = `${target.u.replace(/\/+$/, '')}/v1/peer/${target.r}`;
    attempts.push(new Promise<Opened>((resolve, reject) => {
      setTimeout(() => {
        if (winner) return reject(new Error('cancelled'));
        openOne(relayUrl, 'relay', RELAY_TIMEOUT_MS, mode, makeHs, payload, cancelled).then(resolve, reject);
      }, target.l.length ? RELAY_DELAY_MS : 0);
    }));
  }
  if (!attempts.length) throw new Error('нет ни адреса в сети, ни посредника');
  try {
    const opened = await Promise.any(attempts);
    winner = true;
    return opened;
  } catch (e) {
    // Отказ ноутбука (не сопряжён, QR устарел) важнее «не достучались»
    const errors = e instanceof AggregateError ? e.errors : [e];
    const refusal = errors.find((x) => x instanceof LinkClosed && x.code >= 4400 && x.code < 4500);
    throw refusal ?? errors[0] ?? e;
  }
}

interface StreamState {
  resolve: (r: Response) => void;
  reject: (e: unknown) => void;
  controller: ReadableStreamDefaultController<Uint8Array> | null;
  started: boolean;
}

class Session {
  private ws: WebSocket;
  private transport: Transport;
  readonly via: 'lan' | 'relay';
  private streams = new Map<number, StreamState>();
  private nextId = 1;
  private pingTimer: ReturnType<typeof setInterval>;
  private pongTimer: ReturnType<typeof setTimeout> | null = null;
  private waiters = new Map<number, (body: Uint8Array) => void>(); // служебные кадры потока 0
  dead = false;
  onDead: (() => void) | null = null;

  constructor(o: Opened) {
    this.ws = o.ws;
    this.transport = o.transport;
    this.via = o.via;
    this.ws.onmessage = (ev) => this.onMessage(new Uint8Array(ev.data as ArrayBuffer));
    this.ws.onclose = () => this.kill(new TypeError('VPC Link: соединение закрыто'));
    this.ws.onerror = () => this.kill(new TypeError('VPC Link: ошибка соединения'));
    for (const data of o.pending) this.onMessage(data);
    this.pingTimer = setInterval(() => this.ping(), PING_EVERY_MS);
  }

  private ping() {
    if (this.dead) return;
    this.send(PING, 0);
    this.pongTimer ??= setTimeout(() => {
      this.kill(new TypeError('VPC Link: ноутбук не отвечает'));
      try {
        this.ws.close();
      } catch {
        /* уже закрыт */
      }
    }, PONG_TIMEOUT_MS);
  }

  send(kind: number, stream: number, body?: Uint8Array) {
    if (this.dead) throw new TypeError('VPC Link: соединение закрыто');
    wsSend(this.ws, this.transport.encrypt(frame(kind, stream, body)));
  }

  waitFrame(kind: number, timeoutMs: number): Promise<Uint8Array> {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        this.waiters.delete(kind);
        reject(new Error('timeout'));
      }, timeoutMs);
      this.waiters.set(kind, (body) => {
        clearTimeout(t);
        resolve(body);
      });
    });
  }

  private onMessage(data: Uint8Array) {
    let body: Uint8Array;
    try {
      body = this.transport.decrypt(data);
    } catch (e) {
      this.kill(e instanceof NoiseError ? new TypeError(`VPC Link: ${e.message}`) : e);
      this.ws.close();
      return;
    }
    if (body.length < 5) return;
    const kind = body[0];
    const id = new DataView(body.buffer, body.byteOffset, body.byteLength).getUint32(1);
    const payload = body.subarray(5);
    if (id === 0) {
      if (kind === PONG) {
        if (this.pongTimer) clearTimeout(this.pongTimer);
        this.pongTimer = null;
      }
      this.waiters.get(kind)?.(payload);
      this.waiters.delete(kind);
      return;
    }
    const st = this.streams.get(id);
    if (!st) return;
    if (kind === RES) {
      const head = JSON.parse(dec.decode(payload)) as { s: number; h: Record<string, string> };
      const nullBody = [101, 204, 205, 304].includes(head.s);
      const stream = nullBody ? null : new ReadableStream<Uint8Array>({
        start: (c) => {
          st.controller = c;
        },
        cancel: () => {
          this.streams.delete(id);
          try {
            this.send(CANCEL, id);
          } catch {
            /* соединения уже нет */
          }
        },
      });
      st.started = true;
      st.resolve(new Response(stream, { status: head.s, headers: head.h }));
      if (nullBody) this.streams.delete(id);
    } else if (kind === RES_DATA) {
      st.controller?.enqueue(payload.slice());
    } else if (kind === RES_END) {
      this.streams.delete(id);
      st.controller?.close();
    } else if (kind === ERROR) {
      this.streams.delete(id);
      let msg = 'error';
      try {
        msg = (JSON.parse(dec.decode(payload)) as { e?: string }).e ?? msg;
      } catch {
        /* тело не JSON */
      }
      const err = new TypeError(`VPC Link: ${msg}`);
      if (st.started) st.controller?.error(err);
      else st.reject(err);
    }
  }

  kill(err: unknown) {
    if (this.dead) return;
    this.dead = true;
    clearInterval(this.pingTimer);
    if (this.pongTimer) clearTimeout(this.pongTimer);
    for (const st of this.streams.values()) {
      if (st.started) st.controller?.error(err);
      else st.reject(err);
    }
    this.streams.clear();
    this.onDead?.();
  }

  close() {
    this.kill(new TypeError('VPC Link: соединение закрыто'));
    try {
      this.ws.close();
    } catch {
      /* уже закрыт */
    }
  }

  async request(path: string, init: RequestInit): Promise<Response> {
    const method = (init.method ?? 'GET').toUpperCase();
    // Request сам кодирует тело (строка, FormData с boundary, Blob…) и заголовки
    const req = new Request('http://link.invalid' + path, { method, headers: init.headers, body: init.body });
    const body = method === 'GET' || method === 'HEAD' ? new Uint8Array(0) : new Uint8Array(await req.arrayBuffer());
    const headers: Record<string, string> = {};
    req.headers.forEach((v, k) => {
      headers[k] = v;
    });
    const signal = init.signal;
    if (signal?.aborted) throw signal.reason ?? new DOMException('Aborted', 'AbortError');
    const id = this.nextId++;
    const result = new Promise<Response>((resolve, reject) => {
      this.streams.set(id, { resolve, reject, controller: null, started: false });
    });
    if (signal) {
      const onAbort = () => {
        const st = this.streams.get(id);
        if (!st) return;
        this.streams.delete(id);
        const reason = signal.reason ?? new DOMException('Aborted', 'AbortError');
        if (st.started) st.controller?.error(reason);
        else st.reject(reason);
        try {
          this.send(CANCEL, id);
        } catch {
          /* соединения уже нет */
        }
      };
      signal.addEventListener('abort', onAbort, { once: true });
    }
    this.send(REQ, id, enc.encode(JSON.stringify({ m: method, p: path, h: headers })));
    for (let i = 0; i < body.length; i += CHUNK) this.send(REQ_DATA, id, body.subarray(i, Math.min(i + CHUNK, body.length)));
    this.send(REQ_END, id);
    return result;
  }
}

let session: Session | null = null;
let opening: Promise<Session> | null = null;

/** Закрыть текущее соединение: следующий запрос подключится заново
 *  (сменилась сеть, «переподключиться» в интерфейсе). */
export function reconnectLink() {
  dropSession();
}

function dropSession() {
  session?.close();
  session = null;
  opening = null;
  setState(getLinkConfig() ? 'connecting' : 'off');
}

function rememberHello(cfg: LinkConfig, hello: Hello) {
  // Ноутбук сообщает свои текущие адреса — DHCP мог их поменять
  const port = hello.port;
  const lan = port && Array.isArray(hello.lan) ? hello.lan.map((ip) => `${ip}:${port}`) : null;
  const name = hello.name || cfg.n;
  if ((lan && JSON.stringify(lan) !== JSON.stringify(cfg.l)) || name !== cfg.n) {
    setLinkConfig({ ...cfg, l: lan ?? cfg.l, n: name });
  }
}

async function getSession(): Promise<Session> {
  if (session && !session.dead) return session;
  if (opening) return opening;
  const cfg = getLinkConfig();
  if (!cfg) throw new TypeError('VPC Link: ноутбук не сопряжён');
  setState('connecting');
  opening = connect(cfg, unb64u(cfg.sk), MODE_CONNECT, enc.encode('{"v":1}'))
    .then((o) => {
      const s = new Session(o);
      s.onDead = () => {
        if (session === s) {
          session = null;
          setState('offline');
        }
      };
      session = s;
      setState(o.via);
      rememberHello(cfg, o.hello);
      return s;
    })
    .catch((e) => {
      setState('offline');
      throw e instanceof TypeError ? e : new TypeError(`VPC Link: ${e instanceof Error ? e.message : String(e)}`);
    })
    .finally(() => {
      opening = null;
    });
  return opening;
}

/** fetch к API ядра через канал (path — от корня: /api/…). */
export async function linkFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const s = await getSession();
  return s.request(path, init);
}

/** Сопряжение по QR-коду: свой ключ, рукопожатие с одноразовым секретом,
 *  подтверждение. Успех — настройки сохранены, канал открыт. */
export async function pair(offer: PairingOffer, deviceName: string): Promise<LinkConfig> {
  const sk = generatePrivate();
  const payload = enc.encode(JSON.stringify({ v: 1, offer: offer.o, name: deviceName }));
  dropSession();
  const o = await connect(offer, sk, MODE_PAIR, payload, unb64u(offer.p));
  const s = new Session(o);
  const paired = s.waitFrame(PAIRED, 10000);
  s.send(PAIR_CONFIRM, 0);
  let id = '';
  try {
    id = (JSON.parse(dec.decode(await paired)) as { id?: string }).id ?? '';
  } catch (e) {
    s.close();
    throw e;
  }
  const cfg: LinkConfig = { v: 1, k: offer.k, r: offer.r, n: offer.n, u: offer.u, l: offer.l, sk: b64u(sk), id };
  setLinkConfig(cfg);
  rememberHello(cfg, o.hello);
  s.onDead = () => {
    if (session === s) {
      session = null;
      setState('offline');
    }
  };
  session = s;
  setState(o.via);
  return getLinkConfig() ?? cfg;
}

/** Причина отказа ноутбука для экрана: код закрытия → ключ перевода. */
export function refusalKey(e: unknown): string | null {
  if (!(e instanceof LinkClosed)) return null;
  if (e.code === 4403 || e.code === 4401) return 'link.errNotPaired';
  if (e.code === 4410) return 'link.errOfferExpired';
  if (e.code === 4404) return 'link.errLaptopOffline';
  return null;
}
