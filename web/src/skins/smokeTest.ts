/* Runtime-проверка скина («смоук-тест»): файл рендерится в скрытом
   sandboxed iframe тем же путём, что и в SkinFrame (prepareSkin + эталонный
   bridge), получает снапшот и какое-то время слушает ошибки. validateSkin
   проверяет только разметку — здесь ловится то, что всплывает при запуске:
   падающий JS скина, bridge, который не отработал, пустая лента чата,
   спрятанный экран.

   Скин изолирован (opaque origin), поэтому в документ до его скриптов
   вставляется маленький зонд: ловит window.error / unhandledrejection /
   нарушения CSP с самого начала (bridge вешает свой обработчик только
   в конце <body>) и после применённого снапшота сообщает, сколько
   сообщений в ленте, виден ли корневой блок экрана и обязательные
   элементы (поле ввода и кнопка отправки чата, аватар и занятие комнаты).

   Проверки:
   - сигнал ready от bridge — за READY_TIMEOUT_MS;
   - снапшот применился (событие vpc:state) — за STATE_TIMEOUT_MS после ready;
   - чат: второй снапшот — «печатает» + плашка «ответ на…» (ветки, которые
     первый снапшот не трогает) — тоже применился за STATE_TIMEOUT_MS;
   - в течение SETTLE_MS после последнего снапшота — ни одной ошибки;
   - чат: лента [data-vpc="messages"] наполнилась, если в снапшоте есть сообщения;
   - чат: поле ввода и кнопка отправки видны;
   - комната: аватар виден, подпись занятия заполнена (если оно есть в снапшоте);
   - корневой блок [data-vpc-screen] активного экрана виден;
   - документ скина не уводит iframe на другую страницу (второй load).

   Ошибки формулируются так, чтобы их можно было отдать нейросети-редактору. */

import { prepareSkin } from './engine';
import type { SkinScreen } from './engine';
import type { SkinStatePayload } from './payloads';

export const SMOKE_READY_TIMEOUT_MS = 3000;
export const SMOKE_STATE_TIMEOUT_MS = 2000;
export const SMOKE_SETTLE_MS = 1500;

export interface SmokeResult {
  ok: boolean;
  errors: string[];
  aborted?: boolean;
}

type Lang = 'ru' | 'en';

