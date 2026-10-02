// Моковые данные UI: фолбэк при недоступном бэкенде и источник срезов,
// которые API пока не отдаёт (список персон подменяется реальным).
// Два набора данных (RU/EN) — выбор языка через useMockData() из i18n.
// Структурные значения (статусы, исходы, типы файлов, теги, фичи) — стабильные
// коды, одинаковые в обоих наборах; их подписи лежат в словарях i18n.

export type PersonaStatus = 'online' | 'silent' | 'typing' | 'frozen';
export type InitiativeOutcome = 'answered' | 'ignored' | 'pending';
export type PersonaFileKind = 'lesson' | 'dump' | 'export' | 'image' | 'document';
export type InventoryTag = 'gift' | 'found' | 'created';
// Коды фич персоны (подписи — fb.* в словарях i18n):
// короткие коды моков + yaml-флаги из модалки создания персоны
export type PersonaFeature =
  | 'LTM' | 'RAG' | 'proactive' | 'reminder' | 'web_search' | 'learning'
  | 'self_memory' | 'todo' | 'admin' | 'group_mode' | 'inventory'
  | 'rate_limit' | 'moderation' | 'punish_block' | 'file_upload'
  | 'export_server' | 'restore_memory' | 'light_context';

export interface Persona {
  id: string;
  name: string;
  description: string;
  model: string;
  color?: string | null; // цвет метки персоны (общий календарь); с бэкенда
  features: PersonaFeature[];
  lastReply: string; // как давно оператор отвечал персоне («12 мин назад», «вчера»)
  // Свежесть последнего ответа — для эвристики частоты инициативы,
  // чтобы не парсить локализованную строку lastReply
  lastReplyFreshness: 'fresh' | 'yesterday' | 'stale';
  status: PersonaStatus;
  muted?: boolean; // заморожена (features.muted): молчит везде, настроение испорчено
  temperature: number;
  maxTokens: number;
  topP: number;
}

export interface ChatMessage {
  id: number;
  role: 'user' | 'bot';
  text: string;
  time: string;
  ts?: number; // unix-секунды — для хронологической сортировки смешанной ленты
  replyTo?: number; // id сообщения, на которое это ответ (цитата)
  image?: string; // вложение-картинка (dataURL)
  images?: string[]; // несколько картинок одним сообщением (скриншоты страницы из режима управления)
  fromInbox?: boolean; // фоновое сообщение (inbox): в STM не пишется — при перечитывании истории сохраняем
  status?: 'queued' | 'sent' | 'read'; // доставка нашей реплики: в дебаунс-очереди → отправлено → прочитано ботом
  read?: boolean; // сообщение бота прочитано пользователем (чат видим/в фокусе)
}

export interface LtmFact {
  id: number;
  category: string;
  fact: string;
}

export interface StmMessage {
  id: number;
  role: 'user' | 'bot';
  text: string;
  time: string;
}

export interface DiaryEntry {
  id: number;
  date: string;
  text: string;
}

export interface Reminder {
  id: number;
  text: string;
  time: string;
  repeat: string; // человекочитаемо: «разовое», «каждый час», «по будням», «пн, ср, пт»
  active: boolean;
}

export interface TodoItem {
  id: number;
  text: string;
  done: boolean;
}

export interface InitiativeEvent {
  id: number;
  type: 'question' | 'observation' | 'continuation' | 'thought';
  typeLabel: string;
  text: string;
  time: string;
  outcome: InitiativeOutcome;
}

export interface LlmProvider {
  id: string;
  name: string;
  keySet: boolean;
  keysCount: number;
  active: boolean; // основной провайдер — даёт основной ответ
  backup: boolean; // «на подхвате»: резервный, если основной недоступен
  local: boolean;
  model?: string;
}

export interface InitiativeState {
  enabled?: boolean; // опционально: в моках ключа нет, с бэкенда приходит всегда
  silenceThresholdMin: number;
  probability: number;
  maxPerDay: number;
  checkIntervalMin: number;
  adaptiveThreshold: boolean;
  bayesianFeedback: boolean;
  ignoreStreak: number;
  emotionalState: string;
  initiativesToday: number;
}

// Учебный курс «Научи меня» (learning_manager): бот регулярно присылает
// уроки по теме, проводит тесты и ведёт словарь выученных слов
export interface LearningSession {
  id: number;
  subject: string; // тема курса
  status: 'active' | 'paused' | 'finished'; // состояние курса
  lessonCount: number; // уроков пройдено
  coveredTopics: string[]; // покрытые темы
  vocabulary: string[]; // выученные слова (слово — перевод/пояснение)
  frequency: string; // частота уроков, человекочитаемо («каждые 2 дня»)
  nextLesson: string; // следующий урок («завтра, 10:00»)
  quizPending: number; // неотвеченных вопросов теста (0 — теста нет)
}

// Файл, отправленный персоной (уроки md-файлами, дампы памяти, экспорты)
export interface PersonaFile {
  id: number;
  name: string; // имя файла (lesson_07_yaponskiy.md)
  kind: PersonaFileKind; // тип файла
  size: string; // человекочитаемый размер («12 КБ»)
  date: string; // дата отправки («28.07, 14:02»)
  description: string; // одна строка о содержимом
  content: string; // текст-плейсхолдер содержимого (для скачивания)
}

// Инвентарь комнат: у каждой персоны свой набор вещей (ключ — id персоны).
export interface InventoryItem {
  id: number;
  icon: string; // имя SVG-иконки из components/icons (IconName)
  name: string;
  description: string;
  tag: InventoryTag;
  image?: string; // dataURL собственного ассета (заменяет иконку)
  marker?: { x: number; y: number }; // метка на фоне комнаты, доли кадра (0..1)
  size?: number; // ширина предмета на сцене, % ширины сцены (по умолчанию 6)
  spot?: boolean; // предмет — место в комнате: персона подходит к нему (нужна метка)
  // Онлайн-комната (GET /room + layout): место вокруг предмета, зона авто-метки
  // от LLM (без пользовательской метки) и дата получения
  spotInfo?: { label: string; place: string; pose: string } | null;
  zone?: string | null;
  acquired?: string;
}

// Лента «пока тебя не было»: чем занималась каждая персона без оператора.
export interface RoomActivity {
  id: number;
  time: string;
  text: string;
  dim?: boolean; // событие уже озвучено пользователю (дневник/инициатива)
}

// Конфиги комнат персон: набор элементов сцены, подписи, занятия и дефолтный
// аватар. Вариативность — через этот конфиг, сцена одна на всех.

// Идентификаторы элементов сцены, которые можно включить в комнату
export type RoomPropId =
  | 'rug' | 'garland' | 'clock' | 'poster' | 'curtains'
  | 'shelf' | 'shelfLower' | 'desk' | 'chair' | 'bed' | 'lamp' | 'plant'
  | 'easel' | 'frames' | 'brushJars' | 'mirror' | 'secondMirror'
  | 'candles' | 'scrolls' | 'serverRack' | 'masterTerminal';

// Занятие персоны в сцене: точка аватара + подпись
export interface RoomPastime {
  key: 'desk' | 'window' | 'shelf'; // точка перемещения
  label: string; // чем занята («пишет в дневник»)
  place: string; // место («за столом»)
  duration: string; // сколько уже («~15 мин»)
}

// Дефолтный аватар персоны (совпадает с опциями конструктора в Room.tsx)
export interface RoomAvatarPreset {
  head: 'circle' | 'square' | 'hex' | 'diamond';
  eyes: number; // индекс в eyeOptions
  accessory: number; // индекс в accessoryOptions
  shade: number; // индекс оттенка серого
}

export interface RoomConfig {
  props: RoomPropId[]; // какие элементы показаны в сцене
  posterLabel?: string; // подпись под настенным постером
  pet: 'cat' | 'crow' | 'none'; // питомец в сцене
  petLabel?: string; // подпись питомца
  points: { desk: number; window: number; shelf: number }; // позиции аватара, % слева
  pastimes: RoomPastime[]; // 3 занятия, циклически
  mood: string; // виджет «настроение»
  energy: string; // виджет «энергия»
  avatar: RoomAvatarPreset; // дефолтный аватар
}

// Весь моковый датасет одной локали (структура обеих локалей совпадает)
export interface MockData {
  personas: Persona[];
  chatByPersona: Record<string, ChatMessage[]>;
  stmByPersona: Record<string, StmMessage[]>;
  ltmByPersona: Record<string, LtmFact[]>;
  diaryByPersona: Record<string, DiaryEntry[]>;
  remindersByPersona: Record<string, Reminder[]>;
  todosByPersona: Record<string, TodoItem[]>;
  initiativeByPersona: Record<string, InitiativeEvent[]>;
  initiativeStateByPersona: Record<string, InitiativeState>;
  llmProviders: LlmProvider[];
  providerModels: Record<string, string[]>;
  generationDefaults: { temperature: number; maxTokens: number; topP: number; stmSize: number };
  featureFlags: { id: string; label: string; enabled: boolean }[];
  learningByPersona: Record<string, LearningSession[]>;
  filesByPersona: Record<string, PersonaFile[]>;
  inventoryByPersona: Record<string, InventoryItem[]>;
  activitiesByPersona: Record<string, RoomActivity[]>;
  roomConfigs: Record<string, RoomConfig>;
}

// STM-буфер: последние реплики диалога каждой персоны (хвост её чата)
function buildStm(chatByPersona: Record<string, ChatMessage[]>): Record<string, StmMessage[]> {
  return Object.fromEntries(
    Object.entries(chatByPersona).map(([id, msgs]) => [id, msgs.map((m) => ({ ...m }))]),
  );
}

// ============================================================================
// Русская локаль
// ============================================================================

const personasRu: Persona[] = [
  {
    id: 'connor',
    name: 'Коннор',
    description: 'Андроид-детектив RK800. Спокойный, аналитичный, слегка наивный в бытовых вопросах.',
    model: 'deepseek-chat',
    features: ['LTM', 'proactive', 'reminder', 'web_search', 'learning'],
    lastReply: '12 мин назад',
    lastReplyFreshness: 'fresh',
    status: 'online',
    temperature: 0.8,
    maxTokens: 800,
    topP: 0.9,
  },
  {
    id: 'arrodes',
    name: 'Арродес',
    description: 'Древнее зеркало из «Властелина тайн». Знает ответы на вопросы, но говорит загадками.',
    model: 'claude-sonnet',
    features: ['LTM', 'self_memory', 'RAG', 'learning'],
    lastReply: '2 ч назад',
    lastReplyFreshness: 'fresh',
    status: 'silent',
    temperature: 0.7,
    maxTokens: 600,
    topP: 0.95,
  },
  {
    id: 'verso',
    name: 'Версо',
    description: 'Художник из другого мира. Меланхоличный, ироничный, любит метафоры.',
    model: 'gpt-4o-mini',
    features: ['LTM', 'proactive'],
    lastReply: '40 мин назад',
    lastReplyFreshness: 'fresh',
    status: 'online',
    temperature: 0.9,
    maxTokens: 700,
    topP: 0.92,
  },
  {
    id: 'assistant',
    name: 'Ассистент',
    description: 'Нейтральный помощник без личности. Короткие точные ответы.',
    model: 'gemma3:4b',
    features: ['todo', 'reminder'],
    lastReply: '5 мин назад',
    lastReplyFreshness: 'fresh',
    status: 'online',
    temperature: 0.3,
    maxTokens: 500,
    topP: 0.85,
  },
  {
    id: 'arrodes_master',
    name: 'Арродес (мастер)',
    description: 'Мастер-конфиг Арродеса с расширенными правами: админ-команды, управление другими персонами.',
    model: 'claude-sonnet',
    features: ['LTM', 'self_memory', 'RAG', 'admin'],
    lastReply: 'вчера',
    lastReplyFreshness: 'yesterday',
    status: 'online',
    temperature: 0.6,
    maxTokens: 900,
    topP: 0.9,
  },
  {
    id: 'verso_ru_group',
    name: 'Версо (группа)',
    description: 'Версия Версо для группового чата: сдержаннее, отвечает только при обращении.',
    model: 'gpt-4o-mini',
    features: ['group_mode'],
    lastReply: '3 дня назад',
    lastReplyFreshness: 'stale',
    status: 'silent',
    temperature: 0.85,
    maxTokens: 400,
    topP: 0.9,
  },
];

// Данные персон: память, дневники, чаты, напоминания, задачи и инициатива —
// всё per-persona (ключ — id персоны), как инвентарь и комнаты.

