/* SkinPanel — блок управления скинами персон (раздел «Персоны»).
   Скин — запись библиотеки (skinStore): до трёх файлов экранов (чат, досье,
   комната) + перекраска. Библиотека общая, персоне назначается скин из неё.
   Флоу: скачать шаблон(ы) → отдать нейросети (по одному экрану за раз) →
   загрузить файл(ы) новым скином или в экраны существующего → валидация →
   предпросмотр в песочнице → сохранить и применить. Либо сгенерировать
   скин нейросетью прямо здесь (SkinGenerator) — готовые файлы попадают
   в ту же загрузку. Под карточками
   библиотеки — одна рабочая область: загрузка, цвета или предпросмотр.
   При runtime-ошибке скина здесь же показывается отчёт по каждому экрану,
   который можно скопировать и вернуть редактору. */

import { useEffect, useState } from 'react';
import {
  SKIN_SCREENS,
  dismissSkinError,
  findEntry,
  refetchSkins,
  restoreBuiltinSkins,
  usePersonaSkin,
  useSkinFiles,
  useSkinLibrary,
} from '../skins/skinStore';
import type { PersonaSkins, SkinEntry } from '../skins/skinStore';
import type { SkinScreen } from '../skins/engine';
import type { Persona } from '../mockData';
import { useI18n, useMockData } from '../i18n';
import { buildSkinContractDoc } from '../skins/contract';
import SkinLibrary from './skins/SkinLibrary';
import type { SkinWorkspaceKind } from './skins/SkinLibrary';
import SkinUploader from './skins/SkinUploader';
import SkinColorEditor from './skins/SkinColorEditor';
import SkinPreview from './skins/SkinPreview';
import SkinGenerator from './skins/SkinGenerator';
import { SCREEN_NAME_KEY, TEMPLATES, downloadFile, skinTitle } from './skins/skinUi';

interface Workspace {
  kind: SkinWorkspaceKind;
  id: string | null; // null — новый скин (загрузка)
  initial?: PersonaSkins; // готовые файлы для загрузки (генератор скинов)
  nonce?: number; // новая порция готовых файлов — свежая загрузка, а не старая
}

// Предпросмотр скина библиотеки как есть (с его перекраской)
function LibraryPreview({ entry, persona, onClose }: { entry: SkinEntry; persona: Persona; onClose: () => void }) {
  const { t } = useI18n();
  const { files, loading } = useSkinFiles(entry.id);
  return (
    <div className="skin-workspace">
      <div className="skin-workspace-title">
        {t('skin.preview')}: {skinTitle(entry, t)}
      </div>
      {loading && <p className="ctx-note">{t('skin.loadingFiles')}</p>}
      <SkinPreview files={files} persona={persona} colors={entry.colors} hueShift={entry.hueShift} />
      <div className="skin-actions">
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          {t('skin.close')}
        </button>
      </div>
    </div>
  );
}