const MSG: Record<string, Record<Lang, string>> = {
  noReady: {
    ru: 'Скин не прислал сигнал ready за {n} с: bridge не запустился — вероятно, ошибка в скрипте скина до bridge или повреждён блок VPC-BRIDGE.',
    en: 'The skin did not send ready within {n} s: the bridge did not start — probably a script error before the bridge or a damaged VPC-BRIDGE block.',
  },
  noState: {
    ru: 'Снапшот данных не применился за {n} с после ready (событие vpc:state не пришло).',
    en: 'The data snapshot was not applied within {n} s after ready (no vpc:state event).',
  },
  runtime: {
    ru: 'Ошибка JS при запуске скина: {msg}',
    en: 'JS error while running the skin: {msg}',
  },
  csp: {
    ru: 'Песочница (CSP) заблокировала {what}: {uri}. Сети нет, eval запрещён — только inline-код и data-URI.',
    en: 'The sandbox (CSP) blocked {what}: {uri}. No network, no eval — inline code and data-URIs only.',
  },
  emptyFeed: {
    ru: 'Лента чата [data-vpc="messages"] осталась пустой после снапшота с {n} сообщениями — проверь <template data-vpc="message"> (внутри [data-vpc-field="text"]) и что контейнер ленты не пересоздаётся скриптом скина.',
    en: 'The chat feed [data-vpc="messages"] stayed empty after a snapshot with {n} messages — check <template data-vpc="message"> (with [data-vpc-field="text"] inside) and that the skin script does not recreate the feed container.',
  },
  navigated: {
    ru: 'Скин увёл свой документ на другую страницу (location, ссылка или reload) — так нельзя: приложение отключает такой скин. Ссылки и переходы не используй.',
    en: 'The skin navigated its document to another page (location, a link or reload) — not allowed: the app disables such a skin. Do not use links or navigation.',
  },
  hidden: {
    ru: 'Корневой блок [data-vpc-screen="{s}"] не виден при html[data-vpc-active="{s}"] (display:none или visibility:hidden) — активный экран должен показываться.',
    en: 'The root block [data-vpc-screen="{s}"] is not visible under html[data-vpc-active="{s}"] (display:none or visibility:hidden) — the active screen must be shown.',
  },
  noInput: {
    ru: 'Поле ввода [data-vpc="input"] не видно (display:none, visibility:hidden или нулевой размер) — строка ввода чата должна всегда показываться под лентой.',
    en: 'The input [data-vpc="input"] is not visible (display:none, visibility:hidden or zero size) — the chat input row must always be shown below the feed.',
  },
  noSend: {
    ru: 'Кнопка отправки [data-vpc="send"] не видна (display:none, visibility:hidden или нулевой размер) — она должна быть рядом с полем ввода.',
    en: 'The send button [data-vpc="send"] is not visible (display:none, visibility:hidden or zero size) — it must sit next to the input.',
  },
  noAvatar: {
    ru: 'Аватар комнаты [data-vpc="room-avatar"] не виден (display:none или visibility:hidden) — он должен быть на сцене [data-vpc="room-scene"].',
    en: 'The room avatar [data-vpc="room-avatar"] is not visible (display:none or visibility:hidden) — it must be shown in the scene [data-vpc="room-scene"].',
  },
  noPastime: {
    ru: 'Подпись занятия [data-vpc="room-pastime"] осталась пустой после снапшота с занятием «{text}» — bridge пишет в неё текст; не пересоздавай и не очищай её скриптом.',
    en: 'The pastime caption [data-vpc="room-pastime"] stayed empty after a snapshot with pastime "{text}" — the bridge writes text into it; do not recreate or clear it from the skin script.',
  },
  noState2: {
    ru: 'Второй снапшот («печатает» + плашка «ответ на…») не применился за {n} с — скрипт скина, вероятно, падает или блокирует обработку на этих ветках.',
    en: 'The second snapshot (typing + "replying to" bar) was not applied within {n} s — the skin script probably fails or blocks on these branches.',
  },
};

function msg(lang: Lang, key: string, vars: Record<string, string | number> = {}): string {
  let s = MSG[key][lang];
  for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, String(v));
  return s;
}

// Зонд: ES5 без шаблонных литералов — исполняется внутри документа скина
function probeSource(screen: SkinScreen): string {
  return String.raw`
(function () {
  function post(m) { m.vpc = 'probe'; try { parent.postMessage(m, '*'); } catch (e) {} }
  window.addEventListener('error', function (e) {
    var where = e.lineno ? ' (line ' + e.lineno + (e.colno ? ':' + e.colno : '') + ')' : '';
    post({ type: 'error', message: String(e.message || 'unknown error'), where: where });
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e.reason;
    post({ type: 'error', message: 'Unhandled promise rejection: ' + String((r && r.message) || r), where: '' });
  });
  document.addEventListener('securitypolicyviolation', function (e) {
    post({ type: 'csp', directive: String(e.effectiveDirective || e.violatedDirective || ''), uri: String(e.blockedURI || '').slice(0, 200) });
  });
  // Виден ли элемент: есть в раскладке и не visibility:hidden; sized —
  // ещё и ненулевого размера (поле ввода и кнопка, а не декоративный узел)
  function shown(sel, sized) {
    var el = document.querySelector(sel);
    if (!el || el.getClientRects().length === 0 || getComputedStyle(el).visibility === 'hidden') return false;
    if (!sized) return true;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  document.addEventListener('vpc:state', function () {
    setTimeout(function () {
      var box = document.querySelector('[data-vpc-screen="chat"] [data-vpc="messages"]');
      var n = 0;
      if (box) {
        for (var i = 0; i < box.children.length; i++) {
          var tag = box.children[i].tagName;
          if (tag !== 'TEMPLATE' && tag !== 'SCRIPT' && tag !== 'STYLE') n++;
        }
      }
      var root = document.querySelector('[data-vpc-screen="${screen}"]');
      var visible = !!root && root.getClientRects().length > 0 && getComputedStyle(root).visibility !== 'hidden';
      var pastime = document.querySelector('[data-vpc-screen="room"] [data-vpc="room-pastime"]');
      post({
        type: 'applied', messages: n, visible: visible,
        input: shown('[data-vpc-screen="chat"] [data-vpc="input"]', true),
        send: shown('[data-vpc-screen="chat"] [data-vpc="send"]', true),
        avatar: shown('[data-vpc-screen="room"] [data-vpc="room-avatar"]', false),
        pastime: pastime ? String(pastime.textContent || '').trim().length > 0 : false
      });
    }, 60);
  });
})();
`;
}

