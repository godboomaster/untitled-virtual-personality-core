import { useEffect, useId, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent } from 'react';
import type { Section } from '../App';
import { useI18n } from '../i18n';
import OllamaModels from './OllamaModels';
import { requestDossierTab } from '../chatNavStore';

/* Обучение на странице «Старт»: маршруты (консоль, спецфункции, Telegram) —
   вертикальная лента шагов с отметками «пройдено», копированием команд и
   переходами в разделы, рядом — живой демо-диалог. Блок показывает свой набор
   маршрутов (trackIds), поэтому на странице их два: обучение и Telegram.
   Установка и запуск ядра сюда не входят. Отметки персистятся в localStorage
   (vpc-guide-done); prefers-reduced-motion — сразу финальное состояние. */

interface StartGuideProps {
  onNavigate: (s: Section) => void;
  trackIds: TrackId[]; // какие маршруты показывать в этом блоке
  titleKey: string;
  num: string; // номер блока в шапке, «01 / BRIEFING»
  leadKey: string;
  allDoneKey: string;
  id?: string; // якорь блока для прокрутки из шапки страницы
}

export type TrackId = 'console' | 'features' | 'telegram';

// Код-чип: литерал (команды, пути) или ключ i18n (фразы обычной речью);
// hint — ключ пояснения, тогда чипы рисуются списком «команда — что делает»
interface Chip {
  code: string;
  i18n?: boolean;
  hint?: string;
}

interface Step {
  id: string;
  chips?: Chip[];
  chipsEn?: Chip[]; // свои примеры для английской локали (фразы бота другие)
  nav?: Section;
  here?: boolean; // шаг про текущий раздел — вместо кнопки метка «вы здесь»
  warn?: string; // ключ i18n предупреждения под описанием шага
  anchor?: string; // id блока на этой же странице — кнопка прокрутки к нему
  dossier?: 'initiative' | 'settings'; // кнопка «Досье → вкладка» (чат последней активной персоны)
  models?: boolean; // сравнение локальных моделей Ollama (шаг «Провайдеры: Ollama»)
}

// Реплика демо-диалога: op — оператор, bot — персона, sys — служебная
interface DialogLine {
  who: 'op' | 'bot' | 'sys';
  text: string;
  i18n?: boolean;
}

interface Track {
  id: TrackId;
  index: string;
  steps: Step[];
  window: string; // подпись титульной строки демо-окна
  dialog: DialogLine[];
}

