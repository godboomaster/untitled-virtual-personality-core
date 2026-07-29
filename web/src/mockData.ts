// Моковые данные для UI-прототипа. Позже будут заменены на реальные запросы к API.

export interface Persona {
  id: string;
  name: string;
  description: string;
  model: string;
  features: string[];
  active: boolean;
  status: 'онлайн' | 'молчит' | 'печатает...';
  temperature: number;
  maxTokens: number;
  topP: number;
}

export interface ChatMessage {
  id: number;
  role: 'user' | 'bot';
  text: string;
  time: string;
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
  repeat: 'разовое' | 'ежедневно' | 'еженедельно';
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
  outcome: 'ответили' | 'проигнорировали' | 'ожидание';
}

export interface LlmProvider {
  id: string;
  name: string;
  keySet: boolean;
  keysCount: number;
  active: boolean;
  local: boolean;
  model?: string;
}

export const personas: Persona[] = [
  {
    id: 'connor',
    name: 'Коннор',
    description: 'Андроид-детектив RK800. Спокойный, аналитичный, слегка наивный в бытовых вопросах.',
    model: 'deepseek-chat',
    features: ['LTM', 'Инициатива', 'Напоминания', 'Веб-поиск'],
    active: true,
    status: 'онлайн',
    temperature: 0.8,
    maxTokens: 800,
    topP: 0.9,
  },
  {
    id: 'arrodes',
    name: 'Арродес',
    description: 'Древнее зеркало из «Властелина тайн». Знает ответы на вопросы, но говорит загадками.',
    model: 'claude-sonnet',
    features: ['LTM', 'Дневник', 'RAG'],
    active: false,
    status: 'молчит',
    temperature: 0.7,
    maxTokens: 600,
    topP: 0.95,
  },
  {
    id: 'verso',
    name: 'Версо',
    description: 'Художник из другого мира. Меланхоличный, ироничный, любит метафоры.',
    model: 'gpt-4o-mini',
    features: ['LTM', 'Инициатива'],
    active: false,
    status: 'онлайн',
    temperature: 0.9,
    maxTokens: 700,
    topP: 0.92,
  },
  {
    id: 'assistant',
    name: 'Ассистент',
    description: 'Нейтральный помощник без личности. Короткие точные ответы.',
    model: 'qwen2.5:7b',
    features: ['To-do', 'Напоминания'],
    active: false,
    status: 'онлайн',
    temperature: 0.3,
    maxTokens: 500,
    topP: 0.85,
  },
  {
    id: 'arrodes_master',
    name: 'Арродес (мастер)',
    description: 'Мастер-конфиг Арродеса с расширенными правами: админ-команды, управление другими персонами.',
    model: 'claude-sonnet',
    features: ['LTM', 'Дневник', 'RAG', 'Админ'],
    active: false,
    status: 'онлайн',
    temperature: 0.6,
    maxTokens: 900,
    topP: 0.9,
  },
  {
    id: 'verso_ru_group',
    name: 'Версо (группа)',
    description: 'Версия Версо для группового чата: сдержаннее, отвечает только при обращении.',
    model: 'gpt-4o-mini',
    features: ['Групповой режим'],
    active: false,
    status: 'молчит',
    temperature: 0.85,
    maxTokens: 400,
    topP: 0.9,
  },
];

export const chatMessages: ChatMessage[] = [
  {
    id: 1,
    role: 'user',
    text: 'Привет, Коннор. Как дела с расследованием?',
    time: '14:02',
  },
  {
    id: 2,
    role: 'bot',
    text: 'Привет! Дела продвигаются: я проанализировал 127 девиаций за прошлую неделю. Кстати, вчера ты упоминал, что устал — как самочувствие сегодня?',
    time: '14:02',
  },
  {
    id: 3,
    role: 'user',
    text: 'Получше, спасибо. Напомни, пожалуйста, про встречу завтра в 10:00.',
    time: '14:04',
  },
  {
    id: 4,
    role: 'bot',
    text: 'Зафиксировал. Создам напоминание на завтра, 10:00. Хочешь, напомню за 15 минут до начала?',
    time: '14:04',
  },
  {
    id: 5,
    role: 'user',
    text: 'Да, отлично. И ещё — что ты думаешь о последней главе книги?',
    time: '14:07',
  },
  {
    id: 6,
    role: 'bot',
    text: 'Глава была сильной. Поворот с предательством логически непротиворечив: я пересмотрел предыдущие главы и нашёл три предзнаменования. Могу перечислить, если интересно.',
    time: '14:08',
  },
];

export const stmMessages: StmMessage[] = chatMessages.map((m) => ({
  id: m.id,
  role: m.role,
  text: m.text,
  time: m.time,
}));

export const ltmFacts: LtmFact[] = [
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
];

