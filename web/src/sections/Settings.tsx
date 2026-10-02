import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import Collapsible from '../components/Collapsible';
import InfoButton from '../components/InfoButton';
import Select from '../components/Select';
import { useHelpTexts, useI18n, useMockData } from '../i18n';
import type { HelpKey } from '../helpTexts';
import { api } from '../api';
import type { PersonaLocalTasks, ProviderInfo } from '../api';
import { refetchProviders, useApiOnline, useApiProviders, refetchPersonaLlm, useApiWebchat } from '../apiData';
import { WEB_HIDDEN_FEATURES } from '../mockData';

/* Настройки персоны (вкладка в досье): провайдеры, параметры генерации,
   фичи. При доступном бэкенде — реальные: ключи/active из .env, генерация
   и фичи пишутся в YAML персоны (фичи применяются после перезапуска).
   Без бэкенда — статичный мок-прототип. */

const featureHelpKeys: Record<string, HelpKey> = {
  web_search: 'settings.feature.web_search',
  file_upload: 'settings.feature.file_upload',
  todo: 'settings.feature.todo',
  reminder: 'settings.feature.reminder',
  learning: 'settings.feature.learning',
  computer_control: 'settings.feature.computer_control',
  proactive: 'settings.feature.proactive',
  rhythm: 'settings.feature.rhythm',
  life: 'settings.feature.life',
  self_memory: 'settings.feature.self_memory',
  inventory: 'settings.feature.inventory',
  ui_room_mood_sync: 'settings.feature.ui_room_mood_sync',
  room_llm_placement: 'settings.feature.room_llm_placement',
  room_pokes_to_llm: 'settings.feature.room_pokes_to_llm',
  moderation: 'settings.feature.moderation',
  rate_limit: 'settings.feature.rate_limit',
  punish_block: 'settings.feature.punish_block',
  light_context: 'settings.feature.light_context',
};

// Группы фич в карточке «Фичи». Флаги из YAML, которых нет в группах
// (незнакомые UI), попадают в «Прочее» под своим ключом
const featureGroups: { title: string; ids: string[] }[] = [
  { title: 'settings.featGroup.assistant', ids: ['web_search', 'file_upload', 'todo', 'reminder', 'learning', 'computer_control'] },
  {
    title: 'settings.featGroup.life',
    ids: ['proactive', 'rhythm', 'life', 'self_memory', 'inventory', 'ui_room_mood_sync', 'room_llm_placement', 'room_pokes_to_llm'],
  },
  { title: 'settings.featGroup.safety', ids: ['moderation', 'rate_limit', 'punish_block'] },
  { title: 'settings.featGroup.other', ids: ['light_context'] },
];

// Фичи, которые в YAML хранятся dict'ом с параметрами (enabled + интервалы и т.п.)
const DICT_FEATURES = new Set(['proactive', 'learning', 'rhythm', 'life']);

interface SettingsProps {
  embedded?: boolean; // встраивание без заголовка раздела (модалка «Досье»)
  personaId?: string; // персона, чей конфиг редактируем (обязателен для API-режима)
}

