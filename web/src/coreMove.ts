import { useEffect } from 'react';
import { getApiToken, getApiUrl, isNativeApp, setApiUrl } from './api';
import { getLinkConfig, reconnectLink, setLinkConfig } from './link/client.ts';
import { recheckCore, useCoreHealth } from './apiData';
import { isStandalone } from './standalone';
import { syncBgInbox } from './bgInbox';
import { syncWakeWord } from './wakeWord';
import { discoverCores } from './coreFinder';
import { knownCoreId } from './coreId';

/* Ядро сменило адрес (DHCP выдал компьютеру другой IP) — телефон находит его
   сам. Id своего ядра телефон знает по /api/health (coreId.ts); связи нет —
   спрашивает сеть «кто тут ядро?» (coreFinder.ts) и ищет ответ с тем же id.
   Назваться им может кто угодно, поэтому прежде чем слать новому адресу
   токен — доказательство: GET /api/core/proof?nonce=… без токена, ядро
   отвечает HMAC токеном по nonce, id, своим адресам и порту
   (app/link/discovery.py). Подпись сошлась и адрес — среди адресов ядра:
   переключаемся (пересланная от настоящего ядра подпись к чужому адресу не
   подойдёт). По каналу VPC Link проверять нечего: Noise сам убедится, что на
   том конце ноутбук с ключом из сопряжения, — новый адрес пробуется первым.
   Подменяется только адрес вида http://IP:порт: имя (обратный прокси, связь
   вне дома) адресом домашней сети не заменяем. */

const DISCOVER_WAIT_MS = 1500;
const PROOF_TIMEOUT_MS = 4000;
const RETRY_MS = 60_000; // без связи — снова спросить сеть
const PROOF_PREFIX = 'vpc-core-proof/1';

interface CoreProof {
  id?: unknown;
  hosts?: unknown;
  port?: unknown;
  proof?: unknown;
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

async function hmacHex(key: string, msg: string): Promise<string> {
  const enc = new TextEncoder();
  const k = await crypto.subtle.importKey('raw', enc.encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return hex(new Uint8Array(await crypto.subtle.sign('HMAC', k, enc.encode(msg))));
}

/** Ядро на base — то самое: знает токен и считает host:port своим адресом. */
export async function verifyCore(base: string, host: string, port: number, id: string, token: string): Promise<boolean> {
  const nonce = hex(crypto.getRandomValues(new Uint8Array(16)));
  let r: CoreProof;
  try {
    const res = await fetch(`${base}/api/core/proof?nonce=${nonce}`, { signal: AbortSignal.timeout(PROOF_TIMEOUT_MS) });
    if (!res.ok) return false;
    r = (await res.json()) as CoreProof;
  } catch {
    return false;
  }
  const hosts = Array.isArray(r.hosts) && r.hosts.every((h) => typeof h === 'string') ? (r.hosts as string[]) : null;
  if (r.id !== id || r.port !== port || !hosts?.includes(host) || typeof r.proof !== 'string') return false;
  const expect = await hmacHex(token, [PROOF_PREFIX, id, nonce, hosts.join(','), String(port)].join('|'));
  return expect === r.proof;
}

// Сохранённый адрес — http://IPv4:порт (его и можно заменить найденным)
function ipAddressUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return u.protocol === 'http:' && /^(\d{1,3}\.){3}\d{1,3}$/.test(u.hostname);
  } catch {
    return false;
  }
}

let running: Promise<boolean> | null = null;

/** Найти своё ядро на новом адресе и переключиться. true — адрес сменился. */
export function relocateCore(): Promise<boolean> {
  running ??= (async () => {
    const id = knownCoreId();
    if (!id) return false;
    // Адрес до поиска: сменят его, пока идёт поиск и проверка, — выбор
    // пользователя важнее найденного
    const current = getApiUrl();
    let found;
    try {
      found = (await discoverCores(DISCOVER_WAIT_MS)).found;
    } catch {
      return false; // не в Wi-Fi или плагина нет
    }
    const mine = found.filter((c) => c.id === id);
    const link = getLinkConfig();
    if (link) {
      const fresh = mine.filter((c) => c.link > 0).map((c) => `${c.host}:${c.link}`).filter((a) => !link.l.includes(a));
      if (!fresh.length) return false;
      setLinkConfig({ ...link, l: [...fresh, ...link.l] });
      reconnectLink();
      return true;
    }
    const token = getApiToken();
    if (!ipAddressUrl(current) || !token) return false;
    for (const c of mine) {
      if (c.api <= 0) continue;
      const next = `http://${c.host}:${c.api}`;
      if (getApiUrl() !== current) return false;
      if (next === current || !(await verifyCore(next, c.host, c.api, id, token))) continue;
      if (getApiUrl() !== current) return false;
      setApiUrl(next);
      // Фоновая служба и голосовая активация — на новый адрес
      syncBgInbox();
      syncWakeWord();
      return true;
    }
    return false;
  })().finally(() => {
    running = null;
  });
  return running;
}

/** App: связи с ядром нет — спросить сеть, не переехало ли оно (сразу и
 * раз в минуту, пока связи нет). */
export function useCoreRelocate() {
  const health = useCoreHealth();
  useEffect(() => {
    if (!isNativeApp() || isStandalone() || health !== 'down') return;
    let alive = true;
    const run = () => {
      void relocateCore().then((moved) => {
        if (alive && moved) recheckCore();
      });
    };
    run();
    const timer = window.setInterval(run, RETRY_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [health]);
}