export default function SkinPanel() {
  const { t, lang } = useI18n();
  const { personas } = useMockData();

  const [personaId, setPersonaId] = useState(personas[0]?.id ?? '');
  const persona = personas.find((p) => p.id === personaId) ?? personas[0];
  const library = useSkinLibrary();
  const { entry: current, broken } = usePersonaSkin(persona?.id ?? '');

  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  // Ошибки валидации загруженных файлов
  const [errors, setErrors] = useState<string[]>([]);
  const [copied, setCopied] = useState(false);

  // Открытие панели — свежая библиотека (могли поменять в другом браузере)
  useEffect(() => {
    void refetchSkins();
  }, []);

  if (!persona) return null;

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  const openWorkspace = (kind: SkinWorkspaceKind, id: string | null, initial?: PersonaSkins) => {
    setErrors([]);
    setWorkspace((cur) =>
      cur && cur.kind === kind && cur.id === id && !initial ? null : { kind, id, initial, nonce: initial ? Date.now() : undefined },
    );
  };
  const closeWorkspace = () => setWorkspace(null);

  // Скин рабочей области (удалён в другом окне — область закрывается)
  const wsEntry = workspace?.id ? (findEntry(workspace.id) ?? null) : null;
  const wsMissing = workspace?.id != null && wsEntry == null;

  const hasBroken = SKIN_SCREENS.some((s) => broken[s]);
  const screenStatus = (s: SkinScreen) =>
    broken[s] ? 'skin.statusBroken' : current?.screens[s] ? 'skin.statusCustom' : 'skin.statusDefault';

  return (
    <div className="card skin-panel">
      <div className="card-title-row">
        <h3 className="card-title">{t('skin.title')}</h3>
        <span className={`badge ${current && !hasBroken ? 'badge--active' : ''} ${hasBroken ? 'skin-badge-broken' : ''}`}>
          {t(hasBroken ? 'skin.statusBroken' : current ? 'skin.statusCustom' : 'skin.statusDefault')}
        </span>
      </div>
      <p className="ctx-note">{t('skin.subtitle')}</p>

      {/* Выбор персоны, которой назначаем скин */}
      <div className="tabs room-persona-tabs">
        {personas.map((p) => (
          <button
            key={p.id}
            type="button"
            className={`tab ${p.id === persona.id ? 'tab--active' : ''}`}
            onClick={() => {
              setPersonaId(p.id);
              setErrors([]);
            }}
          >
            {p.name}
          </button>
        ))}
      </div>

      {/* Текущий скин персоны и состояние его экранов */}
      <div className="skin-persona-row">
        <span>
          {t('skin.personaSkin', { name: persona.name })}{' '}
          <b>{current ? skinTitle(current, t) : t('skin.personaNoSkin')}</b>
        </span>
        {current &&
          SKIN_SCREENS.map((s) => (
            <span
              key={s}
              className={`badge ${current.screens[s] && !broken[s] ? 'badge--active' : ''} ${broken[s] ? 'skin-badge-broken' : ''}`}
            >
              {t(SCREEN_NAME_KEY[s])}: {t(screenStatus(s))}
            </span>
          ))}
      </div>

      <p className="ctx-note">
        {t(
          library.mode === 'server'
            ? 'skin.storageServer'
            : library.mode === 'local'
              ? 'skin.storageLocal'
              : 'skin.storageLoading',
        )}
      </p>

      {/* Ошибка хранилища/сервера (квота, отказ сервера, миграция) */}
      {library.error && (
        <div className="skin-report">
          <code>{t(library.error.key, { detail: library.error.detail })}</code>
          <button type="button" className="btn btn--ghost" onClick={dismissSkinError}>
            {t('skin.dismiss')}
          </button>
        </div>
      )}

      {/* Отчёты о runtime-ошибках по экранам: копируются нейросети-редактору */}
      {SKIN_SCREENS.filter((s) => broken[s]).map((s) => (
        <div className="skin-report" key={s}>
          <div className="skin-report-title">
            {t('skin.brokenTitle')} · {t(SCREEN_NAME_KEY[s])}
          </div>
          <code>{broken[s]}</code>
          <button type="button" className="btn btn--ghost" onClick={() => copyText(t('skin.reportPrefix') + broken[s])}>
            {copied ? t('skin.copied') : t('skin.copyReport')}
          </button>
        </div>
      ))}

      {/* Библиотека */}
      <div className="card-title-row skin-library-head">
        <h4 className="card-title">{t('skin.libraryTitle')}</h4>
        <div className="skin-library-head-actions">
          {library.hiddenBuiltins > 0 && (
            <button type="button" className="btn btn--ghost" onClick={() => restoreBuiltinSkins().catch(() => {})}>
              {t('skin.restoreBuiltins', { n: library.hiddenBuiltins })}
            </button>
          )}
          <button type="button" className="btn btn--ghost" onClick={() => openWorkspace('upload', null)}>
            {t('skin.newSkin')}
          </button>
        </div>
      </div>
      {library.entries.length === 0 && <p className="ctx-note">{t('skin.libraryEmpty')}</p>}
      <SkinLibrary
        entries={library.entries}
        assignments={library.assignments}
        persona={persona}
        personas={personas}
        open={workspace}
        onOpen={openWorkspace}
      />

      {/* Ошибки валидации загруженного файла */}
      {errors.length > 0 && (
        <div className="skin-report">
          <div className="skin-report-title">{t('skin.errorsTitle')}</div>
          <ul className="skin-error-list">
            {errors.map((err, i) => (
              <li key={i}>{err}</li>
            ))}
          </ul>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => copyText(t('skin.reportPrefix') + errors.join('\n'))}
          >
            {copied ? t('skin.copied') : t('skin.copyReport')}
          </button>
        </div>
      )}

      {/* Рабочая область: загрузка / цвета / предпросмотр */}
      {workspace && !wsMissing && (
        <div key={`${workspace.kind}:${workspace.id ?? 'new'}:${workspace.nonce ?? ''}`}>
          {workspace.kind === 'upload' && (
            <SkinUploader
              entry={wsEntry}
              persona={persona}
              initial={workspace.initial}
              onErrors={setErrors}
              onDone={closeWorkspace}
            />
          )}
          {workspace.kind === 'colors' && wsEntry && (
            <SkinColorEditor entry={wsEntry} persona={persona} onDone={closeWorkspace} />
          )}
          {workspace.kind === 'preview' && wsEntry && (
            <LibraryPreview entry={wsEntry} persona={persona} onClose={closeWorkspace} />
          )}
        </div>
      )}

      {/* Генерация скина нейросетью: готовые файлы экранов уходят в обычный
          флоу загрузки (валидация, проверка, предпросмотр, сохранение) */}
      <SkinGenerator
        persona={persona}
        entries={library.entries}
        onGenerated={(files) => openWorkspace('upload', null, files)}
      />

      {/* Создание скина внешней нейросетью: шаблоны экранов + промпт
          (инструкция + контракт скина, собранный из кода) */}
      <div className="skin-actions skin-templates">
        <span className="field-label">{t('skin.templates')}</span>
        {SKIN_SCREENS.map((s) => (
          <button
            key={s}
            type="button"
            className="btn btn--ghost"
            onClick={() => downloadFile(`skin-template.${s}.html`, TEMPLATES[s])}
          >
            ⬇ {t(SCREEN_NAME_KEY[s])}
          </button>
        ))}
        <button type="button" className="btn btn--ghost" onClick={() => copyText(t('skin.llmPrompt') + '\n\n' + buildSkinContractDoc(lang))}>
          {copied ? t('skin.copied') : t('skin.copyPrompt')}
        </button>
      </div>
      <p className="ctx-note">{t('skin.screenshotHint')}</p>
    </div>
  );
}
