import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useI18n, useMockData } from '../i18n';
import type { ChatMessage, DiaryEntry, InitiativeEvent, InventoryItem, LearningSession, LtmFact, PersonaFile, Reminder, TodoItem } from '../mockData';
import { api, streamChat, StreamInterruptedError } from '../api';
import type { ApiHistoryMessage, ClearPart, InitiativeData, PersonaConfig, ReminderEntry } from '../api';
import { refetchPersonaLlm, useApiOnline, useApiPersonaLlm, useApiProviders, usePersonaLivingState } from '../apiData';
import { alertDialog, confirmDialog } from '../dialogStore';
import {
  getServerLastTs, latestActivePersona, markRead, pollInboxNow, pruneInbox, setControlMode, setFastPoll, setGenerating, touchActivity, useInbox,
} from '../inboxStore';
import { usePresenceReporting } from '../presence';
import { notifyBotMessage } from '../notifications';
import { consumeChatPersonaRequest, useChatOverviewRequest, useChatPersonaRequest } from '../chatNavStore';
import { captureRects, playFlip } from '../flip';
import type { FlipRects } from '../flip';
import ChatOverview from './ChatOverview';
import { formatSilence, INIT_TYPE_MAP } from '../initiativeTypes';
import { agoLabel } from '../timeAgo';
import { usePersonaAvatars } from '../avatarStore';
import PersonaDossier from '../components/PersonaDossier';
import PersonaYamlModal from '../components/PersonaYamlModal';
import MessageText from '../components/MessageText';
import VoiceChat from '../components/VoiceChat';
import Icon from '../components/icons';
import type { IconName } from '../components/icons';
import SkinFrame from '../components/SkinFrame';
import { usePersonaSkin } from '../skins/skinStore';
import { usePersonaOverlay } from '../skins/overlayStore';
import { buildChatPayload } from '../skins/payloads';
import { useShellTheme } from '../skins/shellTheme';
import { useSkinEnv } from '../skins/useSkinEnv';
import { parseReminderWhen } from '../reminderWhen';
import { fmtRecurrence, formFromRepeatText, recurrenceFromForm } from '../reminderRepeat';
import { useRoomView } from '../room/useRoomView';

// Допуск сравнения серверных меток: last_ts и timestamp реплики STM — один и
// тот же float (memory.add_message), но после рестарта буфер читается из БД
const TS_EPS = 0.001;
// Сколько после НАШЕГО завершённого стрима рост серверного last_ts считается
// «своим» (реплики уже в локальной ленте): поллер может показать его и через
// тик после конца стрима
const ABSORB_MS = 30000;
// Пауза перед повторной загрузкой истории, если первый запрос не прошёл
const HISTORY_RETRY_MS = 3000;
// Самая свежая серверная метка в истории STM (локальные пузыри сюда не идут:
// их ts — часы браузера, с серверными не сравнимы)
const newestTs = (list: ChatMessage[] | undefined) =>
  (list ?? []).reduce((mx, m) => Math.max(mx, m.ts ?? 0), 0);

// Стабильный id реплики STM (см. toChatMessage): серверная метка в мкс
// (~1.7e15 — не пересекается с локальными id вида Date.now() ~1.7e12) или
// FNV-1a хеш автора+текста (< 2^31); повторы базы в той же выдаче
// получают +1 за каждое более раннее совпадение
const stmBaseId = (m: ApiHistoryMessage): number => {
  if (m.timestamp) return Math.round(m.timestamp * 1e6);
  const s = m.role + '\n' + m.content;
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193);
  return (h >>> 1) + 1;
};
const stmIdCache = new WeakMap<ApiHistoryMessage[], number[]>();
const stmMessageId = (all: ApiHistoryMessage[], i: number): number => {
  let ids = stmIdCache.get(all);
  if (!ids) {
    const used = new Set<number>();
    ids = all.map((m) => {
      let id = stmBaseId(m);
      while (used.has(id)) id += 1;
      used.add(id);
      return id;
    });
    stmIdCache.set(all, ids);
  }
  return ids[i];
};

// Фичи, хранящиеся в YAML словарём с параметрами (enabled + доп. поля,
// см. Settings.tsx): сохранение фичи не должно затирать эти поля
const DICT_FEATURES = new Set(['proactive', 'learning', 'rhythm', 'life']);

// Включена ли фича по конфигу персоны (та же логика, что в Settings.tsx):
// нет ключа — выключена, dict — по полю enabled (computer_control без
// enabled в dict — включён по умолчанию)
function featureEnabledFromConfig(features: Record<string, unknown> | undefined, id: string): boolean {
  const v = features?.[id];
  if (typeof v === 'boolean') return v;
  if (v !== null && typeof v === 'object') {
    return id === 'computer_control'
      ? (v as { enabled?: unknown }).enabled !== false
      : Boolean((v as { enabled?: unknown }).enabled);
  }
  return false;
}

// Раздел «Чат»: сначала страница всех чатов, клик по карточке открывает
// чат персоны (карточки уезжают в список слева). Назад — кнопка «Все чаты»
// над списком или повторный клик по «Чат» в сайдбаре (так же и в скине).
// Переход из других секций (карточки главной, уведомление) открывает чат
// сразу, минуя страницу всех чатов
export default function Chat() {
  const { personas } = useMockData();
  const request = useChatPersonaRequest();
  const [openId, setOpenId] = useState<string | null>(() =>
    request && personas.some((p) => p.id === request) ? request : null,
  );
  // Прямоугольники карточек/строк на момент смены вида — для FLIP-перехода
  const [flipFrom, setFlipFrom] = useState<FlipRects | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const open = (id: string) => {
    setFlipFrom(captureRects(rootRef.current));
    setOpenId(id);
  };
  const back = () => {
    setFlipFrom(captureRects(rootRef.current));
    setOpenId(null);
  };

  // Запрос «открыть чат с персоной», пока открыта страница всех чатов
  // (в открытом чате его подхватывает сам ChatRoom)
  useEffect(() => {
    if (!request || openId !== null || !personas.some((p) => p.id === request)) return;
    setFlipFrom(null);
    setOpenId(request);
    consumeChatPersonaRequest();
  }, [request, openId, personas]);

  const overviewRequest = useChatOverviewRequest();
  const seenOverviewRequest = useRef(overviewRequest);
  useEffect(() => {
    if (overviewRequest === seenOverviewRequest.current) return;
    seenOverviewRequest.current = overviewRequest;
    if (openId !== null) back();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overviewRequest]);

  return (
    <div className="chat-root" ref={rootRef}>
      {openId === null ? (
        <ChatOverview onOpen={open} flipFrom={flipFrom} />
      ) : (
        <ChatRoom initialPersonaId={openId} flipFrom={flipFrom} onBack={back} />
      )}
    </div>
  );
}

interface ChatRoomProps {
  initialPersonaId: string;
  flipFrom: FlipRects | null; // карточки страницы всех чатов — старт перехода
  onBack: () => void;
}

