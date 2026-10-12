// Проверка движков голосового режима (web/src/voice/speechEngines.ts) под Node:
// поддельные Web Speech API и плагин NativeSpeech. Печатает строки JSON
// {name, ok} — их разбирает scripts/test_app_voice.py.
// Запуск: node scripts/app_voice_node.mjs <путь к speechEngines.ts>

const { createBrowserEngine, createNativeEngine } = await import(process.argv[2]);

const report = (name, ok, extra) => console.log(JSON.stringify({ name, ok: Boolean(ok), extra }));
const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async () => {
  for (let i = 0; i < 20; i++) await tick();
};

function recorder() {
  const log = [];
  return {
    log,
    cb: {
      onResult: (f, i) => log.push(['result', f, i]),
      onError: (c) => log.push(['error', c]),
      onEnd: () => log.push(['end']),
    },
  };
}

// ===== Браузер =====

{
  const none = createBrowserEngine({});
  report('браузер: без SpeechRecognition — распознавания нет', none.recognitionSupportedNow() === false && (await none.recognitionAvailable()) === false);
  const r = recorder();
  none.listen('ru-RU', r.cb);
  report('браузер: listen без распознавания — unavailable и end', JSON.stringify(r.log) === JSON.stringify([['error', 'unavailable'], ['end']]));
  report('браузер: без speechSynthesis — speak = unavailable', (await none.speak('x', 'ru-RU')) === 'unavailable');
}

{
  let last = null;
  class FakeRec {
    constructor() {
      last = this;
      this.started = 0;
      this.stopped = 0;
    }
    start() {
      this.started++;
    }
    stop() {
      this.stopped++;
    }
  }
  const eng = createBrowserEngine({ webkitSpeechRecognition: FakeRec });
  report('браузер: webkitSpeechRecognition — распознавание есть', eng.recognitionSupportedNow() === true && eng.kind === 'browser');

  const r = recorder();
  const s = eng.listen('ru-RU', r.cb);
  report('браузер: настройки как раньше (lang, interim, не continuous, start)', last.lang === 'ru-RU' && last.interimResults === true && last.continuous === false && last.started === 1);
  last.onresult({ results: [{ isFinal: false, 0: { transcript: 'при' } }] });
  last.onresult({ results: [{ isFinal: true, 0: { transcript: 'привет' } }, { isFinal: false, 0: { transcript: ' ка' } }] });
  s.stop();
  last.onend();
  last.onend();
  report(
    'браузер: промежуточный и итоговый текст, end ровно один',
    JSON.stringify(r.log) === JSON.stringify([['result', '', 'при'], ['result', 'привет', ' ка'], ['end']]) && last.stopped === 1,
    r.log,
  );

  const r2 = recorder();
  eng.listen('en-US', r2.cb);
  last.onerror({ error: 'no-speech' });
  last.onend();
  report('браузер: ошибка — код, затем один end', JSON.stringify(r2.log) === JSON.stringify([['error', 'no-speech'], ['end']]), r2.log);
}

{
  // Синтез: cancel() обрывает текущую реплику через onerror, как в браузере
  const calls = [];
  let current = null;
  const synth = {
    cancel() {
      calls.push('cancel');
      const c = current;
      current = null;
      c?.onerror?.();
    },
    speak(u) {
      calls.push(['speak', u.text, u.lang, u.voice?.name]);
      current = u;
    },
    getVoices: () => [
      { lang: 'en-US', name: 'en' },
      { lang: 'ru-RU', name: 'ru' },
    ],
  };
  class Utter {
    constructor(text) {
      this.text = text;
      this.onend = null;
      this.onerror = null;
    }
  }
  const eng = createBrowserEngine({ speechSynthesis: synth, SpeechSynthesisUtterance: Utter });
  const p1 = eng.speak('первая', 'ru-RU');
  report('браузер: speak — cancel, затем фраза с языком и голосом под язык', JSON.stringify(calls) === JSON.stringify(['cancel', ['speak', 'первая', 'ru-RU', 'ru']]), calls);
  const p2 = eng.speak('вторая', 'ru-RU');
  report('браузер: новая реплика прерывает прошлую (interrupted)', (await p1) === 'interrupted');
  current.onend();
  report('браузер: реплика договорена — done', (await p2) === 'done');
  eng.stopSpeaking();
  report('браузер: stopSpeaking — cancel', calls.at(-1) === 'cancel');
}

