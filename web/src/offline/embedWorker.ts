import { createEmbedder } from './embedCore';
import type { Embedder } from './embedCore';

/* Воркер модели векторов (embed.ts): загрузка (разбор токенизатора 17 МБ и
   модели 118 МБ) и счёт не держат интерфейс, а память модели уходит вместе
   с воркером, когда его закрывают после простоя. Файлы читает основной
   поток и передаёт сюда — сам воркер ничего не скачивает. */

export type EmbedRequest =
  | { id: number; type: 'init'; tokenizer: ArrayBuffer; model: ArrayBuffer; wasm: ArrayBuffer }
  | { id: number; type: 'embed'; texts: string[] };

export type EmbedReply = { id: number; vecs?: Float32Array[]; error?: string };

let embedder: Promise<Embedder> | null = null;

const reply = (msg: EmbedReply, transfer: Transferable[] = []) => self.postMessage(msg, { transfer });

self.onmessage = async (e: MessageEvent<EmbedRequest>) => {
  const m = e.data;
  try {
    if (m.type === 'init') {
      embedder = createEmbedder({
        tokenizer: JSON.parse(new TextDecoder().decode(m.tokenizer)) as object,
        model: new Uint8Array(m.model),
        wasm: m.wasm,
      });
      await embedder;
      reply({ id: m.id, vecs: [] });
      return;
    }
    if (!embedder) throw new Error('not loaded');
    const emb = await embedder;
    const vecs: Float32Array[] = [];
    for (const text of m.texts) vecs.push(await emb.embed(text));
    reply({ id: m.id, vecs }, vecs.map((v) => v.buffer));
  } catch (err) {
    reply({ id: m.id, error: err instanceof Error ? err.message : String(err) });
  }
};
