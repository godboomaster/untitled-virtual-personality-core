/* Noise Protocol Framework (ревизия 34): Noise_<паттерн>_25519_ChaChaPoly_SHA256 —
   сторона телефона канала VPC Link. Зеркало app/link/noise.py: паттерны IK
   (обычное подключение), IKpsk2 (сопряжение по QR), XX и KK — для сверки с
   официальными тестовыми векторами. Примитивы — аудированные @noble/*; своих
   шифров здесь нет, только склейка по спецификации
   https://noiseprotocol.org/noise.html */

import { x25519 } from '@noble/curves/ed25519.js';
import { chacha20poly1305 } from '@noble/ciphers/chacha.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { hmac } from '@noble/hashes/hmac.js';

const DHLEN = 32;
const HASHLEN = 32;
const TAGLEN = 16;
export const MAX_MESSAGE = 65535;
export const MAX_PAYLOAD = MAX_MESSAGE - TAGLEN;

type Token = 'e' | 's' | 'ee' | 'es' | 'se' | 'ss' | 'psk';

// Паттерн → [предсообщения инициатора, предсообщения ответчика, сообщения]
const PATTERNS: Record<string, [Token[], Token[], Token[][]]> = {
  IK: [[], ['s'], [['e', 'es', 's', 'ss'], ['e', 'ee', 'se']]],
  XX: [[], [], [['e'], ['e', 'ee', 's', 'es'], ['s', 'se']]],
  KK: [['s'], ['s'], [['e', 'es', 'ss'], ['e', 'ee', 'se']]],
};

export class NoiseError extends Error {}

export function generatePrivate(): Uint8Array {
  return x25519.utils.randomSecretKey();
}

export function publicKey(priv: Uint8Array): Uint8Array {
  return x25519.getPublicKey(priv);
}

function dh(priv: Uint8Array, pub: Uint8Array): Uint8Array {
  try {
    return x25519.getSharedSecret(priv, pub);
  } catch (e) {
    throw new NoiseError(`DH: ${e instanceof Error ? e.message : String(e)}`);
  }
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let pos = 0;
  for (const p of parts) {
    out.set(p, pos);
    pos += p.length;
  }
  return out;
}

function hkdf(ck: Uint8Array, ikm: Uint8Array, n: number): Uint8Array[] {
  const temp = hmac(sha256, ck, ikm);
  const out: Uint8Array[] = [];
  let prev: Uint8Array = new Uint8Array(0);
  for (let i = 1; i <= n; i++) {
    prev = hmac(sha256, temp, concat(prev, Uint8Array.of(i)));
    out.push(prev);
  }
  return out;
}

export class CipherState {
  k: Uint8Array | null;
  n = 0;

  constructor(k: Uint8Array | null = null) {
    this.k = k;
  }

  private nonce(): Uint8Array {
    // 2^53 сообщений в одном канале не наберётся; Number — достаточно
    if (this.n >= Number.MAX_SAFE_INTEGER) throw new NoiseError('исчерпан счётчик nonce');
    const nonce = new Uint8Array(12);
    new DataView(nonce.buffer).setBigUint64(4, BigInt(this.n), true);
    return nonce;
  }

  encrypt(ad: Uint8Array, plaintext: Uint8Array): Uint8Array {
    if (!this.k) return plaintext;
    const out = chacha20poly1305(this.k, this.nonce(), ad).encrypt(plaintext);
    this.n++;
    return out;
  }

  decrypt(ad: Uint8Array, ciphertext: Uint8Array): Uint8Array {
    if (!this.k) return ciphertext;
    let out: Uint8Array;
    try {
      out = chacha20poly1305(this.k, this.nonce(), ad).decrypt(ciphertext);
    } catch {
      throw new NoiseError('неверная метка: подмена или чужой ключ');
    }
    this.n++;
    return out;
  }
}

class SymmetricState {
  h: Uint8Array;
  ck: Uint8Array;
  cs = new CipherState();

  constructor(protocolName: Uint8Array) {
    if (protocolName.length <= HASHLEN) {
      this.h = new Uint8Array(HASHLEN);
      this.h.set(protocolName);
    } else {
      this.h = sha256(protocolName);
    }
    this.ck = this.h;
  }

  mixKey(ikm: Uint8Array) {
    const [ck, k] = hkdf(this.ck, ikm, 2);
    this.ck = ck;
    this.cs = new CipherState(k);
  }

