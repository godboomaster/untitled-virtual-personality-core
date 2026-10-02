import { useState } from 'react';
import { useI18n } from '../i18n';
import { PERSONA_ID_RE } from '../personaRename';
import InfoButton from './InfoButton';
import Select from './Select';
import { simpleFlags, tierSettings } from './personaYamlSync';
import type { FormState, IntellectTier } from './personaYamlSync';

/* Поля конфига персоны (базовое / features / system_prompt / settings) —
   общие для создания новой персоны и правки существующей. Структура
   повторяет persona.yaml; состояние и синхронизация с YAML — у родителя. */

interface PersonaFormFieldsProps {
  form: FormState;
  patch: (p: Partial<FormState>) => void;
  onNameChange: (name: string) => void; // создание: имя заодно подставляет id
  onIdChange: (id: string) => void;
  idLocked?: boolean; // правка существующей персоны: id = имя файла, напрямую не правится
  // Смена id существующей персоны (файл, папка памяти, аватар): null — успех,
  // строка — ошибка для подсказки под полем. Без колбэка кнопки «сменить» нет
  onIdRename?: (newId: string) => Promise<string | null>;
  idRenameBlocked?: string; // почему сменить id сейчас нельзя (несохранённые правки)
  // Настоящий id существующей персоны — имя её файла. Поле id: в YAML может с ним
  // расходиться (правили руками) — показываем и сравниваем с именем файла
  fileId?: string;
  autoFocusName?: boolean;
}

export default function PersonaFormFields({
  form,
  patch,
  onNameChange,
  onIdChange,
  idLocked,
  onIdRename,
  idRenameBlocked,
  fileId,
  autoFocusName,
}: PersonaFormFieldsProps) {
  const { t } = useI18n();
  // Режим смены id: null — поле заблокировано, строка — вводимый новый id
  const [newId, setNewId] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameError, setRenameError] = useState('');

  const idValid = newId !== null && PERSONA_ID_RE.test(newId.trim());
  const currentId = fileId ?? form.id;
  const idChanged = newId !== null && newId.trim() !== currentId;

  const applyRename = async () => {
    if (!onIdRename || newId === null || !idValid || !idChanged || renaming) return;
    setRenaming(true);
    setRenameError('');
    const err = await onIdRename(newId.trim());
    setRenaming(false);
    if (err === null) setNewId(null);
    else if (err) setRenameError(err);
  };

  return (
    <>
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
            onChange={(e) => onNameChange(e.target.value)}
            autoFocus={autoFocusName}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="pc-id">
            id
            <InfoButton helpKey="pc.id" />
          </label>
          {newId === null ? (
            <div className="pcreate-id-row">
              <input
                id="pc-id"
                className="input"
                placeholder="persona_id"
                value={idLocked ? currentId : form.id}
                onChange={(e) => onIdChange(e.target.value)}
                disabled={idLocked}
                title={idLocked && !onIdRename ? t('pc.idLocked') : undefined}
                spellCheck={false}
              />
              {idLocked && onIdRename && (
                <button
                  type="button"
                  className="btn btn--ghost pcreate-id-btn"
                  disabled={!!idRenameBlocked}
                  title={idRenameBlocked || t('pc.idRenameTitle')}
                  onClick={() => {
                    setNewId(currentId);
                    setRenameError('');
                  }}
                >
                  {t('pc.idRename')}
                </button>
              )}
            </div>
          ) : (
            <>
              <input
                id="pc-id"
                className="input"
                value={newId}
                onChange={(e) => {
                  setNewId(e.target.value);
                  setRenameError('');
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void applyRename();
                  if (e.key === 'Escape') {
                    e.stopPropagation();
                    setNewId(null);
                  }
                }}
                disabled={renaming}
                spellCheck={false}
                autoFocus
              />
              <div className="pcreate-id-actions">
                <button
                  type="button"
                  className="btn btn--primary pcreate-id-btn"
                  disabled={!idValid || !idChanged || renaming}
                  onClick={() => void applyRename()}
                >
                  {renaming ? '…' : t('common.apply')}
                </button>
                <button
                  type="button"
                  className="btn btn--ghost pcreate-id-btn"
                  disabled={renaming}
                  onClick={() => setNewId(null)}
                >
                  {t('common.cancel')}
                </button>
              </div>
              <div className="field-hint">
                {renameError
                  ? `// ${renameError}`
                  : !idValid
                    ? t('pc.idFormat')
                    : t('pc.idRenameHint')}
              </div>
            </>
          )}
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
        <Select
          id="pc-intellect"
          value={form.intellectTier}
          options={(['normal', 'primitive', 'bot', 'none'] as const).map((tier) => ({
            value: tier,
            label: t(`pc.intellect.${tier}`),
            hint: t(`pc.intellect.${tier}Hint`),
          }))}
          onChange={(v) => {
            const tier = v as IntellectTier;
            patch({ intellectTier: tier, ...(tier !== 'none' ? tierSettings[tier] : {}) });
          }}
        />
      </div>

      <div className="pcreate-sec">
        {t('pc.secFeatures')}
        <InfoButton helpKey="pc.features" />
      </div>
      {/* Поле owner намеренно отсутствует: пользователь всегда один и всегда владелец */}
      <div className="pcreate-flags">
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
    </>
  );
}