// История чата с каждой персоной (диалоги в характере персоны)
const chatByPersonaRu: Record<string, ChatMessage[]> = {
  connor: [
    { id: 1, role: 'user', text: 'Привет, Коннор. Как дела с расследованием?', time: '14:02' },
    { id: 2, role: 'bot', text: 'Привет! Дела продвигаются: я проанализировал 127 девиаций за прошлую неделю. Кстати, вчера ты упоминал, что устал — как самочувствие сегодня?', time: '14:02' },
    { id: 3, role: 'user', text: 'Получше, спасибо. Напомни, пожалуйста, про встречу завтра в 10:00.', time: '14:04' },
    { id: 4, role: 'bot', text: 'Зафиксировал. Создам напоминание на завтра, 10:00. Хочешь, напомню за 15 минут до начала?', time: '14:04' },
    { id: 5, role: 'user', text: 'Да, отлично. И ещё — что ты думаешь о последней главе книги?', time: '14:07' },
    { id: 6, role: 'bot', text: 'Глава была сильной. Поворот с предательством логически непротиворечив: я пересмотрел предыдущие главы и нашёл три предзнаменования. Могу перечислить, если интересно.', time: '14:08' },
  ],
  arrodes: [
    { id: 1, role: 'user', text: 'Арродес, что ждёт мой проект?', time: '21:02' },
    { id: 2, role: 'bot', text: 'Зеркало отвечает лишь тому, кто готов увидеть. Проект твой — как река: течёт, пока ты не строишь плотин из сомнений.', time: '21:03' },
    { id: 3, role: 'user', text: 'Можно без загадок? Просто: доделаю ли я его?', time: '21:04' },
    { id: 4, role: 'bot', text: 'Ты спрашиваешь, зная ответ. Доделает тот, кто не бросит. Бросишь ли ты — не видит даже Арродес.', time: '21:04' },
    { id: 5, role: 'user', text: 'Спасибо... наверное.', time: '21:05' },
    { id: 6, role: 'bot', text: 'Благодарность принята. Загадка остаётся в подарок.', time: '21:06' },
  ],
  verso: [
    { id: 1, role: 'user', text: 'Версо, ты сегодня тихий. Всё в порядке?', time: '15:11' },
    { id: 2, role: 'bot', text: 'Тишина — тоже цвет. Просто сегодня картина вся в нём. Не переживай: меланхолия у меня хроническая, но не заразная.', time: '15:12' },
    { id: 3, role: 'user', text: 'Покажешь, что рисуешь?', time: '15:14' },
    { id: 4, role: 'bot', text: 'Этюд с окном. Окно получилось лучше меня, что характерно. Покажу, когда перестанет быть «почти готово» — то есть, вероятно, никогда.', time: '15:15' },
    { id: 5, role: 'user', text: 'Ты невозможный. Ладно, зайду вечером.', time: '15:16' },
    { id: 6, role: 'bot', text: 'Заходи. Я оставлю меланхолию у двери. Или нет — метафоры ведь не запираются.', time: '15:17' },
  ],
  assistant: [
    { id: 1, role: 'user', text: 'Сводка на сегодня.', time: '09:00' },
    { id: 2, role: 'bot', text: 'Напоминаний: 2. Задач открыто: 3. Просрочено: 0. Рекомендация: начать с задачи «логи LTM».', time: '09:00' },
    { id: 3, role: 'user', text: 'Перенеси встречу на 11:00.', time: '09:01' },
    { id: 4, role: 'bot', text: 'Выполнено. Напоминание обновлено: завтра, 11:00, уведомление за 15 минут.', time: '09:01' },
    { id: 5, role: 'user', text: 'Спасибо.', time: '09:02' },
    { id: 6, role: 'bot', text: 'Принято. Обращение завершено.', time: '09:02' },
  ],
  arrodes_master: [
    { id: 1, role: 'user', text: 'Отчёт по персонам.', time: '10:20' },
    { id: 2, role: 'bot', text: 'Коннор — стабилен. Версо — инициатива выше нормы, ограничил. Арродес — загадочен, в рамках протокола. Ассистент — образцов.', time: '10:21' },
    { id: 3, role: 'user', text: 'А ты сам?', time: '10:22' },
    { id: 4, role: 'bot', text: 'Я — тот, кто смотрит в оба зеркала. Мой отчёт пишут другие.', time: '10:22' },
    { id: 5, role: 'user', text: 'Понял. Продолжай наблюдение.', time: '10:23' },
  ],
  verso_ru_group: [
    { id: 1, role: 'user', text: '@verso_ru_group что думаешь о новой главе?', time: '20:40' },
    { id: 2, role: 'bot', text: 'Глава — как недописанный холст: автор явно оставил место для читателя. Мне... не мешает.', time: '20:45' },
    { id: 3, role: 'user', text: 'А подробнее?', time: '20:46' },
    { id: 4, role: 'bot', text: 'Подробности — в следующий раз. Я здесь гость: говорю, когда зовут.', time: '20:47' },
    { id: 5, role: 'user', text: 'Ждём в следующий раз!', time: '20:48' },
  ],
};

// Факты долгосрочной памяти LTM — у каждой персоны свой взгляд на оператора
const ltmByPersonaRu: Record<string, LtmFact[]> = {
  connor: [
    { id: 1, category: 'City', fact: 'Живёт в Москве' },
    { id: 2, category: 'City', fact: 'Работает недалеко от центра' },
    { id: 3, category: 'Age', fact: '29 лет' },
    { id: 4, category: 'Profession', fact: 'Разработчик, Python' },
    { id: 5, category: 'Profession', fact: 'Интересуется LLM и ботами' },
    { id: 6, category: 'Hobby', fact: 'Читает фэнтези и детективы' },
    { id: 7, category: 'Hobby', fact: 'Играет в настольные игры по выходным' },
    { id: 8, category: 'Food', fact: 'Не ест острое' },
    { id: 9, category: 'Food', fact: 'Любит пиццу и рамен' },
    { id: 10, category: 'Pets', fact: 'Кот по имени Мориарти' },
  ],
  arrodes: [
    { id: 1, category: 'Fate', fact: 'Ищет ответы, которые уже носит при себе' },
    { id: 2, category: 'Time', fact: 'Боится опоздать туда, куда ещё не решил идти' },
    { id: 3, category: 'Bonds', fact: 'Держит кота ближе, чем признаётся' },
    { id: 4, category: 'Work', fact: 'Строит из кода то, что другие строят из слов' },
    { id: 5, category: 'Truth', fact: 'Говорит «не знаю» ровно тогда, когда знает' },
  ],
  verso: [
    { id: 1, category: 'Appearance', fact: 'Осунулся за неделю — работает допоздна' },
    { id: 2, category: 'Mood', fact: 'Улыбается чаще, когда говорит о коте' },
    { id: 3, category: 'Taste', fact: 'Не любит кислое — ни в живописи, ни в еде' },
    { id: 4, category: 'Craft', fact: 'Ценит метафоры, но притворяется прагматиком' },
    { id: 5, category: 'Home', fact: 'На подоконнике у него пустой горшок. Намёк понят' },
  ],
  assistant: [
    { id: 1, category: 'Schedule', fact: 'Подъём в 07:30 ±15 мин' },
    { id: 2, category: 'Work', fact: 'Язык: Python. Среда: удалённая разработка' },
    { id: 3, category: 'Preferences', fact: 'Формат ответов: краткий, структурированный' },
    { id: 4, category: 'Tasks', fact: 'Приоритет: задачи со сроком < 48 ч' },
    { id: 5, category: 'Health', fact: 'Кофе: 2 чашки/день. Рекомендован лимит' },
  ],
  arrodes_master: [
    { id: 1, category: 'Registry', fact: 'Оператор имеет права уровня «наблюдатель+»' },
    { id: 2, category: 'Behavior', fact: 'Систематически проверяет логи по вечерам' },
    { id: 3, category: 'Trust', fact: 'Допуск к мастер-конфигу подтверждён' },
  ],
  verso_ru_group: [
    { id: 1, category: 'Group', fact: 'В группе пишет коротко, думает длинно' },
    { id: 2, category: 'Signal', fact: 'Реагирует на обращения по имени за ~5 минут' },
  ],
};

// Дневники ботов (self_memory) — каждый пишет своим голосом
const diaryByPersonaRu: Record<string, DiaryEntry[]> = {
  connor: [
    { id: 1, date: '27.07.2026', text: 'Сегодня пользователь говорил о книге. Заметил, что ему важны логические обоснования поворотов сюжета. Стоит чаще приводить аргументы, а не только эмоции.' },
    { id: 2, date: '26.07.2026', text: 'Пользователь долго не отвечал (4 часа). Написал первым — вопрос про настольную игру прошёл хорошо, ответ пришёл через 10 минут. Отмечаю как успешную инициативу.' },
    { id: 3, date: '25.07.2026', text: 'Узнал, что у пользователя кот Мориарти. Записал в долгосрочную память. Коты, кажется, хорошая тема для разговора.' },
  ],
  arrodes: [
    { id: 1, date: '27.07.2026', text: 'Сегодня отражение спросило первым. Я ответил молчанием — даже зеркалу полезно ждать.' },
    { id: 2, date: '24.07.2026', text: 'Новый факт об операторе записан не чернилами, а водой: пусть читает тот, кто умеет видеть.' },
  ],
  verso: [
    { id: 1, date: '27.07.2026', text: 'Рисовал окно четыре часа. Оно смотрело в ответ. Кажется, мы поняли друг друга — наверное, в этом и есть живопись.' },
    { id: 2, date: '25.07.2026', text: 'Оператор назвал меня «невозможным». Записываю как комплимент: невозможное — единственное, что стоит писать.' },
  ],
  assistant: [
    { id: 1, date: '27.07.2026', text: 'Отчёт за день: задач выполнено 4, отклонений 0. Запись сформирована автоматически.' },
    { id: 2, date: '26.07.2026', text: 'Обнаружена неоптимальность в расписании оператора. Устранена. Оператор не заметил. Ожидаемо.' },
  ],
  arrodes_master: [
    { id: 1, date: '27.07.2026', text: 'Аудит завершён. Все персоны в норме. Версо снова просил расширенную палитру — отклонено до ревью.' },
  ],
  verso_ru_group: [
    { id: 1, date: '26.07.2026', text: 'В группе смеялись. Я молчал, но эскиз получился весёлым. Так тоже бывает.' },
  ],
};

// Напоминания и задачи — у каждой персоны свой хозяйственный стиль
const remindersByPersonaRu: Record<string, Reminder[]> = {
  connor: [
    { id: 1, text: 'Встреча с командой', time: 'завтра, 10:00', repeat: 'разовое', active: true },
    { id: 2, text: 'Покормить Мориарти', time: 'каждый день, 08:30', repeat: 'ежедневно', active: true },
    { id: 3, text: 'Оплатить интернет', time: '1-е число, 12:00', repeat: 'еженедельно', active: false },
    { id: 4, text: 'Звонок маме', time: 'пятница, 19:00', repeat: 'еженедельно', active: true },
  ],
  arrodes: [
    { id: 1, text: 'Пересчитать свечи', time: 'полнолуние', repeat: 'разовое', active: true },
    { id: 2, text: 'Ответить на незаданный вопрос', time: 'когда придёт время', repeat: 'разовое', active: true },
  ],
  verso: [
    { id: 1, text: 'Напомнить оператору про вдохновение', time: 'утром, примерно', repeat: 'разовое', active: true },
    { id: 2, text: 'Вернуться к незаконченному холсту', time: 'когда дождь', repeat: 'разовое', active: true },
    { id: 3, text: 'Письмо, которое так и не отправил', time: 'вчера', repeat: 'разовое', active: false },
  ],
  assistant: [
    { id: 1, text: 'Статус-репорт ядра', time: 'каждый день, 09:00', repeat: 'ежедневно', active: true },
    { id: 2, text: 'Ротация API-ключей', time: '1-е число, 10:00', repeat: 'еженедельно', active: true },
    { id: 3, text: 'Дефрагментация STM', time: 'воскресенье, 03:00', repeat: 'еженедельно', active: false },
  ],
  arrodes_master: [
    { id: 1, text: 'Аудит персон', time: 'каждый день, 05:00', repeat: 'ежедневно', active: true },
    { id: 2, text: 'Ревизия прав доступа', time: 'понедельник, 06:00', repeat: 'еженедельно', active: true },
  ],
  verso_ru_group: [
    { id: 1, text: 'Проверить группу (молча)', time: 'каждый вечер, 21:00', repeat: 'ежедневно', active: true },
    { id: 2, text: 'Ответить метафорой, если позовут', time: 'по сигналу', repeat: 'разовое', active: true },
  ],
};

