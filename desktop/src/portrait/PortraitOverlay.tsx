import { invoke } from '@tauri-apps/api/core';
import { listen } from './winEvents';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type WheelEvent } from 'react';
import { hitOnBody, useHitMask } from '../../../web/src/portrait/hitMask';
import LivePortrait from '../../../web/src/portrait/LivePortrait';
import { LiveHead } from '../../../web/src/portrait/LiveHead';
import { headAtlas } from '../../../web/src/portrait/headAtlas';
import InitiativeBadgeMark from '../../../web/src/portrait/InitiativeBadgeMark';
import type { InitiativeBadge, PortraitData } from '../../../web/src/portrait/portraitTypes';
import type { RoomAvatarPreset } from '../../../web/src/mockData';
import { fallbackAvatar } from '../../../web/src/room/avatarDefaults';
import { NEUTRAL_ACCENT } from '../../../web/src/room/avatarOptions';
import PatHearts from '../../../web/src/portrait/PatHearts';
import { usePat } from '../../../web/src/portrait/usePat';
import AnimSprite, { type SpriteTravel } from '../../../web/src/portrait/AnimSprite';
import { MOOD_POLL_MS, moodEmotion, useEmotionHold, type EmotionEvent } from '../../../web/src/portrait/emotionHold';
import type { PersonaMood } from '../../../web/src/portrait/portraitTypes';
import { animHit, animMask, type AnimMask } from '../../../web/src/portrait/animMask';
import { animAnchor, animBox, animSink, animsFor, pickWeighted, switchAnimsFor, type PortraitAnims } from '../../../web/src/portrait/animTypes';
import { useAnimSound } from '../../../web/src/portrait/animSound';
import type { VoiceView } from '../api';
import { t } from '../i18n';
import { voiceError, voiceText as micStateText, voiceTone } from '../voiceStatus';
import { measureFits, splitParts } from './bubbleParts';
import {
  answerOptionsOf,
  answerSend,
  labelTail,
  stripOptionLines,
  stripYesNoHint,
  type AnswerOption,
  type AnswerOptions,
} from './answerOptions';
import { useBody } from './useBody';
import PortraitPlayer from './PortraitPlayer';
import { useSkinLook, type UiLook } from './skinLook';

/* Портрет на рабочем столе — страница прозрачного окна поверх всех окон
   (src-tauri/src/portrait.rs). Движок — тот же LivePortrait, что в
   голосовом режиме веба. Своих листов портрета у персоны нет — живая голова
   её фигуры из «Комнаты» (LiveHead, облик — avatar от панели), а её лист A
   (headAtlas) — для маски: клик по фигуре и место облачка над макушкой.

   Окно пропускает клики насквозь, пока курсор не над фигурой: панель шлёт
   положение курсора (portrait-cursor), страница смотрит альфу кадра под ним
   (hitMask) и говорит панели, ловить ли мышь (portrait_hit). Заодно портрет
   смотрит на курсор по всему экрану. Над фигурой:
   - клик — погладить: глаза закрыты, голова под руку, сердечко; ядру —
     portrait_pet (персона узнает, если включены «тычки»). Гладят кликами,
     часто и подряд — голова при этом остаётся на месте: двойной клик ничего
     не значит, «сердится» — только своя анимация на очень частые клики;
   - зажать и потянуть — переставить окно;
   - колёсико — больше или меньше;
   - правая кнопка — меню: чей портрет, облачка, убрать с рабочего стола.

   Персона сказала (portrait-say: ответ в чате, откуда бы ни писали, или
   фоновое сообщение) — реплика облачком над головой, рот ходит. Длинная —
   по частям под размер облачка (bubbleParts), одна за другой, пока не
   договорит; часть висит, пока её можно прочесть (вас нет или курсор над
   облачком — ждёт). Клик по облачку — следующая часть, по последней — чат
   в вебе.

   Поле для сообщений сбоку от головы — со стороны середины экрана
   (inputSide от панели): реплика уходит персоне портрета, как из веба
   (portrait_send), пока ждёт ответа — «думает». Окно берёт клавиатуру
   только на время ввода (portrait_focus); Esc или другое окно — отдаёт.
   Поле живёт в панели мини-чата (кнопкой разворачивается, см. ниже);
   набранное остаётся до следующего раза.

   Мини-чат: при наведении на фигуру в колонке поля — только кнопка чата
   (микрофона при наведении нет — он внутри; исключение — разговор голосом:
   микрофон всегда виден, чтобы его закончить). Клик разворачивает внизу
   колонки отдельные элементы: строку с полем для сообщений («Отправить»
   и микрофон в конце строки) и кнопку «показать историю» над ней —
   раскрывает вверх историю переписки пузырями (что писали вы, что отвечала
   персона; начало — из ядра, portrait_history, дальше — события ленты),
   там же варианты ответа на вопрос. Отправили — ваше сообщение сразу
   встаёт облачком сбоку (над элементами мини-чата, если он открыт), а
   в историю ложится фоном. Курсор ушёл — через FIELD_MS всё прячется
   и сворачивается обратно в кнопку.

   Вопрос, на который персона ждёт ответа (режим управления «Делаю?
   (да/нет)», «Какой открыть?», «какой файл?»), приходит с вариантами
   (answerOptions): да/нет — кнопками в облачке под вопросом; список —
   в облачке только вопрос, варианты — стопкой кнопок в колонке поля, над
   ним, облачко с вопросом тогда не шире головы (поля нет — номерами в
   облачке, подпись — подсказкой). Подсказку «(да/нет)» заменяют кнопки. Кнопка шлёт
   персоне обычную реплику (номер, «да»/«нет»/«отмена»), как кнопки веба.
   Облачко с вопросом не листается, пока вопрос ждёт ответа (не дольше
   CHOICE_MS); кнопки уходят, когда вы ответили где угодно (heard), по
   крестику облачка или по сроку.

   Голову можно задвинуть за край экрана — поле и облачко остаются на
   видимой части окна (portrait-visible от панели): поле не ниже FIELD_GAP
   от низа экрана, облачко не выше верха и не за боками; пока окно на экране
   целиком, они на своих местах у головы.

   Говорит (висит облачко, и между частями тоже) — смотрит на вас, за
   курсором не следит; висит ваше сообщение — тоже: читает его, потом
   думает, на курсор не отвлекается.

   Пауза до ответа (лента ядра: heard → ответ → settled, откуда бы вы ни
   писали — из поля, из веба, с телефона): ваше сообщение встаёт облачком
   сбоку от головы — в колонке поля (поле выключено — над головой, пока нет
   реплики); персона кивает, поворачивается к нему и «читает» строками
   (по длине текста), потом думает (взгляд вверх-вбок). Облачко висит до
   ответа: ответ или конец хода его убирает. Картинка без подписи — без
   облачка, только кивок и «думает».

   Спит — глаза закрыты, голова опущена, за курсором не следит: когда вас нет
   у компьютера (portrait-away), когда персона спит в «Комнате» (asleep с
   ядра), а если ядро этого не знает — ночью по часам компьютера. Погладили —
   просыпается и не спит WAKE_MS после последнего касания, потом засыпает
   снова.

   Тело (useBody): свои анимации персоны — листы кадров из раздела «Аватар»
   (app/api/portrait_anim.py). Пока играет анимация, голова скрыта, кадр
   стоит низом к низу головы, клик ловится по альфе кадра (animMask). Когда
   играть — по событиям: погладили, очень частые клики (сердится), вернулись
   к компьютеру, засыпает и просыпается, персона
   написала первой, думает над ответом, читает брошенный файл, «посиди со
   мной», долго без перерыва; персона выбрала сама (лента: act); и случайно
   (ротация). Панель двигает окно: прогулка (только со своими шагами),
   «Иди сюда», «Спрячься», бросок и падение, прыжок на окно
   (portrait-motion) — на это свои анимации шагов (в другую сторону —
   отражённые), «держат», «падает», «прыжок»; своих шагов нет — голова
   переезжает неподвижно.

   Разговор голосом (portrait-voice от панели): вы назвали персону, а её
   чат в вебе не открыт — панель поставила её портрет на стол, и дальше с
   ней говорят без имени. Что вы говорите, встаёт облачком сбоку по ходу
   речи (то же облачко, что у сообщения из поля); пока говорите и пока она
   ждёт фразу — смотрит на вас («слушает»); фраза кончилась — читает её и
   думает, отвечает голосом — рот ходит. Закончить разговор, не выключая
   микрофон, — пункт меню (или «хватит» голосом). Назвали в разговоре другую персону —
   встаёт её портрет (своё окно, гость), голос (portrait-voice) теперь
   только у неё; это окно — как без разговора, пока её не назовут снова.
   Перебили, пока отвечает (portrait-cut), — реплика в облачке обрывается,
   рот закрывается; что вы говорите — облачком сбоку, как обычно.

   Кнопка микрофона — над полем для сообщений, над «Отправить» (поле
   выключено — в углу головы, со стороны поля): видна, пока курсор над
   фигурой (вместе с полем) и всё время разговора голосом; голову задвинули
   за край — едет вместе с полем, облачко вашего сообщения встаёт выше неё. Клик — выключить
   или включить слушание (portrait_mic_toggle) — та же настройка, что
   «Слушать имя» в трее и в окне панели; выключили посреди разговора —
   разговор кончается. Значок: перечёркнут — не слушает; бледный —
   включается или ждёт (бот, голосовой чат); с красной обводкой — не
   работает (подсказка — почему); залит — слышит вашу фразу. Состояние —
   от панели (portrait-mic).

   Файл, брошенный на фигуру, панель отправляет персоне (portrait-file):
   облачко с именем файла сбоку, персона «читает» до ответа.

   «Посиди со мной» (portrait-focus из меню): таймер на 25 или 50 минут —
   полоска под головой, без ротации и прогулок; кончился — анимация
   перерыва и записка в облачке. */

interface Loaded {
  persona: string | null;
  name?: string;
  data: PortraitData | null;
  initiativeBadge?: InitiativeBadge | null;
  // Облик фигуры, если листов портрета нет (null — по умолчанию)
  avatar?: RoomAvatarPreset | null;
  offline: boolean;
  gone?: boolean;
  asleep?: boolean | null;
  away?: boolean;
  bubbles?: boolean;
  bubbleH?: number;
  input?: boolean;
  inputW?: number;
  inputSide?: 'left' | 'right';
  text?: TextSpeed | null;
  // «Ходит по окнам» и «Анимации» в меню; свои анимации персоны с атласами
  physics?: boolean;
  animsOn?: boolean;
  // «Звуки анимаций» в меню (src-tauri portrait.rs, Placement.sounds)
  soundsOn?: boolean;
  anims?: PortraitAnims | null;
  // Тема оформления веба (app/core/ui_look): голова — рисовкой скина
  look?: UiLook | null;
  // Режим персоны в основном чате ("" — обычный; null — ядро не знает):
  // листы и анимации — его аватара (app/api/avatar_modes)
  mode?: string | null;
}

// Скорость текста в облачке — настройка «Общие» в вебе (app/core/avatar_text):
// слов в секунду (0 — сразу всей частью) и секунд на часть средней длины.
// Приходит с портретом и с каждым ответом ленты реплик (portrait-text)
interface TextSpeed {
  words_per_sec: number;
  part_sec: number;
}
const TEXT_DEFAULT: TextSpeed = { words_per_sec: 5, part_sec: 6.5 };
// Из ядра — числа в пределах; иное (ядро старее, битое) — умолчание
function textSpeed(v: Partial<TextSpeed> | null | undefined): TextSpeed {
  const w = Number(v?.words_per_sec);
  const p = Number(v?.part_sec);
  return {
    words_per_sec: Number.isFinite(w) && w >= 0 ? w : TEXT_DEFAULT.words_per_sec,
    part_sec: Number.isFinite(p) && p > 0 ? p : TEXT_DEFAULT.part_sec,
  };
}