// Маршруты обучения. Команды Telegram и русские фразы — литералы; через
// i18n — плейсхолдеры аргументов и фразы, у которых английский вариант
// дословный. Список дел и инвентарь по-английски устроены иначе — у них
// chipsEn: дела — со словом «todo» (по нему модель получает список и
// инструкцию маркеров), инвентарь — фразы, которые основная модель
// разбирает сама (русские триггеры его не ловят). Правила и имя — свои
// английские формы (_CORRECTION_HINT_EN_RE, _extract_alias в bot_instance)
const TRACKS: Track[] = [
  {
    id: 'console',
    index: 'T_01',
    steps: [
      { id: 'persona', nav: 'personas' },
      { id: 'api', nav: 'settings' },
      { id: 'web', nav: 'settings', chips: [{ code: 'guide.codeFixBrowser', i18n: true }] },
      {
        id: 'ollama',
        nav: 'settings',
        warn: 'guide.console.ollamaWarn',
        chips: [{ code: 'curl -fsSL https://ollama.com/install.sh | sh', hint: 'guide.hOllamaLinux' }],
        models: true,
      },
      { id: 'talk', nav: 'chat', warn: 'guide.console.talkWarn' },
      { id: 'start', here: true },
      { id: 'home', nav: 'home' },
      { id: 'chat', nav: 'chat' },
      { id: 'room', nav: 'room' },
    ],
    window: 'console — chat — persona',
    dialog: [
      { who: 'op', text: 'guide.dlgConAsk', i18n: true },
      { who: 'sys', text: 'guide.dlgConWarm', i18n: true },
      { who: 'bot', text: 'guide.dlgConAnswer', i18n: true },
      { who: 'sys', text: 'guide.dlgConHint', i18n: true },
    ],
  },
  {
    id: 'features',
    index: 'T_02',
    steps: [
      {
        id: 'remind',
        warn: 'guide.features.modeWarn',
        chips: [
          { code: 'guide.phrRemindIn', i18n: true },
          { code: 'guide.phrRemindMinutes', i18n: true },
          { code: 'guide.phrRemindAt', i18n: true },
          { code: 'guide.phrRemindTomorrow', i18n: true },
          { code: 'guide.phrDaily', i18n: true },
          { code: 'guide.phrMondays', i18n: true },
          { code: 'guide.phrFridays', i18n: true },
        ],
      },
      {
        id: 'move',
        chips: [
          { code: 'guide.phrPostpone', i18n: true },
          { code: 'guide.phrSnooze', i18n: true },
          { code: 'guide.phrShift', i18n: true },
        ],
      },
      {
        id: 'todo',
        chips: [
          { code: '[запиши] купить хлеб', hint: 'guide.hAdd' },
          { code: '[добавь в список] сходить в аптеку', hint: 'guide.hAdd' },
          { code: 'что у меня в списке дел?', hint: 'guide.hShow' },
          { code: '[покажи список дел] на завтра', hint: 'guide.hShowDay' },
          { code: '[вычеркни пункт] 2', hint: 'guide.hByNum' },
          { code: '[убери из списка] …', hint: 'guide.hRemove' },
          { code: 'сделано', hint: 'guide.hDone' },
        ],
        chipsEn: [
          { code: '[add to my todo list:] buy bread', hint: 'guide.hAdd' },
          { code: '[todo:] book tickets', hint: 'guide.hAdd' },
          { code: 'what’s on my todo list?', hint: 'guide.hShow' },
          { code: 'cross off item 2 on my todo list', hint: 'guide.hByNum' },
        ],
      },
      {
        id: 'inventory',
        chips: [
          { code: '[держи] яблоко', hint: 'guide.hGive' },
          { code: '[возьми] зонт', hint: 'guide.hGive' },
          { code: '[добавь в инвентарь] нож', hint: 'guide.hGive' },
          { code: '[надень] шлем', hint: 'guide.hEquip' },
          { code: '[сними] доспехи', hint: 'guide.hUnequip' },
          { code: '[выбрось] меч', hint: 'guide.hRemove' },
          { code: '[удали из инвентаря] нож', hint: 'guide.hRemove' },
        ],
        chipsEn: [
          { code: '[here’s] an apple', hint: 'guide.hGive' },
          { code: '[take] this umbrella', hint: 'guide.hGive' },
          { code: '[put on] the helmet', hint: 'guide.hEquip' },
          { code: '[take off] the armor', hint: 'guide.hUnequip' },
          { code: '[throw away] the sword', hint: 'guide.hRemove' },
        ],
      },
      {
        id: 'learning',
        chips: [
          { code: 'guide.phrTeach', i18n: true },
          { code: 'guide.phrWantLearn', i18n: true },
          { code: 'guide.phrOnceDay', i18n: true },
          { code: 'guide.phrEvery2h', i18n: true },
          { code: 'guide.phrThriceWeek', i18n: true },
        ],
      },
      {
        id: 'rules',
        chips: [
          { code: '[запомни:] я не пью кофе', hint: 'guide.hRule' },
          { code: '[неправильно], я имел в виду …', hint: 'guide.hRule' },
          { code: '[не говори так] — обращайся ко мне на «вы»', hint: 'guide.hRule' },
          { code: '[не называй меня] …', hint: 'guide.hRule' },
          { code: '[зови меня] <untitled>', hint: 'guide.hAlias' },
        ],
        chipsEn: [
          { code: '[remember:] I don’t drink coffee', hint: 'guide.hRule' },
          { code: '[that’s wrong, I meant] …', hint: 'guide.hRule' },
          { code: '[don’t say it like that] — talk to me formally', hint: 'guide.hRule' },
          { code: '[don’t call me] …', hint: 'guide.hRule' },
          { code: '[call me] <untitled>', hint: 'guide.hAlias' },
        ],
      },
      { id: 'search', chips: [{ code: '/web', hint: 'guide.cmdWeb' }] },
      { id: 'files', chips: [{ code: '/files', hint: 'guide.cmdFiles' }] },
      { id: 'initiative', dossier: 'initiative' },
      { id: 'rhythm', dossier: 'settings' },
      { id: 'control', anchor: 'start-control' },
    ],
    window: 'console — chat — persona',
    dialog: [
      { who: 'op', text: 'guide.dlgFeatAsk', i18n: true },
      { who: 'bot', text: 'guide.dlgFeatOk', i18n: true },
      { who: 'op', text: 'guide.dlgFeatSnooze', i18n: true },
      { who: 'bot', text: 'guide.dlgFeatSnoozed', i18n: true },
      { who: 'sys', text: '— 16:50 —' },
      { who: 'bot', text: 'guide.dlgFeatPing', i18n: true },
    ],
  },
  {
    id: 'telegram',
    index: 'TG',
    steps: [
      { id: 'menu', chips: [{ code: '/' }] },
      { id: 'chats' },
      {
        id: 'basic',
        chips: [
          { code: '/start', hint: 'guide.cmdStart' },
          { code: '/help', hint: 'guide.cmdHelp' },
          { code: '/stats', hint: 'guide.cmdStats' },
          { code: '/relations', hint: 'guide.cmdRelations' },
          { code: '/last', hint: 'guide.cmdLast' },
          { code: '/context', hint: 'guide.cmdContext' },
          { code: '/ratelimits', hint: 'guide.cmdRatelimits' },
        ],
      },
      {
        id: 'memory',
        chips: [
          { code: 'guide.codeForget', i18n: true, hint: 'guide.cmdForget' },
          { code: '/reset', hint: 'guide.cmdReset' },
          { code: '/ltm_privacy smart|strict', hint: 'guide.cmdPrivacy' },
          { code: '/ltm_export', hint: 'guide.cmdExport' },
        ],
      },
      {
        id: 'features',
        chips: [
          { code: 'guide.codeRemind', i18n: true, hint: 'guide.cmdRemind' },
          { code: '/reminders', hint: 'guide.cmdReminders' },
          { code: 'guide.codeCancelReminder', i18n: true, hint: 'guide.cmdCancelReminder' },
          { code: '/todo', hint: 'guide.cmdTodo' },
          { code: 'guide.codeAddTodo', i18n: true, hint: 'guide.cmdAddTodo' },
          { code: '/inventory', hint: 'guide.cmdInventory' },
          { code: 'guide.codeAddInventory', i18n: true, hint: 'guide.cmdAddInventory' },
          { code: 'guide.codeLearn', i18n: true, hint: 'guide.cmdLearn' },
          { code: '/files', hint: 'guide.cmdFiles' },
          { code: '/reset_files', hint: 'guide.cmdResetFiles' },
          { code: '/web', hint: 'guide.cmdWeb' },
        ],
      },
      {
        id: 'owner',
        chips: [
          { code: '/erase', hint: 'guide.cmdErase' },
          { code: '/resetall', hint: 'guide.cmdResetall' },
          { code: '/reset_diary', hint: 'guide.cmdResetDiary' },
        ],
      },
    ],
    window: 'telegram — @persona_bot',
    dialog: [
      { who: 'op', text: '/start' },
      { who: 'bot', text: 'guide.dlgTgHello', i18n: true },
      { who: 'op', text: 'guide.dlgTgForget', i18n: true },
      { who: 'bot', text: 'guide.dlgTgForgot', i18n: true },
    ],
  },
];