  mixHash(data: Uint8Array) {
    this.h = sha256(concat(this.h, data));
  }

  mixKeyAndHash(ikm: Uint8Array) {
    const [ck, h, k] = hkdf(this.ck, ikm, 3);
    this.ck = ck;
    this.mixHash(h);
    this.cs = new CipherState(k);
  }

  encryptAndHash(plaintext: Uint8Array): Uint8Array {
    const ct = this.cs.encrypt(this.h, plaintext);
    this.mixHash(ct);
    return ct;
  }

  decryptAndHash(ciphertext: Uint8Array): Uint8Array {
    const pt = this.cs.decrypt(this.h, ciphertext);
    this.mixHash(ciphertext);
    return pt;
  }

  split(): [CipherState, CipherState] {
    const [k1, k2] = hkdf(this.ck, new Uint8Array(0), 2);
    return [new CipherState(k1), new CipherState(k2)];
  }
}

export interface HandshakeOptions {
  s?: Uint8Array; // свой статический закрытый ключ
  rs?: Uint8Array; // заранее известный открытый ключ собеседника
  psk?: Uint8Array; // общий секрет для паттернов pskN
  e?: Uint8Array; // только для тестовых векторов
}

/** Одно рукопожатие. initiator — кто пишет первым (телефон). */
export class HandshakeState {
  private messages: Token[][];
  private pskMode: boolean;
  private psk: Uint8Array | null;
  private initiator: boolean;
  private s: Uint8Array | null;
  private sPub: Uint8Array | null;
  private e: Uint8Array | null;
  private ePub: Uint8Array | null;
  rs: Uint8Array | null;
  private re: Uint8Array | null = null;
  private ss: SymmetricState;
  private step = 0;
  private result: [CipherState, CipherState] | null = null;

  constructor(pattern: string, initiator: boolean, prologue: Uint8Array, opts: HandshakeOptions = {}) {
    const [base, pskPos] = parsePattern(pattern);
    const [preI, preR, messages] = PATTERNS[base];
    this.messages = messages.map((m) => [...m]);
    for (const p of pskPos) {
      if (p === 0) this.messages[0].unshift('psk');
      else this.messages[p - 1].push('psk');
    }
    this.pskMode = pskPos.length > 0;
    if (opts.psk && opts.psk.length !== 32) throw new Error('нужен psk из 32 байт');
    this.psk = opts.psk ?? null;
    this.initiator = initiator;
    this.s = opts.s ?? null;
    this.sPub = this.s ? publicKey(this.s) : null;
    this.e = opts.e ?? null;
    this.ePub = this.e ? publicKey(this.e) : null;
    this.rs = opts.rs ?? null;
    this.ss = new SymmetricState(new TextEncoder().encode(`Noise_${pattern}_25519_ChaChaPoly_SHA256`));
    this.ss.mixHash(prologue);
    // Предсообщения: ключи, известные заранее, входят в хеш рукопожатия
    for (const [ownerIsInitiator, pre] of [[true, preI], [false, preR]] as const) {
      for (const token of pre) {
        void token;
        const key = ownerIsInitiator === initiator ? this.sPub : this.rs;
        if (!key) throw new Error('паттерн требует заранее известный статический ключ');
        this.ss.mixHash(key);
      }
    }
  }

  get finished(): boolean {
    return this.result !== null;
  }

  get handshakeHash(): Uint8Array {
    return this.ss.h;
  }

  private myTurn(): boolean {
    return (this.step % 2 === 0) === this.initiator;
  }

  private dhToken(token: Token): Uint8Array {
    const need = (k: Uint8Array | null) => {
      if (!k) throw new NoiseError(`нет ключа для ${token}`);
      return k;
    };
    if (token === 'ee') return dh(need(this.e), need(this.re));
    if (token === 'ss') return dh(need(this.s), need(this.rs));
    // es: e инициатора × s ответчика; se — наоборот
    const initSide = token[0];
    const respSide = token[1];
    if (this.initiator) {
      return initSide === 'e' ? dh(need(this.e), need(this.rs)) : dh(need(this.s), need(this.re));
    }
    return respSide === 's' ? dh(need(this.s), need(this.re)) : dh(need(this.e), need(this.rs));
  }

