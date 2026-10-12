// Телефон VPC Link под Node — настоящий web/src/link/client.ts (Node 22+
// запускает TypeScript сам; WebSocket — встроенный). Вызывается из
// scripts/test_link.py: сопряжение по строке из QR, затем запросы к тестовому
// API; результат каждого шага — строка JSON в stdout.
//
//   node scripts/link_node_client.mjs <vpclink:…> [lan|relay]
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const link = await import(root + 'web/src/link/client.ts');

const mem = new Map();
link.useStorage({ getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v), removeItem: (k) => mem.delete(k) });

const [uri, mode = 'lan'] = process.argv.slice(2);
const out = (step, data) => console.log(JSON.stringify({ step, ...data }));
const fail = (step, e) => out(step, { error: String(e?.message ?? e), name: e?.name });

try {
  const offer = link.parsePairingUri(uri);
  const cfg = await link.pair(offer, 'Node test');
  out('pair', { id: cfg.id, state: link.linkState(), name: cfg.n });
} catch (e) {
  fail('pair', e);
  process.exit(0);
}

if (mode === 'relay') {
  // Локальный адрес недоступен — остаётся только посредник
  const cfg = link.getLinkConfig();
  link.setLinkConfig({ ...cfg, l: ['127.0.0.1:9'] });
  link.reconnectLink();
}

try {
  const r = await link.linkFetch('/api/health');
  out('health', { status: r.status, json: await r.json(), state: link.linkState() });
} catch (e) {
  fail('health', e);
}

try {
  const r = await link.linkFetch('/api/echo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Test': 'spoof', 'X-VPC-Link': 'spoof' },
    body: JSON.stringify({ hello: 'мир', n: 1 }),
  });
  out('echo', { status: r.status, json: await r.json() });
} catch (e) {
  fail('echo', e);
}

try {
  const fd = new FormData();
  fd.append('file', new Blob([new Uint8Array(200_000).fill(7)]), 'a.bin');
  const r = await link.linkFetch('/api/echo', { method: 'POST', body: fd });
  out('upload', { status: r.status, json: await r.json() });
} catch (e) {
  fail('upload', e);
}

try {
  const t0 = Date.now();
  const r = await link.linkFetch('/api/stream');
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  const chunks = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push({ t: Date.now() - t0, text: dec.decode(value) });
  }
  out('stream', { status: r.status, chunks });
} catch (e) {
  fail('stream', e);
}

try {
  const r = await link.linkFetch('/api/big');
  const buf = new Uint8Array(await r.arrayBuffer());
  let sum = 0;
  for (const b of buf) sum = (sum + b) % 65521;
  out('big', { status: r.status, len: buf.length, sum });
} catch (e) {
  fail('big', e);
}

try {
  const ctl = new AbortController();
  setTimeout(() => ctl.abort(), 400);
  const r = await link.linkFetch('/api/slow', { signal: ctl.signal });
  await r.text();
  out('abort', { error: 'not aborted' });
} catch (e) {
  out('abort', { name: e?.name, error: String(e?.message ?? e) });
}

try {
  const r = await link.linkFetch('/api/empty');
  out('empty', { status: r.status, body: r.body === null });
} catch (e) {
  fail('empty', e);
}

try {
  const r = await link.linkFetch('/docs');
  out('outside', { status: r.status });
} catch (e) {
  fail('outside', e);
}

out('done', { state: link.linkState() });
process.exit(0);
