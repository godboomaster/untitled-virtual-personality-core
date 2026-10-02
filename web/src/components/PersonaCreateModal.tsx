import { useEffect, useMemo, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { api, ApiError } from '../api';
import type { PersonaDraft } from '../api';
import FormModal from './FormModal';
import Select from './Select';
import PersonaFormFields from './PersonaFormFields';
import { defaultForm, formFromYaml, simpleFlags, transliterate, yamlWithForm } from './personaYamlSync';
import type { FormState } from './personaYamlSync';

/* Модалка создания персоны в виде YAML-редактора конфига: слева форма,
   повторяющая структуру persona.yaml (базовое / features / system_prompt /
   settings), справа — YAML: живое превью формы, либо вставленный/загруженный
   текст — тогда поля формы заполняются из него и правки полей пишутся в него же
   (personaYamlSync). Флаги features — как в app/personas/*.yaml; флаги
   мессенджер-бота export_server/restore_memory в форму не входят — в вебе они
   не действуют, но во вставленном YAML сохраняются. */

interface PersonaCreateModalProps {
  initial?: { name?: string }; // предзаполнение (legacy, из Home)
  onClose: () => void;
  onCreate: () => void; // персона создана на бэкенде — родитель обновляет список
}

export default function PersonaCreateModal({ initial, onClose, onCreate }: PersonaCreateModalProps) {
  const { t } = useI18n();
  const [form, setForm] = useState<FormState>(() => defaultForm(initial?.name));
  const [idTouched, setIdTouched] = useState(false); // id авто, пока оператор не правил вручную
  const [copied, setCopied] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  // Режим «готовый YAML»: текст вставлен, загружен файлом или правлен вручную —
  // на бэкенд уходит rawYaml, форма синхронизирована с ним в обе стороны
  const [rawYaml, setRawYaml] = useState<string | null>(null);
  const [yamlError, setYamlError] = useState(''); // текст не разбирается — форма не синхронизируется
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

  // Частичное обновление формы; в режиме готового YAML правка сразу вносится в текст
  const patch = (p: Partial<FormState>) => {
    const next = { ...form, ...p };
    setForm(next);
    if (rawYaml !== null && !yamlError) setRawYaml(yamlWithForm(rawYaml, form, next));
  };

  // Новый текст YAML (вставка, файл, ручная правка) → поля формы
  const setRawText = (text: string) => {
    setRawYaml(text);
    const r = formFromYaml(text, defaultForm());
    if ('error' in r) {
      setYamlError(r.error);
      return;
    }
    setYamlError('');
    setForm(r.form);
    setIdTouched(true); // id из YAML не перезатирать транслитом имени
  };

  const backToForm = () => {
    setRawYaml(null);
    setYamlError('');
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
    setRawText(text);
    setCreateError('');
  };

  const pasteFromClipboard = () => {
    // Буфер может быть недоступен (не secure context) — тогда просто
    // переключаем панель в режим вставки: пустое поле ждёт Ctrl+V
    if (navigator.clipboard?.readText) {
      navigator.clipboard.readText().then((text) => pasteYamlText(text || yaml)).catch(() => setRawText(yaml));
    } else {
      setRawText(yaml);
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
                <Select
                  value={draftId ?? ''}
                  options={[
                    { value: '', label: t('pc.draftSelect') },
                    ...drafts.map((d) => ({
                      value: d.id,
                      label: (d.name || d.id) + ' · ' + new Date(d.updated_at * 1000).toLocaleString(),
                    })),
                  ]}
                  onChange={(v) => {
                    const d = drafts.find((x) => x.id === v);
                    if (d) loadDraft(d);
                  }}
                />
                {draftId && (
                  <button type="button" className="btn btn--danger" onClick={() => removeDraft(draftId)}>
                    {t('common.delete')}
                  </button>
                )}
              </div>
            </div>
          )}

          <PersonaFormFields
            form={form}
            patch={patch}
            onNameChange={(v) => patch(idTouched ? { name: v } : { name: v, id: transliterate(v) })}
            onIdChange={(v) => {
              patch({ id: v });
              setIdTouched(true);
            }}
            autoFocusName
          />
          {/* Провайдер и модель в форме не задаются: они настраиваются потом в досье персоны */}
          {createError && <div className="field-hint">// {createError}</div>}
        </div>

        {/* Правая панель: YAML всегда редактируем (textarea). Пока оператор
            не трогал текст — это живое превью формы; любая правка/вставка
            переводит панель в ручной режим (rawYaml): на бэкенд уйдёт он,
            поля слева заполняются из него, а их правки пишутся в текст */}
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
                <button type="button" className="btn btn--ghost" onClick={backToForm}>
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
            onChange={(e) => setRawText(e.target.value)}
            spellCheck={false}
          />
          {rawYaml !== null && (
            <div className="field-hint">
              {yamlError ? t('pc.yamlParseError', { msg: yamlError }) : t('pc.rawYamlHint')}
            </div>
          )}
        </div>
      </div>
    </FormModal>
  );
}