  writeMessage(payload: Uint8Array = new Uint8Array(0)): Uint8Array {
    if (this.finished || !this.myTurn()) throw new NoiseError('сейчас не очередь писать');
    const parts: Uint8Array[] = [];
    for (const token of this.messages[this.step]) {
      if (token === 'e') {
        if (!this.e) {
          this.e = generatePrivate();
          this.ePub = publicKey(this.e);
        }
        const ePub = this.ePub as Uint8Array;
        parts.push(ePub);
        this.ss.mixHash(ePub);
        if (this.pskMode) this.ss.mixKey(ePub);
      } else if (token === 's') {
        parts.push(this.ss.encryptAndHash(this.sPub as Uint8Array));
      } else if (token === 'psk') {
        if (!this.psk) throw new NoiseError('psk не задан');
        this.ss.mixKeyAndHash(this.psk);
      } else {
        this.ss.mixKey(this.dhToken(token));
      }
    }
    parts.push(this.ss.encryptAndHash(payload));
    const out = concat(...parts);
    if (out.length > MAX_MESSAGE) throw new NoiseError('сообщение рукопожатия длиннее 65535 байт');
    this.advance();
    return out;
  }

  readMessage(message: Uint8Array): Uint8Array {
    if (this.finished || this.myTurn()) throw new NoiseError('сейчас не очередь читать');
    if (message.length > MAX_MESSAGE) throw new NoiseError('сообщение длиннее 65535 байт');
    let pos = 0;
    for (const token of this.messages[this.step]) {
      if (token === 'e') {
        if (message.length - pos < DHLEN) throw new NoiseError('короткое сообщение рукопожатия');
        this.re = message.slice(pos, pos + DHLEN);
        pos += DHLEN;
        this.ss.mixHash(this.re);
        if (this.pskMode) this.ss.mixKey(this.re);
      } else if (token === 's') {
        const n = DHLEN + (this.ss.cs.k ? TAGLEN : 0);
        if (message.length - pos < n) throw new NoiseError('короткое сообщение рукопожатия');
        this.rs = this.ss.decryptAndHash(message.slice(pos, pos + n));
        pos += n;
      } else if (token === 'psk') {
        if (!this.psk) throw new NoiseError('psk не задан');
        this.ss.mixKeyAndHash(this.psk);
      } else {
        this.ss.mixKey(this.dhToken(token));
      }
    }
    const payload = this.ss.decryptAndHash(message.slice(pos));
    this.advance();
    return payload;
  }

  private advance() {
    this.step++;
    if (this.step === this.messages.length) this.result = this.ss.split();
  }

  transport(): Transport {
    if (!this.result) throw new NoiseError('рукопожатие не завершено');
    const [c1, c2] = this.result;
    return new Transport(this.initiator ? c1 : c2, this.initiator ? c2 : c1, this.handshakeHash, this.rs);
  }
}

function parsePattern(pattern: string): [string, number[]] {
  // "IKpsk2" → ["IK", [2]]; несколько модификаторов — через «+»: "XXpsk0+psk3"
  const i = pattern.indexOf('psk');
  const base = i < 0 ? pattern : pattern.slice(0, i);
  const psks = i < 0 ? [] : pattern.slice(i).split('+').map((p) => Number(p.replace('psk', '')));
  if (!PATTERNS[base]) throw new Error(`паттерн ${pattern} не поддерживается`);
  return [base, psks];
}

/** Канал после рукопожатия: свой шифр на каждое направление. */
export class Transport {
  private sendCs: CipherState;
  private recvCs: CipherState;
  readonly handshakeHash: Uint8Array;
  readonly remoteStatic: Uint8Array | null;

  constructor(send: CipherState, recv: CipherState, handshakeHash: Uint8Array, remoteStatic: Uint8Array | null) {
    this.sendCs = send;
    this.recvCs = recv;
    this.handshakeHash = handshakeHash;
    this.remoteStatic = remoteStatic;
  }

  encrypt(plaintext: Uint8Array): Uint8Array {
    if (plaintext.length > MAX_PAYLOAD) throw new NoiseError('кадр длиннее 65519 байт — делите на части');
    return this.sendCs.encrypt(new Uint8Array(0), plaintext);
  }

  decrypt(ciphertext: Uint8Array): Uint8Array {
    if (ciphertext.length > MAX_MESSAGE) throw new NoiseError('кадр длиннее 65535 байт');
    return this.recvCs.decrypt(new Uint8Array(0), ciphertext);
  }
}