// Пример речью: «[напомни] через 2 часа позвонить» — копируется только
// команда в квадратных скобках, остальное — поясняющий текст примера.
// Без скобок фраза копируется целиком (команды, короткие ответы)
const splitExample = (text: string): { text: string; key: boolean }[] =>
  text
    .split(/(\[[^\]]+\])/)
    .filter(Boolean)
    .map((part) =>
      part.startsWith('[') && part.endsWith(']') ? { text: part.slice(1, -1), key: true } : { text: part, key: false },
    );

const ALL_STEP_IDS = TRACKS.flatMap((tr) => tr.steps.map((s) => `${tr.id}.${s.id}`));

// Пройденные шаги из localStorage; неизвестные id (старые версии) отбрасываем.
// Ключ общий для всех блоков страницы, поэтому пишем через свежее чтение —
// иначе блок со старым состоянием затрёт отметки соседнего
function loadDone(): Set<string> {
  try {
    const raw = localStorage.getItem('vpc-guide-done');
    const ids: unknown = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(ids)) return new Set();
    return new Set(ids.filter((id): id is string => typeof id === 'string' && ALL_STEP_IDS.includes(id)));
  } catch {
    return new Set();
  }
}

function saveDone(ids: Set<string>) {
  try {
    localStorage.setItem('vpc-guide-done', JSON.stringify([...ids]));
  } catch {
    /* приватный режим / квота — прогресс живёт до перезагрузки */
  }
}