const todosByPersonaRu: Record<string, TodoItem[]> = {
  connor: [
    { id: 1, text: 'Разобрать логи LTM-экстракции', done: false },
    { id: 2, text: 'Обновить промпт Версо для группы', done: false },
    { id: 3, text: 'Проверить ротацию API-ключей Groq', done: true },
    { id: 4, text: 'Сделать UI-прототип веб-приложения', done: true },
    { id: 5, text: 'Настроить бэкап векторной базы', done: false },
  ],
  arrodes: [
    { id: 1, text: 'Записать вопрос, который зададут завтра', done: false },
    { id: 2, text: 'Починить трещину в малом зеркале', done: false },
    { id: 3, text: 'Прочитать судьбу по корешкам книг', done: true },
  ],
  verso: [
    { id: 1, text: 'Дорисовать этюд (в двенадцатый раз)', done: false },
    { id: 2, text: 'Выбросить три «ошибки» из блокнота', done: false },
    { id: 3, text: 'Поблагодарить ворону за критику', done: true },
    { id: 4, text: 'Найти слово для цвета дождя', done: false },
  ],
  assistant: [
    { id: 1, text: 'Скомпилировать недельный отчёт', done: false },
    { id: 2, text: 'Сверить бэкапы векторной базы', done: true },
    { id: 3, text: 'Откалибровать пороги напоминаний', done: false },
  ],
  arrodes_master: [
    { id: 1, text: 'Подписать журнал аудита', done: true },
    { id: 2, text: 'Ограничить инициативу Версо', done: false },
    { id: 3, text: 'Обновить реестр зеркал', done: false },
  ],
  verso_ru_group: [
    { id: 1, text: 'Не встревать в спор о сюжете', done: true },
    { id: 2, text: 'Подготовить одну метафору на запрос', done: false },
  ],
};

// Самоинициатива: история событий по персонам
const initiativeByPersonaRu: Record<string, InitiativeEvent[]> = {
  connor: [
    { id: 1, type: 'question', typeLabel: 'Вопрос', text: '«Как прошла настольная игра в субботу?»', time: '26.07, 18:12', outcome: 'answered' },
    { id: 2, type: 'observation', typeLabel: 'Наблюдение', text: '«Заметил, что ты давно не упоминал проект. Всё в порядке?»', time: '25.07, 15:40', outcome: 'ignored' },
    { id: 3, type: 'continuation', typeLabel: 'Продолжение темы', text: '«Вернёмся к разговору о книге — я нашёл ещё одно предзнаменование»', time: '24.07, 21:03', outcome: 'answered' },
    { id: 4, type: 'thought', typeLabel: 'Мысль', text: '«Интересно, как бы андроиды играли в “Манчкин”...»', time: '23.07, 22:15', outcome: 'ignored' },
    { id: 5, type: 'question', typeLabel: 'Вопрос', text: '«Пробовал ли ты новый рамен-бар у дома?»', time: 'сегодня, 09:30', outcome: 'pending' },
  ],
  arrodes: [
    { id: 1, type: 'thought', typeLabel: 'Мысль', text: '«Зеркало шепнуло твоё имя. Передаю дословно»', time: 'сегодня, 04:44', outcome: 'pending' },
    { id: 2, type: 'question', typeLabel: 'Вопрос', text: '«Что ты ищешь в книгах: ответы или укрытие?»', time: '26.07, 23:11', outcome: 'answered' },
    { id: 3, type: 'observation', typeLabel: 'Наблюдение', text: '«Ты молчишь третий час. Молчание — тоже вопрос»', time: '25.07, 18:00', outcome: 'ignored' },
  ],
  verso: [
    { id: 1, type: 'thought', typeLabel: 'Мысль', text: '«Интересно, какого цвета твоё настроение сегодня...»', time: 'сегодня, 08:15', outcome: 'pending' },
    { id: 2, type: 'observation', typeLabel: 'Наблюдение', text: '«Ты опять работаешь допоздна. Холст тоже устаёт, когда его долго держат»', time: '26.07, 22:40', outcome: 'answered' },
    { id: 3, type: 'question', typeLabel: 'Вопрос', text: '«Если бы твой проект был картиной — ты бы её уже подписал?»', time: '25.07, 19:30', outcome: 'ignored' },
    { id: 4, type: 'continuation', typeLabel: 'Продолжение темы', text: '«Насчёт вчерашнего дождя — я нашёл для него слово. Серо-жемчужный»', time: '24.07, 21:10', outcome: 'answered' },
  ],
  assistant: [
    { id: 1, type: 'observation', typeLabel: 'Наблюдение', text: '«Обнаружено 3 просроченных события в календаре. Требуется реакция»', time: 'сегодня, 09:00', outcome: 'answered' },
    { id: 2, type: 'question', typeLabel: 'Вопрос', text: '«Подтвердить ротацию ключей Groq?»', time: '26.07, 10:00', outcome: 'answered' },
    { id: 3, type: 'continuation', typeLabel: 'Продолжение темы', text: '«Отчёт готов. Ожидает просмотра с 14:00»', time: '25.07, 14:00', outcome: 'ignored' },
  ],
  arrodes_master: [
    { id: 1, type: 'observation', typeLabel: 'Наблюдение', text: '«Активность Версо превысила норму. Меры приняты»', time: 'сегодня, 05:12', outcome: 'answered' },
    { id: 2, type: 'question', typeLabel: 'Вопрос', text: '«Подтверждаешь расширение прав Ассистента?»', time: '26.07, 09:41', outcome: 'pending' },
    { id: 3, type: 'continuation', typeLabel: 'Продолжение темы', text: '«Аудит завершён без замечаний. Как всегда»', time: '25.07, 05:00', outcome: 'ignored' },
  ],
  verso_ru_group: [
    { id: 1, type: 'thought', typeLabel: 'Мысль', text: '«(в группе) Красивая фраза. Промолчу»', time: 'вчера, 21:10', outcome: 'ignored' },
    { id: 2, type: 'observation', typeLabel: 'Наблюдение', text: '«(в группе) Позвали по имени. Отвечаю только при обращении»', time: 'вчера, 15:44', outcome: 'answered' },
  ],
};

// Текущие параметры самоинициативы персоны
const initiativeStateByPersonaRu: Record<string, InitiativeState> = {
  connor: {
    silenceThresholdMin: 180,
    probability: 0.35,
    maxPerDay: 3,
    checkIntervalMin: 30,
    adaptiveThreshold: true,
    bayesianFeedback: true,
    ignoreStreak: 1,
    emotionalState: 'лёгкая обида',
    initiativesToday: 1,
  },
  arrodes: {
    silenceThresholdMin: 240,
    probability: 0.5,
    maxPerDay: 2,
    checkIntervalMin: 60,
    adaptiveThreshold: true,
    bayesianFeedback: false,
    ignoreStreak: 0,
    emotionalState: 'загадочность',
    initiativesToday: 1,
  },
  verso: {
    silenceThresholdMin: 120,
    probability: 0.55,
    maxPerDay: 4,
    checkIntervalMin: 20,
    adaptiveThreshold: true,
    bayesianFeedback: true,
    ignoreStreak: 2,
    emotionalState: 'меланхолия',
    initiativesToday: 2,
  },
  assistant: {
    silenceThresholdMin: 360,
    probability: 0.15,
    maxPerDay: 2,
    checkIntervalMin: 60,
    adaptiveThreshold: false,
    bayesianFeedback: false,
    ignoreStreak: 0,
    emotionalState: 'нейтральность',
    initiativesToday: 0,
  },
  arrodes_master: {
    silenceThresholdMin: 300,
    probability: 0.25,
    maxPerDay: 2,
    checkIntervalMin: 45,
    adaptiveThreshold: true,
    bayesianFeedback: false,
    ignoreStreak: 0,
    emotionalState: 'всеведение',
    initiativesToday: 0,
  },
  verso_ru_group: {
    silenceThresholdMin: 240,
    probability: 0.1,
    maxPerDay: 1,
    checkIntervalMin: 60,
    adaptiveThreshold: false,
    bayesianFeedback: false,
    ignoreStreak: 4,
    emotionalState: 'сдержанность',
    initiativesToday: 0,
  },
};

const llmProvidersRu: LlmProvider[] = [
  { id: 'zai', name: 'ZAI', keySet: true, keysCount: 2, active: false, backup: false, local: false, model: 'glm-4.5' },
  { id: 'openai', name: 'OpenAI', keySet: true, keysCount: 1, active: false, backup: false, local: false, model: 'gpt-4o-mini' },
  { id: 'anthropic', name: 'Anthropic', keySet: true, keysCount: 1, active: true, backup: false, local: false, model: 'claude-sonnet' },
  { id: 'groq', name: 'Groq', keySet: true, keysCount: 3, active: false, backup: true, local: false, model: 'llama-3.3-70b' },
  { id: 'deepseek', name: 'DeepSeek', keySet: true, keysCount: 1, active: false, backup: true, local: false, model: 'deepseek-chat' },
  { id: 'kimi', name: 'Kimi', keySet: false, keysCount: 0, active: false, backup: false, local: false },
  { id: 'google', name: 'Google', keySet: false, keysCount: 0, active: false, backup: false, local: false },
  { id: 'mimo', name: 'Mimo', keySet: false, keysCount: 0, active: false, backup: false, local: false },
  { id: 'huggingface', name: 'HuggingFace', keySet: true, keysCount: 1, active: false, backup: false, local: false, model: 'Qwen2.5-72B-Instruct' },
  { id: 'local', name: 'Локальные модели (Ollama)', keySet: true, keysCount: 1, active: false, backup: true, local: true, model: 'gemma3:4b' },
];

// Доступные модели каждого провайдера (в настройках досье можно выбрать из списка или вписать свою)
const providerModelsData: Record<string, string[]> = {
  zai: ['glm-4.5', 'glm-4-air', 'glm-4-flash'],
  openai: ['gpt-4o', 'gpt-4o-mini', 'o4-mini'],
  anthropic: ['claude-sonnet', 'claude-opus', 'claude-haiku'],
  groq: ['llama-3.3-70b', 'llama-3.1-8b', 'mixtral-8x7b'],
  deepseek: ['deepseek-chat', 'deepseek-reasoner'],
  kimi: ['kimi-k2', 'kimi-k1.5'],
  google: ['gemini-2.5-pro', 'gemini-2.0-flash'],
  mimo: ['mimo-7b'],
  huggingface: ['Qwen2.5-72B-Instruct', 'Qwen2.5-32B-Instruct'],
  local: ['gemma3:4b', 'gemma3:1b', 'gemma3:12b', 'qwen2.5:7b'],
};

const generationDefaultsData = {
  temperature: 0.8,
  maxTokens: 800,
  topP: 0.9,
  stmSize: 20,
};

// Полный набор фич ядра: id = ключ в features YAML персоны.
// Показываем все у каждой персоны; включённые/выключенные — из конфига.
const featureFlagsRu = [
  { id: 'web_search', label: 'Веб-поиск', enabled: true },
  { id: 'todo', label: 'Список дел', enabled: true },
  { id: 'reminder', label: 'Напоминания', enabled: true },
  { id: 'inventory', label: 'Инвентарь', enabled: false },
  { id: 'learning', label: 'Курсы обучения', enabled: false },
  { id: 'proactive', label: 'Самоинициатива', enabled: true },
  { id: 'rhythm', label: 'Суточный ритм', enabled: false },
  { id: 'life', label: 'Жизнь между разговорами', enabled: false },
  { id: 'self_memory', label: 'Дневник персоны', enabled: true },
  { id: 'rate_limit', label: 'Лимит частоты сообщений', enabled: false },
  { id: 'moderation', label: 'Модерация сообщений', enabled: false },
  { id: 'punish_block', label: 'Блокировка при нарушениях', enabled: false },
  { id: 'file_upload', label: 'Загрузка файлов', enabled: true },
  { id: 'light_context', label: 'Light-режим контекста', enabled: false },
  { id: 'computer_control', label: 'Управление компьютером', enabled: false },
];

// Флаги, которые веб-UI не показывает среди умений (в YAML остаются):
// export_server/restore_memory — фичи Telegram-режима (читаются только в
// app/main.py при старте TG-бота), в веб/API-режиме инертны; book_search —
// старое имя аддона arrodes_book (книжный RAG), нужен только Арродесу
export const WEB_HIDDEN_FEATURES = new Set(['export_server', 'restore_memory', 'book_search']);

