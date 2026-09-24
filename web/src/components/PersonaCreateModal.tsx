import { useEffect, useMemo, useRef, useState } from 'react';
import { useI18n, useMockData } from '../i18n';
import { api, ApiError } from '../api';
import type { PersonaDraft } from '../api';
import FormModal from './FormModal';
import InfoButton from './InfoButton';

/* Модалка создания персоны в виде YAML-редактора конфига: слева форма,
   повторяющая структуру persona.yaml (базовое / features / system_prompt /
   settings), справа — живое превью генерируемого YAML. Кнопки архетипов —
   подсказки: подставляют значения в поля, после чего всё редактируется. */

// Все флаги features как в реальных yaml-конфигах персон (app/personas/*.yaml);
// флаги мессенджер-бота export_server/restore_memory сюда не входят — в вебе они не действуют
const simpleFlags = [
  'rate_limit',
  'moderation',
  'punish_block',
  'web_search',
  'file_upload',
  'self_memory',
  'todo',
  'reminder',
  'inventory',
] as const;

// Все ключи features, включая вложенные learning и proactive
type FeatureKey = (typeof simpleFlags)[number] | 'learning' | 'proactive';

const allFeatureKeys: FeatureKey[] = [...simpleFlags, 'learning', 'proactive'];

// Уровень интеллекта (app/core/intellect.py): 'none' — блок intellect в yaml
// не пишется вовсе, персона работает в legacy-режиме без уровневых механик
type IntellectTier = 'none' | 'primitive' | 'normal' | 'bot';

// Дефолты LLM-настроек по уровню интеллекта: bot — высокая точность
// (temperature/top_p), но max_tokens выше — полный разбор сложной просьбы
// (код, расчёты) требует длинных ответов; normal — тёплый, свободный стиль;
// primitive — стереотипная короткая речь инстинктивного существа.
// Подставляются при выборе tier, дальше правятся вручную.
const tierSettings: Record<Exclude<IntellectTier, 'none'>, { temperature: number; maxTokens: number; topP: number }> = {
  primitive: { temperature: 0.5, maxTokens: 800, topP: 0.85 },
  normal: { temperature: 0.85, maxTokens: 3000, topP: 0.92 },
  bot: { temperature: 0.7, maxTokens: 4000, topP: 0.9 },
};

// Пресеты архетипов-подсказок: какие флаги и tier подставить в поля
// (LLM-настройки берутся из tierSettings по tier архетипа)
const archetypePresets: Record<string, { on: FeatureKey[]; tier: Exclude<IntellectTier, 'none'> }> = {
  analyst: { on: ['web_search', 'file_upload', 'self_memory'], tier: 'bot' },
  companion: { on: ['self_memory', 'reminder', 'inventory', 'proactive'], tier: 'normal' },
  keeper: { on: ['file_upload', 'self_memory', 'inventory', 'learning'], tier: 'normal' },
  assistant: { on: ['todo', 'reminder'], tier: 'bot' },
};

// Транслитерация имени в id (латиница, snake_case)
function transliterate(s: string): string {
  const map: Record<string, string> = {
    а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'yo', ж: 'zh', з: 'z',
    и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r',
    с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'c', ч: 'ch', ш: 'sh', щ: 'sch',
    ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya',
  };
  return s
    .toLowerCase()
    .split('')
    .map((ch) => map[ch] ?? (/[a-z0-9]/.test(ch) ? ch : '_'))
    .join('')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '');
}

// Состояние формы конфига
interface FormState {
  id: string;
  name: string;
  version: string;
  description: string;
  stmSize: number;
  intellectTier: IntellectTier;
  features: Record<FeatureKey, boolean>;
  learning: { quizEvery: number; silenceThreshold: number; minInterval: number; maxInterval: number };
  proactive: { checkInterval: number; silenceThresholdMin: number; probability: number; maxDaily: number };
  triggerWords: string;
  systemPrompt: string;
  temperature: number;
  maxTokens: number;
  topP: number;
}

interface PersonaCreateModalProps {
  initial?: { name?: string; archetypeId?: string }; // предзаполнение (legacy, из Home)
  onClose: () => void;
  onCreate: () => void; // персона создана на бэкенде — родитель обновляет список
}

