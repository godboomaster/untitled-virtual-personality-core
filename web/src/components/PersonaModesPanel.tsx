import { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { api, ApiError, isMissingRoute, isNativeApp } from '../api';
import type { PersonaModeInfo, PersonaModesList } from '../api';
import { refetchPersonas, useApiOnline } from '../apiData';
import { confirmDialog } from '../dialogStore';
import { useOfflineMode } from '../offline/state';
import { saveBlob } from '../sentFiles';

/* Режимы персоны: тот же персонаж с другим характером — файлы
   <id>_modes/<режим>.yaml (app/api/modes_api). Обращение именем режима в
   начале реплики включает его, обычное имя персоны — возвращает; переписка,
   факты и дневник общие. Одна и та же панель — вкладка «Режимы» в досье и в
   окне «Редактировать»: список, YAML режима (посмотреть, править, скачать),
   новый по заготовке, загрузка файла с диска, удаление. Файлы — на ядре:
   без связи с ним панель только говорит об этом. */

// Id из имени, как suggest_id на ядре: «Sancho (1)» → «sancho_1»
function suggestId(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/\.(ya?ml)$/i, '')
    .replace(/[^\p{L}\p{N}_-]+/gu, '_')
    .replace(/^[_-]+|[_-]+$/g, '')
    .slice(0, 40)
    .replace(/[_-]+$/g, '');
}

const ID_RE = /^[\p{L}\p{N}_-]{1,40}$/u;

interface Editing {
  mode: string;
  text: string;
  saved: string; // что в файле ('' — новый, ещё не записан)
  create: boolean;
  builtin: boolean;
}

function errText(e: unknown, t: (key: string) => string): string {
  if (isMissingRoute(e)) return t('common.coreOutdated');
  return e instanceof ApiError || e instanceof Error ? e.message : String(e);
}