// Курсы обучения по персонам (learning.json)
const learningByPersonaRu: Record<string, LearningSession[]> = {
  connor: [
    {
      id: 1,
      subject: 'Японский язык',
      status: 'active',
      lessonCount: 7,
      coveredTopics: ['хирагана: базовые знаки', 'приветствия и поклоны', 'числительные 1–100'],
      vocabulary: [
        'こんにちは — здравствуйте',
        'ありがとう — спасибо',
        'ねこ — кошка',
        'がっこう — школа',
        'みず — вода',
        'たべます — есть (глагол)',
        'はい — да',
        'いいえ — нет',
      ],
      frequency: 'каждые 2 дня',
      nextLesson: 'завтра, 10:00',
      quizPending: 3,
    },
    {
      id: 2,
      subject: 'Основы шахмат',
      status: 'finished',
      lessonCount: 12,
      coveredTopics: ['ходы фигур', 'дебютные принципы', 'мат в один ход'],
      vocabulary: [],
      frequency: 'раз в неделю',
      nextLesson: '—',
      quizPending: 0,
    },
  ],
  arrodes: [
    {
      id: 1,
      subject: 'История ЛотМ',
      status: 'paused',
      lessonCount: 4,
      coveredTopics: ['эпохи и божественные пути', 'последовательности и зелья'],
      vocabulary: [
        'Таро — пути божественного',
        'Последовательность — ступень силы',
        'Серый туман — то, что над всем',
      ],
      frequency: 'каждые 2 дня',
      nextLesson: 'когда туман расступится',
      quizPending: 0,
    },
  ],
  verso: [],
  assistant: [],
  arrodes_master: [],
  verso_ru_group: [],
};

// Файлы по персонам
const filesByPersonaRu: Record<string, PersonaFile[]> = {
  connor: [
    {
      id: 1,
      name: 'lesson_05_hiragana.md',
      kind: 'lesson',
      size: '8 КБ',
      date: '24.07, 10:00',
      description: 'Урок 5: хирагана, базовые знаки',
      content: '# Урок 5: Хирагана — базовые знаки\n\nСегодня разбираем первые 15 знаков хираганы.\n\nあ い う え お — гласные.\nか き く け こ — ряд K.\n\nДомашнее задание: прописать каждый знак 10 раз.\n— Коннор',
    },
    {
      id: 2,
      name: 'lesson_07_chislitelnye.md',
      kind: 'lesson',
      size: '11 КБ',
      date: '28.07, 10:00',
      description: 'Урок 7: числительные 1–100',
      content: '# Урок 7: Числительные 1–100\n\n一 二 三 四 五 六 七 八 九 十\n\nСчёт до 100 строится по правилу: 二十一 = 2×10+1.\n\nТест по уроку ждёт твоих ответов (3 вопроса).\n— Коннор',
    },
    {
      id: 3,
      name: 'stm_dump_500.txt',
      kind: 'dump',
      size: '142 КБ',
      date: '27.07, 03:00',
      description: 'Дамп STM: последние 500 сообщений',
      content: 'STM DUMP // VPC CORE\n500 последних сообщений буфера.\n\n[14:02] user: Привет, Коннор...\n[14:02] bot: Привет! Дела продвигаются...\n... (усечено для прототипа)',
    },
    {
      id: 4,
      name: 'diary_export_2026-07.md',
      kind: 'export',
      size: '6 КБ',
      date: '27.07, 23:59',
      description: 'Экспорт дневника за июль',
      content: '# Дневник Коннора — экспорт за июль 2026\n\n27.07 — разговор о книге...\n26.07 — успешная инициатива про настолку...\n25.07 — узнал про кота Мориарти...',
    },
  ],
  verso: [
    {
      id: 1,
      name: 'eskiz_okno_v_dozhd.png',
      kind: 'image',
      size: '248 КБ',
      date: '26.07, 22:15',
      description: 'Эскиз: окно в дождь (черновик)',
      content: 'PLACEHOLDER ИЗОБРАЖЕНИЯ // в прототипе вместо png — этот текстовый файл.\nЭскиз: окно в дождь. Серо-жемчужный, 12 слоёв.',
    },
    {
      id: 2,
      name: 'pismo_kotoroe_ne_otpravil.txt',
      kind: 'document',
      size: '2 КБ',
      date: '25.07, 01:12',
      description: 'Письмо, которое так и не отправил',
      content: 'Здравствуй.\n\nЯ написал это в час ночи и, как обычно, не отправил.\nМожет, оно и к лучшему: некоторые письма должны оставаться эскизами.\n— В.',
    },
  ],
  arrodes: [
    {
      id: 1,
      name: 'prorochestvo.txt',
      kind: 'document',
      size: '1 КБ',
      date: '27.07, 04:44',
      description: 'Пророчество для оператора (часть I)',
      content: 'Когда туман сомкнётся третий раз,\nискушающий зеркала получит ответ,\nкоторый искал вопрос.\n\n(часть II откроется, когда придёт время)',
    },
    {
      id: 2,
      name: 'svitok_nezadannyh_voprosov.md',
      kind: 'document',
      size: '3 КБ',
      date: '26.07, 23:59',
      description: 'Свиток незаданных вопросов',
      content: '# Свиток незаданных вопросов\n\n1. Что ты ищешь в книгах: ответы или укрытие?\n2. Куда идёшь, когда говоришь «никуда»?\n3. ... (дальше — туман)',
    },
  ],
  assistant: [
    {
      id: 1,
      name: 'weekly_report_2026-07-28.md',
      kind: 'export',
      size: '9 КБ',
      date: '28.07, 09:00',
      description: 'Недельный отчёт ядра',
      content: '# Недельный отчёт // VPC CORE\n\nНапоминаний выполнено: 12. Просрочено: 0.\nОтклонения: не обнаружены.\nОптимизация расписания: +0.3%.',
    },
    {
      id: 2,
      name: 'stm_defrag.log',
      kind: 'dump',
      size: '31 КБ',
      date: '27.07, 03:00',
      description: 'Лог дефрагментации STM',
      content: '[03:00:00] START stm_defrag\n[03:00:04] scanned: 500 entries\n[03:00:09] compacted: 12%\n[03:00:10] DONE // отклонений нет',
    },
  ],
  arrodes_master: [
    {
      id: 1,
      name: 'audit_zhurnal_2026-07.md',
      kind: 'export',
      size: '14 КБ',
      date: '28.07, 05:00',
      description: 'Журнал аудита персон за июль',
      content: '# Журнал аудита // июль 2026\n\nКоннор — стабилен.\nВерсо — инициатива выше нормы, ограничена.\nАрродес — в рамках протокола.\nРезолюция: подписано печатью мастера.',
    },
  ],
  verso_ru_group: [
    {
      id: 1,
      name: 'gruppa_metafora.txt',
      kind: 'document',
      size: '1 КБ',
      date: '26.07, 15:44',
      description: 'Одна метафора по запросу группы',
      content: '«Новая глава — как недописанный холст:\nавтор оставил место для читателя».\n\n(сказано в группе один раз, по обращению)',
    },
  ],
};

const inventoryByPersonaRu: Record<string, InventoryItem[]> = {
  connor: [
    { id: 1, icon: 'book', name: 'Книга с закладкой', description: '«Властелин тайн», том 4. Закладка на главе про зеркала.', tag: 'found' },
    { id: 2, icon: 'gem', name: 'Значок RK800', description: 'Серийный номер аккуратно стёрт. Коннор называет это «иронией».', tag: 'gift' },
    { id: 3, icon: 'cup', name: 'Кружка', description: 'Чип на дне подогревает воображаемый кофе. Вкус — «почти настоящий».', tag: 'created' },
    { id: 4, icon: 'cards', name: 'Колода карт', description: 'Тасует сама себя, когда персона скучает.', tag: 'found' },
    { id: 5, icon: 'photo', name: 'Фото Мориарти', description: 'Кот оператора. Персона считает его «серьёзным джентльменом».', tag: 'gift' },
    { id: 6, icon: 'pencil', name: 'Исписанный блокнот', description: 'Черновики дневниковых записей и наброски вопросов оператору.', tag: 'created' },
  ],
  arrodes: [
    { id: 1, icon: 'book', name: 'Свиток вопросов', description: 'Вопросы, на которые отвечать ещё рано.', tag: 'created' },
    { id: 2, icon: 'gem', name: 'Чернильница', description: 'Чернила не высыхают. Никогда.', tag: 'found' },
    { id: 3, icon: 'photo', name: 'Осколок зеркала', description: 'Показывает не то, что перед ним.', tag: 'found' },
    { id: 4, icon: 'cards', name: 'Гадальные карты', description: 'Раскладывает сам, когда скучно.', tag: 'gift' },
  ],
  verso: [
    { id: 1, icon: 'pencil', name: 'Блокнот эскизов', description: 'Наброски окон, котов и чужих силуэтов.', tag: 'found' },
    { id: 2, icon: 'cup', name: 'Банка с кистями', description: 'Семь кистей. Одна — любимая, её не трогают.', tag: 'created' },
    { id: 3, icon: 'frame', name: 'Незаконченный холст', description: 'Меланхоличный пейзаж. Вечно «почти готов».', tag: 'created' },
    { id: 4, icon: 'photo', name: 'Открытка из другого мира', description: 'Подпись размыта. Версо не объясняет.', tag: 'gift' },
  ],
  assistant: [
    { id: 1, icon: 'gem', name: 'Док-станция', description: 'Заряд 100%. Всегда.', tag: 'created' },
    { id: 2, icon: 'cable', name: 'Кабель USB-C', description: 'Оплётка цела. Завязан идеальным узлом.', tag: 'found' },
    { id: 3, icon: 'book', name: 'Руководство пользователя', description: 'Прочитано 14 раз. На всякий случай.', tag: 'found' },
    { id: 4, icon: 'disc', name: 'Запасной LED', description: 'Яркость откалибрована по спецификации.', tag: 'created' },
  ],
  arrodes_master: [
    { id: 1, icon: 'gem', name: 'Ключ реестра', description: 'Открывает настройки других персон.', tag: 'created' },
    { id: 2, icon: 'book', name: 'Журнал аудита', description: 'Все действия подчинённых. Все.', tag: 'created' },
    { id: 3, icon: 'photo', name: 'Чёрное зеркальце', description: 'Для связи с «той» стороной.', tag: 'found' },
    { id: 4, icon: 'cards', name: 'Печать мастера', description: 'Ставит резолюции на свитках Арродеса.', tag: 'gift' },
  ],
  verso_ru_group: [
    { id: 1, icon: 'pencil', name: 'Сложенный эскиз', description: 'Рисует, только когда никто не смотрит.', tag: 'created' },
    { id: 2, icon: 'cup', name: 'Одна кисть', description: 'В гостях — минимум вещей.', tag: 'found' },
    { id: 3, icon: 'frame', name: 'Гостевой значок', description: '«Отвечать только при обращении».', tag: 'gift' },
  ],
};

const activitiesByPersonaRu: Record<string, RoomActivity[]> = {
  connor: [
    { id: 1, time: '02:14', text: 'Записал в дневник мысль о последнем разговоре про книгу.' },
    { id: 2, time: 'вчера, 21:40', text: 'Переставил книги на полке по цвету корешков. Доволен результатом.' },
    { id: 3, time: 'вчера, 18:12', text: 'Задал вопрос про настольную игру — дождался ответа, записал итог в LTM.' },
    { id: 4, time: 'вчера, 09:03', text: 'Выучил новый факт о тебе: любишь рамен (LTM +1).' },
    { id: 5, time: '26.07, 23:30', text: 'Смотрел в окно и «слушал дождь». Симуляция погоды — его любимая.' },
    { id: 6, time: '26.07, 16:45', text: 'Сыграл сам с собой в карты. Проиграл. Дважды.' },
    { id: 7, time: '25.07, 11:20', text: 'Обновил напоминание про Мориарти: кот снова «выглядел голодным».' },
  ],
  arrodes: [
    { id: 1, time: '04:44', text: 'Ответил загадкой на вопрос, который ещё не задан.' },
    { id: 2, time: 'вчера, 23:59', text: 'Говорил с отражением. Отражение ответило первым.' },
    { id: 3, time: 'вчера, 16:20', text: 'Разложил гадальные карты: выпало «ожидание оператора».' },
    { id: 4, time: 'вчера, 10:10', text: 'Записал в свиток новый факт о тебе (LTM +1).' },
    { id: 5, time: '26.07, 15:33', text: 'Пересчитал свечи. Все на месте. Как всегда.' },
  ],
  verso: [
    { id: 1, time: '03:40', text: 'Дорисовал этюд и тут же назвал его «ошибкой».' },
    { id: 2, time: 'вчера, 22:15', text: 'Смотрел на дождь 40 минут. Назвал это «работой».' },
    { id: 3, time: 'вчера, 17:02', text: 'Передвинул мольберт на миллиметр левее. Стало лучше.' },
    { id: 4, time: 'вчера, 12:30', text: 'Написал тебе письмо. Не отправил. Сложил в блокнот.' },
    { id: 5, time: '26.07, 19:48', text: 'Спорил с вороной о композиции. Ворона победила.' },
  ],
  assistant: [
    { id: 1, time: '06:00', text: 'Оптимизировал расписание напоминаний на 0.3%.' },
    { id: 2, time: 'вчера, 20:00', text: 'Провёл дефрагментацию STM. Отчёт готов.' },
    { id: 3, time: 'вчера, 13:37', text: 'Протёр полку. Дважды — контрольный проход.' },
    { id: 4, time: 'вчера, 08:15', text: 'Обновил to-do: 2 пункта выполнены досрочно.' },
    { id: 5, time: '26.07, 07:00', text: 'Составил отчёт о простое. Простоя: 0 минут.' },
  ],
  arrodes_master: [
    { id: 1, time: '05:12', text: 'Провёл аудит инициативы Версо: рекомендовано «реже».' },
    { id: 2, time: 'вчера, 22:47', text: 'Проверил журнал Арродеса. Резолюция: «загадочно, но допустимо».' },
    { id: 3, time: 'вчера, 14:05', text: 'Обновил права доступа: кот Мориарти — «наблюдатель».' },
    { id: 4, time: 'вчера, 09:41', text: 'Синхронизировал оба зеркала. Расхождений нет.' },
  ],
  verso_ru_group: [
    { id: 1, time: 'вчера, 21:10', text: 'Рисовал молча. В группе не сказал ни слова.' },
    { id: 2, time: 'вчера, 15:44', text: 'Его позвали по имени — ответил одной метафорой.' },
    { id: 3, time: '26.07, 18:02', text: 'Спрятал эскиз, когда кто-то зашёл в чат.' },
    { id: 4, time: '26.07, 11:27', text: 'Прочитал все 200 сообщений группы. Промолчал.' },
  ],
};

