/* Два движка голосового режима под одним интерфейсом:
   - браузер — Web Speech API (SpeechRecognition + speechSynthesis), как было
     в VoiceChat;
   - приложение Android — плагин NativeSpeech (SpeechRecognizer и TextToSpeech
     телефона): в WebView нет ни распознавания, ни озвучки.
   Модуль без зависимостей от остального веба — его гоняет тест под Node
   с поддельными window и плагином (scripts/test_app_voice.py). */

// ===== Общий интерфейс =====

export interface ListenCallbacks {
  // Каждое событие распознавания: итоговый текст (может быть пустым) и
  // промежуточный — живая расшифровка
  onResult: (finalText: string, interimText: string) => void;
  // Ошибка сессии (коды в духе Web Speech API: no-speech, no-match,
  // not-allowed, network, unavailable...). После неё всегда приходит onEnd
  onError: (code: string) => void;
  // Конец сессии — ровно один раз
  onEnd: () => void;
}

export interface ListenSession {
  // Перестать слушать: движок ещё пришлёт итог сказанного и onEnd
  stop: () => void;
}

export type SpeakStatus = 'done' | 'interrupted' | 'error' | 'unavailable';

export interface SpeechEngine {
  kind: 'browser' | 'native';
  // Ответ без ожидания, если он известен (браузер), иначе null
  recognitionSupportedNow: () => boolean | null;
  recognitionAvailable: () => Promise<boolean>;
  listen: (lang: string, cb: ListenCallbacks) => ListenSession;
  // Озвучить реплику целиком; промис — когда договорено, прервано или сорвалось
  speak: (text: string, lang: string) => Promise<SpeakStatus>;
  stopSpeaking: () => void;
}

// ===== Браузер: Web Speech API =====

// Минимальные типы Web Speech API (в lib.dom их нет)
interface SpeechRecognitionAlternativeLike {
  transcript: string;
}
interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: SpeechRecognitionAlternativeLike;
}
interface SpeechRecognitionEventLike {
  results: ArrayLike<SpeechRecognitionResultLike>;
}
interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: ((e?: { error?: string }) => void) | null;
  start: () => void;
  stop: () => void;
}
type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

interface UtteranceLike {
  lang: string;
  voice: unknown;
  onend: (() => void) | null;
  onerror: (() => void) | null;
}
interface SynthesisLike {
  cancel: () => void;
  speak: (u: UtteranceLike) => void;
  getVoices: () => { lang: string }[];
}

// Окно браузера — только то, что нужно движку (в тесте подделывается)
export type WindowLike = Record<string, unknown>;

function recognitionCtor(win: WindowLike): SpeechRecognitionCtor | null {
  return (win.SpeechRecognition ?? win.webkitSpeechRecognition ?? null) as SpeechRecognitionCtor | null;
}

export function createBrowserEngine(win: WindowLike): SpeechEngine {
  const synthesis = (): SynthesisLike | null => (win.speechSynthesis as SynthesisLike | undefined) ?? null;
  return {
    kind: 'browser',
    recognitionSupportedNow: () => recognitionCtor(win) != null,
    recognitionAvailable: async () => recognitionCtor(win) != null,

    listen(lang, cb) {
      const Ctor = recognitionCtor(win);
      let ended = false;
      const end = () => {
        if (ended) return;
        ended = true;
        cb.onEnd();
      };
      if (!Ctor) {
        cb.onError('unavailable');
        end();
        return { stop: () => {} };
      }
      const rec = new Ctor();
      rec.lang = lang;
      rec.interimResults = true;
      rec.continuous = false;
      rec.onresult = (e) => {
        let finalText = '';
        let interimText = '';
        for (let i = 0; i < e.results.length; i++) {
          const r = e.results[i];
          if (r.isFinal) finalText += r[0].transcript;
          else interimText += r[0].transcript;
        }
        cb.onResult(finalText, interimText);
      };
      rec.onend = end;
      rec.onerror = (e) => {
        if (ended) return;
        cb.onError(e?.error || 'client');
        // onend браузер пришлёт следом — но не каждый, поэтому закрываем сами
        end();
      };
      try {
        rec.start();
      } catch {
        cb.onError('client');
        end();
      }
      return { stop: () => rec.stop() };
    },

    speak(text, lang) {
      const synth = synthesis();
      const Utterance = win.SpeechSynthesisUtterance as (new (text: string) => UtteranceLike) | undefined;
      if (!synth || !Utterance) return Promise.resolve('unavailable');
      synth.cancel();
      const utter = new Utterance(text);
      utter.lang = lang;
      // Голос — под язык (первая часть тега: ru-RU → ru)
      const prefix = lang.split('-')[0].toLowerCase();
      const voice = synth.getVoices().find((v) => v.lang.toLowerCase().startsWith(prefix));
      if (voice) utter.voice = voice;
      return new Promise<SpeakStatus>((resolve) => {
        utter.onend = () => resolve('done');
        // cancel() прошлой реплики тоже приходит сюда — для неё это «прервано»
        utter.onerror = () => resolve('interrupted');
        synth.speak(utter);
      });
    },

    stopSpeaking() {
      synthesis()?.cancel();
    },
  };
}