// ===== Приложение: плагин =====

function fakePlugin(opts = {}) {
  const st = {
    perm: opts.perm ?? 'granted',
    afterRequest: opts.afterRequest ?? 'granted',
    available: opts.available ?? true,
    calls: [],
    listeners: {},
    session: opts.session ?? 7,
    startReject: opts.startReject ?? null,
    earlyEvents: opts.earlyEvents ?? [],
    speakResult: opts.speakResult,
  };
  const emit = (name, data) => (st.listeners[name] || []).slice().forEach((fn) => fn(data));
  const plugin = {
    async isRecognitionAvailable() {
      st.calls.push('isRecognitionAvailable');
      return { available: st.available };
    },
    async checkPermissions() {
      st.calls.push('checkPermissions');
      return { microphone: st.perm };
    },
    async requestPermissions(o) {
      st.calls.push(['requestPermissions', o]);
      st.perm = st.afterRequest;
      return { microphone: st.perm };
    },
    async addListener(name, fn) {
      (st.listeners[name] ||= []).push(fn);
      return {
        remove: async () => {
          st.listeners[name] = st.listeners[name].filter((f) => f !== fn);
        },
      };
    },
    async startListening(o) {
      st.calls.push(['startListening', o]);
      if (st.startReject) throw Object.assign(new Error('x'), { code: st.startReject });
      // События могут прийти раньше, чем веб узнает номер сессии
      st.earlyEvents.forEach(([n, d]) => emit(n, d));
      return { session: st.session };
    },
    async stopListening() {
      st.calls.push('stopListening');
    },
    async isTtsAvailable() {
      return { available: true };
    },
    async speak(o) {
      st.calls.push(['speak', o]);
      if (st.speakResult instanceof Error) throw st.speakResult;
      return { status: st.speakResult ?? 'done' };
    },
    async stopSpeaking() {
      st.calls.push('stopSpeaking');
    },
  };
  return { plugin, st, emit };
}

const count = (st, name) => st.calls.filter((c) => c === name || c[0] === name).length;
const listenerCount = (st) => Object.values(st.listeners).reduce((n, a) => n + a.length, 0);

{
  const { plugin, st } = fakePlugin();
  const eng = createNativeEngine(plugin);
  const before = eng.recognitionSupportedNow();
  const a = await eng.recognitionAvailable();
  await eng.recognitionAvailable();
  report('приложение: доступность — null до вопроса, потом ответ телефона, спрашиваем один раз', before === null && a === true && eng.recognitionSupportedNow() === true && count(st, 'isRecognitionAvailable') === 1 && eng.kind === 'native');
}

{
  const { plugin, st, emit } = fakePlugin();
  const eng = createNativeEngine(plugin);
  const r = recorder();
  eng.listen('ru-RU', r.cb);
  await settle();
  const start = st.calls.find((c) => c[0] === 'startListening');
  report('приложение: старт с языком и промежуточными результатами', start && start[1].lang === 'ru-RU' && start[1].partialResults === true && count(st, 'requestPermissions') === 0);
  emit('end', { session: 6 }); // конец прошлой сессии — не наш
  emit('partial', { session: 7, text: 'при' });
  emit('final', { session: 7, text: 'привет' });
  emit('end', { session: 7 });
  emit('end', { session: 7 });
  await settle();
  report(
    'приложение: partial/final → onResult, чужая сессия не считается, end один',
    JSON.stringify(r.log) === JSON.stringify([['result', '', 'при'], ['result', 'привет', ''], ['end']]),
    r.log,
  );
  report('приложение: подписки сняты после конца сессии', listenerCount(st) === 0);
}