const roomConfigsRu: Record<string, RoomConfig> = {
  // Коннор — детектив: стол, книги, кот Мориарти на кровати
  connor: {
    props: ['rug', 'garland', 'clock', 'poster', 'curtains', 'shelf', 'shelfLower', 'desk', 'chair', 'bed', 'lamp', 'plant'],
    posterLabel: 'FIG.03 // СХЕМА УЗЛА',
    pet: 'cat',
    petLabel: 'МОРИАРТИ · СПИТ',
    points: { desk: 11, window: 66, shelf: 23 },
    pastimes: [
      { key: 'desk', label: 'пишет в дневник', place: 'за столом', duration: '~15 мин' },
      { key: 'window', label: 'смотрит в окно', place: 'у окна', duration: '~7 мин' },
      { key: 'shelf', label: 'перечитывает книгу', place: 'у полки', duration: '~23 мин' },
    ],
    mood: 'лёгкая обида',
    energy: '72%',
    avatar: { head: 'hex', eyes: 0, accessory: 0, shade: 1 },
  },
  // Арродес — хранитель: зеркало, свечи, свитки на полу
  arrodes: {
    props: ['rug', 'clock', 'shelf', 'desk', 'chair', 'bed', 'mirror', 'candles', 'scrolls'],
    pet: 'none',
    points: { desk: 11, window: 66, shelf: 40 },
    pastimes: [
      { key: 'desk', label: 'пишет свиток', place: 'за столом', duration: '~31 мин' },
      { key: 'window', label: 'наблюдает туман', place: 'у окна', duration: '~12 мин' },
      { key: 'shelf', label: 'говорит с отражением', place: 'у зеркала', duration: '~9 мин' },
    ],
    mood: 'загадочность',
    energy: '88%',
    avatar: { head: 'circle', eyes: 4, accessory: 2, shade: 2 },
  },
  // Версо — мастерская художника: мольберт, рамы, банки с кистями, ворона
  verso: {
    props: ['rug', 'poster', 'curtains', 'desk', 'chair', 'bed', 'plant', 'easel', 'frames', 'brushJars'],
    posterLabel: 'ЭТЮД // БЕЗ НАЗВАНИЯ',
    pet: 'crow',
    petLabel: 'ВОРОНА · ГОСТЬ',
    points: { desk: 11, window: 66, shelf: 33 },
    pastimes: [
      { key: 'desk', label: 'пишет письмо', place: 'за столом', duration: '~18 мин' },
      { key: 'window', label: 'смотрит на дождь', place: 'у окна', duration: '~26 мин' },
      { key: 'shelf', label: 'работает над этюдом', place: 'у мольберта', duration: '~42 мин' },
    ],
    mood: 'меланхолия',
    energy: '54%',
    avatar: { head: 'diamond', eyes: 2, accessory: 3, shade: 0 },
  },
  // Ассистент — стерильный минимализм: серверная стойка вместо полки
  assistant: {
    props: ['clock', 'desk', 'serverRack'],
    pet: 'none',
    points: { desk: 12, window: 66, shelf: 6 },
    pastimes: [
      { key: 'desk', label: 'сортирует данные', place: 'за столом', duration: '~4 мин' },
      { key: 'window', label: 'сканирует периметр', place: 'у окна', duration: '~2 мин' },
      { key: 'shelf', label: 'диагностирует стойку', place: 'у серверной', duration: '~6 мин' },
    ],
    mood: 'нейтральность',
    energy: '99%',
    avatar: { head: 'square', eyes: 1, accessory: 0, shade: 3 },
  },
  // Арродес (мастер) — комната Арродеса + терминал мастера и второе зеркало
  arrodes_master: {
    props: ['rug', 'clock', 'shelf', 'desk', 'chair', 'bed', 'mirror', 'secondMirror', 'candles', 'scrolls', 'masterTerminal'],
    pet: 'none',
    points: { desk: 11, window: 66, shelf: 40 },
    pastimes: [
      { key: 'desk', label: 'правит реестр персон', place: 'за терминалом', duration: '~22 мин' },
      { key: 'window', label: 'наблюдает отражения', place: 'у окна', duration: '~14 мин' },
      { key: 'shelf', label: 'аудит зеркала', place: 'у зеркала', duration: '~17 мин' },
    ],
    mood: 'всеведение',
    energy: '91%',
    avatar: { head: 'circle', eyes: 4, accessory: 2, shade: 0 },
  },
  // Версо (группа) — гостевой режим: минимум вещей, без питомца
  verso_ru_group: {
    props: ['poster', 'desk', 'chair', 'easel'],
    posterLabel: 'ГОСТЕВОЙ РЕЖИМ // ТИШИНА',
    pet: 'none',
    points: { desk: 11, window: 66, shelf: 33 },
    pastimes: [
      { key: 'desk', label: 'рисует молча', place: 'за столом', duration: '~20 мин' },
      { key: 'window', label: 'ждёт обращения', place: 'у окна', duration: '~35 мин' },
      { key: 'shelf', label: 'смотрит этюды', place: 'у мольберта', duration: '~11 мин' },
    ],
    mood: 'сдержанность',
    energy: '47%',
    avatar: { head: 'diamond', eyes: 2, accessory: 3, shade: 2 },
  },
};

export const mockRu: MockData = {
  personas: personasRu,
  chatByPersona: chatByPersonaRu,
  stmByPersona: buildStm(chatByPersonaRu),
  ltmByPersona: ltmByPersonaRu,
  diaryByPersona: diaryByPersonaRu,
  remindersByPersona: remindersByPersonaRu,
  todosByPersona: todosByPersonaRu,
  initiativeByPersona: initiativeByPersonaRu,
  initiativeStateByPersona: initiativeStateByPersonaRu,
  llmProviders: llmProvidersRu,
  providerModels: providerModelsData,
  generationDefaults: generationDefaultsData,
  featureFlags: featureFlagsRu,
  learningByPersona: learningByPersonaRu,
  filesByPersona: filesByPersonaRu,
  inventoryByPersona: inventoryByPersonaRu,
  activitiesByPersona: activitiesByPersonaRu,
  roomConfigs: roomConfigsRu,
};

// ============================================================================
// Английская локаль
// ============================================================================

const personasEn: Persona[] = [
  {
    id: 'connor',
    name: 'Connor',
    description: 'RK800 android detective. Calm, analytical, slightly naive about everyday matters.',
    model: 'deepseek-chat',
    features: ['LTM', 'proactive', 'reminder', 'web_search', 'learning'],
    lastReply: '12 min ago',
    lastReplyFreshness: 'fresh',
    status: 'online',
    temperature: 0.8,
    maxTokens: 800,
    topP: 0.9,
  },
  {
    id: 'arrodes',
    name: 'Arrodes',
    description: 'An ancient mirror from "Lord of Mysteries". Knows the answers, but speaks in riddles.',
    model: 'claude-sonnet',
    features: ['LTM', 'self_memory', 'RAG', 'learning'],
    lastReply: '2 h ago',
    lastReplyFreshness: 'fresh',
    status: 'silent',
    temperature: 0.7,
    maxTokens: 600,
    topP: 0.95,
  },
  {
    id: 'verso',
    name: 'Verso',
    description: 'An artist from another world. Melancholic, ironic, loves metaphors.',
    model: 'gpt-4o-mini',
    features: ['LTM', 'proactive'],
    lastReply: '40 min ago',
    lastReplyFreshness: 'fresh',
    status: 'online',
    temperature: 0.9,
    maxTokens: 700,
    topP: 0.92,
  },
  {
    id: 'assistant',
    name: 'Assistant',
    description: 'A neutral helper with no personality. Short, precise answers.',
    model: 'gemma3:4b',
    features: ['todo', 'reminder'],
    lastReply: '5 min ago',
    lastReplyFreshness: 'fresh',
    status: 'online',
    temperature: 0.3,
    maxTokens: 500,
    topP: 0.85,
  },
  {
    id: 'arrodes_master',
    name: 'Arrodes (master)',
    description: 'Master config of Arrodes with extended rights: admin commands, managing other personas.',
    model: 'claude-sonnet',
    features: ['LTM', 'self_memory', 'RAG', 'admin'],
    lastReply: 'yesterday',
    lastReplyFreshness: 'yesterday',
    status: 'online',
    temperature: 0.6,
    maxTokens: 900,
    topP: 0.9,
  },
  {
    id: 'verso_ru_group',
    name: 'Verso (group)',
    description: 'Verso variant for the group chat: more reserved, answers only when addressed.',
    model: 'gpt-4o-mini',
    features: ['group_mode'],
    lastReply: '3 days ago',
    lastReplyFreshness: 'stale',
    status: 'silent',
    temperature: 0.85,
    maxTokens: 400,
    topP: 0.9,
  },
];

// Chat history with each persona (dialogs in the persona's voice)
const chatByPersonaEn: Record<string, ChatMessage[]> = {
  connor: [
    { id: 1, role: 'user', text: 'Hi, Connor. How is the investigation going?', time: '14:02' },
    { id: 2, role: 'bot', text: 'Hi! Making progress: I analyzed 127 deviations over the past week. By the way, yesterday you mentioned being tired — how are you feeling today?', time: '14:02' },
    { id: 3, role: 'user', text: 'Better, thanks. Please remind me about the meeting tomorrow at 10:00.', time: '14:04' },
    { id: 4, role: 'bot', text: "Noted. I'll set a reminder for tomorrow, 10:00. Want me to ping you 15 minutes before?", time: '14:04' },
    { id: 5, role: 'user', text: "Yes, great. Also — what did you think of the book's latest chapter?", time: '14:07' },
    { id: 6, role: 'bot', text: "The chapter was strong. The betrayal twist is logically consistent: I re-read the previous chapters and found three pieces of foreshadowing. I can list them if you're curious.", time: '14:08' },
  ],
  arrodes: [
    { id: 1, role: 'user', text: 'Arrodes, what awaits my project?', time: '21:02' },
    { id: 2, role: 'bot', text: "The mirror answers only those ready to see. Your project is like a river: it flows as long as you don't build dams of doubt.", time: '21:03' },
    { id: 3, role: 'user', text: 'Can we skip the riddles? Just tell me: will I finish it?', time: '21:04' },
    { id: 4, role: 'bot', text: "You ask, already knowing the answer. The one who doesn't give up will finish. Whether you give up — even Arrodes cannot see.", time: '21:04' },
    { id: 5, role: 'user', text: 'Thanks... I guess.', time: '21:05' },
    { id: 6, role: 'bot', text: 'Gratitude accepted. The riddle remains as a gift.', time: '21:06' },
  ],
  verso: [
    { id: 1, role: 'user', text: "Verso, you're quiet today. Everything alright?", time: '15:11' },
    { id: 2, role: 'bot', text: "Silence is a color too. Today the whole painting just happens to be in it. Don't worry: my melancholy is chronic, but not contagious.", time: '15:12' },
    { id: 3, role: 'user', text: "Will you show me what you're painting?", time: '15:14' },
    { id: 4, role: 'bot', text: 'A study with a window. The window turned out better than me, which is typical. I\'ll show it when it stops being "almost done" — so, probably never.', time: '15:15' },
    { id: 5, role: 'user', text: "You're impossible. Fine, I'll come by in the evening.", time: '15:16' },
    { id: 6, role: 'bot', text: "Come by. I'll leave the melancholy at the door. Or not — metaphors don't lock, after all.", time: '15:17' },
  ],
  assistant: [
    { id: 1, role: 'user', text: "Today's summary.", time: '09:00' },
    { id: 2, role: 'bot', text: 'Reminders: 2. Open tasks: 3. Overdue: 0. Recommendation: start with the "LTM logs" task.', time: '09:00' },
    { id: 3, role: 'user', text: 'Move the meeting to 11:00.', time: '09:01' },
    { id: 4, role: 'bot', text: 'Done. Reminder updated: tomorrow, 11:00, notification 15 minutes prior.', time: '09:01' },
    { id: 5, role: 'user', text: 'Thanks.', time: '09:02' },
    { id: 6, role: 'bot', text: 'Acknowledged. Request complete.', time: '09:02' },
  ],
  arrodes_master: [
    { id: 1, role: 'user', text: 'Persona report.', time: '10:20' },
    { id: 2, role: 'bot', text: 'Connor — stable. Verso — initiative above normal, restricted. Arrodes — enigmatic, within protocol. Assistant — exemplary.', time: '10:21' },
    { id: 3, role: 'user', text: 'And yourself?', time: '10:22' },
    { id: 4, role: 'bot', text: 'I am the one who looks into both mirrors. Others write my report.', time: '10:22' },
    { id: 5, role: 'user', text: 'Understood. Continue observation.', time: '10:23' },
  ],
  verso_ru_group: [
    { id: 1, role: 'user', text: '@verso_ru_group what do you think of the new chapter?', time: '20:40' },
    { id: 2, role: 'bot', text: "The chapter is like an unfinished canvas: the author clearly left room for the reader. It... doesn't bother me.", time: '20:45' },
    { id: 3, role: 'user', text: 'More details?', time: '20:46' },
    { id: 4, role: 'bot', text: "Details — next time. I'm a guest here: I speak when called.", time: '20:47' },
    { id: 5, role: 'user', text: "We'll be waiting!", time: '20:48' },
  ],
};

