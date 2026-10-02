/* Билдеры снапшотов данных, которые хост отправляет скину через
   postMessage (сообщение {vpc:'host', type:'state', payload}). Их же
   использует SkinPanel для предпросмотра на моковых данных. */

import type {
  ChatMessage,
  DiaryEntry,
  InitiativeEvent,
  InitiativeState,
  InventoryItem,
  LearningSession,
  LlmProvider,
  LtmFact,
  Persona,
  PersonaFile,
  Reminder,
  RoomActivity,
  StmMessage,
  TodoItem,
} from '../mockData';

const COURSE_TOTAL = 20; // моковая длительность курса (как в LearningPanel)

export interface SkinPersonaInfo {
  id: string;
  name: string;
  statusText: string;
  model: string;
  mood?: string;
  pastime?: string;
  avatar?: string; // dataURL, если есть
}

export interface SkinChatMessage {
  id: number;
  role: 'user' | 'persona';
  text: string;
  time: string;
  image?: string;
  quote?: { author: string; text: string };
}

// Элемент левого сайдбара чата — список персон
export interface SkinPersonaListItem {
  id: string;
  name: string;
  statusText: string;
  active: boolean; // выбранная сейчас персона
}

// Правый сайдбар чата — контекстная панель (все строки уже локализованы хостом)
export interface SkinContext {
  pastime: string; // «пишет в дневник · за столом»
  mood: string;
  trend: string;
  initiative: string; // строка вероятности самоинициативы
  lastReply: string;
  nextReminder: string;
  learning: string; // строка об активном курсе ('' — нет)
  features: string[]; // подписи включённых модулей
}

// Досье персоны: списки данных + записываемые через действия сущности
export interface SkinDossier {
  facts: { id: number; category: string; fact: string }[];
  reminders: {
    id: number;
    time: string;
    text: string;
    repeat: string;
    active: boolean;
    date: string; // часть «когда» до запятой ('' — нет) — для формы правки
    clock: string; // часть «время» после запятой
  }[];
  initiatives: { typeLabel: string; text: string; time: string; outcome: string }[];
  diary: { date: string; text: string }[];
  // STM-буфер: последние реплики диалога (вкладка «Память»)
  stm: { id: number; role: 'user' | 'persona'; author: string; text: string; time: string }[];
  // Учебные курсы «Научи меня» (вкладка «Обучение»)
  courses: {
    id: number;
    subject: string;
    status: string; // локализованная подпись состояния
    statusKey: 'active' | 'paused' | 'finished';
    lessons: number;
    topics: number; // покрыто тем
    words: number; // слов в словаре
    quiz: number; // неотвеченных вопросов теста
    frequency: string;
    next: string; // следующий урок, человекочитаемо
    progress: number; // % прохождения курса (для прогресс-бара)
    topicsList: string; // покрытые темы, одной строкой
    vocabList: string; // словарь, одной строкой
    quizLine: string; // «тест ждёт ответов · N» ('' — теста нет)
  }[];
  // Параметры и текущее состояние самоинициативы (вкладка «Инициатива»)
  initState: {
    probability: number; // вероятность, %
    threshold: number; // порог молчания, мин
    maxPerDay: number;
    interval: number; // интервал проверки, мин
    adaptive: string; // '✓' / '—'
    bayes: string;
    ignoreStreak: number;
    today: number; // инициатив сегодня
    mood: string; // текущее эмоциональное состояние
    stages: string; // шкала состояний, одной строкой
    silencePct: number; // прогресс молчания пользователя, %
    silenceText: string; // локализованная подпись прогресса
  };
}

// Файл персоны (досье: вкладка «Файлы»)
export interface SkinFile {
  id: number;
  name: string;
  kind: string;
  size: string;
  date: string;
  description: string;
}

// Провайдер моделей (досье: список провайдеров + выбор модели)
export interface SkinProvider {
  id: string;
  name: string;
  local: boolean;
  model: string;
  active: boolean; // основной провайдер
  backup: boolean; // «на подхвате»
  keyLabel: string; // «ключ задан» / «ключ не задан» / '' (локальный)
}

// Флаг фичи (досье: настройки)
export interface SkinFeatureFlag {
  id: string;
  label: string;
  enabled: boolean;
}