export default function PersonaModesPanel({ personaId }: { personaId: string }) {
  const { t, lang } = useI18n();
  const apiOnline = useApiOnline();
  const offlineMode = useOfflineMode();
  const [list, setList] = useState<PersonaModesList | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null); // текст — при отрисовке
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notes, setNotes] = useState<string[]>([]); // итог последнего действия + предупреждения ядра
  const [editing, setEditing] = useState<Editing | null>(null);
  const [creating, setCreating] = useState<{ name: string; id: string; idTouched: boolean } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const online = apiOnline && !offlineMode;

  useEffect(() => {
    setList(null);
    setLoadError(null);
    setError('');
    setNotes([]);
    setEditing(null);
    setCreating(null);
    if (!online) return;
    let stale = false;
    api
      .getPersonaModes(personaId)
      .then((r) => !stale && setList(r))
      .catch((e) => !stale && setLoadError(e ?? 'error'));
    return () => {
      stale = true;
    };
  }, [online, personaId]);

  if (!online) {
    return (
      <div className="card">
        <h3 className="card-title">{t('modes.title')}</h3>
        <div className="files-empty">{t('modes.needCore')}</div>
      </div>
    );
  }

  const dirty = !!editing && editing.text !== editing.saved;

  // Есть несохранённая правка — спросить, прежде чем её бросить
  const dropEdits = async (): Promise<boolean> =>
    !dirty ||
    confirmDialog({
      title: t('modes.discardTitle'),
      message: t('modes.discard', { id: editing?.mode ?? '' }),
      confirmLabel: t('modes.discardOk'),
      danger: true,
    });

  // Ответ ядра после правки: список, что сделано и его предупреждения
  const applyResult = (r: PersonaModesList, done: string) => {
    setList(r);
    setNotes([done, ...(r.warnings ?? [])]);
    setError('');
    void refetchPersonas(); // имена режимов — в /api/personas (слушатель, Telegram)
  };

  const openMode = async (mode: string) => {
    if (editing?.mode === mode && !editing.create) {
      if (await dropEdits()) setEditing(null);
      return;
    }
    if (!(await dropEdits())) return;
    setError('');
    try {
      const r = await api.getPersonaModeYaml(personaId, mode);
      setEditing({ mode, text: r.yaml, saved: r.yaml, create: false, builtin: r.builtin });
      setCreating(null);
    } catch (e) {
      setError(errText(e, t));
    }
  };

  const save = async () => {
    if (!editing || busy || (!dirty && !editing.create)) return;
    setBusy(true);
    try {
      const r = await api.savePersonaMode(personaId, editing.mode, editing.text, editing.create);
      applyResult(r, t('modes.savedNote', { id: editing.mode }));
      setEditing({ ...editing, saved: editing.text, create: false, builtin: false });
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError(t('modes.exists', { id: editing.mode }));
      } else {
        setError(errText(e, t));
      }
    } finally {
      setBusy(false);
    }
  };

  const remove = async (m: PersonaModeInfo) => {
    const ok = await confirmDialog({
      title: t('modes.deleteTitle', { name: m.name }),
      message: m.overrides_builtin ? t('modes.deleteReset') : t('modes.deleteMsg', { id: m.id }),
      confirmLabel: t('common.delete'),
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api.deletePersonaMode(personaId, m.id);
      applyResult(r, r.reset ? t('modes.resetNote', { id: m.id }) : t('modes.deletedNote', { id: m.id }));
      if (editing?.mode === m.id) setEditing(null);
    } catch (e) {
      setError(errText(e, t));
    } finally {
      setBusy(false);
    }
  };

  const download = async (mode: string) => {
    try {
      const text = editing?.mode === mode ? editing.text : (await api.getPersonaModeYaml(personaId, mode)).yaml;
      await saveBlob(`${mode}.yaml`, new Blob([text], { type: 'application/x-yaml' }));
      if (isNativeApp()) setNotes([t('lore.savedPhone', { name: `${mode}.yaml` })]);
    } catch (e) {
      setError(errText(e, t));
    }
  };

  const upload = async (f: File) => {
    if (!(await dropEdits())) return;
    setBusy(true);
    setError('');
    setNotes([]);
    try {
      let r: PersonaModesList;
      try {
        r = await api.uploadPersonaMode(personaId, f);
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 409)) throw e;
        const ok = await confirmDialog({
          title: t('modes.replaceTitle'),
          message: t('modes.replace', { msg: e.message, file: f.name }),
          confirmLabel: t('modes.replaceOk'),
        });
        if (!ok) return;
        r = await api.uploadPersonaMode(personaId, f, { overwrite: true });
      }
      const mode = r.saved ?? '';
      applyResult(r, t('modes.uploadedNote', { file: f.name, id: mode }));
      // Сразу показать, что загрузилось
      if (mode) {
        const y = await api.getPersonaModeYaml(personaId, mode);
        setEditing({ mode, text: y.yaml, saved: y.yaml, create: false, builtin: y.builtin });
        setCreating(null);
      }
    } catch (e) {
      setError(errText(e, t));
    } finally {
      setBusy(false);
    }
  };

  const startCreate = async () => {
    if (!(await dropEdits())) return;
    setEditing(null);
    setError('');
    setNotes([]);
    setCreating({ name: '', id: '', idTouched: false });
  };

  const create = async () => {
    if (!creating) return;
    const id = creating.id.trim();
    if (!ID_RE.test(id)) {
      setError(t('modes.badId'));
      return;
    }
    if (list?.modes.some((m) => m.id === id)) {
      setError(t('modes.exists', { id }));
      return;
    }
    try {
      const r = await api.personaModeSkeleton(personaId, creating.name.trim() || id, lang);
      setEditing({ mode: id, text: r.yaml, saved: '', create: true, builtin: false });
      setCreating(null);
      setError('');
    } catch (e) {
      setError(errText(e, t));
    }
  };

  const holdText = (m: PersonaModeInfo) =>
    m.hold_minutes == null
      ? t('modes.holdSticky')
      : m.hold_minutes === 0
        ? t('modes.holdOnce')
        : t('modes.holdMin', { n: m.hold_minutes });

  const triggerLines = (m: PersonaModeInfo): string[] => {
    if (m.kind !== 'trigger') return [t(m.kind === 'master' ? 'modes.kindMaster' : 'modes.kindMortal')];
    const out: string[] = [];
    if (m.names.length) out.push(t('modes.byNames', { list: m.names.join(', ') }));
    if (m.keywords.length) out.push(t('modes.byWords', { list: m.keywords.join(', ') }));
    if (m.exit_keywords.length) out.push(t('modes.exitWords', { list: m.exit_keywords.join(', ') }));
    if (m.mood) out.push(t('modes.byMood', { mood: m.mood }));
    if (!out.length) out.push(t('modes.onlyCommand', { id: m.id }));
    else if (m.names.length || m.keywords.length) out.push(holdText(m));
    return out;
  };

  const editor = (e: Editing) => (
    <div className="mode-editor">
      <div className="pcreate-yaml-head">
        <span className="field-label" style={{ marginBottom: 0 }}>
          {e.mode}.yaml{e.create ? ` · ${t('modes.newBadge')}` : dirty ? ` · ${t('yaml.unsaved')}` : ''}
        </span>
        <div className="pcreate-yaml-actions">
          <button type="button" className="btn btn--ghost" onClick={() => void download(e.mode)}>
            {t('modes.download')}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void dropEdits().then((ok) => ok && setEditing(null))}
          >
            {t('common.close')}
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy || (!dirty && !e.create)}
            onClick={() => void save()}
          >
            {busy ? t('yaml.saving') : e.create ? t('common.create') : t('yaml.save')}
          </button>
        </div>
      </div>
      <textarea
        className="input pcreate-yaml pcreate-yaml--edit"
        value={e.text}
        onChange={(ev) => setEditing({ ...e, text: ev.target.value })}
        spellCheck={false}
      />
      <div className="field-hint">{e.builtin ? t('modes.builtinHint') : t('modes.editHint')}</div>
    </div>
  );

  const modes = list?.modes ?? [];
  const base = list?.base_names ?? [];
  return (
    <div className="card">
      <div className="card-title-row">
        <h3 className="card-title">{t('modes.title')}</h3>
        {modes.length > 0 && <span className="badge">{t('modes.badge', { n: modes.length })}</span>}
        <div className="files-actions">
          <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => fileRef.current?.click()}>
            {t('modes.upload')}
          </button>
          <button type="button" className="btn btn--primary" disabled={busy} onClick={() => void startCreate()}>
            {t('modes.add')}
          </button>
        </div>
      </div>
      <input
        ref={fileRef}
        type="file"
        hidden
        accept=".yaml,.yml"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void upload(f);
          e.target.value = '';
        }}
      />
      <div className="field-hint">
        {t('modes.hint', { base: base.length ? base.join(', ') : t('modes.baseNone') })}
      </div>
      {loadError != null && <div className="field-hint">⚠ {errText(loadError, t)}</div>}
      {error && <div className="field-hint">⚠ {error}</div>}
      {notes.map((n, i) => (
        <div key={i} className="field-hint">
          {i === 0 ? '✓ ' : '⚠ '}
          {n}
        </div>
      ))}
      {(list?.skipped ?? []).length > 0 && (
        <div className="field-hint">⚠ {t('modes.skipped', { list: list!.skipped.join(', ') })}</div>
      )}

      {creating && (
        <form
          className="mode-new"
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <label className="mode-new-field">
            <span className="field-label">{t('modes.newName')}</span>
            <input
              className="input"
              autoFocus
              value={creating.name}
              placeholder={t('modes.newNamePh')}
              maxLength={80}
              onChange={(e) =>
                setCreating({
                  ...creating,
                  name: e.target.value,
                  id: creating.idTouched ? creating.id : suggestId(e.target.value),
                })
              }
            />
          </label>
          <label className="mode-new-field">
            <span className="field-label">{t('modes.newId')}</span>
            <input
              className="input"
              value={creating.id}
              placeholder="sancho"
              maxLength={40}
              onChange={(e) => setCreating({ ...creating, id: e.target.value, idTouched: true })}
            />
          </label>
          <div className="mode-new-actions">
            <button type="button" className="btn btn--ghost" onClick={() => setCreating(null)}>
              {t('common.cancel')}
            </button>
            <button type="submit" className="btn btn--primary" disabled={!creating.id.trim()}>
              {t('modes.newGo')}
            </button>
          </div>
        </form>
      )}

      {editing?.create && editor(editing)}

      {list === null && loadError == null ? (
        <div className="files-empty">{t('yaml.loading')}</div>
      ) : modes.length === 0 && !editing?.create ? (
        <div className="files-empty">{t('modes.empty')}</div>
      ) : (
        <ul className="memory-list">
          {modes.map((m) => (
            <li key={m.id} className="lore-file">
              <div className="files-item">
                <div className="files-main">
                  <div className="files-name">
                    {m.name}
                    {m.name !== m.id && <span className="mode-id"> · {m.id}.yaml</span>}
                  </div>
                  {m.description && <div className="files-desc">{m.description}</div>}
                  <div className="files-meta mode-triggers">
                    {triggerLines(m).map((line) => (
                      <span key={line}>{line}</span>
                    ))}
                    {m.builtin && <span className="badge">{t('modes.builtin')}</span>}
                  </div>
                  {m.error && <div className="field-hint">⚠ {m.error}</div>}
                </div>
                <div className="files-actions">
                  <button type="button" className="btn btn--ghost" onClick={() => void openMode(m.id)}>
                    {editing?.mode === m.id && !editing.create ? t('modes.hide') : t('modes.open')}
                  </button>
                  {!m.builtin && (
                    <button type="button" className="btn btn--danger" disabled={busy} onClick={() => void remove(m)}>
                      {t('common.delete')}
                    </button>
                  )}
                </div>
              </div>
              {editing?.mode === m.id && !editing.create && editor(editing)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