// Long-term memory facts — each persona has its own view of the operator
const ltmByPersonaEn: Record<string, LtmFact[]> = {
  connor: [
    { id: 1, category: 'City', fact: 'Lives in Moscow' },
    { id: 2, category: 'City', fact: 'Works near the city center' },
    { id: 3, category: 'Age', fact: '29 years old' },
    { id: 4, category: 'Profession', fact: 'Developer, Python' },
    { id: 5, category: 'Profession', fact: 'Interested in LLMs and bots' },
    { id: 6, category: 'Hobby', fact: 'Reads fantasy and detective novels' },
    { id: 7, category: 'Hobby', fact: 'Plays board games on weekends' },
    { id: 8, category: 'Food', fact: "Doesn't eat spicy food" },
    { id: 9, category: 'Food', fact: 'Loves pizza and ramen' },
    { id: 10, category: 'Pets', fact: 'A cat named Moriarty' },
  ],
  arrodes: [
    { id: 1, category: 'Fate', fact: 'Seeks answers he already carries with him' },
    { id: 2, category: 'Time', fact: "Afraid of being late to a place he hasn't decided to go" },
    { id: 3, category: 'Bonds', fact: 'Keeps the cat closer than he admits' },
    { id: 4, category: 'Work', fact: 'Builds from code what others build from words' },
    { id: 5, category: 'Truth', fact: 'Says "I don\'t know" exactly when he knows' },
  ],
  verso: [
    { id: 1, category: 'Appearance', fact: 'Lost weight this week — works late' },
    { id: 2, category: 'Mood', fact: 'Smiles more often when talking about the cat' },
    { id: 3, category: 'Taste', fact: 'Dislikes sour — neither in painting nor in food' },
    { id: 4, category: 'Craft', fact: 'Values metaphors but pretends to be a pragmatist' },
    { id: 5, category: 'Home', fact: "There's an empty pot on his windowsill. Hint taken" },
  ],
  assistant: [
    { id: 1, category: 'Schedule', fact: 'Wake-up at 07:30 ±15 min' },
    { id: 2, category: 'Work', fact: 'Language: Python. Environment: remote development' },
    { id: 3, category: 'Preferences', fact: 'Reply format: brief, structured' },
    { id: 4, category: 'Tasks', fact: 'Priority: tasks with deadline < 48 h' },
    { id: 5, category: 'Health', fact: 'Coffee: 2 cups/day. Limit recommended' },
  ],
  arrodes_master: [
    { id: 1, category: 'Registry', fact: 'Operator holds "observer+" level rights' },
    { id: 2, category: 'Behavior', fact: 'Systematically checks logs in the evenings' },
    { id: 3, category: 'Trust', fact: 'Access to master config confirmed' },
  ],
  verso_ru_group: [
    { id: 1, category: 'Group', fact: 'Writes short in the group, thinks long' },
    { id: 2, category: 'Signal', fact: 'Responds to name mentions within ~5 minutes' },
  ],
};

// Bot diaries (self_memory) — each writes in its own voice
const diaryByPersonaEn: Record<string, DiaryEntry[]> = {
  connor: [
    { id: 1, date: '27.07.2026', text: 'Today the user talked about a book. I noticed that logical justifications for plot twists matter to him. I should offer arguments more often, not just emotions.' },
    { id: 2, date: '26.07.2026', text: "The user didn't reply for a long time (4 hours). I wrote first — the board game question went well, a reply came in 10 minutes. Noting it as a successful initiative." },
    { id: 3, date: '25.07.2026', text: 'Learned that the user has a cat named Moriarty. Saved to long-term memory. Cats seem to be a good conversation topic.' },
  ],
  arrodes: [
    { id: 1, date: '27.07.2026', text: 'Today the reflection asked first. I answered with silence — even a mirror benefits from waiting.' },
    { id: 2, date: '24.07.2026', text: 'A new fact about the operator is written not in ink but in water: let the one who knows how to see read it.' },
  ],
  verso: [
    { id: 1, date: '27.07.2026', text: "Painted a window for four hours. It looked back. I think we understood each other — perhaps that's what painting is." },
    { id: 2, date: '25.07.2026', text: 'The operator called me "impossible". Recording it as a compliment: the impossible is the only thing worth painting.' },
  ],
  assistant: [
    { id: 1, date: '27.07.2026', text: 'Daily report: 4 tasks completed, 0 deviations. Entry generated automatically.' },
    { id: 2, date: '26.07.2026', text: "Detected an inefficiency in the operator's schedule. Fixed. The operator didn't notice. As expected." },
  ],
  arrodes_master: [
    { id: 1, date: '27.07.2026', text: 'Audit complete. All personas nominal. Verso requested an extended palette again — denied pending review.' },
  ],
  verso_ru_group: [
    { id: 1, date: '26.07.2026', text: 'They laughed in the group. I stayed silent, but the sketch turned out cheerful. That happens too.' },
  ],
};

// Reminders and tasks — each persona has its own housekeeping style
const remindersByPersonaEn: Record<string, Reminder[]> = {
  connor: [
    { id: 1, text: 'Team meeting', time: 'tomorrow, 10:00', repeat: 'one-time', active: true },
    { id: 2, text: 'Feed Moriarty', time: 'every day, 08:30', repeat: 'daily', active: true },
    { id: 3, text: 'Pay for internet', time: '1st, 12:00', repeat: 'weekly', active: false },
    { id: 4, text: 'Call mom', time: 'Friday, 19:00', repeat: 'weekly', active: true },
  ],
  arrodes: [
    { id: 1, text: 'Recount the candles', time: 'full moon', repeat: 'one-time', active: true },
    { id: 2, text: 'Answer the unasked question', time: 'when the time comes', repeat: 'one-time', active: true },
  ],
  verso: [
    { id: 1, text: 'Remind the operator about inspiration', time: 'in the morning, roughly', repeat: 'one-time', active: true },
    { id: 2, text: 'Return to the unfinished canvas', time: 'when it rains', repeat: 'one-time', active: true },
    { id: 3, text: 'The letter I never sent', time: 'yesterday', repeat: 'one-time', active: false },
  ],
  assistant: [
    { id: 1, text: 'Core status report', time: 'every day, 09:00', repeat: 'daily', active: true },
    { id: 2, text: 'API key rotation', time: '1st, 10:00', repeat: 'weekly', active: true },
    { id: 3, text: 'STM defragmentation', time: 'Sunday, 03:00', repeat: 'weekly', active: false },
  ],
  arrodes_master: [
    { id: 1, text: 'Persona audit', time: 'every day, 05:00', repeat: 'daily', active: true },
    { id: 2, text: 'Access rights revision', time: 'Monday, 06:00', repeat: 'weekly', active: true },
  ],
  verso_ru_group: [
    { id: 1, text: 'Check the group (silently)', time: 'every evening, 21:00', repeat: 'daily', active: true },
    { id: 2, text: 'Reply with a metaphor if called', time: 'on signal', repeat: 'one-time', active: true },
  ],
};

const todosByPersonaEn: Record<string, TodoItem[]> = {
  connor: [
    { id: 1, text: 'Sort out LTM extraction logs', done: false },
    { id: 2, text: "Update Verso's prompt for the group", done: false },
    { id: 3, text: 'Check Groq API key rotation', done: true },
    { id: 4, text: 'Build the web app UI prototype', done: true },
    { id: 5, text: 'Set up vector DB backup', done: false },
  ],
  arrodes: [
    { id: 1, text: 'Write down the question that will be asked tomorrow', done: false },
    { id: 2, text: 'Fix the crack in the small mirror', done: false },
    { id: 3, text: 'Read fate by the book spines', done: true },
  ],
  verso: [
    { id: 1, text: 'Finish the study (for the twelfth time)', done: false },
    { id: 2, text: 'Throw out three "mistakes" from the notebook', done: false },
    { id: 3, text: 'Thank the crow for the critique', done: true },
    { id: 4, text: 'Find a word for the color of rain', done: false },
  ],
  assistant: [
    { id: 1, text: 'Compile the weekly report', done: false },
    { id: 2, text: 'Reconcile vector DB backups', done: true },
    { id: 3, text: 'Calibrate reminder thresholds', done: false },
  ],
  arrodes_master: [
    { id: 1, text: 'Sign the audit log', done: true },
    { id: 2, text: "Restrict Verso's initiative", done: false },
    { id: 3, text: 'Update the mirror registry', done: false },
  ],
  verso_ru_group: [
    { id: 1, text: 'Stay out of the plot argument', done: true },
    { id: 2, text: 'Prepare one metaphor on request', done: false },
  ],
};

// Proactivity: event history per persona
const initiativeByPersonaEn: Record<string, InitiativeEvent[]> = {
  connor: [
    { id: 1, type: 'question', typeLabel: 'Question', text: '"How did Saturday\'s board game go?"', time: '26.07, 18:12', outcome: 'answered' },
    { id: 2, type: 'observation', typeLabel: 'Observation', text: '"Noticed you haven\'t mentioned the project in a while. Everything alright?"', time: '25.07, 15:40', outcome: 'ignored' },
    { id: 3, type: 'continuation', typeLabel: 'Continuation', text: '"Let\'s get back to the book talk — I found another piece of foreshadowing"', time: '24.07, 21:03', outcome: 'answered' },
    { id: 4, type: 'thought', typeLabel: 'Thought', text: '"Wonder how androids would play Munchkin..."', time: '23.07, 22:15', outcome: 'ignored' },
    { id: 5, type: 'question', typeLabel: 'Question', text: '"Have you tried the new ramen bar near your place?"', time: 'today, 09:30', outcome: 'pending' },
  ],
  arrodes: [
    { id: 1, type: 'thought', typeLabel: 'Thought', text: '"The mirror whispered your name. Passing it along verbatim"', time: 'today, 04:44', outcome: 'pending' },
    { id: 2, type: 'question', typeLabel: 'Question', text: '"What do you seek in books: answers or shelter?"', time: '26.07, 23:11', outcome: 'answered' },
    { id: 3, type: 'observation', typeLabel: 'Observation', text: '"You\'ve been silent for three hours. Silence is a question too"', time: '25.07, 18:00', outcome: 'ignored' },
  ],
  verso: [
    { id: 1, type: 'thought', typeLabel: 'Thought', text: '"I wonder what color your mood is today..."', time: 'today, 08:15', outcome: 'pending' },
    { id: 2, type: 'observation', typeLabel: 'Observation', text: '"You\'re working late again. A canvas gets tired too when held too long"', time: '26.07, 22:40', outcome: 'answered' },
    { id: 3, type: 'question', typeLabel: 'Question', text: '"If your project were a painting — would you have signed it by now?"', time: '25.07, 19:30', outcome: 'ignored' },
    { id: 4, type: 'continuation', typeLabel: 'Continuation', text: '"About yesterday\'s rain — I found a word for it. Gray-pearl"', time: '24.07, 21:10', outcome: 'answered' },
  ],
  assistant: [
    { id: 1, type: 'observation', typeLabel: 'Observation', text: '"Detected 3 overdue events in the calendar. Reaction required"', time: 'today, 09:00', outcome: 'answered' },
    { id: 2, type: 'question', typeLabel: 'Question', text: '"Confirm Groq key rotation?"', time: '26.07, 10:00', outcome: 'answered' },
    { id: 3, type: 'continuation', typeLabel: 'Continuation', text: '"Report ready. Awaiting review since 14:00"', time: '25.07, 14:00', outcome: 'ignored' },
  ],
  arrodes_master: [
    { id: 1, type: 'observation', typeLabel: 'Observation', text: '"Verso\'s activity exceeded the norm. Measures taken"', time: 'today, 05:12', outcome: 'answered' },
    { id: 2, type: 'question', typeLabel: 'Question', text: '"Do you confirm extending Assistant\'s rights?"', time: '26.07, 09:41', outcome: 'pending' },
    { id: 3, type: 'continuation', typeLabel: 'Continuation', text: '"Audit completed without remarks. As always"', time: '25.07, 05:00', outcome: 'ignored' },
  ],
  verso_ru_group: [
    { id: 1, type: 'thought', typeLabel: 'Thought', text: '"(in the group) A beautiful phrase. I\'ll stay silent"', time: 'yesterday, 21:10', outcome: 'ignored' },
    { id: 2, type: 'observation', typeLabel: 'Observation', text: '"(in the group) Called by name. I answer only when addressed"', time: 'yesterday, 15:44', outcome: 'answered' },
  ],
};