export interface SkinRoomState {
  pastimeLabel: string;
  pastimePlace: string;
  duration: string;
  x: number; // позиция аватара, % сцены слева
  y?: number | null; // % сверху (если задан пользовательским фоном)
  mood: string;
  energy: string;
  pet: string;
  petLabel?: string;
  bg?: string | null; // dataURL пользовательского фона
  sprite?: string | null; // dataURL спрайта аватара
  // Живое присутствие (необязательные, старые скины их игнорируют)
  spot?: string; // место в комнате: desk | window | shelf | bed | chair | floor | away | item:<название>
  pose?: string; // stand | sit | read | write | look | sleep | away | with_you | glance
  timeOfDay?: SkinTimeOfDay; // дублирует env.timeOfDay — для скинов без env
  pastimeSince?: number | null; // epoch, сек — с какого момента длится занятие
}

// Время суток пользователя (по локальным часам браузера)
export type SkinTimeOfDay = 'morning' | 'day' | 'evening' | 'night';

// Погода из строки окружения бэкенда (GET /api/settings/env-preview → line)
export interface SkinWeather {
  text: string; // «Москва: +7°C, light rain, wind 4 m/s» — как отдаёт бэкенд
  city?: string;
  tempC?: number;
  condition?: 'clear' | 'cloudy' | 'fog' | 'rain' | 'snow' | 'storm';
}

// Окружение скина: тема и язык приложения, подписи UI, время суток
export interface SkinEnv {
  theme: 'light' | 'dark';
  locale: string; // 'ru' | 'en' — уходит в <html lang>
  labels: Record<string, string>; // ключ SKIN_LABELS → локализованная подпись
  timeOfDay: SkinTimeOfDay;
  localTime: string; // 'HH:MM'
  weather?: SkinWeather;
}

export interface SkinStatePayload {
  persona: SkinPersonaInfo;
  env?: SkinEnv;
  typing?: boolean;
  messages?: SkinChatMessage[];
  // Плашка «ответ на сообщение» над вводом (null — не показывать)
  reply?: { author: string; text: string } | null;
  personas?: SkinPersonaListItem[];
  context?: SkinContext;
  todos?: { id: number; text: string; done: boolean }[];
  inventory?: { icon: string; name: string; description: string; tag: string }[];
  dossier?: SkinDossier;
  files?: SkinFile[];
  settings?: { temperature: number; maxTokens: number; topP: number; stmSize: number };
  providers?: SkinProvider[];
  providerModels?: SkinProvider[]; // та же сущность для карточки «Модели провайдеров»
  featureFlags?: SkinFeatureFlag[];
  room?: SkinRoomState;
  feed?: { time: string; text: string }[];
}

