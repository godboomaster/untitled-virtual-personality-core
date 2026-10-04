import { useEffect, useRef, useState } from 'react';
import type { CSSProperties, RefObject } from 'react';
import type { Section } from '../App';
import { useI18n } from '../i18n';

/* Страница «Старт» → «Режим управления»: пошаговое объяснение, как
   управлять браузером/приложениями через чат. Пять шагов с автоподсветкой,
   живое демо переписки (печатается построчно), обозреватель команд с
   фильтром по режиму исполнения, сценарии и разбор типичных проблем.
   Фразы для бота — по локали: у каждой команды русская и английская форма
   (обе разбирает код режима управления, см. scripts/test_cc_english.py),
   демо — реальные шаблоны ответов ядра на языке локали. */

// Как исполняется команда (сверено с needs_confirm в computer_control.py):
// now — всегда сразу; ask — по галочке «спрашивать подтверждение»
// (по умолчанию спрашивает); always — спрашивает всегда, галочка не влияет
type CcMode = 'now' | 'ask' | 'always';
// Какой список «Инструментов» нужен команде (без записи в нём не работает)
type CcList = 'apps' | 'search' | 'tasks';

interface CcCmd {
  text: string; // русская фраза
  en: string; // английская фраза
  mode: CcMode;
  list?: CcList;
}

const now = (text: string, en: string): CcCmd => ({ text, en, mode: 'now' });
const ask = (text: string, en: string, list?: CcList): CcCmd => ({ text, en, mode: 'ask', list });

// Шпаргалка команд: группы — через i18n, фразы — как есть. Нейтральные
// примеры: без конкретных товаров, «…» — место для своего
const CMD_GROUPS: { labelKey: string; cmds: CcCmd[] }[] = [
  {
    labelKey: 'cc.gPages',
    cmds: [
      ask('открой сайт …', 'open …'), ask('включи … на ютубе', 'play … on youtube', 'search'),
      ask('найди … на <сайте>', 'search for … on <site>', 'search'),
      ask('обнови страницу', 'refresh the page'), ask('перезагрузи', 'reload'),
      ask('вернись назад', 'go back'), ask('вперёд', 'go forward'),
      ask('закрой вкладку …', 'close the … tab'), now('перейди на вкладку …', 'switch to the … tab'),
      now('какие вкладки открыты', 'what tabs are open?'),
    ],
  },
  {
    labelKey: 'cc.gInput',
    cmds: [
      ask('нажми …', 'click …'), ask('наведи на …', 'hover over …'),
      ask('введи … в поле …', 'type … into the … field'),
      { text: 'введи … в поле пароля', en: 'type … into the password field', mode: 'always' },
      ask('отправь', 'send'), ask('нажми пробел / энтер / эскейп', 'press space / enter / esc'),
      ask('удали N символов', 'delete N characters'), ask('выстави слайдер … на N', 'set the … slider to N'),
      ask('второй результат', 'open the second result'),
    ],
  },
  {
    labelKey: 'cc.gRead',
    cmds: [
      now('что на странице?', 'what’s on the page?'), now('пришли скриншот', 'send a screenshot'),
      now('покажи всю страницу целиком', 'show me the whole page'),
      now('ещё — следующая партия кадров', 'more — the next batch of shots'),
      now('что в разделе …?', 'what’s in the … section?'), now('прочитай страницу', 'read the page'),
    ],
  },
  {
    labelKey: 'cc.gScroll',
    cmds: [
      ask('пролистай страницу', 'scroll the page'), ask('листай вверх', 'scroll up'), now('стоп', 'stop'),
      now('пролистай до …', 'scroll to …'), now('найди … на странице', 'find … on the page'),
      now('докрути до конца', 'scroll all the way down'), now('докрути до начала', 'scroll all the way up'),
    ],
  },
  {
    labelKey: 'cc.gMedia',
    cmds: [
      ask('пауза', 'pause'), ask('продолжи', 'resume'), ask('тише', 'quieter'),
      ask('громче', 'louder'), ask('без звука', 'mute'),
    ],
  },
  {
    labelKey: 'cc.gZoom',
    cmds: [
      now('увеличь масштаб', 'zoom in'), now('уменьши масштаб', 'zoom out'),
      now('сбрось масштаб', 'reset zoom'),
    ],
  },
  {
    labelKey: 'cc.gCart',
    cmds: [
      ask('убери … из корзины', 'remove … from the cart'), ask('прибавь …', 'add one more …'),
      ask('убавь …', 'remove one … from the cart'),
    ],
  },
  {
    labelKey: 'cc.gMisc',
    cmds: [
      ask('скачай …', 'download …'), ask('закрой окно', 'close the popup'),
      ask('запусти приложение …', 'launch …', 'apps'), ask('сделай …', 'run …', 'tasks'),
      now('почини браузер', 'fix the browser'),
    ],
  },
  {
    labelKey: 'cc.gScenarios',
    cmds: [
      now('начни записывать сценарий …', 'start recording a scenario …'),
      now('сохрани сценарий …', 'save the scenario as …'), now('отмени запись', 'cancel the recording'),
      now('запомни сценарий …', 'remember this scenario as …'),
      now('<название сценария>', '<scenario name>'),
      now('повтори', 'retry'), now('дальше', 'skip'), now('отмена', 'cancel'),
    ],
  },
];

