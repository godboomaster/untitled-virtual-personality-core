import { useEffect } from 'react';
import { api, ApiError, inboxClientId, isNativeApp } from '../api';
import type { PhoneLlmJob } from '../api';
import { useApiOnline } from '../apiData';
import { OfflineLlm } from './plugin';

/* Gemma телефона — движок служебных задач ядра (app/core/phone_llm), когда в
   «Настройки» → «Ключи и модели» → Ollama выбрано «на телефоне»: не на
   каждом ноутбуке потянет Gemma, а здесь она уже скачана для ответов без
   ноутбука. Ядро само к телефону прийти не может — приложение держит долгий
   запрос GET /api/phone-llm/next, задача (промпт служебного вызова) приходит
   ответом, её считает OfflineLlm.localChat (та же модель, видеоядро), ответ
   уходит POST /api/phone-llm/result.

   Здесь — пока приложение на экране; свёрнутое считает фоновая служба
   (BackgroundInboxService, при подключении по адресу и токену), и вдвоём они
   не опрашивают. Модели нет — не опрашиваем, считать нечем. Gemma выбрана
   на ноутбуке — спрашиваем раз в IDLE_MS, чтобы в настройках было видно,
   что телефон на связи. */

const WAIT_SEC = 25;
const IDLE_MS = 30_000; // Gemma на ноутбуке или модели нет
const RETRY_MS = 5_000; // ядро не ответило
const HIDDEN_MS = 5_000; // приложение свёрнуто — проверить, не вернулось ли
const BUSY_MS = 2_000; // модель считает ответ — задача встала бы за ним

// Имя телефона для настроек ядра: модель из User-Agent WebView
function deviceName(): string {
  const m = /;\s*([^;)]+?)\s+Build\//.exec(navigator.userAgent);
  return (m?.[1] ?? 'Android').slice(0, 64);
}

async function runJob(worker: string, job: PhoneLlmJob): Promise<void> {
  const t0 = performance.now();
  // Срок — по часам телефона: модель может быть занята, и задача ждёт очереди
  const deadline = job.ttl_ms ? Date.now() + job.ttl_ms : undefined;
  if (job.kind === 'warm') {
    // Только загрузить модель: вопрос придёт следом, а загрузка — 10–20 с.
    // Отчёта ядро не ждёт
    await OfflineLlm.localChat({ requestId: `core-${job.id}`, system: '', history: [], text: '.', maxTokens: 1, temperature: 0 })
      .catch(() => {});
    return;
  }
  let result: { text?: string; error?: string; backend?: string };
  try {
    const r = await OfflineLlm.localChat({
      requestId: `core-${job.id}`,
      system: job.system ?? '',
      history: job.history ?? [],
      text: job.text ?? '',
      maxTokens: job.max_tokens,
      temperature: job.temperature,
      deadline,
    });
    result = { text: r.text, backend: r.backend };
  } catch (e) {
    result = { error: (e instanceof Error ? e.message : String(e)).slice(0, 500) };
  }
  await api.phoneLlmResult({ worker, id: job.id, ...result, ms: Math.round(performance.now() - t0) }).catch(() => {});
}

/** Телефон считает задачи Gemma ядра, пока приложение открыто и ядро на связи. */
export function usePhoneGemma(): void {
  const apiOnline = useApiOnline();
  useEffect(() => {
    if (!isNativeApp() || !apiOnline) return;
    let stop = false;
    const abort = new AbortController();
    const sleep = (ms: number) => new Promise<void>((res) => {
      const done = () => {
        clearTimeout(t);
        abort.signal.removeEventListener('abort', done);
        res();
      };
      const t = setTimeout(done, ms);
      abort.signal.addEventListener('abort', done, { once: true });
    });
    const worker = `${inboxClientId().slice(0, 50)}-gemma`;
    const name = deviceName();
    void (async () => {
      while (!stop) {
        if (document.visibilityState !== 'visible') {
          await sleep(HIDDEN_MS);
          continue;
        }
        try {
          const st = await OfflineLlm.modelStatus();
          if (!st.downloaded) {
            await sleep(IDLE_MS);
            continue;
          }
          // modelStatus у соседнего приложения отвечает до ~13 с — за это
          // время могло свернуться, а visibilitychange уже не придёт
          if (document.visibilityState !== 'visible') continue;
          if (st.busy) {
            await sleep(BUSY_MS);
            continue;
          }
          // Свернулось посреди опроса — опрос прервать: ядро не отдаст задачу
          // тому, кто уже не слушает. Свёрнутому WebView ответы плагинов
          // приходят, только когда приложение вернётся на экран
          const poll = new AbortController();
          const cut = () => poll.abort();
          const onHide = () => { if (document.visibilityState !== 'visible') cut(); };
          document.addEventListener('visibilitychange', onHide);
          abort.signal.addEventListener('abort', cut, { once: true });
          let r;
          try {
            r = await api.phoneLlmNext(
              { worker, wait: WAIT_SEC, backend: st.backend ?? '', loaded: st.loaded, name },
              poll.signal,
            );
          } finally {
            document.removeEventListener('visibilitychange', onHide);
            abort.signal.removeEventListener('abort', cut);
          }
          if (stop) return;
          if (r.job && document.visibilityState !== 'visible') {
            // Задача пришла, когда уже свернулось, — вернуть: её возьмёт фоновая служба
            await api.phoneLlmResult({ worker, id: r.job.id, release: true }).catch(() => {});
          } else if (r.job) await runJob(worker, r.job);
          else if (!r.enabled) await sleep(IDLE_MS);
        } catch (e) {
          // Старое ядро без этого маршрута — опрашивать нечего
          if (stop || (e instanceof ApiError && e.status === 404)) return;
          await sleep(RETRY_MS);
        }
      }
    })();
    return () => {
      stop = true;
      abort.abort();
    };
  }, [apiOnline]);
}