// Снапшот для экрана чата (+ сайдбары и досье — один снапшот на всё)
export function buildChatPayload(args: {
  persona: Persona;
  statusText: string;
  youLabel: string;
  // Индикатор «печатает» этой персоны (локальная отправка или генерация на сервере)
  typing: boolean;
  messages: ChatMessage[];
  mood: string;
  pastimeLabel: string;
  allPersonas: { persona: Persona; statusText: string }[];
  context: Omit<SkinContext, 'pastime' | 'mood'> & { pastimePlace: string };
  reply: { author: string; text: string } | null;
  todos: TodoItem[];
  inventory: InventoryItem[];
  dossier: {
    facts: LtmFact[];
    reminders: Reminder[];
    initiatives: InitiativeEvent[];
    diary: DiaryEntry[];
    stm: StmMessage[]; // уже с учётом overlay (trim/delete)
    courses: LearningSession[]; // уже с учётом overlay (добавленные)
  };
  // Локализованные подписи состояний учебных курсов
  courseStatusLabels: { active: string; paused: string; finished: string };
  quizLineFor: (n: number) => string; // «тест ждёт ответов · N»
  // Самоинициатива: состояние уже с учётом overlay (silence/probability)
  initState: InitiativeState;
  initStages: string[]; // шкала эмоциональных состояний
  initSilenceText: string; // локализованная подпись прогресса молчания
  files: PersonaFile[];
  providers: LlmProvider[];
  // Поверх моков: выбранные модели, основной/резервные провайдеры, флаги фич,
  // параметры генерации (overlayStore)
  modelOverrides: Record<string, string>;
  providerMain: string | null;
  backupToggled: string[];
  featureFlags: SkinFeatureFlag[]; // уже с учётом overlay
  genOverrides: { temperature?: number; maxTokens?: number; topP?: number; stmSize?: number };
  stmSizeDefault: number;
  keySetLabel: string;
  keyNotSetLabel: string;
  env?: SkinEnv; // см. buildSkinEnv
}): SkinStatePayload {
  const {
    persona, statusText, youLabel, typing, messages, mood, pastimeLabel,
    allPersonas, context, reply, todos, inventory, dossier, files, providers,
    modelOverrides, genOverrides, courseStatusLabels, quizLineFor,
    initState, initStages, initSilenceText,
    providerMain, backupToggled, featureFlags, stmSizeDefault,
    keySetLabel, keyNotSetLabel, env,
  } = args;
  const mainProviderId = providerMain ?? providers.find((p) => p.active)?.id ?? providers[0]?.id;
  const toSkinProvider = (p: LlmProvider): SkinProvider => ({
    id: p.id,
    name: p.name,
    local: p.local,
    model: modelOverrides[p.id] ?? p.model ?? '',
    active: p.id === mainProviderId,
    backup: backupToggled.includes(p.id) ? !p.backup : p.backup,
    keyLabel: p.local ? '' : p.keySet ? keySetLabel : keyNotSetLabel,
  });
  return {
    persona: {
      id: persona.id,
      name: persona.name,
      statusText,
      model: persona.model,
      mood,
      pastime: pastimeLabel,
    },
    ...(env ? { env } : {}),
    typing: typing === true,
    messages: messages.map((m) => {
      const quoted = m.replyTo != null ? messages.find((q) => q.id === m.replyTo) : undefined;
      return {
        id: m.id,
        role: m.role === 'user' ? 'user' : 'persona',
        text: m.text,
        time: m.time,
        ...(m.image ? { image: m.image } : {}),
        ...(quoted
          ? { quote: { author: quoted.role === 'user' ? youLabel : persona.name, text: quoted.text } }
          : {}),
      };
    }),
    personas: allPersonas.map(({ persona: p, statusText: s }) => ({
      id: p.id,
      name: p.name,
      statusText: s,
      active: p.id === persona.id,
    })),
    context: {
      pastime: pastimeLabel + (context.pastimePlace ? ' · ' + context.pastimePlace : ''),
      mood,
      trend: context.trend,
      initiative: context.initiative,
      lastReply: context.lastReply,
      nextReminder: context.nextReminder,
      learning: context.learning,
      features: context.features,
    },
    reply,
    todos: todos.map((td) => ({ id: td.id, text: td.text, done: td.done })),
    inventory: inventory.map((i) => ({ icon: i.icon, name: i.name, description: i.description, tag: i.tag })),
    dossier: {
      facts: dossier.facts.map((f) => ({ id: f.id, category: f.category, fact: f.fact })),
      reminders: dossier.reminders.map((r) => {
        // «завтра, 18:00» → date «завтра» + clock «18:00» (для формы правки)
        const idx = r.time.lastIndexOf(', ');
        return {
          id: r.id,
          time: r.time,
          text: r.text,
          repeat: r.repeat,
          active: r.active,
          date: idx > 0 ? r.time.slice(0, idx) : '',
          clock: idx > 0 ? r.time.slice(idx + 2) : r.time,
        };
      }),
      initiatives: dossier.initiatives.map((e) => ({
        typeLabel: e.typeLabel,
        text: e.text,
        time: e.time,
        outcome: e.outcome,
      })),
      diary: dossier.diary.map((d) => ({ date: d.date, text: d.text })),
      stm: dossier.stm.map((m) => ({
        id: m.id,
        role: m.role === 'user' ? ('user' as const) : ('persona' as const),
        author: m.role === 'user' ? youLabel : persona.name,
        text: m.text,
        time: m.time,
      })),
      courses: dossier.courses.map((c) => ({
        id: c.id,
        subject: c.subject,
        status: courseStatusLabels[c.status],
        statusKey: c.status,
        lessons: c.lessonCount,
        topics: c.coveredTopics.length,
        words: c.vocabulary.length,
        quiz: c.quizPending,
        frequency: c.frequency,
        next: c.nextLesson,
        progress: Math.min(100, Math.round((c.lessonCount / COURSE_TOTAL) * 100)),
        topicsList: c.coveredTopics.join(' · '),
        vocabList: c.vocabulary.join(' · '),
        quizLine: c.quizPending > 0 ? quizLineFor(c.quizPending) : '',
      })),
      initState: {
        probability: Math.round(initState.probability * 100),
        threshold: initState.silenceThresholdMin,
        maxPerDay: initState.maxPerDay,
        interval: initState.checkIntervalMin,
        adaptive: initState.adaptiveThreshold ? '✓' : '—',
        bayes: initState.bayesianFeedback ? '✓' : '—',
        ignoreStreak: initState.ignoreStreak,
        today: initState.initiativesToday,
        mood: initState.emotionalState,
        stages: initStages.join(' · '),
        silencePct: 55, // моковый прогресс молчания (как в дефолтной теме)
        silenceText: initSilenceText,
      },
    },
    files: files.map((f) => ({
      id: f.id,
      name: f.name,
      kind: f.kind,
      size: f.size,
      date: f.date,
      description: f.description,
    })),
    settings: {
      temperature: genOverrides.temperature ?? persona.temperature,
      maxTokens: genOverrides.maxTokens ?? persona.maxTokens,
      topP: genOverrides.topP ?? persona.topP,
      stmSize: genOverrides.stmSize ?? stmSizeDefault,
    },
    providers: providers.map(toSkinProvider),
    providerModels: providers.map(toSkinProvider),
    featureFlags,
  };
}