// ===== Приложение: плагин NativeSpeech =====

type PermState = 'granted' | 'denied' | 'prompt' | 'prompt-with-rationale';
interface ListenerHandle {
  remove: () => Promise<void>;
}
interface SessionEvent {
  session?: number;
}

// JS-сторона плагина (Java: android/.../NativeSpeechPlugin.java)
export interface NativeSpeechPlugin {
  isRecognitionAvailable(): Promise<{ available: boolean }>;
  checkPermissions(): Promise<{ microphone: PermState }>;
  requestPermissions(opts?: { permissions: 'microphone'[] }): Promise<{ microphone: PermState }>;
  startListening(opts: { lang: string; partialResults: boolean }): Promise<{ session?: number }>;
  stopListening(): Promise<void>;
  isTtsAvailable(): Promise<{ available: boolean }>;
  speak(opts: { text: string; lang: string; rate?: number }): Promise<{ status?: SpeakStatus }>;
  stopSpeaking(): Promise<void>;
  addListener(event: 'partial' | 'final', fn: (e: SessionEvent & { text?: string }) => void): Promise<ListenerHandle>;
  addListener(event: 'error', fn: (e: SessionEvent & { code?: string; message?: string }) => void): Promise<ListenerHandle>;
  addListener(event: 'end', fn: (e: SessionEvent) => void): Promise<ListenerHandle>;
}

export function createNativeEngine(plugin: NativeSpeechPlugin): SpeechEngine {
  // Ответ телефона «есть ли служба распознавания» не меняется за сеанс
  let available: boolean | null = null;
  let availableAsk: Promise<boolean> | null = null;

  const recognitionAvailable = (): Promise<boolean> => {
    if (available !== null) return Promise.resolve(available);
    if (!availableAsk) {
      availableAsk = plugin
        .isRecognitionAvailable()
        .then((r) => Boolean(r?.available))
        .catch(() => false)
        .then((v) => {
          available = v;
          return v;
        });
    }
    return availableAsk;
  };

  return {
    kind: 'native',
    recognitionSupportedNow: () => available,
    recognitionAvailable,

    listen(lang, cb) {
      let stopped = false;
      let started = false;
      let ended = false;
      // Номер сессии от плагина: события чужих (прошлых) сессий не наши.
      // До ответа startListening события копим и разбираем по номеру потом
      let session: number | null = null;
      let pending: { name: string; e: SessionEvent & { text?: string; code?: string } }[] = [];
      const handles: ListenerHandle[] = [];

      const end = () => {
        if (ended) return;
        ended = true;
        handles.splice(0).forEach((h) => void h.remove().catch(() => {}));
        cb.onEnd();
      };
      const fail = (code: string) => {
        if (ended) return;
        cb.onError(code);
        end();
      };
      const handle = (name: string, e: SessionEvent & { text?: string; code?: string }) => {
        if (ended) return;
        if (session === null) {
          pending.push({ name, e });
          return;
        }
        // -1 — плагин не сообщил номер: принимаем всё
        if (session !== -1 && e?.session !== undefined && e.session !== session) return;
        if (name === 'partial') cb.onResult('', e.text ?? '');
        else if (name === 'final') cb.onResult(e.text ?? '', '');
        else if (name === 'error') cb.onError(e.code || 'client');
        else if (name === 'end') end();
      };

      void (async () => {
        try {
          let perm = await plugin.checkPermissions();
          if (perm?.microphone !== 'granted') {
            if (stopped) return end();
            perm = await plugin.requestPermissions({ permissions: ['microphone'] });
          }
          if (perm?.microphone !== 'granted') return fail('not-allowed');
          if (stopped) return end();
          if (!(await recognitionAvailable())) return fail('unavailable');
          for (const name of ['partial', 'final', 'error', 'end'] as const) {
            handles.push(await plugin.addListener(name as 'end', (e) => handle(name, e)));
          }
          if (stopped) return end();
          const res = await plugin.startListening({ lang, partialResults: true });
          started = true;
          session = typeof res?.session === 'number' ? res.session : -1;
          const early = pending;
          pending = [];
          early.forEach(({ name, e }) => handle(name, e));
          if (stopped && !ended) void plugin.stopListening().catch(() => {});
        } catch (err) {
          const code = (err as { code?: unknown })?.code;
          fail(typeof code === 'string' && code ? code : 'client');
        }
      })();

      return {
        stop: () => {
          stopped = true;
          if (started && !ended) void plugin.stopListening().catch(() => {});
        },
      };
    },

    async speak(text, lang) {
      try {
        const r = await plugin.speak({ text, lang, rate: 1 });
        return r?.status ?? 'done';
      } catch {
        return 'error';
      }
    },

    stopSpeaking() {
      void plugin.stopSpeaking().catch(() => {});
    },
  };
}