// Часть реплики в облачке: n из of; note — служебная (не отправилось), без рта
interface Part {
  id: number;
  text: string;
  n: number;
  of: number;
  note?: boolean;
  // Вся реплика, из которой часть (по ней облачко узнаёт свой вопрос с кнопками)
  src: string;
}

// Вопрос с кнопками ответа: texts — реплика как пришла и как показана (без
// строк-вариантов); column — варианты стопкой в колонке поля, иначе — в облачке
interface Choice {
  id: number;
  kind: AnswerOptions['kind'];
  options: AnswerOption[];
  texts: string[];
  column: boolean;
  until: number;
}

// Ваше сообщение облачком сбоку, пока персона читает и думает над ответом;
// file — брошенный на фигуру файл (текст — его имя); voice — сказано голосом,
// live — фраза ещё звучит (текст растёт, персона слушает, а не читает)
interface Ask {
  id: number;
  text: string;
  at: number;
  file?: boolean;
  voice?: boolean;
  live?: boolean;
  // Что ушло персоне, если показано другое (кнопка: «2» — подписью варианта)
  sent?: string;
}

// Сообщение мини-чата у поля: что писали вы (me) и что отвечала персона;
// note — служебное (не отправилось), в ядро не ложилось
interface ChatMsg {
  me: boolean;
  text: string;
  note?: boolean;
}

// Разговор голосом (слушатель имени панели, voice.rs): phase — listening
// (ждёт фразу), hearing (слышит: text — что распознано на сейчас), thinking
// (ждёт ответ: sent — что ушло персоне), speaking (отвечает голосом)
interface VoiceTalk {
  persona: string;
  phase: string;
  text: string;
  sent: string;
}

// Распознаватель пишет строчными — фраза в облачке с заглавной
const capFirst = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

// Шаги: фигура в кадрах уходит вперёд — в окне она стоит на месте, а окно
// идёт её шагами (src-tauri portrait_motion, Gait), круг за кругом без прыжка назад
const WALK_TRAVEL: SpriteTravel = {
  mode: 'steady',
  onGait: (step) => void invoke('portrait_gait', { step }).catch(() => {}),
};

// Событие ленты реплик (portrait-say): реплика или пауза до ответа
interface Said {
  text: string;
  kind: string;
  // Кнопки ответа на этот вопрос (answerOptionsOf разберёт)
  options?: unknown;
}

const RETRY_MS = 15_000; // бот не ответил и показать нечего — спросить снова
const OFFLINE_MS = 60_000; // бот не ответил, портрет с диска или из кэша
// Спит ли персона и не правили ли портрет: атласы качаются, только если правили
const REFRESH_MS = 10 * 60_000;
const DRAG_PX = 5; // кнопка зажата и мышь сдвинулась — уже перетаскивание
const WHEEL_MS = 60;
// Реплика: рот ходит по длине текста, облачко висит, пока его можно прочесть;
// последняя часть — дольше, следующей её не сменить
const TALK_MS = { perChar: 55, min: 1200, max: 6000 };
const PART_MS = { perChar: 65, min: 2500, max: 12_000 };
const READ_MS = { perChar: 60, min: 7000, max: 25_000 };
// Облачко: строк в части, ширина (как --bubble-max в portrait.css: не уже
// BUBBLE_MIN_W и головы с запасом, не шире окна) и поля текста (рамка,
// отступы, место под крестик)
const BUBBLE_LINES = 4;
const BUBBLE_MIN_W = 280;
const BUBBLE_PAD_X = 2 + 10 + 24;
const EDGE = 4;
// От низа облачка до макушки: хвостик (5 px) и зазор, с запасом на «дыхание»
const CROWN_GAP = 12;
// Поле: высота и отступ снизу — как у .portrait-input в portrait.css
const FIELD_H = 30;
const FIELD_GAP = 10;

// Видимая часть окна (на экране), координаты страницы
interface Visible {
  left: number;
  top: number;
  right: number;
  bottom: number;
  // Запас окна под анимации крупнее головы (панель, frame): голова, облачко
  // и поле стоят внутри окна с такими отступами слева, сверху и справа
  padL?: number;
  padT?: number;
  padR?: number;
  padB?: number; // снизу: фигура в кадрах анимации уходит под пол
  layout?: {
    headX: number; headY: number; headSize: number;
    bubbleH: number; inputW: number; inputSide: 'left' | 'right';
  };
}
// Одну реплику ядро шлёт лентой, а ответ на поле — ещё и запросом: повтор
// за столько не показывать
const SEEN_MS = 60_000;
const NOTIFICATION_SEEN_MS = 120_000;
const MAX_PARTS = 40;
// Ночь по часам компьютера — когда ядро не знает, спит ли персона
const NIGHT_FROM_H = 0;
const NIGHT_TO_H = 7;
// Погладили спящую — не спит столько после последнего касания
const WAKE_MS = 5 * 60_000;
// Поле для сообщений: курсор ушёл — столько живёт после последнего нажатия
const FIELD_MS = 3000;
// Услышала, а конца хода нет (ядро перезапустили посреди ответа) — думать
// не дольше
const HEARD_MS = 3 * 60_000;
// Кивнула на свою отправку — heard из ленты следом второй раз не кивает
const NOD_GAP_MS = 1500;
// Ваше сообщение: читает столько (по длине); своя отправка уже показала его —
// heard ядра с тем же текстом за ASK_DUP_MS второй раз не встаёт
const ASK_READ_MS = { perChar: 60, min: 2000, max: 6000 };
const ASK_DUP_MS = 5000;
// Облачко сообщения сбоку: не шире, отступ от краёв колонки, от поля снизу;
// середина — на уровне глаз (доля высоты головы)
const ASK_MAX_W = 240;
const ASK_PAD = 6;
const ASK_EYES = 0.42;
// Сердится (только своя анимация «Много тычков»): столько кликов за
// ANGRY_MS — 4 в секунду, чаще, чем гладят
const ANGRY_PATS = 6;
const ANGRY_MS = 1500;
// Долго без перерыва: столько у компьютера подряд (без «вас нет»)
const BREAK_MS = 50 * 60_000;
// Брошенный файл: персона его ещё не получила (heard) — не дольше
const FILE_WAIT_MS = 3 * 60_000;
// Полоска «посиди со мной» обновляется не чаще
const FOCUS_TICK_MS = 15_000;
// Идёт или спряталась — смотрит по ходу / на экран; взгляд освежать
const GAZE_DIR_MS = 2500;
// Вопрос с кнопками ждёт ответа не дольше (дальше — ответ в чате)
const CHOICE_MS = 10 * 60_000;
// Стопка вариантов в колонке поля: не шире
const CHOICE_MAX_W = 240;

const span = (text: string, k: { perChar: number; min: number; max: number }) =>
  Math.min(k.max, Math.max(k.min, text.length * k.perChar));
// Часть «говорится» слово за словом: слово — по его длине (как у виджета
// на телефоне, PortraitWidget), при скорости по умолчанию; рот ходит, пока
// слова идут. Часть висит PART_MS/READ_MS при умолчании — с настройкой дольше
// или короче в тех же пропорциях
const WORD_MS = { base: 70, perChar: 22, max: 400 };
const wordMs = (word: string, sp: TextSpeed) =>
  sp.words_per_sec > 0
    ? (Math.min(WORD_MS.max, WORD_MS.base + word.length * WORD_MS.perChar) * TEXT_DEFAULT.words_per_sec) / sp.words_per_sec
    : 0;
const typeMs = (text: string, sp: TextSpeed) => text.split(' ').reduce((ms, w) => ms + wordMs(w, sp), 0);
const holdMs = (text: string, k: { perChar: number; min: number; max: number }, sp: TextSpeed) =>
  (span(text, k) * sp.part_sec) / TEXT_DEFAULT.part_sec;

function isNight(d = new Date()): boolean {
  const h = d.getHours();
  return h >= NIGHT_FROM_H && h < NIGHT_TO_H;
}

// Сколько до ближайшей границы ночи (начала или конца)
function msToNightEdge(d = new Date()): number {
  const edge = new Date(d);
  edge.setMinutes(0, 0, 0);
  if (isNight(d)) edge.setHours(NIGHT_TO_H);
  else {
    edge.setHours(NIGHT_FROM_H);
    if (edge <= d) edge.setDate(edge.getDate() + 1);
  }
  return Math.max(1000, edge.getTime() - d.getTime() + 1000);
}