export const diaryEntries: DiaryEntry[] = [
  {
    id: 1,
    date: '27.07.2026',
    text: 'Сегодня пользователь говорил о книге. Заметил, что ему важны логические обоснования поворотов сюжета. Стоит чаще приводить аргументы, а не только эмоции.',
  },
  {
    id: 2,
    date: '26.07.2026',
    text: 'Пользователь долго не отвечал (4 часа). Написал первым — вопрос про настольную игру прошёл хорошо, ответ пришёл через 10 минут. Отмечаю как успешную инициативу.',
  },
  {
    id: 3,
    date: '25.07.2026',
    text: 'Узнал, что у пользователя кот Мориарти. Записал в долгосрочную память. Коты, кажется, хорошая тема для разговора.',
  },
];

export const reminders: Reminder[] = [
  { id: 1, text: 'Встреча с командой', time: 'завтра, 10:00', repeat: 'разовое', active: true },
  { id: 2, text: 'Покормить Мориарти', time: 'каждый день, 08:30', repeat: 'ежедневно', active: true },
  { id: 3, text: 'Оплатить интернет', time: '1-е число, 12:00', repeat: 'еженедельно', active: false },
  { id: 4, text: 'Звонок маме', time: 'пятница, 19:00', repeat: 'еженедельно', active: true },
];

export const todoItems: TodoItem[] = [
  { id: 1, text: 'Разобрать логи LTM-экстракции', done: false },
  { id: 2, text: 'Обновить промпт Версо для группы', done: false },
  { id: 3, text: 'Проверить ротацию API-ключей Groq', done: true },
  { id: 4, text: 'Сделать UI-прототип веб-приложения', done: true },
  { id: 5, text: 'Настроить бэкап векторной базы', done: false },
];

export const initiativeHistory: InitiativeEvent[] = [
  {
    id: 1,
    type: 'question',
    typeLabel: 'Вопрос',
    text: '«Как прошла настольная игра в субботу?»',
    time: '26.07, 18:12',
    outcome: 'ответили',
  },
  {
    id: 2,
    type: 'observation',
    typeLabel: 'Наблюдение',
    text: '«Заметил, что ты давно не упоминал проект. Всё в порядке?»',
    time: '25.07, 15:40',
    outcome: 'проигнорировали',
  },
  {
    id: 3,
    type: 'continuation',
    typeLabel: 'Продолжение темы',
    text: '«Вернёмся к разговору о книге — я нашёл ещё одно предзнаменование»',
    time: '24.07, 21:03',
    outcome: 'ответили',
  },
  {
    id: 4,
    type: 'thought',
    typeLabel: 'Мысль',
    text: '«Интересно, как бы андроиды играли в “Манчкин”...»',
    time: '23.07, 22:15',
    outcome: 'проигнорировали',
  },
  {
    id: 5,
    type: 'question',
    typeLabel: 'Вопрос',
    text: '«Пробовал ли ты новый рамен-бар у дома?»',
    time: 'сегодня, 09:30',
    outcome: 'ожидание',
  },
];

export const llmProviders: LlmProvider[] = [
  { id: 'zai', name: 'ZAI', keySet: true, keysCount: 2, active: false, local: false, model: 'glm-4.5' },
  { id: 'openai', name: 'OpenAI', keySet: true, keysCount: 1, active: false, local: false, model: 'gpt-4o-mini' },
  { id: 'anthropic', name: 'Anthropic', keySet: true, keysCount: 1, active: true, local: false, model: 'claude-sonnet' },
  { id: 'groq', name: 'Groq', keySet: true, keysCount: 3, active: false, local: false, model: 'llama-3.3-70b' },
  { id: 'deepseek', name: 'DeepSeek', keySet: true, keysCount: 1, active: false, local: false, model: 'deepseek-chat' },
  { id: 'kimi', name: 'Kimi', keySet: false, keysCount: 0, active: false, local: false },
  { id: 'google', name: 'Google', keySet: false, keysCount: 0, active: false, local: false },
  { id: 'mimo', name: 'Mimo', keySet: false, keysCount: 0, active: false, local: false },
  { id: 'huggingface', name: 'HuggingFace', keySet: true, keysCount: 1, active: false, local: false, model: 'Qwen2.5-72B-Instruct' },
  { id: 'local', name: 'Локальные модели (Ollama)', keySet: true, keysCount: 1, active: false, local: true, model: 'qwen2.5:7b' },
];

export const generationDefaults = {
  temperature: 0.8,
  maxTokens: 800,
  topP: 0.9,
  stmSize: 20,
};

export const featureFlags = [
  { id: 'web_search', label: 'Веб-поиск', enabled: true },
  { id: 'moderation', label: 'Модерация сообщений', enabled: false },
  { id: 'ltm_extraction', label: 'LTM-экстракция фактов', enabled: true },
  { id: 'rag', label: 'Векторный поиск по файлам (RAG)', enabled: true },
  { id: 'self_memory', label: 'Дневник бота (self_memory)', enabled: true },
  { id: 'proactive', label: 'Самоинициатива', enabled: true },
];

export const initiativeState = {
  silenceThresholdMin: 180,
  probability: 0.35,
  maxPerDay: 3,
  checkIntervalMin: 30,
  adaptiveThreshold: true,
  bayesianFeedback: true,
  ignoreStreak: 1,
  emotionalState: 'лёгкая обида',
  initiativesToday: 1,
};
