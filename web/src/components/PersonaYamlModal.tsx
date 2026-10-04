import { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { api, ApiError, isMemoryConflict } from '../api';
import { refetchPersonas } from '../apiData';
import { alertDialog, confirmDialog } from '../dialogStore';
import { askLeftoverMemory, renamePersonaId } from '../personaRename';
import FormModal from './FormModal';
import PersonaFormFields from './PersonaFormFields';
import { defaultForm, formFromYaml, prettifyYaml, yamlWithForm } from './personaYamlSync';
import type { FormState } from './personaYamlSync';

/* Правка существующей персоны (кнопка «Редактировать» в «Персонах» и в шапке
   чата): слева — те же поля, что при создании, справа — полный persona.yaml.
   Поля и текст синхронизированы в обе стороны (personaYamlSync); ключи, которых
   нет в форме (color, llm, …), и комментарии сохраняются. YAML целиком можно
   скопировать и вставить. id во вставленном тексте возвращается к тому, что
   записан в файле; сменить id — отдельной кнопкой у поля (переносит файл,
   папку памяти, аватар — personaRename). Только при живом бэкенде. */

export default function PersonaYamlModal({
  personaId,
  onClose,
  onRenamed,
}: {
  personaId: string;
  onClose: () => void;
  onRenamed?: (newId: string) => void; // id сменён — родитель переключается на новый
}) {
  const { t } = useI18n();
  // savedText — что сейчас в файле, draft — текст в редакторе, form — его поля
  const [savedText, setSavedText] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(() => defaultForm());
  const [yamlError, setYamlError] = useState(''); // текст не разбирается — поля не синхронизируются
  const [normalized, setNormalized] = useState(false); // при загрузке \n и слэши переведены в блоки |
  const [idKept, setIdKept] = useState(false); // во вставленном YAML был чужой id
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [restartNote, setRestartNote] = useState(false);
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);
  const noteTimer = useRef<number | undefined>(undefined);
  const fileId = useRef(personaId); // id из файла (обычно = имени файла, но не всегда)

  const dirty = draft !== null && savedText !== null && draft !== savedText;

  // Новый текст (загрузка, ручная правка, вставка) → поля формы
  const applyText = (text: string) => {
    setDraft(text);
    const r = formFromYaml(text, defaultForm());
    if ('error' in r) {
      setYamlError(r.error);
      return;
    }
    setYamlError('');
    setForm(r.form);
  };

  useEffect(() => {
    setSavedText(null);
    setDraft(null);
    setLoadError(false);
    setSaveError('');
    setRestartNote(false);
    setIdKept(false);
    api
      .getPersonaYaml(personaId)
      .then((r) => {
        const pretty = prettifyYaml(r.yaml);
        setSavedText(r.yaml);
        setNormalized(pretty !== r.yaml);
        const parsed = formFromYaml(pretty, defaultForm());
        fileId.current = 'form' in parsed && parsed.form.id ? parsed.form.id : personaId;
        applyText(pretty);
      })
      .catch(() => setLoadError(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personaId]);

  useEffect(() => () => window.clearTimeout(noteTimer.current), []);

  // Мигание «Скопировано»/«Сохранено»
  const flash = (set: (v: boolean) => void) => {
    set(true);
    window.clearTimeout(noteTimer.current);
    noteTimer.current = window.setTimeout(() => {
      setCopied(false);
      setSaved(false);
    }, 1500);
  };

  // Правка поля → точечная правка ключа в тексте
  const patch = (p: Partial<FormState>) => {
    const next = { ...form, ...p };
    setForm(next);
    if (draft !== null && !yamlError) setDraft(yamlWithForm(draft, form, next));
  };

  // Вставка YAML целиком (из буфера): id остаётся тем, что записан в файле
  const pasteText = (text: string) => {
    if (!text.trim()) return;
    const pretty = prettifyYaml(text);
    const r = formFromYaml(pretty, defaultForm());
    if ('form' in r && r.form.id !== fileId.current) {
      applyText(yamlWithForm(pretty, r.form, { ...r.form, id: fileId.current }));
      setIdKept(true);
    } else {
      applyText(pretty);
      setIdKept(false);
    }
    setSaveError('');
  };

  const paste = () => {
    navigator.clipboard?.readText().then(pasteText).catch(() => {});
  };

  const copy = () => {
    if (draft === null) return;
    navigator.clipboard
      ?.writeText(draft)
      .then(() => flash(setCopied))
      .catch(() => {});
  };

  const save = () => {
    if (draft === null || !dirty || saving || yamlError) return;
    setSaving(true);
    setSaveError('');
    api
      .savePersonaYaml(personaId, draft)
      .then((r) => {
        setSavedText(draft);
        setNormalized(false);
        setIdKept(false);
        setRestartNote(r.restart_required);
        flash(setSaved);
        refetchPersonas(); // имя/описание/функции на карточках и в чате
      })
      .catch((e) => setSaveError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setSaving(false));
  };

  // Смена id: подтверждение → бэкенд + браузерные хранилища → родитель на новый id.
  // Под newId осталась память удалённой персоны — отдельный выбор (архив/подхватить)
  const renameId = async (newId: string): Promise<string | null> => {
    const ok = await confirmDialog({
      title: t('pc.idRenameConfirmTitle', { from: personaId, to: newId }),
      message: t('pc.idRenameConfirm'),
      confirmLabel: t('pc.idRename'),
    });
    if (!ok) return '';
    try {
      let result: { restartRequired: boolean };
      try {
        result = await renamePersonaId(personaId, newId);
      } catch (e) {
        if (!isMemoryConflict(e)) throw e;
        const memory = await askLeftoverMemory(t, newId, e.data?.can_keep !== false);
        if (!memory) return '';
        result = await renamePersonaId(personaId, newId, memory);
      }
      const { restartRequired } = result;
      onRenamed?.(newId);
      if (restartRequired) {
        void alertDialog({ title: t('pc.idRenamedTitle'), message: t('pc.idRenamedRestart', { id: newId.toUpperCase() }) });
      }
      return null;
    } catch (e) {
      return e instanceof ApiError ? e.message : String(e);
    }
  };

  // Подсказка под текстом: ошибка разбора важнее остального
  const hint = yamlError
    ? t('pc.yamlParseError', { msg: yamlError })
    : idKept
      ? t('yaml.idKept', { id: fileId.current })
      : normalized
        ? t('yaml.normalized')
        : t('yaml.syncHint');

  return (
    <FormModal
      title={t('yaml.title')}
      badge={`${personaId}.yaml`}
      xl
      submitLabel={saved && !dirty ? t('yaml.saved') : saving ? t('yaml.saving') : t('yaml.save')}
      submitDisabled={!dirty || saving || !!yamlError}
      onSubmit={save}
      onClose={onClose}
    >
      {loadError ? (
        <div className="yaml-view yaml-view--note">{t('yaml.error')}</div>
      ) : draft === null ? (
        <div className="yaml-view yaml-view--note">{t('yaml.loading')}</div>
      ) : (
        <div className="pcreate-columns">
          <div className="pcreate-form-col">
            <PersonaFormFields
              form={form}
              patch={patch}
              onNameChange={(name) => patch({ name })}
              onIdChange={() => {}}
              idLocked
              fileId={personaId}
              onIdRename={renameId}
              idRenameBlocked={dirty ? t('pc.idRenameUnsaved') : undefined}
            />
            {saveError && <div className="field-hint">// {saveError}</div>}
            {restartNote && !saveError && <div className="field-hint">// {t('settings.restartRequired')}</div>}
          </div>

          <div className="pcreate-yaml-col">
            <div className="pcreate-yaml-head">
              <span className="field-label" style={{ marginBottom: 0 }}>
                persona.yaml{dirty ? ` · ${t('yaml.unsaved')}` : ''}
              </span>
              <div className="pcreate-yaml-actions">
                <button type="button" className="btn btn--ghost" title={t('yaml.pasteTitle')} onClick={paste}>
                  {t('pc.pasteYaml')}
                </button>
                <button type="button" className="btn btn--ghost" onClick={copy}>
                  {copied ? t('yaml.copied') : t('pc.copyYaml')}
                </button>
              </div>
            </div>
            <textarea
              className="input pcreate-yaml pcreate-yaml--edit"
              value={draft}
              onChange={(e) => applyText(e.target.value)}
              spellCheck={false}
            />
            <div className="field-hint">{hint}</div>
          </div>
        </div>
      )}
    </FormModal>
  );
}
