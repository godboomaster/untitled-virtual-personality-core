import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useI18n, useMockData } from '../i18n';
import type { ChatMessage, InventoryItem, LearningSession, LtmFact, Reminder, TodoItem } from '../mockData';
import { api, streamChat, StreamInterruptedError } from '../api';
import type { ApiHistoryMessage, InitiativeData } from '../api';
import { useApiOnline, useApiPersonaLlm, useApiProviders } from '../apiData';
import {
  getServerLastTs, markRead, pollInboxNow, pruneInbox, setControlMode, setFastPoll, setGenerating, touchActivity, useInbox,
} from '../inboxStore';
import { usePresenceReporting } from '../presence';
import { notifyBotMessage } from '../notifications';
import { consumeChatPersonaRequest, useChatPersonaRequest } from '../chatNavStore';
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

// Допуск сравнения серверных меток: last_ts и timestamp реплики STM — один и
// тот же float (memory.add_message), но после рестарта буфер читается из БД
const TS_EPS = 0.001;
// Сколько после НАШЕГО завершённого стрима рост серверного last_ts считается
// «своим» (реплики уже в локальной ленте): поллер может показать его и через
// тик после конца стрима
const ABSORB_MS = 30000;
// Самая свежая серверная метка в истории STM (локальные пузыри сюда не идут:
// их ts — часы браузера, с серверными не сравнимы)
const newestTs = (list: ChatMessage[] | undefined) =>
  (list ?? []).reduce((mx, m) => Math.max(mx, m.ts ?? 0), 0);

