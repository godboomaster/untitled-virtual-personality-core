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
}

export interface SkinStatePayload {
  persona: SkinPersonaInfo;
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
}): SkinStatePayload {
  const {
    persona, statusText, youLabel, typing, messages, mood, pastimeLabel,
    allPersonas, context, reply, todos, inventory, dossier, files, providers,
    modelOverrides, genOverrides, courseStatusLabels, quizLineFor,
    initState, initStages, initSilenceText,
    providerMain, backupToggled, featureFlags, stmSizeDefault,
    keySetLabel, keyNotSetLabel,
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
}): SkinStatePayload {
  const { persona, statusText, feed, inventory, ...room } = args;
  return {
    persona: { id: persona.id, name: persona.name, statusText, model: persona.model },
    room,
    feed: feed.map((a) => ({ time: a.time, text: a.text })),
    inventory: inventory.map((i) => ({ icon: i.icon, name: i.name, description: i.description, tag: i.tag })),
  };
}