// Что делает фраза — строка под ней в обозревателе (ключ — русская фраза).
// Сверено с разбором команд в computer_control.py
const CMD_DESC: Record<string, { ru: string; en: string }> = {
  'открой сайт …': { ru: 'Открыть сайт в новой вкладке — по имени из списка «Сайты», истории браузера или поиску', en: 'Open a site in a new tab — by a name from the Sites list, browser history or a web search' },
  'включи … на ютубе': { ru: 'Найти на сайте и сразу открыть первый результат', en: 'Search the site and open the first result right away' },
  'найди … на <сайте>': { ru: 'Открыть страницу поиска на сайте с вашим запросом', en: 'Open the site’s search page for your query' },
  'обнови страницу': { ru: 'Перезагрузить открытую вкладку', en: 'Reload the open tab' },
  'перезагрузи': { ru: 'То же — перезагрузить вкладку', en: 'Same — reload the tab' },
  'вернись назад': { ru: 'Шаг назад по истории вкладки', en: 'One step back in the tab’s history' },
  'вперёд': { ru: 'Шаг вперёд по истории вкладки', en: 'One step forward in the tab’s history' },
  'закрой вкладку …': { ru: 'Закрыть вкладку — названную или текущую', en: 'Close a tab — the named one or the current one' },
  'перейди на вкладку …': { ru: 'Переключиться на уже открытую вкладку', en: 'Switch to an already open tab' },
  'какие вкладки открыты': { ru: 'Список вкладок в браузере бота', en: 'List the tabs in the bot’s browser' },
  'нажми …': { ru: 'Нажать кнопку или ссылку по её подписи', en: 'Click a button or link by its label' },
  'наведи на …': { ru: 'Навести курсор — например, чтобы раскрыть меню', en: 'Hover — for example, to open a menu' },
  'введи … в поле …': { ru: 'Ввести текст в поле по его подписи', en: 'Type text into a field by its label' },
  'введи … в поле пароля': { ru: 'Пароль, код, почта или телефон — подтверждение нужно всегда', en: 'A password, code, email or phone — always needs confirmation' },
  'отправь': { ru: 'Нажать Enter в активном поле — отправить сообщение или форму', en: 'Press Enter in the active field — send the message or form' },
  'нажми пробел / энтер / эскейп': { ru: 'Нажать клавишу на странице: пауза, закрыть окно, игра', en: 'Press a key on the page: pause, close a popup, a game' },
  'удали N символов': { ru: 'Стереть последние N символов в поле', en: 'Erase the last N characters in the field' },
  'выстави слайдер … на N': { ru: 'Передвинуть ползунок на значение', en: 'Move a slider to a value' },
  'второй результат': { ru: 'Открыть N-й результат поиска или видео в списке', en: 'Open the Nth search result or video in a list' },
  'что на странице?': { ru: 'Пересказ открытой страницы и скриншот', en: 'A summary of the open page plus a screenshot' },
  'пришли скриншот': { ru: 'Скриншот видимой части страницы', en: 'A screenshot of the visible part of the page' },
  'покажи всю страницу целиком': { ru: 'Вся страница серией скриншотов с оглавлением', en: 'The whole page as a series of screenshots with a table of contents' },
  'ещё — следующая партия кадров': { ru: 'Прислать остальные скриншоты из «всей страницы»', en: 'Send the rest of the “whole page” screenshots' },
  'что в разделе …?': { ru: 'Что лежит в названном разделе страницы', en: 'What is in the named section of the page' },
  'прочитай страницу': { ru: 'Текст страницы — в чат', en: 'The page text — into the chat' },
  'пролистай страницу': { ru: 'Листать вниз, пока не скажете «стоп»', en: 'Keep scrolling down until you say “stop”' },
  'листай вверх': { ru: 'Листать вверх, пока не скажете «стоп»', en: 'Keep scrolling up until you say “stop”' },
  'стоп': { ru: 'Остановить листание', en: 'Stop scrolling' },
  'пролистай до …': { ru: 'Долистать до нужного места и остановиться', en: 'Scroll to the place you name and stop' },
  'найди … на странице': { ru: 'Найти текст на открытой странице и прокрутить к нему', en: 'Find text on the open page and scroll to it' },
  'докрути до конца': { ru: 'В самый низ страницы', en: 'All the way to the bottom' },
  'докрути до начала': { ru: 'В самый верх страницы', en: 'All the way to the top' },
  'пауза': { ru: 'Поставить видео или музыку на паузу', en: 'Pause the video or music' },
  'продолжи': { ru: 'Продолжить воспроизведение', en: 'Resume playback' },
  'тише': { ru: 'Сделать звук тише', en: 'Turn the volume down' },
  'громче': { ru: 'Сделать звук громче', en: 'Turn the volume up' },
  'без звука': { ru: 'Выключить звук', en: 'Mute the sound' },
  'увеличь масштаб': { ru: 'Крупнее — как Ctrl или Cmd и плюс', en: 'Bigger — like Ctrl or Cmd and plus' },
  'уменьши масштаб': { ru: 'Мельче', en: 'Smaller' },
  'сбрось масштаб': { ru: 'Вернуть 100%', en: 'Back to 100%' },
  'убери … из корзины': { ru: 'Удалить товар из корзины', en: 'Remove an item from the cart' },
  'прибавь …': { ru: 'Ещё одну штуку товара в корзину', en: 'One more of an item in the cart' },
  'убавь …': { ru: 'На одну штуку товара меньше', en: 'One fewer of an item in the cart' },
  'скачай …': { ru: 'Скачать файл по ссылке с этой подписью', en: 'Download the file behind a link with that label' },
  'закрой окно': { ru: 'Закрыть всплывающее окно или выпадающий список', en: 'Close a popup or a dropdown' },
  'запусти приложение …': { ru: 'Запустить программу из списка «Приложения»', en: 'Launch a program from the Apps list' },
  'сделай …': { ru: 'Выполнить задачу из списка «Задачи»', en: 'Run a task from the Tasks list' },
  'почини браузер': { ru: 'Показать окно браузера бота — войти на сайт или пройти капчу', en: 'Show the bot’s browser window — to log in or solve a captcha' },
  'начни записывать сценарий …': { ru: 'Начать запись — дальше действуйте как обычно', en: 'Start recording — then act as usual' },
  'сохрани сценарий …': { ru: 'Закончить запись и сохранить под названием', en: 'Stop recording and save it under a name' },
  'отмени запись': { ru: 'Остановить запись без сохранения', en: 'Stop recording without saving' },
  'запомни сценарий …': { ru: 'Собрать сценарий из действий за последние 30 минут', en: 'Build a scenario from the last 30 minutes of actions' },
  '<название сценария>': { ru: 'Запустить сохранённый сценарий', en: 'Run a saved scenario' },
  'повтори': { ru: 'Шаг не вышел — попробовать ещё раз', en: 'A step failed — try again' },
  'дальше': { ru: 'Шаг не вышел — пропустить его', en: 'A step failed — skip it' },
  'отмена': { ru: 'Остановить сценарий', en: 'Stop the scenario' },
};