// Системная настройка «уменьшить движение»: следим и за её сменой на лету
function useReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)';
  const [reduced, setReduced] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return reduced;
}

export default function StartGuide({ onNavigate, trackIds, titleKey, num, leadKey, allDoneKey, id }: StartGuideProps) {
  const { t, lang } = useI18n();
  const reduced = useReducedMotion();
  const uidBase = useId();
  const tracks = TRACKS.filter((tr) => trackIds.includes(tr.id));
  const ownIds = tracks.flatMap((tr) => tr.steps.map((s) => `${tr.id}.${s.id}`));
  const [trackIdx, setTrackIdx] = useState(0);
  const [done, setDone] = useState<Set<string>>(loadDone);
  // Чип, который только что скопировали, — для вспышки «✓ скопировано»
  const [copied, setCopied] = useState<string | null>(null);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const track = tracks[trackIdx];

  // Таймер вспышки копирования не должен пережить компонент
  useEffect(
    () => () => {
      if (copyTimer.current) clearTimeout(copyTimer.current);
    },
    [],
  );

  const toggleDone = (uid: string) => {
    const next = loadDone();
    if (next.has(uid)) next.delete(uid);
    else next.add(uid);
    saveDone(next);
    setDone(next);
  };

  // Сброс — только шагов этого блока
  const resetDone = () => {
    const next = loadDone();
    ownIds.forEach((id) => next.delete(id));
    saveDone(next);
    setDone(next);
  };

  const copy = async (uid: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Нет доступа к буферу (http, запрет браузера) — вспышку не показываем
      return;
    }
    setCopied(uid);
    if (copyTimer.current) clearTimeout(copyTimer.current);
    copyTimer.current = setTimeout(() => setCopied(null), 1400);
  };

  // Стрелки влево/вправо переключают маршрут, как в обычном tablist
  const onTabsKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    e.preventDefault();
    const next = (trackIdx + (e.key === 'ArrowRight' ? 1 : tracks.length - 1)) % tracks.length;
    setTrackIdx(next);
    tabRefs.current[next]?.focus();
  };

  const total = ownIds.length;
  const doneCount = ownIds.filter((id) => done.has(id)).length;
  const pct = total ? Math.round((doneCount / total) * 100) : 0;

  return (
    <div id={id} className="start-guide">
      <div className="home-block-head">
        <span className="home-block-title">{t(titleKey)}</span>
        <span className="home-block-num">{num}</span>
      </div>

      {/* Строка прогресса по всем маршрутам */}
      <div className="start-guide-bar">
        <span className="start-guide-bar-label">PROGRESS</span>
        <span className="start-guide-bar-count">{t('guide.progress', { n: doneCount, m: total })}</span>
        <span
          className="start-guide-meter"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={doneCount}
          aria-label={t('guide.progress', { n: doneCount, m: total })}
        >
          <i style={{ width: `${pct}%` }} />
        </span>
        <span className="start-guide-bar-pct">{pct}%</span>
        <span className="start-guide-bar-actions">
          {doneCount > 0 && (
            <button type="button" className="start-guide-link" onClick={resetDone}>
              {t('guide.reset')}
            </button>
          )}
        </span>
      </div>

      <div className="start-guide-body">
        <p className="start-guide-lead">
          {doneCount === total ? t(allDoneKey) : t(leadKey)}
        </p>

        {/* Переключатель маршрутов: HUD-сегменты с бегущей полосой активного */}
        {tracks.length > 1 && (
          <div
            className="start-guide-tabs"
            role="tablist"
            aria-label={t('guide.tracksAria')}
            style={{ '--guide-i': trackIdx, '--guide-n': tracks.length } as CSSProperties}
            onKeyDown={onTabsKey}
          >
            {tracks.map((tr, i) => {
              const trDone = tr.steps.filter((s) => done.has(`${tr.id}.${s.id}`)).length;
              const complete = trDone === tr.steps.length;
              return (
                <button
                  key={tr.id}
                  ref={(el) => {
                    tabRefs.current[i] = el;
                  }}
                  type="button"
                  role="tab"
                  id={`${uidBase}-tab-${tr.id}`}
                  aria-selected={i === trackIdx}
                  aria-controls={`${uidBase}-panel`}
                  tabIndex={i === trackIdx ? 0 : -1}
                  className={`start-guide-tab${i === trackIdx ? ' is-active' : ''}${complete ? ' is-complete' : ''}`}
                  onClick={() => setTrackIdx(i)}
                >
                  <span className="start-guide-tab-index">
                    {tr.index}
                    <span className="start-guide-tab-count">
                      {complete ? '✓' : `${trDone}/${tr.steps.length}`}
                    </span>
                  </span>
                  <span className="start-guide-tab-title">{t(`guide.track.${tr.id}`)}</span>
                  <span className="start-guide-tab-sub">{t(`guide.track.${tr.id}Sub`)}</span>
                </button>
              );
            })}
            <span className="start-guide-tab-ink" aria-hidden="true" />
          </div>
        )}

        <div
          className="start-guide-grid"
          id={`${uidBase}-panel`}
          role={tracks.length > 1 ? 'tabpanel' : undefined}
          aria-labelledby={tracks.length > 1 ? `${uidBase}-tab-${track.id}` : undefined}
        >
          {/* Лента шагов: remount по key — шаги заново поднимаются каскадом */}
          <ol key={`steps-${track.id}`} className="start-guide-steps">
            {track.steps.map((s, i) => {
              const uid = `${track.id}.${s.id}`;
              const isDone = done.has(uid);
              const num = String(i + 1).padStart(2, '0');
              const chips = lang === 'en' && s.chipsEn ? s.chipsEn : s.chips;
              const listChips = chips?.some((c) => c.hint);
              // Шаг из примеров речью — список строк, а не россыпь чипов
              const examples = chips?.some((c) => (c.i18n ? t(c.code) : c.code).includes('['));
              const nav = s.nav;
              return (
                <li
                  key={uid}
                  className={`start-guide-step${isDone ? ' is-done' : ''}`}
                  style={{ animationDelay: `${i * 70}ms` }}
                >
                  <div className="start-guide-rail">
                    <button
                      type="button"
                      className="start-guide-mark"
                      aria-pressed={isDone}
                      aria-label={t('guide.markAria', { n: num })}
                      onClick={() => toggleDone(uid)}
                    >
                      {isDone ? '✓' : num}
                    </button>
                    {i < track.steps.length - 1 && (
                      <span className="start-guide-line" aria-hidden="true">
                        <i />
                      </span>
                    )}
                  </div>

                  <div className="start-guide-step-body">
                    <div className="start-guide-step-head">
                      <span className="start-guide-step-idx">STEP_{num}</span>
                      <h4 className="start-guide-step-title">{t(`guide.${track.id}.${s.id}Title`)}</h4>
                      <button
                        type="button"
                        className={`start-guide-check${isDone ? ' is-done' : ''}`}
                        onClick={() => toggleDone(uid)}
                      >
                        {isDone ? t('guide.done') : t('guide.markDone')}
                      </button>
                    </div>
                    <p className="start-guide-step-desc">{t(`guide.${track.id}.${s.id}Desc`)}</p>
                    {s.warn && (
                      <p className="start-guide-warn">
                        <span className="start-guide-warn-tag">!</span>
                        {t(s.warn)}
                      </p>
                    )}

                    {/* Примеры речью: строка — фраза целиком, команда в ней подсвечена;
                        клик копирует только команду (без скобок — фразу целиком) */}
                    {chips && examples && (
                      <ul className="start-guide-ex">
                        {chips.map((c, ci) => {
                          const chipUid = `${uid}#${ci}`;
                          const parts = splitExample(c.i18n ? t(c.code) : c.code);
                          const hasKey = parts.some((pt) => pt.key);
                          const key = hasKey
                            ? parts.filter((pt) => pt.key).map((pt) => pt.text).join(' ')
                            : parts.map((pt) => pt.text).join('');
                          const isCopied = copied === chipUid;
                          return (
                            <li key={chipUid}>
                              <button
                                type="button"
                                className={`start-guide-ex-row${isCopied ? ' is-copied' : ''}`}
                                title={t('guide.copyKey', { k: key })}
                                onClick={() => void copy(chipUid, key)}
                              >
                                <span className="start-guide-ex-line">
                                  <span className="start-guide-ex-prompt" aria-hidden="true">›</span>
                                  <span className="start-guide-ex-text">
                                    {hasKey ? (
                                      parts.map((pt, pi) =>
                                        pt.key ? (
                                          <span key={pi} className="start-guide-ex-key">{pt.text}</span>
                                        ) : (
                                          <span key={pi}>{pt.text}</span>
                                        ),
                                      )
                                    ) : (
                                      <span className="start-guide-ex-key">{key}</span>
                                    )}
                                  </span>
                                  <span className="start-guide-ex-copy" aria-hidden="true">
                                    {isCopied ? `✓ ${t('guide.copiedShort')}` : t('guide.copyShort')}
                                  </span>
                                </span>
                                {c.hint && <span className="start-guide-ex-hint">{t(c.hint)}</span>}
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    )}

                    {chips && !examples && (
                      <div className={listChips ? 'start-guide-cmds' : 'start-guide-chips'}>
                        {chips.map((c, ci) => {
                          const chipUid = `${uid}#${ci}`;
                          const text = c.i18n ? t(c.code) : c.code;
                          const isCopied = copied === chipUid;
                          const chip = (
                            <button
                              type="button"
                              className={`start-guide-code${isCopied ? ' is-copied' : ''}`}
                              title={t('guide.copy')}
                              onClick={() => void copy(chipUid, text)}
                            >
                              <code>{text}</code>
                              {isCopied && <span className="start-guide-code-flash">{t('guide.copied')}</span>}
                            </button>
                          );
                          return c.hint ? (
                            <div key={chipUid} className="start-guide-cmd">
                              {chip}
                              <span className="start-guide-cmd-hint">{t(c.hint)}</span>
                            </div>
                          ) : (
                            <span key={chipUid}>{chip}</span>
                          );
                        })}
                      </div>
                    )}

                    {s.models && (
                      <OllamaModels uid={uid} copied={copied} onCopy={(id, text) => void copy(id, text)} />
                    )}

                    {nav && (
                      <button type="button" className="btn btn--ghost start-guide-go" onClick={() => onNavigate(nav)}>
                        {t('guide.open', { s: t(`nav.${nav}`) })} →
                      </button>
                    )}
                    {s.dossier && (
                      <button
                        type="button"
                        className="btn btn--ghost start-guide-go"
                        onClick={() => {
                          requestDossierTab(s.dossier!);
                          onNavigate('chat');
                        }}
                      >
                        {t('guide.openDossier', { tab: t(`dossier.${s.dossier}`) })} →
                      </button>
                    )}
                    {s.anchor && (
                      <button
                        type="button"
                        className="btn btn--ghost start-guide-go"
                        onClick={() => document.getElementById(s.anchor!)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
                      >
                        {t('guide.toControl')} ↓
                      </button>
                    )}
                    {s.here && <span className="badge badge--active start-guide-here">{t('guide.here')}</span>}
                  </div>
                </li>
              );
            })}
          </ol>

          {/* Демо-диалог: remount по key — сценарий начинается заново */}
          <GuideDialog key={`dlg-${track.id}`} track={track} reduced={reduced} />
        </div>
      </div>
    </div>
  );
}

interface GuideDialogProps {
  track: Track;
  reduced: boolean;
}

// Живое демо-окно: реплики печатаются по символу, у персоны — «печатает…»,
// после финала пауза и повтор. При reduced motion — сразу весь диалог
function GuideDialog({ track, reduced }: GuideDialogProps) {
  const { t } = useI18n();
  const lines = track.dialog.map((l) => ({ ...l, text: l.i18n ? t(l.text) : l.text }));
  // line — текущая реплика, chars — сколько её символов уже видно
  const [pos, setPos] = useState({ line: 0, chars: 0 });
  // Вне вьюпорта печать стоит: страница длинная, демо не должно крутиться зря
  const rootRef = useRef<HTMLDivElement>(null);
  const [inView, setInView] = useState(() => typeof IntersectionObserver === 'undefined');
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(([e]) => setInView(e.isIntersecting), { threshold: 0.2 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  const cur = lines[pos.line] as (typeof lines)[number] | undefined;
  const curLen = cur?.text.length ?? 0;
  const curWho = cur?.who;
  // Служебные строки появляются целиком
  const instant = curWho === 'sys';

  useEffect(() => {
    if (reduced || !inView) return;
    let delay: number;
    let next: { line: number; chars: number };
    if (pos.line >= lines.length) {
      // Сценарий закончился — пауза и повтор с начала
      delay = 4200;
      next = { line: 0, chars: 0 };
    } else if (pos.chars >= curLen) {
      delay = instant ? 260 : 650;
      next = { line: pos.line + 1, chars: 0 };
    } else if (instant) {
      delay = 220;
      next = { line: pos.line, chars: curLen };
    } else if (pos.chars === 0) {
      // Пауза перед репликой: у персоны это «печатает…»
      delay = curWho === 'bot' ? 900 : 480;
      next = { line: pos.line, chars: 1 };
    } else {
      delay = curWho === 'op' ? 45 : 18;
      next = { line: pos.line, chars: pos.chars + 1 };
    }
    const timer = setTimeout(() => setPos(next), delay);
    return () => clearTimeout(timer);
  }, [reduced, inView, pos, lines.length, curLen, curWho, instant]);

  const view = reduced ? { line: lines.length, chars: 0 } : pos;
  const typingOp = curWho === 'op' && view.line < lines.length;
  const botTyping = !reduced && curWho === 'bot' && pos.chars === 0;
  // В чате реплика оператора набирается в поле ввода и «уходит» целиком
  const shown = lines.slice(0, view.line);
  const partial = cur && view.line < lines.length && !typingOp && view.chars > 0 ? cur : undefined;

  const renderLine = (l: (typeof lines)[number], text: string, key: number, typing = false) => (
    <div key={key} className={`start-guide-dlg-msg start-guide-dlg-msg--${l.who}`}>
      {text}
      {typing && <span className="start-guide-dlg-cursor" />}
    </div>
  );

  return (
    <div
      ref={rootRef}
      className="start-guide-dlg bracketed"
      role="region"
      aria-label={t('guide.demoLabel')}
    >
      <div className="corner tl" />
      <div className="corner tr" />
      <div className="corner bl" />
      <div className="corner br" />
      <div className="start-guide-dlg-titlebar">
        <span className="lights"><i /><i /><i /></span>
        <span className="start-guide-dlg-window">{track.window}</span>
        <span className="start-guide-dlg-live">LIVE</span>
      </div>
      <div className="start-guide-dlg-peer">
        <span className="start-guide-dlg-avatar">P</span>
        <span>
          <span className="start-guide-dlg-peer-name">persona</span>
          <span className={`start-guide-dlg-peer-status${botTyping ? ' is-typing' : ''}`}>
            {botTyping ? t('guide.typing') : t('guide.online')}
          </span>
        </span>
      </div>

      <div className="start-guide-dlg-log" aria-live="off">
        {shown.map((l, i) => renderLine(l, l.text, i))}
        {partial && renderLine(partial, partial.text.slice(0, view.chars), view.line, view.chars < partial.text.length)}
        {botTyping && (
          <div className="start-guide-dlg-msg start-guide-dlg-msg--bot start-guide-dlg-dots" aria-hidden="true">
            <i /><i /><i />
          </div>
        )}
      </div>

      <div className="start-guide-dlg-input">
        {typingOp && cur && view.chars > 0 ? (
          <span className="start-guide-dlg-draft">
            {cur.text.slice(0, view.chars)}
            <span className="start-guide-dlg-cursor" />
          </span>
        ) : (
          <span className="start-guide-dlg-placeholder">{t('guide.inputPlaceholder')}</span>
        )}
        <span className="start-guide-dlg-send">↵</span>
      </div>
      <div className="start-guide-dlg-caption">{t('guide.demoLabel')}</div>
    </div>
  );
}