export default function PersonaCreateModal({ initial, onClose, onCreate }: PersonaCreateModalProps) {
  const { t } = useI18n();
  const { personaArchetypes } = useMockData();
  const [form, setForm] = useState<FormState>(() => ({
    id: transliterate(initial?.name ?? ''),
    name: initial?.name ?? '',
    version: '1.0',
    description: '',
    stmSize: 50,
    intellectTier: 'normal',
    features: Object.fromEntries(allFeatureKeys.map((k) => [k, k === 'self_memory'])) as Record<FeatureKey, boolean>,
    learning: { quizEvery: 3, silenceThreshold: 3, minInterval: 600, maxInterval: 604800 },
    proactive: { checkInterval: 30, silenceThresholdMin: 30, probability: 1.0, maxDaily: 20 },
    triggerWords: '',
    systemPrompt: '',
    // начальные LLM-дефолты соответствуют выбранному tier (normal)
    temperature: tierSettings.normal.temperature,
    maxTokens: tierSettings.normal.maxTokens,
    topP: tierSettings.normal.topP,
  }));
  const [idTouched, setIdTouched] = useState(false); // id авто, пока оператор не правил вручную
  const [copied, setCopied] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  // Режим «готовый YAML»: текст вставлен через Ctrl+V или загружен файлом —
  // форма игнорируется, на бэкенд уходит rawYaml как есть
  const [rawYaml, setRawYaml] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // ── Черновики (бэкенд data/persona_drafts). drafts=null — бэк недоступен, UI скрыт ──
  const [drafts, setDrafts] = useState<PersonaDraft[] | null>(null);
  const [draftId, setDraftId] = useState<string | null>(null); // черновик, из которого загружена форма
  const [draftNote, setDraftNote] = useState<string | null>(null); // короткий статус рядом с кнопкой

  useEffect(() => {
    api.getPersonaDrafts()
      .then((r) => setDrafts(r.drafts))
      .catch(() => setDrafts(null));
  }, []);

  // Краткий статус у кнопки черновика (гаснет сам через 2с)
  const noteDraft = (msg: string) => {
    setDraftNote(msg);
    setTimeout(() => setDraftNote(null), 2000);
  };

  // Сохранить текущее состояние формы как черновик (или обновить загруженный)
  const saveDraft = () => {
    api.savePersonaDraft({ id: draftId, name: form.name.trim(), form: form as unknown as Record<string, unknown>, yaml })
      .then((d) => {
        setDraftId(d.id);
        setDrafts((prev) => [d, ...(prev ?? []).filter((x) => x.id !== d.id)]);
        noteDraft(t('pc.draftSaved'));
      })
      .catch(() => noteDraft(t('pc.draftError')));
  };

  // Загрузить черновик в форму (мердж с дефолтами — на случай старых черновиков без новых полей)
  const loadDraft = (d: PersonaDraft) => {
    const df = (d.form ?? {}) as Partial<FormState>;
    setForm((f) => ({
      ...f,
      ...df,
      features: { ...f.features, ...(df.features ?? {}) },
      learning: { ...f.learning, ...(df.learning ?? {}) },
      proactive: { ...f.proactive, ...(df.proactive ?? {}) },
    }));
    setDraftId(d.id);
    setIdTouched(true); // id из черновика не перезатирать транслитом имени
  };

  const removeDraft = (id: string) => {
    api.deletePersonaDraft(id)
      .then(() => {
        setDrafts((prev) => (prev ?? []).filter((x) => x.id !== id));
        if (draftId === id) setDraftId(null); // форму оставляем как есть
      })
      .catch(() => noteDraft(t('pc.draftError')));
  };

  // Черновик system_prompt по структуре persona_template.yaml
  const promptDraft = (name: string, desc: string): string =>
    [
      t('pc.draftStart', { name: name || t('pc.draftName'), desc: desc || t('pc.draftDesc') }),
      '',
      t('pc.draftBody'),
    ].join('\n');

  // Сборка текста persona.yaml из состояния формы (формат — как в app/personas/*.yaml)
  const buildYaml = (v: FormState): string => {
    const words = v.triggerWords
      .split(',')
      .map((w) => w.trim())
      .filter(Boolean);
    const bool = (b: boolean) => (b ? 'true' : 'false');
    const promptLines = (v.systemPrompt.trim() || promptDraft('', ''))
      .split('\n')
      .map((l) => (l ? `  ${l}` : ''))
      .join('\n');

    return [
      `id: ${v.id.trim() || 'your_persona_id'}`,
      `name: ${v.name.trim() || t('pc.yamlName')}`,
      `version: "${v.version.trim() || '1.0'}"`,
      `description: ${v.description.trim() || t('pc.yamlDesc')}`,
      `stm_size: ${v.stmSize}`,
      ...(v.intellectTier !== 'none' ? ['', 'intellect:', `  tier: ${v.intellectTier}   # primitive | normal | bot`] : []),
      '',
      'features:',
      // owner не выводим: пользователь всегда один и всегда владелец
      ...simpleFlags.map((k) => `  ${k}: ${bool(v.features[k])}`),
      '  learning:',
      `    enabled: ${bool(v.features.learning)}`,
      `    quiz_every: ${v.learning.quizEvery}            ${t('pc.yamlQuizComment')}`,
      `    silence_threshold: ${v.learning.silenceThreshold}     ${t('pc.yamlSilenceComment')}`,
      `    min_interval_seconds: ${v.learning.minInterval}`,
      `    max_interval_seconds: ${v.learning.maxInterval}   ${t('pc.yamlWeekComment')}`,
      '  trigger_words:',
      ...(words.length ? words.map((w) => `    - "${w}"`) : [`    - "${t('pc.yamlKeyword')}"`]),
      '  proactive:',
      `    enabled: ${bool(v.features.proactive)}`,
      `    check_interval_minutes: ${v.proactive.checkInterval}`,
      `    silence_threshold_minutes: ${v.proactive.silenceThresholdMin}`,
      `    initiative_probability: ${v.proactive.probability}`,
      `    max_daily_initiatives: ${v.proactive.maxDaily}`,
      '',
      'system_prompt: |',
      promptLines,
      '',
      'settings:',
      `  temperature: ${v.temperature}  ${t('pc.yamlTempComment')}`,
      `  max_tokens: ${v.maxTokens}   ${t('pc.yamlTokensComment')}`,
      `  top_p: ${v.topP}        ${t('pc.yamlTopPComment')}`,
    ].join('\n');
  };

  // Частичное обновление формы
  const patch = (p: Partial<FormState>) => setForm((f) => ({ ...f, ...p }));

  // Архетип-подсказка: подставляет описание, черновик промпта, флаги и настройки
  const applyArchetype = (archetypeId: string) => {
    const preset = archetypePresets[archetypeId];
    const desc = personaArchetypes.find((a) => a.id === archetypeId)?.desc ?? '';
    patch({
      description: desc,
      systemPrompt: promptDraft(form.name, desc),
      ...(preset
        ? {
            features: Object.fromEntries(allFeatureKeys.map((k) => [k, preset.on.includes(k)])) as Record<FeatureKey, boolean>,
            intellectTier: preset.tier,
            ...tierSettings[preset.tier],
          }
        : {}),
    });
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const yaml = useMemo(() => buildYaml(form), [form, t]);

  const copyYaml = () => {
    navigator.clipboard?.writeText(rawYaml ?? yaml).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  // Создание персоны: YAML уходит на бэкенд (POST /api/personas),
  // файл app/personas/{id}.yaml подхватывается реестром без рестарта.
  // Если вставлен готовый YAML (rawYaml) — уходит он, форма игнорируется.
  const effectiveYaml = rawYaml ?? yaml;
  const submit = () => {
    if (creating) return;
    if (rawYaml === null && !form.name.trim()) return;
    if (rawYaml !== null && !rawYaml.trim()) return;
    setCreating(true);
    setCreateError('');
    api.createPersona(effectiveYaml)
      .then(() => {
        // Черновик, из которого создали персону, больше не нужен
        if (draftId) api.deletePersonaDraft(draftId).catch(() => {});
        onCreate();
      })
      .catch((e) => setCreateError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setCreating(false));
  };

  // Вставка готового YAML: из буфера (кнопка) или Ctrl+V по панели превью
  const pasteYamlText = (text: string) => {
    if (!text.trim()) return;
    setRawYaml(text);
    setCreateError('');
  };

  const pasteFromClipboard = () => {
    // Буфер может быть недоступен (не secure context) — тогда просто
    // переключаем панель в режим вставки: пустое поле ждёт Ctrl+V
    if (navigator.clipboard?.readText) {
      navigator.clipboard.readText().then((text) => pasteYamlText(text || yaml)).catch(() => setRawYaml(yaml));
    } else {
      setRawYaml(yaml);
    }
  };

  // Загрузка готового .yaml-файла
  const loadYamlFile = (f: File | undefined) => {
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => pasteYamlText(String(reader.result ?? ''));
    reader.readAsText(f);
  };

  return (
    <FormModal
      title={t('pc.title')}
      badge="PROC_03"
      xl
      submitLabel={creating ? t('pc.creating') : t('common.create')}
      submitDisabled={creating || (rawYaml === null ? !form.name.trim() : !rawYaml.trim())}
      onSubmit={submit}
      onClose={onClose}
    >
      <div className="pcreate-columns">
        {/* Левая панель: форма конфига (скроллится отдельно) */}
        <div className="pcreate-form-col">
          {/* Черновики: загрузка/удаление (видно, когда есть сохранённые и бэкенд доступен) */}
          {drafts !== null && drafts.length > 0 && (
            <div className="field">
              <label className="field-label">{t('pc.drafts')}</label>
              <div className="pcreate-draft-row">
                <select
                  className="input"
                  value={draftId ?? ''}
                  onChange={(e) => {
                    const d = drafts.find((x) => x.id === e.target.value);
                    if (d) loadDraft(d);
                  }}
                >
                  <option value="">{t('pc.draftSelect')}</option>
                  {drafts.map((d) => (
                    <option key={d.id} value={d.id}>
                      {(d.name || d.id) + ' · ' + new Date(d.updated_at * 1000).toLocaleString()}
                    </option>
                  ))}
                </select>
                {draftId && (
                  <button type="button" className="btn btn--danger" onClick={() => removeDraft(draftId)}>
                    {t('common.delete')}
                  </button>
                )}
              </div>
            </div>
          )}

          {/* Шаблон-подсказка: подставляет значения в поля */}
          <div className="field">
            <label className="field-label">{t('pc.fromTemplate')}</label>
            <div className="room-options">
              {personaArchetypes.map((a) => (
                <button key={a.id} type="button" className="room-option" onClick={() => applyArchetype(a.id)}>
                  {a.name}
                </button>
              ))}
            </div>
          </div>

          <div className="pcreate-sec">{t('pc.secBasic')}</div>
          <div className="field-grid">
            <div className="field">
              <label className="field-label" htmlFor="pc-name">
                {t('pc.nameRequired')}
                <InfoButton helpKey="pc.name" />
              </label>
              <input
                id="pc-name"
                className="input"
                placeholder={t('pc.namePh')}
                value={form.name}
                onChange={(e) => {
                  const v = e.target.value;
                  patch(idTouched ? { name: v } : { name: v, id: transliterate(v) });
                }}
                autoFocus
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="pc-id">
                id
                <InfoButton helpKey="pc.id" />
              </label>
              <input
                id="pc-id"
                className="input"
                placeholder="persona_id"
                value={form.id}
                onChange={(e) => {
                  patch({ id: e.target.value });
                  setIdTouched(true);
                }}
                spellCheck={false}
              />
            </div>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="pc-desc">
              description
              <InfoButton helpKey="pc.description" />
            </label>
            <input
              id="pc-desc"
              className="input"
              placeholder={t('pc.descPh')}
              value={form.description}
              onChange={(e) => patch({ description: e.target.value })}
            />
          </div>
          <div className="field-grid">
            <div className="field">
              <label className="field-label" htmlFor="pc-stm">
                stm_size
                <InfoButton helpKey="pc.stmSize" />
              </label>
              <input
                id="pc-stm"
                className="input"
                type="number"
                value={form.stmSize}
                onChange={(e) => patch({ stmSize: Number(e.target.value) })}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="pc-version">
                version
                <InfoButton helpKey="pc.version" />
              </label>
              <input
                id="pc-version"
                className="input"
                value={form.version}
                onChange={(e) => patch({ version: e.target.value })}
                spellCheck={false}
              />
            </div>
          </div>

          {/* Уровень интеллекта: отдельное измерение поверх features,
              поэтому живёт вне секции features (как в yaml — до блока features) */}
          <div className="field">
            <label className="field-label" htmlFor="pc-intellect">
              intellect.tier
              <InfoButton helpKey="pc.intellect" />
            </label>
            <select
              id="pc-intellect"
              className="input"
              value={form.intellectTier}
              onChange={(e) => {
                const tier = e.target.value as IntellectTier;
                patch({ intellectTier: tier, ...(tier !== 'none' ? tierSettings[tier] : {}) });
              }}
            >
              <option value="normal">normal — человек</option>
              <option value="primitive">primitive — нечеловеческое мышление</option>
              <option value="bot">bot — высокий интеллект</option>
              <option value="none">{t('pc.intellectNone')}</option>
            </select>
          </div>

          <div className="pcreate-sec">
            {t('pc.secFeatures')}
            <InfoButton helpKey="pc.features" />
          </div>
          {/* Поле owner намеренно отсутствует: пользователь всегда один и всегда владелец */}
          <div className="field">
            <div className="features-grid">
              {simpleFlags.map((k) => (
                <label key={k} className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={form.features[k]}
                    onChange={() => patch({ features: { ...form.features, [k]: !form.features[k] } })}
                  />
                  <span>{k}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Вложенный блок learning: sub-поля видны при включении */}
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={form.features.learning}
              onChange={() => patch({ features: { ...form.features, learning: !form.features.learning } })}
            />
            <span>
              learning
              <InfoButton helpKey="pc.learning" />
            </span>
          </label>
          {form.features.learning && (
            <div className="pcreate-sub">
              <div className="field-grid">
                <div className="field" style={{ marginBottom: 8 }}>
                  <label className="field-label">quiz_every</label>
                  <input
                    className="input"
                    type="number"
                    value={form.learning.quizEvery}
                    onChange={(e) => patch({ learning: { ...form.learning, quizEvery: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ marginBottom: 8 }}>
                  <label className="field-label">silence_threshold</label>
                  <input
                    className="input"
                    type="number"
                    value={form.learning.silenceThreshold}
                    onChange={(e) => patch({ learning: { ...form.learning, silenceThreshold: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ marginBottom: 8 }}>
                  <label className="field-label">min_interval_seconds</label>
                  <input
                    className="input"
                    type="number"
                    value={form.learning.minInterval}
                    onChange={(e) => patch({ learning: { ...form.learning, minInterval: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ marginBottom: 8 }}>
                  <label className="field-label">max_interval_seconds</label>
                  <input
                    className="input"
                    type="number"
                    value={form.learning.maxInterval}
                    onChange={(e) => patch({ learning: { ...form.learning, maxInterval: Number(e.target.value) } })}
                  />
                </div>
              </div>
            </div>
          )}

          {/* Вложенный блок proactive: sub-поля видны при включении */}
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={form.features.proactive}
              onChange={() => patch({ features: { ...form.features, proactive: !form.features.proactive } })}
            />
            <span>
              proactive
              <InfoButton helpKey="pc.proactive" />
            </span>
          </label>
          {form.features.proactive && (
            <div className="pcreate-sub">
              <div className="field-grid">
                <div className="field" style={{ marginBottom: 8 }}>
                  <label className="field-label">check_interval_minutes</label>
                  <input
                    className="input"
                    type="number"
                    value={form.proactive.checkInterval}
                    onChange={(e) => patch({ proactive: { ...form.proactive, checkInterval: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ marginBottom: 8 }}>
                  <label className="field-label">silence_threshold_minutes</label>
                  <input
                    className="input"
                    type="number"
                    value={form.proactive.silenceThresholdMin}
                    onChange={(e) => patch({ proactive: { ...form.proactive, silenceThresholdMin: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ marginBottom: 8 }}>
                  <label className="field-label">initiative_probability</label>
                  <input
                    className="input"
                    type="number"
                    step="0.05"
                    value={form.proactive.probability}
                    onChange={(e) => patch({ proactive: { ...form.proactive, probability: Number(e.target.value) } })}
                  />
                </div>
                <div className="field" style={{ marginBottom: 8 }}>
                  <label className="field-label">max_daily_initiatives</label>
                  <input
                    className="input"
                    type="number"
                    value={form.proactive.maxDaily}
                    onChange={(e) => patch({ proactive: { ...form.proactive, maxDaily: Number(e.target.value) } })}
                  />
                </div>
              </div>
            </div>
          )}

          <div className="field">
            <label className="field-label" htmlFor="pc-trigger">
              {t('pc.triggerLabel')}
              <InfoButton helpKey="pc.triggerWords" />
            </label>
            <input
              id="pc-trigger"
              className="input"
              placeholder={t('pc.triggerPh')}
              value={form.triggerWords}
              onChange={(e) => patch({ triggerWords: e.target.value })}
              spellCheck={false}
            />
          </div>

          <div className="pcreate-sec">{t('pc.secPrompt')}</div>
          <div className="field">
            <label className="field-label" htmlFor="pc-prompt">
              system_prompt
              <InfoButton helpKey="pc.systemPrompt" />
            </label>
            <textarea
              id="pc-prompt"
              className="input pcreate-textarea pcreate-prompt"
              rows={8}
              placeholder={t('pc.promptPh')}
              value={form.systemPrompt}
              onChange={(e) => patch({ systemPrompt: e.target.value })}
              spellCheck={false}
            />
          </div>

          <div className="pcreate-sec">{t('pc.secSettings')}</div>
          <div className="field-grid">
            <div className="field">
              <label className="field-label">
                temperature
                <InfoButton helpKey="settings.temperature" />
              </label>
              <input className="input" type="number" step="0.05" value={form.temperature} onChange={(e) => patch({ temperature: Number(e.target.value) })} />
            </div>
            <div className="field">
              <label className="field-label">
                max_tokens
                <InfoButton helpKey="settings.maxTokens" />
              </label>
              <input className="input" type="number" value={form.maxTokens} onChange={(e) => patch({ maxTokens: Number(e.target.value) })} />
            </div>
            <div className="field">
              <label className="field-label">
                top_p
                <InfoButton helpKey="settings.topP" />
              </label>
              <input className="input" type="number" step="0.05" value={form.topP} onChange={(e) => patch({ topP: Number(e.target.value) })} />
            </div>
          </div>
          {/* Провайдер и модель в форме не задаются: они настраиваются потом в досье персоны */}
          {createError && <div className="field-hint">// {createError}</div>}
        </div>

        {/* Правая панель: YAML всегда редактируем (textarea). Пока оператор
            не трогал текст — это живое превью формы; любая правка/вставка
            переводит панель в ручной режим (rawYaml): на бэкенд уйдёт он,
            форма слева перестаёт влиять на текст */}
        <div className="pcreate-yaml-col">
          <div className="pcreate-yaml-head">
            <span className="field-label" style={{ marginBottom: 0 }}>
              {rawYaml !== null ? t('pc.yamlPasted') : t('pc.yamlPreview')}
            </span>
            <div className="pcreate-yaml-actions">
              <button type="button" className="btn btn--ghost" title={t('pc.pasteYamlTitle')} onClick={pasteFromClipboard}>
                {t('pc.pasteYaml')}
              </button>
              <button type="button" className="btn btn--ghost" title={t('pc.loadYamlTitle')} onClick={() => fileRef.current?.click()}>
                {t('pc.loadYaml')}
              </button>
              {rawYaml !== null && (
                <button type="button" className="btn btn--ghost" onClick={() => setRawYaml(null)}>
                  {t('pc.backToForm')}
                </button>
              )}
              {drafts !== null && rawYaml === null && (
                <button type="button" className="btn btn--ghost" onClick={saveDraft}>
                  {t('pc.draftSave')}
                </button>
              )}
              <button type="button" className="btn btn--ghost" onClick={copyYaml}>
                {copied ? t('common.copied') : t('pc.copyYaml')}
              </button>
            </div>
          </div>
          <input
            ref={fileRef}
            type="file"
            accept=".yaml,.yml,text/yaml"
            hidden
            onChange={(e) => {
              loadYamlFile(e.target.files?.[0]);
              e.target.value = '';
            }}
          />
          {draftNote && <div className="pcreate-draft-note">{draftNote}</div>}
          <textarea
            className="input pcreate-yaml pcreate-yaml--edit"
            value={rawYaml ?? yaml}
            onChange={(e) => setRawYaml(e.target.value)}
            spellCheck={false}
          />
          {rawYaml !== null && <div className="field-hint">{t('pc.rawYamlHint')}</div>}
        </div>
      </div>
    </FormModal>
  );
}