// Режим фразы с учётом галочки «спрашивать подтверждение»: выключена —
// «ask» исполняется сразу, «always» спрашивает всё равно
const effectiveMode = (c: CcCmd, confirmOn: boolean): CcMode =>
  !confirmOn && c.mode === 'ask' ? 'now' : c.mode;

const MODES: CcMode[] = ['now', 'ask', 'always'];

// Метки режима у фразы: → сразу, ? спросит, ! спросит всегда, ≡ нужен список
const MODE_MARK: Record<CcMode, string> = { now: '→', ask: '?', always: '!' };

// Пять шагов пайплайна: фразы «скажите» и варианты глагола — литеральные
const STEPS: { key: string; say: CcCmd[]; alt?: { ru: string[]; en: string[] } }[] = [
  { key: 'step1', say: [] },
  {
    key: 'step2',
    say: [now('перейди в режим управления', 'enter control mode')],
    alt: {
      ru: ['переключись', 'войди', 'зайди', 'включи', 'активируй'],
      en: ['turn on', 'switch to', 'start', 'enable', 'activate'],
    },
  },
  {
    key: 'step3',
    say: [
      ask('открой ютуб', 'open youtube'), now('что на странице?', 'what’s on the page?'),
      ask('нажми «войти»', 'click “sign in”'),
    ],
  },
  { key: 'step4', say: [] },
  {
    key: 'step5',
    say: [now('выйди из режима управления', 'exit control mode')],
    alt: { ru: ['выключи', 'отключи', 'покинь'], en: ['leave', 'turn off', 'disable', 'quit'] },
  },
];

// Ответы на «выполнить?» — те, что разбирает classify_confirmation
const CONFIRM_YES = { ru: ['да', 'ок', 'давай', 'поехали'], en: ['yes', 'ok', 'sure', 'go ahead', 'do it'] };
const CONFIRM_NO = { ru: ['нет', 'отмена', 'стоп'], en: ['no', 'cancel', 'stop', 'never mind'] };

// Живое демо: реплики бота — шаблоны ядра как есть (lines — русские, en —
// английские из cc_texts), служебные строки (sys) — через i18n, shot —
// вложенный скриншот страницы
type DemoLine =
  | { who: 'you' | 'bot' | 'shot'; text: string }
  | { who: 'sys'; key: string };