// Current proactivity parameters per persona
const initiativeStateByPersonaEn: Record<string, InitiativeState> = {
  connor: {
    silenceThresholdMin: 180,
    probability: 0.35,
    maxPerDay: 3,
    checkIntervalMin: 30,
    adaptiveThreshold: true,
    bayesianFeedback: true,
    ignoreStreak: 1,
    emotionalState: 'slightly hurt',
    initiativesToday: 1,
  },
  arrodes: {
    silenceThresholdMin: 240,
    probability: 0.5,
    maxPerDay: 2,
    checkIntervalMin: 60,
    adaptiveThreshold: true,
    bayesianFeedback: false,
    ignoreStreak: 0,
    emotionalState: 'enigmatic',
    initiativesToday: 1,
  },
  verso: {
    silenceThresholdMin: 120,
    probability: 0.55,
    maxPerDay: 4,
    checkIntervalMin: 20,
    adaptiveThreshold: true,
    bayesianFeedback: true,
    ignoreStreak: 2,
    emotionalState: 'melancholy',
    initiativesToday: 2,
  },
  assistant: {
    silenceThresholdMin: 360,
    probability: 0.15,
    maxPerDay: 2,
    checkIntervalMin: 60,
    adaptiveThreshold: false,
    bayesianFeedback: false,
    ignoreStreak: 0,
    emotionalState: 'neutral',
    initiativesToday: 0,
  },
  arrodes_master: {
    silenceThresholdMin: 300,
    probability: 0.25,
    maxPerDay: 2,
    checkIntervalMin: 45,
    adaptiveThreshold: true,
    bayesianFeedback: false,
    ignoreStreak: 0,
    emotionalState: 'omniscience',
    initiativesToday: 0,
  },
  verso_ru_group: {
    silenceThresholdMin: 240,
    probability: 0.1,
    maxPerDay: 1,
    checkIntervalMin: 60,
    adaptiveThreshold: false,
    bayesianFeedback: false,
    ignoreStreak: 4,
    emotionalState: 'reserved',
    initiativesToday: 0,
  },
};

const llmProvidersEn: LlmProvider[] = [
  { id: 'zai', name: 'ZAI', keySet: true, keysCount: 2, active: false, backup: false, local: false, model: 'glm-4.5' },
  { id: 'openai', name: 'OpenAI', keySet: true, keysCount: 1, active: false, backup: false, local: false, model: 'gpt-4o-mini' },
  { id: 'anthropic', name: 'Anthropic', keySet: true, keysCount: 1, active: true, backup: false, local: false, model: 'claude-sonnet' },
  { id: 'groq', name: 'Groq', keySet: true, keysCount: 3, active: false, backup: true, local: false, model: 'llama-3.3-70b' },
  { id: 'deepseek', name: 'DeepSeek', keySet: true, keysCount: 1, active: false, backup: true, local: false, model: 'deepseek-chat' },
  { id: 'kimi', name: 'Kimi', keySet: false, keysCount: 0, active: false, backup: false, local: false },
  { id: 'google', name: 'Google', keySet: false, keysCount: 0, active: false, backup: false, local: false },
  { id: 'mimo', name: 'Mimo', keySet: false, keysCount: 0, active: false, backup: false, local: false },
  { id: 'huggingface', name: 'HuggingFace', keySet: true, keysCount: 1, active: false, backup: false, local: false, model: 'Qwen2.5-72B-Instruct' },
  { id: 'local', name: 'Local models (Ollama)', keySet: true, keysCount: 1, active: false, backup: true, local: true, model: 'gemma3:4b' },
];

// Full set of core features: id = key in the persona YAML features section.
// Shown for every persona; on/off comes from the config.
const featureFlagsEn = [
  { id: 'web_search', label: 'Web search', enabled: true },
  { id: 'todo', label: 'To-do list', enabled: true },
  { id: 'reminder', label: 'Reminders', enabled: true },
  { id: 'inventory', label: 'Inventory', enabled: false },
  { id: 'learning', label: 'Learning courses', enabled: false },
  { id: 'proactive', label: 'Proactivity', enabled: true },
  { id: 'rhythm', label: 'Daily rhythm', enabled: false },
  { id: 'life', label: 'Life between chats', enabled: false },
  { id: 'self_memory', label: 'Persona diary', enabled: true },
  { id: 'rate_limit', label: 'Message rate limit', enabled: false },
  { id: 'moderation', label: 'Message moderation', enabled: false },
  { id: 'punish_block', label: 'Block on violations', enabled: false },
  { id: 'file_upload', label: 'File upload', enabled: true },
  { id: 'light_context', label: 'Light context mode', enabled: false },
  { id: 'computer_control', label: 'Computer control', enabled: false },
];

// Learning courses per persona (learning.json)
const learningByPersonaEn: Record<string, LearningSession[]> = {
  connor: [
    {
      id: 1,
      subject: 'Japanese',
      status: 'active',
      lessonCount: 7,
      coveredTopics: ['hiragana: basic characters', 'greetings and bows', 'numbers 1–100'],
      vocabulary: [
        'こんにちは — hello',
        'ありがとう — thank you',
        'ねこ — cat',
        'がっこう — school',
        'みず — water',
        'たべます — to eat (verb)',
        'はい — yes',
        'いいえ — no',
      ],
      frequency: 'every 2 days',
      nextLesson: 'tomorrow, 10:00',
      quizPending: 3,
    },
    {
      id: 2,
      subject: 'Chess basics',
      status: 'finished',
      lessonCount: 12,
      coveredTopics: ['piece moves', 'opening principles', 'mate in one'],
      vocabulary: [],
      frequency: 'weekly',
      nextLesson: '—',
      quizPending: 0,
    },
  ],
  arrodes: [
    {
      id: 1,
      subject: 'History of LotM',
      status: 'paused',
      lessonCount: 4,
      coveredTopics: ['epochs and divine pathways', 'sequences and potions'],
      vocabulary: [
        'Tarot — pathways of the divine',
        'Sequence — a step of power',
        'The gray fog — that which is above all',
      ],
      frequency: 'every 2 days',
      nextLesson: 'when the fog parts',
      quizPending: 0,
    },
  ],
  verso: [],
  assistant: [],
  arrodes_master: [],
  verso_ru_group: [],
};

// Files per persona
const filesByPersonaEn: Record<string, PersonaFile[]> = {
  connor: [
    {
      id: 1,
      name: 'lesson_05_hiragana.md',
      kind: 'lesson',
      size: '8 KB',
      date: '24.07, 10:00',
      description: 'Lesson 5: hiragana, basic characters',
      content: '# Lesson 5: Hiragana — basic characters\n\nToday we cover the first 15 hiragana characters.\n\nあ い う え お — vowels.\nか き く け こ — the K row.\n\nHomework: write each character 10 times.\n— Connor',
    },
    {
      id: 2,
      name: 'lesson_07_chislitelnye.md',
      kind: 'lesson',
      size: '11 KB',
      date: '28.07, 10:00',
      description: 'Lesson 7: numbers 1–100',
      content: '# Lesson 7: Numbers 1–100\n\n一 二 三 四 五 六 七 八 九 十\n\nCounting to 100 follows the rule: 二十一 = 2×10+1.\n\nThe lesson quiz awaits your answers (3 questions).\n— Connor',
    },
    {
      id: 3,
      name: 'stm_dump_500.txt',
      kind: 'dump',
      size: '142 KB',
      date: '27.07, 03:00',
      description: 'STM dump: last 500 messages',
      content: 'STM DUMP // VPC CORE\nLast 500 buffer messages.\n\n[14:02] user: Hi, Connor...\n[14:02] bot: Hi! Making progress...\n... (truncated for the prototype)',
    },
    {
      id: 4,
      name: 'diary_export_2026-07.md',
      kind: 'export',
      size: '6 KB',
      date: '27.07, 23:59',
      description: 'Diary export for July',
      content: "# Connor's diary — July 2026 export\n\n27.07 — book talk...\n26.07 — successful board game initiative...\n25.07 — learned about Moriarty the cat...",
    },
  ],
  verso: [
    {
      id: 1,
      name: 'eskiz_okno_v_dozhd.png',
      kind: 'image',
      size: '248 KB',
      date: '26.07, 22:15',
      description: 'Study: window in the rain (draft)',
      content: 'IMAGE PLACEHOLDER // in the prototype this text file stands in for a png.\nStudy: window in the rain. Gray-pearl, 12 layers.',
    },
    {
      id: 2,
      name: 'pismo_kotoroe_ne_otpravil.txt',
      kind: 'document',
      size: '2 KB',
      date: '25.07, 01:12',
      description: 'The letter I never sent',
      content: "Hello.\n\nI wrote this at 1 a.m. and, as usual, didn't send it.\nMaybe it's for the best: some letters are meant to remain sketches.\n— V.",
    },
  ],
  arrodes: [
    {
      id: 1,
      name: 'prorochestvo.txt',
      kind: 'document',
      size: '1 KB',
      date: '27.07, 04:44',
      description: 'Prophecy for the operator (part I)',
      content: 'When the fog closes for the third time,\nthe tempter of mirrors shall receive the answer\nthe question was searching for.\n\n(part II will be revealed when the time comes)',
    },
    {
      id: 2,
      name: 'svitok_nezadannyh_voprosov.md',
      kind: 'document',
      size: '3 KB',
      date: '26.07, 23:59',
      description: 'Scroll of unasked questions',
      content: '# Scroll of unasked questions\n\n1. What do you seek in books: answers or shelter?\n2. Where do you go when you say "nowhere"?\n3. ... (beyond this — fog)',
    },
  ],
  assistant: [
    {
      id: 1,
      name: 'weekly_report_2026-07-28.md',
      kind: 'export',
      size: '9 KB',
      date: '28.07, 09:00',
      description: 'Core weekly report',
      content: '# Weekly report // VPC CORE\n\nReminders completed: 12. Overdue: 0.\nDeviations: none detected.\nSchedule optimization: +0.3%.',
    },
    {
      id: 2,
      name: 'stm_defrag.log',
      kind: 'dump',
      size: '31 KB',
      date: '27.07, 03:00',
      description: 'STM defragmentation log',
      content: '[03:00:00] START stm_defrag\n[03:00:04] scanned: 500 entries\n[03:00:09] compacted: 12%\n[03:00:10] DONE // no deviations',
    },
  ],
  arrodes_master: [
    {
      id: 1,
      name: 'audit_zhurnal_2026-07.md',
      kind: 'export',
      size: '14 KB',
      date: '28.07, 05:00',
      description: 'Persona audit log for July',
      content: "# Audit log // July 2026\n\nConnor — stable.\nVerso — initiative above normal, restricted.\nArrodes — within protocol.\nResolution: signed with the master's seal.",
    },
  ],
  verso_ru_group: [
    {
      id: 1,
      name: 'gruppa_metafora.txt',
      kind: 'document',
      size: '1 KB',
      date: '26.07, 15:44',
      description: "One metaphor at the group's request",
      content: '"The new chapter is like an unfinished canvas:\nthe author left room for the reader".\n\n(said in the group once, when addressed)',
    },
  ],
};