{
  const { plugin, st } = fakePlugin({ perm: 'prompt', afterRequest: 'denied' });
  const eng = createNativeEngine(plugin);
  const r = recorder();
  eng.listen('ru-RU', r.cb);
  await settle();
  const req = st.calls.find((c) => c[0] === 'requestPermissions');
  report('приложение: нет разрешения — спрашиваем микрофон', req && JSON.stringify(req[1]) === JSON.stringify({ permissions: ['microphone'] }));
  report('приложение: отказ — not-allowed и end, без старта', JSON.stringify(r.log) === JSON.stringify([['error', 'not-allowed'], ['end']]) && count(st, 'startListening') === 0, r.log);
}

{
  const { plugin, st } = fakePlugin({ perm: 'prompt', afterRequest: 'granted' });
  const eng = createNativeEngine(plugin);
  const r = recorder();
  eng.listen('en-US', r.cb);
  await settle();
  report('приложение: разрешили — слушаем', count(st, 'startListening') === 1 && r.log.length === 0);
}

{
  const { plugin, st } = fakePlugin({ available: false });
  const eng = createNativeEngine(plugin);
  const r = recorder();
  eng.listen('ru-RU', r.cb);
  await settle();
  report('приложение: нет службы распознавания — unavailable, без старта', JSON.stringify(r.log) === JSON.stringify([['error', 'unavailable'], ['end']]) && count(st, 'startListening') === 0, r.log);
}

{
  const { plugin } = fakePlugin({ startReject: 'not-allowed' });
  const eng = createNativeEngine(plugin);
  const r = recorder();
  eng.listen('ru-RU', r.cb);
  await settle();
  report('приложение: отказ startListening — код ошибки из плагина', JSON.stringify(r.log) === JSON.stringify([['error', 'not-allowed'], ['end']]), r.log);
}

{
  const { plugin } = fakePlugin({
    earlyEvents: [
      ['end', { session: 6 }],
      ['error', { session: 7, code: 'no-match' }],
      ['end', { session: 7 }],
    ],
  });
  const eng = createNativeEngine(plugin);
  const r = recorder();
  eng.listen('ru-RU', r.cb);
  await settle();
  report('приложение: события до ответа startListening — разобраны по номеру сессии', JSON.stringify(r.log) === JSON.stringify([['error', 'no-match'], ['end']]), r.log);
}

{
  const { plugin, st } = fakePlugin();
  const eng = createNativeEngine(plugin);
  const r = recorder();
  const s = eng.listen('ru-RU', r.cb);
  s.stop(); // нажали «стоп» раньше, чем дошло до старта
  await settle();
  report('приложение: стоп до старта — сессия закрыта без старта', JSON.stringify(r.log) === JSON.stringify([['end']]) && count(st, 'startListening') === 0, r.log);

  const r2 = recorder();
  const s2 = eng.listen('ru-RU', r2.cb);
  await settle();
  s2.stop();
  await settle();
  report('приложение: стоп во время прослушивания — stopListening', count(st, 'stopListening') === 1 && r2.log.length === 0);
}

{
  const { plugin, st } = fakePlugin({ speakResult: 'unavailable' });
  const eng = createNativeEngine(plugin);
  const status = await eng.speak('Привет', 'ru-RU');
  const sp = st.calls.find((c) => c[0] === 'speak');
  report('приложение: speak — текст и язык в плагин, статус из плагина', status === 'unavailable' && sp[1].text === 'Привет' && sp[1].lang === 'ru-RU');
  st.speakResult = new Error('boom');
  report('приложение: сбой плагина при озвучке — error, без исключения', (await eng.speak('x', 'ru-RU')) === 'error');
  eng.stopSpeaking();
  await settle();
  report('приложение: stopSpeaking зовёт плагин', count(st, 'stopSpeaking') === 1);
}