const DEMOS: { labelKey: string; lines: DemoLine[]; en: DemoLine[] }[] = [
  {
    labelKey: 'cc.demo1',
    lines: [
      { who: 'you', text: 'перейди в режим управления' },
      { who: 'bot', text: 'Режим управления включён: «открой …», «нажми …», «введи …», сценарии — всё работает. На время режима молчат: напоминания, список дел, инвентарь, обучение. Закончить — «выйди из режима управления».' },
      { who: 'you', text: 'открой ютуб' },
      { who: 'bot', text: 'Открыть youtube.com?' },
      { who: 'you', text: 'да' },
      { who: 'bot', text: 'Готово, открыл youtube.com.' },
      { who: 'you', text: 'что на странице?' },
      { who: 'bot', text: 'Главная youtube.com: строка поиска, лента рекомендаций, слева — «Подписки», «Shorts» и «Библиотека».' },
      { who: 'shot', text: 'Так выглядит страница (youtube.com)' },
    ],
    en: [
      { who: 'you', text: 'enter control mode' },
      { who: 'bot', text: 'Control mode is on: "open …", "click …", "type …", scenarios — all work. While it\'s on, reminders, the todo list, the inventory and lessons are paused. To finish — "exit control mode".' },
      { who: 'you', text: 'open youtube' },
      { who: 'bot', text: 'Open youtube.com?' },
      { who: 'you', text: 'yes' },
      { who: 'bot', text: 'Done: opened youtube.com.' },
      { who: 'you', text: 'what’s on the page?' },
      { who: 'bot', text: 'youtube.com home: a search bar, the recommendations feed, on the left — “Subscriptions”, “Shorts” and “Library”.' },
      { who: 'shot', text: 'This is what the page looks like (youtube.com)' },
    ],
  },
  {
    labelKey: 'cc.demo2',
    lines: [
      { who: 'you', text: 'включи джаз на ютубе' },
      { who: 'bot', text: 'Открыть youtube.com и пройти: джаз?' },
      { who: 'you', text: 'да' },
      { who: 'bot', text: 'Готово, открыл youtube.com и прошёл до «джаз».' },
      { who: 'you', text: 'пауза' },
      { who: 'bot', text: 'Поставить на паузу/продолжить на youtube.com?' },
      { who: 'you', text: 'ок' },
      { who: 'bot', text: 'Готово, поставил на паузу на youtube.com.' },
      { who: 'you', text: 'тише' },
      { who: 'bot', text: 'Уменьшить громкость на youtube.com?' },
      { who: 'you', text: 'давай' },
      { who: 'bot', text: 'Готово, изменил громкость на youtube.com.' },
    ],
    en: [
      { who: 'you', text: 'play jazz on youtube' },
      { who: 'bot', text: 'Open youtube.com and go through: jazz?' },
      { who: 'you', text: 'yes' },
      { who: 'bot', text: 'Done: opened youtube.com and went through to "jazz".' },
      { who: 'you', text: 'pause' },
      { who: 'bot', text: 'Pause/resume on youtube.com?' },
      { who: 'you', text: 'ok' },
      { who: 'bot', text: 'Done: paused the video on youtube.com.' },
      { who: 'you', text: 'quieter' },
      { who: 'bot', text: 'Turn the volume down on youtube.com?' },
      { who: 'you', text: 'sure' },
      { who: 'bot', text: 'Done: turned the volume down on youtube.com.' },
    ],
  },
  {
    labelKey: 'cc.demo3',
    lines: [
      { who: 'you', text: 'начни записывать сценарий утро' },
      { who: 'bot', text: 'Записываю сценарий. Делай действия как обычно — «открой …», «нажми …», «введи …» — всё пойдёт в запись. Закончить: «сохрани сценарий». Отменить: «отмени запись».' },
      { who: 'you', text: 'открой ютуб' },
      { who: 'bot', text: 'Открыть youtube.com?' },
      { who: 'you', text: 'да' },
      { who: 'sys', key: 'cc.demoMore' },
      { who: 'you', text: 'сохрани сценарий' },
      { who: 'bot', text: 'Записал сценарий «утро» — 4 шага. Теперь просто скажи «утро».' },
      { who: 'sys', key: 'cc.demoLater' },
      { who: 'you', text: 'утро' },
      { who: 'bot', text: 'Открыл youtube.com.' },
      { who: 'bot', text: 'Стоп: не нашёл «Подписки» на странице. Скажи «повтори», «дальше» (пропустить) или «отмена».' },
      { who: 'you', text: 'э?' },
      { who: 'bot', text: 'Стою на сбойном шаге. Скажи «повтори», «дальше» (пропустить) или «отмена».' },
      { who: 'you', text: 'повтори' },
      { who: 'bot', text: 'Нажал «Подписки» на youtube.com.' },
      { who: 'bot', text: 'Сценарий «утро» завершён.' },
    ],
    en: [
      { who: 'you', text: 'start recording a scenario morning' },
      { who: 'bot', text: 'Recording a scenario. Do things as usual — "open …", "click …", "type …" — it all goes into the recording. To finish: "save the scenario", optionally with a name. To cancel: "cancel the recording".' },
      { who: 'you', text: 'open youtube' },
      { who: 'bot', text: 'Open youtube.com?' },
      { who: 'you', text: 'yes' },
      { who: 'sys', key: 'cc.demoMore' },
      { who: 'you', text: 'save the scenario' },
      { who: 'bot', text: 'Saved the scenario "morning" — 4 steps. Now just say "morning".' },
      { who: 'sys', key: 'cc.demoLater' },
      { who: 'you', text: 'morning' },
      { who: 'bot', text: 'Done: opened youtube.com.' },
      { who: 'bot', text: 'Stopped: couldn\'t find "Subscriptions" on the page. Say "retry", "skip" or "cancel".' },
      { who: 'you', text: 'huh?' },
      { who: 'bot', text: 'I\'m stuck on a failed step. Say "retry", "skip" or "cancel".' },
      { who: 'you', text: 'retry' },
      { who: 'bot', text: 'Done: clicked "Subscriptions" on youtube.com.' },
      { who: 'bot', text: 'The "morning" scenario is finished.' },
    ],
  },
];

const FAQ_COUNT = 6;

// Длина «печатаемой» части строки: служебные и скриншот появляются целиком
const lineLen = (l: DemoLine): number => (l.who === 'you' || l.who === 'bot' ? l.text.length : 0);

// Пользователь просил меньше движения: демо и шаги сразу в финальном виде
const prefersReducedMotion = (): boolean =>
  typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

// Виден ли блок на экране: вне вьюпорта автопрокрутка и печать стоят
function useInView(ref: RefObject<HTMLElement | null>): boolean {
  const [inView, setInView] = useState(() => typeof IntersectionObserver === 'undefined');
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(([e]) => setInView(e.isIntersecting), { threshold: 0.2 });
    io.observe(el);
    return () => io.disconnect();
  }, [ref]);
  return inView;
}

// Кликабельная фраза-команда: клик копирует текст, метки — режим исполнения
function CmdChip({
  cmd,
  copied,
  onCopy,
  delay,
  bare,
}: {
  cmd: CcCmd;
  copied: boolean;
  onCopy: (text: string) => void;
  delay?: number;
  bare?: boolean; // слово-ответ («да», «повтори») — без меток режима
}) {
  const { t, lang } = useI18n();
  const text = lang === 'en' ? cmd.en : cmd.text;
  const title = [
    bare ? '' : t(`cc.mode_${cmd.mode}`),
    cmd.list ? `${t('cc.modeList')}: ${t(`cc.list_${cmd.list}`)}` : '',
    t('cc.copyHint'),
  ]
    .filter(Boolean)
    .join(' · ');
  return (
    <button
      type="button"
      className={`start-control-cmd ${copied ? 'is-copied' : ''}`}
      style={delay !== undefined ? ({ '--d': `${delay}ms` } as CSSProperties) : undefined}
      title={title}
      onClick={() => onCopy(text)}
    >
      <span className="start-control-cmd-text">{text}</span>
      {copied ? (
        <span className="start-control-mark is-ok">✓</span>
      ) : bare ? null : (
        <>
          <span className="start-control-mark" data-mode={cmd.mode}>{MODE_MARK[cmd.mode]}</span>
          {cmd.list && <span className="start-control-mark" data-mode="list">≡</span>}
        </>
      )}
    </button>
  );
}