export default function Settings({ embedded, personaId }: SettingsProps) {
  const { t } = useI18n();
  const { llmProviders, generationDefaults, featureFlags, providerModels } = useMockData();
  const helpTexts = useHelpTexts();
  const apiOnline = useApiOnline();

  // Провайдеры с бэкенда (общий кеш)
  const providers = useApiProviders();
  // Веб-чаты с бэкенда: включённые сайты (для строк персональной цепочки)
  const webchat = useApiWebchat();
  // Служебные задачи персоны: у каждой свой движок — Ollama или веб-чат
  // (веб-чат — с выбором конкретного сайта); без выбора — дефолт по роду
  // задачи. Сайты фоновых задач зависят от цепочки провайдеров персоны —
  // перечитываем снимок и после сохранения цепочки
  const [localTasks, setLocalTasks] = useState<PersonaLocalTasks | null>(null);
  const refetchLocalTasks = () => {
    if (!personaId) return;
    api.getPersonaLocalTasks(personaId).then(setLocalTasks).catch(() => setLocalTasks(null));
  };
  const patchLocalTasks = (patch: Parameters<typeof api.updatePersonaLocalTasks>[1]) => {
    if (!personaId) return;
    api.updatePersonaLocalTasks(personaId, patch).then(setLocalTasks).catch(() => {});
  };

  // Конфиг персоны с бэкенда → черновики форм
  const [gen, setGen] = useState({ temperature: 0.7, maxTokens: 2000, topP: 0.9, stmSize: 500, splitMessages: false });
  const [featureDraft, setFeatureDraft] = useState<Record<string, boolean>>({});
  // Исходные значения features из YAML — чтобы при сохранении не потерять
  // параметры dict-фич (proactive/learning: интервалы, лимиты и т.п.)
  const [featureRaw, setFeatureRaw] = useState<Record<string, unknown>>({});
  // Персональные провайдеры: основной (null = глобальный) и приоритет fallback
  const [llmPrimary, setLlmPrimary] = useState<string | null>(null);
  const [llmFallback, setLlmFallback] = useState<string[]>([]);
  // Убранные из цепочки ЭТОЙ персоны (llm.exclude); другие персоны не затрагиваются
  const [llmExclude, setLlmExclude] = useState<string[]>([]);
  const [restartNote, setRestartNote] = useState(false);
  // Какая карточка только что сохранилась (✓ только на её кнопке)
  const [savedCard, setSavedCard] = useState<'llm' | 'gen' | 'features' | null>(null);
  // Снимки загруженного конфига — чтобы слать только изменённые поля карточки
  const [genInit, setGenInit] = useState<{ temperature: number; maxTokens: number; topP: number; stmSize: number; splitMessages: boolean } | null>(null);
  const [featureInit, setFeatureInit] = useState<Record<string, boolean> | null>(null);
  const [llmInit, setLlmInit] = useState<{ primary: string | null; fallback: string[]; exclude: string[] } | null>(null);
  // Провайдеры по назначению (llm.answer/cc/vision_provider): '' — обычная цепочка
  const [purposeProvs, setPurposeProvs] = useState({ answer: '', cc: '', vision: '' });
  const [purposeInit, setPurposeInit] = useState<{ answer: string; cc: string; vision: string } | null>(null);
  // Лимиты веб-чатов персоны: {сайт: {enabled, per_hour}}; нет записи — дефолт 40/час
  const [wcLimits, setWcLimits] = useState<Record<string, { enabled: boolean; per_hour: number }>>({});
  const [wcLimitsInit, setWcLimitsInit] = useState<Record<string, { enabled: boolean; per_hour: number }>>({});

  useEffect(() => {
    if (!apiOnline || !personaId) return;
    setRestartNote(false);
    api.getPersonaLocalTasks(personaId).then(setLocalTasks).catch(() => setLocalTasks(null));
    api
      .getPersonaConfig(personaId)
      .then((c) => {
        const loadedGen = {
          temperature: c.settings.temperature ?? 0.7,
          maxTokens: c.settings.max_tokens ?? 2000,
          topP: c.settings.top_p ?? 0.9,
          stmSize: c.stm_size ?? 500,
          splitMessages: c.settings.split_messages === true,
        };
        setGen(loadedGen);
        setGenInit(loadedGen);
        // Полный список: все известные фичи (отсутствующие в YAML — выключены)
        // + любые дополнительные boolean-флаги из конкретного YAML
        const bools: Record<string, boolean> = {};
        for (const f of featureFlags) {
          const v = c.features[f.id];
          // computer_control — dict конфига без enabled-ключа: режим включён
          // (enabled: false внутри dict — выключен, списки при этом сохраняются)
          bools[f.id] =
            typeof v === 'boolean'
              ? v
              : v !== null && typeof v === 'object'
                ? f.id === 'computer_control'
                  ? (v as { enabled?: unknown }).enabled !== false
                  : Boolean((v as { enabled?: unknown }).enabled)
                : false;
        }
        for (const [k, v] of Object.entries(c.features)) {
          // фичи, специфичные для мессенджера (export_server/restore_memory), в вебе инертны — скрываем
          if (WEB_HIDDEN_FEATURES.has(k)) continue;
          if (typeof v === 'boolean' && !(k in bools)) bools[k] = v;
        }
        setFeatureDraft(bools);
        setFeatureInit(bools);
        setFeatureRaw(c.features);
        const loadedPrimary = c.llm?.primary ?? null;
        const loadedFallback = c.llm?.fallback ?? [];
        const loadedExclude = c.llm?.exclude ?? [];
        setLlmPrimary(loadedPrimary);
        setLlmFallback(loadedFallback);
        setLlmExclude(loadedExclude);
        setLlmInit({ primary: loadedPrimary, fallback: loadedFallback, exclude: loadedExclude });
        const loadedPurpose = {
          answer: c.llm?.answer_provider ?? '',
          cc: c.llm?.cc_provider ?? '',
          vision: c.llm?.vision_provider ?? '',
        };
        setPurposeProvs(loadedPurpose);
        setPurposeInit(loadedPurpose);
        const loadedWc: Record<string, { enabled: boolean; per_hour: number }> = {};
        for (const [site, cfg] of Object.entries(c.llm?.webchat_limits ?? {})) {
          loadedWc[site] = { enabled: cfg.enabled !== false, per_hour: cfg.per_hour ?? 40 };
        }
        setWcLimits(loadedWc);
        setWcLimitsInit(loadedWc);
      })
      .catch(() => {});
  }, [apiOnline, personaId, featureFlags]);

  const flashSaved = (card: 'llm' | 'gen' | 'features') => {
    setSavedCard(card);
    setTimeout(() => setSavedCard(null), 2000);
  };

  const saveGen = () => {
    if (!personaId || !genInit) return;
    // Шлём только реально изменённые поля карточки — иначе перезапишем
    // параллельные правки (другая вкладка, правка YAML) старыми значениями
    const settings: Record<string, number | boolean> = {};
    if (gen.temperature !== genInit.temperature) settings.temperature = gen.temperature;
    if (gen.maxTokens !== genInit.maxTokens) settings.max_tokens = gen.maxTokens;
    if (gen.topP !== genInit.topP) settings.top_p = gen.topP;
    if (gen.splitMessages !== genInit.splitMessages) settings.split_messages = gen.splitMessages;
    const patch: { settings?: Record<string, number | boolean>; stm_size?: number } = {};
    if (Object.keys(settings).length) patch.settings = settings;
    if (gen.stmSize !== genInit.stmSize) patch.stm_size = gen.stmSize;
    if (!patch.settings && patch.stm_size === undefined) return;
    api
      .updatePersonaConfig(personaId, patch)
      .then(() => {
        flashSaved('gen');
        setGenInit({ ...gen });
      })
      .catch(() => {});
  };

  const saveFeatures = () => {
    if (!personaId || !featureInit) return;
    // Только переключённые фичи. Нетронутые не шлём: dict-фичи (proactive/learning)
    // хранят параметры, которые правятся во вкладке «Инициатива», — полная
    // перезапись затирала бы их значениями, загруженными при монтировании
    const payload: Record<string, unknown> = {};
    for (const [k, on] of Object.entries(featureDraft)) {
      if (featureInit[k] === on) continue;
      const orig = featureRaw[k];
      payload[k] =
        orig !== null && typeof orig === 'object'
          ? { ...orig, enabled: on }
          : DICT_FEATURES.has(k)
            ? { enabled: on }
            : on;
    }
    if (!Object.keys(payload).length) return;
    api
      .updatePersonaConfig(personaId, { features: payload })
      .then((r) => {
        flashSaved('features');
        setRestartNote(r.restart_required);
        setFeatureInit({ ...featureDraft });
      })
      .catch(() => {});
  };

  // Глобальная смена основного (standalone-настройки без персоны)
  const makeMain = (id: string) => {
    api.setActiveProvider(id).then(refetchProviders).catch(() => {});
  };

  // ── Персональные провайдеры (досье персоны): основной + приоритет fallback ──
  // Веб-чаты (без API-ключей) — полноценные строки в цепочке персоны: их
  // можно сделать основными и двигать в fallback-порядке. Строки — из
  // глобально включённых сайтов + сайтов из сохранённого llm персоны
  // (персона может держать сайт, выключенный глобально)
  const webchatSites = (() => {
    const out = [...(webchat?.sites ?? [])];
    [llmPrimary, ...llmFallback, ...llmExclude].forEach((x) => {
      if (x && x.startsWith('webchat:')) {
        const s = x.split(':', 2)[1];
        if (s && !out.includes(s)) out.push(s);
      }
    });
    return out;
  })();
  const webchatProvs: ProviderInfo[] = webchatSites.map((s) => ({
    id: `webchat:${s}`,
    name: s,
    key_set: true, // доступность не от ключа — от логина в браузере бота
    keys_count: 0,
    keys: [],
    model: '',
    active: false,
    local: false,
  }));
  // Полный список для персональной цепочки (в standalone-настройках веб-чаты
  // не показываем: глобальным основным их сделать нельзя)
  const allProviders = providers ? [...providers, ...webchatProvs] : providers;
  // Эффективный лимит сайта: не настроен — дефолт (включён, 40/час)
  const wcLimitOf = (site: string) => wcLimits[site] ?? { enabled: true, per_hour: 40 };
  const setWcLimit = (site: string, patch: Partial<{ enabled: boolean; per_hour: number }>) =>
    setWcLimits((m) => ({ ...m, [site]: { ...(m[site] ?? { enabled: true, per_hour: 40 }), ...patch } }));
  const personaMode = apiOnline && providers !== null && !!personaId;
  const globalActiveId = providers?.find((p) => p.active)?.id ?? null;
  const effPrimary = llmPrimary ?? globalActiveId;

  // Порядок строк: основной, затем fallback-черновик, затем остальные
  const orderedProviders = () => {
    if (!allProviders) return [];
    if (!personaMode) return providers ?? [];
    // В досье персоны — только провайдеры с заданным ключом (или локальные):
    // бесключевые всё равно не смогут ответить, они настраиваются в общих настройках
    const usable = allProviders.filter(
      (p) => (p.key_set || p.local) && (p.id === effPrimary || !llmExclude.includes(p.id)),
    );
    const byId = new Map(usable.map((p) => [p.id, p]));
    const out: typeof usable = [];
    const push = (id: string) => {
      const p = byId.get(id);
      if (p && !out.includes(p)) out.push(p);
    };
    if (effPrimary) push(effPrimary);
    llmFallback.forEach(push);
    usable.forEach((p) => push(p.id));
    return out;
  };
  // Видимый fallback-порядок для произвольной пары (основной, fallback-черновик)
  const computeVisibleFallback = (primaryId: string | null, fb: string[], exclude: string[] = llmExclude): string[] => {
    if (!allProviders) return [];
    const eff = primaryId ?? globalActiveId;
    const order: string[] = [];
    const push = (id: string | null | undefined) => {
      if (id && !order.includes(id)) order.push(id);
    };
    push(eff);
    fb.forEach(push);
    allProviders.forEach((p) => push(p.id));
    return order.filter((id) => id !== eff && !exclude.includes(id));
  };
  // Убранные провайдеры, которых можно вернуть (основной не убирается)
  const excludedProviders = (allProviders ?? []).filter(
    (p) => llmExclude.includes(p.id) && p.id !== effPrimary && (p.key_set || p.local),
  );
  const excludeProvider = (id: string) => {
    setLlmExclude((cur) => (cur.includes(id) ? cur : [...cur, id]));
    setLlmFallback((cur) => cur.filter((x) => x !== id));
  };
  // Возвращённый встаёт в конец цепочки
  const restoreProvider = (id: string) => {
    setLlmFallback(() => [...visibleFallback(), id]);
    setLlmExclude((cur) => cur.filter((x) => x !== id));
  };
  // Видимый fallback-порядок (все строки после основного)
  const visibleFallback = () => computeVisibleFallback(llmPrimary, llmFallback);

  const makePersonaMain = (id: string) => {
    // Старый основной уходит в голову fallback-цепочки
    setLlmFallback(() => {
      const rest = visibleFallback().filter((x) => x !== id);
      return effPrimary ? [effPrimary, ...rest] : rest;
    });
    setLlmPrimary(id);
  };

  const moveFallback = (id: string, dir: -1 | 1) => {
    const order = visibleFallback();
    const i = order.indexOf(id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    setLlmFallback(order);
  };

  // Перетаскивание строки fallback-цепочки (стрелки ↑↓ остаются). Мышь —
  // за любую часть строки, кроме кнопок/полей; палец — только за ручку ⋮⋮
  // (у неё touch-action: none, остальная строка прокручивает страницу).
  // Место вставки — по серединам остальных строк относительно указателя:
  // вычисление идемпотентно, строка не «дребезжит» между соседями.
  const [fbDrag, setFbDrag] = useState<{ id: string; dy: number } | null>(null);
  const providerListRef = useRef<HTMLUListElement>(null);
  const startFallbackDrag = (e: ReactPointerEvent<HTMLLIElement>, id: string) => {
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest('button, input, select, textarea, a, label, [role="combobox"]')) return;
    if (e.pointerType === 'touch' && !target.closest('.provider-grip')) return;
    // Мышь: без выделения текста на время протаскивания
    if (e.pointerType === 'mouse') e.preventDefault();
    const li = e.currentTarget;
    const startY = e.clientY;
    const grab = e.clientY - li.getBoundingClientRect().top;
    let started = false;
    const onMove = (ev: PointerEvent) => {
      if (!started) {
        if (Math.abs(ev.clientY - startY) < 5) return;
        started = true;
        document.body.classList.add('is-row-dragging');
      }
      const others = Array.from(
        providerListRef.current?.querySelectorAll<HTMLElement>('[data-fb-id]') ?? [],
      ).filter((el) => el !== li);
      const idx = others.filter((el) => {
        const r = el.getBoundingClientRect();
        return r.top + r.height / 2 < ev.clientY;
      }).length;
      const beforeId = others[idx]?.dataset.fbId;
      const afterId = idx > 0 ? others[idx - 1].dataset.fbId : undefined;
      setLlmFallback((prev) => {
        const cur = computeVisibleFallback(llmPrimary, prev);
        const order = cur.filter((x) => x !== id);
        let at = beforeId ? order.indexOf(beforeId) : afterId ? order.indexOf(afterId) + 1 : order.length;
        if (at < 0) at = order.length;
        order.splice(at, 0, id);
        return order.join('\n') === cur.join('\n') ? prev : order;
      });
      // Строка идёт за указателем: смещение от её естественного места
      // (отрисованное смещение — из style: рендер может отставать от событий)
      const applied = parseFloat(li.style.translate.split(' ')[1] ?? '') || 0;
      const naturalTop = li.getBoundingClientRect().top - applied;
      setFbDrag({ id, dy: ev.clientY - grab - naturalTop });
    };
    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      document.body.classList.remove('is-row-dragging');
      setFbDrag(null);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  };

  const saveLlm = () => {
    if (!personaId) return;
    // Лимиты шлём полной картой по всем показанным сайтам (включая дефолтные
    // записи) — серверный merge тогда ничего не теряет и не дублирует
    const limitsPayload: Record<string, { enabled: boolean; per_hour?: number }> = {};
    webchatSites.forEach((s) => {
      const l = wcLimitOf(s);
      limitsPayload[s] = l.enabled ? { enabled: true, per_hour: l.per_hour } : { enabled: false };
    });
    // Основной не может быть убран — такую запись не сохраняем
    const savedExclude = llmExclude.filter((x) => x !== effPrimary);
    api
      .updatePersonaConfig(personaId, {
        llm: {
          primary: llmPrimary, fallback: visibleFallback(), webchat_limits: limitsPayload,
          exclude: savedExclude,
          // провайдеры по назначению: пустая строка → null → сервер снимает ключ
          answer_provider: purposeProvs.answer || null,
          cc_provider: purposeProvs.cc || null,
          vision_provider: purposeProvs.vision || null,
        },
      })
      .then(() => {
        flashSaved('llm');
        setLlmExclude(savedExclude);
        setLlmInit({ primary: llmPrimary, fallback: llmFallback, exclude: savedExclude });
        setWcLimitsInit({ ...wcLimits });
        setPurposeInit({ ...purposeProvs });
        refetchPersonaLlm(personaId); // шапка чата покажет нового основного сразу
        refetchLocalTasks(); // сайты фоновых задач — из новой цепочки
      })
      .catch(() => {});
  };

  // «Грязность» карточек: кнопка сохранения активна только при реальных изменениях
  const genDirty = !!genInit && (
    gen.temperature !== genInit.temperature || gen.maxTokens !== genInit.maxTokens ||
    gen.topP !== genInit.topP || gen.stmSize !== genInit.stmSize ||
    gen.splitMessages !== genInit.splitMessages
  );
  const featuresDirty = !!featureInit && Object.keys(featureDraft).some((k) => featureDraft[k] !== featureInit[k]);
  const llmDirty = !!llmInit && !!providers && (
    (!!purposeInit && (purposeProvs.answer !== purposeInit.answer
      || purposeProvs.cc !== purposeInit.cc || purposeProvs.vision !== purposeInit.vision)) ||
    llmPrimary !== llmInit.primary ||
    visibleFallback().join(',') !== computeVisibleFallback(llmInit.primary, llmInit.fallback, llmInit.exclude).join(',') ||
    [...llmExclude].sort().join(',') !== [...llmInit.exclude].sort().join(',') ||
    webchatSites.some((s) => {
      const a = wcLimitOf(s);
      const b = wcLimitsInit[s] ?? { enabled: true, per_hour: 40 };
      return a.enabled !== b.enabled || a.per_hour !== b.per_hour;
    })
  );

  // Моковый режим (без бэкенда)
  const [mainId, setMainId] = useState(() => llmProviders.find((p) => p.active)?.id ?? llmProviders[0].id);
  const [backupIds, setBackupIds] = useState<string[]>(() => llmProviders.filter((p) => p.backup).map((p) => p.id));
  const main = llmProviders.find((p) => p.id === mainId) ?? llmProviders[0];
  const toggleBackup = (id: string) => {
    setBackupIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const apiMode = apiOnline && providers !== null;
  // Сводка свёрнутого списка: первые звенья цепочки и их число
  const providersSummary = (() => {
    const names = apiMode ? orderedProviders().map((p) => p.name) : [main.name];
    const head = names.slice(0, 3).join(' → ');
    return names.length > 3 ? `${head} … (${names.length})` : head;
  })();

  return (
    <div className={embedded ? undefined : 'section'}>
      {!embedded && (
        <div className="section-header">
          <div>
            <h1 className="section-title">{t('nav.settings')}</h1>
            <p className="section-subtitle">{t('settings.subtitle')}</p>
          </div>
        </div>
      )}

      {/* Провайдеры */}
      <div className="card">
        <h2 className="card-title">
          {t('settings.providers')}
          <InfoButton helpKey="settings.activeProvider" />
        </h2>

        {/* Цепочка провайдеров — разворачивающийся список; в свёрнутом
            заголовке — первые звенья цепочки */}
        <Collapsible
          title={t('settings.providerChain')}
          summary={providersSummary}
          storageKey="vpc-settings-open-providers"
        >
          <ul className="memory-list" ref={providerListRef}>
            {apiMode
              ? orderedProviders().map((p, i) => {
                  const isMain = personaMode ? p.id === effPrimary : p.active;
                  const canUse = p.key_set || p.local;
                  const wcSite = p.id.startsWith('webchat:') ? p.id.split(':')[1] : null;
                  const draggable = personaMode && !isMain && canUse;
                  const dragging = fbDrag?.id === p.id;
                  return (
                    <li
                      key={p.id}
                      data-fb-id={draggable ? p.id : undefined}
                      className={`provider-item stagger-item ${isMain ? 'prov-main' : ''} ${draggable ? 'is-draggable' : ''} ${dragging ? 'is-dragging' : ''}`}
                      // translate, а не transform: transform держит анимация появления (fill both)
                      style={{ animationDelay: `${i * 40}ms`, translate: dragging ? `0 ${fbDrag.dy}px` : undefined }}
                      onPointerDown={draggable ? (e) => startFallbackDrag(e, p.id) : undefined}
                    >
                      <div className="provider-main">
                        {personaMode && (
                          <span
                            className="provider-grip"
                            aria-hidden="true"
                            title={draggable ? t('settings.dragHint') : undefined}
                            style={{ visibility: draggable ? 'visible' : 'hidden' }}
                          >
                            ⋮⋮
                          </span>
                        )}
                        <span className="provider-name">{p.name}</span>
                        {personaMode ? (
                          // В досье модели персоны редактируются в карточке «Модели провайдеров»
                          p.model && <span className="provider-model">{p.model}</span>
                        ) : (
                          // Общие настройки: глобальная модель провайдера (blur/Enter — сохранить)
                          <>
                            <input
                              key={`${p.id}:${p.model}`}
                              className="input pmodel-input"
                              list={`settings-models-${p.id}`}
                              defaultValue={p.model}
                              placeholder={t('dossier.modelPh')}
                              spellCheck={false}
                              onBlur={(e) => {
                                const v = e.target.value.trim();
                                if (v && v !== p.model) {
                                  api.setProviderModel(p.id, v).then(refetchProviders).catch(() => {});
                                } else {
                                  e.target.value = p.model;
                                }
                              }}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                              }}
                            />
                            <datalist id={`settings-models-${p.id}`}>
                              {(providerModels[p.id] ?? []).map((m) => (
                                <option key={m} value={m} />
                              ))}
                            </datalist>
                          </>
                        )}
                        {p.local && <span className="badge">{t('settings.localBadge')}</span>}
                        {isMain && (
                          <span className="badge badge--active">
                            {personaMode && !llmPrimary ? t('settings.mainGlobalBadge') : t('settings.mainBadge')}
                          </span>
                        )}
                      </div>
                      <div className="provider-side">
                        {p.id.startsWith('webchat:') ? (
                          // Веб-чат: без ключа — доступность от логина в браузере бота.
                          // В досье — свой лимит: tick снимает его, число — сообщений в час
                          <>
                            <span className="badge">{t('settings.webchatBadge')}</span>
                            {personaMode && wcSite && (
                              <>
                                <label className="switch" title={t('settings.webchatLimit')}>
                                  <input
                                    type="checkbox"
                                    checked={wcLimitOf(wcSite).enabled}
                                    onChange={(e) => setWcLimit(wcSite, { enabled: e.target.checked })}
                                  />
                                  <span className="switch-slider" />
                                </label>
                                <input
                                  className="input"
                                  style={{ width: 64 }}
                                  type="number"
                                  min={1}
                                  max={500}
                                  disabled={!wcLimitOf(wcSite).enabled}
                                  value={wcLimitOf(wcSite).per_hour}
                                  title={t('settings.webchatPerHour')}
                                  onChange={(e) => {
                                    const v = Math.max(1, Math.min(500, Number(e.target.value) || 1));
                                    setWcLimit(wcSite, { per_hour: v });
                                  }}
                                />
                              </>
                            )}
                          </>
                        ) : (
                          <>
                            {!p.local &&
                              (p.key_set ? (
                                <span className="badge badge--success">
                                  {t('apikeys.keySet')}{p.keys_count > 1 ? ` · ${t('settings.keyRotation', { n: p.keys_count })}` : ''}
                                </span>
                              ) : (
                                <span className="badge badge--muted">{t('apikeys.keyNotSet')}</span>
                              ))}
                            <InfoButton helpKey="settings.keyStatus" />
                          </>
                        )}
                        {personaMode ? (
                          !isMain && canUse && (
                            <>
                              <button
                                className="btn btn--ghost"
                                title={t('settings.moveUp')}
                                aria-label={t('settings.moveUp')}
                                onClick={() => moveFallback(p.id, -1)}
                              >
                                ↑
                              </button>
                              <button
                                className="btn btn--ghost"
                                title={t('settings.moveDown')}
                                aria-label={t('settings.moveDown')}
                                onClick={() => moveFallback(p.id, 1)}
                              >
                                ↓
                              </button>
                              <button className="btn btn--ghost" onClick={() => makePersonaMain(p.id)}>
                                {t('settings.makeMain')}
                              </button>
                              <button
                                className="btn btn--ghost"
                                title={t('settings.excludeHint')}
                                aria-label={t('settings.excludeHint')}
                                onClick={() => excludeProvider(p.id)}
                              >
                                ✕
                              </button>
                            </>
                          )
                        ) : (
                          !p.active && canUse && (
                            <button className="btn btn--ghost" onClick={() => makeMain(p.id)}>
                              {t('settings.makeMain')}
                            </button>
                          )
                        )}
                      </div>
                    </li>
                  );
                })
              : llmProviders.map((p, i) => {
                  const isMain = p.id === main.id;
                  return (
                    <li
                      key={p.id}
                      className={`provider-item stagger-item ${isMain ? 'prov-main' : ''}`}
                      style={{ animationDelay: `${i * 40}ms` }}
                    >
                      <div className="provider-main">
                        <label className="switch" title={t('settings.onBackup')}>
                          <input
                            type="checkbox"
                            disabled={isMain}
                            checked={isMain || backupIds.includes(p.id)}
                            onChange={() => toggleBackup(p.id)}
                          />
                          <span className="switch-slider" />
                        </label>
                        <span className="provider-name">{p.name}</span>
                        {p.model && <span className="provider-model">{p.model}</span>}
                        {p.local && <span className="badge">{t('settings.localBadge')}</span>}
                        {isMain && <span className="badge badge--active">{t('settings.mainBadge')}</span>}
                      </div>
                      <div className="provider-side">
                        {!p.local &&
                          (p.keySet ? (
                            <span className="badge badge--success">
                              {t('apikeys.keySet')}{p.keysCount > 1 ? ` · ${t('settings.keyRotation', { n: p.keysCount })}` : ''}
                            </span>
                          ) : (
                            <span className="badge badge--muted">{t('apikeys.keyNotSet')}</span>
                          ))}
                        <InfoButton helpKey="settings.keyStatus" />
                        {p.local && <button className="btn btn--ghost">{t('settings.checkAvailability')}</button>}
                        {!isMain && (
                          <button className="btn btn--ghost" onClick={() => setMainId(p.id)}>
                            {t('settings.makeMain')}
                          </button>
                        )}
                      </div>
                    </li>
                  );
                })}
          </ul>
          {/* Убранные из цепочки этой персоны — можно вернуть (встают в конец) */}
          {personaMode && apiMode && excludedProviders.length > 0 && (
            <div className="prov-excluded">
              <div className="field-hint">{t('settings.excludedTitle')}</div>
              <ul className="memory-list">
                {excludedProviders.map((p) => (
                  <li key={p.id} className="provider-item prov-excluded-item">
                    <div className="provider-main">
                      <span className="provider-name">{p.name}</span>
                      <span className="badge">
                        {t(
                          p.local
                            ? 'settings.localBadge'
                            : p.id.startsWith('webchat')
                              ? 'settings.webchatBadge'
                              : 'settings.apiBadge',
                        )}
                      </span>
                    </div>
                    <div className="provider-side">
                      <button className="btn btn--ghost" onClick={() => restoreProvider(p.id)}>
                        {t('settings.restoreToChain')}
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="field-hint">
            {personaMode ? t('settings.personaProvidersHint') : t('settings.providersHint')}
          </div>
        </Collapsible>
        {/* Провайдеры режима управления: реплики / решения / vision — сворачиваемый блок */}
        {personaMode && (
          <div className="purpose-provs">
            {(() => {
              // Тип рядом с именем: одно и то же имя бывает и веб-чатом, и API
              const provOptions = (allProviders ?? [])
                .filter((pr) => pr.key_set || pr.local)
                .map((pr) => ({
                  value: pr.id,
                  label: `${pr.name} · ${t(
                    pr.local
                      ? 'settings.localBadge'
                      : pr.id.startsWith('webchat')
                        ? 'settings.webchatBadge'
                        : 'settings.apiBadge',
                  )}`,
                }));
              const rows = [
                ['answer', 'settings.purposeAnswer', 'settings.purposeAnswerShort'],
                ['cc', 'settings.purposeCc', 'settings.purposeCcShort'],
                ['vision', 'settings.purposeVision', 'settings.purposeVisionShort'],
              ] as const;
              // Сводка в свёрнутом заголовке: что назначено, иначе «всё по цепочке»
              const assigned = rows
                .filter(([key]) => purposeProvs[key])
                .map(([key, , shortKey]) => {
                  const id = purposeProvs[key];
                  const label = provOptions.find((o) => o.value === id)?.label ?? id;
                  return `${t(shortKey)}: ${label}`;
                });
              return (
                <Collapsible
                  title={t('settings.purposeTitle')}
                  summary={assigned.length ? assigned.join(' · ') : t('settings.purposeNone')}
                  headExtra={<InfoButton helpKey="settings.purpose" />}
                  storageKey="vpc-settings-open-purpose"
                >
                  {rows.map(([key, labelKey]) => (
                    <div key={key} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <span style={{ flex: 1, fontSize: 13 }}>{t(labelKey)}</span>
                      <Select
                        style={{ maxWidth: 220 }}
                        value={purposeProvs[key]}
                        options={[{ value: '', label: t('settings.purposeChain') }, ...provOptions]}
                        onChange={(v) => setPurposeProvs((cur) => ({ ...cur, [key]: v }))}
                      />
                    </div>
                  ))}
                </Collapsible>
              );
            })()}
          </div>
        )}
        {/* Движки служебных задач персоны: всё, что поручено локальной модели
            (классификаторы, тики жизни, дневники, рерайтер), — по каждой
            задаче свой движок: Ollama или веб-чат (сайт выбирается рядом).
            Без выбора — по роду задачи: на пути ответа — Ollama, фоновые —
            веб-чат фоновых задач, затем запасной веб-чат, затем Ollama */}
        {apiMode && localTasks && localTasks.tasks.length > 0 && (
          <div className="local-backend-row">
            <Collapsible
              title={t('settings.localBackend')}
              headExtra={<InfoButton helpKey="settings.localBackend" />}
              storageKey="vpc-settings-open-local-tasks"
              summary={(() => {
                // Сводка: куда идёт фон и сколько задач на каждом движке
                const bg = localTasks.tasks.find((x) => x.background && x.backend === 'webchat');
                const nOllama = localTasks.tasks.filter((x) => x.backend === 'ollama').length;
                const nWeb = localTasks.tasks.length - nOllama;
                return t('settings.localSummary', {
                  bg: bg ? bg.sites.join(' → ') : 'Ollama',
                  ollama: nOllama,
                  web: nWeb,
                });
              })()}
            >
                <div className="pmodel-row">
                  <span className="provider-name">{t('settings.localBgSite')}</span>
                  <div className="dossier-confirm-actions local-task-controls">
                    <Select
                      className="local-task-site"
                      value={localTasks.bg_site}
                      title={t('settings.localBgSiteTitle')}
                      disabled={localTasks.sites.length === 0}
                      options={[
                        {
                          value: 'fallback',
                          label:
                            t('settings.localBgSiteFallback') +
                            (localTasks.fallback_site ? ` · ${localTasks.fallback_site}` : ''),
                        },
                        {
                          value: 'primary',
                          label:
                            t('settings.localBgSitePrimary') +
                            (localTasks.primary_site ? ` · ${localTasks.primary_site}` : ''),
                        },
                        // Остальные сайты цепочки: первый fallback и основной уже
                        // стоят выше режимами (они следуют за цепочкой персоны).
                        // Выбранный сайт оставляем, даже если он совпал с режимом
                        ...localTasks.sites
                          .filter(
                            (s) =>
                              s === localTasks.bg_site ||
                              (s !== localTasks.fallback_site && s !== localTasks.primary_site),
                          )
                          .map((s) => ({ value: s, label: s })),
                      ]}
                      onChange={(v) => patchLocalTasks({ bg_site: v })}
                    />
                  </div>
                </div>
                {(['conversation', 'background'] as const).map((group) => (
                  <div key={group}>
                    <div className="field-hint">{t(`settings.localGroup.${group}`)}</div>
                    <ul className="memory-list">
                      {localTasks.tasks
                        .filter((task) => task.background === (group === 'background'))
                        .map((task) => {
                          const noSites = localTasks.sites.length === 0;
                          const webDisabled = task.ollama_only || noSites;
                          const title = task.ollama_only
                            ? t('settings.localTaskOllamaOnly')
                            : noSites
                              ? t('settings.localBackendNoSite')
                              : undefined;
                          // Порядок попыток: веб-чаты задачи, затем откат на Ollama
                          const chain = task.backend === 'webchat' ? [...task.sites, 'Ollama'].join(' → ') : 'Ollama';
                          return (
                            <li key={task.id} className="pmodel-row">
                              <span className="provider-name">
                                {t(`localTask.${task.id}`)}
                                <span className="local-task-chain">
                                  {chain}
                                  {!task.explicit && !task.ollama_only && ` · ${t('settings.localTaskDefault')}`}
                                </span>
                              </span>
                              <div className="dossier-confirm-actions local-task-controls">
                                <button
                                  type="button"
                                  className={`btn ${task.backend === 'ollama' ? 'btn--primary' : 'btn--ghost'}`}
                                  disabled={task.backend === 'ollama'}
                                  onClick={() => patchLocalTasks({ task: task.id, backend: 'ollama' })}
                                >
                                  Ollama
                                </button>
                                <button
                                  type="button"
                                  className={`btn ${task.backend === 'webchat' ? 'btn--primary' : 'btn--ghost'}`}
                                  disabled={webDisabled || task.backend === 'webchat'}
                                  title={title}
                                  onClick={() => patchLocalTasks({ task: task.id, backend: 'webchat', site: null })}
                                >
                                  {t('settings.localBackendWebchat')}
                                </button>
                                {task.backend === 'webchat' && !task.ollama_only && (
                                  <Select
                                    className="local-task-site"
                                    value={task.site ?? ''}
                                    title={t('settings.localBackendSite')}
                                    options={[
                                      { value: '', label: t('settings.localBackendSiteAuto') },
                                      ...localTasks.sites.map((s) => ({ value: s, label: s })),
                                    ]}
                                    onChange={(v) =>
                                      patchLocalTasks({ task: task.id, backend: 'webchat', site: v || null })
                                    }
                                  />
                                )}
                                {task.explicit && (
                                  <button
                                    type="button"
                                    className="btn btn--ghost"
                                    title={t('settings.localTaskResetTitle')}
                                    onClick={() => patchLocalTasks({ task: task.id, backend: 'default' })}
                                  >
                                    {t('settings.localTaskReset')}
                                  </button>
                                )}
                              </div>
                            </li>
                          );
                        })}
                    </ul>
                  </div>
                ))}
                <div className="field-hint">{t('settings.localBackendHint')}</div>
            </Collapsible>
          </div>
        )}

        {/* Сохранение провайдеров персоны — под всеми блоками карточки
            (движки локальных задач сохраняются сразу при выборе) */}
        {personaMode && (
          <div className="dossier-confirm-actions" style={{ marginTop: 12 }}>
            <button className="btn btn--primary" onClick={saveLlm} disabled={!llmDirty}>
              {savedCard === 'llm' ? '✓' : t('common.save')}
            </button>
            {(llmPrimary !== null || llmFallback.length > 0 || llmExclude.length > 0) && (
              <button
                className="btn btn--ghost"
                onClick={() => {
                  // Сброс на глобального — сразу сохраняем пустой override
                  setLlmPrimary(null);
                  setLlmFallback([]);
                  setLlmExclude([]);
                  api
                    .updatePersonaConfig(personaId!, { llm: { primary: null, fallback: [], exclude: [] } })
                    .then(() => {
                      flashSaved('llm');
                      setLlmInit({ primary: null, fallback: [], exclude: [] });
                      refetchPersonaLlm(personaId!);
                      refetchLocalTasks();
                    })
                    .catch(() => {});
                }}
              >
                {t('settings.resetToGlobal')}
              </button>
            )}
          </div>
        )}

        {/* Подсказка про лёгкую локальную модель */}
        <div className="prov-gemma-hint">
          {t('settings.gemmaHint')}
          <InfoButton helpKey="settings.gemma" />
        </div>
      </div>

      {/* Параметры генерации */}
      <div className="card">
        <h2 className="card-title">{t('settings.genParams')}</h2>
        <div className="field-grid">
          <div className="field">
            <label className="field-label">
              Temperature
              <InfoButton helpKey="settings.temperature" />
            </label>
            <input
              className="input"
              type="number"
              step="0.1"
              value={apiMode ? gen.temperature : generationDefaults.temperature}
              readOnly={!apiMode}
              onChange={(e) => setGen((g) => ({ ...g, temperature: Number(e.target.value) }))}
            />
          </div>
          <div className="field">
            <label className="field-label">
              Max tokens
              <InfoButton helpKey="settings.maxTokens" />
            </label>
            <input
              className="input"
              type="number"
              value={apiMode ? gen.maxTokens : generationDefaults.maxTokens}
              readOnly={!apiMode}
              onChange={(e) => setGen((g) => ({ ...g, maxTokens: Number(e.target.value) }))}
            />
          </div>
          <div className="field">
            <label className="field-label">
              Top-p
              <InfoButton helpKey="settings.topP" />
            </label>
            <input
              className="input"
              type="number"
              step="0.05"
              value={apiMode ? gen.topP : generationDefaults.topP}
              readOnly={!apiMode}
              onChange={(e) => setGen((g) => ({ ...g, topP: Number(e.target.value) }))}
            />
          </div>
          <div className="field">
            <label className="field-label">
              {t('settings.stmSize')}
              <InfoButton helpKey="settings.stmSize" />
            </label>
            <input
              className="input"
              type="number"
              value={apiMode ? gen.stmSize : generationDefaults.stmSize}
              readOnly={!apiMode}
              onChange={(e) => setGen((g) => ({ ...g, stmSize: Number(e.target.value) }))}
            />
          </div>
        </div>
        <label className="checkbox-row" title={t('settings.splitMessagesHint')}>
          <input
            type="checkbox"
            checked={apiMode ? gen.splitMessages : false}
            readOnly={!apiMode}
            onChange={(e) => setGen((g) => ({ ...g, splitMessages: e.target.checked }))}
          />
          <span>{t('settings.splitMessages')}</span>
        </label>
        {apiMode && (
          <button className="btn btn--primary" onClick={saveGen} disabled={!genDirty}>
            {savedCard === 'gen' ? '✓' : t('common.save')}
          </button>
        )}
      </div>

      {/* Фичи */}
      <div className="card">
        <h2 className="card-title">{t('settings.features')}</h2>
        {(() => {
          // Строки фич: из конфига персоны (API) или мок-список
          const items: { id: string; on: boolean }[] = apiMode
            ? Object.entries(featureDraft).map(([id, on]) => ({ id, on }))
            : featureFlags.map((f) => ({ id: f.id, on: f.enabled }));
          const grouped = new Set(featureGroups.flatMap((g) => g.ids));
          const groups = featureGroups.map((g) => ({
            title: g.title,
            items: g.ids.map((id) => items.find((x) => x.id === id)).filter((x): x is { id: string; on: boolean } => !!x),
          }));
          // Незнакомые флаги — в «Прочее»
          groups[groups.length - 1].items.push(...items.filter((x) => !grouped.has(x.id)));
          return groups
            .filter((g) => g.items.length > 0)
            .map((g) => (
              <div key={g.title} className="features-group">
                <div className="features-group-title">{t(g.title)}</div>
                <div className="features-grid">
                  {g.items.map(({ id, on }) => {
                    const label = featureFlags.find((f) => f.id === id)?.label
                      ?? (featureHelpKeys[id] ? helpTexts[featureHelpKeys[id]].title : id);
                    return (
                      <label key={id} className="checkbox-row feature-row">
                        {apiMode ? (
                          <input
                            type="checkbox"
                            checked={on}
                            onChange={(e) => setFeatureDraft((d) => ({ ...d, [id]: e.target.checked }))}
                          />
                        ) : (
                          <input type="checkbox" defaultChecked={on} readOnly />
                        )}
                        <span className={`feature-label ${featureHelpKeys[id] || featureFlags.some((f) => f.id === id) ? '' : 'feature-label--raw'}`}>
                          {label}
                        </span>
                        {featureHelpKeys[id] && <InfoButton helpKey={featureHelpKeys[id]} />}
                      </label>
                    );
                  })}
                </div>
              </div>
            ));
        })()}
        {apiMode && (
          <>
            <button className="btn btn--primary" onClick={saveFeatures} disabled={!featuresDirty}>
              {savedCard === 'features' ? '✓' : t('common.save')}
            </button>
            {restartNote && <div className="field-hint">// {t('settings.restartRequired')}</div>}
          </>
        )}
      </div>
    </div>
  );
}
