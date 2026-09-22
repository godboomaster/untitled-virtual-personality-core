import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { api, ApiError } from '../api';

/* YAML-файл персоны (кнопка в шапке чата): просмотр, копирование
   в буфер, редактирование с сохранением на сервере (бэкенд валидирует
   YAML). Закрытие — крестик, клик по оверлею, Esc. Только при живом бэкенде. */

export default function PersonaYamlModal({
  personaId,
  onClose,
}: {
  personaId: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  // yamlText — последняя загруженная/сохранённая версия, draft — то, что в редакторе
  const [yamlText, setYamlText] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [restartNote, setRestartNote] = useState(false);
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);

  const dirty = draft !== null && yamlText !== null && draft !== yamlText;

  // Закрытие по Esc
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    setYamlText(null);
    setDraft(null);
    setLoadError(false);
    setSaveError('');
    setRestartNote(false);
    api
      .getPersonaYaml(personaId)
      .then((r) => {
        setYamlText(r.yaml);
        setDraft(r.yaml);
      })
      .catch(() => setLoadError(true));
  }, [personaId]);

  // Мигание «Скопировано»/«Сохранено»
  useEffect(() => {
    if (!copied && !saved) return;
    const timer = setTimeout(() => {
      setCopied(false);
      setSaved(false);
    }, 1500);
    return () => clearTimeout(timer);
  }, [copied, saved]);

  const copy = async () => {
    if (draft === null) return;
    try {
      await navigator.clipboard.writeText(draft);
      setCopied(true);
    } catch {
      // Буфер недоступен (не secure-context) — просто ничего не делаем
    }
  };

  const save = () => {
    if (draft === null || !dirty || saving) return;
    setSaving(true);
    setSaveError('');
    api
      .savePersonaYaml(personaId, draft)
      .then((r) => {
        setYamlText(draft);
        setRestartNote(r.restart_required);
        setSaved(true);
      })
      .catch((e) => setSaveError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setSaving(false));
  };

  return (
    <div className="pcreate-overlay" onClick={onClose}>
      <div
        className="pcreate-panel bracketed pcreate-panel--wide"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="corner tl" />
        <div className="corner tr" />
        <div className="corner bl" />
        <div className="corner br" />
        <div className="pcreate-head">
          <span className="pcreate-title">{t('yaml.title')}</span>
          <span className="badge">{personaId}.yaml</span>
          <button type="button" className="pxe-close" onClick={onClose} aria-label={t('common.close')}>
            ✕
          </button>
        </div>
        <div className="pcreate-body">
          {loadError ? (
            <div className="yaml-view yaml-view--note">{t('yaml.error')}</div>
          ) : draft === null ? (
            <div className="yaml-view yaml-view--note">{t('yaml.loading')}</div>
          ) : (
            <textarea
              className="yaml-view yaml-edit"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              spellCheck={false}
            />
          )}
          {saveError && <div className="field-hint">// {saveError}</div>}
          {restartNote && !saveError && (
            <div className="field-hint">// {t('settings.restartRequired')}</div>
          )}
        </div>
        <div className="pcreate-foot">
          <button
            type="button"
            className="btn btn--ghost"
            disabled={draft === null}
            onClick={copy}
          >
            {copied ? t('yaml.copied') : t('yaml.copy')}
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={!dirty || saving}
            onClick={save}
          >
            {saved && !dirty ? t('yaml.saved') : saving ? t('yaml.saving') : t('yaml.save')}
          </button>
        </div>
      </div>
    </div>
  );
}