// Снапшот для экрана комнаты
export function buildRoomPayload(args: {
  persona: Persona;
  statusText: string;
  pastimeLabel: string;
  pastimePlace: string;
  duration: string;
  x: number;
  y?: number | null;
  mood: string;
  energy: string;
  pet: string;
  petLabel?: string;
  bg?: string | null;
  sprite?: string | null;
  feed: RoomActivity[];
  inventory: InventoryItem[];
  env?: SkinEnv; // см. buildSkinEnv
  spot?: string;
  pose?: string;
  pastimeSince?: number | null;
}): SkinStatePayload {
  const { persona, statusText, feed, inventory, env, ...room } = args;
  return {
    persona: { id: persona.id, name: persona.name, statusText, model: persona.model },
    ...(env ? { env } : {}),
    room: env ? { ...room, timeOfDay: env.timeOfDay } : room,
    feed: feed.map((a) => ({ time: a.time, text: a.text })),
    inventory: inventory.map((i) => ({ icon: i.icon, name: i.name, description: i.description, tag: i.tag })),
  };
}

/* ── Окружение скина ──
   Подписи UI скина: ключ метки (атрибуты data-vpc-label / -placeholder /
   -title / -aria в файле скина) → ключ i18n приложения. Метка, для которой
   словарь не знает ключа, в снапшот не попадает — скин показывает свой
   текст-фолбэк. Значение с {name} подставляет имя персоны. */
export const SKIN_LABELS = {
  // Чат
  'persona-list': 'chat.personaList',
  'open-dossier': 'chat.dossier',
  'open-dossier-title': 'chat.dossierTitle',
  typing: 'status.typing',
  'message-placeholder': 'chat.inputPh', // {name}
  send: 'chat.send',
  attach: 'chat.attachTitle',
  'remove-attach': 'chat.removeAttach',
  reply: 'chat.replyTitle',
  'cancel-reply': 'chat.cancelReply',
  presence: 'chat.presence',
  mood: 'chat.mood',
  initiative: 'chat.initiative',
  'next-reminder': 'chat.nextReminder',
  todos: 'tasks.todo',
  inventory: 'room.inventory',
  modules: 'personas.features',
  // Досье: вкладки и под-вкладки
  'close-dossier': 'common.close',
  'tab-memory': 'dossier.memory',
  'tab-tasks': 'dossier.tasks',
  'tab-initiative': 'dossier.initiative',
  'tab-learning': 'dossier.learning',
  'tab-files': 'dossier.files',
  'tab-settings': 'dossier.settings',
  'tab-stm': 'mem.tabStm',
  'tab-ltm': 'mem.tabLtm',
  'tab-diary': 'mem.tabDiary',
  // Досье: память
  stm: 'mem.bufferLast',
  facts: 'mem.factsTitle',
  diary: 'mem.diaryTitle',
  'show-all': 'mem.showAll',
  collapse: 'mem.collapse',
  'trim-stm': 'mem.trimLast',
  'fact-category': 'mem.category',
  'fact-text': 'mem.factText',
  // Досье: дела
  'new-todo': 'tasks.newTodo',
  reminders: 'tasks.reminders',
  'reminder-date': 'tasks.date',
  'reminder-time': 'tasks.time',
  'reminder-text': 'tasks.textLabel',
  // Досье: инициатива
  'ini-params': 'init.params',
  'ini-silence': 'init.silenceThreshold',
  'ini-probability': 'init.probability',
  'ini-max-per-day': 'init.maxPerDay',
  'ini-interval': 'init.checkInterval',
  'unit-min': 'init.min',
  'ini-adaptive': 'init.adaptive',
  'ini-bayes': 'init.bayesian',
  'ini-state': 'init.currentState',
  'ini-ignore-streak': 'init.ignoreStreak',
  'ini-today': 'init.today',
  'ini-mood': 'init.emotionalState',
  'ini-stages': 'skin.label.stages',
  'user-silence': 'init.userSilence',
  'ini-history': 'init.history',
  // Досье: обучение
  'active-course': 'skin.label.activeCourse',
  'course-lessons': 'learn.statLessons',
  'course-topics': 'learn.statTopics',
  'course-words': 'learn.statWords',
  'course-quiz': 'learn.statQuiz',
  'course-topics-list': 'learn.coveredTopics',
  'course-vocab': 'learn.vocab',
  'course-next': 'skin.label.nextLesson',
  'teach-me': 'learn.newCourse',
  'course-subject': 'learn.subjectLabel',
  'course-start': 'learn.start',
  'course-history': 'learn.historyTitle',
  // Досье: файлы и настройки
  files: 'files.title',
  generation: 'settings.genParams',
  'stm-size': 'settings.stmSize',
  providers: 'settings.providers',
  'make-main': 'settings.makeMain',
  'provider-models': 'dossier.modelsTitle',
  features: 'settings.features',
  'danger-zone': 'dossier.dangerZone',
  'clear-chat': 'dossier.clearDialog',
  // Комната
  'room-mood': 'room.mood',
  'room-energy': 'room.energy',
  'room-place': 'room.placeInRoom',
  'room-feed': 'room.feedTitle',
  'item-name': 'room.itemNamePh',
  'item-icon': 'skin.label.itemIcon',
  'add-item': 'room.addItem',
} as const satisfies Record<string, string>;