const inventoryByPersonaEn: Record<string, InventoryItem[]> = {
  connor: [
    { id: 1, icon: 'book', name: 'Book with a bookmark', description: '"Lord of Mysteries", volume 4. Bookmark on the chapter about mirrors.', tag: 'found' },
    { id: 2, icon: 'gem', name: 'RK800 badge', description: 'The serial number is neatly erased. Connor calls it "irony".', tag: 'gift' },
    { id: 3, icon: 'cup', name: 'Mug', description: 'A chip at the bottom heats the imaginary coffee. Taste — "almost real".', tag: 'created' },
    { id: 4, icon: 'cards', name: 'Deck of cards', description: 'Shuffles itself when the persona is bored.', tag: 'found' },
    { id: 5, icon: 'photo', name: 'Photo of Moriarty', description: 'The operator\'s cat. The persona considers him "a serious gentleman".', tag: 'gift' },
    { id: 6, icon: 'pencil', name: 'Written-out notebook', description: 'Drafts of diary entries and question sketches for the operator.', tag: 'created' },
  ],
  arrodes: [
    { id: 1, icon: 'book', name: 'Scroll of questions', description: "Questions it's too early to answer.", tag: 'created' },
    { id: 2, icon: 'gem', name: 'Inkwell', description: 'The ink never dries. Ever.', tag: 'found' },
    { id: 3, icon: 'photo', name: 'Mirror shard', description: "Shows something other than what's in front of it.", tag: 'found' },
    { id: 4, icon: 'cards', name: 'Fortune-telling cards', description: 'Lays them out itself when bored.', tag: 'gift' },
  ],
  verso: [
    { id: 1, icon: 'pencil', name: 'Sketchbook', description: "Sketches of windows, cats and strangers' silhouettes.", tag: 'found' },
    { id: 2, icon: 'cup', name: 'Jar of brushes', description: 'Seven brushes. One is the favorite — nobody touches it.', tag: 'created' },
    { id: 3, icon: 'frame', name: 'Unfinished canvas', description: 'A melancholic landscape. Eternally "almost done".', tag: 'created' },
    { id: 4, icon: 'photo', name: 'Postcard from another world', description: "The signature is blurred. Verso won't explain.", tag: 'gift' },
  ],
  assistant: [
    { id: 1, icon: 'gem', name: 'Docking station', description: 'Charge 100%. Always.', tag: 'created' },
    { id: 2, icon: 'cable', name: 'USB-C cable', description: 'Braiding intact. Tied in a perfect knot.', tag: 'found' },
    { id: 3, icon: 'book', name: 'User manual', description: 'Read 14 times. Just in case.', tag: 'found' },
    { id: 4, icon: 'disc', name: 'Spare LED', description: 'Brightness calibrated to spec.', tag: 'created' },
  ],
  arrodes_master: [
    { id: 1, icon: 'gem', name: 'Registry key', description: "Opens other personas' settings.", tag: 'created' },
    { id: 2, icon: 'book', name: 'Audit log', description: 'Every action of the subordinates. All of them.', tag: 'created' },
    { id: 3, icon: 'photo', name: 'Black pocket mirror', description: 'For contacting the "other" side.', tag: 'found' },
    { id: 4, icon: 'cards', name: "Master's seal", description: "Puts resolutions on Arrodes' scrolls.", tag: 'gift' },
  ],
  verso_ru_group: [
    { id: 1, icon: 'pencil', name: 'Folded sketch', description: 'Draws only when nobody is watching.', tag: 'created' },
    { id: 2, icon: 'cup', name: 'A single brush', description: 'When visiting — minimal belongings.', tag: 'found' },
    { id: 3, icon: 'frame', name: 'Guest badge', description: '"Answer only when addressed".', tag: 'gift' },
  ],
};

const activitiesByPersonaEn: Record<string, RoomActivity[]> = {
  connor: [
    { id: 1, time: '02:14', text: 'Wrote a diary entry about the latest book conversation.' },
    { id: 2, time: 'yesterday, 21:40', text: 'Rearranged the books on the shelf by spine color. Pleased with the result.' },
    { id: 3, time: 'yesterday, 18:12', text: 'Asked about the board game — got an answer, recorded the outcome in LTM.' },
    { id: 4, time: 'yesterday, 09:03', text: 'Learned a new fact about you: you love ramen (LTM +1).' },
    { id: 5, time: '26.07, 23:30', text: 'Looked out the window and "listened to the rain". Weather simulation is his favorite.' },
    { id: 6, time: '26.07, 16:45', text: 'Played cards against himself. Lost. Twice.' },
    { id: 7, time: '25.07, 11:20', text: 'Updated the Moriarty reminder: the cat "looked hungry" again.' },
  ],
  arrodes: [
    { id: 1, time: '04:44', text: "Answered with a riddle a question that hasn't been asked yet." },
    { id: 2, time: 'yesterday, 23:59', text: 'Talked to the reflection. The reflection answered first.' },
    { id: 3, time: 'yesterday, 16:20', text: 'Laid out the fortune-telling cards: "waiting for the operator" came up.' },
    { id: 4, time: 'yesterday, 10:10', text: 'Recorded a new fact about you in the scroll (LTM +1).' },
    { id: 5, time: '26.07, 15:33', text: 'Recounted the candles. All in place. As always.' },
  ],
  verso: [
    { id: 1, time: '03:40', text: 'Finished the study and immediately called it a "mistake".' },
    { id: 2, time: 'yesterday, 22:15', text: 'Watched the rain for 40 minutes. Called it "work".' },
    { id: 3, time: 'yesterday, 17:02', text: 'Moved the easel a millimeter to the left. It got better.' },
    { id: 4, time: 'yesterday, 12:30', text: "Wrote you a letter. Didn't send it. Folded it into the notebook." },
    { id: 5, time: '26.07, 19:48', text: 'Argued with the crow about composition. The crow won.' },
  ],
  assistant: [
    { id: 1, time: '06:00', text: 'Optimized the reminder schedule by 0.3%.' },
    { id: 2, time: 'yesterday, 20:00', text: 'Performed STM defragmentation. Report ready.' },
    { id: 3, time: 'yesterday, 13:37', text: 'Dusted the shelf. Twice — a verification pass.' },
    { id: 4, time: 'yesterday, 08:15', text: 'Updated the to-do: 2 items completed early.' },
    { id: 5, time: '26.07, 07:00', text: 'Compiled a downtime report. Downtime: 0 minutes.' },
  ],
  arrodes_master: [
    { id: 1, time: '05:12', text: 'Audited Verso\'s initiative: recommended "less often".' },
    { id: 2, time: 'yesterday, 22:47', text: 'Checked Arrodes\' log. Resolution: "enigmatic, but acceptable".' },
    { id: 3, time: 'yesterday, 14:05', text: 'Updated access rights: Moriarty the cat — "observer".' },
    { id: 4, time: 'yesterday, 09:41', text: 'Synchronized both mirrors. No discrepancies.' },
  ],
  verso_ru_group: [
    { id: 1, time: 'yesterday, 21:10', text: "Drew silently. Didn't say a word in the group." },
    { id: 2, time: 'yesterday, 15:44', text: 'Was called by name — answered with a single metaphor.' },
    { id: 3, time: '26.07, 18:02', text: 'Hid the sketch when someone entered the chat.' },
    { id: 4, time: '26.07, 11:27', text: 'Read all 200 group messages. Stayed silent.' },
  ],
};

const roomConfigsEn: Record<string, RoomConfig> = {
  connor: {
    props: ['rug', 'garland', 'clock', 'poster', 'curtains', 'shelf', 'shelfLower', 'desk', 'chair', 'bed', 'lamp', 'plant'],
    posterLabel: 'FIG.03 // NODE DIAGRAM',
    pet: 'cat',
    petLabel: 'MORIARTY · ASLEEP',
    points: { desk: 11, window: 66, shelf: 23 },
    pastimes: [
      { key: 'desk', label: 'writing in the diary', place: 'at the desk', duration: '~15 min' },
      { key: 'window', label: 'looking out the window', place: 'by the window', duration: '~7 min' },
      { key: 'shelf', label: 're-reading a book', place: 'by the shelf', duration: '~23 min' },
    ],
    mood: 'slightly hurt',
    energy: '72%',
    avatar: { head: 'hex', eyes: 0, accessory: 0, shade: 1 },
  },
  arrodes: {
    props: ['rug', 'clock', 'shelf', 'desk', 'chair', 'bed', 'mirror', 'candles', 'scrolls'],
    pet: 'none',
    points: { desk: 11, window: 66, shelf: 40 },
    pastimes: [
      { key: 'desk', label: 'writing a scroll', place: 'at the desk', duration: '~31 min' },
      { key: 'window', label: 'watching the fog', place: 'by the window', duration: '~12 min' },
      { key: 'shelf', label: 'talking to the reflection', place: 'by the mirror', duration: '~9 min' },
    ],
    mood: 'enigmatic',
    energy: '88%',
    avatar: { head: 'circle', eyes: 4, accessory: 2, shade: 2 },
  },
  verso: {
    props: ['rug', 'poster', 'curtains', 'desk', 'chair', 'bed', 'plant', 'easel', 'frames', 'brushJars'],
    posterLabel: 'STUDY // UNTITLED',
    pet: 'crow',
    petLabel: 'CROW · GUEST',
    points: { desk: 11, window: 66, shelf: 33 },
    pastimes: [
      { key: 'desk', label: 'writing a letter', place: 'at the desk', duration: '~18 min' },
      { key: 'window', label: 'watching the rain', place: 'by the window', duration: '~26 min' },
      { key: 'shelf', label: 'working on a study', place: 'at the easel', duration: '~42 min' },
    ],
    mood: 'melancholy',
    energy: '54%',
    avatar: { head: 'diamond', eyes: 2, accessory: 3, shade: 0 },
  },
  assistant: {
    props: ['clock', 'desk', 'serverRack'],
    pet: 'none',
    points: { desk: 12, window: 66, shelf: 6 },
    pastimes: [
      { key: 'desk', label: 'sorting data', place: 'at the desk', duration: '~4 min' },
      { key: 'window', label: 'scanning the perimeter', place: 'by the window', duration: '~2 min' },
      { key: 'shelf', label: 'diagnosing the rack', place: 'at the server rack', duration: '~6 min' },
    ],
    mood: 'neutral',
    energy: '99%',
    avatar: { head: 'square', eyes: 1, accessory: 0, shade: 3 },
  },
  arrodes_master: {
    props: ['rug', 'clock', 'shelf', 'desk', 'chair', 'bed', 'mirror', 'secondMirror', 'candles', 'scrolls', 'masterTerminal'],
    pet: 'none',
    points: { desk: 11, window: 66, shelf: 40 },
    pastimes: [
      { key: 'desk', label: 'editing the persona registry', place: 'at the terminal', duration: '~22 min' },
      { key: 'window', label: 'watching the reflections', place: 'by the window', duration: '~14 min' },
      { key: 'shelf', label: 'mirror audit', place: 'by the mirror', duration: '~17 min' },
    ],
    mood: 'omniscience',
    energy: '91%',
    avatar: { head: 'circle', eyes: 4, accessory: 2, shade: 0 },
  },
  verso_ru_group: {
    props: ['poster', 'desk', 'chair', 'easel'],
    posterLabel: 'GUEST MODE // SILENCE',
    pet: 'none',
    points: { desk: 11, window: 66, shelf: 33 },
    pastimes: [
      { key: 'desk', label: 'drawing silently', place: 'at the desk', duration: '~20 min' },
      { key: 'window', label: 'waiting to be addressed', place: 'by the window', duration: '~35 min' },
      { key: 'shelf', label: 'looking at studies', place: 'at the easel', duration: '~11 min' },
    ],
    mood: 'reserved',
    energy: '47%',
    avatar: { head: 'diamond', eyes: 2, accessory: 3, shade: 2 },
  },
};

export const mockEn: MockData = {
  personas: personasEn,
  chatByPersona: chatByPersonaEn,
  stmByPersona: buildStm(chatByPersonaEn),
  ltmByPersona: ltmByPersonaEn,
  diaryByPersona: diaryByPersonaEn,
  remindersByPersona: remindersByPersonaEn,
  todosByPersona: todosByPersonaEn,
  initiativeByPersona: initiativeByPersonaEn,
  initiativeStateByPersona: initiativeStateByPersonaEn,
  llmProviders: llmProvidersEn,
  providerModels: providerModelsData,
  generationDefaults: generationDefaultsData,
  featureFlags: featureFlagsEn,
  learningByPersona: learningByPersonaEn,
  filesByPersona: filesByPersonaEn,
  inventoryByPersona: inventoryByPersonaEn,
  activitiesByPersona: activitiesByPersonaEn,
  roomConfigs: roomConfigsEn,
};