// Чату нужна непустая лента: без сообщений в снапшоте проверять нечего
const SAMPLE_MESSAGES: NonNullable<SkinStatePayload['messages']> = [
  { id: -1, role: 'user', text: 'Привет! Hello!', time: '12:00' },
  { id: -2, role: 'persona', text: 'Привет. Проверка ленты. Feed check.', time: '12:01' },
];

// Второй снапшот чата: ветки «печатает» и «ответ на…», которые первый не трогает
const SAMPLE_REPLY = { author: 'Проверка · Check', text: 'Ответ на сообщение. Reply check.' };

interface Applied {
  messages: number;
  visible: boolean;
  input: boolean;
  send: boolean;
  avatar: boolean;
  pastime: boolean;
}

export function runSkinSmokeTest(
  html: string,
  screen: SkinScreen,
  state: SkinStatePayload,
  opts: { lang?: Lang; signal?: AbortSignal } = {},
): Promise<SmokeResult> {
  const lang = opts.lang ?? 'ru';
  const payload: SkinStatePayload =
    screen === 'chat' && !state.messages?.length ? { ...state, messages: SAMPLE_MESSAGES } : state;
  const expectMessages = screen === 'chat' ? (payload.messages?.length ?? 0) : 0;
  // Второй снапшот — только чату: «печатает» и плашка ответа живут там
  const second: SkinStatePayload | null =
    screen === 'chat' ? { ...payload, typing: true, reply: payload.reply ?? SAMPLE_REPLY } : null;
  // Подпись занятия проверяем, только если занятие в снапшоте есть
  const pastimeText = screen === 'room' ? (payload.room?.pastimeLabel ?? '').trim() : '';

  let srcdoc: string;
  try {
    // Зонд — сразу после CSP-меты, раньше любых скриптов скина
    srcdoc = prepareSkin(html, screen, { headScript: probeSource(screen) });
  } catch (e) {
    return Promise.resolve({ ok: false, errors: [msg(lang, 'runtime', { msg: String(e) })] });
  }

  return new Promise<SmokeResult>((resolve) => {
    const iframe = document.createElement('iframe');
    iframe.setAttribute('sandbox', 'allow-scripts');
    iframe.setAttribute('aria-hidden', 'true');
    iframe.tabIndex = -1;
    // В пределах окна, но невидимо: вынесенный за экран iframe браузер может
    // притормозить (таймеры/рендер), а размер нужен для проверки видимости
    iframe.style.cssText =
      'position:fixed;left:0;top:0;width:1200px;height:800px;opacity:0;pointer-events:none;z-index:-1;border:0;';

    const errors: string[] = [];
    const seen = new Set<string>();
    const addError = (key: string, text: string) => {
      if (seen.has(key)) return;
      seen.add(key);
      errors.push(text);
    };
    let ready = false;
    let applied: Applied | null = null;
    let secondSent = false;
    let secondApplied = false;
    const timers: number[] = [];
    let done = false;

    const finish = (aborted = false) => {
      if (done) return;
      done = true;
      timers.forEach((id) => window.clearTimeout(id));
      window.removeEventListener('message', onMessage);
      opts.signal?.removeEventListener('abort', onAbort);
      iframe.remove();
      if (!aborted && applied) {
        if (expectMessages > 0 && applied.messages === 0) addError('feed', msg(lang, 'emptyFeed', { n: expectMessages }));
        if (!applied.visible) addError('hidden', msg(lang, 'hidden', { s: screen }));
        // Элементы экрана — только если сам экран виден (иначе это та же ошибка)
        else if (screen === 'chat') {
          if (!applied.input) addError('input', msg(lang, 'noInput'));
          if (!applied.send) addError('send', msg(lang, 'noSend'));
        } else if (screen === 'room') {
          if (!applied.avatar) addError('avatar', msg(lang, 'noAvatar'));
          if (pastimeText && !applied.pastime) addError('pastime', msg(lang, 'noPastime', { text: pastimeText }));
        }
      }
      resolve({ ok: !aborted && errors.length === 0, errors, ...(aborted ? { aborted } : {}) });
    };
    const onAbort = () => finish(true);

    const onMessage = (e: MessageEvent) => {
      if (e.source !== iframe.contentWindow) return;
      const d = e.data;
      if (!d || typeof d.type !== 'string') return;
      if (d.vpc === 'skin') {
        if (d.type === 'ready' && !ready) {
          ready = true;
          iframe.contentWindow?.postMessage({ vpc: 'host', type: 'state', payload }, '*');
          timers.push(
            window.setTimeout(() => {
              if (!applied) {
                addError('state', msg(lang, 'noState', { n: SMOKE_STATE_TIMEOUT_MS / 1000 }));
                finish();
              }
            }, SMOKE_STATE_TIMEOUT_MS),
          );
        } else if (d.type === 'error') {
          // Ошибки window.error bridge дублирует за зондом — ключ по тексту
          const text = String(d.message ?? '').slice(0, 1000);
          addError('e:' + text, msg(lang, 'runtime', { msg: text }));
        }
      } else if (d.vpc === 'probe') {
        if (d.type === 'error') {
          const text = String(d.message ?? '').slice(0, 1000);
          addError('e:' + text, msg(lang, 'runtime', { msg: text + String(d.where ?? '') }));
        } else if (d.type === 'csp') {
          const what = String(d.directive ?? '').startsWith('script') ? 'script/eval' : String(d.directive ?? 'resource');
          addError('c:' + d.directive + d.uri, msg(lang, 'csp', { what, uri: String(d.uri ?? '') }));
        } else if (d.type === 'applied' && !applied) {
          applied = {
            messages: Number(d.messages) || 0,
            visible: d.visible === true,
            input: d.input === true,
            send: d.send === true,
            avatar: d.avatar === true,
            pastime: d.pastime === true,
          };
          if (second) {
            // Ветки «печатает» / «ответ на…»: ошибки скрипта скина на них
            // ловятся тем же зондом в окне SETTLE_MS после второго снапшота
            secondSent = true;
            iframe.contentWindow?.postMessage({ vpc: 'host', type: 'state', payload: second }, '*');
            timers.push(
              window.setTimeout(() => {
                if (!secondApplied) {
                  addError('state2', msg(lang, 'noState2', { n: SMOKE_STATE_TIMEOUT_MS / 1000 }));
                  finish();
                }
              }, SMOKE_STATE_TIMEOUT_MS),
            );
          } else {
            timers.push(window.setTimeout(() => finish(), SMOKE_SETTLE_MS));
          }
        } else if (d.type === 'applied' && secondSent && !secondApplied) {
          secondApplied = true;
          timers.push(window.setTimeout(() => finish(), SMOKE_SETTLE_MS));
        }
      }
    };

    // Второй load того же iframe — скин ушёл на другую страницу
    let loads = 0;
    iframe.addEventListener('load', () => {
      loads += 1;
      if (loads > 1) {
        addError('navigated', msg(lang, 'navigated'));
        finish();
      }
    });

    window.addEventListener('message', onMessage);
    if (opts.signal?.aborted) {
      finish(true);
      return;
    }
    opts.signal?.addEventListener('abort', onAbort);
    timers.push(
      window.setTimeout(() => {
        if (!ready) {
          addError('ready', msg(lang, 'noReady', { n: SMOKE_READY_TIMEOUT_MS / 1000 }));
          finish();
        }
      }, SMOKE_READY_TIMEOUT_MS),
    );
    iframe.srcdoc = srcdoc;
    document.body.appendChild(iframe);
  });
}