export default function StartControl({ onNavigate }: { onNavigate: (s: Section) => void }) {
  const { t, lang } = useI18n();
  const en = lang === 'en';
  const [reduced] = useState(prefersReducedMotion);

  // Копирование фраз: ✓ вспыхивает только после успешной записи в буфер
  const [copied, setCopied] = useState<string | null>(null);
  const copyTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(copyTimer.current), []);
  const copy = (uid: string, text: string) => {
    const flash = () => {
      setCopied(uid);
      window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(null), 1400);
    };
    try {
      navigator.clipboard.writeText(text).then(flash, () => {});
    } catch {
      /* буфер недоступен (не https, iframe) — копирования нет, молчим */
    }
  };
  // scope — место чипа на странице: одна фраза встречается в нескольких блоках
  const chip = (c: CcCmd, scope: string, opts: { delay?: number; bare?: boolean } = {}) => {
    const uid = `${scope}:${en ? c.en : c.text}`;
    return (
      <CmdChip
        key={c.text}
        cmd={c}
        copied={copied === uid}
        onCopy={(text) => copy(uid, text)}
        delay={opts.delay}
        bare={opts.bare}
      />
    );
  };
  // Слово-ответ боту (подтверждение, управление прогоном сценария)
  const word = (w: string, scope: string) => chip(now(w, w), scope, { bare: true });

  // ── Пайплайн шагов: автоподсветка по окончании таймер-полоски ──
  // (CSS-анимация; пауза — animation-play-state при наведении/фокусе)
  const pipeRef = useRef<HTMLDivElement>(null);
  const pipeInView = useInView(pipeRef);
  const [active, setActive] = useState(0);
  const [hold, setHold] = useState(false);
  const cmdRef = useRef<HTMLDivElement>(null);
  const step = STEPS[active];

  // ── Живое демо: строки печатаются по очереди, по кругу ──
  const termRef = useRef<HTMLDivElement>(null);
  const termBodyRef = useRef<HTMLDivElement>(null);
  const termInView = useInView(termRef);
  const [demo, setDemo] = useState(0);
  const [shown, setShown] = useState(0); // полностью выведенных строк
  const [typed, setTyped] = useState(0); // символов текущей строки
  const lines = en ? DEMOS[demo].en : DEMOS[demo].lines;

  useEffect(() => {
    if (reduced || !termInView) return;
    let delay: number;
    let next: () => void;
    if (shown >= lines.length) {
      // Диалог дописан — пауза на прочтение и заново
      delay = 4200;
      next = () => {
        setShown(0);
        setTyped(0);
      };
    } else {
      const line = lines[shown];
      const len = lineLen(line);
      const you = line.who === 'you';
      if (typed < len) {
        // Пользователь «набирает» по букве, бот выдаёт текст быстрее
        delay = typed === 0 ? (you ? 650 : 950) : you ? 55 : 14;
        next = () => setTyped(Math.min(len, typed + (you ? 1 : 3)));
      } else {
        delay = line.who === 'sys' || line.who === 'shot' ? 800 : 320;
        next = () => {
          setShown(shown + 1);
          setTyped(0);
        };
      }
    }
    const timer = window.setTimeout(next, delay);
    return () => window.clearTimeout(timer);
  }, [lines, shown, typed, reduced, termInView]);

  // Новая строка — прокручиваем окно демо вниз (саму страницу не трогаем)
  // Если пользователь отлистал выше — не дёргаем, пока сам не вернётся вниз
  useEffect(() => {
    const body = termBodyRef.current;
    if (body && body.scrollHeight - body.scrollTop - body.clientHeight < 48) body.scrollTop = body.scrollHeight;
  }, [shown, typed]);
  useEffect(() => {
    const body = termBodyRef.current;
    if (body) body.scrollTop = 0;
  }, [demo]);

  const playDemo = (i: number) => {
    setDemo(i);
    setShown(0);
    setTyped(0);
  };

  const visible = reduced ? lines.length : shown;
  const current = !reduced && shown < lines.length ? lines[shown] : undefined;

  // ── Обозреватель команд: раздел слева, его фразы справа; переключатель
  // показывает режим фраз при включённом и выключенном подтверждении ──
  const [group, setGroup] = useState(0);
  const [confirmOn, setConfirmOn] = useState(true);
  const total = CMD_GROUPS.reduce((n, g) => n + g.cmds.length, 0);
  const cur = CMD_GROUPS[group];

  // ── Разбор проблем: открыт один пункт ──
  const [faqOpen, setFaqOpen] = useState<number | null>(null);

  const renderLine = (l: DemoLine, i: number, text?: string) => {
    if (l.who === 'sys') {
      return (
        <div key={i} className="start-control-line is-sys">
          {t(l.key)}
        </div>
      );
    }
    if (l.who === 'shot') {
      return (
        <div key={i} className="start-control-line is-shot">
          <span className="start-control-shot" aria-hidden="true">
            <i /><i /><i />
          </span>
          <span className="start-control-shot-cap">▣ {l.text}</span>
        </div>
      );
    }
    return (
      <div key={i} className={`start-control-line is-${l.who}`}>
        <span className="who">{l.who === 'you' ? t('cc.demoYou') : t('cc.demoBot')} ›</span>
        <span className="txt">{text ?? l.text}</span>
      </div>
    );
  };

  return (
    <div id="start-control" className="start-control">
      <div className="home-block-head">
        <span className="home-block-title">{t('cc.title')}</span>
        <span className="home-block-num">02 / CONTROL</span>
      </div>

      {/* Что это и кому доступно */}
      <div className="start-control-intro bracketed">
        <div className="corner tl plus" />
        <div className="corner br" />
        <div className="start-control-eyebrow">MANUAL // REMOTE CONTROL</div>
        <p className="start-control-lead">{t('cc.lead')}</p>
        <div className="start-control-meta">
          <span className="badge badge--active">WEB CHAT</span>
          <span className="start-control-access">
            <b>{t('cc.accessLabel')}</b> {t('cc.access')}
          </span>
        </div>
      </div>

      {/* Пять шагов: автоподсветка, пауза при наведении/фокусе, клик — выбрать */}
      <div
        ref={pipeRef}
        className={`start-control-pipe ${hold || !pipeInView ? 'is-held' : ''}`}
        style={{ '--p': active / (STEPS.length - 1) } as CSSProperties}
        onPointerEnter={(e) => e.pointerType === 'mouse' && setHold(true)}
        onPointerLeave={(e) => e.pointerType === 'mouse' && setHold(false)}
        onFocus={() => setHold(true)}
        onBlur={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setHold(false);
        }}
      >
        <div className="start-control-pipe-head">
          <span>{t('cc.pipeTitle')}</span>
          <span className="start-control-pipe-state">{hold ? t('cc.pipeHeld') : t('cc.pipeAuto')}</span>
        </div>
        <div className="start-control-steps" role="tablist">
          <div className="start-control-track" aria-hidden="true">
            <i />
          </div>
          {STEPS.map((s, i) => (
            <button
              key={s.key}
              type="button"
              role="tab"
              aria-selected={i === active}
              aria-controls="start-control-stage"
              className={`start-control-step ${i === active ? 'is-active' : ''} ${i < active ? 'is-done' : ''}`}
              onClick={() => setActive(i)}
            >
              <span className="start-control-node" aria-hidden="true" />
              <span className="start-control-step-num">{String(i + 1).padStart(2, '0')}</span>
              <span className="start-control-step-title">{t(`cc.${s.key}Title`)}</span>
              {i === active && (
                <span className="start-control-step-timer" aria-hidden="true">
                  <i
                    onAnimationEnd={(e) => {
                      if (e.target === e.currentTarget && !reduced) setActive((a) => (a + 1) % STEPS.length);
                    }}
                  />
                </span>
              )}
            </button>
          ))}
        </div>

        {/* Карточка активного шага — remount по key даёт анимацию смены */}
        <div id="start-control-stage" key={step.key} className="start-control-stage" role="tabpanel">
          <div className="start-control-stage-text">
            <div className="start-control-stage-num">
              {String(active + 1).padStart(2, '0')} / {String(STEPS.length).padStart(2, '0')}
            </div>
            <h3>{t(`cc.${step.key}Title`)}</h3>
            <p>{t(`cc.${step.key}Body`)}</p>
            {active === 3 && <p className="start-control-off-note">{t('cc.step4Off')}</p>}
            {active === 0 && (
              <div className="start-control-path">
                {t('cc.step1Path')
                  .split(' → ')
                  .map((p, i) => (
                    <span key={i}>{p}</span>
                  ))}
              </div>
            )}
          </div>
          <div className="start-control-say">
            <div className="start-control-say-label">
              {active === 0 ? t('cc.step1Where') : active === 3 ? t('cc.step4Reply') : t('cc.sayLabel')}
            </div>
            {active === 0 && (
              <>
                <div className="start-control-toggles">
                  <span>☑ {t('cc.step1Confirm')}</span>
                  <span>☑ {t('cc.step1Click')}</span>
                </div>
                <button type="button" className="btn btn--primary" onClick={() => onNavigate('chat')}>
                  {t('cc.step1Cta')}
                </button>
              </>
            )}
            {active === 3 && (
              <>
                <div className="start-control-q">
                  {en ? 'Click "Sign in" on example.com?' : 'Нажать «Войти» на example.com?'}
                </div>
                {/* Две ветки ответа: «выполнить» — сплошная рамка и закрашенный ✓,
                    «отменить» — пунктир и контурный ✕ (палитра монохромная) */}
                <div className="start-control-yn">
                  <div className="start-control-yn-opt start-control-yn-opt--yes">
                    <div className="start-control-yn-head">
                      <span className="start-control-yn-icon" aria-hidden="true">✓</span>
                      <span className="start-control-yn-label">{t('cc.step4Yes')}</span>
                    </div>
                    <div className="start-control-chips">{CONFIRM_YES[en ? 'en' : 'ru'].map((w) => word(w, 'yes'))}</div>
                  </div>
                  <div className="start-control-yn-opt start-control-yn-opt--no">
                    <div className="start-control-yn-head">
                      <span className="start-control-yn-icon" aria-hidden="true">✕</span>
                      <span className="start-control-yn-label">{t('cc.step4No')}</span>
                    </div>
                    <div className="start-control-chips">{CONFIRM_NO[en ? 'en' : 'ru'].map((w) => word(w, 'no'))}</div>
                  </div>
                </div>
              </>
            )}
            {step.say.length > 0 && <div className="start-control-chips">{step.say.map((c) => chip(c, step.key))}</div>}
            {step.alt && (
              <div className="start-control-alt">
                {t('cc.altLabel')} {step.alt[en ? 'en' : 'ru'].join(' / ')}
              </div>
            )}
            {active === 2 && (
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => cmdRef.current?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' })}
              >
                {t('cc.step3Jump')}
              </button>
            )}
          </div>
        </div>
      </div>

      <div className="start-control-split">
        {/* Живое демо переписки */}
        <div ref={termRef} className="home-term start-control-term bracketed">
          <div className="corner tl" />
          <div className="corner tr" />
          <div className="corner bl" />
          <div className="corner br" />
          <div className="home-term-titlebar">
            <span className="lights"><i /><i /><i /></span>
            <span>vpc-chat — control — web</span>
            <span className="start-control-rec">● LIVE</span>
          </div>
          <div className="start-control-demo-tabs">
            {/* tablist без обёртки в раскладке: вкладки и «повтор» — один flex-ряд */}
            <div role="tablist" style={{ display: 'contents' }}>
            {DEMOS.map((d, i) => (
              <button
                key={d.labelKey}
                type="button"
                role="tab"
                aria-selected={i === demo}
                className={`start-control-demo-tab ${i === demo ? 'is-active' : ''}`}
                onClick={() => playDemo(i)}
              >
                {String.fromCharCode(97 + i)}) {t(d.labelKey)}
              </button>
            ))}
            </div>
            <button type="button" className="start-control-demo-tab is-replay" onClick={() => playDemo(demo)}>
              {t('cc.demoReplay')}
            </button>
          </div>
          <div ref={termBodyRef} className="start-control-term-body" aria-live="off">
            {lines.slice(0, visible).map((l, i) => renderLine(l, i))}
            {current &&
              (typed > 0 && (current.who === 'you' || current.who === 'bot') ? (
                <div className={`start-control-line is-${current.who}`}>
                  <span className="who">{current.who === 'you' ? t('cc.demoYou') : t('cc.demoBot')} ›</span>
                  <span className="txt">
                    {current.text.slice(0, typed)}
                    <span className="home-term-cursor" />
                  </span>
                </div>
              ) : current.who === 'bot' || current.who === 'shot' ? (
                <div className="start-control-line is-bot">
                  <span className="who">{t('cc.demoBot')} ›</span>
                  <span className="start-control-typing"><i /><i /><i /></span>
                </div>
              ) : current.who === 'you' ? (
                <div className="start-control-line is-you">
                  <span className="who">{t('cc.demoYou')} ›</span>
                  <span className="txt"><span className="home-term-cursor" /></span>
                </div>
              ) : null)}
          </div>
        </div>
      </div>

      {/* Сценарии: что это, как записать (два способа), как запустить, что
          делать при сбое шага. Пример — «заказ пиццы» на всех шагах */}
      <div className="start-control-card start-control-sc">
        <div className="start-control-card-title">{t('cc.scTitle')}</div>
        <p className="start-control-sc-lead">{t('cc.scLead')}</p>

        <div className="start-control-sc-grid">
          <section className="start-control-sc-block">
            <div className="start-control-sc-head">
              <span className="start-control-sc-num">01</span>
              <span className="start-control-sc-label">{t('cc.scRecTitle')}</span>
            </div>
            <div className="start-control-sc-way">
              <div className="start-control-sc-way-label">{t('cc.scWay1')}</div>
              <div className="start-control-chips">
                {chip(now('запомни сценарий заказ пиццы', 'remember this scenario as order pizza'), 'sc')}
              </div>
              <p>{t('cc.scWay1Body')}</p>
            </div>
            <div className="start-control-sc-way">
              <div className="start-control-sc-way-label">{t('cc.scWay2')}</div>
              <ol className="start-control-sc-flow">
                <li>{chip(now('начни записывать сценарий', 'start recording a scenario'), 'sc')}</li>
                <li className="is-do">{t('cc.scWay2Do')}</li>
                <li>{chip(now('сохрани сценарий заказ пиццы', 'save the scenario as order pizza'), 'sc')}</li>
              </ol>
              <div className="start-control-sc-inline">
                <span>{t('cc.scWay2Cancel')}</span>
                {chip(now('отмени запись', 'cancel the recording'), 'sc')}
              </div>
            </div>
            <p className="start-control-sc-tip">{t('cc.scAuto')}</p>
          </section>

          <section className="start-control-sc-block">
            <div className="start-control-sc-head">
              <span className="start-control-sc-num">02</span>
              <span className="start-control-sc-label">{t('cc.scRunTitle')}</span>
            </div>
            <div className="start-control-chips">{chip(now('заказ пиццы', 'order pizza'), 'sc')}</div>
            <p>{t('cc.scRunBody')}</p>
            <ul className="start-control-sc-facts">
              <li><b>→</b>{t('cc.scRunSteps')}</li>
              <li><b>?</b>{t('cc.scRunAsk')}</li>
              <li><b>*</b>{t('cc.scRunSecret')}</li>
              <li><b>!</b>{t('cc.scRunPay')}</li>
            </ul>
          </section>

          <section className="start-control-sc-block">
            <div className="start-control-sc-head">
              <span className="start-control-sc-num">03</span>
              <span className="start-control-sc-label">{t('cc.scFailTitle')}</span>
            </div>
            <p>{t('cc.scFailBody')}</p>
            <div className="start-control-sc-fail">
              <div>{word(en ? 'retry' : 'повтори', 'sc')}<span>{t('cc.scRetry')}</span></div>
              <div>{word(en ? 'skip' : 'дальше', 'sc')}<span>{t('cc.scSkip')}</span></div>
              <div>{word(en ? 'cancel' : 'отмена', 'sc')}<span>{t('cc.scCancel')}</span></div>
            </div>
          </section>
        </div>

        <div className="start-control-note">{t('cc.scNote')}</div>
      </div>

      {/* Обозреватель команд: разделы слева, фразы раздела — таблицей справа.
          Режим — словами; переключатель «Подтверждение» показывает, что
          останется с вопросом, если галочку снять */}
      <div ref={cmdRef} className="start-control-explorer">
        <div className="start-control-filter-row">
          <span className="start-control-sub">{t('cc.cmdTitle')}</span>
          <div className="start-control-ex-confirm">
            <span>{t('cc.confirmLabel')}</span>
            <div className="start-control-filters" role="group" aria-label={t('cc.confirmLabel')}>
              {[true, false].map((v) => (
                <button
                  key={String(v)}
                  type="button"
                  aria-pressed={confirmOn === v}
                  className={`start-control-filter ${confirmOn === v ? 'is-active' : ''}`}
                  onClick={() => setConfirmOn(v)}
                >
                  {t(v ? 'cc.confirmOn' : 'cc.confirmOff')}
                </button>
              ))}
            </div>
          </div>
          <span className="start-control-count">{t('cc.cmdCount', { n: total })}</span>
        </div>
        <p className={`start-control-ex-hint ${confirmOn ? '' : 'is-off'}`}>
          {t(confirmOn ? 'cc.cmdHintOn' : 'cc.cmdHintOff')}
        </p>

        <div className="start-control-ex">
          <nav className="start-control-ex-nav" aria-label={t('cc.cmdTitle')}>
            {CMD_GROUPS.map((g, i) => (
              <button
                key={g.labelKey}
                type="button"
                aria-pressed={i === group}
                className={`start-control-ex-tab ${i === group ? 'is-active' : ''}`}
                onClick={() => setGroup(i)}
              >
                <span>{t(g.labelKey)}</span>
                <span className="start-control-ex-n">{String(g.cmds.length).padStart(2, '0')}</span>
              </button>
            ))}
          </nav>

          {/* key={group}: смена раздела перемонтирует панель — строки появляются заново */}
          <div key={group} className="start-control-ex-panel">
            <div className="start-control-ex-head">
              <h4>{t(cur.labelKey)}</h4>
              <p>{t(`${cur.labelKey}Desc`)}</p>
            </div>
            {/* Фразы — группами по режиму: «сразу», «требует подтверждения», «всегда требует».
                Выключенное подтверждение переносит «требует» в «сразу» */}
            <div className="start-control-ex-blocks">
              {MODES.map((m) => ({ mode: m, cmds: cur.cmds.filter((c) => effectiveMode(c, confirmOn) === m) }))
                .filter((b) => b.cmds.length > 0)
                .map((b) => (
                  <section key={b.mode} className="start-control-ex-block">
                    <header className="start-control-ex-block-head">
                      <span className="start-control-ex-mode" data-mode={b.mode}>{t(`cc.exMode_${b.mode}`)}</span>
                      <span className="start-control-ex-block-desc">{t(`cc.exModeDesc_${b.mode}`)}</span>
                    </header>
                    <ul className="start-control-ex-phrases">
                      {b.cmds.map((c, i) => {
                        const text = en ? c.en : c.text;
                        const uid = `ex:${text}`;
                        const changed = b.mode !== c.mode;
                        return (
                          <li key={c.text} style={{ '--d': `${i * 25}ms` } as CSSProperties}>
                            <button
                              type="button"
                              className={`start-control-ex-phrase ${copied === uid ? 'is-copied' : ''} ${changed ? 'is-changed' : ''}`}
                              title={c.list ? t('cc.exList', { list: t(`cc.list_${c.list}`) }) : t('cc.copyHint')}
                              onClick={() => copy(uid, text)}
                            >
                              <span className="start-control-ex-line">
                                <span className="start-control-ex-text">{text}</span>
                                {c.list && <span className="start-control-ex-tag">{t(`cc.list_${c.list}`)}</span>}
                                <span className="start-control-ex-copy" aria-hidden="true">
                                  {copied === uid ? `✓ ${t('cc.copied')}` : t('cc.copy')}
                                </span>
                              </span>
                              {CMD_DESC[c.text] && (
                                <span className="start-control-ex-desc">{CMD_DESC[c.text][en ? 'en' : 'ru']}</span>
                              )}
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  </section>
                ))}
            </div>
          </div>
        </div>
        <p className="start-control-ex-foot">{t('cc.cmdNote')}</p>
      </div>

      {/* Если что-то не так: аккордеон с раскрытием через grid-rows */}
      <div className="start-control-faq">
        <div className="start-control-sub">{t('cc.faqTitle')}</div>
        {Array.from({ length: FAQ_COUNT }, (_, i) => {
          const open = faqOpen === i;
          return (
            <div key={i} className={`start-control-faq-item ${open ? 'is-open' : ''}`}>
              <button
                type="button"
                className="start-control-faq-q"
                aria-expanded={open}
                aria-controls={`start-control-faq-${i}`}
                onClick={() => setFaqOpen(open ? null : i)}
              >
                <span className="start-control-faq-n">E{String(i + 1).padStart(2, '0')}</span>
                <span className="start-control-faq-text">{t(`cc.faq${i + 1}Q`)}</span>
                <span className="start-control-faq-sign" aria-hidden="true">{open ? '−' : '+'}</span>
              </button>
              <div id={`start-control-faq-${i}`} className="start-control-faq-a" aria-hidden={!open}>
                <div>
                  <p>{t(`cc.faq${i + 1}A`)}</p>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
