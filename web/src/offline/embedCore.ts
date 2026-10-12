import type { InferenceSession, Tensor } from 'onnxruntime-web';

/* Модель векторов на телефоне: та же, что у ядра
   (paraphrase-multilingual-MiniLM-L12-v2), в ONNX int8 (Xenova, 118 МБ) —
   токенизатор XLM-R (@huggingface/tokenizers) и onnxruntime-web (WASM, один
   поток). Вектор — среднее по токенам, как у sentence-transformers;
   токены и векторы сверены с ядром (scripts/test_offline_vectors.py: те же
   номера токенов, косинус с вектором ядра ≥ 0,99). Без Capacitor: файлы
   отдаёт вызывающий — воркер приложения (embedWorker.ts) или тест под Node. */

export interface EmbedFiles {
  tokenizer: object; // tokenizer.json
  model: Uint8Array; // onnx/model_quantized.onnx
  wasm?: ArrayBuffer; // ort-wasm-simd-threaded.wasm — без него ORT ищет его сам
}

export interface Embedder {
  embed(text: string): Promise<Float32Array>;
  release(): Promise<void>;
}

// Как в sentence_bert_config.json модели: длиннее — обрезается (у ядра так же)
export const MAX_TOKENS = 128;

// tokenizer_config.json модели (без него токенизатор не знает спецсимволов)
const TOKENIZER_CONFIG = {
  bos_token: '<s>',
  clean_up_tokenization_spaces: true,
  cls_token: '<s>',
  do_lower_case: true,
  eos_token: '</s>',
  mask_token: { __type: 'AddedToken', content: '<mask>', lstrip: true, normalized: true, rstrip: false, single_word: false },
  model_max_length: 512,
  pad_token: '<pad>',
  sep_token: '</s>',
  strip_accents: null,
  tokenize_chinese_chars: true,
  tokenizer_class: 'BertTokenizer',
  unk_token: '<unk>',
};

export async function createEmbedder(files: EmbedFiles): Promise<Embedder> {
  const [ort, { Tokenizer }] = await Promise.all([import('onnxruntime-web/wasm'), import('@huggingface/tokenizers')]);
  ort.env.wasm.numThreads = 1; // WebView без cross-origin isolation — потоков всё равно нет
  if (files.wasm) ort.env.wasm.wasmBinary = files.wasm;
  const tok = new Tokenizer(files.tokenizer, TOKENIZER_CONFIG);
  const bos = tok.token_to_id('<s>') ?? 0;
  const eos = tok.token_to_id('</s>') ?? 2;
  let session: InferenceSession | null = await ort.InferenceSession.create(files.model, {
    executionProviders: ['wasm'],
    graphOptimizationLevel: 'all',
  });
  const needsTypes = session.inputNames.includes('token_type_ids');

  const ids64 = (ids: number[]) => BigInt64Array.from(ids, (x) => BigInt(x));

  return {
    async embed(text: string): Promise<Float32Array> {
      if (!session) throw new Error('embedder released');
      const body = tok.encode(text, { add_special_tokens: false }).ids.slice(0, MAX_TOKENS - 2);
      const ids = [bos, ...body, eos];
      const n = ids.length;
      const feeds: Record<string, Tensor> = {
        input_ids: new ort.Tensor('int64', ids64(ids), [1, n]),
        attention_mask: new ort.Tensor('int64', new BigInt64Array(n).fill(1n), [1, n]),
      };
      if (needsTypes) feeds.token_type_ids = new ort.Tensor('int64', new BigInt64Array(n), [1, n]);
      const out = await session.run(feeds);
      const hidden = out[session.outputNames[0]];
      const data = hidden.data as Float32Array;
      const dim = hidden.dims[hidden.dims.length - 1];
      const vec = new Float32Array(dim);
      for (let t = 0; t < n; t++) for (let k = 0; k < dim; k++) vec[k] += data[t * dim + k];
      for (let k = 0; k < dim; k++) vec[k] /= n;
      for (const tensor of Object.values(out)) tensor.dispose();
      return vec;
    },
    async release() {
      const s = session;
      session = null;
      await s?.release();
    },
  };
}
