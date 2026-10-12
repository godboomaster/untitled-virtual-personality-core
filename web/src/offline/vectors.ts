/* Векторы для поиска по смыслу без ноутбука. Реплики и факты приходят с
   векторами из базы ядра (offline_api.pack_vector: int8 по наибольшей
   координате, base64), вектор вопроса телефон считает той же моделью
   (embedCore.ts). Без зависимостей — scripts/test_offline_vectors.py гоняет
   его под Node. */

// Модель ядра (app/core/st_embedder.py): векторы с ноутбука — только от неё
export const EMBED_MODEL = 'paraphrase-multilingual-MiniLM-L12-v2';
export const EMBED_DIM = 384;

export type Vec = Int8Array | Float32Array;

/** Вектор с ноутбука; пусто или не той длины — null. */
export function unpackVec(b64: string | undefined | null, dim = EMBED_DIM): Int8Array | null {
  if (!b64) return null;
  try {
    const bin = atob(b64);
    if (bin.length !== dim) return null;
    const out = new Int8Array(dim);
    for (let i = 0; i < dim; i++) out[i] = (bin.charCodeAt(i) << 24) >> 24;
    return out;
  } catch {
    return null;
  }
}

/** Косинус: от масштаба векторов не зависит (int8 и float32 сравнимы). */
export function cosine(a: Vec, b: Vec): number {
  if (a.length !== b.length) return 0;
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return na && nb ? dot / Math.sqrt(na * nb) : 0;
}