export type SkinLabelKey = keyof typeof SKIN_LABELS;

export function skinTimeOfDay(hour: number): SkinTimeOfDay {
  if (hour >= 5 && hour < 11) return 'morning';
  if (hour >= 11 && hour < 17) return 'day';
  if (hour >= 17 && hour < 22) return 'evening';
  return 'night';
}

// Разбор строки окружения бэкенда: «Город: +7°C (feels like +5°C), rain,
// wind 4 m/s | Friday, 26.09.2026, 18:30». Без погодной части (местоположение
// выключено или сеть недоступна) — undefined.
export function parseSkinWeather(line: string | null | undefined): SkinWeather | undefined {
  if (!line) return undefined;
  const sep = line.indexOf(' | ');
  if (sep <= 0) return undefined;
  const text = line.slice(0, sep).trim();
  const colon = text.indexOf(': ');
  const temp = /([+-]?\d+(?:\.\d+)?)°C/.exec(text);
  const low = text.toLowerCase();
  const condition: SkinWeather['condition'] =
    /thunder/.test(low) ? 'storm'
    : /snow/.test(low) ? 'snow'
    : /rain|drizzle|shower/.test(low) ? 'rain'
    : /fog/.test(low) ? 'fog'
    : /cloud|overcast/.test(low) ? 'cloudy'
    : /clear/.test(low) ? 'clear'
    : undefined;
  return {
    text,
    ...(colon > 0 ? { city: text.slice(0, colon) } : {}),
    ...(temp ? { tempC: Number(temp[1]) } : {}),
    ...(condition ? { condition } : {}),
  };
}

// Блок env для снапшота. t — переводчик из useI18n(); weather — строка
// окружения (api.getEnvPreview().line) или уже разобранная погода
export function buildSkinEnv(args: {
  theme: 'light' | 'dark';
  locale: string;
  t: (key: string, vars?: Record<string, string | number>) => string;
  now?: Date;
  weather?: string | SkinWeather | null;
  personaName?: string; // для подписей с {name}
}): SkinEnv {
  const { theme, locale, t, personaName } = args;
  const now = args.now ?? new Date();
  const labels: Record<string, string> = {};
  for (const [label, key] of Object.entries(SKIN_LABELS)) {
    const raw = t(key);
    // t() без перевода возвращает сам ключ; {name} без имени — не подпись
    if (!raw || raw === key) continue;
    if (raw.includes('{name}')) {
      if (personaName == null) continue;
      labels[label] = t(key, { name: personaName });
    } else {
      labels[label] = raw;
    }
  }
  const weather = typeof args.weather === 'string' || args.weather == null
    ? parseSkinWeather(args.weather)
    : args.weather;
  const pad = (n: number) => String(n).padStart(2, '0');
  return {
    theme,
    locale,
    labels,
    timeOfDay: skinTimeOfDay(now.getHours()),
    localTime: pad(now.getHours()) + ':' + pad(now.getMinutes()),
    ...(weather ? { weather } : {}),
  };
}