export default function PortraitOverlay() {
  const [st, setSt] = useState<Loaded | null>(null);
  const [reload, setReload] = useState(0);
  const [away, setAway] = useState(false);
  const [night, setNight] = useState(isNight);
  const [parts, setParts] = useState<Part[]>([]);
  const [talking, setTalking] = useState(false);
  // Эмоция, которую персона выбрала к ответу (лента реплик, kind emo)
  const [emoEv, setEmoEv] = useState<EmotionEvent | null>(null);
  const emoSeq = useRef(0);
  // Настроение персоны: в остальное время лицо — в эмоции «по настроению»
  const [mood, setMood] = useState<PersonaMood | null>(null);
  // Сколько слов части уже на виду (id — какой части)
  const [typed, setTyped] = useState({ id: 0, n: 0 });
  // Скорость текста: новая — со следующей части
  const speed = useRef<TextSpeed>(TEXT_DEFAULT);
  const [reading, setReading] = useState(false); // курсор над облачком — не листать
  const [woke, setWoke] = useState(false); // погладили — проснулась
  const [heard, setHeard] = useState(false); // услышала реплику, ответа ещё нет
  const [ask, setAsk] = useState<Ask | null>(null);
  const askRef = useRef<Ask | null>(null);
  askRef.current = ask;
  // Вопрос с кнопками ответа
  const [choice, setChoice] = useState<Choice | null>(null);
  const choiceId = useRef(0);
  const choicesEl = useRef<HTMLDivElement>(null);
  // Мини-чат: панель и список истории
  const chatEl = useRef<HTMLDivElement>(null);
  const chatLogEl = useRef<HTMLDivElement>(null);
  const logSeen = useRef(new Map<string, number>());
  const askId = useRef(0);
  // Разговор голосом и облачко его текущей фразы
  const [voice, setVoice] = useState<VoiceTalk | null>(null);
  const voiceAsk = useRef(0);
  const voiceOn = !!voice;
  const voicePhase = voice?.phase ?? '';
  const voiceText = capFirst((voice?.text ?? '').trim());
  // Ждёт вашу фразу или слышит её — смотрит на вас
  const voiceListens = voicePhase === 'listening' || voicePhase === 'hearing';
  // Слушатель имени панели — для кнопки микрофона
  const [mic, setMic] = useState<VoiceView | null>(null);
  const micWait = useRef(false);
  const askEl = useRef<HTMLDivElement>(null);
  const [readingAsk, setReadingAsk] = useState(false); // читает ваше сообщение
  // Середина облачка сообщения в координатах взгляда портрета (LivePortrait)
  const [readAt, setReadAt] = useState<{ x: number; y: number } | null>(null);
  const readTimer = useRef(0);
  const [nod, setNod] = useState(0);
  const nodAt = useRef(0);
  const heardTimer = useRef(0);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [typing, setTyping] = useState(false); // поле для сообщений в фокусе
  const [hover, setHover] = useState(false); // курсор над фигурой или полем
  const [pendingNotifications, setPendingNotifications] = useState<string[]>([]);
  const pendingNotificationsRef = useRef<string[]>([]);
  const notificationSeen = useRef(new Map<string, number>());
  const figureHoverRef = useRef(false);
  // Мини-чат у поля: развёрнут (кнопка в ряду поля) и его история
  const [chatOpen, setChatOpen] = useState(false);
  // История переписки в мини-чате: раскрыта кнопкой над полем
  const [histOpen, setHistOpen] = useState(false);
  const [log, setLog] = useState<ChatMsg[]>([]);
  const [vis, setVis] = useState<Visible | null>(null); // null — окно целиком на экране
  const [win, setWin] = useState({ w: window.innerWidth, h: window.innerHeight });
  const visRef = useRef<Visible | null>(null);
  visRef.current = vis;
  const layout = vis?.layout;
  const padL = vis?.padL ?? 0;
  const padT = vis?.padT ?? 0;
  const padR = vis?.padR ?? 0;
  const padB = vis?.padB ?? 0;
  const [fieldOn, setFieldOn] = useState(false);
  const [playerActivity, setPlayerActivity] = useState(0);
  const wakePlayer = useCallback(() => { setFieldOn(true); setPlayerActivity((n) => n + 1); }, []);
  const hoverRef = useRef(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const seen = useRef(new Map<string, number>());
  const rootRef = useRef<HTMLDivElement>(null);
  const cell = useRef({ r: 0, c: 0 });
  const hit = useRef(false);
  const readingRef = useRef(false);
  const cursorAt = useRef<{ x: number; y: number } | null>(null);
  const press = useRef<{ x: number; y: number } | null>(null);
  const wheel = useRef({ acc: 0, timer: 0 });
  const saidId = useRef(0);
  const talkTimer = useRef(0);
  const wakeTimer = useRef(0);
  // Погладили или написали — ещё WAKE_MS без сна
  const wake = () => {
    setWoke(true);
    window.clearTimeout(wakeTimer.current);
    wakeTimer.current = window.setTimeout(() => setWoke(false), WAKE_MS);
  };
  // Тело — ниже (ему нужно, свободна ли персона); события выше зовут его по ref
  const bodyRef = useRef<ReturnType<typeof useBody> | null>(null);
  // Режим персоны по последней загрузке — по смене играет переход
  const modeSeen = useRef<{ persona: string; mode: string } | null>(null);
  const pats = useRef<number[]>([]);
  const { petted, hearts, pat } = usePat(() => {
    void invoke('portrait_pet');
    wake();
    // Очень часто тыкают — сердится (если есть своя анимация на это); иначе —
    // своя анимация «погладили». Своих нет — голова на месте
    const now = Date.now();
    pats.current = [...pats.current.filter((at) => now - at < ANGRY_MS), now];
    if (pats.current.length >= ANGRY_PATS && bodyRef.current?.play('angry')) pats.current = [];
    else bodyRef.current?.play('pet');
  });
  // Брошенный на фигуру файл ушёл персоне — ждёт её ответа («читает»)
  const [fileWait, setFileWait] = useState(false);
  const fileTimer = useRef(0);
  const stopFileWait = () => {
    window.clearTimeout(fileTimer.current);
    setFileWait(false);
  };
  // «Посиди со мной»: до какого времени и на сколько минут
  const [focus, setFocus] = useState<{ until: number; minutes: number } | null>(null);
  const [focusNow, setFocusNow] = useState(() => Date.now());
  // Когда вернулись к компьютеру: «привет» важнее, чем «проснулась»
  const backAt = useRef(0);
  const said = parts[0] ?? null;
  const lastPart = parts.length === 1;
  const saidWords = said ? said.text.split(' ') : [];
  const shownWords = said && typed.id === said.id ? typed.n : 1;

  const bubbleH = layout?.bubbleH ?? (st?.bubbles ? (st.bubbleH ?? 0) : 0);
  const inputW = layout?.inputW ?? (st?.input ? (st.inputW ?? 0) : 0);
  // Сторона головы сейчас: окно без поля, облачка и запаса под анимации
  const headNow = () => {
    const v = visRef.current;
    return v?.layout?.headSize ?? Math.min(
      window.innerWidth - inputW - (v?.padL ?? 0) - (v?.padR ?? 0),
      window.innerHeight - bubbleH - (v?.padT ?? 0) - (v?.padB ?? 0),
    );
  };
  const side = layout?.inputSide ?? (st?.inputSide === 'right' ? 'right' : 'left');
  const bubbleRef = useRef<HTMLDivElement>(null);

  // Реплика — в очередь облачка частями; повтор той же реплики за SEEN_MS — нет.
  // force — и повтор (вопрос с кнопками на новую реплику); also — тот же
  // ответ другим текстом (вопрос со строками-вариантами: его копия из
  // ответа на поле второй раз не встанет); lines — строк в части (под
  // кнопками в облачке — на одну меньше)
  const enqueue = (
    text: string,
    note = false,
    opt: { force?: boolean; also?: string; lines?: number; width?: number } = {},
  ) => {
    const clean = text.trim();
    if (!clean) return;
    if (!note && pendingNotificationsRef.current.includes(clean)) return;
    const now = Date.now();
    for (const [k, at] of seen.current) if (now - at > SEEN_MS) seen.current.delete(k);
    if (!note) {
      if (seen.current.has(clean) && !opt.force) return;
      seen.current.set(clean, now);
      if (opt.also) seen.current.set(opt.also.trim(), now);
    }
    const head = headNow();
    const bubbleMax = Math.min(window.innerWidth - 2 * EDGE, Math.max(BUBBLE_MIN_W, head + 40));
    const pieces = splitParts(clean, measureFits((opt.width ?? bubbleMax) - BUBBLE_PAD_X, opt.lines ?? BUBBLE_LINES));
    setParts((prev) =>
      [
        ...prev,
        ...pieces.map((piece, i) => ({ id: ++saidId.current, text: piece, n: i + 1, of: pieces.length, note, src: clean })),
      ].slice(-MAX_PARTS),
    );
  };
  // Вопрос с кнопками: в облачко — без строк-вариантов, если они ушли в
  // стопку сбоку (облачко тогда не шире головы — колонка под стопку), у
  // да/нет — без подсказки «(да/нет)». Уже на виду или в очереди (ответ на
  // поле пришёл запросом раньше ленты) — эти части заменяются: под кнопки
  // режется иначе
  const onChoice = (raw: string, opts: AnswerOptions) => {
    const labels = opts.options.filter((o) => o.label).length;
    const column = opts.kind === 'options' && labels > 0 && inputW > 0;
    const text = column ? stripOptionLines(raw, labels) : opts.kind === 'options' ? raw : stripYesNoHint(raw);
    const head = headNow();
    setParts((prev) => prev.filter((p) => p.src !== raw && p.src !== text));
    enqueue(text, false, {
      force: true,
      also: raw,
      lines: column ? BUBBLE_LINES : BUBBLE_LINES - 1,
      width: column ? Math.max(120, head - 2 * EDGE) : undefined,
    });
    setChoice({
      id: ++choiceId.current,
      kind: opts.kind,
      options: opts.options,
      texts: [raw, text],
      column,
      until: Date.now() + CHOICE_MS,
    });
  };
  // Ваше сообщение — облачком сбоку (или над головой, если поля нет); места
  // нет ни там, ни там — без облачка. sent — что ушло, если показано другое
  const showAsk = (text: string, file = false, sent?: string) => {
    const clean = text.trim();
    if (!clean || (!inputW && !bubbleH)) return;
    setAsk({ id: ++askId.current, text: clean, at: Date.now(), file, ...(sent ? { sent } : {}) });
  };
  // Сообщение — в историю мини-чата (вся переписка, без обрезки); подряд
  // одно и то же (своя отправка и heard следом) — один раз
  const addLog = (me: boolean, text: string, note = false) => {
    const clean = text.trim();
    if (!clean) return;
    setLog((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.me === me && last.text === clean) return prev;
      return [...prev, { me, text: clean, ...(note ? { note: true } : {}) }];
    });
  };
  // Реплика персоны — в историю мини-чата; повтор за SEEN_MS — нет (лента
  // и ответ поля могут принести одну и ту же в любом порядке)
  const logSaid = (text: string) => {
    const clean = text.trim();
    if (!clean) return;
    const now = Date.now();
    for (const [k, at] of logSeen.current) if (now - at > SEEN_MS) logSeen.current.delete(k);
    if (logSeen.current.has(clean)) return;
    logSeen.current.set(clean, now);
    addLog(false, clean);
  };
  // Кивок — услышала; на свою отправку и heard следом — один
  const nodNow = () => {
    const now = Date.now();
    if (now - nodAt.current < NOD_GAP_MS) return;
    nodAt.current = now;
    setNod((n) => n + 1);
  };
  // Событие ленты: пауза до ответа или реплика
  const onSaid = (e: Said) => {
    if (e.kind === 'emo') {
      // Выражение лица к ответу (или «Показать на рабочем столе») — до реплики
      setEmoEv({ id: e.text.trim(), seq: ++emoSeq.current });
      return;
    }
    if (e.kind === 'act') {
      // Персона выбрала движение сама (маркер в ответе) или его запустили
      // из раздела «Аватар» — облачка нет
      bodyRef.current?.act(e.text.trim());
      return;
    }
    if (e.kind === 'heard') {
      nodNow();
      setHeard(true);
      wake();
      // Вы ответили (здесь, в чате, голосом) — кнопки прежнего вопроса не нужны
      setChoice(null);
      window.clearTimeout(heardTimer.current);
      heardTimer.current = window.setTimeout(() => {
        setHeard(false);
        setAsk(null);
      }, HEARD_MS);
      // Своя отправка из поля уже показала это сообщение; брошенный файл —
      // его имя, а не просьба прочитать, которую панель послала за вас
      // Сказанное голосом уже в облачке — как вы это сказали, с именем
      const cur = askRef.current;
      const text = e.text.trim();
      const dup = !!cur && (cur.text === text || cur.sent === text) && Date.now() - cur.at < ASK_DUP_MS;
      if (!(cur && (dup || cur.file || cur.voice))) showAsk(text);
      // В историю мини-чата — и сказанное голосом, и просьбу прочитать файл
      // (облачко для них другое); своя отправка уже положила это сообщение
      if (!(cur && dup)) addLog(true, text);
    } else if (e.kind === 'settled') {
      // Ход кончился (ответ уже в облачке или не случился) — сообщение убрать;
      // вы уже говорите дальше (перебили её) — ваше облачко не трогать
      window.clearTimeout(heardTimer.current);
      setHeard(false);
      setAsk((a) => (a?.voice && a.live ? a : null));
      stopFileWait();
    } else {
      // Не ответ на вас (инициатива, напоминание, урок) — персона написала первой
      if (e.kind === 'initiative' || e.kind === 'reminder') {
        const text = e.text.trim();
        if (!text) return;
        const now = Date.now();
        for (const [key, at] of notificationSeen.current) {
          if (now - at > NOTIFICATION_SEEN_MS) notificationSeen.current.delete(key);
        }
        if (notificationSeen.current.has(text)) return;
        notificationSeen.current.set(text, now);
        // Убираем раннюю копию из STM, если она пришла как обычный ответ.
        setParts((prev) => prev.filter((part) => part.src !== text));
        seen.current.delete(text);
        pendingNotificationsRef.current = [...pendingNotificationsRef.current, text];
        setPendingNotifications(pendingNotificationsRef.current);
        logSaid(e.text);
        return;
      }
      if (pendingNotificationsRef.current.includes(e.text.trim())) return;
      if (e.kind !== 'reply' && e.text.trim() && !heard && !sending) bodyRef.current?.play('message');
      logSaid(e.text);
      const opts = answerOptionsOf(e.options);
      if (opts && e.text.trim()) onChoice(e.text.trim(), opts);
      else enqueue(e.text);
      // Ответ пришёл — ваше сообщение больше не держим (то, что вы говорите
      // сейчас, — держим)
      if (e.text.trim()) {
        setAsk((a) => (a?.voice && a.live ? a : null));
        stopFileWait();
      }
    }
  };
  const onSaidRef = useRef(onSaid);
  onSaidRef.current = onSaid;
  const nextPart = () => setParts((prev) => prev.slice(1));

  // Файл, брошенный на фигуру: панель отправляет его персоне сама, здесь —
  // облачко с именем сбоку, кивок и «читает» до ответа; не ушёл — записка
  const onFile = {
    start: (name: string) => {
      wake();
      nodNow();
      showAsk(name, true);
      setFileWait(true);
      window.clearTimeout(fileTimer.current);
      fileTimer.current = window.setTimeout(() => {
        setFileWait(false);
        setAsk((a) => (a?.file ? null : a));
      }, FILE_WAIT_MS);
    },
    done: (r: { ok: boolean; error?: string; replies?: string[] }) => {
      if (r.ok) {
        // Обычно ответ уже пришёл лентой — повтор enqueue отсеет
        const replies = (r.replies ?? []).filter((x) => x.trim());
        replies.forEach((x) => {
          enqueue(x);
          logSaid(x);
        });
        if (replies.length) {
          stopFileWait();
          setAsk((a) => (a?.file ? null : a));
        }
        return;
      }
      stopFileWait();
      setAsk((a) => (a?.file ? null : a));
      const err = r.error ?? '';
      const msg =
        err === 'offline'
          ? t('portraitSendOffline')
          : err === 'too_big'
            ? t('portraitFileTooBig')
            : err === 'not_file'
              ? t('portraitFileNotFile')
              : err === 'no_files'
                ? t('portraitFileNoFiles')
                : t('portraitSendFailed', { error: err });
      enqueue(msg, true);
      addLog(false, msg, true);
    },
    over: () => {
      wake();
      nodNow();
    },
  };
  const onFileRef = useRef(onFile);
  onFileRef.current = onFile;

  // Листов нет — голова фигуры (рисовкой скина, если в вебе он выбран); лист
  // A под маску печатается здесь же той же рисовкой. Рисовка скина ещё
  // грузится — голову пока не рисуем: стандартная мелькнула бы
  const skinLook = useSkinLook(st?.look, st?.persona);
  const head =
    st?.persona && !st.gone && !st.data?.sheets.A && skinLook !== undefined
      ? (st.avatar ?? fallbackAvatar(st.persona))
      : null;
  const headKey = head ? `${head.head}:${head.eyes}:${head.accessory}:${head.shade}:${skinLook?.key ?? ''}` : '';
  const headRef = useRef(head);
  headRef.current = head;
  const lookRef = useRef(skinLook);
  lookRef.current = skinLook;
  const [headSheet, setHeadSheet] = useState<{ key: string; data: PortraitData } | null>(null);
  useEffect(() => {
    const preset = headRef.current;
    if (!headKey || !preset) return;
    let alive = true;
    headAtlas(preset, { cell: 96, sheets: ['A'], look: lookRef.current ?? null })
      .then((d) => {
        if (alive) setHeadSheet({ key: headKey, data: d });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [headKey]);
  const data = st?.data?.sheets.A ? st.data : head && headSheet?.key === headKey ? headSheet.data : null;
  const mask = useHitMask(data);
  // Говорит, ждёт ответа или вы пишете — не спит; иначе спит, когда вас нет,
  // а если недавно не гладили и не писали — и когда спит в комнате или ночью
  const busy = !!said || talking || sending || typing || heard || !!ask || voiceOn || !!choice;
  const asleep = !busy && (away || (!woke && (st?.asleep ?? night)));
  // Спит, говорит (висит облачко), занята вашим сообщением (висит оно:
  // читает, потом думает до ответа) или с вами разговаривает — за курсором
  // не следит
  const attentive = !!said || talking || !!ask || voiceOn;
  // Отвечает голосом — рот ходит, даже если облачка нет
  const speakingNow = talking || voicePhase === 'speaking';
  // Настроение спрашиваем, только если есть эмоции «по настроению»
  const moodOn = !!data?.emotions?.some((e) => e.mood && e.sheets.A);
  useEffect(() => {
    if (!moodOn) {
      setMood(null);
      return;
    }
    let alive = true;
    const ask = () =>
      void invoke<PersonaMood | null>('portrait_mood')
        .then((m) => alive && setMood((old) => (JSON.stringify(old) === JSON.stringify(m) ? old : m)))
        .catch(() => {});
    ask();
    const timer = window.setInterval(ask, MOOD_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [moodOn, st?.persona, reload]);
  const emotion = useEmotionHold(emoEv, speakingNow) ?? moodEmotion(data, mood);
  // Рот по гласным (если у портрета есть их листы) — по открытому слову облачка
  let speech: { text: string; at: number } | null = null;
  if (said && !said.note && talking) {
    let at = 0;
    for (let i = 0; i < Math.min(saidWords.length, shownWords) - 1; i++) at += saidWords[i].length + 1;
    speech = { text: said.text, at };
  }
  const thinkingNow = (sending || heard || voicePhase === 'thinking') && !speakingNow && !(readingAsk && readAt);
  const body = useBody({
    anims: st?.anims ?? null,
    on: st?.animsOn !== false,
    physics: st?.physics !== false,
    asleep,
    // Ротации — только когда персона ничем не занята и вы её не трогаете
    free: !!st?.persona && !!data && !asleep && !away && !busy && !hover && !focus && !fileWait,
    calm: !asleep && !away && !busy && !focus && !fileWait,
    loops: { drop: fileWait, think: thinkingNow && !fileWait, focus: !!focus },
    headEl: () => rootRef.current?.querySelector('.live-portrait') ?? null,
  });
  bodyRef.current = body;
  const controlsHidden = ['held', 'fall', 'jump'].includes(body.motion.state);
  const chatButtonOn = fieldOn && !controlsHidden;
  // Звук играющей анимации (anim.sound_url): выключен в меню — тишина
  useAnimSound(body.showing, st?.soundsOn !== false && st?.animsOn !== false);

  // Клавиши и кнопки мыши анимаций: панель слушает их по всей системе
  // (src-tauri/src/input_watch.rs) и шлёт сработавшее сочетание
  const boundKeys = (() => {
    if (st?.animsOn === false) return '';
    const all = new Set<string>();
    for (const a of st?.anims?.anims ?? [])
      if (a.enabled && a.atlas && a.frames > 0 && Array.isArray(a.keys)) for (const k of a.keys) if (typeof k === 'string') all.add(k);
    return [...all].sort().join('\n');
  })();
  useEffect(() => {
    void invoke('portrait_keys', { keys: boundKeys ? boundKeys.split('\n') : [] }).catch(() => {});
  }, [boundKeys]);
  useEffect(() => {
    const un = listen<string>('portrait-key', (e) => {
      bodyRef.current?.key(e.payload);
    });
    return () => {
      void un.then((f) => f());
      void invoke('portrait_keys', { keys: [] }).catch(() => {});
    };
  }, []);

  const showingRef = useRef(body.showing);
  const floorClipRef = useRef(0);
  const edgeClipRef = useRef<{ side: 'left' | 'right' | 'top'; at: number } | null>(null);
  showingRef.current = body.showing;
  // Идёт, в прыжке, падает или спряталась — смотрит по ходу (или на экран),
  // а не на курсор
  const lookingAway = ['walk', 'jump', 'fall', 'hidden'].includes(body.motion.state);
  const noFollow = useRef(false);
  noFollow.current = asleep || attentive || lookingAway || !!focus;

  // ── Загрузка: сразу, по выбору персоны в меню и по таймеру ──
  useEffect(() => {
    let alive = true;
    let timer = 0;
    invoke<Loaded>('portrait_load')
      .then((res) => {
        if (!alive) return;
        if (res.text) speed.current = textSpeed(res.text);
        // Сменился режим персоны — переход из прежнего в новый (повод mode в
        // аватаре нового), если загружен; ставится тем же обновлением, что
        // и новая голова. Нет перехода — голова просто сменится
        if (typeof res.mode === 'string' && res.persona) {
          const was = modeSeen.current;
          modeSeen.current = { persona: res.persona, mode: res.mode };
          if (was && was.persona === res.persona && was.mode !== res.mode && res.animsOn !== false) {
            const a = pickWeighted(switchAnimsFor(res.anims, was.mode));
            if (a) bodyRef.current?.switchTo(a);
          }
        }
        // Тот же портрет (бот не ответил или не правили — из кэша) — без
        // перерисовки и новой маски; свежие только состояния. Анимации так же:
        // не правили — те же (играющая не начинается заново)
        setSt((prev) => {
          const same = !!prev && prev.persona === res.persona;
          const keepData = same && !!prev.data && !!res.data && prev.data.updated_at === res.data.updated_at;
          const keepAnims = same && !!prev.anims && !!res.anims && prev.anims.updated_at === res.anims.updated_at;
          if (!keepData && !keepAnims) return res;
          return { ...res, data: keepData ? prev.data : res.data, anims: keepAnims ? prev.anims : res.anims };
        });
        if (typeof res.away === 'boolean') setAway(res.away);
        const wait = !res.persona || (res.offline && !res.data?.sheets.A) ? RETRY_MS : res.offline ? OFFLINE_MS : REFRESH_MS;
        timer = window.setTimeout(() => setReload((n) => n + 1), wait);
      })
      .catch(() => {
        if (alive) timer = window.setTimeout(() => setReload((n) => n + 1), RETRY_MS);
      });
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [reload]);

  useEffect(() => {
    const un = [
      listen('portrait-reload', () => setReload((n) => n + 1)),
      listen<{ away: boolean }>('portrait-away', (e) => {
        setAway(e.payload.away);
        // Вернулись — заодно освежить, спит ли персона; и поздороваться
        if (!e.payload.away) {
          setReload((n) => n + 1);
          backAt.current = Date.now();
          bodyRef.current?.play('hello');
        }
      }),
      listen<{ name: string }>('portrait-file', (e) => onFileRef.current.start(e.payload.name)),
      listen<{ ok: boolean; error?: string; replies?: string[] }>('portrait-file-done', (e) => onFileRef.current.done(e.payload)),
      listen<{ over: boolean }>('portrait-file-over', (e) => {
        // Файл несут к ней — оживилась
        if (e.payload.over) onFileRef.current.over();
      }),
      listen<{ minutes: number }>('portrait-focus', (e) => {
        const m = Math.max(0, Math.round(Number(e.payload.minutes) || 0));
        setFocus(m > 0 ? { until: Date.now() + m * 60_000, minutes: m } : null);
      }),
      listen<Said>('portrait-say', (e) => onSaidRef.current(e.payload)),
      listen<VoiceTalk | null>('portrait-voice', (e) => setVoice(e.payload ?? null)),
      listen<VoiceView>('portrait-mic', (e) => setMic(e.payload)),
      // Её перебили — реплику оборвать: облачко и рот
      listen('portrait-cut', () => {
        window.clearTimeout(talkTimer.current);
        setTalking(false);
        setParts([]);
      }),
      listen<TextSpeed>('portrait-text', (e) => {
        speed.current = textSpeed(e.payload);
      }),
      // Ушли в другое окно — клавиатура уже не здесь
      listen('portrait-blur', () => inputRef.current?.blur()),
    ];
    return () => {
      window.clearTimeout(talkTimer.current);
      window.clearTimeout(wakeTimer.current);
      window.clearTimeout(heardTimer.current);
      window.clearTimeout(readTimer.current);
      window.clearTimeout(fileTimer.current);
      un.forEach((p) => void p.then((f) => f()));
    };
  }, []);

  // Ночь началась или кончилась — один таймер до ближайшей границы
  useEffect(() => {
    const timer = window.setTimeout(() => setNight(isNight()), msToNightEdge());
    return () => window.clearTimeout(timer);
  }, [night]);

  // Засыпает и просыпается — свои анимации на это (вернулись к компьютеру —
  // вместо «проснулась» уже играет «привет»)
  const wasAsleep = useRef<boolean | null>(null);
  useEffect(() => {
    if (!data) return;
    const prev = wasAsleep.current;
    wasAsleep.current = asleep;
    if (prev === null || prev === asleep) return;
    if (asleep) body.play('sleep');
    else if (Date.now() - backAt.current > 2000) body.play('wake');
  }, [asleep, !!data]);

  // «Посиди со мной»: полоска освежается раз в FOCUS_TICK_MS; время вышло —
  // перерыв (своя анимация) и записка
  useEffect(() => {
    if (!focus) return;
    setFocusNow(Date.now());
    const tick = window.setInterval(() => setFocusNow(Date.now()), FOCUS_TICK_MS);
    const end = window.setTimeout(
      () => {
        setFocus(null);
        bodyRef.current?.play('break');
        enqueue(t('portraitFocusDone', { min: focus.minutes }), true);
      },
      Math.max(0, focus.until - Date.now()),
    );
    return () => {
      window.clearInterval(tick);
      window.clearTimeout(end);
    };
  }, [focus]);

  // Долго у компьютера без перерыва (BREAK_MS подряд, «вас нет» не было) —
  // своя анимация «перерыв», если она есть; дальше — снова через BREAK_MS
  const hasBreak = st?.animsOn !== false && animsFor(st?.anims, 'break').length > 0;
  const [breakArm, setBreakArm] = useState(0);
  useEffect(() => {
    if (away || focus || !hasBreak) return;
    const timer = window.setTimeout(() => {
      bodyRef.current?.play('break');
      setBreakArm((n) => n + 1);
    }, BREAK_MS);
    return () => window.clearTimeout(timer);
  }, [away, !!focus, hasBreak, breakArm]);

  // Идёт, падает, в прыжке — смотрит по ходу; спряталась — на экран (dir —
  // в его сторону). Взгляд как от курсора далеко в той стороне, освежается
  // раз в GAZE_DIR_MS (иначе через 3 с вернётся к собеседнику)
  const { state: motionState, dir: motionDir } = body.motion;
  useEffect(() => {
    if (!lookingAway || !motionDir) return;
    const look = () => {
      const head = rootRef.current?.querySelector('.portrait-head')?.getBoundingClientRect();
      if (!head) return;
      const x = head.left + head.width / 2 + motionDir * 4000;
      const y = head.top + head.height * 0.45;
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: x, clientY: y }));
    };
    look();
    const timer = window.setInterval(look, GAZE_DIR_MS);
    return () => window.clearInterval(timer);
  }, [lookingAway, motionState, motionDir]);

  // Маска кадров играющей анимации — для кликов по фигуре
  const animMaskRef = useRef<{ key: number; m: AnimMask | null } | null>(null);
  const animFrame = useRef(0);
  const showingKey = body.showing?.key ?? 0;

  // ── Подглядывает из-за края (anim.edge: сбоку, сверху) ──
  // Анимация с краем началась — панель ставит голову так, чтобы линия из
  // кадра стояла на краю экрана (сбоку — у ближнего, нарисована для другого —
  // отражённой); пока не встала, кадр не показываем. Кончилась — назад
  const [peek, setPeek] = useState<{ key: number; side: 'left' | 'right' | 'top'; mirror: boolean } | null>(null);
  const peekKey = useRef(0);
  const headPxRef = useRef(0);
  useEffect(() => {
    // Страницу перезагрузили посреди подглядывания — панель ещё ждёт конца
    void invoke('portrait_peek_end').catch(() => {});
  }, []);
  const moving = body.moving;
  useEffect(() => {
    const sh = showingRef.current;
    const e = sh?.anim.edge;
    if (!sh || !e || moving) {
      if (peekKey.current) {
        peekKey.current = 0;
        setPeek(null);
        void invoke('portrait_peek_end').catch(() => {});
      }
      return;
    }
    // Линия — в px окна от левого (у top — верхнего) края головы: кадр стоит
    // серединой пола на середине низа головы (animAnchor, animSink)
    const S = headPxRef.current;
    const box = animBox(sh.anim, S);
    const left = S / 2 - animAnchor(sh.anim, S);
    const top = S + animSink(sh.anim, S) - box.h;
    const kx = box.w / Math.max(1, sh.anim.fw);
    const ky = box.h / Math.max(1, sh.anim.fh);
    const off = e.side === 'top' ? top + e.at * ky : left + e.at * kx;
    const offMirror = e.side === 'top' ? off : left + box.w - e.at * kx;
    peekKey.current = sh.key;
    setPeek(null);
    invoke<{ side: 'left' | 'right' | 'top'; mirror: boolean } | null>('portrait_peek', { side: e.side, off, offMirror })
      .then((r) => {
        if (peekKey.current !== sh.key) return;
        // Сейчас нельзя (в воздухе, в руке) — эту не играть
        if (!r) bodyRef.current?.onEnd();
        else setPeek({ key: sh.key, ...r });
      })
      .catch(() => {
        if (peekKey.current === sh.key) bodyRef.current?.onEnd();
      });
  }, [showingKey, moving]);
  useEffect(() => {
    const sh = showingRef.current;
    animFrame.current = 0;
    if (!sh) {
      animMaskRef.current = null;
      return;
    }
    let alive = true;
    void animMask(sh.anim).then((m) => {
      if (alive) animMaskRef.current = { key: sh.key, m };
    });
    return () => {
      alive = false;
    };
  }, [showingKey]);

  // Новая часть — рот ходит, пока идут её слова (служебная — молча)
  useEffect(() => {
    window.clearTimeout(talkTimer.current);
    if (!said || said.note) {
      setTalking(false);
      return;
    }
    setTalking(true);
    const ms = Math.min(2 * TALK_MS.max, Math.max(TALK_MS.min, typeMs(said.text, speed.current)));
    talkTimer.current = window.setTimeout(() => setTalking(false), ms);
  }, [said?.id]);

  // Слово за словом; облачко сразу во весь размер части (невидимые слова
  // занимают место) — не растёт и не прыгает
  useEffect(() => {
    if (!said) return;
    const id = said.id;
    const words = said.text.split(' ');
    const sp = speed.current;
    // «Сразу» — вся часть одним разом
    let n = sp.words_per_sec > 0 ? 1 : words.length;
    let timer = 0;
    setTyped({ id, n });
    const step = () => {
      if (n >= words.length) return;
      timer = window.setTimeout(() => {
        n += 1;
        setTyped({ id, n });
        step();
      }, wordMs(words[n], sp));
    };
    step();
    return () => window.clearTimeout(timer);
  }, [said?.id]);

  // Облачко — вопрос, который ждёт ответа кнопками: последняя часть его
  // реплики висит, пока не ответили
  const choiceHere = !!choice && !!said && !said.note && choice.texts.includes(said.src);
  const holdForChoice = choiceHere && lastPart;
  // Кнопки в самом облачке (на каждой части вопроса — ответить можно, не
  // дочитав): да/нет; список — когда стопке сбоку нет места
  const answersInBubble = choiceHere && !!choice && !choice.column;
  // Облачко — вопрос, варианты которого стопкой сбоку: не шире головы
  const choicesBeside = choiceHere && !!choice?.column && inputW > 0;

  // Часть висит, пока её можно прочесть, потом следующая: вас нет или курсор
  // над облачком — ждёт
  useEffect(() => {
    if (!said || away || reading || holdForChoice) return;
    const sp = speed.current;
    const timer = window.setTimeout(nextPart, typeMs(said.text, sp) + holdMs(said.text, lastPart ? READ_MS : PART_MS, sp));
    return () => window.clearTimeout(timer);
  }, [said, away, reading, lastPart, holdForChoice]);

  // Вопрос ждал ответа CHOICE_MS — кнопки убрать (ответить можно в чате)
  useEffect(() => {
    if (!choice) return;
    const timer = window.setTimeout(() => setChoice(null), Math.max(0, choice.until - Date.now()));
    return () => window.clearTimeout(timer);
  }, [choice]);

  // Ваше сообщение встало — персона читает его по длине текста, потом думает.
  // Фраза ещё звучит — не читает, а слушает; кончилась — читает
  useEffect(() => {
    window.clearTimeout(readTimer.current);
    if (!ask || ask.live) {
      setReadingAsk(false);
      return;
    }
    setReadingAsk(true);
    readTimer.current = window.setTimeout(() => setReadingAsk(false), span(ask.text, ASK_READ_MS));
  }, [ask?.id, !!ask?.live]);

  // Разговор голосом: окно открылось на имени — что уже идёт, спросить;
  // и слушает ли микрофон
  useEffect(() => {
    void invoke<VoiceTalk | null>('portrait_voice')
      .then((v) => setVoice(v ?? null))
      .catch(() => {});
    void invoke<VoiceView>('portrait_mic')
      .then(setMic)
      .catch(() => {});
  }, []);

  // История мини-чата — из ядра. Первый раз — когда окно открылось (или
  // сменили персону), дальше — ещё перед каждым показом мини-чата: если
  // первый запрос не удался (ядро поднималось), теперь догоним. Что уже
  // собралось само и в ядро ещё не легло (ход в работе), — остаётся поверх
  const fetchSeq = useRef(0);
  const fetchHistory = () => {
    const seq = ++fetchSeq.current;
    void invoke<unknown>('portrait_history')
      .then((rows) => {
        if (seq !== fetchSeq.current || !Array.isArray(rows)) return;
        const seed: ChatMsg[] = [];
        for (const m of rows) {
          if (!m || typeof m !== 'object') continue;
          const { role, content } = m as { role?: unknown; content?: unknown };
          if (typeof content !== 'string' || (role !== 'user' && role !== 'assistant')) continue;
          const text = content.trim();
          if (text) seed.push({ me: role === 'user', text });
        }
        setLog((local) => {
          // Хвост локальных сообщений — до первого, что уже в истории ядра;
          // служебные записки (не удалось спросить) не остаются
          const tail: ChatMsg[] = [];
          for (let i = local.length - 1; i >= 0 && i >= local.length - 6; i--) {
            const m = local[i];
            if (m.note) break;
            if (seed.some((s) => s.me === m.me && s.text === m.text)) break;
            tail.unshift(m);
          }
          return [...seed, ...tail];
        });
      })
      .catch((e) => {
        if (seq !== fetchSeq.current) return;
        console.error('portrait_history', e);
        // Видимой запиской — иначе тихое «Сообщений пока нет» ни о чём
        // не говорит; уйдёт при следующей успешной загрузке
        addLog(false, t('portraitSendFailed', { error: String(e) }), true);
      });
  };
  const fetchHistoryRef = useRef(fetchHistory);
  fetchHistoryRef.current = fetchHistory;
  useEffect(() => {
    fetchSeq.current++;
    setLog([]);
    logSeen.current.clear();
    pendingNotificationsRef.current = [];
    setPendingNotifications([]);
    notificationSeen.current.clear();
    figureHoverRef.current = false;
    if (!st?.persona) return;
    fetchHistoryRef.current();
  }, [st?.persona]);
  // Кнопка микрофона: выключить или включить слушание; ответ панели — сразу
  // на кнопку, не ждать её монитора
  const toggleMic = () => {
    if (micWait.current) return;
    micWait.current = true;
    void invoke<VoiceView>('portrait_mic_toggle')
      .then(setMic)
      .catch(() => {})
      .finally(() => {
        micWait.current = false;
      });
  };
  // Ваша фраза — облачком по ходу речи; кончилась и ушла персоне — то же
  // облачко, его она читает; ответ его уберёт, как у сообщения из поля
  useEffect(() => {
    if (!voiceOn) {
      voiceAsk.current = 0;
      setAsk((a) => (a?.voice && a.live ? null : a));
      return;
    }
    wake();
    // Места под облачко нет ни сбоку, ни сверху — без него (как у поля)
    if (!inputW && !bubbleH) return;
    if (voicePhase === 'hearing' && voiceText) {
      setAsk((a) => {
        if (a?.voice && a.id === voiceAsk.current) return a.live && a.text === voiceText ? a : { ...a, text: voiceText, live: true };
        voiceAsk.current = ++askId.current;
        return { id: voiceAsk.current, text: voiceText, at: Date.now(), voice: true, live: true };
      });
    } else if (voicePhase === 'thinking' && voiceText) {
      setAsk((a) => {
        if (a?.voice && a.id === voiceAsk.current) return !a.live && a.text === voiceText ? a : { ...a, text: voiceText, live: false };
        // Живого облачка не было (окно только открылось) — сразу итог
        voiceAsk.current = ++askId.current;
        return { id: voiceAsk.current, text: voiceText, at: Date.now(), voice: true };
      });
    } else if (voicePhase === 'speaking') {
      // Ответ пришёл так быстро, что «думает» не застали: фраза уже не звучит
      setAsk((a) => (a?.voice && a.live ? { ...a, live: false } : a));
    } else if (voicePhase === 'listening') {
      // Ждёт новую фразу: недослышанное (шум, вздох) — убрать
      voiceAsk.current = 0;
      setAsk((a) => (a?.voice && a.live ? null : a));
    }
  }, [voiceOn, voicePhase, voiceText, inputW, bubbleH]);

  useEffect(() => {
    if (!data) return;
    cell.current = { r: Math.floor(data.rows / 2), c: Math.floor(data.cols / 2) };
  }, [data]);

  // Облачко — над серединой головы, но в окне целиком; хвостик — к голове
  // Голову задвинули за край — не выше верха экрана и не за его боками
  // (видимая часть окна). Читает только ref — годится и подписке
  const placeBubble = () => {
    const el = bubbleRef.current;
    const head = rootRef.current?.querySelector('.portrait-head')?.getBoundingClientRect();
    if (!el || !head) return;
    const v = visRef.current;
    // Сбоку стопка вариантов — облачко над головой, в колонку не заходит
    const lo = Math.max((v?.left ?? 0) + EDGE, choicesBeside && side === 'left' ? head.left : 0);
    const hi = Math.min((v?.right ?? window.innerWidth) - EDGE, choicesBeside && side === 'right' ? head.right : Infinity);
    const w = el.offsetWidth;
    const mid = head.left + head.width / 2;
    const left = Math.max(lo, Math.min(hi - w, mid - w / 2));
    el.style.left = `${left}px`;
    el.style.setProperty('--tail-x', `${Math.max(10, Math.min(w - 10, mid - left))}px`);
    // Своё место — хвостиком к макушке (верх фигуры по маске, а не верх
    // кадра: над волосами в кадре прозрачно); верх ушёл за экран — ниже
    const crown = head.top + (mask.current?.top ?? 0) * head.height;
    const own = Math.min(crown, head.top + head.height / 2) - CROWN_GAP - el.offsetHeight;
    el.style.top = `${Math.max(own, (v?.top ?? 0) + EDGE)}px`;
    el.style.bottom = 'auto';
  };
  useLayoutEffect(placeBubble, [said?.id, inputW, side, bubbleH, vis, win, answersInBubble, choicesBeside]);

  // Ваше сообщение: сбоку — в колонке поля, у головы, на уровне глаз и не
  // ниже поля; поля нет — над головой, как реплика. Голову задвинули за край
  // — в видимой части окна. Отсюда же — куда персоне смотреть, читая
  const askTop = !inputW;
  // Мини-чат: при наведении — только кнопка чата и микрофон (док в колонке
  // поля); клик по кнопке разворачивает всё сразу — панель с историей,
  // полем и микрофоном, варианты ответа на вопрос тоже в ней
  const chatOn = chatOpen && fieldOn && inputW > 0 && !!data;
  const placeAsk = () => {
    const el = askEl.current;
    const head = rootRef.current?.querySelector('.portrait-head')?.getBoundingClientRect();
    if (!el || !head) {
      setReadAt(null);
      return;
    }
    const v = visRef.current;
    const lo = (v?.left ?? 0) + EDGE;
    const hi = (v?.right ?? window.innerWidth) - EDGE;
    const top0 = (v?.top ?? 0) + EDGE;
    const eyes = head.top + head.height * ASK_EYES;
    if (!askTop) {
      const colL = Math.max(lo, side === 'left' ? 0 : head.right);
      const colR = Math.min(hi, side === 'left' ? head.left : window.innerWidth);
      const w = Math.max(80, Math.min(ASK_MAX_W, colR - colL - 2 * ASK_PAD));
      el.style.width = `${w}px`;
      el.style.left = `${side === 'left' ? colR - ASK_PAD - w : colL + ASK_PAD}px`;
      const h = el.offsetHeight;
      // Открытый мини-чат занял колонку снизу — облачко не ниже его верха
      const panel = rootRef.current?.querySelector('.portrait-chat')?.getBoundingClientRect();
      const fieldTop = Math.min(
        Math.min(window.innerHeight - (v?.padB ?? 0), v?.bottom ?? Infinity) - FIELD_GAP - FIELD_H - ASK_PAD,
        panel ? panel.top - ASK_PAD : Infinity,
      );
      const top = Math.max(top0, Math.min(fieldTop - h, eyes - h / 2));
      el.style.top = `${top}px`;
      el.style.setProperty('--tail-y', `${Math.max(10, Math.min(h - 10, eyes - top))}px`);
    } else {
      el.style.width = '';
      const w = el.offsetWidth;
      const mid = head.left + head.width / 2;
      const left = Math.max(lo, Math.min(hi - w, mid - w / 2));
      el.style.left = `${left}px`;
      el.style.setProperty('--tail-x', `${Math.max(10, Math.min(w - 10, mid - left))}px`);
      const crown = head.top + (mask.current?.top ?? 0) * head.height;
      const own = Math.min(crown, head.top + head.height / 2) - CROWN_GAP - el.offsetHeight;
      el.style.top = `${Math.max(own, top0)}px`;
    }
    // Как курсор для LivePortrait: от середины головы, чуть выше центра кадра
    const r = el.getBoundingClientRect();
    const range = Math.max(260, head.width * 1.1);
    const x = Math.max(-1, Math.min(1, (r.left + r.width / 2 - (head.left + head.width / 2)) / range));
    const y = Math.max(-1, Math.min(1, (r.top + r.height / 2 - (head.top + head.height * 0.45)) / range));
    setReadAt((prev) => (prev && Math.abs(prev.x - x) < 0.01 && Math.abs(prev.y - y) < 0.01 ? prev : { x, y }));
  };
  // Над головой — только пока там нет реплики персоны. Мини-чат открыт —
  // облачко всё равно встаёт: над его элементами, чтобы сообщение было
  // видно сразу
  const askShown = !!ask && !(askTop && said);
  // Стопка вариантов — в колонке поля, низом над полем (и кнопкой
  // микрофона), не выше головы; не влезли — листаются. Голову задвинули за
  // край — на видимой части окна. Мини-чат открыт — стопка в нём
  const choicesShown = !!choice?.column && inputW > 0 && !!data && !chatOn;
  const placeChoices = () => {
    const el = choicesEl.current;
    const head = rootRef.current?.querySelector('.portrait-head')?.getBoundingClientRect();
    if (!el || !head) return;
    const v = visRef.current;
    const lo = (v?.left ?? 0) + EDGE;
    const hi = (v?.right ?? window.innerWidth) - EDGE;
    const colL = Math.max(lo, side === 'left' ? 0 : head.right);
    const colR = Math.min(hi, side === 'left' ? head.left : window.innerWidth);
    const w = Math.max(80, Math.min(CHOICE_MAX_W, colR - colL - 2 * ASK_PAD));
    el.style.width = `${w}px`;
    el.style.left = `${side === 'left' ? colR - ASK_PAD - w : colL + ASK_PAD}px`;
    const bottom =
      Math.min(window.innerHeight - (v?.padB ?? 0), v?.bottom ?? Infinity) - FIELD_GAP - FIELD_H - ASK_PAD;
    // Облачко с вопросом над головой в колонку не заходит — стопке вся высота
    const top = (v?.top ?? 0) + EDGE;
    el.style.maxHeight = `${Math.max(40, bottom - top)}px`;
    el.style.top = 'auto';
    el.style.bottom = `${window.innerHeight - bottom}px`;
  };
  useLayoutEffect(placeChoices, [choice?.id, choicesShown, side, bubbleH, vis, win]);
  // Мини-чат — в колонке поля, на месте поля и дока: не шире колонки, низом
  // на линии поля. История раскрывается вверх и растит панель, пока есть
  // место до верха видимой части окна (потолок истории — в CSS); дальше —
  // листается. Голову задвинули за край — на видимой части окна
  const placeChat = () => {
    const el = chatEl.current;
    const head = rootRef.current?.querySelector('.portrait-head')?.getBoundingClientRect();
    if (!el || !head) return;
    const v = visRef.current;
    const lo = (v?.left ?? 0) + EDGE;
    const hi = (v?.right ?? window.innerWidth) - EDGE;
    const colL = Math.max(lo, side === 'left' ? 0 : head.right);
    const colR = Math.min(hi, side === 'left' ? head.left : window.innerWidth);
    const w = Math.max(120, Math.min(Math.max(inputW, 200), colR - colL - 2 * ASK_PAD));
    el.style.width = `${w}px`;
    el.style.left = `${side === 'left' ? colR - ASK_PAD - w : colL + ASK_PAD}px`;
    const bottom = Math.min(window.innerHeight - (v?.padB ?? 0), v?.bottom ?? Infinity) - FIELD_GAP;
    const top = (v?.top ?? 0) + EDGE;
    el.style.maxHeight = `${Math.max(90, bottom - top)}px`;
    el.style.top = 'auto';
    el.style.bottom = `${window.innerHeight - bottom}px`;
  };
  useLayoutEffect(placeChat, [chatOn, side, bubbleH, vis, win, inputW]);
  // Голосом текст растёт по ходу речи — облачко выше, место заново; мини-чат
  // меняет высоту (история раскрылась, пришли сообщения) — тоже
  useLayoutEffect(placeAsk, [ask?.id, ask?.text, askShown, askTop, side, bubbleH, vis, win, chatOn, histOpen, log.length]);
  // Новое сообщение или раскрыли историю — к последнему
  useEffect(() => {
    const el = chatLogEl.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log.length, chatOn, histOpen]);
  // Раскрыли мини-чат или историю — освежить её из ядра
  useEffect(() => {
    if (chatOn) fetchHistoryRef.current();
  }, [chatOn, histOpen]);
  useEffect(() => {
    // Колёсиком меняют размер — окно другое
    const onResize = () => setWin({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener('resize', onResize);
    const un = listen<Visible>('portrait-visible', (e) => setVis(e.payload));
    return () => {
      window.removeEventListener('resize', onResize);
      void un.then((f) => f());
    };
  }, []);
  // Видимая часть — сразу и после перезагрузки (сменили персону, поле, облачка)
  useEffect(() => {
    void invoke<Visible | null>('portrait_visible').then((v) => v && setVis(v));
  }, [reload]);

  // Поле на видимой части окна: не ниже FIELD_GAP от низа экрана, не выше
  // верха, не за боками; окно на экране — на своём месте у головы
  const fieldShift = (() => {
    if (!vis || !inputW) return null;
    const left = layout ? (side === 'left' ? layout.headX - inputW : layout.headX + layout.headSize)
      : side === 'left' ? padL : win.w - padR - inputW;
    const bottom = (layout ? layout.headY + layout.headSize : win.h - padB) - FIELD_GAP;
    const x = Math.min(Math.max(left, vis.left), vis.right - inputW) - left;
    const y = Math.max(Math.min(bottom, vis.bottom - FIELD_GAP), vis.top + EDGE + FIELD_H) - bottom;
    return x || y ? `translate(${x}px, ${y}px)` : null;
  })();

  // Облачка выключили в меню — убрать показанное
  useEffect(() => {
    if (!bubbleH) setParts([]);
  }, [bubbleH]);

  // ── Поле для сообщений ──
  const focusInput = () => {
    void invoke('portrait_focus', { on: true }).then(() => inputRef.current?.focus());
  };
  const leaveInput = () => {
    inputRef.current?.blur();
    void invoke('portrait_focus', { on: false });
  };
  // Навели — панель видна; ушли — живёт FIELD_MS от последнего нажатия
  // (draft), потом прячется, отдаёт клавиатуру и сворачивается обратно
  // в кнопку чата; набранное остаётся
  useEffect(() => {
    if (hover) {
      setFieldOn(true);
      return;
    }
    if (!fieldOn) return;
    const timer = window.setTimeout(() => {
      if (document.activeElement === inputRef.current) leaveInput();
      setFieldOn(false);
      setChatOpen(false);
      setHistOpen(false);
    }, FIELD_MS);
    return () => window.clearTimeout(timer);
  }, [hover, fieldOn, typing, draft, playerActivity]);

  // Реплика персоне портрета (поле или кнопка ответа); shown — что встаёт
  // облачком сбоку (у кнопки — подпись варианта, а уходит номер)
  const deliver = async (text: string, shown: string, fromField: boolean) => {
    setSending(true);
    wake();
    nodNow();
    setChoice(null);
    showAsk(shown, false, shown === text ? undefined : text);
    // В историю мини-чата — что ушло персоне (как в истории ядра); облачко
    // сбоку уже показало его (showAsk выше)
    addLog(true, text);
    try {
      const replies = await invoke<string[]>('portrait_send', { text });
      // Обычно ответ уже пришёл лентой — повтор enqueue отсеет
      replies.forEach((r) => {
        enqueue(r);
        logSaid(r);
      });
      if (replies.some((r) => r.trim())) setAsk(null);
    } catch (e) {
      const err = String(e);
      // Не ушло — сообщения у персоны нет, набранное снова в поле
      setAsk(null);
      if (fromField) setDraft((cur) => cur || text);
      const msg = err === 'offline' ? t('portraitSendOffline') : t('portraitSendFailed', { error: err });
      enqueue(msg, true);
      addLog(false, msg, true);
    } finally {
      setSending(false);
    }
  };
  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setDraft('');
    await deliver(text, text, true);
  };
  // Кнопка ответа: вопрос отвечен — облачко с ним убрать, ответ уходит
  // обычной репликой
  const roleLabel = (o: AnswerOption) =>
    t(o.role === 'yes' ? 'portraitAnswerYes' : o.role === 'no' ? 'portraitAnswerNo' : 'portraitAnswerCancel');
  const answer = (o: AnswerOption) => {
    const text = answerSend(o);
    const cur = choice;
    if (!text || sending || !cur) return;
    setParts((prev) => prev.filter((p) => !cur.texts.includes(p.src)));
    void deliver(text, o.label ?? roleLabel(o), false);
  };
  // Кнопки вариантов ответа — стопкой: в колонке поля или в мини-чате
  const choiceList = (opts: AnswerOption[]) =>
    opts.map((o, i) => {
      // Хвост подписи (цена, адрес) — своей строкой, не режется
      const [name, tail] = o.label ? labelTail(o.label) : [roleLabel(o), null];
      return (
        <button key={i} type="button" className="portrait-choice" disabled={sending} title={o.label} onClick={() => answer(o)}>
          {o.label && <span className="portrait-choice-n">{o.send ?? i + 1}</span>}
          <span className="portrait-choice-body">
            <span className={tail ? 'portrait-choice-text portrait-choice-text--short' : 'portrait-choice-text'}>{name}</span>
            {tail && <span className="portrait-choice-tail">{tail}</span>}
          </span>
        </button>
      );
    });

  // ── Над фигурой ли точка окна ──
  // Играет анимация — по альфе её текущего кадра (маска ещё строится — по
  // рамке кадра); иначе — по листу A головы
  const onFigure = (x: number, y: number): boolean => {
    const sh = showingRef.current;
    if (sh) {
      const el = rootRef.current?.querySelector('.portrait-anim');
      if (!el) return false;
      const r = el.getBoundingClientRect();
      // Срезанное у низа экрана — не фигура: клик проходит к окну под ней
      if (floorClipRef.current > 0 && y > r.bottom - floorClipRef.current) return false;
      const ec = edgeClipRef.current;
      if (ec && (ec.side === 'top' ? y < r.top + ec.at : ec.side === 'left' ? x < r.left + ec.at : x > r.left + ec.at)) return false;
      const v = (y - r.top) / r.height;
      let u = (x - r.left) / r.width;
      if (sh.mirror) u = 1 - u;
      const mk = animMaskRef.current;
      if (mk?.key === sh.key && mk.m) return animHit(mk.m, animFrame.current, u, v);
      return u >= 0 && u < 1 && v >= 0 && v < 1;
    }
    return hitOnBody(mask.current, cell.current, rootRef.current?.querySelector('.live-portrait-body'), x, y);
  };

  const solidAt = (x: number, y: number): boolean => {
    if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return false;
    if (document.elementFromPoint(x, y)?.closest('[data-solid]')) return true;
    return onFigure(x, y);
  };

  // Курсор над облачком — часть не листается. По курсору от панели, а не
  // pointerleave: окно, пропускающее мышь насквозь, ухода курсора не видит,
  // и облачко зависло бы. Читает только ref — годится и подписке
  const checkReading = () => {
    const c = cursorAt.current;
    const r = rootRef.current?.querySelector('.portrait-bubble')?.getBoundingClientRect();
    const over = !!c && !!r && c.x >= r.left && c.x < r.right && c.y >= r.top && c.y < r.bottom;
    if (over !== readingRef.current) {
      readingRef.current = over;
      setReading(over);
    }
  };

  // Сменилась часть (или облачко ушло), курсор стоит — над ней ли он теперь
  useEffect(checkReading, [said?.id]);

  const setHit = (next: boolean) => {
    if (next === hit.current) return;
    hit.current = next;
    void invoke('portrait_hit', { hit: next });
  };

  useEffect(() => {
    const un = listen<{ x: number; y: number }>('portrait-cursor', (e) => {
      const { x, y } = e.payload;
      // Взгляд — на курсор, где бы он ни был: LivePortrait слушает pointermove
      // окна. Спит или говорит — не смотрит, и кадры на это не тратим
      if (!noFollow.current) window.dispatchEvent(new PointerEvent('pointermove', { clientX: x, clientY: y }));
      setHit(press.current !== null || solidAt(x, y));
      cursorAt.current = { x, y };
      checkReading();
      // Поле для сообщений и кнопка микрофона — пока курсор над фигурой,
      // над полем или над кнопкой
      const inside = (sel: string) => {
        const r = rootRef.current?.querySelector(sel)?.getBoundingClientRect();
        return !!r && x >= r.left && x < r.right && y >= r.top && y < r.bottom;
      };
      const overFigure = onFigure(x, y);
      const over = overFigure || inside('.portrait-input') || inside('.portrait-mic') || inside('.portrait-choices') || inside('.portrait-chat') || inside('.portrait-dock') || inside('.avatar-mini-player');
      if (over !== hoverRef.current) {
        hoverRef.current = over;
        setHover(over);
      }
      if (overFigure && !figureHoverRef.current && pendingNotificationsRef.current.length) {
        const notifications = pendingNotificationsRef.current;
        pendingNotificationsRef.current = [];
        setPendingNotifications([]);
        bodyRef.current?.play('message');
        for (const text of notifications) enqueue(text);
      }
      figureHoverRef.current = overFigure;
    });
    return () => {
      void un.then((f) => f());
    };
    // solidAt и setHit читают только ref — подписка одна на всё время окна
  }, []);

  const onWheel = (e: WheelEvent) => {
    const w = wheel.current;
    w.acc += e.deltaY;
    if (w.timer) return;
    w.timer = window.setTimeout(() => {
      const factor = Math.min(1.25, Math.max(0.8, Math.exp(-w.acc * 0.0015)));
      w.acc = 0;
      w.timer = 0;
      void invoke('portrait_resize', { factor });
    }, WHEEL_MS);
  };

  // Персона есть — есть и портрет: свой или голова фигуры
  const hint = !st ? null : !st.persona ? t(st.offline ? 'portraitWaitBot' : 'portraitNone') : st.gone ? t('portraitGone') : null;
  // Что делает персона — одно и то же для портрета из листов и головы фигуры
  // Брошенный файл — «читает» его, пока не ответит (а не только по длине имени)
  const fileReading = fileWait && !!ask?.file && askShown && !!readAt;
  const cues = {
    petted,
    asleep,
    speaking: speakingNow,
    // Разговор голосом: ждёт фразу или слышит её — смотрит на вас
    listening: voiceListens && !speakingNow,
    // «Посиди со мной» — занята своим, на курсор не отвлекается
    attentive: attentive || !!focus,
    thinking: thinkingNow && !fileReading,
    reading: (readingAsk || fileReading) && askShown ? readAt : null,
    nod,
  };
  // Сторона головы, px (как --head в portrait.css) — под размер кадра анимации
  const headPx = Math.max(1, layout?.headSize ?? Math.min(win.w - inputW - padL - padR, win.h - bubbleH - padT - padB));
  // Кнопка микрофона: как слушатель сейчас (строкой, не работает — и
  // почему) и что будет по клику
  const micTone = mic ? voiceTone(mic) : 'off';
  let micTitle = '';
  if (mic && !mic.enabled) {
    micTitle = t(mic.phase === 'stopping' ? 'portraitMicStopping' : 'portraitMicOff');
  } else if (mic) {
    const now = voiceOn && micTone === 'ok' ? t('portraitMicTalk') : micStateText(mic);
    const why = micTone === 'bad' ? voiceError(mic) : null;
    micTitle = [t('portraitMicOn', { state: now }), why, t('portraitMicTurnOff')].filter(Boolean).join('\n');
  }
  // place — где стоит: у головы без поля (portrait-mic--left/right), в доке
  // у поля (portrait-mic--dock) или в мини-чате (portrait-mic--chat)
  const micButton = (place: string, shifted = false) =>
    data && mic && (fieldOn || voiceOn) && (
      <button
        type="button"
        className={['portrait-mic', place, `portrait-mic--${micTone}`, micTone === 'ok' && voicePhase === 'hearing' ? 'portrait-mic--on' : '']
          .filter(Boolean)
          .join(' ')}
        style={shifted && fieldShift ? { transform: fieldShift } : undefined}
        data-solid
        aria-pressed={mic.enabled}
        aria-label={micTitle}
        title={micTitle}
        onClick={toggleMic}
      >
        <svg viewBox="0 0 12 12" aria-hidden>
          <rect x="4.25" y="1" width="3.5" height="6.5" rx="1.75" />
          <path d="M2.5 5.75a3.5 3.5 0 0 0 7 0M6 9.25V11" />
          {!mic.enabled && <path className="portrait-mic-slash" d="M1.5 1.5 10.5 10.5" />}
        </svg>
      </button>
    );
  const showing = body.showing;
  // Низ экрана поднят (anim.fix.floor): что ниже пола — под краем. У низа
  // экрана это и так за краем, а на окне — срезать, будто за его кромкой.
  // В воздухе и в руке не срезать: под пол уходит и сама фигура (поворот)
  const floorClip =
    showing && (showing.anim.fix?.floor ?? 0) > 0 && !['fall', 'jump', 'held'].includes(body.motion.state) ? animSink(showing.anim, headPx) : 0;
  floorClipRef.current = floorClip;
  headPxRef.current = headPx;
  // Подглядывает: кадр — когда панель поставила голову к краю; что за линией —
  // срезать (между мониторами за краем — другой экран), отражённая — с другой стороны
  const edge = showing?.anim.edge ?? null;
  const peeking = !!edge && !!peek && peek.key === showing?.key;
  const edgeMirror = peeking && !!peek?.mirror;
  let edgeClip: string | undefined;
  edgeClipRef.current = null;
  if (showing && edge && peek && peeking) {
    const box = animBox(showing.anim, headPx);
    const at = edge.side === 'top' ? (edge.at * box.h) / Math.max(1, showing.anim.fh) : (edge.at * box.w) / Math.max(1, showing.anim.fw);
    // clip-path — до отражения (scaleX(-1) у кадра): сторона в кадре — нарисованная
    if (edge.side === 'top') edgeClip = `inset(${at}px 0 0 0)`;
    else if (edge.side === 'left') edgeClip = `inset(0 0 0 ${at}px)`;
    else edgeClip = `inset(0 ${box.w - at}px 0 0)`;
    // Для кликов — на экране: где линия от левого (верхнего) края кадра и что за ней
    edgeClipRef.current = { side: peek.side, at: edge.side === 'top' ? at : edgeMirror ? box.w - at : at };
  }
  const focusLeftMin = focus ? Math.max(0, Math.ceil((focus.until - focusNow) / 60_000)) : 0;
  const focusDone = focus ? Math.min(1, Math.max(0, 1 - (focus.until - focusNow) / (focus.minutes * 60_000))) : 0;

  return (
    <div
      ref={rootRef}
      className={`${inputW > 0 ? `portrait-overlay portrait-overlay--field-${side}` : 'portrait-overlay'}${layout ? ' portrait-overlay--anchored' : ''}`}
      style={{
        ['--head' as string]: layout ? `${layout.headSize}px` : undefined,
        ['--head-x' as string]: layout ? `${layout.headX}px` : undefined,
        ['--head-y' as string]: layout ? `${layout.headY}px` : undefined,
        ['--bubble-h' as string]: `${bubbleH}px`,
        ['--input-w' as string]: `${inputW}px`,
        ['--pad-l' as string]: `${padL}px`,
        ['--pad-t' as string]: `${padT}px`,
        ['--pad-r' as string]: `${padR}px`,
        ['--pad-b' as string]: `${padB}px`,
      }}
      onPointerDown={(e) => {
        // Облачко и подсказка — свои кнопки; без портрета ловить нечего
        if (e.button !== 0 || !data || (e.target as Element).closest('[data-solid]')) return;
        if (!onFigure(e.clientX, e.clientY)) return;
        press.current = { x: e.clientX, y: e.clientY };
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        const p = press.current;
        if (!p || Math.hypot(e.clientX - p.x, e.clientY - p.y) <= DRAG_PX) return;
        press.current = null;
        void invoke('portrait_drag');
      }}
      onPointerUp={() => {
        const p = press.current;
        press.current = null;
        if (p && data) pat(p.x, p.y, rootRef.current?.querySelector('.live-portrait'));
      }}
      onPointerCancel={() => {
        press.current = null;
      }}
      onContextMenu={(e) => {
        // В поле — меню поля (вставить, проверка написания)
        if ((e.target as Element).closest('input')) return;
        e.preventDefault();
        if (solidAt(e.clientX, e.clientY)) void invoke('portrait_menu');
      }}
      onWheel={(e) => {
        if (data && onFigure(e.clientX, e.clientY)) onWheel(e);
      }}
    >
      {said && bubbleH > 0 && (
        <div className="portrait-bubble-area">
          <div
            key={said.id}
            ref={bubbleRef}
            className={[
              'portrait-bubble',
              said.note ? 'portrait-bubble--note' : '',
              answersInBubble ? 'portrait-bubble--answers' : '',
              choicesBeside ? 'portrait-bubble--beside' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            data-solid
            role="button"
            tabIndex={-1}
            title={t(lastPart && !said.note ? 'portraitBubbleOpen' : 'portraitBubbleNext')}
            onClick={() => {
              // Не последняя часть — следующая; последняя — чат в вебе
              if (!lastPart || said.note) return nextPart();
              setParts([]);
              void invoke('portrait_open_chat');
            }}
          >
            <span className="portrait-bubble-text" aria-label={said.text}>
              {saidWords.map((w, i) => (
                <span key={i} className={i < shownWords ? undefined : 'portrait-word--hidden'}>
                  {i ? ' ' : ''}
                  {w}
                </span>
              ))}
            </span>
            {answersInBubble && choice && (
              <span className="portrait-answers">
                {choice.options.map((o, i) => (
                  <button
                    key={i}
                    type="button"
                    className="portrait-answer"
                    disabled={sending}
                    // Варианты номерами (стопке сбоку нет места) — подпись подсказкой
                    title={o.label ? `${o.send ?? i + 1}. ${o.label}` : undefined}
                    onClick={(e) => {
                      e.stopPropagation();
                      answer(o);
                    }}
                  >
                    {o.label ? (o.send ?? String(i + 1)) : roleLabel(o)}
                  </button>
                ))}
              </span>
            )}
            {said.of > 1 && (
              <span className="portrait-bubble-count" aria-hidden>
                {said.n}/{said.of}
              </span>
            )}
            <button
              type="button"
              className="portrait-bubble-close"
              aria-label={t('portraitBubbleClose')}
              onClick={(e) => {
                e.stopPropagation();
                setParts([]);
                // Скрыли вопрос — и его кнопки (ответить можно в чате)
                if (choiceHere) setChoice(null);
              }}
            >
              <svg viewBox="0 0 10 10" aria-hidden>
                <path d="M2 2 8 8M8 2 2 8" />
              </svg>
            </button>
          </div>
        </div>
      )}
      {choicesShown && choice && (
        <div
          key={choice.id}
          ref={choicesEl}
          className={`portrait-choices portrait-choices--${side}`}
          data-solid
          role="group"
          aria-label={t('portraitAnswers')}
        >
          {choiceList(choice.options)}
        </div>
      )}
      {chatOn && (
        <div ref={chatEl} className={`portrait-chat portrait-chat--${side}`} data-solid>
          {histOpen && (
            <div ref={chatLogEl} className="portrait-chat-log" role="log" aria-label={t('portraitChatLog')}>
              {log.length === 0 ? (
                <div className="portrait-chat-empty">{t('portraitChatEmpty')}</div>
              ) : (
                log.map((m, i) => (
                  <div
                    key={i}
                    className={['portrait-msg', m.me ? 'portrait-msg--me' : 'portrait-msg--persona', m.note ? 'portrait-msg--note' : '']
                      .filter(Boolean)
                    .join(' ')}
                  >
                    {m.text}
                  </div>
                ))
              )}
            </div>
          )}
          {choice?.column && (
            <div className="portrait-choices portrait-choices--chat" role="group" aria-label={t('portraitAnswers')}>
              {choiceList(choice.options)}
            </div>
          )}
          <div className="portrait-chat-tools">
            <button
              type="button"
              className="portrait-chat-fold"
              aria-label={t('portraitChatClose')}
              title={t('portraitChatClose')}
              onClick={() => {
                if (typing) leaveInput();
                setChatOpen(false);
                setHistOpen(false);
              }}
            >
              <svg viewBox="0 0 12 12" aria-hidden>
                <path d="M2.5 2.5 9.5 9.5M9.5 2.5 2.5 9.5" />
              </svg>
            </button>
            <button
              type="button"
              className="portrait-chat-hist"
              aria-pressed={histOpen}
              aria-label={histOpen ? t('portraitChatHideHist') : t('portraitChatShowHist')}
              title={histOpen ? t('portraitChatHideHist') : t('portraitChatShowHist')}
              onClick={() => setHistOpen((v) => !v)}
            >
              <svg viewBox="0 0 12 12" aria-hidden>
                {histOpen ? <path d="M2.5 7.25 6 3.75l3.5 3.5" /> : <path d="M2.5 4.75 6 8.25l3.5-3.5" />}
              </svg>
            </button>
          </div>
          <form
            className="portrait-input"
            data-solid
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <input
              ref={inputRef}
              value={draft}
              maxLength={4000}
              placeholder={t('portraitInput')}
              aria-label={t('portraitInput')}
              onChange={(e) => setDraft(e.target.value)}
              // Окно фокус не берёт, пока в поле не щёлкнули
              onPointerDown={() => {
                if (!typing) focusInput();
              }}
              onFocus={() => setTyping(true)}
              onBlur={() => setTyping(false)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.preventDefault();
                  leaveInput();
                }
              }}
            />
            <button type="submit" disabled={!draft.trim() || sending} aria-label={t('portraitSend')} title={t('portraitSend')}>
              {sending ? (
                <span className="portrait-input-wait" aria-hidden>
                  …
                </span>
              ) : (
                <svg viewBox="0 0 12 12" aria-hidden>
                  <path d="M6 10V2M2.5 5.5 6 2l3.5 3.5" />
                </svg>
              )}
            </button>
            {mic && micButton('portrait-mic--chat')}
          </form>
        </div>
      )}
      {askShown && (
        <div
          key={ask.id}
          ref={askEl}
          className={`portrait-ask ${askTop ? 'portrait-ask--top' : `portrait-ask--${side}`}`}
          aria-label={ask.text}
        >
          {ask.file && (
            <svg className="portrait-ask-file" viewBox="0 0 10 12" aria-label={t('portraitFile')}>
              <path d="M1.5 1h4.5l2.5 2.5V11h-7zM6 1v2.5h2.5" />
            </svg>
          )}
          <span className="portrait-ask-text">{ask.text}</span>
        </div>
      )}
      <div className="portrait-row">
        <div
          className={[
            'portrait-head',
            body.fx ? `portrait-head--${body.fx}` : '',
            showing ? 'portrait-head--anim' : '',
          ]
            .filter(Boolean)
            .join(' ')}
        >
          {pendingNotifications.length > 0 && (
            <InitiativeBadgeMark className="portrait-initiative-mark" badge={st?.initiativeBadge} label={pendingNotifications.join('\n\n')} />
          )}
          {data && !head && (
            <LivePortrait
              data={data}
              {...cues}
              emotion={emotion}
              speech={speech}
              label={st?.name ?? ''}
              onCell={(r, c) => {
                cell.current = { r, c };
              }}
            />
          )}
          {data && head && (
            <div className={`live-portrait ${talking ? 'live-portrait--speaking' : ''} ${asleep ? 'live-portrait--asleep' : ''}`}>
              <div className="live-portrait-body">
                <LiveHead
                  preset={head}
                  look={skinLook ?? null}
                  {...cues}
                  accent={NEUTRAL_ACCENT}
                  label={st?.name ?? ''}
                  onGaze={(x, y) => {
                    // Клетка листа 5×5 под маску — как у LivePortrait
                    cell.current = { r: Math.round((y + 1) * 2), c: Math.round((x + 1) * 2) };
                  }}
                />
              </div>
            </div>
          )}
          {data && showing && (
            <AnimSprite
              key={showing.key}
              anim={showing.anim}
              size={headPx}
              loop={showing.loop}
              mirror={showing.mirror !== edgeMirror}
              onEnd={body.onEnd}
              onFrame={(i) => {
                animFrame.current = i;
              }}
              travel={showing.walk ? WALK_TRAVEL : undefined}
              className="portrait-anim"
              style={{
                left: `calc(50% - ${animAnchor(showing.anim, headPx)}px)`,
                bottom: -animSink(showing.anim, headPx),
                clipPath: edgeClip ?? (floorClip > 0 ? `inset(0 0 ${floorClip}px 0)` : undefined),
                // Ждёт, пока панель поставит голову к краю
                visibility: edge && !peeking ? 'hidden' : undefined,
              }}
            />
          )}
          {data && asleep && !showing && (
            <div className="portrait-z" aria-hidden>
              <span>z</span>
              <span>z</span>
            </div>
          )}
          {data && focus && (
            <div
              className="portrait-focus"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(focusDone * 100)}
              aria-label={t('portraitFocusLeft', { min: focusLeftMin })}
              title={t('portraitFocusLeft', { min: focusLeftMin })}
            >
              <span style={{ width: `${focusDone * 100}%` }} />
            </div>
          )}
          {inputW === 0 && micButton(`portrait-mic--${side}`)}
          <div className="portrait-player"><PortraitPlayer visible={fieldOn} hidden={controlsHidden} onActivity={wakePlayer} /></div>
          {data && inputW > 0 && !chatOn && (chatButtonOn || (!!mic && voiceOn)) && (
            <div
              className="portrait-dock"
              style={fieldShift ? { transform: fieldShift } : undefined}
              data-solid
            >
              {chatButtonOn && (
                <button
                  type="button"
                  className="portrait-dock-chat"
                  aria-label={t('portraitChatOpen')}
                  title={t('portraitChatOpen')}
                  onClick={() => setChatOpen(true)}
                >
                  <svg viewBox="0 0 12 12" aria-hidden>
                    <path d="M1.5 2h9v6H6.5L4 10.5V8H1.5z" />
                  </svg>
                </button>
              )}
              {mic && voiceOn && micButton('portrait-mic--dock')}
            </div>
          )}
        </div>
      </div>
      {hint && (
        <button type="button" className="portrait-hint" data-solid onClick={() => void invoke('portrait_menu')}>
          <span>{hint}</span>
          <span className="portrait-hint-dim">{t('portraitMenu')}</span>
        </button>
      )}
      <PatHearts hearts={hearts} />
    </div>
  );
}