export default function Chat() {
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
    roomConfigs,
  } = useMockData();

  const [selectedId, setSelectedId] = useState(() => personas[0].id);
  const avatars = usePersonaAvatars();
  const [panelOpen, setPanelOpen] = useState(true);
  // Левый список персон тоже задвигается (как правая контекстная панель)
  const [personaListOpen, setPersonaListOpen] = useState(true);
  const [dossierOpen, setDossierOpen] = useState(false);
  // Просмотр/редактирование YAML текущей персоны (кнопка в шапке чата)
  const [yamlOpen, setYamlOpen] = useState(false);
  // Режим чата: классический (переписка) или голосовой (аватарка персоны)
  const [chatMode, setChatMode] = useState<'classic' | 'voice'>('classic');
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
  // LTM-факты персоны с бэкенда (для скин-досье)
  const [apiFacts, setApiFacts] = useState<LtmFact[] | null>(null);
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
  const [sideData, setSideData] = useState<{
    todos: TodoItem[];
    inventory: InventoryItem[];
    reminders: Reminder[];
    courses: LearningSession[];
    init: InitiativeData | null;
  } | null>(null);

  const persona = personas.find((p) => p.id === selectedId) ?? personas[0];

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
    if (!personas.some((p) => p.id === selectedId)) setSelectedId(personas[0].id);
  }, [personas, selectedId]);

  // Запрос «открыть чат с персоной» из других секций (карточки STATUS на Home)
  const chatPersonaRequest = useChatPersonaRequest();
  useEffect(() => {
    if (!chatPersonaRequest) return;
    if (personas.some((p) => p.id === chatPersonaRequest)) setSelectedId(chatPersonaRequest);
    consumeChatPersonaRequest();
  }, [chatPersonaRequest, personas]);

  // Реплика STM бэкенда → сообщение чата
  const toChatMessage = (m: ApiHistoryMessage, i: number): ChatMessage => ({
    id: i + 1,
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
  const { messages: inboxMessages, unread, generating, lastTs, serverLastTs } = useInbox();
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
  useEffect(() => {
    setFastPoll(remoteGenerating && !inFlight ? persona.id : null);
  }, [remoteGenerating, inFlight, persona.id]);
  useEffect(() => () => setFastPoll(null), []);

  // Список персон по свежести переписки: последняя активная — наверху
  // (персоны без метки свежести остаются в исходном порядке — сортировка
  // стабильная, нули у всех равны)
  const sortedPersonas = [...personas].sort((a, b) => (lastTs[b.id] ?? 0) - (lastTs[a.id] ?? 0));

  // Подтягиваем историю персоны из бэкенда (один раз на персону за сессию)
  useEffect(() => {
    if (!apiOnline || historyByPersona[persona.id]) return;
    const id = persona.id;
    let stale = false;
    api
      .getHistory(id)
      .then((msgs) => {
        if (stale) return;
        setHistoryByPersona((prev) => ({ ...prev, [id]: msgs.map(toChatMessage) }));
        // Инициатива/напоминание пишется и в STM, и в inbox — вычищаем дубли из стора
        pruneInbox(id, new Set(msgs.map((m) => m.content)));
        dropRecovered(id, msgs);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id]);

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
          todos: td.status === 'fulfilled' ? td.value.items.map((x) => ({ id: x.index, text: x.task, done: false })) : [],
          inventory:
            inv.status === 'fulfilled'
              ? inv.value.items.map((x, i) => ({ id: i + 1, icon: 'gem' as IconName, name: x.name, description: x.description, tag: 'created' as const }))
              : [],
          reminders:
            rem.status === 'fulfilled'
              ? rem.value.items.map((r) => ({ id: r.index, text: r.task, time: fmtTs(r.trigger_at), repeat: '', active: true }))
              : [],
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
      return;
    }
    let stale = false;
    api
      .getLtmFacts(persona.id)
      .then((facts) => {
        if (stale) return;
        setApiFacts(
          facts.map((raw, i) => {
            const sep = raw.indexOf(':');
            return sep > 0
              ? { id: i + 1, category: raw.slice(0, sep).trim(), fact: raw.slice(sep + 1).trim() }
              : { id: i + 1, category: 'General', fact: raw };
          }),
        );
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id, waiting, sideEpoch]);
  // Скин персоны: отдельные файлы по экранам, рендер в sandboxed iframe
  const { skins, broken, reportBroken, reset } = usePersonaSkin(persona.id);
  // Записываемые через скин данные (добавленные/переключённые сущности)
  const overlay = usePersonaOverlay(persona.id);
  // История = (STM бэкенда | моковый диалог) + отправленные в этой сессии реплики
  // + фоновые сообщения из inbox-стора (напоминания, инициативы)
  const baseMessages = apiOnline
    ? (historyByPersona[persona.id] ?? [])
    : (chatByPersona[persona.id] ?? []);
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
  const sentVisible = (sentByPersona[persona.id] ?? []).filter(
    (m) => !baseMessages.some((h) => h.text === m.text && (h.ts ?? 0) >= (m.ts ?? 0)),
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
  const mood = init.emotionalState;
  // Тренд настроения из стрика игнорирования инициатив
  const trend = initIgnoreStreak > 2
    ? t('chat.trendWorsening')
    : initIgnoreStreak === 0
      ? t('chat.trendImproving')
      : t('chat.trendStable');
  // Частота самоинициативы: чем свежее диалог, тем чаще персона пишет сама (мок-эвристика)
  const initFreq =
    persona.lastReplyFreshness === 'fresh'
      ? t('chat.freqElevated')
      : persona.lastReplyFreshness === 'yesterday'
        ? t('chat.freqNormal')
        : t('chat.freqReduced');
  // Текущее занятие персоны — первое из конфига её комнаты
  const pastime = (roomConfigs[persona.id] ?? roomConfigs.connor).pastimes[0];
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
  const clearStm = () => {
    setSentByPersona((prev) => ({ ...prev, [persona.id]: [] }));
    if (!apiOnline) return;
    setHistoryByPersona((prev) => ({ ...prev, [persona.id]: [] }));
    api
      .clearChat(persona.id)
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
    if (!apiOnline) {
      // Офлайн-режим (моки): картинка остаётся локальным пузырём
      pushMessage(text, pendingImage);
      setPendingImage(null);
      setDraft('');
      return;
    }
    // Цитата уходит на бэкенд как reply_context (как ответ на сообщение в мессенджерах);
    // картинка — base64 в поле image, в пузыре остаётся локальная копия.
    // pid фиксируем здесь: за время генерации пользователь может уйти
    // в чат другой персоны — флаги waiting/stream гасим именно отправленной
    const pid = persona.id;
    const personaName = persona.name; // имя фиксируем здесь: за время генерации selectedId может смениться
    const quoted = replyToId != null ? messages.find((m) => m.id === replyToId) : undefined;
    const image = pendingImage;
    setPendingImage(null);
    setDraft('');
    markBotRead(pid); // пользователь ответил — реплики бота прочитаны

    pushMessage(text, image, 'sent');
    startGeneration(pid, personaName, text, quoted?.text, image);
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
    const upsertBot = (msgId: number, msgText: string) => {
      const time = new Date().toLocaleTimeString(lang === 'ru' ? 'ru-RU' : 'en-US', { hour: '2-digit', minute: '2-digit' });
      setSentByPersona((prev) => {
        const list = prev[pid] ?? [];
        if (list.some((m) => m.id === msgId)) {
          return { ...prev, [pid]: list.map((m) => (m.id === msgId ? { ...m, text: msgText } : m)) };
        }
        return { ...prev, [pid]: [...list, { id: msgId, role: 'bot' as const, text: msgText, time, ts: msgId / 1000 }] };
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
    )
      .then((res) => {
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
          else pushPersonaMessage(m);
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

  // Реплика персоны в историю (голосовой режим: мок-ответ озвучивается в VoiceChat)
  const pushPersonaMessage = (text: string) => {
    const msg: ChatMessage = {
      id: Date.now(),
      role: 'bot',
      text,
      time: new Date().toLocaleTimeString(lang === 'ru' ? 'ru-RU' : 'en-US', { hour: '2-digit', minute: '2-digit' }),
      ts: Date.now() / 1000,
    };
    setSentByPersona((prev) => ({ ...prev, [persona.id]: [...(prev[persona.id] ?? []), msg] }));
    touchActivity(persona.id, Date.now() / 1000);
  };

  // Данные досье с учётом правок через скин (overlay поверх моков).
  // В API-режиме — реальные списки с бэкенда + добавленные через скин в этой сессии.
  const skinTodos = apiOnline
    ? [...(sideData?.todos ?? []), ...overlay.addedTodos]
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
    ? [...(sideData?.reminders ?? []), ...overlay.addedReminders]
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
    ? [...(apiFacts ?? []), ...overlay.addedFacts]
    : [
        ...(ltmByPersona[persona.id] ?? [])
          .filter((f) => !overlay.deletedFacts.includes(f.id))
          .map((f) => (overlay.editedFacts[f.id] ? { ...f, ...overlay.editedFacts[f.id] } : f)),
        ...overlay.addedFacts,
      ];
  // STM-буфер досье: история диалога с учётом срезки/удалений через скин
  const stmFiltered = messages.filter((m) => !overlay.deletedStm.includes(m.id));
  const skinStm = overlay.stmTrimmed > 0 ? stmFiltered.slice(0, -overlay.stmTrimmed) : stmFiltered;
  // Учебные курсы: реальные с бэкенда / моковые + добавленные через скин
  const skinCourses = apiOnline
    ? [...(sideData?.courses ?? []), ...overlay.addedCourses.map((c) => ({
        ...c,
        status: 'active' as const,
        lessonCount: 0,
        coveredTopics: [] as string[],
        vocabulary: [] as string[],
        nextLesson: '—',
        quizPending: 0,
      }))]
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

  // Действия записи из скина (whitelist — ничего другого скин командовать не может)
  const handleSkinAction = (action: string, values: Record<string, string>, id: string | null) => {
    const numId = id != null ? Number(id) : NaN;
    const hasId = !Number.isNaN(numId);
    switch (action) {
      case 'add-todo':
        // с id — правка существующего дела (кнопка edit-todo заполнила форму)
        if (hasId && values.text?.trim()) overlay.updateTodo(numId, values.text.trim());
        else if (values.text?.trim()) overlay.addTodo(values.text.trim());
        break;
      case 'toggle-todo':
        if (hasId) overlay.toggleTodo(numId);
        break;
      case 'delete-todo':
        if (hasId) overlay.deleteTodo(numId);
        break;
      case 'add-reminder': {
        const clock = (values.clock ?? values.time)?.trim() || '—';
        const when = values.date?.trim() ? `${values.date.trim()}, ${clock}` : clock;
        const repeat = values.repeat?.trim() || '—';
        if (hasId && values.text?.trim()) overlay.updateReminder(numId, { time: when, text: values.text.trim(), repeat });
        else if (values.text?.trim()) overlay.addReminder(when, values.text.trim(), repeat);
        break;
      }
      case 'toggle-reminder':
        if (hasId) overlay.toggleReminder(numId);
        break;
      case 'delete-reminder':
        if (hasId) overlay.deleteReminder(numId);
        break;
      case 'add-fact':
        if (hasId && values.fact?.trim()) overlay.updateFact(numId, values.category?.trim() || '—', values.fact.trim());
        else if (values.fact?.trim()) overlay.addFact(values.category?.trim() || '—', values.fact.trim());
        break;
      case 'delete-fact':
        if (hasId) overlay.deleteFact(numId);
        break;
      case 'trim-stm': {
        const n = Number(values.count);
        if (!Number.isNaN(n) && n > 0) overlay.trimStm(Math.floor(n));
        break;
      }
      case 'delete-stm':
        if (hasId) overlay.deleteStm(numId);
        break;
      case 'add-course':
        if (values.subject?.trim()) overlay.addCourse(values.subject.trim(), values.frequency?.trim() || '—');
        break;
      case 'set-model':
        if (id) overlay.setModel(id, values.model ?? '');
        break;
      case 'make-main':
        if (id) overlay.makeMainProvider(id);
        break;
      case 'toggle-backup':
        if (id) overlay.toggleBackup(id);
        break;
      case 'toggle-feature':
        if (id) overlay.toggleFeature(id);
        break;
      case 'reply':
        if (hasId) setReplyToId(numId);
        break;
      case 'cancel-reply':
        setReplyToId(null);
        break;
      case 'download-file': {
        const file = (filesByPersona[persona.id] ?? []).find((f) => f.id === numId);
        if (file) {
          const url = URL.createObjectURL(new Blob([file.content], { type: 'text/plain' }));
          const a = document.createElement('a');
          a.href = url;
          a.download = file.name;
          a.click();
          URL.revokeObjectURL(url);
        }
        break;
      }
    }
  };

  const handleSkinSetting = (key: string, value: string) => {
    // Чекбоксы инициативы приходят строкой 'true'/'false'
    if (key === 'iniAdaptive' || key === 'iniBayes') {
      overlay.setInit(key === 'iniAdaptive' ? 'adaptive' : 'bayes', value === 'true');
      return;
    }
    const n = Number(value);
    if (Number.isNaN(n)) return;
    if (key === 'temperature' || key === 'maxTokens' || key === 'topP' || key === 'stmSize') {
      overlay.setGen(key, n);
    } else if (key === 'iniSilence' || key === 'iniProbability' || key === 'iniMaxPerDay' || key === 'iniInterval') {
      const map = { iniSilence: 'silence', iniProbability: 'probability', iniMaxPerDay: 'maxPerDay', iniInterval: 'interval' } as const;
      overlay.setInit(map[key], n);
    }
  };

  // Снапшот данных для скина (уходит в iframe через postMessage):
  // переписка + левый сайдбар (персоны) + правый сайдбар (контекст) + досье
  const skinChatState = buildChatPayload({
    persona,
    statusText: t(`status.${persona.status}`),
    youLabel: t('chat.you'),
    typing,
    messages,
    mood,
    pastimeLabel: pastime.label,
    allPersonas: personas.map((p) => ({ persona: p, statusText: t(`status.${p.status}`) })),
    context: {
      pastimePlace: pastime.place,
      trend,
      initiative: t('chat.probLine', {
        p: Math.round(initProb * 100),
        today: initToday,
        max: initMax,
      }),
      lastReply: t('chat.lastReplyLine', { t: persona.lastReply, f: initFreq }),
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
    inventory: sideData ? sideData.inventory : (inventoryByPersona[persona.id] ?? []),
    dossier: {
      facts: skinFacts,
      reminders: skinReminders,
      initiatives: initiativeByPersona[persona.id] ?? [],
      diary: diaryByPersona[persona.id] ?? [],
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
    initState: {
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
      max: overlay.init.silence ?? initBase.silenceThresholdMin,
    }),
    files: filesByPersona[persona.id] ?? [],
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
    modelOverrides: overlay.models,
    providerMain: overlay.mainProvider,
    backupToggled: overlay.backupToggled,
    featureFlags: featureFlags.map((f) => ({
      ...f,
      enabled: overlay.toggledFeatures.includes(f.id) ? !f.enabled : f.enabled,
    })),
    genOverrides: overlay.gen,
    stmSizeDefault: generationDefaults.stmSize,
    keySetLabel: t('apikeys.keySet'),
    keyNotSetLabel: t('apikeys.keyNotSet'),
  });
  // Скин персоны — отдельные файлы по экранам; сломанный экран откатывается
  // на дефолт, остальные остаются кастомными
  const chatSkin = broken.chat ? null : (skins.chat ?? null);
  const dossierSkin = broken.dossier ? null : (skins.dossier ?? null);
  const activeScreen: 'chat' | 'dossier' = dossierOpen ? 'dossier' : 'chat';
  const activeSkin = activeScreen === 'dossier' ? dossierSkin : chatSkin;
  // Пока скин активен, каркас приложения (сайдбар, топбар) красится в его палитру
  useShellTheme(activeSkin);

  // Активный скин заменяет соответствующий вид целиком
  if (activeSkin) {
    return (
      <div className="chat-layout">
        <SkinFrame
          className="skin-frame skin-frame--chat"
          skin={activeSkin}
          screen={activeScreen}
          state={skinChatState}
          onSend={(text, image) => pushMessage(text, image ?? null)}
          onClear={clearStm}
          onSelectPersona={(id) => {
            setSelectedId(id);
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
    <div className="chat-layout">
      {/* Список персон: клик переключает историю диалога; задвигается кнопкой */}
      <div className={`chat-persona-list ${personaListOpen ? '' : 'chat-persona-list--collapsed'}`}>
        <button
          type="button"
          className="chat-context-toggle"
          onClick={() => setPersonaListOpen((v) => !v)}
          title={personaListOpen ? t('chat.collapsePanel') : t('chat.expandPanel')}
        >
          {personaListOpen ? '«' : '»'}
        </button>
        <div className="chat-persona-list-body">
          <div className="chat-persona-list-title">{t('chat.personaList')}</div>
          {sortedPersonas.map((p, i) => (
            <button
              key={p.id}
              className={`chat-persona-item stagger-item ${p.id === selectedId ? 'chat-persona-item--active' : ''}`}
              style={{ animationDelay: `${i * 40}ms` }}
              onClick={() => {
                setSelectedId(p.id);
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
        <PersonaYamlModal personaId={persona.id} onClose={() => setYamlOpen(false)} />
      )}

      {/* Окно чата либо встроенное досье персоны (одно заменяет другое) */}
      {dossierOpen ? (
        <PersonaDossier persona={persona} onClose={() => setDossierOpen(false)} onClearDialog={clearStm} onStmChange={() => { reloadHistory(persona.id, true); setSideEpoch((e) => e + 1); }} stmEpoch={stmEpoch} />
      ) : chatMode === 'voice' ? (
        <VoiceChat
          persona={persona}
          messages={messages}
          onUserMessage={(text) => pushMessage(text, null)}
          onPersonaMessage={pushPersonaMessage}
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
          <div className="ctx-note">{t('chat.now', { label: pastime.label, place: pastime.place })}</div>

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
            {t('chat.lastReplyLine', { t: persona.lastReply, f: initFreq })}
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