function ChatRoom({ initialPersonaId, flipFrom, onBack }: ChatRoomProps) {
  const { lang, t } = useI18n();
  const {
    personas,
    chatByPersona,
    initiativeByPersona,
    initiativeStateByPersona,
    remindersByPersona,
    todosByPersona,
    inventoryByPersona,
    learningByPersona,
    ltmByPersona,
    diaryByPersona,
    filesByPersona,
    llmProviders,
    featureFlags,
    generationDefaults,
  } = useMockData();

  // Чат открывается с персоной, выбранной на странице всех чатов (или
  // запрошенной из другой секции)
  const [selectedId, setSelectedId] = useState(initialPersonaId);
  // Выбор оператора (клик, переход из других секций)
  const pickPersona = (id: string) => {
    setSkinBypass(false);
    setSelectedId(id);
  };
  const avatars = usePersonaAvatars();
  const [panelOpen, setPanelOpen] = useState(true);
  // Левый список персон тоже задвигается (как правая контекстная панель)
  const [personaListOpen, setPersonaListOpen] = useState(true);
  // Переход со страницы всех чатов: строки списка едут с мест карточек.
  // Пока едут, список не обрезает их (overflow) и лежит поверх окна чата
  const personaListRef = useRef<HTMLDivElement>(null);
  const [arriving, setArriving] = useState(flipFrom !== null);
  // Один раз на монтирование: StrictMode повторяет эффект, и второй замер
  // (элемент уже сдвинут первой анимацией) дал бы нулевой переход поверх
  const flipPlayed = useRef(false);
  useLayoutEffect(() => {
    if (!flipFrom || flipPlayed.current) return;
    flipPlayed.current = true;
    void playFlip(personaListRef.current, flipFrom).then(() => setArriving(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [dossierOpen, setDossierOpen] = useState(false);
  // Просмотр/редактирование YAML текущей персоны (кнопка в шапке чата)
  const [yamlOpen, setYamlOpen] = useState(false);
  // Режим чата: классический (переписка) или голосовой (аватарка персоны)
  const [chatMode, setChatMode] = useState<'classic' | 'voice'>('classic');
  // «Обычный вид» поверх назначенного скина (до возврата кнопкой «Вид скина»):
  // в стандартном интерфейсе есть всё, чего скин может не рисовать
  const [skinBypass, setSkinBypass] = useState(false);
  const [draft, setDraft] = useState('');
  // Ответ на конкретное сообщение (id цитируемого)
  const [replyToId, setReplyToId] = useState<number | null>(null);
  // Прикреплённая картинка к черновику (dataURL) и скрытый file input
  const [pendingImage, setPendingImage] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Отправленные оператором реплики — локально, по id персоны (не в mockData)
  const [sentByPersona, setSentByPersona] = useState<Record<string, ChatMessage[]>>({});
  // Бэкенд: доступен ли API и история из STM по id персоны
  const apiOnline = useApiOnline();
  const apiProviders = useApiProviders();
  const [historyByPersona, setHistoryByPersona] = useState<Record<string, ChatMessage[]>>({});
  // Первая загрузка истории упала (ядро ещё поднимает бота персоны или
  // перезапускается) — лента показывает повтор, эпоха перезапускает фетч
  const [historyFailed, setHistoryFailed] = useState<Record<string, boolean>>({});
  const [historyRetry, setHistoryRetry] = useState(0);
  // LTM-факты персоны с бэкенда (для скин-досье); raw — исходные строки
  // «Категория: факт» по тому же индексу, нужны для правки/забывания через API.
  // Данные сайдбара/фактов/конфига помечены id персоны: после переключения
  // персоны до прихода её данных чужие не показываются и не адресуются
  const [apiFactsState, setApiFacts] = useState<{ persona: string; facts: LtmFact[] } | null>(null);
  const apiFactsRawRef = useRef<{ persona: string; raw: string[] }>({ persona: '', raw: [] });
  // Конфиг персоны с бэкенда (фичи + параметры генерации) — источник для
  // скина вместо мока, когда бэкенд онлайн; без merge оверлея поверх сервера
  const [apiPersonaConfigState, setApiPersonaConfig] = useState<{ persona: string; config: PersonaConfig } | null>(null);
  // Ждём ответ бэкенда (индикатор «печатает», блокировка повторной отправки) —
  // по id персоны: генерация идёт у одной, остальные чаты «печатает» не показывают
  const [waitingByPersona, setWaitingByPersona] = useState<Record<string, boolean>>({});
  // id пузыря, в который сейчас стримится ответ (ему markdown достраивается
  // на лету) — по id персоны
  const [streamMsgIdByPersona, setStreamMsgIdByPersona] = useState<Record<string, number | null>>({});
  // Провайдер, реально ответивший последним (по id персоны): «провайдер · модель»
  const [answerer, setAnswerer] = useState<Record<string, string>>({});
  // Счётчик завершённых обменов: досье по нему перечитывает STM
  // (оно может быть открыто, пока бот ещё отвечает)
  const [stmEpoch, setStmEpoch] = useState(0);
  // Счётчик обновления правого сайдбара (дела/инвентарь/напоминания/факты):
  // дёргается после очистки диалога — LTM стёрты, списки могли измениться
  const [sideEpoch, setSideEpoch] = useState(0);
  // Данные сайдбара с бэкенда: дела, инвентарь, напоминания, курсы, инициатива
  const [sideDataState, setSideData] = useState<{
    persona: string;
    todos: TodoItem[];
    inventory: InventoryItem[];
    reminders: Reminder[];
    // Напоминания как их отдал бэкенд (id, trigger_at) — по index
    // совпадают с reminders[].id: адресация правки/отмены по стабильному id
    remindersRaw: ReminderEntry[];
    courses: LearningSession[];
    init: InitiativeData | null;
  } | null>(null);

  const persona = personas.find((p) => p.id === selectedId) ?? personas[0];
  const sideData = sideDataState?.persona === persona.id ? sideDataState : null;
  const apiFacts = apiFactsState?.persona === persona.id ? apiFactsState.facts : null;
  const apiPersonaConfig = apiPersonaConfigState?.persona === persona.id ? apiPersonaConfigState.config : null;

  // Подпись провайдера для шапки: кто ответил последним, иначе закреплённый
  // за персоной основной (а не глобальный активный), иначе глобальный
  const providerLabel = (pid: string | null | undefined, model?: string | null) => {
    if (!pid) return null;
    // webchat:<сайт> → SITE · веб-чат (в списке API-провайдеров его нет)
    const name = apiProviders?.find((p) => p.id === pid)?.name
      ?? (pid.startsWith('webchat:') ? `${pid.split(':')[1].toUpperCase()} · ${t('settings.webchatBadge')}` : pid.toUpperCase());
    return model ? `${name} · ${model}` : name;
  };
  const activeApi = apiProviders?.find((p) => p.active);
  const personaLlm = useApiPersonaLlm(persona.id);
  const configuredPid = personaLlm?.primary ?? activeApi?.id ?? null;
  const configuredModel = configuredPid
    ? personaLlm?.models?.[configuredPid] ?? apiProviders?.find((p) => p.id === configuredPid)?.model
    : null;
  const headerModel = apiOnline
    ? answerer[persona.id] ?? providerLabel(configuredPid, configuredModel) ?? persona.model
    : persona.model;

  // Смена основного провайдера/модели в досье делает прошлого «ответчика»
  // неактуальным — сбрасываем: шапка покажет закреплённого провайдера до
  // первого нового ответа (потом снова будет фактический ответчик)
  const llmKey = `${personaLlm?.primary ?? ''}|${JSON.stringify(personaLlm?.models ?? {})}`;
  const prevLlmKey = useRef<Record<string, string>>({});
  useEffect(() => {
    const prev = prevLlmKey.current[persona.id];
    prevLlmKey.current[persona.id] = llmKey;
    if (prev !== undefined && prev !== llmKey) {
      setAnswerer((prevMap) => {
        if (!(persona.id in prevMap)) return prevMap;
        const next = { ...prevMap };
        delete next[persona.id];
        return next;
      });
    }
  }, [llmKey, persona.id]);

  // Список персон подменился (пришёл API) — выбор сбрасываем на существующую
  useEffect(() => {
    if (!personas.some((p) => p.id === selectedId)) {
      setSelectedId(latestActivePersona(personas.map((p) => p.id)) ?? personas[0].id);
    }
  }, [personas, selectedId]);

  // Запрос «открыть чат с персоной» из других секций (карточки STATUS на Home)
  const chatPersonaRequest = useChatPersonaRequest();
  useEffect(() => {
    if (!chatPersonaRequest) return;
    if (personas.some((p) => p.id === chatPersonaRequest)) pickPersona(chatPersonaRequest);
    consumeChatPersonaRequest();
  }, [chatPersonaRequest, personas]);

  // Реплика STM бэкенда → сообщение чата. id стабилен между перечитками
  // (не позиция: буфер STM — deque с лимитом, позиции сдвигаются): серверная
  // метка в мкс, без метки — хеш автора и текста; совпадения в одной выдаче
  // разводятся +1 по порядку
  const toChatMessage = (m: ApiHistoryMessage, i: number, all: ApiHistoryMessage[]): ChatMessage => ({
    id: stmMessageId(all, i),
    role: m.role === 'user' ? 'user' : 'bot',
    text: m.content,
    time: m.timestamp
      ? new Date(m.timestamp * 1000).toLocaleTimeString(lang === 'ru' ? 'ru-RU' : 'en-US', { hour: '2-digit', minute: '2-digit' })
      : '',
    ts: m.timestamp ?? undefined,
  });

  // Догрузка поздних ответов по серверному last_ts (см. checkFreshness ниже).
  // absorbed — last_ts, отнесённые к нашему завершённому обмену (ответ уже в
  // локальной ленте; перечитка заменила бы пузыри STM-копиями — мерцание)
  const absorbed = useRef<Record<string, number>>({});
  const absorbUntil = useRef<Record<string, number>>({});
  // last_ts, под который историю уже перечитывали: если STM его так и не
  // содержит (реплику удалили в досье, очистка), повторно не дёргаем
  const reloadedFor = useRef<Record<string, number>>({});
  // Оборванный стрим: id его пузырей и серверная метка на момент отправки —
  // пузыри снимаются, когда ответ появится в перечитанной истории
  const recovering = useRef<Record<string, { ids: number[]; since: number }>>({});

  // Стрим обрывался, а ответ сервер дописал в STM — пузыри с пометкой
  // обрыва больше не нужны (в истории есть реплика бота свежее отправки)
  const dropRecovered = (id: string, msgs: ApiHistoryMessage[]) => {
    const rec = recovering.current[id];
    if (!rec || !msgs.some((m) => m.role !== 'user' && (m.timestamp ?? 0) > rec.since + TS_EPS)) return;
    delete recovering.current[id];
    const drop = new Set(rec.ids);
    setSentByPersona((prev) => ({ ...prev, [id]: (prev[id] ?? []).filter((m) => !drop.has(m.id)) }));
  };

  // Перечитать историю после правок STM в досье (удаление реплик).
  // STM — источник правды. dropLocal (явные правки в досье) — сбросить и
  // локальные копии сессии; иначе (фоновая догенерация) локальные оставляем:
  // их дубли с STM отсекаются при сборке ленты (по тексту+ts), а ещё не
  // попавшие в STM остаются видимыми — лента не моргает и не ждёт
  // перерисовки всей истории. Из inbox-стора вычищаем то, что попало в STM.
  // Промис — удалась ли перечитка (checkFreshness снимает гард при сбое)
  const reloadHistory = (id: string, dropLocal = false): Promise<boolean> =>
    api
      .getHistory(id)
      .then((msgs) => {
        setHistoryByPersona((prev) => ({ ...prev, [id]: msgs.map(toChatMessage) }));
        pruneInbox(id, new Set(msgs.map((m) => m.content)));
        dropRecovered(id, msgs);
        if (dropLocal) {
          setSentByPersona((prev) => ({
            ...prev,
            [id]: (prev[id] ?? []).filter((m) => m.image || (m.images && m.images.length > 0)),
          }));
        }
        return true;
      })
      .catch(() => false);

  // Фоновые сообщения (напоминания, инициативы) прилетают в глобальный
  // inbox-стор (поллер в App); здесь только гасим непрочитанные открытой персоны
  const { messages: inboxMessages, unread, generating, lastTs, serverLastTs, controlMode } = useInbox();
  useEffect(() => {
    markRead(persona.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona.id, inboxMessages]);

  // Присутствие: пока открыт чат ЭТОЙ персоны и вкладка в фокусе, её фоновая
  // работа по этому чату молчит. Отчёт живёт здесь, а не в App: ключ отметки —
  // (персона, чат), и только экран чата знает, чей чат открыт. При смене
  // персоны/уходе с экрана хук снимает отметку у старого ключа (presence.ts).
  usePresenceReporting(apiOnline, persona.id);

  // Персона генерирует ответ: локально (ждём/стримим ответ на наше сообщение)
  // ИЛИ на сервере (флаг из inbox-поллера — виден и после перезагрузки
  // страницы, пока запрос продолжается на бэкенде)
  const remoteGenerating = apiOnline && generating[persona.id] === true;
  const waiting = waitingByPersona[persona.id] === true;
  const streamMsgId = streamMsgIdByPersona[persona.id] ?? null;
  const typing = waiting || remoteGenerating;
  // Локальный стрим этой персоны ещё идёт — догрузку истории откладываем
  const inFlight = waiting || streamMsgId != null;

  // Живое «печатает»: если ответ дольше ~5с, статус прерывается на «онлайн»
  // и возвращается — как у собеседника, который остановился и продолжил.
  // Прерывание только в хедере (пузыря typing в ленте нет).
  const [typingBreak, setTypingBreak] = useState(false);
  useEffect(() => {
    if (!typing) {
      setTypingBreak(false);
      return;
    }
    let alive = true;
    let t1 = 0;
    let t2 = 0;
    const cycle = () => {
      t1 = window.setTimeout(() => {
        if (!alive) return;
        setTypingBreak(true);
        t2 = window.setTimeout(() => {
          if (!alive) return;
          setTypingBreak(false);
          cycle();
        }, 2000);
      }, 5000);
    };
    cycle();
    return () => {
      alive = false;
      window.clearTimeout(t1);
      window.clearTimeout(t2);
    };
  }, [typing]);

  // Фокус вернулся в окно — всё от бота прочитано
  useEffect(() => {
    const onFocus = () => markBotRead(persona.id);
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona.id]);

  // Серверная генерация завершилась (флаг true→false): ответ уже в STM,
  // а локальной копии после перезагрузки страницы нет — перечитываем историю.
  // Своя генерация (отправленная из этого окна) перечитку пропускает: ответ
  // уже в локальной ленте, а замена его STM-копией с новым ключом давала бы
  // видимое мерцание последних сообщений
  const prevGenerating = useRef(false);
  const localExchange = useRef<Record<string, boolean>>({});
  useEffect(() => {
    const was = prevGenerating.current;
    prevGenerating.current = remoteGenerating;
    if (was && !remoteGenerating) {
      if (localExchange.current[persona.id]) {
        localExchange.current[persona.id] = false;
      } else if (Date.now() < (absorbUntil.current[persona.id] ?? 0)) {
        // Только что завершился наш обмен, а поллер (внеплановый опрос в
        // конце стрима) успел ещё раз увидеть generating=true — ответ уже в
        // ленте, перечитка дала бы мерцание
      } else {
        reloadHistory(persona.id);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remoteGenerating, persona.id]);

  // ── Догрузка поздних ответов по серверному last_ts ──
  // Сервер штампует last_ts каждой репликой STM (и user, и assistant) тем же
  // float, что timestamp реплики в /api/chat/history. История читается раз на
  // персону за сессию — если ответ лёг в STM ПОСЛЕ её чтения (перезагрузка
  // страницы посреди генерации), флаг generating мог и не поймать это.
  // Правило: last_ts открытой персоны новее самой свежей реплики истории
  // (и не «наш» — см. absorbed) → перечитываем историю. Флага generating
  // для этого не нужно. Refs (absorbed/absorbUntil/reloadedFor) — выше,
  // у reloadHistory.

  // Окно «своего» обмена: рост last_ts в течение ABSORB_MS после нашего
  // стрима — наши же реплики. По всем персонам (пользователь мог уйти в
  // другой чат, пока тикал поллер); эффект объявлен ДО проверки свежести —
  // в одном коммите отрабатывает первым
  useEffect(() => {
    const now = Date.now();
    for (const [id, until] of Object.entries(absorbUntil.current)) {
      if (now >= until) {
        delete absorbUntil.current[id];
        continue;
      }
      absorbed.current[id] = Math.max(absorbed.current[id] ?? 0, serverLastTs[id] ?? 0);
    }
  }, [serverLastTs]);

  const checkFreshness = (id: string) => {
    if (!apiOnline) return;
    const hist = historyByPersona[id];
    if (!hist) return; // первичная загрузка ещё не пришла — она и так свежая
    if (waitingByPersona[id] === true || (streamMsgIdByPersona[id] ?? null) != null) return;
    const srv = serverLastTs[id] ?? 0;
    const known = Math.max(newestTs(hist), absorbed.current[id] ?? 0);
    if (srv <= known + TS_EPS) return;
    if (reloadedFor.current[id] === srv) return; // уже перечитывали под эту метку
    reloadedFor.current[id] = srv;
    reloadHistory(id).then((ok) => {
      // Сеть моргнула — метку не «сжигаем»: следующая сверка (тик поллера,
      // возврат во вкладку) повторит перечитку
      if (!ok && reloadedFor.current[id] === srv) delete reloadedFor.current[id];
    });
  };
  const srvTs = serverLastTs[persona.id] ?? 0;
  const histForPersona = historyByPersona[persona.id];
  useEffect(() => {
    checkFreshness(persona.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id, srvTs, histForPersona, inFlight]);

  // Возврат во вкладку: таймеры фоновой вкладки браузер душит, last_ts мог
  // устареть — опрашиваем inbox открытой персоны сразу (эффект выше сверит
  // метку) и сверяемся с тем, что уже известно
  const checkFreshnessRef = useRef(checkFreshness);
  useEffect(() => {
    checkFreshnessRef.current = checkFreshness;
  });
  useEffect(() => {
    if (!apiOnline) return;
    const id = persona.id;
    const onBack = () => {
      if (document.visibilityState !== 'visible') return;
      pollInboxNow(id);
      checkFreshnessRef.current(id);
    };
    window.addEventListener('focus', onBack);
    document.addEventListener('visibilitychange', onBack);
    return () => {
      window.removeEventListener('focus', onBack);
      document.removeEventListener('visibilitychange', onBack);
    };
  }, [apiOnline, persona.id]);

  // Сервер генерирует ответ открытой персоны, а локального стрима нет
  // (перезагрузка страницы, обрыв) — inbox этой персоны опрашивается раз в 3 с,
  // чтобы поздний ответ появился за секунды, а не через 15-секундный тик
  // Своя задача в режиме управления: промежуточные «Нажал …» идут через
  // inbox, пока наш запрос ещё в пути, — тоже быстрый опрос, иначе они
  // приходили пачкой раз в 15 с
  const ccOn = controlMode[persona.id] === true;
  useEffect(() => {
    setFastPoll((remoteGenerating && !inFlight) || (inFlight && ccOn) ? persona.id : null);
  }, [remoteGenerating, inFlight, ccOn, persona.id]);
  useEffect(() => () => setFastPoll(null), []);

  // Список персон по свежести переписки: последняя активная — наверху
  // (персоны без метки свежести остаются в исходном порядке — сортировка
  // стабильная, нули у всех равны)
  const sortedPersonas = [...personas].sort((a, b) => (lastTs[b.id] ?? 0) - (lastTs[a.id] ?? 0));

  // Подтягиваем историю персоны из бэкенда (один раз на персону за сессию).
  // Первый запрос к персоне поднимает её бота на сервере — это небыстро,
  // лента пока показывает загрузку; при сбое повторяем через паузу
  useEffect(() => {
    if (!apiOnline || historyByPersona[persona.id]) return;
    const id = persona.id;
    let stale = false;
    let retryTimer: number | undefined;
    api
      .getHistory(id)
      .then((msgs) => {
        if (stale) return;
        setHistoryByPersona((prev) => ({ ...prev, [id]: msgs.map(toChatMessage) }));
        setHistoryFailed((prev) => ({ ...prev, [id]: false }));
        // Инициатива/напоминание пишется и в STM, и в inbox — вычищаем дубли из стора
        pruneInbox(id, new Set(msgs.map((m) => m.content)));
        dropRecovered(id, msgs);
      })
      .catch(() => {
        if (stale) return;
        setHistoryFailed((prev) => ({ ...prev, [id]: true }));
        retryTimer = window.setTimeout(() => setHistoryRetry((n) => n + 1), HISTORY_RETRY_MS);
      });
    return () => {
      stale = true;
      window.clearTimeout(retryTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id, historyRetry]);

  // Сайдбар контекста: дела/инвентарь/напоминания/курсы с бэкенда.
  // Перечитываем после ответа бота (waiting) — он мог их изменить по ходу диалога.
  useEffect(() => {
    if (!apiOnline) {
      setSideData(null);
      return;
    }
    const id = persona.id;
    let stale = false;
    const fmtTs = (ts: number | null) =>
      ts
        ? new Date(ts * 1000).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
        : '—';
    Promise.allSettled([api.getTodo(id), api.getInventory(id), api.getReminders(id), api.getLearning(id), api.getInitiative(id)]).then(
      ([td, inv, rem, lr, ini]) => {
        if (stale) return;
        setSideData({
          persona: id,
          todos: td.status === 'fulfilled' ? td.value.items.map((x) => ({ id: x.index, text: x.task, done: false })) : [],
          inventory:
            inv.status === 'fulfilled'
              ? inv.value.items.map((x, i) => ({ id: i + 1, icon: 'gem' as IconName, name: x.name, description: x.description, tag: 'created' as const }))
              : [],
          reminders:
            rem.status === 'fulfilled'
              ? rem.value.items.map((r) => ({
                  id: r.index, text: r.task, time: fmtTs(r.trigger_at),
                  repeat: fmtRecurrence(r.recurrence, t), active: r.active !== false,
                }))
              : [],
          remindersRaw: rem.status === 'fulfilled' ? rem.value.items : [],
          courses:
            lr.status === 'fulfilled'
              ? lr.value.sessions.map((s, i) => ({
                  id: i + 1,
                  subject: s.subject,
                  status: 'active' as const,
                  lessonCount: s.lesson_count,
                  coveredTopics: s.covered_topics,
                  vocabulary: s.learned_vocabulary,
                  frequency: '',
                  nextLesson: fmtTs(s.next_lesson_at),
                  quizPending: s.quiz_pending ? 1 : 0,
                }))
              : [],
          // Параметры и статистика самоинициативы этой персоны (для блока в сайдбаре)
          init: ini.status === 'fulfilled' ? ini.value : null,
        });
      },
    );
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id, waiting, sideEpoch]);

  // LTM-факты для скин-досье (перечитываем после ответа бота — он мог их дополнить)
  useEffect(() => {
    if (!apiOnline) {
      setApiFacts(null);
      apiFactsRawRef.current = { persona: '', raw: [] };
      return;
    }
    const id = persona.id;
    let stale = false;
    api
      .getLtmFacts(id)
      .then((facts) => {
        if (stale) return;
        apiFactsRawRef.current = { persona: id, raw: facts };
        setApiFacts({
          persona: id,
          facts: facts.map((raw, i) => {
            const sep = raw.indexOf(':');
            return sep > 0
              ? { id: i + 1, category: raw.slice(0, sep).trim(), fact: raw.slice(sep + 1).trim() }
              : { id: i + 1, category: 'General', fact: raw };
          }),
        });
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id, waiting, sideEpoch]);

  // Конфиг персоны с бэкенда (фичи + генерация) — перечитываем вместе с
  // остальным сайдбаром (sideEpoch), чтобы правки через скин были видны сразу.
  // Не сбрасываем на каждой перечитке: сбой GET оставил бы конфиг пустым, а
  // переключение фичи по пустому затёрло бы её параметры (чужой персоны
  // конфиг и так не виден — он помечен её id)
  useEffect(() => {
    if (!apiOnline) {
      setApiPersonaConfig(null);
      return;
    }
    const id = persona.id;
    let stale = false;
    api
      .getPersonaConfig(id)
      .then((c) => !stale && setApiPersonaConfig({ persona: id, config: c }))
      .catch(() => {});
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id, sideEpoch]);
  // Скин персоны: отдельные файлы по экранам, рендер в sandboxed iframe
  const { skins, broken, reportBroken, reset } = usePersonaSkin(persona.id);
  // Записываемые через скин данные (добавленные/переключённые сущности)
  const overlay = usePersonaOverlay(persona.id, () => void alertDialog({ message: t('skin.errQuota') }));
  // История = (STM бэкенда | моковый диалог) + отправленные в этой сессии реплики
  // + фоновые сообщения из inbox-стора (напоминания, инициативы)
  const baseMessages = apiOnline
    ? (historyByPersona[persona.id] ?? [])
    : (chatByPersona[persona.id] ?? []);
  // История персоны ещё не пришла с бэкенда — в ленте индикатор загрузки
  const historyLoading = apiOnline && !historyByPersona[persona.id];
  // Дедупликация: инициатива/напоминание лежит и в STM (история), и в inbox —
  // показываем inbox-копию, только если её текста ещё нет в истории
  const baseTexts = new Set(baseMessages.map((m) => m.text));
  const inboxChat: ChatMessage[] = (inboxMessages[persona.id] ?? [])
    .filter((m) => !baseTexts.has(m.text))
    .map((m) => ({
      id: m.id,
      role: 'bot' as const,
      text: m.text,
      time: new Date(m.ts * 1000).toLocaleTimeString(lang === 'ru' ? 'ru-RU' : 'en-US', { hour: '2-digit', minute: '2-digit' }),
      ts: m.ts,
      fromInbox: true,
    }));
  // Локальные реплики сессии, уже попавшие в STM, отсекаем при сборке — по
  // тексту И ts (копия в истории свежее локальной): недеструктивно, лента не
  // моргает при фоновых перечитках, а повторные «да»/«нет» не съедаются
  // старым совпадением текста
  // (TS_EPS: метки STM, перечитанные из БД после рестарта, — с точностью до мс)
  const sentVisible = (sentByPersona[persona.id] ?? []).filter(
    (m) => !baseMessages.some((h) => h.text === m.text && (h.ts ?? 0) + TS_EPS >= (m.ts ?? 0)),
  );
  // Лента смешанная (история STM + локальные реплики сессии + inbox):
  // при простом склеивании инициатива из inbox встала бы ПОСЛЕ свежего
  // сообщения пользователя — сортируем по реальному времени (стабильная
  // сортировка: при равном/отсутствующем ts порядок источников сохраняется)
  const messages = [...baseMessages, ...sentVisible, ...inboxChat].sort(
    (a, b) => (a.ts ?? 0) - (b.ts ?? 0),
  );
  // ── Окно видимых сообщений и скролл ──
  // Полная история (STM до сотен реплик) разом не рендерится: показываем
  // хвост, ранние подгружаем кнопкой. При входе в чат — скролл в конец (к
  // свежим сообщениям), при подгрузке ранних позиция удерживается, новые
  // сообщения доскролливают, только если пользователь уже у низа.
  const listRef = useRef<HTMLDivElement>(null);
  const stickBottom = useRef(true);
  const prevHeight = useRef<number | null>(null);
  const [visibleCount, setVisibleCount] = useState(60);
  useEffect(() => {
    setVisibleCount(60);
    stickBottom.current = true;
  }, [persona.id]);
  const visibleMessages = messages.length > visibleCount ? messages.slice(-visibleCount) : messages;
  const hiddenCount = messages.length - visibleMessages.length;
  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el) return;
    if (prevHeight.current != null) {
      // Подгрузка ранних: удерживаем вьюпорт на тех же сообщениях
      el.scrollTop += el.scrollHeight - prevHeight.current;
      prevHeight.current = null;
    } else if (stickBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [persona.id, visibleMessages]);

  const init = initiativeStateByPersona[persona.id] ?? initiativeStateByPersona.connor;
  // Живые параметры инициативы этой персоны с бэкенда (иначе — мок-фолбэк,
  // он одинаковый для персон без мок-записи)
  const initApi = sideData?.init ?? null;
  const initProb = initApi?.initiative_probability ?? init.probability;
  const initToday = initApi?.initiatives_today ?? init.initiativesToday;
  const initMax = initApi?.max_daily_initiatives ?? init.maxPerDay;
  const initIgnoreStreak = initApi?.ignore_streak ?? init.ignoreStreak;
  // База для скин-payload'а инициативы: живые значения поверх моковых полей
  const initBase = initApi
    ? {
        silenceThresholdMin: initApi.silence_threshold_minutes,
        probability: initApi.initiative_probability,
        maxPerDay: initApi.max_daily_initiatives,
        checkIntervalMin: initApi.check_interval_minutes,
        adaptiveThreshold: initApi.adaptive_threshold,
        bayesianFeedback: initApi.feedback_enabled,
        initiativesToday: initApi.initiatives_today,
        ignoreStreak: initApi.ignore_streak,
      }
    : init;
  // Настроение: живое состояние персоны (слой state), иначе ступень обиды
  // по стрику игнора с бэкенда, иначе мок. Раньше здесь всегда был мок —
  // у всех персон без мок-записи «лёгкая обида» Коннора
  const livingData = usePersonaLivingState(persona.id);
  const liveMood = apiOnline ? livingData?.state?.mood ?? null : null;
  const streakStages = t('init.stages').split('|');
  const mood = liveMood?.tag
    ?? (initApi
      ? streakStages[initIgnoreStreak < 3 ? 0 : initIgnoreStreak < 5 ? 1 : initIgnoreStreak < 7 ? 2 : initIgnoreStreak < 10 ? 3 : 4]
      : init.emotionalState);
  // Тренд: направление последнего сдвига живого mood; без него — по стрику
  // игнорирования инициатив (растёт — хуже, сброшен — лучше)
  const trend = liveMood
    ? liveMood.trend === 'up'
      ? t('chat.trendImproving')
      : liveMood.trend === 'down'
        ? t('chat.trendWorsening')
        : t('chat.trendStable')
    : initIgnoreStreak > 2
      ? t('chat.trendWorsening')
      : initIgnoreStreak === 0
        ? t('chat.trendImproving')
        : t('chat.trendStable');
  // «Последний ответ · когда напишет сама»: время последней реплики
  // пользователя из истории и порог молчания с бэкенда (адаптивный —
  // две медианы интервала между репликами). Нет данных — прочерк
  const lastUserTs = [...messages].reverse().find((m) => m.role === 'user' && m.ts)?.ts ?? null;
  const lastReplyText = apiOnline && initApi
    ? t('chat.lastReplyLine', {
        t: agoLabel(lastUserTs, new Date(), t, lang === 'ru' ? 'ru-RU' : 'en-US'),
        f: !initApi.enabled
          ? t('chat.selfOff')
          : t('chat.selfAfter', {
              n: formatSilence(initApi.effective_silence_minutes ?? initApi.silence_threshold_minutes, t, lang),
            }),
      })
    : t('chat.lastReplyUnknown');
  // Текущее занятие персоны: только живое состояние из кеша комнаты (если
  // она уже загружалась — свой поллинг не запускаем); нет его — прочерк,
  // демо-занятия не подставляем
  const roomCache = useRoomView(persona.id, { enabled: false });
  const roomLiving = roomCache.view?.living;
  const roomState = roomLiving?.enabled && roomLiving.ui_sync ? roomLiving.state : null;
  const pastime: { label: string; place: string } | null = roomState?.pastime
    ? {
        label: roomState.pastime,
        place: roomCache.view?.config?.spots.find((s) => s.key === roomState.spot)?.place || roomState.location || '',
      }
    : null;
  // Ближайшее активное напоминание персоны
  const nextReminder = (sideData ? sideData.reminders : (remindersByPersona[persona.id] ?? [])).find((r) => r.active);
  // Последняя самоинициатива персоны: живая история с бэкенда, иначе мок
  // (записи хронологичны — берём последнюю; длинные тексты режем под ctx-note)
  const lastInitApi = initApi?.history?.length ? initApi.history[initApi.history.length - 1] : null;
  const lastInit = initApi
    ? lastInitApi
      ? {
          time: lastInitApi.date,
          text: lastInitApi.message.length > 140 ? `${lastInitApi.message.slice(0, 140)}…` : lastInitApi.message,
        }
      : null
    : (initiativeByPersona[persona.id] ?? []).at(-1);
  // Дневник и файлы для скин-досье с бэкенда (раньше — моки даже онлайн).
  // Только при назначенном скине: дефолтное досье грузит их само
  const [skinSideState, setSkinSide] = useState<{ persona: string; diary: DiaryEntry[]; files: PersonaFile[] } | null>(null);
  const skinAssigned = !!(skins.chat || skins.dossier);
  useEffect(() => {
    if (!apiOnline || !skinAssigned) {
      setSkinSide(null);
      return;
    }
    const id = persona.id;
    let stale = false;
    const loc = lang === 'ru' ? 'ru-RU' : 'en-US';
    const fmt = (d: Date) =>
      Number.isNaN(d.getTime()) ? '' : d.toLocaleString(loc, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
    Promise.allSettled([api.getDiary(id), api.getFiles(id)]).then(([dr, fr]) => {
      if (stale) return;
      let diary: DiaryEntry[] = [];
      if (dr.status === 'fulfilled') {
        const d = dr.value;
        // Как в разделе «Память»: эпизоды и заметки одной лентой, свежие сверху
        const entries = [
          ...d.episodes.map((e) => ({ date: fmt(new Date(e.timestamp)) || e.timestamp, text: e.text, ts: e.timestamp })),
          ...d.notes.map((n) => ({ date: fmt(new Date(n.timestamp)) || n.timestamp, text: n.text, ts: n.timestamp })),
        ].sort((a, b) => (a.ts < b.ts ? 1 : -1));
        if (d.life_summary) entries.unshift({ date: 'Σ', text: d.life_summary, ts: '' });
        diary = entries.map((e, i) => ({ id: i + 1, date: e.date, text: e.text }));
      }
      const files: PersonaFile[] = fr.status === 'fulfilled'
        ? fr.value.files.map((f, i) => ({
            id: i + 1,
            name: f.filename,
            kind: /\.(png|jpe?g|gif|webp)$/i.test(f.filename) ? 'image' as const : 'document' as const,
            size: f.size >= 1000 ? `${Math.round(f.size / 100) / 10}k` : String(f.size),
            date: fmt(new Date(f.timestamp)),
            description: '',
            content: '', // содержимое — по запросу (download-file → getFileContent)
          }))
        : [];
      setSkinSide({ persona: id, diary, files });
    });
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, skinAssigned, persona.id, waiting, sideEpoch]);
  const skinSide = skinSideState?.persona === persona.id ? skinSideState : null;
  const skinFiles: PersonaFile[] = apiOnline ? (skinSide?.files ?? []) : (filesByPersona[persona.id] ?? []);
  // Самоинициативы онлайн — история бэкенда (как в разделе «Самоинициатива»)
  const skinInitiatives: InitiativeEvent[] = apiOnline
    ? (initApi?.history ?? []).map((h, i) => ({
        id: i + 1,
        type: INIT_TYPE_MAP[h.type] ?? 'thought',
        typeLabel: h.type,
        text: h.message,
        time: h.date,
        outcome: 'pending' as const,
      }))
    : (initiativeByPersona[persona.id] ?? []);

  // Краткие списки для раскрывающихся блоков панели
  const todos = (sideData ? sideData.todos : (todosByPersona[persona.id] ?? [])).slice(0, 4);
  const inventory = (sideData ? sideData.inventory : (inventoryByPersona[persona.id] ?? [])).slice(0, 4);
  const activeCourse = (sideData ? sideData.courses : (learningByPersona[persona.id] ?? [])).find((s) => s.status === 'active');
  const hasLearning = persona.features.includes('learning');
  // Строка «Обучение» для сайдбара скина (пустая — курса нет)
  const learningLine =
    hasLearning && activeCourse
      ? [
          t('chat.lessonLine', { subject: activeCourse.subject, n: activeCourse.lessonCount }),
          activeCourse.quizPending > 0 ? t('chat.quizWaiting', { n: activeCourse.quizPending }) : '',
          activeCourse.nextLesson,
        ]
          .filter(Boolean)
          .join(' · ')
      : '';

  // «Очистить диалог»: сбрасывает локальные реплики сессии и на бэкенде
  // полностью — STM диалога, LTM-факты пользователя и дневник персоны.
  // После ответа бэкенда перечитываем правый сайдбар (sideEpoch):
  // LTM-факты стёрты, списки дел/напоминаний могли измениться
  // parts — стереть только эти части (кнопки опасной зоны досье); без них —
  // всё сразу. Лента чата очищается, только если стёрта переписка
  const clearStm = (parts?: ClearPart[]): Promise<void> => {
    const stm = !parts || parts.includes('stm');
    if (stm) setSentByPersona((prev) => ({ ...prev, [persona.id]: [] }));
    if (!apiOnline) return Promise.resolve();
    if (stm) setHistoryByPersona((prev) => ({ ...prev, [persona.id]: [] }));
    return api
      .clearChat(persona.id, parts)
      .then(() => undefined)
      .catch(() => {})
      .finally(() => setSideEpoch((e) => e + 1));
  };

  // Добавление реплики оператора в историю (общий путь для дефолтного UI и скина)
  // status — метка доставки: sent → read
  const pushMessage = (text: string, image: string | null, status?: ChatMessage['status']) => {
    const msg: ChatMessage = {
      id: Date.now(),
      role: 'user',
      text,
      time: new Date().toLocaleTimeString(lang === 'ru' ? 'ru-RU' : 'en-US', { hour: '2-digit', minute: '2-digit' }),
      ts: Date.now() / 1000,
      ...(replyToId != null ? { replyTo: replyToId } : {}),
      ...(image ? { image } : {}),
      ...(status ? { status } : {}),
    };
    setSentByPersona((prev) => ({ ...prev, [persona.id]: [...(prev[persona.id] ?? []), msg] }));
    setReplyToId(null);
    // Своё сообщение — мгновенный подъём персоны в списке, не дожидаясь поллера
    touchActivity(persona.id, Date.now() / 1000);
  };

  // Метки доставки наших реплик: sent → read (бот прочитал = ответил)
  const setUserMsgsStatus = (pid: string, from: NonNullable<ChatMessage['status']>[],
                             to: NonNullable<ChatMessage['status']>) => {
    setSentByPersona((prev) => ({
      ...prev,
      [pid]: (prev[pid] ?? []).map((m) =>
        m.role === 'user' && m.status && from.includes(m.status) ? { ...m, status: to } : m),
    }));
  };

  // Сообщения бота прочитаны: чат в фокусе / пользователь ответил
  const markBotRead = (pid: string) => {
    setSentByPersona((prev) => ({
      ...prev,
      [pid]: (prev[pid] ?? []).map((m) => (m.role === 'bot' && !m.read ? { ...m, read: true } : m)),
    }));
  };

  // Отправка сразу, без дебаунс-очереди: каждое сообщение — отдельный обмен,
  // серии реплик не склеиваются.

  const send = () => {
    const text = draft.trim();
    if (!text && !pendingImage) return;
    const image = pendingImage;
    setPendingImage(null);
    setDraft('');
    submitMessage(text, image);
  };

  // Реплика пользователя — общий путь поля ввода, скина и голосового режима:
  // пузырь + отправка на бэкенд со стримом ответа
  const submitMessage = (text: string, image: string | null) => {
    if (!text && !image) return;
    if (!apiOnline) {
      // Офлайн-режим (моки): картинка остаётся локальным пузырём
      pushMessage(text, image);
      return;
    }
    // Цитата уходит на бэкенд как reply_context (как ответ на сообщение в мессенджерах);
    // картинка — base64 в поле image, в пузыре остаётся локальная копия.
    // pid фиксируем здесь: за время генерации пользователь может уйти
    // в чат другой персоны — флаги waiting/stream гасим именно отправленной
    const pid = persona.id;
    const personaName = persona.name; // имя фиксируем здесь: за время генерации selectedId может смениться
    const quoted = replyToId != null ? messages.find((m) => m.id === replyToId) : undefined;
    markBotRead(pid); // пользователь ответил — реплики бота прочитаны

    pushMessage(text, image, 'sent');
    startGeneration(pid, personaName, text, quoted?.text, image);
  };

  // Полная очистка диалога из скина — с тем же подтверждением, что в досье:
  // кнопка скина (или его скрипт) не стирает всё одним нажатием
  const confirmClearFromSkin = async () => {
    const ok = await confirmDialog({
      title: t('dossier.clearDialog'),
      message: t('dossier.confirmClear', { name: persona.name }),
      confirmLabel: t('dossier.yesClear'),
      danger: true,
    });
    if (ok) clearStm();
  };

  // Отправка сообщения и стриминг ответа
  const startGeneration = (pid: string, personaName: string, text: string,
                           replyContext?: string, image?: string | null) => {
    localExchange.current[pid] = true; // своя генерация — перечитку истории пропустим
    // Новый обмен закрывает окно поглощения прошлого: его метки теперь
    // прикрывает «in flight», а после конца стрима окно откроется заново
    delete absorbUntil.current[pid];
    // Серверная метка до отправки: реплика бота новее неё — ответ на это сообщение
    const sentSince = Math.max(newestTs(historyByPersona[pid]), getServerLastTs(pid));
    let interrupted = false;
    setUserMsgsStatus(pid, ['queued'], 'sent'); // ушло на бэкенд
    setWaitingByPersona((prev) => ({ ...prev, [pid]: true }));
    // Пузырь ответа растёт по мере стриминга; бэкенд шлёт порции уже
    // финального текста (после _clean_response/маркеров), поэтому в финале
    // upsertBot() просто подтверждает тот же текст — без замены содержимого.
    // При включённом расщеплении (settings.split_messages) бэкенд шлёт
    // part_break между частями ответа — каждая часть печатается в свой пузырь
    const botId = Date.now() + 1;
    setStreamMsgIdByPersona((prev) => ({ ...prev, [pid]: botId }));
    const bubbleIds: number[] = [botId];
    const bubbleTexts: string[] = [''];
    // Метка ответа в STM (reply_ts сервера): пузырь встаёт в ленте после
    // промежуточных сообщений хода из inbox («Нажал …» режима управления),
    // а не сразу под вопросом (момент отправки). Нет метки (старый сервер,
    // обрыв) — по-старому
    let replyTs: number | undefined;
    // Не раньше самого вопроса (часы браузера и сервера могут расходиться);
    // части расщеплённого ответа — сразу за первой, по порядку
    const bubbleTs = (msgId: number) => {
      const i = bubbleIds.indexOf(msgId);
      return replyTs && i >= 0 ? Math.max(replyTs, botId / 1000) + i * 1e-4 : msgId / 1000;
    };
    const upsertBot = (msgId: number, msgText: string) => {
      const ts = bubbleTs(msgId);
      const time = new Date(replyTs ? ts * 1000 : Date.now())
        .toLocaleTimeString(lang === 'ru' ? 'ru-RU' : 'en-US', { hour: '2-digit', minute: '2-digit' });
      setSentByPersona((prev) => {
        const list = prev[pid] ?? [];
        if (list.some((m) => m.id === msgId)) {
          return { ...prev, [pid]: list.map((m) => (m.id === msgId ? { ...m, text: msgText, ts } : m)) };
        }
        return { ...prev, [pid]: [...list, { id: msgId, role: 'bot' as const, text: msgText, time, ts }] };
      });
    };
    // Картинки ответа (скриншоты страницы) — в тот же пузырь, что и текст:
    // одно сообщение, а не отдельный пузырь на каждый кадр
    const attachBotImages = (msgId: number, imgs: string[]) => {
      setSentByPersona((prev) => ({
        ...prev,
        [pid]: (prev[pid] ?? []).map((m) =>
          m.id === msgId ? { ...m, images: [...(m.images ?? []), ...imgs] } : m),
      }));
    };
    streamChat(
      { persona: pid, message: text, replyContext, image: image ?? undefined },
      (tok) => {
        bubbleTexts[bubbleTexts.length - 1] += tok;
        upsertBot(bubbleIds[bubbleIds.length - 1], bubbleTexts[bubbleTexts.length - 1]);
      },
      () => {
        // Граница частей ответа: следующий текст — новое сообщение
        const nextId = Date.now() + bubbleIds.length + 1;
        bubbleIds.push(nextId);
        bubbleTexts.push('');
        setStreamMsgIdByPersona((prev) => ({ ...prev, [pid]: nextId }));
      },
      (ts) => {
        replyTs = ts;
      },
    )
      .then((res) => {
        if (typeof res.reply_ts === 'number') replyTs = res.reply_ts;
        // Обмен целиком у нас: рост last_ts от этих реплик — «свой», историю
        // под него не перечитываем (иначе пузыри заменятся STM-копиями).
        // Реплики уже в STM (сервер пишет их до отправки токенов) — опрашиваем
        // inbox сразу, чтобы метка попала в окно поглощения
        delete recovering.current[pid];
        absorbed.current[pid] = Math.max(absorbed.current[pid] ?? 0, getServerLastTs(pid));
        absorbUntil.current[pid] = Date.now() + ABSORB_MS;
        pollInboxNow(pid);
        upsertBot(bubbleIds[0], res.reply);
        // Ответ пришёл — нашу серию реплик бот «прочитал»
        setUserMsgsStatus(pid, ['queued', 'sent'], 'read');
        // Ответ бота виден (чат в фокусе) — сразу метим прочитанным
        if (document.hasFocus()) markBotRead(pid);
        // Части, стримившиеся своими пузырями, подтверждаем финальным текстом;
        // остальное (несстримленный хвост, досылаемые списки) — новые пузыри
        res.extra_messages.forEach((m, i) => {
          if (i < bubbleIds.length - 1) upsertBot(bubbleIds[i + 1], m);
          // Досылаемые списки — за ответом по той же метке (часы браузера
          // и сервера могут расходиться)
          else pushPersonaMessage(m, replyTs ? bubbleTs(bubbleIds[0]) + (i + 1) * 1e-4 : undefined);
        });
        // Скриншоты страницы из режима управления («что на странице?») —
        // одним сообщением вместе с ответом (кадры в том же пузыре, dataURL).
        // В STM не пишутся — как и картинки пользователя, живут до перечитки
        // истории
        if (res.images?.length) attachBotImages(bubbleIds[0], res.images);
        const label = providerLabel(res.provider, res.model);
        if (label) setAnswerer((prev) => ({ ...prev, [pid]: label }));
        // Режим управления мог переключиться самим этим сообщением —
        // обновляем сразу, не дожидаясь поллера inbox
        if (typeof res.control_mode === 'boolean') setControlMode(pid, res.control_mode);
        // Ответ догенерировался, а вкладка уже не в фокусе — уведомляем (как в мессенджерах)
        notifyBotMessage(pid, personaName, res.reply);
      })
      .catch((e) => {
        if (e instanceof StreamInterruptedError) {
          // Обрыв стрима — не ошибка генерации: сервер, скорее всего, допишет
          // ответ в STM. Обмен больше не «свой» (edge generating true→false
          // должен перечитать историю), пузыри помечены к замене — их
          // снимет reloadHistory, как только ответ появится в истории.
          // Перечитку запускают last_ts/generating (эффекты выше), как
          // только снимется «in flight» в finally
          interrupted = true;
          localExchange.current[pid] = false;
          recovering.current[pid] = { ids: [...bubbleIds], since: sentSince };
          upsertBot(bubbleIds[bubbleIds.length - 1],
            `${bubbleTexts[bubbleTexts.length - 1]}${bubbleTexts[bubbleTexts.length - 1] ? '\n\n' : ''}⚠ ${t('chat.streamInterrupted')}`);
          return;
        }
        // Настоящая ошибка сервера (event.error, HTTP-статус) — текст оставляем
        upsertBot(bubbleIds[0], `⚠ ${e instanceof Error ? e.message : String(e)}`);
      })
      .finally(() => {
        setWaitingByPersona((prev) => ({ ...prev, [pid]: false }));
        setStreamMsgIdByPersona((prev) => ({ ...prev, [pid]: null }));
        if (interrupted) {
          // Флаг «печатает» не гасим вслепую: генерация на сервере, возможно,
          // ещё идёт — берём свежие generating/last_ts с бэкенда сразу
          pollInboxNow(pid);
        } else {
          setGenerating(pid, false); // «печатает» гасим сразу, не дожидаясь поллера
        }
        setStmEpoch((e) => e + 1);
      });
  };

  // Реплика персоны в историю (досылаемые части ответа — extra_messages)
  const pushPersonaMessage = (text: string, ts?: number) => {
    const msg: ChatMessage = {
      id: Date.now(),
      role: 'bot',
      text,
      time: new Date(ts ? ts * 1000 : Date.now()).toLocaleTimeString(lang === 'ru' ? 'ru-RU' : 'en-US', { hour: '2-digit', minute: '2-digit' }),
      ts: ts ?? Date.now() / 1000,
    };
    setSentByPersona((prev) => ({ ...prev, [persona.id]: [...(prev[persona.id] ?? []), msg] }));
    touchActivity(persona.id, Date.now() / 1000);
  };

  // Данные досье для скина. В API-режиме — сервер является истиной целиком:
  // правки через скин уходят в бэкенд (handleSkinAction/handleSkinSetting) и
  // возвращаются через sideEpoch/apiFacts — overlay здесь больше не подмешиваем.
  // Офлайн (моки) — как раньше, overlay поверх мокового списка.
  const skinTodos = apiOnline
    ? (sideData?.todos ?? [])
    : [
        ...(todosByPersona[persona.id] ?? [])
          .filter((td) => !overlay.deletedTodos.includes(td.id))
          .map((td) => ({
            ...(overlay.editedTodos[td.id] ? { ...td, text: overlay.editedTodos[td.id] } : td),
            done: overlay.toggledTodos.includes(td.id) ? !td.done : td.done,
          })),
        ...overlay.addedTodos,
      ];
  const skinReminders = apiOnline
    ? (sideData?.reminders ?? [])
    : [
        ...(remindersByPersona[persona.id] ?? [])
          .filter((r) => !overlay.deletedReminders.includes(r.id))
          .map((r) => ({
            ...(overlay.editedReminders[r.id] ? { ...r, ...overlay.editedReminders[r.id] } : r),
            active: overlay.toggledReminders.includes(r.id) ? !r.active : r.active,
          })),
        ...overlay.addedReminders,
      ];
  const skinFacts = apiOnline
    ? (apiFacts ?? [])
    : [
        ...(ltmByPersona[persona.id] ?? [])
          .filter((f) => !overlay.deletedFacts.includes(f.id))
          .map((f) => (overlay.editedFacts[f.id] ? { ...f, ...overlay.editedFacts[f.id] } : f)),
        ...overlay.addedFacts,
      ];
  // STM-буфер досье: онлайн — история уже отражает удаление/срезку (после
  // trim-stm/delete-stm чат перечитывает её с бэкенда), офлайн — через overlay
  const skinStm = apiOnline
    ? messages
    : (() => {
        const stmFiltered = messages.filter((m) => !overlay.deletedStm.includes(m.id));
        return overlay.stmTrimmed > 0 ? stmFiltered.slice(0, -overlay.stmTrimmed) : stmFiltered;
      })();
  // Учебные курсы: реальные с бэкенда / моковые + добавленные через скин (офлайн)
  const skinCourses = apiOnline
    ? (sideData?.courses ?? [])
    : [
        ...(learningByPersona[persona.id] ?? []),
        ...overlay.addedCourses.map((c) => ({
          ...c,
          status: 'active' as const,
          lessonCount: 0,
          coveredTopics: [] as string[],
          vocabulary: [] as string[],
          nextLesson: '—',
          quizPending: 0,
        })),
      ];

  // Действия записи из скина (whitelist — ничего другого скин командовать не может).
  // Показать ошибку записи: тот же модальный диалог, что и у остального
  // приложения (dialogStore/DialogHost), а не тихое молчание
  const reportSkinError = (e: unknown) => {
    void alertDialog({ message: e instanceof Error ? e.message : String(e) });
  };
  // Онлайн — списки сайдбара перечитываем с бэкенда после успешной записи
  const bumpSide = () => setSideEpoch((e) => e + 1);
  // Ошибка с текстом для человека (показывается через reportSkinError)
  const skinFail = (key: string, vars?: Record<string, string | number>) => new Error(t(key, vars));

  // У todo-модуля нет id — только номер строки, и он сдвигается от любой
  // записи. Пока идёт своя запись, новые действия со списком игнорируем
  // (двойной клик не удалит два разных пункта), а номер берём из свежего
  // списка по тексту пункта, который видел человек
  const todoBusy = useRef(false);
  const runTodoWrite = (op: () => Promise<unknown>) => {
    if (todoBusy.current) return;
    todoBusy.current = true;
    op()
      .then(bumpSide)
      .catch((e) => {
        bumpSide();
        reportSkinError(e);
      })
      .finally(() => {
        todoBusy.current = false;
      });
  };
  const resolveTodoIndex = async (pid: string, shownId: number): Promise<number> => {
    const shown = sideData?.todos.find((td) => td.id === shownId);
    if (!shown) throw skinFail('skin.actStale');
    const { items } = await api.getTodo(pid);
    const hit =
      items.find((x) => x.index === shownId && x.task === shown.text) ?? items.find((x) => x.task === shown.text);
    if (!hit) throw skinFail('skin.actStale');
    return hit.index;
  };

  // Напоминание из показанного списка → его запись бэкенда (id, trigger_at)
  const shownReminder = (shownId: number) => sideData?.remindersRaw.find((r) => r.index === shownId) ?? null;
  // «01.10, 14:30» → дата + время — так же, как их раскладывает payload скина
  // (buildChatPayload) по полям формы правки
  const splitWhen = (time: string) => {
    const idx = time.lastIndexOf(', ');
    return idx > 0 ? { date: time.slice(0, idx), clock: time.slice(idx + 2) } : { date: '', clock: time };
  };
  // Момент срабатывания из полей формы; не разобрать — ошибка для человека
  const reminderAt = (date: string, clock: string): Date => {
    const when = parseReminderWhen(date, clock, lang === 'en' ? 'en' : 'ru');
    if (when.ok) return when.at;
    if (when.error === 'date') throw skinFail('skin.actBadDate', { v: date });
    if (when.error === 'time') throw skinFail('skin.actBadTime', { v: clock });
    throw skinFail('skin.actPast');
  };

  // Исходная строка LTM-факта по id из показанного списка (только этой персоны)
  const factRaw = (shownId: number): string | undefined =>
    apiFactsRawRef.current.persona === persona.id ? apiFactsRawRef.current.raw[shownId - 1] : undefined;

  const handleSkinAction = (action: string, values: Record<string, string>, id: string | null) => {
    const numId = id != null ? Number(id) : NaN;
    const hasId = !Number.isNaN(numId);
    switch (action) {
      case 'add-todo': {
        // с id — правка существующего дела (кнопка edit-todo заполнила форму)
        const text = values.text?.trim();
        if (!text) break;
        if (apiOnline) {
          // У todo-модуля нет PUT: правка = добавить новый пункт + удалить
          // старый (добавление идёт в конец и номер старого не сдвигает; сбой
          // посередине оставит дубль, а не потеряет пункт)
          const pid = persona.id;
          runTodoWrite(async () => {
            if (!hasId) return api.addTodo(pid, text);
            const idx = await resolveTodoIndex(pid, numId);
            await api.addTodo(pid, text);
            return api.removeTodo(pid, idx);
          });
        } else if (hasId) overlay.updateTodo(numId, text);
        else overlay.addTodo(text);
        break;
      }
      // В ядре «сделано» = пункт удаляется из списка (см. Tasks.tsx)
      case 'toggle-todo':
      case 'delete-todo':
        if (!hasId) break;
        if (apiOnline) {
          const pid = persona.id;
          runTodoWrite(async () => api.removeTodo(pid, await resolveTodoIndex(pid, numId)));
        } else if (action === 'toggle-todo') overlay.toggleTodo(numId);
        else overlay.deleteTodo(numId);
        break;
      case 'add-reminder': {
        const text = values.text?.trim();
        if (!text) break;
        if (apiOnline) {
          // Срок из даты+времени формы (reminderWhen: локальные даты, «завтра»,
          // формат списка). Повтор — из текстового поля формы скина: подпись
          // варианта («по будням») или дни («пн, ср, пт»); поля нет — при
          // создании разовое, при правке повтор прежний; не распознан —
          // ошибка, а не молча разовое
          const dateIn = values.date?.trim();
          const clockIn = (values.clock ?? values.time)?.trim();
          const repeatForm = values.repeat != null ? formFromRepeatText(values.repeat) : null;
          const pid = persona.id;
          const req = (async () => {
            // Время повтора сервер берёт из срока (пояс пользователя, не браузера)
            const recurrence = () => {
              if (repeatForm === 'unknown') {
                throw skinFail('skin.repeatUnknown', {
                  v: values.repeat?.trim() ?? '',
                  options: t('tasks.repeatOptions').split('|').slice(0, 4).join(', '),
                  days: t('tasks.weekdays').split('|').filter((_, i) => i % 2 === 0).slice(0, 3).join(', '),
                });
              }
              if (!repeatForm) return undefined;
              const rec = recurrenceFromForm(repeatForm.kind, repeatForm.days);
              if (rec === 'no-days') throw skinFail('tasks.pickDays');
              return rec;
            };
            if (!hasId) {
              const at = reminderAt(dateIn ?? '', clockIn ?? '');
              const delay = Math.max(10, Math.round((at.getTime() - Date.now()) / 1000));
              return api.addReminder(pid, text, delay, recurrence() ?? null);
            }
            // Правка — на месте, по id (атомарно, повтор и id сохраняются).
            // Дата и время не тронуты (или поля нет в форме скина) — срок
            // прежний: точный, а не пересчитанный из округлённой строки списка
            const raw = shownReminder(numId);
            const shown = sideData?.reminders.find((r) => r.id === numId);
            if (!raw || !shown) throw skinFail('skin.actStale');
            const orig = splitWhen(shown.time);
            const date = dateIn ?? orig.date.trim();
            const clock = clockIn ?? orig.clock.trim();
            const patch: { task: string; trigger_at?: number; recurrence?: ReturnType<typeof recurrence> } = { task: text };
            if (date !== orig.date.trim() || clock !== orig.clock.trim()) {
              patch.trigger_at = reminderAt(date, clock).getTime() / 1000;
            }
            const rec = recurrence();
            if (rec !== undefined) patch.recurrence = rec;
            return api.updateReminder(pid, raw.id, patch);
          })();
          req.then(bumpSide).catch((e) => {
            bumpSide();
            reportSkinError(e);
          });
        } else {
          const clock = (values.clock ?? values.time)?.trim() || '—';
          const when = values.date?.trim() ? `${values.date.trim()}, ${clock}` : clock;
          const repeat = values.repeat?.trim() || '—';
          if (hasId) overlay.updateReminder(numId, { time: when, text, repeat });
          else overlay.addReminder(when, text, repeat);
        }
        break;
      }
      case 'toggle-reminder': {
        if (!hasId) break;
        if (apiOnline) {
          // Пауза/продолжение по стабильному id (PUT active); продолженное
          // бэкенд сам ставит на следующее время по расписанию
          const raw = shownReminder(numId);
          if (!raw) {
            bumpSide();
            reportSkinError(skinFail('skin.actStale'));
            break;
          }
          api.updateReminder(persona.id, raw.id, { active: raw.active === false }).then(bumpSide).catch((e) => {
            bumpSide();
            reportSkinError(e);
          });
        } else overlay.toggleReminder(numId);
        break;
      }
      case 'delete-reminder': {
        if (!hasId) break;
        if (apiOnline) {
          // По стабильному id: номер строки мог сдвинуться (одно сработало)
          const raw = shownReminder(numId);
          if (!raw) {
            bumpSide();
            reportSkinError(skinFail('skin.actStale'));
            break;
          }
          api.cancelReminderById(persona.id, raw.id).then(bumpSide).catch((e) => {
            bumpSide();
            reportSkinError(e);
          });
        } else overlay.deleteReminder(numId);
        break;
      }
      case 'add-fact': {
        const fact = values.fact?.trim();
        if (!fact) break;
        if (apiOnline) {
          const category = values.category?.trim();
          const raw = category ? `${category}: ${fact}` : fact;
          if (hasId) {
            const oldRaw = factRaw(numId);
            if (oldRaw) api.updateFact(persona.id, oldRaw, raw).then(bumpSide).catch(reportSkinError);
          } else {
            api.addFact(persona.id, raw).then(bumpSide).catch(reportSkinError);
          }
        } else if (hasId) overlay.updateFact(numId, values.category?.trim() || '—', fact);
        else overlay.addFact(values.category?.trim() || '—', fact);
        break;
      }
      case 'delete-fact': {
        if (!hasId) break;
        if (apiOnline) {
          const raw = factRaw(numId);
          if (raw) api.forgetFact(persona.id, raw).then(bumpSide).catch(reportSkinError);
        } else overlay.deleteFact(numId);
        break;
      }
      case 'trim-stm': {
        const n = Number(values.count);
        if (Number.isNaN(n) || n <= 0) break;
        const count = Math.floor(n);
        if (apiOnline) {
          api.trimStm(persona.id, count)
            .then(() => {
              reloadHistory(persona.id, true);
              bumpSide();
            })
            .catch(reportSkinError);
        } else overlay.trimStm(count);
        break;
      }
      case 'delete-stm':
        if (!hasId) break;
        if (apiOnline) {
          // id — стабильный id реплики загруженной STM-истории (см.
          // toChatMessage); реплики сессии/inbox, ещё не попавшие в STM, идут
          // с другими id и молча пропускаются. Буфер STM с тех пор мог
          // сдвинуться (deque с лимитом, новые реплики) — сервер ищет реплику
          // по тексту и метке, позиция в истории — лишь подсказка
          const msg = baseMessages.find((m) => m.id === numId && !m.fromInbox);
          if (msg) {
            const pid = persona.id;
            api.deleteStmMessage(pid, baseMessages.indexOf(msg), { content: msg.text, timestamp: msg.ts ?? null })
              .then(() => {
                reloadHistory(pid, true);
                bumpSide();
              })
              .catch((e) => {
                reloadHistory(pid, true);
                reportSkinError(e);
              });
          }
        } else overlay.deleteStm(numId);
        break;
      case 'add-course': {
        const subject = values.subject?.trim();
        if (!subject) break;
        if (apiOnline) {
          // Тот же список опций и секунд, что в LearningPanel (форма курса)
          const freqOptions = t('learn.freqOptions').split('|');
          const freqSeconds = [3600, 86400, 86400, 86400, 86400, 604800];
          const idx = freqOptions.indexOf(values.frequency?.trim() ?? '');
          api.startLearning(persona.id, subject, idx >= 0 ? freqSeconds[idx] : 86400)
            .then(bumpSide).catch(reportSkinError);
        } else overlay.addCourse(subject, values.frequency?.trim() || '—');
        break;
      }
      case 'set-model':
        if (!id) break;
        // Персональная модель этой персоны для провайдера (как в PersonaDossier)
        if (apiOnline) {
          api.updatePersonaConfig(persona.id, { llm: { models: { [id]: values.model ?? '' } } })
            .then(() => refetchPersonaLlm(persona.id))
            .catch(reportSkinError);
        } else overlay.setModel(id, values.model ?? '');
        break;
      case 'make-main':
        if (!id) break;
        if (apiOnline) {
          api.updatePersonaConfig(persona.id, { llm: { primary: id } })
            .then(() => refetchPersonaLlm(persona.id))
            .catch(reportSkinError);
        } else overlay.makeMainProvider(id);
        break;
      case 'toggle-backup':
        if (!id) break;
        if (apiOnline) {
          // «На подхвате» = состоит в fallback-цепочке персоны (Settings.tsx)
          const current = personaLlm?.fallback ?? [];
          const next = current.includes(id) ? current.filter((x) => x !== id) : [...current, id];
          api.updatePersonaConfig(persona.id, { llm: { fallback: next } })
            .then(() => refetchPersonaLlm(persona.id))
            .catch(reportSkinError);
        } else overlay.toggleBackup(id);
        break;
      case 'toggle-feature': {
        if (!id) break;
        if (apiOnline) {
          // Сервер заменяет значение фичи целиком — собираем его поверх
          // текущего конфига. Конфига нет (не загрузился) — сначала дочитываем:
          // иначе {enabled} затёр бы параметры dict-фичи, а «выкл» стало «вкл»
          const pid = persona.id;
          const fid = id;
          (async () => {
            let cfg = apiPersonaConfig;
            if (!cfg) {
              try {
                cfg = await api.getPersonaConfig(pid);
              } catch {
                throw skinFail('skin.actConfigMissing');
              }
            }
            const orig = cfg.features?.[fid];
            const enabled = featureEnabledFromConfig(cfg.features, fid);
            const payload: Record<string, unknown> =
              orig !== null && typeof orig === 'object'
                ? { [fid]: { ...(orig as Record<string, unknown>), enabled: !enabled } }
                : { [fid]: DICT_FEATURES.has(fid) ? { enabled: !enabled } : !enabled };
            return api.updatePersonaConfig(pid, { features: payload });
          })().then(bumpSide).catch(reportSkinError);
        } else overlay.toggleFeature(id);
        break;
      }
      case 'reply':
        if (hasId) setReplyToId(numId);
        break;
      case 'cancel-reply':
        setReplyToId(null);
        break;
      case 'download-file': {
        const file = skinFiles.find((f) => f.id === numId);
        if (!file) break;
        const save = (content: string) => {
          const url = URL.createObjectURL(new Blob([content], { type: 'text/plain' }));
          const a = document.createElement('a');
          a.href = url;
          a.download = file.name;
          a.click();
          URL.revokeObjectURL(url);
        };
        // Онлайн содержимое — с бэкенда (в списке его нет), офлайн — из мока
        if (apiOnline) api.getFileContent(persona.id, file.name).then((r) => save(r.content)).catch(reportSkinError);
        else save(file.content);
        break;
      }
    }
  };

  const handleSkinSetting = (key: string, value: string) => {
    // Чекбоксы инициативы приходят строкой 'true'/'false'
    if (key === 'iniAdaptive' || key === 'iniBayes') {
      const on = value === 'true';
      if (apiOnline) {
        api
          .updateInitiative(persona.id, key === 'iniAdaptive' ? { adaptive_threshold: on } : { feedback_enabled: on })
          .then(bumpSide)
          .catch(reportSkinError);
      } else overlay.setInit(key === 'iniAdaptive' ? 'adaptive' : 'bayes', on);
      return;
    }
    const n = Number(value);
    if (Number.isNaN(n)) return;
    if (key === 'temperature' || key === 'maxTokens' || key === 'topP' || key === 'stmSize') {
      if (apiOnline) {
        const patch =
          key === 'stmSize'
            ? { stm_size: n }
            : { settings: { [key === 'maxTokens' ? 'max_tokens' : key === 'topP' ? 'top_p' : 'temperature']: n } };
        api.updatePersonaConfig(persona.id, patch).then(bumpSide).catch(reportSkinError);
      } else overlay.setGen(key, n);
    } else if (key === 'iniSilence' || key === 'iniProbability' || key === 'iniMaxPerDay' || key === 'iniInterval') {
      if (apiOnline) {
        const patch =
          key === 'iniSilence' ? { silence_threshold_minutes: n }
          : key === 'iniProbability' ? { initiative_probability: n / 100 }
          : key === 'iniMaxPerDay' ? { max_daily_initiatives: n }
          : { check_interval_minutes: n };
        api.updateInitiative(persona.id, patch).then(bumpSide).catch(reportSkinError);
      } else {
        const map = { iniSilence: 'silence', iniProbability: 'probability', iniMaxPerDay: 'maxPerDay', iniInterval: 'interval' } as const;
        overlay.setInit(map[key], n);
      }
    }
  };

  // Окружение скина (тема, локаль, подписи UI, время суток, погода) —
  // общий хук для чата/комнаты (и предпросмотра в SkinPanel)
  const skinEnv = useSkinEnv({
    locale: lang,
    t,
    personaName: persona.name,
    apiOnline,
    // Без скина окружение не нужно: ни поллинга погоды, ни минутного тикера
    enabled: !!((skins.chat && !broken.chat) || (skins.dossier && !broken.dossier)),
  });

  // Снапшот данных для скина (уходит в iframe через postMessage):
  // переписка + левый сайдбар (персоны) + правый сайдбар (контекст) + досье
  const skinChatState = buildChatPayload({
    persona,
    statusText: t(`status.${persona.status}`),
    youLabel: t('chat.you'),
    typing,
    messages,
    mood,
    pastimeLabel: pastime?.label ?? '—',
    allPersonas: personas.map((p) => ({
      persona: p,
      statusText: t(`status.${p.status}`),
      avatar: avatars[p.id],
      unread: unread[p.id] ?? 0,
    })),
    avatar: avatars[persona.id],
    historyLoading,
    context: {
      pastimePlace: pastime?.place ?? '',
      lastInitiative: lastInit ? `${lastInit.time} — ${lastInit.text}` : undefined,
      trend,
      initiative: t('chat.probLine', {
        p: Math.round(initProb * 100),
        today: initToday,
        max: initMax,
      }),
      lastReply: lastReplyText,
      nextReminder: nextReminder
        ? `${nextReminder.time} — ${nextReminder.text}`
        : t('chat.noActiveReminders'),
      learning: learningLine,
      features: persona.features.map((f) => t(`fb.${f}`)),
    },
    reply: (() => {
      const q = replyToId != null ? messages.find((m) => m.id === replyToId) : undefined;
      return q ? { author: q.role === 'user' ? t('chat.you') : persona.name, text: q.text } : null;
    })(),
    todos: skinTodos,
    // Онлайн, пока данные не пришли, — пусто, а не моковые предметы
    inventory: sideData ? sideData.inventory : apiOnline ? [] : (inventoryByPersona[persona.id] ?? []),
    dossier: {
      facts: skinFacts,
      reminders: skinReminders,
      initiatives: skinInitiatives,
      diary: apiOnline ? (skinSide?.diary ?? []) : (diaryByPersona[persona.id] ?? []),
      stm: skinStm,
      courses: skinCourses,
    },
    courseStatusLabels: {
      active: t('learn.statusActive'),
      paused: t('learn.statusPaused'),
      finished: t('learn.statusFinished'),
    },
    quizLineFor: (n) => t('learn.quizAlert', { n }),
    // Самоинициатива: живые значения персоны (initBase) + правки через скин
    // (probability в overlay — в %)
    // Онлайн — только значения сервера (правки уходят в updateInitiative,
    // офлайн-оверлей к ним не относится)
    initState: apiOnline
      ? { ...init, ...initBase }
      : {
          ...init,
          ...initBase,
          silenceThresholdMin: overlay.init.silence ?? initBase.silenceThresholdMin,
          probability: (overlay.init.probability ?? Math.round(initBase.probability * 100)) / 100,
          maxPerDay: overlay.init.maxPerDay ?? initBase.maxPerDay,
          checkIntervalMin: overlay.init.interval ?? initBase.checkIntervalMin,
          adaptiveThreshold: overlay.init.adaptive ?? initBase.adaptiveThreshold,
          bayesianFeedback: overlay.init.bayes ?? initBase.bayesianFeedback,
        },
    initStages: t('init.stages').split('|'),
    initSilenceText: t('init.silenceProgress', {
      n: 99,
      max: apiOnline ? initBase.silenceThresholdMin : (overlay.init.silence ?? initBase.silenceThresholdMin),
    }),
    files: skinFiles,
    providers:
      apiOnline && apiProviders
        ? apiProviders.map((p) => ({
            id: p.id,
            name: p.name,
            keySet: p.key_set,
            keysCount: p.keys_count,
            active: p.active,
            backup: false,
            local: p.local,
            model: p.model,
          }))
        : llmProviders,
    // Онлайн — персональные модели/основной провайдер/fallback-цепочка этой
    // персоны с бэкенда (см. PersonaDossier/Settings.tsx), не overlay
    modelOverrides: apiOnline ? (personaLlm?.models ?? {}) : overlay.models,
    providerMain: apiOnline ? (personaLlm?.primary ?? null) : overlay.mainProvider,
    backupToggled: apiOnline ? (personaLlm?.fallback ?? []) : overlay.backupToggled,
    featureFlags: featureFlags.map((f) => ({
      ...f,
      enabled: apiOnline
        ? (apiPersonaConfig ? featureEnabledFromConfig(apiPersonaConfig.features, f.id) : f.enabled)
        : (overlay.toggledFeatures.includes(f.id) ? !f.enabled : f.enabled),
    })),
    genOverrides: apiOnline
      ? {
          temperature: apiPersonaConfig?.settings.temperature,
          maxTokens: apiPersonaConfig?.settings.max_tokens,
          topP: apiPersonaConfig?.settings.top_p,
          stmSize: apiPersonaConfig?.stm_size ?? undefined,
        }
      : overlay.gen,
    stmSizeDefault: generationDefaults.stmSize,
    keySetLabel: t('apikeys.keySet'),
    keyNotSetLabel: t('apikeys.keyNotSet'),
    env: skinEnv,
  });
  // Скин персоны — отдельные файлы по экранам; сломанный экран откатывается
  // на дефолт, остальные остаются кастомными
  const chatSkinFile = broken.chat ? null : (skins.chat ?? null);
  const dossierSkinFile = broken.dossier ? null : (skins.dossier ?? null);
  // «Обычный вид» и голосовой режим показывают стандартный интерфейс поверх
  // назначенного скина (в скине нет ни голоса, ни всех частей интерфейса)
  const chatSkin = skinBypass || chatMode === 'voice' ? null : chatSkinFile;
  const dossierSkin = skinBypass ? null : dossierSkinFile;
  const activeScreen: 'chat' | 'dossier' = dossierOpen ? 'dossier' : 'chat';
  const activeSkin = activeScreen === 'dossier' ? dossierSkin : chatSkin;
  // Пока у персоны есть скин, каркас приложения (сайдбар, топбар) красится
  // в его палитру — и на дефолтных экранах (досье без своего файла,
  // обычный вид), чтобы интерфейс не прыгал между палитрами
  useShellTheme(activeSkin ?? chatSkinFile ?? dossierSkinFile);

  // Активный скин заменяет соответствующий вид целиком; сверху — тонкая
  // панель приложения: то, чего в контракте скина нет (все чаты, голос,
  // YAML, обычный вид со всеми частями интерфейса)
  if (activeSkin) {
    return (
      <div className="chat-layout chat-layout--skin">
        <div className="skin-hostbar">
          <button type="button" className="btn btn--chip" onClick={onBack} title={t('chat.allChatsTitle')}>
            <span aria-hidden="true">←</span>
            {t('chat.allChats')}
          </button>
          <span className="skin-hostbar-spacer" />
          {activeScreen === 'chat' && (
            <button type="button" className="btn btn--chip" title={t('chat.modeVoiceTitle')} onClick={() => setChatMode('voice')}>
              <Icon name="voice" size={13} />
              {t('chat.modeVoice')}
            </button>
          )}
          {apiOnline && (
            <button type="button" className="btn btn--chip" title={t('chat.personaYaml')} onClick={() => setYamlOpen(true)}>
              YAML
            </button>
          )}
          <button type="button" className="btn btn--chip" title={t('chat.skinClassicTitle')} onClick={() => setSkinBypass(true)}>
            {t('chat.skinClassic')}
          </button>
        </div>
        {yamlOpen && apiOnline && (
          <PersonaYamlModal
            personaId={persona.id}
            onClose={() => setYamlOpen(false)}
            onRenamed={(newId) => pickPersona(newId)}
          />
        )}
        <SkinFrame
          className="skin-frame skin-frame--chat"
          skin={activeSkin}
          screen={activeScreen}
          state={skinChatState}
          onSend={(text, image) => submitMessage(text.trim(), image ?? null)}
          onClear={() => { void confirmClearFromSkin(); }}
          onSelectPersona={(id) => {
            pickPersona(id);
            setReplyToId(null);
            setPendingImage(null);
            setDossierOpen(false);
          }}
          onOpenDossier={() => setDossierOpen(true)}
          onCloseDossier={() => setDossierOpen(false)}
          onAction={handleSkinAction}
          onSetSetting={handleSkinSetting}
          onError={(m) => reportBroken(activeScreen, m)}
          title={t('skin.frameTitle')}
        />
      </div>
    );
  }

  // Скин чата есть, а своего досье нет — дефолтное досье вместо него
  if (chatSkin && dossierOpen) {
    return (
      <div className="chat-layout">
        <PersonaDossier persona={persona} onClose={() => setDossierOpen(false)} onClearDialog={clearStm} onStmChange={() => { reloadHistory(persona.id, true); setSideEpoch((e) => e + 1); }} stmEpoch={stmEpoch} />
      </div>
    );
  }

  return (
    <div className={`chat-layout${flipFrom ? ' chat-layout--arrive' : ''}`}>
      {/* Список персон: клик переключает историю диалога; задвигается кнопкой */}
      <div
        ref={personaListRef}
        className={`chat-persona-list ${personaListOpen ? '' : 'chat-persona-list--collapsed'}${arriving ? ' chat-persona-list--flying' : ''}`}
      >
        <button
          type="button"
          className="chat-context-toggle"
          onClick={() => setPersonaListOpen((v) => !v)}
          title={personaListOpen ? t('chat.collapsePanel') : t('chat.expandPanel')}
        >
          {personaListOpen ? '«' : '»'}
        </button>
        <div className="chat-persona-list-body">
          <button type="button" className="chat-persona-back" onClick={onBack} title={t('chat.allChatsTitle')}>
            <span aria-hidden="true">←</span>
            {t('chat.allChats')}
          </button>
          <div className="chat-persona-list-title">{t('chat.personaList')}</div>
          {sortedPersonas.map((p, i) => (
            <button
              key={p.id}
              data-flip-id={p.id}
              className={`chat-persona-item${flipFrom ? '' : ' stagger-item'} ${p.id === selectedId ? 'chat-persona-item--active' : ''}`}
              style={{ animationDelay: `${i * 40}ms` }}
              onClick={() => {
                pickPersona(p.id);
                setReplyToId(null);
                setPendingImage(null);
              }}
            >
              <div className="avatar">
                {avatars[p.id] ? <img src={avatars[p.id]} alt={p.name} /> : p.name.charAt(0)}
              </div>
              <div className="chat-persona-info">
                <div className="chat-persona-name">
                  {p.name}
                  {(unread[p.id] ?? 0) > 0 && (
                    <span className="unread-dot" title={t('chat.unread')}>
                      {unread[p.id]}
                    </span>
                  )}
                </div>
                <div className="chat-persona-status">{t(`status.${p.status}`)}</div>
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* YAML-редактор персоны — поверх чата */}
      {yamlOpen && apiOnline && (
        <PersonaYamlModal
          personaId={persona.id}
          onClose={() => setYamlOpen(false)}
          onRenamed={(newId) => pickPersona(newId)}
        />
      )}

      {/* Окно чата либо встроенное досье персоны (одно заменяет другое) */}
      {dossierOpen ? (
        <PersonaDossier persona={persona} onClose={() => setDossierOpen(false)} onClearDialog={clearStm} onStmChange={() => { reloadHistory(persona.id, true); setSideEpoch((e) => e + 1); }} stmEpoch={stmEpoch} />
      ) : chatMode === 'voice' ? (
        <VoiceChat
          persona={persona}
          messages={messages}
          avatar={avatars[persona.id]}
          typing={typing}
          onSend={(text) => submitMessage(text, null)}
          onSwitchToClassic={() => setChatMode('classic')}
        />
      ) : (
      <div className="chat-main">
        {/* Откат при сломанном скине чата: дефолтный вид + объяснение */}
        {broken.chat && (
          <div className="skin-error-banner">
            <span>{t('skin.brokenBanner', { msg: broken.chat })}</span>
            <button type="button" className="btn btn--ghost" onClick={reset}>
              {t('skin.reset')}
            </button>
          </div>
        )}
        <div className="chat-header">
          <div className="avatar avatar--large">
            {avatars[persona.id] ? <img src={avatars[persona.id]} alt={persona.name} /> : persona.name.charAt(0)}
          </div>
          <div>
            <div className="chat-header-name">{persona.name}</div>
            <div className="chat-header-status">
              {typing && !typingBreak
                ? (
                  <span>
                    {t('status.typing')}
                    <span className="typing-dots" aria-hidden="true"><span>.</span><span>.</span><span>.</span></span>
                  </span>
                )
                : `${t(`status.${persona.status}`)} · ${headerModel}`}
            </div>
          </div>
          <div className="chat-header-actions">
            <button className="btn btn--chip" title={t('chat.modeVoiceTitle')} onClick={() => setChatMode('voice')}>
              <Icon name="voice" size={13} />
              {t('chat.modeVoice')}
            </button>
            <button className="btn btn--chip" title={t('chat.dossierTitle')} onClick={() => setDossierOpen(true)}>
              <Icon name="dossier" size={13} />
              {t('chat.dossier')}
            </button>
            {apiOnline && (
              <button className="btn btn--chip" title={t('chat.personaYaml')} onClick={() => setYamlOpen(true)}>
                YAML
              </button>
            )}
            {skinBypass && chatSkinFile && (
              <button className="btn btn--chip" title={t('chat.skinBackTitle')} onClick={() => setSkinBypass(false)}>
                {t('chat.skinBack')}
              </button>
            )}
          </div>
        </div>

        <div
          className="chat-messages"
          ref={listRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stickBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
          }}
        >
          {historyLoading && (
            <div
              className={`chat-loading${messages.length === 0 ? ' chat-loading--center' : ''}`}
              role="status"
              aria-live="polite"
            >
              <div className="chat-loading-caption">
                <span className="chat-loading-tag">[ .. ]</span>
                {t('chat.historyLoading', { name: persona.name })}
              </div>
              <div className="chat-loading-cells" aria-hidden="true">
                {Array.from({ length: 12 }, (_, i) => (
                  <i key={i} style={{ animationDelay: `${i * 90}ms` }} />
                ))}
              </div>
              <div className="chat-loading-hint">
                {historyFailed[persona.id] ? t('chat.historyRetrying') : t('chat.historyLoadingHint')}
              </div>
              <div className="chat-loading-skeleton" aria-hidden="true">
                <span className="chat-loading-bubble chat-loading-bubble--bot" />
                <span className="chat-loading-bubble chat-loading-bubble--user" />
                <span className="chat-loading-bubble chat-loading-bubble--bot chat-loading-bubble--short" />
              </div>
            </div>
          )}
          {hiddenCount > 0 && (
            <button
              type="button"
              className="btn btn--ghost chat-load-earlier"
              onClick={() => {
                const el = listRef.current;
                if (el) prevHeight.current = el.scrollHeight;
                setVisibleCount((c) => c + 80);
              }}
            >
              {t('chat.loadEarlier', { n: hiddenCount })}
            </button>
          )}
          {visibleMessages.map((m, i) => {
            // Цитируемое сообщение (если это ответ)
            const quoted = m.replyTo != null ? messages.find((q) => q.id === m.replyTo) : undefined;
            return (
              <div
                key={m.id}
                className={`message message--${m.role}`}
                style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}
              >
                <div className="message-bubble">
                  <button
                    type="button"
                    className="message-reply-btn"
                    title={t('chat.replyTitle')}
                    onClick={() => setReplyToId(m.id)}
                  >
                    {t('chat.reply')}
                  </button>
                  {quoted && (
                    <div className="message-quote">
                      <span className="message-quote-author">{quoted.role === 'user' ? t('chat.you') : persona.name}</span>
                      <span className="message-quote-text">{quoted.text}</span>
                    </div>
                  )}
                  <div className="message-text">
                    {/* Ответы бота — с markdown-разметкой; у стримящегося
                        пузыря висячие маркеры достраиваются на лету */}
                    {m.role === 'bot' ? <MessageText text={m.text} streaming={m.id === streamMsgId} /> : m.text}
                  </div>
                  {m.image && <img className="message-image" src={m.image} alt={t('chat.attachment')} />}
                  {m.images && m.images.length > 0 && (
                    <div className="message-images">
                      {m.images.map((src, k) => (
                        <img key={k} className="message-image" src={src} alt={t('chat.attachment')} />
                      ))}
                    </div>
                  )}
                  <div className="message-time">
                    {m.time}
                    {/* Доставка/прочтение: наши — очередь → ✓ → ✓✓ (бот ответил);
                        бота — ✓✓, когда чат виден (фокус/наш ответ) */}
                    {m.role === 'user' && m.status === 'queued' && (
                      <span className="msg-status" title={t('chat.msgQueued')}>◷</span>
                    )}
                    {m.role === 'user' && m.status === 'sent' && (
                      <span className="msg-status" title={t('chat.msgSent')}>✓</span>
                    )}
                    {m.role === 'user' && m.status === 'read' && (
                      <span className="msg-status msg-status--read" title={t('chat.msgRead')}>✓✓</span>
                    )}
                    {m.role === 'bot' && m.read && (
                      <span className="msg-status msg-status--read" title={t('chat.msgRead')}>✓✓</span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="chat-status-bar">
          <div className="chat-status-left">
            <span className="dot dot--green" />
            <span>{t('chat.online', { name: persona.name })}</span>
          </div>
          <span>VPC CORE // ONLINE</span>
        </div>

        {/* Плашка цитаты над вводом (когда выбран ответ на сообщение) */}
        {replyToId != null && (
          <div className="chat-reply-bar">
            <span className="message-quote-author">
              {t('chat.inReplyTo', {
                who: messages.find((q) => q.id === replyToId)?.role === 'user' ? t('chat.youDat') : persona.name,
              })}
            </span>
            <span className="message-quote-text chat-reply-text">
              {messages.find((q) => q.id === replyToId)?.text}
            </span>
            <button type="button" className="btn btn--icon" title={t('chat.cancelReply')} onClick={() => setReplyToId(null)}>
              ✕
            </button>
          </div>
        )}

        {/* Превью прикреплённой картинки над вводом */}
        {pendingImage && (
          <div className="chat-attach-bar">
            <img className="chat-attach-thumb" src={pendingImage} alt={t('chat.attachment')} />
            <span className="chat-attach-label">{t('chat.attached')}</span>
            <button type="button" className="btn btn--icon" title={t('chat.removeAttach')} onClick={() => setPendingImage(null)}>
              ✕
            </button>
          </div>
        )}

        <div className="chat-input-bar">
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f && f.type.startsWith('image/')) {
                const reader = new FileReader();
                reader.onload = () => setPendingImage(String(reader.result));
                reader.readAsDataURL(f);
              }
              e.target.value = '';
            }}
          />
          <button
            className="btn btn--icon"
            title={t('chat.attachTitle')}
            onClick={() => fileInputRef.current?.click()}
          >
            +
          </button>
          <input
            className="chat-input"
            type="text"
            placeholder={t('chat.inputPh', { name: persona.name })}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && send()}
          />
          <button className="btn btn--primary" title={t('chat.send')} disabled={waiting || (!draft.trim() && !pendingImage)} onClick={send}>
            {t('chat.send')}
          </button>
        </div>
      </div>
      )}

      {/* Контекстная панель: статус персоны */}
      <aside className={`chat-context ${panelOpen ? '' : 'chat-context--collapsed'}`}>
        <button
          type="button"
          className="chat-context-toggle"
          onClick={() => setPanelOpen((v) => !v)}
          title={panelOpen ? t('chat.collapsePanel') : t('chat.expandPanel')}
        >
          {panelOpen ? '»' : '«'}
        </button>
        <div className="chat-context-body">
          {/* Присутствие: статус и текущее занятие из комнаты */}
          <div className="chat-context-title">{t('chat.presence')}</div>
          <div className="ctx-row">
            <span className="dot dot--green" />
            <span>{t(`status.${persona.status}`)}</span>
          </div>
          <div className="ctx-note">
            {pastime ? t('chat.now', { label: pastime.label, place: pastime.place || '—' }) : t('chat.nowUnknown')}
          </div>

          {/* Настроение с трендом */}
          <div className="chat-context-title">{t('chat.mood')}</div>
          <div className="chat-mood">
            <span className="dot dot--green" />
            <span>{mood}</span>
            <span className="ctx-trend">// {trend}</span>
          </div>

          {/* Самоинициатива: вероятность и последний случай, когда персона написала сама */}
          <div className="chat-context-title">{t('chat.initiative')}</div>
          <div className="ctx-mono">
            {t('chat.probLine', { p: Math.round(initProb * 100), today: initToday, max: initMax })}
          </div>
          <div className="ctx-mono">
            {lastReplyText}
          </div>
          {lastInit && (
            <div className="ctx-note">
              {t('chat.lastInit', { time: lastInit.time, text: lastInit.text })}
            </div>
          )}

          {/* Ближайшее напоминание */}
          <div className="chat-context-title">{t('chat.nextReminder')}</div>
          <div className="ctx-note">
            {nextReminder ? `${nextReminder.time} — ${nextReminder.text}` : t('chat.noActiveReminders')}
          </div>

          {/* Раскрывающиеся краткие списки: дела, инвентарь, уроки */}
          <details className="ctx-details">
            <summary>{t('chat.todosSummary', { n: todos.filter((t) => !t.done).length })}</summary>
            <ul className="ctx-list">
              {todos.map((t) => (
                <li key={t.id} className={t.done ? 'ctx-list-done' : ''}>{t.text}</li>
              ))}
              {todos.length === 0 && <li>{t('chat.emptyList')}</li>}
            </ul>
          </details>
          <details className="ctx-details">
            <summary>{t('chat.inventorySummary', { n: inventory.length })}</summary>
            <ul className="ctx-list">
              {inventory.map((i) => (
                <li key={i.id}>
                  <Icon name={i.icon as IconName} size={13} /> {i.name}
                </li>
              ))}
              {inventory.length === 0 && <li>{t('chat.empty')}</li>}
            </ul>
          </details>
          {hasLearning && (
            <details className="ctx-details">
              <summary>{t('chat.lessons')}</summary>
              <ul className="ctx-list">
                {activeCourse ? (
                  <>
                    <li>{t('chat.lessonLine', { subject: activeCourse.subject, n: activeCourse.lessonCount })}</li>
                    {activeCourse.quizPending > 0 && <li>{t('chat.quizWaiting', { n: activeCourse.quizPending })}</li>}
                    <li>{activeCourse.nextLesson}</li>
                  </>
                ) : (
                  <li>{t('chat.noActiveCourse')}</li>
                )}
              </ul>
            </details>
          )}

          {/* Включённые модули персоны */}
          <details className="ctx-details">
            <summary>{t('chat.modulesSummary', { n: persona.features.length })}</summary>
            <div className="badge-row ctx-features">
              {persona.features.map((f) => (
                <span key={f} className="badge badge--active">{t(`fb.${f}`)}</span>
              ))}
              {persona.features.length === 0 && <span className="ctx-note">{t('chat.noModules')}</span>}
            </div>
          </details>

          {/* Быстрые действия */}
          <div className="ctx-actions">
            <button type="button" className="btn btn--ghost" onClick={() => setDossierOpen(true)}>
              {t('chat.dossier')}
            </button>
          </div>
        </div>
      </aside>
    </div>
  );
}
